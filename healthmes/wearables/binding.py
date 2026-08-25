"""Private request binding for provider-aware Open Wearables execution."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from healthmes.wearables.lineage import OpenWearablesLineageMode

_PROVIDER_BINDING_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROVIDER_OWNED_PARAMETERS = frozenset(
    {"category", "provider", "series_type", "summary_kind"}
)

AvailabilityReader = Callable[[], Awaitable[Any]]


class OpenWearablesBindingError(ValueError):
    """The frozen catalog cannot authorize an Open Wearables query."""


class OpenWearablesProviderBindingChangedError(RuntimeError):
    """The live provider binding no longer matches the frozen session."""


_PROVIDER_BOUND_CONTEXT_ATTESTATION = object()


@dataclass(frozen=True, slots=True)
class OpenWearablesExecutionDataSource:
    """Private source identity used for exact live-row attribution."""

    data_source_id: str
    device_labels: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderBoundWearableContext:
    """Internal result envelope for the trusted readiness reader.

    The attestation is intentionally an object identity rather than a
    model-visible boolean.  A custom reader cannot make an ordinary JSON
    dictionary look provider-bound merely by setting a flag.
    """

    context: dict[str, Any]
    _attestation: object

    @property
    def lineage_attested(self) -> bool:
        return self._attestation is _PROVIDER_BOUND_CONTEXT_ATTESTATION


def attest_provider_bound_wearable_context(
    context: dict[str, Any],
) -> ProviderBoundWearableContext:
    """Mark a context produced by the bounded HealthMes wearable reader."""

    if not isinstance(context, dict):
        raise TypeError("provider-bound wearable context must be a dict")
    return ProviderBoundWearableContext(
        context=context,
        _attestation=_PROVIDER_BOUND_CONTEXT_ATTESTATION,
    )


@dataclass(frozen=True, slots=True)
class OpenWearablesExecutionSourceBinding:
    """Private provider source lineage frozen for one decision session."""

    provider: str
    active_connection_ids: tuple[str, ...]
    direct_data_source_ids: tuple[str, ...]
    import_data_source_ids: tuple[str, ...]
    data_sources: tuple[OpenWearablesExecutionDataSource, ...] = ()

    @property
    def allowed_data_source_ids(self) -> frozenset[str]:
        return frozenset(
            (*self.direct_data_source_ids, *self.import_data_source_ids)
        )

    @property
    def data_source_identities(
        self,
    ) -> tuple[OpenWearablesExecutionDataSource, ...]:
        return self.data_sources


@dataclass(frozen=True, slots=True)
class OpenWearablesExecutionBinding:
    """Private binding propagated below the model-visible query contract."""

    capability: str
    allowed_providers: tuple[str, ...]
    provider_binding_digest: str
    source_policy_revision: int
    provider_parameters: tuple[tuple[str, str], ...]
    provider_source_bindings: tuple[
        OpenWearablesExecutionSourceBinding, ...
    ]
    lineage_mode: OpenWearablesLineageMode | None = None
    retained_only: bool = False
    availability_reader: AvailabilityReader | None = None

    @property
    def provider_source_allowlist(
        self,
    ) -> dict[str, frozenset[str]]:
        """Return the exact source IDs frozen for each provider."""

        return {
            item.provider: item.allowed_data_source_ids
            for item in self.provider_source_bindings
        }

    @property
    def provider_source_direct_allowlist(
        self,
    ) -> dict[str, frozenset[str]]:
        """Return only source IDs backed by a live direct connection."""

        return {
            item.provider: frozenset(item.direct_data_source_ids)
            for item in self.provider_source_bindings
        }

    @property
    def provider_source_identities(
        self,
    ) -> dict[str, tuple[OpenWearablesExecutionDataSource, ...]]:
        return {
            item.provider: item.data_source_identities
            for item in self.provider_source_bindings
        }

    async def revalidate(self) -> None:
        """Recheck the current binding immediately before snapshot storage."""

        if self.availability_reader is None:
            return
        try:
            current = await self.availability_reader()
        except Exception as exc:
            raise OpenWearablesProviderBindingChangedError(
                "open_wearables_provider_binding_changed"
            ) from exc

        # The existing database source-policy fence owns revision changes.
        if (
            _source_policy_revision(current)
            != self.source_policy_revision
        ):
            return
        try:
            resolved = resolve_open_wearables_execution_binding(
                current,
                capability=self.capability,
                parameters=dict(self.provider_parameters),
                availability_reader=None,
            )
        except OpenWearablesBindingError as exc:
            raise OpenWearablesProviderBindingChangedError(
                "open_wearables_provider_binding_changed"
            ) from exc
        if (
            resolved.provider_binding_digest
            != self.provider_binding_digest
            or resolved.allowed_providers != self.allowed_providers
            or resolved.provider_source_bindings
            != self.provider_source_bindings
            or resolved.lineage_mode != self.lineage_mode
            or resolved.retained_only != self.retained_only
        ):
            raise OpenWearablesProviderBindingChangedError(
                "open_wearables_provider_binding_changed"
            )


_CURRENT_OPEN_WEARABLES_BINDING: ContextVar[
    OpenWearablesExecutionBinding | None
] = ContextVar(
    "healthmes_open_wearables_execution_binding",
    default=None,
)


@contextmanager
def open_wearables_execution_binding(
    binding: OpenWearablesExecutionBinding,
) -> Iterator[None]:
    """Bind private provider ownership for one provider execution."""

    token = _CURRENT_OPEN_WEARABLES_BINDING.set(binding)
    try:
        yield
    finally:
        _CURRENT_OPEN_WEARABLES_BINDING.reset(token)


def current_open_wearables_execution_binding(
    capability: str | None = None,
) -> OpenWearablesExecutionBinding | None:
    """Return the private binding, rejecting cross-capability reuse."""

    binding = _CURRENT_OPEN_WEARABLES_BINDING.get()
    if binding is None:
        return None
    if capability is not None and binding.capability != _identifier(capability):
        raise OpenWearablesProviderBindingChangedError(
            "open_wearables_provider_binding_changed"
        )
    return binding


def resolve_open_wearables_execution_binding(
    snapshot: Any,
    *,
    capability: str,
    parameters: Mapping[str, Any],
    availability_reader: AvailabilityReader | None,
) -> OpenWearablesExecutionBinding:
    """Resolve exact provider ownership from one frozen capability catalog."""

    normalized_capability = _identifier(capability)
    if not bool(getattr(snapshot, "exposes_capabilities", False)):
        raise OpenWearablesBindingError(
            "Open Wearables capabilities are not available"
        )
    if int(getattr(snapshot, "provider_catalog_version", 0)) < 1:
        raise OpenWearablesBindingError(
            "Open Wearables provider catalog is not frozen"
        )
    digest = getattr(snapshot, "provider_binding_digest", None)
    if (
        not isinstance(digest, str)
        or _PROVIDER_BINDING_DIGEST.fullmatch(digest) is None
    ):
        raise OpenWearablesBindingError(
            "Open Wearables provider binding digest is missing"
        )
    if normalized_capability not in _available_capabilities(snapshot):
        raise OpenWearablesBindingError(
            "Open Wearables capability is outside the frozen catalog"
        )
    lineage_mode = _lineage_mode(snapshot, normalized_capability)

    allowed_providers = _providers_for_capability(
        snapshot,
        normalized_capability,
    )
    if not allowed_providers:
        raise OpenWearablesBindingError(
            "Open Wearables capability has no provider ownership"
        )

    provider_parameters: list[tuple[str, str]] = []
    for parameter in sorted(_PROVIDER_OWNED_PARAMETERS):
        if parameter not in parameters:
            continue
        value = parameters[parameter]
        if not isinstance(value, str):
            raise OpenWearablesBindingError(
                f"{parameter} is not a catalog string value"
            )
        allowed_values = _parameter_values(
            snapshot,
            normalized_capability,
            parameter,
        )
        if allowed_values is None or value not in allowed_values:
            raise OpenWearablesBindingError(
                f"{parameter} is outside the frozen capability catalog"
            )
        value_providers = _providers_for_parameter_value(
            snapshot,
            normalized_capability,
            parameter,
            value,
        )
        if not value_providers:
            raise OpenWearablesBindingError(
                f"{parameter} has no frozen provider ownership"
            )
        allowed_providers &= value_providers
        provider_parameters.append((parameter, value))

    if not allowed_providers:
        raise OpenWearablesBindingError(
            "Open Wearables query has no common allowed provider"
        )
    provider_source_bindings = _provider_source_bindings(
        snapshot,
        allowed_providers,
    )
    if {
        binding.provider for binding in provider_source_bindings
    } != allowed_providers:
        raise OpenWearablesBindingError(
            "Open Wearables provider source lineage is incomplete"
        )
    if any(
        not binding.allowed_data_source_ids
        for binding in provider_source_bindings
    ):
        raise OpenWearablesBindingError(
            "Open Wearables provider has no frozen data source"
        )
    provider_modes = _provider_access_modes(
        snapshot,
        allowed_providers,
    )
    if (
        lineage_mode
        is OpenWearablesLineageMode.PROVIDER_ROUTE_AUTHORITATIVE
        and any(
            not binding.active_connection_ids
            or not binding.direct_data_source_ids
            for binding in provider_source_bindings
        )
    ):
        raise OpenWearablesBindingError(
            "Open Wearables provider route requires an active direct connection"
        )
    return OpenWearablesExecutionBinding(
        capability=normalized_capability,
        allowed_providers=tuple(sorted(allowed_providers)),
        provider_binding_digest=digest,
        source_policy_revision=_source_policy_revision(snapshot),
        provider_parameters=tuple(provider_parameters),
        provider_source_bindings=provider_source_bindings,
        lineage_mode=lineage_mode,
        retained_only=_retained_only(
            snapshot,
            provider_modes=provider_modes,
        ),
        availability_reader=availability_reader,
    )


def _source_policy_revision(snapshot: Any) -> int:
    value = getattr(snapshot, "source_policy_revision", 0)
    return value if type(value) is int and value >= 0 else 0


def _retained_only(
    snapshot: Any,
    *,
    provider_modes: tuple[tuple[bool, bool], ...],
) -> bool:
    """Return whether this execution may read retained data only.

    If any selected provider is imported-only, one shared upstream request
    cannot safely separate its retained rows from direct-provider rows.  The
    conservative contract is to avoid live REST for the whole execution.
    """

    state = getattr(snapshot, "state", None)
    value = getattr(state, "value", state)
    if isinstance(value, str) and value.strip().casefold() == "degraded":
        return True
    return bool(provider_modes) and any(
        imported and not direct_api
        for direct_api, imported in provider_modes
    )


def _identifier(value: str) -> str:
    normalized = value.strip().casefold()
    if not normalized:
        raise OpenWearablesBindingError("Open Wearables identifier is empty")
    return normalized


def _provider_set(values: Any) -> frozenset[str]:
    if isinstance(values, str) or not isinstance(
        values,
        (Sequence, set, frozenset),
    ):
        return frozenset()
    providers = {
        _identifier(value)
        for value in values
        if isinstance(value, str) and value.strip()
    }
    return frozenset(providers)


def _private_identifiers(values: Any) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(
        values,
        (Sequence, set, frozenset),
    ):
        return ()
    return tuple(
        sorted(
            {
                value
                for value in values
                if isinstance(value, str) and value
            }
        )
    )


def _provider_source_bindings(
    snapshot: Any,
    allowed_providers: frozenset[str],
) -> tuple[OpenWearablesExecutionSourceBinding, ...]:
    bindings: list[OpenWearablesExecutionSourceBinding] = []
    for item in getattr(snapshot, "provider_source_bindings", ()):
        provider = (
            item.get("provider")
            if isinstance(item, Mapping)
            else getattr(item, "provider", None)
        )
        if not isinstance(provider, str):
            continue
        normalized = _identifier(provider)
        if normalized not in allowed_providers:
            continue

        def values(field: str) -> tuple[str, ...]:
            raw = (
                item.get(field)
                if isinstance(item, Mapping)
                else getattr(item, field, ())
            )
            return _private_identifiers(raw)

        raw_data_sources = (
            item.get("data_sources")
            if isinstance(item, Mapping)
            else getattr(item, "data_sources", ())
        )
        data_sources: list[OpenWearablesExecutionDataSource] = []
        if isinstance(raw_data_sources, Sequence) and not isinstance(
            raw_data_sources,
            str,
        ):
            for raw_source in raw_data_sources:
                source_id = (
                    raw_source.get("data_source_id")
                    if isinstance(raw_source, Mapping)
                    else getattr(raw_source, "data_source_id", None)
                )
                raw_labels = (
                    raw_source.get("device_labels", ())
                    if isinstance(raw_source, Mapping)
                    else getattr(raw_source, "device_labels", ())
                )
                if not isinstance(source_id, str) or not source_id:
                    continue
                labels = (
                    tuple(
                        value
                        for value in raw_labels
                        if isinstance(value, str) and value
                    )
                    if isinstance(raw_labels, Sequence)
                    and not isinstance(raw_labels, str)
                    else ()
                )
                data_sources.append(
                    OpenWearablesExecutionDataSource(
                        data_source_id=source_id,
                        device_labels=labels,
                    )
                )
        data_sources.sort(key=lambda item: item.data_source_id)
        bindings.append(
            OpenWearablesExecutionSourceBinding(
                provider=normalized,
                active_connection_ids=values("active_connection_ids"),
                direct_data_source_ids=values("direct_data_source_ids"),
                import_data_source_ids=values("import_data_source_ids"),
                data_sources=tuple(data_sources),
            )
        )
    bindings.sort(key=lambda binding: binding.provider)
    return tuple(bindings)


def _provider_access_modes(
    snapshot: Any,
    allowed_providers: frozenset[str],
) -> tuple[tuple[bool, bool], ...]:
    """Return frozen ``(direct_api, imported)`` modes for selected providers."""

    modes: dict[str, tuple[bool, bool]] = {}
    for item in getattr(snapshot, "provider_bindings", ()):
        provider = (
            item.get("provider")
            if isinstance(item, Mapping)
            else getattr(item, "provider", None)
        )
        if not isinstance(provider, str):
            continue
        normalized = _identifier(provider)
        if normalized not in allowed_providers:
            continue
        direct_api = (
            item.get("direct_api")
            if isinstance(item, Mapping)
            else getattr(item, "direct_api", None)
        )
        imported = (
            item.get("imported")
            if isinstance(item, Mapping)
            else getattr(item, "imported", None)
        )
        if type(direct_api) is not bool or type(imported) is not bool:
            raise OpenWearablesBindingError(
                "Open Wearables provider access mode is invalid"
            )
        if not direct_api and not imported:
            raise OpenWearablesBindingError(
                "Open Wearables provider is not usable"
            )
        modes[normalized] = (direct_api, imported)
    if set(modes) != allowed_providers:
        raise OpenWearablesBindingError(
            "Open Wearables provider access mode is incomplete"
        )
    return tuple(modes[provider] for provider in sorted(modes))


def _available_capabilities(snapshot: Any) -> frozenset[str]:
    values = getattr(snapshot, "available_capabilities", ())
    return frozenset(
        _identifier(value)
        for value in values
        if isinstance(value, str) and value.strip()
    )


def _lineage_mode(
    snapshot: Any,
    capability: str,
) -> OpenWearablesLineageMode | None:
    binding = _capability_binding(snapshot, capability)
    raw = getattr(binding, "lineage_mode", None)
    if raw is None:
        return None
    try:
        return OpenWearablesLineageMode(raw)
    except (TypeError, ValueError):
        raise OpenWearablesBindingError(
            "Open Wearables capability lineage mode is invalid"
        ) from None


def _providers_for_capability(
    snapshot: Any,
    capability: str,
) -> frozenset[str]:
    method = getattr(snapshot, "providers_for", None)
    if callable(method):
        result = _invoke_supported(method, capability)
        if result is not _UNSUPPORTED:
            return _provider_set(result)
    binding = _capability_binding(snapshot, capability)
    return _provider_set(getattr(binding, "providers", ()))


def _parameter_values(
    snapshot: Any,
    capability: str,
    parameter: str,
) -> frozenset[str] | None:
    method = getattr(snapshot, "parameter_values", None)
    if callable(method):
        result = _invoke_supported(method, capability, parameter)
        if result is not _UNSUPPORTED and result is not None:
            return frozenset(
                value
                for value in result
                if isinstance(value, str)
            )
    binding = _parameter_binding(snapshot, capability, parameter)
    if binding is None:
        return None
    allowed_values = getattr(binding, "allowed_values", None)
    if callable(allowed_values):
        result = _invoke_supported(allowed_values)
        if result is not _UNSUPPORTED and result is not None:
            return frozenset(
                value
                for value in result
                if isinstance(value, str)
            )
    raw_values = getattr(binding, "values", None)
    if isinstance(raw_values, Mapping):
        return frozenset(str(value) for value in raw_values)
    if isinstance(raw_values, (Sequence, set, frozenset)) and not isinstance(
        raw_values,
        str,
    ):
        values: set[str] = set()
        for item in raw_values:
            if isinstance(item, str):
                values.add(item)
                continue
            raw_value = (
                item.get("value")
                if isinstance(item, Mapping)
                else getattr(item, "value", None)
            )
            if isinstance(raw_value, str):
                values.add(raw_value)
        return frozenset(values) if values else None
    return None


def _providers_for_parameter_value(
    snapshot: Any,
    capability: str,
    parameter: str,
    value: str,
) -> frozenset[str]:
    for name in (
        "providers_for_parameter_value",
        "parameter_providers",
        "providers_for_value",
        "providers_for",
    ):
        method = getattr(snapshot, name, None)
        if not callable(method):
            continue
        for args, kwargs in (
            ((capability, parameter, value), {}),
            (
                (capability,),
                {"parameter": parameter, "value": value},
            ),
        ):
            result = _invoke_supported(method, *args, **kwargs)
            if result is not _UNSUPPORTED:
                providers = _provider_set(result)
                if providers:
                    return providers

    capability_binding = _capability_binding(snapshot, capability)
    if capability_binding is not None:
        for name in (
            "providers_for_parameter_value",
            "parameter_providers",
            "providers_for_value",
            "providers_for",
        ):
            method = getattr(capability_binding, name, None)
            if not callable(method):
                continue
            result = _invoke_supported(method, parameter, value)
            if result is not _UNSUPPORTED:
                providers = _provider_set(result)
                if providers:
                    return providers

    parameter_binding = _parameter_binding(
        snapshot,
        capability,
        parameter,
    )
    if parameter_binding is None:
        return frozenset()
    for name in ("providers_for", "providers_for_value"):
        method = getattr(parameter_binding, name, None)
        if callable(method):
            result = _invoke_supported(method, value)
            if result is not _UNSUPPORTED:
                providers = _provider_set(result)
                if providers:
                    return providers
    return _providers_from_parameter_data(parameter_binding, value)


def _capability_binding(snapshot: Any, capability: str) -> Any | None:
    method = getattr(snapshot, "capability_binding", None)
    if callable(method):
        result = _invoke_supported(method, capability)
        if result is not _UNSUPPORTED:
            return result
    for item in getattr(snapshot, "capability_catalog", ()):
        if getattr(item, "capability", None) == capability:
            return item
    return None


def _parameter_binding(
    snapshot: Any,
    capability: str,
    parameter: str,
) -> Any | None:
    binding = _capability_binding(snapshot, capability)
    for item in getattr(binding, "parameters", ()):
        name = (
            item.get("name")
            if isinstance(item, Mapping)
            else getattr(item, "name", None)
        )
        if name == parameter:
            return item
    return None


def _providers_from_parameter_data(
    binding: Any,
    value: str,
) -> frozenset[str]:
    for field in (
        "providers_by_value",
        "value_providers",
        "provider_ownership",
        "ownership",
    ):
        raw = (
            binding.get(field)
            if isinstance(binding, Mapping)
            else getattr(binding, field, None)
        )
        if isinstance(raw, Mapping):
            providers = _provider_set(raw.get(value))
            if providers:
                return providers
        if isinstance(raw, Sequence) and not isinstance(raw, str):
            providers = _providers_from_value_bindings(raw, value)
            if providers:
                return providers
    raw_values = (
        binding.get("values")
        if isinstance(binding, Mapping)
        else getattr(binding, "values", ())
    )
    if isinstance(raw_values, Sequence) and not isinstance(raw_values, str):
        return _providers_from_value_bindings(raw_values, value)
    return frozenset()


def _providers_from_value_bindings(
    bindings: Sequence[Any],
    value: str,
) -> frozenset[str]:
    for item in bindings:
        item_value = (
            item.get("value")
            if isinstance(item, Mapping)
            else getattr(item, "value", None)
        )
        if item_value != value:
            continue
        providers = (
            item.get("providers")
            if isinstance(item, Mapping)
            else getattr(item, "providers", ())
        )
        return _provider_set(providers)
    return frozenset()


class _Unsupported:
    pass


_UNSUPPORTED = _Unsupported()


def _invoke_supported(
    function: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    try:
        inspect.signature(function).bind(*args, **kwargs)
    except (TypeError, ValueError):
        return _UNSUPPORTED
    return function(*args, **kwargs)


__all__ = [
    "OpenWearablesBindingError",
    "OpenWearablesExecutionBinding",
    "OpenWearablesExecutionSourceBinding",
    "OpenWearablesProviderBindingChangedError",
    "ProviderBoundWearableContext",
    "attest_provider_bound_wearable_context",
    "current_open_wearables_execution_binding",
    "open_wearables_execution_binding",
    "resolve_open_wearables_execution_binding",
]
