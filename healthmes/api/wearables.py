"""Settings Hub API for provider and wearable-device management.

This is an adapter over Open Wearables. It does not change the HealthMes
Decision Service, Hermes, MCP, or the normalized wearable storage path.
Clients receive a safe projection of provider state and may never supply an
Open Wearables user id, API key, OAuth token, or arbitrary OAuth return URL.
"""

from __future__ import annotations

import asyncio
import hmac
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from healthmes.api.auth import viewer_token
from healthmes.api.errors import APIError
from healthmes.api.local_session import require_local_session
from healthmes.config import Settings
from healthmes.wearables.management import (
    MANAGEMENT_SUPPORTED_PROVIDERS,
    SUPPORTED_PROVIDER_ORDER,
    OpenWearablesManagementClient,
    WearableManagementAuthError,
    WearableManagementCapabilityError,
    WearableManagementConfigurationError,
    WearableManagementConnectionError,
    WearableManagementError,
    WearableManagementNotFoundError,
    WearableManagementPayloadError,
    WearableManagementRejectedError,
    WearableManagementUpstreamError,
    build_management_client,
    normalize_management_provider_identifier,
    normalize_provider_identifier,
)

router = APIRouter(prefix="/v1/wearables", tags=["wearables"])


class WearableProviderOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    name: str
    has_cloud_api: bool
    is_enabled: bool
    catalog_available: bool = True
    management_supported: bool = False
    icon_url: str | None = None
    live_sync_mode: str | None = None
    live_sync_configurable: bool
    client_sdk: bool = False
    file_import: bool = False
    rest_pull: bool = False
    webhook_stream: bool = False
    webhook_ping: bool = False
    webhook_callback: bool = False
    webhook_registration_api: bool = False
    webhook_inbound_secret: bool = False
    max_historical_days: int | None = None


class WearableConnectionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    status: str
    last_synced_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    max_historical_days: int | None = None
    rest_pull: bool = False
    webhook_stream: bool = False
    webhook_ping: bool = False
    webhook_callback: bool = False
    webhook_registration_api: bool = False
    webhook_inbound_secret: bool = False
    live_sync_mode: str | None = None


class WearableDataSourceOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    provider: str
    display_name: str | None = None
    device_model: str | None = None
    software_version: str | None = None
    device_type: str | None = None
    source: str | None = None
    original_source_name: str | None = None


class WearableSyncOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    success: bool
    async_: bool | None = Field(default=None, alias="async")
    task_id: str | None = None
    method: str | None = None
    message: str | None = None
    days: int | None = None
    start_date: str | None = None
    end_date: str | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class WearableSyncStatusOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    provider: str
    source: str
    stage: str
    status: str
    message: str | None = None
    progress: float | None = None
    items_processed: int | None = None
    items_total: int | None = None
    error: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    last_update: str | None = None


class WearablesOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_configured: bool
    user_configured: bool
    providers: list[WearableProviderOut]
    connections: list[WearableConnectionOut]
    data_sources: list[WearableDataSourceOut]
    coverage: dict[str, Any] | None = None
    recent_sync: list[WearableSyncStatusOut]
    sync_runs: list[WearableSyncStatusOut]
    degraded_components: list[str] = Field(default_factory=list)


_FALLBACK_PROVIDER_NAMES = {
    "apple": "Apple Health",
    "samsung": "Samsung Health",
    "google": "Google Health Connect",
    "garmin": "Garmin",
    "polar": "Polar",
    "suunto": "Suunto",
    "whoop": "WHOOP",
    "strava": "Strava",
    "oura": "Oura",
    "fitbit": "Fitbit",
    "ultrahuman": "Ultrahuman",
}
_CONNECTION_CAPABILITY_FIELDS = (
    "live_sync_mode",
    "rest_pull",
    "webhook_stream",
    "webhook_ping",
    "webhook_callback",
    "webhook_registration_api",
    "webhook_inbound_secret",
    "max_historical_days",
)


class WearableAuthorizationOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    authorization_url: str


class HistoricalSyncIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: int = Field(default=90, ge=1, le=365)


class WearableMutationOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    operation: str
    success: bool = True
    async_: bool | None = Field(default=None, alias="async")
    task_id: str | None = None
    method: str | None = None
    message: str | None = None
    days: int | None = None
    start_date: str | None = None
    end_date: str | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _client_from_request(request: Request) -> OpenWearablesManagementClient:
    client = getattr(request.app.state, "wearable_management_client", None)
    if client is None:
        client = getattr(request.app.state, "ow_management_client", None)
    if client is None:
        raise RuntimeError("management client must be built asynchronously")
    return client


async def get_management_client(
    request: Request,
) -> OpenWearablesManagementClient:
    injected = getattr(request.app.state, "wearable_management_client", None)
    if injected is not None:
        return injected
    injected = getattr(request.app.state, "ow_management_client", None)
    if injected is not None:
        return injected
    settings: Settings = request.app.state.settings
    return await build_management_client(settings)


def _map_management_error(exc: Exception) -> APIError:
    if isinstance(exc, WearableManagementConfigurationError):
        return APIError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "wearables_not_configured",
            "Wearable service is not configured on the HealthMes server.",
        )
    if isinstance(exc, WearableManagementAuthError):
        return APIError(
            status.HTTP_502_BAD_GATEWAY,
            "wearables_upstream_auth_failed",
            "Open Wearables rejected the server-managed credential.",
        )
    if isinstance(exc, WearableManagementNotFoundError):
        return APIError(
            status.HTTP_502_BAD_GATEWAY,
            "wearables_upstream_route_missing",
            "Open Wearables does not expose the required management route.",
        )
    if isinstance(exc, WearableManagementPayloadError):
        return APIError(
            status.HTTP_502_BAD_GATEWAY,
            "wearables_upstream_contract_invalid",
            "Open Wearables returned an invalid management response.",
        )
    if isinstance(exc, WearableManagementUpstreamError):
        return APIError(
            status.HTTP_502_BAD_GATEWAY,
            "wearables_upstream_unavailable",
            "Open Wearables is unavailable.",
        )
    if isinstance(exc, WearableManagementCapabilityError):
        return APIError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "wearables_capability_unsupported",
            "This wearable operation is not supported.",
        )
    if isinstance(exc, WearableManagementConnectionError):
        return APIError(
            status.HTTP_409_CONFLICT,
            "wearables_connection_not_ready",
            "This wearable connection is not ready.",
        )
    if isinstance(exc, WearableManagementRejectedError):
        return APIError(
            exc.status_code,
            "wearables_upstream_rejected",
            "The wearable service rejected the operation.",
        )
    if isinstance(exc, ValueError):
        return APIError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "wearables_request_invalid",
            "The wearable request is invalid.",
        )
    if isinstance(exc, WearableManagementError):
        return APIError(
            status.HTTP_502_BAD_GATEWAY,
            "wearables_upstream_rejected",
            "Open Wearables rejected the management operation.",
        )
    return APIError(
        status.HTTP_502_BAD_GATEWAY,
        "wearables_upstream_unavailable",
        "Open Wearables management failed.",
    )


def _require_mutation_access(request: Request) -> None:
    # BearerTokenMiddleware marks a successful bearer request. This is the
    # path used by paired iPhone/macOS clients and is not CSRF-prone.
    authorization = request.headers.get("authorization", "")
    prefix, _, credential = authorization.partition(" ")
    expected = request.app.state.settings.api_token.get_secret_value().strip()
    if (
        prefix.casefold() == "bearer"
        and expected
        and hmac.compare_digest(credential.strip(), expected)
    ):
        return
    require_local_session(
        request,
        csrf_token=request.headers.get("x-csrf-token", ""),
    )


def fixed_oauth_return_url(settings: Settings, provider: str) -> str:
    base = settings.public_base_url.rstrip("/")
    params: dict[str, str] = {"provider": provider}
    token = settings.api_token.get_secret_value().strip()
    if token:
        params["token"] = viewer_token(token)
    return (
        f"{base}/connect/wearables/oauth-complete?"
        f"{urlencode(params)}"
    )


