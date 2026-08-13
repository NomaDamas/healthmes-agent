from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from errno import EACCES, EAGAIN, EDEADLK
from pathlib import Path
from typing import BinaryIO

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from healthmes.store.enums import CalendarSource

_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_HELD_LOCKS: ContextVar[frozenset[tuple[tuple[int, int | None], str]]] = (
    ContextVar("healthmes_calendar_held_locks", default=frozenset())
)

if os.name == "nt":  # pragma: no cover - exercised on Windows runners
    import msvcrt
else:
    import fcntl


def calendar_write_lock_key(source: CalendarSource) -> str:
    return f"healthmes:calendar-write:{source.value}"


def _process_lock(key: str) -> threading.Lock:
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.Lock())


def _execution_owner() -> tuple[int, int | None]:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), id(task) if task is not None else None


def _lock_identity(
    session: Session,
    source: CalendarSource,
) -> tuple[str, Path | None]:
    bind = session.get_bind()
    engine = bind.engine if isinstance(bind, Connection) else bind
    if bind.dialect.name == "sqlite":
        database = engine.url.database
        if database not in {None, "", ":memory:"}:
            path = Path(database).expanduser().resolve()
            return f"sqlite:{path}:{source.value}", path
        return f"sqlite-memory:{id(engine)}:{source.value}", None
    return (
        f"{bind.dialect.name}:"
        f"{engine.url.render_as_string(hide_password=True)}:{source.value}",
        None,
    )


def _lock_file(handle: BinaryIO) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows runners
        handle.seek(0)
        if handle.read(1) == b"":
            handle.seek(0)
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if exc.errno not in {EACCES, EAGAIN, EDEADLK}:
                    raise
                time.sleep(0.05)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle: BinaryIO) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows runners
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def calendar_write_lock(
    session: Session,
    source: CalendarSource,
) -> Iterator[None]:
    """Serialize HealthMes writes to one provider calendar.

    PostgreSQL uses a session-level advisory lock so separate workers share
    the boundary. SQLite combines a process lock with a database-adjacent file
    lock so the service and a separate ``healthmes connect`` CLI process share
    the same revocation boundary. Nested calls in the same sync/async execution
    context reuse the outer lock.
    """

    bind = session.get_bind()
    identity, sqlite_database = _lock_identity(session, source)
    marker = (_execution_owner(), identity)
    held = _HELD_LOCKS.get()
    if marker in held:
        yield
        return

    token = _HELD_LOCKS.set(frozenset((*held, marker)))
    process_lock = _process_lock(identity)
    process_lock.acquire()
    file_handle: BinaryIO | None = None
    connection: Connection | None = None
    try:
        if sqlite_database is not None:
            lock_path = sqlite_database.with_name(
                f"{sqlite_database.name}.calendar-{source.value}.lock"
            )
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            file_handle = lock_path.open("a+b")
            _lock_file(file_handle)

        if bind.dialect.name != "postgresql":
            yield
            return

        engine = bind.engine if isinstance(bind, Connection) else bind
        key = calendar_write_lock_key(source)
        while connection is None:
            candidate = engine.connect()
            try:
                acquired = candidate.scalar(
                    sa.text(
                        "SELECT pg_try_advisory_lock("
                        "hashtextextended(:lock_key, 0))"
                    ),
                    {"lock_key": key},
                )
            except Exception:
                candidate.close()
                raise
            if acquired is True:
                connection = candidate
                break
            candidate.close()
            time.sleep(0.05)
        yield
    finally:
        try:
            if connection is not None:
                try:
                    released = connection.scalar(
                        sa.text(
                            "SELECT pg_advisory_unlock("
                            "hashtextextended(:lock_key, 0))"
                        ),
                        {"lock_key": calendar_write_lock_key(source)},
                    )
                    if released is not True:
                        raise RuntimeError(
                            "PostgreSQL calendar write lock was not held"
                        )
                finally:
                    connection.close()
            if file_handle is not None:
                try:
                    _unlock_file(file_handle)
                finally:
                    file_handle.close()
        finally:
            process_lock.release()
            _HELD_LOCKS.reset(token)
