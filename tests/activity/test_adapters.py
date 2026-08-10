from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from healthmes.activity.activitywatch import (
    ActivityWatchClient,
    ActivityWatchError,
    StaleActivityWatchImportError,
    import_activitywatch,
    normalize_activitywatch_events,
    prepare_activitywatch_import,
    validate_loopback_base_url,
)
from healthmes.activity.aggregation import (
    get_daily_summary,
    rebuild_day_summaries,
)
from healthmes.activity.android import (
    android_source_record_id,
    backfill_android_canonical_events,
    ingest_android_samples,
)
from healthmes.activity.contracts import (
    ActivityCapability,
    ActivityCollectionStatusUpdate,
    ActivityCollectionUpdate,
    ActivityPermissionStatus,
    ActivityPlatform,
    ActivityWatchImportRequest,
    IOSCapabilityReport,
)
from healthmes.activity.maintenance import delete_activity_data
from healthmes.activity.repository import (
    APP_HOUR_EVENT,
    APP_INTERVAL_EVENT,
    DAY_SUMMARY_EVENT,
    get_activitywatch_import_fence,
    get_control_payload,
    update_collection_config,
    update_collection_status,
)
from healthmes.activity.service import (
    ActivityCollectionBlockedError,
    ActivitySummaryProvenanceError,
)
from healthmes.storage import update_retention_policy
from healthmes.store import AppUsageSample, WellnessEvent


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


def test_activitywatch_client_translates_malformed_json() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"{")
    )
    client = ActivityWatchClient(
        "http://127.0.0.1:5600",
        transport=transport,
    )

    with pytest.raises(ActivityWatchError, match="not valid JSON"):
        client.list_buckets()


def test_activitywatch_client_ignores_environment_proxy(
    monkeypatch,
) -> None:
    real_client = httpx.Client
    observed_trust_env: list[object] = []

    def client_factory(*args, **kwargs):
        observed_trust_env.append(kwargs.get("trust_env"))
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(200, json={})
        )
        return real_client(*args, **kwargs)

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setattr(
        "healthmes.activity.activitywatch.httpx.Client",
        client_factory,
    )

    buckets = ActivityWatchClient(
        "http://127.0.0.1:5600",
    ).list_buckets()

    assert buckets == {}
    assert observed_trust_env == [False]


@pytest.mark.parametrize("explicit_bucket_ids", (False, True))
def test_activitywatch_bucket_ids_are_one_encoded_url_segment(
    session,
    explicit_bucket_ids,
) -> None:
    window_bucket = "window/../../settings?probe=1#fragment%2F"
    afk_bucket = "afk/../status?probe=2#fragment%2F"
    expected_paths = {
        f"/api/0/buckets/{quote(window_bucket, safe='')}/events",
        f"/api/0/buckets/{quote(afk_bucket, safe='')}/events",
    }
    event_paths: list[str] = []
    event_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raw_path = request.url.raw_path.split(b"?", 1)[0].decode("ascii")
        if raw_path == "/api/0/buckets/":
            return httpx.Response(
                200,
                json={
                    window_bucket: {"type": "currentwindow"},
                    afk_bucket: {"type": "afkstatus"},
                },
            )
        event_paths.append(raw_path)
        event_queries.append(request.url.query.decode("ascii"))
        if raw_path == f"/api/0/buckets/{quote(window_bucket, safe='')}/events":
            return httpx.Response(
                200,
                json=[_window_event(title="discarded", event_id=1)],
            )
        if raw_path == f"/api/0/buckets/{quote(afk_bucket, safe='')}/events":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 2,
                        "timestamp": "2026-08-01T10:00:00Z",
                        "duration": 3600,
                        "data": {"status": "not-afk"},
                    }
                ],
            )
        return httpx.Response(404)

    result = import_activitywatch(
        session,
        ActivityWatchImportRequest(
            device_id=f"mac-encoded-{explicit_bucket_ids}",
            platform=ActivityPlatform.MACOS,
            timezone="UTC",
            start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
            end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
            window_bucket_id=(
                window_bucket if explicit_bucket_ids else None
            ),
            afk_bucket_id=afk_bucket if explicit_bucket_ids else None,
        ),
        client=ActivityWatchClient(
            "http://127.0.0.1:5600",
            transport=httpx.MockTransport(handler),
        ),
        now=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )

    assert result.response.accepted == 1
    assert set(event_paths) == expected_paths
    assert all("probe=" not in query for query in event_queries)
    assert all("fragment" not in query for query in event_queries)


@pytest.mark.parametrize("explicit_bucket_ids", (False, True))
@pytest.mark.parametrize("invalid_bucket_id", (".", ".."))
def test_activitywatch_rejects_dot_only_bucket_ids_before_event_read(
    session,
    explicit_bucket_ids,
    invalid_bucket_id,
) -> None:
    event_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal event_reads
        raw_path = request.url.raw_path.split(b"?", 1)[0].decode("ascii")
        if raw_path == "/api/0/buckets/":
            return httpx.Response(
                200,
                json={
                    invalid_bucket_id: {"type": "currentwindow"},
                    "afk": {"type": "afkstatus"},
                },
            )
        event_reads += 1
        return httpx.Response(500)

    with pytest.raises(
        ActivityWatchError,
        match="one non-dot path segment",
    ):
        import_activitywatch(
            session,
            ActivityWatchImportRequest(
                device_id=(
                    f"mac-dot-bucket-{explicit_bucket_ids}-"
                    f"{len(invalid_bucket_id)}"
                ),
                platform=ActivityPlatform.MACOS,
                timezone="UTC",
                start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
                window_bucket_id=(
                    invalid_bucket_id if explicit_bucket_ids else None
                ),
                afk_bucket_id="afk" if explicit_bucket_ids else None,
            ),
            client=ActivityWatchClient(
                "http://127.0.0.1:5600",
                transport=httpx.MockTransport(handler),
            ),
            now=datetime(2026, 8, 1, 11, tzinfo=UTC),
        )

    assert event_reads == 0


@pytest.mark.parametrize(
    "window_event",
    (
        {
            "id": 1,
            "timestamp": "not-a-time",
            "duration": 3600,
            "data": {"app": "Code"},
        },
        {
            "id": 1,
            "timestamp": "2026-08-01T10:00:00Z",
            "duration": "bad",
            "data": {"app": "Code"},
        },
        {
            "id": 1,
            "timestamp": "2026-08-01T10:00:00Z",
            "duration": 10**10_000,
            "data": {"app": "Code"},
        },
        {
            "id": 1,
            "timestamp": "2026-08-01T10:00:00Z",
            "duration": 3600,
            "data": {},
        },
    ),
)
def test_activitywatch_rejects_malformed_window_events(
    window_event: dict,
) -> None:
    with pytest.raises(ActivityWatchError, match="window event is malformed"):
        normalize_activitywatch_events(
            device_id="mac-malformed",
            window_bucket_id="window",
            window_events=[window_event],
            afk_bucket_id=None,
            afk_events=[],
        )


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