def _merge_connection_capabilities(
    provider: WearableProviderOut,
    connection: WearableConnectionOut | None,
) -> WearableProviderOut:
    """Prefer capability metadata calculated for the connected provider."""

    if connection is None:
        return provider
    updates: dict[str, Any] = {}
    for field in _CONNECTION_CAPABILITY_FIELDS:
        value = getattr(connection, field)
        if value is not None:
            updates[field] = value
    return provider.model_copy(update=updates)


def _provider_model(row: dict[str, Any]) -> WearableProviderOut:
    """Decode provider rows while keeping older injected clients fail-closed."""

    payload = dict(row)
    raw_provider = payload.get("provider")
    if "management_supported" not in payload or payload["management_supported"] is None:
        if isinstance(raw_provider, str):
            try:
                raw_provider = normalize_provider_identifier(raw_provider)
            except ValueError:
                pass
        payload["management_supported"] = (
            raw_provider in MANAGEMENT_SUPPORTED_PROVIDERS
        )
    return WearableProviderOut.model_validate(payload)


def _observed_provider(
    provider: str,
    *,
    connection: WearableConnectionOut | None,
    has_data_sources: bool,
) -> WearableProviderOut:
    """Keep observed providers visible when catalog metadata is unavailable."""

    native_provider = provider in {"apple", "samsung", "google"}
    known_cloud_provider = (
        provider in _FALLBACK_PROVIDER_NAMES and not native_provider
    )
    return WearableProviderOut(
        provider=provider,
        name=_FALLBACK_PROVIDER_NAMES.get(
            provider,
            provider.replace("-", " ").replace("_", " ").title(),
        ),
        management_supported=provider in MANAGEMENT_SUPPORTED_PROVIDERS,
        has_cloud_api=(
            not native_provider
            and (known_cloud_provider or connection is not None)
        ),
        is_enabled=connection is not None or has_data_sources,
        catalog_available=False,
        icon_url=None,
        live_sync_mode=(
            connection.live_sync_mode if connection is not None else None
        ),
        live_sync_configurable=False,
        client_sdk=native_provider,
        file_import=provider == "apple",
        rest_pull=connection.rest_pull if connection is not None else False,
        webhook_stream=(
            connection.webhook_stream if connection is not None else False
        ),
        webhook_ping=(
            connection.webhook_ping if connection is not None else False
        ),
        webhook_callback=(
            connection.webhook_callback if connection is not None else False
        ),
        webhook_registration_api=(
            connection.webhook_registration_api
            if connection is not None
            else False
        ),
        webhook_inbound_secret=(
            connection.webhook_inbound_secret
            if connection is not None
            else False
        ),
        max_historical_days=(
            connection.max_historical_days if connection is not None else None
        ),
    )


def _merge_observed_providers(
    providers: list[WearableProviderOut],
    connections: list[WearableConnectionOut],
    data_sources: list[WearableDataSourceOut],
) -> list[WearableProviderOut]:
    """Return catalog, connection, and data-source providers as one list."""

    connection_by_provider = {
        row.provider: row for row in connections
    }
    data_source_providers = {row.provider for row in data_sources}
    providers_by_id = {row.provider: row for row in providers}
    ordered_provider_ids = [row.provider for row in providers]

    for provider in [
        *(row.provider for row in connections),
        *(row.provider for row in data_sources),
    ]:
        if provider in providers_by_id:
            continue
        providers_by_id[provider] = _observed_provider(
            provider,
            connection=connection_by_provider.get(provider),
            has_data_sources=provider in data_source_providers,
        )
        ordered_provider_ids.append(provider)

    return [
        _merge_connection_capabilities(
            providers_by_id[provider],
            connection_by_provider.get(provider),
        )
        for provider in ordered_provider_ids
    ]


