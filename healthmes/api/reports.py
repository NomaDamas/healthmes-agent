"""Weekly report page — the second decision-viewer web surface (docs/PLAN.md §8.5).

PLAN §8.5 defines the viewer surface as "Mermaid tree + weekly report page":
alongside the per-decision Mermaid pages this module serves the
week-at-a-glance digest that the Sunday planning briefing links
(``{public_base_url}/reports/weekly``).

Routes (same conventions as healthmes/api/decisions.py — bare plan-verbatim
paths, mobile-friendly Jinja page, local assets only — the sparkline is
inline SVG, no external libraries):

- ``GET /reports/weekly`` — HTML digest of the last 7 local days:
  energy trend (per-day averages of *persisted* ``cognitive_energy_estimate``
  windows drawn as an inline SVG sparkline), the week's insight rows with
  confidence badges, schedule adherence (proposal status counts), the alert
  digest (fired vs delivered vs the weekly alert budget), and the week's
  decision records, each linking its ``/decisions/{id}`` page.
- ``GET /reports/weekly.json`` — the same numbers as JSON (the ``.json``
  suffix mirrors ``/decisions/{id}.json``). Both routes render from a single
  :func:`build_weekly_report` call, so HTML/JSON parity holds by construction.

Honesty rules follow the glance briefing (healthmes/api/briefing.py): only
persisted energy windows are shown — days without data stay ``null``/"—",
never computed or fetched on view; counts come straight from the store; and
the confidence-badge thresholds are documented placeholders for the
healthcare domain expert to refine (issue #7: plumbing + placeholder
rendering, not final UX).

Auth & links: the shared bearer middleware gates both routes like the rest of
the surface; as GET pages under ``/reports`` they additionally accept the
derived read-only ``?token=`` viewer credential (healthmes/api/auth.py), so
the report link stays tappable from a phone browser. Decision links reuse
:func:`healthmes.api.briefing.decision_viewer_url`, and the report's own
:func:`weekly_report_url` is built by the same single construction point,
:func:`healthmes.api.auth.viewer_url` — never re-implemented here.
"""

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.api.auth import viewer_url
from healthmes.api.briefing import decision_viewer_url
from healthmes.api.common import ensure_utc, utc_now

# Package-internal reuse of the decision viewer's cached Jinja environment and
# timestamp formatter — the report template lives in the same templates/
# directory and follows the same conventions (autoescape on, local-first).
from healthmes.api.decision_html import format_created, template_environment
from healthmes.config import Settings, resolve_timezone
from healthmes.store import (
    CognitiveEnergyEstimate,
    DecisionKind,
    DecisionRecord,
    Insight,
    ProposalStatus,
    ScheduleProposal,
    TriggerEvent,
)
from healthmes.store.session import SessionDep

__all__ = [
    "router",
    "REPORT_DAYS",
    "WEEKLY_REPORT_PATH",
    "confidence_level",
    "weekly_report_url",
    "build_weekly_report",
    "build_energy_sparkline",
    "render_weekly_report_html",
]

router = APIRouter(tags=["reports"])

WEEKLY_REPORT_PATH = "/reports/weekly"
REPORT_DAYS = 7

# Item caps keep the page phone-friendly; the counts always reflect the whole
# week (the caps are display-only and each capped section says so).
MAX_REPORT_INSIGHTS = 50
MAX_REPORT_DECISIONS = 20

# Confidence badge ladder over Insight.confidence in [0, 1] — placeholder
# thresholds for the healthcare domain expert to refine (issue #7).
CONFIDENCE_HIGH_MIN = 0.75
CONFIDENCE_MEDIUM_MIN = 0.4

# Sparkline geometry in viewBox units (the SVG scales to its container).
# Score 0..100 maps linearly to y SPARK_Y_BOTTOM..SPARK_Y_TOP.
SPARK_X_STEP = 48
SPARK_HEIGHT = 72
SPARK_Y_TOP = 6
SPARK_Y_BOTTOM = 66
SPARK_GRID_SCORES = (0, 50, 100)


# ---------------------------------------------------------------------------
# Response models (one payload, rendered as both JSON and HTML)
# ---------------------------------------------------------------------------


class EnergyDayOut(BaseModel):
    """One local day of the energy trend (``null`` scores = honestly missing)."""

    date: date
    avg_score: int | None
    min_score: int | None
    max_score: int | None
    samples: int


class WeeklyEnergyOut(BaseModel):
    """Per-day energy aggregates over the report window."""

    days: list[EnergyDayOut]
    overall_avg: int | None
    samples: int


class ReportInsightOut(BaseModel):
    """An insight row of the week with its badge level."""

    id: uuid.UUID
    period: str
    kind: str
    statement: str
    confidence: float | None
    confidence_level: Literal["high", "medium", "low", "none"]
    created_at: datetime


class WeeklyInsightsOut(BaseModel):
    """Insight digest (``count`` is the full-week total; ``items`` is capped)."""

    count: int
    items: list[ReportInsightOut]


class ScheduleAdherenceOut(BaseModel):
    """Current status counts of the proposals created during the week."""

    proposed: int
    accepted: int
    pushed: int
    declined: int
    decided: int
    """accepted + pushed + declined (``proposed`` is still pending)."""
    acceptance_pct: int | None
    """round(100 * (accepted + pushed) / decided); null when nothing was decided."""


class AlertRuleCountOut(BaseModel):
    """Per-rule slice of the alert digest."""

    rule_id: str
    fired: int
    delivered: int


class AlertDigestOut(BaseModel):
    """Trigger events of the week: fired vs delivered vs the alert budget."""

    fired: int
    delivered: int
    daily_budget: int
    weekly_budget: int
    by_rule: list[AlertRuleCountOut]


class ReportDecisionOut(BaseModel):
    """One decision record of the week with its viewer link."""

    id: uuid.UUID
    kind: DecisionKind
    summary: str
    created_at: datetime
    url: str


class WeeklyDecisionsOut(BaseModel):
    """Decision digest (``count``/``kind_counts`` full week; ``items`` capped)."""

    count: int
    kind_counts: dict[str, int]
    items: list[ReportDecisionOut]


class WeeklyReportOut(BaseModel):
    """Response of ``GET /reports/weekly.json`` (and the HTML page's context)."""

    generated_at: datetime
    timezone: str
    week_start: date
    week_end: date
    report_url: str
    energy: WeeklyEnergyOut
    insights: WeeklyInsightsOut
    schedule: ScheduleAdherenceOut
    alerts: AlertDigestOut
    decisions: WeeklyDecisionsOut


# ---------------------------------------------------------------------------
# Viewer links (healthmes.api.auth.viewer_url — one construction point)
# ---------------------------------------------------------------------------


def weekly_report_url(settings: Settings) -> str:
    """Browser-tappable link to the weekly report page (Sunday briefing link)."""
    return viewer_url(settings, WEEKLY_REPORT_PATH)


# ---------------------------------------------------------------------------
# Section builders (pure store reads; datetimes normalised to aware UTC)
# ---------------------------------------------------------------------------


def confidence_level(confidence: float | None) -> Literal["high", "medium", "low", "none"]:
    """Badge level for an insight confidence (placeholder ladder, see module doc)."""
    if confidence is None:
        return "none"
    if confidence >= CONFIDENCE_HIGH_MIN:
        return "high"
    if confidence >= CONFIDENCE_MEDIUM_MIN:
        return "medium"
    return "low"


def _in_window(value: datetime, window_start: datetime, window_end: datetime) -> bool:
    """Aware-UTC window check (sqlite reads are naive; re-verify in Python)."""
    return window_start <= ensure_utc(value) < window_end


def _energy_section(
    session: Session,
    tz: tzinfo,
    day_dates: list[date],
    window_start: datetime,
    window_end: datetime,
) -> WeeklyEnergyOut:
    """Per-local-day aggregates of the persisted hourly energy windows."""
    rows = session.scalars(
        select(CognitiveEnergyEstimate).where(
            CognitiveEnergyEstimate.window_start >= window_start,
            CognitiveEnergyEstimate.window_start < window_end,
        )
    ).all()

    by_day: dict[date, list[int]] = defaultdict(list)
    for row in rows:
        start = ensure_utc(row.window_start)
        if not _in_window(start, window_start, window_end):
            continue
        by_day[start.astimezone(tz).date()].append(row.score)

    days: list[EnergyDayOut] = []
    all_scores: list[int] = []
    for day in day_dates:
        scores = by_day.get(day, [])
        all_scores.extend(scores)
        days.append(
            EnergyDayOut(
                date=day,
                avg_score=round(sum(scores) / len(scores)) if scores else None,
                min_score=min(scores) if scores else None,
                max_score=max(scores) if scores else None,
                samples=len(scores),
            )
        )
    overall = round(sum(all_scores) / len(all_scores)) if all_scores else None
    return WeeklyEnergyOut(days=days, overall_avg=overall, samples=len(all_scores))


def _insights_section(
    session: Session, window_start: datetime, window_end: datetime
) -> WeeklyInsightsOut:
    """Insight rows recorded during the week, newest first."""
    rows = [
        row
        for row in session.scalars(
            select(Insight)
            .where(Insight.created_at >= window_start, Insight.created_at < window_end)
            .order_by(Insight.created_at.desc(), Insight.id.desc())
        ).all()
        if _in_window(row.created_at, window_start, window_end)
    ]
    items = [
        ReportInsightOut(
            id=row.id,
            period=row.period,
            kind=row.kind,
            statement=row.statement,
            confidence=row.confidence,
            confidence_level=confidence_level(row.confidence),
            created_at=ensure_utc(row.created_at),
        )
        for row in rows[:MAX_REPORT_INSIGHTS]
    ]
    return WeeklyInsightsOut(count=len(rows), items=items)


def _schedule_section(
    session: Session, window_start: datetime, window_end: datetime
) -> ScheduleAdherenceOut:
    """Current status counts of the proposals created during the week."""
    rows = [
        row
        for row in session.scalars(
            select(ScheduleProposal).where(
                ScheduleProposal.created_at >= window_start,
                ScheduleProposal.created_at < window_end,
            )
        ).all()
        if _in_window(row.created_at, window_start, window_end)
    ]
    counts = Counter(row.status for row in rows)
    accepted = counts[ProposalStatus.ACCEPTED]
    pushed = counts[ProposalStatus.PUSHED]
    declined = counts[ProposalStatus.DECLINED]
    decided = accepted + pushed + declined
    return ScheduleAdherenceOut(
        proposed=counts[ProposalStatus.PROPOSED],
        accepted=accepted,
        pushed=pushed,
        declined=declined,
        decided=decided,
        acceptance_pct=round(100 * (accepted + pushed) / decided) if decided else None,
    )


def _alerts_section(
    session: Session, settings: Settings, window_start: datetime, window_end: datetime
) -> AlertDigestOut:
    """Trigger events of the week vs the configured alert budget (PLAN §11)."""
    rows = [
        row
        for row in session.scalars(
            select(TriggerEvent).where(
                TriggerEvent.fired_at >= window_start,
                TriggerEvent.fired_at < window_end,
            )
        ).all()
        if _in_window(row.fired_at, window_start, window_end)
    ]
    per_rule: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in rows:
        tally = per_rule[row.rule_id]
        tally[0] += 1
        if row.alert_sent:
            tally[1] += 1
    by_rule = [
        AlertRuleCountOut(rule_id=rule_id, fired=fired, delivered=delivered)
        for rule_id, (fired, delivered) in sorted(
            per_rule.items(), key=lambda item: (-item[1][0], item[0])
        )
    ]
    return AlertDigestOut(
        fired=len(rows),
        delivered=sum(1 for row in rows if row.alert_sent),
        daily_budget=settings.alert_daily_budget,
        weekly_budget=settings.alert_daily_budget * REPORT_DAYS,
        by_rule=by_rule,
    )


def _decisions_section(
    session: Session, settings: Settings, window_start: datetime, window_end: datetime
) -> WeeklyDecisionsOut:
    """Decision records of the week, newest first, each with its viewer link."""
    rows = [
        row
        for row in session.scalars(
            select(DecisionRecord)
            .where(
                DecisionRecord.created_at >= window_start,
                DecisionRecord.created_at < window_end,
            )
            .order_by(DecisionRecord.created_at.desc(), DecisionRecord.id.desc())
        ).all()
        if _in_window(row.created_at, window_start, window_end)
    ]
    kind_counts = {kind.value: 0 for kind in DecisionKind}
    for row in rows:
        kind_counts[row.kind.value] += 1
    items = [
        ReportDecisionOut(
            id=row.id,
            kind=row.kind,
            summary=row.summary,
            created_at=ensure_utc(row.created_at),
            url=decision_viewer_url(settings, row.id),
        )
        for row in rows[:MAX_REPORT_DECISIONS]
    ]
    return WeeklyDecisionsOut(count=len(rows), kind_counts=kind_counts, items=items)


