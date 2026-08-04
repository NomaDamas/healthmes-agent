from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from sqlalchemy.orm import Session

from healthmes.calendars.base import HealthmesEventKind, coerce_utc
from healthmes.store.models import CalendarEventMirror


def actual_sleep_context(
    session: Session,
    local_date: dt.date,
    timezone: dt.tzinfo,
) -> dict[str, object]:
    row = _actual_sleep_for_date(session, local_date)
    if row is None:
        return {
            "status": "insufficient_data",
            "reason": "no_actual_sleep_observation",
        }
    start = coerce_utc(row.start_at).astimezone(timezone)
    wake = coerce_utc(row.end_at).astimezone(timezone)
    return {
        "status": "ok",
        "local_date": local_date.isoformat(),
        "start": start.isoformat(),
        "wake_time": wake.isoformat(),
        "duration_minutes": row.sleep_duration_minutes,
        "time_in_bed_minutes": row.sleep_time_in_bed_minutes,
        "source": row.sleep_provider or row.healthmes_source,
        "freshness": "current",
        "earliest_available_work_time": wake.isoformat(),
    }


def actual_sleep_violation(
    session: Session,
    start: dt.datetime,
    end: dt.datetime,
    timezone: dt.tzinfo,
) -> str | None:
    start_utc = coerce_utc(start)
    end_utc = coerce_utc(end)
    row = session.scalar(
        sa.select(CalendarEventMirror)
        .where(
            CalendarEventMirror.is_agent_created.is_(True),
            CalendarEventMirror.healthmes_kind == HealthmesEventKind.ACTUAL_SLEEP.value,
            CalendarEventMirror.start_at < end_utc,
            CalendarEventMirror.end_at > start_utc,
        )
        .order_by(CalendarEventMirror.end_at.desc())
        .limit(1)
    )
    if row is None:
        return None
    wake = coerce_utc(row.end_at)
    return (
        f"block starts before actual wake time "
        f"{wake.astimezone(timezone).isoformat()} and overlaps actual sleep"
    )


def _actual_sleep_for_date(
    session: Session,
    local_date: dt.date,
) -> CalendarEventMirror | None:
    return session.scalar(
        sa.select(CalendarEventMirror)
        .where(
            CalendarEventMirror.is_agent_created.is_(True),
            CalendarEventMirror.healthmes_kind == HealthmesEventKind.ACTUAL_SLEEP.value,
            CalendarEventMirror.sleep_local_date == local_date,
        )
        .order_by(
            CalendarEventMirror.sleep_duration_minutes.desc(),
            CalendarEventMirror.end_at.desc(),
        )
        .limit(1)
    )
