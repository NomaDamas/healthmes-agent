"""Process-local serialization for the self-hosted activity write plane."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from time import monotonic, sleep
from typing import BinaryIO

from sqlalchemy import event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

_ACTIVITY_WRITE_LOCK = RLock()
_ACTIVITY_WRITE_PLANE_KEY = "healthmes:activity:write-plane:v1"
_SQLITE_FILE_LOCK_INFO_KEY = "healthmes_activity_sqlite_file_lock"
_POSTGRES_GUARD_TIMEOUT_SECONDS = 5.0
_POSTGRES_GUARD_POLL_SECONDS = 0.05
_LOGGER = logging.getLogger(__name__)

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


@contextmanager
def postgres_activity_write_plane_guard(
    bind: Engine | Connection,
    *,
    timeout_seconds: float = _POSTGRES_GUARD_TIMEOUT_SECONDS,
    poll_seconds: float = _POSTGRES_GUARD_POLL_SECONDS,
) -> Iterator[Connection | None]:
    """Hold the activity write plane before opening a serializable snapshot.

    PostgreSQL transaction-scoped advisory locks establish the transaction
    snapshot before a waiter necessarily acquires the lock. Finalization needs
    the opposite order: wait for existing activity writers, then open the
    serializable transaction that revalidates sources. A session-scoped lock
    on a dedicated pooled connection provides that ordering and conflicts with
    the existing transaction-scoped lock because both use the same key.
    """

    if bind.dialect.name != "postgresql":
        yield None
        return

    engine = bind.engine if isinstance(bind, Connection) else bind
    with engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as connection:
        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("PostgreSQL lock bounds must be positive")
        lock_attempted = False
        try:
            deadline = monotonic() + timeout_seconds
            acquired = False
            while monotonic() < deadline:
                # Cleanup starts before PostgreSQL can grant the lock. A driver
                # or result-processing failure after server-side acquisition
                # must not return a lock-holding connection to the pool.
                lock_attempted = True
                acquired = bool(
                    connection.scalar(
                        text(
                            "SELECT pg_try_advisory_lock("
                            "hashtextextended(:write_plane_key, 0)"
                            ")"
                        ),
                        {"write_plane_key": _ACTIVITY_WRITE_PLANE_KEY},
                    )
                )
                if acquired:
                    break
                sleep(
                    min(
                        poll_seconds,
                        max(0.0, deadline - monotonic()),
                    )
                )
            if not acquired:
                raise TimeoutError(
                    "timed out waiting for the activity write plane"
                )
            # SQLAlchemy still opens a logical transaction around AUTOCOMMIT
            # statements. End it before changing the connection isolation
            # level; the session-scoped advisory lock survives this boundary.
            # Keep this inside the cleanup guard because even this logical
            # commit can fail after PostgreSQL granted the advisory lock.
            connection.commit()
            # Reuse this connection for the finalization transaction. Using a
            # second pooled connection after acquiring the guard can deadlock
            # when concurrent waiters exhaust a small connection pool.
            connection.execution_options(
                isolation_level="SERIALIZABLE"
            )
            yield connection
        finally:
            if lock_attempted:
                try:
                    if connection.in_transaction():
                        connection.rollback()
                    connection.execution_options(
                        isolation_level="AUTOCOMMIT"
                    )
                    connection.execute(
                        text(
                            "SELECT pg_advisory_unlock("
                            "hashtextextended(:write_plane_key, 0)"
                            ")"
                        ),
                        {"write_plane_key": _ACTIVITY_WRITE_PLANE_KEY},
                    )
                    connection.commit()
                except Exception as exc:
                    # Closing an invalidated PostgreSQL connection releases
                    # every session advisory lock without changing an already
                    # committed DecisionRecord outcome.
                    _LOGGER.exception(
                        "failed to clean up PostgreSQL activity write guard"
                    )
                    try:
                        connection.invalidate(exc)
                    except Exception:
                        _LOGGER.exception(
                            "failed to invalidate PostgreSQL activity guard "
                            "connection"
                        )
                        try:
                            # Detach prevents a physical connection that may
                            # still hold a session advisory lock from returning
                            # to the pool. Closing the detached checkout
                            # discards it while preserving the committed
                            # DecisionRecord.
                            connection.detach()
                            connection.close()
                        except Exception:
                            _LOGGER.exception(
                                "failed to discard PostgreSQL activity guard "
                                "connection"
                            )
