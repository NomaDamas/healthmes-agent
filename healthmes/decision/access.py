"""HealthMes-owned authorization and privacy boundary for context reads."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from threading import Lock
from typing import Any

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from healthmes.activity.privacy import collection_gate
from healthmes.activity.repository import (
    DELETION_PROVIDER,
    DELETION_TOMBSTONE_EVENT,
    RAW_EVENT_TYPES,
    event_bounds,
    get_control_payload,
)
from healthmes.calendars.base import HealthmesEventKind
from healthmes.decision.contracts import (
    ContextCoverage,
    ContextFreshness,
    ContextQuery,
    ContextResult,
    ContextStatus,
    CoverageStatus,
    DecisionRequest,
    ExecutionScope,
    FreshnessStatus,
    PrivacyLevel,
    RawSourceHandle,
    SourceRef,
)
from healthmes.decision.providers import (
    ContextCapability,
    ContextProviderRegistry,
    DisabledProviderError,
    UnknownCapabilityError,
    UnknownProviderError,
)
from healthmes.nutrition.intake_service import (
    DECISION_EVENT,
    DECISION_REQUEST_EVENT,
    INTERACTION_EVENT,
    OUTCOME_EVENT,
)
from healthmes.store import (
    CalendarEventMirror,
    StorageObject,
    WellnessEvent,
)
from healthmes.timezones import parse_timezone

_PRIVACY_RANK = {
    PrivacyLevel.AGGREGATE: 0,
    PrivacyLevel.IDENTITY: 1,
    PrivacyLevel.SCOPED_RAW: 2,
}
_IDENTITY_KEYS = frozenset(
    {
        "app",
        "app_id",
        "app_name",
        "application",
        "application_id",
        "bundle_id",
        "calendar_title",
        "event_title",
        "external_id",
        "package",
        "package_name",
        "process_name",
        "window_title",
        "url",
        "website",
    }
)
_RAW_KEYS = frozenset(
    {
        "audio",
        "audio_bytes",
        "image",
        "image_bytes",
        "media_bytes",
        "media_path",
        "pixels",
        "raw",
        "raw_bytes",
        "raw_payload",
        "screen_capture",
        "source_text",
        "transcript",
    }
)
_MAX_FUTURE_SKEW = timedelta(minutes=1)
_ACTIVITY_MAX_EVENT_DURATION = timedelta(days=1)
_WEARABLE_PROVENANCE_LOOKBACK_DAYS = 2
_WEARABLE_SLEEP_MIN_DURATION = timedelta(hours=22)
_WEARABLE_SLEEP_MAX_DURATION = timedelta(hours=26)
_OPEN_WEARABLES_DERIVERS = {
    "health_score": "open-wearables.daily-readiness.v1",
    "sleep_summary": "open-wearables.daily-readiness.v1",
    "workout": "open-wearables.daily-readiness.v1",
}
_REFERENCE_FREE_NO_DATA_STATUSES = frozenset(
    {
        "insufficient_data",
        "no_data",
        "not_found",
        "unavailable",
    }
)
_AUDIT_REASON_CODES = frozenset(
    {
        "activity_collection_disabled",
        "activity_collection_paused",
        "activity_permission_denied",
        "activity_permission_revoked",
        "activity_permission_unavailable",
        "caller_not_authenticated",
        "caller_not_policy_owner",
        "capability_privacy_level_unsupported",
        "context_stale",
        "coverage_unknown",
        "domain_consent_denied",
        "domain_privacy_consent_denied",
        "execution_scope_denied",
        "external_source_identity_mismatch",
        "external_source_provenance_denied",
        "external_source_retention_unverified",
        "freshness_as_of_in_future",
        "freshness_unknown",
        "future_context_unavailable",
        "future_range_trimmed",
        "privacy_fields_redacted",
        "privacy_implicit_escalation_denied",
        "provider_access_changed",
        "provider_capability_mismatch",
        "provider_disabled",
        "provider_fields_redacted",
        "provider_or_capability_unknown",
        "provider_reported_limitation",
        "provider_source_refs_discarded",
        "query_date_invalid",
        "query_date_range_mismatch",
        "query_date_too_far_in_future",
        "query_fields_unsupported",
        "query_granularity_unsupported",
        "query_limit_trimmed",
        "query_lookback_invalid",
        "query_lookback_trimmed",
        "query_parameters_unsupported",
        "query_range_does_not_cover_lookback",
        "query_range_exceeds_limit",
        "query_range_trimmed",
        "query_timezone_mismatch",
        "raw_source_unavailable",
        "raw_fields_require_controlled_resolver",
        "raw_source_not_selected",
        "reference_free_no_data_sanitized",
        "result_payload_exceeds_limit",
        "result_rows_exceed_limit",
        "result_source_refs_exceed_limit",
        "result_truncated",
        "scoped_raw_access_denied",
        "scoped_raw_selection_required",
        "source_consent_scope_denied",
        "source_ref_domain_mismatch",
        "source_ref_expired",
        "source_ref_identity_mismatch",
        "source_ref_observation_mismatch",
        "source_ref_observed_in_future",
        "source_ref_outside_query",
        "source_ref_record_missing",
        "source_ref_resource_mismatch",
        "source_ref_tombstoned",
        "source_refs_unavailable",
        "stable_provenance_incomplete",
        "stable_provenance_missing",
        "turn_context_byte_budget_exhausted",
        "turn_source_ref_budget_exhausted",
        "turn_tool_call_budget_exhausted",
    }
)
_PROVIDER_GENERIC_LIMITATION_CODES = frozenset(
    {
        "invalid_provider_query",
        "provider_contract_violation",
        "provider_execution_failed",
        "provenance_incomplete",
    }
)
_AUDIT_SAFE_REDACTED_FIELDS = frozenset(
    {
        *_IDENTITY_KEYS,
        *_RAW_KEYS,
        "analysis_provenance",
        "evidence",
        "evidence_text",
        "name",
        "note",
        "provider",
        "question",
        "recommendation",
        "source",
        "summary",
        "warnings",
    }
)


@dataclass(frozen=True, slots=True)
class _WellnessEventSnapshot:
    id: uuid.UUID
    event_type: str
    schema_version: int
    observed_at: datetime
    recorded_at: datetime
    timezone: str | None
    source_provider: str
    source_device: str | None
    source_record_id: str
    coverage: float | None
    sensitivity: str
    consent_scope: str
    expires_at: datetime | None
    payload: dict[str, Any]
    raw_object_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class _StorageObjectSnapshot:
    id: uuid.UUID
    content_type: str | None
    size_bytes: int
    sha256: str | None
    expires_at: datetime | None
    purged_at: datetime | None


@dataclass(frozen=True, slots=True)
class _CalendarEventSnapshot:
    id: uuid.UUID
    start_at: datetime
    end_at: datetime
    healthmes_kind: str | None
    is_all_day: bool


def _as_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )


def _identifier(value: str) -> str:
    cleaned = value.strip().casefold()
    if not cleaned or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789_.-"
        for character in cleaned
    ):
        raise ValueError("access identifiers contain invalid characters")
    return cleaned


def _wellness_event_snapshot_select():
    return select(
        WellnessEvent.id,
        WellnessEvent.event_type,
        WellnessEvent.schema_version,
        WellnessEvent.observed_at,
        WellnessEvent.recorded_at,
        WellnessEvent.timezone,
        WellnessEvent.source_provider,
        WellnessEvent.source_device,
        WellnessEvent.source_record_id,
        WellnessEvent.coverage,
        WellnessEvent.sensitivity,
        WellnessEvent.consent_scope,
        WellnessEvent.expires_at,
        WellnessEvent.payload,
        WellnessEvent.raw_object_id,
    )


def _wellness_event_snapshot(
    row: Mapping[str, Any],
) -> _WellnessEventSnapshot:
    return _WellnessEventSnapshot(
        id=row["id"],
        event_type=row["event_type"],
        schema_version=row["schema_version"],
        observed_at=row["observed_at"],
        recorded_at=row["recorded_at"],
        timezone=row["timezone"],
        source_provider=row["source_provider"],
        source_device=row["source_device"],
        source_record_id=row["source_record_id"],
        coverage=row["coverage"],
        sensitivity=row["sensitivity"],
        consent_scope=row["consent_scope"],
        expires_at=row["expires_at"],
        payload=dict(row["payload"] or {}),
        raw_object_id=row["raw_object_id"],
    )


def _fresh_wellness_event(
    session: Session,
    event_id: uuid.UUID,
) -> _WellnessEventSnapshot | None:
    row = session.execute(
        _wellness_event_snapshot_select().where(
            WellnessEvent.id == event_id
        )
    ).mappings().one_or_none()
    return _wellness_event_snapshot(row) if row is not None else None


def _fresh_storage_object(
    session: Session,
    object_id: uuid.UUID,
) -> _StorageObjectSnapshot | None:
    row = session.execute(
        select(
            StorageObject.id,
            StorageObject.content_type,
            StorageObject.size_bytes,
            StorageObject.sha256,
            StorageObject.expires_at,
            StorageObject.purged_at,
        ).where(StorageObject.id == object_id)
    ).mappings().one_or_none()
    if row is None:
        return None
    return _StorageObjectSnapshot(
        id=row["id"],
        content_type=row["content_type"],
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        expires_at=row["expires_at"],
        purged_at=row["purged_at"],
    )


def _fresh_calendar_event(
    session: Session,
    event_id: uuid.UUID,
) -> _CalendarEventSnapshot | None:
    row = session.execute(
        select(
            CalendarEventMirror.id,
            CalendarEventMirror.start_at,
            CalendarEventMirror.end_at,
            CalendarEventMirror.healthmes_kind,
            CalendarEventMirror.is_all_day,
        ).where(CalendarEventMirror.id == event_id)
    ).mappings().one_or_none()
    if row is None:
        return None
    return _CalendarEventSnapshot(
        id=row["id"],
        start_at=row["start_at"],
        end_at=row["end_at"],
        healthmes_kind=row["healthmes_kind"],
        is_all_day=row["is_all_day"],
    )


class AccessOutcome(StrEnum):
    ALLOWED = "allowed"
    PARTIAL = "partial"
    DENIED = "denied"


class DomainAccessGrant(BaseModel):
    """Explicit owner consent for one provider domain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    max_privacy_level: PrivacyLevel = PrivacyLevel.AGGREGATE
    execution_scopes: tuple[ExecutionScope, ...] = (
        ExecutionScope.LOCAL,
        ExecutionScope.HOSTED,
    )
    consent_scopes: tuple[str, ...] = ("personal",)
    allow_hosted_raw: bool = False

    @field_validator("domain")
    @classmethod
    def normalize_domain(cls, value: str) -> str:
        return _identifier(value)

    @field_validator("consent_scopes")
    @classmethod
    def normalize_consent_scopes(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        normalized = tuple(_identifier(item) for item in value)
        if not normalized:
            raise ValueError("at least one consent scope is required")
        if len(normalized) != len(set(normalized)):
            raise ValueError("consent scopes must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_execution_scopes(self) -> DomainAccessGrant:
        if not self.execution_scopes:
            raise ValueError("at least one execution scope is required")
        if len(self.execution_scopes) != len(set(self.execution_scopes)):
            raise ValueError("execution scopes must be unique")
        if (
            self.allow_hosted_raw
            and self.max_privacy_level is not PrivacyLevel.SCOPED_RAW
        ):
            raise ValueError(
                "allow_hosted_raw requires scoped_raw domain consent"
            )
        return self


class ContextAccessPolicy(BaseModel):
    """Single-owner access policy injected by the API/runtime boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_principal_id: str = Field(min_length=1, max_length=255)
    grants: tuple[DomainAccessGrant, ...]
    max_query_days: int = Field(default=90, ge=1, le=90)
    max_rows_per_query: int = Field(default=250, ge=1, le=1_000)
    max_payload_bytes_per_query: int = Field(
        default=256_000,
        ge=1_024,
        le=2_000_000,
    )
    max_source_refs_per_query: int = Field(default=200, ge=1, le=500)
    trim_overlong_queries: bool = True
    allow_external_provenance: bool = True

    @field_validator("owner_principal_id")
    @classmethod
    def strip_owner(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("owner principal must not be blank")
        return cleaned

    @model_validator(mode="after")
    def validate_grants(self) -> ContextAccessPolicy:
        domains = [grant.domain for grant in self.grants]
        if len(domains) != len(set(domains)):
            raise ValueError("domain grants must be unique")
        return self

    def grant(self, domain: str) -> DomainAccessGrant | None:
        normalized = domain.strip().casefold()
        return next(
            (grant for grant in self.grants if grant.domain == normalized),
            None,
        )

class AccessAuditEntry(BaseModel):
    """Non-sensitive trace of one gateway policy decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query_id: uuid.UUID
    provider_id: str
    capability: str
    outcome: AccessOutcome
    occurred_at: AwareDatetime
    reason_codes: tuple[str, ...] = ()
    redacted_paths: tuple[str, ...] = ()
    requested_privacy_level: PrivacyLevel
    effective_privacy_level: PrivacyLevel | None = None
    requested_start: AwareDatetime | None = None
    requested_end: AwareDatetime | None = None
    effective_start: AwareDatetime | None = None
    effective_end: AwareDatetime | None = None
    requested_limit: int
    effective_limit: int | None = None
    source_ref_ids: tuple[str, ...] = ()
    payload_bytes: int = Field(default=0, ge=0)

    @field_validator(
        "occurred_at",
        "requested_start",
        "requested_end",
        "effective_start",
        "effective_end",
        mode="after",
    )
    @classmethod
    def normalize_time(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        return _as_utc(value) if value is not None else None


class ContextAccessLayer:
    """Creates turn-scoped gateways; it never selects a provider."""

    def __init__(
        self,
        registry: ContextProviderRegistry,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.registry = registry
        self._clock = clock or (lambda: datetime.now(UTC))

    def start_turn(
        self,
        request: DecisionRequest,
        *,
        policy: ContextAccessPolicy,
    ) -> ContextAccessTurn:
        return ContextAccessTurn(
            layer=self,
            request=request,
            policy=policy,
        )


class ContextAccessTurn:
    """The only provider execution surface exposed to a decision runtime."""

    def __init__(
        self,
        *,
        layer: ContextAccessLayer,
        request: DecisionRequest,
        policy: ContextAccessPolicy,
    ) -> None:
        self._layer = layer
        self.request = request
        self.policy = policy
        self._trace: list[AccessAuditEntry] = []
        self._calls = 0
        self._context_bytes = 0
        self._source_refs = 0
        self._budget_lock = Lock()

    @property
    def trace(self) -> tuple[AccessAuditEntry, ...]:
        return tuple(self._trace)

    @property
    def calls_used(self) -> int:
        with self._budget_lock:
            return self._calls

    @property
    def context_bytes_used(self) -> int:
        with self._budget_lock:
            return self._context_bytes

    @property
    def source_refs_used(self) -> int:
        with self._budget_lock:
            return self._source_refs

    async def query(
        self,
        session: Session,
        query: ContextQuery,
    ) -> ContextResult:
        now = _as_utc(self._layer._clock())
        with self._budget_lock:
            self._calls += 1
            tool_budget_exhausted = (
                self._calls > self.request.budget.max_tool_calls
            )
        if tool_budget_exhausted:
            return self._deny(
                query,
                now=now,
                reason_codes=("turn_tool_call_budget_exhausted",),
            )

        preflight = self._preflight(session, query, now=now)
        if isinstance(preflight, ContextResult):
            return preflight
        effective_query, capability, grant, pre_limitations = preflight

        try:
            result = await self._layer.registry.execute(
                session,
                effective_query,
                now=now,
            )
        except (
            DisabledProviderError,
            UnknownCapabilityError,
            UnknownProviderError,
        ):
            return self._deny(
                query,
                effective_query=effective_query,
                now=now,
                reason_codes=("provider_access_changed",),
            )
        postflight_now = max(
            now,
            _as_utc(self._layer._clock()),
        )
        try:
            postflight_descriptor, postflight_capability = (
                self._layer.registry.capability(
                    effective_query.capability
                )
            )
        except (
            DisabledProviderError,
            UnknownCapabilityError,
            UnknownProviderError,
        ):
            return self._deny(
                query,
                effective_query=effective_query,
                now=postflight_now,
                reason_codes=("provider_access_changed",),
            )
        if (
            postflight_descriptor.metadata.provider_id
            != effective_query.provider_id
            or postflight_capability != capability
        ):
            return self._deny(
                query,
                effective_query=effective_query,
                now=postflight_now,
                reason_codes=("provider_access_changed",),
            )
        now = postflight_now
        if grant.domain == "activity":
            query_bounds = _query_bounds(effective_query, now=now)
            bounds = (
                query_bounds
                if not isinstance(query_bounds, str)
                else (None, None)
            )
            blocked = _activity_collection_blockers(
                session,
                bounds=bounds,
                now=now,
            )
            if blocked:
                return self._deny(
                    query,
                    effective_query=effective_query,
                    now=now,
                    reason_codes=tuple(blocked),
                )
        provider_limitations = _provider_limitations(
            result.limitations,
            capability=capability,
        )
        if result.status in {
            ContextStatus.DENIED,
            ContextStatus.UNAVAILABLE,
            ContextStatus.FAILED,
        }:
            limitations = {
                *provider_limitations,
                *pre_limitations,
            }
            if result.source_refs:
                limitations.add("provider_source_refs_discarded")
            normalized = result.model_copy(
                update={
                    "source_refs": [],
                    "raw_sources": [],
                    "limitations": sorted(limitations),
                    "truncated": False,
                    "next_cursor": None,
                }
            )
            self._append_audit(
                query,
                effective_query=effective_query,
                now=now,
                outcome=(
                    AccessOutcome.DENIED
                    if result.status is ContextStatus.DENIED
                    else AccessOutcome.PARTIAL
                ),
                reason_codes=tuple(limitations),
                result=normalized,
            )
            return normalized

        no_data_payload = _reference_free_no_data_payload(result)
        no_data_sanitized = (
            no_data_payload is not None
            and result.payload != no_data_payload
        )
        if no_data_payload is not None:
            result = result.model_copy(
                update={"payload": no_data_payload}
            )

        freshness = result.freshness
        if (
            freshness.as_of is not None
            and freshness.as_of > now + _MAX_FUTURE_SKEW
        ):
            return self._deny(
                query,
                effective_query=effective_query,
                now=now,
                reason_codes=("freshness_as_of_in_future",),
            )
        if freshness.as_of is not None:
            freshness = freshness.model_copy(
                update={
                    "age_seconds": max(
                        0,
                        int((now - freshness.as_of).total_seconds()),
                    )
                }
            )

        source_validation = self._validate_source_refs(
            session,
            result,
            effective_query,
            capability=capability,
            grant=grant,
            now=now,
        )
        if source_validation[2]:
            return self._deny(
                query,
                effective_query=effective_query,
                now=now,
                reason_codes=tuple(
                    sorted(
                        {
                            *pre_limitations,
                            *source_validation[1],
                        }
                    )
                ),
            )
        refs, source_limitations, _ = source_validation
        raw_sources: list[RawSourceHandle] = []
        if effective_query.privacy_level is PrivacyLevel.SCOPED_RAW:
            raw_sources = _raw_source_handles(
                session,
                refs,
                selected_record_ids=frozenset(
                    self.request.hints.related_record_ids.values()
                ),
                now=now,
            )
            if not raw_sources:
                return self._deny(
                    query,
                    effective_query=effective_query,
                    now=now,
                    reason_codes=("raw_source_unavailable",),
                )
        allowlisted_payload, contract_redactions = _allowlisted_payload(
            result.payload,
            capability=capability,
            query=effective_query,
        )
        redacted_payload, privacy_redactions = _redact_payload(
            allowlisted_payload,
            capability=capability,
            privacy_level=effective_query.privacy_level,
        )
        redacted_paths = tuple(
            sorted({*contract_redactions, *privacy_redactions})
        )
        limitations = {
            *pre_limitations,
            *provider_limitations,
            *source_limitations,
        }
        if no_data_sanitized:
            limitations.add("reference_free_no_data_sanitized")
        if contract_redactions:
            limitations.add("provider_fields_redacted")
        if privacy_redactions:
            limitations.add("privacy_fields_redacted")
            if effective_query.privacy_level is PrivacyLevel.SCOPED_RAW:
                limitations.add("raw_fields_require_controlled_resolver")
        status = result.status
        if result.truncated:
            limitations.add("result_truncated")
            if status is ContextStatus.OK:
                status = ContextStatus.PARTIAL
        if freshness.status is FreshnessStatus.STALE:
            limitations.add("context_stale")
            if status is ContextStatus.OK:
                status = ContextStatus.PARTIAL
        elif freshness.status in {
            FreshnessStatus.UNKNOWN,
            FreshnessStatus.UNAVAILABLE,
        }:
            limitations.add("freshness_unknown")
            if status is ContextStatus.OK:
                status = ContextStatus.PARTIAL
        if result.coverage.status in {
            CoverageStatus.UNKNOWN,
            CoverageStatus.UNAVAILABLE,
        }:
            limitations.add("coverage_unknown")
            if status is ContextStatus.OK:
                status = ContextStatus.PARTIAL
        if redacted_payload and not refs:
            limitations.add("source_refs_unavailable")
            if status is ContextStatus.OK:
                status = ContextStatus.PARTIAL
        if limitations and status is ContextStatus.OK:
            status = ContextStatus.PARTIAL

        normalized = ContextResult(
            query_id=result.query_id,
            provider_id=result.provider_id,
            capability=result.capability,
            status=status,
            payload=redacted_payload,
            source_refs=refs,
            raw_sources=raw_sources,
            freshness=freshness,
            coverage=result.coverage,
            limitations=sorted(limitations),
            truncated=result.truncated,
            next_cursor=result.next_cursor,
        )
        payload_bytes = len(
            json.dumps(
                normalized.model_dump(mode="json"),
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode()
        )
        per_query_byte_limit = min(
            self.policy.max_payload_bytes_per_query,
            self.request.budget.max_context_bytes,
        )
        per_query_ref_limit = min(
            self.policy.max_source_refs_per_query,
            self.request.budget.max_source_refs,
        )
        if (
            _max_declared_collection_length(
                redacted_payload,
                capability.limit_output_fields,
            )
            > effective_query.limit
        ):
            return self._deny(
                query,
                effective_query=effective_query,
                now=now,
                reason_codes=("result_rows_exceed_limit",),
            )
        if payload_bytes > per_query_byte_limit:
            return self._deny(
                query,
                effective_query=effective_query,
                now=now,
                reason_codes=("result_payload_exceeds_limit",),
            )
        if len(refs) > per_query_ref_limit:
            return self._deny(
                query,
                effective_query=effective_query,
                now=now,
                reason_codes=("result_source_refs_exceed_limit",),
            )
        budget_denial: tuple[str, ...] | None = None
        with self._budget_lock:
            if (
                self._context_bytes + payload_bytes
                > self.request.budget.max_context_bytes
            ):
                budget_denial = (
                    "turn_context_byte_budget_exhausted",
                )
            elif (
                self._source_refs + len(refs)
                > self.request.budget.max_source_refs
            ):
                budget_denial = (
                    "turn_source_ref_budget_exhausted",
                )
            else:
                self._context_bytes += payload_bytes
                self._source_refs += len(refs)
        if budget_denial is not None:
            return self._deny(
                query,
                effective_query=effective_query,
                now=now,
                reason_codes=budget_denial,
            )
        outcome = (
            AccessOutcome.ALLOWED
            if normalized.status is ContextStatus.OK
            and not redacted_paths
            and not pre_limitations
            else AccessOutcome.PARTIAL
        )
        self._append_audit(
            query,
            effective_query=effective_query,
            now=now,
            outcome=outcome,
            reason_codes=tuple(sorted(limitations)),
            redacted_paths=redacted_paths,
            result=normalized,
            payload_bytes=payload_bytes,
        )
        return normalized

    def _preflight(
        self,
        session: Session,
        query: ContextQuery,
        *,
        now: datetime,
    ) -> (
        tuple[
            ContextQuery,
            ContextCapability,
            DomainAccessGrant,
            tuple[str, ...],
        ]
        | ContextResult
    ):
        caller = self.request.caller
        if not caller.authenticated:
            return self._deny(
                query,
                now=now,
                reason_codes=("caller_not_authenticated",),
            )
        if caller.principal_id != self.policy.owner_principal_id:
            return self._deny(
                query,
                now=now,
                reason_codes=("caller_not_policy_owner",),
            )
        if query.timezone != self.request.timezone:
            return self._deny(
                query,
                now=now,
                reason_codes=("query_timezone_mismatch",),
            )
        try:
            descriptor, capability = self._layer.registry.capability(
                query.capability
            )
        except DisabledProviderError:
            return self._deny(
                query,
                now=now,
                reason_codes=("provider_disabled",),
            )
        except (UnknownProviderError, UnknownCapabilityError):
            return self._deny(
                query,
                now=now,
                reason_codes=("provider_or_capability_unknown",),
            )
        if descriptor.metadata.provider_id != query.provider_id:
            return self._deny(
                query,
                now=now,
                reason_codes=("provider_capability_mismatch",),
            )
        if query.granularity not in capability.granularities:
            return self._deny(
                query,
                now=now,
                reason_codes=("query_granularity_unsupported",),
            )
        if not set(query.fields).issubset(capability.output_fields):
            return self._deny(
                query,
                now=now,
                reason_codes=("query_fields_unsupported",),
            )
        if not set(query.parameters).issubset(capability.parameters):
            return self._deny(
                query,
                now=now,
                reason_codes=("query_parameters_unsupported",),
            )
        date_range_error = _explicit_date_range_error(query)
        if date_range_error is not None:
            return self._deny(
                query,
                now=now,
                reason_codes=(date_range_error,),
            )
        grant = self.policy.grant(descriptor.metadata.domain)
        if grant is None or not grant.enabled:
            return self._deny(
                query,
                now=now,
                reason_codes=("domain_consent_denied",),
            )
        if caller.execution_scope not in grant.execution_scopes:
            return self._deny(
                query,
                now=now,
                reason_codes=("execution_scope_denied",),
            )
        if (
            _PRIVACY_RANK[query.privacy_level]
            > _PRIVACY_RANK[self.request.requested_privacy_level]
        ):
            return self._deny(
                query,
                now=now,
                reason_codes=("privacy_implicit_escalation_denied",),
            )
        if (
            _PRIVACY_RANK[query.privacy_level]
            > _PRIVACY_RANK[grant.max_privacy_level]
        ):
            return self._deny(
                query,
                now=now,
                reason_codes=("domain_privacy_consent_denied",),
            )
        if query.privacy_level not in capability.privacy_levels:
            return self._deny(
                query,
                now=now,
                reason_codes=("capability_privacy_level_unsupported",),
            )
        if (
            query.privacy_level is PrivacyLevel.SCOPED_RAW
            and (
                not capability.supports_raw
                or query.purpose is None
                or not self.request.hints.related_record_ids
                or (
                    caller.execution_scope is ExecutionScope.HOSTED
                    and not grant.allow_hosted_raw
                )
            )
        ):
            return self._deny(
                query,
                now=now,
                reason_codes=(
                    (
                        "scoped_raw_selection_required"
                        if not self.request.hints.related_record_ids
                        else "scoped_raw_access_denied"
                    ),
                ),
            )

        limitations: list[str] = []
        effective = query
        effective_limit = min(query.limit, self.policy.max_rows_per_query)
        if effective_limit != query.limit:
            effective = effective.model_copy(
                update={"limit": effective_limit}
            )
            limitations.append("query_limit_trimmed")

        bounds = _query_bounds(effective, now=now)
        if isinstance(bounds, str):
            return self._deny(
                query,
                effective_query=effective,
                now=now,
                reason_codes=(bounds,),
            )
        expand_default_lookback = False
        if bounds == (None, None):
            hint_bounds = _request_hint_bounds(
                self.request,
                timezone=effective.timezone,
                now=now,
            )
            if isinstance(hint_bounds, str):
                return self._deny(
                    query,
                    effective_query=effective,
                    now=now,
                    reason_codes=(hint_bounds,),
                )
            bounds, expand_default_lookback = hint_bounds
            if (
                self.request.hints.start is None
                and self.request.hints.local_date is None
                and not capability.allows_future
            ):
                bounds = (
                    bounds[0],
                    min(bounds[1], now + timedelta(seconds=1)),
                )
        elif effective.start is None:
            expand_default_lookback = True
        start, end = bounds
        if start is not None and end is not None:
            max_days = min(
                capability.max_lookback_days,
                self.policy.max_query_days,
            )
            lookback = _capability_lookback_days(
                capability,
                effective,
            )
            if isinstance(lookback, str):
                return self._deny(
                    query,
                    effective_query=effective,
                    now=now,
                    reason_codes=(lookback,),
                )
            parameter_lookback, access_days = lookback
            if access_days > max_days:
                if not self.policy.trim_overlong_queries:
                    return self._deny(
                        query,
                        effective_query=effective,
                        now=now,
                        reason_codes=("query_range_exceeds_limit",),
                    )
                if capability.lookback_parameter is not None:
                    parameter_lookback = (
                        max_days
                        - capability.lookback_parameter_offset_days
                    )
                    if parameter_lookback < 1:
                        return self._deny(
                            query,
                            effective_query=effective,
                            now=now,
                            reason_codes=("query_range_exceeds_limit",),
                        )
                access_days = max_days
                limitations.append("query_lookback_trimmed")
            if capability.lookback_parameter is not None:
                parameters = dict(effective.parameters)
                parameters[capability.lookback_parameter] = (
                    parameter_lookback
                )
                effective = effective.model_copy(
                    update={"parameters": parameters}
                )
            if expand_default_lookback:
                start = _trim_start_for_calendar_days(
                    end,
                    timezone=effective.timezone,
                    max_days=access_days,
                )
            elif (
                capability.lookback_parameter is not None
                and _calendar_day_count(
                    start,
                    end,
                    timezone=effective.timezone,
                )
                < access_days
            ):
                return self._deny(
                    query,
                    effective_query=effective,
                    now=now,
                    reason_codes=(
                        "query_range_does_not_cover_lookback",
                    ),
                )
            if _calendar_day_count(
                start,
                end,
                timezone=effective.timezone,
            ) > max_days:
                if not self.policy.trim_overlong_queries:
                    return self._deny(
                        query,
                        effective_query=effective,
                        now=now,
                        reason_codes=("query_range_exceeds_limit",),
                    )
                start = _trim_start_for_calendar_days(
                    end,
                    timezone=effective.timezone,
                    max_days=max_days,
                )
                effective = effective.model_copy(
                    update={"start": start, "end": end}
                )
                limitations.append("query_range_trimmed")
            effective = effective.model_copy(
                update={"start": start, "end": end}
            )
            if not capability.allows_future and end > now + _MAX_FUTURE_SKEW:
                if start >= now + _MAX_FUTURE_SKEW:
                    return self._deny(
                        query,
                        effective_query=effective,
                        now=now,
                        reason_codes=("future_context_unavailable",),
                    )
                end = now + timedelta(seconds=1)
                effective = effective.model_copy(
                    update={"start": start, "end": end}
                )
                limitations.append("future_range_trimmed")

        if descriptor.metadata.domain == "activity":
            blocked = _activity_collection_blockers(
                session,
                bounds=(start, end),
                now=now,
            )
            if blocked:
                return self._deny(
                    query,
                    effective_query=effective,
                    now=now,
                    reason_codes=tuple(blocked),
                )
        return effective, capability, grant, tuple(limitations)

    def _validate_source_refs(
        self,
        session: Session,
        result: ContextResult,
        query: ContextQuery,
        *,
        capability: ContextCapability,
        grant: DomainAccessGrant,
        now: datetime,
    ) -> tuple[list[SourceRef], list[str], bool]:
        refs: list[SourceRef] = []
        limitations: set[str] = set()
        denied = False
        query_bounds = _query_bounds(query, now=now)
        bounds = (
            query_bounds
            if not isinstance(query_bounds, str)
            else (None, None)
        )
        related_nutrition_interactions = (
            _in_range_nutrition_interactions(
                session,
                result.source_refs,
                query_bounds=bounds,
            )
        )
        for source_ref in result.source_refs:
            if source_ref.domain != grant.domain:
                limitations.add("source_ref_domain_mismatch")
                denied = True
                continue
            validated = _validate_source_ref(
                session,
                source_ref,
                grant=grant,
                query_bounds=bounds,
                query_timezone=query.timezone,
                privacy_level=query.privacy_level,
                selected_record_ids=frozenset(
                    self.request.hints.related_record_ids.values()
                ),
                related_nutrition_interactions=(
                    related_nutrition_interactions
                ),
                now=now,
                allow_external=self.policy.allow_external_provenance,
            )
            if validated[0] is None:
                limitations.update(validated[1])
                denied = True
                continue
            refs.append(validated[0])
            limitations.update(validated[1])
        if result.payload and not result.source_refs:
            limitations.add("source_refs_unavailable")
            if query.privacy_level is PrivacyLevel.SCOPED_RAW:
                denied = True
        if (
            capability.provenance.value == "stable"
            and _requires_stable_provenance(result)
        ):
            if not result.source_refs:
                limitations.add("stable_provenance_missing")
                denied = True
            if "provenance_incomplete" in result.limitations:
                limitations.add("stable_provenance_incomplete")
                denied = True
        return refs, sorted(limitations), denied

    def _deny(
        self,
        query: ContextQuery,
        *,
        now: datetime,
        reason_codes: tuple[str, ...],
        effective_query: ContextQuery | None = None,
    ) -> ContextResult:
        result = ContextResult(
            query_id=query.query_id,
            provider_id=query.provider_id,
            capability=query.capability,
            status=ContextStatus.DENIED,
            freshness=ContextFreshness(
                status=FreshnessStatus.UNAVAILABLE
            ),
            coverage=ContextCoverage(
                status=CoverageStatus.UNAVAILABLE
            ),
            limitations=sorted(set(reason_codes)),
        )
        self._append_audit(
            query,
            effective_query=effective_query,
            now=now,
            outcome=AccessOutcome.DENIED,
            reason_codes=tuple(result.limitations),
            result=result,
        )
        return result

    def _append_audit(
        self,
        query: ContextQuery,
        *,
        now: datetime,
        outcome: AccessOutcome,
        reason_codes: tuple[str, ...],
        result: ContextResult,
        effective_query: ContextQuery | None = None,
        redacted_paths: tuple[str, ...] = (),
        payload_bytes: int = 0,
    ) -> None:
        effective = effective_query
        self._trace.append(
            AccessAuditEntry(
                query_id=query.query_id,
                provider_id=query.provider_id,
                capability=query.capability,
                outcome=outcome,
                occurred_at=now,
                reason_codes=_audit_reason_codes(reason_codes),
                redacted_paths=_audit_redacted_paths(redacted_paths),
                requested_privacy_level=query.privacy_level,
                effective_privacy_level=(
                    effective.privacy_level if effective else None
                ),
                requested_start=query.start,
                requested_end=query.end,
                effective_start=effective.start if effective else None,
                effective_end=effective.end if effective else None,
                requested_limit=query.limit,
                effective_limit=effective.limit if effective else None,
                source_ref_ids=tuple(
                    ref.reference_id for ref in result.source_refs
                ),
                payload_bytes=payload_bytes,
            )
        )


def _query_bounds(
    query: ContextQuery,
    *,
    now: datetime,
) -> tuple[datetime | None, datetime | None] | str:
    if query.start is not None and query.end is not None:
        return query.start, query.end
    raw_date = query.parameters.get("date")
    if not isinstance(raw_date, str):
        return None, None
    try:
        day = date.fromisoformat(raw_date)
        zone = parse_timezone(query.timezone)
    except ValueError:
        return "query_date_invalid"
    start = datetime.combine(
        day,
        datetime.min.time(),
        tzinfo=zone,
    ).astimezone(UTC)
    end = datetime.combine(
        day + timedelta(days=1),
        datetime.min.time(),
        tzinfo=zone,
    ).astimezone(UTC)
    if start > now + timedelta(days=366):
        return "query_date_too_far_in_future"
    return start, end


def _request_hint_bounds(
    request: DecisionRequest,
    *,
    timezone: str,
    now: datetime,
) -> tuple[tuple[datetime, datetime], bool] | str:
    hints = request.hints
    if hints.start is not None and hints.end is not None:
        return (hints.start, hints.end), False
    zone = parse_timezone(timezone)
    day = hints.local_date or now.astimezone(zone).date()
    start = datetime.combine(
        day,
        datetime.min.time(),
        tzinfo=zone,
    ).astimezone(UTC)
    end = datetime.combine(
        day + timedelta(days=1),
        datetime.min.time(),
        tzinfo=zone,
    ).astimezone(UTC)
    if start > now + timedelta(days=366):
        return "query_date_too_far_in_future"
    return (start, end), True


def _explicit_date_range_error(query: ContextQuery) -> str | None:
    raw_date = query.parameters.get("date")
    if raw_date is None or query.start is None or query.end is None:
        return None
    if not isinstance(raw_date, str):
        return "query_date_invalid"
    try:
        requested_day = date.fromisoformat(raw_date)
        zone = parse_timezone(query.timezone)
    except ValueError:
        return "query_date_invalid"
    anchor_day = (
        query.end - timedelta(microseconds=1)
    ).astimezone(zone).date()
    return (
        None
        if requested_day == anchor_day
        else "query_date_range_mismatch"
    )


def _capability_lookback_days(
    capability: ContextCapability,
    query: ContextQuery,
) -> tuple[int, int] | str:
    parameter = capability.lookback_parameter
    raw = (
        query.parameters.get(parameter, capability.default_lookback_days)
        if parameter is not None
        else capability.default_lookback_days
    )
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return "query_lookback_invalid"
    return raw, raw + capability.lookback_parameter_offset_days


def _requires_stable_provenance(result: ContextResult) -> bool:
    if not result.payload:
        return False
    return _reference_free_no_data_payload(result) is None


def _reference_free_no_data_payload(
    result: ContextResult,
) -> dict[str, Any] | None:
    if result.source_refs or result.raw_sources:
        return None
    if result.status is not ContextStatus.PARTIAL:
        return None
    raw_status = result.payload.get("status")
    if not isinstance(raw_status, str):
        return None
    status = raw_status.strip().casefold()
    if status not in _REFERENCE_FREE_NO_DATA_STATUSES:
        return None
    return {"status": status}


def _activity_collection_blockers(
    session: Session,
    *,
    bounds: tuple[datetime | None, datetime | None],
    now: datetime,
) -> tuple[str, ...]:
    start, end = bounds
    statement = _wellness_event_snapshot_select().where(
        WellnessEvent.source_device.is_not(None),
        WellnessEvent.event_type.in_(RAW_EVENT_TYPES),
        or_(
            WellnessEvent.expires_at.is_(None),
            WellnessEvent.expires_at > now,
        ),
    )
    if start is not None and end is not None:
        statement = statement.where(
            WellnessEvent.observed_at < end,
            WellnessEvent.observed_at
            >= start - _ACTIVITY_MAX_EVENT_DURATION,
        )
    device_ids: set[str] = set()
    for row in session.execute(statement).mappings():
        event = _wellness_event_snapshot(row)
        event_start, event_end = event_bounds(event)
        if (
            start is not None
            and end is not None
            and not (event_start < end and event_end > start)
        ):
            continue
        if event.source_device is not None:
            device_ids.add(str(event.source_device))
    blockers: set[str] = set()
    for device_id in sorted(device_ids):
        gate = collection_gate(
            get_control_payload(
                session,
                device_id,
                refresh=True,
            ),
            now=now,
        )
        if not gate.allowed:
            blockers.add(
                f"activity_{gate.reason or 'collection_blocked'}"
            )
    return tuple(sorted(blockers))


def _provider_limitations(
    values: Sequence[str],
    *,
    capability: ContextCapability,
) -> tuple[str, ...]:
    allowed = {
        *_PROVIDER_GENERIC_LIMITATION_CODES,
        *capability.limitation_codes,
    }
    return tuple(
        sorted(
            {
                value
                if value in allowed
                else "provider_reported_limitation"
                for value in values
            }
        )
    )


def _allowlisted_payload(
    payload: Mapping[str, Any],
    *,
    capability: ContextCapability,
    query: ContextQuery,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    allowed = set(query.fields or capability.output_fields)
    redacted = [
        str(key) for key in payload if str(key) not in allowed
    ]
    nested_allowed = {
        *capability.output_fields,
        *capability.nested_output_fields,
    }

    def clean(value: Any, path: str) -> Any:
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, item in value.items():
                normalized = str(key).strip().casefold()
                child_path = f"{path}.{key}" if path else str(key)
                if normalized not in nested_allowed:
                    redacted.append(child_path)
                    continue
                result[str(key)] = clean(item, child_path)
            return result
        if isinstance(value, Sequence) and not isinstance(
            value, str | bytes
        ):
            return [
                clean(item, f"{path}[{index}]")
                for index, item in enumerate(value)
            ]
        return value

    return (
        {
            str(key): clean(value, str(key))
            for key, value in payload.items()
            if str(key) in allowed
        },
        tuple(sorted(redacted)),
    )


def _redact_payload(
    payload: Mapping[str, Any],
    *,
    capability: ContextCapability,
    privacy_level: PrivacyLevel,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    redacted: list[str] = []
    identity_keys = {
        *_IDENTITY_KEYS,
        *capability.identity_fields,
    }
    raw_keys = {
        *_RAW_KEYS,
        *capability.raw_fields,
    }

    def walk(value: Any, path: str) -> Any:
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, item in value.items():
                normalized = str(key).strip().casefold()
                child_path = f"{path}.{key}" if path else str(key)
                if (
                    privacy_level is PrivacyLevel.AGGREGATE
                    and normalized in identity_keys
                ) or normalized in raw_keys:
                    redacted.append(child_path)
                    continue
                result[str(key)] = walk(item, child_path)
            return result
        if isinstance(value, Sequence) and not isinstance(
            value, str | bytes
        ):
            return [
                walk(item, f"{path}[{index}]")
                for index, item in enumerate(value)
            ]
        return value

    return walk(payload, ""), tuple(sorted(redacted))


def _max_declared_collection_length(
    payload: Mapping[str, Any],
    fields: Sequence[str],
) -> int:
    lengths = [
        len(value)
        for field in fields
        if isinstance(
            (value := payload.get(field)),
            Sequence,
        )
        and not isinstance(value, str | bytes)
    ]
    return max(lengths, default=0)


def _audit_reason_codes(values: Sequence[str]) -> tuple[str, ...]:
    normalized = {
        value
        if value in _AUDIT_REASON_CODES
        else "provider_reported_limitation"
        for value in values
    }
    return tuple(sorted(normalized))


def _audit_redacted_paths(values: Sequence[str]) -> tuple[str, ...]:
    safe: set[str] = set()
    for value in values:
        normalized = value.casefold()
        matched = next(
            (
                field
                for field in _AUDIT_SAFE_REDACTED_FIELDS
                if normalized == field
                or normalized.endswith(f".{field}")
                or f".{field}[" in normalized
            ),
            None,
        )
        safe.add(matched or "provider_field")
    return tuple(sorted(safe))


def _raw_source_handles(
    session: Session,
    source_refs: Sequence[SourceRef],
    *,
    selected_record_ids: frozenset[str],
    now: datetime,
) -> list[RawSourceHandle]:
    handles: list[RawSourceHandle] = []
    seen: set[uuid.UUID] = set()
    for source_ref in source_refs:
        try:
            event_id = uuid.UUID(source_ref.record_id)
        except ValueError:
            return []
        event = _fresh_wellness_event(session, event_id)
        if event is None or event.raw_object_id is None:
            return []
        selectable_ids = {
            str(event.id),
            event.source_record_id,
            str(event.raw_object_id),
            source_ref.reference_id,
        }
        if selectable_ids.isdisjoint(selected_record_ids):
            return []
        raw = _fresh_storage_object(session, event.raw_object_id)
        if (
            raw is None
            or raw.purged_at is not None
            or (
                raw.expires_at is not None
                and _as_utc(raw.expires_at) <= now
            )
        ):
            return []
        if raw.id in seen:
            continue
        seen.add(raw.id)
        handles.append(
            RawSourceHandle(
                source_ref_id=source_ref.reference_id,
                storage_object_id=raw.id,
                content_type=raw.content_type,
                size_bytes=raw.size_bytes,
                sha256=raw.sha256,
            )
        )
    return handles


def _validate_source_ref(
    session: Session,
    source_ref: SourceRef,
    *,
    grant: DomainAccessGrant,
    query_bounds: tuple[datetime | None, datetime | None],
    query_timezone: str,
    privacy_level: PrivacyLevel,
    selected_record_ids: frozenset[str],
    related_nutrition_interactions: frozenset[str],
    now: datetime,
    allow_external: bool,
) -> tuple[SourceRef | None, tuple[str, ...]]:
    try:
        record_uuid = uuid.UUID(source_ref.record_id)
    except ValueError:
        record_uuid = None
    event = (
        _fresh_wellness_event(session, record_uuid)
        if record_uuid is not None
        else None
    )
    if event is not None:
        return _validate_wellness_event_ref(
            session,
            source_ref,
            event,
            grant=grant,
            query_bounds=query_bounds,
            privacy_level=privacy_level,
            selected_record_ids=selected_record_ids,
            related_nutrition_interactions=(
                related_nutrition_interactions
            ),
            now=now,
        )

    if privacy_level is PrivacyLevel.SCOPED_RAW:
        return None, ("raw_source_unavailable",)

    if source_ref.source_provider == "healthmes-calendar-mirror":
        row = (
            _fresh_calendar_event(session, record_uuid)
            if record_uuid is not None
            else None
        )
        if row is None:
            return None, ("source_ref_record_missing",)
        if source_ref.resource_type not in {
            "calendar.event",
            "actual_sleep",
        }:
            return None, ("source_ref_resource_mismatch",)
        if source_ref.resource_type == "actual_sleep":
            if (
                row.is_all_day
                or row.healthmes_kind
                != HealthmesEventKind.ACTUAL_SLEEP.value
                or source_ref.derived_by
                != "healthmes.actual-sleep-mirror.v1"
                or source_ref.sensitivity != "wearable"
            ):
                return None, ("source_ref_identity_mismatch",)
        elif (
            row.is_all_day
            or row.healthmes_kind == HealthmesEventKind.ACTUAL_SLEEP.value
            or source_ref.derived_by != "calendar.context.v1"
            or source_ref.sensitivity != "calendar-metadata"
        ):
            return None, ("source_ref_identity_mismatch",)
        if (
            source_ref.schema_version != 1
            or _as_utc(row.start_at) != source_ref.observed_start
            or source_ref.observed_end is None
            or _as_utc(row.end_at) != source_ref.observed_end
            or source_ref.coverage is not None
        ):
            return None, ("source_ref_observation_mismatch",)
        if not _overlaps_query(source_ref, query_bounds):
            return None, ("source_ref_outside_query",)
        return source_ref, ()

    if (
        source_ref.domain == "wearable"
        and source_ref.source_provider == "open-wearables"
    ):
        if not allow_external:
            return None, ("external_source_provenance_denied",)
        expected_deriver = _OPEN_WEARABLES_DERIVERS.get(
            source_ref.resource_type
        )
        if (
            expected_deriver is None
            or source_ref.schema_version != 1
            or source_ref.derived_by != expected_deriver
            or source_ref.sensitivity != "wearable"
        ):
            return None, ("external_source_identity_mismatch",)
        if source_ref.resource_type == "sleep_summary":
            if (
                source_ref.observed_end is None
                or not (
                    _WEARABLE_SLEEP_MIN_DURATION
                    <= (
                        source_ref.observed_end
                        - source_ref.observed_start
                    )
                    <= _WEARABLE_SLEEP_MAX_DURATION
                )
            ):
                return None, ("external_source_identity_mismatch",)
        elif source_ref.observed_end is not None:
            return None, ("external_source_identity_mismatch",)
        if source_ref.observed_start > now + _MAX_FUTURE_SKEW:
            return None, ("source_ref_observed_in_future",)
        if not _wearable_ref_in_provenance_window(
            source_ref,
            query_bounds,
            timezone=query_timezone,
        ):
            return None, ("source_ref_outside_query",)
        return source_ref, ("external_source_retention_unverified",)

    return None, ("source_ref_record_missing",)


def _validate_wellness_event_ref(
    session: Session,
    source_ref: SourceRef,
    event: _WellnessEventSnapshot,
    *,
    grant: DomainAccessGrant,
    query_bounds: tuple[datetime | None, datetime | None],
    privacy_level: PrivacyLevel,
    selected_record_ids: frozenset[str],
    related_nutrition_interactions: frozenset[str],
    now: datetime,
) -> tuple[SourceRef | None, tuple[str, ...]]:
    if not event.event_type.startswith(f"{source_ref.domain}."):
        return None, ("source_ref_domain_mismatch",)
    expected_observed_end, valid_window = _wellness_event_observed_end(
        event
    )
    if (
        not valid_window
        or event.event_type != source_ref.resource_type
        or event.source_provider != source_ref.source_provider
        or event.schema_version != source_ref.schema_version
        or _as_utc(event.observed_at) != source_ref.observed_start
        or expected_observed_end != source_ref.observed_end
        or event.coverage != source_ref.coverage
        or event.sensitivity != source_ref.sensitivity
    ):
        return None, ("source_ref_identity_mismatch",)
    if event.consent_scope not in grant.consent_scopes:
        return None, ("source_consent_scope_denied",)
    if (
        event.expires_at is not None
        and _as_utc(event.expires_at) <= now
    ):
        return None, ("source_ref_expired",)
    if not _overlaps_query(
        source_ref,
        query_bounds,
    ) and not _related_nutrition_ref_is_in_scope(
        source_ref,
        event,
        related_nutrition_interactions=(
            related_nutrition_interactions
        ),
    ):
        return None, ("source_ref_outside_query",)
    if source_ref.domain == "activity" and _activity_ref_tombstoned(
        session,
        source_ref,
        event=event,
    ):
        return None, ("source_ref_tombstoned",)
    if privacy_level is PrivacyLevel.SCOPED_RAW:
        if event.raw_object_id is None:
            return None, ("raw_source_unavailable",)
        selectable_ids = {
            str(event.id),
            event.source_record_id,
            str(event.raw_object_id),
            source_ref.reference_id,
        }
        if selectable_ids.isdisjoint(selected_record_ids):
            return None, ("raw_source_not_selected",)
        raw = _fresh_storage_object(session, event.raw_object_id)
        if (
            raw is None
            or raw.purged_at is not None
            or (
                raw.expires_at is not None
                and _as_utc(raw.expires_at) <= now
            )
        ):
            return None, ("raw_source_unavailable",)
    return source_ref, ()


def _overlaps_query(
    source_ref: SourceRef,
    bounds: tuple[datetime | None, datetime | None],
) -> bool:
    start, end = bounds
    if start is None or end is None:
        return True
    observed_end = source_ref.observed_end or (
        source_ref.observed_start + timedelta(microseconds=1)
    )
    return source_ref.observed_start < end and observed_end > start


def _in_range_nutrition_interactions(
    session: Session,
    source_refs: Sequence[SourceRef],
    *,
    query_bounds: tuple[datetime | None, datetime | None],
) -> frozenset[str]:
    start, end = query_bounds
    if start is None or end is None:
        return frozenset()
    interactions: set[str] = set()
    for source_ref in source_refs:
        if (
            source_ref.domain != "nutrition"
            or source_ref.derived_by != "nutrition.intake-history.v1"
        ):
            continue
        try:
            event_id = uuid.UUID(source_ref.record_id)
        except ValueError:
            continue
        event = _fresh_wellness_event(session, event_id)
        if (
            event is None
            or event.event_type != source_ref.resource_type
        ):
            continue
        subject = _nutrition_subject(event)
        if subject is None:
            continue
        interaction_id, observed_at = subject
        if start <= observed_at < end:
            interactions.add(interaction_id)
    return frozenset(interactions)


def _nutrition_subject(
    event: _WellnessEventSnapshot,
) -> tuple[str, datetime] | None:
    payload: Mapping[str, Any] | None
    if event.event_type == INTERACTION_EVENT:
        payload = event.payload
    elif event.event_type == OUTCOME_EVENT:
        raw = event.payload.get("intake_snapshot")
        payload = raw if isinstance(raw, Mapping) else None
    elif event.event_type == DECISION_REQUEST_EVENT:
        snapshot = event.payload.get("context_snapshot")
        candidate = (
            snapshot.get("candidate")
            if isinstance(snapshot, Mapping)
            else None
        )
        payload = candidate if isinstance(candidate, Mapping) else None
    else:
        return None
    if payload is None:
        return None
    interaction_id = payload.get("interaction_id")
    observed_at = payload.get("observed_at")
    if not isinstance(interaction_id, str) or not isinstance(
        observed_at,
        str,
    ):
        return None
    try:
        normalized_id = str(uuid.UUID(interaction_id))
        parsed = datetime.fromisoformat(observed_at)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return normalized_id, parsed.astimezone(UTC)


def _related_nutrition_ref_is_in_scope(
    source_ref: SourceRef,
    event: _WellnessEventSnapshot,
    *,
    related_nutrition_interactions: frozenset[str],
) -> bool:
    if (
        source_ref.domain != "nutrition"
        or source_ref.derived_by != "nutrition.intake-history.v1"
        or event.event_type
        not in {
            OUTCOME_EVENT,
            DECISION_REQUEST_EVENT,
            DECISION_EVENT,
        }
    ):
        return False
    interaction_id = event.payload.get("interaction_id")
    if not isinstance(interaction_id, str):
        return False
    try:
        normalized_id = str(uuid.UUID(interaction_id))
    except ValueError:
        return False
    return normalized_id in related_nutrition_interactions


def _wearable_ref_in_provenance_window(
    source_ref: SourceRef,
    bounds: tuple[datetime | None, datetime | None],
    *,
    timezone: str,
) -> bool:
    start, end = bounds
    if start is None or end is None:
        return True
    zone = parse_timezone(timezone)
    first_allowed = (
        start.astimezone(zone).date()
        - timedelta(days=_WEARABLE_PROVENANCE_LOOKBACK_DAYS)
    )
    last_allowed = (
        end - timedelta(microseconds=1)
    ).astimezone(zone).date()
    observed_day = source_ref.observed_start.astimezone(zone).date()
    return first_allowed <= observed_day <= last_allowed


def _wellness_event_observed_end(
    event: _WellnessEventSnapshot,
) -> tuple[datetime | None, bool]:
    window = event.payload.get("window")
    if window is None:
        return None, True
    if not isinstance(window, Mapping):
        return None, False
    raw_end = window.get("end")
    if not isinstance(raw_end, str):
        return None, False
    try:
        observed_end = datetime.fromisoformat(raw_end)
    except ValueError:
        return None, False
    if observed_end.tzinfo is None:
        return None, False
    observed_end = observed_end.astimezone(UTC)
    if observed_end <= _as_utc(event.observed_at):
        return None, False
    return observed_end, True


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


def _trim_start_for_calendar_days(
    end: datetime,
    *,
    timezone: str,
    max_days: int,
) -> datetime:
    zone = parse_timezone(timezone)
    last = (end - timedelta(microseconds=1)).astimezone(zone).date()
    first = last - timedelta(days=max_days - 1)
    return datetime.combine(
        first,
        datetime.min.time(),
        tzinfo=zone,
    ).astimezone(UTC)


def _activity_ref_tombstoned(
    session: Session,
    source_ref: SourceRef,
    *,
    event: _WellnessEventSnapshot,
) -> bool:
    observed_end = source_ref.observed_end or (
        source_ref.observed_start + timedelta(microseconds=1)
    )
    statement = _wellness_event_snapshot_select().where(
        WellnessEvent.event_type == DELETION_TOMBSTONE_EVENT,
        WellnessEvent.source_provider == DELETION_PROVIDER,
        WellnessEvent.recorded_at >= event.recorded_at,
    )
    if event.source_device is not None:
        statement = statement.where(
            or_(
                WellnessEvent.source_device.is_(None),
                WellnessEvent.source_device == event.source_device,
            )
        )
    rows = session.execute(statement).mappings()
    for row in rows:
        tombstone = _wellness_event_snapshot(row)
        payload = tombstone.payload
        raw_end = payload.get("end")
        if not isinstance(raw_end, str):
            continue
        try:
            deleted_end = _as_utc(datetime.fromisoformat(raw_end))
        except ValueError:
            continue
        raw_start = payload.get("start")
        deleted_start: datetime | None = None
        if isinstance(raw_start, str):
            try:
                deleted_start = _as_utc(
                    datetime.fromisoformat(raw_start)
                )
            except ValueError:
                continue
        if deleted_end <= source_ref.observed_start:
            continue
        if deleted_start is not None and deleted_start >= observed_end:
            continue
        return True
    return False
