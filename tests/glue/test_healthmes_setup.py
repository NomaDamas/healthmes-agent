"""Machine-readable Mac setup wrapper contract."""

import importlib.util
import json
import os
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "healthmes_setup.py"


def load_module():
    spec = importlib.util.spec_from_file_location("healthmes_setup", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_setup_script_parses_and_exposes_all_actions() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for action in (
        "preflight",
        "install",
        "verify",
        "repair",
        "update",
        "diagnostics",
        "uninstall",
    ):
        assert f'"{action}"' in text
    compile(text, str(SCRIPT), "exec")


def test_setup_events_are_stable_json(capsys) -> None:
    module = load_module()
    module.emit(
        module.SetupEvent(
            action="install",
            step="preflight",
            state="running",
            message="Checking.",
        ),
        json_output=True,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "action": "install",
        "detail": None,
        "expires_at": None,
        "message": "Checking.",
        "schema": "healthmes.setup.v1",
        "state": "running",
        "step": "preflight",
    }


def test_install_dry_run_never_executes_runtime(monkeypatch, capsys) -> None:
    module = load_module()
    monkeypatch.setattr(
        module,
        "preflight",
        lambda: [
            module.SetupEvent(
                "preflight",
                "platform",
                "ready",
                "Supported.",
            ),
            module.SetupEvent(
                "preflight",
                "tool_brew",
                "ready",
                "brew ready.",
            ),
        ],
    )
    monkeypatch.setattr(module.shutil, "which", lambda command: f"/bin/{command}")
    called = []
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: called.append((args, kwargs)),
    )

    module.install(json_output=True, dry_run=True)

    assert called == []
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[-1]["step"] == "runtime_install"
    assert lines[-1]["state"] == "ready"


def test_prepare_runtime_stops_when_disk_preflight_requires_action(
    monkeypatch,
    capsys,
) -> None:
    module = load_module()
    monkeypatch.setattr(
        module,
        "preflight",
        lambda: [
            module.SetupEvent(
                "preflight",
                "disk",
                "action_required",
                "At least 5 GB of free space is required.",
                "1024",
            )
        ],
    )
    monkeypatch.setattr(
        module,
        "ensure_environment",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("environment setup must not start")
        ),
    )

    try:
        module.prepare_runtime("install", json_output=True, dry_run=False)
    except module.SetupFailure as exc:
        assert str(exc) == "At least 5 GB of free space is required."
    else:
        raise AssertionError("low disk space must block installation")

    event = json.loads(capsys.readouterr().out)
    assert event["step"] == "disk"
    assert event["state"] == "action_required"


def test_uninstall_contract_preserves_data_by_default() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    uninstall = text.split('elif args.action == "uninstall":', 1)[1]
    assert '["bash", str(runtime_script()), "uninstall"]' in uninstall
    assert "--delete-data" not in uninstall


