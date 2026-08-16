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
