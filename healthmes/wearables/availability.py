"""Dynamic Open Wearables availability for HealthMes decision sessions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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
from healthmes.wearables.lineage import (
    OpenWearablesLineageMode,
    open_wearables_lineage_mode,
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
OPEN_WEARABLES_PROVIDER_BINDING_CHANGED = (
    "open_wearables_provider_binding_changed"
)
_RETAINED_OPEN_WEARABLES_EVENT_TYPES = (
    OPEN_WEARABLES_OBSERVATION_EVENT_TYPE,
    OPEN_WEARABLES_QUERY_EVENT_TYPE,
)

_RETAINED_EMPTY_STATUSES = frozenset(
    {
        "empty_success",
        "failed",
        "insufficient_data",
        "no_data",
        "unavailable",
    }
)
_DAILY_EVIDENCE_FIELDS = {
    "wearable.metric-detail": (
        "actual_sleep",
        "charge",
        "hrv",
        "sleep_debt",
        "stress",
        "yesterday_load",
    ),
    "wearable.readiness": (
        "actual_sleep",
        "charge",
        "hrv",
        "sleep_debt",
        "stress",
        "yesterday_load",
    ),
    "wearable.recovery": ("charge", "hrv", "yesterday_load"),
    "wearable.sleep": ("actual_sleep", "hrv", "sleep_debt"),
    "wearable.stress": ("stress",),
}

_PROVIDER_ALIASES = {
    "apple": "apple_health",
    "apple_health": "apple_health",
    "applehealth": "apple_health",
    "fitbit": "fitbit",
    "garmin": "garmin",
    "google": "google_health_connect",
    "google_health_connect": "google_health_connect",
    "oura": "oura",
    "polar": "polar",
    "samsung": "samsung_health",
    "samsung_health": "samsung_health",
    "strava": "strava",
    "suunto": "suunto",
    "ultrahuman": "ultrahuman",
    "whoop": "whoop",
}
_DIRECT_PROVIDER_CAPABILITIES = frozenset(
    {"garmin", "polar", "suunto"}
)
_HEALTH_SCORE_CAPABILITIES = frozenset(
    {
        "wearable.health-scores",
        "wearable.whoop-recovery-package",
    }
)
_ACTIVITY_SERIES = frozenset(
    {
        "active_time",
        "energy",
        "exercise_time",
        "flights_climbed",
        "physical_effort",
        "steps",
        "stand_time",
    }
)
_BODY_SERIES = frozenset(
    {
        "blood_pressure_diastolic",
        "blood_pressure_systolic",
        "body_fat_percentage",
        "body_mass_index",
        "body_temperature",
        "height",
        "heart_rate_variability_rmssd",
        "heart_rate_variability_sdnn",
        "lean_body_mass",
        "resting_heart_rate",
        "skin_temperature",
        "weight",
    }
)
_SEARCH_TIMESERIES = frozenset(
    {
        "active_time",
        "body_temperature",
        "energy",
        "exercise_time",
        "garmin_body_battery",
        "garmin_stress_level",
        "heart_rate",
        "heart_rate_variability_rmssd",
        "heart_rate_variability_sdnn",
        "oxygen_saturation",
        "physical_effort",
        "respiratory_rate",
        "resting_heart_rate",
        "skin_temperature",
        "skin_temperature_deviation",
        "stand_time",
        "steps",
        "time_in_daylight",
        "vo2_max",
    }
)
_SEARCH_SCORE_CATEGORIES = frozenset(
    {
        "activity",
        "body_battery",
        "day_strain",
        "readiness",
        "recovery",
        "resilience",
        "sleep",
        "strain",
        "stress",
    }
)
_SEARCH_SUMMARY_KINDS = frozenset(
    {"activity", "recovery", "sleep"}
)
_SEARCH_PROVIDER_WORKOUTS = frozenset(
    {"garmin", "polar", "suunto"}
)
_MENSTRUAL_CYCLE_PROVIDERS = frozenset({"garmin"})


def _canonical_provider(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return _PROVIDER_ALIASES.get(value.strip().casefold())


def _sorted_unique(values: Any) -> tuple[str, ...]:
    if not isinstance(values, list | tuple | set | frozenset):
        return ()
    return tuple(sorted({str(value) for value in values if str(value)}))


class OpenWearablesProviderCoverage(BaseModel):
    """Provider coverage metadata used to build a safe capability catalog."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timeseries: tuple[str, ...] = ()
    workout_fields: tuple[str, ...] = ()
    sleep_fields: tuple[str, ...] = ()
    health_scores: tuple[str, ...] = ()


class OpenWearablesProviderInventory(BaseModel):
    """User-owned inventory counts for one provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    series_types: tuple[str, ...] = ()
    data_points: int = Field(default=0, ge=0)
    workout_count: int = Field(default=0, ge=0)
    sleep_count: int = Field(default=0, ge=0)
    has_womens_health_data: bool = False


class OpenWearablesProviderBinding(BaseModel):
    """Non-sensitive, session-safe binding for one active provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=64)
    direct_api: bool = False
    imported: bool = False
    capabilities: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.direct_api or self.imported


class OpenWearablesDataSourceIdentity(BaseModel):
    """Private metadata for attributing rows that omit ``data_source_id``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_source_id: str = Field(min_length=1, max_length=128)
    device_labels: tuple[str, ...] = Field(default=(), max_length=8)


class OpenWearablesProviderSourceBinding(BaseModel):
    """Private source lineage for one provider availability binding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=64)
    active_connection_ids: tuple[str, ...] = Field(
        default=(),
        max_length=64,
    )
    direct_data_source_ids: tuple[str, ...] = Field(
        default=(),
        max_length=128,
    )
    import_data_source_ids: tuple[str, ...] = Field(
        default=(),
        max_length=128,
    )
    data_sources: tuple[OpenWearablesDataSourceIdentity, ...] = Field(
        default=(),
        max_length=128,
        exclude=True,
        repr=False,
    )


class OpenWearablesParameterValueBinding(BaseModel):
    """Provider ownership for one concrete capability parameter value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str = Field(min_length=1, max_length=128)
    providers: tuple[str, ...] = Field(min_length=1, max_length=16)


class OpenWearablesParameterAllowlist(BaseModel):
    """Model-visible values allowed for one capability parameter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    values: tuple[str, ...] = Field(min_length=1, max_length=128)
    providers: tuple[str, ...] = Field(default=(), max_length=16)
    value_bindings: tuple[OpenWearablesParameterValueBinding, ...] = Field(
        default=(),
        max_length=128,
    )

    def providers_for(self, value: str) -> frozenset[str]:
        """Return the providers that own one allowlisted value.

        The public catalog intentionally keeps the mapping compact: values
        remain model-visible while connection and data-source identities stay
        private in the binding digest.
        """

        normalized = value.strip().casefold()
        for binding in self.value_bindings:
            if binding.value.strip().casefold() == normalized:
                return frozenset(binding.providers)
        return frozenset()


