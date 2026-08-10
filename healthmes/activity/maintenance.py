"""Retention cleanup for activity events and the legacy Android read model."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from apscheduler.job import Job
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from healthmes.activity.aggregation import (
    BASELINE_DAYS,
    refresh_existing_day_baseline,
    summary_raw_provenance_complete,
)
from healthmes.activity.android import (
    ANDROID_PROVIDER,
    android_source_record_id,
)
from healthmes.activity.locking import lock_activity_write_plane
from healthmes.activity.repository import (
    ACTIVITY_RAW_CLASS,
    APP_HOUR_EVENT,
    APP_INTERVAL_EVENT,
    CONTROL_EVENT_TYPES,
    CONTROL_PROVIDER,
    DAY_SUMMARY_EVENT,
    RAW_EVENT_TYPES,
    SUMMARY_EVENT_TYPES,
    SUMMARY_PROVIDER,
    ActivityChangeWindow,
    activity_source_identity_digest,
    activity_write_lock,
    as_utc,
    create_deletion_tombstone,
    ensure_activity_policies,
    fixed_offset_summary_scopes_by_change,
    get_control_event,
    invalidate_activitywatch_imports,
)
from healthmes.activity.repository import (
    event_bounds as repository_event_bounds,
)
from healthmes.store import AppUsageSample, WellnessEvent
from healthmes.store.session import session_scope
from healthmes.timezones import parse_timezone

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


class ActivityDeletionGranularityError(ValueError):
    """A coarse aggregate cannot represent the requested partial deletion."""


def _event_bounds(event: WellnessEvent) -> tuple[datetime, datetime]:
    return repository_event_bounds(event)


def _event_scopes(event: WellnessEvent) -> set[ActivitySummaryScope]:
    timezone = event.timezone or "UTC"
    try:
        zone = parse_timezone(timezone)
    except ValueError:
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


def _range_fully_covers_event(
    event: WellnessEvent,
    *,
    start: datetime | None,
    end: datetime | None,
) -> bool:
    event_start, event_end = _event_bounds(event)
    return (
        (start is None or as_utc(start) <= event_start)
        and (end is None or as_utc(end) >= event_end)
    )


def _fragment_source_record_id(
    event: WellnessEvent,
    *,
    start: datetime,
    end: datetime,
) -> str:
    digest = hashlib.sha256(
        (
            f"{event.source_provider}|{event.source_device}|"
            f"{event.source_record_id}|{start.isoformat()}|{end.isoformat()}"
        ).encode()
    ).hexdigest()[:40]
    return f"delete-fragment-{digest}"


def _interval_fragments_after_delete(
    event: WellnessEvent,
    *,
    start: datetime | None,
    end: datetime | None,
    recorded_at: datetime,
) -> list[WellnessEvent]:
    if event.event_type != APP_INTERVAL_EVENT:
        return []
    event_start, event_end = _event_bounds(event)
    delete_start = as_utc(start) if start is not None else None
    delete_end = as_utc(end) if end is not None else None
    spans: list[tuple[datetime, datetime, bool]] = []
    if delete_start is not None and event_start < delete_start:
        spans.append((event_start, min(delete_start, event_end), True))
    if delete_end is not None and delete_end < event_end:
        spans.append((max(delete_end, event_start), event_end, False))

    fragments: list[WellnessEvent] = []
    for fragment_start, fragment_end, keeps_original_start in spans:
        if fragment_end <= fragment_start:
            continue
        payload = dict(event.payload)
        payload["start_at"] = fragment_start.isoformat()
        payload["end_at"] = fragment_end.isoformat()
        if not keeps_original_start:
            payload["launches"] = 0
        quality_flags = dict(event.quality_flags or {})
        quality_flags["deletion_fragment"] = True
        fragments.append(
            WellnessEvent(
                event_type=event.event_type,
                schema_version=event.schema_version,
                observed_at=fragment_start,
                recorded_at=recorded_at,
                timezone=event.timezone,
                source_provider=event.source_provider,
                source_device=event.source_device,
                source_record_id=_fragment_source_record_id(
                    event,
                    start=fragment_start,
                    end=fragment_end,
                ),
                capture_method=event.capture_method,
                quality_flags=quality_flags,
                confidence=event.confidence,
                coverage=event.coverage,
                sensitivity=event.sensitivity,
                consent_scope=event.consent_scope,
                retention_policy_id=event.retention_policy_id,
                expires_at=event.expires_at,
                payload=payload,
                raw_object_id=None,
                derived_from={
                    "fragment_of_event_id": str(event.id),
                    "fragment_of_source_record_id": event.source_record_id,
                    "fragmented_at": recorded_at.isoformat(),
                },
            )
        )
    return fragments


def run_activity_maintenance(
    session: Session,
    *,
    now: datetime | None = None,
) -> ActivityMaintenanceReport:
    with activity_write_lock():
        lock_activity_write_plane(session)
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
        expired_daily_scopes: set[ActivitySummaryScope] = set()
        for event in expired:
            if (
                event.event_type != DAY_SUMMARY_EVENT
                or not isinstance(event.payload, dict)
            ):
                continue
            raw_day = event.payload.get("date")
            timezone = event.payload.get("timezone")
            if not isinstance(raw_day, str) or not isinstance(timezone, str):
                continue
            try:
                expired_day = date.fromisoformat(raw_day)
                parse_timezone(timezone)
            except ValueError:
                continue
            expired_daily_scopes.add(
                ActivitySummaryScope(day=expired_day, timezone=timezone)
            )
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
        following_scopes = {
            ActivitySummaryScope(
                day=scope.day + timedelta(days=offset),
                timezone=scope.timezone,
            )
            for scope in expired_daily_scopes
            for offset in range(1, BASELINE_DAYS + 1)
        }
        for scope in sorted(following_scopes):
            refresh_existing_day_baseline(
                session,
                day=scope.day,
                timezone=scope.timezone,
                now=current,
            )
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
        lock_activity_write_plane(session)
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
        partial_hour_rows = [
            row
            for row in raw_rows
            if row.event_type == APP_HOUR_EVENT
            and not _range_fully_covers_event(
                row,
                start=start,
                end=selection_end,
            )
        ]
        if partial_hour_rows:
            raise ActivityDeletionGranularityError(
                "hourly aggregate activity can only be deleted as complete buckets"
            )
        affected_scopes = {
            scope for row in raw_rows for scope in _event_scopes(row)
        }
        alias_changes: list[ActivityChangeWindow] = []
        for row in raw_rows:
            row_start, row_end = _event_bounds(row)
            alias_changes.append(
                ActivityChangeWindow(
                    key=str(row.id),
                    start=row_start,
                    end=row_end,
                    timezone=row.timezone or "UTC",
                )
            )
        affected_scopes.update(
            ActivitySummaryScope(
                day=scope.day,
                timezone=scope.timezone,
            )
            for scopes in fixed_offset_summary_scopes_by_change(
                session,
                alias_changes,
                now=current,
            ).values()
            for scope in scopes
        )

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
        for row in compatibility_rows:
            bucket_end = as_utc(row.bucket_start) + timedelta(hours=1)
            if (
                (start is not None and as_utc(start) > as_utc(row.bucket_start))
                or (
                    selection_end is not None
                    and as_utc(selection_end) < bucket_end
                )
            ):
                raise ActivityDeletionGranularityError(
                    "hourly compatibility activity can only be deleted as complete buckets"
                )

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
                        row.collection_generation,
                    ),
                )
            )

        invalidate_activitywatch_imports(
            session,
            device_id=device_id,
            now=current,
        )
        tombstone = create_deletion_tombstone(
            session,
            device_id=device_id,
            start=start,
            end=effective_end,
            blocked_identity_digests=blocked_identity_digests,
            now=current,
        )

        fragments = [
            fragment
            for row in raw_rows
            for fragment in _interval_fragments_after_delete(
                row,
                start=start,
                end=selection_end,
                recorded_at=current,
            )
        ]
        session.add_all(fragments)
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
