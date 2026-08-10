from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from healthmes.activity.aggregation import (
    LEGACY_SUMMARY_REASON,
    SUMMARY_DERIVATION_VERSION,
    evidence_digest,
    get_daily_summary,
    local_day_bounds,
    migrate_activity_summary_derivations,
    personal_baseline_delta,
    rebuild_affected_days,
    rebuild_day_summaries,
    summarize_window,
)
from healthmes.activity.android import android_batch
from healthmes.activity.context import (
    activity_summary_context,
    focus_context,
    overwork_context,
)
from healthmes.activity.contracts import (
    ActivityBatchIn,
    ActivityCapability,
    ActivityPlatform,
    AppHourRecord,
    AppIntervalRecord,
)
from healthmes.activity.maintenance import (
    ActivityDeletionUnsafeError,
    delete_activity_data,
    run_activity_maintenance,
)
from healthmes.activity.repository import (
    APP_HOUR_EVENT,
    APP_INTERVAL_EVENT,
    DAY_SUMMARY_EVENT,
    DELETION_TOMBSTONE_EVENT,
    HOUR_SUMMARY_EVENT,
    ensure_activity_policies,
    persist_activity_record,
)
from healthmes.activity.service import (
    ActivitySummaryProvenanceError,
    ingest_activity_batch,
)
from healthmes.storage import (
    run_storage_maintenance,
    update_retention_policy,
)
from healthmes.store import AppUsageSample, WellnessEvent


def _interval_batch(
    records: list[AppIntervalRecord],
    *,
    device_id: str = "desktop-1",
    timezone_name: str = "UTC",
) -> ActivityBatchIn:
    return ActivityBatchIn(
        source_provider="test-desktop",
        source_device=device_id,
        platform=ActivityPlatform.MACOS,
        capability=ActivityCapability.DETAILED,
        timezone=timezone_name,
        records=records,
    )


def test_day_summary_preserves_a_continuous_block_across_hour_boundaries(session) -> None:
    ingest_activity_batch(
        session,
        _interval_batch(
            [
                AppIntervalRecord(
                    source_record_id="before-long-block",
                    start_at=datetime(2026, 8, 1, 0, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 20, 30, tzinfo=UTC),
                    state="idle",
                ),
                AppIntervalRecord(
                    source_record_id="long-block",
                    start_at=datetime(2026, 8, 1, 20, 30, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 22, 45, tzinfo=UTC),
                    state="active",
                    app_id="editor",
                    category="productivity",
                    launches=1,
                ),
                AppIntervalRecord(
                    source_record_id="after-long-block",
                    start_at=datetime(2026, 8, 1, 22, 45, tzinfo=UTC),
                    end_at=datetime(2026, 8, 2, 0, tzinfo=UTC),
                    state="idle",
                ),
            ]
        ),
    )

    _, summary = get_daily_summary(
        session,
        day=date(2026, 8, 1),
        timezone="UTC",
    )
    overwork = overwork_context(
        session,
        day=date(2026, 8, 1),
        timezone="UTC",
    )

    assert summary["total_active_minutes"] == 135.0
    assert summary["longest_active_block_minutes"] == 135.0
    assert summary["late_activity_minutes"] == 45.0
    assert summary["source_coverage"]["ratio"] == 1.0
    assert overwork["risk_level"] == "elevated"
    assert {signal["kind"] for signal in overwork["signals"]} == {"long_continuous_activity"}


def test_overlapping_intervals_on_one_device_are_not_double_counted(session) -> None:
    batch = _interval_batch(
        [
            AppIntervalRecord(
                source_record_id="overlap-a",
                start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
                state="active",
                app_id="editor",
                category="productivity",
            ),
            AppIntervalRecord(
                source_record_id="overlap-b",
                start_at=datetime(2026, 8, 1, 10, 30, tzinfo=UTC),
                end_at=datetime(2026, 8, 1, 11, 30, tzinfo=UTC),
                state="active",
                app_id="browser",
                category="research",
            ),
        ]
    )
    ingest_activity_batch(session, batch, rebuild_summaries=False)
    events = list(
        session.scalars(select(WellnessEvent).where(WellnessEvent.event_type == APP_INTERVAL_EVENT))
    )

    summary = summarize_window(
        events,
        start=datetime(2026, 8, 1, 10, tzinfo=UTC),
        end=datetime(2026, 8, 1, 12, tzinfo=UTC),
        timezone="UTC",
    )

    assert summary["total_active_minutes"] == 90.0
    assert summary["longest_active_block_minutes"] == 90.0
    assert sum(summary["category_minutes"].values()) == 90.0


def test_daily_device_count_unions_devices_across_separate_hours(session) -> None:
    ingest_activity_batch(
        session,
        _interval_batch(
            [
                AppIntervalRecord(
                    source_record_id="device-a-hour",
                    start_at=datetime(2026, 8, 1, 9, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 9, 30, tzinfo=UTC),
                    state="active",
                    app_id="editor",
                )
            ],
            device_id="desktop-a",
        ),
    )
    ingest_activity_batch(
        session,
        _interval_batch(
            [
                AppIntervalRecord(
                    source_record_id="device-b-hour",
                    start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 10, 30, tzinfo=UTC),
                    state="active",
                    app_id="browser",
                )
            ],
            device_id="desktop-b",
        ),
    )

    daily = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == DAY_SUMMARY_EVENT
        )
    )

    assert daily is not None
    assert daily.payload["device_count"] == 2


