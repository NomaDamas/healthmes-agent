"""Retention-aware read helpers for the Calendar mirror."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.activity.locking import (
    activity_write_lock,
    lock_activity_write_plane,
)
from healthmes.store.models import CalendarEventMirror

CALENDAR_RETENTION_CLASS = "calendar_mirror"


def retained_calendar_statement(
    session: Session,
    statement: Any,
    *,
    now: datetime | None = None,
) -> Any:
    """Apply the active Calendar retention boundary to one ORM statement."""
    from healthmes.storage.service import retention_cutoff

    cutoff = retention_cutoff(
        session,
        CALENDAR_RETENTION_CLASS,
        now=now,
    )
    if cutoff is None:
        return statement
    return statement.where(CalendarEventMirror.end_at > cutoff)


def retained_calendar_event(
    session: Session,
    event_id: uuid.UUID,
    *,
    now: datetime | None = None,
    lock: bool = False,
) -> CalendarEventMirror | None:
    """Load one Calendar row only while its retained interval is still valid."""
    statement = retained_calendar_statement(
        session,
        select(CalendarEventMirror).where(
            CalendarEventMirror.id == event_id
        ),
        now=now,
    )
    if lock:
        if session.get_bind().dialect.name == "postgresql":
            with activity_write_lock():
                lock_activity_write_plane(session)
                return session.scalar(statement.with_for_update())
        statement = statement.with_for_update()
    return session.scalar(statement)
