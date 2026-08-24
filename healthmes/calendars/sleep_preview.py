from __future__ import annotations

from typing import NotRequired, TypedDict

import sqlalchemy as sa
from sqlalchemy.orm import Session

from healthmes.calendars.base import (
    CalendarBackend,
    CalendarConflictError,
    CalendarEventIdentity,
    EventNotFoundError,
    ExternalEvent,
    HealthmesEventKind,
    OwnershipError,
    calendar_identity_external_id,
    ensure_utc,
)
from healthmes.calendars.planned_sleep_replacement import (
    read_owned_planned_sleep,
)
from healthmes.calendars.sleep_event_rendering import (
    ACTUAL_SLEEP_SUMMARY,
    observation_fingerprint,
)
from healthmes.calendars.sleep_mirror import (
    SLEEP_CREATE_PENDING_STATUS,
    SLEEP_UPDATE_PENDING_STATUS,
    actual_sleep_identity,
    actual_sleep_identity_from_mirror,
    canonical_actual_sleep_mirror,
    find_actual_sleep_mirrors,
    pending_sleep_observation,
)
from healthmes.calendars.sleep_observation import (
    ActualSleepObservation,
    calendar_observations,
)
from healthmes.calendars.sleep_reconciliation_guards import (
    assert_owned_actual_sleep,
    assert_remote_actual_sleep,
    pending_remote_matches,
)
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
    *,
    account_generation: str | None = None,
) -> SleepPreview:
    children = calendar_observations(observation)
    child_keys = {child.source_key for child in children}
    child_actions: list[str] = []
    reason: str | None = None
    for child in children:
        rows = find_actual_sleep_mirrors(
            session,
            calendar_source,
            child,
            account_generation=account_generation,
        )
        identity = actual_sleep_identity(child)
        existing = canonical_actual_sleep_mirror(rows, identity)
        if existing is None:
            existing = next(
                (
                    row
                    for row in rows
                    if (
                        (row_identity := actual_sleep_identity_from_mirror(row))
                        is not None
                        and row.is_agent_created
                        and row.external_id
                        == calendar_identity_external_id(calendar_source, row_identity)
                    )
                ),
                rows[0] if rows else None,
            )
        child_action, child_reason = _actual_sleep_action(
            existing,
            child,
            observation_fingerprint(child),
            backend,
        )
        if child_action != "blocked":
            child_reason = _legacy_actual_sleep_reason(
                rows,
                identity,
                calendar_source,
                backend,
            )
            if child_reason is not None:
                child_action = "blocked"
        child_actions.append(child_action)
        if child_reason is not None:
            reason = child_reason
            break
    stale_statement = sa.select(CalendarEventMirror).where(
        CalendarEventMirror.calendar_source == calendar_source,
        CalendarEventMirror.is_agent_created.is_(True),
        CalendarEventMirror.healthmes_kind
        == HealthmesEventKind.ACTUAL_SLEEP.value,
        CalendarEventMirror.sleep_local_date == observation.local_date,
        CalendarEventMirror.healthmes_source_key.like(
            f"{observation.source_key}:segment:%"
        ),
        CalendarEventMirror.healthmes_source_key.not_in(child_keys),
    )
    if account_generation is not None:
        stale_statement = stale_statement.where(
            CalendarEventMirror.connection_generation
            == account_generation
        )
    stale_segments = session.scalars(stale_statement).all()
    action = _combined_action(child_actions, stale_segments, bool(observation.segments))
    if reason is not None:
        action = "blocked"
    planned_statement = sa.select(CalendarEventMirror).where(
        CalendarEventMirror.calendar_source == calendar_source,
        CalendarEventMirror.is_agent_created.is_(True),
        CalendarEventMirror.healthmes_kind
        == HealthmesEventKind.PLANNED_SLEEP.value,
        CalendarEventMirror.start_at < ensure_utc(observation.end_at),
        CalendarEventMirror.end_at > ensure_utc(observation.start_at),
    )
    if account_generation is not None:
        planned_statement = planned_statement.where(
            CalendarEventMirror.connection_generation
            == account_generation
        )
    planned = session.scalars(planned_statement).all()
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
    identity = actual_sleep_identity(observation)
    if existing is None:
        return "would_create", None
    expected_external_id = calendar_identity_external_id(
        existing.calendar_source,
        identity,
    )
    has_canonical_identity = (
        existing.healthmes_kind == identity.kind.value
        and existing.healthmes_source == identity.source
        and existing.healthmes_source_key == identity.source_key
        and existing.external_id == expected_external_id
    )
    if has_canonical_identity and not existing.is_agent_created:
        return "blocked", "ownership_mismatch"
    if not _matches_actual_identity(existing, identity):
        if existing.external_id != expected_external_id:
            return "would_create", None
        if backend is None:
            return "blocked", "calendar_backend_unavailable"
        try:
            remote = backend.read_event(existing.external_id)
        except EventNotFoundError:
            return "would_create", None
        if not _matches_remote_actual_identity(remote, identity, expected_external_id):
            return "blocked", "ownership_mismatch"
        return (
            ("noop", None)
            if pending_remote_matches(remote, observation)
            else ("would_update", None)
        )
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
    if not _matches_remote_actual_identity(remote, identity, expected_external_id):
        return "blocked", "ownership_mismatch"
    pending_observation = (
        pending_sleep_observation(existing)
        if pending_create or pending_update
        else observation
    )
    if pending_create:
        if not pending_remote_matches(remote, pending_observation):
            return "blocked", "calendar_changed"
        return (
            ("noop", None)
            if existing.observation_fingerprint == fingerprint
            else ("would_update", None)
        )
    if pending_update:
        if existing.etag is None or remote.etag == existing.etag:
            return "would_update", None
        if pending_remote_matches(remote, pending_observation):
            return (
                ("noop", None)
                if existing.observation_fingerprint == fingerprint
                else ("would_update", None)
            )
        return "blocked", "calendar_changed"
    if action == "would_update" and existing.etag is not None and remote.etag != existing.etag:
        return "blocked", "calendar_changed"
    return action, None


