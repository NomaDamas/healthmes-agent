"""Process-local serialization for the self-hosted activity write plane."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import queue
import re
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from errno import EACCES, EAGAIN, EDEADLK
from pathlib import Path
from threading import (
    BoundedSemaphore,
    Condition,
    Lock,
    Thread,
    get_ident,
)
from time import sleep
from typing import BinaryIO

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.orm import ORMExecuteState, Session
from sqlalchemy.schema import DDLElement
from sqlalchemy.sql.elements import TextClause

from healthmes.durable_files import (
    DurabilityUnsupportedError,
    ensure_durable_directory,
    open_directory_anchored,
)
from healthmes.timing import steady_time

_ACTIVITY_WRITE_PLANE_KEY = "healthmes:activity:write-plane:v1"
_PAYLOAD_GENERATION_KEY = "healthmes:storage:payload-generation:v1"
_SQLITE_FILE_LOCK_INFO_KEY = "healthmes_activity_sqlite_file_lock"
_TRANSACTION_WRITE_LOCK_INFO_KEY = "healthmes_transaction_write_lock"
_SESSION_PROCESS_WRITE_LOCK_INFO_KEY = (
    "healthmes_session_process_write_lock"
)
_SESSION_CONNECTION_FENCE_INFO_KEY = (
    "healthmes_session_connection_write_fence"
)
_CONNECTION_WRITE_FENCE_INFO_KEY = "healthmes_connection_write_fence"
_GLOBAL_GUARD_CONNECTION_INFO_KEY = (
    "healthmes_global_write_guard_connection"
)
_POSTGRES_GUARD_TIMEOUT_SECONDS = 5.0
_GLOBAL_GUARD_TIMEOUT_SECONDS = 30.0
_POSTGRES_GUARD_POLL_SECONDS = 0.05
_POSTGRES_CONNECT_WORKER_LIMIT = 4
_POSTGRES_CONNECT_WORKER_SLOTS = BoundedSemaphore(
    _POSTGRES_CONNECT_WORKER_LIMIT
)
_SQLITE_FILE_LOCK_POLL_SECONDS = 0.05
_SECURE_LOCK_FILES_SUPPORTED = os.name != "nt"
_LOGGER = logging.getLogger(__name__)
_TEXTUAL_DML_KEYWORDS = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "REPLACE",
    }
)
_TEXTUAL_EXTENDED_MUTATION_KEYWORDS = frozenset(
    {
        "ALTER",
        "ATTACH",
        "CALL",
        "CLUSTER",
        "COMMENT",
        "COPY",
        "CREATE",
        "DETACH",
        "DO",
        "DROP",
        "GRANT",
        "REFRESH",
        "REINDEX",
        "RENAME",
        "REVOKE",
        "TRUNCATE",
        "VACUUM",
    }
)
_RUNTIME_EXTENDED_WRITE_FENCE_MARKER = (
    "_healthmes_runtime_extended_write_fence_active"
)
_RUNTIME_WRITE_FENCE_ENABLED_MARKER = (
    "_healthmes_runtime_write_fence_enabled"
)
_INTERNAL_DATABASE_CONTROL_OPTION = (
    "_healthmes_internal_database_control"
)
_DOLLAR_QUOTE_TAG = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")

type _WritePlaneIdentity = tuple[str, str]
type _ActivityWriteOwner = tuple[int, int | None]


class _LeaseLock:
    """A re-entrant process lock whose lease may be released by another thread."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._owner: object | None = None
        self._depth = 0

    def acquire(
        self,
        lease: object,
        *,
        timeout_seconds: float | None,
        deadline: float | None = None,
    ) -> bool:
        if timeout_seconds is not None and deadline is not None:
            raise ValueError("lock timeout and deadline are mutually exclusive")
        if deadline is None and timeout_seconds is not None:
            deadline = steady_time() + timeout_seconds
        with self._condition:
            if self._owner is lease:
                self._depth += 1
                return True
            while self._owner is not None:
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - steady_time()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            self._owner = lease
            self._depth = 1
            return True

    def release(self, lease: object) -> None:
        with self._condition:
            if self._owner is not lease or self._depth <= 0:
                raise RuntimeError("activity write lock lease is not held")
            self._depth -= 1
            if self._depth == 0:
                self._owner = None
                self._condition.notify_all()

    def owned_by(self, lease: object | None) -> bool:
        if lease is None:
            return False
        with self._condition:
            return self._owner is lease


@dataclass(frozen=True, slots=True, eq=False)
class _ActivityWriteLease:
    owner: _ActivityWriteOwner


@dataclass(frozen=True, slots=True, eq=False)
class _PayloadGenerationLease:
    owner: _ActivityWriteOwner


@dataclass(frozen=True, slots=True, eq=False)
class _GlobalGuardConnectionLease:
    owner: _ActivityWriteOwner


@dataclass(slots=True, eq=False)
class _GlobalWriteGuardLease:
    owner: _ActivityWriteOwner
    identity: _WritePlaneIdentity
    active: bool = True


@dataclass(frozen=True, slots=True)
class _ConnectionWriteFence:
    transaction: object
    global_guard: _GlobalWriteGuardLease | None = None


@dataclass(frozen=True, slots=True)
class _SessionWriteLease:
    identity: _WritePlaneIdentity
    lease: _ActivityWriteLease


_ACTIVITY_WRITE_LOCK = _LeaseLock()
_PAYLOAD_GENERATION_LOCK = _LeaseLock()
_CURRENT_ACTIVITY_WRITE_LEASE: ContextVar[
    _ActivityWriteLease | None
] = ContextVar(
    "healthmes_current_activity_write_lease",
    default=None,
)
_CURRENT_PAYLOAD_GENERATION_LEASE: ContextVar[
    _PayloadGenerationLease | None
] = ContextVar(
    "healthmes_current_payload_generation_lease",
    default=None,
)
_SESSION_WRITE_GUARDS_LOCK = Lock()
_SESSION_WRITE_GUARDS: dict[
    object,
    dict[_WritePlaneIdentity, int],
] = {}


def _activity_write_owner() -> _ActivityWriteOwner:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return get_ident(), id(task) if task is not None else None


def _current_or_new_activity_write_lease() -> _ActivityWriteLease:
    owner = _activity_write_owner()
    lease = _CURRENT_ACTIVITY_WRITE_LEASE.get()
    if (
        lease is None
        or lease.owner != owner
        or not _ACTIVITY_WRITE_LOCK.owned_by(lease)
    ):
        lease = _ActivityWriteLease(owner=owner)
        _CURRENT_ACTIVITY_WRITE_LEASE.set(lease)
    return lease


def _release_activity_write_lease(lease: _ActivityWriteLease) -> None:
    _ACTIVITY_WRITE_LOCK.release(lease)
    if (
        _CURRENT_ACTIVITY_WRITE_LEASE.get() is lease
        and not _ACTIVITY_WRITE_LOCK.owned_by(lease)
    ):
        _CURRENT_ACTIVITY_WRITE_LEASE.set(None)


def _current_or_new_payload_generation_lease() -> _PayloadGenerationLease:
    owner = _activity_write_owner()
    lease = _CURRENT_PAYLOAD_GENERATION_LEASE.get()
    if (
        lease is None
        or lease.owner != owner
        or not _PAYLOAD_GENERATION_LOCK.owned_by(lease)
    ):
        lease = _PayloadGenerationLease(owner=owner)
        _CURRENT_PAYLOAD_GENERATION_LEASE.set(lease)
    return lease


def _release_payload_generation_lease(
    lease: _PayloadGenerationLease,
) -> None:
    _PAYLOAD_GENERATION_LOCK.release(lease)
    if (
        _CURRENT_PAYLOAD_GENERATION_LEASE.get() is lease
        and not _PAYLOAD_GENERATION_LOCK.owned_by(lease)
    ):
        _CURRENT_PAYLOAD_GENERATION_LEASE.set(None)


def _register_session_write_guard(
    lease: _ActivityWriteLease,
    identity: _WritePlaneIdentity,
) -> None:
    with _SESSION_WRITE_GUARDS_LOCK:
        identities = _SESSION_WRITE_GUARDS.setdefault(lease, {})
        identities[identity] = identities.get(identity, 0) + 1


def _unregister_session_write_guard(
    lease: _ActivityWriteLease,
    identity: _WritePlaneIdentity,
) -> None:
    with _SESSION_WRITE_GUARDS_LOCK:
        identities = _SESSION_WRITE_GUARDS.get(lease)
        if identities is None:
            return
        count = identities.get(identity, 0)
        if count <= 1:
            identities.pop(identity, None)
        else:
            identities[identity] = count - 1
        if not identities:
            _SESSION_WRITE_GUARDS.pop(lease, None)


def _current_lease_has_session_guard(
    identity: _WritePlaneIdentity,
) -> bool:
    lease = _CURRENT_ACTIVITY_WRITE_LEASE.get()
    if (
        lease is None
        or lease.owner != _activity_write_owner()
        or not _ACTIVITY_WRITE_LOCK.owned_by(lease)
    ):
        return False
    with _SESSION_WRITE_GUARDS_LOCK:
        return _SESSION_WRITE_GUARDS.get(lease, {}).get(identity, 0) > 0


