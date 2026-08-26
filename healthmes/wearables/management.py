"""Server-side management adapter for the Open Wearables REST API.

The decision engine and the MCP layer already consume normalized wearable
context. This module is deliberately narrower: it manages provider
connections and sync operations for the Settings Hub without exposing
upstream credentials or provider account identities to clients.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

import httpx

from healthmes.config import Settings
from healthmes.mcp_server.ow_client import OWClientError, resolve_single_user_id

logger = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 512_000
SUPPORTED_PROVIDERS = frozenset(
    {
        "apple",
        "samsung",
        "google",
        "garmin",
        "polar",
        "suunto",
        "whoop",
        "strava",
        "oura",
        "fitbit",
        "ultrahuman",
    }
)
_PROVIDER_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
MAX_PUBLIC_TEXT_LENGTH = 240
MAX_PUBLIC_IDENTIFIER_LENGTH = 128
_SENSITIVE_MARKER = re.compile(
    r"(?i)\b(?:api[_ -]?key|apikey|access[_ -]?token|"
    r"refresh[_ -]?token|id[_ -]?token|client[_ -]?secret|"
    r"provider[_ -]?user[_ -]?id|connection[_ -]?id|user[_ -]?id|"
    r"authorization|password|private[_ -]?key|secret|token)\b"
)
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_UUID_VALUE = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)
_SAFE_SYNC_MESSAGES = frozenset(
    {
        "sync queued",
        "historical sync queued",
        "sync complete",
        "sync completed",
        "sync requested",
        "historical sync requested",
        "sync started",
        "sync running",
        "sync failed",
        "historical sync failed",
    }
)
CONNECTION_STATUSES = frozenset({"active", "expired", "revoked"})
LIVE_SYNC_MODES = frozenset({"pull", "webhook"})
CAPABILITY_KEYS = (
    "client_sdk",
    "file_import",
    "rest_pull",
    "webhook_stream",
    "webhook_ping",
    "webhook_callback",
    "webhook_registration_api",
    "webhook_inbound_secret",
    "max_historical_days",
    "live_sync_mode",
)

SUPPORTED_PROVIDER_ORDER = (
    "apple",
    "samsung",
    "google",
    "garmin",
    "polar",
    "suunto",
    "whoop",
    "strava",
    "oura",
    "fitbit",
    "ultrahuman",
)

MANAGEMENT_SUPPORTED_PROVIDERS = frozenset(SUPPORTED_PROVIDER_ORDER)
NATIVE_PROVIDERS = frozenset({"apple", "samsung", "google"})
FORBIDDEN_AUTHORIZATION_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth_token",
        "client_secret",
        "id_token",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    }
)

PROVIDER_DISPLAY_NAMES = {
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

# Open Wearables' provider catalog predates the connection capability
# projection. Keep a compatibility fallback for providers that are not yet
# connected; upstream capability fields, when present, always take precedence.
PROVIDER_CAPABILITY_DEFAULTS: dict[str, dict[str, Any]] = {
    "apple": {
        "client_sdk": True,
        "file_import": True,
        "rest_pull": False,
        "webhook_stream": False,
        "webhook_ping": False,
        "webhook_callback": False,
        "webhook_registration_api": False,
        "webhook_inbound_secret": False,
        "max_historical_days": None,
        "live_sync_mode": None,
    },
    "samsung": {
        "client_sdk": True,
        "file_import": False,
        "rest_pull": False,
        "webhook_stream": False,
        "webhook_ping": False,
        "webhook_callback": False,
        "webhook_registration_api": False,
        "webhook_inbound_secret": False,
        "max_historical_days": None,
        "live_sync_mode": None,
    },
    "google": {
        "client_sdk": True,
        "file_import": False,
        "rest_pull": False,
        "webhook_stream": False,
        "webhook_ping": False,
        "webhook_callback": False,
        "webhook_registration_api": False,
        "webhook_inbound_secret": False,
        "max_historical_days": None,
        "live_sync_mode": None,
    },
    "garmin": {
        "client_sdk": False,
        "file_import": False,
        "rest_pull": False,
        "webhook_stream": True,
        "webhook_ping": False,
        "webhook_callback": True,
        "webhook_registration_api": False,
        "webhook_inbound_secret": False,
        "max_historical_days": 30,
        "live_sync_mode": "webhook",
    },
    "polar": {
        "client_sdk": False,
        "file_import": False,
        "rest_pull": True,
        "webhook_stream": False,
        "webhook_ping": True,
        "webhook_callback": False,
        "webhook_registration_api": True,
        "webhook_inbound_secret": True,
        "max_historical_days": None,
        "live_sync_mode": "pull",
    },
    "suunto": {
        "client_sdk": False,
        "file_import": False,
        "rest_pull": True,
        "webhook_stream": True,
        "webhook_ping": False,
        "webhook_callback": False,
        "webhook_registration_api": False,
        "webhook_inbound_secret": False,
        "max_historical_days": None,
        "live_sync_mode": "pull",
    },
    "whoop": {
        "client_sdk": False,
        "file_import": False,
        "rest_pull": True,
        "webhook_stream": False,
        "webhook_ping": True,
        "webhook_callback": False,
        "webhook_registration_api": False,
        "webhook_inbound_secret": False,
        "max_historical_days": None,
        "live_sync_mode": "pull",
    },
    "strava": {
        "client_sdk": False,
        "file_import": False,
        "rest_pull": True,
        "webhook_stream": False,
        "webhook_ping": True,
        "webhook_callback": False,
        "webhook_registration_api": True,
        "webhook_inbound_secret": False,
        "max_historical_days": None,
        "live_sync_mode": "pull",
    },
    "oura": {
        "client_sdk": False,
        "file_import": False,
        "rest_pull": True,
        "webhook_stream": False,
        "webhook_ping": True,
        "webhook_callback": False,
        "webhook_registration_api": True,
        "webhook_inbound_secret": False,
        "max_historical_days": None,
        "live_sync_mode": "pull",
    },
    "fitbit": {
        "client_sdk": False,
        "file_import": False,
        "rest_pull": True,
        "webhook_stream": False,
        "webhook_ping": False,
        "webhook_callback": False,
        "webhook_registration_api": False,
        "webhook_inbound_secret": False,
        "max_historical_days": None,
        "live_sync_mode": "pull",
    },
    "ultrahuman": {
        "client_sdk": False,
        "file_import": False,
        "rest_pull": True,
        "webhook_stream": False,
        "webhook_ping": False,
        "webhook_callback": False,
        "webhook_registration_api": False,
        "webhook_inbound_secret": False,
        "max_historical_days": None,
        "live_sync_mode": "pull",
    },
}

# A provider added upstream can still be displayed in the Settings Hub before
# HealthMes ships an explicit management contract for it. Its action surface
# remains disabled until the provider is added to the supported set above.
GENERIC_PROVIDER_CAPABILITY_DEFAULTS: dict[str, Any] = {
    "client_sdk": False,
    "file_import": False,
    "rest_pull": False,
    "webhook_stream": False,
    "webhook_ping": False,
    "webhook_callback": False,
    "webhook_registration_api": False,
    "webhook_inbound_secret": False,
    "max_historical_days": None,
    "live_sync_mode": None,
}


class WearableManagementError(Exception):
    """Base error for management adapter failures."""


class WearableManagementConfigurationError(WearableManagementError):
    """HealthMes is missing Open Wearables configuration."""


class WearableManagementAuthError(WearableManagementError):
    """Open Wearables rejected the server credential."""


class WearableManagementNotFoundError(WearableManagementError):
    """Open Wearables could not find the requested resource."""


class WearableManagementPayloadError(WearableManagementError):
    """Open Wearables returned a response outside its documented shape."""


class WearableManagementUpstreamError(WearableManagementError):
    """Open Wearables failed for a transport or server-side reason."""


class WearableManagementCapabilityError(WearableManagementError):
    """The requested operation is not supported by the provider."""


class WearableManagementConnectionError(WearableManagementError):
    """The provider connection is not in a state usable for the operation."""


class WearableManagementRejectedError(WearableManagementError):
    """Open Wearables rejected an otherwise valid management request."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class WearableManagementConfig:
    base_url: str
    api_key: str
    user_id: str
    transport: httpx.AsyncBaseTransport | None = None
    timeout: float = 30.0