def test_same_device_id_from_different_providers_stays_separate(session) -> None:
    shared_device_id = "shared-provider-local-id"
    ingest_activity_batch(
        session,
        ActivityBatchIn(
            source_provider="provider-detailed",
            source_device=shared_device_id,
            platform=ActivityPlatform.MACOS,
            capability=ActivityCapability.DETAILED,
            timezone="UTC",
            records=[
                AppIntervalRecord(
                    source_record_id="provider-detailed-interval",
                    start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 10, 30, tzinfo=UTC),
                    state="active",
                    app_id="editor",
                )
            ],
        ),
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )
    ingest_activity_batch(
        session,
        ActivityBatchIn(
            source_provider="provider-hourly",
            source_device=shared_device_id,
            platform=ActivityPlatform.ANDROID,
            capability=ActivityCapability.AGGREGATE,
            timezone="UTC",
            records=[
                AppHourRecord(
                    source_record_id="provider-hourly-bucket",
                    bucket_start=datetime(2026, 8, 1, 10, tzinfo=UTC),
                    app_id="browser",
                    foreground_seconds=20 * 60,
                    coverage_seconds=3600,
                )
            ],
        ),
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )
    events = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type.in_(
                    (APP_INTERVAL_EVENT, APP_HOUR_EVENT)
                )
            )
        )
    )

    summary = summarize_window(
        events,
        start=datetime(2026, 8, 1, 10, tzinfo=UTC),
        end=datetime(2026, 8, 1, 11, tzinfo=UTC),
        timezone="UTC",
    )
    daily = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == DAY_SUMMARY_EVENT,
        )
    )

    assert summary["total_active_minutes"] == 50.0
    assert summary["device_count"] == 2
    assert len(summary["_evidence_event_ids"]) == 2
    assert "cross_device_overlap_not_deduplicated" in summary["limitations"]
    assert daily is not None
    assert daily.payload["device_count"] == 2
    assert daily.derived_from["raw_event_count"] == 2


def test_local_day_bounds_handle_dst_spring_and_fall_days() -> None:
    spring_start, spring_end = local_day_bounds(
        date(2026, 3, 8),
        "America/New_York",
    )
    fall_start, fall_end = local_day_bounds(
        date(2026, 11, 1),
        "America/New_York",
    )

    assert spring_end - spring_start == timedelta(hours=23)
    assert fall_end - fall_start == timedelta(hours=25)


def test_fixed_offset_timezone_is_supported_by_context_contract(session) -> None:
    fixed_kst = timezone(timedelta(hours=9))
    ingest_activity_batch(
        session,
        _interval_batch(
            [
                AppIntervalRecord(
                    source_record_id="fixed-offset",
                    start_at=datetime(2026, 8, 1, 1, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 2, tzinfo=UTC),
                    state="active",
                    app_id="editor",
                )
            ],
            timezone_name="Asia/Seoul",
        ),
        rebuild_summaries=False,
    )
    rebuild_day_summaries(
        session,
        day=date(2026, 8, 1),
        timezone=fixed_kst,
    )

    context = activity_summary_context(
        session,
        day=date(2026, 8, 1),
        timezone=fixed_kst,
    )

    assert context["status"] == "ok"
    assert context["timezone"] == "UTC+09:00"
    assert context["total_active_minutes"] == 60.0


def test_fixed_offset_timezone_name_can_be_reused_for_day_bounds() -> None:
    fixed_kst_name = str(timezone(timedelta(hours=9)))

    start, end = local_day_bounds(
        date(2026, 8, 1),
        fixed_kst_name,
    )

    assert fixed_kst_name == "UTC+09:00"
    assert start == datetime(2026, 7, 31, 15, tzinfo=UTC)
    assert end == datetime(2026, 8, 1, 15, tzinfo=UTC)


@pytest.mark.parametrize(
    "timezone_order",
    (
        ("UTC+09:00", "Asia/Seoul"),
        ("Asia/Seoul", "UTC+09:00"),
    ),
)
def test_fixed_offset_summary_refreshes_for_equivalent_iana_ingest(
    session,
    timezone_order,
) -> None:
    current = datetime(2026, 8, 1, 12, tzinfo=UTC)
    for index, timezone_name in enumerate(timezone_order):
        ingest_activity_batch(
            session,
            _interval_batch(
                [
                    AppIntervalRecord(
                        source_record_id=f"offset-alias-{index}",
                        start_at=datetime(
                            2026,
                            8,
                            1,
                            1 + index,
                            tzinfo=UTC,
                        ),
                        end_at=datetime(
                            2026,
                            8,
                            1,
                            2 + index,
                            tzinfo=UTC,
                        ),
                        state="active",
                        app_id=f"editor-{index}",
                    )
                ],
                device_id=f"offset-device-{index}",
                timezone_name=timezone_name,
            ),
            now=current,
        )

    fixed_event, fixed_summary = get_daily_summary(
        session,
        day=date(2026, 8, 1),
        timezone="UTC+09:00",
        now=current,
    )
    raw_events = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_INTERVAL_EVENT
            )
        )
    )

    assert fixed_event is not None
    assert fixed_summary["total_active_minutes"] == 120.0
    assert fixed_event.derived_from["raw_event_count"] == 2
    assert fixed_event.derived_from["raw_evidence_sha256"] == evidence_digest(
        str(event.id) for event in raw_events
    )


