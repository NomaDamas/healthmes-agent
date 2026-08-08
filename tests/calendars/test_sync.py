"""Mirror-service tests: diff detection, sync-state persistence, ownership guard."""

import json
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from healthmes.calendars.base import (
    CalendarConflictError,
    CalendarError,
    CalendarEventIdentity,
    EventDraft,
    EventNotFoundError,
    ExternalEvent,
    HealthmesEventKind,
    OwnershipError,
    calendar_identity_external_id,
    coerce_utc,
)
from healthmes.calendars.sleep_event_rendering import observation_fingerprint
from healthmes.calendars.sleep_mirror import (
    SLEEP_CREATE_PENDING_STATUS,
    SLEEP_UPDATE_PENDING_STATUS,
    mark_sleep_update_pending,
)
from healthmes.calendars.sleep_observation import (
    ACTUAL_SLEEP_IDENTITY_SOURCE,
    ActualSleepObservation,
    actual_sleep_source_key,
)
from healthmes.calendars.state import (
    InMemoryPendingDiffStore,
    InMemorySyncStateStore,
)
from healthmes.calendars.sync import CalendarMirrorService, ChangeKind, SyncDiff
from healthmes.store import (
    CalendarEventMirror,
    CalendarSource,
    ProposalStatus,
    ScheduleProposal,
    Task,
)


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

    def test_first_sync_mirrors_provider_metadata(
        self, service, fake_backend, session, make_event
    ) -> None:
        fake_backend.queue_changes(
            [
                make_event(
                    "meet-1",
                    organizer_self=True,
                    has_attendees=True,
                    is_recurring=True,
                    event_type="default",
                    is_all_day=False,
                    is_locked=True,
                    status="confirmed",
                )
            ],
            {"sync_token": "tok-1"},
        )
        service.sync_backend(fake_backend)

        row = rows(session)["meet-1"]
        assert row.organizer_self is True
        assert row.has_attendees is True
        assert row.is_recurring is True
        assert row.event_type == "default"
        assert row.is_all_day is False
        assert row.is_locked is True
        assert row.status == "confirmed"


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

    def test_metadata_only_change_is_mirrored_silently(
        self, service, fake_backend, session, make_event
    ) -> None:
        self._bootstrap(service, fake_backend, make_event)
        fake_backend.queue_changes(
            [
                make_event(
                    "meet-1",
                    organizer_self=True,
                    event_type="default",
                    status="confirmed",
                )
            ],
            {"sync_token": "tok-2"},
        )
        diff = service.sync_backend(fake_backend)

        row = rows(session)["meet-1"]
        assert not diff.has_changes
        assert row.organizer_self is True
        assert row.event_type == "default"
        assert row.status == "confirmed"

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

    @pytest.mark.parametrize("proposal_status", [ProposalStatus.ACCEPTED, ProposalStatus.PUSHED])
    def test_deleted_intake_invalidates_active_proposal(
        self,
        service,
        fake_backend,
        session,
        make_event,
        proposal_status,
    ) -> None:
        task = Task(title="Imported task")
        session.add(task)
        session.flush()
        session.add(
            ScheduleProposal(
                task_id=task.id,
                proposed_start=utc(2026, 7, 10, 9),
                proposed_end=utc(2026, 7, 10, 10),
                status=proposal_status,
            )
        )
        session.commit()
        fake_backend.queue_changes(
            [
                make_event(
                    "hm-intake-delete",
                    summary="[HM] Imported task",
                    organizer_self=True,
                    event_type="default",
                )
            ],
            {"sync_token": "tok-1"},
        )
        service.sync_backend(fake_backend)
        mirror = rows(session)["hm-intake-delete"]
        mirror.intake_task_id = task.id
        session.commit()

        fake_backend.queue_changes(
            [
                make_event(
                    "hm-intake-delete",
                    deleted=True,
                    summary=None,
                    etag=None,
                )
            ],
            {"sync_token": "tok-2"},
        )
        service.sync_backend(fake_backend)

        session.expire_all()
        assert session.get(Task, task.id).status == "cancelled"
        assert session.scalars(select(ScheduleProposal)).one().status is (
            ProposalStatus.INVALIDATED
        )

    def test_deleted_intake_removes_pushed_proposal_block(
        self,
        service,
        fake_backend,
        session,
        make_event,
    ) -> None:
        task = Task(title="Imported task", status="scheduled")
        session.add(task)
        session.flush()
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=utc(2026, 7, 10, 9),
            proposed_end=utc(2026, 7, 10, 10),
            status=ProposalStatus.PUSHED,
        )
        session.add(proposal)
        session.commit()
        identity = CalendarEventIdentity(
            kind=HealthmesEventKind.SCHEDULE_BLOCK,
            source="planner",
            source_key=f"proposal:{proposal.id}",
        )
        fake_backend.queue_changes(
            [
                make_event(
                    "hm-intake-delete",
                    summary="[HM] Imported task",
                    organizer_self=True,
                    event_type="default",
                )
            ],
            {"sync_token": "tok-1"},
        )
        service.sync_backend(fake_backend)
        intake_row = rows(session)["hm-intake-delete"]
        intake_row.intake_task_id = task.id
        session.commit()
        block = service.create_agent_event(
            fake_backend.source,
            EventDraft(
                summary=task.title,
                start_at=utc(2026, 7, 10, 9),
                end_at=utc(2026, 7, 10, 10),
                agent_task_id=task.id,
                identity=identity,
            ),
        )

        fake_backend.queue_changes(
            [
                make_event(
                    "hm-intake-delete",
                    deleted=True,
                    summary=None,
                    etag=None,
                )
            ],
            {"sync_token": "tok-2"},
        )
        service.sync_backend(fake_backend)

        session.expire_all()
        assert block.external_id not in fake_backend.events
        assert block.external_id not in rows(session)
        assert fake_backend.delete_calls == [block.external_id]
        assert session.get(Task, task.id).status == "cancelled"
        assert session.get(ScheduleProposal, proposal.id).status is (
            ProposalStatus.INVALIDATED
        )

    def test_deleted_intake_defers_block_owned_by_another_provider(
        self,
        session,
        state_store,
        fake_backend_factory,
        make_event,
    ) -> None:
        google = fake_backend_factory(CalendarSource.GOOGLE)
        caldav = fake_backend_factory(CalendarSource.CALDAV)
        google_service = CalendarMirrorService(session, [google], state_store)
        caldav_service = CalendarMirrorService(session, [caldav], state_store)
        task = Task(title="Cross-provider task", status="scheduled")
        session.add(task)
        session.flush()
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=utc(2026, 7, 10, 9),
            proposed_end=utc(2026, 7, 10, 10),
            status=ProposalStatus.PUSHED,
        )
        session.add(proposal)
        session.commit()
        identity = CalendarEventIdentity(
            kind=HealthmesEventKind.SCHEDULE_BLOCK,
            source="planner",
            source_key=f"proposal:{proposal.id}",
        )
        block = google_service.create_agent_event(
            google.source,
            EventDraft(
                summary=task.title,
                start_at=utc(2026, 7, 10, 9),
                end_at=utc(2026, 7, 10, 10),
                agent_task_id=task.id,
                identity=identity,
            ),
        )
        caldav.queue_changes(
            [
                make_event(
                    "icloud-intake",
                    summary="[HM] Cross-provider task",
                    organizer_self=True,
                    event_type="default",
                )
            ],
            {"ctag": "ctag-1"},
        )
        caldav_service.sync_backend(caldav)
        intake_row = rows(session)["icloud-intake"]
        intake_row.intake_task_id = task.id
        session.commit()

        caldav.queue_changes(
            [
                make_event(
                    "icloud-intake",
                    deleted=True,
                    summary=None,
                    etag=None,
                )
            ],
            {"ctag": "ctag-2"},
        )
        caldav_service.sync_backend(caldav)

        session.expire_all()
        assert session.get(Task, task.id).status == "cancelled"
        assert session.get(ScheduleProposal, proposal.id).status is (
            ProposalStatus.PUSHED
        )
        assert session.get(CalendarEventMirror, block.id) is not None
        assert block.external_id in google.events
        assert caldav.delete_calls == []

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


