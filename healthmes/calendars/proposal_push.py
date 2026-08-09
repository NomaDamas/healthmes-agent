import logging
from collections.abc import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.calendars.base import (
    CalendarEventIdentity,
    EventDraft,
    HealthmesEventKind,
    coerce_utc,
    parse_event_kind,
)
from healthmes.calendars.sync import CalendarMirrorService
from healthmes.store.enums import CalendarSource, ProposalStatus
from healthmes.store.models import CalendarEventMirror, ScheduleProposal, Task

logger = logging.getLogger(__name__)


def _accepted_proposals(session: Session) -> Iterator[tuple[ScheduleProposal, Task]]:
    rows = session.execute(
        select(ScheduleProposal, Task)
        .join(Task, ScheduleProposal.task_id == Task.id)
        .where(ScheduleProposal.status == ProposalStatus.ACCEPTED)
        .order_by(ScheduleProposal.proposed_start)
    )
    yield from ((proposal, task) for proposal, task in rows)


def _existing_agent_block(
    session: Session,
    source: CalendarSource,
    task_id: object,
    proposal: ScheduleProposal,
) -> CalendarEventMirror | None:
    candidates = (
        session.execute(
            select(CalendarEventMirror).where(
                CalendarEventMirror.calendar_source == source,
                CalendarEventMirror.agent_task_id == task_id,
                CalendarEventMirror.is_agent_created.is_(True),
            )
        )
        .scalars()
        .all()
    )
    start = coerce_utc(proposal.proposed_start)
    end = coerce_utc(proposal.proposed_end)
    for row in candidates:
        if coerce_utc(row.start_at) == start and coerce_utc(row.end_at) == end:
            return row
    return None


def push_accepted_proposals(
    service: CalendarMirrorService, session: Session, source: CalendarSource
) -> int:
    pushed = 0
    for proposal, task in list(_accepted_proposals(session)):
        row = _existing_agent_block(session, source, task.id, proposal)
        if row is None:
            identity = CalendarEventIdentity(
                kind=parse_event_kind(proposal.healthmes_kind)
                or HealthmesEventKind.TASK_BLOCK,
                source="planner",
                source_key=f"proposal:{proposal.id}",
            )
            draft = EventDraft(
                summary=task.title,
                start_at=coerce_utc(proposal.proposed_start),
                end_at=coerce_utc(proposal.proposed_end),
                agent_task_id=task.id,
                identity=identity,
            )
            try:
                row = service.create_agent_event(source, draft)
            except Exception:
                logger.exception(
                    "Pushing proposal %s (%s) to %s failed; retrying next poll.",
                    proposal.id,
                    task.title,
                    source.value,
                )
                continue
        else:
            logger.info(
                "Proposal %s already has agent block %s on %s; finishing the "
                "interrupted status advance instead of re-creating it.",
                proposal.id,
                row.external_id,
                source.value,
            )
        proposal.status = ProposalStatus.PUSHED
        task.status = "scheduled"
        session.commit()
        pushed += 1
        logger.info(
            "Proposal %s pushed to %s as event %s (%s).",
            proposal.id,
            source.value,
            row.external_id,
            task.title,
        )
    return pushed
