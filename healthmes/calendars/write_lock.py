from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from healthmes.store.enums import CalendarSource

_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[CalendarSource, threading.RLock] = {}


def calendar_write_lock_key(source: CalendarSource) -> str:
    return f"healthmes:calendar-write:{source.value}"


def _process_lock(source: CalendarSource) -> threading.RLock:
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(source, threading.RLock())


@contextmanager
def calendar_write_lock(
    session: Session,
    source: CalendarSource,
) -> Iterator[None]:
    """Serialize HealthMes writes to one provider calendar.

    PostgreSQL uses a session-level advisory lock so separate workers share
    the boundary. SQLite uses a process lock, matching its single-host runtime.
    """

    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        with _process_lock(source):
            yield
        return

    engine = bind.engine if isinstance(bind, Connection) else bind
    key = calendar_write_lock_key(source)
    connection: Connection | None = None
    try:
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
        if connection is not None:
            try:
                released = connection.scalar(
                    sa.text(
                        "SELECT pg_advisory_unlock(hashtextextended(:lock_key, 0))"
                    ),
                    {"lock_key": key},
                )
                if released is not True:
                    raise RuntimeError(
                        "PostgreSQL calendar write lock was not held"
                    )
            finally:
                connection.close()
