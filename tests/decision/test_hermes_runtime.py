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
from healthmes.hermes_mcp_inventory import (
    HERMES_DECISION_MCP_INPUT_SCHEMA_SHA256,
    HermesMcpInventoryError,
    expected_hermes_mcp_inventory,
    inventory_from_schema_digests,
    validate_model_visible_mcp_inventory,
)
from healthmes.hermes_runtime_identity import (
    HERMES_RUNTIME_ATTESTATION_PATH,
    HERMES_RUNTIME_HOME_ARTIFACT_NAMES,
    HERMES_RUNTIME_PROVIDER_ENV_NAMES,
    HermesDecisionRuntimeManifest,
    HermesRuntimeIdentityError,
    build_runtime_manifest,
    load_runtime_manifest,
    runtime_execution_artifacts,
    runtime_home_artifact_sha256,
    seal_supervised_runtime,
    sign_runtime_attestation,
    validate_expected_runtime,
    validate_supervised_runtime,
    write_new_attestation_key,
    write_runtime_manifest,
)
from healthmes.hermes_runtime_supervisor import (
    HermesRuntimeProcess,
    HermesRuntimeState,
    HermesRuntimeSupervisorConfig,
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
EXPECTED_MCP_INVENTORY = expected_hermes_mcp_inventory()


@dataclass(frozen=True)
class RuntimeBundle:
    home: Path
    vendor_root: Path
    profile_path: Path
    manifest_path: Path
    key_path: Path
    prepared_manifest: HermesDecisionRuntimeManifest
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
                "compression": {"in_place": True},
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
                            "resources": False,
                            "prompts": False,
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
    vendor_root.mkdir()
    from healthmes import hermes_runtime_identity

    for index, relative in enumerate(
        hermes_runtime_identity._VENDOR_FINGERPRINT_ROOT_FILES
    ):
        path = vendor_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"vendor artifact {index}\n", encoding="utf-8")
    (vendor_root / "run_agent.py").write_text(
        "def main():\n    return None\n",
        encoding="utf-8",
    )
    plugins = vendor_root / "plugins"
    plugins.mkdir(exist_ok=True)
    (plugins / "__init__.py").write_text("", encoding="utf-8")
    for relative in hermes_runtime_identity._VENDOR_FINGERPRINT_TREES:
        directory = vendor_root / relative
        directory.mkdir(parents=True, exist_ok=True)
        runtime_file = directory / "runtime.py"
        runtime_file.write_text(
            f"RUNTIME_TREE = {relative!r}\n",
            encoding="utf-8",
        )
    (vendor_root / "hermes_cli" / "main.py").write_text(
        "def main():\n    return None\n",
        encoding="utf-8",
    )
    for relative in (
        "tools/nested/dispatcher.py",
        "providers/nested/provider.py",
        "plugins/model-providers/openai/runtime.py",
    ):
        path = vendor_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"SOURCE = {relative!r}\n", encoding="utf-8")

    runtime_venv = tmp_path / "runtime-venv"
    runtime_bin = runtime_venv / "bin"
    runtime_bin.mkdir(parents=True)
    launcher = runtime_bin / "python"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    (runtime_venv / "pyvenv.cfg").write_text(
        "home = /test/python\n",
        encoding="utf-8",
    )

    key_path = home / "runtime-attestation.key"
    key = write_new_attestation_key(key_path)
    profile_digest = HermesDecisionProfileAssertion(
        profile_path,
        expected_model=MODEL,
        expected_provider=PROVIDER,
        expected_api_key=API_KEY,
    ).verify()
    prepared_manifest = build_runtime_manifest(
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
        launch_argv=(
            str(launcher),
            "-m",
            "hermes_cli.main",
            "gateway",
            "run",
        ),
        home_artifact_sha256=runtime_home_artifact_sha256(home),
        provider_environment=PROVIDER_ENV,
    )
    manifest_path = home / "runtime-manifest.json"
    write_runtime_manifest(manifest_path, prepared_manifest)
    manifest, _, _ = seal_supervised_runtime(
        manifest_path=manifest_path,
        attestation_key_path=key_path,
        hermes_home=home,
        vendor_root=vendor_root,
        environment=PROVIDER_ENV,
        expected_launch_argv=prepared_manifest.launch_argv,
    )
    return RuntimeBundle(
        home=home,
        vendor_root=vendor_root,
        profile_path=profile_path,
        manifest_path=manifest_path,
        key_path=key_path,
        prepared_manifest=prepared_manifest,
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


@pytest.mark.parametrize("tool_count", (0, 5, 7))
def test_live_inventory_rejects_wrong_model_visible_tool_count(
    tool_count: int,
) -> None:
    schema_digests = dict(HERMES_DECISION_MCP_INPUT_SCHEMA_SHA256)
    if tool_count == 0:
        schema_digests.clear()
    elif tool_count == 5:
        schema_digests.pop("search_wearable")
    else:
        schema_digests["unexpected_tool"] = "0" * 64

    with pytest.raises(
        HermesMcpInventoryError,
        match="hermes_runtime_mcp_inventory_",
    ):
        validate_model_visible_mcp_inventory(schema_digests)


def test_live_inventory_rejects_canonical_tool_schema_drift() -> None:
    schema_digests = dict(HERMES_DECISION_MCP_INPUT_SCHEMA_SHA256)
    schema_digests["search_activity"] = "0" * 64

    with pytest.raises(
        HermesMcpInventoryError,
        match="hermes_runtime_mcp_schema_mismatch",
    ):
        validate_model_visible_mcp_inventory(schema_digests)


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


@pytest.mark.parametrize(
    "relative",
    (
        "hermes_cli/main.py",
        "tools/nested/dispatcher.py",
        "providers/nested/provider.py",
        "plugins/model-providers/openai/runtime.py",
    ),
)
def test_vendor_fingerprint_binds_canonical_runtime_sources(
    runtime_bundle: RuntimeBundle,
    relative: str,
) -> None:
    (runtime_bundle.vendor_root / relative).write_text(
        "changed canonical runtime source\n",
        encoding="utf-8",
    )

    with pytest.raises(
        HermesRuntimeIdentityError,
        match="hermes_runtime_vendor_fingerprint_mismatch",
    ):
        validate_supervised_runtime(
            manifest_path=runtime_bundle.manifest_path,
            attestation_key_path=runtime_bundle.key_path,
            hermes_home=runtime_bundle.home,
            vendor_root=runtime_bundle.vendor_root,
            environment=PROVIDER_ENV,
            expected_launch_argv=runtime_bundle.manifest.launch_argv,
        )


def test_supervised_runtime_rejects_launcher_content_drift(
    runtime_bundle: RuntimeBundle,
) -> None:
    launcher = Path(runtime_bundle.manifest.launch_argv[0])
    launcher.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    launcher.chmod(0o755)

    with pytest.raises(
        HermesRuntimeIdentityError,
        match="hermes_runtime_execution_artifact_mismatch",
    ):
        validate_supervised_runtime(
            manifest_path=runtime_bundle.manifest_path,
            attestation_key_path=runtime_bundle.key_path,
            hermes_home=runtime_bundle.home,
            vendor_root=runtime_bundle.vendor_root,
            environment=PROVIDER_ENV,
            expected_launch_argv=runtime_bundle.manifest.launch_argv,
        )


def test_verified_launcher_rejects_one_byte_over_limit(
    runtime_bundle: RuntimeBundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from healthmes import hermes_runtime_identity

    launcher = Path(runtime_bundle.manifest.launch_argv[0])
    size = launcher.stat().st_size
    assert size > 1
    monkeypatch.setattr(
        hermes_runtime_identity,
        "_MAX_EXECUTION_ARTIFACT_BYTES",
        size - 1,
    )

    with pytest.raises(
        HermesRuntimeIdentityError,
        match="hermes_runtime_execution_artifact_too_large",
    ):
        hermes_runtime_identity.open_verified_runtime_launcher(
            runtime_bundle.manifest
        )


def test_execution_artifact_rejects_replacement_during_fingerprint(
    runtime_bundle: RuntimeBundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from healthmes import hermes_runtime_identity

    launcher = Path(runtime_bundle.manifest.launch_argv[0])
    original_read = hermes_runtime_identity._read_regular_file

    def replacing_read(
        path: Path,
        *,
        code: str,
        max_bytes: int,
        owner_only: bool,
    ) -> bytes:
        content = original_read(
            path,
            code=code,
            max_bytes=max_bytes,
            owner_only=owner_only,
        )
        if path == launcher.resolve():
            launcher.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            launcher.chmod(0o755)
        return content

    monkeypatch.setattr(
        hermes_runtime_identity,
        "_read_regular_file",
        replacing_read,
    )

    with pytest.raises(
        HermesRuntimeIdentityError,
        match="hermes_runtime_execution_artifact_unsafe",
    ):
        runtime_execution_artifacts(
            manifest=runtime_bundle.manifest,
            vendor_root=runtime_bundle.vendor_root,
        )


def test_supervised_runtime_rejects_child_environment_drift(
    runtime_bundle: RuntimeBundle,
) -> None:
    launcher = Path(runtime_bundle.manifest.launch_argv[0])
    (launcher.parent.parent / "pyvenv.cfg").write_text(
        "home = /changed/python\n",
        encoding="utf-8",
    )

    with pytest.raises(
        HermesRuntimeIdentityError,
        match="hermes_runtime_execution_artifact_mismatch",
    ):
        validate_supervised_runtime(
            manifest_path=runtime_bundle.manifest_path,
            attestation_key_path=runtime_bundle.key_path,
            hermes_home=runtime_bundle.home,
            vendor_root=runtime_bundle.vendor_root,
            environment=PROVIDER_ENV,
            expected_launch_argv=runtime_bundle.manifest.launch_argv,
        )


def test_supervised_runtime_rejects_supervisor_interpreter_drift(
    runtime_bundle: RuntimeBundle,
    tmp_path: Path,
) -> None:
    interpreter = tmp_path / "supervisor-python"
    interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    interpreter.chmod(0o755)
    write_runtime_manifest(
        runtime_bundle.manifest_path,
        runtime_bundle.prepared_manifest,
    )
    seal_supervised_runtime(
        manifest_path=runtime_bundle.manifest_path,
        attestation_key_path=runtime_bundle.key_path,
        hermes_home=runtime_bundle.home,
        vendor_root=runtime_bundle.vendor_root,
        environment=PROVIDER_ENV,
        expected_launch_argv=runtime_bundle.prepared_manifest.launch_argv,
        supervisor_interpreter=interpreter,
    )
    interpreter.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    interpreter.chmod(0o755)

    with pytest.raises(
        HermesRuntimeIdentityError,
        match="hermes_runtime_execution_artifact_mismatch",
    ):
        validate_supervised_runtime(
            manifest_path=runtime_bundle.manifest_path,
            attestation_key_path=runtime_bundle.key_path,
            hermes_home=runtime_bundle.home,
            vendor_root=runtime_bundle.vendor_root,
            environment=PROVIDER_ENV,
            expected_launch_argv=(
                runtime_bundle.prepared_manifest.launch_argv
            ),
            supervisor_interpreter=interpreter,
        )


def test_unsealed_runtime_rejects_control_source_drift(
    runtime_bundle: RuntimeBundle,
    tmp_path: Path,
) -> None:
    from healthmes import hermes_runtime_supervisor

    supervisor_source = Path(hermes_runtime_supervisor.__file__)
    copied_supervisor = tmp_path / "hermes_runtime_supervisor.py"
    copied_supervisor.write_bytes(supervisor_source.read_bytes())
    write_runtime_manifest(
        runtime_bundle.manifest_path,
        runtime_bundle.prepared_manifest,
    )
    seal_supervised_runtime(
        manifest_path=runtime_bundle.manifest_path,
        attestation_key_path=runtime_bundle.key_path,
        hermes_home=runtime_bundle.home,
        vendor_root=runtime_bundle.vendor_root,
        environment=PROVIDER_ENV,
        expected_launch_argv=runtime_bundle.prepared_manifest.launch_argv,
        supervisor_module=copied_supervisor,
    )

    copied_supervisor.write_text(
        "# changed supervisor source\n",
        encoding="utf-8",
    )
    write_runtime_manifest(
        runtime_bundle.manifest_path,
        runtime_bundle.prepared_manifest,
    )
    with pytest.raises(
        HermesRuntimeIdentityError,
        match="hermes_runtime_control_source_mismatch",
    ):
        seal_supervised_runtime(
            manifest_path=runtime_bundle.manifest_path,
            attestation_key_path=runtime_bundle.key_path,
            hermes_home=runtime_bundle.home,
            vendor_root=runtime_bundle.vendor_root,
            environment=PROVIDER_ENV,
            expected_launch_argv=(
                runtime_bundle.prepared_manifest.launch_argv
            ),
            supervisor_module=copied_supervisor,
        )


def test_healthmes_rejects_an_unsealed_runtime_manifest(
    runtime_bundle: RuntimeBundle,
) -> None:
    write_runtime_manifest(
        runtime_bundle.manifest_path,
        runtime_bundle.prepared_manifest,
    )

    with pytest.raises(
        HermesRuntimeIdentityError,
        match="hermes_runtime_manifest_unsealed",
    ):
        validate_expected_runtime(
            manifest_path=runtime_bundle.manifest_path,
            attestation_key_path=runtime_bundle.key_path,
            profile_path=runtime_bundle.profile_path,
            profile_semantic_digest=runtime_bundle.profile_digest,
            expected_origin=PUBLIC_ORIGIN,
            expected_model=MODEL,
            expected_provider=PROVIDER,
            expected_api_key=API_KEY,
        )


@pytest.mark.asyncio
async def test_runtime_process_seals_manifest_before_child_launch(
    runtime_bundle: RuntimeBundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_runtime_manifest(
        runtime_bundle.manifest_path,
        runtime_bundle.prepared_manifest,
    )
    launched: list[tuple[str, ...]] = []

    class FakeProcess:
        returncode = None
        pid = 424242

    async def fake_create_subprocess_exec(
        *argv: str,
        **_kwargs: Any,
    ) -> FakeProcess:
        assert load_runtime_manifest(runtime_bundle.manifest_path).sealed
        launched.append(argv)
        return FakeProcess()

    async def fake_wait_until_ready(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    process = HermesRuntimeProcess(
        HermesRuntimeSupervisorConfig(
            hermes_home=runtime_bundle.home,
            manifest_path=runtime_bundle.manifest_path,
            attestation_key_path=runtime_bundle.key_path,
            vendor_root=runtime_bundle.vendor_root,
        ),
        environ=PROVIDER_ENV,
        mcp_inventory_probe=lambda _connection, _timeout: asyncio.sleep(
            0,
            result=HERMES_DECISION_MCP_INPUT_SCHEMA_SHA256,
        ),
    )
    monkeypatch.setattr(
        process,
        "_wait_until_ready",
        fake_wait_until_ready,
    )

    await process.start()

    assert launched == [runtime_bundle.prepared_manifest.launch_argv]
    assert process.state.manifest.sealed is True
    assert (
        process.state.manifest.runtime_id
        != runtime_bundle.prepared_manifest.runtime_id
    )


@pytest.mark.asyncio
async def test_runtime_process_executes_verified_launcher_snapshot(
    runtime_bundle: RuntimeBundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_runtime_manifest(
        runtime_bundle.manifest_path,
        runtime_bundle.prepared_manifest,
    )
    launcher = Path(runtime_bundle.prepared_manifest.launch_argv[0])
    sealed_bytes = launcher.read_bytes()
    replacement = launcher.with_name("replacement-python")
    replacement.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    replacement.chmod(0o755)
    observed_executable: list[str] = []

    class FakeProcess:
        returncode = None
        pid = 424242

    async def fake_create_subprocess_exec(
        *argv: str,
        **kwargs: Any,
    ) -> FakeProcess:
        replacement.replace(launcher)
        assert launcher.read_text(encoding="utf-8") == (
            "#!/bin/sh\nexit 99\n"
        )
        executable = str(kwargs["executable"])
        observed_executable.append(executable)
        assert executable != str(launcher)
        assert Path(executable).read_bytes() == sealed_bytes
        assert argv == runtime_bundle.prepared_manifest.launch_argv
        launcher.write_bytes(sealed_bytes)
        launcher.chmod(0o755)
        return FakeProcess()

    async def fake_wait_until_ready(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    process = HermesRuntimeProcess(
        HermesRuntimeSupervisorConfig(
            hermes_home=runtime_bundle.home,
            manifest_path=runtime_bundle.manifest_path,
            attestation_key_path=runtime_bundle.key_path,
            vendor_root=runtime_bundle.vendor_root,
        ),
        environ=PROVIDER_ENV,
        mcp_inventory_probe=lambda _connection, _timeout: asyncio.sleep(
            0,
            result=HERMES_DECISION_MCP_INPUT_SCHEMA_SHA256,
        ),
    )
    monkeypatch.setattr(
        process,
        "_wait_until_ready",
        fake_wait_until_ready,
    )

    await process.start()

    assert observed_executable
    assert launcher.read_bytes() == sealed_bytes
    assert process.state.manifest.sealed is True


@pytest.mark.asyncio
async def test_runtime_process_startup_fails_closed_on_live_inventory_drift(
    runtime_bundle: RuntimeBundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_runtime_manifest(
        runtime_bundle.manifest_path,
        runtime_bundle.prepared_manifest,
    )
    schema_digests = dict(HERMES_DECISION_MCP_INPUT_SCHEMA_SHA256)
    schema_digests.pop("search_wearable")

    class FakeProcess:
        returncode = None
        pid = 424242

    async def fake_create_subprocess_exec(
        *_argv: str,
        **_kwargs: Any,
    ) -> FakeProcess:
        return FakeProcess()

    async def fake_wait_until_ready(**_kwargs: Any) -> None:
        return None

    async def fake_aclose() -> None:
        process._process = None
        process._state = None
        process._launch_argv = None

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    process = HermesRuntimeProcess(
        HermesRuntimeSupervisorConfig(
            hermes_home=runtime_bundle.home,
            manifest_path=runtime_bundle.manifest_path,
            attestation_key_path=runtime_bundle.key_path,
            vendor_root=runtime_bundle.vendor_root,
        ),
        environ=PROVIDER_ENV,
        mcp_inventory_probe=lambda _connection, _timeout: asyncio.sleep(
            0,
            result=schema_digests,
        ),
    )
    monkeypatch.setattr(
        process,
        "_wait_until_ready",
        fake_wait_until_ready,
    )
    monkeypatch.setattr(process, "aclose", fake_aclose)

    with pytest.raises(
        HermesRuntimeIdentityError,
        match="hermes_runtime_mcp_inventory_mismatch",
    ):
        await process.start()

    assert process._process is None
    assert process._state is None


def test_child_environment_is_exact_and_scrubs_general_reasoning(
    runtime_bundle: RuntimeBundle,
) -> None:
    source = {
        **PROVIDER_ENV,
        "PATH": "/unsafe/path",
        "TELEGRAM_BOT_TOKEN": "telegram-secret",
        "HERMES_HOME": "/wrong/home",
    }

    child = build_child_environment(
        source,
        manifest=runtime_bundle.manifest,
    )

    assert child["OPENAI_API_KEY"] == PROVIDER_ENV["OPENAI_API_KEY"]
    assert child["HERMES_HOME"] == str(runtime_bundle.home)
    assert "TELEGRAM_BOT_TOKEN" not in child
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
        mcp_inventory=EXPECTED_MCP_INVENTORY,
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
        assert 0 < timeout_seconds <= 5
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


def test_attestation_rejects_signed_live_schema_drift(
    runtime_bundle: RuntimeBundle,
) -> None:
    schema_digests = dict(HERMES_DECISION_MCP_INPUT_SCHEMA_SHA256)
    schema_digests["search_activity"] = "0" * 64
    drifted_inventory = inventory_from_schema_digests(schema_digests)
    nonce = "n" * 32
    proof = sign_runtime_attestation(
        manifest=runtime_bundle.manifest,
        key=runtime_bundle.key,
        nonce=nonce,
        mcp_inventory=drifted_inventory,
    )

    from healthmes.hermes_runtime_identity import verify_runtime_attestation

    with pytest.raises(
        HermesRuntimeIdentityError,
        match="hermes_runtime_mcp_inventory_mismatch",
    ):
        verify_runtime_attestation(
            proof.model_dump(mode="json", by_alias=True),
            expected_manifest=runtime_bundle.manifest,
            key=runtime_bundle.key,
            nonce=nonce,
        )


@pytest.mark.asyncio
async def test_runtime_attestation_fails_closed_after_live_inventory_drift(
    runtime_bundle: RuntimeBundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = object.__new__(HermesRuntimeProcess)
    state = HermesRuntimeState(
        manifest=runtime_bundle.manifest,
        attestation_key=runtime_bundle.key,
        api_key=API_KEY,
        mcp_inventory=EXPECTED_MCP_INVENTORY,
    )
    process.revalidate = lambda: state

    async def drifted_inventory():
        schema_digests = dict(HERMES_DECISION_MCP_INPUT_SCHEMA_SHA256)
        schema_digests["search_activity"] = "0" * 64
        return inventory_from_schema_digests(schema_digests)

    monkeypatch.setattr(
        process,
        "_probe_runtime_inventory",
        drifted_inventory,
    )

    with pytest.raises(
        HermesRuntimeIdentityError,
        match="hermes_runtime_mcp_inventory_changed",
    ):
        await process.attest()


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
        self.attestations = 0

    @property
    def state(self) -> HermesRuntimeState:
        if self.failure is not None:
            raise self.failure
        return self._state

    def revalidate(self) -> HermesRuntimeState:
        self.revalidations += 1
        return self.state

    async def attest(self) -> HermesRuntimeState:
        self.attestations += 1
        return self.revalidate()

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
            mcp_inventory=EXPECTED_MCP_INVENTORY,
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
    assert controller.attestations == 0


def test_supervisor_revalidates_live_inventory_before_each_response(
    runtime_bundle: RuntimeBundle,
) -> None:
    class OneShotStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"event: response.completed\ndata: {}\n\n"

    controller = _FakeController(
        HermesRuntimeState(
            manifest=runtime_bundle.manifest,
            attestation_key=runtime_bundle.key,
            api_key=API_KEY,
            mcp_inventory=EXPECTED_MCP_INVENTORY,
        )
    )
    app = create_supervisor_app(
        controller,
        proxy_transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=OneShotStream(),
            )
        ),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"stream": True, "store": False},
        )

    assert response.status_code == 200
    assert controller.attestations == 1


def test_supervisor_authenticates_before_parsing_responses_body(
    runtime_bundle: RuntimeBundle,
) -> None:
    controller = _FakeController(
        HermesRuntimeState(
            manifest=runtime_bundle.manifest,
            attestation_key=runtime_bundle.key,
            api_key=API_KEY,
            mcp_inventory=EXPECTED_MCP_INVENTORY,
        )
    )

    with TestClient(create_supervisor_app(controller)) as client:
        response = client.post(
            "/v1/responses",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 401
    assert controller.attestations == 0


def test_supervisor_reports_stopped_runtime_as_unavailable(
    runtime_bundle: RuntimeBundle,
) -> None:
    controller = _FakeController(
        HermesRuntimeState(
            manifest=runtime_bundle.manifest,
            attestation_key=runtime_bundle.key,
            api_key=API_KEY,
            mcp_inventory=EXPECTED_MCP_INVENTORY,
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
