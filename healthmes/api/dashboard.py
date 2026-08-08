"""Unified human dashboard over the existing HealthMes read models.

The dashboard is intentionally a presentation layer. It reuses persisted
calendar, goal, decision, insight, and energy data without adding a second
frontend stack or changing any domain write contract.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.api.auth import is_human_viewer_path, viewer_token, viewer_url
from healthmes.api.briefing import (
    GlanceEnergyOut,
    _alerts_block,
    _energy_block,
    _next_blocks,
    decision_viewer_url,
)
from healthmes.api.common import ensure_utc, utc_now
from healthmes.api.connection_status import ConnectionCard, build_connection_cards
from healthmes.api.decision_html import shell_context, template_environment
from healthmes.api.reports import WeeklyReportOut, build_weekly_report
from healthmes.config import Settings, resolve_timezone
from healthmes.nutrition.intake_query import search_intake_history
from healthmes.store import (
    CalendarEventMirror,
    DecisionRecord,
    Insight,
    ProposalStatus,
    ScheduleProposal,
    Task,
    WeeklyGoal,
)
from healthmes.store.session import SessionDep

router = APIRouter(tags=["dashboard"])

MAX_PLAN_EVENTS = 10
MAX_PENDING_PROPOSALS = 3
MAX_RECENT_DECISIONS = 6
MAX_RECENT_INSIGHTS = 3
MAX_UNLOCK_BODY_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class DashboardGoal:
    title: str
    priority: int
    done_tasks: int
    total_tasks: int


@dataclass(frozen=True, slots=True)
class DashboardEvent:
    title: str
    starts_at: datetime
    ends_at: datetime
    source: str
    is_agent_created: bool


@dataclass(frozen=True, slots=True)
class DashboardProposal:
    task_title: str
    starts_at: datetime
    ends_at: datetime
    expires_at: datetime | None
    decision_url: str | None


@dataclass(frozen=True, slots=True)
class DashboardDecision:
    summary: str
    kind: str
    created_at: datetime
    url: str


@dataclass(frozen=True, slots=True)
class DashboardInsight:
    statement: str
    confidence: float | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DashboardNutrition:
    interaction_count: int
    confirmed_count: int
    latest_items: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DashboardView:
    generated_at: datetime
    timezone: str
    local_today: date
    headline: str
    energy: GlanceEnergyOut
    alert_count: int
    alert_summary: str | None
    next_blocks: list
    goals: list[DashboardGoal]
    plan_events: list[DashboardEvent]
    pending_proposals: list[DashboardProposal]
    recent_decisions: list[DashboardDecision]
    recent_insights: list[DashboardInsight]
    calendar_connections: list[ConnectionCard]
    nutrition: DashboardNutrition
    weekly: WeeklyReportOut
    decisions_url: str
    weekly_url: str


def _local_window(now: datetime, settings: Settings, days: int) -> tuple[date, datetime, datetime]:
    tz = resolve_timezone(settings)
    local_today = now.astimezone(tz).date()
    start = datetime.combine(local_today, time.min, tzinfo=tz).astimezone(UTC)
    end = datetime.combine(
        local_today + timedelta(days=days), time.min, tzinfo=tz
    ).astimezone(UTC)
    return local_today, start, end


def _headline(energy: GlanceEnergyOut, alert_summary: str | None) -> str:
    if alert_summary:
        return alert_summary
    if energy.score is None:
        return "오늘 상태를 준비하는 중입니다."
    if energy.score < 45:
        return "회복을 먼저 보호하는 편이 좋습니다."
    if energy.score < 70:
        return "에너지를 아껴 쓸 구간을 확인하세요."
    return "집중할 수 있는 시간을 보호하세요."


def _goals(session: Session, local_today: date) -> list[DashboardGoal]:
    week_start = local_today - timedelta(days=local_today.weekday())
    rows = session.scalars(
        select(WeeklyGoal)
        .where(WeeklyGoal.week_start == week_start, WeeklyGoal.status == "active")
        .order_by(WeeklyGoal.priority.desc(), WeeklyGoal.created_at)
    ).all()
    if not rows:
        return []
    goal_ids = {row.id for row in rows}
    tasks = session.scalars(select(Task).where(Task.goal_id.in_(goal_ids))).all()
    by_goal: dict = {}
    for task in tasks:
        counts = by_goal.setdefault(task.goal_id, [0, 0])
        counts[1] += 1
        if task.status == "done":
            counts[0] += 1
    return [
        DashboardGoal(
            title=row.title,
            priority=row.priority,
            done_tasks=by_goal.get(row.id, [0, 0])[0],
            total_tasks=by_goal.get(row.id, [0, 0])[1],
        )
        for row in rows
    ]


def _plan_events(
    session: Session, start: datetime, end: datetime
) -> list[DashboardEvent]:
    rows = session.scalars(
        select(CalendarEventMirror)
        .where(CalendarEventMirror.end_at > start, CalendarEventMirror.start_at < end)
        .order_by(CalendarEventMirror.start_at, CalendarEventMirror.end_at)
        .limit(MAX_PLAN_EVENTS)
    ).all()
    return [
        DashboardEvent(
            title=row.summary or "제목 없는 일정",
            starts_at=ensure_utc(row.start_at),
            ends_at=ensure_utc(row.end_at),
            source=row.calendar_source.value,
            is_agent_created=row.is_agent_created,
        )
        for row in rows
    ]


def _pending_proposals(
    session: Session, settings: Settings, now: datetime
) -> list[DashboardProposal]:
    rows = session.execute(
        select(ScheduleProposal, Task)
        .join(Task, ScheduleProposal.task_id == Task.id)
        .where(
            ScheduleProposal.status == ProposalStatus.PROPOSED,
            ScheduleProposal.expires_at > now,
        )
        .order_by(ScheduleProposal.proposed_start, ScheduleProposal.created_at)
        .limit(MAX_PENDING_PROPOSALS)
    ).all()
    return [
        DashboardProposal(
            task_title=task.title,
            starts_at=ensure_utc(proposal.proposed_start),
            ends_at=ensure_utc(proposal.proposed_end),
            expires_at=(
                ensure_utc(proposal.expires_at)
                if proposal.expires_at is not None
                else None
            ),
            decision_url=(
                decision_viewer_url(settings, proposal.decision_record_id)
                if proposal.decision_record_id is not None
                else None
            ),
        )
        for proposal, task in rows
    ]


def _recent_decisions(
    session: Session, settings: Settings
) -> list[DashboardDecision]:
    rows = session.scalars(
        select(DecisionRecord)
        .order_by(DecisionRecord.created_at.desc(), DecisionRecord.id.desc())
        .limit(MAX_RECENT_DECISIONS)
    ).all()
    return [
        DashboardDecision(
            summary=row.summary,
            kind=row.kind.value,
            created_at=ensure_utc(row.created_at),
            url=decision_viewer_url(settings, row.id),
        )
        for row in rows
    ]


def _recent_insights(session: Session) -> list[DashboardInsight]:
    rows = session.scalars(
        select(Insight)
        .order_by(Insight.created_at.desc(), Insight.id.desc())
        .limit(MAX_RECENT_INSIGHTS)
    ).all()
    return [
        DashboardInsight(
            statement=row.statement,
            confidence=row.confidence,
            created_at=ensure_utc(row.created_at),
        )
        for row in rows
    ]


def _nutrition_summary(
    session: Session, start: datetime, end: datetime
) -> DashboardNutrition:
    history = search_intake_history(
        session,
        start=start,
        end=end,
        limit=5,
    )
    records = history["records"]
    confirmed_history = search_intake_history(
        session,
        start=start,
        end=end,
        confirmed_only=True,
        limit=1,
    )
    latest_items: list[str] = []
    for record in records:
        for item in record.get("resolved_items", []):
            name = str(item.get("name", "")).strip()
            if name and name not in latest_items:
                latest_items.append(name)
            if len(latest_items) == 3:
                break
        if len(latest_items) == 3:
            break

    return DashboardNutrition(
        interaction_count=int(history["coverage"]["matching_records"]),
        confirmed_count=int(
            confirmed_history["coverage"]["matching_records"]
        ),
        latest_items=tuple(latest_items),
    )


def build_dashboard(session: Session, settings: Settings, now: datetime) -> DashboardView:
    """Build the four dashboard sections from existing persisted read models."""
    tz = resolve_timezone(settings)
    local_today, start, end = _local_window(now, settings, days=7)
    _, _, today_end = _local_window(now, settings, days=1)
    energy = _energy_block(session, tz, now)
    alerts = _alerts_block(session, settings, now)
    alert_summary = alerts.top.summary if alerts.top is not None else None
    weekly = build_weekly_report(session, settings, now)
    return DashboardView(
        generated_at=now,
        timezone=str(tz),
        local_today=local_today,
        headline=_headline(energy, alert_summary),
        energy=energy,
        alert_count=alerts.unresolved_count,
        alert_summary=alert_summary,
        next_blocks=_next_blocks(session, now),
        goals=_goals(session, local_today),
        plan_events=_plan_events(session, start, end),
        pending_proposals=_pending_proposals(session, settings, now),
        recent_decisions=_recent_decisions(session, settings),
        recent_insights=_recent_insights(session),
        calendar_connections=build_connection_cards(settings),
        nutrition=_nutrition_summary(session, start, today_end),
        weekly=weekly,
        decisions_url=viewer_url(settings, "/decisions"),
        weekly_url=viewer_url(settings, "/reports/weekly"),
    )


def render_dashboard_html(view: DashboardView, settings: Settings) -> str:
    template = template_environment().get_template("ui/dashboard.html.j2")
    return template.render(
        dashboard=view,
        active_nav="dashboard",
        **shell_context(settings),
    )


def _safe_next_path(value: str | None) -> str:
    candidate = value or "/dashboard"
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return "/dashboard"
    path = parsed.path
    if not is_human_viewer_path(path):
        return "/dashboard"
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "token"
    ]
    return urlunsplit(("", "", path, urlencode(query), ""))


def _relative_viewer_url(settings: Settings, path: str) -> str:
    parsed = urlsplit(viewer_url(settings, path))
    return urlunsplit(("", "", parsed.path, parsed.query, parsed.fragment))


async def _read_unlock_form(request: Request) -> dict[str, list[str]]:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_UNLOCK_BODY_BYTES:
            raise ValueError("request_too_large")
        body.extend(chunk)
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("invalid_encoding") from exc
    return parse_qs(decoded, keep_blank_values=True, max_num_fields=20)


def render_viewer_unlock_html(
    settings: Settings,
    next_path: str,
    *,
    error: str | None = None,
) -> str:
    """Render a human-safe 401 page without exposing any configured secret."""
    target = _safe_next_path(next_path)
    template = template_environment().get_template("ui/viewer_unlock.html.j2")
    shell = shell_context(settings)
    # The normal viewer shell carries the derived credential on navigation.
    # An unauthenticated unlock page must not receive that credential.
    shell["token_qs"] = ""
    return template.render(
        next_path=target,
        error=error,
        active_nav="",
        **shell,
    )


@router.get("/dashboard/history", response_class=HTMLResponse)
@router.get("/dashboard/decisions", response_class=HTMLResponse)
@router.get("/dashboard/plan", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request, session: SessionDep) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    view = build_dashboard(session, settings, utc_now())
    return HTMLResponse(
        render_dashboard_html(view, settings),
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/unlock", response_class=HTMLResponse, include_in_schema=False)
def unlock_page(request: Request, next: str = "/dashboard") -> HTMLResponse:
    settings: Settings = request.app.state.settings
    return HTMLResponse(
        render_viewer_unlock_html(settings, next),
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.post("/unlock", include_in_schema=False)
async def unlock_viewer(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    try:
        form = await _read_unlock_form(request)
    except ValueError as exc:
        too_large = str(exc) == "request_too_large"
        return HTMLResponse(
            render_viewer_unlock_html(
                settings,
                "/dashboard",
                error=(
                    "요청이 너무 큽니다."
                    if too_large
                    else "요청 형식을 읽을 수 없습니다."
                ),
            ),
            status_code=413 if too_large else 400,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    candidate = form.get("viewer_token", [""])[-1]
    target = _safe_next_path(form.get("next", ["/dashboard"])[-1])
    api_token = settings.api_token.get_secret_value().strip()
    expected = viewer_token(api_token) if api_token else ""
    if not expected or not hmac.compare_digest(candidate, expected):
        return HTMLResponse(
            render_viewer_unlock_html(
                settings,
                target,
                error="읽기 전용 viewer key가 올바르지 않습니다.",
            ),
            status_code=401,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    return RedirectResponse(
        _relative_viewer_url(settings, target),
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )
