"""HealthMes-owned supervisor and narrow proxy for one Hermes decision runtime."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import errno
import fcntl
import hashlib
import hmac
import json
import math
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Mapping,
)
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from types import FrameType
from typing import Any, Literal, Protocol
from urllib.parse import quote

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from pydantic import TypeAdapter, ValidationError
from starlette.types import Receive, Scope, Send

from healthmes.hermes_mcp_inventory import (
    HERMES_DECISION_MCP_TOOL_NAMES,
    HermesMcpInventoryError,
    HermesMcpToolInventory,
    schema_digests_from_mcp_tools,
    validate_model_visible_mcp_inventory,
)
from healthmes.hermes_runtime_identity import (
    HERMES_RUNTIME_ATTESTATION_PATH,
    HERMES_RUNTIME_HEALTH_PATH,
    HERMES_RUNTIME_PROVIDER_ENV_NAMES,
    HermesDecisionRuntimeManifest,
    HermesRuntimeBootIdentity,
    HermesRuntimeIdentityError,
    HermesRuntimeMcpConnection,
    capture_runtime_boot_identity,
    load_runtime_mcp_connection,
    seal_supervised_runtime,
    sign_runtime_attestation,
    validate_supervised_runtime,
)

_MAX_REQUEST_BYTES = 2_000_000
_FORWARDED_RESPONSE_HEADERS = frozenset(
    {
        "cache-control",
        "content-type",
        "x-hermes-session-id",
    }
)
McpInventoryProbe = Callable[
    [HermesRuntimeMcpConnection, float],
    Awaitable[Mapping[str, str]],
]
_LifecycleState = Literal[
    "new",
    "running",
    "closing",
    "close_failed",
    "closed",
]
_MAX_CHILD_TERM_TIMEOUT_SECONDS = 10.0
_MAX_CHILD_KILL_TIMEOUT_SECONDS = 5.0
_MAX_DECISION_TIMEOUT_SECONDS = 300.0
_MAX_RUNTIME_DRAIN_TIMEOUT_SECONDS = 315
_RUNTIME_SHUTDOWN_BUDGET_VERSION = 3
_MAX_RUNTIME_SHUTDOWN_BUDGET_BYTES = 1024
_PROCESS_GROUP_POLL_INTERVAL_SECONDS = 0.05
_PROCESS_GROUP_PROBE_TIMEOUT_SECONDS = 1.0
_PROXY_CONNECT_TIMEOUT_SECONDS = 5.0
_PROXY_READ_TIMEOUT_SECONDS = 5.0
_PROXY_WRITE_TIMEOUT_SECONDS = 5.0
_PROXY_POOL_TIMEOUT_SECONDS = 5.0
_PYDANTIC_FLOAT = TypeAdapter(float)
# This post-import disk snapshot detects later control-source drift. It is not
# proof that already-loaded Python bytecode came from those exact file bytes.
_SUPERVISOR_BOOT_IDENTITY = capture_runtime_boot_identity()
_DARWIN_PROC_PIDTBSDINFO = 3
_DARWIN_MAXCOMLEN = 16
_DARWIN_PS_PATH = "/bin/ps"


@dataclass(frozen=True, slots=True)
class HermesRuntimeShutdownBudget:
    """Validated wall-clock budget shared by every runtime shutdown layer."""

    decision_timeout_seconds: float
    child_term_timeout_seconds: float
    child_kill_timeout_seconds: float

    @property
    def drain_timeout_seconds(self) -> int:
        return math.ceil(
            self.decision_timeout_seconds
            + self.child_term_timeout_seconds
            + self.child_kill_timeout_seconds
        )


@dataclass(frozen=True, slots=True)
class HermesRuntimeProcessIdentity:
    """Stable native process identity suitable for verified signalling."""

    pid: int
    start_token: str

    def __post_init__(self) -> None:
        if self.pid < 1:
            raise ValueError("runtime process PID must be positive")
        if (
            not self.start_token
            or len(self.start_token) > 256
            or not self.start_token.isascii()
            or not self.start_token.startswith(("linux:", "darwin:"))
            or "\t" in self.start_token
            or "\n" in self.start_token
            or "\r" in self.start_token
        ):
            raise ValueError("runtime process start token is invalid")


@dataclass(frozen=True, slots=True)
class HermesRuntimeLauncherIdentity:
    """Managed launcher identity inherited by the Python supervisor."""

    pid: int
    start_token: str
    service_nonce: str

    def __post_init__(self) -> None:
        if self.pid < 1:
            raise ValueError("runtime launcher PID must be positive")
        if (
            not self.start_token
            or len(self.start_token) > 256
            or not self.start_token.isascii()
            or not self.start_token.startswith(
                ("linux:", "darwin:", "ps:")
            )
            or "\t" in self.start_token
            or "\n" in self.start_token
            or "\r" in self.start_token
        ):
            raise ValueError("runtime launcher start token is invalid")
        if (
            not self.service_nonce
            or len(self.service_nonce) > 128
            or not self.service_nonce.isascii()
            or not all(
                character.isalnum() or character == "-"
                for character in self.service_nonce
            )
        ):
            raise ValueError("runtime launcher service nonce is invalid")


def _validate_identity_nonce(value: str, *, label: str) -> None:
    if (
        not value
        or len(value) > 128
        or not value.isascii()
        or not all(
            character.isalnum() or character == "-"
            for character in value
        )
    ):
        raise ValueError(f"{label} is invalid")


@dataclass(frozen=True, slots=True)
class HermesRuntimeShutdownBudgetRecord:
    """Identity-bound, canonical stop budget published by a live server."""

    drain_timeout_seconds: int
    launcher_pid: int
    launcher_start_token: str
    launcher_service_nonce: str
    supervisor_pid: int
    supervisor_start_token: str
    publication_instance_nonce: str = field(
        default_factory=lambda: secrets.token_hex(16)
    )

    def __post_init__(self) -> None:
        if not 1 <= self.drain_timeout_seconds <= (
            _MAX_RUNTIME_DRAIN_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "runtime drain timeout is outside the supported bound"
            )
        HermesRuntimeLauncherIdentity(
            pid=self.launcher_pid,
            start_token=self.launcher_start_token,
            service_nonce=self.launcher_service_nonce,
        )
        HermesRuntimeProcessIdentity(
            pid=self.supervisor_pid,
            start_token=self.supervisor_start_token,
        )
        _validate_identity_nonce(
            self.publication_instance_nonce,
            label="runtime publication instance nonce",
        )

    @property
    def launcher_identity(self) -> HermesRuntimeLauncherIdentity:
        return HermesRuntimeLauncherIdentity(
            pid=self.launcher_pid,
            start_token=self.launcher_start_token,
            service_nonce=self.launcher_service_nonce,
        )

    @property
    def supervisor_identity(self) -> HermesRuntimeProcessIdentity:
        return HermesRuntimeProcessIdentity(
            pid=self.supervisor_pid,
            start_token=self.supervisor_start_token,
        )

    def to_bytes(self) -> bytes:
        return (
            f"version\t{_RUNTIME_SHUTDOWN_BUDGET_VERSION}\n"
            f"drain_timeout_seconds\t{self.drain_timeout_seconds}\n"
            f"launcher_pid\t{self.launcher_pid}\n"
            f"launcher_start_token\t{self.launcher_start_token}\n"
            "launcher_service_nonce\t"
            f"{self.launcher_service_nonce}\n"
            f"supervisor_pid\t{self.supervisor_pid}\n"
            f"supervisor_start_token\t{self.supervisor_start_token}\n"
            "publication_instance_nonce\t"
            f"{self.publication_instance_nonce}\n"
        ).encode("ascii")


@dataclass(frozen=True, slots=True)
class _LegacyRuntimeShutdownBudgetRecord:
    """Read-only v1/v2 budget used only to protect a live legacy owner."""

    drain_timeout_seconds: int
    supervisor_pid: int
    supervisor_start_token: str
    service_nonce: str
    publication_instance_nonce: str | None = None

    @property
    def launcher_identity(self) -> HermesRuntimeLauncherIdentity:
        return HermesRuntimeLauncherIdentity(
            pid=self.supervisor_pid,
            start_token=self.supervisor_start_token,
            service_nonce=self.service_nonce,
        )


@dataclass(frozen=True, slots=True)
class _ProcessGroupMember:
    """Stable OS identity used to avoid signaling a reused process group."""

    pid: int
    start_token: str


ProcessGroupProbe = Callable[
    [int, float],
    frozenset[_ProcessGroupMember],
]


class _DarwinProcBsdInfo(ctypes.Structure):
    """ABI layout for macOS PROC_PIDTBSDINFO."""

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


def _lock_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.lock")


@contextmanager
def _exclusive_file_lock(path: Path):
    target = path.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        _lock_path(target),
        os.O_CREAT | os.O_RDWR,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@lru_cache(maxsize=1)
def _load_darwin_libproc() -> Any:
    try:
        library = ctypes.CDLL(
            "/usr/lib/libproc.dylib",
            use_errno=True,
        )
    except OSError as exc:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_darwin_identity_unavailable"
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
    return library


def _probe_darwin_process_snapshot(
    pid: int,
) -> tuple[int, str] | None:
    """Return kernel process-group/start identity at microsecond resolution."""

    if pid < 1:
        return None
    information = _DarwinProcBsdInfo()
    size = ctypes.sizeof(information)
    library = _load_darwin_libproc()
    ctypes.set_errno(0)
    result = library.proc_pidinfo(
        pid,
        _DARWIN_PROC_PIDTBSDINFO,
        0,
        ctypes.byref(information),
        size,
    )
    if result != size:
        # A disappearing process is safe to ignore. If the numeric PID still
        # exists, libproc failed to prove which process it names, so callers
        # must fail closed rather than signal it.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError as exc:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_darwin_identity_unavailable"
            ) from exc
        raise HermesRuntimeIdentityError(
            "hermes_runtime_darwin_identity_unavailable"
        )
    if (
        information.pbi_pid != pid
        or information.pbi_pgid < 1
        or information.pbi_start_tvsec < 1
        or information.pbi_start_tvusec > 999_999
    ):
        raise HermesRuntimeIdentityError(
            "hermes_runtime_darwin_identity_invalid"
        )
    return (
        information.pbi_pgid,
        "darwin:"
        f"{information.pbi_start_tvsec}:"
        f"{information.pbi_start_tvusec:06d}",
    )


def _probe_process_start_token(
    pid: int,
    *,
    expected_style: str | None = None,
) -> str | None:
    if (
        expected_style in (None, "linux")
        and sys.platform.startswith("linux")
    ):
        stat_path = Path(f"/proc/{pid}/stat")
        try:
            stat = stat_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_linux_process_identity_unavailable"
            ) from exc
        closing_parenthesis = stat.rfind(")")
        fields = stat[closing_parenthesis + 1 :].split()
        try:
            start_ticks = int(fields[19])
        except (IndexError, ValueError) as exc:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_linux_process_identity_invalid"
            ) from exc
        if closing_parenthesis < 1 or start_ticks < 1:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_linux_process_identity_invalid"
            )
        return f"linux:{start_ticks}"
    if (
        expected_style in (None, "darwin")
        and sys.platform == "darwin"
    ):
        snapshot = _probe_darwin_process_snapshot(pid)
        return snapshot[1] if snapshot is not None else None
    if expected_style not in (None, "ps"):
        return None
    environment = dict(os.environ)
    environment["LC_ALL"] = "C"
    try:
        result = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "lstart="],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            timeout=_PROCESS_GROUP_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    start_time = result.stdout.strip()
    return f"ps:{start_time}" if start_time else None


def capture_runtime_launcher_identity(
    environment: Mapping[str, str],
    *,
    supervisor_identity: HermesRuntimeProcessIdentity,
) -> HermesRuntimeLauncherIdentity:
    """Capture the managed launcher, or bind a direct launch to this process."""

    pid_value = environment.get("HEALTHMES_SERVICE_PID", "").strip()
    start_token = environment.get(
        "HEALTHMES_SERVICE_START_TOKEN",
        "",
    ).strip()
    service_nonce = environment.get("HEALTHMES_SERVICE_NONCE", "").strip()
    inherited = (pid_value, start_token, service_nonce)
    if any(inherited):
        if not all(inherited):
            raise ValueError(
                "runtime supervisor launcher identity is incomplete"
            )
        try:
            pid = int(pid_value)
        except ValueError as exc:
            raise ValueError(
                "runtime launcher PID is invalid"
            ) from exc
        identity = HermesRuntimeLauncherIdentity(
            pid=pid,
            start_token=start_token,
            service_nonce=service_nonce,
        )
    else:
        identity = HermesRuntimeLauncherIdentity(
            pid=supervisor_identity.pid,
            start_token=supervisor_identity.start_token,
            service_nonce=secrets.token_hex(16),
        )
    if not runtime_launcher_identity_is_live(identity):
        raise ValueError("runtime launcher identity is stale")
    return identity


def capture_runtime_supervisor_identity() -> HermesRuntimeProcessIdentity:
    """Capture the actual Python supervisor through native OS identity."""

    pid = os.getpid()
    if sys.platform.startswith("linux"):
        if not Path("/proc/self/stat").is_file():
            raise ValueError(
                "runtime supervisor Linux /proc identity is unavailable"
            )
        if not callable(getattr(os, "pidfd_open", None)) or not callable(
            getattr(signal, "pidfd_send_signal", None)
        ):
            raise ValueError(
                "runtime supervisor Linux pidfd signalling is unavailable"
            )
        token = _probe_process_start_token(pid, expected_style="linux")
    elif sys.platform == "darwin":
        token = _probe_process_start_token(pid, expected_style="darwin")
    else:
        raise ValueError(
            f"runtime supervisor identity is unsupported on {sys.platform}"
        )
    if token is None:
        raise ValueError("runtime supervisor start token is unavailable")
    identity = HermesRuntimeProcessIdentity(pid=pid, start_token=token)
    if _runtime_process_identity_state(identity) != "live":
        raise ValueError("runtime supervisor identity is stale")
    return identity


def runtime_launcher_identity_is_live(
    identity: HermesRuntimeLauncherIdentity,
) -> bool:
    style = identity.start_token.partition(":")[0]
    return hmac.compare_digest(
        _probe_process_start_token(
            identity.pid,
            expected_style=style,
        )
        or "",
        identity.start_token,
    )


def _runtime_process_identity_state(
    identity: HermesRuntimeProcessIdentity,
) -> Literal["live", "gone", "changed"]:
    style = identity.start_token.partition(":")[0]
    if style == "linux":
        if not sys.platform.startswith("linux"):
            raise HermesRuntimeIdentityError(
                "hermes_runtime_supervisor_platform_mismatch"
            )
        if not Path("/proc").is_dir():
            raise HermesRuntimeIdentityError(
                "hermes_runtime_supervisor_proc_unavailable"
            )
    elif style == "darwin":
        if sys.platform != "darwin":
            raise HermesRuntimeIdentityError(
                "hermes_runtime_supervisor_platform_mismatch"
            )
    else:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_supervisor_identity_unsupported"
        )
    current = _probe_process_start_token(
        identity.pid,
        expected_style=style,
    )
    if current is None:
        return "gone"
    if not hmac.compare_digest(current, identity.start_token):
        return "changed"
    return "live"


def runtime_supervisor_identity_is_live(
    identity: HermesRuntimeProcessIdentity,
) -> bool:
    return _runtime_process_identity_state(identity) == "live"


def _shutdown_budget_owner_is_live(
    record: (
        HermesRuntimeShutdownBudgetRecord
        | _LegacyRuntimeShutdownBudgetRecord
    ),
) -> bool:
    if isinstance(record, HermesRuntimeShutdownBudgetRecord):
        return runtime_supervisor_identity_is_live(
            record.supervisor_identity
        )
    return runtime_launcher_identity_is_live(record.launcher_identity)


@dataclass(slots=True)
class _RuntimeShutdownBudgetPublication:
    path: Path
    record: HermesRuntimeShutdownBudgetRecord
    _published: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        # The launcher identity can be inherited by multiple competing child
        # processes. This nonce identifies this exact publication attempt.
        self.record = replace(
            self.record,
            publication_instance_nonce=secrets.token_hex(16),
        )

    def publish(self) -> None:
        if not runtime_launcher_identity_is_live(
            self.record.launcher_identity
        ):
            raise RuntimeError(
                "runtime shutdown budget launcher is not running"
            )
        if not runtime_supervisor_identity_is_live(
            self.record.supervisor_identity
        ):
            raise RuntimeError(
                "runtime shutdown budget owner is not running"
            )
        with _exclusive_file_lock(self.path):
            current = None
            if os.path.lexists(self.path):
                try:
                    current = load_runtime_shutdown_budget(self.path)
                except ValueError as exc:
                    raise RuntimeError(
                        "runtime shutdown budget is malformed; "
                        "refusing to overwrite it without explicit repair"
                    ) from exc
            if (
                current is not None
                and current != self.record
                and _shutdown_budget_owner_is_live(current)
            ):
                raise RuntimeError(
                    "runtime shutdown budget already has a live owner"
                )
            persist_runtime_shutdown_budget(self.path, self.record)
            self._published = True

    def remove_if_owned(self) -> None:
        target = self.path.expanduser()
        with _exclusive_file_lock(target):
            if not self._published:
                return
            try:
                current = load_runtime_shutdown_budget(target)
            except ValueError:
                self._published = False
                return
            if current != self.record:
                self._published = False
                return
            target.unlink(missing_ok=True)
            _fsync_parent(target)
            self._published = False


@dataclass(frozen=True, slots=True)
class HermesRuntimeSupervisorConfig:
    """Filesystem and network identity owned by the supervisor."""

    hermes_home: Path
    manifest_path: Path
    attestation_key_path: Path
    vendor_root: Path
    host: str = "127.0.0.1"
    port: int = 8645
    startup_timeout_seconds: float = 30
    mcp_probe_timeout_seconds: float = 5
    health_check_interval_seconds: float = 2
    health_check_timeout_seconds: float = 1
    unhealthy_threshold: int = 3
    restart_backoff_initial_seconds: float = 0.25
    restart_backoff_max_seconds: float = 5
    max_concurrent_responses: int = 8
    decision_timeout_seconds: float = 60.0
    child_term_timeout_seconds: float = _MAX_CHILD_TERM_TIMEOUT_SECONDS
    child_kill_timeout_seconds: float = _MAX_CHILD_KILL_TIMEOUT_SECONDS
    shutdown_budget_path: Path | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65_535:
            raise ValueError("invalid runtime port")
        durations = (
            ("startup timeout", self.startup_timeout_seconds),
            ("MCP probe timeout", self.mcp_probe_timeout_seconds),
            (
                "health check interval",
                self.health_check_interval_seconds,
            ),
            ("health check timeout", self.health_check_timeout_seconds),
            (
                "restart backoff initial",
                self.restart_backoff_initial_seconds,
            ),
            (
                "restart backoff max",
                self.restart_backoff_max_seconds,
            ),
            ("child TERM timeout", self.child_term_timeout_seconds),
            ("child KILL timeout", self.child_kill_timeout_seconds),
            ("decision timeout", self.decision_timeout_seconds),
        )
        for label, value in durations:
            if not math.isfinite(value) or value <= 0:
                raise ValueError(
                    f"{label} must be finite and positive"
                )
        if self.unhealthy_threshold < 1:
            raise ValueError("unhealthy threshold must be positive")
        if not 1 <= self.max_concurrent_responses <= 128:
            raise ValueError(
                "max concurrent responses must be between 1 and 128"
            )
        if (
            self.child_term_timeout_seconds
            > _MAX_CHILD_TERM_TIMEOUT_SECONDS
        ):
            raise ValueError("child TERM timeout must be at most 10 seconds")
        if (
            self.child_kill_timeout_seconds
            > _MAX_CHILD_KILL_TIMEOUT_SECONDS
        ):
            raise ValueError("child KILL timeout must be at most 5 seconds")
        if self.decision_timeout_seconds > _MAX_DECISION_TIMEOUT_SECONDS:
            raise ValueError("decision timeout must be at most 300 seconds")
        if (
            self.restart_backoff_max_seconds
            < self.restart_backoff_initial_seconds
        ):
            raise ValueError(
                "restart backoff max must be at least the initial delay"
            )

    @property
    def shutdown_budget(self) -> HermesRuntimeShutdownBudget:
        return HermesRuntimeShutdownBudget(
            decision_timeout_seconds=self.decision_timeout_seconds,
            child_term_timeout_seconds=self.child_term_timeout_seconds,
            child_kill_timeout_seconds=self.child_kill_timeout_seconds,
        )


@dataclass(frozen=True, slots=True)
class HermesRuntimeState:
    """Validated immutable state shared by attestation and proxy routes."""

    manifest: HermesDecisionRuntimeManifest
    attestation_key: bytes
    api_key: str
    mcp_inventory: HermesMcpToolInventory


@dataclass(slots=True)
class HermesRuntimeResponseLease:
    """Hold one verified child generation for a complete Responses stream."""

    state: HermesRuntimeState
    generation: int
    _release_callback: Callable[[], Awaitable[None]]
    _release_task: asyncio.Task[BaseException | None] | None = None

    async def release(self) -> None:
        """Release the child generation exactly once."""

        task = self._release_task
        if task is None:
            task = asyncio.create_task(
                self._finish_release(),
                name="healthmes-hermes-response-lease-release",
            )
            self._release_task = task
        caller_cancelled, release_error = await _await_teardown_task(task)
        if release_error is not None:
            raise release_error
        if caller_cancelled:
            raise asyncio.CancelledError

    async def _finish_release(self) -> BaseException | None:
        try:
            await self._release_callback()
        except BaseException as exc:
            return exc
        return None


@dataclass(slots=True)
class _ProxyResponseResources:
    """Own one upstream stream, client, and optional generation lease."""

    client: httpx.AsyncClient
    response_lease: HermesRuntimeResponseLease | None = None
    upstream: httpx.Response | None = None
    _cleanup_task: asyncio.Task[BaseException | None] | None = None

    async def aclose(self) -> None:
        """Close every response resource exactly once despite cancellation."""

        task = self._cleanup_task
        if task is None:
            task = asyncio.create_task(
                self._finish_close(),
                name="healthmes-hermes-proxy-response-close",
            )
            self._cleanup_task = task
        caller_cancelled, cleanup_error = await _await_teardown_task(task)
        if cleanup_error is not None:
            raise cleanup_error
        if caller_cancelled:
            raise asyncio.CancelledError

    async def _finish_close(self) -> BaseException | None:
        first_error: BaseException | None = None
        upstream = self.upstream
        if upstream is not None:
            try:
                await upstream.aclose()
            except BaseException as exc:
                first_error = exc
        try:
            await self.client.aclose()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        lease = self.response_lease
        if lease is not None:
            try:
                await lease.release()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        return first_error


class _ManagedStreamingResponse(StreamingResponse):
    """Release proxy resources even if ASGI response startup fails."""

    def __init__(
        self,
        *args: Any,
        resources: _ProxyResponseResources,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._resources = resources

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._resources.aclose()


class RuntimeController(Protocol):
    """Lifecycle boundary used by the ASGI app and isolated tests."""

    @property
    def state(self) -> HermesRuntimeState:
        """Return state only after successful startup."""

    def revalidate(self) -> HermesRuntimeState:
        """Return state only while the exact launched child remains valid."""

    async def attest(self) -> HermesRuntimeState:
        """Revalidate the child and its live model-visible MCP inventory."""

    async def acquire_response_lease(
        self,
    ) -> HermesRuntimeResponseLease:
        """Hold one attested child generation through an entire response."""

    async def start(self) -> None:
        """Start and verify the dedicated child."""

    def begin_closing(self) -> None:
        """Reject new response leases before asynchronous shutdown."""

    async def aclose(self) -> None:
        """Stop the dedicated child."""


class _RuntimeShutdownCoordinator:
    """Start controller shutdown once and let lifespan await the same task."""

    def __init__(self, controller: RuntimeController) -> None:
        self._controller = controller
        self._close_task: asyncio.Task[None] | None = None

    def request_close(self) -> asyncio.Task[None]:
        begin_closing = getattr(self._controller, "begin_closing", None)
        if callable(begin_closing):
            begin_closing()
        task = self._close_task
        if task is None:
            task = asyncio.create_task(
                self._controller.aclose(),
                name="healthmes-hermes-uvicorn-controller-close",
            )
            self._close_task = task
        return task

    async def aclose(self) -> None:
        await asyncio.shield(self.request_close())

    @property
    def cleanup_succeeded(self) -> bool:
        """Return true only after the shared controller close completed."""

        task = self._close_task
        return (
            task is not None
            and task.done()
            and not task.cancelled()
            and task.exception() is None
        )


class _HermesRuntimeUvicornServer(uvicorn.Server):
    """Close lease admission as soon as Uvicorn receives an exit signal."""

    def __init__(
        self,
        config: uvicorn.Config,
        *,
        shutdown_coordinator: _RuntimeShutdownCoordinator,
        shutdown_budget_publication: (
            _RuntimeShutdownBudgetPublication | None
        ) = None,
    ) -> None:
        super().__init__(config)
        self._shutdown_coordinator = shutdown_coordinator
        self._shutdown_budget_publication = (
            shutdown_budget_publication
        )

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self._shutdown_coordinator.request_close()
        super().handle_exit(sig, frame)

    async def startup(
        self,
        sockets: list[socket.socket] | None = None,
    ) -> None:
        publication = self._shutdown_budget_publication
        if publication is not None:
            # Publish the supervisor identity before the ASGI lifespan can
            # launch Hermes in its separate process group. Native stop can
            # therefore use either this record or a proven-empty launcher
            # group; there is no unrecorded Hermes child in between.
            publication.publish()
        try:
            await super().startup(sockets=sockets)
        except BaseException:
            self.should_exit = True
            try:
                await self._shutdown_coordinator.aclose()
            finally:
                if (
                    publication is not None
                    and self._shutdown_coordinator.cleanup_succeeded
                ):
                    publication.remove_if_owned()
            raise
        if self.started:
            return
        try:
            await self._shutdown_coordinator.aclose()
        finally:
            if (
                publication is not None
                and self._shutdown_coordinator.cleanup_succeeded
            ):
                publication.remove_if_owned()

    async def shutdown(
        self,
        sockets: list[socket.socket] | None = None,
    ) -> None:
        try:
            await super().shutdown(sockets=sockets)
        finally:
            publication = self._shutdown_budget_publication
            if (
                publication is not None
                and self._shutdown_coordinator.cleanup_succeeded
            ):
                publication.remove_if_owned()


class HermesRuntimeProcess:
    """Launch the exact manifest-bound Hermes child in a scrubbed environment."""

    def __init__(
        self,
        config: HermesRuntimeSupervisorConfig,
        *,
        environ: Mapping[str, str] | None = None,
        mcp_inventory_probe: McpInventoryProbe | None = None,
        boot_identity: HermesRuntimeBootIdentity | None = None,
        process_group_probe: ProcessGroupProbe | None = None,
    ) -> None:
        self._config = config
        self._environ = dict(os.environ if environ is None else environ)
        self._mcp_inventory_probe = (
            mcp_inventory_probe or _probe_live_mcp_schema_digests
        )
        self._boot_identity = (
            _SUPERVISOR_BOOT_IDENTITY
            if boot_identity is None
            else boot_identity
        )
        self._process_group_probe = (
            _probe_process_group_members
            if process_group_probe is None
            else process_group_probe
        )
        self._state: HermesRuntimeState | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._child_pgid: int | None = None
        self._known_child_group_members: frozenset[_ProcessGroupMember] = (
            frozenset()
        )
        self._launch_argv: tuple[str, ...] | None = None
        self._child_generation = 0
        self._healthy = False
        self._lifecycle_state: _LifecycleState = "new"
        self._lifecycle_lock = asyncio.Lock()
        self._child_lock = asyncio.Lock()
        self._child_condition = asyncio.Condition(self._child_lock)
        self._active_response_leases: dict[int, int] = {}
        self._waiting_child_writers = 0
        self._child_writer_active = False
        self._close_lock = asyncio.Lock()
        self._monitor_task: asyncio.Task[None] | None = None
        self._response_admission_closed = False

    @property
    def state(self) -> HermesRuntimeState:
        process = self._process
        if process is None or process.returncode is not None:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_not_running"
            )
        state = self._state
        if state is None:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_not_ready"
            )
        if not getattr(self, "_healthy", True):
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_unhealthy"
            )
        return state

    def revalidate(self) -> HermesRuntimeState:
        state = self.state
        return self._validate_bound_state(expected=state)

    def _validate_bound_state(
        self,
        *,
        expected: HermesRuntimeState | None = None,
    ) -> HermesRuntimeState:
        process = self._process
        if process is None or process.returncode is not None:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_not_running"
            )
        state = self._state
        if state is None:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_not_ready"
            )
        launch_argv = self._launch_argv
        if launch_argv is None:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_launch_identity_missing"
            )
        manifest, key, api_key = validate_supervised_runtime(
            manifest_path=self._config.manifest_path,
            attestation_key_path=self._config.attestation_key_path,
            hermes_home=self._config.hermes_home,
            vendor_root=self._config.vendor_root,
            environment=self._environ,
            expected_launch_argv=launch_argv,
            expected_boot_identity=self._boot_identity,
        )
        if (
            (expected is not None and state != expected)
            or manifest != state.manifest
            or key != state.attestation_key
            or not hmac.compare_digest(api_key, state.api_key)
        ):
            raise HermesRuntimeIdentityError(
                "hermes_runtime_identity_changed"
            )
        return state

    async def _attest_pinned_generation(
        self,
        *,
        expected: HermesRuntimeState,
        generation: int,
    ) -> HermesRuntimeState:
        state = self._validate_bound_state(expected=expected)
        if generation != self._child_generation:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_generation_changed"
            )
        inventory = await self._probe_runtime_inventory()
        verified = self._validate_bound_state(expected=state)
        if (
            generation != self._child_generation
            or verified != state
            or inventory != state.mcp_inventory
        ):
            raise HermesRuntimeIdentityError(
                "hermes_runtime_mcp_inventory_changed"
            )
        return state

    async def attest(self) -> HermesRuntimeState:
        lease = await self.acquire_response_lease()
        try:
            return lease.state
        finally:
            await lease.release()

    async def acquire_response_lease(
        self,
    ) -> HermesRuntimeResponseLease:
        """Attest and pin the current child until the caller releases it."""

        async with self._child_condition:
            while True:
                if (
                    self._response_admission_closed
                    or self._lifecycle_state != "running"
                ):
                    raise HermesRuntimeIdentityError(
                        "hermes_runtime_response_admission_closed"
                    )
                if self._response_lease_available():
                    break
                await self._child_condition.wait()
            state = self.state
            generation = self._child_generation
            self._active_response_leases[generation] = (
                self._active_response_leases.get(generation, 0) + 1
            )
        lease = HermesRuntimeResponseLease(
            state=state,
            generation=generation,
            _release_callback=lambda: self._release_response_lease(
                generation
            ),
        )
        try:
            verified = await self._attest_pinned_generation(
                expected=state,
                generation=generation,
            )
            process = self._process
            if process is None or process.returncode is not None:
                raise HermesRuntimeIdentityError(
                    "hermes_runtime_child_not_running"
                )
            if verified != state:
                raise HermesRuntimeIdentityError(
                    "hermes_runtime_identity_changed"
                )
            return lease
        except BaseException:
            await lease.release()
            raise

    def begin_closing(self) -> None:
        """Synchronously reject new leases before async shutdown begins."""

        self._response_admission_closed = True

    def _response_lease_available(self) -> bool:
        return (
            not self._child_writer_active
            and self._waiting_child_writers == 0
            and sum(self._active_response_leases.values())
            < self._config.max_concurrent_responses
        )

    async def _release_response_lease(self, generation: int) -> None:
        async with self._child_condition:
            count = self._active_response_leases.get(generation, 0)
            if count < 1:
                raise RuntimeError(
                    "Hermes response lease accounting underflow"
                )
            if count == 1:
                del self._active_response_leases[generation]
            else:
                self._active_response_leases[generation] = count - 1
            self._child_condition.notify_all()

    @asynccontextmanager
    async def _exclusive_child_generation(self) -> AsyncIterator[None]:
        """Block new leases, drain current readers, then mutate the child."""

        acquired = False
        async with self._child_condition:
            self._waiting_child_writers += 1
            self._child_condition.notify_all()
            try:
                await self._child_condition.wait_for(
                    lambda: (
                        not self._child_writer_active
                        and not self._active_response_leases
                    )
                )
                self._child_writer_active = True
                acquired = True
            finally:
                self._waiting_child_writers -= 1
                if not acquired:
                    self._child_condition.notify_all()
        try:
            yield
        finally:
            async with self._child_condition:
                self._child_writer_active = False
                self._child_condition.notify_all()

    async def start(self) -> None:
        """Strictly launch and verify one child for direct callers."""

        async with self._lifecycle_lock:
            if self._lifecycle_state not in {"new", "running"}:
                return
            self._lifecycle_state = "running"
        async with self._exclusive_child_generation():
            if self._lifecycle_state != "running":
                return
            if self._child_is_available():
                return
            await self._stop_child()
            if self._lifecycle_state != "running":
                return
            await self._launch_child()

    async def start_observable(self) -> None:
        """Start recovery in the background so the parent can serve health."""

        async with self._lifecycle_lock:
            if self._lifecycle_state not in {"new", "running"}:
                return
            monitor = self._monitor_task
            if monitor is not None and not monitor.done():
                return
            self._lifecycle_state = "running"
            self._monitor_task = asyncio.create_task(
                self._monitor_runtime(),
                name="healthmes-hermes-runtime-watchdog",
            )

    def _child_is_available(self) -> bool:
        process = self._process
        return (
            process is not None
            and process.returncode is None
            and self._state is not None
            and self._healthy
        )

    async def _launch_child(self) -> None:
        config = self._config
        self._healthy = False
        manifest, key, api_key = seal_supervised_runtime(
            manifest_path=config.manifest_path,
            attestation_key_path=config.attestation_key_path,
            hermes_home=config.hermes_home,
            vendor_root=config.vendor_root,
            environment=self._environ,
            expected_boot_identity=self._boot_identity,
        )
        if (config.vendor_root / ".env").exists():
            raise HermesRuntimeIdentityError(
                "hermes_runtime_vendor_env_rejected"
            )
        child_env = build_child_environment(
            self._environ,
            manifest=manifest,
        )
        launch_manifest, launch_key, launch_api_key = (
            validate_supervised_runtime(
                manifest_path=config.manifest_path,
                attestation_key_path=config.attestation_key_path,
                hermes_home=config.hermes_home,
                vendor_root=config.vendor_root,
                environment=self._environ,
                expected_launch_argv=manifest.launch_argv,
                expected_boot_identity=self._boot_identity,
            )
        )
        if (
            launch_manifest != manifest
            or launch_key != key
            or not hmac.compare_digest(launch_api_key, api_key)
        ):
            raise HermesRuntimeIdentityError(
                "hermes_runtime_identity_changed"
            )
        process = await asyncio.create_subprocess_exec(
            *launch_manifest.launch_argv,
            cwd=str(config.vendor_root),
            env=child_env,
            start_new_session=True,
        )
        self._process = process
        self._child_pgid = process.pid
        self._known_child_group_members = frozenset()
        self._launch_argv = launch_manifest.launch_argv
        try:
            self._refresh_child_group_identity(require_leader=True)
            await self._wait_until_ready(
                manifest=manifest,
                api_key=api_key,
            )
            inventory = await self._probe_runtime_inventory()
            verified_manifest, verified_key, verified_api_key = (
                validate_supervised_runtime(
                    manifest_path=config.manifest_path,
                    attestation_key_path=config.attestation_key_path,
                    hermes_home=config.hermes_home,
                    vendor_root=config.vendor_root,
                    environment=self._environ,
                    expected_launch_argv=manifest.launch_argv,
                    expected_boot_identity=self._boot_identity,
                )
            )
            if (
                verified_manifest != manifest
                or verified_key != key
                or not hmac.compare_digest(
                    verified_api_key,
                    api_key,
                )
            ):
                raise HermesRuntimeIdentityError(
                    "hermes_runtime_identity_changed"
                )
        except BaseException:
            await self._stop_child()
            raise
        self._state = HermesRuntimeState(
            manifest=manifest,
            attestation_key=key,
            api_key=api_key,
            mcp_inventory=inventory,
        )
        self._child_generation += 1
        self._healthy = True

    async def _monitor_runtime(self) -> None:
        restart_delay = 0.0
        unhealthy_count = 0
        while self._lifecycle_state == "running":
            process = self._process
            if (
                process is None
                or process.returncode is not None
                or self._state is None
            ):
                if process is not None or self._state is not None:
                    restart_delay = _next_restart_backoff(
                        restart_delay,
                        initial=(
                            self._config.restart_backoff_initial_seconds
                        ),
                        maximum=self._config.restart_backoff_max_seconds,
                    )
                self._healthy = False
                try:
                    async with self._exclusive_child_generation():
                        if self._lifecycle_state != "running":
                            return
                        await self._stop_child()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    restart_delay = _next_restart_backoff(
                        restart_delay,
                        initial=(
                            self._config.restart_backoff_initial_seconds
                        ),
                        maximum=self._config.restart_backoff_max_seconds,
                    )
                    await asyncio.sleep(restart_delay)
                    continue
                if restart_delay > 0:
                    await asyncio.sleep(restart_delay)
                if self._lifecycle_state != "running":
                    return
                try:
                    async with self._exclusive_child_generation():
                        if self._lifecycle_state != "running":
                            return
                        if not self._child_is_available():
                            await self._launch_child()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    restart_delay = _next_restart_backoff(
                        restart_delay,
                        initial=(
                            self._config.restart_backoff_initial_seconds
                        ),
                        maximum=self._config.restart_backoff_max_seconds,
                    )
                    continue
                unhealthy_count = 0
                continue

            await asyncio.sleep(
                self._config.health_check_interval_seconds
            )
            if self._lifecycle_state != "running":
                return
            if (
                self._process is not process
                or process.returncode is not None
            ):
                self._healthy = False
                continue

            identity_invalid = False
            try:
                healthy = await self._probe_child_health()
            except asyncio.CancelledError:
                raise
            except HermesRuntimeIdentityError:
                healthy = False
                identity_invalid = True
            except Exception:
                healthy = False

            if healthy:
                self._healthy = True
                unhealthy_count = 0
                restart_delay = 0
                continue

            self._healthy = False
            unhealthy_count += 1
            if (
                not identity_invalid
                and unhealthy_count < self._config.unhealthy_threshold
            ):
                continue

            try:
                async with self._exclusive_child_generation():
                    if self._process is process:
                        await self._stop_child()
            except asyncio.CancelledError:
                raise
            except Exception:
                restart_delay = _next_restart_backoff(
                    restart_delay,
                    initial=self._config.restart_backoff_initial_seconds,
                    maximum=self._config.restart_backoff_max_seconds,
                )
                await asyncio.sleep(restart_delay)
                continue
            unhealthy_count = 0
            restart_delay = _next_restart_backoff(
                restart_delay,
                initial=self._config.restart_backoff_initial_seconds,
                maximum=self._config.restart_backoff_max_seconds,
            )

    async def _probe_child_health(self) -> bool:
        state = self._validate_bound_state()
        headers = {"Authorization": f"Bearer {state.api_key}"}
        try:
            async with httpx.AsyncClient(
                base_url=state.manifest.internal_origin,
                headers=headers,
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    "/v1/models",
                    timeout=self._config.health_check_timeout_seconds,
                )
        except httpx.HTTPError:
            return False
        if response.status_code != 200:
            return False
        if self._validate_bound_state(expected=state) != state:
            return False
        self._refresh_child_group_identity(require_leader=True)
        return True

    async def _wait_until_ready(
        self,
        *,
        manifest: HermesDecisionRuntimeManifest,
        api_key: str,
    ) -> None:
        deadline = (
            asyncio.get_running_loop().time()
            + self._config.startup_timeout_seconds
        )
        headers = {"Authorization": f"Bearer {api_key}"}
        async with httpx.AsyncClient(
            base_url=manifest.internal_origin,
            headers=headers,
            follow_redirects=False,
        ) as client:
            while True:
                process = self._process
                if process is None:
                    raise RuntimeError("Hermes child disappeared")
                if process.returncode is not None:
                    raise RuntimeError(
                        f"Hermes child exited with {process.returncode}"
                    )
                try:
                    response = await client.get("/v1/models", timeout=1)
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("Hermes child startup timed out")
                await asyncio.sleep(min(0.1, remaining))

    async def _probe_runtime_inventory(
        self,
    ) -> HermesMcpToolInventory:
        connection = load_runtime_mcp_connection(
            self._config.hermes_home
        )
        try:
            async with asyncio.timeout(
                self._config.mcp_probe_timeout_seconds
            ):
                schema_digests = await self._mcp_inventory_probe(
                    connection,
                    self._config.mcp_probe_timeout_seconds,
                )
            return validate_model_visible_mcp_inventory(schema_digests)
        except asyncio.CancelledError:
            raise
        except HermesMcpInventoryError as exc:
            raise HermesRuntimeIdentityError(str(exc)) from exc
        except HermesRuntimeIdentityError:
            raise
        except TimeoutError as exc:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_mcp_inventory_timeout"
            ) from exc
        except Exception as exc:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_mcp_inventory_unavailable"
            ) from exc

    def _refresh_child_group_identity(
        self,
        *,
        require_leader: bool,
        timeout_seconds: float = _PROCESS_GROUP_PROBE_TIMEOUT_SECONDS,
    ) -> frozenset[_ProcessGroupMember]:
        pgid = self._child_pgid
        if pgid is None:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_group_identity_missing"
            )
        current = self._process_group_probe(pgid, timeout_seconds)
        if not current:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_group_not_running"
            )
        known = self._known_child_group_members
        if known and current.isdisjoint(known):
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_group_identity_changed"
            )
        if require_leader and not any(
            member.pid == pgid for member in current
        ):
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_group_leader_changed"
            )
        self._known_child_group_members = known | current
        return current

    async def _establish_initial_child_group_ownership(
        self,
        process: asyncio.subprocess.Process,
        *,
        deadline: float,
    ) -> frozenset[_ProcessGroupMember]:
        """Bind the first group snapshot even when the leader exits first."""

        pgid = self._child_pgid
        if pgid is None:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_group_identity_missing"
            )
        loop = asyncio.get_running_loop()
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise RuntimeError("hermes_runtime_child_term_timeout")
        before_reap = self._process_group_probe(
            pgid,
            min(_PROCESS_GROUP_PROBE_TIMEOUT_SECONDS, remaining),
        )
        leader_in_before = any(
            member.pid == process.pid for member in before_reap
        )
        if leader_in_before:
            if process.returncode is not None:
                raise HermesRuntimeIdentityError(
                    "hermes_runtime_child_group_leader_changed"
                )
            self._known_child_group_members = before_reap
            return before_reap

        if process.returncode is None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RuntimeError(
                    "hermes_runtime_child_term_timeout"
                )
            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=remaining,
                )
            except TimeoutError as exc:
                raise HermesRuntimeIdentityError(
                    "hermes_runtime_child_group_leader_state_unknown"
                ) from exc
            if process.returncode is None:
                raise HermesRuntimeIdentityError(
                    "hermes_runtime_child_group_leader_state_unknown"
                )

        remaining = deadline - loop.time()
        if remaining <= 0:
            raise RuntimeError("hermes_runtime_child_term_timeout")
        after_reap = self._process_group_probe(
            pgid,
            min(_PROCESS_GROUP_PROBE_TIMEOUT_SECONDS, remaining),
        )
        if any(member.pid == process.pid for member in after_reap):
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_group_leader_changed"
            )
        if (
            before_reap
            and after_reap
            and before_reap.isdisjoint(after_reap)
        ):
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_group_identity_changed"
            )
        self._known_child_group_members = before_reap | after_reap
        return after_reap

    def _owned_child_group_members(
        self,
        *,
        timeout_seconds: float,
    ) -> frozenset[_ProcessGroupMember]:
        pgid = self._child_pgid
        if pgid is None:
            return frozenset()
        current = self._process_group_probe(pgid, timeout_seconds)
        if not current:
            return frozenset()
        known = self._known_child_group_members
        if not known:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_group_identity_missing"
            )
        if current.isdisjoint(known):
            # Never report a reused numeric PGID as a successfully drained
            # child, and never signal the unrelated replacement group.
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_group_identity_changed"
            )
        self._known_child_group_members = known | current
        return current

    def _signal_owned_child_group_members(
        self,
        sent: signal.Signals,
        *,
        deadline: float,
    ) -> bool:
        if self._child_pgid is None:
            return False
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        current = self._owned_child_group_members(
            timeout_seconds=min(
                _PROCESS_GROUP_PROBE_TIMEOUT_SECONDS,
                remaining,
            )
        )
        if not current:
            return False
        signaled = False
        for member in sorted(current, key=lambda item: item.pid):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            revalidated = self._owned_child_group_members(
                timeout_seconds=min(
                    _PROCESS_GROUP_PROBE_TIMEOUT_SECONDS,
                    remaining,
                )
            )
            if member not in revalidated:
                continue
            try:
                # Never signal the numeric PGID directly. A dead leader can
                # free that number while verified descendants still need
                # cleanup, so each exact PID/start-token member is signaled.
                member_signaled = _signal_process_group_member(
                    member,
                    sent,
                )
            except ProcessLookupError:
                continue
            except PermissionError as exc:
                raise RuntimeError(
                    "hermes_runtime_child_group_signal_denied"
                ) from exc
            signaled = member_signaled or signaled
        return signaled

    async def _wait_for_child_group_exit(self, *, deadline: float) -> bool:
        loop = asyncio.get_running_loop()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            if not self._owned_child_group_members(
                timeout_seconds=min(
                    _PROCESS_GROUP_PROBE_TIMEOUT_SECONDS,
                    remaining,
                )
            ):
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(
                min(_PROCESS_GROUP_POLL_INTERVAL_SECONDS, remaining)
            )

    async def _wait_for_child_reap(
        self,
        process: asyncio.subprocess.Process,
        *,
        deadline: float,
    ) -> None:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise RuntimeError("hermes_runtime_child_kill_timeout")
        try:
            await asyncio.wait_for(process.wait(), timeout=remaining)
        except TimeoutError as exc:
            raise RuntimeError(
                "hermes_runtime_child_kill_timeout"
            ) from exc

    async def _stop_child(self) -> None:
        process = self._process
        self._state = None
        self._launch_argv = None
        self._healthy = False
        if process is None:
            self._child_pgid = None
            self._known_child_group_members = frozenset()
            return
        if self._child_pgid is None:
            self._child_pgid = process.pid
        loop = asyncio.get_running_loop()
        term_deadline = (
            loop.time() + self._config.child_term_timeout_seconds
        )
        if not self._known_child_group_members:
            current = await self._establish_initial_child_group_ownership(
                process,
                deadline=term_deadline,
            )
            if not current:
                self._child_pgid = None
                self._process = None
                return
        self._signal_owned_child_group_members(
            signal.SIGTERM,
            deadline=term_deadline,
        )
        group_gone = await self._wait_for_child_group_exit(
            deadline=term_deadline
        )
        kill_deadline = (
            loop.time() + self._config.child_kill_timeout_seconds
        )
        if not group_gone:
            self._signal_owned_child_group_members(
                signal.SIGKILL,
                deadline=kill_deadline,
            )
            if not await self._wait_for_child_group_exit(
                deadline=kill_deadline
            ):
                raise RuntimeError(
                    "hermes_runtime_child_kill_timeout"
                )
        await self._wait_for_child_reap(
            process,
            deadline=kill_deadline,
        )
        if self._process is process:
            self._process = None
        self._child_pgid = None
        self._known_child_group_members = frozenset()

    async def aclose(self) -> None:
        self.begin_closing()
        async with self._close_lock:
            async with self._lifecycle_lock:
                if self._lifecycle_state == "closed":
                    return
                async with self._child_condition:
                    self._lifecycle_state = "closing"
                    monitor = self._monitor_task
                    self._monitor_task = None
                    self._child_condition.notify_all()
            close_owner = asyncio.current_task()
            cleanup_task = asyncio.create_task(
                self._finish_close(monitor, close_owner),
                name="healthmes-hermes-runtime-close",
            )
            caller_cancelled, monitor_error = await _await_teardown_task(
                cleanup_task
            )

            if monitor_error is not None:
                raise monitor_error
            if caller_cancelled:
                raise asyncio.CancelledError

    async def _finish_close(
        self,
        monitor: asyncio.Task[None] | None,
        close_owner: asyncio.Task[Any] | None,
    ) -> BaseException | None:
        try:
            monitor_error: BaseException | None = None
            if monitor is not None and monitor is not close_owner:
                monitor.cancel()
                try:
                    await monitor
                except asyncio.CancelledError:
                    pass
                except BaseException as exc:
                    monitor_error = exc
            async with self._exclusive_child_generation():
                await self._stop_child()
        except BaseException:
            async with self._lifecycle_lock:
                self._monitor_task = None
                self._lifecycle_state = "close_failed"
            raise
        async with self._lifecycle_lock:
            self._monitor_task = None
            self._lifecycle_state = "closed"
        return monitor_error


async def _await_teardown_task(
    task: asyncio.Task[BaseException | None],
) -> tuple[bool, BaseException | None]:
    """Finish teardown despite caller cancellation and report cancellation."""

    caller_cancelled = False
    while True:
        try:
            return caller_cancelled, await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            caller_cancelled = True
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            if task.done():
                return caller_cancelled, task.result()


def _probe_process_group_members(
    pgid: int,
    timeout_seconds: float,
) -> frozenset[_ProcessGroupMember]:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise RuntimeError(
            "hermes_runtime_child_group_probe_timeout"
        )
    if sys.platform.startswith("linux"):
        if not Path("/proc/self/stat").exists():
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_group_proc_unavailable"
            )
        return _probe_linux_process_group_members(
            pgid,
            timeout_seconds,
        )
    if sys.platform == "darwin":
        return _probe_darwin_process_group_members(
            pgid,
            timeout_seconds,
        )
    raise HermesRuntimeIdentityError(
        "hermes_runtime_child_group_platform_unsupported"
    )


def _probe_linux_process_group_members(
    pgid: int,
    timeout_seconds: float,
) -> frozenset[_ProcessGroupMember]:
    members: set[_ProcessGroupMember] = set()
    deadline = time.monotonic() + timeout_seconds
    try:
        entries = Path("/proc").iterdir()
        for entry in entries:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "hermes_runtime_child_group_probe_timeout"
                )
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise HermesRuntimeIdentityError(
                    "hermes_runtime_child_group_proc_unreadable"
                ) from exc
            closing_parenthesis = stat.rfind(")")
            fields = stat[closing_parenthesis + 1 :].split()
            try:
                pid = int(entry.name)
                process_group = int(fields[2])
                start_ticks = int(fields[19])
            except (IndexError, ValueError) as exc:
                raise HermesRuntimeIdentityError(
                    "hermes_runtime_child_group_proc_invalid"
                ) from exc
            if (
                closing_parenthesis < 1
                or pid < 1
                or process_group < 1
                or start_ticks < 1
            ):
                raise HermesRuntimeIdentityError(
                    "hermes_runtime_child_group_proc_invalid"
                )
            if process_group == pgid:
                members.add(
                    _ProcessGroupMember(
                        pid=pid,
                        start_token=f"linux:{start_ticks}",
                    )
                )
    except OSError as exc:
        raise RuntimeError(
            "hermes_runtime_child_group_probe_failed"
        ) from exc
    return frozenset(members)


def _probe_darwin_process_group_members(
    pgid: int,
    timeout_seconds: float,
) -> frozenset[_ProcessGroupMember]:
    """Enumerate a group with ps, but identify every PID through libproc."""

    environment = {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }
    deadline = time.monotonic() + timeout_seconds
    try:
        result = subprocess.run(
            [_DARWIN_PS_PATH, "-axo", "pid=,pgid="],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "hermes_runtime_child_group_probe_timeout"
        ) from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "hermes_runtime_child_group_probe_failed"
        ) from exc
    if result.stderr.strip():
        raise HermesRuntimeIdentityError(
            "hermes_runtime_child_group_ps_output_invalid"
        )
    if result.stdout and not result.stdout.endswith("\n"):
        raise HermesRuntimeIdentityError(
            "hermes_runtime_child_group_ps_output_invalid"
        )
    members: set[_ProcessGroupMember] = set()
    seen_pids: set[int] = set()
    saw_process_row = False
    for line in result.stdout.splitlines():
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "hermes_runtime_child_group_probe_timeout"
            )
        stripped = line.strip()
        if not stripped:
            continue
        saw_process_row = True
        fields = stripped.split()
        if len(fields) != 2:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_group_ps_output_invalid"
            )
        try:
            pid = int(fields[0])
            process_group = int(fields[1])
        except ValueError as exc:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_group_ps_output_invalid"
            ) from exc
        if pid < 1 or process_group < 1:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_group_ps_output_invalid"
            )
        if pid in seen_pids:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_group_ps_output_duplicate"
            )
        seen_pids.add(pid)
        if process_group != pgid:
            continue
        snapshot = _probe_darwin_process_snapshot(pid)
        if snapshot is None:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_group_ps_output_inconsistent"
            )
        verified_pgid, start_token = snapshot
        if verified_pgid != pgid:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_group_ps_output_inconsistent"
            )
        members.add(
            _ProcessGroupMember(
                pid=pid,
                start_token=start_token,
            )
        )
    if not saw_process_row:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_child_group_ps_output_invalid"
        )
    return frozenset(members)


def _probe_ps_process_group_members(
    pgid: int,
    timeout_seconds: float,
) -> frozenset[_ProcessGroupMember]:
    environment = dict(os.environ)
    environment["LC_ALL"] = "C"
    deadline = time.monotonic() + timeout_seconds
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,pgid=,lstart="],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "hermes_runtime_child_group_probe_timeout"
        ) from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "hermes_runtime_child_group_probe_failed"
        ) from exc
    members: set[_ProcessGroupMember] = set()
    for line in result.stdout.splitlines():
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "hermes_runtime_child_group_probe_timeout"
            )
        fields = line.strip().split(None, 2)
        if len(fields) != 3:
            continue
        try:
            pid = int(fields[0])
            process_group = int(fields[1])
        except ValueError:
            continue
        if process_group == pgid:
            members.add(
                _ProcessGroupMember(
                    pid=pid,
                    start_token=f"ps:{fields[2]}",
                )
            )
    return frozenset(members)


def _signal_process_group_member(
    member: _ProcessGroupMember,
    sent: signal.Signals,
) -> bool:
    if member.start_token.startswith("linux:"):
        return _signal_linux_process_group_member(member, sent)
    if member.start_token.startswith("darwin:"):
        return _signal_darwin_process_group_member(member, sent)
    raise HermesRuntimeIdentityError(
        "hermes_runtime_child_group_identity_unsupported"
    )


def _signal_linux_process_group_member(
    member: _ProcessGroupMember,
    sent: signal.Signals,
) -> bool:
    """Atomically signal the verified Linux process through a pidfd."""

    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if not callable(pidfd_open) or not callable(pidfd_send_signal):
        raise HermesRuntimeIdentityError(
            "hermes_runtime_child_pidfd_unavailable"
        )
    try:
        descriptor = pidfd_open(member.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        raise RuntimeError(
            "hermes_runtime_child_group_signal_denied"
        ) from exc
    except OSError as exc:
        if exc.errno in {
            errno.ENOSYS,
            errno.ENOTSUP,
            errno.EINVAL,
        }:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_pidfd_unavailable"
            ) from exc
        raise RuntimeError(
            "hermes_runtime_child_pidfd_open_failed"
        ) from exc
    try:
        current = _probe_process_start_token(
            member.pid,
            expected_style="linux",
        )
        if current is None:
            try:
                pidfd_send_signal(descriptor, 0, None, 0)
            except ProcessLookupError:
                return False
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_group_identity_unavailable"
            )
        if not hmac.compare_digest(current, member.start_token):
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_group_identity_changed"
            )
        try:
            pidfd_send_signal(descriptor, sent, None, 0)
        except ProcessLookupError:
            return False
        except PermissionError as exc:
            raise RuntimeError(
                "hermes_runtime_child_group_signal_denied"
            ) from exc
        except OSError as exc:
            raise RuntimeError(
                "hermes_runtime_child_group_signal_failed"
            ) from exc
        return True
    finally:
        os.close(descriptor)


def _signal_darwin_process_group_member(
    member: _ProcessGroupMember,
    sent: signal.Signals,
) -> bool:
    """Fail closed unless libproc proves the current numeric PID identity."""

    current = _probe_process_start_token(
        member.pid,
        expected_style="darwin",
    )
    if current is None:
        return False
    if not hmac.compare_digest(current, member.start_token):
        raise HermesRuntimeIdentityError(
            "hermes_runtime_child_group_identity_changed"
        )
    # macOS exposes no pidfd-equivalent signal handle. libproc gives the
    # highest-resolution public start identity, but the kernel still accepts
    # only a numeric PID here. The final check above minimizes that unavoidable
    # interval; an unprovable identity is never signaled.
    os.kill(member.pid, sent)
    return True


def _run_runtime_process_action(
    *,
    action: Literal["probe", "signal", "wait"],
    pid: int,
    start_token: str,
    timeout_seconds: float | None = None,
) -> int:
    """Probe or TERM one exact supervisor identity for the native launcher."""

    try:
        identity = HermesRuntimeProcessIdentity(
            pid=pid,
            start_token=start_token,
        )
        if action == "wait":
            if (
                timeout_seconds is None
                or not math.isfinite(timeout_seconds)
                or timeout_seconds <= 0
                or timeout_seconds
                > _MAX_RUNTIME_DRAIN_TIMEOUT_SECONDS
                + 2
            ):
                raise ValueError(
                    "runtime supervisor wait timeout is invalid"
                )
            deadline = time.monotonic() + timeout_seconds
            while True:
                state = _runtime_process_identity_state(identity)
                if state == "gone":
                    return 0
                if state == "changed":
                    print(
                        "hermes_runtime_supervisor_identity_changed",
                        file=sys.stderr,
                    )
                    return 4
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    print(
                        "hermes_runtime_supervisor_wait_timeout",
                        file=sys.stderr,
                    )
                    return 6
                time.sleep(min(0.1, remaining))
        state = _runtime_process_identity_state(identity)
        if state == "gone":
            return 3
        if state == "changed":
            print(
                "hermes_runtime_supervisor_identity_changed",
                file=sys.stderr,
            )
            return 4
        if action == "probe":
            return 0
        signaled = _signal_process_group_member(
            _ProcessGroupMember(
                pid=identity.pid,
                start_token=identity.start_token,
            ),
            signal.SIGTERM,
        )
        return 0 if signaled else 3
    except (HermesRuntimeIdentityError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 5


def _run_runtime_process_group_probe(
    *,
    pgid: int,
    timeout_seconds: float | None,
) -> int:
    """Prove that a launcher's process group has no surviving members."""

    try:
        if pgid <= 1:
            raise ValueError("runtime launcher process group is invalid")
        if (
            timeout_seconds is None
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > _PROCESS_GROUP_PROBE_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "runtime launcher process-group probe timeout is invalid"
            )
        members = _probe_process_group_members(pgid, timeout_seconds)
    except (HermesRuntimeIdentityError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 5
    if members:
        print(
            "hermes_runtime_launcher_group_not_empty",
            file=sys.stderr,
        )
        return 6
    return 0


def _next_restart_backoff(
    current: float,
    *,
    initial: float,
    maximum: float,
) -> float:
    """Return a positive exponential delay capped at ``maximum``."""

    if current <= 0:
        return min(initial, maximum)
    return min(maximum, max(initial, current * 2))


async def _probe_live_mcp_schema_digests(
    connection: HermesRuntimeMcpConnection,
    timeout_seconds: float,
) -> Mapping[str, str]:
    """Reach the configured MCP server and return the filtered live schemas."""

    transport = StreamableHttpTransport(
        connection.url,
        headers=connection.header_mapping(),
    )
    async with Client(
        transport,
        timeout=timeout_seconds,
        init_timeout=timeout_seconds,
    ) as client:
        tools = await client.list_tools(max_pages=8)
    return schema_digests_from_mcp_tools(
        tools,
        included_names=HERMES_DECISION_MCP_TOOL_NAMES,
    )


def build_child_environment(
    source: Mapping[str, str],
    *,
    manifest: HermesDecisionRuntimeManifest,
) -> dict[str, str]:
    """Build only the manifest-bound runtime and provider environment."""

    home = Path(manifest.hermes_home)
    managed_dir = home / ".managed-scope-disabled"
    if managed_dir.exists():
        raise HermesRuntimeIdentityError(
            "hermes_runtime_managed_scope_present"
        )
    selected = {
        item.name: item.value for item in manifest.required_environment
    }
    expected_provider = {
        item.name: item.sha256 for item in manifest.provider_environment
    }
    actual_provider = {
        name: value
        for name, value in source.items()
        if name in HERMES_RUNTIME_PROVIDER_ENV_NAMES and value
    }
    if set(actual_provider) != set(expected_provider):
        raise HermesRuntimeIdentityError(
            "hermes_runtime_provider_environment_mismatch"
        )
    for name, value in actual_provider.items():
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(digest, expected_provider[name]):
            raise HermesRuntimeIdentityError(
                "hermes_runtime_provider_environment_mismatch"
            )
        selected[name] = value
    return selected


def create_supervisor_app(
    controller: RuntimeController,
    *,
    proxy_transport: httpx.AsyncBaseTransport | None = None,
    response_timeout_seconds: float = _MAX_DECISION_TIMEOUT_SECONDS,
    shutdown_coordinator: _RuntimeShutdownCoordinator | None = None,
) -> FastAPI:
    """Expose only attestation and the Responses/session endpoints."""

    if (
        not math.isfinite(response_timeout_seconds)
        or response_timeout_seconds <= 0
        or response_timeout_seconds > _MAX_DECISION_TIMEOUT_SECONDS
    ):
        raise ValueError(
            "response timeout must be finite and within (0, 300]"
        )
    coordinator = shutdown_coordinator or _RuntimeShutdownCoordinator(
        controller
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        observable_start = getattr(
            controller,
            "start_observable",
            None,
        )
        if callable(observable_start):
            await observable_start()
        else:
            await controller.start()
        try:
            yield
        finally:
            await coordinator.aclose()

    app = FastAPI(
        title="HealthMes Hermes Decision Runtime",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get(HERMES_RUNTIME_HEALTH_PATH)
    async def runtime_health() -> dict[str, str]:
        try:
            state = controller.revalidate()
        except HermesRuntimeIdentityError as exc:
            raise HTTPException(
                status_code=503,
                detail="runtime identity unavailable",
            ) from exc
        return {
            "status": "ready",
            "runtime_id": state.manifest.runtime_id,
        }

    @app.post(HERMES_RUNTIME_ATTESTATION_PATH)
    async def runtime_attestation(request: Request) -> JSONResponse:
        try:
            state = controller.state
        except HermesRuntimeIdentityError as exc:
            raise HTTPException(
                status_code=503,
                detail="runtime attestation unavailable",
            ) from exc
        _require_api_key(request, state.api_key)
        payload = await _bounded_json(request)
        nonce = payload.get("nonce")
        if not isinstance(nonce, str):
            raise HTTPException(status_code=400, detail="invalid nonce")
        try:
            state = await controller.attest()
            proof = sign_runtime_attestation(
                manifest=state.manifest,
                key=state.attestation_key,
                nonce=nonce,
                mcp_inventory=state.mcp_inventory,
            )
        except HermesRuntimeIdentityError as exc:
            raise HTTPException(
                status_code=503,
                detail="runtime attestation unavailable",
            ) from exc
        return JSONResponse(
            proof.model_dump(mode="json", by_alias=True),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/v1/models")
    async def proxy_models(request: Request) -> StreamingResponse:
        return await _proxy(request, "/v1/models")

    @app.get("/v1/toolsets")
    async def proxy_toolsets(request: Request) -> StreamingResponse:
        return await _proxy(request, "/v1/toolsets")

    @app.post("/v1/responses")
    async def proxy_responses(request: Request) -> StreamingResponse:
        try:
            state = controller.revalidate()
        except HermesRuntimeIdentityError as exc:
            raise HTTPException(
                status_code=503,
                detail="runtime identity unavailable",
            ) from exc
        _require_api_key(request, state.api_key)
        payload = await _bounded_json(request)
        if payload.get("stream") is not True or payload.get("store") is not False:
            raise HTTPException(
                status_code=400,
                detail="stream=true and store=false are required",
            )
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        deadline = (
            asyncio.get_running_loop().time()
            + response_timeout_seconds
        )
        lease: HermesRuntimeResponseLease | None = None
        try:
            async with asyncio.timeout_at(deadline):
                lease = await controller.acquire_response_lease()
        except TimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail="runtime response deadline exceeded",
            ) from exc
        except HermesRuntimeIdentityError as exc:
            raise HTTPException(
                status_code=503,
                detail="runtime identity unavailable",
            ) from exc
        try:
            _require_api_key(request, lease.state.api_key)
            return await _proxy(
                request,
                "/v1/responses",
                body=encoded,
                accept="text/event-stream",
                state=lease.state,
                response_lease=lease,
                deadline=deadline,
            )
        except BaseException:
            await lease.release()
            raise

    @app.get("/api/sessions")
    async def proxy_sessions(request: Request) -> StreamingResponse:
        return await _proxy(request, "/api/sessions")

    @app.delete("/api/sessions/{session_id}")
    async def proxy_delete_session(
        session_id: str,
        request: Request,
    ) -> StreamingResponse:
        if not session_id or any(char in session_id for char in "\r\n\x00/"):
            raise HTTPException(status_code=400, detail="invalid session id")
        return await _proxy(
            request,
            f"/api/sessions/{quote(session_id, safe='')}",
        )

    async def _proxy(
        request: Request,
        path: str,
        *,
        body: bytes | None = None,
        accept: str = "application/json",
        state: HermesRuntimeState | None = None,
        response_lease: HermesRuntimeResponseLease | None = None,
        deadline: float | None = None,
    ) -> StreamingResponse:
        if state is None:
            try:
                state = controller.revalidate()
            except HermesRuntimeIdentityError as exc:
                raise HTTPException(
                    status_code=503,
                    detail="runtime identity unavailable",
                ) from exc
        _require_api_key(request, state.api_key)
        if body is None and request.method in {"POST", "PUT", "PATCH"}:
            body = await _bounded_body(request)
        query = request.url.query
        target = f"{path}?{query}" if query else path
        headers = {
            "Accept": accept,
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {state.api_key}",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        resources: _ProxyResponseResources | None = None
        try:
            client = httpx.AsyncClient(
                base_url=state.manifest.internal_origin,
                follow_redirects=False,
                transport=proxy_transport,
                timeout=_proxy_timeout(streaming=response_lease is not None),
            )
            resources = _ProxyResponseResources(
                client=client,
                response_lease=response_lease,
            )
            upstream_request = client.build_request(
                request.method,
                target,
                content=body,
                headers=headers,
            )
            if deadline is None:
                upstream = await client.send(
                    upstream_request,
                    stream=True,
                )
            else:
                async with asyncio.timeout_at(deadline):
                    upstream = await client.send(
                        upstream_request,
                        stream=True,
                    )
            resources.upstream = upstream
            response_headers = {
                key: value
                for key, value in upstream.headers.items()
                if key.lower() in _FORWARDED_RESPONSE_HEADERS
            }
            response_headers["Cache-Control"] = "no-store"
            return _ManagedStreamingResponse(
                _stream_upstream_response(
                    request=request,
                    resources=resources,
                    deadline=deadline,
                ),
                status_code=upstream.status_code,
                headers=response_headers,
                resources=resources,
            )
        except TimeoutError as exc:
            if resources is not None:
                await resources.aclose()
            elif response_lease is not None:
                await response_lease.release()
            raise HTTPException(
                status_code=504,
                detail="runtime response deadline exceeded",
            ) from exc
        except httpx.HTTPError as exc:
            if resources is not None:
                await resources.aclose()
            elif response_lease is not None:
                await response_lease.release()
            raise HTTPException(
                status_code=503,
                detail="runtime upstream unavailable",
            ) from exc
        except BaseException:
            if resources is not None:
                await resources.aclose()
            elif response_lease is not None:
                await response_lease.release()
            raise

    return app


async def _stream_upstream_response(
    *,
    request: Request,
    resources: _ProxyResponseResources,
    deadline: float | None = None,
) -> AsyncIterator[bytes]:
    """Close Hermes immediately when the HealthMes caller goes away."""

    upstream = resources.upstream
    if upstream is None:
        raise RuntimeError("Hermes upstream response is unavailable")
    try:
        if deadline is None:
            async for chunk in _connected_upstream_chunks(
                request=request,
                upstream=upstream,
            ):
                yield chunk
        else:
            async with asyncio.timeout_at(deadline):
                async for chunk in _connected_upstream_chunks(
                    request=request,
                    upstream=upstream,
                ):
                    yield chunk
    finally:
        # Hermes maps this upstream disconnect to agent.interrupt().
        await resources.aclose()


async def _connected_upstream_chunks(
    *,
    request: Request,
    upstream: httpx.Response,
) -> AsyncIterator[bytes]:
    async for chunk in upstream.aiter_raw():
        if await request.is_disconnected():
            return
        yield chunk


def _proxy_timeout(*, streaming: bool) -> httpx.Timeout:
    return httpx.Timeout(
        connect=_PROXY_CONNECT_TIMEOUT_SECONDS,
        read=(
            None
            if streaming
            else _PROXY_READ_TIMEOUT_SECONDS
        ),
        write=_PROXY_WRITE_TIMEOUT_SECONDS,
        pool=_PROXY_POOL_TIMEOUT_SECONDS,
    )


async def _bounded_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_bytes = int(declared)
            if declared_bytes < 0:
                raise HTTPException(
                    status_code=400,
                    detail="invalid content length",
                )
            if declared_bytes > _MAX_REQUEST_BYTES:
                raise HTTPException(status_code=413, detail="request too large")
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="invalid content length",
            ) from exc
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="request too large")
        body.extend(chunk)
    return bytes(body)


async def _bounded_json(request: Request) -> dict[str, object]:
    body = await _bounded_body(request)
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc
    if type(payload) is not dict:
        raise HTTPException(status_code=400, detail="JSON object required")
    return payload


def _require_api_key(request: Request, expected: str) -> None:
    supplied = request.headers.get("authorization", "")
    if not supplied.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="unauthorized")
    import hmac

    if not hmac.compare_digest(supplied[7:].strip(), expected):
        raise HTTPException(status_code=401, detail="unauthorized")


def _parse_pydantic_float(value: object, *, label: str) -> float:
    try:
        return _PYDANTIC_FLOAT.validate_python(value)
    except ValidationError as exc:
        raise ValueError(f"{label} must be a valid number") from exc


def persist_runtime_shutdown_budget(
    path: Path,
    record: HermesRuntimeShutdownBudgetRecord,
) -> None:
    """Atomically save the exact identity-bound budget used at startup."""

    payload = record.to_bytes()
    if len(payload) > _MAX_RUNTIME_SHUTDOWN_BUDGET_BYTES:
        raise ValueError("runtime shutdown budget record is too large")
    target = path.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
        target.chmod(0o600)
        _fsync_parent(target)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def load_runtime_shutdown_budget(
    path: Path,
) -> (
    HermesRuntimeShutdownBudgetRecord
    | _LegacyRuntimeShutdownBudgetRecord
):
    """Read a canonical identity-bound drain record."""

    try:
        payload = path.expanduser().read_bytes()
    except OSError as exc:
        raise ValueError("runtime shutdown budget is unavailable") from exc
    if (
        not payload
        or len(payload) > _MAX_RUNTIME_SHUTDOWN_BUDGET_BYTES
        or not payload.endswith(b"\n")
    ):
        raise ValueError("runtime shutdown budget is invalid")
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("runtime shutdown budget is invalid") from exc
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("\t")
        if not separator or key in fields:
            raise ValueError("runtime shutdown budget is invalid")
        fields[key] = value
    version = fields.get("version")
    try:
        if version == str(_RUNTIME_SHUTDOWN_BUDGET_VERSION):
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
            if set(fields) != expected:
                raise ValueError
            return HermesRuntimeShutdownBudgetRecord(
                drain_timeout_seconds=int(
                    fields["drain_timeout_seconds"]
                ),
                launcher_pid=int(fields["launcher_pid"]),
                launcher_start_token=fields["launcher_start_token"],
                launcher_service_nonce=fields[
                    "launcher_service_nonce"
                ],
                supervisor_pid=int(fields["supervisor_pid"]),
                supervisor_start_token=fields[
                    "supervisor_start_token"
                ],
                publication_instance_nonce=fields[
                    "publication_instance_nonce"
                ],
            )
        if version in {"1", "2"}:
            expected = {
                "version",
                "drain_timeout_seconds",
                "supervisor_pid",
                "supervisor_start_token",
                "service_nonce",
            }
            if version == "2":
                expected.add("publication_instance_nonce")
            if set(fields) != expected:
                raise ValueError
            record = _LegacyRuntimeShutdownBudgetRecord(
                drain_timeout_seconds=int(
                    fields["drain_timeout_seconds"]
                ),
                supervisor_pid=int(fields["supervisor_pid"]),
                supervisor_start_token=fields[
                    "supervisor_start_token"
                ],
                service_nonce=fields["service_nonce"],
                publication_instance_nonce=fields.get(
                    "publication_instance_nonce"
                ),
            )
            if not 1 <= record.drain_timeout_seconds <= (
                _MAX_RUNTIME_DRAIN_TIMEOUT_SECONDS
            ):
                raise ValueError
            _ = record.launcher_identity
            if record.publication_instance_nonce is not None:
                _validate_identity_nonce(
                    record.publication_instance_nonce,
                    label="runtime publication instance nonce",
                )
            return record
        raise ValueError
    except (KeyError, ValueError) as exc:
        raise ValueError("runtime shutdown budget is invalid") from exc


def _build_supervisor_server(
    controller: RuntimeController,
    config: HermesRuntimeSupervisorConfig,
    *,
    shutdown_budget_publication: (
        _RuntimeShutdownBudgetPublication | None
    ) = None,
) -> _HermesRuntimeUvicornServer:
    coordinator = _RuntimeShutdownCoordinator(controller)
    app = create_supervisor_app(
        controller,
        response_timeout_seconds=config.decision_timeout_seconds,
        shutdown_coordinator=coordinator,
    )
    server_config = uvicorn.Config(
        app,
        host=config.host,
        port=config.port,
        access_log=False,
        server_header=False,
        timeout_graceful_shutdown=(
            config.shutdown_budget.drain_timeout_seconds
        ),
    )
    return _HermesRuntimeUvicornServer(
        server_config,
        shutdown_coordinator=coordinator,
        shutdown_budget_publication=shutdown_budget_publication,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the attested HealthMes Hermes decision runtime."
    )
    default_home = Path(os.environ.get("HERMES_HOME", "~/.hermes/decision"))
    parser.add_argument("--hermes-home", default=str(default_home))
    parser.add_argument("--manifest")
    parser.add_argument("--attestation-key")
    parser.add_argument("--vendor-root", default=str(Path.cwd()))
    parser.add_argument(
        "--host",
        default=os.environ.get(
            "HEALTHMES_DECISION_RUNTIME_HOST",
            "127.0.0.1",
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(
            os.environ.get("HEALTHMES_DECISION_RUNTIME_PORT", "8645")
        ),
    )
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=float(
            os.environ.get(
                "HEALTHMES_DECISION_RUNTIME_STARTUP_TIMEOUT_SECONDS",
                "30",
            )
        ),
    )
    parser.add_argument(
        "--mcp-probe-timeout",
        type=float,
        default=float(
            os.environ.get(
                "HEALTHMES_DECISION_RUNTIME_MCP_PROBE_TIMEOUT_SECONDS",
                "5",
            )
        ),
    )
    parser.add_argument(
        "--health-check-interval",
        type=float,
        default=float(
            os.environ.get(
                "HEALTHMES_DECISION_RUNTIME_HEALTH_INTERVAL_SECONDS",
                "2",
            )
        ),
    )
    parser.add_argument(
        "--health-check-timeout",
        type=float,
        default=float(
            os.environ.get(
                "HEALTHMES_DECISION_RUNTIME_HEALTH_TIMEOUT_SECONDS",
                "1",
            )
        ),
    )
    parser.add_argument(
        "--unhealthy-threshold",
        type=int,
        default=int(
            os.environ.get(
                "HEALTHMES_DECISION_RUNTIME_UNHEALTHY_THRESHOLD",
                "3",
            )
        ),
    )
    parser.add_argument(
        "--restart-backoff-initial",
        type=float,
        default=float(
            os.environ.get(
                "HEALTHMES_DECISION_RUNTIME_RESTART_BACKOFF_INITIAL_SECONDS",
                "0.25",
            )
        ),
    )
    parser.add_argument(
        "--restart-backoff-max",
        type=float,
        default=float(
            os.environ.get(
                "HEALTHMES_DECISION_RUNTIME_RESTART_BACKOFF_MAX_SECONDS",
                "5",
            )
        ),
    )
    parser.add_argument(
        "--max-concurrent-responses",
        type=int,
        default=int(
            os.environ.get(
                "HEALTHMES_DECISION_RUNTIME_MAX_CONCURRENT_RESPONSES",
                "8",
            )
        ),
    )
    parser.add_argument(
        "--decision-timeout",
        default=os.environ.get(
            "HEALTHMES_DECISION_TIMEOUT_SECONDS",
            "60",
        ),
    )
    parser.add_argument(
        "--child-term-timeout",
        default=os.environ.get(
            "HEALTHMES_DECISION_RUNTIME_CHILD_TERM_TIMEOUT_SECONDS",
            str(_MAX_CHILD_TERM_TIMEOUT_SECONDS),
        ),
    )
    parser.add_argument(
        "--child-kill-timeout",
        default=os.environ.get(
            "HEALTHMES_DECISION_RUNTIME_CHILD_KILL_TIMEOUT_SECONDS",
            str(_MAX_CHILD_KILL_TIMEOUT_SECONDS),
        ),
    )
    parser.add_argument(
        "--shutdown-budget-path",
        default=os.environ.get(
            "HEALTHMES_DECISION_RUNTIME_SHUTDOWN_BUDGET_PATH",
        ),
    )
    parser.add_argument(
        "--runtime-process-action",
        choices=("probe", "signal", "wait"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--runtime-process-pid",
        type=int,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--runtime-process-start-token",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--runtime-process-timeout",
        type=float,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--runtime-process-group-pgid",
        type=int,
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.runtime_process_group_pgid is not None:
        status = _run_runtime_process_group_probe(
            pgid=args.runtime_process_group_pgid,
            timeout_seconds=args.runtime_process_timeout,
        )
        if status:
            raise SystemExit(status)
        return
    if args.runtime_process_action is not None:
        if (
            args.runtime_process_pid is None
            or args.runtime_process_start_token is None
        ):
            raise SystemExit(
                "runtime process action requires PID and start token"
            )
        status = _run_runtime_process_action(
            action=args.runtime_process_action,
            pid=args.runtime_process_pid,
            start_token=args.runtime_process_start_token,
            timeout_seconds=args.runtime_process_timeout,
        )
        if status:
            raise SystemExit(status)
        return
    home = Path(args.hermes_home).expanduser().resolve()
    try:
        decision_timeout = _parse_pydantic_float(
            args.decision_timeout,
            label="decision timeout",
        )
        child_term_timeout = _parse_pydantic_float(
            args.child_term_timeout,
            label="child TERM timeout",
        )
        child_kill_timeout = _parse_pydantic_float(
            args.child_kill_timeout,
            label="child KILL timeout",
        )
        config = HermesRuntimeSupervisorConfig(
            hermes_home=home,
            manifest_path=(
                Path(args.manifest).expanduser()
                if args.manifest
                else home / "runtime-manifest.json"
            ),
            attestation_key_path=(
                Path(args.attestation_key).expanduser()
                if args.attestation_key
                else home / "runtime-attestation.key"
            ),
            vendor_root=Path(args.vendor_root).expanduser().resolve(),
            host=args.host,
            port=args.port,
            startup_timeout_seconds=args.startup_timeout,
            mcp_probe_timeout_seconds=args.mcp_probe_timeout,
            health_check_interval_seconds=args.health_check_interval,
            health_check_timeout_seconds=args.health_check_timeout,
            unhealthy_threshold=args.unhealthy_threshold,
            restart_backoff_initial_seconds=args.restart_backoff_initial,
            restart_backoff_max_seconds=args.restart_backoff_max,
            max_concurrent_responses=args.max_concurrent_responses,
            decision_timeout_seconds=decision_timeout,
            child_term_timeout_seconds=child_term_timeout,
            child_kill_timeout_seconds=child_kill_timeout,
            shutdown_budget_path=(
                Path(args.shutdown_budget_path).expanduser()
                if args.shutdown_budget_path
                else None
            ),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    controller = HermesRuntimeProcess(config)
    publication: _RuntimeShutdownBudgetPublication | None = None
    if config.shutdown_budget_path is not None:
        try:
            supervisor_identity = capture_runtime_supervisor_identity()
            launcher_identity = capture_runtime_launcher_identity(
                os.environ,
                supervisor_identity=supervisor_identity,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        publication = _RuntimeShutdownBudgetPublication(
            path=config.shutdown_budget_path,
            record=HermesRuntimeShutdownBudgetRecord(
                drain_timeout_seconds=(
                    config.shutdown_budget.drain_timeout_seconds
                ),
                launcher_pid=launcher_identity.pid,
                launcher_start_token=launcher_identity.start_token,
                launcher_service_nonce=(
                    launcher_identity.service_nonce
                ),
                supervisor_pid=supervisor_identity.pid,
                supervisor_start_token=(
                    supervisor_identity.start_token
                ),
            ),
        )
    server = _build_supervisor_server(
        controller,
        config,
        shutdown_budget_publication=publication,
    )
    server.run()


if __name__ == "__main__":
    main()
