"""App-usage batch ingest from the Android collector (docs/PLAN.md §7).

The companion app (``apps/android-usage/``) buckets
``UsageStatsManager.queryEvents`` output into hourly buckets and POSTs the
batch every ~30 minutes via WorkManager. Because the current (still-growing)
hour is re-sent on every run, ingest is an **upsert** on the natural key
``(device_id, collection_generation, bucket_start, app_package)`` — matching
the store's unique constraint. Ordered snapshots may revise a provisional
hour, while stale snapshots and rewrites of completed hours fail closed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from healthmes.activity.aggregation import (
    rebuild_affected_days,
    summary_raw_provenance_complete,
)
from healthmes.activity.android import (
    ANDROID_BUCKET_SNAPSHOT_EVENT,
    ANDROID_PROVIDER,
    android_batch,
    android_source_record_id,
    android_source_record_prefix,
)
from healthmes.activity.contracts import (
    ActivityBatchIn,
    ActivityCapability,
    ActivityCollectionStatusUpdate,
    ActivityPermissionStatus,
    ActivityPlatform,
    validate_timezone,
)
from healthmes.activity.locking import lock_activity_write_plane
from healthmes.activity.privacy import collection_gate
from healthmes.activity.repository import (
    ACTIVITY_RAW_CLASS,
    APP_HOUR_EVENT,
    ActivityChangeWindow,
    ActivityConflictError,
    ActivityLocalScope,
    activity_write_lock,
    ensure_activity_policies,
    event_bounds,
    event_expiry,
    event_scopes,
    fixed_offset_summary_scopes_by_change,
    get_control_payload,
    update_collection_status,
)
from healthmes.activity.service import (
    MAX_FUTURE_SKEW,
    ActivityCollectionBlockedError,
    ActivityFutureDataError,
    ActivityLateDataError,
    ActivitySourceModeConflictError,
    ActivitySummaryProvenanceError,
    StaleCollectionRevisionError,
    ingest_activity_batch,
    prepare_activity_batch,
)
from healthmes.api.common import UTCDateTime
from healthmes.api.errors import APIError
from healthmes.config import resolve_timezone
from healthmes.store import AppUsageSample, WellnessEvent
from healthmes.store.session import SessionDep

router = APIRouter(prefix="/v1/app-usage", tags=["app-usage"])

MAX_BATCH_SAMPLES = 1000
MAX_BUCKET_SNAPSHOTS = 500


class AppUsageSampleIn(BaseModel):
    """One app's foreground usage within one (hourly) bucket."""

    bucket_start: UTCDateTime
    app_package: str = Field(min_length=1, max_length=255)
    foreground_seconds: int = Field(ge=0, le=24 * 3600)
    launches: int = Field(default=0, ge=0, le=100_000)
    category: str | None = Field(default=None, max_length=64)
    bucket_complete: bool = False
    snapshot_sequence: int = Field(default=0, ge=0, le=2**63 - 1)


class AppUsageBucketSnapshotIn(BaseModel):
    """App manifest for one device hour and snapshot sequence."""

    bucket_start: UTCDateTime
    bucket_complete: bool
    snapshot_sequence: int = Field(ge=1, le=2**63 - 1)
    source_set_complete: bool = True
    app_packages: list[str] = Field(max_length=MAX_BATCH_SAMPLES)

    @field_validator("app_packages")
    @classmethod
    def validate_app_packages(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item or len(item) > 255 for item in cleaned):
            raise ValueError("snapshot app_packages must be non-empty and <= 255 chars")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("snapshot app_packages must be unique")
        return sorted(cleaned)


