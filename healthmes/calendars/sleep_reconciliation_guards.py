from __future__ import annotations

from healthmes.calendars.base import (
    CalendarConflictError,
    CalendarEventIdentity,
    ExternalEvent,
    OwnershipError,
    ensure_utc,
)
from healthmes.calendars.sleep_event_rendering import (
    ACTUAL_SLEEP_SUMMARY,
    LEGACY_ACTUAL_SLEEP_SUMMARY,
)
from healthmes.calendars.sleep_observation import ActualSleepObservation
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
    identity: CalendarEventIdentity,
    mirror_etag: str | None,
) -> str | None:
    if event.deleted or not event.is_agent_created or event.identity != identity:
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
        not event.deleted
        and event.summary in {ACTUAL_SLEEP_SUMMARY, LEGACY_ACTUAL_SLEEP_SUMMARY}
        and event.start_at == ensure_utc(observation.start_at)
        and event.end_at == ensure_utc(observation.end_at)
    )