@pytest.mark.parametrize(
    "mutation",
    ("upsert", "delete", "tombstone"),
)
@pytest.mark.parametrize(
    "pending_status",
    (SLEEP_CREATE_PENDING_STATUS, SLEEP_UPDATE_PENDING_STATUS),
)
def test_sqlite_stale_sync_cache_preserves_pending_sleep_intent(
    session_factory,
    fake_backend,
    mutation: str,
    pending_status: str,
) -> None:
    observation = ActualSleepObservation(
        local_date=date(2026, 7, 26),
        provider="oura",
        source_key=actual_sleep_source_key(date(2026, 7, 26)),
        start_at=utc(2026, 7, 25, 23),
        end_at=utc(2026, 7, 26, 7),
        duration_minutes=420,
        time_in_bed_minutes=480,
    )
    corrected = replace(
        observation,
        provider="garmin",
        start_at=observation.start_at - timedelta(minutes=15),
        end_at=observation.end_at + timedelta(minutes=45),
        duration_minutes=465,
        time_in_bed_minutes=525,
    )
    identity = CalendarEventIdentity(
        kind=HealthmesEventKind.ACTUAL_SLEEP,
        source=ACTUAL_SLEEP_IDENTITY_SOURCE,
        source_key=observation.source_key,
    )
    external_id = calendar_identity_external_id(
        CalendarSource.GOOGLE,
        identity,
    )
    with session_factory() as seed_session:
        seed_session.add(
            CalendarEventMirror(
                external_id=external_id,
                calendar_source=CalendarSource.GOOGLE,
                summary="수면 (실제)",
                start_at=observation.start_at,
                end_at=observation.end_at,
                is_agent_created=True,
                healthmes_kind=identity.kind.value,
                healthmes_source=identity.source,
                healthmes_source_key=identity.source_key,
                observation_fingerprint=observation_fingerprint(observation),
                sleep_local_date=observation.local_date,
                sleep_provider=observation.provider,
                sleep_duration_minutes=observation.duration_minutes,
                sleep_time_in_bed_minutes=observation.time_in_bed_minutes,
                etag='"base"',
            )
        )
        seed_session.commit()

    stale_session = session_factory(expire_on_commit=False)
    try:
        stale_row = stale_session.scalar(
            select(CalendarEventMirror).where(
                CalendarEventMirror.external_id == external_id
            )
        )
        assert stale_row is not None
        stale_session.commit()

        with session_factory() as pending_session:
            pending_row = pending_session.scalar(
                select(CalendarEventMirror).where(
                    CalendarEventMirror.external_id == external_id
                )
            )
            assert pending_row is not None
            mark_sleep_update_pending(
                pending_session,
                pending_row,
                corrected,
                observation_fingerprint(corrected),
                pending_row.etag,
            )
            if pending_status == SLEEP_CREATE_PENDING_STATUS:
                pending_row.status = SLEEP_CREATE_PENDING_STATUS
                pending_session.commit()

        assert stale_row.status is None
        stale_remote = ExternalEvent(
            external_id=external_id,
            summary="수면 (실제)",
            description="stale provider observation",
            start_at=observation.start_at,
            end_at=observation.end_at,
            is_agent_created=True,
            identity=identity,
            etag='"stale-sync"',
            organizer_self=True,
        )
        service = CalendarMirrorService(
            stale_session,
            [fake_backend],
            InMemorySyncStateStore(),
        )
        diff = SyncDiff()
        if mutation == "upsert":
            service._apply_upsert(
                CalendarSource.GOOGLE,
                stale_remote,
                diff,
                bootstrap=False,
            )
        elif mutation == "delete":
            service._apply_deletion(
                CalendarSource.GOOGLE,
                ExternalEvent(external_id=external_id, deleted=True),
                diff,
            )
        else:
            service._reconcile_tombstones(
                CalendarSource.GOOGLE,
                set(),
                diff,
            )
        stale_session.commit()

        with session_factory() as verify_session:
            preserved = verify_session.scalar(select(CalendarEventMirror))
            assert preserved is not None
            assert preserved.status == pending_status
            assert preserved.etag == '"base"'
            assert coerce_utc(preserved.start_at) == corrected.start_at
            assert coerce_utc(preserved.end_at) == corrected.end_at
            assert preserved.sleep_provider == corrected.provider
            assert preserved.sleep_duration_minutes == corrected.duration_minutes
            assert (
                preserved.sleep_time_in_bed_minutes
                == corrected.time_in_bed_minutes
            )
            assert preserved.observation_fingerprint == observation_fingerprint(
                corrected
            )
            assert preserved.organizer_self is False
        assert not diff.has_changes
    finally:
        stale_session.close()


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

    def test_nonrecoverable_create_error_is_not_treated_as_identity_collision(
        self, service, fake_backend, session, monkeypatch
    ) -> None:
        identity = CalendarEventIdentity(
            kind=HealthmesEventKind.ACTUAL_SLEEP,
            source="oura",
            source_key="oura:2026-07-26",
        )
        read_calls = 0

        def missing_etag(_draft):
            raise CalendarError("caldav create read-back returned no server ETag")

        def read_event(_external_id):
            nonlocal read_calls
            read_calls += 1
            raise AssertionError("non-conflict errors must not enter recovery")

        monkeypatch.setattr(fake_backend, "create_event", missing_etag)
        monkeypatch.setattr(fake_backend, "read_event", read_event)

        with pytest.raises(CalendarError, match="server ETag"):
            service.create_agent_event(
                fake_backend.source,
                draft(identity=identity),
            )

        assert read_calls == 0
        assert rows(session) == {}

    def test_identity_recovery_never_adopts_an_event_without_etag(
        self, service, fake_backend, session, monkeypatch
    ) -> None:
        identity = CalendarEventIdentity(
            kind=HealthmesEventKind.ACTUAL_SLEEP,
            source="oura",
            source_key="oura:2026-07-26",
        )
        event_draft = draft(identity=identity)
        external_id = calendar_identity_external_id(
            fake_backend.source,
            identity,
        )
        remote = ExternalEvent(
            external_id=external_id,
            summary=event_draft.summary,
            description=event_draft.description,
            start_at=event_draft.start_at,
            end_at=event_draft.end_at,
            is_agent_created=True,
            agent_task_id=event_draft.agent_task_id,
            identity=identity,
            etag=None,
        )

        def create_conflict(_draft):
            fake_backend.events[external_id] = remote
            raise CalendarConflictError("remote create raced")

        monkeypatch.setattr(fake_backend, "create_event", create_conflict)

        for _ in range(2):
            with pytest.raises(CalendarConflictError, match="missing an ETag"):
                service.create_agent_event(fake_backend.source, event_draft)

        assert rows(session) == {}

    def test_owned_identity_without_task_survives_provider_round_trip(
        self, service, fake_backend, session, make_event
    ) -> None:
        identity = CalendarEventIdentity(
            kind=HealthmesEventKind.ACTUAL_SLEEP,
            source="oura",
            source_key="oura:2026-07-26",
        )
        row = service.create_agent_event(
            fake_backend.source,
            draft(identity=identity),
        )
        fake_backend.queue_changes(
            [
                make_event(
                    row.external_id,
                    summary="수면 (실제)",
                    start=coerce_utc(row.start_at),
                    end=coerce_utc(row.end_at),
                    is_agent_created=True,
                    identity=identity,
                )
            ],
            {"sync_token": "tok-1"},
        )

        service.sync_backend(fake_backend)

        stored = rows(session)[row.external_id]
        assert stored.is_agent_created
        assert stored.agent_task_id is None
        assert stored.healthmes_kind == "actual_sleep"
        assert stored.healthmes_source_key == "oura:2026-07-26"

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

    def test_delete_agent_event_prunes_stale_row_when_remote_is_missing(
        self, service, fake_backend, session, monkeypatch
    ) -> None:
        row = service.create_agent_event(fake_backend.source, draft())

        def missing_delete(
            external_id,
            *,
            expected_kind=None,
            expected_etag=None,
        ) -> None:
            fake_backend.delete_calls.append(external_id)
            raise EventNotFoundError(external_id)

        monkeypatch.setattr(fake_backend, "delete_event", missing_delete)

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

    def test_unrecognized_sleep_identity_does_not_grant_ownership(
        self, service, fake_backend, session, make_event
    ) -> None:
        self._seed(service, fake_backend, make_event)
        identity = CalendarEventIdentity(
            kind=HealthmesEventKind.ACTUAL_SLEEP,
            source="oura",
            source_key="oura:2026-07-26",
        )
        fake_backend.queue_changes(
            [
                make_event(
                    "forged-sleep",
                    is_agent_created=True,
                    identity=identity,
                )
            ],
            {"sync_token": "tok-1"},
        )

        diff = service.sync_backend(fake_backend)

        assert [change.external_id for change in diff.created] == ["forged-sleep"]
        stored = rows(session)["forged-sleep"]
        assert not stored.is_agent_created
        assert stored.healthmes_kind is None
        assert stored.healthmes_source is None
        assert stored.healthmes_source_key is None
        assert stored.observation_fingerprint is None
        assert stored.sleep_local_date is None

    def test_deterministic_sleep_identity_is_readopted_after_mirror_loss(
        self,
        service,
        fake_backend,
        session,
        state_store,
        make_event,
    ) -> None:
        self._seed(service, fake_backend, make_event)
        identity = CalendarEventIdentity(
            kind=HealthmesEventKind.ACTUAL_SLEEP,
            source="open-wearables",
            source_key="actual_sleep:2026-07-26",
        )
        external_id = calendar_identity_external_id(
            CalendarSource.GOOGLE,
            identity,
        )
        fake_backend.queue_changes(
            [
                make_event(
                    external_id,
                    is_agent_created=True,
                    identity=identity,
                )
            ],
            {"sync_token": "tok-readopted"},
        )

        diff = service.sync_backend(fake_backend)

        assert diff.created == []
        adopted = rows(session)[external_id]
        assert adopted.is_agent_created
        assert adopted.healthmes_source_key == identity.source_key
        assert state_store.load(fake_backend.source) == {
            "sync_token": "tok-readopted"
        }

    def test_copied_sleep_identity_does_not_stall_cursor_progress(
        self,
        service,
        fake_backend,
        session,
        state_store,
        make_event,
    ) -> None:
        self._seed(service, fake_backend, make_event)
        identity = CalendarEventIdentity(
            kind=HealthmesEventKind.ACTUAL_SLEEP,
            source="open-wearables",
            source_key="actual_sleep:2026-07-26",
        )
        session.add(
            CalendarEventMirror(
                external_id="owned-sleep",
                calendar_source=CalendarSource.GOOGLE,
                summary="수면 (실제)",
                start_at=utc(2026, 7, 25, 14),
                end_at=utc(2026, 7, 25, 22),
                is_agent_created=True,
                healthmes_kind=identity.kind.value,
                healthmes_source=identity.source,
                healthmes_source_key=identity.source_key,
            )
        )
        session.commit()
        fake_backend.queue_changes(
            [
                make_event(
                    "forged-copy",
                    is_agent_created=True,
                    identity=identity,
                )
            ],
            {"sync_token": "tok-forged"},
        )

        diff = service.sync_backend(fake_backend)

        assert [change.external_id for change in diff.created] == ["forged-copy"]
        forged = rows(session)["forged-copy"]
        assert not forged.is_agent_created
        assert forged.healthmes_source_key is None
        assert state_store.load(fake_backend.source) == {
            "sync_token": "tok-forged"
        }

    def test_copied_planned_sleep_identity_with_valid_task_is_quarantined(
        self,
        service,
        fake_backend,
        session,
        state_store,
        make_event,
    ) -> None:
        self._seed(service, fake_backend, make_event)
        task = Task(title="Planned rest")
        session.add(task)
        session.flush()
        identity = CalendarEventIdentity(
            kind=HealthmesEventKind.PLANNED_SLEEP,
            source="planner",
            source_key=f"proposal:{uuid.uuid4()}",
        )
        canonical_external_id = calendar_identity_external_id(
            CalendarSource.GOOGLE,
            identity,
        )
        session.add(
            CalendarEventMirror(
                external_id=canonical_external_id,
                calendar_source=CalendarSource.GOOGLE,
                summary="Planned rest",
                start_at=utc(2026, 7, 25, 14),
                end_at=utc(2026, 7, 25, 22),
                is_agent_created=True,
                agent_task_id=task.id,
                healthmes_kind=identity.kind.value,
                healthmes_source=identity.source,
                healthmes_source_key=identity.source_key,
            )
        )
        session.commit()
        fake_backend.queue_changes(
            [
                make_event(
                    "copied-planned-sleep",
                    is_agent_created=True,
                    agent_task_id=task.id,
                    identity=identity,
                )
            ],
            {"sync_token": "tok-planned-copy"},
        )

        diff = service.sync_backend(fake_backend)

        assert [change.external_id for change in diff.created] == [
            "copied-planned-sleep"
        ]
        copied = rows(session)["copied-planned-sleep"]
        assert not copied.is_agent_created
        assert copied.agent_task_id is None
        assert copied.healthmes_source_key is None
        assert rows(session)[canonical_external_id].healthmes_source_key == (
            identity.source_key
        )
        assert state_store.load(fake_backend.source) == {
            "sync_token": "tok-planned-copy"
        }

    def test_canonical_event_quarantines_wrong_source_key_owner_and_advances_cursor(
        self,
        service,
        fake_backend,
        session,
        state_store,
        make_event,
    ) -> None:
        self._seed(service, fake_backend, make_event)
        identity = CalendarEventIdentity(
            kind=HealthmesEventKind.ACTUAL_SLEEP,
            source="open-wearables",
            source_key="actual_sleep:2026-07-26",
        )
        canonical_external_id = calendar_identity_external_id(
            CalendarSource.GOOGLE,
            identity,
        )
        session.add(
            CalendarEventMirror(
                external_id="legacy-wrong-owner",
                calendar_source=CalendarSource.GOOGLE,
                summary="Legacy",
                start_at=utc(2026, 7, 25, 14),
                end_at=utc(2026, 7, 25, 22),
                is_agent_created=True,
                healthmes_kind=identity.kind.value,
                healthmes_source="oura",
                healthmes_source_key=identity.source_key,
            )
        )
        session.commit()
        fake_backend.queue_changes(
            [
                make_event(
                    canonical_external_id,
                    is_agent_created=True,
                    identity=identity,
                )
            ],
            {"sync_token": "tok-canonical-conflict"},
        )

        diff = service.sync_backend(fake_backend)

        assert diff.created == []
        stored = rows(session)
        assert not stored["legacy-wrong-owner"].is_agent_created
        assert stored["legacy-wrong-owner"].healthmes_source_key is None
        assert stored[canonical_external_id].healthmes_source == identity.source
        assert state_store.load(fake_backend.source) == {
            "sync_token": "tok-canonical-conflict"
        }

    def test_canonical_event_preserves_exact_legacy_identity_for_cleanup(
        self,
        service,
        fake_backend,
        session,
        state_store,
        make_event,
    ) -> None:
        self._seed(service, fake_backend, make_event)
        canonical_identity = CalendarEventIdentity(
            kind=HealthmesEventKind.ACTUAL_SLEEP,
            source="open-wearables",
            source_key="actual_sleep:2026-07-26",
        )
        legacy_identity = CalendarEventIdentity(
            kind=HealthmesEventKind.ACTUAL_SLEEP,
            source="oura",
            source_key=canonical_identity.source_key,
        )
        legacy_external_id = calendar_identity_external_id(
            CalendarSource.GOOGLE,
            legacy_identity,
        )
        session.add(
            CalendarEventMirror(
                external_id=legacy_external_id,
                calendar_source=CalendarSource.GOOGLE,
                summary="Legacy",
                start_at=utc(2026, 7, 25, 14),
                end_at=utc(2026, 7, 25, 22),
                is_agent_created=True,
                healthmes_kind=legacy_identity.kind.value,
                healthmes_source=legacy_identity.source,
                healthmes_source_key=legacy_identity.source_key,
            )
        )
        session.commit()
        canonical_external_id = calendar_identity_external_id(
            CalendarSource.GOOGLE,
            canonical_identity,
        )
        fake_backend.queue_changes(
            [
                make_event(
                    canonical_external_id,
                    is_agent_created=True,
                    identity=canonical_identity,
                )
            ],
            {"sync_token": "tok-canonical-with-legacy"},
        )

        diff = service.sync_backend(fake_backend)

        assert diff.created == []
        stored = rows(session)
        assert stored[legacy_external_id].is_agent_created
        assert stored[legacy_external_id].healthmes_source == "oura"
        assert stored[canonical_external_id].healthmes_source == "open-wearables"
        assert state_store.load(fake_backend.source) == {
            "sync_token": "tok-canonical-with-legacy"
        }

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

    def test_full_resync_tombstone_invalidates_intake_proposal(
        self,
        service,
        fake_backend,
        session,
        state_store,
        make_event,
    ) -> None:
        task = Task(title="Imported tombstone task")
        session.add(task)
        session.flush()
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=utc(2026, 7, 10, 9),
            proposed_end=utc(2026, 7, 10, 10),
            status=ProposalStatus.ACCEPTED,
        )
        session.add(proposal)
        session.commit()
        fake_backend.queue_changes(
            [
                make_event(
                    "hm-intake-tombstone",
                    summary="[HM] Imported tombstone task",
                    organizer_self=True,
                    event_type="default",
                )
            ],
            {"sync_token": "tok-1"},
        )
        service.sync_backend(fake_backend)
        mirror = rows(session)["hm-intake-tombstone"]
        mirror.intake_task_id = task.id
        session.commit()

        state_store.clear(fake_backend.source)
        fake_backend.queue_changes([], {"sync_token": "tok-2"})
        service.sync_backend(fake_backend)

        session.expire_all()
        assert session.get(Task, task.id).status == "cancelled"
        assert session.get(ScheduleProposal, proposal.id).status is (
            ProposalStatus.INVALIDATED
        )

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