class AppUsageBatchIn(BaseModel):
    """Batch payload sent by the collector."""

    device_id: str = Field(min_length=1, max_length=64)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    collection_revision: int = Field(ge=0)
    collection_generation: int = Field(ge=0, le=2**63 - 1)
    pairing_revision: int = Field(default=0, ge=0, le=2**63 - 1)
    samples: list[AppUsageSampleIn] = Field(
        default_factory=list,
        max_length=MAX_BATCH_SAMPLES,
    )
    bucket_snapshots: list[AppUsageBucketSnapshotIn] = Field(
        default_factory=list,
        max_length=MAX_BUCKET_SNAPSHOTS,
    )

    @field_validator("timezone")
    @classmethod
    def validate_optional_timezone(cls, value: str | None) -> str | None:
        return validate_timezone(value) if value is not None else None

    @model_validator(mode="after")
    def validate_snapshot_manifests(self) -> AppUsageBatchIn:
        if not self.bucket_snapshots:
            if not self.samples:
                raise ValueError("samples or bucket_snapshots are required")
            if any(sample.snapshot_sequence > 0 for sample in self.samples):
                raise ValueError(
                    "ordered Android samples require bucket_snapshots"
                )
            return self

        manifests = {
            snapshot.bucket_start: snapshot
            for snapshot in self.bucket_snapshots
        }
        if len(manifests) != len(self.bucket_snapshots):
            raise ValueError("bucket_snapshots must contain unique hours")
        grouped: dict[datetime, list[AppUsageSampleIn]] = {}
        for sample in self.samples:
            grouped.setdefault(sample.bucket_start, []).append(sample)
        if any(
            len(bucket_samples)
            != len({sample.app_package for sample in bucket_samples})
            for bucket_samples in grouped.values()
        ):
            raise ValueError(
                "bucket snapshot samples must contain unique app packages"
            )
        if set(grouped) - set(manifests):
            raise ValueError("every sample must belong to a bucket snapshot")
        for bucket_start, snapshot in manifests.items():
            bucket_samples = grouped.get(bucket_start, [])
            packages = {sample.app_package for sample in bucket_samples}
            if not snapshot.source_set_complete and packages:
                raise ValueError(
                    "an incomplete source set must not carry app samples"
                )
            if packages != set(snapshot.app_packages):
                raise ValueError(
                    "bucket snapshot manifest does not match its sample packages"
                )
            if any(
                sample.snapshot_sequence != snapshot.snapshot_sequence
                or sample.bucket_complete != snapshot.bucket_complete
                for sample in bucket_samples
            ):
                raise ValueError(
                    "bucket snapshot sequence/completion must match every sample"
                )
        return self


class AppUsageBatchOut(BaseModel):
    """Ingest acknowledgement (counts after in-payload dedup)."""

    accepted: int
    created: int
    updated: int
    suppressed: int = 0


def _dedupe_samples(
    samples: list[AppUsageSampleIn],
) -> list[AppUsageSampleIn]:
    deduped: dict[tuple[datetime, str], AppUsageSampleIn] = {}
    for sample in samples:
        key = (sample.bucket_start, sample.app_package)
        previous = deduped.get(key)
        if previous is not None and previous != sample:
            raise ActivityConflictError(
                "one Android batch contains conflicting snapshots for the "
                "same hour and app"
            )
        deduped[key] = sample
    return list(deduped.values())


@dataclass(frozen=True, slots=True)
class _BucketSnapshotPlan:
    manifest: AppUsageBucketSnapshotIn
    digest: str
    state_event: WellnessEvent | None
    apply: bool


def _snapshot_source_record_id(
    *,
    device_id: str,
    collection_generation: int,
    bucket_start: datetime,
) -> str:
    identity = android_source_record_prefix(
        device_id,
        bucket_start,
        collection_generation,
    )
    return "snapshot:" + hashlib.sha256(identity.encode()).hexdigest()


def _snapshot_manifest_digest(
    manifest: AppUsageBucketSnapshotIn,
    samples: list[AppUsageSampleIn],
) -> str:
    payload = {
        "bucket_start": manifest.bucket_start.isoformat(),
        "bucket_complete": manifest.bucket_complete,
        "source_set_complete": manifest.source_set_complete,
        "app_packages": manifest.app_packages,
        "samples": [
            sample.model_dump(
                mode="json",
                exclude={"snapshot_sequence"},
            )
            for sample in sorted(samples, key=lambda item: item.app_package)
        ],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()


def _snapshot_state_payload(event: WellnessEvent) -> dict[str, Any]:
    payload = event.payload
    if not isinstance(payload, dict):
        raise ActivityConflictError("Android bucket snapshot state is malformed")
    sequence = payload.get("snapshot_sequence")
    digest = payload.get("manifest_sha256")
    complete = payload.get("bucket_complete")
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 1
        or not isinstance(digest, str)
        or len(digest) != 64
        or not isinstance(complete, bool)
    ):
        raise ActivityConflictError("Android bucket snapshot state is malformed")
    return payload


