"""Runtime-neutral model iteration contracts for HealthMes decisions."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from healthmes.decision.contracts import (
    ContextCoverage,
    ContextFreshness,
    ContextStatus,
    DecisionDraft,
    PrivacyLevel,
    RuntimeMetadata,
)
from healthmes.decision.providers import (
    ContextParameterSpec,
    ProvenanceSupport,
)

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.-]*$")


def _identifier(value: str, *, label: str, max_length: int = 128) -> str:
    normalized = value.strip().casefold()
    if (
        not normalized
        or len(normalized) > max_length
        or _IDENTIFIER.fullmatch(normalized) is None
    ):
        raise ValueError(f"{label} contains invalid characters")
    return normalized


def _text(value: str, *, label: str, max_length: int) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label} must not be blank")
    if len(cleaned) > max_length:
        raise ValueError(f"{label} must be at most {max_length} characters")
    return cleaned


class DecisionToolSpec(BaseModel):
    """One model-visible capability already filtered by HealthMes policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: str = Field(min_length=1, max_length=128)
    provider_id: str = Field(min_length=1, max_length=64)
    domain: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=1_000)
    granularities: tuple[str, ...] = Field(min_length=1, max_length=16)
    query_fields: tuple[str, ...] = Field(default=(), max_length=32)
    output_fields: tuple[str, ...] = Field(default=(), max_length=128)
    parameters: tuple[str, ...] = Field(default=(), max_length=32)
    parameter_specs: tuple[ContextParameterSpec, ...] = Field(
        default=(),
        max_length=32,
    )
    privacy_levels: tuple[PrivacyLevel, ...] = Field(
        min_length=1,
        max_length=3,
    )
    max_lookback_days: int = Field(ge=1, le=90)
    max_rows: int = Field(ge=1, le=1_000)
    supports_raw: bool = False
    allows_future: bool = False
    provenance: ProvenanceSupport
    freshness_expectation: str = Field(min_length=1, max_length=255)

    @field_validator(
        "capability",
        "provider_id",
        "domain",
        "granularities",
        "query_fields",
        "output_fields",
        "parameters",
    )
    @classmethod
    def normalize_identifiers(cls, value):
        if isinstance(value, str):
            return _identifier(value, label="tool identifier")
        normalized = tuple(
            _identifier(item, label="tool identifier") for item in value
        )
        if len(normalized) != len(set(normalized)):
            raise ValueError("tool identifier collections must be unique")
        return normalized

    @field_validator("description", "freshness_expectation")
    @classmethod
    def normalize_text(cls, value: str, info) -> str:
        return _text(value, label=info.field_name, max_length=1_000)

    @model_validator(mode="after")
    def validate_spec(self) -> DecisionToolSpec:
        if len(self.privacy_levels) != len(set(self.privacy_levels)):
            raise ValueError("privacy_levels must be unique")
        if tuple(
            spec.name for spec in self.parameter_specs
        ) != self.parameters:
            raise ValueError(
                "parameter_specs must exactly match parameters in order"
            )
        if (
            self.supports_raw
            and PrivacyLevel.SCOPED_RAW not in self.privacy_levels
        ):
            raise ValueError("supports_raw requires scoped_raw privacy")
        return self


