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
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, tzinfo
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from healthmes.calendars import creds
from healthmes.calendars.base import (
    CalendarAuthError,
    CalendarBackend,
    CalendarConflictError,
    CalendarEventIdentity,
    EventDraft,
    HealthmesEventKind,
    OwnershipError,
    calendar_identity_external_id,
    coerce_utc,
)
from healthmes.calendars.intake import (
    intake_calendar_tasks,
    intake_revision,
    is_intake_eligible,
)
from healthmes.calendars.sleep_context import actual_sleep_violation
from healthmes.calendars.state import (
    FilePendingDiffStore,
    FileSyncStateStore,
    PendingDiffStore,
    SyncStateStore,
)
from healthmes.calendars.sync import CalendarMirrorService, SyncDiff
from healthmes.calendars.write_lock import calendar_write_lock
from healthmes.config import Settings, resolve_timezone
from healthmes.schedule_outcomes import (
    record_calendar_push_outcome,
    record_invalidation_outcome,
)
from healthmes.store.enums import CalendarSource, ProposalStatus
from healthmes.store.models import CalendarEventMirror, ScheduleProposal, Task
from healthmes.store.session import session_scope

__all__ = [
    "CalendarJobSpec",
    "build_calendar_jobs",
    "calendar_job_id",
    "enabled_sources",
    "push_accepted_proposals",
    "write_source",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CalendarJobSpec:
    """One scheduler registration: a zero-arg poll job plus its cadence."""

    source: CalendarSource
    job_id: str
    interval_minutes: int
    job: Callable[[], SyncDiff | None]


def calendar_job_id(source: CalendarSource) -> str:
    return f"healthmes-calendar-{source.value}"


def enabled_sources(settings: Settings) -> tuple[CalendarSource, ...]:
    """Backends enabled by settings OR connected via ``healthmes connect``.

    Write-preference order (Google first). "Connected" means the runtime
    token/creds file under ``Settings.data_dir`` exists and is usable
    (healthmes/calendars/creds.py) — establishing a connection with the CLI
    is enough, no ``.env`` edit required. The settings flags keep working
    and force a backend on even without a stored file (its poll then fails
    per-run until credentials appear, exactly as before).
    """
    sources: list[CalendarSource] = []
    if settings.google_calendar_enabled or creds.google_connected(settings.data_dir):
        sources.append(CalendarSource.GOOGLE)
    if settings.caldav_enabled or creds.load_caldav_credentials(settings.data_dir) is not None:
        sources.append(CalendarSource.CALDAV)
    return tuple(sources)


def write_source(settings: Settings) -> CalendarSource | None:
    """Return the explicitly selected writer, or the legacy auto fallback."""
    sources = enabled_sources(settings)
    if not sources:
        return None
    configured = settings.calendar_write_provider
    if configured == "auto":
        return sources[0]
    selected = CalendarSource(configured)
    if selected not in sources:
        logger.warning(
            "Configured calendar writer %s is not connected; calendar writes "
            "are disabled until that provider is available.",
            configured,
        )
        return None
    return selected


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
        local_timezone=resolve_timezone(settings),
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
) -> CalendarEventMirror | None:
    """Return the trusted agent mirror row already written for this proposal.

    The proposal UUID is part of the deterministic remote identity, so another
    proposal for the same task and time can never be mistaken for this block.
    Times are compared in Python via ``coerce_utc`` because sqlite round-trips
    ``DateTime`` columns as naive UTC.
    """
    row = _owned_proposal_block(session, source, proposal)
    if row is None:
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


def _owned_proposal_block(
    session: Session,
    source: CalendarSource,
    proposal: ScheduleProposal,
) -> CalendarEventMirror | None:
    """Return the canonical agent-owned block for a proposal, regardless of time."""
    identity = _proposal_identity(proposal)
    row = session.scalar(
        select(CalendarEventMirror).where(
            CalendarEventMirror.calendar_source == source,
            CalendarEventMirror.is_agent_created.is_(True),
            CalendarEventMirror.healthmes_kind == identity.kind.value,
            CalendarEventMirror.healthmes_source == identity.source,
            CalendarEventMirror.healthmes_source_key == identity.source_key,
        )
    )
    if row is None:
        return None
    if row.external_id != calendar_identity_external_id(source, identity):
        return None
    return row


