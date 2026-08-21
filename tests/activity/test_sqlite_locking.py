from __future__ import annotations

import asyncio
import multiprocessing
import os
import threading
import time

import pytest
import sqlalchemy as sa
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

import healthmes.activity.locking as locking_module
from healthmes.activity.locking import (
    activate_runtime_extended_write_fence,
    activity_write_lock,
    anchored_sqlite_lock_parent,
    fenced_core_transaction,
    global_write_plane_guard,
    lock_activity_write_plane,
    payload_generation_guard,
    session_holds_write_plane,
    set_sqlite_query_only,
    sqlite_runtime_guard,
)
from healthmes.durable_files import DurabilityUnsupportedError
from healthmes.store.session import create_db_engine

_WRITE_FENCE_INFO_KEY = "healthmes_connection_write_fence"


class _WriteFenceBase(DeclarativeBase):
    pass


class _WriteFenceProbe(_WriteFenceBase):
    __tablename__ = "write_fence_probe"

    id: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[str]


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


def _hold_sqlite_runtime(
    database_url: str,
    acquired,
    release,
) -> None:
    with sqlite_runtime_guard(database_url):
        acquired.set()
        if not release.wait(timeout=10):
            raise TimeoutError("timed out waiting to release SQLite runtime")


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


def test_windows_lock_files_fail_closed_before_pathname_open(
    tmp_path,
    monkeypatch,
) -> None:
    lock_path = tmp_path / "healthmes.db.activity.lock"
    target = tmp_path / "outside.bin"
    target.write_bytes(b"must remain untouched")
    lock_path.symlink_to(target)
    monkeypatch.setattr(
        locking_module,
        "_SECURE_LOCK_FILES_SUPPORTED",
        False,
    )

    with pytest.raises(
        DurabilityUnsupportedError,
        match="descriptor-relative lock files are unavailable on Windows",
    ):
        locking_module._open_lock_handle(lock_path)

    assert lock_path.is_symlink()
    assert target.read_bytes() == b"must remain untouched"


@pytest.mark.asyncio
async def test_child_async_task_cannot_reenter_parent_activity_lease() -> None:
    async def contend() -> None:
        with pytest.raises(
            TimeoutError,
            match="process activity write lock",
        ):
            with activity_write_lock(timeout_seconds=0.02):
                pytest.fail("child task reused the parent task's lock lease")

    with activity_write_lock():
        await asyncio.create_task(contend())


@pytest.mark.asyncio
async def test_child_async_task_cannot_reenter_parent_global_guard(
    tmp_path,
) -> None:
    engine = create_db_engine(
        f"sqlite:///{tmp_path / 'child-global-guard.db'}"
    )

    async def contend() -> None:
        with pytest.raises(
            TimeoutError,
            match="process activity write lock",
        ):
            with global_write_plane_guard(engine, timeout_seconds=0.02):
                pytest.fail("child task reused the parent global guard")

    try:
        with global_write_plane_guard(engine):
            await asyncio.create_task(contend())
    finally:
        engine.dispose()


