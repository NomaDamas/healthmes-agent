#!/usr/bin/env python3
"""Portable native process identity and script digest helper.

The launcher uses this stdlib-only helper before the HealthMes virtual
environment is guaranteed to exist. Exit statuses are part of the shell
contract:

0 = identity captured or matched
3 = process is absent
4 = numeric PID exists but names a different process
5 = identity cannot be proved on this platform
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import stat
import sys
from pathlib import Path

_DARWIN_PROC_PIDTBSDINFO = 3
_DARWIN_MAXCOMLEN = 16


class _IdentityUnavailable(RuntimeError):
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
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        if args.action == "sha256":
            print(_sha256_regular_file(args.path))
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
    except _IdentityUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
