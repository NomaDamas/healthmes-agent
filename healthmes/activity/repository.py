"""Persistence for canonical activity events and collector control state."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from healthmes.activity.contracts import (
    ActivityBatchIn,
    ActivityBatchOut,
    ActivityCapability,
    ActivityCollectionStatusUpdate,
    ActivityCollectionUpdate,
    ActivityPermissionStatus,
    ActivityPlatform,
    ActivityRecord,
    ActivityState,
    AppHourRecord,
    AppIntervalRecord,
    ios_app_token_key_id,
    is_ios_app_token,
)
from healthmes.activity.locking import (
    activity_write_lock,
    lock_activity_write_plane,
)
from healthmes.activity.privacy import BLOCKED_PERMISSION_STATES, collection_gate
from healthmes.storage import ensure_default_policies
from healthmes.store import RetentionPolicy, WellnessEvent
from healthmes.timezones import (
    is_fixed_offset_timezone_name,
    parse_timezone,
)

APP_HOUR_EVENT = "activity.app-hour.v1"
APP_INTERVAL_EVENT = "activity.app-interval.v1"
HOUR_SUMMARY_EVENT = "activity.hour-summary.v1"
DAY_SUMMARY_EVENT = "activity.day-summary.v1"
# Kept as a read-only compatibility contract for pre-split control rows.
COLLECTION_CONTROL_EVENT = "activity.collection-control.v1"
COLLECTION_CONFIG_EVENT = "activity.collection-config.v1"
COLLECTION_STATUS_EVENT = "activity.collection-status.v1"
COLLECTION_CURSOR_EVENT = "activity.collection-cursor.v1"
ACTIVITYWATCH_IMPORT_FENCE_EVENT = "activity.activitywatch-import-fence.v1"
IOS_SNAPSHOT_FENCE_EVENT = "activity.ios-snapshot-fence.v1"
DELETION_TOMBSTONE_EVENT = "activity.deletion-tombstone.v1"
DELETION_IDENTITY_CHUNK_SIZE = 500

ACTIVITY_RAW_CLASS = "activity_raw"
ACTIVITY_HOURLY_CLASS = "activity_hourly"
ACTIVITY_DAILY_CLASS = "activity_daily"
ACTIVITY_RETENTION_DEFAULTS: dict[str, int | None] = {
    ACTIVITY_RAW_CLASS: 14,
    ACTIVITY_HOURLY_CLASS: 90,
    ACTIVITY_DAILY_CLASS: None,
}

CONTROL_PROVIDER = "healthmes-activity-control"
DELETION_PROVIDER = "healthmes-activity-deletion"
IOS_PROVIDER = "ios-device-activity"
SUMMARY_PROVIDER = "healthmes-activity-aggregator"
RAW_EVENT_TYPES = (APP_HOUR_EVENT, APP_INTERVAL_EVENT)
SUMMARY_EVENT_TYPES = (HOUR_SUMMARY_EVENT, DAY_SUMMARY_EVENT)
CONTROL_EVENT_TYPES = (
    COLLECTION_CONTROL_EVENT,
    COLLECTION_CONFIG_EVENT,
    COLLECTION_STATUS_EVENT,
    COLLECTION_CURSOR_EVENT,
)
# The import fence is deliberately not a public collection-control event.
# User control-state deletion must preserve it so an old prepared snapshot can
# never become current again after the visible control rows are reset.
CONTROL_LOCK_PREFIX = "healthmes:activity:control:"

class ActivityConflictError(ValueError):
    """A source identity was reused for different immutable input."""


class InvalidIOSAppTokenError(ValueError):
    """An iOS exclusion was not keyed by the device pseudonym secret."""


def ios_exclusion_namespace(
    excluded_apps: Sequence[object],
) -> tuple[bool, str | None]:
    """Return whether exclusions form one valid iOS pseudonym namespace."""

    if any(
        not isinstance(value, str) or not is_ios_app_token(value)
        for value in excluded_apps
    ):
        return False, None
    key_ids = {
        key_id
        for value in excluded_apps
        for key_id in (ios_app_token_key_id(value),)
        if key_id is not None
    }
    if len(key_ids) > 1:
        return False, None
    return True, next(iter(key_ids), None)


class ActivityWriteConflictError(RuntimeError):
    """A concurrent writer won creation of the same canonical identity."""


@dataclass(frozen=True, order=True, slots=True)
class ActivityLocalScope:
    day: date
    timezone: str


@dataclass(frozen=True, slots=True)
class ActivityChangeWindow:
    key: str
    start: datetime
    end: datetime
    timezone: str


@dataclass(frozen=True, slots=True)
class PersistResult:
    event: WellnessEvent
    state: str
    previous_scopes: tuple[ActivityLocalScope, ...] = ()


@dataclass(frozen=True, slots=True)
class ActivityTombstoneFilterResult:
    records: tuple[ActivityRecord, ...]
    affected_source_record_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class IOSSnapshotFence:
    collection_generation: int | None
    sequence: int
    manifest_sha256: str
    snapshot_start: datetime
    snapshot_end: datetime
    accepted_response: ActivityBatchOut | None


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def iso_or_none(value: datetime | None) -> str | None:
    return as_utc(value).isoformat() if value is not None else None


def parse_optional_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return as_utc(value)
    if isinstance(value, str):
        try:
            return as_utc(datetime.fromisoformat(value))
        except ValueError:
            return None
    return None


def record_bounds(record: ActivityRecord) -> tuple[datetime, datetime]:
    if isinstance(record, AppHourRecord):
        start = as_utc(record.bucket_start)
        return start, start + timedelta(hours=1)
    return as_utc(record.start_at), as_utc(record.end_at)


def record_scopes(
    record: ActivityRecord,
    timezone: str,
) -> tuple[ActivityLocalScope, ...]:
    zone = parse_timezone(timezone)
    start, end = record_bounds(record)
    first = start.astimezone(zone).date()
    last = (end - timedelta(microseconds=1)).astimezone(zone).date()
    return tuple(
        ActivityLocalScope(
            day=first + timedelta(days=offset),
            timezone=timezone,
        )
        for offset in range((last - first).days + 1)
    )


def range_scopes(
    *,
    start: datetime,
    end: datetime,
    timezone: str,
) -> set[ActivityLocalScope]:
    zone = parse_timezone(timezone)
    first = as_utc(start).astimezone(zone).date()
    last = (
        as_utc(end) - timedelta(microseconds=1)
    ).astimezone(zone).date()
    return {
        ActivityLocalScope(
            day=first + timedelta(days=offset),
            timezone=timezone,
        )
        for offset in range((last - first).days + 1)
    }


def event_bounds(event: WellnessEvent) -> tuple[datetime, datetime]:
    start = as_utc(event.observed_at)
    if event.event_type == APP_HOUR_EVENT:
        return start, start + timedelta(hours=1)
    if event.event_type == APP_INTERVAL_EVENT:
        end = parse_optional_datetime(event.payload.get("end_at"))
        if end is not None and end > start:
            return start, end
    if event.event_type == HOUR_SUMMARY_EVENT:
        end = parse_optional_datetime(event.payload.get("window", {}).get("end"))
        if end is not None and end > start:
            return start, end
        return start, start + timedelta(hours=1)
    if event.event_type == DAY_SUMMARY_EVENT:
        timezone = event.timezone or str(event.payload.get("timezone") or "UTC")
        raw_day = event.payload.get("date")
        if isinstance(raw_day, str):
            try:
                zone = parse_timezone(timezone)
                day = date.fromisoformat(raw_day)
            except ValueError:
                pass
            else:
                day_start = datetime.combine(day, datetime.min.time(), tzinfo=zone)
                day_end = datetime.combine(
                    day + timedelta(days=1),
                    datetime.min.time(),
                    tzinfo=zone,
                )
                return day_start.astimezone(UTC), day_end.astimezone(UTC)
    return start, start + timedelta(microseconds=1)


def event_scopes(event: WellnessEvent) -> tuple[ActivityLocalScope, ...]:
    timezone = event.timezone or str(event.payload.get("timezone") or "UTC")
    try:
        zone = parse_timezone(timezone)
    except ValueError:
        timezone = "UTC"
        zone = parse_timezone("UTC")
    start, end = event_bounds(event)
    first = start.astimezone(zone).date()
    last = (end - timedelta(microseconds=1)).astimezone(zone).date()
    return tuple(
        ActivityLocalScope(
            day=first + timedelta(days=offset),
            timezone=timezone,
        )
        for offset in range((last - first).days + 1)
    )


def event_is_expired(
    event: WellnessEvent,
    *,
    now: datetime | None = None,
) -> bool:
    expires_at = parse_optional_datetime(event.expires_at)
    return expires_at is not None and expires_at <= as_utc(now or datetime.now(UTC))


def fixed_offset_summary_scopes_by_change(
    session: Session,
    changes: Iterable[ActivityChangeWindow],
    *,
    now: datetime | None = None,
) -> dict[str, set[ActivityLocalScope]]:
    """Map raw changes to materialized fixed-offset summaries they can affect."""
    current = as_utc(now or datetime.now(UTC))
    normalized: list[tuple[ActivityChangeWindow, datetime, datetime, Any]] = []
    scopes_by_key: dict[str, set[ActivityLocalScope]] = {}
    for change in changes:
        scopes_by_key.setdefault(change.key, set())
        start = as_utc(change.start)
        end = as_utc(change.end)
        if end <= start:
            continue
        try:
            source_zone = parse_timezone(change.timezone)
        except ValueError:
            continue
        normalized.append((change, start, end, source_zone))
    if not normalized:
        return scopes_by_key

    summaries = [
        row
        for row in session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type.in_(SUMMARY_EVENT_TYPES),
                WellnessEvent.source_provider == SUMMARY_PROVIDER,
            )
        )
        if (
            isinstance(row.timezone, str)
            and is_fixed_offset_timezone_name(row.timezone)
            and not event_is_expired(row, now=current)
        )
    ]
    for summary in summaries:
        assert summary.timezone is not None
        target_zone = parse_timezone(summary.timezone)
        summary_start, summary_end = event_bounds(summary)
        summary_scopes = event_scopes(summary)
        for change, start, end, source_zone in normalized:
            if end <= summary_start or start >= summary_end:
                continue
            if (
                start.astimezone(source_zone).utcoffset()
                != start.astimezone(target_zone).utcoffset()
            ):
                continue
            scopes_by_key[change.key].update(summary_scopes)
    return scopes_by_key


def ensure_activity_policies(session: Session) -> dict[str, RetentionPolicy]:
    policies = {row.data_class: row for row in ensure_default_policies(session)}
    return {data_class: policies[data_class] for data_class in ACTIVITY_RETENTION_DEFAULTS}


def event_expiry(
    policy: RetentionPolicy | None,
    observed_at: datetime,
) -> datetime | None:
    if policy is None or not policy.enabled or policy.retention_days is None:
        return None
    return as_utc(observed_at) + timedelta(days=policy.retention_days)


def legacy_app_usage_cutoff(
    session: Session,
    *,
    now: datetime | None = None,
) -> datetime | None:
    policy = session.scalar(
        select(RetentionPolicy).where(
            RetentionPolicy.data_class == ACTIVITY_RAW_CLASS
        )
    )
    if policy is None:
        retention_days = ACTIVITY_RETENTION_DEFAULTS[ACTIVITY_RAW_CLASS]
    elif not policy.enabled or policy.retention_days is None:
        return None
    else:
        retention_days = policy.retention_days
    return as_utc(now or datetime.now(UTC)) - timedelta(days=retention_days)


def activity_source_identity_digest(
    *,
    source_provider: str,
    source_device: str,
    source_record_id: str,
) -> str:
    return hashlib.sha256(
        (
            f"{source_provider}\0{source_device}\0"
            f"{source_record_id}"
        ).encode()
    ).hexdigest()


ACTIVITY_FRAGMENT_PREFIX = "healthmes-activity-fragment"
ACTIVITY_MUTABLE_FRAGMENT_VERSION = "v2m"


def _datetime_epoch_microseconds(value: datetime) -> int:
    normalized = as_utc(value)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = normalized - epoch
    return (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


def _fragment_identity_parts(source_record_id: str) -> list[str] | None:
    parts = source_record_id.split(":")
    if (
        len(parts) == 3
        and parts[0] == ACTIVITY_FRAGMENT_PREFIX
        and len(parts[1]) == 64
        and all(character in "0123456789abcdef" for character in parts[1])
    ):
        return parts
    if (
        len(parts) == 5
        and parts[0] == ACTIVITY_FRAGMENT_PREFIX
        and parts[1] == ACTIVITY_MUTABLE_FRAGMENT_VERSION
        and len(parts[2]) == 64
        and all(character in "0123456789abcdef" for character in parts[2])
        and parts[3].lstrip("-").isdigit()
        and parts[4].lstrip("-").isdigit()
    ):
        return parts
    return None


def activity_fragment_root_identity_digest(
    *,
    source_provider: str,
    source_device: str,
    source_record_id: str,
) -> str:
    """Return the original source identity across repeated fragmentation."""
    parts = _fragment_identity_parts(source_record_id)
    if parts is not None:
        return parts[2] if len(parts) == 5 else parts[1]
    return activity_source_identity_digest(
        source_provider=source_provider,
        source_device=source_device,
        source_record_id=source_record_id,
    )


def activity_fragment_root_start_microseconds(
    source_record_id: str,
) -> int | None:
    """Return the immutable root start carried by mutable-end fragments."""
    parts = _fragment_identity_parts(source_record_id)
    if parts is None or len(parts) != 5:
        return None
    return int(parts[3])


def activity_fragment_source_record_id(
    *,
    source_provider: str,
    source_device: str,
    source_record_id: str,
    start: datetime,
    end: datetime,
    root_start: datetime | None = None,
    mutable_end_identity: bool = False,
) -> str:
    """Build one stable identity for deletion and reconciliation fragments."""
    root_digest = activity_fragment_root_identity_digest(
        source_provider=source_provider,
        source_device=source_device,
        source_record_id=source_record_id,
    )
    if mutable_end_identity:
        carried_root_start = activity_fragment_root_start_microseconds(
            source_record_id
        )
        root_start_us = (
            carried_root_start
            if carried_root_start is not None
            else _datetime_epoch_microseconds(root_start or start)
        )
        fragment_start_us = _datetime_epoch_microseconds(start)
        return (
            f"{ACTIVITY_FRAGMENT_PREFIX}:"
            f"{ACTIVITY_MUTABLE_FRAGMENT_VERSION}:"
            f"{root_digest}:{root_start_us}:{fragment_start_us}"
        )
    bounds_digest = hashlib.sha256(
        (
            f"{root_digest}|{as_utc(start).isoformat()}|"
            f"{as_utc(end).isoformat()}"
        ).encode()
    ).hexdigest()[:40]
    return f"{ACTIVITY_FRAGMENT_PREFIX}:{root_digest}:{bounds_digest}"


def _fingerprint(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def _record_payload(
    record: ActivityRecord,
    *,
    platform: ActivityPlatform,
    capability: ActivityCapability,
) -> dict[str, Any]:
    common = {
        "platform": platform.value,
        "capability": capability.value,
    }
    if isinstance(record, AppHourRecord):
        payload = {
            **common,
            "kind": record.kind,
            "bucket_start": record.bucket_start.isoformat(),
            "app_id": record.app_id,
            "foreground_seconds": record.foreground_seconds,
            "launches": record.launches,
            "category": record.category,
            "coverage_seconds": record.coverage_seconds,
            "bucket_complete": record.bucket_complete,
        }
        if record.coverage_only:
            payload["coverage_only"] = True
        if record.coverage_status is not None:
            payload["coverage_status"] = record.coverage_status.value
        for key, value in (
            (
                "observed_activity_seconds",
                record.observed_activity_seconds,
            ),
            (
                "represented_app_seconds",
                record.represented_app_seconds,
            ),
            (
                "privacy_filtered_seconds",
                record.privacy_filtered_seconds,
            ),
            (
                "website_activity_seconds",
                record.website_activity_seconds,
            ),
            (
                "unknown_activity_seconds",
                record.unknown_activity_seconds,
            ),
        ):
            if value is not None:
                payload[key] = value
        if not record.launches_observed:
            payload["launches_observed"] = False
        if record.snapshot_sequence is not None:
            payload["snapshot_sequence"] = record.snapshot_sequence
        return payload
    payload = {
        **common,
        "kind": record.kind,
        "start_at": record.start_at.isoformat(),
        "end_at": record.end_at.isoformat(),
        "state": record.state.value,
        "app_id": record.app_id,
        "launches": record.launches,
        "category": record.category,
    }
    if record.source_group_id is not None:
        payload["source_group_id"] = record.source_group_id
    return payload


def _observed_at(record: ActivityRecord) -> datetime:
    if isinstance(record, AppHourRecord):
        return record.bucket_start
    return record.start_at


def _provisional_hour_replacement_allowed(
    existing: WellnessEvent,
    *,
    batch: ActivityBatchIn,
    record: ActivityRecord,
) -> bool:
    if existing.event_type != APP_HOUR_EVENT or not isinstance(record, AppHourRecord):
        return False
    previous = existing.payload if isinstance(existing.payload, dict) else {}
    if previous.get("bucket_complete") is not False:
        return False
    if (
        existing.source_device != batch.source_device
        or existing.timezone != batch.timezone
        or previous.get("bucket_start") != record.bucket_start.isoformat()
        or previous.get("app_id") != record.app_id
    ):
        return False
    previous_revision = (existing.quality_flags or {}).get("collection_revision")
    if (
        previous_revision is not None
        and batch.collection_revision is not None
        and batch.collection_revision < previous_revision
    ):
        return False
    previous_sequence = int(previous.get("snapshot_sequence") or 0)
    incoming_sequence = int(record.snapshot_sequence or 0)
    if previous_sequence > 0 or incoming_sequence > 0:
        return incoming_sequence > previous_sequence
    if record.foreground_seconds < int(
        previous.get("foreground_seconds") or 0
    ):
        return False
    if (
        record.launches_observed
        and previous.get("launches_observed", True) is not False
        and record.launches < int(previous.get("launches") or 0)
    ):
        return False
    return as_utc(batch.collected_at) > as_utc(existing.recorded_at)


def _hour_snapshot_content(payload: dict[str, Any]) -> tuple[Any, ...]:
    return (
        payload.get("kind"),
        payload.get("bucket_start"),
        payload.get("app_id"),
        payload.get("foreground_seconds"),
        payload.get("launches"),
        payload.get("launches_observed", True),
        payload.get("category"),
        payload.get("coverage_seconds"),
        payload.get("coverage_only", False),
        payload.get("coverage_status"),
        payload.get("observed_activity_seconds"),
        payload.get("represented_app_seconds"),
        payload.get("privacy_filtered_seconds"),
        payload.get("website_activity_seconds"),
        payload.get("unknown_activity_seconds"),
        payload.get("bucket_complete"),
    )


def _update_existing_activity_event(
    session: Session,
    existing: WellnessEvent,
    *,
    batch: ActivityBatchIn,
    record: ActivityRecord,
    payload: dict[str, Any],
    fingerprint: str,
    policy: RetentionPolicy,
    allow_replace: bool,
) -> PersistResult:
    previous = (
        existing.derived_from.get("_ingest_fingerprint")
        if isinstance(existing.derived_from, dict)
        else None
    )
    expected_event_type = (
        APP_HOUR_EVENT if isinstance(record, AppHourRecord) else APP_INTERVAL_EVENT
    )
    same_hour_content = (
        isinstance(record, AppHourRecord)
        and existing.event_type == APP_HOUR_EVENT
        and isinstance(existing.payload, dict)
        and _hour_snapshot_content(existing.payload)
        == _hour_snapshot_content(payload)
    )
    if previous == fingerprint or (
        previous is None
        and existing.event_type == expected_event_type
        and existing.source_device == batch.source_device
        and existing.timezone == batch.timezone
        and existing.payload == payload
    ):
        return PersistResult(existing, "duplicate")
    if same_hour_content and (
        existing.payload.get("bucket_complete") is True
        or int(record.snapshot_sequence or 0)
        <= int(existing.payload.get("snapshot_sequence") or 0)
    ):
        return PersistResult(existing, "duplicate")
    if (
        isinstance(record, AppHourRecord)
        and batch.platform != ActivityPlatform.IOS
    ):
        replacement_allowed = _provisional_hour_replacement_allowed(
            existing,
            batch=batch,
            record=record,
        )
    elif batch.platform == ActivityPlatform.IOS:
        previous_sequence = int(existing.payload.get("snapshot_sequence") or 0)
        incoming_sequence = int(record.snapshot_sequence or 0)
        replacement_allowed = allow_replace and (
            incoming_sequence > previous_sequence
            if incoming_sequence > 0
            else (
                previous_sequence == 0
                and as_utc(batch.collected_at)
                > as_utc(existing.recorded_at)
            )
        )
    else:
        replacement_allowed = allow_replace
    if not replacement_allowed:
        raise ActivityConflictError(
            "source_record_id was already used with different activity input"
        )
    previous_scopes = event_scopes(existing)
    observed = _observed_at(record)
    existing.event_type = expected_event_type
    existing.observed_at = observed
    existing.recorded_at = batch.collected_at
    existing.timezone = batch.timezone
    existing.source_device = batch.source_device
    existing.capture_method = "sensor"
    existing.quality_flags = {
        "platform": batch.platform.value,
        "capability": batch.capability.value,
        "collection_revision": batch.collection_revision,
    }
    existing.coverage = (
        record.coverage_seconds / 3600
        if isinstance(record, AppHourRecord) and record.coverage_seconds is not None
        else None
    )
    existing.retention_policy_id = policy.id
    existing.expires_at = event_expiry(policy, observed)
    existing.payload = payload
    existing.derived_from = {"_ingest_fingerprint": fingerprint}
    session.flush([existing])
    return PersistResult(
        existing,
        "updated",
        previous_scopes=previous_scopes,
    )


def persist_activity_record(
    session: Session,
    batch: ActivityBatchIn,
    record: ActivityRecord,
    *,
    allow_replace: bool = False,
    raw_policy: RetentionPolicy | None = None,
) -> PersistResult:
    policy = raw_policy or ensure_activity_policies(session)[ACTIVITY_RAW_CLASS]
    payload = _record_payload(
        record,
        platform=batch.platform,
        capability=batch.capability,
    )
    fingerprint = _fingerprint(
        {
            "event_type": (
                APP_HOUR_EVENT if isinstance(record, AppHourRecord) else APP_INTERVAL_EVENT
            ),
            "timezone": batch.timezone,
            "source_device": batch.source_device,
            "payload": payload,
        }
    )
    existing = session.scalar(
        select(WellnessEvent)
        .where(
            WellnessEvent.source_provider == batch.source_provider,
            WellnessEvent.source_record_id == record.source_record_id,
        )
        .with_for_update()
    )
    if existing is not None:
        return _update_existing_activity_event(
            session,
            existing,
            batch=batch,
            record=record,
            payload=payload,
            fingerprint=fingerprint,
            policy=policy,
            allow_replace=allow_replace,
        )

    observed = _observed_at(record)
    event = WellnessEvent(
        event_type=(APP_HOUR_EVENT if isinstance(record, AppHourRecord) else APP_INTERVAL_EVENT),
        schema_version=1,
        observed_at=observed,
        recorded_at=batch.collected_at,
        timezone=batch.timezone,
        source_provider=batch.source_provider,
        source_device=batch.source_device,
        source_record_id=record.source_record_id,
        capture_method="sensor",
        quality_flags={
            "platform": batch.platform.value,
            "capability": batch.capability.value,
            "collection_revision": batch.collection_revision,
        },
        confidence=None,
        coverage=(
            record.coverage_seconds / 3600
            if isinstance(record, AppHourRecord) and record.coverage_seconds is not None
            else None
        ),
        sensitivity="activity",
        consent_scope="personal",
        retention_policy_id=policy.id,
        expires_at=event_expiry(policy, observed),
        payload=payload,
        derived_from={"_ingest_fingerprint": fingerprint},
    )
    try:
        with session.begin_nested():
            session.add(event)
            session.flush([event])
    except IntegrityError:
        concurrent = session.scalar(
            select(WellnessEvent)
            .where(
                WellnessEvent.source_provider == batch.source_provider,
                WellnessEvent.source_record_id == record.source_record_id,
            )
            .with_for_update()
        )
        if concurrent is None:
            raise ActivityWriteConflictError(
                "activity source identity raced but could not be reloaded"
            )
        return _update_existing_activity_event(
            session,
            concurrent,
            batch=batch,
            record=record,
            payload=payload,
            fingerprint=fingerprint,
            policy=policy,
            allow_replace=allow_replace,
        )
    return PersistResult(event, "created")


CONFIG_KEYS = {
    "device_id",
    "platform",
    "enabled",
    "excluded_apps",
    "paused_until",
    "config_revision",
    "ios_pseudonym_key_id",
}
STATUS_KEYS = {
    "device_id",
    "platform",
    "permission_status",
    "capability",
    "status_reason",
    "status_observed_at",
    "collection_generation",
    "pairing_revision",
    "last_collected_at",
    "last_uploaded_at",
    "queue_oldest_at",
    "queue_depth",
    "coverage",
}


def _collection_generation(value: object) -> int | None:
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    ):
        return value
    return None


def _pairing_revision(value: object) -> int:
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    ):
        return value
    return 0


def _status_boundary_is_stale(
    *,
    payload: dict[str, Any],
    update: ActivityCollectionStatusUpdate,
    incoming_observed_at: datetime,
) -> bool:
    existing_observed_at = parse_optional_datetime(
        payload.get("status_observed_at")
    )
    existing_permission = payload.get("permission_status")
    incoming_permission = (
        update.permission_status.value
        if update.permission_status is not None
        else existing_permission
    )
    existing_generation = _collection_generation(
        payload.get("collection_generation")
    )
    incoming_generation = update.collection_generation
    existing_pairing_revision = _pairing_revision(
        payload.get("pairing_revision")
    )
    incoming_pairing_revision = (
        update.pairing_revision
        if update.pairing_revision is not None
        else (
            0
            if update.platform is ActivityPlatform.ANDROID
            and incoming_generation is not None
            else existing_pairing_revision
        )
    )

    # Pairing revisions are monotonic on Android. Once the same HealthMes
    # instance has observed a newer pairing, an older worker cannot reopen it
    # even if its wall clock or permission observation arrives later.
    if incoming_pairing_revision < existing_pairing_revision:
        return True

    if existing_generation is not None:
        # Once a collector establishes a monotonic generation, an unversioned
        # boundary or any lower generation must never reopen it.
        if incoming_generation is None or incoming_generation < existing_generation:
            return True
        if incoming_generation > existing_generation:
            return False

        # A permission transition must advance the Android generation. Within
        # one generation, blocked evidence wins even if a delayed grant carries
        # a later wall-clock timestamp.
        if (
            existing_permission in BLOCKED_PERMISSION_STATES
            and incoming_permission not in BLOCKED_PERMISSION_STATES
        ):
            return True
        if (
            incoming_permission in BLOCKED_PERMISSION_STATES
            and existing_permission not in BLOCKED_PERMISSION_STATES
        ):
            return False
    elif incoming_generation is not None:
        # The first versioned observation establishes the monotonic boundary.
        return False

    if existing_observed_at is None:
        return False
    if incoming_observed_at < existing_observed_at:
        return True
    return (
        incoming_observed_at == existing_observed_at
        and existing_permission in BLOCKED_PERMISSION_STATES
        and incoming_permission not in BLOCKED_PERMISSION_STATES
    )


def _control_source_id(device_id: str, kind: str = "legacy") -> str:
    digest = hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:32]
    return f"{kind}:device:{digest}"


def _cursor_source_id(device_id: str, cursor_key: str) -> str:
    digest = hashlib.sha256(
        f"{device_id}\0{cursor_key}".encode()
    ).hexdigest()
    return f"cursor:{digest}"


def _default_collection_enabled(
    device_id: str,
    platform: ActivityPlatform,
) -> bool:
    # Versioned iPhone collector identities are introduced by the Keychain
    # migration in PR #138. They must be registered through the input control
    # plane before upload; arbitrary legacy IDs retain compatibility.
    return not (
        platform is ActivityPlatform.IOS
        and device_id.startswith("ios-collector-v1-")
    )


def default_control_payload(
    device_id: str,
    *,
    platform: ActivityPlatform = ActivityPlatform.UNKNOWN,
) -> dict[str, Any]:
    return {
        "device_id": device_id,
        "platform": platform.value,
        "enabled": _default_collection_enabled(device_id, platform),
        "excluded_apps": [],
        "paused_until": None,
        "permission_status": ActivityPermissionStatus.UNKNOWN.value,
        "capability": ActivityCapability.UNAVAILABLE.value,
        "status_reason": None,
        "status_observed_at": None,
        "collection_generation": None,
        "pairing_revision": 0,
        "last_collected_at": None,
        "last_uploaded_at": None,
        "queue_oldest_at": None,
        "queue_depth": 0,
        "coverage": None,
        "config_revision": 0,
        "cursors": {},
    }


def lock_activity_control_device(session: Session, device_id: str) -> None:
    """Serialize one device's control boundary across PostgreSQL processes."""
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended(:control_key, 0)"
            ")"
        ),
        {"control_key": f"{CONTROL_LOCK_PREFIX}{device_id}"},
    )