def test_same_owner_can_reenter_sqlite_global_guard(tmp_path) -> None:
    engine = create_db_engine(
        f"sqlite:///{tmp_path / 'same-owner-global-guard.db'}"
    )

    try:
        with global_write_plane_guard(engine):
            with global_write_plane_guard(
                engine,
                timeout_seconds=0.02,
            ):
                pass
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("guard", "process_lock_name"),
    (
        (global_write_plane_guard, "_ACTIVITY_WRITE_LOCK"),
        (payload_generation_guard, "_PAYLOAD_GENERATION_LOCK"),
    ),
)
def test_sqlite_multilayer_guards_share_one_monotonic_deadline(
    tmp_path,
    monkeypatch,
    guard,
    process_lock_name,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'single-deadline.db'}"
    clock = [100.0]
    process_deadlines: list[float | None] = []
    file_deadlines: list[float | None] = []
    process_lock = getattr(locking_module, process_lock_name)
    real_acquire = process_lock.acquire

    def delayed_process_acquire(
        lease,
        *,
        timeout_seconds,
        deadline=None,
    ):
        process_deadlines.append(deadline)
        acquired = real_acquire(
            lease,
            timeout_seconds=timeout_seconds,
            deadline=deadline,
        )
        clock[0] += 0.4
        return acquired

    def record_file_deadline(
        _handle,
        *,
        timeout_seconds,
        poll_seconds,
        _deadline=None,
    ) -> None:
        assert timeout_seconds is None
        assert poll_seconds > 0
        file_deadlines.append(_deadline)
        clock[0] += 0.4

    monkeypatch.setattr(locking_module, "steady_time", lambda: clock[0])
    monkeypatch.setattr(process_lock, "acquire", delayed_process_acquire)
    monkeypatch.setattr(locking_module, "_lock_file", record_file_deadline)
    monkeypatch.setattr(locking_module, "_unlock_file", lambda _handle: None)

    with guard(database_url, timeout_seconds=1.0):
        assert clock[0] == pytest.approx(100.8)

    assert process_deadlines == [101.0]
    assert file_deadlines == [101.0]


@pytest.mark.asyncio
async def test_child_async_task_cannot_inherit_postgres_guard_connection() -> None:
    identity = ("postgresql", "postgresql://healthmes.example/healthmes")
    connection = object()

    async def inspect_child_context() -> None:
        assert (
            locking_module._active_postgres_guard_connection(identity)
            is None
        )

    with locking_module._active_postgres_guard(identity, connection):
        assert (
            locking_module._active_postgres_guard_connection(identity)
            is connection
        )
        await asyncio.create_task(inspect_child_context())


@pytest.mark.skipif(
    os.name == "nt",
    reason="descriptor-relative SQLite lock anchoring is POSIX-only",
)
@pytest.mark.asyncio
async def test_child_async_task_cannot_inherit_sqlite_lock_parent(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'anchored-child.db'}"
    identity = locking_module._sqlite_lock_parent_identity(database_url)
    parent_descriptor = os.open(
        tmp_path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )

    async def inspect_child_context() -> None:
        assert (
            locking_module._active_sqlite_lock_parent_descriptor(identity)
            is None
        )

    try:
        with anchored_sqlite_lock_parent(database_url, parent_descriptor):
            active_descriptor = (
                locking_module._active_sqlite_lock_parent_descriptor(identity)
            )
            assert active_descriptor is not None
            assert active_descriptor != parent_descriptor
            await asyncio.create_task(inspect_child_context())
    finally:
        os.close(parent_descriptor)


def test_automatic_orm_fence_holds_process_lock_until_transaction_end(
    tmp_path,
) -> None:
    engine = create_db_engine(
        f"sqlite:///{tmp_path / 'automatic-process-lock.db'}"
    )
    _WriteFenceBase.metadata.create_all(engine)
    session = Session(engine)
    errors: list[BaseException] = []

    try:
        session.add(_WriteFenceProbe(value="held"))
        session.flush()

        def contend_for_process_lock() -> None:
            try:
                with activity_write_lock(timeout_seconds=0.1):
                    pytest.fail(
                        "process write lock unexpectedly acquired"
                    )
            except BaseException as exc:
                errors.append(exc)

        contender = threading.Thread(
            target=contend_for_process_lock,
            daemon=True,
        )
        contender.start()
        contender.join(timeout=2)

        assert not contender.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], TimeoutError)
        assert "process activity write lock" in str(errors[0])

        with pytest.raises(
            RuntimeError,
            match="cannot start inside an active Session write transaction",
        ):
            with global_write_plane_guard(engine):
                pytest.fail("nested global guard unexpectedly started")

        session.rollback()
        with activity_write_lock(timeout_seconds=1):
            pass
    finally:
        if session.in_transaction():
            session.rollback()
        session.close()
        engine.dispose()


def test_automatic_orm_fence_can_be_released_by_dependency_cleanup_thread(
    tmp_path,
) -> None:
    engine = create_db_engine(
        f"sqlite:///{tmp_path / 'cross-thread-cleanup.db'}"
    )
    _WriteFenceBase.metadata.create_all(engine)
    session = Session(engine)
    errors: list[BaseException] = []

    try:
        session.add(_WriteFenceProbe(value="cross-thread"))
        session.flush()

        def close_session() -> None:
            try:
                session.close()
            except BaseException as exc:
                errors.append(exc)

        cleanup = threading.Thread(target=close_session)
        cleanup.start()
        cleanup.join(timeout=2)

        assert not cleanup.is_alive()
        assert errors == []
        with activity_write_lock(timeout_seconds=0.1):
            pass
    finally:
        session.close()
        engine.dispose()


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
            match="SQLite file lock",
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


@pytest.mark.parametrize(
    "write_mode",
    ["orm_flush", "bulk_dml", "text_dml", "text_cte_dml"],
)
def test_unannotated_session_writes_automatically_share_the_global_fence(
    tmp_path,
    write_mode,
) -> None:
    database_url = f"sqlite:///{tmp_path / f'automatic-{write_mode}.db'}"
    engine = create_db_engine(database_url)
    _WriteFenceBase.metadata.create_all(engine)
    holder = Session(engine)
    writer_started = threading.Event()
    writer_finished = threading.Event()
    errors: list[BaseException] = []

    lock_activity_write_plane(holder)
    holder.execute(text("SELECT 1"))

    def write_without_explicit_lock() -> None:
        session = Session(engine)
        try:
            writer_started.set()
            if write_mode == "orm_flush":
                session.add(_WriteFenceProbe(value=write_mode))
            elif write_mode == "bulk_dml":
                session.execute(
                    insert(_WriteFenceProbe).values(value=write_mode)
                )
            elif write_mode == "text_dml":
                session.execute(
                    text(
                        "INSERT INTO write_fence_probe (value) "
                        "VALUES (:value)"
                    ),
                    {"value": write_mode},
                )
            else:
                session.execute(
                    text(
                        "WITH payload(value) AS (VALUES (:value)) "
                        "INSERT INTO write_fence_probe (value) "
                        "SELECT value FROM payload"
                    ),
                    {"value": write_mode},
                )
            session.commit()
        except BaseException as exc:
            errors.append(exc)
        finally:
            session.close()
            writer_finished.set()

    thread = threading.Thread(target=write_without_explicit_lock, daemon=True)
    thread.start()
    try:
        assert writer_started.wait(timeout=5)
        assert not writer_finished.wait(timeout=0.25)
        holder.rollback()
        assert writer_finished.wait(timeout=5)
        assert errors == []
        with Session(engine) as session:
            assert session.scalars(select(_WriteFenceProbe.value)).all() == [
                write_mode
            ]
    finally:
        if holder.in_transaction():
            holder.rollback()
        holder.close()
        thread.join(timeout=5)
        engine.dispose()

    assert not thread.is_alive()


@pytest.mark.parametrize(
    "write_mode",
    (
        "core_insert",
        "core_update",
        "core_delete",
        "text_insert",
        "text_cte_insert",
    ),
)
def test_unfenced_connection_execute_rejects_runtime_dml(
    tmp_path,
    write_mode,
) -> None:
    engine = create_db_engine(
        f"sqlite:///{tmp_path / f'core-fence-{write_mode}.db'}"
    )
    _WriteFenceBase.metadata.create_all(engine)
    if write_mode in {"core_update", "core_delete"}:
        with fenced_core_transaction(engine) as connection:
            connection.execute(
                insert(_WriteFenceProbe).values(id=1, value="seed")
            )

    try:
        with engine.connect() as connection:
            with pytest.raises(
                RuntimeError,
                match=r"fenced_core_transaction\(\)",
            ):
                if write_mode == "core_insert":
                    connection.execute(
                        insert(_WriteFenceProbe).values(
                            id=1,
                            value=write_mode,
                        )
                    )
                elif write_mode == "core_update":
                    connection.execute(
                        update(_WriteFenceProbe)
                        .where(_WriteFenceProbe.id == 1)
                        .values(value=write_mode)
                    )
                elif write_mode == "core_delete":
                    connection.execute(
                        delete(_WriteFenceProbe).where(
                            _WriteFenceProbe.id == 1
                        )
                    )
                elif write_mode == "text_insert":
                    connection.execute(
                        text(
                            "INSERT INTO write_fence_probe (id, value) "
                            "VALUES (:id, :value)"
                        ),
                        {"id": 1, "value": write_mode},
                    )
                else:
                    connection.execute(
                        text(
                            "WITH payload(id, value) AS "
                            "(VALUES (:id, :value)) "
                            "INSERT INTO write_fence_probe (id, value) "
                            "SELECT id, value FROM payload"
                        ),
                        {"id": 1, "value": write_mode},
                    )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("write_mode", "statement"),
    (
        (
            "insert",
            "INSERT INTO write_fence_probe (id, value) "
            "VALUES (1, 'insert')",
        ),
        (
            "update",
            "UPDATE write_fence_probe SET value = 'update' WHERE id = 1",
        ),
        ("delete", "DELETE FROM write_fence_probe WHERE id = 1"),
        (
            "cte_insert",
            "WITH payload(id, value) AS (VALUES (1, 'cte_insert')) "
            "INSERT INTO write_fence_probe (id, value) "
            "SELECT id, value FROM payload",
        ),
    ),
)
def test_unfenced_exec_driver_sql_rejects_runtime_dml(
    tmp_path,
    write_mode,
    statement,
) -> None:
    engine = create_db_engine(
        f"sqlite:///{tmp_path / f'driver-fence-{write_mode}.db'}"
    )
    _WriteFenceBase.metadata.create_all(engine)
    if write_mode in {"update", "delete"}:
        with fenced_core_transaction(engine) as connection:
            connection.execute(
                insert(_WriteFenceProbe).values(id=1, value="seed")
            )

    try:
        with engine.connect() as connection:
            with pytest.raises(
                RuntimeError,
                match=r"fenced_core_transaction\(\)",
            ):
                connection.exec_driver_sql(statement)
    finally:
        engine.dispose()


def test_fenced_core_transaction_permits_runtime_core_dml(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'fenced-core.db'}")
    _WriteFenceBase.metadata.create_all(engine)

    try:
        with fenced_core_transaction(engine) as connection:
            connection.execute(
                insert(_WriteFenceProbe).values(id=1, value="initial")
            )
            connection.execute(
                update(_WriteFenceProbe)
                .where(_WriteFenceProbe.id == 1)
                .values(value="updated")
            )
            connection.execute(
                text(
                    "WITH payload(id, value) AS (VALUES (:id, :value)) "
                    "INSERT INTO write_fence_probe (id, value) "
                    "SELECT id, value FROM payload"
                ),
                {"id": 2, "value": "cte"},
            )
            connection.exec_driver_sql(
                "INSERT INTO write_fence_probe (id, value) VALUES (?, ?)",
                (3, "driver"),
            )
            connection.execute(
                delete(_WriteFenceProbe).where(_WriteFenceProbe.id == 2)
            )

        with engine.connect() as connection:
            assert connection.execute(
                select(_WriteFenceProbe.id, _WriteFenceProbe.value).order_by(
                    _WriteFenceProbe.id
                )
            ).all() == [(1, "updated"), (3, "driver")]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "statement",
    (
        "CREATE TABLE runtime_ddl_probe (id INTEGER PRIMARY KEY)",
        "ALTER TABLE write_fence_probe ADD COLUMN note TEXT",
        "DROP TABLE write_fence_probe",
        "TRUNCATE TABLE write_fence_probe",
        "COPY write_fence_probe FROM STDIN",
        "CALL refresh_wellness_summary()",
        "DO 'BEGIN NULL; END'",
    ),
)
def test_extended_write_fence_rejects_unfenced_driver_mutations(
    tmp_path,
    statement,
) -> None:
    engine = create_db_engine(
        f"sqlite:///{tmp_path / 'extended-driver-fence.db'}"
    )
    _WriteFenceBase.metadata.create_all(engine)
    activate_runtime_extended_write_fence(engine)

    try:
        with engine.connect() as connection:
            with pytest.raises(
                RuntimeError,
                match=r"fenced_core_transaction\(\)",
            ):
                connection.exec_driver_sql(statement)
    finally:
        engine.dispose()


def test_extended_write_fence_rejects_unfenced_textual_ddl(
    tmp_path,
) -> None:
    engine = create_db_engine(
        f"sqlite:///{tmp_path / 'extended-text-fence.db'}"
    )
    _WriteFenceBase.metadata.create_all(engine)
    activate_runtime_extended_write_fence(engine)

    try:
        with engine.connect() as connection:
            with pytest.raises(
                RuntimeError,
                match=r"fenced_core_transaction\(\)",
            ):
                connection.execute(
                    text(
                        "CREATE TABLE runtime_text_probe "
                        "(id INTEGER PRIMARY KEY)"
                    )
                )
    finally:
        engine.dispose()


def test_extended_write_fence_starts_after_trusted_schema_bootstrap(
    tmp_path,
) -> None:
    engine = create_db_engine(
        f"sqlite:///{tmp_path / 'trusted-bootstrap.db'}"
    )

    try:
        _WriteFenceBase.metadata.create_all(engine)
        activate_runtime_extended_write_fence(engine)

        with engine.connect() as connection:
            assert connection.execute(
                select(_WriteFenceProbe.id)
            ).all() == []
            with pytest.raises(
                RuntimeError,
                match=r"fenced_core_transaction\(\)",
            ):
                _WriteFenceBase.metadata.drop_all(connection)
    finally:
        engine.dispose()


def test_fenced_core_transaction_clears_marker_after_commit(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'commit-marker.db'}")
    _WriteFenceBase.metadata.create_all(engine)

    try:
        with engine.connect() as connection:
            with fenced_core_transaction(connection) as fenced_connection:
                assert fenced_connection is connection
                assert _WRITE_FENCE_INFO_KEY in connection.info
                connection.execute(
                    insert(_WriteFenceProbe).values(id=1, value="committed")
                )

            assert _WRITE_FENCE_INFO_KEY not in connection.info
            with pytest.raises(
                RuntimeError,
                match=r"fenced_core_transaction\(\)",
            ):
                connection.execute(
                    insert(_WriteFenceProbe).values(
                        id=2,
                        value="unfenced",
                    )
                )
    finally:
        engine.dispose()


def test_fenced_core_transaction_clears_marker_after_rollback(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'rollback-marker.db'}")
    _WriteFenceBase.metadata.create_all(engine)

    try:
        with engine.connect() as connection:
            with pytest.raises(ValueError, match="force rollback"):
                with fenced_core_transaction(connection):
                    assert _WRITE_FENCE_INFO_KEY in connection.info
                    connection.execute(
                        insert(_WriteFenceProbe).values(
                            id=1,
                            value="rolled-back",
                        )
                    )
                    raise ValueError("force rollback")

            assert _WRITE_FENCE_INFO_KEY not in connection.info
            assert connection.scalar(
                select(_WriteFenceProbe.id).where(_WriteFenceProbe.id == 1)
            ) is None
            connection.rollback()
            with pytest.raises(
                RuntimeError,
                match=r"fenced_core_transaction\(\)",
            ):
                connection.execute(
                    insert(_WriteFenceProbe).values(
                        id=2,
                        value="unfenced",
                    )
                )
    finally:
        engine.dispose()


