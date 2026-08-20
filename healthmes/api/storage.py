"""Storage settings, usage, maintenance, and common wellness-event API."""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from typing import Any, Literal

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from healthmes import clock
from healthmes.activity.contracts import is_reserved_activity_provider
from healthmes.activity.locking import activity_write_lock
from healthmes.api.decision_html import shell_context, template_environment
from healthmes.api.errors import APIError
from healthmes.api.local_session import issue_local_session, require_local_session
from healthmes.backup.snapshot import (
    RECOVERY_SCOPE_PARTIAL_COMPONENT,
    SNAPSHOT_SUFFIX,
    partial_backup_warning,
    resolve_backup_dir,
    resolve_backup_provider_name,
    resolve_data_locations,
)
from healthmes.config import Settings
from healthmes.storage import (
    RETENTION_PRESETS,
    ensure_default_policies,
    load_latest_usage_snapshot,
    measure_usage,
    run_storage_maintenance,
    update_retention_policy,
)
from healthmes.store import RetentionPolicy, WellnessEvent
from healthmes.store.session import SessionDep

router = APIRouter(tags=["storage"])
UsageDeferredReason = Literal["timeout", "permission", "io_error"]


def _preset(days: int | None) -> str:
    return next(key for key, value in RETENTION_PRESETS.items() if value == days)


def _next_snapshot_scope(settings: Settings) -> dict[str, Any]:
    """Describe the next attempted snapshot, never infer encrypted history."""
    locations = resolve_data_locations(settings)
    statuses = {
        "healthmes_db": "required",
        "open_wearables_db": (
            "included"
            if locations.ow_database_url
            else (
                "omitted_missing_dump_url"
                if locations.ow_runtime_configured
                else "not_configured"
            )
        ),
        "media": (
            "included"
            if locations.media_dir is not None and locations.media_dir.is_dir()
            else "source_not_present"
        ),
        "raw_ingest": (
            "included"
            if (
                locations.raw_ingest_dir is not None
                and locations.raw_ingest_dir.is_dir()
            )
            else "source_not_present"
        ),
        "hermes_home": (
            "included"
            if locations.hermes_home is not None and locations.hermes_home.is_dir()
            else (
                "not_configured"
                if locations.hermes_home is None
                else "source_not_present"
            )
        ),
    }
    included = [
        component
        for component, status in statuses.items()
        if status in {"required", "included"}
    ]
    omitted = [
        component
        for component, status in statuses.items()
        if status not in {"required", "included"}
    ]
    return {
        "basis": "current_configuration_and_source_presence",
        "describes_latest_snapshot": False,
        "components": statuses,
        "included_components": included,
        "omitted_components": omitted,
    }


class RetentionPolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    data_class: str
    preset: str
    retention_days: int | None
    enabled: bool


class UsageMeasurementOut(BaseModel):
    status: Literal["current", "stale", "unavailable"]
    provider: str
    measured_on: date | None
    deferred_reason: UsageDeferredReason | None = None


class StorageSettingsOut(BaseModel):
    data_dir: str
    disk_total_bytes: int
    disk_free_bytes: int
    usage: dict[str, dict[str, int]]
    usage_measurement: UsageMeasurementOut
    policies: list[RetentionPolicyOut]
    backup: dict[str, Any]


class RetentionUpdate(BaseModel):
    preset: str = Field(pattern=r"^(1d|7d|14d|30d|90d|forever)$")


class WellnessEventCreate(BaseModel):
    event_type: str = Field(min_length=1, max_length=64)
    observed_at: AwareDatetime
    recorded_at: AwareDatetime | None = None
    timezone: str | None = None
    source_provider: str = Field(min_length=1, max_length=64)
    source_device: str | None = None
    source_record_id: str = Field(min_length=1, max_length=255)
    capture_method: str = "import"
    quality_flags: dict[str, Any] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    coverage: float | None = Field(default=None, ge=0, le=1)
    sensitivity: str = "wellness"
    consent_scope: str = "personal"
    data_class: str = "normalized"
    payload: dict[str, Any]
    derived_from: dict[str, Any] | None = None


class WellnessEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    event_type: str
    observed_at: datetime
    recorded_at: datetime
    source_provider: str
    source_record_id: str
    expires_at: datetime | None
    payload: dict[str, Any]


def _usage_deferred_reason(exc: OSError | TimeoutError) -> UsageDeferredReason:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, PermissionError):
        return "permission"
    return "io_error"


