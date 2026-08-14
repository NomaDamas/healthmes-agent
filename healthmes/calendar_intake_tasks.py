"""Lifecycle helpers for tasks derived from Calendar intake rows."""

from __future__ import annotations

from sqlalchemy.orm import Session

from healthmes.store.models import CalendarEventMirror, Task


def retire_calendar_intake_task(
    session: Session,
    row: CalendarEventMirror,
) -> Task | None:
    """Cancel the derived task without mutating the mirror's CAS identity."""

    if row.intake_task_id is None:
        return None
    task = session.get(Task, row.intake_task_id)
    if task is not None:
        task.status = "cancelled"
    return task
