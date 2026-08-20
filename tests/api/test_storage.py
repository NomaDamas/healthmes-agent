import os
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.activity.contracts import (
    ActivityBatchIn,
    ActivityCapability,
    ActivityPlatform,
    AppIntervalRecord,
)
from healthmes.activity.locking import (
    activity_write_lock as real_activity_write_lock,
)
from healthmes.activity.repository import DAY_SUMMARY_EVENT
from healthmes.activity.service import ingest_activity_batch
from healthmes.api import storage as storage_api_mod
from healthmes.backup import snapshot as snapshot_mod
from healthmes.backup.snapshot import (
    DataLocations,
    create_snapshot,
    restore_snapshot,
)
from healthmes.storage import (
    register_storage_object,
    run_storage_maintenance,
    update_retention_policy,
)
from healthmes.storage import service as storage_service
from healthmes.store import (
    CalendarEventMirror,
    CalendarSource,
    DecisionKind,
    DecisionRecord,
    PurgeJob,
    RetentionPolicy,
    StorageObject,
    StorageUsageDaily,
    Task,
    WellnessEvent,
    create_db_engine,
)


def test_storage_settings_bootstraps_defaults_and_measures_files(
    client: TestClient, settings
) -> None:
    raw = settings.data_dir / "raw_ingest" / "2026" / "08" / "05" / "sample.json"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"12345")

    response = client.get("/v1/storage/settings")

    assert response.status_code == 200
    body = response.json()
    assert body["data_dir"] == str(settings.data_dir.resolve())
    assert body["usage"]["raw_payload"] == {"bytes": 5, "objects": 1}
    assert body["usage_measurement"] == {
        "status": "current",
        "provider": "local",
        "measured_on": date.today().isoformat(),
        "deferred_reason": None,
    }
    policies = {row["data_class"]: row["preset"] for row in body["policies"]}
    assert policies["raw_payload"] == "14d"
    assert policies["media"] == "7d"
    assert policies["aggregate"] == "forever"
    assert policies["activity_raw"] == "14d"
    assert policies["activity_hourly"] == "90d"
    assert policies["activity_daily"] == "forever"
    assert policies["wearable_normalized"] == "30d"
    assert policies["alert"] == "7d"
    assert policies["decision"] == "forever"
    assert body["backup"]["provider"] == "local"
    assert body["backup"]["snapshot_count"] == 0
    assert body["backup"]["recovery_scope"] == "partial_component_snapshot"
    assert body["backup"]["full_node_recovery"] is False
    assert body["backup"]["open_wearables_runtime_configured"] is True
    assert body["backup"]["open_wearables_dump_configured"] is False
    assert "HEALTHMES_OW_DATABASE_URL is unset" in body["backup"][
        "operational_warning"
    ]
    assert body["backup"]["next_snapshot_scope"] == {
        "basis": "current_configuration_and_source_presence",
        "describes_latest_snapshot": False,
        "components": {
            "healthmes_db": "required",
            "open_wearables_db": "omitted_missing_dump_url",
            "media": "source_not_present",
            "raw_ingest": "included",
            "hermes_home": "not_configured",
        },
        "included_components": ["healthmes_db", "raw_ingest"],
        "omitted_components": [
            "open_wearables_db",
            "media",
            "hermes_home",
        ],
    }


def test_storage_settings_reports_prospective_open_wearables_dump_scope(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "HEALTHMES_OW_DATABASE_URL",
        "postgresql+psycopg://ow@localhost/open_wearables",
    )

    response = client.get("/v1/storage/settings")

    assert response.status_code == 200
    backup = response.json()["backup"]
    assert backup["open_wearables_runtime_configured"] is True
    assert backup["open_wearables_dump_configured"] is True
    assert backup["operational_warning"] is None
    assert backup["next_snapshot_scope"][
        "describes_latest_snapshot"
    ] is False
    assert backup["next_snapshot_scope"]["components"][
        "open_wearables_db"
    ] == "included"


def test_storage_settings_uses_only_latest_complete_usage_cohort_on_timeout(
    client: TestClient,
    session,
    monkeypatch,
) -> None:
    session.add_all(
        [
            StorageUsageDaily(
                measured_on=date(2026, 8, 17),
                provider="local",
                data_class="raw_payload",
                bytes_used=900,
                object_count=9,
            ),
            StorageUsageDaily(
                measured_on=date(2026, 8, 18),
                provider="local",
                data_class="media",
                bytes_used=120,
                object_count=3,
            ),
        ]
    )
    session.commit()

    def timeout(*_args, **_kwargs):
        raise TimeoutError("private filesystem detail")

    monkeypatch.setattr(storage_api_mod, "measure_usage", timeout)

    response = client.get("/v1/storage/settings")

    assert response.status_code == 200
    body = response.json()
    assert body["usage"] == {
        "media": {"bytes": 120, "objects": 3},
    }
    assert body["usage_measurement"] == {
        "status": "stale",
        "provider": "local",
        "measured_on": "2026-08-18",
        "deferred_reason": "timeout",
    }
    assert "private filesystem detail" not in response.text


def test_storage_settings_reports_unavailable_and_recreates_defaults_after_rollback(
    client: TestClient,
    session,
    monkeypatch,
) -> None:
    def denied(*_args, **_kwargs):
        raise PermissionError("private denied path")

    monkeypatch.setattr(storage_api_mod, "measure_usage", denied)

    response = client.get("/v1/storage/settings")

    assert response.status_code == 200
    assert response.json()["usage"] == {}
    assert response.json()["usage_measurement"] == {
        "status": "unavailable",
        "provider": "local",
        "measured_on": None,
        "deferred_reason": "permission",
    }
    assert "private denied path" not in response.text
    session.expire_all()
    raw_policy = session.scalar(
        select(RetentionPolicy).where(
            RetentionPolicy.data_class == "raw_payload"
        )
    )
    assert raw_policy is not None
    assert raw_policy.retention_days == 14