def test_deleting_iana_raw_refreshes_equivalent_fixed_offset_summary(
    session,
) -> None:
    current = datetime(2026, 8, 1, 12, tzinfo=UTC)
    ingest_activity_batch(
        session,
        _interval_batch(
            [
                AppIntervalRecord(
                    source_record_id="fixed-offset-keep",
                    start_at=datetime(2026, 8, 1, 1, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 2, tzinfo=UTC),
                    state="active",
                    app_id="fixed-editor",
                )
            ],
            device_id="fixed-offset-device",
            timezone_name="UTC+09:00",
        ),
        now=current,
    )
    ingest_activity_batch(
        session,
        _interval_batch(
            [
                AppIntervalRecord(
                    source_record_id="iana-delete",
                    start_at=datetime(2026, 8, 1, 2, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 3, tzinfo=UTC),
                    state="active",
                    app_id="iana-editor",
                )
            ],
            device_id="iana-delete-device",
            timezone_name="Asia/Seoul",
        ),
        now=current,
    )

    before_event, before_summary = get_daily_summary(
        session,
        day=date(2026, 8, 1),
        timezone="UTC+09:00",
        now=current,
    )
    assert before_event is not None
    assert before_summary["total_active_minutes"] == 120.0
    assert before_event.derived_from["raw_event_count"] == 2

    report = delete_activity_data(
        session,
        device_id="iana-delete-device",
        start=None,
        end=None,
        include_summaries=False,
        include_control=False,
        now=current,
    )
    by_timezone: dict[str, set[date]] = {}
    for scope in report.affected_scopes:
        by_timezone.setdefault(scope.timezone, set()).add(scope.day)
    for timezone_name, days in by_timezone.items():
        rebuild_affected_days(
            session,
            days=days,
            timezone=timezone_name,
            force_rebuild=True,
            now=current,
        )

    fixed_event, fixed_summary = get_daily_summary(
        session,
        day=date(2026, 8, 1),
        timezone="UTC+09:00",
        now=current,
    )
    remaining_raw = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_INTERVAL_EVENT
            )
        )
    )

    assert report.raw_events_deleted == 1
    assert any(
        scope.timezone == "UTC+09:00"
        for scope in report.affected_scopes
    )
    assert fixed_event is not None
    assert fixed_summary["total_active_minutes"] == 60.0
    assert fixed_event.derived_from["raw_event_count"] == 1
    assert fixed_event.derived_from["raw_evidence_sha256"] == evidence_digest(
        str(event.id) for event in remaining_raw
    )


def test_missing_activity_is_not_reported_as_zero(session) -> None:
    context = activity_summary_context(
        session,
        day=date(2026, 8, 1),
        timezone="UTC",
    )

    assert context["status"] == "insufficient_data"
    assert context["reason"] == "no_activity_summary"
    assert context["source_coverage"]["status"] == "no_data"
    assert "missing_is_not_zero" in context["limitations"]


def test_manual_deletion_rebuilds_only_from_remaining_raw_evidence(session) -> None:
    ingest_activity_batch(
        session,
        _interval_batch(
            [
                AppIntervalRecord(
                    source_record_id="keep",
                    start_at=datetime(2026, 8, 1, 9, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 9, 30, tzinfo=UTC),
                    state="active",
                    app_id="editor",
                ),
                AppIntervalRecord(
                    source_record_id="delete",
                    start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 10, 30, tzinfo=UTC),
                    state="active",
                    app_id="browser",
                ),
            ]
        ),
    )

    report = delete_activity_data(
        session,
        device_id="desktop-1",
        start=datetime(2026, 8, 1, 10, 15, tzinfo=UTC),
        end=datetime(2026, 8, 1, 10, 20, tzinfo=UTC),
        include_summaries=True,
        include_control=False,
    )
    for scope in report.affected_scopes:
        rebuild_affected_days(
            session,
            days=[scope.day],
            timezone=scope.timezone,
        )

    _, summary = get_daily_summary(
        session,
        day=date(2026, 8, 1),
        timezone="UTC",
    )
    remaining = list(
        session.scalars(select(WellnessEvent).where(WellnessEvent.event_type == APP_INTERVAL_EVENT))
    )

    assert report.raw_events_deleted == 1
    assert [row.source_record_id for row in remaining] == ["keep"]
    assert summary["total_active_minutes"] == 30.0


