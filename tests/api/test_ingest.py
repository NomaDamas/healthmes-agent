"""Raw-first ingest receiver (/v1/ingest/*, healthmes/ingest.py).

The contract under test: the verbatim payload is durable on disk + indexed
BEFORE any interpretation, and parse/forward failures degrade to statuses on
the 202 body, never to request errors (docs/PLAN.md §13).
"""

import asyncio
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Event, Thread
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from healthmes import durable_files as durable_files_mod
from healthmes import ingest as ingest_mod
from healthmes.activity.locking import (
    global_write_plane_guard as real_global_write_plane_guard,
)
from healthmes.api import ingest as ingest_api_mod
from healthmes.api.errors import APIError
from healthmes.backup import snapshot as snapshot_mod
from healthmes.backup.snapshot import (
    DataLocations,
    create_snapshot,
    restore_snapshot,
)
from healthmes.ingest import (
    InvalidHealthKitPayloadError,
    transform_hae,
    transform_healthkit_v1,
)
from healthmes.store import (
    HealthKitDeletionTombstone,
    HealthKitIngestReceipt,
    RawIngestEvent,
    StorageObject,
    WellnessEvent,
    create_db_engine,
)

OW_USER = "0b6f3a52-8c1d-4e2a-9f10-2a5b7c9d1e3f"

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

NATIVE_HEALTHKIT_PAYLOAD = {
    "schema": "healthmes.healthkit.v1",
    "sdkVersion": "healthmes-ios/1",
    "syncTimestamp": "2026-08-24T03:00:00Z",
    "data": {
        "records": [
            {
                "id": "metric-1",
                "type": "HKQuantityTypeIdentifierHeartRate",
                "startDate": "2026-08-24T02:59:00Z",
                "endDate": "2026-08-24T03:00:00Z",
                "value": 62.0,
                "unit": "count/min",
                "source": {
                    "bundleIdentifier": "com.apple.health",
                    "deviceType": "watch",
                    "productType": "Watch7,5",
                },
            }
        ],
        "sleep": [
            {
                "id": "sleep-1",
                "stage": "deep",
                "startDate": "2026-08-23T15:00:00Z",
                "endDate": "2026-08-23T16:00:00Z",
            }
        ],
        "workouts": [
            {
                "id": "workout-1",
                "type": "running",
                "startDate": "2026-08-23T22:00:00Z",
                "endDate": "2026-08-23T22:30:00Z",
                "values": [
                    {"type": "duration", "unit": "s", "value": 1800.0}
                ],
            }
        ],
        "deletions": [],
    },
}


def _stored_file(settings, event: RawIngestEvent):
    return settings.data_dir / event.path


def _staged_raw_files(settings) -> list:
    staging_root = settings.data_dir / ".staging" / "raw_ingest"
    if not staging_root.exists():
        return []
    return [path for path in staging_root.rglob("*.part") if path.is_file()]


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


def test_transform_native_healthkit_preserves_all_sdk_arrays():
    batch = transform_healthkit_v1(NATIVE_HEALTHKIT_PAYLOAD)

    assert batch is not None
    assert batch.sdk_version == "healthmes-ios/1"
    assert batch.sync_timestamp == "2026-08-24T03:00:00Z"
    assert [row["id"] for row in batch.records] == ["metric-1"]
    assert [row["id"] for row in batch.sleep] == ["sleep-1"]
    assert [row["id"] for row in batch.workouts] == ["workout-1"]
    assert batch.deletions == ()


@pytest.mark.parametrize(
    ("payload", "path"),
    [
        (
            {
                "schema": "healthmes.healthkit.v1",
                "sdkVersion": "healthmes-ios/1",
                "syncTimestamp": "2026-08-24T03:00:00Z",
            },
            "data",
        ),
        (
            {
                **NATIVE_HEALTHKIT_PAYLOAD,
                "data": {
                    **NATIVE_HEALTHKIT_PAYLOAD["data"],
                    "records": {},
                },
            },
            "data.records",
        ),
        (
            {
                **NATIVE_HEALTHKIT_PAYLOAD,
                "data": {
                    **NATIVE_HEALTHKIT_PAYLOAD["data"],
                    "records": [
                        {
                            **NATIVE_HEALTHKIT_PAYLOAD["data"]["records"][0],
                            "id": "",
                        }
                    ],
                },
            },
            "data.records[0].id",
        ),
        (
            {
                **NATIVE_HEALTHKIT_PAYLOAD,
                "data": {
                    **NATIVE_HEALTHKIT_PAYLOAD["data"],
                    "records": [
                        {
                            **NATIVE_HEALTHKIT_PAYLOAD["data"]["records"][0],
                            "value": True,
                        }
                    ],
                },
            },
            "data.records[0].value",
        ),
        (
            {
                **NATIVE_HEALTHKIT_PAYLOAD,
                "data": {
                    **NATIVE_HEALTHKIT_PAYLOAD["data"],
                    "sleep": [
                        {
                            **NATIVE_HEALTHKIT_PAYLOAD["data"]["sleep"][0],
                            "endDate": "not-a-date",
                        }
                    ],
                },
            },
            "data.sleep[0].endDate",
        ),
        (
            {
                **NATIVE_HEALTHKIT_PAYLOAD,
                "data": {
                    **NATIVE_HEALTHKIT_PAYLOAD["data"],
                    "workouts": [
                        {
                            **NATIVE_HEALTHKIT_PAYLOAD["data"]["workouts"][0],
                            "values": [{"type": "duration", "value": 1}],
                        }
                    ],
                },
            },
            "data.workouts[0].values[0].unit",
        ),
    ],
)
def test_transform_native_healthkit_rejects_malformed_contract(payload, path):
    with pytest.raises(InvalidHealthKitPayloadError) as exc_info:
        transform_healthkit_v1(payload)

    assert exc_info.value.path == path


