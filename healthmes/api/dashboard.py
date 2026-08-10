"""Unified human dashboard over the existing HealthMes read models.

The dashboard is intentionally a presentation layer. It reuses persisted
calendar, goal, decision, insight, and energy data without adding a second
frontend stack or changing any domain write contract.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
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
from healthmes.calendars.jobs import enabled_sources
from healthmes.calendars.state import FileSyncStateStore
from healthmes.config import Settings, resolve_timezone
from healthmes.nutrition.intake_query import search_intake_history
from healthmes.store import (
    CalendarEventMirror,
    CalendarSource,
    DecisionKind,
    DecisionRecord,
    Insight,
    ProposalStatus,
    ScheduleProposal,
    Task,
    WeeklyGoal,
)
from healthmes.store.session import SessionDep

router = APIRouter(tags=["dashboard"])

MAX_PLAN_EVENTS = 100
MAX_PENDING_PROPOSALS = 3
MAX_CALENDAR_PROPOSALS = 100
MAX_RECENT_DECISIONS = 6
MAX_RECENT_INSIGHTS = 3
MAX_UNLOCK_BODY_BYTES = 16 * 1024
CALENDAR_FRESHNESS_LIMIT = timedelta(minutes=30)
CALENDAR_FUTURE_SKEW_LIMIT = timedelta(minutes=5)
PROPOSAL_PROVENANCE_WINDOW = timedelta(minutes=15)

CalendarSyncStatus = Literal["current", "stale", "future_skew", "unconfirmed"]


@dataclass(frozen=True, slots=True)
class DashboardGoal:
    title: str
    priority: int
    done_tasks: int
    total_tasks: int


@dataclass(frozen=True, slots=True)
class DashboardEvent:
    external_id: str
    title: str
    starts_at: datetime
    ends_at: datetime
    source: str
    calendar_id: str
    calendar_name: str
    calendar_color: str
    is_agent_created: bool
    agent_task_id: str | None
    is_all_day: bool
    is_recurring: bool
    is_locked: bool
    has_attendees: bool
    organizer_self: bool
    status: str | None
    energy_demand: str | None


@dataclass(frozen=True, slots=True)
class DashboardProposal:
    id: str
    task_id: str
    task_title: str
    starts_at: datetime
    ends_at: datetime
    expires_at: datetime | None
    decision_record_id: str | None
    decision_url: str | None
    decision_summary: str | None
    decision_has_trusted_provenance: bool


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
    plan_events_total: int
    plan_events_truncated: bool
    calendar_sources: tuple[str, ...]
    calendar_sync_observed_at: dict[str, datetime]
    calendar_sync_statuses: dict[str, CalendarSyncStatus]
    schedule_approval_available: bool
    pending_proposals: list[DashboardProposal]
    calendar_proposals: list[DashboardProposal]
    calendar_proposals_total: int
    calendar_proposals_truncated: bool
    pending_proposals_total: int
    pending_proposals_truncated: bool
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
    end = datetime.combine(local_today + timedelta(days=days), time.min, tzinfo=tz).astimezone(UTC)
    return local_today, start, end


def _headline(energy: GlanceEnergyOut, alert_summary: str | None) -> str:
    if alert_summary:
        return alert_summary
    if energy.score is None:
        return "오늘 상태 데이터가 아직 없습니다."
    if energy.score < 45:
        return "현재 저장된 에너지 점수는 낮은 구간입니다."
    if energy.score < 70:
        return "현재 저장된 에너지 점수는 중간 구간입니다."
    return "현재 저장된 에너지 점수는 높은 구간입니다."


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
    session: Session,
    settings: Settings,
    start: datetime,
    end: datetime,
) -> tuple[list[DashboardEvent], int]:
    filters = (
        CalendarEventMirror.end_at > start,
        CalendarEventMirror.start_at < end,
    )
    total = (
        session.scalar(select(func.count()).select_from(CalendarEventMirror).where(*filters)) or 0
    )
    rows = session.scalars(
        select(CalendarEventMirror)
        .where(*filters)
        .order_by(
            CalendarEventMirror.start_at,
            CalendarEventMirror.end_at,
            CalendarEventMirror.external_id,
        )
        .limit(MAX_PLAN_EVENTS)
    ).all()
    task_ids = {row.agent_task_id for row in rows if row.agent_task_id is not None}
    task_demands = (
        {
            task.id: (task.energy_demand.value if task.energy_demand is not None else None)
            for task in session.scalars(select(Task).where(Task.id.in_(task_ids))).all()
        }
        if task_ids
        else {}
    )
    return (
        [
            DashboardEvent(
                external_id=row.external_id,
                title=row.summary or "제목 없는 일정",
                starts_at=ensure_utc(row.start_at),
                ends_at=ensure_utc(row.end_at),
                source=row.calendar_source.value,
                calendar_id=(
                    settings.google_calendar_id
                    if row.calendar_source is CalendarSource.GOOGLE
                    else settings.caldav_calendar_name or "default"
                ),
                calendar_name=(
                    f"Google · {settings.google_calendar_id}"
                    if row.calendar_source is CalendarSource.GOOGLE
                    else f"Apple · {settings.caldav_calendar_name or 'iCloud'}"
                ),
                calendar_color=(
                    "#4285F4"
                    if row.calendar_source is CalendarSource.GOOGLE
                    else "#34C759"
                ),
                is_agent_created=row.is_agent_created,
                agent_task_id=(str(row.agent_task_id) if row.agent_task_id is not None else None),
                is_all_day=row.is_all_day,
                is_recurring=row.is_recurring,
                is_locked=row.is_locked,
                has_attendees=row.has_attendees,
                organizer_self=row.organizer_self,
                status=row.status,
                energy_demand=task_demands.get(row.agent_task_id),
            )
            for row in rows
        ],
        total,
    )


def _calendar_sync_observed_at(settings: Settings) -> dict[str, datetime]:
    """Return durable evidence of the latest successful provider sync.

    Calendar events are intentionally not rewritten when an unchanged provider
    event is observed, so row timestamps cannot establish mirror freshness.
    The sync-state file is atomically saved only after a successful provider
    poll and its modification time is therefore the presentation layer's
    conservative freshness evidence.
    """
    store = FileSyncStateStore.for_data_dir(settings.data_dir)
    observed: dict[str, datetime] = {}
    for source in (CalendarSource.GOOGLE, CalendarSource.CALDAV):
        path = store.path_for(source)
        try:
            if store.load(source) is None:
                continue
            observed[source.value] = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        except OSError:
            continue
    return observed


def calendar_sync_status(
    observed_at: datetime | None,
    now: datetime,
) -> CalendarSyncStatus:
    """Classify provider sync evidence for every presentation surface."""
    if observed_at is None:
        return "unconfirmed"
    observed_at = ensure_utc(observed_at)
    now = ensure_utc(now)
    if observed_at - now > CALENDAR_FUTURE_SKEW_LIMIT:
        return "future_skew"
    if max(now - observed_at, timedelta()) > CALENDAR_FRESHNESS_LIMIT:
        return "stale"
    return "current"


def _decision_has_trusted_provenance(
    decision: DecisionRecord | None,
    proposal: ScheduleProposal,
    now: datetime,
) -> bool:
    """Validate server-owned proposal provenance without interpreting free-form trees."""
    if (
        decision is None
        or proposal.decision_record_id is None
        or decision.id != proposal.decision_record_id
        or decision.kind is not DecisionKind.SCHEDULE_CHANGE
    ):
        return False

    decision_created_at = ensure_utc(decision.created_at)
    proposal_created_at = ensure_utc(proposal.created_at)
    now = ensure_utc(now)
    if (
        decision_created_at > now + CALENDAR_FUTURE_SKEW_LIMIT
        or proposal_created_at > now + CALENDAR_FUTURE_SKEW_LIMIT
    ):
        return False
    return abs(decision_created_at - proposal_created_at) <= PROPOSAL_PROVENANCE_WINDOW


def _pending_proposals(
    session: Session,
    settings: Settings,
    now: datetime,
    *,
    range_start: datetime | None = None,
    range_end: datetime | None = None,
    limit: int | None = MAX_PENDING_PROPOSALS,
) -> tuple[list[DashboardProposal], int]:
    filters = [
        ScheduleProposal.status == ProposalStatus.PROPOSED,
        ScheduleProposal.expires_at > now,
    ]
    if range_start is not None:
        filters.append(ScheduleProposal.proposed_end > range_start)
    if range_end is not None:
        filters.append(ScheduleProposal.proposed_start < range_end)
    total = session.scalar(select(func.count()).select_from(ScheduleProposal).where(*filters)) or 0
    query = (
        select(ScheduleProposal, Task, DecisionRecord)
        .join(Task, ScheduleProposal.task_id == Task.id)
        .outerjoin(
            DecisionRecord,
            ScheduleProposal.decision_record_id == DecisionRecord.id,
        )
        .where(*filters)
        .order_by(ScheduleProposal.proposed_start, ScheduleProposal.created_at)
    )
    if limit is not None:
        query = query.limit(limit)
    rows = session.execute(query).all()
    return (
        [
            _proposal_projection(proposal, task, decision, settings, now)
            for proposal, task, decision in rows
        ],
        total,
    )


def _proposal_projection(
    proposal: ScheduleProposal,
    task: Task,
    decision: DecisionRecord | None,
    settings: Settings,
    now: datetime,
) -> DashboardProposal:
    trusted_provenance = _decision_has_trusted_provenance(decision, proposal, now)
    return DashboardProposal(
        id=str(proposal.id),
        task_id=str(proposal.task_id),
        task_title=task.title,
        starts_at=ensure_utc(proposal.proposed_start),
        ends_at=ensure_utc(proposal.proposed_end),
        expires_at=(ensure_utc(proposal.expires_at) if proposal.expires_at is not None else None),
        decision_record_id=(
            str(proposal.decision_record_id) if proposal.decision_record_id is not None else None
        ),
        decision_url=(
            decision_viewer_url(settings, proposal.decision_record_id)
            if proposal.decision_record_id is not None and trusted_provenance
            else None
        ),
        decision_summary=decision.summary if decision is not None and trusted_provenance else None,
        decision_has_trusted_provenance=trusted_provenance,
    )


def pending_proposal_by_id(
    session: Session,
    settings: Settings,
    now: datetime,
    proposal_id: UUID,
) -> DashboardProposal | None:
    """Project one exact active proposal, independent of dashboard list limits."""
    row = session.execute(
        select(ScheduleProposal, Task, DecisionRecord)
        .join(Task, ScheduleProposal.task_id == Task.id)
        .outerjoin(
            DecisionRecord,
            ScheduleProposal.decision_record_id == DecisionRecord.id,
        )
        .where(
            ScheduleProposal.id == proposal_id,
            ScheduleProposal.status == ProposalStatus.PROPOSED,
            ScheduleProposal.expires_at > now,
        )
    ).one_or_none()
    if row is None:
        return None
    proposal, task, decision = row
    return _proposal_projection(proposal, task, decision, settings, now)


def _recent_decisions(session: Session, settings: Settings) -> list[DashboardDecision]:
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


def _nutrition_summary(session: Session, start: datetime, end: datetime) -> DashboardNutrition:
    history = search_intake_history(
        session,
        start=start,
        end=end,
        limit=5,
    )
    confirmed_history = search_intake_history(
        session,
        start=start,
        end=end,
        confirmed_only=True,
        limit=5,
    )
    latest_items: list[str] = []
    for record in confirmed_history["records"]:
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
        confirmed_count=int(confirmed_history["coverage"]["matching_records"]),
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
    plan_events, plan_events_total = _plan_events(session, settings, start, end)
    calendar_sources = tuple(
        sorted(
            {
                *(source.value for source in enabled_sources(settings)),
                *(event.source for event in plan_events),
            }
        )
    )
    pending_proposals, pending_proposals_total = _pending_proposals(session, settings, now)
    calendar_proposals, calendar_proposals_total = _pending_proposals(
        session,
        settings,
        now,
        range_start=start,
        range_end=end,
        limit=MAX_CALENDAR_PROPOSALS,
    )
    calendar_sync_observed_at = _calendar_sync_observed_at(settings)
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
        plan_events=plan_events,
        plan_events_total=plan_events_total,
        plan_events_truncated=plan_events_total > len(plan_events),
        calendar_sources=calendar_sources,
        calendar_sync_observed_at=calendar_sync_observed_at,
        calendar_sync_statuses={
            source: calendar_sync_status(calendar_sync_observed_at.get(source), now)
            for source in calendar_sources
        },
        schedule_approval_available=(
            len(settings.calendar_adjustment_secret.get_secret_value().strip()) >= 32
        ),
        pending_proposals=pending_proposals,
        calendar_proposals=calendar_proposals,
        calendar_proposals_total=calendar_proposals_total,
        calendar_proposals_truncated=(
            calendar_proposals_total > len(calendar_proposals)
        ),
        pending_proposals_total=pending_proposals_total,
        pending_proposals_truncated=(pending_proposals_total > len(pending_proposals)),
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
                error=("요청이 너무 큽니다." if too_large else "요청 형식을 읽을 수 없습니다."),
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
