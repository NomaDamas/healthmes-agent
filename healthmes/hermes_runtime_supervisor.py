"""HealthMes-owned supervisor and narrow proxy for one Hermes decision runtime."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import signal
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from healthmes.hermes_runtime_identity import (
    HERMES_RUNTIME_ATTESTATION_PATH,
    HERMES_RUNTIME_HEALTH_PATH,
    HERMES_RUNTIME_PROVIDER_ENV_NAMES,
    HermesDecisionRuntimeManifest,
    HermesRuntimeIdentityError,
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


@dataclass(frozen=True, slots=True)
class HermesRuntimeState:
    """Validated immutable state shared by attestation and proxy routes."""

    manifest: HermesDecisionRuntimeManifest
    attestation_key: bytes
    api_key: str


class RuntimeController(Protocol):
    """Lifecycle boundary used by the ASGI app and isolated tests."""

    @property
    def state(self) -> HermesRuntimeState:
        """Return state only after successful startup."""

    def revalidate(self) -> HermesRuntimeState:
        """Return state only while the exact launched child remains valid."""

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
    ) -> None:
        self._config = config
        self._environ = dict(os.environ if environ is None else environ)
        self._state: HermesRuntimeState | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._launch_argv: tuple[str, ...] | None = None

    @property
    def state(self) -> HermesRuntimeState:
        if self._state is None:
            raise RuntimeError("Hermes runtime is not ready")
        process = self._process
        if process is None or process.returncode is not None:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_not_running"
            )
        return self._state

    def revalidate(self) -> HermesRuntimeState:
        state = self.state
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
            manifest != state.manifest
            or key != state.attestation_key
            or not hmac.compare_digest(api_key, state.api_key)
        ):
            raise HermesRuntimeIdentityError(
                "hermes_runtime_identity_changed"
            )
        return state

    async def start(self) -> None:
        if self._process is not None:
            return
        config = self._config
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
            await self.aclose()
            raise
        self._state = HermesRuntimeState(
            manifest=manifest,
            attestation_key=key,
            api_key=api_key,
        )

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

    async def aclose(self) -> None:
        process = self._process
        self._process = None
        self._state = None
        self._launch_argv = None
        if process is None or process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            await process.wait()


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
            state = controller.revalidate()
            proof = sign_runtime_attestation(
                manifest=state.manifest,
                key=state.attestation_key,
                nonce=nonce,
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
        try:
            state = controller.revalidate()
        except HermesRuntimeIdentityError as exc:
            raise HTTPException(
                status_code=503,
                detail="runtime identity unavailable",
            ) from exc
        return await _proxy(
            request,
            "/v1/responses",
            body=encoded,
            accept="text/event-stream",
            state=state,
        )

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
        try:
            upstream = await client.send(upstream_request, stream=True)
        except BaseException:
            await client.aclose()
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
) -> AsyncIterator[bytes]:
    """Close Hermes immediately when the HealthMes caller goes away."""

    try:
        async for chunk in upstream.aiter_raw():
            if await request.is_disconnected():
                return
            yield chunk
    finally:
        # Hermes maps this upstream disconnect to agent.interrupt().
        await upstream.aclose()
        await client.aclose()


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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    home = Path(args.hermes_home).expanduser().resolve()
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
    )
    if not 1 <= config.port <= 65_535:
        raise SystemExit("invalid runtime port")
    if config.startup_timeout_seconds <= 0:
        raise SystemExit("startup timeout must be positive")
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