def get_control_event(session: Session, device_id: str) -> WellnessEvent | None:
    current = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == COLLECTION_CONFIG_EVENT,
            WellnessEvent.source_provider == CONTROL_PROVIDER,
            WellnessEvent.source_record_id == _control_source_id(device_id, "config"),
        )
    )
    if current is not None:
        return current
    return session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == COLLECTION_CONTROL_EVENT,
            WellnessEvent.source_provider == CONTROL_PROVIDER,
            WellnessEvent.source_record_id.in_(
                (
                    _control_source_id(device_id),
                    # Compatibility with the initial composite implementation.
                    f"device:{hashlib.sha256(device_id.encode('utf-8')).hexdigest()[:32]}",
                )
            ),
        )
    )


def _typed_control_event(
    session: Session,
    device_id: str,
    *,
    event_type: str,
    kind: str,
    lock: bool = False,
) -> WellnessEvent | None:
    statement = select(WellnessEvent).where(
        WellnessEvent.event_type == event_type,
        WellnessEvent.source_provider == CONTROL_PROVIDER,
        WellnessEvent.source_record_id == _control_source_id(device_id, kind),
    )
    if lock:
        statement = statement.with_for_update().execution_options(
            populate_existing=True
        )
    return session.scalar(statement)


def _legacy_control_payload(
    session: Session,
    device_id: str,
    *,
    lock: bool = False,
) -> dict[str, Any]:
    statement = select(WellnessEvent).where(
        WellnessEvent.event_type == COLLECTION_CONTROL_EVENT,
        WellnessEvent.source_provider == CONTROL_PROVIDER,
        WellnessEvent.source_record_id.in_(
            (
                _control_source_id(device_id),
                f"device:{hashlib.sha256(device_id.encode('utf-8')).hexdigest()[:32]}",
            )
        ),
    )
    if lock:
        statement = statement.with_for_update().execution_options(
            populate_existing=True
        )
    event = session.scalar(statement)
    return dict(event.payload) if event is not None and isinstance(event.payload, dict) else {}


