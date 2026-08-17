from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from healthmes.storage import (
    apply_decision_retention,
    purge_expired_decision_records,
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
        result_expires_at=current,
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
        result_expires_at=current + timedelta(microseconds=1),
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


def test_direct_decision_purge_scrubs_trigger_answer_and_dispatch_claim(
    session,
) -> None:
    current = datetime(2026, 8, 17, 12, tzinfo=UTC)
    basis = current - timedelta(days=1)
    update_retention_policy(
        session,
        "decision",
        "1d",
        now=current,
    )
    trigger = TriggerEvent(
        fired_at=basis,
        rule_id="direct-decision-purge",
        payload={
            "summary": "An expired proactive prompt.",
            "message": "Sensitive answer that must be scrubbed.",
            "decision": {"record_id": str(uuid.uuid4())},
            "push": {
                "sent": False,
                "state": "app_available",
                "channel": "app_poll",
            },
        },
        alert_sent=False,
        dispatch_owner_token=uuid.uuid4(),
        dispatch_generation=4,
        dispatch_lease_expires_at=current + timedelta(minutes=5),
    )
    session.add(trigger)
    session.flush()
    decision = _wellness_decision(
        created_at=basis,
        trigger_event_id=trigger.id,
    )
    apply_decision_retention(
        session,
        decision,
        basis_at=basis,
    )
    session.add(decision)
    session.flush()
    trigger.payload = {
        **trigger.payload,
        "decision": {"record_id": str(decision.id)},
        "decision_record_id": str(decision.id),
    }
    session.commit()
    trigger_id = trigger.id
    decision_id = decision.id

    assert purge_expired_decision_records(
        session,
        now=current,
    ) == 1
    session.commit()
    session.expire_all()

    assert session.get(DecisionRecord, decision_id) is None
    stored_trigger = session.get(TriggerEvent, trigger_id)
    assert stored_trigger is not None
    assert "message" not in stored_trigger.payload
    assert "decision" not in stored_trigger.payload
    assert "decision_record_id" not in stored_trigger.payload
    assert stored_trigger.payload["push"]["state"] == "expired"
    assert stored_trigger.dispatch_owner_token is None
    assert stored_trigger.dispatch_lease_expires_at is None
    assert stored_trigger.dispatch_generation == 4


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


def test_retention_shrink_scrubs_trigger_and_receipt_but_keeps_identity(
    session,
) -> None:
    current = datetime(2026, 8, 16, 12, tzinfo=UTC)
    basis = current - timedelta(days=1)
    update_retention_policy(
        session,
        "decision",
        "forever",
        now=basis,
    )
    trigger = TriggerEvent(
        fired_at=basis,
        rule_id="scheduled_briefing.morning",
        payload={
            "summary": "Morning briefing trigger.",
            "message": "Sensitive generated wellness answer.",
            "decision": {"record_id": str(uuid.uuid4())},
            "push": {
                "sent": False,
                "state": "app_available",
                "channel": "app_poll",
            },
        },
        alert_sent=False,
    )
    session.add(trigger)
    session.flush()
    decision = _wellness_decision(
        created_at=basis,
        trigger_event_id=trigger.id,
    )
    apply_decision_retention(session, decision, basis_at=basis)
    session.add(decision)
    session.flush()
    trigger.payload = {
        **trigger.payload,
        "decision": {"record_id": str(decision.id)},
        "decision_record_id": str(decision.id),
    }
    request_id = uuid.uuid4()
    fingerprint = "e" * 64
    identity_expires_at = basis + timedelta(days=30)
    receipt = DecisionRequestReceipt(
        request_id=request_id,
        request_fingerprint=fingerprint,
        requested_at=basis,
        state="completed",
        result_payload={
            "schema": "healthmes.decision-receipt.v1",
            "result": {"answer": "Sensitive generated wellness answer."},
        },
        result_expires_at=identity_expires_at,
        expires_at=identity_expires_at,
    )
    session.add(receipt)
    session.commit()
    trigger_id = trigger.id
    receipt_id = receipt.id

    update_retention_policy(
        session,
        "decision",
        "1d",
        now=current,
    )
    session.commit()
    session.expire_all()

    stored_trigger = session.get(TriggerEvent, trigger_id)
    stored_receipt = session.get(DecisionRequestReceipt, receipt_id)
    assert stored_trigger is not None
    assert "message" not in stored_trigger.payload
    assert "decision" not in stored_trigger.payload
    assert "decision_record_id" not in stored_trigger.payload
    assert stored_trigger.payload["push"]["state"] == "expired"
    assert stored_receipt is not None
    assert stored_receipt.state == "tombstone"
    assert stored_receipt.result_payload is None
    assert stored_receipt.result_expires_at is None
    assert stored_receipt.request_id == request_id
    assert stored_receipt.request_fingerprint == fingerprint
    assert _as_utc(stored_receipt.expires_at) == identity_expires_at


def test_retention_extension_cannot_revive_expired_receipt_result(
    session,
) -> None:
    requested_at = datetime(2026, 8, 1, 12, tzinfo=UTC)
    original_deadline = requested_at + timedelta(days=1)
    current = original_deadline + timedelta(hours=1)
    identity_expires_at = requested_at + timedelta(days=30)
    update_retention_policy(
        session,
        "decision",
        "1d",
        now=requested_at,
    )
    receipt = DecisionRequestReceipt(
        request_id=uuid.uuid4(),
        request_fingerprint="f" * 64,
        requested_at=requested_at,
        state="completed",
        result_payload={
            "schema": "healthmes.decision-receipt.v1",
            "result": {"answer": "Already expired sensitive answer."},
        },
        result_expires_at=original_deadline,
        expires_at=identity_expires_at,
    )
    session.add(receipt)
    session.commit()
    receipt_id = receipt.id

    update_retention_policy(
        session,
        "decision",
        "30d",
        now=current,
    )
    session.commit()
    session.expire_all()

    stored = session.get(DecisionRequestReceipt, receipt_id)
    assert stored is not None
    assert stored.state == "tombstone"
    assert stored.result_payload is None
    assert stored.result_expires_at is None
    assert _as_utc(stored.expires_at) == identity_expires_at
