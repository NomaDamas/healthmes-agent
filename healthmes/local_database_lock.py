"""Cross-engine transaction lock for the local SQLite data node."""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, BinaryIO

from sqlalchemy import event
from sqlalchemy.orm import Session

try:
    import fcntl
except ImportError:  # pragma: no cover - HealthMes local runtime targets POSIX
    fcntl = None

_SESSION_LOCK_KEY = "healthmes_local_database_lock"
_PROCESS_LOCKS: dict[Hashable, dict[str, Any]] = {}
_PROCESS_LOCKS_GUARD = RLock()
_SQLITE_MEMORY_DATABASES = {None, "", ":memory:"}


@dataclass(slots=True)
class _HeldDatabaseLock:
    key: Hashable
    process_lock: RLock
    database_handle: BinaryIO | None


def _engine(session: Session):
    bind = session.get_bind()
    return getattr(bind, "engine", bind)


def _lock_identity(session: Session) -> tuple[Hashable, Path | None]:
    engine = _engine(session)
    if engine.dialect.name == "sqlite":
        database = engine.url.database
        if database not in _SQLITE_MEMORY_DATABASES:
            path = Path(str(database)).expanduser().resolve()
            return ("sqlite-file", str(path)), path
    return ("engine", id(engine)), None


def _advisory_lock_path(database_path: Path) -> Path:
    return database_path.with_name(f".{database_path.name}.healthmes.lock")


def acquire_local_database_lock(session: Session) -> None:
    """Hold one process- and file-wide lock until the transaction ends.

    SQLite coordinates SQL writes itself, but HealthMes needs a wider critical
    section that starts before ledger reads and spans related writes. A single
    advisory file lock on the database coordinates separate engines and
    processes; the in-process RLock also gives deterministic thread behavior.
    """

    if _SESSION_LOCK_KEY in session.info:
        return
    key, database_path = _lock_identity(session)
    with _PROCESS_LOCKS_GUARD:
        entry = _PROCESS_LOCKS.setdefault(
            key,
            {"lock": RLock(), "users": 0},
        )
        entry["users"] += 1
        process_lock = entry["lock"]
    database_handle: BinaryIO | None = None
    process_lock_acquired = False
    try:
        process_lock.acquire()
        process_lock_acquired = True
        if database_path is not None and fcntl is not None:
            database_handle = _advisory_lock_path(database_path).open("a+b")
            fcntl.flock(database_handle.fileno(), fcntl.LOCK_EX)
    except BaseException:
        if database_handle is not None:
            database_handle.close()
        if process_lock_acquired:
            process_lock.release()
        with _PROCESS_LOCKS_GUARD:
            entry = _PROCESS_LOCKS.get(key)
            if entry is not None and entry["lock"] is process_lock:
                entry["users"] -= 1
                if entry["users"] == 0:
                    _PROCESS_LOCKS.pop(key, None)
        raise
    session.info[_SESSION_LOCK_KEY] = _HeldDatabaseLock(
        key=key,
        process_lock=process_lock,
        database_handle=database_handle,
    )


def _release_local_database_lock(session: Session) -> None:
    held = session.info.pop(_SESSION_LOCK_KEY, None)
    if not isinstance(held, _HeldDatabaseLock):
        return
    if held.database_handle is not None:
        if fcntl is not None:
            fcntl.flock(held.database_handle.fileno(), fcntl.LOCK_UN)
        held.database_handle.close()
    held.process_lock.release()
    with _PROCESS_LOCKS_GUARD:
        entry = _PROCESS_LOCKS.get(held.key)
        if entry is None or entry["lock"] is not held.process_lock:
            return
        entry["users"] -= 1
        if entry["users"] == 0:
            _PROCESS_LOCKS.pop(held.key, None)


@event.listens_for(Session, "after_commit")
def _release_committed_lock(session: Session) -> None:
    if not session.in_nested_transaction():
        _release_local_database_lock(session)


@event.listens_for(Session, "after_soft_rollback")
def _release_rolled_back_lock(
    session: Session,
    previous_transaction: Any,
) -> None:
    if not previous_transaction.nested:
        _release_local_database_lock(session)


@event.listens_for(Session, "after_transaction_end")
def _release_closed_lock(session: Session, transaction: Any) -> None:
    if transaction.parent is None and not session.in_transaction():
        _release_local_database_lock(session)
