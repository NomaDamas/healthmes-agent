"""Request-scoped autonomous search sessions for HealthMes context tools."""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import Lock
from time import monotonic
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from healthmes.decision.access import (
    AccessAuditEntry,
    ContextAccessLayer,
    ContextAccessPolicy,
    ContextAccessTurn,
)
from healthmes.decision.contracts import (
    ContextQuery,
    ContextResult,
    DecisionRequest,
    PrivacyLevel,
    SourceRef,
)
from healthmes.decision.providers import (
    UnknownCapabilityError,
    UnknownProviderError,
)
from healthmes.decision.validation import strict_model_validate

DECISION_SEARCH_SESSION_ID_PATTERN = r"^dss_[A-Za-z0-9_-]{43}$"

AccessPolicyResolver = Callable[[DecisionRequest], ContextAccessPolicy]


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )


class DecisionSearchSessionState(StrEnum):
    ACTIVE = "active"
    FINISHED = "finished"
    ABORTED = "aborted"
    EXPIRED = "expired"


class DecisionSearchSessionError(RuntimeError):
    """Base error with a stable machine-readable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class UnknownDecisionSearchSessionError(DecisionSearchSessionError):
    def __init__(self) -> None:
        super().__init__("decision_search_session_unknown")


class ExpiredDecisionSearchSessionError(DecisionSearchSessionError):
    def __init__(self) -> None:
        super().__init__("decision_search_session_expired")


class FinishedDecisionSearchSessionError(DecisionSearchSessionError):
    def __init__(self) -> None:
        super().__init__("decision_search_session_finished")


class AbortedDecisionSearchSessionError(DecisionSearchSessionError):
    def __init__(self) -> None:
        super().__init__("decision_search_session_aborted")


class BusyDecisionSearchSessionError(DecisionSearchSessionError):
    def __init__(self) -> None:
        super().__init__("decision_search_session_busy")


class DecisionSearchSessionCapacityError(DecisionSearchSessionError):
    def __init__(self) -> None:
        super().__init__("decision_search_session_capacity_exhausted")


class DecisionSearchPolicyError(DecisionSearchSessionError):
    pass


class DecisionSearchQueryError(DecisionSearchSessionError):
    def __init__(self) -> None:
        super().__init__("decision_search_query_invalid")


class DecisionSearchSessionHandle(BaseModel):
    """Opaque handle returned to the server-owned decision runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(pattern=DECISION_SEARCH_SESSION_ID_PATTERN)
    expires_at: AwareDatetime


class DecisionSearchBudgetUsage(BaseModel):
    """Cumulative ContextAccessLayer budget state for one decision turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_calls_used: int = Field(ge=0)
    tool_calls_limit: int = Field(ge=1)
    context_bytes_used: int = Field(ge=0)
    context_bytes_limit: int = Field(ge=1)
    source_refs_used: int = Field(ge=0)
    source_refs_limit: int = Field(ge=1)


class ContextSearchAccessAudit(AccessAuditEntry):
    """One gateway audit entry plus the shared turn budget after the call."""

    budget: DecisionSearchBudgetUsage


class ContextSearchResult(ContextResult):
    """Wire result for one autonomous MCP search call."""

    access_audit: ContextSearchAccessAudit


class DecisionSearchSessionSnapshot(BaseModel):
    """Authoritative session state used by the decision runtime at finish."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(pattern=DECISION_SEARCH_SESSION_ID_PATTERN)
    state: DecisionSearchSessionState
    request_id: uuid.UUID
    turn_id: uuid.UUID
    created_at: AwareDatetime
    expires_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    budget: DecisionSearchBudgetUsage
    results: tuple[ContextResult, ...] = ()
    source_refs: tuple[SourceRef, ...] = ()
    access_trace: tuple[AccessAuditEntry, ...] = ()


@dataclass(slots=True)
class _DecisionSearchSession:
    session_id: str
    request: DecisionRequest
    access_turn: ContextAccessTurn
    created_at: datetime
    expires_at: datetime
    deadline: float
    state: DecisionSearchSessionState = DecisionSearchSessionState.ACTIVE
    ended_at: datetime | None = None
    in_flight: int = 0
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    result_lock: Lock = field(default_factory=Lock)
    results: list[ContextResult] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _TerminalSession:
    state: DecisionSearchSessionState
    ended_monotonic: float


