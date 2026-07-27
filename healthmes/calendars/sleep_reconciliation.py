from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import Session

from healthmes.calendars.base import (
    CalendarBackend,
    CalendarEventIdentity,
    EventDraft,
    EventNotFoundError,
    HealthmesEventKind,
    OwnershipError,
    ensure_utc,
)
from healthmes.calendars.sleep_observation import ActualSleepObservation
from healthmes.store.models import CalendarEventMirror


class SleepCalendarAction(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    NOOP = "noop"


@dataclass(frozen=True, slots=True)
class SleepCalendarResult:
    action: SleepCalendarAction
    external_id: str
    observation_fingerprint: str
    deleted_planned_external_ids: tuple[str, ...] = ()


class SleepCalendarReconciler:
    def __init__(self, session: Session, backend: CalendarBackend) -> None:
        self._session = session
        self._backend = backend

    def reconcile(self, observation: ActualSleepObservation) -> SleepCalendarResult:
        fingerprint = observation_fingerprint(observation)
        identity = CalendarEventIdentity(
            kind=HealthmesEventKind.ACTUAL_SLEEP,
            source=observation.provider,
            source_key=observation.source_key,
        )
        self._lock_source_key(identity.source_key)
        row = self._find_source_key(identity.source_key)
        if row is None:
            result = self._create(observation, identity, fingerprint)
        else:
            self._assert_owned_actual_sleep(row, identity)
            result = (
                SleepCalendarResult(
                    action=SleepCalendarAction.NOOP,
                    external_id=row.external_id,
                    observation_fingerprint=fingerprint,
                )
                if row.observation_fingerprint == fingerprint
                else self._update(row, observation, fingerprint)
            )
        deleted = self._delete_planned_sleep(observation)
        return replace(result, deleted_planned_external_ids=deleted)

    def _create(
        self,
        observation: ActualSleepObservation,
        identity: CalendarEventIdentity,
        fingerprint: str,
    ) -> SleepCalendarResult:
        draft = _event_draft(observation, identity)
        created = self._backend.create_event(draft)
        start_at = created.start_at or ensure_utc(observation.start_at)
        end_at = created.end_at or ensure_utc(observation.end_at)
        row = CalendarEventMirror(
            external_id=created.external_id,
            calendar_source=self._backend.source,
            summary=created.summary or draft.summary,
            start_at=start_at,
            end_at=end_at,
            is_agent_created=True,
            agent_task_id=None,
            healthmes_kind=identity.kind.value,
            healthmes_source=identity.source,
            healthmes_source_key=identity.source_key,
            observation_fingerprint=fingerprint,
            etag=created.etag,
            organizer_self=created.organizer_self,
            has_attendees=created.has_attendees,
            is_recurring=created.is_recurring,
            event_type=created.event_type,
            is_all_day=created.is_all_day,
            is_locked=created.is_locked,
            status=created.status,
        )
        self._session.add(row)
        self._session.commit()
        return SleepCalendarResult(
            action=SleepCalendarAction.CREATED,
            external_id=created.external_id,
            observation_fingerprint=fingerprint,
        )

    def _update(
        self,
        row: CalendarEventMirror,
        observation: ActualSleepObservation,
        fingerprint: str,
    ) -> SleepCalendarResult:
        updated = self._backend.update_event(
            row.external_id,
            summary="수면 (실제)",
            start_at=ensure_utc(observation.start_at),
            end_at=ensure_utc(observation.end_at),
            description=_description(observation),
        )
        row.summary = updated.summary or "수면 (실제)"
        row.start_at = updated.start_at or ensure_utc(observation.start_at)
        row.end_at = updated.end_at or ensure_utc(observation.end_at)
        row.etag = updated.etag
        row.observation_fingerprint = fingerprint
        self._session.commit()
        return SleepCalendarResult(
            action=SleepCalendarAction.UPDATED,
            external_id=row.external_id,
            observation_fingerprint=fingerprint,
        )

    def _find_source_key(self, source_key: str) -> CalendarEventMirror | None:
        return self._session.scalar(
            sa.select(CalendarEventMirror).where(
                CalendarEventMirror.calendar_source == self._backend.source,
                CalendarEventMirror.healthmes_source_key == source_key,
            )
        )

    def _lock_source_key(self, source_key: str) -> None:
        if self._session.get_bind().dialect.name != "postgresql":
            return
        self._session.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:source_key, 0))"),
            {"source_key": f"{self._backend.source.value}:{source_key}"},
        )

    def _delete_planned_sleep(
        self,
        observation: ActualSleepObservation,
    ) -> tuple[str, ...]:
        planned = self._session.scalars(
            sa.select(CalendarEventMirror).where(
                CalendarEventMirror.calendar_source == self._backend.source,
                CalendarEventMirror.is_agent_created.is_(True),
                CalendarEventMirror.healthmes_kind == HealthmesEventKind.PLANNED_SLEEP.value,
                CalendarEventMirror.start_at < ensure_utc(observation.end_at),
                CalendarEventMirror.end_at > ensure_utc(observation.start_at),
            )
        ).all()
        deleted: list[str] = []
        for row in planned:
            try:
                self._backend.delete_event(
                    row.external_id,
                    expected_kind=HealthmesEventKind.PLANNED_SLEEP,
                )
            except EventNotFoundError:
                pass
            self._session.delete(row)
            self._session.commit()
            deleted.append(row.external_id)
        return tuple(deleted)

    @staticmethod
    def _assert_owned_actual_sleep(
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


def observation_fingerprint(observation: ActualSleepObservation) -> str:
    values = (
        observation.local_date.isoformat(),
        observation.provider,
        observation.source_key,
        ensure_utc(observation.start_at).isoformat(),
        ensure_utc(observation.end_at).isoformat(),
        str(observation.duration_minutes),
        str(observation.time_in_bed_minutes),
    )
    return hashlib.sha256("\x1f".join(values).encode()).hexdigest()


def _event_draft(
    observation: ActualSleepObservation,
    identity: CalendarEventIdentity,
) -> EventDraft:
    return EventDraft(
        summary="수면 (실제)",
        start_at=observation.start_at,
        end_at=observation.end_at,
        description=_description(observation),
        identity=identity,
    )


def _description(observation: ActualSleepObservation) -> str:
    time_in_bed = (
        f"{observation.time_in_bed_minutes} min"
        if observation.time_in_bed_minutes is not None
        else "unavailable"
    )
    return (
        f"Actual sleep: {observation.duration_minutes} min\n"
        f"Time in bed: {time_in_bed}\n"
        f"Source: {observation.provider}"
    )
