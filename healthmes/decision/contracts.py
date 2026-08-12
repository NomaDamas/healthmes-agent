"""Runtime-neutral contracts owned by the HealthMes decision layer."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from healthmes.timezones import parse_timezone

MAX_QUESTION_LENGTH = 8_000
MAX_ANSWER_LENGTH = 16_000
MAX_LIMITATIONS = 100
MAX_SOURCE_REFS = 500
MAX_TOOL_TRACE = 64

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.-]*$")
_SOURCE_REF_ID = re.compile(r"^sr_[0-9a-f]{32}$")
_CONTENT_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _calendar_day_count(
    start: datetime,
    end: datetime,
    *,
    timezone: str,
) -> int:
    zone = parse_timezone(timezone)
    local_start = start.astimezone(zone).replace(tzinfo=None)
    local_end = end.astimezone(zone).replace(tzinfo=None)
    duration = local_end - local_start
    whole_days = duration.days
    return max(
        1,
        whole_days
        + (1 if duration > timedelta(days=whole_days) else 0),
    )


def _identifier(value: str, *, label: str, max_length: int = 128) -> str:
    normalized = value.strip().casefold()
    if (
        not normalized
        or len(normalized) > max_length
        or _IDENTIFIER.fullmatch(normalized) is None
    ):
        raise ValueError(
            f"{label} must match {_IDENTIFIER.pattern!r} and be at most "
            f"{max_length} characters"
        )
    return normalized


def _bounded_text(value: str, *, label: str, max_length: int) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label} must not be blank")
    if len(cleaned) > max_length:
        raise ValueError(f"{label} must be at most {max_length} characters")
    return cleaned


def _unique_strings(
    values: list[str],
    *,
    label: str,
    max_item_length: int,
) -> list[str]:
    cleaned = [
        _bounded_text(
            value,
            label=f"{label} item",
            max_length=max_item_length,
        )
        for value in values
    ]
    if len(cleaned) != len(set(cleaned)):
        raise ValueError(f"{label} must contain unique values")
    return cleaned


class PrivacyLevel(StrEnum):
    """Maximum data detail requested for one decision turn."""

    AGGREGATE = "aggregate"
    IDENTITY = "identity"
    SCOPED_RAW = "scoped_raw"


class ExecutionScope(StrEnum):
    LOCAL = "local"
    HOSTED = "hosted"


class DecisionStatus(StrEnum):
    COMPLETED = "completed"
    NEEDS_CLARIFICATION = "needs_clarification"
    BLOCKED = "blocked"
    FAILED = "failed"


class ContextStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class FreshnessStatus(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class CoverageStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class ToolCallStatus(StrEnum):
    COMPLETED = "completed"
    DENIED = "denied"
    FAILED = "failed"


class PersistenceStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    PERSISTED = "persisted"
    FAILED = "failed"


class CompatibilityPreset(StrEnum):
    """Legacy resolver presets; never required by the decision core."""

    ACTIVITY_SUMMARY = "activity_summary"
    FOCUS = "focus"
    OVERWORK = "overwork"
    RECOVERY = "recovery"
    CAFFEINE_FOR_FOCUS = "caffeine_for_focus"


_COMPATIBILITY_QUESTIONS = {
    CompatibilityPreset.ACTIVITY_SUMMARY: "Summarize my activity.",
    CompatibilityPreset.FOCUS: "What affected my focus?",
    CompatibilityPreset.OVERWORK: "Am I overworking?",
    CompatibilityPreset.RECOVERY: "How is my recovery?",
    CompatibilityPreset.CAFFEINE_FOR_FOCUS: (
        "Would caffeine be a reasonable choice for focus?"
    ),
}


class DecisionCaller(BaseModel):
    """Authenticated caller metadata supplied by the API/runtime boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_id: str = Field(min_length=1, max_length=255)
    authenticated: bool
    execution_scope: ExecutionScope
    session_id: str | None = Field(default=None, max_length=255)
    channel: str = Field(default="service", min_length=1, max_length=64)

    @field_validator("principal_id", "session_id", "channel")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("caller text fields must not be blank")
        return cleaned


