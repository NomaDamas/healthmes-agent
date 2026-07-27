"""Shared calendar-backend contract (docs/PLAN.md section 6).

Every backend normalizes provider events into :class:`ExternalEvent` and
implements :class:`CalendarBackend`. Change detection is cursor-based: the
caller passes the previous opaque ``sync_state`` (JSON-serializable dict) and
receives the changed events plus the next state to persist.

Agent-ownership tagging contract
--------------------------------
The external calendar is the source of truth for every event the agent did
not create; the agent may only write/move/delete its own blocks. Agent-created
events are tagged so ownership survives round-trips through the provider:

- **Google Calendar**: private extended property ``healthmes=1``
  (``extendedProperties.private``), plus ``healthmes_task_id=<uuid>`` linking
  back to the healthmes ``task`` row.
- **CalDAV (iCloud)**: iCalendar ``X-HEALTHMES:1`` property on the VEVENT,
  plus ``X-HEALTHMES-TASK-ID:<uuid>``.

All datetimes crossing this boundary are timezone-aware and normalized to
UTC. Credentials are runtime-only: importing this package never reads token
files or opens network connections.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from healthmes.store.enums import CalendarSource

__all__ = [
    "AGENT_TAG_VALUE",
    "GOOGLE_AGENT_TAG_KEY",
    "GOOGLE_AGENT_TASK_ID_KEY",
    "GOOGLE_EVENT_KIND_KEY",
    "GOOGLE_EVENT_SOURCE_KEY",
    "GOOGLE_EVENT_UPSERT_KEY",
    "ICAL_AGENT_PROPERTY",
    "ICAL_AGENT_TASK_ID_PROPERTY",
    "ICAL_EVENT_KIND_PROPERTY",
    "ICAL_EVENT_SOURCE_KEY_PROPERTY",
    "ICAL_EVENT_SOURCE_PROPERTY",
    "CalendarAuthError",
    "CalendarBackend",
    "CalendarConflictError",
    "CalendarError",
    "CalendarEventIdentity",
    "ConfirmedExternalTimeChange",
    "EventDraft",
    "EventNotFoundError",
    "ExternalEvent",
    "HealthmesEventKind",
    "OwnershipError",
    "SyncState",
    "coerce_utc",
    "ensure_utc",
    "parse_calendar_identity",
    "parse_event_kind",
    "parse_task_id",
]

# --- agent-ownership tagging contract ---------------------------------------

#: Google private extended-property key marking an agent-created event.
GOOGLE_AGENT_TAG_KEY = "healthmes"
#: Value of the ownership tag (both backends).
AGENT_TAG_VALUE = "1"
#: Google private extended-property key carrying the healthmes task UUID.
GOOGLE_AGENT_TASK_ID_KEY = "healthmes_task_id"
GOOGLE_EVENT_KIND_KEY = "healthmes_kind"
GOOGLE_EVENT_SOURCE_KEY = "healthmes_source"
GOOGLE_EVENT_UPSERT_KEY = "healthmes_source_key"
#: iCalendar X-property marking an agent-created VEVENT.
ICAL_AGENT_PROPERTY = "X-HEALTHMES"
#: iCalendar X-property carrying the healthmes task UUID.
ICAL_AGENT_TASK_ID_PROPERTY = "X-HEALTHMES-TASK-ID"
ICAL_EVENT_KIND_PROPERTY = "X-HEALTHMES-KIND"
ICAL_EVENT_SOURCE_PROPERTY = "X-HEALTHMES-SOURCE"
ICAL_EVENT_SOURCE_KEY_PROPERTY = "X-HEALTHMES-SOURCE-KEY"

#: Opaque per-backend change cursor (Google syncToken / CalDAV ctag+etags).
#: Must stay JSON-serializable so any SyncStateStore can persist it.
SyncState = dict[str, Any]


class HealthmesEventKind(StrEnum):
    ACTUAL_SLEEP = "actual_sleep"
    PLANNED_SLEEP = "planned_sleep"


def parse_event_kind(value: object) -> HealthmesEventKind | None:
    if not isinstance(value, str):
        return None
    try:
        return HealthmesEventKind(value.strip())
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class CalendarEventIdentity:
    kind: HealthmesEventKind
    source: str
    source_key: str

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("CalendarEventIdentity.source must be non-empty")
        if not self.source_key.strip():
            raise ValueError("CalendarEventIdentity.source_key must be non-empty")


def parse_calendar_identity(
    kind: object,
    source: object,
    source_key: object,
) -> CalendarEventIdentity | None:
    if not isinstance(kind, str) or not isinstance(source, str) or not isinstance(
        source_key, str
    ):
        return None
    event_kind = parse_event_kind(kind)
    if event_kind is None:
        return None
    try:
        return CalendarEventIdentity(
            kind=event_kind,
            source=source.strip(),
            source_key=source_key.strip(),
        )
    except ValueError:
        return None


class CalendarError(Exception):
    """Base error for calendar backends and the mirror service."""


class CalendarAuthError(CalendarError):
    """Credentials are missing, expired beyond refresh, or rejected."""


class EventNotFoundError(CalendarError):
    """The referenced event does not exist on the backend (or in the mirror)."""


class OwnershipError(CalendarError):
    """Write refused: the event is owned by the external calendar, not the agent.

    Raised whenever a move/delete targets an event that is not agent-created
    (docs/PLAN.md section 6 ownership split).
    """


class CalendarConflictError(CalendarError):
    """A conditional write lost its ``If-Match`` precondition (HTTP 412).

    The event changed on the provider between the read that supplied the etag
    and the write it guarded (a check-then-act race). Rather than blindly
    overwriting the newer remote state, the backend refuses the write; the
    caller re-syncs the mirror and retries against the fresh version
    (docs/PLAN.md section 6).
    """


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` converted to UTC; reject naive datetimes.

    Boundary datetimes (drafts, normalized events) must carry an explicit
    timezone — silently guessing would corrupt schedules across DST changes.
    """
    if value.tzinfo is None:
        raise ValueError("naive datetime not allowed at the calendar boundary; pass tz-aware")
    return value.astimezone(UTC)


