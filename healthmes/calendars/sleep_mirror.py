from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from healthmes.calendars.base import (
    CalendarEventIdentity,
    ExternalEvent,
    HealthmesEventKind,
    calendar_identity_external_id,
    coerce_utc,
    ensure_utc,
    parse_calendar_identity,
)
from healthmes.calendars.sleep_observation import (
    ACTUAL_SLEEP_IDENTITY_SOURCE,
    ActualSleepObservation,
    actual_sleep_source_key,
)
from healthmes.store.enums import CalendarSource
from healthmes.store.models import CalendarEventMirror

SLEEP_CREATE_PENDING_STATUS = "healthmes_pending_create"
SLEEP_UPDATE_PENDING_STATUS = "healthmes_pending_update"


def actual_sleep_identity(
    observation: ActualSleepObservation,
) -> CalendarEventIdentity:
    return CalendarEventIdentity(
        kind=HealthmesEventKind.ACTUAL_SLEEP,
        source=ACTUAL_SLEEP_IDENTITY_SOURCE,
        source_key=actual_sleep_source_key(observation.local_date),
    )


def actual_sleep_identity_from_mirror(
    row: CalendarEventMirror,
) -> CalendarEventIdentity | None:
    identity = parse_calendar_identity(
        row.healthmes_kind,
        row.healthmes_source,
        row.healthmes_source_key,
    )
    if identity is None or identity.kind is not HealthmesEventKind.ACTUAL_SLEEP:
        return None
    return identity


def find_actual_sleep_mirrors(
    session: Session,
    calendar_source: CalendarSource,
    observation: ActualSleepObservation,
) -> list[CalendarEventMirror]:
    canonical = actual_sleep_identity(observation)
    provider_match = sa.or_(
        CalendarEventMirror.sleep_provider == observation.provider,
        sa.and_(
            CalendarEventMirror.sleep_provider.is_(None),
            CalendarEventMirror.healthmes_source == observation.provider,
        ),
    )
    return list(
        session.scalars(
            sa.select(CalendarEventMirror)
            .where(
                CalendarEventMirror.calendar_source == calendar_source,
                sa.or_(
                    CalendarEventMirror.healthmes_source_key
                    == canonical.source_key,
                    sa.and_(
                        CalendarEventMirror.is_agent_created.is_(True),
                        CalendarEventMirror.healthmes_kind
                        == HealthmesEventKind.ACTUAL_SLEEP.value,
                        CalendarEventMirror.sleep_local_date
                        == observation.local_date,
                    ),
                ),
            )
            .order_by(
                sa.case(
                    (
                        CalendarEventMirror.healthmes_source_key
                        == canonical.source_key,
                        1,
                    ),
                    else_=0,
                ).desc(),
                sa.case((provider_match, 1), else_=0).desc(),
                sa.case(
                    (
                        CalendarEventMirror.status.in_(
                            (
                                SLEEP_CREATE_PENDING_STATUS,
                                SLEEP_UPDATE_PENDING_STATUS,
                            )
                        ),
                        1,
                    ),
                    else_=0,
                ).desc(),
                CalendarEventMirror.updated_at.desc(),
                CalendarEventMirror.id,
            )
        ).all()
    )


def find_actual_sleep_mirror(
    session: Session,
    calendar_source: CalendarSource,
    observation: ActualSleepObservation,
) -> CalendarEventMirror | None:
    rows = find_actual_sleep_mirrors(
        session,
        calendar_source,
        observation,
    )
    return rows[0] if rows else None


def pending_sleep_observation(
    row: CalendarEventMirror,
) -> ActualSleepObservation:
    provider = row.sleep_provider
    if provider is None and row.healthmes_source != ACTUAL_SLEEP_IDENTITY_SOURCE:
        provider = row.healthmes_source
    if (
        provider is None
        or row.sleep_local_date is None
        or row.sleep_duration_minutes is None
    ):
        raise RuntimeError("pending actual_sleep mirror is missing observation context")
    return ActualSleepObservation(
        local_date=row.sleep_local_date,
        provider=provider,
        source_key=actual_sleep_source_key(row.sleep_local_date),
        start_at=coerce_utc(row.start_at),
        end_at=coerce_utc(row.end_at),
        duration_minutes=row.sleep_duration_minutes,
        time_in_bed_minutes=row.sleep_time_in_bed_minutes,
    )


def pending_sleep_mirror(
    calendar_source: CalendarSource,
    observation: ActualSleepObservation,
    identity: CalendarEventIdentity,
    fingerprint: str,
) -> CalendarEventMirror:
    return CalendarEventMirror(
        external_id=calendar_identity_external_id(calendar_source, identity),
        calendar_source=calendar_source,
        summary="수면 (실제)",
        start_at=ensure_utc(observation.start_at),
        end_at=ensure_utc(observation.end_at),
        is_agent_created=True,
        agent_task_id=None,
        healthmes_kind=identity.kind.value,
        healthmes_source=identity.source,
        healthmes_source_key=identity.source_key,
        observation_fingerprint=fingerprint,
        sleep_local_date=observation.local_date,
        sleep_provider=observation.provider,
        sleep_duration_minutes=observation.duration_minutes,
        sleep_time_in_bed_minutes=observation.time_in_bed_minutes,
        status=SLEEP_CREATE_PENDING_STATUS,
    )


def finalize_sleep_mirror(
    session: Session,
    row: CalendarEventMirror,
    created: ExternalEvent,
    observation: ActualSleepObservation,
    fingerprint: str,
) -> None:
    row.external_id = created.external_id
    row.summary = created.summary or "수면 (실제)"
    row.start_at = created.start_at or ensure_utc(observation.start_at)
    row.end_at = created.end_at or ensure_utc(observation.end_at)
    row.etag = created.etag
    row.observation_fingerprint = fingerprint
    row.sleep_local_date = observation.local_date
    row.sleep_provider = observation.provider
    row.sleep_duration_minutes = observation.duration_minutes
    row.sleep_time_in_bed_minutes = observation.time_in_bed_minutes
    row.organizer_self = created.organizer_self
    row.has_attendees = created.has_attendees
    row.is_recurring = created.is_recurring
    row.event_type = created.event_type
    row.is_all_day = created.is_all_day
    row.is_locked = created.is_locked
    row.status = created.status
    session.commit()


def mark_sleep_update_pending(
    session: Session,
    row: CalendarEventMirror,
    observation: ActualSleepObservation,
    fingerprint: str,
    expected_etag: str | None,
) -> None:
    row.summary = "수면 (실제)"
    row.start_at = ensure_utc(observation.start_at)
    row.end_at = ensure_utc(observation.end_at)
    row.etag = expected_etag
    row.observation_fingerprint = fingerprint
    row.sleep_local_date = observation.local_date
    row.sleep_provider = observation.provider
    row.sleep_duration_minutes = observation.duration_minutes
    row.sleep_time_in_bed_minutes = observation.time_in_bed_minutes
    row.status = SLEEP_UPDATE_PENDING_STATUS
    session.commit()