def test_activitywatch_unions_overlapping_not_afk_events() -> None:
    records = normalize_activitywatch_events(
        device_id="mac-1",
        window_bucket_id="window",
        window_events=[_window_event(title="ignored", event_id=1)],
        afk_bucket_id="afk",
        afk_events=[
            {
                "id": 2,
                "timestamp": "2026-08-01T10:00:00Z",
                "duration": 3600,
                "data": {"status": "not-afk"},
            },
            {
                "id": 3,
                "timestamp": "2026-08-01T10:30:00Z",
                "duration": 1800,
                "data": {"status": "not-afk"},
            },
        ],
    )

    assert len(records) == 1
    assert records[0].start_at == datetime(2026, 8, 1, 10, tzinfo=UTC)
    assert records[0].end_at == datetime(2026, 8, 1, 11, tzinfo=UTC)


def test_activitywatch_rejects_conflicting_afk_states() -> None:
    with pytest.raises(ActivityWatchError, match="conflicting overlapping states"):
        normalize_activitywatch_events(
            device_id="mac-1",
            window_bucket_id="window",
            window_events=[_window_event(title="ignored", event_id=1)],
            afk_bucket_id="afk",
            afk_events=[
                {
                    "id": 2,
                    "timestamp": "2026-08-01T10:00:00Z",
                    "duration": 3600,
                    "data": {"status": "not-afk"},
                },
                {
                    "id": 3,
                    "timestamp": "2026-08-01T10:30:00Z",
                    "duration": 1800,
                    "data": {"status": "afk"},
                },
            ],
        )


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
    ("boundary_change", "expected_reason"),
    (
        ("disable", "collection_disabled"),
        ("revoke", "permission_revoked"),
    ),
)
def test_activitywatch_rechecks_privacy_boundary_after_localhost_read(
    session,
    boundary_change,
    expected_reason,
) -> None:
    device_id = f"mac-race-{boundary_change}"

    class BoundaryChangingClient:
        changed = False

        def list_buckets(self):
            return {
                "window": {"type": "currentwindow"},
                "afk": {"type": "afkstatus"},
            }

        def get_events(self, bucket_id, *, start, end):
            if not self.changed:
                self.changed = True
                if boundary_change == "disable":
                    update_collection_config(
                        session,
                        device_id,
                        ActivityCollectionUpdate(enabled=False),
                    )
                else:
                    update_collection_status(
                        session,
                        device_id,
                        ActivityCollectionStatusUpdate(
                            platform=ActivityPlatform.MACOS,
                            capability=ActivityCapability.DETAILED,
                            permission_status=ActivityPermissionStatus.REVOKED,
                            status_observed_at=datetime(
                                2026,
                                8,
                                1,
                                10,
                                30,
                                tzinfo=UTC,
                            ),
                        ),
                        now=datetime(2026, 8, 1, 10, 30, tzinfo=UTC),
                    )
            if bucket_id == "window":
                return [_window_event(title="must not be stored", event_id=1)]
            return [
                {
                    "id": 2,
                    "timestamp": "2026-08-01T10:00:00Z",
                    "duration": 3600,
                    "data": {"status": "not-afk"},
                }
            ]

    with pytest.raises(ActivityCollectionBlockedError) as raised:
        import_activitywatch(
            session,
            ActivityWatchImportRequest(
                device_id=device_id,
                platform=ActivityPlatform.MACOS,
                timezone="UTC",
                start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
            ),
            client=BoundaryChangingClient(),
            now=datetime(2026, 8, 1, 11, tzinfo=UTC),
        )

    assert raised.value.reason == expected_reason
    state = get_control_payload(session, device_id)
    assert state["cursors"] == {}
    assert state["last_uploaded_at"] is None
    if boundary_change == "disable":
        assert state["enabled"] is False
    else:
        assert state["permission_status"] == "revoked"
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type.in_(
                    (APP_INTERVAL_EVENT, DAY_SUMMARY_EVENT)
                )
            )
        )
        is None
    )


def test_activitywatch_latest_started_snapshot_wins_if_it_finishes_first(
    session,
) -> None:
    class SnapshotClient:
        def __init__(self, *, include_window: bool) -> None:
            self.include_window = include_window

        def list_buckets(self):
            return {
                "window": {"type": "currentwindow"},
                "afk": {"type": "afkstatus"},
            }

        def get_events(self, bucket_id, *, start, end):
            if bucket_id == "window":
                if not self.include_window:
                    return []
                return [
                    {
                        "id": 1,
                        "timestamp": "2026-08-01T10:00:00Z",
                        "duration": 3600,
                        "data": {"app": "Code", "title": "discarded"},
                    }
                ]
            return [
                {
                    "id": 2,
                    "timestamp": start.isoformat(),
                    "duration": (end - start).total_seconds(),
                    "data": {"status": "not-afk"},
                }
            ]

    request = ActivityWatchImportRequest(
        device_id="mac-latest-started-wins",
        platform=ActivityPlatform.MACOS,
        timezone="UTC",
        start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
        end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )
    old_snapshot = prepare_activitywatch_import(
        session,
        request,
        client=SnapshotClient(include_window=True),
        now=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )
    latest_snapshot = prepare_activitywatch_import(
        session,
        request,
        client=SnapshotClient(include_window=False),
        now=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )

    latest = import_activitywatch(
        session,
        request,
        prepared=latest_snapshot,
    )
    session.commit()
    with pytest.raises(StaleActivityWatchImportError):
        import_activitywatch(
            session,
            request,
            prepared=old_snapshot,
        )

    state = get_control_payload(session, request.device_id)
    stored = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type.in_(
                    (APP_INTERVAL_EVENT, DAY_SUMMARY_EVENT)
                )
            )
        )
    )

    assert latest.response.accepted == 0
    assert stored == []
    assert state["cursors"]["activitywatch:window"] == (
        "2026-08-01T11:00:00+00:00"
    )
    assert "sequence" not in state


def test_activitywatch_control_delete_preserves_and_advances_import_fence(
    session,
) -> None:
    class SnapshotClient:
        def list_buckets(self):
            return {
                "window": {"type": "currentwindow"},
                "afk": {"type": "afkstatus"},
            }

        def get_events(self, bucket_id, *, start, end):
            if bucket_id == "window":
                return [
                    {
                        "id": 1,
                        "timestamp": "2026-08-01T10:00:00Z",
                        "duration": 3600,
                        "data": {"app": "Code", "title": "discarded"},
                    }
                ]
            return [
                {
                    "id": 2,
                    "timestamp": start.isoformat(),
                    "duration": (end - start).total_seconds(),
                    "data": {"status": "not-afk"},
                }
            ]

    request = ActivityWatchImportRequest(
        device_id="mac-delete-fence",
        platform=ActivityPlatform.MACOS,
        timezone="UTC",
        start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
        end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )
    prepared = prepare_activitywatch_import(
        session,
        request,
        client=SnapshotClient(),
        now=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )
    report = delete_activity_data(
        session,
        device_id=request.device_id,
        start=None,
        end=None,
        include_summaries=False,
        include_control=True,
        now=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )
    session.commit()

    assert report.control_events_deleted == 0
    assert get_activitywatch_import_fence(
        session,
        request.device_id,
    ) > prepared.import_sequence
    with pytest.raises(StaleActivityWatchImportError):
        import_activitywatch(
            session,
            request,
            prepared=prepared,
        )
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_INTERVAL_EVENT
            )
        )
        is None
    )


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
    other_generation = android_source_record_id(
        "pixel-1",
        bucket,
        "com.example.app",
        1,
    )

    assert first == repeated
    assert first != other_device
    assert first != other_generation
    assert ":0:" not in first
    assert ":1:" in other_generation
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


