from __future__ import annotations

import datetime as dt
from collections.abc import Mapping

import sqlalchemy as sa
from sqlalchemy.orm import Session

from healthmes.calendars.base import HealthmesEventKind, coerce_utc
from healthmes.calendars.repository import retained_calendar_statement
from healthmes.calendars.sleep_observation import ActualSleepObservation
from healthmes.store.enums import CalendarSource
from healthmes.store.models import CalendarEventMirror


def actual_sleep_context(
    session: Session,
    local_date: dt.date,
    timezone: dt.tzinfo,
    *,
    account_generations: Mapping[CalendarSource, str] | None = None,
) -> dict[str, object]:
    context, _ = actual_sleep_context_with_source_ref(
        session,
        local_date,
        timezone,
        account_generations=account_generations,
    )
    return context


def actual_sleep_context_with_source_ref(
    session: Session,
    local_date: dt.date,
    timezone: dt.tzinfo,
    *,
    account_generations: Mapping[CalendarSource, str] | None = None,
) -> tuple[dict[str, object], dict[str, object] | None]:
    row = _actual_sleep_for_date(
        session,
        local_date,
        account_generations=account_generations,
    )
    if row is None:
        return (
            {
                "status": "insufficient_data",
                "reason": "no_actual_sleep_observation",
            },
            None,
        )
    start = coerce_utc(row.start_at).astimezone(timezone)
    wake = coerce_utc(row.end_at).astimezone(timezone)
    source = row.sleep_provider or row.healthmes_source or "unknown"
    return (
        {
            "status": "ok",
            "local_date": local_date.isoformat(),
            "start": start.isoformat(),
            "wake_time": wake.isoformat(),
            "duration_minutes": row.sleep_duration_minutes,
            "time_in_bed_minutes": row.sleep_time_in_bed_minutes,
            "source": source,
            "freshness": "current",
            "earliest_available_work_time": wake.isoformat(),
        },
        {
            "domain": "wearable",
            "record_id": str(row.id),
            "source_provider": "healthmes-calendar-mirror",
            "upstream_provider": source,
            "resource_type": "actual_sleep",
            "observed_at": coerce_utc(row.end_at).isoformat(),
            "calendar_source": row.calendar_source.value,
            "account_generation": row.connection_generation,
            "schema_version": 1,
            "derived_by": "healthmes.actual-sleep-mirror.v1",
        },
    )


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
    *,
    account_generations: Mapping[CalendarSource, str] | None = None,
) -> str | None:
    start_utc = coerce_utc(start)
    end_utc = coerce_utc(end)
    statement = sa.select(CalendarEventMirror).where(
        CalendarEventMirror.is_agent_created.is_(True),
        CalendarEventMirror.healthmes_kind == HealthmesEventKind.ACTUAL_SLEEP.value,
        CalendarEventMirror.start_at < end_utc,
        CalendarEventMirror.end_at > start_utc,
    )
    if account_generations is not None:
        filters = tuple(
            sa.and_(
                CalendarEventMirror.calendar_source == source,
                CalendarEventMirror.connection_generation == generation,
            )
            for source, generation in account_generations.items()
        )
        if not filters:
            return None
        statement = statement.where(sa.or_(*filters))
    row = session.scalar(
        retained_calendar_statement(session, statement)
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
    *,
    account_generations: Mapping[CalendarSource, str] | None = None,
) -> CalendarEventMirror | None:
    statement = sa.select(CalendarEventMirror).where(
        CalendarEventMirror.is_agent_created.is_(True),
        CalendarEventMirror.healthmes_kind == HealthmesEventKind.ACTUAL_SLEEP.value,
        CalendarEventMirror.sleep_local_date == local_date,
    )
    if account_generations is not None:
        filters = tuple(
            sa.and_(
                CalendarEventMirror.calendar_source == source,
                CalendarEventMirror.connection_generation == generation,
            )
            for source, generation in account_generations.items()
        )
        if not filters:
            return None
        statement = statement.where(sa.or_(*filters))
    return session.scalar(
        retained_calendar_statement(session, statement)
        .order_by(
            CalendarEventMirror.sleep_duration_minutes.desc(),
            CalendarEventMirror.end_at.desc(),
        )
        .limit(1)
    )
