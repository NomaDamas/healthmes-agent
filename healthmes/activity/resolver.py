"""Bounded cross-domain context selection owned by HealthMes."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.activity.aggregation import local_day_bounds, timezone_name
from healthmes.activity.context import (
    activity_summary_context,
    focus_context,
    overwork_context,
    recovery_activity_context,
)
from healthmes.activity.contracts import ActivityContextResolveRequest
from healthmes.nutrition.intake_query import decision_context as nutrition_decision_context
from healthmes.nutrition.query import known_caffeine_for_day
from healthmes.store import CalendarEventMirror

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


def _timezone_name(value: str | tzinfo) -> str:
    return timezone_name(value)


def _parse_day(value: str | None, timezone: str | tzinfo) -> date:
    if value is not None:
        return date.fromisoformat(value)
    return datetime.now(_zone(timezone)).date()


def _zone(value: str | tzinfo) -> tzinfo:
    return ZoneInfo(value) if isinstance(value, str) else value


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
            }
        evidence_ids = [
            str(value) for value in context.get("evidence_event_ids", []) if value is not None
        ]
        return {
            "status": str(context.get("status", "ok")),
            "kind": "intake_decision_context",
            "context": context,
            "evidence_ids": evidence_ids,
            "limitations": list(context.get("limitations") or []),
            "freshness": {
                "recorded_at": context.get("request", {}).get("requested_at"),
                "status": "stored_decision_context",
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
        "status": str(ledger.get("status", "incomplete")),
        "kind": "confirmed_caffeine_ledger",
        "context": ledger,
        "evidence_ids": evidence_ids,
        "limitations": (
            []
            if ledger.get("total_intake_complete") is True
            else ["caffeine_day_not_confirmed_complete"]
        ),
        "freshness": {
            "recorded_at": None,
            "status": "confirmed_ledger_snapshot",
        },
    }


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
    day = _parse_day(request.date, zone)
    current = (now or datetime.now(UTC)).astimezone(UTC)
    selected = DOMAIN_SELECTION[request.question_kind]
    contexts: dict[str, Any] = {}

    if "activity" in selected:
        if request.question_kind == "activity_summary":
            contexts["activity"] = activity_summary_context(
                session,
                day=day,
                timezone=timezone_value,
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
                )
        elif request.question_kind == "recovery":
            contexts["activity"] = recovery_activity_context(
                session,
                day=day,
                timezone=timezone_value,
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
                ),
            )
        else:
            contexts["activity"] = overwork_context(
                session,
                day=day,
                timezone=timezone_value,
                lookback_days=request.lookback_days,
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
                contexts["wearable"] = await wearable_reader(day)
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
        ],
    }
