from __future__ import annotations

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
from healthmes.calendars.sleep_event_rendering import observation_fingerprint
from healthmes.calendars.sleep_mirror import (
    SLEEP_CREATE_PENDING_STATUS,
    SLEEP_UPDATE_PENDING_STATUS,
    actual_sleep_identity,
    actual_sleep_identity_from_mirror,
    canonical_actual_sleep_mirror,
    find_actual_sleep_mirrors,
    pending_sleep_observation,
)
from healthmes.calendars.sleep_observation import ActualSleepObservation
from healthmes.calendars.sleep_reconciliation_guards import (
    assert_owned_actual_sleep,
    assert_remote_actual_sleep,
    pending_remote_matches,
)
from healthmes.store.enums import CalendarSource
from healthmes.store.models import CalendarEventMirror


def preview_sleep_reconciliation(
    session: Session,
    calendar_source: CalendarSource,
    observation: ActualSleepObservation,
    backend: CalendarBackend | None,
) -> dict[str, object]:
    rows = find_actual_sleep_mirrors(
        session,
        calendar_source,
        observation,
    )
    identity = actual_sleep_identity(observation)
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
    fingerprint = observation_fingerprint(observation)
    action, reason = _actual_sleep_action(
        existing,
        observation,
        fingerprint,
        backend,
    )
    if action != "blocked":
        legacy_reason = _legacy_actual_sleep_reason(
            rows,
            identity,
            calendar_source,
            backend,
        )
        if legacy_reason is not None:
            action = "blocked"
            reason = legacy_reason
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