def _legacy_actual_sleep_reason(
    rows: list[CalendarEventMirror],
    canonical_identity: CalendarEventIdentity,
    calendar_source: CalendarSource,
    backend: CalendarBackend | None,
) -> str | None:
    for row in rows:
        identity = actual_sleep_identity_from_mirror(row)
        if (
            identity is None
            or identity == canonical_identity
            or row.external_id
            != calendar_identity_external_id(calendar_source, identity)
        ):
            continue
        try:
            assert_owned_actual_sleep(row, identity)
        except OwnershipError:
            return "ownership_mismatch"
        if backend is None:
            return "calendar_backend_unavailable"
        try:
            remote = backend.read_event(row.external_id)
        except EventNotFoundError:
            continue
        try:
            assert_remote_actual_sleep(
                remote,
                calendar_source,
                identity,
                row.etag,
            )
        except OwnershipError:
            return "ownership_mismatch"
        except CalendarConflictError:
            return "calendar_changed"
    return None


def _matches_actual_identity(
    row: CalendarEventMirror,
    identity: CalendarEventIdentity,
) -> bool:
    return (
        row.is_agent_created
        and row.external_id
        == calendar_identity_external_id(row.calendar_source, identity)
        and row.healthmes_kind == identity.kind.value
        and row.healthmes_source == identity.source
        and row.healthmes_source_key == identity.source_key
    )


def _matches_remote_actual_identity(
    event: ExternalEvent,
    identity: CalendarEventIdentity,
    expected_external_id: str,
) -> bool:
    return (
        event.is_agent_created
        and event.external_id == expected_external_id
        and event.identity == identity
    )


def _remote_planned_sleep_state(
    backend: CalendarBackend | None,
    row: CalendarEventMirror,
    observation: ActualSleepObservation,
) -> tuple[bool, str | None]:
    if backend is None:
        return False, "calendar_backend_unavailable"
    try:
        read_owned_planned_sleep(backend, row, observation)
    except OwnershipError:
        return False, "planned_sleep_ownership_mismatch"
    except CalendarConflictError:
        return False, "planned_sleep_changed"
    return True, None
