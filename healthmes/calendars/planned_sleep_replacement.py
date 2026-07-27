from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session

from healthmes.calendars.base import (
    CalendarBackend,
    CalendarConflictError,
    EventNotFoundError,
    HealthmesEventKind,
    ensure_utc,
)
from healthmes.calendars.sleep_observation import ActualSleepObservation
from healthmes.store.models import CalendarEventMirror


@dataclass(frozen=True, slots=True)
class PlannedSleepReplacement:
    deleted_external_ids: tuple[str, ...]
    cleanup_pending: int


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
            backend.delete_event(
                row.external_id,
                expected_kind=HealthmesEventKind.PLANNED_SLEEP,
                expected_etag=row.etag,
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
