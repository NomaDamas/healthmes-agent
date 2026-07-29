from datetime import UTC, datetime, time, timedelta, tzinfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.calendars.base import coerce_utc
from healthmes.store.enums import CalendarSource
from healthmes.store.models import CalendarEventMirror, Task

INTAKE_PREFIX = "[HM]"


def intake_title(summary: str | None) -> str | None:
    if summary is None:
        return None
    normalized = summary.strip()
    if not normalized.startswith(INTAKE_PREFIX):
        return None
    title = normalized[len(INTAKE_PREFIX) :].strip()
    return title or None


def intake_calendar_tasks(
    session: Session, source: CalendarSource, local_timezone: tzinfo
) -> tuple[Task, ...]:
    if source is not CalendarSource.GOOGLE:
        return ()

    mirrors = session.scalars(
        select(CalendarEventMirror)
        .where(CalendarEventMirror.calendar_source == source)
        .order_by(CalendarEventMirror.created_at, CalendarEventMirror.id)
    ).all()
    affected: list[Task] = []
    for mirror in mirrors:
        title = intake_title(mirror.summary)
        if (
            title is None
            or mirror.is_agent_created
            or not mirror.organizer_self
            or mirror.has_attendees
            or mirror.is_recurring
            or mirror.event_type not in (None, "default")
            or mirror.status == "cancelled"
        ):
            continue

        if mirror.is_all_day:
            est_minutes = None
            exclusive_end_date = coerce_utc(mirror.end_at).date()
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