def _fresh_control_payload(
    session: Session,
    device_id: str,
    *,
    platform: ActivityPlatform,
) -> dict[str, Any]:
    def one_payload(statement) -> dict[str, Any]:
        value = session.scalar(
            statement.with_only_columns(
                WellnessEvent.payload,
                maintain_column_froms=True,
            )
        )
        return dict(value) if isinstance(value, dict) else {}

    payload = {
        **default_control_payload(device_id, platform=platform),
        **one_payload(
            select(WellnessEvent).where(
                WellnessEvent.event_type == COLLECTION_CONTROL_EVENT,
                WellnessEvent.source_provider == CONTROL_PROVIDER,
                WellnessEvent.source_record_id.in_(
                    (
                        _control_source_id(device_id),
                        "device:"
                        + hashlib.sha256(
                            device_id.encode("utf-8")
                        ).hexdigest()[:32],
                    )
                ),
            )
        ),
    }
    payload.update(
        one_payload(
            select(WellnessEvent).where(
                WellnessEvent.event_type == COLLECTION_STATUS_EVENT,
                WellnessEvent.source_provider == CONTROL_PROVIDER,
                WellnessEvent.source_record_id
                == _control_source_id(device_id, "status"),
            )
        )
    )
    payload.update(
        one_payload(
            select(WellnessEvent).where(
                WellnessEvent.event_type == COLLECTION_CONFIG_EVENT,
                WellnessEvent.source_provider == CONTROL_PROVIDER,
                WellnessEvent.source_record_id
                == _control_source_id(device_id, "config"),
            )
        )
    )
    cursor_payloads = session.scalars(
        select(WellnessEvent.payload).where(
            WellnessEvent.event_type == COLLECTION_CURSOR_EVENT,
            WellnessEvent.source_provider == CONTROL_PROVIDER,
            WellnessEvent.source_device == device_id,
        )
    )
    cursors = dict(payload.get("cursors") or {})
    for value in cursor_payloads:
        if not isinstance(value, dict):
            continue
        cursor_key = value.get("cursor_key")
        cursor_value = value.get("cursor_value")
        if isinstance(cursor_key, str) and isinstance(
            cursor_value,
            str,
        ):
            cursors[cursor_key] = cursor_value
    payload["cursors"] = cursors
    payload["device_id"] = device_id
    return payload


