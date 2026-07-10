"""CalDAV backend tests against fake collection objects (no network).

Components are built with the real ``icalendar`` library so parsing and
serialization exercise the same code paths as against caldav.icloud.com.
"""

import uuid
from datetime import UTC, date, datetime, timedelta, timezone

import icalendar
import pytest

from healthmes.calendars.base import (
    EventDraft,
    EventNotFoundError,
    OwnershipError,
)
from healthmes.calendars.caldav_icloud import (
    CTAG_PROPERTY_TAG,
    ETAG_PROPERTY_TAG,
    CalDavCalendarBackend,
)

KST = timezone(timedelta(hours=9))


class NotFoundError(Exception):
    """Stub matching caldav.lib.error.NotFoundError by class name."""


def make_component(
    uid: str,
    *,
    summary: str = "Standup",
    start: object = None,
    end: object = None,
    agent: bool = False,
    task_id: uuid.UUID | None = None,
) -> icalendar.Event:
    component = icalendar.Event()
    component.add("uid", uid)
    component.add("dtstamp", datetime(2026, 7, 1, tzinfo=UTC))
    component.add("summary", summary)
    component.add("dtstart", start if start is not None else datetime(2026, 7, 9, 9, 0, tzinfo=UTC))
    if end is not None:
        component.add("dtend", end)
    elif start is None:
        component.add("dtend", datetime(2026, 7, 9, 9, 30, tzinfo=UTC))
    if agent:
        component["X-HEALTHMES"] = "1"
    if task_id is not None:
        component["X-HEALTHMES-TASK-ID"] = str(task_id)
    return component


class FakeCalDavObject:
    def __init__(
        self, component: icalendar.Event, etag: str | None, calendar: "FakeCalDavCalendar"
    ) -> None:
        self.icalendar_component = component
        self.props = {ETAG_PROPERTY_TAG: etag} if etag else {}
        self._calendar = calendar
        self.saved = False

    @property
    def uid(self) -> str:
        return str(self.icalendar_component.get("UID"))

    def save(self) -> None:
        self.saved = True
        self._calendar.saved_objects.append(self.uid)

    def delete(self) -> None:
        self._calendar.deleted_uids.append(self.uid)
        self._calendar.objects.pop(self.uid, None)


class FakeCalDavCalendar:
    def __init__(self, ctag: str | None = "ctag-1") -> None:
        self.ctag = ctag
        self.objects: dict[str, FakeCalDavObject] = {}
        self.events_calls = 0
        self.ctag_requests: list[object] = []
        self.added_icals: list[str] = []
        self.saved_objects: list[str] = []
        self.deleted_uids: list[str] = []

    def put(
        self, component: icalendar.Event, etag: str | None = '"etag-1"'
    ) -> FakeCalDavObject:
        obj = FakeCalDavObject(component, etag, self)
        self.objects[obj.uid] = obj
        return obj

    # caldav Calendar surface used by the backend --------------------------
    def get_property(self, prop: object) -> str | None:
        self.ctag_requests.append(prop)
        return self.ctag

    def events(self) -> list[FakeCalDavObject]:
        self.events_calls += 1
        return list(self.objects.values())

    def event_by_uid(self, uid: str) -> FakeCalDavObject:
        if uid not in self.objects:
            raise NotFoundError(uid)
        return self.objects[uid]

    def add_event(self, ical: str | None = None, **_: object) -> FakeCalDavObject:
        assert ical is not None
        self.added_icals.append(ical)
        parsed = icalendar.Calendar.from_ical(ical)
        (component,) = [c for c in parsed.subcomponents if c.name == "VEVENT"]
        return self.put(component, etag='"fresh"')


@pytest.fixture
def calendar() -> FakeCalDavCalendar:
    return FakeCalDavCalendar()


@pytest.fixture
def backend(calendar: FakeCalDavCalendar) -> CalDavCalendarBackend:
    return CalDavCalendarBackend(calendar)


# --- change feed ---------------------------------------------------------------


