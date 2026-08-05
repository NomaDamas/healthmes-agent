from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from healthmes.storage import register_storage_object, run_storage_maintenance
from healthmes.store import PurgeJob, RetentionPolicy, WellnessEvent


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


def test_storage_web_page_renders(client: TestClient) -> None:
    response = client.get("/storage")
    assert response.status_code == 200
    assert "저장 관리" in response.text
    assert "데이터별 보존기간" in response.text
