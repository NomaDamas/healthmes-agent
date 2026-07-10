"""Mirror-service tests: diff detection, sync-state persistence, ownership guard."""

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from healthmes.calendars.base import (
    CalendarError,
    EventDraft,
    EventNotFoundError,
    OwnershipError,
)
from healthmes.calendars.state import InMemorySyncStateStore
from healthmes.calendars.sync import CalendarMirrorService, ChangeKind
from healthmes.store import CalendarEventMirror, CalendarSource, Task


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.fixture
def state_store() -> InMemorySyncStateStore:
    return InMemorySyncStateStore()


@pytest.fixture
def service(session, fake_backend, state_store) -> CalendarMirrorService:
    return CalendarMirrorService(session, [fake_backend], state_store)


def rows(session) -> dict[str, CalendarEventMirror]:
    result = session.execute(select(CalendarEventMirror)).scalars().all()
    return {row.external_id: row for row in result}


def draft(**overrides) -> EventDraft:
    defaults = {
        "summary": "Deep work",
        "start_at": utc(2026, 7, 10, 9, 0),
        "end_at": utc(2026, 7, 10, 11, 0),
    }
    defaults.update(overrides)
    return EventDraft(**defaults)


class TestBootstrap:
    def test_first_sync_adopts_everything_silently(
        self, service, fake_backend, session, make_event
    ) -> None:
        fake_backend.queue_changes(
            [
                make_event("meet-1"),
                make_event("mine-1", is_agent_created=True),
            ],
            {"sync_token": "tok-1"},
        )
        diff = service.sync_backend(fake_backend)

        assert not diff.has_changes  # bootstrap must not fire schedule_changed
        mirrored = rows(session)
        assert set(mirrored) == {"meet-1", "mine-1"}
        assert not mirrored["meet-1"].is_agent_created
        assert mirrored["mine-1"].is_agent_created  # tag adopted from the wire

    def test_bootstrap_persists_sync_state(self, service, fake_backend, state_store) -> None:
        fake_backend.queue_changes([], {"sync_token": "tok-1"})
        service.sync_backend(fake_backend)
        assert state_store.load(fake_backend.source) == {"sync_token": "tok-1"}


class TestSyncStatePersistence:
    def test_next_run_receives_persisted_state(self, service, fake_backend) -> None:
        fake_backend.queue_changes([], {"sync_token": "tok-1"})
        fake_backend.queue_changes([], {"sync_token": "tok-2"})

        service.sync_backend(fake_backend)
        service.sync_backend(fake_backend)
        service.sync_backend(fake_backend)

        assert fake_backend.received_sync_states == [
            None,
            {"sync_token": "tok-1"},
            {"sync_token": "tok-2"},
        ]

    def test_state_survives_service_recreation(
        self, session, fake_backend, state_store
    ) -> None:
        fake_backend.queue_changes([], {"sync_token": "tok-1"})
        CalendarMirrorService(session, [fake_backend], state_store).sync_backend(fake_backend)

        CalendarMirrorService(session, [fake_backend], state_store).sync_backend(fake_backend)
        assert fake_backend.received_sync_states[-1] == {"sync_token": "tok-1"}


