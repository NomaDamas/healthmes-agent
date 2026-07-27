from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.orm import Session

from healthmes.calendars.base import (
    CalendarAuthError,
    CalendarConflictError,
    CalendarError,
    ConfirmedExternalTimeChange,
    EventNotFoundError,
    ExternalEvent,
    coerce_utc,
    ensure_utc,
)
from healthmes.engine.rules import RuleThresholds
from healthmes.mcp_server.interpret import normalize_recovery
from healthmes.store.enums import (
    CalendarMutationOperation,
    CalendarMutationStatus,
    CalendarSource,
    DecisionKind,
)
from healthmes.store.models import (
    CalendarEventMirror,
    CalendarMutationProposal,
    DecisionRecord,
    TriggerEvent,
)

MORNING_NUDGE_RULE_ID = "morning_calendar_nudge"
SHORTEN_MINUTES = 30
MIN_ORIGINAL_DURATION = timedelta(minutes=60)
MIN_PROPOSED_DURATION = timedelta(minutes=30)
MIN_START_LEAD = timedelta(minutes=60)
START_SAFETY_LEAD = timedelta(minutes=15)
HANDLE_TTL = timedelta(minutes=60)
DEFAULT_FRESHNESS = timedelta(days=1)
APPLYING_RECONCILE_DELAY = timedelta(minutes=5)

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


AdjustmentOperation = CalendarMutationOperation
AdjustmentStatus = CalendarMutationStatus


TERMINAL_STATUSES = frozenset(
    {
        AdjustmentStatus.DECLINED,
        AdjustmentStatus.EXPIRED,
        AdjustmentStatus.APPLIED,
        AdjustmentStatus.APPLIED_RECOVERED,
        AdjustmentStatus.CONFLICTED,
        AdjustmentStatus.FAILED,
        AdjustmentStatus.FAILED_NO_CHANGE,
        AdjustmentStatus.UNKNOWN,
    }
)


class AmbiguousProviderResult(CalendarError):
    def __init__(self, provider_code: str = "ambiguous") -> None:
        super().__init__("calendar provider result is ambiguous")
        self.provider_code = provider_code


class AdjustmentError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    eligible: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HealthEvidenceResult:
    allowed: bool
    reason: str | None = None
    facts: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HandlePair:
    plaintext: str
    digest: str


@dataclass(frozen=True, slots=True)
class ProposalSnapshot:
    calendar_source: CalendarSource
    mirror_event_id: uuid.UUID | str
    external_event_id: str
    operation: AdjustmentOperation
    original_start_at: datetime
    original_end_at: datetime
    proposed_start_at: datetime
    proposed_end_at: datetime
    expected_etag: str
    protected_fingerprint: str
    dedup_key: str
    event_label: str | None = None
    local_timezone: str | None = None
    local_original_start_at: str | None = None
    local_original_end_at: str | None = None
    local_proposed_end_at: str | None = None

    def confirmed_change(self) -> ConfirmedExternalTimeChange:
        return ConfirmedExternalTimeChange(
            external_event_id=self.external_event_id,
            original_start_at=self.original_start_at,
            original_end_at=self.original_end_at,
            proposed_start_at=self.proposed_start_at,
            proposed_end_at=self.proposed_end_at,
            expected_etag=self.expected_etag,
        )


@dataclass(slots=True)
class StoredAdjustmentProposal:
    id: uuid.UUID
    snapshot: ProposalSnapshot
    reply_handle_digest: str
    expires_at: datetime
    status: AdjustmentStatus = AdjustmentStatus.PENDING
    consumed_at: datetime | None = None
    attempt_id: uuid.UUID | None = None
    receipt: dict[str, Any] | None = None
    proposal_decision_record_id: uuid.UUID | str | None = None
    outcome_decision_record_id: uuid.UUID | str | None = None
    response_channel: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    outcome: str
    proposal_id: uuid.UUID | None = None
    decision_record_id: uuid.UUID | str | None = None
    reply_handle: str | None = None
    expires_at: datetime | None = None
    reason: str | None = None
    decision_tree: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ResolveResult:
    status: AdjustmentStatus
    receipt: dict[str, Any]
    outcome_decision_tree: dict[str, Any] | None = None


class AdjustmentRepository(Protocol):
    def claim_daily_evaluation(
        self, dedup_key: str, payload: Mapping[str, Any], fired_at: datetime
    ) -> bool: ...

    def record_daily_evaluation_outcome(
        self, dedup_key: str, payload: Mapping[str, Any]
    ) -> None: ...

    def record_no_action_evaluation(
        self,
        dedup_key: str,
        *,
        reason: str,
        decision_tree: Mapping[str, Any],
    ) -> uuid.UUID | str: ...

    def has_existing_proposal(self, dedup_key: str) -> bool: ...

    def create_pending_proposal(
        self,
        snapshot: ProposalSnapshot,
        *,
        handle_digest: str,
        expires_at: datetime,
        decision_tree: Mapping[str, Any],
        now: datetime,
    ) -> StoredAdjustmentProposal: ...

    def get_proposal(self, proposal_id: uuid.UUID) -> StoredAdjustmentProposal | None: ...

    def compare_and_mark_applying(
        self,
        proposal_id: uuid.UUID,
        *,
        expected_digest: str,
        now: datetime,
        attempt_id: uuid.UUID,
        response_channel: str | None,
    ) -> StoredAdjustmentProposal | None: ...

    def commit_applying_boundary(
        self, proposal_id: uuid.UUID
    ) -> StoredAdjustmentProposal | None: ...

    def update_mirror_after_apply(
        self, proposal: StoredAdjustmentProposal, event: ExternalEvent
    ) -> None: ...

    def compare_and_mark_terminal(
        self,
        proposal_id: uuid.UUID,
        *,
        expected_status: AdjustmentStatus,
        status: AdjustmentStatus,
        receipt: Mapping[str, Any],
        outcome_decision_tree: Mapping[str, Any],
        now: datetime,
    ) -> StoredAdjustmentProposal | None: ...

    def pending_expired(self, now: datetime) -> Sequence[StoredAdjustmentProposal]: ...

    def stale_applying(self, now: datetime) -> Sequence[StoredAdjustmentProposal]: ...


