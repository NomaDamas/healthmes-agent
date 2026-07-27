"""iCloud CalDAV backend (docs/PLAN.md section 6).

Talks to ``caldav.icloud.com`` through the ``caldav`` library using an Apple
ID + app-specific password. Change detection is ctag/etag based:

- The collection ``getctag`` is read first; if it matches the persisted one
  the calendar is unchanged and no event fetch happens (cheap 10-minute poll).
- Otherwise all VEVENTs are listed and fingerprinted (etag, falling back to a
  content hash when the server omits etags in REPORT responses); comparing
  fingerprints against the persisted map yields created/updated/deleted.

Agent ownership tag: ``X-HEALTHMES:1`` (+ ``X-HEALTHMES-TASK-ID``) on the
VEVENT, see :mod:`healthmes.calendars.base`.

Credentials are runtime-only: the ``caldav`` client is only constructed in
:meth:`CalDavCalendarBackend.connect`; the backend itself wraps an injected
calendar collection object, so tests use fakes without any network.

v1 scope note: recurring events are mirrored as their master VEVENT only
(no expansion, recurrence exceptions collapse onto the master UID).
"""

import hashlib
import logging
import uuid
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import Any

import icalendar

from healthmes.calendars.base import (
    AGENT_TAG_VALUE,
    ICAL_AGENT_PROPERTY,
    ICAL_AGENT_TASK_ID_PROPERTY,
    ICAL_EVENT_KIND_PROPERTY,
    ICAL_EVENT_SOURCE_KEY_PROPERTY,
    ICAL_EVENT_SOURCE_PROPERTY,
    CalendarConflictError,
    CalendarError,
    EventDraft,
    EventNotFoundError,
    ExternalEvent,
    HealthmesEventKind,
    OwnershipError,
    SyncState,
    calendar_identity_external_id,
    coerce_utc,
    ensure_utc,
    parse_calendar_identity,
    parse_event_kind,
    parse_task_id,
)
from healthmes.store.enums import CalendarSource

__all__ = [
    "CTAG_PROPERTY_TAG",
    "ETAG_PROPERTY_TAG",
    "ICLOUD_CALDAV_URL",
    "CalDavCalendarBackend",
]

logger = logging.getLogger(__name__)

ICLOUD_CALDAV_URL = "https://caldav.icloud.com"

#: WebDAV property tags (RFC 6578 / calendarserver extension).
CTAG_PROPERTY_TAG = "{http://calendarserver.org/ns/}getctag"
ETAG_PROPERTY_TAG = "{DAV:}getetag"


@lru_cache(maxsize=1)
def _ctag_element_cls() -> type:
    """Build the getctag PROPFIND element (caldav ships no first-class one)."""
    from caldav.elements.base import ValuedBaseElement

    class _GetCTag(ValuedBaseElement):
        tag = CTAG_PROPERTY_TAG

    return _GetCTag


def _is_caldav_not_found(exc: BaseException) -> bool:
    """Match ``caldav.lib.error.NotFoundError`` without importing it eagerly."""
    return type(exc).__name__ == "NotFoundError"


