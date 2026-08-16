"""Contracts for the dedicated HealthMes Hermes decision bootstrap."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest
import yaml

from healthmes.decision import (
    HERMES_DECISION_MCP_TOOL_NAMES,
    HermesDecisionProfileAssertion,
)
from healthmes.hermes_runtime_identity import (
    HERMES_RUNTIME_HOME_ARTIFACT_NAMES,
    HERMES_RUNTIME_PROVIDER_ENV_NAMES,
    load_attestation_key,
    load_runtime_manifest,
    runtime_home_artifact_sha256,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.usefixtures("clean_env")


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(
        "HEALTHMES_DECISION_HERMES_MODEL=decision-model\n"
        "HEALTHMES_DECISION_HERMES_PROVIDER=openai\n"
        "OPENAI_API_KEY=provider-secret\n"
        # These legacy values must not influence the dedicated runtime.
        "TELEGRAM_BOT_TOKEN=legacy-telegram-secret\n"
        "HEALTHMES_TELEGRAM_OWNER_USER_ID=*\n"
        "HEALTHMES_TELEGRAM_OWNER_CHAT_ID=*\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def hermes_home(tmp_path: Path) -> Path:
    return tmp_path / "hermes-home"


def run_bootstrap(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
    *extra: str,
) -> int:
    return bootstrap.main(
        [
            "--hermes-home",
            str(hermes_home),
            "--env-file",
            str(env_file),
            *extra,
        ]
    )


def _decision_home(hermes_home: Path) -> Path:
    return hermes_home / "decision"


def _load_profile(hermes_home: Path) -> dict:
    return yaml.safe_load(
        (_decision_home(hermes_home) / "config.yaml").read_text(
            encoding="utf-8"
        )
    )


def test_full_run_builds_only_attested_decision_runtime(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
    capsys,
) -> None:
    hermes_home.mkdir()
    general_config = hermes_home / "config.yaml"
    general_config.write_text(
        "platforms:\n  telegram:\n    enabled: true\n",
        encoding="utf-8",
    )
    general_cron = hermes_home / "cron" / "jobs.json"
    general_cron.parent.mkdir()
    general_cron.write_text('{"jobs":[{"name":"user-job"}]}\n')

    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0

    # The legacy general Hermes home is not modified or populated.
    assert general_config.read_text(encoding="utf-8") == (
        "platforms:\n  telegram:\n    enabled: true\n"
    )
    assert general_cron.read_text() == '{"jobs":[{"name":"user-job"}]}\n'
    assert not (hermes_home / "skills").exists()
    assert not (hermes_home / "scripts").exists()

    decision_home = _decision_home(hermes_home)
    assert stat.S_IMODE(decision_home.stat().st_mode) == 0o700
    profile_path = decision_home / "config.yaml"
    profile = _load_profile(hermes_home)
    assert set(profile["platforms"]) == {"api_server"}
    assert profile["platform_toolsets"] == {"api_server": ["healthmes"]}
    assert set(profile["mcp_servers"]) == {"healthmes"}
    assert (
        profile["mcp_servers"]["healthmes"]["tools"]["resources"] is False
    )
    assert (
        profile["mcp_servers"]["healthmes"]["tools"]["prompts"] is False
    )
    assert profile["mcp_servers"]["healthmes"]["tools"]["include"] == list(
        HERMES_DECISION_MCP_TOOL_NAMES
    )
    assert profile["mcp_servers"]["healthmes"]["url"] == (
        "http://localhost:8100/mcp"
    )
    assert "telegram" not in profile["platforms"]
    assert "webhook" not in profile["platforms"]

    env = bootstrap.load_env_file(env_file)
    assert len(env["HEALTHMES_DECISION_HERMES_API_KEY"]) == 64
    assert env["HEALTHMES_DECISION_HERMES_BASE_URL"] == (
        "http://127.0.0.1:8645"
    )
    assert env["HEALTHMES_DECISION_HERMES_PROFILE_PATH"] == str(
        profile_path.resolve()
    )
    manifest_path = decision_home / "runtime-manifest.json"
    key_path = decision_home / "runtime-attestation.key"
    assert env["HEALTHMES_DECISION_HERMES_RUNTIME_MANIFEST_PATH"] == str(
        manifest_path.resolve()
    )
    assert env["HEALTHMES_DECISION_HERMES_ATTESTATION_KEY_PATH"] == str(
        key_path.resolve()
    )

    profile_digest = HermesDecisionProfileAssertion(
        profile_path,
        expected_model="decision-model",
        expected_provider="openai",
        expected_api_key=env["HEALTHMES_DECISION_HERMES_API_KEY"],
    ).verify()
    manifest = load_runtime_manifest(manifest_path)
    key = load_attestation_key(key_path)
    assert manifest.sealed is False
    assert manifest.execution_artifacts == ()
    assert manifest.profile_semantic_digest == profile_digest
    assert manifest.hermes_home == str(decision_home.resolve())
    assert manifest.public_origin == "http://127.0.0.1:8645"
    assert manifest.internal_origin == "http://127.0.0.1:8646"
    assert manifest.vendor_root == str(
        (REPO_ROOT / "vendor" / "hermes-agent").resolve()
    )
    assert manifest.model_alias == "healthmes-decision-runtime"
    assert manifest.model == "decision-model"
    assert manifest.provider == "openai"
    assert manifest.attestation_key_sha256 == hashlib.sha256(key).hexdigest()
    assert {
        item.name: item.sha256 for item in manifest.home_artifacts
    } == runtime_home_artifact_sha256(decision_home)
    assert {
        item.name: item.sha256 for item in manifest.provider_environment
    } == {
        "OPENAI_API_KEY": hashlib.sha256(
            b"provider-secret"
        ).hexdigest()
    }
    assert manifest.launch_argv[-4:] == (
        "-m",
        "hermes_cli.main",
        "gateway",
        "run",
    )

    for name in (
        *HERMES_RUNTIME_HOME_ARTIFACT_NAMES,
        "runtime-manifest.json",
        "runtime-attestation.key",
    ):
        mode = stat.S_IMODE((decision_home / name).stat().st_mode)
        assert mode == 0o600
    for forbidden in (
        "cron",
        "hooks",
        "memories",
        "mcp-tokens",
        "plugins",
        "scripts",
        "skills",
    ):
        assert not (decision_home / forbidden).exists()

    output = capsys.readouterr().out
    assert "content-bound runtime manifest" in output
    assert "do not install HealthMes Telegram, webhook" in output
    assert "cron registration method" not in output


def test_second_run_is_byte_idempotent(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
) -> None:
    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0
    decision_home = _decision_home(hermes_home)
    tracked = (
        "config.yaml",
        "SOUL.md",
        ".env",
        ".no-bundled-skills",
        "runtime-manifest.json",
        "runtime-attestation.key",
    )
    before = {
        name: (decision_home / name).read_bytes() for name in tracked
    }
    env_before = env_file.read_bytes()

    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0

    assert {
        name: (decision_home / name).read_bytes() for name in tracked
    } == before
    assert env_file.read_bytes() == env_before


def test_dry_run_is_inert_and_reports_dedicated_artifacts(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
    capsys,
) -> None:
    before = env_file.read_bytes()

    assert run_bootstrap(
        bootstrap,
        hermes_home,
        env_file,
        "--dry-run",
    ) == 0

    assert not hermes_home.exists()
    assert env_file.read_bytes() == before
    output = capsys.readouterr().out
    assert "decision/config.yaml" in output
    assert "runtime-manifest.json" in output
    assert "runtime-attestation.key" in output
    assert "healthmes-morning-plan" not in output
    assert "healthmes-planner" not in output


@pytest.mark.parametrize(
    "content",
    (
        "",
        "HEALTHMES_DECISION_HERMES_MODEL=decision-model\n",
        "HEALTHMES_DECISION_HERMES_PROVIDER=openai\n",
    ),
)
def test_model_and_provider_are_required_before_any_write(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
    content: str,
) -> None:
    env_file.write_text(content, encoding="utf-8")
    before = env_file.read_bytes()

    with pytest.raises(ValueError, match="MODEL and .*PROVIDER are required"):
        run_bootstrap(bootstrap, hermes_home, env_file)

    assert env_file.read_bytes() == before
    assert not hermes_home.exists()


def test_existing_api_key_is_preserved(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
) -> None:
    env_file.write_text(
        env_file.read_text(encoding="utf-8")
        + "HEALTHMES_DECISION_HERMES_API_KEY="
        + "a" * 64
        + "\n",
        encoding="utf-8",
    )

    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0

    env = bootstrap.load_env_file(env_file)
    assert env["HEALTHMES_DECISION_HERMES_API_KEY"] == "a" * 64
    profile = _load_profile(hermes_home)
    assert profile["platforms"]["api_server"]["extra"]["key"] == "a" * 64


def test_short_existing_api_key_fails_closed(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
) -> None:
    env_file.write_text(
        env_file.read_text(encoding="utf-8")
        + "HEALTHMES_DECISION_HERMES_API_KEY=short\n",
        encoding="utf-8",
    )
    before = env_file.read_bytes()

    with pytest.raises(ValueError, match="at least 32 characters"):
        run_bootstrap(bootstrap, hermes_home, env_file)

    assert env_file.read_bytes() == before
    assert not hermes_home.exists()


@pytest.mark.parametrize(
    "key",
    (
        "HEALTHMES_DECISION_HERMES_PROFILE_PATH",
        "HEALTHMES_DECISION_HERMES_RUNTIME_MANIFEST_PATH",
        "HEALTHMES_DECISION_HERMES_ATTESTATION_KEY_PATH",
    ),
)
def test_runtime_artifact_path_override_cannot_detach_identity(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
    key: str,
    tmp_path: Path,
) -> None:
    env_file.write_text(
        env_file.read_text(encoding="utf-8")
        + f"{key}={tmp_path / 'other-runtime-artifact'}\n",
        encoding="utf-8",
    )
    before = env_file.read_bytes()

    with pytest.raises(ValueError, match=f"{key} must point"):
        run_bootstrap(bootstrap, hermes_home, env_file)

    assert env_file.read_bytes() == before
    assert not hermes_home.exists()


@pytest.mark.parametrize(
    ("kind", "name"),
    (
        ("file", "auth.json"),
        ("file", ".anthropic_oauth.json"),
        ("dir", ".codex"),
        ("dir", "cron"),
        ("dir", "mcp-tokens"),
        ("dir", "skills"),
    ),
)
def test_broad_runtime_state_is_rejected_before_bootstrap_mutates_env(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
    kind: str,
    name: str,
) -> None:
    path = _decision_home(hermes_home) / name
    if kind == "file":
        path.parent.mkdir(parents=True)
        path.write_text("legacy credential or state\n", encoding="utf-8")
    else:
        path.mkdir(parents=True)
        (path / "state").write_text("legacy state\n", encoding="utf-8")
    before = env_file.read_bytes()

    with pytest.raises(
        ValueError,
        match="contains broad reasoning artifacts",
    ):
        run_bootstrap(bootstrap, hermes_home, env_file)

    assert env_file.read_bytes() == before
    assert not (_decision_home(hermes_home) / "config.yaml").exists()


def test_symlinked_dedicated_home_is_rejected_before_bootstrap_mutates_env(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
    tmp_path: Path,
) -> None:
    target = tmp_path / "other-home"
    target.mkdir()
    hermes_home.mkdir()
    _decision_home(hermes_home).symlink_to(target, target_is_directory=True)
    before = env_file.read_bytes()

    with pytest.raises(ValueError, match="runtime home is unsafe"):
        run_bootstrap(bootstrap, hermes_home, env_file)

    assert env_file.read_bytes() == before
    assert list(target.iterdir()) == []


def test_existing_dedicated_home_permissions_are_tightened(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
) -> None:
    decision_home = _decision_home(hermes_home)
    decision_home.mkdir(parents=True, mode=0o755)
    decision_home.chmod(0o755)

    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0

    assert stat.S_IMODE(decision_home.stat().st_mode) == 0o700


def test_drifted_profile_is_replaced_exactly_and_archived(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
) -> None:
    decision_home = _decision_home(hermes_home)
    decision_home.mkdir(parents=True)
    profile_path = decision_home / "config.yaml"
    profile_path.write_text(
        "platforms:\n  telegram:\n    enabled: true\n",
        encoding="utf-8",
    )

    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0

    profile = _load_profile(hermes_home)
    assert set(profile["platforms"]) == {"api_server"}
    backup = (
        hermes_home
        / "decision-runtime-backups"
        / "config.yaml.pre-healthmes-runtime"
    )
    assert "telegram" in backup.read_text(encoding="utf-8")
    assert not (decision_home / "decision-runtime-backups").exists()


def test_docker_mode_binds_container_runtime_identity(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
) -> None:
    assert run_bootstrap(
        bootstrap,
        hermes_home,
        env_file,
        "--mode",
        "docker",
    ) == 0

    profile = _load_profile(hermes_home)
    assert profile["mcp_servers"]["healthmes"]["url"] == (
        "http://healthmes:8100/mcp"
    )
    assert profile["platforms"]["api_server"]["extra"]["port"] == 8646
    manifest = load_runtime_manifest(
        _decision_home(hermes_home) / "runtime-manifest.json"
    )
    assert manifest.hermes_home == "/opt/data"
    assert manifest.vendor_root == "/opt/hermes"
    assert manifest.public_origin == "http://hermes-decision:8645"
    assert manifest.internal_origin == "http://127.0.0.1:8646"
    assert manifest.launch_argv == (
        "/opt/hermes/.venv/bin/python",
        "-m",
        "hermes_cli.main",
        "gateway",
        "run",
    )
    assert bootstrap.load_env_file(env_file)[
        "HEALTHMES_DECISION_HERMES_BASE_URL"
    ] == "http://hermes-decision:8645"
    assert bootstrap.load_env_file(env_file)["HERMES_UID"] == str(
        os.getuid()
    )
    assert bootstrap.load_env_file(env_file)["HERMES_GID"] == str(
        os.getgid()
    )


@pytest.mark.parametrize(
    ("first_mode", "second_mode", "expected_origin"),
    (
        ("native", "docker", "http://hermes-decision:8645"),
        ("docker", "native", "http://127.0.0.1:8645"),
    ),
)
def test_mode_switch_replaces_only_bootstrap_owned_public_origin(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
    first_mode: str,
    second_mode: str,
    expected_origin: str,
) -> None:
    assert run_bootstrap(
        bootstrap,
        hermes_home,
        env_file,
        "--mode",
        first_mode,
    ) == 0
    assert run_bootstrap(
        bootstrap,
        hermes_home,
        env_file,
        "--mode",
        second_mode,
    ) == 0

    assert bootstrap.load_env_file(env_file)[
        "HEALTHMES_DECISION_HERMES_BASE_URL"
    ] == expected_origin
    manifest = load_runtime_manifest(
        _decision_home(hermes_home) / "runtime-manifest.json"
    )
    assert manifest.public_origin == expected_origin


def test_mode_switch_preserves_custom_public_origin(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
) -> None:
    custom_origin = "https://decision.example.test"
    env_file.write_text(
        env_file.read_text(encoding="utf-8")
        + "HEALTHMES_DECISION_HERMES_BASE_URL="
        + custom_origin
        + "\n",
        encoding="utf-8",
    )

    assert run_bootstrap(
        bootstrap,
        hermes_home,
        env_file,
        "--mode",
        "docker",
    ) == 0

    assert bootstrap.load_env_file(env_file)[
        "HEALTHMES_DECISION_HERMES_BASE_URL"
    ] == custom_origin
    manifest = load_runtime_manifest(
        _decision_home(hermes_home) / "runtime-manifest.json"
    )
    assert manifest.public_origin == custom_origin


def test_decision_profile_honors_only_decision_and_healthmes_overrides(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "HEALTHMES_MCP_URL",
        "https://healthmes.example.test/mcp",
    )
    monkeypatch.setenv(
        "HEALTHMES_DECISION_HERMES_MODEL_BASE_URL",
        "https://models.example.test/v1",
    )
    monkeypatch.setenv(
        "HEALTHMES_DECISION_HERMES_MODEL_API_KEY",
        "model-route-secret",
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "must-not-be-read")

    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0

    profile = _load_profile(hermes_home)
    assert profile["mcp_servers"]["healthmes"]["url"] == (
        "https://healthmes.example.test/mcp"
    )
    assert profile["model"]["base_url"] == (
        "https://models.example.test/v1"
    )
    assert profile["model"]["api_key"] == "model-route-secret"
    encoded = (
        _decision_home(hermes_home) / "runtime-manifest.json"
    ).read_text(encoding="ascii")
    assert "provider-secret" not in encoded
    assert "legacy-telegram-secret" not in encoded
    assert "must-not-be-read" not in encoded


def test_provider_environment_manifest_is_an_exact_allowlist(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
) -> None:
    env_file.write_text(
        env_file.read_text(encoding="utf-8")
        + "UNRELATED_API_KEY=unrelated-secret\n"
        + "ANTHROPIC_API_KEY=second-provider-secret\n",
        encoding="utf-8",
    )

    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0

    manifest = load_runtime_manifest(
        _decision_home(hermes_home) / "runtime-manifest.json"
    )
    names = {item.name for item in manifest.provider_environment}
    assert names == {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"}
    assert names.issubset(HERMES_RUNTIME_PROVIDER_ENV_NAMES)
    manifest_text = (
        _decision_home(hermes_home) / "runtime-manifest.json"
    ).read_text(encoding="ascii")
    assert "second-provider-secret" not in manifest_text
    assert "unrelated-secret" not in manifest_text