def test_storage_settings_does_not_hide_programming_errors(
    client: TestClient,
    monkeypatch,
) -> None:
    def fail(*_args, **_kwargs):
        raise RuntimeError("unexpected usage implementation failure")

    monkeypatch.setattr(storage_api_mod, "measure_usage", fail)

    with pytest.raises(
        RuntimeError,
        match="unexpected usage implementation failure",
    ):
        client.get("/v1/storage/settings")


def test_retention_update_is_persisted(client: TestClient, session) -> None:
    response = client.put(
        "/v1/storage/settings/raw_payload", json={"preset": "1d"}
    )
    assert response.status_code == 200
    assert response.json()["retention_days"] == 1

    session.expire_all()
    policy = session.scalar(
        select(RetentionPolicy).where(RetentionPolicy.data_class == "raw_payload")
    )
    assert policy is not None
    assert policy.retention_days == 1


def test_storage_maintenance_api_exposes_physical_cleanup_contract(
    client: TestClient,
    monkeypatch,
) -> None:
    report = SimpleNamespace(
        job_id="maintenance-contract",
        dry_run=True,
        candidates=3,
        records_purged=2,
        files_deleted=0,
        file_cleanup_pending=2,
        deleted=0,
        bytes_reclaimed=0,
        decision_candidates=1,
        decisions_deleted=0,
        decision_receipt_candidates=4,
        decision_receipts_deleted=0,
        budget_exhausted=False,
        budget_resource=None,
        budget_phase=None,
        errors=("raw_ingest/example.bin: cleanup pending",),
    )
    monkeypatch.setattr(
        storage_api_mod,
        "run_storage_maintenance",
        lambda *args, **kwargs: report,
    )

    response = client.post("/v1/storage/maintenance?dry_run=true")

    assert response.status_code == 200
    assert response.json() == {
        "job_id": "maintenance-contract",
        "dry_run": True,
        "candidates": 3,
        "records_purged": 2,
        "files_deleted": 0,
        "file_cleanup_pending": 2,
        "deleted": 0,
        "bytes_reclaimed": 0,
        "decision_candidates": 1,
        "decisions_deleted": 0,
        "decision_receipt_candidates": 4,
        "decision_receipts_deleted": 0,
        "budget_exhausted": False,
        "budget_resource": None,
        "budget_phase": None,
        "errors": ["raw_ingest/example.bin: cleanup pending"],
    }


def test_storage_maintenance_api_exposes_structured_budget_exhaustion(
    client: TestClient,
    monkeypatch,
) -> None:
    report = SimpleNamespace(
        job_id="maintenance-budget",
        dry_run=True,
        candidates=2,
        records_purged=0,
        files_deleted=0,
        file_cleanup_pending=1,
        deleted=0,
        bytes_reclaimed=0,
        decision_candidates=0,
        decisions_deleted=0,
        decision_receipt_candidates=0,
        decision_receipts_deleted=0,
        budget_exhausted=True,
        budget_resource="hash_bytes",
        budget_phase="unindexed payload discovery hash",
        errors=("storage maintenance budget exhausted",),
    )
    monkeypatch.setattr(
        storage_api_mod,
        "run_storage_maintenance",
        lambda *args, **kwargs: report,
    )

    response = client.post("/v1/storage/maintenance?dry_run=true")

    assert response.status_code == 200
    body = response.json()
    assert body["budget_exhausted"] is True
    assert body["budget_resource"] == "hash_bytes"
    assert body["budget_phase"] == "unindexed payload discovery hash"
    assert body["file_cleanup_pending"] == 1


def test_storage_maintenance_redacts_deferred_usage_failure(
    client: TestClient,
    monkeypatch,
) -> None:
    report = SimpleNamespace(
        job_id="maintenance-redaction",
        dry_run=False,
        candidates=0,
        records_purged=0,
        files_deleted=0,
        file_cleanup_pending=0,
        deleted=0,
        bytes_reclaimed=0,
        decision_candidates=0,
        decisions_deleted=0,
        decision_receipt_candidates=0,
        decision_receipts_deleted=0,
        budget_exhausted=False,
        budget_resource=None,
        budget_phase=None,
        errors=(),
    )
    monkeypatch.setattr(
        storage_api_mod,
        "run_storage_maintenance",
        lambda *args, **kwargs: report,
    )

    def fail_usage(*_args, **_kwargs):
        raise OSError("/private/secret-storage-path")

    monkeypatch.setattr(storage_api_mod, "measure_usage", fail_usage)

    response = client.post("/v1/storage/maintenance")

    assert response.status_code == 200
    assert response.json()["errors"] == [
        "storage usage measurement was deferred: io_error"
    ]
    assert "/private/secret-storage-path" not in response.text