async def _snapshot(client: OpenWearablesManagementClient) -> WearablesOut:
    settings_results = await asyncio.gather(
        client.provider_catalog(),
        client.connections(),
        client.data_sources(),
        client.coverage(),
        client.recent_sync(),
        client.sync_runs(),
        return_exceptions=True,
    )
    critical = settings_results[:3]
    first_error = next(
        (value for value in critical if isinstance(value, Exception)),
        None,
    )
    if first_error is not None:
        raise _map_management_error(first_error)
    provider_rows, connection_rows, data_source_rows = critical
    providers = [_provider_model(row) for row in provider_rows]
    connections = [
        WearableConnectionOut.model_validate(row)
        for row in connection_rows
    ]
    data_sources = [
        WearableDataSourceOut.model_validate(row)
        for row in data_source_rows
    ]
    providers = _merge_observed_providers(
        providers,
        connections,
        data_sources,
    )
    optional_names = ("coverage", "recent_sync", "sync_runs")
    optional_results = settings_results[3:]
    degraded_components = [
        name
        for name, value in zip(optional_names, optional_results)
        if isinstance(value, Exception)
    ]
    coverage = (
        optional_results[0]
        if not isinstance(optional_results[0], Exception)
        else None
    )
    recent_sync = (
        optional_results[1]
        if not isinstance(optional_results[1], Exception)
        else []
    )
    sync_runs = (
        optional_results[2]
        if not isinstance(optional_results[2], Exception)
        else []
    )
    return WearablesOut(
        api_configured=True,
        user_configured=True,
        providers=providers,
        connections=connections,
        data_sources=data_sources,
        coverage=coverage,
        recent_sync=recent_sync,
        sync_runs=sync_runs,
        degraded_components=degraded_components,
    )


def _fallback_providers() -> list[WearableProviderOut]:
    """Keep the setup page useful before the server credential is configured."""

    return [
        WearableProviderOut(
            provider=provider,
            name=_FALLBACK_PROVIDER_NAMES[provider],
            has_cloud_api=provider
            not in {"apple", "samsung", "google"},
            is_enabled=False,
            catalog_available=False,
            management_supported=provider in MANAGEMENT_SUPPORTED_PROVIDERS,
            icon_url=None,
            live_sync_mode=None,
            live_sync_configurable=False,
            client_sdk=provider in {"apple", "samsung", "google"},
            file_import=provider == "apple",
            rest_pull=False,
            webhook_stream=False,
            webhook_ping=False,
            webhook_callback=False,
            webhook_registration_api=False,
            webhook_inbound_secret=False,
            max_historical_days=None,
        )
        for provider in SUPPORTED_PROVIDER_ORDER
    ]


def _hub_provider_projection(
    provider: WearableProviderOut,
    connection: WearableConnectionOut | None,
    *,
    native_source_connected: bool = False,
) -> dict[str, Any]:
    """Project one provider using the same action policy as companion apps."""

    status = (
        connection.status
        if connection is not None
        else "active"
        if provider.client_sdk and native_source_connected
        else "data_available"
        if native_source_connected
        else "not_connected"
    )
    is_active = status == "active"
    has_connection = connection is not None
    limits = [
        provider.max_historical_days,
        connection.max_historical_days if connection is not None else None,
        365,
    ]
    historical_limit = min(value for value in limits if value is not None)
    return {
        **provider.model_dump(),
        "connection_status": status,
        "management_supported": provider.management_supported,
        "last_synced_at": (
            connection.last_synced_at if connection is not None else None
        ),
        "can_authorize": (
            provider.management_supported
            and provider.catalog_available
            and provider.has_cloud_api
            and provider.is_enabled
            and not is_active
        ),
        "can_sync": (
            provider.management_supported
            and has_connection
            and is_active
            and (provider.is_enabled or has_connection)
            and provider.rest_pull
        ),
        "can_historical_sync": (
            provider.management_supported
            and has_connection
            and is_active
            and (provider.is_enabled or has_connection)
            and (provider.rest_pull or provider.webhook_callback)
        ),
        "can_disconnect": (
            provider.management_supported
            and has_connection
            and is_active
            and (provider.is_enabled or has_connection)
            and provider.has_cloud_api
        ),
        "historical_limit": historical_limit,
        "default_historical_days": min(90, historical_limit),
        "devices": [],
    }


