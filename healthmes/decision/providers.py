"""Discoverable context-provider registry for the HealthMes decision agent."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from healthmes.decision.contracts import (
    ContextQuery,
    ContextResult,
    ContextStatus,
    PrivacyLevel,
)


class ProvenanceSupport(StrEnum):
    """Quality of source references a capability can return."""

    STABLE = "stable"
    PARTIAL = "partial"
    NONE = "none"


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
    """A domain-specific read adapter with no cross-domain decision authority."""

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
        metadata = provider.metadata
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
            metadata=provider.metadata,
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
            provider = self._providers[provider_id]
            enabled = self._enabled[provider_id]
            metadata = provider.metadata
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
    ) -> ContextResult:
        provider = self._providers.get(query.provider_id)
        if provider is None:
            raise UnknownProviderError(
                f"unknown context provider {query.provider_id!r}"
            )
        if not self._enabled[query.provider_id]:
            raise DisabledProviderError(
                f"context provider {query.provider_id!r} is disabled"
            )
        owner = self._capability_owners.get(query.capability)
        if owner != query.provider_id:
            raise UnknownCapabilityError(
                f"provider {query.provider_id!r} does not own "
                f"capability {query.capability!r}"
            )
        try:
            result = await provider.query(
                session,
                query,
                now=(now or datetime.now(UTC)).astimezone(UTC),
            )
        except ValueError:
            return ContextResult(
                query_id=query.query_id,
                provider_id=query.provider_id,
                capability=query.capability,
                status=ContextStatus.FAILED,
                limitations=["invalid_provider_query"],
            )
        except Exception:
            return ContextResult(
                query_id=query.query_id,
                provider_id=query.provider_id,
                capability=query.capability,
                status=ContextStatus.FAILED,
                limitations=["provider_execution_failed"],
            )
        if not isinstance(result, ContextResult) or (
            result.query_id != query.query_id
            or result.provider_id != query.provider_id
            or result.capability != query.capability
        ):
            return ContextResult(
                query_id=query.query_id,
                provider_id=query.provider_id,
                capability=query.capability,
                status=ContextStatus.FAILED,
                limitations=["provider_contract_violation"],
            )
        return result
