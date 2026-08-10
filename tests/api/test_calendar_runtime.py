from types import SimpleNamespace

from apscheduler.schedulers.background import BackgroundScheduler

from healthmes.api.calendar_runtime import refresh_calendar_jobs
from healthmes.calendars.jobs import CalendarJobSpec, calendar_job_id
from healthmes.store import CalendarSource


def noop() -> None:
    return None


def test_refresh_registers_current_jobs_and_removes_disconnected_jobs(
    app,
    monkeypatch,
) -> None:
    scheduler = BackgroundScheduler()
    app.state.scheduler = scheduler
    stale = calendar_job_id(CalendarSource.GOOGLE)
    scheduler.add_job(noop, "interval", minutes=5, id=stale)
    desired = CalendarJobSpec(
        source=CalendarSource.CALDAV,
        job_id=calendar_job_id(CalendarSource.CALDAV),
        interval_minutes=10,
        job=noop,
    )
    monkeypatch.setattr(
        "healthmes.api.calendar_runtime.build_calendar_jobs",
        lambda settings: [desired],
    )

    refresh_calendar_jobs(app)

    assert scheduler.get_job(stale) is None
    assert scheduler.get_job(desired.job_id).func is noop


def test_refresh_is_noop_when_scheduler_is_disabled(app) -> None:
    app.state.scheduler = None
    refresh_calendar_jobs(app)


def test_refresh_accepts_app_without_lifespan_scheduler(settings) -> None:
    app = SimpleNamespace(state=SimpleNamespace(settings=settings))
    refresh_calendar_jobs(app)
