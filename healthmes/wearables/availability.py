"""Dynamic Open Wearables availability for HealthMes decision sessions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from healthmes.config import Settings
from healthmes.mcp_server.ow_client import (
    OWClient,
    OWClientError,
    resolve_single_user_id,
)
from healthmes.source_policy import (
    OPEN_WEARABLES_INPUT_SOURCE_ID,
    InputSourcePolicyBinding,
    input_source_policy_binding,
)
from healthmes.store import (
    WellnessEvent,
)
from healthmes.wearables.open_wearables_routes import (
    OPEN_WEARABLES_V1_EXPOSED_CAPABILITIES,
)
from healthmes.wearables.provenance import (
    OPEN_WEARABLES_OBSERVATION_EVENT_TYPE,
    OPEN_WEARABLES_QUERY_EVENT_TYPE,
    OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER,
    wearable_query_snapshot_from_event,
    wearable_snapshot_from_event,
)

WEARABLE_INPUT_DISABLED = "wearable_input_disabled"
OPEN_WEARABLES_UNCONFIGURED = "open_wearables_unconfigured"
OPEN_WEARABLES_DISCONNECTED = "open_wearables_disconnected"
OPEN_WEARABLES_METADATA_DEGRADED = "open_wearables_metadata_degraded"
OPEN_WEARABLES_METADATA_UNAVAILABLE = (
    "open_wearables_metadata_unavailable"
)
OPEN_WEARABLES_SOURCE_SETTING_UNAVAILABLE = (
    "open_wearables_source_setting_unavailable"
)
OPEN_WEARABLES_SOURCE_POLICY_CHANGED = (
    "open_wearables_source_policy_changed"
)
_RETAINED_OPEN_WEARABLES_EVENT_TYPES = (
    OPEN_WEARABLES_OBSERVATION_EVENT_TYPE,
    OPEN_WEARABLES_QUERY_EVENT_TYPE,
)
OPEN_WEARABLES_BACKED_CAPABILITIES = (
    OPEN_WEARABLES_V1_EXPOSED_CAPABILITIES
    | frozenset(
        {
            "wearable.metric-detail",
            "wearable.readiness",
            "wearable.recovery",
            "wearable.sleep",
            "wearable.stress",
        }
    )
)


class OpenWearablesAvailabilityState(StrEnum):
    """Why Open Wearables capabilities are or are not currently usable."""

    DISABLED = "disabled"
    UNCONFIGURED = "unconfigured"
    DISCONNECTED = "disconnected"
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class OpenWearablesAvailabilitySnapshot(BaseModel):
    """Non-sensitive availability snapshot frozen into a decision session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: OpenWearablesAvailabilityState
    observed_at: AwareDatetime
    source_policy_revision: int = Field(default=0, ge=0)
    reason_codes: tuple[str, ...] = Field(default=(), max_length=8)

    @property
    def exposes_capabilities(self) -> bool:
        return self.state in {
            OpenWearablesAvailabilityState.AVAILABLE,
            OpenWearablesAvailabilityState.DEGRADED,
        }

    @property
    def blocking_reason_code(self) -> str | None:
        if OPEN_WEARABLES_SOURCE_SETTING_UNAVAILABLE in self.reason_codes:
            return OPEN_WEARABLES_SOURCE_SETTING_UNAVAILABLE
        if OPEN_WEARABLES_METADATA_UNAVAILABLE in self.reason_codes:
            return OPEN_WEARABLES_METADATA_UNAVAILABLE
        if self.state is OpenWearablesAvailabilityState.DISABLED:
            return WEARABLE_INPUT_DISABLED
        if self.state is OpenWearablesAvailabilityState.UNCONFIGURED:
            return OPEN_WEARABLES_UNCONFIGURED
        if self.state is OpenWearablesAvailabilityState.DISCONNECTED:
            return OPEN_WEARABLES_DISCONNECTED
        if self.state is OpenWearablesAvailabilityState.UNAVAILABLE:
            return OPEN_WEARABLES_METADATA_UNAVAILABLE
        return None


# Compatibility name for the initial #196 implementation draft.
OpenWearablesAvailability = OpenWearablesAvailabilitySnapshot


