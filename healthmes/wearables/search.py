"""Bounded, sanitized Open Wearables search for the HealthMes MCP surface."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from datetime import UTC, date, datetime, time, timedelta, timezone
from functools import partial
from typing import Any

from healthmes.decision.contracts import PrivacyLevel
from healthmes.mcp_server.ow_client import OWClient
from healthmes.timezones import parse_timezone
from healthmes.wearables.binding import (
    OpenWearablesExecutionDataSource,
    current_open_wearables_execution_binding,
)
from healthmes.wearables.lineage import OpenWearablesLineageMode
from healthmes.wearables.open_wearables_routes import (
    OPEN_WEARABLES_V1_EXPOSED_CAPABILITIES,
)
from healthmes.wearables.whoop_recovery import (
    WHOOP_RECOVERY_PACKAGE_CAPABILITY,
    WHOOP_UPSTREAM_PROVIDER,
    calculate_whoop_recovery_package,
)

WEARABLE_DETAIL_CAPABILITIES = OPEN_WEARABLES_V1_EXPOSED_CAPABILITIES
WEARABLE_HEALTH_SCORE_CATEGORIES = (
    "activity",
    "body_battery",
    "day_strain",
    "readiness",
    "recovery",
    "resilience",
    "sleep",
    "strain",
    "stress",
)
WEARABLE_SUMMARY_KINDS = ("activity", "recovery", "sleep")
WEARABLE_PROVIDER_WORKOUT_PROVIDERS = (
    "garmin",
    "polar",
    "suunto",
)
WEARABLE_TIMESERIES_RESOLUTIONS = ("1min", "5min", "15min", "1hour")
WEARABLE_TIMESERIES_TYPES = (
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
)

MAX_WEARABLE_SEARCH_PAGES = 3
MAX_WEARABLE_SEARCH_ROWS = 250
MAX_WEARABLE_SEARCH_PAYLOAD_BYTES = 180_000
_PAGE_SIZE = 100
_TIMESERIES_WINDOWS = {
    "1min": timedelta(hours=6),
    "5min": timedelta(days=1),
    "15min": timedelta(days=7),
    "1hour": timedelta(days=30),
}
_TIMESERIES_RESOLUTION_SECONDS = {
    "1min": 60,
    "5min": 5 * 60,
    "15min": 15 * 60,
    "1hour": 60 * 60,
}
_SUM_TIMESERIES_TYPES = frozenset(
    {
        "active_time",
        "energy",
        "exercise_time",
        "stand_time",
        "steps",
        "time_in_daylight",
    }
)
_CAPABILITY_PARAMETERS = {
    "wearable.body-summary": frozenset(
        {"average_period", "latest_window_hours"}
    ),
    "wearable.health-scores": frozenset({"category"}),
    "wearable.menstrual-cycles": frozenset(),
    "wearable.provider-workout-detail": frozenset(
        {"provider", "route", "samples", "workout_id", "zones"}
    ),
    "wearable.provider-workouts": frozenset(
        {"provider", "route", "samples", "zones"}
    ),
    "wearable.sleep-sessions": frozenset(),
    "wearable.summaries": frozenset({"summary_kind"}),
    "wearable.workouts": frozenset(),
    "wearable.timeseries": frozenset({"resolution", "series_type"}),
    WHOOP_RECOVERY_PACKAGE_CAPABILITY: frozenset(),
}
_PRIVATE_KEYS = frozenset(
    {
        "accesstoken",
        "accountid",
        "apikey",
        "authorization",
        "connectionid",
        "credential",
        "datasourceid",
        "externaluserid",
        "password",
        "privatekey",
        "refreshtoken",
        "secret",
        "token",
        "userid",
    }
)
_PROVIDER_FAMILY_ALIASES = {
    "apple": "apple_health",
    "applehealth": "apple_health",
    "applehealthsdk": "apple_health",
    "fitbit": "fitbit",
    "garmin": "garmin",
    "garminconnect": "garmin",
    "google": "google_health_connect",
    "googlehealthconnect": "google_health_connect",
    "googlehealthconnectsdk": "google_health_connect",
    "internal": "internal",
    "oura": "oura",
    "polar": "polar",
    "polarflow": "polar",
    "samsung": "samsung_health",
    "samsunghealth": "samsung_health",
    "strava": "strava",
    "suunto": "suunto",
    "suuntoapp": "suunto",
    "ultrahuman": "ultrahuman",
    "whoop": "whoop",
}
_INTERNAL_PAGE_INDEX = "_healthmes_page_index"
_INTERNAL_PAGE_ORDINAL = "_healthmes_page_ordinal"
_INTERNAL_ROW_IDENTITY = "_healthmes_row_identity"
_INTERNAL_STABLE_ROW_ID = "_healthmes_stable_row_id"
_INTERNAL_STREAM_KEY = "_healthmes_stream_key"
_PROVIDER_ROW_ID_FIELDS = (
    "provider_workout_id",
    "provider_numeric_workout_id",
    "id",
    "record_id",
    "summary_id",
    "workout_id",
    "sample_id",
    "event_id",
)
_TRUSTED_PROVIDER_ATTRIBUTIONS = frozenset(
    {"allowed_provider_binding", "declared", "source_exact_alias"}
)
_SAFE_PROVIDER_WORKOUT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,512}$")
_ISO_DURATION = re.compile(
    r"^P"
    r"(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T"
    r"(?:(?P<hours>\d+(?:\.\d+)?)H)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?"
    r")?$",
    re.IGNORECASE,
)
_MAX_GRANULAR_WORKOUT_ROWS = 250
_MAX_GRANULAR_WORKOUT_TEXT = 4_096
_MAX_SLEEP_STAGE_INTERVALS = 250
_MAX_COLLECTION_CLOCK_SKEW = timedelta(minutes=5)
_SLEEP_STAGE_TYPES = frozenset(
    {
        "awake",
        "deep",
        "in_bed",
        "light",
        "rem",
        "sleeping",
        "unknown",
    }
)

WearableUserIdResolver = Callable[[], str | Awaitable[str]]
_LIVE_SOURCE_LINEAGE_ATTESTATION = object()


@dataclass(frozen=True, slots=True)
class WearableSearchRequest:
    capability: str
    start: datetime
    end: datetime
    timezone: str
    parameters: Mapping[str, Any]
    collected_at: datetime
    privacy_level: PrivacyLevel = PrivacyLevel.AGGREGATE
    retained_after: datetime | None = None
    as_of: date | None = None
    allowed_providers: tuple[str, ...] | frozenset[str] | None = None
    provider_source_allowlist: Mapping[str, frozenset[str]] | None = None
    provider_source_identities: Mapping[
        str,
        tuple[OpenWearablesExecutionDataSource, ...],
    ] | None = None
    lineage_mode: OpenWearablesLineageMode | None = None
    # A retained snapshot is source-bound at the event level. Its public
    # records intentionally omit raw data-source IDs, so the binding digest
    # is the lineage proof for those already-verified records.
    provider_source_lineage_verified: bool = False


@dataclass(frozen=True, slots=True)
class _ProviderPage:
    rows: tuple[dict[str, Any], ...]
    raw_count: int
    discarded_rows: int


@dataclass(frozen=True, slots=True)
class WearableSearchFetch:
    records: tuple[dict[str, Any], ...]
    upstream_truncated: bool = False
    upstream_completeness_unverified: bool = False
    payload_trimmed: bool = False
    discarded_rows: int = 0
    summary_window_partial: bool = False
    conflicting_duplicate_rows: bool = False
    stream_attribution_unavailable: bool = False
    granular_truncated: bool = False
    package: dict[str, Any] | None = None
    private_provenance: tuple[dict[str, Any], ...] = ()
    provider_source_lineage_verified: bool = False
    _live_source_lineage_attestation: object | None = dataclass_field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def live_source_lineage_attested(self) -> bool:
        """Whether the bounded live adapter performed exact source filtering."""

        return (
            self.provider_source_lineage_verified
            and self._live_source_lineage_attestation
            is _LIVE_SOURCE_LINEAGE_ATTESTATION
        )

    @property
    def limitations(self) -> tuple[str, ...]:
        values: list[str] = []
        if self.upstream_truncated:
            values.append("wearable_upstream_page_limit_reached")
        if self.upstream_completeness_unverified:
            values.append(
                "wearable_upstream_completeness_unverified"
            )
        if self.payload_trimmed:
            values.append("wearable_payload_limit_reached")
        if self.discarded_rows:
            values.append("wearable_rows_discarded")
        if self.summary_window_partial:
            values.append("wearable_summary_window_partial")
        if self.conflicting_duplicate_rows:
            values.append("wearable_conflicting_duplicate_rows")
        if self.stream_attribution_unavailable:
            values.append("wearable_stream_attribution_unavailable")
        if self.granular_truncated:
            values.append("wearable_granular_data_truncated")
        if any(
            record.get("provider") == "unknown"
            or record.get("provider_attribution")
            not in _TRUSTED_PROVIDER_ATTRIBUTIONS
            for record in self.records
        ):
            values.append("wearable_provider_attribution_unavailable")
        return tuple(values)


WearableSearchReader = Callable[
    [WearableSearchRequest],
    Awaitable[WearableSearchFetch],
]


def normalize_retained_wearable_health_scores(
    records: Sequence[Mapping[str, Any]],
    *,
    category: str | None,
    start: datetime,
    end: datetime,
    retained_after: datetime | None = None,
    allowed_providers: tuple[str, ...] | frozenset[str] | None = None,
    provider_source_allowlist: Mapping[str, frozenset[str]] | None = None,
    provider_source_lineage_verified: bool = False,
    lineage_mode: OpenWearablesLineageMode | None = None,
    allow_missing_source_id: bool = False,
) -> WearableSearchFetch:
    """Reapply the current window to retained health-score records."""

    if (
        category is not None
        and category not in WEARABLE_HEALTH_SCORE_CATEGORIES
    ):
        raise ValueError("wearable health score category is not allowlisted")
    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    canonical_allowed = _canonical_allowed_providers(allowed_providers)
    normalized: list[dict[str, Any]] = []
    discarded = 0
    for record in records:
        recorded_at = _timestamp(record.get("recorded_at"))
        retained_category = _safe_text(
            record.get("category"),
            max_length=32,
        )
        if (
            recorded_at is None
            or not start_utc <= recorded_at < end_utc
            or not _observation_is_retained(
                recorded_at,
                retained_after=retained_after,
            )
            or retained_category
            not in WEARABLE_HEALTH_SCORE_CATEGORIES
            or (
                category is not None
                and retained_category != category
            )
        ):
            discarded += 1
            continue
        clean = _filter_provider_attribution(
            dict(record),
            capability="wearable.health-scores",
            source_row=record,
            allowed_providers=canonical_allowed,
            provider_source_allowlist=provider_source_allowlist,
            lineage_mode=lineage_mode,
            allow_missing_source_id=allow_missing_source_id,
        )
        if clean is None:
            discarded += 1
            continue
        normalized.append(clean)
    normalized.sort(key=_row_sort_key)
    return WearableSearchFetch(
        records=tuple(normalized),
        discarded_rows=discarded,
        provider_source_lineage_verified=(
            provider_source_lineage_verified
        ),
    )


def normalize_retained_wearable_summaries(
    records: Sequence[Mapping[str, Any]],
    *,
    kind: str,
    start: datetime,
    end: datetime,
    timezone: str,
    retained_after: datetime | None = None,
    allowed_providers: tuple[str, ...] | frozenset[str] | None = None,
    provider_source_allowlist: Mapping[str, frozenset[str]] | None = None,
    provider_source_lineage_verified: bool = False,
    lineage_mode: OpenWearablesLineageMode | None = None,
    allow_missing_source_id: bool = False,
) -> WearableSearchFetch:
    """Filter legacy mirrors against the exact retained summary window."""

    if kind not in WEARABLE_SUMMARY_KINDS:
        raise ValueError("wearable summary kind is not allowlisted")
    canonical_allowed = _canonical_allowed_providers(allowed_providers)
    normalized: list[dict[str, Any]] = []
    discarded = 0
    for record in records:
        observed_day = _day(record.get("date"))
        if (
            record.get("summary_kind") != kind
            or observed_day is None
            or not _summary_day_fully_covered(
                observed_day,
                start=start,
                end=end,
                timezone=timezone,
            )
            or not _observation_is_retained(
                _summary_day_bounds(
                    observed_day,
                    timezone=timezone,
                )[0],
                retained_after=retained_after,
            )
        ):
            discarded += 1
            continue
        clean = _filter_provider_attribution(
            dict(record),
            capability="wearable.summaries",
            source_row=record,
            allowed_providers=canonical_allowed,
            provider_source_allowlist=provider_source_allowlist,
            lineage_mode=lineage_mode,
            allow_missing_source_id=allow_missing_source_id,
        )
        if clean is None:
            discarded += 1
            continue
        normalized.append(clean)
    normalized.sort(key=_row_sort_key)
    return WearableSearchFetch(
        records=tuple(normalized),
        discarded_rows=discarded,
        summary_window_partial=_summary_window_is_partial(
            start=start,
            end=end,
            timezone=timezone,
        ),
        provider_source_lineage_verified=(
            provider_source_lineage_verified
        ),
    )


def normalize_retained_wearable_timeseries(
    records: Sequence[Mapping[str, Any]],
    *,
    series_type: str,
    resolution: str,
    start: datetime,
    end: datetime,
    stream_attribution_verified: bool = False,
    retained_after: datetime | None = None,
    allowed_providers: tuple[str, ...] | frozenset[str] | None = None,
    provider_source_allowlist: Mapping[str, frozenset[str]] | None = None,
    provider_source_lineage_verified: bool = False,
    lineage_mode: OpenWearablesLineageMode | None = None,
    allow_missing_source_id: bool = False,
) -> WearableSearchFetch:
    """Reapply current privacy rules to previously stored public records."""

    if series_type not in WEARABLE_TIMESERIES_TYPES:
        raise ValueError("wearable timeseries type is not allowlisted")
    if resolution not in WEARABLE_TIMESERIES_RESOLUTIONS:
        raise ValueError("wearable timeseries resolution is not allowlisted")

    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    canonical_allowed = _canonical_allowed_providers(allowed_providers)
    normalized: list[dict[str, Any]] = []
    discarded = 0
    for record in records:
        timestamp = _timestamp(record.get("timestamp"))
        retained_type = _safe_text(
            record.get("series_type"),
            max_length=64,
        )
        value = _number(record.get("value"))
        unit = _safe_text(record.get("unit"), max_length=32)
        provider = _safe_text(record.get("provider"), max_length=64)
        if (
            timestamp is None
            or not start_utc <= timestamp < end_utc
            or not _observation_is_retained(
                timestamp,
                retained_after=retained_after,
            )
            or retained_type != series_type
            or value is None
            or unit is None
            or provider is None
        ):
            discarded += 1
            continue
        bucket_start = _resolution_bucket_start(
            timestamp,
            resolution=resolution,
        )
        public_timestamp = _bounded_bucket_timestamp(
            bucket_start,
            start=start_utc,
            end=end_utc,
            observation=timestamp,
            retained_after=retained_after,
        )
        result: dict[str, Any] = {
            "timestamp": public_timestamp.isoformat(),
            "series_type": series_type,
            "value": value,
            "unit": unit,
            "provider": provider,
        }
        attribution = _safe_text(
            record.get("provider_attribution"),
            max_length=32,
        )
        if attribution is not None:
            result["provider_attribution"] = attribution
        zone_offset = _safe_text(
            record.get("zone_offset"),
            max_length=16,
        )
        if zone_offset is not None:
            result["zone_offset"] = zone_offset
        if type(record.get("is_daily_total")) is bool:
            result["is_daily_total"] = record["is_daily_total"]
        clean = _filter_provider_attribution(
            result,
            capability="wearable.timeseries",
            source_row=record,
            allowed_providers=canonical_allowed,
            provider_source_allowlist=provider_source_allowlist,
            lineage_mode=lineage_mode,
            allow_missing_source_id=allow_missing_source_id,
        )
        if clean is None:
            discarded += 1
            continue
        normalized.append(clean)
    normalized.sort(key=_row_sort_key)
    return WearableSearchFetch(
        records=tuple(normalized),
        discarded_rows=discarded,
        stream_attribution_unavailable=(
            bool(normalized) and not stream_attribution_verified
        ),
        provider_source_lineage_verified=(
            provider_source_lineage_verified
        ),
    )


def normalize_retained_wearable_workouts(
    records: Sequence[Mapping[str, Any]],
    *,
    start: datetime,
    end: datetime,
    retained_after: datetime | None = None,
    allowed_providers: tuple[str, ...] | frozenset[str] | None = None,
    provider_source_allowlist: Mapping[str, frozenset[str]] | None = None,
    provider_source_lineage_verified: bool = False,
    lineage_mode: OpenWearablesLineageMode | None = None,
    allow_missing_source_id: bool = False,
) -> WearableSearchFetch:
    """Reject retained workouts whose complete interval is no longer valid."""

    canonical_allowed = _canonical_allowed_providers(allowed_providers)
    normalized: list[dict[str, Any]] = []
    discarded = 0
    for record in records:
        start_time = _timestamp(record.get("start_time"))
        end_time = _timestamp(record.get("end_time"))
        if not _workout_interval_is_allowed(
            start_time,
            end_time,
            start=start,
            end=end,
            retained_after=retained_after,
        ):
            discarded += 1
            continue
        clean = _filter_provider_attribution(
            dict(record),
            capability="wearable.workouts",
            source_row=record,
            allowed_providers=canonical_allowed,
            provider_source_allowlist=provider_source_allowlist,
            lineage_mode=lineage_mode,
            allow_missing_source_id=allow_missing_source_id,
        )
        if clean is None:
            discarded += 1
            continue
        normalized.append(clean)
    normalized.sort(key=_row_sort_key)
    return WearableSearchFetch(
        records=tuple(normalized),
        discarded_rows=discarded,
        provider_source_lineage_verified=(
            provider_source_lineage_verified
        ),
    )


def normalize_retained_wearable_search(
    records: Sequence[Mapping[str, Any]],
    *,
    request: WearableSearchRequest,
    stream_attribution_verified: bool = False,
    allow_missing_source_id: bool = False,
) -> WearableSearchFetch:
    """Reapply current capability, privacy, and time rules to a local mirror."""

    request = _request_with_execution_binding(request)
    validate_wearable_search_request(request)
    if request.capability == "wearable.health-scores":
        return normalize_retained_wearable_health_scores(
            records,
            category=(
                str(request.parameters["category"])
                if request.parameters.get("category") is not None
                else None
            ),
            start=request.start,
            end=request.end,
            retained_after=request.retained_after,
            allowed_providers=request.allowed_providers,
            provider_source_allowlist=request.provider_source_allowlist,
            provider_source_lineage_verified=(
                request.provider_source_lineage_verified
            ),
            lineage_mode=request.lineage_mode,
            allow_missing_source_id=allow_missing_source_id,
        )
    if request.capability == "wearable.summaries":
        return normalize_retained_wearable_summaries(
            records,
            kind=str(request.parameters["summary_kind"]),
            start=request.start,
            end=request.end,
            timezone=request.timezone,
            retained_after=request.retained_after,
            allowed_providers=request.allowed_providers,
            provider_source_allowlist=request.provider_source_allowlist,
            provider_source_lineage_verified=(
                request.provider_source_lineage_verified
            ),
            lineage_mode=request.lineage_mode,
            allow_missing_source_id=allow_missing_source_id,
        )
    if request.capability == "wearable.workouts":
        return normalize_retained_wearable_workouts(
            records,
            start=request.start,
            end=request.end,
            retained_after=request.retained_after,
            allowed_providers=request.allowed_providers,
            provider_source_allowlist=request.provider_source_allowlist,
            provider_source_lineage_verified=(
                request.provider_source_lineage_verified
            ),
            lineage_mode=request.lineage_mode,
            allow_missing_source_id=allow_missing_source_id,
        )
    if request.capability == "wearable.timeseries":
        return normalize_retained_wearable_timeseries(
            records,
            series_type=str(request.parameters["series_type"]),
            resolution=str(request.parameters["resolution"]),
            start=request.start,
            end=request.end,
            stream_attribution_verified=stream_attribution_verified,
            retained_after=request.retained_after,
            allowed_providers=request.allowed_providers,
            provider_source_allowlist=request.provider_source_allowlist,
            provider_source_lineage_verified=(
                request.provider_source_lineage_verified
            ),
            lineage_mode=request.lineage_mode,
            allow_missing_source_id=allow_missing_source_id,
        )

    if request.capability == WHOOP_RECOVERY_PACKAGE_CAPABILITY:
        raise ValueError("WHOOP package mirrors use their dedicated normalizer")

    if request.capability == "wearable.body-summary":
        sanitizer = partial(
            _sanitize_body_summary,
            start=request.start,
            end=request.end,
            retained_after=request.retained_after,
            collected_at=request.collected_at,
            average_period=_bounded_request_integer(
                request.parameters,
                "average_period",
                default=7,
                minimum=1,
                maximum=7,
            ),
            latest_window_hours=_bounded_request_integer(
                request.parameters,
                "latest_window_hours",
                default=4,
                minimum=1,
                maximum=24,
            ),
        )
    elif request.capability == "wearable.sleep-sessions":
        sanitizer = partial(
            _sanitize_sleep_session,
            start=request.start,
            end=request.end,
            retained_after=request.retained_after,
            privacy_level=request.privacy_level,
        )
    elif request.capability == "wearable.menstrual-cycles":
        sanitizer = partial(
            _sanitize_menstrual_cycle,
            start=request.start,
            end=request.end,
            retained_after=request.retained_after,
        )
    elif request.capability in {
        "wearable.provider-workout-detail",
        "wearable.provider-workouts",
    }:
        sanitizer = partial(
            _sanitize_retained_provider_workout,
            provider=str(request.parameters["provider"]),
            expected_workout_id=(
                str(request.parameters["workout_id"])
                if request.capability
                == "wearable.provider-workout-detail"
                else None
            ),
            start=request.start,
            end=request.end,
            retained_after=request.retained_after,
            samples=request.parameters.get("samples", False) is True,
            zones=request.parameters.get("zones", False) is True,
            route=request.parameters.get("route", False) is True,
        )
    else:
        raise ValueError("unsupported wearable detail capability")

    normalized: list[dict[str, Any]] = []
    discarded = 0
    allowed_providers = _canonical_allowed_providers(
        request.allowed_providers
    )
    for record in records:
        clean = sanitizer(record)
        if clean is None:
            discarded += 1
            continue
        filtered = _filter_provider_attribution(
            clean,
            capability=request.capability,
            source_row=record,
            allowed_providers=allowed_providers,
            body_summary=(
                request.capability == "wearable.body-summary"
            ),
            provider_source_allowlist=request.provider_source_allowlist,
            lineage_mode=request.lineage_mode,
            allow_missing_source_id=allow_missing_source_id,
        )
        if filtered is None:
            discarded += 1
            continue
        normalized.append(filtered)
    normalized, conflicting = _deduplicate_wearable_records(normalized)
    normalized.sort(key=_row_sort_key)
    return WearableSearchFetch(
        records=tuple(normalized),
        discarded_rows=discarded,
        conflicting_duplicate_rows=conflicting,
        granular_truncated=any(
            record.get("granular_truncated") is True
            for record in normalized
        ),
        provider_source_lineage_verified=(
            request.provider_source_lineage_verified
        ),
    )


def validate_wearable_search_request(
    request: WearableSearchRequest,
) -> None:
    """Validate capability-specific bounds before any upstream access."""

    if request.capability not in WEARABLE_DETAIL_CAPABILITIES:
        raise ValueError("unsupported wearable detail capability")
    if (
        request.start.tzinfo is None
        or request.start.utcoffset() is None
        or request.end.tzinfo is None
        or request.end.utcoffset() is None
    ):
        raise ValueError("wearable query bounds must be timezone-aware")
    start = request.start.astimezone(UTC)
    end = request.end.astimezone(UTC)
    if end <= start:
        raise ValueError("wearable query end must be after start")
    if (
        request.collected_at.tzinfo is None
        or request.collected_at.utcoffset() is None
    ):
        raise ValueError(
            "wearable collection timestamp must be timezone-aware"
        )
    parse_timezone(request.timezone)
    if request.retained_after is not None:
        retained_after = request.retained_after
        if (
            retained_after.tzinfo is None
            or retained_after.utcoffset() is None
        ):
            raise ValueError("wearable retention cutoff must be timezone-aware")
    if type(request.privacy_level) is not PrivacyLevel:
        raise ValueError("wearable privacy level is invalid")
    if type(request.provider_source_lineage_verified) is not bool:
        raise ValueError(
            "wearable provider source lineage state is invalid"
        )
    allowed_providers = _canonical_allowed_providers(
        request.allowed_providers
    )
    unexpected = (
        set(request.parameters)
        - _CAPABILITY_PARAMETERS[request.capability]
    )
    if unexpected:
        raise ValueError("wearable query contains unsupported parameters")
    if request.capability == "wearable.body-summary":
        _bounded_request_integer(
            request.parameters,
            "average_period",
            default=7,
            minimum=1,
            maximum=7,
        )
        _bounded_request_integer(
            request.parameters,
            "latest_window_hours",
            default=4,
            minimum=1,
            maximum=24,
        )
    elif request.capability == "wearable.health-scores":
        category = request.parameters.get("category")
        if (
            category is not None
            and category not in WEARABLE_HEALTH_SCORE_CATEGORIES
        ):
            raise ValueError("wearable health score category is not allowlisted")
    elif request.capability == "wearable.summaries":
        if (
            request.parameters.get("summary_kind")
            not in WEARABLE_SUMMARY_KINDS
        ):
            raise ValueError("wearable summary kind is not allowlisted")
    elif request.capability in {
        "wearable.provider-workout-detail",
        "wearable.provider-workouts",
    }:
        provider = request.parameters.get("provider")
        if (
            type(provider) is not str
            or provider not in WEARABLE_PROVIDER_WORKOUT_PROVIDERS
        ):
            raise ValueError(
                "wearable provider workout provider is not allowlisted"
            )
        for field in ("samples", "zones", "route"):
            value = request.parameters.get(field, False)
            if type(value) is not bool:
                raise ValueError(
                    f"wearable provider workout {field} must be boolean"
                )
        if (
            any(
                request.parameters.get(field, False) is True
                for field in ("samples", "zones", "route")
            )
            and request.privacy_level is not PrivacyLevel.IDENTITY
        ):
            raise ValueError(
                "granular provider workout data requires identity privacy"
            )
        if provider != "polar" and any(
            request.parameters.get(field, False) is True
            for field in ("samples", "zones", "route")
        ):
            raise ValueError(
                "granular provider workout options are supported only for polar"
            )
        if request.capability == "wearable.provider-workout-detail":
            workout_id = request.parameters.get("workout_id")
            if (
                type(workout_id) is not str
                or _SAFE_PROVIDER_WORKOUT_ID.fullmatch(workout_id) is None
            ):
                raise ValueError(
                    "wearable provider workout_id is invalid"
                )
    elif request.capability == WHOOP_RECOVERY_PACKAGE_CAPABILITY:
        _whoop_as_of(request.as_of)
        if (
            allowed_providers is not None
            and WHOOP_UPSTREAM_PROVIDER not in allowed_providers
        ):
            raise ValueError(
                "WHOOP package requires an allowed WHOOP provider"
            )
    elif request.as_of is not None:
        raise ValueError("as_of is only supported by the WHOOP package")
    if request.capability != "wearable.timeseries":
        if end - start > timedelta(days=30):
            raise ValueError("wearable detail window exceeds 30 days")
        return

    series_type = request.parameters.get("series_type")
    resolution = request.parameters.get("resolution")
    if series_type not in WEARABLE_TIMESERIES_TYPES:
        raise ValueError("wearable timeseries type is not allowlisted")
    if resolution not in WEARABLE_TIMESERIES_RESOLUTIONS:
        raise ValueError("wearable timeseries resolution is not allowlisted")
    if end - start > _TIMESERIES_WINDOWS[str(resolution)]:
        raise ValueError("wearable timeseries window exceeds resolution limit")


def _bounded_request_integer(
    parameters: Mapping[str, Any],
    field: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = parameters.get(field, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(
            f"wearable {field} must be between {minimum} and {maximum}"
        )
    return value


class BoundedOpenWearablesSearch:
    """Read a narrow Open Wearables slice without exposing its raw database."""

    def __init__(
        self,
        client: OWClient,
        user_id_resolver: WearableUserIdResolver,
    ) -> None:
        self._client = client
        self._user_id_resolver = user_id_resolver

    async def __call__(
        self,
        request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        binding = current_open_wearables_execution_binding(
            request.capability
        )
        if binding is not None and binding.retained_only:
            raise LookupError(
                "retained-only Open Wearables binding cannot use live REST"
            )
        request = replace(
            _request_with_execution_binding(request),
            provider_source_lineage_verified=False,
        )
        validate_wearable_search_request(request)
        user_id = self._user_id_resolver()
        if inspect.isawaitable(user_id):
            user_id = await user_id
        if not isinstance(user_id, str) or not user_id:
            raise LookupError("open-wearables user is unavailable")

        if request.capability == WHOOP_RECOVERY_PACKAGE_CAPABILITY:
            return await self._whoop_recovery_package(user_id, request)

        summary_window_partial = False
        upstream_completeness_unverified = False
        if request.capability == "wearable.body-summary":
            rows, truncated, discarded = await self._body_summary(
                user_id,
                request,
            )
            sanitizer = partial(
                _sanitize_body_summary,
                start=request.start,
                end=request.end,
                retained_after=request.retained_after,
                collected_at=request.collected_at,
                average_period=_bounded_request_integer(
                    request.parameters,
                    "average_period",
                    default=7,
                    minimum=1,
                    maximum=7,
                ),
                latest_window_hours=_bounded_request_integer(
                    request.parameters,
                    "latest_window_hours",
                    default=4,
                    minimum=1,
                    maximum=24,
                ),
            )
        elif request.capability == "wearable.health-scores":
            rows, truncated, discarded = await self._health_scores(
                user_id,
                request,
            )
            sanitizer = partial(
                _sanitize_health_score,
                start=request.start,
                end=request.end,
                retained_after=request.retained_after,
            )
        elif request.capability == "wearable.summaries":
            rows, truncated, discarded = await self._summaries(
                user_id,
                request,
            )
            kind = str(request.parameters["summary_kind"])
            summary_window_partial = _summary_window_is_partial(
                start=request.start,
                end=request.end,
                timezone=request.timezone,
            )
            sanitizer = partial(
                _sanitize_summary,
                kind=kind,
                start=request.start,
                end=request.end,
                timezone=request.timezone,
                retained_after=request.retained_after,
            )
        elif request.capability == "wearable.sleep-sessions":
            rows, truncated, discarded = await self._sleep_sessions(
                user_id,
                request,
            )
            sanitizer = partial(
                _sanitize_sleep_session,
                start=request.start,
                end=request.end,
                retained_after=request.retained_after,
                privacy_level=request.privacy_level,
            )
        elif request.capability == "wearable.menstrual-cycles":
            rows, truncated, discarded = await self._menstrual_cycles(
                user_id,
                request,
            )
            sanitizer = partial(
                _sanitize_menstrual_cycle,
                start=request.start,
                end=request.end,
                retained_after=request.retained_after,
            )
        elif request.capability == "wearable.provider-workouts":
            (
                rows,
                truncated,
                discarded,
                upstream_completeness_unverified,
            ) = await self._provider_workouts(user_id, request)
            sanitizer = partial(
                _sanitize_provider_workout,
                provider=str(request.parameters["provider"]),
                expected_workout_id=None,
                start=request.start,
                end=request.end,
                retained_after=request.retained_after,
                samples=request.parameters.get("samples", False) is True,
                zones=request.parameters.get("zones", False) is True,
                route=request.parameters.get("route", False) is True,
            )
        elif request.capability == "wearable.provider-workout-detail":
            rows, truncated, discarded = (
                await self._provider_workout_detail(
                    user_id,
                    request,
                )
            )
            sanitizer = partial(
                _sanitize_provider_workout,
                provider=str(request.parameters["provider"]),
                expected_workout_id=str(
                    request.parameters["workout_id"]
                ),
                start=request.start,
                end=request.end,
                retained_after=request.retained_after,
                samples=request.parameters.get("samples", False) is True,
                zones=request.parameters.get("zones", False) is True,
                route=request.parameters.get("route", False) is True,
            )
        elif request.capability == "wearable.workouts":
            rows, truncated, discarded = await self._workouts(
                user_id,
                request,
            )
            sanitizer = partial(
                _sanitize_workout,
                start=request.start,
                end=request.end,
                retained_after=request.retained_after,
            )
        elif request.capability == "wearable.timeseries":
            rows, truncated, discarded = await self._timeseries(
                user_id,
                request,
            )
            series_type = str(request.parameters["series_type"])
            sanitizer = partial(
                _sanitize_timeseries,
                series_type=series_type,
                start=request.start,
                end=request.end,
                retained_after=request.retained_after,
            )
        else:
            raise ValueError("unsupported wearable detail capability")

        sanitized: list[dict[str, Any]] = []
        lineage_verified_rows = 0
        lineage_rejected_rows = 0
        allowed_providers = _canonical_allowed_providers(
            request.allowed_providers
        )
        upstream_rows_present = bool(rows) or discarded > 0
        for row in rows:
            source_row = _bind_provider_source_identity(
                row,
                capability=request.capability,
                provider_source_allowlist=(
                    request.provider_source_allowlist
                ),
                provider_source_identities=(
                    request.provider_source_identities
                ),
                lineage_mode=request.lineage_mode,
                authoritative_provider=_request_provider(request),
            )
            if source_row is None:
                lineage_rejected_rows += 1
                discarded += 1
                continue
            lineage_verified_rows += 1
            clean = sanitizer(source_row)
            if clean is None or _contains_private_value(clean, user_id):
                discarded += 1
                continue
            filtered = _filter_provider_attribution(
                clean,
                capability=request.capability,
                source_row=source_row,
                allowed_providers=allowed_providers,
                body_summary=(
                    request.capability == "wearable.body-summary"
                ),
                provider_source_allowlist=(
                    request.provider_source_allowlist
                ),
                lineage_mode=request.lineage_mode,
                authoritative_provider=_request_provider(request),
            )
            if filtered is None:
                discarded += 1
                continue
            sanitized.append(filtered)
        conflicting_duplicate_rows = False
        stream_attribution_unavailable = False
        sanitized, conflicting_duplicate_rows = (
            _deduplicate_wearable_records(sanitized)
        )
        if request.capability == "wearable.timeseries":
            sanitized, stream_attribution_unavailable = _aggregate_timeseries(
                sanitized,
                series_type=str(request.parameters["series_type"]),
                resolution=str(request.parameters["resolution"]),
                start=request.start,
                end=request.end,
                retained_after=request.retained_after,
            )
        sanitized.sort(key=_row_sort_key)

        row_trimmed = len(sanitized) > MAX_WEARABLE_SEARCH_ROWS
        selected = sanitized[:MAX_WEARABLE_SEARCH_ROWS]
        payload_trimmed = False
        while (
            selected
            and _encoded_size({"records": selected})
            > MAX_WEARABLE_SEARCH_PAYLOAD_BYTES
        ):
            selected.pop()
            payload_trimmed = True
        return WearableSearchFetch(
            records=tuple(selected),
            upstream_truncated=truncated or row_trimmed,
            upstream_completeness_unverified=(
                upstream_completeness_unverified
            ),
            payload_trimmed=payload_trimmed,
            discarded_rows=discarded,
            summary_window_partial=summary_window_partial,
            conflicting_duplicate_rows=conflicting_duplicate_rows,
            stream_attribution_unavailable=(
                stream_attribution_unavailable
            ),
            granular_truncated=any(
                record.get("granular_truncated") is True
                for record in selected
            ),
            provider_source_lineage_verified=(
                _lineage_response_attested(
                    request,
                    upstream_rows_present=upstream_rows_present,
                    lineage_verified_rows=lineage_verified_rows,
                    lineage_rejected_rows=lineage_rejected_rows,
                )
            ),
            _live_source_lineage_attestation=(
                _LIVE_SOURCE_LINEAGE_ATTESTATION
                if _lineage_response_attested(
                    request,
                    upstream_rows_present=upstream_rows_present,
                    lineage_verified_rows=lineage_verified_rows,
                    lineage_rejected_rows=lineage_rejected_rows,
                )
                else None
            ),
        )

    async def _whoop_recovery_package(
        self,
        user_id: str,
        request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        as_of = _whoop_as_of(request.as_of)
        timezone = parse_timezone(request.timezone)
        fetch_start = datetime.combine(
            as_of - timedelta(days=2),
            time.min,
            tzinfo=timezone,
        ).astimezone(UTC)
        fetch_end = datetime.combine(
            as_of + timedelta(days=1),
            time.min,
            tzinfo=timezone,
        ).astimezone(UTC)

        recovery_rows, recovery_truncated, recovery_discarded = (
            await self._whoop_health_scores(
                user_id,
                start=fetch_start,
                end=fetch_end,
                category="recovery",
            )
        )
        recovery_upstream_rows_present = (
            bool(recovery_rows) or recovery_discarded > 0
        )
        day_strain_rows, day_strain_truncated, day_strain_discarded = (
            await self._whoop_health_scores(
                user_id,
                start=fetch_start,
                end=fetch_end,
                category="day_strain",
            )
        )
        day_strain_upstream_rows_present = (
            bool(day_strain_rows) or day_strain_discarded > 0
        )
        (
            recovery_rows,
            recovery_lineage_discarded,
            recovery_lineage_verified,
        ) = (
            _filter_whoop_source_rows(
                recovery_rows,
                provider_source_allowlist=request.provider_source_allowlist,
                provider_source_identities=(
                    request.provider_source_identities
                ),
                lineage_mode=request.lineage_mode,
            )
        )
        (
            day_strain_rows,
            day_strain_lineage_discarded,
            day_strain_lineage_verified,
        ) = (
            _filter_whoop_source_rows(
                day_strain_rows,
                provider_source_allowlist=request.provider_source_allowlist,
                provider_source_identities=(
                    request.provider_source_identities
                ),
                lineage_mode=request.lineage_mode,
            )
        )
        calculation = calculate_whoop_recovery_package(
            recovery_rows,
            day_strain_rows,
            as_of=as_of,
            timezone=timezone,
            recovery_truncated=recovery_truncated,
            day_strain_truncated=day_strain_truncated,
            retained_after=request.retained_after,
        )
        if _contains_private_value(
            calculation.public,
            user_id,
        ) or _contains_private_value(calculation.provenance, user_id):
            raise ValueError(
                "open-wearables package exposed the private user identifier"
            )
        return WearableSearchFetch(
            records=(),
            package=calculation.public,
            private_provenance=calculation.provenance,
            upstream_truncated=(
                recovery_truncated or day_strain_truncated
            ),
            discarded_rows=(
                recovery_discarded
                + day_strain_discarded
                + recovery_lineage_discarded
                + day_strain_lineage_discarded
            ),
            conflicting_duplicate_rows=(
                calculation.conflicting_duplicate_rows
            ),
            provider_source_lineage_verified=(
                _lineage_response_attested(
                    request,
                    upstream_rows_present=(
                        recovery_upstream_rows_present
                        or day_strain_upstream_rows_present
                    ),
                    lineage_verified_rows=(
                        recovery_lineage_verified
                        + day_strain_lineage_verified
                    ),
                    lineage_rejected_rows=(
                        recovery_lineage_discarded
                        + day_strain_lineage_discarded
                    ),
                    component_attested=(
                        (
                            not recovery_upstream_rows_present
                            or (
                                recovery_lineage_verified > 0
                                and recovery_lineage_discarded == 0
                            )
                        )
                        and (
                            not day_strain_upstream_rows_present
                            or (
                                day_strain_lineage_verified > 0
                                and day_strain_lineage_discarded == 0
                            )
                        )
                    ),
                )
            ),
            _live_source_lineage_attestation=(
                _LIVE_SOURCE_LINEAGE_ATTESTATION
                if _lineage_response_attested(
                    request,
                    upstream_rows_present=(
                        recovery_upstream_rows_present
                        or day_strain_upstream_rows_present
                    ),
                    lineage_verified_rows=(
                        recovery_lineage_verified
                        + day_strain_lineage_verified
                    ),
                    lineage_rejected_rows=(
                        recovery_lineage_discarded
                        + day_strain_lineage_discarded
                    ),
                    component_attested=(
                        (
                            not recovery_upstream_rows_present
                            or (
                                recovery_lineage_verified > 0
                                and recovery_lineage_discarded == 0
                            )
                        )
                        and (
                            not day_strain_upstream_rows_present
                            or (
                                day_strain_lineage_verified > 0
                                and day_strain_lineage_discarded == 0
                            )
                        )
                    ),
                )
                else None
            ),
        )

    async def _whoop_health_scores(
        self,
        user_id: str,
        *,
        start: datetime,
        end: datetime,
        category: str,
    ) -> tuple[list[dict[str, Any]], bool, int]:
        async def fetch(limit: int, offset: int) -> Mapping[str, Any]:
            return await self._client.get_health_scores(
                user_id,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                category=category,
                provider=WHOOP_UPSTREAM_PROVIDER,
                limit=limit,
                offset=offset,
            )

        return await _collect_offset_pages(fetch)

    async def _health_scores(
        self,
        user_id: str,
        request: WearableSearchRequest,
    ) -> tuple[list[dict[str, Any]], bool, int]:
        category = request.parameters.get("category")

        async def fetch(limit: int, offset: int) -> Mapping[str, Any]:
            return await self._client.get_health_scores(
                user_id,
                start_date=request.start.astimezone(UTC).isoformat(),
                end_date=request.end.astimezone(UTC).isoformat(),
                category=str(category) if category is not None else None,
                limit=limit,
                offset=offset,
            )

        return await _collect_offset_pages(fetch)

    async def _body_summary(
        self,
        user_id: str,
        request: WearableSearchRequest,
    ) -> tuple[list[dict[str, Any]], bool, int]:
        payload = await self._client.get_body_summary(
            user_id,
            average_period=_bounded_request_integer(
                request.parameters,
                "average_period",
                default=7,
                minimum=1,
                maximum=7,
            ),
            latest_window_hours=_bounded_request_integer(
                request.parameters,
                "latest_window_hours",
                default=4,
                minimum=1,
                maximum=24,
            ),
        )
        return ([] if payload is None else [dict(payload)]), False, 0

    async def _summaries(
        self,
        user_id: str,
        request: WearableSearchRequest,
    ) -> tuple[list[dict[str, Any]], bool, int]:
        kind = request.parameters.get("summary_kind")
        if kind not in WEARABLE_SUMMARY_KINDS:
            raise ValueError("wearable summary kind is not allowlisted")

        async def fetch(
            limit: int,
            cursor: str | None,
        ) -> Mapping[str, Any]:
            args = (
                user_id,
                request.start.astimezone(UTC).isoformat(),
                request.end.astimezone(UTC).isoformat(),
            )
            if kind == "activity":
                return await self._client.get_activity_summaries(
                    *args,
                    cursor=cursor,
                    limit=limit,
                    sort_order="asc",
                )
            if kind == "sleep":
                return await self._client.get_sleep_summaries(
                    *args,
                    cursor=cursor,
                    limit=limit,
                )
            return await self._client.get_recovery_summaries(
                *args,
                cursor=cursor,
                limit=limit,
            )

        return await _collect_cursor_pages(fetch)

    async def _workouts(
        self,
        user_id: str,
        request: WearableSearchRequest,
    ) -> tuple[list[dict[str, Any]], bool, int]:
        async def fetch(
            limit: int,
            cursor: str | None,
        ) -> Mapping[str, Any]:
            return await self._client.get_workouts(
                user_id,
                request.start.astimezone(UTC).isoformat(),
                request.end.astimezone(UTC).isoformat(),
                cursor=cursor,
                limit=limit,
            )

        return await _collect_cursor_pages(fetch)

    async def _sleep_sessions(
        self,
        user_id: str,
        request: WearableSearchRequest,
    ) -> tuple[list[dict[str, Any]], bool, int]:
        async def fetch(
            limit: int,
            cursor: str | None,
        ) -> Mapping[str, Any]:
            return await self._client.get_sleep_sessions(
                user_id,
                request.start.astimezone(UTC).isoformat(),
                request.end.astimezone(UTC).isoformat(),
                cursor=cursor,
                limit=limit,
                filter_by_priority=True,
            )

        return await _collect_cursor_pages(fetch)

    async def _menstrual_cycles(
        self,
        user_id: str,
        request: WearableSearchRequest,
    ) -> tuple[list[dict[str, Any]], bool, int]:
        async def fetch(
            limit: int,
            cursor: str | None,
        ) -> Mapping[str, Any]:
            return await self._client.get_menstrual_cycles(
                user_id,
                request.start.astimezone(UTC).isoformat(),
                request.end.astimezone(UTC).isoformat(),
                cursor=cursor,
                limit=limit,
            )

        return await _collect_cursor_pages(fetch)

    async def _provider_workouts(
        self,
        user_id: str,
        request: WearableSearchRequest,
    ) -> tuple[list[dict[str, Any]], bool, int, bool]:
        collection = await self._client.collect_vendor_workouts_tracked(
            str(request.parameters["provider"]),
            user_id,
            request.start.astimezone(UTC).isoformat(),
            request.end.astimezone(UTC).isoformat(),
            samples=request.parameters.get("samples", False) is True,
            zones=request.parameters.get("zones", False) is True,
            route=request.parameters.get("route", False) is True,
            max_pages=MAX_WEARABLE_SEARCH_PAGES,
        )
        return (
            _rows_with_page_provenance(
                collection.rows,
                page_index=0,
            ),
            collection.truncated,
            0,
            collection.completeness_unverified,
        )

    async def _provider_workout_detail(
        self,
        user_id: str,
        request: WearableSearchRequest,
    ) -> tuple[list[dict[str, Any]], bool, int]:
        row = await self._client.get_vendor_workout_detail(
            str(request.parameters["provider"]),
            user_id,
            str(request.parameters["workout_id"]),
            samples=request.parameters.get("samples", False) is True,
            zones=request.parameters.get("zones", False) is True,
            route=request.parameters.get("route", False) is True,
        )
        return _rows_with_page_provenance([row], page_index=0), False, 0

    async def _timeseries(
        self,
        user_id: str,
        request: WearableSearchRequest,
    ) -> tuple[list[dict[str, Any]], bool, int]:
        series_type = str(request.parameters["series_type"])
        resolution = str(request.parameters["resolution"])

        async def fetch(
            limit: int,
            cursor: str | None,
        ) -> Mapping[str, Any]:
            return await self._client.get_timeseries(
                user_id,
                request.start.astimezone(UTC).isoformat(),
                request.end.astimezone(UTC).isoformat(),
                [series_type],
                resolution=resolution,  # type: ignore[arg-type]
                cursor=cursor,
                limit=limit,
            )

        return await _collect_cursor_pages(fetch)


async def _collect_offset_pages(
    fetch: Callable[[int, int], Awaitable[Mapping[str, Any]]],
) -> tuple[list[dict[str, Any]], bool, int]:
    rows: list[dict[str, Any]] = []
    raw_rows = 0
    discarded_rows = 0
    offset = 0
    has_more = False
    for page_index in range(MAX_WEARABLE_SEARCH_PAGES):
        remaining = MAX_WEARABLE_SEARCH_ROWS + 1 - raw_rows
        if remaining <= 0:
            return rows, True, discarded_rows
        page_limit = min(_PAGE_SIZE, remaining)
        payload = await fetch(page_limit, offset)
        page = _response_page(payload, max_rows=page_limit)
        rows.extend(
            _rows_with_page_provenance(
                page.rows,
                page_index=page_index,
            )
        )
        raw_rows += page.raw_count
        discarded_rows += page.discarded_rows
        pagination = _response_pagination(payload)
        raw_has_more = pagination.get("has_more")
        if type(raw_has_more) is not bool:
            raise ValueError(
                "open-wearables returned invalid offset pagination"
            )
        has_more = raw_has_more
        if raw_rows > MAX_WEARABLE_SEARCH_ROWS:
            return rows, True, discarded_rows
        if not has_more:
            return rows, False, discarded_rows
        if page.raw_count == 0:
            return rows, True, discarded_rows
        offset += page.raw_count
    return rows, has_more, discarded_rows


async def _collect_cursor_pages(
    fetch: Callable[
        [int, str | None],
        Awaitable[Mapping[str, Any]],
    ],
) -> tuple[list[dict[str, Any]], bool, int]:
    rows: list[dict[str, Any]] = []
    raw_rows = 0
    discarded_rows = 0
    cursor: str | None = None
    seen_cursors: set[str] = set()
    has_more = False
    for page_index in range(MAX_WEARABLE_SEARCH_PAGES):
        remaining = MAX_WEARABLE_SEARCH_ROWS + 1 - raw_rows
        if remaining <= 0:
            return rows, True, discarded_rows
        page_limit = min(_PAGE_SIZE, remaining)
        payload = await fetch(page_limit, cursor)
        page = _response_page(payload, max_rows=page_limit)
        pagination = _response_pagination(payload)
        if (
            "next_cursor" not in pagination
            or "has_more" not in pagination
        ):
            raise ValueError(
                "open-wearables returned invalid cursor pagination"
            )
        raw_has_more = pagination["has_more"]
        if type(raw_has_more) is not bool:
            raise ValueError(
                "open-wearables returned invalid cursor pagination"
            )
        raw_cursor = pagination["next_cursor"]
        if raw_cursor is not None and (
            not isinstance(raw_cursor, str) or not raw_cursor
        ):
            raise ValueError(
                "open-wearables returned invalid cursor pagination"
            )
        next_cursor = raw_cursor
        if (raw_has_more and next_cursor is None) or (
            not raw_has_more and next_cursor is not None
        ):
            raise ValueError(
                "open-wearables returned contradictory cursor pagination"
            )
        rows.extend(
            _rows_with_page_provenance(
                page.rows,
                page_index=page_index,
            )
        )
        raw_rows += page.raw_count
        discarded_rows += page.discarded_rows
        if raw_rows > MAX_WEARABLE_SEARCH_ROWS:
            return rows, True, discarded_rows
        has_more = raw_has_more
        if has_more and (
            next_cursor is None
            or next_cursor == cursor
            or next_cursor in seen_cursors
        ):
            return rows, True, discarded_rows
        if not has_more:
            return rows, False, discarded_rows
        assert next_cursor is not None
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return rows, has_more, discarded_rows


def _response_page(
    payload: Mapping[str, Any],
    *,
    max_rows: int,
) -> _ProviderPage:
    values = payload.get("data")
    if not isinstance(values, list):
        raise ValueError("open-wearables returned an invalid page")
    if len(values) > max_rows:
        raise ValueError(
            "open-wearables page exceeded the requested row limit"
        )
    rows = tuple(
        dict(value) for value in values if isinstance(value, Mapping)
    )
    return _ProviderPage(
        rows=rows,
        raw_count=len(values),
        discarded_rows=len(values) - len(rows),
    )


def _response_pagination(
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    pagination = payload.get("pagination")
    if not isinstance(pagination, Mapping):
        raise ValueError(
            "open-wearables returned invalid pagination metadata"
        )
    return pagination


def _rows_with_page_provenance(
    rows: Sequence[Mapping[str, Any]],
    *,
    page_index: int,
) -> list[dict[str, Any]]:
    tagged: list[dict[str, Any]] = []
    for page_ordinal, row in enumerate(rows):
        value = dict(row)
        value[_INTERNAL_PAGE_INDEX] = page_index
        value[_INTERNAL_PAGE_ORDINAL] = page_ordinal
        tagged.append(value)
    return tagged


def _normalized_key(value: str) -> str:
    return "".join(
        character for character in value.casefold() if character.isalnum()
    )


def _contains_private_value(value: Any, private_value: str) -> bool:
    needle = private_value.casefold()
    if isinstance(value, Mapping):
        return any(
            _contains_private_value(item, private_value)
            for item in value.values()
        )
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return any(
            _contains_private_value(item, private_value)
            for item in value
        )
    if not isinstance(value, str):
        return False
    candidate = value.casefold()
    return candidate == needle or (
        len(needle) >= 8 and needle in candidate
    )


def _safe_text(value: Any, *, max_length: int = 128) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.strip().split())
    if not cleaned or len(cleaned) > max_length:
        return None
    return cleaned


def _number(value: Any, *, integer: bool = False) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if integer:
        return int(number)
    return int(number) if number.is_integer() else number


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _day(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _whoop_as_of(value: Any) -> date:
    if type(value) is not date:
        raise ValueError("WHOOP package as_of must be an exact local date")
    return value


def _provider_family(value: Any) -> str | None:
    cleaned = _safe_text(value, max_length=64)
    if cleaned is None:
        return None
    return _PROVIDER_FAMILY_ALIASES.get(_normalized_key(cleaned))


def canonical_wearable_provider(value: Any) -> str | None:
    """Return the canonical provider family used by wearable boundaries."""

    return _provider_family(value)


def _canonical_allowed_providers(
    values: tuple[str, ...] | frozenset[str] | None,
) -> frozenset[str] | None:
    if values is None:
        return None
    if not isinstance(values, tuple | frozenset):
        raise ValueError(
            "wearable allowed providers must be a tuple or frozenset"
        )
    canonical: set[str] = set()
    for value in values:
        provider = _provider_family(value)
        if provider is None:
            raise ValueError(
                "wearable allowed provider is not recognized"
            )
        canonical.add(provider)
    return frozenset(canonical)


def _request_with_execution_binding(
    request: WearableSearchRequest,
) -> WearableSearchRequest:
    binding = current_open_wearables_execution_binding(request.capability)
    if binding is None:
        return request
    lineage_mode = _effective_lineage_mode(
        request.capability,
        binding.lineage_mode,
    )
    source_allowlist = (
        binding.provider_source_direct_allowlist
        if lineage_mode
        is OpenWearablesLineageMode.PROVIDER_ROUTE_AUTHORITATIVE
        else binding.provider_source_allowlist
    )
    return replace(
        request,
        allowed_providers=binding.allowed_providers,
        provider_source_allowlist=source_allowlist,
        provider_source_identities=binding.provider_source_identities,
        lineage_mode=lineage_mode,
    )


def _source_lineage_is_allowed(
    row: Mapping[str, Any],
    *,
    provider: str,
    provider_source_allowlist: Mapping[str, frozenset[str]] | None,
    lineage_mode: OpenWearablesLineageMode | None = None,
    authoritative_provider: str | None = None,
    require_source: bool = False,
    allow_missing_source_id: bool = False,
) -> bool:
    if provider_source_allowlist is None:
        return not require_source
    data_source_id = _safe_text(
        row.get("data_source_id"),
        max_length=512,
    )
    allowed = provider_source_allowlist.get(provider)
    if allowed is None or not allowed:
        return False
    if data_source_id is None:
        if (
            lineage_mode
            is OpenWearablesLineageMode.PROVIDER_ROUTE_AUTHORITATIVE
            and authoritative_provider == provider
        ):
            return True
        return allow_missing_source_id is True
    return data_source_id in allowed


def _filter_whoop_source_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    provider_source_allowlist: Mapping[str, frozenset[str]] | None,
    provider_source_identities: Mapping[
        str,
        tuple[OpenWearablesExecutionDataSource, ...],
    ]
    | None,
    lineage_mode: OpenWearablesLineageMode | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    if provider_source_allowlist is None:
        return [dict(row) for row in rows], 0, len(rows)
    selected: list[dict[str, Any]] = []
    discarded = 0
    verified = 0
    for row in rows:
        provider, _attribution = _provider(row)
        if provider != WHOOP_UPSTREAM_PROVIDER:
            discarded += 1
            continue
        source_row = _bind_provider_source_identity(
            row,
            capability=WHOOP_RECOVERY_PACKAGE_CAPABILITY,
            provider_source_allowlist=provider_source_allowlist,
            provider_source_identities=provider_source_identities,
            lineage_mode=lineage_mode,
            authoritative_provider=WHOOP_UPSTREAM_PROVIDER,
        )
        if source_row is None or not _source_lineage_is_allowed(
            source_row,
            provider=WHOOP_UPSTREAM_PROVIDER,
            provider_source_allowlist=provider_source_allowlist,
            lineage_mode=lineage_mode,
            authoritative_provider=WHOOP_UPSTREAM_PROVIDER,
            require_source=True,
        ):
            discarded += 1
            continue
        selected.append(source_row)
        verified += 1
    return selected, discarded, verified


def _provider(row: Mapping[str, Any]) -> tuple[str, str]:
    declared = row.get("provider")
    if declared is not None:
        family = _provider_family(declared)
        return (
            (family, "declared")
            if family is not None
            else ("unknown", "declared_unclassified")
        )

    source = row.get("source")
    value = source.get("provider") if isinstance(source, Mapping) else None
    if value is None:
        return "unknown", "missing"
    family = _provider_family(value)
    return (
        (family, "source_exact_alias")
        if family is not None
        else ("unknown", "source_unclassified")
    )


def _bind_provider_source_identity(
    row: Mapping[str, Any],
    *,
    capability: str,
    provider_source_allowlist: Mapping[str, frozenset[str]] | None,
    provider_source_identities: Mapping[
        str,
        tuple[OpenWearablesExecutionDataSource, ...],
    ]
    | None,
    lineage_mode: OpenWearablesLineageMode | None = None,
    authoritative_provider: str | None = None,
) -> dict[str, Any] | None:
    """Bind one upstream row to the frozen provider/source contract.

    Explicit-row capabilities require an exact ``data_source_id``.  Direct
    provider-route capabilities may use the route itself as the authoritative
    source when the upstream row omits that ID; an explicit ID is still
    checked against the direct-source allowlist.
    """

    copied = dict(row)
    if provider_source_allowlist is None:
        return copied

    mode = _effective_lineage_mode(
        capability,
        lineage_mode,
    )
    declared_provider = _provider_family(row.get("provider"))
    source = row.get("source")
    source_provider = _provider_family(
        source.get("provider")
        if isinstance(source, Mapping)
        else None
    )
    if (
        declared_provider is not None
        and source_provider is not None
        and declared_provider != source_provider
    ):
        return None
    provider = declared_provider or source_provider
    authoritative = _provider_family(authoritative_provider)
    if mode is OpenWearablesLineageMode.PROVIDER_ROUTE_AUTHORITATIVE:
        if authoritative is None:
            return None
        if authoritative not in provider_source_allowlist:
            return None
        if provider is not None and provider != authoritative:
            return None
        provider = authoritative
        copied["provider"] = authoritative
        copied["provider_attribution"] = "allowed_provider_binding"
    elif provider is None:
        return None
    if provider is None:
        return None
    allowed = provider_source_allowlist.get(provider)
    if allowed is None or not allowed:
        return None

    explicit_ids, malformed = _explicit_row_source_ids(row)
    if malformed or len(explicit_ids) > 1:
        return None
    if explicit_ids:
        source_id = next(iter(explicit_ids))
        if source_id not in allowed:
            return None
        copied["data_source_id"] = source_id
        return copied

    if mode is OpenWearablesLineageMode.PROVIDER_ROUTE_AUTHORITATIVE:
        return copied
    return None


def _explicit_row_source_ids(
    row: Mapping[str, Any],
) -> tuple[set[str], bool]:
    """Read explicit IDs while rejecting conflicting or malformed values."""

    containers: list[Mapping[str, Any]] = [row]
    source = row.get("source")
    if isinstance(source, Mapping):
        containers.append(source)
    values: set[str] = set()
    malformed = False
    for container in containers:
        if "data_source_id" not in container:
            continue
        raw_value = container.get("data_source_id")
        if raw_value is None:
            continue
        value = _safe_text(raw_value, max_length=512)
        if value is None:
            malformed = True
            continue
        values.add(value)
    return values, malformed


def _row_device_labels(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Return exact device/source labels exposed by an upstream row."""

    labels: set[str] = set()
    fields = (
        "device",
        "device_model",
        "device_name",
        "display_name",
        "original_source_name",
    )
    for container in (row, row.get("source")):
        if not isinstance(container, Mapping):
            continue
        for field in fields:
            value = _safe_text(container.get(field), max_length=128)
            if value is not None:
                labels.add(value)
    return tuple(sorted(labels, key=lambda value: (value.casefold(), value)))