def test_natural_raw_expiry_keeps_longer_lived_hourly_and_daily_summaries(session) -> None:
    update_retention_policy(session, "activity_raw", "1d")
    ingest_activity_batch(
        session,
        _interval_batch(
            [
                AppIntervalRecord(
                    source_record_id="expiring-raw",
                    start_at=datetime(2026, 8, 1, 9, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                    state="active",
                    app_id="editor",
                )
            ]
        ),
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )
    session.add(
        AppUsageSample(
            device_id="desktop-1",
            bucket_start=datetime(2026, 8, 1, 9, tzinfo=UTC),
            app_package="legacy",
            foreground_seconds=60,
            launches=1,
            category="productivity",
        )
    )
    session.flush()

    report = run_activity_maintenance(
        session,
        now=datetime(2026, 8, 3, tzinfo=UTC),
    )
    raw_count = len(
        list(
            session.scalars(
                select(WellnessEvent).where(WellnessEvent.event_type == APP_INTERVAL_EVENT)
            )
        )
    )
    hourly_count = len(
        list(
            session.scalars(
                select(WellnessEvent).where(WellnessEvent.event_type == HOUR_SUMMARY_EVENT)
            )
        )
    )
    daily_count = len(
        list(
            session.scalars(
                select(WellnessEvent).where(WellnessEvent.event_type == DAY_SUMMARY_EVENT)
            )
        )
    )

    assert report.expired_events_deleted == 1
    assert report.compatibility_rows_deleted == 1
    assert raw_count == 0
    assert hourly_count == 1
    assert daily_count == 1


def test_shortening_activity_raw_retention_deletes_expired_legacy_rows_immediately(
    session,
) -> None:
    now = datetime.now(UTC)
    session.add(
        AppUsageSample(
            device_id="legacy-expired-on-policy-change",
            bucket_start=now - timedelta(days=2),
            app_package="legacy",
            foreground_seconds=60,
            launches=1,
            category="productivity",
        )
    )
    session.flush()

    update_retention_policy(session, "activity_raw", "1d")

    assert (
        session.scalar(
            select(AppUsageSample).where(
                AppUsageSample.device_id
                == "legacy-expired-on-policy-change"
            )
        )
        is None
    )


def test_switching_activity_raw_retention_to_forever_does_not_resurrect_legacy_rows(
    session,
) -> None:
    now = datetime.now(UTC)
    update_retention_policy(session, "activity_raw", "1d")
    session.add_all(
        [
            AppUsageSample(
                device_id="legacy-expired-before-forever",
                bucket_start=now - timedelta(days=2),
                app_package="expired",
                foreground_seconds=60,
                launches=1,
                category="productivity",
            ),
            AppUsageSample(
                device_id="legacy-retained-before-forever",
                bucket_start=now - timedelta(hours=12),
                app_package="retained",
                foreground_seconds=60,
                launches=1,
                category="productivity",
            ),
        ]
    )
    session.flush()

    update_retention_policy(session, "activity_raw", "forever")

    rows = list(
        session.scalars(
            select(AppUsageSample).order_by(AppUsageSample.app_package)
        )
    )
    assert [row.app_package for row in rows] == ["retained"]


@pytest.mark.parametrize("device_id", ("desktop-future-delete", None))
def test_unbounded_delete_removes_preexisting_future_activity(
    session,
    device_id,
) -> None:
    future = datetime(2026, 8, 3, 10, tzinfo=UTC)
    ingest_activity_batch(
        session,
        _interval_batch(
            [
                AppIntervalRecord(
                    source_record_id=f"future-delete-{device_id}",
                    start_at=future,
                    end_at=future + timedelta(hours=1),
                    state="active",
                    app_id="editor",
                )
            ],
            device_id="desktop-future-delete",
        ),
        now=future + timedelta(hours=2),
    )
    session.add(
        AppUsageSample(
            device_id="desktop-future-delete",
            bucket_start=future,
            app_package="legacy",
            foreground_seconds=60,
            launches=1,
            category="productivity",
        )
    )
    session.flush()

    report = delete_activity_data(
        session,
        device_id=device_id,
        start=None,
        end=None,
        include_summaries=True,
        include_control=False,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    assert report.raw_events_deleted == 1
    assert report.compatibility_rows_deleted == 1
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_INTERVAL_EVENT
            )
        )
        is None
    )
    assert session.scalar(select(AppUsageSample)) is None

    replay = ingest_activity_batch(
        session,
        _interval_batch(
            [
                AppIntervalRecord(
                    source_record_id=f"future-delete-{device_id}",
                    start_at=future,
                    end_at=future + timedelta(hours=1),
                    state="active",
                    app_id="editor",
                )
            ],
            device_id="desktop-future-delete",
        ),
        now=future + timedelta(hours=2),
    )
    tombstone = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == DELETION_TOMBSTONE_EVENT,
            WellnessEvent.source_record_id.not_like("%:identities:%"),
        )
    )

    assert replay.response.accepted == 0
    assert replay.response.tombstoned == 1
    assert tombstone is not None
    assert tombstone.payload["end"] == "2026-08-01T12:00:00+00:00"
    assert tombstone.payload["identity_digest_count"] >= 1
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_INTERVAL_EVENT
            )
        )
        is None
    )

    new_activity = ingest_activity_batch(
        session,
        _interval_batch(
            [
                AppIntervalRecord(
                    source_record_id=f"new-after-delete-{device_id}",
                    start_at=future + timedelta(hours=2),
                    end_at=future + timedelta(hours=3),
                    state="active",
                    app_id="editor",
                )
            ],
            device_id="desktop-future-delete",
        ),
        now=future + timedelta(hours=4),
    )

    assert new_activity.response.created == 1
    assert new_activity.response.tombstoned == 0


def test_unbounded_delete_tombstones_future_legacy_android_identity(
    session,
) -> None:
    future = datetime(2026, 8, 3, 10, tzinfo=UTC)
    session.add(
        AppUsageSample(
            device_id="legacy-future-delete",
            bucket_start=future,
            app_package="com.example.queued",
            foreground_seconds=300,
            launches=1,
            category="productivity",
        )
    )
    session.flush()

    delete_activity_data(
        session,
        device_id="legacy-future-delete",
        start=None,
        end=None,
        include_summaries=True,
        include_control=False,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )
    replay = ingest_activity_batch(
        session,
        android_batch(
            device_id="legacy-future-delete",
            timezone="UTC",
            samples=[
                SimpleNamespace(
                    bucket_start=future,
                    app_package="com.example.queued",
                    foreground_seconds=300,
                    launches=1,
                    category="productivity",
                )
            ],
            collected_at=future + timedelta(hours=1),
        ),
        now=future + timedelta(hours=2),
    )
    new_activity = ingest_activity_batch(
        session,
        android_batch(
            device_id="legacy-future-delete",
            timezone="UTC",
            samples=[
                SimpleNamespace(
                    bucket_start=future + timedelta(hours=3),
                    app_package="com.example.new",
                    foreground_seconds=300,
                    launches=1,
                    category="productivity",
                )
            ],
            collected_at=future + timedelta(hours=4),
        ),
        now=future + timedelta(hours=5),
    )

    assert replay.response.accepted == 0
    assert replay.response.tombstoned == 1
    assert new_activity.response.created == 1
    assert new_activity.response.tombstoned == 0