class CalDavCalendarBackend:
    """CalDAV backend over an injected calendar collection object.

    ``sync_state`` layout::

        {"ctag": "<collection ctag>", "fingerprints": {"<uid>": "<etag-or-hash>"}}
    """

    source = CalendarSource.CALDAV

    def __init__(self, calendar: Any) -> None:
        self._calendar = calendar

    @classmethod
    def connect(
        cls,
        *,
        username: str,
        app_password: str,
        url: str = ICLOUD_CALDAV_URL,
        calendar_name: str | None = None,
    ) -> "CalDavCalendarBackend":
        """Open a CalDAV session (iCloud: Apple ID + app-specific password).

        Picks the named calendar when ``calendar_name`` is given, else the
        principal's first calendar. Runtime-only: this is the single place
        credentials are used.
        """
        import caldav

        client = caldav.DAVClient(url=url, username=username, password=app_password)
        principal = client.principal()
        if calendar_name:
            try:
                return cls(principal.calendar(name=calendar_name))
            except Exception as exc:  # noqa: BLE001 - library raises broad errors
                raise CalendarError(
                    f"caldav calendar {calendar_name!r} not found at {url}"
                ) from exc
        calendars = principal.calendars()
        if not calendars:
            raise CalendarError(f"no caldav calendars available at {url}")
        return cls(calendars[0])

    # -- change feed -----------------------------------------------------

    def list_changes(
        self, sync_state: SyncState | None
    ) -> tuple[list[ExternalEvent], SyncState]:
        previous = dict(sync_state or {})
        previous_fingerprints: dict[str, str] = dict(previous.get("fingerprints") or {})
        ctag = self._read_ctag()

        if (
            sync_state is not None
            and ctag is not None
            and previous.get("ctag") == ctag
        ):
            return [], previous  # collection unchanged; skip the event fetch

        changed: list[ExternalEvent] = []
        fingerprints: dict[str, str] = {}
        for obj in self._calendar.events():
            parsed = self._event_from_object(obj)
            if parsed is None:
                continue
            event, fingerprint = parsed
            fingerprints[event.external_id] = fingerprint
            if previous_fingerprints.get(event.external_id) != fingerprint:
                changed.append(event)

        deletions = [
            ExternalEvent(external_id=uid, deleted=True)
            for uid in previous_fingerprints
            if uid not in fingerprints
        ]
        return changed + deletions, {"ctag": ctag, "fingerprints": fingerprints}

    def _read_ctag(self) -> str | None:
        try:
            value = self._calendar.get_property(_ctag_element_cls()())
        except Exception:  # noqa: BLE001 - a ctag-less server must not break sync
            logger.debug("caldav getctag PROPFIND failed; falling back to full scan")
            return None
        return str(value) if value is not None else None

    # -- agent writes ------------------------------------------------------

    def create_event(self, draft: EventDraft) -> ExternalEvent:
        external_id = (
            calendar_identity_external_id(self.source, draft.identity)
            if draft.identity is not None
            else f"{uuid.uuid4()}@healthmes"
        )
        if draft.identity is not None:
            try:
                self.read_event(external_id)
            except EventNotFoundError:
                pass
            else:
                raise CalendarConflictError("caldav identity key already exists")
        component = icalendar.Event()
        component.add("uid", external_id)
        component.add("dtstamp", datetime.now(UTC))
        component.add("summary", draft.summary)
        component.add("dtstart", draft.start_at)
        component.add("dtend", draft.end_at)
        if draft.description:
            component.add("description", draft.description)
        component[ICAL_AGENT_PROPERTY] = AGENT_TAG_VALUE
        if draft.agent_task_id is not None:
            component[ICAL_AGENT_TASK_ID_PROPERTY] = str(draft.agent_task_id)
        if draft.identity is not None:
            component[ICAL_EVENT_KIND_PROPERTY] = draft.identity.kind.value
            component[ICAL_EVENT_SOURCE_PROPERTY] = draft.identity.source
            component[ICAL_EVENT_SOURCE_KEY_PROPERTY] = draft.identity.source_key

        calendar = icalendar.Calendar()
        calendar.add("prodid", "-//HealthMes Agent//healthmes//EN")
        calendar.add("version", "2.0")
        calendar.add_component(component)
        try:
            self._calendar.add_event(
                ical=calendar.to_ical().decode("utf-8"),
                no_overwrite=draft.identity is not None,
            )
        except Exception as exc:  # noqa: BLE001 - caldav exposes broad DAV errors
            if draft.identity is None:
                raise
            try:
                self.read_event(external_id)
            except EventNotFoundError:
                raise exc
            raise CalendarConflictError(
                "caldav identity key already exists or create conflicted"
            ) from exc

        return ExternalEvent(
            external_id=external_id,
            summary=draft.summary,
            start_at=draft.start_at,
            end_at=draft.end_at,
            is_agent_created=True,
            agent_task_id=draft.agent_task_id,
            identity=draft.identity,
            healthmes_kind=draft.identity.kind if draft.identity is not None else None,
        )

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
        obj = self._get_owned_object(external_id)
        self._assert_expected_etag(obj, external_id, expected_etag)
        component = obj.icalendar_component
        if summary is not None:
            _replace_property(component, "summary", summary)
        if start_at is not None:
            _replace_property(component, "dtstart", ensure_utc(start_at))
        if end_at is not None:
            _replace_property(component, "dtend", ensure_utc(end_at))
        if description is not None:
            _replace_property(component, "description", description)
        # caldav 3.2.x reads its read-only ``obj.etag`` during save() and
        # sends it as ``if-match`` on the PUT.
        try:
            obj.save()
        except Exception as exc:  # noqa: BLE001 - caldav exposes broad DAV errors
            if _is_precondition_failure(exc):
                raise CalendarConflictError(
                    f"caldav event {external_id!r} changed during update"
                ) from exc
            raise
        parsed = self._event_from_object(obj)
        if parsed is None:  # pragma: no cover - we just wrote a valid component
            raise CalendarError(f"caldav event {external_id!r} unparsable after update")
        return parsed[0]

    def delete_event(
        self,
        external_id: str,
        *,
        expected_kind: HealthmesEventKind | None = None,
        expected_etag: str | None = None,
    ) -> None:
        obj = self._get_owned_object(external_id)
        self._assert_expected_etag(obj, external_id, expected_etag)
        remote_kind = parse_event_kind(
            obj.icalendar_component.get(ICAL_EVENT_KIND_PROPERTY)
        )
        if expected_kind is not None and remote_kind is not expected_kind:
            raise OwnershipError(
                f"caldav event {external_id!r} is not {expected_kind.value}"
            )
        delete_etag = expected_etag or _object_etag(obj)
        if delete_etag is None:
            raise CalendarConflictError(
                "caldav event has no ETag for conditional delete"
            )
        if not _conditional_delete(obj, delete_etag):
            raise CalendarConflictError(
                "caldav backend cannot guarantee a conditional delete"
            )

    def read_event(self, external_id: str) -> ExternalEvent:
        parsed = self._event_from_object(self._get_owned_object(external_id))
        if parsed is None:
            raise CalendarError(f"caldav event {external_id!r} is unparsable")
        return parsed[0]

    @staticmethod
    def _assert_expected_etag(
        obj: Any,
        external_id: str,
        expected_etag: str | None,
    ) -> None:
        current_etag = _object_etag(obj)
        if expected_etag is not None and current_etag != expected_etag:
            raise CalendarConflictError(
                f"caldav event {external_id!r} changed since mirror sync"
            )

    def _get_owned_object(self, external_id: str) -> Any:
        """Fetch by UID and enforce the X-HEALTHMES ownership tag."""
        try:
            obj = self._calendar.event_by_uid(external_id)
        except Exception as exc:  # noqa: BLE001 - library raises broad errors
            if _is_caldav_not_found(exc):
                raise EventNotFoundError(f"caldav event {external_id!r} not found") from exc
            raise
        component = obj.icalendar_component
        if str(component.get(ICAL_AGENT_PROPERTY, "")).strip() != AGENT_TAG_VALUE:
            raise OwnershipError(
                f"caldav event {external_id!r} is not agent-created "
                f"(missing {ICAL_AGENT_PROPERTY}:{AGENT_TAG_VALUE}); "
                "the external calendar owns it"
            )
        return obj

    # -- parsing -----------------------------------------------------------

    def _event_from_object(self, obj: Any) -> tuple[ExternalEvent, str] | None:
        """Normalize one CalDAV object; ``None`` skips unusable resources."""
        try:
            component = obj.icalendar_component
        except Exception:  # noqa: BLE001 - tolerate broken resources
            logger.warning("skipping unparsable caldav resource %s", getattr(obj, "url", "?"))
            return None
        if component is None or component.get("UID") is None:
            logger.warning("skipping caldav resource without UID: %s", getattr(obj, "url", "?"))
            return None

        uid = str(component.get("UID"))
        start_at = _component_datetime(component, "DTSTART")
        if start_at is None:
            logger.warning("skipping caldav VEVENT %s without DTSTART", uid)
            return None
        end_at = _component_datetime(component, "DTEND")
        if end_at is None:
            duration = component.get("DURATION")
            if duration is not None:
                end_at = start_at + duration.dt
            else:
                # RFC 5545: all-day defaults to one day, timed to zero length.
                dtstart_value = component.get("DTSTART").dt
                is_all_day = isinstance(dtstart_value, date) and not isinstance(
                    dtstart_value, datetime
                )
                end_at = start_at + (timedelta(days=1) if is_all_day else timedelta())

        summary_value = component.get("SUMMARY")
        is_agent = str(component.get(ICAL_AGENT_PROPERTY, "")).strip() == AGENT_TAG_VALUE
        event = ExternalEvent(
            external_id=uid,
            summary=str(summary_value) if summary_value is not None else None,
            start_at=start_at,
            end_at=end_at,
            is_agent_created=is_agent,
            agent_task_id=parse_task_id(component.get(ICAL_AGENT_TASK_ID_PROPERTY)),
            identity=parse_calendar_identity(
                component.get(ICAL_EVENT_KIND_PROPERTY),
                component.get(ICAL_EVENT_SOURCE_PROPERTY),
                component.get(ICAL_EVENT_SOURCE_KEY_PROPERTY),
            ),
            healthmes_kind=parse_event_kind(
                component.get(ICAL_EVENT_KIND_PROPERTY)
            ),
            etag=_object_etag(obj),
        )
        return event, _fingerprint(obj, component)