def _settings_payload(session: SessionDep, settings: Settings) -> StorageSettingsOut:
    policies = ensure_default_policies(session)
    try:
        usage = measure_usage(session, settings)
    except (TimeoutError, PermissionError, OSError) as exc:
        session.rollback()
        policies = ensure_default_policies(session)
        snapshot = load_latest_usage_snapshot(session)
        usage = snapshot.usage
        usage_measurement = UsageMeasurementOut(
            status=(
                "stale"
                if snapshot.measured_on is not None
                else "unavailable"
            ),
            provider=snapshot.provider,
            measured_on=snapshot.measured_on,
            deferred_reason=_usage_deferred_reason(exc),
        )
    else:
        usage_measurement = UsageMeasurementOut(
            status="current",
            provider="local",
            measured_on=date.today(),
        )
    session.commit()
    disk = shutil.disk_usage(settings.data_dir.resolve().parent)
    backup_dir = resolve_backup_dir(settings)
    snapshots = sorted(backup_dir.glob(f"*{SNAPSHOT_SUFFIX}"), reverse=True)
    backup_locations = resolve_data_locations(settings)
    return StorageSettingsOut(
        data_dir=str(settings.data_dir.resolve()),
        disk_total_bytes=disk.total,
        disk_free_bytes=disk.free,
        usage=usage,
        usage_measurement=usage_measurement,
        policies=[
            RetentionPolicyOut(
                data_class=row.data_class,
                preset=_preset(row.retention_days),
                retention_days=row.retention_days,
                enabled=row.enabled,
            )
            for row in policies
        ],
        backup={
            "provider": resolve_backup_provider_name(settings),
            "directory": str(backup_dir.resolve()),
            "snapshot_count": len(snapshots),
            "latest_snapshot": snapshots[0].name if snapshots else None,
            "encryption_configured": bool(
                settings.backup_passphrase.get_secret_value()
            ),
            "recovery_scope": RECOVERY_SCOPE_PARTIAL_COMPONENT,
            "full_node_recovery": False,
            "open_wearables_runtime_configured": (
                backup_locations.ow_runtime_configured
            ),
            "open_wearables_dump_configured": bool(
                backup_locations.ow_database_url
            ),
            "operational_warning": partial_backup_warning(backup_locations),
            "next_snapshot_scope": _next_snapshot_scope(settings),
        },
    )


@router.get("/v1/storage/settings")
def get_storage_settings(request: Request, session: SessionDep) -> StorageSettingsOut:
    return _settings_payload(session, request.app.state.settings)


@router.put("/v1/storage/settings/{data_class}")
def put_retention_policy(
    data_class: str,
    body: RetentionUpdate,
    session: SessionDep,
) -> RetentionPolicyOut:
    with activity_write_lock():
        policy = update_retention_policy(session, data_class, body.preset)
        session.commit()
        return RetentionPolicyOut(
            data_class=policy.data_class,
            preset=_preset(policy.retention_days),
            retention_days=policy.retention_days,
            enabled=policy.enabled,
        )


@router.post("/v1/storage/maintenance")
def maintain_storage(
    request: Request, session: SessionDep, dry_run: bool = False
) -> dict[str, Any]:
    settings = request.app.state.settings
    report = run_storage_maintenance(
        session, settings, dry_run=dry_run
    )
    usage_errors: list[str] = []
    if not dry_run:
        try:
            measure_usage(session, settings)
            session.commit()
        except (OSError, TimeoutError) as exc:
            session.rollback()
            usage_errors.append(
                "storage usage measurement was deferred: "
                f"{_usage_deferred_reason(exc)}"
            )
    return {
        "job_id": report.job_id,
        "dry_run": report.dry_run,
        "candidates": report.candidates,
        "records_purged": report.records_purged,
        "files_deleted": report.files_deleted,
        "file_cleanup_pending": report.file_cleanup_pending,
        "deleted": report.deleted,
        "bytes_reclaimed": report.bytes_reclaimed,
        "decision_candidates": report.decision_candidates,
        "decisions_deleted": report.decisions_deleted,
        "decision_receipt_candidates": report.decision_receipt_candidates,
        "decision_receipts_deleted": report.decision_receipts_deleted,
        "budget_exhausted": report.budget_exhausted,
        "budget_resource": report.budget_resource,
        "budget_phase": report.budget_phase,
        "errors": [*report.errors, *usage_errors],
    }


