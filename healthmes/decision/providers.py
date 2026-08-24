"""Discoverable context-provider registry for the HealthMes decision agent."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy.orm import Session

from healthmes.decision.contracts import (
    ContextQuery,
    ContextResult,
    ContextStatus,
    PrivacyLevel,
)
from healthmes.decision.validation import strict_model_validate


class ProvenanceSupport(StrEnum):
    """Quality of source references a capability can return."""

    STABLE = "stable"
    PARTIAL = "partial"
    NONE = "none"


class ContextParameterType(StrEnum):
    """Primitive JSON type accepted by one provider parameter."""

    BOOLEAN = "boolean"
    INTEGER = "integer"
    STRING = "string"


class ContextParameterFormat(StrEnum):
    """Additional validation for string-valued parameters."""

    PLAIN = "plain"
    DATE = "date"
    UUID = "uuid"
    RELATED_RECORD_REF = "related_record_ref"


class ContextParameterSpec(BaseModel):
    """Typed and bounded provider parameter contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    value_type: ContextParameterType
    required: bool = False
    minimum: int | None = None
    maximum: int | None = None
    min_length: int | None = Field(default=None, ge=0, le=2_000)
    max_length: int | None = Field(default=None, ge=1, le=2_000)
    allowed_values: tuple[str, ...] = Field(default=(), max_length=64)
    format: ContextParameterFormat = ContextParameterFormat.PLAIN
    accepts_related_record_ref: bool = False

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        cleaned = value.strip().casefold()
        if not cleaned or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_.-"
            for character in cleaned
        ):
            raise ValueError("parameter name contains invalid characters")
        return cleaned

    @field_validator("allowed_values")
    @classmethod
    def validate_allowed_values(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("allowed_values must be unique")
        if any(not item or len(item) > 255 for item in value):
            raise ValueError(
                "allowed_values must be non-empty and at most 255 characters"
            )
        return value

    @model_validator(mode="after")
    def validate_bounds(self) -> ContextParameterSpec:
        if self.value_type is ContextParameterType.INTEGER:
            if self.minimum is None or self.maximum is None:
                raise ValueError(
                    "integer parameters require minimum and maximum"
                )
            if self.minimum > self.maximum:
                raise ValueError("minimum must not exceed maximum")
            if (
                self.min_length is not None
                or self.max_length is not None
                or self.allowed_values
                or self.format is not ContextParameterFormat.PLAIN
            ):
                raise ValueError(
                    "integer parameters cannot use string constraints"
                )
        elif self.value_type is ContextParameterType.BOOLEAN:
            if (
                self.minimum is not None
                or self.maximum is not None
                or self.min_length is not None
                or self.max_length is not None
                or self.allowed_values
                or self.format is not ContextParameterFormat.PLAIN
            ):
                raise ValueError(
                    "boolean parameters cannot use value constraints"
                )
        else:
            if self.max_length is None:
                raise ValueError("string parameters require max_length")
            if self.minimum is not None or self.maximum is not None:
                raise ValueError(
                    "string parameters cannot use numeric constraints"
                )
            if (
                self.min_length is not None
                and self.min_length > self.max_length
            ):
                raise ValueError("min_length must not exceed max_length")
            if any(
                len(item) > self.max_length
                for item in self.allowed_values
            ):
                raise ValueError(
                    "allowed_values must satisfy max_length"
                )
            if (
                self.accepts_related_record_ref
                and self.format is not ContextParameterFormat.UUID
            ):
                raise ValueError(
                    "related record parameters must use UUID provider format"
                )
        if (
            self.accepts_related_record_ref
            and self.value_type is not ContextParameterType.STRING
        ):
            raise ValueError(
                "related record parameters must be strings"
            )
        return self


_RELATED_RECORD_REF = re.compile(r"^rr_[0-9a-f]{16}$")


def validate_context_parameters(
    parameters: Mapping[str, JsonValue],
    specs: tuple[ContextParameterSpec, ...],
) -> dict[str, JsonValue]:
    """Validate parameter values without coercing model-provided JSON."""

    spec_by_name = {spec.name: spec for spec in specs}
    if not set(parameters).issubset(spec_by_name):
        raise ValueError("unsupported context parameters")
    missing = {
        spec.name
        for spec in specs
        if spec.required and spec.name not in parameters
    }
    if missing:
        raise ValueError("required context parameters are missing")

    validated: dict[str, JsonValue] = {}
    for name, value in parameters.items():
        spec = spec_by_name[name]
        if spec.value_type is ContextParameterType.BOOLEAN:
            if type(value) is not bool:
                raise ValueError(f"{name} must be a boolean")
        elif spec.value_type is ContextParameterType.INTEGER:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
            assert spec.minimum is not None
            assert spec.maximum is not None
            if not spec.minimum <= value <= spec.maximum:
                raise ValueError(f"{name} is outside its allowed range")
        else:
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
            if (
                spec.min_length is not None
                and len(value) < spec.min_length
            ):
                raise ValueError(f"{name} is shorter than allowed")
            assert spec.max_length is not None
            if len(value) > spec.max_length:
                raise ValueError(f"{name} is longer than allowed")
            if spec.allowed_values and value not in spec.allowed_values:
                raise ValueError(f"{name} is not an allowed value")
            if spec.format is ContextParameterFormat.DATE:
                try:
                    parsed_date = date.fromisoformat(value)
                except ValueError as exc:
                    raise ValueError(f"{name} must be an ISO date") from exc
                if parsed_date.isoformat() != value:
                    raise ValueError(f"{name} must be a canonical ISO date")
            elif spec.format is ContextParameterFormat.UUID:
                try:
                    candidate = value
                    if candidate.casefold().startswith("urn:uuid:"):
                        candidate = candidate[len("urn:uuid:") :]
                    uuid.UUID(candidate)
                except ValueError as exc:
                    raise ValueError(f"{name} must be a UUID") from exc
            elif (
                spec.format is ContextParameterFormat.RELATED_RECORD_REF
                and _RELATED_RECORD_REF.fullmatch(value) is None
            ):
                raise ValueError(
                    f"{name} must be a related record reference"
                )
        validated[name] = value
    return validated


class ContextCapability(BaseModel):
    """Model-visible description of one provider-owned read capability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=1_000)
    granularities: tuple[str, ...] = Field(min_length=1, max_length=16)
    query_fields: tuple[str, ...] = Field(default=(), max_length=32)
    output_fields: tuple[str, ...] = Field(default=(), max_length=128)
    nested_output_fields: tuple[str, ...] = Field(
        default=(),
        max_length=256,
    )
    identity_fields: tuple[str, ...] = Field(default=(), max_length=128)
    raw_fields: tuple[str, ...] = Field(default=(), max_length=128)
    limit_output_fields: tuple[str, ...] = Field(default=(), max_length=16)
    limitation_codes: tuple[str, ...] = Field(default=(), max_length=128)
    parameters: tuple[str, ...] = Field(default=(), max_length=32)
    parameter_specs: tuple[ContextParameterSpec, ...] = Field(
        default=(),
        max_length=32,
    )
    max_lookback_days: int = Field(ge=1, le=90)
    default_lookback_days: int = Field(default=1, ge=1, le=90)
    lookback_parameter: str | None = None
    lookback_parameter_offset_days: int = Field(default=0, ge=0, le=1)
    privacy_levels: tuple[PrivacyLevel, ...] = Field(
        default=(PrivacyLevel.AGGREGATE,),
        min_length=1,
        max_length=3,
    )
    sensitivity: str = Field(min_length=1, max_length=64)
    supports_raw: bool = False
    allows_future: bool = False
    provenance: ProvenanceSupport = ProvenanceSupport.STABLE
    freshness_expectation: str = Field(min_length=1, max_length=255)

    @field_validator(
        "capability",
        "granularities",
        "query_fields",
        "output_fields",
        "nested_output_fields",
        "identity_fields",
        "raw_fields",
        "limit_output_fields",
        "limitation_codes",
        "parameters",
        "sensitivity",
        "lookback_parameter",
    )
    @classmethod
    def normalize_identifiers(cls, value):
        if value is None:
            return None

        def normalize(item: str) -> str:
            cleaned = item.strip().casefold()
            if not cleaned:
                raise ValueError("provider identifiers must not be blank")
            if any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_.-"
                for character in cleaned
            ):
                raise ValueError(
                    "provider identifiers must use lowercase letters, digits, "
                    "underscore, dot, or hyphen"
                )
            return cleaned

        if isinstance(value, str):
            return normalize(value)
        normalized = tuple(normalize(item) for item in value)
        if len(normalized) != len(set(normalized)):
            raise ValueError("provider identifier collections must be unique")
        return normalized

    @field_validator("description", "freshness_expectation")
    @classmethod
    def strip_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("provider metadata text must not be blank")
        return cleaned

    @model_validator(mode="after")
    def validate_privacy(self) -> ContextCapability:
        if len(self.privacy_levels) != len(set(self.privacy_levels)):
            raise ValueError("privacy_levels must be unique")
        if self.supports_raw and PrivacyLevel.SCOPED_RAW not in self.privacy_levels:
            raise ValueError(
                "supports_raw capabilities must declare scoped_raw privacy"
            )
        if self.default_lookback_days > self.max_lookback_days:
            raise ValueError(
                "default_lookback_days cannot exceed max_lookback_days"
            )
        if (
            self.lookback_parameter is not None
            and self.lookback_parameter not in self.parameters
        ):
            raise ValueError(
                "lookback_parameter must be declared in parameters"
            )
        if (
            self.lookback_parameter_offset_days
            and self.lookback_parameter is None
        ):
            raise ValueError(
                "lookback_parameter_offset_days requires lookback_parameter"
            )
        if not set(self.limit_output_fields).issubset(self.output_fields):
            raise ValueError(
                "limit_output_fields must be declared in output_fields"
            )
        parameter_names = tuple(
            spec.name for spec in self.parameter_specs
        )
        if parameter_names != self.parameters:
            raise ValueError(
                "parameter_specs must exactly match parameters in order"
            )
        return self


class ContextProviderMetadata(BaseModel):
    """Stable identity and capabilities for one context provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str = Field(min_length=1, max_length=64)
    domain: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=1_000)
    version: str = Field(default="1", min_length=1, max_length=64)
    capabilities: tuple[ContextCapability, ...] = Field(
        min_length=1,
        max_length=64,
    )

    @field_validator("provider_id", "domain")
    @classmethod
    def normalize_identifier(cls, value: str) -> str:
        cleaned = value.strip().casefold()
        if not cleaned or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_.-"
            for character in cleaned
        ):
            raise ValueError("provider identifiers contain invalid characters")
        return cleaned

    @field_validator("description", "version")
    @classmethod
    def strip_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("provider metadata text must not be blank")
        return cleaned

    @model_validator(mode="after")
    def validate_capabilities(self) -> ContextProviderMetadata:
        names = [item.capability for item in self.capabilities]
        if len(names) != len(set(names)):
            raise ValueError("provider capabilities must be unique")
        expected_prefix = f"{self.domain}."
        if any(not name.startswith(expected_prefix) for name in names):
            raise ValueError(
                f"capabilities for domain {self.domain!r} must start with "
                f"{expected_prefix!r}"
            )
        return self

    def capability(self, name: str) -> ContextCapability | None:
        normalized = name.strip().casefold()
        return next(
            (
                capability
                for capability in self.capabilities
                if capability.capability == normalized
            ),
            None,
        )