def _unconfigured_snapshot() -> WearablesOut:
    return WearablesOut(
        api_configured=False,
        user_configured=False,
        providers=_fallback_providers(),
        connections=[],
        data_sources=[],
        coverage=None,
        recent_sync=[],
        sync_runs=[],
        degraded_components=[],
    )


async def build_wearable_hub(
    settings: Settings,
    *,
    client: OpenWearablesManagementClient | None = None,
) -> dict[str, Any]:
    """Build a template-safe, secret-free projection for the Web Settings Hub."""

    if not settings.ow_api_key.get_secret_value().strip():
        return {
            "configured": False,
            "error": "",
            "providers": [
                {
                    **provider.model_dump(),
                    "connection_status": "not_configured",
                    "management_supported": provider.management_supported,
                    "last_synced_at": None,
                    "can_authorize": False,
                    "can_sync": False,
                    "can_historical_sync": False,
                    "can_disconnect": False,
                    "historical_limit": min(
                        provider.max_historical_days or 365,
                        365,
                    ),
                    "default_historical_days": min(
                        provider.max_historical_days or 365,
                        90,
                    ),
                    "devices": [],
                }
                for provider in _fallback_providers()
            ],
            "recent_sync": [],
            "sync_runs": [],
        }
    try:
        snapshot = await _snapshot(
            client or await build_management_client(settings)
        )
    except WearableManagementConfigurationError:
        return {
            "configured": False,
            "error": "Wearable service is not configured on the HealthMes server.",
            "providers": [],
            "recent_sync": [],
            "sync_runs": [],
            "degraded_components": ["snapshot"],
        }
    except Exception:
        # Do not render upstream bodies, account ids, or credential material.
        return {
            "configured": True,
            "error": "Open Wearables 상태를 불러오지 못했습니다.",
            "providers": [],
            "recent_sync": [],
            "sync_runs": [],
            "degraded_components": ["snapshot"],
        }

    connection_by_provider = {
        row.provider: row for row in snapshot.connections
    }
    devices_by_provider: dict[str, list[dict[str, Any]]] = {}
    for source in snapshot.data_sources:
        devices_by_provider.setdefault(source.provider, []).append(
            source.model_dump()
        )
    providers: list[dict[str, Any]] = []
    for provider in snapshot.providers:
        connection = connection_by_provider.get(provider.provider)
        devices = devices_by_provider.get(provider.provider, [])
        projection = _hub_provider_projection(
            provider,
            connection,
            native_source_connected=bool(devices),
        )
        projection["devices"] = devices
        providers.append(projection)
    return {
        "configured": snapshot.api_configured and snapshot.user_configured,
        "error": "",
        "providers": providers,
        "recent_sync": [item.model_dump() for item in snapshot.recent_sync],
        "sync_runs": [item.model_dump() for item in snapshot.sync_runs],
        "degraded_components": snapshot.degraded_components,
    }


@router.get("", response_model=WearablesOut)
async def get_wearables(request: Request, response: Response) -> WearablesOut:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await _snapshot(await get_management_client(request))
    except WearableManagementConfigurationError:
        return _unconfigured_snapshot()
    except APIError:
        raise
    except Exception as exc:
        raise _map_management_error(exc) from exc


@router.get("/providers", response_model=list[WearableProviderOut])
async def get_wearable_providers(
    request: Request,
    response: Response,
) -> list[WearableProviderOut]:
    response.headers["Cache-Control"] = "no-store"
    try:
        client = await get_management_client(request)
        provider_rows, connection_rows, data_source_rows = await asyncio.gather(
            client.provider_catalog(),
            client.connections(),
            client.data_sources(),
        )
        providers = [_provider_model(row) for row in provider_rows]
        connections = [
            WearableConnectionOut.model_validate(row)
            for row in connection_rows
        ]
        data_sources = [
            WearableDataSourceOut.model_validate(row)
            for row in data_source_rows
        ]
        return _merge_observed_providers(
            providers,
            connections,
            data_sources,
        )
    except Exception as exc:
        raise _map_management_error(exc) from exc


