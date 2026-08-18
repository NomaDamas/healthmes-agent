from __future__ import annotations

import re

_SLEEP = re.compile(r"(?:oura|오우라|수면|잠)", re.IGNORECASE)
_CALENDAR = re.compile(r"(?:calendar|캘린더)", re.IGNORECASE)
_UPDATE = re.compile(r"(?:sync|update|reflect|동기화|업데이트|반영|기록)", re.IGNORECASE)
_EXPLICIT_DATE = re.compile(
    r"(?:\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}[/-]\d{1,2}\b|\d{1,2}월\s*\d{1,2}일)"
)
_EXPLICIT_TIMES = re.compile(
    r"\b(?:[01]?\d|2[0-3]):[0-5]\d\s*(?:→|->|~|–|—|-)\s*"
    r"(?:[01]?\d|2[0-3]):[0-5]\d\b"
)
_SUMMARY_DATE = re.compile(r"(?:oura\s*(?:summary|요약)|summary\s*date|요약일)", re.IGNORECASE)


def _sleep_calendar_context(user_message: str) -> str | None:
    if not (
        _SLEEP.search(user_message)
        and _CALENDAR.search(user_message)
        and _UPDATE.search(user_message)
    ):
        return None
    if _EXPLICIT_TIMES.search(user_message) or _SUMMARY_DATE.search(user_message):
        date_basis = "oura_summary"
    elif _EXPLICIT_DATE.search(user_message):
        date_basis = "night_start"
    else:
        date_basis = "oura_summary"
    return (
        "[HealthMes deterministic route]\n"
        "This turn is an explicit request to prepare an Oura actual-sleep Calendar "
        "update. Ignore any earlier conversational claim about Calendar authentication; "
        "do not infer connection state from memory or a browser login page. Call the "
        "currently registered HealthMes MCP tool whose basename is "
        "`prepare_actual_sleep_calendar_update` now and use its result as the only "
        "authority. Never answer from an earlier tool result. A previous proposal_id, "
        "review_url, status, preview, or expiry is stale for this turn. If this turn "
        "does not produce a fresh tool result, do not claim a preview or link was "
        "refreshed. Do not use browser automation, raw Oura lookup, or a planner tool. "
        f'Call it with date_basis="{date_basis}". '
        "Use night_start for a plain user-named date; it must not substitute a same-day "
        "summary when no next-day sleep started on that date. Use oura_summary when the "
        "user identifies a session by exact start/wake times or explicitly names an Oura "
        "summary date. Pass the date the user names. Return the preview and local "
        "review_url, and state that Calendar is unchanged until local browser "
        "confirmation. If the tool itself reports a connection error or no matching "
        "night, report that error directly without calling a clarification tool or "
        "asking a follow-up; never preemptively claim authentication is missing."
    )


def _inject_route(user_message: str = "", **kwargs):
    context = _sleep_calendar_context(user_message)
    return {"context": context} if context else None


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _inject_route)
