"""Scheduler wiring tests: trigger job, registration hooks, enable gate."""

from datetime import timedelta

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from healthmes.config import Settings
from healthmes.engine.scheduler import (
    BACKUP_JOB_ID,
    ENERGY_JOB_ID,
    TRIGGER_JOB_ID,
    create_scheduler,
    register_backup_job,
    register_energy_job,
    shutdown_scheduler,
    start_scheduler,
)
from healthmes.engine.triggers import TRIGGER_INTERVAL_MINUTES


def noop() -> None:  # job stub for registration tests
    return None


@pytest.fixture
def scheduler(settings: Settings):
    scheduler = create_scheduler(settings, trigger_job=noop)
    yield scheduler
    shutdown_scheduler(scheduler, wait=False)


def test_create_scheduler_registers_10_minute_trigger_sweep(scheduler) -> None:
    job = scheduler.get_job(TRIGGER_JOB_ID)
    assert job is not None
    assert job.func is noop
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval == timedelta(minutes=TRIGGER_INTERVAL_MINUTES)
    assert TRIGGER_INTERVAL_MINUTES == 10  # docs/PLAN.md section 4
    # A slow sweep must never stack behind itself.
    assert job.max_instances == 1
    assert job.coalesce is True


def test_create_scheduler_does_not_start(scheduler) -> None:
    assert scheduler.running is False


def test_energy_job_hook_registers_hourly_cron(scheduler) -> None:
    assert scheduler.get_job(ENERGY_JOB_ID) is None  # hook only - no default job
    job = register_energy_job(scheduler, noop)
    assert scheduler.get_job(ENERGY_JOB_ID) is job
    assert job.func is noop
    assert isinstance(job.trigger, CronTrigger)
    fields = {field.name: str(field) for field in job.trigger.fields}
    assert fields["minute"] == "5"
    assert fields["hour"] == "*"  # every hour


def test_backup_job_hook_registers_weekly_cron(scheduler) -> None:
    assert scheduler.get_job(BACKUP_JOB_ID) is None  # hook only - no default job
    job = register_backup_job(scheduler, noop)
    assert scheduler.get_job(BACKUP_JOB_ID) is job
    assert isinstance(job.trigger, CronTrigger)
    fields = {field.name: str(field) for field in job.trigger.fields}
    assert fields["day_of_week"] == "sun"
    assert fields["hour"] == "3"
    assert fields["minute"] == "30"


def test_hooks_are_replaceable(scheduler) -> None:
    register_energy_job(scheduler, noop)

    def replacement() -> None:
        return None

    job = register_energy_job(scheduler, replacement)
    assert job.func is replacement
    assert len([j for j in scheduler.get_jobs() if j.id == ENERGY_JOB_ID]) == 1


def test_start_scheduler_honors_disabled_flag(settings: Settings) -> None:
    assert settings.scheduler_enabled is False
    assert start_scheduler(settings) is None


def test_start_scheduler_starts_and_shutdown_stops(settings: Settings) -> None:
    enabled = settings.model_copy(update={"scheduler_enabled": True})
    scheduler = create_scheduler(enabled, trigger_job=noop)
    try:
        started = start_scheduler(enabled, scheduler=scheduler)
        assert started is scheduler
        assert scheduler.running is True
    finally:
        shutdown_scheduler(scheduler, wait=False)
    assert scheduler.running is False


def test_shutdown_scheduler_is_none_safe() -> None:
    shutdown_scheduler(None)  # must not raise
    shutdown_scheduler(BackgroundScheduler())  # never started -> no-op
