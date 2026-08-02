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
from healthmes.calendars.sleep_observation import (
    ActualSleepObservation,
    calendar_observations,
)
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
    segments: NotRequired[list[dict[str, object]]]
    segment_count: NotRequired[int]
    stale_segment_removals: NotRequired[int]
    reason: NotRequired[str]


def preview_sleep_reconciliation(
    session: Session,
    calendar_source: CalendarSource,
    observation: ActualSleepObservation,
    backend: CalendarBackend | None,
) -> SleepPreview:
    children = calendar_observations(observation)
    child_keys = {child.source_key for child in children}
    child_actions: list[str] = []
    reason: str | None = None
    for child in children:
        existing = session.scalar(
            sa.select(CalendarEventMirror).where(
                CalendarEventMirror.calendar_source == calendar_source,
                CalendarEventMirror.healthmes_source_key == child.source_key,
            )
        )
        child_action, child_reason = _actual_sleep_action(
            existing,
            child,
            observation_fingerprint(child),
            backend,
        )
        child_actions.append(child_action)
        if child_reason is not None:
            reason = child_reason
            break
    stale_segments = session.scalars(
        sa.select(CalendarEventMirror).where(
            CalendarEventMirror.calendar_source == calendar_source,
            CalendarEventMirror.is_agent_created.is_(True),
            CalendarEventMirror.healthmes_kind == HealthmesEventKind.ACTUAL_SLEEP.value,
            CalendarEventMirror.healthmes_source == observation.provider,
            CalendarEventMirror.sleep_local_date == observation.local_date,
            CalendarEventMirror.healthmes_source_key.like(
                f"{observation.source_key}:segment:%"
            ),
            CalendarEventMirror.healthmes_source_key.not_in(child_keys),
        )
    ).all()
    action = _combined_action(child_actions, stale_segments, bool(observation.segments))
    if reason is not None:
        action = "blocked"
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
    if observation.segments:
        result["segments"] = [
            {
                "start": ensure_utc(segment.start_at).isoformat(),
                "wake_time": ensure_utc(segment.end_at).isoformat(),
                "duration_minutes": int(
                    (segment.end_at - segment.start_at).total_seconds() // 60
                ),
            }
            for segment in observation.segments
        ]
        result["segment_count"] = len(observation.segments)
        result["stale_segment_removals"] = len(stale_segments)
    return result


def _combined_action(
    child_actions: list[str],
    stale_segments: list[CalendarEventMirror],
    is_split: bool,
) -> str:
    if "blocked" in child_actions:
        return "blocked"
    if all(action == "noop" for action in child_actions) and not stale_segments:
        return "noop"
    if is_split:
        return "would_split"
    if "would_create" in child_actions:
        return "would_create"
    return "would_update"


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
        return "would_create", None
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
    return not event.deleted and event.is_agent_created and event.identity == identity


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