@router.get("/coverage")
async def get_wearable_coverage(
    request: Request,
    response: Response,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await (await get_management_client(request)).coverage()
    except Exception as exc:
        raise _map_management_error(exc) from exc


@router.get("/sync/recent", response_model=list[WearableSyncStatusOut])
async def get_recent_wearable_sync(
    request: Request,
    response: Response,
) -> list[WearableSyncStatusOut]:
    response.headers["Cache-Control"] = "no-store"
    try:
        rows = await (await get_management_client(request)).recent_sync()
        return [WearableSyncStatusOut.model_validate(row) for row in rows]
    except Exception as exc:
        raise _map_management_error(exc) from exc


@router.get("/sync/runs", response_model=list[WearableSyncStatusOut])
async def get_wearable_sync_runs(
    request: Request,
    response: Response,
) -> list[WearableSyncStatusOut]:
    response.headers["Cache-Control"] = "no-store"
    try:
        rows = await (await get_management_client(request)).sync_runs()
        return [WearableSyncStatusOut.model_validate(row) for row in rows]
    except Exception as exc:
        raise _map_management_error(exc) from exc


@router.get(
    "/{provider}/authorize",
    response_model=WearableAuthorizationOut,
)
async def authorize_wearable_provider(
    provider: str,
    request: Request,
    response: Response,
) -> WearableAuthorizationOut:
    response.headers["Cache-Control"] = "no-store"
    _require_mutation_access(request)
    try:
        client = await get_management_client(request)
        normalized = normalize_management_provider_identifier(provider)
        result = await client.authorize_url(
            normalized,
            redirect_uri=fixed_oauth_return_url(
                request.app.state.settings,
                normalized,
            ),
        )
        return WearableAuthorizationOut(
            provider=normalized,
            authorization_url=result["authorization_url"],
        )
    except Exception as exc:
        raise _map_management_error(exc) from exc


@router.post(
    "/{provider}/disconnect",
    response_model=WearableMutationOut,
)
async def disconnect_wearable_provider(
    provider: str,
    request: Request,
) -> WearableMutationOut:
    _require_mutation_access(request)
    try:
        client = await get_management_client(request)
        normalized = normalize_management_provider_identifier(provider)
        await client.disconnect(normalized)
        return WearableMutationOut(
            provider=normalized,
            operation="disconnect",
            message="Wearable provider disconnected.",
        )
    except Exception as exc:
        raise _map_management_error(exc) from exc


@router.post(
    "/{provider}/sync",
    response_model=WearableMutationOut,
)
async def sync_wearable_provider(
    provider: str,
    request: Request,
) -> WearableMutationOut:
    _require_mutation_access(request)
    try:
        client = await get_management_client(request)
        normalized = normalize_management_provider_identifier(provider)
        result = await client.sync(normalized)
        return WearableMutationOut(
            provider=normalized,
            operation="sync",
            success=bool(result.get("success", False)),
            async_=result.get("async"),
            task_id=result.get("task_id"),
            method=result.get("method"),
            message=result.get("message"),
            days=result.get("days"),
            start_date=result.get("start_date"),
            end_date=result.get("end_date"),
        )
    except Exception as exc:
        raise _map_management_error(exc) from exc


@router.post(
    "/{provider}/sync/historical",
    response_model=WearableMutationOut,
)
async def historical_sync_wearable_provider(
    provider: str,
    body: HistoricalSyncIn,
    request: Request,
) -> WearableMutationOut:
    _require_mutation_access(request)
    try:
        client = await get_management_client(request)
        normalized = normalize_management_provider_identifier(provider)
        result = await client.historical_sync(normalized, days=body.days)
        return WearableMutationOut(
            provider=normalized,
            operation="historical_sync",
            success=bool(result.get("success", False)),
            async_=result.get("async"),
            task_id=result.get("task_id"),
            method=result.get("method"),
            message=result.get("message"),
            days=result.get("days"),
            start_date=result.get("start_date"),
            end_date=result.get("end_date"),
        )
    except Exception as exc:
        raise _map_management_error(exc) from exc


__all__ = [
    "HistoricalSyncIn",
    "WearablesOut",
    "fixed_oauth_return_url",
    "get_management_client",
    "router",
]
