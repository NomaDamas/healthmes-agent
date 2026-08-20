"""Crash-durable primitives for publishing immutable regular files."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import math
import os
import stat
import struct
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

if os.name != "nt":  # pragma: no cover - imported on POSIX runners
    import fcntl

__all__ = [
    "DurableFileIdentity",
    "DurablePublishError",
    "DurableUnlinkRecoveryReport",
    "DurabilityUnsupportedError",
    "FileGenerationMismatchError",
    "MaintenanceBudget",
    "MaintenanceBudgetExceeded",
    "durable_exclusive_writer",
    "durable_exclusive_writer_at",
    "durable_publish_no_clobber",
    "durable_unlink",
    "ensure_durable_directory",
    "open_directory_anchored",
    "read_directory_batch",
    "recover_durable_unlink_target",
    "recover_durable_unlink_quarantines",
    "require_directory_entry_durability",
    "verify_regular_file",
    "write_all",
]

_DIRECTORY_ENTRY_DURABILITY_SUPPORTED = os.name != "nt"
_UNLINK_QUARANTINE_PREFIX = ".healthmes-unlink-"
_UNLINK_QUARANTINE_V2_PREFIX = f"{_UNLINK_QUARANTINE_PREFIX}v2-"
_UNLINK_RECOVERY_CONTROL_DIRECTORY = ".healthmes-recovery"
_UNLINK_RECOVERY_CURSOR_NAME = "unlink-recovery-v1.json"
_UNLINK_RECOVERY_CURSOR_TMP_PREFIX = ".unlink-recovery-v1.tmp-"
_UNLINK_RECOVERY_CURSOR_MUTATION_RESERVE = 3
_UNLINK_RECOVERY_CURSOR_MAX_BYTES = 4 * 1024 * 1024
_UNLINK_RECOVERY_CURSOR_MAX_GROWTH_RESERVE = 512 * 1024
_UNLINK_RECOVERY_CURSOR_TEMP_CLEANUP_LIMIT = 256
_UNLINK_RECOVERY_MAX_QUEUE = 16 * 1024
_UNLINK_RECOVERY_DIRECTORY_QUANTUM = 32
_UNLINK_RECOVERY_DIRECTORY_BATCH_BYTES = 4096
_UNLINK_RECOVERY_LOCK_POLL_SECONDS = 0.01


class MaintenanceBudgetExceeded(RuntimeError):
    """One bounded maintenance resource was exhausted before an operation."""

    def __init__(
        self,
        *,
        resource: str,
        phase: str,
        used: int | float,
        limit: int | float,
        detail: str | None = None,
    ) -> None:
        self.resource = resource
        self.phase = phase
        self.used = used
        self.limit = limit
        message = (
            f"maintenance budget exceeded for {resource} during {phase}: "
            f"used {used}, limit {limit}"
        )
        if detail is not None:
            message = f"{message} ({detail})"
        super().__init__(message)


@dataclass(slots=True, init=False)
class MaintenanceBudget:
    """Shared absolute deadline and finite I/O budget for maintenance work."""

    deadline: float
    _started_at: float
    _timeout_seconds: float
    _hash_byte_limit: int
    _remaining_hash_bytes: int
    _directory_entry_limit: int
    _remaining_directory_entries: int

    @classmethod
    def start(
        cls,
        *,
        timeout_seconds: float,
        max_hash_bytes: int,
        max_directory_entries: int,
    ) -> MaintenanceBudget:
        if isinstance(timeout_seconds, bool):
            raise ValueError("timeout_seconds must be a finite non-negative number")
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout_seconds must be a finite non-negative number")
        for name, value in (
            ("max_hash_bytes", max_hash_bytes),
            ("max_directory_entries", max_directory_entries),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        started_at = time.monotonic()
        deadline = started_at + timeout
        if not math.isfinite(deadline):
            raise ValueError("timeout_seconds produces a non-finite deadline")
        budget = cls()
        budget.deadline = deadline
        budget._started_at = started_at
        budget._timeout_seconds = timeout
        budget._hash_byte_limit = max_hash_bytes
        budget._remaining_hash_bytes = max_hash_bytes
        budget._directory_entry_limit = max_directory_entries
        budget._remaining_directory_entries = max_directory_entries
        return budget

    def checkpoint(self, *, phase: str) -> None:
        now = time.monotonic()
        if now >= self.deadline:
            raise MaintenanceBudgetExceeded(
                resource="deadline",
                phase=phase,
                used=max(0.0, now - self._started_at),
                limit=self._timeout_seconds,
            )

    def reserve_hash_bytes(self, count: int, *, phase: str) -> None:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("hash byte count must be a non-negative integer")
        self.checkpoint(phase=phase)
        already_used = self._hash_byte_limit - self._remaining_hash_bytes
        attempted = already_used + count
        if count > self._remaining_hash_bytes:
            raise MaintenanceBudgetExceeded(
                resource="hash_bytes",
                phase=phase,
                used=attempted,
                limit=self._hash_byte_limit,
            )
        self._remaining_hash_bytes -= count

    def _reserve_directory_entries(
        self,
        count: int,
        *,
        phase: str,
        operation: str,
    ) -> None:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(
                "directory entry count must be a non-negative integer"
            )
        self.checkpoint(phase=phase)
        already_used = (
            self._directory_entry_limit
            - self._remaining_directory_entries
        )
        attempted = already_used + count
        if count > self._remaining_directory_entries:
            raise MaintenanceBudgetExceeded(
                resource="directory_entries",
                phase=phase,
                used=attempted,
                limit=self._directory_entry_limit,
                detail=f"operation={operation}",
            )
        self._remaining_directory_entries -= count

    def consume_directory_entry(
        self,
        *,
        phase: str,
        operation: str,
    ) -> None:
        self._reserve_directory_entries(
            1,
            phase=phase,
            operation=operation,
        )

    def reserve_directory_entries(
        self,
        count: int,
        *,
        phase: str,
        operation: str,
    ) -> None:
        """Atomically reserve a bounded namespace completion capsule."""

        self._reserve_directory_entries(
            count,
            phase=phase,
            operation=operation,
        )


@dataclass(frozen=True, slots=True)
class DurableFileIdentity:
    """Stable identity captured for one immutable regular-file generation."""

    device: int
    inode: int
    size: int
    mtime_ns: int

    @classmethod
    def from_metadata(cls, metadata: os.stat_result) -> DurableFileIdentity:
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("durable file identity requires a regular file")
        return cls(
            device=metadata.st_dev,
            inode=metadata.st_ino,
            size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
        )

    def matches(self, metadata: os.stat_result) -> bool:
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_dev == self.device
            and metadata.st_ino == self.inode
            and metadata.st_size == self.size
            and metadata.st_mtime_ns == self.mtime_ns
        )


class FileGenerationMismatchError(OSError):
    """The path no longer names the immutable generation being handled."""


class DurabilityUnsupportedError(OSError):
    """The platform cannot prove crash persistence for directory entries."""


class DurablePublishError(OSError):
    """Publication failed, possibly after creating the destination entry."""

    def __init__(
        self,
        message: str,
        *,
        destination_created: bool,
        identity: DurableFileIdentity | None = None,
    ) -> None:
        super().__init__(message)
        self.destination_created = destination_created
        self.identity = identity


@dataclass(frozen=True, slots=True)
class DurableUnlinkRecoveryReport:
    """Result of recovering crash-left ``durable_unlink`` quarantine entries."""

    scanned: int
    restored: int
    cleaned: int
    unresolved: int
    truncated: bool
    errors: tuple[str, ...]
    budget_exhausted: bool = False


def _absolute(path: Path) -> Path:
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else Path.cwd() / candidate


def _directory_identity(metadata: os.stat_result) -> tuple[int, int]:
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError("durable-unlink recovery requires a real directory")
    return metadata.st_dev, metadata.st_ino


def _directory_generation(metadata: os.stat_result) -> tuple[int, int, int, int]:
    device, inode = _directory_identity(metadata)
    return device, inode, metadata.st_mtime_ns, metadata.st_ctime_ns


def _recovery_name_is_safe(name: object) -> bool:
    return (
        isinstance(name, str)
        and name not in {"", ".", ".."}
        and "/" not in name
        and (os.name != "nt" or "\\" not in name)
        and "\x00" not in name
    )


def _recovery_parts_are_safe(parts: object) -> bool:
    return (
        isinstance(parts, list)
        and len(parts) <= 256
        and all(_recovery_name_is_safe(part) for part in parts)
    )


def _recovery_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid durable-unlink recovery {field}")
    return value


def _recovery_node(
    parts: tuple[str, ...],
    metadata: os.stat_result,
) -> dict[str, object]:
    device, inode, mtime_ns, ctime_ns = _directory_generation(metadata)
    return {
        "parts": list(parts),
        "device": device,
        "inode": inode,
        "mtime_ns": mtime_ns,
        "ctime_ns": ctime_ns,
        "offset": 0,
        "batch_index": 0,
        "complete": False,
        "rescan": False,
        "retry": [],
    }


def _recovery_node_generation(
    node: dict[str, object],
) -> tuple[int, int, int, int]:
    return (
        _recovery_integer(node.get("device"), "device"),
        _recovery_integer(node.get("inode"), "inode"),
        _recovery_integer(node.get("mtime_ns"), "mtime_ns"),
        _recovery_integer(node.get("ctime_ns"), "ctime_ns"),
    )


def _parse_recovery_cursor(payload: bytes) -> list[dict[str, object]]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid durable-unlink recovery cursor") from exc
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("unsupported durable-unlink recovery cursor")
    root_identity = value.get("root_identity")
    if (
        not isinstance(root_identity, list)
        or len(root_identity) != 2
        or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in root_identity
        )
    ):
        raise ValueError("invalid durable-unlink recovery root identity")
    nodes = value.get("nodes")
    if not isinstance(nodes, list):
        raise ValueError("durable-unlink recovery cursor nodes are invalid")
    if len(nodes) > _UNLINK_RECOVERY_MAX_QUEUE:
        raise ValueError("durable-unlink recovery cursor queue is too large")
    parsed: list[dict[str, object]] = []
    seen: set[tuple[str, ...]] = set()
    for raw_node in nodes:
        if not isinstance(raw_node, dict):
            raise ValueError("invalid durable-unlink recovery cursor node")
        raw_parts = raw_node.get("parts")
        if not _recovery_parts_are_safe(raw_parts):
            raise ValueError("invalid durable-unlink recovery cursor path")
        parts = tuple(raw_parts)
        if parts in seen:
            raise ValueError("duplicate durable-unlink recovery cursor path")
        seen.add(parts)
        retry = raw_node.get("retry", [])
        if (
            not isinstance(retry, list)
            or len(retry) > 256
            or not all(_recovery_name_is_safe(name) for name in retry)
        ):
            raise ValueError("invalid durable-unlink recovery retry list")
        offset = _recovery_integer(raw_node.get("offset"), "offset")
        if offset > 2**63 - 1:
            raise ValueError("durable-unlink recovery offset is too large")
        batch_index = _recovery_integer(
            raw_node.get("batch_index", 0),
            "batch index",
        )
        if batch_index > 4096:
            raise ValueError(
                "durable-unlink recovery batch index is too large"
            )
        parsed.append(
            {
                "parts": list(parts),
                "device": _recovery_integer(raw_node.get("device"), "device"),
                "inode": _recovery_integer(raw_node.get("inode"), "inode"),
                "mtime_ns": _recovery_integer(
                    raw_node.get("mtime_ns"),
                    "mtime_ns",
                ),
                "ctime_ns": _recovery_integer(
                    raw_node.get("ctime_ns"),
                    "ctime_ns",
                ),
                "offset": offset,
                "batch_index": batch_index,
                "complete": raw_node.get("complete") is True,
                "rescan": raw_node.get("rescan") is True,
                "retry": list(dict.fromkeys(retry)),
            }
        )
    if not parsed or not any(tuple(node["parts"]) == () for node in parsed):
        raise ValueError("durable-unlink recovery cursor has no root node")
    return parsed


def _recovery_cursor_payload(
    root_identity: tuple[int, int],
    nodes: list[dict[str, object]],
) -> bytes:
    return json.dumps(
        {
            "version": 1,
            "root_identity": list(root_identity),
            "nodes": nodes,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _recovery_node_payload_size(node: dict[str, object]) -> int:
    return len(
        json.dumps(
            node,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    )


def _recovery_cursor_size(
    root_identity: tuple[int, int],
    nodes: list[dict[str, object]],
) -> int:
    empty_size = len(_recovery_cursor_payload(root_identity, []))
    if not nodes:
        return empty_size
    return (
        empty_size
        + sum(_recovery_node_payload_size(node) for node in nodes)
        + len(nodes)
        - 1
    )


def _recovery_cursor_soft_limit() -> int:
    reserve = min(
        _UNLINK_RECOVERY_CURSOR_MAX_GROWTH_RESERVE,
        _UNLINK_RECOVERY_CURSOR_MAX_BYTES // 8,
    )
    return _UNLINK_RECOVERY_CURSOR_MAX_BYTES - reserve


def _recovery_cursor_fits(
    root_identity: tuple[int, int],
    nodes: list[dict[str, object]],
    *,
    soft: bool = True,
) -> bool:
    limit = (
        _recovery_cursor_soft_limit()
        if soft
        else _UNLINK_RECOVERY_CURSOR_MAX_BYTES
    )
    return _recovery_cursor_size(root_identity, nodes) <= limit


def _read_recovery_cursor(
    root_descriptor: int,
    *,
    deadline: float | None = None,
    budget: MaintenanceBudget | None = None,
) -> tuple[tuple[int, int] | None, list[dict[str, object]] | None, str | None]:
    control_descriptor: int | None = None
    payload_descriptor: int | None = None
    try:
        control_descriptor = os.open(
            _UNLINK_RECOVERY_CONTROL_DIRECTORY,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_descriptor,
        )
        metadata = os.fstat(control_descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_mode & 0o077
            or (
                hasattr(os, "geteuid")
                and metadata.st_uid != os.geteuid()
            )
        ):
            raise ValueError(
                "durable-unlink recovery control must be an owner-only "
                "directory"
            )
        try:
            payload_descriptor = os.open(
                _UNLINK_RECOVERY_CURSOR_NAME,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=control_descriptor,
            )
        except FileNotFoundError:
            return None, None, None
        metadata = os.fstat(payload_descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > _UNLINK_RECOVERY_CURSOR_MAX_BYTES
            or metadata.st_mode & 0o077
            or (
                hasattr(os, "geteuid")
                and metadata.st_uid != os.geteuid()
            )
        ):
            raise ValueError(
                "durable-unlink recovery cursor must be a private "
                "small regular file"
            )
        payload = bytearray()
        while len(payload) <= _UNLINK_RECOVERY_CURSOR_MAX_BYTES:
            _checkpoint_maintenance(
                budget,
                phase="durable unlink recovery cursor read",
            )
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(
                    "durable-unlink recovery deadline exceeded while "
                    "reading its cursor"
                )
            chunk = os.read(payload_descriptor, 16 * 1024)
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > _UNLINK_RECOVERY_CURSOR_MAX_BYTES:
            raise ValueError("durable-unlink recovery cursor exceeds size limit")
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError("invalid durable-unlink recovery cursor")
        raw_root_identity = value.get("root_identity")
        if (
            not isinstance(raw_root_identity, list)
            or len(raw_root_identity) != 2
        ):
            raise ValueError("invalid durable-unlink recovery root identity")
        root_identity = (
            _recovery_integer(raw_root_identity[0], "root device"),
            _recovery_integer(raw_root_identity[1], "root inode"),
        )
        return root_identity, _parse_recovery_cursor(bytes(payload)), None
    except FileNotFoundError:
        return None, None, None
    except (MaintenanceBudgetExceeded, TimeoutError):
        raise
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return None, None, f"invalid durable-unlink recovery cursor: {exc}"
    finally:
        if payload_descriptor is not None:
            os.close(payload_descriptor)
        if control_descriptor is not None:
            os.close(control_descriptor)


def _is_recovery_cursor_temporary_name(name: str) -> bool:
    if not name.startswith(_UNLINK_RECOVERY_CURSOR_TMP_PREFIX):
        return False
    token = name[len(_UNLINK_RECOVERY_CURSOR_TMP_PREFIX) :]
    return len(token) == 32 and all(
        character in "0123456789abcdef" for character in token
    )


def _cleanup_recovery_cursor_temporaries(
    root_descriptor: int,
    *,
    deadline: float,
    budget: MaintenanceBudget | None,
) -> tuple[str, ...]:
    """Remove bounded crash-left cursor temps from the private control dir."""

    try:
        control_descriptor = _open_recovery_control_directory(
            root_descriptor,
            create=False,
        )
    except FileNotFoundError:
        return ()
    errors: list[str] = []
    removed = False
    inspected = 0
    offset = 0
    try:
        while inspected < _UNLINK_RECOVERY_CURSOR_TEMP_CLEANUP_LIMIT:
            _checkpoint_maintenance(
                budget,
                phase="durable unlink recovery cursor temp scan",
            )
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "durable-unlink recovery deadline exceeded while "
                    "cleaning cursor temporaries"
                )
            names, next_offset, complete = read_directory_batch(
                control_descriptor,
                offset,
            )
            if not names and complete:
                break
            for name in names:
                if inspected >= _UNLINK_RECOVERY_CURSOR_TEMP_CLEANUP_LIMIT:
                    break
                inspected += 1
                if budget is not None:
                    budget.consume_directory_entry(
                        phase="durable unlink recovery cursor temp scan",
                        operation="scan",
                    )
                if not _is_recovery_cursor_temporary_name(name):
                    continue
                try:
                    metadata = os.stat(
                        name,
                        dir_fd=control_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    continue
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_mode & 0o077
                    or (
                        hasattr(os, "geteuid")
                        and metadata.st_uid != os.geteuid()
                    )
                ):
                    errors.append(
                        "unsafe durable-unlink recovery cursor temporary "
                        f"was preserved: {name}"
                    )
                    continue
                _charge_directory_mutation(
                    budget,
                    phase="durable unlink recovery cursor temp cleanup",
                    operation="unlink",
                )
                try:
                    os.unlink(name, dir_fd=control_descriptor)
                except FileNotFoundError:
                    continue
                removed = True
            if complete:
                break
            offset = next_offset
        if removed:
            os.fsync(control_descriptor)
    finally:
        os.close(control_descriptor)
    return tuple(errors)


def _acquire_recovery_lock(
    root_descriptor: int,
    *,
    deadline: float,
    budget: MaintenanceBudget | None,
) -> None:
    """Serialize the per-root cursor protocol until the descriptor closes."""

    while True:
        _checkpoint_maintenance(
            budget,
            phase="durable unlink recovery lock",
        )
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "durable-unlink recovery deadline exceeded while waiting "
                "for its per-root lock"
            )
        try:
            fcntl.flock(
                root_descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EINTR}:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "durable-unlink recovery deadline exceeded while waiting "
                    "for its per-root lock"
                ) from exc
            time.sleep(
                min(_UNLINK_RECOVERY_LOCK_POLL_SECONDS, remaining)
            )


def _open_recovery_control_directory(
    root_descriptor: int,
    *,
    create: bool = True,
) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(
            _UNLINK_RECOVERY_CONTROL_DIRECTORY,
            flags,
            dir_fd=root_descriptor,
        )
    except FileNotFoundError:
        if not create:
            raise
        os.mkdir(
            _UNLINK_RECOVERY_CONTROL_DIRECTORY,
            mode=0o700,
            dir_fd=root_descriptor,
        )
        _fsync_directory(".", root_descriptor)
        descriptor = os.open(
            _UNLINK_RECOVERY_CONTROL_DIRECTORY,
            flags,
            dir_fd=root_descriptor,
        )
    metadata = os.fstat(descriptor)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_mode & 0o077
        or (
            hasattr(os, "geteuid")
            and metadata.st_uid != os.geteuid()
        )
    ):
        os.close(descriptor)
        raise OSError(
            "durable-unlink recovery control must be an owner-only directory"
        )
    return descriptor


def _write_recovery_cursor(
    root_descriptor: int,
    root_identity: tuple[int, int],
    nodes: list[dict[str, object]],
    *,
    budget: MaintenanceBudget | None = None,
    mutations_precharged: bool = False,
    create_control: bool | None = None,
) -> None:
    if create_control is None:
        try:
            os.stat(
                _UNLINK_RECOVERY_CONTROL_DIRECTORY,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            create_control = True
        else:
            create_control = False
    if budget is not None and not mutations_precharged:
        _reserve_directory_mutations(
            budget,
            _UNLINK_RECOVERY_CURSOR_MUTATION_RESERVE
            + int(create_control),
            phase="durable unlink recovery cursor publication",
            operation="mutation",
        )
    payload = _recovery_cursor_payload(root_identity, nodes)
    if len(payload) > _UNLINK_RECOVERY_CURSOR_MAX_BYTES:
        raise OSError("durable-unlink recovery cursor is too large")
    control_descriptor = _open_recovery_control_directory(
        root_descriptor,
        create=create_control,
    )
    temporary = f"{_UNLINK_RECOVERY_CURSOR_TMP_PREFIX}{uuid.uuid4().hex}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=control_descriptor,
        )
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("durable-unlink recovery cursor made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary,
            _UNLINK_RECOVERY_CURSOR_NAME,
            src_dir_fd=control_descriptor,
            dst_dir_fd=control_descriptor,
        )
        os.fsync(control_descriptor)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=control_descriptor)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(control_descriptor)


def _read_directory_batch(
    descriptor: int,
    offset: int,
) -> tuple[tuple[str, ...], int, bool]:
    """Read one kernel directory batch without following path components."""
    os.lseek(descriptor, offset, os.SEEK_SET)
    buffer = ctypes.create_string_buffer(
        _UNLINK_RECOVERY_DIRECTORY_BATCH_BYTES
    )
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        get_entries = getattr(libc, "__getdirentries64")
        get_entries.argtypes = (
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_longlong),
        )
        get_entries.restype = ctypes.c_ssize_t
        base = ctypes.c_longlong()
        ctypes.set_errno(0)
        size = get_entries(
            descriptor,
            buffer,
            len(buffer),
            ctypes.byref(base),
        )
        header_size = 21
        format_string = "=QQHHB"
    elif sys.platform.startswith("linux"):
        get_entries = getattr(libc, "getdents64", None)
        if get_entries is None:
            raise DurabilityUnsupportedError(
                "durable-unlink recovery requires libc getdents64 on Linux"
            )
        get_entries.argtypes = (
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_size_t,
        )
        get_entries.restype = ctypes.c_ssize_t
        ctypes.set_errno(0)
        size = get_entries(descriptor, buffer, len(buffer))
        header_size = 19
        format_string = "=QqHB"
    else:
        raise DurabilityUnsupportedError(
            "resumable durable-unlink recovery is unsupported on this POSIX "
            f"platform: {sys.platform}"
        )
    if size < 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
        )
    next_offset = os.lseek(descriptor, 0, os.SEEK_CUR)
    if size == 0:
        return (), next_offset, True

    payload = memoryview(buffer.raw)[:size]
    names: list[str] = []
    position = 0
    while position < size:
        if position + header_size > size:
            raise OSError("truncated durable-unlink directory record")
        fields = struct.unpack_from(format_string, payload, position)
        inode = fields[0]
        record_length = fields[-3] if sys.platform == "darwin" else fields[-2]
        if (
            record_length < header_size + 1
            or position + record_length > size
        ):
            raise OSError("invalid durable-unlink directory record length")
        if sys.platform == "darwin":
            name_length = fields[-2]
            if name_length > record_length - header_size:
                raise OSError(
                    "invalid durable-unlink directory entry name length"
                )
            name_bytes = bytes(
                payload[
                    position + header_size : position
                    + header_size
                    + name_length
                ]
            )
        else:
            record = bytes(
                payload[
                    position + header_size : position + record_length
                ]
            )
            name_bytes = record.split(b"\x00", 1)[0]
        if inode and name_bytes not in {b".", b".."}:
            name = os.fsdecode(name_bytes)
            if not _recovery_name_is_safe(name):
                raise OSError("unsafe durable-unlink directory entry name")
            names.append(name)
        position += record_length
    return tuple(names), next_offset, False


def read_directory_batch(
    descriptor: int,
    offset: int,
) -> tuple[tuple[str, ...], int, bool]:
    """Read one bounded, resumable directory batch from an open descriptor."""
    names, next_offset, complete = _read_directory_batch(descriptor, offset)
    # Directory cursors are opaque cookies, not monotonically increasing byte
    # offsets. Darwin can legitimately return a numerically smaller cookie for
    # the next batch; only an unchanged cookie proves that no progress occurred.
    if not complete and next_offset == offset:
        raise OSError(
            "directory batch read returned entries without advancing its "
            "cursor"
        )
    return names, next_offset, complete


def _checkpoint_maintenance(
    budget: MaintenanceBudget | None,
    *,
    phase: str,
) -> None:
    if budget is not None:
        budget.checkpoint(phase=phase)


def _charge_directory_mutation(
    budget: MaintenanceBudget | None,
    *,
    phase: str,
    operation: str,
) -> None:
    if budget is not None:
        budget.consume_directory_entry(
            phase=phase,
            operation=operation,
        )


def _reserve_directory_mutations(
    budget: MaintenanceBudget | None,
    count: int,
    *,
    phase: str,
    operation: str,
) -> None:
    """Reserve one uninterrupted durable completion capsule before unlinking."""
    if budget is not None:
        budget.reserve_directory_entries(
            count,
            phase=phase,
            operation=operation,
        )


def _iter_directory_names_with_budget(
    descriptor: int,
    *,
    budget: MaintenanceBudget | None,
    phase: str,
) -> Iterator[str]:
    """Yield names from bounded kernel batches while charging each entry."""
    offset = 0
    while True:
        _checkpoint_maintenance(budget, phase=phase)
        names, offset, complete = read_directory_batch(descriptor, offset)
        if budget is not None:
            budget._reserve_directory_entries(
                len(names),
                phase=phase,
                operation="scan",
            )
        yield from names
        if complete:
            return


def _directory_names_for_maintenance(
    descriptor: int,
    *,
    budget: MaintenanceBudget | None,
    phase: str,
) -> tuple[str, ...]:
    """Keep legacy unbounded enumeration when no maintenance budget exists."""
    if budget is None:
        return tuple(os.listdir(descriptor))
    return tuple(
        _iter_directory_names_with_budget(
            descriptor,
            budget=budget,
            phase=phase,
        )
    )


def require_directory_entry_durability() -> None:
    if not _DIRECTORY_ENTRY_DURABILITY_SUPPORTED:
        raise DurabilityUnsupportedError(
            "crash-durable directory entry operations are unsupported "
            "on Windows; run the HealthMes Personal Data Node on a "
            "supported POSIX filesystem"
        )


@contextmanager
def _open_canonical_directory(
    canonical: Path,
    expected: os.stat_result,
) -> Iterator[int]:
    """Open one already-canonical directory without following any component."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(canonical.anchor, flags)
    try:
        for component in canonical.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_dev != expected.st_dev
            or metadata.st_ino != expected.st_ino
        ):
            raise OSError(
                f"directory path changed while it was being opened: {canonical}"
            )
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def open_directory_anchored(path: Path) -> Iterator[tuple[Path, int]]:
    """Resolve configured aliases once, then pin every directory component."""
    candidate = _absolute(path)
    canonical = candidate.resolve(strict=True)
    expected = os.stat(canonical, follow_symlinks=False)
    if not stat.S_ISDIR(expected.st_mode):
        raise OSError(f"directory must be real: {candidate}")
    if os.name == "nt":  # pragma: no cover - exercised on Windows runners
        named = os.stat(candidate, follow_symlinks=True)
        if (
            not stat.S_ISDIR(named.st_mode)
            or named.st_dev != expected.st_dev
            or named.st_ino != expected.st_ino
        ):
            raise OSError(
                f"directory path changed while it was being opened: {candidate}"
            )
        yield canonical, -1
        return
    with _open_canonical_directory(canonical, expected) as descriptor:
        named = os.stat(candidate, follow_symlinks=True)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(named.st_mode)
            or named.st_dev != opened.st_dev
            or named.st_ino != opened.st_ino
        ):
            raise OSError(
                f"directory path changed while it was being opened: {candidate}"
            )
        yield canonical, descriptor


