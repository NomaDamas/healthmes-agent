"""Tests for the Android app-usage batch ingest endpoint."""

from sqlalchemy import select

from healthmes.store import AppUsageSample


def _batch(samples):
    return {"device_id": "pixel-8-test", "samples": samples}


SAMPLE_SLACK = {
    "bucket_start": "2026-07-09T10:00:00Z",
    "app_package": "com.slack",
    "foreground_seconds": 340,
    "launches": 9,
    "category": "communication",
}
SAMPLE_MAPS = {
    "bucket_start": "2026-07-09T10:00:00Z",
    "app_package": "com.google.maps",
    "foreground_seconds": 120,
    "launches": 2,
}


def test_batch_ingest_creates_rows(client, session):
    response = client.post("/v1/app-usage/batch", json=_batch([SAMPLE_SLACK, SAMPLE_MAPS]))

    assert response.status_code == 200
    assert response.json() == {"accepted": 2, "created": 2, "updated": 0}

    rows = session.scalars(select(AppUsageSample)).all()
    assert {row.app_package for row in rows} == {"com.slack", "com.google.maps"}
    slack = next(row for row in rows if row.app_package == "com.slack")
    assert slack.device_id == "pixel-8-test"
    assert slack.foreground_seconds == 340
    assert slack.launches == 9
    assert slack.category == "communication"


def test_batch_ingest_upserts_growing_bucket(client, session):
    client.post("/v1/app-usage/batch", json=_batch([SAMPLE_SLACK]))

    grown = {**SAMPLE_SLACK, "foreground_seconds": 900, "launches": 15}
    response = client.post("/v1/app-usage/batch", json=_batch([grown]))

    assert response.json() == {"accepted": 1, "created": 0, "updated": 1}
    rows = session.scalars(select(AppUsageSample)).all()
    assert len(rows) == 1
    assert rows[0].foreground_seconds == 900
    assert rows[0].launches == 15


def test_batch_ingest_dedupes_within_payload_last_wins(client, session):
    first = {**SAMPLE_SLACK, "foreground_seconds": 100}
    last = {**SAMPLE_SLACK, "foreground_seconds": 250}

    response = client.post("/v1/app-usage/batch", json=_batch([first, last]))

    assert response.json() == {"accepted": 1, "created": 1, "updated": 0}
    rows = session.scalars(select(AppUsageSample)).all()
    assert len(rows) == 1
    assert rows[0].foreground_seconds == 250


def test_batch_ingest_same_bucket_different_devices_kept_apart(client, session):
    client.post("/v1/app-usage/batch", json=_batch([SAMPLE_SLACK]))
    response = client.post(
        "/v1/app-usage/batch",
        json={"device_id": "tab-s9-test", "samples": [SAMPLE_SLACK]},
    )

    assert response.json() == {"accepted": 1, "created": 1, "updated": 0}
    assert len(session.scalars(select(AppUsageSample)).all()) == 2


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
