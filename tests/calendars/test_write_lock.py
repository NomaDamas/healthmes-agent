from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from errno import EAGAIN

import pytest
from sqlalchemy.orm import sessionmaker

import healthmes.calendars.sleep_source_lock as sleep_source_lock_module
import healthmes.calendars.write_lock as write_lock_module
from healthmes.activity.locking import activate_runtime_extended_write_fence
from healthmes.calendars.sleep_source_lock import (
    lock_sleep_source_key,
    unlock_sleep_source_key,
)
from healthmes.calendars.write_lock import (
    CalendarWriteLockOrderError,
    calendar_write_lock,
    calendar_write_locks,
    ordered_calendar_write_sources,
)
from healthmes.store import CalendarSource, create_db_engine


def test_sqlite_calendar_write_lock_serializes_process_writers(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'calendar-lock.db'}")
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    first_entered = threading.Event()
    release_first = threading.Event()
    timeline: list[str] = []

    def first_writer() -> None:
        with factory() as session, calendar_write_lock(
            session,
            CalendarSource.GOOGLE,
        ):
            timeline.append("first-enter")
            first_entered.set()
            assert release_first.wait(timeout=5)
            timeline.append("first-exit")

    def second_writer() -> None:
        assert first_entered.wait(timeout=5)
        with factory() as session, calendar_write_lock(
            session,
            CalendarSource.GOOGLE,
        ):
            timeline.append("second-enter")

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(first_writer)
            second = pool.submit(second_writer)
            assert first_entered.wait(timeout=5)
            assert timeline == ["first-enter"]
            release_first.set()
            first.result(timeout=5)
            second.result(timeout=5)
        assert timeline == ["first-enter", "first-exit", "second-enter"]
    finally:
        engine.dispose()


def test_sqlite_calendar_write_lock_process_wait_has_timeout(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'process-timeout.db'}")
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    first_entered = threading.Event()
    release_first = threading.Event()

    def holder() -> None:
        with factory() as session, calendar_write_lock(
            session,
            CalendarSource.GOOGLE,
        ):
            first_entered.set()
            assert release_first.wait(timeout=5)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(holder)
            assert first_entered.wait(timeout=5)
            started = time.monotonic()
            try:
                with factory() as session, pytest.raises(
                    TimeoutError,
                    match="process calendar write lock",
                ):
                    with calendar_write_lock(
                        session,
                        CalendarSource.GOOGLE,
                        timeout_seconds=0.05,
                    ):
                        pytest.fail("contended process lock must not be entered")
            finally:
                release_first.set()
            future.result(timeout=5)
            assert time.monotonic() - started < 1
    finally:
        release_first.set()
        engine.dispose()


def test_reverse_provider_requests_share_one_canonical_order(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'multi-provider.db'}")
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    start = threading.Barrier(2, timeout=5)
    state_guard = threading.Lock()
    active = 0
    maximum_active = 0

    def writer(sources: tuple[CalendarSource, ...]) -> None:
        nonlocal active, maximum_active
        start.wait()
        with factory() as session, calendar_write_locks(
            session,
            sources,
            timeout_seconds=1,
        ):
            with state_guard:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                time.sleep(0.05)
            finally:
                with state_guard:
                    active -= 1

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                writer,
                (CalendarSource.GOOGLE, CalendarSource.CALDAV),
            )
            second = pool.submit(
                writer,
                (CalendarSource.CALDAV, CalendarSource.GOOGLE),
            )
            first.result(timeout=5)
            second.result(timeout=5)
        assert maximum_active == 1
        assert ordered_calendar_write_sources(
            (CalendarSource.CALDAV, CalendarSource.GOOGLE)
        ) == (CalendarSource.GOOGLE, CalendarSource.CALDAV)
    finally:
        engine.dispose()


def test_nested_reverse_provider_order_fails_before_waiting(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'nested-order.db'}")
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    try:
        with factory() as session, calendar_write_lock(
            session,
            CalendarSource.CALDAV,
        ):
            with pytest.raises(
                CalendarWriteLockOrderError,
                match="canonical order",
            ):
                with calendar_write_locks(
                    session,
                    (CalendarSource.GOOGLE, CalendarSource.CALDAV),
                    timeout_seconds=0.05,
                ):
                    pytest.fail("inverse nested order must not be entered")
    finally:
        engine.dispose()