# --- POST /v1/ingest/healthkit ----------------------------------------------


def test_healthkit_ingest_stores_raw_and_forwards(client, session, settings):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["api_key"] = request.headers.get("X-Open-Wearables-API-Key")
        captured["body"] = json.loads(request.read())
        return httpx.Response(202, json={"status": "queued"})

    client.app.state.ingest_transport = httpx.MockTransport(handler)
    settings_user = settings.model_copy(update={"ow_user_id": OW_USER})
    client.app.state.settings = settings_user

    response = client.post("/v1/ingest/healthkit", json=HAE_PAYLOAD)

    assert response.status_code == 202
    ack = response.json()
    assert ack["parse_status"] == "parsed"
    assert ack["forward_status"] == "queued"
    assert ack["records_forwarded"] == 3
    assert ack["status_persistence_uncertain"] is False

    # Forwarded to the SDK sync contract for the configured user.
    assert captured["url"].endswith(f"/api/v1/sdk/users/{OW_USER}/sync")
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
    client.app.state.settings = settings.model_copy(update={"ow_user_id": OW_USER})

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


def test_healthkit_ingest_deep_json_with_idempotency_header_is_stored_raw_first(
    client,
    session,
    settings,
):
    depth = 1_024
    while True:
        body = b'{"nested":' * depth + b"0" + b"}" * depth
        try:
            json.loads(body)
        except RecursionError:
            break
        depth *= 2
        if len(body) >= settings.ingest_max_bytes:
            pytest.fail("JSON parser accepted nesting up to the ingest byte cap")

    response = client.post(
        "/v1/ingest/healthkit",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": "deep-legacy-payload-1",
        },
    )

    assert response.status_code == 202
    ack = response.json()
    assert ack["parse_status"] == "stored_unparsed"
    assert ack["forward_status"] == "nothing_mapped"
    event = session.scalars(select(RawIngestEvent)).one()
    assert _stored_file(settings, event).read_bytes() == body
    assert session.scalar(
        select(func.count()).select_from(HealthKitIngestReceipt)
    ) == 0


def test_native_healthkit_forwards_all_arrays_and_stores_exact_body(
    client,
    session,
    settings,
):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(202, json={"status": "queued"})

    client.app.state.ingest_transport = httpx.MockTransport(handler)
    client.app.state.settings = settings.model_copy(update={"ow_user_id": OW_USER})
    body = json.dumps(
        NATIVE_HEALTHKIT_PAYLOAD,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()

    response = client.post(
        "/v1/ingest/healthkit",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": "native-all-arrays-1",
        },
    )

    assert response.status_code == 202
    ack = response.json()
    assert ack["durable"] is True
    assert ack["records_forwarded"] == 1
    assert ack["sleep_forwarded"] == 1
    assert ack["workouts_forwarded"] == 1
    assert ack["deletions_received"] == 0
    assert captured["body"]["sdkVersion"] == "healthmes-ios/1"
    assert captured["body"]["syncTimestamp"] == "2026-08-24T03:00:00Z"
    assert [row["id"] for row in captured["body"]["data"]["records"]] == [
        "metric-1"
    ]
    assert [row["id"] for row in captured["body"]["data"]["sleep"]] == [
        "sleep-1"
    ]
    assert [row["id"] for row in captured["body"]["data"]["workouts"]] == [
        "workout-1"
    ]

    event = session.scalars(select(RawIngestEvent)).one()
    assert _stored_file(client.app.state.settings, event).read_bytes() == body
    receipt = session.scalars(select(HealthKitIngestReceipt)).one()
    assert receipt.state == "completed"
    assert receipt.raw_id == event.id


def test_native_healthkit_exact_replay_returns_stored_ack_once(
    client,
    session,
    settings,
):
    forward_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal forward_count
        forward_count += 1
        return httpx.Response(202, json={"status": "queued"})

    client.app.state.ingest_transport = httpx.MockTransport(handler)
    client.app.state.settings = settings.model_copy(update={"ow_user_id": OW_USER})
    body = json.dumps(
        NATIVE_HEALTHKIT_PAYLOAD,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": "native-exact-replay-1",
    }

    first = client.post("/v1/ingest/healthkit", content=body, headers=headers)
    second = client.post("/v1/ingest/healthkit", content=body, headers=headers)

    assert first.status_code == second.status_code == 202
    assert second.json() == first.json()
    assert forward_count == 1
    assert session.scalar(select(func.count()).select_from(RawIngestEvent)) == 1
    assert (
        session.scalar(select(func.count()).select_from(HealthKitIngestReceipt))
        == 1
    )