class ContextProviderDescriptor(BaseModel):
    """Registry discovery response, including runtime enablement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metadata: ContextProviderMetadata
    enabled: bool


@runtime_checkable
class ContextProvider(Protocol):
    """A domain read adapter with no cross-domain decision authority.

    Decision Agent calls run on an agent-owned worker event loop. Providers must
    not reuse API-loop-bound async objects across that execution boundary.
    """

    @property
    def metadata(self) -> ContextProviderMetadata: ...

    async def query(
        self,
        session: Session,
        query: ContextQuery,
        *,
        now: datetime,
    ) -> ContextResult: ...


class ContextProviderRegistryError(RuntimeError):
    """Base error for fail-closed registry operations."""


class DuplicateProviderError(ContextProviderRegistryError):
    pass


class DuplicateCapabilityError(ContextProviderRegistryError):
    pass


class UnknownProviderError(ContextProviderRegistryError):
    pass


class DisabledProviderError(ContextProviderRegistryError):
    pass


class UnknownCapabilityError(ContextProviderRegistryError):
    pass


class ContextProviderRegistry:
    """Explicit provider composition without import-time registration."""

    def __init__(
        self,
        providers: Iterable[ContextProvider] = (),
    ) -> None:
        self._providers: dict[str, ContextProvider] = {}
        self._metadata: dict[str, ContextProviderMetadata] = {}
        self._enabled: dict[str, bool] = {}
        self._capability_owners: dict[str, str] = {}
        for provider in providers:
            self.register(provider)

    def register(
        self,
        provider: ContextProvider,
        *,
        enabled: bool = True,
    ) -> None:
        metadata = strict_model_validate(
            ContextProviderMetadata,
            provider.metadata,
        )
        provider_id = metadata.provider_id
        if provider_id in self._providers:
            raise DuplicateProviderError(
                f"context provider {provider_id!r} is already registered"
            )
        collisions = sorted(
            capability.capability
            for capability in metadata.capabilities
            if capability.capability in self._capability_owners
        )
        if collisions:
            raise DuplicateCapabilityError(
                "context capabilities are already registered: "
                + ", ".join(collisions)
            )
        self._providers[provider_id] = provider
        self._metadata[provider_id] = metadata
        self._enabled[provider_id] = enabled
        for capability in metadata.capabilities:
            self._capability_owners[capability.capability] = provider_id

    def set_enabled(self, provider_id: str, *, enabled: bool) -> None:
        normalized = provider_id.strip().casefold()
        if normalized not in self._providers:
            raise UnknownProviderError(
                f"unknown context provider {normalized!r}"
            )
        self._enabled[normalized] = enabled

    def descriptor(self, provider_id: str) -> ContextProviderDescriptor:
        normalized = provider_id.strip().casefold()
        provider = self._providers.get(normalized)
        if provider is None:
            raise UnknownProviderError(
                f"unknown context provider {normalized!r}"
            )
        return ContextProviderDescriptor(
            metadata=self._metadata[normalized],
            enabled=self._enabled[normalized],
        )

    def discover(
        self,
        *,
        domain: str | None = None,
        capability: str | None = None,
        include_disabled: bool = False,
    ) -> tuple[ContextProviderDescriptor, ...]:
        normalized_domain = domain.strip().casefold() if domain else None
        normalized_capability = (
            capability.strip().casefold() if capability else None
        )
        descriptors: list[ContextProviderDescriptor] = []
        for provider_id in sorted(self._providers):
            enabled = self._enabled[provider_id]
            metadata = self._metadata[provider_id]
            if not include_disabled and not enabled:
                continue
            if normalized_domain and metadata.domain != normalized_domain:
                continue
            if (
                normalized_capability
                and metadata.capability(normalized_capability) is None
            ):
                continue
            descriptors.append(
                ContextProviderDescriptor(
                    metadata=metadata,
                    enabled=enabled,
                )
            )
        return tuple(descriptors)

    def capability(
        self,
        capability: str,
        *,
        include_disabled: bool = False,
    ) -> tuple[ContextProviderDescriptor, ContextCapability]:
        normalized = capability.strip().casefold()
        provider_id = self._capability_owners.get(normalized)
        if provider_id is None:
            raise UnknownCapabilityError(
                f"unknown context capability {normalized!r}"
            )
        descriptor = self.descriptor(provider_id)
        if not include_disabled and not descriptor.enabled:
            raise DisabledProviderError(
                f"context provider {provider_id!r} is disabled"
            )
        metadata = descriptor.metadata.capability(normalized)
        assert metadata is not None
        return descriptor, metadata

    async def execute(
        self,
        session: Session,
        query: ContextQuery,
        *,
        now: datetime | None = None,
        ensure_active: Callable[[], None] | None = None,
    ) -> ContextResult:
        canonical_query = strict_model_validate(ContextQuery, query)
        provider = self._providers.get(canonical_query.provider_id)
        if provider is None:
            raise UnknownProviderError(
                "unknown context provider "
                f"{canonical_query.provider_id!r}"
            )
        if not self._enabled[canonical_query.provider_id]:
            raise DisabledProviderError(
                "context provider "
                f"{canonical_query.provider_id!r} is disabled"
            )
        owner = self._capability_owners.get(canonical_query.capability)
        if owner != canonical_query.provider_id:
            raise UnknownCapabilityError(
                f"provider {canonical_query.provider_id!r} does not own "
                f"capability {canonical_query.capability!r}"
            )
        if ensure_active is not None:
            ensure_active()
        try:
            result = await provider.query(
                session,
                canonical_query.model_copy(deep=True),
                now=(now or datetime.now(UTC)).astimezone(UTC),
            )
        except ValueError:
            return ContextResult(
                query_id=canonical_query.query_id,
                provider_id=canonical_query.provider_id,
                capability=canonical_query.capability,
                status=ContextStatus.FAILED,
                limitations=["invalid_provider_query"],
            )
        except Exception:
            return ContextResult(
                query_id=canonical_query.query_id,
                provider_id=canonical_query.provider_id,
                capability=canonical_query.capability,
                status=ContextStatus.FAILED,
                limitations=["provider_execution_failed"],
            )
        if not isinstance(result, ContextResult):
            return ContextResult(
                query_id=canonical_query.query_id,
                provider_id=canonical_query.provider_id,
                capability=canonical_query.capability,
                status=ContextStatus.FAILED,
                limitations=["provider_contract_violation"],
            )
        try:
            validated_result = strict_model_validate(
                ContextResult,
                result,
            )
        except (TypeError, ValueError, ValidationError):
            return ContextResult(
                query_id=canonical_query.query_id,
                provider_id=canonical_query.provider_id,
                capability=canonical_query.capability,
                status=ContextStatus.FAILED,
                limitations=["provider_contract_violation"],
            )
        if (
            validated_result.query_id != canonical_query.query_id
            or validated_result.provider_id
            != canonical_query.provider_id
            or validated_result.capability
            != canonical_query.capability
        ):
            return ContextResult(
                query_id=canonical_query.query_id,
                provider_id=canonical_query.provider_id,
                capability=canonical_query.capability,
                status=ContextStatus.FAILED,
                limitations=["provider_contract_violation"],
            )
        return validated_result