def _plan_bucket_snapshot(
    session,
    *,
    device_id: str,
    collection_generation: int,
    manifest: AppUsageBucketSnapshotIn,
    samples: list[AppUsageSampleIn],
) -> _BucketSnapshotPlan:
    source_record_id = _snapshot_source_record_id(
        device_id=device_id,
        collection_generation=collection_generation,
        bucket_start=manifest.bucket_start,
    )
    event = session.scalar(
        select(WellnessEvent)
        .where(
            WellnessEvent.event_type == ANDROID_BUCKET_SNAPSHOT_EVENT,
            WellnessEvent.source_provider == ANDROID_PROVIDER,
            WellnessEvent.source_record_id == source_record_id,
        )
        .with_for_update()
    )
    digest = _snapshot_manifest_digest(manifest, samples)
    if event is None:
        return _BucketSnapshotPlan(manifest, digest, None, True)
    previous = _snapshot_state_payload(event)
    previous_sequence = int(previous["snapshot_sequence"])
    previous_digest = str(previous["manifest_sha256"])
    previous_complete = bool(previous["bucket_complete"])
    if manifest.snapshot_sequence < previous_sequence:
        raise ActivityConflictError(
            "Android hourly snapshot is older than the latest accepted manifest"
        )
    if manifest.snapshot_sequence == previous_sequence:
        if digest != previous_digest:
            raise ActivityConflictError(
                "Android hourly snapshot sequence was reused with different content"
            )
        return _BucketSnapshotPlan(manifest, digest, event, False)
    if previous_complete:
        if digest != previous_digest:
            raise ActivityConflictError(
                "a completed Android hour cannot be rewritten"
            )
        return _BucketSnapshotPlan(manifest, digest, event, False)
    return _BucketSnapshotPlan(manifest, digest, event, True)


def _persist_bucket_snapshot_state(
    session,
    *,
    plan: _BucketSnapshotPlan,
    device_id: str,
    collection_generation: int,
    timezone: str,
    now: datetime,
) -> None:
    policy = ensure_activity_policies(session)[ACTIVITY_RAW_CLASS]
    payload = {
        "device_id": device_id,
        "collection_generation": collection_generation,
        "bucket_start": plan.manifest.bucket_start.isoformat(),
        "snapshot_sequence": plan.manifest.snapshot_sequence,
        "bucket_complete": plan.manifest.bucket_complete,
        "manifest_sha256": plan.digest,
        "app_count": len(plan.manifest.app_packages),
    }
    event = plan.state_event
    if event is None:
        event = WellnessEvent(
            event_type=ANDROID_BUCKET_SNAPSHOT_EVENT,
            schema_version=1,
            observed_at=plan.manifest.bucket_start,
            recorded_at=now,
            timezone=timezone,
            source_provider=ANDROID_PROVIDER,
            source_device=device_id,
            source_record_id=_snapshot_source_record_id(
                device_id=device_id,
                collection_generation=collection_generation,
                bucket_start=plan.manifest.bucket_start,
            ),
            capture_method="derived",
            quality_flags=None,
            confidence=None,
            coverage=None,
            sensitivity="activity-control",
            consent_scope="personal",
            retention_policy_id=policy.id,
            expires_at=event_expiry(policy, plan.manifest.bucket_start),
            payload=payload,
            derived_from=None,
        )
        session.add(event)
    else:
        event.recorded_at = now
        event.timezone = timezone
        event.retention_policy_id = policy.id
        event.expires_at = event_expiry(policy, plan.manifest.bucket_start)
        event.payload = payload
    session.flush([event])


def _deletion_scopes(
    session,
    rows: list[WellnessEvent],
    *,
    now: datetime,
) -> set[ActivityLocalScope]:
    scopes = {
        scope
        for row in rows
        for scope in event_scopes(row)
    }
    changes = []
    for row in rows:
        start, end = event_bounds(row)
        changes.append(
            ActivityChangeWindow(
                key=str(row.id),
                start=start,
                end=end,
                timezone=row.timezone or "UTC",
            )
        )
    scopes.update(
        scope
        for values in fixed_offset_summary_scopes_by_change(
            session,
            changes,
            now=now,
        ).values()
        for scope in values
    )
    incomplete = [
        scope
        for scope in sorted(scopes)
        if not summary_raw_provenance_complete(
            session,
            day=scope.day,
            timezone=scope.timezone,
            now=now,
        )
    ]
    if incomplete:
        raise ActivitySummaryProvenanceError(
            "Android snapshot replacement requires retained raw provenance "
            f"for {len(incomplete)} summary scope(s)"
        )
    return scopes


