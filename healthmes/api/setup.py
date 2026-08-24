"""Apple-client setup readiness and one-time pairing grants.

This module is an adapter around existing HealthMes controls. It does not
replace the input registry, Decision Service, or Hermes runtime:

- ``GET /v1/setup/readiness`` summarizes those existing controls for the
  iPhone and macOS one-page setup surfaces.
- authenticated clients mint short-lived pairing grants without exposing the
  long-lived bearer token in a QR code.
- an unpaired client exchanges one grant exactly once for the server-owned
  base URL and bearer token.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import quote, urlsplit
from uuid import UUID

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from healthmes.api.errors import APIError
from healthmes.config import Settings, is_loopback_host
from healthmes.inputs import (
    InputConnectionState,
    InputSourceDescriptor,
    InputSourceRegistry,
)
from healthmes.pairing import (
    PairingGrantConsumed,
    PairingGrantCorrupt,
    PairingGrantExpired,
    PairingGrantStore,
    PairingGrantUnknown,
    is_loopback_pairing_origin,
    is_secure_pairing_origin,
)
from healthmes.store.session import SessionDep

router = APIRouter(prefix="/v1/setup", tags=["setup"])


class SetupReadinessCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    state: Literal["ready", "action_required", "blocked"]
    detail: str


class SetupReadinessOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overall: Literal["ready", "action_required", "blocked"]
    checks: list[SetupReadinessCheck]


class PairingGrantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audience: Literal["mac", "phone"]


class PairingGrantOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pairing_url: str
    base_url: str
    expires_at: int


class PairingExchangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=32, max_length=128)


class PairingExchangeOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    token: str


def _store(settings: Settings) -> PairingGrantStore:
    return PairingGrantStore(settings.data_dir)


def _approved_base_url(settings: Settings, audience: str) -> str:
    if audience == "mac":
        return f"http://127.0.0.1:{settings.port}"
    candidate = settings.public_base_url.rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme != "https" or not is_secure_pairing_origin(candidate):
        raise APIError(
            status.HTTP_409_CONFLICT,
            "secure_pairing_origin_required",
            (
                "iPhone pairing requires HEALTHMES_PUBLIC_BASE_URL to be a "
                "credential-free HTTPS origin reachable from the phone."
            ),
        )
    return candidate


def _pairing_url(base_url: str, code: str) -> str:
    return (
        f"healthmes://pair?url={quote(base_url, safe='')}"
        f"&code={quote(code, safe='')}"
    )


def _source_check(
    descriptors: dict[str, InputSourceDescriptor],
    *,
    source_id: str,
    key: str,
    label: str,
    ready_detail: str,
    action_detail: str,
    ready_states: tuple[InputConnectionState, ...] = (
        InputConnectionState.CONFIGURED,
        InputConnectionState.CONNECTED,
    ),
    prerequisites_ready: bool = True,
) -> SetupReadinessCheck:
    descriptor = descriptors.get(source_id)
    connected = (
        prerequisites_ready
        and descriptor is not None
        and descriptor.connection_state in ready_states
    )
    return SetupReadinessCheck(
        key=key,
        label=label,
        state="ready" if connected else "action_required",
        detail=ready_detail if connected else action_detail,
    )


def _overall(checks: list[SetupReadinessCheck]) -> str:
    required = [
        check
        for check in checks
        if check.key not in {"calendar_google", "calendar_icloud"}
    ]
    if any(check.state == "blocked" for check in required):
        return "blocked"
    if any(check.state == "action_required" for check in required):
        return "action_required"
    return "ready"


def _valid_open_wearables_user_id(value: str | None) -> bool:
    if value is None or not value.strip():
        return False
    try:
        UUID(value)
    except (AttributeError, ValueError):
        return False
    return True


@router.get(
    "/readiness",
    response_model=SetupReadinessOut,
)
def setup_readiness(
    request: Request,
    response: Response,
    session: SessionDep,
) -> SetupReadinessOut:
    settings: Settings = request.app.state.settings
    descriptors = {
        item.source_id: item
        for item in InputSourceRegistry(settings=settings).list(session)
    }
    api_token = settings.api_token.get_secret_value().strip()
    instance_state: Literal["ready", "action_required", "blocked"]
    if api_token:
        instance_state = "ready"
    elif is_loopback_host(settings.host):
        instance_state = "action_required"
    else:
        instance_state = "blocked"
    public_base_url = settings.public_base_url.rstrip("/")
    public_pairing_ready = bool(
        api_token
        and urlsplit(public_base_url).scheme == "https"
        and is_secure_pairing_origin(public_base_url)
    )
    open_wearables_ready = bool(
        settings.ow_api_key.get_secret_value().strip()
        and _valid_open_wearables_user_id(settings.ow_user_id)
    )
    decision_runtime_ready = (
        getattr(request.app.state, "decision_engine", None) is not None
    )
    checks = [
        SetupReadinessCheck(
            key="instance",
            label="HealthMes instance",
            state=instance_state,
            detail=(
                "The API is protected by the configured bearer token."
                if api_token
                else (
                    "Create HEALTHMES_API_TOKEN before pairing another device."
                    if instance_state == "action_required"
                    else "A network-reachable instance cannot run without an API token."
                )
            ),
        ),
        SetupReadinessCheck(
            key="public_pairing",
            label="iPhone secure connection",
            state="ready" if public_pairing_ready else "action_required",
            detail=(
                f"iPhone pairing uses {settings.public_base_url.rstrip('/')}."
                if public_pairing_ready
                else (
                    "Configure a reachable HTTPS HEALTHMES_PUBLIC_BASE_URL "
                    "before generating the iPhone QR."
                )
            ),
        ),
        _source_check(
            descriptors,
            source_id="wearable.healthkit-bridge",
            key="healthkit",
            label="Apple Health",
            ready_detail=(
                "The first-party iPhone HealthKit collector can upload to "
                "the native HealthMes ingest endpoint."
            ),
            action_detail="Enable the Apple Health input on the iPhone.",
            ready_states=(InputConnectionState.CONNECTED,),
        ),
        _source_check(
            descriptors,
            source_id="wearable.open-wearables",
            key="open_wearables",
            label="Wearable data service",
            ready_detail="Open Wearables is configured for normalized wearable data.",
            action_detail="Configure the Open Wearables API key and user.",
            prerequisites_ready=open_wearables_ready,
        ),
        _source_check(
            descriptors,
            source_id="calendar.google",
            key="calendar_google",
            label="Google Calendar",
            ready_detail="Google Calendar is connected.",
            action_detail="Optional: connect Google Calendar from Settings.",
        ),
        _source_check(
            descriptors,
            source_id="calendar.icloud",
            key="calendar_icloud",
            label="Apple Calendar",
            ready_detail="iCloud CalDAV is connected.",
            action_detail="Optional: connect iCloud Calendar from Settings.",
        ),
        SetupReadinessCheck(
            key="decision_runtime",
            label="Wellness Decision Agent",
            state="ready" if decision_runtime_ready else "action_required",
            detail=(
                "Natural-language wellness requests use the configured "
                "HealthMes Decision Service and isolated Hermes runtime."
                if decision_runtime_ready
                else (
                    "Configure the existing Main Decision/Hermes runtime to "
                    "enable natural-language wellness requests."
                )
            ),
        ),
    ]
    response.headers["Cache-Control"] = "no-store"
    return SetupReadinessOut(overall=_overall(checks), checks=checks)


@router.post(
    "/pairing/grants",
    response_model=PairingGrantOut,
    status_code=status.HTTP_201_CREATED,
)
def create_pairing_grant(
    body: PairingGrantCreate,
    request: Request,
    response: Response,
) -> PairingGrantOut:
    settings: Settings = request.app.state.settings
    if not settings.api_token.get_secret_value().strip():
        raise APIError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "pairing_token_unconfigured",
            "Configure HEALTHMES_API_TOKEN before issuing pairing grants.",
        )
    base_url = _approved_base_url(settings, body.audience)
    code, expires_at = _store(settings).issue(base_url)
    response.headers["Cache-Control"] = "no-store"
    return PairingGrantOut(
        pairing_url=_pairing_url(base_url, code),
        base_url=base_url,
        expires_at=int(expires_at.timestamp()),
    )


@router.post(
    "/pairing/exchange",
    response_model=PairingExchangeOut,
)
def exchange_pairing_grant(
    body: PairingExchangeIn,
    request: Request,
    response: Response,
) -> PairingExchangeOut:
    settings: Settings = request.app.state.settings
    token = settings.api_token.get_secret_value().strip()

    def validate_exchange(base_url: str) -> None:
        if not token and not is_loopback_pairing_origin(base_url):
            raise APIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "pairing_token_unconfigured",
                "The HealthMes API token is no longer configured.",
            )

    try:
        base_url = _store(settings).consume(
            body.code,
            validate_base_url=validate_exchange,
        )
    except PairingGrantConsumed as exc:
        raise APIError(
            status.HTTP_409_CONFLICT,
            "pairing_code_consumed",
            "The pairing code was already used.",
        ) from exc
    except PairingGrantExpired as exc:
        raise APIError(
            status.HTTP_410_GONE,
            "pairing_code_expired",
            "The pairing code expired.",
        ) from exc
    except PairingGrantUnknown as exc:
        raise APIError(
            status.HTTP_404_NOT_FOUND,
            "pairing_code_not_found",
            "The pairing code is unknown.",
        ) from exc
    except PairingGrantCorrupt as exc:
        raise APIError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "pairing_grant_unavailable",
            "The pairing grant could not be verified safely.",
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return PairingExchangeOut(base_url=base_url, token=token)