class CalendarAdjustmentWriter(Protocol):
    def apply_confirmed_external_time_change(
        self, change: ConfirmedExternalTimeChange
    ) -> ExternalEvent: ...

    def read_event(self, external_id: str) -> ExternalEvent: ...


class InMemoryAdjustmentRepository:
    def __init__(self) -> None:
        self.proposals: dict[uuid.UUID, StoredAdjustmentProposal] = {}
        self.trigger_events: dict[str, dict[str, Any]] = {}
        self.decision_records: dict[uuid.UUID, dict[str, Any]] = {}
        self.committed_applying: list[uuid.UUID] = []

    def claim_daily_evaluation(
        self, dedup_key: str, payload: Mapping[str, Any], fired_at: datetime
    ) -> bool:
        if dedup_key in self.trigger_events:
            return False
        self.trigger_events[dedup_key] = {
            "rule_id": MORNING_NUDGE_RULE_ID,
            "dedup_key": dedup_key,
            "payload": dict(payload),
            "fired_at": ensure_utc(fired_at),
        }
        return True

    def has_existing_proposal(self, dedup_key: str) -> bool:
        return any(proposal.snapshot.dedup_key == dedup_key for proposal in self.proposals.values())

    def record_daily_evaluation_outcome(self, dedup_key: str, payload: Mapping[str, Any]) -> None:
        if dedup_key in self.trigger_events:
            self.trigger_events[dedup_key]["payload"].update(dict(payload))

    def record_no_action_evaluation(
        self,
        dedup_key: str,
        *,
        reason: str,
        decision_tree: Mapping[str, Any],
    ) -> uuid.UUID:
        decision_id = uuid.uuid4()
        self.decision_records[decision_id] = dict(decision_tree)
        self.record_daily_evaluation_outcome(
            dedup_key,
            {
                "outcome": "no_action",
                "reason": reason,
                "decision_record_id": str(decision_id),
            },
        )
        return decision_id

    def create_pending_proposal(
        self,
        snapshot: ProposalSnapshot,
        *,
        handle_digest: str,
        expires_at: datetime,
        decision_tree: Mapping[str, Any],
        now: datetime,
    ) -> StoredAdjustmentProposal:
        proposal_id = uuid.uuid4()
        proposal = StoredAdjustmentProposal(
            id=proposal_id,
            snapshot=snapshot,
            reply_handle_digest=handle_digest,
            expires_at=ensure_utc(expires_at),
            proposal_decision_record_id=str(uuid.uuid4()),
            created_at=ensure_utc(now),
            updated_at=ensure_utc(now),
        )
        self.proposals[proposal.id] = proposal
        return proposal

    def get_proposal(self, proposal_id: uuid.UUID) -> StoredAdjustmentProposal | None:
        return self.proposals.get(proposal_id)

    def commit_applying_boundary(self, proposal_id: uuid.UUID) -> StoredAdjustmentProposal | None:
        proposal = self.proposals.get(proposal_id)
        if proposal is not None:
            self.committed_applying.append(proposal_id)
        return proposal

    def update_mirror_after_apply(
        self, proposal: StoredAdjustmentProposal, event: ExternalEvent
    ) -> None:
        return None

    def compare_and_mark_applying(
        self,
        proposal_id: uuid.UUID,
        *,
        expected_digest: str,
        now: datetime,
        attempt_id: uuid.UUID,
        response_channel: str | None,
    ) -> StoredAdjustmentProposal | None:
        proposal = self.proposals.get(proposal_id)
        if (
            proposal is None
            or proposal.status != AdjustmentStatus.PENDING
            or not hmac.compare_digest(proposal.reply_handle_digest, expected_digest)
            or proposal.expires_at <= ensure_utc(now)
        ):
            return None
        proposal.status = AdjustmentStatus.APPLYING
        proposal.consumed_at = ensure_utc(now)
        proposal.attempt_id = attempt_id
        proposal.response_channel = response_channel
        proposal.updated_at = ensure_utc(now)
        return proposal

    def compare_and_mark_terminal(
        self,
        proposal_id: uuid.UUID,
        *,
        expected_status: AdjustmentStatus,
        status: AdjustmentStatus,
        receipt: Mapping[str, Any],
        outcome_decision_tree: Mapping[str, Any],
        now: datetime,
    ) -> StoredAdjustmentProposal | None:
        proposal = self.proposals.get(proposal_id)
        if proposal is None or proposal.status != expected_status:
            return None
        proposal.status = status
        proposal.receipt = dict(receipt)
        if status == AdjustmentStatus.DECLINED and proposal.consumed_at is None:
            proposal.consumed_at = ensure_utc(now)
        proposal.outcome_decision_record_id = str(uuid.uuid4())
        proposal.updated_at = ensure_utc(now)
        return proposal

    def pending_expired(self, now: datetime) -> Sequence[StoredAdjustmentProposal]:
        now_utc = ensure_utc(now)
        return [
            proposal
            for proposal in self.proposals.values()
            if proposal.status == AdjustmentStatus.PENDING and proposal.expires_at <= now_utc
        ]

    def stale_applying(self, now: datetime) -> Sequence[StoredAdjustmentProposal]:
        cutoff = ensure_utc(now) - APPLYING_RECONCILE_DELAY
        return [
            proposal
            for proposal in self.proposals.values()
            if proposal.status == AdjustmentStatus.APPLYING
            and proposal.updated_at is not None
            and proposal.updated_at <= cutoff
        ]


class SqlAlchemyAdjustmentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session
        self.begin_immediate_attempted = False

    def begin_evaluation_boundary(self) -> None:
        self._begin_immediate_if_possible()

    def claim_daily_evaluation(
        self, dedup_key: str, payload: Mapping[str, Any], fired_at: datetime
    ) -> bool:
        self._begin_immediate_if_possible()
        existing = self._session.scalar(
            sa.select(TriggerEvent.id).where(TriggerEvent.dedup_key == dedup_key).limit(1)
        )
        if existing is not None:
            return False
        self._session.add(
            TriggerEvent(
                fired_at=ensure_utc(fired_at),
                rule_id=MORNING_NUDGE_RULE_ID,
                payload=dict(payload),
                alert_sent=False,
                dedup_key=dedup_key,
            )
        )
        self._session.flush()
        return True

    def record_daily_evaluation_outcome(self, dedup_key: str, payload: Mapping[str, Any]) -> None:
        event = self._session.scalar(
            sa.select(TriggerEvent).where(TriggerEvent.dedup_key == dedup_key).limit(1)
        )
        if event is None:
            return
        event.payload = {**dict(event.payload or {}), **dict(payload)}
        self._session.flush()

    def record_no_action_evaluation(
        self,
        dedup_key: str,
        *,
        reason: str,
        decision_tree: Mapping[str, Any],
    ) -> uuid.UUID:
        decision = DecisionRecord(
            kind=DecisionKind.SCHEDULE_CHANGE,
            tree=dict(decision_tree),
            summary="Calendar adjustment no action",
            llm_model=None,
            tokens=None,
        )
        self._session.add(decision)
        self._session.flush()
        self.record_daily_evaluation_outcome(
            dedup_key,
            {
                "outcome": "no_action",
                "reason": reason,
                "decision_record_id": str(decision.id),
            },
        )
        return decision.id

    def has_existing_proposal(self, dedup_key: str) -> bool:
        existing = self._session.scalar(
            sa.select(CalendarMutationProposal.id)
            .where(CalendarMutationProposal.dedup_key == dedup_key)
            .limit(1)
        )
        return existing is not None

    def create_pending_proposal(
        self,
        snapshot: ProposalSnapshot,
        *,
        handle_digest: str,
        expires_at: datetime,
        decision_tree: Mapping[str, Any],
        now: datetime,
    ) -> StoredAdjustmentProposal:
        existing = self._session.scalar(
            sa.select(CalendarMutationProposal)
            .where(CalendarMutationProposal.dedup_key == snapshot.dedup_key)
            .limit(1)
        )
        if existing is not None:
            return self._from_model(existing)

        proposal_id = uuid.uuid4()
        stored_tree = dict(decision_tree)
        stored_tree["detail"] = {
            **dict(stored_tree.get("detail") or {}),
            "proposal_id": str(proposal_id),
        }
        decision = DecisionRecord(
            kind=DecisionKind.SCHEDULE_CHANGE,
            tree=stored_tree,
            summary="Calendar adjustment proposal",
            llm_model=None,
            tokens=None,
        )
        self._session.add(decision)
        self._session.flush()

        proposal = CalendarMutationProposal(
            id=proposal_id,
            calendar_source=snapshot.calendar_source,
            mirror_event_id=snapshot.mirror_event_id,
            external_event_id=snapshot.external_event_id,
            operation=AdjustmentOperation.SHORTEN,
            original_start_at=snapshot.original_start_at,
            original_end_at=snapshot.original_end_at,
            proposed_start_at=snapshot.proposed_start_at,
            proposed_end_at=snapshot.proposed_end_at,
            expected_etag=snapshot.expected_etag,
            protected_fingerprint=snapshot.protected_fingerprint,
            reply_handle_digest=handle_digest,
            expires_at=ensure_utc(expires_at),
            consumed_at=None,
            attempt_id=None,
            status=AdjustmentStatus.PENDING,
            dedup_key=snapshot.dedup_key,
            proposal_decision_record_id=decision.id,
            outcome_decision_record_id=None,
            response_channel=None,
            receipt=None,
        )
        self._session.add(proposal)
        self._session.flush()
        return self._from_model(proposal)

    def get_proposal(self, proposal_id: uuid.UUID) -> StoredAdjustmentProposal | None:
        proposal = self._session.get(CalendarMutationProposal, proposal_id)
        return self._from_model(proposal) if proposal is not None else None

    def compare_and_mark_applying(
        self,
        proposal_id: uuid.UUID,
        *,
        expected_digest: str,
        now: datetime,
        attempt_id: uuid.UUID,
        response_channel: str | None,
    ) -> StoredAdjustmentProposal | None:
        result = self._session.execute(
            sa.update(CalendarMutationProposal)
            .where(
                CalendarMutationProposal.id == proposal_id,
                CalendarMutationProposal.status == AdjustmentStatus.PENDING,
                CalendarMutationProposal.reply_handle_digest == expected_digest,
                CalendarMutationProposal.expires_at > ensure_utc(now),
            )
            .values(
                status=AdjustmentStatus.APPLYING,
                consumed_at=ensure_utc(now),
                attempt_id=str(attempt_id),
                response_channel=response_channel,
            )
        )
        if result.rowcount != 1:
            self._session.expire_all()
            return None
        self._session.flush()
        return self.get_proposal(proposal_id)

    def commit_applying_boundary(self, proposal_id: uuid.UUID) -> StoredAdjustmentProposal | None:
        self._session.commit()
        return self.get_proposal(proposal_id)

    def update_mirror_after_apply(
        self, proposal: StoredAdjustmentProposal, event: ExternalEvent
    ) -> None:
        mirror = self._session.get(CalendarEventMirror, proposal.snapshot.mirror_event_id)
        if mirror is None:
            return
        mirror.end_at = event.end_at
        mirror.etag = event.etag
        self._session.flush()

    def compare_and_mark_terminal(
        self,
        proposal_id: uuid.UUID,
        *,
        expected_status: AdjustmentStatus,
        status: AdjustmentStatus,
        receipt: Mapping[str, Any],
        outcome_decision_tree: Mapping[str, Any],
        now: datetime,
    ) -> StoredAdjustmentProposal | None:
        decision = DecisionRecord(
            kind=DecisionKind.SCHEDULE_CHANGE,
            tree=dict(outcome_decision_tree),
            summary=f"Calendar adjustment {status.value}",
            llm_model=None,
            tokens=None,
        )
        self._session.add(decision)
        self._session.flush()
        values: dict[str, Any] = {
            "status": status,
            "receipt": dict(receipt),
            "outcome_decision_record_id": decision.id,
        }
        if status == AdjustmentStatus.DECLINED:
            values["consumed_at"] = ensure_utc(now)
        result = self._session.execute(
            sa.update(CalendarMutationProposal)
            .where(
                CalendarMutationProposal.id == proposal_id,
                CalendarMutationProposal.status == expected_status,
            )
            .values(**values)
        )
        if result.rowcount != 1:
            self._session.delete(decision)
            self._session.flush()
            self._session.expire_all()
            return None
        self._session.flush()
        self._session.expire_all()
        return self.get_proposal(proposal_id)

    def pending_expired(self, now: datetime) -> Sequence[StoredAdjustmentProposal]:
        rows = self._session.scalars(
            sa.select(CalendarMutationProposal).where(
                CalendarMutationProposal.status == AdjustmentStatus.PENDING,
                CalendarMutationProposal.expires_at <= ensure_utc(now),
            )
        ).all()
        return [self._from_model(row) for row in rows]

    def stale_applying(self, now: datetime) -> Sequence[StoredAdjustmentProposal]:
        cutoff = ensure_utc(now) - APPLYING_RECONCILE_DELAY
        rows = self._session.scalars(
            sa.select(CalendarMutationProposal).where(
                CalendarMutationProposal.status == AdjustmentStatus.APPLYING,
                CalendarMutationProposal.updated_at <= cutoff,
            )
        ).all()
        return [self._from_model(row) for row in rows]

    @staticmethod
    def _from_model(row: CalendarMutationProposal) -> StoredAdjustmentProposal:
        snapshot = ProposalSnapshot(
            calendar_source=row.calendar_source,
            mirror_event_id=row.mirror_event_id,
            external_event_id=row.external_event_id,
            operation=row.operation,
            original_start_at=coerce_utc(row.original_start_at),
            original_end_at=coerce_utc(row.original_end_at),
            proposed_start_at=coerce_utc(row.proposed_start_at),
            proposed_end_at=coerce_utc(row.proposed_end_at),
            expected_etag=row.expected_etag,
            protected_fingerprint=row.protected_fingerprint,
            dedup_key=row.dedup_key,
        )
        parsed_attempt_id = uuid.UUID(row.attempt_id) if row.attempt_id else None
        return StoredAdjustmentProposal(
            id=row.id,
            snapshot=snapshot,
            reply_handle_digest=row.reply_handle_digest,
            expires_at=coerce_utc(row.expires_at),
            status=row.status,
            consumed_at=coerce_utc(row.consumed_at) if row.consumed_at is not None else None,
            attempt_id=parsed_attempt_id,
            receipt=dict(row.receipt or {}) if row.receipt is not None else None,
            proposal_decision_record_id=row.proposal_decision_record_id,
            outcome_decision_record_id=row.outcome_decision_record_id,
            response_channel=row.response_channel,
            created_at=coerce_utc(row.created_at) if row.created_at is not None else None,
            updated_at=coerce_utc(row.updated_at) if row.updated_at is not None else None,
        )

    def _begin_immediate_if_possible(self) -> None:
        bind = self._session.get_bind()
        if bind.dialect.name != "sqlite" or self._session.in_transaction():
            return
        self._session.execute(sa.text("BEGIN IMMEDIATE"))
        self.begin_immediate_attempted = True