class _DatabaseControlOp(Enum):
    POSTGRES_TRY_ACTIVITY_XACT_LOCK = "postgres_try_activity_xact_lock"
    POSTGRES_ACTIVITY_XACT_LOCK = "postgres_activity_xact_lock"
    POSTGRES_TRY_ADVISORY_LOCK = "postgres_try_advisory_lock"
    POSTGRES_ADVISORY_LOCK = "postgres_advisory_lock"
    POSTGRES_ADVISORY_UNLOCK = "postgres_advisory_unlock"
    POSTGRES_TRANSACTION_READ_ONLY = "postgres_transaction_read_only"
    SQLITE_BUSY_TIMEOUT_READ = "sqlite_busy_timeout_read"
    SQLITE_BUSY_TIMEOUT_SET = "sqlite_busy_timeout_set"
    SQLITE_QUERY_ONLY_READ = "sqlite_query_only_read"
    SQLITE_QUERY_ONLY_ON = "sqlite_query_only_on"
    SQLITE_QUERY_ONLY_OFF = "sqlite_query_only_off"


@dataclass(frozen=True, slots=True)
class _DatabaseControlSpec:
    dialect: str
    sql: str
    execution_api: str
    parameter_names: frozenset[str] = frozenset()
    dynamic_integer_argument: bool = False


@dataclass(frozen=True, slots=True)
class _DatabaseControlAuthorization:
    operation: _DatabaseControlOp
    token: object
    integer_argument: int | None = None


_DATABASE_CONTROL_SPECS = {
    _DatabaseControlOp.POSTGRES_TRY_ACTIVITY_XACT_LOCK: (
        _DatabaseControlSpec(
            dialect="postgresql",
            sql=(
                "SELECT pg_try_advisory_xact_lock("
                "hashtextextended(:write_plane_key, 0))"
            ),
            execution_api="text",
            parameter_names=frozenset({"write_plane_key"}),
        )
    ),
    _DatabaseControlOp.POSTGRES_ACTIVITY_XACT_LOCK: (
        _DatabaseControlSpec(
            dialect="postgresql",
            sql=(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended(:write_plane_key, 0))"
            ),
            execution_api="text",
            parameter_names=frozenset({"write_plane_key"}),
        )
    ),
    _DatabaseControlOp.POSTGRES_TRY_ADVISORY_LOCK: (
        _DatabaseControlSpec(
            dialect="postgresql",
            sql=(
                "SELECT pg_try_advisory_lock("
                "hashtextextended(:lock_key, 0))"
            ),
            execution_api="text",
            parameter_names=frozenset({"lock_key"}),
        )
    ),
    _DatabaseControlOp.POSTGRES_ADVISORY_LOCK: _DatabaseControlSpec(
        dialect="postgresql",
        sql=(
            "SELECT pg_advisory_lock("
            "hashtextextended(:lock_key, 0))"
        ),
        execution_api="text",
        parameter_names=frozenset({"lock_key"}),
    ),
    _DatabaseControlOp.POSTGRES_ADVISORY_UNLOCK: _DatabaseControlSpec(
        dialect="postgresql",
        sql=(
            "SELECT pg_advisory_unlock("
            "hashtextextended(:lock_key, 0))"
        ),
        execution_api="text",
        parameter_names=frozenset({"lock_key"}),
    ),
    _DatabaseControlOp.POSTGRES_TRANSACTION_READ_ONLY: (
        _DatabaseControlSpec(
            dialect="postgresql",
            sql="SET TRANSACTION READ ONLY",
            execution_api="driver",
        )
    ),
    _DatabaseControlOp.SQLITE_BUSY_TIMEOUT_READ: _DatabaseControlSpec(
        dialect="sqlite",
        sql="PRAGMA busy_timeout",
        execution_api="driver",
    ),
    _DatabaseControlOp.SQLITE_BUSY_TIMEOUT_SET: _DatabaseControlSpec(
        dialect="sqlite",
        sql="PRAGMA busy_timeout={integer_argument}",
        execution_api="driver",
        dynamic_integer_argument=True,
    ),
    _DatabaseControlOp.SQLITE_QUERY_ONLY_READ: _DatabaseControlSpec(
        dialect="sqlite",
        sql="PRAGMA query_only",
        execution_api="driver",
    ),
    _DatabaseControlOp.SQLITE_QUERY_ONLY_ON: _DatabaseControlSpec(
        dialect="sqlite",
        sql="PRAGMA query_only=ON",
        execution_api="driver",
    ),
    _DatabaseControlOp.SQLITE_QUERY_ONLY_OFF: _DatabaseControlSpec(
        dialect="sqlite",
        sql="PRAGMA query_only=OFF",
        execution_api="driver",
    ),
}
_DATABASE_CONTROL_TOKENS = {
    operation: object() for operation in _DatabaseControlOp
}

_ACTIVE_GLOBAL_WRITE_GUARDS: ContextVar[
    tuple[_GlobalWriteGuardLease, ...]
] = ContextVar(
    "healthmes_active_global_write_guards",
    default=(),
)
_ACTIVE_POSTGRES_WRITE_GUARDS: ContextVar[
    tuple[
        tuple[_ActivityWriteOwner, _WritePlaneIdentity, Connection],
        ...,
    ]
] = ContextVar(
    "healthmes_active_postgres_write_guards",
    default=(),
)
_ACTIVE_SQLITE_LOCK_PARENTS: ContextVar[
    tuple[tuple[_ActivityWriteOwner, _WritePlaneIdentity, int], ...]
] = ContextVar(
    "healthmes_active_sqlite_lock_parents",
    default=(),
)
if os.name == "nt":  # pragma: no cover - exercised on Windows runners
    import msvcrt
else:
    import fcntl


@contextmanager
def activity_write_lock(
    *,
    timeout_seconds: float | None = None,
    cancellation_check: Callable[[], None] | None = None,
    poll_seconds: float = _SQLITE_FILE_LOCK_POLL_SECONDS,
) -> Iterator[None]:
    """Serialize activity ingest, retention, deletion, and summary writes."""
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise ValueError("activity write lock timeout must be positive")
    if cancellation_check is not None and poll_seconds <= 0:
        raise ValueError("activity write lock poll must be positive")

    lease = _current_or_new_activity_write_lease()
    deadline = (
        None
        if timeout_seconds is None
        else steady_time() + timeout_seconds
    )
    if cancellation_check is None:
        acquired = _ACTIVITY_WRITE_LOCK.acquire(
            lease,
            timeout_seconds=None,
            deadline=deadline,
        )
    else:
        acquired = False
        while not acquired:
            cancellation_check()
            wait_deadline = steady_time() + poll_seconds
            if deadline is not None:
                remaining = deadline - steady_time()
                if remaining <= 0:
                    break
                wait_deadline = min(wait_deadline, deadline)
            acquired = _ACTIVITY_WRITE_LOCK.acquire(
                lease,
                timeout_seconds=None,
                deadline=wait_deadline,
            )
    if not acquired:
        raise TimeoutError("timed out waiting for the process activity write lock")
    try:
        if cancellation_check is not None:
            cancellation_check()
        yield
    finally:
        _release_activity_write_lease(lease)


def _remaining_guard_time(deadline: float, *, message: str) -> float:
    remaining = deadline - steady_time()
    if remaining <= 0:
        raise TimeoutError(message)
    return remaining


@contextmanager
def _activity_write_lock_until(deadline: float) -> Iterator[None]:
    lease = _current_or_new_activity_write_lease()
    acquired = _ACTIVITY_WRITE_LOCK.acquire(
        lease,
        timeout_seconds=None,
        deadline=deadline,
    )
    if not acquired:
        raise TimeoutError(
            "timed out waiting for the process activity write lock"
        )
    try:
        yield
    finally:
        _release_activity_write_lease(lease)


@dataclass(slots=True)
class _PostgresCheckout:
    connection: Connection
    disposable_engine: Engine | None = None

    def close(self) -> None:
        try:
            self.connection.close()
        finally:
            if self.disposable_engine is not None:
                self.disposable_engine.dispose()


def _bounded_postgres_engine(
    url,
    *,
    deadline: float,
    timeout_message: str,
) -> Engine:
    remaining = _remaining_guard_time(
        deadline,
        message=timeout_message,
    )
    # libpq accepts integral seconds and treats zero as unbounded. The worker
    # deadline below remains authoritative for sub-second caller timeouts.
    connect_timeout = max(1, math.floor(remaining))
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_timeout=remaining,
        connect_args={"connect_timeout": connect_timeout},
    )


