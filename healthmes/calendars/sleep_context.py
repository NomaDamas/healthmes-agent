from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from sqlalchemy.orm import Session

from healthmes.calendars.base import HealthmesEventKind, coerce_utc
from healthmes.calendars.sleep_observation import ActualSleepObservation
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
        "source": row.healthmes_source,
        "freshness": "current",
        "earliest_available_work_time": wake.isoformat(),
    }


def actual_sleep_observation_context(
    observation: ActualSleepObservation,
    timezone: dt.tzinfo,
) -> dict[str, object]:
    start = observation.start_at.astimezone(timezone)
    wake = observation.end_at.astimezone(timezone)
    return {
        "status": "ok",
        "local_date": observation.local_date.isoformat(),
        "start": start.isoformat(),
        "wake_time": wake.isoformat(),
        "duration_minutes": observation.duration_minutes,
        "time_in_bed_minutes": observation.time_in_bed_minutes,
        "source": observation.provider,
        "freshness": "current",
        "earliest_available_work_time": wake.isoformat(),
    }


def actual_sleep_violation(
    session: Session,
    start: dt.datetime,
    end: dt.datetime,
    timezone: dt.tzinfo,
) -> str | None:
    local_date = start.astimezone(timezone).date()
    row = _actual_sleep_for_date(session, local_date)
    if row is None:
        return None
    sleep_start = coerce_utc(row.start_at)
    wake = coerce_utc(row.end_at)
    if start < wake and end > sleep_start:
        return (
            f"block starts before actual wake time "
            f"{wake.astimezone(timezone).isoformat()} and overlaps actual sleep"
        )
    if start < wake:
        return (
            f"block starts before actual wake time "
            f"{wake.astimezone(timezone).isoformat()}"
        )
    return None


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