def test_decision_retention_api_recalculates_wellness_record(
    client: TestClient,
    session,
    monkeypatch,
) -> None:
    current = datetime(2026, 8, 16, 12, tzinfo=UTC)
    basis = current - timedelta(hours=12)
    record = DecisionRecord(
        kind=DecisionKind.INSIGHT,
        tree={"id": "healthmes-decision", "children": []},
        summary="Compact wellness outcome",
        decision_request_id=uuid.uuid4(),
        decision_turn_id=uuid.uuid4(),
        decision_request_fingerprint="f" * 64,
        decision_payload={
            "schema": "healthmes.decision-private.v2"
        },
        decision_payload_digest="d" * 64,
        created_at=basis,
    )
    session.add(record)
    session.commit()
    monkeypatch.setattr(
        "healthmes.storage.service._now",
        lambda: current,
    )

    response = client.put(
        "/v1/storage/settings/decision",
        json={"preset": "1d"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "data_class": "decision",
        "preset": "1d",
        "retention_days": 1,
        "enabled": True,
    }
    session.expire_all()
    stored = session.get(DecisionRecord, record.id)
    assert stored is not None
    assert stored.retention_basis_at.replace(tzinfo=UTC) == basis
    assert stored.expires_at.replace(tzinfo=UTC) == (
        basis + timedelta(days=1)
    )


def test_calendar_retention_update_immediately_removes_expired_mirror_rows(
    session,
) -> None:
    current = datetime(2026, 8, 14, 12, tzinfo=UTC)
    intake_task = Task(title="Expired calendar intake")
    session.add(intake_task)
    session.flush()
    expired = CalendarEventMirror(
        external_id="expired-calendar-retention",
        calendar_source=CalendarSource.GOOGLE,
        start_at=current - timedelta(days=10, hours=1),
        end_at=current - timedelta(days=10),
        intake_task_id=intake_task.id,
    )
    retained = CalendarEventMirror(
        external_id="retained-calendar-retention",
        calendar_source=CalendarSource.GOOGLE,
        start_at=current - timedelta(hours=2),
        end_at=current - timedelta(hours=1),
    )
    session.add_all((expired, retained))
    session.commit()
    expired_id = expired.id
    retained_id = retained.id

    update_retention_policy(
        session,
        "calendar_mirror",
        "1d",
        now=current,
    )
    session.commit()

    assert session.get(CalendarEventMirror, expired_id) is None
    assert session.get(CalendarEventMirror, retained_id) is retained
    assert session.get(Task, intake_task.id).status == "cancelled"


def test_calendar_maintenance_retires_expired_intake_task(
    session,
    settings,
) -> None:
    current = datetime(2026, 8, 14, 12, tzinfo=UTC)
    intake_task = Task(title="Maintenance-expired calendar intake")
    session.add(intake_task)
    session.flush()
    expired = CalendarEventMirror(
        external_id="maintenance-expired-calendar-retention",
        calendar_source=CalendarSource.GOOGLE,
        start_at=current - timedelta(days=100, hours=1),
        end_at=current - timedelta(days=100),
        intake_task_id=intake_task.id,
    )
    session.add(expired)
    session.commit()
    expired_id = expired.id
    task_id = intake_task.id

    run_storage_maintenance(session, settings, now=current)
    session.commit()

    assert session.get(CalendarEventMirror, expired_id) is None
    assert session.get(Task, task_id).status == "cancelled"


def test_disabled_calendar_retention_does_not_delete_mirror_rows(
    session,
    settings,
) -> None:
    current = datetime(2026, 8, 14, 12, tzinfo=UTC)
    update_retention_policy(
        session,
        "calendar_mirror",
        "1d",
        now=current,
    )
    expired = CalendarEventMirror(
        external_id="disabled-calendar-retention",
        calendar_source=CalendarSource.CALDAV,
        start_at=current - timedelta(days=100, hours=1),
        end_at=current - timedelta(days=100),
    )
    session.add(expired)
    policy = session.scalar(
        select(RetentionPolicy).where(
            RetentionPolicy.data_class == "calendar_mirror"
        )
    )
    assert policy is not None
    policy.enabled = False
    session.add(expired)
    session.commit()

    run_storage_maintenance(session, settings, now=current)
    session.commit()

    assert session.get(CalendarEventMirror, expired.id) is expired


def test_daily_retention_update_immediately_refreshes_rest_baseline(
    client: TestClient,
    session,
    monkeypatch,
) -> None:
    current = datetime.now(UTC)
    target_day = current.date() - timedelta(days=6)
    source_days = [
        target_day - timedelta(days=offset)
        for offset in range(3, -1, -1)
    ]
    for day in source_days:
        start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        ingest_activity_batch(
            session,
            ActivityBatchIn(
                source_provider="retention-rest-test",
                source_device="desktop-retention-rest",
                platform=ActivityPlatform.MACOS,
                capability=ActivityCapability.DETAILED,
                timezone="UTC",
                records=[
                    AppIntervalRecord(
                        source_record_id=f"retention-rest-{day.isoformat()}",
                        start_at=start,
                        end_at=start + timedelta(hours=12),
                        state="active",
                        app_id="editor",
                    )
                ],
            ),
            now=start + timedelta(hours=13),
        )
    session.commit()

    target = next(
        row
        for row in session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == DAY_SUMMARY_EVENT
            )
        )
        if row.payload.get("date") == target_day.isoformat()
    )
    assert target.payload["seven_day_baseline_delta"]["status"] == "ok"
    monkeypatch.setattr(
        "healthmes.storage.service._now",
        lambda: current,
    )

    updated = client.put(
        "/v1/storage/settings/activity_daily",
        json={"preset": "7d"},
    )
    summary = client.get(
        "/v1/activity/summary",
        params={"date": target_day.isoformat(), "timezone": "UTC"},
    )

    assert updated.status_code == 200
    assert summary.status_code == 200
    assert summary.json()["seven_day_baseline_delta"] == {
        "status": "insufficient_data",
        "days_with_data": 0,
        "required_days": 3,
        "lookback_days": 7,
    }
    session.expire_all()
    remaining_days = {
        row.payload.get("date")
        for row in session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == DAY_SUMMARY_EVENT
            )
        )
    }
    assert remaining_days == {target_day.isoformat()}


