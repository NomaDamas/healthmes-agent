from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from typing import Any

from healthmes.calendars.adjustments import evaluate_event_eligibility
from healthmes.calendars.base import coerce_utc
from healthmes.engine.rules import RuleThresholds
from healthmes.store import CalendarEventMirror
from healthmes.store import enums as store_enums


def public_calendar_adjustment_status(status: Any) -> str:
    value = getattr(status, "value", status)
    aliases = {
        store_enums.CalendarMutationStatus.APPLIED_RECOVERED.value: (
            store_enums.CalendarMutationStatus.APPLIED.value
        ),
        store_enums.CalendarMutationStatus.FAILED_NO_CHANGE.value: (
            store_enums.CalendarMutationStatus.FAILED.value
        ),
    }
    return aliases.get(value, str(value))


def calendar_adjustment_display(
    proposal: Any,
    tz: dt.tzinfo,
    reply_handle: str,
    decision_tree: Mapping[str, Any] | None,
    event_label: str | None,
    viewer_url: str | None,
) -> dict[str, Any]:
    snapshot = proposal.snapshot
    evidence = {}
    for child in (decision_tree or {}).get("children", ()):
        if isinstance(child, Mapping) and child.get("id") == "evidence":
            evidence = dict(child.get("detail") or {})
            break
    return {
        "proposal_id": str(proposal.id),
        "operation": getattr(snapshot.operation, "value", snapshot.operation),
        "event_label": event_label if event_label is not None else snapshot.event_label,
        "delta_minutes": 30,
        "before": {
            "start": coerce_utc(snapshot.original_start_at).astimezone(tz).isoformat(),
            "end": coerce_utc(snapshot.original_end_at).astimezone(tz).isoformat(),
        },
        "after": {
            "start": coerce_utc(snapshot.proposed_start_at).astimezone(tz).isoformat(),
            "end": coerce_utc(snapshot.proposed_end_at).astimezone(tz).isoformat(),
        },
        "evidence": evidence,
        "freshness": "validated_for_local_date",
        "limitations": [
            "technical_eligibility_only",
            "explicit_confirmation_required",
        ],
        "reply_options": [f"적용 {reply_handle}", f"그대로 {reply_handle}"],
        "viewer_url": viewer_url,
    }


def mirror_to_adjustment_candidate(event: CalendarEventMirror) -> dict[str, Any]:
    return {
        "id": event.id,
        "external_id": event.external_id,
        "calendar_source": event.calendar_source,
        "connection_generation": event.connection_generation,
        "summary": event.summary,
        "start_at": coerce_utc(event.start_at),
        "end_at": coerce_utc(event.end_at),
        "is_agent_created": event.is_agent_created,
        "etag": event.etag,
        "organizer_self": event.organizer_self,
        "has_attendees": event.has_attendees,
        "is_recurring": event.is_recurring,
        "event_type": event.event_type,
        "is_all_day": event.is_all_day,
        "is_locked": event.is_locked,
        "status": event.status,
    }


def adjustment_projection(
    event: CalendarEventMirror, *, now: dt.datetime, tz: dt.tzinfo
) -> dict[str, Any]:
    local_day = coerce_utc(event.start_at).astimezone(tz).date()
    result = evaluate_event_eligibility(
        event,
        now=now,
        local_date=local_day,
        timezone=tz,
    )
    return {
        "eligible": result.eligible,
        "operations": ["shorten"] if result.eligible else [],
        "reasons": list(result.reasons),
    }


def afternoon_busy_minutes(
    events: Iterable[CalendarEventMirror], day: dt.date, tz: dt.tzinfo
) -> int:
    thresholds = RuleThresholds()
    start = dt.datetime.combine(
        day,
        dt.time(hour=thresholds.afternoon_start_hour),
        tzinfo=tz,
    ).astimezone(dt.UTC)
    end = dt.datetime.combine(
        day,
        dt.time(hour=thresholds.afternoon_end_hour),
        tzinfo=tz,
    ).astimezone(dt.UTC)
    total = 0
    for event in events:
        event_start = max(coerce_utc(event.start_at), start)
        event_end = min(coerce_utc(event.end_at), end)
        if event_end > event_start:
            total += int((event_end - event_start).total_seconds() // 60)
    return total
