"""Adapters from HealthMes domain calculations to decision context providers."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from math import isfinite
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session, sessionmaker

from healthmes.activity.aggregation import local_day_bounds
from healthmes.activity.context import (
    activity_summary_context,
    focus_context,
    overwork_context,
    recovery_activity_context,
)
from healthmes.activity.contracts import ActivityPlatform
from healthmes.activity.repository import (
    APP_INTERVAL_EVENT,
    RAW_EVENT_TYPES,
    event_bounds,
    parse_optional_datetime,
)
from healthmes.activity.resolver import (
    _normalize_wearable_context,
)
from healthmes.calendars.base import HealthmesEventKind
from healthmes.calendars.repository import retained_calendar_event
from healthmes.calendars.state import (
    CalendarSyncHealth,
    SyncHealthStatus,
    SyncHealthStore,
)
from healthmes.calendars.visibility import (
    CalendarVisibility,
    CalendarVisibilityChanged,
    read_visible_calendar,
)
from healthmes.config import Settings
from healthmes.decision.contracts import (
    ContextCoverage,
    ContextFreshness,
    ContextQuery,
    ContextResult,
    ContextStatus,
    CoverageStatus,
    FreshnessStatus,
    PrivacyLevel,
    SourceRef,
)
from healthmes.decision.providers import (
    ContextCapability,
    ContextParameterFormat,
    ContextParameterSpec,
    ContextParameterType,
    ContextProviderMetadata,
    ProvenanceSupport,
    validate_context_parameters,
)
from healthmes.nutrition.intake_contracts import CaptureModality, IntakeIntent
from healthmes.nutrition.intake_query import (
    decision_context as nutrition_decision_context,
)
from healthmes.nutrition.intake_query import search_intake_history
from healthmes.nutrition.intake_service import (
    DECISION_EVENT,
    DECISION_REQUEST_EVENT,
    INTERACTION_EVENT,
    OUTCOME_EVENT,
)
from healthmes.nutrition.query import known_caffeine_for_day
from healthmes.nutrition.repository import (
    CONFIRMATION_EVENT,
    DAILY_CONFIRMATION_EVENT,
    OBSERVATION_EVENT,
    REVIEW_EVENT,
)
from healthmes.nutrition.repository import (
    SOURCE_PROVIDER as NUTRITION_OBSERVATION_PROVIDER,
)
from healthmes.storage import retention_cutoff
from healthmes.store import CalendarEventMirror, WellnessEvent
from healthmes.store.enums import CalendarSource
from healthmes.timezones import parse_timezone
from healthmes.wearables.provenance import (
    OPEN_WEARABLES_OBSERVATION_EVENT_TYPE,
    OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER,
    WearableSnapshot,
    latest_retained_open_wearables_snapshot,
    persist_open_wearables_observation,
)

WearableReader = Callable[[date], Awaitable[dict[str, Any]]]
CalendarSourceResolver = Callable[[], Sequence[CalendarSource]]
CalendarAccountGenerationResolver = Callable[
    [CalendarSource],
    str | None,
]

_DATE_PARAMETER = ContextParameterSpec(
    name="date",
    value_type=ContextParameterType.STRING,
    min_length=10,
    max_length=10,
    format=ContextParameterFormat.DATE,
)
_LOOKBACK_DAYS_PARAMETER = ContextParameterSpec(
    name="lookback_days",
    value_type=ContextParameterType.INTEGER,
    minimum=1,
    maximum=90,
)
_CONFIRMED_ONLY_PARAMETER = ContextParameterSpec(
    name="confirmed_only",
    value_type=ContextParameterType.BOOLEAN,
)
_INTENT_PARAMETER = ContextParameterSpec(
    name="intent",
    value_type=ContextParameterType.STRING,
    min_length=1,
    max_length=64,
    allowed_values=tuple(item.value for item in IntakeIntent),
)
_MODALITY_PARAMETER = ContextParameterSpec(
    name="modality",
    value_type=ContextParameterType.STRING,
    min_length=1,
    max_length=32,
    allowed_values=tuple(item.value for item in CaptureModality),
)
_NUTRIENT_PARAMETER = ContextParameterSpec(
    name="nutrient",
    value_type=ContextParameterType.STRING,
    min_length=1,
    max_length=128,
)
_QUERY_PARAMETER = ContextParameterSpec(
    name="query",
    value_type=ContextParameterType.STRING,
    min_length=1,
    max_length=500,
)
_REQUEST_ID_PARAMETER = ContextParameterSpec(
    name="request_id",
    value_type=ContextParameterType.STRING,
    required=True,
    min_length=36,
    max_length=36,
    format=ContextParameterFormat.UUID,
    accepts_related_record_ref=True,
)
_MINIMUM_MINUTES_PARAMETER = ContextParameterSpec(
    name="minimum_minutes",
    value_type=ContextParameterType.INTEGER,
    minimum=1,
    maximum=1_440,
)
_CURSOR_PATTERN = re.compile(r"^hmc1_[0-9a-f]{64}$")
_CURSOR_PARAMETER = ContextParameterSpec(
    name="cursor",
    value_type=ContextParameterType.STRING,
    min_length=69,
    max_length=69,
)
_DEVICE_ID_PARAMETER = ContextParameterSpec(
    name="device_id",
    value_type=ContextParameterType.STRING,
    min_length=1,
    max_length=255,
)
_PLATFORM_PARAMETER = ContextParameterSpec(
    name="platform",
    value_type=ContextParameterType.STRING,
    min_length=1,
    max_length=32,
    allowed_values=tuple(item.value for item in ActivityPlatform),
)
_WEARABLE_KIND_VALUES = (
    "load",
    "recovery",
    "sleep",
    "stress",
)
_WEARABLE_METRIC_VALUES = (
    "actual_sleep",
    "charge",
    "hrv",
    "sleep_debt",
    "stress",
    "yesterday_load",
)
_WEARABLE_KIND_PARAMETER = ContextParameterSpec(
    name="kind",
    value_type=ContextParameterType.STRING,
    min_length=1,
    max_length=16,
    allowed_values=_WEARABLE_KIND_VALUES,
)
_WEARABLE_METRIC_PARAMETER = ContextParameterSpec(
    name="metric",
    value_type=ContextParameterType.STRING,
    min_length=1,
    max_length=32,
    allowed_values=_WEARABLE_METRIC_VALUES,
)

CALENDAR_AGGREGATE_SOURCE_PROVIDER = "healthmes-calendar-aggregate"
CALENDAR_AGGREGATE_RESOURCE_TYPE = "calendar.aggregate"
CALENDAR_AGGREGATE_DERIVER = "calendar.aggregate.v1"

_ENVELOPE_KEYS = {
    "coverage",
    "evidence_ids",
    "freshness",
    "limitations",
    "source_refs",
}
_RAW_KEYS = {
    "audio_bytes",
    "image_bytes",
    "media_path",
    "raw_bytes",
    "source_text",
}
_ACTIVITY_NESTED_FIELDS = (
    "active_minutes",
    "active_minutes_upper",
    "active_time_range",
    "active_seconds",
    "app_id",
    "app_launches_or_switches",
    "app_launches_or_switches_range",
    "baseline_minutes",
    "capabilities",
    "category_attribution",
    "confidence",
    "conflict",
    "coverage",
    "device_id",
    "days_with_data",
    "deduplication",
    "delta_minutes",
    "delta_percent",
    "end",
    "expected_seconds",
    "hours_with_data",
    "idle_and_break_minutes",
    "kind",
    "launches_observed",
    "known_seconds",
    "launches_or_switches_per_active_hour",
    "late_activity_minutes",
    "late_activity_time_range",
    "launches",
    "longest_active_block_minutes",
    "longest_block",
    "lookback_baseline_delta",
    "lower_bound",
    "lower_bound_minutes",
    "metrics",
    "method",
    "precision",
    "records",
    "ratio",
    "required_days",
    "seven_day_baseline_delta",
    "signals",
    "start",
    "threshold_minutes",
    "threshold_uncertainties",
    "total_active_minutes",
    "timezone",
    "platform",
    "source_provider",
    "source_record_id",
    "state",
    "upper_bound",
    "upper_bound_minutes",
    "value_minutes",
    "window",
)
_NUTRITION_NESTED_FIELDS = (
    "amount",
    "analysis_provenance",
    "analyzed_at",
    "boundaries",
    "caffeine",
    "candidate",
    "category",
    "comparison_candidates",
    "complete",
    "confidence",
    "confirmed_at",
    "confirmed_caffeine_mg",
    "confirmed_intake_history",
    "consumed_at",
    "consumed_outcome_count",
    "coverage",
    "corrected_items",
    "candidate_is_not_consumed",
    "caffeine_total_intake_complete",
    "daily_confirmation_id",
    "decided_at",
    "decision_id",
    "delta",
    "end",
    "evidence",
    "evidence_text",
    "estimation_basis",
    "exact",
    "generic_caffeine_actionable_decisions_forbidden",
    "history_is_not_complete_day_proof",
    "history_window",
    "intake_type",
    "interaction_id",
    "intended_consumption_at",
    "intent",
    "intake_snapshot",
    "is_confirmed_intake",
    "items",
    "kind",
    "latest_decision",
    "latest_outcome",
    "ledger_entry_count",
    "limitations",
    "lookback_days",
    "local_date",
    "matching_records",
    "maximum",
    "medical_safety_requires_separate_policy",
    "media_path",
    "minimum",
    "modality",
    "model",
    "model_digest",
    "name",
    "note",
    "nutrient",
    "nutrients",
    "nutrition_observation_id",
    "nutrition_review_id",
    "observation_count",
    "observed_at",
    "operation_fingerprint",
    "origin",
    "outcome_id",
    "outcome_state_count",
    "prompt_version",
    "provider",
    "query",
    "question",
    "raw_capture_available",
    "recommendation",
    "recorded_at",
    "records",
    "request",
    "request_id",
    "requested_at",
    "reason",
    "resolved_items",
    "result_limit",
    "reviewed_count",
    "schema_version",
    "scope",
    "scanned_latest_outcomes",
    "scanned_records",
    "serving",
    "source",
    "source_text",
    "specialized_evidence",
    "start",
    "status",
    "summary",
    "timezone",
    "total_intake_complete",
    "transcription_model",
    "transcription_provider",
    "truncated",
    "unit",
    "unquantified_observation_ids",
    "unquantified_outcome_ids",
    "unreviewed_observation_ids",
    "warnings",
)
_NUTRITION_IDENTITY_FIELDS = (
    "analysis_provenance",
    "daily_confirmation_id",
    "decision_id",
    "evidence",
    "evidence_text",
    "interaction_id",
    "media_path",
    "model",
    "model_digest",
    "name",
    "note",
    "nutrition_observation_id",
    "nutrition_review_id",
    "operation_fingerprint",
    "outcome_id",
    "provider",
    "question",
    "recommendation",
    "request_id",
    "source",
    "source_text",
    "summary",
    "transcription_model",
    "transcription_provider",
    "unquantified_observation_ids",
    "unquantified_outcome_ids",
    "unreviewed_observation_ids",
    "warnings",
)
_NUTRITION_RAW_FIELDS = tuple(sorted(_RAW_KEYS))
_WEARABLE_NESTED_FIELDS = (
    "actual_sleep",
    "baseline_median",
    "block",
    "category",
    "charge",
    "confidence",
    "coverage",
    "current",
    "date",
    "delta",
    "delta_pct",
    "duration_minutes",
    "earliest_available_work_time",
    "entries",
    "freshest_at",
    "freshness",
    "hrv",
    "index",
    "last_night",
    "local_date",
    "max_avg_heart_rate_bpm",
    "metric",
    "n_days",
    "nights_counted",
    "observed_at",
    "observed_on",
    "provider",
    "qualifier",
    "ratio",
    "reason",
    "recorded_at",
    "records",
    "scale",
    "score",
    "sleep_debt",
    "source",
    "stale_days",
    "start",
    "status",
    "stress",
    "time_in_bed_minutes",
    "total_blocks",
    "total_calories_kcal",
    "total_minutes",
    "types",
    "unit",
    "usable_blocks",
    "value",
    "variant",
    "wake_time",
    "window_days",
    "workouts",
    "yesterday_load",
    "z_score",
)
_WEARABLE_IDENTITY_FIELDS = ("provider", "source")
_WEARABLE_METRIC_KIND = {
    "actual_sleep": "sleep",
    "charge": "recovery",
    "hrv": "recovery",
    "sleep_debt": "sleep",
    "stress": "stress",
    "yesterday_load": "load",
}
_CALENDAR_NESTED_FIELDS = (
    "calendar_source",
    "end",
    "event_id",
    "event_type",
    "events",
    "has_attendees",
    "intervals",
    "is_agent_created",
    "is_recurring",
    "start",
    "timezone",
    "window",
    "windows",
)
_ACTIVITY_LIMITATION_CODES = (
    "active_idle_overlap_resolved_active_wins",
    "category_attribution_conflict_detected",
    "category_attribution_conflict_possible",
    "coverage_unknown",
    "cross_device_activity_time_bounded",
    "cross_device_category_time_bounded",
    "cross_device_category_totals_may_overlap",
    "cross_device_idle_time_unresolved",
    "exact_focus_blocks_unavailable_for_hourly_sources",
    "focus_thresholds_blocked_by_cross_device_overlap",
    "focus_thresholds_blocked_by_partial_hour_uncertainty",
    "hourly_aggregate_cannot_reconstruct_exact_focus_blocks",
    "interval_source_takes_precedence_over_hourly_for_device",
    "legacy_activity_summary_incompatible",
    "launches_unavailable_for_some_sources",
    "low_source_coverage",
    "missing_is_not_zero",
    "overwork_thresholds_blocked_by_cross_device_overlap",
    "partial_hour_requires_retained_raw_events",
    "partial_hourly_activity_time_bounded",
    "partial_hourly_category_totals_bounded",
    "partial_hourly_launches_bounded",
    "provisional_hourly_activity",
    "source_coverage_unknown_for_some_devices",
    "source_reported_seconds_exceeded_bucket",
)
_NUTRITION_LIMITATION_CODES = (
    "caffeine_day_not_confirmed_complete",
    "nutrition_history_scan_limit_reached",
    "nutrition_decision_request_not_found",
)
_NUTRITION_EVENT_PROVIDERS = {
    INTERACTION_EVENT: frozenset({"nutrition-interaction"}),
    OUTCOME_EVENT: frozenset({"nutrition-intake-outcome"}),
    DECISION_REQUEST_EVENT: frozenset({"nutrition-decision-request"}),
    DECISION_EVENT: frozenset({"nutrition-decision"}),
    OBSERVATION_EVENT: frozenset({NUTRITION_OBSERVATION_PROVIDER}),
    REVIEW_EVENT: frozenset({"user-nutrition-review"}),
    CONFIRMATION_EVENT: frozenset({"user-confirmation"}),
    DAILY_CONFIRMATION_EVENT: frozenset({"user-confirmation"}),
}
_NUTRITION_HISTORY_SCAN_MULTIPLIER = 20
_NUTRITION_HISTORY_MIN_SCAN = 100
_NUTRITION_HISTORY_MAX_SCAN = 5_000
_WEARABLE_LIMITATION_CODES = (
    "open_wearables_context_unavailable",
    "wearable_readiness_evidence_ids_unavailable",
    "wearable_snapshot_fallback_used",
    "wearable_snapshot_persistence_failed",
    "wearable_snapshot_writer_unavailable",
    "wearable_source_refs_are_readiness_level",
)
_CALENDAR_LIMITATION_CODES = (
    "calendar_account_not_synced",
    "calendar_connection_state_unavailable",
    "calendar_never_synced",
    "calendar_not_connected",
    "calendar_mirror_completeness_unknown",
    "calendar_query_outside_sync_coverage",
    "calendar_recent_sync_failure",
    "calendar_sync_health_unavailable",
    "calendar_titles_omitted",
)


def _as_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )


def _canonical_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _json_value(value),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cursor_scope(
    query: ContextQuery,
    *,
    parameters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    selected_parameters = parameters or {
        key: value
        for key, value in query.parameters.items()
        if key != "cursor"
    }
    return {
        "provider_id": query.provider_id,
        "capability": query.capability,
        "start": query.start,
        "end": query.end,
        "timezone": query.timezone,
        "granularity": query.granularity,
        "privacy_level": query.privacy_level.value,
        "parameters": selected_parameters,
    }


def _opaque_cursor(
    namespace: str,
    *,
    scope: Mapping[str, Any],
    identity: Mapping[str, Any],
) -> str:
    return "hmc1_" + _canonical_digest(
        {
            "schema": "healthmes.domain-cursor.v1",
            "namespace": namespace,
            "scope": scope,
            "identity": identity,
        }
    )


def _page_items(
    items: Sequence[Any],
    *,
    query: ContextQuery,
    namespace: str,
    identity: Callable[[Any], Mapping[str, Any]],
) -> tuple[list[Any], str | None]:
    supplied = query.parameters.get("cursor")
    if supplied is not None and (
        not isinstance(supplied, str)
        or _CURSOR_PATTERN.fullmatch(supplied) is None
    ):
        raise ValueError("cursor is invalid")
    scope = _cursor_scope(query)
    offset = 0
    if supplied is not None:
        for index, item in enumerate(items):
            if (
                _opaque_cursor(
                    namespace,
                    scope=scope,
                    identity=identity(item),
                )
                == supplied
            ):
                offset = index + 1
                break
        else:
            raise ValueError("cursor is invalid for this result set")
    selected = list(items[offset : offset + query.limit])
    has_more = offset + len(selected) < len(items)
    next_cursor = (
        _opaque_cursor(
            namespace,
            scope=scope,
            identity=identity(selected[-1]),
        )
        if selected and has_more
        else None
    )
    return selected, next_cursor


def _calendar_row_digest_payload(
    row: CalendarEventMirror,
) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "calendar_source": row.calendar_source.value,
        "connection_generation": row.connection_generation,
        "external_id": row.external_id,
        "start_at": _as_utc(row.start_at),
        "end_at": _as_utc(row.end_at),
        "healthmes_kind": row.healthmes_kind,
        "is_all_day": row.is_all_day,
        "status": row.status,
        "updated_at": _as_utc(row.updated_at),
    }


def calendar_aggregate_identity(
    *,
    capability: str,
    start: datetime,
    end: datetime,
    timezone: str,
    granularity: str,
    parameters: Mapping[str, Any],
    source_scope: Mapping[str, str] | Sequence[str],
) -> str:
    """Return the opaque identity for one aggregate calendar query."""

    normalized_scope = canonical_calendar_source_scope(source_scope)
    return "aggregate:v1:" + _canonical_digest(
        {
            "schema": "healthmes.calendar-aggregate-identity.v1",
            "capability": capability,
            "start": _as_utc(start),
            "end": _as_utc(end),
            "timezone": timezone,
            "granularity": granularity,
            "parameters": {
                key: value
                for key, value in parameters.items()
                if key != "cursor"
            },
            "source_scope": normalized_scope,
        }
    )


def canonical_calendar_source_scope(
    source_scope: Mapping[str, str] | Sequence[str],
) -> dict[str, str] | tuple[str, ...]:
    """Normalize the source/generation scope used by aggregate provenance."""

    if isinstance(source_scope, Mapping):
        return {
            str(source): str(generation)
            for source, generation in sorted(
                source_scope.items(),
                key=lambda item: str(item[0]),
            )
        }
    return tuple(sorted({str(value) for value in source_scope}))


def calendar_aggregate_content_digest(
    rows: Sequence[CalendarEventMirror],
    *,
    capability: str,
    start: datetime,
    end: datetime,
    timezone: str,
    granularity: str,
    parameters: Mapping[str, Any],
    source_scope: Mapping[str, str] | Sequence[str],
) -> str:
    """Digest the exact retained rows used by a calendar aggregate."""

    record_id = calendar_aggregate_identity(
        capability=capability,
        start=start,
        end=end,
        timezone=timezone,
        granularity=granularity,
        parameters=parameters,
        source_scope=source_scope,
    )
    ordered_rows = sorted(
        rows,
        key=lambda row: (_as_utc(row.start_at), str(row.id)),
    )
    return _canonical_digest(
        {
            "schema": "healthmes.calendar-aggregate-content.v1",
            "record_id": record_id,
            "rows": [
                _calendar_row_digest_payload(row)
                for row in ordered_rows
            ],
        }
    )


def calendar_aggregate_source_ref(
    rows: Sequence[CalendarEventMirror],
    *,
    capability: str,
    start: datetime,
    end: datetime,
    timezone: str,
    granularity: str,
    parameters: Mapping[str, Any],
    source_scope: Mapping[str, str] | Sequence[str],
    collected_at: datetime | None,
    coverage: float | None,
) -> SourceRef:
    """Create one revalidatable ref without exposing per-event timestamps."""

    return SourceRef(
        domain="calendar",
        resource_type=CALENDAR_AGGREGATE_RESOURCE_TYPE,
        record_id=calendar_aggregate_identity(
            capability=capability,
            start=start,
            end=end,
            timezone=timezone,
            granularity=granularity,
            parameters=parameters,
            source_scope=source_scope,
        ),
        source_provider=CALENDAR_AGGREGATE_SOURCE_PROVIDER,
        observed_start=_as_utc(start),
        observed_end=_as_utc(end),
        collected_at=(
            _as_utc(collected_at)
            if collected_at is not None
            else None
        ),
        schema_version=1,
        derived_by=CALENDAR_AGGREGATE_DERIVER,
        freshness=FreshnessStatus.UNKNOWN,
        coverage=coverage,
        content_digest=calendar_aggregate_content_digest(
            rows,
            capability=capability,
            start=start,
            end=end,
            timezone=timezone,
            granularity=granularity,
            parameters=parameters,
            source_scope=source_scope,
        ),
        sensitivity="calendar-metadata",
    )


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


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        return value if isfinite(value) else None
    if isinstance(value, datetime):
        return _as_utc(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, uuid.UUID | Enum):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_json_value(item) for item in value]
    return str(value)


def _without_raw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _without_raw(item)
            for key, item in value.items()
            if str(key).casefold() not in _RAW_KEYS
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_without_raw(item) for item in value]
    return value


def _payload(
    raw: Mapping[str, Any],
    query: ContextQuery,
) -> dict[str, Any]:
    cleaned = {
        str(key): _json_value(value)
        for key, value in raw.items()
        if key not in _ENVELOPE_KEYS
    }
    cleaned = _without_raw(cleaned)
    if not query.fields:
        return cleaned
    return {
        key: cleaned[key]
        for key in query.fields
        if key in cleaned
    }


def _status(raw: Mapping[str, Any]) -> ContextStatus:
    value = str(raw.get("status") or "").casefold()
    if value in {"ok", "known"}:
        return ContextStatus.OK
    if value in {"partial", "insufficient_data", "incomplete"}:
        return ContextStatus.PARTIAL
    if value in {"unavailable", "not_configured"}:
        return ContextStatus.UNAVAILABLE
    if value in {"failed", "error"}:
        return ContextStatus.FAILED
    return ContextStatus.PARTIAL


def _timestamp(value: Any, *, timezone: str) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed_day = date.fromisoformat(value)
        except ValueError:
            return None
        return datetime.combine(
            parsed_day,
            datetime.min.time(),
            tzinfo=parse_timezone(timezone),
        ).astimezone(UTC)
    return _as_utc(parsed) if parsed.tzinfo is not None else None


def _freshness(
    raw: Mapping[str, Any],
    *,
    now: datetime,
    timezone: str,
) -> ContextFreshness:
    value = raw.get("freshness")
    if not isinstance(value, Mapping):
        return ContextFreshness(status=FreshnessStatus.UNKNOWN)
    as_of = _timestamp(
        value.get("recorded_at") or value.get("observed_on"),
        timezone=timezone,
    )
    raw_status = str(value.get("status") or "").casefold()
    if as_of is None:
        return ContextFreshness(
            status=(
                FreshnessStatus.UNAVAILABLE
                if raw_status == "unavailable"
                else FreshnessStatus.UNKNOWN
            )
        )
    age = max(0, int((now - as_of).total_seconds()))
    return ContextFreshness(
        status=(
            FreshnessStatus.STALE
            if "stale" in raw_status
            else FreshnessStatus.CURRENT
        ),
        as_of=as_of,
        age_seconds=age,
    )


def _coverage(raw: Mapping[str, Any]) -> ContextCoverage:
    value = (
        raw.get("coverage")
        if "coverage" in raw
        else raw.get("source_coverage")
    )
    ratio: float | None = None
    if isinstance(value, int | float) and not isinstance(value, bool):
        ratio = float(value)
    elif isinstance(value, Mapping):
        candidate = value.get("ratio")
        if isinstance(candidate, int | float) and not isinstance(
            candidate, bool
        ):
            ratio = float(candidate)
        elif value.get("complete") is True:
            ratio = 1.0
    if ratio is not None and isfinite(ratio) and 0 <= ratio <= 1:
        if ratio == 1:
            return ContextCoverage(
                status=CoverageStatus.COMPLETE,
                ratio=1,
            )
        return ContextCoverage(
            status=CoverageStatus.PARTIAL,
            ratio=ratio,
        )
    if str(raw.get("status") or "").casefold() in {
        "unavailable",
        "failed",
    }:
        return ContextCoverage(status=CoverageStatus.UNAVAILABLE)
    return ContextCoverage(status=CoverageStatus.UNKNOWN)


def _limitations(raw: Mapping[str, Any]) -> list[str]:
    values = raw.get("limitations")
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        return []
    return sorted(
        {
            str(value).strip()
            for value in values
            if str(value).strip()
        }
    )


def _event_observed_end(event: WellnessEvent) -> datetime | None:
    if event.event_type == APP_INTERVAL_EVENT:
        raw_end = event.payload.get("end_at")
        if raw_end is not None:
            end = parse_optional_datetime(raw_end)
            start = _as_utc(event.observed_at)
            return end if end is not None and end > start else None
    if (
        event.event_type in RAW_EVENT_TYPES
        and event.event_type != APP_INTERVAL_EVENT
    ):
        start, end = event_bounds(event)
        return end if end > start else None
    window = event.payload.get("window")
    if not isinstance(window, Mapping):
        return None
    end = window.get("end")
    if not isinstance(end, str):
        return None
    try:
        parsed = datetime.fromisoformat(end)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    parsed = parsed.astimezone(UTC)
    return parsed if parsed > _as_utc(event.observed_at) else None


def _event_source_refs(
    session: Session,
    event_ids: Sequence[Any],
    *,
    domain: str,
    derived_by: str,
    now: datetime,
) -> tuple[list[SourceRef], bool]:
    raw_ids = [str(value) for value in event_ids if value is not None]
    if not raw_ids:
        return [], True
    uuid_ids: list[uuid.UUID] = []
    for value in raw_ids:
        try:
            uuid_ids.append(uuid.UUID(value))
        except ValueError:
            continue
    if not uuid_ids:
        return [], False
    rows = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.id.in_(uuid_ids)
            )
        )
    )
    by_id = {str(row.id): row for row in rows}

    selected: list[WellnessEvent] = []
    seen: set[uuid.UUID] = set()
    resolved_identifiers: set[str] = set()
    for identifier in raw_ids:
        row = by_id.get(identifier)
        if row is not None and row.id not in seen:
            seen.add(row.id)
            selected.append(row)
        if row is not None:
            resolved_identifiers.add(identifier)
    refs = [
        SourceRef(
            domain=domain,
            resource_type=row.event_type,
            record_id=str(row.id),
            source_provider=row.source_provider,
            observed_start=_as_utc(row.observed_at),
            observed_end=_event_observed_end(row),
            collected_at=_as_utc(row.recorded_at),
            schema_version=row.schema_version,
            derived_by=derived_by,
            freshness=(
                FreshnessStatus.STALE
                if row.expires_at is not None
                and _as_utc(row.expires_at) <= now
                else FreshnessStatus.CURRENT
            ),
            coverage=row.coverage,
            sensitivity=row.sensitivity,
        )
        for row in selected
    ]
    return refs, len(resolved_identifiers) == len(set(raw_ids))


def _typed_nutrition_source_refs(
    session: Session,
    requirements: Sequence[tuple[Any, str, bool]],
    *,
    derived_by: str,
    now: datetime,
) -> tuple[list[SourceRef], bool]:
    normalized = [
        (str(identifier), event_type, is_event_id)
        for identifier, event_type, is_event_id in requirements
        if identifier is not None
    ]
    if not normalized:
        return [], True
    source_record_ids = [
        identifier
        for identifier, _event_type, is_event_id in normalized
        if not is_event_id
    ]
    uuid_ids: list[uuid.UUID] = []
    for identifier, _event_type, is_event_id in normalized:
        if not is_event_id:
            continue
        try:
            uuid_ids.append(uuid.UUID(identifier))
        except ValueError:
            continue
    conditions = []
    if source_record_ids:
        conditions.append(
            WellnessEvent.source_record_id.in_(source_record_ids)
        )
    if uuid_ids:
        conditions.append(WellnessEvent.id.in_(uuid_ids))
    if not conditions:
        return [], False
    rows = list(
        session.scalars(
            select(WellnessEvent).where(or_(*conditions))
        )
    )
    by_id = {str(row.id): row for row in rows}
    by_source_record: dict[str, list[WellnessEvent]] = {}
    for row in rows:
        by_source_record.setdefault(row.source_record_id, []).append(row)

    selected: list[WellnessEvent] = []
    seen: set[uuid.UUID] = set()
    resolved = 0
    for identifier, event_type, is_event_id in normalized:
        expected_providers = _NUTRITION_EVENT_PROVIDERS.get(event_type)
        if expected_providers is None:
            continue

        def matches(row: WellnessEvent) -> bool:
            return (
                row.event_type == event_type
                and row.source_provider in expected_providers
            )

        if is_event_id:
            exact = by_id.get(identifier)
            row = exact if exact is not None and matches(exact) else None
        else:
            candidates = [
                candidate
                for candidate in by_source_record.get(identifier, [])
                if matches(candidate)
            ]
            row = candidates[0] if len(candidates) == 1 else None
        if row is None:
            continue
        resolved += 1
        if row.id not in seen:
            seen.add(row.id)
            selected.append(row)

    refs = [
        SourceRef(
            domain="nutrition",
            resource_type=row.event_type,
            record_id=str(row.id),
            source_provider=row.source_provider,
            observed_start=_as_utc(row.observed_at),
            observed_end=_event_observed_end(row),
            collected_at=_as_utc(row.recorded_at),
            schema_version=row.schema_version,
            derived_by=derived_by,
            freshness=(
                FreshnessStatus.STALE
                if row.expires_at is not None
                and _as_utc(row.expires_at) <= now
                else FreshnessStatus.CURRENT
            ),
            coverage=row.coverage,
            sensitivity=row.sensitivity,
        )
        for row in selected
    ]
    return refs, resolved == len(normalized)


def _calendar_source_refs(
    rows: Sequence[CalendarEventMirror],
) -> list[SourceRef]:
    return [
        SourceRef(
            domain="calendar",
            resource_type="calendar.event",
            record_id=str(row.id),
            source_provider="healthmes-calendar-mirror",
            observed_start=_as_utc(row.start_at),
            observed_end=_as_utc(row.end_at),
            collected_at=_as_utc(row.updated_at),
            schema_version=1,
            derived_by="calendar.context.v1",
            freshness=FreshnessStatus.UNKNOWN,
            sensitivity="calendar-metadata",
        )
        for row in rows
    ]


def _wearable_source_refs(
    session: Session,
    raw: Mapping[str, Any],
    *,
    timezone: str,
) -> tuple[list[SourceRef], bool]:
    values = raw.get("source_refs")
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        return [], False
    refs: list[SourceRef] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        resource_type = str(value.get("resource_type") or "")
        source_provider = str(
            value.get("source_provider") or "open-wearables"
        )
        raw_observed = value.get("observed_at")
        if (
            source_provider == "healthmes-calendar-mirror"
            and resource_type == "actual_sleep"
        ):
            try:
                row_id = uuid.UUID(str(value.get("record_id") or ""))
            except ValueError:
                continue
            row = retained_calendar_event(session, row_id)
            supplied_observed = _timestamp(
                raw_observed,
                timezone=timezone,
            )
            if (
                row is None
                or row.healthmes_kind
                != HealthmesEventKind.ACTUAL_SLEEP.value
                or supplied_observed != _as_utc(row.end_at)
            ):
                continue
            observed = _as_utc(row.start_at)
            observed_end = _as_utc(row.end_at)
        elif resource_type == "sleep_summary" and isinstance(
            raw_observed,
            str,
        ):
            try:
                observed_day = date.fromisoformat(raw_observed)
            except ValueError:
                continue
            observed, observed_end = local_day_bounds(
                observed_day,
                timezone,
            )
        else:
            observed = _timestamp(raw_observed, timezone=timezone)
            observed_end = None
            if observed is None:
                continue
        try:
            refs.append(
                SourceRef(
                    domain="wearable",
                    resource_type=resource_type,
                    record_id=str(value.get("record_id") or ""),
                    source_provider=source_provider,
                    observed_start=observed,
                    observed_end=observed_end,
                    schema_version=int(value.get("schema_version") or 1),
                    derived_by=(
                        str(value["derived_by"])
                        if value.get("derived_by")
                        else None
                    ),
                    freshness=FreshnessStatus.UNKNOWN,
                    sensitivity="wearable",
                )
            )
        except (TypeError, ValueError):
            continue
    return refs, len(refs) == len(values)


def _result(
    query: ContextQuery,
    raw: Mapping[str, Any],
    *,
    refs: Sequence[SourceRef],
    refs_complete: bool,
    now: datetime,
    extra_limitations: Sequence[str] = (),
    truncated: bool = False,
    next_cursor: str | None = None,
    observed_start: datetime | None = None,
    observed_end: datetime | None = None,
    collected_at: datetime | None = None,
) -> ContextResult:
    status = _status(raw)
    limitations = {
        *_limitations(raw),
        *(value for value in extra_limitations if value),
    }
    if not refs_complete:
        limitations.add("provenance_incomplete")
    if status in {
        ContextStatus.UNAVAILABLE,
        ContextStatus.FAILED,
    }:
        payload = {}
        refs = ()
    else:
        payload = _payload(raw, query)
    ref_start, ref_end, ref_collected = _source_ref_times(refs)
    if refs:
        observed_start = ref_start
        observed_end = ref_end
        collected_at = ref_collected
    return ContextResult(
        query_id=query.query_id,
        provider_id=query.provider_id,
        capability=query.capability,
        status=status,
        payload=payload,
        source_refs=list(refs),
        observed_start=observed_start,
        observed_end=observed_end,
        collected_at=collected_at,
        freshness=_freshness(
            raw,
            now=now,
            timezone=query.timezone,
        ),
        coverage=_coverage(raw),
        limitations=sorted(limitations),
        truncated=truncated,
        next_cursor=next_cursor,
    )


def _source_ref_times(
    refs: Sequence[SourceRef],
) -> tuple[datetime | None, datetime | None, datetime | None]:
    if not refs:
        return None, None, None
    observed_start = min(ref.observed_start for ref in refs)
    observed_end = max(
        ref.observed_end or ref.observed_start for ref in refs
    )
    collected = [
        ref.collected_at for ref in refs if ref.collected_at is not None
    ]
    return (
        observed_start,
        observed_end,
        max(collected) if collected else None,
    )


def _query_day(
    query: ContextQuery,
    *,
    now: datetime,
) -> date:
    raw = query.parameters.get("date")
    if isinstance(raw, str):
        return date.fromisoformat(raw)
    zone = parse_timezone(query.timezone)
    if query.start is not None:
        return query.start.astimezone(zone).date()
    return now.astimezone(zone).date()


def _query_anchor_day(
    query: ContextQuery,
    *,
    now: datetime,
) -> date:
    raw = query.parameters.get("date")
    if isinstance(raw, str):
        return date.fromisoformat(raw)
    zone = parse_timezone(query.timezone)
    if query.end is not None:
        return (
            query.end - timedelta(microseconds=1)
        ).astimezone(zone).date()
    return now.astimezone(zone).date()


def _query_window(
    query: ContextQuery,
    *,
    now: datetime,
) -> tuple[date, datetime, datetime]:
    day = _query_day(query, now=now)
    day_start, day_end = local_day_bounds(day, query.timezone)
    start = query.start or day_start
    end = query.end or min(day_end, now + timedelta(seconds=1))
    if end <= start:
        raise ValueError("context window has not started")
    if (
        start.astimezone(parse_timezone(query.timezone)).date() != day
        or (end - timedelta(microseconds=1))
        .astimezone(parse_timezone(query.timezone))
        .date()
        != day
    ):
        raise ValueError("context query must stay within one local day")
    return day, start, end


def _validate_query(
    metadata: ContextProviderMetadata,
    query: ContextQuery,
) -> ContextCapability:
    capability = metadata.capability(query.capability)
    if capability is None:
        raise ValueError("unsupported provider capability")
    if query.granularity not in capability.granularities:
        raise ValueError("unsupported context granularity")
    if query.privacy_level not in capability.privacy_levels:
        raise ValueError("unsupported context privacy level")
    unsupported_fields = set(query.fields) - set(capability.output_fields)
    if unsupported_fields:
        raise ValueError("unsupported context output fields")
    unsupported_parameters = set(query.parameters) - set(
        capability.parameters
    )
    if unsupported_parameters:
        raise ValueError("unsupported context parameters")
    validate_context_parameters(
        query.parameters,
        capability.parameter_specs,
    )
    if capability.lookback_parameter is not None:
        raw_lookback = query.parameters.get(
            capability.lookback_parameter,
            capability.default_lookback_days,
        )
        if (
            isinstance(raw_lookback, bool)
            or not isinstance(raw_lookback, int)
            or not 1 <= raw_lookback <= capability.max_lookback_days
        ):
            raise ValueError("invalid context lookback")
        if (
            query.start is not None
            and query.end is not None
            and _calendar_day_count(
                query.start,
                query.end,
                timezone=query.timezone,
            )
            < (
                raw_lookback
                + capability.lookback_parameter_offset_days
            )
        ):
            raise ValueError("context range does not cover lookback")
    if (
        query.start is not None
        and query.end is not None
        and _calendar_day_count(
            query.start,
            query.end,
            timezone=query.timezone,
        )
        > capability.max_lookback_days
    ):
        raise ValueError("context query exceeds provider lookback")
    return capability


def _validate_nutrition_snapshot_window(
    raw: Mapping[str, Any],
    query: ContextQuery,
) -> None:
    if query.start is None or query.end is None:
        return
    history_window = raw.get("history_window")
    if not isinstance(history_window, Mapping):
        raise ValueError("nutrition snapshot history window is missing")
    start = _timestamp(
        history_window.get("start"),
        timezone=query.timezone,
    )
    end = _timestamp(
        history_window.get("end"),
        timezone=query.timezone,
    )
    if (
        start is None
        or end is None
        or start >= end
        or start < query.start
        or end > query.end
    ):
        raise ValueError("nutrition snapshot exceeds context range")


def _activity_row_window(
    row: WellnessEvent,
) -> tuple[datetime, datetime]:
    payload = row.payload
    start = _as_utc(row.observed_at)
    if payload.get("kind") == "app_hour":
        return start, start + timedelta(hours=1)
    raw_end = payload.get("end_at")
    parsed_end = (
        _timestamp(raw_end, timezone=row.timezone or "UTC")
        if isinstance(raw_end, str)
        else None
    )
    return start, parsed_end or (start + timedelta(microseconds=1))


def _activity_detail_rows(
    session: Session,
    *,
    start: datetime,
    end: datetime,
    now: datetime,
    device_id: str | None,
    platform: str | None,
) -> list[WellnessEvent]:
    statement = select(WellnessEvent).where(
        WellnessEvent.event_type.in_(RAW_EVENT_TYPES),
        WellnessEvent.observed_at < end,
        WellnessEvent.observed_at >= start - timedelta(days=1),
        or_(
            WellnessEvent.expires_at.is_(None),
            WellnessEvent.expires_at > now,
        ),
    )
    retained_after = retention_cutoff(
        session,
        "activity_raw",
        now=now,
    )
    if retained_after is not None:
        statement = statement.where(
            WellnessEvent.observed_at >= retained_after
        )
    if device_id is not None:
        statement = statement.where(
            WellnessEvent.source_device == device_id
        )
    rows = list(
        session.scalars(
            statement.order_by(
                WellnessEvent.observed_at,
                WellnessEvent.id,
            )
        )
    )
    selected: list[WellnessEvent] = []
    for row in rows:
        row_start, row_end = _activity_row_window(row)
        if row_start >= end or row_end <= start:
            continue
        if platform is not None and row.payload.get("platform") != platform:
            continue
        selected.append(row)
    return selected


def _activity_detail_record(
    row: WellnessEvent,
) -> dict[str, Any]:
    payload = row.payload
    start, end = _activity_row_window(row)
    common = {
        "record_id": str(row.id),
        "kind": str(payload.get("kind") or ""),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "platform": str(payload.get("platform") or "unknown"),
        "device_id": row.source_device,
        "source_provider": row.source_provider,
        "source_record_id": row.source_record_id,
        "app_id": payload.get("app_id"),
        "category": payload.get("category"),
        "launches": payload.get("launches"),
    }
    if payload.get("kind") == "app_hour":
        common.update(
            {
                "active_seconds": payload.get("foreground_seconds"),
                "launches_observed": payload.get(
                    "launches_observed",
                    True,
                ),
                "state": "active",
            }
        )
    else:
        common.update(
            {
                "active_seconds": round(
                    (end - start).total_seconds()
                    if payload.get("state") == "active"
                    else 0
                ),
                "launches_observed": True,
                "state": payload.get("state"),
            }
        )
    return common


class ActivityContextProvider:
    """Typed access to deterministic activity metrics and classifications."""

    metadata = ContextProviderMetadata(
        provider_id="activity",
        domain="activity",
        description=(
            "Deterministic device-activity summaries, focus fragmentation, "
            "overwork signals, and recovery-relevant activity metrics."
        ),
        capabilities=(
            ContextCapability(
                capability="activity.summary",
                description="One local day's aggregate activity summary.",
                granularities=("summary", "day"),
                query_fields=("start", "end", "timezone", "fields"),
                output_fields=(
                    "status",
                    "date",
                    "timezone",
                    "total_active_minutes",
                    "active_time_range",
                    "idle_and_break_minutes",
                    "late_activity_minutes",
                    "late_activity_time_range",
                    "longest_active_block_minutes",
                    "app_launches_or_switches",
                    "app_launches_or_switches_range",
                    "deduplication",
                    "category_attribution",
                    "source_coverage",
                    "reason",
                ),
                nested_output_fields=_ACTIVITY_NESTED_FIELDS,
                parameters=("date",),
                parameter_specs=(_DATE_PARAMETER,),
                max_lookback_days=1,
                sensitivity="activity-aggregate",
                limitation_codes=_ACTIVITY_LIMITATION_CODES,
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Final or provisional local-day summary.",
            ),
            ContextCapability(
                capability="activity.focus",
                description=(
                    "Focus fragmentation and sustained-block metrics for one "
                    "bounded local-day window."
                ),
                granularities=("summary", "window"),
                query_fields=("start", "end", "timezone", "fields"),
                output_fields=(
                    "status",
                    "window",
                    "classification",
                    "reason",
                    "metrics",
                    "boundary",
                ),
                nested_output_fields=_ACTIVITY_NESTED_FIELDS,
                parameters=("date",),
                parameter_specs=(_DATE_PARAMETER,),
                max_lookback_days=1,
                sensitivity="activity-aggregate",
                limitation_codes=_ACTIVITY_LIMITATION_CODES,
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Derived from retained raw or hourly summaries.",
            ),
            ContextCapability(
                capability="activity.overwork",
                description=(
                    "Daily workload, long-block, late-activity, and personal "
                    "baseline signals without making a wellness decision."
                ),
                granularities=("summary", "day"),
                query_fields=("start", "end", "timezone", "fields"),
                output_fields=(
                    "status",
                    "date",
                    "timezone",
                    "lookback_days",
                    "risk_level",
                    "reason",
                    "signals",
                    "threshold_uncertainties",
                    "metrics",
                    "boundary",
                ),
                nested_output_fields=_ACTIVITY_NESTED_FIELDS,
                parameters=("date", "lookback_days"),
                parameter_specs=(
                    _DATE_PARAMETER,
                    _LOOKBACK_DAYS_PARAMETER,
                ),
                max_lookback_days=90,
                default_lookback_days=7,
                lookback_parameter="lookback_days",
                lookback_parameter_offset_days=1,
                sensitivity="activity-aggregate",
                limitation_codes=_ACTIVITY_LIMITATION_CODES,
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Current local-day summary plus trailing baseline.",
            ),
            ContextCapability(
                capability="activity.recovery",
                description="Activity-only metrics relevant to recovery context.",
                granularities=("summary", "day"),
                query_fields=("start", "end", "timezone", "fields"),
                output_fields=(
                    "status",
                    "date",
                    "timezone",
                    "reason",
                    "metrics",
                    "boundary",
                ),
                nested_output_fields=_ACTIVITY_NESTED_FIELDS,
                parameters=("date",),
                parameter_specs=(_DATE_PARAMETER,),
                max_lookback_days=1,
                sensitivity="activity-aggregate",
                limitation_codes=_ACTIVITY_LIMITATION_CODES,
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="One local day's finalized or provisional activity.",
            ),
            ContextCapability(
                capability="activity.timeline",
                description=(
                    "Bounded retained activity records with optional device and "
                    "platform filters. App and device identity require an "
                    "explicit identity privacy request."
                ),
                granularities=("record", "window"),
                query_fields=(
                    "start",
                    "end",
                    "timezone",
                    "fields",
                    "limit",
                ),
                output_fields=("status", "count", "records"),
                nested_output_fields=_ACTIVITY_NESTED_FIELDS,
                identity_fields=(
                    "app_id",
                    "device_id",
                    "source_provider",
                    "source_record_id",
                ),
                limit_output_fields=("records",),
                parameters=("date", "cursor", "device_id", "platform"),
                parameter_specs=(
                    _DATE_PARAMETER,
                    _CURSOR_PARAMETER,
                    _DEVICE_ID_PARAMETER,
                    _PLATFORM_PARAMETER,
                ),
                max_lookback_days=31,
                default_lookback_days=1,
                privacy_levels=(
                    PrivacyLevel.AGGREGATE,
                    PrivacyLevel.IDENTITY,
                ),
                sensitivity="activity",
                limitation_codes=_ACTIVITY_LIMITATION_CODES,
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Retained raw activity rows at query time.",
            ),
        ),
    )

    async def query(
        self,
        session: Session,
        query: ContextQuery,
        *,
        now: datetime,
    ) -> ContextResult:
        _validate_query(self.metadata, query)
        if query.capability == "activity.timeline":
            if query.start is None or query.end is None:
                day = _query_day(query, now=now)
                start, end = local_day_bounds(day, query.timezone)
                end = min(end, now + timedelta(seconds=1))
            else:
                start, end = query.start, query.end
            rows = _activity_detail_rows(
                session,
                start=start,
                end=end,
                now=now,
                device_id=(
                    str(query.parameters["device_id"])
                    if query.parameters.get("device_id") is not None
                    else None
                ),
                platform=(
                    str(query.parameters["platform"])
                    if query.parameters.get("platform") is not None
                    else None
                ),
            )
            selected, next_cursor = _page_items(
                rows,
                query=query,
                namespace="activity.timeline",
                identity=lambda row: {
                    "observed_at": _as_utc(row.observed_at),
                    "id": str(row.id),
                },
            )
            refs, complete = _event_source_refs(
                session,
                [row.id for row in selected],
                domain="activity",
                derived_by="activity.timeline.v1",
                now=now,
            )
            latest = max(
                (_as_utc(row.recorded_at) for row in selected),
                default=None,
            )
            raw = {
                "status": "ok" if rows else "insufficient_data",
                "count": len(selected),
                "records": [
                    _activity_detail_record(row)
                    for row in selected
                ],
                "freshness": {
                    "recorded_at": (
                        latest.isoformat() if latest is not None else None
                    ),
                    "status": (
                        "stored_activity_records"
                        if latest is not None
                        else "unavailable"
                    ),
                },
                "coverage": {"ratio": 1.0 if rows else None},
            }
            return _result(
                query,
                raw,
                refs=refs,
                refs_complete=complete,
                now=now,
                truncated=next_cursor is not None,
                next_cursor=next_cursor,
                observed_start=start if not refs else None,
                observed_end=end if not refs else None,
                collected_at=latest if not refs else None,
            )
        if query.capability == "activity.overwork":
            day = _query_anchor_day(query, now=now)
            start, end = local_day_bounds(day, query.timezone)
            end = min(end, now + timedelta(seconds=1))
        else:
            day, start, end = _query_window(query, now=now)
        if query.capability == "activity.summary":
            raw = activity_summary_context(
                session,
                day=day,
                timezone=query.timezone,
                now=now,
            )
        elif query.capability == "activity.focus":
            raw = focus_context(
                session,
                start=start,
                end=end,
                timezone=query.timezone,
                now=now,
            )
        elif query.capability == "activity.overwork":
            lookback = query.parameters.get("lookback_days", 7)
            if isinstance(lookback, bool) or not isinstance(lookback, int):
                raise ValueError("lookback_days must be an integer")
            raw = overwork_context(
                session,
                day=day,
                timezone=query.timezone,
                lookback_days=lookback,
                now=now,
            )
        else:
            raw = recovery_activity_context(
                session,
                day=day,
                timezone=query.timezone,
                now=now,
            )
        refs, complete = _event_source_refs(
            session,
            list(raw.get("evidence_ids") or []),
            domain="activity",
            derived_by=f"{query.capability}.v1",
            now=now,
        )
        return _result(
            query,
            raw,
            refs=refs,
            refs_complete=complete,
            now=now,
        )


class NutritionContextProvider:
    """Typed reads over confirmed and candidate nutrition records."""

    metadata = ContextProviderMetadata(
        provider_id="nutrition",
        domain="nutrition",
        description=(
            "Confirmed intake history, caffeine ledgers, and structured "
            "candidate-food context from the unified wellness store."
        ),
        capabilities=(
            ContextCapability(
                capability="nutrition.intake-history",
                description=(
                    "Bounded captured intake history with optional confirmation, "
                    "nutrient, intent, modality, and text filters."
                ),
                granularities=("summary", "record"),
                query_fields=(
                    "start",
                    "end",
                    "timezone",
                    "fields",
                    "limit",
                ),
                output_fields=("status", "count", "records"),
                nested_output_fields=_NUTRITION_NESTED_FIELDS,
                identity_fields=_NUTRITION_IDENTITY_FIELDS,
                raw_fields=_NUTRITION_RAW_FIELDS,
                limit_output_fields=("records",),
                parameters=(
                    "confirmed_only",
                    "intent",
                    "modality",
                    "nutrient",
                    "query",
                ),
                parameter_specs=(
                    _CONFIRMED_ONLY_PARAMETER,
                    _INTENT_PARAMETER,
                    _MODALITY_PARAMETER,
                    _NUTRIENT_PARAMETER,
                    _QUERY_PARAMETER,
                ),
                max_lookback_days=90,
                default_lookback_days=14,
                privacy_levels=(
                    PrivacyLevel.AGGREGATE,
                    PrivacyLevel.IDENTITY,
                ),
                sensitivity="nutrition",
                limitation_codes=_NUTRITION_LIMITATION_CODES,
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Latest retained intake state at query time.",
            ),
            ContextCapability(
                capability="nutrition.caffeine-ledger",
                description=(
                    "Confirmed local-day caffeine total and completeness state; "
                    "missing confirmation is never treated as zero."
                ),
                granularities=("summary", "day"),
                query_fields=("start", "end", "timezone", "fields"),
                output_fields=(
                    "status",
                    "local_date",
                    "timezone",
                    "confirmed_caffeine_mg",
                    "total_intake_complete",
                    "observation_count",
                    "consumed_outcome_count",
                    "outcome_state_count",
                    "ledger_entry_count",
                    "reviewed_count",
                    "unreviewed_observation_ids",
                    "unquantified_observation_ids",
                    "unquantified_outcome_ids",
                    "daily_confirmation_id",
                ),
                nested_output_fields=_NUTRITION_NESTED_FIELDS,
                identity_fields=_NUTRITION_IDENTITY_FIELDS,
                parameters=("date",),
                parameter_specs=(_DATE_PARAMETER,),
                max_lookback_days=1,
                sensitivity="nutrition",
                limitation_codes=_NUTRITION_LIMITATION_CODES,
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Latest retained daily confirmation and intake outcomes.",
            ),
            ContextCapability(
                capability="nutrition.decision-context",
                description=(
                    "Structured candidate, comparisons, confirmed history, and "
                    "specialized evidence saved for an intake decision request."
                ),
                granularities=("summary", "record"),
                query_fields=(
                    "start",
                    "end",
                    "timezone",
                    "fields",
                ),
                output_fields=(
                    "status",
                    "request",
                    "candidate",
                    "comparison_candidates",
                    "confirmed_intake_history",
                    "history_window",
                    "specialized_evidence",
                    "boundaries",
                ),
                nested_output_fields=_NUTRITION_NESTED_FIELDS,
                identity_fields=_NUTRITION_IDENTITY_FIELDS,
                raw_fields=_NUTRITION_RAW_FIELDS,
                parameters=("request_id",),
                parameter_specs=(_REQUEST_ID_PARAMETER,),
                max_lookback_days=90,
                default_lookback_days=15,
                privacy_levels=(
                    PrivacyLevel.AGGREGATE,
                    PrivacyLevel.IDENTITY,
                ),
                sensitivity="nutrition",
                limitation_codes=_NUTRITION_LIMITATION_CODES,
                allows_future=True,
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Immutable request-time context snapshot.",
            ),
        ),
    )

    async def query(
        self,
        session: Session,
        query: ContextQuery,
        *,
        now: datetime,
    ) -> ContextResult:
        _validate_query(self.metadata, query)
        if query.capability == "nutrition.intake-history":
            intent = query.parameters.get("intent")
            modality = query.parameters.get("modality")
            max_scan_records = min(
                _NUTRITION_HISTORY_MAX_SCAN,
                max(
                    _NUTRITION_HISTORY_MIN_SCAN,
                    query.limit * _NUTRITION_HISTORY_SCAN_MULTIPLIER,
                ),
            )
            raw = search_intake_history(
                session,
                start=query.start,
                end=query.end,
                intent=IntakeIntent(str(intent)) if intent is not None else None,
                modality=(
                    CaptureModality(str(modality))
                    if modality is not None
                    else None
                ),
                confirmed_only=bool(
                    query.parameters.get("confirmed_only", False)
                ),
                nutrient=(
                    str(query.parameters["nutrient"])
                    if query.parameters.get("nutrient") is not None
                    else None
                ),
                query=(
                    str(query.parameters["query"])
                    if query.parameters.get("query") is not None
                    else None
                ),
                limit=query.limit,
                max_scan_records=max_scan_records,
                include_source_event_ids=True,
            )
            source_event_ids = raw.pop("source_event_ids", None)
            if source_event_ids is not None:
                refs, complete = _event_source_refs(
                    session,
                    (
                        source_event_ids
                        if isinstance(source_event_ids, Sequence)
                        and not isinstance(source_event_ids, str | bytes)
                        else ()
                    ),
                    domain="nutrition",
                    derived_by="nutrition.intake-history.v1",
                    now=now,
                )
                if not isinstance(source_event_ids, Sequence) or isinstance(
                    source_event_ids,
                    str | bytes,
                ):
                    complete = False
            else:
                requirements: list[tuple[str, str, bool]] = []
                for record in raw.get("records", []):
                    if not isinstance(record, Mapping):
                        continue
                    for key, event_type in (
                        ("interaction_id", INTERACTION_EVENT),
                        ("nutrition_observation_id", OBSERVATION_EVENT),
                        ("nutrition_review_id", REVIEW_EVENT),
                    ):
                        if record.get(key):
                            requirements.append(
                                (str(record[key]), event_type, False)
                            )
                    outcome = record.get("latest_outcome")
                    if (
                        isinstance(outcome, Mapping)
                        and outcome.get("outcome_id")
                    ):
                        requirements.append(
                            (
                                str(outcome["outcome_id"]),
                                OUTCOME_EVENT,
                                False,
                            )
                        )
                    decision = record.get("latest_decision")
                    if (
                        isinstance(decision, Mapping)
                        and decision.get("decision_id")
                    ):
                        requirements.append(
                            (
                                str(decision["decision_id"]),
                                DECISION_EVENT,
                                False,
                            )
                        )
                refs, complete = _typed_nutrition_source_refs(
                    session,
                    requirements,
                    derived_by="nutrition.intake-history.v1",
                    now=now,
                )
            recorded_times = [
                parsed
                for record in raw.get("records", [])
                if isinstance(record, Mapping)
                and (
                    parsed := _timestamp(
                        record.get("recorded_at"),
                        timezone=query.timezone,
                    )
                )
                is not None
            ]
            latest_recorded_at = max(recorded_times, default=None)
            raw["freshness"] = {
                "recorded_at": (
                    latest_recorded_at.isoformat()
                    if latest_recorded_at is not None
                    else None
                ),
                "status": (
                    "stored_intake_records"
                    if raw.get("records")
                    else "unavailable"
                ),
            }
            raw["coverage"] = raw.get("coverage", {})
            if not raw.get("records") and not raw.get("truncated"):
                raw["status"] = "insufficient_data"
            return _result(
                query,
                raw,
                refs=refs,
                refs_complete=complete,
                now=now,
                truncated=bool(raw.get("truncated")),
            )

        if query.capability == "nutrition.caffeine-ledger":
            day = _query_day(query, now=now)
            raw = known_caffeine_for_day(
                session,
                local_date=day,
                timezone=query.timezone,
            )
            requirements: list[tuple[str, str, bool]] = []
            for entry in raw.get("evidence", []):
                if not isinstance(entry, Mapping):
                    continue
                event_type = str(entry.get("event_type") or "")
                if entry.get("event_id"):
                    requirements.append(
                        (str(entry["event_id"]), event_type, True)
                    )
                for key, required_type in (
                    ("observation_id", OBSERVATION_EVENT),
                    ("nutrition_observation_id", OBSERVATION_EVENT),
                    ("nutrition_review_id", REVIEW_EVENT),
                ):
                    if entry.get(key):
                        requirements.append(
                            (str(entry[key]), required_type, False)
                        )
                if entry.get("confirmation_id"):
                    confirmation_type = (
                        REVIEW_EVENT
                        if event_type == REVIEW_EVENT
                        else CONFIRMATION_EVENT
                    )
                    requirements.append(
                        (
                            str(entry["confirmation_id"]),
                            confirmation_type,
                            False,
                        )
                    )
            if raw.get("daily_confirmation_id"):
                requirements.append(
                    (
                        str(raw["daily_confirmation_id"]),
                        DAILY_CONFIRMATION_EVENT,
                        False,
                    )
                )
            refs, complete = _typed_nutrition_source_refs(
                session,
                requirements,
                derived_by="nutrition.caffeine-ledger.v1",
                now=now,
            )
            latest = max(
                (
                    ref.observed_start
                    for ref in refs
                ),
                default=None,
            )
            raw["freshness"] = {
                "recorded_at": latest.isoformat() if latest else None,
                "status": "stored_nutrition_ledger" if latest else "unavailable",
            }
            raw["coverage"] = {
                "ratio": (
                    1.0
                    if raw.get("total_intake_complete") is True
                    else None
                )
            }
            raw["limitations"] = (
                []
                if raw.get("total_intake_complete") is True
                else ["caffeine_day_not_confirmed_complete"]
            )
            return _result(
                query,
                raw,
                refs=refs,
                refs_complete=complete,
                now=now,
            )

        request_id = query.parameters.get("request_id")
        if not isinstance(request_id, str):
            raise ValueError("nutrition decision context requires request_id")
        try:
            request_uuid = uuid.UUID(request_id)
        except ValueError as exc:
            raise ValueError("request_id must be a UUID") from exc
        raw = nutrition_decision_context(
            session,
            request_id=request_uuid,
        )
        if raw is None:
            return ContextResult(
                query_id=query.query_id,
                provider_id=query.provider_id,
                capability=query.capability,
                status=ContextStatus.UNAVAILABLE,
                freshness=ContextFreshness(
                    status=FreshnessStatus.UNAVAILABLE
                ),
                coverage=ContextCoverage(
                    status=CoverageStatus.UNAVAILABLE
                ),
                limitations=["nutrition_decision_request_not_found"],
            )
        _validate_nutrition_snapshot_window(raw, query)
        refs, complete = _event_source_refs(
            session,
            list(raw.get("evidence_event_ids") or []),
            domain="nutrition",
            derived_by="nutrition.decision-context.v1",
            now=now,
        )
        raw["freshness"] = {
            "recorded_at": raw.get("request", {}).get("requested_at"),
            "status": "stored_decision_context",
        }
        raw["coverage"] = {
            "ratio": (
                1.0
                if not raw.get("history_window", {})
                .get("query", {})
                .get("truncated")
                else None
            )
        }
        return _result(
            query,
            raw,
            refs=refs,
            refs_complete=complete,
            now=now,
        )


class WearableContextProvider:
    """Retained Open Wearables context backed by a local immutable snapshot."""

    metadata = ContextProviderMetadata(
        provider_id="wearable",
        domain="wearable",
        description=(
            "Normalized sleep, HRV, stress, charge, and training-load context "
            "mirrored into the HealthMes wellness store."
        ),
        capabilities=(
            ContextCapability(
                capability="wearable.readiness",
                description="Full normalized daily readiness context.",
                granularities=("summary", "day"),
                query_fields=("start", "end", "timezone", "fields"),
                output_fields=(
                    "status",
                    "reason",
                    "date",
                    "baseline_window_days",
                    "confidence",
                    "sleep_debt",
                    "actual_sleep",
                    "hrv",
                    "stress",
                    "charge",
                    "yesterday_load",
                ),
                nested_output_fields=_WEARABLE_NESTED_FIELDS,
                identity_fields=_WEARABLE_IDENTITY_FIELDS,
                parameters=("date",),
                parameter_specs=(_DATE_PARAMETER,),
                max_lookback_days=1,
                sensitivity="wearable",
                limitation_codes=_WEARABLE_LIMITATION_CODES,
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Daily Open Wearables snapshot.",
            ),
            ContextCapability(
                capability="wearable.sleep",
                description="Sleep debt, actual sleep, and nocturnal HRV blocks.",
                granularities=("summary", "day"),
                query_fields=("start", "end", "timezone", "fields"),
                output_fields=(
                    "status",
                    "reason",
                    "date",
                    "confidence",
                    "sleep_debt",
                    "actual_sleep",
                    "hrv",
                ),
                nested_output_fields=_WEARABLE_NESTED_FIELDS,
                identity_fields=_WEARABLE_IDENTITY_FIELDS,
                parameters=("date",),
                parameter_specs=(_DATE_PARAMETER,),
                max_lookback_days=1,
                sensitivity="wearable",
                limitation_codes=_WEARABLE_LIMITATION_CODES,
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Latest retained or upstream sleep observations.",
            ),
            ContextCapability(
                capability="wearable.recovery",
                description="HRV, charge, and prior-day training-load blocks.",
                granularities=("summary", "day"),
                query_fields=("start", "end", "timezone", "fields"),
                output_fields=(
                    "status",
                    "reason",
                    "date",
                    "confidence",
                    "hrv",
                    "charge",
                    "yesterday_load",
                ),
                nested_output_fields=_WEARABLE_NESTED_FIELDS,
                identity_fields=_WEARABLE_IDENTITY_FIELDS,
                parameters=("date",),
                parameter_specs=(_DATE_PARAMETER,),
                max_lookback_days=1,
                sensitivity="wearable",
                limitation_codes=_WEARABLE_LIMITATION_CODES,
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Daily Open Wearables readiness snapshot.",
            ),
            ContextCapability(
                capability="wearable.stress",
                description="Normalized wearable stress block.",
                granularities=("summary", "day"),
                query_fields=("start", "end", "timezone", "fields"),
                output_fields=(
                    "status",
                    "reason",
                    "date",
                    "confidence",
                    "stress",
                ),
                nested_output_fields=_WEARABLE_NESTED_FIELDS,
                identity_fields=_WEARABLE_IDENTITY_FIELDS,
                parameters=("date",),
                parameter_specs=(_DATE_PARAMETER,),
                max_lookback_days=1,
                sensitivity="wearable",
                limitation_codes=_WEARABLE_LIMITATION_CODES,
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Latest daily stress or resilience observation.",
            ),
            ContextCapability(
                capability="wearable.metric-detail",
                description=(
                    "Bounded allowlisted readiness metric blocks with optional "
                    "kind and metric filters. No raw wearable series is exposed."
                ),
                granularities=("record", "day"),
                query_fields=(
                    "start",
                    "end",
                    "timezone",
                    "fields",
                    "limit",
                ),
                output_fields=(
                    "status",
                    "date",
                    "count",
                    "records",
                ),
                nested_output_fields=_WEARABLE_NESTED_FIELDS,
                identity_fields=_WEARABLE_IDENTITY_FIELDS,
                limit_output_fields=("records",),
                parameters=("date", "cursor", "kind", "metric"),
                parameter_specs=(
                    _DATE_PARAMETER,
                    _CURSOR_PARAMETER,
                    _WEARABLE_KIND_PARAMETER,
                    _WEARABLE_METRIC_PARAMETER,
                ),
                max_lookback_days=1,
                privacy_levels=(
                    PrivacyLevel.AGGREGATE,
                    PrivacyLevel.IDENTITY,
                ),
                sensitivity="wearable",
                limitation_codes=_WEARABLE_LIMITATION_CODES,
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Latest retained daily wearable snapshot.",
            ),
        ),
    )

    def __init__(
        self,
        reader: WearableReader | None = None,
        *,
        snapshot_session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self._reader = reader
        # Kept for source compatibility. Provider writes use the caller-owned
        # unit of work so a second Session cannot commit unrelated SQLite data.
        self._snapshot_session_factory = snapshot_session_factory

    async def query(
        self,
        session: Session,
        query: ContextQuery,
        *,
        now: datetime,
    ) -> ContextResult:
        _validate_query(self.metadata, query)
        day = _query_day(query, now=now)
        raw, snapshot, limitations = await self._snapshot_context(
            session,
            day=day,
            timezone=query.timezone,
            now=now,
        )
        if raw is None or snapshot is None:
            return ContextResult(
                query_id=query.query_id,
                provider_id=query.provider_id,
                capability=query.capability,
                status=(
                    ContextStatus.FAILED
                    if "wearable_snapshot_persistence_failed"
                    in limitations
                    else ContextStatus.UNAVAILABLE
                ),
                freshness=ContextFreshness(
                    status=FreshnessStatus.UNAVAILABLE
                ),
                coverage=ContextCoverage(
                    status=CoverageStatus.UNAVAILABLE
                ),
                limitations=sorted(limitations),
            )
        if query.capability == "wearable.metric-detail":
            selected_kind = query.parameters.get("kind")
            selected_metric = query.parameters.get("metric")
            if (
                selected_metric is not None
                and selected_kind is not None
                and _WEARABLE_METRIC_KIND[str(selected_metric)]
                != selected_kind
            ):
                raise ValueError("wearable kind and metric filters conflict")
            metrics = [
                (
                    _WEARABLE_METRIC_KIND[metric],
                    metric,
                    raw[metric],
                )
                for metric in _WEARABLE_METRIC_VALUES
                if isinstance(raw.get(metric), Mapping)
                and (
                    selected_kind is None
                    or _WEARABLE_METRIC_KIND[metric] == selected_kind
                )
                and (
                    selected_metric is None
                    or metric == selected_metric
                )
            ]
            selected, next_cursor = _page_items(
                metrics,
                query=query,
                namespace="wearable.metric-detail",
                identity=lambda item: {
                    "snapshot_content_event_id": str(
                        snapshot.content_event_id
                    ),
                    "kind": item[0],
                    "metric": item[1],
                },
            )
            raw = {
                "status": "ok" if metrics else "insufficient_data",
                "date": day.isoformat(),
                "count": len(selected),
                "records": [
                    {
                        "kind": kind,
                        "metric": metric,
                        "block": block,
                    }
                    for kind, metric, block in selected
                ],
                "freshness": raw.get("freshness"),
                "coverage": raw.get("coverage"),
                "limitations": sorted(
                    {
                        *_limitations(raw),
                        *limitations,
                    }
                ),
            }
            freshness = _freshness(
                raw,
                now=now,
                timezone=query.timezone,
            )
            refs = [
                SourceRef(
                    domain="wearable",
                    resource_type=(
                        OPEN_WEARABLES_OBSERVATION_EVENT_TYPE
                    ),
                    record_id=str(snapshot.event_id),
                    source_provider=(
                        OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER
                    ),
                    observed_start=snapshot.observed_start,
                    observed_end=snapshot.observed_end,
                    collected_at=snapshot.collected_at,
                    schema_version=1,
                    derived_by="wearable.metric-detail.snapshot.v1",
                    freshness=freshness.status,
                    coverage=snapshot.coverage,
                    sensitivity="wearable",
                )
            ]
            return _result(
                query,
                raw,
                refs=refs,
                refs_complete=True,
                now=now,
                truncated=next_cursor is not None,
                next_cursor=next_cursor,
            )
        selected = {
            "wearable.sleep": {"sleep_debt", "actual_sleep", "hrv"},
            "wearable.recovery": {"hrv", "charge", "yesterday_load"},
            "wearable.stress": {"stress"},
        }.get(query.capability)
        if selected is not None:
            raw = {
                key: value
                for key, value in raw.items()
                if key in {
                    "status",
                    "reason",
                    "date",
                    "confidence",
                    "freshness",
                    "coverage",
                    "source_refs",
                    "evidence_ids",
                    "limitations",
                    *selected,
                }
            }
        raw["limitations"] = sorted(
            {
                *_limitations(raw),
                *limitations,
            }
        )
        freshness = _freshness(
            raw,
            now=now,
            timezone=query.timezone,
        )
        refs = [
            SourceRef(
                domain="wearable",
                resource_type=OPEN_WEARABLES_OBSERVATION_EVENT_TYPE,
                record_id=str(snapshot.event_id),
                source_provider=(
                    OPEN_WEARABLES_SNAPSHOT_SOURCE_PROVIDER
                ),
                observed_start=snapshot.observed_start,
                observed_end=snapshot.observed_end,
                collected_at=snapshot.collected_at,
                schema_version=1,
                derived_by=f"{query.capability}.snapshot.v1",
                freshness=freshness.status,
                coverage=snapshot.coverage,
                sensitivity="wearable",
            )
        ]
        return _result(
            query,
            raw,
            refs=refs,
            refs_complete=True,
            now=now,
            extra_limitations=(
                ("wearable_source_refs_are_readiness_level",)
                if selected is not None and refs
                else ()
            ),
        )

    async def _snapshot_context(
        self,
        session: Session,
        *,
        day: date,
        timezone: str,
        now: datetime,
    ) -> tuple[
        dict[str, Any] | None,
        WearableSnapshot | None,
        set[str],
    ]:
        limitations: set[str] = set()
        normalized: dict[str, Any] | None = None
        if self._reader is not None:
            try:
                normalized = _normalize_wearable_context(
                    await self._reader(day),
                    day=day,
                    now=now,
                    timezone=parse_timezone(timezone),
                )
            except Exception:
                limitations.add("open_wearables_context_unavailable")

        if (
            normalized is not None
            and _status(normalized)
            not in {ContextStatus.UNAVAILABLE, ContextStatus.FAILED}
        ):
            try:
                snapshot = persist_open_wearables_observation(
                    session,
                    normalized_context=normalized,
                    local_day=day,
                    timezone=timezone,
                    collected_at=now,
                    now=now,
                )
            except Exception:
                limitations.add(
                    "wearable_snapshot_persistence_failed"
                )
            else:
                return normalized, snapshot, limitations
        elif normalized is not None:
            limitations.add("open_wearables_context_unavailable")

        fallback = latest_retained_open_wearables_snapshot(
            session,
            local_day=day,
            timezone=timezone,
            now=now,
        )
        if fallback is not None:
            limitations.add("wearable_snapshot_fallback_used")
            if self._reader is None:
                limitations.add("open_wearables_context_unavailable")
            return (
                dict(fallback.normalized_context),
                fallback,
                limitations,
            )
        if self._reader is None:
            limitations.add("open_wearables_context_unavailable")
        return None, None, limitations


def _calendar_rows(
    session: Session,
    *,
    start: datetime,
    end: datetime,
    retained_after: datetime | None,
    sources: Sequence[CalendarSource] | None = None,
    account_generations: Mapping[CalendarSource, str] | None = None,
    lock_for_share: bool = False,
) -> list[CalendarEventMirror]:
    if lock_for_share and session.get_bind().dialect.name == "postgresql":
        # Row locks do not cover an empty result or gaps in a time range.
        # This transaction-scoped table lock prevents a concurrent insert,
        # update, or delete from changing the aggregate before finalization
        # commits its DecisionRecord.
        session.execute(
            text("LOCK TABLE calendar_event_mirror IN SHARE MODE")
        )
    statement = select(CalendarEventMirror).where(
        CalendarEventMirror.start_at < end,
        CalendarEventMirror.end_at > start,
        CalendarEventMirror.is_all_day.is_(False),
        or_(
            CalendarEventMirror.healthmes_kind.is_(None),
            CalendarEventMirror.healthmes_kind
            != HealthmesEventKind.ACTUAL_SLEEP.value,
        )
    )
    if retained_after is not None:
        statement = statement.where(
            CalendarEventMirror.end_at > retained_after
        )
    if account_generations is not None:
        account_filters = tuple(
            (
                CalendarEventMirror.calendar_source == source
            )
            & (
                CalendarEventMirror.connection_generation == generation
            )
            for source, generation in account_generations.items()
        )
        if not account_filters:
            return []
        statement = statement.where(or_(*account_filters))
    elif sources is not None:
        statement = statement.where(
            CalendarEventMirror.calendar_source.in_(sources)
        )
    statement = statement.order_by(
        CalendarEventMirror.start_at,
        CalendarEventMirror.id,
    )
    if lock_for_share and session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update(read=True)
    return list(session.scalars(statement))


def _merged_spans(
    rows: Sequence[CalendarEventMirror],
    *,
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, datetime]]:
    spans = sorted(
        (
            max(_as_utc(row.start_at), start),
            min(_as_utc(row.end_at), end),
        )
        for row in rows
        if min(_as_utc(row.end_at), end)
        > max(_as_utc(row.start_at), start)
    )
    merged: list[tuple[datetime, datetime]] = []
    for span_start, span_end in spans:
        if not merged or span_start > merged[-1][1]:
            merged.append((span_start, span_end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], span_end))
    return merged


def _calendar_sync_states(
    store: SyncHealthStore,
    sources: Sequence[CalendarSource],
) -> tuple[CalendarSyncHealth | None, ...] | None:
    try:
        return tuple(store.load(source) for source in sources)
    except Exception:
        return None


def _calendar_ready_account_generations(
    *,
    store: SyncHealthStore | None,
    sources: Sequence[CalendarSource],
    account_generations: Mapping[CalendarSource, str] | None,
) -> tuple[
    dict[CalendarSource, str] | None,
    tuple[CalendarSyncHealth | None, ...] | None,
    str | None,
]:
    """Resolve the exact account-generation scope safe for mirror reads."""

    states = (
        _calendar_sync_states(store, sources)
        if store is not None
        else None
    )
    if account_generations is None:
        return None, states, None
    if states is None:
        return None, None, "calendar_sync_health_unavailable"
    ready = {
        source: account_generations[source]
        for source, state in zip(sources, states, strict=True)
        if (
            state is not None
            and state.account_generation == account_generations[source]
            and state.last_success_at is not None
        )
    }
    if not ready:
        return {}, states, "calendar_account_not_synced"
    return ready, states, None


def _calendar_completeness(
    *,
    rows: Sequence[CalendarEventMirror],
    store: SyncHealthStore | None,
    sources: Sequence[CalendarSource],
    start: datetime,
    end: datetime,
    states: tuple[CalendarSyncHealth | None, ...] | None = None,
    account_generations: Mapping[CalendarSource, str] | None = None,
) -> tuple[
    str,
    datetime | None,
    dict[str, Any],
    set[str],
]:
    if store is None or not sources:
        freshness = max(
            (_as_utc(row.updated_at) for row in rows),
            default=None,
        )
        return (
            "ok" if rows else "insufficient_data",
            freshness,
            {"status": "unknown", "ratio": None},
            {"calendar_mirror_completeness_unknown"},
        )
    states = (
        states
        if states is not None
        else _calendar_sync_states(store, sources)
    )
    if states is None:
        return (
            "partial" if rows else "insufficient_data",
            max(
                (_as_utc(row.updated_at) for row in rows),
                default=None,
            ),
            {"status": "sync_health_unavailable", "ratio": None},
            {"calendar_sync_health_unavailable"},
        )
    current_states = tuple(
        state
        if (
            account_generations is None
            or (
                state is not None
                and state.account_generation
                == account_generations.get(source)
            )
        )
        else None
        for source, state in zip(sources, states, strict=True)
    )
    successful = tuple(
        state
        for state in current_states
        if state is not None
        and state.status
        in {SyncHealthStatus.SUCCESS, SyncHealthStatus.EMPTY_SUCCESS}
        and state.covers(start, end)
    )
    failures = tuple(
        state
        for state in current_states
        if state is not None
        and state.status is SyncHealthStatus.RECENT_FAILURE
    )
    oldest_success = min(
        (
            state.last_success_at
            for state in current_states
            if state is not None
            and state.last_success_at is not None
        ),
        default=None,
    )
    ratio = len(successful) / len(sources)
    limitations: set[str] = set()
    if failures:
        limitations.add("calendar_recent_sync_failure")
    if account_generations is not None and any(
        state is None
        for state in current_states
    ):
        limitations.add("calendar_account_not_synced")
    if any(
        state is None or state.status is SyncHealthStatus.NEVER_SYNCED
        for state in current_states
    ):
        limitations.add("calendar_never_synced")
    if any(
        state is not None
        and state.status
        in {SyncHealthStatus.SUCCESS, SyncHealthStatus.EMPTY_SUCCESS}
        and not state.covers(start, end)
        for state in current_states
    ):
        limitations.add("calendar_query_outside_sync_coverage")
    all_currently_successful = len(successful) == len(sources)
    any_prior_success = any(
        state is not None and state.last_success_at is not None
        for state in current_states
    )
    if rows:
        status = "ok" if all_currently_successful else "partial"
    else:
        status = (
            "empty_success"
            if all_currently_successful
            else "insufficient_data"
        )
    return (
        status,
        oldest_success,
        {
            "status": (
                "all_sources_synced"
                if all_currently_successful
                else "partial_source_sync"
                if successful
                else "unavailable"
            ),
            "ratio": ratio if successful or any_prior_success else None,
        },
        limitations,
    )


class CalendarContextProvider:
    """Privacy-minimized calendar workload and availability context."""

    metadata = ContextProviderMetadata(
        provider_id="calendar",
        domain="calendar",
        description=(
            "Mirrored calendar occupancy, meeting density, busy intervals, "
            "and available windows without event titles."
        ),
        capabilities=(
            ContextCapability(
                capability="calendar.day-summary",
                description="One local day's event count and occupied minutes.",
                granularities=("summary", "day"),
                query_fields=("start", "end", "timezone", "fields"),
                output_fields=(
                    "status",
                    "date",
                    "timezone",
                    "event_count",
                    "busy_minutes",
                    "first_event_at",
                    "last_event_at",
                    "meeting_density_per_hour",
                ),
                nested_output_fields=_CALENDAR_NESTED_FIELDS,
                parameters=("date",),
                parameter_specs=(_DATE_PARAMETER,),
                max_lookback_days=1,
                sensitivity="calendar-metadata",
                limitation_codes=_CALENDAR_LIMITATION_CODES,
                allows_future=True,
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Latest local calendar mirror state.",
            ),
            ContextCapability(
                capability="calendar.busy-intervals",
                description="Merged occupied intervals in a bounded window.",
                granularities=("summary", "window"),
                query_fields=(
                    "start",
                    "end",
                    "timezone",
                    "fields",
                    "limit",
                ),
                output_fields=(
                    "status",
                    "window",
                    "event_count",
                    "busy_minutes",
                    "intervals",
                ),
                nested_output_fields=_CALENDAR_NESTED_FIELDS,
                limit_output_fields=("intervals",),
                parameters=("date", "cursor"),
                parameter_specs=(
                    _DATE_PARAMETER,
                    _CURSOR_PARAMETER,
                ),
                max_lookback_days=31,
                sensitivity="calendar-metadata",
                limitation_codes=_CALENDAR_LIMITATION_CODES,
                allows_future=True,
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Latest local calendar mirror state.",
            ),
            ContextCapability(
                capability="calendar.available-windows",
                description="Unoccupied intervals in a bounded window.",
                granularities=("summary", "window"),
                query_fields=(
                    "start",
                    "end",
                    "timezone",
                    "fields",
                    "limit",
                ),
                output_fields=(
                    "status",
                    "window",
                    "available_minutes",
                    "windows",
                ),
                nested_output_fields=_CALENDAR_NESTED_FIELDS,
                limit_output_fields=("windows",),
                parameters=("date", "minimum_minutes", "cursor"),
                parameter_specs=(
                    _DATE_PARAMETER,
                    _MINIMUM_MINUTES_PARAMETER,
                    _CURSOR_PARAMETER,
                ),
                max_lookback_days=31,
                sensitivity="calendar-metadata",
                limitation_codes=_CALENDAR_LIMITATION_CODES,
                allows_future=True,
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Latest local calendar mirror state.",
            ),
            ContextCapability(
                capability="calendar.event-detail",
                description=(
                    "Bounded event identity and timing metadata without titles, "
                    "notes, attendee identities, or raw provider payloads."
                ),
                granularities=("record", "window"),
                query_fields=(
                    "start",
                    "end",
                    "timezone",
                    "fields",
                    "limit",
                ),
                output_fields=("status", "count", "events"),
                nested_output_fields=_CALENDAR_NESTED_FIELDS,
                identity_fields=(
                    "calendar_source",
                    "event_id",
                    "events",
                ),
                limit_output_fields=("events",),
                parameters=("date", "cursor"),
                parameter_specs=(
                    _DATE_PARAMETER,
                    _CURSOR_PARAMETER,
                ),
                max_lookback_days=31,
                privacy_levels=(PrivacyLevel.IDENTITY,),
                sensitivity="calendar-metadata",
                limitation_codes=_CALENDAR_LIMITATION_CODES,
                allows_future=True,
                provenance=ProvenanceSupport.STABLE,
                freshness_expectation="Latest local calendar mirror state.",
            ),
        ),
    )

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        sync_health_store: SyncHealthStore | None = None,
        sources: Sequence[CalendarSource] = (),
        source_resolver: CalendarSourceResolver | None = None,
        account_generation_resolver: (
            CalendarAccountGenerationResolver | None
        ) = None,
    ) -> None:
        if source_resolver is not None and not callable(source_resolver):
            raise TypeError("source_resolver must be callable")
        if source_resolver is not None and sources:
            raise ValueError(
                "sources and source_resolver are mutually exclusive"
            )
        self._sync_health_store = sync_health_store
        self._settings = settings
        self._sources = tuple(dict.fromkeys(sources))
        self._source_resolver = source_resolver
        if (
            account_generation_resolver is not None
            and not callable(account_generation_resolver)
        ):
            raise TypeError(
                "account_generation_resolver must be callable"
            )
        self._account_generation_resolver = (
            account_generation_resolver
        )

    def _read_visible_rows(
        self,
        session: Session,
        visibility: CalendarVisibility,
        *,
        start: datetime,
        end: datetime,
        now: datetime,
    ) -> tuple[
        list[CalendarEventMirror],
        tuple[CalendarSource, ...],
        tuple[CalendarSyncHealth | None, ...] | None,
    ]:
        sources = tuple(visibility.account_generations)
        states = (
            _calendar_sync_states(self._sync_health_store, sources)
            if self._sync_health_store is not None
            else None
        )
        return (
            _calendar_rows(
                session,
                start=start,
                end=end,
                retained_after=retention_cutoff(
                    session,
                    "calendar_mirror",
                    now=now,
                ),
                account_generations=visibility.account_generations,
            ),
            sources,
            states,
        )

    def _current_sources(
        self,
    ) -> tuple[tuple[CalendarSource, ...], str | None]:
        if self._source_resolver is None:
            return self._sources, None
        try:
            resolved = tuple(
                dict.fromkeys(
                    CalendarSource(source)
                    for source in self._source_resolver()
                )
            )
        except Exception:
            return (), "calendar_connection_state_unavailable"
        return resolved, None

    def _current_account_generations(
        self,
        sources: Sequence[CalendarSource],
    ) -> tuple[dict[CalendarSource, str] | None, str | None]:
        if self._account_generation_resolver is None:
            return None, None
        try:
            generations = {
                source: generation
                for source in sources
                if (
                    generation := self._account_generation_resolver(
                        source
                    )
                )
                is not None
            }
        except Exception:
            return None, "calendar_connection_state_unavailable"
        if len(generations) != len(sources):
            return None, "calendar_connection_state_unavailable"
        return generations, None

    async def query(
        self,
        session: Session,
        query: ContextQuery,
        *,
        now: datetime,
    ) -> ContextResult:
        _validate_query(self.metadata, query)
        day = _query_day(query, now=now)
        day_start, day_end = local_day_bounds(day, query.timezone)
        start = query.start or day_start
        end = query.end or day_end
        visibility_limitations: tuple[str, ...] = ()
        if self._settings is not None:
            try:
                (
                    (rows, sources, sync_states),
                    visibility,
                ) = read_visible_calendar(
                    session,
                    self._settings,
                    lambda snapshot: self._read_visible_rows(
                        session,
                        snapshot,
                        start=start,
                        end=end,
                        now=now,
                    ),
                    sync_health_store=self._sync_health_store,
                )
            except CalendarVisibilityChanged:
                return _result(
                    query,
                    {
                        "status": "unavailable",
                        "limitations": [
                            "calendar_connection_state_unavailable"
                        ],
                    },
                    refs=(),
                    refs_complete=True,
                    now=now,
                )
            account_generations = dict(
                visibility.account_generations
            )
            row_generations = account_generations
            visibility_limitations = visibility.limitations
            if not visibility.available:
                return _result(
                    query,
                    {
                        "status": "unavailable",
                        "limitations": list(
                            visibility.limitations
                            or ("calendar_not_connected",)
                        ),
                    },
                    refs=(),
                    refs_complete=True,
                    now=now,
                )
        else:
            sources, connection_error = self._current_sources()
            if connection_error is not None or not sources:
                return _result(
                    query,
                    {
                        "status": "unavailable",
                        "limitations": [
                            connection_error or "calendar_not_connected"
                        ],
                    },
                    refs=(),
                    refs_complete=True,
                    now=now,
                )
            account_generations, generation_error = (
                self._current_account_generations(sources)
            )
            if generation_error is not None:
                return _result(
                    query,
                    {
                        "status": "unavailable",
                        "limitations": [generation_error],
                    },
                    refs=(),
                    refs_complete=True,
                    now=now,
                )
            (
                ready_generations,
                sync_states,
                generation_readiness_error,
            ) = _calendar_ready_account_generations(
                store=self._sync_health_store,
                sources=sources,
                account_generations=account_generations,
            )
            if generation_readiness_error is not None:
                return _result(
                    query,
                    {
                        "status": "unavailable",
                        "limitations": [
                            generation_readiness_error
                        ],
                    },
                    refs=(),
                    refs_complete=True,
                    now=now,
                )
            row_generations = ready_generations
            rows = _calendar_rows(
                session,
                start=start,
                end=end,
                retained_after=retention_cutoff(
                    session,
                    "calendar_mirror",
                    now=now,
                ),
                sources=sources,
                account_generations=ready_generations,
            )
        status, freshness, coverage, completeness_limitations = (
            _calendar_completeness(
                rows=rows,
                store=self._sync_health_store,
                sources=sources,
                start=start,
                end=end,
                states=sync_states,
                account_generations=account_generations,
            )
        )
        if visibility_limitations:
            status = "partial" if rows else "insufficient_data"
        source_scope: Mapping[str, str] | Sequence[str] = (
            {
                source.value: generation
                for source, generation in row_generations.items()
            }
            if row_generations is not None
            else [source.value for source in sources]
        )
        if query.capability == "calendar.event-detail":
            selected, next_cursor = _page_items(
                rows,
                query=query,
                namespace="calendar.event-detail",
                identity=lambda row: {
                    "start_at": _as_utc(row.start_at),
                    "id": str(row.id),
                    "updated_at": _as_utc(row.updated_at),
                },
            )
            refs = _calendar_source_refs(selected)
            raw = {
                "status": status,
                "count": len(selected),
                "events": [
                    {
                        "event_id": str(row.id),
                        "calendar_source": row.calendar_source.value,
                        "start": _as_utc(row.start_at).isoformat(),
                        "end": _as_utc(row.end_at).isoformat(),
                        "has_attendees": row.has_attendees,
                        "is_recurring": row.is_recurring,
                        "is_agent_created": row.is_agent_created,
                        "event_type": row.event_type,
                    }
                    for row in selected
                ],
                "freshness": {
                    "recorded_at": (
                        freshness.isoformat() if freshness else None
                    ),
                    "status": (
                        "calendar_sync_success"
                        if freshness
                        else "unavailable"
                    ),
                },
                "coverage": coverage,
                "limitations": sorted(
                    {
                        "calendar_titles_omitted",
                        *completeness_limitations,
                        *visibility_limitations,
                    }
                ),
            }
            return _result(
                query,
                raw,
                refs=refs,
                refs_complete=True,
                now=now,
                truncated=next_cursor is not None,
                next_cursor=next_cursor,
                observed_start=start if not refs else None,
                observed_end=end if not refs else None,
                collected_at=freshness if not refs else None,
            )
        spans = _merged_spans(rows, start=start, end=end)
        if query.capability == "calendar.day-summary":
            busy_minutes = round(
                sum(
                    (span_end - span_start).total_seconds()
                    for span_start, span_end in spans
                )
                / 60,
                2,
            )
            raw = {
                "status": status,
                "date": day.isoformat(),
                "timezone": query.timezone,
                "event_count": len(rows),
                "busy_minutes": busy_minutes,
                "first_event_at": (
                    spans[0][0].isoformat() if spans else None
                ),
                "last_event_at": (
                    spans[-1][1].isoformat() if spans else None
                ),
            }
            hours = max(
                1 / 60,
                (end - start).total_seconds() / 3600,
            )
            raw["meeting_density_per_hour"] = round(
                len(rows) / hours,
                3,
            )
        else:
            if query.capability == "calendar.busy-intervals":
                selected, next_cursor = _page_items(
                    spans,
                    query=query,
                    namespace="calendar.busy-intervals",
                    identity=lambda span: {
                        "start": span[0],
                        "end": span[1],
                    },
                )
                raw = {
                    "status": status,
                    "window": {
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "timezone": query.timezone,
                    },
                    "event_count": len(rows),
                    "busy_minutes": round(
                        sum(
                            (span_end - span_start).total_seconds()
                            for span_start, span_end in spans
                        )
                        / 60,
                        2,
                    ),
                    "intervals": [
                        {
                            "start": span_start.isoformat(),
                            "end": span_end.isoformat(),
                        }
                        for span_start, span_end in selected
                    ],
                    "truncated": next_cursor is not None,
                }
            else:
                minimum = query.parameters.get("minimum_minutes", 15)
                if isinstance(minimum, bool) or not isinstance(
                    minimum, int | float
                ):
                    raise ValueError("minimum_minutes must be numeric")
                cursor = start
                available: list[tuple[datetime, datetime]] = []
                for span_start, span_end in spans:
                    if (
                        span_start - cursor
                    ).total_seconds() >= float(minimum) * 60:
                        available.append((cursor, span_start))
                    cursor = max(cursor, span_end)
                if (end - cursor).total_seconds() >= float(minimum) * 60:
                    available.append((cursor, end))
                selected, next_cursor = _page_items(
                    available,
                    query=query,
                    namespace="calendar.available-windows",
                    identity=lambda window: {
                        "start": window[0],
                        "end": window[1],
                    },
                )
                raw = {
                    "status": status,
                    "window": {
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "timezone": query.timezone,
                    },
                    "available_minutes": round(
                        sum(
                            (window_end - window_start).total_seconds()
                            for window_start, window_end in available
                        )
                        / 60,
                        2,
                    ),
                    "windows": [
                        {
                            "start": window_start.isoformat(),
                            "end": window_end.isoformat(),
                        }
                        for window_start, window_end in selected
                    ],
                    "truncated": next_cursor is not None,
                }
        raw["freshness"] = {
            "recorded_at": freshness.isoformat() if freshness else None,
            "status": (
                "calendar_sync_success"
                if freshness
                else "unavailable"
            ),
        }
        raw["coverage"] = coverage
        raw["limitations"] = sorted(
            {
                *list(raw.get("limitations") or []),
                "calendar_titles_omitted",
                *completeness_limitations,
                *visibility_limitations,
            }
        )
        aggregate_ref = calendar_aggregate_source_ref(
            rows,
            capability=query.capability,
            start=start,
            end=end,
            timezone=query.timezone,
            granularity=query.granularity,
            parameters=query.parameters,
            source_scope=source_scope,
            collected_at=freshness,
            coverage=(
                float(coverage["ratio"])
                if isinstance(coverage.get("ratio"), int | float)
                and not isinstance(coverage.get("ratio"), bool)
                else None
            ),
        )
        return _result(
            query,
            raw,
            refs=(aggregate_ref,),
            refs_complete=True,
            now=now,
            truncated=bool(raw.get("truncated")),
            next_cursor=(
                next_cursor
                if query.capability
                in {
                    "calendar.busy-intervals",
                    "calendar.available-windows",
                }
                else None
            ),
        )
