"""Calendar poll jobs: mirror sync + accepted-proposal push (docs/PLAN.md §6).

This is the production entry point of the calendar plane — the piece that
turns the (fully tested) backend/service library into running behavior:

- :func:`build_calendar_jobs` returns one :class:`CalendarJobSpec` per
  *enabled* backend, polling at ``google_poll_minutes`` /
  ``caldav_poll_minutes`` (PLAN §6: 5 / 10 minutes). A backend is enabled by
  its settings flag (``Settings.google_calendar_enabled`` /
  ``Settings.caldav_enabled``) OR by a runtime connection established with
  ``healthmes connect`` — i.e. the token/creds file under ``Settings.data_dir``
  exists (healthmes/calendars/creds.py) — so connecting via the CLI needs no
  ``.env`` edit. The app lifespan registers each spec on the in-process
  scheduler.
- Every run syncs that backend into ``calendar_event_mirror`` (the trigger
  sweep's ``schedule_changed`` rule and the energy engine's meeting-load
  factor read the mirror; the sync itself is enough — no push here).
- The **write backend** (Google when enabled, else CalDAV — one designated
  writer so the same block is never created twice) additionally pushes
  ``accepted`` schedule proposals to the external calendar as tagged agent
  blocks and advances them to ``pushed`` — the contract promised by
  ``healthmes/api/schedule.py`` and skills/healthmes-planner/SKILL.md
  ("blocks are written to the calendar only after the user confirms").

Backends are constructed lazily on the first run (credentials are runtime
state, docs/PLAN.md §6); every failure is contained per run so a broken
credential can never take down the scheduler loop.
"""

import logging
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from healthmes.calendars import creds
from healthmes.calendars.base import (
    CalendarAuthError,
    CalendarBackend,
    CalendarConflictError,
    CalendarError,
    CalendarEventIdentity,
    EventDraft,
    EventNotFoundError,
    HealthmesEventKind,
    OwnershipError,
    calendar_identity_external_id,
    coerce_utc,
)
from healthmes.calendars.connection import CalendarBackendFence
from healthmes.calendars.intake import (
    intake_calendar_tasks,
    intake_revision,
    is_intake_eligible,
)
from healthmes.calendars.sleep_context import actual_sleep_violation
from healthmes.calendars.state import (
    FilePendingDiffStore,
    FileSyncHealthStore,
    FileSyncStateStore,
    PendingDiffStore,
    SyncCoverageKind,
    SyncHealthStore,
    SyncStateStore,
    sync_state_coverage,
)
from healthmes.calendars.sync import CalendarMirrorService, SyncDiff
from healthmes.calendars.write_lock import calendar_write_lock
from healthmes.config import Settings, resolve_timezone
from healthmes.store.enums import CalendarSource, ProposalStatus
from healthmes.store.models import CalendarEventMirror, ScheduleProposal, Task
from healthmes.store.session import session_scope

__all__ = [
    "CalendarJobSpec",
    "build_calendar_jobs",
    "calendar_job_id",
    "connected_sources",
    "enabled_sources",
    "push_accepted_proposals",
    "write_source",
]

logger = logging.getLogger(__name__)

_INTAKE_ACCOUNT_CHANGED = "calendar_intake_account_changed"
_INTAKE_EVENT_CHANGED = "calendar_intake_event_changed"
_INTAKE_IDENTITY_INVALID = "calendar_intake_identity_invalid"


@dataclass(frozen=True, slots=True)
class CalendarJobSpec:
    """One scheduler registration: a zero-arg poll job plus its cadence."""

    source: CalendarSource
    job_id: str
    interval_minutes: int
    job: Callable[[], SyncDiff | None]


def calendar_job_id(source: CalendarSource) -> str:
    return f"healthmes-calendar-{source.value}"