class OpenWearablesAvailabilityResolver:
    """Combine the source switch with bounded Open Wearables metadata reads."""

    def __init__(
        self,
        *,
        settings: Settings,
        client: OWClient,
        session_factory: sessionmaker[Session],
        timeout_seconds: float = 5.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 30:
            raise ValueError(
                "Open Wearables availability timeout must be within (0, 30]"
            )
        self._settings = settings
        self._client = client
        self._session_factory = session_factory
        self._timeout_seconds = timeout_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    async def __call__(self) -> OpenWearablesAvailabilitySnapshot:
        observed_at = _utc(self._clock())
        try:
            source_policy = await asyncio.to_thread(
                self._source_policy_binding
            )
        except Exception:
            return OpenWearablesAvailabilitySnapshot(
                state=OpenWearablesAvailabilityState.DISABLED,
                observed_at=observed_at,
                reason_codes=(
                    OPEN_WEARABLES_SOURCE_SETTING_UNAVAILABLE,
                ),
            )
        if not source_policy.enabled:
            return OpenWearablesAvailabilitySnapshot(
                state=OpenWearablesAvailabilityState.DISABLED,
                observed_at=observed_at,
                source_policy_revision=source_policy.revision,
                reason_codes=(WEARABLE_INPUT_DISABLED,),
            )

        if not _settings_are_configured(self._settings):
            return OpenWearablesAvailabilitySnapshot(
                state=OpenWearablesAvailabilityState.UNCONFIGURED,
                observed_at=observed_at,
                source_policy_revision=source_policy.revision,
                reason_codes=(OPEN_WEARABLES_UNCONFIGURED,),
            )

        try:
            async with asyncio.timeout(self._timeout_seconds):
                user_id = await resolve_single_user_id(
                    self._client,
                    self._settings,
                )
        except LookupError:
            return OpenWearablesAvailabilitySnapshot(
                state=OpenWearablesAvailabilityState.UNCONFIGURED,
                observed_at=observed_at,
                source_policy_revision=source_policy.revision,
                reason_codes=(OPEN_WEARABLES_UNCONFIGURED,),
            )
        except (TimeoutError, OWClientError):
            return await self._metadata_failure(
                observed_at,
                source_policy_revision=source_policy.revision,
            )

        try:
            async with asyncio.timeout(self._timeout_seconds):
                connections, data_sources = await self._read_metadata(user_id)
        except (TimeoutError, OWClientError):
            return await self._metadata_failure(
                observed_at,
                source_policy_revision=source_policy.revision,
            )

        active_connection_ids = _active_connection_ids(connections)
        if _has_active_connection(connections) or _has_data_source(
            data_sources,
            active_connection_ids=active_connection_ids,
        ):
            return OpenWearablesAvailabilitySnapshot(
                state=OpenWearablesAvailabilityState.AVAILABLE,
                observed_at=observed_at,
                source_policy_revision=source_policy.revision,
            )
        return OpenWearablesAvailabilitySnapshot(
            state=OpenWearablesAvailabilityState.DISCONNECTED,
            observed_at=observed_at,
            source_policy_revision=source_policy.revision,
            reason_codes=(OPEN_WEARABLES_DISCONNECTED,),
        )

    async def _read_metadata(
        self,
        user_id: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Read both metadata endpoints and clean up a sibling on failure."""

        tasks = (
            asyncio.create_task(
                self._client.get_connections(user_id),
                name="open-wearables-connections",
            ),
            asyncio.create_task(
                self._client.get_user_data_sources(user_id),
                name="open-wearables-data-sources",
            ),
        )
        try:
            return await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _metadata_failure(
        self,
        observed_at: datetime,
        *,
        source_policy_revision: int,
    ) -> OpenWearablesAvailabilitySnapshot:
        if await asyncio.to_thread(
            self._has_retained_snapshot,
            observed_at,
        ):
            return OpenWearablesAvailabilitySnapshot(
                state=OpenWearablesAvailabilityState.DEGRADED,
                observed_at=observed_at,
                source_policy_revision=source_policy_revision,
                reason_codes=(OPEN_WEARABLES_METADATA_DEGRADED,),
            )
        return OpenWearablesAvailabilitySnapshot(
            state=OpenWearablesAvailabilityState.UNAVAILABLE,
            observed_at=observed_at,
            source_policy_revision=source_policy_revision,
            reason_codes=(OPEN_WEARABLES_METADATA_UNAVAILABLE,),
        )

    def _has_retained_snapshot(self, now: datetime) -> bool:
        """Allow degraded mode only with a non-expired local last-known-good."""

        try:
            with self._session_factory() as session:
                events = session.scalars(
                    select(WellnessEvent.id)
                    .where(
                        WellnessEvent.event_type.in_(
                            _RETAINED_OPEN_WEARABLES_EVENT_TYPES
                        ),
                        WellnessEvent.source_provider
                        == OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER,
                        (
                            WellnessEvent.expires_at.is_(None)
                            | (WellnessEvent.expires_at > now)
                        ),
                    )
                    .order_by(
                        WellnessEvent.recorded_at.desc(),
                        WellnessEvent.created_at.desc(),
                    )
                    .limit(32)
                )
                for event_id in events:
                    event = session.get(WellnessEvent, event_id)
                    if event is None:
                        continue
                    if (
                        event.event_type
                        == OPEN_WEARABLES_QUERY_EVENT_TYPE
                        and wearable_query_snapshot_from_event(
                            session,
                            event,
                            now=now,
                        )
                        is not None
                    ):
                        return True
                    if (
                        event.event_type
                        == OPEN_WEARABLES_OBSERVATION_EVENT_TYPE
                        and wearable_snapshot_from_event(
                            session,
                            event,
                            now=now,
                        )
                        is not None
                    ):
                        return True
                return False
        except Exception:
            return False

    def _source_policy_binding(self) -> InputSourcePolicyBinding:
        with self._session_factory() as session:
            return input_source_policy_binding(
                session,
                owner_principal_id=(
                    self._settings.decision_owner_principal_id
                ),
                source_id=OPEN_WEARABLES_INPUT_SOURCE_ID,
            )


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )


def _settings_are_configured(settings: Settings) -> bool:
    return bool(
        settings.ow_base_url.strip()
        and settings.ow_api_key.get_secret_value().strip()
    )


def _has_active_connection(connections: Any) -> bool:
    if not isinstance(connections, list):
        return False
    return any(
        isinstance(item, Mapping)
        and str(item.get("status", "")).strip().casefold() == "active"
        for item in connections
    )


def _active_connection_ids(connections: Any) -> frozenset[str]:
    if not isinstance(connections, list):
        return frozenset()
    return frozenset(
        str(item["id"])
        for item in connections
        if isinstance(item, Mapping)
        and str(item.get("status", "")).strip().casefold() == "active"
        and item.get("id") is not None
    )


def _has_data_source(
    payload: Any,
    *,
    active_connection_ids: frozenset[str],
) -> bool:
    if not isinstance(payload, Mapping):
        return False
    items = payload.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, Mapping):
                continue
            connection_id = item.get("user_connection_id")
            if connection_id is None:
                # Open Wearables uses a null connection for one-time imports.
                return True
            if str(connection_id) in active_connection_ids:
                return True
    total = payload.get("total")
    return False if items is not None else (
        isinstance(total, int)
        and not isinstance(total, bool)
        and total > 0
    )


__all__ = [
    "OPEN_WEARABLES_BACKED_CAPABILITIES",
    "OPEN_WEARABLES_INPUT_SOURCE_ID",
    "OPEN_WEARABLES_DISCONNECTED",
    "OPEN_WEARABLES_METADATA_DEGRADED",
    "OPEN_WEARABLES_METADATA_UNAVAILABLE",
    "OPEN_WEARABLES_SOURCE_SETTING_UNAVAILABLE",
    "OPEN_WEARABLES_SOURCE_POLICY_CHANGED",
    "OPEN_WEARABLES_UNCONFIGURED",
    "WEARABLE_INPUT_DISABLED",
    "OpenWearablesAvailability",
    "OpenWearablesAvailabilityResolver",
    "OpenWearablesAvailabilitySnapshot",
    "OpenWearablesAvailabilityState",
]