def _update_compatibility_sample(
    existing: AppUsageSample,
    sample: AppUsageSampleIn,
    *,
    category: str | None,
) -> bool:
    incoming_content = (
        sample.foreground_seconds,
        sample.launches,
        category,
        sample.bucket_complete,
    )
    previous_content = (
        existing.foreground_seconds,
        existing.launches,
        existing.category,
        existing.bucket_complete,
    )
    if incoming_content == previous_content:
        if (
            not existing.bucket_complete
            and sample.snapshot_sequence > existing.snapshot_sequence
        ):
            existing.snapshot_sequence = sample.snapshot_sequence
            return True
        return False
    if existing.bucket_complete:
        raise ActivityConflictError(
            "a complete Android hour cannot be reopened or rewritten"
        )
    if existing.snapshot_sequence > 0 or sample.snapshot_sequence > 0:
        if sample.snapshot_sequence <= existing.snapshot_sequence:
            raise ActivityConflictError(
                "Android hour snapshot_sequence is stale or conflicting"
            )
    elif (
        sample.foreground_seconds < existing.foreground_seconds
        or sample.launches < existing.launches
    ):
        raise ActivityConflictError(
            "a provisional Android hour cannot move backwards"
        )
    existing.foreground_seconds = sample.foreground_seconds
    existing.launches = sample.launches
    existing.category = category
    existing.bucket_complete = sample.bucket_complete
    existing.snapshot_sequence = sample.snapshot_sequence
    return True


def _insert_or_update_compatibility_sample(
    session,
    *,
    body: AppUsageBatchIn,
    sample: AppUsageSampleIn,
    category: str | None,
) -> str:
    existing = session.scalar(
        select(AppUsageSample)
        .where(
            AppUsageSample.device_id == body.device_id,
            AppUsageSample.collection_generation
            == body.collection_generation,
            AppUsageSample.bucket_start == sample.bucket_start,
            AppUsageSample.app_package == sample.app_package,
        )
        .with_for_update()
    )
    if existing is None:
        row = AppUsageSample(
            device_id=body.device_id,
            collection_generation=body.collection_generation,
            bucket_start=sample.bucket_start,
            app_package=sample.app_package,
            foreground_seconds=sample.foreground_seconds,
            launches=sample.launches,
            category=category,
            bucket_complete=sample.bucket_complete,
            snapshot_sequence=sample.snapshot_sequence,
        )
        try:
            with session.begin_nested():
                session.add(row)
                session.flush([row])
            return "created"
        except IntegrityError:
            existing = session.scalar(
                select(AppUsageSample)
                .where(
                    AppUsageSample.device_id == body.device_id,
                    AppUsageSample.collection_generation
                    == body.collection_generation,
                    AppUsageSample.bucket_start == sample.bucket_start,
                    AppUsageSample.app_package == sample.app_package,
                )
                .with_for_update()
            )
            if existing is None:
                raise
    return (
        "updated"
        if _update_compatibility_sample(
            existing,
            sample,
            category=category,
        )
        else "duplicate"
    )


def _validate_empty_snapshot_gate(
    state: dict[str, Any],
    *,
    collection_revision: int,
    now: datetime,
) -> None:
    revision = int(state.get("config_revision", 0))
    if collection_revision != revision:
        raise StaleCollectionRevisionError(
            f"collector configuration revision {collection_revision} "
            f"does not match server revision {revision}"
        )
    gate = collection_gate(state, now=now)
    if not gate.allowed:
        raise ActivityCollectionBlockedError(
            gate.reason or "collection_blocked"
        )


def _reject_expired_bucket_snapshots(
    session,
    *,
    snapshots: list[AppUsageBucketSnapshotIn],
    now: datetime,
) -> None:
    policy = ensure_activity_policies(session)[ACTIVITY_RAW_CLASS]
    if not policy.enabled or policy.retention_days is None:
        return
    if any(
        (
            expiry := event_expiry(policy, snapshot.bucket_start)
        ) is not None
        and expiry <= now
        for snapshot in snapshots
        if snapshot.source_set_complete
    ):
        raise ActivityLateDataError(
            "Android hourly snapshot is outside the configured raw retention"
        )