def test_engine_checkout_clears_stale_write_fence_marker(tmp_path) -> None:
    engine = create_db_engine(
        f"sqlite:///{tmp_path / 'checkout-marker.db'}",
        pool_size=1,
        max_overflow=0,
    )
    pool_identity_key = "test_write_fence_pool_identity"

    try:
        with engine.connect() as connection:
            connection.info[pool_identity_key] = "same-pool-record"
            connection.info[_WRITE_FENCE_INFO_KEY] = object()

        with engine.connect() as connection:
            assert connection.info[pool_identity_key] == "same-pool-record"
            assert _WRITE_FENCE_INFO_KEY not in connection.info
            connection.info.pop(pool_identity_key)
    finally:
        engine.dispose()


def test_global_guard_transaction_fence_expires_with_guard_lifetime(
    tmp_path,
) -> None:
    engine = create_db_engine(
        f"sqlite:///{tmp_path / 'global-guard-transaction-lifetime.db'}"
    )
    _WriteFenceBase.metadata.create_all(engine)
    session = Session(engine)
    second_entered = threading.Event()
    release_second = threading.Event()
    failures: list[BaseException] = []

    def hold_second_guard() -> None:
        try:
            with global_write_plane_guard(engine):
                second_entered.set()
                if not release_second.wait(timeout=5):
                    raise TimeoutError("timed out releasing second guard")
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=hold_second_guard)
    try:
        with global_write_plane_guard(engine):
            session.execute(
                insert(_WriteFenceProbe).values(
                    id=1,
                    value="started-under-first-guard",
                )
            )
        assert session.in_transaction()

        worker.start()
        assert second_entered.wait(timeout=5)
        with pytest.raises(
            RuntimeError,
            match=r"fenced_core_transaction\(\)",
        ):
            session.connection().execute(
                insert(_WriteFenceProbe).values(
                    id=2,
                    value="write-under-second-guard",
                )
            )

        release_second.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert failures == []

        # A Session write remains legitimate: it acquires a fresh transaction
        # fence instead of reusing the expired global-guard authorization.
        session.execute(
            insert(_WriteFenceProbe).values(
                id=3,
                value="fresh-session-fence",
            )
        )
        session.commit()
        assert session.scalars(
            select(_WriteFenceProbe.id).order_by(_WriteFenceProbe.id)
        ).all() == [1, 3]
    finally:
        release_second.set()
        worker.join(timeout=5)
        if session.in_transaction():
            session.rollback()
        session.close()
        engine.dispose()


def test_migration_engine_can_opt_out_of_runtime_core_dml_fence(
    tmp_path,
) -> None:
    engine = create_db_engine(
        f"sqlite:///{tmp_path / 'migration-opt-out.db'}",
        enforce_runtime_write_fence=False,
    )
    _WriteFenceBase.metadata.create_all(engine)

    try:
        with Session(engine) as session:
            session.add(
                _WriteFenceProbe(id=10, value="orm-unfenced")
            )
            session.flush()
            assert not session_holds_write_plane(session)
            session.commit()

        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE migration_probe "
                "(id INTEGER PRIMARY KEY)"
            )
            connection.execute(
                insert(_WriteFenceProbe).values(id=1, value="inserted")
            )
            connection.execute(
                update(_WriteFenceProbe)
                .where(_WriteFenceProbe.id == 1)
                .values(value="updated")
            )
            connection.execute(
                text(
                    "WITH payload(id, value) AS (VALUES (:id, :value)) "
                    "INSERT INTO write_fence_probe (id, value) "
                    "SELECT id, value FROM payload"
                ),
                {"id": 2, "value": "cte"},
            )
            connection.exec_driver_sql(
                "DELETE FROM write_fence_probe WHERE id = ?",
                (2,),
            )

        with engine.connect() as connection:
            assert connection.execute(
                select(_WriteFenceProbe.id, _WriteFenceProbe.value).order_by(
                    _WriteFenceProbe.id
                )
            ).all() == [
                (1, "updated"),
                (10, "orm-unfenced"),
            ]
    finally:
        engine.dispose()


