"""Atomic provenance validation and persistence for decision-agent runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
from sqlalchemy import event, or_, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from healthmes.activity.locking import (
    activity_write_lock,
    lock_activity_write_plane,
    postgres_activity_write_plane_guard,
)
from healthmes.calendars.visibility import (
    CalendarVisibility,
    CalendarVisibilityChanged,
)
from healthmes.decision.access import (
    AccessAuditEntry,
    ContextAccessLayer,
    ContextAccessPolicy,
)
from healthmes.decision.agent import DecisionAgentRun, SessionFactory
from healthmes.decision.contracts import (
    ContextQuery,
    ContextStatus,
    DecisionPersistenceIntent,
    DecisionRequest,
    DecisionResult,
    DecisionStatus,
    PersistenceStatus,
    RuntimeMetadata,
    SourceRef,
    ToolCallRecord,
    ToolCallStatus,
)
from healthmes.decision.validation import (
    NormalizedJson,
    normalize_untrusted_json,
    strict_model_validate,
)
from healthmes.storage import (
    apply_decision_retention,
    purge_expired_decision_records,
)
from healthmes.store import DecisionKind, DecisionRecord
from healthmes.timing import steady_time

DECISION_RECORD_SCHEMA = "healthmes.decision-record.v1"
DECISION_PAYLOAD_SCHEMA = "healthmes.decision-private.v3"
_LEGACY_DECISION_PAYLOAD_SCHEMA = "healthmes.decision-private.v1"
_LEGACY_DECISION_PAYLOAD_SCHEMA_V2 = "healthmes.decision-private.v2"
_PERSISTENCE_FAILURE = "decision_record_persistence_failed"
_MAX_STORED_JSON_BYTES = 2_000_000
_MAX_STORED_OUTCOME_SUMMARY_LENGTH = 160
_MAX_STORED_LIMITATION_CODES = 32
_COMPACT_RECOVERY_LIMITATION = "decision_response_compacted"
_MAX_POSTGRES_ATTEMPTS = 3
_RETRYABLE_POSTGRES_STATES = frozenset({"40001", "40P01"})
_POSTGRES_TIMEOUT_STATES = frozenset({"55P03", "57014"})
_DEFAULT_FINALIZATION_TIMEOUT_SECONDS = 5.0
_FINALIZATION_TIMEOUT = "decision_finalization_timeout"
_FINALIZATION_OUTCOME_UNKNOWN = "decision_finalization_outcome_unknown"
_FINALIZATION_CAPACITY_EXHAUSTED = (
    "decision_finalization_capacity_exhausted"
)
_SQLITE_BUSY_TIMEOUT_INFO_KEY = (
    "healthmes_decision_finalization_sqlite_busy_timeout"
)
_SAFE_TOOL_ERROR_CODES = frozenset(
    {
        "decision_tool_call_budget_exhausted",
        "duplicate_tool_call",
        "invalid_provider_query",
        "malformed_tool_arguments",
        "provider_contract_violation",
        "provider_execution_failed",
        "tool_execution_failed",
        "turn_context_byte_budget_exhausted",
        "turn_source_ref_budget_exhausted",
        "turn_tool_call_budget_exhausted",
        "unknown_tool",
    }
)
_SOURCE_REF_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])sr_[0-9a-f]{32}(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_SAFE_LIMITATION_CODE = re.compile(
    r"^[a-z][a-z0-9_.:-]{0,127}$"
)

AccessPolicyResolver = Callable[[DecisionRequest], ContextAccessPolicy]


@dataclass(frozen=True, slots=True)
class _SourceAttempt:
    query: ContextQuery
    supporting_refs: tuple[SourceRef, ...]


SourceCandidates = Mapping[str, tuple[_SourceAttempt, ...]]


@dataclass(frozen=True, slots=True)
class _StoredDecision:
    result: DecisionResult
    source_refs: tuple[SourceRef, ...]
    candidates: SourceCandidates
    access_trace: tuple[AccessAuditEntry, ...]


class _StoredRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime: RuntimeMetadata
    system_policy_version: str = Field(min_length=1, max_length=128)
    started_at: AwareDatetime
    finished_at: AwareDatetime
    steps_used: int = Field(ge=0, le=32)

    @model_validator(mode="after")
    def validate_range(self) -> _StoredRun:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not be before started_at")
        return self


class _StoredToolTraceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: uuid.UUID
    query: ContextQuery
    status: ToolCallStatus
    started_at: AwareDatetime
    finished_at: AwareDatetime
    context_status: ContextStatus | None = None
    source_refs: tuple[SourceRef, ...] = ()
    limitations: tuple[str, ...] = ()
    error_code: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_trace(self) -> _StoredToolTraceRecord:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not be before started_at")
        if self.status is ToolCallStatus.COMPLETED:
            if self.context_status is None:
                raise ValueError(
                    "completed stored tool calls require context status"
                )
        elif self.source_refs:
            raise ValueError(
                "non-completed stored tool calls cannot expose source refs"
            )
        reference_ids = [item.reference_id for item in self.source_refs]
        if len(reference_ids) != len(set(reference_ids)):
            raise ValueError("stored source refs must be unique")
        if len(self.limitations) != len(set(self.limitations)):
            raise ValueError("stored limitations must be unique")
        return self


class _StoredDecisionPayloadV1(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    schema_name: Literal["healthmes.decision-private.v1"] = Field(
        alias="schema"
    )
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    request: DecisionRequest
    result: DecisionResult
    run: _StoredRun
    source_refs: tuple[SourceRef, ...]
    tool_trace: tuple[_StoredToolTraceRecord, ...]
    access_trace: tuple[AccessAuditEntry, ...]


class _StoredDecisionRequestMetadata(BaseModel):
    """Non-content request fields needed for correlation and auditing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: uuid.UUID
    turn_id: uuid.UUID
    requested_at: AwareDatetime
    timezone: str = Field(min_length=1, max_length=64)
    execution_scope: str = Field(min_length=1, max_length=16)
    requested_privacy_level: str = Field(min_length=1, max_length=32)


class _StoredSourceAttestation(BaseModel):
    """Minimum source/query material required for later revalidation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: ContextQuery
    source_refs: tuple[SourceRef, ...]

    @model_validator(mode="after")
    def validate_refs(self) -> _StoredSourceAttestation:
        reference_ids = [
            source_ref.reference_id for source_ref in self.source_refs
        ]
        if not reference_ids:
            raise ValueError("source attestations require source refs")
        if len(reference_ids) != len(set(reference_ids)):
            raise ValueError(
                "source attestation refs must be unique"
            )
        return self


class _StoredDecisionPayloadV2(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    schema_name: Literal["healthmes.decision-private.v2"] = Field(
        alias="schema"
    )
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    request: _StoredDecisionRequestMetadata
    persistence_intent: DecisionPersistenceIntent
    result: DecisionResult
    run: _StoredRun
    source_refs: tuple[SourceRef, ...]
    source_attestations: tuple[_StoredSourceAttestation, ...]
    access_trace: tuple[AccessAuditEntry, ...]

    @model_validator(mode="after")
    def validate_persistence_contract(
        self,
    ) -> _StoredDecisionPayloadV2:
        if self.persistence_intent in {
            DecisionPersistenceIntent.NONE,
            DecisionPersistenceIntent.MUTATION,
        }:
            raise ValueError(
                "stored decisions require a read-only persistence intent"
            )
        action_or_risk = self.persistence_intent in {
            DecisionPersistenceIntent.ACTION,
            DecisionPersistenceIntent.RISK,
        }
        if self.result.proposed_action is not action_or_risk:
            raise ValueError(
                "stored persistence intent conflicts with proposed_action"
            )
        return self


class _StoredDecisionOutcome(BaseModel):
    """Bounded, non-verbatim outcome retained for audit and recovery."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: DecisionStatus
    summary: str = Field(
        min_length=1,
        max_length=_MAX_STORED_OUTCOME_SUMMARY_LENGTH,
    )
    proposed_action: bool
    limitation_codes: tuple[str, ...] = Field(
        default=(),
        max_length=_MAX_STORED_LIMITATION_CODES,
    )
    confidence: float | None = Field(default=None, ge=0, le=1)
    decision_record_id: uuid.UUID

    @model_validator(mode="after")
    def validate_outcome(self) -> _StoredDecisionOutcome:
        if self.status is not DecisionStatus.COMPLETED:
            raise ValueError("stored outcomes must be completed")
        if any(
            _SAFE_LIMITATION_CODE.fullmatch(value) is None
            for value in self.limitation_codes
        ):
            raise ValueError(
                "stored outcome limitations must be safe codes"
            )
        if len(self.limitation_codes) != len(
            set(self.limitation_codes)
        ):
            raise ValueError(
                "stored outcome limitation codes must be unique"
            )
        return self