def connected_sources(settings: Settings) -> tuple[CalendarSource, ...]:
    """Calendar sources with currently usable local credentials.

    Unlike :func:`enabled_sources`, this deliberately ignores force-enable
    flags. A scheduler may keep trying an unconfigured backend, but decision
    reads must not treat retained mirror rows as owner-authorized after the
    corresponding credentials have been removed.
    """

    sources: list[CalendarSource] = []
    if creds.google_connected(settings.data_dir):
        sources.append(CalendarSource.GOOGLE)
    if creds.resolve_caldav_credentials(settings) is not None:
        sources.append(CalendarSource.CALDAV)
    return tuple(sources)


def enabled_sources(settings: Settings) -> tuple[CalendarSource, ...]:
    """Backends enabled by settings OR connected via ``healthmes connect``.

    Write-preference order (Google first). "Connected" means the runtime
    token/creds file under ``Settings.data_dir`` exists and is usable
    (healthmes/calendars/creds.py) — establishing a connection with the CLI
    is enough, no ``.env`` edit required. The settings flags keep working
    and force a backend on even without a stored file (its poll then fails
    per-run until credentials appear, exactly as before).
    """
    connected = set(connected_sources(settings))
    sources: list[CalendarSource] = []
    if settings.google_calendar_enabled or CalendarSource.GOOGLE in connected:
        sources.append(CalendarSource.GOOGLE)
    if settings.caldav_enabled or CalendarSource.CALDAV in connected:
        sources.append(CalendarSource.CALDAV)
    return tuple(sources)


def write_source(settings: Settings) -> CalendarSource | None:
    """The single backend agent blocks are written to (None: nothing enabled)."""
    sources = enabled_sources(settings)
    return sources[0] if sources else None


def _build_backend(settings: Settings, source: CalendarSource) -> CalendarBackend:
    """Construct a live backend from settings (deferred imports keep this
    module import-light; credentials are read here, at runtime only)."""
    if source is CalendarSource.GOOGLE:
        from healthmes.calendars.google import GoogleCalendarBackend

        return GoogleCalendarBackend.from_data_dir(
            settings.data_dir, settings.google_calendar_id, interactive=False
        )
    from healthmes.calendars.caldav_icloud import CalDavCalendarBackend

    resolved = creds.resolve_caldav_credentials(settings)
    if resolved is None:
        raise CalendarAuthError(
            "no CalDAV credentials: set HEALTHMES_CALDAV_USERNAME + "
            "HEALTHMES_CALDAV_APP_PASSWORD, or run `healthmes connect icloud "
            "--username <apple-id>` once"
        )
    return CalDavCalendarBackend.connect(
        username=resolved.username,
        app_password=resolved.app_password,
        url=resolved.url,
        calendar_name=settings.caldav_calendar_name,
    )


def _accepted_proposal_ids(session: Session) -> list[UUID]:
    statement = (
        select(ScheduleProposal.id)
        .join(Task, ScheduleProposal.task_id == Task.id)
        .where(ScheduleProposal.status == ProposalStatus.ACCEPTED)
        .order_by(ScheduleProposal.proposed_start)
    )
    bind = session.get_bind()
    if isinstance(bind, Connection):
        return list(bind.scalars(statement))
    assert isinstance(bind, Engine)
    with bind.connect() as connection:
        return list(connection.scalars(statement))