class DecisionContextSearchSessionService:
    """Own one ContextAccessTurn across repeated autonomous MCP searches."""

    def __init__(
        self,
        *,
        access_layer: ContextAccessLayer,
        session_factory: sessionmaker[Session],
        policy_resolver: AccessPolicyResolver,
        ttl_seconds: float = 60,
        max_active_sessions: int = 8,
        terminal_retention_seconds: float = 300,
        max_terminal_sessions: int = 1_024,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        if not callable(policy_resolver):
            raise TypeError("policy_resolver must be callable")
        if ttl_seconds <= 0 or ttl_seconds > 300:
            raise ValueError("ttl_seconds must be greater than 0 and at most 300")
        if not 1 <= max_active_sessions <= 128:
            raise ValueError("max_active_sessions must be between 1 and 128")
        if terminal_retention_seconds <= 0:
            raise ValueError("terminal_retention_seconds must be positive")
        if max_terminal_sessions < 1:
            raise ValueError("max_terminal_sessions must be positive")
        self.access_layer = access_layer
        self._session_factory = session_factory
        self._policy_resolver = policy_resolver
        self._ttl_seconds = ttl_seconds
        self._max_active_sessions = max_active_sessions
        self._terminal_retention_seconds = terminal_retention_seconds
        self._max_terminal_sessions = max_terminal_sessions
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic_clock or monotonic
        self._lock = Lock()
        self._active: dict[str, _DecisionSearchSession] = {}
        self._terminal: OrderedDict[str, _TerminalSession] = OrderedDict()
        self._closed = False

    def begin(
        self,
        request: DecisionRequest,
    ) -> DecisionSearchSessionHandle:
        """Create one opaque session from a server-owned DecisionRequest."""

        canonical_request = strict_model_validate(DecisionRequest, request)
        policy = self._resolve_policy(canonical_request)
        now = _utc(self._clock())
        current_monotonic = self._monotonic()
        access_turn = self.access_layer.start_turn(
            canonical_request,
            policy=policy,
            reject_duplicate_effective_queries=True,
        )
        with self._lock:
            self._prune_locked(current_monotonic)
            if self._closed:
                raise AbortedDecisionSearchSessionError()
            if len(self._active) >= self._max_active_sessions:
                raise DecisionSearchSessionCapacityError()
            session_id = self._new_session_id_locked()
            record = _DecisionSearchSession(
                session_id=session_id,
                request=canonical_request,
                access_turn=access_turn,
                created_at=now,
                expires_at=now + timedelta(seconds=self._ttl_seconds),
                deadline=current_monotonic + self._ttl_seconds,
            )
            self._active[session_id] = record
        return DecisionSearchSessionHandle(
            session_id=session_id,
            expires_at=record.expires_at,
        )

    async def search(
        self,
        decision_session_id: str,
        *,
        domain: str,
        capability: str,
        start: datetime | None = None,
        end: datetime | None = None,
        granularity: str = "summary",
        fields: tuple[str, ...] = (),
        privacy_level: PrivacyLevel = PrivacyLevel.AGGREGATE,
        limit: int = 100,
        parameters: Mapping[str, Any] | None = None,
    ) -> ContextSearchResult:
        """Execute one provider capability through the session's shared turn."""

        record = self._lookup_active(decision_session_id)
        async with record.operation_lock:
            self._ensure_active(record)
            with self._lock:
                self._ensure_active_locked(record, self._monotonic())
                record.in_flight += 1
            try:
                return await self._search_active(
                    record,
                    domain=domain,
                    capability=capability,
                    start=start,
                    end=end,
                    granularity=granularity,
                    fields=fields,
                    privacy_level=privacy_level,
                    limit=limit,
                    parameters=parameters or {},
                )
            finally:
                with self._lock:
                    record.in_flight = max(0, record.in_flight - 1)

    def inspect(
        self,
        decision_session_id: str,
    ) -> DecisionSearchSessionSnapshot:
        """Return current results, full refs, access trace, and shared budget."""

        record = self._lookup_active(decision_session_id)
        return self._snapshot(record)

    def finish(
        self,
        decision_session_id: str,
    ) -> DecisionSearchSessionSnapshot:
        """Seal and remove an idle active session, returning its final snapshot."""

        with self._lock:
            self._prune_locked(self._monotonic())
            record = self._active.get(decision_session_id)
            if record is None:
                self._raise_lookup_error_locked(decision_session_id)
            assert record is not None
            if record.in_flight:
                raise BusyDecisionSearchSessionError()
            self._transition_locked(
                record,
                DecisionSearchSessionState.FINISHED,
            )
        return self._snapshot(record)

    def abort(
        self,
        decision_session_id: str,
    ) -> DecisionSearchSessionSnapshot:
        """Fail closed immediately; in-flight work observes the terminal state."""

        with self._lock:
            self._prune_locked(self._monotonic())
            record = self._active.get(decision_session_id)
            if record is None:
                self._raise_lookup_error_locked(decision_session_id)
            assert record is not None
            self._transition_locked(
                record,
                DecisionSearchSessionState.ABORTED,
            )
        return self._snapshot(record)

    def close(self) -> None:
        """Abort every active session and reject future begin calls."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            for record in tuple(self._active.values()):
                self._transition_locked(
                    record,
                    DecisionSearchSessionState.ABORTED,
                )

    async def _search_active(
        self,
        record: _DecisionSearchSession,
        *,
        domain: str,
        capability: str,
        start: datetime | None,
        end: datetime | None,
        granularity: str,
        fields: tuple[str, ...],
        privacy_level: PrivacyLevel,
        limit: int,
        parameters: Mapping[str, Any],
    ) -> ContextSearchResult:
        try:
            descriptor, capability_spec = (
                self.access_layer.registry.capability(
                    capability,
                    include_disabled=True,
                )
            )
        except (
            UnknownCapabilityError,
            UnknownProviderError,
        ) as exc:
            raise DecisionSearchQueryError() from exc
        if descriptor.metadata.domain != domain.strip().casefold():
            raise DecisionSearchQueryError()
        self._validate_selected_records(
            record.request,
            parameters=parameters,
            capability=capability_spec,
        )
        try:
            query = ContextQuery(
                provider_id=descriptor.metadata.provider_id,
                capability=capability_spec.capability,
                start=start,
                end=end,
                timezone=record.request.timezone,
                granularity=granularity,
                fields=list(fields),
                privacy_level=privacy_level,
                limit=limit,
                parameters=dict(parameters),
            )
        except Exception as exc:
            raise DecisionSearchQueryError() from exc

        try:
            policy_before = self._resolve_policy(record.request)
        except DecisionSearchPolicyError as exc:
            result = record.access_turn.deny(
                query,
                reason_codes=(exc.code,),
            )
            return self._store_result(record, result)
        record.access_turn.update_policy(policy_before)
        policy_fingerprint = _policy_fingerprint(
            policy_before,
            domain=descriptor.metadata.domain,
        )

        remaining = record.deadline - self._monotonic()
        if remaining <= 0:
            self._expire(record)
            raise ExpiredDecisionSearchSessionError()

        try:
            with self._session_factory() as session:
                try:
                    async with asyncio.timeout(remaining):
                        result = await record.access_turn.query(
                            session,
                            query,
                            ensure_active=lambda: self._ensure_active(record),
                        )
                        try:
                            policy_after = self._resolve_policy(
                                record.request
                            )
                        except DecisionSearchPolicyError as exc:
                            result = record.access_turn.deny(
                                query,
                                reason_codes=(exc.code,),
                            )
                        else:
                            record.access_turn.update_policy(policy_after)
                            if (
                                _policy_fingerprint(
                                    policy_after,
                                    domain=descriptor.metadata.domain,
                                )
                                != policy_fingerprint
                            ):
                                result = record.access_turn.deny(
                                    query,
                                    reason_codes=(
                                        "domain_consent_changed",
                                    ),
                                )
                        self._ensure_active(record)
                    # MCP search is read-only. Production composition disables
                    # the wearable refresh reader, so every result is backed
                    # by already-retained data and the transaction can always
                    # be rolled back.
                    session.rollback()
                except BaseException:
                    session.rollback()
                    raise
        except TimeoutError as exc:
            self._expire(record)
            raise ExpiredDecisionSearchSessionError() from exc
        except DecisionSearchSessionError:
            raise
        except Exception:
            result = record.access_turn.deny(
                query,
                reason_codes=("provider_execution_failed",),
            )
        return self._store_result(record, result)

    def _store_result(
        self,
        record: _DecisionSearchSession,
        result: ContextResult,
    ) -> ContextSearchResult:
        canonical = strict_model_validate(ContextResult, result)
        with record.result_lock:
            record.results.append(canonical.model_copy(deep=True))
        audit = next(
            (
                entry
                for entry in reversed(record.access_turn.trace)
                if entry.query_id == canonical.query_id
            ),
            None,
        )
        if audit is None:
            raise RuntimeError("context search result is missing access audit")
        access_audit = ContextSearchAccessAudit.model_validate(
            {
                **audit.model_dump(mode="python", round_trip=True),
                "budget": self._budget(record),
            }
        )
        return ContextSearchResult.model_validate(
            {
                **canonical.model_dump(mode="python", round_trip=True),
                "access_audit": access_audit,
            }
        )

    def _resolve_policy(
        self,
        request: DecisionRequest,
    ) -> ContextAccessPolicy:
        try:
            policy = strict_model_validate(
                ContextAccessPolicy,
                self._policy_resolver(request),
            )
        except Exception as exc:
            raise DecisionSearchPolicyError(
                "access_policy_resolution_failed"
            ) from exc
        if not request.caller.authenticated:
            raise DecisionSearchPolicyError("caller_not_authenticated")
        if request.caller.principal_id != policy.owner_principal_id:
            raise DecisionSearchPolicyError("caller_not_policy_owner")
        return policy

    def _validate_selected_records(
        self,
        request: DecisionRequest,
        *,
        parameters: Mapping[str, Any],
        capability,
    ) -> None:
        selected = {
            _canonical_uuid(value)
            for value in request.hints.related_record_ids.values()
            if _canonical_uuid(value) is not None
        }
        for spec in capability.parameter_specs:
            if not spec.accepts_related_record_ref:
                continue
            value = parameters.get(spec.name)
            if value is None:
                continue
            if not isinstance(value, str) or _canonical_uuid(value) not in selected:
                raise DecisionSearchQueryError()

    def _budget(
        self,
        record: _DecisionSearchSession,
    ) -> DecisionSearchBudgetUsage:
        budget = record.request.budget
        return DecisionSearchBudgetUsage(
            tool_calls_used=record.access_turn.calls_used,
            tool_calls_limit=budget.max_tool_calls,
            context_bytes_used=record.access_turn.context_bytes_used,
            context_bytes_limit=budget.max_context_bytes,
            source_refs_used=record.access_turn.source_refs_used,
            source_refs_limit=budget.max_source_refs,
        )

    def _snapshot(
        self,
        record: _DecisionSearchSession,
    ) -> DecisionSearchSessionSnapshot:
        with record.result_lock:
            results = tuple(
                result.model_copy(deep=True) for result in record.results
            )
        refs: OrderedDict[str, SourceRef] = OrderedDict()
        for result in results:
            for source_ref in result.source_refs:
                refs.setdefault(
                    source_ref.reference_id,
                    source_ref.model_copy(deep=True),
                )
        ordered_refs = tuple(
            refs[reference_id] for reference_id in sorted(refs)
        )
        return DecisionSearchSessionSnapshot(
            session_id=record.session_id,
            state=record.state,
            request_id=record.request.request_id,
            turn_id=record.request.turn_id,
            created_at=record.created_at,
            expires_at=record.expires_at,
            ended_at=record.ended_at,
            budget=self._budget(record),
            results=results,
            source_refs=ordered_refs,
            access_trace=record.access_turn.trace,
        )

    def _lookup_active(
        self,
        session_id: str,
    ) -> _DecisionSearchSession:
        with self._lock:
            self._prune_locked(self._monotonic())
            record = self._active.get(session_id)
            if record is None:
                self._raise_lookup_error_locked(session_id)
            assert record is not None
            return record

    def _ensure_active(
        self,
        record: _DecisionSearchSession,
    ) -> None:
        with self._lock:
            self._ensure_active_locked(record, self._monotonic())

    def _ensure_active_locked(
        self,
        record: _DecisionSearchSession,
        current_monotonic: float,
    ) -> None:
        if (
            record.state is DecisionSearchSessionState.ACTIVE
            and current_monotonic >= record.deadline
        ):
            self._transition_locked(
                record,
                DecisionSearchSessionState.EXPIRED,
                ended_monotonic=current_monotonic,
            )
        if record.state is DecisionSearchSessionState.EXPIRED:
            raise ExpiredDecisionSearchSessionError()
        if record.state is DecisionSearchSessionState.FINISHED:
            raise FinishedDecisionSearchSessionError()
        if record.state is DecisionSearchSessionState.ABORTED:
            raise AbortedDecisionSearchSessionError()
        if self._active.get(record.session_id) is not record:
            self._raise_lookup_error_locked(record.session_id)

    def _expire(self, record: _DecisionSearchSession) -> None:
        with self._lock:
            if record.state is DecisionSearchSessionState.ACTIVE:
                self._transition_locked(
                    record,
                    DecisionSearchSessionState.EXPIRED,
                )

    def _prune_locked(self, current_monotonic: float) -> None:
        for record in tuple(self._active.values()):
            if current_monotonic >= record.deadline:
                self._transition_locked(
                    record,
                    DecisionSearchSessionState.EXPIRED,
                    ended_monotonic=current_monotonic,
                )
        terminal_cutoff = (
            current_monotonic - self._terminal_retention_seconds
        )
        while self._terminal:
            session_id, terminal = next(iter(self._terminal.items()))
            if (
                terminal.ended_monotonic >= terminal_cutoff
                and len(self._terminal) <= self._max_terminal_sessions
            ):
                break
            self._terminal.pop(session_id)

    def _transition_locked(
        self,
        record: _DecisionSearchSession,
        state: DecisionSearchSessionState,
        *,
        ended_monotonic: float | None = None,
    ) -> None:
        self._active.pop(record.session_id, None)
        record.state = state
        record.ended_at = _utc(self._clock())
        self._terminal[record.session_id] = _TerminalSession(
            state=state,
            ended_monotonic=(
                ended_monotonic
                if ended_monotonic is not None
                else self._monotonic()
            ),
        )
        self._terminal.move_to_end(record.session_id)
        while len(self._terminal) > self._max_terminal_sessions:
            self._terminal.popitem(last=False)

    def _raise_lookup_error_locked(self, session_id: str) -> None:
        terminal = self._terminal.get(session_id)
        if terminal is None:
            raise UnknownDecisionSearchSessionError()
        if terminal.state is DecisionSearchSessionState.EXPIRED:
            raise ExpiredDecisionSearchSessionError()
        if terminal.state is DecisionSearchSessionState.FINISHED:
            raise FinishedDecisionSearchSessionError()
        raise AbortedDecisionSearchSessionError()

    def _new_session_id_locked(self) -> str:
        while True:
            candidate = "dss_" + secrets.token_urlsafe(32)
            if (
                candidate not in self._active
                and candidate not in self._terminal
            ):
                return candidate


def _canonical_uuid(value: str) -> str | None:
    try:
        candidate = value
        if candidate.casefold().startswith("urn:uuid:"):
            candidate = candidate[len("urn:uuid:") :]
        return str(uuid.UUID(candidate))
    except (AttributeError, ValueError):
        return None


def _policy_fingerprint(
    policy: ContextAccessPolicy,
    *,
    domain: str,
) -> str:
    grant = policy.grant(domain)
    payload = {
        "owner_principal_id": policy.owner_principal_id,
        "grant": (
            grant.model_dump(mode="json", round_trip=True)
            if grant is not None
            else None
        ),
        "max_query_days": policy.max_query_days,
        "max_rows_per_query": policy.max_rows_per_query,
        "max_payload_bytes_per_query": (
            policy.max_payload_bytes_per_query
        ),
        "max_source_refs_per_query": (
            policy.max_source_refs_per_query
        ),
        "trim_overlong_queries": policy.trim_overlong_queries,
        "allow_external_provenance": policy.allow_external_provenance,
    }
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