def _legacy_agent_block(
    session: Session,
    source: CalendarSource,
    proposal: ScheduleProposal,
    task: Task,
) -> CalendarEventMirror | None:
    """Find one unambiguous block created before proposal identities existed."""
    if proposal.healthmes_kind == HealthmesEventKind.PLANNED_SLEEP.value:
        return None
    candidates = list(
        session.scalars(
            select(CalendarEventMirror).where(
                CalendarEventMirror.calendar_source == source,
                CalendarEventMirror.agent_task_id == task.id,
                CalendarEventMirror.is_agent_created.is_(True),
                CalendarEventMirror.healthmes_kind.is_(None),
                CalendarEventMirror.healthmes_source.is_(None),
                CalendarEventMirror.healthmes_source_key.is_(None),
            )
        ).all()
    )
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
) -> CalendarEventMirror | None:
    """Return the exact externally-owned timed intake event captured by a proposal."""
    fields = (
        proposal.intake_calendar_source,
        proposal.intake_external_id,
        proposal.intake_revision,
    )
    if fields == (None, None, None):
        return None
    if any(value is None for value in fields):
        raise CalendarConflictError(
            f"proposal {proposal.id} has incomplete timed intake identity"
        )
    row = session.scalar(
        select(CalendarEventMirror).where(
            CalendarEventMirror.calendar_source
            == proposal.intake_calendar_source,
            CalendarEventMirror.external_id == proposal.intake_external_id,
        ).with_for_update()
    )
    if (
        row is None
        or row.intake_task_id != proposal.task_id
        or row.is_all_day
        or not is_intake_eligible(row)
        or intake_revision(row) != proposal.intake_revision
    ):
        raise CalendarConflictError(
            f"proposal {proposal.id} timed intake event changed or disappeared"
        )
    start = coerce_utc(proposal.proposed_start)
    end = coerce_utc(proposal.proposed_end)
    if coerce_utc(row.start_at) != start or coerce_utc(row.end_at) != end:
        raise CalendarConflictError(
            f"proposal {proposal.id} timed intake interval changed"
        )
    return row