def test_activitywatch_missing_afk_events_does_not_advance_cursor(session) -> None:
    class MissingAfkClient:
        def list_buckets(self):
            return {
                "window": {"type": "currentwindow"},
                "afk": {"type": "afkstatus"},
            }

        def get_events(self, bucket_id, *, start, end):
            if bucket_id == "window":
                return [_window_event(title="never stored", event_id=1)]
            return []

    with pytest.raises(ActivityWatchError, match="AFK coverage is unavailable"):
        import_activitywatch(
            session,
            ActivityWatchImportRequest(
                device_id="mac-missing-afk",
                platform=ActivityPlatform.MACOS,
                timezone="UTC",
                start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
            ),
            client=MissingAfkClient(),
            now=datetime(2026, 8, 1, 11, tzinfo=UTC),
        )

    control = get_control_payload(session, "mac-missing-afk")
    assert control["cursors"] == {}
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_INTERVAL_EVENT
            )
        )
        is None
    )


@pytest.mark.parametrize(
    "afk_events",
    (
        [
            {
                "id": 2,
                "timestamp": "2026-08-01T10:00:00Z",
                "duration": 300,
                "data": {"status": "not-afk"},
            }
        ],
        [
            {
                "id": 2,
                "timestamp": "2026-08-01T10:00:00Z",
                "duration": 1800,
                "data": {"status": "not-afk"},
            },
            {
                "id": 3,
                "timestamp": "2026-08-01T10:35:00Z",
                "duration": 1500,
                "data": {"status": "afk"},
            },
        ],
    ),
    ids=("partial-tail", "middle-gap"),
)
def test_activitywatch_incomplete_afk_coverage_does_not_advance_cursor(
    session,
    afk_events,
) -> None:
    class IncompleteAfkClient:
        def list_buckets(self):
            return {
                "window": {"type": "currentwindow"},
                "afk": {"type": "afkstatus"},
            }

        def get_events(self, bucket_id, *, start, end):
            if bucket_id == "window":
                return [_window_event(title="never stored", event_id=1)]
            return afk_events

    with pytest.raises(ActivityWatchError, match="AFK coverage is unavailable"):
        import_activitywatch(
            session,
            ActivityWatchImportRequest(
                device_id="mac-incomplete-afk",
                platform=ActivityPlatform.MACOS,
                timezone="UTC",
                start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
            ),
            client=IncompleteAfkClient(),
            now=datetime(2026, 8, 1, 11, tzinfo=UTC),
        )

    assert get_control_payload(session, "mac-incomplete-afk")["cursors"] == {}
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_INTERVAL_EVENT
            )
        )
        is None
    )


def test_activitywatch_missing_afk_bucket_does_not_read_or_advance(session) -> None:
    class MissingAfkBucketClient:
        def list_buckets(self):
            return {"window": {"type": "currentwindow"}}

        def get_events(self, bucket_id, *, start, end):
            raise AssertionError("missing AFK bucket must fail before event reads")

    with pytest.raises(ActivityWatchError, match="AFK bucket is unavailable"):
        import_activitywatch(
            session,
            ActivityWatchImportRequest(
                device_id="mac-no-afk-bucket",
                platform=ActivityPlatform.MACOS,
                timezone="UTC",
                start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
            ),
            client=MissingAfkBucketClient(),
            now=datetime(2026, 8, 1, 11, tzinfo=UTC),
        )

    assert get_control_payload(session, "mac-no-afk-bucket")["cursors"] == {}


def test_activitywatch_chooses_the_most_recent_matching_bucket(session) -> None:
    class MultiBucketClient:
        def list_buckets(self):
            return {
                "window-old": {
                    "type": "currentwindow",
                    "last_updated": "2026-08-01T09:00:00Z",
                },
                "window-new": {
                    "type": "currentwindow",
                    "last_updated": "2026-08-01T11:00:00Z",
                },
                "afk": {"type": "afkstatus"},
            }

        def get_events(self, bucket_id, *, start, end):
            if bucket_id == "afk":
                return [
                    {
                        "id": 3,
                        "timestamp": "2026-08-01T10:00:00Z",
                        "duration": 3600,
                        "data": {"status": "not-afk"},
                    }
                ]
            app = "Old" if bucket_id == "window-old" else "New"
            return [
                {
                    "id": 1,
                    "timestamp": "2026-08-01T10:00:00Z",
                    "duration": 3600,
                    "data": {"app": app, "title": "discarded"},
                }
            ]

    import_activitywatch(
        session,
        ActivityWatchImportRequest(
            device_id="mac-newest-bucket",
            platform=ActivityPlatform.MACOS,
            timezone="UTC",
            start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
            end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
        ),
        client=MultiBucketClient(),
        now=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )

    raw = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == APP_INTERVAL_EVENT
        )
    )
    assert raw is not None
    assert raw.payload["app_id"] == "New"
    assert (
        get_control_payload(session, "mac-newest-bucket")["cursors"][
            "activitywatch:window-new"
        ]
        == "2026-08-01T11:00:00+00:00"
    )


def test_activitywatch_rejects_future_end_before_reading_events(session) -> None:
    class FutureRangeClient:
        def list_buckets(self):
            raise AssertionError("future range must fail before source discovery")

        def get_events(self, bucket_id, *, start, end):
            raise AssertionError("future range must fail before event reads")

    with pytest.raises(ActivityWatchError, match="cannot be in the future"):
        import_activitywatch(
            session,
            ActivityWatchImportRequest(
                device_id="mac-future",
                platform=ActivityPlatform.MACOS,
                timezone="UTC",
                start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                end_at=datetime(2026, 8, 1, 12, 2, tzinfo=UTC),
            ),
            client=FutureRangeClient(),
            now=datetime(2026, 8, 1, 12, tzinfo=UTC),
        )


