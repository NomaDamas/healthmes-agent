"""One-click setup pairing exchange and component readiness."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from healthmes.api.connection_status import build_connection_cards, build_oura_card
from healthmes.api.errors import APIError
from healthmes.config import Settings
from healthmes.pairing import (
    PairingGrantConsumed,
    PairingGrantError,
    PairingGrantExpired,
    exchange_pairing_grant,
)
from healthmes.storage import measure_usage
from healthmes.store import RawIngestEvent
from healthmes.store.session import SessionDep

router = APIRouter(prefix="/v1/setup", tags=["setup"])


class PairingExchangeIn(BaseModel):
    code: str = Field(min_length=32, max_length=2048)


class PairingExchangeOut(BaseModel):
    base_url: str
    token: str
    expires_in: int = 0


class ReadinessCheck(BaseModel):
    key: str
    label: str
    state: Literal["ready", "action_required", "blocked"]
    detail: str


class SetupReadinessOut(BaseModel):
    overall: Literal["ready", "action_required"]
    checks: list[ReadinessCheck]


@router.post("/pairing/exchange")
def exchange_pairing(
    body: PairingExchangeIn,
    request: Request,
) -> PairingExchangeOut:
    settings: Settings = request.app.state.settings
    try:
        token = exchange_pairing_grant(settings, body.code)
    except PairingGrantExpired as exc:
        raise APIError(410, "pairing_expired", str(exc)) from exc
    except PairingGrantConsumed as exc:
        raise APIError(409, "pairing_consumed", str(exc)) from exc
    except PairingGrantError as exc:
        raise APIError(400, "invalid_pairing_code", str(exc)) from exc
    return PairingExchangeOut(
        base_url=settings.public_base_url.rstrip("/"),
        token=token,
    )


@router.get("/readiness")
async def setup_readiness(
    request: Request,
    session: SessionDep,
) -> SetupReadinessOut:
    settings: Settings = request.app.state.settings
    calendar_cards = build_connection_cards(settings)
    wearable = await build_oura_card(settings)
    usage = measure_usage(session, settings)
    latest_healthkit = session.scalar(
        select(RawIngestEvent)
        .where(RawIngestEvent.source == "healthkit-bridge")
        .order_by(RawIngestEvent.received_at.desc())
        .limit(1)
    )
    public_url = urlsplit(settings.public_base_url)
    health_ready = wearable.connected or latest_healthkit is not None
    checks = [
        ReadinessCheck(
            key="instance",
            label="HealthMes instance",
            state="ready",
            detail="Authenticated API is reachable.",
        ),
        ReadinessCheck(
            key="health",
            label="Health data",
            state="ready" if health_ready else "action_required",
            detail=(
                f"iPhone HealthKit received · {latest_healthkit.received_at.isoformat()}"
                if latest_healthkit is not None
                else wearable.detail
            ),
        ),
        ReadinessCheck(
            key="healthkit_ingest",
            label="iPhone HealthKit upload",
            state="ready" if latest_healthkit is not None else "action_required",
            detail=(
                f"Last raw upload {latest_healthkit.received_at.isoformat()}"
                if latest_healthkit is not None
                else "Pair iPhone, grant Apple Health access, and run the first sync."
            ),
        ),
        *[
            ReadinessCheck(
                key=f"calendar_{card.key}",
                label=card.label,
                state="ready" if card.connected else "action_required",
                detail=card.detail,
            )
            for card in calendar_cards
        ],
        ReadinessCheck(
            key="notifications",
            label="Decision notifications",
            state="ready" if settings.native_alert_delivery else "action_required",
            detail=(
                "Native alert feed is enabled; iOS delivery remains best-effort polling."
                if settings.native_alert_delivery
                else "Enable HEALTHMES_NATIVE_ALERT_DELIVERY for iPhone and Watch alerts."
            ),
        ),
        ReadinessCheck(
            key="scheduler",
            label="Wellness scheduler",
            state="ready" if settings.scheduler_enabled else "action_required",
            detail=(
                "Trigger, calendar, and outcome jobs are enabled."
                if settings.scheduler_enabled
                else "Enable HEALTHMES_SCHEDULER_ENABLED."
            ),
        ),
        ReadinessCheck(
            key="public_https",
            label="Phone and Watch access",
            state=(
                "ready"
                if public_url.scheme == "https" and bool(public_url.hostname)
                else "action_required"
            ),
            detail=(
                settings.public_base_url
                if public_url.scheme == "https" and public_url.hostname
                else "Configure an HTTPS URL owned by this personal server."
            ),
        ),
        ReadinessCheck(
            key="storage",
            label="Local storage",
            state="ready",
            detail=f"{sum(row.get('bytes', 0) for row in usage.values())} bytes in use.",
        ),
    ]
    return SetupReadinessOut(
        overall=(
            "ready"
            if all(check.state == "ready" for check in checks)
            else "action_required"
        ),
        checks=checks,
    )
