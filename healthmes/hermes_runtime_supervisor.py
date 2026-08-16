"""HealthMes-owned supervisor and narrow proxy for one Hermes decision runtime."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import math
import os
import signal
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Mapping,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import quote

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

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
    HermesRuntimeIdentityError,
    HermesRuntimeMcpConnection,
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
        )
        for label, value in durations:
            if not math.isfinite(value) or value <= 0:
                raise ValueError(
                    f"{label} must be finite and positive"
                )
        if self.unhealthy_threshold < 1:
            raise ValueError("unhealthy threshold must be positive")
        if (
            self.restart_backoff_max_seconds
            < self.restart_backoff_initial_seconds
        ):
            raise ValueError(
                "restart backoff max must be at least the initial delay"
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
    _release_callback: Callable[[], None]
    _released: bool = False

    def release(self) -> None:
        """Release the child generation exactly once."""

        if self._released:
            return
        self._released = True
        self._release_callback()


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

    async def aclose(self) -> None:
        """Stop the dedicated child."""


class HermesRuntimeProcess:
    """Launch the exact manifest-bound Hermes child in a scrubbed environment."""

    def __init__(
        self,
        config: HermesRuntimeSupervisorConfig,
        *,
        environ: Mapping[str, str] | None = None,
        mcp_inventory_probe: McpInventoryProbe | None = None,
    ) -> None:
        self._config = config
        self._environ = dict(os.environ if environ is None else environ)
        self._mcp_inventory_probe = (
            mcp_inventory_probe or _probe_live_mcp_schema_digests
        )
        self._state: HermesRuntimeState | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._launch_argv: tuple[str, ...] | None = None
        self._child_generation = 0
        self._healthy = False
        self._lifecycle_state: _LifecycleState = "new"
        self._lifecycle_lock = asyncio.Lock()
        self._child_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._monitor_task: asyncio.Task[None] | None = None

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

    async def _attest_locked(self) -> HermesRuntimeState:
        state = self.revalidate()
        inventory = await self._probe_runtime_inventory()
        verified = self.revalidate()
        if (
            verified != state
            or inventory != state.mcp_inventory
        ):
            raise HermesRuntimeIdentityError(
                "hermes_runtime_mcp_inventory_changed"
            )
        return state

    async def attest(self) -> HermesRuntimeState:
        async with self._child_lock:
            return await self._attest_locked()

    async def acquire_response_lease(
        self,
    ) -> HermesRuntimeResponseLease:
        """Attest and pin the current child until the caller releases it."""

        await self._child_lock.acquire()
        try:
            state = await self._attest_locked()
            process = self._process
            if process is None or process.returncode is not None:
                raise HermesRuntimeIdentityError(
                    "hermes_runtime_child_not_running"
                )
            return HermesRuntimeResponseLease(
                state=state,
                generation=self._child_generation,
                _release_callback=self._child_lock.release,
            )
        except BaseException:
            self._child_lock.release()
            raise

    async def start(self) -> None:
        """Strictly launch and verify one child for direct callers."""

        async with self._lifecycle_lock:
            if self._lifecycle_state not in {"new", "running"}:
                return
            self._lifecycle_state = "running"
        async with self._child_lock:
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
        self._launch_argv = launch_manifest.launch_argv
        try:
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
                    async with self._child_lock:
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
                    async with self._child_lock:
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
                async with self._child_lock:
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
        return self._validate_bound_state(expected=state) == state

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

    async def _stop_child(self) -> None:
        process = self._process
        self._state = None
        self._launch_argv = None
        self._healthy = False
        if process is None:
            return
        if process.returncode is not None:
            if self._process is process:
                self._process = None
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            if self._process is process:
                self._process = None
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                if self._process is process:
                    self._process = None
                return
            await process.wait()
        if self._process is process:
            self._process = None

    async def aclose(self) -> None:
        async with self._close_lock:
            async with self._lifecycle_lock:
                if self._lifecycle_state == "closed":
                    return
                self._lifecycle_state = "closing"
                monitor = self._monitor_task
                self._monitor_task = None
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
            async with self._child_lock:
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
) -> FastAPI:
    """Expose only attestation and the Responses/session endpoints."""

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
            await controller.aclose()

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
        lease: HermesRuntimeResponseLease | None = None
        try:
            lease = await controller.acquire_response_lease()
            _require_api_key(request, lease.state.api_key)
        except HermesRuntimeIdentityError as exc:
            raise HTTPException(
                status_code=503,
                detail="runtime identity unavailable",
            ) from exc
        try:
            return await _proxy(
                request,
                "/v1/responses",
                body=encoded,
                accept="text/event-stream",
                state=lease.state,
                response_lease=lease,
            )
        except BaseException:
            lease.release()
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
        client: httpx.AsyncClient | None = None
        try:
            client = httpx.AsyncClient(
                base_url=state.manifest.internal_origin,
                follow_redirects=False,
                transport=proxy_transport,
            )
            upstream_request = client.build_request(
                request.method,
                target,
                content=body,
                headers=headers,
            )
            upstream = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            if client is not None:
                await client.aclose()
            if response_lease is not None:
                response_lease.release()
            raise HTTPException(
                status_code=503,
                detail="runtime upstream unavailable",
            ) from exc
        except BaseException:
            if client is not None:
                await client.aclose()
            if response_lease is not None:
                response_lease.release()
            raise
        response_headers = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() in _FORWARDED_RESPONSE_HEADERS
        }
        response_headers["Cache-Control"] = "no-store"

        return StreamingResponse(
            _stream_upstream_response(
                request=request,
                upstream=upstream,
                client=client,
                response_lease=response_lease,
            ),
            status_code=upstream.status_code,
            headers=response_headers,
        )

    return app


async def _stream_upstream_response(
    *,
    request: Request,
    upstream: httpx.Response,
    client: httpx.AsyncClient,
    response_lease: HermesRuntimeResponseLease | None = None,
) -> AsyncIterator[bytes]:
    """Close Hermes immediately when the HealthMes caller goes away."""

    try:
        async for chunk in upstream.aiter_raw():
            if await request.is_disconnected():
                return
            yield chunk
    finally:
        # Hermes maps this upstream disconnect to agent.interrupt().
        try:
            await upstream.aclose()
        finally:
            try:
                await client.aclose()
            finally:
                if response_lease is not None:
                    response_lease.release()


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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    home = Path(args.hermes_home).expanduser().resolve()
    try:
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
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    controller = HermesRuntimeProcess(config)
    uvicorn.run(
        create_supervisor_app(controller),
        host=config.host,
        port=config.port,
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
