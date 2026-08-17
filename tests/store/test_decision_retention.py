from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from healthmes.storage import (
    apply_decision_retention,
    run_storage_maintenance,
    update_retention_policy,
)
from healthmes.store import (
    DecisionKind,
    DecisionRecord,
    DecisionRequestReceipt,
    ProposalStatus,
    ScheduleProposal,
    Task,
    TriggerEvent,
)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _wellness_decision(
    *,
    created_at: datetime,
    trigger_event_id: uuid.UUID | None = None,
) -> DecisionRecord:
    return DecisionRecord(
        kind=DecisionKind.INSIGHT,
        tree={"id": "healthmes-decision", "children": []},
        summary="Compact wellness outcome",
        trigger_event_id=trigger_event_id,
        decision_request_id=uuid.uuid4(),
        decision_turn_id=uuid.uuid4(),
        decision_request_fingerprint=uuid.uuid4().hex * 2,
        decision_payload={
            "schema": "healthmes.decision-private.v2"
        },
        decision_payload_digest=uuid.uuid4().hex * 2,
        created_at=created_at,
    )


@pytest.mark.parametrize(
    ("preset", "retention_days"),
    (
        ("1d", 1),
        ("7d", 7),
        ("14d", 14),
        ("30d", 30),
        ("90d", 90),
        ("forever", None),
    ),
)
def test_all_decision_retention_presets_apply_only_to_wellness_records(
    session,
    preset: str,
    retention_days: int | None,
) -> None:
    basis = datetime(2026, 8, 16, 6, tzinfo=UTC)
    current = basis + timedelta(hours=1)
    wellness = _wellness_decision(created_at=basis)
    historical_non_wellness = DecisionRecord(
        kind=DecisionKind.SCHEDULE_CHANGE,
        tree={"id": "legacy", "children": []},
        summary="Historical non-wellness decision",
        created_at=basis,
    )
    session.add_all((wellness, historical_non_wellness))
    session.commit()

    update_retention_policy(
        session,
        "decision",
        preset,
        now=current,
    )
    session.commit()
    session.expire_all()

    retained = session.get(DecisionRecord, wellness.id)
    legacy = session.get(DecisionRecord, historical_non_wellness.id)
    assert retained is not None
    assert legacy is not None
    assert _as_utc(retained.retention_basis_at) == basis
    if retention_days is None:
        assert retained.expires_at is None
    else:
        assert _as_utc(retained.expires_at) == (
            basis + timedelta(days=retention_days)
        )
    assert legacy.retention_basis_at is None
    assert legacy.expires_at is None


def test_maintenance_deletes_exact_cutoff_and_preserves_related_rows(
    session,
    settings,
) -> None:
    current = datetime(2026, 8, 16, 12, tzinfo=UTC)
    update_retention_policy(
        session,
        "decision",
        "1d",
        now=current,
    )
    trigger = TriggerEvent(
        fired_at=current - timedelta(days=2),
        rule_id="decision-retention-test",
        payload={},
        alert_sent=False,
    )
    session.add(trigger)
    session.flush()
    at_cutoff = _wellness_decision(
        created_at=current - timedelta(days=1),
        trigger_event_id=trigger.id,
    )
    after_cutoff = _wellness_decision(
        created_at=(
            current - timedelta(days=1) + timedelta(microseconds=1)
        ),
    )
    historical_non_wellness = DecisionRecord(
        kind=DecisionKind.SCHEDULE_CHANGE,
        tree={"id": "legacy", "children": []},
        summary="Historical non-wellness decision",
        created_at=current - timedelta(days=30),
    )
    apply_decision_retention(
        session,
        at_cutoff,
        basis_at=current - timedelta(days=1),
    )
    apply_decision_retention(
        session,
        after_cutoff,
        basis_at=(
            current - timedelta(days=1) + timedelta(microseconds=1)
        ),
    )
    session.add_all(
        (at_cutoff, after_cutoff, historical_non_wellness)
    )
    session.flush()
    task = Task(title="Retain proposal after decision expiry")
    session.add(task)
    session.flush()
    proposal = ScheduleProposal(
        task_id=task.id,
        proposed_start=current + timedelta(hours=1),
        proposed_end=current + timedelta(hours=2),
        status=ProposalStatus.PROPOSED,
        decision_record_id=at_cutoff.id,
    )
    session.add(proposal)
    session.commit()
    at_cutoff_id = at_cutoff.id
    after_cutoff_id = after_cutoff.id
    legacy_id = historical_non_wellness.id
    trigger_id = trigger.id
    proposal_id = proposal.id

    preview = run_storage_maintenance(
        session,
        settings,
        dry_run=True,
        now=current,
    )
    session.commit()

    assert preview.decision_candidates == 1
    assert preview.decisions_deleted == 0
    assert session.get(DecisionRecord, at_cutoff_id) is not None

    report = run_storage_maintenance(
        session,
        settings,
        now=current,
    )
    session.commit()
    session.expire_all()

    assert report.decision_candidates == 1
    assert report.decisions_deleted == 1
    assert session.get(DecisionRecord, at_cutoff_id) is None
    assert session.get(DecisionRecord, after_cutoff_id) is not None
    assert session.get(DecisionRecord, legacy_id) is not None
    assert session.get(TriggerEvent, trigger_id) is not None
    retained_proposal = session.get(ScheduleProposal, proposal_id)
    assert retained_proposal is not None
    assert retained_proposal.decision_record_id is None
    assert session.scalar(
        sa.select(sa.func.count()).select_from(Task)
    ) == 1