class TestNonAgentDiff:
    def _bootstrap(self, service, fake_backend, make_event) -> None:
        fake_backend.queue_changes([make_event("meet-1")], {"sync_token": "tok-1"})
        service.sync_backend(fake_backend)

    def test_created(self, service, fake_backend, make_event) -> None:
        self._bootstrap(service, fake_backend, make_event)
        fake_backend.queue_changes(
            [
                make_event(
                    "meet-2",
                    summary="1:1",
                    start=utc(2026, 7, 9, 13, 0),
                    end=utc(2026, 7, 9, 14, 0),
                )
            ],
            {"sync_token": "tok-2"},
        )
        diff = service.sync_backend(fake_backend)

        (change,) = diff.created
        assert change.kind is ChangeKind.CREATED
        assert change.external_id == "meet-2"
        assert not change.is_agent_created
        assert change.new_start_at == utc(2026, 7, 9, 13, 0)
        assert diff.moved == diff.deleted == diff.agent_modified == []

    def test_moved_updates_row_and_reports_old_new(
        self, service, fake_backend, session, make_event
    ) -> None:
        self._bootstrap(service, fake_backend, make_event)
        fake_backend.queue_changes(
            [
                make_event(
                    "meet-1",
                    start=utc(2026, 7, 9, 15, 0),
                    end=utc(2026, 7, 9, 15, 30),
                    etag="etag-2",
                )
            ],
            {"sync_token": "tok-2"},
        )
        diff = service.sync_backend(fake_backend)

        (change,) = diff.moved
        assert change.kind is ChangeKind.MOVED
        assert change.old_start_at == utc(2026, 7, 9, 9, 0)
        assert change.new_start_at == utc(2026, 7, 9, 15, 0)

        row = rows(session)["meet-1"]  # external wins
        assert row.start_at.replace(tzinfo=UTC) == utc(2026, 7, 9, 15, 0)
        assert row.etag == "etag-2"

    def test_summary_only_change_is_mirrored_silently(
        self, service, fake_backend, session, make_event
    ) -> None:
        self._bootstrap(service, fake_backend, make_event)
        fake_backend.queue_changes(
            [make_event("meet-1", summary="Team standup (renamed)")],
            {"sync_token": "tok-2"},
        )
        diff = service.sync_backend(fake_backend)

        assert not diff.has_changes
        assert rows(session)["meet-1"].summary == "Team standup (renamed)"

    def test_deleted(self, service, fake_backend, session, make_event) -> None:
        self._bootstrap(service, fake_backend, make_event)
        fake_backend.queue_changes(
            [make_event("meet-1", deleted=True, summary=None, etag=None)],
            {"sync_token": "tok-2"},
        )
        diff = service.sync_backend(fake_backend)

        (change,) = diff.deleted
        assert change.kind is ChangeKind.DELETED
        assert change.old_start_at == utc(2026, 7, 9, 9, 0)
        assert rows(session) == {}

    def test_deletion_of_unknown_event_is_ignored(
        self, service, fake_backend, make_event
    ) -> None:
        self._bootstrap(service, fake_backend, make_event)
        fake_backend.queue_changes(
            [make_event("never-seen", deleted=True, summary=None)],
            {"sync_token": "tok-2"},
        )
        diff = service.sync_backend(fake_backend)
        assert not diff.has_changes


class TestUnchangedRedelivery:
    """Byte-identical re-delivery must not touch the row at all.

    Google 410 full resyncs, a lost sync_state.json and a crash between
    commit and cursor save all re-deliver unchanged events; any UPDATE bumps
    ``updated_at`` (onupdate), which the trigger sweep reads as an external
    change — a false ``schedule_changed`` alert. The tz-aware assigned values
    vs naive sqlite-loaded values made even equal assignments dirty.
    """

    BACKDATED = "2026-07-01 00:00:00"

    def _backdate(self, session, external_id: str) -> None:
        from sqlalchemy import text

        session.execute(
            text(
                "UPDATE calendar_event_mirror SET created_at = :stamp, "
                "updated_at = :stamp WHERE external_id = :external_id"
            ),
            {"stamp": self.BACKDATED, "external_id": external_id},
        )
        session.commit()

    def test_identical_redelivery_does_not_bump_updated_at(
        self, service, fake_backend, session, make_event
    ) -> None:
        fake_backend.queue_changes([make_event("meet-1")], {"sync_token": "tok-1"})
        service.sync_backend(fake_backend)
        self._backdate(session, "meet-1")

        # Same event, byte-identical (e.g. lost sync state -> full resync).
        fake_backend.queue_changes([make_event("meet-1")], {"sync_token": "tok-2"})
        diff = service.sync_backend(fake_backend)

        assert not diff.has_changes
        session.expire_all()
        row = rows(session)["meet-1"]
        assert row.updated_at.strftime("%Y-%m-%d %H:%M:%S") == self.BACKDATED
        assert row.created_at.strftime("%Y-%m-%d %H:%M:%S") == self.BACKDATED

    def test_real_move_still_bumps_updated_at(
        self, service, fake_backend, session, make_event
    ) -> None:
        fake_backend.queue_changes([make_event("meet-1")], {"sync_token": "tok-1"})
        service.sync_backend(fake_backend)
        self._backdate(session, "meet-1")

        fake_backend.queue_changes(
            [make_event("meet-1", start=utc(2026, 7, 9, 15, 0), end=utc(2026, 7, 9, 16, 0))],
            {"sync_token": "tok-2"},
        )
        diff = service.sync_backend(fake_backend)

        assert len(diff.moved) == 1
        session.expire_all()
        row = rows(session)["meet-1"]
        assert row.updated_at.strftime("%Y-%m-%d %H:%M:%S") != self.BACKDATED


