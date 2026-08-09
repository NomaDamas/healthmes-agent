"""Mirror planner-created iCloud blocks into Google after their primary write."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.calendars.base import (
    CalendarEventIdentity,
    EventDraft,
    HealthmesEventKind,
    coerce_utc,
    parse_event_kind,
)
from healthmes.calendars.sync import CalendarMirrorService
from healthmes.store.enums import CalendarSource
from healthmes.store.models import CalendarEventMirror

_MIRROR_SOURCE = "apple_calendar_mirror"


def mirror_apple_events_to_google(service: CalendarMirrorService, session: Session) -> int:
    """Create one Google mirror for each planner block already written to iCloud.

    The unique ``healthmes_source_key`` on the Google mirror makes repeated poll
    runs idempotent. Only ``planner`` blocks participate, keeping external and
    observed-health events out of this delivery path.
    """
    apple_rows = session.scalars(
        select(CalendarEventMirror)
        .where(
            CalendarEventMirror.calendar_source == CalendarSource.CALDAV,
            CalendarEventMirror.is_agent_created.is_(True),
            CalendarEventMirror.healthmes_source == "planner",
            CalendarEventMirror.summary.is_not(None),
        )
        .order_by(CalendarEventMirror.created_at)
    ).all()
    mirrored = 0
    for apple_row in apple_rows:
        identity = CalendarEventIdentity(
            kind=parse_event_kind(apple_row.healthmes_kind) or HealthmesEventKind.TASK_BLOCK,
            source=_MIRROR_SOURCE,
            source_key=f"caldav:{apple_row.id}",
        )
        exists = session.scalar(
            select(CalendarEventMirror.id).where(
                CalendarEventMirror.calendar_source == CalendarSource.GOOGLE,
                CalendarEventMirror.healthmes_source == identity.source,
                CalendarEventMirror.healthmes_source_key == identity.source_key,
            )
        )
        if exists is not None:
            continue
        service.create_agent_event(
            CalendarSource.GOOGLE,
            EventDraft(
                summary=apple_row.summary,
                start_at=coerce_utc(apple_row.start_at),
                end_at=coerce_utc(apple_row.end_at),
                agent_task_id=apple_row.agent_task_id,
                identity=identity,
            ),
        )
        mirrored += 1
    return mirrored
