from __future__ import annotations

import asyncio
import math
import os
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from errno import EACCES, EAGAIN, EDEADLK, EWOULDBLOCK
from pathlib import Path
from typing import BinaryIO

from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from healthmes.activity.locking import (
    _POSTGRES_CONNECT_WORKER_LIMIT as _SHARED_POSTGRES_CONNECT_WORKER_LIMIT,
)
from healthmes.activity.locking import (
    _connect_with_bounded_worker,
    _raise_postgres_advisory_cleanup_failure,
    release_postgres_advisory_lock,
    try_postgres_advisory_lock,
)
from healthmes.store.enums import CalendarSource

__all__ = [
    "DEFAULT_CALENDAR_WRITE_LOCK_TIMEOUT_SECONDS",
    "CalendarWriteLockOrderError",
    "calendar_write_lock",
    "calendar_write_lock_key",
    "calendar_write_locks",
    "ordered_calendar_write_sources",
]

DEFAULT_CALENDAR_WRITE_LOCK_TIMEOUT_SECONDS = 30.0
_CALENDAR_WRITE_LOCK_POLL_SECONDS = 0.05
_POSTGRES_CONNECT_WORKER_LIMIT = _SHARED_POSTGRES_CONNECT_WORKER_LIMIT
_CALENDAR_SOURCE_ORDER = {
    source: index for index, source in enumerate(CalendarSource)
}
_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_HELD_LOCKS: ContextVar[frozenset[tuple[tuple[int, int | None], str]]] = (
    ContextVar("healthmes_calendar_held_locks", default=frozenset())
)

if os.name == "nt":  # pragma: no cover - exercised on Windows runners
    import msvcrt
else:
    import fcntl


class CalendarWriteLockOrderError(RuntimeError):
    """Raised before a nested provider request would invert canonical order."""


def calendar_write_lock_key(source: CalendarSource) -> str:
    return f"healthmes:calendar-write:{source.value}"


def _calendar_write_lock_order_key(
    source: CalendarSource,
) -> tuple[int, str]:
    return (
        _CALENDAR_SOURCE_ORDER[source],
        calendar_write_lock_key(source),
    )


def ordered_calendar_write_sources(
    sources: Iterable[CalendarSource],
) -> tuple[CalendarSource, ...]:
    """Deduplicate provider locks and return their canonical acquisition order."""

    return tuple(
        sorted(
            set(sources),
            key=_calendar_write_lock_order_key,
        )
    )


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


def _lock_deadline(timeout_seconds: float) -> float:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError(
            "calendar write lock timeout must be a finite positive number"
        )
    return time.monotonic() + timeout_seconds


def _remaining_lock_time(
    deadline: float,
    *,
    layer: str,
    key: str,
) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(
            f"timed out waiting for {layer} calendar write lock {key!r}"
        )
    return remaining


def _wait_for_lock_retry(
    deadline: float,
    *,
    layer: str,
    key: str,
) -> None:
    remaining = _remaining_lock_time(
        deadline,
        layer=layer,
        key=key,
    )
    time.sleep(min(_CALENDAR_WRITE_LOCK_POLL_SECONDS, remaining))


@dataclass(slots=True)
class _CalendarControlConnection:
    connection: Connection

    def close(self) -> None:
        self.connection.close()


def _connect_before_deadline(
    engine,
    *,
    deadline: float,
    key: str,
) -> _CalendarControlConnection:
    """Bound pool checkout/connect without leaking a late connection."""
    _remaining_lock_time(
        deadline,
        layer="PostgreSQL advisory",
        key=key,
    )

    def connect() -> _CalendarControlConnection:
        candidate = engine.connect()
        return _CalendarControlConnection(
            connection=candidate,
        )

    return _connect_with_bounded_worker(
        connect,
        close_late=lambda connected: connected.close(),
        deadline=deadline,
        timeout_message=(
            "timed out waiting for PostgreSQL advisory calendar write lock "
            f"{key!r}"
        ),
        worker_name="healthmes-calendar-control-connect",
        clock=time.monotonic,
    )


