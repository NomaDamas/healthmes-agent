from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from healthmes.activity.contracts import (
    ActivityBatchIn,
    ActivityCapability,
    ActivityPlatform,
    AppIntervalRecord,
)
from healthmes.activity.repository import DAY_SUMMARY_EVENT
from healthmes.activity.service import ingest_activity_batch
from healthmes.storage import (
    register_storage_object,
    run_storage_maintenance,
    update_retention_policy,
)
from healthmes.store import (
    PurgeJob,
    RetentionPolicy,
    StorageObject,
    WellnessEvent,
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
    policies = {row["data_class"]: row["preset"] for row in body["policies"]}
    assert policies["raw_payload"] == "14d"
    assert policies["media"] == "7d"
    assert policies["aggregate"] == "forever"
    assert policies["activity_raw"] == "14d"
    assert policies["activity_hourly"] == "90d"
    assert policies["activity_daily"] == "forever"
    assert body["backup"]["provider"] == "local"
    assert body["backup"]["snapshot_count"] == 0


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


def test_daily_retention_update_immediately_refreshes_rest_baseline(
    client: TestClient,
    session,
    monkeypatch,
) -> None:
    current = datetime(2026, 8, 10, 12, tzinfo=UTC)
    for day in range(1, 5):
        start = datetime(2026, 8, day, 0, tzinfo=UTC)
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
                        source_record_id=f"retention-rest-{day}",
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
        if row.payload.get("date") == "2026-08-04"
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
        params={"date": "2026-08-04", "timezone": "UTC"},
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
    assert remaining_days == {"2026-08-04"}


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
    assert preview.deleted == 0
    assert target.exists()

    result = run_storage_maintenance(
        session, settings, now=datetime(2026, 8, 5, tzinfo=UTC)
    )
    session.commit()
    session.refresh(obj)
    assert result.deleted == 1
    assert result.bytes_reclaimed == 7
    assert not target.exists()
    assert obj.purged_at is not None
    assert len(list(session.scalars(select(PurgeJob)))) == 2


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
    run_storage_maintenance(session, settings, dry_run=True)

    assert transitions == ["enter", "exit", "enter", "exit"]


def test_storage_web_page_renders(client: TestClient) -> None:
    response = client.get("/storage")
    assert response.status_code == 200
    assert "저장 관리" in response.text
    assert "데이터별 보존기간" in response.text
