"""Calendar poll-job tests: enablement wiring, sync runs, proposal push.

These pin the production entry point of the calendar plane (docs/PLAN.md §6):
the settings flags actually build jobs, each run syncs the mirror, and the
write backend advances accepted proposals to ``pushed`` by writing tagged
agent blocks — the contract promised by healthmes/api/schedule.py.
"""

import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from healthmes.calendars import creds
from healthmes.calendars.base import (
    CalendarAuthError,
    CalendarConflictError,
    CalendarEventIdentity,
    EventDraft,
    EventNotFoundError,
    HealthmesEventKind,
)
from healthmes.calendars.intake import intake_calendar_tasks, intake_revision
from healthmes.calendars.jobs import (
    _proposal_identity,
    build_calendar_job,
    build_calendar_jobs,
    calendar_job_id,
    enabled_sources,
    push_accepted_proposals,
    write_source,
)
from healthmes.calendars.state import (
    InMemoryPendingDiffStore,
    InMemorySyncHealthStore,
    InMemorySyncStateStore,
    SyncCoverageKind,
    SyncHealthStatus,
)
from healthmes.calendars.sync import CalendarMirrorService
from healthmes.store import (
    Base,
    CalendarEventMirror,
    CalendarSource,
    ProposalStatus,
    ScheduleProposal,
    Task,
    TaskSource,
    create_db_engine,
)
from tests.calendars.conftest import FakeCalendarBackend


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


class TestEnablement:
    def test_disabled_flags_build_no_jobs(self, settings) -> None:
        assert build_calendar_jobs(settings) == []
        assert enabled_sources(settings) == ()
        assert write_source(settings) is None

    def test_google_enabled_builds_google_job(self, settings) -> None:
        enabled = settings.model_copy(update={"google_calendar_enabled": True})
        specs = build_calendar_jobs(enabled)
        assert [spec.source for spec in specs] == [CalendarSource.GOOGLE]
        assert specs[0].job_id == calendar_job_id(CalendarSource.GOOGLE)
        assert specs[0].interval_minutes == enabled.google_poll_minutes
        assert write_source(enabled) is CalendarSource.GOOGLE

    def test_both_enabled_builds_both_with_google_as_writer(self, settings) -> None:
        enabled = settings.model_copy(
            update={"google_calendar_enabled": True, "caldav_enabled": True}
        )
        specs = build_calendar_jobs(enabled)
        assert [spec.source for spec in specs] == [
            CalendarSource.GOOGLE,
            CalendarSource.CALDAV,
        ]
        assert [spec.interval_minutes for spec in specs] == [
            enabled.google_poll_minutes,
            enabled.caldav_poll_minutes,
        ]
        assert write_source(enabled) is CalendarSource.GOOGLE

    def test_caldav_only_is_the_writer(self, settings) -> None:
        enabled = settings.model_copy(update={"caldav_enabled": True})
        assert write_source(enabled) is CalendarSource.CALDAV
        (spec,) = build_calendar_jobs(enabled)
        assert spec.interval_minutes == enabled.caldav_poll_minutes