def test_activitywatch_clips_source_spans_to_the_requested_window() -> None:
    start = datetime(2026, 8, 1, 10, tzinfo=UTC)
    end = datetime(2026, 8, 1, 11, tzinfo=UTC)
    records = normalize_activitywatch_events(
        device_id="mac-clipped",
        window_bucket_id="window",
        window_events=[
            {
                "id": 1,
                "timestamp": "2026-08-01T09:30:00Z",
                "duration": 7200,
                "data": {"app": "Code", "title": "discarded"},
            }
        ],
        afk_bucket_id="afk",
        afk_events=[
            {
                "id": 2,
                "timestamp": "2026-08-01T09:00:00Z",
                "duration": 5400,
                "data": {"status": "not-afk"},
            },
            {
                "id": 3,
                "timestamp": "2026-08-01T10:30:00Z",
                "duration": 3600,
                "data": {"status": "afk"},
            },
        ],
        range_start=start,
        range_end=end,
    )
    repeated = normalize_activitywatch_events(
        device_id="mac-clipped",
        window_bucket_id="window",
        window_events=[
            {
                "id": 1,
                "timestamp": "2026-08-01T09:30:00Z",
                "duration": 7200,
                "data": {"app": "Code"},
            }
        ],
        afk_bucket_id="afk",
        afk_events=[
            {
                "id": 2,
                "timestamp": "2026-08-01T09:00:00Z",
                "duration": 5400,
                "data": {"status": "not-afk"},
            },
            {
                "id": 3,
                "timestamp": "2026-08-01T10:30:00Z",
                "duration": 3600,
                "data": {"status": "afk"},
            },
        ],
        range_start=start,
        range_end=end,
    )
    shifted = normalize_activitywatch_events(
        device_id="mac-clipped",
        window_bucket_id="window",
        window_events=[
            {
                "id": 1,
                "timestamp": "2026-08-01T09:30:00Z",
                "duration": 7200,
                "data": {"app": "Code"},
            }
        ],
        afk_bucket_id="afk",
        afk_events=[
            {
                "id": 2,
                "timestamp": "2026-08-01T09:00:00Z",
                "duration": 5400,
                "data": {"status": "not-afk"},
            },
            {
                "id": 3,
                "timestamp": "2026-08-01T10:30:00Z",
                "duration": 3600,
                "data": {"status": "afk"},
            },
        ],
        range_start=start + timedelta(minutes=15),
        range_end=end,
    )

    assert [(record.start_at, record.end_at) for record in records] == [
        (start, datetime(2026, 8, 1, 10, 30, tzinfo=UTC)),
        (datetime(2026, 8, 1, 10, 30, tzinfo=UTC), end),
    ]
    assert records[0].launches == 0
    assert [record.source_record_id for record in repeated] == [
        record.source_record_id for record in records
    ]
    assert [record.source_record_id for record in shifted] == [
        record.source_record_id for record in records
    ]


def test_activitywatch_mutable_duration_replaces_one_canonical_row(session) -> None:
    class MutableClient:
        duration = 1800

        def list_buckets(self):
            return {
                "window": {"type": "currentwindow"},
                "afk": {"type": "afkstatus"},
            }

        def get_events(self, bucket_id, *, start, end):
            if bucket_id == "window":
                return [
                    {
                        "timestamp": "2026-08-01T10:00:00Z",
                        "duration": self.duration,
                        "data": {"app": "Code", "title": "discarded"},
                    }
                ]
            return [
                {
                    "timestamp": "2026-08-01T10:00:00Z",
                    "duration": 3600,
                    "data": {"status": "not-afk"},
                }
            ]

    client = MutableClient()
    request = ActivityWatchImportRequest(
        device_id="mac-mutable",
        platform=ActivityPlatform.MACOS,
        timezone="UTC",
        start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
        end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )
    first = import_activitywatch(
        session,
        request,
        client=client,
        now=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )
    client.duration = 3600
    second = import_activitywatch(
        session,
        request,
        client=client,
        now=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )
    rows = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_INTERVAL_EVENT
            )
        )
    )

    assert first.response.created == 1
    assert second.response.updated == 1
    assert len(rows) == 1
    assert rows[0].payload["end_at"] == "2026-08-01T11:00:00+00:00"


def test_activitywatch_cursor_overlap_does_not_double_count_launches(session) -> None:
    class OverlapClient:
        def list_buckets(self):
            return {
                "window": {"type": "currentwindow"},
                "afk": {"type": "afkstatus"},
            }

        def get_events(self, bucket_id, *, start, end):
            if bucket_id == "window":
                return [
                    {
                        "id": 1,
                        "timestamp": "2026-08-01T09:55:00Z",
                        "duration": 600,
                        "data": {"app": "Code", "title": "discarded"},
                    }
                ]
            return [
                {
                    "id": 2,
                    "timestamp": start.isoformat(),
                    "duration": (end - start).total_seconds(),
                    "data": {"status": "not-afk"},
                }
            ]

    client = OverlapClient()
    import_activitywatch(
        session,
        ActivityWatchImportRequest(
            device_id="mac-overlap-launch",
            platform=ActivityPlatform.MACOS,
            timezone="UTC",
            start_at=datetime(2026, 8, 1, 9, tzinfo=UTC),
            end_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
        ),
        client=client,
        now=datetime(2026, 8, 1, 10, tzinfo=UTC),
    )
    import_activitywatch(
        session,
        ActivityWatchImportRequest(
            device_id="mac-overlap-launch",
            platform=ActivityPlatform.MACOS,
            timezone="UTC",
        ),
        client=client,
        now=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )
    rows = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_INTERVAL_EVENT
            )
        )
    )
    daily = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == DAY_SUMMARY_EVENT
        )
    )

    assert len(rows) == 1
    assert rows[0].payload["launches"] == 1
    assert rows[0].payload["start_at"] == "2026-08-01T09:55:00+00:00"
    assert rows[0].payload["end_at"] == "2026-08-01T10:05:00+00:00"
    assert daily is not None
    assert daily.payload["app_launches_or_switches"] == 1


def test_activitywatch_cursor_overlap_keeps_launch_when_afk_id_changes(
    session,
) -> None:
    class ReidentifiedAfkClient:
        afk_id = 2

        def list_buckets(self):
            return {
                "window": {"type": "currentwindow"},
                "afk": {"type": "afkstatus"},
            }

        def get_events(self, bucket_id, *, start, end):
            if bucket_id == "window":
                return [
                    {
                        "id": 1,
                        "timestamp": "2026-08-01T09:55:00Z",
                        "duration": 600,
                        "data": {"app": "Code", "title": "discarded"},
                    }
                ]
            return [
                {
                    "id": self.afk_id,
                    "timestamp": start.isoformat(),
                    "duration": (end - start).total_seconds(),
                    "data": {"status": "not-afk"},
                }
            ]

    client = ReidentifiedAfkClient()
    import_activitywatch(
        session,
        ActivityWatchImportRequest(
            device_id="mac-overlap-afk-reidentified",
            platform=ActivityPlatform.MACOS,
            timezone="UTC",
            start_at=datetime(2026, 8, 1, 9, tzinfo=UTC),
            end_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
        ),
        client=client,
        now=datetime(2026, 8, 1, 10, tzinfo=UTC),
    )
    client.afk_id = 99
    import_activitywatch(
        session,
        ActivityWatchImportRequest(
            device_id="mac-overlap-afk-reidentified",
            platform=ActivityPlatform.MACOS,
            timezone="UTC",
        ),
        client=client,
        now=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )
    rows = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_INTERVAL_EVENT
            )
        )
    )
    daily = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == DAY_SUMMARY_EVENT
        )
    )

    assert len(rows) == 1
    assert rows[0].payload["launches"] == 1
    assert daily is not None
    assert daily.payload["app_launches_or_switches"] == 1


