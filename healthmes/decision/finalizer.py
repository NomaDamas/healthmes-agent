"""Atomic provenance validation and persistence for decision-agent runs."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
from sqlalchemy import or_, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from healthmes.activity.locking import (
    activity_write_lock,
    lock_activity_write_plane,
    postgres_activity_write_plane_guard,
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
from healthmes.store import DecisionKind, DecisionRecord

DECISION_RECORD_SCHEMA = "healthmes.decision-record.v1"
DECISION_PAYLOAD_SCHEMA = "healthmes.decision-private.v1"
_PERSISTENCE_FAILURE = "decision_record_persistence_failed"
_MAX_STORED_JSON_BYTES = 2_000_000
_MAX_POSTGRES_ATTEMPTS = 3
_RETRYABLE_POSTGRES_STATES = frozenset({"40001", "40P01"})
_SOURCE_REF_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])sr_[0-9a-f]{32}(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

AccessPolicyResolver = Callable[[DecisionRequest], ContextAccessPolicy]


@dataclass(frozen=True, slots=True)
class _SourceAttempt:
    query: ContextQuery
    supporting_refs: tuple[SourceRef, ...]


SourceCandidates = Mapping[str, tuple[_SourceAttempt, ...]]


@dataclass(frozen=True, slots=True)
class _StoredDecision:
    request: DecisionRequest
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


class _StoredDecisionPayload(BaseModel):
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


class _FinalizationRejected(RuntimeError):
    def __init__(self, code: str, *limitations: str) -> None:
        super().__init__(code)
        self.code = code
        self.limitations = limitations


def decision_request_fingerprint(request: DecisionRequest) -> str:
    """Hash semantic request contents while excluding retry correlation IDs."""

    canonical = strict_model_validate(DecisionRequest, request)
    payload = canonical.model_dump(
        mode="json",
        round_trip=True,
        exclude={"request_id", "turn_id"},
    )
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DecisionFinalizer:
    """Promote a runtime draft only after current-source and storage checks."""

    def __init__(
        self,
        *,
        access_layer: ContextAccessLayer,
        session_factory: SessionFactory,
        policy_resolver: AccessPolicyResolver,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._access_layer = access_layer
        self._session_factory = session_factory
        self._policy_resolver = policy_resolver
        self._clock = clock or (lambda: datetime.now(UTC))

    def finalize(
        self,
        request: DecisionRequest,
        run: DecisionAgentRun,
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

        persistence_required = (
            canonical_run.draft.status is DecisionStatus.COMPLETED
            and bool(used_ids)
        )
        fingerprint = decision_request_fingerprint(canonical_request)

        try:
            policy = strict_model_validate(
                ContextAccessPolicy,
                self._policy_resolver(canonical_request),
            )
        except Exception:
            if used_ids:
                return _failure_result(
                    canonical_run,
                    code="access_policy_resolution_failed",
                    persistence_required=persistence_required,
                )
            return _result_without_persistence(canonical_run, ())
        policy_error = _request_policy_error(canonical_request, policy)
        if policy_error is not None:
            return _failure_result(
                canonical_run,
                code=policy_error,
                persistence_required=persistence_required,
            )

        try:
            if not persistence_required:
                with self._session_factory() as session:
                    validated_refs, source_limitations = (
                        self._revalidate_used_refs(
                            session,
                            canonical_request,
                            policy=policy,
                            used_ids=used_ids,
                            candidates=source_candidates,
                            access_trace=canonical_run.access_trace,
                            lock_sources=False,
                        )
                    )
                    session.rollback()
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
                    insert_if_missing=False,
                )
            except _FinalizationRejected as exc:
                return _failure_result(
                    canonical_run,
                    code=exc.code,
                    extra_limitations=exc.limitations,
                    persistence_required=persistence_required,
                )
            except Exception:
                return _failure_result(
                    canonical_run,
                    code=_PERSISTENCE_FAILURE,
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
        insert_if_missing: bool = True,
    ) -> DecisionResult:
        with activity_write_lock():
            for attempt in range(_MAX_POSTGRES_ATTEMPTS):
                try:
                    with self._session_factory() as session:
                        original_bind = session.bind
                        with postgres_activity_write_plane_guard(
                            session.get_bind()
                        ) as guarded_connection:
                            if guarded_connection is not None:
                                session.bind = guarded_connection
                            try:
                                _begin_finalization_transaction(session)
                                final_policy = self._resolve_policy(
                                    request,
                                )
                                existing = _existing_stored_decision(
                                    session,
                                    request,
                                    fingerprint=fingerprint,
                                    lock=True,
                                )
                                if existing is not None:
                                    if isinstance(existing, str):
                                        raise _FinalizationRejected(existing)
                                    return self._revalidate_stored_decision(
                                        session,
                                        request,
                                        policy=final_policy,
                                        stored=existing,
                                    )
                                if not insert_if_missing:
                                    raise _FinalizationRejected(
                                        _PERSISTENCE_FAILURE
                                    )

                                self._lock_used_ref_sources(
                                    session,
                                    request,
                                    policy=final_policy,
                                    used_ids=used_ids,
                                    candidates=candidates,
                                )

                                validated_refs, source_limitations = (
                                    self._revalidate_used_refs(
                                        session,
                                        request,
                                        policy=final_policy,
                                        used_ids=used_ids,
                                        candidates=candidates,
                                        access_trace=run.access_trace,
                                        lock_sources=False,
                                    )
                                )
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
                                payload = _decision_payload(
                                    request,
                                    run,
                                    result,
                                    request_fingerprint=fingerprint,
                                )
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
                                session.add(row)
                                session.flush()
                                session.commit()
                                return result
                            except BaseException:
                                session.rollback()
                                raise
                            finally:
                                if session.in_transaction():
                                    session.rollback()
                                session.bind = original_bind
                except DBAPIError as exc:
                    if (
                        attempt + 1 < _MAX_POSTGRES_ATTEMPTS
                        and _retryable_postgres_error(exc)
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
        self._lock_used_ref_sources(
            session,
            request,
            policy=policy,
            used_ids=used_ids,
            candidates=stored.candidates,
        )
        validated_refs, source_limitations = self._revalidate_used_refs(
            session,
            request,
            policy=policy,
            used_ids=used_ids,
            candidates=stored.candidates,
            access_trace=stored.access_trace,
            lock_sources=False,
        )
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
    ) -> ContextAccessPolicy:
        try:
            policy = strict_model_validate(
                ContextAccessPolicy,
                self._policy_resolver(request),
            )
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
    ) -> tuple[tuple[SourceRef, ...], tuple[str, ...]]:
        if not used_ids:
            return (), ()

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
            )
        now = _as_utc(self._clock())
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
                    now=now,
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
    result = payload.result
    if (
        payload.request_fingerprint != fingerprint
        or decision_request_fingerprint(payload.request) != fingerprint
        or payload.request.request_id != row.decision_request_id
        or payload.request.turn_id != row.decision_turn_id
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
    ):
        return "decision_record_contract_invalid"
    try:
        candidates, trace_refs = _stored_source_candidates(
            payload.tool_trace
        )
    except ValueError:
        return "decision_record_contract_invalid"
    if any(
        source_ref.reference_id not in candidates
        or trace_refs.get(source_ref.reference_id) != source_ref
        for source_ref in payload.source_refs
    ):
        return "decision_record_contract_invalid"
    trace_query_ids = {
        record.query.query_id for record in payload.tool_trace
    }
    if any(
        entry.query_id not in trace_query_ids
        for entry in payload.access_trace
    ):
        return "decision_record_contract_invalid"
    return _StoredDecision(
        request=payload.request,
        result=result,
        source_refs=payload.source_refs,
        candidates=candidates,
        access_trace=payload.access_trace,
    )


def _validate_stored_payload(
    normalized: NormalizedJson,
) -> _StoredDecisionPayload:
    payload = _StoredDecisionPayload.model_validate(normalized.value)
    canonical = normalize_untrusted_json(
        payload.model_dump(
            mode="json",
            round_trip=True,
            by_alias=True,
        ),
        max_bytes=_MAX_STORED_JSON_BYTES,
    )
    if _json_digest(canonical.value) != _json_digest(normalized.value):
        raise ValueError("stored payload is not canonical")
    return payload


def _stored_source_candidates(
    trace: Sequence[_StoredToolTraceRecord],
) -> tuple[dict[str, tuple[_SourceAttempt, ...]], dict[str, SourceRef]]:
    candidates: dict[str, list[_SourceAttempt]] = {}
    refs: dict[str, SourceRef] = {}
    canonical_payloads: dict[str, dict[str, Any]] = {}
    for record in trace:
        if record.status is not ToolCallStatus.COMPLETED:
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
    return DecisionResult(
        request_id=request_id,
        turn_id=turn_id,
        status=DecisionStatus.FAILED,
        proposed_action=False,
        limitations=_merge_limitations(
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
) -> dict[str, Any]:
    stored_result = result.model_copy(
        update={"tool_trace": []},
        deep=True,
    )
    stored_run = _StoredRun(
        runtime=run.runtime,
        system_policy_version=run.system_policy_version,
        started_at=run.started_at,
        finished_at=run.finished_at,
        steps_used=run.steps_used,
    )
    payload: dict[str, Any] = {
        "schema": DECISION_PAYLOAD_SCHEMA,
        "request_fingerprint": request_fingerprint,
        "request": request.model_dump(mode="json", round_trip=True),
        "result": stored_result.model_dump(
            mode="json",
            round_trip=True,
        ),
        "run": stored_run.model_dump(mode="json", round_trip=True),
        "source_refs": [
            item.model_dump(mode="json", round_trip=True)
            for item in result.source_refs
        ],
        "tool_trace": [
            _tool_trace_summary(record)
            for record in run.tool_trace
        ],
        "access_trace": [
            entry.model_dump(mode="json", round_trip=True)
            for entry in run.access_trace
        ],
    }
    normalized = normalize_untrusted_json(
        payload,
        max_bytes=_MAX_STORED_JSON_BYTES,
    )
    assert isinstance(normalized.value, dict)
    return normalized.value


def _tool_trace_summary(record: ToolCallRecord) -> dict[str, Any]:
    result = record.result
    stored = _StoredToolTraceRecord(
        call_id=record.call_id,
        query=record.query,
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


def _begin_finalization_transaction(session: Session) -> None:
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        # The file lock is acquired before SQLite's reserved write lock to
        # preserve the same order used by activity ingest/deletion.
        lock_activity_write_plane(session)
        session.execute(text("BEGIN IMMEDIATE"))
        return
    if dialect == "postgresql":
        # ``postgres_activity_write_plane_guard`` already configured the
        # guarded connection before binding it to this session.
        session.connection()


def _retryable_postgres_error(exc: DBAPIError) -> bool:
    original = exc.orig
    state = getattr(original, "sqlstate", None) or getattr(
        original,
        "pgcode",
        None,
    )
    return state in _RETRYABLE_POSTGRES_STATES


def _as_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )
