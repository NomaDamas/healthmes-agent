from __future__ import annotations

import multiprocessing
import threading
import time

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from healthmes.activity.locking import (
    activity_write_lock,
    lock_activity_write_plane,
)
from healthmes.store.session import create_db_engine


def _hold_activity_lock(
    database_url: str,
    acquired,
    release,
) -> None:
    engine = create_db_engine(database_url)
    session = Session(engine)
    try:
        lock_activity_write_plane(session)
        session.execute(text("SELECT 1"))
        acquired.set()
        if not release.wait(timeout=10):
            raise TimeoutError("timed out waiting to release activity lock")
        session.rollback()
    finally:
        session.close()
        engine.dispose()


def _acquire_activity_lock(
    database_url: str,
    acquired,
) -> None:
    engine = create_db_engine(database_url)
    session = Session(engine)
    try:
        lock_activity_write_plane(session)
        session.execute(text("SELECT 1"))
        acquired.set()
        session.rollback()
    finally:
        session.close()
        engine.dispose()


def test_file_sqlite_activity_lock_lasts_until_transaction_end(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'activity.db'}")
    first = Session(engine)
    second = Session(engine)
    acquired = threading.Event()

    lock_activity_write_plane(first)
    first.execute(text("SELECT 1"))

    def acquire_second() -> None:
        lock_activity_write_plane(second)
        second.execute(text("SELECT 1"))
        acquired.set()
        second.rollback()

    thread = threading.Thread(target=acquire_second, daemon=True)
    thread.start()
    time.sleep(0.1)
    assert not acquired.is_set()

    first.rollback()
    thread.join(timeout=2)

    assert acquired.is_set()
    assert not thread.is_alive()
    first.close()
    second.close()
    engine.dispose()


def test_process_activity_write_lock_timeout_is_bounded() -> None:
    acquired = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with activity_write_lock():
            acquired.set()
            assert release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    try:
        assert acquired.wait(timeout=5)
        started = time.monotonic()
        with pytest.raises(
            TimeoutError,
            match="process activity write lock",
        ):
            with activity_write_lock(timeout_seconds=0.1):
                pytest.fail("process write lock unexpectedly acquired")
        assert 0.05 <= time.monotonic() - started < 1
    finally:
        release.set()
        holder.join(timeout=5)

    assert not holder.is_alive()


def test_process_activity_write_lock_releases_when_cancelled_after_acquire(
) -> None:
    class ReplayCancelled(RuntimeError):
        pass

    checks = 0

    def cancel_after_acquire() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise ReplayCancelled

    with pytest.raises(ReplayCancelled):
        with activity_write_lock(
            timeout_seconds=1,
            cancellation_check=cancel_after_acquire,
            poll_seconds=0.01,
        ):
            pytest.fail("cancelled lock acquisition entered its body")

    with activity_write_lock(timeout_seconds=0.1):
        pass


def test_file_sqlite_activity_lock_timeout_is_bounded(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'bounded.db'}")
    first = Session(engine)
    second = Session(engine)
    try:
        lock_activity_write_plane(first)
        first.execute(text("SELECT 1"))

        started = time.monotonic()
        with pytest.raises(
            TimeoutError,
            match="SQLite activity file lock",
        ):
            lock_activity_write_plane(
                second,
                timeout_seconds=0.1,
                poll_seconds=0.01,
            )
        assert 0.05 <= time.monotonic() - started < 1
        assert "healthmes_activity_sqlite_file_lock" not in second.info

        first.rollback()
        lock_activity_write_plane(
            second,
            timeout_seconds=1,
            poll_seconds=0.01,
        )
        second.rollback()
    finally:
        first.close()
        second.close()
        engine.dispose()


def test_file_sqlite_activity_lock_serializes_independent_processes(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'activity.db'}"
    context = multiprocessing.get_context("spawn")
    first_acquired = context.Event()
    release_first = context.Event()
    second_acquired = context.Event()
    first = context.Process(
        target=_hold_activity_lock,
        args=(database_url, first_acquired, release_first),
    )
    second = context.Process(
        target=_acquire_activity_lock,
        args=(database_url, second_acquired),
    )

    first.start()
    try:
        assert first_acquired.wait(timeout=5)
        second.start()
        assert not second_acquired.wait(timeout=0.25)
        release_first.set()
        assert second_acquired.wait(timeout=5)
    finally:
        release_first.set()
        first.join(timeout=5)
        if first.is_alive():
            first.terminate()
            first.join(timeout=5)
        if second.pid is not None:
            second.join(timeout=5)
            if second.is_alive():
                second.terminate()
                second.join(timeout=5)

    assert first.exitcode == 0
    assert second.exitcode == 0


def test_file_sqlite_engine_enables_wal_and_busy_timeout(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'activity.db'}")
    with engine.connect() as connection:
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
        busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()

    assert str(journal_mode).casefold() == "wal"
    assert int(busy_timeout) == 30_000
    engine.dispose()