def _connect_with_bounded_worker[ConnectedResource](
    connect: Callable[[], ConnectedResource],
    *,
    close_late: Callable[[ConnectedResource], None],
    deadline: float,
    timeout_message: str,
    worker_name: str,
    clock: Callable[[], float] | None = None,
) -> ConnectedResource:
    """Bound late PostgreSQL checkouts across all HealthMes lock helpers."""
    clock = steady_time if clock is None else clock
    remaining = deadline - clock()
    if remaining <= 0:
        raise TimeoutError(timeout_message)
    if not _POSTGRES_CONNECT_WORKER_SLOTS.acquire(timeout=remaining):
        raise TimeoutError(timeout_message)
    result: queue.Queue[ConnectedResource | BaseException] = queue.Queue(
        maxsize=1
    )
    state_lock = Lock()
    cancelled = False

    def run_connect() -> None:
        try:
            try:
                candidate: ConnectedResource | BaseException = connect()
            except BaseException as exc:
                candidate = exc
            with state_lock:
                if cancelled:
                    publish = False
                else:
                    try:
                        result.put_nowait(candidate)
                    except queue.Full:
                        publish = False
                    else:
                        publish = True
            if not publish and not isinstance(candidate, BaseException):
                try:
                    close_late(candidate)
                except Exception:
                    _LOGGER.exception(
                        "failed to close a late PostgreSQL connection"
                    )
        finally:
            _POSTGRES_CONNECT_WORKER_SLOTS.release()

    worker = Thread(
        target=run_connect,
        name=worker_name,
        daemon=True,
    )
    try:
        worker.start()
    except BaseException:
        _POSTGRES_CONNECT_WORKER_SLOTS.release()
        raise
    try:
        remaining = deadline - clock()
        if remaining <= 0:
            raise queue.Empty
        candidate = result.get(
            timeout=remaining
        )
    except queue.Empty as exc:
        with state_lock:
            cancelled = True
            try:
                late = result.get_nowait()
            except queue.Empty:
                late = None
        if late is not None and not isinstance(late, BaseException):
            close_late(late)
        raise TimeoutError(timeout_message) from exc
    if isinstance(candidate, BaseException):
        raise candidate
    if clock() >= deadline:
        close_late(candidate)
        raise TimeoutError(timeout_message)
    return candidate


def _connect_postgres_before_deadline(
    engine: Engine,
    *,
    deadline: float,
    timeout_message: str,
    dispose_engine: bool,
) -> _PostgresCheckout:
    """Bound pool checkout/connect and close any connection that arrives late."""
    def connect() -> _PostgresCheckout:
        try:
            candidate = engine.connect()
        except BaseException:
            if dispose_engine:
                engine.dispose()
            raise
        return _PostgresCheckout(
            connection=candidate,
            disposable_engine=engine if dispose_engine else None,
        )

    return _connect_with_bounded_worker(
        connect,
        close_late=lambda checkout: checkout.close(),
        deadline=deadline,
        timeout_message=timeout_message,
        worker_name="healthmes-postgres-guard-connect",
    )


@contextmanager
def _postgres_guard_checkout(
    url,
    *,
    supplied_engine: Engine | None,
    supplied_connection: Connection | None,
    deadline: float,
    timeout_message: str,
) -> Iterator[Connection]:
    if supplied_connection is not None:
        yield supplied_connection
        return
    owns_engine = supplied_engine is None
    engine = (
        supplied_engine
        if supplied_engine is not None
        else _bounded_postgres_engine(
            url,
            deadline=deadline,
            timeout_message=timeout_message,
        )
    )
    checkout = _connect_postgres_before_deadline(
        engine,
        deadline=deadline,
        timeout_message=timeout_message,
        dispose_engine=owns_engine,
    )
    try:
        yield checkout.connection
    finally:
        checkout.close()


def _fail_closed_postgres_connection(
    connection: Connection,
    *,
    cause: Exception,
) -> None:
    """Ensure a possibly advisory-lock-holding physical connection is retired."""
    failures: list[Exception] = []
    try:
        connection.invalidate(cause)
    except Exception as exc:
        failures.append(exc)
    else:
        return

    pool_connection = None
    driver_connection = None
    try:
        pool_connection = connection.connection
        driver_connection = getattr(
            pool_connection,
            "driver_connection",
            None,
        )
        pool_connection.invalidate(cause)
    except Exception as exc:
        failures.append(exc)
    else:
        return

    detached = False
    try:
        connection.detach()
    except Exception as exc:
        failures.append(exc)
    else:
        detached = True

    closed = False
    try:
        connection.close()
    except Exception as exc:
        failures.append(exc)
    else:
        closed = True

    if detached and closed:
        for failure in failures:
            _LOGGER.error(
                "PostgreSQL connection discard fallback failed",
                exc_info=(
                    type(failure),
                    failure,
                    failure.__traceback__,
                ),
            )
        return

    if driver_connection is not None:
        try:
            driver_connection.close()
        except Exception as exc:
            failures.append(exc)
        else:
            return

    try:
        connection.engine.dispose()
    except Exception as exc:
        failures.append(exc)
    raise ExceptionGroup(
        "could not retire a possibly advisory-lock-holding connection",
        failures
        or [RuntimeError("no PostgreSQL connection discard path succeeded")],
    )


def _raise_postgres_advisory_cleanup_failure(
    connection: Connection,
    *,
    cause: Exception,
    context: str,
) -> None:
    try:
        _fail_closed_postgres_connection(connection, cause=cause)
    except Exception as discard_error:
        raise RuntimeError(
            f"{context}; failed to retire the PostgreSQL connection"
        ) from ExceptionGroup(
            "PostgreSQL advisory cleanup and connection retirement failed",
            [cause, discard_error],
        )
    raise RuntimeError(
        f"{context}; the PostgreSQL connection was retired"
    ) from cause


def _sqlite_lock_path(database_url: str, *, purpose: str) -> Path | None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or url.database in {
        None,
        "",
        ":memory:",
    }:
        return None
    database = Path(url.database).expanduser().resolve()
    return database.with_name(f"{database.name}.{purpose}.lock")


def _open_lock_handle(
    path: Path,
    *,
    parent_descriptor: int | None = None,
) -> BinaryIO:
    """Open one lock file without following a raced final-component symlink."""
    lock_path = Path(path).expanduser()
    if not lock_path.is_absolute():
        lock_path = Path.cwd() / lock_path
    if parent_descriptor is None:
        if os.name == "nt":  # pragma: no cover - exercised on Windows runners
            lock_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            ensure_durable_directory(lock_path.parent)
            with open_directory_anchored(lock_path.parent) as (
                _canonical,
                anchored_parent,
            ):
                return _open_lock_handle(
                    lock_path,
                    parent_descriptor=anchored_parent,
                )
    if not _SECURE_LOCK_FILES_SUPPORTED:
        raise DurabilityUnsupportedError(
            "secure descriptor-relative lock files are unavailable on Windows; "
            "run the HealthMes Personal Data Node on a supported POSIX host"
        )

    parent_fd = parent_descriptor
    assert parent_fd is not None
    descriptor: int | None = None
    try:
        descriptor = os.open(
            lock_path.name,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"lock file must be regular: {lock_path}")
        if metadata.st_uid != os.geteuid():
            raise PermissionError(f"lock file is owned by another user: {lock_path}")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "a+b")
        descriptor = None
        return handle
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_plane_identity(database_url: str) -> _WritePlaneIdentity:
    url = make_url(database_url)
    backend = url.get_backend_name()
    if backend == "sqlite" and url.database not in {None, "", ":memory:"}:
        return backend, str(Path(url.database).expanduser().resolve())
    return backend, url.render_as_string(hide_password=True)


def _sqlite_lock_parent_identity(
    database_url: str,
) -> _WritePlaneIdentity:
    url = make_url(database_url)
    database = url.database
    if url.get_backend_name() != "sqlite" or database in {
        None,
        "",
        ":memory:",
    }:
        return _write_plane_identity(database_url)
    return (
        "sqlite-lock-parent",
        str(Path(database).expanduser().absolute()),
    )


def _session_write_plane_identity(session: Session) -> _WritePlaneIdentity | None:
    bind = session.get_bind()
    engine = getattr(bind, "engine", bind)
    url = getattr(engine, "url", None)
    if url is None:
        return None
    return _write_plane_identity(url.render_as_string(hide_password=False))


def _session_is_inside_global_guard(session: Session) -> bool:
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        return isinstance(
            bind,
            Connection,
        ) and _connection_is_inside_global_guard(bind)
    identity = _session_write_plane_identity(session)
    return identity is not None and _active_global_guard_exists(identity)


def _connection_is_inside_global_guard(connection: Connection) -> bool:
    marker = connection.info.get(_GLOBAL_GUARD_CONNECTION_INFO_KEY)
    return (
        isinstance(marker, _GlobalGuardConnectionLease)
        and marker.owner == _activity_write_owner()
    )


def session_holds_write_plane(session: Session) -> bool:
    """Return whether this transaction already owns a cooperative write fence."""
    return (
        _session_is_inside_global_guard(session)
        or
        _SQLITE_FILE_LOCK_INFO_KEY in session.info
        or _SESSION_PROCESS_WRITE_LOCK_INFO_KEY in session.info
        or bool(session.info.get(_TRANSACTION_WRITE_LOCK_INFO_KEY))
    )


@contextmanager
def _active_global_guard(identity: _WritePlaneIdentity) -> Iterator[None]:
    lease = _GlobalWriteGuardLease(
        owner=_activity_write_owner(),
        identity=identity,
    )
    active = _ACTIVE_GLOBAL_WRITE_GUARDS.get()
    token = _ACTIVE_GLOBAL_WRITE_GUARDS.set(
        (*active, lease)
    )
    try:
        yield
    finally:
        lease.active = False
        _ACTIVE_GLOBAL_WRITE_GUARDS.reset(token)


def _active_global_guard_lease(
    identity: _WritePlaneIdentity,
) -> _GlobalWriteGuardLease | None:
    owner = _activity_write_owner()
    for lease in reversed(_ACTIVE_GLOBAL_WRITE_GUARDS.get()):
        if (
            lease.active
            and lease.owner == owner
            and lease.identity == identity
        ):
            return lease
    return None


def _active_global_guard_exists(identity: _WritePlaneIdentity) -> bool:
    return _active_global_guard_lease(identity) is not None


def _active_postgres_guard_connection(
    identity: _WritePlaneIdentity,
) -> Connection | None:
    owner = _activity_write_owner()
    for active_owner, active_identity, connection in reversed(
        _ACTIVE_POSTGRES_WRITE_GUARDS.get()
    ):
        if active_owner == owner and active_identity == identity:
            return connection
    return None


def _active_sqlite_lock_parent_descriptor(
    identity: _WritePlaneIdentity,
) -> int | None:
    owner = _activity_write_owner()
    for active_owner, active_identity, descriptor in reversed(
        _ACTIVE_SQLITE_LOCK_PARENTS.get()
    ):
        if active_owner == owner and active_identity == identity:
            return descriptor
    return None


@contextmanager
def anchored_sqlite_lock_parent(
    database_url: str,
    parent_descriptor: int,
) -> Iterator[None]:
    """Route SQLite lock files through a retained parent directory."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or url.database in {
        None,
        "",
        ":memory:",
    }:
        yield
        return
    identity = _sqlite_lock_parent_identity(database_url)
    owned_descriptor = os.dup(parent_descriptor)
    try:
        metadata = os.fstat(owned_descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError(
                "anchored SQLite lock parent must refer to a directory"
            )
        active = _ACTIVE_SQLITE_LOCK_PARENTS.get()
        token = _ACTIVE_SQLITE_LOCK_PARENTS.set(
            (
                *active,
                (
                    _activity_write_owner(),
                    identity,
                    owned_descriptor,
                ),
            )
        )
        try:
            yield
        finally:
            _ACTIVE_SQLITE_LOCK_PARENTS.reset(token)
    finally:
        os.close(owned_descriptor)


def _open_sqlite_lock_handle(
    database_url: str,
    *,
    purpose: str,
) -> BinaryIO | None:
    lock_path = _sqlite_lock_path(database_url, purpose=purpose)
    if lock_path is None:
        return None
    identity = _sqlite_lock_parent_identity(database_url)
    return _open_lock_handle(
        lock_path,
        parent_descriptor=_active_sqlite_lock_parent_descriptor(identity),
    )


@contextmanager
def _active_postgres_guard(
    identity: _WritePlaneIdentity,
    connection: Connection,
) -> Iterator[None]:
    active = _ACTIVE_POSTGRES_WRITE_GUARDS.get()
    token = _ACTIVE_POSTGRES_WRITE_GUARDS.set(
        (
            *active,
            (
                _activity_write_owner(),
                identity,
                connection,
            ),
        )
    )
    try:
        yield
    finally:
        _ACTIVE_POSTGRES_WRITE_GUARDS.reset(token)


@contextmanager
def sqlite_runtime_guard(
    database_url: str,
    *,
    timeout_seconds: float | None = None,
) -> Iterator[None]:
    """Keep file-backed SQLite attached to only one live HealthMes runtime.

    Restore acquires the same process-lifetime lock before replacing the
    database file. This prevents pooled connections from continuing to use
    the old inode after an otherwise atomic rename.
    """
    handle = _open_sqlite_lock_handle(
        database_url,
        purpose="runtime",
    )
    if handle is None:
        yield
        return
    locked = False
    try:
        _lock_file(
            handle,
            timeout_seconds=timeout_seconds,
            poll_seconds=_SQLITE_FILE_LOCK_POLL_SECONDS,
        )
        locked = True
        yield
    finally:
        try:
            if locked:
                _unlock_file(handle)
        finally:
            handle.close()


@contextmanager
def exclusive_file_lock(
    path: Path,
    *,
    timeout_seconds: float | None = None,
    poll_seconds: float = _SQLITE_FILE_LOCK_POLL_SECONDS,
    parent_descriptor: int | None = None,
    _deadline: float | None = None,
) -> Iterator[None]:
    """Serialize a filesystem mutation protocol across local processes."""
    lock_path = Path(path).expanduser()
    try:
        handle = _open_lock_handle(
            lock_path,
            parent_descriptor=parent_descriptor,
        )
    except FileNotFoundError:
        # Darwin can transiently report ENOENT when separate processes race
        # to create the same first-use lock entry. Re-open through the same
        # symlink-safe path validation rather than weakening the flags.
        handle = _open_lock_handle(
            lock_path,
            parent_descriptor=parent_descriptor,
        )
    locked = False
    try:
        _lock_file(
            handle,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
            _deadline=_deadline,
        )
        locked = True
        yield
    finally:
        try:
            if locked:
                _unlock_file(handle)
        finally:
            handle.close()


@contextmanager
def global_write_plane_guard(
    bind: str | Engine | Connection,
    *,
    timeout_seconds: float = _GLOBAL_GUARD_TIMEOUT_SECONDS,
) -> Iterator[Connection | None]:
    """Fence all cooperative HealthMes writers without opening the live SQLite DB.

    Backup and restore need the same cross-process boundary as request writers,
    but opening a SQLite session while replacing its database file would keep
    the old inode alive. SQLite therefore locks the shared sidecar directly;
    PostgreSQL uses the existing session advisory lock on a dedicated engine.
    """
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("global write-plane timeout must be positive")
    deadline = steady_time() + timeout_seconds
    if isinstance(bind, str):
        database_url = bind
        supplied_engine: Engine | None = None
        supplied_connection: Connection | None = None
    else:
        supplied_connection = bind if isinstance(bind, Connection) else None
        supplied_engine = (
            supplied_connection.engine
            if supplied_connection is not None
            else bind
        )
        database_url = supplied_engine.url.render_as_string(
            hide_password=False
        )
    url = make_url(database_url)
    backend = url.get_backend_name()
    identity = _write_plane_identity(database_url)
    if backend == "postgresql":
        active_connection = _active_postgres_guard_connection(identity)
        if active_connection is not None:
            yield active_connection
            return
    elif _active_global_guard_exists(identity):
        yield None
        return
    if _current_lease_has_session_guard(identity):
        raise RuntimeError(
            "global write-plane guard cannot start inside an active "
            "Session write transaction for the same database"
        )
    with _activity_write_lock_until(deadline):
        if backend == "sqlite":
            if url.database in {None, "", ":memory:"}:
                _remaining_guard_time(
                    deadline,
                    message="timed out waiting for the global write plane",
                )
                with _active_global_guard(identity):
                    yield None
                return
            handle = _open_sqlite_lock_handle(
                database_url,
                purpose="activity",
            )
            assert handle is not None
            locked = False
            try:
                _lock_file(
                    handle,
                    timeout_seconds=None,
                    poll_seconds=_SQLITE_FILE_LOCK_POLL_SECONDS,
                    _deadline=deadline,
                )
                locked = True
                with _active_global_guard(identity):
                    yield None
            finally:
                try:
                    if locked:
                        _unlock_file(handle)
                finally:
                    handle.close()
            return
        if backend == "postgresql":
            postgres_bind: str | Engine | Connection = (
                supplied_connection
                if supplied_connection is not None
                else (
                    supplied_engine
                    if supplied_engine is not None
                    else database_url
                )
            )
            with postgres_activity_write_plane_guard(
                postgres_bind,
                timeout_seconds=timeout_seconds,
                _deadline=deadline,
            ) as connection:
                assert connection is not None
                with _active_postgres_guard(identity, connection):
                    yield connection
            return
        _remaining_guard_time(
            deadline,
            message="timed out waiting for the global write plane",
        )
        with _active_global_guard(identity):
            yield None


@contextmanager
def payload_generation_guard(
    bind: str | Engine | Connection,
    *,
    timeout_seconds: float = _GLOBAL_GUARD_TIMEOUT_SECONDS,
    poll_seconds: float = _POSTGRES_GUARD_POLL_SECONDS,
) -> Iterator[Connection | None]:
    """Serialize snapshot, restore, and retention payload generations.

    Ordinary HealthMes writers intentionally do not acquire this guard. A
    generation operation acquires it before taking the shorter global write
    fence, so filesystem cleanup can continue without blocking normal ingest
    while snapshots and restores still observe one DB/files generation.
    """
    if (
        not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or not math.isfinite(poll_seconds)
        or poll_seconds <= 0
    ):
        raise ValueError("payload-generation lock bounds must be positive")
    deadline = steady_time() + timeout_seconds
    if isinstance(bind, str):
        database_url = bind
        supplied_connection: Connection | None = None
        supplied_engine: Engine | None = None
    else:
        supplied_connection = bind if isinstance(bind, Connection) else None
        supplied_engine = (
            supplied_connection.engine
            if supplied_connection is not None
            else bind
        )
        database_url = supplied_engine.url.render_as_string(
            hide_password=False
        )
    url = make_url(database_url)
    backend = url.get_backend_name()
    lease = _current_or_new_payload_generation_lease()
    acquired = _PAYLOAD_GENERATION_LOCK.acquire(
        lease,
        timeout_seconds=None,
        deadline=deadline,
    )
    if not acquired:
        raise TimeoutError(
            "timed out waiting for the process payload-generation lock"
        )
    try:
        if backend == "sqlite":
            lock_path = _sqlite_lock_path(
                database_url,
                purpose="payload-generation",
            )
            if lock_path is None:
                _remaining_guard_time(
                    deadline,
                    message="timed out waiting for payload-generation lock",
                )
                yield None
                return
            with exclusive_file_lock(
                lock_path,
                timeout_seconds=None,
                poll_seconds=poll_seconds,
                parent_descriptor=_active_sqlite_lock_parent_descriptor(
                    _sqlite_lock_parent_identity(database_url)
                ),
                _deadline=deadline,
            ):
                yield None
            return
        if backend != "postgresql":
            _remaining_guard_time(
                deadline,
                message="timed out waiting for payload-generation lock",
            )
            yield None
            return

        with _postgres_guard_checkout(
            url,
            supplied_engine=supplied_engine,
            supplied_connection=supplied_connection,
            deadline=deadline,
            timeout_message=(
                "timed out waiting for the PostgreSQL "
                "payload-generation connection"
            ),
        ) as connection:
            if connection.closed:
                raise RuntimeError(
                    "PostgreSQL payload-generation guard requires an open connection"
                )
            if connection.in_transaction():
                raise RuntimeError(
                    "PostgreSQL payload-generation guard requires a connection "
                    "without an active transaction"
                )
            original_isolation_level = connection.get_isolation_level()
            lock_attempted = False
            try:
                connection.execution_options(isolation_level="AUTOCOMMIT")
                acquired_postgres = False
                while True:
                    _remaining_guard_time(
                        deadline,
                        message="timed out waiting for the payload-generation lock",
                    )
                    lock_attempted = True
                    acquired_postgres = try_postgres_advisory_lock(
                        connection,
                        _PAYLOAD_GENERATION_KEY,
                    )
                    # A normal False result proves this attempt did not acquire
                    # the session lock. Keep cleanup armed only while the
                    # outcome is ambiguous or after a confirmed acquisition.
                    lock_attempted = acquired_postgres
                    if acquired_postgres:
                        break
                    sleep(
                        min(
                            poll_seconds,
                            _remaining_guard_time(
                                deadline,
                                message=(
                                    "timed out waiting for the "
                                    "payload-generation lock"
                                ),
                            ),
                        )
                    )
                connection.commit()
                connection.execution_options(
                    isolation_level=original_isolation_level
                )
                yield connection
            finally:
                if lock_attempted and not connection.closed:
                    try:
                        if connection.in_transaction():
                            connection.rollback()
                        connection.execution_options(
                            isolation_level="AUTOCOMMIT"
                        )
                        released = release_postgres_advisory_lock(
                            connection,
                            _PAYLOAD_GENERATION_KEY,
                        )
                        if released is not True:
                            raise RuntimeError(
                                "PostgreSQL payload-generation lock was not held"
                            )
                        connection.commit()
                    except Exception as exc:
                        _LOGGER.exception(
                            "failed to clean up PostgreSQL payload-generation guard"
                        )
                        _raise_postgres_advisory_cleanup_failure(
                            connection,
                            cause=exc,
                            context=(
                                "failed to clean up PostgreSQL "
                                "payload-generation guard"
                            ),
                        )
                    if not connection.closed and not connection.invalidated:
                        if connection.in_transaction():
                            connection.rollback()
                        connection.execution_options(
                            isolation_level=original_isolation_level
                        )
    finally:
        _release_payload_generation_lease(lease)


def _sqlite_database_path(session: Session) -> Path | None:
    bind = session.get_bind()
    engine = getattr(bind, "engine", bind)
    url = engine.url
    database = url.database
    if database in {None, "", ":memory:"}:
        return None
    return Path(database).expanduser().resolve()


def _lock_file(
    handle: BinaryIO,
    *,
    timeout_seconds: float | None,
    poll_seconds: float,
    cancellation_check: Callable[[], None] | None = None,
    _deadline: float | None = None,
) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows runners
        handle.seek(0)
        if handle.read(1) == b"":
            handle.seek(0)
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        blocking_mode = msvcrt.LK_LOCK
        nonblocking_mode = msvcrt.LK_NBLCK
    else:
        blocking_mode = fcntl.LOCK_EX
        nonblocking_mode = fcntl.LOCK_EX | fcntl.LOCK_NB

    if timeout_seconds is not None and _deadline is not None:
        raise ValueError(
            "SQLite file-lock timeout and deadline are mutually exclusive"
        )
    if (
        timeout_seconds is None
        and _deadline is None
        and cancellation_check is None
    ):
        if os.name == "nt":  # pragma: no cover - Windows runners
            msvcrt.locking(handle.fileno(), blocking_mode, 1)
        else:
            fcntl.flock(handle.fileno(), blocking_mode)
        return
    if poll_seconds <= 0 or (
        timeout_seconds is not None and timeout_seconds <= 0
    ):
        raise ValueError("SQLite file-lock bounds must be positive")

    deadline = _deadline
    if deadline is None and timeout_seconds is not None:
        deadline = steady_time() + timeout_seconds
    if deadline is not None and deadline - steady_time() <= 0:
        raise TimeoutError("timed out waiting for the SQLite file lock")
    while True:
        if cancellation_check is not None:
            cancellation_check()
        try:
            if os.name == "nt":  # pragma: no cover - Windows runners
                handle.seek(0)
                msvcrt.locking(handle.fileno(), nonblocking_mode, 1)
            else:
                fcntl.flock(handle.fileno(), nonblocking_mode)
            if cancellation_check is not None:
                try:
                    cancellation_check()
                except BaseException:
                    _unlock_file(handle)
                    raise
            return
        except OSError as exc:
            if exc.errno not in {EACCES, EAGAIN, EDEADLK}:
                raise
            if deadline is None:
                sleep(poll_seconds)
                continue
            remaining = deadline - steady_time()
            if remaining <= 0:
                raise TimeoutError(
                    "timed out waiting for the SQLite file lock"
                ) from exc
            sleep(min(poll_seconds, remaining))


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
    session.info.pop(_TRANSACTION_WRITE_LOCK_INFO_KEY, None)
    connection_fence = session.info.pop(
        _SESSION_CONNECTION_FENCE_INFO_KEY,
        None,
    )
    if connection_fence is not None:
        connection, root_transaction = connection_fence
        if not connection.closed:
            marker = connection.info.get(
                _CONNECTION_WRITE_FENCE_INFO_KEY
            )
            if _write_fence_transaction(marker) is root_transaction:
                connection.info.pop(
                    _CONNECTION_WRITE_FENCE_INFO_KEY,
                    None,
                )
    handle = session.info.pop(_SQLITE_FILE_LOCK_INFO_KEY, None)
    if handle is not None:
        try:
            _unlock_file(handle)
        finally:
            handle.close()
    session_lease = session.info.pop(
        _SESSION_PROCESS_WRITE_LOCK_INFO_KEY,
        None,
    )
    if session_lease is not None:
        assert isinstance(session_lease, _SessionWriteLease)
        _unregister_session_write_guard(
            session_lease.lease,
            session_lease.identity,
        )
        _release_activity_write_lease(session_lease.lease)


def _lock_sqlite_write_plane(
    session: Session,
    *,
    timeout_seconds: float | None,
    poll_seconds: float,
    cancellation_check: Callable[[], None] | None = None,
    _deadline: float | None = None,
) -> None:
    if _SQLITE_FILE_LOCK_INFO_KEY in session.info:
        return
    database = _sqlite_database_path(session)
    if database is None:
        return
    lock_path = database.with_name(f"{database.name}.activity.lock")
    handle = _open_lock_handle(lock_path)
    locked = False
    try:
        _lock_file(
            handle,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
            cancellation_check=cancellation_check,
            _deadline=_deadline,
        )
        locked = True
        if cancellation_check is not None:
            cancellation_check()
    except BaseException:
        try:
            if locked:
                _unlock_file(handle)
        finally:
            handle.close()
        raise
    session.info[_SQLITE_FILE_LOCK_INFO_KEY] = handle
    # Ensure transaction cleanup, session.close(), and rollback all trigger the
    # after_transaction_end release hook even if the caller fails before DML.
    try:
        session.connection()
    except BaseException:
        session.info.pop(_SQLITE_FILE_LOCK_INFO_KEY, None)
        try:
            _unlock_file(handle)
        finally:
            handle.close()
        raise


def lock_activity_write_plane(
    session: Session,
    *,
    timeout_seconds: float | None = None,
    poll_seconds: float = _SQLITE_FILE_LOCK_POLL_SECONDS,
    cancellation_check: Callable[[], None] | None = None,
) -> None:
    """Serialize PostgreSQL activity writes across processes and devices."""
    if _session_is_inside_global_guard(session):
        if cancellation_check is not None:
            cancellation_check()
        _mark_session_connection_fenced(session)
        return
    if _SESSION_PROCESS_WRITE_LOCK_INFO_KEY in session.info:
        if cancellation_check is not None:
            cancellation_check()
        return
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise ValueError("activity write lock timeout must be positive")
    if cancellation_check is not None and poll_seconds <= 0:
        raise ValueError("activity write lock poll must be positive")
    deadline = (
        None
        if timeout_seconds is None
        else steady_time() + timeout_seconds
    )
    lease = _current_or_new_activity_write_lease()
    if cancellation_check is None:
        process_acquired = _ACTIVITY_WRITE_LOCK.acquire(
            lease,
            timeout_seconds=None,
            deadline=deadline,
        )
    else:
        process_acquired = False
        while not process_acquired:
            cancellation_check()
            wait_deadline = steady_time() + poll_seconds
            if deadline is not None:
                remaining = deadline - steady_time()
                if remaining <= 0:
                    break
                wait_deadline = min(wait_deadline, deadline)
            process_acquired = _ACTIVITY_WRITE_LOCK.acquire(
                lease,
                timeout_seconds=None,
                deadline=wait_deadline,
            )
    if not process_acquired:
        raise TimeoutError(
            "timed out waiting for the process activity write lock"
        )
    identity = _session_write_plane_identity(session)
    if identity is None:
        _release_activity_write_lease(lease)
        raise RuntimeError(
            "activity write fence requires a bound database engine"
        )
    try:
        if cancellation_check is not None:
            cancellation_check()
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            _lock_sqlite_write_plane(
                session,
                timeout_seconds=(
                    timeout_seconds if deadline is None else None
                ),
                poll_seconds=poll_seconds,
                cancellation_check=cancellation_check,
                _deadline=deadline,
            )
            session.info[_TRANSACTION_WRITE_LOCK_INFO_KEY] = True
            _mark_session_connection_fenced(session)
        elif dialect != "postgresql":
            _mark_session_connection_fenced(session)
        elif timeout_seconds is not None or cancellation_check is not None:
            connection = session.connection()
            statement = _internal_control_statement(
                _DatabaseControlOp.POSTGRES_TRY_ACTIVITY_XACT_LOCK
            )
            while True:
                if cancellation_check is not None:
                    cancellation_check()
                if deadline is not None:
                    _remaining_guard_time(
                        deadline,
                        message="timed out waiting for the activity write plane",
                    )
                acquired = bool(
                    connection.scalar(
                        statement,
                        {"write_plane_key": _ACTIVITY_WRITE_PLANE_KEY},
                    )
                )
                if acquired:
                    if cancellation_check is not None:
                        cancellation_check()
                    session.info[
                        _TRANSACTION_WRITE_LOCK_INFO_KEY
                    ] = True
                    _mark_session_connection_fenced(session)
                    break
                if deadline is None:
                    sleep(poll_seconds)
                else:
                    sleep(
                        min(
                            poll_seconds,
                            _remaining_guard_time(
                                deadline,
                                message=(
                                    "timed out waiting for the activity "
                                    "write plane"
                                ),
                            ),
                        )
                    )
        else:
            session.connection().execute(
                _internal_control_statement(
                    _DatabaseControlOp.POSTGRES_ACTIVITY_XACT_LOCK
                ),
                {"write_plane_key": _ACTIVITY_WRITE_PLANE_KEY},
            )
            session.info[_TRANSACTION_WRITE_LOCK_INFO_KEY] = True
            _mark_session_connection_fenced(session)
    except BaseException:
        if session.in_transaction():
            session.rollback()
        _release_activity_write_lease(lease)
        raise
    session.info[_SESSION_PROCESS_WRITE_LOCK_INFO_KEY] = _SessionWriteLease(
        identity=identity,
        lease=lease,
    )
    _register_session_write_guard(lease, identity)


def _mark_session_connection_fenced(session: Session) -> None:
    connection = session.connection()
    root_transaction = connection.get_transaction()
    if root_transaction is None:  # pragma: no cover - autobegin invariant
        raise RuntimeError("write fence requires an active root transaction")
    identity = _session_write_plane_identity(session)
    global_guard = (
        _active_global_guard_lease(identity)
        if identity is not None
        else None
    )
    connection.info[_CONNECTION_WRITE_FENCE_INFO_KEY] = (
        _ConnectionWriteFence(
            transaction=root_transaction,
            global_guard=global_guard,
        )
    )
    session.info[_SESSION_CONNECTION_FENCE_INFO_KEY] = (
        connection,
        root_transaction,
    )


def _write_fence_transaction(marker: object) -> object | None:
    if isinstance(marker, _ConnectionWriteFence):
        return marker.transaction
    return marker


def _connection_holds_write_fence(connection: Connection) -> bool:
    if _connection_is_inside_global_guard(connection):
        return True
    transaction = connection.get_transaction()
    if transaction is None or not transaction.is_active:
        return False
    marker = connection.info.get(_CONNECTION_WRITE_FENCE_INFO_KEY)
    if _write_fence_transaction(marker) is not transaction:
        return False
    if not isinstance(marker, _ConnectionWriteFence):
        return True
    guard = marker.global_guard
    return guard is None or (
        guard.active and guard.owner == _activity_write_owner()
    )


def _textual_sql_words(statement: str) -> Iterator[str]:
    """Yield unquoted SQL words without treating comments or literals as code."""
    index = 0
    length = len(statement)
    while index < length:
        character = statement[index]
        if character.isspace():
            index += 1
            continue
        if statement.startswith("--", index):
            newline = statement.find("\n", index + 2)
            index = length if newline < 0 else newline + 1
            continue
        if statement.startswith("/*", index):
            end = statement.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        if character in {"'", '"', "`"}:
            quote = character
            index += 1
            while index < length:
                if statement[index] == "\\":
                    index += 2
                    continue
                if statement[index] == quote:
                    if index + 1 < length and statement[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        if character == "[":
            closing = statement.find("]", index + 1)
            index = length if closing < 0 else closing + 1
            continue
        if character == "$":
            match = _DOLLAR_QUOTE_TAG.match(statement, index)
            if match is not None:
                closing = statement.find(match.group(0), match.end())
                index = length if closing < 0 else closing + len(match.group(0))
                continue
        if character.isalpha() or character == "_":
            end = index + 1
            while end < length and (
                statement[end].isalnum() or statement[end] in {"_", "$"}
            ):
                end += 1
            yield statement[index:end].upper()
            index = end
            continue
        index += 1


def _textual_statement_writes(
    statement: object,
    *,
    include_extended_mutations: bool = False,
) -> bool:
    """Detect textual writes that SQLAlchemy cannot classify structurally."""
    if not isinstance(statement, TextClause):
        return False
    keywords = _TEXTUAL_DML_KEYWORDS
    if include_extended_mutations:
        keywords = keywords | _TEXTUAL_EXTENDED_MUTATION_KEYWORDS
    if any(
        word in keywords
        for word in _textual_sql_words(statement.text)
    ):
        return True
    return include_extended_mutations


def _sqlalchemy_statement_writes(
    statement: object,
    *,
    include_extended_mutations: bool = False,
) -> bool:
    return bool(
        getattr(statement, "is_insert", False)
        or getattr(statement, "is_update", False)
        or getattr(statement, "is_delete", False)
        or (
            include_extended_mutations
            and isinstance(statement, DDLElement)
        )
        or _textual_statement_writes(
            statement,
            include_extended_mutations=include_extended_mutations,
        )
    )


def _driver_sql_writes(
    statement: object,
    *,
    include_extended_mutations: bool = False,
) -> bool:
    keywords = _TEXTUAL_DML_KEYWORDS
    if include_extended_mutations:
        keywords = keywords | _TEXTUAL_EXTENDED_MUTATION_KEYWORDS
    if not isinstance(statement, str):
        return False
    if any(
        word in keywords for word in _textual_sql_words(statement)
    ):
        return True
    return include_extended_mutations


def _database_control_options(
    operation: _DatabaseControlOp,
    *,
    integer_argument: int | None = None,
) -> dict[str, _DatabaseControlAuthorization]:
    return {
        _INTERNAL_DATABASE_CONTROL_OPTION: _DatabaseControlAuthorization(
            operation=operation,
            token=_DATABASE_CONTROL_TOKENS[operation],
            integer_argument=integer_argument,
        )
    }


def _core_parameter_names(
    multiparams: object,
    params: object,
) -> frozenset[str] | None:
    if multiparams:
        if (
            not isinstance(multiparams, (tuple, list))
            or len(multiparams) != 1
            or not isinstance(multiparams[0], Mapping)
            or params
        ):
            return None
        return frozenset(str(name) for name in multiparams[0])
    if not params:
        return frozenset()
    if not isinstance(params, Mapping):
        return None
    return frozenset(str(name) for name in params)


def _driver_parameter_names(
    parameters: object,
    *,
    executemany: bool,
) -> frozenset[str] | None:
    if executemany:
        return None
    if parameters is None or parameters == () or parameters == []:
        return frozenset()
    if isinstance(parameters, Mapping):
        return frozenset(str(name) for name in parameters)
    return None


def _database_control_authorized(
    connection: Connection,
    *,
    statement: object,
    execution_api: str,
    execution_options: Mapping[str, object],
    parameter_names: frozenset[str] | None,
) -> bool:
    """Validate a closed, module-owned database control operation."""
    if _INTERNAL_DATABASE_CONTROL_OPTION not in execution_options:
        return False

    authorization = execution_options[_INTERNAL_DATABASE_CONTROL_OPTION]
    if not isinstance(
        authorization,
        _DatabaseControlAuthorization,
    ):
        raise RuntimeError("invalid internal database control authorization")
    operation = authorization.operation
    if (
        not isinstance(operation, _DatabaseControlOp)
        or authorization.token
        is not _DATABASE_CONTROL_TOKENS.get(operation)
    ):
        raise RuntimeError("invalid internal database control authorization")

    spec = _DATABASE_CONTROL_SPECS[operation]
    integer_argument = authorization.integer_argument
    if spec.dynamic_integer_argument:
        if (
            type(integer_argument) is not int
            or integer_argument < 0
            or integer_argument > 2_147_483_647
        ):
            raise RuntimeError(
                "invalid internal database control integer argument"
            )
        expected_sql = spec.sql.format(
            integer_argument=integer_argument,
        )
    else:
        if integer_argument is not None:
            raise RuntimeError(
                "invalid internal database control integer argument"
            )
        expected_sql = spec.sql
    sql = statement.text if isinstance(statement, TextClause) else statement
    statement_parameter_names = (
        frozenset(statement._bindparams)
        if isinstance(statement, TextClause)
        else frozenset()
    )
    if (
        connection.dialect.name != spec.dialect
        or execution_api != spec.execution_api
        or sql != expected_sql
        or statement_parameter_names != spec.parameter_names
        or parameter_names != spec.parameter_names
    ):
        raise RuntimeError(
            "internal database control does not match its canonical contract"
        )
    return True


def _internal_control_statement(
    operation: _DatabaseControlOp,
) -> TextClause:
    """Build one exact hard-coded TextClause database control operation."""
    spec = _DATABASE_CONTROL_SPECS[operation]
    if spec.execution_api != "text":
        raise ValueError(
            f"{operation.value} must use the driver execution API"
        )
    return text(spec.sql).execution_options(
        **_database_control_options(operation)
    )


def try_postgres_advisory_lock(
    connection: Connection,
    key: str,
) -> bool:
    """Try one session advisory lock without bypassing retained-data writes."""
    if connection.dialect.name != "postgresql":
        raise ValueError("PostgreSQL advisory locks require PostgreSQL")
    return bool(
        connection.scalar(
            _internal_control_statement(
                _DatabaseControlOp.POSTGRES_TRY_ADVISORY_LOCK
            ),
            {"lock_key": key},
        )
    )


def acquire_postgres_advisory_lock(
    connection: Connection,
    key: str,
) -> None:
    """Acquire one session advisory lock on a dedicated connection."""
    if connection.dialect.name != "postgresql":
        raise ValueError("PostgreSQL advisory locks require PostgreSQL")
    connection.execute(
        _internal_control_statement(
            _DatabaseControlOp.POSTGRES_ADVISORY_LOCK
        ),
        {"lock_key": key},
    )


def release_postgres_advisory_lock(
    connection: Connection,
    key: str,
) -> bool:
    """Release one session advisory lock from its owning connection."""
    if connection.dialect.name != "postgresql":
        raise ValueError("PostgreSQL advisory locks require PostgreSQL")
    return bool(
        connection.scalar(
            _internal_control_statement(
                _DatabaseControlOp.POSTGRES_ADVISORY_UNLOCK
            ),
            {"lock_key": key},
        )
    )


def set_sqlite_query_only(
    connection: Connection,
    *,
    enabled: bool,
) -> None:
    """Toggle SQLite's connection-local query-only barrier."""
    if connection.dialect.name != "sqlite":
        raise ValueError("SQLite query-only mode requires SQLite")
    operation = (
        _DatabaseControlOp.SQLITE_QUERY_ONLY_ON
        if enabled
        else _DatabaseControlOp.SQLITE_QUERY_ONLY_OFF
    )
    spec = _DATABASE_CONTROL_SPECS[operation]
    connection.exec_driver_sql(
        spec.sql,
        execution_options=_database_control_options(operation),
    )


def sqlite_busy_timeout_ms(connection: Connection) -> int:
    """Return SQLite's connection-local busy timeout."""
    if connection.dialect.name != "sqlite":
        raise ValueError("SQLite busy timeout requires SQLite")
    operation = _DatabaseControlOp.SQLITE_BUSY_TIMEOUT_READ
    value = connection.exec_driver_sql(
        _DATABASE_CONTROL_SPECS[operation].sql,
        execution_options=_database_control_options(operation),
    ).scalar_one()
    if type(value) is not int or value < 0:
        raise RuntimeError(f"unexpected SQLite busy timeout: {value!r}")
    return value


def set_sqlite_busy_timeout_ms(
    connection: Connection,
    timeout_ms: int,
) -> None:
    """Set SQLite's connection-local busy timeout through a closed control."""
    if connection.dialect.name != "sqlite":
        raise ValueError("SQLite busy timeout requires SQLite")
    if (
        type(timeout_ms) is not int
        or timeout_ms < 0
        or timeout_ms > 2_147_483_647
    ):
        raise ValueError("SQLite busy timeout must be a valid millisecond integer")
    operation = _DatabaseControlOp.SQLITE_BUSY_TIMEOUT_SET
    statement = _DATABASE_CONTROL_SPECS[operation].sql.format(
        integer_argument=timeout_ms,
    )
    connection.exec_driver_sql(
        statement,
        execution_options=_database_control_options(
            operation,
            integer_argument=timeout_ms,
        ),
    )


def sqlite_query_only_enabled(connection: Connection) -> bool:
    """Return SQLite's connection-local query-only state."""
    if connection.dialect.name != "sqlite":
        raise ValueError("SQLite query-only mode requires SQLite")
    operation = _DatabaseControlOp.SQLITE_QUERY_ONLY_READ
    value = connection.exec_driver_sql(
        _DATABASE_CONTROL_SPECS[operation].sql,
        execution_options=_database_control_options(operation),
    ).scalar_one()
    if value not in {0, 1, False, True}:
        raise RuntimeError(f"unexpected SQLite query-only state: {value!r}")
    return bool(value)


def set_postgres_transaction_read_only(
    connection: Connection,
) -> None:
    """Make the current PostgreSQL transaction reject retained-data writes."""
    if connection.dialect.name != "postgresql":
        raise ValueError(
            "PostgreSQL transaction read-only mode requires PostgreSQL"
        )
    operation = _DatabaseControlOp.POSTGRES_TRANSACTION_READ_ONLY
    connection.exec_driver_sql(
        _DATABASE_CONTROL_SPECS[operation].sql,
        execution_options=_database_control_options(operation),
    )


def _extended_write_fence_is_active(bind: Engine | Connection) -> bool:
    engine = bind.engine if isinstance(bind, Connection) else bind
    return bool(getattr(engine, _RUNTIME_EXTENDED_WRITE_FENCE_MARKER, False))


