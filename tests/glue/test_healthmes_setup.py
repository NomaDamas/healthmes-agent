"""Contract tests for the macOS machine-readable setup adapter."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from healthmes.pairing import PairingGrantStore

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "healthmes_setup.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "healthmes_setup_test_module",
        SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _temporary_repo(tmp_path: Path) -> Path:
    root = tmp_path / "managed-runtime"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "[project]\nname='healthmes-test'\nversion='0'\n",
        encoding="utf-8",
    )
    lifecycle = scripts / "healthmes_local.sh"
    lifecycle.write_text(
        "#!/usr/bin/env bash\nprintf 'fake lifecycle: %s\\n' \"$1\"\n",
        encoding="utf-8",
    )
    lifecycle.chmod(0o755)
    return root


def test_runtime_config_generates_private_token_without_printing_it(
    tmp_path,
) -> None:
    module = _load_module()
    root = _temporary_repo(tmp_path)
    events = []

    class Reporter:
        def emit(self, *args, **kwargs):
            events.append((args, kwargs))

    config = module._ensure_runtime_config(root, Reporter())

    env_text = (root / ".env").read_text(encoding="utf-8")
    assert f"HEALTHMES_API_TOKEN={config.api_token}" in env_text
    assert len(config.api_token) >= 32
    assert config.api_token not in repr(events)
    if os.name != "nt":
        assert (root / ".env").stat().st_mode & 0o077 == 0


def test_pair_action_emits_code_only_and_server_store_can_consume_it(
    tmp_path,
) -> None:
    root = _temporary_repo(tmp_path)
    token = "pair-script-test-token-32-characters"
    (root / ".env").write_text(
        "\n".join(
            [
                f"HEALTHMES_API_TOKEN={token}",
                "HEALTHMES_PUBLIC_BASE_URL=https://healthmes.example.ts.net",
                "HEALTHMES_DATA_DIR=data",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["HEALTHMES_REPO_ROOT"] = str(root)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "pair", "--json"],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert token not in result.stdout
    events = [json.loads(line) for line in result.stdout.splitlines()]
    pair = next(event for event in events if event["step"] == "pair_phone")
    assert pair["schema"] == "healthmes.setup.v1"
    assert pair["state"] == "ready"
    assert "token=" not in pair["detail"]
    code = parse_qs(urlsplit(pair["detail"]).query)["code"][0]
    assert PairingGrantStore(root / "data").consume(code) == (
        "https://healthmes.example.ts.net"
    )


def test_pair_action_reports_tailscale_requirement_without_failing_mac(
    tmp_path,
) -> None:
    root = _temporary_repo(tmp_path)
    (root / ".env").write_text(
        "\n".join(
            [
                "HEALTHMES_API_TOKEN=pair-script-test-token-32-characters",
                "HEALTHMES_PUBLIC_BASE_URL=http://127.0.0.1:8100",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "HEALTHMES_REPO_ROOT": str(root),
        "HEALTHMES_TAILSCALE_BINARY": "",
        "PATH": "/usr/bin:/bin",
    }
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "pair", "--json"],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )

    assert result.returncode == 0
    events = [json.loads(line) for line in result.stdout.splitlines()]
    pair = next(event for event in events if event["step"] == "pair_phone")
    assert pair["state"] == "action_required"
    assert pair["detail"]


def test_tailscale_serve_timeout_is_reported_as_action_required(
    tmp_path,
    monkeypatch,
) -> None:
    module = _load_module()
    root = _temporary_repo(tmp_path)
    config = module.RuntimeConfig(
        root=root,
        env_file=root / ".env",
        data_dir=root / "data",
        port=8100,
        api_token="pair-script-test-token-32-characters",
        public_base_url="http://127.0.0.1:8100",
    )
    events = []

    class Reporter:
        def emit(self, *args, **kwargs):
            events.append((args, kwargs))

    monkeypatch.setattr(module, "_tailscale_binary", lambda: "/fake/tailscale")
    monkeypatch.setattr(
        module,
        "_tailscale_dns_name",
        lambda _binary: "healthmes.example.ts.net",
    )

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(module.subprocess, "run", timeout)

    origin, updated = module._prepare_phone_origin(config, Reporter())

    assert origin is None
    assert updated == config
    assert events[-1][0][:2] == ("tailscale", "action_required")


def test_diagnostics_maps_to_existing_status_lifecycle(tmp_path) -> None:
    root = _temporary_repo(tmp_path)
    (root / ".env").write_text(
        "HEALTHMES_API_TOKEN=diagnostics-test-token-32-characters\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "diagnostics", "--json"],
        text=True,
        capture_output=True,
        env={**os.environ, "HEALTHMES_REPO_ROOT": str(root)},
        check=False,
    )

    assert result.returncode == 0
    events = [json.loads(line) for line in result.stdout.splitlines()]
    runtime = [
        event
        for event in events
        if event["step"] == "runtime" and event["state"] == "ready"
    ]
    assert runtime
    assert runtime[-1]["detail"] == "fake lifecycle: status"


def test_script_supports_every_mac_coordinator_action() -> None:
    module = _load_module()

    assert module.SUPPORTED_ACTIONS == (
        "install",
        "pair",
        "repair",
        "update",
        "diagnostics",
        "uninstall",
    )