def test_activitywatch_cursor_overlap_counts_a_newly_discovered_launch(
    session,
) -> None:
    class LateArrivalClient:
        include_window = False

        def list_buckets(self):
            return {
                "window": {"type": "currentwindow"},
                "afk": {"type": "afkstatus"},
            }

        def get_events(self, bucket_id, *, start, end):
            if bucket_id == "window":
                if not self.include_window:
                    return []
                return [
                    {
                        "id": 1,
                        "timestamp": "2026-08-01T09:58:00Z",
                        "duration": 420,
                        "data": {"app": "Code", "title": "discarded"},
                    }
                ]
            return [
                {
                    "id": 2,
                    "timestamp": start.isoformat(),
                    "duration": (end - start).total_seconds(),
                    "data": {"status": "not-afk"},
                }
            ]

    client = LateArrivalClient()
    import_activitywatch(
        session,
        ActivityWatchImportRequest(
            device_id="mac-late-overlap",
            platform=ActivityPlatform.MACOS,
            timezone="UTC",
            start_at=datetime(2026, 8, 1, 9, tzinfo=UTC),
            end_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
        ),
        client=client,
        now=datetime(2026, 8, 1, 10, tzinfo=UTC),
    )
    client.include_window = True
    import_activitywatch(
        session,
        ActivityWatchImportRequest(
            device_id="mac-late-overlap",
            platform=ActivityPlatform.MACOS,
            timezone="UTC",
        ),
        client=client,
        now=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )
    rows = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_INTERVAL_EVENT
            )
        )
    )
    daily = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == DAY_SUMMARY_EVENT
        )
    )

    assert len(rows) == 1
    assert rows[0].payload["launches"] == 1
    assert daily is not None
    assert daily.payload["app_launches_or_switches"] == 1


def test_activitywatch_cursor_overlap_replaces_a_shortened_source_event(
    session,
) -> None:
    class CorrectedClient:
        duration = 3600

        def list_buckets(self):
            return {
                "window": {"type": "currentwindow"},
                "afk": {"type": "afkstatus"},
            }

        def get_events(self, bucket_id, *, start, end):
            if bucket_id == "window":
                return [
                    {
                        "id": 1,
                        "timestamp": "2026-08-01T10:00:00Z",
                        "duration": self.duration,
                        "data": {"app": "Code", "title": "discarded"},
                    }
                ]
            return [
                {
                    "id": 2,
                    "timestamp": start.isoformat(),
                    "duration": (end - start).total_seconds(),
                    "data": {"status": "not-afk"},
                }
            ]

    client = CorrectedClient()
    import_activitywatch(
        session,
        ActivityWatchImportRequest(
            device_id="mac-overlap-correction",
            platform=ActivityPlatform.MACOS,
            timezone="UTC",
            start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
            end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
        ),
        client=client,
        now=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )
    client.duration = 58 * 60
    corrected = import_activitywatch(
        session,
        ActivityWatchImportRequest(
            device_id="mac-overlap-correction",
            platform=ActivityPlatform.MACOS,
            timezone="UTC",
        ),
        client=client,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )
    rows = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_INTERVAL_EVENT
            )
        )
    )
    daily = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == DAY_SUMMARY_EVENT
        )
    )

    assert corrected.response.updated == 1
    assert len(rows) == 1
    assert rows[0].payload["start_at"] == "2026-08-01T10:00:00+00:00"
    assert rows[0].payload["end_at"] == "2026-08-01T10:58:00+00:00"
    assert rows[0].payload["launches"] == 1
    assert daily is not None
    assert daily.payload["total_active_minutes"] == 58.0


def test_activitywatch_explicit_repair_preserves_only_outside_fragments(
    session,
) -> None:
    class RepairClient:
        include_window = True

        def list_buckets(self):
            return {
                "window": {"type": "currentwindow"},
                "afk": {"type": "afkstatus"},
            }

        def get_events(self, bucket_id, *, start, end):
            if bucket_id == "window":
                if not self.include_window:
                    return []
                return [
                    {
                        "id": 1,
                        "timestamp": "2026-08-01T10:00:00Z",
                        "duration": 7200,
                        "data": {"app": "Code", "title": "discarded"},
                    }
                ]
            return [
                {
                    "id": 2,
                    "timestamp": start.isoformat(),
                    "duration": (end - start).total_seconds(),
                    "data": {"status": "not-afk"},
                }
            ]

    client = RepairClient()
    import_activitywatch(
        session,
        ActivityWatchImportRequest(
            device_id="mac-explicit-repair",
            platform=ActivityPlatform.MACOS,
            timezone="UTC",
            start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
            end_at=datetime(2026, 8, 1, 12, tzinfo=UTC),
        ),
        client=client,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )
    client.include_window = False
    repaired = import_activitywatch(
        session,
        ActivityWatchImportRequest(
            device_id="mac-explicit-repair",
            platform=ActivityPlatform.MACOS,
            timezone="UTC",
            start_at=datetime(2026, 8, 1, 10, 30, tzinfo=UTC),
            end_at=datetime(2026, 8, 1, 11, 30, tzinfo=UTC),
        ),
        client=client,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )
    rows = sorted(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_INTERVAL_EVENT
            )
        ),
        key=lambda row: row.observed_at,
    )
    daily = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == DAY_SUMMARY_EVENT
        )
    )

    assert repaired.response.accepted == 0
    assert [
        (row.payload["start_at"], row.payload["end_at"])
        for row in rows
    ] == [
        (
            "2026-08-01T10:00:00+00:00",
            "2026-08-01T10:30:00+00:00",
        ),
        (
            "2026-08-01T11:30:00+00:00",
            "2026-08-01T12:00:00+00:00",
        ),
    ]
    assert [row.payload["launches"] for row in rows] == [1, 0]
    assert daily is not None
    assert daily.payload["total_active_minutes"] == 60.0


