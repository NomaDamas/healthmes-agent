"""Personal Data Node storage control plane.

The database owns policy and audit state; payload bytes remain below
``HEALTHMES_DATA_DIR``. Maintenance is deliberately idempotent and path-safe.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.config import Settings
from healthmes.store import (
    MedicalRecord,
    PurgeJob,
    RawIngestEvent,
    RetentionPolicy,
    StorageObject,
    StorageUsageDaily,
    WellnessEvent,
)
from healthmes.store.session import session_scope

RETENTION_PRESETS: dict[str, int | None] = {
    "1d": 1,
    "7d": 7,
    "14d": 14,
    "30d": 30,
    "90d": 90,
    "forever": None,
}
DEFAULT_RETENTION: dict[str, str] = {
    "raw_payload": "14d",
    "media": "7d",
    "nutrition_media": "7d",
    "nutrition_raw_capture": "14d",
    "normalized": "30d",
    "nutrition_observation": "90d",
    "nutrition_confirmation": "forever",
    "aggregate": "forever",
    "decision": "forever",
    "medical_record": "forever",
}


@dataclass(frozen=True, slots=True)
class StorageMaintenanceReport:
    job_id: str
    dry_run: bool
    candidates: int
    deleted: int
    bytes_reclaimed: int
    errors: tuple[str, ...]


def build_storage_maintenance_job(settings: Settings):
    """Return the scheduler-safe zero-argument lifecycle job."""

    def job() -> None:
        with session_scope() as session:
            run_storage_maintenance(session, settings)

    return job


def _now() -> datetime:
    return datetime.now(UTC)


def ensure_default_policies(session: Session) -> list[RetentionPolicy]:
    policies = {row.data_class: row for row in session.scalars(select(RetentionPolicy))}
    for data_class, preset in DEFAULT_RETENTION.items():
        if data_class not in policies:
            row = RetentionPolicy(
                data_class=data_class,
                retention_days=RETENTION_PRESETS[preset],
                enabled=True,
            )
            session.add(row)
            policies[data_class] = row
    session.flush()
    return sorted(policies.values(), key=lambda row: row.data_class)


def update_retention_policy(
    session: Session, data_class: str, preset: str
) -> RetentionPolicy:
    if preset not in RETENTION_PRESETS:
        raise ValueError(f"unsupported retention preset: {preset}")
    ensure_default_policies(session)
    policy = session.scalar(
        select(RetentionPolicy).where(RetentionPolicy.data_class == data_class)
    )
    if policy is None:
        policy = RetentionPolicy(data_class=data_class, enabled=True)
        session.add(policy)
    previous_retention_days = policy.retention_days
    policy.retention_days = RETENTION_PRESETS[preset]
    session.flush()
    _recalculate_expiry(
        session,
        policy,
        previous_retention_days=previous_retention_days,
    )
    return policy


def _expiry(policy: RetentionPolicy, observed_at: datetime) -> datetime | None:
    if not policy.enabled or policy.retention_days is None:
        return None
    return observed_at + timedelta(days=policy.retention_days)


def _recalculate_expiry(
    session: Session,
    policy: RetentionPolicy,
    *,
    previous_retention_days: int | None,
) -> None:
    current = _now()
    for obj in session.scalars(
        select(StorageObject).where(
            StorageObject.data_class == policy.data_class,
            StorageObject.purged_at.is_(None),
        )
    ):
        if (
            obj.expires_at is not None
            and _as_utc(obj.expires_at) <= current
        ):
            continue
        obj.retention_policy_id = policy.id
        basis = obj.retention_basis_at
        if (
            basis is None
            and obj.expires_at is not None
            and previous_retention_days is not None
        ):
            basis = obj.expires_at - timedelta(
                days=previous_retention_days
            )
        basis = basis or obj.created_at
        obj.retention_basis_at = basis
        obj.expires_at = _expiry(policy, basis)
    for event in session.scalars(
        select(WellnessEvent).where(WellnessEvent.retention_policy_id == policy.id)
    ):
        if (
            event.expires_at is not None
            and _as_utc(event.expires_at) <= current
        ):
            continue
        event.expires_at = _expiry(policy, event.observed_at)


def register_storage_object(
    session: Session,
    settings: Settings,
    *,
    relative_path: str,
    data_class: str,
    content_type: str | None,
    size_bytes: int,
    sha256: str | None = None,
    observed_at: datetime | None = None,
    safe_to_purge: bool = True,
) -> StorageObject:
    existing = session.scalar(
        select(StorageObject).where(StorageObject.relative_path == relative_path)
    )
    if existing is not None:
        return existing
    policies = {row.data_class: row for row in ensure_default_policies(session)}
    policy = policies.get(data_class)
    observed = observed_at or _now()
    obj = StorageObject(
        data_class=data_class,
        relative_path=relative_path,
        content_type=content_type,
        size_bytes=size_bytes,
        sha256=sha256,
        retention_policy_id=policy.id if policy else None,
        retention_basis_at=observed,
        expires_at=_expiry(policy, observed) if policy else None,
        safe_to_purge=safe_to_purge,
    )
    session.add(obj)
    session.flush()
    return obj


def classify_storage_object(
    session: Session,
    obj: StorageObject,
    *,
    data_class: str,
    observed_at: datetime,
    safe_to_purge: bool,
) -> StorageObject:
    """Move an indexed object under a purpose-specific retention policy."""
    policies = {row.data_class: row for row in ensure_default_policies(session)}
    policy = policies[data_class]
    already_classified = obj.data_class == data_class
    obj.data_class = data_class
    obj.retention_policy_id = policy.id
    if not already_classified or obj.retention_basis_at is None:
        obj.retention_basis_at = observed_at
        obj.expires_at = _expiry(policy, observed_at)
    obj.safe_to_purge = safe_to_purge
    session.flush()
    return obj


def index_raw_ingest(
    session: Session, settings: Settings, raw: RawIngestEvent
) -> WellnessEvent:
    obj = register_storage_object(
        session,
        settings,
        relative_path=raw.path,
        data_class="raw_payload",
        content_type=raw.content_type,
        size_bytes=raw.size_bytes,
        sha256=raw.sha256,
        observed_at=raw.received_at,
    )
    existing = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.source_provider == raw.source,
            WellnessEvent.source_record_id == str(raw.id),
        )
    )
    if existing is not None:
        return existing
    policy = session.get(RetentionPolicy, obj.retention_policy_id)
    event = WellnessEvent(
        event_type="raw_ingest",
        observed_at=raw.received_at,
        recorded_at=raw.received_at,
        source_provider=raw.source,
        source_record_id=str(raw.id),
        capture_method="import",
        retention_policy_id=obj.retention_policy_id,
        expires_at=_expiry(policy, raw.received_at) if policy else None,
        payload={
            "content_type": raw.content_type,
            "size_bytes": raw.size_bytes,
            "parse_status": raw.parse_status,
            "forward_status": raw.forward_status,
        },
        raw_object_id=obj.id,
    )
    session.add(event)
    session.flush()
    return event


def _class_for(path: Path, root: Path) -> str:
    relative = path.relative_to(root)
    first = relative.parts[0] if relative.parts else "other"
    return {
        "raw_ingest": "raw_payload",
        "media": "media",
        "backups": "backup",
        "exports": "export",
    }.get(first, "other")


def _discover_unindexed(session: Session, settings: Settings) -> None:
    root = settings.data_dir.resolve()
    if not root.exists():
        return
    known = set(session.scalars(select(StorageObject.relative_path)))
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        data_class = _class_for(path, root)
        if relative in known or data_class not in {"raw_payload", "media"}:
            continue
        stat = path.stat()
        register_storage_object(
            session,
            settings,
            relative_path=relative,
            data_class=data_class,
            content_type=None,
            size_bytes=stat.st_size,
            observed_at=datetime.fromtimestamp(stat.st_mtime, UTC),
        )


def _usage_index(session: Session) -> dict[str, str]:
    return {
        row.relative_path: row.data_class
        for row in session.scalars(
            select(StorageObject).where(StorageObject.purged_at.is_(None))
        )
    }


def _scan_usage(
    settings: Settings,
    indexed: dict[str, str],
) -> dict[str, dict[str, int]]:
    root = settings.data_dir.resolve()
    totals: dict[str, dict[str, int]] = {}
    if root.exists():
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            data_class = indexed.get(relative, _class_for(path, root))
            bucket = totals.setdefault(data_class, {"bytes": 0, "objects": 0})
            bucket["bytes"] += path.stat().st_size
            bucket["objects"] += 1
    return totals


def _record_usage(
    session: Session,
    totals: dict[str, dict[str, int]],
) -> None:
    today = date.today()
    for data_class, values in totals.items():
        row = session.scalar(
            select(StorageUsageDaily).where(
                StorageUsageDaily.measured_on == today,
                StorageUsageDaily.provider == "local",
                StorageUsageDaily.data_class == data_class,
            )
        )
        if row is None:
            row = StorageUsageDaily(
                measured_on=today, provider="local", data_class=data_class
            )
            session.add(row)
        row.bytes_used = values["bytes"]
        row.object_count = values["objects"]
    session.flush()


def measure_usage(session: Session, settings: Settings) -> dict[str, dict[str, int]]:
    totals = _scan_usage(settings, _usage_index(session))
    _record_usage(session, totals)
    return totals


async def measure_usage_async(
    session: Session,
    settings: Settings,
) -> dict[str, dict[str, int]]:
    """Measure files off-loop while keeping the DB session on its owner thread."""
    indexed = _usage_index(session)
    totals = await asyncio.to_thread(_scan_usage, settings, indexed)
    _record_usage(session, totals)
    return totals


def _safe_path(settings: Settings, relative_path: str) -> Path | None:
    root = settings.data_dir.resolve()
    candidate = (root / relative_path).resolve()
    return candidate if candidate.is_relative_to(root) else None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _strip_legacy_raw_fields(value: object) -> object:
    if isinstance(value, list):
        return [_strip_legacy_raw_fields(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: (
            []
            if key == "warnings"
            else None
            if key
            in {
                "source_text",
                "media_path",
                "evidence_text",
                "note",
            }
            else _strip_legacy_raw_fields(item)
        )
        for key, item in value.items()
    }


def _legacy_raw_texts(value: object) -> list[str]:
    if isinstance(value, list):
        return [
            text
            for item in value
            for text in _legacy_raw_texts(item)
        ]
    if not isinstance(value, dict):
        return []
    texts: list[str] = []
    for key, item in value.items():
        if key == "evidence_text" and isinstance(item, str):
            texts.append(item)
        elif key == "warnings" and isinstance(item, list):
            texts.extend(
                warning
                for warning in item
                if isinstance(warning, str)
            )
        else:
            texts.extend(_legacy_raw_texts(item))
    return list(dict.fromkeys(texts))


def _migrate_legacy_nutrition_raw_captures(
    session: Session,
    *,
    current: datetime,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    raw_policy = session.scalar(
        select(RetentionPolicy).where(
            RetentionPolicy.data_class == "nutrition_raw_capture"
        )
    )
    if raw_policy is None:  # pragma: no cover - defaults own this invariant
        return
    raw_record_ids = set(
        session.scalars(
            select(WellnessEvent.source_record_id).where(
                WellnessEvent.source_provider == "nutrition-raw-capture"
            )
        )
    )
    linked_raw_object_ids = set(
        session.scalars(
            select(WellnessEvent.raw_object_id).where(
                WellnessEvent.event_type == "nutrition.raw-capture.v1",
                WellnessEvent.raw_object_id.is_not(None),
            )
        )
    )
    legacy_events = session.scalars(
        select(WellnessEvent).where(
            WellnessEvent.source_provider == "nutrition-interaction"
        )
    )
    for event in legacy_events:
        source_text = event.payload.get("source_text")
        media_path = event.payload.get("media_path")
        warnings = event.payload.get("warnings")
        items = event.payload.get("items")
        item_warnings = (
            [
                (
                    item.get("warnings", [])
                    if isinstance(item, dict)
                    else []
                )
                for item in items
            ]
            if isinstance(items, list)
            else []
        )
        raw_texts = _legacy_raw_texts(event.payload)
        if (
            source_text is None
            and media_path is None
            and not raw_texts
        ):
            continue
        observed_at = _as_utc(event.observed_at)
        expires_at = (
            None
            if not raw_policy.enabled
            or raw_policy.retention_days is None
            else observed_at
            + timedelta(days=raw_policy.retention_days)
        )
        if (
            event.source_record_id not in raw_record_ids
            and (expires_at is None or expires_at > _as_utc(current))
        ):
            raw_object_id = None
            if isinstance(media_path, str):
                obj = session.scalar(
                    select(StorageObject).where(
                        StorageObject.relative_path == media_path
                    )
                )
                if obj is not None and obj.id not in linked_raw_object_ids:
                    raw_object_id = obj.id
                    linked_raw_object_ids.add(obj.id)
            session.add(
                WellnessEvent(
                    event_type="nutrition.raw-capture.v1",
                    schema_version=1,
                    observed_at=event.observed_at,
                    recorded_at=event.recorded_at,
                    timezone=event.timezone,
                    source_provider="nutrition-raw-capture",
                    source_device=event.source_device,
                    source_record_id=event.source_record_id,
                    capture_method=event.capture_method,
                    quality_flags=None,
                    confidence=None,
                    coverage=None,
                    sensitivity=event.sensitivity,
                    consent_scope=event.consent_scope,
                    retention_policy_id=raw_policy.id,
                    expires_at=expires_at,
                    payload={
                        "operation_fingerprint": event.payload.get(
                            "operation_fingerprint"
                        ),
                        "source_text": source_text,
                        "media_path": media_path,
                        "warnings": (
                            warnings
                            if isinstance(warnings, list)
                            else []
                        ),
                        "item_warnings": item_warnings,
                        "legacy_raw_texts": raw_texts,
                    },
                    raw_object_id=raw_object_id,
                    derived_from={
                        "interaction_id": event.source_record_id
                    },
                )
            )
            raw_record_ids.add(event.source_record_id)
        event.payload = _strip_legacy_raw_fields(event.payload)
        event.quality_flags = {
            "warning_count": (
                event.quality_flags.get("warning_count", 0)
                if isinstance(event.quality_flags, dict)
                else 0
            )
        }
    durable_legacy_events = session.scalars(
        select(WellnessEvent).where(
            WellnessEvent.event_type.in_(
                (
                    "nutrition.intake-outcome.v1",
                    "nutrition.decision-request.v1",
                    "nutrition.decision.v1",
                )
            )
        )
    )
    for event in durable_legacy_events:
        note = event.payload.get("note")
        if (
            event.event_type == "nutrition.intake-outcome.v1"
            and isinstance(note, str)
            and note
        ):
            raw_source_record_id = event.source_record_id
            existing_raw = session.scalar(
                select(WellnessEvent.id).where(
                    WellnessEvent.source_provider
                    == "nutrition-outcome-raw",
                    WellnessEvent.source_record_id
                    == raw_source_record_id,
                )
            )
            raw_expires_at = (
                None
                if not raw_policy.enabled
                or raw_policy.retention_days is None
                else _as_utc(event.recorded_at)
                + timedelta(days=raw_policy.retention_days)
            )
            if existing_raw is None and (
                raw_expires_at is None
                or raw_expires_at > _as_utc(current)
            ):
                session.add(
                    WellnessEvent(
                        event_type="nutrition.outcome-raw.v1",
                        schema_version=1,
                        observed_at=event.recorded_at,
                        recorded_at=event.recorded_at,
                        timezone=event.timezone,
                        source_provider="nutrition-outcome-raw",
                        source_device=event.source_device,
                        source_record_id=raw_source_record_id,
                        capture_method="manual",
                        quality_flags=None,
                        confidence=None,
                        sensitivity=event.sensitivity,
                        consent_scope=event.consent_scope,
                        retention_policy_id=raw_policy.id,
                        expires_at=raw_expires_at,
                        payload={
                            "operation_fingerprint": event.payload.get(
                                "operation_fingerprint"
                            ),
                            "note": note,
                        },
                        derived_from={
                            "outcome_id": raw_source_record_id
                        },
                    )
                )
        event.payload = _strip_legacy_raw_fields(event.payload)
        if isinstance(event.quality_flags, dict):
            event.quality_flags = _strip_legacy_raw_fields(
                event.quality_flags
            )


def run_storage_maintenance(
    session: Session,
    settings: Settings,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> StorageMaintenanceReport:
    current = now or _now()
    ensure_default_policies(session)
    _migrate_legacy_nutrition_raw_captures(
        session,
        current=current,
        dry_run=dry_run,
    )
    _discover_unindexed(session, settings)
    job = PurgeJob(started_at=current, dry_run=dry_run, status="running")
    session.add(job)
    session.flush()
    candidates = list(
        session.scalars(
            select(StorageObject).where(
                StorageObject.purged_at.is_(None),
                StorageObject.safe_to_purge.is_(True),
                StorageObject.expires_at.is_not(None),
                StorageObject.expires_at <= current,
            )
        )
    )
    deleted = 0
    reclaimed = 0
    errors: list[str] = []
    for obj in candidates:
        path = _safe_path(settings, obj.relative_path)
        if path is None:
            errors.append(f"unsafe path rejected: {obj.relative_path}")
            continue
        if dry_run:
            continue
        try:
            if path.exists():
                path.unlink()
            if obj.data_class in {"media", "nutrition_media"}:
                medical_rows = session.scalars(
                    select(MedicalRecord).where(
                        MedicalRecord.media_path == obj.relative_path
                    )
                )
                for row in medical_rows:
                    row.media_path = None
            if obj.data_class == "raw_payload":
                raw = session.scalar(
                    select(RawIngestEvent).where(
                        RawIngestEvent.path == obj.relative_path
                    )
                )
                if raw is not None:
                    session.delete(raw)
            obj.purged_at = current
            deleted += 1
            reclaimed += obj.size_bytes
        except OSError as exc:
            errors.append(f"{obj.relative_path}: {exc}")
    if not dry_run:
        expired_events = session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.expires_at.is_not(None),
                WellnessEvent.expires_at <= current,
            )
        )
        for event in expired_events:
            session.delete(event)
    measure_usage(session, settings)
    job.finished_at = _now()
    job.status = "completed_with_errors" if errors else "completed"
    job.candidates = len(candidates)
    job.deleted = deleted
    job.bytes_reclaimed = reclaimed
    job.detail = {"errors": errors}
    session.flush()
    return StorageMaintenanceReport(
        job_id=str(job.id),
        dry_run=dry_run,
        candidates=len(candidates),
        deleted=deleted,
        bytes_reclaimed=reclaimed,
        errors=tuple(errors),
    )