def _reject_future_bucket_snapshots(
    snapshots: list[AppUsageBucketSnapshotIn],
    *,
    now: datetime,
) -> None:
    future_limit = now + MAX_FUTURE_SKEW
    if any(snapshot.bucket_start > future_limit for snapshot in snapshots):
        raise ActivityFutureDataError(
            "Android hourly snapshot is beyond the allowed one-minute clock skew"
        )


def _ingest_authoritative_snapshots(
    *,
    body: AppUsageBatchIn,
    session,
    state: dict[str, Any],
    timezone: str,
    now: datetime,
) -> AppUsageBatchOut:
    _reject_future_bucket_snapshots(
        body.bucket_snapshots,
        now=now,
    )
    _reject_expired_bucket_snapshots(
        session,
        snapshots=body.bucket_snapshots,
        now=now,
    )
    samples = _dedupe_samples(body.samples)
    samples_by_bucket: dict[datetime, list[AppUsageSampleIn]] = {}
    for sample in samples:
        samples_by_bucket.setdefault(sample.bucket_start, []).append(sample)

    filtered_batch: ActivityBatchIn | None = None
    excluded = tombstoned = 0
    allowed_records: dict[str, Any] = {}
    allowed_samples: list[tuple[AppUsageSampleIn, Any]] = []
    if samples:
        canonical = android_batch(
            device_id=body.device_id,
            samples=samples,
            timezone=timezone,
            collected_at=now,
            collection_revision=body.collection_revision,
            collection_generation=body.collection_generation,
        )
        filtered_batch, excluded, tombstoned, _ = prepare_activity_batch(
            session,
            canonical,
            now=now,
            control_payload=state,
        )
        allowed_records = {
            record.source_record_id: record
            for record in filtered_batch.records
        }
        allowed_samples = [
            (sample, allowed_records[source_record_id])
            for sample in samples
            if (
                source_record_id := android_source_record_id(
                    body.device_id,
                    sample.bucket_start,
                    sample.app_package,
                    body.collection_generation,
                )
            )
            in allowed_records
        ]
    else:
        _validate_empty_snapshot_gate(
            state,
            collection_revision=body.collection_revision,
            now=now,
        )

    allowed_by_bucket: dict[datetime, list[tuple[AppUsageSampleIn, Any]]] = {}
    for sample, record in allowed_samples:
        allowed_by_bucket.setdefault(sample.bucket_start, []).append(
            (sample, record)
        )

    # Android UsageEvents cannot prove that an event-free query window had no
    # foreground app: a session may have started before the lookback edge.
    # Incomplete source sets are therefore heartbeats only. They must never
    # delete rows, seal an hour, or conflict with an earlier complete manifest.
    plans = [
        _plan_bucket_snapshot(
            session,
            device_id=body.device_id,
            collection_generation=body.collection_generation,
            manifest=manifest,
            samples=samples_by_bucket.get(manifest.bucket_start, []),
        )
        for manifest in body.bucket_snapshots
        if manifest.source_set_complete
    ]
    apply_plans = [plan for plan in plans if plan.apply]

    canonical_to_delete: list[WellnessEvent] = []
    compatibility_to_delete: list[AppUsageSample] = []
    apply_source_ids: set[str] = set()
    for plan in apply_plans:
        incoming = allowed_by_bucket.get(
            plan.manifest.bucket_start,
            [],
        )
        expected_packages = {sample.app_package for sample, _ in incoming}
        expected_source_ids = {
            record.source_record_id for _, record in incoming
        }
        apply_source_ids.update(expected_source_ids)

        compatibility_rows = list(
            session.scalars(
                select(AppUsageSample)
                .where(
                    AppUsageSample.device_id == body.device_id,
                    AppUsageSample.collection_generation
                    == body.collection_generation,
                    AppUsageSample.bucket_start
                    == plan.manifest.bucket_start,
                )
                .with_for_update()
            )
        )
        compatibility_to_delete.extend(
            row
            for row in compatibility_rows
            if row.app_package not in expected_packages
        )
        prefix = android_source_record_prefix(
            body.device_id,
            plan.manifest.bucket_start,
            body.collection_generation,
        )
        canonical_rows = list(
            session.scalars(
                select(WellnessEvent)
                .where(
                    WellnessEvent.event_type == APP_HOUR_EVENT,
                    WellnessEvent.source_provider == ANDROID_PROVIDER,
                    WellnessEvent.source_device == body.device_id,
                    WellnessEvent.observed_at
                    == plan.manifest.bucket_start,
                    WellnessEvent.source_record_id.like(f"{prefix}%"),
                )
                .with_for_update()
            )
        )
        canonical_to_delete.extend(
            row
            for row in canonical_rows
            if row.source_record_id not in expected_source_ids
        )

    prevalidated_scopes = _deletion_scopes(
        session,
        canonical_to_delete,
        now=now,
    )
    for row in compatibility_to_delete:
        session.delete(row)
    for row in canonical_to_delete:
        session.delete(row)
    if compatibility_to_delete or canonical_to_delete:
        session.flush()

    created = updated = 0
    for plan in apply_plans:
        for sample, record in allowed_by_bucket.get(
            plan.manifest.bucket_start,
            [],
        ):
            result = _insert_or_update_compatibility_sample(
                session,
                body=body,
                sample=sample,
                category=record.category,
            )
            created += result == "created"
            updated += result == "updated"

    changed_scopes = set(prevalidated_scopes)
    if filtered_batch is not None and apply_source_ids:
        apply_batch = filtered_batch.model_copy(
            update={
                "records": [
                    record
                    for record in filtered_batch.records
                    if record.source_record_id in apply_source_ids
                ]
            }
        )
        result = ingest_activity_batch(
            session,
            apply_batch,
            allow_replace=True,
            now=now,
            already_filtered=True,
            rebuild_summaries=False,
            prevalidated_summary_scopes=prevalidated_scopes,
            update_permission_status=False,
        )
        changed_scopes.update(result.changed_scopes)
    else:
        update_collection_status(
            session,
            body.device_id,
            ActivityCollectionStatusUpdate(
                platform=ActivityPlatform.ANDROID,
                capability=ActivityCapability.AGGREGATE,
                last_collected_at=now,
                last_uploaded_at=now,
                queue_depth=0,
                queue_oldest_at=None,
            ),
            now=now,
        )

    for plan in apply_plans:
        _persist_bucket_snapshot_state(
            session,
            plan=plan,
            device_id=body.device_id,
            collection_generation=body.collection_generation,
            timezone=timezone,
            now=now,
        )

    if changed_scopes:
        by_timezone: dict[str, set] = {}
        for scope in changed_scopes:
            by_timezone.setdefault(scope.timezone, set()).add(scope.day)
        for scope_timezone, days in by_timezone.items():
            rebuild_affected_days(
                session,
                days=days,
                timezone=scope_timezone,
                force_rebuild=True,
                now=now,
            )

    return AppUsageBatchOut(
        accepted=len(allowed_samples),
        created=created,
        updated=updated,
        suppressed=excluded + tombstoned,
    )


