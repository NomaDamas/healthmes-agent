"""Durable restore journal primitives.

The journal contains paths and state transitions only. It never stores
database URLs, credentials, passphrases, or decrypted payload contents.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from healthmes.backup.provider import BackupError
from healthmes.durable_files import require_directory_entry_durability

logger = logging.getLogger(__name__)

JOURNAL_VERSION = 3
JOURNAL_FILENAME = "pending.json"
JOURNAL_MAX_BYTES = 1024 * 1024

PHASES = frozenset(
    {
        "staging",
        "prepared",
        "applying_local",
        "local_applied",
        "postgres_in_progress",
        "manual_recovery_required",
        "rolling_back",
        "committed",
    }
)
OPERATION_STATES = frozenset(
    {"pending", "applying", "applied", "rolling_back", "rolled_back"}
)
POSTGRES_STATES = frozenset(
    {
        "pending",
        "applying",
        "committed",
        "unknown",
        "fence_unknown",
        "committed_fence_unknown",
        "unknown_fence_unknown",
    }
)
_TRANSACTION_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class _AnchoredJournalDirectory:
    path: Path
    descriptor: int


_ACTIVE_JOURNAL_DIRECTORIES: ContextVar[
    tuple[_AnchoredJournalDirectory, ...]
] = ContextVar(
    "healthmes_active_restore_journal_directories",
    default=(),
)


@dataclass(slots=True)
class JournalEntryIdentity:
    """Content-bound identity of one staged, live, or rollback generation."""

    kind: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str


@dataclass(slots=True)
class JournalOperation:
    component: str
    destination: Path
    staged: Path | None
    backup: Path
    parent_device: int | None = None
    parent_inode: int | None = None
    original_existed: bool | None = None
    state: str = "pending"
    staged_identity: JournalEntryIdentity | None = None
    rollback_identity: JournalEntryIdentity | None = None
    applied_identity: JournalEntryIdentity | None = None


@dataclass(slots=True)
class JournalPostgresTarget:
    component: str
    expected_system_identifier: str
    expected_database_oid: int
    state: str = "pending"


@dataclass(slots=True)
class RestoreJournal:
    transaction_id: str
    phase: str
    recovery_mode: str
    operations: list[JournalOperation]
    postgres_targets: list[JournalPostgresTarget]
    current_postgres: str | None = None


def _absolute_lexical(path: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return Path(os.path.abspath(candidate))


def restore_journal_path(state_dir: Path) -> Path:
    return _absolute_lexical(state_dir) / JOURNAL_FILENAME


@contextmanager
def anchored_restore_journal_directory(
    state_dir: Path,
    descriptor: int,
) -> Iterator[None]:
    """Route journal I/O through a retained no-follow directory descriptor."""
    path = _absolute_lexical(state_dir)
    owned_descriptor = os.dup(descriptor)
    try:
        metadata = os.fstat(owned_descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise BackupError(
                f"restore journal parent is not a directory: {path}"
            )
        if os.name != "nt":
            os.fchmod(owned_descriptor, 0o700)
        active = _ACTIVE_JOURNAL_DIRECTORIES.get()
        token = _ACTIVE_JOURNAL_DIRECTORIES.set(
            active
            + (
                _AnchoredJournalDirectory(
                    path=path,
                    descriptor=owned_descriptor,
                ),
            )
        )
        try:
            yield
        finally:
            _ACTIVE_JOURNAL_DIRECTORIES.reset(token)
    finally:
        os.close(owned_descriptor)


def _active_journal_descriptor(path: Path) -> int | None:
    parent = _absolute_lexical(path).parent
    for anchor in reversed(_ACTIVE_JOURNAL_DIRECTORIES.get()):
        if anchor.path == parent:
            return anchor.descriptor
    return None


def _fsync_directory(path: Path) -> None:
    require_directory_entry_durability()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_path(path: Path) -> None:
    """Persist one regular file and the directory entry that names it."""
    if path.is_file() and not path.is_symlink():
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    if path.parent.exists():
        _fsync_directory(path.parent)


def fsync_tree(path: Path) -> None:
    """Persist a staged file/tree before it can replace live data."""
    if path.is_symlink():
        _fsync_directory(path.parent)
        return
    if path.is_file():
        fsync_path(path)
        return
    directories = [path]
    for child in path.rglob("*"):
        if child.is_symlink():
            continue
        if child.is_file():
            descriptor = os.open(child, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        elif child.is_dir():
            directories.append(child)
    for directory in reversed(directories):
        _fsync_directory(directory)
    _fsync_directory(path.parent)


def _operation_payload(operation: JournalOperation) -> dict[str, Any]:
    if operation.parent_device is None or operation.parent_inode is None:
        raise BackupError(
            "restore journal operation lacks its anchored parent identity"
        )
    return {
        "component": operation.component,
        "destination": str(operation.destination),
        "staged": str(operation.staged) if operation.staged is not None else None,
        "backup": str(operation.backup),
        "parent_device": operation.parent_device,
        "parent_inode": operation.parent_inode,
        "original_existed": operation.original_existed,
        "state": operation.state,
        "staged_identity": _entry_identity_payload(
            operation.staged_identity
        ),
        "rollback_identity": _entry_identity_payload(
            operation.rollback_identity
        ),
        "applied_identity": _entry_identity_payload(
            operation.applied_identity
        ),
    }


def _entry_identity_payload(
    identity: JournalEntryIdentity | None,
) -> dict[str, Any] | None:
    if identity is None:
        return None
    return {
        "kind": identity.kind,
        "device": identity.device,
        "inode": identity.inode,
        "size": identity.size,
        "mtime_ns": identity.mtime_ns,
        "sha256": identity.sha256,
    }


def _postgres_payload(target: JournalPostgresTarget) -> dict[str, Any]:
    return {
        "component": target.component,
        "expected_system_identifier": target.expected_system_identifier,
        "expected_database_oid": target.expected_database_oid,
        "state": target.state,
    }


def write_restore_journal(path: Path, journal: RestoreJournal) -> None:
    """Atomically write and fsync the complete restore state."""
    path = _absolute_lexical(path)
    if journal.phase not in PHASES:
        raise BackupError(f"unsupported restore journal phase: {journal.phase}")
    parent_descriptor = _active_journal_descriptor(path)
    if parent_descriptor is None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise BackupError(f"restore journal is not a regular file: {path}")
        except BackupError:
            raise
        except OSError as exc:
            raise BackupError(
                f"could not prepare restore journal directory {path.parent}: {exc}"
            ) from exc
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
    else:
        try:
            metadata = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise BackupError(
                f"could not inspect restore journal {path}: {exc}"
            ) from exc
        else:
            if not stat.S_ISREG(metadata.st_mode):
                raise BackupError(f"restore journal is not a regular file: {path}")
    payload = {
        "version": JOURNAL_VERSION,
        "transaction_id": journal.transaction_id,
        "phase": journal.phase,
        "recovery_mode": journal.recovery_mode,
        "current_postgres": journal.current_postgres,
        "operations": [_operation_payload(item) for item in journal.operations],
        "postgres_targets": [_postgres_payload(item) for item in journal.postgres_targets],
    }
    temporary = path.with_name(
        f".{path.name}.{journal.transaction_id}.{uuid.uuid4().hex}.tmp"
    )
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    if len(encoded) > JOURNAL_MAX_BYTES:
        raise BackupError("restore journal exceeds the 1 MiB safety limit")
    if parent_descriptor is not None:
        temporary_descriptor: int | None = None
        try:
            temporary_descriptor = os.open(
                temporary.name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_descriptor,
            )
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(temporary_descriptor, remaining)
                if written <= 0:
                    raise OSError("restore journal write made no progress")
                remaining = remaining[written:]
            os.fsync(temporary_descriptor)
            os.close(temporary_descriptor)
            temporary_descriptor = None
            os.rename(
                temporary.name,
                path.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            os.fsync(parent_descriptor)
            return
        except BaseException as exc:
            if temporary_descriptor is not None:
                os.close(temporary_descriptor)
            try:
                os.unlink(temporary.name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning(
                    "Could not remove temporary restore journal %s",
                    temporary,
                    exc_info=True,
                )
            if isinstance(exc, BackupError):
                raise
            if isinstance(exc, OSError):
                raise BackupError(
                    f"could not persist restore journal {path}: {exc}"
                ) from exc
            raise
    try:
        with temporary.open("xb") as handle:
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # The journal is deliberately left in place when cleanup is
            # ambiguous; startup recovery can inspect it on the next run.
            logger.warning(
                "Could not remove temporary restore journal %s",
                temporary,
                exc_info=True,
            )
        if isinstance(exc, BackupError):
            raise
        if isinstance(exc, OSError):
            raise BackupError(
                f"could not persist restore journal {path}: {exc}"
            ) from exc
        raise


def remove_restore_journal(path: Path) -> None:
    path = _absolute_lexical(path)
    parent_descriptor = _active_journal_descriptor(path)
    if parent_descriptor is not None:
        try:
            try:
                os.unlink(path.name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            os.fsync(parent_descriptor)
            return
        except OSError as exc:
            raise BackupError(
                f"could not remove restore journal {path}: {exc}"
            ) from exc
    try:
        path.unlink(missing_ok=True)
        if path.parent.exists():
            _fsync_directory(path.parent)
    except OSError as exc:
        raise BackupError(
            f"could not remove restore journal {path}: {exc}"
        ) from exc


def _absolute_path(value: Any, *, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise BackupError(f"restore journal {field} must be a non-empty path")
    path = Path(value)
    if not path.is_absolute():
        raise BackupError(f"restore journal {field} must be absolute")
    return path


def _load_entry_identity(
    value: Any,
    *,
    field: str,
) -> JournalEntryIdentity | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise BackupError(f"restore journal {field} is invalid")
    kind = value.get("kind")
    numeric_fields = {
        name: value.get(name)
        for name in (
            "device",
            "inode",
            "size",
            "mtime_ns",
        )
    }
    sha256 = value.get("sha256")
    if kind not in {"file", "directory"}:
        raise BackupError(f"restore journal {field}.kind is invalid")
    if any(
        not isinstance(item, int) or isinstance(item, bool) or item < 0
        for item in numeric_fields.values()
    ):
        raise BackupError(f"restore journal {field} metadata is invalid")
    if numeric_fields["inode"] <= 0:
        raise BackupError(f"restore journal {field}.inode is invalid")
    if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
        raise BackupError(f"restore journal {field}.sha256 is invalid")
    return JournalEntryIdentity(
        kind=kind,
        device=numeric_fields["device"],
        inode=numeric_fields["inode"],
        size=numeric_fields["size"],
        mtime_ns=numeric_fields["mtime_ns"],
        sha256=sha256,
    )


def load_restore_journal(path: Path) -> RestoreJournal | None:
    path = _absolute_lexical(path)
    parent_descriptor = _active_journal_descriptor(path)
    if parent_descriptor is None:
        try:
            if path.is_symlink():
                raise BackupError(f"restore journal is not a regular file: {path}")
        except BackupError:
            raise
        except OSError as exc:
            raise BackupError(
                f"restore journal is unreadable or corrupt: {path}"
            ) from exc
    else:
        try:
            metadata = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise BackupError(
                f"restore journal is unreadable or corrupt: {path}"
            ) from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise BackupError(f"restore journal is not a regular file: {path}")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = (
            os.open(path, flags)
            if parent_descriptor is None
            else os.open(path.name, flags, dir_fd=parent_descriptor)
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BackupError(f"restore journal is unreadable or corrupt: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise BackupError(f"restore journal is not a regular file: {path}")
        if metadata.st_size > JOURNAL_MAX_BYTES:
            raise BackupError("restore journal exceeds the 1 MiB safety limit")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(JOURNAL_MAX_BYTES + 1)
        if len(raw) > JOURNAL_MAX_BYTES:
            raise BackupError("restore journal exceeds the 1 MiB safety limit")
        payload = json.loads(raw.decode("utf-8"))
    except BackupError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError(f"restore journal is unreadable or corrupt: {path}") from exc
    finally:
        try:
            os.close(descriptor)
        except OSError:
            logger.warning(
                "Could not close restore journal descriptor %s",
                path,
                exc_info=True,
            )
    if not isinstance(payload, dict):
        raise BackupError("restore journal has an unsupported format")
    version = payload.get("version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != JOURNAL_VERSION
    ):
        raise BackupError("restore journal has an unsupported format")
    transaction_id = payload.get("transaction_id")
    phase = payload.get("phase")
    recovery_mode = payload.get("recovery_mode")
    current_postgres = payload.get("current_postgres")
    if not isinstance(transaction_id, str) or not _TRANSACTION_ID.fullmatch(transaction_id):
        raise BackupError("restore journal transaction_id is invalid")
    if not isinstance(phase, str) or phase not in PHASES:
        raise BackupError("restore journal phase is invalid")
    if not isinstance(recovery_mode, str) or not recovery_mode:
        raise BackupError("restore journal recovery_mode is invalid")
    if current_postgres is not None and not isinstance(current_postgres, str):
        raise BackupError("restore journal current_postgres is invalid")

    raw_operations = payload.get("operations")
    if not isinstance(raw_operations, list):
        raise BackupError("restore journal operations are invalid")
    operations: list[JournalOperation] = []
    for index, item in enumerate(raw_operations):
        if not isinstance(item, dict):
            raise BackupError(f"restore journal operation {index} is invalid")
        component = item.get("component")
        state = item.get("state")
        original_existed = item.get("original_existed")
        parent_device = item.get("parent_device")
        parent_inode = item.get("parent_inode")
        if not isinstance(component, str) or not component:
            raise BackupError(f"restore journal operation {index} component is invalid")
        if not isinstance(state, str) or state not in OPERATION_STATES:
            raise BackupError(f"restore journal operation {index} state is invalid")
        if original_existed is not None and not isinstance(original_existed, bool):
            raise BackupError(
                f"restore journal operation {index} original_existed is invalid"
            )
        if (
            not isinstance(parent_device, int)
            or isinstance(parent_device, bool)
            or parent_device < 0
            or not isinstance(parent_inode, int)
            or isinstance(parent_inode, bool)
            or parent_inode <= 0
        ):
            raise BackupError(
                f"restore journal operation {index} parent identity is invalid"
            )
        staged_value = item.get("staged")
        staged = (
            None
            if staged_value is None
            else _absolute_path(staged_value, field=f"operations[{index}].staged")
        )
        operations.append(
            JournalOperation(
                component=component,
                destination=_absolute_path(
                    item.get("destination"),
                    field=f"operations[{index}].destination",
                ),
                staged=staged,
                backup=_absolute_path(
                    item.get("backup"),
                    field=f"operations[{index}].backup",
                ),
                parent_device=parent_device,
                parent_inode=parent_inode,
                original_existed=original_existed,
                state=state,
                staged_identity=_load_entry_identity(
                    item.get("staged_identity"),
                    field=f"operations[{index}].staged_identity",
                ),
                rollback_identity=_load_entry_identity(
                    item.get("rollback_identity"),
                    field=f"operations[{index}].rollback_identity",
                ),
                applied_identity=_load_entry_identity(
                    item.get("applied_identity"),
                    field=f"operations[{index}].applied_identity",
                ),
            )
        )

    raw_targets = payload.get("postgres_targets")
    if not isinstance(raw_targets, list):
        raise BackupError("restore journal PostgreSQL targets are invalid")
    postgres_targets: list[JournalPostgresTarget] = []
    for index, item in enumerate(raw_targets):
        if not isinstance(item, dict):
            raise BackupError(f"restore journal PostgreSQL target {index} is invalid")
        component = item.get("component")
        system_identifier = item.get("expected_system_identifier")
        database_oid = item.get("expected_database_oid")
        state = item.get("state")
        if not isinstance(component, str) or not component:
            raise BackupError(f"restore journal PostgreSQL target {index} component is invalid")
        if (
            not isinstance(system_identifier, str)
            or not system_identifier
            or not isinstance(database_oid, int)
            or isinstance(database_oid, bool)
            or database_oid <= 0
            or not isinstance(state, str)
            or state not in POSTGRES_STATES
        ):
            raise BackupError(f"restore journal PostgreSQL target {index} is invalid")
        postgres_targets.append(
            JournalPostgresTarget(
                component=component,
                expected_system_identifier=system_identifier,
                expected_database_oid=database_oid,
                state=state,
            )
        )
    if current_postgres is not None and current_postgres not in {
        target.component for target in postgres_targets
    }:
        raise BackupError("restore journal current_postgres is not a declared target")
    return RestoreJournal(
        transaction_id=transaction_id,
        phase=phase,
        recovery_mode=recovery_mode,
        operations=operations,
        postgres_targets=postgres_targets,
        current_postgres=current_postgres,
    )
