"""Persistence for canonical activity events and collector control state."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
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
from healthmes.activity.privacy import collection_gate
from healthmes.storage import ensure_default_policies
from healthmes.store import RetentionPolicy, WellnessEvent

APP_HOUR_EVENT = "activity.app-hour.v1"
APP_INTERVAL_EVENT = "activity.app-interval.v1"
HOUR_SUMMARY_EVENT = "activity.hour-summary.v1"
DAY_SUMMARY_EVENT = "activity.day-summary.v1"
COLLECTION_CONTROL_EVENT = "activity.collection-control.v1"

ACTIVITY_RAW_CLASS = "activity_raw"
ACTIVITY_HOURLY_CLASS = "activity_hourly"
ACTIVITY_DAILY_CLASS = "activity_daily"
ACTIVITY_RETENTION_DEFAULTS: dict[str, int | None] = {
    ACTIVITY_RAW_CLASS: 14,
    ACTIVITY_HOURLY_CLASS: 90,
    ACTIVITY_DAILY_CLASS: None,
}

CONTROL_PROVIDER = "healthmes-activity-control"
SUMMARY_PROVIDER = "healthmes-activity-aggregator"
RAW_EVENT_TYPES = (APP_HOUR_EVENT, APP_INTERVAL_EVENT)
SUMMARY_EVENT_TYPES = (HOUR_SUMMARY_EVENT, DAY_SUMMARY_EVENT)


class ActivityConflictError(ValueError):
    """A source identity was reused for different immutable input."""


@dataclass(frozen=True, slots=True)
class PersistResult:
    event: WellnessEvent
    state: str


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
    return {
        **common,
        "kind": record.kind,
        "start_at": record.start_at.isoformat(),
        "end_at": record.end_at.isoformat(),
        "state": record.state.value,
        "app_id": record.app_id,
        "launches": record.launches,
        "category": record.category,
    }


def _observed_at(record: ActivityRecord) -> datetime:
    if isinstance(record, AppHourRecord):
        return record.bucket_start
    return record.start_at


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
        select(WellnessEvent).where(
            WellnessEvent.source_provider == batch.source_provider,
            WellnessEvent.source_record_id == record.source_record_id,
        )
    )
    if existing is not None:
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
        session.flush()
        return PersistResult(existing, "updated")

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
    session.add(event)
    session.flush()
    return PersistResult(event, "created")


def _control_source_id(device_id: str) -> str:
    digest = hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:32]
    return f"device:{digest}"


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
        "last_collected_at": None,
        "last_uploaded_at": None,
        "queue_oldest_at": None,
        "queue_depth": 0,
        "coverage": None,
        "config_revision": 0,
        "cursors": {},
    }


def get_control_event(session: Session, device_id: str) -> WellnessEvent | None:
    return session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == COLLECTION_CONTROL_EVENT,
            WellnessEvent.source_provider == CONTROL_PROVIDER,
            WellnessEvent.source_record_id == _control_source_id(device_id),
        )
    )


def get_control_payload(
    session: Session,
    device_id: str,
    *,
    platform: ActivityPlatform = ActivityPlatform.UNKNOWN,
) -> dict[str, Any]:
    event = get_control_event(session, device_id)
    if event is None or not isinstance(event.payload, dict):
        return default_control_payload(device_id, platform=platform)
    payload = {**default_control_payload(device_id, platform=platform), **event.payload}
    payload["device_id"] = device_id
    return payload


def _persist_control_payload(
    session: Session,
    device_id: str,
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
) -> WellnessEvent:
    current = as_utc(now or datetime.now(UTC))
    event = get_control_event(session, device_id)
    if event is None:
        event = WellnessEvent(
            event_type=COLLECTION_CONTROL_EVENT,
            schema_version=1,
            observed_at=current,
            recorded_at=current,
            timezone=None,
            source_provider=CONTROL_PROVIDER,
            source_device=device_id,
            source_record_id=_control_source_id(device_id),
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
        session.add(event)
    else:
        event.observed_at = current
        event.recorded_at = current
        event.source_device = device_id
        event.payload = payload
    session.flush()
    return event


def update_collection_config(
    session: Session,
    device_id: str,
    update: ActivityCollectionUpdate,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    payload = get_control_payload(
        session,
        device_id,
        platform=update.platform or ActivityPlatform.UNKNOWN,
    )
    changed = False
    if update.platform is not None and payload["platform"] != update.platform.value:
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
    _persist_control_payload(session, device_id, payload, now=now)
    return payload


def update_collection_status(
    session: Session,
    device_id: str,
    update: ActivityCollectionStatusUpdate,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    payload = get_control_payload(
        session,
        device_id,
        platform=update.platform or ActivityPlatform.UNKNOWN,
    )
    values = update.model_dump(exclude_unset=True)
    for key, value in values.items():
        if isinstance(value, datetime):
            payload[key] = iso_or_none(value)
        elif hasattr(value, "value"):
            payload[key] = value.value
        else:
            payload[key] = value
    _persist_control_payload(session, device_id, payload, now=now)
    return payload


def update_cursor(
    session: Session,
    device_id: str,
    cursor_key: str,
    cursor_value: str,
    *,
    platform: ActivityPlatform,
    now: datetime | None = None,
) -> dict[str, Any]:
    payload = get_control_payload(session, device_id, platform=platform)
    cursors = dict(payload.get("cursors") or {})
    cursors[cursor_key] = cursor_value
    payload["cursors"] = cursors
    payload["platform"] = platform.value
    _persist_control_payload(session, device_id, payload, now=now)
    return payload


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
        select(WellnessEvent).where(
            WellnessEvent.source_provider == SUMMARY_PROVIDER,
            WellnessEvent.source_record_id == source_record_id,
        )
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
        session.add(event)
    else:
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
    session.flush()
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
