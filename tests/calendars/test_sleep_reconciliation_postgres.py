from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from healthmes.calendars.base import (
    EventDraft,
    EventNotFoundError,
    ExternalEvent,
    HealthmesEventKind,
    SyncState,
)
from healthmes.calendars.sleep_observation import (
    ActualSleepObservation,
    actual_sleep_source_key,
)
from healthmes.calendars.sleep_reconciliation import (
    SleepCalendarAction,
    SleepCalendarReconciler,
)
from healthmes.store import Base, CalendarEventMirror, CalendarSource, create_db_engine


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
            self.event = ExternalEvent(
                external_id="sleep-concurrent",
                summary=draft.summary,
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


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_postgres_source_key_lock_allows_one_concurrent_create() -> None:
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
def test_postgres_source_key_lock_is_pinned_away_from_session_pool_commits() -> None:
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
def test_postgres_source_key_lock_is_released_after_provider_failure() -> None:
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
    advisory_key = f"{CalendarSource.GOOGLE.value}:{observation.source_key}"

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