def _existing_agent_block(
    session: Session,
    source: CalendarSource,
    proposal: ScheduleProposal,
    *,
    account_generation: str | None = None,
) -> CalendarEventMirror | None:
    """Return the trusted agent mirror row already written for this proposal.

    The proposal UUID is part of the deterministic remote identity, so another
    proposal for the same task and time can never be mistaken for this block.
    Times are compared in Python via ``coerce_utc`` because sqlite round-trips
    ``DateTime`` columns as naive UTC.
    """
    identity = _proposal_identity(proposal)
    statement = select(CalendarEventMirror).where(
        CalendarEventMirror.calendar_source == source,
        CalendarEventMirror.is_agent_created.is_(True),
        CalendarEventMirror.healthmes_kind == identity.kind.value,
        CalendarEventMirror.healthmes_source == identity.source,
        CalendarEventMirror.healthmes_source_key == identity.source_key,
    )
    if account_generation is not None:
        statement = statement.where(
            CalendarEventMirror.connection_generation
            == account_generation
        )
    row = session.scalar(statement)
    if row is None:
        return None
    if row.external_id != calendar_identity_external_id(source, identity):
        return None
    start = coerce_utc(proposal.proposed_start)
    end = coerce_utc(proposal.proposed_end)
    if (
        row.agent_task_id != proposal.task_id
        or coerce_utc(row.start_at) != start
        or coerce_utc(row.end_at) != end
    ):
        raise CalendarConflictError(
            f"proposal {proposal.id} owns a calendar block with different content"
        )
    return row


def _legacy_agent_block(
    session: Session,
    source: CalendarSource,
    proposal: ScheduleProposal,
    task: Task,
    *,
    account_generation: str | None = None,
) -> CalendarEventMirror | None:
    """Find one unambiguous block created before proposal identities existed."""
    if proposal.healthmes_kind == HealthmesEventKind.PLANNED_SLEEP.value:
        return None
    statement = select(CalendarEventMirror).where(
        CalendarEventMirror.calendar_source == source,
        CalendarEventMirror.agent_task_id == task.id,
        CalendarEventMirror.is_agent_created.is_(True),
        CalendarEventMirror.healthmes_kind.is_(None),
        CalendarEventMirror.healthmes_source.is_(None),
        CalendarEventMirror.healthmes_source_key.is_(None),
    )
    if account_generation is not None:
        statement = statement.where(
            CalendarEventMirror.connection_generation
            == account_generation
        )
    candidates = list(session.scalars(statement).all())
    start = coerce_utc(proposal.proposed_start)
    end = coerce_utc(proposal.proposed_end)
    matching = [
        row
        for row in candidates
        if coerce_utc(row.start_at) == start and coerce_utc(row.end_at) == end
    ]
    if not matching:
        return None
    if len(matching) != 1:
        raise CalendarConflictError(
            f"proposal {proposal.id} has ambiguous legacy calendar blocks"
        )
    competing = list(
        session.scalars(
            select(ScheduleProposal).where(
                ScheduleProposal.id != proposal.id,
                ScheduleProposal.task_id == proposal.task_id,
                ScheduleProposal.status.in_(
                    [ProposalStatus.ACCEPTED, ProposalStatus.PUSHED]
                ),
            )
        ).all()
    )
    if any(
        coerce_utc(other.proposed_start) == start
        and coerce_utc(other.proposed_end) == end
        for other in competing
    ):
        raise CalendarConflictError(
            f"proposal {proposal.id} cannot uniquely claim its legacy block"
        )
    return matching[0]


def _proposal_identity(proposal: ScheduleProposal) -> CalendarEventIdentity:
    kind = (
        HealthmesEventKind.PLANNED_SLEEP
        if proposal.healthmes_kind == HealthmesEventKind.PLANNED_SLEEP.value
        else HealthmesEventKind.SCHEDULE_BLOCK
    )
    return CalendarEventIdentity(
        kind=kind,
        source="planner",
        source_key=f"proposal:{proposal.id}",
    )