def get_control_payload(
    session: Session,
    device_id: str,
    *,
    platform: ActivityPlatform = ActivityPlatform.UNKNOWN,
    lock: bool = False,
    refresh: bool = False,
) -> dict[str, Any]:
    if refresh and not lock:
        return _fresh_control_payload(
            session,
            device_id,
            platform=platform,
        )
    if lock:
        lock_activity_control_device(session, device_id)
    payload = {
        **default_control_payload(device_id, platform=platform),
        **_legacy_control_payload(session, device_id, lock=lock),
    }
    status_event = _typed_control_event(
        session,
        device_id,
        event_type=COLLECTION_STATUS_EVENT,
        kind="status",
        lock=lock,
    )
    if status_event is not None and isinstance(status_event.payload, dict):
        payload.update(status_event.payload)
    config_event = _typed_control_event(
        session,
        device_id,
        event_type=COLLECTION_CONFIG_EVENT,
        kind="config",
        lock=lock,
    )
    if config_event is not None and isinstance(config_event.payload, dict):
        payload.update(config_event.payload)
    cursor_statement = select(WellnessEvent).where(
        WellnessEvent.event_type == COLLECTION_CURSOR_EVENT,
        WellnessEvent.source_provider == CONTROL_PROVIDER,
        WellnessEvent.source_device == device_id,
    )
    if lock:
        cursor_statement = cursor_statement.with_for_update().execution_options(
            populate_existing=True
        )
    cursor_rows = session.scalars(cursor_statement)
    cursors = dict(payload.get("cursors") or {})
    for row in cursor_rows:
        cursor_key = row.payload.get("cursor_key")
        cursor_value = row.payload.get("cursor_value")
        if isinstance(cursor_key, str) and isinstance(cursor_value, str):
            cursors[cursor_key] = cursor_value
    payload["cursors"] = cursors
    payload["device_id"] = device_id
    return payload