@pytest.mark.parametrize(
    ("changed_data", "path"),
    [
        (None, "data"),
        (
            {
                **NATIVE_HEALTHKIT_PAYLOAD["data"],
                "records": "not-an-array",
            },
            "data.records",
        ),
        (
            {
                **NATIVE_HEALTHKIT_PAYLOAD["data"],
                "records": [
                    NATIVE_HEALTHKIT_PAYLOAD["data"]["records"][0],
                    {"id": "broken-row"},
                ],
            },
            "data.records[1].type",
        ),
        (
            {
                **NATIVE_HEALTHKIT_PAYLOAD["data"],
                "deletions": [{"id": "metric-1", "type": ""}],
            },
            "data.deletions[0].type",
        ),
    ],
)
def test_native_malformed_payload_is_stored_exactly_then_rejected(
    client,
    session,
    settings,
    changed_data,
    path,
):
    payload = {
        **NATIVE_HEALTHKIT_PAYLOAD,
        "data": changed_data,
    }
    body = json.dumps(
        payload,
        separators=(", ", ": "),
        sort_keys=False,
    ).encode()
    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": f"native-invalid-{path}",
    }

    first = client.post(
        "/v1/ingest/healthkit",
        content=body,
        headers=headers,
    )
    replay = client.post(
        "/v1/ingest/healthkit",
        content=body,
        headers=headers,
    )

    assert first.status_code == replay.status_code == 422
    assert first.json()["error"]["code"] == "invalid_healthkit_payload"
    assert first.json()["error"]["detail"]["path"] == path
    assert replay.json() == first.json()
    assert session.scalar(select(func.count()).select_from(RawIngestEvent)) == 1
    event = session.scalars(select(RawIngestEvent)).one()
    assert _stored_file(settings, event).read_bytes() == body
    assert event.parse_status == "parsed"
    assert event.forward_status == "rejected_invalid_payload"
    receipt = session.scalars(select(HealthKitIngestReceipt)).one()
    assert receipt.state == "pending"
    assert receipt.raw_id == event.id
    assert receipt.ack_payload is None


def test_native_malformed_payload_key_reuse_with_different_bytes_conflicts(
    client,
    session,
):
    first_payload = {
        **NATIVE_HEALTHKIT_PAYLOAD,
        "data": None,
    }
    second_payload = {
        **NATIVE_HEALTHKIT_PAYLOAD,
        "data": [],
    }
    headers = {"Idempotency-Key": "native-invalid-conflict-1"}

    first = client.post(
        "/v1/ingest/healthkit",
        json=first_payload,
        headers=headers,
    )
    second = client.post(
        "/v1/ingest/healthkit",
        json=second_payload,
        headers=headers,
    )

    assert first.status_code == 422
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_conflict"
    assert session.scalar(select(func.count()).select_from(RawIngestEvent)) == 1


