from __future__ import annotations

import logging
from dataclasses import replace

import sqlalchemy as sa
from sqlalchemy.orm import Session

from healthmes.calendars.base import (
    CalendarBackend,
    CalendarConflictError,
    CalendarError,
    CalendarEventIdentity,
    EventNotFoundError,
    HealthmesEventKind,
    OwnershipError,
    calendar_identity_external_id,
    coerce_utc,
    ensure_utc,
    parse_calendar_identity,
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
    calendar_observations,
)
from healthmes.calendars.sleep_reconciliation_guards import (
    assert_owned_actual_sleep,
    assert_remote_actual_sleep,
    pending_remote_matches,
)
from healthmes.calendars.sleep_reconciliation_result import (
    SleepCalendarAction,
    SleepCalendarResult,
    created_sleep_result,
    updated_sleep_result,
)
from healthmes.calendars.write_lock import calendar_write_lock
from healthmes.store.enums import ProposalStatus
from healthmes.store.models import CalendarEventMirror, ScheduleProposal

logger = logging.getLogger(__name__)
LEGACY_SLEEP_CLEANUP_BATCH_SIZE = 25


class SleepCalendarWriteBlocked(RuntimeError):
    def __init__(
        self,
        *,
        reason: str,
        proposal_id: str,
        retryable: bool,
        invalidated_proposal_ids: tuple[str, ...] = (),
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.proposal_id = proposal_id
        self.retryable = retryable
        self.invalidated_proposal_ids = invalidated_proposal_ids


class SleepCalendarReconciler:
    def __init__(
        self,
        session: Session,
        backend: CalendarBackend,
        *,
        account_generation: str | None = None,
    ) -> None:
        self._session = session
        self._backend = backend
        self._account_generation = account_generation

    def _scope_current_generation(
        self,
        statement: sa.Select,
    ) -> sa.Select:
        if self._account_generation is None:
            return statement
        return statement.where(
            CalendarEventMirror.connection_generation
            == self._account_generation
        )

    def reconcile(self, observation: ActualSleepObservation) -> SleepCalendarResult:
        with calendar_write_lock(self._session, self._backend.source):
            self._session.expire_all()
            return self._reconcile_observation_locked(observation)

    def _reconcile_observation_locked(
        self,
        observation: ActualSleepObservation,
    ) -> SleepCalendarResult:
        invalidated_proposal_ids = (
            self._invalidate_overlapping_pushed_schedule_blocks(observation)
        )
        children = calendar_observations(observation)
        completed: list[tuple[ActualSleepObservation, SleepCalendarResult]] = []
        try:
            for child in children:
                completed.append((child, self._reconcile_one_locked(child)))
        except Exception:
            self._compensate_created_segments(completed)
            raise
        results = tuple(result for _, result in completed)
        stale = self._delete_stale_segments(
            observation,
            {child.source_key for child in children},
        )
        planned_deleted = tuple(
            dict.fromkeys(
                external_id
                for result in results
                for external_id in result.deleted_planned_external_ids
            )
        )
        action = (
            SleepCalendarAction.CREATED
            if any(result.action is SleepCalendarAction.CREATED for result in results)
            else SleepCalendarAction.UPDATED
            if any(result.action is SleepCalendarAction.UPDATED for result in results)
            or stale
            else SleepCalendarAction.NOOP
        )
        combined = SleepCalendarResult(
            action=action,
            external_id=results[0].external_id,
            external_ids=tuple(result.external_id for result in results),
            observation_fingerprint=observation_fingerprint(observation),
            deleted_planned_external_ids=planned_deleted,
            deleted_actual_external_ids=stale,
            planned_sleep_cleanup_pending=sum(
                result.planned_sleep_cleanup_pending for result in results
            ),
            invalidated_schedule_proposal_ids=invalidated_proposal_ids,
        )
        return self._replace_planned_sleep(combined, observation)

    def _reconcile_one_locked(
        self,
        observation: ActualSleepObservation,
    ) -> SleepCalendarResult:
        fingerprint = observation_fingerprint(observation)
        identity = actual_sleep_identity(observation)
        return self._reconcile_locked(observation, identity, fingerprint)

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
                    account_generation=self._account_generation,
                )
                canonical = canonical_actual_sleep_mirror(
                    rows,
                    actual_sleep_identity(observation),
                )
                if canonical is not None:
                    observation = sleep_observation_from_mirror(canonical)
            invalidated_proposal_ids = (
                self._invalidate_overlapping_pushed_schedule_blocks(observation)
            )
            result = self._reconcile_one_locked(observation)
            result = replace(
                result,
                invalidated_schedule_proposal_ids=invalidated_proposal_ids,
            )
            return self._replace_planned_sleep(result, observation)

    def _invalidate_overlapping_pushed_schedule_blocks(
        self,
        observation: ActualSleepObservation,
    ) -> tuple[str, ...]:
        start_at = ensure_utc(observation.start_at)
        end_at = ensure_utc(observation.end_at)
        proposals = list(
            self._session.scalars(
                sa.select(ScheduleProposal)
                .where(
                    ScheduleProposal.status == ProposalStatus.PUSHED,
                    ScheduleProposal.proposed_start < end_at,
                    ScheduleProposal.proposed_end > start_at,
                    sa.or_(
                        ScheduleProposal.healthmes_kind.is_(None),
                        ScheduleProposal.healthmes_kind
                        == HealthmesEventKind.SCHEDULE_BLOCK.value,
                    ),
                )
                .order_by(ScheduleProposal.proposed_start, ScheduleProposal.id)
            ).all()
        )
        invalidated: list[str] = []
        for proposal in proposals:
            proposal_id = str(proposal.id)
            identity = CalendarEventIdentity(
                kind=HealthmesEventKind.SCHEDULE_BLOCK,
                source="planner",
                source_key=f"proposal:{proposal.id}",
            )
            external_id = calendar_identity_external_id(
                self._backend.source,
                identity,
            )
            row = self._session.scalar(
                self._scope_current_generation(
                    sa.select(CalendarEventMirror).where(
                        CalendarEventMirror.calendar_source
                        == self._backend.source,
                        CalendarEventMirror.external_id == external_id,
                    )
                )
            )
            identity_row = self._session.scalar(
                self._scope_current_generation(
                    sa.select(CalendarEventMirror).where(
                        CalendarEventMirror.calendar_source
                        == self._backend.source,
                        CalendarEventMirror.healthmes_kind
                        == identity.kind.value,
                        CalendarEventMirror.healthmes_source
                        == identity.source,
                        CalendarEventMirror.healthmes_source_key
                        == identity.source_key,
                    )
                )
            )
            if (
                identity_row is not None
                and identity_row.external_id != external_id
            ) or (
                row is not None
                and (
                    not row.is_agent_created
                    or row.agent_task_id != proposal.task_id
                    or row.healthmes_kind != identity.kind.value
                    or row.healthmes_source != identity.source
                    or row.healthmes_source_key != identity.source_key
                    or coerce_utc(row.start_at)
                    != coerce_utc(proposal.proposed_start)
                    or coerce_utc(row.end_at)
                    != coerce_utc(proposal.proposed_end)
                )
            ) or self._has_unverifiable_owned_block(proposal):
                raise SleepCalendarWriteBlocked(
                    reason="overlapping_pushed_block_ownership_conflict",
                    proposal_id=proposal_id,
                    retryable=False,
                    invalidated_proposal_ids=tuple(invalidated),
                )

            if self._account_generation is not None and row is None:
                proposal.status = ProposalStatus.INVALIDATED
                self._session.commit()
                invalidated.append(proposal_id)
                logger.warning(
                    "Invalidated pushed proposal %s after calendar account "
                    "change without touching the current account.",
                    proposal.id,
                )
                continue

            try:
                remote = self._backend.read_event(external_id)
            except EventNotFoundError:
                remote = None
            except OwnershipError as exc:
                raise SleepCalendarWriteBlocked(
                    reason="overlapping_pushed_block_ownership_conflict",
                    proposal_id=proposal_id,
                    retryable=False,
                    invalidated_proposal_ids=tuple(invalidated),
                ) from exc
            except Exception as exc:
                raise SleepCalendarWriteBlocked(
                    reason="overlapping_pushed_block_cleanup_retry",
                    proposal_id=proposal_id,
                    retryable=True,
                    invalidated_proposal_ids=tuple(invalidated),
                ) from exc

            if remote is not None:
                if (
                    not remote.is_agent_created
                    or remote.identity != identity
                    or remote.external_id != external_id
                    or remote.agent_task_id != proposal.task_id
                    or remote.start_at != coerce_utc(proposal.proposed_start)
                    or remote.end_at != coerce_utc(proposal.proposed_end)
                ):
                    raise SleepCalendarWriteBlocked(
                        reason="overlapping_pushed_block_ownership_conflict",
                        proposal_id=proposal_id,
                        retryable=False,
                        invalidated_proposal_ids=tuple(invalidated),
                    )
                if remote.etag is None:
                    raise SleepCalendarWriteBlocked(
                        reason="overlapping_pushed_block_cleanup_retry",
                        proposal_id=proposal_id,
                        retryable=True,
                        invalidated_proposal_ids=tuple(invalidated),
                    )
                try:
                    self._backend.delete_event(
                        external_id,
                        expected_kind=HealthmesEventKind.SCHEDULE_BLOCK,
                        expected_etag=remote.etag,
                    )
                except EventNotFoundError:
                    pass
                except OwnershipError as exc:
                    raise SleepCalendarWriteBlocked(
                        reason="overlapping_pushed_block_ownership_conflict",
                        proposal_id=proposal_id,
                        retryable=False,
                        invalidated_proposal_ids=tuple(invalidated),
                    ) from exc
                except Exception as exc:
                    raise SleepCalendarWriteBlocked(
                        reason="overlapping_pushed_block_cleanup_retry",
                        proposal_id=proposal_id,
                        retryable=True,
                        invalidated_proposal_ids=tuple(invalidated),
                    ) from exc

            try:
                if row is not None:
                    self._session.delete(row)
                proposal.status = ProposalStatus.INVALIDATED
                self._session.commit()
            except Exception as exc:
                self._session.rollback()
                raise SleepCalendarWriteBlocked(
                    reason="overlapping_pushed_block_cleanup_retry",
                    proposal_id=proposal_id,
                    retryable=True,
                    invalidated_proposal_ids=tuple(invalidated),
                ) from exc
            invalidated.append(proposal_id)
            logger.warning(
                "Invalidated pushed proposal %s because actual sleep overlaps "
                "its exact owned calendar block.",
                proposal.id,
            )
        return tuple(invalidated)

    def _has_unverifiable_owned_block(
        self,
        proposal: ScheduleProposal,
    ) -> bool:
        candidates = self._session.scalars(
            self._scope_current_generation(
                sa.select(CalendarEventMirror).where(
                    CalendarEventMirror.calendar_source
                    == self._backend.source,
                    CalendarEventMirror.is_agent_created.is_(True),
                    CalendarEventMirror.agent_task_id == proposal.task_id,
                )
            )
        ).all()
        start_at = coerce_utc(proposal.proposed_start)
        end_at = coerce_utc(proposal.proposed_end)
        for candidate in candidates:
            if (
                coerce_utc(candidate.start_at) != start_at
                or coerce_utc(candidate.end_at) != end_at
            ):
                continue
            candidate_identity = parse_calendar_identity(
                candidate.healthmes_kind,
                candidate.healthmes_source,
                candidate.healthmes_source_key,
            )
            if candidate_identity is None:
                return True
            if candidate.external_id != calendar_identity_external_id(
                self._backend.source,
                candidate_identity,
            ):
                return True
        return False

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
                self._scope_current_generation(
                    sa.select(CalendarEventMirror)
                    .where(
                        CalendarEventMirror.calendar_source
                        == self._backend.source,
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

    def _delete_stale_segments(
        self,
        observation: ActualSleepObservation,
        current_keys: set[str],
    ) -> tuple[str, ...]:
        rows = self._session.scalars(
            self._scope_current_generation(
                sa.select(CalendarEventMirror).where(
                    CalendarEventMirror.calendar_source
                    == self._backend.source,
                    CalendarEventMirror.is_agent_created.is_(True),
                    CalendarEventMirror.healthmes_kind
                    == HealthmesEventKind.ACTUAL_SLEEP.value,
                    CalendarEventMirror.sleep_local_date
                    == observation.local_date,
                    CalendarEventMirror.healthmes_source_key.like(
                        f"{observation.source_key}:segment:%"
                    ),
                    CalendarEventMirror.healthmes_source_key.not_in(
                        current_keys
                    ),
                )
            )
        ).all()
        deleted: list[str] = []
        for row in rows:
            identity = actual_sleep_identity_from_mirror(row)
            if identity is None:
                raise OwnershipError(
                    f"{row.calendar_source.value} event {row.external_id!r} "
                    "has invalid actual_sleep identity"
                )
            try:
                remote = self._backend.read_event(row.external_id)
                expected_etag = assert_remote_actual_sleep(
                    remote,
                    self._backend.source,
                    identity,
                    row.etag,
                )
                self._backend.delete_event(
                    row.external_id,
                    expected_kind=HealthmesEventKind.ACTUAL_SLEEP,
                    expected_etag=expected_etag,
                )
            except EventNotFoundError:
                pass
            deleted.append(row.external_id)
            self._session.delete(row)
        if deleted:
            self._session.commit()
        return tuple(deleted)

    def _compensate_created_segments(
        self,
        completed: list[tuple[ActualSleepObservation, SleepCalendarResult]],
    ) -> None:
        for child, result in reversed(completed):
            if result.action is not SleepCalendarAction.CREATED:
                continue
            identity = actual_sleep_identity(child)
            try:
                remote = self._backend.read_event(result.external_id)
                expected_etag = assert_remote_actual_sleep(
                    remote,
                    self._backend.source,
                    identity,
                    None,
                )
                if expected_etag is None or not pending_remote_matches(remote, child):
                    continue
                self._backend.delete_event(
                    result.external_id,
                    expected_kind=HealthmesEventKind.ACTUAL_SLEEP,
                    expected_etag=expected_etag,
                )
                row = self._session.scalar(
                    self._scope_current_generation(
                        sa.select(CalendarEventMirror).where(
                            CalendarEventMirror.calendar_source
                            == self._backend.source,
                            CalendarEventMirror.external_id
                            == result.external_id,
                            CalendarEventMirror.healthmes_kind
                            == identity.kind.value,
                            CalendarEventMirror.healthmes_source
                            == identity.source,
                            CalendarEventMirror.healthmes_source_key
                            == identity.source_key,
                        )
                    )
                )
                if row is not None:
                    self._session.delete(row)
                self._session.commit()
            except Exception:
                self._session.rollback()
                logger.exception(
                    "Failed to compensate partial split-sleep create for %s",
                    child.source_key,
                )

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
            account_generation=self._account_generation,
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
                    account_generation=self._account_generation,
                )
                result = created_sleep_result(
                    row.external_id,
                    pending_fingerprint,
                )
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
        return result

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
            account_generation=self._account_generation,
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
        remote = None
        if existing is not None:
            stale_generation = (
                self._account_generation is not None
                and existing.connection_generation
                != self._account_generation
            )
            try:
                remote = self._backend.read_event(external_id)
            except EventNotFoundError:
                if not stale_generation:
                    raise
                self._session.delete(existing)
                self._session.commit()
                existing = None
            if existing is not None:
                assert remote is not None
                expected_etag = assert_remote_actual_sleep(
                    remote,
                    self._backend.source,
                    identity,
                    None if stale_generation else existing.etag,
                )
                adopt_remote_actual_sleep(
                    existing,
                    remote,
                    identity,
                    account_generation=self._account_generation,
                )
                self._session.commit()
                if pending_remote_matches(remote, observation):
                    finalize_sleep_mirror(
                        self._session,
                        existing,
                        remote,
                        observation,
                        fingerprint,
                        account_generation=self._account_generation,
                    )
                    return self._noop_result(external_id, fingerprint)
                return self._update(
                    existing,
                    observation,
                    fingerprint,
                    expected_etag,
                )

        self._quarantine_stale_identity_conflicts(
            identity,
            expected_external_id=external_id,
        )
        row = pending_sleep_mirror(
            self._backend.source,
            observation,
            identity,
            fingerprint,
            account_generation=self._account_generation,
        )
        self._session.add(row)
        self._session.commit()
        return self._create_remote(row, observation, identity, fingerprint)

    def _quarantine_stale_identity_conflicts(
        self,
        identity: CalendarEventIdentity,
        *,
        expected_external_id: str,
    ) -> None:
        if self._account_generation is None:
            return
        rows = self._session.scalars(
            sa.select(CalendarEventMirror).where(
                CalendarEventMirror.calendar_source
                == self._backend.source,
                CalendarEventMirror.healthmes_kind
                == identity.kind.value,
                CalendarEventMirror.healthmes_source == identity.source,
                CalendarEventMirror.healthmes_source_key
                == identity.source_key,
                CalendarEventMirror.connection_generation
                != self._account_generation,
            )
        ).all()
        changed = False
        for row in rows:
            if row.external_id == expected_external_id:
                self._session.delete(row)
            else:
                quarantine_sleep_identity(row)
            changed = True
        if changed:
            self._session.commit()

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
        except CalendarError as error:
            try:
                created = self._backend.read_event(row.external_id)
            except EventNotFoundError:
                raise error
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
            account_generation=self._account_generation,
        )
        return created_sleep_result(row.external_id, fingerprint)

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
            account_generation=self._account_generation,
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
        if pending_remote_matches(remote, observation):
            finalize_sleep_mirror(
                self._session,
                row,
                remote,
                observation,
                fingerprint,
                account_generation=self._account_generation,
            )
            return updated_sleep_result(row.external_id, fingerprint)
        if row.etag is not None and remote_etag != row.etag:
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
            summary=ACTUAL_SLEEP_SUMMARY,
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
            account_generation=self._account_generation,
        )
        return updated_sleep_result(row.external_id, fingerprint)
