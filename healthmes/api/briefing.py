"""Glanceable briefing endpoint for the companion-app surfaces (issue #7).

``GET /v1/briefing/glance`` is the single compact payload every widget,
lock-screen surface and watch complication reads. It is deliberately
*plumbing*: deterministic values straight from the store, shaped after the
PLAN §8.5 notification grammar (observation → evidence → link) — the final
watch/widget UX is reserved for the healthcare domain expert; clients render
placeholders against this stable contract.

Response shape (all timestamps ISO-8601 aware UTC, ``Z`` suffix)::

    {
      "generated_at": "2026-07-09T14:23:00Z",
      "timezone": "Asia/Seoul",                    // user tz (IANA when configured)
      "energy": {
        "score": 58,                               // int|null — latest persisted window
        "confidence": "high",                      // "high"|"medium"|"low" (freshness)
        "curve_24h": [ {"hour": 0, "score": null}, ... 24 entries ... ]
      },
      "next_blocks": [                             // <= 3, soonest first
        {"start": ..., "end": ..., "title": "Deep work"|null,
         "energy_demand": "low"|"med"|"high"|null,
         "source": "calendar"|"proposal"}
      ],
      "alerts": {
        "unresolved_count": 2,
        "top": {"rule_id": ..., "summary": ..., "decision_url": ...|null} | null
      },
      "latest_decision": {"id": ..., "url": ...} | null
    }

Data sources and honesty rules:

- **energy** comes only from *persisted* ``cognitive_energy_estimate`` windows
  of today (the user's local day; hour = local wall-clock hour, mapped to the
  engine's UTC-hour windows). Missing hours are ``null`` — never computed on
  demand here (no open-wearables round trips on a widget poll) and never
  fabricated. ``score`` is the latest non-null hour at or before now;
  ``confidence`` is a freshness ladder over that window's age (current hour →
  high, ≤3 h stale → medium, else low — including "no data at all").
- **next_blocks** merges mirrored calendar events (source ``calendar``) with
  *accepted-but-not-yet-pushed* schedule proposals (source ``proposal``;
  pushed proposals already appear via the mirror, so including them twice
  would double-count). ``energy_demand`` comes from the linked task when one
  exists.
- **alerts** are recent pushed trigger events (``alert_sent`` within
  :data:`ALERT_RECENT_HOURS`). The store has no resolution tracking yet, so
  "unresolved" == "recent" — a documented placeholder policy for the domain
  expert to refine. ``top.decision_url`` links the earliest alert-kind
  decision recorded at/after the fire (the agent records its reasoning right
  after pushing; there is no FK yet), else ``null``.
- **decision URLs** come from :func:`healthmes.api.auth.viewer_url` — the one
  construction point shared with the MCP ``record_decision`` tool and the
  weekly report: ``{public_base_url}/decisions/{id}`` plus the *derived
  read-only* ``?token=`` credential when an API token is configured, so links
  stay browser-tappable without ever embedding the full-access token.

Budget friendliness (iOS widget timelines / Android WorkManager poll this):
responses carry ``Cache-Control: private, max-age=300`` and a strong ``ETag``
(sha-256 of the payload *excluding* ``generated_at`` — the timestamp changes
every request and would otherwise defeat revalidation); ``If-None-Match``
answers ``304 Not Modified`` with the same caching headers and no body.

Auth: the shared bearer middleware (healthmes/api/auth.py) gates this route
like the rest of ``/v1`` — loopback-open when no token is configured.
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.api.auth import viewer_url
from healthmes.api.common import ensure_utc, utc_now
from healthmes.config import Settings, resolve_timezone
from healthmes.store import (
    CalendarEventMirror,
    CognitiveEnergyEstimate,
    DecisionKind,
    DecisionRecord,
    EnergyDemand,
    ProposalStatus,
    ScheduleProposal,
    Task,
    TriggerEvent,
)
from healthmes.store.session import SessionDep

__all__ = ["router", "ALERT_RECENT_HOURS", "CACHE_MAX_AGE_SECONDS", "decision_viewer_url"]

router = APIRouter(prefix="/v1/briefing", tags=["briefing"])

# Widgets poll on coarse OS-scheduled budgets; 5 minutes of freshness is
# plenty for hourly energy windows and calendar blocks.
CACHE_MAX_AGE_SECONDS = 300
CACHE_CONTROL_VALUE = f"private, max-age={CACHE_MAX_AGE_SECONDS}"

# "Unresolved" placeholder policy: a pushed alert stays glanceable this long.
# The store has no resolution tracking yet (see module docstring).
ALERT_RECENT_HOURS = 24

# Freshness ladder for energy.confidence (ages measured against the window
# start of the latest persisted score).
CONFIDENCE_HIGH_MAX_AGE = timedelta(hours=1)  # the window covering "now"
CONFIDENCE_MEDIUM_MAX_AGE = timedelta(hours=3)

MAX_NEXT_BLOCKS = 3

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"


# ---------------------------------------------------------------------------
# Response models (the contract the companion apps build against)
# ---------------------------------------------------------------------------


class EnergyCurvePointOut(BaseModel):
    """One local wall-clock hour of today's energy curve."""

    hour: int  # 0-23, hour of the user's local day
    score: int | None  # persisted window score; null = honestly missing