class TestAgentEventDiff:
    def _create_agent_block(self, service, fake_backend) -> str:
        row = service.create_agent_event(fake_backend.source, draft())
        return row.external_id

    def test_external_move_of_agent_event_lands_in_agent_modified(
        self, service, fake_backend, session, make_event
    ) -> None:
        external_id = self._create_agent_block(service, fake_backend)
        fake_backend.queue_changes([], {"sync_token": "tok-1"})
        service.sync_backend(fake_backend)  # establish sync state

        # User drags the agent block in Google Calendar: times change.
        fake_backend.queue_changes(
            [
                make_event(
                    external_id,
                    summary="Deep work",
                    start=utc(2026, 7, 10, 16, 0),
                    end=utc(2026, 7, 10, 18, 0),
                    is_agent_created=True,
                    etag="etag-3",
                )
            ],
            {"sync_token": "tok-2"},
        )
        diff = service.sync_backend(fake_backend)

        assert diff.created == diff.moved == diff.deleted == []
        (change,) = diff.agent_modified
        assert change.kind is ChangeKind.MOVED
        assert change.is_agent_created
        assert change.old_start_at == utc(2026, 7, 10, 9, 0)
        assert change.new_start_at == utc(2026, 7, 10, 16, 0)

        row = rows(session)[external_id]  # external wins for agent events too
        assert row.start_at.replace(tzinfo=UTC) == utc(2026, 7, 10, 16, 0)
        assert row.is_agent_created

    def test_external_delete_of_agent_event_lands_in_agent_modified(
        self, service, fake_backend, session, make_event
    ) -> None:
        external_id = self._create_agent_block(service, fake_backend)
        fake_backend.queue_changes([], {"sync_token": "tok-1"})
        service.sync_backend(fake_backend)

        fake_backend.queue_changes(
            [make_event(external_id, deleted=True, summary=None, etag=None)],
            {"sync_token": "tok-2"},
        )
        diff = service.sync_backend(fake_backend)

        (change,) = diff.agent_modified
        assert change.kind is ChangeKind.DELETED
        assert change.is_agent_created
        assert rows(session) == {}

    def test_agent_event_resync_without_row_is_adopted_silently(
        self, service, fake_backend, session, make_event
    ) -> None:
        # State exists (not bootstrap) but the row is missing (e.g. restored
        # DB): a tagged event appearing is re-adopted without alerting.
        fake_backend.queue_changes([], {"sync_token": "tok-1"})
        service.sync_backend(fake_backend)
        fake_backend.queue_changes(
            [make_event("mine-recovered", is_agent_created=True)],
            {"sync_token": "tok-2"},
        )
        diff = service.sync_backend(fake_backend)

        assert not diff.has_changes
        assert rows(session)["mine-recovered"].is_agent_created


class TestOwnershipGuard:
    def _mirror_external_event(self, service, fake_backend, make_event, external_id) -> None:
        fake_backend.queue_changes([make_event(external_id)], {"sync_token": "tok-1"})
        service.sync_backend(fake_backend)

    def test_create_agent_event_mirrors_immediately(
        self, service, fake_backend, session
    ) -> None:
        task = Task(title="Write report")
        session.add(task)
        session.commit()

        row = service.create_agent_event(
            fake_backend.source, draft(agent_task_id=task.id)
        )

        assert fake_backend.created_drafts[0].agent_task_id == task.id
        stored = rows(session)[row.external_id]
        assert stored.is_agent_created
        assert stored.agent_task_id == task.id
        assert stored.calendar_source is CalendarSource.GOOGLE

    def test_create_with_unknown_task_id_drops_link(self, service, fake_backend, session) -> None:
        row = service.create_agent_event(
            fake_backend.source, draft(agent_task_id=uuid.uuid4())
        )
        assert rows(session)[row.external_id].agent_task_id is None

    def test_move_agent_event(self, service, fake_backend, session) -> None:
        row = service.create_agent_event(fake_backend.source, draft())
        moved = service.move_agent_event(
            fake_backend.source,
            row.external_id,
            start_at=utc(2026, 7, 11, 9, 0),
            end_at=utc(2026, 7, 11, 10, 0),
        )
        assert fake_backend.update_calls[0]["external_id"] == row.external_id
        assert moved.start_at.replace(tzinfo=UTC) == utc(2026, 7, 11, 9, 0)
        assert moved.etag == "etag-updated"

    def test_move_refuses_external_event(self, service, fake_backend, make_event) -> None:
        self._mirror_external_event(service, fake_backend, make_event, "meet-1")
        with pytest.raises(OwnershipError, match="external calendar owns it"):
            service.move_agent_event(
                fake_backend.source,
                "meet-1",
                start_at=utc(2026, 7, 11, 9, 0),
                end_at=utc(2026, 7, 11, 10, 0),
            )
        assert fake_backend.update_calls == []  # guard fires before any backend call

    def test_delete_refuses_external_event(self, service, fake_backend, make_event) -> None:
        self._mirror_external_event(service, fake_backend, make_event, "meet-1")
        with pytest.raises(OwnershipError):
            service.delete_agent_event(fake_backend.source, "meet-1")
        assert fake_backend.delete_calls == []

    def test_delete_agent_event_removes_row_and_remote(
        self, service, fake_backend, session
    ) -> None:
        row = service.create_agent_event(fake_backend.source, draft())
        service.delete_agent_event(fake_backend.source, row.external_id)
        assert fake_backend.delete_calls == [row.external_id]
        assert rows(session) == {}

    def test_move_unknown_event_raises_not_found(self, service, fake_backend) -> None:
        with pytest.raises(EventNotFoundError):
            service.move_agent_event(
                fake_backend.source,
                "ghost",
                start_at=utc(2026, 7, 11, 9, 0),
                end_at=utc(2026, 7, 11, 10, 0),
            )

    def test_move_validates_time_order(self, service, fake_backend) -> None:
        row = service.create_agent_event(fake_backend.source, draft())
        with pytest.raises(ValueError, match="after start_at"):
            service.move_agent_event(
                fake_backend.source,
                row.external_id,
                start_at=utc(2026, 7, 11, 10, 0),
                end_at=utc(2026, 7, 11, 9, 0),
            )