def build_weekly_report(session: Session, settings: Settings, now: datetime) -> WeeklyReportOut:
    """Assemble the whole report for the 7 local days ending today.

    The single source of both routes: the JSON endpoint returns this model and
    the HTML page renders it, so the two views can never disagree. Day
    bucketing happens in the user's timezone (``Settings.timezone``,
    machine-local when unset — same rule as insights and the glance briefing).
    """
    tz = resolve_timezone(settings)
    local_today = now.astimezone(tz).date()
    day_dates = [local_today - timedelta(days=offset) for offset in range(REPORT_DAYS - 1, -1, -1)]
    window_start = datetime.combine(day_dates[0], time.min, tzinfo=tz).astimezone(UTC)
    window_end = datetime.combine(
        local_today + timedelta(days=1), time.min, tzinfo=tz
    ).astimezone(UTC)

    return WeeklyReportOut(
        generated_at=now,
        timezone=str(tz),
        week_start=day_dates[0],
        week_end=local_today,
        report_url=weekly_report_url(settings),
        energy=_energy_section(session, tz, day_dates, window_start, window_end),
        insights=_insights_section(session, window_start, window_end),
        schedule=_schedule_section(session, window_start, window_end),
        alerts=_alerts_section(session, settings, window_start, window_end),
        decisions=_decisions_section(session, settings, window_start, window_end),
    )


# ---------------------------------------------------------------------------
# Inline SVG sparkline (no external libraries — local-first, PLAN §5/§8.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SparklinePoint:
    """One plotted day: viewBox coordinates plus the accessible label."""

    x: float
    y: float
    label: str


@dataclass(frozen=True)
class SparklineView:
    """Everything the template needs to draw the energy sparkline."""

    width: int
    height: int
    segments: list[str]
    """Polyline ``points`` strings — one per run of consecutive data days."""
    points: list[SparklinePoint]
    gridlines: list[float]
    """y coordinates of the score 0/50/100 reference lines."""


def _score_y(score: int) -> float:
    """Map a 0-100 score to a viewBox y (clamped — defensive on bad data)."""
    clamped = min(max(score, 0), 100)
    return round(SPARK_Y_BOTTOM - (clamped / 100) * (SPARK_Y_BOTTOM - SPARK_Y_TOP), 1)


def build_energy_sparkline(days: list[EnergyDayOut]) -> SparklineView:
    """Pure transform of the per-day averages into sparkline geometry.

    Days without data break the line into separate segments (no interpolation
    across gaps — a missing day is honestly missing); an isolated data day
    still gets its dot. The y scale is fixed to 0-100 so week-over-week shapes
    stay comparable.
    """
    points: list[SparklinePoint] = []
    segments: list[str] = []
    run: list[str] = []

    def flush_run() -> None:
        nonlocal run
        if len(run) >= 2:
            segments.append(" ".join(run))
        run = []

    for index, day in enumerate(days):
        if day.avg_score is None:
            flush_run()
            continue
        x = SPARK_X_STEP // 2 + index * SPARK_X_STEP
        y = _score_y(day.avg_score)
        points.append(
            SparklinePoint(x=x, y=y, label=f"{day.date.isoformat()}: avg {day.avg_score}")
        )
        run.append(f"{x},{y}")
    flush_run()

    return SparklineView(
        width=SPARK_X_STEP * len(days),
        height=SPARK_HEIGHT,
        segments=segments,
        points=points,
        gridlines=[_score_y(score) for score in SPARK_GRID_SCORES],
    )


# ---------------------------------------------------------------------------
# Rendering + routes
# ---------------------------------------------------------------------------


def render_weekly_report_html(report: WeeklyReportOut, settings: Settings) -> str:
    """Render the weekly report page from the already-built report model."""
    template = template_environment().get_template("report_weekly.html.j2")
    return template.render(
        report=report,
        spark=build_energy_sparkline(report.energy.days) if report.energy.samples else None,
        json_url=viewer_url(settings, WEEKLY_REPORT_PATH + ".json"),
        format_created=format_created,
    )


@router.get(WEEKLY_REPORT_PATH + ".json")
def get_weekly_report_json(session: SessionDep, request: Request) -> WeeklyReportOut:
    """The weekly report numbers as JSON (same payload the HTML page renders)."""
    settings: Settings = request.app.state.settings
    return build_weekly_report(session, settings, utc_now())


@router.get(WEEKLY_REPORT_PATH, response_class=HTMLResponse)
def get_weekly_report_page(session: SessionDep, request: Request) -> HTMLResponse:
    """Human-facing weekly report: energy trend, insights, adherence, alerts."""
    settings: Settings = request.app.state.settings
    report = build_weekly_report(session, settings, utc_now())
    return HTMLResponse(render_weekly_report_html(report, settings))
