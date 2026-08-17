#!/usr/bin/env python3
"""Portable native process identity, digest, and filesystem helper.

The launcher uses this stdlib-only helper before the HealthMes virtual
environment is guaranteed to exist. Exit statuses are part of the shell
contract:

0 = identity captured or matched
3 = process is absent
4 = numeric PID exists but names a different process
5 = identity cannot be proved on this platform
6 = an exclusive filesystem publication target already exists
7 = the source generation changed before an atomic transition
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import math
import os
import selectors
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - supported hosts provide fcntl.
    fcntl = None  # type: ignore[assignment]

_DARWIN_PROC_PIDTBSDINFO = 3
_DARWIN_MAXCOMLEN = 16
_AT_FDCWD = -100
_LINUX_RENAME_NOREPLACE = 1
_DARWIN_RENAME_EXCL = 0x00000004
_MAX_PS_OUTPUT_BYTES = 64 * 1024


class _IdentityUnavailable(RuntimeError):
    pass


class _PublicationConflict(RuntimeError):
    pass


class _GenerationChanged(RuntimeError):
    pass


class _ProcessAbsent(RuntimeError):
    pass


class _PsOutputOversized(RuntimeError):
    pass


class _DarwinProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * _DARWIN_MAXCOMLEN),
        ("pbi_name", ctypes.c_char * (2 * _DARWIN_MAXCOMLEN)),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def _linux_start_token(pid: int) -> str | None:
    try:
        payload = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _IdentityUnavailable(
            "native_identity_linux_proc_unreadable"
        ) from exc
    closing_parenthesis = payload.rfind(")")
    fields = payload[closing_parenthesis + 1 :].split()
    try:
        start_ticks = int(fields[19])
    except (IndexError, ValueError) as exc:
        raise _IdentityUnavailable(
            "native_identity_linux_proc_invalid"
        ) from exc
    if closing_parenthesis < 1 or start_ticks < 1:
        raise _IdentityUnavailable(
            "native_identity_linux_proc_invalid"
        )
    return f"linux:{start_ticks}"


def _darwin_start_token(pid: int) -> str | None:
    try:
        library = ctypes.CDLL(
            "/usr/lib/libproc.dylib",
            use_errno=True,
        )
    except OSError as exc:
        raise _IdentityUnavailable(
            "native_identity_darwin_libproc_unavailable"
        ) from exc
    proc_pidinfo = library.proc_pidinfo
    proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    proc_pidinfo.restype = ctypes.c_int
    information = _DarwinProcBsdInfo()
    size = ctypes.sizeof(information)
    ctypes.set_errno(0)
    result = proc_pidinfo(
        pid,
        _DARWIN_PROC_PIDTBSDINFO,
        0,
        ctypes.byref(information),
        size,
    )
    if result != size:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError as exc:
            raise _IdentityUnavailable(
                "native_identity_darwin_process_unreadable"
            ) from exc
        raise _IdentityUnavailable(
            "native_identity_darwin_process_unreadable"
        )
    if (
        information.pbi_pid != pid
        or information.pbi_start_tvsec < 1
        or information.pbi_start_tvusec > 999_999
    ):
        raise _IdentityUnavailable(
            "native_identity_darwin_process_invalid"
        )
    return (
        "darwin:"
        f"{information.pbi_start_tvsec}:"
        f"{information.pbi_start_tvusec:06d}"
    )


def _start_token(pid: int) -> str | None:
    if pid <= 1:
        raise _IdentityUnavailable("native_identity_pid_invalid")
    if sys.platform.startswith("linux"):
        return _linux_start_token(pid)
    if sys.platform == "darwin":
        return _darwin_start_token(pid)
    raise _IdentityUnavailable(
        f"native_identity_platform_unsupported:{sys.platform}"
    )


def _sha256_regular_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _IdentityUnavailable(
            "native_identity_digest_file_unavailable"
        ) from exc
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise _IdentityUnavailable(
                "native_identity_digest_file_invalid"
            )
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _read_bounded_regular_file(
    path: Path,
    max_bytes: int,
    *,
    require_ascii_text: bool,
) -> bytes:
    if max_bytes < 1:
        raise _IdentityUnavailable(
            "native_identity_bounded_read_limit_invalid"
        )
    try:
        parent_metadata = path.parent.lstat()
    except OSError as exc:
        raise _IdentityUnavailable(
            "native_identity_bounded_read_parent_unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.geteuid()
    ):
        raise _IdentityUnavailable(
            "native_identity_bounded_read_parent_invalid"
        )
    try:
        candidate = path.lstat()
    except FileNotFoundError as exc:
        raise _ProcessAbsent(
            "native_identity_bounded_read_file_absent"
        ) from exc
    except OSError as exc:
        raise _IdentityUnavailable(
            "native_identity_bounded_read_file_unavailable"
        ) from exc
    if (
        not stat.S_ISREG(candidate.st_mode)
        or candidate.st_uid != os.geteuid()
        or candidate.st_nlink != 1
    ):
        raise _IdentityUnavailable(
            "native_identity_bounded_read_file_invalid"
        )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    elif os.path.lexists(path):
        try:
            if stat.S_ISLNK(path.lstat().st_mode):
                raise _IdentityUnavailable(
                    "native_identity_bounded_read_file_invalid"
                )
        except OSError as exc:
            raise _IdentityUnavailable(
                "native_identity_bounded_read_file_unavailable"
            ) from exc
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise _ProcessAbsent(
            "native_identity_bounded_read_file_absent"
        ) from exc
    except OSError as exc:
        raise _IdentityUnavailable(
            "native_identity_bounded_read_file_unavailable"
        ) from exc
    try:
        try:
            opened = os.fstat(descriptor)
            current = path.lstat()
        except OSError as exc:
            raise _IdentityUnavailable(
                "native_identity_bounded_read_file_unavailable"
            ) from exc
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or opened.st_uid != os.geteuid()
            or current.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or current.st_nlink != 1
            or opened.st_dev != current.st_dev
            or opened.st_ino != current.st_ino
            or opened.st_size != current.st_size
            or opened.st_size < 1
            or opened.st_size > max_bytes
        ):
            raise _IdentityUnavailable(
                "native_identity_bounded_read_file_invalid"
            )
        payload = bytearray()
        while len(payload) <= max_bytes:
            chunk = os.read(
                descriptor,
                min(4096, max_bytes + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
        try:
            verified = os.fstat(descriptor)
            final_path = path.lstat()
        except OSError as exc:
            raise _IdentityUnavailable(
                "native_identity_bounded_read_file_unavailable"
            ) from exc
        if (
            len(payload) != opened.st_size
            or len(payload) > max_bytes
            or verified.st_dev != opened.st_dev
            or verified.st_ino != opened.st_ino
            or verified.st_size != opened.st_size
            or final_path.st_dev != opened.st_dev
            or final_path.st_ino != opened.st_ino
            or final_path.st_size != opened.st_size
            or not stat.S_ISREG(final_path.st_mode)
            or final_path.st_uid != os.geteuid()
            or final_path.st_nlink != 1
        ):
            raise _IdentityUnavailable(
                "native_identity_bounded_read_file_changed"
            )
        result = bytes(payload)
        if require_ascii_text:
            try:
                result.decode("ascii")
            except UnicodeDecodeError as exc:
                raise _IdentityUnavailable(
                    "native_identity_bounded_read_text_invalid"
                ) from exc
            if b"\x00" in result or not result.endswith(b"\n"):
                raise _IdentityUnavailable(
                    "native_identity_bounded_read_text_invalid"
                )
        return result
    finally:
        os.close(descriptor)


def _native_process_existence_state(pid: int) -> str:
    try:
        token = _start_token(pid)
    except _IdentityUnavailable:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "gone"
        except PermissionError:
            return "live"
        except OSError as exc:
            raise _IdentityUnavailable(
                "native_identity_process_existence_unavailable"
            ) from exc
        return "live"
    return "gone" if token is None else "live"


def _terminate_ps_probe(
    process: subprocess.Popen[bytes],
    deadline: float,
) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()
    remaining = deadline - time.monotonic()
    if remaining > 0:
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            pass
    else:
        process.poll()


def _collect_bounded_ps_output(
    process: subprocess.Popen[bytes],
    deadline: float,
) -> tuple[bytes, bytes]:
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    streams = (
        (process.stdout, stdout),
        (process.stderr, stderr),
    )
    try:
        for stream, target in streams:
            if stream is None:
                continue
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, target)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, 0)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(process.args, remaining)
            for key, _ in events:
                stream = key.fileobj
                target = key.data
                try:
                    chunk = os.read(stream.fileno(), 4096)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                target.extend(chunk)
                if len(stdout) + len(stderr) > _MAX_PS_OUTPUT_BYTES:
                    raise _PsOutputOversized
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, 0)
        process.wait(timeout=remaining)
        return bytes(stdout), bytes(stderr)
    finally:
        selector.close()


def _bounded_ps_value(
    *,
    ps_bin: str,
    pid: int,
    field: str,
    timeout_seconds: float,
) -> str:
    if (
        pid <= 1
        or field not in {"pid", "pgid", "comm", "lstart", "command"}
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or timeout_seconds > 10
    ):
        raise _IdentityUnavailable(
            "native_identity_ps_probe_arguments_invalid"
        )
    environment = dict(os.environ)
    environment["LANG"] = "C"
    environment["LC_ALL"] = "C"
    try:
        process = subprocess.Popen(
            [ps_bin, "-ww", "-p", str(pid), "-o", f"{field}="],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
    except OSError as exc:
        raise _IdentityUnavailable(
            "native_identity_ps_probe_unavailable"
        ) from exc
    deadline = time.monotonic() + timeout_seconds
    cleanup_reserve = min(0.1, timeout_seconds / 4)
    try:
        stdout_bytes, stderr_bytes = _collect_bounded_ps_output(
            process,
            deadline - cleanup_reserve,
        )
    except subprocess.TimeoutExpired as exc:
        _terminate_ps_probe(process, deadline)
        if _native_process_existence_state(pid) == "gone":
            raise _ProcessAbsent(
                "native_identity_ps_process_absent"
            ) from exc
        raise _IdentityUnavailable(
            "native_identity_ps_probe_timeout"
        ) from exc
    except _PsOutputOversized as exc:
        _terminate_ps_probe(process, deadline)
        raise _IdentityUnavailable(
            "native_identity_ps_output_oversized"
        ) from exc
    try:
        output = stdout_bytes.decode("utf-8").strip()
        error = stderr_bytes.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise _IdentityUnavailable(
            "native_identity_ps_output_invalid"
        ) from exc
    if process.returncode != 0:
        if _native_process_existence_state(pid) == "gone":
            raise _ProcessAbsent("native_identity_ps_process_absent")
        raise _IdentityUnavailable(
            "native_identity_ps_probe_failed"
        )
    if (
        error
        or not output
        or "\n" in output
        or "\r" in output
        or "\t" in output
    ):
        raise _IdentityUnavailable(
            "native_identity_ps_output_invalid"
        )
    return output


def _valid_identity_nonce(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 128
        and value.isascii()
        and all(character.isalnum() or character == "-" for character in value)
    )


def _valid_start_token(
    value: str,
    *,
    prefixes: tuple[str, ...],
) -> bool:
    return (
        bool(value)
        and len(value) <= 256
        and value.isascii()
        and value.startswith(prefixes)
        and "\t" not in value
        and "\n" not in value
        and "\r" not in value
    )


def _parse_shutdown_budget_payload(
    payload: bytes,
    *,
    max_bytes: int,
    max_drain_seconds: int,
) -> None:
    if (
        max_bytes < 1
        or max_drain_seconds < 1
        or not payload
        or len(payload) > max_bytes
        or not payload.endswith(b"\n")
        or b"\r" in payload
    ):
        raise _IdentityUnavailable(
            "native_identity_shutdown_budget_invalid"
        )
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise _IdentityUnavailable(
            "native_identity_shutdown_budget_invalid"
        ) from exc
    lines = text[:-1].split("\n")
    if not lines or any(not line for line in lines):
        raise _IdentityUnavailable(
            "native_identity_shutdown_budget_invalid"
        )
    fields: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("\t")
        if not separator or key in fields:
            raise _IdentityUnavailable(
                "native_identity_shutdown_budget_invalid"
            )
        fields[key] = value
    version = fields.get("version")
    if version == "3":
        expected = {
            "version",
            "drain_timeout_seconds",
            "launcher_pid",
            "launcher_start_token",
            "launcher_service_nonce",
            "supervisor_pid",
            "supervisor_start_token",
            "publication_instance_nonce",
        }
    elif version in {"1", "2"}:
        expected = {
            "version",
            "drain_timeout_seconds",
            "supervisor_pid",
            "supervisor_start_token",
            "service_nonce",
        }
        if version == "2":
            expected.add("publication_instance_nonce")
    else:
        raise _IdentityUnavailable(
            "native_identity_shutdown_budget_invalid"
        )
    if set(fields) != expected:
        raise _IdentityUnavailable(
            "native_identity_shutdown_budget_invalid"
        )
    try:
        drain_timeout = int(fields["drain_timeout_seconds"])
        supervisor_pid = int(fields["supervisor_pid"])
        launcher_pid = (
            int(fields["launcher_pid"])
            if version == "3"
            else supervisor_pid
        )
    except ValueError as exc:
        raise _IdentityUnavailable(
            "native_identity_shutdown_budget_invalid"
        ) from exc
    if (
        not 1 <= drain_timeout <= max_drain_seconds
        or launcher_pid < 1
        or supervisor_pid < 1
    ):
        raise _IdentityUnavailable(
            "native_identity_shutdown_budget_invalid"
        )
    launcher_start_token = (
        fields["launcher_start_token"]
        if version == "3"
        else fields["supervisor_start_token"]
    )
    launcher_service_nonce = (
        fields["launcher_service_nonce"]
        if version == "3"
        else fields["service_nonce"]
    )
    if (
        not _valid_start_token(
            launcher_start_token,
            prefixes=("linux:", "darwin:", "ps:"),
        )
        or not _valid_identity_nonce(launcher_service_nonce)
        or not _valid_start_token(
            fields["supervisor_start_token"],
            prefixes=(
                ("linux:", "darwin:")
                if version == "3"
                else ("linux:", "darwin:", "ps:")
            ),
        )
    ):
        raise _IdentityUnavailable(
            "native_identity_shutdown_budget_invalid"
        )
    publication_nonce = fields.get("publication_instance_nonce")
    if publication_nonce is not None and not _valid_identity_nonce(
        publication_nonce
    ):
        raise _IdentityUnavailable(
            "native_identity_shutdown_budget_invalid"
        )


def _bounded_ps_snapshot(
    *,
    ps_bin: str,
    pid: int,
    timeout_seconds: float,
) -> tuple[str, str, str, str, str]:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise _IdentityUnavailable(
            "native_identity_ps_probe_arguments_invalid"
        )
    deadline = time.monotonic() + timeout_seconds
    values: list[str] = []
    for field in ("pid", "pgid", "comm", "lstart", "command"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _IdentityUnavailable(
                "native_identity_ps_probe_timeout"
            )
        values.append(
            _bounded_ps_value(
                ps_bin=ps_bin,
                pid=pid,
                field=field,
                timeout_seconds=remaining,
            )
        )
    return (
        values[0],
        values[1],
        values[2],
        values[3],
        values[4],
    )


def _validate_sha256(value: str) -> None:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise _IdentityUnavailable(
            "native_identity_expected_record_digest_invalid"
        )


@contextmanager
def _exclusive_transition_lock(path: Path) -> Iterator[None]:
    if fcntl is None:
        raise _IdentityUnavailable(
            "native_identity_transition_lock_unsupported"
        )
    flags = (
        os.O_CREAT
        | os.O_RDWR
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise _IdentityUnavailable(
            "native_identity_transition_lock_unavailable"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise _IdentityUnavailable(
                "native_identity_transition_lock_invalid"
            )
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except OSError as exc:
        raise _IdentityUnavailable(
            "native_identity_transition_lock_unavailable"
        ) from exc
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _rename_path_exclusive(
    source: Path,
    target: Path,
    *,
    require_directory: bool,
) -> None:
    """Atomically rename a managed path without replacing the target."""
    try:
        source_metadata = source.lstat()
    except OSError as exc:
        raise _IdentityUnavailable(
            "native_identity_rename_source_unavailable"
        ) from exc
    source_type_matches = (
        stat.S_ISDIR(source_metadata.st_mode)
        if require_directory
        else stat.S_ISREG(source_metadata.st_mode)
    )
    if not source_type_matches:
        raise _IdentityUnavailable(
            "native_identity_rename_source_invalid"
        )
    if require_directory and source.parent != target.parent:
        raise _IdentityUnavailable(
            "native_identity_rename_cross_parent"
        )

    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        try:
            rename = library.renameat2
        except AttributeError as exc:
            raise _IdentityUnavailable(
                "native_identity_rename_exclusive_unavailable"
            ) from exc
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            _AT_FDCWD,
            source_bytes,
            _AT_FDCWD,
            target_bytes,
            _LINUX_RENAME_NOREPLACE,
        )
    elif sys.platform == "darwin":
        try:
            rename = library.renamex_np
        except AttributeError as exc:
            raise _IdentityUnavailable(
                "native_identity_rename_exclusive_unavailable"
            ) from exc
        rename.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            source_bytes,
            target_bytes,
            _DARWIN_RENAME_EXCL,
        )
    else:
        raise _IdentityUnavailable(
            f"native_identity_platform_unsupported:{sys.platform}"
        )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise _PublicationConflict(
            "native_identity_rename_target_exists"
        )
    raise _IdentityUnavailable(
        f"native_identity_rename_exclusive_failed:{error}"
    )


def _rename_directory_transaction(
    source: Path,
    target: Path,
    *,
    lock_path: Path,
    expected_record_sha256: str | None,
) -> None:
    if not lock_path.parent.is_dir():
        raise _IdentityUnavailable(
            "native_identity_transition_lock_parent_unavailable"
        )
    with _exclusive_transition_lock(lock_path):
        if expected_record_sha256 is not None:
            _validate_sha256(expected_record_sha256)
            try:
                current_digest = _sha256_regular_file(source / "record")
            except _IdentityUnavailable as exc:
                raise _GenerationChanged(
                    "native_identity_record_generation_changed"
                ) from exc
            if current_digest != expected_record_sha256:
                raise _GenerationChanged(
                    "native_identity_record_generation_changed"
                )
        _rename_path_exclusive(
            source,
            target,
            require_directory=True,
        )


def _replace_record_transaction(
    source: Path,
    target: Path,
    *,
    lock_path: Path,
    expected_record_sha256: str,
) -> None:
    if not lock_path.parent.is_dir():
        raise _IdentityUnavailable(
            "native_identity_transition_lock_parent_unavailable"
        )
    _validate_sha256(expected_record_sha256)
    with _exclusive_transition_lock(lock_path):
        try:
            current_digest = _sha256_regular_file(target)
        except _IdentityUnavailable as exc:
            raise _GenerationChanged(
                "native_identity_record_generation_changed"
            ) from exc
        if current_digest != expected_record_sha256:
            raise _GenerationChanged(
                "native_identity_record_generation_changed"
            )
        try:
            source_metadata = source.lstat()
        except OSError as exc:
            raise _IdentityUnavailable(
                "native_identity_record_source_unavailable"
            ) from exc
        if not stat.S_ISREG(source_metadata.st_mode):
            raise _IdentityUnavailable(
                "native_identity_record_source_invalid"
            )
        try:
            os.replace(source, target)
        except OSError as exc:
            raise _IdentityUnavailable(
                "native_identity_record_replace_failed"
            ) from exc


def _publish_record_transaction(
    source: Path,
    target: Path,
    *,
    lock_path: Path,
    expected_source_sha256: str,
) -> None:
    if not lock_path.parent.is_dir():
        raise _IdentityUnavailable(
            "native_identity_transition_lock_parent_unavailable"
        )
    _validate_sha256(expected_source_sha256)
    with _exclusive_transition_lock(lock_path):
        try:
            current_digest = _sha256_regular_file(source)
        except _IdentityUnavailable as exc:
            raise _GenerationChanged(
                "native_identity_record_generation_changed"
            ) from exc
        if current_digest != expected_source_sha256:
            raise _GenerationChanged(
                "native_identity_record_generation_changed"
            )
        _rename_path_exclusive(
            source,
            target,
            require_directory=False,
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    subparsers = parser.add_subparsers(dest="action", required=True)
    capture = subparsers.add_parser("capture", add_help=False)
    capture.add_argument("pid", type=int)
    check = subparsers.add_parser("check", add_help=False)
    check.add_argument("pid", type=int)
    check.add_argument("expected")
    digest = subparsers.add_parser("sha256", add_help=False)
    digest.add_argument("path", type=Path)
    rename = subparsers.add_parser("rename-exclusive", add_help=False)
    rename.add_argument("source", type=Path)
    rename.add_argument("target", type=Path)
    rename.add_argument("--lock-path", required=True, type=Path)
    rename.add_argument("--expected-record-sha256")
    replace_record = subparsers.add_parser(
        "replace-record",
        add_help=False,
    )
    replace_record.add_argument("source", type=Path)
    replace_record.add_argument("target", type=Path)
    replace_record.add_argument(
        "--lock-path",
        required=True,
        type=Path,
    )
    replace_record.add_argument(
        "--expected-record-sha256",
        required=True,
    )
    publish_record = subparsers.add_parser(
        "publish-record",
        add_help=False,
    )
    publish_record.add_argument("source", type=Path)
    publish_record.add_argument("target", type=Path)
    publish_record.add_argument(
        "--lock-path",
        required=True,
        type=Path,
    )
    publish_record.add_argument(
        "--expected-source-sha256",
        required=True,
    )
    bounded_read = subparsers.add_parser(
        "read-bounded",
        add_help=False,
    )
    bounded_read.add_argument("path", type=Path)
    bounded_read.add_argument(
        "--max-bytes",
        required=True,
        type=int,
    )
    bounded_read.add_argument(
        "--require-ascii-text",
        action="store_true",
    )
    ps_value = subparsers.add_parser("ps-value", add_help=False)
    ps_value.add_argument("--ps-bin", required=True)
    ps_value.add_argument("--pid", required=True, type=int)
    ps_value.add_argument("--field", required=True)
    ps_value.add_argument(
        "--timeout-seconds",
        required=True,
        type=float,
    )
    ps_snapshot = subparsers.add_parser(
        "ps-snapshot",
        add_help=False,
    )
    ps_snapshot.add_argument("--ps-bin", required=True)
    ps_snapshot.add_argument("--pid", required=True, type=int)
    ps_snapshot.add_argument(
        "--timeout-seconds",
        required=True,
        type=float,
    )
    shutdown_budget = subparsers.add_parser(
        "read-shutdown-budget",
        add_help=False,
    )
    shutdown_budget.add_argument("path", type=Path)
    shutdown_budget.add_argument(
        "--max-bytes",
        required=True,
        type=int,
    )
    shutdown_budget.add_argument(
        "--max-drain-seconds",
        required=True,
        type=int,
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        if args.action == "sha256":
            print(_sha256_regular_file(args.path))
            return 0
        if args.action == "rename-exclusive":
            _rename_directory_transaction(
                args.source,
                args.target,
                lock_path=args.lock_path,
                expected_record_sha256=args.expected_record_sha256,
            )
            return 0
        if args.action == "replace-record":
            _replace_record_transaction(
                args.source,
                args.target,
                lock_path=args.lock_path,
                expected_record_sha256=args.expected_record_sha256,
            )
            return 0
        if args.action == "publish-record":
            _publish_record_transaction(
                args.source,
                args.target,
                lock_path=args.lock_path,
                expected_source_sha256=args.expected_source_sha256,
            )
            return 0
        if args.action == "read-bounded":
            sys.stdout.buffer.write(
                _read_bounded_regular_file(
                    args.path,
                    args.max_bytes,
                    require_ascii_text=args.require_ascii_text,
                )
            )
            return 0
        if args.action == "ps-value":
            print(
                _bounded_ps_value(
                    ps_bin=args.ps_bin,
                    pid=args.pid,
                    field=args.field,
                    timeout_seconds=args.timeout_seconds,
                )
            )
            return 0
        if args.action == "ps-snapshot":
            for field, value in zip(
                ("pid", "pgid", "comm", "lstart", "command"),
                _bounded_ps_snapshot(
                    ps_bin=args.ps_bin,
                    pid=args.pid,
                    timeout_seconds=args.timeout_seconds,
                ),
                strict=True,
            ):
                print(f"{field}\t{value}")
            return 0
        if args.action == "read-shutdown-budget":
            payload = _read_bounded_regular_file(
                args.path,
                args.max_bytes,
                require_ascii_text=True,
            )
            _parse_shutdown_budget_payload(
                payload,
                max_bytes=args.max_bytes,
                max_drain_seconds=args.max_drain_seconds,
            )
            sys.stdout.buffer.write(payload)
            return 0
        current = _start_token(args.pid)
        if current is None:
            return 3
        if args.action == "capture":
            print(current)
            return 0
        if args.expected not in {current}:
            return 4
        return 0
    except _PublicationConflict as exc:
        print(str(exc), file=sys.stderr)
        return 6
    except _GenerationChanged as exc:
        print(str(exc), file=sys.stderr)
        return 7
    except _ProcessAbsent as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except _IdentityUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