class TestListChanges:
    def test_first_sync_returns_everything(self, backend, calendar) -> None:
        calendar.put(make_component("a"), etag='"a1"')
        calendar.put(make_component("b", agent=True), etag='"b1"')

        events, state = backend.list_changes(None)

        assert {event.external_id for event in events} == {"a", "b"}
        assert state == {"ctag": "ctag-1", "fingerprints": {"a": '"a1"', "b": '"b1"'}}
        agent_event = next(event for event in events if event.external_id == "b")
        assert agent_event.is_agent_created

    def test_unchanged_ctag_skips_event_fetch(self, backend, calendar) -> None:
        calendar.put(make_component("a"), etag='"a1"')
        _, state = backend.list_changes(None)
        assert calendar.events_calls == 1

        events, next_state = backend.list_changes(state)
        assert events == []
        assert next_state == state
        assert calendar.events_calls == 1  # no second fetch
        assert all(prop.tag == CTAG_PROPERTY_TAG for prop in calendar.ctag_requests)

    def test_etag_change_yields_only_changed_event(self, backend, calendar) -> None:
        calendar.put(make_component("a"), etag='"a1"')
        calendar.put(make_component("b"), etag='"b1"')
        _, state = backend.list_changes(None)

        calendar.ctag = "ctag-2"
        calendar.objects["b"] = FakeCalDavObject(
            make_component("b", summary="Standup (moved)"), '"b2"', calendar
        )
        events, next_state = backend.list_changes(state)

        assert [event.external_id for event in events] == ["b"]
        assert events[0].summary == "Standup (moved)"
        assert next_state["fingerprints"] == {"a": '"a1"', "b": '"b2"'}

    def test_removed_event_reported_deleted(self, backend, calendar) -> None:
        calendar.put(make_component("a"), etag='"a1"')
        calendar.put(make_component("b"), etag='"b1"')
        _, state = backend.list_changes(None)

        calendar.ctag = "ctag-2"
        del calendar.objects["b"]
        events, next_state = backend.list_changes(state)

        (gone,) = events
        assert gone.external_id == "b" and gone.deleted
        assert next_state["fingerprints"] == {"a": '"a1"'}

    def test_missing_etag_falls_back_to_content_hash(self, backend, calendar) -> None:
        calendar.put(make_component("a"), etag=None)
        _, state = backend.list_changes(None)
        assert len(state["fingerprints"]["a"]) == 64  # sha256 hex

        # Same content, ctag bumped: full rescan finds no changes.
        calendar.ctag = "ctag-2"
        events, _ = backend.list_changes(state)
        assert events == []


class TestComponentParsing:
    def test_agent_tag_and_task_id(self, backend, calendar) -> None:
        task_id = uuid.uuid4()
        calendar.put(make_component("mine", agent=True, task_id=task_id))
        (event,), _ = backend.list_changes(None)
        assert event.is_agent_created
        assert event.agent_task_id == task_id

    def test_aware_times_normalized_to_utc(self, backend, calendar) -> None:
        calendar.put(
            make_component(
                "kst",
                start=datetime(2026, 7, 9, 18, 0, tzinfo=KST),
                end=datetime(2026, 7, 9, 19, 0, tzinfo=KST),
            )
        )
        (event,), _ = backend.list_changes(None)
        assert event.start_at == datetime(2026, 7, 9, 9, 0, tzinfo=UTC)

    def test_floating_time_assumed_utc(self, backend, calendar) -> None:
        calendar.put(
            make_component(
                "floating",
                start=datetime(2026, 7, 9, 9, 0),
                end=datetime(2026, 7, 9, 10, 0),
            )
        )
        (event,), _ = backend.list_changes(None)
        assert event.start_at == datetime(2026, 7, 9, 9, 0, tzinfo=UTC)

    def test_all_day_event_maps_to_midnight_utc(self, backend, calendar) -> None:
        calendar.put(make_component("allday", start=date(2026, 7, 10), end=date(2026, 7, 11)))
        (event,), _ = backend.list_changes(None)
        assert event.start_at == datetime(2026, 7, 10, tzinfo=UTC)
        assert event.end_at == datetime(2026, 7, 11, tzinfo=UTC)

    def test_all_day_without_dtend_defaults_to_one_day(self, backend, calendar) -> None:
        component = icalendar.Event()
        component.add("uid", "open-ended")
        component.add("dtstamp", datetime(2026, 7, 1, tzinfo=UTC))
        component.add("summary", "Holiday")
        component.add("dtstart", date(2026, 7, 10))
        calendar.put(component)
        (event,), _ = backend.list_changes(None)
        assert event.end_at == datetime(2026, 7, 11, tzinfo=UTC)

    def test_component_without_uid_is_skipped(self, backend, calendar) -> None:
        component = icalendar.Event()
        component.add("dtstamp", datetime(2026, 7, 1, tzinfo=UTC))
        component.add("dtstart", datetime(2026, 7, 9, 9, 0, tzinfo=UTC))
        obj = FakeCalDavObject(component, '"x"', calendar)
        calendar.objects["broken"] = obj
        events, state = backend.list_changes(None)
        assert events == [] and state["fingerprints"] == {}