def test_activitywatch_empty_repair_fails_closed_after_raw_provenance_expires(
    session,
) -> None:
    class EmptyRepairClient:
        include_window = True

        def list_buckets(self):
            return {
                "window": {"type": "currentwindow"},
                "afk": {"type": "afkstatus"},
            }

        def get_events(self, bucket_id, *, start, end):
            if bucket_id == "window":
                if not self.include_window:
                    return []
                return [
                    {
                        "id": 1,
                        "timestamp": "2026-08-01T10:00:00Z",
                        "duration": 3600,
                        "data": {"app": "Code", "title": "discarded"},
                    }
                ]
            return [
                {
                    "id": 2,
                    "timestamp": start.isoformat(),
                    "duration": (end - start).total_seconds(),
                    "data": {"status": "not-afk"},
                }
            ]

    update_retention_policy(session, "activity_raw", "1d")
    client = EmptyRepairClient()
    request = ActivityWatchImportRequest(
        device_id="mac-expired-empty-repair",
        platform=ActivityPlatform.MACOS,
        timezone="UTC",
        start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
        end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )
    import_activitywatch(
        session,
        request,
        client=client,
        now=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )
    client.include_window = False

    with pytest.raises(ActivitySummaryProvenanceError):
        import_activitywatch(
            session,
            request,
            client=client,
            now=datetime(2026, 8, 3, 11, tzinfo=UTC),
        )

    daily = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == DAY_SUMMARY_EVENT
        )
    )
    assert daily is not None
    assert daily.payload["total_active_minutes"] == 60.0


def test_activitywatch_empty_repair_rebuilds_fixed_offset_alias_summary(
    session,
) -> None:
    class EmptyRepairClient:
        include_window = True

        def list_buckets(self):
            return {
                "window": {"type": "currentwindow"},
                "afk": {"type": "afkstatus"},
            }

        def get_events(self, bucket_id, *, start, end):
            if bucket_id == "window":
                if not self.include_window:
                    return []
                return [
                    {
                        "id": 1,
                        "timestamp": "2026-08-01T01:00:00Z",
                        "duration": 3600,
                        "data": {"app": "Code", "title": "discarded"},
                    }
                ]
            return [
                {
                    "id": 2,
                    "timestamp": start.isoformat(),
                    "duration": (end - start).total_seconds(),
                    "data": {"status": "not-afk"},
                }
            ]

    current = datetime(2026, 8, 1, 2, tzinfo=UTC)
    request = ActivityWatchImportRequest(
        device_id="mac-fixed-offset-empty-repair",
        platform=ActivityPlatform.MACOS,
        timezone="Asia/Seoul",
        start_at=datetime(2026, 8, 1, 1, tzinfo=UTC),
        end_at=current,
    )
    client = EmptyRepairClient()
    import_activitywatch(
        session,
        request,
        client=client,
        now=current,
    )
    rebuild_day_summaries(
        session,
        day=date(2026, 8, 1),
        timezone="UTC+09:00",
        force_rebuild=True,
        now=current,
    )
    before_event, before_summary = get_daily_summary(
        session,
        day=date(2026, 8, 1),
        timezone="UTC+09:00",
        now=current,
    )
    assert before_event is not None
    assert before_summary["total_active_minutes"] == 60.0

    client.include_window = False
    repaired = import_activitywatch(
        session,
        request,
        client=client,
        now=current,
    )
    after_event, after_summary = get_daily_summary(
        session,
        day=date(2026, 8, 1),
        timezone="UTC+09:00",
        now=current,
    )

    assert repaired.response.accepted == 0
    assert after_event is None
    assert after_summary["reason"] == "no_activity_summary"


def test_activitywatch_malformed_repair_preserves_existing_raw_and_summary(
    session,
) -> None:
    class MalformedRepairClient:
        malformed = False

        def list_buckets(self):
            return {
                "window": {"type": "currentwindow"},
                "afk": {"type": "afkstatus"},
            }

        def get_events(self, bucket_id, *, start, end):
            if bucket_id == "window":
                return [
                    {
                        "id": 1,
                        "timestamp": "2026-08-01T10:00:00Z",
                        "duration": "bad" if self.malformed else 3600,
                        "data": {"app": "Code", "title": "discarded"},
                    }
                ]
            return [
                {
                    "id": 2,
                    "timestamp": start.isoformat(),
                    "duration": (end - start).total_seconds(),
                    "data": {"status": "not-afk"},
                }
            ]

    client = MalformedRepairClient()
    request = ActivityWatchImportRequest(
        device_id="mac-malformed-repair",
        platform=ActivityPlatform.MACOS,
        timezone="UTC",
        start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
        end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )
    import_activitywatch(
        session,
        request,
        client=client,
        now=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )
    client.malformed = True

    with pytest.raises(ActivityWatchError, match="window event is malformed"):
        import_activitywatch(
            session,
            request,
            client=client,
            now=datetime(2026, 8, 1, 12, tzinfo=UTC),
        )

    raw = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_INTERVAL_EVENT
            )
        )
    )
    daily = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == DAY_SUMMARY_EVENT
        )
    )
    assert len(raw) == 1
    assert raw[0].payload["end_at"] == "2026-08-01T11:00:00+00:00"
    assert daily is not None
    assert daily.payload["total_active_minutes"] == 60.0


def test_activitywatch_explicit_past_repair_does_not_rewind_auto_cursor(
    session,
) -> None:
    class EmptyClient:
        requests: list[tuple[str, datetime, datetime]] = []

        def list_buckets(self):
            return {
                "window": {"type": "currentwindow"},
                "afk": {"type": "afkstatus"},
            }

        def get_events(self, bucket_id, *, start, end):
            self.requests.append((bucket_id, start, end))
            if bucket_id == "window":
                return []
            return [
                {
                    "id": f"afk-{start.isoformat()}",
                    "timestamp": start.isoformat(),
                    "duration": (end - start).total_seconds(),
                    "data": {"status": "not-afk"},
                }
            ]

    client = EmptyClient()
    import_activitywatch(
        session,
        ActivityWatchImportRequest(
            device_id="mac-monotonic-cursor",
            platform=ActivityPlatform.MACOS,
            timezone="UTC",
            start_at=datetime(2026, 8, 8, 10, tzinfo=UTC),
            end_at=datetime(2026, 8, 8, 11, tzinfo=UTC),
        ),
        client=client,
        now=datetime(2026, 8, 8, 11, tzinfo=UTC),
    )
    import_activitywatch(
        session,
        ActivityWatchImportRequest(
            device_id="mac-monotonic-cursor",
            platform=ActivityPlatform.MACOS,
            timezone="UTC",
            start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
            end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
        ),
        client=client,
        now=datetime(2026, 8, 8, 11, tzinfo=UTC),
    )
    after_repair = get_control_payload(session, "mac-monotonic-cursor")

    assert after_repair["cursors"]["activitywatch:window"] == (
        "2026-08-08T11:00:00+00:00"
    )
    assert after_repair["cursors"]["activitywatch:afk"] == (
        "2026-08-08T11:00:00+00:00"
    )

    import_activitywatch(
        session,
        ActivityWatchImportRequest(
            device_id="mac-monotonic-cursor",
            platform=ActivityPlatform.MACOS,
            timezone="UTC",
        ),
        client=client,
        now=datetime(2026, 8, 8, 12, tzinfo=UTC),
    )
    final = get_control_payload(session, "mac-monotonic-cursor")

    assert final["cursors"]["activitywatch:window"] == (
        "2026-08-08T12:00:00+00:00"
    )
    assert final["cursors"]["activitywatch:afk"] == (
        "2026-08-08T12:00:00+00:00"
    )


