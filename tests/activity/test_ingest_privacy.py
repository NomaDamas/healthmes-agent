from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

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
from healthmes.activity.repository import (
    ACTIVITY_RAW_CLASS,
    APP_HOUR_EVENT,
    APP_INTERVAL_EVENT,
    ActivityConflictError,
    get_control_payload,
    serialize_collection_state,
    update_collection_config,
    update_collection_status,
)
from healthmes.activity.service import (
    ActivityCollectionBlockedError,
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
