from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from healthmes.activity.repository import (
    APP_HOUR_EVENT,
    APP_INTERVAL_EVENT,
)
from healthmes.store import WellnessEvent


def _hour_record(
    *,
    source_record_id: str,
    app_id: str,
    seconds: int,
) -> dict:
    return {
        "kind": "app_hour",
        "source_record_id": source_record_id,
        "bucket_start": "2026-08-01T10:00:00Z",
        "app_id": app_id,
        "foreground_seconds": seconds,
        "launches": 2,
        "category": "productivity",
        "coverage_seconds": 3600,
    }


def _activity_batch(records: list[dict]) -> dict:
    return {
        "source_provider": "api-test-collector",
        "source_device": "desktop-api-test",
        "platform": "macos",
        "capability": "aggregate",
        "timezone": "UTC",
        "collected_at": "2026-08-01T11:00:00Z",
        "records": records,
    }


def test_collection_settings_filter_raw_events_and_context_identity(client, session) -> None:
    configured = client.put(
        "/v1/activity/devices/desktop-api-test/collection",
        json={
            "platform": "macos",
            "excluded_apps": ["private.app"],
        },
    )
    assert configured.status_code == 200
    assert configured.json()["config_revision"] == 1

    ingested = client.post(
        "/v1/activity/events/batch",
        json=_activity_batch(
            [
                _hour_record(
                    source_record_id="private",
                    app_id="PRIVATE.APP",
                    seconds=1200,
                ),
                _hour_record(
                    source_record_id="allowed",
                    app_id="editor.app",
                    seconds=1800,
                ),
            ]
        ),
    )

    assert ingested.status_code == 200
    assert ingested.json()["accepted"] == 1
    assert ingested.json()["excluded"] == 1
    rows = list(
        session.scalars(select(WellnessEvent).where(WellnessEvent.event_type == APP_HOUR_EVENT))
    )
    assert [row.payload["app_id"] for row in rows] == ["editor.app"]

    summary = client.get(
        "/v1/activity/summary",
        params={"date": "2026-08-01", "timezone": "UTC"},
    )
    assert summary.status_code == 200
    assert summary.json()["total_active_minutes"] == 30.0
    assert "private.app" not in summary.text.casefold()
    assert "editor.app" not in summary.text.casefold()


