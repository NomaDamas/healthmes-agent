"""Calendar poll jobs: mirror sync + accepted-proposal push (docs/PLAN.md §6).

This is the production entry point of the calendar plane — the piece that
turns the (fully tested) backend/service library into running behavior:

- :func:`build_calendar_jobs` returns one :class:`CalendarJobSpec` per
  *enabled* backend (``Settings.google_calendar_enabled`` /
  ``Settings.caldav_enabled``), polling at ``google_poll_minutes`` /
  ``caldav_poll_minutes`` (PLAN §6: 5 / 10 minutes). The app lifespan
  registers each spec on the in-process scheduler.
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
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from healthmes.calendars.base import CalendarBackend, EventDraft, coerce_utc
from healthmes.calendars.state import FileSyncStateStore, SyncStateStore
from healthmes.calendars.sync import CalendarMirrorService
from healthmes.config import Settings
from healthmes.store.enums import CalendarSource, ProposalStatus
from healthmes.store.models import ScheduleProposal, Task
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
    job: Callable[[], None]


def calendar_job_id(source: CalendarSource) -> str:
    return f"healthmes-calendar-{source.value}"


def enabled_sources(settings: Settings) -> tuple[CalendarSource, ...]:
    """Backends enabled by settings, in write-preference order (Google first)."""
    sources: list[CalendarSource] = []
    if settings.google_calendar_enabled:
        sources.append(CalendarSource.GOOGLE)
    if settings.caldav_enabled:
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

    return CalDavCalendarBackend.connect(
        username=settings.caldav_username,
        app_password=settings.caldav_app_password.get_secret_value(),
        url=settings.caldav_url,
        calendar_name=settings.caldav_calendar_name,
    )


def _accepted_proposals(session: Session) -> Iterator[tuple[ScheduleProposal, Task]]:
    rows = session.execute(
        select(ScheduleProposal, Task)
        .join(Task, ScheduleProposal.task_id == Task.id)
        .where(ScheduleProposal.status == ProposalStatus.ACCEPTED)
        .order_by(ScheduleProposal.proposed_start)
    )
    yield from ((proposal, task) for proposal, task in rows)


def push_accepted_proposals(
    service: CalendarMirrorService, session: Session, source: CalendarSource
) -> int:
    """Write every ``accepted`` proposal to the calendar; advance to ``pushed``.

    Each proposal is pushed independently: the remote create commits the
    mirror row first, then the status flips to ``pushed`` and commits. A crash
    between the two leaves the proposal ``accepted`` — the next poll retries
    (at worst duplicating one block, never losing one). A failing backend
    call leaves the proposal untouched for the next poll.
    """
    pushed = 0
    for proposal, task in list(_accepted_proposals(session)):
        draft = EventDraft(
            summary=task.title,
            start_at=coerce_utc(proposal.proposed_start),
            end_at=coerce_utc(proposal.proposed_end),
            agent_task_id=task.id,
        )
        try:
            row = service.create_agent_event(source, draft)
        except Exception:
            logger.exception(
                "Pushing proposal %s (%s) to %s failed; retrying next poll.",
                proposal.id,
                task.title,
                source.value,
            )
            continue
        proposal.status = ProposalStatus.PUSHED
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


def build_calendar_job(
    settings: Settings,
    source: CalendarSource,
    *,
    is_write_backend: bool,
    backend_factory: Callable[[], CalendarBackend] | None = None,
    session_factory: sessionmaker[Session] | None = None,
    state_store: SyncStateStore | None = None,
) -> Callable[[], None]:
    """Zero-arg poll job for one backend (collaborators injectable for tests).

    The backend is constructed lazily on the first run and reused (Google
    keeps an authorized service, CalDAV keeps its session); a failed
    construction is retried on the next interval.
    """
    backend: CalendarBackend | None = None

    def run_calendar_sync() -> None:
        nonlocal backend
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
            with session_scope(session_factory) as session:
                service = CalendarMirrorService(session, [backend], store)
                service.sync_backend(backend)
                if is_write_backend:
                    push_accepted_proposals(service, session, source)
        except Exception:
            logger.exception(
                "Calendar sync for %s failed; next interval will retry.", source.value
            )

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