def test_deleted_source_identity_cannot_move_beyond_the_deleted_range(
    session,
) -> None:
    original = AppIntervalRecord(
        source_record_id="stable-source-identity",
        start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
        end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
        state="active",
        app_id="editor",
    )
    ingest_activity_batch(
        session,
        _interval_batch([original], device_id="desktop-moved-replay"),
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    delete_activity_data(
        session,
        device_id="desktop-moved-replay",
        start=datetime(2026, 8, 1, 9, tzinfo=UTC),
        end=datetime(2026, 8, 1, 12, tzinfo=UTC),
        include_summaries=True,
        include_control=False,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    moved_replay = ingest_activity_batch(
        session,
        _interval_batch(
            [
                original.model_copy(
                    update={
                        "start_at": datetime(2026, 8, 1, 13, tzinfo=UTC),
                        "end_at": datetime(2026, 8, 1, 14, tzinfo=UTC),
                    }
                )
            ],
            device_id="desktop-moved-replay",
        ),
        now=datetime(2026, 8, 1, 15, tzinfo=UTC),
    )
    new_identity = ingest_activity_batch(
        session,
        _interval_batch(
            [
                original.model_copy(
                    update={
                        "source_record_id": "new-source-identity",
                        "start_at": datetime(2026, 8, 1, 13, tzinfo=UTC),
                        "end_at": datetime(2026, 8, 1, 14, tzinfo=UTC),
                    }
                )
            ],
            device_id="desktop-moved-replay",
        ),
        now=datetime(2026, 8, 1, 15, tzinfo=UTC),
    )

    assert moved_replay.response.accepted == 0
    assert moved_replay.response.tombstoned == 1
    assert new_identity.response.created == 1
    assert new_identity.response.tombstoned == 0


def test_daily_coverage_uses_the_full_local_day_as_denominator(session) -> None:
    ingest_activity_batch(
        session,
        _interval_batch(
            [
                AppIntervalRecord(
                    source_record_id="one-known-hour",
                    start_at=datetime(2026, 8, 1, 9, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                    state="active",
                    app_id="editor",
                )
            ]
        ),
    )

    _, summary = get_daily_summary(
        session,
        day=date(2026, 8, 1),
        timezone="UTC",
    )
    overwork = overwork_context(
        session,
        day=date(2026, 8, 1),
        timezone="UTC",
    )

    assert summary["source_coverage"] == {
        "status": "partial",
        "ratio": 0.0417,
        "known_seconds": 3600,
        "expected_seconds": 86400,
        "hours_with_data": 1,
    }
    assert overwork["status"] == "insufficient_data"
    assert overwork["reason"] == "low_source_coverage"


def test_focus_coverage_uses_the_entire_requested_window(session) -> None:
    ingest_activity_batch(
        session,
        _interval_batch(
            [
                AppIntervalRecord(
                    source_record_id="focus-hour",
                    start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
                    state="active",
                    app_id="editor",
                    launches=4,
                )
            ]
        ),
    )

    context = activity_summary_context(
        session,
        day=date(2026, 8, 1),
        timezone="UTC",
    )
    focus = focus_context(
        session,
        start=datetime(2026, 8, 1, 9, tzinfo=UTC),
        end=datetime(2026, 8, 1, 14, tzinfo=UTC),
        timezone="UTC",
    )

    assert context["source_coverage"]["known_seconds"] == 3600
    assert focus["status"] == "insufficient_data"
    assert focus["reason"] == "low_source_coverage"
    assert focus["coverage"] == 0.2


def test_partial_hour_focus_metrics_use_exact_retained_raw_window(session) -> None:
    ingest_activity_batch(
        session,
        _interval_batch(
            [
                AppIntervalRecord(
                    source_record_id="partial-focus-hour",
                    start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
                    state="active",
                    app_id="editor",
                    launches=4,
                )
            ]
        ),
    )

    focus = focus_context(
        session,
        start=datetime(2026, 8, 1, 10, 30, tzinfo=UTC),
        end=datetime(2026, 8, 1, 11, 30, tzinfo=UTC),
        timezone="UTC",
    )

    assert focus["status"] == "ok"
    assert focus["metrics"]["total_active_minutes"] == 30.0
    assert focus["metrics"]["app_launches_or_switches"] == 0
    assert focus["coverage"] == 0.5
    assert "exact_window_from_retained_raw_events" in focus["limitations"]


def test_daily_summary_provenance_is_bounded_and_records_capability(session) -> None:
    ingest_activity_batch(
        session,
        _interval_batch(
            [
                AppIntervalRecord(
                    source_record_id=f"raw-{index}",
                    start_at=datetime(2026, 8, 1, 9, tzinfo=UTC) + timedelta(minutes=index),
                    end_at=datetime(2026, 8, 1, 9, 1, tzinfo=UTC) + timedelta(minutes=index),
                    state="active",
                    app_id="editor",
                )
                for index in range(10)
            ]
        ),
    )

    daily = session.scalar(
        select(WellnessEvent).where(WellnessEvent.event_type == DAY_SUMMARY_EVENT)
    )
    hourly = session.scalar(
        select(WellnessEvent).where(WellnessEvent.event_type == HOUR_SUMMARY_EVENT)
    )

    assert daily is not None
    assert hourly is not None
    assert daily.payload["capabilities"] == ["detailed"]
    assert hourly.payload["capabilities"] == ["detailed"]
    assert set(hourly.derived_from) == {
        "derivation_version",
        "raw_event_count",
        "raw_evidence_sha256",
    }
    assert (
        hourly.derived_from["derivation_version"]
        == SUMMARY_DERIVATION_VERSION
    )
    assert hourly.derived_from["raw_event_count"] == 10
    assert len(hourly.derived_from["raw_evidence_sha256"]) == 64
    assert set(daily.derived_from) == {
        "derivation_version",
        "raw_event_count",
        "raw_evidence_sha256",
        "hour_summary_ids",
    }
    assert (
        daily.derived_from["derivation_version"]
        == SUMMARY_DERIVATION_VERSION
    )
    assert daily.derived_from["raw_event_count"] == 10
    assert len(daily.derived_from["raw_evidence_sha256"]) == 64
    assert "event_ids" not in daily.derived_from


def test_legacy_summary_migration_reaggregates_complete_raw_by_provider_device(
    session,
) -> None:
    for provider in ("desktop-provider-a", "desktop-provider-b"):
        ingest_activity_batch(
            session,
            ActivityBatchIn(
                source_provider=provider,
                source_device="shared-device-id",
                platform=ActivityPlatform.MACOS,
                capability=ActivityCapability.DETAILED,
                timezone="UTC",
                records=[
                    AppIntervalRecord(
                        source_record_id=f"{provider}-active",
                        start_at=datetime(2026, 8, 1, 9, tzinfo=UTC),
                        end_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                        state="active",
                        app_id="editor",
                    )
                ],
            ),
            now=datetime(2026, 8, 1, 12, tzinfo=UTC),
        )

    summaries = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type.in_(
                    (HOUR_SUMMARY_EVENT, DAY_SUMMARY_EVENT)
                )
            )
        )
    )
    for summary in summaries:
        summary.derived_from = {
            key: value
            for key, value in summary.derived_from.items()
            if key != "derivation_version"
        }
        payload = dict(summary.payload)
        payload["total_active_minutes"] = 60.0
        payload["device_count"] = 1
        summary.payload = payload
    session.flush()

    report = migrate_activity_summary_derivations(
        session,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )
    _, daily = get_daily_summary(
        session,
        day=date(2026, 8, 1),
        timezone="UTC",
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    assert report.migrated_scopes == 1
    assert report.incompatible_scopes == 0
    assert daily["status"] == "ok"
    assert daily["total_active_minutes"] == 120.0
    assert daily["device_count"] == 2


def test_legacy_summary_without_complete_raw_is_blocked_not_republished(
    session,
) -> None:
    ingest_activity_batch(
        session,
        _interval_batch(
            [
                AppIntervalRecord(
                    source_record_id="legacy-missing-raw",
                    start_at=datetime(2026, 8, 1, 9, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                    state="active",
                    app_id="editor",
                )
            ]
        ),
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )
    for summary in session.scalars(
        select(WellnessEvent).where(
            WellnessEvent.event_type.in_(
                (HOUR_SUMMARY_EVENT, DAY_SUMMARY_EVENT)
            )
        )
    ):
        summary.derived_from = {
            key: value
            for key, value in summary.derived_from.items()
            if key != "derivation_version"
        }
    raw = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == APP_INTERVAL_EVENT
        )
    )
    assert raw is not None
    session.delete(raw)
    session.flush()

    report = migrate_activity_summary_derivations(
        session,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )
    event, daily = get_daily_summary(
        session,
        day=date(2026, 8, 1),
        timezone="UTC",
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )
    focus = focus_context(
        session,
        start=datetime(2026, 8, 1, 9, tzinfo=UTC),
        end=datetime(2026, 8, 1, 10, tzinfo=UTC),
        timezone="UTC",
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    assert report.migrated_scopes == 0
    assert report.incompatible_scopes == 1
    assert event is None
    assert daily["reason"] == LEGACY_SUMMARY_REASON
    assert focus["status"] == "insufficient_data"
    assert focus["reason"] == LEGACY_SUMMARY_REASON


def test_following_daily_summary_survives_baseline_refresh_after_raw_expiry(
    session,
) -> None:
    update_retention_policy(session, "activity_raw", "1d")
    for offset in (0, 1):
        ingest_activity_batch(
            session,
            _interval_batch(
                [
                    AppIntervalRecord(
                        source_record_id=f"raw-day-{offset}",
                        start_at=datetime(2026, 8, 1 + offset, 9, tzinfo=UTC),
                        end_at=datetime(2026, 8, 1 + offset, 10, tzinfo=UTC),
                        state="active",
                        app_id="editor",
                    )
                ]
            ),
            now=datetime(2026, 8, 1 + offset, 12, tzinfo=UTC),
        )

    run_activity_maintenance(
        session,
        now=datetime(2026, 8, 4, tzinfo=UTC),
    )
    rebuild_affected_days(
        session,
        days=[date(2026, 8, 1)],
        timezone="UTC",
    )

    surviving = next(
        (
            row
            for row in session.scalars(
                select(WellnessEvent).where(WellnessEvent.event_type == DAY_SUMMARY_EVENT)
            )
            if row.payload.get("date") == "2026-08-02"
        ),
        None,
    )

    assert surviving is not None
    assert surviving.payload["total_active_minutes"] == 60.0


def test_active_time_wins_when_source_reports_overlapping_idle(session) -> None:
    ingest_activity_batch(
        session,
        _interval_batch(
            [
                AppIntervalRecord(
                    source_record_id="active",
                    start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
                    state="active",
                    app_id="editor",
                ),
                AppIntervalRecord(
                    source_record_id="contradictory-idle",
                    start_at=datetime(2026, 8, 1, 10, 30, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 11, 30, tzinfo=UTC),
                    state="idle",
                ),
            ]
        ),
        rebuild_summaries=False,
    )
    events = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_INTERVAL_EVENT
            )
        )
    )

    summary = summarize_window(
        events,
        start=datetime(2026, 8, 1, 10, tzinfo=UTC),
        end=datetime(2026, 8, 1, 12, tzinfo=UTC),
        timezone="UTC",
    )

    assert summary["total_active_minutes"] == 60.0
    assert summary["idle_and_break_minutes"] == 30.0
    assert "active_idle_overlap_resolved_active_wins" in summary["limitations"]