def test_pause_and_resume_control_the_same_ingest_contract(client) -> None:
    until = datetime.now(UTC) + timedelta(hours=1)
    paused = client.post(
        "/v1/activity/devices/desktop-api-test/pause",
        json={"until": until.isoformat()},
    )
    assert paused.status_code == 200
    assert paused.json()["effective_collecting"] is False
    assert paused.json()["blocked_reason"] == "collection_paused"

    blocked = client.post(
        "/v1/activity/events/batch",
        json=_activity_batch(
            [
                _hour_record(
                    source_record_id="paused",
                    app_id="editor.app",
                    seconds=100,
                )
            ]
        ),
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "activity_collection_blocked"

    resumed = client.post("/v1/activity/devices/desktop-api-test/resume")
    assert resumed.status_code == 200
    assert resumed.json()["effective_collecting"] is True

    accepted = client.post(
        "/v1/activity/events/batch",
        json=_activity_batch(
            [
                _hour_record(
                    source_record_id="resumed",
                    app_id="editor.app",
                    seconds=100,
                )
            ]
        ),
    )
    assert accepted.status_code == 200
    assert accepted.json()["created"] == 1


def test_activity_ingest_rejects_internal_provider_namespace(client) -> None:
    payload = _activity_batch(
        [
            _hour_record(
                source_record_id="spoofed-summary",
                app_id="editor.app",
                seconds=100,
            )
        ]
    )
    payload["source_provider"] = "healthmes-activity-aggregator"

    response = client.post("/v1/activity/events/batch", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_ios_unavailable_is_status_not_fake_zero_activity(client, session) -> None:
    response = client.post(
        "/v1/activity/ios/report",
        json={
            "device_id": "iphone-api-test",
            "timezone": "Asia/Seoul",
            "capability": "unavailable",
            "permission_status": "unavailable",
            "reason": "screen_time_export_not_available",
            "samples": [],
        },
    )

    assert response.status_code == 200
    assert response.json()["accepted"] == 0
    assert (
        session.scalar(select(WellnessEvent).where(WellnessEvent.event_type == APP_HOUR_EVENT))
        is None
    )
    status = client.get("/v1/activity/devices/iphone-api-test/collection")
    assert status.json()["capability"] == "unavailable"
    assert status.json()["permission_status"] == "unavailable"
    assert status.json()["effective_collecting"] is False
    assert status.json()["last_uploaded_at"] is not None
    assert status.json()["last_collected_at"] is None


def test_ios_aggregate_hour_can_be_revised_without_duplicate_rows(client, session) -> None:
    payload = {
        "device_id": "iphone-api-test",
        "timezone": "Asia/Seoul",
        "capability": "aggregate",
        "permission_status": "granted",
        "samples": [
            {
                "source_record_id": "screen-time-hour-1",
                "bucket_start": "2026-08-01T10:00:00Z",
                "foreground_seconds": 600,
                "category": "social",
                "launches": 4,
                "coverage_seconds": 3600,
            }
        ],
    }
    first = client.post("/v1/activity/ios/report", json=payload)
    revised = {
        **payload,
        "samples": [
            {
                **payload["samples"][0],
                "foreground_seconds": 900,
            }
        ],
    }
    second = client.post("/v1/activity/ios/report", json=revised)

    assert first.status_code == 200
    assert first.json()["created"] == 1
    assert second.status_code == 200
    assert second.json()["updated"] == 1
    rows = list(
        session.scalars(select(WellnessEvent).where(WellnessEvent.event_type == APP_HOUR_EVENT))
    )
    assert len(rows) == 1
    assert rows[0].payload["foreground_seconds"] == 900
    assert rows[0].payload["app_id"] == "category:social"


def test_partial_manual_delete_rebuilds_the_day_summary(client, session) -> None:
    created = client.post(
        "/v1/activity/events/batch",
        json={
            "source_provider": "api-test-collector",
            "source_device": "desktop-delete-test",
            "platform": "macos",
            "capability": "detailed",
            "timezone": "UTC",
            "records": [
                {
                    "kind": "app_interval",
                    "source_record_id": "keep",
                    "start_at": "2026-08-01T09:00:00Z",
                    "end_at": "2026-08-01T09:30:00Z",
                    "state": "active",
                    "app_id": "editor",
                },
                {
                    "kind": "app_interval",
                    "source_record_id": "delete",
                    "start_at": "2026-08-01T10:00:00Z",
                    "end_at": "2026-08-01T10:30:00Z",
                    "state": "active",
                    "app_id": "browser",
                },
            ],
        },
    )
    assert created.status_code == 200

    deleted = client.post(
        "/v1/activity/data/delete",
        json={
            "device_id": "desktop-delete-test",
            "start": "2026-08-01T10:15:00Z",
            "end": "2026-08-01T10:20:00Z",
            "include_summaries": True,
            "include_control": False,
            "confirm": True,
        },
    )
    summary = client.get(
        "/v1/activity/summary",
        params={"date": "2026-08-01", "timezone": "UTC"},
    )

    assert deleted.status_code == 200
    assert deleted.json()["raw_events_deleted"] == 1
    assert summary.json()["total_active_minutes"] == 30.0
    remaining = list(
        session.scalars(select(WellnessEvent).where(WellnessEvent.event_type == APP_INTERVAL_EVENT))
    )
    assert [row.source_record_id for row in remaining] == ["keep"]


def test_raw_only_manual_delete_cannot_leave_a_stale_summary(client) -> None:
    created = client.post(
        "/v1/activity/events/batch",
        json={
            "source_provider": "api-test-collector",
            "source_device": "desktop-raw-only-delete",
            "platform": "macos",
            "capability": "detailed",
            "timezone": "UTC",
            "records": [
                {
                    "kind": "app_interval",
                    "source_record_id": "keep",
                    "start_at": "2026-08-01T09:00:00Z",
                    "end_at": "2026-08-01T09:30:00Z",
                    "state": "active",
                    "app_id": "editor",
                },
                {
                    "kind": "app_interval",
                    "source_record_id": "delete",
                    "start_at": "2026-08-01T10:00:00Z",
                    "end_at": "2026-08-01T10:30:00Z",
                    "state": "active",
                    "app_id": "browser",
                },
            ],
        },
    )
    assert created.status_code == 200

    deleted = client.post(
        "/v1/activity/data/delete",
        json={
            "device_id": "desktop-raw-only-delete",
            "start": "2026-08-01T10:15:00Z",
            "end": "2026-08-01T10:20:00Z",
            "include_summaries": False,
            "include_control": False,
            "confirm": True,
        },
    )
    summary = client.get(
        "/v1/activity/summary",
        params={"date": "2026-08-01", "timezone": "UTC"},
    )

    assert deleted.status_code == 200
    assert deleted.json()["summary_events_deleted"] == 0
    assert summary.json()["total_active_minutes"] == 30.0


def test_activitywatch_import_rejects_half_open_explicit_range_before_network(client) -> None:
    response = client.post(
        "/v1/activity/activitywatch/import",
        json={
            "device_id": "mac-api-test",
            "platform": "macos",
            "timezone": "UTC",
            "start_at": "2026-08-01T10:00:00Z",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
