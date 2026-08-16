from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from healthmes.decision.hermes_profile import (
    HERMES_DECISION_MCP_TOOL_NAMES,
    HERMES_DECISION_NATIVE_TOOLSET_DENYLIST,
    HermesDecisionProfileAssertion,
)
from healthmes.decision.responses import (
    HermesHttpResponsesTransport,
    HermesResponsesContractError,
    HermesResponsesHttpResult,
    HermesRuntimeAttestationAssertion,
)
from healthmes.hermes_runtime_identity import (
    HERMES_RUNTIME_ATTESTATION_PATH,
    HERMES_RUNTIME_HOME_ARTIFACT_NAMES,
    HERMES_RUNTIME_PROVIDER_ENV_NAMES,
    HermesDecisionRuntimeManifest,
    HermesRuntimeIdentityError,
    build_runtime_manifest,
    runtime_home_artifact_sha256,
    sign_runtime_attestation,
    validate_expected_runtime,
    validate_supervised_runtime,
    write_new_attestation_key,
    write_runtime_manifest,
)
from healthmes.hermes_runtime_supervisor import (
    HermesRuntimeProcess,
    HermesRuntimeState,
    _stream_upstream_response,
    build_child_environment,
    create_supervisor_app,
)

MODEL = "decision-model"
PROVIDER = "openai"
API_KEY = "k" * 64
PUBLIC_ORIGIN = "http://127.0.0.1:8645"
INTERNAL_ORIGIN = "http://127.0.0.1:8646"
PROVIDER_ENV = {"OPENAI_API_KEY": "provider-secret"}


@dataclass(frozen=True)
class RuntimeBundle:
    home: Path
    vendor_root: Path
    profile_path: Path
    manifest_path: Path
    key_path: Path
    manifest: HermesDecisionRuntimeManifest
    key: bytes
    profile_digest: str