def test_personal_baseline_ignores_days_below_minimum_coverage(session) -> None:
    for offset, hours in ((0, 6), (1, 6), (2, 1)):
        day = 29 + offset
        ingest_activity_batch(
            session,
            _interval_batch(
                [
                    AppIntervalRecord(
                        source_record_id=f"baseline-{offset}",
                        start_at=datetime(2026, 7, day, 9, tzinfo=UTC),
                        end_at=datetime(2026, 7, day, 9 + hours, tzinfo=UTC),
                        state="active",
                        app_id="editor",
                    )
                ]
            ),
            now=datetime(2026, 7, day, 20, tzinfo=UTC),
        )

    baseline = personal_baseline_delta(
        session,
        day=date(2026, 8, 1),
        timezone="UTC",
        current_minutes=300,
        lookback_days=3,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    assert baseline == {
        "status": "insufficient_data",
        "days_with_data": 2,
        "required_days": 3,
        "lookback_days": 3,
    }


def test_historical_rebuild_uses_one_injected_clock_for_baseline(session) -> None:
    update_retention_policy(session, "activity_daily", "7d")
    for day in range(20, 24):
        ingest_activity_batch(
            session,
            _interval_batch(
                [
                    AppIntervalRecord(
                        source_record_id=f"historical-baseline-{day}",
                        start_at=datetime(2026, 7, day, 0, tzinfo=UTC),
                        end_at=datetime(2026, 7, day, 12, tzinfo=UTC),
                        state="active",
                        app_id="editor",
                    )
                ]
            ),
            now=datetime(2026, 7, day, 13, tzinfo=UTC),
        )

    target = next(
        row
        for row in session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == DAY_SUMMARY_EVENT,
            )
        )
        if row.payload.get("date") == "2026-07-23"
    )

    assert target.payload["seven_day_baseline_delta"] == {
        "status": "ok",
        "days_with_data": 3,
        "lookback_days": 7,
        "baseline_minutes": 720.0,
        "delta_minutes": 0.0,
        "delta_percent": 0.0,
    }