class GlanceEnergyOut(BaseModel):
    """Today's persisted cognitive-energy picture."""

    score: int | None
    confidence: Literal["high", "medium", "low"]
    curve_24h: list[EnergyCurvePointOut]


class GlanceBlockOut(BaseModel):
    """One upcoming (or ongoing) schedule block."""

    start: datetime
    end: datetime
    title: str | None
    energy_demand: EnergyDemand | None
    source: Literal["calendar", "proposal"]


class GlanceAlertOut(BaseModel):
    """The most recent unresolved alert, notification-grammar shaped."""

    rule_id: str
    summary: str
    decision_url: str | None


class GlanceAlertsOut(BaseModel):
    """Unresolved-alert digest."""

    unresolved_count: int
    top: GlanceAlertOut | None


class GlanceDecisionOut(BaseModel):
    """Pointer to a decision-viewer page."""

    id: uuid.UUID
    url: str


class GlanceOut(BaseModel):
    """Response of ``GET /v1/briefing/glance``."""

    generated_at: datetime
    timezone: str
    energy: GlanceEnergyOut
    next_blocks: list[GlanceBlockOut]
    alerts: GlanceAlertsOut
    latest_decision: GlanceDecisionOut | None


# ---------------------------------------------------------------------------
# Decision viewer URL (delegates to the shared healthmes.api.auth.viewer_url)
# ---------------------------------------------------------------------------


def decision_viewer_url(settings: Settings, decision_id: uuid.UUID | str) -> str:
    """Browser-tappable viewer link for one decision record.

    Thin wrapper over :func:`healthmes.api.auth.viewer_url` — the single
    construction point shared with the ``record_decision`` MCP tool and the
    weekly report, so the derived read-only credential is embedded (never
    re-derived) when an API token is configured.
    """
    return viewer_url(settings, f"/decisions/{decision_id}")


# ---------------------------------------------------------------------------
# Block builders (pure store reads; every datetime normalised to aware UTC)
# ---------------------------------------------------------------------------


def _floor_hour(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)


def _energy_block(session: Session, tz: tzinfo, now: datetime) -> GlanceEnergyOut:
    """Today's curve from persisted windows only (null-fill missing hours).

    Hour ``h`` of the local day maps to the persisted UTC-hour window
    containing that local hour's start (exact for whole-hour offsets; for
    fractional-offset zones the floored containing window is used). DST days
    follow PEP 495 wall-clock arithmetic — the curve always has 24 entries.
    """
    local_day = now.astimezone(tz).date()
    local_midnight = datetime.combine(local_day, time.min, tzinfo=tz)
    hour_keys = [
        _floor_hour((local_midnight + timedelta(hours=hour)).astimezone(UTC))
        for hour in range(24)
    ]

    rows = session.scalars(
        select(CognitiveEnergyEstimate).where(
            CognitiveEnergyEstimate.window_start >= hour_keys[0],
            CognitiveEnergyEstimate.window_start < hour_keys[-1] + timedelta(hours=1),
        )
    ).all()
    scores: dict[datetime, int] = {
        ensure_utc(row.window_start): row.score for row in rows
    }

    curve = [
        EnergyCurvePointOut(hour=hour, score=scores.get(hour_keys[hour]))
        for hour in range(24)
    ]

    current_score: int | None = None
    current_key: datetime | None = None
    for hour in range(23, -1, -1):
        key = hour_keys[hour]
        if key <= now and curve[hour].score is not None:
            current_score = curve[hour].score
            current_key = key
            break

    if current_score is None or current_key is None:
        confidence = CONFIDENCE_LOW
    else:
        age = now - current_key
        if age < CONFIDENCE_HIGH_MAX_AGE:
            confidence = CONFIDENCE_HIGH
        elif age <= CONFIDENCE_MEDIUM_MAX_AGE:
            confidence = CONFIDENCE_MEDIUM
        else:
            confidence = CONFIDENCE_LOW

    return GlanceEnergyOut(score=current_score, confidence=confidence, curve_24h=curve)