def _runtime_write_fence_is_disabled(
    bind: Engine | Connection,
) -> bool:
    engine = bind.engine if isinstance(bind, Connection) else bind
    return not bool(
        getattr(engine, _RUNTIME_WRITE_FENCE_ENABLED_MARKER, False)
    )


def activate_runtime_extended_write_fence(engine: Engine) -> None:
    """Fence runtime DDL, COPY, CALL, and similar mutating raw SQL.

    Engines begin with the narrower DML fence so trusted schema bootstrap can
    create an empty local database. The application activates this extended
    boundary immediately after bootstrap. Migration engines opt out of the
    runtime fence entirely.
    """
    install_engine_write_fence(engine)
    setattr(engine, _RUNTIME_EXTENDED_WRITE_FENCE_MARKER, True)


def _require_connection_write_fence(connection: Connection) -> None:
    if not _connection_holds_write_fence(connection):
        raise RuntimeError(
            "direct SQLAlchemy Core mutation requires "
            "fenced_core_transaction()"
        )


def install_engine_write_fence(engine: Engine) -> None:
    """Reject direct runtime Core DML that bypasses the Session write fence."""
    marker = "_healthmes_runtime_write_fence_installed"
    if getattr(engine, marker, False):
        return
    setattr(engine, marker, True)

    @event.listens_for(engine, "engine_connect")
    def _clear_checked_out_fence(connection: Connection) -> None:
        connection.info.pop(_CONNECTION_WRITE_FENCE_INFO_KEY, None)
        connection.info.pop(_GLOBAL_GUARD_CONNECTION_INFO_KEY, None)

    @event.listens_for(engine, "commit")
    @event.listens_for(engine, "rollback")
    def _clear_finished_transaction_fence(connection: Connection) -> None:
        if connection.closed or connection.invalidated:
            return
        connection.info.pop(_CONNECTION_WRITE_FENCE_INFO_KEY, None)

    @event.listens_for(engine, "before_execute")
    def _fence_core_execute(
        connection: Connection,
        clauseelement,
        multiparams,
        params,
        _execution_options,
    ) -> None:
        if _database_control_authorized(
            connection,
            statement=clauseelement,
            execution_api="text",
            execution_options=_execution_options,
            parameter_names=_core_parameter_names(
                multiparams,
                params,
            ),
        ):
            return
        if _sqlalchemy_statement_writes(
            clauseelement,
            include_extended_mutations=(
                _extended_write_fence_is_active(connection)
            ),
        ):
            _require_connection_write_fence(connection)

    @event.listens_for(engine, "before_cursor_execute")
    def _fence_driver_execute(
        connection: Connection,
        _cursor,
        statement,
        _parameters,
        context,
        _executemany,
    ) -> None:
        execution_options = context.execution_options
        if context.compiled is None:
            if _database_control_authorized(
                connection,
                statement=statement,
                execution_api="driver",
                execution_options=execution_options,
                parameter_names=_driver_parameter_names(
                    _parameters,
                    executemany=_executemany,
                ),
            ):
                return
            if _driver_sql_writes(
                statement,
                include_extended_mutations=(
                    _extended_write_fence_is_active(connection)
                ),
            ):
                _require_connection_write_fence(connection)