@contextmanager
def _open_directory(path: Path) -> Iterator[int]:
    with open_directory_anchored(path) as (_canonical, descriptor):
        yield descriptor


def _fsync_directory(path: Path, descriptor: int) -> None:
    require_directory_entry_durability()
    os.fsync(descriptor)


def ensure_durable_directory(path: Path, *, mode: int = 0o700) -> None:
    """Create missing directory components and persist every new parent entry."""
    target = _absolute(path)
    if os.name == "nt":  # pragma: no cover - exercised on Windows runners
        target.mkdir(parents=True, exist_ok=True, mode=mode)
        if target.is_symlink() or not target.is_dir():
            raise OSError(f"directory must not be a symlink: {target}")
        return

    canonical_target = target.resolve(strict=False)
    existing = canonical_target
    while True:
        try:
            expected = os.stat(existing, follow_symlinks=False)
        except FileNotFoundError:
            parent = existing.parent
            if parent == existing:
                raise
            existing = parent
            continue
        if not stat.S_ISDIR(expected.st_mode):
            raise OSError(f"directory must not be a symlink: {existing}")
        break

    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    relative_parts = canonical_target.relative_to(existing).parts
    with _open_canonical_directory(existing, expected) as ancestor_descriptor:
        current_descriptor = os.dup(ancestor_descriptor)
        try:
            for component in relative_parts:
                try:
                    child = os.open(
                        component,
                        flags,
                        dir_fd=current_descriptor,
                    )
                except FileNotFoundError:
                    try:
                        os.mkdir(
                            component,
                            mode=mode,
                            dir_fd=current_descriptor,
                        )
                    except FileExistsError:
                        pass
                    else:
                        os.fsync(current_descriptor)
                    child = os.open(
                        component,
                        flags,
                        dir_fd=current_descriptor,
                    )
                metadata = os.fstat(child)
                if not stat.S_ISDIR(metadata.st_mode):
                    os.close(child)
                    raise OSError(
                        f"directory component is not real: "
                        f"{canonical_target}"
                    )
                os.close(current_descriptor)
                current_descriptor = child

            opened = os.fstat(current_descriptor)
            named = os.stat(target, follow_symlinks=True)
            if (
                not stat.S_ISDIR(named.st_mode)
                or named.st_dev != opened.st_dev
                or named.st_ino != opened.st_ino
            ):
                raise OSError(
                    f"directory path changed while it was being created: {target}"
                )
        finally:
            os.close(current_descriptor)


