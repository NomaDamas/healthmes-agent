import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import assert_never

from healthmes.calendars.base import CalendarBackend
from healthmes.config import Settings
from healthmes.store import CalendarSource


@dataclass(frozen=True, slots=True)
class ApprovalCalendar:
    backend: CalendarBackend
    target: str
    review_base_url: str | None = None
    review_url_builder: Callable[[dt.date], str] | None = None


def calendar_approval_target(settings: Settings, source: CalendarSource) -> str:
    match source:
        case CalendarSource.GOOGLE:
            return f"google:{settings.google_calendar_id}"
        case CalendarSource.CALDAV:
            calendar = settings.caldav_calendar_name or "default"
            return f"caldav:{settings.caldav_url}:{calendar}"
        case unexpected:
            assert_never(unexpected)
