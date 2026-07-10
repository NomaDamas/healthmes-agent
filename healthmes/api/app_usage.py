"""App-usage batch ingest from the Android collector (docs/PLAN.md §7).

The companion app (``apps/android-usage/``) buckets
``UsageStatsManager.queryEvents`` output into hourly buckets and POSTs the
batch every ~30 minutes via WorkManager. Because the current (still-growing)
hour is re-sent on every run, ingest is an **upsert** on the natural key
``(device_id, bucket_start, app_package)`` — matching the store's unique
constraint — with last-write-wins values.
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from healthmes.api.common import UTCDateTime
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
def ingest_batch(body: AppUsageBatchIn, session: SessionDep) -> AppUsageBatchOut:
    """Upsert a batch of usage samples for one device."""
    # Dedupe inside the payload (last occurrence wins) so one flush never
    # violates the unique constraint with itself.
    deduped: dict[tuple, AppUsageSampleIn] = {
        (sample.bucket_start, sample.app_package): sample for sample in body.samples
    }

    created = updated = 0
    for sample in deduped.values():
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
    session.commit()
    return AppUsageBatchOut(accepted=len(deduped), created=created, updated=updated)
