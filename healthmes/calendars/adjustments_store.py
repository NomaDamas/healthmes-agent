from __future__ import annotations

# One SQL repository owns the complete atomic transition contract.
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from healthmes.activity.locking import lock_activity_write_plane
from healthmes.calendars.adjustments_types import (
    APPLYING_RECONCILE_DELAY,
    MORNING_NUDGE_RULE_ID,
    AdjustmentOperation,
    AdjustmentStatus,
    ProposalSnapshot,
    StoredAdjustmentProposal,
)
from healthmes.calendars.base import (
    ExternalEvent,
    coerce_utc,
    ensure_utc,
)
from healthmes.store.enums import (
    DecisionKind,
)
from healthmes.store.models import (
    CalendarEventMirror,
    CalendarMutationProposal,
    DecisionRecord,
    TriggerEvent,
)


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
        if self._session.get_bind().dialect.name == "postgresql":
            self._session.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:dedup_key, 0))"),
                {"dedup_key": dedup_key},
            )
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
            account_generation=snapshot.account_generation,
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

    def get_pending_proposal_by_handle_digest(
        self, handle_digest: str
    ) -> StoredAdjustmentProposal | None:
        proposals = list(
            self._session.scalars(
                sa.select(CalendarMutationProposal)
                .where(
                    CalendarMutationProposal.reply_handle_digest == handle_digest,
                    CalendarMutationProposal.status == AdjustmentStatus.PENDING,
                )
                .limit(2)
            )
        )
        return self._from_model(proposals[0]) if len(proposals) == 1 else None

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
            .execution_options(synchronize_session=False)
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
    ) -> bool:
        snapshot = proposal.snapshot
        if snapshot.mirror_event_id is None:
            return False
        result = self._session.execute(
            sa.update(CalendarEventMirror)
            .where(
                CalendarEventMirror.id == snapshot.mirror_event_id,
                CalendarEventMirror.calendar_source
                == snapshot.calendar_source,
                (
                    CalendarEventMirror.connection_generation.is_(None)
                    if snapshot.account_generation is None
                    else CalendarEventMirror.connection_generation
                    == snapshot.account_generation
                ),
                CalendarEventMirror.external_id
                == snapshot.external_event_id,
                CalendarEventMirror.start_at
                == snapshot.original_start_at,
                CalendarEventMirror.end_at == snapshot.original_end_at,
                CalendarEventMirror.etag == snapshot.expected_etag,
            )
            .values(
                end_at=event.end_at,
                etag=event.etag,
                updated_at=sa.func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        self._session.flush()
        if result.rowcount != 1:
            return False
        identity = self._session.get(
            CalendarEventMirror,
            snapshot.mirror_event_id,
        )
        if identity is not None:
            set_committed_value(identity, "end_at", event.end_at)
            set_committed_value(identity, "etag", event.etag)
        return True

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
            account_generation=row.account_generation,
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
        # The daily claim writes TriggerEvent and DecisionRecord rows. Acquire
        # the global write plane before SQLite's immediate transaction or the
        # PostgreSQL dedup advisory lock to preserve one canonical lock order.
        transaction_was_active = self._session.in_transaction()
        lock_activity_write_plane(self._session)
        bind = self._session.get_bind()
        if bind.dialect.name != "sqlite" or transaction_was_active:
            return
        self._session.execute(sa.text("BEGIN IMMEDIATE"))
        self.begin_immediate_attempted = True