def _timed_intake_block(
    session: Session,
    proposal: ScheduleProposal,
    *,
    current_account_generations: (
        Mapping[CalendarSource, str] | None
    ) = None,
) -> CalendarEventMirror | None:
    """Return the exact externally-owned timed intake event captured by a proposal."""
    fields = (
        proposal.intake_calendar_source,
        proposal.intake_account_generation,
        proposal.intake_external_id,
        proposal.intake_revision,
    )
    if fields == (None, None, None, None):
        return None
    if any(value is None for value in fields):
        raise CalendarConflictError(
            _INTAKE_IDENTITY_INVALID
        )
    assert proposal.intake_calendar_source is not None
    assert proposal.intake_account_generation is not None
    if current_account_generations is not None:
        current_generation = current_account_generations.get(
            proposal.intake_calendar_source
        )
        if current_generation != proposal.intake_account_generation:
            raise CalendarConflictError(_INTAKE_ACCOUNT_CHANGED)
    statement = select(CalendarEventMirror).where(
        CalendarEventMirror.calendar_source
        == proposal.intake_calendar_source,
        CalendarEventMirror.connection_generation
        == proposal.intake_account_generation,
        CalendarEventMirror.external_id == proposal.intake_external_id,
    )
    row = session.scalar(statement.with_for_update())
    if (
        row is None
        or row.intake_task_id != proposal.task_id
        or row.is_all_day
        or not is_intake_eligible(row)
        or intake_revision(row) != proposal.intake_revision
    ):
        raise CalendarConflictError(_INTAKE_EVENT_CHANGED)
    start = coerce_utc(proposal.proposed_start)
    end = coerce_utc(proposal.proposed_end)
    if coerce_utc(row.start_at) != start or coerce_utc(row.end_at) != end:
        raise CalendarConflictError(_INTAKE_EVENT_CHANGED)
    return row


