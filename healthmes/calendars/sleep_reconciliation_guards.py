from __future__ import annotations

from healthmes.calendars.base import (
    CalendarConflictError,
    CalendarEventIdentity,
    ExternalEvent,
    OwnershipError,
    calendar_identity_external_id,
    ensure_utc,
)
from healthmes.calendars.sleep_event_rendering import (
    ACTUAL_SLEEP_SUMMARY,
    description,
)
from healthmes.calendars.sleep_observation import ActualSleepObservation
from healthmes.store.enums import CalendarSource
from healthmes.store.models import CalendarEventMirror


def assert_owned_actual_sleep(
    row: CalendarEventMirror,
    identity: CalendarEventIdentity,
) -> None:
    matches_identity = (
        row.healthmes_kind == identity.kind.value
        and row.healthmes_source == identity.source
        and row.healthmes_source_key == identity.source_key
    )
    if not row.is_agent_created or not matches_identity:
        raise OwnershipError(
            f"{row.calendar_source.value} source key {identity.source_key!r} "
            "is not an owned actual_sleep event"
        )


def assert_remote_actual_sleep(
    event: ExternalEvent,
    calendar_source: CalendarSource,
    identity: CalendarEventIdentity,
    mirror_etag: str | None,
) -> str | None:
    if (
        not event.is_agent_created
        or event.identity != identity
        or event.external_id
        != calendar_identity_external_id(calendar_source, identity)
    ):
        raise OwnershipError("remote event is not the expected actual_sleep event")
    if mirror_etag is not None and event.etag != mirror_etag:
        raise CalendarConflictError("remote actual_sleep event changed after sync")
    return mirror_etag or event.etag


def assert_pending_remote_matches(
    event: ExternalEvent,
    observation: ActualSleepObservation,
) -> None:
    if not pending_remote_matches(event, observation):
        raise CalendarConflictError(
            "pending actual_sleep event changed before local recovery"
        )


def pending_remote_matches(
    event: ExternalEvent,
    observation: ActualSleepObservation,
) -> bool:
    return (
        event.summary == ACTUAL_SLEEP_SUMMARY
        and event.description == description(observation)
        and event.start_at == ensure_utc(observation.start_at)
        and event.end_at == ensure_utc(observation.end_at)
    )
