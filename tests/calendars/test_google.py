"""Google backend tests against a fake ``calendar v3`` service (no network)."""

import json
import uuid
from datetime import UTC, datetime

import pytest

from healthmes.calendars.base import (
    CalendarAuthError,
    CalendarConflictError,
    CalendarEventIdentity,
    ConfirmedExternalTimeChange,
    EventDraft,
    EventNotFoundError,
    HealthmesEventKind,
    OwnershipError,
)
from healthmes.calendars.google import (
    GoogleCalendarBackend,
    ensure_credentials,
    google_token_path,
    load_credentials,
)
from healthmes.calendars.state import (
    SyncCoverageKind,
    sync_state_coverage,
    with_sync_state_coverage,
)

# --- fake googleapiclient plumbing ------------------------------------------


class FakeStatusError(Exception):
    """Stub for googleapiclient HttpError: only the status attributes matter."""

    def __init__(self, status: int) -> None:
        super().__init__(f"http status {status}")
        self.status_code = status


class _FakeRequest:
    """Mirrors googleapiclient ``HttpRequest``: a mutable ``headers`` dict that
    the backend can set (``If-Match``) before ``execute``."""

    def __init__(self, fn) -> None:
        self._fn = fn
        self.headers: dict[str, str] = {}

    def execute(self):
        return self._fn(self.headers)


class _FakeEvents:
    def __init__(self, service: "FakeGoogleService") -> None:
        self._service = service

    def list(self, **params):
        self._service.list_calls.append(params)
        return _FakeRequest(lambda headers: self._service.next_list_response())

    def get(self, calendarId, eventId):  # noqa: N803 - google client casing
        self._service.get_calls.append((calendarId, eventId))
        return _FakeRequest(lambda headers: self._service.get_event(eventId))

    def insert(self, calendarId, body):  # noqa: N803
        self._service.insert_calls.append((calendarId, body))
        return _FakeRequest(lambda headers: self._service.insert_event(body))

    def patch(self, calendarId, eventId, body):  # noqa: N803
        self._service.patch_calls.append((calendarId, eventId, body))
        return _FakeRequest(
            lambda headers: self._service.patch_event(eventId, body, headers)
        )

    def delete(self, calendarId, eventId):  # noqa: N803
        self._service.delete_calls.append((calendarId, eventId))
        return _FakeRequest(
            lambda headers: self._service.delete_event(eventId, headers)
        )


class FakeGoogleService:
    """Canned-response calendar v3 service double."""

    def __init__(self) -> None:
        self.list_responses: list[object] = []  # dict pages or Exception to raise
        self.stored_events: dict[str, dict] = {}
        self.list_calls: list[dict] = []
        self.get_calls: list[tuple] = []
        self.insert_calls: list[tuple] = []
        self.patch_calls: list[tuple] = []
        self.delete_calls: list[tuple] = []
        self.write_headers: list[dict] = []  # headers seen by patch/delete execs
        #: When True the next conditional write 412s (simulate a concurrent
        #: remote edit invalidating the etag we read).
        self.fail_precondition = False

    def events(self) -> _FakeEvents:
        return _FakeEvents(self)

    def next_list_response(self) -> dict:
        response = self.list_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get_event(self, event_id: str) -> dict:
        if event_id not in self.stored_events:
            raise FakeStatusError(404)
        return self.stored_events[event_id]

    def insert_event(self, body: dict) -> dict:
        event_id = body.get("id") or f"generated-{len(self.stored_events) + 1}"
        if event_id in self.stored_events:
            raise FakeStatusError(409)
        stored = {"id": event_id, "etag": '"1"', "status": "confirmed", **body}
        self.stored_events[event_id] = stored
        return stored

    def patch_event(self, event_id: str, body: dict, headers: dict) -> dict:
        self.write_headers.append(dict(headers))
        self._enforce_precondition(event_id, headers)
        if event_id not in self.stored_events:
            raise FakeStatusError(404)
        stored = dict(self.stored_events[event_id])
        stored.update(body)
        stored["etag"] = '"patched"'
        self.stored_events[event_id] = stored
        return stored

    def delete_event(self, event_id: str, headers: dict) -> None:
        self.write_headers.append(dict(headers))
        self._enforce_precondition(event_id, headers)
        if event_id not in self.stored_events:
            raise FakeStatusError(404)
        del self.stored_events[event_id]

    def _enforce_precondition(self, event_id: str, headers: dict) -> None:
        """Reject a conditional write whose ``If-Match`` no longer matches."""
        if_match = headers.get("If-Match")
        if if_match is None:
            return
        if self.fail_precondition:
            raise FakeStatusError(412)
        current = self.stored_events.get(event_id)
        if current is not None and current.get("etag") != if_match:
            raise FakeStatusError(412)


