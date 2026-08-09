"""FastMCP registration for identity-free Activity Wellness context."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime, tzinfo
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from sqlalchemy.orm import Session

from healthmes.activity.context import (
    activity_summary_context,
    focus_context,
    overwork_context,
)
from healthmes.activity.contracts import ActivityContextResolveRequest
from healthmes.activity.resolver import resolve_wellness_context as resolve_context

StoreSessionFactory = Callable[[], AbstractContextManager[Session]]
TimezoneResolver = Callable[[], tzinfo]
ReadinessReader = Callable[[str | None], Any]

_registered_mcp_ids: set[int] = set()


def _day(value: str | None, timezone: tzinfo) -> date:
    if value is None:
        return datetime.now(timezone).date()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ToolError(f"date must be ISO YYYY-MM-DD, got {value!r}") from exc


def _aware(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolError(f"{field} must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        raise ToolError(f"{field} must include a timezone offset")
    return parsed.astimezone(UTC)


def register_activity_tools(
    mcp: FastMCP,
    *,
    store_session_factory: StoreSessionFactory,
    timezone_resolver: TimezoneResolver,
    readiness_reader: ReadinessReader,
) -> None:
    """Register once on the process-global HealthMes MCP server."""
    identity = id(mcp)
    if identity in _registered_mcp_ids:
        return
    _registered_mcp_ids.add(identity)

    @mcp.tool
    def get_activity_summary(date: str | None = None) -> dict[str, Any]:
        """Return one local day's deterministic activity summary without app identity."""
        timezone = timezone_resolver()
        day = _day(date, timezone)
        with store_session_factory() as session:
            return activity_summary_context(
                session,
                day=day,
                timezone=timezone,
            )

    @mcp.tool
    def get_focus_context(start: str, end: str) -> dict[str, Any]:
        """Return fragmentation and sustained-block context for an explicit time window."""
        start_at = _aware(start, "start")
        end_at = _aware(end, "end")
        if start_at >= end_at:
            raise ToolError("start must be before end")
        with store_session_factory() as session:
            return focus_context(
                session,
                start=start_at,
                end=end_at,
                timezone=timezone_resolver(),
            )

    @mcp.tool
    def get_overwork_context(
        date: str | None = None,
        lookback_days: int = 7,
    ) -> dict[str, Any]:
        """Return bounded overwork signals from activity totals, blocks, late use, and baseline."""
        if not 1 <= lookback_days <= 90:
            raise ToolError("lookback_days must be between 1 and 90")
        timezone = timezone_resolver()
        day = _day(date, timezone)
        with store_session_factory() as session:
            return overwork_context(
                session,
                day=day,
                timezone=timezone,
                lookback_days=lookback_days,
            )

    @mcp.tool
    async def resolve_wellness_context(
        question_kind: str,
        date: str | None = None,
        start: str | None = None,
        end: str | None = None,
        lookback_days: int = 7,
        nutrition_request_id: str | None = None,
    ) -> dict[str, Any]:
        """Select only the domains needed for one bounded cross-domain wellness question."""
        timezone = timezone_resolver()
        parsed_start = _aware(start, "start") if start is not None else None
        parsed_end = _aware(end, "end") if end is not None else None
        try:
            request = ActivityContextResolveRequest(
                question_kind=question_kind,
                date=date,
                start=parsed_start,
                end=parsed_end,
                lookback_days=lookback_days,
                nutrition_request_id=nutrition_request_id,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

        async def wearable(day: date) -> dict[str, Any]:
            result = readiness_reader(day.isoformat())
            if hasattr(result, "__await__"):
                return await result
            return result

        with store_session_factory() as session:
            return await resolve_context(
                session,
                request,
                default_timezone=timezone,
                wearable_reader=wearable,
            )