def test_nested_single_provider_reverse_order_fails_before_waiting(
    tmp_path,
) -> None:
    engine = create_db_engine(
        f"sqlite:///{tmp_path / 'nested-single-order.db'}"
    )
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    try:
        with factory() as session, calendar_write_lock(
            session,
            CalendarSource.CALDAV,
        ):
            with pytest.raises(
                CalendarWriteLockOrderError,
                match="canonical order",
            ):
                with calendar_write_lock(
                    session,
                    CalendarSource.GOOGLE,
                    timeout_seconds=0.05,
                ):
                    pytest.fail("inverse nested order must not be entered")
    finally:
        engine.dispose()


def test_nested_single_provider_canonical_order_is_allowed(tmp_path) -> None:
    engine = create_db_engine(
        f"sqlite:///{tmp_path / 'nested-single-canonical.db'}"
    )
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    try:
        with factory() as session, calendar_write_lock(
            session,
            CalendarSource.GOOGLE,
        ):
            with calendar_write_lock(
                session,
                CalendarSource.CALDAV,
                timeout_seconds=0.05,
            ):
                pass
    finally:
        engine.dispose()


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock-specific test")
def test_sqlite_file_lock_wait_has_timeout(tmp_path, monkeypatch) -> None:
    handle = (tmp_path / "calendar.lock").open("a+b")

    def always_contended(_descriptor, _operation) -> None:
        raise OSError(EAGAIN, "lock busy")

    monkeypatch.setattr(write_lock_module.fcntl, "flock", always_contended)
    try:
        with pytest.raises(
            TimeoutError,
            match="SQLite file calendar write lock",
        ):
            write_lock_module._lock_file(
                handle,
                deadline=time.monotonic() + 0.02,
                key="sqlite:test:google",
            )
    finally:
        handle.close()


def test_postgres_advisory_lock_wait_has_timeout() -> None:
    candidates = []

    class FakeUrl:
        @staticmethod
        def render_as_string(*, hide_password: bool) -> str:
            assert hide_password is True
            return "postgresql://healthmes@localhost/test"

    class FakeDialect:
        name = "postgresql"

    class FakeConnection:
        dialect = FakeDialect()

        def __init__(self) -> None:
            self.closed = False

        def scalar(self, _statement, _parameters):
            return False

        def close(self) -> None:
            self.closed = True

    class FakeEngine:
        dialect = FakeDialect()
        url = FakeUrl()

        @staticmethod
        def connect():
            candidate = FakeConnection()
            candidates.append(candidate)
            return candidate

    class FakeSession:
        @staticmethod
        def get_bind():
            return FakeEngine()

    with pytest.raises(
        TimeoutError,
        match="PostgreSQL advisory calendar write lock",
    ):
        with calendar_write_lock(
            FakeSession(),  # type: ignore[arg-type]
            CalendarSource.GOOGLE,
            timeout_seconds=0.02,
        ):
            pytest.fail("contended advisory lock must not be entered")

    assert candidates
    assert all(candidate.closed for candidate in candidates)


def test_postgres_pool_checkout_is_bounded_by_calendar_deadline() -> None:
    class FakeUrl:
        @staticmethod
        def render_as_string(*, hide_password: bool) -> str:
            assert hide_password is True
            return "postgresql://healthmes@localhost/test"

    class FakeDialect:
        name = "postgresql"

    class FakeConnection:
        dialect = FakeDialect()

        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    created: list[FakeConnection] = []

    class SlowEngine:
        dialect = FakeDialect()
        url = FakeUrl()

        @staticmethod
        def connect():
            time.sleep(0.2)
            candidate = FakeConnection()
            created.append(candidate)
            return candidate

    class FakeSession:
        @staticmethod
        def get_bind():
            return SlowEngine()

    started = time.monotonic()
    with pytest.raises(
        TimeoutError,
        match="PostgreSQL advisory calendar write lock",
    ):
        with calendar_write_lock(
            FakeSession(),  # type: ignore[arg-type]
            CalendarSource.GOOGLE,
            timeout_seconds=0.02,
        ):
            pytest.fail("slow pool checkout must not enter the lock")
    assert time.monotonic() - started < 0.15

    time.sleep(0.25)
    assert created and all(candidate.closed for candidate in created)