class TestMultiBackend:
    def test_sync_all_merges_sources(
        self, session, state_store, fake_backend_factory, make_event
    ) -> None:
        google = fake_backend_factory(CalendarSource.GOOGLE)
        caldav = fake_backend_factory(CalendarSource.CALDAV)
        service = CalendarMirrorService(session, [google, caldav], state_store)

        # bootstrap both, then one change on each
        google.queue_changes([make_event("g-1")], {"sync_token": "tok-1"})
        caldav.queue_changes([make_event("c-1")], {"ctag": "ctag-1"})
        assert not service.sync_all().has_changes

        google.queue_changes([make_event("g-2")], {"sync_token": "tok-2"})
        caldav.queue_changes(
            [make_event("c-1", deleted=True, summary=None)], {"ctag": "ctag-2"}
        )
        diff = service.sync_all()

        assert [change.external_id for change in diff.created] == ["g-2"]
        assert [change.external_id for change in diff.deleted] == ["c-1"]
        assert diff.deleted[0].calendar_source is CalendarSource.CALDAV

    def test_same_external_id_isolated_per_source(
        self, session, state_store, fake_backend_factory, make_event
    ) -> None:
        google = fake_backend_factory(CalendarSource.GOOGLE)
        caldav = fake_backend_factory(CalendarSource.CALDAV)
        service = CalendarMirrorService(session, [google, caldav], state_store)

        google.queue_changes([make_event("shared-id")], {"sync_token": "t"})
        caldav.queue_changes(
            [make_event("shared-id", start=utc(2026, 7, 9, 12, 0), end=utc(2026, 7, 9, 13, 0))],
            {"ctag": "c"},
        )
        service.sync_all()

        stored = session.execute(select(CalendarEventMirror)).scalars().all()
        assert len(stored) == 2

    def test_duplicate_backend_source_rejected(
        self, session, state_store, fake_backend_factory
    ) -> None:
        with pytest.raises(CalendarError, match="duplicate backend"):
            CalendarMirrorService(
                session,
                [
                    fake_backend_factory(CalendarSource.GOOGLE),
                    fake_backend_factory(CalendarSource.GOOGLE),
                ],
                state_store,
            )


class TestDiffPayload:
    def test_payload_is_json_safe(self, service, fake_backend, make_event) -> None:
        fake_backend.queue_changes([make_event("meet-1")], {"sync_token": "tok-1"})
        service.sync_backend(fake_backend)
        fake_backend.queue_changes(
            [
                make_event("meet-2"),
                make_event("meet-1", start=utc(2026, 7, 9, 10, 0), end=utc(2026, 7, 9, 10, 30)),
            ],
            {"sync_token": "tok-2"},
        )
        diff = service.sync_backend(fake_backend)

        payload = diff.to_payload()
        encoded = json.loads(json.dumps(payload))
        assert encoded["created"][0]["calendar_source"] == "google"
        assert encoded["moved"][0]["kind"] == "moved"
        assert encoded["moved"][0]["old_start_at"] == "2026-07-09T09:00:00+00:00"