def push_accepted_proposals(
    service: CalendarMirrorService,
    session: Session,
    source: CalendarSource,
    timezone: tzinfo = UTC,
    *,
    raise_on_write_failure: bool = False,
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
                intake_row = _timed_intake_block(session, proposal)
            except CalendarConflictError as exc:
                proposal.status = ProposalStatus.INVALIDATED
                record_invalidation_outcome(
                    session,
                    proposal,
                    reason=f"timed_intake_conflict:{exc}",
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
                row = _existing_agent_block(session, source, proposal)
                legacy_row = (
                    _legacy_agent_block(session, source, proposal, task)
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
                        if raise_on_write_failure:
                            raise
                        continue
                proposal.status = ProposalStatus.INVALIDATED
                record_invalidation_outcome(
                    session,
                    proposal,
                    reason=f"sleep_conflict_before_push:{violation}",
                )
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
                record_calendar_push_outcome(
                    session,
                    proposal,
                    source,
                    provider_event_id=intake_row.external_id,
                    reused_existing=True,
                )
                session.commit()
                pushed += 1
                logger.info(
                    "Proposal %s adopted timed intake event %s on %s without "
                    "creating a duplicate.",
                    proposal.id,
                    intake_row.external_id,
                    source.value,
                )
                continue
            if legacy_row is not None:
                proposal.status = ProposalStatus.PUSHED
                task.status = "scheduled"
                record_calendar_push_outcome(
                    session,
                    proposal,
                    source,
                    provider_event_id=legacy_row.external_id,
                    reused_existing=True,
                )
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
                if raise_on_write_failure:
                    raise
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
                    if raise_on_write_failure:
                        raise
                    continue
                proposal.status = ProposalStatus.INVALIDATED
                record_invalidation_outcome(
                    session,
                    proposal,
                    reason=f"sleep_conflict_after_create:{post_create_violation}",
                )
                session.commit()
                logger.warning(
                    "Proposal %s invalidated after calendar create: %s",
                    proposal.id,
                    post_create_violation,
                )
                continue
            proposal.status = ProposalStatus.PUSHED
            task.status = "scheduled"
            record_calendar_push_outcome(
                session,
                proposal,
                source,
                provider_event_id=row.external_id,
                reused_existing=reused_existing,
            )
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


def _cleanup_cancelled_pushed_proposals(
    service: CalendarMirrorService,
    session: Session,
    source: CalendarSource,
) -> int:
    """Remove provider-owned blocks whose imported source task was cancelled."""
    proposal_ids = list(
        session.scalars(
            select(ScheduleProposal.id)
            .join(Task, ScheduleProposal.task_id == Task.id)
            .where(
                ScheduleProposal.status == ProposalStatus.PUSHED,
                Task.status == "cancelled",
            )
            .order_by(ScheduleProposal.proposed_start)
        ).all()
    )
    cleaned = 0
    for proposal_id in proposal_ids:
        with calendar_write_lock(session, source):
            session.expire_all()
            proposal = session.get(ScheduleProposal, proposal_id)
            if proposal is None or proposal.status is not ProposalStatus.PUSHED:
                continue
            task = session.get(Task, proposal.task_id)
            if task is None or task.status != "cancelled":
                continue
            identity = _proposal_identity(proposal)
            row = _owned_proposal_block(session, source, proposal)
            if row is not None:
                service.delete_agent_event(
                    source,
                    row.external_id,
                    expected_identity=identity,
                )
            else:
                block_on_another_source = session.scalar(
                    select(CalendarEventMirror.id).where(
                        CalendarEventMirror.is_agent_created.is_(True),
                        CalendarEventMirror.healthmes_kind == identity.kind.value,
                        CalendarEventMirror.healthmes_source == identity.source,
                        CalendarEventMirror.healthmes_source_key
                        == identity.source_key,
                    )
                )
                if block_on_another_source is not None:
                    continue
            proposal.status = ProposalStatus.INVALIDATED
            record_invalidation_outcome(
                session,
                proposal,
                reason="source_task_cancelled",
            )
            session.commit()
            cleaned += 1
    return cleaned


def build_calendar_job(
    settings: Settings,
    source: CalendarSource,
    *,
    is_write_backend: bool,
    backend_factory: Callable[[], CalendarBackend] | None = None,
    session_factory: sessionmaker[Session] | None = None,
    state_store: SyncStateStore | None = None,
    pending_store: PendingDiffStore | None = None,
) -> Callable[[], SyncDiff | None]:
    """Zero-arg poll job for one backend (collaborators injectable for tests).

    The backend is constructed lazily on the first run and reused (Google
    keeps an authorized service, CalDAV keeps its session); a failed
    construction is retried on the next interval. The job RETURNS the run's
    :class:`SyncDiff` (``None`` if the run failed) so the downstream
    ``schedule_changed`` trigger can consume deletions — which vanish from the
    mirror and so cannot be re-derived from row ``updated_at`` alone.
    """
    backend: CalendarBackend | None = None

    def run_calendar_sync() -> SyncDiff | None:
        nonlocal backend
        diff: SyncDiff | None = None
        service: CalendarMirrorService | None = None
        try:
            if backend is None:
                backend = (
                    backend_factory() if backend_factory is not None else _build_backend(
                        settings, source
                    )
                )
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
                service = CalendarMirrorService(session, [backend], store, journal)
                with calendar_write_lock(session, source):
                    diff = service.sync_backend(backend)
                    intake_calendar_tasks(
                        session,
                        source,
                        resolve_timezone(settings),
                    )
                    session.commit()
                _cleanup_cancelled_pushed_proposals(
                    service,
                    session,
                    source,
                )
                if is_write_backend:
                    push_accepted_proposals(
                        service,
                        session,
                        source,
                        resolve_timezone(settings),
                        raise_on_write_failure=True,
                    )
                return diff
        except Exception:
            if service is not None and diff is not None:
                try:
                    service.preserve_diff_for_retry(source, diff)
                except Exception:
                    logger.exception(
                        "Calendar diff journal restore for %s failed after "
                        "downstream processing error.",
                        source.value,
                    )
            # Authorized service/session objects can become permanently stale
            # after credential rotation or server-side session invalidation.
            # Rebuild on the next interval instead of caching a poisoned client.
            backend = None
            logger.exception(
                "Calendar sync for %s failed; next interval will retry.", source.value
            )
            return None

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
