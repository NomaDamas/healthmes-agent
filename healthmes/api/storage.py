"""Storage settings, usage, maintenance, and common wellness-event API."""

from __future__ import annotations

import shutil
import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from healthmes.api.decision_html import shell_context, template_environment
from healthmes.api.local_session import issue_local_session, require_local_session
from healthmes.backup.snapshot import (
    SNAPSHOT_SUFFIX,
    resolve_backup_dir,
    resolve_backup_provider_name,
)
from healthmes.config import Settings
from healthmes.storage import (
    RETENTION_PRESETS,
    ensure_default_policies,
    measure_usage,
    run_storage_maintenance,
    update_retention_policy,
)
from healthmes.store import RetentionPolicy, WellnessEvent
from healthmes.store.session import SessionDep

router = APIRouter(tags=["storage"])


def _preset(days: int | None) -> str:
    return next(key for key, value in RETENTION_PRESETS.items() if value == days)


class RetentionPolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    data_class: str
    preset: str
    retention_days: int | None
    enabled: bool


class StorageSettingsOut(BaseModel):
    data_dir: str
    disk_total_bytes: int
    disk_free_bytes: int
    usage: dict[str, dict[str, int]]
    policies: list[RetentionPolicyOut]
    backup: dict[str, Any]


class RetentionUpdate(BaseModel):
    preset: str = Field(pattern=r"^(1d|7d|14d|30d|90d|forever)$")


class WellnessEventCreate(BaseModel):
    event_type: str = Field(min_length=1, max_length=64)
    observed_at: datetime
    recorded_at: datetime | None = None
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


def _settings_payload(session: SessionDep, settings: Settings) -> StorageSettingsOut:
    policies = ensure_default_policies(session)
    usage = measure_usage(session, settings)
    session.commit()
    disk = shutil.disk_usage(settings.data_dir.resolve().parent)
    backup_dir = resolve_backup_dir(settings)
    snapshots = sorted(backup_dir.glob(f"*{SNAPSHOT_SUFFIX}"), reverse=True)
    return StorageSettingsOut(
        data_dir=str(settings.data_dir.resolve()),
        disk_total_bytes=disk.total,
        disk_free_bytes=disk.free,
        usage=usage,
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
    report = run_storage_maintenance(
        session, request.app.state.settings, dry_run=dry_run
    )
    session.commit()
    return {
        "job_id": report.job_id,
        "dry_run": report.dry_run,
        "candidates": report.candidates,
        "deleted": report.deleted,
        "bytes_reclaimed": report.bytes_reclaimed,
        "errors": list(report.errors),
    }


@router.post("/v1/wellness-events", status_code=201)
def create_wellness_event(
    body: WellnessEventCreate, session: SessionDep
) -> WellnessEventOut:
    existing = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.source_provider == body.source_provider,
            WellnessEvent.source_record_id == body.source_record_id,
        )
    )
    if existing is not None:
        return WellnessEventOut.model_validate(existing)
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
        derived_from=body.derived_from,
    )
    session.add(event)
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
    update_retention_policy(session, data_class, preset)
    session.commit()
    return RedirectResponse("/storage", status_code=303)


@router.post("/storage/maintenance", include_in_schema=False)
async def storage_maintenance_form(
    request: Request,
    session: SessionDep,
    csrf: str = Form(),
    dry_run: bool = Form(default=False),
) -> RedirectResponse:
    require_local_session(request, csrf_token=csrf)
    run_storage_maintenance(
        session, request.app.state.settings, dry_run=dry_run
    )
    session.commit()
    return RedirectResponse("/storage", status_code=303)
