from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from healthmes.calendars.base import (
    CalendarBackend,
    CalendarEventIdentity,
    EventNotFoundError,
    ExternalEvent,
    HealthmesEventKind,
    ensure_utc,
)
from healthmes.calendars.sleep_observation import ActualSleepObservation
from healthmes.calendars.sleep_reconciliation import observation_fingerprint
from healthmes.store.enums import CalendarSource
from healthmes.store.models import CalendarEventMirror


def preview_sleep_reconciliation(
    session: Session,
    calendar_source: CalendarSource,
    observation: ActualSleepObservation,
    backend: CalendarBackend | None,
) -> dict[str, object]:
    existing = session.scalar(
        sa.select(CalendarEventMirror).where(
            CalendarEventMirror.calendar_source == calendar_source,
            CalendarEventMirror.healthmes_source_key == observation.source_key,
        )
    )
    fingerprint = observation_fingerprint(observation)
    action, reason = _actual_sleep_action(
        existing,
        observation,
        fingerprint,
        backend,
    )
    planned = session.scalars(
        sa.select(CalendarEventMirror).where(
            CalendarEventMirror.calendar_source == calendar_source,
            CalendarEventMirror.is_agent_created.is_(True),
            CalendarEventMirror.healthmes_kind
            == HealthmesEventKind.PLANNED_SLEEP.value,
            CalendarEventMirror.start_at < ensure_utc(observation.end_at),
            CalendarEventMirror.end_at > ensure_utc(observation.start_at),
        )
    ).all()
    planned_count = (
        0
        if action == "blocked"
        else sum(
            _remote_planned_sleep_overlaps(backend, row, observation)
            for row in planned
        )
    )
    result: dict[str, object] = {
        "status": "preview",
        "action": action,
        "calendar": calendar_source.value,
        "local_date": observation.local_date.isoformat(),
        "summary": "수면 (실제)",
        "start": ensure_utc(observation.start_at).isoformat(),
        "wake_time": ensure_utc(observation.end_at).isoformat(),
        "duration_minutes": observation.duration_minutes,
        "time_in_bed_minutes": observation.time_in_bed_minutes,
        "source": observation.provider,
        "planned_sleep_replacements": planned_count,
    }
    if reason is not None:
        result["reason"] = reason
    return result


def _actual_sleep_action(
    existing: CalendarEventMirror | None,
    observation: ActualSleepObservation,
    fingerprint: str,
    backend: CalendarBackend | None,
) -> tuple[str, str | None]:
    if existing is None:
        return "would_create", None
    identity = CalendarEventIdentity(
        kind=HealthmesEventKind.ACTUAL_SLEEP,
        source=observation.provider,
        source_key=observation.source_key,
    )
    if not _matches_actual_identity(existing, identity):
        return "blocked", "ownership_mismatch"
    action = (
        "noop"
        if existing.observation_fingerprint == fingerprint
        else "would_update"
    )
    if backend is None:
        return action, None
    try:
        remote = backend.read_event(existing.external_id)
    except EventNotFoundError:
        return "blocked", "calendar_event_missing"
    if not _matches_remote_actual_identity(remote, identity):
        return "blocked", "ownership_mismatch"
    if (
        action == "would_update"
        and existing.etag is not None
        and remote.etag != existing.etag
    ):
        return "blocked", "calendar_changed"
    return action, None


def _matches_actual_identity(
    row: CalendarEventMirror,
    identity: CalendarEventIdentity,
) -> bool:
    return (
        row.is_agent_created
        and row.healthmes_kind == identity.kind.value
        and row.healthmes_source == identity.source
        and row.healthmes_source_key == identity.source_key
    )


def _matches_remote_actual_identity(
    event: ExternalEvent,
    identity: CalendarEventIdentity,
) -> bool:
    return event.is_agent_created and event.identity == identity


def _remote_planned_sleep_overlaps(
    backend: CalendarBackend | None,
    row: CalendarEventMirror,
    observation: ActualSleepObservation,
) -> bool:
    if backend is None:
        return False
    try:
        event = backend.read_event(row.external_id)
    except EventNotFoundError:
        return False
    return bool(
        event.is_agent_created
        and event.healthmes_kind is HealthmesEventKind.PLANNED_SLEEP
        and (row.etag is None or event.etag == row.etag)
        and event.start_at is not None
        and event.end_at is not None
        and event.start_at < ensure_utc(observation.end_at)
        and event.end_at > ensure_utc(observation.start_at)
    )