@pytest.fixture
def service() -> FakeGoogleService:
    return FakeGoogleService()


@pytest.fixture
def backend(service: FakeGoogleService) -> GoogleCalendarBackend:
    return GoogleCalendarBackend(service, calendar_id="primary", lookback_days=7)


def api_event(
    event_id: str,
    *,
    summary: str = "Standup",
    start: str = "2026-07-09T09:00:00+09:00",
    end: str = "2026-07-09T09:30:00+09:00",
    agent: bool = False,
    task_id: uuid.UUID | None = None,
    healthmes_kind: str | None = None,
    status: str = "confirmed",
    etag: str = '"e1"',
    organizer_self: bool = False,
    attendees: list[dict] | None = None,
    recurrence: list[str] | None = None,
    recurring_event_id: str | None = None,
    event_type: str | None = None,
    all_day: bool = False,
    locked: bool = False,
) -> dict:
    item: dict = {"id": event_id, "status": status, "etag": etag, "summary": summary}
    if status != "cancelled":
        if all_day:
            item["start"] = {"date": start[:10]}
            item["end"] = {"date": end[:10]}
        else:
            item["start"] = {"dateTime": start}
            item["end"] = {"dateTime": end}
    if organizer_self:
        item["organizer"] = {"email": "owner@example.invalid", "self": True}
    if attendees is not None:
        item["attendees"] = attendees
    if recurrence is not None:
        item["recurrence"] = recurrence
    if recurring_event_id is not None:
        item["recurringEventId"] = recurring_event_id
    if event_type is not None:
        item["eventType"] = event_type
    if locked:
        item["locked"] = True
    if agent:
        private = {"healthmes": "1"}
        if task_id is not None:
            private["healthmes_task_id"] = str(task_id)
        if healthmes_kind is not None:
            private["healthmes_kind"] = healthmes_kind
        item["extendedProperties"] = {"private": private}
    return item


# --- change feed -------------------------------------------------------------


class TestInitialFullSync:
    def test_pages_are_drained_and_token_stored(self, backend, service) -> None:
        service.list_responses = [
            {"items": [api_event("a")], "nextPageToken": "page-2"},
            {"items": [api_event("b")], "nextSyncToken": "tok-1"},
        ]
        events, state = backend.list_changes(None)

        assert [event.external_id for event in events] == ["a", "b"]
        assert state["sync_token"] == "tok-1"
        assert state["known_ids"] == {"a": '"e1"', "b": '"e1"'}

        first, second = service.list_calls
        assert first["singleEvents"] is True
        assert "timeMin" in first and "timeMax" in first
        assert "syncToken" not in first
        assert second["pageToken"] == "page-2"
        assert sync_state_coverage(state) == (
            SyncCoverageKind.BOUNDED_WINDOW,
            datetime.fromisoformat(first["timeMin"]),
            datetime.fromisoformat(first["timeMax"]),
        )

    def test_no_synthetic_deletions_on_bootstrap(self, backend, service) -> None:
        service.list_responses = [{"items": [api_event("a")], "nextSyncToken": "tok-1"}]
        events, _ = backend.list_changes(None)
        assert all(not event.deleted for event in events)