@contextmanager
def fenced_core_transaction(
    bind: Engine | Connection,
    *,
    timeout_seconds: float = _GLOBAL_GUARD_TIMEOUT_SECONDS,
) -> Iterator[Connection]:
    """Run direct Core DML on the globally fenced connection."""
    if isinstance(bind, Connection) and bind.in_transaction():
        raise RuntimeError(
            "fenced Core transaction requires a connection without an "
            "active transaction"
        )
    with global_write_plane_guard(
        bind,
        timeout_seconds=timeout_seconds,
    ) as guard_connection:
        owns_connection = False
        if guard_connection is not None:
            connection = guard_connection
        elif isinstance(bind, Connection):
            connection = bind
        else:
            connection = bind.connect()
            owns_connection = True
        try:
            if connection.in_transaction():
                raise RuntimeError(
                    "fenced Core transaction requires a clean connection"
                )
            transaction = connection.begin()
            connection.info[_CONNECTION_WRITE_FENCE_INFO_KEY] = transaction
            try:
                yield connection
            except BaseException:
                if transaction.is_active:
                    transaction.rollback()
                raise
            else:
                if not transaction.is_active:
                    raise RuntimeError(
                        "fenced Core transaction was ended by its caller"
                    )
                transaction.commit()
            finally:
                if (
                    connection.info.get(
                        _CONNECTION_WRITE_FENCE_INFO_KEY
                    )
                    is transaction
                ):
                    connection.info.pop(
                        _CONNECTION_WRITE_FENCE_INFO_KEY,
                        None,
                    )
        finally:
            if owns_connection:
                connection.close()


@event.listens_for(Session, "before_flush")
def _fence_orm_flush(
    session: Session,
    _flush_context,
    _instances,
) -> None:
    """Fence every ORM write, including domains without an explicit lock call."""
    bind = session.get_bind()
    if _runtime_write_fence_is_disabled(bind):
        return
    if session.new or session.dirty or session.deleted:
        lock_activity_write_plane(session)


@event.listens_for(Session, "do_orm_execute")
def _fence_orm_dml(execute_state: ORMExecuteState) -> None:
    """Fence SQLAlchemy bulk DML that bypasses unit-of-work flushing."""
    bind = execute_state.session.get_bind()
    if _runtime_write_fence_is_disabled(bind):
        return
    if (
        execute_state.is_insert
        or execute_state.is_update
        or execute_state.is_delete
        or _textual_statement_writes(
            execute_state.statement,
            include_extended_mutations=(
                _extended_write_fence_is_active(bind)
            ),
        )
    ):
        lock_activity_write_plane(execute_state.session)


@contextmanager
def postgres_activity_write_plane_guard(
    bind: str | Engine | Connection,
    *,
    timeout_seconds: float = _POSTGRES_GUARD_TIMEOUT_SECONDS,
    poll_seconds: float = _POSTGRES_GUARD_POLL_SECONDS,
    cancellation_check: Callable[[], None] | None = None,
    _deadline: float | None = None,
) -> Iterator[Connection | None]:
    """Hold the activity write plane before opening a serializable snapshot.

    PostgreSQL transaction-scoped advisory locks establish the transaction
    snapshot before a waiter necessarily acquires the lock. Finalization needs
    the opposite order: wait for existing activity writers, then open the
    serializable transaction that revalidates sources. A session-scoped lock
    provides that ordering and conflicts with the existing transaction-scoped
    lock because both use the same key. A supplied clean ``Connection`` is
    reused without transferring ownership; an ``Engine`` gets one temporary
    checkout.
    """

    if (
        not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or not math.isfinite(poll_seconds)
        or poll_seconds <= 0
    ):
        raise ValueError("PostgreSQL lock bounds must be positive")
    deadline = (
        _deadline
        if _deadline is not None
        else steady_time() + timeout_seconds
    )
    if isinstance(bind, str):
        url = make_url(bind)
        supplied_engine: Engine | None = None
        supplied_connection: Connection | None = None
    else:
        supplied_connection = bind if isinstance(bind, Connection) else None
        supplied_engine = (
            supplied_connection.engine
            if supplied_connection is not None
            else bind
        )
        url = supplied_engine.url
    if url.get_backend_name() != "postgresql":
        if cancellation_check is not None:
            cancellation_check()
        yield None
        return

    with _postgres_guard_checkout(
        url,
        supplied_engine=supplied_engine,
        supplied_connection=supplied_connection,
        deadline=deadline,
        timeout_message=(
            "timed out waiting for the PostgreSQL "
            "activity write-plane connection"
        ),
    ) as connection:
        if connection.closed:
            raise RuntimeError(
                "PostgreSQL write guard requires an open connection"
            )
        if connection.in_transaction():
            raise RuntimeError(
                "PostgreSQL write guard requires a connection without an "
                "active transaction"
            )
        original_isolation_level = connection.get_isolation_level()
        try:
            connection.execution_options(isolation_level="AUTOCOMMIT")
            lock_attempted = False
            guard_marker = _GlobalGuardConnectionLease(
                owner=_activity_write_owner()
            )
            try:
                acquired = False
                while True:
                    _remaining_guard_time(
                        deadline,
                        message=(
                            "timed out waiting for the activity write plane"
                        ),
                    )
                    if cancellation_check is not None:
                        cancellation_check()
                    # Cleanup starts before PostgreSQL can grant the lock. A
                    # driver or result-processing failure after server-side
                    # acquisition must not return a lock-holding connection.
                    lock_attempted = True
                    acquired = try_postgres_advisory_lock(
                        connection,
                        _ACTIVITY_WRITE_PLANE_KEY,
                    )
                    # A normal False result proves this attempt did not acquire
                    # the session lock. Keep cleanup armed only while the
                    # outcome is ambiguous or after a confirmed acquisition.
                    lock_attempted = acquired
                    if acquired:
                        if cancellation_check is not None:
                            cancellation_check()
                        break
                    sleep(
                        min(
                            poll_seconds,
                            _remaining_guard_time(
                                deadline,
                                message=(
                                    "timed out waiting for the "
                                    "activity write plane"
                                ),
                            ),
                        )
                    )
                # SQLAlchemy still opens a logical transaction around
                # AUTOCOMMIT statements. End it before changing isolation; the
                # session-scoped advisory lock survives this boundary.
                connection.commit()
                # Reuse this connection for the finalization transaction.
                connection.execution_options(isolation_level="SERIALIZABLE")
                connection.info[_GLOBAL_GUARD_CONNECTION_INFO_KEY] = (
                    guard_marker
                )
                yield connection
            finally:
                if (
                    connection.info.get(
                        _GLOBAL_GUARD_CONNECTION_INFO_KEY
                    )
                    is guard_marker
                ):
                    connection.info.pop(
                        _GLOBAL_GUARD_CONNECTION_INFO_KEY,
                        None,
                    )
                if lock_attempted:
                    try:
                        if connection.in_transaction():
                            connection.rollback()
                        connection.execution_options(
                            isolation_level="AUTOCOMMIT"
                        )
                        released = release_postgres_advisory_lock(
                            connection,
                            _ACTIVITY_WRITE_PLANE_KEY,
                        )
                        if released is not True:
                            raise RuntimeError(
                                "PostgreSQL activity write lock was not held"
                            )
                        connection.commit()
                    except Exception as exc:
                        _LOGGER.exception(
                            "failed to clean up PostgreSQL "
                            "activity write guard"
                        )
                        _raise_postgres_advisory_cleanup_failure(
                            connection,
                            cause=exc,
                            context=(
                                "failed to clean up PostgreSQL "
                                "activity write guard"
                            ),
                        )
                if not connection.closed and not connection.invalidated:
                    if connection.in_transaction():
                        connection.rollback()
                    connection.execution_options(
                        isolation_level=original_isolation_level
                    )
        except BaseException:
            if (
                not connection.closed
                and not connection.invalidated
                and connection.in_transaction()
            ):
                connection.rollback()
            raise