@pytest.fixture
def runtime_bundle(tmp_path: Path) -> RuntimeBundle:
    home = tmp_path / "decision"
    home.mkdir(mode=0o700)
    profile_path = home / "config.yaml"
    profile_path.write_text(
        yaml.safe_dump(
            {
                "model": {
                    "default": MODEL,
                    "provider": PROVIDER,
                },
                "platforms": {
                    "api_server": {
                        "enabled": True,
                        "extra": {
                            "host": "127.0.0.1",
                            "port": 8646,
                            "key": API_KEY,
                            "model_name": "healthmes-decision-runtime",
                            "model_routes": {
                                MODEL: {
                                    "model": MODEL,
                                    "provider": PROVIDER,
                                }
                            },
                        },
                    }
                },
                "platform_toolsets": {"api_server": ["healthmes"]},
                "agent": {
                    "disabled_toolsets": sorted(
                        HERMES_DECISION_NATIVE_TOOLSET_DENYLIST
                    )
                },
                "mcp_servers": {
                    "healthmes": {
                        "url": "http://127.0.0.1:8100/mcp",
                        "tools": {
                            "include": list(
                                HERMES_DECISION_MCP_TOOL_NAMES
                            )
                        },
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    for name, content in {
        "SOUL.md": "Dedicated decision runtime.\n",
        ".env": "# Managed by HealthMes.\n",
        ".no-bundled-skills": "",
    }.items():
        (home / name).write_text(content, encoding="utf-8")
    for name in HERMES_RUNTIME_HOME_ARTIFACT_NAMES:
        (home / name).chmod(0o600)

    vendor_root = tmp_path / "vendor"
    from healthmes import hermes_runtime_identity

    for index, relative in enumerate(
        hermes_runtime_identity._VENDOR_FINGERPRINT_PATHS
    ):
        path = vendor_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"vendor artifact {index}\n", encoding="utf-8")

    key_path = home / "runtime-attestation.key"
    key = write_new_attestation_key(key_path)
    profile_digest = HermesDecisionProfileAssertion(
        profile_path,
        expected_model=MODEL,
        expected_provider=PROVIDER,
        expected_api_key=API_KEY,
    ).verify()
    manifest = build_runtime_manifest(
        profile_bytes=profile_path.read_bytes(),
        profile_semantic_digest=profile_digest,
        model=MODEL,
        provider=PROVIDER,
        api_key=API_KEY,
        attestation_key=key,
        hermes_home=home,
        public_origin=PUBLIC_ORIGIN,
        internal_origin=INTERNAL_ORIGIN,
        vendor_root=vendor_root,
        launch_argv=("/bin/true",),
        home_artifact_sha256=runtime_home_artifact_sha256(home),
        provider_environment=PROVIDER_ENV,
    )
    manifest_path = home / "runtime-manifest.json"
    write_runtime_manifest(manifest_path, manifest)
    return RuntimeBundle(
        home=home,
        vendor_root=vendor_root,
        profile_path=profile_path,
        manifest_path=manifest_path,
        key_path=key_path,
        manifest=manifest,
        key=key,
        profile_digest=profile_digest,
    )


def test_expected_runtime_binds_profile_home_origin_and_credentials(
    runtime_bundle: RuntimeBundle,
) -> None:
    manifest, key = validate_expected_runtime(
        manifest_path=runtime_bundle.manifest_path,
        attestation_key_path=runtime_bundle.key_path,
        profile_path=runtime_bundle.profile_path,
        profile_semantic_digest=runtime_bundle.profile_digest,
        expected_origin=PUBLIC_ORIGIN,
        expected_model=MODEL,
        expected_provider=PROVIDER,
        expected_api_key=API_KEY,
    )

    assert manifest == runtime_bundle.manifest
    assert key == runtime_bundle.key


@pytest.mark.parametrize(
    ("drift", "expected_code"),
    (
        ("home", "hermes_runtime_home_artifact_mismatch"),
        ("vendor", "hermes_runtime_vendor_fingerprint_mismatch"),
        ("provider", "hermes_runtime_provider_environment_mismatch"),
        ("broad_home", "hermes_runtime_broad_home_rejected"),
        ("launch", "hermes_runtime_launch_identity_mismatch"),
    ),
)
def test_supervised_runtime_rejects_identity_drift(
    runtime_bundle: RuntimeBundle,
    drift: str,
    expected_code: str,
) -> None:
    environment = dict(PROVIDER_ENV)
    expected_launch_argv = runtime_bundle.manifest.launch_argv
    if drift == "home":
        (runtime_bundle.home / "SOUL.md").write_text(
            "Changed after bootstrap.\n",
            encoding="utf-8",
        )
    elif drift == "vendor":
        (runtime_bundle.vendor_root / "gateway" / "run.py").write_text(
            "changed vendor runtime\n",
            encoding="utf-8",
        )
    elif drift == "provider":
        environment["OPENAI_API_KEY"] = "changed-provider-secret"
    elif drift == "broad_home":
        cron = runtime_bundle.home / "cron"
        cron.mkdir()
        (cron / "jobs.json").write_text("{}", encoding="utf-8")
    elif drift == "launch":
        expected_launch_argv = ("/bin/false",)

    with pytest.raises(HermesRuntimeIdentityError, match=expected_code):
        validate_supervised_runtime(
            manifest_path=runtime_bundle.manifest_path,
            attestation_key_path=runtime_bundle.key_path,
            hermes_home=runtime_bundle.home,
            vendor_root=runtime_bundle.vendor_root,
            environment=environment,
            expected_launch_argv=expected_launch_argv,
        )


def test_child_environment_is_exact_and_scrubs_general_reasoning(
    runtime_bundle: RuntimeBundle,
) -> None:
    source = {
        **PROVIDER_ENV,
        "PATH": "/unsafe/path",
        "TELEGRAM_BOT_TOKEN": "telegram-secret",
        "HEALTHMES_HERMES_WEBHOOK_SECRET": "webhook-secret",
        "HERMES_HOME": "/wrong/home",
    }

    child = build_child_environment(
        source,
        manifest=runtime_bundle.manifest,
    )

    assert child["OPENAI_API_KEY"] == PROVIDER_ENV["OPENAI_API_KEY"]
    assert child["HERMES_HOME"] == str(runtime_bundle.home)
    assert "TELEGRAM_BOT_TOKEN" not in child
    assert "HEALTHMES_HERMES_WEBHOOK_SECRET" not in child
    assert "PATH" not in child
    assert set(child) - HERMES_RUNTIME_PROVIDER_ENV_NAMES == {
        item.name
        for item in runtime_bundle.manifest.required_environment
    }


def test_runtime_process_state_rejects_a_stopped_child(
    runtime_bundle: RuntimeBundle,
) -> None:
    process = object.__new__(HermesRuntimeProcess)
    process._state = HermesRuntimeState(
        manifest=runtime_bundle.manifest,
        attestation_key=runtime_bundle.key,
        api_key=API_KEY,
    )
    process._process = SimpleNamespace(returncode=1)

    with pytest.raises(
        HermesRuntimeIdentityError,
        match="hermes_runtime_child_not_running",
    ):
        _ = process.state


@pytest.mark.asyncio
async def test_transport_attests_immediately_before_responses(
    runtime_bundle: RuntimeBundle,
) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.path == HERMES_RUNTIME_ATTESTATION_PATH
        nonce = json.loads(request.content)["nonce"]
        proof = sign_runtime_attestation(
            manifest=runtime_bundle.manifest,
            key=runtime_bundle.key,
            nonce=nonce,
        )
        return httpx.Response(
            200,
            json=proof.model_dump(mode="json", by_alias=True),
        )

    transport = HermesHttpResponsesTransport(
        base_url=PUBLIC_ORIGIN,
        api_key=API_KEY,
        runtime_attestation=HermesRuntimeAttestationAssertion(
            manifest_path=runtime_bundle.manifest_path,
            attestation_key_path=runtime_bundle.key_path,
            profile_assertion=HermesDecisionProfileAssertion(
                runtime_bundle.profile_path,
                expected_model=MODEL,
                expected_provider=PROVIDER,
                expected_api_key=API_KEY,
            ),
            expected_origin=PUBLIC_ORIGIN,
            expected_model=MODEL,
            expected_provider=PROVIDER,
            expected_api_key=API_KEY,
        ),
        http_transport=httpx.MockTransport(handler),
    )
    responses_called = False

    async def fake_responses(
        _payload: Any,
        *,
        timeout_seconds: float,
    ) -> HermesResponsesHttpResult:
        nonlocal responses_called
        assert timeout_seconds == 5
        responses_called = True
        return HermesResponsesHttpResult(
            payload={},
            session_id="session",
        )

    transport._request_sse_response = fake_responses

    await transport.create_response(
        {"stream": True, "store": False},
        timeout_seconds=5,
    )

    assert paths == [HERMES_RUNTIME_ATTESTATION_PATH]
    assert responses_called is True


@pytest.mark.asyncio
async def test_transport_rejects_attestation_mismatch_before_responses(
    runtime_bundle: RuntimeBundle,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        nonce = json.loads(request.content)["nonce"]
        proof = sign_runtime_attestation(
            manifest=runtime_bundle.manifest,
            key=runtime_bundle.key,
            nonce=nonce,
        ).model_dump(mode="json", by_alias=True)
        proof["signature"] = "0" * 64
        return httpx.Response(200, json=proof)

    transport = HermesHttpResponsesTransport(
        base_url=PUBLIC_ORIGIN,
        api_key=API_KEY,
        runtime_attestation=HermesRuntimeAttestationAssertion(
            manifest_path=runtime_bundle.manifest_path,
            attestation_key_path=runtime_bundle.key_path,
            profile_assertion=HermesDecisionProfileAssertion(
                runtime_bundle.profile_path,
                expected_model=MODEL,
                expected_provider=PROVIDER,
                expected_api_key=API_KEY,
            ),
            expected_origin=PUBLIC_ORIGIN,
            expected_model=MODEL,
            expected_provider=PROVIDER,
            expected_api_key=API_KEY,
        ),
        http_transport=httpx.MockTransport(handler),
    )

    with pytest.raises(
        HermesResponsesContractError,
        match="hermes_runtime_attestation_signature_mismatch",
    ):
        await transport.create_response(
            {"stream": True, "store": False},
            timeout_seconds=5,
        )


def test_arbitrary_remote_runtime_without_attestation_is_rejected() -> None:
    with pytest.raises(
        ValueError,
        match="requires content-bound attestation",
    ):
        HermesHttpResponsesTransport(
            base_url="https://hermes.example.com",
            api_key=API_KEY,
        )


class _FakeController:
    def __init__(
        self,
        state: HermesRuntimeState,
        *,
        failure: HermesRuntimeIdentityError | None = None,
    ) -> None:
        self._state = state
        self.failure = failure
        self.revalidations = 0

    @property
    def state(self) -> HermesRuntimeState:
        if self.failure is not None:
            raise self.failure
        return self._state

    def revalidate(self) -> HermesRuntimeState:
        self.revalidations += 1
        return self.state

    async def start(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


def test_supervisor_exposes_only_bounded_runtime_ingress(
    runtime_bundle: RuntimeBundle,
) -> None:
    controller = _FakeController(
        HermesRuntimeState(
            manifest=runtime_bundle.manifest,
            attestation_key=runtime_bundle.key,
            api_key=API_KEY,
        )
    )
    app = create_supervisor_app(
        controller,
        proxy_transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"object": "list"})
        ),
    )

    with TestClient(app) as client:
        assert client.post("/v1/model/iterations").status_code == 404
        assert client.post("/v1/chat/completions").status_code == 404
        assert client.post("/webhooks/healthmes-alerts").status_code == 404
        response = client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"stream": False, "store": False},
        )

    assert response.status_code == 400


def test_supervisor_authenticates_before_parsing_responses_body(
    runtime_bundle: RuntimeBundle,
) -> None:
    controller = _FakeController(
        HermesRuntimeState(
            manifest=runtime_bundle.manifest,
            attestation_key=runtime_bundle.key,
            api_key=API_KEY,
        )
    )

    with TestClient(create_supervisor_app(controller)) as client:
        response = client.post(
            "/v1/responses",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 401


def test_supervisor_reports_stopped_runtime_as_unavailable(
    runtime_bundle: RuntimeBundle,
) -> None:
    controller = _FakeController(
        HermesRuntimeState(
            manifest=runtime_bundle.manifest,
            attestation_key=runtime_bundle.key,
            api_key=API_KEY,
        ),
        failure=HermesRuntimeIdentityError(
            "hermes_runtime_child_not_running"
        ),
    )

    with TestClient(create_supervisor_app(controller)) as client:
        response = client.get("/healthmes/runtime-health")

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_proxy_stream_cancel_closes_upstream_connection() -> None:
    stream_started = asyncio.Event()
    stream_closed = asyncio.Event()

    class BlockingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first"
            stream_started.set()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            stream_closed.set()

    class ConnectedRequest:
        async def is_disconnected(self) -> bool:
            return False

    class ClosingClient:
        def __init__(self) -> None:
            self.closed = asyncio.Event()

        async def aclose(self) -> None:
            self.closed.set()

    upstream = httpx.Response(200, stream=BlockingStream())
    client = ClosingClient()
    stream = _stream_upstream_response(
        request=ConnectedRequest(),  # type: ignore[arg-type]
        upstream=upstream,
        client=client,  # type: ignore[arg-type]
    )
    assert await anext(stream) == b"first"
    next_chunk = asyncio.create_task(anext(stream))
    await asyncio.wait_for(stream_started.wait(), timeout=1)
    next_chunk.cancel()

    with pytest.raises(asyncio.CancelledError):
        await next_chunk

    await asyncio.wait_for(stream_closed.wait(), timeout=1)
    await asyncio.wait_for(client.closed.wait(), timeout=1)