_ensure_durable_directory = ensure_durable_directory


@contextmanager
def durable_exclusive_writer(
    path: Path,
    *,
    mode: int = 0o600,
) -> Iterator[BinaryIO]:
    """Exclusively create, completely flush, and fsync one regular file."""
    target = _absolute(path)
    _ensure_durable_directory(target.parent)
    descriptor: int | None = None
    handle: BinaryIO | None = None
    with _open_directory(target.parent) as parent_descriptor:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        if os.name == "nt":  # pragma: no cover - exercised on Windows runners
            descriptor = os.open(target, flags, mode)
        else:
            descriptor = os.open(
                target.name,
                flags,
                mode,
                dir_fd=parent_descriptor,
            )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError(f"temporary output must be regular: {target}")
            handle = os.fdopen(descriptor, "wb")
            descriptor = None
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            handle = None
            _fsync_directory(target.parent, parent_descriptor)
        finally:
            if handle is not None:
                handle.close()
            if descriptor is not None:
                os.close(descriptor)


@contextmanager
def durable_exclusive_writer_at(
    directory_descriptor: int,
    name: str,
    *,
    mode: int = 0o600,
) -> Iterator[BinaryIO]:
    """Create and persist one file relative to an already-pinned directory."""
    if os.name == "nt":  # pragma: no cover - POSIX dir_fd contract
        raise DurabilityUnsupportedError(
            "descriptor-relative durable writes are unsupported on Windows"
        )
    if name in {"", ".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError("descriptor-relative file name must be one safe component")
    descriptor: int | None = None
    handle: BinaryIO | None = None
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=directory_descriptor,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(
                f"descriptor-relative output must be regular: {name}"
            )
        handle = os.fdopen(descriptor, "wb")
        descriptor = None
        yield handle
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.fsync(directory_descriptor)
    finally:
        if handle is not None:
            handle.close()
        if descriptor is not None:
            os.close(descriptor)


def write_all(handle: BinaryIO, payload: bytes | bytearray | memoryview) -> None:
    """Write every byte or raise instead of accepting a short write."""
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        written = handle.write(view[offset:])
        if written is None or written <= 0:
            raise OSError("regular-file write made no progress")
        offset += written


def _entry_generation(metadata: os.stat_result) -> tuple[int, ...]:
    """Identity fields that remain stable when the entry is renamed."""
    return (
        stat.S_IFMT(metadata.st_mode),
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _hash_generation(metadata: os.stat_result) -> tuple[int, ...]:
    """Fields proving cached bytes were not changed between hash operations."""

    return (
        stat.S_IFMT(metadata.st_mode),
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _remove_mismatched_publication_posix(
    parent_descriptor: int,
    target_name: str,
    expected: os.stat_result,
) -> None:
    """Remove only the exact non-regular generation created by publication."""
    expected_link_target = (
        os.readlink(target_name, dir_fd=parent_descriptor)
        if stat.S_ISLNK(expected.st_mode)
        else None
    )
    quarantine_name = f".healthmes-publish-rollback-{uuid.uuid4().hex}"
    os.rename(
        target_name,
        quarantine_name,
        src_dir_fd=parent_descriptor,
        dst_dir_fd=parent_descriptor,
    )
    os.fsync(parent_descriptor)
    quarantined = os.stat(
        quarantine_name,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )
    matches = _entry_generation(quarantined) == _entry_generation(expected)
    if matches and expected_link_target is not None:
        matches = (
            os.readlink(
                quarantine_name,
                dir_fd=parent_descriptor,
            )
            == expected_link_target
        )
    if matches:
        os.unlink(quarantine_name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
        return

    try:
        os.link(
            quarantine_name,
            target_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileExistsError as exc:
        raise FileGenerationMismatchError(
            "a newer destination appeared while the mismatched publication "
            "was quarantined; both generations were preserved"
        ) from exc
    os.fsync(parent_descriptor)
    os.unlink(quarantine_name, dir_fd=parent_descriptor)
    os.fsync(parent_descriptor)
    raise FileGenerationMismatchError(
        "published destination changed before rollback and was restored"
    )


def durable_publish_no_clobber(
    staged: Path,
    destination: Path,
) -> DurableFileIdentity:
    """Atomically publish a complete staged file without replacing any entry.

    The hard-link publication is same-filesystem by construction. The staged
    name remains available as a durable fallback until the caller explicitly
    removes it after the database outcome is known.
    """
    source = _absolute(staged)
    target = _absolute(destination)
    require_directory_entry_durability()
    _ensure_durable_directory(target.parent)
    destination_created = False
    published_identity: DurableFileIdentity | None = None
    try:
        with (
            _open_directory(source.parent) as source_parent,
            _open_directory(target.parent) as target_parent,
        ):
            source_flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            if os.name == "nt":  # pragma: no cover - exercised on Windows runners
                source_descriptor = os.open(source, source_flags)
            else:
                source_descriptor = os.open(
                    source.name,
                    source_flags,
                    dir_fd=source_parent,
                )
            try:
                source_metadata = os.fstat(source_descriptor)
                if not stat.S_ISREG(source_metadata.st_mode):
                    raise OSError(f"staged output must be regular: {source}")
                os.fsync(source_descriptor)
                if os.name == "nt":  # pragma: no cover - exercised on Windows runners
                    os.link(source, target, follow_symlinks=False)
                    destination_created = True
                    target_metadata = target.lstat()
                else:
                    os.link(
                        source.name,
                        target.name,
                        src_dir_fd=source_parent,
                        dst_dir_fd=target_parent,
                        follow_symlinks=False,
                    )
                    destination_created = True
                    target_metadata = os.stat(
                        target.name,
                        dir_fd=target_parent,
                        follow_symlinks=False,
                    )
                if (
                    not stat.S_ISREG(target_metadata.st_mode)
                    or target_metadata.st_dev != source_metadata.st_dev
                    or target_metadata.st_ino != source_metadata.st_ino
                ):
                    try:
                        if stat.S_ISREG(target_metadata.st_mode):
                            durable_unlink(
                                target,
                                expected=DurableFileIdentity.from_metadata(
                                    target_metadata
                                ),
                            )
                        else:
                            _remove_mismatched_publication_posix(
                                target_parent,
                                target.name,
                                target_metadata,
                            )
                    except OSError as exc:
                        raise OSError(
                            "published entry used a replaced staged generation "
                            "and could not be safely removed"
                        ) from exc
                    raise OSError(
                        "published entry used a replaced staged generation and "
                        "was safely removed"
                    )
                published_identity = DurableFileIdentity.from_metadata(
                    source_metadata
                )
                _fsync_directory(target.parent, target_parent)
                identity = published_identity
            finally:
                os.close(source_descriptor)
    except FileExistsError as exc:
        if not destination_created:
            raise
        raise DurablePublishError(
            f"could not durably publish {target}: {exc}",
            destination_created=True,
            identity=published_identity,
        ) from exc
    except OSError as exc:
        raise DurablePublishError(
            f"could not durably publish {target}: {exc}",
            destination_created=destination_created,
            identity=published_identity,
        ) from exc
    return identity


def verify_regular_file(
    path: Path,
    expected: DurableFileIdentity,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
    deadline: float | None = None,
) -> None:
    """Verify path, inode generation, size, and optionally content digest."""
    target = _absolute(path)
    digest = hashlib.sha256() if expected_sha256 is not None else None
    with _open_directory(target.parent) as parent_descriptor:
        if os.name == "nt":  # pragma: no cover - exercised on Windows runners
            descriptor = os.open(target, os.O_RDONLY)
        else:
            descriptor = os.open(
                target.name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_descriptor,
            )
        try:
            initial = os.fstat(descriptor)
            if not expected.matches(initial):
                raise FileGenerationMismatchError(
                    f"file generation changed: {target}"
                )
            if expected_size is not None and initial.st_size != expected_size:
                raise FileGenerationMismatchError(
                    f"file size changed: {target}"
                )
            while digest is not None:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(
                        "regular-file verification deadline exceeded"
                    )
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            final = os.fstat(descriptor)
            if not expected.matches(final):
                raise FileGenerationMismatchError(
                    f"file generation changed while reading: {target}"
                )
            if digest is not None and digest.hexdigest() != expected_sha256.lower():
                raise FileGenerationMismatchError(
                    f"file contents changed: {target}"
                )
            if os.name != "nt":
                named = os.stat(
                    target.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            else:  # pragma: no cover - exercised on Windows runners
                named = target.stat()
            if not expected.matches(named):
                raise FileGenerationMismatchError(
                    f"file path generation changed: {target}"
                )
        finally:
            os.close(descriptor)


def durable_unlink(
    path: Path,
    *,
    missing_ok: bool = False,
    expected: DurableFileIdentity | None = None,
    budget: MaintenanceBudget | None = None,
) -> bool:
    """Unlink one entry and fsync its parent before reporting success."""
    target = _absolute(path)
    try:
        with _open_directory(target.parent) as parent_descriptor:
            recovered = False
            if expected is not None and os.name != "nt":
                target_descriptor: int | None = None
                try:
                    target_descriptor = os.open(
                        target.name,
                        os.O_RDONLY
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=parent_descriptor,
                    )
                except FileNotFoundError:
                    # A live target can be deleted without enumerating every
                    # unrelated journal in a potentially large directory.
                    # Bounded maintenance relies on the resumable global
                    # recovery pass for crash-left journals.
                    if budget is None:
                        recovered = _recover_target_unlink_quarantines_posix(
                            target,
                            parent_descriptor,
                            expected,
                            budget=None,
                        )
                except OSError:
                    # The expected-generation path below reports the exact
                    # unsafe or mismatched entry. Only absence enables legacy
                    # journal discovery.
                    pass
                finally:
                    if target_descriptor is not None:
                        os.close(target_descriptor)
            try:
                if os.name == "nt":  # pragma: no cover - exercised on Windows runners
                    if expected is None:
                        _charge_directory_mutation(
                            budget,
                            phase="durable unlink",
                            operation="unlink",
                        )
                        target.unlink()
                    else:
                        return _unlink_expected_generation_windows(
                            target,
                            expected,
                            budget=budget,
                        )
                else:
                    if expected is None:
                        _charge_directory_mutation(
                            budget,
                            phase="durable unlink",
                            operation="unlink",
                        )
                        os.unlink(target.name, dir_fd=parent_descriptor)
                    else:
                        return _unlink_expected_generation_posix(
                            target,
                            parent_descriptor,
                            expected,
                            budget=budget,
                        )
            except FileNotFoundError:
                if missing_ok:
                    return recovered
                raise
            _fsync_directory(target.parent, parent_descriptor)
            return True
    except FileNotFoundError:
        if missing_ok:
            return False
        raise


def recover_durable_unlink_target(
    path: Path,
    expected: DurableFileIdentity,
    *,
    budget: MaintenanceBudget | None = None,
) -> bool:
    """Finish crash-left unlink intent for exactly one expected target.

    Unlike the global resumable recovery pass, this scans only the target's
    parent and considers quarantine records that name that exact target.
    """

    if os.name == "nt":  # pragma: no cover - POSIX quarantine contract
        return False
    target = _absolute(path)
    try:
        with _open_directory(target.parent) as parent_descriptor:
            return _recover_target_unlink_quarantines_posix(
                target,
                parent_descriptor,
                expected,
                budget=budget,
            )
    except FileNotFoundError:
        return False


_UNLINK_METADATA_NAME = "metadata.json"
_UNLINK_METADATA_TMP_PREFIX = ".metadata-v3.tmp-"
_UNLINK_PAYLOAD_NAME = "payload"
_UNLINK_MANUAL_REVIEW_PREFIX = ".healthmes-manual-unlink-v1-"
_UNLINK_RECOVERY_MAX_METADATA_BYTES = 64 * 1024


def _unlink_quarantine_name(_name: str) -> str:
    return f"{_UNLINK_QUARANTINE_V2_PREFIX}{uuid.uuid4().hex}"


def _unlink_metadata_temporary_name() -> str:
    return f"{_UNLINK_METADATA_TMP_PREFIX}{uuid.uuid4().hex}"


def _is_unlink_metadata_temporary_name(name: str) -> bool:
    if not name.startswith(_UNLINK_METADATA_TMP_PREFIX):
        return False
    token = name[len(_UNLINK_METADATA_TMP_PREFIX) :]
    return len(token) == 32 and all(
        character in "0123456789abcdef" for character in token
    )


def _unlink_manual_review_name() -> str:
    return f"{_UNLINK_MANUAL_REVIEW_PREFIX}{uuid.uuid4().hex}"


def _identity_payload(identity: DurableFileIdentity) -> dict[str, int]:
    return {
        "device": identity.device,
        "inode": identity.inode,
        "size": identity.size,
        "mtime_ns": identity.mtime_ns,
    }


def _identity_from_payload(value: object) -> DurableFileIdentity:
    if not isinstance(value, dict):
        raise ValueError("durable-unlink identity must be an object")
    fields: dict[str, int] = {}
    for key in ("device", "inode", "size", "mtime_ns"):
        candidate = value.get(key)
        if (
            isinstance(candidate, bool)
            or not isinstance(candidate, int)
            or candidate < 0
        ):
            raise ValueError(f"invalid durable-unlink identity field: {key}")
        fields[key] = candidate
    return DurableFileIdentity(**fields)


def _unlink_metadata(
    *,
    target_name: str,
    expected: DurableFileIdentity,
    expected_sha256: str,
) -> bytes:
    return json.dumps(
        {
            "version": 3,
            "target_name": target_name,
            "expected": _identity_payload(expected),
            "expected_sha256": expected_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _publish_unlink_metadata_posix(
    quarantine_descriptor: int,
    payload: bytes,
    *,
    budget: MaintenanceBudget | None = None,
) -> None:
    """Atomically expose only a completely written unlink intent record."""

    if len(payload) > _UNLINK_RECOVERY_MAX_METADATA_BYTES:
        raise ValueError("durable-unlink metadata exceeds the size limit")
    _reserve_directory_mutations(
        budget,
        3,
        phase="durable unlink metadata publication",
        operation="mutation",
    )
    temporary = _unlink_metadata_temporary_name()
    descriptor: int | None = None
    temporary_present = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=quarantine_descriptor,
        )
        temporary_present = True
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError(
                    "durable-unlink metadata write made no progress"
                )
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(
            temporary,
            _UNLINK_METADATA_NAME,
            src_dir_fd=quarantine_descriptor,
            dst_dir_fd=quarantine_descriptor,
            follow_symlinks=False,
        )
        os.fsync(quarantine_descriptor)
        os.unlink(temporary, dir_fd=quarantine_descriptor)
        temporary_present = False
        os.fsync(quarantine_descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_present:
            try:
                os.unlink(temporary, dir_fd=quarantine_descriptor)
            except FileNotFoundError:
                pass
            else:
                os.fsync(quarantine_descriptor)


def _parse_unlink_metadata(
    payload: bytes,
) -> tuple[str, DurableFileIdentity, str]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid durable-unlink metadata") from exc
    if not isinstance(value, dict) or value.get("version") != 3:
        raise ValueError("unsupported durable-unlink metadata version")
    target_name = value.get("target_name")
    if not _recovery_name_is_safe(target_name):
        raise ValueError("invalid durable-unlink target name")
    expected_sha256 = value.get("expected_sha256")
    if (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in expected_sha256
        )
    ):
        raise ValueError("invalid durable-unlink content digest")
    return (
        target_name,
        _identity_from_payload(value.get("expected")),
        expected_sha256,
    )


def _cleanup_unlink_quarantine_posix(
    parent_descriptor: int,
    quarantine_name: str,
    quarantine_descriptor: int,
    *,
    budget: MaintenanceBudget | None = None,
) -> None:
    _reserve_directory_mutations(
        budget,
        3,
        phase="durable unlink quarantine cleanup",
        operation="mutation",
    )
    for entry_name in (_UNLINK_PAYLOAD_NAME, _UNLINK_METADATA_NAME):
        try:
            os.unlink(entry_name, dir_fd=quarantine_descriptor)
        except FileNotFoundError:
            pass
    os.fsync(quarantine_descriptor)
    os.rmdir(quarantine_name, dir_fd=parent_descriptor)
    os.fsync(parent_descriptor)


def _read_unlink_metadata_posix(
    quarantine_descriptor: int,
) -> tuple[str, DurableFileIdentity, str]:
    descriptor = os.open(
        _UNLINK_METADATA_NAME,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=quarantine_descriptor,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > _UNLINK_RECOVERY_MAX_METADATA_BYTES
        ):
            raise ValueError("durable-unlink metadata must be a small regular file")
        payload = bytearray()
        while len(payload) <= _UNLINK_RECOVERY_MAX_METADATA_BYTES:
            chunk = os.read(descriptor, 16 * 1024)
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > _UNLINK_RECOVERY_MAX_METADATA_BYTES:
            raise ValueError("durable-unlink metadata exceeds the size limit")
    finally:
        os.close(descriptor)
    return _parse_unlink_metadata(bytes(payload))


def _unlink_metadata_temporary_names_posix(
    quarantine_descriptor: int,
    *,
    budget: MaintenanceBudget | None = None,
) -> tuple[str, ...]:
    temporary_names: list[str] = []
    for name in _directory_names_for_maintenance(
        quarantine_descriptor,
        budget=budget,
        phase="durable unlink quarantine scan",
    ):
        if name in {_UNLINK_METADATA_NAME, _UNLINK_PAYLOAD_NAME}:
            continue
        if not _is_unlink_metadata_temporary_name(name):
            raise ValueError(
                "unexpected entry in durable-unlink quarantine"
            )
        temporary_names.append(name)
    return tuple(temporary_names)


def _metadata_generation(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _sha256_expected_entry_posix(
    parent_descriptor: int,
    name: str,
    expected: DurableFileIdentity,
    *,
    deadline: float | None = None,
    budget: MaintenanceBudget | None = None,
    phase: str = "durable unlink hash",
    digest_cache: (
        dict[tuple[int, int], tuple[tuple[int, ...], str]] | None
    ) = None,
) -> str:
    """Hash one no-follow entry while proving its descriptor and name stay stable."""
    descriptor = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_descriptor,
    )
    try:
        before = os.fstat(descriptor)
        if not expected.matches(before):
            raise FileGenerationMismatchError(
                "durable-unlink entry records a different generation"
            )
        generation = _metadata_generation(before)
        cache_generation = _hash_generation(before)
        cache_key = before.st_dev, before.st_ino
        cached = (
            digest_cache.get(cache_key)
            if digest_cache is not None
            else None
        )
        digest = None
        if cached is None or cached[0] != cache_generation:
            if budget is not None:
                budget.reserve_hash_bytes(
                    before.st_size,
                    phase=phase,
                )
            digest = hashlib.sha256()
            while True:
                _checkpoint_maintenance(budget, phase=phase)
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(
                        "durable-unlink recovery deadline exceeded"
                    )
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            result = digest.hexdigest()
        else:
            result = cached[1]
        after = os.fstat(descriptor)
        if _metadata_generation(after) != generation:
            raise FileGenerationMismatchError(
                "durable-unlink entry changed while hashing"
            )
        named = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if _metadata_generation(named) != generation:
            raise FileGenerationMismatchError(
                "durable-unlink path generation changed while hashing"
            )
        if digest_cache is not None:
            digest_cache[cache_key] = cache_generation, result
        return result
    finally:
        os.close(descriptor)


def _open_unlink_quarantine_posix(
    parent_descriptor: int,
    name: str,
) -> int:
    return os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_descriptor,
    )


def _isolate_unlink_quarantine_posix(
    parent_descriptor: int,
    quarantine_name: str,
    *,
    budget: MaintenanceBudget | None = None,
) -> str:
    """Preserve an ambiguous journal under a name global recovery ignores."""

    descriptor = _open_unlink_quarantine_posix(
        parent_descriptor,
        quarantine_name,
    )
    try:
        opened = os.fstat(descriptor)
        named = os.stat(
            quarantine_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or opened.st_dev != named.st_dev
            or opened.st_ino != named.st_ino
        ):
            raise FileGenerationMismatchError(
                "durable-unlink quarantine changed before manual-review "
                "isolation"
            )
        manual_name = _unlink_manual_review_name()
        _charge_directory_mutation(
            budget,
            phase="durable unlink manual-review isolation",
            operation="rename",
        )
        os.rename(
            quarantine_name,
            manual_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        isolated = os.stat(
            manual_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(isolated.st_mode)
            or opened.st_dev != isolated.st_dev
            or opened.st_ino != isolated.st_ino
        ):
            raise FileGenerationMismatchError(
                "manual-review quarantine publication changed generation"
            )
        os.fsync(parent_descriptor)
        return manual_name
    finally:
        os.close(descriptor)


def _legacy_unlink_quarantine_names(
    parent_descriptor: int,
    target_name: str,
    *,
    budget: MaintenanceBudget | None = None,
) -> tuple[str, ...]:
    suffix = f"-{target_name}"
    matches: list[str] = []
    for name in _directory_names_for_maintenance(
        parent_descriptor,
        budget=budget,
        phase="durable unlink recovery scan",
    ):
        if (
            not name.startswith(_UNLINK_QUARANTINE_PREFIX)
            or name.startswith(_UNLINK_QUARANTINE_V2_PREFIX)
            or not name.endswith(suffix)
        ):
            continue
        token = name[len(_UNLINK_QUARANTINE_PREFIX) : -len(suffix)]
        if len(token) == 32 and all(character in "0123456789abcdef" for character in token):
            matches.append(name)
    return tuple(sorted(matches))


def _recover_legacy_unlink_quarantine_posix(
    parent_descriptor: int,
    quarantine_name: str,
    *,
    target_name: str,
    expected: DurableFileIdentity,
    budget: MaintenanceBudget | None = None,
) -> bool:
    """Finish a v1 unlink only when the caller supplies its exact identity."""
    descriptor = os.open(
        quarantine_name,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_descriptor,
    )
    try:
        quarantine_metadata = os.fstat(descriptor)
        if not expected.matches(quarantine_metadata):
            raise FileGenerationMismatchError(
                "legacy durable-unlink quarantine records a different "
                "generation"
            )
        try:
            target_metadata = os.stat(
                target_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            target_metadata = None
        if target_metadata is not None and expected.matches(target_metadata):
            if (
                target_metadata.st_dev != quarantine_metadata.st_dev
                or target_metadata.st_ino != quarantine_metadata.st_ino
            ):
                raise FileGenerationMismatchError(
                    "legacy durable-unlink target and quarantine disagree"
                )
            _unlink_expected_generation_posix(
                Path(target_name),
                parent_descriptor,
                expected,
                budget=budget,
            )
            _charge_directory_mutation(
                budget,
                phase="durable unlink legacy recovery completion",
                operation="unlink",
            )
        else:
            _charge_directory_mutation(
                budget,
                phase="durable unlink legacy recovery completion",
                operation="unlink",
            )
        os.unlink(quarantine_name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    finally:
        os.close(descriptor)
    return True


def _recover_one_unlink_quarantine_posix(
    parent_descriptor: int,
    quarantine_name: str,
    *,
    target_name: str | None = None,
    expected: DurableFileIdentity | None = None,
    deadline: float | None = None,
    budget: MaintenanceBudget | None = None,
) -> tuple[str, DurableFileIdentity, bool]:
    _checkpoint_maintenance(
        budget,
        phase="durable unlink recovery",
    )
    quarantine_descriptor = _open_unlink_quarantine_posix(
        parent_descriptor,
        quarantine_name,
    )
    try:
        digest_cache: dict[
            tuple[int, int],
            tuple[tuple[int, ...], str],
        ] = {}
        (
            recorded_target,
            recorded_identity,
            recorded_sha256,
        ) = _read_unlink_metadata_posix(quarantine_descriptor)
        if target_name is not None and recorded_target != target_name:
            return recorded_target, recorded_identity, False
        if expected is not None and recorded_identity != expected:
            raise FileGenerationMismatchError(
                "durable-unlink quarantine records a different generation"
            )
        temporary_names = _unlink_metadata_temporary_names_posix(
            quarantine_descriptor,
            budget=budget,
        )
        try:
            payload_metadata = os.stat(
                _UNLINK_PAYLOAD_NAME,
                dir_fd=quarantine_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            payload_present = False
        else:
            payload_present = True
            if not recorded_identity.matches(payload_metadata):
                raise FileGenerationMismatchError(
                    "durable-unlink quarantine payload generation changed"
                )
            if (
                _sha256_expected_entry_posix(
                    quarantine_descriptor,
                    _UNLINK_PAYLOAD_NAME,
                    recorded_identity,
                    deadline=deadline,
                    budget=budget,
                    phase="durable unlink recovery payload hash",
                    digest_cache=digest_cache,
                )
                != recorded_sha256
            ):
                raise FileGenerationMismatchError(
                    "durable-unlink quarantine payload contents changed"
                )
        try:
            target_metadata = os.stat(
                recorded_target,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            target_metadata = None
        if target_metadata is not None and recorded_identity.matches(
            target_metadata
        ):
            if payload_present:
                if (
                    target_metadata.st_dev != payload_metadata.st_dev
                    or target_metadata.st_ino != payload_metadata.st_ino
                ):
                    raise FileGenerationMismatchError(
                        "durable-unlink target and quarantine payload disagree"
                    )
            # Never unlink the public target name after validation. A writer
            # could replace it in that gap. The normal journal protocol moves
            # the current name into a new private quarantine first, validates
            # that moved generation, and restores a raced replacement.
            _unlink_expected_generation_posix(
                Path(recorded_target),
                parent_descriptor,
                recorded_identity,
                budget=budget,
                expected_sha256=recorded_sha256,
                deadline=deadline,
                digest_cache=digest_cache,
            )
        if payload_present:
            # Once the payload starts unlinking, finish the durable completion
            # capsule even if its cooperative deadline has just elapsed.
            _reserve_directory_mutations(
                budget,
                3 + len(temporary_names),
                phase="durable unlink recovery completion",
                operation="mutation",
            )
            os.unlink(_UNLINK_PAYLOAD_NAME, dir_fd=quarantine_descriptor)
            os.fsync(quarantine_descriptor)
        else:
            if not payload_present:
                _reserve_directory_mutations(
                    budget,
                    2 + len(temporary_names),
                    phase="durable unlink recovery completion",
                    operation="mutation",
                )
        for temporary_name in temporary_names:
            try:
                os.unlink(
                    temporary_name,
                    dir_fd=quarantine_descriptor,
                )
            except FileNotFoundError:
                pass
        if temporary_names:
            os.fsync(quarantine_descriptor)
        try:
            os.unlink(_UNLINK_METADATA_NAME, dir_fd=quarantine_descriptor)
        except FileNotFoundError:
            pass
        os.fsync(quarantine_descriptor)
    finally:
        os.close(quarantine_descriptor)
    os.rmdir(quarantine_name, dir_fd=parent_descriptor)
    os.fsync(parent_descriptor)
    return recorded_target, recorded_identity, payload_present


def _recover_target_unlink_quarantines_posix(
    target: Path,
    parent_descriptor: int,
    expected: DurableFileIdentity,
    *,
    budget: MaintenanceBudget | None = None,
) -> bool:
    legacy = _legacy_unlink_quarantine_names(
        parent_descriptor,
        target.name,
        budget=budget,
    )
    v2_quarantines = sorted(
        name for name in _directory_names_for_maintenance(
            parent_descriptor,
            budget=budget,
            phase="durable unlink recovery scan",
        )
        if name.startswith(_UNLINK_QUARANTINE_V2_PREFIX)
    )
    recovered = False
    for name in legacy:
        recovered = (
            _recover_legacy_unlink_quarantine_posix(
                parent_descriptor,
                name,
                target_name=target.name,
                expected=expected,
                budget=budget,
            )
            or recovered
        )
    for name in v2_quarantines:
        try:
            _, _, payload_present = _recover_one_unlink_quarantine_posix(
                parent_descriptor,
                name,
                target_name=target.name,
                expected=expected,
                budget=budget,
            )
        except FileNotFoundError:
            if _remove_empty_unlink_quarantine_posix(
                parent_descriptor,
                name,
                budget=budget,
            ):
                continue
            try:
                _isolate_unlink_quarantine_posix(
                    parent_descriptor,
                    name,
                    budget=budget,
                )
            except OSError:
                # The journal remains private and preserved. It must not block
                # deletion of a separately named live target.
                pass
            continue
        except FileGenerationMismatchError:
            try:
                _isolate_unlink_quarantine_posix(
                    parent_descriptor,
                    name,
                    budget=budget,
                )
            except OSError:
                # Valid metadata tied this journal to the requested target.
                # If it cannot be isolated, do not claim that target cleanup
                # completed.
                raise
            continue
        except ValueError:
            try:
                _isolate_unlink_quarantine_posix(
                    parent_descriptor,
                    name,
                    budget=budget,
                )
            except OSError:
                # Preserve ambiguous bytes under their existing private name.
                # Global recovery will report and retry them independently.
                pass
            continue
        recovered = recovered or payload_present
    return recovered


def _restore_unlink_payload_posix(
    parent_descriptor: int,
    quarantine_descriptor: int,
    target_name: str,
    *,
    budget: MaintenanceBudget | None = None,
) -> None:
    """Restore a raced generation without replacing a newer target entry."""
    _charge_directory_mutation(
        budget,
        phase="durable unlink restore",
        operation="link",
    )
    try:
        os.link(
            _UNLINK_PAYLOAD_NAME,
            target_name,
            src_dir_fd=quarantine_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileExistsError as exc:
        raise FileGenerationMismatchError(
            "replacement generation is preserved in durable-unlink quarantine"
        ) from exc
    os.fsync(parent_descriptor)


def _remove_empty_unlink_quarantine_posix(
    parent_descriptor: int,
    quarantine_name: str,
    *,
    budget: MaintenanceBudget | None = None,
) -> bool:
    """Remove only a v2 journal with no committed deletion intent."""
    try:
        quarantine_descriptor = _open_unlink_quarantine_posix(
            parent_descriptor,
            quarantine_name,
        )
    except FileNotFoundError:
        return True
    try:
        temporary_names: list[str] = []
        for name in _directory_names_for_maintenance(
            quarantine_descriptor,
            budget=budget,
            phase="durable unlink empty quarantine scan",
        ):
            if not _is_unlink_metadata_temporary_name(name):
                return False
            temporary_names.append(name)
        _reserve_directory_mutations(
            budget,
            len(temporary_names) + 1,
            phase="durable unlink empty quarantine cleanup",
            operation="mutation",
        )
        for name in temporary_names:
            try:
                os.unlink(name, dir_fd=quarantine_descriptor)
            except FileNotFoundError:
                pass
        if temporary_names:
            os.fsync(quarantine_descriptor)
    finally:
        os.close(quarantine_descriptor)
    try:
        os.rmdir(quarantine_name, dir_fd=parent_descriptor)
    except FileNotFoundError:
        return True
    os.fsync(parent_descriptor)
    return True


def _unlink_expected_generation_posix(
    target: Path,
    parent_descriptor: int,
    expected: DurableFileIdentity,
    *,
    budget: MaintenanceBudget | None = None,
    expected_sha256: str | None = None,
    deadline: float | None = None,
    digest_cache: (
        dict[tuple[int, int], tuple[tuple[int, ...], str]] | None
    ) = None,
) -> bool:
    if digest_cache is None:
        digest_cache = {}
    if expected_sha256 is not None and (
        len(expected_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in expected_sha256
        )
    ):
        raise ValueError("invalid expected SHA-256 for durable unlink")
    current_sha256 = _sha256_expected_entry_posix(
        parent_descriptor,
        target.name,
        expected,
        deadline=deadline,
        budget=budget,
        phase="durable unlink target hash",
        digest_cache=digest_cache,
    )
    if expected_sha256 is None:
        expected_sha256 = current_sha256
    elif current_sha256 != expected_sha256:
        raise FileGenerationMismatchError(
            "durable-unlink target contents do not match the "
            "journaled generation"
        )
    quarantine_name = _unlink_quarantine_name(target.name)
    _charge_directory_mutation(
        budget,
        phase="durable unlink quarantine create",
        operation="mkdir",
    )
    os.mkdir(quarantine_name, mode=0o700, dir_fd=parent_descriptor)
    os.fsync(parent_descriptor)
    quarantine_descriptor = os.open(
        quarantine_name,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_descriptor,
    )
    try:
        _publish_unlink_metadata_posix(
            quarantine_descriptor,
            _unlink_metadata(
                target_name=target.name,
                expected=expected,
                expected_sha256=expected_sha256,
            ),
            budget=budget,
        )
        try:
            _charge_directory_mutation(
                budget,
                phase="durable unlink quarantine payload",
                operation="rename",
            )
            os.rename(
                target.name,
                _UNLINK_PAYLOAD_NAME,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=quarantine_descriptor,
            )
        except BaseException:
            _cleanup_unlink_quarantine_posix(
                parent_descriptor,
                quarantine_name,
                quarantine_descriptor,
                budget=budget,
            )
            raise
        os.fsync(quarantine_descriptor)
        os.fsync(parent_descriptor)
        metadata = os.stat(
            _UNLINK_PAYLOAD_NAME,
            dir_fd=quarantine_descriptor,
            follow_symlinks=False,
        )
        contents_match = False
        if expected.matches(metadata):
            contents_match = (
                _sha256_expected_entry_posix(
                    quarantine_descriptor,
                    _UNLINK_PAYLOAD_NAME,
                    expected,
                    deadline=deadline,
                    budget=budget,
                    phase="durable unlink quarantined payload hash",
                    digest_cache=digest_cache,
                )
                == expected_sha256
            )
        if not expected.matches(metadata) or not contents_match:
            try:
                _restore_unlink_payload_posix(
                    parent_descriptor,
                    quarantine_descriptor,
                    target.name,
                    budget=budget,
                )
                _cleanup_unlink_quarantine_posix(
                    parent_descriptor,
                    quarantine_name,
                    quarantine_descriptor,
                    budget=budget,
                )
            except BaseException:
                # Preserve the self-describing quarantine if a newer target
                # appeared or restoration became ambiguous.
                pass
            raise FileGenerationMismatchError(f"file generation changed: {target}")
        try:
            # The rename has already journaled deletion intent. Reserve every
            # namespace mutation before unlinking the payload so fsync and the
            # completion capsule cannot be abandoned at the deadline.
            _reserve_directory_mutations(
                budget,
                3,
                phase="durable unlink completion",
                operation="mutation",
            )
            os.unlink(_UNLINK_PAYLOAD_NAME, dir_fd=quarantine_descriptor)
            os.fsync(quarantine_descriptor)
            os.unlink(_UNLINK_METADATA_NAME, dir_fd=quarantine_descriptor)
            os.fsync(quarantine_descriptor)
            os.rmdir(quarantine_name, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        except BaseException:
            # The metadata makes this deletion intent recoverable. Do not
            # restore the payload after the caller has requested deletion.
            raise
    finally:
        os.close(quarantine_descriptor)
    return True


def _unlink_expected_generation_windows(
    target: Path,
    expected: DurableFileIdentity,
    *,
    budget: MaintenanceBudget | None = None,
) -> bool:  # pragma: no cover - exercised on Windows runners
    quarantine = target.with_name(_unlink_quarantine_name(target.name))
    _charge_directory_mutation(
        budget,
        phase="durable unlink quarantine create",
        operation="rename",
    )
    os.rename(target, quarantine)
    try:
        metadata = quarantine.lstat()
        if not expected.matches(metadata):
            try:
                _reserve_directory_mutations(
                    budget,
                    2,
                    phase="durable unlink restore",
                    operation="mutation",
                )
                os.link(quarantine, target, follow_symlinks=False)
                quarantine.unlink()
            except FileExistsError as exc:
                raise FileGenerationMismatchError(
                    f"replacement generation is preserved in quarantine "
                    f"{quarantine}"
                ) from exc
            raise FileGenerationMismatchError(
                f"file generation changed: {target}"
            )
        try:
            _charge_directory_mutation(
                budget,
                phase="durable unlink completion",
                operation="unlink",
            )
            quarantine.unlink()
        except BaseException:
            try:
                os.rename(quarantine, target)
            except BaseException:
                pass
            raise
    except BaseException:
        raise
    return True


def recover_durable_unlink_quarantines(
    root: Path,
    *,
    max_entries: int = 256,
    max_seconds: float = 1.0,
    deadline: float | None = None,
    budget: MaintenanceBudget | None = None,
) -> DurableUnlinkRecoveryReport:
    """Finish self-describing crash-left unlinks below ``root``.

    Only v2 quarantine directories created by this module are mutated. Legacy
    v1 names and malformed entries are preserved and reported because their
    deletion intent cannot be proven after a crash.
    """
    if (
        isinstance(max_entries, bool)
        or not isinstance(max_entries, int)
        or max_entries <= 0
    ):
        raise ValueError("max_entries must be positive")
    if isinstance(max_seconds, bool):
        raise ValueError("max_seconds must be a finite positive number")
    max_seconds_value = float(max_seconds)
    if not math.isfinite(max_seconds_value) or max_seconds_value <= 0:
        raise ValueError("max_seconds must be a finite positive number")
    if deadline is not None:
        if isinstance(deadline, bool):
            raise ValueError("deadline must be a finite monotonic timestamp")
        deadline = float(deadline)
        if not math.isfinite(deadline):
            raise ValueError("deadline must be a finite monotonic timestamp")
    if os.name == "nt":  # pragma: no cover - exercised on Windows runners
        return DurableUnlinkRecoveryReport(
            scanned=0,
            restored=0,
            cleaned=0,
            unresolved=1,
            truncated=True,
            errors=(
                "durable-unlink quarantine recovery is unsupported on "
                "Windows; run the HealthMes Personal Data Node on a "
                "supported POSIX filesystem",
            ),
        )

    target_root = _absolute(root)
    try:
        root_metadata = target_root.lstat()
    except FileNotFoundError:
        return DurableUnlinkRecoveryReport(
            scanned=0,
            restored=0,
            cleaned=0,
            unresolved=0,
            truncated=False,
            errors=(),
        )
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(
        root_metadata.st_mode
    ):
        raise OSError(f"durable-unlink recovery root must be a real directory: {target_root}")

    started_at = time.monotonic()
    deadline_candidates = [started_at + max_seconds_value]
    if deadline is not None:
        deadline_candidates.append(deadline)
    if budget is not None:
        deadline_candidates.append(budget.deadline)
    recovery_deadline = min(deadline_candidates)
    scanned = 0
    cleaned = 0
    unresolved = 0
    errors: list[str] = []
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )

    @contextmanager
    def open_relative_directory(
        root_descriptor: int,
        parts: tuple[str, ...],
    ) -> Iterator[int]:
        descriptor = os.dup(root_descriptor)
        try:
            for part in parts:
                child = os.open(
                    part,
                    directory_flags,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)

    with _open_directory(target_root) as root_descriptor:
        try:
            _acquire_recovery_lock(
                root_descriptor,
                deadline=recovery_deadline,
                budget=budget,
            )
            errors.extend(
                _cleanup_recovery_cursor_temporaries(
                    root_descriptor,
                    deadline=recovery_deadline,
                    budget=budget,
                )
            )
        except MaintenanceBudgetExceeded as exc:
            return DurableUnlinkRecoveryReport(
                scanned=0,
                restored=0,
                cleaned=0,
                unresolved=0,
                truncated=True,
                errors=(str(exc),),
                budget_exhausted=True,
            )
        except TimeoutError as exc:
            return DurableUnlinkRecoveryReport(
                scanned=0,
                restored=0,
                cleaned=0,
                unresolved=0,
                truncated=True,
                errors=(str(exc),),
            )
        root_metadata = os.fstat(root_descriptor)
        root_identity = _directory_identity(root_metadata)
        try:
            (
                cursor_root_identity,
                nodes,
                cursor_error,
            ) = _read_recovery_cursor(
                root_descriptor,
                deadline=recovery_deadline,
                budget=budget,
            )
        except MaintenanceBudgetExceeded as exc:
            return DurableUnlinkRecoveryReport(
                scanned=0,
                restored=0,
                cleaned=0,
                unresolved=0,
                truncated=True,
                errors=(str(exc),),
                budget_exhausted=True,
            )
        except TimeoutError as exc:
            return DurableUnlinkRecoveryReport(
                scanned=0,
                restored=0,
                cleaned=0,
                unresolved=0,
                truncated=True,
                errors=(str(exc),),
            )
        if cursor_error is not None:
            unresolved += 1
            errors.append(cursor_error)
        if (
            nodes is None
            or cursor_root_identity is None
            or cursor_root_identity != root_identity
        ):
            if (
                cursor_root_identity is not None
                and cursor_root_identity != root_identity
            ):
                unresolved += 1
                errors.append(
                    "stale durable-unlink recovery cursor was reset for a "
                    "different storage root"
                )
            nodes = [_recovery_node((), root_metadata)]
        elif not nodes:
            nodes = [_recovery_node((), root_metadata)]
        elif all(
            node["complete"] and not node["retry"]
            for node in nodes
        ):
            nodes = [_recovery_node((), root_metadata)]

        cursor_mutations_precharged = False
        try:
            os.stat(
                _UNLINK_RECOVERY_CONTROL_DIRECTORY,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            cursor_control_missing = True
        else:
            cursor_control_missing = False
        if budget is not None:
            try:
                _reserve_directory_mutations(
                    budget,
                    _UNLINK_RECOVERY_CURSOR_MUTATION_RESERVE
                    + int(cursor_control_missing),
                    phase="durable unlink recovery cursor publication",
                    operation="mutation",
                )
            except MaintenanceBudgetExceeded as exc:
                return DurableUnlinkRecoveryReport(
                    scanned=0,
                    restored=0,
                    cleaned=0,
                    unresolved=0,
                    truncated=True,
                    errors=(str(exc),),
                    budget_exhausted=True,
                )
            cursor_mutations_precharged = True

        queued_paths = {
            tuple(node["parts"])
            for node in nodes
        }
        cursor_empty_size = len(
            _recovery_cursor_payload(root_identity, [])
        )
        queued_node_bytes = sum(
            _recovery_node_payload_size(node) for node in nodes
        )

        def queued_cursor_size(
            *additional: dict[str, object],
        ) -> int:
            count = len(nodes) + len(additional)
            return (
                cursor_empty_size
                + queued_node_bytes
                + sum(
                    _recovery_node_payload_size(node)
                    for node in additional
                )
                + max(0, count - 1)
            )

        def append_node(node: dict[str, object]) -> None:
            nonlocal queued_node_bytes
            nodes.append(node)
            queued_node_bytes += _recovery_node_payload_size(node)

        def pop_node(index: int) -> dict[str, object]:
            nonlocal queued_node_bytes
            node = nodes.pop(index)
            queued_node_bytes -= _recovery_node_payload_size(node)
            return node

        def evict_completed_node() -> bool:
            for index, queued in enumerate(nodes):
                if (
                    tuple(queued["parts"])
                    and queued["complete"]
                    and not queued["retry"]
                ):
                    evicted = pop_node(index)
                    queued_paths.discard(tuple(evicted["parts"]))
                    return True
            return False

        while queued_cursor_size() > _recovery_cursor_soft_limit():
            if not evict_completed_node():
                break

        nodes_this_round = len(nodes)
        active_node: dict[str, object] | None = None
        budget_error: MaintenanceBudgetExceeded | None = None

        def charge_recovery_scan() -> None:
            if budget is not None:
                budget.consume_directory_entry(
                    phase="durable unlink recovery scan",
                    operation="scan",
                )

        def enqueue_directory(
            parts: tuple[str, ...],
            metadata: os.stat_result,
        ) -> bool:
            if parts in queued_paths:
                return True
            candidate = _recovery_node(parts, metadata)
            while True:
                additional = (
                    (candidate,)
                    if active_node is None
                    else (active_node, candidate)
                )
                if (
                    len(nodes) + len(additional)
                    <= _UNLINK_RECOVERY_MAX_QUEUE
                    and queued_cursor_size(*additional)
                    <= _recovery_cursor_soft_limit()
                ):
                    break
                if not evict_completed_node():
                    return False
            append_node(candidate)
            queued_paths.add(parts)
            return True

        def inspect_name(
            parent_descriptor: int,
            relative_directory: tuple[str, ...],
            name: str,
        ) -> tuple[bool, bool]:
            """Return ``(retry, timed_out)`` for one anchored entry."""
            nonlocal cleaned, unresolved
            path = target_root.joinpath(*relative_directory, name)
            if name.startswith(_UNLINK_MANUAL_REVIEW_PREFIX):
                return False, False

            def isolate_for_manual_review(exc: BaseException) -> None:
                nonlocal unresolved
                try:
                    manual_name = _isolate_unlink_quarantine_posix(
                        parent_descriptor,
                        name,
                        budget=budget,
                    )
                except OSError as isolation_exc:
                    unresolved += 1
                    errors.append(
                        f"{path}: {exc}; manual-review isolation failed: "
                        f"{isolation_exc}"
                    )
                    return
                unresolved += 1
                errors.append(
                    f"{path}: {exc}; preserved for manual review as "
                    f"{path.with_name(manual_name)}"
                )

            try:
                metadata = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return False, False
            except OSError as exc:
                unresolved += 1
                errors.append(
                    "could not inspect durable-unlink entry "
                    f"{path}: {exc}"
                )
                return False, False
            if stat.S_ISLNK(metadata.st_mode):
                if name.startswith(_UNLINK_QUARANTINE_PREFIX):
                    unresolved += 1
                    errors.append(
                        f"durable-unlink symlink preserved: {path}"
                    )
                    return False, False
                return False, False
            if stat.S_ISDIR(metadata.st_mode):
                if name == _UNLINK_RECOVERY_CONTROL_DIRECTORY:
                    return False, False
                if name.startswith(_UNLINK_QUARANTINE_V2_PREFIX):
                    try:
                        _recover_one_unlink_quarantine_posix(
                            parent_descriptor,
                            name,
                            deadline=recovery_deadline,
                            budget=budget,
                        )
                    except TimeoutError:
                        return True, True
                    except FileNotFoundError as exc:
                        try:
                            removed = _remove_empty_unlink_quarantine_posix(
                                parent_descriptor,
                                name,
                                budget=budget,
                            )
                        except OSError as cleanup_exc:
                            unresolved += 1
                            errors.append(
                                f"{path}: could not clean an empty "
                                f"durable-unlink quarantine: {cleanup_exc}"
                            )
                            return False, False
                        if removed:
                            cleaned += 1
                            return False, False
                        isolate_for_manual_review(exc)
                        return False, False
                    except (FileGenerationMismatchError, ValueError) as exc:
                        isolate_for_manual_review(exc)
                        return False, False
                    except OSError as exc:
                        unresolved += 1
                        errors.append(f"{path}: {exc}")
                        return False, False
                    cleaned += 1
                    return False, False
                if name.startswith(_UNLINK_QUARANTINE_PREFIX):
                    unresolved += 1
                    errors.append(
                        f"unknown durable-unlink quarantine preserved: {path}"
                    )
                    return False, False
                child_parts = (*relative_directory, name)
                if not enqueue_directory(child_parts, metadata):
                    unresolved += 1
                    errors.append(
                        "durable-unlink recovery directory queue is full; "
                        f"retrying {path}"
                    )
                    return True, False
                return False, False
            if name.startswith(_UNLINK_QUARANTINE_PREFIX):
                unresolved += 1
                errors.append(
                    f"legacy durable-unlink quarantine preserved: {path}"
                )
                return False, False
            return False, False

        for _ in range(nodes_this_round):
            if not nodes:
                break
            if scanned >= max_entries or time.monotonic() >= recovery_deadline:
                break
            try:
                _checkpoint_maintenance(
                    budget,
                    phase="durable unlink recovery scan",
                )
            except MaintenanceBudgetExceeded as exc:
                budget_error = exc
                break
            node = pop_node(0)
            active_node = node
            relative_directory = tuple(node["parts"])
            queued_paths.discard(relative_directory)
            directory = target_root.joinpath(*relative_directory)
            try:
                with open_relative_directory(
                    root_descriptor,
                    relative_directory,
                ) as parent_descriptor:
                    metadata = os.fstat(parent_descriptor)
                    retry = list(node["retry"])
                    recorded_generation = _recovery_node_generation(node)
                    current_generation = _directory_generation(metadata)
                    if recorded_generation[:2] != current_generation[:2]:
                        replacement = _recovery_node(
                            relative_directory,
                            metadata,
                        )
                        replacement["retry"] = retry
                        node = replacement
                    elif recorded_generation != current_generation:
                        node["rescan"] = True
                        (
                            node["device"],
                            node["inode"],
                            node["mtime_ns"],
                            node["ctime_ns"],
                        ) = current_generation
                    active_node = node
                    if (
                        node["complete"]
                        and not node["retry"]
                        and not node["rescan"]
                    ):
                        append_node(node)
                        queued_paths.add(relative_directory)
                        continue
                    processed = 0
                    blocked_on_retry = False
                    if retry and scanned < max_entries:
                        retry_name = retry[0]
                        try:
                            charge_recovery_scan()
                        except MaintenanceBudgetExceeded as exc:
                            budget_error = exc
                        else:
                            retry.pop(0)
                            scanned += 1
                            processed += 1
                            try:
                                retry_again, timed_out = inspect_name(
                                    parent_descriptor,
                                    relative_directory,
                                    retry_name,
                                )
                            except MaintenanceBudgetExceeded as exc:
                                retry.insert(0, retry_name)
                                budget_error = exc
                            else:
                                if retry_again:
                                    retry.append(retry_name)
                                    blocked_on_retry = True
                                if timed_out:
                                    node["retry"] = retry
                                    append_node(node)
                                    queued_paths.add(relative_directory)
                                    break

                    batch_names: tuple[str, ...] = ()
                    batch_next_offset = int(node["offset"])
                    while (
                        budget_error is None
                        and
                        not blocked_on_retry
                        and
                        processed < _UNLINK_RECOVERY_DIRECTORY_QUANTUM
                        and scanned < max_entries
                        and time.monotonic() < recovery_deadline
                    ):
                        if node["complete"]:
                            break
                        batch_index = int(node["batch_index"])
                        if not batch_names:
                            try:
                                (
                                    batch_names,
                                    batch_next_offset,
                                    complete,
                                ) = read_directory_batch(
                                    parent_descriptor,
                                    int(node["offset"]),
                                )
                            except OSError:
                                if (
                                    int(node["offset"]) == 0
                                    and int(node["batch_index"]) == 0
                                ):
                                    raise
                                node["offset"] = 0
                                node["batch_index"] = 0
                                node["complete"] = False
                                node["rescan"] = False
                                break
                            if batch_index > len(batch_names):
                                node["offset"] = 0
                                node["batch_index"] = 0
                                node["rescan"] = False
                                batch_names = ()
                                continue
                            if not batch_names:
                                if complete:
                                    node["offset"] = batch_next_offset
                                    node["batch_index"] = 0
                                    node["complete"] = True
                                    break
                                node["offset"] = batch_next_offset
                                node["batch_index"] = 0
                                continue
                        if batch_index == len(batch_names):
                            node["offset"] = batch_next_offset
                            node["batch_index"] = 0
                            batch_names = ()
                            continue
                        name = batch_names[batch_index]
                        if (
                            not relative_directory
                            and name
                            == _UNLINK_RECOVERY_CONTROL_DIRECTORY
                        ):
                            node["batch_index"] = batch_index + 1
                            continue
                        try:
                            charge_recovery_scan()
                        except MaintenanceBudgetExceeded as exc:
                            budget_error = exc
                            break
                        node["batch_index"] = batch_index + 1
                        scanned += 1
                        processed += 1
                        try:
                            retry_again, timed_out = inspect_name(
                                parent_descriptor,
                                relative_directory,
                                name,
                            )
                        except MaintenanceBudgetExceeded as exc:
                            if name not in retry:
                                retry.append(name)
                            budget_error = exc
                            break
                        if retry_again and name not in retry:
                            retry.append(name)
                            blocked_on_retry = True
                        if timed_out:
                            break

                    if (
                        batch_names
                        and int(node["batch_index"]) == len(batch_names)
                    ):
                        node["offset"] = batch_next_offset
                        node["batch_index"] = 0
                    post_metadata = os.fstat(parent_descriptor)
                    post_generation = _directory_generation(post_metadata)
                    if post_generation != current_generation:
                        node["rescan"] = True
                    (
                        node["device"],
                        node["inode"],
                        node["mtime_ns"],
                        node["ctime_ns"],
                    ) = post_generation
                    node["retry"] = retry
                    if (
                        node["complete"]
                        and node["rescan"]
                    ):
                        node["offset"] = 0
                        node["batch_index"] = 0
                        node["complete"] = False
                        node["rescan"] = False
                    append_node(node)
                    queued_paths.add(relative_directory)
            except FileNotFoundError:
                continue
            except (DurabilityUnsupportedError, OSError, ValueError) as exc:
                unresolved += 1
                errors.append(
                    f"could not scan durable-unlink directory {directory}: {exc}"
                )
                append_node(node)
                queued_paths.add(relative_directory)
            if budget_error is not None:
                break

        while queued_cursor_size() > _UNLINK_RECOVERY_CURSOR_MAX_BYTES:
            if not evict_completed_node():
                break
        try:
            _write_recovery_cursor(
                root_descriptor,
                root_identity,
                nodes,
                budget=budget,
                mutations_precharged=cursor_mutations_precharged,
                create_control=cursor_control_missing,
            )
        except OSError as exc:
            unresolved += 1
            errors.append(
                "could not persist durable-unlink recovery cursor: "
                f"{exc}"
            )
        if budget_error is not None:
            errors.append(str(budget_error))

    truncated = budget_error is not None or any(
        not node["complete"] or node["retry"] for node in nodes
    )
    if (
        scanned >= max_entries
        or time.monotonic() >= recovery_deadline
    ) and nodes:
        truncated = True

    return DurableUnlinkRecoveryReport(
        scanned=scanned,
        restored=0,
        cleaned=cleaned,
        unresolved=unresolved,
        truncated=truncated,
        errors=tuple(errors),
        budget_exhausted=budget_error is not None,
    )
