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
from healthmes.calendars.state import (
    InMemoryPendingDiffStore,
    InMemorySyncStateStore,
)
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
        # A genuine agent event carries BOTH the tag and a task id that resolves
        # to a local Task row — a bare tag is no longer trusted (see F1).
        task = Task(title="My focus block")
        session.add(task)
        session.commit()
        fake_backend.queue_changes(
            [
                make_event("meet-1"),
                make_event("mine-1", is_agent_created=True, agent_task_id=task.id),
            ],
            {"sync_token": "tok-1"},
        )
        diff = service.sync_backend(fake_backend)

        assert not diff.has_changes  # bootstrap must not fire schedule_changed
        mirrored = rows(session)
        assert set(mirrored) == {"meet-1", "mine-1"}
        assert not mirrored["meet-1"].is_agent_created
        assert mirrored["mine-1"].is_agent_created  # trusted tag adopted from wire
        assert mirrored["mine-1"].agent_task_id == task.id

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
    def _create_agent_block(self, service, fake_backend, session) -> tuple[str, object]:
        """Create a task-linked agent block and return (external_id, task_id).

        Agent blocks must carry a resolvable task id to stay trusted across a
        provider round-trip (a bare tag is no longer trusted — see F1); the push
        path always supplies one.
        """
        task = Task(title="Deep work")
        session.add(task)
        session.commit()
        row = service.create_agent_event(
            fake_backend.source, draft(agent_task_id=task.id)
        )
        return row.external_id, task.id

    def _still_live(self, make_event, external_id, task_id):
        """The agent block as the provider still returns it during bootstrap
        (F6 would tombstone a mirror row the full-resync set omits)."""
        return make_event(
            external_id,
            summary="Deep work",
            start=utc(2026, 7, 10, 9, 0),
            end=utc(2026, 7, 10, 11, 0),
            is_agent_created=True,
            agent_task_id=task_id,
        )

    def test_external_move_of_agent_event_lands_in_agent_modified(
        self, service, fake_backend, session, make_event
    ) -> None:
        external_id, task_id = self._create_agent_block(service, fake_backend, session)
        fake_backend.queue_changes(
            [self._still_live(make_event, external_id, task_id)], {"sync_token": "tok-1"}
        )
        service.sync_backend(fake_backend)  # establish sync state

        # User drags the agent block in Google Calendar: times change, tag kept.
        fake_backend.queue_changes(
            [
                make_event(
                    external_id,
                    summary="Deep work",
                    start=utc(2026, 7, 10, 16, 0),
                    end=utc(2026, 7, 10, 18, 0),
                    is_agent_created=True,
                    agent_task_id=task_id,
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
        external_id, task_id = self._create_agent_block(service, fake_backend, session)
        fake_backend.queue_changes(
            [self._still_live(make_event, external_id, task_id)], {"sync_token": "tok-1"}
        )
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
        # DB): a trusted tagged event appearing is re-adopted without alerting.
        task = Task(title="Recovered block")
        session.add(task)
        session.commit()
        fake_backend.queue_changes([], {"sync_token": "tok-1"})
        service.sync_backend(fake_backend)
        fake_backend.queue_changes(
            [make_event("mine-recovered", is_agent_created=True, agent_task_id=task.id)],
            {"sync_token": "tok-2"},
        )
        diff = service.sync_backend(fake_backend)

        assert not diff.has_changes
        assert rows(session)["mine-recovered"].is_agent_created
        assert rows(session)["mine-recovered"].agent_task_id == task.id


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


class TestForgedTagOwnership:
    """F1/F2: the ownership tag is trusted only with a resolvable task id."""

    def _seed(self, service, fake_backend, make_event) -> None:
        # Bootstrap so subsequent syncs report changes (not silent adoption).
        fake_backend.queue_changes([make_event("seed")], {"sync_token": "tok-0"})
        service.sync_backend(fake_backend)

    def test_forged_agent_tag_never_authorizes_writes(
        self, service, fake_backend, session, make_event
    ) -> None:
        self._seed(service, fake_backend, make_event)
        # An external event carries the tag but its task id is bogus (never a
        # local Task) — a hand-crafted claim of agent ownership.
        fake_backend.queue_changes(
            [make_event("forged", is_agent_created=True, agent_task_id=uuid.uuid4())],
            {"sync_token": "tok-1"},
        )
        diff = service.sync_backend(fake_backend)

        # Treated as the genuine external creation it is, not adopted as agent.
        assert [change.external_id for change in diff.created] == ["forged"]
        assert diff.agent_modified == []
        row = rows(session)["forged"]
        assert not row.is_agent_created
        assert row.agent_task_id is None

        # The ownership guard refuses agent writes, and no backend call is made.
        with pytest.raises(OwnershipError):
            service.move_agent_event(
                fake_backend.source,
                "forged",
                start_at=utc(2026, 7, 11, 9, 0),
                end_at=utc(2026, 7, 11, 10, 0),
            )
        with pytest.raises(OwnershipError):
            service.delete_agent_event(fake_backend.source, "forged")
        assert fake_backend.update_calls == []
        assert fake_backend.delete_calls == []

    def test_bare_agent_tag_without_task_id_is_external(
        self, service, fake_backend, session, make_event
    ) -> None:
        self._seed(service, fake_backend, make_event)
        fake_backend.queue_changes(
            [make_event("bare", is_agent_created=True)],  # tag, but no task id
            {"sync_token": "tok-1"},
        )
        diff = service.sync_backend(fake_backend)

        assert [change.external_id for change in diff.created] == ["bare"]
        assert not rows(session)["bare"].is_agent_created

    def test_tag_stripped_during_move_becomes_external(
        self, service, fake_backend, session, make_event
    ) -> None:
        task = Task(title="Deep work")
        session.add(task)
        session.commit()
        created = service.create_agent_event(
            fake_backend.source, draft(agent_task_id=task.id)
        )
        external_id = created.external_id
        fake_backend.queue_changes(
            [
                make_event(
                    external_id,
                    summary="Deep work",
                    start=utc(2026, 7, 10, 9, 0),
                    end=utc(2026, 7, 10, 11, 0),
                    is_agent_created=True,
                    agent_task_id=task.id,
                )
            ],
            {"sync_token": "tok-1"},
        )
        service.sync_backend(fake_backend)  # establish state, block still agent-owned

        # The user strips the healthmes tag AND drags the event: it is no longer
        # agent-owned, so the move is an EXTERNAL change (diff.moved, not
        # agent_modified), and the agent can no longer write to it.
        fake_backend.queue_changes(
            [
                make_event(
                    external_id,
                    summary="Deep work",
                    start=utc(2026, 7, 10, 16, 0),
                    end=utc(2026, 7, 10, 18, 0),
                    is_agent_created=False,  # tag stripped
                    etag="etag-x",
                )
            ],
            {"sync_token": "tok-2"},
        )
        diff = service.sync_backend(fake_backend)

        assert diff.agent_modified == []
        (change,) = diff.moved
        assert change.kind is ChangeKind.MOVED
        assert not change.is_agent_created
        assert change.new_start_at == utc(2026, 7, 10, 16, 0)

        updated = rows(session)[external_id]
        assert not updated.is_agent_created  # flipped to external
        assert updated.agent_task_id is None
        with pytest.raises(OwnershipError):
            service.delete_agent_event(fake_backend.source, external_id)
        assert fake_backend.delete_calls == []


class TestFullResyncReconcile:
    """F6: a full resync tombstones mirror rows the provider no longer returns."""

    def test_full_resync_reconciles_missing_provider_events(
        self, service, fake_backend, session, state_store, make_event
    ) -> None:
        fake_backend.queue_changes(
            [make_event("keep-1"), make_event("gone-1")], {"sync_token": "tok-1"}
        )
        service.sync_backend(fake_backend)
        assert set(rows(session)) == {"keep-1", "gone-1"}

        # Sync state is lost -> the next run is a full resync that returns only
        # keep-1; gone-1 was deleted while we had no cursor to observe it.
        state_store.clear(fake_backend.source)
        fake_backend.queue_changes([make_event("keep-1")], {"sync_token": "tok-2"})
        diff = service.sync_backend(fake_backend)

        assert [change.external_id for change in diff.deleted] == ["gone-1"]
        assert diff.created == diff.moved == diff.agent_modified == []
        assert set(rows(session)) == {"keep-1"}

    def test_true_first_sync_emits_no_tombstones(
        self, service, fake_backend, session, make_event
    ) -> None:
        # An empty mirror at bootstrap must stay silent (no phantom deletions).
        fake_backend.queue_changes([make_event("meet-1")], {"sync_token": "tok-1"})
        diff = service.sync_backend(fake_backend)
        assert not diff.has_changes
        assert set(rows(session)) == {"meet-1"}


class _FailingOnceStateStore:
    """Wraps a store, raising on the Nth ``save`` to simulate a cursor-save crash."""

    def __init__(self, inner: InMemorySyncStateStore, *, fail_on_call: int) -> None:
        self._inner = inner
        self._fail_on = fail_on_call
        self._calls = 0

    def load(self, source):
        return self._inner.load(source)

    def save(self, source, state) -> None:
        self._calls += 1
        if self._calls == self._fail_on:
            raise RuntimeError("cursor save failed")
        self._inner.save(source, state)


class TestPendingDiffJournal:
    """F5: a diff whose cursor save failed is replayed, not lost."""

    def test_pending_diff_replays_after_cursor_save_failure(
        self, session, fake_backend, make_event
    ) -> None:
        # Bootstrap save is call #1 (succeeds); the deletion run's cursor save
        # is call #2 (fails) after the idempotent mirror delete already landed.
        flaky = _FailingOnceStateStore(InMemorySyncStateStore(), fail_on_call=2)
        pending = InMemoryPendingDiffStore()
        service = CalendarMirrorService(session, [fake_backend], flaky, pending)

        fake_backend.queue_changes([make_event("meet-1")], {"sync_token": "tok-1"})
        service.sync_backend(fake_backend)
        assert set(rows(session)) == {"meet-1"}

        fake_backend.queue_changes(
            [make_event("meet-1", deleted=True, summary=None, etag=None)],
            {"sync_token": "tok-2"},
        )
        with pytest.raises(RuntimeError, match="cursor save failed"):
            service.sync_backend(fake_backend)
        # The delete committed (idempotent, cannot be re-derived) and the diff
        # is journaled for replay.
        assert rows(session) == {}
        assert pending.load(fake_backend.source) is not None

        # Next run: nothing new from the provider, but the journal replays the
        # deletion so the trigger still learns of it; the journal then clears.
        diff = service.sync_backend(fake_backend)
        assert [change.external_id for change in diff.deleted] == ["meet-1"]
        assert pending.load(fake_backend.source) is None


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
