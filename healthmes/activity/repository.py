"""Persistence for canonical activity events and collector control state."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from healthmes.activity.contracts import (
    ActivityBatchIn,
    ActivityCapability,
    ActivityCollectionStatusUpdate,
    ActivityCollectionUpdate,
    ActivityPermissionStatus,
    ActivityPlatform,
    ActivityRecord,
    ActivityState,
    AppHourRecord,
    AppIntervalRecord,
)
from healthmes.activity.locking import activity_write_lock
from healthmes.activity.privacy import BLOCKED_PERMISSION_STATES, collection_gate
from healthmes.storage import ensure_default_policies
from healthmes.store import RetentionPolicy, WellnessEvent

APP_HOUR_EVENT = "activity.app-hour.v1"
APP_INTERVAL_EVENT = "activity.app-interval.v1"
HOUR_SUMMARY_EVENT = "activity.hour-summary.v1"
DAY_SUMMARY_EVENT = "activity.day-summary.v1"
# Kept as a read-only compatibility contract for pre-split control rows.
COLLECTION_CONTROL_EVENT = "activity.collection-control.v1"
COLLECTION_CONFIG_EVENT = "activity.collection-config.v1"
COLLECTION_STATUS_EVENT = "activity.collection-status.v1"
COLLECTION_CURSOR_EVENT = "activity.collection-cursor.v1"
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
SUMMARY_PROVIDER = "healthmes-activity-aggregator"
RAW_EVENT_TYPES = (APP_HOUR_EVENT, APP_INTERVAL_EVENT)
SUMMARY_EVENT_TYPES = (HOUR_SUMMARY_EVENT, DAY_SUMMARY_EVENT)
CONTROL_EVENT_TYPES = (
    COLLECTION_CONTROL_EVENT,
    COLLECTION_CONFIG_EVENT,
    COLLECTION_STATUS_EVENT,
    COLLECTION_CURSOR_EVENT,
)

class ActivityConflictError(ValueError):
    """A source identity was reused for different immutable input."""


class ActivityWriteConflictError(RuntimeError):
    """A concurrent writer won creation of the same canonical identity."""


@dataclass(frozen=True, order=True, slots=True)
class ActivityLocalScope:
    day: date
    timezone: str


@dataclass(frozen=True, slots=True)
class PersistResult:
    event: WellnessEvent
    state: str
    previous_scopes: tuple[ActivityLocalScope, ...] = ()


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
    zone = ZoneInfo(timezone)
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
                zone = ZoneInfo(timezone)
                day = date.fromisoformat(raw_day)
            except (ValueError, ZoneInfoNotFoundError):
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
        zone = ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError):
        timezone = "UTC"
        zone = ZoneInfo("UTC")
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
        return {
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
    if previous == fingerprint:
        return PersistResult(existing, "duplicate")
    if not allow_replace:
        raise ActivityConflictError(
            "source_record_id was already used with different activity input"
        )
    previous_scopes = event_scopes(existing)
    observed = _observed_at(record)
    existing.event_type = (
        APP_HOUR_EVENT if isinstance(record, AppHourRecord) else APP_INTERVAL_EVENT
    )
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
}
STATUS_KEYS = {
    "device_id",
    "platform",
    "permission_status",
    "capability",
    "status_reason",
    "status_observed_at",
    "last_collected_at",
    "last_uploaded_at",
    "queue_oldest_at",
    "queue_depth",
    "coverage",
}


def _control_source_id(device_id: str, kind: str = "legacy") -> str:
    digest = hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:32]
    return f"{kind}:device:{digest}"


def _cursor_source_id(device_id: str, cursor_key: str) -> str:
    digest = hashlib.sha256(
        f"{device_id}\0{cursor_key}".encode()
    ).hexdigest()
    return f"cursor:{digest}"


def default_control_payload(
    device_id: str,
    *,
    platform: ActivityPlatform = ActivityPlatform.UNKNOWN,
) -> dict[str, Any]:
    return {
        "device_id": device_id,
        "platform": platform.value,
        "enabled": True,
        "excluded_apps": [],
        "paused_until": None,
        "permission_status": ActivityPermissionStatus.UNKNOWN.value,
        "capability": ActivityCapability.UNAVAILABLE.value,
        "status_reason": None,
        "status_observed_at": None,
        "last_collected_at": None,
        "last_uploaded_at": None,
        "queue_oldest_at": None,
        "queue_depth": 0,
        "coverage": None,
        "config_revision": 0,
        "cursors": {},
    }


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
        statement = statement.with_for_update()
    return session.scalar(statement)


def _legacy_control_payload(
    session: Session,
    device_id: str,
) -> dict[str, Any]:
    event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == COLLECTION_CONTROL_EVENT,
            WellnessEvent.source_provider == CONTROL_PROVIDER,
            WellnessEvent.source_record_id.in_(
                (
                    _control_source_id(device_id),
                    f"device:{hashlib.sha256(device_id.encode('utf-8')).hexdigest()[:32]}",
                )
            ),
        )
    )
    return dict(event.payload) if event is not None and isinstance(event.payload, dict) else {}


def get_control_payload(
    session: Session,
    device_id: str,
    *,
    platform: ActivityPlatform = ActivityPlatform.UNKNOWN,
) -> dict[str, Any]:
    payload = {
        **default_control_payload(device_id, platform=platform),
        **_legacy_control_payload(session, device_id),
    }
    status_event = _typed_control_event(
        session,
        device_id,
        event_type=COLLECTION_STATUS_EVENT,
        kind="status",
    )
    if status_event is not None and isinstance(status_event.payload, dict):
        payload.update(status_event.payload)
    config_event = _typed_control_event(
        session,
        device_id,
        event_type=COLLECTION_CONFIG_EVENT,
        kind="config",
    )
    if config_event is not None and isinstance(config_event.payload, dict):
        payload.update(config_event.payload)
    cursor_rows = session.scalars(
        select(WellnessEvent).where(
            WellnessEvent.event_type == COLLECTION_CURSOR_EVENT,
            WellnessEvent.source_provider == CONTROL_PROVIDER,
            WellnessEvent.source_device == device_id,
        )
    )
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
    legacy = _legacy_control_payload(session, device_id)
    return {key: value for key, value in legacy.items() if key in allowed_keys}


def update_collection_config(
    session: Session,
    device_id: str,
    update: ActivityCollectionUpdate,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    with activity_write_lock():
        for attempt in range(2):
            payload = {
                "device_id": device_id,
                "enabled": True,
                "excluded_apps": [],
                "paused_until": None,
                "config_revision": 0,
                **_control_payload_for_update(
                    session,
                    device_id,
                    event_type=COLLECTION_CONFIG_EVENT,
                    kind="config",
                    allowed_keys=CONFIG_KEYS,
                ),
            }
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
            if "paused_until" in update.model_fields_set:
                value = iso_or_none(update.paused_until)
                if payload["paused_until"] != value:
                    payload["paused_until"] = value
                    changed = True
            if changed:
                payload["config_revision"] = int(payload.get("config_revision", 0)) + 1
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
            incoming_observed_at = (
                update.status_observed_at
                if update.status_observed_at is not None
                else current
            )
            existing_observed_at = parse_optional_datetime(
                payload.get("status_observed_at")
            )
            incoming_permission = (
                update.permission_status.value
                if update.permission_status is not None
                else payload.get("permission_status")
            )
            existing_permission = payload.get("permission_status")
            if (
                existing_observed_at is not None
                and (
                    incoming_observed_at < existing_observed_at
                    or (
                        incoming_observed_at == existing_observed_at
                        and existing_permission in BLOCKED_PERMISSION_STATES
                        and incoming_permission not in BLOCKED_PERMISSION_STATES
                    )
                )
            ):
                return get_control_payload(
                    session,
                    device_id,
                    platform=update.platform or ActivityPlatform.UNKNOWN,
                )
            values["status_observed_at"] = incoming_observed_at
            for key, value in values.items():
                if isinstance(value, datetime):
                    payload[key] = iso_or_none(value)
                elif hasattr(value, "value"):
                    payload[key] = value.value
                else:
                    payload[key] = value
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
    tombstones = activity_deletion_tombstones(session, device_id=device_id)
    if not tombstones:
        return set()
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
    blocked: set[str] = set()
    for record in records:
        digest = activity_source_identity_digest(
            source_provider=source_provider,
            source_device=device_id,
            source_record_id=record.source_record_id,
        )
        if digest in exact_digests:
            blocked.add(record.source_record_id)
            continue
        record_start, record_end = record_bounds(record)
        for start, end in ranges:
            if start is not None and record_end <= start:
                continue
            if record_start < end:
                blocked.add(record.source_record_id)
                break
    return blocked


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
        "last_collected_at": parse_optional_datetime(payload.get("last_collected_at")),
        "last_uploaded_at": parse_optional_datetime(payload.get("last_uploaded_at")),
        "queue_oldest_at": queue_oldest,
        "queue_age_seconds": queue_age,
        "queue_depth": int(payload.get("queue_depth", 0)),
        "coverage": payload.get("coverage"),
        "config_revision": int(payload.get("config_revision", 0)),
        "cursors": dict(payload.get("cursors") or {}),
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
