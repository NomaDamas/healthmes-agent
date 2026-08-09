from datetime import UTC, date, datetime, timedelta, timezone

from sqlalchemy import select

from healthmes.activity.aggregation import (
    get_daily_summary,
    local_day_bounds,
    rebuild_affected_days,
    rebuild_day_summaries,
    summarize_window,
)
from healthmes.activity.context import (
    activity_summary_context,
    focus_context,
    overwork_context,
)
from healthmes.activity.contracts import (
    ActivityBatchIn,
    ActivityCapability,
    ActivityPlatform,
    AppIntervalRecord,
)
from healthmes.activity.maintenance import (
    delete_activity_data,
    run_activity_maintenance,
)
from healthmes.activity.repository import (
    APP_INTERVAL_EVENT,
    DAY_SUMMARY_EVENT,
    HOUR_SUMMARY_EVENT,
)
from healthmes.activity.service import ingest_activity_batch
from healthmes.storage import update_retention_policy
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
        collected_at=datetime(2026, 8, 1, 23, 59, tzinfo=UTC),
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


def test_partial_hour_focus_metrics_are_prorated_and_marked(session) -> None:
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
    assert focus["metrics"]["app_launches_or_switches"] == 2
    assert focus["coverage"] == 0.5
    assert "partial_hour_metrics_prorated" in focus["limitations"]


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
        "raw_event_count",
        "raw_evidence_sha256",
    }
    assert hourly.derived_from["raw_event_count"] == 10
    assert len(hourly.derived_from["raw_evidence_sha256"]) == 64
    assert set(daily.derived_from) == {
        "raw_event_count",
        "raw_evidence_sha256",
        "hour_summary_ids",
    }
    assert daily.derived_from["raw_event_count"] == 10
    assert len(daily.derived_from["raw_evidence_sha256"]) == 64
    assert "event_ids" not in daily.derived_from


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
