from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from healthmes.activity.activitywatch import (
    ActivityWatchError,
    import_activitywatch,
    normalize_activitywatch_events,
    validate_loopback_base_url,
)
from healthmes.activity.android import android_source_record_id
from healthmes.activity.contracts import (
    ActivityCollectionUpdate,
    ActivityPlatform,
    ActivityWatchImportRequest,
    IOSCapabilityReport,
)
from healthmes.activity.repository import (
    APP_INTERVAL_EVENT,
    get_control_payload,
    update_collection_config,
)
from healthmes.activity.service import ActivityCollectionBlockedError
from healthmes.store import WellnessEvent


def _window_event(*, title: str, event_id: int | None = None) -> dict:
    event = {
        "timestamp": "2026-08-01T10:00:00Z",
        "duration": 3600,
        "data": {
            "app": "Code",
            "title": title,
        },
    }
    if event_id is not None:
        event["id"] = event_id
    return event


def test_activitywatch_discards_title_before_identity_or_payload_creation() -> None:
    first = normalize_activitywatch_events(
        device_id="mac-1",
        window_bucket_id="aw-watcher-window_mac",
        window_events=[_window_event(title="Secret customer document")],
        afk_bucket_id=None,
        afk_events=[],
    )
    renamed = normalize_activitywatch_events(
        device_id="mac-1",
        window_bucket_id="aw-watcher-window_mac",
        window_events=[_window_event(title="Different private title")],
        afk_bucket_id=None,
        afk_events=[],
    )

    assert len(first) == 1
    assert first[0].source_record_id == renamed[0].source_record_id
    assert "secret" not in first[0].model_dump_json().casefold()
    assert "title" not in first[0].model_dump_json().casefold()


def test_activitywatch_source_ids_are_namespaced_per_device() -> None:
    first = normalize_activitywatch_events(
        device_id="mac-1",
        window_bucket_id="window",
        window_events=[_window_event(title="same", event_id=1)],
        afk_bucket_id=None,
        afk_events=[],
    )
    second = normalize_activitywatch_events(
        device_id="mac-2",
        window_bucket_id="window",
        window_events=[_window_event(title="same", event_id=1)],
        afk_bucket_id=None,
        afk_events=[],
    )

    assert first[0].source_record_id != second[0].source_record_id


def test_activitywatch_afk_intersection_produces_active_and_idle_intervals() -> None:
    records = normalize_activitywatch_events(
        device_id="mac-1",
        window_bucket_id="window",
        window_events=[_window_event(title="ignored", event_id=1)],
        afk_bucket_id="afk",
        afk_events=[
            {
                "id": 2,
                "timestamp": "2026-08-01T10:00:00Z",
                "duration": 1800,
                "data": {"status": "not-afk"},
            },
            {
                "id": 3,
                "timestamp": "2026-08-01T10:30:00Z",
                "duration": 900,
                "data": {"status": "afk"},
            },
        ],
    )

    observed = [
        (row.state.value, int((row.end_at - row.start_at).total_seconds())) for row in records
    ]
    assert observed == [
        ("active", 1800),
        ("idle", 900),
    ]


@pytest.mark.parametrize(
    "platform",
    (
        ActivityPlatform.MACOS,
        ActivityPlatform.WINDOWS,
        ActivityPlatform.LINUX,
    ),
)
def test_activitywatch_import_normalizes_all_desktop_platforms(
    session,
    platform: ActivityPlatform,
) -> None:
    class FakeClient:
        def list_buckets(self):
            return {
                "window": {"type": "currentwindow"},
                "afk": {"type": "afkstatus"},
            }

        def get_events(self, bucket_id, *, start, end):
            if bucket_id == "window":
                return [_window_event(title="never stored", event_id=1)]
            return [
                {
                    "id": 2,
                    "timestamp": "2026-08-01T10:00:00Z",
                    "duration": 3600,
                    "data": {"status": "not-afk"},
                }
            ]

    result = import_activitywatch(
        session,
        ActivityWatchImportRequest(
            device_id=f"{platform.value}-device",
            platform=platform,
            timezone="UTC",
            start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
            end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
        ),
        client=FakeClient(),
        now=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )
    raw = session.scalar(
        select(WellnessEvent).where(WellnessEvent.event_type == APP_INTERVAL_EVENT)
    )
    control = get_control_payload(session, f"{platform.value}-device")

    assert result.response.created == 1
    assert raw is not None
    assert raw.payload["platform"] == platform.value
    assert raw.payload["app_id"] == "Code"
    assert "title" not in str(raw.payload).casefold()
    assert control["cursors"]["activitywatch:window"] == "2026-08-01T11:00:00+00:00"


def test_activitywatch_checks_privacy_gate_before_localhost_read(session) -> None:
    class MustNotReadClient:
        def list_buckets(self):
            raise AssertionError("privacy gate must run before ActivityWatch is read")

    update_collection_config(
        session,
        "mac-1",
        ActivityCollectionUpdate(enabled=False),
    )

    with pytest.raises(ActivityCollectionBlockedError) as raised:
        import_activitywatch(
            session,
            ActivityWatchImportRequest(
                device_id="mac-1",
                platform=ActivityPlatform.MACOS,
                timezone="UTC",
                start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
            ),
            client=MustNotReadClient(),
            now=datetime(2026, 8, 1, 11, tzinfo=UTC),
        )

    assert raised.value.reason == "collection_disabled"


@pytest.mark.parametrize(
    "value",
    (
        "https://127.0.0.1:5600",
        "http://activitywatch.example:5600",
        "http://192.168.1.20:5600",
    ),
)
def test_activitywatch_rejects_non_loopback_or_https_urls(value: str) -> None:
    with pytest.raises(ActivityWatchError):
        validate_loopback_base_url(value)


def test_activitywatch_requires_a_complete_explicit_range() -> None:
    with pytest.raises(ValidationError):
        ActivityWatchImportRequest(
            device_id="mac-1",
            platform=ActivityPlatform.MACOS,
            timezone="UTC",
            start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
        )


def test_android_source_identity_is_stable_and_device_scoped() -> None:
    bucket = datetime(2026, 8, 1, 10, tzinfo=UTC)

    first = android_source_record_id("pixel-1", bucket, "com.example.app")
    repeated = android_source_record_id("pixel-1", bucket, "com.example.app")
    other_device = android_source_record_id("pixel-2", bucket, "com.example.app")

    assert first == repeated
    assert first != other_device
    assert "pixel-1" not in first
    assert "com.example.app" not in first


def test_ios_contract_reports_unavailable_without_fake_samples() -> None:
    report = IOSCapabilityReport(
        device_id="iphone-1",
        timezone="Asia/Seoul",
        capability="unavailable",
        permission_status="unavailable",
        reason="screen_time_export_not_available",
    )

    assert report.samples == []

    with pytest.raises(ValidationError):
        IOSCapabilityReport(
            device_id="iphone-1",
            timezone="Asia/Seoul",
            capability="unavailable",
            permission_status="unavailable",
            samples=[
                {
                    "source_record_id": "fake",
                    "bucket_start": "2026-08-01T10:00:00Z",
                    "foreground_seconds": 0,
                    "category": "fake",
                }
            ],
        )