def _persist_control_payload(
    session: Session,
    device_id: str,
    payload: dict[str, Any],
    *,
    event_type: str,
    source_record_id: str,
    now: datetime | None = None,
) -> WellnessEvent:
    lock_activity_control_device(session, device_id)
    current = as_utc(now or datetime.now(UTC))
    event = session.scalar(
        select(WellnessEvent)
        .where(
            WellnessEvent.event_type == event_type,
            WellnessEvent.source_provider == CONTROL_PROVIDER,
            WellnessEvent.source_record_id == source_record_id,
        )
        .with_for_update()
    )
    if event is None:
        event = WellnessEvent(
            event_type=event_type,
            schema_version=1,
            observed_at=current,
            recorded_at=current,
            timezone=None,
            source_provider=CONTROL_PROVIDER,
            source_device=device_id,
            source_record_id=source_record_id,
            capture_method="configuration",
            quality_flags=None,
            confidence=None,
            coverage=None,
            sensitivity="activity-control",
            consent_scope="personal",
            retention_policy_id=None,
            expires_at=None,
            payload=payload,
            derived_from=None,
        )
        try:
            with session.begin_nested():
                session.add(event)
                session.flush([event])
        except IntegrityError as exc:
            raise ActivityWriteConflictError(
                "concurrent activity control creation"
            ) from exc
    else:
        event.observed_at = current
        event.recorded_at = current
        event.source_device = device_id
        event.payload = payload
        session.flush([event])
    return event


def get_activitywatch_import_fence(
    session: Session,
    device_id: str,
    *,
    lock: bool = False,
) -> int | None:
    """Return the private latest-started ActivityWatch import sequence."""
    if lock:
        lock_activity_control_device(session, device_id)
    event = _typed_control_event(
        session,
        device_id,
        event_type=ACTIVITYWATCH_IMPORT_FENCE_EVENT,
        kind="activitywatch-import-fence",
        lock=lock,
    )
    if event is None:
        return None
    value = event.payload.get("sequence")
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
    ):
        raise ActivityWriteConflictError(
            "ActivityWatch import fence is malformed"
        )
    return value


def get_ios_snapshot_fence(
    session: Session,
    device_id: str,
    *,
    lock: bool = False,
) -> IOSSnapshotFence | None:
    """Return the private ordering fence for iOS authoritative snapshots."""
    if lock:
        lock_activity_control_device(session, device_id)
    event = _typed_control_event(
        session,
        device_id,
        event_type=IOS_SNAPSHOT_FENCE_EVENT,
        kind="ios-snapshot-fence",
        lock=lock,
    )
    if event is None or not isinstance(event.payload, dict):
        return None
    sequence = event.payload.get("sequence")
    collection_generation = _collection_generation(
        event.payload.get("collection_generation")
    )
    digest = event.payload.get("manifest_sha256")
    start = parse_optional_datetime(event.payload.get("snapshot_start"))
    end = parse_optional_datetime(event.payload.get("snapshot_end"))
    response_payload = event.payload.get("accepted_response")
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 1
        or not isinstance(digest, str)
        or len(digest) != 64
        or start is None
        or end is None
        or start >= end
    ):
        raise ActivityWriteConflictError("iOS snapshot fence is malformed")
    accepted_response: ActivityBatchOut | None = None
    if response_payload is not None:
        try:
            accepted_response = ActivityBatchOut.model_validate(
                response_payload
            )
        except ValueError as exc:
            raise ActivityWriteConflictError(
                "iOS snapshot fence response is malformed"
            ) from exc
    return IOSSnapshotFence(
        collection_generation=collection_generation,
        sequence=sequence,
        manifest_sha256=digest,
        snapshot_start=start,
        snapshot_end=end,
        accepted_response=accepted_response,
    )


def persist_ios_snapshot_fence(
    session: Session,
    device_id: str,
    *,
    collection_generation: int | None,
    sequence: int,
    manifest_sha256: str,
    snapshot_start: datetime,
    snapshot_end: datetime,
    accepted_response: ActivityBatchOut,
    now: datetime | None = None,
) -> IOSSnapshotFence:
    if sequence < 1 or len(manifest_sha256) != 64:
        raise ValueError("invalid iOS snapshot fence")
    if collection_generation is not None and collection_generation < 0:
        raise ValueError("invalid iOS snapshot collection generation")
    start = as_utc(snapshot_start)
    end = as_utc(snapshot_end)
    if start >= end:
        raise ValueError("invalid iOS snapshot range")
    _persist_control_payload(
        session,
        device_id,
        {
            "device_id": device_id,
            "collection_generation": collection_generation,
            "sequence": sequence,
            "manifest_sha256": manifest_sha256,
            "snapshot_start": start.isoformat(),
            "snapshot_end": end.isoformat(),
            "accepted_response": accepted_response.model_dump(mode="json"),
        },
        event_type=IOS_SNAPSHOT_FENCE_EVENT,
        source_record_id=_control_source_id(
            device_id,
            "ios-snapshot-fence",
        ),
        now=now,
    )
    return IOSSnapshotFence(
        collection_generation=collection_generation,
        sequence=sequence,
        manifest_sha256=manifest_sha256,
        snapshot_start=start,
        snapshot_end=end,
        accepted_response=accepted_response,
    )