def test_postgres_control_connect_preserves_source_engine_contract() -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class SourceEngine:
        def __init__(self) -> None:
            self.connection = FakeConnection()
            self.connect_calls = 0
            self.creator_contract = object()

        def connect(self):
            self.connect_calls += 1
            return self.connection

    source = SourceEngine()
    connected = write_lock_module._connect_before_deadline(
        source,
        deadline=time.monotonic() + 2.2,
        key="healthmes:calendar-write:google",
    )

    assert source.connect_calls == 1
    assert connected.connection is source.connection
    assert source.creator_contract is not None

    connected.close()
    assert source.connection.closed is True


def test_timed_out_postgres_control_connect_closes_late_connection() -> None:
    finished = threading.Event()

    class FakeConnection:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class SlowSourceEngine:
        def __init__(self) -> None:
            self.connection = FakeConnection()

        def connect(self):
            time.sleep(0.08)
            finished.set()
            return self.connection

    source = SlowSourceEngine()
    with pytest.raises(
        TimeoutError,
        match="PostgreSQL advisory calendar write lock",
    ):
        write_lock_module._connect_before_deadline(
            source,
            deadline=time.monotonic() + 0.02,
            key="healthmes:calendar-write:google",
        )

    assert finished.wait(timeout=1)
    assert source.connection.closed is True


def test_calendar_advisory_acquire_failure_retires_control_connection(
    monkeypatch,
) -> None:
    class FakeUrl:
        @staticmethod
        def render_as_string(*, hide_password: bool) -> str:
            assert hide_password is True
            return "postgresql://healthmes@localhost/test"

    class FakeDialect:
        name = "postgresql"

    class FakeConnection:
        dialect = FakeDialect()

        def __init__(self) -> None:
            self.invalidated = False
            self.closed = False

        def invalidate(self, _cause=None) -> None:
            self.invalidated = True

        def close(self) -> None:
            self.closed = True

    connection = FakeConnection()

    class FakeEngine:
        dialect = FakeDialect()
        url = FakeUrl()

        @staticmethod
        def connect():
            return connection

    class FakeSession:
        @staticmethod
        def get_bind():
            return FakeEngine()

    monkeypatch.setattr(
        write_lock_module,
        "try_postgres_advisory_lock",
        lambda *_args: (_ for _ in ()).throw(
            RuntimeError("ambiguous acquire result")
        ),
    )

    with pytest.raises(RuntimeError, match="connection was retired"):
        with calendar_write_lock(
            FakeSession(),  # type: ignore[arg-type]
            CalendarSource.GOOGLE,
            timeout_seconds=0.2,
        ):
            pytest.fail("ambiguous advisory result must not enter the lock")

    assert connection.invalidated is True
    assert connection.closed is True


def test_calendar_checkout_timeouts_bound_live_workers() -> None:
    release = threading.Event()
    state_lock = threading.Lock()
    started = 0
    closed = 0

    class FakeConnection:
        def close(self) -> None:
            nonlocal closed
            with state_lock:
                closed += 1

    class FakeEngine:
        def connect(self):
            nonlocal started
            with state_lock:
                started += 1
            assert release.wait(timeout=5)
            return FakeConnection()

    engine = FakeEngine()
    key = "healthmes:calendar-write:google"
    try:
        for _ in range(12):
            with pytest.raises(
                TimeoutError,
                match="PostgreSQL advisory calendar write lock",
            ):
                write_lock_module._connect_before_deadline(
                    engine,
                    deadline=time.monotonic() + 0.01,
                    key=key,
                )

        assert (
            started
            == write_lock_module._POSTGRES_CONNECT_WORKER_LIMIT
        )
        live_workers = [
            thread
            for thread in threading.enumerate()
            if thread.name == "healthmes-calendar-control-connect"
        ]
        assert (
            len(live_workers)
            <= write_lock_module._POSTGRES_CONNECT_WORKER_LIMIT
        )
    finally:
        release.set()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with state_lock:
            if closed == started:
                break
        time.sleep(0.01)
    assert closed == started