def push_accepted_proposals(
    service: CalendarMirrorService,
    session: Session,
    source: CalendarSource,
    timezone: tzinfo = UTC,
    *,
    current_account_generations: (
        Mapping[CalendarSource, str] | None
    ) = None,
) -> int:
    """Write every ``accepted`` proposal to the calendar; advance to ``pushed``.

    Each proposal is pushed independently: the remote create commits the
    mirror row first, then the status flips to ``pushed`` and commits. A crash
    between the two leaves the proposal ``accepted`` — but the next poll now
    detects the already-written agent block and reuses it instead of creating a
    duplicate, so the retry is idempotent (never a second remote event, never a
    lost one). A failing backend call leaves the proposal untouched for retry.
    """
    pushed = 0
    account_generations = (
        {source: service.account_generation}
        if service.account_generation is not None
        else None
    )
    # Do not hold a Session read transaction while waiting for the provider
    # write lock. That can deadlock SQLite commits and needlessly consume a
    # PostgreSQL pool connection while the advisory-lock connection waits.
    proposal_ids = _accepted_proposal_ids(session)
    for proposal_id in proposal_ids:
        with ExitStack() as locks:
            locks.enter_context(calendar_write_lock(session, source))
            session.expire_all()
            proposal = session.get(ScheduleProposal, proposal_id)
            if proposal is None or proposal.status is not ProposalStatus.ACCEPTED:
                continue
            if (
                proposal.intake_calendar_source is not None
                and proposal.intake_calendar_source is not source
            ):
                locks.enter_context(
                    calendar_write_lock(
                        session,
                        proposal.intake_calendar_source,
                    )
                )
            task = session.get(Task, proposal.task_id)
            if task is None:
                continue
            try:
                intake_row = _timed_intake_block(
                    session,
                    proposal,
                    current_account_generations=(
                        current_account_generations
                    ),
                )
            except CalendarConflictError as exc:
                proposal.status = ProposalStatus.INVALIDATED
                proposal.invalidation_reason = (
                    str(exc)
                    if str(exc)
                    in {
                        _INTAKE_ACCOUNT_CHANGED,
                        _INTAKE_EVENT_CHANGED,
                        _INTAKE_IDENTITY_INVALID,
                    }
                    else _INTAKE_EVENT_CHANGED
                )
                session.commit()
                logger.warning(
                    "Proposal %s invalidated because its timed intake event "
                    "cannot be safely adopted: %s",
                    proposal.id,
                    exc,
                )
                continue
            identity = _proposal_identity(proposal)
            draft = EventDraft(
                summary=task.title,
                start_at=coerce_utc(proposal.proposed_start),
                end_at=coerce_utc(proposal.proposed_end),
                agent_task_id=task.id,
                identity=identity,
            )
            try:
                row = _existing_agent_block(
                    session,
                    source,
                    proposal,
                    account_generation=service.account_generation,
                )
                legacy_row = (
                    _legacy_agent_block(
                        session,
                        source,
                        proposal,
                        task,
                        account_generation=service.account_generation,
                    )
                    if row is None
                    else None
                )
                if legacy_row is not None:
                    service.assert_legacy_agent_event_matches(
                        source,
                        legacy_row,
                        EventDraft(
                            summary=draft.summary,
                            start_at=draft.start_at,
                            end_at=draft.end_at,
                            description=draft.description,
                            agent_task_id=draft.agent_task_id,
                        ),
                    )
            except (CalendarConflictError, OwnershipError):
                logger.exception(
                    "Proposal %s has a conflicting owned calendar identity; "
                    "leaving it accepted for review.",
                    proposal.id,
                )
                continue
            violation = actual_sleep_violation(
                session,
                coerce_utc(proposal.proposed_start),
                coerce_utc(proposal.proposed_end),
                timezone,
                account_generations=account_generations,
            )
            if violation is not None:
                owned_row = row or legacy_row
                if owned_row is not None:
                    try:
                        service.delete_agent_event(
                            source,
                            owned_row.external_id,
                            expected_identity=(
                                identity if row is not None else None
                            ),
                        )
                    except Exception:
                        session.rollback()
                        logger.exception(
                            "Removing invalidated proposal %s event %s from %s "
                            "failed; retrying next poll.",
                            proposal.id,
                            owned_row.external_id,
                            source.value,
                        )
                        continue
                proposal.status = ProposalStatus.INVALIDATED
                session.commit()
                logger.warning(
                    "Proposal %s invalidated before calendar push: %s",
                    proposal.id,
                    violation,
                )
                continue
            if intake_row is not None:
                proposal.status = ProposalStatus.PUSHED
                task.status = "scheduled"
                session.commit()
                pushed += 1
                logger.info(
                    "Proposal %s adopted timed intake event %s on %s without "
                    "creating a duplicate.",
                    proposal.id,
                    intake_row.external_id,
                    intake_row.calendar_source.value,
                )
                continue
            if legacy_row is not None:
                proposal.status = ProposalStatus.PUSHED
                task.status = "scheduled"
                session.commit()
                pushed += 1
                logger.info(
                    "Proposal %s adopted legacy agent block %s on %s without "
                    "creating a duplicate.",
                    proposal.id,
                    legacy_row.external_id,
                    source.value,
                )
                continue
            reused_existing = row is not None
            try:
                row = service.create_agent_event(source, draft)
            except Exception:
                session.rollback()
                logger.exception(
                    "Pushing proposal %s (%s) to %s failed; retrying next poll.",
                    proposal.id,
                    task.title,
                    source.value,
                )
                continue
            if reused_existing:
                logger.info(
                    "Proposal %s already has agent block %s on %s; finishing the "
                    "interrupted status advance instead of re-creating it.",
                    proposal.id,
                    row.external_id,
                    source.value,
                )

            session.expire_all()
            post_create_violation = actual_sleep_violation(
                session,
                coerce_utc(proposal.proposed_start),
                coerce_utc(proposal.proposed_end),
                timezone,
                account_generations=account_generations,
            )
            if post_create_violation is not None:
                try:
                    service.delete_agent_event(
                        source,
                        row.external_id,
                        expected_identity=identity,
                    )
                except Exception:
                    session.rollback()
                    logger.exception(
                        "Rolling back proposal %s event %s after a concurrent "
                        "sleep update failed; retrying next poll.",
                        proposal.id,
                        row.external_id,
                    )
                    continue
                proposal.status = ProposalStatus.INVALIDATED
                session.commit()
                logger.warning(
                    "Proposal %s invalidated after calendar create: %s",
                    proposal.id,
                    post_create_violation,
                )
                continue
            proposal.status = ProposalStatus.PUSHED
            task.status = "scheduled"
            session.commit()
            pushed += 1
            logger.info(
                "Proposal %s pushed to %s as event %s (%s).",
                proposal.id,
                source.value,
                row.external_id,
                task.title,
            )
    return pushed


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _calendar_sync_error_code(exc: Exception) -> str:
    """Map an exception class to a stable code without retaining its message."""
    if isinstance(exc, CalendarAuthError):
        return "calendar_auth_error"
    if isinstance(exc, CalendarConflictError):
        return "calendar_conflict_error"
    if isinstance(exc, EventNotFoundError):
        return "calendar_event_not_found"
    if isinstance(exc, OwnershipError):
        return "calendar_ownership_error"
    if isinstance(exc, SQLAlchemyError):
        return "calendar_storage_error"
    if isinstance(exc, TimeoutError):
        return "calendar_timeout"
    if isinstance(exc, CalendarError):
        return "calendar_provider_error"
    if isinstance(exc, ValueError):
        return "calendar_invalid_data"
    if isinstance(exc, OSError):
        return "calendar_io_error"
    return "calendar_sync_error"


