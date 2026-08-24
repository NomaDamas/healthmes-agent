from __future__ import annotations

import hmac
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from healthmes.calendars.adjustments_types import (
    APPLYING_RECONCILE_DELAY,
    MORNING_NUDGE_RULE_ID,
    AdjustmentStatus,
    ProposalSnapshot,
    StoredAdjustmentProposal,
)
from healthmes.calendars.base import (
    ExternalEvent,
    ensure_utc,
)


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

    def get_pending_proposal_by_handle_digest(
        self, handle_digest: str
    ) -> StoredAdjustmentProposal | None:
        matches = [
            proposal
            for proposal in self.proposals.values()
            if proposal.status == AdjustmentStatus.PENDING
            and hmac.compare_digest(proposal.reply_handle_digest, handle_digest)
        ]
        return matches[0] if len(matches) == 1 else None

    def commit_applying_boundary(self, proposal_id: uuid.UUID) -> StoredAdjustmentProposal | None:
        proposal = self.proposals.get(proposal_id)
        if proposal is not None:
            self.committed_applying.append(proposal_id)
        return proposal

    def update_mirror_after_apply(
        self, proposal: StoredAdjustmentProposal, event: ExternalEvent
    ) -> bool:
        return True

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