def test_third_party_engine_is_not_claimed_by_healthmes_session_fence(
    tmp_path,
    monkeypatch,
) -> None:
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'third-party.db'}"
    )
    _WriteFenceBase.metadata.create_all(engine)
    calls: list[Session] = []
    real_lock = locking_module.lock_activity_write_plane

    def record_lock(session, **kwargs):
        calls.append(session)
        return real_lock(session, **kwargs)

    monkeypatch.setattr(
        locking_module,
        "lock_activity_write_plane",
        record_lock,
    )
    try:
        with Session(engine) as session:
            session.add(_WriteFenceProbe(value="third-party"))
            session.commit()

        assert calls == []
        with Session(engine) as session:
            assert session.scalars(
                select(_WriteFenceProbe.value)
            ).all() == ["third-party"]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("execution_api", "statement"),
    (
        ("driver", "PRAGMA user_version = 165"),
        ("text", "SELECT setval('wellness_sequence', 42)"),
        ("driver", "SELECT 1 INTO wellness_snapshot"),
        ("text", "SELECT user_defined_mutation()"),
    ),
)
def test_extended_fence_treats_unverified_raw_sql_as_write(
    tmp_path,
    execution_api,
    statement,
) -> None:
    engine = create_db_engine(
        f"sqlite:///{tmp_path / 'unverified-raw-sql.db'}"
    )
    activate_runtime_extended_write_fence(engine)

    try:
        with engine.connect() as connection:
            with pytest.raises(
                RuntimeError,
                match=r"fenced_core_transaction\(\)",
            ):
                if execution_api == "driver":
                    connection.exec_driver_sql(statement)
                else:
                    connection.execute(text(statement))
    finally:
        engine.dispose()


def test_sqlite_query_only_control_survives_extended_runtime_fence(
    tmp_path,
) -> None:
    engine = create_db_engine(
        f"sqlite:///{tmp_path / 'query-only-control.db'}"
    )
    activate_runtime_extended_write_fence(engine)

    try:
        with engine.connect() as connection:
            driver_connection = connection.connection.driver_connection
            set_sqlite_query_only(connection, enabled=True)
            assert driver_connection.execute(
                "PRAGMA query_only"
            ).fetchone() == (1,)

            set_sqlite_query_only(connection, enabled=False)
            assert driver_connection.execute(
                "PRAGMA query_only"
            ).fetchone() == (0,)
    finally:
        engine.dispose()


def test_sqlite_busy_timeout_control_survives_extended_runtime_fence(
    tmp_path,
) -> None:
    engine = create_db_engine(
        f"sqlite:///{tmp_path / 'busy-timeout-control.db'}"
    )
    activate_runtime_extended_write_fence(engine)

    try:
        with engine.connect() as connection:
            locking_module.set_sqlite_busy_timeout_ms(connection, 165)
            assert locking_module.sqlite_busy_timeout_ms(connection) == 165
            with pytest.raises(
                RuntimeError,
                match=r"fenced_core_transaction\(\)",
            ):
                connection.exec_driver_sql("PRAGMA busy_timeout=166")
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "legacy_option",
    (
        "_healthmes_internal_write_fence_bypass",
        "_healthmes_verified_read_only_sql",
    ),
)
def test_legacy_execution_options_cannot_bypass_core_write_fence(
    tmp_path,
    legacy_option,
) -> None:
    engine = create_db_engine(
        f"sqlite:///{tmp_path / f'legacy-option-{legacy_option}.db'}"
    )
    _WriteFenceBase.metadata.create_all(engine)
    activate_runtime_extended_write_fence(engine)

    try:
        with engine.connect() as connection:
            with pytest.raises(
                RuntimeError,
                match=r"fenced_core_transaction\(\)",
            ):
                connection.execute(
                    insert(_WriteFenceProbe)
                    .values(id=1, value="forged")
                    .execution_options(**{legacy_option: True})
                )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "statement",
    (
        "PRAGMA query_only",
        "PRAGMA user_version=165",
    ),
)
def test_forged_database_control_token_is_rejected(
    tmp_path,
    statement,
) -> None:
    engine = create_db_engine(
        f"sqlite:///{tmp_path / 'forged-control-token.db'}"
    )
    activate_runtime_extended_write_fence(engine)

    try:
        with engine.connect() as connection:
            with pytest.raises(
                RuntimeError,
                match="invalid internal database control authorization",
            ):
                connection.exec_driver_sql(
                    statement,
                    execution_options={
                        "_healthmes_internal_database_control": object(),
                    },
                )
    finally:
        engine.dispose()