def coerce_utc(value: datetime) -> datetime:
    """Return ``value`` in UTC, assuming UTC for naive datetimes.

    Lenient variant for values we stored ourselves: sqlite round-trips
    ``DateTime(timezone=True)`` columns as naive UTC, and iCalendar "floating"
    times have no zone. Both are treated as UTC by convention (all values
    written by this package are UTC).
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_task_id(value: object) -> uuid.UUID | None:
    """Parse an ownership-tag task id into a UUID; ``None`` when absent/invalid.

    External systems may hand back arbitrary strings; a malformed tag must
    never break a sync run.
    """
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value).strip())
    except (ValueError, AttributeError, TypeError):
        return None


@dataclass(frozen=True, slots=True)
class ExternalEvent:
    """A provider event normalized to the backend-independent shape.

    ``deleted=True`` marks a deletion notice (Google ``status=cancelled`` /
    CalDAV resource gone); deleted events may lack times because providers
    only guarantee the id. Live events always have UTC-normalized aware
    ``start_at``/``end_at``.
    """

    external_id: str
    summary: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    is_agent_created: bool = False
    agent_task_id: uuid.UUID | None = None
    identity: CalendarEventIdentity | None = None
    healthmes_kind: HealthmesEventKind | None = None
    etag: str | None = None
    deleted: bool = False
    organizer_self: bool = False
    has_attendees: bool = False
    is_recurring: bool = False
    event_type: str | None = None
    is_all_day: bool = False
    is_locked: bool = False
    status: str | None = None

    def __post_init__(self) -> None:
        if not self.external_id:
            raise ValueError("ExternalEvent.external_id must be non-empty")
        if not self.deleted and (self.start_at is None or self.end_at is None):
            raise ValueError("live ExternalEvent requires start_at and end_at")
        if self.start_at is not None:
            object.__setattr__(self, "start_at", ensure_utc(self.start_at))
        if self.end_at is not None:
            object.__setattr__(self, "end_at", ensure_utc(self.end_at))
        if self.start_at is not None and self.end_at is not None and self.end_at < self.start_at:
            raise ValueError("ExternalEvent.end_at must not precede start_at")


@dataclass(frozen=True, slots=True)
class ConfirmedExternalTimeChange:
    """A user-confirmed, time-only mutation for one external event.

    This is the narrow exception to the external-calendar ownership rule:
    only an end-time shortening fixed by a confirmed proposal can cross this
    boundary.
    """

    external_event_id: str
    original_start_at: datetime
    original_end_at: datetime
    proposed_start_at: datetime
    proposed_end_at: datetime
    expected_etag: str

    def __post_init__(self) -> None:
        if not self.external_event_id:
            raise ValueError("ConfirmedExternalTimeChange.external_event_id must be non-empty")
        if not self.expected_etag:
            raise ValueError("ConfirmedExternalTimeChange.expected_etag must be non-empty")

        original_start_date = self.original_start_at.date()
        original_end_date = self.original_end_at.date()
        proposed_start_date = self.proposed_start_at.date()
        proposed_end_date = self.proposed_end_at.date()

        original_start = ensure_utc(self.original_start_at)
        original_end = ensure_utc(self.original_end_at)
        proposed_start = ensure_utc(self.proposed_start_at)
        proposed_end = ensure_utc(self.proposed_end_at)

        object.__setattr__(self, "original_start_at", original_start)
        object.__setattr__(self, "original_end_at", original_end)
        object.__setattr__(self, "proposed_start_at", proposed_start)
        object.__setattr__(self, "proposed_end_at", proposed_end)

        if original_end <= original_start:
            raise ValueError("original_end_at must be after original_start_at")
        if proposed_end <= proposed_start:
            raise ValueError("proposed_end_at must be after proposed_start_at")
        if proposed_start != original_start:
            raise ValueError("v0 external time changes may not move start_at")
        if not (original_start < proposed_end < original_end):
            raise ValueError("v0 external time changes must shorten the event end")
        if original_end - original_start < timedelta(minutes=60):
            raise ValueError("original duration must be at least 60 minutes")
        if proposed_end - proposed_start < timedelta(minutes=30):
            raise ValueError("proposed duration must be at least 30 minutes")
        if original_end - proposed_end != timedelta(minutes=30):
            raise ValueError("v0 external time changes must shorten by exactly 30 minutes")
        dates = {
            original_start_date,
            original_end_date,
            proposed_start_date,
            proposed_end_date,
        }
        if len(dates) != 1:
            raise ValueError("v0 external time changes must remain on one local date")


@dataclass(frozen=True, slots=True)
class EventDraft:
    """Agent-authored event content for ``create_event``/``update_event``.

    Backends add the ownership tag themselves — every draft written through
    a backend is by definition agent-created.
    """

    summary: str
    start_at: datetime
    end_at: datetime
    description: str | None = None
    agent_task_id: uuid.UUID | None = None
    identity: CalendarEventIdentity | None = None

    def __post_init__(self) -> None:
        if not self.summary:
            raise ValueError("EventDraft.summary must be non-empty")
        object.__setattr__(self, "start_at", ensure_utc(self.start_at))
        object.__setattr__(self, "end_at", ensure_utc(self.end_at))
        if self.end_at <= self.start_at:
            raise ValueError("EventDraft.end_at must be after start_at")


@runtime_checkable
class CalendarBackend(Protocol):
    """Protocol every calendar backend implements (Google, CalDAV, test fakes).

    Implementations must be usable without credentials at construction-test
    time (clients/services are injected); network I/O happens only inside the
    methods below.
    """

    #: Which ``calendar_event_mirror.calendar_source`` this backend feeds.
    source: CalendarSource

    def list_changes(
        self, sync_state: SyncState | None
    ) -> tuple[list[ExternalEvent], SyncState]:
        """Return events changed since ``sync_state`` plus the next cursor.

        ``sync_state=None`` means "never synced": the backend returns the
        full current window and a fresh cursor. Deletions are returned as
        :class:`ExternalEvent` with ``deleted=True``. The returned state must
        be persisted by the caller and passed back on the next call.
        """
        ...

    def create_event(self, draft: EventDraft) -> ExternalEvent:
        """Create an agent-owned (tagged) event and return its normalized form."""
        ...

    def read_event(self, external_id: str) -> ExternalEvent:
        ...

    def update_event(
        self,
        external_id: str,
        *,
        summary: str | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        description: str | None = None,
        expected_etag: str | None = None,
    ) -> ExternalEvent:
        """Patch the given fields of an agent-owned event.

        Raises :class:`OwnershipError` when the target is not agent-tagged
        and :class:`EventNotFoundError` when it does not exist.
        """
        ...

    def delete_event(
        self,
        external_id: str,
        *,
        expected_kind: HealthmesEventKind | None = None,
        expected_etag: str | None = None,
    ) -> None:
        """Delete an agent-owned event (same ownership guarantees as update)."""
        ...
