"""Schedule surface: mirrored calendar events + schedule proposals.

- ``GET /v1/schedule/events`` lists ``calendar_event_mirror`` rows overlapping
  a required ``[start, end)`` range (the mirror is written by the calendar
  sync layer, docs/PLAN.md §6 — no write endpoints here).
- ``GET /v1/schedule/proposals`` + accept/decline actions drive the
  propose-then-confirm gate (docs/PLAN.md §2, §6). Proposals are created by
  the planner via the MCP tool ``propose_schedule_blocks``, not over REST.
  Authenticated app responses include a scoped resolution token required by
  the REST actions; Telegram uses the separate one-time reply handle.

Accepting only marks the proposal ``accepted``; the calendar sync layer later
writes the block to the external calendar and advances it to ``pushed``.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from starlette.status import (
    HTTP_403_FORBIDDEN,
    HTTP_409_CONFLICT,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from healthmes.api.common import UTCDateTime
from healthmes.api.errors import APIError, invalid_transition, not_found
from healthmes.api.pagination import Page, PageParamsDep, paginate
from healthmes.schedule_proposals import (
    ScheduleProposalResolutionError,
    resolution_token,
    resolve_schedule_proposal,
)
from healthmes.store import CalendarEventMirror, CalendarSource, ProposalStatus, ScheduleProposal
from healthmes.store.session import SessionDep

router = APIRouter(prefix="/v1/schedule", tags=["schedule"])


class CalendarEventOut(BaseModel):
    """Mirrored calendar event as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    external_id: str
    calendar_source: CalendarSource
    summary: str | None
    start_at: datetime
    end_at: datetime
    is_agent_created: bool
    agent_task_id: uuid.UUID | None


class ProposalOut(BaseModel):
    """Schedule proposal as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    proposed_start: datetime
    proposed_end: datetime
    status: ProposalStatus
    decision_record_id: uuid.UUID | None
    healthmes_kind: str | None
    resolution_token: str | None = None


class ProposalResolutionIn(BaseModel):
    resolution_token: str


def _handle_secret(request: Request) -> str:
    return request.app.state.settings.calendar_adjustment_secret.get_secret_value().strip()


def _proposal_out(proposal: ScheduleProposal, request: Request) -> ProposalOut:
    handle_secret = _handle_secret(request)
    token = (
        resolution_token(proposal, handle_secret)
        if proposal.status is ProposalStatus.PROPOSED and len(handle_secret) >= 32
        else None
    )
    return ProposalOut.model_validate(proposal).model_copy(
        update={"resolution_token": token}
    )


@router.get("/events")
def list_events(
    session: SessionDep,
    page: PageParamsDep,
    start: UTCDateTime,
    end: UTCDateTime,
    calendar_source: CalendarSource | None = None,
) -> Page[CalendarEventOut]:
    """List mirrored events overlapping ``[start, end)``, ordered by start."""
    if end <= start:
        raise APIError(
            HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_range",
            "'end' must be after 'start'",
        )
    stmt = (
        select(CalendarEventMirror)
        .where(CalendarEventMirror.end_at > start, CalendarEventMirror.start_at < end)
        .order_by(CalendarEventMirror.start_at, CalendarEventMirror.end_at)
    )
    if calendar_source is not None:
        stmt = stmt.where(CalendarEventMirror.calendar_source == calendar_source)
    rows, meta = paginate(session, stmt, page)
    return Page(data=[CalendarEventOut.model_validate(row) for row in rows], pagination=meta)


@router.get("/proposals")
def list_proposals(
    session: SessionDep,
    page: PageParamsDep,
    request: Request,
    status_filter: Annotated[ProposalStatus | None, Query(alias="status")] = None,
    task_id: uuid.UUID | None = None,
) -> Page[ProposalOut]:
    """List schedule proposals ordered by proposed start."""
    stmt = select(ScheduleProposal).order_by(
        ScheduleProposal.proposed_start, ScheduleProposal.created_at
    )
    if status_filter is not None:
        stmt = stmt.where(ScheduleProposal.status == status_filter)
    if task_id is not None:
        stmt = stmt.where(ScheduleProposal.task_id == task_id)
    rows, meta = paginate(session, stmt, page)
    return Page(data=[_proposal_out(row, request) for row in rows], pagination=meta)


def _resolve_proposal(
    session: SessionDep,
    request: Request,
    proposal_id: uuid.UUID,
    target: ProposalStatus,
    token: str,
) -> ProposalOut:
    handle_secret = _handle_secret(request)
    if len(handle_secret) < 32:
        raise APIError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "approval_unavailable",
            "Schedule proposal approval is not configured",
        )
    try:
        proposal = resolve_schedule_proposal(
            session,
            proposal_id,
            target,
            token,
            handle_secret,
            allow_reply_handle=False,
            allow_resolution_token=True,
        )
    except ScheduleProposalResolutionError as exc:
        if exc.code == "not_found":
            raise not_found("schedule_proposal", proposal_id) from exc
        if exc.code == "not_proposed":
            current = session.get(ScheduleProposal, proposal_id)
            if current is None:
                raise not_found("schedule_proposal", proposal_id) from exc
            raise invalid_transition(
                "schedule_proposal", current.status.value, target.value
            ) from exc
        if exc.code == "invalid_handle":
            raise APIError(
                HTTP_403_FORBIDDEN,
                "invalid_resolution_token",
                "The schedule proposal resolution token is invalid",
            ) from exc
        if exc.code == "expired":
            raise APIError(
                HTTP_409_CONFLICT,
                "proposal_expired",
                "The schedule proposal has expired",
            ) from exc
        raise
    session.commit()
    session.refresh(proposal)
    return _proposal_out(proposal, request)


@router.post("/proposals/{proposal_id}/accept")
def accept_proposal(
    proposal_id: uuid.UUID,
    body: ProposalResolutionIn,
    session: SessionDep,
    request: Request,
) -> ProposalOut:
    """Accept a pending proposal (``proposed`` -> ``accepted``)."""
    return _resolve_proposal(
        session,
        request,
        proposal_id,
        ProposalStatus.ACCEPTED,
        body.resolution_token,
    )


@router.post("/proposals/{proposal_id}/decline")
def decline_proposal(
    proposal_id: uuid.UUID,
    body: ProposalResolutionIn,
    session: SessionDep,
    request: Request,
) -> ProposalOut:
    """Decline a pending proposal (``proposed`` -> ``declined``)."""
    return _resolve_proposal(
        session,
        request,
        proposal_id,
        ProposalStatus.DECLINED,
        body.resolution_token,
    )