def test_maintenance_bounds_decision_receipts_at_exact_cutoff(
    session,
    settings,
) -> None:
    current = datetime(2026, 8, 16, 12, tzinfo=UTC)
    expired = DecisionRequestReceipt(
        request_id=uuid.uuid4(),
        request_fingerprint="a" * 64,
        requested_at=current - timedelta(minutes=1),
        state="completed",
        result_payload={
            "schema": "healthmes.decision-receipt.v1",
            "result": {"status": "completed"},
        },
        expires_at=current,
    )
    future = DecisionRequestReceipt(
        request_id=uuid.uuid4(),
        request_fingerprint="b" * 64,
        requested_at=current - timedelta(minutes=1),
        state="completed",
        result_payload={
            "schema": "healthmes.decision-receipt.v1",
            "result": {"status": "completed"},
        },
        expires_at=current + timedelta(microseconds=1),
    )
    session.add_all((expired, future))
    session.commit()
    expired_id = expired.id
    future_id = future.id

    preview = run_storage_maintenance(
        session,
        settings,
        dry_run=True,
        now=current,
    )
    session.commit()

    assert preview.decision_receipt_candidates == 1
    assert preview.decision_receipts_deleted == 0
    assert session.get(DecisionRequestReceipt, expired_id) is not None

    report = run_storage_maintenance(
        session,
        settings,
        now=current,
    )
    session.commit()
    session.expire_all()

    assert report.decision_receipt_candidates == 1
    assert report.decision_receipts_deleted == 1
    assert session.get(DecisionRequestReceipt, expired_id) is None
    assert session.get(DecisionRequestReceipt, future_id) is not None


def test_maintenance_expires_trigger_answers_at_earliest_retention_cutoff(
    session,
    settings,
) -> None:
    current = datetime(2026, 8, 16, 12, tzinfo=UTC)
    update_retention_policy(
        session,
        "alert",
        "7d",
        now=current,
    )
    update_retention_policy(
        session,
        "decision",
        "1d",
        now=current,
    )

    decision_event = TriggerEvent(
        fired_at=current - timedelta(days=1),
        rule_id="proactive_focus",
        payload={
            "summary": "Focus was fragmented.",
            "message": "Take a short break.",
            "push": {"sent": True, "channel": "apns"},
        },
        alert_sent=True,
    )
    alert_event = TriggerEvent(
        fired_at=current - timedelta(days=7),
        rule_id="scheduled_briefing.morning",
        payload={
            "summary": "Morning briefing.",
            "message": "Keep the morning light.",
            "push": {
                "sent": False,
                "state": "app_available",
                "channel": "app_poll",
            },
        },
        alert_sent=False,
    )
    fresh_event = TriggerEvent(
        fired_at=(
            current
            - timedelta(days=7)
            + timedelta(microseconds=1)
        ),
        rule_id="scheduled_briefing.evening",
        payload={
            "summary": "Evening briefing.",
            "message": "Wind down gradually.",
            "push": {
                "sent": False,
                "state": "app_available",
                "channel": "app_poll",
            },
        },
        alert_sent=False,
    )
    session.add_all((decision_event, alert_event, fresh_event))
    session.flush()
    decision = _wellness_decision(
        created_at=current - timedelta(days=1),
        trigger_event_id=decision_event.id,
    )
    apply_decision_retention(
        session,
        decision,
        basis_at=current - timedelta(days=1),
    )
    session.add(decision)
    session.flush()
    decision_event.payload = {
        **decision_event.payload,
        "decision_record_id": str(decision.id),
    }
    session.commit()
    event_ids = (
        decision_event.id,
        alert_event.id,
        fresh_event.id,
    )

    preview = run_storage_maintenance(
        session,
        settings,
        dry_run=True,
        now=current,
    )
    session.commit()
    assert preview.decisions_deleted == 0
    assert all(
        "message" in session.get(TriggerEvent, event_id).payload
        for event_id in event_ids
    )

    run_storage_maintenance(
        session,
        settings,
        now=current,
    )
    session.commit()
    session.expire_all()

    expired_by_decision = session.get(
        TriggerEvent,
        decision_event.id,
    )
    expired_by_alert = session.get(TriggerEvent, alert_event.id)
    retained = session.get(TriggerEvent, fresh_event.id)
    assert expired_by_decision is not None
    assert expired_by_alert is not None
    assert retained is not None
    for event in (expired_by_decision, expired_by_alert):
        assert "message" not in event.payload
        assert "decision" not in event.payload
        assert "decision_record_id" not in event.payload
        assert event.payload["push"]["state"] == "expired"
    assert retained.payload["message"] == "Wind down gradually."


def test_retention_shrink_purges_recalculated_exact_cutoff(session) -> None:
    current = datetime(2026, 8, 16, 12, tzinfo=UTC)
    basis = current - timedelta(days=1)
    update_retention_policy(
        session,
        "decision",
        "forever",
        now=basis,
    )
    row = _wellness_decision(created_at=basis)
    apply_decision_retention(session, row, basis_at=basis)
    session.add(row)
    session.commit()
    row_id = row.id
    assert row.expires_at is None

    update_retention_policy(
        session,
        "decision",
        "1d",
        now=current,
    )
    session.commit()

    assert session.get(DecisionRecord, row_id) is None
