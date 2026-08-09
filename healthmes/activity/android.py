"""Compatibility projection from the existing Android hourly collector."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.activity.contracts import (
    ActivityBatchIn,
    ActivityCapability,
    ActivityPlatform,
    AppHourRecord,
)
from healthmes.activity.identity import device_namespace
from healthmes.activity.service import (
    ActivityCollectionBlockedError,
    ActivityIngestResult,
    ingest_activity_batch,
)
from healthmes.store import AppUsageSample, WellnessEvent

ANDROID_PROVIDER = "android-usage"


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
) -> str:
    normalized = (
        bucket_start.replace(tzinfo=UTC)
        if bucket_start.tzinfo is None
        else bucket_start.astimezone(UTC)
    )
    package_digest = hashlib.sha256(app_package.encode("utf-8")).hexdigest()[:24]
    return f"hour:{device_namespace(device_id)}:{normalized.isoformat()}:{package_digest}"


def android_batch(
    *,
    device_id: str,
    samples: list[AndroidSampleLike],
    timezone: str,
    collected_at: datetime | None = None,
) -> ActivityBatchIn:
    return ActivityBatchIn(
        source_provider=ANDROID_PROVIDER,
        source_device=device_id,
        platform=ActivityPlatform.ANDROID,
        capability=ActivityCapability.AGGREGATE,
        timezone=timezone,
        collected_at=collected_at or datetime.now(UTC),
        records=[
            AppHourRecord(
                source_record_id=android_source_record_id(
                    device_id,
                    sample.bucket_start,
                    sample.app_package,
                ),
                bucket_start=sample.bucket_start,
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
    already_filtered: bool = False,
    excluded_count: int = 0,
) -> ActivityIngestResult:
    return ingest_activity_batch(
        session,
        android_batch(
            device_id=device_id,
            samples=samples,
            timezone=timezone,
            collected_at=collected_at,
        ),
        allow_replace=True,
        already_filtered=already_filtered,
        excluded_count=excluded_count,
    )


def backfill_android_canonical_events(
    session: Session,
    *,
    timezone: str,
) -> ActivityIngestResult | None:
    rows = list(
        session.scalars(
            select(AppUsageSample).order_by(
                AppUsageSample.bucket_start,
                AppUsageSample.device_id,
                AppUsageSample.app_package,
            )
        )
    )
    if not rows:
        return None
    existing_ids = set(
        session.scalars(
            select(WellnessEvent.source_record_id).where(
                WellnessEvent.source_provider == ANDROID_PROVIDER
            )
        )
    )
    # Grouping preserves each legacy device's collection control boundary.
    aggregate_result: ActivityIngestResult | None = None
    for device_id in sorted({row.device_id for row in rows}):
        device_rows = [
            row
            for row in rows
            if row.device_id == device_id
            and android_source_record_id(
                row.device_id,
                row.bucket_start,
                row.app_package,
            )
            not in existing_ids
        ]
        if not device_rows:
            continue
        try:
            aggregate_result = ingest_android_samples(
                session,
                device_id=device_id,
                samples=device_rows,
                timezone=timezone,
            )
        except ActivityCollectionBlockedError:
            # Existing legacy rows remain untouched when collection is disabled,
            # paused, or revoked; startup must not override that privacy state.
            continue
    return aggregate_result