def test_retention_update_uses_the_original_object_observation_time(
    client: TestClient,
    session,
    settings,
) -> None:
    observed = datetime(2026, 7, 1, tzinfo=UTC)
    obj = register_storage_object(
        session,
        settings,
        relative_path="media/private.m4a",
        data_class="nutrition_media",
        content_type="audio/m4a",
        size_bytes=7,
        observed_at=observed,
    )
    session.commit()

    response = client.put(
        "/v1/storage/settings/nutrition_media",
        json={"preset": "1d"},
    )

    assert response.status_code == 200
    session.expire_all()
    stored = session.get(StorageObject, obj.id)
    assert stored is not None
    assert stored.retention_basis_at.replace(tzinfo=UTC) == observed
    assert stored.expires_at.replace(tzinfo=UTC) == observed + timedelta(days=7)


def test_register_storage_object_rejects_conflicting_payload_for_existing_path(
    session,
    settings,
) -> None:
    register_storage_object(
        session,
        settings,
        relative_path="media/immutable.jpg",
        data_class="media",
        content_type="image/jpeg",
        size_bytes=10,
        sha256="a" * 64,
        observed_at=datetime(2026, 8, 18, tzinfo=UTC),
    )
    session.commit()

    with pytest.raises(ValueError, match="different payload"):
        register_storage_object(
            session,
            settings,
            relative_path="media/immutable.jpg",
            data_class="nutrition_media",
            content_type="image/jpeg",
            size_bytes=11,
            sha256="b" * 64,
            observed_at=datetime(2026, 8, 18, tzinfo=UTC),
        )


def test_wellness_event_contract_sets_expiry_and_is_idempotent(
    client: TestClient, session
) -> None:
    observed = datetime(2026, 8, 1, 9, tzinfo=UTC)
    payload = {
        "event_type": "subjective_energy",
        "observed_at": observed.isoformat(),
        "source_provider": "manual",
        "source_record_id": "energy-1",
        "data_class": "normalized",
        "payload": {"score": 4},
    }
    first = client.post("/v1/wellness-events", json=payload)
    second = client.post("/v1/wellness-events", json=payload)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    expires = datetime.fromisoformat(first.json()["expires_at"]).replace(tzinfo=UTC)
    assert expires == observed + timedelta(days=30)
    assert len(list(session.scalars(select(WellnessEvent)))) == 1


@pytest.mark.parametrize(
    ("event_type", "source_provider"),
    (
        ("subjective_energy", "nutrition-operation"),
        ("subjective_energy", "nutrition-outcome-raw"),
        ("subjective_energy", "nutrition-future-internal"),
        ("nutrition.interaction.v1", "manual"),
        ("activity.app-hour.v1", "manual"),
        ("subjective_energy", "activitywatch"),
        ("subjective_energy", "healthmes-activity-aggregator"),
        ("subjective_energy", "ActivityWatch"),
        ("subjective_energy", "HEALTHMES-ACTIVITY-AGGREGATOR"),
        ("subjective_energy", "healthmes-activity-deletion"),
    ),
)
def test_generic_wellness_api_rejects_internal_domain_namespaces(
    client: TestClient,
    session,
    event_type: str,
    source_provider: str,
) -> None:
    response = client.post(
        "/v1/wellness-events",
        json={
            "event_type": event_type,
            "observed_at": datetime.now(UTC).isoformat(),
            "source_provider": source_provider,
            "source_record_id": "reserved-namespace-attempt",
            "data_class": "normalized",
            "payload": {"note": "forged internal payload"},
        },
    )

    assert response.status_code == 422
    assert (
        response.json()["error"]["code"]
        == "reserved_wellness_namespace"
    )
    assert session.scalar(select(WellnessEvent)) is None


def test_maintenance_dry_run_then_deletes_expired_object(
    session, settings
) -> None:
    target = settings.data_dir / "raw_ingest" / "old.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"expired")
    obj = register_storage_object(
        session,
        settings,
        relative_path="raw_ingest/old.bin",
        data_class="raw_payload",
        content_type="application/octet-stream",
        size_bytes=7,
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    session.commit()

    preview = run_storage_maintenance(
        session, settings, dry_run=True, now=datetime(2026, 8, 5, tzinfo=UTC)
    )
    session.commit()
    assert preview.candidates == 1
    assert preview.records_purged == 0
    assert preview.files_deleted == 0
    assert preview.file_cleanup_pending == 0
    assert preview.deleted == 0
    assert target.exists()

    result = run_storage_maintenance(
        session, settings, now=datetime(2026, 8, 5, tzinfo=UTC)
    )
    session.commit()
    session.refresh(obj)
    assert result.records_purged == 1
    assert result.files_deleted == 1
    assert result.file_cleanup_pending == 0
    assert result.deleted == 1
    assert result.bytes_reclaimed == 7
    assert not target.exists()
    assert obj.purged_at is not None
    assert obj.file_cleanup_completed_at is not None
    assert len(list(session.scalars(select(PurgeJob)))) == 2

    follow_up = run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 6, tzinfo=UTC),
    )
    follow_up_job = session.get(PurgeJob, uuid.UUID(follow_up.job_id))
    assert follow_up.bytes_reclaimed == 0
    assert follow_up.records_purged == 0
    assert follow_up.files_deleted == 0
    assert follow_up.file_cleanup_pending == 0
    assert follow_up_job is not None
    assert follow_up_job.detail["file_cleanup_candidates"] == 0