class TestIncrementalSync:
    def test_uses_sync_token_and_reports_cancellations(self, backend, service) -> None:
        service.list_responses = [
            {
                "items": [
                    api_event("a", summary="Standup (moved)"),
                    api_event("b", status="cancelled"),
                ],
                "nextSyncToken": "tok-2",
            }
        ]
        coverage_start = datetime(2026, 8, 1, tzinfo=UTC)
        coverage_end = datetime(2027, 8, 1, tzinfo=UTC)
        previous = with_sync_state_coverage(
            {
                "sync_token": "tok-1",
                "known_ids": {"a": '"e0"', "b": '"e0"'},
            },
            kind=SyncCoverageKind.BOUNDED_WINDOW,
            start=coverage_start,
            end=coverage_end,
        )
        events, state = backend.list_changes(previous)

        (call,) = service.list_calls
        assert call["syncToken"] == "tok-1"
        assert "timeMin" not in call and "timeMax" not in call

        live, gone = events
        assert live.external_id == "a" and not live.deleted
        assert gone.external_id == "b" and gone.deleted
        assert state["sync_token"] == "tok-2"
        assert state["known_ids"] == {"a": '"e1"'}
        assert sync_state_coverage(state) == (
            SyncCoverageKind.BOUNDED_WINDOW,
            coverage_start,
            coverage_end,
        )

    def test_gone_410_falls_back_to_full_resync(self, backend, service) -> None:
        service.list_responses = [
            FakeStatusError(410),
            {"items": [api_event("a")], "nextSyncToken": "tok-3"},
        ]
        previous = with_sync_state_coverage(
            {
                "sync_token": "expired",
                "known_ids": {"a": '"e0"', "stale": '"e0"'},
            },
            kind=SyncCoverageKind.BOUNDED_WINDOW,
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2027, 8, 1, tzinfo=UTC),
        )
        events, state = backend.list_changes(previous)

        # The event that vanished while the token was invalid is synthesized
        # as a deletion; the surviving event is re-delivered.
        assert {event.external_id: event.deleted for event in events} == {
            "a": False,
            "stale": True,
        }
        assert state["sync_token"] == "tok-3"
        assert state["known_ids"] == {"a": '"e1"'}
        assert sync_state_coverage(state)[0] is SyncCoverageKind.BOUNDED_WINDOW
        assert "syncToken" in service.list_calls[0]
        assert "timeMin" in service.list_calls[1]

    def test_legacy_cursor_without_coverage_forces_full_refresh(
        self,
        backend,
        service,
    ) -> None:
        service.list_responses = [
            {"items": [api_event("a")], "nextSyncToken": "tok-fresh"}
        ]

        _, state = backend.list_changes(
            {"sync_token": "legacy", "known_ids": {"a": '"e0"'}}
        )

        (call,) = service.list_calls
        assert "syncToken" not in call
        assert "timeMin" in call and "timeMax" in call
        assert state["sync_token"] == "tok-fresh"
        assert sync_state_coverage(state)[0] is SyncCoverageKind.BOUNDED_WINDOW

    def test_non_410_errors_propagate(self, backend, service) -> None:
        service.list_responses = [FakeStatusError(500)]
        with pytest.raises(FakeStatusError):
            backend.list_changes(
                with_sync_state_coverage(
                    {"sync_token": "tok", "known_ids": {}},
                    kind=SyncCoverageKind.BOUNDED_WINDOW,
                    start=datetime(2026, 8, 1, tzinfo=UTC),
                    end=datetime(2027, 8, 1, tzinfo=UTC),
                )
            )


