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
- The **write backend** (CalDAV/iCloud when enabled, else Google — one
  designated writer so the same block is never created twice) additionally pushes
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
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from healthmes.calendars import creds
from healthmes.calendars.apple_google_mirror import mirror_apple_events_to_google
from healthmes.calendars.base import CalendarAuthError, CalendarBackend
from healthmes.calendars.intake import intake_calendar_tasks
from healthmes.calendars.proposal_push import push_accepted_proposals
from healthmes.calendars.runtime_status import record_calendar_status
from healthmes.calendars.state import (
    FilePendingDiffStore,
    FileSyncStateStore,
    PendingDiffStore,
    SyncStateStore,
)
from healthmes.calendars.sync import CalendarMirrorService, SyncDiff
from healthmes.config import Settings, resolve_timezone
from healthmes.store.enums import CalendarSource
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

    Write-preference order (CalDAV/iCloud first, then Google). "Connected" means the runtime
    token/creds file under ``Settings.data_dir`` exists and is usable
    (healthmes/calendars/creds.py) — establishing a connection with the CLI
    is enough, no ``.env`` edit required. The settings flags keep working
    and force a backend on even without a stored file (its poll then fails
    per-run until credentials appear, exactly as before).
    """
    sources: list[CalendarSource] = []
    if settings.caldav_enabled or creds.load_caldav_credentials(settings.data_dir) is not None:
        sources.append(CalendarSource.CALDAV)
    if settings.google_calendar_enabled or creds.google_connected(settings.data_dir):
        sources.append(CalendarSource.GOOGLE)
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
                diff = service.sync_backend(backend)
                intake_calendar_tasks(session, source, resolve_timezone(settings))
                if is_write_backend:
                    push_accepted_proposals(service, session, source)
                elif (
                    source is CalendarSource.GOOGLE
                    and write_source(settings) is CalendarSource.CALDAV
                ):
                    mirror_apple_events_to_google(service, session)
                record_calendar_status(
                    settings.data_dir,
                    source,
                    mode=("write" if is_write_backend else "mirror"),
                )
                return diff
        except Exception as exc:
            record_calendar_status(
                settings.data_dir,
                source,
                mode=("write" if is_write_backend else "mirror"),
                error=exc,
            )
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
