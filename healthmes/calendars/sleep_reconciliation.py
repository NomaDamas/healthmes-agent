from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import Session

from healthmes.calendars.base import (
    CalendarBackend,
    CalendarConflictError,
    CalendarEventIdentity,
    EventNotFoundError,
    HealthmesEventKind,
    OwnershipError,
    calendar_identity_external_id,
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
    adopt_remote_actual_sleep,
    canonical_actual_sleep_mirror,
    finalize_sleep_mirror,
    find_actual_sleep_mirrors,
    mark_sleep_update_pending,
    pending_sleep_mirror,
    pending_sleep_observation,
    quarantine_sleep_identity,
    sleep_observation_from_mirror,
)
from healthmes.calendars.sleep_observation import (
    ACTUAL_SLEEP_IDENTITY_SOURCE,
    ActualSleepObservation,
    actual_sleep_source_key,
)
from healthmes.calendars.sleep_reconciliation_guards import (
    assert_owned_actual_sleep,
    assert_remote_actual_sleep,
    pending_remote_matches,
)
from healthmes.calendars.write_lock import calendar_write_lock
from healthmes.store.models import CalendarEventMirror

logger = logging.getLogger(__name__)
LEGACY_SLEEP_CLEANUP_BATCH_SIZE = 25


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
        return self._reconcile_with_lock(
            observation,
            prefer_current_canonical=False,
        )

    def _reconcile_with_lock(
        self,
        observation: ActualSleepObservation,
        *,
        prefer_current_canonical: bool,
    ) -> SleepCalendarResult:
        with calendar_write_lock(self._session, self._backend.source):
            self._session.expire_all()
            if prefer_current_canonical:
                rows = find_actual_sleep_mirrors(
                    self._session,
                    self._backend.source,
                    observation,
                )
                canonical = canonical_actual_sleep_mirror(
                    rows,
                    actual_sleep_identity(observation),
                )
                if canonical is not None:
                    observation = sleep_observation_from_mirror(canonical)
            fingerprint = observation_fingerprint(observation)
            identity = actual_sleep_identity(observation)
            return self._reconcile_locked(observation, identity, fingerprint)

    def reconcile_legacy_history(
        self,
        *,
        limit: int = LEGACY_SLEEP_CLEANUP_BATCH_SIZE,
    ) -> dict[str, int]:
        """Eventually replace provider-specific actual-sleep events.

        Only a currently owned remote event with the exact deterministic
        legacy identity can seed a canonical replacement. Copied identities
        are quarantined locally and never deleted remotely.
        """

        rows = list(
            self._session.scalars(
                sa.select(CalendarEventMirror)
                .where(
                    CalendarEventMirror.calendar_source == self._backend.source,
                    CalendarEventMirror.is_agent_created.is_(True),
                    CalendarEventMirror.healthmes_kind
                    == HealthmesEventKind.ACTUAL_SLEEP.value,
                    CalendarEventMirror.sleep_local_date.is_not(None),
                )
                .order_by(
                    CalendarEventMirror.sleep_local_date,
                    CalendarEventMirror.updated_at.desc(),
                    CalendarEventMirror.id,
                )
            ).all()
        )
        candidates: dict[object, ActualSleepObservation] = {}
        canonical_observations: dict[object, ActualSleepObservation] = {}
        blocked_dates: set[object] = set()
        quarantined = 0
        removed_missing = 0
        failed = 0

        for row in rows:
            identity = actual_sleep_identity_from_mirror(row)
            if identity is None or row.sleep_local_date is None:
                continue
            expected_identity = CalendarEventIdentity(
                kind=HealthmesEventKind.ACTUAL_SLEEP,
                source=ACTUAL_SLEEP_IDENTITY_SOURCE,
                source_key=actual_sleep_source_key(row.sleep_local_date),
            )
            if (
                identity != expected_identity
                or row.external_id
                != calendar_identity_external_id(
                    self._backend.source,
                    expected_identity,
                )
            ):
                continue
            try:
                canonical_observations[row.sleep_local_date] = (
                    sleep_observation_from_mirror(row)
                )
            except RuntimeError:
                blocked_dates.add(row.sleep_local_date)
                failed += 1

        for row in rows:
            identity = actual_sleep_identity_from_mirror(row)
            if identity is None or row.sleep_local_date is None:
                continue
            canonical = CalendarEventIdentity(
                kind=HealthmesEventKind.ACTUAL_SLEEP,
                source=ACTUAL_SLEEP_IDENTITY_SOURCE,
                source_key=actual_sleep_source_key(row.sleep_local_date),
            )
            expected_external_id = calendar_identity_external_id(
                self._backend.source,
                identity,
            )
            already_canonical = (
                identity == canonical and row.external_id == expected_external_id
            )
            if already_canonical:
                continue
            if row.external_id != expected_external_id:
                quarantine_sleep_identity(row)
                self._session.commit()
                quarantined += 1
                continue
            try:
                remote = self._backend.read_event(row.external_id)
                assert_remote_actual_sleep(
                    remote,
                    self._backend.source,
                    identity,
                    row.etag,
                )
            except EventNotFoundError:
                self._session.delete(row)
                self._session.commit()
                removed_missing += 1
                continue
            except Exception:
                self._session.rollback()
                failed += 1
                logger.exception(
                    "Legacy actual-sleep validation failed for %s",
                    row.external_id,
                )
                continue
            assert remote.start_at is not None and remote.end_at is not None
            row.start_at = remote.start_at
            row.end_at = remote.end_at
            row.etag = remote.etag
            row.summary = remote.summary or row.summary
            self._session.commit()
            if row.sleep_local_date in blocked_dates:
                continue
            stored_observation = canonical_observations.get(row.sleep_local_date)
            if stored_observation is None:
                try:
                    stored_observation = sleep_observation_from_mirror(row)
                except RuntimeError:
                    failed += 1
                    continue
            candidates.setdefault(
                row.sleep_local_date,
                stored_observation,
            )

        migrated = 0
        for observation in candidates.values():
            if migrated >= limit:
                break
            try:
                self._reconcile_with_lock(
                    observation,
                    prefer_current_canonical=(
                        observation.local_date in canonical_observations
                    ),
                )
                migrated += 1
            except Exception:
                self._session.rollback()
                failed += 1
                logger.exception(
                    "Legacy actual-sleep canonicalization failed for %s",
                    observation.local_date,
                )
        return {
            "migrated": migrated,
            "quarantined": quarantined,
            "removed_missing": removed_missing,
            "failed": failed,
        }

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
        row = canonical_actual_sleep_mirror(rows, identity)
        duplicates = list(rows)
        if row is None:
            self._quarantine_untrusted_identity_copies(duplicates)
            duplicates = [
                duplicate
                for duplicate in duplicates
                if duplicate.healthmes_source_key is not None
            ]
            self._validate_duplicate_actual_sleep_mirrors(duplicates)
            result = self._create(observation, identity, fingerprint)
            return self._finish(result, observation, duplicates)

        duplicates.remove(row)
        self._quarantine_untrusted_identity_copies(duplicates)
        duplicates = [
            duplicate
            for duplicate in duplicates
            if duplicate.healthmes_source_key is not None
        ]
        assert_owned_actual_sleep(row, identity)
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
            self._backend.source,
            identity,
            row.etag,
        )
        if row.status == SLEEP_CREATE_PENDING_STATUS:
            assert pending_observation is not None
            assert pending_fingerprint is not None
            if pending_remote_matches(remote, pending_observation):
                finalize_sleep_mirror(
                    self._session,
                    row,
                    remote,
                    pending_observation,
                    pending_fingerprint,
                )
                result = self._created_result(row.external_id, pending_fingerprint)
            else:
                result = self._update_remote(
                    row,
                    pending_observation,
                    pending_fingerprint,
                    expected_etag,
                )
            result = self._apply_latest_after_recovery(
                row,
                observation,
                fingerprint,
                result,
            )
            return self._finish(result, observation, duplicates)

        if row.observation_fingerprint == fingerprint:
            if pending_remote_matches(remote, observation):
                result = SleepCalendarResult(
                    action=SleepCalendarAction.NOOP,
                    external_id=row.external_id,
                    observation_fingerprint=fingerprint,
                )
            else:
                result = self._update(
                    row,
                    observation,
                    fingerprint,
                    expected_etag,
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

    def _quarantine_untrusted_identity_copies(
        self,
        rows: list[CalendarEventMirror],
    ) -> None:
        changed = False
        for row in rows:
            identity = actual_sleep_identity_from_mirror(row)
            if identity is None or row.external_id != calendar_identity_external_id(
                self._backend.source,
                identity,
            ):
                quarantine_sleep_identity(row)
                changed = True
        if changed:
            self._session.commit()

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
            if duplicate.external_id != calendar_identity_external_id(
                duplicate.calendar_source,
                identity,
            ):
                quarantine_sleep_identity(duplicate)
                self._session.commit()
                continue
            try:
                remote = self._backend.read_event(duplicate.external_id)
                expected_etag = assert_remote_actual_sleep(
                    remote,
                    self._backend.source,
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

    def _validate_duplicate_actual_sleep_mirrors(
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
            if duplicate.external_id != calendar_identity_external_id(
                duplicate.calendar_source,
                identity,
            ):
                continue
            try:
                remote = self._backend.read_event(duplicate.external_id)
            except EventNotFoundError:
                continue
            assert_remote_actual_sleep(
                remote,
                self._backend.source,
                identity,
                duplicate.etag,
            )

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
        external_id = calendar_identity_external_id(
            self._backend.source,
            identity,
        )
        existing = self._session.scalar(
            sa.select(CalendarEventMirror).where(
                CalendarEventMirror.calendar_source == self._backend.source,
                CalendarEventMirror.external_id == external_id,
            )
        )
        if existing is not None:
            remote = self._backend.read_event(external_id)
            expected_etag = assert_remote_actual_sleep(
                remote,
                self._backend.source,
                identity,
                None,
            )
            adopt_remote_actual_sleep(existing, remote, identity)
            self._session.commit()
            if pending_remote_matches(remote, observation):
                finalize_sleep_mirror(
                    self._session,
                    existing,
                    remote,
                    observation,
                    fingerprint,
                )
                return self._noop_result(external_id, fingerprint)
            return self._update(
                existing,
                observation,
                fingerprint,
                expected_etag,
            )

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
            expected_etag = assert_remote_actual_sleep(
                created,
                self._backend.source,
                identity,
                None,
            )
            if not pending_remote_matches(created, observation):
                row.etag = expected_etag
                self._session.commit()
                return self._update_remote(
                    row,
                    observation,
                    fingerprint,
                    expected_etag,
                )
        assert_remote_actual_sleep(
            created,
            self._backend.source,
            identity,
            None,
        )
        if not pending_remote_matches(created, observation):
            raise CalendarConflictError(
                "provider returned actual_sleep content that differs from intent"
            )
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

    @staticmethod
    def _noop_result(external_id: str, fingerprint: str) -> SleepCalendarResult:
        return SleepCalendarResult(
            action=SleepCalendarAction.NOOP,
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
        remote_etag = assert_remote_actual_sleep(
            remote,
            self._backend.source,
            identity,
            None,
        )
        if row.etag is not None and remote_etag != row.etag:
            if pending_remote_matches(remote, observation):
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
                remote_etag,
            )
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
        identity = actual_sleep_identity_from_mirror(row)
        if identity is None:
            raise OwnershipError(
                f"{row.calendar_source.value} event {row.external_id!r} "
                "lost its actual_sleep identity during update"
            )
        assert_remote_actual_sleep(
            updated,
            self._backend.source,
            identity,
            None,
        )
        if not pending_remote_matches(updated, observation):
            raise CalendarConflictError(
                "provider returned actual_sleep content that differs from intent"
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
