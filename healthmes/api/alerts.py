"""Alert history for the companion apps (issues #10/#11).

``GET /v1/alerts`` lists recent *pushed* trigger events with the same
"unresolved == recent" placeholder semantics as the glance ``alerts`` block
(healthmes/api/briefing.py — the store has no resolution tracking yet; the
domain expert owns refining that policy). Each item carries the §8.5
notification-grammar lines the trigger recorded at fire time (observation
``summary``, ``evidence`` facts, ``proposal``) plus the "why this?"
decision-viewer deep link, resolved with the exact heuristic the glance top
alert uses — so an app listing alerts never disagrees with its own widget.

The window (``hours``, default = glance's ALERT_RECENT_HOURS) and the SQL
filter mirror ``briefing._alerts_block``, including the Python-side re-check
of ``fired_at`` (sqlite reads are naive). Pagination happens in Python over
the verified rows: pushed alerts are budget-capped per day
(Settings.alert_daily_budget), so a full week's window stays tiny.
"""

import uuid
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.api.briefing import ALERT_RECENT_HOURS, decision_viewer_url
from healthmes.api.common import ensure_utc, utc_now
from healthmes.api.pagination import Page, PageMeta, PageParamsDep
from healthmes.config import Settings
from healthmes.store import DecisionKind, DecisionRecord, TriggerEvent
from healthmes.store.session import SessionDep

__all__ = ["router", "MAX_WINDOW_HOURS"]

router = APIRouter(prefix="/v1/alerts", tags=["alerts"])

# One week of alert history is plenty for the apps' alert screens.
MAX_WINDOW_HOURS = 24 * 7


class AlertOut(BaseModel):
    """One pushed alert, shaped after the §8.5 notification grammar."""

    id: uuid.UUID
    rule_id: str
    fired_at: datetime
    summary: str  # observation line (falls back to rule_id for legacy rows)
    proposal: str | None  # proposal line
    evidence: dict[str, Any] | None  # evidence facts (client renders the line)
    decision_url: str | None  # "why this?" decision-viewer deep link


def _decision_links(
    session: Session, settings: Settings, events: list[TriggerEvent]
) -> dict[uuid.UUID, str]:
    """Viewer link per event: earliest alert-kind decision at/after its fire.

    Exactly the glance top-alert heuristic (there is no FK yet —
    briefing._alerts_block documents the placeholder policy), batched so one
    page of alerts costs one decision query instead of N.
    """
    if not events:
        return {}
    earliest_fire = min(ensure_utc(event.fired_at) for event in events)
    decisions = [
        (ensure_utc(record.created_at), record.id)
        for record in session.scalars(
            select(DecisionRecord)
            .where(
                DecisionRecord.kind == DecisionKind.ALERT,
                DecisionRecord.created_at >= earliest_fire,
            )
            .order_by(DecisionRecord.created_at.asc(), DecisionRecord.id.asc())
        ).all()
    ]
    links: dict[uuid.UUID, str] = {}
    for event in events:
        fired = ensure_utc(event.fired_at)
        for created_at, decision_id in decisions:
            if created_at >= fired:
                links[event.id] = decision_viewer_url(settings, decision_id)
                break
    return links


@router.get("")
def list_alerts(
    request: Request,
    session: SessionDep,
    page: PageParamsDep,
    hours: Annotated[int, Query(ge=1, le=MAX_WINDOW_HOURS)] = ALERT_RECENT_HOURS,
) -> Page[AlertOut]:
    """Recent pushed alerts, newest first (glance ``alerts`` block semantics)."""
    settings: Settings = request.app.state.settings
    cutoff = utc_now() - timedelta(hours=hours)
    events = [
        event
        for event in session.scalars(
            select(TriggerEvent)
            .where(TriggerEvent.alert_sent.is_(True), TriggerEvent.fired_at >= cutoff)
            .order_by(TriggerEvent.fired_at.desc(), TriggerEvent.created_at.desc())
        ).all()
        if ensure_utc(event.fired_at) >= cutoff  # sqlite reads are naive; re-verify
    ]

    window = events[page.offset : page.offset + page.limit]
    links = _decision_links(session, settings, window)
    data = []
    for event in window:
        payload: dict[str, Any] = event.payload or {}
        summary = payload.get("summary")
        evidence = payload.get("evidence")
        proposal = payload.get("proposal")
        data.append(
            AlertOut(
                id=event.id,
                rule_id=event.rule_id,
                fired_at=ensure_utc(event.fired_at),
                # Same honest fallback as the glance top alert: the rule id
                # when a legacy row carries no payload.
                summary=str(summary) if summary else event.rule_id,
                proposal=str(proposal) if proposal is not None else None,
                evidence=evidence if isinstance(evidence, dict) else None,
                decision_url=links.get(event.id),
            )
        )
    meta = PageMeta(
        total_count=len(events),
        limit=page.limit,
        offset=page.offset,
        has_more=page.offset + len(window) < len(events),
    )
    return Page(data=data, pagination=meta)
