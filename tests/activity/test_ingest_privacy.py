from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from healthmes.activity.aggregation import get_daily_summary
from healthmes.activity.contracts import (
    ActivityBatchIn,
    ActivityCapability,
    ActivityCollectionStatusUpdate,
    ActivityCollectionUpdate,
    ActivityPermissionStatus,
    ActivityPlatform,
    AppHourRecord,
    AppIntervalRecord,
)
from healthmes.activity.maintenance import delete_activity_data
from healthmes.activity.repository import (
    ACTIVITY_RAW_CLASS,
    APP_HOUR_EVENT,
    APP_INTERVAL_EVENT,
    COLLECTION_CONFIG_EVENT,
    COLLECTION_CURSOR_EVENT,
    COLLECTION_STATUS_EVENT,
    DELETION_TOMBSTONE_EVENT,
    ActivityConflictError,
    get_control_payload,
    parse_optional_datetime,
    serialize_collection_state,
    update_collection_config,
    update_collection_status,
    update_cursor,
)
from healthmes.activity.service import (
    ActivityCollectionBlockedError,
    ActivityFutureDataError,
    ActivityLateDataError,
    ActivitySourceModeConflictError,
    StaleCollectionRevisionError,
    ingest_activity_batch,
)
from healthmes.store import RetentionPolicy, WellnessEvent


def _hour_batch(
    *,
    source_record_id: str = "hour-1",
    foreground_seconds: int = 600,
    collection_revision: int | None = None,
) -> ActivityBatchIn:
    return ActivityBatchIn(
        source_provider="test-collector",
        source_device="test-device",
        platform=ActivityPlatform.ANDROID,
        capability=ActivityCapability.AGGREGATE,
        timezone="UTC",
        collected_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
        collection_revision=collection_revision,
        records=[
            AppHourRecord(
                source_record_id=source_record_id,
                bucket_start=datetime(2026, 8, 1, 10, tzinfo=UTC),
                app_id="com.example.editor",
                foreground_seconds=foreground_seconds,
                launches=3,
                category="productivity",
                coverage_seconds=3600,
            )
        ],
    )


def test_ingest_is_idempotent_and_rejects_source_identity_reuse(session) -> None:
    batch = _hour_batch()

    first = ingest_activity_batch(session, batch, rebuild_summaries=False)
    duplicate = ingest_activity_batch(session, batch, rebuild_summaries=False)

    assert first.response.created == 1
    assert duplicate.response.duplicates == 1
    assert (
        len(
            list(
                session.scalars(
                    select(WellnessEvent).where(WellnessEvent.event_type == APP_HOUR_EVENT)
                )
            )
        )
        == 1
    )

    changed = _hour_batch(foreground_seconds=900)
    with pytest.raises(ActivityConflictError):
        ingest_activity_batch(session, changed, rebuild_summaries=False)


def test_raw_activity_uses_the_activity_retention_policy(session) -> None:
    observed = datetime(2026, 8, 1, 10, tzinfo=UTC)
    ingest_activity_batch(session, _hour_batch(), rebuild_summaries=False)

    event = session.scalar(select(WellnessEvent).where(WellnessEvent.event_type == APP_HOUR_EVENT))
    policy = session.scalar(
        select(RetentionPolicy).where(RetentionPolicy.data_class == ACTIVITY_RAW_CLASS)
    )

    assert event is not None
    assert policy is not None
    assert policy.retention_days == 14
    assert event.retention_policy_id == policy.id
    assert event.expires_at.replace(tzinfo=UTC) == observed + timedelta(days=14)


def test_excluded_app_is_filtered_before_any_raw_event_is_persisted(session) -> None:
    state = update_collection_config(
        session,
        "test-device",
        ActivityCollectionUpdate(
            excluded_apps=["com.example.private"],
        ),
    )
    batch = ActivityBatchIn(
        source_provider="test-collector",
        source_device="test-device",
        platform=ActivityPlatform.ANDROID,
        capability=ActivityCapability.AGGREGATE,
        timezone="UTC",
        collection_revision=int(state["config_revision"]),
        records=[
            AppHourRecord(
                source_record_id="private",
                bucket_start=datetime(2026, 8, 1, 10, tzinfo=UTC),
                app_id="COM.EXAMPLE.PRIVATE",
                foreground_seconds=500,
            ),
            AppHourRecord(
                source_record_id="allowed",
                bucket_start=datetime(2026, 8, 1, 10, tzinfo=UTC),
                app_id="com.example.editor",
                foreground_seconds=400,
            ),
        ],
    )

    result = ingest_activity_batch(session, batch, rebuild_summaries=False)
    rows = list(
        session.scalars(select(WellnessEvent).where(WellnessEvent.event_type == APP_HOUR_EVENT))
    )

    assert result.response.accepted == 1
    assert result.response.excluded == 1
    assert [row.payload["app_id"] for row in rows] == ["com.example.editor"]
    serialized = str([row.payload for row in rows])
    assert "private" not in serialized.casefold()


