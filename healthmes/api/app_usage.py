"""App-usage batch ingest from the Android collector (docs/PLAN.md §7).

The companion app (``apps/android-usage/``) buckets
``UsageStatsManager.queryEvents`` output into hourly buckets and POSTs the
batch every ~30 minutes via WorkManager. Because the current (still-growing)
hour is re-sent on every run, ingest is an **upsert** on the natural key
``(device_id, bucket_start, app_package)`` — matching the store's unique
constraint — with last-write-wins values.
"""

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from healthmes.activity.android import android_batch, android_source_record_id
from healthmes.activity.repository import ActivityConflictError
from healthmes.activity.service import (
    ActivityCollectionBlockedError,
    StaleCollectionRevisionError,
    ingest_activity_batch,
    prepare_activity_batch,
)
from healthmes.api.common import UTCDateTime
from healthmes.api.errors import APIError
from healthmes.config import resolve_timezone
from healthmes.store import AppUsageSample
from healthmes.store.session import SessionDep

router = APIRouter(prefix="/v1/app-usage", tags=["app-usage"])

MAX_BATCH_SAMPLES = 1000


class AppUsageSampleIn(BaseModel):
    """One app's foreground usage within one (hourly) bucket."""

    bucket_start: UTCDateTime
    app_package: str = Field(min_length=1, max_length=255)
    foreground_seconds: int = Field(ge=0, le=24 * 3600)
    launches: int = Field(default=0, ge=0, le=100_000)
    category: str | None = Field(default=None, max_length=64)


class AppUsageBatchIn(BaseModel):
    """Batch payload sent by the collector."""

    device_id: str = Field(min_length=1, max_length=64)
    samples: list[AppUsageSampleIn] = Field(min_length=1, max_length=MAX_BATCH_SAMPLES)


class AppUsageBatchOut(BaseModel):
    """Ingest acknowledgement (counts after in-payload dedup)."""

    accepted: int
    created: int
    updated: int


@router.post("/batch")
def ingest_batch(
    body: AppUsageBatchIn,
    request: Request,
    session: SessionDep,
) -> AppUsageBatchOut:
    """Upsert a batch of usage samples for one device."""
    # Dedupe inside the payload (last occurrence wins) so one flush never
    # violates the unique constraint with itself.
    deduped: dict[tuple, AppUsageSampleIn] = {
        (sample.bucket_start, sample.app_package): sample for sample in body.samples
    }

    samples = list(deduped.values())
    timezone = str(resolve_timezone(request.app.state.settings))
    canonical = android_batch(
        device_id=body.device_id,
        samples=samples,
        timezone=timezone,
    )
    try:
        filtered, excluded, _ = prepare_activity_batch(session, canonical)
    except ActivityCollectionBlockedError as exc:
        raise APIError(409, "activity_collection_blocked", exc.reason) from exc
    except StaleCollectionRevisionError as exc:
        raise APIError(409, "stale_collection_revision", str(exc)) from exc
    allowed_ids = {record.source_record_id for record in filtered.records}
    allowed_samples = [
        sample
        for sample in samples
        if android_source_record_id(
            body.device_id,
            sample.bucket_start,
            sample.app_package,
        )
        in allowed_ids
    ]

    created = updated = 0
    for sample in allowed_samples:
        existing = session.scalar(
            select(AppUsageSample).where(
                AppUsageSample.device_id == body.device_id,
                AppUsageSample.bucket_start == sample.bucket_start,
                AppUsageSample.app_package == sample.app_package,
            )
        )
        if existing is None:
            session.add(
                AppUsageSample(
                    device_id=body.device_id,
                    bucket_start=sample.bucket_start,
                    app_package=sample.app_package,
                    foreground_seconds=sample.foreground_seconds,
                    launches=sample.launches,
                    category=sample.category,
                )
            )
            created += 1
        else:
            existing.foreground_seconds = sample.foreground_seconds
            existing.launches = sample.launches
            existing.category = sample.category
            updated += 1
    try:
        ingest_activity_batch(
            session,
            filtered,
            allow_replace=True,
            already_filtered=True,
            excluded_count=excluded,
        )
    except ActivityConflictError as exc:  # defensive: replace mode should own this
        raise APIError(409, "activity_source_conflict", str(exc)) from exc
    session.commit()
    return AppUsageBatchOut(
        accepted=len(allowed_samples),
        created=created,
        updated=updated,
    )