def test_maintenance_commit_failure_keeps_database_and_file(
    session,
    engine,
    settings,
    monkeypatch,
) -> None:
    target = settings.data_dir / "raw_ingest" / "commit-failure.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"must survive")
    obj = register_storage_object(
        session,
        settings,
        relative_path="raw_ingest/commit-failure.bin",
        data_class="raw_payload",
        content_type="application/octet-stream",
        size_bytes=12,
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    object_id = obj.id
    session.commit()
    real_commit = Session.commit

    def fail_commit(_session):
        raise OSError("injected maintenance commit failure")

    monkeypatch.setattr(Session, "commit", fail_commit)
    with pytest.raises(OSError, match="maintenance commit failure"):
        run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 5, tzinfo=UTC),
        )

    assert target.read_bytes() == b"must survive"
    with Session(engine) as verification:
        stored = verification.get(StorageObject, object_id)
        assert stored is not None
        assert stored.purged_at is None
    monkeypatch.setattr(Session, "commit", real_commit)


def test_maintenance_commit_acknowledgement_loss_is_retried_without_data_loss(
    session,
    engine,
    settings,
    monkeypatch,
) -> None:
    target = settings.data_dir / "raw_ingest" / "ambiguous-commit.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"retry after ambiguous commit")
    obj = register_storage_object(
        session,
        settings,
        relative_path="raw_ingest/ambiguous-commit.bin",
        data_class="raw_payload",
        content_type="application/octet-stream",
        size_bytes=28,
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    object_id = obj.id
    session.commit()
    real_commit = Session.commit

    def commit_then_raise(current_session):
        real_commit(current_session)
        assert target.exists()
        raise OSError("injected lost maintenance commit acknowledgement")

    monkeypatch.setattr(Session, "commit", commit_then_raise)
    with pytest.raises(OSError, match="lost maintenance commit acknowledgement"):
        run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 5, tzinfo=UTC),
        )

    assert target.exists()
    with Session(engine) as verification:
        stored = verification.get(StorageObject, object_id)
        assert stored is not None
        assert stored.purged_at is not None
        assert stored.file_cleanup_completed_at is None
        pending_jobs = list(
            verification.scalars(
                select(PurgeJob).where(
                    PurgeJob.status == "pending_file_cleanup"
                )
            )
        )
        assert len(pending_jobs) == 1
        stranded_job_id = pending_jobs[0].id

    monkeypatch.setattr(Session, "commit", real_commit)
    retry = run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 6, tzinfo=UTC),
    )
    assert retry.candidates == 0
    assert retry.records_purged == 0
    assert retry.files_deleted == 1
    assert retry.file_cleanup_pending == 0
    assert retry.deleted == retry.files_deleted
    assert retry.bytes_reclaimed == 28
    assert not target.exists()
    session.refresh(obj)
    assert obj.file_cleanup_completed_at is not None
    with Session(engine) as verification:
        stranded_job = verification.get(PurgeJob, stranded_job_id)
        assert stranded_job is not None
        assert stranded_job.status == "completed"
        assert stranded_job.detail["file_cleanup_pending"] == 0
        assert (
            stranded_job.detail["file_cleanup_recovered_by_job_id"]
            == retry.job_id
        )


@pytest.mark.parametrize(
    ("commit_before_raise", "expected_retry_candidates"),
    ((False, 1), (True, 0)),
)
def test_maintenance_file_cleanup_commit_failure_is_recoverable(
    session,
    engine,
    settings,
    monkeypatch,
    commit_before_raise,
    expected_retry_candidates,
) -> None:
    target = settings.data_dir / "raw_ingest" / (
        f"cleanup-commit-{int(commit_before_raise)}.bin"
    )
    target.parent.mkdir(parents=True)
    target.write_bytes(b"cleanup commit recovery")
    obj = register_storage_object(
        session,
        settings,
        relative_path=target.relative_to(settings.data_dir).as_posix(),
        data_class="raw_payload",
        content_type="application/octet-stream",
        size_bytes=23,
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    object_id = obj.id
    session.commit()
    real_commit = Session.commit
    commit_calls = 0

    def fail_cleanup_commit(current_session):
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 2:
            if commit_before_raise:
                real_commit(current_session)
            raise OSError("injected file cleanup commit failure")
        real_commit(current_session)

    monkeypatch.setattr(Session, "commit", fail_cleanup_commit)
    with pytest.raises(OSError, match="file cleanup commit failure"):
        run_storage_maintenance(
            session,
            settings,
            now=datetime(2026, 8, 5, tzinfo=UTC),
        )

    assert not target.exists()
    with Session(engine) as verification:
        stored = verification.get(StorageObject, object_id)
        assert stored is not None
        assert stored.purged_at is not None
        assert (
            stored.file_cleanup_completed_at is not None
        ) is commit_before_raise

    journal_dir = (
        settings.data_dir
        / storage_service._CLEANUP_JOURNAL_DIRECTORY
    )
    journals = list(journal_dir.glob(f"*{object_id.hex}-*.json"))
    assert {path.name.rsplit("-", 1)[-1] for path in journals} == {
        "intent.json",
        "progress.json",
        "complete.json",
    }

    monkeypatch.setattr(Session, "commit", real_commit)
    retry = run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 6, tzinfo=UTC),
    )
    retry_job = session.get(PurgeJob, uuid.UUID(retry.job_id))
    assert retry.bytes_reclaimed == 0
    assert retry_job is not None
    assert (
        retry_job.detail["file_cleanup_candidates"]
        == expected_retry_candidates
    )
    with Session(engine) as verification:
        stored = verification.get(StorageObject, object_id)
        assert stored is not None
        assert stored.file_cleanup_completed_at is not None
    assert list(journal_dir.glob(f"*{object_id.hex}-*.json")) == []


