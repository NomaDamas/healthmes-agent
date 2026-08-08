import hashlib
from datetime import UTC, datetime, time, timedelta, tzinfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.calendars.base import coerce_utc
from healthmes.store.enums import CalendarSource, TaskSource
from healthmes.store.models import CalendarEventMirror, Task

INTAKE_PREFIX = "[HM]"
MAX_INTAKE_TITLE_LENGTH = 500


def intake_title(summary: str | None) -> str | None:
    if summary is None:
        return None
    normalized = summary.strip()
    if not normalized.startswith(INTAKE_PREFIX):
        return None
    title = " ".join(normalized[len(INTAKE_PREFIX) :].split())
    title = title[:MAX_INTAKE_TITLE_LENGTH].rstrip()
    return title or None


def is_intake_eligible(mirror: CalendarEventMirror) -> bool:
    return bool(
        mirror.calendar_source in (CalendarSource.GOOGLE, CalendarSource.CALDAV)
        and intake_title(mirror.summary) is not None
        and not mirror.is_agent_created
        and mirror.organizer_self
        and not mirror.has_attendees
        and not mirror.is_recurring
        and mirror.event_type in (None, "default")
        and mirror.status != "cancelled"
    )


def intake_revision(mirror: CalendarEventMirror) -> str:
    provider_revision = mirror.etag or coerce_utc(mirror.updated_at).isoformat()
    raw = (
        f"{mirror.calendar_source.value}:{mirror.external_id}:"
        f"{provider_revision}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def intake_calendar_tasks(
    session: Session, source: CalendarSource, local_timezone: tzinfo
) -> tuple[Task, ...]:
    if source not in (CalendarSource.GOOGLE, CalendarSource.CALDAV):
        return ()

    mirrors = session.scalars(
        select(CalendarEventMirror)
        .where(CalendarEventMirror.calendar_source == source)
        .order_by(CalendarEventMirror.created_at, CalendarEventMirror.id)
    ).all()
    affected: list[Task] = []
    for mirror in mirrors:
        title = intake_title(mirror.summary)
        if not is_intake_eligible(mirror):
            if mirror.intake_task_id is not None:
                task = session.get(Task, mirror.intake_task_id)
                if task is not None:
                    task.status = "cancelled"
                    affected.append(task)
                mirror.intake_task_id = None
                mirror.intake_opted_out = True
            continue

        assert title is not None
        mirror.intake_opted_out = False

        if mirror.is_all_day:
            est_minutes = None
            end_at = coerce_utc(mirror.end_at)
            exclusive_end_date = (
                end_at.astimezone(local_timezone).date()
                if mirror.calendar_source is CalendarSource.CALDAV
                else end_at.date()
            )
            deadline = (
                datetime.combine(exclusive_end_date, time.min, tzinfo=local_timezone)
                - timedelta(microseconds=1)
            ).astimezone(UTC)
        else:
            duration = coerce_utc(mirror.end_at) - coerce_utc(mirror.start_at)
            est_minutes = int(duration.total_seconds() // 60)
            if est_minutes <= 0:
                continue
            deadline = None

        task = (
            session.get(Task, mirror.intake_task_id)
            if mirror.intake_task_id is not None
            else None
        )
        if task is None:
            task = Task(
                title=title,
                goal_id=None,
                est_minutes=est_minutes,
                deadline=deadline,
                source=TaskSource.USER,
            )
            session.add(task)
            session.flush()
            mirror.intake_task_id = task.id
        else:
            task.title = title
            task.est_minutes = est_minutes
            task.deadline = deadline
        affected.append(task)

    return tuple(affected)