def test_calendar_unlock_failure_retires_control_connection(
    monkeypatch,
) -> None:
    class BrokenPoolConnection:
        def invalidate(self, _cause) -> None:
            raise RuntimeError("injected pool invalidate failure")

    class FakeUrl:
        @staticmethod
        def render_as_string(*, hide_password: bool) -> str:
            assert hide_password is True
            return "postgresql://healthmes@localhost/test"

    class FakeDialect:
        name = "postgresql"

    class FakeEngine:
        dialect = FakeDialect()
        url = FakeUrl()

        @staticmethod
        def connect():
            return connection

        @staticmethod
        def dispose() -> None:
            pass

    class FakeConnection:
        dialect = FakeDialect()

        def __init__(self) -> None:
            self.engine = FakeEngine()
            self.closed = False
            self.invalidated = False
            self.detached = False
            self.pool_connection = BrokenPoolConnection()

        @property
        def connection(self):
            return self.pool_connection

        def invalidate(self, _cause=None) -> None:
            raise RuntimeError("injected connection invalidate failure")

        def detach(self) -> None:
            self.detached = True

        def close(self) -> None:
            self.closed = True

    connection = FakeConnection()

    class FakeSession:
        @staticmethod
        def get_bind():
            return connection.engine

    monkeypatch.setattr(
        write_lock_module,
        "try_postgres_advisory_lock",
        lambda *_args: True,
    )

    def fail_unlock(*_args):
        raise RuntimeError("injected advisory unlock failure")

    monkeypatch.setattr(
        write_lock_module,
        "release_postgres_advisory_lock",
        fail_unlock,
    )

    with pytest.raises(RuntimeError, match="connection was retired"):
        with calendar_write_lock(
            FakeSession(),  # type: ignore[arg-type]
            CalendarSource.GOOGLE,
            timeout_seconds=0.2,
        ):
            pass

    assert connection.detached is True
    assert connection.closed is True


@pytest.mark.parametrize("operation", ("acquire", "release"))
def test_sleep_source_advisory_failure_retires_control_connection(
    monkeypatch,
    operation,
) -> None:
    class FakeDialect:
        name = "postgresql"

    class FakeConnection:
        dialect = FakeDialect()

        def __init__(self) -> None:
            self.invalidated = False
            self.closed = False

        def invalidate(self, _cause=None) -> None:
            self.invalidated = True

        def close(self) -> None:
            self.closed = True

    connection = FakeConnection()

    class FakeEngine:
        dialect = FakeDialect()

        @staticmethod
        def connect():
            return connection

    class FakeSession:
        @staticmethod
        def get_bind():
            return FakeEngine()

    if operation == "acquire":
        monkeypatch.setattr(
            sleep_source_lock_module,
            "acquire_postgres_advisory_lock",
            lambda *_args: (_ for _ in ()).throw(
                RuntimeError("ambiguous acquire result")
            ),
        )
        with pytest.raises(RuntimeError, match="connection was retired"):
            lock_sleep_source_key(
                FakeSession(),  # type: ignore[arg-type]
                CalendarSource.GOOGLE,
                "sleep-source",
            )
    else:
        monkeypatch.setattr(
            sleep_source_lock_module,
            "release_postgres_advisory_lock",
            lambda *_args: (_ for _ in ()).throw(
                RuntimeError("ambiguous release result")
            ),
        )
        with pytest.raises(RuntimeError, match="connection was retired"):
            unlock_sleep_source_key(
                connection,  # type: ignore[arg-type]
                CalendarSource.GOOGLE,
                "sleep-source",
            )

    assert connection.invalidated is True
    assert connection.closed is True


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason=(
        "requires a disposable PostgreSQL URL in "
        "HEALTHMES_TEST_POSTGRES_URL"
    ),
)
def test_postgres_internal_calendar_locks_survive_runtime_fence() -> None:
    engine = create_db_engine(os.environ["HEALTHMES_TEST_POSTGRES_URL"])
    activate_runtime_extended_write_fence(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    try:
        with factory() as session, calendar_write_lock(
            session,
            CalendarSource.GOOGLE,
            timeout_seconds=1,
        ):
            pass

        with factory() as session:
            connection = lock_sleep_source_key(
                session,
                CalendarSource.GOOGLE,
                "runtime-fence-test",
            )
            assert connection is not None
            unlock_sleep_source_key(
                connection,
                CalendarSource.GOOGLE,
                "runtime-fence-test",
            )
    finally:
        engine.dispose()