def test_maintenance_retries_file_unlink_after_database_purge(
    session,
    settings,
    monkeypatch,
) -> None:
    target = settings.data_dir / "raw_ingest" / "unlink-retry.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"unlink later")
    obj = register_storage_object(
        session,
        settings,
        relative_path="raw_ingest/unlink-retry.bin",
        data_class="raw_payload",
        content_type="application/octet-stream",
        size_bytes=12,
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    session.commit()
    real_unlink = storage_service._unlink_cleanup_entry
    failed = False

    def fail_once(parent):
        nonlocal failed
        if (
            parent.name == storage_service._CLEANUP_QUARANTINE_ENTRY
            and not failed
        ):
            failed = True
            raise OSError("injected unlink failure")
        return real_unlink(parent)

    monkeypatch.setattr(
        storage_service,
        "_unlink_cleanup_entry",
        fail_once,
    )
    first = run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )
    assert first.records_purged == 1
    assert first.files_deleted == 0
    assert first.file_cleanup_pending == 1
    assert first.deleted == first.files_deleted
    assert first.bytes_reclaimed == 0
    assert first.errors == ("raw_ingest/unlink-retry.bin: injected unlink failure",)
    assert not target.exists()
    prefix = storage_service._cleanup_quarantine_prefix(target.name)
    assert len(list(target.parent.glob(f"{prefix}*"))) == 1
    session.refresh(obj)
    assert obj.file_cleanup_completed_at is None

    monkeypatch.setattr(
        storage_service,
        "_unlink_cleanup_entry",
        real_unlink,
    )
    second = run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 6, tzinfo=UTC),
    )
    assert second.candidates == 0
    assert second.records_purged == 0
    assert second.files_deleted == 1
    assert second.file_cleanup_pending == 0
    assert second.deleted == second.files_deleted
    assert second.bytes_reclaimed == 12
    assert second.errors == ()
    assert not target.exists()
    assert list(target.parent.glob(f"{prefix}*")) == []
    session.refresh(obj)
    assert obj.file_cleanup_completed_at is not None


def test_maintenance_reports_malformed_cleanup_identity_as_pending(
    session,
    settings,
) -> None:
    target = settings.data_dir / "raw_ingest" / "malformed-cleanup.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"pending malformed cleanup")
    obj = register_storage_object(
        session,
        settings,
        relative_path="raw_ingest/malformed-cleanup.bin",
        data_class="raw_payload",
        content_type="application/octet-stream",
        size_bytes=25,
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    obj.purged_at = datetime(2026, 8, 5, tzinfo=UTC)
    obj.file_cleanup_identity = {"version": 2, "kind": "regular"}
    session.commit()

    preview = run_storage_maintenance(
        session,
        settings,
        dry_run=True,
        now=datetime(2026, 8, 6, tzinfo=UTC),
    )
    assert preview.file_cleanup_pending == 1
    assert preview.files_deleted == 0
    assert target.exists()

    result = run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 6, tzinfo=UTC),
    )
    job = session.get(PurgeJob, uuid.UUID(result.job_id))

    assert result.file_cleanup_pending == 1
    assert result.files_deleted == 0
    assert len(result.errors) == 1
    assert "invalid file cleanup identity field" in result.errors[0]
    assert job is not None
    assert job.status == "pending_file_cleanup"
    assert job.detail["file_cleanup_pending"] == 1
    assert job.detail["file_cleanup_object_ids"] == [str(obj.id)]
    assert target.read_bytes() == b"pending malformed cleanup"


def test_cleanup_quarantine_is_not_rediscovered_or_measured(
    session,
    settings,
    monkeypatch,
) -> None:
    payload = b"quarantined deletion retry"
    target = settings.data_dir / "raw_ingest" / "quarantine-scan.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    obj = register_storage_object(
        session,
        settings,
        relative_path="raw_ingest/quarantine-scan.bin",
        data_class="raw_payload",
        content_type="application/octet-stream",
        size_bytes=len(payload),
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    session.commit()
    real_unlink = storage_service._unlink_cleanup_entry

    def fail_payload_unlink(parent):
        if parent.name == storage_service._CLEANUP_QUARANTINE_ENTRY:
            raise OSError("injected persistent unlink failure")
        return real_unlink(parent)

    monkeypatch.setattr(
        storage_service,
        "_unlink_cleanup_entry",
        fail_payload_unlink,
    )

    result = run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )

    assert result.errors == (
        "raw_ingest/quarantine-scan.bin: injected persistent unlink failure",
    )
    prefix = storage_service._cleanup_quarantine_prefix(target.name)
    quarantine = next(target.parent.glob(f"{prefix}*"))
    quarantined_payload = (
        quarantine / storage_service._CLEANUP_QUARANTINE_ENTRY
    )
    quarantined_relative = quarantined_payload.relative_to(
        settings.data_dir
    ).as_posix()
    assert quarantined_payload.read_bytes() == payload

    storage_service._discover_unindexed(session, settings)
    usage = storage_service.measure_usage(session, settings)
    session.flush()

    assert session.scalar(
        select(StorageObject).where(
            StorageObject.relative_path == quarantined_relative
        )
    ) is None
    assert usage.get("raw_payload", {"bytes": 0, "objects": 0}) == {
        "bytes": 0,
        "objects": 0,
    }
    session.refresh(obj)
    assert obj.purged_at is not None
    assert obj.file_cleanup_completed_at is None


