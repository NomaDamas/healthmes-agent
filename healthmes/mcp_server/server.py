"""HealthMes fastmcp server — Layer B "interpreted context" tools (tranche 1).

Design (docs/PLAN.md 1.5): MCP tools return deterministic facts — lookups and
*interpreted* deltas with confidence/coverage — never raw series dumps and
never LLM judgment. Health tools read the open-wearables REST API through
:class:`healthmes.mcp_server.ow_client.OWClient`; task/schedule/food/decision
tools use the healthmes domain store. All numeric interpretation lives in
:mod:`healthmes.mcp_server.interpret`.

The server is exposed over Streamable HTTP for mounting at ``/mcp`` on the
HealthMes FastAPI app (see :func:`build_mcp_http_app`); Hermes registers it as
a ``url:`` MCP server (contract: ``vendor/hermes-agent/tools/mcp_tool.py``).

Tranche-1 tools:
- ``get_health_scores`` / ``get_daily_readiness_context`` /
  ``get_personal_baselines`` (open-wearables interpreted reads)
- ``list_tasks`` / ``upsert_task`` / ``get_schedule`` /
  ``propose_schedule_blocks`` (schedule domain, propose-then-confirm gate)
- ``log_food`` / ``record_decision`` (capture + explainability)

Tranche-2 tools (docs/PLAN.md Phase 2, Layer B second tranche):
- ``get_cognitive_energy_forecast`` — persisted ``cognitive_energy_estimate``
  windows (engine-computed on demand when absent) for one **local** day
- ``get_stress_timeline`` — the day's stress series joined with calendar
  events and app-usage sessions into labeled intervals (never a raw dump);
  interval logic lives in :mod:`healthmes.mcp_server.timeline`
- ``compare_impact`` — before/after metric deltas around tagged occurrences
  of a factor; delta logic lives in :mod:`healthmes.mcp_server.impact`

Tranche-3 tools (docs/PLAN.md Phase 3, medical-lite capture, §8):
- ``create_medical_record`` — persist a medication/symptom capture with a
  deterministic health-context snapshot (reuses ``get_daily_readiness_context``)
- ``list_medical_records`` — local read for the ``doctor-visit-summary``
  skill; deliberately omits transcripts (only description text ever re-enters
  the LLM after capture — media and transcripts stay on this machine)

Timezone rule (load-bearing for tranche 2): "today", occurrence days, and
every stress/calendar/app-usage join use the **user's local timezone**,
resolved by :func:`_local_timezone` (override -> settings -> env -> system).
"""

import asyncio
import datetime as dt
import functools
import os
import re
import uuid
import zoneinfo
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from healthmes.config import Settings, get_settings, system_timezone
from healthmes.mcp_server import impact, interpret, timeline
from healthmes.mcp_server.ow_client import OWClient, OWClientError, resolve_single_user_id
from healthmes.store import (
    AppUsageSample,
    CalendarEventMirror,
    DecisionKind,
    DecisionRecord,
    EnergyDemand,
    FoodLog,
    MedicalRecord,
    MedicalRecordKind,
    ProposalStatus,
    ScheduleProposal,
    Task,
    TaskSource,
    WeeklyGoal,
    session_scope,
)
from healthmes.store import enums as store_enums

# ---------------------------------------------------------------------------
# Vocabulary grounded in vendor/open-wearables (do not invent values)
# ---------------------------------------------------------------------------

# backend/app/schemas/enums/health_score_category.py
SCORE_CATEGORIES = frozenset(
    {"sleep", "recovery", "readiness", "activity", "stress", "resilience", "body_battery", "strain"}
)
# The categories the vendor MCP server cannot see (docs/PLAN.md 1.5 gap table).
DEFAULT_SCORE_CATEGORIES = (
    "stress",
    "body_battery",
    "readiness",
    "recovery",
    "sleep",
    "resilience",
)

# Charge-style categories reported in the readiness context.
CHARGE_CATEGORIES = ("body_battery", "readiness", "recovery")

# Domain vocabularies for the store tools. Task statuses come from the store
# (single source of truth shared with the REST state machine in
# healthmes/api/tasks.py — the two write surfaces must agree, incl.
# "scheduled" for tasks whose blocks were placed).
TASK_STATUSES = store_enums.TASK_STATUSES
TASK_DONE_STATUSES = store_enums.TASK_DONE_STATUSES
MEAL_TYPES = frozenset({"breakfast", "lunch", "dinner", "snack"})  # events.py Meal literal
MEDICAL_RECORD_KINDS = frozenset(kind.value for kind in MedicalRecordKind)
# Reserved key inside medical_record.context holding the deterministic
# health snapshot; caller-supplied capture context lives under "capture".
MEDICAL_HEALTH_CONTEXT_KEY = "health"
MEDICAL_CAPTURE_CONTEXT_KEY = "capture"
MAX_MEDICAL_RANGE_DAYS = 365
MAX_MEDICAL_LIST_LIMIT = 500
DECISION_TREE_NODE_TYPES = frozenset({"input", "rule", "llm_step", "option", "action"})
MAX_TREE_DEPTH = 12
MAX_TREE_NODES = 500

_RANGE_PATTERN = re.compile(r"^(\d{1,3})d$")
MAX_RANGE_DAYS = 90
MIN_AGG_DAYS_WITH_DATA = 3
MIN_TIMELINE_SAMPLES = 3

mcp: FastMCP = FastMCP(
    "healthmes",
    instructions=(
        "HealthMes Layer B tools: deterministic, pre-interpreted health context "
        "(baselines, deltas, confidence/coverage — never raw series) plus the "
        "task/schedule/food/decision domain of the HealthMes assistant. "
        "Health tools may honestly return status=insufficient_data; treat low "
        "confidence as a reason to avoid categorical advice. For planning use "
        "get_cognitive_energy_forecast (hourly local-day energy windows), "
        "get_stress_timeline (when/why stressed — intervals joined with "
        "calendar and app context), and compare_impact (before/after deltas "
        "around a factor; associations, not causation). Schedule writes go "
        "through propose_schedule_blocks (propose-then-confirm; proposals are "
        "not calendar events yet). Always call record_decision after making or "
        "proposing a decision so it can be explained in the decision viewer. "
        "Medical-lite capture (create_medical_record / list_medical_records) "
        "is strictly local: records, media and transcripts never leave this "
        "machine — after capture only the structured description text may "
        "re-enter the model context."
    ),
)

# ---------------------------------------------------------------------------
# Runtime state (overridable for tests and for app-lifespan wiring)
# ---------------------------------------------------------------------------

_settings_override: Settings | None = None
_ow_client_override: OWClient | None = None
_session_factory_override: sessionmaker[Session] | None = None
_ow_user_id_override: str | None = None
_discovered_user_id: str | None = None
_timezone_override: dt.tzinfo | None = None
_energy_engine_override: Any | None = None


def set_settings(settings: Settings | None) -> None:
    """Override the settings used by the tools (None restores env settings).

    Also drops the cached resolved user id — the settings may pin a
    different ``ow_user_id``.
    """
    global _settings_override, _discovered_user_id
    _settings_override = settings
    _discovered_user_id = None


def set_ow_client(client: OWClient | None) -> None:
    """Override the open-wearables client (tests inject httpx.MockTransport)."""
    global _ow_client_override
    _ow_client_override = client


def set_session_factory(factory: sessionmaker[Session] | None) -> None:
    """Override the store session factory (None restores the process-wide one)."""
    global _session_factory_override
    _session_factory_override = factory


def set_ow_user_id(user_id: str | None) -> None:
    """Pin the open-wearables user id (None re-enables discovery)."""
    global _ow_user_id_override, _discovered_user_id
    _ow_user_id_override = user_id
    _discovered_user_id = None


def set_timezone(tz: dt.tzinfo | str | None) -> None:
    """Pin the user-local timezone (tests / wiring); None restores resolution."""
    global _timezone_override
    if isinstance(tz, str):
        tz = zoneinfo.ZoneInfo(tz)
    _timezone_override = tz


def set_energy_engine(engine: Any | None) -> None:
    """Override the cognitive-energy engine (tests inject fakes)."""
    global _energy_engine_override
    _energy_engine_override = engine


def reset_runtime_state() -> None:
    """Clear every override and cache (test teardown)."""
    global _discovered_user_id
    set_settings(None)
    set_ow_client(None)
    set_session_factory(None)
    set_ow_user_id(None)
    set_timezone(None)
    set_energy_engine(None)
    _discovered_user_id = None


def _active_settings() -> Settings:
    return _settings_override if _settings_override is not None else get_settings()


def get_ow_client() -> OWClient:
    """The open-wearables client (override or built from settings)."""
    if _ow_client_override is not None:
        return _ow_client_override
    return OWClient.from_settings(_active_settings())


def _store_session():
    """Commit-on-success session context for the healthmes store."""
    return session_scope(_session_factory_override)


async def _resolve_user_id() -> str:
    """The open-wearables user this single-user deployment reads.

    Order: explicit override -> the shared resolution policy of
    :func:`healthmes.mcp_server.ow_client.resolve_single_user_id`
    (``Settings.ow_user_id`` -> ``HEALTHMES_OW_USER_ID`` env var -> automatic
    discovery, accepted only when the API key sees exactly one user). The
    trigger sweep, energy persist and insight recompute use the same policy,
    so every surface reads the same subject.
    """
    global _discovered_user_id
    if _ow_user_id_override:
        return _ow_user_id_override
    if _discovered_user_id:
        return _discovered_user_id
    try:
        user_id = await resolve_single_user_id(get_ow_client(), _active_settings())
    except LookupError as exc:
        raise ToolError(str(exc)) from exc
    _discovered_user_id = user_id
    return user_id


