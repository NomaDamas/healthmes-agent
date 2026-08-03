from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from healthmes.calendars.base import (
    CalendarBackend,
    CalendarConflictError,
    CalendarEventIdentity,
    EventNotFoundError,
    HealthmesEventKind,
    OwnershipError,
    ensure_utc,
)
from healthmes.calendars.planned_sleep_replacement import (
    delete_replaced_planned_sleep,
)
from healthmes.calendars.sleep_event_rendering import (
    description,
    event_draft,
    observation_fingerprint,
)
from healthmes.calendars.sleep_mirror import (
    SLEEP_CREATE_PENDING_STATUS,
    SLEEP_UPDATE_PENDING_STATUS,
    actual_sleep_identity,
    actual_sleep_identity_from_mirror,
    finalize_sleep_mirror,
    find_actual_sleep_mirrors,
    mark_sleep_update_pending,
    pending_sleep_mirror,
    pending_sleep_observation,
)
from healthmes.calendars.sleep_observation import ActualSleepObservation
from healthmes.calendars.sleep_reconciliation_guards import (
    assert_owned_actual_sleep,
    assert_pending_remote_matches,
    assert_remote_actual_sleep,
)
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
    planned_sleep_cleanup_pending: int = 0


class SleepCalendarReconciler:
    def __init__(self, session: Session, backend: CalendarBackend) -> None:
        self._session = session
        self._backend = backend

    def reconcile(self, observation: ActualSleepObservation) -> SleepCalendarResult:
        fingerprint = observation_fingerprint(observation)
        identity = actual_sleep_identity(observation)
        lock_connection = self._lock_source_key(identity.source_key)
        try:
            return self._reconcile_locked(observation, identity, fingerprint)
        finally:
            if lock_connection is not None:
                self._unlock_source_key(lock_connection, identity.source_key)

    def _reconcile_locked(
        self,
        observation: ActualSleepObservation,
        identity: CalendarEventIdentity,
        fingerprint: str,
    ) -> SleepCalendarResult:
        rows = find_actual_sleep_mirrors(
            self._session,
            self._backend.source,
            observation,
        )
        if not rows:
            result = self._create(observation, identity, fingerprint)
            return self._finish(result, observation, [])

        row = rows[0]
        identity = actual_sleep_identity_from_mirror(row) or identity
        assert_owned_actual_sleep(row, identity)
        duplicates = rows[1:]
        pending_observation = (
            pending_sleep_observation(row)
            if row.status in {
                SLEEP_CREATE_PENDING_STATUS,
                SLEEP_UPDATE_PENDING_STATUS,
            }
            else None
        )
        pending_fingerprint = row.observation_fingerprint
        if pending_observation is not None and not pending_fingerprint:
            raise RuntimeError("pending actual_sleep mirror is missing its fingerprint")
        try:
            remote = self._backend.read_event(row.external_id)
        except EventNotFoundError:
            if row.status != SLEEP_CREATE_PENDING_STATUS:
                raise
            assert pending_observation is not None
            assert pending_fingerprint is not None
            result = self._create_remote(
                row,
                pending_observation,
                identity,
                pending_fingerprint,
            )
            result = self._apply_latest_after_recovery(
                row,
                observation,
                fingerprint,
                result,
            )
            return self._finish(result, observation, duplicates)

        if row.status == SLEEP_UPDATE_PENDING_STATUS:
            assert pending_observation is not None
            assert pending_fingerprint is not None
            result = self._recover_pending_update(
                row,
                remote,
                pending_observation,
                identity,
                pending_fingerprint,
            )
            result = self._apply_latest_after_recovery(
                row,
                observation,
                fingerprint,
                result,
            )
            return self._finish(result, observation, duplicates)

        expected_etag = assert_remote_actual_sleep(
            remote,
            identity,
            row.etag,
        )
        if row.status == SLEEP_CREATE_PENDING_STATUS:
            assert pending_observation is not None
            assert pending_fingerprint is not None
            assert_pending_remote_matches(remote, pending_observation)
            finalize_sleep_mirror(
                self._session,
                row,
                remote,
                pending_observation,
                pending_fingerprint,
            )
            result = self._created_result(row.external_id, pending_fingerprint)
            result = self._apply_latest_after_recovery(
                row,
                observation,
                fingerprint,
                result,
            )
            return self._finish(result, observation, duplicates)

        if row.observation_fingerprint == fingerprint:
            result = SleepCalendarResult(
                action=SleepCalendarAction.NOOP,
                external_id=row.external_id,
                observation_fingerprint=fingerprint,
            )
        else:
            result = self._update(row, observation, fingerprint, expected_etag)
        return self._finish(result, observation, duplicates)

    def _finish(
        self,
        result: SleepCalendarResult,
        observation: ActualSleepObservation,
        duplicates: list[CalendarEventMirror],
    ) -> SleepCalendarResult:
        self._delete_duplicate_actual_sleep_mirrors(duplicates)
        return self._replace_planned_sleep(result, observation)

    def _delete_duplicate_actual_sleep_mirrors(
        self,
        duplicates: list[CalendarEventMirror],
    ) -> None:
        for duplicate in duplicates:
            identity = actual_sleep_identity_from_mirror(duplicate)
            if identity is None:
                raise OwnershipError(
                    f"{duplicate.calendar_source.value} event "
                    f"{duplicate.external_id!r} has invalid actual_sleep identity"
                )
            assert_owned_actual_sleep(duplicate, identity)
            try:
                remote = self._backend.read_event(duplicate.external_id)
                expected_etag = assert_remote_actual_sleep(
                    remote,
                    identity,
                    duplicate.etag,
                )
                self._backend.delete_event(
                    duplicate.external_id,
                    expected_kind=HealthmesEventKind.ACTUAL_SLEEP,
                    expected_etag=expected_etag,
                )
            except EventNotFoundError:
                pass
            self._session.delete(duplicate)
            self._session.commit()

    def _apply_latest_after_recovery(
        self,
        row: CalendarEventMirror,
        observation: ActualSleepObservation,
        fingerprint: str,
        recovered: SleepCalendarResult,
    ) -> SleepCalendarResult:
        if row.observation_fingerprint == fingerprint:
            return recovered
        return self._update(
            row,
            observation,
            fingerprint,
            row.etag,
        )

    def _replace_planned_sleep(
        self,
        result: SleepCalendarResult,
        observation: ActualSleepObservation,
    ) -> SleepCalendarResult:
        replacement = delete_replaced_planned_sleep(
            self._session,
            self._backend,
            observation,
        )
        return replace(
            result,
            deleted_planned_external_ids=replacement.deleted_external_ids,
            planned_sleep_cleanup_pending=replacement.cleanup_pending,
        )

    def _create(
        self,
        observation: ActualSleepObservation,
        identity: CalendarEventIdentity,
        fingerprint: str,
    ) -> SleepCalendarResult:
        row = pending_sleep_mirror(
            self._backend.source,
            observation,
            identity,
            fingerprint,
        )
        self._session.add(row)
        self._session.commit()
        return self._create_remote(row, observation, identity, fingerprint)

    def _create_remote(
        self,
        row: CalendarEventMirror,
        observation: ActualSleepObservation,
        identity: CalendarEventIdentity,
        fingerprint: str,
    ) -> SleepCalendarResult:
        try:
            created = self._backend.create_event(event_draft(observation, identity))
        except CalendarConflictError:
            created = self._backend.read_event(row.external_id)
            assert_remote_actual_sleep(created, identity, row.etag)
            assert_pending_remote_matches(created, observation)
        finalize_sleep_mirror(
            self._session,
            row,
            created,
            observation,
            fingerprint,
        )
        return self._created_result(row.external_id, fingerprint)

    @staticmethod
    def _created_result(external_id: str, fingerprint: str) -> SleepCalendarResult:
        return SleepCalendarResult(
            action=SleepCalendarAction.CREATED,
            external_id=external_id,
            observation_fingerprint=fingerprint,
        )

    def _update(
        self,
        row: CalendarEventMirror,
        observation: ActualSleepObservation,
        fingerprint: str,
        expected_etag: str | None,
    ) -> SleepCalendarResult:
        mark_sleep_update_pending(
            self._session,
            row,
            observation,
            fingerprint,
            expected_etag,
        )
        return self._update_remote(row, observation, fingerprint, expected_etag)

    def _recover_pending_update(
        self,
        row: CalendarEventMirror,
        remote,
        observation: ActualSleepObservation,
        identity: CalendarEventIdentity,
        fingerprint: str,
    ) -> SleepCalendarResult:
        remote_etag = assert_remote_actual_sleep(remote, identity, None)
        if row.etag is not None and remote_etag != row.etag:
            assert_pending_remote_matches(remote, observation)
            finalize_sleep_mirror(
                self._session,
                row,
                remote,
                observation,
                fingerprint,
            )
            return self._updated_result(row.external_id, fingerprint)
        return self._update_remote(
            row,
            observation,
            fingerprint,
            row.etag or remote_etag,
        )

    def _update_remote(
        self,
        row: CalendarEventMirror,
        observation: ActualSleepObservation,
        fingerprint: str,
        expected_etag: str | None,
    ) -> SleepCalendarResult:
        updated = self._backend.update_event(
            row.external_id,
            summary="수면 (실제)",
            start_at=ensure_utc(observation.start_at),
            end_at=ensure_utc(observation.end_at),
            description=description(observation),
            expected_etag=expected_etag,
        )
        finalize_sleep_mirror(
            self._session,
            row,
            updated,
            observation,
            fingerprint,
        )
        return self._updated_result(row.external_id, fingerprint)

    @staticmethod
    def _updated_result(external_id: str, fingerprint: str) -> SleepCalendarResult:
        return SleepCalendarResult(
            action=SleepCalendarAction.UPDATED,
            external_id=external_id,
            observation_fingerprint=fingerprint,
        )

    def _lock_source_key(self, source_key: str) -> Connection | None:
        bind = self._session.get_bind()
        if bind.dialect.name != "postgresql":
            return None
        engine = bind.engine if isinstance(bind, Connection) else bind
        connection = engine.connect()
        try:
            connection.execute(
                sa.text("SELECT pg_advisory_lock(hashtextextended(:source_key, 0))"),
                {"source_key": f"{self._backend.source.value}:{source_key}"},
            )
        except BaseException:
            connection.close()
            raise
        return connection

    def _unlock_source_key(
        self,
        connection: Connection,
        source_key: str,
    ) -> None:
        try:
            released = connection.scalar(
                sa.text(
                    "SELECT pg_advisory_unlock(hashtextextended(:source_key, 0))"
                ),
                {"source_key": f"{self._backend.source.value}:{source_key}"},
            )
            if released is not True:
                raise RuntimeError("PostgreSQL source-key advisory lock was not held")
        finally:
            connection.close()
