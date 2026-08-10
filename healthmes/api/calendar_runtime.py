"""Refresh live calendar scheduler jobs after browser credential changes."""

from __future__ import annotations

from apscheduler.jobstores.base import JobLookupError
from fastapi import FastAPI

from healthmes.calendars.jobs import build_calendar_jobs, calendar_job_id
from healthmes.engine.scheduler import (
    SLEEP_RECONCILIATION_JOB_ID,
    register_calendar_job,
    register_sleep_reconciliation_job,
)
from healthmes.store import CalendarSource


def refresh_calendar_jobs(app: FastAPI) -> None:
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is None:
        return

    specs = build_calendar_jobs(app.state.settings)
    desired = {spec.job_id: spec for spec in specs}
    for source in CalendarSource:
        job_id = calendar_job_id(source)
        if job_id in desired:
            continue
        try:
            scheduler.remove_job(job_id)
        except JobLookupError:
            pass
    for spec in specs:
        register_calendar_job(
            scheduler,
            spec.job,
            job_id=spec.job_id,
            minutes=spec.interval_minutes,
        )
    # sleep_job imports API auth helpers, so loading it at module import time
    # creates a cycle through healthmes.api.__init__ during CLI startup.
    from healthmes.calendars.sleep_job import build_sleep_reconciliation_job

    sleep_job = build_sleep_reconciliation_job(app.state.settings)
    if sleep_job is None:
        try:
            scheduler.remove_job(SLEEP_RECONCILIATION_JOB_ID)
        except JobLookupError:
            pass
    else:
        register_sleep_reconciliation_job(scheduler, sleep_job)