def test_activitywatch_rebuilds_preserved_fragment_scopes_in_request_timezone(
    session,
) -> None:
    class TimezoneRepairClient:
        include_window = True

        def list_buckets(self):
            return {
                "window": {"type": "currentwindow"},
                "afk": {"type": "afkstatus"},
            }

        def get_events(self, bucket_id, *, start, end):
            if bucket_id == "window":
                if not self.include_window:
                    return []
                return [
                    {
                        "id": 1,
                        "timestamp": "2026-08-01T23:30:00Z",
                        "duration": 3600,
                        "data": {"app": "Code", "title": "discarded"},
                    }
                ]
            return [
                {
                    "id": 2,
                    "timestamp": start.isoformat(),
                    "duration": (end - start).total_seconds(),
                    "data": {"status": "not-afk"},
                }
            ]

    client = TimezoneRepairClient()
    import_activitywatch(
        session,
        ActivityWatchImportRequest(
            device_id="mac-timezone-fragments",
            platform=ActivityPlatform.MACOS,
            timezone="UTC",
            start_at=datetime(2026, 8, 1, 23, 30, tzinfo=UTC),
            end_at=datetime(2026, 8, 2, 0, 30, tzinfo=UTC),
        ),
        client=client,
        now=datetime(2026, 8, 2, 0, 30, tzinfo=UTC),
    )
    client.include_window = False
    import_activitywatch(
        session,
        ActivityWatchImportRequest(
            device_id="mac-timezone-fragments",
            platform=ActivityPlatform.MACOS,
            timezone="America/Los_Angeles",
            start_at=datetime(2026, 8, 1, 23, 45, tzinfo=UTC),
            end_at=datetime(2026, 8, 2, 0, 15, tzinfo=UTC),
        ),
        client=client,
        now=datetime(2026, 8, 2, 0, 30, tzinfo=UTC),
    )
    pacific = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == DAY_SUMMARY_EVENT,
            WellnessEvent.timezone == "America/Los_Angeles",
        )
    )

    assert pacific is not None
    assert pacific.payload["date"] == "2026-08-01"
    assert pacific.payload["total_active_minutes"] == 30.0
    assert pacific.derived_from["raw_event_count"] == 2


def test_activitywatch_disjoint_repair_keeps_one_source_launch(session) -> None:
    class DisjointRepairClient:
        corrected = False

        def list_buckets(self):
            return {
                "window": {"type": "currentwindow"},
                "afk": {"type": "afkstatus"},
            }

        def get_events(self, bucket_id, *, start, end):
            if bucket_id == "window":
                return [
                    {
                        "id": 1,
                        "timestamp": (
                            "2026-08-01T10:20:00Z"
                            if self.corrected
                            else "2026-08-01T10:00:00Z"
                        ),
                        "duration": 600 if self.corrected else 3600,
                        "data": {"app": "Code", "title": "discarded"},
                    }
                ]
            return [
                {
                    "id": 2,
                    "timestamp": start.isoformat(),
                    "duration": (end - start).total_seconds(),
                    "data": {"status": "not-afk"},
                }
            ]

    client = DisjointRepairClient()
    import_activitywatch(
        session,
        ActivityWatchImportRequest(
            device_id="mac-disjoint-repair",
            platform=ActivityPlatform.MACOS,
            timezone="UTC",
            start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
            end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
        ),
        client=client,
        now=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )
    client.corrected = True
    import_activitywatch(
        session,
        ActivityWatchImportRequest(
            device_id="mac-disjoint-repair",
            platform=ActivityPlatform.MACOS,
            timezone="UTC",
            start_at=datetime(2026, 8, 1, 10, 15, tzinfo=UTC),
            end_at=datetime(2026, 8, 1, 10, 45, tzinfo=UTC),
        ),
        client=client,
        now=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )
    import_activitywatch(
        session,
        ActivityWatchImportRequest(
            device_id="mac-disjoint-repair",
            platform=ActivityPlatform.MACOS,
            timezone="UTC",
            start_at=datetime(2026, 8, 1, 10, 15, tzinfo=UTC),
            end_at=datetime(2026, 8, 1, 10, 45, tzinfo=UTC),
        ),
        client=client,
        now=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )
    rows = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_INTERVAL_EVENT
            )
        )
    )
    daily = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == DAY_SUMMARY_EVENT
        )
    )

    assert sorted(row.payload["launches"] for row in rows) == [0, 0, 1]
    assert daily is not None
    assert daily.payload["app_launches_or_switches"] == 1
    assert daily.payload["total_active_minutes"] == 40.0


def test_activitywatch_rebuilds_summary_between_ingest_chunks(
    session,
    monkeypatch,
) -> None:
    import healthmes.activity.activitywatch as activitywatch_module

    monkeypatch.setattr(activitywatch_module, "IMPORT_BATCH_SIZE", 1)

    class ChunkedClient:
        def list_buckets(self):
            return {
                "window": {"type": "currentwindow"},
                "afk": {"type": "afkstatus"},
            }

        def get_events(self, bucket_id, *, start, end):
            if bucket_id == "window":
                return [
                    {
                        "id": 1,
                        "timestamp": "2026-08-01T10:00:00Z",
                        "duration": 1800,
                        "data": {"app": "Code", "title": "discarded"},
                    },
                    {
                        "id": 2,
                        "timestamp": "2026-08-01T10:30:00Z",
                        "duration": 1800,
                        "data": {"app": "Browser", "title": "discarded"},
                    },
                ]
            return [
                {
                    "id": 3,
                    "timestamp": "2026-08-01T10:00:00Z",
                    "duration": 3600,
                    "data": {"status": "not-afk"},
                }
            ]

    result = import_activitywatch(
        session,
        ActivityWatchImportRequest(
            device_id="mac-chunked",
            platform=ActivityPlatform.MACOS,
            timezone="UTC",
            start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
            end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
        ),
        client=ChunkedClient(),
        now=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )
    daily = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == DAY_SUMMARY_EVENT
        )
    )

    assert result.response.created == 2
    assert daily is not None
    assert daily.payload["total_active_minutes"] == 60.0
    assert daily.derived_from["raw_event_count"] == 2


