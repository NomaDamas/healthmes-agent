from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from healthmes.calendars.base import (
    CalendarBackend,
    CalendarConflictError,
    CalendarEventIdentity,
    EventNotFoundError,
    ExternalEvent,
    HealthmesEventKind,
    OwnershipError,
    calendar_identity_external_id,
    coerce_utc,
    ensure_utc,
    parse_calendar_identity,
)
from healthmes.calendars.sleep_observation import ActualSleepObservation
from healthmes.store.enums import ProposalStatus
from healthmes.store.models import CalendarEventMirror, ScheduleProposal


@dataclass(frozen=True, slots=True)
class PlannedSleepReplacement:
    deleted_external_ids: tuple[str, ...]
    cleanup_pending: int


def proposal_for_planner_event(
    session: Session,
    identity: CalendarEventIdentity,
    *,
    agent_task_id: UUID | None,
    start_at: datetime | None,
    end_at: datetime | None,
) -> ScheduleProposal | None:
    if (
        identity.source != "planner"
        or identity.kind
        not in {
            HealthmesEventKind.PLANNED_SLEEP,
            HealthmesEventKind.SCHEDULE_BLOCK,
        }
        or not identity.source_key.startswith("proposal:")
        or start_at is None
        or end_at is None
    ):
        return None
    try:
        proposal_id = UUID(identity.source_key.removeprefix("proposal:"))
    except ValueError:
        return None
    if identity.source_key != f"proposal:{proposal_id}":
        return None
    proposal = session.get(ScheduleProposal, proposal_id)
    if (
        proposal is None
        or proposal.status
        not in {
            ProposalStatus.ACCEPTED,
            ProposalStatus.PUSHED,
        }
        or proposal.task_id != agent_task_id
    ):
        return None
    expected_kind = (
        HealthmesEventKind.PLANNED_SLEEP
        if proposal.healthmes_kind == HealthmesEventKind.PLANNED_SLEEP.value
        else HealthmesEventKind.SCHEDULE_BLOCK
    )
    if (
        identity.kind is not expected_kind
        or coerce_utc(proposal.proposed_start) != coerce_utc(start_at)
        or coerce_utc(proposal.proposed_end) != coerce_utc(end_at)
    ):
        return None
    return proposal


def planned_sleep_identity_from_mirror(
    row: CalendarEventMirror,
) -> CalendarEventIdentity | None:
    identity = parse_calendar_identity(
        row.healthmes_kind,
        row.healthmes_source,
        row.healthmes_source_key,
    )
    if (
        identity is None
        or identity.kind is not HealthmesEventKind.PLANNED_SLEEP
        or identity.source != "planner"
        or not identity.source_key.startswith("proposal:")
    ):
        return None
    return identity


def read_owned_planned_sleep(
    backend: CalendarBackend,
    row: CalendarEventMirror,
    observation: ActualSleepObservation,
) -> ExternalEvent | None:
    identity = planned_sleep_identity_from_mirror(row)
    if (
        identity is None
        or not row.is_agent_created
        or row.calendar_source is not backend.source
        or row.external_id
        != calendar_identity_external_id(backend.source, identity)
    ):
        raise OwnershipError(
            f"{backend.source.value} event {row.external_id!r} is not an exact "
            "proposal-owned planned_sleep event"
        )
    try:
        event = backend.read_event(row.external_id)
    except EventNotFoundError:
        return None
    if (
        not event.is_agent_created
        or event.identity != identity
        or event.external_id
        != calendar_identity_external_id(backend.source, identity)
    ):
        raise OwnershipError(
            f"{backend.source.value} event {row.external_id!r} failed remote "
            "proposal identity validation"
        )
    if row.etag is not None and event.etag != row.etag:
        raise CalendarConflictError("remote planned_sleep changed after sync")
    if (
        event.start_at is None
        or event.end_at is None
        or event.start_at >= ensure_utc(observation.end_at)
        or event.end_at <= ensure_utc(observation.start_at)
    ):
        raise CalendarConflictError(
            "remote planned_sleep no longer overlaps actual sleep"
        )
    return event


def delete_replaced_planned_sleep(
    session: Session,
    backend: CalendarBackend,
    observation: ActualSleepObservation,
) -> PlannedSleepReplacement:
    planned = session.scalars(
        sa.select(CalendarEventMirror).where(
            CalendarEventMirror.calendar_source == backend.source,
            CalendarEventMirror.is_agent_created.is_(True),
            CalendarEventMirror.healthmes_kind
            == HealthmesEventKind.PLANNED_SLEEP.value,
            CalendarEventMirror.start_at < ensure_utc(observation.end_at),
            CalendarEventMirror.end_at > ensure_utc(observation.start_at),
        )
    ).all()
    deleted: list[str] = []
    cleanup_pending = 0
    for row in planned:
        try:
            remote = read_owned_planned_sleep(backend, row, observation)
        except (CalendarConflictError, OwnershipError):
            cleanup_pending += 1
            continue
        if remote is not None:
            try:
                backend.delete_event(
                    row.external_id,
                    expected_kind=HealthmesEventKind.PLANNED_SLEEP,
                    expected_etag=remote.etag,
                )
            except CalendarConflictError:
                cleanup_pending += 1
                continue
            except EventNotFoundError:
                pass
        session.delete(row)
        session.commit()
        deleted.append(row.external_id)
    return PlannedSleepReplacement(tuple(deleted), cleanup_pending)
