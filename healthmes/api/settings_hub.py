"""Single read model for the native iPhone and macOS Settings surfaces.

The hub is intentionally an adapter. It composes the existing input registry,
setup-readiness checks, and Open Wearables management projection without
changing the Decision Service, Hermes runtime, MCP tools, or storage model.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from healthmes.api.setup import (
    SetupReadinessOut,
    build_setup_readiness,
    resolve_management_client,
)
from healthmes.api.wearables import (
    WearablesOut,
    _hub_provider_projection,
    _unconfigured_snapshot,
)
from healthmes.api.wearables import _snapshot as snapshot_wearables
from healthmes.config import Settings
from healthmes.inputs import (
    InputSourceDescriptor,
    InputSourceRegistry,
)
from healthmes.store.session import SessionDep
from healthmes.wearables.management import (
    WearableManagementAuthError,
    WearableManagementConfigurationError,
    WearableManagementError,
    WearableManagementNotFoundError,
    WearableManagementPayloadError,
    WearableManagementUpstreamError,
)

router = APIRouter(prefix="/v1/settings", tags=["settings"])


class SettingsHubOut(BaseModel):
    """Secret-free aggregate state shared by every native settings client."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: Literal["healthmes.settings-hub.v1"] = Field(
        default="healthmes.settings-hub.v1",
        alias="schema",
    )
    generated_at: datetime
    readiness: SetupReadinessOut
    inputs: list[InputSourceDescriptor]
    wearables: SettingsHubWearablesOut | None
    wearables_state: Literal["ready", "not_configured", "degraded"]
    wearables_error: str | None = None


class SettingsHubWearableControlOut(BaseModel):
    """Server-authoritative actions for one wearable provider."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    connection_status: str
    last_synced_at: str | None = None
    can_authorize: bool = False
    can_sync: bool = False
    can_historical_sync: bool = False
    can_disconnect: bool = False
    historical_limit: int
    default_historical_days: int


class SettingsHubWearablesOut(WearablesOut):
    controls: list[SettingsHubWearableControlOut] = Field(
        default_factory=list
    )


SettingsHubOut.model_rebuild()


def _wearables_error_message(exc: Exception) -> str:
    """Return a stable, non-sensitive message for a degraded snapshot."""

    if isinstance(exc, WearableManagementConfigurationError):
        return "Open Wearables is not configured on the HealthMes server."
    if isinstance(exc, WearableManagementAuthError):
        return "Open Wearables rejected the server-managed credential."
    if isinstance(exc, WearableManagementNotFoundError):
        return "Open Wearables does not expose the required management route."
    if isinstance(exc, WearableManagementPayloadError):
        return "Open Wearables returned an invalid management response."
    if isinstance(exc, WearableManagementUpstreamError):
        return "Open Wearables is temporarily unavailable."
    if isinstance(exc, WearableManagementError):
        return "Open Wearables could not complete the request."
    return "Wearable status is temporarily unavailable."


async def _wearables_projection(
    settings: Settings,
    request: Request,
    *,
    management_client: Any | None = None,
    management_error: Exception | None = None,
) -> tuple[
    SettingsHubWearablesOut | None,
    Literal["ready", "not_configured", "degraded"],
    str | None,
]:
    if not settings.ow_api_key.get_secret_value().strip():
        snapshot = _unconfigured_snapshot()
        return SettingsHubWearablesOut(
            **snapshot.model_dump(),
            controls=[
                SettingsHubWearableControlOut(
                    provider=provider.provider,
                    connection_status="not_configured",
                    historical_limit=min(
                        provider.max_historical_days or 365,
                        365,
                    ),
                    default_historical_days=min(
                        provider.max_historical_days or 365,
                        90,
                    ),
                )
                for provider in snapshot.providers
            ],
        ), "not_configured", None

    if management_error is not None:
        return None, "degraded", _wearables_error_message(management_error)
    if management_client is None:
        return None, "degraded", "Wearable status is temporarily unavailable."

    try:
        snapshot = await snapshot_wearables(management_client)
    except Exception as exc:
        return None, "degraded", _wearables_error_message(exc)
    connections = {
        item.provider: item
        for item in snapshot.connections
    }
    data_source_providers = {
        item.provider for item in snapshot.data_sources
    }
    controls: list[SettingsHubWearableControlOut] = []
    for provider in snapshot.providers:
        projection = _hub_provider_projection(
            provider,
            connections.get(provider.provider),
            native_source_connected=(
                provider.provider in data_source_providers
            ),
        )
        controls.append(
            SettingsHubWearableControlOut.model_validate(
                {
                    key: value
                    for key, value in projection.items()
                    if key
                    in SettingsHubWearableControlOut.model_fields
                }
            )
        )
    return SettingsHubWearablesOut(
        **snapshot.model_dump(),
        controls=controls,
    ), (
        "degraded"
        if snapshot.degraded_components
        else "ready"
    ), (
        "Some wearable status details are temporarily unavailable."
        if snapshot.degraded_components
        else None
    )


@router.get("/hub", response_model=SettingsHubOut)
async def get_settings_hub(
    request: Request,
    response: Response,
    session: SessionDep,
) -> SettingsHubOut:
    """Return one server-backed settings snapshot for iPhone and macOS."""

    settings: Settings = request.app.state.settings
    inputs = list(InputSourceRegistry(settings=settings).list(session))
    management_client, management_error = await resolve_management_client(request)
    readiness = await build_setup_readiness(
        request,
        inputs,
        management_client=management_client,
        management_error=management_error,
    )
    wearables, wearables_state, wearables_error = await _wearables_projection(
        settings,
        request,
        management_client=management_client,
        management_error=management_error,
    )
    response.headers["Cache-Control"] = "no-store"
    return SettingsHubOut(
        generated_at=datetime.now(UTC),
        readiness=readiness,
        inputs=inputs,
        wearables=wearables,
        wearables_state=wearables_state,
        wearables_error=wearables_error,
    )


__all__ = [
    "SettingsHubOut",
    "SettingsHubWearableControlOut",
    "SettingsHubWearablesOut",
    "get_settings_hub",
    "router",
]
