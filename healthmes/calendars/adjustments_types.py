from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from healthmes.calendars.base import (
    CalendarError,
    ConfirmedExternalTimeChange,
    ExternalEvent,
)
from healthmes.store.enums import (
    CalendarMutationOperation,
    CalendarMutationStatus,
    CalendarSource,
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
    mirror_event_id: uuid.UUID | str | None
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


@dataclass(slots=True)  # noqa: MUTABLE_OK — in-memory CAS transitions mutate this stored record.
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