class CalendarAdjustmentService:
    def __init__(
        self,
        repository: AdjustmentRepository,
        *,
        handle_secret: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._handle_secret = handle_secret
        self._clock = clock or (lambda: datetime.now(UTC))

    def evaluate_morning_calendar_nudge(
        self,
        *,
        local_date: date,
        timezone: tzinfo,
        health_context: Mapping[str, Any],
        candidates: Sequence[Any],
        afternoon_busy_minutes: int,
        handle_factory: Callable[[], str] | None = None,
    ) -> EvaluationResult:
        now = ensure_utc(self._clock())
        day_key = morning_dedup_key(local_date)
        claimed = self._repository.claim_daily_evaluation(day_key, {"outcome": "evaluating"}, now)
        if not claimed:
            return EvaluationResult(outcome="deduplicated")

        eligible = [
            candidate
            for candidate in sorted(
                candidates, key=lambda item: coerce_utc(_attr(item, "start_at"))
            )
            if evaluate_event_eligibility(
                candidate,
                now=now,
                local_date=local_date,
                timezone=timezone,
                already_proposed=self._repository.has_existing_proposal(
                    proposal_dedup_key(
                        candidate, timezone=timezone, operation=AdjustmentOperation.SHORTEN
                    )
                ),
            ).eligible
        ]
        health = evaluate_health_evidence(
            health_context,
            local_date=local_date,
            now=now,
            afternoon_busy_minutes=afternoon_busy_minutes,
            eligible_event_count=len(eligible),
        )
        if not health.allowed:
            reason = health.reason or "policy_blocked"
            tree = no_action_decision_tree(reason, health.facts)
            decision_record_id = self._repository.record_no_action_evaluation(
                day_key,
                reason=reason,
                decision_tree=tree,
            )
            return EvaluationResult(
                outcome="no_action",
                reason=reason,
                decision_record_id=decision_record_id,
                decision_tree=tree,
            )

        target = eligible[0]
        snapshot = make_shorten_snapshot(target, timezone=timezone)
        decision_tree = initial_decision_tree(snapshot, health.facts)
        pair = issue_reply_handle(self._handle_secret, handle_factory=handle_factory)
        proposal = self._repository.create_pending_proposal(
            snapshot,
            handle_digest=pair.digest,
            expires_at=proposal_expiry(snapshot.original_start_at, now),
            decision_tree=decision_tree,
            now=now,
        )
        self._repository.record_daily_evaluation_outcome(
            day_key,
            {
                "outcome": "proposed",
                "proposal_id": str(proposal.id),
                "decision_record_id": str(proposal.proposal_decision_record_id),
            },
        )
        return EvaluationResult(
            outcome="proposed",
            proposal_id=proposal.id,
            reply_handle=pair.plaintext,
            expires_at=proposal.expires_at,
            decision_tree=decision_tree,
        )

    def resolve_calendar_adjustment(
        self,
        proposal_id: uuid.UUID,
        *,
        response: str,
        reply_handle: str | None,
        writer: CalendarAdjustmentWriter,
        response_channel: str | None = None,
        mirror_snapshot: Any | None = None,
    ) -> ResolveResult:
        now = ensure_utc(self._clock())
        proposal = self._repository.get_proposal(proposal_id)
        if proposal is None:
            return ResolveResult(
                AdjustmentStatus.FAILED,
                redacted_receipt(status=AdjustmentStatus.FAILED, provider_code="not_found"),
            )
        if proposal.status in TERMINAL_STATUSES:
            return ResolveResult(
                proposal.status, proposal.receipt or redacted_receipt(status=proposal.status)
            )
        if not reply_handle:
            return ResolveResult(
                proposal.status,
                redacted_receipt(status=proposal.status, provider_code="missing_handle"),
            )

        digest = digest_reply_handle(reply_handle, self._handle_secret)
        if not hmac.compare_digest(proposal.reply_handle_digest, digest):
            return ResolveResult(
                proposal.status,
                redacted_receipt(status=proposal.status, provider_code="invalid_handle"),
            )

        if is_expired(proposal, now):
            return self._expire(proposal, now)

        normalized = response.strip().lower()
        if normalized in {"no", "n", "decline", "declined", "그대로"}:
            return self._terminal(
                proposal,
                AdjustmentStatus.DECLINED,
                "user_declined",
                now,
                expected_status=AdjustmentStatus.PENDING,
            )
        if normalized not in {"yes", "y", "apply", "applied", "적용"}:
            return ResolveResult(
                proposal.status,
                redacted_receipt(status=proposal.status, provider_code="unsupported_response"),
            )

        attempt_id = uuid.uuid4()
        applying = self._repository.compare_and_mark_applying(
            proposal.id,
            expected_digest=digest,
            now=now,
            attempt_id=attempt_id,
            response_channel=response_channel,
        )
        if applying is None:
            current = self._repository.get_proposal(proposal.id)
            current_status = current.status if current is not None else AdjustmentStatus.FAILED
            return ResolveResult(
                current_status,
                redacted_receipt(status=current_status, provider_code="not_pending"),
            )
        applying = self._repository.commit_applying_boundary(applying.id) or applying
        if mirror_snapshot is not None and not snapshot_matches(applying.snapshot, mirror_snapshot):
            return self._terminal(
                applying,
                AdjustmentStatus.CONFLICTED,
                "snapshot_changed",
                now,
                expected_status=AdjustmentStatus.APPLYING,
            )

        try:
            event = writer.apply_confirmed_external_time_change(
                applying.snapshot.confirmed_change()
            )
        except CalendarConflictError:
            return self._terminal(
                applying,
                AdjustmentStatus.CONFLICTED,
                "provider_412",
                now,
                expected_status=AdjustmentStatus.APPLYING,
            )
        except (CalendarAuthError, EventNotFoundError, ValueError):
            return self._terminal(
                applying,
                AdjustmentStatus.FAILED,
                "known_provider_failure",
                now,
                expected_status=AdjustmentStatus.APPLYING,
            )
        except AmbiguousProviderResult as exc:
            return self._reconcile(applying, writer, exc.provider_code, now)
        except CalendarError:
            return self._terminal(
                applying,
                AdjustmentStatus.UNKNOWN,
                "provider_200_mismatch",
                now,
                expected_status=AdjustmentStatus.APPLYING,
            )
        except Exception as exc:
            status = _http_status(exc)
            if status == 412:
                return self._terminal(
                    applying,
                    AdjustmentStatus.CONFLICTED,
                    "provider_412",
                    now,
                    expected_status=AdjustmentStatus.APPLYING,
                )
            if status in {400, 401, 403, 404, 410}:
                return self._terminal(
                    applying,
                    AdjustmentStatus.FAILED,
                    f"provider_{status}",
                    now,
                    expected_status=AdjustmentStatus.APPLYING,
                )
            if status in {429, 500, 502, 503, 504}:
                return self._reconcile(applying, writer, f"provider_{status}", now)
            return self._reconcile(applying, writer, "ambiguous", now)

        if remote_matches_snapshot(applying.snapshot, event):
            return self._terminal(
                applying,
                AdjustmentStatus.APPLIED,
                "provider_200",
                now,
                event=event,
                expected_status=AdjustmentStatus.APPLYING,
            )
        return self._terminal(
            applying,
            AdjustmentStatus.UNKNOWN,
            "provider_200_mismatch",
            now,
            expected_status=AdjustmentStatus.APPLYING,
        )

    def expire_and_reconcile_adjustments(
        self, writer: CalendarAdjustmentWriter
    ) -> list[ResolveResult]:
        now = ensure_utc(self._clock())
        results: list[ResolveResult] = []
        for proposal in self._repository.pending_expired(now):
            results.append(self._expire(proposal, now))
        for proposal in self._repository.stale_applying(now):
            results.append(self._reconcile(proposal, writer, "restart_recovery", now))
        return results

    def _expire(self, proposal: StoredAdjustmentProposal, now: datetime) -> ResolveResult:
        return self._terminal(
            proposal,
            AdjustmentStatus.EXPIRED,
            "expired",
            now,
            expected_status=AdjustmentStatus.PENDING,
        )

    def _terminal(
        self,
        proposal: StoredAdjustmentProposal,
        status: AdjustmentStatus,
        provider_code: str,
        now: datetime,
        *,
        event: ExternalEvent | None = None,
        expected_status: AdjustmentStatus,
    ) -> ResolveResult:
        receipt = redacted_receipt(
            status=status,
            provider_code=provider_code,
            provider_event=event,
        )
        tree = outcome_decision_tree(proposal, receipt)
        terminal = self._repository.compare_and_mark_terminal(
            proposal.id,
            expected_status=expected_status,
            status=status,
            receipt=receipt,
            outcome_decision_tree=tree,
            now=now,
        )
        if terminal is None:
            current = self._repository.get_proposal(proposal.id)
            current_status = current.status if current is not None else AdjustmentStatus.FAILED
            current_receipt = (
                current.receipt
                if current is not None and current.receipt is not None
                else redacted_receipt(status=current_status, provider_code="transition_lost")
            )
            return ResolveResult(current_status, current_receipt)
        if event is not None and status in {
            AdjustmentStatus.APPLIED,
            AdjustmentStatus.APPLIED_RECOVERED,
        }:
            self._repository.update_mirror_after_apply(terminal, event)
        return ResolveResult(status, receipt, tree)

    def _reconcile(
        self,
        proposal: StoredAdjustmentProposal,
        writer: CalendarAdjustmentWriter,
        provider_code: str,
        now: datetime,
    ) -> ResolveResult:
        event = read_remote_event(writer, proposal.snapshot.external_event_id)
        if event is not None and remote_matches_snapshot(proposal.snapshot, event):
            return self._terminal(
                proposal,
                AdjustmentStatus.APPLIED_RECOVERED,
                provider_code,
                now,
                event=event,
                expected_status=AdjustmentStatus.APPLYING,
            )
        return self._terminal(
            proposal,
            AdjustmentStatus.UNKNOWN,
            provider_code,
            now,
            expected_status=AdjustmentStatus.APPLYING,
        )


def evaluate_event_eligibility(
    event: Any,
    *,
    now: datetime,
    local_date: date,
    timezone: tzinfo,
    already_proposed: bool = False,
) -> EligibilityResult:
    reasons: list[str] = []
    source = _attr(event, "calendar_source", CalendarSource.GOOGLE)
    start = coerce_utc(_attr(event, "start_at"))
    end = coerce_utc(_attr(event, "end_at"))
    local_start = start.astimezone(timezone)
    local_end = end.astimezone(timezone)

    if _source_value(source) != CalendarSource.GOOGLE.value:
        reasons.append("unsupported_source")
    if bool(_attr(event, "is_agent_created", False)):
        reasons.append("agent_owned_path_only")
    if not bool(_attr(event, "organizer_self", False)):
        reasons.append("not_self_organized")
    if bool(_attr(event, "has_attendees", False)):
        reasons.append("has_attendees")
    if bool(_attr(event, "is_recurring", False)):
        reasons.append("recurring")
    if bool(_attr(event, "is_all_day", False)):
        reasons.append("all_day")
    if (_attr(event, "event_type", "default") or "default") != "default":
        reasons.append("unsupported_event_type")
    if bool(_attr(event, "is_locked", False)):
        reasons.append("locked")
    if str(_attr(event, "status", "") or "").lower() == "cancelled":
        reasons.append("cancelled")
    if start < ensure_utc(now) + MIN_START_LEAD:
        reasons.append("too_soon")
    if local_start.date() != local_date or local_end.date() != local_date:
        reasons.append("not_today")
    if end - start < MIN_ORIGINAL_DURATION:
        reasons.append("too_short")
    if already_proposed:
        reasons.append("already_proposed")
    if not _attr(event, "etag", None):
        reasons.append("missing_etag")
    return EligibilityResult(eligible=not reasons, reasons=tuple(reasons))


def evaluate_health_evidence(
    context: Mapping[str, Any],
    *,
    local_date: date,
    now: datetime,
    afternoon_busy_minutes: int,
    eligible_event_count: int,
    freshness: timedelta = DEFAULT_FRESHNESS,
    thresholds: RuleThresholds | None = None,
) -> HealthEvidenceResult:
    thresholds = thresholds or RuleThresholds()
    sleep = _mapping(context.get("sleep_debt"))
    if sleep.get("status") != "ok":
        return HealthEvidenceResult(False, "missing_sleep")
    if _confidence(sleep.get("confidence")) < CONFIDENCE_RANK["medium"]:
        return HealthEvidenceResult(False, "low_confidence_sleep")
    if not _fresh_enough(sleep, local_date=local_date, now=now, freshness=freshness):
        return HealthEvidenceResult(False, "stale_sleep")

    hrv_block = _mapping(context.get("nocturnal_hrv") or context.get("hrv"))
    charge_block = _mapping(
        context.get("charge")
        or context.get("body_battery")
        or context.get("readiness")
        or context.get("recovery")
    )
    recovery_blocks = [hrv_block, charge_block]
    usable_recovery = [
        block
        for block in recovery_blocks
        if block.get("status") == "ok"
        and _confidence(block.get("confidence")) >= CONFIDENCE_RANK["medium"]
        and _fresh_enough(block, local_date=local_date, now=now, freshness=freshness)
    ]
    if not usable_recovery:
        if any(block for block in recovery_blocks):
            if any(
                _confidence(block.get("confidence")) < CONFIDENCE_RANK["medium"]
                for block in recovery_blocks
            ):
                return HealthEvidenceResult(False, "low_confidence_recovery")
            return HealthEvidenceResult(False, "stale_recovery")
        return HealthEvidenceResult(False, "missing_recovery")

    charge_is_usable = any(block is charge_block for block in usable_recovery)
    recovery_value = _recovery_value([charge_block]) if charge_is_usable else None
    if recovery_value is None:
        return HealthEvidenceResult(False, "missing_recovery_score")
    if recovery_value > thresholds.low_recovery_max_value:
        return HealthEvidenceResult(False, "no_nudge_needed")
    if afternoon_busy_minutes < thresholds.heavy_afternoon_min_busy_minutes:
        return HealthEvidenceResult(False, "afternoon_not_heavy")
    if eligible_event_count < 1:
        return HealthEvidenceResult(False, "no_eligible_event")

    return HealthEvidenceResult(
        True,
        facts={
            "sleep_confidence": sleep.get("confidence"),
            "recovery_confidence": usable_recovery[0].get("confidence"),
            "recovery_value_bucket": _bucket_recovery(recovery_value),
            "afternoon_busy_minutes": afternoon_busy_minutes,
        },
    )


def validate_shorten_change(
    *,
    external_event_id: str,
    original_start_at: datetime,
    original_end_at: datetime,
    proposed_start_at: datetime,
    proposed_end_at: datetime,
    expected_etag: str,
    operation: AdjustmentOperation | str = AdjustmentOperation.SHORTEN,
) -> ConfirmedExternalTimeChange:
    if _operation_value(operation) != AdjustmentOperation.SHORTEN.value:
        raise AdjustmentError("v0 supports only SHORTEN")
    try:
        return ConfirmedExternalTimeChange(
            external_event_id=external_event_id,
            original_start_at=original_start_at,
            original_end_at=original_end_at,
            proposed_start_at=proposed_start_at,
            proposed_end_at=proposed_end_at,
            expected_etag=expected_etag,
        )
    except ValueError as exc:
        raise AdjustmentError(str(exc)) from exc


def make_shorten_snapshot(event: Any, *, timezone: tzinfo) -> ProposalSnapshot:
    start = coerce_utc(_attr(event, "start_at"))
    end = coerce_utc(_attr(event, "end_at"))
    proposed_end = end - timedelta(minutes=SHORTEN_MINUTES)
    change = validate_shorten_change(
        external_event_id=str(_attr(event, "external_id")),
        original_start_at=start,
        original_end_at=end,
        proposed_start_at=start,
        proposed_end_at=proposed_end,
        expected_etag=str(_attr(event, "etag")),
    )
    return ProposalSnapshot(
        calendar_source=CalendarSource.GOOGLE,
        mirror_event_id=_attr(event, "id", _attr(event, "external_id")),
        external_event_id=change.external_event_id,
        operation=AdjustmentOperation.SHORTEN,
        original_start_at=change.original_start_at,
        original_end_at=change.original_end_at,
        proposed_start_at=change.proposed_start_at,
        proposed_end_at=change.proposed_end_at,
        expected_etag=change.expected_etag,
        protected_fingerprint=protected_event_fingerprint(event),
        dedup_key=proposal_dedup_key(
            event, timezone=timezone, operation=AdjustmentOperation.SHORTEN
        ),
        event_label=_attr(event, "summary", None),
        local_timezone=str(timezone),
        local_original_start_at=start.astimezone(timezone).isoformat(),
        local_original_end_at=end.astimezone(timezone).isoformat(),
        local_proposed_end_at=proposed_end.astimezone(timezone).isoformat(),
    )


def proposal_expiry(event_start_at: datetime, created_at: datetime) -> datetime:
    created = ensure_utc(created_at)
    start = ensure_utc(event_start_at)
    return min(created + HANDLE_TTL, start - START_SAFETY_LEAD)


def is_expired(proposal: StoredAdjustmentProposal, now: datetime) -> bool:
    now_utc = ensure_utc(now)
    return now_utc >= proposal.expires_at or now_utc >= (
        proposal.snapshot.original_start_at - START_SAFETY_LEAD
    )


def issue_reply_handle(
    secret: str, *, handle_factory: Callable[[], str] | None = None
) -> HandlePair:
    plaintext = handle_factory() if handle_factory is not None else secrets.token_urlsafe(32)
    return HandlePair(plaintext=plaintext, digest=digest_reply_handle(plaintext, secret))


def digest_reply_handle(handle: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), handle.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_reply_handle(handle: str, digest: str, secret: str) -> bool:
    return hmac.compare_digest(digest_reply_handle(handle, secret), digest)


def morning_dedup_key(local_date: date) -> str:
    return f"{MORNING_NUDGE_RULE_ID}:{local_date.isoformat()}"


def proposal_dedup_key(
    event: Any, *, timezone: tzinfo, operation: AdjustmentOperation | str
) -> str:
    start = coerce_utc(_attr(event, "start_at"))
    end = coerce_utc(_attr(event, "end_at"))
    proposed_end = end - timedelta(minutes=SHORTEN_MINUTES)
    parts = [
        start.astimezone(timezone).date().isoformat(),
        _source_value(_attr(event, "calendar_source", CalendarSource.GOOGLE)),
        str(_attr(event, "id", "")),
        str(_attr(event, "etag", "")),
        _operation_value(operation),
        start.isoformat(),
        proposed_end.isoformat(),
    ]
    return "calendar_adjustment:" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def protected_event_fingerprint(event: Any) -> str:
    fields = {
        "summary": _attr(event, "summary", None) or "",
        "organizer_self": bool(_attr(event, "organizer_self", False)),
        "has_attendees": bool(_attr(event, "has_attendees", False)),
        "is_recurring": bool(_attr(event, "is_recurring", False)),
        "event_type": _attr(event, "event_type", "default") or "default",
        "is_all_day": bool(_attr(event, "is_all_day", False)),
        "is_locked": bool(_attr(event, "is_locked", False)),
        "status": _attr(event, "status", None) or "",
    }
    material = "|".join(f"{key}={fields[key]}" for key in sorted(fields))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def snapshot_matches(snapshot: ProposalSnapshot, event: Any) -> bool:
    return (
        coerce_utc(_attr(event, "start_at")) == snapshot.original_start_at
        and coerce_utc(_attr(event, "end_at")) == snapshot.original_end_at
        and _attr(event, "etag", None) == snapshot.expected_etag
        and protected_event_fingerprint(event) == snapshot.protected_fingerprint
    )


def remote_matches_snapshot(snapshot: ProposalSnapshot, event: ExternalEvent) -> bool:
    return (
        event.external_id == snapshot.external_event_id
        and event.start_at == snapshot.proposed_start_at
        and event.end_at == snapshot.proposed_end_at
        and protected_event_fingerprint(event) == snapshot.protected_fingerprint
    )


def read_remote_event(
    writer: CalendarAdjustmentWriter, external_event_id: str
) -> ExternalEvent | None:
    try:
        return writer.read_event(external_event_id)
    except Exception:
        return None


def _http_status(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    return status if isinstance(status, int) else None


def redacted_receipt(
    *,
    status: AdjustmentStatus,
    provider_code: str | None = None,
    provider_event: ExternalEvent | None = None,
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "operation": AdjustmentOperation.SHORTEN.value,
        "delta_minutes": SHORTEN_MINUTES,
        "status": status.value,
    }
    if provider_code is not None:
        receipt["provider_code"] = provider_code
    if provider_event is not None and provider_event.etag:
        receipt["provider_result"] = "matched"
    return receipt


def initial_decision_tree(snapshot: ProposalSnapshot, facts: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": "calendar_adjustment_initial",
        "type": "rule",
        "label": "morning recovery calendar nudge",
        "detail": {
            "proposal_id": None,
            "operation": snapshot.operation.value,
            "delta_minutes": SHORTEN_MINUTES,
        },
        "children": [
            {"id": "evidence", "type": "input", "label": "health evidence", "detail": dict(facts)},
            {
                "id": "limitation",
                "type": "rule",
                "label": "technical eligibility only",
                "detail": {"confirmation_required": True},
            },
            {
                "id": "option_keep",
                "type": "option",
                "label": "keep event unchanged",
                "detail": {},
            },
            {
                "id": "option_shorten",
                "type": "option",
                "label": "shorten event by 30 minutes",
                "detail": {},
            },
            {
                "id": "action",
                "type": "action",
                "label": "ask user to confirm exact calendar change",
                "detail": {
                    "event_label": snapshot.event_label,
                    "timezone": snapshot.local_timezone,
                    "before": {
                        "start_at": snapshot.local_original_start_at,
                        "end_at": snapshot.local_original_end_at,
                    },
                    "after": {
                        "start_at": snapshot.local_original_start_at,
                        "end_at": snapshot.local_proposed_end_at,
                    },
                },
            },
        ],
    }


def no_action_decision_tree(reason: str, facts: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": "calendar_adjustment_no_action",
        "type": "rule",
        "label": "morning recovery calendar nudge skipped",
        "detail": {
            "outcome": "no_action",
            "reason": reason,
        },
        "children": [
            {
                "id": "evidence",
                "type": "input",
                "label": "redacted evaluation evidence",
                "detail": dict(facts),
            },
            {
                "id": "action",
                "type": "action",
                "label": "leave calendar unchanged",
                "detail": {"calendar_write": False},
            },
        ],
    }


def outcome_decision_tree(
    proposal: StoredAdjustmentProposal, receipt: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "id": "calendar_adjustment_outcome",
        "type": "action",
        "label": "calendar adjustment resolved",
        "detail": {
            "initial_decision_record_id": str(proposal.proposal_decision_record_id),
            "proposal_id": str(proposal.id),
            "receipt": dict(receipt),
        },
        "children": [],
    }


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _source_value(source: Any) -> str:
    return str(getattr(source, "value", source))


def _operation_value(operation: Any) -> str:
    return str(getattr(operation, "value", operation)).lower()


def _confidence(value: Any) -> int:
    return CONFIDENCE_RANK.get(str(value or "low").lower(), 0)


def _fresh_enough(
    block: Mapping[str, Any], *, local_date: date, now: datetime, freshness: timedelta
) -> bool:
    observed = _observed_at(block)
    if observed is None:
        observed_date = (
            block.get("date") or block.get("observed_date") or block.get("freshest_date")
        )
        if observed_date is None:
            entry_dates = [
                str(entry.get("observed_on"))
                for entry in block.get("entries", ())
                if isinstance(entry, Mapping) and entry.get("observed_on")
            ]
            observed_date = max(entry_dates, default=None)
        return str(observed_date) == local_date.isoformat()
    return ensure_utc(now) - observed <= freshness


def _observed_at(block: Mapping[str, Any]) -> datetime | None:
    for key in ("observed_at", "recorded_at", "freshest_at", "as_of"):
        value = block.get(key)
        if isinstance(value, datetime):
            return ensure_utc(value)
    return None


def _recovery_value(blocks: Sequence[Mapping[str, Any]]) -> float | None:
    for block in blocks:
        for key in ("recovery_value", "value", "score"):
            value = block.get(key)
            if isinstance(value, int | float):
                return float(value)
        for entry in block.get("entries", ()):
            if not isinstance(entry, Mapping):
                continue
            value = entry.get("value")
            category = entry.get("category")
            if isinstance(value, int | float) and isinstance(category, str):
                return normalize_recovery(
                    category,
                    str(entry.get("provider")) if entry.get("provider") is not None else None,
                    float(value),
                )
    return None


def _bucket_recovery(value: float) -> str:
    if value <= 20:
        return "very_low"
    if value <= 40:
        return "low"
    if value <= 70:
        return "medium"
    return "high"