def _replace_property(component: Any, name: str, value: Any) -> None:
    """Replace an iCalendar property, letting ``add`` re-encode the value."""
    component.pop(name.upper(), None)
    component.add(name, value)


def _object_etag(obj: Any) -> str | None:
    props = getattr(obj, "props", None) or {}
    etag = props.get(ETAG_PROPERTY_TAG)
    return str(etag) if etag else None


def _conditional_delete(obj: Any, expected_etag: str) -> bool:
    client = getattr(obj, "client", None)
    url = getattr(obj, "url", None)
    if client is None or url is None or not hasattr(client, "request"):
        return False
    response = client.request(
        str(url),
        method="DELETE",
        headers={"If-Match": expected_etag},
    )
    status = getattr(response, "status", None)
    if status == 412:
        raise CalendarConflictError("caldav event changed during delete")
    if status == 404:
        raise EventNotFoundError("caldav event disappeared during delete")
    if status not in (200, 204):
        raise CalendarError("caldav conditional delete failed")
    return True


def _is_precondition_failure(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    if status == 412:
        return True
    reason = getattr(exc, "reason", "")
    return isinstance(reason, str) and "412" in reason


def _fingerprint(obj: Any, component: Any) -> str:
    """Change fingerprint: server etag, else a hash of the serialized VEVENT."""
    etag = _object_etag(obj)
    if etag:
        return etag
    return hashlib.sha256(component.to_ical()).hexdigest()


def _component_datetime(component: Any, name: str) -> datetime | None:
    """Read DTSTART/DTEND as an aware UTC datetime.

    All-day dates map to midnight UTC and iCalendar "floating" times are
    treated as UTC (documented convention, mirrors the Google backend).
    """
    prop = component.get(name)
    if prop is None:
        return None
    value = prop.dt
    if isinstance(value, datetime):
        return coerce_utc(value)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=UTC)
    return None