class ContextToolCall(BaseModel):
    """Provider-independent context request proposed by one model iteration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: str = Field(min_length=1, max_length=128)
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    granularity: str = Field(default="summary", min_length=1, max_length=64)
    fields: tuple[str, ...] = Field(default=(), max_length=64)
    privacy_level: PrivacyLevel = PrivacyLevel.AGGREGATE
    limit: int = Field(default=100, ge=1, le=1_000)
    parameters: dict[str, JsonValue] = Field(
        default_factory=dict,
        max_length=64,
    )
    purpose: str | None = Field(default=None, max_length=500)

    @field_validator("capability", "granularity")
    @classmethod
    def normalize_identifier(cls, value: str, info) -> str:
        return _identifier(value, label=info.field_name)

    @field_validator("fields")
    @classmethod
    def normalize_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            _identifier(item, label="field") for item in value
        )
        if len(normalized) != len(set(normalized)):
            raise ValueError("fields must be unique")
        return normalized

    @field_validator("parameters")
    @classmethod
    def normalize_parameters(
        cls,
        value: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        normalized: dict[str, JsonValue] = {}
        for key, item in value.items():
            clean_key = _identifier(key, label="parameter key")
            if clean_key in normalized:
                raise ValueError(
                    "parameters contain duplicate normalized keys"
                )
            normalized[clean_key] = item
        return normalized

    @field_validator("start", "end", mode="after")
    @classmethod
    def normalize_time(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @field_validator("purpose")
    @classmethod
    def normalize_purpose(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _text(value, label="purpose", max_length=500)

    @model_validator(mode="after")
    def validate_range(self) -> ContextToolCall:
        if (self.start is None) != (self.end is None):
            raise ValueError("start and end must be provided together")
        if (
            self.start is not None
            and self.end is not None
            and self.start >= self.end
        ):
            raise ValueError("start must be before end")
        return self


class RuntimeContextResult(BaseModel):
    """Minimum gateway result visible to an interchangeable model runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str = Field(min_length=1, max_length=64)
    capability: str = Field(min_length=1, max_length=128)
    status: ContextStatus
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    source_ref_ids: tuple[str, ...] = Field(default=(), max_length=500)
    freshness: ContextFreshness = Field(default_factory=ContextFreshness)
    coverage: ContextCoverage = Field(default_factory=ContextCoverage)
    limitations: tuple[str, ...] = Field(default=(), max_length=100)
    truncated: bool = False

    @field_validator("provider_id", "capability")
    @classmethod
    def normalize_result_identifiers(cls, value: str, info) -> str:
        return _identifier(value, label=info.field_name)

    @field_validator("source_ref_ids")
    @classmethod
    def validate_source_ref_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("source_ref_ids must be unique")
        if any(
            re.fullmatch(r"sr_[0-9a-f]{32}", item) is None
            for item in value
        ):
            raise ValueError("source_ref_ids contain an invalid reference")
        return value

    @field_validator("limitations")
    @classmethod
    def validate_limitations(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        cleaned = tuple(
            _identifier(item, label="limitation", max_length=255)
            for item in value
        )
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("limitations must be unique")
        return cleaned

    @model_validator(mode="after")
    def validate_result(self) -> RuntimeContextResult:
        if self.status in {
            ContextStatus.DENIED,
            ContextStatus.UNAVAILABLE,
            ContextStatus.FAILED,
        } and self.payload:
            raise ValueError(
                "non-readable runtime context must not expose payload"
            )
        if self.status is ContextStatus.DENIED and self.source_ref_ids:
            raise ValueError(
                "denied runtime context must not expose source refs"
            )
        return self


class RuntimeToolExchange(BaseModel):
    """Validated tool requests and gateway results from one prior step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_number: int = Field(ge=1, le=32)
    tool_calls: tuple[ContextToolCall, ...] = Field(
        min_length=1,
        max_length=32,
    )
    results: tuple[RuntimeContextResult, ...] = Field(
        min_length=1,
        max_length=32,
    )

    @model_validator(mode="after")
    def validate_exchange(self) -> RuntimeToolExchange:
        if len(self.tool_calls) != len(self.results):
            raise ValueError("tool_calls and results must have equal length")
        for call, result in zip(
            self.tool_calls,
            self.results,
            strict=True,
        ):
            if call.capability != result.capability:
                raise ValueError("tool result capability must match its call")
        return self


class RuntimeRelatedRecord(BaseModel):
    """Turn-scoped opaque handle for a caller-selected stored record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: str = Field(pattern=r"^rr_[0-9a-f]{16}$")
    domain: str = Field(min_length=1, max_length=64)

    @field_validator("domain")
    @classmethod
    def normalize_domain(cls, value: str) -> str:
        return _identifier(value, label="related record domain")


class RuntimeDecisionContextHints(BaseModel):
    """Caller hints safe to expose to an interchangeable model runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    local_date: date | None = None
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    lookback_days: int | None = Field(default=None, ge=1, le=90)
    has_related_records: bool = False
    related_domains: tuple[str, ...] = Field(
        default=(),
        max_length=32,
    )
    related_records: tuple[RuntimeRelatedRecord, ...] = Field(
        default=(),
        max_length=32,
    )

    @field_validator("start", "end", mode="after")
    @classmethod
    def normalize_time(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @field_validator("related_domains")
    @classmethod
    def normalize_related_domains(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        normalized = tuple(
            _identifier(item, label="related domain")
            for item in value
        )
        if len(normalized) != len(set(normalized)):
            raise ValueError("related_domains must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_range(self) -> RuntimeDecisionContextHints:
        if (self.start is None) != (self.end is None):
            raise ValueError("start and end must be provided together")
        if (
            self.start is not None
            and self.end is not None
            and self.start >= self.end
        ):
            raise ValueError("start must be before end")
        references = [item.reference for item in self.related_records]
        if len(references) != len(set(references)):
            raise ValueError("related record references must be unique")
        record_domains = tuple(
            sorted({item.domain for item in self.related_records})
        )
        if record_domains != tuple(sorted(self.related_domains)):
            raise ValueError(
                "related_domains must match related_records"
            )
        if self.related_records and not self.has_related_records:
            raise ValueError(
                "related_records require has_related_records"
            )
        return self


class RuntimeDecisionRequest(BaseModel):
    """Minimum request context visible to a local or hosted model runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str = Field(min_length=1, max_length=8_000)
    requested_at: AwareDatetime
    timezone: str = Field(min_length=1, max_length=64)
    requested_privacy_level: PrivacyLevel = PrivacyLevel.AGGREGATE
    hints: RuntimeDecisionContextHints = Field(
        default_factory=RuntimeDecisionContextHints
    )

    @field_validator("question", "timezone")
    @classmethod
    def normalize_text(cls, value: str, info) -> str:
        max_length = 8_000 if info.field_name == "question" else 64
        return _text(
            value,
            label=info.field_name,
            max_length=max_length,
        )

    @field_validator("requested_at", mode="after")
    @classmethod
    def normalize_requested_at(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class DecisionRuntimeTurn(BaseModel):
    """Read-only snapshot for exactly one model iteration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request: RuntimeDecisionRequest
    system_policy: str = Field(min_length=1, max_length=16_000)
    system_policy_version: str = Field(min_length=1, max_length=128)
    tools: tuple[DecisionToolSpec, ...] = Field(
        default=(),
        max_length=1_024,
    )
    history: tuple[RuntimeToolExchange, ...] = Field(
        default=(),
        max_length=32,
    )
    step_number: int = Field(ge=1, le=32)
    remaining_steps: int = Field(ge=1, le=32)

    @field_validator("system_policy", "system_policy_version")
    @classmethod
    def normalize_policy_text(cls, value: str, info) -> str:
        return _text(value, label=info.field_name, max_length=16_000)

    @model_validator(mode="after")
    def validate_history(self) -> DecisionRuntimeTurn:
        expected_steps = tuple(range(1, self.step_number))
        actual_steps = tuple(item.step_number for item in self.history)
        if actual_steps != expected_steps:
            raise ValueError(
                "history must contain one ordered exchange per prior step"
            )
        return self


class RuntimeStepOutput(BaseModel):
    """One model iteration: either tool requests or a final draft."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_calls: tuple[dict[str, Any], ...] = Field(
        default=(),
        max_length=32,
    )
    draft: DecisionDraft | None = None
    metadata: RuntimeMetadata

    @model_validator(mode="after")
    def validate_single_action(self) -> RuntimeStepOutput:
        has_tools = bool(self.tool_calls)
        has_draft = self.draft is not None
        if has_tools == has_draft:
            raise ValueError(
                "runtime step must return exactly one of tool_calls or draft"
            )
        return self


@runtime_checkable
class DecisionRuntime(Protocol):
    """Exchangeable runtime that performs one isolated model iteration.

    Calls run on one agent-owned worker event loop, not necessarily the API
    loop. Whole decision turns are serialized for one runtime instance.
    Implementations must create loop-bound async clients within that boundary
    or use a worker-safe client factory.
    """

    @property
    def metadata(self) -> RuntimeMetadata: ...

    async def next_step(
        self,
        turn: DecisionRuntimeTurn,
    ) -> RuntimeStepOutput: ...


class DecisionToolCallError(RuntimeError):
    """Fail-closed validation or execution error for one proposed tool call."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