def test_forged_database_control_identity_is_rejected(tmp_path) -> None:
    engine = create_db_engine(
        f"sqlite:///{tmp_path / 'forged-control-identity.db'}"
    )
    activate_runtime_extended_write_fence(engine)
    operation = locking_module._DatabaseControlOp.SQLITE_QUERY_ONLY_READ
    authorization = locking_module._DatabaseControlAuthorization(
        operation=operation,
        token=object(),
    )

    try:
        with engine.connect() as connection:
            with pytest.raises(
                RuntimeError,
                match="invalid internal database control authorization",
            ):
                connection.exec_driver_sql(
                    "PRAGMA query_only",
                    execution_options={
                        "_healthmes_internal_database_control": (
                            authorization
                        ),
                    },
                )
    finally:
        engine.dispose()


def test_canonical_control_authorization_rejects_noncanonical_sql(
    tmp_path,
) -> None:
    engine = create_db_engine(
        f"sqlite:///{tmp_path / 'noncanonical-control-sql.db'}"
    )
    activate_runtime_extended_write_fence(engine)
    operation = locking_module._DatabaseControlOp.SQLITE_QUERY_ONLY_READ

    try:
        with engine.connect() as connection:
            with pytest.raises(
                RuntimeError,
                match="does not match its canonical contract",
            ):
                connection.exec_driver_sql(
                    "PRAGMA user_version=165",
                    execution_options=(
                        locking_module._database_control_options(operation)
                    ),
                )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "legacy_option",
    (
        "_healthmes_internal_write_fence_bypass",
        "_healthmes_verified_read_only_sql",
    ),
)
def test_legacy_execution_options_cannot_mark_raw_mutation_safe(
    tmp_path,
    legacy_option,
) -> None:
    engine = create_db_engine(
        f"sqlite:///{tmp_path / f'legacy-raw-{legacy_option}.db'}"
    )
    activate_runtime_extended_write_fence(engine)

    try:
        with engine.connect() as connection:
            with pytest.raises(
                RuntimeError,
                match=r"fenced_core_transaction\(\)",
            ):
                connection.exec_driver_sql(
                    "PRAGMA user_version=165",
                    execution_options={legacy_option: True},
                )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "statement",
    (
        "SELECT 1",
        "SELECT 'INSERT INTO ignored' /* UPDATE ignored */",
    ),
)
def test_raw_text_reads_do_not_claim_the_write_plane(
    tmp_path,
    statement,
) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'read-only.db'}")
    try:
        with Session(engine) as session:
            session.execute(text(statement))
            assert not session_holds_write_plane(session)
            session.rollback()
    finally:
        engine.dispose()


def test_file_sqlite_runtime_lock_blocks_independent_processes(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'runtime.db'}"
    context = multiprocessing.get_context("spawn")
    acquired = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_sqlite_runtime,
        args=(database_url, acquired, release),
    )

    holder.start()
    try:
        assert acquired.wait(timeout=5)
        with pytest.raises(
            TimeoutError,
            match="SQLite file lock",
        ):
            with sqlite_runtime_guard(
                database_url,
                timeout_seconds=0.1,
            ):
                pytest.fail("SQLite runtime lock unexpectedly acquired")
    finally:
        release.set()
        holder.join(timeout=5)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5)

    assert holder.exitcode == 0


def test_file_sqlite_engine_enables_wal_and_busy_timeout(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'activity.db'}")
    with engine.connect() as connection:
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
        busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()

    assert str(journal_mode).casefold() == "wal"
    assert int(busy_timeout) == 30_000
    engine.dispose()