def test_headerless_malformed_native_payload_is_raw_before_rejection(
    client,
    session,
    settings,
):
    payload = {
        **NATIVE_HEALTHKIT_PAYLOAD,
        "data": {"records": []},
    }
    body = json.dumps(payload, separators=(",", ":")).encode()

    response = client.post(
        "/v1/ingest/healthkit",
        content=body,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_healthkit_payload"
    event = session.scalars(select(RawIngestEvent)).one()
    assert _stored_file(settings, event).read_bytes() == body
    assert event.forward_status == "rejected_invalid_payload"
    assert session.scalar(
        select(func.count()).select_from(HealthKitIngestReceipt)
    ) == 0


def test_native_forward_failure_reuses_raw_and_completes_after_retry(
    client,
    session,
    settings,
):
    forward_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal forward_count
        forward_count += 1
        if forward_count == 1:
            return httpx.Response(500, text="worker down")
        return httpx.Response(202, json={"status": "queued"})

    client.app.state.ingest_transport = httpx.MockTransport(handler)
    client.app.state.settings = settings.model_copy(update={"ow_user_id": OW_USER})
    body = json.dumps(
        NATIVE_HEALTHKIT_PAYLOAD,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": "native-forward-retry-1",
    }

    first = client.post("/v1/ingest/healthkit", content=body, headers=headers)
    replay = client.post("/v1/ingest/healthkit", content=body, headers=headers)

    assert first.status_code == replay.status_code == 202
    assert first.json()["forward_status"] == "forward_failed"
    assert replay.json()["forward_status"] == "queued"
    assert replay.json()["raw_id"] == first.json()["raw_id"]
    assert forward_count == 2
    assert session.scalar(select(func.count()).select_from(RawIngestEvent)) == 1
    receipt = session.scalars(select(HealthKitIngestReceipt)).one()
    assert receipt.state == "completed"
    assert receipt.ack_payload == replay.json()


def test_native_missing_user_reuses_raw_until_forwarding_is_configured(
    client,
    session,
    settings,
):
    forward_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal forward_count
        forward_count += 1
        return httpx.Response(202, json={"status": "queued"})

    client.app.state.ingest_transport = httpx.MockTransport(handler)
    body = json.dumps(
        NATIVE_HEALTHKIT_PAYLOAD,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": "native-user-retry-1",
    }

    first = client.post("/v1/ingest/healthkit", content=body, headers=headers)
    assert first.status_code == 202
    assert first.json()["forward_status"] == "skipped_no_user"
    receipt = session.scalars(select(HealthKitIngestReceipt)).one()
    assert receipt.state == "pending"
    assert receipt.ack_payload is None

    client.app.state.settings = settings.model_copy(update={"ow_user_id": OW_USER})
    replay = client.post("/v1/ingest/healthkit", content=body, headers=headers)

    assert replay.status_code == 202
    assert replay.json()["forward_status"] == "queued"
    assert replay.json()["raw_id"] == first.json()["raw_id"]
    assert forward_count == 1
    assert session.scalar(select(func.count()).select_from(RawIngestEvent)) == 1
    session.refresh(receipt)
    assert receipt.state == "completed"


def test_native_healthkit_reused_key_with_different_bytes_conflicts(
    client,
    session,
):
    first_body = json.dumps(
        NATIVE_HEALTHKIT_PAYLOAD,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    changed = {
        **NATIVE_HEALTHKIT_PAYLOAD,
        "syncTimestamp": "2026-08-24T03:00:01Z",
    }
    second_body = json.dumps(
        changed,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": "native-byte-conflict-1",
    }

    first = client.post(
        "/v1/ingest/healthkit",
        content=first_body,
        headers=headers,
    )
    second = client.post(
        "/v1/ingest/healthkit",
        content=second_body,
        headers=headers,
    )

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_conflict"
    assert session.scalar(select(func.count()).select_from(RawIngestEvent)) == 1


def test_headerless_native_payload_is_stored_before_missing_key_rejection(
    client,
    session,
    settings,
):
    body = json.dumps(
        NATIVE_HEALTHKIT_PAYLOAD,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()

    response = client.post(
        "/v1/ingest/healthkit",
        content=body,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_idempotency_key"
    event = session.scalars(select(RawIngestEvent)).one()
    assert _stored_file(settings, event).read_bytes() == body
    assert event.parse_status == "parsed"
    assert event.forward_status == "rejected_missing_idempotency"


def test_native_invalid_idempotency_key_preserves_exact_raw_before_rejection(
    client,
    session,
    settings,
):
    body = json.dumps(
        NATIVE_HEALTHKIT_PAYLOAD,
        separators=(", ", ": "),
        sort_keys=False,
    ).encode()

    response = client.post(
        "/v1/ingest/healthkit",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": "x" * 256,
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_idempotency_key"
    event = session.scalars(select(RawIngestEvent)).one()
    assert _stored_file(settings, event).read_bytes() == body
    assert event.parse_status == "parsed"
    assert event.forward_status == "rejected_invalid_idempotency"
    assert session.scalar(
        select(func.count()).select_from(HealthKitIngestReceipt)
    ) == 0


def test_native_healthkit_deletion_stays_pending_and_suppresses_stale_replay(
    client,
    session,
    settings,
):
    forward_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal forward_count
        forward_count += 1
        return httpx.Response(202, json={"status": "queued"})

    client.app.state.ingest_transport = httpx.MockTransport(handler)
    client.app.state.settings = settings.model_copy(update={"ow_user_id": OW_USER})
    deleted = {
        **NATIVE_HEALTHKIT_PAYLOAD,
        "data": {
            "records": [
                {
                    **NATIVE_HEALTHKIT_PAYLOAD["data"]["records"][0],
                    "id": "metric-2",
                }
            ],
            "sleep": [],
            "workouts": [],
            "deletions": [
                {
                    "id": "metric-1",
                    "type": "HKQuantityTypeIdentifierHeartRate",
                }
            ],
        },
    }
    stale = {
        **NATIVE_HEALTHKIT_PAYLOAD,
        "data": {
            "records": NATIVE_HEALTHKIT_PAYLOAD["data"]["records"],
            "sleep": [],
            "workouts": [],
            "deletions": [],
        },
    }

    deletion_response = client.post(
        "/v1/ingest/healthkit",
        json=deleted,
        headers={"Idempotency-Key": "native-delete-1"},
    )
    deletion_replay = client.post(
        "/v1/ingest/healthkit",
        json=deleted,
        headers={"Idempotency-Key": "native-delete-1"},
    )

    assert deletion_response.status_code == deletion_replay.status_code == 503
    assert deletion_response.json()["error"]["code"] == (
        "healthkit_deletion_pending"
    )
    assert deletion_replay.json() == deletion_response.json()
    assert forward_count == 0
    assert session.scalar(select(func.count()).select_from(RawIngestEvent)) == 1
    receipt = session.scalars(select(HealthKitIngestReceipt)).one()
    assert receipt.state == "pending"
    assert receipt.ack_payload is None
    event = session.scalars(select(RawIngestEvent)).one()
    assert event.forward_status == "deletion_pending"

    stale_response = client.post(
        "/v1/ingest/healthkit",
        json=stale,
        headers={"Idempotency-Key": "native-stale-replay-1"},
    )

    assert stale_response.status_code == 202
    assert stale_response.json()["records_forwarded"] == 0
    assert stale_response.json()["forward_status"] == "nothing_mapped"
    assert forward_count == 0
    tombstone = session.scalars(select(HealthKitDeletionTombstone)).one()
    assert tombstone.sample_id == "metric-1"


@pytest.mark.parametrize(
    "key",
    (
        " native-key",
        "native-key ",
        "native\nkey",
        "native\x7fkey",
    ),
)
def test_native_healthkit_idempotency_key_rejects_unsafe_characters(key):
    request = SimpleNamespace(headers={"idempotency-key": key})

    with pytest.raises(APIError) as exc_info:
        ingest_api_mod._native_idempotency_key(request)

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "invalid_idempotency_key"


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
    assert ack["status_persistence_uncertain"] is False
    event = session.scalars(select(RawIngestEvent)).one()
    assert event.source == "sleep-diary"
    assert _stored_file(settings, event).read_text() == "오늘 새벽 3시에 깼다"


def test_raw_ingest_canonicalizes_before_provider_length_check(
    client,
    session,
) -> None:
    provider = "A" * 64

    response = client.post(
        "/v1/ingest/raw",
        params={"source": f" {provider} "},
        content=b"x",
    )

    assert response.status_code == 202
    assert session.scalars(select(RawIngestEvent)).one().source == (
        provider.lower()
    )


def test_raw_ingest_validates_source_slug(client):
    response = client.post("/v1/ingest/raw?source=../evil", content=b"x")
    assert response.status_code == 422


@pytest.mark.parametrize(
    "source",
    (
        "   ",
        "-sleep-diary",
        "\tsleep-diary\t",
        "ÄPFEL",
        "sleep\x00diary",
        "a" * 65,
    ),
)
def test_raw_ingest_rejects_invalid_provider_before_writing(
    client,
    session,
    settings,
    source,
) -> None:
    response = client.post(
        "/v1/ingest/raw",
        params={"source": source},
        content=b"x",
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_source_provider"
    assert list(session.scalars(select(RawIngestEvent))) == []
    raw_root = settings.data_dir / "raw_ingest"
    assert not raw_root.exists() or not any(raw_root.rglob("*"))


def test_raw_ingest_partial_staging_write_failure_removes_bytes_and_indexes(
    client,
    engine,
    settings,
    monkeypatch,
):
    def write_part_then_fail(output, payload):
        output.write(payload[:9])
        raise OSError("injected partial raw-ingest staging write")

    monkeypatch.setattr(ingest_mod, "write_all", write_part_then_fail)

    with pytest.raises(OSError, match="injected partial raw-ingest staging write"):
        client.post(
            "/v1/ingest/raw?source=partial-write",
            content=b"raw payload must not be partially indexed",
            headers={"Content-Type": "application/octet-stream"},
        )

    assert _staged_raw_files(settings) == []
    assert not (settings.data_dir / "raw_ingest").exists()
    with Session(engine) as verification:
        assert verification.scalars(select(RawIngestEvent)).all() == []
        assert verification.scalars(select(StorageObject)).all() == []
        assert verification.scalars(select(WellnessEvent)).all() == []


def test_raw_ingest_publish_failure_before_destination_removes_staging(
    client,
    engine,
    settings,
    monkeypatch,
):
    def fail_before_destination(_staged, _destination):
        raise durable_files_mod.DurablePublishError(
            "injected pre-publication failure",
            destination_created=False,
            identity=None,
        )

    monkeypatch.setattr(
        ingest_mod,
        "durable_publish_no_clobber",
        fail_before_destination,
    )

    with pytest.raises(
        durable_files_mod.DurablePublishError,
        match="injected pre-publication failure",
    ):
        client.post(
            "/v1/ingest/raw?source=publish-failure",
            content=b"must not remain in staging",
            headers={"Content-Type": "application/octet-stream"},
        )

    assert _staged_raw_files(settings) == []
    assert not (settings.data_dir / "raw_ingest").exists()
    with Session(engine) as verification:
        assert verification.scalars(select(RawIngestEvent)).all() == []
        assert verification.scalars(select(StorageObject)).all() == []
        assert verification.scalars(select(WellnessEvent)).all() == []


def test_raw_ingest_publish_directory_fsync_failure_accepts_and_indexes_staging_fallback(
    client,
    engine,
    settings,
    monkeypatch,
):
    received_at = datetime(2026, 8, 18, 4, 45, tzinfo=UTC)
    destination_dir = settings.data_dir / "raw_ingest" / "2026" / "08" / "18"
    destination_dir.mkdir(parents=True)
    real_fsync_directory = durable_files_mod._fsync_directory

    def fail_destination_directory_fsync(path, descriptor):
        if path == destination_dir:
            raise OSError("injected raw-ingest destination-directory fsync failure")
        return real_fsync_directory(path, descriptor)

    monkeypatch.setattr(ingest_mod, "_utcnow", lambda: received_at)
    monkeypatch.setattr(
        durable_files_mod,
        "_fsync_directory",
        fail_destination_directory_fsync,
    )

    response = client.post(
        "/v1/ingest/raw?source=publish-fsync",
        content=b"raw bytes with an ambiguous publication",
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 202
    assert response.json()["status_persistence_uncertain"] is True
    published = [path for path in destination_dir.iterdir() if path.is_file()]
    staged = _staged_raw_files(settings)
    assert len(published) == 1
    assert len(staged) == 1
    assert published[0].read_bytes() == b"raw bytes with an ambiguous publication"
    assert staged[0].read_bytes() == b"raw bytes with an ambiguous publication"
    assert published[0].stat().st_ino == staged[0].stat().st_ino
    with Session(engine) as verification:
        raw = verification.scalars(select(RawIngestEvent)).one()
        storage = verification.scalars(select(StorageObject)).one()
        wellness = verification.scalars(select(WellnessEvent)).one()
        assert storage.relative_path == raw.path
        assert wellness.raw_object_id == storage.id


def test_raw_ingest_staging_cleanup_failure_keeps_committed_copy_for_recovery(
    client,
    engine,
    settings,
    monkeypatch,
    caplog,
):
    real_unlink = ingest_api_mod.durable_unlink

    def fail_staging_cleanup(path, *, missing_ok=False, expected=None):
        if path.suffix == ".part":
            raise OSError("injected raw staging-directory fsync failure")
        return real_unlink(
            path,
            missing_ok=missing_ok,
            expected=expected,
        )

    monkeypatch.setattr(
        ingest_api_mod,
        "durable_unlink",
        fail_staging_cleanup,
    )

    with caplog.at_level("ERROR", logger="healthmes.api.ingest"):
        response = client.post(
            "/v1/ingest/raw?source=cleanup-uncertainty",
            content=b"retain the published raw payload",
            headers={"Content-Type": "application/octet-stream"},
        )

    assert response.status_code == 202
    published = [
        path
        for path in (settings.data_dir / "raw_ingest").rglob("*")
        if path.is_file()
    ]
    assert len(published) == 1
    assert published[0].read_bytes() == b"retain the published raw payload"
    staged = _staged_raw_files(settings)
    assert len(staged) == 1
    assert staged[0].read_bytes() == b"retain the published raw payload"
    with Session(engine) as verification:
        assert len(verification.scalars(select(RawIngestEvent)).all()) == 1
        assert len(verification.scalars(select(StorageObject)).all()) == 1
        assert len(verification.scalars(select(WellnessEvent)).all()) == 1
    assert "failed to durably clean up raw-ingest bytes" in caplog.text


def test_raw_ingest_commit_applied_then_raises_keeps_referenced_bytes(
    client,
    engine,
    settings,
    monkeypatch,
):
    real_commit = Session.commit

    def commit_then_raise(session):
        real_commit(session)
        raise OSError("injected lost raw commit acknowledgement")

    monkeypatch.setattr(Session, "commit", commit_then_raise)

    response = client.post(
        "/v1/ingest/raw?source=commit-ambiguity",
        content=b"raw bytes must survive",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 202
    assert response.json()["forward_status"] == "not_applicable"

    with Session(engine) as verification:
        raw = verification.scalars(select(RawIngestEvent)).one()
        storage = verification.scalars(select(StorageObject)).one()
        wellness = verification.scalars(select(WellnessEvent)).one()
        assert storage.relative_path == raw.path
        assert wellness.source_record_id == str(raw.id)
        assert wellness.raw_object_id == storage.id
        assert (settings.data_dir / raw.path).read_bytes() == (b"raw bytes must survive")


def test_raw_ingest_post_commit_reload_failure_returns_uncertain_ack(
    client,
    engine,
    settings,
    monkeypatch,
):
    def fail_reload(_bind, _raw_id):
        raise OSError("injected raw post-commit reload failure")

    monkeypatch.setattr(ingest_api_mod, "_load_raw_state", fail_reload)

    response = client.post(
        "/v1/ingest/raw?source=reload-uncertainty",
        content=b"the committed raw generation must remain retryable",
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 202
    ack = response.json()
    assert ack["status_persistence_uncertain"] is True
    assert ack["parse_status"] == "stored_unparsed"
    assert ack["forward_status"] == "not_applicable"
    with Session(engine) as verification:
        raw = verification.scalars(select(RawIngestEvent)).one()
        storage = verification.scalars(select(StorageObject)).one()
        wellness = verification.scalars(select(WellnessEvent)).one()
        assert ack["raw_id"] == str(raw.id)
        assert ack["sha256"] == raw.sha256
        assert storage.relative_path == raw.path
        assert wellness.source_record_id == str(raw.id)
        assert (settings.data_dir / raw.path).read_bytes() == (
            b"the committed raw generation must remain retryable"
        )


def test_healthkit_status_reload_failure_returns_uncertain_ack(
    client,
    engine,
    settings,
    monkeypatch,
):
    real_load = ingest_api_mod._load_raw_state
    calls = 0

    def fail_status_reload(bind, raw_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected healthkit status reload failure")
        return real_load(bind, raw_id)

    monkeypatch.setattr(ingest_api_mod, "_load_raw_state", fail_status_reload)
    client.app.state.ingest_transport = httpx.MockTransport(
        lambda request: httpx.Response(202, json={"status": "queued"})
    )
    client.app.state.settings = settings.model_copy(update={"ow_user_id": OW_USER})

    response = client.post("/v1/ingest/healthkit", json=HAE_PAYLOAD)

    assert response.status_code == 202
    ack = response.json()
    assert ack["parse_status"] == "parsed"
    assert ack["forward_status"] == "queued"
    assert ack["records_forwarded"] == 3
    assert ack["status_persistence_uncertain"] is True
    with Session(engine) as verification:
        raw = verification.scalars(select(RawIngestEvent)).one()
        assert raw.parse_status == "parsed"
        assert raw.forward_status == "queued"
        assert raw.records_forwarded == 3


def test_raw_ingest_rejects_destination_generation_replaced_before_commit(
    client,
    engine,
    settings,
    monkeypatch,
):
    real_index = ingest_api_mod.index_raw_ingest

    def index_then_replace(session, configured, event):
        wellness = real_index(session, configured, event)
        destination = settings.data_dir / event.path
        replacement = destination.with_name(f".{destination.name}.replacement")
        replacement.write_bytes(b"replacement raw generation")
        os.replace(replacement, destination)
        return wellness

    monkeypatch.setattr(
        ingest_api_mod,
        "index_raw_ingest",
        index_then_replace,
    )

    with pytest.raises(OSError, match="file generation changed"):
        client.post(
            "/v1/ingest/raw?source=generation-race",
            content=b"original raw generation",
            headers={"Content-Type": "application/octet-stream"},
        )

    with Session(engine) as verification:
        assert verification.scalars(select(RawIngestEvent)).all() == []
        assert verification.scalars(select(StorageObject)).all() == []
        assert verification.scalars(select(WellnessEvent)).all() == []
    published = [
        path
        for path in (settings.data_dir / "raw_ingest").rglob("*")
        if path.is_file()
    ]
    assert len(published) == 1
    assert published[0].read_bytes() == b"replacement raw generation"
    staged = _staged_raw_files(settings)
    assert len(staged) == 1
    assert staged[0].read_bytes() == b"original raw generation"


def test_raw_ingest_ambiguous_commit_rejects_replaced_destination_generation(
    client,
    engine,
    settings,
    monkeypatch,
):
    real_commit = Session.commit

    def commit_replace_then_raise(session):
        real_commit(session)
        destination = next(
            path
            for path in (settings.data_dir / "raw_ingest").rglob("*")
            if path.is_file()
        )
        replacement = destination.with_name(f".{destination.name}.replacement")
        replacement.write_bytes(b"replacement after raw commit")
        os.replace(replacement, destination)
        raise OSError("injected replaced raw commit acknowledgement")

    monkeypatch.setattr(Session, "commit", commit_replace_then_raise)

    with pytest.raises(
        OSError,
        match="injected replaced raw commit acknowledgement",
    ):
        client.post(
            "/v1/ingest/raw?source=ambiguous-generation",
            content=b"original raw commit generation",
            headers={"Content-Type": "application/octet-stream"},
        )

    with Session(engine) as verification:
        raw = verification.scalars(select(RawIngestEvent)).one()
        destination = settings.data_dir / raw.path
        assert destination.read_bytes() == b"replacement after raw commit"
    staged = _staged_raw_files(settings)
    assert len(staged) == 1
    assert staged[0].read_bytes() == b"original raw commit generation"


def test_healthkit_status_commit_failure_returns_current_ack_marked_uncertain(
    client,
    engine,
    settings,
    monkeypatch,
):
    real_commit = Session.commit
    calls = 0

    def fail_status_commit(session):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected healthkit status commit failure")
        return real_commit(session)

    monkeypatch.setattr(Session, "commit", fail_status_commit)
    client.app.state.ingest_transport = httpx.MockTransport(
        lambda request: httpx.Response(202, json={"status": "queued"})
    )
    client.app.state.settings = settings.model_copy(update={"ow_user_id": OW_USER})

    response = client.post("/v1/ingest/healthkit", json=HAE_PAYLOAD)

    assert response.status_code == 202
    ack = response.json()
    assert ack["parse_status"] == "parsed"
    assert ack["forward_status"] == "queued"
    assert ack["records_forwarded"] == 3
    assert ack["status_persistence_uncertain"] is True
    with Session(engine) as verification:
        raw = verification.scalars(select(RawIngestEvent)).one()
        assert raw.parse_status == "stored_unparsed"
        assert raw.forward_status == "pending"
        assert raw.records_forwarded == 0
        assert (client.app.state.settings.data_dir / raw.path).is_file()


def test_healthkit_status_lost_commit_ack_is_verified_as_persisted(
    client,
    engine,
    settings,
    monkeypatch,
):
    real_commit = Session.commit
    calls = 0

    def commit_status_then_raise(session):
        nonlocal calls
        calls += 1
        result = real_commit(session)
        if calls == 2:
            raise OSError("injected lost healthkit status commit acknowledgement")
        return result

    monkeypatch.setattr(Session, "commit", commit_status_then_raise)
    client.app.state.ingest_transport = httpx.MockTransport(
        lambda request: httpx.Response(202, json={"status": "queued"})
    )
    client.app.state.settings = settings.model_copy(update={"ow_user_id": OW_USER})

    response = client.post("/v1/ingest/healthkit", json=HAE_PAYLOAD)

    assert response.status_code == 202
    ack = response.json()
    assert ack["parse_status"] == "parsed"
    assert ack["forward_status"] == "queued"
    assert ack["records_forwarded"] == 3
    assert ack["status_persistence_uncertain"] is False
    with Session(engine) as verification:
        raw = verification.scalars(select(RawIngestEvent)).one()
        assert raw.parse_status == "parsed"
        assert raw.forward_status == "queued"
        assert raw.records_forwarded == 3


@pytest.mark.asyncio
async def test_raw_ingest_waiting_for_write_plane_does_not_block_health(
    app,
    engine,
    settings,
    monkeypatch,
):
    attempted = Event()

    @contextmanager
    def tracked_guard(database_url):
        attempted.set()
        with real_global_write_plane_guard(database_url):
            yield

    monkeypatch.setattr(ingest_api_mod, "global_write_plane_guard", tracked_guard)
    app.state.settings = settings.model_copy(update={"database_url": str(engine.url)})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:8100",
    ) as async_client:
        with real_global_write_plane_guard(str(engine.url)):
            request_task = asyncio.create_task(
                async_client.post(
                    "/v1/ingest/raw?source=event-loop-fence",
                    content=b"worker-thread persistence",
                    headers={"Content-Type": "application/octet-stream"},
                )
            )
            assert await asyncio.to_thread(attempted.wait, 1)
            health = await asyncio.wait_for(async_client.get("/health"), timeout=0.5)
            assert health.status_code == 200
            assert not request_task.done()
        response = await asyncio.wait_for(request_task, timeout=2)

    assert response.status_code == 202


def test_raw_ingest_unknown_commit_outcome_retains_bytes(
    client,
    settings,
    monkeypatch,
    caplog,
):
    def fail_commit(_session):
        raise OSError("injected unknown raw commit outcome")

    monkeypatch.setattr(Session, "commit", fail_commit)
    monkeypatch.setattr(
        ingest_api_mod,
        "_verify_raw_ingest_commit",
        lambda *_args, **_kwargs: None,
    )

    with caplog.at_level("WARNING", logger="healthmes.api.ingest"):
        with pytest.raises(OSError, match="unknown raw commit outcome"):
            client.post(
                "/v1/ingest/raw?source=unknown-outcome",
                content=b"retain when database outcome cannot be checked",
                headers={"Content-Type": "application/octet-stream"},
            )

    files = [path for path in (settings.data_dir / "raw_ingest").rglob("*") if path.is_file()]
    assert len(files) == 1
    assert files[0].read_bytes() == (b"retain when database outcome cannot be checked")
    assert "retaining bytes" in caplog.text


def test_raw_ingest_delayed_commit_visibility_retains_bytes(
    client,
    settings,
    monkeypatch,
    caplog,
):
    def fail_commit(_session):
        raise OSError("injected delayed raw commit visibility")

    monkeypatch.setattr(Session, "commit", fail_commit)
    monkeypatch.setattr(
        ingest_api_mod,
        "_verify_raw_ingest_commit",
        lambda *_args, **_kwargs: False,
    )

    with caplog.at_level("WARNING", logger="healthmes.api.ingest"):
        with pytest.raises(OSError, match="delayed raw commit visibility"):
            client.post(
                "/v1/ingest/raw?source=delayed-visibility",
                content=b"retain while a committed row may become visible",
                headers={"Content-Type": "application/octet-stream"},
            )

    files = [path for path in (settings.data_dir / "raw_ingest").rglob("*") if path.is_file()]
    assert len(files) == 1
    assert files[0].read_bytes() == b"retain while a committed row may become visible"
    assert "not yet visible" in caplog.text
    assert "retaining bytes" in caplog.text


def test_snapshot_fence_excludes_concurrent_raw_ingest_generation(
    client,
    engine,
    settings,
    monkeypatch,
    tmp_path,
):
    live_settings = settings.model_copy(update={"database_url": str(engine.url)})
    client.app.state.settings = live_settings
    locations = DataLocations(
        database_url=str(engine.url),
        media_dir=live_settings.data_dir / "media",
        raw_ingest_dir=live_settings.data_dir / "raw_ingest",
    )
    snapshot_path = tmp_path / "concurrent-ingest.age"
    db_captured = Event()
    release_snapshot = Event()
    writer_attempted = Event()
    writer_finished = Event()
    errors: list[BaseException] = []
    original_stage = snapshot_mod._stage_healthmes_db

    def paused_database_stage(database_url, stage, **kwargs):
        result = original_stage(database_url, stage, **kwargs)
        db_captured.set()
        if not release_snapshot.wait(10):
            raise TimeoutError("test did not release snapshot staging")
        return result

    @contextmanager
    def tracked_writer_lock(database_url):
        writer_attempted.set()
        with real_global_write_plane_guard(database_url):
            yield

    monkeypatch.setattr(
        snapshot_mod,
        "_stage_healthmes_db",
        paused_database_stage,
    )
    monkeypatch.setattr(
        "healthmes.api.ingest.global_write_plane_guard",
        tracked_writer_lock,
    )

    def take_snapshot() -> None:
        try:
            create_snapshot(
                locations,
                passphrase="fence-test-passphrase",
                out_path=snapshot_path,
                created_at=datetime(2026, 8, 17, 1, tzinfo=UTC),
            )
        except BaseException as exc:
            errors.append(exc)

    def ingest_after_database_capture() -> None:
        try:
            response = client.post(
                "/v1/ingest/raw?source=fence-test",
                content=b"arrived after captured database",
                headers={"Content-Type": "application/octet-stream"},
            )
            assert response.status_code == 202
        except BaseException as exc:
            errors.append(exc)
        finally:
            writer_finished.set()

    snapshot_thread = Thread(target=take_snapshot)
    writer_thread = Thread(target=ingest_after_database_capture)
    snapshot_thread.start()
    assert db_captured.wait(10)
    writer_thread.start()
    assert writer_attempted.wait(10)
    assert not writer_finished.wait(0.2)
    release_snapshot.set()
    snapshot_thread.join(10)
    writer_thread.join(10)
    assert not snapshot_thread.is_alive()
    assert not writer_thread.is_alive()
    assert errors == []

    target_root = tmp_path / "restored-ingest-generation"
    restored_db = target_root / "healthmes.db"
    restore_snapshot(
        snapshot_path,
        passphrase="fence-test-passphrase",
        locations=DataLocations(
            database_url=f"sqlite+pysqlite:///{restored_db}",
            media_dir=target_root / "media",
            raw_ingest_dir=target_root / "raw_ingest",
        ),
    )
    restored_engine = create_db_engine(f"sqlite+pysqlite:///{restored_db}")
    try:
        with Session(restored_engine) as restored:
            assert restored.scalar(select(func.count()).select_from(RawIngestEvent)) == 0
    finally:
        restored_engine.dispose()
    assert not (target_root / "raw_ingest").exists()

    with Session(engine) as live:
        event = live.scalars(select(RawIngestEvent)).one()
        assert (live_settings.data_dir / event.path).read_bytes() == (
            b"arrived after captured database"
        )


def test_forward_redirect_is_not_success(client, session, settings):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(307, headers={"location": "http://x"})
    )
    client.app.state.ingest_transport = transport
    client.app.state.settings = settings.model_copy(update={"ow_user_id": OW_USER})
    response = client.post("/v1/ingest/healthkit", json=HAE_PAYLOAD)
    assert response.status_code == 202
    assert response.json()["forward_status"] == "forward_failed"


def test_forward_refuses_non_uuid_user(client, session, settings):
    client.app.state.settings = settings.model_copy(update={"ow_user_id": "not-a-uuid"})
    response = client.post("/v1/ingest/healthkit", json=HAE_PAYLOAD)
    assert response.status_code == 202
    ack = response.json()
    assert ack["forward_status"] == "forward_failed"
    assert "UUID" in session.scalars(select(RawIngestEvent)).one().forward_detail


def test_transform_skips_non_finite_values():
    payload = {
        "data": {
            "metrics": [
                {
                    "name": "heart_rate_variability",
                    "units": "ms",
                    "data": [
                        {"date": "2026-07-16 03:00:00 +0900", "qty": float("nan")},
                        {"date": "2026-07-16 03:05:00 +0900", "qty": float("inf")},
                        {"date": "2026-07-16 03:10:00 +0900", "qty": True},
                        {"date": "2026-07-16 03:15:00 +0900", "qty": 44.0},
                    ],
                }
            ]
        }
    }
    records = transform_hae(payload)
    assert [r["value"] for r in records] == [44.0]