class DecisionContextHints(BaseModel):
    """Optional caller facts that constrain, but do not route, tool planning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    local_date: date | None = None
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    lookback_days: int | None = Field(default=None, ge=1, le=90)
    related_record_ids: dict[str, str] = Field(
        default_factory=dict,
        max_length=32,
    )

    @field_validator("start", "end", mode="after")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value is not None else None

    @field_validator("related_record_ids")
    @classmethod
    def validate_related_record_ids(
        cls,
        value: dict[str, str],
    ) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, record_id in value.items():
            clean_key = _identifier(
                key,
                label="related record key",
                max_length=64,
            )
            clean_id = _bounded_text(
                record_id,
                label="related record id",
                max_length=255,
            )
            normalized[clean_key] = clean_id
        return normalized

    @model_validator(mode="after")
    def validate_range(self) -> DecisionContextHints:
        if (self.start is None) != (self.end is None):
            raise ValueError("start and end must be provided together")
        if self.start is not None and self.end is not None:
            if self.start >= self.end:
                raise ValueError("start must be before end")
        return self


class DecisionBudget(BaseModel):
    """Hard turn bounds enforced independently from model instructions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_steps: int = Field(default=8, ge=1, le=32)
    max_tool_calls: int = Field(default=8, ge=1, le=32)
    max_source_refs: int = Field(default=200, ge=1, le=MAX_SOURCE_REFS)
    max_context_bytes: int = Field(
        default=256_000,
        ge=1_024,
        le=2_000_000,
    )


