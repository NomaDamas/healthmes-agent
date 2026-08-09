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

from healthmes.activity.aggregation import summary_raw_provenance_complete
from healthmes.activity.android import (
    ANDROID_PROVIDER,
    android_source_record_id,
)
from healthmes.activity.repository import (
    ACTIVITY_RAW_CLASS,
    CONTROL_EVENT_TYPES,
    CONTROL_PROVIDER,
    RAW_EVENT_TYPES,
    SUMMARY_EVENT_TYPES,
    SUMMARY_PROVIDER,
    activity_source_identity_digest,
    activity_write_lock,
    as_utc,
    create_deletion_tombstone,
    ensure_activity_policies,
    get_control_event,
)
from healthmes.activity.repository import (
    event_bounds as repository_event_bounds,
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
    tombstone_id: str


class ActivityDeletionUnsafeError(ValueError):
    """A targeted deletion cannot safely rewrite a durable aggregate."""


def _event_bounds(event: WellnessEvent) -> tuple[datetime, datetime]:
    return repository_event_bounds(event)


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
    with activity_write_lock():
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
        with activity_write_lock():
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
    now: datetime | None = None,
) -> ActivityDeleteReport:
    with activity_write_lock():
        current = as_utc(now or datetime.now(UTC))
        effective_end = min(as_utc(end), current) if end is not None else current
        selection_end = (
            None
            if start is None and end is None
            else effective_end
        )
        if start is not None and as_utc(start) >= effective_end:
            raise ValueError("activity deletion range must include past time")

        raw_stmt = select(WellnessEvent).where(
            WellnessEvent.event_type.in_(RAW_EVENT_TYPES)
        )
        if device_id is not None:
            raw_stmt = raw_stmt.where(WellnessEvent.source_device == device_id)
        if start is not None:
            raw_stmt = raw_stmt.where(
                WellnessEvent.observed_at >= as_utc(start) - timedelta(hours=24)
            )
        if selection_end is not None:
            raw_stmt = raw_stmt.where(
                WellnessEvent.observed_at < selection_end
            )
        raw_rows = [
            row
            for row in session.scalars(raw_stmt)
            if _event_overlaps(row, start=start, end=selection_end)
        ]
        affected_scopes = {
            scope for row in raw_rows for scope in _event_scopes(row)
        }

        all_summary_rows = list(
            session.scalars(
                select(WellnessEvent).where(
                    WellnessEvent.event_type.in_(SUMMARY_EVENT_TYPES),
                    WellnessEvent.source_provider == SUMMARY_PROVIDER,
                )
            )
        )
        full_global_delete = (
            device_id is None and start is None and end is None
        )
        safety_scopes = set(affected_scopes)
        if not full_global_delete:
            for row in all_summary_rows:
                if (start is not None or end is not None) and not _event_overlaps(
                    row,
                    start=start,
                    end=effective_end,
                ):
                    continue
                safety_scopes.update(_event_scopes(row))
        if not full_global_delete:
            incomplete = [
                scope
                for scope in sorted(safety_scopes)
                if not summary_raw_provenance_complete(
                    session,
                    day=scope.day,
                    timezone=scope.timezone,
                    now=current,
                )
            ]
            if incomplete:
                raise ActivityDeletionUnsafeError(
                    "targeted deletion requires retained raw provenance for "
                    f"{len(incomplete)} summary scope(s)"
                )

        usage_stmt = select(AppUsageSample)
        if device_id is not None:
            usage_stmt = usage_stmt.where(AppUsageSample.device_id == device_id)
        if start is not None:
            usage_stmt = usage_stmt.where(
                AppUsageSample.bucket_start > as_utc(start) - timedelta(hours=1)
            )
        if selection_end is not None:
            usage_stmt = usage_stmt.where(
                AppUsageSample.bucket_start < selection_end
            )
        compatibility_rows = list(session.scalars(usage_stmt))

        blocked_identity_digests = {
            activity_source_identity_digest(
                source_provider=str(row.source_provider),
                source_device=str(row.source_device),
                source_record_id=str(row.source_record_id),
            )
            for row in raw_rows
        }
        for row in compatibility_rows:
            blocked_identity_digests.add(
                activity_source_identity_digest(
                    source_provider=ANDROID_PROVIDER,
                    source_device=row.device_id,
                    source_record_id=android_source_record_id(
                        row.device_id,
                        row.bucket_start,
                        row.app_package,
                    ),
                )
            )

        tombstone = create_deletion_tombstone(
            session,
            device_id=device_id,
            start=start,
            end=effective_end,
            blocked_identity_digests=blocked_identity_digests,
            now=current,
        )

        for row in raw_rows:
            session.delete(row)
        for row in compatibility_rows:
            session.delete(row)

        summary_rows: list[WellnessEvent] = []
        if include_summaries:
            if full_global_delete:
                summary_rows = all_summary_rows
            else:
                summary_rows = [
                    row
                    for row in all_summary_rows
                    if set(_event_scopes(row)) & affected_scopes
                ]
            for row in summary_rows:
                affected_scopes.update(_event_scopes(row))
                session.delete(row)
        elif full_global_delete:
            affected_scopes.update(
                scope
                for row in all_summary_rows
                for scope in _event_scopes(row)
            )

        control_rows: list[WellnessEvent] = []
        if include_control:
            control_statement = select(WellnessEvent).where(
                WellnessEvent.event_type.in_(CONTROL_EVENT_TYPES),
                WellnessEvent.source_provider == CONTROL_PROVIDER,
            )
            if device_id is not None:
                control_statement = control_statement.where(
                    WellnessEvent.source_device == device_id
                )
            control_rows = list(session.scalars(control_statement))
            # A legacy row may not have source_device populated.
            if device_id is not None:
                legacy = get_control_event(session, device_id)
                if legacy is not None and legacy not in control_rows:
                    control_rows.append(legacy)
            for row in control_rows:
                session.delete(row)

        session.flush()
        return ActivityDeleteReport(
            raw_events_deleted=len(raw_rows),
            summary_events_deleted=len(summary_rows),
            control_events_deleted=len(control_rows),
            compatibility_rows_deleted=len(compatibility_rows),
            affected_dates=tuple(
                sorted({scope.day for scope in affected_scopes})
            ),
            affected_scopes=tuple(sorted(affected_scopes)),
            tombstone_id=str(tombstone.id),
        )