class TestJobRun:
    def test_disconnect_waits_for_sync_and_reconnect_rebuilds_backend(
        self,
        settings,
        tmp_path,
    ) -> None:
        database_url = (
            f"sqlite+pysqlite:///{tmp_path / 'calendar-fence.db'}"
        )
        engine = create_db_engine(database_url)
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        generation = {"value": "generation-1"}
        sync_started = threading.Event()
        release_sync = threading.Event()
        disconnect_attempted = threading.Event()
        disconnect_completed = threading.Event()
        failures: list[BaseException] = []

        class BlockingBackend(FakeCalendarBackend):
            def list_changes(self, sync_state):
                sync_started.set()
                assert release_sync.wait(timeout=5)
                return super().list_changes(sync_state)

        backends = [
            BlockingBackend(CalendarSource.GOOGLE),
            FakeCalendarBackend(CalendarSource.GOOGLE),
        ]
        factory_calls = 0

        def build_backend():
            nonlocal factory_calls
            backend = backends[factory_calls]
            factory_calls += 1
            return backend

        job = build_calendar_job(
            settings.model_copy(update={"database_url": database_url}),
            CalendarSource.GOOGLE,
            is_write_backend=False,
            backend_factory=build_backend,
            session_factory=factory,
            state_store=InMemorySyncStateStore(),
            pending_store=InMemoryPendingDiffStore(),
            health_store=InMemorySyncHealthStore(),
            connection_generation_resolver=lambda: generation["value"],
        )

        def run_sync() -> None:
            try:
                job()
            except BaseException as exc:
                failures.append(exc)

        def disconnect() -> None:
            try:
                disconnect_attempted.set()
                with factory() as session:
                    with creds.calendar_connection_write(
                        session,
                        CalendarSource.GOOGLE,
                    ):
                        generation["value"] = None
                disconnect_completed.set()
            except BaseException as exc:
                failures.append(exc)

        sync_thread = threading.Thread(target=run_sync)
        disconnect_thread = threading.Thread(target=disconnect)
        try:
            sync_thread.start()
            assert sync_started.wait(timeout=5)
            disconnect_thread.start()
            assert disconnect_attempted.wait(timeout=5)
            time.sleep(0.1)
            assert not disconnect_completed.is_set()

            release_sync.set()
            sync_thread.join(timeout=10)
            disconnect_thread.join(timeout=10)

            assert not sync_thread.is_alive()
            assert not disconnect_thread.is_alive()
            assert disconnect_completed.is_set()
            assert failures == []
            assert factory_calls == 1
            assert len(backends[0].received_sync_states) == 1

            assert job() is None
            assert factory_calls == 1
            assert len(backends[0].received_sync_states) == 1

            with factory() as session:
                with creds.calendar_connection_write(
                    session,
                    CalendarSource.GOOGLE,
                ):
                    generation["value"] = "generation-2"

            assert job() is not None
            assert factory_calls == 2
            assert len(backends[0].received_sync_states) == 1
            assert len(backends[1].received_sync_states) == 1
        finally:
            release_sync.set()
            sync_thread.join(timeout=5)
            disconnect_thread.join(timeout=5)
            engine.dispose()

    def test_health_attempt_precedes_backend_construction_and_empty_success(
        self,
        settings,
        session_factory,
        fake_backend,
    ) -> None:
        health_store = InMemorySyncHealthStore()
        timeline: list[str] = []
        times = iter(
            [
                utc(2026, 8, 12, 9),
                utc(2026, 8, 12, 9, 0, 2),
            ]
        )

        class OrderedHealthStore(InMemorySyncHealthStore):
            def record_attempt(self, source, at) -> None:
                timeline.append("attempt")
                super().record_attempt(source, at)

            def record_success(
                self,
                source,
                at,
                *,
                event_count,
                coverage_kind=SyncCoverageKind.UNKNOWN,
                coverage_start=None,
                coverage_end=None,
                account_generation=None,
            ) -> None:
                timeline.append("success")
                super().record_success(
                    source,
                    at,
                    event_count=event_count,
                    coverage_kind=coverage_kind,
                    coverage_start=coverage_start,
                    coverage_end=coverage_end,
                    account_generation=account_generation,
                )

        health_store = OrderedHealthStore()

        def build_backend():
            timeline.append("backend")
            return fake_backend

        job = build_calendar_job(
            settings,
            fake_backend.source,
            is_write_backend=False,
            backend_factory=build_backend,
            session_factory=session_factory,
            state_store=InMemorySyncStateStore(),
            health_store=health_store,
            clock=lambda: next(times),
        )

        assert job() is not None
        assert timeline == ["attempt", "backend", "success"]
        health = health_store.load(fake_backend.source)
        assert health is not None
        assert health.status is SyncHealthStatus.EMPTY_SUCCESS
        assert health.last_attempt_at == utc(2026, 8, 12, 9)
        assert health.last_success_at == utc(2026, 8, 12, 9, 0, 2)
        assert health.last_success_event_count == 0

    def test_health_success_is_recorded_after_calendar_commit(
        self,
        settings,
        session_factory,
        fake_backend,
        make_event,
    ) -> None:
        fake_backend.queue_changes(
            [make_event("health-commit", summary="Private planning title")],
            {"sync_token": "tok-1"},
        )

        class CommitObservingHealthStore(InMemorySyncHealthStore):
            def record_success(
                self,
                source,
                at,
                *,
                event_count,
                coverage_kind=SyncCoverageKind.UNKNOWN,
                coverage_start=None,
                coverage_end=None,
                account_generation=None,
            ) -> None:
                with session_factory() as observer:
                    rows = observer.scalars(
                        select(CalendarEventMirror).where(
                            CalendarEventMirror.calendar_source == source
                        )
                    ).all()
                assert [row.external_id for row in rows] == ["health-commit"]
                assert event_count == 1
                super().record_success(
                    source,
                    at,
                    event_count=event_count,
                    coverage_kind=coverage_kind,
                    coverage_start=coverage_start,
                    coverage_end=coverage_end,
                    account_generation=account_generation,
                )

        health_store = CommitObservingHealthStore()
        times = iter(
            [
                utc(2026, 8, 12, 9),
                utc(2026, 8, 12, 9, 0, 2),
            ]
        )
        job = build_calendar_job(
            settings,
            fake_backend.source,
            is_write_backend=False,
            backend_factory=lambda: fake_backend,
            session_factory=session_factory,
            state_store=InMemorySyncStateStore(),
            health_store=health_store,
            clock=lambda: next(times),
        )

        assert job() is not None
        health = health_store.load(fake_backend.source)
        assert health is not None
        assert health.status is SyncHealthStatus.SUCCESS
        assert health.last_success_event_count == 1

    def test_backend_failure_records_sanitized_code_without_message(
        self,
        settings,
        session_factory,
    ) -> None:
        health_store = InMemorySyncHealthStore()
        credential = "user@example.test:super-secret-app-password"
        times = iter(
            [
                utc(2026, 8, 12, 9),
                utc(2026, 8, 12, 9, 0, 1),
            ]
        )

        def exploding_factory():
            raise CalendarAuthError(credential)

        job = build_calendar_job(
            settings,
            CalendarSource.GOOGLE,
            is_write_backend=False,
            backend_factory=exploding_factory,
            session_factory=session_factory,
            state_store=InMemorySyncStateStore(),
            health_store=health_store,
            clock=lambda: next(times),
        )

        assert job() is None
        health = health_store.load(CalendarSource.GOOGLE)
        assert health is not None
        assert health.status is SyncHealthStatus.RECENT_FAILURE
        assert health.last_error_code == "calendar_auth_error"
        assert credential not in repr(health)

    def test_success_then_failure_and_recovery_preserve_history(
        self,
        settings,
        session_factory,
        fake_backend,
    ) -> None:
        health_store = InMemorySyncHealthStore()
        times = iter(
            [
                utc(2026, 8, 12, 9),
                utc(2026, 8, 12, 9, 0, 1),
                utc(2026, 8, 12, 9, 5),
                utc(2026, 8, 12, 9, 5, 1),
                utc(2026, 8, 12, 9, 10),
                utc(2026, 8, 12, 9, 10, 1),
            ]
        )
        job = build_calendar_job(
            settings,
            fake_backend.source,
            is_write_backend=False,
            backend_factory=lambda: fake_backend,
            session_factory=session_factory,
            state_store=InMemorySyncStateStore(),
            health_store=health_store,
            clock=lambda: next(times),
        )

        assert job() is not None
        original_list_changes = fake_backend.list_changes

        def fail_sync(_state):
            raise TimeoutError("private provider response")

        fake_backend.list_changes = fail_sync
        assert job() is None
        failed = health_store.load(fake_backend.source)
        assert failed is not None
        assert failed.status is SyncHealthStatus.RECENT_FAILURE
        assert failed.last_success_at == utc(2026, 8, 12, 9, 0, 1)
        assert failed.last_error_code == "calendar_timeout"

        fake_backend.list_changes = original_list_changes
        assert job() is not None
        recovered = health_store.load(fake_backend.source)
        assert recovered is not None
        assert recovered.status is SyncHealthStatus.EMPTY_SUCCESS
        assert recovered.last_failure_at == utc(2026, 8, 12, 9, 5, 1)
        assert recovered.last_error_code == "calendar_timeout"
        assert recovered.last_success_at == utc(2026, 8, 12, 9, 10, 1)

    @pytest.mark.parametrize(
        "failing_method",
        ["record_attempt", "record_success"],
    )
    def test_health_write_failure_does_not_mask_success(
        self,
        settings,
        session_factory,
        fake_backend,
        failing_method,
    ) -> None:
        class FailingHealthStore(InMemorySyncHealthStore):
            def record_attempt(self, source, at) -> None:
                if failing_method == "record_attempt":
                    raise OSError("health store unavailable")
                super().record_attempt(source, at)

            def record_success(
                self,
                source,
                at,
                *,
                event_count,
                coverage_kind=SyncCoverageKind.UNKNOWN,
                coverage_start=None,
                coverage_end=None,
                account_generation=None,
            ) -> None:
                if failing_method == "record_success":
                    raise OSError("health store unavailable")
                super().record_success(
                    source,
                    at,
                    event_count=event_count,
                    coverage_kind=coverage_kind,
                    coverage_start=coverage_start,
                    coverage_end=coverage_end,
                    account_generation=account_generation,
                )

        job = build_calendar_job(
            settings,
            fake_backend.source,
            is_write_backend=False,
            backend_factory=lambda: fake_backend,
            session_factory=session_factory,
            state_store=InMemorySyncStateStore(),
            health_store=FailingHealthStore(),
            clock=lambda: utc(2026, 8, 12, 9),
        )

        result = job()

        assert result is not None
        with session_factory() as persisted:
            assert persisted.scalars(select(CalendarEventMirror)).all() == []

    def test_failure_health_write_does_not_escape_scheduler(
        self,
        settings,
        session_factory,
    ) -> None:
        class FailingHealthStore(InMemorySyncHealthStore):
            def record_failure(self, source, at, *, error_code) -> None:
                raise OSError("health store unavailable")

        def exploding_factory():
            raise RuntimeError("private backend failure")

        job = build_calendar_job(
            settings,
            CalendarSource.GOOGLE,
            is_write_backend=False,
            backend_factory=exploding_factory,
            session_factory=session_factory,
            state_store=InMemorySyncStateStore(),
            health_store=FailingHealthStore(),
            clock=lambda: utc(2026, 8, 12, 9),
        )

        assert job() is None

    def test_writeback_exception_does_not_replace_inbound_success(
        self,
        settings,
        session_factory,
        fake_backend,
        monkeypatch,
    ) -> None:
        with session_factory() as setup:
            task = Task(title="Retry calendar writeback")
            setup.add(task)
            setup.flush()
            proposal = ScheduleProposal(
                task_id=task.id,
                proposed_start=utc(2026, 8, 12, 10),
                proposed_end=utc(2026, 8, 12, 11),
                status=ProposalStatus.ACCEPTED,
            )
            setup.add(proposal)
            setup.commit()
            proposal_id = proposal.id

        health_store = InMemorySyncHealthStore()
        times = iter(
            [
                utc(2026, 8, 12, 9),
                utc(2026, 8, 12, 9, 0, 1),
                utc(2026, 8, 12, 9, 0, 2),
                utc(2026, 8, 12, 9, 0, 3),
            ]
        )

        def fail_writeback(*_args, **_kwargs):
            raise TimeoutError("private provider response")

        monkeypatch.setattr(
            "healthmes.calendars.jobs.push_accepted_proposals",
            fail_writeback,
        )
        job = build_calendar_job(
            settings,
            fake_backend.source,
            is_write_backend=True,
            backend_factory=lambda: fake_backend,
            session_factory=session_factory,
            state_store=InMemorySyncStateStore(),
            health_store=health_store,
            clock=lambda: next(times),
        )

        assert job() is not None
        health = health_store.load(fake_backend.source)
        assert health is not None
        assert health.status is SyncHealthStatus.EMPTY_SUCCESS
        assert health.last_success_at == utc(2026, 8, 12, 9, 0, 1)
        assert health.last_failure_at is None
        assert health.writeback_last_attempt_at == utc(
            2026,
            8,
            12,
            9,
            0,
            2,
        )
        assert health.writeback_last_failure_at == utc(
            2026,
            8,
            12,
            9,
            0,
            3,
        )
        assert health.writeback_last_error_code == "calendar_timeout"
        assert health.writeback_attempted_count == 1
        assert health.writeback_succeeded_count == 0
        assert health.writeback_failed_count == 1
        with session_factory() as persisted:
            assert persisted.get(ScheduleProposal, proposal_id).status is (
                ProposalStatus.ACCEPTED
            )

    def test_writeback_preparation_failure_does_not_replace_inbound_success(
        self,
        settings,
        session_factory,
        fake_backend,
        monkeypatch,
    ) -> None:
        health_store = InMemorySyncHealthStore()
        times = iter(
            [
                utc(2026, 8, 12, 9),
                utc(2026, 8, 12, 9, 0, 1),
                utc(2026, 8, 12, 9, 0, 2),
                utc(2026, 8, 12, 9, 0, 3),
            ]
        )

        def fail_preparation(_session):
            raise RuntimeError("private writeback preparation failure")

        monkeypatch.setattr(
            "healthmes.calendars.jobs._accepted_proposal_ids",
            fail_preparation,
        )
        job = build_calendar_job(
            settings,
            fake_backend.source,
            is_write_backend=True,
            backend_factory=lambda: fake_backend,
            session_factory=session_factory,
            state_store=InMemorySyncStateStore(),
            health_store=health_store,
            clock=lambda: next(times),
        )

        assert job() is not None
        health = health_store.load(fake_backend.source)
        assert health is not None
        assert health.status is SyncHealthStatus.EMPTY_SUCCESS
        assert health.last_success_at == utc(2026, 8, 12, 9, 0, 1)
        assert health.last_failure_at is None
        assert health.writeback_last_attempt_at == utc(
            2026,
            8,
            12,
            9,
            0,
            2,
        )
        assert health.writeback_last_failure_at == utc(
            2026,
            8,
            12,
            9,
            0,
            3,
        )
        assert health.writeback_last_error_code == "calendar_sync_error"
        assert health.writeback_attempted_count == 0
        assert health.writeback_succeeded_count == 0
        assert health.writeback_failed_count == 0

    def test_intake_opt_out_is_sticky_until_hm_is_readded(self, session) -> None:
        mirror = CalendarEventMirror(
            external_id="hm-opt-out",
            calendar_source=CalendarSource.GOOGLE,
            summary="[HM] Write launch notes",
            start_at=utc(2026, 8, 4, 9),
            end_at=utc(2026, 8, 4, 10),
            organizer_self=True,
            event_type="default",
            etag="etag-1",
        )
        session.add(mirror)
        session.commit()

        [created] = intake_calendar_tasks(
            session,
            CalendarSource.GOOGLE,
            UTC,
        )
        session.commit()
        original_task_id = created.id
        assert created.source is TaskSource.USER

        mirror.summary = "Write launch notes"
        mirror.etag = "etag-2"
        [cancelled] = intake_calendar_tasks(
            session,
            CalendarSource.GOOGLE,
            UTC,
        )
        session.commit()
        assert cancelled.id == original_task_id
        assert cancelled.status == "cancelled"
        assert mirror.intake_task_id is None
        assert mirror.intake_opted_out is True

        assert intake_calendar_tasks(
            session,
            CalendarSource.GOOGLE,
            UTC,
        ) == ()
        assert len(session.scalars(select(Task)).all()) == 1

        mirror.summary = "[HM] Write revised launch notes"
        mirror.etag = "etag-3"
        [readded] = intake_calendar_tasks(
            session,
            CalendarSource.GOOGLE,
            UTC,
        )
        session.commit()
        assert readded.id != original_task_id
        assert readded.title == "Write revised launch notes"
        assert mirror.intake_task_id == readded.id
        assert mirror.intake_opted_out is False

    def test_timed_intake_event_is_adopted_without_provider_create(
        self,
        session,
        fake_backend,
    ) -> None:
        task = Task(title="Write launch notes", source=TaskSource.USER)
        session.add(task)
        session.flush()
        mirror = CalendarEventMirror(
            external_id="hm-timed",
            calendar_source=CalendarSource.GOOGLE,
            summary="[HM] Write launch notes",
            start_at=utc(2026, 8, 4, 9),
            end_at=utc(2026, 8, 4, 10),
            organizer_self=True,
            event_type="default",
            etag="etag-1",
            intake_task_id=task.id,
        )
        session.add(mirror)
        session.commit()
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=mirror.start_at,
            proposed_end=mirror.end_at,
            status=ProposalStatus.ACCEPTED,
            intake_calendar_source=mirror.calendar_source,
            intake_account_generation=mirror.connection_generation,
            intake_external_id=mirror.external_id,
            intake_revision=intake_revision(mirror),
        )
        session.add(proposal)
        session.commit()
        service = CalendarMirrorService(
            session,
            [fake_backend],
            InMemorySyncStateStore(),
        )

        assert push_accepted_proposals(
            service,
            session,
            fake_backend.source,
        ) == 1
        assert fake_backend.created_drafts == []
        assert session.get(ScheduleProposal, proposal.id).status is (
            ProposalStatus.PUSHED
        )
        assert session.get(Task, task.id).status == "scheduled"
        assert mirror.is_agent_created is False

    def test_changed_timed_intake_invalidates_instead_of_creating_duplicate(
        self,
        session,
        fake_backend,
    ) -> None:
        task = Task(title="Write launch notes", source=TaskSource.USER)
        session.add(task)
        session.flush()
        mirror = CalendarEventMirror(
            external_id="hm-changed",
            calendar_source=CalendarSource.GOOGLE,
            summary="[HM] Write launch notes",
            start_at=utc(2026, 8, 4, 9),
            end_at=utc(2026, 8, 4, 10),
            organizer_self=True,
            event_type="default",
            etag="etag-1",
            intake_task_id=task.id,
        )
        session.add(mirror)
        session.commit()
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=mirror.start_at,
            proposed_end=mirror.end_at,
            status=ProposalStatus.ACCEPTED,
            intake_calendar_source=mirror.calendar_source,
            intake_account_generation=mirror.connection_generation,
            intake_external_id=mirror.external_id,
            intake_revision=intake_revision(mirror),
        )
        session.add(proposal)
        session.commit()

        mirror.end_at = mirror.end_at + timedelta(minutes=30)
        mirror.etag = "etag-2"
        session.commit()
        service = CalendarMirrorService(
            session,
            [fake_backend],
            InMemorySyncStateStore(),
        )

        assert push_accepted_proposals(
            service,
            session,
            fake_backend.source,
        ) == 0
        assert fake_backend.created_drafts == []
        assert session.get(ScheduleProposal, proposal.id).status is (
            ProposalStatus.INVALIDATED
        )
        assert session.get(
            ScheduleProposal,
            proposal.id,
        ).invalidation_reason == "calendar_intake_event_changed"

    def test_cross_provider_timed_intake_uses_original_account_generation(
        self,
        session,
        fake_backend_factory,
        monkeypatch,
    ) -> None:
        google_generation = "google-account-a"
        caldav_generation = "caldav-account-b"
        task = Task(title="Use existing Google block", source=TaskSource.USER)
        session.add(task)
        session.flush()
        mirror = CalendarEventMirror(
            external_id="google-timed-intake",
            calendar_source=CalendarSource.GOOGLE,
            connection_generation=google_generation,
            summary="[HM] Use existing Google block",
            start_at=utc(2026, 8, 4, 9),
            end_at=utc(2026, 8, 4, 10),
            organizer_self=True,
            event_type="default",
            etag="etag-1",
            intake_task_id=task.id,
        )
        session.add(mirror)
        session.flush()
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=mirror.start_at,
            proposed_end=mirror.end_at,
            status=ProposalStatus.ACCEPTED,
            intake_calendar_source=mirror.calendar_source,
            intake_account_generation=google_generation,
            intake_external_id=mirror.external_id,
            intake_revision=intake_revision(mirror),
        )
        session.add(proposal)
        session.commit()
        caldav_backend = fake_backend_factory(CalendarSource.CALDAV)
        service = CalendarMirrorService(
            session,
            [caldav_backend],
            InMemorySyncStateStore(),
            account_generation=caldav_generation,
        )
        lock_entries: list[tuple[CalendarSource, ...]] = []

        @contextmanager
        def capture_lock_plan(waiting_session, sources):
            assert not waiting_session.in_transaction()
            lock_entries.append(tuple(sources))
            yield

        monkeypatch.setattr(
            "healthmes.calendars.jobs.calendar_write_locks",
            capture_lock_plan,
        )

        assert push_accepted_proposals(
            service,
            session,
            CalendarSource.CALDAV,
            current_account_generations={
                CalendarSource.GOOGLE: google_generation,
                CalendarSource.CALDAV: caldav_generation,
            },
        ) == 1

        assert caldav_backend.created_drafts == []
        assert lock_entries == [
            (CalendarSource.GOOGLE, CalendarSource.CALDAV)
        ]
        stored = session.get(ScheduleProposal, proposal.id)
        assert stored.status is ProposalStatus.PUSHED
        assert stored.invalidation_reason is None

    def test_timed_intake_from_reconnected_account_is_explicitly_stale(
        self,
        session,
        fake_backend,
    ) -> None:
        original_generation = "google-account-a"
        task = Task(title="Stale account block", source=TaskSource.USER)
        session.add(task)
        session.flush()
        mirror = CalendarEventMirror(
            external_id="stale-google-intake",
            calendar_source=CalendarSource.GOOGLE,
            connection_generation=original_generation,
            summary="[HM] Stale account block",
            start_at=utc(2026, 8, 4, 9),
            end_at=utc(2026, 8, 4, 10),
            organizer_self=True,
            event_type="default",
            etag="etag-1",
            intake_task_id=task.id,
        )
        session.add(mirror)
        session.flush()
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=mirror.start_at,
            proposed_end=mirror.end_at,
            status=ProposalStatus.ACCEPTED,
            intake_calendar_source=mirror.calendar_source,
            intake_account_generation=original_generation,
            intake_external_id=mirror.external_id,
            intake_revision=intake_revision(mirror),
        )
        session.add(proposal)
        session.commit()
        service = CalendarMirrorService(
            session,
            [fake_backend],
            InMemorySyncStateStore(),
            account_generation="google-account-b",
        )

        assert push_accepted_proposals(
            service,
            session,
            CalendarSource.GOOGLE,
            current_account_generations={
                CalendarSource.GOOGLE: "google-account-b"
            },
        ) == 0

        assert fake_backend.created_drafts == []
        stored = session.get(ScheduleProposal, proposal.id)
        assert stored.status is ProposalStatus.INVALIDATED
        assert (
            stored.invalidation_reason
            == "calendar_intake_account_changed"
        )

    def test_all_day_intake_still_creates_provider_agnostic_output_block(
        self,
        session,
        fake_backend_factory,
    ) -> None:
        task = Task(title="Prepare launch", source=TaskSource.USER)
        session.add(task)
        session.flush()
        session.add(
            CalendarEventMirror(
                external_id="hm-all-day",
                calendar_source=CalendarSource.GOOGLE,
                summary="[HM] Prepare launch",
                start_at=utc(2026, 8, 4),
                end_at=utc(2026, 8, 5),
                organizer_self=True,
                event_type="default",
                is_all_day=True,
                etag="etag-1",
                intake_task_id=task.id,
            )
        )
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=utc(2026, 8, 4, 9),
            proposed_end=utc(2026, 8, 4, 10),
            status=ProposalStatus.ACCEPTED,
        )
        session.add(proposal)
        session.commit()
        caldav_backend = fake_backend_factory(CalendarSource.CALDAV)
        service = CalendarMirrorService(
            session,
            [caldav_backend],
            InMemorySyncStateStore(),
        )

        assert push_accepted_proposals(
            service,
            session,
            CalendarSource.CALDAV,
        ) == 1
        assert [draft.summary for draft in caldav_backend.created_drafts] == [
            "Prepare launch"
        ]
        assert session.get(ScheduleProposal, proposal.id).status is (
            ProposalStatus.PUSHED
        )

    def test_job_syncs_backend_into_mirror(
        self, settings, session_factory, session, fake_backend, make_event
    ) -> None:
        fake_backend.queue_changes([make_event("meet-1")], {"sync_token": "tok-1"})
        job = build_calendar_job(
            settings,
            fake_backend.source,
            is_write_backend=False,
            backend_factory=lambda: fake_backend,
            session_factory=session_factory,
            state_store=InMemorySyncStateStore(),
        )

        job()

        rows = session.scalars(select(CalendarEventMirror)).all()
        assert [row.external_id for row in rows] == ["meet-1"]
        assert fake_backend.received_sync_states == [None]

    def test_job_commits_intake_before_proposal_push(
        self,
        settings,
        session_factory,
        fake_backend,
        make_event,
    ) -> None:
        fake_backend.queue_changes(
            [
                make_event(
                    "hm-intake",
                    summary="[HM] Persist imported task",
                    organizer_self=True,
                    event_type="default",
                )
            ],
            {"sync_token": "tok-1"},
        )
        job = build_calendar_job(
            settings,
            fake_backend.source,
            is_write_backend=True,
            backend_factory=lambda: fake_backend,
            session_factory=session_factory,
            state_store=InMemorySyncStateStore(),
        )

        assert job() is not None

        with session_factory() as persisted:
            task = persisted.scalars(select(Task)).one()
            mirror = persisted.scalars(select(CalendarEventMirror)).one()
            assert task.title == "Persist imported task"
            assert mirror.intake_task_id == task.id

    def test_job_returns_deletion_diff(
        self, settings, session_factory, session, fake_backend, make_event
    ) -> None:
        # F4: the poll job must RETURN the SyncDiff so the schedule_changed
        # trigger can consume deletions (which vanish from the mirror and so
        # cannot be re-derived from row updated_at).
        job = build_calendar_job(
            settings,
            fake_backend.source,
            is_write_backend=False,
            backend_factory=lambda: fake_backend,
            session_factory=session_factory,
            state_store=InMemorySyncStateStore(),
            pending_store=InMemoryPendingDiffStore(),
        )
        fake_backend.queue_changes([make_event("meet-1")], {"sync_token": "tok-1"})
        fake_backend.queue_changes(
            [make_event("meet-1", deleted=True, summary=None, etag=None)],
            {"sync_token": "tok-2"},
        )

        bootstrap_diff = job()  # silent adoption, but still a SyncDiff
        assert bootstrap_diff is not None and not bootstrap_diff.has_changes

        deletion_diff = job()
        assert deletion_diff is not None
        assert [change.external_id for change in deletion_diff.deleted] == ["meet-1"]

    def test_write_backend_pushes_accepted_proposals(
        self, settings, session_factory, session, fake_backend
    ) -> None:
        task = Task(title="Write weekly report")
        session.add(task)
        session.flush()
        session.add_all(
            [
                ScheduleProposal(
                    task_id=task.id,
                    proposed_start=utc(2026, 7, 10, 9, 0),
                    proposed_end=utc(2026, 7, 10, 11, 0),
                    status=ProposalStatus.ACCEPTED,
                ),
                ScheduleProposal(
                    task_id=task.id,
                    proposed_start=utc(2026, 7, 11, 9, 0),
                    proposed_end=utc(2026, 7, 11, 10, 0),
                    status=ProposalStatus.PROPOSED,  # not confirmed: never pushed
                ),
            ]
        )
        session.commit()

        job = build_calendar_job(
            settings,
            fake_backend.source,
            is_write_backend=True,
            backend_factory=lambda: fake_backend,
            session_factory=session_factory,
            state_store=InMemorySyncStateStore(),
        )
        job()

        # The accepted proposal became a tagged agent block + a mirror row...
        assert [draft.summary for draft in fake_backend.created_drafts] == [
            "Write weekly report"
        ]
        assert fake_backend.created_drafts[0].agent_task_id == task.id
        session.expire_all()
        mirror = session.scalars(select(CalendarEventMirror)).one()
        assert mirror.is_agent_created
        assert mirror.agent_task_id == task.id
        # ...and advanced to pushed; the unconfirmed one is untouched.
        statuses = {
            proposal.status
            for proposal in session.scalars(select(ScheduleProposal)).all()
        }
        assert statuses == {ProposalStatus.PUSHED, ProposalStatus.PROPOSED}

    def test_read_backend_never_pushes(
        self, settings, session_factory, session, fake_backend
    ) -> None:
        task = Task(title="Read-only backend task")
        session.add(task)
        session.flush()
        session.add(
            ScheduleProposal(
                task_id=task.id,
                proposed_start=utc(2026, 7, 10, 9, 0),
                proposed_end=utc(2026, 7, 10, 10, 0),
                status=ProposalStatus.ACCEPTED,
            )
        )
        session.commit()

        job = build_calendar_job(
            settings,
            fake_backend.source,
            is_write_backend=False,
            backend_factory=lambda: fake_backend,
            session_factory=session_factory,
            state_store=InMemorySyncStateStore(),
        )
        job()

        assert fake_backend.created_drafts == []
        session.expire_all()
        proposal = session.scalars(select(ScheduleProposal)).one()
        assert proposal.status is ProposalStatus.ACCEPTED

    def test_confirmed_planned_sleep_is_written_with_reconciliation_identity(
        self, session, fake_backend
    ) -> None:
        task = Task(title="Night rest")
        session.add(task)
        session.flush()
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=utc(2026, 7, 10, 23, 0),
            proposed_end=utc(2026, 7, 11, 7, 0),
            status=ProposalStatus.ACCEPTED,
            healthmes_kind=HealthmesEventKind.PLANNED_SLEEP.value,
        )
        session.add(proposal)
        session.commit()
        service = CalendarMirrorService(
            session, [fake_backend], InMemorySyncStateStore()
        )

        assert push_accepted_proposals(service, session, fake_backend.source) == 1

        [draft] = fake_backend.created_drafts
        assert draft.identity is not None
        assert draft.identity.kind is HealthmesEventKind.PLANNED_SLEEP
        assert draft.identity.source == "planner"
        assert draft.identity.source_key == f"proposal:{proposal.id}"
        mirror = session.scalars(select(CalendarEventMirror)).one()
        assert mirror.healthmes_kind == HealthmesEventKind.PLANNED_SLEEP.value

    def test_job_contains_backend_failures(self, settings, session_factory) -> None:
        def exploding_factory():
            raise RuntimeError("credentials missing")

        job = build_calendar_job(
            settings,
            CalendarSource.GOOGLE,
            is_write_backend=True,
            backend_factory=exploding_factory,
            session_factory=session_factory,
            state_store=InMemorySyncStateStore(),
        )
        job()  # must not raise — next interval retries

    def test_failed_push_leaves_proposal_accepted_for_retry(
        self, settings, session_factory, session, fake_backend, monkeypatch
    ) -> None:
        task = Task(title="Flaky push")
        session.add(task)
        session.flush()
        session.add(
            ScheduleProposal(
                task_id=task.id,
                proposed_start=utc(2026, 7, 10, 9, 0),
                proposed_end=utc(2026, 7, 10, 10, 0),
                status=ProposalStatus.ACCEPTED,
            )
        )
        session.commit()

        def failing_create(draft):
            raise RuntimeError("backend down")

        monkeypatch.setattr(fake_backend, "create_event", failing_create)
        service = CalendarMirrorService(session, [fake_backend], InMemorySyncStateStore())

        pushed = push_accepted_proposals(service, session, fake_backend.source)

        assert pushed == 0
        session.expire_all()
        proposal = session.scalars(select(ScheduleProposal)).one()
        assert proposal.status is ProposalStatus.ACCEPTED

    def test_sleep_change_invalidates_accepted_proposal_before_push(
        self,
        session,
        fake_backend,
    ) -> None:
        task = Task(title="Too early after corrected sleep")
        session.add(task)
        session.flush()
        session.add_all(
            [
                ScheduleProposal(
                    task_id=task.id,
                    proposed_start=utc(2026, 7, 10, 6),
                    proposed_end=utc(2026, 7, 10, 7),
                    status=ProposalStatus.ACCEPTED,
                ),
                CalendarEventMirror(
                    external_id="actual-sleep",
                    calendar_source=CalendarSource.GOOGLE,
                    summary="수면 (실제)",
                    start_at=utc(2026, 7, 9, 23),
                    end_at=utc(2026, 7, 10, 7, 30),
                    is_agent_created=True,
                    healthmes_kind=HealthmesEventKind.ACTUAL_SLEEP.value,
                    sleep_local_date=utc(2026, 7, 10).date(),
                ),
            ]
        )
        session.commit()
        service = CalendarMirrorService(
            session,
            [fake_backend],
            InMemorySyncStateStore(),
        )

        pushed = push_accepted_proposals(
            service,
            session,
            fake_backend.source,
        )

        assert pushed == 0
        assert fake_backend.created_drafts == []
        proposal = session.scalars(select(ScheduleProposal)).one()
        assert proposal.status is ProposalStatus.INVALIDATED

    def test_invalidation_never_deletes_another_proposals_pushed_block(
        self,
        session,
        fake_backend,
    ) -> None:
        task = Task(title="Shared task")
        session.add(task)
        session.flush()
        start = utc(2026, 7, 10, 6)
        end = utc(2026, 7, 10, 7)
        pushed_proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=start,
            proposed_end=end,
            status=ProposalStatus.PUSHED,
        )
        accepted_proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=start,
            proposed_end=end,
            status=ProposalStatus.ACCEPTED,
        )
        session.add_all([pushed_proposal, accepted_proposal])
        session.commit()
        service = CalendarMirrorService(
            session,
            [fake_backend],
            InMemorySyncStateStore(),
        )
        pushed_row = service.create_agent_event(
            fake_backend.source,
            EventDraft(
                summary=task.title,
                start_at=start,
                end_at=end,
                agent_task_id=task.id,
                identity=_proposal_identity(pushed_proposal),
            ),
        )
        session.add(
            CalendarEventMirror(
                external_id="actual-sleep",
                calendar_source=CalendarSource.GOOGLE,
                summary="수면 (실제)",
                start_at=utc(2026, 7, 9, 23),
                end_at=utc(2026, 7, 10, 7, 30),
                is_agent_created=True,
                healthmes_kind=HealthmesEventKind.ACTUAL_SLEEP.value,
                sleep_local_date=utc(2026, 7, 10).date(),
            )
        )
        session.commit()

        assert push_accepted_proposals(
            service,
            session,
            fake_backend.source,
        ) == 0

        assert session.get(ScheduleProposal, accepted_proposal.id).status is (
            ProposalStatus.INVALIDATED
        )
        assert session.get(ScheduleProposal, pushed_proposal.id).status is (
            ProposalStatus.PUSHED
        )
        assert fake_backend.delete_calls == []
        assert pushed_row.external_id in fake_backend.events

    def test_post_create_sleep_recheck_conditionally_rolls_back_exact_block(
        self,
        session,
        fake_backend,
        monkeypatch,
    ) -> None:
        task = Task(title="Concurrent wake correction")
        session.add(task)
        session.flush()
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=utc(2026, 7, 10, 8),
            proposed_end=utc(2026, 7, 10, 9),
            status=ProposalStatus.ACCEPTED,
        )
        session.add(proposal)
        session.commit()
        service = CalendarMirrorService(
            session,
            [fake_backend],
            InMemorySyncStateStore(),
        )
        checks = iter((None, "concurrent actual-sleep overlap"))
        monkeypatch.setattr(
            "healthmes.calendars.jobs.actual_sleep_violation",
            lambda *_args, **_kwargs: next(checks),
        )

        assert push_accepted_proposals(
            service,
            session,
            fake_backend.source,
        ) == 0

        assert session.get(ScheduleProposal, proposal.id).status is (
            ProposalStatus.INVALIDATED
        )
        assert len(fake_backend.created_drafts) == 1
        assert len(fake_backend.delete_calls) == 1
        assert fake_backend.delete_expected_kinds == [
            HealthmesEventKind.SCHEDULE_BLOCK
        ]
        assert session.scalars(select(CalendarEventMirror)).all() == []

    def test_push_waits_for_write_lock_without_open_session_transaction(
        self,
        session,
        fake_backend,
        monkeypatch,
    ) -> None:
        task = Task(title="Connection-safe push")
        session.add(task)
        session.flush()
        session.add(
            ScheduleProposal(
                task_id=task.id,
                proposed_start=utc(2026, 7, 10, 9),
                proposed_end=utc(2026, 7, 10, 10),
                status=ProposalStatus.ACCEPTED,
            )
        )
        session.commit()
        lock_entries: list[bool] = []

        @contextmanager
        def assert_connection_free(waiting_session, sources):
            lock_entries.append(waiting_session.in_transaction())
            assert tuple(sources) == (CalendarSource.GOOGLE,)
            yield

        monkeypatch.setattr(
            "healthmes.calendars.jobs.calendar_write_locks",
            assert_connection_free,
        )
        service = CalendarMirrorService(
            session,
            [fake_backend],
            InMemorySyncStateStore(),
        )

        assert push_accepted_proposals(
            service,
            session,
            fake_backend.source,
        ) == 1
        assert lock_entries == [False]

    def test_crash_after_remote_create_does_not_duplicate_event(
        self, session, fake_backend
    ) -> None:
        # F8: a prior poll created the remote block + mirror row but crashed
        # before flipping the proposal to pushed. The retry must reuse the
        # existing block, not create a second remote event.
        task = Task(title="Write report")
        session.add(task)
        session.flush()
        session.add(
            ScheduleProposal(
                task_id=task.id,
                proposed_start=utc(2026, 7, 10, 9, 0),
                proposed_end=utc(2026, 7, 10, 11, 0),
                status=ProposalStatus.ACCEPTED,
            )
        )
        session.commit()

        service = CalendarMirrorService(session, [fake_backend], InMemorySyncStateStore())
        # Simulate the interrupted prior poll: the block already exists remotely
        # and in the mirror, but the proposal is still ACCEPTED.
        service.create_agent_event(
            fake_backend.source,
            EventDraft(
                summary="Write report",
                start_at=utc(2026, 7, 10, 9, 0),
                end_at=utc(2026, 7, 10, 11, 0),
                agent_task_id=task.id,
                identity=_proposal_identity(
                    session.scalars(select(ScheduleProposal)).one()
                ),
            ),
        )
        assert len(fake_backend.created_drafts) == 1

        pushed = push_accepted_proposals(service, session, fake_backend.source)

        assert pushed == 1
        assert len(fake_backend.created_drafts) == 1  # NO second remote create
        session.expire_all()
        assert len(session.scalars(select(CalendarEventMirror)).all()) == 1
        assert session.scalars(select(ScheduleProposal)).one().status is (
            ProposalStatus.PUSHED
        )

    def test_crash_recovery_refuses_remote_block_with_changed_identity(
        self,
        session,
        fake_backend,
    ) -> None:
        task = Task(title="Protected block")
        session.add(task)
        session.flush()
        proposed_start = utc(2026, 7, 10, 9)
        proposed_end = utc(2026, 7, 10, 10)
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=proposed_start,
            proposed_end=proposed_end,
            status=ProposalStatus.ACCEPTED,
        )
        session.add(proposal)
        session.commit()
        service = CalendarMirrorService(
            session,
            [fake_backend],
            InMemorySyncStateStore(),
        )
        created = service.create_agent_event(
            fake_backend.source,
            EventDraft(
                summary=task.title,
                start_at=proposed_start,
                end_at=proposed_end,
                agent_task_id=task.id,
                identity=_proposal_identity(proposal),
            ),
        )
        fake_backend.events[created.external_id] = replace(
            fake_backend.events[created.external_id],
            identity=CalendarEventIdentity(
                kind=HealthmesEventKind.SCHEDULE_BLOCK,
                source="planner",
                source_key="proposal:another",
            ),
        )

        assert push_accepted_proposals(
            service,
            session,
            fake_backend.source,
        ) == 0

        assert session.get(ScheduleProposal, proposal.id).status is (
            ProposalStatus.ACCEPTED
        )
        assert len(fake_backend.created_drafts) == 1

    def test_sleep_change_after_remote_create_crash_removes_block_and_invalidates(
        self,
        session,
        fake_backend,
    ) -> None:
        task = Task(title="Block invalidated after corrected sleep")
        session.add(task)
        session.flush()
        proposed_start = utc(2026, 7, 10, 6)
        proposed_end = utc(2026, 7, 10, 7)
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=proposed_start,
            proposed_end=proposed_end,
            status=ProposalStatus.ACCEPTED,
        )
        session.add(proposal)
        session.commit()
        service = CalendarMirrorService(
            session,
            [fake_backend],
            InMemorySyncStateStore(),
        )
        created = service.create_agent_event(
            fake_backend.source,
            EventDraft(
                summary=task.title,
                start_at=proposed_start,
                end_at=proposed_end,
                agent_task_id=task.id,
                identity=_proposal_identity(proposal),
            ),
        )
        session.add(
            CalendarEventMirror(
                external_id="actual-sleep",
                calendar_source=CalendarSource.GOOGLE,
                summary="수면 (실제)",
                start_at=utc(2026, 7, 9, 23),
                end_at=utc(2026, 7, 10, 7, 30),
                is_agent_created=True,
                healthmes_kind=HealthmesEventKind.ACTUAL_SLEEP.value,
                sleep_local_date=utc(2026, 7, 10).date(),
            )
        )
        session.commit()

        pushed = push_accepted_proposals(service, session, fake_backend.source)

        assert pushed == 0
        assert fake_backend.delete_calls == [created.external_id]
        assert created.external_id not in fake_backend.events
        assert session.get(ScheduleProposal, proposal.id).status is (
            ProposalStatus.INVALIDATED
        )
        assert {
            row.external_id
            for row in session.scalars(select(CalendarEventMirror)).all()
        } == {"actual-sleep"}

    def test_failed_crash_recovery_delete_leaves_proposal_retryable(
        self,
        session,
        fake_backend,
        monkeypatch,
    ) -> None:
        task = Task(title="Retry invalidated block cleanup")
        session.add(task)
        session.flush()
        proposed_start = utc(2026, 7, 10, 6)
        proposed_end = utc(2026, 7, 10, 7)
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=proposed_start,
            proposed_end=proposed_end,
            status=ProposalStatus.ACCEPTED,
        )
        session.add(proposal)
        session.commit()
        service = CalendarMirrorService(
            session,
            [fake_backend],
            InMemorySyncStateStore(),
        )
        created = service.create_agent_event(
            fake_backend.source,
            EventDraft(
                summary=task.title,
                start_at=proposed_start,
                end_at=proposed_end,
                agent_task_id=task.id,
                identity=_proposal_identity(proposal),
            ),
        )
        session.add(
            CalendarEventMirror(
                external_id="actual-sleep",
                calendar_source=CalendarSource.GOOGLE,
                summary="수면 (실제)",
                start_at=utc(2026, 7, 9, 23),
                end_at=utc(2026, 7, 10, 7, 30),
                is_agent_created=True,
                healthmes_kind=HealthmesEventKind.ACTUAL_SLEEP.value,
                sleep_local_date=utc(2026, 7, 10).date(),
            )
        )
        session.commit()

        def conflict_delete(*args, **kwargs) -> None:
            raise CalendarConflictError("remote changed")

        monkeypatch.setattr(fake_backend, "delete_event", conflict_delete)

        assert push_accepted_proposals(service, session, fake_backend.source) == 0
        assert session.get(ScheduleProposal, proposal.id).status is (
            ProposalStatus.ACCEPTED
        )
        assert session.scalar(
            select(CalendarEventMirror).where(
                CalendarEventMirror.external_id == created.external_id
            )
        ) is not None

    def test_remote_delete_local_commit_crash_recovers_and_invalidates(
        self,
        session_factory,
        fake_backend,
        monkeypatch,
    ) -> None:
        with session_factory() as setup_session:
            task = Task(title="Recover invalidated block cleanup")
            setup_session.add(task)
            setup_session.flush()
            proposed_start = utc(2026, 7, 10, 6)
            proposed_end = utc(2026, 7, 10, 7)
            proposal = ScheduleProposal(
                task_id=task.id,
                proposed_start=proposed_start,
                proposed_end=proposed_end,
                status=ProposalStatus.ACCEPTED,
            )
            setup_session.add(proposal)
            setup_session.commit()
            service = CalendarMirrorService(
                setup_session,
                [fake_backend],
                InMemorySyncStateStore(),
            )
            created = service.create_agent_event(
                fake_backend.source,
                EventDraft(
                    summary=task.title,
                    start_at=proposed_start,
                    end_at=proposed_end,
                    agent_task_id=task.id,
                    identity=_proposal_identity(proposal),
                ),
            )
            setup_session.add(
                CalendarEventMirror(
                    external_id="actual-sleep",
                    calendar_source=CalendarSource.GOOGLE,
                    summary="수면 (실제)",
                    start_at=utc(2026, 7, 9, 23),
                    end_at=utc(2026, 7, 10, 7, 30),
                    is_agent_created=True,
                    healthmes_kind=HealthmesEventKind.ACTUAL_SLEEP.value,
                    sleep_local_date=utc(2026, 7, 10).date(),
                )
            )
            setup_session.commit()
            proposal_id = proposal.id
            created_external_id = created.external_id

        original_delete = fake_backend.delete_event

        def strict_delete(
            external_id,
            *,
            expected_kind=None,
            expected_etag=None,
        ) -> None:
            if external_id not in fake_backend.events:
                fake_backend.delete_calls.append(external_id)
                raise EventNotFoundError(external_id)
            original_delete(
                external_id,
                expected_kind=expected_kind,
                expected_etag=expected_etag,
            )

        monkeypatch.setattr(fake_backend, "delete_event", strict_delete)
        with session_factory() as failing_session:
            service = CalendarMirrorService(
                failing_session,
                [fake_backend],
                InMemorySyncStateStore(),
            )
            real_commit = failing_session.commit

            def fail_after_remote_delete() -> None:
                if created_external_id not in fake_backend.events:
                    raise RuntimeError("simulated local delete commit failure")
                real_commit()

            monkeypatch.setattr(failing_session, "commit", fail_after_remote_delete)
            assert (
                push_accepted_proposals(
                    service,
                    failing_session,
                    fake_backend.source,
                )
                == 0
            )
            failing_session.rollback()

        with session_factory() as retry_session:
            service = CalendarMirrorService(
                retry_session,
                [fake_backend],
                InMemorySyncStateStore(),
            )
            assert (
                push_accepted_proposals(
                    service,
                    retry_session,
                    fake_backend.source,
                )
                == 0
            )
            assert retry_session.get(ScheduleProposal, proposal_id).status is (
                ProposalStatus.INVALIDATED
            )
            assert retry_session.scalar(
                select(CalendarEventMirror).where(
                    CalendarEventMirror.external_id == created_external_id
                )
            ) is None
        assert fake_backend.delete_calls == [created_external_id]


@pytest.mark.parametrize("source", [CalendarSource.GOOGLE, CalendarSource.CALDAV])
def test_job_ids_are_per_source(source) -> None:
    assert calendar_job_id(source) == f"healthmes-calendar-{source.value}"
