"""CalDAV backend tests against fake collection objects (no network).

Components are built with the real ``icalendar`` library so parsing and
serialization exercise the same code paths as against caldav.icloud.com.
"""

import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import icalendar
import pytest
from caldav import Event as CalDavEvent
from caldav.lib.url import URL

from healthmes.calendars.base import (
    CalendarConflictError,
    CalendarError,
    CalendarEventIdentity,
    EventDraft,
    EventNotFoundError,
    HealthmesEventKind,
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


class FakeDavResponse:
    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = headers or {}


class FakeDavClient:
    def __init__(self, calendar: "FakeCalDavCalendar") -> None:
        self.calendar = calendar
        self.url = URL("https://caldav.invalid/")

    def request(
        self,
        url: str,
        method: str = "GET",
        body: str = "",
        headers: dict[str, str] | None = None,
    ) -> FakeDavResponse:
        headers = headers or {}
        if method == "PUT" and headers.get("If-None-Match") == "*":
            self.calendar.conditional_create_calls.append(
                (url, body, dict(headers))
            )
            parsed = icalendar.Calendar.from_ical(body)
            (component,) = [
                item for item in parsed.subcomponents if item.name == "VEVENT"
            ]
            uid = str(component.get("UID"))
            if uid in self.calendar.objects:
                return FakeDavResponse(412)
            self.calendar.added_icals.append(body)
            self.calendar.put(component, etag='"fresh"')
            return FakeDavResponse(201, {"Etag": '"fresh"'})
        uid = url.rsplit("/", 1)[-1]
        self.calendar.conditional_delete_calls.append((uid, method, headers))
        obj = self.calendar.objects.get(uid)
        if obj is None:
            return FakeDavResponse(404)
        if headers.get("If-Match") != obj.etag:
            return FakeDavResponse(412)
        self.calendar.deleted_uids.append(uid)
        self.calendar.objects.pop(uid)
        return FakeDavResponse(204)

    def put(self, url, body: str, headers: dict[str, str]) -> FakeDavResponse:
        self.calendar.conditional_update_calls.append(
            (str(url), body, dict(headers))
        )
        return FakeDavResponse(204, {"Etag": '"etag-after-put"'})


def make_component(
    uid: str,
    *,
    summary: str = "Standup",
    start: object = None,
    end: object = None,
    agent: bool = False,
    task_id: uuid.UUID | None = None,
    healthmes_kind: str | None = None,
    organizer: str | None = None,
    attendee: str | None = None,
    recurring: bool = False,
    status: str | None = None,
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
    if healthmes_kind is not None:
        component["X-HEALTHMES-KIND"] = healthmes_kind
    if organizer is not None:
        component.add("organizer", organizer)
    if attendee is not None:
        component.add("attendee", attendee)
    if recurring:
        component.add("rrule", {"freq": "daily"})
    if status is not None:
        component.add("status", status)
    return component


class FakeCalDavObject:
    def __init__(
        self, component: icalendar.Event, etag: str | None, calendar: "FakeCalDavCalendar"
    ) -> None:
        self.icalendar_component = component
        self.props = {ETAG_PROPERTY_TAG: etag} if etag else {}
        self._calendar = calendar
        self.client = FakeDavClient(calendar)
        self.url = f"https://caldav.invalid/{self.uid}"
        self.saved = False

    @property
    def etag(self) -> str | None:
        return self.props.get(ETAG_PROPERTY_TAG)

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
        self.url = URL("https://caldav.invalid/")
        self.ctag = ctag
        self.objects: dict[str, FakeCalDavObject] = {}
        self.events_calls = 0
        self.event_by_uid_calls = 0
        self.ctag_requests: list[object] = []
        self.added_icals: list[str] = []
        self.saved_objects: list[str] = []
        self.deleted_uids: list[str] = []
        self.conditional_create_calls: list[
            tuple[str, str, dict[str, str]]
        ] = []
        self.conditional_delete_calls: list[tuple[str, str, dict[str, str]]] = []
        self.conditional_update_calls: list[tuple[str, str, dict[str, str]]] = []

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
        self.event_by_uid_calls += 1
        if uid not in self.objects:
            raise NotFoundError(uid)
        return self.objects[uid]

    def conditional_create(self, external_id: str, ical: str) -> None:
        self.add_event(ical=ical, no_overwrite=True)

    def add_event(
        self,
        ical: str | None = None,
        *,
        no_overwrite: bool = False,
        **_: object,
    ) -> FakeCalDavObject:
        assert ical is not None
        parsed = icalendar.Calendar.from_ical(ical)
        (component,) = [c for c in parsed.subcomponents if c.name == "VEVENT"]
        uid = str(component.get("UID"))
        if no_overwrite and uid in self.objects:
            raise RuntimeError(f"precondition failed for {uid}")
        self.added_icals.append(ical)
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

    def test_cancelled_event_is_reported_deleted(self, backend, calendar) -> None:
        calendar.put(make_component("a"), etag='"a1"')
        _, state = backend.list_changes(None)

        calendar.ctag = "ctag-2"
        calendar.objects["a"] = FakeCalDavObject(
            make_component("a", status="CANCELLED"), '"a2"', calendar
        )
        events, next_state = backend.list_changes(state)

        (cancelled,) = events
        assert cancelled.external_id == "a"
        assert cancelled.deleted
        assert next_state["fingerprints"] == {"a": '"a2"'}

    def test_cancelled_event_removal_remains_a_deletion_notice(
        self, backend, calendar
    ) -> None:
        calendar.put(make_component("a", status="CANCELLED"), etag='"a1"')
        events, state = backend.list_changes(None)
        assert len(events) == 1 and events[0].deleted

        calendar.ctag = "ctag-2"
        del calendar.objects["a"]
        events, next_state = backend.list_changes(state)

        (removed,) = events
        assert removed.external_id == "a"
        assert removed.deleted
        assert next_state["fingerprints"] == {}

    def test_cancelled_event_reactivation_is_reported_live(
        self, backend, calendar
    ) -> None:
        calendar.put(make_component("a", status="CANCELLED"), etag='"a1"')
        _, state = backend.list_changes(None)

        calendar.ctag = "ctag-2"
        calendar.objects["a"] = FakeCalDavObject(
            make_component("a", status="CONFIRMED"), '"a2"', calendar
        )
        events, next_state = backend.list_changes(state)

        (reactivated,) = events
        assert reactivated.external_id == "a"
        assert not reactivated.deleted
        assert reactivated.status == "confirmed"
        assert next_state["fingerprints"] == {"a": '"a2"'}

    def test_missing_etag_falls_back_to_content_hash(self, backend, calendar) -> None:
        calendar.put(make_component("a"), etag=None)
        _, state = backend.list_changes(None)
        assert len(state["fingerprints"]["a"]) == 64  # sha256 hex

        # Same content, ctag bumped: full rescan finds no changes.
        calendar.ctag = "ctag-2"
        events, _ = backend.list_changes(state)
        assert events == []

    def test_detached_recurrence_exception_has_independent_identity(
        self, backend, calendar
    ) -> None:
        master = make_component("series", recurring=True)
        exception = make_component(
            "series",
            summary="Standup (moved)",
            start=datetime(2026, 7, 10, 11, tzinfo=UTC),
            end=datetime(2026, 7, 10, 11, 30, tzinfo=UTC),
        )
        exception.add(
            "recurrence-id",
            datetime(2026, 7, 10, 9, tzinfo=UTC),
        )
        payload = icalendar.Calendar()
        payload.add("prodid", "-//HealthMes Test//EN")
        payload.add("version", "2.0")
        payload.add_component(master)
        payload.add_component(exception)
        calendar.objects["series"] = CalDavEvent(
            client=FakeDavClient(calendar),
            url="https://caldav.invalid/series.ics",
            data=payload.to_ical().decode(),
            parent=calendar,
            props={ETAG_PROPERTY_TAG: '"series-v1"'},
        )

        events, state = backend.list_changes(None)

        assert {event.external_id for event in events} == {
            "series",
            "series::recurrence::2026-07-10T09:00:00+00:00",
        }
        detached = next(
            event for event in events if "::recurrence::" in event.external_id
        )
        assert detached.summary == "Standup (moved)"
        assert detached.start_at == datetime(2026, 7, 10, 11, tzinfo=UTC)
        assert len(state["fingerprints"]) == 2

    def test_detached_exception_content_has_its_own_fingerprint(
        self, backend, calendar
    ) -> None:
        master = make_component("series", recurring=True)
        exception = make_component(
            "series",
            summary="Standup (moved)",
            start=datetime(2026, 7, 10, 11, tzinfo=UTC),
            end=datetime(2026, 7, 10, 11, 30, tzinfo=UTC),
        )
        exception.add(
            "recurrence-id",
            datetime(2026, 7, 10, 9, tzinfo=UTC),
        )

        def put_series() -> None:
            payload = icalendar.Calendar()
            payload.add("prodid", "-//HealthMes Test//EN")
            payload.add("version", "2.0")
            payload.add_component(master)
            payload.add_component(exception)
            calendar.objects["series"] = CalDavEvent(
                client=FakeDavClient(calendar),
                url="https://caldav.invalid/series.ics",
                data=payload.to_ical().decode(),
                parent=calendar,
                # Some REPORT responses omit ETags; content hashing must still
                # distinguish the master from a detached override.
                props={},
            )

        put_series()
        _, state = backend.list_changes(None)
        master_fingerprint = state["fingerprints"]["series"]
        exception_id = "series::recurrence::2026-07-10T09:00:00+00:00"
        exception_fingerprint = state["fingerprints"][exception_id]
        assert master_fingerprint != exception_fingerprint

        exception["SUMMARY"] = "Standup (moved again)"
        calendar.ctag = "ctag-2"
        put_series()

        events, next_state = backend.list_changes(state)

        assert [event.external_id for event in events] == [exception_id]
        assert next_state["fingerprints"]["series"] == master_fingerprint
        assert next_state["fingerprints"][exception_id] != exception_fingerprint


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
        assert event.is_all_day

    def test_all_day_without_dtend_defaults_to_one_day(self, backend, calendar) -> None:
        component = icalendar.Event()
        component.add("uid", "open-ended")
        component.add("dtstamp", datetime(2026, 7, 1, tzinfo=UTC))
        component.add("summary", "Holiday")
        component.add("dtstart", date(2026, 7, 10))
        calendar.put(component)
        (event,), _ = backend.list_changes(None)
        assert event.end_at == datetime(2026, 7, 11, tzinfo=UTC)
        assert event.is_all_day

    @pytest.mark.parametrize(
        ("local_date", "expected_start", "expected_end"),
        [
            (
                date(2026, 3, 8),
                datetime(2026, 3, 8, 5, tzinfo=UTC),
                datetime(2026, 3, 9, 4, tzinfo=UTC),
            ),
            (
                date(2026, 11, 1),
                datetime(2026, 11, 1, 4, tzinfo=UTC),
                datetime(2026, 11, 2, 5, tzinfo=UTC),
            ),
        ],
    )
    def test_all_day_without_dtend_uses_next_local_midnight_across_dst(
        self,
        calendar,
        local_date,
        expected_start,
        expected_end,
    ) -> None:
        backend = CalDavCalendarBackend(
            calendar,
            local_timezone=ZoneInfo("America/New_York"),
        )
        component = icalendar.Event()
        component.add("uid", f"dst-{local_date.isoformat()}")
        component.add("dtstamp", datetime(2026, 7, 1, tzinfo=UTC))
        component.add("summary", "DST-safe holiday")
        component.add("dtstart", local_date)
        calendar.put(component)

        (event,), _ = backend.list_changes(None)

        assert event.start_at == expected_start
        assert event.end_at == expected_end
        assert event.is_all_day

    def test_all_day_uses_configured_local_midnight(self, calendar) -> None:
        backend = CalDavCalendarBackend(calendar, local_timezone=KST)
        calendar.put(
            make_component(
                "kst-allday",
                start=date(2026, 7, 10),
                end=date(2026, 7, 11),
            )
        )

        (event,), _ = backend.list_changes(None)

        assert event.start_at == datetime(2026, 7, 9, 15, tzinfo=UTC)
        assert event.end_at == datetime(2026, 7, 10, 15, tzinfo=UTC)

    def test_intake_safety_metadata_is_parsed(self, calendar) -> None:
        backend = CalDavCalendarBackend(
            calendar,
            owner_email="owner@example.com",
        )
        calendar.put(
            make_component(
                "owned",
                organizer="mailto:owner@example.com",
                attendee="mailto:guest@example.com",
                recurring=True,
            )
        )

        (event,), _ = backend.list_changes(None)

        assert event.organizer_self
        assert event.has_attendees
        assert event.is_recurring
        assert event.event_type == "default"

    @pytest.mark.parametrize("property_name", ["rdate", "recurrence-id"])
    def test_recurrence_variants_are_marked_recurring(
        self, backend, calendar, property_name
    ) -> None:
        component = make_component("recurrence-variant")
        if property_name == "rdate":
            component.add("rdate", datetime(2026, 7, 10, 9, tzinfo=UTC))
        else:
            component.add("recurrence-id", datetime(2026, 7, 9, 9, tzinfo=UTC))
        calendar.put(component)

        (event,), _ = backend.list_changes(None)

        assert event.is_recurring

    def test_foreign_organizer_is_not_self(self, calendar) -> None:
        backend = CalDavCalendarBackend(
            calendar,
            owner_email="owner@example.com",
        )
        calendar.put(
            make_component(
                "foreign",
                organizer="mailto:other@example.com",
            )
        )

        (event,), _ = backend.list_changes(None)

        assert not event.organizer_self

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
        assert created.etag == '"fresh"'
        assert created.start_at == datetime(2026, 7, 10, 9, 0, tzinfo=UTC)
        assert calendar.event_by_uid_calls == 1

        # Round-trip through parsing keeps the tag readable.
        stored = calendar.objects[created.external_id]
        events, _ = backend.list_changes(None)
        assert events[0].is_agent_created
        assert stored.uid == created.external_id

    def test_create_requires_server_read_back_etag(
        self, backend, calendar, monkeypatch
    ) -> None:
        original_add = calendar.add_event

        def add_without_server_etag(*args, **kwargs):
            created = original_add(*args, **kwargs)
            created.props.clear()
            return created

        monkeypatch.setattr(calendar, "add_event", add_without_server_etag)

        with pytest.raises(CalendarError, match="server ETag"):
            backend.create_event(
                EventDraft(
                    summary="No ETag",
                    start_at=datetime(2026, 7, 10, 9, tzinfo=UTC),
                    end_at=datetime(2026, 7, 10, 10, tzinfo=UTC),
                )
            )

    def test_identity_create_uses_atomic_if_none_match(
        self, calendar
    ) -> None:
        calendar.conditional_create = None
        calendar.client = FakeDavClient(calendar)
        backend = CalDavCalendarBackend(calendar)
        identity = CalendarEventIdentity(
            kind=HealthmesEventKind.ACTUAL_SLEEP,
            source="oura",
            source_key="oura:2026-07-26",
        )
        draft = EventDraft(
            summary="Sleep",
            start_at=datetime(2026, 7, 25, 23, tzinfo=UTC),
            end_at=datetime(2026, 7, 26, 7, tzinfo=UTC),
            identity=identity,
        )

        created = backend.create_event(draft)

        assert created.identity == identity
        assert len(calendar.conditional_create_calls) == 1
        url, _, headers = calendar.conditional_create_calls[0]
        assert url.endswith(".ics")
        assert headers["If-None-Match"] == "*"
        assert headers["Content-Type"] == "text/calendar; charset=utf-8"

        with pytest.raises(CalendarConflictError):
            backend.create_event(draft)


class TestConnect:
    def test_unnamed_calendar_requires_unambiguous_collection(
        self, monkeypatch
    ) -> None:
        principal = SimpleNamespace(
            calendars=lambda: [object(), object()],
        )
        monkeypatch.setattr(
            "caldav.DAVClient",
            lambda **_: SimpleNamespace(principal=lambda: principal),
        )

        with pytest.raises(CalendarError, match="multiple caldav calendars"):
            CalDavCalendarBackend.connect(
                username="owner@example.com",
                app_password="app-password",
            )

    def test_actual_sleep_identity_round_trips_x_properties(
        self, backend, calendar
    ) -> None:
        identity = CalendarEventIdentity(
            kind=HealthmesEventKind.ACTUAL_SLEEP,
            source="oura",
            source_key="oura:2026-07-26",
        )
        created = backend.create_event(
            EventDraft(
                summary="수면 (실제)",
                start_at=datetime(2026, 7, 25, 23, tzinfo=UTC),
                end_at=datetime(2026, 7, 26, 7, tzinfo=UTC),
                identity=identity,
            )
        )

        (ical_text,) = calendar.added_icals
        assert "X-HEALTHMES-KIND:actual_sleep" in ical_text
        assert "X-HEALTHMES-SOURCE:oura" in ical_text
        assert "X-HEALTHMES-SOURCE-KEY:oura:2026-07-26" in ical_text
        (parsed,), _ = backend.list_changes(None)
        assert created.identity == identity
        assert parsed.identity == identity

    def test_identity_collision_requires_a_local_recovery_intent(
        self, backend, calendar
    ) -> None:
        # Given
        draft = EventDraft(
            summary="수면 (실제)",
            start_at=datetime(2026, 7, 25, 23, tzinfo=UTC),
            end_at=datetime(2026, 7, 26, 7, tzinfo=UTC),
            identity=CalendarEventIdentity(
                kind=HealthmesEventKind.ACTUAL_SLEEP,
                source="oura",
                source_key="oura:2026-07-26",
            ),
        )

        # When
        first = backend.create_event(draft)
        with pytest.raises(CalendarConflictError):
            backend.create_event(draft)

        # Then
        assert first.identity == draft.identity
        assert len(calendar.objects) == 1
        assert len(calendar.added_icals) == 1

    def test_identity_create_does_not_overwrite_a_concurrent_foreign_event(
        self, backend, calendar, monkeypatch
    ) -> None:
        # Given
        draft = EventDraft(
            summary="수면 (실제)",
            start_at=datetime(2026, 7, 25, 23, tzinfo=UTC),
            end_at=datetime(2026, 7, 26, 7, tzinfo=UTC),
            identity=CalendarEventIdentity(
                kind=HealthmesEventKind.ACTUAL_SLEEP,
                source="oura",
                source_key="oura:2026-07-26",
            ),
        )
        add_event = calendar.add_event

        def raced_add_event(
            ical: str | None = None,
            *,
            no_overwrite: bool = False,
            **kwargs: object,
        ) -> FakeCalDavObject:
            assert ical is not None
            parsed = icalendar.Calendar.from_ical(ical)
            (component,) = [c for c in parsed.subcomponents if c.name == "VEVENT"]
            uid = str(component.get("UID"))
            calendar.put(make_component(uid, summary="Foreign"))
            return add_event(
                ical=ical,
                no_overwrite=no_overwrite,
                **kwargs,
            )

        monkeypatch.setattr(calendar, "add_event", raced_add_event)

        # When / Then
        with pytest.raises(OwnershipError):
            backend.create_event(draft)
        stored = next(iter(calendar.objects.values())).icalendar_component
        assert str(stored.get("SUMMARY")) == "Foreign"
        assert stored.get("X-HEALTHMES") is None


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

    def test_update_uses_the_read_only_remote_etag_for_if_match(
        self, backend, calendar
    ) -> None:
        obj = calendar.put(
            make_component("mine@healthmes", agent=True),
            etag='"remote-v1"',
        )

        updated = backend.update_event(
            "mine@healthmes",
            summary="corrected",
            expected_etag='"remote-v1"',
        )

        assert obj.saved
        assert updated.summary == "corrected"
        assert obj.etag == '"remote-v1"'

    def test_real_caldav_save_sends_the_read_only_etag_as_if_match(
        self, backend, calendar
    ) -> None:
        component = make_component("mine@healthmes", agent=True)
        obj = CalDavEvent(
            client=FakeDavClient(calendar),
            url="https://caldav.invalid/mine@healthmes",
            data=component.to_ical().decode(),
            parent=calendar,
            props={ETAG_PROPERTY_TAG: '"remote-v1"'},
        )
        calendar.objects["mine@healthmes"] = obj

        updated = backend.update_event(
            "mine@healthmes",
            summary="corrected",
            expected_etag='"remote-v1"',
        )

        assert updated.summary == "corrected"
        assert len(calendar.conditional_update_calls) == 1
        _, _, headers = calendar.conditional_update_calls[0]
        assert headers["if-match"] == '"remote-v1"'

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

    def test_delete_requires_expected_healthmes_kind(self, backend, calendar) -> None:
        calendar.put(
            make_component(
                "mine@healthmes",
                agent=True,
                healthmes_kind="actual_sleep",
            )
        )
        with pytest.raises(OwnershipError, match="planned_sleep"):
            backend.delete_event(
                "mine@healthmes",
                expected_kind=HealthmesEventKind.PLANNED_SLEEP,
            )
        assert calendar.deleted_uids == []

    def test_delete_accepts_matching_healthmes_kind(self, backend, calendar) -> None:
        calendar.put(
            make_component(
                "mine@healthmes",
                agent=True,
                healthmes_kind="planned_sleep",
            )
        )
        backend.delete_event(
            "mine@healthmes",
            expected_kind=HealthmesEventKind.PLANNED_SLEEP,
        )
        assert calendar.deleted_uids == ["mine@healthmes"]

    def test_delete_sends_mirror_etag_as_if_match(self, backend, calendar) -> None:
        calendar.put(
            make_component(
                "mine@healthmes",
                agent=True,
                healthmes_kind="planned_sleep",
            ),
            etag='"etag-1"',
        )
        backend.delete_event(
            "mine@healthmes",
            expected_kind=HealthmesEventKind.PLANNED_SLEEP,
            expected_etag='"etag-1"',
        )
        assert calendar.conditional_delete_calls == [
            (
                "mine@healthmes",
                "DELETE",
                {"If-Match": '"etag-1"'},
            )
        ]
        assert calendar.deleted_uids == ["mine@healthmes"]

    def test_delete_blocks_when_neither_mirror_nor_remote_has_an_etag(
        self, backend, calendar
    ) -> None:
        calendar.put(
            make_component(
                "mine@healthmes",
                agent=True,
                healthmes_kind="planned_sleep",
            ),
            etag=None,
        )

        with pytest.raises(CalendarConflictError, match="ETag"):
            backend.delete_event(
                "mine@healthmes",
                expected_kind=HealthmesEventKind.PLANNED_SLEEP,
            )

        assert calendar.conditional_delete_calls == []
        assert calendar.deleted_uids == []

    def test_update_rejects_stale_mirror_etag(self, backend, calendar) -> None:
        obj = calendar.put(make_component("mine@healthmes", agent=True), etag='"remote-v2"')
        with pytest.raises(CalendarConflictError):
            backend.update_event(
                "mine@healthmes",
                summary="corrected",
                expected_etag='"mirror-v1"',
            )
        assert not obj.saved

    def test_delete_rejects_stale_mirror_etag(self, backend, calendar) -> None:
        calendar.put(
            make_component(
                "mine@healthmes",
                agent=True,
                healthmes_kind="planned_sleep",
            ),
            etag='"remote-v2"',
        )
        with pytest.raises(CalendarConflictError):
            backend.delete_event(
                "mine@healthmes",
                expected_kind=HealthmesEventKind.PLANNED_SLEEP,
                expected_etag='"mirror-v1"',
            )
        assert calendar.deleted_uids == []