def _local_timezone() -> dt.tzinfo:
    """The user's local timezone (all tranche-2 joins happen in it).

    Order: explicit override -> ``Settings.timezone`` (IANA name; field
    pending in the shared config, see needs) -> ``HEALTHMES_TIMEZONE`` env
    var -> the machine's local timezone. A configured-but-invalid name is a
    loud error, never a silent UTC fallback (silent guessing corrupts every
    date join).
    """
    if _timezone_override is not None:
        return _timezone_override
    name = getattr(_active_settings(), "timezone", None) or os.environ.get(
        "HEALTHMES_TIMEZONE"
    )
    if name:
        try:
            return zoneinfo.ZoneInfo(str(name))
        except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
            raise ToolError(
                f"Configured timezone {name!r} is not a valid IANA name "
                "(HEALTHMES_TIMEZONE, e.g. 'Asia/Seoul')."
            ) from exc
    return system_timezone()


def _build_energy_engine() -> Any:
    """The cognitive-energy engine (override, or built from runtime state).

    The engine import is deferred so importing the MCP server never pulls the
    engine stack (mirrors the engine's own deferred import of this module).
    When a test injected an ow client, the engine's reader reuses it — same
    MockTransport, no network.
    """
    if _energy_engine_override is not None:
        return _energy_engine_override
    from healthmes.engine.cognitive_energy import CognitiveEnergyEngine, OwEnergyReader

    settings = _active_settings()
    reader = (
        OwEnergyReader(settings, client=_ow_client_override)
        if _ow_client_override is not None
        else None
    )
    return CognitiveEnergyEngine(
        settings, session_factory=_session_factory_override, ow_reader=reader
    )


# ---------------------------------------------------------------------------
# Parsing / serialization helpers
# ---------------------------------------------------------------------------


def _today_local() -> dt.date:
    """Today in the user's local timezone — the single 'today' of this server.

    Every tool defaults its date anchor here so one briefing can never mix
    two different 'todays' (at 07:00 Asia/Seoul, UTC-today is still
    yesterday's local date).
    """
    return dt.datetime.now(_local_timezone()).date()


def _parse_date_local(value: str | None, field: str, tz: dt.tzinfo) -> dt.date:
    """Parse an ISO date, defaulting to *today in the user's local timezone*."""
    if value is None:
        return dt.datetime.now(tz).date()
    return _parse_date(value, field)


def _local_day_bounds_utc(day: dt.date, tz: dt.tzinfo) -> tuple[dt.datetime, dt.datetime]:
    """UTC instants of the local day's [00:00, next 00:00) span."""
    start_local = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
    end_local = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=tz)
    return start_local.astimezone(dt.UTC), end_local.astimezone(dt.UTC)


def _ensure_utc_dt(value: dt.datetime) -> dt.datetime:
    """Aware datetimes go to UTC; naive ones (sqlite reads) are already UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _parse_date(value: str | None, field: str) -> dt.date:
    if value is None:
        return _today_local()
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ToolError(f"{field} must be an ISO date (YYYY-MM-DD), got {value!r}") from exc


def _parse_datetime_utc(value: str, field: str) -> dt.datetime:
    """Parse ISO-8601 into an aware UTC datetime (naive input is assumed UTC)."""
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolError(f"{field} must be an ISO-8601 datetime, got {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _parse_range_days(value: str, field: str = "range", max_days: int = MAX_RANGE_DAYS) -> int:
    """Parse a trailing/forward window like ``'7d'`` (also accepts ``'today'``)."""
    if value == "today":
        return 1
    match = _RANGE_PATTERN.match(value.strip())
    if not match:
        raise ToolError(f"{field} must look like '7d' (1-{max_days} days), got {value!r}")
    days = int(match.group(1))
    if not 1 <= days <= max_days:
        raise ToolError(f"{field} must be between 1 and {max_days} days, got {days}")
    return days


def _parse_uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ToolError(f"{field} must be a UUID, got {value!r}") from exc


def _iso_utc(value: dt.datetime | None) -> str | None:
    """Serialize a stored datetime as UTC ISO-8601 (naive values are UTC)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).isoformat()


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


# ---------------------------------------------------------------------------
# open-wearables row digestion — shared pure functions living in
# healthmes/mcp_server/interpret.py (also consumed by the cognitive-energy
# engine and the trigger sweep; never duplicated, never reached into as
# another module's privates). The local names below are kept for this
# module's own call sites.
# ---------------------------------------------------------------------------

_parse_recorded_at = interpret.parse_recorded_at
_as_float = interpret.as_float
_localized = interpret.localized
_score_points = interpret.score_points
_resilience_score_points = interpret.resilience_score_points
_sleep_score_series = interpret.sleep_score_series
_summary_daily_values = interpret.summary_daily_values


async def _fetch_health_scores(
    user_id: str, start: dt.date, end_exclusive: dt.date
) -> list[dict[str, Any]]:
    rows, _truncated = await _fetch_health_scores_tracked(user_id, start, end_exclusive)
    return rows


async def _fetch_health_scores_tracked(
    user_id: str, start: dt.date, end_exclusive: dt.date
) -> tuple[list[dict[str, Any]], bool]:
    client = get_ow_client()
    return await client.collect_health_scores_tracked(
        user_id, start_date=start.isoformat(), end_date=end_exclusive.isoformat()
    )


def _ow_error(exc: Exception) -> ToolError:
    return ToolError(f"open-wearables API error: {exc}")