def test_daily_retention_refreshes_following_baselines(session) -> None:
    update_retention_policy(session, "activity_daily", "7d")
    for day in range(6, 10):
        ingest_activity_batch(
            session,
            _interval_batch(
                [
                    AppIntervalRecord(
                        source_record_id=f"retained-baseline-{day}",
                        start_at=datetime(2026, 8, day, 0, tzinfo=UTC),
                        end_at=datetime(2026, 8, day, 12, tzinfo=UTC),
                        state="active",
                        app_id="editor",
                    )
                ]
            ),
            now=datetime(2026, 8, day, 13, tzinfo=UTC),
        )

    target = next(
        row
        for row in session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == DAY_SUMMARY_EVENT,
            )
        )
        if row.payload.get("date") == "2026-08-09"
    )
    assert target.payload["seven_day_baseline_delta"]["status"] == "ok"

    report = run_activity_maintenance(
        session,
        now=datetime(2026, 8, 15, 12, tzinfo=UTC),
    )
    session.refresh(target)

    assert report.expired_events_deleted >= 3
    assert target.payload["seven_day_baseline_delta"] == {
        "status": "insufficient_data",
        "days_with_data": 0,
        "required_days": 3,
        "lookback_days": 7,
    }


def test_storage_maintenance_delegates_activity_expiry_before_generic_purge(
    session,
    settings,
) -> None:
    update_retention_policy(session, "activity_daily", "7d")
    for day in range(6, 10):
        ingest_activity_batch(
            session,
            _interval_batch(
                [
                    AppIntervalRecord(
                        source_record_id=f"storage-baseline-{day}",
                        start_at=datetime(2026, 8, day, 0, tzinfo=UTC),
                        end_at=datetime(2026, 8, day, 12, tzinfo=UTC),
                        state="active",
                        app_id="editor",
                    )
                ]
            ),
            now=datetime(2026, 8, day, 13, tzinfo=UTC),
        )

    target = next(
        row
        for row in session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == DAY_SUMMARY_EVENT,
            )
        )
        if row.payload.get("date") == "2026-08-09"
    )
    assert target.payload["seven_day_baseline_delta"]["status"] == "ok"

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 15, 12, tzinfo=UTC),
    )
    session.refresh(target)

    assert target.payload["seven_day_baseline_delta"] == {
        "status": "insufficient_data",
        "days_with_data": 0,
        "required_days": 3,
        "lookback_days": 7,
    }


def test_daily_retention_refreshes_fixed_offset_following_baselines(
    session,
) -> None:
    update_retention_policy(session, "activity_daily", "7d")
    fixed_kst = timezone(timedelta(hours=9))
    fixed_kst_name = str(fixed_kst)
    for local_day in range(6, 10):
        local_start = datetime(2026, 8, local_day, tzinfo=fixed_kst)
        ingest_activity_batch(
            session,
            _interval_batch(
                [
                    AppIntervalRecord(
                        source_record_id=f"fixed-retained-baseline-{local_day}",
                        start_at=local_start.astimezone(UTC),
                        end_at=(local_start + timedelta(hours=12)).astimezone(UTC),
                        state="active",
                        app_id="editor",
                    )
                ],
                timezone_name="Asia/Seoul",
            ),
            rebuild_summaries=False,
            now=(local_start + timedelta(hours=13)).astimezone(UTC),
        )
        rebuild_day_summaries(
            session,
            day=date(2026, 8, local_day),
            timezone=fixed_kst,
            force_rebuild=True,
            now=(local_start + timedelta(hours=13)).astimezone(UTC),
        )

    target = next(
        row
        for row in session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == DAY_SUMMARY_EVENT,
            )
        )
        if (
            row.payload.get("date") == "2026-08-09"
            and row.timezone == fixed_kst_name
        )
    )
    assert target.payload["seven_day_baseline_delta"]["status"] == "ok"

    report = run_activity_maintenance(
        session,
        now=datetime(2026, 8, 15, 12, tzinfo=UTC),
    )
    session.refresh(target)

    assert report.expired_events_deleted >= 3
    assert target.payload["seven_day_baseline_delta"] == {
        "status": "insufficient_data",
        "days_with_data": 0,
        "required_days": 3,
        "lookback_days": 7,
    }