def test_activitywatch_rebuilds_old_and_new_days_when_identity_moves_beyond_lookback(
    session,
) -> None:
    class MovedIdentityClient:
        event_day = 1

        def list_buckets(self):
            return {
                "window": {"type": "currentwindow"},
                "afk": {"type": "afkstatus"},
            }

        def get_events(self, bucket_id, *, start, end):
            timestamp = f"2026-08-{self.event_day:02d}T10:00:00Z"
            if bucket_id == "window":
                return [
                    {
                        "id": 1,
                        "timestamp": timestamp,
                        "duration": 3600,
                        "data": {"app": "Code", "title": "discarded"},
                    }
                ]
            return [
                {
                    "id": 2,
                    "timestamp": timestamp,
                    "duration": 3600,
                    "data": {"status": "not-afk"},
                }
            ]

    client = MovedIdentityClient()
    import_activitywatch(
        session,
        ActivityWatchImportRequest(
            device_id="mac-moved-identity",
            platform=ActivityPlatform.MACOS,
            timezone="UTC",
            start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
            end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
        ),
        client=client,
        now=datetime(2026, 8, 1, 11, tzinfo=UTC),
    )
    client.event_day = 9
    moved = import_activitywatch(
        session,
        ActivityWatchImportRequest(
            device_id="mac-moved-identity",
            platform=ActivityPlatform.MACOS,
            timezone="UTC",
            start_at=datetime(2026, 8, 9, 10, tzinfo=UTC),
            end_at=datetime(2026, 8, 9, 11, tzinfo=UTC),
        ),
        client=client,
        now=datetime(2026, 8, 9, 11, tzinfo=UTC),
    )
    summaries = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == DAY_SUMMARY_EVENT
            )
        )
    )
    raw = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_INTERVAL_EVENT
            )
        )
    )

    assert moved.response.affected_dates == ["2026-08-01", "2026-08-09"]
    assert len(raw) == 1
    assert raw[0].payload["start_at"] == "2026-08-09T10:00:00+00:00"
    assert [row.payload["date"] for row in summaries] == ["2026-08-09"]
    assert summaries[0].payload["total_active_minutes"] == 60.0


def test_android_backfill_pages_all_rows_and_accepts_sqlite_naive_times(
    session,
    monkeypatch,
) -> None:
    import healthmes.activity.android as android_module

    monkeypatch.setattr(android_module, "BACKFILL_PAGE_SIZE", 2)
    session.add_all(
        [
            AppUsageSample(
                device_id="pixel-backfill",
                bucket_start=datetime(2026, 8, 1, 8 + index),
                app_package=f"com.example.app{index}",
                foreground_seconds=300,
                launches=index,
                category="productivity",
            )
            for index in range(5)
        ]
    )
    session.flush()

    result = backfill_android_canonical_events(
        session,
        timezone="UTC",
        now=datetime(2026, 8, 2, tzinfo=UTC),
    )
    rows = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT
            )
        )
    )

    assert result is not None
    assert result.response.created == 5
    assert result.response.accepted == 5
    assert len(rows) == 5
    assert all(row.observed_at.tzinfo is None for row in rows)
    daily = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == DAY_SUMMARY_EVENT
        )
    )
    assert daily is not None
    assert daily.payload["total_active_minutes"] == 25.0


def test_android_generation_zero_backfill_reuses_legacy_canonical_identity(
    session,
) -> None:
    sample = AppUsageSample(
        device_id="pixel-generation-zero",
        collection_generation=0,
        bucket_start=datetime(2026, 8, 1, 10),
        app_package="com.example.editor",
        foreground_seconds=600,
        launches=1,
        category="productivity",
    )
    initial = ingest_android_samples(
        session,
        device_id=sample.device_id,
        samples=[sample],
        timezone="UTC",
        collected_at=datetime(2026, 8, 1, 12, tzinfo=UTC),
        collection_generation=0,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )
    session.add(sample)
    session.flush()

    backfill = backfill_android_canonical_events(
        session,
        timezone="UTC",
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )
    raw = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT
            )
        )
    )

    assert initial.response.created == 1
    assert backfill is not None
    assert backfill.response.created == 0
    assert len(raw) == 1


def test_android_backfill_skips_incomplete_summary_provenance(session) -> None:
    update_retention_policy(session, "activity_raw", "1d")
    session.add_all(
        [
            AppUsageSample(
                device_id="pixel-provenance",
                bucket_start=datetime(2026, 8, 1, 9),
                app_package="com.example.morning",
                foreground_seconds=300,
                launches=1,
                category="productivity",
            ),
            AppUsageSample(
                device_id="pixel-provenance",
                bucket_start=datetime(2026, 8, 1, 18),
                app_package="com.example.evening",
                foreground_seconds=600,
                launches=1,
                category="productivity",
            ),
        ]
    )
    session.flush()
    first = backfill_android_canonical_events(
        session,
        timezone="UTC",
        now=datetime(2026, 8, 1, 20, tzinfo=UTC),
    )
    summary = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == DAY_SUMMARY_EVENT
        )
    )
    assert first is not None
    assert first.response.created == 2
    assert summary is not None
    summary_id = summary.id

    session.add(
        AppUsageSample(
            device_id="pixel-provenance",
            bucket_start=datetime(2026, 8, 1, 19),
            app_package="com.example.late",
            foreground_seconds=900,
            launches=1,
            category="productivity",
        )
    )
    session.flush()

    # At noon the morning raw row is outside the 1-day policy while the
    # long-lived summary still contains it. Startup migration must preserve
    # that summary and skip the new legacy row instead of failing the app.
    second = backfill_android_canonical_events(
        session,
        timezone="UTC",
        now=datetime(2026, 8, 2, 12, tzinfo=UTC),
    )
    raw_rows = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT
            )
        )
    )
    preserved_summary = session.get(WellnessEvent, summary_id)

    assert second is not None
    assert second.response.accepted == 0
    assert len(raw_rows) == 2
    assert all(
        row.source_record_id
        != android_source_record_id(
            "pixel-provenance",
            datetime(2026, 8, 1, 19),
            "com.example.late",
        )
        for row in raw_rows
    )
    assert preserved_summary is not None
    assert preserved_summary.payload["total_active_minutes"] == 15.0
    assert preserved_summary.derived_from["raw_event_count"] == 2


def test_android_backfill_skips_future_legacy_rows_without_blocking_startup(
    session,
) -> None:
    session.add_all(
        [
            AppUsageSample(
                device_id="pixel-future-backfill",
                bucket_start=datetime(2026, 8, 1, 10),
                app_package="com.example.valid",
                foreground_seconds=300,
                launches=1,
                category="productivity",
            ),
            AppUsageSample(
                device_id="pixel-future-backfill",
                bucket_start=datetime(2026, 8, 2, 10),
                app_package="com.example.future",
                foreground_seconds=300,
                launches=1,
                category="productivity",
            ),
        ]
    )
    session.flush()

    result = backfill_android_canonical_events(
        session,
        timezone="UTC",
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )
    rows = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_HOUR_EVENT
            )
        )
    )

    assert result is not None
    assert result.response.created == 1
    assert [row.payload["app_id"] for row in rows] == [
        "com.example.valid"
    ]
    assert (
        session.scalar(
            select(AppUsageSample).where(
                AppUsageSample.app_package == "com.example.future"
            )
        )
        is not None
    )


def test_activitywatch_contract_rejects_ranges_longer_than_seven_days() -> None:
    with pytest.raises(ValidationError, match="cannot exceed 7 days"):
        ActivityWatchImportRequest(
            device_id="mac-too-wide",
            platform=ActivityPlatform.MACOS,
            timezone="UTC",
            start_at=datetime(2026, 8, 1, tzinfo=UTC),
            end_at=datetime(2026, 8, 8, tzinfo=UTC) + timedelta(seconds=1),
        )