def _next_blocks(session: Session, now: datetime) -> list[GlanceBlockOut]:
    """Up to 3 ongoing/upcoming blocks: mirror events + accepted proposals.

    Accepted proposals are the ones *not yet written* to the external calendar
    (once pushed they surface through the mirror), so the merge never shows
    the same block twice.
    """
    events = session.scalars(
        select(CalendarEventMirror)
        .where(CalendarEventMirror.end_at > now)
        .order_by(CalendarEventMirror.start_at, CalendarEventMirror.end_at)
        .limit(MAX_NEXT_BLOCKS)
    ).all()

    task_ids = {event.agent_task_id for event in events if event.agent_task_id is not None}
    tasks_by_id: dict[uuid.UUID, Task] = {}
    if task_ids:
        tasks_by_id = {
            task.id: task
            for task in session.scalars(select(Task).where(Task.id.in_(task_ids))).all()
        }

    blocks: list[GlanceBlockOut] = [
        GlanceBlockOut(
            start=ensure_utc(event.start_at),
            end=ensure_utc(event.end_at),
            title=event.summary,
            energy_demand=(
                tasks_by_id[event.agent_task_id].energy_demand
                if event.agent_task_id in tasks_by_id
                else None
            ),
            source="calendar",
        )
        for event in events
    ]

    proposal_rows = session.execute(
        select(ScheduleProposal, Task)
        .join(Task, ScheduleProposal.task_id == Task.id)
        .where(
            ScheduleProposal.status == ProposalStatus.ACCEPTED,
            ScheduleProposal.proposed_end > now,
        )
        .order_by(ScheduleProposal.proposed_start, ScheduleProposal.proposed_end)
        .limit(MAX_NEXT_BLOCKS)
    ).all()
    blocks.extend(
        GlanceBlockOut(
            start=ensure_utc(proposal.proposed_start),
            end=ensure_utc(proposal.proposed_end),
            title=task.title,
            energy_demand=task.energy_demand,
            source="proposal",
        )
        for proposal, task in proposal_rows
    )

    blocks.sort(key=lambda block: (block.start, block.end, block.source))
    return blocks[:MAX_NEXT_BLOCKS]


def _alerts_block(session: Session, settings: Settings, now: datetime) -> GlanceAlertsOut:
    """Recent pushed alerts, newest first (recency stands in for resolution)."""
    cutoff = now - timedelta(hours=ALERT_RECENT_HOURS)
    events = [
        event
        for event in session.scalars(
            select(TriggerEvent)
            .where(TriggerEvent.alert_sent.is_(True), TriggerEvent.fired_at >= cutoff)
            .order_by(TriggerEvent.fired_at.desc(), TriggerEvent.created_at.desc())
        ).all()
        if ensure_utc(event.fired_at) >= cutoff  # sqlite reads are naive; re-verify
    ]
    if not events:
        return GlanceAlertsOut(unresolved_count=0, top=None)

    top = events[0]
    payload: dict[str, Any] = top.payload or {}
    summary = payload.get("summary")
    decision = session.scalars(
        select(DecisionRecord)
        .where(
            DecisionRecord.kind == DecisionKind.ALERT,
            DecisionRecord.created_at >= ensure_utc(top.fired_at),
        )
        .order_by(DecisionRecord.created_at.asc(), DecisionRecord.id.asc())
        .limit(1)
    ).first()
    return GlanceAlertsOut(
        unresolved_count=len(events),
        top=GlanceAlertOut(
            rule_id=top.rule_id,
            # The observation line of the notification grammar; the rule id is
            # the honest fallback when a legacy row has no payload.
            summary=str(summary) if summary else top.rule_id,
            decision_url=(
                decision_viewer_url(settings, decision.id) if decision is not None else None
            ),
        ),
    )


def _latest_decision(session: Session, settings: Settings) -> GlanceDecisionOut | None:
    """Newest decision record (same ordering as the /v1/decisions list)."""
    record = session.scalars(
        select(DecisionRecord)
        .order_by(DecisionRecord.created_at.desc(), DecisionRecord.id.desc())
        .limit(1)
    ).first()
    if record is None:
        return None
    return GlanceDecisionOut(id=record.id, url=decision_viewer_url(settings, record.id))


# ---------------------------------------------------------------------------
# Conditional-GET plumbing
# ---------------------------------------------------------------------------


def _compute_etag(payload: dict[str, Any]) -> str:
    """Strong ETag over the payload *content* (``generated_at`` excluded).

    The timestamp changes on every request; hashing it would make
    ``If-None-Match`` never hit and burn the widgets' polling budget — the
    ETag identifies the underlying data instead.
    """
    basis = {key: value for key, value in payload.items() if key != "generated_at"}
    digest = hashlib.sha256(
        json.dumps(basis, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f'"{digest}"'


def _if_none_match_hit(header_value: str | None, etag: str) -> bool:
    """RFC 9110 If-None-Match: any listed entity-tag (or ``*``) matches."""
    if not header_value:
        return False
    for candidate in header_value.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get("/glance", response_model=GlanceOut)
def get_glance_briefing(request: Request, session: SessionDep) -> Response:
    """The compact briefing payload for widgets/complications (docs above)."""
    settings: Settings = request.app.state.settings
    tz = resolve_timezone(settings)
    now = utc_now()

    glance = GlanceOut(
        generated_at=now,
        timezone=str(tz),
        energy=_energy_block(session, tz, now),
        next_blocks=_next_blocks(session, now),
        alerts=_alerts_block(session, settings, now),
        latest_decision=_latest_decision(session, settings),
    )
    payload = glance.model_dump(mode="json")
    etag = _compute_etag(payload)
    headers = {"Cache-Control": CACHE_CONTROL_VALUE, "ETag": etag}

    if _if_none_match_hit(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(content=payload, headers=headers)