def test_cleanup_retry_preserves_recreated_file_at_same_path(
    session,
    settings,
    monkeypatch,
) -> None:
    target = settings.data_dir / "raw_ingest" / "recreated.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old generation")
    obj = register_storage_object(
        session,
        settings,
        relative_path="raw_ingest/recreated.bin",
        data_class="raw_payload",
        content_type="application/octet-stream",
        size_bytes=len(b"old generation"),
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    session.commit()
    real_unlink = storage_service._unlink_cleanup_entry
    failed = False

    def fail_once(parent):
        nonlocal failed
        if (
            parent.name == storage_service._CLEANUP_QUARANTINE_ENTRY
            and not failed
        ):
            failed = True
            raise OSError("injected first cleanup failure")
        return real_unlink(parent)

    monkeypatch.setattr(
        storage_service,
        "_unlink_cleanup_entry",
        fail_once,
    )
    first = run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )
    assert first.errors == (
        "raw_ingest/recreated.bin: injected first cleanup failure",
    )
    session.refresh(obj)
    assert obj.purged_at is not None
    assert obj.file_cleanup_identity is not None
    assert obj.file_cleanup_completed_at is None
    assert not target.exists()

    monkeypatch.setattr(
        storage_service,
        "_unlink_cleanup_entry",
        real_unlink,
    )
    target.write_bytes(b"new generation must survive")
    second = run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 6, tzinfo=UTC),
    )

    assert target.read_bytes() == b"new generation must survive"
    assert second.bytes_reclaimed == len(b"old generation")
    assert second.errors == ()
    session.refresh(obj)
    assert obj.file_cleanup_completed_at is not None


def test_cleanup_preserves_replacement_raced_after_identity_verification(
    session,
    settings,
    monkeypatch,
) -> None:
    target = settings.data_dir / "raw_ingest" / "verify-race.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"verified old generation")
    replacement = b"replacement inserted after verification"
    obj = register_storage_object(
        session,
        settings,
        relative_path="raw_ingest/verify-race.bin",
        data_class="raw_payload",
        content_type="application/octet-stream",
        size_bytes=len(b"verified old generation"),
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    session.commit()
    real_quarantine = storage_service._quarantine_cleanup_entry
    raced = False

    def replace_then_quarantine(parent, quarantine, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            target.unlink()
            target.write_bytes(replacement)
        return real_quarantine(parent, quarantine, **kwargs)

    monkeypatch.setattr(
        storage_service,
        "_quarantine_cleanup_entry",
        replace_then_quarantine,
    )

    result = run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )

    assert raced is True
    assert target.read_bytes() == replacement
    assert result.bytes_reclaimed == 0
    assert len(result.errors) == 1
    assert "file identity changed after verification" in result.errors[0]
    prefix = storage_service._cleanup_quarantine_prefix(target.name)
    assert list(target.parent.glob(f"{prefix}*")) == []
    session.refresh(obj)
    assert obj.file_cleanup_completed_at is None


@pytest.mark.skipif(
    os.name == "nt",
    reason="symlink creation requires Windows privileges",
)
def test_cleanup_unlinks_final_symlink_without_touching_target(
    session,
    settings,
) -> None:
    victim = settings.data_dir / "raw_ingest" / "victim.bin"
    link = settings.data_dir / "raw_ingest" / "link.bin"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"victim survives")
    link.symlink_to(victim)
    obj = register_storage_object(
        session,
        settings,
        relative_path="raw_ingest/link.bin",
        data_class="raw_payload",
        content_type="application/octet-stream",
        size_bytes=link.lstat().st_size,
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    session.commit()

    result = run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )

    assert result.errors == ()
    assert not link.exists() and not link.is_symlink()
    assert victim.read_bytes() == b"victim survives"
    session.refresh(obj)
    assert obj.file_cleanup_identity["kind"] == "symlink"
    assert obj.file_cleanup_completed_at is not None


@pytest.mark.skipif(
    os.name == "nt",
    reason="symlink creation requires Windows privileges",
)
def test_cleanup_unlinks_dangling_final_symlink(
    session,
    settings,
) -> None:
    link = settings.data_dir / "raw_ingest" / "dangling.bin"
    link.parent.mkdir(parents=True)
    link.symlink_to(settings.data_dir / "missing-target.bin")
    obj = register_storage_object(
        session,
        settings,
        relative_path="raw_ingest/dangling.bin",
        data_class="raw_payload",
        content_type="application/octet-stream",
        size_bytes=link.lstat().st_size,
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    session.commit()

    result = run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )

    assert result.errors == ()
    assert not link.is_symlink()
    session.refresh(obj)
    assert obj.file_cleanup_completed_at is not None


@pytest.mark.skipif(
    os.name == "nt",
    reason="symlink creation requires Windows privileges",
)
def test_cleanup_rejects_symlinked_parent_directory(
    session,
    settings,
    tmp_path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.bin"
    victim.write_bytes(b"outside survives")
    linked_parent = settings.data_dir / "linked-parent"
    linked_parent.parent.mkdir(parents=True, exist_ok=True)
    linked_parent.symlink_to(outside, target_is_directory=True)
    obj = register_storage_object(
        session,
        settings,
        relative_path="linked-parent/victim.bin",
        data_class="raw_payload",
        content_type="application/octet-stream",
        size_bytes=len(b"outside survives"),
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    session.commit()

    result = run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )

    assert victim.read_bytes() == b"outside survives"
    assert result.deleted == 0
    assert len(result.errors) == 1
    session.refresh(obj)
    assert obj.purged_at is None


def test_cleanup_fsync_failure_remains_retryable(
    session,
    settings,
    monkeypatch,
) -> None:
    target = settings.data_dir / "raw_ingest" / "fsync-retry.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"fsync retry")
    obj = register_storage_object(
        session,
        settings,
        relative_path="raw_ingest/fsync-retry.bin",
        data_class="raw_payload",
        content_type="application/octet-stream",
        size_bytes=len(b"fsync retry"),
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    session.commit()
    real_fsync = storage_service._fsync_cleanup_parent
    failed = False

    def fail_once(parent):
        nonlocal failed
        entry = parent.path / parent.name
        if (
            not failed
            and parent.name == storage_service._CLEANUP_QUARANTINE_ENTRY
            and not entry.exists()
        ):
            failed = True
            raise OSError("injected cleanup fsync failure")
        return real_fsync(parent)

    monkeypatch.setattr(
        storage_service,
        "_fsync_cleanup_parent",
        fail_once,
    )
    first = run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )
    assert not target.exists()
    assert first.bytes_reclaimed == 0
    assert first.errors == (
        "raw_ingest/fsync-retry.bin: injected cleanup fsync failure",
    )
    session.refresh(obj)
    assert obj.file_cleanup_completed_at is None

    monkeypatch.setattr(
        storage_service,
        "_fsync_cleanup_parent",
        real_fsync,
    )
    second = run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 6, tzinfo=UTC),
    )
    assert second.file_cleanup_pending == 1
    assert len(second.errors) == 1
    assert "no remaining HealthMes-owned name" in second.errors[0]
    assert second.bytes_reclaimed == 0
    session.refresh(obj)
    assert obj.file_cleanup_identity["manual_review_required"] == (
        "cleanup_outcome_unproven"
    )
    assert obj.file_cleanup_completed_at is None


