"""Alert history for the companion apps (issues #10/#11).

``GET /v1/alerts`` lists recent *pushed* trigger events with the same
"unresolved == recent" placeholder semantics as the glance ``alerts`` block
(healthmes/api/briefing.py — the store has no resolution tracking yet; the
domain expert owns refining that policy). Each item carries the §8.5
notification-grammar lines the trigger recorded at fire time (observation
``summary``, ``evidence`` facts, ``proposal``) plus the "why this?"
decision-viewer deep link, resolved through the same persisted trigger
correlation the glance top alert uses — so an app listing alerts never
disagrees with its own widget.

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
from healthmes.store import DecisionRecord, ProposalStatus, ScheduleProposal, Task, TriggerEvent
from healthmes.store.decision_records import (
    decision_record_is_available_at,
)
from healthmes.store.session import SessionDep

__all__ = ["router", "MAX_WINDOW_HOURS"]

router = APIRouter(prefix="/v1/alerts", tags=["alerts"])

# One week of alert history is plenty for the apps' alert screens.
MAX_WINDOW_HOURS = 24 * 7


class DecisionCardOut(BaseModel):
    """Platform-neutral payload for a three-second Yes/No decision."""

    decision_id: uuid.UUID
    proposal_id: uuid.UUID
    kind: str
    severity: str
    title: str
    observation_short: str
    evidence_short: str | None
    proposed_action: str
    before: datetime | None
    after: datetime
    ends_at: datetime
    expires_at: datetime
    decision_url: str | None


class AlertOut(BaseModel):
    """One pushed alert, shaped after the §8.5 notification grammar."""

    id: uuid.UUID
    rule_id: str
    fired_at: datetime
    summary: str  # observation line (falls back to rule_id for legacy rows)
    proposal: str | None  # proposal line
    evidence: dict[str, Any] | None  # evidence facts (client renders the line)
    decision_url: str | None  # "why this?" decision-viewer deep link
    proposal_id: uuid.UUID | None = None  # unique pending proposal for native actions
    decision_card: DecisionCardOut | None = None


def _decision_ids(
    session: Session,
    events: list[TriggerEvent],
    *,
    now: datetime,
) -> dict[uuid.UUID, uuid.UUID]:
    """Return the exact persisted decision correlation for each alert."""
    if not events:
        return {}
    event_ids = {event.id for event in events}
    return {
        trigger_event_id: decision_id
        for trigger_event_id, decision_id in session.execute(
            select(DecisionRecord.trigger_event_id, DecisionRecord.id).where(
                DecisionRecord.trigger_event_id.in_(event_ids),
                decision_record_is_available_at(now),
            )
        )
        if trigger_event_id is not None
    }


def _proposal_ids(
    session: Session, decision_ids: dict[uuid.UUID, uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """Unique pending schedule proposal per alert decision.

    A decision may propose multiple blocks. Native one-tap actions are attached
    only when the alert maps to exactly one pending proposal; otherwise clients
    retain their ambiguity-safe fallback.
    """
    if not decision_ids:
        return {}
    now = utc_now()
    by_decision: dict[uuid.UUID, list[uuid.UUID]] = {}
    for decision_id, proposal_id in session.execute(
        select(ScheduleProposal.decision_record_id, ScheduleProposal.id).where(
            ScheduleProposal.decision_record_id.in_(set(decision_ids.values())),
            ScheduleProposal.status == ProposalStatus.PROPOSED,
            ScheduleProposal.expires_at > now,
        )
    ):
        if decision_id is not None:
            by_decision.setdefault(decision_id, []).append(proposal_id)
    return {
        event_id: proposals[0]
        for event_id, decision_id in decision_ids.items()
        if len(proposals := by_decision.get(decision_id, [])) == 1
    }


def _decision_cards(
    session: Session,
    events: list[TriggerEvent],
    decision_ids: dict[uuid.UUID, uuid.UUID],
    proposal_ids: dict[uuid.UUID, uuid.UUID],
    links: dict[uuid.UUID, str],
) -> dict[uuid.UUID, DecisionCardOut]:
    if not proposal_ids:
        return {}
    proposal_rows = {
        proposal.id: (proposal, task)
        for proposal, task in session.execute(
            select(ScheduleProposal, Task)
            .join(Task, ScheduleProposal.task_id == Task.id)
            .where(ScheduleProposal.id.in_(set(proposal_ids.values())))
        )
    }
    cards: dict[uuid.UUID, DecisionCardOut] = {}
    for event in events:
        proposal_id = proposal_ids.get(event.id)
        decision_id = decision_ids.get(event.id)
        row = proposal_rows.get(proposal_id) if proposal_id is not None else None
        if row is None or decision_id is None:
            continue
        proposal, task = row
        payload = event.payload or {}
        evidence = payload.get("evidence")
        evidence_short = None
        if isinstance(evidence, dict) and evidence:
            evidence_short = " · ".join(
                f"{key} {value}" for key, value in sorted(evidence.items())
            )
        starts_at = ensure_utc(proposal.proposed_start)
        ends_at = ensure_utc(proposal.proposed_end)
        expires_at = proposal.expires_at
        if expires_at is None:
            continue
        cards[event.id] = DecisionCardOut(
            decision_id=decision_id,
            proposal_id=proposal.id,
            kind=proposal.healthmes_kind or "schedule_change",
            severity=str(payload.get("severity") or "normal"),
            title=task.title,
            observation_short=str(payload.get("summary") or event.rule_id),
            evidence_short=evidence_short,
            proposed_action=str(payload.get("proposal") or f"{task.title} 일정을 조정합니다."),
            before=None,
            after=starts_at,
            ends_at=ends_at,
            expires_at=ensure_utc(expires_at),
            decision_url=links.get(event.id),
        )
    return cards


@router.get("")
def list_alerts(
    request: Request,
    session: SessionDep,
    page: PageParamsDep,
    hours: Annotated[int, Query(ge=1, le=MAX_WINDOW_HOURS)] = ALERT_RECENT_HOURS,
) -> Page[AlertOut]:
    """Recent pushed alerts, newest first (glance ``alerts`` block semantics)."""
    settings: Settings = request.app.state.settings
    now = utc_now()
    cutoff = now - timedelta(hours=hours)
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
    decision_ids = _decision_ids(session, window, now=now)
    links = {
        event_id: decision_viewer_url(settings, decision_id)
        for event_id, decision_id in decision_ids.items()
    }
    proposal_ids = _proposal_ids(session, decision_ids)
    decision_cards = _decision_cards(
        session,
        window,
        decision_ids,
        proposal_ids,
        links,
    )
    data = []
    for event in window:
        payload: dict[str, Any] = event.payload or {}
        summary = payload.get("message") or payload.get("summary")
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
                proposal_id=proposal_ids.get(event.id),
                decision_card=decision_cards.get(event.id),
            )
        )
    meta = PageMeta(
        total_count=len(events),
        limit=page.limit,
        offset=page.offset,
        has_more=page.offset + len(window) < len(events),
    )
    return Page(data=data, pagination=meta)