class DecisionRequest(BaseModel):
    """Natural-language request accepted by the HealthMes decision core."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    turn_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    requested_at: AwareDatetime = Field(
        default_factory=lambda: datetime.now(UTC)
    )
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    caller: DecisionCaller
    requested_privacy_level: PrivacyLevel = PrivacyLevel.AGGREGATE
    budget: DecisionBudget = Field(default_factory=DecisionBudget)
    hints: DecisionContextHints = Field(default_factory=DecisionContextHints)
    compatibility_preset: CompatibilityPreset | None = None

    @field_validator("question")
    @classmethod
    def validate_question(cls, value: str) -> str:
        return _bounded_text(
            value,
            label="question",
            max_length=MAX_QUESTION_LENGTH,
        )

    @field_validator("requested_at", mode="after")
    @classmethod
    def normalize_requested_at(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            parse_timezone(value)
        except ValueError as exc:
            raise ValueError(
                "timezone must be a valid IANA name or UTC offset"
            ) from exc
        return value

    @model_validator(mode="after")
    def validate_hint_range(self) -> DecisionRequest:
        if self.hints.start is not None and self.hints.end is not None:
            if (
                _calendar_day_count(
                    self.hints.start,
                    self.hints.end,
                    timezone=self.timezone,
                )
                > 90
            ):
                raise ValueError(
                    "one decision hint range cannot exceed 90 local days"
                )
        return self

    @classmethod
    def from_compatibility_preset(
        cls,
        preset: CompatibilityPreset,
        *,
        caller: DecisionCaller,
        timezone: str,
        question: str | None = None,
        requested_at: datetime | None = None,
        hints: DecisionContextHints | None = None,
        requested_privacy_level: PrivacyLevel = PrivacyLevel.AGGREGATE,
    ) -> DecisionRequest:
        return cls(
            question=question or _COMPATIBILITY_QUESTIONS[preset],
            requested_at=requested_at or datetime.now(UTC),
            timezone=timezone,
            caller=caller,
            requested_privacy_level=requested_privacy_level,
            hints=hints or DecisionContextHints(),
            compatibility_preset=preset,
        )


class ContextQuery(BaseModel):
    """Provider-independent query selected by a decision runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    provider_id: str = Field(min_length=1, max_length=64)
    capability: str = Field(min_length=1, max_length=128)
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    granularity: str = Field(default="summary", min_length=1, max_length=64)
    fields: list[str] = Field(default_factory=list, max_length=64)
    privacy_level: PrivacyLevel = PrivacyLevel.AGGREGATE
    limit: int = Field(default=100, ge=1, le=1_000)
    parameters: dict[str, JsonValue] = Field(
        default_factory=dict,
        max_length=64,
    )
    purpose: str | None = Field(default=None, max_length=500)

    @field_validator(
        "provider_id",
        "capability",
        "granularity",
    )
    @classmethod
    def validate_identifiers(cls, value: str, info) -> str:
        return _identifier(
            value,
            label=info.field_name,
            max_length=128,
        )

    @field_validator("start", "end", mode="after")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value is not None else None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            parse_timezone(value)
        except ValueError as exc:
            raise ValueError(
                "timezone must be a valid IANA name or UTC offset"
            ) from exc
        return value

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, value: list[str]) -> list[str]:
        return [
            _identifier(item, label="field", max_length=128)
            for item in _unique_strings(
                value,
                label="fields",
                max_item_length=128,
            )
        ]

    @field_validator("parameters")
    @classmethod
    def validate_parameter_keys(
        cls,
        value: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        return {
            _identifier(key, label="parameter key", max_length=128): item
            for key, item in value.items()
        }

    @field_validator("purpose")
    @classmethod
    def validate_purpose(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_text(value, label="purpose", max_length=500)

    @model_validator(mode="after")
    def validate_range(self) -> ContextQuery:
        if (self.start is None) != (self.end is None):
            raise ValueError("start and end must be provided together")
        if self.start is not None and self.end is not None:
            if self.start >= self.end:
                raise ValueError("start must be before end")
            if (
                _calendar_day_count(
                    self.start,
                    self.end,
                    timezone=self.timezone,
                )
                > 90
            ):
                raise ValueError("one context query cannot exceed 90 days")
        return self


class ContextFreshness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: FreshnessStatus = FreshnessStatus.UNKNOWN
    as_of: AwareDatetime | None = None
    age_seconds: int | None = Field(default=None, ge=0)

    @field_validator("as_of", mode="after")
    @classmethod
    def normalize_as_of(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value is not None else None

    @model_validator(mode="after")
    def validate_known_freshness(self) -> ContextFreshness:
        if self.status in {
            FreshnessStatus.CURRENT,
            FreshnessStatus.STALE,
        } and self.as_of is None:
            raise ValueError("current or stale freshness requires as_of")
        if self.age_seconds is not None and self.as_of is None:
            raise ValueError("age_seconds requires as_of")
        return self


class ContextCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: CoverageStatus = CoverageStatus.UNKNOWN
    ratio: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_ratio(self) -> ContextCoverage:
        if self.status is CoverageStatus.COMPLETE and self.ratio != 1:
            raise ValueError("complete coverage requires ratio=1")
        if self.status is CoverageStatus.PARTIAL and self.ratio is None:
            raise ValueError("partial coverage requires an explicit ratio")
        if self.status in {
            CoverageStatus.UNKNOWN,
            CoverageStatus.UNAVAILABLE,
        } and self.ratio is not None:
            raise ValueError(
                "unknown or unavailable coverage must not invent a ratio"
            )
        return self


def source_ref_id(
    *,
    domain: str,
    resource_type: str,
    source_provider: str,
    record_id: str,
) -> str:
    """Return a stable opaque ID for one provider-owned source record."""

    identity = json.dumps(
        [
            domain.strip().casefold(),
            resource_type.strip().casefold(),
            source_provider.strip().casefold(),
            record_id.strip(),
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return "sr_" + hashlib.sha256(identity.encode()).hexdigest()[:32]


class SourceRef(BaseModel):
    """Traceable address of data actually returned by one context tool."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )

    reference_id: str = Field(pattern=_SOURCE_REF_ID.pattern)
    domain: str = Field(min_length=1, max_length=64)
    resource_type: str = Field(min_length=1, max_length=128)
    record_id: str = Field(min_length=1, max_length=512)
    source_provider: str = Field(min_length=1, max_length=128)
    observed_start: AwareDatetime
    observed_end: AwareDatetime | None = None
    schema_version: int = Field(default=1, ge=1)
    derived_by: str | None = Field(default=None, max_length=128)
    freshness: FreshnessStatus = FreshnessStatus.UNKNOWN
    coverage: float | None = Field(default=None, ge=0, le=1)
    content_digest: str | None = Field(
        default=None,
        pattern=_CONTENT_DIGEST.pattern,
    )
    sensitivity: str = Field(
        default="wellness",
        min_length=1,
        max_length=64,
    )

    @model_validator(mode="before")
    @classmethod
    def populate_and_validate_reference_id(cls, value):
        if not isinstance(value, dict):
            return value
        required = (
            value.get("domain"),
            value.get("resource_type"),
            value.get("source_provider"),
            value.get("record_id"),
        )
        if not all(isinstance(item, str) and item for item in required):
            return value
        expected = source_ref_id(
            domain=str(value["domain"]),
            resource_type=str(value["resource_type"]),
            source_provider=str(value["source_provider"]),
            record_id=str(value["record_id"]),
        )
        supplied = value.get("reference_id")
        if supplied is not None and supplied != expected:
            raise ValueError(
                "reference_id does not match the source identity"
            )
        normalized = dict(value)
        normalized["reference_id"] = expected
        return normalized

    @field_validator(
        "domain",
        "resource_type",
        "source_provider",
        "sensitivity",
    )
    @classmethod
    def validate_identifiers(cls, value: str, info) -> str:
        return _identifier(
            value,
            label=info.field_name,
            max_length=128,
        )

    @field_validator("record_id")
    @classmethod
    def validate_record_id(cls, value: str) -> str:
        return _bounded_text(value, label="record_id", max_length=512)

    @field_validator("derived_by")
    @classmethod
    def validate_derived_by(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _identifier(value, label="derived_by", max_length=128)

    @field_validator("observed_start", "observed_end", mode="after")
    @classmethod
    def normalize_observed_time(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        return _utc(value) if value is not None else None

    @model_validator(mode="after")
    def validate_observed_range(self) -> SourceRef:
        expected_reference_id = source_ref_id(
            domain=self.domain,
            resource_type=self.resource_type,
            source_provider=self.source_provider,
            record_id=self.record_id,
        )
        if self.reference_id != expected_reference_id:
            raise ValueError(
                "reference_id does not match the source identity"
            )
        if (
            self.observed_end is not None
            and self.observed_end <= self.observed_start
        ):
            raise ValueError("observed_end must be after observed_start")
        return self


class RawSourceHandle(BaseModel):
    """Opaque handle for one explicitly selected retained raw object."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_ref_id: str = Field(pattern=_SOURCE_REF_ID.pattern)
    storage_object_id: uuid.UUID
    content_type: str | None = Field(default=None, max_length=255)
    size_bytes: int = Field(ge=0)
    sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @field_validator("content_type")
    @classmethod
    def validate_content_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_text(
            value,
            label="content_type",
            max_length=255,
        )


class ContextResult(BaseModel):
    """Policy-ready result returned by a registered context provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query_id: uuid.UUID
    provider_id: str = Field(min_length=1, max_length=64)
    capability: str = Field(min_length=1, max_length=128)
    status: ContextStatus
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    source_refs: list[SourceRef] = Field(
        default_factory=list,
        max_length=MAX_SOURCE_REFS,
    )
    raw_sources: list[RawSourceHandle] = Field(
        default_factory=list,
        max_length=MAX_SOURCE_REFS,
    )
    freshness: ContextFreshness = Field(default_factory=ContextFreshness)
    coverage: ContextCoverage = Field(default_factory=ContextCoverage)
    limitations: list[str] = Field(
        default_factory=list,
        max_length=MAX_LIMITATIONS,
    )
    truncated: bool = False
    next_cursor: str | None = Field(default=None, max_length=512)

    @field_validator("provider_id", "capability")
    @classmethod
    def validate_identifiers(cls, value: str, info) -> str:
        return _identifier(
            value,
            label=info.field_name,
            max_length=128,
        )

    @field_validator("limitations")
    @classmethod
    def validate_limitations(cls, value: list[str]) -> list[str]:
        return _unique_strings(
            value,
            label="limitations",
            max_item_length=255,
        )

    @field_validator("next_cursor")
    @classmethod
    def validate_next_cursor(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_text(value, label="next_cursor", max_length=512)

    @model_validator(mode="after")
    def validate_result(self) -> ContextResult:
        reference_ids = [item.reference_id for item in self.source_refs]
        if len(reference_ids) != len(set(reference_ids)):
            raise ValueError("source_refs must contain unique references")
        raw_source_ids = [
            item.storage_object_id for item in self.raw_sources
        ]
        if len(raw_source_ids) != len(set(raw_source_ids)):
            raise ValueError("raw_sources must contain unique objects")
        if any(
            item.source_ref_id not in set(reference_ids)
            for item in self.raw_sources
        ):
            raise ValueError("raw_sources must reference returned source_refs")
        if self.status is ContextStatus.DENIED and self.source_refs:
            raise ValueError("denied context must not expose source_refs")
        if self.status in {
            ContextStatus.DENIED,
            ContextStatus.UNAVAILABLE,
            ContextStatus.FAILED,
        } and (self.payload or self.raw_sources):
            raise ValueError(
                "denied, unavailable, or failed context must not expose data"
            )
        if self.next_cursor is not None and not self.truncated:
            raise ValueError("next_cursor requires truncated=true")
        return self


class ToolCallRecord(BaseModel):
    """Runtime-independent trace of one gateway-mediated context call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    query: ContextQuery
    status: ToolCallStatus
    started_at: AwareDatetime
    finished_at: AwareDatetime
    result: ContextResult | None = None
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=500)

    @field_validator("started_at", "finished_at", mode="after")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("error_code")
    @classmethod
    def validate_error_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _identifier(value, label="error_code", max_length=128)

    @field_validator("error_message")
    @classmethod
    def validate_error_message(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_text(
            value,
            label="error_message",
            max_length=500,
        )

    @model_validator(mode="after")
    def validate_trace(self) -> ToolCallRecord:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not be before started_at")
        if self.status is ToolCallStatus.COMPLETED:
            if self.result is None:
                raise ValueError("completed tool calls require a result")
            if self.result.status in {
                ContextStatus.DENIED,
                ContextStatus.FAILED,
            }:
                raise ValueError(
                    "completed tool calls cannot contain denied/failed results"
                )
        elif self.status is ToolCallStatus.DENIED:
            if (
                self.result is None
                or self.result.status is not ContextStatus.DENIED
            ):
                raise ValueError(
                    "denied tool calls require a denied context result"
                )
        elif self.error_code is None:
            raise ValueError("failed tool calls require error_code")
        if self.result is not None and (
            self.result.query_id != self.query.query_id
            or self.result.provider_id != self.query.provider_id
            or self.result.capability != self.query.capability
        ):
            raise ValueError("tool result must match its context query")
        return self


class RuntimeMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime: str = Field(min_length=1, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    provider: str | None = Field(default=None, max_length=128)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)

    @field_validator("runtime")
    @classmethod
    def validate_runtime(cls, value: str) -> str:
        return _identifier(value, label="runtime", max_length=64)

    @field_validator("model", "provider")
    @classmethod
    def validate_runtime_selection(
        cls,
        value: str | None,
        info,
    ) -> str | None:
        if value is None:
            return None
        return _bounded_text(
            value,
            label=info.field_name,
            max_length=128,
        )


class DecisionDraft(BaseModel):
    """Structured model output before provenance and persistence checks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: DecisionStatus
    answer: str | None = Field(default=None, max_length=MAX_ANSWER_LENGTH)
    proposed_action: bool = False
    used_source_ref_ids: list[str] = Field(
        default_factory=list,
        max_length=MAX_SOURCE_REFS,
    )
    limitations: list[str] = Field(
        default_factory=list,
        max_length=MAX_LIMITATIONS,
    )
    clarification_question: str | None = Field(
        default=None,
        max_length=2_000,
    )
    confidence: float | None = Field(default=None, ge=0, le=1)
    uncertainty: str | None = Field(default=None, max_length=2_000)
    follow_up_question: str | None = Field(default=None, max_length=2_000)

    @field_validator("answer")
    @classmethod
    def validate_answer(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_text(
            value,
            label="answer",
            max_length=MAX_ANSWER_LENGTH,
        )

    @field_validator(
        "clarification_question",
        "uncertainty",
        "follow_up_question",
    )
    @classmethod
    def validate_optional_text(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None
        return _bounded_text(
            value,
            label="decision draft text",
            max_length=2_000,
        )

    @field_validator("used_source_ref_ids")
    @classmethod
    def validate_source_ref_ids(cls, value: list[str]) -> list[str]:
        cleaned = _unique_strings(
            value,
            label="used_source_ref_ids",
            max_item_length=35,
        )
        if any(_SOURCE_REF_ID.fullmatch(item) is None for item in cleaned):
            raise ValueError("used_source_ref_ids contain an invalid ID")
        return cleaned

    @field_validator("limitations")
    @classmethod
    def validate_limitations(cls, value: list[str]) -> list[str]:
        return _unique_strings(
            value,
            label="limitations",
            max_item_length=255,
        )

    @model_validator(mode="after")
    def validate_draft(self) -> DecisionDraft:
        if self.status is DecisionStatus.COMPLETED and self.answer is None:
            raise ValueError("completed decisions require an answer")
        if self.status is DecisionStatus.NEEDS_CLARIFICATION:
            if self.clarification_question is None:
                raise ValueError(
                    "needs_clarification requires clarification_question"
                )
            if self.proposed_action:
                raise ValueError(
                    "clarification responses cannot propose an action"
                )
        if self.status in {
            DecisionStatus.BLOCKED,
            DecisionStatus.FAILED,
        } and self.proposed_action:
            raise ValueError("blocked or failed decisions cannot propose actions")
        if self.proposed_action and not self.used_source_ref_ids:
            raise ValueError(
                "proposed actions require at least one source reference"
            )
        return self


class DecisionResult(BaseModel):
    """Final result returned after source validation and persistence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: uuid.UUID
    turn_id: uuid.UUID
    status: DecisionStatus
    answer: str | None = Field(default=None, max_length=MAX_ANSWER_LENGTH)
    proposed_action: bool = False
    source_refs: list[SourceRef] = Field(
        default_factory=list,
        max_length=MAX_SOURCE_REFS,
    )
    limitations: list[str] = Field(
        default_factory=list,
        max_length=MAX_LIMITATIONS,
    )
    clarification_question: str | None = Field(
        default=None,
        max_length=2_000,
    )
    confidence: float | None = Field(default=None, ge=0, le=1)
    uncertainty: str | None = Field(default=None, max_length=2_000)
    follow_up_question: str | None = Field(default=None, max_length=2_000)
    persistence_status: PersistenceStatus = PersistenceStatus.NOT_REQUIRED
    decision_record_id: uuid.UUID | None = None
    runtime: RuntimeMetadata
    tool_trace: list[ToolCallRecord] = Field(
        default_factory=list,
        max_length=MAX_TOOL_TRACE,
    )

    @field_validator("answer")
    @classmethod
    def validate_answer(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_text(
            value,
            label="answer",
            max_length=MAX_ANSWER_LENGTH,
        )

    @field_validator(
        "clarification_question",
        "uncertainty",
        "follow_up_question",
    )
    @classmethod
    def validate_optional_text(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None
        return _bounded_text(
            value,
            label="decision result text",
            max_length=2_000,
        )

    @field_validator("limitations")
    @classmethod
    def validate_limitations(cls, value: list[str]) -> list[str]:
        return _unique_strings(
            value,
            label="limitations",
            max_item_length=255,
        )

    @model_validator(mode="after")
    def validate_final_result(self) -> DecisionResult:
        reference_ids = [item.reference_id for item in self.source_refs]
        if len(reference_ids) != len(set(reference_ids)):
            raise ValueError("source_refs must contain unique references")
        if self.status is DecisionStatus.COMPLETED and self.answer is None:
            raise ValueError("completed decisions require an answer")
        if self.status is DecisionStatus.NEEDS_CLARIFICATION:
            if self.clarification_question is None:
                raise ValueError(
                    "needs_clarification requires clarification_question"
                )
            if self.proposed_action:
                raise ValueError(
                    "clarification responses cannot propose an action"
                )
        if self.status in {
            DecisionStatus.BLOCKED,
            DecisionStatus.FAILED,
        } and self.proposed_action:
            raise ValueError("blocked or failed decisions cannot propose actions")
        if self.persistence_status is PersistenceStatus.PERSISTED:
            if self.decision_record_id is None:
                raise ValueError(
                    "persisted decisions require decision_record_id"
                )
        elif self.decision_record_id is not None:
            raise ValueError(
                "decision_record_id requires persistence_status=persisted"
            )
        if self.proposed_action:
            if not self.source_refs:
                raise ValueError(
                    "proposed actions require validated source_refs"
                )
            if self.persistence_status is not PersistenceStatus.PERSISTED:
                raise ValueError(
                    "proposed actions are completed only after persistence"
                )
        return self
