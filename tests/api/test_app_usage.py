"""Tests for the Android app-usage batch ingest endpoint."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from healthmes.activity.repository import APP_HOUR_EVENT, DAY_SUMMARY_EVENT
from healthmes.store import AppUsageSample, WellnessEvent


def _batch(samples):
    return {
        "device_id": "pixel-8-test",
        "collection_revision": 0,
        "samples": samples,
    }


SAMPLE_SLACK = {
    "bucket_start": "2026-08-01T10:00:00Z",
    "app_package": "com.slack",
    "foreground_seconds": 340,
    "launches": 9,
    "category": "communication",
}
SAMPLE_MAPS = {
    "bucket_start": "2026-08-01T10:00:00Z",
    "app_package": "com.google.maps",
    "foreground_seconds": 120,
    "launches": 2,
}


def test_batch_ingest_creates_rows(client, session):
    response = client.post("/v1/app-usage/batch", json=_batch([SAMPLE_SLACK, SAMPLE_MAPS]))

    assert response.status_code == 200
    assert response.json() == {
        "accepted": 2,
        "created": 2,
        "updated": 0,
        "suppressed": 0,
    }

    rows = session.scalars(select(AppUsageSample)).all()
    assert {row.app_package for row in rows} == {"com.slack", "com.google.maps"}
    slack = next(row for row in rows if row.app_package == "com.slack")
    assert slack.device_id == "pixel-8-test"
    assert slack.foreground_seconds == 340
    assert slack.launches == 9
    assert slack.category == "communication"
    canonical = session.scalars(
        select(WellnessEvent).where(
            WellnessEvent.event_type == APP_HOUR_EVENT
        )
    ).all()
    assert {row.payload["app_id"] for row in canonical} == {
        "com.slack",
        "com.google.maps",
    }


def test_batch_ingest_upserts_growing_bucket(client, session):
    client.post("/v1/app-usage/batch", json=_batch([SAMPLE_SLACK]))

    grown = {**SAMPLE_SLACK, "foreground_seconds": 900, "launches": 15}
    response = client.post("/v1/app-usage/batch", json=_batch([grown]))

    assert response.json() == {
        "accepted": 1,
        "created": 0,
        "updated": 1,
        "suppressed": 0,
    }
    rows = session.scalars(select(AppUsageSample)).all()
    assert len(rows) == 1
    assert rows[0].foreground_seconds == 900
    assert rows[0].launches == 15
    canonical = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == APP_HOUR_EVENT
        )
    )
    assert canonical is not None
    assert canonical.payload["foreground_seconds"] == 900


def test_batch_ingest_dedupes_within_payload_last_wins(client, session):
    first = {**SAMPLE_SLACK, "foreground_seconds": 100}
    last = {**SAMPLE_SLACK, "foreground_seconds": 250}

    response = client.post("/v1/app-usage/batch", json=_batch([first, last]))

    assert response.json() == {
        "accepted": 1,
        "created": 1,
        "updated": 0,
        "suppressed": 0,
    }
    rows = session.scalars(select(AppUsageSample)).all()
    assert len(rows) == 1
    assert rows[0].foreground_seconds == 250


def test_batch_ingest_same_bucket_different_devices_kept_apart(client, session):
    client.post("/v1/app-usage/batch", json=_batch([SAMPLE_SLACK]))
    response = client.post(
        "/v1/app-usage/batch",
        json={
            "device_id": "tab-s9-test",
            "collection_revision": 0,
            "samples": [SAMPLE_SLACK],
        },
    )

    assert response.json() == {
        "accepted": 1,
        "created": 1,
        "updated": 0,
        "suppressed": 0,
    }
    assert len(session.scalars(select(AppUsageSample)).all()) == 2


def test_legacy_overflow_is_clamped_in_canonical_summary(client, session):
    overflow = {**SAMPLE_SLACK, "foreground_seconds": 7200}

    response = client.post("/v1/app-usage/batch", json=_batch([overflow]))

    assert response.status_code == 200
    legacy = session.scalar(select(AppUsageSample))
    summary = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == DAY_SUMMARY_EVENT
        )
    )
    assert legacy is not None
    assert legacy.foreground_seconds == 7200
    assert summary is not None
    assert summary.payload["total_active_minutes"] == 60.0
    assert "source_reported_seconds_exceeded_bucket" in summary.payload["limitations"]


def test_batch_ingest_validation_errors(client):
    empty = client.post("/v1/app-usage/batch", json=_batch([]))
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "validation_error"

    negative = client.post(
        "/v1/app-usage/batch",
        json=_batch([{**SAMPLE_SLACK, "foreground_seconds": -1}]),
    )
    assert negative.status_code == 422

    no_device = client.post("/v1/app-usage/batch", json={"samples": [SAMPLE_SLACK]})
    assert no_device.status_code == 422


def test_batch_ingest_requires_collection_revision(client):
    response = client.post(
        "/v1/app-usage/batch",
        json={
            "device_id": "pixel-8-test",
            "samples": [SAMPLE_SLACK],
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_batch_ingest_rejects_future_sample_without_persisting(client, session):
    future_bucket = (datetime.now(UTC) + timedelta(hours=2)).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    response = client.post(
        "/v1/app-usage/batch",
        json=_batch(
            [
                {
                    **SAMPLE_SLACK,
                    "bucket_start": future_bucket.isoformat(),
                }
            ]
        ),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "activity_future_data"
    assert session.scalar(select(AppUsageSample)) is None
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT
            )
        )
        is None
    )


def test_deleted_legacy_sample_is_suppressed_on_retry(client, session):
    first = client.post("/v1/app-usage/batch", json=_batch([SAMPLE_SLACK]))
    assert first.status_code == 200
    deleted = client.post(
        "/v1/activity/data/delete",
        json={
            "device_id": "pixel-8-test",
            "start": "2026-08-01T10:15:00Z",
            "end": "2026-08-01T10:30:00Z",
            "include_summaries": True,
            "include_control": False,
            "confirm": True,
        },
    )
    assert deleted.status_code == 200

    retried = client.post("/v1/app-usage/batch", json=_batch([SAMPLE_SLACK]))

    assert retried.status_code == 200
    assert retried.json() == {
        "accepted": 0,
        "created": 0,
        "updated": 0,
        "suppressed": 1,
    }
    assert session.scalar(select(AppUsageSample)) is None