def test_expired_raw_is_not_used_for_partial_focus_before_maintenance(
    session,
) -> None:
    update_retention_policy(session, "activity_raw", "1d")
    ingest_activity_batch(
        session,
        _interval_batch(
            [
                AppIntervalRecord(
                    source_record_id="expired-partial-focus",
                    start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
                    state="active",
                    app_id="editor",
                    launches=4,
                )
            ]
        ),
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    focus = focus_context(
        session,
        start=datetime(2026, 8, 1, 10, 30, tzinfo=UTC),
        end=datetime(2026, 8, 1, 11, 30, tzinfo=UTC),
        timezone="UTC",
        now=datetime(2026, 8, 3, tzinfo=UTC),
    )

    assert focus["status"] == "insufficient_data"
    assert focus["reason"] == "partial_hour_requires_raw"
    assert focus["evidence_ids"] == []


def test_focus_uses_hourly_summaries_when_only_later_raw_is_retained(
    session,
) -> None:
    update_retention_policy(session, "activity_raw", "1d")
    ingest_activity_batch(
        session,
        _interval_batch(
            [
                AppIntervalRecord(
                    source_record_id="mixed-retention-morning",
                    start_at=datetime(2026, 8, 1, 9, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                    state="active",
                    app_id="editor",
                    launches=2,
                ),
                AppIntervalRecord(
                    source_record_id="mixed-retention-evening",
                    start_at=datetime(2026, 8, 1, 18, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 19, tzinfo=UTC),
                    state="active",
                    app_id="editor",
                    launches=3,
                ),
            ]
        ),
        now=datetime(2026, 8, 1, 20, tzinfo=UTC),
    )

    focus = focus_context(
        session,
        start=datetime(2026, 8, 1, 9, tzinfo=UTC),
        end=datetime(2026, 8, 1, 19, tzinfo=UTC),
        timezone="UTC",
        now=datetime(2026, 8, 2, 12, tzinfo=UTC),
    )

    assert focus["status"] == "insufficient_data"
    assert focus["reason"] == "low_source_coverage"
    assert focus["coverage"] == 0.2
    assert "exact_window_from_retained_raw_events" not in focus["limitations"]


def test_default_rebuild_preserves_summary_when_raw_provenance_is_incomplete(
    session,
) -> None:
    ingest_activity_batch(
        session,
        _interval_batch(
            [
                AppIntervalRecord(
                    source_record_id="original-a",
                    start_at=datetime(2026, 8, 1, 9, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                    state="active",
                    app_id="editor",
                ),
                AppIntervalRecord(
                    source_record_id="original-b",
                    start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
                    state="active",
                    app_id="editor",
                ),
            ]
        ),
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )
    original_summary = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == DAY_SUMMARY_EVENT
        )
    )
    removed = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == APP_INTERVAL_EVENT,
            WellnessEvent.source_record_id == "original-b",
        )
    )
    assert original_summary is not None
    assert removed is not None
    original_id = original_summary.id
    session.delete(removed)
    session.flush()
    # Build a repository-level inconsistent state to prove the defensive
    # default rebuild itself remains lossless. The service layer now rejects
    # this transition before persistence.
    raw_policy = ensure_activity_policies(session)["activity_raw"]
    unguarded_batch = _interval_batch(
        [
            AppIntervalRecord(
                source_record_id="new-c",
                start_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
                end_at=datetime(2026, 8, 1, 11, 30, tzinfo=UTC),
                state="active",
                app_id="editor",
            ),
            AppIntervalRecord(
                source_record_id="new-d",
                start_at=datetime(2026, 8, 1, 12, tzinfo=UTC),
                end_at=datetime(2026, 8, 1, 12, 30, tzinfo=UTC),
                state="active",
                app_id="editor",
            ),
        ]
    )
    for record in unguarded_batch.records:
        persist_activity_record(
            session,
            unguarded_batch,
            record,
            raw_policy=raw_policy,
        )

    rebuilt = rebuild_day_summaries(
        session,
        day=date(2026, 8, 1),
        timezone="UTC",
        force_rebuild=False,
        now=datetime(2026, 8, 1, 13, tzinfo=UTC),
    )

    assert rebuilt is not None
    assert rebuilt.id == original_id
    assert rebuilt.payload["total_active_minutes"] == 120.0
    assert rebuilt.derived_from["raw_event_count"] == 2


def test_ingest_rejects_change_when_same_day_raw_provenance_is_incomplete(
    session,
) -> None:
    update_retention_policy(session, "activity_raw", "1d")
    ingest_activity_batch(
        session,
        _interval_batch(
            [
                AppIntervalRecord(
                    source_record_id="expired-morning",
                    start_at=datetime(2026, 8, 1, 9, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                    state="active",
                    app_id="editor",
                ),
                AppIntervalRecord(
                    source_record_id="retained-evening",
                    start_at=datetime(2026, 8, 1, 18, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 19, tzinfo=UTC),
                    state="active",
                    app_id="editor",
                ),
            ]
        ),
        now=datetime(2026, 8, 1, 20, tzinfo=UTC),
    )

    with pytest.raises(ActivitySummaryProvenanceError):
        ingest_activity_batch(
            session,
            _interval_batch(
                [
                    AppIntervalRecord(
                        source_record_id="retained-evening",
                        start_at=datetime(2026, 8, 1, 18, tzinfo=UTC),
                        end_at=datetime(2026, 8, 1, 18, 30, tzinfo=UTC),
                        state="active",
                        app_id="editor",
                    )
                ]
            ),
            allow_replace=True,
            now=datetime(2026, 8, 2, 12, tzinfo=UTC),
        )

    duplicate = ingest_activity_batch(
        session,
        _interval_batch(
            [
                AppIntervalRecord(
                    source_record_id="retained-evening",
                    start_at=datetime(2026, 8, 1, 18, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 19, tzinfo=UTC),
                    state="active",
                    app_id="editor",
                )
            ]
        ),
        allow_replace=True,
        now=datetime(2026, 8, 2, 12, tzinfo=UTC),
    )

    session.expire_all()
    retained = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == APP_INTERVAL_EVENT,
            WellnessEvent.source_record_id == "retained-evening",
        )
    )
    _, summary = get_daily_summary(
        session,
        day=date(2026, 8, 1),
        timezone="UTC",
    )
    hourly = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == HOUR_SUMMARY_EVENT
            )
        )
    )

    assert retained is not None
    assert duplicate.response.duplicates == 1
    assert retained.payload["end_at"] == "2026-08-01T19:00:00+00:00"
    assert summary["total_active_minutes"] == 120.0
    assert len(hourly) == 2


def test_targeted_delete_fails_closed_when_raw_provenance_has_expired(
    session,
) -> None:
    update_retention_policy(session, "activity_raw", "1d")
    ingest_activity_batch(
        session,
        _interval_batch(
            [
                AppIntervalRecord(
                    source_record_id="expired-delete-evidence",
                    start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
                    state="active",
                    app_id="editor",
                )
            ]
        ),
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    with pytest.raises(ActivityDeletionUnsafeError):
        delete_activity_data(
            session,
            device_id="desktop-1",
            start=datetime(2026, 8, 1, 10, 15, tzinfo=UTC),
            end=datetime(2026, 8, 1, 10, 30, tzinfo=UTC),
            include_summaries=True,
            include_control=False,
            now=datetime(2026, 8, 3, tzinfo=UTC),
        )

    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == DELETION_TOMBSTONE_EVENT
            )
        )
        is None
    )