@pytest.mark.parametrize(
    ("setup", "reason"),
    (
        (
            lambda session: update_collection_config(
                session,
                "test-device",
                ActivityCollectionUpdate(enabled=False),
            ),
            "collection_disabled",
        ),
        (
            lambda session: update_collection_config(
                session,
                "test-device",
                ActivityCollectionUpdate(paused_until=datetime(2026, 8, 2, tzinfo=UTC)),
            ),
            "collection_paused",
        ),
        (
            lambda session: update_collection_status(
                session,
                "test-device",
                ActivityCollectionStatusUpdate(permission_status=ActivityPermissionStatus.REVOKED),
            ),
            "permission_revoked",
        ),
    ),
)
def test_collection_gate_blocks_disabled_paused_and_revoked_devices(
    session,
    setup,
    reason: str,
) -> None:
    setup(session)

    with pytest.raises(ActivityCollectionBlockedError) as raised:
        ingest_activity_batch(
            session,
            _hour_batch(),
            now=datetime(2026, 8, 1, 12, tzinfo=UTC),
            rebuild_summaries=False,
        )

    assert raised.value.reason == reason
    assert (
        session.scalar(select(WellnessEvent).where(WellnessEvent.event_type == APP_HOUR_EVENT))
        is None
    )


def test_stale_collection_revision_is_rejected(session) -> None:
    state = update_collection_config(
        session,
        "test-device",
        ActivityCollectionUpdate(excluded_apps=["com.example.private"]),
    )
    assert state["config_revision"] == 1

    with pytest.raises(StaleCollectionRevisionError):
        ingest_activity_batch(
            session,
            _hour_batch(collection_revision=0),
            rebuild_summaries=False,
        )


def test_collection_status_exposes_queue_age_coverage_and_effective_state(session) -> None:
    now = datetime(2026, 8, 1, 12, tzinfo=UTC)
    payload = update_collection_status(
        session,
        "test-device",
        ActivityCollectionStatusUpdate(
            platform=ActivityPlatform.ANDROID,
            capability=ActivityCapability.AGGREGATE,
            permission_status=ActivityPermissionStatus.GRANTED,
            queue_oldest_at=now - timedelta(minutes=20),
            queue_depth=4,
            coverage=0.75,
        ),
        now=now,
    )

    output = serialize_collection_state(payload, now=now)

    assert output["effective_collecting"] is True
    assert output["queue_age_seconds"] == 1200
    assert output["queue_depth"] == 4
    assert output["coverage"] == 0.75


def test_ingest_updates_telemetry_without_rewriting_permission_boundary(
    session,
) -> None:
    observed_at = datetime(2026, 8, 1, 9, tzinfo=UTC)
    update_collection_status(
        session,
        "test-device",
        ActivityCollectionStatusUpdate(
            platform=ActivityPlatform.ANDROID,
            capability=ActivityCapability.AGGREGATE,
            permission_status=ActivityPermissionStatus.GRANTED,
            status_observed_at=observed_at,
            collection_generation=4,
        ),
        now=observed_at,
    )

    ingest_activity_batch(
        session,
        _hour_batch(),
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
        rebuild_summaries=False,
    )
    payload = get_control_payload(session, "test-device")

    assert payload["permission_status"] == "granted"
    assert payload["collection_generation"] == 4
    assert parse_optional_datetime(payload["status_observed_at"]) == observed_at
    assert parse_optional_datetime(payload["last_collected_at"]) == datetime(
        2026,
        8,
        1,
        11,
        tzinfo=UTC,
    )
    assert parse_optional_datetime(payload["last_uploaded_at"]) == datetime(
        2026,
        8,
        1,
        12,
        tzinfo=UTC,
    )


def test_cross_midnight_interval_marks_both_local_dates(session) -> None:
    batch = ActivityBatchIn(
        source_provider="test-desktop",
        source_device="desktop-1",
        platform=ActivityPlatform.MACOS,
        capability=ActivityCapability.DETAILED,
        timezone="Asia/Seoul",
        records=[
            AppIntervalRecord(
                source_record_id="cross-midnight",
                start_at=datetime(2026, 8, 1, 14, 50, tzinfo=UTC),
                end_at=datetime(2026, 8, 1, 15, 10, tzinfo=UTC),
                state="active",
                app_id="editor",
            )
        ],
    )

    result = ingest_activity_batch(session, batch, rebuild_summaries=False)

    assert result.response.affected_dates == ["2026-08-01", "2026-08-02"]
    event = session.scalar(
        select(WellnessEvent).where(WellnessEvent.event_type == APP_INTERVAL_EVENT)
    )
    assert event is not None