class _StoredDecisionPayloadV3(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    schema_name: Literal["healthmes.decision-private.v3"] = Field(
        alias="schema"
    )
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    request: _StoredDecisionRequestMetadata
    persistence_intent: DecisionPersistenceIntent
    outcome: _StoredDecisionOutcome
    run: _StoredRun
    source_refs: tuple[SourceRef, ...]
    source_attestations: tuple[_StoredSourceAttestation, ...]
    access_trace: tuple[AccessAuditEntry, ...]

    @model_validator(mode="after")
    def validate_persistence_contract(
        self,
    ) -> _StoredDecisionPayloadV3:
        if self.persistence_intent in {
            DecisionPersistenceIntent.NONE,
            DecisionPersistenceIntent.MUTATION,
        }:
            raise ValueError(
                "stored decisions require a read-only persistence intent"
            )
        action_or_risk = self.persistence_intent in {
            DecisionPersistenceIntent.ACTION,
            DecisionPersistenceIntent.RISK,
        }
        if self.outcome.proposed_action is not action_or_risk:
            raise ValueError(
                "stored persistence intent conflicts with proposed_action"
            )
        if action_or_risk and not self.source_refs:
            raise ValueError(
                "stored action and risk outcomes require source refs"
            )
        if self.outcome.summary != _stored_outcome_summary(
            self.persistence_intent
        ):
            raise ValueError("stored outcome summary is not canonical")
        return self


StoredDecisionPayload = (
    _StoredDecisionPayloadV1
    | _StoredDecisionPayloadV2
    | _StoredDecisionPayloadV3
)


class _FinalizationRejected(RuntimeError):
    def __init__(self, code: str, *limitations: str) -> None:
        super().__init__(code)
        self.code = code
        self.limitations = limitations


class _FinalizationPhase(Enum):
    PRE_COMMIT = "pre_commit"
    COMMITTING = "committing"
    COMMITTED = "committed"
    ABORTED = "aborted"
    OUTCOME_UNKNOWN = "outcome_unknown"
    FINISHED = "finished"


class _FinalizationControl:
    """Cross-thread commit fence for one supervised finalization."""

    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self.done = threading.Event()
        self._lock = threading.Lock()
        self._phase = _FinalizationPhase.PRE_COMMIT
        self._result: DecisionResult | None = None
        self._published_at: float | None = None

    def enter_commit(self) -> None:
        """Enter the irreversible phase only at SQLAlchemy before_commit."""

        with self._lock:
            if self._phase is _FinalizationPhase.ABORTED:
                raise TimeoutError(
                    "decision finalization was aborted before commit"
                )
            if self._phase in {
                _FinalizationPhase.COMMITTING,
                _FinalizationPhase.OUTCOME_UNKNOWN,
            }:
                return
            if self._phase is not _FinalizationPhase.PRE_COMMIT:
                raise RuntimeError(
                    "decision finalization commit phase is invalid"
                )
            if steady_time() >= self.deadline:
                self._phase = _FinalizationPhase.ABORTED
                raise TimeoutError(
                    "decision finalization deadline expired before commit"
                )
            self._phase = _FinalizationPhase.COMMITTING

    def mark_committed(self, result: DecisionResult) -> None:
        with self._lock:
            if self._phase is _FinalizationPhase.ABORTED:
                raise RuntimeError(
                    "aborted decision finalization reached commit"
                )
            self._result = result
            # Once the caller has classified an in-flight commit as unknown,
            # only request-ID recovery may reveal its eventual outcome.
            if self._phase is not _FinalizationPhase.OUTCOME_UNKNOWN:
                self._phase = _FinalizationPhase.COMMITTED

    def reset_retryable_commit(self) -> bool:
        """Return a known-rolled-back PostgreSQL retry to PRE_COMMIT."""

        with self._lock:
            if self._phase is _FinalizationPhase.COMMITTING:
                if steady_time() >= self.deadline:
                    self._phase = _FinalizationPhase.ABORTED
                    return False
                self._phase = _FinalizationPhase.PRE_COMMIT
                return True
            return self._phase is _FinalizationPhase.PRE_COMMIT

    def expire(
        self,
        *,
        response_deadline: bool = False,
    ) -> _FinalizationPhase:
        """Fence future commits and classify unfinished work safely."""

        with self._lock:
            published_in_time = (
                self._published_at is not None
                and self._published_at < self.deadline
            )
            if published_in_time:
                return self._phase
            if self._phase is _FinalizationPhase.PRE_COMMIT:
                self._phase = _FinalizationPhase.ABORTED
            elif (
                self._phase is _FinalizationPhase.COMMITTING
                or (
                    response_deadline
                    and self._phase is _FinalizationPhase.COMMITTED
                )
            ):
                # after_commit can run before session cleanup and capacity
                # release. A deadline at that point still uses request-ID
                # recovery rather than exposing success prematurely.
                self._phase = _FinalizationPhase.OUTCOME_UNKNOWN
            return self._phase

    def publish(self, result: DecisionResult) -> None:
        self.prepare_result(result)
        self.signal_done()

    def prepare_result(self, result: DecisionResult) -> None:
        """Freeze the worker outcome without waking a waiting caller."""

        with self._lock:
            if self._phase is not _FinalizationPhase.COMMITTED:
                self._result = result
            if self._phase not in {
                _FinalizationPhase.ABORTED,
                _FinalizationPhase.COMMITTED,
                _FinalizationPhase.OUTCOME_UNKNOWN,
            }:
                self._phase = _FinalizationPhase.FINISHED

    def signal_done(self) -> None:
        """Expose a prepared outcome after worker resources are reusable."""

        with self._lock:
            if self._published_at is None:
                self._published_at = steady_time()
                self.done.set()

    def snapshot(
        self,
    ) -> tuple[_FinalizationPhase, DecisionResult | None]:
        with self._lock:
            return self._phase, self._result

    def response_snapshot(
        self,
    ) -> tuple[_FinalizationPhase, DecisionResult | None, bool]:
        """Return outcome plus whether it became visible within deadline."""

        with self._lock:
            return (
                self._phase,
                self._result,
                self._published_at is not None
                and self._published_at < self.deadline,
            )


def decision_request_fingerprint(request: DecisionRequest) -> str:
    """Hash semantic request contents while excluding retry correlation IDs."""

    canonical = strict_model_validate(DecisionRequest, request)
    payload = canonical.model_dump(
        mode="json",
        round_trip=True,
        exclude={"request_id", "turn_id", "persistence_requested"},
    )
    if canonical.persistence_requested:
        payload["persistence_requested"] = True
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def decision_result_from_record(row: DecisionRecord) -> DecisionResult:
    """Recover one verified result after an outcome-unknown response."""

    if (
        row.expires_at is not None
        and _as_utc(row.expires_at) <= datetime.now(UTC)
    ):
        raise ValueError("decision record has expired")
    fingerprint = row.decision_request_fingerprint
    if fingerprint is None:
        raise ValueError("decision record is not Decision Agent correlated")
    stored = _stored_decision(row, fingerprint=fingerprint)
    if isinstance(stored, str):
        raise ValueError(stored)
    return stored.result.model_copy(deep=True)


class DecisionFinalizer:
    """Promote a runtime draft only after current-source and storage checks."""

    def __init__(
        self,
        *,
        access_layer: ContextAccessLayer,
        session_factory: SessionFactory,
        policy_resolver: AccessPolicyResolver,
        timeout_seconds: float = _DEFAULT_FINALIZATION_TIMEOUT_SECONDS,
        max_workers: int = 8,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("finalization timeout must be positive")
        if max_workers < 1:
            raise ValueError("finalization max_workers must be positive")
        self._access_layer = access_layer
        self._session_factory = session_factory
        self._policy_resolver = policy_resolver
        self._timeout_seconds = timeout_seconds
        self._worker_slots = threading.BoundedSemaphore(max_workers)
        self._workers_lock = threading.Lock()
        self._active_controls: set[_FinalizationControl] = set()
        self._shutdown_started = False
        self._clock = clock or (lambda: datetime.now(UTC))

    def finalize(
        self,
        request: DecisionRequest,
        run: DecisionAgentRun,
    ) -> DecisionResult:
        control = self._start_supervised_finalization(request, run)
        control.done.wait(
            timeout=max(0.0, control.deadline - steady_time())
        )
        return self._supervised_result(request, run, control)

    async def afinalize(
        self,
        request: DecisionRequest,
        run: DecisionAgentRun,
    ) -> DecisionResult:
        """Finalize without occupying asyncio's non-daemon thread pool."""

        control = self._start_supervised_finalization(request, run)
        while not control.done.is_set():
            remaining = control.deadline - steady_time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.01, remaining))
        return self._supervised_result(request, run, control)

    def _start_supervised_finalization(
        self,
        request: DecisionRequest,
        run: DecisionAgentRun,
    ) -> _FinalizationControl:
        control = _FinalizationControl(
            steady_time() + self._timeout_seconds
        )
        if not self._worker_slots.acquire(blocking=False):
            control.publish(
                _failure_result(
                    run,
                    code=_FINALIZATION_CAPACITY_EXHAUSTED,
                    persistence_required=_run_requires_persistence(
                        request,
                        run,
                    ),
                )
            )
            return control
        with self._workers_lock:
            if self._shutdown_started:
                self._worker_slots.release()
                control.publish(
                    _failure_result(
                        run,
                        code=_FINALIZATION_TIMEOUT,
                        persistence_required=_run_requires_persistence(
                            request,
                            run,
                        ),
                    )
                )
                return control
            self._active_controls.add(control)

        def worker() -> None:
            try:
                result = self._finalize_inline(
                    request,
                    run,
                    control=control,
                )
            except BaseException:
                result = _failure_result(
                    run,
                    code=_PERSISTENCE_FAILURE,
                    persistence_required=_run_requires_persistence(
                        request,
                        run,
                    ),
                )
            control.prepare_result(result)
            with self._workers_lock:
                self._worker_slots.release()
                self._active_controls.discard(control)
                control.signal_done()

        thread = threading.Thread(
            target=worker,
            name=f"healthmes-finalizer-{getattr(run, 'request_id', 'invalid')}",
            daemon=True,
        )
        try:
            thread.start()
        except BaseException:
            result = _failure_result(
                run,
                code=_PERSISTENCE_FAILURE,
                persistence_required=_run_requires_persistence(
                    request,
                    run,
                ),
            )
            control.prepare_result(result)
            with self._workers_lock:
                self._worker_slots.release()
                self._active_controls.discard(control)
                control.signal_done()
        return control

    def begin_shutdown(self) -> None:
        """Seal worker admission and fence every pre-commit finalization."""

        with self._workers_lock:
            self._shutdown_started = True
            controls = tuple(self._active_controls)
        for control in controls:
            control.expire()

    async def adrain(self) -> None:
        """Wait until every accepted worker has released its DB resources.

        A COMMITTING worker can outlive the response deadline and return
        ``UNKNOWN`` to the caller. Graceful shutdown must still retain the
        database engine until that irreversible commit attempt and its
        connection cleanup have finished.
        """

        while True:
            with self._workers_lock:
                if not self._active_controls:
                    return
            await asyncio.sleep(0.01)

    async def aclose(self) -> None:
        """Seal admission and durably drain before the database is disposed."""

        self.begin_shutdown()
        cancelled: asyncio.CancelledError | None = None
        current = asyncio.current_task()
        while True:
            cancelling_before = (
                current.cancelling() if current is not None else 0
            )
            try:
                await self.adrain()
                break
            except asyncio.CancelledError as exc:
                cancelling_after = (
                    current.cancelling() if current is not None else 0
                )
                if current is None or cancelling_after <= cancelling_before:
                    raise
                cancelled = exc
        if cancelled is not None:
            raise cancelled

    def close(self) -> None:
        """Synchronously close when no event loop is running."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.aclose())
            return
        raise RuntimeError(
            "DecisionFinalizer.close() cannot run inside an active "
            "event loop; await aclose() instead"
        )

    def abort_active(self) -> None:
        """Fence every in-flight pre-commit worker during bounded shutdown."""

        with self._workers_lock:
            controls = tuple(self._active_controls)
        for control in controls:
            control.expire()

    def _supervised_result(
        self,
        request: DecisionRequest,
        run: DecisionAgentRun,
        control: _FinalizationControl,
    ) -> DecisionResult:
        phase, result, published_in_time = control.response_snapshot()
        if published_in_time:
            if phase is _FinalizationPhase.COMMITTED and result is not None:
                return result
            if phase is _FinalizationPhase.OUTCOME_UNKNOWN:
                return _unknown_result(run)
            if phase is _FinalizationPhase.ABORTED:
                return _failure_result(
                    run,
                    code=_FINALIZATION_TIMEOUT,
                    persistence_required=_run_requires_persistence(
                        request,
                        run,
                    ),
                )
            if result is not None:
                return result

        phase = control.expire(response_deadline=True)
        final_phase, final_result, published_in_time = (
            control.response_snapshot()
        )
        if published_in_time:
            if (
                final_phase is _FinalizationPhase.COMMITTED
                and final_result is not None
            ):
                return final_result
            if final_phase is _FinalizationPhase.OUTCOME_UNKNOWN:
                return _unknown_result(run)
            if final_phase is _FinalizationPhase.ABORTED:
                return _failure_result(
                    run,
                    code=_FINALIZATION_TIMEOUT,
                    persistence_required=_run_requires_persistence(
                        request,
                        run,
                    ),
                )
            if final_result is not None:
                return final_result
        if final_phase is _FinalizationPhase.OUTCOME_UNKNOWN or phase in {
            _FinalizationPhase.COMMITTING,
            _FinalizationPhase.OUTCOME_UNKNOWN,
        }:
            return _unknown_result(run)
        return _failure_result(
            run,
            code=_FINALIZATION_TIMEOUT,
            persistence_required=_run_requires_persistence(request, run),
        )

    def _finalize_inline(
        self,
        request: DecisionRequest,
        run: DecisionAgentRun,
        *,
        control: _FinalizationControl,
    ) -> DecisionResult:
        try:
            canonical_request = strict_model_validate(
                DecisionRequest,
                request,
            )
            canonical_run = strict_model_validate(
                DecisionAgentRun,
                run,
            )
        except Exception:
            return _failure_result(
                run,
                code="invalid_decision_finalization_input",
                persistence_required=False,
            )

        if (
            canonical_run.request_id != canonical_request.request_id
            or canonical_run.turn_id != canonical_request.turn_id
        ):
            return _failure_result(
                canonical_run,
                code="decision_run_request_mismatch",
                persistence_required=False,
            )

        try:
            source_candidates, trace_refs = _trace_source_candidates(
                canonical_run.tool_trace
            )
        except _FinalizationRejected as exc:
            return _failure_result(
                canonical_run,
                code=exc.code,
                extra_limitations=exc.limitations,
                persistence_required=False,
            )

        used_ids = tuple(canonical_run.draft.used_source_ref_ids)
        if (
            canonical_run.draft.status is DecisionStatus.COMPLETED
            and trace_refs
            and not used_ids
        ):
            return _failure_result(
                canonical_run,
                code="decision_source_refs_omitted",
                persistence_required=False,
            )
        if any(reference_id not in source_candidates for reference_id in used_ids):
            return _failure_result(
                canonical_run,
                code="decision_source_ref_not_in_tool_trace",
                persistence_required=False,
            )
        forged_text_ref = _unvalidated_text_source_ref(
            canonical_run,
            allowed_reference_ids=frozenset(used_ids),
        )
        if forged_text_ref is not None:
            return _failure_result(
                canonical_run,
                code="decision_text_contains_unvalidated_source_ref",
                persistence_required=False,
            )

        effective_persistence_intent = _effective_persistence_intent(
            canonical_request,
            canonical_run,
        )
        persistence_required = (
            effective_persistence_intent
            is not DecisionPersistenceIntent.NONE
        )
        fingerprint = decision_request_fingerprint(canonical_request)
        finalization_deadline = control.deadline

        try:
            with self._session_factory() as session:
                _configure_finalization_database_timeouts(
                    session,
                    timeout_seconds=_remaining_seconds(
                        finalization_deadline
                    ),
                )
                policy = self._resolve_policy(
                    canonical_request,
                    session=session,
                    lock=False,
                )
                _ensure_finalization_deadline(finalization_deadline)
                if not persistence_required:
                    calendar_snapshot = (
                        self._calendar_visibility_for_candidates(
                            session,
                            used_ids=used_ids,
                            candidates=source_candidates,
                        )
                    )
                    validated_refs, source_limitations = (
                        self._revalidate_used_refs(
                            session,
                            canonical_request,
                            policy=policy,
                            used_ids=used_ids,
                            candidates=source_candidates,
                            access_trace=canonical_run.access_trace,
                            lock_sources=False,
                            calendar_visibility_snapshot=(
                                calendar_snapshot
                            ),
                        )
                    )
                    self._require_calendar_visibility_current(
                        calendar_snapshot
                    )
                    _ensure_finalization_deadline(
                        finalization_deadline
                    )
                session.rollback()
            if not persistence_required:
                return _result_without_persistence(
                    canonical_run,
                    validated_refs,
                    extra_limitations=source_limitations,
                )

            return self._persist_with_retry(
                canonical_request,
                canonical_run,
                used_ids=used_ids,
                candidates=source_candidates,
                fingerprint=fingerprint,
                persistence_intent=effective_persistence_intent,
                deadline=finalization_deadline,
                control=control,
            )
        except _FinalizationRejected as exc:
            return _failure_result(
                canonical_run,
                code=exc.code,
                extra_limitations=exc.limitations,
                persistence_required=persistence_required,
            )
        except IntegrityError:
            try:
                return self._persist_with_retry(
                    canonical_request,
                    canonical_run,
                    used_ids=used_ids,
                    candidates=source_candidates,
                    fingerprint=fingerprint,
                    persistence_intent=effective_persistence_intent,
                    insert_if_missing=False,
                    deadline=finalization_deadline,
                    control=control,
                )
            except _FinalizationRejected as exc:
                return _failure_result(
                    canonical_run,
                    code=exc.code,
                    extra_limitations=exc.limitations,
                    persistence_required=persistence_required,
                )
            except TimeoutError:
                return _failure_result(
                    canonical_run,
                    code=_FINALIZATION_TIMEOUT,
                    persistence_required=persistence_required,
                )
            except DBAPIError as exc:
                return _failure_result(
                    canonical_run,
                    code=(
                        _FINALIZATION_TIMEOUT
                        if _database_timeout_error(exc)
                        else _PERSISTENCE_FAILURE
                    ),
                    persistence_required=persistence_required,
                )
            except Exception:
                return _failure_result(
                    canonical_run,
                    code=_PERSISTENCE_FAILURE,
                    persistence_required=persistence_required,
                )
        except TimeoutError:
            return _failure_result(
                canonical_run,
                code=_FINALIZATION_TIMEOUT,
                persistence_required=persistence_required,
            )
        except DBAPIError as exc:
            return _failure_result(
                canonical_run,
                code=(
                    _FINALIZATION_TIMEOUT
                    if _database_timeout_error(exc)
                    else _PERSISTENCE_FAILURE
                ),
                persistence_required=persistence_required,
            )
        except Exception:
            return _failure_result(
                canonical_run,
                code=_PERSISTENCE_FAILURE,
                persistence_required=persistence_required,
            )

    def _persist_with_retry(
        self,
        request: DecisionRequest,
        run: DecisionAgentRun,
        *,
        used_ids: Sequence[str],
        candidates: SourceCandidates,
        fingerprint: str,
        persistence_intent: DecisionPersistenceIntent,
        insert_if_missing: bool = True,
        deadline: float | None = None,
        control: _FinalizationControl | None = None,
    ) -> DecisionResult:
        deadline = (
            deadline
            if deadline is not None
            else steady_time() + self._timeout_seconds
        )
        control = control or _FinalizationControl(deadline)
        with activity_write_lock(
            timeout_seconds=_remaining_seconds(deadline)
        ):
            for attempt in range(_MAX_POSTGRES_ATTEMPTS):
                try:
                    with self._session_factory() as session:
                        original_bind = session.bind
                        with _finalization_connection_guard(
                            session.get_bind(),
                            timeout_seconds=_remaining_seconds(deadline),
                        ) as guarded_connection:
                            if guarded_connection is not None:
                                session.bind = guarded_connection
                            try:
                                _begin_finalization_transaction(
                                    session,
                                    timeout_seconds=_remaining_seconds(
                                        deadline
                                    ),
                                )
                                purge_expired_decision_records(
                                    session,
                                    now=_as_utc(self._clock()),
                                )
                                final_policy = self._resolve_policy(
                                    request,
                                    session=session,
                                    lock=True,
                                )
                                _ensure_finalization_deadline(deadline)
                                existing = _existing_stored_decision(
                                    session,
                                    request,
                                    fingerprint=fingerprint,
                                    lock=True,
                                )
                                _ensure_finalization_deadline(deadline)
                                if existing is not None:
                                    if isinstance(existing, str):
                                        raise _FinalizationRejected(existing)
                                    stored_result = (
                                        self._revalidate_stored_decision(
                                            session,
                                            request,
                                            policy=final_policy,
                                            stored=existing,
                                        )
                                    )
                                    _ensure_finalization_deadline(
                                        deadline
                                    )
                                    return stored_result
                                if not insert_if_missing:
                                    raise _FinalizationRejected(
                                        _PERSISTENCE_FAILURE
                                    )

                                calendar_snapshot = (
                                    self._calendar_visibility_for_candidates(
                                        session,
                                        used_ids=used_ids,
                                        candidates=candidates,
                                    )
                                )
                                source_validation_now = _as_utc(
                                    self._clock()
                                )
                                self._lock_used_ref_sources(
                                    session,
                                    request,
                                    policy=final_policy,
                                    used_ids=used_ids,
                                    candidates=candidates,
                                    now=source_validation_now,
                                    calendar_visibility_snapshot=(
                                        calendar_snapshot
                                    ),
                                )
                                _ensure_finalization_deadline(deadline)

                                validated_refs, source_limitations = (
                                    self._revalidate_used_refs(
                                        session,
                                        request,
                                        policy=final_policy,
                                        used_ids=used_ids,
                                        candidates=candidates,
                                        access_trace=run.access_trace,
                                        lock_sources=False,
                                        now=source_validation_now,
                                        calendar_visibility_snapshot=(
                                            calendar_snapshot
                                        ),
                                    )
                                )
                                _ensure_finalization_deadline(deadline)
                                if (
                                    run.draft.proposed_action
                                    and (
                                        "external_source_retention_unverified"
                                        in source_limitations
                                    )
                                ):
                                    raise _FinalizationRejected(
                                        "decision_action_requires_retained_source",
                                        "external_source_retention_unverified",
                                    )

                                record_id = uuid.uuid4()
                                result = _persisted_result(
                                    run,
                                    validated_refs,
                                    record_id=record_id,
                                    extra_limitations=source_limitations,
                                )
                                _ensure_finalization_deadline(deadline)
                                payload = _decision_payload(
                                    request,
                                    run,
                                    result,
                                    request_fingerprint=fingerprint,
                                    persistence_intent=persistence_intent,
                                    source_limitations=source_limitations,
                                )
                                _ensure_finalization_deadline(deadline)
                                row = DecisionRecord(
                                    id=record_id,
                                    kind=DecisionKind.INSIGHT,
                                    tree=_decision_tree(result),
                                    summary=_public_summary(result),
                                    llm_model=(
                                        run.runtime.model[:64]
                                        if run.runtime.model
                                        else None
                                    ),
                                    tokens=_token_total(run),
                                    decision_request_id=request.request_id,
                                    decision_turn_id=run.turn_id,
                                    decision_request_fingerprint=fingerprint,
                                    decision_payload=payload,
                                    decision_payload_digest=_json_digest(
                                        payload
                                    ),
                                )
                                apply_decision_retention(
                                    session,
                                    row,
                                    basis_at=source_validation_now,
                                )
                                session.add(row)
                                _ensure_finalization_deadline(deadline)
                                session.flush()
                                _ensure_finalization_deadline(deadline)
                                self._require_calendar_visibility_current(
                                    calendar_snapshot
                                )
                                _ensure_finalization_deadline(deadline)
                                def before_commit(
                                    _session: Session,
                                ) -> None:
                                    control.enter_commit()

                                def after_commit(
                                    _session: Session,
                                ) -> None:
                                    control.mark_committed(result)

                                event.listen(
                                    session,
                                    "before_commit",
                                    before_commit,
                                )
                                event.listen(
                                    session,
                                    "after_commit",
                                    after_commit,
                                )
                                try:
                                    session.commit()
                                finally:
                                    event.remove(
                                        session,
                                        "before_commit",
                                        before_commit,
                                    )
                                    event.remove(
                                        session,
                                        "after_commit",
                                        after_commit,
                                    )
                                return result
                            except BaseException:
                                session.rollback()
                                raise
                            finally:
                                if session.in_transaction():
                                    session.rollback()
                                _restore_sqlite_busy_timeout(session)
                                session.bind = original_bind
                except DBAPIError as exc:
                    if (
                        attempt + 1 < _MAX_POSTGRES_ATTEMPTS
                        and _retryable_postgres_error(exc)
                        and control.reset_retryable_commit()
                    ):
                        continue
                    raise
        raise AssertionError("unreachable")

    def _revalidate_stored_decision(
        self,
        session: Session,
        request: DecisionRequest,
        *,
        policy: ContextAccessPolicy,
        stored: _StoredDecision,
    ) -> DecisionResult:
        used_ids = tuple(
            source_ref.reference_id
            for source_ref in stored.source_refs
        )
        calendar_snapshot = self._calendar_visibility_for_candidates(
            session,
            used_ids=used_ids,
            candidates=stored.candidates,
        )
        source_validation_now = _as_utc(self._clock())
        self._lock_used_ref_sources(
            session,
            request,
            policy=policy,
            used_ids=used_ids,
            candidates=stored.candidates,
            now=source_validation_now,
            calendar_visibility_snapshot=calendar_snapshot,
        )
        validated_refs, source_limitations = self._revalidate_used_refs(
            session,
            request,
            policy=policy,
            used_ids=used_ids,
            candidates=stored.candidates,
            access_trace=stored.access_trace,
            lock_sources=False,
            now=source_validation_now,
            calendar_visibility_snapshot=calendar_snapshot,
        )
        self._require_calendar_visibility_current(calendar_snapshot)
        if tuple(validated_refs) != stored.source_refs:
            raise _FinalizationRejected(
                "decision_stored_source_contract_changed"
            )
        if any(
            limitation not in stored.result.limitations
            for limitation in source_limitations
        ):
            raise _FinalizationRejected(
                "decision_stored_source_context_changed",
                *source_limitations,
            )
        if (
            stored.result.proposed_action
            and "external_source_retention_unverified"
            in source_limitations
        ):
            raise _FinalizationRejected(
                "decision_action_requires_retained_source",
                "external_source_retention_unverified",
            )
        session.rollback()
        return stored.result.model_copy(
            update={
                "source_refs": list(validated_refs),
                "tool_trace": [],
            },
            deep=True,
        )

    def _resolve_policy(
        self,
        request: DecisionRequest,
        *,
        session: Session | None = None,
        lock: bool = False,
    ) -> ContextAccessPolicy:
        try:
            in_session = getattr(
                self._policy_resolver,
                "resolve_in_session",
                None,
            )
            raw_policy = (
                in_session(
                    request,
                    session,
                    lock=lock,
                )
                if session is not None and callable(in_session)
                else self._policy_resolver(request)
            )
            policy = strict_model_validate(
                ContextAccessPolicy,
                raw_policy,
            )
        except DBAPIError as exc:
            if _database_timeout_error(exc):
                raise TimeoutError(
                    "decision finalization database deadline expired"
                ) from exc
            raise _FinalizationRejected(
                "access_policy_resolution_failed"
            ) from exc
        except Exception as exc:
            raise _FinalizationRejected(
                "access_policy_resolution_failed"
            ) from exc
        policy_error = _request_policy_error(request, policy)
        if policy_error is not None:
            raise _FinalizationRejected(policy_error)
        return policy

    def _lock_used_ref_sources(
        self,
        session: Session,
        request: DecisionRequest,
        *,
        policy: ContextAccessPolicy,
        used_ids: Sequence[str],
        candidates: SourceCandidates,
        now: datetime,
        calendar_visibility_snapshot: CalendarVisibility | None,
    ) -> None:
        lock_refs: dict[str, SourceRef] = {}
        for reference_id in used_ids:
            for attempt in candidates.get(
                reference_id,
                (),
            ):
                for source_ref in attempt.supporting_refs:
                    lock_refs[source_ref.reference_id] = source_ref
        self._access_layer.start_turn(
            request,
            policy=policy,
        ).lock_source_refs_for_finalization(
            session,
            tuple(lock_refs.values()),
            now=now,
            calendar_visibility_snapshot=calendar_visibility_snapshot,
        )

    def _revalidate_used_refs(
        self,
        session: Session,
        request: DecisionRequest,
        *,
        policy: ContextAccessPolicy,
        used_ids: Sequence[str],
        candidates: SourceCandidates,
        access_trace: Sequence[AccessAuditEntry],
        lock_sources: bool,
        now: datetime | None = None,
        calendar_visibility_snapshot: CalendarVisibility | None,
    ) -> tuple[tuple[SourceRef, ...], tuple[str, ...]]:
        if not used_ids:
            return (), ()

        validation_now = _as_utc(now or self._clock())
        turn = self._access_layer.start_turn(request, policy=policy)
        if lock_sources:
            lock_refs: dict[str, SourceRef] = {}
            for reference_id in used_ids:
                for attempt in candidates.get(
                    reference_id,
                    (),
                ):
                    for source_ref in attempt.supporting_refs:
                        lock_refs[source_ref.reference_id] = source_ref
            turn.lock_source_refs_for_finalization(
                session,
                tuple(lock_refs.values()),
                now=validation_now,
                calendar_visibility_snapshot=(
                    calendar_visibility_snapshot
                ),
            )
        validated: list[SourceRef] = []
        limitations: set[str] = set()
        audit_by_query_id = {
            entry.query_id: entry for entry in access_trace
        }
        for reference_id in used_ids:
            attempts = candidates.get(reference_id, ())
            accepted: SourceRef | None = None
            rejected_reasons: set[str] = set()
            for attempt in attempts:
                candidate = next(
                    (
                        source_ref
                        for source_ref in attempt.supporting_refs
                        if source_ref.reference_id == reference_id
                    ),
                    None,
                )
                if candidate is None:
                    continue
                query = _effective_revalidation_query(
                    attempt.query,
                    audit_by_query_id.get(attempt.query.query_id),
                )
                checked, reasons = turn.revalidate_source_ref(
                    session,
                    query,
                    candidate,
                    context_source_refs=attempt.supporting_refs,
                    now=validation_now,
                    calendar_visibility_snapshot=(
                        calendar_visibility_snapshot
                    ),
                )
                rejected_reasons.update(reasons)
                if checked is not None:
                    accepted = checked
                    limitations.update(reasons)
                    break
            if accepted is None:
                raise _FinalizationRejected(
                    "decision_source_ref_revalidation_failed",
                    *sorted(rejected_reasons),
                )
            validated.append(accepted)
        return tuple(validated), tuple(sorted(limitations))

    def _calendar_visibility_for_candidates(
        self,
        session: Session,
        *,
        used_ids: Sequence[str],
        candidates: SourceCandidates,
    ) -> CalendarVisibility | None:
        refs = _supporting_refs_for_ids(
            used_ids=used_ids,
            candidates=candidates,
        )
        return self._access_layer.calendar_visibility_for_source_refs(
            session,
            refs,
        )

    def _require_calendar_visibility_current(
        self,
        visibility: CalendarVisibility | None,
    ) -> None:
        try:
            self._access_layer.require_calendar_visibility_current(
                visibility
            )
        except CalendarVisibilityChanged as exc:
            raise _FinalizationRejected(
                "decision_source_ref_revalidation_failed",
                "calendar_visibility_changed",
            ) from exc


def _supporting_refs_for_ids(
    *,
    used_ids: Sequence[str],
    candidates: SourceCandidates,
) -> tuple[SourceRef, ...]:
    refs: dict[str, SourceRef] = {}
    for reference_id in used_ids:
        for attempt in candidates.get(reference_id, ()):
            for source_ref in attempt.supporting_refs:
                refs[source_ref.reference_id] = source_ref
    return tuple(refs.values())


def _trace_source_candidates(
    trace: Sequence[ToolCallRecord],
) -> tuple[dict[str, tuple[_SourceAttempt, ...]], dict[str, SourceRef]]:
    candidates: dict[str, list[_SourceAttempt]] = {}
    refs: dict[str, SourceRef] = {}
    canonical_payloads: dict[str, dict[str, Any]] = {}
    for record in trace:
        if (
            record.status is not ToolCallStatus.COMPLETED
            or record.result is None
        ):
            continue
        supporting_refs = tuple(record.result.source_refs)
        for source_ref in supporting_refs:
            payload = source_ref.model_dump(
                mode="json",
                round_trip=True,
            )
            previous = canonical_payloads.get(source_ref.reference_id)
            if previous is not None and previous != payload:
                raise _FinalizationRejected(
                    "decision_source_ref_trace_conflict"
                )
            canonical_payloads[source_ref.reference_id] = payload
            refs[source_ref.reference_id] = source_ref
            candidates.setdefault(source_ref.reference_id, []).append(
                _SourceAttempt(
                    query=record.effective_query or record.query,
                    supporting_refs=supporting_refs,
                )
            )
    return (
        {
            reference_id: tuple(items)
            for reference_id, items in candidates.items()
        },
        refs,
    )


def _existing_stored_decision(
    session: Session,
    request: DecisionRequest,
    *,
    fingerprint: str,
    lock: bool = False,
) -> _StoredDecision | str | None:
    statement = select(DecisionRecord).where(
        or_(
            DecisionRecord.decision_request_id == request.request_id,
            DecisionRecord.decision_turn_id == request.turn_id,
        )
    )
    if lock:
        statement = statement.with_for_update()
    rows = tuple(session.scalars(statement))
    return _correlated_stored_decision(
        rows,
        request_id=request.request_id,
        turn_id=request.turn_id,
        fingerprint=fingerprint,
    )


def _correlated_stored_decision(
    rows: Sequence[DecisionRecord],
    *,
    request_id: uuid.UUID,
    turn_id: uuid.UUID,
    fingerprint: str,
) -> _StoredDecision | str | None:
    if not rows:
        return None
    by_request = next(
        (
            row
            for row in rows
            if row.decision_request_id == request_id
        ),
        None,
    )
    by_turn = next(
        (
            row
            for row in rows
            if row.decision_turn_id == turn_id
        ),
        None,
    )
    if by_turn is not None and by_turn is not by_request:
        return "decision_turn_id_conflict"
    if by_request is None:
        return "decision_turn_id_conflict"
    return _stored_decision(by_request, fingerprint=fingerprint)


def _stored_decision(
    row: DecisionRecord,
    *,
    fingerprint: str,
) -> _StoredDecision | str:
    if row.decision_request_fingerprint != fingerprint:
        return "decision_request_id_conflict"
    if (
        row.decision_request_id is None
        or row.decision_turn_id is None
        or row.decision_payload is None
        or row.decision_payload_digest is None
    ):
        return "decision_record_contract_invalid"
    try:
        normalized = normalize_untrusted_json(
            row.decision_payload,
            max_bytes=_MAX_STORED_JSON_BYTES,
        )
        if _json_digest(normalized.value) != row.decision_payload_digest:
            raise ValueError
        payload = _validate_stored_payload(normalized)
    except (TypeError, ValueError, ValidationError):
        return "decision_record_contract_invalid"
    if isinstance(payload, _StoredDecisionPayloadV1):
        result = payload.result
        request_ids_match = (
            decision_request_fingerprint(payload.request) == fingerprint
            and payload.request.request_id == row.decision_request_id
            and payload.request.turn_id == row.decision_turn_id
        )
        candidates_trace = payload.tool_trace
        access_trace = payload.access_trace
        persistence_intent = (
            DecisionPersistenceIntent.ACTION
            if result.proposed_action
            else DecisionPersistenceIntent.EXPLICIT_TRACKING
        )
    elif isinstance(payload, _StoredDecisionPayloadV2):
        result = payload.result
        request_ids_match = (
            payload.request.request_id == row.decision_request_id
            and payload.request.turn_id == row.decision_turn_id
        )
        candidates_trace = payload.source_attestations
        access_trace = payload.access_trace
        persistence_intent = payload.persistence_intent
    else:
        result = _result_from_stored_outcome(payload)
        request_ids_match = (
            payload.request.request_id == row.decision_request_id
            and payload.request.turn_id == row.decision_turn_id
        )
        candidates_trace = payload.source_attestations
        access_trace = payload.access_trace
        persistence_intent = payload.persistence_intent
    if (
        payload.request_fingerprint != fingerprint
        or not request_ids_match
        or result.request_id != row.decision_request_id
        or result.turn_id != row.decision_turn_id
        or result.decision_record_id != row.id
        or result.persistence_status is not PersistenceStatus.PERSISTED
        or result.tool_trace
        or payload.source_refs != tuple(result.source_refs)
        or payload.run.runtime != result.runtime
        or row.kind is not DecisionKind.INSIGHT
        or row.tree != _decision_tree(result)
        or row.summary != _public_summary(result)
        or row.llm_model
        != (
            payload.run.runtime.model[:64]
            if payload.run.runtime.model
            else None
        )
        or row.tokens != _runtime_token_total(payload.run.runtime)
        or persistence_intent is DecisionPersistenceIntent.NONE
        or (
            isinstance(payload, _StoredDecisionPayloadV3)
            and row.retention_basis_at is None
        )
    ):
        return "decision_record_contract_invalid"
    try:
        candidates, trace_refs = _stored_source_candidates(
            candidates_trace
        )
    except ValueError:
        return "decision_record_contract_invalid"
    if any(
        source_ref.reference_id not in candidates
        or trace_refs.get(source_ref.reference_id) != source_ref
        for source_ref in payload.source_refs
    ):
        return "decision_record_contract_invalid"
    trace_query_ids = {record.query.query_id for record in candidates_trace}
    if any(
        entry.query_id not in trace_query_ids
        for entry in access_trace
    ):
        return "decision_record_contract_invalid"
    return _StoredDecision(
        result=result,
        source_refs=payload.source_refs,
        candidates=candidates,
        access_trace=access_trace,
    )


def _validate_stored_payload(
    normalized: NormalizedJson,
) -> StoredDecisionPayload:
    if not isinstance(normalized.value, dict):
        raise ValueError("stored decision payload must be an object")
    schema_name = normalized.value.get("schema")
    if schema_name == _LEGACY_DECISION_PAYLOAD_SCHEMA:
        payload: StoredDecisionPayload = (
            _StoredDecisionPayloadV1.model_validate(normalized.value)
        )
    elif schema_name == _LEGACY_DECISION_PAYLOAD_SCHEMA_V2:
        payload = _StoredDecisionPayloadV2.model_validate(
            normalized.value
        )
    elif schema_name == DECISION_PAYLOAD_SCHEMA:
        payload = _StoredDecisionPayloadV3.model_validate(
            normalized.value
        )
    else:
        raise ValueError("stored decision payload schema is unsupported")
    canonical_payload = payload.model_dump(
        mode="json",
        round_trip=True,
        by_alias=True,
    )
    if (
        isinstance(payload, _StoredDecisionPayloadV1)
        and isinstance(normalized.value.get("request"), dict)
        and "persistence_requested" not in normalized.value["request"]
    ):
        canonical_payload["request"].pop(
            "persistence_requested",
            None,
        )
    canonical = normalize_untrusted_json(
        canonical_payload,
        max_bytes=_MAX_STORED_JSON_BYTES,
    )
    if _json_digest(canonical.value) != _json_digest(normalized.value):
        raise ValueError("stored payload is not canonical")
    return payload


def _stored_source_candidates(
    trace: Sequence[
        _StoredToolTraceRecord | _StoredSourceAttestation
    ],
) -> tuple[dict[str, tuple[_SourceAttempt, ...]], dict[str, SourceRef]]:
    candidates: dict[str, list[_SourceAttempt]] = {}
    refs: dict[str, SourceRef] = {}
    canonical_payloads: dict[str, dict[str, Any]] = {}
    for record in trace:
        if (
            isinstance(record, _StoredToolTraceRecord)
            and record.status is not ToolCallStatus.COMPLETED
        ):
            continue
        supporting_refs = record.source_refs
        for source_ref in supporting_refs:
            ref_payload = source_ref.model_dump(
                mode="json",
                round_trip=True,
            )
            previous = canonical_payloads.get(source_ref.reference_id)
            if previous is not None and previous != ref_payload:
                raise ValueError("stored source ref trace conflict")
            canonical_payloads[source_ref.reference_id] = ref_payload
            refs[source_ref.reference_id] = source_ref
            candidates.setdefault(source_ref.reference_id, []).append(
                _SourceAttempt(
                    query=record.query,
                    supporting_refs=supporting_refs,
                )
            )
    return (
        {
            reference_id: tuple(items)
            for reference_id, items in candidates.items()
        },
        refs,
    )


def _result_from_stored_outcome(
    payload: _StoredDecisionPayloadV3,
) -> DecisionResult:
    outcome = payload.outcome
    return DecisionResult(
        request_id=payload.request.request_id,
        turn_id=payload.request.turn_id,
        status=outcome.status,
        answer=outcome.summary,
        proposed_action=outcome.proposed_action,
        source_refs=list(payload.source_refs),
        limitations=_merge_limitations(
            outcome.limitation_codes,
            (_COMPACT_RECOVERY_LIMITATION,),
        ),
        confidence=outcome.confidence,
        persistence_status=PersistenceStatus.PERSISTED,
        decision_record_id=outcome.decision_record_id,
        runtime=payload.run.runtime,
        tool_trace=[],
    )


def _persisted_result(
    run: DecisionAgentRun,
    source_refs: Sequence[SourceRef],
    *,
    record_id: uuid.UUID,
    extra_limitations: Sequence[str] = (),
) -> DecisionResult:
    draft = run.draft
    return DecisionResult(
        request_id=run.request_id,
        turn_id=run.turn_id,
        status=draft.status,
        answer=draft.answer,
        proposed_action=draft.proposed_action,
        source_refs=list(source_refs),
        limitations=_merge_limitations(
            draft.limitations,
            _tool_trace_limitations(run.tool_trace),
            extra_limitations,
        ),
        clarification_question=draft.clarification_question,
        confidence=draft.confidence,
        uncertainty=draft.uncertainty,
        follow_up_question=draft.follow_up_question,
        persistence_status=PersistenceStatus.PERSISTED,
        decision_record_id=record_id,
        runtime=run.runtime,
        tool_trace=list(run.tool_trace),
    )


def _result_without_persistence(
    run: DecisionAgentRun,
    source_refs: Sequence[SourceRef],
    *,
    extra_limitations: Sequence[str] = (),
) -> DecisionResult:
    draft = run.draft
    return DecisionResult(
        request_id=run.request_id,
        turn_id=run.turn_id,
        status=draft.status,
        answer=draft.answer,
        proposed_action=False,
        source_refs=list(source_refs),
        limitations=_merge_limitations(
            draft.limitations,
            _tool_trace_limitations(run.tool_trace),
            extra_limitations,
        ),
        clarification_question=draft.clarification_question,
        confidence=draft.confidence,
        uncertainty=draft.uncertainty,
        follow_up_question=draft.follow_up_question,
        persistence_status=PersistenceStatus.NOT_REQUIRED,
        runtime=run.runtime,
        tool_trace=list(run.tool_trace),
    )


def _failure_result(
    run: DecisionAgentRun | Any,
    *,
    code: str,
    extra_limitations: Sequence[str] = (),
    persistence_required: bool,
) -> DecisionResult:
    request_id = getattr(run, "request_id", uuid.uuid4())
    turn_id = getattr(run, "turn_id", uuid.uuid4())
    run_limitations = (
        _tool_trace_limitations(run.tool_trace)
        if isinstance(run, DecisionAgentRun)
        else ()
    )
    return DecisionResult(
        request_id=request_id,
        turn_id=turn_id,
        status=DecisionStatus.FAILED,
        proposed_action=False,
        limitations=_merge_limitations(
            run_limitations,
            (*extra_limitations, code),
        ),
        persistence_status=(
            PersistenceStatus.FAILED
            if persistence_required
            else PersistenceStatus.NOT_REQUIRED
        ),
        runtime=RuntimeMetadata(runtime="healthmes-finalizer"),
        tool_trace=[],
    )


def _unknown_result(run: DecisionAgentRun | Any) -> DecisionResult:
    request_id = getattr(run, "request_id", uuid.uuid4())
    turn_id = getattr(run, "turn_id", uuid.uuid4())
    return DecisionResult(
        request_id=request_id,
        turn_id=turn_id,
        status=DecisionStatus.FAILED,
        proposed_action=False,
        limitations=[_FINALIZATION_OUTCOME_UNKNOWN],
        persistence_status=PersistenceStatus.UNKNOWN,
        runtime=RuntimeMetadata(runtime="healthmes-finalizer"),
        tool_trace=[],
    )


def _effective_persistence_intent(
    request: DecisionRequest | Any,
    run: DecisionAgentRun | Any,
) -> DecisionPersistenceIntent:
    """Classify persistence from trusted request state and bounded output."""

    if not isinstance(request, DecisionRequest) or not isinstance(
        run,
        DecisionAgentRun,
    ):
        return DecisionPersistenceIntent.NONE
    draft = run.draft
    if draft.status is not DecisionStatus.COMPLETED:
        return DecisionPersistenceIntent.NONE
    if draft.proposed_action:
        if draft.persistence_intent is DecisionPersistenceIntent.RISK:
            return DecisionPersistenceIntent.RISK
        return DecisionPersistenceIntent.ACTION
    if request.persistence_requested:
        return DecisionPersistenceIntent.EXPLICIT_TRACKING
    return DecisionPersistenceIntent.NONE


def _run_requires_persistence(
    request: DecisionRequest | Any,
    run: DecisionAgentRun | Any,
) -> bool:
    return (
        _effective_persistence_intent(request, run)
        is not DecisionPersistenceIntent.NONE
    )


def _merge_limitations(
    *groups: Sequence[str],
) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for value in group:
            if value not in merged:
                merged.append(value)
    if len(merged) <= 100:
        return merged
    return merged[:99] + [merged[-1]]


def _tool_trace_limitations(
    trace: Sequence[ToolCallRecord],
) -> tuple[str, ...]:
    """Preserve gateway-safe context limits even when the model omits them."""

    limitations: list[str] = []
    for record in trace:
        result_codes = (
            tuple(record.result.limitations)
            if record.result is not None
            else ()
        )
        limitations.extend(result_codes)
        if (
            record.error_code is not None
            and (
                record.error_code in result_codes
                or record.error_code in _SAFE_TOOL_ERROR_CODES
            )
        ):
            limitations.append(record.error_code)
    return tuple(_merge_limitations(limitations))


def _decision_tree(result: DecisionResult) -> dict[str, Any]:
    source_count = len(result.source_refs)
    tree: dict[str, Any] = {
        "schema": DECISION_RECORD_SCHEMA,
        "id": "healthmes-decision",
        "type": "llm_step",
        "label": "HealthMes wellness decision",
        "detail": "Private reasoning and provenance are audit-only.",
        "children": [
            {
                "id": "used-data",
                "type": "input",
                "label": (
                    f"{source_count} validated source reference"
                    f"{'' if source_count == 1 else 's'}"
                ),
                "detail": "Exact provenance is stored in private audit metadata.",
                "children": [],
            },
            *(
                [
                    {
                        "id": "proposed-action",
                        "type": "action",
                        "label": "Proposed wellness action",
                        "detail": "See the private decision response.",
                        "children": [],
                    }
                ]
                if result.proposed_action
                else []
            ),
        ],
        "healthmes": {
            "status": result.status.value,
            "proposed_action": result.proposed_action,
            "validated_source_count": source_count,
            "confidence": result.confidence,
            "persistence_status": result.persistence_status.value,
        },
    }
    normalized = normalize_untrusted_json(
        tree,
        max_bytes=_MAX_STORED_JSON_BYTES,
    )
    assert isinstance(normalized.value, dict)
    return normalized.value


def _decision_payload(
    request: DecisionRequest,
    run: DecisionAgentRun,
    result: DecisionResult,
    *,
    request_fingerprint: str,
    persistence_intent: DecisionPersistenceIntent,
    source_limitations: Sequence[str],
) -> dict[str, Any]:
    stored_outcome = _StoredDecisionOutcome(
        status=result.status,
        summary=_stored_outcome_summary(persistence_intent),
        proposed_action=result.proposed_action,
        limitation_codes=_sanitized_limitation_codes(
            _tool_trace_limitations(run.tool_trace),
            source_limitations,
        ),
        confidence=result.confidence,
        decision_record_id=result.decision_record_id,
    )
    stored_run = _StoredRun(
        runtime=run.runtime,
        system_policy_version=run.system_policy_version,
        started_at=run.started_at,
        finished_at=run.finished_at,
        steps_used=run.steps_used,
    )
    source_attestations = _source_attestations(
        run.tool_trace,
        selected_refs=result.source_refs,
    )
    attested_query_ids = {
        attestation.query.query_id
        for attestation in source_attestations
    }
    payload_model = _StoredDecisionPayloadV3(
        schema_name=DECISION_PAYLOAD_SCHEMA,
        request_fingerprint=request_fingerprint,
        request=_StoredDecisionRequestMetadata(
            request_id=request.request_id,
            turn_id=request.turn_id,
            requested_at=request.requested_at,
            timezone=request.timezone,
            execution_scope=request.caller.execution_scope.value,
            requested_privacy_level=(
                request.requested_privacy_level.value
            ),
        ),
        persistence_intent=persistence_intent,
        outcome=stored_outcome,
        run=stored_run,
        source_refs=tuple(result.source_refs),
        source_attestations=source_attestations,
        access_trace=tuple(
            entry
            for entry in run.access_trace
            if entry.query_id in attested_query_ids
        ),
    )
    payload = payload_model.model_dump(
        mode="json",
        round_trip=True,
        by_alias=True,
    )
    normalized = normalize_untrusted_json(
        payload,
        max_bytes=_MAX_STORED_JSON_BYTES,
    )
    assert isinstance(normalized.value, dict)
    return normalized.value


def _stored_outcome_summary(
    persistence_intent: DecisionPersistenceIntent,
) -> str:
    if persistence_intent is DecisionPersistenceIntent.RISK:
        return "A wellness risk warning was recorded."
    if persistence_intent is DecisionPersistenceIntent.ACTION:
        return "A wellness action recommendation was recorded."
    if (
        persistence_intent
        is DecisionPersistenceIntent.EXPLICIT_TRACKING
    ):
        return "A wellness decision was explicitly tracked."
    raise ValueError("unsupported stored decision persistence intent")


def _sanitized_limitation_codes(
    *groups: Sequence[str],
) -> tuple[str, ...]:
    codes: list[str] = []
    for values in groups:
        for value in values:
            if (
                _SAFE_LIMITATION_CODE.fullmatch(value) is None
                or value in codes
            ):
                continue
            codes.append(value)
            if len(codes) == _MAX_STORED_LIMITATION_CODES:
                return tuple(codes)
    return tuple(codes)


def _source_attestations(
    trace: Sequence[ToolCallRecord],
    *,
    selected_refs: Sequence[SourceRef],
) -> tuple[_StoredSourceAttestation, ...]:
    selected_ids = {
        source_ref.reference_id for source_ref in selected_refs
    }
    attestations: list[_StoredSourceAttestation] = []
    for record in trace:
        selected_result_refs = tuple(
            source_ref
            for source_ref in (
                record.result.source_refs
                if record.result is not None
                else ()
            )
            if source_ref.reference_id in selected_ids
        )
        if (
            record.status is not ToolCallStatus.COMPLETED
            or record.result is None
            or not selected_result_refs
        ):
            continue
        attestations.append(
            _StoredSourceAttestation(
                query=_attestation_query(
                    record.effective_query or record.query
                ),
                source_refs=selected_result_refs,
            )
        )
    return tuple(attestations)


def _attestation_query(query: ContextQuery) -> ContextQuery:
    """Remove model-authored prose while retaining revalidation selectors."""

    return query.model_copy(
        update={
            "parameters": {
                key: value
                for key, value in query.parameters.items()
                if key != "query"
            },
            "purpose": None,
        },
        deep=True,
    )


def _tool_trace_summary(record: ToolCallRecord) -> dict[str, Any]:
    result = record.result
    stored = _StoredToolTraceRecord(
        call_id=record.call_id,
        query=record.effective_query or record.query,
        status=record.status,
        started_at=record.started_at,
        finished_at=record.finished_at,
        context_status=(
            result.status if result is not None else None
        ),
        source_refs=(
            tuple(result.source_refs)
            if result is not None
            else ()
        ),
        limitations=(
            tuple(result.limitations)
            if result is not None
            else ()
        ),
        error_code=record.error_code,
    )
    return stored.model_dump(mode="json", round_trip=True)


def _public_summary(result: DecisionResult) -> str:
    if result.proposed_action:
        return "HealthMes wellness action recorded"
    return "HealthMes wellness decision recorded"


def _unvalidated_text_source_ref(
    run: DecisionAgentRun,
    *,
    allowed_reference_ids: frozenset[str],
) -> str | None:
    values = (
        run.draft.answer,
        run.draft.clarification_question,
        run.draft.uncertainty,
        run.draft.follow_up_question,
        *run.draft.limitations,
    )
    for value in values:
        if value is None:
            continue
        for match in _SOURCE_REF_TOKEN.finditer(value):
            token = match.group(0)
            if token not in allowed_reference_ids:
                return token
    return None


def _json_digest(value: Any) -> str:
    normalized = normalize_untrusted_json(
        value,
        max_bytes=_MAX_STORED_JSON_BYTES,
    ).value
    encoded = json.dumps(
        normalized,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _token_total(run: DecisionAgentRun) -> int | None:
    return _runtime_token_total(run.runtime)


def _runtime_token_total(runtime: RuntimeMetadata) -> int | None:
    values = [
        value
        for value in (
            runtime.input_tokens,
            runtime.output_tokens,
        )
        if value is not None
    ]
    if not values:
        return None
    total = sum(values)
    return total if total <= 2_147_483_647 else None


def _effective_revalidation_query(
    query: ContextQuery,
    audit: AccessAuditEntry | None,
) -> ContextQuery:
    if audit is None or audit.effective_privacy_level is None:
        return query
    update: dict[str, Any] = {
        "privacy_level": audit.effective_privacy_level,
    }
    if (
        audit.effective_start is not None
        and audit.effective_end is not None
    ):
        update["start"] = audit.effective_start
        update["end"] = audit.effective_end
    if audit.effective_limit is not None:
        update["limit"] = audit.effective_limit
    return query.model_copy(update=update, deep=True)


def _request_policy_error(
    request: DecisionRequest,
    policy: ContextAccessPolicy,
) -> str | None:
    if not request.caller.authenticated:
        return "caller_not_authenticated"
    if request.caller.principal_id != policy.owner_principal_id:
        return "caller_not_policy_owner"
    return None


def _begin_finalization_transaction(
    session: Session,
    *,
    timeout_seconds: float,
) -> None:
    if timeout_seconds <= 0:
        raise TimeoutError("decision finalization deadline expired")
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        # The file lock is acquired before SQLite's reserved write lock to
        # preserve the same order used by activity ingest/deletion.
        lock_activity_write_plane(
            session,
            timeout_seconds=timeout_seconds,
        )
        _begin_sqlite_immediate(
            session,
            timeout_seconds=timeout_seconds,
        )
        return
    if dialect == "postgresql":
        # ``postgres_activity_write_plane_guard`` already configured the
        # guarded connection before binding it to this session.
        session.connection()
        _configure_finalization_database_timeouts(
            session,
            timeout_seconds=timeout_seconds,
        )


@contextmanager
def _finalization_connection_guard(
    bind: Engine | Connection,
    *,
    timeout_seconds: float,
) -> Iterator[Connection | None]:
    """Keep SQLite checkouts alive through connection-local timeout cleanup."""

    if bind.dialect.name == "sqlite":
        if isinstance(bind, Connection):
            yield bind
            return
        with bind.connect() as connection:
            yield connection
        return
    with postgres_activity_write_plane_guard(
        bind,
        timeout_seconds=timeout_seconds,
    ) as connection:
        yield connection


def _begin_sqlite_immediate(
    session: Session,
    *,
    timeout_seconds: float,
) -> None:
    """Bound SQLite writers that bypass the HealthMes file-lock protocol."""

    connection = session.connection()
    if _SQLITE_BUSY_TIMEOUT_INFO_KEY not in session.info:
        original_timeout_ms = int(
            connection.exec_driver_sql(
                "PRAGMA busy_timeout"
            ).scalar_one()
        )
        session.info[_SQLITE_BUSY_TIMEOUT_INFO_KEY] = (
            connection,
            original_timeout_ms,
        )
    timeout_ms = max(1, int(timeout_seconds * 1_000))
    connection.exec_driver_sql(
        f"PRAGMA busy_timeout={timeout_ms}"
    )
    try:
        session.execute(text("BEGIN IMMEDIATE"))
    except BaseException:
        _restore_sqlite_busy_timeout(session)
        raise


def _restore_sqlite_busy_timeout(session: Session) -> None:
    state = session.info.pop(_SQLITE_BUSY_TIMEOUT_INFO_KEY, None)
    if state is None:
        return
    connection, original_timeout_ms = state
    try:
        connection.exec_driver_sql(
            f"PRAGMA busy_timeout={original_timeout_ms}"
        )
    except Exception as exc:
        try:
            connection.invalidate(exc)
        except Exception:
            pass


def _configure_finalization_database_timeouts(
    session: Session,
    *,
    timeout_seconds: float,
) -> None:
    if timeout_seconds <= 0:
        raise TimeoutError("decision finalization deadline expired")
    if session.get_bind().dialect.name != "postgresql":
        return
    session.connection()
    timeout_ms = max(1, int(timeout_seconds * 1_000))
    timeout_value = f"{timeout_ms}ms"
    session.execute(
        text(
            "SELECT set_config('lock_timeout', :timeout, true)"
        ),
        {"timeout": timeout_value},
    )
    session.execute(
        text(
            "SELECT set_config('statement_timeout', :timeout, true)"
        ),
        {"timeout": timeout_value},
    )


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - steady_time()
    if remaining <= 0:
        raise TimeoutError("decision finalization deadline expired")
    return remaining


def _ensure_finalization_deadline(deadline: float) -> None:
    _remaining_seconds(deadline)


def _retryable_postgres_error(exc: DBAPIError) -> bool:
    original = exc.orig
    state = getattr(original, "sqlstate", None) or getattr(
        original,
        "pgcode",
        None,
    )
    return state in _RETRYABLE_POSTGRES_STATES


def _database_timeout_error(exc: DBAPIError) -> bool:
    original = exc.orig
    state = getattr(original, "sqlstate", None) or getattr(
        original,
        "pgcode",
        None,
    )
    if state in _POSTGRES_TIMEOUT_STATES:
        return True
    return "database is locked" in str(original).casefold()


def _as_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )
