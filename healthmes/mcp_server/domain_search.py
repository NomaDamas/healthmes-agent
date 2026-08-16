"""FastMCP adapters for request-scoped decision context searches."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from healthmes.decision.contracts import PrivacyLevel
from healthmes.decision.search import (
    DECISION_SEARCH_SESSION_ID_PATTERN,
    DecisionContextSearchSessionService,
    DecisionSearchSessionError,
)

DecisionSessionId = Annotated[
    str,
    Field(
        min_length=47,
        max_length=47,
        pattern=DECISION_SEARCH_SESSION_ID_PATTERN,
        description=(
            "Opaque identifier created by the HealthMes decision request "
            "boundary; it is not an owner, conversation, or database ID."
        ),
    ),
]
IsoDate = Annotated[
    str,
    Field(
        min_length=10,
        max_length=10,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Canonical local date in YYYY-MM-DD form.",
    ),
]
IsoDateTime = Annotated[
    str,
    Field(
        min_length=20,
        max_length=64,
        description="ISO 8601 datetime with an explicit UTC offset.",
    ),
]
SearchLimit = Annotated[
    int,
    Field(
        ge=1,
        le=1_000,
        description=(
            "Requested result row bound; the session policy may reduce it."
        ),
    ),
]
OpaqueCursor = Annotated[
    str,
    Field(
        min_length=69,
        max_length=69,
        pattern=r"^hmc1_[0-9a-f]{64}$",
        description=(
            "Opaque provider cursor returned by the immediately preceding "
            "page for the same capability, window, privacy, and filters."
        ),
    ),
]
LookbackDays = Annotated[int, Field(ge=1, le=90)]
MinimumMinutes = Annotated[int, Field(ge=1, le=1_440)]
PrivacySelection = Literal["aggregate", "identity"]

ActivityCapability = Literal[
    "activity.summary",
    "activity.focus",
    "activity.overwork",
    "activity.recovery",
    "activity.timeline",
]
ActivityGranularity = Literal["summary", "day", "window", "record"]
ActivityPlatform = Literal[
    "android",
    "ios",
    "macos",
    "windows",
    "linux",
    "unknown",
]
ActivityField = Literal[
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
    "window",
    "classification",
    "metrics",
    "boundary",
    "lookback_days",
    "risk_level",
    "signals",
    "threshold_uncertainties",
    "count",
    "records",
]

NutritionCapability = Literal[
    "nutrition.intake-history",
    "nutrition.caffeine-ledger",
    "nutrition.decision-context",
]
NutritionGranularity = Literal["summary", "record", "day"]
NutritionIntent = Literal[
    "log_consumed",
    "ask_before_intake",
    "inspect_only",
    "plan_future",
    "compare_option",
]
NutritionModality = Literal["photo", "text", "voice"]
NutritionField = Literal[
    "status",
    "count",
    "records",
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
    "request",
    "candidate",
    "comparison_candidates",
    "confirmed_intake_history",
    "history_window",
    "specialized_evidence",
    "boundaries",
]

CalendarCapability = Literal[
    "calendar.day-summary",
    "calendar.busy-intervals",
    "calendar.available-windows",
    "calendar.event-detail",
]
CalendarGranularity = Literal["summary", "day", "window", "record"]
CalendarField = Literal[
    "status",
    "date",
    "timezone",
    "event_count",
    "busy_minutes",
    "first_event_at",
    "last_event_at",
    "meeting_density_per_hour",
    "window",
    "intervals",
    "available_minutes",
    "windows",
    "count",
    "events",
]

WearableCapability = Literal[
    "wearable.readiness",
    "wearable.sleep",
    "wearable.recovery",
    "wearable.stress",
    "wearable.metric-detail",
    "wearable.health-scores",
    "wearable.summaries",
    "wearable.workouts",
    "wearable.timeseries",
]
WearableGranularity = Literal[
    "summary",
    "day",
    "record",
    "window",
    "series",
]
WearableKind = Literal["load", "recovery", "sleep", "stress"]
WearableMetric = Literal[
    "actual_sleep",
    "charge",
    "hrv",
    "sleep_debt",
    "stress",
    "yesterday_load",
]
WearableHealthScoreCategory = Literal[
    "activity",
    "body_battery",
    "readiness",
    "recovery",
    "resilience",
    "sleep",
    "strain",
    "stress",
]
WearableSummaryKind = Literal["activity", "recovery", "sleep"]
WearableTimeseriesType = Literal[
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
]
WearableTimeseriesResolution = Literal["1min", "5min", "15min", "1hour"]
WearableField = Literal[
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
    "count",
    "records",
    "window",
    "provenance_mode",
]

ServiceResolver = Callable[[], DecisionContextSearchSessionService]

_registered_mcp_ids: set[int] = set()


def _aware(value: str | None, field: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolError(f"{field} must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        raise ToolError(f"{field} must include a timezone offset")
    return parsed.astimezone(UTC)


def _privacy(value: PrivacySelection) -> PrivacyLevel:
    return PrivacyLevel(value)


def _wearable_granularity(
    capability: WearableCapability,
    requested: WearableGranularity | None,
) -> WearableGranularity:
    if requested is not None:
        return requested
    return {
        "wearable.health-scores": "record",
        "wearable.workouts": "record",
        "wearable.timeseries": "series",
    }.get(capability, "summary")  # type: ignore[return-value]


async def _search(
    service_resolver: ServiceResolver,
    decision_session_id: str,
    *,
    domain: str,
    capability: str,
    start: str | None,
    end: str | None,
    granularity: str,
    fields: list[str] | None,
    privacy_level: PrivacySelection,
    limit: int,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    try:
        service = service_resolver()
        result = await service.search(
            decision_session_id,
            domain=domain,
            capability=capability,
            start=_aware(start, "start"),
            end=_aware(end, "end"),
            granularity=granularity,
            fields=tuple(fields or ()),
            privacy_level=_privacy(privacy_level),
            limit=limit,
            parameters={
                key: value
                for key, value in parameters.items()
                if value is not None
            },
        )
    except DecisionSearchSessionError as exc:
        raise ToolError(exc.code) from exc
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError("decision_search_service_unavailable") from exc
    return result.model_dump(mode="json")


def register_domain_search_tools(
    mcp: FastMCP,
    *,
    service_resolver: ServiceResolver,
) -> None:
    """Register the four read-only decision-domain search tools once."""

    identity = id(mcp)
    if identity in _registered_mcp_ids:
        return
    _registered_mcp_ids.add(identity)

    @mcp.tool
    async def search_activity(
        decision_session_id: DecisionSessionId,
        capability: ActivityCapability,
        start: IsoDateTime | None = None,
        end: IsoDateTime | None = None,
        date: IsoDate | None = None,
        lookback_days: LookbackDays | None = None,
        cursor: OpaqueCursor | None = None,
        device_id: Annotated[
            str | None,
            Field(min_length=1, max_length=255),
        ] = None,
        platform: ActivityPlatform | None = None,
        granularity: ActivityGranularity = "summary",
        fields: Annotated[list[ActivityField] | None, Field(max_length=64)] = None,
        privacy_level: PrivacySelection = "aggregate",
        limit: SearchLimit = 100,
    ) -> dict[str, Any]:
        """Search one bounded Activity capability through an existing decision session."""

        return await _search(
            service_resolver,
            decision_session_id,
            domain="activity",
            capability=capability,
            start=start,
            end=end,
            granularity=granularity,
            fields=fields,
            privacy_level=privacy_level,
            limit=limit,
            parameters={
                "date": date,
                "lookback_days": lookback_days,
                "cursor": cursor,
                "device_id": device_id,
                "platform": platform,
            },
        )

    @mcp.tool
    async def search_nutrition(
        decision_session_id: DecisionSessionId,
        capability: NutritionCapability,
        start: IsoDateTime | None = None,
        end: IsoDateTime | None = None,
        date: IsoDate | None = None,
        confirmed_only: bool | None = None,
        intent: NutritionIntent | None = None,
        modality: NutritionModality | None = None,
        nutrient: Annotated[str | None, Field(min_length=1, max_length=128)] = None,
        text_query: Annotated[str | None, Field(min_length=1, max_length=500)] = None,
        request_id: Annotated[
            str | None,
            Field(
                min_length=36,
                max_length=36,
                description=(
                    "A nutrition request ID already selected in the "
                    "server-owned DecisionRequest."
                ),
            ),
        ] = None,
        granularity: NutritionGranularity = "summary",
        fields: Annotated[list[NutritionField] | None, Field(max_length=64)] = None,
        privacy_level: PrivacySelection = "aggregate",
        limit: SearchLimit = 100,
    ) -> dict[str, Any]:
        """Search one bounded Nutrition capability without exposing capture bytes."""

        return await _search(
            service_resolver,
            decision_session_id,
            domain="nutrition",
            capability=capability,
            start=start,
            end=end,
            granularity=granularity,
            fields=fields,
            privacy_level=privacy_level,
            limit=limit,
            parameters={
                "date": date,
                "confirmed_only": confirmed_only,
                "intent": intent,
                "modality": modality,
                "nutrient": nutrient,
                "query": text_query,
                "request_id": request_id,
            },
        )

    @mcp.tool
    async def search_calendar(
        decision_session_id: DecisionSessionId,
        capability: CalendarCapability,
        start: IsoDateTime | None = None,
        end: IsoDateTime | None = None,
        date: IsoDate | None = None,
        minimum_minutes: MinimumMinutes | None = None,
        cursor: OpaqueCursor | None = None,
        granularity: CalendarGranularity = "summary",
        fields: Annotated[list[CalendarField] | None, Field(max_length=64)] = None,
        privacy_level: PrivacySelection = "aggregate",
        limit: SearchLimit = 100,
    ) -> dict[str, Any]:
        """Search bounded mirrored Calendar availability without event titles."""

        return await _search(
            service_resolver,
            decision_session_id,
            domain="calendar",
            capability=capability,
            start=start,
            end=end,
            granularity=granularity,
            fields=fields,
            privacy_level=privacy_level,
            limit=limit,
            parameters={
                "date": date,
                "minimum_minutes": minimum_minutes,
                "cursor": cursor,
            },
        )

    @mcp.tool
    async def search_wearable(
        decision_session_id: DecisionSessionId,
        capability: WearableCapability,
        start: IsoDateTime | None = None,
        end: IsoDateTime | None = None,
        date: IsoDate | None = None,
        cursor: OpaqueCursor | None = None,
        kind: WearableKind | None = None,
        metric: WearableMetric | None = None,
        category: WearableHealthScoreCategory | None = None,
        summary_kind: WearableSummaryKind | None = None,
        series_type: WearableTimeseriesType | None = None,
        resolution: WearableTimeseriesResolution | None = None,
        granularity: WearableGranularity | None = None,
        fields: Annotated[list[WearableField] | None, Field(max_length=64)] = None,
        privacy_level: PrivacySelection = "aggregate",
        limit: SearchLimit = 100,
    ) -> dict[str, Any]:
        """Search bounded wearable context through HealthMes-owned mirrors."""

        return await _search(
            service_resolver,
            decision_session_id,
            domain="wearable",
            capability=capability,
            start=start,
            end=end,
            granularity=_wearable_granularity(
                capability,
                granularity,
            ),
            fields=fields,
            privacy_level=privacy_level,
            limit=limit,
            parameters={
                "date": date,
                "cursor": cursor,
                "kind": kind,
                "metric": metric,
                "category": category,
                "summary_kind": summary_kind,
                "series_type": series_type,
                "resolution": resolution,
            },
        )