def advance_activitywatch_import_fence(
    session: Session,
    device_id: str,
    *,
    now: datetime | None = None,
) -> int:
    """Invalidate older prepared snapshots and return a new device sequence."""
    lock_activity_write_plane(session)
    lock_activity_control_device(session, device_id)
    current = get_activitywatch_import_fence(
        session,
        device_id,
        lock=True,
    )
    sequence = (current or 0) + 1
    _persist_control_payload(
        session,
        device_id,
        {
            "device_id": device_id,
            "sequence": sequence,
        },
        event_type=ACTIVITYWATCH_IMPORT_FENCE_EVENT,
        source_record_id=_control_source_id(
            device_id,
            "activitywatch-import-fence",
        ),
        now=now,
    )
    return sequence


def invalidate_activitywatch_imports(
    session: Session,
    *,
    device_id: str | None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Fence in-flight ActivityWatch snapshots before a committed deletion."""
    lock_activity_write_plane(session)
    if device_id is not None:
        return {
            device_id: advance_activitywatch_import_fence(
                session,
                device_id,
                now=now,
            )
        }

    device_ids = sorted(
        {
            str(value)
            for value in session.scalars(
                select(WellnessEvent.source_device).where(
                    WellnessEvent.event_type
                    == ACTIVITYWATCH_IMPORT_FENCE_EVENT,
                    WellnessEvent.source_provider == CONTROL_PROVIDER,
                    WellnessEvent.source_device.is_not(None),
                )
            )
        }
    )
    return {
        value: advance_activitywatch_import_fence(
            session,
            value,
            now=now,
        )
        for value in device_ids
    }


def _control_payload_for_update(
    session: Session,
    device_id: str,
    *,
    event_type: str,
    kind: str,
    allowed_keys: set[str],
) -> dict[str, Any]:
    event = _typed_control_event(
        session,
        device_id,
        event_type=event_type,
        kind=kind,
        lock=True,
    )
    if event is not None and isinstance(event.payload, dict):
        return {
            key: value
            for key, value in event.payload.items()
            if key in allowed_keys
        }
    legacy = _legacy_control_payload(session, device_id, lock=True)
    return {key: value for key, value in legacy.items() if key in allowed_keys}


def update_collection_config(
    session: Session,
    device_id: str,
    update: ActivityCollectionUpdate,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    with activity_write_lock():
        lock_activity_write_plane(session)
        lock_activity_control_device(session, device_id)
        for attempt in range(2):
            stored = _control_payload_for_update(
                session,
                device_id,
                event_type=COLLECTION_CONFIG_EVENT,
                kind="config",
                allowed_keys=CONFIG_KEYS,
            )
            initial_platform = (
                update.platform.value
                if update.platform is not None
                else str(
                    stored.get("platform")
                    or ActivityPlatform.UNKNOWN.value
                )
            )
            try:
                initial_platform_value = ActivityPlatform(initial_platform)
            except ValueError:
                initial_platform_value = ActivityPlatform.UNKNOWN
            payload = {
                "device_id": device_id,
                "enabled": _default_collection_enabled(
                    device_id,
                    initial_platform_value,
                ),
                "excluded_apps": [],
                "paused_until": None,
                "config_revision": 0,
                **stored,
            }
            effective_platform = (
                update.platform.value
                if update.platform is not None
                else str(
                    payload.get("platform")
                    or ActivityPlatform.UNKNOWN.value
                )
            )
            effective_excluded_apps = (
                update.excluded_apps
                if update.excluded_apps is not None
                else list(payload.get("excluded_apps") or [])
            )
            if effective_platform == ActivityPlatform.IOS.value:
                exclusions_valid, exclusion_key_id = (
                    ios_exclusion_namespace(effective_excluded_apps)
                )
                invalid_exclusions_supplied = (
                    update.excluded_apps is not None
                    and not exclusions_valid
                )
                resume_requested = (
                    "paused_until" in update.model_fields_set
                    and update.paused_until is None
                )
                enable_requested = update.enabled is True
                containment_requested = (
                    update.enabled is False
                    or (
                        "paused_until" in update.model_fields_set
                        and update.paused_until is not None
                    )
                )
                if not exclusions_valid and (
                    invalid_exclusions_supplied
                    or enable_requested
                    or resume_requested
                    or not containment_requested
                ):
                    raise InvalidIOSAppTokenError(
                        "iOS excluded apps must be v2 tokens from one "
                        "device pseudonym key namespace"
                    )
                effective_key_id = (
                    exclusion_key_id
                    if exclusions_valid
                    else payload.get("ios_pseudonym_key_id")
                )
            else:
                effective_key_id = None
            changed = False
            if update.platform is not None and payload.get("platform") != update.platform.value:
                payload["platform"] = update.platform.value
                changed = True
            if update.enabled is not None and payload["enabled"] is not update.enabled:
                payload["enabled"] = update.enabled
                changed = True
            if update.excluded_apps is not None:
                if payload["excluded_apps"] != update.excluded_apps:
                    payload["excluded_apps"] = update.excluded_apps
                    changed = True
            if payload.get("ios_pseudonym_key_id") != effective_key_id:
                if effective_key_id is None:
                    payload.pop("ios_pseudonym_key_id", None)
                else:
                    payload["ios_pseudonym_key_id"] = effective_key_id
                changed = True
            if "paused_until" in update.model_fields_set:
                value = iso_or_none(update.paused_until)
                if payload["paused_until"] != value:
                    payload["paused_until"] = value
                    changed = True
            if changed:
                payload["config_revision"] = int(payload.get("config_revision", 0)) + 1
                advance_activitywatch_import_fence(
                    session,
                    device_id,
                    now=now,
                )
            try:
                _persist_control_payload(
                    session,
                    device_id,
                    payload,
                    event_type=COLLECTION_CONFIG_EVENT,
                    source_record_id=_control_source_id(device_id, "config"),
                    now=now,
                )
            except ActivityWriteConflictError:
                if attempt == 0:
                    continue
                raise
            return get_control_payload(
                session,
                device_id,
                platform=update.platform or ActivityPlatform.UNKNOWN,
            )
    raise AssertionError("unreachable")


def update_collection_status(
    session: Session,
    device_id: str,
    update: ActivityCollectionStatusUpdate,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    with activity_write_lock():
        lock_activity_write_plane(session)
        lock_activity_control_device(session, device_id)
        current = as_utc(now or datetime.now(UTC))
        for attempt in range(2):
            payload = {
                "device_id": device_id,
                **_control_payload_for_update(
                    session,
                    device_id,
                    event_type=COLLECTION_STATUS_EVENT,
                    kind="status",
                    allowed_keys=STATUS_KEYS,
                ),
            }
            values = update.model_dump(exclude_unset=True)
            boundary_update = bool(
                {
                    "permission_status",
                    "status_observed_at",
                    "collection_generation",
                    "pairing_revision",
                }
                & update.model_fields_set
            )
            if boundary_update:
                incoming_observed_at = (
                    update.status_observed_at
                    if update.status_observed_at is not None
                    else current
                )
                if _status_boundary_is_stale(
                    payload=payload,
                    update=update,
                    incoming_observed_at=incoming_observed_at,
                ):
                    return get_control_payload(
                        session,
                        device_id,
                        platform=update.platform or ActivityPlatform.UNKNOWN,
                    )
                values["status_observed_at"] = incoming_observed_at
                if (
                    update.platform is ActivityPlatform.ANDROID
                    and update.collection_generation is not None
                    and update.pairing_revision is None
                ):
                    values["pairing_revision"] = 0
            for key, value in values.items():
                if isinstance(value, datetime):
                    payload[key] = iso_or_none(value)
                elif hasattr(value, "value"):
                    payload[key] = value.value
                else:
                    payload[key] = value
            if boundary_update:
                advance_activitywatch_import_fence(
                    session,
                    device_id,
                    now=current,
                )
            try:
                _persist_control_payload(
                    session,
                    device_id,
                    payload,
                    event_type=COLLECTION_STATUS_EVENT,
                    source_record_id=_control_source_id(device_id, "status"),
                    now=current,
                )
            except ActivityWriteConflictError:
                if attempt == 0:
                    continue
                raise
            return get_control_payload(
                session,
                device_id,
                platform=update.platform or ActivityPlatform.UNKNOWN,
            )
    raise AssertionError("unreachable")


def update_cursor(
    session: Session,
    device_id: str,
    cursor_key: str,
    cursor_value: str,
    *,
    platform: ActivityPlatform,
    now: datetime | None = None,
) -> dict[str, Any]:
    with activity_write_lock():
        lock_activity_write_plane(session)
        lock_activity_control_device(session, device_id)
        payload = {
            "device_id": device_id,
            "platform": platform.value,
            "cursor_key": cursor_key,
            "cursor_value": cursor_value,
        }
        for attempt in range(2):
            try:
                _persist_control_payload(
                    session,
                    device_id,
                    payload,
                    event_type=COLLECTION_CURSOR_EVENT,
                    source_record_id=_cursor_source_id(device_id, cursor_key),
                    now=now,
                )
            except ActivityWriteConflictError:
                if attempt == 0:
                    continue
                raise
            return get_control_payload(session, device_id, platform=platform)
    raise AssertionError("unreachable")


def create_deletion_tombstone(
    session: Session,
    *,
    device_id: str | None,
    start: datetime | None,
    end: datetime,
    blocked_identity_digests: Iterable[str] = (),
    now: datetime | None = None,
) -> WellnessEvent:
    current = as_utc(now or datetime.now(UTC))
    normalized_start = as_utc(start) if start is not None else None
    normalized_end = min(as_utc(end), current)
    if normalized_start is not None and normalized_start >= normalized_end:
        raise ValueError("deletion tombstone range must include past time")
    tombstone_id = uuid.uuid4().hex
    identity_digests = sorted(set(blocked_identity_digests))

    def deletion_event(
        *,
        source_record_id: str,
        payload: dict[str, Any],
    ) -> WellnessEvent:
        return WellnessEvent(
            event_type=DELETION_TOMBSTONE_EVENT,
            schema_version=1,
            observed_at=current,
            recorded_at=current,
            timezone=None,
            source_provider=DELETION_PROVIDER,
            source_device=device_id,
            source_record_id=source_record_id,
            capture_method="user-deletion",
            quality_flags=None,
            confidence=None,
            coverage=None,
            sensitivity="activity-control",
            consent_scope="personal",
            retention_policy_id=None,
            expires_at=None,
            payload=payload,
            derived_from=None,
        )

    root = deletion_event(
        source_record_id=f"delete:{tombstone_id}",
        payload={
            "device_id": device_id,
            "start": iso_or_none(normalized_start),
            "end": normalized_end.isoformat(),
            "created_at": current.isoformat(),
            "identity_digest_count": len(identity_digests),
        },
    )
    events = [root]
    for offset in range(
        0,
        len(identity_digests),
        DELETION_IDENTITY_CHUNK_SIZE,
    ):
        chunk_index = offset // DELETION_IDENTITY_CHUNK_SIZE
        events.append(
            deletion_event(
                source_record_id=(
                    f"delete:{tombstone_id}:identities:{chunk_index}"
                ),
                payload={
                    "device_id": device_id,
                    "deletion_id": tombstone_id,
                    "identity_sha256": identity_digests[
                        offset : offset + DELETION_IDENTITY_CHUNK_SIZE
                    ],
                    "created_at": current.isoformat(),
                },
            )
        )
    session.add_all(events)
    session.flush(events)
    return root


def activity_deletion_tombstones(
    session: Session,
    *,
    device_id: str,
) -> tuple[WellnessEvent, ...]:
    rows = session.scalars(
        select(WellnessEvent).where(
            WellnessEvent.event_type == DELETION_TOMBSTONE_EVENT,
            WellnessEvent.source_provider == DELETION_PROVIDER,
            (
                WellnessEvent.source_device.is_(None)
                | (WellnessEvent.source_device == device_id)
            ),
        )
    )
    return tuple(rows)


def tombstoned_record_ids(
    session: Session,
    *,
    source_provider: str,
    device_id: str,
    records: list[ActivityRecord],
) -> set[str]:
    return set(
        filter_tombstoned_records(
            session,
            source_provider=source_provider,
            device_id=device_id,
            records=records,
        ).affected_source_record_ids
    )


def _subtract_tombstone_ranges(
    *,
    start: datetime,
    end: datetime,
    ranges: list[tuple[datetime | None, datetime]],
) -> list[tuple[datetime, datetime]]:
    spans = [(start, end)]
    for deleted_start, deleted_end in ranges:
        next_spans: list[tuple[datetime, datetime]] = []
        for span_start, span_end in spans:
            if deleted_end <= span_start or (
                deleted_start is not None and deleted_start >= span_end
            ):
                next_spans.append((span_start, span_end))
                continue
            if deleted_start is not None and span_start < deleted_start:
                next_spans.append(
                    (span_start, min(span_end, deleted_start))
                )
            if deleted_end < span_end:
                next_spans.append((max(span_start, deleted_end), span_end))
        spans = [
            (span_start, span_end)
            for span_start, span_end in next_spans
            if span_end > span_start
        ]
        if not spans:
            break
    return spans


def _tombstone_fragment(
    record: AppIntervalRecord,
    *,
    source_provider: str,
    device_id: str,
    start: datetime,
    end: datetime,
    mutable_end_identity: bool,
) -> AppIntervalRecord:
    return record.model_copy(
        update={
            "source_record_id": activity_fragment_source_record_id(
                source_provider=source_provider,
                source_device=device_id,
                source_record_id=record.source_record_id,
                start=start,
                end=end,
                root_start=record.start_at,
                mutable_end_identity=mutable_end_identity,
            ),
            "start_at": start,
            "end_at": end,
            "launches": record.launches if start == record.start_at else 0,
        }
    )


def filter_tombstoned_records(
    session: Session,
    *,
    source_provider: str,
    device_id: str,
    records: list[ActivityRecord],
    allow_mutable_interval_end: bool = False,
) -> ActivityTombstoneFilterResult:
    """Apply user-deletion tombstones without widening a partial delete.

    Fully deleted source identities stay blocked even if a queued retry moves
    them elsewhere. Range-only tombstones subtract only the deleted portion of
    detailed intervals; hourly aggregate rows remain all-or-nothing because
    the deletion API rejects partial-hour requests.
    """
    tombstones = activity_deletion_tombstones(session, device_id=device_id)
    if not tombstones:
        return ActivityTombstoneFilterResult(
            records=tuple(records),
            affected_source_record_ids=frozenset(),
        )
    exact_digests = {
        str(digest)
        for row in tombstones
        for digest in (
            row.payload.get("identity_sha256", [])
            if isinstance(row.payload, dict)
            and isinstance(row.payload.get("identity_sha256"), list)
            else []
        )
    }
    ranges = []
    for row in tombstones:
        if not isinstance(row.payload, dict):
            continue
        end = parse_optional_datetime(row.payload.get("end"))
        if end is not None:
            ranges.append(
                (
                    parse_optional_datetime(row.payload.get("start")),
                    end,
                )
            )
    accepted: list[ActivityRecord] = []
    affected: set[str] = set()
    expected_fragment_ids: dict[str, set[str]] = {}
    expected_fragment_records: dict[str, list[AppIntervalRecord]] = {}
    for record in records:
        digest = activity_source_identity_digest(
            source_provider=source_provider,
            source_device=device_id,
            source_record_id=record.source_record_id,
        )
        if digest in exact_digests:
            affected.add(record.source_record_id)
            continue
        record_start, record_end = record_bounds(record)
        remaining = _subtract_tombstone_ranges(
            start=record_start,
            end=record_end,
            ranges=ranges,
        )
        if remaining == [(record_start, record_end)]:
            accepted.append(record)
            continue
        affected.add(record.source_record_id)
        if isinstance(record, AppHourRecord):
            if remaining:
                raise ActivityConflictError(
                    "hourly aggregate overlaps a partial user-deletion tombstone"
                )
            continue
        fragments = [
            _tombstone_fragment(
                record,
                source_provider=source_provider,
                device_id=device_id,
                start=fragment_start,
                end=fragment_end,
                mutable_end_identity=allow_mutable_interval_end,
            )
            for fragment_start, fragment_end in remaining
        ]
        accepted.extend(fragments)
        root_digest = activity_fragment_root_identity_digest(
            source_provider=source_provider,
            source_device=device_id,
            source_record_id=record.source_record_id,
        )
        expected_fragment_ids.setdefault(root_digest, set()).update(
            fragment.source_record_id for fragment in fragments
        )
        expected_fragment_records.setdefault(root_digest, []).extend(
            fragments
        )
    for root_digest, expected_ids in expected_fragment_ids.items():
        prefix = f"{ACTIVITY_FRAGMENT_PREFIX}:{root_digest}:"
        mutable_prefix = (
            f"{ACTIVITY_FRAGMENT_PREFIX}:"
            f"{ACTIVITY_MUTABLE_FRAGMENT_VERSION}:{root_digest}:"
        )
        existing_rows = list(
            session.scalars(
                select(WellnessEvent).where(
                    WellnessEvent.event_type == APP_INTERVAL_EVENT,
                    WellnessEvent.source_provider == source_provider,
                    WellnessEvent.source_device == device_id,
                    (
                        WellnessEvent.source_record_id.like(f"{prefix}%")
                        | WellnessEvent.source_record_id.like(
                            f"{mutable_prefix}%"
                        )
                    ),
                )
            )
        )
        existing_ids = {
            str(row.source_record_id)
            for row in existing_rows
        }
        if not existing_ids:
            continue
        if not allow_mutable_interval_end:
            if existing_ids == expected_ids:
                continue
            raise ActivityConflictError(
                "activity source bounds changed after a partial user deletion; "
                "explicit repair is required"
            )
        if allow_mutable_interval_end:
            expected_records = expected_fragment_records[root_digest]
            expected_root_starts = {
                activity_fragment_root_start_microseconds(
                    record.source_record_id
                )
                for record in expected_records
            }
            existing_root_starts = {
                activity_fragment_root_start_microseconds(
                    str(row.source_record_id)
                )
                for row in existing_rows
            }
            expected_semantics = {
                (
                    record.state.value,
                    record.app_id,
                    record.category,
                    record.source_group_id,
                )
                for record in expected_records
            }
            existing_semantics = {
                (
                    str(row.payload.get("state")),
                    row.payload.get("app_id"),
                    row.payload.get("category"),
                    row.payload.get("source_group_id"),
                )
                for row in existing_rows
                if isinstance(row.payload, dict)
            }
            if (
                len(expected_root_starts) == 1
                and None not in expected_root_starts
                and expected_root_starts == existing_root_starts
                and len(expected_semantics) == 1
                and expected_semantics == existing_semantics
            ):
                continue
            if (
                existing_ids == expected_ids
                and existing_root_starts == {None}
            ):
                # Legacy exact-bound fragments remain replayable, but any
                # mutable-bound change still fails closed.
                continue
        if existing_ids != expected_ids:
            raise ActivityConflictError(
                "activity source bounds changed after a partial user deletion; "
                "explicit repair is required"
            )
        raise ActivityConflictError(
            "activity source semantics changed after a partial user deletion; "
            "explicit repair is required"
        )
    return ActivityTombstoneFilterResult(
        records=tuple(accepted),
        affected_source_record_ids=frozenset(affected),
    )


def serialize_collection_state(
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = as_utc(now or datetime.now(UTC))
    gate = collection_gate(payload, now=current)
    queue_oldest = parse_optional_datetime(payload.get("queue_oldest_at"))
    queue_age = (
        max(0, int((current - queue_oldest).total_seconds())) if queue_oldest is not None else None
    )
    return {
        "device_id": str(payload["device_id"]),
        "platform": str(payload.get("platform", ActivityPlatform.UNKNOWN.value)),
        "enabled": bool(payload.get("enabled", True)),
        "excluded_apps": list(payload.get("excluded_apps") or []),
        "paused_until": parse_optional_datetime(payload.get("paused_until")),
        "effective_collecting": gate.allowed,
        "blocked_reason": gate.reason,
        "permission_status": str(
            payload.get("permission_status", ActivityPermissionStatus.UNKNOWN.value)
        ),
        "capability": str(payload.get("capability", ActivityCapability.UNAVAILABLE.value)),
        "status_reason": payload.get("status_reason"),
        "status_observed_at": parse_optional_datetime(
            payload.get("status_observed_at")
        ),
        "collection_generation": _collection_generation(
            payload.get("collection_generation")
        ),
        "last_collected_at": parse_optional_datetime(payload.get("last_collected_at")),
        "last_uploaded_at": parse_optional_datetime(payload.get("last_uploaded_at")),
        "queue_oldest_at": queue_oldest,
        "queue_age_seconds": queue_age,
        "queue_depth": int(payload.get("queue_depth", 0)),
        "coverage": payload.get("coverage"),
        "config_revision": int(payload.get("config_revision", 0)),
        "cursors": dict(payload.get("cursors") or {}),
        "ios_pseudonym_key_id": payload.get(
            "ios_pseudonym_key_id"
        ),
    }


def upsert_summary_event(
    session: Session,
    *,
    event_type: str,
    source_record_id: str,
    observed_at: datetime,
    timezone: str,
    payload: dict[str, Any],
    derived_from: dict[str, Any],
    policy: RetentionPolicy | None = None,
) -> WellnessEvent:
    if policy is None:
        policies = ensure_activity_policies(session)
        data_class = (
            ACTIVITY_HOURLY_CLASS if event_type == HOUR_SUMMARY_EVENT else ACTIVITY_DAILY_CLASS
        )
        policy = policies[data_class]
    event = session.scalar(
        select(WellnessEvent)
        .where(
            WellnessEvent.source_provider == SUMMARY_PROVIDER,
            WellnessEvent.source_record_id == source_record_id,
        )
        .with_for_update()
    )
    current = datetime.now(UTC)
    if event is None:
        event = WellnessEvent(
            event_type=event_type,
            schema_version=1,
            observed_at=as_utc(observed_at),
            recorded_at=current,
            timezone=timezone,
            source_provider=SUMMARY_PROVIDER,
            source_device=None,
            source_record_id=source_record_id,
            capture_method="derived",
            quality_flags=None,
            confidence=None,
            coverage=(
                payload.get("source_coverage", {}).get("ratio")
                if isinstance(payload.get("source_coverage"), dict)
                else None
            ),
            sensitivity="activity-summary",
            consent_scope="personal",
            retention_policy_id=policy.id,
            expires_at=event_expiry(policy, observed_at),
            payload=payload,
            derived_from=derived_from,
        )
        try:
            with session.begin_nested():
                session.add(event)
                session.flush([event])
            return event
        except IntegrityError:
            event = session.scalar(
                select(WellnessEvent)
                .where(
                    WellnessEvent.source_provider == SUMMARY_PROVIDER,
                    WellnessEvent.source_record_id == source_record_id,
                )
                .with_for_update()
            )
            if event is None:
                raise ActivityWriteConflictError(
                    "summary identity raced but could not be reloaded"
                )
    event.event_type = event_type
    event.observed_at = as_utc(observed_at)
    event.recorded_at = current
    event.timezone = timezone
    event.coverage = (
        payload.get("source_coverage", {}).get("ratio")
        if isinstance(payload.get("source_coverage"), dict)
        else None
    )
    event.retention_policy_id = policy.id
    event.expires_at = event_expiry(policy, observed_at)
    event.payload = payload
    event.derived_from = derived_from
    session.flush([event])
    return event


def active_state_record(
    *,
    source_record_id: str,
    start_at: datetime,
    end_at: datetime,
    app_id: str,
    category: str | None,
    launches: int = 0,
) -> AppIntervalRecord:
    return AppIntervalRecord(
        source_record_id=source_record_id,
        start_at=start_at,
        end_at=end_at,
        state=ActivityState.ACTIVE,
        app_id=app_id,
        category=category,
        launches=launches,
    )
