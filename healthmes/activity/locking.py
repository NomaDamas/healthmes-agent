"""Process-local serialization for the self-hosted activity write plane."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import BinaryIO

from sqlalchemy import event, text
from sqlalchemy.orm import Session

_ACTIVITY_WRITE_LOCK = RLock()
_ACTIVITY_WRITE_PLANE_KEY = "healthmes:activity:write-plane:v1"
_SQLITE_FILE_LOCK_INFO_KEY = "healthmes_activity_sqlite_file_lock"

if os.name == "nt":  # pragma: no cover - exercised on Windows runners
    import msvcrt
else:
    import fcntl


@contextmanager
def activity_write_lock():
    """Serialize activity ingest, retention, deletion, and summary writes."""
    with _ACTIVITY_WRITE_LOCK:
        yield


def _sqlite_database_path(session: Session) -> Path | None:
    bind = session.get_bind()
    engine = getattr(bind, "engine", bind)
    url = engine.url
    database = url.database
    if database in {None, "", ":memory:"}:
        return None
    return Path(database).expanduser().resolve()


def _lock_file(handle: BinaryIO) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows runners
        handle.seek(0)
        if handle.read(1) == b"":
            handle.seek(0)
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle: BinaryIO) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows runners
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@event.listens_for(Session, "after_transaction_end")
def _release_sqlite_activity_lock(session: Session, transaction) -> None:
    if transaction.parent is not None:
        return
    handle = session.info.pop(_SQLITE_FILE_LOCK_INFO_KEY, None)
    if handle is None:
        return
    try:
        _unlock_file(handle)
    finally:
        handle.close()


def _lock_sqlite_write_plane(session: Session) -> None:
    if _SQLITE_FILE_LOCK_INFO_KEY in session.info:
        return
    database = _sqlite_database_path(session)
    if database is None:
        return
    lock_path = database.with_name(f"{database.name}.activity.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        _lock_file(handle)
    except BaseException:
        handle.close()
        raise
    session.info[_SQLITE_FILE_LOCK_INFO_KEY] = handle
    # Ensure transaction cleanup, session.close(), and rollback all trigger the
    # after_transaction_end release hook even if the caller fails before DML.
    session.connection()


def lock_activity_write_plane(session: Session) -> None:
    """Serialize PostgreSQL activity writes across processes and devices."""
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        _lock_sqlite_write_plane(session)
        return
    if dialect != "postgresql":
        return
    session.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended(:write_plane_key, 0)"
            ")"
        ),
        {"write_plane_key": _ACTIVITY_WRITE_PLANE_KEY},
    )
