"""Compatibility projection from the existing Android hourly collector."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.activity.contracts import (
    ActivityBatchIn,
    ActivityBatchOut,
    ActivityCapability,
    ActivityPlatform,
    AppHourRecord,
)
from healthmes.activity.identity import device_namespace
from healthmes.activity.locking import lock_activity_write_plane
from healthmes.activity.repository import (
    ACTIVITY_RAW_CLASS,
    activity_write_lock,
    as_utc,
    ensure_activity_policies,
    event_expiry,
)
from healthmes.activity.service import (
    MAX_FUTURE_SKEW,
    ActivityCollectionBlockedError,
    ActivityFutureDataError,
    ActivityIngestResult,
    ActivitySummaryProvenanceError,
    ingest_activity_batch,
)
from healthmes.store import AppUsageSample, WellnessEvent

ANDROID_PROVIDER = "android-usage"
BACKFILL_PAGE_SIZE = 500


class AndroidSampleLike(Protocol):
    bucket_start: datetime
    app_package: str
    foreground_seconds: int
    launches: int
    category: str | None


def android_source_record_id(
    device_id: str,
    bucket_start: datetime,
    app_package: str,
    collection_generation: int = 0,
) -> str:
    if collection_generation < 0:
        raise ValueError("collection_generation must be non-negative")
    normalized = (
        bucket_start.replace(tzinfo=UTC)
        if bucket_start.tzinfo is None
        else bucket_start.astimezone(UTC)
    )
    package_digest = hashlib.sha256(app_package.encode("utf-8")).hexdigest()[:24]
    generation_segment = (
        "" if collection_generation == 0 else f"{collection_generation}:"
    )
    return (
        f"hour:{device_namespace(device_id)}:{generation_segment}"
        f"{normalized.isoformat()}:{package_digest}"
    )


def android_batch(
    *,
    device_id: str,
    samples: list[AndroidSampleLike],
    timezone: str,
    collected_at: datetime | None = None,
    collection_revision: int | None = None,
    collection_generation: int = 0,
) -> ActivityBatchIn:
    return ActivityBatchIn(
        source_provider=ANDROID_PROVIDER,
        source_device=device_id,
        platform=ActivityPlatform.ANDROID,
        capability=ActivityCapability.AGGREGATE,
        timezone=timezone,
        collected_at=collected_at or datetime.now(UTC),
        collection_revision=collection_revision,
        records=[
            AppHourRecord(
                source_record_id=android_source_record_id(
                    device_id,
                    sample.bucket_start,
                    sample.app_package,
                    int(
                        getattr(
                            sample,
                            "collection_generation",
                            collection_generation,
                        )
                    ),
                ),
                bucket_start=(
                    sample.bucket_start.replace(tzinfo=UTC)
                    if sample.bucket_start.tzinfo is None
                    else sample.bucket_start.astimezone(UTC)
                ),
                app_id=sample.app_package,
                foreground_seconds=sample.foreground_seconds,
                launches=sample.launches,
                category=sample.category,
                # The legacy payload omits explicit collector coverage. Never
                # claim a complete hour merely because one app row exists.
                coverage_seconds=None,
                bucket_complete=True,
            )
            for sample in samples
        ],
    )


def ingest_android_samples(
    session: Session,
    *,
    device_id: str,
    samples: list[AndroidSampleLike],
    timezone: str,
    collected_at: datetime | None = None,
    collection_revision: int | None = None,
    collection_generation: int = 0,
    already_filtered: bool = False,
    excluded_count: int = 0,
    tombstoned_count: int = 0,
    rebuild_summaries: bool = True,
    update_permission_status: bool = False,
    now: datetime | None = None,
) -> ActivityIngestResult:
    return ingest_activity_batch(
        session,
        android_batch(
            device_id=device_id,
            samples=samples,
            timezone=timezone,
            collected_at=collected_at,
            collection_revision=collection_revision,
            collection_generation=collection_generation,
        ),
        allow_replace=True,
        already_filtered=already_filtered,
        excluded_count=excluded_count,
        tombstoned_count=tombstoned_count,
        rebuild_summaries=rebuild_summaries,
        update_permission_status=update_permission_status,
        now=now,
    )


def backfill_android_canonical_events(
    session: Session,
    *,
    timezone: str,
    now: datetime | None = None,
) -> ActivityIngestResult | None:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    with activity_write_lock():
        lock_activity_write_plane(session)
        raw_policy = ensure_activity_policies(session)[ACTIVITY_RAW_CLASS]
        last_id = None
        found_rows = False
        created = updated = duplicates = excluded = tombstoned = 0
        accepted = 0
        affected_dates: set[str] = set()
        while True:
            statement = select(AppUsageSample).order_by(AppUsageSample.id).limit(
                BACKFILL_PAGE_SIZE
            )
            if last_id is not None:
                statement = statement.where(AppUsageSample.id > last_id)
            rows = list(session.scalars(statement))
            if not rows:
                break
            found_rows = True
            last_id = rows[-1].id
            source_ids = {
                android_source_record_id(
                    row.device_id,
                    row.bucket_start,
                    row.app_package,
                    row.collection_generation,
                )
                for row in rows
            }
            existing_ids = set(
                session.scalars(
                    select(WellnessEvent.source_record_id).where(
                        WellnessEvent.source_provider == ANDROID_PROVIDER,
                        WellnessEvent.source_record_id.in_(source_ids),
                    )
                )
            )
            eligible_rows = [
                row
                for row in rows
                if android_source_record_id(
                    row.device_id,
                    row.bucket_start,
                    row.app_package,
                    row.collection_generation,
                )
                not in existing_ids
                and (
                    event_expiry(raw_policy, row.bucket_start)
                    is None
                    or event_expiry(raw_policy, row.bucket_start) > current
                )
                and as_utc(row.bucket_start) <= current + MAX_FUTURE_SKEW
            ]
            for device_id, collection_generation in sorted(
                {
                    (row.device_id, row.collection_generation)
                    for row in eligible_rows
                }
            ):
                device_rows = [
                    row
                    for row in eligible_rows
                    if row.device_id == device_id
                    and row.collection_generation == collection_generation
                ]
                try:
                    result = ingest_android_samples(
                        session,
                        device_id=device_id,
                        samples=device_rows,
                        timezone=timezone,
                        collected_at=current,
                        collection_generation=collection_generation,
                        update_permission_status=False,
                        now=current,
                    )
                except (
                    ActivityCollectionBlockedError,
                    ActivityFutureDataError,
                    ActivitySummaryProvenanceError,
                ):
                    # Startup must never override collection privacy or
                    # replace a summary whose retained raw provenance is
                    # incomplete. Leave the legacy row untouched and continue
                    # migrating other independent devices/pages.
                    continue
                accepted += result.response.accepted
                created += result.response.created
                updated += result.response.updated
                duplicates += result.response.duplicates
                excluded += result.response.excluded
                tombstoned += result.response.tombstoned
                affected_dates.update(result.response.affected_dates)
        if not found_rows:
            return None
        return ActivityIngestResult(
            response=ActivityBatchOut(
                accepted=accepted,
                created=created,
                updated=updated,
                duplicates=duplicates,
                excluded=excluded,
                tombstoned=tombstoned,
                affected_dates=sorted(affected_dates),
            ),
            records=(),
        )
