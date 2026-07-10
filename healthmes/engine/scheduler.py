"""In-service APScheduler wiring (docs/PLAN.md sections 3-4).

``create_scheduler`` builds an unstarted ``BackgroundScheduler`` that already
carries the 10-minute trigger sweep, plus explicit registration points for
jobs owned by later scopes:

- ``register_energy_job``   — hourly cognitive-energy persist (PLAN §3; the
  cognitive-energy scope passes its real callable);
- ``register_backup_job``   — weekly local-first backup snapshot (PLAN §9; the
  backup scope passes its real callable);
- ``register_calendar_job`` — per-backend calendar mirror poll + accepted-
  proposal push (PLAN §6; the calendars scope passes one job per enabled
  backend, see ``healthmes/calendars/jobs.py``).

Nothing here starts by itself: the app lifespan calls ``start_scheduler``,
which honors ``Settings.scheduler_enabled`` (False in tests, one-off tooling
and any process that must not run background loops), and pairs it with
``shutdown_scheduler`` on shutdown. Intended wiring in ``healthmes/app.py``::

    scheduler = start_scheduler(settings)   # None when disabled
    ...
    shutdown_scheduler(scheduler)
"""

import logging
from collections.abc import Callable

from apscheduler.job import Job
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from healthmes.config import Settings
from healthmes.engine.triggers import TRIGGER_INTERVAL_MINUTES, build_trigger_job

__all__ = [
    "TRIGGER_JOB_ID",
    "ENERGY_JOB_ID",
    "BACKUP_JOB_ID",
    "create_scheduler",
    "register_energy_job",
    "register_backup_job",
    "register_calendar_job",
    "start_scheduler",
    "shutdown_scheduler",
]

logger = logging.getLogger(__name__)

TRIGGER_JOB_ID = "healthmes-trigger-sweep"
ENERGY_JOB_ID = "healthmes-cognitive-energy"
BACKUP_JOB_ID = "healthmes-weekly-backup"

# One misfired run is coalesced and allowed to start this late (seconds);
# with max_instances=1 a slow sweep can never pile up behind itself.
_MISFIRE_GRACE_SECONDS = 120


def _remove_job_if_present(scheduler: BackgroundScheduler, job_id: str) -> None:
    """Deterministic replace for the registration hooks.

    APScheduler's ``replace_existing`` only dedupes when jobs reach a real
    job store (i.e. at/after ``start()``); on a not-yet-started scheduler a
    re-registration would otherwise leave two pending jobs with the same id.
    """
    try:
        scheduler.remove_job(job_id)
    except JobLookupError:
        pass


def create_scheduler(
    settings: Settings, *, trigger_job: Callable[[], None] | None = None
) -> BackgroundScheduler:
    """Build the (unstarted) scheduler with the trigger sweep registered.

    ``trigger_job`` is injectable for tests; the default is the lazy
    evaluator job from ``healthmes/engine/triggers.py``.
    """
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        trigger_job if trigger_job is not None else build_trigger_job(settings),
        trigger=IntervalTrigger(minutes=TRIGGER_INTERVAL_MINUTES),
        id=TRIGGER_JOB_ID,
        name="HealthMes trigger sweep",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=_MISFIRE_GRACE_SECONDS,
    )
    return scheduler


def register_energy_job(
    scheduler: BackgroundScheduler,
    job: Callable[[], None],
    *,
    minute: int = 5,
) -> Job:
    """Registration point for the hourly cognitive-energy persist job.

    The cognitive-energy scope calls this with its real callable (a thin
    zero-arg wrapper around the engine persist). Runs at ``minute`` past
    every hour — offset from the top of the hour so it never lines up with
    provider syncs writing the same windows.
    """
    _remove_job_if_present(scheduler, ENERGY_JOB_ID)
    return scheduler.add_job(
        job,
        trigger=CronTrigger(minute=minute),
        id=ENERGY_JOB_ID,
        name="HealthMes cognitive-energy persist",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=600,
    )


def register_backup_job(
    scheduler: BackgroundScheduler,
    job: Callable[[], None],
    *,
    day_of_week: str = "sun",
    hour: int = 3,
    minute: int = 30,
) -> Job:
    """Registration point for the weekly local-first backup job.

    The backup scope calls this with its real callable (snapshot + age
    encryption per PLAN §9). Default slot: Sunday 03:30 local — inside quiet
    hours on purpose, backups produce no alerts.
    """
    _remove_job_if_present(scheduler, BACKUP_JOB_ID)
    return scheduler.add_job(
        job,
        trigger=CronTrigger(day_of_week=day_of_week, hour=hour, minute=minute),
        id=BACKUP_JOB_ID,
        name="HealthMes weekly backup",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )


def register_calendar_job(
    scheduler: BackgroundScheduler,
    job: Callable[[], None],
    *,
    job_id: str,
    minutes: int,
) -> Job:
    """Registration point for one calendar mirror poll job (PLAN §6).

    The calendars scope calls this once per enabled backend with its poll
    interval (Google 5 min / CalDAV 10 min by default); the job body owns
    lazy backend construction and per-run error containment.
    """
    _remove_job_if_present(scheduler, job_id)
    return scheduler.add_job(
        job,
        trigger=IntervalTrigger(minutes=minutes),
        id=job_id,
        name=f"HealthMes calendar sync ({job_id})",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=_MISFIRE_GRACE_SECONDS,
    )


def start_scheduler(
    settings: Settings, *, scheduler: BackgroundScheduler | None = None
) -> BackgroundScheduler | None:
    """Start the scheduler iff ``settings.scheduler_enabled``; else return None.

    A prebuilt ``scheduler`` (with extra jobs already registered via the
    hooks above) is used as-is; otherwise ``create_scheduler`` builds the
    default one.
    """
    if not settings.scheduler_enabled:
        logger.info("Scheduler disabled (HEALTHMES_SCHEDULER_ENABLED=false); not starting.")
        return None
    scheduler = scheduler if scheduler is not None else create_scheduler(settings)
    scheduler.start()
    logger.info(
        "Scheduler started with jobs: %s",
        ", ".join(job.id for job in scheduler.get_jobs()),
    )
    return scheduler


def shutdown_scheduler(scheduler: BackgroundScheduler | None, *, wait: bool = True) -> None:
    """Stop a scheduler returned by ``start_scheduler`` (None-safe)."""
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=wait)
        logger.info("Scheduler stopped.")
