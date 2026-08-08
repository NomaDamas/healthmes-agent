"""One-click setup pairing exchange and component readiness."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

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
            state="ready" if wearable.connected else "action_required",
            detail=wearable.detail,
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
            state="ready",
            detail="Native apps can poll the authenticated alert feed.",
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