def _sync_health_time(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None:
        raise ValueError("calendar sync-health clock must be timezone-aware")
    return value.astimezone(UTC)


def _record_sync_health_attempt(
    store: SyncHealthStore,
    source: CalendarSource,
    clock: Callable[[], datetime],
) -> None:
    try:
        store.record_attempt(source, _sync_health_time(clock))
    except Exception:
        logger.warning(
            "Calendar sync-health attempt write for %s failed; continuing sync.",
            source.value,
            exc_info=True,
        )


def _record_sync_health_success(
    store: SyncHealthStore,
    source: CalendarSource,
    clock: Callable[[], datetime],
    *,
    event_count: int | None,
    coverage_kind: SyncCoverageKind,
    coverage_start: datetime | None,
    coverage_end: datetime | None,
    account_generation: str | None,
) -> bool:
    try:
        store.record_success(
            source,
            _sync_health_time(clock),
            event_count=event_count,
            coverage_kind=coverage_kind,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            account_generation=account_generation,
        )
    except Exception:
        logger.warning(
            "Calendar sync-health success write for %s failed; mirror sync "
            "remains durable but generation-derived work stays unpublished.",
            source.value,
            exc_info=True,
        )
        return False
    return True


def _record_writeback_attempt(
    store: SyncHealthStore,
    source: CalendarSource,
    clock: Callable[[], datetime],
    *,
    attempted_count: int,
) -> None:
    try:
        store.record_writeback_attempt(
            source,
            _sync_health_time(clock),
            attempted_count=attempted_count,
        )
    except Exception:
        logger.warning(
            "Calendar writeback-health attempt write for %s failed; "
            "continuing writeback.",
            source.value,
            exc_info=True,
        )


def _record_writeback_result(
    store: SyncHealthStore,
    source: CalendarSource,
    clock: Callable[[], datetime],
    *,
    attempted_count: int,
    succeeded_count: int,
    failed_count: int,
    error_code: str | None = None,
) -> None:
    try:
        store.record_writeback_result(
            source,
            _sync_health_time(clock),
            attempted_count=attempted_count,
            succeeded_count=succeeded_count,
            failed_count=failed_count,
            error_code=error_code,
        )
    except Exception:
        logger.warning(
            "Calendar writeback-health result write for %s failed; "
            "writeback result remains unchanged.",
            source.value,
            exc_info=True,
        )


def _record_sync_health_failure(
    store: SyncHealthStore,
    source: CalendarSource,
    clock: Callable[[], datetime],
    error_code: str,
) -> None:
    try:
        store.record_failure(
            source,
            _sync_health_time(clock),
            error_code=error_code,
        )
    except Exception:
        logger.warning(
            "Calendar sync-health failure write for %s failed; scheduler will continue.",
            source.value,
            exc_info=True,
        )


def _calendar_mirror_count(
    session: Session,
    source: CalendarSource,
    *,
    account_generation: str | None = None,
) -> int | None:
    """Count local mirror rows without opening a transaction on ``session``."""
    statement = select(func.count()).select_from(
        CalendarEventMirror
    ).where(
        CalendarEventMirror.calendar_source == source
    )
    if account_generation is not None:
        statement = statement.where(
            CalendarEventMirror.connection_generation
            == account_generation
        )
    try:
        bind = session.get_bind()
        if isinstance(bind, Engine):
            with bind.connect() as connection:
                return int(connection.scalar(statement) or 0)
        if isinstance(bind, Connection):
            return int(bind.scalar(statement) or 0)
    except Exception:
        logger.warning(
            "Calendar mirror count for %s failed; recording success with unknown coverage.",
            source.value,
            exc_info=True,
        )
    return None


def build_calendar_job(
    settings: Settings,
    source: CalendarSource,
    *,
    is_write_backend: bool,
    backend_factory: Callable[[], CalendarBackend] | None = None,
    session_factory: sessionmaker[Session] | None = None,
    state_store: SyncStateStore | None = None,
    pending_store: PendingDiffStore | None = None,
    health_store: SyncHealthStore | None = None,
    clock: Callable[[], datetime] | None = None,
    connection_generation_resolver: Callable[[], str | None] | None = None,
    account_generation_resolver: Callable[[], str | None] | None = None,
    account_generations_resolver: (
        Callable[[], Mapping[CalendarSource, str]] | None
    ) = None,
) -> Callable[[], SyncDiff | None]:
    """Zero-arg poll job for one backend (collaborators injectable for tests).

    The backend is constructed lazily and reused only while the credential
    generation is unchanged. Every remote read/write and backend construction
    runs under the same source lock used by connect/disconnect, so a completed
    revocation cannot leave a cached client performing remote work. A failed
    construction is retried on the next interval. The job RETURNS the run's
    :class:`SyncDiff` (``None`` if the run failed) so the downstream
    ``schedule_changed`` trigger can consume deletions.
    """
    resolve_generation = (
        connection_generation_resolver
        if connection_generation_resolver is not None
        else (
            (lambda: "injected")
            if backend_factory is not None
            else lambda: creds.calendar_connection_generation(
                settings,
                source,
            )
        )
    )
    backend_fence = CalendarBackendFence(
        source=source,
        backend_factory=(
            backend_factory
            if backend_factory is not None
            else lambda: _build_backend(settings, source)
        ),
        generation_resolver=resolve_generation,
    )
    sync_health = (
        health_store
        if health_store is not None
        else FileSyncHealthStore.for_data_dir(settings.data_dir)
    )
    sync_clock = clock if clock is not None else _utc_now
    resolve_account_generation = (
        account_generation_resolver
        if account_generation_resolver is not None
        else (
            None
            if backend_factory is not None
            else lambda: creds.calendar_account_generation(
                settings,
                source,
            )
        )
    )
    resolve_account_generations = (
        account_generations_resolver
        if account_generations_resolver is not None
        else (
            None
            if backend_factory is not None
            else lambda: creds.calendar_account_generations(settings)
        )
    )

    def run_calendar_sync() -> SyncDiff | None:
        _record_sync_health_attempt(sync_health, source, sync_clock)
        try:
            store = (
                state_store
                if state_store is not None
                else FileSyncStateStore.for_data_dir(settings.data_dir)
            )
            journal = (
                pending_store
                if pending_store is not None
                else FilePendingDiffStore.for_data_dir(settings.data_dir)
            )
            with session_scope(session_factory) as session:
                with backend_fence.use(session) as backend:
                    account_generation = (
                        resolve_account_generation()
                        if resolve_account_generation is not None
                        else None
                    )
                    if (
                        backend_factory is None
                        and account_generation is None
                    ):
                        raise CalendarAuthError(
                            "calendar account generation is unavailable"
                        )
                    service = CalendarMirrorService(
                        session,
                        [backend],
                        store,
                        journal,
                        account_generation=account_generation,
                    )
                    diff = service.sync_backend(backend)
                    event_count = _calendar_mirror_count(
                        session,
                        source,
                        account_generation=account_generation,
                    )
                    coverage_kind, coverage_start, coverage_end = (
                        sync_state_coverage(store.load(source))
                    )
                    generation_published = _record_sync_health_success(
                        sync_health,
                        source,
                        sync_clock,
                        event_count=event_count,
                        coverage_kind=coverage_kind,
                        coverage_start=coverage_start,
                        coverage_end=coverage_end,
                        account_generation=account_generation,
                    )
                    if generation_published:
                        intake_calendar_tasks(
                            session,
                            source,
                            resolve_timezone(settings),
                            account_generation=account_generation,
                        )
                        session.commit()
                    if is_write_backend:
                        proposal_ids: list[UUID] = []
                        attempted_count = 0
                        succeeded_count = 0
                        attempt_recorded = False
                        try:
                            proposal_ids = _accepted_proposal_ids(
                                session
                            )
                            attempted_count = len(proposal_ids)
                            _record_writeback_attempt(
                                sync_health,
                                source,
                                sync_clock,
                                attempted_count=attempted_count,
                            )
                            attempt_recorded = True
                            succeeded_count = push_accepted_proposals(
                                service,
                                session,
                                source,
                                resolve_timezone(settings),
                                current_account_generations=(
                                    resolve_account_generations()
                                    if resolve_account_generations
                                    is not None
                                    else (
                                        {source: account_generation}
                                        if account_generation is not None
                                        else None
                                    )
                                ),
                            )
                            remaining = set(
                                _accepted_proposal_ids(session)
                            )
                            failed_count = len(
                                set(proposal_ids).intersection(
                                    remaining
                                )
                            )
                            _record_writeback_result(
                                sync_health,
                                source,
                                sync_clock,
                                attempted_count=attempted_count,
                                succeeded_count=succeeded_count,
                                failed_count=failed_count,
                            )
                        except Exception as exc:
                            session.rollback()
                            if not attempt_recorded:
                                _record_writeback_attempt(
                                    sync_health,
                                    source,
                                    sync_clock,
                                    attempted_count=attempted_count,
                                )
                            _record_writeback_result(
                                sync_health,
                                source,
                                sync_clock,
                                attempted_count=attempted_count,
                                succeeded_count=succeeded_count,
                                failed_count=max(
                                    0,
                                    attempted_count - succeeded_count,
                                ),
                                error_code=_calendar_sync_error_code(
                                    exc
                                ),
                            )
                            logger.exception(
                                "Calendar proposal writeback for %s failed "
                                "after a successful inbound sync; next "
                                "interval will retry.",
                                source.value,
                            )
        except Exception as exc:
            _record_sync_health_failure(
                sync_health,
                source,
                sync_clock,
                _calendar_sync_error_code(exc),
            )
            logger.exception(
                "Calendar sync for %s failed; next interval will retry.", source.value
            )
            return None
        return diff

    return run_calendar_sync


def build_calendar_jobs(settings: Settings) -> list[CalendarJobSpec]:
    """Job specs for every enabled backend (empty when both flags are off)."""
    writer = write_source(settings)
    specs: list[CalendarJobSpec] = []
    for source in enabled_sources(settings):
        minutes = (
            settings.google_poll_minutes
            if source is CalendarSource.GOOGLE
            else settings.caldav_poll_minutes
        )
        specs.append(
            CalendarJobSpec(
                source=source,
                job_id=calendar_job_id(source),
                interval_minutes=minutes,
                job=build_calendar_job(settings, source, is_write_backend=source is writer),
            )
        )
    return specs