def test_control_payload_round_trips_without_creating_raw_activity(session) -> None:
    update_collection_config(
        session,
        "test-device",
        ActivityCollectionUpdate(enabled=False, excluded_apps=["private"]),
    )

    payload = get_control_payload(session, "test-device")

    assert payload["enabled"] is False
    assert payload["excluded_apps"] == ["private"]
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type.in_((APP_HOUR_EVENT, APP_INTERVAL_EVENT))
            )
        )
        is None
    )


def test_capability_contract_rejects_mismatched_record_shapes() -> None:
    interval = AppIntervalRecord(
        source_record_id="interval",
        start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
        end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
        state="active",
        app_id="editor",
    )
    hour = _hour_batch().records[0]

    with pytest.raises(ValidationError, match="aggregate collectors"):
        ActivityBatchIn(
            source_provider="test",
            source_device="device",
            platform=ActivityPlatform.ANDROID,
            capability=ActivityCapability.AGGREGATE,
            timezone="UTC",
            records=[interval],
        )
    with pytest.raises(ValidationError, match="detailed collectors"):
        ActivityBatchIn(
            source_provider="test",
            source_device="device",
            platform=ActivityPlatform.MACOS,
            capability=ActivityCapability.DETAILED,
            timezone="UTC",
            records=[hour],
        )
    with pytest.raises(ValidationError, match="iOS detailed"):
        ActivityBatchIn(
            source_provider="test",
            source_device="iphone",
            platform=ActivityPlatform.IOS,
            capability=ActivityCapability.DETAILED,
            timezone="UTC",
            records=[interval],
        )


def test_free_form_category_is_reduced_to_the_public_taxonomy(session) -> None:
    batch = _hour_batch().model_copy(
        update={
            "records": [
                _hour_batch().records[0].model_copy(
                    update={"category": "Secret Customer Project"}
                )
            ]
        }
    )

    ingest_activity_batch(session, batch, rebuild_summaries=False)

    event = session.scalar(
        select(WellnessEvent).where(WellnessEvent.event_type == APP_HOUR_EVENT)
    )
    assert event is not None
    assert event.payload["category"] == "other"
    assert "secret" not in str(event.payload).casefold()


def test_config_status_and_cursor_are_separate_events_but_one_read_contract(
    session,
) -> None:
    update_collection_config(
        session,
        "test-device",
        ActivityCollectionUpdate(
            platform=ActivityPlatform.ANDROID,
            excluded_apps=["com.example.private"],
        ),
    )
    update_collection_status(
        session,
        "test-device",
        ActivityCollectionStatusUpdate(
            platform=ActivityPlatform.ANDROID,
            capability=ActivityCapability.AGGREGATE,
            permission_status=ActivityPermissionStatus.GRANTED,
            queue_depth=3,
        ),
    )
    update_cursor(
        session,
        "test-device",
        "android:hour",
        "2026-08-01T11:00:00+00:00",
        platform=ActivityPlatform.ANDROID,
    )

    rows = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type.in_(
                    (
                        COLLECTION_CONFIG_EVENT,
                        COLLECTION_STATUS_EVENT,
                        COLLECTION_CURSOR_EVENT,
                    )
                )
            )
        )
    )
    payload = get_control_payload(session, "test-device")

    assert {row.event_type for row in rows} == {
        COLLECTION_CONFIG_EVENT,
        COLLECTION_STATUS_EVENT,
        COLLECTION_CURSOR_EVENT,
    }
    assert payload["excluded_apps"] == ["com.example.private"]
    assert payload["queue_depth"] == 3
    assert payload["permission_status"] == "granted"
    assert payload["cursors"]["android:hour"] == "2026-08-01T11:00:00+00:00"
    config = next(row for row in rows if row.event_type == COLLECTION_CONFIG_EVENT)
    status = next(row for row in rows if row.event_type == COLLECTION_STATUS_EVENT)
    assert "queue_depth" not in config.payload
    assert "excluded_apps" not in status.payload


def test_deleted_activity_cannot_be_restored_by_a_queued_retry(session) -> None:
    batch = _hour_batch()
    ingest_activity_batch(
        session,
        batch,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    report = delete_activity_data(
        session,
        device_id="test-device",
        start=datetime(2026, 8, 1, 10, 15, tzinfo=UTC),
        end=datetime(2026, 8, 1, 10, 30, tzinfo=UTC),
        include_summaries=True,
        include_control=False,
        now=datetime(2026, 8, 1, 13, tzinfo=UTC),
    )
    retried = ingest_activity_batch(
        session,
        batch,
        now=datetime(2026, 8, 1, 13, tzinfo=UTC),
        rebuild_summaries=False,
    )

    assert report.raw_events_deleted == 1
    assert retried.response.accepted == 0
    assert retried.response.tombstoned == 1
    assert (
        session.scalar(
            select(WellnessEvent).where(WellnessEvent.event_type == APP_HOUR_EVENT)
        )
        is None
    )
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == DELETION_TOMBSTONE_EVENT
            )
        )
        is not None
    )


