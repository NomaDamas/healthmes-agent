"""Retention cleanup for activity events and the legacy Android read model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.job import Job
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from healthmes.activity.repository import (
    ACTIVITY_RAW_CLASS,
    APP_HOUR_EVENT,
    APP_INTERVAL_EVENT,
    COLLECTION_CONTROL_EVENT,
    CONTROL_PROVIDER,
    RAW_EVENT_TYPES,
    SUMMARY_EVENT_TYPES,
    SUMMARY_PROVIDER,
    as_utc,
    ensure_activity_policies,
    get_control_event,
)
from healthmes.store import AppUsageSample, WellnessEvent
from healthmes.store.session import session_scope

ACTIVITY_MAINTENANCE_JOB_ID = "healthmes-activity-maintenance"


@dataclass(frozen=True, slots=True)
class ActivityMaintenanceReport:
    expired_events_deleted: int
    compatibility_rows_deleted: int
    affected_dates: tuple[date, ...]


@dataclass(frozen=True, order=True, slots=True)
class ActivitySummaryScope:
    day: date
    timezone: str


@dataclass(frozen=True, slots=True)
class ActivityDeleteReport:
    raw_events_deleted: int
    summary_events_deleted: int
    control_events_deleted: int
    compatibility_rows_deleted: int
    affected_dates: tuple[date, ...]
    affected_scopes: tuple[ActivitySummaryScope, ...]


def _event_bounds(event: WellnessEvent) -> tuple[datetime, datetime]:
    start = as_utc(event.observed_at)
    if event.event_type == APP_HOUR_EVENT:
        return start, start + timedelta(hours=1)
    if event.event_type == APP_INTERVAL_EVENT:
        raw_end = event.payload.get("end_at")
        if isinstance(raw_end, str):
            try:
                end = as_utc(datetime.fromisoformat(raw_end))
            except ValueError:
                end = start
            if end > start:
                return start, end
    return start, start + timedelta(microseconds=1)


def _event_scopes(event: WellnessEvent) -> set[ActivitySummaryScope]:
    timezone = event.timezone or "UTC"
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        timezone = "UTC"
        zone = UTC
    start, end = _event_bounds(event)
    first = start.astimezone(zone).date()
    last = (end - timedelta(microseconds=1)).astimezone(zone).date()
    return {
        ActivitySummaryScope(
            day=first + timedelta(days=offset),
            timezone=timezone,
        )
        for offset in range((last - first).days + 1)
    }


def _event_overlaps(
    event: WellnessEvent,
    *,
    start: datetime | None,
    end: datetime | None,
) -> bool:
    event_start, event_end = _event_bounds(event)
    if start is not None and event_end <= as_utc(start):
        return False
    return end is None or event_start < as_utc(end)


def run_activity_maintenance(
    session: Session,
    *,
    now: datetime | None = None,
) -> ActivityMaintenanceReport:
    current = as_utc(now or datetime.now(UTC))
    policies = ensure_activity_policies(session)
    expired = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type.in_((*RAW_EVENT_TYPES, *SUMMARY_EVENT_TYPES)),
                WellnessEvent.expires_at.is_not(None),
                WellnessEvent.expires_at <= current,
            )
        )
    )
    affected_dates = {
        scope.day
        for event in expired
        if event.event_type in RAW_EVENT_TYPES
        for scope in _event_scopes(event)
    }
    for event in expired:
        session.delete(event)

    raw_policy = policies[ACTIVITY_RAW_CLASS]
    compatibility_deleted = 0
    if raw_policy.enabled and raw_policy.retention_days is not None:
        cutoff = current - timedelta(days=raw_policy.retention_days)
        result = session.execute(
            delete(AppUsageSample).where(AppUsageSample.bucket_start <= cutoff)
        )
        compatibility_deleted = int(result.rowcount or 0)
    session.flush()
    return ActivityMaintenanceReport(
        expired_events_deleted=len(expired),
        compatibility_rows_deleted=compatibility_deleted,
        affected_dates=tuple(sorted(affected_dates)),
    )


def build_activity_maintenance_job():
    def job() -> None:
        with session_scope() as session:
            run_activity_maintenance(session)

    return job


def register_activity_maintenance_job(
    scheduler: BackgroundScheduler,
    job,
    *,
    minutes: int = 60,
) -> Job:
    try:
        scheduler.remove_job(ACTIVITY_MAINTENANCE_JOB_ID)
    except JobLookupError:
        pass
    return scheduler.add_job(
        job,
        trigger=IntervalTrigger(minutes=minutes),
        id=ACTIVITY_MAINTENANCE_JOB_ID,
        name="HealthMes activity retention maintenance",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=600,
    )


def delete_activity_data(
    session: Session,
    *,
    device_id: str | None,
    start: datetime | None,
    end: datetime | None,
    include_summaries: bool,
    include_control: bool,
) -> ActivityDeleteReport:
    raw_stmt = select(WellnessEvent).where(WellnessEvent.event_type.in_(RAW_EVENT_TYPES))
    if device_id is not None:
        raw_stmt = raw_stmt.where(WellnessEvent.source_device == device_id)
    if start is not None:
        raw_stmt = raw_stmt.where(WellnessEvent.observed_at >= as_utc(start) - timedelta(hours=24))
    if end is not None:
        raw_stmt = raw_stmt.where(WellnessEvent.observed_at < as_utc(end))
    raw_rows = [
        row for row in session.scalars(raw_stmt) if _event_overlaps(row, start=start, end=end)
    ]
    affected_scopes = {scope for row in raw_rows for scope in _event_scopes(row)}
    for row in raw_rows:
        session.delete(row)

    usage_delete = delete(AppUsageSample)
    if device_id is not None:
        usage_delete = usage_delete.where(AppUsageSample.device_id == device_id)
    if start is not None:
        usage_delete = usage_delete.where(
            AppUsageSample.bucket_start > as_utc(start) - timedelta(hours=1)
        )
    if end is not None:
        usage_delete = usage_delete.where(AppUsageSample.bucket_start < as_utc(end))
    compatibility_result = session.execute(usage_delete)

    summary_rows: list[WellnessEvent] = []
    if include_summaries:
        summary_stmt = select(WellnessEvent).where(
            WellnessEvent.event_type.in_(SUMMARY_EVENT_TYPES),
            WellnessEvent.source_provider == SUMMARY_PROVIDER,
        )
        if start is not None:
            summary_stmt = summary_stmt.where(WellnessEvent.observed_at >= as_utc(start))
        if end is not None:
            summary_stmt = summary_stmt.where(WellnessEvent.observed_at < as_utc(end))
        summary_rows = list(session.scalars(summary_stmt))
        for row in summary_rows:
            affected_scopes.update(_event_scopes(row))
            session.delete(row)

    control_rows: list[WellnessEvent] = []
    if include_control:
        if device_id is not None:
            row = get_control_event(session, device_id)
            control_rows = [row] if row is not None else []
        else:
            control_rows = list(
                session.scalars(
                    select(WellnessEvent).where(
                        WellnessEvent.event_type == COLLECTION_CONTROL_EVENT,
                        WellnessEvent.source_provider == CONTROL_PROVIDER,
                    )
                )
            )
        for row in control_rows:
            session.delete(row)

    session.flush()
    return ActivityDeleteReport(
        raw_events_deleted=len(raw_rows),
        summary_events_deleted=len(summary_rows),
        control_events_deleted=len(control_rows),
        compatibility_rows_deleted=int(compatibility_result.rowcount or 0),
        affected_dates=tuple(sorted({scope.day for scope in affected_scopes})),
        affected_scopes=tuple(sorted(affected_scopes)),
    )