# --- agent writes ---------------------------------------------------------------


class TestCreateEvent:
    def test_saved_ical_carries_ownership_tag(self, backend, calendar) -> None:
        task_id = uuid.uuid4()
        draft = EventDraft(
            summary="Deep work",
            start_at=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
            end_at=datetime(2026, 7, 10, 11, 0, tzinfo=UTC),
            description="Focus block",
            agent_task_id=task_id,
        )
        created = backend.create_event(draft)

        (ical_text,) = calendar.added_icals
        assert "X-HEALTHMES:1" in ical_text
        assert f"X-HEALTHMES-TASK-ID:{task_id}" in ical_text

        assert created.external_id.endswith("@healthmes")
        assert created.is_agent_created
        assert created.start_at == datetime(2026, 7, 10, 9, 0, tzinfo=UTC)

        # Round-trip through parsing keeps the tag readable.
        stored = calendar.objects[created.external_id]
        events, _ = backend.list_changes(None)
        assert events[0].is_agent_created
        assert stored.uid == created.external_id


class TestUpdateAndDelete:
    def test_update_rewrites_times_and_saves(self, backend, calendar) -> None:
        calendar.put(make_component("mine@healthmes", agent=True))
        updated = backend.update_event(
            "mine@healthmes",
            start_at=datetime(2026, 7, 10, 14, 0, tzinfo=UTC),
            end_at=datetime(2026, 7, 10, 15, 0, tzinfo=UTC),
            summary="Deep work (moved)",
        )
        assert calendar.saved_objects == ["mine@healthmes"]
        assert updated.start_at == datetime(2026, 7, 10, 14, 0, tzinfo=UTC)
        assert updated.summary == "Deep work (moved)"
        component = calendar.objects["mine@healthmes"].icalendar_component
        assert component.get("DTSTART").dt == datetime(2026, 7, 10, 14, 0, tzinfo=UTC)

    def test_update_refuses_untagged_event(self, backend, calendar) -> None:
        calendar.put(make_component("theirs", agent=False))
        with pytest.raises(OwnershipError):
            backend.update_event("theirs", summary="hijack")
        assert calendar.saved_objects == []

    def test_update_missing_event_raises_not_found(self, backend) -> None:
        with pytest.raises(EventNotFoundError):
            backend.update_event("ghost", summary="x")

    def test_delete_checks_ownership_then_deletes(self, backend, calendar) -> None:
        calendar.put(make_component("mine@healthmes", agent=True))
        backend.delete_event("mine@healthmes")
        assert calendar.deleted_uids == ["mine@healthmes"]
        assert "mine@healthmes" not in calendar.objects

    def test_delete_refuses_untagged_event(self, backend, calendar) -> None:
        calendar.put(make_component("theirs", agent=False))
        with pytest.raises(OwnershipError):
            backend.delete_event("theirs")
        assert calendar.deleted_uids == []
