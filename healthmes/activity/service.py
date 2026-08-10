"""Application service for privacy-filtered canonical activity ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.activity.aggregation import (
    rebuild_affected_days,
    summary_raw_provenance_complete,
)
from healthmes.activity.contracts import (
    ActivityBatchIn,
    ActivityBatchOut,
    ActivityCollectionStatusUpdate,
    ActivityPermissionStatus,
    ActivityRecord,
    AppHourRecord,
)
from healthmes.activity.privacy import CollectionGate, filter_records
from healthmes.activity.repository import (
    ACTIVITY_RAW_CLASS,
    APP_HOUR_EVENT,
    APP_INTERVAL_EVENT,
    ActivityLocalScope,
    activity_write_lock,
    ensure_activity_policies,
    event_bounds,
    event_expiry,
    event_scopes,
    get_control_payload,
    persist_activity_record,
    record_scopes,
    tombstoned_record_ids,
    update_collection_status,
)
from healthmes.store import WellnessEvent

MAX_FUTURE_SKEW = timedelta(minutes=1)


class ActivityCollectionBlockedError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class StaleCollectionRevisionError(ValueError):
    pass


class ActivityLateDataError(ValueError):
    pass


class ActivityFutureDataError(ValueError):
    pass


class ActivitySourceModeConflictError(ValueError):
    pass


class ActivitySummaryProvenanceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ActivityIngestResult:
    response: ActivityBatchOut
    records: tuple[ActivityRecord, ...]
    changed_scopes: frozenset[ActivityLocalScope] = frozenset()


def _record_dates(record: ActivityRecord, timezone: str) -> set[date]:
    zone = ZoneInfo(timezone)
    start = record.bucket_start if isinstance(record, AppHourRecord) else record.start_at
    end = (
        record.bucket_start + timedelta(hours=1)
        if isinstance(record, AppHourRecord)
        else record.end_at
    )
    first = start.astimezone(zone).date()
    # End is exclusive. Subtracting one microsecond avoids marking the next
    # local day when an interval closes exactly at midnight.
    last = (end - timedelta(microseconds=1)).astimezone(zone).date()
    return {first + timedelta(days=offset) for offset in range((last - first).days + 1)}


def prepare_activity_batch(
    session: Session,
    batch: ActivityBatchIn,
    *,
    now: datetime | None = None,
) -> tuple[ActivityBatchIn, int, int, CollectionGate]:
    state = get_control_payload(
        session,
        batch.source_device,
        platform=batch.platform,
    )
    revision = int(state.get("config_revision", 0))
    if batch.collection_revision is not None and batch.collection_revision != revision:
        raise StaleCollectionRevisionError(
            f"collector configuration revision {batch.collection_revision} "
            f"does not match server revision {revision}"
        )
    records, excluded, gate = filter_records(batch.records, state, now=now)
    if not gate.allowed:
        raise ActivityCollectionBlockedError(gate.reason or "collection_blocked")
    tombstoned = tombstoned_record_ids(
        session,
        source_provider=batch.source_provider,
        device_id=batch.source_device,
        records=records,
    )
    filtered = batch.model_copy(
        update={
            "records": [
                record
                for record in records
                if record.source_record_id not in tombstoned
            ]
        }
    )
    return filtered, excluded, len(tombstoned), gate


def _reject_expired_late_data(
    records: list[ActivityRecord],
    *,
    raw_policy,
    now: datetime,
) -> None:
    if raw_policy.retention_days is None or not raw_policy.enabled:
        return
    expired = [
        record.source_record_id
        for record in records
        if (
            expiry := event_expiry(
                raw_policy,
                record.bucket_start
                if isinstance(record, AppHourRecord)
                else record.start_at,
            )
        )
        is not None
        and expiry <= now
    ]
    if expired:
        raise ActivityLateDataError(
            f"{len(expired)} activity record(s) are outside the raw retention window"
        )


def _reject_future_data(
    batch: ActivityBatchIn,
    *,
    now: datetime,
) -> None:
    future_limit = now + MAX_FUTURE_SKEW
    future_records = [
        record.source_record_id
        for record in batch.records
        if (
            record.bucket_start > future_limit
            if isinstance(record, AppHourRecord)
            else record.end_at > future_limit
        )
    ]
    if (
        "collected_at" in batch.model_fields_set
        and batch.collected_at > future_limit
    ):
        raise ActivityFutureDataError(
            "activity collected_at is beyond the allowed one-minute clock skew"
        )
    if future_records:
        raise ActivityFutureDataError(
            f"{len(future_records)} activity record(s) end in the future"
        )


def _reject_overlapping_source_mode(
    session: Session,
    batch: ActivityBatchIn,
) -> None:
    if not batch.records:
        return
    incoming_type = (
        APP_HOUR_EVENT
        if isinstance(batch.records[0], AppHourRecord)
        else APP_INTERVAL_EVENT
    )
    other_type = (
        APP_INTERVAL_EVENT
        if incoming_type == APP_HOUR_EVENT
        else APP_HOUR_EVENT
    )
    starts = [
        record.bucket_start
        if isinstance(record, AppHourRecord)
        else record.start_at
        for record in batch.records
    ]
    ends = [
        record.bucket_start + timedelta(hours=1)
        if isinstance(record, AppHourRecord)
        else record.end_at
        for record in batch.records
    ]
    window_start = min(starts)
    window_end = max(ends)
    existing = session.scalars(
        select(WellnessEvent).where(
            WellnessEvent.event_type == other_type,
            WellnessEvent.source_provider == batch.source_provider,
            WellnessEvent.source_device == batch.source_device,
            WellnessEvent.observed_at >= window_start - timedelta(hours=24),
            WellnessEvent.observed_at < window_end,
        )
    )
    for row in existing:
        existing_start, existing_end = event_bounds(row)
        if existing_end > window_start and existing_start < window_end:
            raise ActivitySourceModeConflictError(
                "one provider/device cannot mix overlapping hourly and interval activity"
            )


def _potential_summary_scopes(
    session: Session,
    batch: ActivityBatchIn,
) -> set[ActivityLocalScope]:
    scopes = {
        scope
        for record in batch.records
        for scope in record_scopes(record, batch.timezone)
    }
    source_record_ids = sorted(
        {record.source_record_id for record in batch.records}
    )
    # SQLite commonly caps one IN clause at 999 parameters.
    for offset in range(0, len(source_record_ids), 500):
        rows = session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.source_provider == batch.source_provider,
                WellnessEvent.source_record_id.in_(
                    source_record_ids[offset : offset + 500]
                ),
            )
        )
        for row in rows:
            scopes.update(event_scopes(row))
    return scopes


def ingest_activity_batch(
    session: Session,
    batch: ActivityBatchIn,
    *,
    allow_replace: bool = False,
    now: datetime | None = None,
    already_filtered: bool = False,
    excluded_count: int = 0,
    tombstoned_count: int = 0,
    rebuild_summaries: bool = True,
    prevalidated_summary_scopes: set[ActivityLocalScope] | None = None,
) -> ActivityIngestResult:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    with activity_write_lock():
        _reject_future_data(batch, now=current)
        if already_filtered:
            filtered = batch
            excluded = excluded_count
            tombstoned = tombstoned_count
        else:
            filtered, excluded, tombstoned, _ = prepare_activity_batch(
                session,
                batch,
                now=current,
            )
        raw_policy = ensure_activity_policies(session)[ACTIVITY_RAW_CLASS]
        _reject_expired_late_data(
            filtered.records,
            raw_policy=raw_policy,
            now=current,
        )
        _reject_overlapping_source_mode(session, filtered)
        potential_scopes = _potential_summary_scopes(session, filtered)
        unsafe_scopes = {
            scope
            for scope in potential_scopes
            if (
                prevalidated_summary_scopes is None
                or scope not in prevalidated_summary_scopes
            )
            if not summary_raw_provenance_complete(
                session,
                day=scope.day,
                timezone=scope.timezone,
                now=current,
            )
        }
        with session.begin_nested():
            created = updated = duplicates = 0
            changed_scopes: set[ActivityLocalScope] = set()
            for record in filtered.records:
                result = persist_activity_record(
                    session,
                    filtered,
                    record,
                    allow_replace=allow_replace,
                    raw_policy=raw_policy,
                )
                if result.state == "created":
                    created += 1
                elif result.state == "updated":
                    updated += 1
                else:
                    duplicates += 1
                if result.state in {"created", "updated"}:
                    changed_scopes.update(
                        record_scopes(record, filtered.timezone)
                    )
                    changed_scopes.update(result.previous_scopes)

            unsafe_changes = changed_scopes & unsafe_scopes
            if unsafe_changes:
                raise ActivitySummaryProvenanceError(
                    "activity change requires retained raw provenance for "
                    f"{len(unsafe_changes)} summary scope(s)"
                )

            affected_dates = sorted(
                {scope.day for scope in changed_scopes}
                or {
                    day
                    for record in filtered.records
                    for day in _record_dates(record, filtered.timezone)
                }
            )
            update_collection_status(
                session,
                filtered.source_device,
                ActivityCollectionStatusUpdate(
                    platform=filtered.platform,
                    capability=filtered.capability,
                    permission_status=ActivityPermissionStatus.GRANTED,
                    status_reason=None,
                    status_observed_at=filtered.collected_at,
                    last_collected_at=filtered.collected_at,
                    last_uploaded_at=current,
                    queue_depth=0,
                    queue_oldest_at=None,
                ),
                now=current,
            )
            if rebuild_summaries and changed_scopes:
                by_timezone: dict[str, set[date]] = {}
                for scope in changed_scopes:
                    by_timezone.setdefault(scope.timezone, set()).add(scope.day)
                for timezone, days in by_timezone.items():
                    rebuild_affected_days(
                        session,
                        days=days,
                        timezone=timezone,
                        force_rebuild=True,
                        now=current,
                    )
        return ActivityIngestResult(
            response=ActivityBatchOut(
                accepted=len(filtered.records),
                created=created,
                updated=updated,
                duplicates=duplicates,
                excluded=excluded,
                tombstoned=tombstoned,
                affected_dates=[value.isoformat() for value in affected_dates],
            ),
            records=tuple(filtered.records),
            changed_scopes=frozenset(changed_scopes),
        )
