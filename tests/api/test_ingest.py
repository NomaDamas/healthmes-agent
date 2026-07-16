"""Raw-first ingest receiver (/v1/ingest/*, healthmes/ingest.py).

The contract under test: the verbatim payload is durable on disk + indexed
BEFORE any interpretation, and parse/forward failures degrade to statuses on
the 202 body, never to request errors (docs/PLAN.md §13).
"""

import json

import httpx
import pytest
from sqlalchemy import select

from healthmes.ingest import transform_hae
from healthmes.store import RawIngestEvent

HAE_PAYLOAD = {
    "data": {
        "metrics": [
            {
                "name": "heart_rate",
                "units": "count/min",
                "data": [
                    {"date": "2026-07-15 23:10:00 +0900", "Min": 52, "Avg": 57.5, "Max": 66},
                    {"date": "2026-07-15 23:11:00 +0900", "Min": 51, "Avg": 56.0, "Max": 61},
                ],
            },
            {
                "name": "heart_rate_variability",
                "units": "ms",
                "data": [{"date": "2026-07-16 03:00:00 +0900", "qty": 48.2}],
            },
            {
                "name": "mystery_future_metric",
                "units": "??",
                "data": [{"date": "2026-07-16 03:00:00 +0900", "qty": 1}],
            },
        ]
    }
}


def _stored_file(settings, event: RawIngestEvent):
    return settings.data_dir / event.path


# --- transform_hae -----------------------------------------------------------


def test_transform_maps_known_metrics_and_skips_unknown():
    records = transform_hae(HAE_PAYLOAD)
    types = {record["type"] for record in records}
    assert types == {
        "HKQuantityTypeIdentifierHeartRate",
        "HKQuantityTypeIdentifierHeartRateVariabilitySDNN",
    }
    assert len(records) == 3  # 2 HR points (Avg used) + 1 HRV point
    hr = [r for r in records if r["type"] == "HKQuantityTypeIdentifierHeartRate"]
    assert {r["value"] for r in hr} == {57.5, 56.0}
    assert all("+09:00" in r["startDate"] for r in records)


@pytest.mark.parametrize("junk", [None, [], "str", {"data": {"metrics": "nope"}}, {}])
def test_transform_tolerates_garbage(junk):
    assert transform_hae(junk) == []


# --- POST /v1/ingest/healthkit ----------------------------------------------


def test_healthkit_ingest_stores_raw_and_forwards(client, session, settings):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["api_key"] = request.headers.get("X-Open-Wearables-API-Key")
        captured["body"] = json.loads(request.read())
        return httpx.Response(202, json={"status": "queued"})

    client.app.state.ingest_transport = httpx.MockTransport(handler)
    settings_user = settings.model_copy(update={"ow_user_id": "ow-user-7"})
    client.app.state.settings = settings_user

    response = client.post("/v1/ingest/healthkit", json=HAE_PAYLOAD)

    assert response.status_code == 202
    ack = response.json()
    assert ack["parse_status"] == "parsed"
    assert ack["forward_status"] == "forwarded"
    assert ack["records_forwarded"] == 3

    # Forwarded to the SDK sync contract for the configured user.
    assert captured["url"].endswith("/api/v1/sdk/users/ow-user-7/sync")
    assert captured["api_key"] == "test-ow-api-key"
    assert captured["body"]["provider"] == "apple"
    assert len(captured["body"]["data"]["records"]) == 3

    # Raw file is verbatim on disk, owner-only, and indexed.
    event = session.scalars(select(RawIngestEvent)).one()
    stored = _stored_file(settings_user, event)
    assert json.loads(stored.read_bytes()) == HAE_PAYLOAD
    assert (stored.stat().st_mode & 0o777) == 0o600
    assert event.sha256 == ack["sha256"]


def test_healthkit_ingest_without_user_still_stores(client, session):
    response = client.post("/v1/ingest/healthkit", json=HAE_PAYLOAD)

    assert response.status_code == 202
    ack = response.json()
    assert ack["parse_status"] == "parsed"
    assert ack["forward_status"] == "skipped_no_user"  # conftest ow_user_id=None
    event = session.scalars(select(RawIngestEvent)).one()
    assert event.forward_status == "skipped_no_user"


def test_healthkit_ingest_forward_failure_keeps_raw(client, session, settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="worker down")

    client.app.state.ingest_transport = httpx.MockTransport(handler)
    client.app.state.settings = settings.model_copy(update={"ow_user_id": "u"})

    response = client.post("/v1/ingest/healthkit", json=HAE_PAYLOAD)

    assert response.status_code == 202  # raw durable => success
    ack = response.json()
    assert ack["forward_status"] == "forward_failed"
    event = session.scalars(select(RawIngestEvent)).one()
    assert event.forward_detail and "500" in event.forward_detail
    assert "test-ow-api-key" not in (event.forward_detail or "")


def test_healthkit_ingest_non_json_is_kept_unparsed(client, session, settings):
    response = client.post(
        "/v1/ingest/healthkit",
        content=b"\x00\x01 not json",
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 202
    ack = response.json()
    assert ack["parse_status"] == "stored_unparsed"
    assert ack["forward_status"] == "nothing_mapped"
    event = session.scalars(select(RawIngestEvent)).one()
    assert _stored_file(settings, event).read_bytes() == b"\x00\x01 not json"
    assert event.path.endswith(".bin")


def test_ingest_rejects_oversize_payload(client, settings):
    client.app.state.settings = settings.model_copy(update={"ingest_max_bytes": 10})
    response = client.post("/v1/ingest/healthkit", json=HAE_PAYLOAD)
    assert response.status_code == 413


def test_ingest_rejects_empty_body(client):
    response = client.post("/v1/ingest/healthkit", content=b"")
    assert response.status_code == 400


# --- POST /v1/ingest/raw ------------------------------------------------------


def test_raw_ingest_stores_anything(client, session, settings):
    response = client.post(
        "/v1/ingest/raw?source=sleep-diary",
        content="오늘 새벽 3시에 깼다".encode(),
        headers={"Content-Type": "text/plain"},
    )

    assert response.status_code == 202
    ack = response.json()
    assert ack["forward_status"] == "not_applicable"
    event = session.scalars(select(RawIngestEvent)).one()
    assert event.source == "sleep-diary"
    assert _stored_file(settings, event).read_text() == "오늘 새벽 3시에 깼다"


def test_raw_ingest_validates_source_slug(client):
    response = client.post("/v1/ingest/raw?source=../evil", content=b"x")
    assert response.status_code == 422
