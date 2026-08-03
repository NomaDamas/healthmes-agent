from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from healthmes.calendars.base import (
    CalendarEventIdentity,
    EventDraft,
    EventNotFoundError,
    ExternalEvent,
    HealthmesEventKind,
    SyncState,
    calendar_identity_external_id,
)
from healthmes.calendars.jobs import push_accepted_proposals
from healthmes.calendars.sleep_mirror import mark_sleep_update_pending
from healthmes.calendars.sleep_observation import ActualSleepObservation, actual_sleep_source_key
from healthmes.calendars.sleep_reconciliation import (
    SleepCalendarAction,
    SleepCalendarReconciler,
    observation_fingerprint,
)
from healthmes.calendars.state import InMemorySyncStateStore
from healthmes.calendars.sync import CalendarMirrorService, SyncDiff
from healthmes.calendars.write_lock import calendar_write_lock_key
from healthmes.store import (
    Base,
    CalendarEventMirror,
    CalendarSource,
    ProposalStatus,
    ScheduleProposal,
    Task,
    create_db_engine,
)


class ConcurrentCalendarBackend:
    source = CalendarSource.GOOGLE

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self.create_count = 0
        self.event: ExternalEvent | None = None

    def list_changes(self, sync_state: SyncState | None) -> tuple[list[ExternalEvent], SyncState]:
        return [], dict(sync_state or {})

    def create_event(self, draft: EventDraft) -> ExternalEvent:
        with self._guard:
            self.create_count += 1
            assert draft.identity is not None
            self.event = ExternalEvent(
                external_id=calendar_identity_external_id(
                    self.source,
                    draft.identity,
                ),
                summary=draft.summary,
                description=draft.description,
                start_at=draft.start_at,
                end_at=draft.end_at,
                is_agent_created=True,
                identity=draft.identity,
                etag='"created"',
            )
            return self.event

    def read_event(self, external_id: str) -> ExternalEvent:
        with self._guard:
            if self.event is None:
                raise EventNotFoundError(external_id)
            return self.event

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
        raise AssertionError("identical concurrent replay must not update")

    def delete_event(
        self,
        external_id: str,
        *,
        expected_kind: HealthmesEventKind | None = None,
        expected_etag: str | None = None,
    ) -> None:
        raise AssertionError("actual sleep upsert must not delete")


class OrderedPushSleepBackend:
    source = CalendarSource.GOOGLE

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self.push_create_started = threading.Event()
        self.release_push_create = threading.Event()
        self.events: dict[str, ExternalEvent] = {}
        self.timeline: list[str] = []
        self.delete_requests: list[
            tuple[str, HealthmesEventKind | None, str | None]
        ] = []

    def list_changes(
        self,
        sync_state: SyncState | None,
    ) -> tuple[list[ExternalEvent], SyncState]:
        return [], dict(sync_state or {})

    def create_event(self, draft: EventDraft) -> ExternalEvent:
        assert draft.identity is not None
        if draft.identity.kind is HealthmesEventKind.SCHEDULE_BLOCK:
            self.push_create_started.set()
            if not self.release_push_create.wait(timeout=5):
                raise RuntimeError("timed out waiting to release proposal push")
        event = ExternalEvent(
            external_id=calendar_identity_external_id(
                self.source,
                draft.identity,
            ),
            summary=draft.summary,
            description=draft.description,
            start_at=draft.start_at,
            end_at=draft.end_at,
            is_agent_created=True,
            agent_task_id=draft.agent_task_id,
            identity=draft.identity,
            etag=f'"{draft.identity.kind.value}"',
        )
        with self._guard:
            self.events[event.external_id] = event
            self.timeline.append(f"create:{draft.identity.kind.value}")
        return event

    def read_event(self, external_id: str) -> ExternalEvent:
        with self._guard:
            try:
                return self.events[external_id]
            except KeyError as exc:
                raise EventNotFoundError(external_id) from exc

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
        raise AssertionError("ordered push/sleep test must not update")

    def delete_event(
        self,
        external_id: str,
        *,
        expected_kind: HealthmesEventKind | None = None,
        expected_etag: str | None = None,
    ) -> None:
        with self._guard:
            try:
                event = self.events[external_id]
            except KeyError as exc:
                raise EventNotFoundError(external_id) from exc
            assert event.identity is not None
            assert expected_kind is event.identity.kind
            assert expected_etag == event.etag
            self.delete_requests.append(
                (external_id, expected_kind, expected_etag)
            )
            self.timeline.append(f"delete:{event.identity.kind.value}")
            del self.events[external_id]


