from __future__ import annotations

from dataclasses import replace

from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from healthmes.calendars.base import (
    CalendarBackend,
    CalendarConflictError,
    CalendarEventIdentity,
    EventNotFoundError,
    HealthmesEventKind,
    ensure_utc,
)
from healthmes.calendars.planned_sleep_replacement import (
    delete_replaced_planned_sleep,
)
from healthmes.calendars.sleep_event_rendering import (
    ACTUAL_SLEEP_SUMMARY,
    description,
    event_draft,
    observation_fingerprint,
)
from healthmes.calendars.sleep_mirror import (
    SLEEP_CREATE_PENDING_STATUS,
    SLEEP_UPDATE_PENDING_STATUS,
    finalize_sleep_mirror,
    find_sleep_source_key,
    mark_sleep_update_pending,
    pending_sleep_mirror,
)
from healthmes.calendars.sleep_observation import ActualSleepObservation
from healthmes.calendars.sleep_reconciliation_guards import (
    assert_owned_actual_sleep,
    assert_pending_remote_matches,
    assert_remote_actual_sleep,
)
from healthmes.calendars.sleep_reconciliation_result import (
    SleepCalendarAction,
    SleepCalendarResult,
    created_sleep_result,
    updated_sleep_result,
)
from healthmes.calendars.sleep_source_lock import (
    lock_sleep_source_key,
    unlock_sleep_source_key,
)
from healthmes.store.models import CalendarEventMirror


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
        row = find_sleep_source_key(
            self._session,
            self._backend.source,
            identity.source_key,
        )
        if row is None:
            result = self._create(observation, identity, fingerprint)
            return self._replace_planned_sleep(result, observation)

        assert_owned_actual_sleep(row, identity)
        try:
            remote = self._backend.read_event(row.external_id)
        except EventNotFoundError:
            if row.status != SLEEP_CREATE_PENDING_STATUS:
                raise
            result = self._create_remote(row, observation, identity, fingerprint)
            return self._replace_planned_sleep(result, observation)

        if row.status == SLEEP_UPDATE_PENDING_STATUS:
            result = self._recover_pending_update(
                row,
                remote,
                observation,
                identity,
                fingerprint,
            )
            return self._replace_planned_sleep(result, observation)

        expected_etag = assert_remote_actual_sleep(
            remote,
            identity,
            row.etag,
        )
        if row.status == SLEEP_CREATE_PENDING_STATUS:
            assert_pending_remote_matches(remote, observation)
            finalize_sleep_mirror(
                self._session,
                row,
                remote,
                observation,
                fingerprint,
            )
            result = self._created_result(row.external_id, fingerprint)
            return self._replace_planned_sleep(result, observation)

        if row.observation_fingerprint == fingerprint:
            result = SleepCalendarResult(
                action=SleepCalendarAction.NOOP,
                external_id=row.external_id,
                observation_fingerprint=fingerprint,
            )
        else:
            result = self._update(row, observation, fingerprint, expected_etag)
        return self._replace_planned_sleep(result, observation)

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
        return created_sleep_result(row.external_id, fingerprint)

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
            return updated_sleep_result(row.external_id, fingerprint)
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
            summary=ACTUAL_SLEEP_SUMMARY,
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
        return updated_sleep_result(row.external_id, fingerprint)

    def _lock_source_key(self, source_key: str) -> Connection | None:
        return lock_sleep_source_key(
            self._session,
            self._backend.source,
            source_key,
        )

    def _unlock_source_key(
        self,
        connection: Connection,
        source_key: str,
    ) -> None:
        unlock_sleep_source_key(
            connection,
            self._backend.source,
            source_key,
        )