def _source_label_key(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _filter_provider_attribution(
    record: dict[str, Any],
    *,
    capability: str,
    source_row: Mapping[str, Any],
    allowed_providers: frozenset[str] | None,
    body_summary: bool = False,
    provider_source_allowlist: Mapping[str, frozenset[str]] | None = None,
    lineage_mode: OpenWearablesLineageMode | None = None,
    authoritative_provider: str | None = None,
    allow_missing_source_id: bool = False,
) -> dict[str, Any] | None:
    effective_lineage_mode = _effective_lineage_mode(
        capability,
        lineage_mode,
    )
    if allowed_providers is None:
        return record
    if body_summary:
        if len(allowed_providers) != 1:
            return None
        allowed_provider = next(iter(allowed_providers))
        declared_raw = source_row.get("provider")
        if declared_raw == "unknown":
            declared_raw = None
        declared = _provider_family(declared_raw)
        source = source_row.get("source")
        source_raw = (
            source.get("provider")
            if isinstance(source, Mapping)
            else None
        )
        if source_raw == "unknown":
            source_raw = None
        source_provider = _provider_family(source_raw)
        if (
            declared_raw is not None
            and declared is None
            or source_raw is not None
            and source_provider is None
            or declared is not None
            and source_provider is not None
            and declared != source_provider
            or declared is not None
            and declared != allowed_provider
            or source_provider is not None
            and source_provider != allowed_provider
        ):
            return None
        if not _source_lineage_is_allowed(
            source_row,
            provider=allowed_provider,
            provider_source_allowlist=provider_source_allowlist,
            lineage_mode=effective_lineage_mode,
            authoritative_provider=authoritative_provider,
            allow_missing_source_id=allow_missing_source_id,
        ):
            return None
        record["provider"] = allowed_provider
        record["provider_attribution"] = "allowed_provider_binding"
        return record

    provider = _provider_family(record.get("provider"))
    attribution = _safe_text(
        record.get("provider_attribution"),
        max_length=32,
    )
    if (
        provider is None
        or attribution not in _TRUSTED_PROVIDER_ATTRIBUTIONS
        or provider not in allowed_providers
    ):
        return None

    declared = _provider_family(source_row.get("provider"))
    source = source_row.get("source")
    source_provider = _provider_family(
        source.get("provider")
        if isinstance(source, Mapping)
        else None
    )
    if (
        declared is not None
        and source_provider is not None
        and declared != source_provider
    ):
        return None
    if not _source_lineage_is_allowed(
        source_row,
        provider=provider,
        provider_source_allowlist=provider_source_allowlist,
        lineage_mode=effective_lineage_mode,
        authoritative_provider=authoritative_provider,
        allow_missing_source_id=allow_missing_source_id,
    ):
        return None
    record["provider"] = provider
    return record


def _effective_lineage_mode(
    capability: str,
    lineage_mode: OpenWearablesLineageMode | None,
) -> OpenWearablesLineageMode | None:
    """Resolve legacy requests while keeping the catalog contract strict."""

    if lineage_mode is not None:
        return lineage_mode
    normalized = capability.strip().casefold()
    if normalized in {
        "wearable.health-scores",
        WHOOP_RECOVERY_PACKAGE_CAPABILITY,
    }:
        return OpenWearablesLineageMode.EXPLICIT_ROW_SOURCE_ID
    if normalized in {
        "wearable.provider-workouts",
        "wearable.provider-workout-detail",
    }:
        return OpenWearablesLineageMode.PROVIDER_ROUTE_AUTHORITATIVE
    return None


def _request_provider(request: WearableSearchRequest) -> str | None:
    """Return the single provider selected by a provider-owned request."""

    raw = request.parameters.get("provider")
    if raw is not None:
        return _provider_family(raw)
    allowed = _canonical_allowed_providers(request.allowed_providers)
    if allowed is not None and len(allowed) == 1:
        return next(iter(allowed))
    return None


def _lineage_response_attested(
    request: WearableSearchRequest,
    *,
    upstream_rows_present: bool,
    lineage_verified_rows: int,
    lineage_rejected_rows: int = 0,
    component_attested: bool = True,
) -> bool:
    """Return whether the bounded adapter proved its provider boundary.

    An empty upstream response is an attested no-data result.  A non-empty
    response requires at least one source-verified row.  If every row was
    rejected, the adapter observed data but could not prove that any row
    belonged to the frozen binding, so it must not attest normal no-data.
    """

    if request.provider_source_allowlist is None:
        return False
    if not component_attested:
        return False
    if not upstream_rows_present:
        return True
    # A response that mixes rows from the frozen source with rows that cannot
    # be attributed is not a complete source-attested response.  Returning
    # only the accepted subset would make coverage look stronger than the
    # provider boundary actually proves.
    return (
        lineage_verified_rows > 0
        and lineage_rejected_rows == 0
    )


def _trusted_stream_key(row: Mapping[str, Any]) -> str | None:
    data_source_id = _safe_text(
        row.get("data_source_id"),
        max_length=512,
    )
    if data_source_id is None:
        return None
    encoded = json.dumps(
        {"data_source_id": data_source_id},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _provider_identity_token(row: Mapping[str, Any]) -> str:
    family, _attribution = _provider(row)
    if family != "unknown":
        identity: Mapping[str, Any] = {
            "kind": "canonical-family",
            "family": family,
        }
        return hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

    declared = _safe_text(row.get("provider"), max_length=512)
    source = row.get("source")
    source_provider = (
        _safe_text(source.get("provider"), max_length=512)
        if isinstance(source, Mapping)
        else None
    )
    encoded = json.dumps(
        {
            "kind": "unclassified-provider",
            "declared_provider": (
                _normalized_key(declared) if declared is not None else None
            ),
            "source_provider": (
                _normalized_key(source_provider)
                if source_provider is not None
                else None
            ),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _provider_row_identifier(
    row: Mapping[str, Any],
) -> tuple[str, str] | None:
    for field in _PROVIDER_ROW_ID_FIELDS:
        value = _safe_text(row.get(field), max_length=512)
        if value is not None:
            return field, value
    return None


def _structural_row_locator(
    record: Mapping[str, Any],
) -> dict[str, Any]:
    if "category" in record and "recorded_at" in record:
        return {
            "category": record.get("category"),
            "recorded_at": record.get("recorded_at"),
        }
    if "summary_kind" in record:
        return {
            "summary_kind": record.get("summary_kind"),
            "date": record.get("date"),
        }
    if record.get("record_kind") == "body_summary":
        return {
            "record_kind": "body_summary",
            "summary_collected_at": record.get("summary_collected_at"),
        }
    if "workout_type" in record:
        return {
            "workout_type": record.get("workout_type"),
            "start_time": record.get("start_time"),
            "end_time": record.get("end_time"),
        }
    if "start_time" in record or "end_time" in record:
        return {
            "record_kind": record.get("record_kind"),
            "start_time": record.get("start_time"),
            "end_time": record.get("end_time"),
        }
    return {
        "series_type": record.get("series_type"),
        "timestamp": record.get("timestamp"),
        "unit": record.get("unit"),
        "is_daily_total": record.get("is_daily_total") is True,
    }


def _row_identity(
    row: Mapping[str, Any],
    record: Mapping[str, Any],
) -> str:
    provider_token = _provider_identity_token(row)
    provider_row_id = _provider_row_identifier(row)
    structural = _structural_row_locator(record)
    if provider_row_id is not None:
        identity: Mapping[str, Any] = {
            "kind": "provider-row",
            "provider": provider_token,
            "field": provider_row_id[0],
            "value": provider_row_id[1],
        }
    else:
        stream_key = _trusted_stream_key(row)
        identity = {
            "kind": (
                "provider-stream"
                if stream_key is not None
                else "structural"
            ),
            "provider": provider_token,
            "stream": stream_key,
            "locator": structural,
        }
    return hashlib.sha256(
        json.dumps(
            identity,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _with_row_identity(
    record: dict[str, Any],
    *,
    row: Mapping[str, Any],
) -> dict[str, Any]:
    record[_INTERNAL_ROW_IDENTITY] = _row_identity(row, record)
    record[_INTERNAL_STABLE_ROW_ID] = (
        _provider_row_identifier(row) is not None
    )
    for field in (_INTERNAL_PAGE_INDEX, _INTERNAL_PAGE_ORDINAL):
        value = row.get(field)
        if type(value) is int and value >= 0:
            record[field] = value
    return record


def _observation_is_retained(
    observation: datetime,
    *,
    retained_after: datetime | None,
) -> bool:
    return (
        retained_after is None
        or observation.astimezone(UTC) > retained_after.astimezone(UTC)
    )


def _put_number(
    target: dict[str, Any],
    source: Mapping[str, Any],
    field: str,
    *,
    integer: bool = False,
) -> None:
    value = _number(source.get(field), integer=integer)
    if value is not None:
        target[field] = value


def _nonnegative_number(
    value: Any,
    *,
    integer: bool = False,
) -> int | float | None:
    number = _number(value, integer=integer)
    return number if number is not None and number >= 0 else None


def _put_nonnegative_number(
    target: dict[str, Any],
    source: Mapping[str, Any],
    field: str,
    *,
    integer: bool = False,
) -> None:
    value = _nonnegative_number(source.get(field), integer=integer)
    if value is not None:
        target[field] = value


def _normalized_label(value: Any, *, max_length: int = 64) -> str | None:
    cleaned = _safe_text(value, max_length=max_length)
    if cleaned is None:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "_", cleaned.casefold()).strip("_")
    return normalized or None


def _safe_identifier(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    return (
        value
        if isinstance(value, str)
        and _SAFE_PROVIDER_WORKOUT_ID.fullmatch(value) is not None
        else None
    )


def _epoch_timestamp(
    value: Any,
    *,
    milliseconds: bool,
) -> datetime | None:
    number = _number(value)
    if number is None:
        return None
    seconds = float(number) / (1_000 if milliseconds else 1)
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _iso_duration_seconds(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    matched = _ISO_DURATION.fullmatch(value.strip())
    if matched is None or not any(matched.groupdict().values()):
        return None
    seconds = sum(
        float(matched.group(name) or 0) * multiplier
        for name, multiplier in (
            ("days", 86_400),
            ("hours", 3_600),
            ("minutes", 60),
            ("seconds", 1),
        )
    )
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _offset_string(total_minutes: Any) -> str | None:
    minutes = _number(total_minutes, integer=True)
    if minutes is None or not -1_439 <= minutes <= 1_439:
        return None
    sign = "+" if minutes >= 0 else "-"
    absolute = abs(minutes)
    return f"{sign}{absolute // 60:02d}:{absolute % 60:02d}"


def _polar_start_time(
    value: Any,
    *,
    offset_minutes: Any,
) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        return parsed.astimezone(UTC)
    minutes = _number(offset_minutes, integer=True)
    if minutes is None or not -1_439 <= minutes <= 1_439:
        return None
    return parsed.replace(
        tzinfo=timezone(timedelta(minutes=minutes))
    ).astimezone(UTC)


def _sanitize_body_summary(
    row: Mapping[str, Any],
    *,
    start: datetime,
    end: datetime,
    retained_after: datetime | None,
    collected_at: datetime,
    average_period: int,
    latest_window_hours: int,
) -> dict[str, Any] | None:
    slow = row.get("slow_changing")
    averaged = row.get("averaged")
    latest = row.get("latest")
    query_start = start.astimezone(UTC)
    query_end = end.astimezone(UTC)
    collected = collected_at.astimezone(UTC)
    if (
        not query_start <= collected < query_end
        or not _observation_is_retained(
            collected,
            retained_after=retained_after,
        )
    ):
        return None
    stored_collected_at = row.get("summary_collected_at")
    if stored_collected_at is not None:
        if _timestamp(stored_collected_at) != collected:
            return None

    provider, attribution = _provider(row)
    result: dict[str, Any] = {
        "record_kind": "body_summary",
        "summary_collected_at": collected.isoformat(),
        "provider": provider,
        "provider_attribution": attribution,
    }

    clean_slow: dict[str, Any] = {}
    if (
        isinstance(slow, Mapping)
    ):
        for field in (
            "weight_kg",
            "height_cm",
            "body_fat_percent",
            "muscle_mass_kg",
            "bmi",
        ):
            _put_nonnegative_number(clean_slow, slow, field)
        _put_nonnegative_number(clean_slow, slow, "age", integer=True)
    if clean_slow:
        result["slow_changing"] = clean_slow

    clean_averaged: dict[str, Any] = {}
    period_start = (
        _timestamp(averaged.get("period_start"))
        if isinstance(averaged, Mapping)
        else None
    )
    period_end = (
        _timestamp(averaged.get("period_end"))
        if isinstance(averaged, Mapping)
        else None
    )
    period_days = (
        _number(averaged.get("period_days"), integer=True)
        if isinstance(averaged, Mapping)
        else None
    )
    if (
        isinstance(averaged, Mapping)
        and period_start is not None
        and period_end is not None
        and period_start < period_end
        and period_days == average_period
        and query_start <= period_end < query_end
        and period_end <= collected + _MAX_COLLECTION_CLOCK_SKEW
        and _observation_is_retained(
            period_start,
            retained_after=retained_after,
        )
    ):
        _put_nonnegative_number(
            clean_averaged,
            averaged,
            "resting_heart_rate_bpm",
            integer=True,
        )
        for field in ("avg_hrv_sdnn_ms", "avg_hrv_rmssd_ms"):
            _put_nonnegative_number(clean_averaged, averaged, field)
    if clean_averaged:
        assert period_start is not None
        assert period_end is not None
        clean_averaged.update(
            {
                "period_days": period_days,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
            }
        )
        result["averaged"] = clean_averaged

    clean_latest: dict[str, Any] = {}
    if isinstance(latest, Mapping):
        for value_field, measured_field in (
            (
                "body_temperature_celsius",
                "body_temperature_measured_at",
            ),
            (
                "skin_temperature_celsius",
                "skin_temperature_measured_at",
            ),
        ):
            value = _number(latest.get(value_field))
            measured_at = _timestamp(latest.get(measured_field))
            if (
                value is not None
                and measured_at is not None
                and query_start <= measured_at < query_end
                and collected - timedelta(
                    hours=latest_window_hours
                )
                <= measured_at
                <= collected + _MAX_COLLECTION_CLOCK_SKEW
                and _observation_is_retained(
                    measured_at,
                    retained_after=retained_after,
                )
            ):
                clean_latest[value_field] = value
                clean_latest[measured_field] = measured_at.isoformat()

    pressure = (
        latest.get("blood_pressure")
        if isinstance(latest, Mapping)
        else None
    )
    pressure_at = (
        _timestamp(latest.get("blood_pressure_measured_at"))
        if isinstance(latest, Mapping)
        else None
    )
    if (
        isinstance(pressure, Mapping)
        and pressure_at is not None
        and query_start <= pressure_at < query_end
        and collected - timedelta(hours=latest_window_hours)
        <= pressure_at
        <= collected + _MAX_COLLECTION_CLOCK_SKEW
        and _observation_is_retained(
            pressure_at,
            retained_after=retained_after,
        )
    ):
        clean_pressure: dict[str, Any] = {}
        for field in (
            "avg_systolic_mmhg",
            "avg_diastolic_mmhg",
            "max_systolic_mmhg",
            "max_diastolic_mmhg",
            "min_systolic_mmhg",
            "min_diastolic_mmhg",
            "reading_count",
        ):
            _put_nonnegative_number(
                clean_pressure,
                pressure,
                field,
                integer=True,
            )
        if clean_pressure:
            clean_latest["blood_pressure"] = clean_pressure
            clean_latest["blood_pressure_measured_at"] = (
                pressure_at.isoformat()
            )
    if clean_latest:
        result["latest"] = clean_latest

    if not any(
        key in result for key in ("slow_changing", "averaged", "latest")
    ):
        return None
    return _with_row_identity(result, row=row)


def _sanitize_sleep_stage_intervals(
    value: Any,
    *,
    session_start: datetime,
    session_end: datetime,
) -> tuple[list[dict[str, Any]], bool]:
    if value is None:
        return [], False
    if not isinstance(value, list):
        return [], True

    candidates: list[dict[str, Any]] = []
    truncated = len(value) > _MAX_SLEEP_STAGE_INTERVALS
    for raw in value[:_MAX_SLEEP_STAGE_INTERVALS]:
        if not isinstance(raw, Mapping):
            truncated = True
            continue
        stage = _safe_text(raw.get("stage"), max_length=16)
        interval_start = _timestamp(raw.get("start_time"))
        interval_end = _timestamp(raw.get("end_time"))
        if (
            stage not in _SLEEP_STAGE_TYPES
            or interval_start is None
            or interval_end is None
            or not session_start <= interval_start < interval_end <= session_end
        ):
            truncated = True
            continue
        candidates.append(
            {
                "stage": stage,
                "start_time": interval_start.isoformat(),
                "end_time": interval_end.isoformat(),
            }
        )

    candidates.sort(
        key=lambda item: (item["start_time"], item["end_time"], item["stage"])
    )
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    last_end: datetime | None = None
    for item in candidates:
        identity = (
            str(item["stage"]),
            str(item["start_time"]),
            str(item["end_time"]),
        )
        if identity in seen:
            continue
        interval_start = _timestamp(item["start_time"])
        interval_end = _timestamp(item["end_time"])
        assert interval_start is not None
        assert interval_end is not None
        if last_end is not None and interval_start < last_end:
            truncated = True
            continue
        seen.add(identity)
        selected.append(item)
        last_end = interval_end
    return selected, truncated


def _sanitize_sleep_session(
    row: Mapping[str, Any],
    *,
    start: datetime,
    end: datetime,
    retained_after: datetime | None,
    privacy_level: PrivacyLevel,
) -> dict[str, Any] | None:
    start_time = _timestamp(row.get("start_time"))
    end_time = _timestamp(row.get("end_time"))
    if not _workout_interval_is_allowed(
        start_time,
        end_time,
        start=start,
        end=end,
        retained_after=retained_after,
    ):
        return None
    assert start_time is not None
    assert end_time is not None
    provider, attribution = _provider(row)
    result: dict[str, Any] = {
        "record_kind": "sleep_session",
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "provider": provider,
        "provider_attribution": attribution,
    }
    for field in ("duration_seconds", "sleep_duration_seconds"):
        _put_nonnegative_number(result, row, field, integer=True)
    efficiency = _number(row.get("efficiency_percent"))
    if efficiency is not None and 0 <= efficiency <= 100:
        result["efficiency_percent"] = efficiency
    if type(row.get("is_nap")) is bool:
        result["is_nap"] = row["is_nap"]
    zone_offset = _safe_text(row.get("zone_offset"), max_length=16)
    if zone_offset is not None:
        result["zone_offset"] = zone_offset
    stages = row.get("stages")
    if isinstance(stages, Mapping):
        clean_stages: dict[str, Any] = {}
        for field in (
            "awake_minutes",
            "light_minutes",
            "deep_minutes",
            "rem_minutes",
        ):
            _put_nonnegative_number(
                clean_stages,
                stages,
                field,
                integer=True,
            )
        if clean_stages:
            result["stages"] = clean_stages
    if privacy_level is PrivacyLevel.IDENTITY:
        intervals, intervals_truncated = _sanitize_sleep_stage_intervals(
            row.get("sleep_stage_intervals"),
            session_start=start_time,
            session_end=end_time,
        )
        if intervals:
            result["sleep_stage_intervals"] = intervals
        if (
            intervals_truncated
            or row.get("sleep_stage_intervals_truncated") is True
        ):
            result["sleep_stage_intervals_truncated"] = True
    return _with_row_identity(result, row=row)


def _sanitize_menstrual_cycle(
    row: Mapping[str, Any],
    *,
    start: datetime,
    end: datetime,
    retained_after: datetime | None,
) -> dict[str, Any] | None:
    start_time = _timestamp(row.get("start_time"))
    end_time = _timestamp(row.get("end_time"))
    if (
        start_time is None
        or end_time is None
        or end_time <= start_time
        or not start.astimezone(UTC)
        <= start_time
        < end.astimezone(UTC)
        or not _observation_is_retained(
            start_time,
            retained_after=retained_after,
        )
    ):
        return None
    provider, attribution = _provider(row)
    result: dict[str, Any] = {
        "record_kind": "menstrual_cycle",
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "provider": provider,
        "provider_attribution": attribution,
    }
    zone_offset = _safe_text(row.get("zone_offset"), max_length=16)
    if zone_offset is not None:
        result["zone_offset"] = zone_offset
    for field in (
        "current_phase",
        "day_in_cycle",
        "cycle_length",
        "predicted_cycle_length",
        "period_length",
        "length_of_current_phase",
        "days_until_next_phase",
        "fertile_window_start",
        "length_of_fertile_window",
    ):
        _put_nonnegative_number(result, row, field, integer=True)
    phase_type = _safe_text(
        row.get("current_phase_type"),
        max_length=64,
    )
    if phase_type is not None:
        result["current_phase_type"] = phase_type
    for field in (
        "is_predicted_cycle",
        "has_specified_cycle_length",
        "has_specified_period_length",
    ):
        if type(row.get(field)) is bool:
            result[field] = row[field]
    last_updated_at = _timestamp(row.get("last_updated_at"))
    if last_updated_at is not None:
        result["last_updated_at"] = last_updated_at.isoformat()
    return _with_row_identity(result, row=row)


def _provider_workout_source(
    row: Mapping[str, Any],
    *,
    provider: str,
) -> Mapping[str, Any]:
    if provider == "suunto":
        payload = row.get("payload")
        if isinstance(payload, Mapping):
            return payload
    if provider == "garmin":
        summary = row.get("summary")
        if isinstance(summary, Mapping):
            return {**row, **summary}
    return row


def _provider_workout_interval(
    row: Mapping[str, Any],
    *,
    provider: str,
) -> tuple[datetime | None, datetime | None, int | float | None]:
    if provider == "garmin":
        start_time = _epoch_timestamp(
            row.get("startTimeInSeconds"),
            milliseconds=False,
        )
        duration = _nonnegative_number(
            row.get("durationInSeconds"),
            integer=True,
        )
    elif provider == "polar":
        start_time = _polar_start_time(
            row.get("start_time"),
            offset_minutes=row.get("start_time_utc_offset"),
        )
        duration = _iso_duration_seconds(row.get("duration"))
    else:
        start_time = _epoch_timestamp(
            row.get("startTime"),
            milliseconds=True,
        )
        duration = _nonnegative_number(row.get("totalTime"))

    end_time: datetime | None = None
    if provider == "suunto":
        end_time = _epoch_timestamp(
            row.get("stopTime"),
            milliseconds=True,
        )
    if end_time is None and start_time is not None and duration is not None:
        end_time = start_time + timedelta(seconds=float(duration))
    return start_time, end_time, duration


def _provider_workout_ids(
    row: Mapping[str, Any],
    *,
    provider: str,
) -> tuple[str | None, str | None]:
    if provider == "garmin":
        return _safe_identifier(row.get("activityId")), None
    if provider == "polar":
        return _safe_identifier(row.get("id")), None
    return (
        _safe_identifier(row.get("workoutKey")),
        _safe_identifier(row.get("workoutId")),
    )


def _provider_workout_type(
    row: Mapping[str, Any],
    *,
    provider: str,
) -> tuple[str | None, int | str | None]:
    if provider == "garmin":
        raw = row.get("activityType")
        return _normalized_label(raw), _safe_text(raw, max_length=64)
    if provider == "polar":
        raw = row.get("detailed_sport_info") or row.get("sport")
        return _normalized_label(raw), _safe_text(raw, max_length=64)
    code = _nonnegative_number(row.get("activityId"), integer=True)
    return (
        f"suunto_activity_{code}" if code is not None else None,
        code,
    )


def _provider_workout_zone_offset(
    row: Mapping[str, Any],
    *,
    provider: str,
) -> str | None:
    if provider == "garmin":
        seconds = _number(
            row.get("startTimeOffsetInSeconds"),
            integer=True,
        )
        return (
            _offset_string(seconds // 60)
            if seconds is not None and seconds % 60 == 0
            else None
        )
    if provider == "polar":
        return _offset_string(row.get("start_time_utc_offset"))
    return _offset_string(row.get("timeOffsetInMinutes"))


def _first_number(
    *values: Any,
    integer: bool = False,
    nonnegative: bool = True,
) -> int | float | None:
    for value in values:
        number = (
            _nonnegative_number(value, integer=integer)
            if nonnegative
            else _number(value, integer=integer)
        )
        if number is not None:
            return number
    return None


def _sanitize_garmin_samples(
    row: Mapping[str, Any],
    *,
    start_time: datetime,
    end_time: datetime,
    include_location: bool,
) -> tuple[list[dict[str, Any]], bool]:
    raw_samples = row.get("samples")
    if not isinstance(raw_samples, list):
        return [], False
    selected: list[dict[str, Any]] = []
    truncated = len(raw_samples) > _MAX_GRANULAR_WORKOUT_ROWS
    for raw in raw_samples[:_MAX_GRANULAR_WORKOUT_ROWS]:
        if not isinstance(raw, Mapping):
            truncated = True
            continue
        timestamp = _epoch_timestamp(
            raw.get("startTimeInSeconds"),
            milliseconds=False,
        )
        if timestamp is None or not start_time <= timestamp <= end_time:
            truncated = True
            continue
        sample: dict[str, Any] = {"timestamp": timestamp.isoformat()}
        for source_field, public_field in (
            ("heartRate", "heart_rate_bpm"),
            ("speedMetersPerSecond", "speed_meters_per_second"),
            ("stepsPerMinute", "cadence_steps_per_minute"),
            ("powerInWatts", "power_watts"),
            ("elevationInMeters", "elevation_meters"),
            ("airTemperatureCelcius", "air_temperature_celsius"),
        ):
            value = _number(raw.get(source_field))
            if value is not None:
                sample[public_field] = value
        if include_location:
            latitude = _number(raw.get("latitudeInDegree"))
            longitude = _number(raw.get("longitudeInDegree"))
            if (
                latitude is not None
                and longitude is not None
                and -90 <= latitude <= 90
                and -180 <= longitude <= 180
            ):
                sample["latitude"] = latitude
                sample["longitude"] = longitude
        if len(sample) > 1:
            selected.append(sample)
    return selected, truncated


def _sanitize_polar_samples(
    row: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    raw_samples = row.get("samples")
    if not isinstance(raw_samples, list):
        return [], False
    selected: list[dict[str, Any]] = []
    truncated = len(raw_samples) > _MAX_GRANULAR_WORKOUT_ROWS
    for raw in raw_samples[:_MAX_GRANULAR_WORKOUT_ROWS]:
        if not isinstance(raw, Mapping):
            truncated = True
            continue
        recording_rate = _nonnegative_number(
            raw.get("recording-rate"),
            integer=True,
        )
        sample_type = _safe_text(
            raw.get("sample-type"),
            max_length=64,
        )
        data = _safe_text(
            raw.get("data"),
            max_length=_MAX_GRANULAR_WORKOUT_TEXT,
        )
        if recording_rate is None or sample_type is None or data is None:
            truncated = True
            continue
        selected.append(
            {
                "recording_rate_seconds": recording_rate,
                "sample_type": sample_type,
                "data": data,
            }
        )
    return selected, truncated


def _sanitize_polar_zones(
    row: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    raw_zones = row.get("heart_rate_zones")
    if not isinstance(raw_zones, list):
        return [], False
    selected: list[dict[str, Any]] = []
    truncated = len(raw_zones) > _MAX_GRANULAR_WORKOUT_ROWS
    for raw in raw_zones[:_MAX_GRANULAR_WORKOUT_ROWS]:
        if not isinstance(raw, Mapping):
            truncated = True
            continue
        index = _nonnegative_number(raw.get("index"), integer=True)
        lower = _nonnegative_number(
            raw.get("lower-limit"),
            integer=True,
        )
        upper = _nonnegative_number(
            raw.get("upper-limit"),
            integer=True,
        )
        duration = _iso_duration_seconds(raw.get("in-zone"))
        if (
            index is None
            or lower is None
            or upper is None
            or lower > upper
            or duration is None
        ):
            truncated = True
            continue
        selected.append(
            {
                "index": index,
                "lower_bpm": lower,
                "upper_bpm": upper,
                "duration_seconds": duration,
            }
        )
    return selected, truncated


def _sanitize_polar_route(
    row: Mapping[str, Any],
    *,
    start_time: datetime,
    end_time: datetime,
) -> tuple[list[dict[str, Any]], bool]:
    raw_route = row.get("route")
    if not isinstance(raw_route, list):
        return [], False
    selected: list[dict[str, Any]] = []
    truncated = len(raw_route) > _MAX_GRANULAR_WORKOUT_ROWS
    for raw in raw_route[:_MAX_GRANULAR_WORKOUT_ROWS]:
        if not isinstance(raw, Mapping):
            truncated = True
            continue
        latitude = _number(raw.get("latitude"))
        longitude = _number(raw.get("longitude"))
        timestamp = _timestamp(raw.get("time"))
        if (
            latitude is None
            or longitude is None
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
            or (
                timestamp is not None
                and not start_time <= timestamp <= end_time
            )
        ):
            truncated = True
            continue
        point: dict[str, Any] = {
            "latitude": latitude,
            "longitude": longitude,
        }
        if timestamp is not None:
            point["timestamp"] = timestamp.isoformat()
        for field in ("satellites", "fix"):
            value = _nonnegative_number(raw.get(field), integer=True)
            if value is not None:
                point[field] = value
        selected.append(point)
    return selected, truncated


def _sanitize_provider_workout(
    row: Mapping[str, Any],
    *,
    provider: str,
    expected_workout_id: str | None,
    start: datetime,
    end: datetime,
    retained_after: datetime | None,
    samples: bool,
    zones: bool,
    route: bool,
) -> dict[str, Any] | None:
    source = _provider_workout_source(row, provider=provider)
    workout_id, numeric_workout_id = _provider_workout_ids(
        source,
        provider=provider,
    )
    if expected_workout_id is not None and workout_id != expected_workout_id:
        return None
    if provider != "suunto" and workout_id is None:
        return None
    if provider == "suunto" and workout_id is None and numeric_workout_id is None:
        return None

    start_time, end_time, duration = _provider_workout_interval(
        source,
        provider=provider,
    )
    if not _workout_interval_is_allowed(
        start_time,
        end_time,
        start=start,
        end=end,
        retained_after=retained_after,
    ):
        return None
    assert start_time is not None
    assert end_time is not None
    workout_type, workout_type_code = _provider_workout_type(
        source,
        provider=provider,
    )
    if workout_type is None:
        return None

    result: dict[str, Any] = {
        "record_kind": "provider_workout",
        "provider": provider,
        "provider_attribution": "declared",
        "workout_type": workout_type,
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
    }
    if workout_id is not None:
        result["provider_workout_id"] = workout_id
    if numeric_workout_id is not None:
        result["provider_numeric_workout_id"] = numeric_workout_id
    if workout_type_code is not None:
        result["provider_workout_type_code"] = workout_type_code
    if duration is not None:
        result["duration_seconds"] = (
            int(duration)
            if float(duration).is_integer()
            else duration
        )
    zone_offset = _provider_workout_zone_offset(
        source,
        provider=provider,
    )
    if zone_offset is not None:
        result["zone_offset"] = zone_offset

    heart_rate = source.get("heart_rate")
    heart_rate = heart_rate if isinstance(heart_rate, Mapping) else {}
    hrdata = source.get("hrdata")
    hrdata = hrdata if isinstance(hrdata, Mapping) else {}
    metric_values = {
        "distance_meters": _first_number(
            source.get(
                "distanceInMeters"
                if provider == "garmin"
                else "distance"
                if provider == "polar"
                else "totalDistance"
            )
        ),
        "calories_kcal": _first_number(
            source.get(
                "activeKilocalories"
                if provider == "garmin"
                else "calories"
                if provider == "polar"
                else "energyConsumption"
            )
        ),
        "steps": _first_number(
            source.get("steps" if provider == "garmin" else "stepCount"),
            integer=True,
        ),
        "avg_heart_rate_bpm": _first_number(
            source.get("averageHeartRateInBeatsPerMinute"),
            heart_rate.get("average"),
            hrdata.get("avg"),
            hrdata.get("workoutAvgHR"),
        ),
        "max_heart_rate_bpm": _first_number(
            source.get("maxHeartRateInBeatsPerMinute"),
            heart_rate.get("maximum"),
            hrdata.get("hrmax"),
            hrdata.get("workoutMaxHR"),
        ),
        "min_heart_rate_bpm": _first_number(hrdata.get("min")),
        "avg_speed_meters_per_second": _first_number(
            source.get("averageSpeedInMetersPerSecond"),
            source.get("avgSpeed"),
        ),
        "max_speed_meters_per_second": _first_number(
            source.get("maxSpeed")
        ),
        "avg_power_watts": _first_number(source.get("avgPower")),
        "max_power_watts": _first_number(source.get("maxPower")),
        "avg_cadence": _first_number(
            source.get("averageRunCadenceInStepsPerMinute"),
            source.get("averageBikingCadenceInRevPerMinute"),
            source.get("averageSwimCadenceInStrokesPerMinute"),
            source.get("averageCadence"),
            source.get("avgCadence"),
        ),
        "max_cadence": _first_number(source.get("maxCadence")),
        "elevation_gain_meters": _first_number(
            source.get("elevationGainInMeters"),
            source.get("totalAscent"),
        ),
        "elevation_loss_meters": _first_number(
            source.get("totalDescent")
        ),
        "max_altitude_meters": _first_number(
            source.get("maxAltitude")
        ),
        "min_altitude_meters": _first_number(
            source.get("minAltitude")
        ),
        "training_load": _first_number(source.get("training_load")),
    }
    for field, value in metric_values.items():
        if value is not None:
            result[field] = value
    if type(source.get("has_route")) is bool:
        result["route_available"] = source["has_route"]

    granular_truncated = False
    if samples:
        clean_samples, was_truncated = (
            _sanitize_garmin_samples(
                source,
                start_time=start_time,
                end_time=end_time,
                include_location=route,
            )
            if provider == "garmin"
            else _sanitize_polar_samples(source)
            if provider == "polar"
            else ([], False)
        )
        if clean_samples:
            result["samples"] = clean_samples
        granular_truncated |= was_truncated
    if zones and provider == "polar":
        clean_zones, was_truncated = _sanitize_polar_zones(source)
        if clean_zones:
            result["heart_rate_zones"] = clean_zones
        granular_truncated |= was_truncated
    if route and provider == "polar":
        clean_route, was_truncated = _sanitize_polar_route(
            source,
            start_time=start_time,
            end_time=end_time,
        )
        if clean_route:
            result["route"] = clean_route
        granular_truncated |= was_truncated
    if granular_truncated:
        result["granular_truncated"] = True

    identity_row = {
        **source,
        "provider": provider,
    }
    if workout_id is not None:
        identity_row["provider_workout_id"] = workout_id
    if numeric_workout_id is not None:
        identity_row["provider_numeric_workout_id"] = numeric_workout_id
    return _with_row_identity(result, row=identity_row)


def _sanitize_retained_provider_workout(
    row: Mapping[str, Any],
    *,
    provider: str,
    expected_workout_id: str | None,
    start: datetime,
    end: datetime,
    retained_after: datetime | None,
    samples: bool,
    zones: bool,
    route: bool,
) -> dict[str, Any] | None:
    if (
        row.get("record_kind") != "provider_workout"
        or row.get("provider") != provider
    ):
        return None
    workout_id = _safe_identifier(row.get("provider_workout_id"))
    numeric_workout_id = _safe_identifier(
        row.get("provider_numeric_workout_id")
    )
    if expected_workout_id is not None and workout_id != expected_workout_id:
        return None
    start_time = _timestamp(row.get("start_time"))
    end_time = _timestamp(row.get("end_time"))
    if not _workout_interval_is_allowed(
        start_time,
        end_time,
        start=start,
        end=end,
        retained_after=retained_after,
    ):
        return None
    workout_type = _safe_text(row.get("workout_type"), max_length=64)
    if workout_type is None:
        return None

    allowed = {
        "record_kind",
        "provider",
        "provider_attribution",
        "provider_workout_id",
        "provider_numeric_workout_id",
        "provider_workout_type_code",
        "workout_type",
        "start_time",
        "end_time",
        "duration_seconds",
        "zone_offset",
        "distance_meters",
        "calories_kcal",
        "steps",
        "avg_heart_rate_bpm",
        "max_heart_rate_bpm",
        "min_heart_rate_bpm",
        "avg_speed_meters_per_second",
        "max_speed_meters_per_second",
        "avg_power_watts",
        "max_power_watts",
        "avg_cadence",
        "max_cadence",
        "elevation_gain_meters",
        "elevation_loss_meters",
        "max_altitude_meters",
        "min_altitude_meters",
        "training_load",
        "route_available",
        "granular_truncated",
    }
    if samples:
        allowed.add("samples")
    if zones:
        allowed.add("heart_rate_zones")
    if route:
        allowed.add("route")
    result = {
        key: value
        for key, value in row.items()
        if key in allowed
    }
    if samples:
        clean_samples, samples_truncated = (
            _sanitize_retained_polar_samples(row.get("samples"))
        )
        result.pop("samples", None)
        if clean_samples:
            result["samples"] = clean_samples
        if samples_truncated:
            result["granular_truncated"] = True
    if zones:
        clean_zones, zones_truncated = (
            _sanitize_retained_polar_zones(
                row.get("heart_rate_zones")
            )
        )
        result.pop("heart_rate_zones", None)
        if clean_zones:
            result["heart_rate_zones"] = clean_zones
        if zones_truncated:
            result["granular_truncated"] = True
    if route:
        assert start_time is not None
        assert end_time is not None
        clean_route, route_truncated = (
            _sanitize_retained_polar_route(
                row.get("route"),
                start_time=start_time,
                end_time=end_time,
            )
        )
        result.pop("route", None)
        if clean_route:
            result["route"] = clean_route
        if route_truncated:
            result["granular_truncated"] = True
    identity_row = {
        "provider": provider,
        "provider_workout_id": workout_id,
        "provider_numeric_workout_id": numeric_workout_id,
    }
    return _with_row_identity(result, row=identity_row)


def _sanitize_retained_polar_samples(
    value: Any,
) -> tuple[list[dict[str, Any]], bool]:
    if value is None:
        return [], False
    if not isinstance(value, list):
        return [], True
    selected: list[dict[str, Any]] = []
    truncated = len(value) > _MAX_GRANULAR_WORKOUT_ROWS
    for raw in value[:_MAX_GRANULAR_WORKOUT_ROWS]:
        if not isinstance(raw, Mapping):
            truncated = True
            continue
        recording_rate = _nonnegative_number(
            raw.get("recording_rate_seconds"),
            integer=True,
        )
        sample_type = _safe_text(raw.get("sample_type"), max_length=64)
        data = _safe_text(
            raw.get("data"),
            max_length=_MAX_GRANULAR_WORKOUT_TEXT,
        )
        if recording_rate is None or sample_type is None or data is None:
            truncated = True
            continue
        selected.append(
            {
                "recording_rate_seconds": recording_rate,
                "sample_type": sample_type,
                "data": data,
            }
        )
    return selected, truncated


def _sanitize_retained_polar_zones(
    value: Any,
) -> tuple[list[dict[str, Any]], bool]:
    if value is None:
        return [], False
    if not isinstance(value, list):
        return [], True
    selected: list[dict[str, Any]] = []
    truncated = len(value) > _MAX_GRANULAR_WORKOUT_ROWS
    for raw in value[:_MAX_GRANULAR_WORKOUT_ROWS]:
        if not isinstance(raw, Mapping):
            truncated = True
            continue
        index = _nonnegative_number(raw.get("index"), integer=True)
        lower = _nonnegative_number(raw.get("lower_bpm"), integer=True)
        upper = _nonnegative_number(raw.get("upper_bpm"), integer=True)
        duration = _nonnegative_number(raw.get("duration_seconds"))
        if (
            index is None
            or lower is None
            or upper is None
            or lower > upper
            or duration is None
        ):
            truncated = True
            continue
        selected.append(
            {
                "index": index,
                "lower_bpm": lower,
                "upper_bpm": upper,
                "duration_seconds": duration,
            }
        )
    return selected, truncated


def _sanitize_retained_polar_route(
    value: Any,
    *,
    start_time: datetime,
    end_time: datetime,
) -> tuple[list[dict[str, Any]], bool]:
    if value is None:
        return [], False
    if not isinstance(value, list):
        return [], True
    selected: list[dict[str, Any]] = []
    truncated = len(value) > _MAX_GRANULAR_WORKOUT_ROWS
    for raw in value[:_MAX_GRANULAR_WORKOUT_ROWS]:
        if not isinstance(raw, Mapping):
            truncated = True
            continue
        latitude = _number(raw.get("latitude"))
        longitude = _number(raw.get("longitude"))
        timestamp = _timestamp(raw.get("timestamp"))
        if (
            latitude is None
            or longitude is None
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
            or (
                timestamp is not None
                and not start_time <= timestamp <= end_time
            )
        ):
            truncated = True
            continue
        point: dict[str, Any] = {
            "latitude": latitude,
            "longitude": longitude,
        }
        if timestamp is not None:
            point["timestamp"] = timestamp.isoformat()
        for field in ("satellites", "fix"):
            number = _nonnegative_number(raw.get(field), integer=True)
            if number is not None:
                point[field] = number
        selected.append(point)
    return selected, truncated


def _sanitize_health_score(
    row: Mapping[str, Any],
    *,
    start: datetime,
    end: datetime,
    retained_after: datetime | None,
) -> dict[str, Any] | None:
    recorded_at = _timestamp(row.get("recorded_at"))
    category = _safe_text(row.get("category"), max_length=32)
    provider, attribution = _provider(row)
    if (
        recorded_at is None
        or not start.astimezone(UTC) <= recorded_at < end.astimezone(UTC)
        or not _observation_is_retained(
            recorded_at,
            retained_after=retained_after,
        )
        or category not in WEARABLE_HEALTH_SCORE_CATEGORIES
    ):
        return None
    result: dict[str, Any] = {
        "category": category,
        "recorded_at": recorded_at.isoformat(),
        "provider": provider,
        "provider_attribution": attribution,
    }
    _put_number(result, row, "value")
    qualifier = _safe_text(row.get("qualifier"), max_length=64)
    if qualifier is not None:
        result["qualifier"] = qualifier
    components = row.get("components")
    if isinstance(components, Mapping):
        clean_components: list[dict[str, Any]] = []
        for name, raw in sorted(
            components.items(),
            key=lambda item: str(item[0]),
        )[:16]:
            component_name = _safe_text(str(name), max_length=64)
            if (
                component_name is None
                or _normalized_key(component_name) in _PRIVATE_KEYS
                or not isinstance(raw, Mapping)
            ):
                continue
            component: dict[str, Any] = {"component": component_name}
            _put_number(component, raw, "value")
            component_qualifier = _safe_text(
                raw.get("qualifier"),
                max_length=64,
            )
            if component_qualifier is not None:
                component["qualifier"] = component_qualifier
            clean_components.append(component)
        if clean_components:
            result["components"] = clean_components
    return _with_row_identity(result, row=row)


def _sanitize_summary(
    row: Mapping[str, Any],
    *,
    kind: str,
    start: datetime,
    end: datetime,
    timezone: str,
    retained_after: datetime | None,
) -> dict[str, Any] | None:
    observed_day = _day(row.get("date"))
    provider, attribution = _provider(row)
    if (
        observed_day is None
        or not _summary_day_fully_covered(
            observed_day,
            start=start,
            end=end,
            timezone=timezone,
        )
        or not _observation_is_retained(
            _summary_day_bounds(
                observed_day,
                timezone=timezone,
            )[0],
            retained_after=retained_after,
        )
    ):
        return None
    result: dict[str, Any] = {
        "summary_kind": kind,
        "date": observed_day.isoformat(),
        "provider": provider,
        "provider_attribution": attribution,
    }
    if kind == "activity":
        for field in (
            "steps",
            "floors_climbed",
            "active_minutes",
            "sedentary_minutes",
        ):
            _put_number(result, row, field, integer=True)
        for field in (
            "distance_meters",
            "elevation_meters",
            "active_calories_kcal",
            "total_calories_kcal",
        ):
            _put_number(result, row, field)
        _copy_numeric_block(
            result,
            row,
            "intensity_minutes",
            ("light", "moderate", "vigorous"),
            integer=True,
        )
        _copy_numeric_block(
            result,
            row,
            "heart_rate",
            ("avg_bpm", "max_bpm", "min_bpm"),
            integer=True,
        )
        return _with_row_identity(result, row=row)
    if kind == "sleep":
        for field in ("start_time", "end_time"):
            value = _timestamp(row.get(field))
            if value is not None:
                result[field] = value.isoformat()
        zone_offset = _safe_text(row.get("zone_offset"), max_length=16)
        if zone_offset is not None:
            result["zone_offset"] = zone_offset
        for field in (
            "duration_minutes",
            "total_duration_minutes",
            "time_in_bed_minutes",
            "interruptions_count",
            "nap_count",
            "nap_duration_minutes",
            "avg_heart_rate_bpm",
        ):
            _put_number(result, row, field, integer=True)
        for field in (
            "efficiency_percent",
            "avg_hrv_sdnn_ms",
            "avg_hrv_rmssd_ms",
            "avg_respiratory_rate",
            "avg_spo2_percent",
        ):
            _put_number(result, row, field)
        _copy_numeric_block(
            result,
            row,
            "stages",
            (
                "awake_minutes",
                "light_minutes",
                "deep_minutes",
                "rem_minutes",
            ),
            integer=True,
        )
        return _with_row_identity(result, row=row)
    for field in (
        "sleep_duration_seconds",
        "resting_heart_rate_bpm",
        "recovery_score",
    ):
        _put_number(result, row, field, integer=True)
    for field in (
        "sleep_efficiency_percent",
        "avg_hrv_sdnn_ms",
        "avg_spo2_percent",
    ):
        _put_number(result, row, field)
    return _with_row_identity(result, row=row)


def _copy_numeric_block(
    target: dict[str, Any],
    source: Mapping[str, Any],
    field: str,
    allowed: Sequence[str],
    *,
    integer: bool,
) -> None:
    raw = source.get(field)
    if not isinstance(raw, Mapping):
        return
    block: dict[str, Any] = {}
    for name in allowed:
        _put_number(block, raw, name, integer=integer)
    if block:
        target[field] = block


def _sanitize_workout(
    row: Mapping[str, Any],
    *,
    start: datetime,
    end: datetime,
    retained_after: datetime | None,
) -> dict[str, Any] | None:
    start_time = _timestamp(row.get("start_time"))
    end_time = _timestamp(row.get("end_time"))
    workout_type = _safe_text(row.get("type"), max_length=64)
    provider, attribution = _provider(row)
    if workout_type is None or not _workout_interval_is_allowed(
        start_time,
        end_time,
        start=start,
        end=end,
        retained_after=retained_after,
    ):
        return None
    assert start_time is not None
    assert end_time is not None
    result: dict[str, Any] = {
        "workout_type": workout_type,
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "provider": provider,
        "provider_attribution": attribution,
    }
    zone_offset = _safe_text(row.get("zone_offset"), max_length=16)
    if zone_offset is not None:
        result["zone_offset"] = zone_offset
    for field in (
        "duration_seconds",
        "avg_heart_rate_bpm",
        "max_heart_rate_bpm",
    ):
        _put_number(result, row, field, integer=True)
    for field in (
        "calories_kcal",
        "distance_meters",
        "avg_pace_sec_per_km",
        "elevation_gain_meters",
    ):
        _put_number(result, row, field)
    return _with_row_identity(result, row=row)


def _workout_interval_is_allowed(
    start_time: datetime | None,
    end_time: datetime | None,
    *,
    start: datetime,
    end: datetime,
    retained_after: datetime | None,
) -> bool:
    if start_time is None or end_time is None:
        return False
    query_start = start.astimezone(UTC)
    query_end = end.astimezone(UTC)
    return (
        query_start <= start_time < end_time <= query_end
        and _observation_is_retained(
            start_time,
            retained_after=retained_after,
        )
    )


def _sanitize_timeseries(
    row: Mapping[str, Any],
    *,
    series_type: str,
    start: datetime,
    end: datetime,
    retained_after: datetime | None,
) -> dict[str, Any] | None:
    timestamp = _timestamp(row.get("timestamp"))
    raw_type = _safe_text(row.get("type"), max_length=64)
    value = _number(row.get("value"))
    unit = _safe_text(row.get("unit"), max_length=32)
    provider, attribution = _provider(row)
    if (
        timestamp is None
        or not start.astimezone(UTC) <= timestamp < end.astimezone(UTC)
        or not _observation_is_retained(
            timestamp,
            retained_after=retained_after,
        )
        or raw_type != series_type
        or raw_type not in WEARABLE_TIMESERIES_TYPES
        or value is None
        or unit is None
    ):
        return None
    result: dict[str, Any] = {
        "timestamp": timestamp.isoformat(),
        "series_type": raw_type,
        "value": value,
        "unit": unit,
        "provider": provider,
        "provider_attribution": attribution,
    }
    stream_key = _trusted_stream_key(row)
    if stream_key is not None:
        result[_INTERNAL_STREAM_KEY] = stream_key
    zone_offset = _safe_text(row.get("zone_offset"), max_length=16)
    if zone_offset is not None:
        result["zone_offset"] = zone_offset
    if type(row.get("is_daily_total")) is bool:
        result["is_daily_total"] = row["is_daily_total"]
    return _with_row_identity(result, row=row)


def _summary_day_bounds(
    observed_day: date,
    *,
    timezone: str,
) -> tuple[datetime, datetime]:
    zone = parse_timezone(timezone)
    day_start = datetime.combine(observed_day, time.min, tzinfo=zone)
    day_end = datetime.combine(
        observed_day + timedelta(days=1),
        time.min,
        tzinfo=zone,
    )
    return day_start.astimezone(UTC), day_end.astimezone(UTC)


def _summary_day_fully_covered(
    observed_day: date,
    *,
    start: datetime,
    end: datetime,
    timezone: str,
) -> bool:
    day_start, day_end = _summary_day_bounds(
        observed_day,
        timezone=timezone,
    )
    return (
        start.astimezone(UTC) <= day_start
        and day_end <= end.astimezone(UTC)
    )


def _summary_window_is_partial(
    *,
    start: datetime,
    end: datetime,
    timezone: str,
) -> bool:
    zone = parse_timezone(timezone)
    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    start_boundary, _ = _summary_day_bounds(
        start_utc.astimezone(zone).date(),
        timezone=timezone,
    )
    end_boundary, _ = _summary_day_bounds(
        end_utc.astimezone(zone).date(),
        timezone=timezone,
    )
    return start_utc != start_boundary or end_utc != end_boundary


def _deduplicate_wearable_records(
    records: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Collapse authoritative duplicates and retain ambiguous observations."""

    selected: list[dict[str, Any]] = []
    selected_indexes: dict[str, list[int]] = {}
    variant_origins: dict[str, dict[str, set[int]]] = {}
    stable_identities: set[str] = set()
    page_occurrences: dict[tuple[int, str, str], int] = {}
    occurrence_origins: dict[
        tuple[str, str, int],
        tuple[int, int],
    ] = {}
    conflicting = False
    for fallback_ordinal, raw_record in enumerate(
        records[: MAX_WEARABLE_SEARCH_ROWS + 1]
    ):
        record = dict(raw_record)
        identity = _safe_text(
            record.pop(_INTERNAL_ROW_IDENTITY, None),
            max_length=64,
        )
        if identity is None:
            identity = hashlib.sha256(
                json.dumps(
                    {
                        "kind": "sanitized-structural",
                        "provider": record.get("provider"),
                        "locator": _structural_row_locator(record),
                    },
                    allow_nan=False,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                ).hexdigest()
        stable_identity = (
            record.pop(_INTERNAL_STABLE_ROW_ID, False) is True
        )
        if stable_identity:
            stable_identities.add(identity)
        stable_identity = identity in stable_identities
        raw_page_index = record.pop(_INTERNAL_PAGE_INDEX, None)
        raw_page_ordinal = record.pop(_INTERNAL_PAGE_ORDINAL, None)
        page_index = (
            raw_page_index
            if type(raw_page_index) is int and raw_page_index >= 0
            else fallback_ordinal
        )
        page_ordinal = (
            raw_page_ordinal
            if type(raw_page_ordinal) is int and raw_page_ordinal >= 0
            else 0
        )
        digest = hashlib.sha256(
            json.dumps(
                record,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        occurrence_key = (page_index, identity, digest)
        occurrence = (
            0
            if stable_identity
            else page_occurrences.get(occurrence_key, 0)
        )
        if not stable_identity:
            page_occurrences[occurrence_key] = occurrence + 1
        cross_page_key = (identity, digest, occurrence)
        identity_variants = variant_origins.setdefault(identity, {})
        has_conflict = any(
            variant_digest != digest
            and (
                stable_identity
                or any(
                    origin_page != page_index
                    for origin_page in origin_pages
                )
            )
            for variant_digest, origin_pages in identity_variants.items()
        )
        if has_conflict:
            conflicting = True
            for index in selected_indexes.get(identity, ()):
                selected[index].pop(_INTERNAL_STREAM_KEY, None)
            record.pop(_INTERNAL_STREAM_KEY, None)

        prior_origin = occurrence_origins.get(cross_page_key)
        if prior_origin is not None and (
            stable_identity or prior_origin[0] != page_index
        ):
            identity_variants.setdefault(digest, set()).add(page_index)
            continue
        occurrence_origins[cross_page_key] = (page_index, page_ordinal)

        identity_variants.setdefault(digest, set()).add(page_index)
        selected_indexes.setdefault(identity, []).append(len(selected))
        selected.append(record)
    return selected, conflicting


def _aggregate_timeseries(
    records: Sequence[Mapping[str, Any]],
    *,
    series_type: str,
    resolution: str,
    start: datetime,
    end: datetime,
    retained_after: datetime | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Enforce the requested resolution even if the upstream route ignores it."""

    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    buckets: dict[
        tuple[datetime, str, str, str],
        list[Mapping[str, Any]],
    ] = {}
    unattributed: list[dict[str, Any]] = []
    for record in records:
        timestamp = _timestamp(record.get("timestamp"))
        unit = _safe_text(record.get("unit"), max_length=32)
        provider = _safe_text(record.get("provider"), max_length=64)
        stream_key = _safe_text(
            record.get(_INTERNAL_STREAM_KEY),
            max_length=64,
        )
        if timestamp is None or unit is None or provider is None:
            continue
        bucket_start = _resolution_bucket_start(
            timestamp,
            resolution=resolution,
        )
        public_timestamp = _bounded_bucket_timestamp(
            bucket_start,
            start=start_utc,
            end=end_utc,
            observation=timestamp,
            retained_after=retained_after,
        )
        if stream_key is None:
            # Matching provider or device labels do not prove that samples
            # belong to one sensor. Keep observations separate while still
            # enforcing the requested timestamp resolution.
            coarsened = {
                key: value
                for key, value in record.items()
                if key != _INTERNAL_STREAM_KEY
            }
            coarsened["timestamp"] = public_timestamp.isoformat()
            unattributed.append(coarsened)
            continue
        buckets.setdefault(
            (bucket_start, unit, provider, stream_key),
            [],
        ).append(record)

    aggregated = list(unattributed)
    for (bucket_start, unit, provider, stream_key), bucket in sorted(
        buckets.items(),
        key=lambda item: (
            item[0][0],
            item[0][1],
            item[0][2],
            item[0][3],
        ),
    ):
        values = [
            float(value)
            for record in bucket
            if (value := _number(record.get("value"))) is not None
        ]
        if not values:
            continue
        result: dict[str, Any] = {
            "timestamp": _bounded_bucket_timestamp(
                bucket_start,
                start=start_utc,
                end=end_utc,
                observation=min(
                    timestamp
                    for record in bucket
                    if (
                        timestamp := _timestamp(
                            record.get("timestamp")
                        )
                    )
                    is not None
                ),
                retained_after=retained_after,
            ).isoformat(),
            "series_type": series_type,
            "value": _normalized_aggregate_value(
                bucket,
                values=values,
                series_type=series_type,
            ),
            "unit": unit,
            "provider": provider,
        }
        attributions = {
            value
            for record in bucket
            if (
                value := _safe_text(
                    record.get("provider_attribution"),
                    max_length=32,
                )
            )
            is not None
        }
        if len(attributions) == 1:
            result["provider_attribution"] = attributions.pop()
        elif attributions:
            result["provider_attribution"] = "mixed"
        if (
            series_type in _SUM_TIMESERIES_TYPES
            and any(record.get("is_daily_total") is True for record in bucket)
        ):
            result["is_daily_total"] = True
        aggregated.append(result)
    return aggregated, bool(unattributed)


def _resolution_bucket_start(
    timestamp: datetime,
    *,
    resolution: str,
) -> datetime:
    interval_seconds = _TIMESERIES_RESOLUTION_SECONDS[resolution]
    epoch_seconds = int(timestamp.timestamp())
    return datetime.fromtimestamp(
        epoch_seconds - (epoch_seconds % interval_seconds),
        tz=UTC,
    )


def _bounded_bucket_timestamp(
    bucket_start: datetime,
    *,
    start: datetime,
    end: datetime,
    observation: datetime,
    retained_after: datetime | None,
) -> datetime:
    bounded = max(bucket_start, start.astimezone(UTC))
    if (
        retained_after is not None
        and bounded <= retained_after.astimezone(UTC)
    ):
        bounded = max(bounded, observation.astimezone(UTC))
    if bounded >= end.astimezone(UTC):
        raise ValueError("wearable timeseries bucket is outside query bounds")
    return bounded


def _normalized_aggregate_value(
    bucket: Sequence[Mapping[str, Any]],
    *,
    values: Sequence[float],
    series_type: str,
) -> int | float:
    if series_type in _SUM_TIMESERIES_TYPES:
        daily_totals = [
            float(value)
            for record in bucket
            if record.get("is_daily_total") is True
            and (value := _number(record.get("value"))) is not None
        ]
        aggregate = max(daily_totals) if daily_totals else sum(values)
    else:
        aggregate = sum(values) / len(values)
    return int(aggregate) if aggregate.is_integer() else aggregate


def _row_sort_key(row: Mapping[str, Any]) -> tuple[str, str]:
    timestamp = next(
        (
            str(row[field])
            for field in (
                "timestamp",
                "recorded_at",
                "start_time",
                "date",
            )
            if field in row
        ),
        "",
    )
    return timestamp, json.dumps(
        row,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _encoded_size(value: Mapping[str, Any]) -> int:
    return len(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
