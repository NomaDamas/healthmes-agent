"""Shared retention cleanup for Calendar mirrors and derived tasks."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.calendar_intake_tasks import retire_calendar_intake_task
from healthmes.store.models import CalendarEventMirror, Task


def purge_expired_calendar_mirrors(
    session: Session,
    *,
    cutoff: datetime | None,
) -> tuple[Task, ...]:
    """Delete expired mirrors and retire any task derived from them."""
    if cutoff is None:
        return ()
    rows = session.scalars(
        select(CalendarEventMirror)
        .where(CalendarEventMirror.end_at <= cutoff)
        .with_for_update()
    ).all()
    retired: list[Task] = []
    for row in rows:
        task = retire_calendar_intake_task(session, row)
        if task is not None:
            retired.append(task)
        session.delete(row)
    return tuple(retired)
