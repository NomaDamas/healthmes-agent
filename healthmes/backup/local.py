"""LocalDirectoryProvider — MVP BackupProvider writing to a local directory.

Snapshots land in ``resolve_backup_dir(settings)`` (default:
``{HEALTHMES_DATA_DIR}/backups``) as ``healthmes-backup-<UTC stamp>.tar.gz.age``
files. Listing never needs the passphrase: metadata comes from the file name
and size, keeping ``healthmes backup list`` usable on a machine that only
holds the ciphertext.

``build_backup_job`` is the zero-arg callable handed to
``healthmes.engine.scheduler.register_backup_job`` (the weekly Sunday-03:30
slot); it never raises so a misconfigured backup can never take the
scheduler thread down.
"""

import json
import logging
import os
import stat
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO

from healthmes.activity.locking import exclusive_file_lock
from healthmes.backup.filesystem import (
    PinnedPublishedFile,
    RegularFileIdentity,
    durable_atomic_writer,
    fsync_directory,
    open_directory_anchored,
    open_regular_file,
)
from healthmes.backup.limits import SnapshotResourceLimits
from healthmes.backup.provider import BackupError, SnapshotInfo
from healthmes.backup.snapshot import (
    PROVIDER_REMOTE_VAULT,
    SNAPSHOT_SUFFIX,
    DataLocations,
    RestoreResult,
    _require_disk_capacity,
    create_snapshot,
    parse_snapshot_name,
    resolve_backup_dir,
    resolve_backup_provider_name,
    resolve_data_locations,
    resolve_passphrase,
    restore_snapshot,
    snapshot_name,
)
from healthmes.config import Settings
from healthmes.durable_files import (
    durable_exclusive_writer_at,
    read_directory_batch,
)

__all__ = ["LocalDirectoryProvider", "build_backup_job"]

logger = logging.getLogger(__name__)
_EXPORT_LOCK_NAME = ".healthmes-backup-export.lock"
_SNAPSHOT_QUARANTINE_ENTRY = "payload"
_SNAPSHOT_QUARANTINE_METADATA = "metadata.json"
_SNAPSHOT_QUARANTINE_PREFIX = ".healthmes-snapshot-delete-"
_SNAPSHOT_RECOVERY_CONTROL_DIRECTORY = ".healthmes-snapshot-recovery"
_SNAPSHOT_RECOVERY_CURSOR_NAME = "quarantine-scan-v1.json"
_SNAPSHOT_RECOVERY_SOURCE_CURSOR_PREFIX = "quarantine-source-v1-"
_SNAPSHOT_RECOVERY_CURSOR_MAX_BYTES = 64 * 1024
_SNAPSHOT_RECOVERY_MAX_ENTRIES = 256
_SNAPSHOT_RECOVERY_MAX_SECONDS = 1.0
_SNAPSHOT_RECOVERY_MAX_DIRECTORY_BATCHES = 64
_SNAPSHOT_METADATA_MAX_BYTES = 16 * 1024


class _SnapshotGenerationChanged(BackupError):
    """A snapshot name no longer refers to the expected local generation."""


@dataclass(frozen=True, slots=True)
class _SnapshotQuarantine:
    path: Path
    name: str
    parent_descriptor: int | None
    descriptor: int | None


@dataclass(frozen=True, slots=True)
class _SnapshotQuarantineIntent:
    target_name: str
    expected: RegularFileIdentity


@dataclass(frozen=True, slots=True)
class _SnapshotRecoveryReport:
    scanned: int
    cleaned: int
    unresolved: int
    truncated: bool
    recovered_target: bool
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _SnapshotRecoveryCursor:
    directory_device: int
    directory_inode: int
    offset: int
    pending: tuple[str, ...]
    complete: bool


def _snapshot_quarantine_prefix(name: str) -> str:
    digest = sha256(os.fsencode(name)).hexdigest()[:20]
    return f"{_SNAPSHOT_QUARANTINE_PREFIX}{digest}-"


def _snapshot_source_recovery_cursor_name(name: str) -> str:
    digest = sha256(os.fsencode(name)).hexdigest()
    return f"{_SNAPSHOT_RECOVERY_SOURCE_CURSOR_PREFIX}{digest}.json"