def test_late_activity_outside_raw_retention_is_rejected(session) -> None:
    stale = _hour_batch().model_copy(
        update={
            "records": [
                _hour_batch().records[0].model_copy(
                    update={"bucket_start": datetime(2026, 7, 1, 10, tzinfo=UTC)}
                )
            ]
        }
    )

    with pytest.raises(ActivityLateDataError):
        ingest_activity_batch(
            session,
            stale,
            now=datetime(2026, 8, 1, 12, tzinfo=UTC),
            rebuild_summaries=False,
        )


@pytest.mark.parametrize(
    "batch",
    (
        _hour_batch().model_copy(
            update={
                "records": [
                    _hour_batch().records[0].model_copy(
                        update={
                            "bucket_start": datetime(
                                2026,
                                8,
                                1,
                                12,
                                2,
                                tzinfo=UTC,
                            )
                        }
                    )
                ]
            }
        ),
        ActivityBatchIn(
            source_provider="test-desktop",
            source_device="desktop-future",
            platform=ActivityPlatform.MACOS,
            capability=ActivityCapability.DETAILED,
            timezone="UTC",
            records=[
                AppIntervalRecord(
                    source_record_id="future-interval",
                    start_at=datetime(2026, 8, 1, 11, 30, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 12, 2, tzinfo=UTC),
                    state="active",
                    app_id="editor",
                )
            ],
        ),
        _hour_batch().model_copy(
            update={
                "collected_at": datetime(2026, 8, 1, 12, 2, tzinfo=UTC),
            }
        ),
    ),
    ids=("future-hour", "future-interval", "future-collected-at"),
)
def test_future_activity_is_rejected_before_persistence(session, batch) -> None:
    with pytest.raises(ActivityFutureDataError):
        ingest_activity_batch(
            session,
            batch,
            now=datetime(2026, 8, 1, 12, tzinfo=UTC),
            rebuild_summaries=False,
        )

    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type.in_(
                    (APP_HOUR_EVENT, APP_INTERVAL_EVENT)
                )
            )
        )
        is None
    )


def test_one_provider_device_cannot_mix_overlapping_source_modes(session) -> None:
    ingest_activity_batch(
        session,
        _hour_batch(),
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
        rebuild_summaries=False,
    )
    detailed = ActivityBatchIn(
        source_provider="test-collector",
        source_device="test-device",
        platform=ActivityPlatform.MACOS,
        capability=ActivityCapability.DETAILED,
        timezone="UTC",
        records=[
            AppIntervalRecord(
                source_record_id="interval-overlap",
                start_at=datetime(2026, 8, 1, 10, 30, tzinfo=UTC),
                end_at=datetime(2026, 8, 1, 11, 30, tzinfo=UTC),
                state="active",
                app_id="editor",
            )
        ],
    )

    with pytest.raises(ActivitySourceModeConflictError):
        ingest_activity_batch(
            session,
            detailed,
            now=datetime(2026, 8, 1, 12, tzinfo=UTC),
            rebuild_summaries=False,
        )


def test_replacing_a_moved_record_rebuilds_old_and_new_local_dates(session) -> None:
    original = ActivityBatchIn(
        source_provider="mutable-desktop",
        source_device="desktop",
        platform=ActivityPlatform.MACOS,
        capability=ActivityCapability.DETAILED,
        timezone="UTC",
        records=[
            AppIntervalRecord(
                source_record_id="mutable",
                start_at=datetime(2026, 8, 1, 9, tzinfo=UTC),
                end_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                state="active",
                app_id="editor",
            )
        ],
    )
    moved = original.model_copy(
        update={
            "records": [
                original.records[0].model_copy(
                    update={
                        "start_at": datetime(2026, 8, 2, 9, tzinfo=UTC),
                        "end_at": datetime(2026, 8, 2, 10, tzinfo=UTC),
                    }
                )
            ]
        }
    )

    ingest_activity_batch(
        session,
        original,
        allow_replace=True,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )
    ingest_activity_batch(
        session,
        moved,
        allow_replace=True,
        now=datetime(2026, 8, 2, 12, tzinfo=UTC),
    )

    old_event, old_summary = get_daily_summary(
        session,
        day=date(2026, 8, 1),
        timezone="UTC",
    )
    new_event, new_summary = get_daily_summary(
        session,
        day=date(2026, 8, 2),
        timezone="UTC",
    )
    assert old_event is None
    assert old_summary["status"] == "insufficient_data"
    assert new_event is not None
    assert new_summary["total_active_minutes"] == 60.0