def sanitize_public_text(
    value: object,
    resource: str,
    *,
    sensitive_values: tuple[str, ...] = (),
    max_length: int = MAX_PUBLIC_TEXT_LENGTH,
) -> str | None:
    """Bound free-form upstream text and reject unsafe material."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise WearableManagementPayloadError(
            f"open-wearables returned an invalid {resource} text value"
        )
    if max_length < 4:
        raise ValueError("max_length must be at least four characters")
    text = " ".join(value.split())
    if not text:
        return None
    folded = text.casefold()
    if any(
        sensitive.casefold() in folded
        for sensitive in sensitive_values
        if sensitive and len(sensitive) >= 4
    ):
        return None
    if (
        "://" in text
        or _SENSITIVE_MARKER.search(text)
        or _BEARER_TOKEN.search(text)
        or _UUID_VALUE.search(text)
    ):
        return None
    if len(text) > max_length:
        text = text[: max_length - 3].rstrip() + "..."
    return text


def _required_public_text(
    value: str,
    resource: str,
    *,
    sensitive_values: tuple[str, ...] = (),
    max_length: int = MAX_PUBLIC_TEXT_LENGTH,
) -> str:
    text = sanitize_public_text(
        value,
        resource,
        sensitive_values=sensitive_values,
        max_length=max_length,
    )
    if text is None:
        raise WearableManagementPayloadError(
            f"open-wearables returned unsafe {resource} text"
        )
    return text


def _safe_sync_message(
    value: object,
    resource: str,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> str | None:
    text = sanitize_public_text(
        value,
        resource,
        sensitive_values=sensitive_values,
    )
    if text is None or text.casefold().rstrip(".") not in _SAFE_SYNC_MESSAGES:
        return None
    return text


def _safe_sync_error(
    value: object,
    resource: str,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> str | None:
    if value is None:
        return None
    # Error details are intentionally not projected. The UI can use status
    # and stage while diagnostics remain server-side.
    if not isinstance(value, str):
        raise WearableManagementPayloadError(
            f"open-wearables returned an invalid {resource} error value"
        )
    return "The wearable provider reported an error."


def _safe_public_identifier(
    value: object,
    resource: str,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> str:
    """Keep an opaque identifier useful without exposing its original value."""

    text = sanitize_public_text(
        value,
        resource,
        sensitive_values=sensitive_values,
        max_length=MAX_PUBLIC_IDENTIFIER_LENGTH,
    )
    if text is not None:
        return text
    if not isinstance(value, str) or not value.strip():
        raise WearableManagementPayloadError(
            f"open-wearables returned an invalid {resource} identifier"
        )
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"opaque-{digest}"


def _require_mapping(value: object, resource: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WearableManagementPayloadError(
            f"open-wearables returned an invalid {resource} response"
        )
    return value


def _require_list(value: object, resource: str) -> list[Any]:
    if not isinstance(value, list):
        raise WearableManagementPayloadError(
            f"open-wearables returned an invalid {resource} response"
        )
    return value


def _string(value: object, resource: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise WearableManagementPayloadError(
            f"open-wearables returned an invalid {resource} response"
        )
    return value


def _optional_text(value: object, resource: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WearableManagementPayloadError(
            f"open-wearables returned an invalid {resource} value"
        )
    value = value.strip()
    return value or None


def _boolean(value: object, resource: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    if type(value) is not bool:
        raise WearableManagementPayloadError(
            f"open-wearables returned an invalid {resource} boolean"
        )
    return value


def _optional_positive_int(value: object, resource: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise WearableManagementPayloadError(
            f"open-wearables returned an invalid {resource} limit"
        )
    return value


def _optional_live_sync_mode(value: object, resource: str) -> str | None:
    if value is None:
        return None
    mode = _string(value, resource).strip().casefold()
    if mode not in LIVE_SYNC_MODES:
        raise WearableManagementPayloadError(
            f"open-wearables returned an invalid {resource} mode"
        )
    return mode


def _capability_value(
    row: Mapping[str, Any],
    provider: str,
    key: str,
) -> Any:
    defaults = PROVIDER_CAPABILITY_DEFAULTS.get(
        provider,
        GENERIC_PROVIDER_CAPABILITY_DEFAULTS,
    )
    if key not in row or row[key] is None:
        return defaults[key]
    return row[key]


def _provider(value: object, resource: str) -> str:
    try:
        return normalize_provider_identifier(
            _string(value, resource),
        )
    except ValueError:
        raise WearableManagementPayloadError(
            f"open-wearables returned an invalid provider in {resource}"
        ) from None


def normalize_provider_identifier(provider: str) -> str:
    """Normalize a provider slug without constraining it to a release list.

    Open Wearables can add providers independently of HealthMes. The stable
    provider order below is only a fallback/catalog presentation concern, not
    a validation allow-list.
    """

    normalized = provider.strip().casefold()
    if _PROVIDER_IDENTIFIER.fullmatch(normalized) is None:
        raise ValueError(f"unsupported wearable provider: {provider}")
    return normalized


def normalize_management_provider_identifier(provider: str) -> str:
    """Require a provider whose management routes HealthMes has verified."""

    normalized = normalize_provider_identifier(provider)
    if normalized not in MANAGEMENT_SUPPORTED_PROVIDERS:
        raise WearableManagementCapabilityError(
            f"{normalized} is visible in Open Wearables but is not enabled "
            "in the HealthMes management contract"
        )
    return normalized


def _uuid_string(value: object, resource: str) -> str:
    text = _string(value, resource)
    try:
        parsed = UUID(text)
    except (TypeError, ValueError):
        raise WearableManagementPayloadError(
            f"open-wearables returned an invalid {resource} identifier"
        ) from None
    if str(parsed) != text:
        raise WearableManagementPayloadError(
            f"open-wearables returned a non-canonical {resource} identifier"
        )
    return text


def _optional_uuid_string(value: object, resource: str) -> str | None:
    if value is None:
        return None
    return _uuid_string(value, resource)


def _optional_datetime(value: object, resource: str) -> str | None:
    if value is None:
        return None
    text = _string(value, resource)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise WearableManagementPayloadError(
            f"open-wearables returned an invalid {resource} timestamp"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _public_url(value: object, resource: str) -> str:
    text = _string(value, resource)
    parsed = urlsplit(text)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise WearableManagementPayloadError(
            f"open-wearables returned an invalid {resource} URL"
        )
    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_key = key.strip().casefold().replace("-", "_")
        if (
            normalized_key in FORBIDDEN_AUTHORIZATION_QUERY_KEYS
            or any(
                marker in normalized_key
                for marker in ("secret", "token", "password", "private_key")
            )
        ):
            raise WearableManagementPayloadError(
                f"open-wearables returned an unsafe {resource} URL"
            )
    return text


def _safe_provider_name(value: object, provider: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return provider.replace("-", " ").title()


def _safe_icon_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value
    if value.startswith("/") and not value.startswith("//"):
        return value
    return None


class OpenWearablesManagementClient:
    """Authenticated management client with a secret-free public projection."""

    def __init__(
        self,
        config: WearableManagementConfig,
    ) -> None:
        self.base_url = config.base_url.rstrip("/")
        self._api_key = config.api_key
        self.user_id = config.user_id
        self._transport = config.transport
        self.timeout = config.timeout

    @classmethod
    async def from_settings(
        cls,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> OpenWearablesManagementClient:
        api_key = settings.ow_api_key.get_secret_value().strip()
        if not api_key:
            raise WearableManagementConfigurationError(
                "Open Wearables API key is not configured on the HealthMes server."
            )
        # Reuse the single-user policy used by MCP and the decision engine.
        from healthmes.mcp_server.ow_client import OWClient

        reader = OWClient(
            base_url=settings.ow_base_url,
            api_key=api_key,
            transport=transport,
        )
        try:
            user_id = await resolve_single_user_id(reader, settings)
        except (LookupError, OWClientError) as exc:
            raise WearableManagementConfigurationError(str(exc)) from exc
        try:
            UUID(user_id)
        except (TypeError, ValueError):
            raise WearableManagementConfigurationError(
                "Open Wearables user id must be a canonical UUID."
            ) from None
        return cls(
            WearableManagementConfig(
                base_url=settings.ow_base_url,
                api_key=api_key,
                user_id=user_id,
                transport=transport,
            )
        )

    @classmethod
    def from_settings_with_user(
        cls,
        settings: Settings,
        user_id: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> OpenWearablesManagementClient:
        api_key = settings.ow_api_key.get_secret_value().strip()
        if not api_key:
            raise WearableManagementConfigurationError(
                "Open Wearables API key is not configured on the HealthMes server."
            )
        try:
            UUID(user_id)
        except (TypeError, ValueError):
            raise WearableManagementConfigurationError(
                "Open Wearables user id must be a canonical UUID."
            ) from None
        return cls(
            WearableManagementConfig(
                base_url=settings.ow_base_url,
                api_key=api_key,
                user_id=user_id,
                transport=transport,
            )
        )

    @property
    def headers(self) -> dict[str, str]:
        return {
            "X-Open-Wearables-API-Key": self._api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> Any:
        if not self._api_key:
            raise WearableManagementConfigurationError(
                "Open Wearables API key is not configured on the HealthMes server."
            )
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=self._transport,
            ) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=self.headers,
                    params=dict(params or {}),
                    json=dict(json_body) if json_body is not None else None,
                )
        except httpx.HTTPError as exc:
            raise WearableManagementUpstreamError(
                "Open Wearables request failed."
            ) from exc
        if response.status_code == 401:
            raise WearableManagementAuthError(
                "Open Wearables rejected the configured API key."
            )
        if response.status_code == 404:
            raise WearableManagementNotFoundError(
                "Open Wearables resource was not found."
            )
        if response.status_code >= 500:
            raise WearableManagementUpstreamError(
                f"Open Wearables returned HTTP {response.status_code}."
            )
        if response.status_code >= 400:
            raise WearableManagementRejectedError(
                "Open Wearables rejected the request.",
                status_code=response.status_code,
            )
        if response.status_code == 204 or not response.content:
            return None
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise WearableManagementUpstreamError(
                "Open Wearables response exceeded the size limit."
            )
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise WearableManagementPayloadError(
                "Open Wearables returned invalid JSON."
            ) from exc

    async def provider_catalog(self) -> list[dict[str, Any]]:
        payload = _require_list(
            await self._request("GET", "/api/v1/oauth/providers"),
            "provider catalog",
        )
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in payload:
            row = _require_mapping(raw, "provider catalog")
            provider = _provider(row.get("provider"), "provider catalog")
            if provider in seen:
                raise WearableManagementPayloadError(
                    "Open Wearables returned duplicate providers."
                )
            seen.add(provider)
            rows.append(
                {
                    "provider": provider,
                    "name": _safe_provider_name(row.get("name"), provider),
                    # This row came from Open Wearables' live catalog. Rows
                    # added below are explicit HealthMes fallbacks and must
                    # not look OAuth-manageable to clients.
                    "catalog_available": True,
                    "management_supported": (
                        provider in MANAGEMENT_SUPPORTED_PROVIDERS
                    ),
                    "has_cloud_api": _boolean(
                        row.get("has_cloud_api"),
                        "provider catalog has_cloud_api",
                    ),
                    "is_enabled": _boolean(
                        row.get("is_enabled"),
                        "provider catalog is_enabled",
                    ),
                    "icon_url": _safe_icon_url(row.get("icon_url")),
                    "live_sync_mode": _optional_live_sync_mode(
                        _capability_value(row, provider, "live_sync_mode"),
                        "provider catalog live_sync_mode",
                    ),
                    "live_sync_configurable": _boolean(
                        row.get("live_sync_configurable"),
                        "provider catalog live_sync_configurable",
                    ),
                    "client_sdk": _boolean(
                        _capability_value(row, provider, "client_sdk"),
                        "provider catalog client_sdk",
                    ),
                    "file_import": _boolean(
                        _capability_value(row, provider, "file_import"),
                        "provider catalog file_import",
                    ),
                    "rest_pull": _boolean(
                        _capability_value(row, provider, "rest_pull"),
                        "provider catalog rest_pull",
                    ),
                    "webhook_stream": _boolean(
                        _capability_value(row, provider, "webhook_stream"),
                        "provider catalog webhook_stream",
                    ),
                    "webhook_ping": _boolean(
                        _capability_value(row, provider, "webhook_ping"),
                        "provider catalog webhook_ping",
                    ),
                    "webhook_callback": _boolean(
                        _capability_value(row, provider, "webhook_callback"),
                        "provider catalog webhook_callback",
                    ),
                    "webhook_registration_api": _boolean(
                        _capability_value(
                            row,
                            provider,
                            "webhook_registration_api",
                        ),
                        "provider catalog webhook_registration_api",
                    ),
                    "webhook_inbound_secret": _boolean(
                        _capability_value(
                            row,
                            provider,
                            "webhook_inbound_secret",
                        ),
                        "provider catalog webhook_inbound_secret",
                    ),
                    "max_historical_days": _optional_positive_int(
                        _capability_value(row, provider, "max_historical_days"),
                        "provider catalog max_historical_days",
                    ),
                }
            )
        rows_by_provider = {row["provider"]: row for row in rows}
        upstream_order = [row["provider"] for row in rows]
        for provider in SUPPORTED_PROVIDER_ORDER:
            if provider in rows_by_provider:
                continue
            defaults = PROVIDER_CAPABILITY_DEFAULTS[provider]
            rows_by_provider[provider] = {
                "provider": provider,
                "name": PROVIDER_DISPLAY_NAMES[provider],
                "catalog_available": False,
                "management_supported": True,
                "has_cloud_api": provider not in {"apple", "samsung", "google"},
                "is_enabled": False,
                "icon_url": None,
                "live_sync_mode": defaults["live_sync_mode"],
                "live_sync_configurable": False,
                **defaults,
            }
        # Keep the familiar providers stable across clients, then append
        # providers newly introduced by Open Wearables. This makes the UI
        # deterministic without hiding upstream additions.
        ordered_providers = list(SUPPORTED_PROVIDER_ORDER)
        ordered_providers.extend(
            provider
            for provider in upstream_order
            if provider not in SUPPORTED_PROVIDER_ORDER
        )
        return [rows_by_provider[provider] for provider in ordered_providers]

    async def coverage(self) -> dict[str, Any]:
        payload = _require_mapping(
            await self._request("GET", "/api/v1/meta/coverage"),
            "coverage",
        )
        # Coverage is descriptive metadata. Keep it bounded and omit no
        # provider identity, but never pass arbitrary upstream objects through.
        providers = payload.get("providers")
        if not isinstance(providers, list):
            raise WearableManagementPayloadError(
                "Open Wearables returned an invalid coverage response."
            )
        return {
            "providers": [
                _provider(item, "coverage") for item in providers
            ],
            "timeseries": payload.get("timeseries", []),
            "workout_fields": payload.get("workout_fields", []),
            "sleep_fields": payload.get("sleep_fields", []),
            "health_scores": payload.get("health_scores", []),
        }

    async def connections(self) -> list[dict[str, Any]]:
        payload = _require_list(
            await self._request(
                "GET",
                f"/api/v1/users/{self.user_id}/connections",
            ),
            "connections",
        )
        rows: list[dict[str, Any]] = []
        for raw in payload:
            row = _require_mapping(raw, "connections")
            provider = _provider(row.get("provider"), "connections")
            status = _string(row.get("status"), "connections").casefold()
            if status not in CONNECTION_STATUSES:
                raise WearableManagementPayloadError(
                    "Open Wearables returned an invalid connection status."
                )
            owner = row.get("user_id")
            if owner is not None and _uuid_string(owner, "connections") != self.user_id:
                raise WearableManagementPayloadError(
                    "Open Wearables returned a connection for another user."
                )
            rows.append(
                {
                    "provider": provider,
                    "status": status,
                    "last_synced_at": _optional_datetime(
                        row.get("last_synced_at"), "connections"
                    ),
                    "created_at": _optional_datetime(
                        row.get("created_at"), "connections"
                    ),
                    "updated_at": _optional_datetime(
                        row.get("updated_at"), "connections"
                    ),
                    "max_historical_days": _optional_positive_int(
                        _capability_value(
                            row,
                            provider,
                            "max_historical_days",
                        ),
                        "connection max_historical_days",
                    ),
                    "rest_pull": _boolean(
                        _capability_value(row, provider, "rest_pull"),
                        "connection rest_pull",
                    ),
                    "webhook_stream": _boolean(
                        _capability_value(row, provider, "webhook_stream"),
                        "connection webhook_stream",
                    ),
                    "webhook_ping": _boolean(
                        _capability_value(row, provider, "webhook_ping"),
                        "connection webhook_ping",
                    ),
                    "webhook_callback": _boolean(
                        _capability_value(row, provider, "webhook_callback"),
                        "connection webhook_callback",
                    ),
                    "webhook_registration_api": _boolean(
                        _capability_value(
                            row,
                            provider,
                            "webhook_registration_api",
                        ),
                        "connection webhook_registration_api",
                    ),
                    "webhook_inbound_secret": _boolean(
                        _capability_value(
                            row,
                            provider,
                            "webhook_inbound_secret",
                        ),
                        "connection webhook_inbound_secret",
                    ),
                    "live_sync_mode": _optional_live_sync_mode(
                        _capability_value(row, provider, "live_sync_mode"),
                        "connection live_sync_mode",
                    ),
                }
            )
        return rows

    async def data_sources(self) -> list[dict[str, Any]]:
        payload = _require_mapping(
            await self._request(
                "GET",
                f"/api/v1/users/{self.user_id}/data-sources",
            ),
            "data sources",
        )
        items = _require_list(payload.get("items"), "data sources")
        total = payload.get("total")
        if type(total) is not int or total != len(items):
            raise WearableManagementPayloadError(
                "Open Wearables returned an invalid data source count."
            )
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in items:
            row = _require_mapping(raw, "data sources")
            source_id = _uuid_string(row.get("id"), "data source")
            if source_id in seen:
                raise WearableManagementPayloadError(
                    "Open Wearables returned duplicate data sources."
                )
            seen.add(source_id)
            owner = _uuid_string(row.get("user_id"), "data source")
            if owner != self.user_id:
                raise WearableManagementPayloadError(
                    "Open Wearables returned a data source for another user."
                )
            provider = _provider(row.get("provider"), "data source")
            rows.append(
                {
                    "id": source_id,
                    "provider": provider,
                    "display_name": (
                        _optional_text(row.get("display_name"), "data source display_name")
                    ),
                    "device_model": (
                        _optional_text(row.get("device_model"), "data source device_model")
                    ),
                    "software_version": (
                        _optional_text(
                            row.get("software_version"),
                            "data source software_version",
                        )
                    ),
                    "device_type": (
                        _optional_text(row.get("device_type"), "data source device_type")
                    ),
                    "source": (
                        _optional_text(row.get("source"), "data source source")
                    ),
                    "original_source_name": (
                        _optional_text(
                            row.get("original_source_name"),
                            "data source original_source_name",
                        )
                    ),
                }
            )
        return rows

    async def authorize_url(
        self,
        provider: str,
        *,
        redirect_uri: str,
    ) -> dict[str, str]:
        provider = self._validate_management_provider(provider)
        metadata = next(
            (
                row
                for row in await self.provider_catalog()
                if row["provider"] == provider
            ),
            None,
        )
        if metadata is None or not metadata["has_cloud_api"]:
            raise WearableManagementCapabilityError(
                f"{provider} is connected through a native client, not OAuth"
            )
        if not metadata["is_enabled"]:
            raise WearableManagementCapabilityError(
                f"{provider} is disabled on the Open Wearables server"
            )
        if not _is_fixed_redirect_uri(redirect_uri):
            raise WearableManagementConfigurationError(
                "HealthMes generated an invalid OAuth return URL."
            )
        payload = _require_mapping(
            await self._request(
                "GET",
                f"/api/v1/oauth/{provider}/authorize",
                params={
                    "user_id": self.user_id,
                    "redirect_uri": redirect_uri,
                },
            ),
            "authorization",
        )
        authorization_url = _public_url(
            payload.get("authorization_url"),
            "authorization",
        )
        state = _string(payload.get("state"), "authorization")
        return {"authorization_url": authorization_url, "state": state}

    async def disconnect(self, provider: str) -> None:
        provider = self._validate_management_provider(provider)
        if provider in NATIVE_PROVIDERS:
            raise WearableManagementCapabilityError(
                f"{provider} is managed by the native HealthMes client"
            )
        connection = await self._connection_for(provider)
        self._require_active_connection(connection, provider)
        await self._request(
            "DELETE",
            f"/api/v1/users/{self.user_id}/connections/{provider}",
        )

    async def sync(self, provider: str) -> dict[str, Any]:
        provider = self._validate_management_provider(provider)
        connection = await self._connection_for(provider)
        self._require_active_connection(connection, provider)
        if not connection["rest_pull"]:
            raise WearableManagementCapabilityError(
                f"{provider} does not support an explicit REST sync request"
            )
        payload = _require_mapping(
            await self._request(
                "POST",
                f"/api/v1/providers/{provider}/users/{self.user_id}/sync",
            ),
            "sync",
        )
        return _safe_sync_result(
            payload,
            provider,
            sensitive_values=(self._api_key, self.user_id),
        )

    async def historical_sync(self, provider: str, *, days: int) -> dict[str, Any]:
        provider = self._validate_management_provider(provider)
        if days < 1 or days > 365:
            raise ValueError("historical sync days must be between 1 and 365")
        connection = await self._connection_for(provider)
        self._require_active_connection(connection, provider)
        capabilities = connection

        if not (
            capabilities["rest_pull"]
            or capabilities["webhook_callback"]
        ):
            raise WearableManagementCapabilityError(
                f"{provider} does not support historical sync through "
                "the server management API"
            )

        max_days = capabilities["max_historical_days"]
        if isinstance(max_days, int) and days > max_days:
            raise ValueError(
                f"{provider} historical sync supports at most {max_days} days"
            )
        payload = _require_mapping(
            await self._request(
                "POST",
                f"/api/v1/providers/{provider}/users/{self.user_id}/sync/historical",
                params={"days": days},
            ),
            "historical sync",
        )
        return _safe_sync_result(
            payload,
            provider,
            sensitive_values=(self._api_key, self.user_id),
        )

    async def _connection_for(
        self,
        provider: str,
    ) -> dict[str, Any]:
        connection = next(
            (
                row
                for row in await self.connections()
                if row["provider"] == provider
            ),
            None,
        )
        if connection is None:
            raise WearableManagementConnectionError(
                f"{provider} is not connected to HealthMes"
            )
        return connection

    @staticmethod
    def _require_active_connection(
        connection: dict[str, Any],
        provider: str,
    ) -> None:
        status = connection["status"]
        if status != "active":
            if status == "expired":
                raise WearableManagementConnectionError(
                    f"{provider} authorization expired; reconnect is required"
                )
            raise WearableManagementConnectionError(
                f"{provider} is not connected to HealthMes"
            )

    async def recent_sync(self, *, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 1 or limit > 200:
            raise ValueError("sync event limit must be between 1 and 200")
        payload = _require_list(
            await self._request(
                "GET",
                f"/api/v1/users/{self.user_id}/sync/recent",
                params={"limit": limit},
            ),
            "recent sync",
        )
        return [
            _safe_sync_event(
                item,
                "recent sync",
                sensitive_values=(self._api_key, self.user_id),
            )
            for item in payload
        ]

    async def sync_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 1 or limit > 200:
            raise ValueError("sync run limit must be between 1 and 200")
        payload = _require_list(
            await self._request(
                "GET",
                f"/api/v1/users/{self.user_id}/sync/runs",
                params={"limit": limit},
            ),
            "sync runs",
        )
        return [
            _safe_sync_event(
                item,
                "sync runs",
                sensitive_values=(self._api_key, self.user_id),
            )
            for item in payload
        ]

    def _validate_provider(self, provider: str) -> str:
        return normalize_provider_identifier(provider)

    def _validate_management_provider(self, provider: str) -> str:
        return normalize_management_provider_identifier(provider)


def _safe_sync_result(
    value: Mapping[str, Any],
    provider: str,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> dict[str, Any]:
    success = value.get("success")
    if type(success) is not bool:
        raise WearableManagementPayloadError(
            "open-wearables returned an invalid sync success value"
        )
    result: dict[str, Any] = {
        "provider": provider,
        "success": success,
    }
    expected_types: dict[str, type | tuple[type, ...]] = {
        "async": bool,
        "task_id": str,
        "method": str,
        "message": str,
        "days": int,
        "start_date": str,
        "end_date": str,
    }
    for key, expected_type in expected_types.items():
        raw = value.get(key)
        if raw is None:
            result[key] = None
            continue
        if type(raw) is not expected_type:
            raise WearableManagementPayloadError(
                f"open-wearables returned an invalid sync {key} value"
            )
        if expected_type is str:
            result[key] = (
                _safe_public_identifier(
                    raw,
                    f"sync {key}",
                    sensitive_values=sensitive_values,
                )
                if key == "task_id"
                else sanitize_public_text(
                    raw,
                    f"sync {key}",
                    sensitive_values=sensitive_values,
                )
            )
        else:
            result[key] = raw
    return result


def _safe_sync_event(
    value: object,
    resource: str,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> dict[str, Any]:
    row = _require_mapping(value, resource)
    provider = _provider(row.get("provider"), resource)
    run_id = _safe_public_identifier(
        row.get("run_id"),
        f"{resource} run_id",
        sensitive_values=sensitive_values,
    )
    source = _safe_public_identifier(
        row.get("source"),
        f"{resource} source",
        sensitive_values=sensitive_values,
    )
    stage = _safe_public_identifier(
        row.get("stage"),
        f"{resource} stage",
        sensitive_values=sensitive_values,
    )
    status = _safe_public_identifier(
        row.get("status"),
        f"{resource} status",
        sensitive_values=sensitive_values,
    )
    safe: dict[str, Any] = {
        "run_id": run_id,
        "provider": provider,
        "source": source,
        "stage": stage,
        "status": status,
        "message": (
            _safe_sync_message(
                row.get("message"),
                f"{resource} message",
                sensitive_values=sensitive_values,
            )
            if isinstance(row.get("message"), str)
            else None
        ),
        "progress": _safe_progress(row.get("progress"), resource),
        "items_processed": (
            row.get("items_processed")
            if type(row.get("items_processed")) is int
            else None
        ),
        "items_total": (
            row.get("items_total")
            if type(row.get("items_total")) is int
            else None
        ),
        "error": _safe_sync_error(
            row.get("error"),
            f"{resource} error",
            sensitive_values=sensitive_values,
        ),
        "started_at": _optional_datetime(row.get("started_at"), resource),
        "ended_at": _optional_datetime(row.get("ended_at"), resource),
        "last_update": _optional_datetime(row.get("last_update"), resource),
    }
    return safe


def _safe_progress(value: object, resource: str) -> float | None:
    if value is None:
        return None
    if type(value) not in {int, float} or not 0 <= float(value) <= 1:
        raise WearableManagementPayloadError(
            f"open-wearables returned an invalid {resource} progress value"
        )
    return float(value)


def _is_fixed_redirect_uri(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
        and "://" in value
    )


async def build_management_client(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> OpenWearablesManagementClient:
    """Build the management client through the shared single-user policy."""

    return await OpenWearablesManagementClient.from_settings(
        settings,
        transport=transport,
    )


__all__ = [
    "CONNECTION_STATUSES",
    "FORBIDDEN_AUTHORIZATION_QUERY_KEYS",
    "MANAGEMENT_SUPPORTED_PROVIDERS",
    "NATIVE_PROVIDERS",
    "OpenWearablesManagementClient",
    "SUPPORTED_PROVIDER_ORDER",
    "SUPPORTED_PROVIDERS",
    "WearableManagementAuthError",
    "WearableManagementCapabilityError",
    "WearableManagementConfigurationError",
    "WearableManagementConnectionError",
    "WearableManagementError",
    "WearableManagementNotFoundError",
    "WearableManagementPayloadError",
    "WearableManagementRejectedError",
    "WearableManagementUpstreamError",
    "build_management_client",
    "normalize_management_provider_identifier",
    "normalize_provider_identifier",
]
