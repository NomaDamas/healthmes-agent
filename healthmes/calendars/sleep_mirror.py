from __future__ import annotations

from sqlalchemy.orm import Session

from healthmes.calendars.base import (
    CalendarEventIdentity,
    ExternalEvent,
    calendar_identity_external_id,
    ensure_utc,
)
from healthmes.calendars.sleep_observation import ActualSleepObservation
from healthmes.store.enums import CalendarSource
from healthmes.store.models import CalendarEventMirror

SLEEP_CREATE_PENDING_STATUS = "healthmes_pending_create"
SLEEP_UPDATE_PENDING_STATUS = "healthmes_pending_update"


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
    row.sleep_duration_minutes = observation.duration_minutes
    row.sleep_time_in_bed_minutes = observation.time_in_bed_minutes
    row.status = SLEEP_UPDATE_PENDING_STATUS
    session.commit()
