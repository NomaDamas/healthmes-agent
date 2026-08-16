"""Bounded, sanitized Open Wearables search for the HealthMes MCP surface."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import Any

from healthmes.mcp_server.ow_client import OWClient
from healthmes.timezones import parse_timezone

WEARABLE_DETAIL_CAPABILITIES = frozenset(
    {
        "wearable.health-scores",
        "wearable.summaries",
        "wearable.workouts",
        "wearable.timeseries",
    }
)
WEARABLE_HEALTH_SCORE_CATEGORIES = (
    "activity",
    "body_battery",
    "readiness",
    "recovery",
    "resilience",
    "sleep",
    "strain",
    "stress",
)
WEARABLE_SUMMARY_KINDS = ("activity", "recovery", "sleep")
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
    "wearable.health-scores": frozenset({"category"}),
    "wearable.summaries": frozenset({"summary_kind"}),
    "wearable.workouts": frozenset(),
    "wearable.timeseries": frozenset({"resolution", "series_type"}),
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
    "apple_health": "apple_health",
    "fitbit": "fitbit",
    "garmin": "garmin",
    "google": "google_health_connect",
    "google_health_connect": "google_health_connect",
    "internal": "internal",
    "oura": "oura",
    "polar": "polar",
    "samsung": "samsung_health",
    "samsung_health": "samsung_health",
    "strava": "strava",
    "suunto": "suunto",
    "ultrahuman": "ultrahuman",
    "whoop": "whoop",
}
_INTERNAL_STREAM_KEY = "_healthmes_stream_key"
_TRUSTED_PROVIDER_ATTRIBUTIONS = frozenset(
    {"declared", "source_exact_alias"}
)

WearableUserIdResolver = Callable[[], str | Awaitable[str]]


@dataclass(frozen=True, slots=True)
class WearableSearchRequest:
    capability: str
    start: datetime
    end: datetime
    timezone: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _ProviderPage:
    rows: tuple[dict[str, Any], ...]
    raw_count: int
    discarded_rows: int


@dataclass(frozen=True, slots=True)
class WearableSearchFetch:
    records: tuple[dict[str, Any], ...]
    upstream_truncated: bool = False
    payload_trimmed: bool = False
    discarded_rows: int = 0
    stream_attribution_unavailable: bool = False

    @property
    def limitations(self) -> tuple[str, ...]:
        values: list[str] = []
        if self.upstream_truncated:
            values.append("wearable_upstream_page_limit_reached")
        if self.payload_trimmed:
            values.append("wearable_payload_limit_reached")
        if self.discarded_rows:
            values.append("wearable_rows_discarded")
        if self.stream_attribution_unavailable:
            values.append("wearable_stream_attribution_unavailable")
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


def normalize_retained_wearable_timeseries(
    records: Sequence[Mapping[str, Any]],
    *,
    series_type: str,
    resolution: str,
    start: datetime,
    end: datetime,
    stream_attribution_verified: bool = False,
) -> WearableSearchFetch:
    """Reapply current privacy rules to previously stored public records."""

    if series_type not in WEARABLE_TIMESERIES_TYPES:
        raise ValueError("wearable timeseries type is not allowlisted")
    if resolution not in WEARABLE_TIMESERIES_RESOLUTIONS:
        raise ValueError("wearable timeseries resolution is not allowlisted")

    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
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
        normalized.append(result)
    normalized.sort(key=_row_sort_key)
    return WearableSearchFetch(
        records=tuple(normalized),
        discarded_rows=discarded,
        stream_attribution_unavailable=(
            bool(normalized) and not stream_attribution_verified
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
    parse_timezone(request.timezone)
    unexpected = (
        set(request.parameters)
        - _CAPABILITY_PARAMETERS[request.capability]
    )
    if unexpected:
        raise ValueError("wearable query contains unsupported parameters")
    if request.capability == "wearable.health-scores":
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
        validate_wearable_search_request(request)
        user_id = self._user_id_resolver()
        if inspect.isawaitable(user_id):
            user_id = await user_id
        if not isinstance(user_id, str) or not user_id:
            raise LookupError("open-wearables user is unavailable")

        if request.capability == "wearable.health-scores":
            rows, truncated, discarded = await self._health_scores(
                user_id,
                request,
            )
            sanitizer = partial(
                _sanitize_health_score,
                start=request.start,
                end=request.end,
            )
        elif request.capability == "wearable.summaries":
            rows, truncated, discarded = await self._summaries(
                user_id,
                request,
            )
            kind = str(request.parameters["summary_kind"])
            sanitizer = partial(
                _sanitize_summary,
                kind=kind,
                start=request.start,
                end=request.end,
                timezone=request.timezone,
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
            )
        else:
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
            )

        sanitized: list[dict[str, Any]] = []
        for row in rows:
            clean = sanitizer(row)
            if clean is None or _contains_private_value(clean, user_id):
                discarded += 1
                continue
            sanitized.append(clean)
        stream_attribution_unavailable = False
        if request.capability == "wearable.timeseries":
            sanitized, stream_attribution_unavailable = _aggregate_timeseries(
                sanitized,
                series_type=str(request.parameters["series_type"]),
                resolution=str(request.parameters["resolution"]),
                start=request.start,
                end=request.end,
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
            payload_trimmed=payload_trimmed,
            discarded_rows=discarded,
            stream_attribution_unavailable=(
                stream_attribution_unavailable
            ),
        )

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
    for _ in range(MAX_WEARABLE_SEARCH_PAGES):
        remaining = MAX_WEARABLE_SEARCH_ROWS + 1 - raw_rows
        if remaining <= 0:
            return rows, True, discarded_rows
        page_limit = min(_PAGE_SIZE, remaining)
        payload = await fetch(page_limit, offset)
        page = _response_page(payload, max_rows=page_limit)
        rows.extend(page.rows)
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
    for _ in range(MAX_WEARABLE_SEARCH_PAGES):
        remaining = MAX_WEARABLE_SEARCH_ROWS + 1 - raw_rows
        if remaining <= 0:
            return rows, True, discarded_rows
        page_limit = min(_PAGE_SIZE, remaining)
        payload = await fetch(page_limit, cursor)
        page = _response_page(payload, max_rows=page_limit)
        pagination = _response_pagination(payload)
        if (
            "next_cursor" not in pagination
            and "has_more" not in pagination
        ):
            raise ValueError(
                "open-wearables returned invalid cursor pagination"
            )
        raw_has_more = pagination.get("has_more", False)
        if type(raw_has_more) is not bool:
            raise ValueError(
                "open-wearables returned invalid cursor pagination"
            )
        raw_cursor = pagination.get("next_cursor")
        if raw_cursor is not None and (
            not isinstance(raw_cursor, str) or not raw_cursor
        ):
            raise ValueError(
                "open-wearables returned invalid cursor pagination"
            )
        next_cursor = raw_cursor
        has_more = bool(next_cursor or raw_has_more)
        if has_more and (
            next_cursor is None
            or next_cursor == cursor
            or next_cursor in seen_cursors
        ):
            return rows, True, discarded_rows
        rows.extend(page.rows)
        raw_rows += page.raw_count
        discarded_rows += page.discarded_rows
        if raw_rows > MAX_WEARABLE_SEARCH_ROWS:
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


def _provider_family(value: Any) -> str | None:
    cleaned = _safe_text(value, max_length=64)
    if cleaned is None:
        return None
    return _PROVIDER_FAMILY_ALIASES.get(cleaned.casefold())


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


def _sanitize_health_score(
    row: Mapping[str, Any],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any] | None:
    recorded_at = _timestamp(row.get("recorded_at"))
    category = _safe_text(row.get("category"), max_length=32)
    provider, attribution = _provider(row)
    if (
        recorded_at is None
        or not start.astimezone(UTC) <= recorded_at < end.astimezone(UTC)
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
    return result


def _sanitize_summary(
    row: Mapping[str, Any],
    *,
    kind: str,
    start: datetime,
    end: datetime,
    timezone: str,
) -> dict[str, Any] | None:
    observed_day = _day(row.get("date"))
    provider, attribution = _provider(row)
    zone = parse_timezone(timezone)
    first_day = start.astimezone(zone).date()
    last_day = (end - timedelta(microseconds=1)).astimezone(zone).date()
    if (
        observed_day is None
        or not first_day <= observed_day <= last_day
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
        return result
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
        return result
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
    return result


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
) -> dict[str, Any] | None:
    start_time = _timestamp(row.get("start_time"))
    end_time = _timestamp(row.get("end_time"))
    workout_type = _safe_text(row.get("type"), max_length=64)
    provider, attribution = _provider(row)
    if (
        start_time is None
        or end_time is None
        or end_time <= start_time
        or not start.astimezone(UTC) <= start_time < end.astimezone(UTC)
        or workout_type is None
    ):
        return None
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
    return result


def _sanitize_timeseries(
    row: Mapping[str, Any],
    *,
    series_type: str,
    start: datetime,
    end: datetime,
) -> dict[str, Any] | None:
    timestamp = _timestamp(row.get("timestamp"))
    raw_type = _safe_text(row.get("type"), max_length=64)
    value = _number(row.get("value"))
    unit = _safe_text(row.get("unit"), max_length=32)
    provider, attribution = _provider(row)
    if (
        timestamp is None
        or not start.astimezone(UTC) <= timestamp < end.astimezone(UTC)
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
    return result


def _aggregate_timeseries(
    records: Sequence[Mapping[str, Any]],
    *,
    series_type: str,
    resolution: str,
    start: datetime,
    end: datetime,
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
) -> datetime:
    bounded = max(bucket_start, start.astimezone(UTC))
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
