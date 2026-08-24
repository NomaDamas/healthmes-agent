"""Audited coverage manifest for vendored Open Wearables v1 route decorators.

This module is deliberately declarative. Runtime/provider code must not infer
that a route is safe for an LLM merely because it appears here. The manifest
records which upstream routes are user-health reads, which are internal
identity/availability metadata, and which are outside the wearable capability
surface.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class OpenWearablesRouteClassification(StrEnum):
    """HealthMes treatment of one Open Wearables v1 route decorator."""

    EXPOSED_USER_HEALTH_READ = "exposed_user_health_read"
    INTERNAL_AVAILABILITY_IDENTITY_METADATA = (
        "internal_availability_identity_metadata"
    )
    INTENTIONALLY_EXCLUDED = "intentionally_excluded"


@dataclass(frozen=True, order=True, slots=True)
class OpenWearablesRouteIdentity:
    """Stable identity copied exactly from an upstream route decorator."""

    module: str
    method: str
    path: str


@dataclass(frozen=True, slots=True)
class OpenWearablesRouteCoverage:
    """Classification and audit rationale for an upstream route."""

    identity: OpenWearablesRouteIdentity
    classification: OpenWearablesRouteClassification
    reason: str
    capabilities: tuple[str, ...] = ()


_V1_MODULE_PREFIX = "app.api.routes.v1"


def _routes(
    module: str,
    classification: OpenWearablesRouteClassification,
    reason: str,
    *routes: tuple[str, str],
    capabilities: tuple[str, ...] = (),
) -> tuple[OpenWearablesRouteCoverage, ...]:
    return tuple(
        OpenWearablesRouteCoverage(
            identity=OpenWearablesRouteIdentity(
                module=f"{_V1_MODULE_PREFIX}.{module}",
                method=method,
                path=path,
            ),
            classification=classification,
            reason=reason,
            capabilities=capabilities,
        )
        for method, path in routes
    )


_HEALTH = OpenWearablesRouteClassification.EXPOSED_USER_HEALTH_READ
_METADATA = (
    OpenWearablesRouteClassification.INTERNAL_AVAILABILITY_IDENTITY_METADATA
)
_EXCLUDED = OpenWearablesRouteClassification.INTENTIONALLY_EXCLUDED


OPEN_WEARABLES_V1_ROUTE_COVERAGE: Final[
    tuple[OpenWearablesRouteCoverage, ...]
] = (
    # Canonical normalized user-health records.
    *_routes(
        "events",
        _HEALTH,
        "Reads normalized user-owned workout records.",
        ("GET", "/users/{user_id}/events/workouts"),
        capabilities=("wearable.workouts",),
    ),
    *_routes(
        "events",
        _HEALTH,
        "Reads normalized user-owned sleep-session records.",
        ("GET", "/users/{user_id}/events/sleep"),
        capabilities=("wearable.sleep-sessions",),
    ),
    *_routes(
        "events",
        _HEALTH,
        "Reads normalized user-owned menstrual-cycle records.",
        ("GET", "/users/{user_id}/events/menstrual-cycles"),
        capabilities=("wearable.menstrual-cycles",),
    ),
    *_routes(
        "health_scores",
        _HEALTH,
        "Reads normalized user-owned health scores with provider and time filters.",
        ("GET", "/users/{user_id}/health-scores"),
        capabilities=(
            "wearable.health-scores",
            "wearable.whoop-recovery-package",
        ),
    ),
    *_routes(
        "summaries",
        _HEALTH,
        "Reads an aggregate derived from the user's stored health observations.",
        ("GET", "/users/{user_id}/summaries/activity"),
        ("GET", "/users/{user_id}/summaries/sleep"),
        ("GET", "/users/{user_id}/summaries/recovery"),
        capabilities=("wearable.summaries",),
    ),
    *_routes(
        "summaries",
        _HEALTH,
        "Reads a bounded body composition and recent-vitals aggregate.",
        ("GET", "/users/{user_id}/summaries/body"),
        capabilities=("wearable.body-summary",),
    ),
    *_routes(
        "timeseries",
        _HEALTH,
        "Reads granular user-owned biometric or activity samples.",
        ("GET", "/users/{user_id}/timeseries"),
        capabilities=("wearable.timeseries",),
    ),
    *_routes(
        "vendor_workouts",
        _HEALTH,
        (
            "Reads bounded user-owned workout rows directly from a connected "
            "provider; sample, zone, and route fields require privacy gates."
        ),
        ("GET", "/{provider}/users/{user_id}/workouts"),
        capabilities=("wearable.provider-workouts",),
    ),
    *_routes(
        "vendor_workouts",
        _HEALTH,
        (
            "Reads one provider-owned workout by stable upstream workout ID; "
            "sample, zone, and route fields require privacy gates."
        ),
        ("GET", "/{provider}/users/{user_id}/workouts/{workout_id}"),
        capabilities=("wearable.provider-workout-detail",),
    ),
    # Read-only metadata used to decide whether and how health data can be read.
    *_routes(
        "connections",
        _METADATA,
        "Identifies the user's configured provider connections and capabilities.",
        ("GET", "/users/{user_id}/connections"),
    ),
    *_routes(
        "data_sources",
        _METADATA,
        "Identifies providers and device sources that hold data for the user.",
        ("GET", "/users/{user_id}/data-sources"),
    ),
    *_routes(
        "meta",
        _METADATA,
        "Describes provider-level metric coverage without returning user health values.",
        ("GET", "/meta/coverage"),
    ),
    *_routes(
        "oauth",
        _METADATA,
        "Lists configured provider availability without initiating OAuth or mutating settings.",
        ("GET", "/providers"),
    ),
    *_routes(
        "summaries",
        _METADATA,
        (
            "Describes which user-owned observation families and providers "
            "have data without returning the health observations themselves."
        ),
        ("GET", "/users/{user_id}/summaries/data"),
    ),
    *_routes(
        "users",
        _METADATA,
        "Resolves Open Wearables user identity; it does not return health observations.",
        ("GET", "/users"),
        ("GET", "/users/{user_id}"),
    ),
    # Authentication, administration, writes, ingestion, and operational control.
    *_routes(
        "api_keys",
        _EXCLUDED,
        "Developer API-key administration is outside the user-health capability surface.",
        ("GET", "/api-keys"),
        ("POST", "/api-keys"),
        ("DELETE", "/api-keys/{key_id}"),
        ("PATCH", "/api-keys/{key_id}"),
        ("POST", "/api-keys/{key_id}/rotate"),
    ),
    *_routes(
        "applications",
        _EXCLUDED,
        "Application credential administration is not a user-health data read.",
        ("GET", "/applications"),
        ("POST", "/applications"),
        ("DELETE", "/applications/{app_id}"),
        ("POST", "/applications/{app_id}/rotate-secret"),
    ),
    *_routes(
        "archival",
        _EXCLUDED,
        "Storage lifecycle settings and archival execution are operator controls.",
        ("GET", "/settings/archival"),
        ("PUT", "/settings/archival"),
        ("POST", "/settings/archival/run"),
    ),
    *_routes(
        "auth",
        _EXCLUDED,
        "Developer authentication and profile administration are not wearable-user identity.",
        ("POST", "/login"),
        ("POST", "/logout"),
        ("POST", "/change-password"),
        ("GET", "/me"),
        ("PATCH", "/me"),
    ),
    *_routes(
        "connections",
        _EXCLUDED,
        "Disconnecting a provider mutates credentials and connection state.",
        ("DELETE", "/users/{user_id}/connections/{provider}"),
    ),
    *_routes(
        "dashboard",
        _EXCLUDED,
        "Platform-wide operational statistics are not user health observations.",
        ("GET", "/stats"),
    ),
    *_routes(
        "deprecated_webhooks",
        _EXCLUDED,
        "Deprecated provider webhook endpoints ingest external events.",
        ("POST", "/garmin/webhooks/ping"),
        ("POST", "/garmin/webhooks/push"),
    ),
    *_routes(
        "developers",
        _EXCLUDED,
        "Operator/developer identity administration is not wearable-user identity.",
        ("GET", ""),
        ("GET", "/{developer_id}"),
        ("PATCH", "/{developer_id}"),
        ("DELETE", "/{developer_id}"),
    ),
    *_routes(
        "events",
        _EXCLUDED,
        "Deleting canonical event records mutates the user's health-data store.",
        ("DELETE", "/users/{user_id}/events/workouts/{workout_id}"),
        ("DELETE", "/users/{user_id}/events/sleep/{sleep_id}"),
        (
            "DELETE",
            "/users/{user_id}/events/menstrual-cycles/{cycle_id}",
        ),
    ),
    *_routes(
        "import_xml",
        _EXCLUDED,
        "Apple Health XML and SNS endpoints ingest or trigger writes to health data.",
        ("POST", "/users/{user_id}/import/apple/xml/s3"),
        ("POST", "/users/{user_id}/import/apple/xml/direct"),
        ("POST", "/sns/notification"),
    ),
    *_routes(
        "invitations",
        _EXCLUDED,
        "Developer invitation administration is unrelated to user-health reads.",
        ("POST", ""),
        ("GET", ""),
        ("DELETE", "/{invitation_id}"),
        ("POST", "/{invitation_id}/resend"),
        ("POST", "/accept"),
    ),
    *_routes(
        "oauth",
        _EXCLUDED,
        "OAuth control flow and provider-setting writes are not health-data reads.",
        ("GET", "/{provider}/authorize"),
        ("GET", "/{provider}/callback"),
        ("GET", "/success"),
        ("GET", "/error"),
        ("PUT", "/providers/{provider}"),
        ("PUT", "/providers"),
    ),
    *_routes(
        "oura_webhooks",
        _EXCLUDED,
        "Oura webhook ingress, verification, health checks, and subscription "
        "control are operational.",
        ("POST", ""),
        ("GET", ""),
        ("GET", "/health"),
        ("POST", "/subscriptions"),
        ("GET", "/subscriptions"),
        ("POST", "/subscriptions/renew"),
    ),
    *_routes(
        "outgoing_webhooks",
        _EXCLUDED,
        "Outgoing webhook configuration, secrets, and delivery audit are operational controls.",
        ("POST", "/endpoints"),
        ("GET", "/endpoints"),
        ("GET", "/endpoints/{endpoint_id}"),
        ("PATCH", "/endpoints/{endpoint_id}"),
        ("DELETE", "/endpoints/{endpoint_id}"),
        ("GET", "/endpoints/{endpoint_id}/secret"),
        ("GET", "/event-types"),
        ("GET", "/messages"),
        ("GET", "/endpoints/{endpoint_id}/attempts"),
        ("POST", "/endpoints/{endpoint_id}/test"),
    ),
    *_routes(
        "priorities",
        _EXCLUDED,
        (
            "Open Wearables source-priority administration is configuration, "
            "not connection availability or a health observation."
        ),
        ("GET", "/priorities/providers"),
        ("PUT", "/priorities/providers/{provider}"),
        ("PUT", "/priorities/providers"),
        ("GET", "/priorities/device-types"),
        ("PUT", "/priorities/device-types/{device_type}"),
        ("PUT", "/priorities/device-types"),
    ),
    *_routes(
        "sdk_logs",
        _EXCLUDED,
        "Mobile SDK log submission is diagnostic ingestion.",
        ("POST", "/sdk/users/{user_id}/logs"),
    ),
    *_routes(
        "sdk_sync",
        _EXCLUDED,
        "Mobile SDK synchronization ingests user health data.",
        ("POST", "/sdk/users/{user_id}/sync"),
    ),
    *_routes(
        "sdk_token",
        _EXCLUDED,
        "Mobile SDK token issuance is an authentication operation.",
        ("POST", "/users/{user_id}/token"),
    ),
    *_routes(
        "seed_data",
        _EXCLUDED,
        "Seed generation and fixture catalogs are development/operator utilities.",
        ("POST", "/settings/seed"),
        ("GET", "/settings/seed/presets"),
        ("GET", "/settings/seed/sleep-profiles"),
    ),
    *_routes(
        "strava_webhooks",
        _EXCLUDED,
        "Strava webhook ingress, verification, and health checks are operational.",
        ("GET", ""),
        ("POST", ""),
        ("GET", "/health"),
    ),
    *_routes(
        "sync_data",
        _EXCLUDED,
        "Provider synchronization and backfill status/control are operational, "
        "not stored-data reads.",
        ("POST", "/{provider}/users/{user_id}/sync"),
        ("GET", "/garmin/users/{user_id}/backfill/status"),
        ("POST", "/garmin/users/{user_id}/backfill/cancel"),
        (
            "POST",
            "/garmin/users/{user_id}/backfill/{type_name}/retry",
        ),
        ("POST", "/{provider}/users/{user_id}/sync/historical"),
    ),
    *_routes(
        "sync_status",
        _EXCLUDED,
        "Sync streams and run logs are operational telemetry, not wearable observations.",
        ("GET", "/users/{user_id}/sync/stream"),
        ("GET", "/users/{user_id}/sync/recent"),
        ("GET", "/users/{user_id}/sync/runs"),
        ("GET", "/sync/runs"),
    ),
    *_routes(
        "token",
        _EXCLUDED,
        "Refresh-token lifecycle operations are authentication controls.",
        ("POST", "/token/refresh"),
        ("POST", "/token/revoke"),
    ),
    *_routes(
        "user_invitation_code",
        _EXCLUDED,
        "Mobile invitation-code issuance and redemption are enrollment controls.",
        ("POST", "/users/{user_id}/invitation-code"),
        ("POST", "/invitation-code/redeem"),
    ),
    *_routes(
        "users",
        _EXCLUDED,
        "Creating, deleting, or editing an Open Wearables user mutates identity state.",
        ("POST", "/users"),
        ("DELETE", "/users/{user_id}"),
        ("PATCH", "/users/{user_id}"),
    ),
    *_routes(
        "webhooks",
        _EXCLUDED,
        "Generic provider webhook ingress and subscription control are operational.",
        ("POST", ""),
        ("GET", ""),
        ("GET", "/subscriptions"),
        ("POST", "/subscriptions"),
        ("POST", "/subscriptions/renew"),
        ("GET", "/subscriptions/{subscription_id}"),
        ("DELETE", "/subscriptions/{subscription_id}"),
        ("PUT", "/subscriptions/{subscription_id}"),
    ),
)


OPEN_WEARABLES_V1_ROUTE_BY_IDENTITY: Final = MappingProxyType(
    {entry.identity: entry for entry in OPEN_WEARABLES_V1_ROUTE_COVERAGE}
)

OPEN_WEARABLES_V1_CLASSIFICATION_COUNTS: Final = MappingProxyType(
    dict(
        Counter(
            entry.classification
            for entry in OPEN_WEARABLES_V1_ROUTE_COVERAGE
        )
    )
)

OPEN_WEARABLES_V1_ROUTE_COUNT: Final = len(
    OPEN_WEARABLES_V1_ROUTE_COVERAGE
)

OPEN_WEARABLES_V1_EXPOSED_CAPABILITIES: Final = frozenset(
    capability
    for entry in OPEN_WEARABLES_V1_ROUTE_COVERAGE
    if entry.classification
    is OpenWearablesRouteClassification.EXPOSED_USER_HEALTH_READ
    for capability in entry.capabilities
)


__all__ = [
    "OPEN_WEARABLES_V1_CLASSIFICATION_COUNTS",
    "OPEN_WEARABLES_V1_EXPOSED_CAPABILITIES",
    "OPEN_WEARABLES_V1_ROUTE_BY_IDENTITY",
    "OPEN_WEARABLES_V1_ROUTE_COUNT",
    "OPEN_WEARABLES_V1_ROUTE_COVERAGE",
    "OpenWearablesRouteClassification",
    "OpenWearablesRouteCoverage",
    "OpenWearablesRouteIdentity",
]