def _validate_snapshot_recovery_cursor_name(name: str) -> None:
    if (
        name in {"", ".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise ValueError("invalid snapshot recovery cursor name")


@contextmanager
def _open_snapshot_parent(storage_dir: Path) -> Iterator[int | None]:
    if os.name == "nt":  # pragma: no cover - Windows runners
        yield None
        return
    with open_directory_anchored(storage_dir) as (
        _canonical,
        descriptor,
    ):
        yield descriptor


@contextmanager
def _open_snapshot_recovery_control(
    storage_dir: Path,
    parent_descriptor: int,
) -> Iterator[int]:
    created = False
    try:
        os.mkdir(
            _SNAPSHOT_RECOVERY_CONTROL_DIRECTORY,
            mode=0o700,
            dir_fd=parent_descriptor,
        )
        created = True
    except FileExistsError:
        pass
    descriptor = os.open(
        _SNAPSHOT_RECOVERY_CONTROL_DIRECTORY,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_descriptor,
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError(
                "snapshot recovery control path must be a real directory"
            )
        if metadata.st_uid != os.getuid():
            raise OSError(
                "snapshot recovery control directory must be owned by the "
                "current user"
            )
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            os.fchmod(descriptor, 0o700)
            os.fsync(descriptor)
        if created:
            os.fsync(parent_descriptor)
        yield descriptor
    finally:
        os.close(descriptor)


def _snapshot_recovery_cursor_payload(
    cursor: _SnapshotRecoveryCursor,
) -> bytes:
    return (
        json.dumps(
            {
                "version": 1,
                "directory": [
                    cursor.directory_device,
                    cursor.directory_inode,
                ],
                "offset": cursor.offset,
                "pending": list(cursor.pending),
                "complete": cursor.complete,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _parse_snapshot_recovery_cursor(
    payload: bytes,
    *,
    pending_prefix: str = _SNAPSHOT_QUARANTINE_PREFIX,
) -> _SnapshotRecoveryCursor:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid snapshot recovery cursor") from exc
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("unsupported snapshot recovery cursor")
    directory = value.get("directory")
    if (
        not isinstance(directory, list)
        or len(directory) != 2
        or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in directory
        )
    ):
        raise ValueError("invalid snapshot recovery cursor directory")
    offset = value.get("offset")
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
        or offset > 2**63 - 1
    ):
        raise ValueError("invalid snapshot recovery cursor offset")
    pending = value.get("pending")
    if (
        not isinstance(pending, list)
        or len(pending) > _SNAPSHOT_RECOVERY_MAX_ENTRIES
        or not all(
            isinstance(name, str)
            and name.startswith(pending_prefix)
            and name not in {"", ".", ".."}
            and "/" not in name
            and "\\" not in name
            and "\x00" not in name
            for name in pending
        )
    ):
        raise ValueError("invalid snapshot recovery cursor pending entries")
    complete = value.get("complete")
    if not isinstance(complete, bool):
        raise ValueError("invalid snapshot recovery cursor completion state")
    return _SnapshotRecoveryCursor(
        directory_device=directory[0],
        directory_inode=directory[1],
        offset=offset,
        pending=tuple(dict.fromkeys(pending)),
        complete=complete,
    )


def _empty_snapshot_recovery_cursor(
    parent_descriptor: int,
) -> _SnapshotRecoveryCursor:
    metadata = os.fstat(parent_descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError("snapshot recovery parent must be a real directory")
    return _SnapshotRecoveryCursor(
        directory_device=metadata.st_dev,
        directory_inode=metadata.st_ino,
        offset=0,
        pending=(),
        complete=False,
    )


def _read_snapshot_recovery_cursor(
    parent_descriptor: int,
    control_descriptor: int,
    *,
    cursor_name: str = _SNAPSHOT_RECOVERY_CURSOR_NAME,
    pending_prefix: str = _SNAPSHOT_QUARANTINE_PREFIX,
) -> _SnapshotRecoveryCursor:
    _validate_snapshot_recovery_cursor_name(cursor_name)
    current = _empty_snapshot_recovery_cursor(parent_descriptor)
    try:
        descriptor = os.open(
            cursor_name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=control_descriptor,
        )
    except FileNotFoundError:
        return current
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > _SNAPSHOT_RECOVERY_CURSOR_MAX_BYTES
        ):
            raise ValueError(
                "snapshot recovery cursor must be a small owner-only regular file"
            )
        payload = bytearray()
        while len(payload) <= _SNAPSHOT_RECOVERY_CURSOR_MAX_BYTES:
            chunk = os.read(descriptor, 16 * 1024)
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > _SNAPSHOT_RECOVERY_CURSOR_MAX_BYTES:
            raise ValueError("snapshot recovery cursor exceeds its size limit")
    finally:
        os.close(descriptor)
    cursor = _parse_snapshot_recovery_cursor(
        bytes(payload),
        pending_prefix=pending_prefix,
    )
    if (
        cursor.directory_device != current.directory_device
        or cursor.directory_inode != current.directory_inode
    ):
        return current
    return cursor


def _write_snapshot_recovery_cursor(
    control_descriptor: int,
    cursor: _SnapshotRecoveryCursor,
    *,
    cursor_name: str = _SNAPSHOT_RECOVERY_CURSOR_NAME,
) -> None:
    _validate_snapshot_recovery_cursor_name(cursor_name)
    payload = _snapshot_recovery_cursor_payload(cursor)
    if len(payload) > _SNAPSHOT_RECOVERY_CURSOR_MAX_BYTES:
        raise OSError("snapshot recovery cursor exceeds its size limit")
    temporary_name = (
        f".{cursor_name}.{uuid.uuid4().hex}.tmp"
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=control_descriptor,
        )
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("snapshot recovery cursor write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            cursor_name,
            src_dir_fd=control_descriptor,
            dst_dir_fd=control_descriptor,
        )
        os.fsync(control_descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=control_descriptor)
        except FileNotFoundError:
            pass


def _remove_snapshot_recovery_cursor(
    control_descriptor: int,
    *,
    cursor_name: str = _SNAPSHOT_RECOVERY_CURSOR_NAME,
) -> None:
    _validate_snapshot_recovery_cursor_name(cursor_name)
    try:
        os.unlink(
            cursor_name,
            dir_fd=control_descriptor,
        )
    except FileNotFoundError:
        return
    os.fsync(control_descriptor)


def _fsync_snapshot_directory(
    path: Path,
    descriptor: int | None,
) -> None:
    if descriptor is None:  # pragma: no cover - Windows
        fsync_directory(path)
        return
    os.fsync(descriptor)


def _create_snapshot_quarantine(
    storage_dir: Path,
    parent_descriptor: int | None,
    source_name: str,
    expected: RegularFileIdentity,
) -> str:
    prefix = _snapshot_quarantine_prefix(source_name)
    while True:
        name = f"{prefix}{uuid.uuid4().hex}"
        try:
            if parent_descriptor is None:  # pragma: no cover - Windows
                (storage_dir / name).mkdir(mode=0o700)
            else:
                os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            continue
        _fsync_snapshot_directory(storage_dir, parent_descriptor)
        with _open_snapshot_quarantine(
            storage_dir,
            parent_descriptor,
            name,
        ) as quarantine:
            _write_snapshot_quarantine_metadata(
                quarantine,
                source_name,
                expected,
            )
        return name


@contextmanager
def _open_snapshot_quarantine(
    storage_dir: Path,
    parent_descriptor: int | None,
    name: str,
) -> Iterator[_SnapshotQuarantine]:
    path = storage_dir / name
    if parent_descriptor is None:  # pragma: no cover - Windows
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise OSError(f"snapshot quarantine must be a real directory: {path}")
        yield _SnapshotQuarantine(
            path=path,
            name=name,
            parent_descriptor=None,
            descriptor=None,
        )
        return

    descriptor = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_descriptor,
    )
    try:
        yield _SnapshotQuarantine(
            path=path,
            name=name,
            parent_descriptor=parent_descriptor,
            descriptor=descriptor,
        )
    finally:
        os.close(descriptor)


def _snapshot_quarantine_lstat(
    quarantine: _SnapshotQuarantine,
) -> os.stat_result:
    if quarantine.descriptor is None:  # pragma: no cover - Windows
        return (quarantine.path / _SNAPSHOT_QUARANTINE_ENTRY).lstat()
    return os.stat(
        _SNAPSHOT_QUARANTINE_ENTRY,
        dir_fd=quarantine.descriptor,
        follow_symlinks=False,
    )


def _snapshot_quarantine_entry_lstat(
    quarantine: _SnapshotQuarantine,
    name: str,
) -> os.stat_result:
    if quarantine.descriptor is None:  # pragma: no cover - Windows
        return (quarantine.path / name).lstat()
    return os.stat(
        name,
        dir_fd=quarantine.descriptor,
        follow_symlinks=False,
    )


def _unlink_snapshot_quarantine_entry(
    quarantine: _SnapshotQuarantine,
) -> None:
    if quarantine.descriptor is None:  # pragma: no cover - Windows
        (quarantine.path / _SNAPSHOT_QUARANTINE_ENTRY).unlink()
        return
    os.unlink(
        _SNAPSHOT_QUARANTINE_ENTRY,
        dir_fd=quarantine.descriptor,
    )


def _unlink_snapshot_quarantine_metadata(
    quarantine: _SnapshotQuarantine,
) -> None:
    if quarantine.descriptor is None:  # pragma: no cover - Windows
        (quarantine.path / _SNAPSHOT_QUARANTINE_METADATA).unlink()
        return
    os.unlink(
        _SNAPSHOT_QUARANTINE_METADATA,
        dir_fd=quarantine.descriptor,
    )


def _snapshot_identity_payload(
    identity: RegularFileIdentity,
) -> dict[str, int]:
    return {
        "device": identity.device,
        "inode": identity.inode,
        "size": identity.size,
        "mtime_ns": identity.mtime_ns,
        "ctime_ns": identity.ctime_ns,
    }


def _snapshot_quarantine_metadata(
    source_name: str,
    expected: RegularFileIdentity,
) -> bytes:
    return (
        json.dumps(
            {
                "version": 1,
                "target_name": source_name,
                "expected": _snapshot_identity_payload(expected),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _parse_snapshot_quarantine_metadata(
    payload: bytes,
) -> _SnapshotQuarantineIntent:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid snapshot quarantine metadata") from exc
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("unsupported snapshot quarantine metadata version")
    target_name = value.get("target_name")
    if (
        not isinstance(target_name, str)
        or target_name in {"", ".", ".."}
        or "/" in target_name
        or "\\" in target_name
        or "\x00" in target_name
    ):
        raise ValueError("invalid snapshot quarantine target name")
    expected_value = value.get("expected")
    if not isinstance(expected_value, dict):
        raise ValueError("snapshot quarantine identity must be an object")
    fields: dict[str, int] = {}
    for key in ("device", "inode", "size", "mtime_ns", "ctime_ns"):
        candidate = expected_value.get(key)
        if (
            isinstance(candidate, bool)
            or not isinstance(candidate, int)
            or candidate < 0
        ):
            raise ValueError(
                f"invalid snapshot quarantine identity field: {key}"
            )
        fields[key] = candidate
    return _SnapshotQuarantineIntent(
        target_name=target_name,
        expected=RegularFileIdentity(**fields),
    )


def _read_snapshot_quarantine_metadata(
    quarantine: _SnapshotQuarantine,
) -> _SnapshotQuarantineIntent:
    if quarantine.descriptor is None:  # pragma: no cover - Windows
        metadata_path = quarantine.path / _SNAPSHOT_QUARANTINE_METADATA
        metadata = metadata_path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > _SNAPSHOT_METADATA_MAX_BYTES
        ):
            raise ValueError(
                "snapshot quarantine metadata must be a small regular file"
            )
        return _parse_snapshot_quarantine_metadata(
            metadata_path.read_bytes()
        )

    descriptor = os.open(
        _SNAPSHOT_QUARANTINE_METADATA,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=quarantine.descriptor,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > _SNAPSHOT_METADATA_MAX_BYTES
        ):
            raise ValueError(
                "snapshot quarantine metadata must be a small regular file"
            )
        payload = bytearray()
        while len(payload) <= _SNAPSHOT_METADATA_MAX_BYTES:
            chunk = os.read(descriptor, 16 * 1024)
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > _SNAPSHOT_METADATA_MAX_BYTES:
            raise ValueError(
                "snapshot quarantine metadata exceeds the size limit"
            )
    finally:
        os.close(descriptor)
    return _parse_snapshot_quarantine_metadata(bytes(payload))


def _write_snapshot_quarantine_metadata(
    quarantine: _SnapshotQuarantine,
    source_name: str,
    expected: RegularFileIdentity,
) -> None:
    payload = _snapshot_quarantine_metadata(source_name, expected)
    if quarantine.descriptor is not None:
        with durable_exclusive_writer_at(
            quarantine.descriptor,
            _SNAPSHOT_QUARANTINE_METADATA,
        ) as output:
            output.write(payload)
        return
    metadata_path = quarantine.path / _SNAPSHOT_QUARANTINE_METADATA
    with durable_atomic_writer(
        metadata_path,
        replace_existing=False,
    ) as output:
        output.write(payload)
    _fsync_snapshot_directory(quarantine.path, quarantine.descriptor)


def _quarantine_snapshot_entry(
    source_name: str,
    quarantine: _SnapshotQuarantine,
) -> None:
    """Atomically detach the currently named snapshot into private storage."""
    if quarantine.parent_descriptor is None:  # pragma: no cover - Windows
        os.rename(
            quarantine.path.parent / source_name,
            quarantine.path / _SNAPSHOT_QUARANTINE_ENTRY,
        )
        return
    os.rename(
        source_name,
        _SNAPSHOT_QUARANTINE_ENTRY,
        src_dir_fd=quarantine.parent_descriptor,
        dst_dir_fd=quarantine.descriptor,
    )


def _remove_snapshot_quarantine(
    storage_dir: Path,
    parent_descriptor: int | None,
    name: str,
) -> None:
    with _open_snapshot_quarantine(
        storage_dir,
        parent_descriptor,
        name,
    ) as quarantine:
        try:
            _unlink_snapshot_quarantine_metadata(quarantine)
        except FileNotFoundError:
            pass
        _fsync_snapshot_directory(
            quarantine.path,
            quarantine.descriptor,
        )
        entries = os.listdir(
            quarantine.path
            if quarantine.descriptor is None
            else quarantine.descriptor
        )
        if entries:
            raise OSError(
                "refusing to remove non-empty snapshot quarantine "
                f"{quarantine.path}"
            )
    if parent_descriptor is None:  # pragma: no cover - Windows
        (storage_dir / name).rmdir()
    else:
        os.rmdir(name, dir_fd=parent_descriptor)
    _fsync_snapshot_directory(storage_dir, parent_descriptor)


def _quarantined_snapshot_matches(
    expected: RegularFileIdentity,
    metadata: os.stat_result,
) -> bool:
    """Match the moved file while allowing rename-induced ctime changes."""
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_dev == expected.device
        and metadata.st_ino == expected.inode
        and metadata.st_size == expected.size
        and metadata.st_mtime_ns == expected.mtime_ns
    )


def _snapshot_intent_matches(
    expected: RegularFileIdentity,
    intent: _SnapshotQuarantineIntent,
) -> bool:
    return (
        expected.device == intent.expected.device
        and expected.inode == intent.expected.inode
        and expected.size == intent.expected.size
        and expected.mtime_ns == intent.expected.mtime_ns
    )


def _same_snapshot_object(
    first: os.stat_result,
    second: os.stat_result,
) -> bool:
    return (
        stat.S_ISREG(first.st_mode)
        and stat.S_ISREG(second.st_mode)
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
    )


def _source_snapshot_lstat(
    storage_dir: Path,
    parent_descriptor: int | None,
    source_name: str,
) -> os.stat_result:
    if parent_descriptor is None:  # pragma: no cover - Windows
        return (storage_dir / source_name).lstat()
    return os.stat(
        source_name,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )


def _restore_quarantined_snapshot(
    storage_dir: Path,
    source_name: str,
    quarantine: _SnapshotQuarantine,
) -> bool:
    """Restore without clobbering a newer snapshot, or preserve quarantine."""
    quarantined_metadata = _snapshot_quarantine_lstat(quarantine)
    try:
        current_metadata = _source_snapshot_lstat(
            storage_dir,
            quarantine.parent_descriptor,
            source_name,
        )
    except FileNotFoundError:
        current_metadata = None

    if current_metadata is not None:
        if not _same_snapshot_object(
            current_metadata,
            quarantined_metadata,
        ):
            return False
        _unlink_snapshot_quarantine_entry(quarantine)
        _fsync_snapshot_directory(
            quarantine.path,
            quarantine.descriptor,
        )
        return True

    try:
        if quarantine.parent_descriptor is None:  # pragma: no cover - Windows
            os.link(
                quarantine.path / _SNAPSHOT_QUARANTINE_ENTRY,
                storage_dir / source_name,
                follow_symlinks=False,
            )
        else:
            os.link(
                _SNAPSHOT_QUARANTINE_ENTRY,
                source_name,
                src_dir_fd=quarantine.descriptor,
                dst_dir_fd=quarantine.parent_descriptor,
                follow_symlinks=False,
            )
    except FileExistsError:
        return False
    except OSError:
        logger.warning(
            "Could not restore raced snapshot generation from %s",
            quarantine.path,
            exc_info=True,
        )
        return False

    # Persist the restored name before retiring its quarantine hard link.
    _fsync_snapshot_directory(
        storage_dir,
        quarantine.parent_descriptor,
    )
    _unlink_snapshot_quarantine_entry(quarantine)
    _fsync_snapshot_directory(
        quarantine.path,
        quarantine.descriptor,
    )
    return True


def _cleanup_snapshot_quarantine(
    storage_dir: Path,
    parent_descriptor: int | None,
    quarantine: _SnapshotQuarantine,
    *,
    payload_present: bool,
) -> None:
    if payload_present:
        _unlink_snapshot_quarantine_entry(quarantine)
        _fsync_snapshot_directory(
            quarantine.path,
            quarantine.descriptor,
        )
    _remove_snapshot_quarantine(
        storage_dir,
        parent_descriptor,
        quarantine.name,
    )


def _recover_one_snapshot_quarantine(
    storage_dir: Path,
    parent_descriptor: int | None,
    quarantine_name: str,
    *,
    requested_source_name: str | None,
    requested_expected: RegularFileIdentity | None,
) -> tuple[bool, bool, str | None]:
    """Recover one self-describing remote-only deletion intent.

    The metadata is written before the source rename, so recovery may safely
    finish the deletion even when the source name is no longer supplied by a
    caller. A newer live generation is never overwritten.
    """
    with _open_snapshot_quarantine(
        storage_dir,
        parent_descriptor,
        quarantine_name,
    ) as quarantine:
        intent = _read_snapshot_quarantine_metadata(quarantine)
        if not quarantine_name.startswith(
            _snapshot_quarantine_prefix(intent.target_name)
        ):
            raise ValueError(
                "snapshot quarantine name is not bound to its target name"
            )
        requested_matches = (
            requested_source_name == intent.target_name
            and (
                requested_expected is None
                or _snapshot_intent_matches(requested_expected, intent)
            )
        )

        try:
            payload_metadata = _snapshot_quarantine_entry_lstat(
                quarantine,
                _SNAPSHOT_QUARANTINE_ENTRY,
            )
        except FileNotFoundError:
            payload_metadata = None
        if payload_metadata is not None and not _quarantined_snapshot_matches(
            intent.expected,
            payload_metadata,
        ):
            raise ValueError(
                "snapshot quarantine payload records a different generation"
            )

        try:
            target_metadata = _source_snapshot_lstat(
                storage_dir,
                parent_descriptor,
                intent.target_name,
            )
        except FileNotFoundError:
            target_metadata = None

        if target_metadata is not None and _quarantined_snapshot_matches(
            intent.expected,
            target_metadata,
        ):
            if (
                payload_metadata is not None
                and _same_snapshot_object(
                    target_metadata,
                    payload_metadata,
                )
            ):
                if parent_descriptor is None:  # pragma: no cover - Windows
                    (storage_dir / intent.target_name).unlink()
                else:
                    os.unlink(
                        intent.target_name,
                        dir_fd=parent_descriptor,
                    )
                _fsync_snapshot_directory(storage_dir, parent_descriptor)
                target_metadata = None
            elif payload_metadata is None:
                _quarantine_snapshot_entry(
                    intent.target_name,
                    quarantine,
                )
                _fsync_snapshot_directory(
                    quarantine.path,
                    quarantine.descriptor,
                )
                _fsync_snapshot_directory(storage_dir, parent_descriptor)
                payload_metadata = _snapshot_quarantine_lstat(quarantine)
                if not _quarantined_snapshot_matches(
                    intent.expected,
                    payload_metadata,
                ):
                    raise ValueError(
                        "snapshot quarantine payload changed during recovery"
                    )
                target_metadata = None

        if payload_metadata is None:
            if target_metadata is not None:
                return False, requested_matches, (
                    "snapshot quarantine target changed; preserving the "
                    f"current generation at {storage_dir / intent.target_name}"
                )
            _cleanup_snapshot_quarantine(
                storage_dir,
                parent_descriptor,
                quarantine,
                payload_present=False,
            )
            return True, requested_matches, None

        # The payload is the exact generation whose remote upload was already
        # verified. It may be deleted without touching a newer live name.
        _cleanup_snapshot_quarantine(
            storage_dir,
            parent_descriptor,
            quarantine,
            payload_present=True,
        )
        return True, requested_matches, None


def _recover_snapshot_quarantines(
    storage_dir: Path,
    parent_descriptor: int | None,
    source_name: str | None = None,
    expected: RegularFileIdentity | None = None,
    *,
    max_entries: int = _SNAPSHOT_RECOVERY_MAX_ENTRIES,
    max_seconds: float = _SNAPSHOT_RECOVERY_MAX_SECONDS,
) -> _SnapshotRecoveryReport:
    """Boundedly discover and recover all self-describing quarantines.

    Global scans persist an opaque directory cursor in a separate control
    directory. Ordinary snapshot files therefore neither consume the recovery
    entry budget nor force every invocation to restart at the first entry.
    """
    if max_entries <= 0:
        raise ValueError("max_entries must be positive")
    if max_seconds <= 0:
        raise ValueError("max_seconds must be positive")
    if os.name == "nt":  # pragma: no cover - Windows runners
        return _SnapshotRecoveryReport(0, 0, 0, False, False, ())

    deadline = time.monotonic() + max_seconds
    scanned = cleaned = unresolved = 0
    recovered_target = False
    truncated = False
    errors: list[str] = []

    def recover_name(name: str) -> None:
        nonlocal cleaned, recovered_target, unresolved
        path = storage_dir / name
        try:
            metadata = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError("snapshot quarantine symlink preserved")
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError(
                    "snapshot quarantine must be a real directory"
                )
            cleaned_one, requested, error = (
                _recover_one_snapshot_quarantine(
                    storage_dir,
                    parent_descriptor,
                    name,
                    requested_source_name=source_name,
                    requested_expected=expected,
                )
            )
            cleaned += int(cleaned_one)
            recovered_target = recovered_target or (
                cleaned_one and requested
            )
            if error is not None:
                unresolved += 1
                errors.append(f"{path}: {error}")
        except FileNotFoundError as exc:
            try:
                os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            except OSError as inspect_exc:
                unresolved += 1
                errors.append(f"{path}: {inspect_exc}")
            else:
                unresolved += 1
                errors.append(
                    f"{path}: incomplete snapshot quarantine: {exc}"
                )
        except (OSError, ValueError) as exc:
            unresolved += 1
            errors.append(f"{path}: {exc}")

    assert parent_descriptor is not None
    cursor_name = (
        _SNAPSHOT_RECOVERY_CURSOR_NAME
        if source_name is None
        else _snapshot_source_recovery_cursor_name(source_name)
    )
    scope_prefix = (
        _SNAPSHOT_QUARANTINE_PREFIX
        if source_name is None
        else _snapshot_quarantine_prefix(source_name)
    )
    try:
        with _open_snapshot_recovery_control(
            storage_dir,
            parent_descriptor,
        ) as control_descriptor:
            try:
                cursor = _read_snapshot_recovery_cursor(
                    parent_descriptor,
                    control_descriptor,
                    cursor_name=cursor_name,
                    pending_prefix=scope_prefix,
                )
            except (OSError, ValueError) as exc:
                errors.append(f"invalid snapshot recovery cursor reset: {exc}")
                unresolved += 1
                cursor = _empty_snapshot_recovery_cursor(parent_descriptor)
                _write_snapshot_recovery_cursor(
                    control_descriptor,
                    cursor,
                    cursor_name=cursor_name,
                )

            attempted_names: set[str] = set()
            directory_batches = 0
            while True:
                if scanned >= max_entries or time.monotonic() >= deadline:
                    truncated = bool(
                        cursor.pending or not cursor.complete
                    )
                    break

                if not cursor.pending:
                    if cursor.complete:
                        _remove_snapshot_recovery_cursor(
                            control_descriptor,
                            cursor_name=cursor_name,
                        )
                        break
                    if (
                        directory_batches
                        >= _SNAPSHOT_RECOVERY_MAX_DIRECTORY_BATCHES
                    ):
                        truncated = True
                        break
                    try:
                        names, next_offset, complete = read_directory_batch(
                            parent_descriptor,
                            cursor.offset,
                        )
                    except (OSError, ValueError) as exc:
                        unresolved += 1
                        errors.append(
                            "snapshot recovery directory cursor reset after "
                            f"a scan failure: {exc}"
                        )
                        cursor = _empty_snapshot_recovery_cursor(
                            parent_descriptor
                        )
                        _write_snapshot_recovery_cursor(
                            control_descriptor,
                            cursor,
                            cursor_name=cursor_name,
                        )
                        truncated = True
                        break
                    directory_batches += 1
                    cursor = replace(
                        cursor,
                        offset=next_offset,
                        pending=tuple(
                            name
                            for name in names
                            if name.startswith(scope_prefix)
                            and name not in attempted_names
                        ),
                        complete=complete,
                    )
                    _write_snapshot_recovery_cursor(
                        control_descriptor,
                        cursor,
                        cursor_name=cursor_name,
                    )
                    if not cursor.pending:
                        continue

                while cursor.pending:
                    if (
                        scanned >= max_entries
                        or time.monotonic() >= deadline
                    ):
                        break
                    name = cursor.pending[0]
                    cursor = replace(
                        cursor,
                        pending=cursor.pending[1:],
                    )
                    # Checkpoint before touching a potentially malformed
                    # quarantine so one bad entry cannot pin every later one.
                    _write_snapshot_recovery_cursor(
                        control_descriptor,
                        cursor,
                        cursor_name=cursor_name,
                    )
                    scanned += 1
                    attempted_names.add(name)
                    recover_name(name)
                    if source_name is not None and recovered_target:
                        _remove_snapshot_recovery_cursor(
                            control_descriptor,
                            cursor_name=cursor_name,
                        )
                        break
                if source_name is not None and recovered_target:
                    break
                if cursor.complete and not cursor.pending:
                    _remove_snapshot_recovery_cursor(
                        control_descriptor,
                        cursor_name=cursor_name,
                    )
                    break
                if cursor.pending:
                    truncated = True
                    break
    except (OSError, ValueError) as exc:
        unresolved += 1
        truncated = True
        errors.append(
            f"could not persist snapshot quarantine scan progress: {exc}"
        )
    return _SnapshotRecoveryReport(
        scanned,
        cleaned,
        unresolved,
        truncated,
        recovered_target,
        tuple(errors),
    )


def _log_snapshot_recovery_report(
    report: _SnapshotRecoveryReport,
    *,
    operation: str,
) -> None:
    if report.truncated:
        logger.warning(
            "Snapshot quarantine recovery was bounded during %s "
            "(scanned=%d, cleaned=%d, unresolved=%d)",
            operation,
            report.scanned,
            report.cleaned,
            report.unresolved,
        )
    if report.errors:
        logger.warning(
            "Snapshot quarantine recovery left %d entries unresolved during %s",
            len(report.errors),
            operation,
        )
        for error in report.errors:
            logger.warning("Snapshot quarantine recovery detail: %s", error)


def _recover_snapshot_quarantines_at_boundary(
    storage_dir: Path,
    *,
    operation: str,
) -> None:
    """Run bounded global recovery under the provider export lock."""
    if not storage_dir.is_dir():
        return
    try:
        with exclusive_file_lock(storage_dir / _EXPORT_LOCK_NAME):
            with _open_snapshot_parent(storage_dir) as parent_descriptor:
                report = _recover_snapshot_quarantines(
                    storage_dir,
                    parent_descriptor,
                )
    except OSError as exc:
        logger.warning(
            "Snapshot quarantine recovery could not run during %s: %s",
            operation,
            exc,
        )
        return
    _log_snapshot_recovery_report(report, operation=operation)


def _publish_staged_snapshot(
    staged_path: Path,
    destination: Path,
    *,
    limits: SnapshotResourceLimits,
    pinned: PinnedPublishedFile | None = None,
) -> None:
    """Durably publish one staged ciphertext without replacing any entry."""
    with open_regular_file(staged_path) as source:
        _publish_open_snapshot(
            source,
            destination,
            limits=limits,
            pinned=pinned,
        )


def _publish_open_snapshot(
    source: BinaryIO,
    destination: Path,
    *,
    limits: SnapshotResourceLimits,
    pinned: PinnedPublishedFile | None = None,
) -> RegularFileIdentity:
    """Publish the exact regular-file generation held by ``source``."""
    expected = RegularFileIdentity.from_descriptor(source.fileno())
    source.seek(0)
    copied = 0
    try:
        _require_disk_capacity(
            destination.parent,
            payload_bytes=expected.size,
            limits=limits,
            label="final local backup publication",
        )
        with durable_atomic_writer(
            destination,
            replace_existing=False,
            pinned=pinned,
        ) as output:
            while chunk := source.read(1024 * 1024):
                copied += len(chunk)
                if copied > expected.size:
                    raise BackupError(
                        "sealed snapshot generation grew while it was being "
                        "published"
                    )
                output.write(chunk)
            if (
                copied != expected.size
                or RegularFileIdentity.from_descriptor(source.fileno())
                != expected
            ):
                raise BackupError(
                    "sealed snapshot generation changed while it was being "
                    "published"
                )
    finally:
        source.seek(0)
    if pinned is not None:
        if pinned.identity is None:
            raise BackupError(
                "published snapshot generation was not retained"
            )
        return pinned.identity
    return expected


def _link_relocation_hold(
    candidate: Path,
    hold: Path,
    *,
    parent_descriptor: int | None,
) -> None:
    """Keep a second durable name for a relocation candidate."""
    if parent_descriptor is None:  # pragma: no cover - Windows
        os.link(candidate, hold, follow_symlinks=False)
        fsync_directory(hold.parent)
        return
    os.link(
        candidate.name,
        hold.name,
        src_dir_fd=parent_descriptor,
        dst_dir_fd=parent_descriptor,
        follow_symlinks=False,
    )
    os.fsync(parent_descriptor)


def _named_regular_file_identity(
    path: Path,
    *,
    label: str,
) -> RegularFileIdentity | None:
    """Read one named regular-file identity without hiding I/O failures."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BackupError(f"could not inspect {label} {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        return None
    return RegularFileIdentity.from_metadata(metadata)


def _same_relocation_payload(
    expected: RegularFileIdentity,
    observed: RegularFileIdentity,
) -> bool:
    """Match a published payload while allowing hard-link ctime changes."""
    return (
        observed.device == expected.device
        and observed.inode == expected.inode
        and observed.size == expected.size
        and observed.mtime_ns == expected.mtime_ns
    )


def _seal_open_snapshot(
    source: BinaryIO,
    sealed: BinaryIO,
    *,
    expected: RegularFileIdentity | None = None,
) -> RegularFileIdentity:
    """Copy one open generation into an anonymous, durable sealed handle."""
    opened_identity = RegularFileIdentity.from_descriptor(source.fileno())
    if expected is not None and opened_identity != expected:
        raise BackupError(
            "snapshot generation changed before it could be sealed"
        )
    expected = opened_identity
    source.seek(0)
    sealed.seek(0)
    sealed.truncate()
    copied = 0
    try:
        while chunk := source.read(1024 * 1024):
            copied += len(chunk)
            if copied > expected.size:
                raise BackupError(
                    "snapshot generation grew while it was being sealed"
                )
            sealed.write(chunk)
        sealed.flush()
        os.fsync(sealed.fileno())
        if (
            copied != expected.size
            or RegularFileIdentity.from_descriptor(source.fileno())
            != expected
        ):
            raise BackupError(
                "snapshot generation changed while it was being sealed"
            )
        sealed.seek(0)
        return expected
    finally:
        source.seek(0)


class LocalDirectoryProvider:
    """Store age-encrypted snapshot envelopes in a local directory.

    Implements the :class:`healthmes.backup.provider.BackupProvider`
    protocol. ``clock`` is injectable so tests (and callers that must align
    names with external timestamps) control the creation instant; snapshots
    themselves only ever receive caller-injected timestamps.
    """

    def __init__(
        self,
        backup_dir: Path,
        *,
        locations: DataLocations,
        passphrase: str | None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._backup_dir = Path(backup_dir)
        self._locations = locations
        self._passphrase = passphrase
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        _recover_snapshot_quarantines_at_boundary(
            self._storage_dir(),
            operation="provider startup",
        )

    @classmethod
    def from_settings(
        cls, settings: Settings, *, passphrase: str | None = None
    ) -> "LocalDirectoryProvider":
        """Build the provider from Settings (target dir, locations, passphrase).

        ``passphrase`` overrides the Settings/env resolution — the CLI uses
        it for ``--passphrase-file``.
        """
        return cls(
            resolve_backup_dir(settings),
            locations=resolve_data_locations(settings),
            passphrase=passphrase if passphrase is not None else resolve_passphrase(settings),
        )

    @property
    def backup_dir(self) -> Path:
        return self._backup_dir

    @property
    def resource_limits(self) -> SnapshotResourceLimits:
        """Limits applied to snapshots handled by this provider."""
        return self._locations.resource_limits

    def _require_passphrase(self) -> str:
        if not self._passphrase:
            raise BackupError(
                "no backup passphrase configured; set HEALTHMES_BACKUP_PASSPHRASE "
                "(losing it makes every snapshot unrecoverable)"
            )
        return self._passphrase

    def _unique_out_path(self, created_at: datetime) -> Path:
        """Snapshot path for ``created_at``, deduplicated on same-second runs."""
        return self._unique_out_path_from(created_at, minimum_counter=1)

    def _storage_dir(self) -> Path:
        """Canonical write directory; configured directory symlinks remain valid."""
        return self._backup_dir.expanduser().resolve(strict=False)

    def _unique_out_path_from(
        self,
        created_at: datetime,
        *,
        minimum_counter: int,
    ) -> Path:
        if minimum_counter < 1:
            raise ValueError("snapshot collision counter must be positive")
        base = snapshot_name(created_at)
        backup_dir = self._storage_dir()
        counter = minimum_counter
        candidate = (
            backup_dir / base
            if counter == 1
            else backup_dir
            / f"{base[: -len(SNAPSHOT_SUFFIX)]}-{counter}{SNAPSHOT_SUFFIX}"
        )
        counter = max(2, counter + 1)
        while True:
            try:
                candidate.lstat()
            except FileNotFoundError:
                return candidate
            stem = base[: -len(SNAPSHOT_SUFFIX)]
            candidate = backup_dir / f"{stem}-{counter}{SNAPSHOT_SUFFIX}"
            counter += 1

    def export_snapshot(
        self,
        *,
        _pinned: PinnedPublishedFile | None = None,
    ) -> SnapshotInfo:
        """Create one encrypted snapshot of the live data in the backup dir."""
        passphrase = self._require_passphrase()
        storage_dir = self._storage_dir()
        try:
            storage_dir.mkdir(parents=True, exist_ok=True)
            with exclusive_file_lock(storage_dir / _EXPORT_LOCK_NAME):
                with _open_snapshot_parent(storage_dir) as parent_descriptor:
                    report = _recover_snapshot_quarantines(
                        storage_dir,
                        parent_descriptor,
                    )
                _log_snapshot_recovery_report(report, operation="export")
                created_at = self._clock()
                with tempfile.TemporaryDirectory(
                    prefix="healthmes-backup-export-"
                ) as temporary_dir:
                    staged_path = Path(temporary_dir) / "snapshot.tar.gz.age"
                    create_snapshot(
                        self._locations,
                        passphrase=passphrase,
                        out_path=staged_path,
                        created_at=created_at,
                    )
                    while True:
                        out_path = self._unique_out_path(created_at)
                        publish_pin = _pinned or PinnedPublishedFile()
                        try:
                            _publish_staged_snapshot(
                                staged_path,
                                out_path,
                                limits=self.resource_limits,
                                pinned=publish_pin,
                            )
                        except FileExistsError:
                            if _pinned is None:
                                publish_pin.close()
                            logger.info(
                                "Snapshot name appeared during publish; retrying "
                                "with the next collision suffix: %s",
                                out_path,
                            )
                            continue
                        try:
                            if publish_pin.identity is None:
                                raise BackupError(
                                    "published snapshot generation was not retained"
                                )
                            return SnapshotInfo(
                                name=out_path.name,
                                path=out_path,
                                created_at=created_at,
                                size_bytes=publish_pin.identity.size,
                            )
                        finally:
                            if _pinned is None:
                                publish_pin.close()
        except BackupError:
            raise
        except OSError as exc:
            raise BackupError(
                f"could not write encrypted snapshot in {self._backup_dir}: {exc}"
            ) from exc

    def relocate_snapshot_after_collision(
        self,
        info: SnapshotInfo,
        *,
        minimum_counter: int,
    ) -> SnapshotInfo:
        """Republish a sealed local snapshot under an unused collision suffix."""
        source, selected_identity = self._resolve_snapshot_generation(
            info.path
        )
        try:
            with open_regular_file(source) as opened:
                opened_identity = RegularFileIdentity.from_descriptor(
                    opened.fileno()
                )
                if opened_identity != selected_identity:
                    raise BackupError(
                        "colliding snapshot changed before it could be sealed: "
                        f"{source}"
                    )
                max_bytes = self.resource_limits.max_encrypted_bytes
                if opened_identity.size > max_bytes:
                    raise BackupError(
                        "snapshot exceeds the configured "
                        f"{max_bytes}-byte encrypted limit"
                    )
                _require_disk_capacity(
                    Path(tempfile.gettempdir()),
                    payload_bytes=opened_identity.size,
                    limits=self.resource_limits,
                    label="collision relocation sealed generation",
                )
                with tempfile.TemporaryFile(mode="w+b") as sealed:
                    _seal_open_snapshot(
                        opened,
                        sealed,
                        expected=opened_identity,
                    )
                    relocated, _identity = (
                        self._relocate_sealed_snapshot_after_collision(
                            info,
                            minimum_counter=minimum_counter,
                            sealed=sealed,
                            expected=opened_identity,
                        )
                    )
                    return relocated
        except BackupError:
            raise
        except OSError as exc:
            raise BackupError(
                f"could not relocate colliding snapshot {source}: {exc}"
            ) from exc

    def _relocate_sealed_snapshot_after_collision(
        self,
        info: SnapshotInfo,
        *,
        minimum_counter: int,
        sealed: BinaryIO,
        expected: RegularFileIdentity,
    ) -> tuple[SnapshotInfo, RegularFileIdentity]:
        """Publish ``sealed`` first, then safely retire only ``expected``."""
        if minimum_counter < 1:
            raise ValueError("snapshot collision counter must be positive")
        sealed_identity = RegularFileIdentity.from_descriptor(
            sealed.fileno()
        )
        if sealed_identity.size != expected.size:
            raise BackupError(
                "sealed collision generation size does not match its local "
                "snapshot identity"
            )
        storage_dir = self._storage_dir()
        source = Path(info.path)
        if source.name in {"", ".", ".."}:
            raise BackupError(
                "refusing to relocate a snapshot outside the configured backup directory"
            )
        try:
            source_parent = source.parent.resolve(strict=True)
        except OSError as exc:
            raise BackupError(
                f"could not verify colliding snapshot parent {source}: {exc}"
            ) from exc
        source_retirement_done = False
        source_retirement_error: BackupError | None = None
        counter = minimum_counter
        while True:
            published = PinnedPublishedFile()
            try:
                try:
                    with open_directory_anchored(storage_dir) as (
                        canonical_storage_dir,
                        parent_descriptor,
                    ):
                        if source_parent != canonical_storage_dir:
                            raise BackupError(
                                "refusing to relocate a snapshot outside the configured "
                                "backup directory"
                            )
                        with exclusive_file_lock(
                            canonical_storage_dir / _EXPORT_LOCK_NAME,
                            parent_descriptor=parent_descriptor,
                        ):
                            opened_parent = os.fstat(parent_descriptor)
                            named_parent = os.stat(
                                storage_dir,
                                follow_symlinks=True,
                            )
                            if (
                                not stat.S_ISDIR(named_parent.st_mode)
                                or named_parent.st_dev != opened_parent.st_dev
                                or named_parent.st_ino != opened_parent.st_ino
                            ):
                                raise BackupError(
                                    "backup directory changed while relocating the "
                                    "colliding snapshot"
                                )
                            base = snapshot_name(info.created_at)
                            stem = base[: -len(SNAPSHOT_SUFFIX)]
                            candidate_name = (
                                base
                                if counter == 1
                                else f"{stem}-{counter}{SNAPSHOT_SUFFIX}"
                            )
                            candidate = (
                                canonical_storage_dir / candidate_name
                            )
                            try:
                                published_identity = _publish_open_snapshot(
                                    sealed,
                                    candidate,
                                    limits=self.resource_limits,
                                    pinned=published,
                                )
                            except FileExistsError:
                                counter = max(2, counter + 1)
                                continue
                            hold_counter = max(2, counter + 1)
                            while True:
                                hold_name = (
                                    f"{stem}-{hold_counter}{SNAPSHOT_SUFFIX}"
                                )
                                hold = (
                                    canonical_storage_dir / hold_name
                                )
                                try:
                                    hold.lstat()
                                except FileNotFoundError:
                                    try:
                                        _link_relocation_hold(
                                            candidate,
                                            hold,
                                            parent_descriptor=parent_descriptor,
                                        )
                                    except FileExistsError:
                                        hold_counter += 1
                                        continue
                                    break
                                hold_counter += 1
                            if published.handle is None:
                                raise BackupError(
                                    "published collision candidate was not retained"
                                )
                            current_pinned = (
                                RegularFileIdentity.from_descriptor(
                                    published.handle.fileno()
                                )
                            )
                            if not _same_relocation_payload(
                                published_identity,
                                current_pinned,
                            ):
                                logger.warning(
                                    "Published collision candidate changed while "
                                    "creating its recovery name; retrying without "
                                    "removing %s",
                                    source,
                                )
                                counter = max(2, hold_counter + 1)
                                continue
                            named_hold = _named_regular_file_identity(
                                hold,
                                label="relocation safety name",
                            )
                            if named_hold != current_pinned:
                                logger.warning(
                                    "Relocation safety name changed before "
                                    "source retirement; retrying without "
                                    "removing %s",
                                    source,
                                )
                                counter = max(2, hold_counter + 1)
                                continue
                except BackupError:
                    raise
                except OSError as exc:
                    raise BackupError(
                        f"could not relocate colliding snapshot {source}: {exc}"
                    ) from exc

                if not source_retirement_done:
                    try:
                        self.remove_snapshot_if_unchanged(
                            source,
                            expected=expected,
                        )
                    except _SnapshotGenerationChanged:
                        logger.warning(
                            "Colliding snapshot name now belongs to another generation; "
                            "preserving it after publishing %s",
                            candidate,
                        )
                    except BackupError as exc:
                        source_retirement_error = exc
                    source_retirement_done = True

                named_candidate = _named_regular_file_identity(
                    candidate,
                    label="published collision candidate",
                )
                named_hold = _named_regular_file_identity(
                    hold,
                    label="relocation safety name",
                )
                try:
                    if published.handle is None:
                        raise BackupError(
                            "published collision candidate was not retained"
                        )
                    current_pinned = RegularFileIdentity.from_descriptor(
                        published.handle.fileno()
                    )
                except OSError as exc:
                    raise BackupError(
                        "could not verify the retained collision candidate"
                    ) from exc
                if not _same_relocation_payload(
                    published_identity,
                    current_pinned,
                ):
                    logger.warning(
                        "Published collision candidate payload changed during "
                        "source retirement; retrying from the sealed generation"
                    )
                    counter = max(2, hold_counter + 1)
                    continue
                if named_candidate != current_pinned:
                    logger.warning(
                        "Published collision candidate changed during source "
                        "retirement; using the retained relocation copy after %s",
                        candidate,
                    )
                    if named_hold == current_pinned:
                        if source_retirement_error is not None:
                            raise source_retirement_error
                        return (
                            SnapshotInfo(
                                name=hold_name,
                                path=hold,
                                created_at=info.created_at,
                                size_bytes=current_pinned.size,
                            ),
                            current_pinned,
                        )
                    counter = max(2, hold_counter)
                    continue
                if source_retirement_error is not None:
                    raise source_retirement_error
                try:
                    self.remove_snapshot_if_unchanged(
                        hold,
                        expected=current_pinned,
                    )
                except _SnapshotGenerationChanged:
                    logger.warning(
                        "Relocation safety name now belongs to another "
                        "generation; preserving it at %s",
                        hold,
                    )
                except BackupError as exc:
                    source_retirement_error = exc
                final_candidate = _named_regular_file_identity(
                    candidate,
                    label="published collision candidate",
                )
                try:
                    if published.handle is None:
                        raise BackupError(
                            "published collision candidate was not retained"
                        )
                    final_pinned = RegularFileIdentity.from_descriptor(
                        published.handle.fileno()
                    )
                except OSError as exc:
                    raise BackupError(
                        "could not verify the retained collision candidate"
                    ) from exc
                if not _same_relocation_payload(
                    published_identity,
                    final_pinned,
                ):
                    logger.warning(
                        "Published collision candidate payload changed while "
                        "retiring its recovery name; retrying from the sealed "
                        "generation"
                    )
                    counter = max(2, hold_counter + 1)
                    continue
                if (
                    final_candidate is not None
                    and final_candidate == final_pinned
                ):
                    if source_retirement_error is not None:
                        raise source_retirement_error
                    return (
                        SnapshotInfo(
                            name=candidate_name,
                            path=candidate,
                            created_at=info.created_at,
                            size_bytes=final_candidate.size,
                        ),
                        final_candidate,
                    )
                final_hold = _named_regular_file_identity(
                    hold,
                    label="relocation safety name",
                )
                if final_hold is not None and final_hold == final_pinned:
                    if source_retirement_error is not None:
                        raise source_retirement_error
                    return (
                        SnapshotInfo(
                            name=hold_name,
                            path=hold,
                            created_at=info.created_at,
                            size_bytes=final_hold.size,
                        ),
                        final_hold,
                    )
                counter = max(2, hold_counter + 1)
                continue
            finally:
                published.close()

    def remove_snapshot_if_unchanged(
        self,
        path: Path,
        *,
        expected: RegularFileIdentity,
    ) -> None:
        """Remove one replicated snapshot only while its file identity matches.

        The export lock serializes cooperating HealthMes processes. The named
        entry is atomically moved into a private same-directory quarantine and
        verified there, so a replacement inserted after the first identity
        check can never be unlinked. Crash-left quarantines are recovered on
        the next call.
        """
        storage_dir = self._storage_dir()
        source = Path(path)
        try:
            source_parent = source.parent.resolve(strict=True)
        except OSError as exc:
            raise BackupError(
                f"could not verify replicated local snapshot {source}: {exc}"
            ) from exc
        if source_parent != storage_dir or source.name in {"", ".", ".."}:
            raise BackupError(
                "refusing to remove a replicated snapshot outside the "
                "configured backup directory"
            )

        try:
            with exclusive_file_lock(storage_dir / _EXPORT_LOCK_NAME):
                with _open_snapshot_parent(
                    storage_dir
                ) as parent_descriptor:
                    report = _recover_snapshot_quarantines(
                        storage_dir,
                        parent_descriptor,
                        source.name,
                        expected,
                    )
                    _log_snapshot_recovery_report(
                        report,
                        operation="replicated snapshot removal",
                    )
                    if report.recovered_target:
                        return

                    try:
                        if (
                            parent_descriptor is None
                            and source.is_symlink()
                        ):  # pragma: no cover - Windows
                            raise OSError(
                                f"snapshot must not be a symlink: {source}"
                            )
                        source_descriptor = os.open(
                            (
                                source
                                if parent_descriptor is None
                                else source.name
                            ),
                            os.O_RDONLY
                            | getattr(os, "O_CLOEXEC", 0)
                            | getattr(os, "O_NOFOLLOW", 0),
                            dir_fd=parent_descriptor,
                        )
                    except FileNotFoundError:
                        return
                    try:
                        metadata = os.fstat(source_descriptor)
                        if not expected.matches(metadata):
                            raise _SnapshotGenerationChanged(
                                "local snapshot changed after upload; "
                                f"preserving the current file: {source}"
                            )
                        quarantine_name = _create_snapshot_quarantine(
                            storage_dir,
                            parent_descriptor,
                            source.name,
                            expected,
                        )
                        remove_directory = False
                        mismatch_message: str | None = None
                        try:
                            with _open_snapshot_quarantine(
                                storage_dir,
                                parent_descriptor,
                                quarantine_name,
                            ) as quarantine:
                                try:
                                    _quarantine_snapshot_entry(
                                        source.name,
                                        quarantine,
                                    )
                                except FileNotFoundError:
                                    remove_directory = True
                                else:
                                    _fsync_snapshot_directory(
                                        quarantine.path,
                                        quarantine.descriptor,
                                    )
                                    _fsync_snapshot_directory(
                                        storage_dir,
                                        parent_descriptor,
                                    )
                                    moved_metadata = (
                                        _snapshot_quarantine_lstat(
                                            quarantine
                                        )
                                    )
                                    opened_metadata = os.fstat(
                                        source_descriptor
                                    )
                                    if (
                                        _quarantined_snapshot_matches(
                                            expected,
                                            moved_metadata,
                                        )
                                        and _same_snapshot_object(
                                            moved_metadata,
                                            opened_metadata,
                                        )
                                    ):
                                        _unlink_snapshot_quarantine_entry(
                                            quarantine
                                        )
                                        _fsync_snapshot_directory(
                                            quarantine.path,
                                            quarantine.descriptor,
                                        )
                                        remove_directory = True
                                    elif _restore_quarantined_snapshot(
                                        storage_dir,
                                        source.name,
                                        quarantine,
                                    ):
                                        remove_directory = True
                                        mismatch_message = (
                                            "local snapshot changed after "
                                            "upload; restored the raced "
                                            "generation without overwriting "
                                            "a newer file"
                                        )
                                    else:
                                        mismatch_message = (
                                            "local snapshot changed after "
                                            "upload; preserved the raced "
                                            "generation at "
                                            f"{quarantine.path}"
                                        )
                        except BaseException:
                            if remove_directory:
                                _remove_snapshot_quarantine(
                                    storage_dir,
                                    parent_descriptor,
                                    quarantine_name,
                                )
                            raise
                        if remove_directory:
                            _remove_snapshot_quarantine(
                                storage_dir,
                                parent_descriptor,
                                quarantine_name,
                            )
                        if mismatch_message is not None:
                            raise _SnapshotGenerationChanged(
                                mismatch_message
                            )
                    finally:
                        os.close(source_descriptor)
        except BackupError:
            raise
        except OSError as exc:
            raise BackupError(
                f"could not remove replicated local snapshot {source}: {exc}"
            ) from exc

    def restore(
        self,
        path: Path | str,
        *,
        allow_cross_store_partial: bool = False,
    ) -> RestoreResult:
        """Restore a snapshot by absolute path or bare name in the backup dir."""
        snapshot_path, expected = self._resolve_snapshot_generation(path)
        try:
            with open_regular_file(snapshot_path) as handle:
                if RegularFileIdentity.from_descriptor(handle.fileno()) != expected:
                    raise BackupError(
                        "snapshot changed after it was selected for restore: "
                        f"{snapshot_path}"
                    )
                return self.restore_open_snapshot(
                    snapshot_path,
                    handle,
                    allow_cross_store_partial=allow_cross_store_partial,
                )
        except BackupError:
            raise
        except OSError as exc:
            raise BackupError(
                f"could not open selected snapshot {snapshot_path}: {exc}"
            ) from exc

    def restore_open_snapshot(
        self,
        path: Path,
        handle: BinaryIO,
        *,
        allow_cross_store_partial: bool = False,
    ) -> RestoreResult:
        """Restore the exact regular-file generation held by ``handle``."""
        return restore_snapshot(
            path,
            passphrase=self._require_passphrase(),
            locations=self._locations,
            allow_cross_store_partial=allow_cross_store_partial,
            snapshot_handle=handle,
        )

    def resolve_snapshot_path(self, path: Path | str) -> Path:
        """Accept an absolute/relative path or a bare snapshot name in backup_dir."""
        candidate, _identity = self._resolve_snapshot_generation(path)
        return candidate

    def _resolve_snapshot_generation(
        self,
        path: Path | str,
    ) -> tuple[Path, RegularFileIdentity]:
        """Select one exact regular-file generation for a later open."""
        candidate = Path(path).expanduser()
        candidates = [candidate]
        if candidate.parent == Path("."):
            candidates.append(self._backup_dir / candidate.name)
        for selected in candidates:
            try:
                metadata = selected.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(
                metadata.st_mode
            ):
                continue
            return selected, RegularFileIdentity.from_metadata(metadata)
        raise BackupError(
            f"snapshot not found: {path} (looked in {self._backup_dir}; "
            "run `healthmes backup list`)"
        )

    def list_snapshots(self) -> list[SnapshotInfo]:
        """All snapshots in the backup dir, newest first; no passphrase needed."""
        storage_dir = self._storage_dir()
        if not storage_dir.is_dir():
            return []
        try:
            with exclusive_file_lock(storage_dir / _EXPORT_LOCK_NAME):
                with _open_snapshot_parent(storage_dir) as parent_descriptor:
                    report = _recover_snapshot_quarantines(
                        storage_dir,
                        parent_descriptor,
                    )
                _log_snapshot_recovery_report(report, operation="list")
                snapshots: list[SnapshotInfo] = []
                for entry in storage_dir.iterdir():
                    if entry.is_symlink() or not entry.is_file():
                        continue
                    created_at = parse_snapshot_name(entry.name)
                    if created_at is None:
                        continue
                    snapshots.append(
                        SnapshotInfo(
                            name=entry.name,
                            path=entry,
                            created_at=created_at,
                            size_bytes=entry.stat().st_size,
                        )
                    )
        except OSError as exc:
            raise BackupError(
                f"could not list snapshots in {self._backup_dir}: {exc}"
            ) from exc
        snapshots.sort(key=lambda info: (info.created_at, info.name), reverse=True)
        return snapshots


def build_backup_job(settings: Settings) -> Callable[[], None]:
    """Zero-arg weekly backup callable for ``register_backup_job``.

    Intended wiring (healthmes/app.py lifespan, before ``start_scheduler``)::

        scheduler = create_scheduler(settings)
        register_backup_job(scheduler, build_backup_job(settings))
        app.state.scheduler = start_scheduler(settings, scheduler=scheduler)

    Skips (with a warning) when no passphrase is configured and logs — never
    raises — on failure, so the APScheduler thread stays healthy.

    When the backup provider selector (``HEALTHMES_BACKUP_PROVIDER`` /
    ``Settings.backup_provider``) is ``remote_vault``, the local snapshot is
    additionally replicated to the configured S3-compatible vault. Local
    first: the local write happens (and is kept) regardless; a failed
    replication only logs. The remote_vault module is imported lazily so the
    default local path never pays the boto3 import.
    """

    def _replicate_to_vault(snapshot_path: Path) -> None:
        if resolve_backup_provider_name(settings) != PROVIDER_REMOTE_VAULT:
            return
        from healthmes.backup.remote_vault import RemoteVaultProvider

        remote_info = RemoteVaultProvider.from_settings(settings).push(snapshot_path)
        logger.info("Weekly backup replicated to vault: %s", remote_info.path)

    def run_weekly_backup() -> None:
        if resolve_passphrase(settings) is None:
            logger.warning(
                "Weekly backup skipped: no passphrase configured "
                "(set HEALTHMES_BACKUP_PASSPHRASE)."
            )
            return
        try:
            info = LocalDirectoryProvider.from_settings(settings).export_snapshot()
        except Exception:
            logger.exception("Weekly backup failed.")
            return
        logger.info("Weekly backup written: %s (%d bytes)", info.path, info.size_bytes)
        try:
            _replicate_to_vault(info.path)
        except Exception:
            # The local snapshot exists and stays valid; only replication failed.
            logger.exception("Weekly backup vault replication failed (local copy kept).")

    return run_weekly_backup