def test_environment_generation_is_private_and_never_prints_token(
    monkeypatch, tmp_path, capsys
) -> None:
    module = load_module()
    env_file = tmp_path / ".env"
    example = tmp_path / ".env.example"
    example.write_text(
        "HEALTHMES_HOST=127.0.0.1\n"
        "HEALTHMES_PUBLIC_BASE_URL=http://localhost:8100\n"
        "HEALTHMES_PORT=8100\n"
        "HEALTHMES_API_TOKEN=\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "ENV_FILE", env_file)
    monkeypatch.setattr(module, "ENV_EXAMPLE", example)
    module.ensure_environment(json_output=True, dry_run=False)

    values = module._load_env(env_file)
    assert len(values["HEALTHMES_API_TOKEN"]) >= 32
    assert values["HEALTHMES_HOST"] == "127.0.0.1"
    assert values["HEALTHMES_PUBLIC_BASE_URL"] == "http://127.0.0.1:8100"
    assert values["HEALTHMES_SCHEDULER_ENABLED"] == "true"
    assert values["HEALTHMES_NATIVE_ALERT_DELIVERY"] == "true"
    assert os.stat(env_file).st_mode & 0o777 == 0o600
    assert values["HEALTHMES_API_TOKEN"] not in capsys.readouterr().out


def test_env_replace_failure_cleans_private_temporary(
    monkeypatch,
    tmp_path,
) -> None:
    module = load_module()
    env_file = tmp_path / ".env"
    env_file.write_text("EXISTING=value\n", encoding="utf-8")

    def failing_replace(source, destination):
        assert os.stat(source).st_mode & 0o777 == 0o600
        assert destination == env_file
        raise OSError("replace failed")

    monkeypatch.setattr(module.os, "replace", failing_replace)

    try:
        module._upsert_env(env_file, "SECRET", "private")
    except OSError as exc:
        assert str(exc) == "replace failed"
    else:
        raise AssertionError("replace failure was not propagated")

    assert env_file.read_text(encoding="utf-8") == "EXISTING=value\n"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_env_fsync_failure_cleans_private_temporary(
    monkeypatch,
    tmp_path,
) -> None:
    module = load_module()
    env_file = tmp_path / ".env"
    env_file.write_text("EXISTING=value\n", encoding="utf-8")

    def failing_fsync(fd):
        assert os.fstat(fd).st_mode & 0o777 == 0o600
        raise OSError("fsync failed")

    monkeypatch.setattr(module.os, "fsync", failing_fsync)

    try:
        module._upsert_env(env_file, "SECRET", "private")
    except OSError as exc:
        assert str(exc) == "fsync failed"
    else:
        raise AssertionError("fsync failure was not propagated")

    assert env_file.read_text(encoding="utf-8") == "EXISTING=value\n"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_environment_replaces_plain_http_bind_with_loopback(
    monkeypatch, tmp_path
) -> None:
    module = load_module()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "HEALTHMES_HOST=0.0.0.0\n"
        "HEALTHMES_PUBLIC_BASE_URL=http://192.168.1.10:8100\n"
        "HEALTHMES_PORT=8100\n"
        f"HEALTHMES_API_TOKEN={'x' * 32}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "ENV_FILE", env_file)

    module.ensure_environment(json_output=True, dry_run=False)

    values = module._load_env(env_file)
    assert values["HEALTHMES_HOST"] == "127.0.0.1"
    assert values["HEALTHMES_PUBLIC_BASE_URL"] == "http://127.0.0.1:8100"


def test_environment_preserves_explicit_https_public_instance(
    monkeypatch, tmp_path
) -> None:
    module = load_module()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "HEALTHMES_HOST=0.0.0.0\n"
        "HEALTHMES_PUBLIC_BASE_URL=https://healthmes.example.com\n"
        "HEALTHMES_PORT=8100\n"
        f"HEALTHMES_API_TOKEN={'x' * 32}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "ENV_FILE", env_file)

    module.ensure_environment(json_output=True, dry_run=False)

    values = module._load_env(env_file)
    assert values["HEALTHMES_HOST"] == "0.0.0.0"
    assert values["HEALTHMES_PUBLIC_BASE_URL"] == "https://healthmes.example.com"


def test_repair_and_update_prepare_environment_before_runtime(monkeypatch) -> None:
    module = load_module()
    calls: list[tuple[str, str, bool | None]] = []
    monkeypatch.setattr(
        module,
        "prepare_runtime",
        lambda action, **kwargs: calls.append(
            ("prepare", action, kwargs.get("bootstrap"))
        ),
    )
    monkeypatch.setattr(
        module,
        "run_command",
        lambda action, step, command, **kwargs: calls.append(
            (action, step, None)
        ),
    )
    monkeypatch.setattr(module, "verify", lambda **kwargs: True)

    assert module.main(["repair", "--json"]) == 0
    assert module.main(["update", "--json"]) == 0

    assert calls == [
        ("prepare", "repair", None),
        ("repair", "runtime_repair", None),
        ("prepare", "update", False),
        ("update", "runtime_update", None),
    ]


def test_linux_preflight_uses_docker_and_systemd_not_homebrew(monkeypatch) -> None:
    module = load_module()
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "platform", lambda: "Linux-test")
    class Usage:
        free = 10 * 1024**3

    monkeypatch.setattr(module.shutil, "disk_usage", lambda _: Usage())
    monkeypatch.setattr(module.shutil, "which", lambda command: f"/usr/bin/{command}")

    events = module.preflight()

    keys = {event.step for event in events}
    assert "tool_docker" in keys
    assert "tool_systemctl" in keys
    assert "tool_brew" not in keys
    assert all(event.state == "ready" for event in events)


def test_linux_prepare_uses_containerized_uv_bootstrap(monkeypatch, tmp_path) -> None:
    module = load_module()
    env_file = tmp_path / ".env"
    env_file.write_text("HEALTHMES_PORT=8199\n", encoding="utf-8")
    monkeypatch.setattr(module, "ENV_FILE", env_file)
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        module,
        "preflight",
        lambda: [module.SetupEvent("preflight", "platform", "ready", "Linux")],
    )
    monkeypatch.setattr(module, "ensure_environment", lambda **kwargs: None)
    commands = []
    monkeypatch.setattr(
        module,
        "run_command",
        lambda action, step, command, **kwargs: commands.append((step, command)),
    )

    module.prepare_runtime("install", json_output=True, dry_run=False)

    assert commands[0][0] == "bootstrap"
    assert commands[0][1][:3] == ["docker", "run", "--rm"]
    assert "--mode" in commands[0][1]
    assert "docker" in commands[0][1]
    assert (
        module._load_env(env_file)["HEALTHMES_MCP_URL"]
        == "http://host.docker.internal:8199/mcp"
    )


def test_linux_runtime_adapter_is_selected(monkeypatch) -> None:
    module = load_module()
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    assert module.runtime_script() == module.LINUX_SCRIPT


def test_verify_uses_port_from_env_file(monkeypatch, tmp_path) -> None:
    module = load_module()
    env_file = tmp_path / ".env"
    env_file.write_text("HEALTHMES_PORT=8199\n", encoding="utf-8")
    monkeypatch.setattr(module, "ENV_FILE", env_file)
    requested: list[str] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def fake_urlopen(url, timeout):
        requested.append(url)
        return Response()

    monkeypatch.delenv("HEALTHMES_PORT", raising=False)
    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)

    assert module.verify(json_output=True)
    assert requested == ["http://127.0.0.1:8199/health"]