@router.post("/batch")
def ingest_batch(
    body: AppUsageBatchIn,
    request: Request,
    session: SessionDep,
) -> AppUsageBatchOut:
    """Upsert a batch of usage samples for one device."""
    with activity_write_lock():
        lock_activity_write_plane(session)
        state = get_control_payload(
            session,
            body.device_id,
            lock=True,
        )
        current_generation = state.get("collection_generation")
        if current_generation is None:
            raise APIError(
                409,
                "activity_collection_generation_unregistered",
                "Android collector must register its current collection generation "
                "through the permission status endpoint before uploading",
            )
        if body.collection_generation != int(current_generation):
            raise APIError(
                409,
                "stale_collection_generation",
                f"collector generation {body.collection_generation} does not match "
                f"server generation {current_generation}",
            )
        current_pairing_revision = int(state.get("pairing_revision", 0))
        if body.pairing_revision != current_pairing_revision:
            raise APIError(
                409,
                "stale_pairing_revision",
                f"collector pairing revision {body.pairing_revision} does not "
                f"match server pairing revision {current_pairing_revision}",
            )
        if (
            state.get("platform") != ActivityPlatform.ANDROID.value
            or state.get("capability") != ActivityCapability.AGGREGATE.value
            or state.get("permission_status")
            != ActivityPermissionStatus.GRANTED.value
        ):
            raise APIError(
                409,
                "activity_android_boundary_invalid",
                "Android collector boundary must be registered as "
                "android, aggregate, and granted before uploading",
            )
        timezone = body.timezone or str(
            resolve_timezone(request.app.state.settings)
        )
        if body.bucket_snapshots:
            try:
                with session.begin_nested():
                    response = _ingest_authoritative_snapshots(
                        body=body,
                        session=session,
                        state=state,
                        timezone=timezone,
                        now=datetime.now(UTC),
                    )
            except ActivityConflictError as exc:
                raise APIError(
                    409,
                    "activity_source_conflict",
                    str(exc),
                ) from exc
            except ActivityCollectionBlockedError as exc:
                raise APIError(
                    409,
                    "activity_collection_blocked",
                    exc.reason,
                ) from exc
            except StaleCollectionRevisionError as exc:
                raise APIError(
                    409,
                    "stale_collection_revision",
                    str(exc),
                ) from exc
            except ActivityLateDataError as exc:
                raise APIError(
                    409,
                    "activity_outside_retention",
                    str(exc),
                ) from exc
            except ActivityFutureDataError as exc:
                raise APIError(
                    409,
                    "activity_future_data",
                    str(exc),
                ) from exc
            except ActivitySourceModeConflictError as exc:
                raise APIError(
                    409,
                    "activity_source_mode_conflict",
                    str(exc),
                ) from exc
            except ActivitySummaryProvenanceError as exc:
                raise APIError(
                    409,
                    "activity_summary_requires_complete_raw",
                    str(exc),
                ) from exc
            session.commit()
            return response

        # Legacy sequence-0 clients remain row-upsert compatible, but ordered
        # snapshots must use the authoritative bucket manifest path above.
        try:
            samples = _dedupe_samples(body.samples)
        except ActivityConflictError as exc:
            raise APIError(
                409,
                "activity_source_conflict",
                str(exc),
            ) from exc
        canonical = android_batch(
            device_id=body.device_id,
            samples=samples,
            timezone=timezone,
            collection_revision=body.collection_revision,
            collection_generation=body.collection_generation,
        )
        try:
            filtered, excluded, tombstoned, _ = prepare_activity_batch(
                session,
                canonical,
            )
        except ActivityCollectionBlockedError as exc:
            raise APIError(409, "activity_collection_blocked", exc.reason) from exc
        except StaleCollectionRevisionError as exc:
            raise APIError(409, "stale_collection_revision", str(exc)) from exc
        allowed_records = {
            record.source_record_id: record for record in filtered.records
        }
        allowed_samples = [
            (
                sample,
                allowed_records[
                    android_source_record_id(
                        body.device_id,
                        sample.bucket_start,
                        sample.app_package,
                        body.collection_generation,
                    )
                ],
            )
            for sample in samples
            if android_source_record_id(
                body.device_id,
                sample.bucket_start,
                sample.app_package,
                body.collection_generation,
            )
            in allowed_records
        ]

        created = updated = 0
        try:
            with session.begin_nested():
                for sample, canonical_record in allowed_samples:
                    state_result = _insert_or_update_compatibility_sample(
                        session,
                        body=body,
                        sample=sample,
                        category=canonical_record.category,
                    )
                    created += state_result == "created"
                    updated += state_result == "updated"
                ingest_activity_batch(
                    session,
                    filtered,
                    allow_replace=True,
                    already_filtered=True,
                    excluded_count=excluded,
                    tombstoned_count=tombstoned,
                    update_permission_status=False,
                )
        except ActivityConflictError as exc:  # defensive: replace mode should own this
            raise APIError(409, "activity_source_conflict", str(exc)) from exc
        except ActivityLateDataError as exc:
            raise APIError(409, "activity_outside_retention", str(exc)) from exc
        except ActivityFutureDataError as exc:
            raise APIError(409, "activity_future_data", str(exc)) from exc
        except ActivitySourceModeConflictError as exc:
            raise APIError(409, "activity_source_mode_conflict", str(exc)) from exc
        except ActivitySummaryProvenanceError as exc:
            raise APIError(
                409,
                "activity_summary_requires_complete_raw",
                str(exc),
            ) from exc
        session.commit()
        return AppUsageBatchOut(
            accepted=len(allowed_samples),
            created=created,
            updated=updated,
            suppressed=excluded + tombstoned,
        )