class PendingRecoveryBackend(ConcurrentCalendarBackend):
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
        with self._guard:
            assert self.event is not None
            assert self.event.external_id == external_id
            assert self.event.etag == expected_etag
            assert start_at is not None
            assert end_at is not None
            self.event = replace(
                self.event,
                summary=summary,
                description=description,
                start_at=start_at,
                end_at=end_at,
                etag='"updated"',
            )
            return self.event


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_postgres_calendar_write_lock_allows_one_concurrent_create() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_test_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    backend = ConcurrentCalendarBackend()
    start_gate = threading.Barrier(2, timeout=5)
    observation = ActualSleepObservation(
        local_date=date(2026, 7, 26),
        provider="oura",
        source_key=actual_sleep_source_key(date(2026, 7, 26)),
        start_at=datetime(2026, 7, 25, 23, tzinfo=UTC),
        end_at=datetime(2026, 7, 26, 7, tzinfo=UTC),
        duration_minutes=420,
        time_in_bed_minutes=480,
    )

    def reconcile_once() -> SleepCalendarAction:
        with factory() as session:
            start_gate.wait()
            return SleepCalendarReconciler(session, backend).reconcile(observation).action

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            actions = [
                future.result(timeout=10)
                for future in {
                    pool.submit(reconcile_once),
                    pool.submit(reconcile_once),
                }
            ]
        with factory() as session:
            row_count = session.scalar(sa.select(sa.func.count()).select_from(CalendarEventMirror))

        assert sorted(actions) == [SleepCalendarAction.CREATED, SleepCalendarAction.NOOP]
        assert backend.create_count == 1
        assert row_count == 1
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_postgres_late_pending_sync_cannot_roll_back_recovery() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_test_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    backend = PendingRecoveryBackend()
    observation = ActualSleepObservation(
        local_date=date(2026, 7, 26),
        provider="oura",
        source_key=actual_sleep_source_key(date(2026, 7, 26)),
        start_at=datetime(2026, 7, 25, 23, tzinfo=UTC),
        end_at=datetime(2026, 7, 26, 7, tzinfo=UTC),
        duration_minutes=420,
        time_in_bed_minutes=480,
    )
    corrected = replace(
        observation,
        provider="garmin",
        start_at=datetime(2026, 7, 25, 22, 45, tzinfo=UTC),
        end_at=datetime(2026, 7, 26, 7, 45, tzinfo=UTC),
        duration_minutes=465,
        time_in_bed_minutes=525,
    )
    stale_session = None

    try:
        with factory() as session:
            SleepCalendarReconciler(session, backend).reconcile(observation)
            row = session.scalar(sa.select(CalendarEventMirror))
            assert row is not None
            mark_sleep_update_pending(
                session,
                row,
                corrected,
                observation_fingerprint(corrected),
                row.etag,
            )

        assert backend.event is not None
        stale_session = factory()
        stale_service = CalendarMirrorService(
            stale_session,
            [backend],
            InMemorySyncStateStore(),
        )
        stale_service._apply_upsert(
            CalendarSource.GOOGLE,
            replace(
                backend.event,
                etag='"stale-sync-observation"',
                organizer_self=True,
            ),
            SyncDiff(),
            bootstrap=False,
        )

        with factory() as recovery_session:
            result = SleepCalendarReconciler(
                recovery_session,
                backend,
            ).reconcile(corrected)
        assert result.action is SleepCalendarAction.UPDATED

        stale_session.commit()
        with factory() as verify_session:
            recovered = verify_session.scalar(sa.select(CalendarEventMirror))
            assert recovered is not None
            assert recovered.status is None
            assert recovered.etag == '"updated"'
            assert recovered.organizer_self is False
            assert recovered.sleep_provider == corrected.provider
            assert recovered.observation_fingerprint == observation_fingerprint(
                corrected
            )
    finally:
        if stale_session is not None:
            stale_session.close()
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_postgres_push_then_sleep_reconcile_preserves_write_order() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_test_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    backend = OrderedPushSleepBackend()
    observation = ActualSleepObservation(
        local_date=date(2026, 7, 26),
        provider="oura",
        source_key=actual_sleep_source_key(date(2026, 7, 26)),
        start_at=datetime(2026, 7, 25, 23, tzinfo=UTC),
        end_at=datetime(2026, 7, 26, 7, tzinfo=UTC),
        duration_minutes=420,
        time_in_bed_minutes=480,
    )
    with factory() as session:
        task = Task(title="Late focus")
        session.add(task)
        session.flush()
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=datetime(2026, 7, 25, 22, 30, tzinfo=UTC),
            proposed_end=datetime(2026, 7, 25, 23, 30, tzinfo=UTC),
            status=ProposalStatus.ACCEPTED,
        )
        session.add(proposal)
        session.commit()
        proposal_id = proposal.id
        proposal_identity = CalendarEventIdentity(
            kind=HealthmesEventKind.SCHEDULE_BLOCK,
            source="planner",
            source_key=f"proposal:{proposal_id}",
        )
        schedule_external_id = calendar_identity_external_id(
            CalendarSource.GOOGLE,
            proposal_identity,
        )

    reconcile_started = threading.Event()

    def push_once() -> int:
        with factory() as session:
            service = CalendarMirrorService(
                session,
                [backend],
                InMemorySyncStateStore(),
            )
            return push_accepted_proposals(
                service,
                session,
                CalendarSource.GOOGLE,
            )

    def reconcile_once():
        reconcile_started.set()
        with factory() as session:
            return SleepCalendarReconciler(session, backend).reconcile(
                observation
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            push_future = pool.submit(push_once)
            assert backend.push_create_started.wait(timeout=5)
            reconcile_future = pool.submit(reconcile_once)
            assert reconcile_started.wait(timeout=5)
            with pytest.raises(FuturesTimeoutError):
                reconcile_future.result(timeout=0.2)
            backend.release_push_create.set()

            assert push_future.result(timeout=10) == 1
            result = reconcile_future.result(timeout=10)

        assert result.action is SleepCalendarAction.CREATED
        assert result.invalidated_schedule_proposal_ids == (str(proposal_id),)
        assert backend.timeline == [
            "create:schedule_block",
            "delete:schedule_block",
            "create:actual_sleep",
        ]
        assert backend.delete_requests == [
            (
                schedule_external_id,
                HealthmesEventKind.SCHEDULE_BLOCK,
                '"schedule_block"',
            )
        ]
        with factory() as session:
            assert session.get(ScheduleProposal, proposal_id).status is (
                ProposalStatus.INVALIDATED
            )
            mirrors = list(session.scalars(sa.select(CalendarEventMirror)).all())
            assert [row.healthmes_kind for row in mirrors] == [
                HealthmesEventKind.ACTUAL_SLEEP.value
            ]
        assert [
            event.identity.kind
            for event in backend.events.values()
            if event.identity is not None
        ] == [HealthmesEventKind.ACTUAL_SLEEP]
    finally:
        backend.release_push_create.set()
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_postgres_lock_waiters_do_not_exhaust_the_session_pool() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_test_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
        pool_size=3,
        max_overflow=0,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    backend = ConcurrentCalendarBackend()
    start_gate = threading.Barrier(3, timeout=5)
    observation = ActualSleepObservation(
        local_date=date(2026, 7, 26),
        provider="oura",
        source_key=actual_sleep_source_key(date(2026, 7, 26)),
        start_at=datetime(2026, 7, 25, 23, tzinfo=UTC),
        end_at=datetime(2026, 7, 26, 7, tzinfo=UTC),
        duration_minutes=420,
        time_in_bed_minutes=480,
    )

    def reconcile_once() -> SleepCalendarAction:
        with factory() as session:
            start_gate.wait()
            return SleepCalendarReconciler(session, backend).reconcile(observation).action

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            actions = [
                future.result(timeout=10)
                for future in {
                    pool.submit(reconcile_once),
                    pool.submit(reconcile_once),
                    pool.submit(reconcile_once),
                }
            ]

        assert actions.count(SleepCalendarAction.CREATED) == 1
        assert actions.count(SleepCalendarAction.NOOP) == 2
        assert backend.create_count == 1
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_postgres_calendar_write_lock_is_pinned_away_from_session_pool_commits() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_test_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
        pool_size=3,
        max_overflow=0,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    class PoolBorrowingBackend(ConcurrentCalendarBackend):
        def __init__(self) -> None:
            super().__init__()
            self.borrowed_connection = None
            self.borrowed_advisory_locks: int | None = None

        def create_event(self, draft: EventDraft) -> ExternalEvent:
            self.borrowed_connection = engine.connect()
            self.borrowed_advisory_locks = self.borrowed_connection.scalar(
                sa.text(
                    "SELECT count(*) FROM pg_locks "
                    "WHERE pid = pg_backend_pid() AND locktype = 'advisory'"
                )
            )
            return super().create_event(draft)

    backend = PoolBorrowingBackend()
    observation = ActualSleepObservation(
        local_date=date(2026, 7, 26),
        provider="oura",
        source_key=actual_sleep_source_key(date(2026, 7, 26)),
        start_at=datetime(2026, 7, 25, 23, tzinfo=UTC),
        end_at=datetime(2026, 7, 26, 7, tzinfo=UTC),
        duration_minutes=420,
        time_in_bed_minutes=480,
    )

    try:
        with factory() as session:
            result = SleepCalendarReconciler(session, backend).reconcile(observation)

        assert result.action is SleepCalendarAction.CREATED
        assert backend.borrowed_advisory_locks == 0
    finally:
        if backend.borrowed_connection is not None:
            backend.borrowed_connection.execute(
                sa.text("SELECT pg_advisory_unlock_all()")
            )
            backend.borrowed_connection.close()
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_postgres_calendar_write_lock_is_released_after_provider_failure() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_test_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    class FailingBackend(ConcurrentCalendarBackend):
        def create_event(self, draft: EventDraft) -> ExternalEvent:
            raise RuntimeError("provider unavailable")

    observation = ActualSleepObservation(
        local_date=date(2026, 7, 26),
        provider="oura",
        source_key=actual_sleep_source_key(date(2026, 7, 26)),
        start_at=datetime(2026, 7, 25, 23, tzinfo=UTC),
        end_at=datetime(2026, 7, 26, 7, tzinfo=UTC),
        duration_minutes=420,
        time_in_bed_minutes=480,
    )
    advisory_key = calendar_write_lock_key(CalendarSource.GOOGLE)

    try:
        with factory() as session, pytest.raises(RuntimeError, match="unavailable"):
            SleepCalendarReconciler(session, FailingBackend()).reconcile(observation)

        with engine.connect() as connection:
            acquired = connection.scalar(
                sa.text(
                    "SELECT pg_try_advisory_lock("
                    "hashtextextended(:source_key, 0))"
                ),
                {"source_key": advisory_key},
            )
            assert acquired is True
            assert connection.scalar(
                sa.text(
                    "SELECT pg_advisory_unlock("
                    "hashtextextended(:source_key, 0))"
                ),
                {"source_key": advisory_key},
            ) is True
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()