class TestEventParsing:
    def test_timed_event_normalized_to_utc(self, backend, service) -> None:
        service.list_responses = [{"items": [api_event("a")], "nextSyncToken": "t"}]
        (event,), _ = backend.list_changes(None)
        assert event.start_at == datetime(2026, 7, 9, 0, 0, tzinfo=UTC)
        assert event.end_at == datetime(2026, 7, 9, 0, 30, tzinfo=UTC)
        assert event.etag == '"e1"'
        assert not event.is_agent_created

    def test_all_day_event_maps_to_midnight_utc(self, backend, service) -> None:
        item = {
            "id": "allday",
            "status": "confirmed",
            "etag": '"e1"',
            "summary": "Conference",
            "start": {"date": "2026-07-10"},
            "end": {"date": "2026-07-11"},
        }
        service.list_responses = [{"items": [item], "nextSyncToken": "t"}]
        (event,), _ = backend.list_changes(None)
        assert event.start_at == datetime(2026, 7, 10, tzinfo=UTC)
        assert event.end_at == datetime(2026, 7, 11, tzinfo=UTC)

    def test_agent_tag_and_task_id_detected(self, backend, service) -> None:
        task_id = uuid.uuid4()
        service.list_responses = [
            {"items": [api_event("mine", agent=True, task_id=task_id)], "nextSyncToken": "t"}
        ]
        (event,), _ = backend.list_changes(None)
        assert event.is_agent_created
        assert event.agent_task_id == task_id

    def test_google_eligibility_metadata_is_normalized(self, backend, service) -> None:
        service.list_responses = [
            {
                "items": [
                    api_event(
                        "a",
                        organizer_self=True,
                        attendees=[{"email": "guest@example.invalid"}],
                        recurrence=["RRULE:FREQ=WEEKLY"],
                        event_type="default",
                        locked=True,
                    ),
                    api_event("b", all_day=True),
                ],
                "nextSyncToken": "t",
            }
        ]
        first, second = backend.list_changes(None)[0]
        assert first.organizer_self
        assert first.has_attendees
        assert first.is_recurring
        assert first.event_type == "default"
        assert first.is_locked
        assert first.status == "confirmed"
        assert not first.is_all_day
        assert second.is_all_day

    def test_google_recurring_instance_is_recurring_metadata(self, backend, service) -> None:
        service.list_responses = [
            {
                "items": [api_event("a", recurring_event_id="series-1")],
                "nextSyncToken": "t",
            }
        ]
        (event,), _ = backend.list_changes(None)
        assert event.is_recurring


# --- agent writes -------------------------------------------------------------


