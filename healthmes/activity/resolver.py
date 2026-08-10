"""Bounded cross-domain context selection owned by HealthMes."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta, tzinfo
from math import isfinite
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from healthmes.activity.aggregation import local_day_bounds, timezone_name
from healthmes.activity.context import (
    activity_summary_context,
    focus_context,
    overwork_context,
    recovery_activity_context,
)
from healthmes.activity.contracts import ActivityContextResolveRequest
from healthmes.calendars.base import HealthmesEventKind
from healthmes.nutrition.intake_query import decision_context as nutrition_decision_context
from healthmes.nutrition.query import known_caffeine_for_day
from healthmes.store import CalendarEventMirror
from healthmes.timezones import parse_timezone

WearableReader = Callable[[date], Awaitable[dict[str, Any]]]

ALL_DOMAINS = ("activity", "wearable", "calendar", "nutrition", "time")
DOMAIN_SELECTION: dict[str, tuple[str, ...]] = {
    "activity_summary": ("activity",),
    "focus": ("activity", "wearable", "calendar"),
    "overwork": ("activity", "wearable", "calendar"),
    "recovery": ("activity", "wearable"),
    "caffeine_for_focus": (
        "activity",
        "wearable",
        "calendar",
        "nutrition",
        "time",
    ),
}
MAX_FUTURE_SKEW = timedelta(minutes=1)


class WellnessContextRangeError(ValueError):
    pass


def _timezone_name(value: str | tzinfo) -> str:
    return timezone_name(value)


def _parse_day(
    value: str | None,
    timezone: str | tzinfo,
    *,
    start: datetime | None = None,
    now: datetime | None = None,
) -> date:
    if value is not None:
        return date.fromisoformat(value)
    if start is not None:
        return start.astimezone(_zone(timezone)).date()
    return (now or datetime.now(UTC)).astimezone(_zone(timezone)).date()


def _zone(value: str | tzinfo) -> tzinfo:
    return parse_timezone(value)


def calendar_context(
    session: Session,
    *,
    day: date,
    timezone: str | tzinfo,
) -> dict[str, Any]:
    name = _timezone_name(timezone)
    start, end = local_day_bounds(day, timezone)
    rows = list(
        session.scalars(
            select(CalendarEventMirror)
            .where(
                CalendarEventMirror.start_at < end,
                CalendarEventMirror.end_at > start,
                CalendarEventMirror.is_all_day.is_(False),
                or_(
                    CalendarEventMirror.healthmes_kind.is_(None),
                    CalendarEventMirror.healthmes_kind
                    != HealthmesEventKind.ACTUAL_SLEEP.value,
                ),
            )
            .order_by(CalendarEventMirror.start_at)
        )
    )
    spans: list[tuple[datetime, datetime]] = []
    for row in rows:
        row_start = (
            row.start_at.replace(tzinfo=UTC)
            if row.start_at.tzinfo is None
            else row.start_at.astimezone(UTC)
        )
        row_end = (
            row.end_at.replace(tzinfo=UTC)
            if row.end_at.tzinfo is None
            else row.end_at.astimezone(UTC)
        )
        clipped_start = max(row_start, start)
        clipped_end = min(row_end, end)
        if clipped_end > clipped_start:
            spans.append((clipped_start, clipped_end))
    busy_seconds = 0.0
    merged: list[tuple[datetime, datetime]] = []
    for span_start, span_end in sorted(spans):
        if not merged or span_start > merged[-1][1]:
            merged.append((span_start, span_end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], span_end))
    busy_seconds = sum((span_end - span_start).total_seconds() for span_start, span_end in merged)
    freshness = (
        max(
            (
                row.updated_at.replace(tzinfo=UTC)
                if row.updated_at.tzinfo is None
                else row.updated_at.astimezone(UTC)
            )
            for row in rows
        )
        if rows
        else None
    )
    return {
        "status": "ok" if rows else "insufficient_data",
        "date": day.isoformat(),
        "timezone": name,
        "event_count": len(rows),
        "busy_minutes": round(busy_seconds / 60, 2),
        "first_event_at": (
            first.isoformat()
            if (first := min((value[0] for value in spans), default=None)) is not None
            else None
        ),
        "last_event_at": (
            last.isoformat()
            if (last := max((value[1] for value in spans), default=None)) is not None
            else None
        ),
        "evidence_ids": [str(row.id) for row in rows],
        "freshness": {
            "recorded_at": freshness.isoformat() if freshness is not None else None,
            "status": "calendar_mirror" if rows else "unavailable",
        },
        "coverage": {
            "status": "calendar_mirror_rows" if rows else "no_data",
            "ratio": None,
        },
        "limitations": [
            "calendar_titles_omitted",
            "calendar_presence_is_not_work_intensity",
        ],
    }


def nutrition_context(
    session: Session,
    *,
    day: date,
    timezone: str,
    request_id: uuid.UUID | None,
) -> dict[str, Any]:
    if request_id is not None:
        context = nutrition_decision_context(session, request_id=request_id)
        if context is None:
            return {
                "status": "insufficient_data",
                "reason": "nutrition_decision_request_not_found",
                "evidence_ids": [],
                "limitations": ["candidate_food_context_missing"],
                "freshness": {"recorded_at": None, "status": "unavailable"},
                "coverage": {"status": "no_data", "ratio": None},
                "candidate_ledger_complete": False,
                "decision_ready": False,
            }
        request_context = context.get("request", {})
        specialized = context.get("specialized_evidence", {})
        caffeine = specialized.get("caffeine")
        boundaries = context.get("boundaries", {})
        failures: list[str] = []
        if request_context.get("scope") != "caffeine_sleep":
            failures.append("nutrition_request_scope_is_not_caffeine_sleep")
        anchor_raw = (
            request_context.get("intended_consumption_at")
            or request_context.get("requested_at")
        )
        try:
            anchor = datetime.fromisoformat(str(anchor_raw))
            if anchor.tzinfo is None:
                raise ValueError
        except (TypeError, ValueError):
            failures.append("nutrition_request_time_missing")
        else:
            if anchor.astimezone(parse_timezone(timezone)).date() != day:
                failures.append("nutrition_request_date_mismatch")
        if not isinstance(caffeine, dict):
            failures.append("caffeine_specialized_evidence_missing")
        elif caffeine.get("total_intake_complete") is not True:
            failures.append("caffeine_day_not_confirmed_complete")
        elif caffeine.get("status") != "known":
            failures.append("caffeine_ledger_status_not_known")
        elif caffeine.get("local_date") != day.isoformat():
            failures.append("caffeine_ledger_date_mismatch")
        elif caffeine.get("timezone") != timezone:
            failures.append("caffeine_ledger_timezone_mismatch")
        elif (
            _finite_nonnegative_number(
                caffeine.get("confirmed_caffeine_mg")
            )
            is None
        ):
            failures.append("caffeine_ledger_amount_missing")
        if boundaries.get("caffeine_total_intake_complete") is not True:
            failures.append("caffeine_boundary_not_complete")
        candidate = context.get("candidate", {})
        has_candidate_caffeine = any(
            str(fact.get("nutrient") or "").casefold() in {"caffeine", "카페인"}
            and _known_caffeine_amount(fact.get("amount"))
            for item in candidate.get("resolved_items", [])
            if isinstance(item, dict)
            for fact in item.get("nutrients", [])
            if isinstance(fact, dict)
        )
        if not has_candidate_caffeine:
            failures.append("candidate_caffeine_estimate_missing")
        evidence_ids = [
            str(value) for value in context.get("evidence_event_ids", []) if value is not None
        ]
        return {
            "status": "ok" if not failures else "insufficient_data",
            "kind": "intake_decision_context",
            "context": context,
            "evidence_ids": evidence_ids,
            "candidate_ledger_complete": not failures,
            "specialist_policy_executed": False,
            "decision_ready": False,
            "reason": failures[0] if failures else None,
            "limitations": sorted(
                {
                    *list(context.get("limitations") or []),
                    *failures,
                }
            ),
            "freshness": {
                "recorded_at": context.get("request", {}).get("requested_at"),
                "status": "stored_decision_context",
            },
            "coverage": {
                "status": "complete_day_confirmation"
                if not failures
                else "incomplete",
                "ratio": 1.0 if not failures else None,
            },
        }
    ledger = known_caffeine_for_day(
        session,
        local_date=day,
        timezone=timezone,
    )
    evidence_ids = [
        str(item.get("event_id") or item.get("confirmation_id"))
        for item in ledger.get("evidence", [])
        if item.get("event_id") or item.get("confirmation_id")
    ]
    return {
        "status": "insufficient_data",
        "kind": "confirmed_caffeine_ledger",
        "context": ledger,
        "evidence_ids": evidence_ids,
        "candidate_ledger_complete": False,
        "specialist_policy_executed": False,
        "decision_ready": False,
        "reason": "candidate_caffeine_context_missing",
        "limitations": (
            ["candidate_caffeine_context_missing"]
            if ledger.get("total_intake_complete") is True
            else [
                "candidate_caffeine_context_missing",
                "caffeine_day_not_confirmed_complete",
            ]
        ),
        "freshness": {
            "recorded_at": None,
            "status": "confirmed_ledger_snapshot",
        },
        "coverage": {
            "status": (
                "complete_day_confirmation"
                if ledger.get("total_intake_complete") is True
                else "incomplete"
            ),
            "ratio": (
                1.0 if ledger.get("total_intake_complete") is True else None
            ),
        },
    }


def _known_caffeine_amount(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if str(value.get("unit") or "").casefold() != "mg":
        return False

    kind = str(value.get("kind") or "").casefold()
    if kind == "exact":
        return _finite_nonnegative_number(value.get("exact")) is not None
    if kind == "range":
        minimum = _finite_nonnegative_number(value.get("minimum"))
        maximum = _finite_nonnegative_number(value.get("maximum"))
        return (
            minimum is not None
            and maximum is not None
            and minimum <= maximum
        )
    return False


def _finite_nonnegative_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    return result if isfinite(result) and result >= 0 else None


_WEARABLE_TOP_LEVEL_SCALARS = frozenset(
    {
        "status",
        "reason",
        "date",
        "baseline_window_days",
        "confidence",
    }
)
_WEARABLE_BLOCK_SCALARS: dict[str, frozenset[str]] = {
    "sleep_debt": frozenset(
        {
            "status",
            "reason",
            "confidence",
            "date",
            "source",
            "recorded_at",
            "observed_at",
            "window_days",
            "nights_counted",
            "coverage",
            "index",
        }
    ),
    "actual_sleep": frozenset(
        {
            "status",
            "reason",
            "confidence",
            "date",
            "local_date",
            "start",
            "wake_time",
            "duration_minutes",
            "time_in_bed_minutes",
            "source",
            "freshness",
            "earliest_available_work_time",
            "recorded_at",
            "observed_at",
        }
    ),
    "hrv": frozenset(
        {
            "status",
            "reason",
            "confidence",
            "date",
            "score",
            "source",
            "recorded_at",
            "observed_at",
            "window_days",
            "n_days",
            "coverage",
            "stale_days",
            "baseline_median",
            "delta",
            "delta_pct",
            "z_score",
            "variant",
            "unit",
        }
    ),
    "stress": frozenset(
        {
            "status",
            "reason",
            "confidence",
            "date",
            "source",
            "recorded_at",
            "observed_at",
            "value",
            "scale",
            "observed_on",
            "stale_days",
        }
    ),
    "charge": frozenset(
        {
            "status",
            "reason",
            "confidence",
            "date",
            "source",
            "recorded_at",
            "observed_at",
            "value",
            "freshest_at",
        }
    ),
    "yesterday_load": frozenset(
        {
            "status",
            "reason",
            "confidence",
            "date",
            "source",
            "recorded_at",
            "observed_at",
            "workouts",
            "total_minutes",
            "total_calories_kcal",
            "max_avg_heart_rate_bpm",
        }
    ),
}
_WEARABLE_LAST_NIGHT_SCALARS = frozenset(
    {"date", "score", "recorded_at", "observed_at"}
)
_WEARABLE_CURRENT_SCALARS = frozenset(
    {"date", "value", "unit", "qualifier", "recorded_at", "observed_at"}
)
_WEARABLE_CHARGE_ENTRY_SCALARS = frozenset(
    {
        "category",
        "provider",
        "value",
        "qualifier",
        "observed_on",
        "recorded_at",
    }
)
_WEARABLE_FRESHNESS_SCALARS = frozenset({"recorded_at", "status"})
_WEARABLE_COVERAGE_SCALARS = frozenset(
    {"status", "ratio", "usable_blocks", "total_blocks"}
)


def _safe_context_scalar(value: object) -> bool:
    if value is None or isinstance(value, str | bool | int):
        return True
    return isinstance(value, float) and isfinite(value)


def _allowlisted_scalars(
    value: dict[str, Any],
    allowed: frozenset[str],
) -> dict[str, Any]:
    return {
        key: value[key]
        for key in allowed
        if key in value and _safe_context_scalar(value[key])
    }


def _normalize_wearable_block(
    name: str,
    value: dict[str, Any],
) -> dict[str, Any]:
    block = _allowlisted_scalars(value, _WEARABLE_BLOCK_SCALARS[name])
    last_night = value.get("last_night")
    if isinstance(last_night, dict):
        block["last_night"] = _allowlisted_scalars(
            last_night,
            _WEARABLE_LAST_NIGHT_SCALARS,
        )
    current = value.get("current")
    if isinstance(current, dict):
        block["current"] = _allowlisted_scalars(
            current,
            _WEARABLE_CURRENT_SCALARS,
        )
    if name == "charge" and isinstance(value.get("entries"), list):
        block["entries"] = [
            _allowlisted_scalars(entry, _WEARABLE_CHARGE_ENTRY_SCALARS)
            for entry in value["entries"]
            if isinstance(entry, dict)
        ]
    if name == "yesterday_load" and isinstance(value.get("types"), list):
        block["types"] = [
            item for item in value["types"] if isinstance(item, str)
        ]
    return block


def _normalize_wearable_context(
    value: dict[str, Any],
    *,
    day: date,
) -> dict[str, Any]:
    raw_date = value.get("date")
    if raw_date not in {None, day.isoformat()}:
        return {
            "status": "insufficient_data",
            "reason": "wearable_context_date_mismatch",
            "date": raw_date if isinstance(raw_date, str) else None,
            "evidence_ids": [],
            "freshness": {"recorded_at": None, "status": "unavailable"},
            "coverage": {"status": "no_matching_day", "ratio": None},
            "limitations": [
                "wearable_context_date_mismatch",
                "open_wearables_context_not_combined",
            ],
        }
    context = _allowlisted_scalars(value, _WEARABLE_TOP_LEVEL_SCALARS)
    limitations = [
        item
        for item in value.get("limitations", [])
        if isinstance(item, str)
    ] if isinstance(value.get("limitations"), list) else []
    evidence_ids = [
        item
        for item in value.get("evidence_ids", [])
        if isinstance(item, str)
    ] if isinstance(value.get("evidence_ids"), list) else []
    block_names = tuple(_WEARABLE_BLOCK_SCALARS)
    for name in block_names:
        raw_block = value.get(name)
        if isinstance(raw_block, dict):
            context[name] = _normalize_wearable_block(name, raw_block)
    blocks = [
        context[name]
        for name in block_names
        if isinstance(context.get(name), dict)
    ]
    usable_blocks = [
        block for block in blocks if block.get("status") == "ok"
    ]
    raw_coverage = value.get("coverage")
    if isinstance(raw_coverage, dict):
        coverage = _allowlisted_scalars(
            raw_coverage,
            _WEARABLE_COVERAGE_SCALARS,
        )
        if coverage:
            context["coverage"] = coverage
    if "coverage" not in context:
        context["coverage"] = {
            "status": "readiness_blocks",
            "ratio": (
                round(len(usable_blocks) / len(blocks), 4)
                if blocks
                else None
            ),
            "usable_blocks": len(usable_blocks),
            "total_blocks": len(blocks),
        }

    def collect_timestamps(node: Any, timestamps: list[str]) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if key in {
                    "recorded_at",
                    "freshest_at",
                    "observed_at",
                } and isinstance(item, str):
                    try:
                        parsed = datetime.fromisoformat(item)
                    except ValueError:
                        pass
                    else:
                        if parsed.tzinfo is not None:
                            timestamps.append(
                                parsed.astimezone(UTC).isoformat()
                            )
                else:
                    collect_timestamps(item, timestamps)
        elif isinstance(node, list):
            for item in node:
                collect_timestamps(item, timestamps)

    raw_freshness = value.get("freshness")
    if isinstance(raw_freshness, dict):
        freshness = _allowlisted_scalars(
            raw_freshness,
            _WEARABLE_FRESHNESS_SCALARS,
        )
        if freshness:
            context["freshness"] = freshness
    explicit_timestamps: list[str] = []
    collect_timestamps(context.get("freshness"), explicit_timestamps)
    if not explicit_timestamps:
        timestamps: list[str] = []
        collect_timestamps(blocks, timestamps)
        context["freshness"] = {
            "recorded_at": max(timestamps) if timestamps else None,
            "status": "derived_from_readiness_blocks"
            if timestamps
            else "unavailable",
        }
    if not evidence_ids:
        limitations.append("wearable_readiness_evidence_ids_unavailable")
    context["evidence_ids"] = evidence_ids
    context["limitations"] = sorted(set(limitations))
    return context


def _validate_context_window(
    request: ActivityContextResolveRequest,
    *,
    day: date,
    zone: tzinfo,
    now: datetime,
) -> None:
    if request.start is None or request.end is None:
        return
    if request.end > now + MAX_FUTURE_SKEW:
        raise WellnessContextRangeError("future activity is unknown")
    start_day = request.start.astimezone(zone).date()
    end_day = (
        request.end - timedelta(microseconds=1)
    ).astimezone(zone).date()
    if start_day != day or end_day != day:
        raise WellnessContextRangeError(
            "activity window and selected date must refer to the same local day"
        )


def _compound_activity_context(
    focus: dict[str, Any],
    overwork: dict[str, Any],
) -> dict[str, Any]:
    children = {"focus": focus, "overwork": overwork}
    evidence_ids = sorted(
        {str(value) for child in children.values() for value in child.get("evidence_ids", [])}
    )
    limitations = sorted(
        {str(value) for child in children.values() for value in child.get("limitations", [])}
    )
    return {
        "status": (
            "ok"
            if any(child.get("status") == "ok" for child in children.values())
            else "insufficient_data"
        ),
        **children,
        "evidence_ids": evidence_ids,
        "freshness": {key: value.get("freshness") for key, value in children.items()},
        "coverage": {key: value.get("coverage") for key, value in children.items()},
        "limitations": limitations,
    }


def _context_ready_for_preset(
    question_kind: str,
    selected: tuple[str, ...],
    contexts: dict[str, Any],
) -> bool:
    usable = {"ok", "known"}
    for domain in selected:
        context = contexts.get(domain)
        if not isinstance(context, dict) or context.get("status") not in usable:
            return False
        if domain != "time" and not context.get("evidence_ids"):
            return False
    if question_kind == "caffeine_for_focus":
        return (
            contexts.get("nutrition", {}).get("candidate_ledger_complete") is True
        )
    return True


async def resolve_wellness_context(
    session: Session,
    request: ActivityContextResolveRequest,
    *,
    default_timezone: str | tzinfo,
    wearable_reader: WearableReader | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    timezone_value: str | tzinfo = request.timezone or default_timezone
    timezone = _timezone_name(timezone_value)
    zone = _zone(timezone_value)
    current = (now or datetime.now(UTC)).astimezone(UTC)
    day = _parse_day(
        request.date,
        zone,
        start=request.start,
        now=current,
    )
    _validate_context_window(
        request,
        day=day,
        zone=zone,
        now=current,
    )
    selected = DOMAIN_SELECTION[request.question_kind]
    contexts: dict[str, Any] = {}

    if "activity" in selected:
        if request.question_kind == "activity_summary":
            contexts["activity"] = activity_summary_context(
                session,
                day=day,
                timezone=timezone_value,
                now=current,
            )
        elif request.question_kind == "focus":
            start, end = local_day_bounds(day, timezone_value)
            effective_start = request.start or start
            effective_end = request.end or min(end, current + timedelta(seconds=1))
            if effective_end <= effective_start:
                contexts["activity"] = {
                    "status": "insufficient_data",
                    "reason": "activity_window_has_not_started",
                    "evidence_ids": [],
                    "freshness": {"recorded_at": None, "status": "unavailable"},
                    "coverage": None,
                    "limitations": ["future_activity_is_unknown"],
                }
            else:
                contexts["activity"] = focus_context(
                    session,
                    start=effective_start,
                    end=effective_end,
                    timezone=timezone_value,
                    now=current,
                )
        elif request.question_kind == "recovery":
            contexts["activity"] = recovery_activity_context(
                session,
                day=day,
                timezone=timezone_value,
                now=current,
            )
        elif request.question_kind == "caffeine_for_focus":
            start, end = local_day_bounds(day, timezone_value)
            effective_start = request.start or start
            effective_end = request.end or min(end, current + timedelta(seconds=1))
            focus_value = (
                focus_context(
                    session,
                    start=effective_start,
                    end=effective_end,
                    timezone=timezone_value,
                    now=current,
                )
                if effective_end > effective_start
                else {
                    "status": "insufficient_data",
                    "reason": "activity_window_has_not_started",
                    "evidence_ids": [],
                    "freshness": {"recorded_at": None, "status": "unavailable"},
                    "coverage": None,
                    "limitations": ["future_activity_is_unknown"],
                }
            )
            contexts["activity"] = _compound_activity_context(
                focus_value,
                overwork_context(
                    session,
                    day=day,
                    timezone=timezone_value,
                    lookback_days=request.lookback_days,
                    now=current,
                ),
            )
        else:
            contexts["activity"] = overwork_context(
                session,
                day=day,
                timezone=timezone_value,
                lookback_days=request.lookback_days,
                now=current,
            )

    if "wearable" in selected:
        if wearable_reader is None:
            contexts["wearable"] = {
                "status": "unavailable",
                "reason": "wearable_reader_not_configured",
                "evidence_ids": [],
                "freshness": {"recorded_at": None, "status": "unavailable"},
                "coverage": None,
                "limitations": ["open_wearables_context_unavailable"],
            }
        else:
            try:
                contexts["wearable"] = _normalize_wearable_context(
                    await wearable_reader(day),
                    day=day,
                )
            except Exception as exc:  # runtime boundary: one source must not erase the rest
                contexts["wearable"] = {
                    "status": "unavailable",
                    "reason": type(exc).__name__,
                    "evidence_ids": [],
                    "freshness": {"recorded_at": None, "status": "unavailable"},
                    "coverage": None,
                    "limitations": ["open_wearables_context_unavailable"],
                }

    if "calendar" in selected:
        contexts["calendar"] = calendar_context(
            session,
            day=day,
            timezone=timezone_value,
        )

    if "nutrition" in selected:
        contexts["nutrition"] = nutrition_context(
            session,
            day=day,
            timezone=timezone,
            request_id=request.nutrition_request_id,
        )

    if "time" in selected:
        contexts["time"] = {
            "status": "ok",
            "now": current.isoformat(),
            "local_now": current.astimezone(zone).isoformat(),
            "timezone": timezone,
            "evidence_ids": [],
            "freshness": {
                "recorded_at": current.isoformat(),
                "status": "request_time",
            },
            "coverage": {"status": "exact", "ratio": 1.0},
            "limitations": [],
        }

    limitations = sorted(
        {
            str(limit)
            for context in contexts.values()
            for limit in (context.get("limitations", []) if isinstance(context, dict) else [])
        }
    )
    if request.question_kind == "caffeine_for_focus":
        limitations.extend(
            [
                "context_resolver_does_not_recalculate_caffeine_policy",
                "candidate_and_safety_context_are_required_for_a_caffeine_proposal",
            ]
        )
    domain_statuses = {
        domain: str(context.get("status", "insufficient_data"))
        for domain, context in contexts.items()
        if isinstance(context, dict)
    }
    usable = {"ok", "known"}
    context_ready = _context_ready_for_preset(
        request.question_kind,
        selected,
        contexts,
    )
    if not any(status in usable for status in domain_statuses.values()):
        overall_status = "insufficient_data"
    elif all(status in usable for status in domain_statuses.values()):
        overall_status = "ok"
    else:
        overall_status = "partial"
    return {
        "status": overall_status,
        "question_kind": request.question_kind,
        "date": day.isoformat(),
        "timezone": timezone,
        "selected_domains": list(selected),
        "not_selected_domains": [domain for domain in ALL_DOMAINS if domain not in selected],
        "contexts": contexts,
        "domain_statuses": domain_statuses,
        "context_ready": context_ready,
        # This compatibility resolver assembles context only. Final policy,
        # LLM synthesis, source-ref validation, and DecisionRecord persistence
        # belong to the HealthMes Decision Agent.
        "decision_ready": False,
        "evidence": [
            {"domain": domain, "id": str(evidence_id)}
            for domain, context in contexts.items()
            if isinstance(context, dict)
            for evidence_id in context.get("evidence_ids", [])
        ],
        "freshness": {
            domain: context.get("freshness")
            for domain, context in contexts.items()
            if isinstance(context, dict)
        },
        "coverage": {
            domain: (
                context.get("coverage") if "coverage" in context else context.get("source_coverage")
            )
            for domain, context in contexts.items()
            if isinstance(context, dict)
        },
        "conflicts": [],
        "limitations": sorted(set(limitations)),
        "boundaries": [
            "specialized_policy_numbers_are_not_recomputed",
            "missing_data_is_not_zero",
            "association_is_not_causation",
            "context_only_not_a_final_wellness_decision",
            "decision_ready_requires_healthmes_decision_agent",
        ],
    }