def _with_ow_errors(
    fn: Callable[..., Awaitable[dict[str, Any]]],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Map client/transport failures to ToolError without hiding tool bugs."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return await fn(*args, **kwargs)
        except (OWClientError, httpx.HTTPError) as exc:
            raise _ow_error(exc) from exc

    return wrapper


# ---------------------------------------------------------------------------
# Health tools (open-wearables + deterministic interpretation)
# ---------------------------------------------------------------------------


@mcp.tool
@_with_ow_errors
async def get_health_scores(
    range: str = "14d",
    categories: list[str] | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Interpreted health scores per category+provider over a trailing window.

    Fills the vendor-MCP gap (STRESS / BODY_BATTERY / READINESS / RECOVERY plus
    the internal sleep and resilience scores) with per-group latest value,
    qualifier, mean/min/max, days_with_data, coverage and confidence — not raw
    rows. `range` is a trailing window like '7d' or '14d' ending on `end_date`
    (default today in the user's timezone). `categories` defaults to the gap set; valid values:
    sleep, recovery, readiness, activity, stress, resilience, body_battery,
    strain. Note scale differences per provider (e.g. polar readiness is 0-10);
    for `resilience:internal` groups the reported score is the 0-100
    resilience score and `latest_hrv_cv` carries the raw coefficient of
    variation.
    """
    days = _parse_range_days(range)
    end_day = _parse_date(end_date, "end_date")
    start_day = end_day - dt.timedelta(days=days - 1)

    requested = tuple(categories) if categories else DEFAULT_SCORE_CATEGORIES
    invalid = sorted(set(requested) - SCORE_CATEGORIES)
    if invalid:
        raise ToolError(
            f"Unknown categories {invalid}; valid: {sorted(SCORE_CATEGORIES)}"
        )

    user_id = await _resolve_user_id()
    rows, truncated = await _fetch_health_scores_tracked(
        user_id, start_day, end_day + dt.timedelta(days=1)
    )

    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        category = row.get("category")
        if category not in requested:
            continue
        provider = row.get("provider") or "unknown"
        recorded_at = _parse_recorded_at(row.get("recorded_at"))
        if recorded_at is None or not (start_day <= recorded_at.date() <= end_day):
            continue
        is_internal_resilience = category == "resilience" and provider == "internal"
        if is_internal_resilience:
            components = row.get("components") or {}
            value = _as_float((components.get("resilience_score") or {}).get("value"))
        else:
            value = _as_float(row.get("value"))
        if value is None:
            continue
        key = f"{category}:{provider}"
        group = groups.setdefault(
            key,
            {
                "category": category,
                "provider": provider,
                "points": [],
                "latest": None,
                "latest_at": None,
                "value_kind": (
                    "resilience_score_0_100" if is_internal_resilience else "provider_value"
                ),
            },
        )
        group["points"].append((recorded_at, value))
        if group["latest_at"] is None or recorded_at > group["latest_at"]:
            group["latest_at"] = recorded_at
            latest: dict[str, Any] = {
                "date": recorded_at.date().isoformat(),
                "value": round(value, 2),
                "qualifier": row.get("qualifier"),
            }
            if is_internal_resilience:
                latest["hrv_cv"] = _as_float(row.get("value"))
            group["latest"] = latest

    scores: dict[str, dict[str, Any]] = {}
    for key, group in sorted(groups.items()):
        by_day = interpret.daily_series(group["points"], how="mean")
        values = [value for _, value in group["points"]]
        scores[key] = {
            "category": group["category"],
            "provider": group["provider"],
            "value_kind": group["value_kind"],
            "latest": group["latest"],
            "mean": round(sum(values) / len(values), 2),
            "min": round(min(values), 2),
            "max": round(max(values), 2),
            "n_samples": len(values),
            "days_with_data": len(by_day),
            "coverage": interpret.coverage_ratio(len(by_day), days),
            "confidence": interpret.confidence_label(len(by_day), days),
        }

    best_days = max(
        (group["days_with_data"] for group in scores.values()),
        default=0,
    )
    enough_data = bool(scores) and best_days >= min(MIN_AGG_DAYS_WITH_DATA, days)
    return {
        "status": (
            interpret.STATUS_OK
            if enough_data and not truncated
            else interpret.STATUS_INSUFFICIENT
        ),
        "window": {
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
            "days": days,
        },
        "categories_requested": list(requested),
        "scores": scores,
        "truncated": truncated,
    }


@mcp.tool
@_with_ow_errors
async def get_daily_readiness_context(date: str | None = None) -> dict[str, Any]:
    """"Can I push hard today?" — deterministic readiness context for one day.

    Returns interpreted blocks, each with its own status/confidence: sleep_debt
    (trailing 7 nights of the OW internal sleep score), hrv (last night vs the
    personal 14-day trailing-median baseline, z-score; RMSSD and SDNN are never
    mixed), stress (native Garmin stress, else the internal resilience proxy),
    charge (latest body_battery/readiness/recovery scores), and yesterday_load
    (workout count/minutes). Blocks honestly report insufficient_data instead
    of guessing; overall confidence is the weakest confirmed block. `date` is
    ISO YYYY-MM-DD, default today in the user's timezone.
    """
    tz = _local_timezone()
    as_of = _parse_date_local(date, "date", tz)
    fetch_start = as_of - dt.timedelta(days=interpret.BASELINE_WINDOW_DAYS + 7)
    end_exclusive = as_of + dt.timedelta(days=1)
    user_id = await _resolve_user_id()
    client = get_ow_client()

    score_rows = await _fetch_health_scores(user_id, fetch_start, end_exclusive)
    sleep_rows = await client.collect_sleep_summaries(
        user_id, fetch_start.isoformat(), end_exclusive.isoformat()
    )
    workout_rows = await client.collect_workouts(
        user_id, (as_of - dt.timedelta(days=1)).isoformat(), as_of.isoformat()
    )

    # --- sleep debt (internal sleep score; algorithms/sleep.py, never reinvented)
    internal_scores = interpret.daily_series(
        _localized(_score_points(score_rows, "sleep", provider="internal"), tz),
        how="max",
    )
    if internal_scores:
        sleep_block = interpret.sleep_debt(internal_scores, as_of)
        sleep_block["source"] = "internal_sleep_score"
    else:
        sleep_block = {
            "status": interpret.STATUS_INSUFFICIENT,
            "reason": "no_internal_sleep_score",
            "confidence": "low",
        }

    # --- nocturnal HRV vs 14-day baseline (variant-separated, never mixed)
    rmssd = _summary_daily_values(sleep_rows, "avg_hrv_rmssd_ms", as_of)
    sdnn = _summary_daily_values(sleep_rows, "avg_hrv_sdnn_ms", as_of)
    if rmssd or sdnn:
        variant = "rmssd" if len(rmssd) >= len(sdnn) else "sdnn"
        series = rmssd if variant == "rmssd" else sdnn
        hrv_block = interpret.metric_baseline(
            series,
            as_of,
            max_stale_days=interpret.HRV_MAX_STALE_DAYS,
        )
        hrv_block["variant"] = variant
        hrv_block["unit"] = "ms"
        hrv_block["source"] = "nocturnal (sleep summaries)"
    else:
        hrv_block = {
            "status": interpret.STATUS_INSUFFICIENT,
            "reason": "no_nocturnal_hrv_data",
            "confidence": "low",
        }

    # --- stress (Garmin native, else internal resilience proxy)
    garmin_stress = interpret.daily_series(
        _localized(_score_points(score_rows, "stress", provider="garmin"), tz),
        how="mean",
    )
    resilience = interpret.daily_series(
        _localized(_resilience_score_points(score_rows), tz),
        how="latest",
    )
    stress_block = interpret.stress_context(garmin_stress, resilience, as_of)

    # --- charge scores (freshest body_battery / readiness / recovery)
    charge_entries: list[dict[str, Any]] = []
    for category in CHARGE_CATEGORIES:
        candidates = [
            (recorded_at, value, row)
            for row in score_rows
            if row.get("category") == category
            for recorded_at, value in _localized(
                _score_points([row], category),
                tz,
            )
            if (as_of - recorded_at.date()).days in (0, 1)
        ]
        if not candidates:
            continue
        recorded_at, value, row = max(candidates, key=lambda item: item[0])
        charge_entries.append(
            {
                "category": category,
                "provider": row.get("provider") or "unknown",
                "value": round(value, 1),
                "qualifier": row.get("qualifier"),
                "observed_on": recorded_at.date().isoformat(),
            }
        )
    if charge_entries:
        freshest = max(entry["observed_on"] for entry in charge_entries)
        charge_block: dict[str, Any] = {
            "status": interpret.STATUS_OK,
            "entries": charge_entries,
            "confidence": "high" if freshest == as_of.isoformat() else "medium",
        }
    else:
        charge_block = {
            "status": interpret.STATUS_INSUFFICIENT,
            "reason": "no_body_battery_readiness_or_recovery_scores",
            "confidence": "low",
        }

    # --- previous-day training load
    yesterday = as_of - dt.timedelta(days=1)
    durations = [_as_float(row.get("duration_seconds")) or 0.0 for row in workout_rows]
    calories = [c for row in workout_rows if (c := _as_float(row.get("calories_kcal")))]
    avg_hrs = [h for row in workout_rows if (h := _as_float(row.get("avg_heart_rate_bpm")))]
    load_block: dict[str, Any] = {
        "status": interpret.STATUS_OK,
        "date": yesterday.isoformat(),
        "workouts": len(workout_rows),
        "total_minutes": round(sum(durations) / 60.0, 1),
        "total_calories_kcal": round(sum(calories), 1) if calories else None,
        "max_avg_heart_rate_bpm": max(avg_hrs) if avg_hrs else None,
        "types": sorted({str(row.get("type")) for row in workout_rows if row.get("type")}),
        "confidence": "high" if workout_rows else "medium",
    }

    core_blocks = (sleep_block, hrv_block, stress_block, charge_block)
    any_ok = any(block.get("status") == interpret.STATUS_OK for block in core_blocks)
    return {
        "status": interpret.STATUS_OK if any_ok else interpret.STATUS_INSUFFICIENT,
        "date": as_of.isoformat(),
        "baseline_window_days": interpret.BASELINE_WINDOW_DAYS,
        "confidence": interpret.overall_confidence(core_blocks),
        "sleep_debt": sleep_block,
        "hrv": hrv_block,
        "stress": stress_block,
        "charge": charge_block,
        "yesterday_load": load_block,
    }


# Metric registry for get_personal_baselines: name -> (source, field, unit).
_BASELINE_METRICS: dict[str, tuple[str, str, str]] = {
    "hrv_rmssd_ms": ("sleep_summaries", "avg_hrv_rmssd_ms", "ms"),
    "hrv_sdnn_ms": ("sleep_summaries", "avg_hrv_sdnn_ms", "ms"),
    "sleep_duration_minutes": ("sleep_summaries", "duration_minutes", "minutes"),
    "sleep_efficiency_percent": ("sleep_summaries", "efficiency_percent", "percent"),
    "resting_heart_rate_bpm": ("recovery_summaries", "resting_heart_rate_bpm", "bpm"),
    "sleep_score": ("health_scores", "sleep", "score_0_100"),
    "stress": ("health_scores", "stress", "score_0_100"),
}
DEFAULT_BASELINE_METRICS = (
    "hrv_rmssd_ms",
    "resting_heart_rate_bpm",
    "sleep_duration_minutes",
    "sleep_score",
)


@mcp.tool
@_with_ow_errors
async def get_personal_baselines(
    metrics: list[str] | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Personal 14-day and 90-day baselines with the current deviation.

    For each metric: current daily value, 14-day trailing-median baseline with
    delta / delta_pct / z_score, plus a 90-day median for slow drift — each
    with n_days, coverage and confidence, or an honest insufficient_data.
    Supported metrics: hrv_rmssd_ms, hrv_sdnn_ms, sleep_duration_minutes,
    sleep_efficiency_percent, resting_heart_rate_bpm, sleep_score, stress.
    `as_of` is ISO YYYY-MM-DD (default today in the user's timezone).
    """
    requested = tuple(metrics) if metrics else DEFAULT_BASELINE_METRICS
    unknown = sorted(set(requested) - set(_BASELINE_METRICS))
    if unknown:
        raise ToolError(
            f"Unknown metrics {unknown}; supported: {sorted(_BASELINE_METRICS)}"
        )
    tz = _local_timezone()
    anchor = _parse_date_local(as_of, "as_of", tz)
    fetch_start = anchor - dt.timedelta(days=interpret.LONG_BASELINE_WINDOW_DAYS + 1)
    end_exclusive = anchor + dt.timedelta(days=1)
    user_id = await _resolve_user_id()
    client = get_ow_client()

    needed_sources = {_BASELINE_METRICS[name][0] for name in requested}
    sleep_rows: list[dict[str, Any]] = []
    recovery_rows: list[dict[str, Any]] = []
    score_rows: list[dict[str, Any]] = []
    if "sleep_summaries" in needed_sources:
        sleep_rows = await client.collect_sleep_summaries(
            user_id, fetch_start.isoformat(), end_exclusive.isoformat()
        )
    if "recovery_summaries" in needed_sources:
        recovery_rows = await client.collect_recovery_summaries(
            user_id, fetch_start.isoformat(), end_exclusive.isoformat()
        )
    if "health_scores" in needed_sources:
        score_rows = await _fetch_health_scores(user_id, fetch_start, end_exclusive)

    results: dict[str, dict[str, Any]] = {}
    for name in requested:
        source, field, unit = _BASELINE_METRICS[name]
        source_label = f"{source}.{field}"
        if source == "sleep_summaries":
            daily = _summary_daily_values(sleep_rows, field, anchor)
        elif source == "recovery_summaries":
            daily = _summary_daily_values(recovery_rows, field, anchor)
        elif field == "sleep":
            daily, sleep_source = _sleep_score_series(score_rows, tz=tz)
            source_label = sleep_source or "health_scores.sleep"
        else:  # stress: Garmin native, else internal resilience proxy
            garmin_daily = interpret.daily_series(
                _localized(_score_points(score_rows, "stress", provider="garmin"), tz),
                how="mean",
            )
            resilience_daily = interpret.daily_series(
                _localized(_resilience_score_points(score_rows), tz),
                how="latest",
            )
            proxy_daily = {
                day: max(0.0, min(100.0, 100.0 - score))
                for day, score in resilience_daily.items()
            }
            daily, which = interpret.choose_stress_series(
                garmin_daily,
                proxy_daily,
                anchor,
            )
            source_label = {
                "garmin": "health_scores.stress(garmin)",
                "proxy": "internal_resilience_proxy(100-resilience_score)",
                "none": "health_scores.stress(garmin)",
            }[which]

        entry = interpret.metric_baseline(daily, anchor)
        entry["unit"] = unit
        entry["source"] = source_label
        current = entry.get("current")
        if current:
            current_day = dt.date.fromisoformat(current["date"])
            window_start = current_day - dt.timedelta(days=interpret.LONG_BASELINE_WINDOW_DAYS)
            history_90 = [
                value for day, value in daily.items() if window_start <= day < current_day
            ]
            if len(history_90) >= interpret.MIN_BASELINE_DAYS:
                median_90 = interpret.trailing_median(history_90)
                assert median_90 is not None
                entry["baseline_90d"] = {
                    "median": round(median_90, 2),
                    "n_days": len(history_90),
                    "window_days": interpret.LONG_BASELINE_WINDOW_DAYS,
                }
        results[name] = entry

    any_ok = any(entry.get("status") == interpret.STATUS_OK for entry in results.values())
    return {
        "status": interpret.STATUS_OK if any_ok else interpret.STATUS_INSUFFICIENT,
        "as_of": anchor.isoformat(),
        "metrics": results,
    }


# ---------------------------------------------------------------------------
# Tranche-2 tools: energy forecast / stress timeline / impact comparison
# (docs/PLAN.md Phase 2 — all joins in the user's local timezone)
# ---------------------------------------------------------------------------

# The vendor series type carrying intraday stress (schemas/enums/series_types.py;
# Garmin is the only provider that ships one — docs/PLAN.md 1.5).
STRESS_SERIES_TYPE = "garmin_stress_level"

# Component terms sourced from open-wearables health data; how many of them a
# forecast window carries grades the forecast's evidence richness.
_OW_FACTOR_TERMS = frozenset(
    {"sleep_debt_penalty", "stress_penalty", "hrv_deviation_penalty", "body_battery_bonus"}
)
_CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}


def _weakest_confidence(*labels: str) -> str:
    return min(labels, key=lambda label: _CONFIDENCE_ORDER.get(label, 0))


def _serialize_energy_window(slot: Any, tz: dt.tzinfo) -> dict[str, Any]:
    """Compact wire shape of one engine WindowSlot (local times, no raw evidence)."""
    components = [
        {
            "name": item.get("name"),
            "kind": item.get("kind"),
            "weight": round(item["weight"], 4) if item.get("weight") is not None else None,
            "contribution": round(float(item.get("contribution", 0.0)), 2),
        }
        for item in slot.components
    ]
    return {
        "start": slot.window_start.astimezone(tz).isoformat(),
        "end": slot.window_end.astimezone(tz).isoformat(),
        "source": slot.source,
        "status": slot.status,
        "score": slot.score,
        "components": components,
    }


@mcp.tool
async def get_cognitive_energy_forecast(date: str | None = None) -> dict[str, Any]:
    """"When is deep work today?" — hourly cognitive-energy scores for one day.

    Returns every hourly window of the given **local** day (`date` is ISO
    YYYY-MM-DD, default today in the user's timezone): windows the hourly
    persist job already stored come back verbatim (`source: "persisted"`),
    missing hours are computed on demand by the deterministic rule engine
    (`source: "computed"`; future hours drop the app-fragmentation term).
    Each window carries `score` (0-100, higher = more cognitive energy) and
    its weighted `components` (sleep debt / stress / HRV deviation / charge /
    meeting load / fragmentation — contributions sum to the score). `summary`
    names the best and worst windows; `confidence` is the weaker of window
    coverage and health-factor richness, with both sub-labels reported.
    Windows without any usable signal are honest `insufficient_data`.
    """
    tz = _local_timezone()
    day = _parse_date_local(date, "date", tz)
    start_utc, end_utc = _local_day_bounds_utc(day, tz)

    engine = _build_energy_engine()
    utc_days = sorted({start_utc.date(), (end_utc - dt.timedelta(microseconds=1)).date()})
    slots: list[Any] = []
    for utc_day in utc_days:
        # forecast_day is sync and may drive an async reader via asyncio.run;
        # a worker thread keeps that off this (running) event loop.
        slots.extend(await asyncio.to_thread(engine.forecast_day, utc_day))

    windows = sorted(
        (slot for slot in slots if start_utc <= slot.window_start < end_utc),
        key=lambda slot: slot.window_start,
    )
    ok_windows = [slot for slot in windows if slot.status == interpret.STATUS_OK]
    factors_present = sorted(
        {
            str(item.get("name"))
            for slot in ok_windows
            for item in slot.components
            if item.get("name") in _OW_FACTOR_TERMS
        }
    )
    coverage_label = interpret.confidence_label(len(ok_windows), len(windows))
    richness_label = (
        "high"
        if len(factors_present) >= 3
        else "medium"
        if len(factors_present) == 2
        else "low"
    )

    def _extreme(pick: Callable[..., Any]) -> Any | None:
        if not ok_windows:
            return None
        return pick(ok_windows, key=lambda slot: (slot.score, slot.window_start))

    best = _extreme(max)
    worst = _extreme(min)

    def _peak(slot: Any | None) -> dict[str, Any] | None:
        if slot is None:
            return None
        return {"start": slot.window_start.astimezone(tz).isoformat(), "score": slot.score}

    return {
        "status": interpret.STATUS_OK if ok_windows else interpret.STATUS_INSUFFICIENT,
        "date": day.isoformat(),
        "timezone": str(tz),
        "baseline_window_days": interpret.BASELINE_WINDOW_DAYS,
        "confidence": _weakest_confidence(coverage_label, richness_label)
        if ok_windows
        else "low",
        "confidence_detail": {
            "window_coverage": coverage_label,
            "health_factor_richness": richness_label,
        },
        "summary": {
            "windows_total": len(windows),
            "windows_ok": len(ok_windows),
            "best": _peak(best),
            "worst": _peak(worst),
            "health_factors_present": factors_present,
        },
        "windows": [_serialize_energy_window(slot, tz) for slot in windows],
    }


def _stress_samples(rows: Iterable[Mapping[str, Any]]) -> list[tuple[dt.datetime, float]]:
    """(aware UTC timestamp, value) stress samples from timeseries rows.

    Negative values are Garmin's "unmeasurable" markers; the vendor ingest
    already drops them (providers/garmin/data_247.py) — filtered again here
    defensively.
    """
    samples: list[tuple[dt.datetime, float]] = []
    for row in rows:
        if row.get("type") != STRESS_SERIES_TYPE:
            continue
        recorded_at = _parse_recorded_at(row.get("timestamp"))
        value = _as_float(row.get("value"))
        if recorded_at is None or value is None or value < 0:
            continue
        samples.append((recorded_at, value))
    return samples


@mcp.tool
@_with_ow_errors
async def get_stress_timeline(date: str | None = None) -> dict[str, Any]:
    """"When and why was I stressed?" — labeled stress intervals for one day.

    Joins the day's stress series with mirrored calendar events and app-usage
    sessions in the **user's local timezone** and returns interpreted
    intervals only (never a raw series): each interval has a local `window`,
    a `stress_level` band (Garmin bands: rest 0-25 / low 26-50 / medium 51-75
    / high 76-100) with mean/peak, and `likely_context` — overlapping
    calendar event titles plus dominant app categories by foreground time.
    Uses the Garmin stress timeseries when present; otherwise falls back to a
    day-level proxy (native daily stress score, else 100 - the night-HRV
    resilience score) spread over morning/afternoon/evening sections — the
    proxy has no intraday resolution, so treat within-day differences as
    context, not measurement. `date` is ISO YYYY-MM-DD (default today,
    local). Honest `insufficient_data` when no stress signal exists at all.
    """
    tz = _local_timezone()
    day = _parse_date_local(date, "date", tz)
    start_utc, end_utc = _local_day_bounds_utc(day, tz)
    user_id = await _resolve_user_id()
    client = get_ow_client()

    series_rows, series_truncated = await client.collect_timeseries_tracked(
        user_id, start_utc.isoformat(), end_utc.isoformat(), [STRESS_SERIES_TYPE]
    )
    samples_utc = [
        (recorded_at, value)
        for recorded_at, value in _stress_samples(series_rows)
        if start_utc <= recorded_at < end_utc
    ]

    if samples_utc and len(samples_utc) < MIN_TIMELINE_SAMPLES and not series_truncated:
        return {
            "status": interpret.STATUS_INSUFFICIENT,
            "date": day.isoformat(),
            "timezone": str(tz),
            "reason": "insufficient_stress_samples",
            "confidence": "low",
            "truncated": False,
            "intervals": [],
        }
    if series_truncated and not samples_utc:
        return {
            "status": interpret.STATUS_INSUFFICIENT,
            "date": day.isoformat(),
            "timezone": str(tz),
            "reason": "stress_timeseries_truncated",
            "confidence": "low",
            "coverage": 0.0,
            "truncated": True,
            "intervals": [],
        }

    day_level: dict[str, Any] | None = None
    coverage: float | None = None
    if samples_utc:
        intervals = timeline.build_stress_intervals(
            [(recorded_at.astimezone(tz), value) for recorded_at, value in samples_utc]
        )
        source = "garmin_stress_timeseries"
        coverage = timeline.day_coverage(intervals)
        confidence = timeline.timeline_confidence(coverage)
    else:
        score_rows = await _fetch_health_scores(
            user_id,
            day - dt.timedelta(days=interpret.STRESS_MAX_STALE_DAYS),
            day + dt.timedelta(days=1),
        )
        garmin_daily = interpret.daily_series(
            _localized(_score_points(score_rows, "stress", provider="garmin"), tz),
            how="mean",
        )
        resilience_daily = interpret.daily_series(
            _localized(_resilience_score_points(score_rows), tz), how="latest"
        )
        context = interpret.stress_context(garmin_daily, resilience_daily, day)
        if context.get("status") != interpret.STATUS_OK:
            return {
                "status": interpret.STATUS_INSUFFICIENT,
                "date": day.isoformat(),
                "timezone": str(tz),
                "reason": str(
                    context.get("reason", "no_stress_timeseries_and_no_daily_proxy")
                ),
                "confidence": "low",
                "truncated": False,
                "intervals": [],
            }
        intervals = timeline.proxy_sections(day, tz, float(context["value"]))
        source = (
            "night_hrv_resilience_proxy"
            if context.get("source") == "internal_resilience_proxy"
            else "garmin_daily_stress_score"
        )
        # A day-level value spread over sections is never more than medium.
        raw_confidence = str(context.get("confidence", "low"))
        confidence = _weakest_confidence(raw_confidence, "medium")
        day_level = {
            "value": context["value"],
            "observed_on": context["observed_on"],
            "stale_days": context["stale_days"],
        }

    with _store_session() as session:
        event_rows = session.scalars(
            select(CalendarEventMirror)
            .where(
                CalendarEventMirror.start_at < end_utc,
                CalendarEventMirror.end_at > start_utc,
            )
            .order_by(CalendarEventMirror.start_at)
        ).all()
        usage_rows = session.scalars(
            select(AppUsageSample)
            .where(
                AppUsageSample.bucket_start >= start_utc - dt.timedelta(minutes=60),
                AppUsageSample.bucket_start < end_utc,
            )
            .order_by(AppUsageSample.bucket_start)
        ).all()
        events = [
            (
                _ensure_utc_dt(row.start_at).astimezone(tz),
                _ensure_utc_dt(row.end_at).astimezone(tz),
                row.summary,
            )
            for row in event_rows
        ]
        usage = [
            (
                _ensure_utc_dt(row.bucket_start).astimezone(tz),
                row.category,
                row.foreground_seconds,
            )
            for row in usage_rows
        ]

    interval_payload = [
        timeline.serialize_interval(
            interval,
            timeline.attach_context(interval.start, interval.end, events, usage),
        )
        for interval in intervals
    ]
    response = {
        "status": interpret.STATUS_OK,
        "date": day.isoformat(),
        "timezone": str(tz),
        "source": source,
        "coverage": coverage,
        "confidence": confidence,
        "day_level_stress": day_level,
        "truncated": series_truncated,
        "intervals": interval_payload,
    }
    if series_truncated:
        response["status"] = interpret.STATUS_INSUFFICIENT
        response["reason"] = "stress_timeseries_truncated"
        response["confidence"] = "low"
    return response


# Metric registry for compare_impact: how each metric is measured around an
# occurrence. "nightly" metrics compare the night after the occurrence day
# (date D+1 — the vendor stamps nights at wake time) with the night before
# (date D); "intraday" metrics compare the series mean in the hours before vs
# after the occurrence.
_IMPACT_METRICS: dict[str, dict[str, Any]] = {
    "stress": {
        "kind": "intraday",
        "unit": "stress_score_0_100",
        "higher_is_better": False,
        "source": f"timeseries.{STRESS_SERIES_TYPE}",
    },
    "sleep_score": {
        "kind": "nightly",
        "unit": "score_0_100",
        "higher_is_better": True,
        "source": "health_scores.sleep",
    },
    "hrv": {
        "kind": "nightly",
        "unit": "ms",
        "higher_is_better": True,
        "source": "sleep_summaries.avg_hrv_*_ms",
    },
    "sleep_duration_minutes": {
        "kind": "nightly",
        "unit": "minutes",
        "higher_is_better": True,
        "source": "sleep_summaries.duration_minutes",
    },
    "resting_heart_rate_bpm": {
        "kind": "nightly",
        "unit": "bpm",
        "higher_is_better": False,
        "source": "recovery_summaries.resting_heart_rate_bpm",
    },
}
IMPACT_PRE_POST_HOURS = 2
IMPACT_MIN_SIDE_SAMPLES = 3
IMPACT_MAX_OCCURRENCES = 30
IMPACT_MIN_FACTOR_LENGTH = 2
IMPACT_MAX_EXAMPLES = 3


def _collect_store_occurrences(
    factor: str, start_utc: dt.datetime, end_utc: dt.datetime
) -> tuple[list[impact.Occurrence], dict[str, int]]:
    """Factor occurrences from the healthmes store (food / calendar / done tasks)."""
    occurrences: list[impact.Occurrence] = []
    counts = {"food_log": 0, "calendar": 0, "task": 0}
    with _store_session() as session:
        for row in session.scalars(
            select(FoodLog).where(
                FoodLog.logged_at >= start_utc, FoodLog.logged_at < end_utc
            )
        ):
            if impact.matches(factor, row.description):
                at = _ensure_utc_dt(row.logged_at)
                occurrences.append(
                    impact.Occurrence("food_log", row.description[:80], at, at)
                )
                counts["food_log"] += 1
        for row in session.scalars(
            select(CalendarEventMirror).where(
                CalendarEventMirror.start_at >= start_utc,
                CalendarEventMirror.start_at < end_utc,
            )
        ):
            if impact.matches(factor, row.summary):
                occurrences.append(
                    impact.Occurrence(
                        "calendar",
                        (row.summary or "(untitled)")[:80],
                        _ensure_utc_dt(row.start_at),
                        _ensure_utc_dt(row.end_at),
                    )
                )
                counts["calendar"] += 1
        for row in session.scalars(
            select(Task).where(
                Task.status == "done",
                Task.updated_at >= start_utc,
                Task.updated_at < end_utc,
            )
        ):
            if impact.matches(factor, row.title):
                at = _ensure_utc_dt(row.updated_at)
                occurrences.append(impact.Occurrence("task", row.title[:80], at, at))
                counts["task"] += 1
    return occurrences, counts


async def _collect_workout_occurrences(
    client: OWClient,
    user_id: str,
    factor: str,
    start_utc: dt.datetime,
    end_utc: dt.datetime,
) -> list[impact.Occurrence]:
    """Factor occurrences among open-wearables workouts (matched on type)."""
    rows = await client.collect_workouts(
        user_id,
        (start_utc - dt.timedelta(days=1)).date().isoformat(),
        (end_utc + dt.timedelta(days=1)).date().isoformat(),
    )
    occurrences: list[impact.Occurrence] = []
    for row in rows:
        workout_type = str(row.get("type") or "")
        if not impact.matches(factor, workout_type):
            continue
        start = _parse_recorded_at(row.get("start_time"))
        if start is None or not (start_utc <= start < end_utc):
            continue
        end = _parse_recorded_at(row.get("end_time")) or start
        occurrences.append(
            impact.Occurrence("workout", workout_type, start, max(end, start))
        )
    return occurrences


async def _nightly_daily_series(
    metric: str,
    client: OWClient,
    user_id: str,
    fetch_start: dt.date,
    fetch_end: dt.date,
    tz: dt.tzinfo,
) -> tuple[dict[dt.date, float], dict[str, Any]]:
    """Daily values of one nightly metric keyed by the user's local dates.

    Summary rows carry the provider's local ``date`` (used as-is — providers
    stamp the user's dates); health-score timestamps are converted to the
    local timezone before keying.
    """
    if metric == "sleep_score":
        score_rows = await _fetch_health_scores(user_id, fetch_start, fetch_end)
        series, source = _sleep_score_series(score_rows, tz=tz)
        return series, {"source": source or "health_scores.sleep"}
    if metric in ("hrv", "sleep_duration_minutes"):
        sleep_rows = await client.collect_sleep_summaries(
            user_id, fetch_start.isoformat(), fetch_end.isoformat()
        )
        if metric == "sleep_duration_minutes":
            series = _summary_daily_values(sleep_rows, "duration_minutes", fetch_end)
            return series, {"source": "sleep_summaries.duration_minutes"}
        rmssd = _summary_daily_values(sleep_rows, "avg_hrv_rmssd_ms", fetch_end)
        sdnn = _summary_daily_values(sleep_rows, "avg_hrv_sdnn_ms", fetch_end)
        variant = "rmssd" if len(rmssd) >= len(sdnn) else "sdnn"
        return (
            rmssd if variant == "rmssd" else sdnn,
            {"source": f"sleep_summaries.avg_hrv_{variant}_ms", "variant": variant},
        )
    recovery_rows = await client.collect_recovery_summaries(
        user_id, fetch_start.isoformat(), fetch_end.isoformat()
    )
    series = _summary_daily_values(recovery_rows, "resting_heart_rate_bpm", fetch_end)
    return series, {"source": "recovery_summaries.resting_heart_rate_bpm"}


async def _intraday_stress_deltas(
    client: OWClient,
    user_id: str,
    occurrences: list[impact.Occurrence],
    tz: dt.tzinfo,
) -> tuple[list[dict[str, Any]], int]:
    """Per-occurrence stress deltas: mean of the 2h after minus the 2h before."""
    rows: list[dict[str, Any]] = []
    skipped = 0
    span = dt.timedelta(hours=IMPACT_PRE_POST_HOURS)
    for occurrence in occurrences:
        pre_start = occurrence.start - span
        post_end = occurrence.end + span
        series_rows, truncated = await client.collect_timeseries_tracked(
            user_id,
            pre_start.isoformat(),
            post_end.isoformat(),
            [STRESS_SERIES_TYPE],
            max_pages=4,
        )
        if truncated:
            skipped += 1
            continue
        samples = _stress_samples(series_rows)
        before = impact.window_mean(
            samples, pre_start, occurrence.start, min_samples=IMPACT_MIN_SIDE_SAMPLES
        )
        after = impact.window_mean(
            samples, occurrence.end, post_end, min_samples=IMPACT_MIN_SIDE_SAMPLES
        )
        if before is None or after is None:
            skipped += 1
            continue
        local_start = occurrence.start.astimezone(tz)
        rows.append(
            {
                "occurred_on": local_start.date().isoformat(),
                "occurred_at": local_start.isoformat(),
                "source": occurrence.source,
                "label": occurrence.label,
                "before": before[0],
                "after": after[0],
                "delta": after[0] - before[0],
            }
        )
    return rows, skipped


@mcp.tool
@_with_ow_errors
async def compare_impact(
    factor: str,
    metric: str,
    window: str = "30d",
    end_date: str | None = None,
) -> dict[str, Any]:
    """"Does X agree with me?" — before/after metric deltas around a factor.

    `factor` is a case-insensitive keyword matched against food-log
    descriptions, calendar event titles, completed task titles, and workout
    types (e.g. 'wine', 'running', '1:1'). `metric` is one of: stress
    (intraday — mean Garmin stress 2h after vs 2h before each occurrence),
    sleep_score, hrv, sleep_duration_minutes, resting_heart_rate_bpm (nightly
    — the night after the occurrence day vs the night before, joined on the
    user's local dates, max one occurrence per day). `window` is a trailing
    span like '30d' ending on `end_date` (default today, local). Returns the
    aggregate only: n, mean delta, stdev spread, min/max, confidence (from
    n), up to 3 examples, and `metric.higher_is_better` for reading the sign.
    Honest `insufficient_data` below 3 paired observations — never guess from
    less. Deltas are observational associations, not causation.
    """
    factor = factor.strip()
    if len(factor) < IMPACT_MIN_FACTOR_LENGTH:
        raise ToolError(
            f"factor must be at least {IMPACT_MIN_FACTOR_LENGTH} characters, got {factor!r}"
        )
    if metric not in _IMPACT_METRICS:
        raise ToolError(
            f"metric must be one of {sorted(_IMPACT_METRICS)}, got {metric!r}"
        )
    days = _parse_range_days(window, "window")
    tz = _local_timezone()
    end_day = _parse_date_local(end_date, "end_date", tz)
    start_day = end_day - dt.timedelta(days=days - 1)
    start_utc, _ = _local_day_bounds_utc(start_day, tz)
    _, end_utc = _local_day_bounds_utc(end_day, tz)

    user_id = await _resolve_user_id()
    client = get_ow_client()

    occurrences, counts = _collect_store_occurrences(factor, start_utc, end_utc)
    workout_occurrences = await _collect_workout_occurrences(
        client, user_id, factor, start_utc, end_utc
    )
    counts["workout"] = len(workout_occurrences)
    occurrences.extend(workout_occurrences)
    occurrences.sort(key=lambda occurrence: occurrence.start)
    total_matched = len(occurrences)

    spec = dict(_IMPACT_METRICS[metric])
    if spec["kind"] == "nightly":
        occurrences_by_day = impact.dedupe_by_local_day(occurrences, tz)
        truncated = len(occurrences_by_day) > IMPACT_MAX_OCCURRENCES
        if truncated:
            recent_days = sorted(occurrences_by_day)[-IMPACT_MAX_OCCURRENCES:]
            occurrences_by_day = {
                day: occurrences_by_day[day] for day in recent_days
            }
        used = len(occurrences_by_day)
        daily, detail = await _nightly_daily_series(
            metric,
            client,
            user_id,
            start_day - dt.timedelta(days=1),
            end_day + dt.timedelta(days=2),
            tz,
        )
        spec.update(detail)
        rows, skipped = impact.nightly_deltas(occurrences_by_day, daily)
    else:
        truncated = total_matched > IMPACT_MAX_OCCURRENCES
        if truncated:  # intraday metrics cap raw occurrences
            occurrences = occurrences[-IMPACT_MAX_OCCURRENCES:]
        used = len(occurrences)
        rows, skipped = await _intraday_stress_deltas(client, user_id, occurrences, tz)

    stats = impact.summarize_deltas([row["delta"] for row in rows])
    base = {
        "factor": factor,
        "metric": {"name": metric, **spec},
        "window": {
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
            "days": days,
            "timezone": str(tz),
        },
        "occurrences": {
            "matched_by_source": counts,
            "total_matched": total_matched,
            "used": used,
            "paired": stats["n"],
            "skipped_no_metric": skipped,
            "truncated": truncated,
        },
    }
    if stats["n"] < impact.MIN_PAIRED_OBSERVATIONS:
        return {
            "status": interpret.STATUS_INSUFFICIENT,
            "reason": (
                f"need_at_least_{impact.MIN_PAIRED_OBSERVATIONS}_paired_observations"
            ),
            **base,
            "confidence": "low",
        }
    effect = {
        key: round(value, 2) if isinstance(value, float) else value
        for key, value in stats.items()
    }
    examples = [
        {
            **row,
            "before": round(row["before"], 2),
            "after": round(row["after"], 2),
            "delta": round(row["delta"], 2),
        }
        for row in rows[:IMPACT_MAX_EXAMPLES]
    ]
    return {
        "status": interpret.STATUS_OK,
        **base,
        "effect": effect,
        "confidence": impact.confidence_from_n(stats["n"]),
        "examples": examples,
        "note": "observational association, not causation",
    }


# ---------------------------------------------------------------------------
# Store tools: tasks / schedule (propose-then-confirm) / food / decisions
# ---------------------------------------------------------------------------


def _serialize_task(task: Task) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "title": task.title,
        "goal_id": str(task.goal_id) if task.goal_id else None,
        "est_minutes": task.est_minutes,
        "deadline": _iso_utc(task.deadline),
        "energy_demand": _enum_value(task.energy_demand),
        "status": task.status,
        "source": _enum_value(task.source),
        "created_at": _iso_utc(task.created_at),
        "updated_at": _iso_utc(task.updated_at),
    }


@mcp.tool
def list_tasks(status: str | None = None, include_done: bool = False) -> dict[str, Any]:
    """List tasks from the HealthMes store, soonest deadline first.

    `status` filters to one of todo / scheduled / in_progress / done /
    cancelled; without it, done and cancelled tasks are hidden unless
    `include_done` is true.
    """
    if status is not None and status not in TASK_STATUSES:
        raise ToolError(f"status must be one of {sorted(TASK_STATUSES)}, got {status!r}")
    with _store_session() as session:
        rows = list(session.scalars(select(Task)))
        if status is not None:
            rows = [task for task in rows if task.status == status]
        elif not include_done:
            rows = [task for task in rows if task.status not in TASK_DONE_STATUSES]
        # Sort on normalized ISO strings: stored datetimes are naive on sqlite
        # but aware on postgres, so raw datetime keys could not be compared.
        rows.sort(
            key=lambda task: (
                task.deadline is None,
                _iso_utc(task.deadline) or "",
                _iso_utc(task.created_at) or "",
            )
        )
        tasks = [_serialize_task(task) for task in rows]
    return {"status": "ok", "count": len(tasks), "tasks": tasks}


@mcp.tool
def upsert_task(
    task_id: str | None = None,
    title: str | None = None,
    goal_id: str | None = None,
    est_minutes: int | None = None,
    deadline: str | None = None,
    energy_demand: str | None = None,
    status: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Create a task (omit `task_id`) or update the given fields of one.

    `energy_demand` is low / med / high ('medium' is accepted as med) and
    drives schedule placement; `status` is todo / scheduled / in_progress /
    done / cancelled ('scheduled' = its block was placed via the
    propose-then-confirm gate); `source` is user / agent (default agent —
    pass 'user' when relaying a task the user stated). `deadline` is
    ISO-8601; a date-only deadline is stored as midnight UTC of that date.
    `est_minutes` must be positive. Only provided fields change on update.
    """
    if energy_demand is not None:
        energy_demand = "med" if energy_demand == "medium" else energy_demand
        if energy_demand not in {e.value for e in EnergyDemand}:
            raise ToolError(
                f"energy_demand must be one of ['low', 'med', 'high'], got {energy_demand!r}"
            )
    if status is not None and status not in TASK_STATUSES:
        raise ToolError(f"status must be one of {sorted(TASK_STATUSES)}, got {status!r}")
    if source is not None and source not in {s.value for s in TaskSource}:
        raise ToolError(f"source must be one of ['user', 'agent'], got {source!r}")
    if est_minutes is not None and est_minutes <= 0:
        raise ToolError(f"est_minutes must be positive, got {est_minutes}")
    deadline_dt = _parse_datetime_utc(deadline, "deadline") if deadline is not None else None
    goal_uuid = _parse_uuid(goal_id, "goal_id") if goal_id is not None else None

    with _store_session() as session:
        if goal_uuid is not None and session.get(WeeklyGoal, goal_uuid) is None:
            raise ToolError(f"weekly_goal {goal_id} not found")

        if task_id is not None:
            task = session.get(Task, _parse_uuid(task_id, "task_id"))
            if task is None:
                raise ToolError(f"task {task_id} not found")
            if title is not None:
                if not title.strip():
                    raise ToolError("title must not be empty")
                task.title = title
            if goal_uuid is not None:
                task.goal_id = goal_uuid
            if est_minutes is not None:
                task.est_minutes = est_minutes
            if deadline_dt is not None:
                task.deadline = deadline_dt
            if energy_demand is not None:
                task.energy_demand = EnergyDemand(energy_demand)
            if status is not None:
                task.status = status
            if source is not None:
                task.source = TaskSource(source)
            created = False
        else:
            if title is None or not title.strip():
                raise ToolError("title is required to create a task")
            task = Task(
                title=title,
                goal_id=goal_uuid,
                est_minutes=est_minutes,
                deadline=deadline_dt,
                energy_demand=EnergyDemand(energy_demand) if energy_demand else EnergyDemand.MED,
                status=status or "todo",
                source=TaskSource(source) if source else TaskSource.AGENT,
            )
            session.add(task)
            created = True
        session.flush()
        payload = _serialize_task(task)
    return {"status": "ok", "created": created, "task": payload}


@mcp.tool
def get_schedule(range: str = "7d") -> dict[str, Any]:
    """The known schedule for the next days: calendar events + pending blocks.

    Returns mirrored external calendar events (the external calendar is the
    source of truth; `is_agent_created` marks blocks the agent placed) and
    schedule proposals in proposed/accepted state overlapping the window.
    `range` is a forward window like '7d' (or 'today') anchored at midnight
    of today in the user's timezone; window bounds are returned as UTC
    instants.
    """
    days = _parse_range_days(range)
    tz = _local_timezone()
    start = dt.datetime.combine(_today_local(), dt.time.min, tzinfo=tz).astimezone(dt.UTC)
    end = start + dt.timedelta(days=days)

    with _store_session() as session:
        events = list(
            session.scalars(
                select(CalendarEventMirror)
                .where(CalendarEventMirror.start_at < end, CalendarEventMirror.end_at > start)
                .order_by(CalendarEventMirror.start_at)
            )
        )
        proposals = list(
            session.scalars(
                select(ScheduleProposal).where(
                    ScheduleProposal.proposed_start < end,
                    ScheduleProposal.proposed_end > start,
                    ScheduleProposal.status.in_(
                        [ProposalStatus.PROPOSED, ProposalStatus.ACCEPTED]
                    ),
                )
                .order_by(ScheduleProposal.proposed_start)
            )
        )
        task_titles = {
            task.id: task.title
            for task in session.scalars(
                select(Task).where(Task.id.in_([p.task_id for p in proposals]))
            )
        }
        event_payload = [
            {
                "id": str(event.id),
                "summary": event.summary,
                "start": _iso_utc(event.start_at),
                "end": _iso_utc(event.end_at),
                "calendar_source": _enum_value(event.calendar_source),
                "is_agent_created": event.is_agent_created,
                "agent_task_id": str(event.agent_task_id) if event.agent_task_id else None,
            }
            for event in events
        ]
        proposal_payload = [
            {
                "id": str(proposal.id),
                "task_id": str(proposal.task_id),
                "task_title": task_titles.get(proposal.task_id),
                "start": _iso_utc(proposal.proposed_start),
                "end": _iso_utc(proposal.proposed_end),
                "proposal_status": _enum_value(proposal.status),
            }
            for proposal in proposals
        ]
    return {
        "status": "ok",
        "window": {"start": _iso_utc(start), "end": _iso_utc(end), "days": days},
        "events": event_payload,
        "proposals": proposal_payload,
    }


class ScheduleBlockIn(BaseModel):
    """One proposed time block for a task (input of propose_schedule_blocks)."""

    task_id: str = Field(description="UUID of an existing task")
    start: str = Field(description="Block start, ISO-8601 (naive = UTC)")
    end: str = Field(description="Block end, ISO-8601, after start")


@mcp.tool
def propose_schedule_blocks(
    blocks: list[ScheduleBlockIn],
    decision_record_id: str | None = None,
) -> dict[str, Any]:
    """Propose schedule blocks for tasks (propose-then-confirm gate).

    Creates schedule proposals in `proposed` state — nothing is written to any
    calendar until the user confirms. Each returned block lists overlapping
    mirrored calendar events as `conflicts` so clashes are visible before
    asking. Optionally link the decision_record_id of the reasoning that
    produced the plan.
    """
    if not blocks:
        raise ToolError("blocks must not be empty")
    decision_uuid = (
        _parse_uuid(decision_record_id, "decision_record_id")
        if decision_record_id is not None
        else None
    )
    parsed: list[tuple[uuid.UUID, dt.datetime, dt.datetime]] = []
    for index, block in enumerate(blocks):
        start = _parse_datetime_utc(block.start, f"blocks[{index}].start")
        end = _parse_datetime_utc(block.end, f"blocks[{index}].end")
        if end <= start:
            raise ToolError(f"blocks[{index}]: end must be after start")
        parsed.append((_parse_uuid(block.task_id, f"blocks[{index}].task_id"), start, end))

    with _store_session() as session:
        if decision_uuid is not None and session.get(DecisionRecord, decision_uuid) is None:
            raise ToolError(f"decision_record {decision_record_id} not found")
        created: list[dict[str, Any]] = []
        for task_uuid, start, end in parsed:
            task = session.get(Task, task_uuid)
            if task is None:
                raise ToolError(f"task {task_uuid} not found")
            conflicts = [
                {
                    "summary": event.summary,
                    "start": _iso_utc(event.start_at),
                    "end": _iso_utc(event.end_at),
                    "is_agent_created": event.is_agent_created,
                }
                for event in session.scalars(
                    select(CalendarEventMirror)
                    .where(
                        CalendarEventMirror.start_at < end,
                        CalendarEventMirror.end_at > start,
                    )
                    .order_by(CalendarEventMirror.start_at)
                )
            ]
            proposal = ScheduleProposal(
                task_id=task.id,
                proposed_start=start,
                proposed_end=end,
                status=ProposalStatus.PROPOSED,
                decision_record_id=decision_uuid,
            )
            session.add(proposal)
            session.flush()
            created.append(
                {
                    "id": str(proposal.id),
                    "task_id": str(task.id),
                    "task_title": task.title,
                    "start": _iso_utc(start),
                    "end": _iso_utc(end),
                    "proposal_status": _enum_value(proposal.status),
                    "conflicts": conflicts,
                }
            )
    return {"status": "ok", "proposals": created}


@mcp.tool
def log_food(
    description: str,
    logged_at: str | None = None,
    meal_type: str | None = None,
    media_path: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Log a meal/snack with its (vision/transcription-derived) description.

    `meal_type` is breakfast / lunch / dinner / snack when known; `logged_at`
    is ISO-8601 (default: now, UTC); `media_path` is the stored media file path
    relative to the HealthMes data dir (never raw bytes); `source` names the
    capture channel (e.g. 'telegram').
    """
    if not description.strip():
        raise ToolError("description must not be empty")
    if meal_type is not None and meal_type not in MEAL_TYPES:
        raise ToolError(f"meal_type must be one of {sorted(MEAL_TYPES)}, got {meal_type!r}")
    logged_dt = (
        _parse_datetime_utc(logged_at, "logged_at")
        if logged_at is not None
        else dt.datetime.now(dt.UTC)
    )
    with _store_session() as session:
        row = FoodLog(
            logged_at=logged_dt,
            description=description,
            media_path=media_path,
            meal_type=meal_type,
            source=source,
        )
        session.add(row)
        session.flush()
        food_log_id = str(row.id)
        logged_iso = _iso_utc(row.logged_at)
    return {
        "status": "ok",
        "food_log_id": food_log_id,
        "logged_at": logged_iso,
        "meal_type": meal_type,
    }


# ---------------------------------------------------------------------------
# Medical-lite capture tools (docs/PLAN.md §8, Phase 3)
#
# Privacy contract: medical records, media files, and voice transcripts stay
# on this machine. The only medical content that ever re-enters the model
# context after capture is the structured description text — which is why
# ``list_medical_records`` returns descriptions but never transcripts, and
# media only as local paths (never bytes).
# ---------------------------------------------------------------------------


async def _capture_health_context() -> dict[str, Any]:
    """Current-day health snapshot for capture time (never fails the capture).

    Reuses :func:`get_daily_readiness_context` (import-level reuse, per
    docs/PLAN.md §8 — the snapshot is the same deterministic block set the
    planner sees). Infrastructure failures (open-wearables down, unknown user)
    degrade to an honest ``{"status": "unavailable"}`` marker instead of
    losing the capture.
    """
    try:
        return await get_daily_readiness_context()
    except ToolError as exc:
        return {"status": "unavailable", "reason": str(exc)}


def _health_context_status(context: Any) -> str | None:
    """Status of the stored health snapshot inside a medical record context."""
    if not isinstance(context, dict):
        return None
    health = context.get(MEDICAL_HEALTH_CONTEXT_KEY)
    if isinstance(health, dict):
        status = health.get("status")
        return str(status) if status is not None else None
    return None


@mcp.tool
async def create_medical_record(
    kind: str,
    description: str,
    media_path: str | None = None,
    transcript: str | None = None,
    context: dict[str, Any] | None = None,
    record_id: str | None = None,
) -> dict[str, Any]:
    """Persist a medical-lite capture (medication or symptom) locally.

    `kind` is medication / symptom. `description` is the structured text
    derived from the photo/voice note (what is legible/stated — never guessed
    drug names, never diagnosis). `media_path` is the LOCAL path of the stored
    photo/voice file (never bytes); `transcript` is the voice-note transcript
    when the capture was spoken. `context` is optional capture metadata (e.g.
    {"source": "telegram-photo", "user_stated_time": ...}); the tool itself
    attaches a deterministic health-context snapshot for today (sleep debt,
    HRV vs baseline, stress, charge) under the record's context — do not pass
    health data yourself. One-tap correction: pass `record_id` with the
    corrected `kind`/`description` to update the record just created; the
    original media, transcript, and capture-time health snapshot are
    preserved unless explicitly re-supplied. Everything stored here stays on
    this machine (returned fields carry ids and statuses only).
    """
    if kind not in MEDICAL_RECORD_KINDS:
        raise ToolError(
            f"kind must be one of {sorted(MEDICAL_RECORD_KINDS)}, got {kind!r}"
        )
    if not description.strip():
        raise ToolError("description must not be empty")

    if record_id is not None:
        record_uuid = _parse_uuid(record_id, "record_id")
        with _store_session() as session:
            row = session.get(MedicalRecord, record_uuid)
            if row is None:
                raise ToolError(f"medical_record {record_id} not found")
            row.kind = MedicalRecordKind(kind)
            row.description = description
            if media_path is not None:
                row.media_path = media_path
            if transcript is not None:
                row.transcript = transcript
            if context is not None:
                stored = dict(row.context or {})
                stored[MEDICAL_CAPTURE_CONTEXT_KEY] = context
                row.context = stored
            session.flush()
            payload = {
                "status": "ok",
                "created": False,
                "medical_record_id": str(row.id),
                "kind": _enum_value(row.kind),
                "recorded_at": _iso_utc(row.created_at),
                "health_context_status": _health_context_status(row.context),
            }
        return payload

    snapshot = await _capture_health_context()
    stored_context: dict[str, Any] = {MEDICAL_HEALTH_CONTEXT_KEY: snapshot}
    if context is not None:
        stored_context[MEDICAL_CAPTURE_CONTEXT_KEY] = context
    with _store_session() as session:
        row = MedicalRecord(
            kind=MedicalRecordKind(kind),
            description=description,
            media_path=media_path,
            transcript=transcript,
            context=stored_context,
        )
        session.add(row)
        session.flush()
        payload = {
            "status": "ok",
            "created": True,
            "medical_record_id": str(row.id),
            "kind": _enum_value(row.kind),
            "recorded_at": _iso_utc(row.created_at),
            "health_context_status": _health_context_status(row.context),
        }
    return payload


@mcp.tool
def list_medical_records(
    kind: str | None = None,
    range: str = "90d",
    include_context: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    """List local medical records for briefings (oldest first — timeline order).

    Feeds the `doctor-visit-summary` skill: `kind` filters to medication or
    symptom (omit for both); `range` is a trailing window of local days like
    '90d' (max 365) ending today. Privacy: transcripts are NEVER returned
    (only `has_transcript`; after capture only description text may re-enter
    the model), and media appears as local paths only — relative paths
    resolve against the returned `data_dir` (the service-local data
    directory, also where briefing exports belong, e.g.
    `{data_dir}/exports/`). `include_context` adds each record's stored
    capture-time context (deterministic health snapshot + capture metadata).
    When more than `limit` records match, the OLDEST are dropped (the most
    recent `limit` records are kept, still oldest-first).
    """
    if kind is not None and kind not in MEDICAL_RECORD_KINDS:
        raise ToolError(
            f"kind must be one of {sorted(MEDICAL_RECORD_KINDS)}, got {kind!r}"
        )
    if not 1 <= limit <= MAX_MEDICAL_LIST_LIMIT:
        raise ToolError(f"limit must be between 1 and {MAX_MEDICAL_LIST_LIMIT}, got {limit}")
    days = _parse_range_days(range, max_days=MAX_MEDICAL_RANGE_DAYS)
    tz = _local_timezone()
    end_day = dt.datetime.now(tz).date()
    start_day = end_day - dt.timedelta(days=days - 1)
    start_utc, _ = _local_day_bounds_utc(start_day, tz)
    _, end_utc = _local_day_bounds_utc(end_day, tz)

    with _store_session() as session:
        stmt = select(MedicalRecord)
        if kind is not None:
            stmt = stmt.where(MedicalRecord.kind == MedicalRecordKind(kind))
        rows = [
            row
            for row in session.scalars(stmt)
            # Window-filter in Python: stored datetimes are naive on sqlite
            # but aware on postgres, so SQL comparisons could not be portable.
            if start_utc <= _ensure_utc_dt(row.created_at) < end_utc
        ]
        rows.sort(key=lambda row: (_iso_utc(row.created_at) or "", str(row.id)))
        truncated = len(rows) > limit
        rows = rows[-limit:]
        records = []
        for row in rows:
            record: dict[str, Any] = {
                "id": str(row.id),
                "kind": _enum_value(row.kind),
                "recorded_at": _iso_utc(row.created_at),
                "description": row.description,
                "media_path": row.media_path,
                "has_transcript": row.transcript is not None,
                "health_context_status": _health_context_status(row.context),
            }
            if include_context:
                record["context"] = row.context
            records.append(record)

    data_dir = _active_settings().data_dir
    return {
        "status": "ok",
        "window": {
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
            "days": days,
            "timezone": str(tz),
        },
        "kind": kind or "all",
        "count": len(records),
        "truncated": truncated,
        "records": records,
        "data_dir": str(data_dir.expanduser().resolve()),
    }


def _validate_tree(node: Any, depth: int = 0, count: int = 0) -> int:
    """Validate a decision-tree node recursively; returns the node count."""
    if depth > MAX_TREE_DEPTH:
        raise ToolError(f"decision tree exceeds max depth {MAX_TREE_DEPTH}")
    if not isinstance(node, dict):
        raise ToolError("every decision tree node must be an object")
    node_type = node.get("type")
    if node_type not in DECISION_TREE_NODE_TYPES:
        raise ToolError(
            f"node type must be one of {sorted(DECISION_TREE_NODE_TYPES)}, got {node_type!r}"
        )
    label = node.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ToolError("every decision tree node needs a non-empty 'label'")
    count += 1
    if count > MAX_TREE_NODES:
        raise ToolError(f"decision tree exceeds max node count {MAX_TREE_NODES}")
    children = node.get("children", [])
    if children is None:
        children = []
    if not isinstance(children, list):
        raise ToolError("'children' must be a list of nodes")
    for child in children:
        count = _validate_tree(child, depth + 1, count)
    return count


@mcp.tool
def record_decision(
    kind: str,
    summary: str,
    tree: dict[str, Any],
    llm_model: str | None = None,
    tokens: int | None = None,
) -> dict[str, Any]:
    """Persist an explainable decision record and get its viewer link.

    `kind` is schedule_change / alert / insight / capture. `tree` is the
    recursive node structure {id?, type: input|rule|llm_step|option|action,
    label, detail?, children[]} — deterministic layers pre-fill input/rule
    nodes; append your own llm_step/option/action nodes honestly (never
    rewrite pre-filled ones). Returns the decision viewer URL to attach to any
    alert or message about this decision.
    """
    if kind not in {k.value for k in DecisionKind}:
        raise ToolError(
            f"kind must be one of {sorted(k.value for k in DecisionKind)}, got {kind!r}"
        )
    if not summary.strip():
        raise ToolError("summary must not be empty")
    _validate_tree(tree)
    with _store_session() as session:
        row = DecisionRecord(
            kind=DecisionKind(kind),
            tree=tree,
            summary=summary,
            llm_model=llm_model,
            tokens=tokens,
        )
        session.add(row)
        session.flush()
        decision_id = str(row.id)
    # Viewer pages are opened from the phone browser (no headers); the shared
    # construction point embeds the derived read-only credential — never the
    # API token itself. (Function-local import keeps healthmes.api off this
    # module's import path.)
    from healthmes.api.auth import viewer_url

    return {
        "status": "ok",
        "decision_id": decision_id,
        "viewer_url": viewer_url(_active_settings(), f"/decisions/{decision_id}"),
    }


# ---------------------------------------------------------------------------
# ASGI app for mounting at /mcp (Streamable HTTP)
# ---------------------------------------------------------------------------


def build_mcp_http_app(path: str = "/mcp", stateless_http: bool = False):
    """Streamable-HTTP ASGI app serving this MCP server at ``path``.

    Returns a Starlette app whose ``.lifespan`` MUST be run by the hosting app
    (it starts the MCP session manager). Composition root wiring:

        mcp_app = build_mcp_http_app()                 # endpoint: POST /mcp
        app = FastAPI(lifespan=mcp_app.lifespan)       # or chain lifespans
        app.mount("", mcp_app)                         # after app routes

    Mounting at ``""`` (bare mount) keeps the endpoint at exactly ``/mcp`` —
    the URL Hermes registers (mcp_tool.py ``url:`` transport) — while FastAPI's
    own routes, added earlier, keep precedence.
    """
    return mcp.http_app(path=path, stateless_http=stateless_http, transport="http")