class TestCreateEvent:
    def test_insert_body_carries_ownership_tag(self, backend, service) -> None:
        task_id = uuid.uuid4()
        draft = EventDraft(
            summary="Deep work",
            start_at=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
            end_at=datetime(2026, 7, 10, 11, 0, tzinfo=UTC),
            description="Focus block placed by HealthMes",
            agent_task_id=task_id,
        )
        created = backend.create_event(draft)

        ((calendar_id, body),) = service.insert_calls
        assert calendar_id == "primary"
        assert body["extendedProperties"]["private"] == {
            "healthmes": "1",
            "healthmes_task_id": str(task_id),
        }
        assert body["start"] == {"dateTime": "2026-07-10T09:00:00+00:00"}
        assert body["description"] == "Focus block placed by HealthMes"

        assert created.is_agent_created
        assert created.agent_task_id == task_id
        assert created.external_id == "generated-1"

    def test_actual_sleep_identity_round_trips_private_metadata(
        self, backend, service
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

        ((_, body),) = service.insert_calls
        assert body["extendedProperties"]["private"] == {
            "healthmes": "1",
            "healthmes_kind": "actual_sleep",
            "healthmes_source": "oura",
            "healthmes_source_key": "oura:2026-07-26",
        }
        assert created.identity == identity

    def test_identity_collision_requires_a_local_recovery_intent(
        self, backend, service
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
        assert len(service.stored_events) == 1


class TestUpdateAndDelete:
    def _store_agent_event(self, service: FakeGoogleService, event_id: str = "mine") -> None:
        service.stored_events[event_id] = api_event(event_id, agent=True)

    def test_update_patches_only_given_fields(self, backend, service) -> None:
        self._store_agent_event(service)
        updated = backend.update_event(
            "mine",
            start_at=datetime(2026, 7, 10, 14, 0, tzinfo=UTC),
            end_at=datetime(2026, 7, 10, 15, 0, tzinfo=UTC),
        )
        ((_, event_id, body),) = service.patch_calls
        assert event_id == "mine"
        assert set(body) == {"start", "end"}
        assert updated.start_at == datetime(2026, 7, 10, 14, 0, tzinfo=UTC)
        # The patch is guarded by the etag read via GET (optimistic concurrency).
        assert service.write_headers[-1]["If-Match"] == '"e1"'

    def test_update_refuses_untagged_event(self, backend, service) -> None:
        service.stored_events["theirs"] = api_event("theirs", agent=False)
        with pytest.raises(OwnershipError):
            backend.update_event(
                "theirs",
                start_at=datetime(2026, 7, 10, 14, 0, tzinfo=UTC),
                end_at=datetime(2026, 7, 10, 15, 0, tzinfo=UTC),
            )
        assert service.patch_calls == []

    def test_update_missing_event_raises_not_found(self, backend, service) -> None:
        with pytest.raises(EventNotFoundError):
            backend.update_event("ghost", summary="x")

    def test_update_cancelled_event_raises_not_found(self, backend, service) -> None:
        service.stored_events["gone"] = api_event("gone", agent=True, status="cancelled")
        with pytest.raises(EventNotFoundError):
            backend.update_event("gone", summary="x")

    def test_delete_checks_ownership_then_deletes(self, backend, service) -> None:
        self._store_agent_event(service)
        backend.delete_event("mine")
        assert service.delete_calls == [("primary", "mine")]
        assert "mine" not in service.stored_events
        # The delete is guarded by the etag read via GET (optimistic concurrency).
        assert service.write_headers[-1]["If-Match"] == '"e1"'

    def test_delete_refuses_untagged_event(self, backend, service) -> None:
        service.stored_events["theirs"] = api_event("theirs", agent=False)
        with pytest.raises(OwnershipError):
            backend.delete_event("theirs")
        assert service.delete_calls == []

    def test_delete_requires_expected_healthmes_kind(self, backend, service) -> None:
        service.stored_events["mine"] = api_event(
            "mine",
            agent=True,
            healthmes_kind="actual_sleep",
        )
        with pytest.raises(OwnershipError, match="planned_sleep"):
            backend.delete_event(
                "mine",
                expected_kind=HealthmesEventKind.PLANNED_SLEEP,
            )
        assert service.delete_calls == []

    def test_delete_accepts_matching_healthmes_kind(self, backend, service) -> None:
        service.stored_events["mine"] = api_event(
            "mine",
            agent=True,
            healthmes_kind="planned_sleep",
        )
        backend.delete_event(
            "mine",
            expected_kind=HealthmesEventKind.PLANNED_SLEEP,
        )
        assert service.delete_calls == [("primary", "mine")]

    def test_update_conflict_412_raises_and_leaves_event_unmutated(
        self, backend, service
    ) -> None:
        # F3: the event changed remotely between our GET and our PATCH. The
        # If-Match precondition must make the write 412, not silently overwrite.
        service.stored_events["mine"] = api_event("mine", agent=True)
        original = dict(service.stored_events["mine"])
        service.fail_precondition = True
        with pytest.raises(CalendarConflictError):
            backend.update_event(
                "mine",
                start_at=datetime(2026, 7, 10, 14, 0, tzinfo=UTC),
                end_at=datetime(2026, 7, 10, 15, 0, tzinfo=UTC),
            )
        assert service.write_headers[-1]["If-Match"] == '"e1"'  # precondition sent
        assert service.stored_events["mine"] == original  # never overwritten

    def test_update_uses_mirror_etag_instead_of_fresh_remote_etag(
        self, backend, service
    ) -> None:
        service.stored_events["mine"] = api_event("mine", agent=True, etag='"remote-v2"')
        with pytest.raises(CalendarConflictError):
            backend.update_event(
                "mine",
                summary="corrected",
                expected_etag='"mirror-v1"',
            )
        assert service.write_headers[-1]["If-Match"] == '"mirror-v1"'

    def test_delete_uses_mirror_etag_instead_of_fresh_remote_etag(
        self, backend, service
    ) -> None:
        service.stored_events["mine"] = api_event(
            "mine",
            agent=True,
            healthmes_kind="planned_sleep",
            etag='"remote-v2"',
        )
        with pytest.raises(CalendarConflictError):
            backend.delete_event(
                "mine",
                expected_kind=HealthmesEventKind.PLANNED_SLEEP,
                expected_etag='"mirror-v1"',
            )
        assert service.write_headers[-1]["If-Match"] == '"mirror-v1"'

    def test_delete_conflict_412_raises_and_leaves_event(self, backend, service) -> None:
        service.stored_events["mine"] = api_event("mine", agent=True)
        original = dict(service.stored_events["mine"])
        service.fail_precondition = True
        with pytest.raises(CalendarConflictError):
            backend.delete_event("mine")
        assert service.write_headers[-1]["If-Match"] == '"e1"'  # precondition sent
        assert service.stored_events["mine"] == original  # never deleted


class TestConfirmedExternalTimeChangeWriter:
    def _change(self, event_id: str = "theirs") -> ConfirmedExternalTimeChange:
        return ConfirmedExternalTimeChange(
            external_event_id=event_id,
            original_start_at=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
            original_end_at=datetime(2026, 7, 10, 10, 30, tzinfo=UTC),
            proposed_start_at=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
            proposed_end_at=datetime(2026, 7, 10, 10, 0, tzinfo=UTC),
            expected_etag='"etag-v1"',
        )

    def test_applies_external_end_only_patch_with_proposal_etag(
        self, backend, service
    ) -> None:
        service.stored_events["theirs"] = api_event(
            "theirs",
            start="2026-07-10T09:00:00+00:00",
            end="2026-07-10T10:30:00+00:00",
            agent=False,
            etag='"etag-v1"',
        )
        updated = backend.apply_confirmed_external_time_change(self._change())

        assert service.get_calls == []
        assert service.patch_calls == [
            (
                "primary",
                "theirs",
                {"end": {"dateTime": "2026-07-10T10:00:00+00:00"}},
            )
        ]
        assert service.write_headers[-1]["If-Match"] == '"etag-v1"'
        assert updated.external_id == "theirs"
        assert updated.start_at == datetime(2026, 7, 10, 9, 0, tzinfo=UTC)
        assert updated.end_at == datetime(2026, 7, 10, 10, 0, tzinfo=UTC)

    def test_conflict_uses_proposal_etag_and_leaves_event_unmutated(
        self, backend, service
    ) -> None:
        service.stored_events["theirs"] = api_event(
            "theirs",
            start="2026-07-10T09:00:00+00:00",
            end="2026-07-10T10:30:00+00:00",
            agent=False,
            etag='"etag-v2"',
        )
        original = dict(service.stored_events["theirs"])
        with pytest.raises(CalendarConflictError):
            backend.apply_confirmed_external_time_change(self._change())
        assert service.write_headers[-1]["If-Match"] == '"etag-v1"'
        assert service.stored_events["theirs"] == original


# --- OAuth helpers (offline: file handling only) -------------------------------


class TestOAuthHelpers:
    def _write_token(self, path, expiry: str = "2099-01-01T00:00:00Z") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "type": "authorized_user",
                    "token": "access-token",
                    "refresh_token": "refresh-token",
                    "client_id": "client-id",
                    "client_secret": "client-secret",
                    "expiry": expiry,
                    "scopes": ["https://www.googleapis.com/auth/calendar.events"],
                }
            ),
            encoding="utf-8",
        )

    def test_missing_token_file_returns_none(self, tmp_path) -> None:
        assert load_credentials(tmp_path / "google" / "calendar_token.json") is None

    def test_valid_unexpired_token_loads_without_network(self, tmp_path) -> None:
        token_file = google_token_path(tmp_path)
        self._write_token(token_file)
        credentials = load_credentials(token_file)
        assert credentials is not None
        assert credentials.valid

    def test_unreadable_token_file_returns_none(self, tmp_path) -> None:
        token_file = google_token_path(tmp_path)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(json.dumps({"token": "x"}), encoding="utf-8")  # missing keys
        assert load_credentials(token_file) is None

    def test_ensure_credentials_non_interactive_raises(self, tmp_path) -> None:
        with pytest.raises(CalendarAuthError, match="run the interactive setup"):
            ensure_credentials(tmp_path, interactive=False)

    def test_ensure_credentials_interactive_needs_client_secret(self, tmp_path) -> None:
        with pytest.raises(CalendarAuthError, match="client secret"):
            ensure_credentials(tmp_path, interactive=True)

    def test_token_path_lives_under_data_dir(self, tmp_path) -> None:
        assert google_token_path(tmp_path) == tmp_path / "google" / "calendar_token.json"
