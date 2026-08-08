"""Personal Data Node storage control plane.

The database owns policy and audit state; payload bytes remain below
``HEALTHMES_DATA_DIR``. Maintenance is deliberately idempotent and path-safe.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from re import fullmatch

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from healthmes.config import Settings
from healthmes.nutrition.ledger_lock import lock_nutrition_ledger
from healthmes.nutrition.operation_integrity import (
    RESULT_PAYLOAD_DIGEST_FIELD,
    is_sha256_digest,
    result_payload_digest,
)
from healthmes.storage.retention_lock import lock_retention_policies
from healthmes.store import (
    FoodLog,
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
_NUTRITION_OPERATION_EVENT = "nutrition.operation.v1"
_NUTRITION_OPERATION_PROVIDER = "nutrition-operation"
_INTERACTION_TRANSITION_EVENT = "nutrition.interaction-transition.v1"
_INTERACTION_TRANSITION_PROVIDER = "nutrition-interaction-transition"
_COMPLETED_OPERATION_MARKER_FIELDS = (
    "operation_kind",
    "operation_id",
    "operation_fingerprint",
    "operation_state",
)
_MAINTENANCE_QUARANTINE_FLAG = "maintenance_quarantine"
_LEGACY_TRANSITION_QUARANTINE_REASON = "legacy_transition_metadata_unmigrated"
_LEGACY_RESULT_IDENTITY_QUARANTINE_REASON = "legacy_operation_result_identity_invalid"
_LEGACY_JSON_QUARANTINE_REASON = "legacy_json_document_invalid"


@dataclass(frozen=True, slots=True)
class _NutritionOperationMarkerSpec:
    event_type: str
    source_provider: str
    operation_kind: str
    operation_prefix: str
    payload_operation_id_field: str


_NUTRITION_OPERATION_MARKER_SPECS = (
    _NutritionOperationMarkerSpec(
        "nutrition.confirmation.v1",
        "user-confirmation",
        "caffeine_confirmation",
        "caffeine-confirmation",
        "confirmation_id",
    ),
    _NutritionOperationMarkerSpec(
        "nutrition.review.v1",
        "user-nutrition-review",
        "nutrition_review",
        "nutrition-review",
        "review_id",
    ),
    _NutritionOperationMarkerSpec(
        "nutrition.daily-confirmation.v1",
        "user-confirmation",
        "daily_intake_confirmation",
        "daily-confirmation",
        "confirmation_id",
    ),
    _NutritionOperationMarkerSpec(
        "nutrition.interaction-review.v1",
        "nutrition-intake-review",
        "intake_interaction_review",
        "intake-review",
        "review_id",
    ),
    _NutritionOperationMarkerSpec(
        "nutrition.intake-outcome.v1",
        "nutrition-intake-outcome",
        "intake_outcome",
        "intake-outcome",
        "outcome_id",
    ),
    _NutritionOperationMarkerSpec(
        "nutrition.decision-request.v1",
        "nutrition-decision-request",
        "intake_decision_request",
        "intake-decision-request",
        "request_id",
    ),
    _NutritionOperationMarkerSpec(
        "nutrition.decision.v1",
        "nutrition-decision",
        "intake_decision",
        "intake-decision",
        "decision_id",
    ),
)


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


def _default_retention_days(data_class: str) -> int | None:
    try:
        preset = DEFAULT_RETENTION[data_class]
    except KeyError as exc:
        raise ValueError(f"unsupported retention data class: {data_class}") from exc
    return RETENTION_PRESETS[preset]


def retention_policies_for_write(
    session: Session,
    data_classes: set[str],
) -> dict[str, RetentionPolicy]:
    """Lock known policy rows before deriving expiry for new records."""

    return lock_retention_policies(
        session,
        {data_class: _default_retention_days(data_class) for data_class in data_classes},
    )


def retention_policy_for_write(
    session: Session,
    data_class: str,
) -> RetentionPolicy:
    return retention_policies_for_write(
        session,
        {data_class},
    )[data_class]


def _lock_all_retention_policies(
    session: Session,
) -> list[RetentionPolicy]:
    defaults = {
        data_class: RETENTION_PRESETS[preset] for data_class, preset in DEFAULT_RETENTION.items()
    }
    for data_class in session.scalars(select(RetentionPolicy.data_class)):
        defaults.setdefault(data_class, None)
    policies = lock_retention_policies(session, defaults)
    return sorted(policies.values(), key=lambda row: row.data_class)


def ensure_default_policies(session: Session) -> list[RetentionPolicy]:
    policies = lock_retention_policies(
        session,
        {data_class: RETENTION_PRESETS[preset] for data_class, preset in DEFAULT_RETENTION.items()},
    )
    return sorted(policies.values(), key=lambda row: row.data_class)


def update_retention_policy(session: Session, data_class: str, preset: str) -> RetentionPolicy:
    if preset not in RETENTION_PRESETS:
        raise ValueError(f"unsupported retention preset: {preset}")
    policy = lock_retention_policies(
        session,
        {data_class: RETENTION_PRESETS[preset]},
    )[data_class]
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
        if obj.expires_at is not None and _as_utc(obj.expires_at) <= current:
            continue
        obj.retention_policy_id = policy.id
        basis = obj.retention_basis_at
        if basis is None and obj.expires_at is not None and previous_retention_days is not None:
            basis = obj.expires_at - timedelta(days=previous_retention_days)
        basis = basis or obj.created_at
        obj.retention_basis_at = basis
        obj.expires_at = _expiry(policy, basis)
    for event in session.scalars(
        select(WellnessEvent).where(WellnessEvent.retention_policy_id == policy.id)
    ):
        if event.expires_at is not None and _as_utc(event.expires_at) <= current:
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
    policy = (
        retention_policy_for_write(session, data_class) if data_class in DEFAULT_RETENTION else None
    )
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
    policy = retention_policy_for_write(session, data_class)
    already_classified = obj.data_class == data_class
    obj.data_class = data_class
    obj.retention_policy_id = policy.id
    if not already_classified or obj.retention_basis_at is None:
        obj.retention_basis_at = observed_at
        obj.expires_at = _expiry(policy, observed_at)
    obj.safe_to_purge = safe_to_purge
    session.flush()
    return obj


def index_raw_ingest(session: Session, settings: Settings, raw: RawIngestEvent) -> WellnessEvent:
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


def _is_internal_lock_file(path: Path) -> bool:
    return path.name.startswith(".") and path.name.endswith(".healthmes.lock")


def _discover_unindexed(session: Session, settings: Settings) -> None:
    root = settings.data_dir.resolve()
    if not root.exists():
        return
    known = set(session.scalars(select(StorageObject.relative_path)))
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if _is_internal_lock_file(path):
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


def measure_usage(session: Session, settings: Settings) -> dict[str, dict[str, int]]:
    root = settings.data_dir.resolve()
    totals: dict[str, dict[str, int]] = {}
    indexed = {
        row.relative_path: row.data_class
        for row in session.scalars(select(StorageObject).where(StorageObject.purged_at.is_(None)))
    }
    if root.exists():
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if _is_internal_lock_file(path):
                continue
            relative = path.relative_to(root).as_posix()
            data_class = indexed.get(relative, _class_for(path, root))
            bucket = totals.setdefault(data_class, {"bytes": 0, "objects": 0})
            bucket["bytes"] += path.stat().st_size
            bucket["objects"] += 1
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
            row = StorageUsageDaily(measured_on=today, provider="local", data_class=data_class)
            session.add(row)
        row.bytes_used = values["bytes"]
        row.object_count = values["objects"]
    session.flush()
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
        return [text for item in value for text in _legacy_raw_texts(item)]
    if not isinstance(value, dict):
        return []
    texts: list[str] = []
    for key, item in value.items():
        if key == "evidence_text" and isinstance(item, str):
            texts.append(item)
        elif key == "warnings" and isinstance(item, list):
            texts.extend(warning for warning in item if isinstance(warning, str))
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
        select(RetentionPolicy).where(RetentionPolicy.data_class == "nutrition_raw_capture")
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
    legacy_events = session.scalars(
        select(WellnessEvent).where(WellnessEvent.source_provider == "nutrition-interaction")
    )
    for event in legacy_events:
        if _has_malformed_legacy_json_quarantine(event):
            continue
        source_text = event.payload.get("source_text")
        media_path = event.payload.get("media_path")
        warnings = event.payload.get("warnings")
        items = event.payload.get("items")
        item_warnings = (
            [(item.get("warnings", []) if isinstance(item, dict) else []) for item in items]
            if isinstance(items, list)
            else []
        )
        raw_texts = _legacy_raw_texts(event.payload)
        if source_text is None and media_path is None and not raw_texts:
            continue
        observed_at = _as_utc(event.observed_at)
        expires_at = (
            None
            if not raw_policy.enabled or raw_policy.retention_days is None
            else observed_at + timedelta(days=raw_policy.retention_days)
        )
        if event.source_record_id not in raw_record_ids and (
            expires_at is None or expires_at > _as_utc(current)
        ):
            raw_object_id = None
            if isinstance(media_path, str):
                obj = session.scalar(
                    select(StorageObject).where(StorageObject.relative_path == media_path)
                )
                raw_object_id = obj.id if obj is not None else None
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
                        "operation_fingerprint": event.payload.get("operation_fingerprint"),
                        "source_text": source_text,
                        "media_path": media_path,
                        "warnings": (warnings if isinstance(warnings, list) else []),
                        "item_warnings": item_warnings,
                        "legacy_raw_texts": raw_texts,
                    },
                    raw_object_id=raw_object_id,
                    derived_from={"interaction_id": event.source_record_id},
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
        if _has_malformed_legacy_json_quarantine(event):
            continue
        spec = _operation_result_spec(event)
        operation_id = _operation_id_for_result(event, spec) if spec is not None else None
        if spec is not None and operation_id is not None:
            marker = _event_by_source_identity(
                session,
                source_provider=_NUTRITION_OPERATION_PROVIDER,
                source_record_id=f"{spec.operation_prefix}:{operation_id}",
            )
            if marker is not None and RESULT_PAYLOAD_DIGEST_FIELD in marker.payload:
                # Current result payloads are cryptographically bound to their
                # marker. Only explicitly marker-less legacy rows may be
                # rewritten by this raw-capture migration.
                continue
        note = event.payload.get("note")
        if event.event_type == "nutrition.intake-outcome.v1" and isinstance(note, str) and note:
            raw_source_record_id = event.source_record_id
            existing_raw = session.scalar(
                select(WellnessEvent.id).where(
                    WellnessEvent.source_provider == "nutrition-outcome-raw",
                    WellnessEvent.source_record_id == raw_source_record_id,
                )
            )
            raw_expires_at = (
                None
                if not raw_policy.enabled or raw_policy.retention_days is None
                else _as_utc(event.recorded_at) + timedelta(days=raw_policy.retention_days)
            )
            if existing_raw is None and (
                raw_expires_at is None or raw_expires_at > _as_utc(current)
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
                            "operation_fingerprint": event.payload.get("operation_fingerprint"),
                            "note": note,
                        },
                        derived_from={"outcome_id": raw_source_record_id},
                    )
                )
        event.payload = _strip_legacy_raw_fields(event.payload)
        if isinstance(event.quality_flags, dict):
            event.quality_flags = _strip_legacy_raw_fields(event.quality_flags)


def _event_by_source_identity(
    session: Session,
    *,
    source_provider: str,
    source_record_id: str,
) -> WellnessEvent | None:
    return session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.source_provider == source_provider,
            WellnessEvent.source_record_id == source_record_id,
        )
    )


def _persist_event_by_source_identity(
    session: Session,
    event: WellnessEvent,
) -> WellnessEvent:
    existing = _event_by_source_identity(
        session,
        source_provider=event.source_provider,
        source_record_id=event.source_record_id,
    )
    if existing is not None:
        if existing.event_type != event.event_type:
            raise RuntimeError("wellness event source identity belongs to another event type")
        return existing
    try:
        with session.begin_nested():
            session.add(event)
            session.flush()
    except IntegrityError:
        existing = _event_by_source_identity(
            session,
            source_provider=event.source_provider,
            source_record_id=event.source_record_id,
        )
        if existing is None or existing.event_type != event.event_type:
            raise
        return existing
    return event


def _operation_id_for_result(
    event: WellnessEvent,
    spec: _NutritionOperationMarkerSpec,
) -> uuid.UUID | None:
    prefix = f"{spec.operation_prefix}:"
    if event.source_record_id.startswith(prefix):
        raw_source_operation_id = event.source_record_id.removeprefix(prefix)
    else:
        raw_source_operation_id = event.source_record_id
    source_operation_id = _canonical_uuid(raw_source_operation_id)
    payload_operation_id = _canonical_uuid(event.payload.get(spec.payload_operation_id_field))
    if source_operation_id is None or payload_operation_id != source_operation_id:
        return None
    return source_operation_id


def _legacy_transition_details(
    event: WellnessEvent,
    spec: _NutritionOperationMarkerSpec,
    operation_id: uuid.UUID,
) -> tuple[uuid.UUID, str, str, uuid.UUID] | None:
    if spec.operation_kind == "intake_interaction_review":
        mutation_kind = "review"
        accepted_statuses = {"confirmed", "corrected", "rejected"}
    elif spec.operation_kind == "intake_outcome":
        mutation_kind = "outcome"
        accepted_statuses = {"consumed", "not_consumed", "cancelled"}
    else:
        return None
    raw_interaction_id = event.payload.get("interaction_id")
    status = event.payload.get("status")
    interaction_id = _canonical_uuid(raw_interaction_id)
    if interaction_id is None:
        return None
    if not isinstance(status, str) or status not in accepted_statuses:
        return None
    return interaction_id, mutation_kind, status, operation_id


def _operation_marker_spec(
    marker: WellnessEvent,
) -> _NutritionOperationMarkerSpec | None:
    operation_kind = marker.payload.get("operation_kind")
    return next(
        (
            value
            for value in _NUTRITION_OPERATION_MARKER_SPECS
            if value.operation_kind == operation_kind
        ),
        None,
    )


def _operation_result_spec(
    event: WellnessEvent,
) -> _NutritionOperationMarkerSpec | None:
    return next(
        (
            value
            for value in _NUTRITION_OPERATION_MARKER_SPECS
            if value.event_type == event.event_type
            and value.source_provider == event.source_provider
        ),
        None,
    )


def _canonical_uuid(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return parsed if value == str(parsed) else None


def _operation_id_for_marker(marker: WellnessEvent) -> uuid.UUID | None:
    spec = _operation_marker_spec(marker)
    if spec is None:
        return None
    prefix = f"{spec.operation_prefix}:"
    if not marker.source_record_id.startswith(prefix):
        return None
    source_operation_id = _canonical_uuid(marker.source_record_id.removeprefix(prefix))
    if source_operation_id is None:
        return None
    if "operation_id" not in marker.payload:
        return source_operation_id
    raw_operation_id = marker.payload.get("operation_id")
    payload_operation_id = _canonical_uuid(raw_operation_id)
    if payload_operation_id != source_operation_id:
        return None
    return source_operation_id


def _legacy_marker_transition_details(
    marker: WellnessEvent,
) -> tuple[uuid.UUID, str, str, uuid.UUID] | None:
    if marker.payload.get("operation_state") != "completed":
        return None
    operation_kind = marker.payload.get("operation_kind")
    if operation_kind == "intake_interaction_review":
        mutation_kind = "review"
        accepted_statuses = {"confirmed", "corrected", "rejected"}
        status_fields = ("review_status", "mutation_status", "status")
    elif operation_kind == "intake_outcome":
        mutation_kind = "outcome"
        accepted_statuses = {"consumed", "not_consumed", "cancelled"}
        status_fields = ("outcome_status", "mutation_status", "status")
    else:
        return None

    interaction_id = _canonical_uuid(marker.payload.get("interaction_id"))
    if interaction_id is None:
        return None
    status = next(
        (
            marker.payload.get(field)
            for field in status_fields
            if isinstance(marker.payload.get(field), str)
        ),
        None,
    )
    if status not in accepted_statuses:
        return None
    operation_id = _operation_id_for_marker(marker)
    if operation_id is None:
        return None
    return interaction_id, mutation_kind, status, operation_id


def _legacy_marker_has_transition_metadata(marker: WellnessEvent) -> bool:
    payload = marker.payload
    if any(
        field in marker.payload for field in ("review_status", "outcome_status", "mutation_status")
    ):
        return True
    transition_scope = payload.get("operation_kind") in {
        "intake_interaction_review",
        "intake_outcome",
    } or marker.source_record_id.startswith(("intake-review:", "intake-outcome:"))
    return transition_scope and any(field in payload for field in ("interaction_id", "status"))


def _transition_revision(event: WellnessEvent) -> int | None:
    if not isinstance(event.payload, dict):
        return None
    revision = event.payload.get("revision")
    if type(revision) is not int or revision < 1:
        return None
    return revision


def _transition_identity(
    event: WellnessEvent,
) -> tuple[str, uuid.UUID, str] | None:
    if not isinstance(event.payload, dict):
        return None
    mutation_kind = event.payload.get("mutation_kind")
    status = event.payload.get("mutation_status")
    accepted_statuses = {
        "review": {"confirmed", "corrected", "rejected"},
        "outcome": {"consumed", "not_consumed", "cancelled"},
    }
    if (
        not isinstance(mutation_kind, str)
        or not isinstance(status, str)
        or status not in accepted_statuses.get(mutation_kind, set())
    ):
        return None
    operation_id = _canonical_uuid(event.payload.get("operation_id"))
    if operation_id is None:
        return None
    return mutation_kind, operation_id, status


def _validated_transition_chain(
    transitions: list[WellnessEvent],
    interaction_id: uuid.UUID,
) -> tuple[int, dict[tuple[str, uuid.UUID], str]] | None:
    transitions.sort(
        key=lambda event: (
            _transition_revision(event) or 0,
            _as_utc(event.recorded_at),
            str(event.id),
        )
    )
    next_revision = 1
    identities: dict[tuple[str, uuid.UUID], str] = {}
    outcome_seen = False
    for transition in transitions:
        revision = _transition_revision(transition)
        identity = _transition_identity(transition)
        if (
            revision != next_revision
            or identity is None
            or not isinstance(transition.payload, dict)
            or transition.payload.get("interaction_id") != str(interaction_id)
            or transition.source_record_id != f"{interaction_id}:{next_revision}"
        ):
            return None
        mutation_kind, operation_id, status = identity
        if mutation_kind == "review" and outcome_seen:
            return None
        key = (mutation_kind, operation_id)
        if key in identities:
            return None
        identities[key] = status
        if mutation_kind == "outcome":
            outcome_seen = True
        next_revision += 1
    return next_revision, identities


def _backfill_legacy_interaction_transitions(
    session: Session,
    candidates: list[
        tuple[
            WellnessEvent,
            _NutritionOperationMarkerSpec,
            uuid.UUID,
        ]
    ],
) -> None:
    grouped_entries: dict[
        uuid.UUID,
        dict[
            tuple[str, uuid.UUID],
            tuple[WellnessEvent, str, str, uuid.UUID],
        ],
    ] = {}

    def add_entry(
        event: WellnessEvent,
        details: tuple[uuid.UUID, str, str, uuid.UUID],
    ) -> None:
        interaction_id, mutation_kind, status, operation_id = details
        entries = grouped_entries.setdefault(interaction_id, {})
        key = (mutation_kind, operation_id)
        existing = entries.get(key)
        if existing is None or (
            existing[0].event_type == _NUTRITION_OPERATION_EVENT
            and event.event_type != _NUTRITION_OPERATION_EVENT
        ):
            entries[key] = (
                event,
                mutation_kind,
                status,
                operation_id,
            )

    for event, spec, operation_id in candidates:
        details = _legacy_transition_details(event, spec, operation_id)
        if details is None:
            continue
        add_entry(event, details)

    legacy_markers = session.scalars(
        select(WellnessEvent).where(
            WellnessEvent.event_type == _NUTRITION_OPERATION_EVENT,
            WellnessEvent.source_provider == _NUTRITION_OPERATION_PROVIDER,
        )
    )
    for marker in legacy_markers:
        if _has_malformed_legacy_json_quarantine(marker):
            continue
        details = _legacy_marker_transition_details(marker)
        if details is not None:
            add_entry(marker, details)

    grouped = {
        interaction_id: list(entries.values())
        for interaction_id, entries in grouped_entries.items()
    }
    if not grouped:
        return

    for interaction_id, entries in grouped.items():
        ordering_keys: set[tuple[datetime, datetime, str]] = set()
        ambiguous_order = False
        for event, mutation_kind, _status, _operation_id in entries:
            ordering_key = (
                _as_utc(event.created_at),
                _as_utc(event.recorded_at),
                mutation_kind,
            )
            if ordering_key in ordering_keys:
                ambiguous_order = True
                break
            ordering_keys.add(ordering_key)
        if ambiguous_order:
            continue

        from healthmes.nutrition.intake_service import (
            IntakeInteractionError,
            lock_interaction_transition_state,
        )

        try:
            lock_interaction_transition_state(
                session,
                interaction_id,
                allow_legacy_without_marker=True,
            )
        except IntakeInteractionError:
            continue
        # When legacy timestamps tie, review-before-outcome is the only
        # ordering that can form a valid interaction transition chain.
        entries.sort(
            key=lambda value: (
                _as_utc(value[0].created_at),
                _as_utc(value[0].recorded_at),
                0 if value[1] == "review" else 1,
                str(value[0].id),
            )
        )
        existing = list(
            session.scalars(
                select(WellnessEvent).where(
                    WellnessEvent.event_type == _INTERACTION_TRANSITION_EVENT,
                    WellnessEvent.source_provider == _INTERACTION_TRANSITION_PROVIDER,
                    WellnessEvent.source_record_id.like(f"{interaction_id}:%"),
                )
            )
        )
        chain = _validated_transition_chain(existing, interaction_id)
        if chain is None:
            continue
        next_revision, existing_identities = chain
        outcome_seen = any(
            identity is not None and identity[0] == "outcome"
            for identity in (_transition_identity(event) for event in existing)
        )

        pending_entries = []
        incompatible = False
        for entry in entries:
            _, mutation_kind, status, operation_id = entry
            existing_status = existing_identities.get((mutation_kind, operation_id))
            if existing_status is None:
                if mutation_kind == "review" and outcome_seen:
                    incompatible = True
                    break
                pending_entries.append(entry)
                if mutation_kind == "outcome":
                    outcome_seen = True
            elif existing_status != status:
                incompatible = True
                break
        if incompatible:
            continue

        for event, mutation_kind, status, operation_id in pending_entries:
            transition_payload: dict[str, object] = {
                "interaction_id": str(interaction_id),
                "revision": next_revision,
                "mutation_kind": mutation_kind,
                "operation_id": str(operation_id),
                "mutation_status": status,
                "legacy_backfill": True,
            }
            if mutation_kind == "outcome":
                from healthmes.nutrition.intake_contracts import (
                    outcome_from_payload,
                )

                outcome_source = event
                if event.event_type != "nutrition.intake-outcome.v1":
                    outcome_spec = next(
                        spec
                        for spec in _NUTRITION_OPERATION_MARKER_SPECS
                        if spec.operation_kind == "intake_outcome"
                    )
                    stored_outcome = _operation_result(
                        session,
                        spec=outcome_spec,
                        operation_id=operation_id,
                    )
                    if stored_outcome is not None:
                        outcome_source = stored_outcome
                try:
                    outcome = outcome_from_payload(outcome_source.payload)
                except (AttributeError, KeyError, TypeError, ValueError):
                    outcome = None
                if outcome is not None and outcome.intake_snapshot is not None:
                    transition_payload.update(
                        {
                            "interaction_observed_at": _as_utc(
                                outcome.intake_snapshot.observed_at
                            ).isoformat(),
                            "outcome_consumed_at": (
                                _as_utc(outcome.consumed_at).isoformat()
                                if outcome.consumed_at is not None
                                else None
                            ),
                        }
                    )
            transition = WellnessEvent(
                event_type=_INTERACTION_TRANSITION_EVENT,
                schema_version=1,
                observed_at=event.recorded_at,
                recorded_at=event.recorded_at,
                timezone=event.timezone,
                source_provider=_INTERACTION_TRANSITION_PROVIDER,
                source_device=None,
                source_record_id=f"{interaction_id}:{next_revision}",
                capture_method="system",
                quality_flags=None,
                confidence=None,
                sensitivity="wellness",
                consent_scope="personal",
                retention_policy_id=None,
                expires_at=None,
                payload=transition_payload,
                derived_from={"interaction_id": str(interaction_id)},
            )
            stored = _persist_event_by_source_identity(session, transition)
            if (
                stored.payload.get("interaction_id") != str(interaction_id)
                or stored.payload.get("revision") != next_revision
                or stored.payload.get("mutation_kind") != mutation_kind
                or stored.payload.get("mutation_status") != status
                or stored.payload.get("operation_id") != str(operation_id)
            ):
                break
            next_revision += 1


def _matching_transition_exists_for_marker(
    session: Session,
    marker: WellnessEvent,
) -> bool:
    details = _legacy_marker_transition_details(marker)
    if details is None:
        return False
    return _matching_transition_exists(session, details)


def _matching_transition_exists(
    session: Session,
    details: tuple[uuid.UUID, str, str, uuid.UUID],
) -> bool:
    interaction_id, mutation_kind, status, operation_id = details
    transitions = session.scalars(
        select(WellnessEvent).where(
            WellnessEvent.event_type == _INTERACTION_TRANSITION_EVENT,
            WellnessEvent.source_provider == _INTERACTION_TRANSITION_PROVIDER,
            WellnessEvent.source_record_id.like(f"{interaction_id}:%"),
        )
    )
    chain = _validated_transition_chain(
        list(transitions),
        interaction_id,
    )
    if chain is None:
        return False
    _, identities = chain
    return identities.get((mutation_kind, operation_id)) == status


def _maintenance_quarantine_is_active(
    event: WellnessEvent,
) -> bool:
    if not isinstance(event.quality_flags, dict):
        return False
    reason = event.quality_flags.get(_MAINTENANCE_QUARANTINE_FLAG)
    if reason == _LEGACY_JSON_QUARANTINE_REASON:
        return True
    spec = _operation_result_spec(event)
    if reason == _LEGACY_RESULT_IDENTITY_QUARANTINE_REASON:
        return spec is not None
    if reason != _LEGACY_TRANSITION_QUARANTINE_REASON:
        return False
    if spec is not None and spec.operation_kind in {
        "intake_interaction_review",
        "intake_outcome",
    }:
        return True
    return (
        event.event_type == _NUTRITION_OPERATION_EVENT
        and event.source_provider == _NUTRITION_OPERATION_PROVIDER
    )


def _has_malformed_legacy_json_quarantine(
    event: WellnessEvent,
) -> bool:
    return bool(
        isinstance(event.quality_flags, dict)
        and event.quality_flags.get(_MAINTENANCE_QUARANTINE_FLAG) == _LEGACY_JSON_QUARANTINE_REASON
    )


def _set_maintenance_quarantine(
    event: WellnessEvent,
    *,
    reason: str | None,
) -> None:
    quality_flags = dict(event.quality_flags) if isinstance(event.quality_flags, dict) else {}
    if reason is not None:
        quality_flags[_MAINTENANCE_QUARANTINE_FLAG] = reason
    else:
        quality_flags.pop(_MAINTENANCE_QUARANTINE_FLAG, None)
    normalized = quality_flags or None
    if event.quality_flags != normalized:
        event.quality_flags = normalized


def _quarantine_malformed_legacy_json_documents(
    session: Session,
) -> None:
    for event in session.scalars(select(WellnessEvent)):
        malformed_fields = []
        if not isinstance(event.payload, dict):
            malformed_fields.append("payload")
        if event.quality_flags is not None and not isinstance(event.quality_flags, dict):
            malformed_fields.append("quality_flags")
        if not malformed_fields:
            continue
        event.quality_flags = {
            _MAINTENANCE_QUARANTINE_FLAG: _LEGACY_JSON_QUARANTINE_REASON,
            "malformed_json_fields": malformed_fields,
        }


def _operation_identity_from_source(
    event: WellnessEvent,
) -> tuple[_NutritionOperationMarkerSpec, uuid.UUID] | None:
    if (
        event.event_type == _NUTRITION_OPERATION_EVENT
        and event.source_provider == _NUTRITION_OPERATION_PROVIDER
    ):
        for spec in _NUTRITION_OPERATION_MARKER_SPECS:
            prefix = f"{spec.operation_prefix}:"
            if not event.source_record_id.startswith(prefix):
                continue
            operation_id = _canonical_uuid(event.source_record_id.removeprefix(prefix))
            if operation_id is not None:
                return spec, operation_id
        return None
    spec = _operation_result_spec(event)
    if spec is None:
        return None
    prefix = f"{spec.operation_prefix}:"
    source_record_id = event.source_record_id
    operation_id = _canonical_uuid(
        source_record_id.removeprefix(prefix)
        if source_record_id.startswith(prefix)
        else source_record_id
    )
    return (spec, operation_id) if operation_id is not None else None


def _invalidated_operation_marker_payload(
    spec: _NutritionOperationMarkerSpec,
    operation_id: uuid.UUID,
) -> dict[str, object]:
    return {
        "operation_kind": spec.operation_kind,
        "operation_id": str(operation_id),
        "operation_fingerprint": None,
        "operation_state": "invalidated",
        "legacy_quarantine": True,
    }


def _ensure_quarantined_operation_tombstones(
    session: Session,
    *,
    current: datetime,
) -> None:
    """Keep only non-content operation identity before quarantined results purge."""

    for event in list(session.scalars(select(WellnessEvent))):
        if (
            not _maintenance_quarantine_is_active(event)
            or event.expires_at is None
            or _as_utc(event.expires_at) > current
        ):
            continue
        identity = _operation_identity_from_source(event)
        if identity is None:
            continue
        spec, operation_id = identity
        marker_id = f"{spec.operation_prefix}:{operation_id}"
        marker = _event_by_source_identity(
            session,
            source_provider=_NUTRITION_OPERATION_PROVIDER,
            source_record_id=marker_id,
        )
        payload = _invalidated_operation_marker_payload(
            spec,
            operation_id,
        )
        if marker is None:
            session.add(
                WellnessEvent(
                    event_type=_NUTRITION_OPERATION_EVENT,
                    schema_version=1,
                    observed_at=current,
                    recorded_at=current,
                    timezone=None,
                    source_provider=_NUTRITION_OPERATION_PROVIDER,
                    source_device=None,
                    source_record_id=marker_id,
                    capture_method="system",
                    quality_flags=None,
                    confidence=None,
                    sensitivity="wellness",
                    consent_scope="personal",
                    retention_policy_id=None,
                    expires_at=None,
                    payload=payload,
                    derived_from=None,
                )
            )
            continue
        if marker is event:
            marker.event_type = _NUTRITION_OPERATION_EVENT
            marker.schema_version = 1
            marker.observed_at = current
            marker.recorded_at = current
            marker.timezone = None
            marker.source_device = None
            marker.capture_method = "system"
            marker.quality_flags = None
            marker.confidence = None
            marker.sensitivity = "wellness"
            marker.consent_scope = "personal"
            marker.retention_policy_id = None
            marker.expires_at = None
            marker.payload = payload
            marker.raw_object_id = None
            marker.derived_from = None
    session.flush()


def _operation_result(
    session: Session,
    *,
    spec: _NutritionOperationMarkerSpec,
    operation_id: uuid.UUID,
) -> WellnessEvent | None:
    for source_record_id in (
        f"{spec.operation_prefix}:{operation_id}",
        str(operation_id),
    ):
        event = _event_by_source_identity(
            session,
            source_provider=spec.source_provider,
            source_record_id=source_record_id,
        )
        if event is not None and event.event_type == spec.event_type:
            return event
    return None


def _completed_marker_matches_result(
    session: Session,
    *,
    marker: WellnessEvent,
    result: WellnessEvent,
    spec: _NutritionOperationMarkerSpec,
    operation_id: uuid.UUID,
    operation_fingerprint: str | None,
) -> bool:
    if (
        marker.event_type != _NUTRITION_OPERATION_EVENT
        or marker.source_provider != _NUTRITION_OPERATION_PROVIDER
        or _operation_marker_spec(marker) != spec
        or _operation_id_for_marker(marker) != operation_id
        or marker.payload.get("operation_state") != "completed"
        or marker.payload.get("operation_fingerprint") != operation_fingerprint
        or marker.payload.get(RESULT_PAYLOAD_DIGEST_FIELD) != result_payload_digest(result.payload)
    ):
        return False
    return not _legacy_marker_has_transition_metadata(
        marker
    ) or _matching_transition_exists_for_marker(session, marker)


def _scrub_completed_nutrition_operation_markers(
    session: Session,
) -> None:
    markers = session.scalars(
        select(WellnessEvent).where(
            WellnessEvent.event_type == _NUTRITION_OPERATION_EVENT,
            WellnessEvent.source_provider == _NUTRITION_OPERATION_PROVIDER,
        )
    )
    for marker in markers:
        if _has_malformed_legacy_json_quarantine(marker):
            continue
        operation_state = marker.payload.get("operation_state")
        spec = _operation_marker_spec(marker)
        operation_id = _operation_id_for_marker(marker)
        has_transition_metadata = _legacy_marker_has_transition_metadata(marker)
        known_identity_invalid = spec is not None and operation_id is None
        if has_transition_metadata and (
            operation_state != "completed"
            or known_identity_invalid
            or not _matching_transition_exists_for_marker(session, marker)
        ):
            _set_maintenance_quarantine(
                marker,
                reason=_LEGACY_TRANSITION_QUARANTINE_REASON,
            )
            continue
        if operation_state != "completed":
            continue
        if known_identity_invalid:
            _set_maintenance_quarantine(
                marker,
                reason=_LEGACY_TRANSITION_QUARANTINE_REASON,
            )
            continue
        result = (
            _operation_result(
                session,
                spec=spec,
                operation_id=operation_id,
            )
            if spec is not None and operation_id is not None
            else None
        )
        if RESULT_PAYLOAD_DIGEST_FIELD in marker.payload:
            digest = marker.payload.get(RESULT_PAYLOAD_DIGEST_FIELD)
            if not is_sha256_digest(digest) or (
                result is not None and digest != result_payload_digest(result.payload)
            ):
                reason = (
                    _LEGACY_TRANSITION_QUARANTINE_REASON
                    if spec is not None
                    and spec.operation_kind
                    in {
                        "intake_interaction_review",
                        "intake_outcome",
                    }
                    else _LEGACY_RESULT_IDENTITY_QUARANTINE_REASON
                )
                _set_maintenance_quarantine(marker, reason=reason)
                if result is not None:
                    _set_maintenance_quarantine(
                        result,
                        reason=reason,
                    )
                continue
        elif result is not None:
            marker.payload = {
                **marker.payload,
                RESULT_PAYLOAD_DIGEST_FIELD: result_payload_digest(result.payload),
            }
        _set_maintenance_quarantine(
            marker,
            reason=None,
        )
        minimal = {field: marker.payload.get(field) for field in _COMPLETED_OPERATION_MARKER_FIELDS}
        normalized_operation_id = _operation_id_for_marker(marker)
        if normalized_operation_id is not None:
            minimal["operation_id"] = str(normalized_operation_id)
        digest = marker.payload.get(RESULT_PAYLOAD_DIGEST_FIELD)
        if is_sha256_digest(digest):
            minimal[RESULT_PAYLOAD_DIGEST_FIELD] = digest
        if marker.payload.get("legacy_backfill") is True:
            minimal["legacy_backfill"] = True
        if marker.payload != minimal:
            marker.payload = minimal


def _backfill_legacy_nutrition_operation_markers(
    session: Session,
    *,
    dry_run: bool,
) -> None:
    """Preserve operation identity before retained result events are purged."""

    if dry_run:
        return
    result_events = list(
        session.scalars(
            select(WellnessEvent)
            .where(
                WellnessEvent.event_type.in_(
                    spec.event_type for spec in _NUTRITION_OPERATION_MARKER_SPECS
                )
            )
            .order_by(
                WellnessEvent.recorded_at,
                WellnessEvent.created_at,
            )
        )
    )
    candidates: list[
        tuple[
            WellnessEvent,
            _NutritionOperationMarkerSpec,
            uuid.UUID,
        ]
    ] = []
    for event in result_events:
        if _has_malformed_legacy_json_quarantine(event):
            continue
        spec = _operation_result_spec(event)
        if spec is None:
            continue
        operation_id = _operation_id_for_result(event, spec)
        if operation_id is None:
            _set_maintenance_quarantine(
                event,
                reason=_LEGACY_RESULT_IDENTITY_QUARANTINE_REASON,
            )
            continue
        candidates.append((event, spec, operation_id))

    _backfill_legacy_interaction_transitions(session, candidates)
    safe_candidates = []
    for event, spec, operation_id in candidates:
        if spec.operation_kind in {
            "intake_interaction_review",
            "intake_outcome",
        }:
            details = _legacy_transition_details(
                event,
                spec,
                operation_id,
            )
            if details is None or not _matching_transition_exists(
                session,
                details,
            ):
                _set_maintenance_quarantine(
                    event,
                    reason=_LEGACY_TRANSITION_QUARANTINE_REASON,
                )
                continue
        safe_candidates.append((event, spec, operation_id))

    for event, spec, operation_id in safe_candidates:
        marker_id = f"{spec.operation_prefix}:{operation_id}"
        fingerprint = event.payload.get("operation_fingerprint")
        if not isinstance(fingerprint, str) or fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
            fingerprint = None
        marker = _persist_event_by_source_identity(
            session,
            WellnessEvent(
                event_type=_NUTRITION_OPERATION_EVENT,
                schema_version=1,
                observed_at=event.recorded_at,
                recorded_at=event.recorded_at,
                timezone=event.timezone,
                source_provider=_NUTRITION_OPERATION_PROVIDER,
                source_device=None,
                source_record_id=marker_id,
                capture_method="system",
                quality_flags=None,
                confidence=None,
                sensitivity="wellness",
                consent_scope="personal",
                retention_policy_id=None,
                expires_at=None,
                payload={
                    "operation_kind": spec.operation_kind,
                    "operation_id": str(operation_id),
                    "operation_fingerprint": fingerprint,
                    "operation_state": "completed",
                    RESULT_PAYLOAD_DIGEST_FIELD: result_payload_digest(event.payload),
                    "legacy_backfill": True,
                },
                derived_from=None,
            ),
        )
        if RESULT_PAYLOAD_DIGEST_FIELD not in marker.payload:
            marker.payload = {
                **marker.payload,
                RESULT_PAYLOAD_DIGEST_FIELD: result_payload_digest(event.payload),
            }
        if not _completed_marker_matches_result(
            session,
            marker=marker,
            result=event,
            spec=spec,
            operation_id=operation_id,
            operation_fingerprint=fingerprint,
        ):
            _set_maintenance_quarantine(
                event,
                reason=(
                    _LEGACY_TRANSITION_QUARANTINE_REASON
                    if spec.operation_kind
                    in {
                        "intake_interaction_review",
                        "intake_outcome",
                    }
                    else _LEGACY_RESULT_IDENTITY_QUARANTINE_REASON
                ),
            )
            _set_maintenance_quarantine(
                marker,
                reason=(
                    _LEGACY_TRANSITION_QUARANTINE_REASON
                    if spec.operation_kind
                    in {
                        "intake_interaction_review",
                        "intake_outcome",
                    }
                    else _LEGACY_RESULT_IDENTITY_QUARANTINE_REASON
                ),
            )
            continue
        _set_maintenance_quarantine(event, reason=None)


def _run_storage_maintenance(
    session: Session,
    settings: Settings,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> StorageMaintenanceReport:
    lock_nutrition_ledger(session)
    current = now or _now()
    _lock_all_retention_policies(session)
    _quarantine_malformed_legacy_json_documents(session)
    _migrate_legacy_nutrition_raw_captures(
        session,
        current=current,
        dry_run=dry_run,
    )
    _backfill_legacy_nutrition_operation_markers(
        session,
        dry_run=dry_run,
    )
    if not dry_run:
        _scrub_completed_nutrition_operation_markers(session)
        _ensure_quarantined_operation_tombstones(
            session,
            current=current,
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
                food_rows = session.scalars(
                    select(FoodLog).where(FoodLog.media_path == obj.relative_path)
                )
                for row in food_rows:
                    row.media_path = None
                medical_rows = session.scalars(
                    select(MedicalRecord).where(MedicalRecord.media_path == obj.relative_path)
                )
                for row in medical_rows:
                    row.media_path = None
            if obj.data_class == "raw_payload":
                raw = session.scalar(
                    select(RawIngestEvent).where(RawIngestEvent.path == obj.relative_path)
                )
                if raw is not None:
                    session.delete(raw)
            obj.purged_at = current
            deleted += 1
            reclaimed += obj.size_bytes
        except OSError as exc:
            errors.append(f"{obj.relative_path}: {exc}")
    if not dry_run:
        from healthmes.nutrition.confirmation_gate import (
            finalize_expired_nutrition_confirmations,
        )

        finalize_expired_nutrition_confirmations(
            session,
            now=current,
        )
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


def run_storage_maintenance(
    session: Session,
    settings: Settings,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> StorageMaintenanceReport:
    if not dry_run:
        return _run_storage_maintenance(
            session,
            settings,
            dry_run=False,
            now=now,
        )
    savepoint = session.begin_nested()
    try:
        report = _run_storage_maintenance(
            session,
            settings,
            dry_run=True,
            now=now,
        )
    finally:
        if savepoint.is_active:
            savepoint.rollback()
        session.expire_all()
    return report