class OpenWearablesCapabilityBinding(BaseModel):
    """Public provider ownership and bounded values for one capability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: str = Field(min_length=1, max_length=128)
    providers: tuple[str, ...] = Field(min_length=1, max_length=16)
    # ``None`` is retained for snapshots assembled by older callers.  The
    # resolver-generated catalog always sets an explicit mode; the private
    # execution binder treats legacy snapshots as unbound compatibility data.
    lineage_mode: OpenWearablesLineageMode | None = None
    parameters: tuple[OpenWearablesParameterAllowlist, ...] = Field(
        default=(),
        max_length=16,
    )

    def allowed_values(self, name: str) -> frozenset[str] | None:
        normalized = name.strip().casefold()
        for item in self.parameters:
            if item.name == normalized:
                return frozenset(item.values)
        return None

    def providers_for_value(
        self,
        name: str,
        value: str,
    ) -> frozenset[str]:
        normalized = name.strip().casefold()
        for item in self.parameters:
            if item.name == normalized:
                return item.providers_for(value)
        return frozenset()


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
    provider_catalog_version: int = Field(default=0, ge=0, le=1)
    providers: tuple[str, ...] = Field(default=(), max_length=16)
    provider_bindings: tuple[OpenWearablesProviderBinding, ...] = Field(
        default=(),
        max_length=16,
    )
    capability_catalog: tuple[OpenWearablesCapabilityBinding, ...] = Field(
        default=(),
        max_length=32,
    )
    provider_binding_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
        exclude=True,
    )
    provider_coverage_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
        exclude=True,
        repr=False,
    )
    provider_source_bindings: tuple[
        OpenWearablesProviderSourceBinding, ...
    ] = Field(
        default=(),
        max_length=16,
        exclude=True,
        repr=False,
    )

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
        if OPEN_WEARABLES_SOURCE_POLICY_CHANGED in self.reason_codes:
            return OPEN_WEARABLES_SOURCE_POLICY_CHANGED
        if OPEN_WEARABLES_PROVIDER_BINDING_CHANGED in self.reason_codes:
            return OPEN_WEARABLES_PROVIDER_BINDING_CHANGED
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

    @property
    def available_capabilities(self) -> frozenset[str]:
        return frozenset(item.capability for item in self.capability_catalog)

    def capability_binding(
        self,
        capability: str,
    ) -> OpenWearablesCapabilityBinding | None:
        normalized = capability.strip().casefold()
        return next(
            (
                item
                for item in self.capability_catalog
                if item.capability == normalized
            ),
            None,
        )

    def providers_for(self, capability: str) -> frozenset[str]:
        binding = self.capability_binding(capability)
        return (
            frozenset(binding.providers)
            if binding is not None
            else frozenset()
        )

    def parameter_values(
        self,
        capability: str,
        parameter: str,
    ) -> frozenset[str] | None:
        binding = self.capability_binding(capability)
        return (
            binding.allowed_values(parameter)
            if binding is not None
            else None
        )


# Compatibility name for the initial #196 implementation draft.
OpenWearablesAvailability = OpenWearablesAvailabilitySnapshot


@dataclass(frozen=True, slots=True)
class _OpenWearablesMetadataRead:
    connections: Any
    data_sources: Any
    coverage: Any
    inventory: Any

    @property
    def complete(self) -> bool:
        return all(
            not isinstance(value, BaseException)
            for value in (
                self.connections,
                self.data_sources,
                self.coverage,
                self.inventory,
            )
        )


@dataclass(slots=True)
class _RetainedEvidenceIndex:
    """Capability/provider evidence proven by retained HealthMes events."""

    providers_by_capability: defaultdict[str, set[str]]
    providers_by_parameter_value: defaultdict[
        tuple[str, str, str],
        set[str],
    ]

    @classmethod
    def create(cls) -> _RetainedEvidenceIndex:
        return cls(
            providers_by_capability=defaultdict(set),
            providers_by_parameter_value=defaultdict(set),
        )

    def add(
        self,
        *,
        capability: str,
        providers: set[str],
        parameters: Mapping[str, Any] = (),
    ) -> None:
        normalized_capability = capability.strip().casefold()
        if not providers:
            return
        self.providers_by_capability[normalized_capability].update(
            providers
        )
        if not isinstance(parameters, Mapping):
            return
        for name, value in parameters.items():
            if not isinstance(name, str) or not isinstance(value, str):
                continue
            self.providers_by_parameter_value[
                (normalized_capability, name, value)
            ].update(providers)


_RETAINED_TRUSTED_PROVIDER_ATTRIBUTIONS = frozenset(
    {
        "allowed_provider_binding",
        "declared",
        "source_exact_alias",
    }
)
_RETAINED_PARAMETER_NAMES = {
    "wearable.health-scores": "category",
    "wearable.summaries": "summary_kind",
    "wearable.timeseries": "series_type",
    "wearable.provider-workouts": "provider",
    "wearable.provider-workout-detail": "provider",
}


def _retained_source_allowlist(
    snapshot: OpenWearablesAvailabilitySnapshot,
) -> dict[str, frozenset[str]]:
    """Return private source IDs from the frozen LKG binding."""

    return {
        binding.provider: frozenset(
            (
                *binding.direct_data_source_ids,
                *binding.import_data_source_ids,
            )
        )
        for binding in snapshot.provider_source_bindings
    }


def _retained_record_provider(
    record: Mapping[str, Any],
    *,
    source_allowlist: Mapping[str, frozenset[str]],
) -> str | None:
    """Resolve provider ownership without guessing from device labels."""

    provider = _canonical_provider(record.get("provider"))
    attribution = record.get("provider_attribution")
    if (
        provider is None
        or attribution not in _RETAINED_TRUSTED_PROVIDER_ATTRIBUTIONS
    ):
        return None
    source = record.get("source")
    if isinstance(source, Mapping):
        source_provider = _canonical_provider(source.get("provider"))
        if (
            source.get("provider") is not None
            and source_provider != provider
        ):
            return None
    source_id = record.get("data_source_id")
    if source_id is None and isinstance(source, Mapping):
        source_id = source.get("data_source_id")
    if source_id is not None:
        if not isinstance(source_id, str) or not source_id:
            return None
        if source_id not in source_allowlist.get(provider, frozenset()):
            return None
    return provider


def _retained_query_evidence(
    index: _RetainedEvidenceIndex,
    event: WellnessEvent,
    snapshot: Any,
    *,
    source_allowlist: Mapping[str, frozenset[str]],
) -> None:
    """Project one already-validated query mirror into provider evidence."""

    capability = str(snapshot.capability).strip().casefold()
    result = snapshot.result
    if not isinstance(result, Mapping):
        return
    status = result.get("status")
    if isinstance(status, str) and status.strip().casefold() in (
        _RETAINED_EMPTY_STATUSES
    ):
        return

    if capability == "wearable.whoop-recovery-package":
        # The package is valid only when both Sake signals survived validation.
        payload = event.payload
        private_provenance = (
            payload.get("private_provenance")
            if isinstance(payload, Mapping)
            else None
        )
        if not isinstance(private_provenance, list):
            return
        metrics = {
            row.get("metric")
            for row in private_provenance
            if isinstance(row, Mapping)
            and row.get("source_provider") == "open-wearables"
            and row.get("upstream_provider") == "whoop"
        }
        if metrics != {"recovery", "day_strain"}:
            return
        index.add(
            capability=capability,
            providers={"whoop"},
        )
        return

    records = result.get("records")
    if not isinstance(records, list) or not records:
        return
    parameter_name = _RETAINED_PARAMETER_NAMES.get(capability)
    for raw_record in records:
        if not isinstance(raw_record, Mapping):
            continue
        provider = _retained_record_provider(
            raw_record,
            source_allowlist=source_allowlist,
        )
        if provider is None:
            continue
        parameters: dict[str, str] = {}
        if parameter_name is not None:
            value = raw_record.get(parameter_name)
            if not isinstance(value, str) or not value:
                continue
            parameters[parameter_name] = value
        index.add(
            capability=capability,
            providers={provider},
            parameters=parameters,
        )


def _retained_observation_evidence(
    index: _RetainedEvidenceIndex,
    snapshot: Any,
    *,
    allowed_providers: frozenset[str],
) -> None:
    """Project explicit provider-attributed daily context evidence only."""

    context = snapshot.normalized_context
    if not isinstance(context, Mapping):
        return
    raw_refs = context.get("source_refs")
    if not isinstance(raw_refs, Sequence) or isinstance(
        raw_refs,
        str | bytes,
    ):
        return
    providers: set[str] = set()
    for raw_ref in raw_refs:
        if not isinstance(raw_ref, Mapping):
            continue
        if raw_ref.get("source_provider") != "open-wearables":
            continue
        provider = _canonical_provider(raw_ref.get("upstream_provider"))
        if provider is None or provider not in allowed_providers:
            continue
        if not isinstance(raw_ref.get("record_id"), str):
            continue
        providers.add(provider)
    if not providers:
        return

    def field_is_present(name: str) -> bool:
        value = context.get(name)
        if value is None:
            return False
        if isinstance(value, Mapping):
            state = value.get("status")
            if (
                isinstance(state, str)
                and state.strip().casefold() in _RETAINED_EMPTY_STATUSES
            ):
                return False
        return True

    for capability, fields in _DAILY_EVIDENCE_FIELDS.items():
        if all(field_is_present(field) for field in fields):
            index.add(
                capability=capability,
                providers=set(providers),
            )


def _retained_evidence_index(
    session_factory: sessionmaker[Session],
    *,
    snapshot: OpenWearablesAvailabilitySnapshot,
    now: datetime,
    expected_provider_binding_digest: str,
) -> _RetainedEvidenceIndex:
    """Scan all retained mirrors and return only validated positive evidence."""

    index = _RetainedEvidenceIndex.create()
    current = _utc(now)
    source_allowlist = _retained_source_allowlist(snapshot)
    allowed_providers = frozenset(
        item.provider for item in snapshot.provider_bindings
    )
    with session_factory() as session:
        events = session.scalars(
            select(WellnessEvent)
            .where(
                WellnessEvent.event_type.in_(
                    _RETAINED_OPEN_WEARABLES_EVENT_TYPES
                ),
                WellnessEvent.source_provider
                == OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER,
                (
                    WellnessEvent.expires_at.is_(None)
                    | (WellnessEvent.expires_at > current)
                ),
            )
            .order_by(
                WellnessEvent.recorded_at.desc(),
                WellnessEvent.created_at.desc(),
                WellnessEvent.id.desc(),
            )
        )
        for event in events:
            if event.event_type == OPEN_WEARABLES_QUERY_EVENT_TYPE:
                retained = wearable_query_snapshot_from_event(
                    session,
                    event,
                    now=current,
                    expected_provider_binding_digest=(
                        expected_provider_binding_digest
                    ),
                )
                if (
                    retained is None
                    or retained.provider_binding_digest
                    != expected_provider_binding_digest
                ):
                    continue
                _retained_query_evidence(
                    index,
                    event,
                    retained,
                    source_allowlist=source_allowlist,
                )
                continue
            retained_observation = wearable_snapshot_from_event(
                session,
                event,
                now=current,
                expected_provider_binding_digest=(
                    expected_provider_binding_digest
                ),
            )
            if (
                retained_observation is None
                or retained_observation.provider_binding_digest
                != expected_provider_binding_digest
            ):
                continue
            _retained_observation_evidence(
                index,
                retained_observation,
                allowed_providers=allowed_providers,
            )
    return index


def _project_capability_catalog(
    snapshot: OpenWearablesAvailabilitySnapshot,
    evidence: _RetainedEvidenceIndex,
    *,
    retained_only_projection: bool,
) -> tuple[OpenWearablesCapabilityBinding, ...]:
    """Narrow an LKG catalog to capabilities backed by retained evidence."""

    projected: list[OpenWearablesCapabilityBinding] = []
    for original in snapshot.capability_catalog:
        capability = original.capability
        evidence_providers = evidence.providers_by_capability.get(
            capability,
            set(),
        )
        providers = set(original.providers) & set(evidence_providers)
        if not providers:
            continue
        parameters: list[OpenWearablesParameterAllowlist] = []
        for parameter in original.parameters:
            value_bindings: list[OpenWearablesParameterValueBinding] = []
            for value_binding in parameter.value_bindings:
                supported = evidence.providers_by_parameter_value.get(
                    (capability, parameter.name, value_binding.value),
                    set(),
                )
                value_providers = (
                    set(value_binding.providers)
                    & set(supported)
                    & providers
                )
                if value_providers:
                    value_bindings.append(
                        OpenWearablesParameterValueBinding(
                            value=value_binding.value,
                            providers=tuple(sorted(value_providers)),
                        )
                    )
            if not value_bindings:
                parameters = []
                break
            parameter_providers = frozenset().union(
                *(set(item.providers) for item in value_bindings)
            )
            parameters.append(
                OpenWearablesParameterAllowlist(
                    name=parameter.name,
                    values=tuple(
                        item.value for item in value_bindings
                    ),
                    providers=tuple(sorted(parameter_providers)),
                    value_bindings=tuple(value_bindings),
                )
            )
        if original.parameters and not parameters:
            continue
        projected.append(
            OpenWearablesCapabilityBinding(
                capability=capability,
                providers=tuple(sorted(providers)),
                lineage_mode=original.lineage_mode,
                parameters=tuple(parameters),
            )
        )
    return tuple(projected)


def _project_provider_models(
    original: Sequence[OpenWearablesProviderBinding],
    catalog: Sequence[OpenWearablesCapabilityBinding],
) -> tuple[OpenWearablesProviderBinding, ...]:
    """Rebuild provider models from the narrowed public catalog."""

    capabilities_by_provider: defaultdict[str, set[str]] = defaultdict(set)
    for binding in catalog:
        for provider in binding.providers:
            capabilities_by_provider[provider].add(binding.capability)
    projected: list[OpenWearablesProviderBinding] = []
    for model in original:
        capabilities = capabilities_by_provider.get(model.provider, set())
        if not capabilities:
            continue
        projected.append(
            OpenWearablesProviderBinding(
                provider=model.provider,
                direct_api=model.direct_api,
                imported=model.imported,
                capabilities=tuple(sorted(capabilities)),
            )
        )
    return tuple(projected)


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
        self._last_known_good: OpenWearablesAvailabilitySnapshot | None = None
        self._resolution_lock = asyncio.Lock()

    async def __call__(self) -> OpenWearablesAvailabilitySnapshot:
        async with self._resolution_lock:
            return await self._resolve()

    async def _resolve(self) -> OpenWearablesAvailabilitySnapshot:
        observed_at = _utc(self._clock())
        try:
            source_policy = await asyncio.to_thread(
                self._source_policy_binding
            )
        except Exception:
            self._last_known_good = None
            return OpenWearablesAvailabilitySnapshot(
                state=OpenWearablesAvailabilityState.DISABLED,
                observed_at=observed_at,
                provider_catalog_version=1,
                reason_codes=(
                    OPEN_WEARABLES_SOURCE_SETTING_UNAVAILABLE,
                ),
            )
        if not source_policy.enabled:
            self._last_known_good = None
            return OpenWearablesAvailabilitySnapshot(
                state=OpenWearablesAvailabilityState.DISABLED,
                observed_at=observed_at,
                source_policy_revision=source_policy.revision,
                provider_catalog_version=1,
                reason_codes=(WEARABLE_INPUT_DISABLED,),
            )

        if not _settings_are_configured(self._settings):
            self._last_known_good = None
            return OpenWearablesAvailabilitySnapshot(
                state=OpenWearablesAvailabilityState.UNCONFIGURED,
                observed_at=observed_at,
                source_policy_revision=source_policy.revision,
                provider_catalog_version=1,
                reason_codes=(OPEN_WEARABLES_UNCONFIGURED,),
            )

        try:
            async with asyncio.timeout(self._timeout_seconds):
                user_id = await resolve_single_user_id(
                    self._client,
                    self._settings,
                )
        except LookupError:
            self._last_known_good = None
            return await self._finalize_snapshot(
                source_policy,
                OpenWearablesAvailabilitySnapshot(
                    state=OpenWearablesAvailabilityState.UNCONFIGURED,
                    observed_at=observed_at,
                    source_policy_revision=source_policy.revision,
                    provider_catalog_version=1,
                    reason_codes=(OPEN_WEARABLES_UNCONFIGURED,),
                ),
            )
        except (TimeoutError, OWClientError):
            snapshot = await self._metadata_failure(
                observed_at,
                source_policy_revision=source_policy.revision,
            )
            return await self._finalize_snapshot(
                source_policy,
                snapshot,
            )

        policy_failure = await self._source_policy_failure(
            source_policy,
            observed_at=observed_at,
        )
        if policy_failure is not None:
            return policy_failure

        metadata = await self._read_metadata(user_id)
        if not metadata.complete:
            snapshot = await self._metadata_failure(
                observed_at,
                source_policy_revision=source_policy.revision,
                metadata=metadata,
            )
            return await self._finalize_snapshot(
                source_policy,
                snapshot,
            )

        (
            provider_bindings,
            capability_catalog,
            provider_source_bindings,
            binding_digest,
        ) = _provider_catalog(
            connections=metadata.connections,
            data_sources=metadata.data_sources,
            coverage=metadata.coverage,
            inventory=metadata.inventory,
        )
        coverage_digest = _canonical_coverage_digest(metadata.coverage)
        if (
            provider_bindings
            and capability_catalog
            and binding_digest
            and coverage_digest
        ):
            snapshot = OpenWearablesAvailabilitySnapshot(
                state=OpenWearablesAvailabilityState.AVAILABLE,
                observed_at=observed_at,
                source_policy_revision=source_policy.revision,
                provider_catalog_version=1,
                providers=tuple(
                    item.provider for item in provider_bindings
                ),
                provider_bindings=provider_bindings,
                capability_catalog=capability_catalog,
                provider_binding_digest=binding_digest,
                provider_coverage_digest=coverage_digest,
                provider_source_bindings=provider_source_bindings,
            )
        else:
            snapshot = OpenWearablesAvailabilitySnapshot(
                state=OpenWearablesAvailabilityState.DISCONNECTED,
                observed_at=observed_at,
                source_policy_revision=source_policy.revision,
                provider_catalog_version=1,
                reason_codes=(OPEN_WEARABLES_DISCONNECTED,),
            )
        finalized = await self._finalize_snapshot(
            source_policy,
            snapshot,
        )
        if finalized.state is OpenWearablesAvailabilityState.AVAILABLE:
            self._last_known_good = finalized
        elif finalized.state in {
            OpenWearablesAvailabilityState.DISCONNECTED,
            OpenWearablesAvailabilityState.DISABLED,
            OpenWearablesAvailabilityState.UNCONFIGURED,
        }:
            # Authoritative source or connection changes revoke the old LKG.
            self._last_known_good = None
        return finalized

    async def _source_policy_failure(
        self,
        expected: InputSourcePolicyBinding,
        *,
        observed_at: datetime,
    ) -> OpenWearablesAvailabilitySnapshot | None:
        """Fence policy changes that occur during awaited metadata reads."""

        try:
            current = await asyncio.to_thread(
                self._source_policy_binding
            )
        except Exception:
            return OpenWearablesAvailabilitySnapshot(
                state=OpenWearablesAvailabilityState.DISABLED,
                observed_at=observed_at,
                source_policy_revision=expected.revision,
                provider_catalog_version=1,
                reason_codes=(
                    OPEN_WEARABLES_SOURCE_SETTING_UNAVAILABLE,
                ),
            )
        if current == expected and current.enabled:
            return None
        return OpenWearablesAvailabilitySnapshot(
            state=OpenWearablesAvailabilityState.DISABLED,
            observed_at=observed_at,
            source_policy_revision=current.revision,
            provider_catalog_version=1,
            reason_codes=(OPEN_WEARABLES_SOURCE_POLICY_CHANGED,),
        )

    async def _finalize_snapshot(
        self,
        expected: InputSourcePolicyBinding,
        snapshot: OpenWearablesAvailabilitySnapshot,
    ) -> OpenWearablesAvailabilitySnapshot:
        policy_failure = await self._source_policy_failure(
            expected,
            observed_at=snapshot.observed_at,
        )
        return policy_failure or snapshot

    async def _read_metadata(
        self,
        user_id: str,
    ) -> _OpenWearablesMetadataRead:
        """Read each metadata endpoint without discarding sibling results."""

        async def bounded_read(awaitable: Any) -> Any:
            async with asyncio.timeout(self._timeout_seconds):
                return await awaitable

        tasks = (
            asyncio.create_task(
                bounded_read(self._client.get_connections(user_id)),
                name="open-wearables-connections",
            ),
            asyncio.create_task(
                bounded_read(self._client.get_user_data_sources(user_id)),
                name="open-wearables-data-sources",
            ),
            asyncio.create_task(
                bounded_read(self._client.get_provider_coverage()),
                name="open-wearables-provider-coverage",
            ),
            asyncio.create_task(
                bounded_read(self._client.get_data_summary(user_id)),
                name="open-wearables-data-inventory",
            ),
        )
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return _OpenWearablesMetadataRead(*results)

    async def _metadata_failure(
        self,
        observed_at: datetime,
        *,
        source_policy_revision: int,
        metadata: _OpenWearablesMetadataRead | None = None,
    ) -> OpenWearablesAvailabilitySnapshot:
        last_known_good = self._last_known_good
        if (
            last_known_good is not None
            and metadata is not None
            and _metadata_conflicts_with_lkg(metadata, last_known_good)
        ):
            self._last_known_good = None
            last_known_good = None
        binding_digest = (
            last_known_good.provider_binding_digest
            if last_known_good is not None
            else None
        )
        if (
            last_known_good is not None
            and last_known_good.source_policy_revision
            == source_policy_revision
            and last_known_good.exposes_capabilities
            and binding_digest is not None
        ):
            retained_catalog = await asyncio.to_thread(
                self._retained_capability_catalog,
                last_known_good,
                observed_at,
                binding_digest,
            )
            if not retained_catalog:
                return OpenWearablesAvailabilitySnapshot(
                    state=OpenWearablesAvailabilityState.UNAVAILABLE,
                    observed_at=observed_at,
                    source_policy_revision=source_policy_revision,
                    provider_catalog_version=1,
                    reason_codes=(OPEN_WEARABLES_METADATA_UNAVAILABLE,),
                )
            provider_bindings = _project_provider_models(
                last_known_good.provider_bindings,
                retained_catalog,
            )
            return OpenWearablesAvailabilitySnapshot(
                state=OpenWearablesAvailabilityState.DEGRADED,
                observed_at=observed_at,
                source_policy_revision=source_policy_revision,
                provider_catalog_version=1,
                reason_codes=(OPEN_WEARABLES_METADATA_DEGRADED,),
                providers=tuple(
                    item.provider for item in provider_bindings
                ),
                provider_bindings=provider_bindings,
                capability_catalog=retained_catalog,
                provider_binding_digest=binding_digest,
                provider_coverage_digest=(
                    last_known_good.provider_coverage_digest
                ),
                provider_source_bindings=(
                    last_known_good.provider_source_bindings
                ),
            )
        return OpenWearablesAvailabilitySnapshot(
            state=OpenWearablesAvailabilityState.UNAVAILABLE,
            observed_at=observed_at,
            source_policy_revision=source_policy_revision,
            provider_catalog_version=1,
            reason_codes=(OPEN_WEARABLES_METADATA_UNAVAILABLE,),
        )

    def _retained_capability_catalog(
        self,
        snapshot: OpenWearablesAvailabilitySnapshot,
        now: datetime,
        expected_provider_binding_digest: str,
    ) -> tuple[OpenWearablesCapabilityBinding, ...]:
        """Return only catalog entries backed by valid retained evidence."""

        evidence = _retained_evidence_index(
            self._session_factory,
            snapshot=snapshot,
            now=now,
            expected_provider_binding_digest=(
                expected_provider_binding_digest
            ),
        )
        return _project_capability_catalog(
            snapshot,
            evidence,
            retained_only_projection=True,
        )

    def _has_retained_snapshot(
        self,
        now: datetime,
        *,
        expected_provider_binding_digest: str,
    ) -> bool:
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
                            expected_provider_binding_digest=(
                                expected_provider_binding_digest
                            ),
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
                            expected_provider_binding_digest=(
                                expected_provider_binding_digest
                            ),
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


def _provider_inventory(
    payload: Any,
) -> dict[str, OpenWearablesProviderInventory]:
    """Normalize the all-time provider inventory returned by Open Wearables."""

    if not isinstance(payload, Mapping):
        return {}
    rows = payload.get("by_provider")
    if not isinstance(rows, list):
        return {}
    has_womens_health_data = (
        payload.get("has_womens_health_data", False) is True
    )
    inventory: dict[str, OpenWearablesProviderInventory] = {}
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        provider = _canonical_provider(item.get("provider"))
        if provider is None or provider in inventory:
            continue
        series_counts = item.get("series_counts")
        series_types: tuple[str, ...] = ()
        if isinstance(series_counts, Mapping):
            positive_series: list[str] = []
            for value, count in series_counts.items():
                if not isinstance(value, str) or not value:
                    positive_series = []
                    break
                try:
                    normalized_count = int(count)
                except (TypeError, ValueError):
                    positive_series = []
                    break
                if normalized_count > 0:
                    positive_series.append(value)
            series_types = tuple(sorted(positive_series))
        try:
            data_points = int(item.get("data_points", 0))
            workout_count = int(item.get("workout_count", 0))
            sleep_count = int(item.get("sleep_count", 0))
        except (TypeError, ValueError):
            continue
        if (
            data_points < 0
            or workout_count < 0
            or sleep_count < 0
        ):
            continue
        inventory[provider] = OpenWearablesProviderInventory(
            series_types=series_types,
            data_points=data_points,
            workout_count=workout_count,
            sleep_count=sleep_count,
            has_womens_health_data=(
                provider in _MENSTRUAL_CYCLE_PROVIDERS
                and has_womens_health_data
            ),
        )
    if has_womens_health_data and "garmin" not in inventory:
        # Open Wearables exposes women's-health inventory only as a global
        # boolean, while the underlying route is currently Garmin-owned.
        inventory["garmin"] = OpenWearablesProviderInventory(
            has_womens_health_data=True
        )
    return inventory


def _coverage_ownership(
    payload: Any,
) -> tuple[
    dict[str, OpenWearablesProviderCoverage],
    dict[str, frozenset[str]],
    dict[str, frozenset[str]],
    dict[str, frozenset[str]],
    dict[str, frozenset[str]],
]:
    """Build provider coverage and value-level ownership maps."""

    if not isinstance(payload, Mapping):
        return {}, {}, {}, {}, {}
    declared = {
        provider
        for value in _sorted_unique(payload.get("providers"))
        if (provider := _canonical_provider(value)) is not None
    }
    if not declared:
        return {}, {}, {}, {}, {}

    timeseries_owners: defaultdict[str, set[str]] = defaultdict(set)
    for category in payload.get("timeseries", ()):
        if not isinstance(category, Mapping):
            continue
        for metric in category.get("metrics", ()):
            if not isinstance(metric, Mapping):
                continue
            code = metric.get("code")
            if not isinstance(code, str) or not code:
                continue
            for value in _sorted_unique(metric.get("providers")):
                provider = _canonical_provider(value)
                if provider in declared:
                    timeseries_owners[code].add(provider)

    def field_owners(field_name: str) -> dict[str, frozenset[str]]:
        owners: defaultdict[str, set[str]] = defaultdict(set)
        for field in payload.get(field_name, ()):
            if not isinstance(field, Mapping):
                continue
            code = field.get("code")
            if not isinstance(code, str) or not code:
                continue
            for value in _sorted_unique(field.get("providers")):
                provider = _canonical_provider(value)
                if provider in declared:
                    owners[code].add(provider)
        return {
            code: frozenset(values)
            for code, values in owners.items()
            if values
        }

    score_owners = field_owners("health_scores")
    timeseries = {
        code: frozenset(values)
        for code, values in timeseries_owners.items()
        if values
    }
    workout = field_owners("workout_fields")
    sleep = field_owners("sleep_fields")
    all_codes = set(declared)
    coverage: dict[str, OpenWearablesProviderCoverage] = {}
    for provider in sorted(all_codes):
        coverage[provider] = OpenWearablesProviderCoverage(
            timeseries=tuple(
                sorted(
                    code
                    for code, owners in timeseries.items()
                    if provider in owners
                )
            ),
            workout_fields=tuple(
                sorted(
                    code
                    for code, owners in workout.items()
                    if provider in owners
                )
            ),
            sleep_fields=tuple(
                sorted(
                    code
                    for code, owners in sleep.items()
                    if provider in owners
                )
            ),
            health_scores=tuple(
                sorted(
                    code
                    for code, owners in score_owners.items()
                    if provider in owners
                )
            ),
        )
    return coverage, timeseries, workout, sleep, score_owners


def _canonical_coverage_digest(payload: Any) -> str | None:
    """Hash a strict, order-independent provider coverage representation."""

    if not isinstance(payload, Mapping):
        return None

    raw_providers = payload.get("providers")
    if not isinstance(raw_providers, list) or not raw_providers:
        return None
    providers: set[str] = set()
    for raw in raw_providers:
        provider = _canonical_provider(raw)
        if provider is None or provider in providers:
            return None
        providers.add(provider)

    def canonical_owners(value: Any) -> tuple[str, ...] | None:
        if not isinstance(value, list) or not value:
            return None
        owners: set[str] = set()
        for raw in value:
            provider = _canonical_provider(raw)
            if (
                provider is None
                or provider not in providers
                or provider in owners
            ):
                return None
            owners.add(provider)
        return tuple(sorted(owners))

    raw_categories = payload.get("timeseries")
    if not isinstance(raw_categories, list):
        return None
    categories: list[dict[str, Any]] = []
    category_names: set[str] = set()
    metric_codes: set[str] = set()
    for raw_category in raw_categories:
        if not isinstance(raw_category, Mapping):
            return None
        name = raw_category.get("name")
        raw_metrics = raw_category.get("metrics")
        if (
            not isinstance(name, str)
            or not name.strip()
            or name in category_names
            or not isinstance(raw_metrics, list)
        ):
            return None
        category_names.add(name)
        metrics: list[dict[str, Any]] = []
        for raw_metric in raw_metrics:
            if not isinstance(raw_metric, Mapping):
                return None
            code = raw_metric.get("code")
            unit = raw_metric.get("unit")
            owners = canonical_owners(raw_metric.get("providers"))
            if (
                not isinstance(code, str)
                or not code
                or code in metric_codes
                or not isinstance(unit, str)
                or owners is None
            ):
                return None
            metric_codes.add(code)
            metrics.append(
                {
                    "code": code,
                    "providers": list(owners),
                    "unit": unit,
                }
            )
        categories.append(
            {
                "metrics": sorted(
                    metrics,
                    key=lambda item: item["code"],
                ),
                "name": name,
            }
        )

    def canonical_fields(field_name: str) -> list[dict[str, Any]] | None:
        raw_fields = payload.get(field_name)
        if not isinstance(raw_fields, list):
            return None
        fields: list[dict[str, Any]] = []
        codes: set[str] = set()
        for raw_field in raw_fields:
            if not isinstance(raw_field, Mapping):
                return None
            code = raw_field.get("code")
            owners = canonical_owners(raw_field.get("providers"))
            if (
                not isinstance(code, str)
                or not code
                or code in codes
                or owners is None
            ):
                return None
            codes.add(code)
            fields.append(
                {
                    "code": code,
                    "providers": list(owners),
                }
            )
        return sorted(fields, key=lambda item: item["code"])

    workout_fields = canonical_fields("workout_fields")
    sleep_fields = canonical_fields("sleep_fields")
    health_scores = canonical_fields("health_scores")
    if (
        workout_fields is None
        or sleep_fields is None
        or health_scores is None
    ):
        return None

    canonical = {
        "health_scores": health_scores,
        "providers": sorted(providers),
        "sleep_fields": sleep_fields,
        "timeseries": sorted(
            categories,
            key=lambda item: item["name"],
        ),
        "workout_fields": workout_fields,
    }
    return "sha256:" + hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _positive_inventory(
    provider: str,
    inventory: Mapping[str, OpenWearablesProviderInventory],
) -> bool:
    item = inventory.get(provider)
    return bool(
        item is not None
        and (
            item.data_points > 0
            or item.workout_count > 0
            or item.sleep_count > 0
            or item.series_types
            or item.has_womens_health_data
        )
    )


def _successful_metadata(value: Any) -> bool:
    return not isinstance(value, BaseException)


def _active_connections_by_provider(
    connections: Any,
) -> dict[str, frozenset[str]]:
    if not isinstance(connections, list):
        return {}
    active: defaultdict[str, set[str]] = defaultdict(set)
    for item in connections:
        if not isinstance(item, Mapping):
            continue
        provider = _canonical_provider(item.get("provider"))
        connection_id = item.get("id")
        if (
            provider is not None
            and isinstance(connection_id, str)
            and connection_id
            and str(item.get("status", "")).strip().casefold() == "active"
        ):
            active[provider].add(connection_id)
    return {
        provider: frozenset(values)
        for provider, values in active.items()
        if values
    }


def _private_source_binding_map(
    snapshot: OpenWearablesAvailabilitySnapshot,
) -> dict[str, OpenWearablesProviderSourceBinding]:
    return {
        binding.provider: binding
        for binding in snapshot.provider_source_bindings
    }


def _metadata_conflicts_with_lkg(
    metadata: _OpenWearablesMetadataRead,
    last_known_good: OpenWearablesAvailabilitySnapshot,
) -> bool:
    """Reject an LKG contradicted by any authoritative partial result."""

    previous = _private_source_binding_map(last_known_good)
    if not previous:
        return True

    if _successful_metadata(metadata.coverage):
        coverage_digest = _canonical_coverage_digest(metadata.coverage)
        if (
            coverage_digest is None
            or last_known_good.provider_coverage_digest is None
            or coverage_digest
            != last_known_good.provider_coverage_digest
        ):
            return True

    if _successful_metadata(metadata.connections):
        if metadata.connections == []:
            return True
        active = _active_connections_by_provider(metadata.connections)
        expected_active = {
            provider: frozenset(binding.active_connection_ids)
            for provider, binding in previous.items()
            if binding.active_connection_ids
        }
        if active != expected_active:
            return True
    else:
        active = {
            provider: frozenset(binding.active_connection_ids)
            for provider, binding in previous.items()
            if binding.active_connection_ids
        }

    if not _successful_metadata(metadata.data_sources):
        return False
    if not isinstance(metadata.data_sources, Mapping):
        return True
    rows = metadata.data_sources.get("items")
    if not isinstance(rows, list):
        return True

    inventory = (
        _provider_inventory(metadata.inventory)
        if _successful_metadata(metadata.inventory)
        else {}
    )
    current_direct: defaultdict[str, set[str]] = defaultdict(set)
    current_imports: defaultdict[str, set[str]] = defaultdict(set)
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        provider = _canonical_provider(item.get("provider"))
        source_id = item.get("id")
        connection_id = item.get("user_connection_id")
        if (
            provider is None
            or not isinstance(source_id, str)
            or not source_id
        ):
            continue
        if (
            isinstance(connection_id, str)
            and connection_id in active.get(provider, frozenset())
        ):
            current_direct[provider].add(source_id)
        elif connection_id is None and (
            (
                previous.get(provider) is not None
                and bool(previous[provider].import_data_source_ids)
            )
            or (
                _successful_metadata(metadata.inventory)
                and _positive_inventory(provider, inventory)
            )
        ):
            current_imports[provider].add(source_id)

    expected_direct = {
        provider: frozenset(binding.direct_data_source_ids)
        for provider, binding in previous.items()
        if binding.direct_data_source_ids
    }
    actual_direct = {
        provider: frozenset(values)
        for provider, values in current_direct.items()
        if values
    }
    if actual_direct != expected_direct:
        return True

    expected_imports = {
        provider: frozenset(binding.import_data_source_ids)
        for provider, binding in previous.items()
        if binding.import_data_source_ids
    }
    actual_imports = {
        provider: frozenset(values)
        for provider, values in current_imports.items()
        if values
    }
    return actual_imports != expected_imports


def _provider_catalog(
    *,
    connections: Any,
    data_sources: Any,
    coverage: Any,
    inventory: Any,
) -> tuple[
    tuple[OpenWearablesProviderBinding, ...],
    tuple[OpenWearablesCapabilityBinding, ...],
    tuple[OpenWearablesProviderSourceBinding, ...],
    str | None,
]:
    """Derive a provider-aware catalog from verified metadata.

    Connection and data-source IDs are deliberately kept out of the public
    models. They are included in the private digest so retained snapshots and
    cursors cannot be replayed after a binding changes.
    """

    try:
        def source_labels(item: Mapping[str, Any]) -> tuple[str, ...]:
            labels: set[str] = set()
            for field in (
                "device_model",
                "display_name",
                "original_source_name",
            ):
                value = item.get(field)
                if isinstance(value, str) and value.strip():
                    labels.add(value.strip())
            return tuple(
                sorted(labels, key=lambda value: (value.casefold(), value))
            )

        (
            coverage_by_provider,
            timeseries_owners,
            workout_owners,
            sleep_owners,
            score_owners,
        ) = (
            _coverage_ownership(coverage)
        )
        inventory_by_provider = _provider_inventory(inventory)
        active_connections: defaultdict[str, set[str]] = defaultdict(set)
        for item in connections if isinstance(connections, list) else ():
            if not isinstance(item, Mapping):
                continue
            provider = _canonical_provider(item.get("provider"))
            connection_id = item.get("id")
            if (
                provider is None
                or not isinstance(connection_id, str)
                or not connection_id
                or str(item.get("status", "")).strip().casefold()
                != "active"
            ):
                continue
            active_connections[provider].add(connection_id)

        bindings: defaultdict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "direct_api": False,
                "imported": False,
                "connection_ids": set(),
                "direct_data_source_ids": set(),
                "import_data_source_ids": set(),
                "data_source_identities": {},
            }
        )
        for item in (
            data_sources.get("items", ())
            if isinstance(data_sources, Mapping)
            else ()
        ):
            if not isinstance(item, Mapping):
                continue
            provider = _canonical_provider(item.get("provider"))
            source_id = item.get("id")
            connection_id = item.get("user_connection_id")
            if (
                provider is None
                or not isinstance(source_id, str)
                or not source_id
            ):
                continue
            active_ids = active_connections.get(provider, set())
            if (
                isinstance(connection_id, str)
                and connection_id in active_ids
            ):
                state = bindings[provider]
                state["direct_api"] = True
                state["connection_ids"].add(connection_id)
                state["direct_data_source_ids"].add(source_id)
                state["data_source_identities"][source_id] = {
                    "data_source_id": source_id,
                    "device_labels": source_labels(item),
                }
            elif connection_id is None and _positive_inventory(
                provider,
                inventory_by_provider,
            ):
                state = bindings[provider]
                state["imported"] = True
                state["import_data_source_ids"].add(source_id)
                state["data_source_identities"][source_id] = {
                    "data_source_id": source_id,
                    "device_labels": source_labels(item),
                }

        usable = {
            provider
            for provider, state in bindings.items()
            if coverage_by_provider.get(provider) is not None
            and (state["direct_api"] or state["imported"])
        }
        if not usable:
            return (), (), (), None

        def owner_values(
            owners: Mapping[str, frozenset[str]],
            *,
            predicate: Callable[[str, str], bool] | None = None,
        ) -> dict[str, frozenset[str]]:
            result: dict[str, frozenset[str]] = {}
            for value, providers in owners.items():
                selected = frozenset(
                    provider
                    for provider in providers
                    if provider in usable
                    and (
                        predicate is None
                        or predicate(provider, value)
                    )
                )
                if selected:
                    result[value] = selected
            return result

        def imported_has_series(provider: str, value: str) -> bool:
            state = bindings[provider]
            if state["direct_api"]:
                return True
            return value in inventory_by_provider.get(
                provider,
                OpenWearablesProviderInventory(),
            ).series_types

        def imported_has_workouts(provider: str, _value: str) -> bool:
            state = bindings[provider]
            return state["direct_api"] or (
                inventory_by_provider.get(
                    provider,
                    OpenWearablesProviderInventory(),
                ).workout_count
                > 0
            )

        def imported_has_sleep(provider: str, _value: str) -> bool:
            state = bindings[provider]
            return state["direct_api"] or (
                inventory_by_provider.get(
                    provider,
                    OpenWearablesProviderInventory(),
                ).sleep_count
                > 0
            )

        timeseries = owner_values(
            {
                value: owners
                for value, owners in timeseries_owners.items()
                if value in _SEARCH_TIMESERIES
            },
            predicate=imported_has_series,
        )
        scores = owner_values(
            {
                value: owners
                for value, owners in score_owners.items()
                if value in _SEARCH_SCORE_CATEGORIES
            },
            predicate=lambda provider, _value: bool(
                bindings[provider]["direct_api"]
            ),
        )
        summary_owners = {
            "activity": frozenset(
                provider
                for provider in usable
                if (
                    (
                        bindings[provider]["direct_api"]
                        and (
                            set(coverage_by_provider[provider].timeseries)
                            & _ACTIVITY_SERIES
                        )
                    )
                    or (
                        not bindings[provider]["direct_api"]
                        and set(
                            inventory_by_provider.get(
                                provider,
                                OpenWearablesProviderInventory(),
                            ).series_types
                        )
                        & _ACTIVITY_SERIES
                    )
                )
            ),
            "recovery": frozenset(
                provider
                for provider in usable
                if (
                    bindings[provider]["direct_api"]
                    and (
                        set(coverage_by_provider[provider].health_scores)
                        & _SEARCH_SCORE_CATEGORIES
                        or set(coverage_by_provider[provider].timeseries)
                        & {
                            "heart_rate_variability_rmssd",
                            "heart_rate_variability_sdnn",
                            "resting_heart_rate",
                        }
                    )
                    or not bindings[provider]["direct_api"]
                    and (
                        set(
                            inventory_by_provider.get(
                                provider,
                                OpenWearablesProviderInventory(),
                            ).series_types
                        )
                        & {
                            "heart_rate_variability_rmssd",
                            "heart_rate_variability_sdnn",
                            "resting_heart_rate",
                        }
                    )
                )
            ),
            "sleep": frozenset(
                provider
                for provider in usable
                if (
                    coverage_by_provider[provider].sleep_fields
                    and imported_has_sleep(provider, "sleep")
                )
            ),
        }
        summary_owners = {
            value: owners
            for value, owners in summary_owners.items()
            if owners
        }
        workout_providers = frozenset(
            provider
            for provider in usable
            if provider in _DIRECT_PROVIDER_CAPABILITIES
            and bindings[provider]["direct_api"]
        )
        generic_workout_providers = frozenset(
            provider
            for provider in usable
            if (
                coverage_by_provider[provider].workout_fields
                and imported_has_workouts(provider, "workout")
            )
        )
        sleep_providers = frozenset(
            provider
            for provider in usable
            if (
                coverage_by_provider[provider].sleep_fields
                and imported_has_sleep(provider, "sleep")
            )
        )
        body_inventory_providers = frozenset(
            provider
            for provider, item in inventory_by_provider.items()
            if set(item.series_types) & _BODY_SERIES
        )
        if body_inventory_providers:
            body_providers = (
                body_inventory_providers
                if len(body_inventory_providers) == 1
                and body_inventory_providers <= usable
                and any(
                    set(coverage_by_provider[provider].timeseries)
                    & _BODY_SERIES
                    for provider in body_inventory_providers
                )
                else frozenset()
            )
        else:
            body_candidates = frozenset(
                provider
                for provider in usable
                if bindings[provider]["direct_api"]
                and (
                    set(coverage_by_provider[provider].timeseries)
                    & _BODY_SERIES
                )
            )
            body_providers = (
                body_candidates
                if len(body_candidates) == 1
                else frozenset()
            )
        menstrual_cycle_providers = frozenset(
            provider
            for provider in usable
            if (
                provider in _MENSTRUAL_CYCLE_PROVIDERS
                and (
                    bindings[provider]["direct_api"]
                    or inventory_by_provider.get(
                        provider,
                        OpenWearablesProviderInventory(),
                    ).has_womens_health_data
                )
            )
        )
        recovery_providers = frozenset().union(
            scores.get("recovery", frozenset()),
            scores.get("readiness", frozenset()),
            timeseries.get(
                "heart_rate_variability_rmssd",
                frozenset(),
            ),
            timeseries.get(
                "heart_rate_variability_sdnn",
                frozenset(),
            ),
            timeseries.get("resting_heart_rate", frozenset()),
        )
        stress_providers = frozenset().union(
            scores.get("stress", frozenset()),
            scores.get("resilience", frozenset()),
            timeseries.get("garmin_stress_level", frozenset()),
        )
        readiness_providers = frozenset().union(
            sleep_providers,
            recovery_providers,
            stress_providers,
        )
        compatibility_capability_providers = {
            "wearable.metric-detail": readiness_providers,
            "wearable.readiness": readiness_providers,
            "wearable.recovery": recovery_providers,
            "wearable.sleep": sleep_providers,
            "wearable.stress": stress_providers,
        }

        capability_values: dict[str, dict[str, frozenset[str]]] = {
            "wearable.health-scores": scores,
            "wearable.summaries": summary_owners,
            "wearable.timeseries": timeseries,
            "wearable.provider-workouts": {
                provider: frozenset({provider})
                for provider in workout_providers
            }
            if workout_providers
            else {},
            "wearable.provider-workout-detail": {
                provider: frozenset({provider})
                for provider in workout_providers
            }
            if workout_providers
            else {},
        }
        if generic_workout_providers:
            capability_values["wearable.workouts"] = {}
        if sleep_providers:
            capability_values["wearable.sleep-sessions"] = {}
        if body_providers:
            capability_values["wearable.body-summary"] = {}
        if menstrual_cycle_providers:
            capability_values["wearable.menstrual-cycles"] = {}
        whoop_binding = bindings.get("whoop", {})
        if (
            "whoop" in scores.get("recovery", frozenset())
            and "whoop" in scores.get("day_strain", frozenset())
            and bool(whoop_binding.get("direct_api"))
            and bool(whoop_binding.get("connection_ids"))
            and bool(whoop_binding.get("direct_data_source_ids"))
        ):
            capability_values["wearable.whoop-recovery-package"] = {}
        for capability, providers in (
            compatibility_capability_providers.items()
        ):
            if providers:
                capability_values[capability] = {}

        catalog: list[OpenWearablesCapabilityBinding] = []
        for capability, value_owners in sorted(capability_values.items()):
            lineage_mode = open_wearables_lineage_mode(capability)
            if capability in {
                "wearable.body-summary",
                "wearable.menstrual-cycles",
                "wearable.metric-detail",
                "wearable.readiness",
                "wearable.recovery",
                "wearable.sleep",
                "wearable.stress",
                "wearable.workouts",
                "wearable.sleep-sessions",
                "wearable.whoop-recovery-package",
            }:
                if capability == "wearable.body-summary":
                    capability_providers = body_providers
                elif capability == "wearable.menstrual-cycles":
                    capability_providers = menstrual_cycle_providers
                elif capability in compatibility_capability_providers:
                    capability_providers = (
                        compatibility_capability_providers[capability]
                    )
                elif capability == "wearable.workouts":
                    capability_providers = generic_workout_providers
                elif capability == "wearable.sleep-sessions":
                    capability_providers = sleep_providers
                else:
                    capability_providers = frozenset({"whoop"})
                if not capability_providers:
                    continue
                parameters = ()
            else:
                if not value_owners:
                    continue
                capability_providers = frozenset().union(
                    *value_owners.values()
                )
                if not capability_providers:
                    continue
                parameter_name = {
                    "wearable.health-scores": "category",
                    "wearable.summaries": "summary_kind",
                    "wearable.timeseries": "series_type",
                    "wearable.provider-workouts": "provider",
                    "wearable.provider-workout-detail": "provider",
                }.get(capability)
                if parameter_name is None:
                    continue
                parameter_values = tuple(sorted(value_owners))
                parameters = (
                    OpenWearablesParameterAllowlist(
                        name=parameter_name,
                        values=parameter_values,
                        providers=tuple(sorted(capability_providers)),
                        value_bindings=tuple(
                            OpenWearablesParameterValueBinding(
                                value=value,
                                providers=tuple(
                                    sorted(value_owners[value])
                                ),
                            )
                            for value in parameter_values
                        ),
                    ),
                )
            if not capability_providers:
                continue
            catalog.append(
                OpenWearablesCapabilityBinding(
                    capability=capability,
                    providers=tuple(sorted(capability_providers)),
                    lineage_mode=lineage_mode,
                    parameters=parameters,
                )
            )

        capability_names_by_provider: defaultdict[str, set[str]] = (
            defaultdict(set)
        )
        for binding in catalog:
            for provider in binding.providers:
                capability_names_by_provider[provider].add(
                    binding.capability
                )
        provider_models = tuple(
            OpenWearablesProviderBinding(
                provider=provider,
                direct_api=bool(bindings[provider]["direct_api"]),
                imported=bool(bindings[provider]["imported"]),
                capabilities=tuple(
                    sorted(capability_names_by_provider[provider])
                ),
            )
            for provider in sorted(usable)
        )
        provider_source_bindings = tuple(
            OpenWearablesProviderSourceBinding(
                provider=model.provider,
                active_connection_ids=tuple(
                    sorted(bindings[model.provider]["connection_ids"])
                ),
                direct_data_source_ids=tuple(
                    sorted(
                        bindings[model.provider][
                            "direct_data_source_ids"
                        ]
                    )
                ),
                import_data_source_ids=tuple(
                    sorted(
                        bindings[model.provider][
                            "import_data_source_ids"
                        ]
                    )
                ),
                data_sources=tuple(
                    OpenWearablesDataSourceIdentity(
                        **bindings[model.provider]["data_source_identities"][
                            source_id
                        ]
                    )
                    for source_id in sorted(
                        bindings[model.provider]["data_source_identities"]
                    )
                ),
            )
            for model in provider_models
        )
        digest_payload = {
            "providers": [
                {
                    "capabilities": list(model.capabilities),
                    "connection_ids": sorted(
                        bindings[model.provider]["connection_ids"]
                    ),
                    "direct_data_source_ids": sorted(
                        bindings[model.provider]["direct_data_source_ids"]
                    ),
                    "import_data_source_ids": sorted(
                        bindings[model.provider]["import_data_source_ids"]
                    ),
                    "data_source_identities": [
                        bindings[model.provider]["data_source_identities"][
                            source_id
                        ]
                        for source_id in sorted(
                            bindings[model.provider]["data_source_identities"]
                        )
                    ],
                    "direct_api": model.direct_api,
                    "imported": model.imported,
                    "provider": model.provider,
                }
                for model in provider_models
            ],
            "capability_values": [
                {
                    "capability": binding.capability,
                    "lineage_mode": binding.lineage_mode.value,
                    "parameters": [
                        {
                            "name": parameter.name,
                            "value_bindings": [
                                {
                                    "providers": list(
                                        value_binding.providers
                                    ),
                                    "value": value_binding.value,
                                }
                                for value_binding in parameter.value_bindings
                            ],
                            "providers": list(parameter.providers),
                            "values": list(parameter.values),
                        }
                        for parameter in binding.parameters
                    ],
                    "providers": list(binding.providers),
                }
                for binding in catalog
            ],
        }
        digest = "sha256:" + hashlib.sha256(
            json.dumps(
                digest_payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return (
            provider_models,
            tuple(catalog),
            provider_source_bindings,
            digest,
        )
    except (KeyError, TypeError, ValueError):
        # Metadata is untrusted input. A malformed catalog must never widen
        # access; callers treat an empty result as disconnected.
        return (), (), (), None


__all__ = [
    "OPEN_WEARABLES_BACKED_CAPABILITIES",
    "OPEN_WEARABLES_INPUT_SOURCE_ID",
    "OPEN_WEARABLES_DISCONNECTED",
    "OPEN_WEARABLES_METADATA_DEGRADED",
    "OPEN_WEARABLES_METADATA_UNAVAILABLE",
    "OPEN_WEARABLES_PROVIDER_BINDING_CHANGED",
    "OPEN_WEARABLES_SOURCE_SETTING_UNAVAILABLE",
    "OPEN_WEARABLES_SOURCE_POLICY_CHANGED",
    "OPEN_WEARABLES_UNCONFIGURED",
    "WEARABLE_INPUT_DISABLED",
    "OpenWearablesAvailability",
    "OpenWearablesAvailabilityResolver",
    "OpenWearablesAvailabilitySnapshot",
    "OpenWearablesAvailabilityState",
    "OpenWearablesCapabilityBinding",
    "OpenWearablesLineageMode",
    "OpenWearablesParameterAllowlist",
    "OpenWearablesParameterValueBinding",
    "OpenWearablesProviderBinding",
    "OpenWearablesProviderCoverage",
    "OpenWearablesProviderInventory",
    "OpenWearablesProviderSourceBinding",
]
