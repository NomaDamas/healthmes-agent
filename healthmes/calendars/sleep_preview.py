from __future__ import annotations

from typing import NotRequired, TypedDict

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
from healthmes.calendars.sleep_event_rendering import ACTUAL_SLEEP_SUMMARY
from healthmes.calendars.sleep_mirror import (
    SLEEP_CREATE_PENDING_STATUS,
    SLEEP_UPDATE_PENDING_STATUS,
)
from healthmes.calendars.sleep_observation import ActualSleepObservation
from healthmes.calendars.sleep_reconciliation import observation_fingerprint
from healthmes.calendars.sleep_reconciliation_guards import pending_remote_matches
from healthmes.store.enums import CalendarSource
from healthmes.store.models import CalendarEventMirror


class SleepPreview(TypedDict):
    status: str
    action: str
    calendar: str
    local_date: str
    summary: str
    start: str
    wake_time: str
    duration_minutes: int
    time_in_bed_minutes: int | None
    non_sleep_minutes: int | None
    source: str
    planned_sleep_replacements: int
    reason: NotRequired[str]


def preview_sleep_reconciliation(
    session: Session,
    calendar_source: CalendarSource,
    observation: ActualSleepObservation,
    backend: CalendarBackend | None,
) -> SleepPreview:
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
            CalendarEventMirror.healthmes_kind == HealthmesEventKind.PLANNED_SLEEP.value,
            CalendarEventMirror.start_at < ensure_utc(observation.end_at),
            CalendarEventMirror.end_at > ensure_utc(observation.start_at),
        )
    ).all()
    planned_count = 0
    if action != "blocked":
        for row in planned:
            replace, planned_reason = _remote_planned_sleep_state(
                backend,
                row,
                observation,
            )
            if planned_reason is not None:
                action = "blocked"
                reason = planned_reason
                planned_count = 0
                break
            planned_count += int(replace)
    result: SleepPreview = {
        "status": "preview",
        "action": action,
        "calendar": calendar_source.value,
        "local_date": observation.local_date.isoformat(),
        "summary": ACTUAL_SLEEP_SUMMARY,
        "start": ensure_utc(observation.start_at).isoformat(),
        "wake_time": ensure_utc(observation.end_at).isoformat(),
        "duration_minutes": observation.duration_minutes,
        "time_in_bed_minutes": observation.time_in_bed_minutes,
        "non_sleep_minutes": (
            observation.time_in_bed_minutes - observation.duration_minutes
            if observation.time_in_bed_minutes is not None
            else None
        ),
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
    pending_create = existing.status == SLEEP_CREATE_PENDING_STATUS
    pending_update = existing.status == SLEEP_UPDATE_PENDING_STATUS
    action = "noop" if existing.observation_fingerprint == fingerprint else "would_update"
    if backend is None:
        if pending_create:
            return "would_create", None
        if pending_update:
            return "would_update", None
        return action, None
    try:
        remote = backend.read_event(existing.external_id)
    except EventNotFoundError:
        return ("would_create", None) if pending_create else ("blocked", "calendar_event_missing")
    if not _matches_remote_actual_identity(remote, identity):
        return "blocked", "ownership_mismatch"
    if pending_create:
        if not pending_remote_matches(remote, observation):
            return "blocked", "calendar_changed"
        return "noop", None
    if pending_update:
        if existing.etag is None or remote.etag == existing.etag:
            return "would_update", None
        if pending_remote_matches(remote, observation):
            return "noop", None
        return "blocked", "calendar_changed"
    if action == "would_update" and existing.etag is not None and remote.etag != existing.etag:
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


def _remote_planned_sleep_state(
    backend: CalendarBackend | None,
    row: CalendarEventMirror,
    observation: ActualSleepObservation,
) -> tuple[bool, str | None]:
    if backend is None:
        return False, "calendar_backend_unavailable"
    try:
        event = backend.read_event(row.external_id)
    except EventNotFoundError:
        return False, "planned_sleep_missing"
    if not event.is_agent_created or event.healthmes_kind is not HealthmesEventKind.PLANNED_SLEEP:
        return False, "planned_sleep_ownership_mismatch"
    if row.etag is not None and event.etag != row.etag:
        return False, "planned_sleep_changed"
    if (
        event.start_at is None
        or event.end_at is None
        or event.start_at >= ensure_utc(observation.end_at)
        or event.end_at <= ensure_utc(observation.start_at)
    ):
        return False, "planned_sleep_changed"
    return True, None
