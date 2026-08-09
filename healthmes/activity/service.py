"""Application service for privacy-filtered canonical activity ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from healthmes.activity.aggregation import rebuild_affected_days
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
    ensure_activity_policies,
    get_control_payload,
    persist_activity_record,
    update_collection_status,
)


class ActivityCollectionBlockedError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class StaleCollectionRevisionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ActivityIngestResult:
    response: ActivityBatchOut
    records: tuple[ActivityRecord, ...]


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
) -> tuple[ActivityBatchIn, int, CollectionGate]:
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
    filtered = batch.model_copy(update={"records": records})
    return filtered, excluded, gate


def ingest_activity_batch(
    session: Session,
    batch: ActivityBatchIn,
    *,
    allow_replace: bool = False,
    now: datetime | None = None,
    already_filtered: bool = False,
    excluded_count: int = 0,
    rebuild_summaries: bool = True,
) -> ActivityIngestResult:
    current = now or datetime.now(UTC)
    if already_filtered:
        filtered = batch
        excluded = excluded_count
    else:
        filtered, excluded, _ = prepare_activity_batch(
            session,
            batch,
            now=current,
        )
    raw_policy = ensure_activity_policies(session)[ACTIVITY_RAW_CLASS]
    created = updated = duplicates = 0
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

    affected_dates = sorted(
        {day for record in filtered.records for day in _record_dates(record, filtered.timezone)}
    )
    update_collection_status(
        session,
        filtered.source_device,
        ActivityCollectionStatusUpdate(
            platform=filtered.platform,
            capability=filtered.capability,
            permission_status=ActivityPermissionStatus.GRANTED,
            status_reason=None,
            last_collected_at=filtered.collected_at,
            last_uploaded_at=current,
            queue_depth=0,
            queue_oldest_at=None,
        ),
        now=current,
    )
    if rebuild_summaries and affected_dates:
        rebuild_affected_days(
            session,
            days=affected_dates,
            timezone=filtered.timezone,
        )
    return ActivityIngestResult(
        response=ActivityBatchOut(
            accepted=len(filtered.records),
            created=created,
            updated=updated,
            duplicates=duplicates,
            excluded=excluded,
            affected_dates=[value.isoformat() for value in affected_dates],
        ),
        records=tuple(filtered.records),
    )