def _lock_file(
    handle: BinaryIO,
    *,
    deadline: float,
    key: str,
) -> None:
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
                if exc.errno not in {
                    EACCES,
                    EAGAIN,
                    EDEADLK,
                    EWOULDBLOCK,
                }:
                    raise
                _wait_for_lock_retry(
                    deadline,
                    layer="SQLite file",
                    key=key,
                )
        return

    while True:
        try:
            fcntl.flock(
                handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
            return
        except OSError as exc:
            if exc.errno not in {
                EACCES,
                EAGAIN,
                EDEADLK,
                EWOULDBLOCK,
            }:
                raise
            _wait_for_lock_retry(
                deadline,
                layer="SQLite file",
                key=key,
            )


def _unlock_file(handle: BinaryIO) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows runners
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _calendar_write_lock_until(
    session: Session,
    source: CalendarSource,
    *,
    deadline: float,
) -> Iterator[None]:
    bind = session.get_bind()
    identity, sqlite_database = _lock_identity(session, source)
    marker = (_execution_owner(), identity)
    held = _HELD_LOCKS.get()
    if marker in held:
        yield
        return

    process_lock = _process_lock(identity)
    process_acquired = process_lock.acquire(
        timeout=_remaining_lock_time(
            deadline,
            layer="process",
            key=identity,
        )
    )
    if not process_acquired:
        raise TimeoutError(
            "timed out waiting for process calendar write lock "
            f"{identity!r}"
        )

    token = None
    file_handle: BinaryIO | None = None
    file_acquired = False
    connection: _CalendarControlConnection | None = None
    try:
        if sqlite_database is not None:
            lock_path = sqlite_database.with_name(
                f"{sqlite_database.name}.calendar-{source.value}.lock"
            )
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            file_handle = lock_path.open("a+b")
            _lock_file(
                file_handle,
                deadline=deadline,
                key=identity,
            )
            file_acquired = True

        if bind.dialect.name != "postgresql":
            token = _HELD_LOCKS.set(frozenset((*held, marker)))
            yield
            return

        engine = bind.engine if isinstance(bind, Connection) else bind
        key = calendar_write_lock_key(source)
        while connection is None:
            _remaining_lock_time(
                deadline,
                layer="PostgreSQL advisory",
                key=key,
            )
            candidate = _connect_before_deadline(
                engine,
                deadline=deadline,
                key=key,
            )
            try:
                acquired = try_postgres_advisory_lock(
                    candidate.connection,
                    key,
                )
            except Exception as exc:
                try:
                    _raise_postgres_advisory_cleanup_failure(
                        candidate.connection,
                        cause=exc,
                        context=(
                            "failed to acquire PostgreSQL calendar "
                            "write guard"
                        ),
                    )
                finally:
                    candidate.close()
            if acquired is True:
                connection = candidate
                break
            candidate.close()
            _wait_for_lock_retry(
                deadline,
                layer="PostgreSQL advisory",
                key=key,
            )
        token = _HELD_LOCKS.set(frozenset((*held, marker)))
        yield
    finally:
        try:
            if connection is not None:
                try:
                    try:
                        released = release_postgres_advisory_lock(
                            connection.connection,
                            calendar_write_lock_key(source),
                        )
                        if released is not True:
                            raise RuntimeError(
                                "PostgreSQL calendar write lock was not held"
                            )
                    except Exception as exc:
                        _raise_postgres_advisory_cleanup_failure(
                            connection.connection,
                            cause=exc,
                            context=(
                                "failed to clean up PostgreSQL calendar "
                                "write guard"
                            ),
                        )
                finally:
                    connection.close()
            if file_handle is not None:
                try:
                    if file_acquired:
                        _unlock_file(file_handle)
                finally:
                    file_handle.close()
        finally:
            process_lock.release()
            if token is not None:
                _HELD_LOCKS.reset(token)


def _assert_nested_lock_order(
    session: Session,
    ordered_sources: tuple[CalendarSource, ...],
) -> None:
    owner = _execution_owner()
    held_identities = {
        identity
        for held_owner, identity in _HELD_LOCKS.get()
        if held_owner == owner
    }
    if not held_identities:
        return

    identities_by_source = {
        source: _lock_identity(session, source)[0]
        for source in CalendarSource
    }
    held_positions = [
        _CALENDAR_SOURCE_ORDER[source]
        for source, identity in identities_by_source.items()
        if identity in held_identities
    ]
    if not held_positions:
        return

    latest_held_position = max(held_positions)
    for source in ordered_sources:
        if (
            identities_by_source[source] not in held_identities
            and _CALENDAR_SOURCE_ORDER[source] < latest_held_position
        ):
            raise CalendarWriteLockOrderError(
                "calendar provider locks must be acquired in canonical order; "
                f"cannot acquire {calendar_write_lock_key(source)!r} after a "
                "later provider lock"
            )


@contextmanager
def calendar_write_lock(
    session: Session,
    source: CalendarSource,
    *,
    timeout_seconds: float = (
        DEFAULT_CALENDAR_WRITE_LOCK_TIMEOUT_SECONDS
    ),
) -> Iterator[None]:
    """Serialize writes to one provider with a finite cross-layer deadline.

    PostgreSQL uses a session-level advisory lock so separate workers share
    the boundary. SQLite combines a process lock with a database-adjacent file
    lock so the service and a separate ``healthmes connect`` CLI process share
    the same revocation boundary. Nested calls in the same sync/async execution
    context reuse the outer lock.
    """

    _assert_nested_lock_order(session, (source,))
    with _calendar_write_lock_until(
        session,
        source,
        deadline=_lock_deadline(timeout_seconds),
    ):
        yield


@contextmanager
def calendar_write_locks(
    session: Session,
    sources: Iterable[CalendarSource],
    *,
    timeout_seconds: float = (
        DEFAULT_CALENDAR_WRITE_LOCK_TIMEOUT_SECONDS
    ),
) -> Iterator[None]:
    """Acquire every provider lock once, in one canonical bounded order."""

    ordered_sources = ordered_calendar_write_sources(sources)
    deadline = _lock_deadline(timeout_seconds)
    _assert_nested_lock_order(session, ordered_sources)
    with ExitStack() as locks:
        for source in ordered_sources:
            locks.enter_context(
                _calendar_write_lock_until(
                    session,
                    source,
                    deadline=deadline,
                )
            )
        yield
