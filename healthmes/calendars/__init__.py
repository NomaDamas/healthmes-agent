"""Calendar sync backends (Google Calendar API + iCloud CalDAV).

Ownership-split conflict philosophy: the external calendar owns every event
the agent did not create; the agent only writes/moves its own tagged events
(Google ``healthmes=1`` extended property, CalDAV ``X-HEALTHMES``). External
edits to agent events win and surface in the sync diff so the trigger engine
can fire ``schedule_changed``. See docs/PLAN.md section 6.

Import layering: this package only depends on ``healthmes.store`` (models +
enums). Provider client libraries are imported lazily at call time and
credentials are runtime-only.
"""

from healthmes.calendars.base import (
    AGENT_TAG_VALUE,
    GOOGLE_AGENT_TAG_KEY,
    GOOGLE_AGENT_TASK_ID_KEY,
    ICAL_AGENT_PROPERTY,
    ICAL_AGENT_TASK_ID_PROPERTY,
    CalendarAuthError,
    CalendarBackend,
    CalendarError,
    EventDraft,
    EventNotFoundError,
    ExternalEvent,
    OwnershipError,
    SyncState,
)
from healthmes.calendars.caldav_icloud import ICLOUD_CALDAV_URL, CalDavCalendarBackend
from healthmes.calendars.google import GOOGLE_SCOPES, GoogleCalendarBackend
from healthmes.calendars.state import (
    FileSyncStateStore,
    InMemorySyncStateStore,
    SyncStateStore,
)
from healthmes.calendars.sync import (
    CalendarMirrorService,
    ChangeKind,
    EventChange,
    SyncDiff,
)

__all__ = [
    # tagging contract
    "AGENT_TAG_VALUE",
    "GOOGLE_AGENT_TAG_KEY",
    "GOOGLE_AGENT_TASK_ID_KEY",
    "ICAL_AGENT_PROPERTY",
    "ICAL_AGENT_TASK_ID_PROPERTY",
    # contract types
    "CalendarAuthError",
    "CalendarBackend",
    "CalendarError",
    "EventDraft",
    "EventNotFoundError",
    "ExternalEvent",
    "OwnershipError",
    "SyncState",
    # backends
    "CalDavCalendarBackend",
    "GoogleCalendarBackend",
    "GOOGLE_SCOPES",
    "ICLOUD_CALDAV_URL",
    # sync state persistence
    "FileSyncStateStore",
    "InMemorySyncStateStore",
    "SyncStateStore",
    # mirror service / diff
    "CalendarMirrorService",
    "ChangeKind",
    "EventChange",
    "SyncDiff",
]