@router.post("/v1/wellness-events", status_code=201)
def create_wellness_event(
    body: WellnessEventCreate, session: SessionDep
) -> WellnessEventOut:
    event_type = body.event_type.casefold()
    source_provider = body.source_provider.casefold()
    if (
        event_type.startswith("nutrition.")
        or event_type.startswith("activity.")
        or source_provider.startswith("nutrition-")
        or is_reserved_activity_provider(body.source_provider)
        or source_provider
        in {
            "sake-vlm",
            "sake-vlm-raw",
            "user-confirmation",
            "user-nutrition-review",
        }
    ):
        raise APIError(
            422,
            "reserved_wellness_namespace",
            "internal nutrition and activity event namespaces are reserved",
        )
    if body.data_class != "normalized":
        raise APIError(
            422,
            "unsupported_wellness_data_class",
            "external wellness events must use the normalized data class",
        )
    if body.observed_at.astimezone(UTC) > clock.utc_now() + timedelta(
        minutes=5
    ):
        raise APIError(
            422,
            "invalid_observed_at",
            "observed_at cannot be more than 5 minutes in the future",
        )
    fingerprint = sha256(
        json.dumps(
            body.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    def validate_existing(event: WellnessEvent) -> WellnessEventOut:
        if (
            event.expires_at is not None
            and (
                event.expires_at.replace(tzinfo=UTC)
                if event.expires_at.tzinfo is None
                else event.expires_at.astimezone(UTC)
            )
            <= clock.utc_now()
        ):
            raise APIError(
                409,
                "expired_wellness_event",
                "expired wellness events cannot be retried",
            )
        if (
            not isinstance(event.derived_from, dict)
            or event.derived_from.get("_ingest_fingerprint") != fingerprint
        ):
            raise APIError(
                409,
                "wellness_event_conflict",
                "source key was already used with different input",
            )
        return WellnessEventOut.model_validate(event)

    existing = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.source_provider == body.source_provider,
            WellnessEvent.source_record_id == body.source_record_id,
        )
    )
    if existing is not None:
        return validate_existing(existing)
    ensure_default_policies(session)
    policy = session.scalar(
        select(RetentionPolicy).where(RetentionPolicy.data_class == body.data_class)
    )
    if policy is None:
        policy = update_retention_policy(session, body.data_class, "30d")
    expires_at = (
        None
        if policy.retention_days is None
        else body.observed_at + timedelta(days=policy.retention_days)
    )
    if expires_at is not None and expires_at.astimezone(UTC) <= datetime.now(
        UTC
    ):
        raise APIError(
            422,
            "expired_wellness_event",
            "observed_at falls outside the retention window",
        )
    event = WellnessEvent(
        event_type=body.event_type,
        observed_at=body.observed_at,
        recorded_at=body.recorded_at or body.observed_at,
        timezone=body.timezone,
        source_provider=body.source_provider,
        source_device=body.source_device,
        source_record_id=body.source_record_id,
        capture_method=body.capture_method,
        quality_flags=body.quality_flags,
        confidence=body.confidence,
        coverage=body.coverage,
        sensitivity=body.sensitivity,
        consent_scope=body.consent_scope,
        retention_policy_id=policy.id,
        expires_at=expires_at,
        payload=body.payload,
        derived_from={
            **(body.derived_from or {}),
            "_ingest_fingerprint": fingerprint,
        },
    )
    try:
        with session.begin_nested():
            session.add(event)
            session.flush()
    except IntegrityError:
        existing = session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.source_provider == body.source_provider,
                WellnessEvent.source_record_id == body.source_record_id,
            )
        )
        if existing is None:
            raise
        return validate_existing(existing)
    session.commit()
    session.refresh(event)
    return WellnessEventOut.model_validate(event)


@router.get("/storage", response_class=HTMLResponse, include_in_schema=False)
def storage_page(
    request: Request, response: Response, session: SessionDep
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    local = issue_local_session(request, response)
    payload = _settings_payload(session, settings)
    template = template_environment().get_template("ui/storage.html.j2")
    html = template.render(
        storage=payload,
        presets=RETENTION_PRESETS,
        local_session=local,
        active_nav="storage",
        **shell_context(settings),
    )
    return HTMLResponse(html, headers=response.headers)


@router.post("/storage/policy", include_in_schema=False)
async def storage_policy_form(
    request: Request,
    session: SessionDep,
    data_class: str = Form(),
    preset: str = Form(),
    csrf: str = Form(),
) -> RedirectResponse:
    require_local_session(request, csrf_token=csrf)
    with activity_write_lock():
        update_retention_policy(session, data_class, preset)
        session.commit()
    return RedirectResponse("/storage", status_code=303)


@router.post("/storage/maintenance", include_in_schema=False)
def storage_maintenance_form(
    request: Request,
    session: SessionDep,
    csrf: str = Form(),
    dry_run: bool = Form(default=False),
) -> RedirectResponse:
    require_local_session(request, csrf_token=csrf)
    run_storage_maintenance(
        session, request.app.state.settings, dry_run=dry_run
    )
    return RedirectResponse("/storage", status_code=303)