def test_retention_and_maintenance_enter_the_activity_write_lock(
    session,
    settings,
    monkeypatch,
) -> None:
    transitions: list[str] = []

    @contextmanager
    def tracked_lock():
        transitions.append("enter")
        try:
            yield
        finally:
            transitions.append("exit")

    monkeypatch.setattr(
        "healthmes.storage.service.activity_write_lock",
        tracked_lock,
    )

    update_retention_policy(session, "activity_raw", "14d")
    session.commit()
    run_storage_maintenance(session, settings, dry_run=True)

    assert transitions == ["enter", "exit", "enter", "exit"]


def test_snapshot_fence_preserves_pre_retention_database_and_media_generation(
    engine,
    session,
    settings,
    monkeypatch,
    tmp_path,
) -> None:
    current = datetime(2026, 8, 17, 12, tzinfo=UTC)
    payload = b"media retained by the captured generation"
    relative_path = "media/fence/retained.bin"
    media_path = settings.data_dir / relative_path
    media_path.parent.mkdir(parents=True)
    media_path.write_bytes(payload)
    storage = StorageObject(
        data_class="media",
        relative_path=relative_path,
        content_type="application/octet-stream",
        size_bytes=len(payload),
        sha256=sha256(payload).hexdigest(),
        retention_basis_at=current - timedelta(days=30),
        expires_at=current - timedelta(days=1),
        safe_to_purge=True,
    )
    session.add(storage)
    session.commit()
    storage_id = storage.id

    live_settings = settings.model_copy(
        update={"database_url": str(engine.url)}
    )
    locations = DataLocations(
        database_url=str(engine.url),
        media_dir=live_settings.data_dir / "media",
        raw_ingest_dir=live_settings.data_dir / "raw_ingest",
    )
    snapshot_path = tmp_path / "concurrent-retention.age"
    db_captured = Event()
    release_snapshot = Event()
    retention_attempted = Event()
    retention_finished = Event()
    errors: list[BaseException] = []
    original_stage = snapshot_mod._stage_healthmes_db

    def paused_database_stage(database_url, stage, **kwargs):
        result = original_stage(database_url, stage, **kwargs)
        db_captured.set()
        if not release_snapshot.wait(10):
            raise TimeoutError("test did not release snapshot staging")
        return result

    @contextmanager
    def tracked_retention_lock():
        retention_attempted.set()
        with real_activity_write_lock():
            yield

    monkeypatch.setattr(
        snapshot_mod,
        "_stage_healthmes_db",
        paused_database_stage,
    )
    monkeypatch.setattr(
        "healthmes.storage.service.activity_write_lock",
        tracked_retention_lock,
    )

    def take_snapshot() -> None:
        try:
            create_snapshot(
                locations,
                passphrase="fence-test-passphrase",
                out_path=snapshot_path,
                created_at=datetime(2026, 8, 17, 2, tzinfo=UTC),
            )
        except BaseException as exc:
            errors.append(exc)

    def run_retention() -> None:
        try:
            with Session(engine) as writer:
                report = run_storage_maintenance(
                    writer,
                    live_settings,
                    now=current,
                )
                writer.commit()
                assert report.deleted >= 1
        except BaseException as exc:
            errors.append(exc)
        finally:
            retention_finished.set()

    snapshot_thread = Thread(target=take_snapshot)
    retention_thread = Thread(target=run_retention)
    snapshot_thread.start()
    assert db_captured.wait(10)
    retention_thread.start()
    assert retention_attempted.wait(10)
    assert not retention_finished.wait(0.2)
    release_snapshot.set()
    snapshot_thread.join(10)
    retention_thread.join(10)
    assert not snapshot_thread.is_alive()
    assert not retention_thread.is_alive()
    assert errors == []
    assert not media_path.exists()

    target_root = tmp_path / "restored-retention-generation"
    restored_db = target_root / "healthmes.db"
    restored_media = target_root / "media"
    restore_snapshot(
        snapshot_path,
        passphrase="fence-test-passphrase",
        locations=DataLocations(
            database_url=f"sqlite+pysqlite:///{restored_db}",
            media_dir=restored_media,
            raw_ingest_dir=target_root / "raw_ingest",
        ),
    )

    restored_engine = create_db_engine(
        f"sqlite+pysqlite:///{restored_db}"
    )
    try:
        with Session(restored_engine) as restored:
            restored_object = restored.get(StorageObject, storage_id)
            assert restored_object is not None
            assert restored_object.purged_at is None
            assert restored_object.sha256 == sha256(payload).hexdigest()
    finally:
        restored_engine.dispose()
    assert (target_root / relative_path).read_bytes() == payload


def test_storage_web_page_renders(client: TestClient) -> None:
    response = client.get("/storage")
    assert response.status_code == 200
    assert "저장 관리" in response.text
    assert "데이터별 보존기간" in response.text
