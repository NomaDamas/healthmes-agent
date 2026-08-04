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
from healthmes.calendars.sleep_event_rendering import ACTUAL_SLEEP_SUMMARY
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
        source_key=observation.source_key,
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
    canonical_external_id = calendar_identity_external_id(
        calendar_source,
        canonical,
    )
    base_source_key = actual_sleep_source_key(observation.local_date)
    is_base_observation = observation.source_key == base_source_key
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
                    CalendarEventMirror.external_id == canonical_external_id,
                    CalendarEventMirror.healthmes_source_key
                    == canonical.source_key,
                    sa.and_(
                        is_base_observation,
                        CalendarEventMirror.is_agent_created.is_(True),
                        CalendarEventMirror.healthmes_kind
                        == HealthmesEventKind.ACTUAL_SLEEP.value,
                        CalendarEventMirror.sleep_local_date
                        == observation.local_date,
                        sa.or_(
                            CalendarEventMirror.healthmes_source_key.is_(None),
                            CalendarEventMirror.healthmes_source_key.not_like(
                                f"{base_source_key}:segment:%"
                            ),
                        ),
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
    canonical = canonical_actual_sleep_mirror(
        rows,
        actual_sleep_identity(observation),
    )
    if canonical is not None:
        return canonical
    for row in rows:
        identity = actual_sleep_identity_from_mirror(row)
        if (
            identity is not None
            and row.is_agent_created
            and row.external_id
            == calendar_identity_external_id(calendar_source, identity)
        ):
            return row
    return rows[0] if rows else None


def canonical_actual_sleep_mirror(
    rows: list[CalendarEventMirror],
    identity: CalendarEventIdentity,
) -> CalendarEventMirror | None:
    expected_external_id = calendar_identity_external_id(
        rows[0].calendar_source,
        identity,
    ) if rows else None
    for row in rows:
        if (
            row.external_id == expected_external_id
            and row.healthmes_kind == identity.kind.value
            and row.healthmes_source == identity.source
            and row.healthmes_source_key == identity.source_key
        ):
            return row
    return None


def sleep_observation_from_mirror(
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
    base_source_key = actual_sleep_source_key(row.sleep_local_date)
    source_key = (
        row.healthmes_source_key
        if row.healthmes_source_key
        and (
            row.healthmes_source == ACTUAL_SLEEP_IDENTITY_SOURCE
            or row.healthmes_source_key.startswith(f"{base_source_key}:segment:")
        )
        else base_source_key
    )
    return ActualSleepObservation(
        local_date=row.sleep_local_date,
        provider=provider,
        source_key=source_key,
        start_at=coerce_utc(row.start_at),
        end_at=coerce_utc(row.end_at),
        duration_minutes=row.sleep_duration_minutes,
        time_in_bed_minutes=row.sleep_time_in_bed_minutes,
    )


def pending_sleep_observation(
    row: CalendarEventMirror,
) -> ActualSleepObservation:
    return sleep_observation_from_mirror(row)


def adopt_remote_actual_sleep(
    row: CalendarEventMirror,
    remote: ExternalEvent,
    identity: CalendarEventIdentity,
) -> None:
    if remote.start_at is None or remote.end_at is None:
        raise RuntimeError("live actual_sleep event is missing its time range")
    row.external_id = remote.external_id
    row.summary = remote.summary or "수면 (실제)"
    row.start_at = remote.start_at
    row.end_at = remote.end_at
    row.is_agent_created = True
    row.agent_task_id = None
    row.healthmes_kind = identity.kind.value
    row.healthmes_source = identity.source
    row.healthmes_source_key = identity.source_key
    row.etag = remote.etag
    row.organizer_self = remote.organizer_self
    row.has_attendees = remote.has_attendees
    row.is_recurring = remote.is_recurring
    row.event_type = remote.event_type
    row.is_all_day = remote.is_all_day
    row.is_locked = remote.is_locked
    row.status = remote.status


def quarantine_sleep_identity(row: CalendarEventMirror) -> None:
    row.is_agent_created = False
    row.agent_task_id = None
    row.healthmes_kind = None
    row.healthmes_source = None
    row.healthmes_source_key = None
    row.observation_fingerprint = None
    row.sleep_local_date = None
    row.sleep_provider = None
    row.sleep_duration_minutes = None
    row.sleep_time_in_bed_minutes = None


def pending_sleep_mirror(
    calendar_source: CalendarSource,
    observation: ActualSleepObservation,
    identity: CalendarEventIdentity,
    fingerprint: str,
) -> CalendarEventMirror:
    return CalendarEventMirror(
        external_id=calendar_identity_external_id(calendar_source, identity),
        calendar_source=calendar_source,
        summary=ACTUAL_SLEEP_SUMMARY,
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


def find_sleep_source_key(
    session: Session,
    calendar_source: CalendarSource,
    source_key: str,
) -> CalendarEventMirror | None:
    return session.scalar(
        sa.select(CalendarEventMirror).where(
            CalendarEventMirror.calendar_source == calendar_source,
            CalendarEventMirror.healthmes_source_key == source_key,
        )
    )


def finalize_sleep_mirror(
    session: Session,
    row: CalendarEventMirror,
    created: ExternalEvent,
    observation: ActualSleepObservation,
    fingerprint: str,
) -> None:
    row.external_id = created.external_id
    row.summary = created.summary or ACTUAL_SLEEP_SUMMARY
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
    row.summary = ACTUAL_SLEEP_SUMMARY
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
