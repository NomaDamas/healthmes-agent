"""Contracts for the dedicated HealthMes Hermes decision bootstrap."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
import yaml

from healthmes import hermes_runtime_identity
from healthmes.decision import (
    HERMES_DECISION_MCP_TOOL_NAMES,
    HermesDecisionProfileAssertion,
)
from healthmes.hermes_runtime_identity import (
    HERMES_RUNTIME_CONTROL_SOURCE_NAMES,
    HERMES_RUNTIME_EXECUTION_ARTIFACT_NAMES,
    HERMES_RUNTIME_HOME_ARTIFACT_NAMES,
    HERMES_RUNTIME_PROVIDER_ENV_NAMES,
    HermesDecisionRuntimeManifest,
    HermesRuntimeExecutionArtifact,
    HermesRuntimeIdentityError,
    load_attestation_key,
    load_runtime_manifest,
    runtime_home_artifact_sha256,
    seal_supervised_runtime,
    validate_supervised_runtime,
    write_runtime_manifest,
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


def _configure_sealable_native_runtime(
    bootstrap,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    monkeypatch.setattr(bootstrap, "NATIVE_DECISION_VENV", runtime_venv)


def _seal_bootstrapped_runtime(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
) -> HermesDecisionRuntimeManifest:
    decision_home = _decision_home(hermes_home)
    env = {
        name: value
        for name, value in bootstrap.load_env_file(env_file).items()
        if name in HERMES_RUNTIME_PROVIDER_ENV_NAMES and value
    }
    return seal_supervised_runtime(
        manifest_path=decision_home / "runtime-manifest.json",
        attestation_key_path=decision_home / "runtime-attestation.key",
        hermes_home=decision_home,
        vendor_root=REPO_ROOT / "vendor" / "hermes-agent",
        environment=env,
    )[0]


def _fake_container_seal(
    manifest: HermesDecisionRuntimeManifest,
) -> HermesDecisionRuntimeManifest:
    artifacts = tuple(
        HermesRuntimeExecutionArtifact(
            name=name,
            path=f"/opt/runtime/{index}",
            resolved_path=f"/opt/runtime/{index}",
            sha256=f"{index + 1:064x}",
            mode=0o755 if "launcher" in name or "interpreter" in name else 0o644,
        )
        for index, name in enumerate(
            HERMES_RUNTIME_EXECUTION_ARTIFACT_NAMES
        )
    )
    payload = manifest.model_dump(
        mode="json",
        by_alias=True,
        exclude={"runtime_id"},
    )
    payload["sealed"] = True
    payload["execution_artifacts"] = [
        artifact.model_dump(mode="json") for artifact in artifacts
    ]
    payload["runtime_id"] = hermes_runtime_identity._sha256_json(payload)
    return hermes_runtime_identity._manifest_validate(payload)


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
    assert profile["compression"] == {"in_place": True}
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
    assert len(env["HEALTHMES_DECISION_CORRELATION_SECRET"]) == 64
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
    assert "preserve all 1 general cron job" in output
    assert "do not install new HealthMes reasoning" in output
    assert "cron registration method" not in output


def test_bootstrap_removes_only_owned_legacy_cron_reasoning_jobs(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
    capsys,
) -> None:
    jobs_file = hermes_home / "cron" / "jobs.json"
    jobs_file.parent.mkdir(parents=True)
    exact_legacy = {
        **bootstrap.LEGACY_HEALTHMES_CRON_REASONING_FINGERPRINTS[1],
        "id": "legacy-exact",
        "origin": None,
    }
    same_name_user_job = {
        **bootstrap.LEGACY_HEALTHMES_CRON_REASONING_FINGERPRINTS[0],
        "id": "same-name-user",
        "prompt": "User-owned replacement prompt.",
    }
    foreign_origin_job = {
        "id": "foreign-origin",
        "name": "healthmes-evening-review",
        "origin": {"source": "user-bootstrap", "version": 1},
        "prompt": "User-owned scheduled message.",
    }
    user_job = {
        "id": "user-job",
        "name": "my-reminder",
        "prompt": "Remember this.",
    }
    jobs_file.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "legacy-origin",
                        "name": "renamed-healthmes-job",
                        "origin": {
                            "source": bootstrap.HEALTHMES_CRON_ORIGIN_SOURCE,
                            "version": 1,
                        },
                    },
                    exact_legacy,
                    same_name_user_job,
                    foreign_origin_job,
                    user_job,
                ],
                "custom_metadata": {"preserve": True},
            }
        ),
        encoding="utf-8",
    )

    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0

    migrated = json.loads(jobs_file.read_text(encoding="utf-8"))
    assert migrated["custom_metadata"] == {"preserve": True}
    assert migrated["jobs"] == [
        same_name_user_job,
        foreign_origin_job,
        user_job,
    ]
    assert "updated_at" in migrated
    output = capsys.readouterr().out
    assert "legacy-origin" in output
    assert "legacy-exact" in output
    assert "preserve 3 unowned job(s)" in output


def test_cron_migration_dry_run_reports_without_writing(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
    capsys,
) -> None:
    jobs_file = hermes_home / "cron" / "jobs.json"
    jobs_file.parent.mkdir(parents=True)
    jobs_file.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "legacy-origin",
                        "name": "healthmes-weekly-plan",
                        "origin": {
                            "source": bootstrap.HEALTHMES_CRON_ORIGIN_SOURCE,
                            "version": 1,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    before = jobs_file.read_bytes()

    assert run_bootstrap(
        bootstrap,
        hermes_home,
        env_file,
        "--dry-run",
    ) == 0

    assert jobs_file.read_bytes() == before
    assert "would remove legacy HealthMes-owned" in capsys.readouterr().out


def test_malformed_cron_database_fails_before_bootstrap_writes(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
) -> None:
    jobs_file = hermes_home / "cron" / "jobs.json"
    jobs_file.parent.mkdir(parents=True)
    jobs_file.write_text('{"jobs": [', encoding="utf-8")
    before_env = env_file.read_bytes()

    with pytest.raises(ValueError, match="malformed"):
        run_bootstrap(bootstrap, hermes_home, env_file)

    assert jobs_file.read_text(encoding="utf-8") == '{"jobs": ['
    assert env_file.read_bytes() == before_env
    assert not _decision_home(hermes_home).exists()


def test_unsafe_cron_database_path_fails_before_bootstrap_writes(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
) -> None:
    jobs_file = hermes_home / "cron" / "jobs.json"
    jobs_file.parent.mkdir(parents=True)
    target = hermes_home / "user-owned-jobs.json"
    target.write_text('{"jobs":[{"id":"user-job"}]}\n', encoding="utf-8")
    jobs_file.symlink_to(target)
    before_env = env_file.read_bytes()

    with pytest.raises(ValueError, match="unsafe"):
        run_bootstrap(bootstrap, hermes_home, env_file)

    assert target.read_text(encoding="utf-8") == (
        '{"jobs":[{"id":"user-job"}]}\n'
    )
    assert env_file.read_bytes() == before_env
    assert not _decision_home(hermes_home).exists()


def test_cron_migration_refuses_a_concurrent_rewrite(
    bootstrap,
    hermes_home: Path,
) -> None:
    jobs_file = hermes_home / "cron" / "jobs.json"
    jobs_file.parent.mkdir(parents=True)
    jobs_file.write_text('{"jobs":[]}\n', encoding="utf-8")
    original = jobs_file.read_bytes()
    jobs_file.write_text('{"jobs":[{"id":"concurrent"}]}\n', encoding="utf-8")

    with pytest.raises(RuntimeError, match="changed during migration"):
        bootstrap._write_cron_document_if_unchanged(
            jobs_file,
            original=original,
            document={"jobs": []},
        )

    assert json.loads(jobs_file.read_text(encoding="utf-8")) == {
        "jobs": [{"id": "concurrent"}]
    }


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


def test_second_run_preserves_unchanged_sealed_runtime_manifest(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_sealable_native_runtime(bootstrap, tmp_path, monkeypatch)
    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0
    sealed = _seal_bootstrapped_runtime(
        bootstrap,
        hermes_home,
        env_file,
    )
    manifest_path = _decision_home(hermes_home) / "runtime-manifest.json"
    before = manifest_path.read_bytes()

    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0

    assert manifest_path.read_bytes() == before
    assert load_runtime_manifest(manifest_path) == sealed
    assert load_runtime_manifest(manifest_path).sealed is True


@pytest.mark.parametrize(
    "artifact_change",
    (
        "launcher-bytes",
        "launcher-mode",
        "launcher-resolved-path",
        "pyvenv",
        "supervisor-interpreter",
    ),
)
def test_changed_execution_artifact_publishes_unsealed_intent(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact_change: str,
) -> None:
    _configure_sealable_native_runtime(bootstrap, tmp_path, monkeypatch)
    launcher = bootstrap.NATIVE_DECISION_VENV / "bin" / "python"
    supervisor_interpreter = tmp_path / "supervisor-python"
    supervisor_interpreter.write_text(
        "#!/bin/sh\nexit 0\n",
        encoding="utf-8",
    )
    supervisor_interpreter.chmod(0o755)
    monkeypatch.setattr(
        hermes_runtime_identity.sys,
        "executable",
        str(supervisor_interpreter),
    )

    alternate_launcher = tmp_path / "alternate-python"
    if artifact_change == "launcher-resolved-path":
        original_launcher = tmp_path / "original-python"
        for path in (original_launcher, alternate_launcher):
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o755)
        launcher.unlink()
        launcher.symlink_to(original_launcher)

    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0
    sealed = _seal_bootstrapped_runtime(
        bootstrap,
        hermes_home,
        env_file,
    )

    if artifact_change == "launcher-bytes":
        launcher.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    elif artifact_change == "launcher-mode":
        launcher.chmod(0o700)
    elif artifact_change == "launcher-resolved-path":
        launcher.unlink()
        launcher.symlink_to(alternate_launcher)
    elif artifact_change == "pyvenv":
        (
            bootstrap.NATIVE_DECISION_VENV / "pyvenv.cfg"
        ).write_text(
            "home = /changed/python\n",
            encoding="utf-8",
        )
    else:
        supervisor_interpreter.write_text(
            "#!/bin/sh\nexit 1\n",
            encoding="utf-8",
        )

    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0

    manifest = load_runtime_manifest(
        _decision_home(hermes_home) / "runtime-manifest.json"
    )
    assert manifest.sealed is False
    assert manifest.execution_artifacts == ()
    assert manifest.runtime_id != sealed.runtime_id


@pytest.mark.parametrize(
    "missing_artifact",
    ("launcher", "pyvenv", "supervisor-interpreter"),
)
def test_unverifiable_execution_artifact_publishes_unsealed_intent(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_artifact: str,
) -> None:
    _configure_sealable_native_runtime(bootstrap, tmp_path, monkeypatch)
    supervisor_interpreter = tmp_path / "supervisor-python"
    supervisor_interpreter.write_text(
        "#!/bin/sh\nexit 0\n",
        encoding="utf-8",
    )
    supervisor_interpreter.chmod(0o755)
    monkeypatch.setattr(
        hermes_runtime_identity.sys,
        "executable",
        str(supervisor_interpreter),
    )
    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0
    _seal_bootstrapped_runtime(
        bootstrap,
        hermes_home,
        env_file,
    )
    if missing_artifact == "launcher":
        missing_path = bootstrap.NATIVE_DECISION_VENV / "bin" / "python"
    elif missing_artifact == "pyvenv":
        missing_path = bootstrap.NATIVE_DECISION_VENV / "pyvenv.cfg"
    else:
        missing_path = supervisor_interpreter
    missing_path.unlink()

    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0

    manifest = load_runtime_manifest(
        _decision_home(hermes_home) / "runtime-manifest.json"
    )
    assert manifest.sealed is False
    assert manifest.execution_artifacts == ()


def test_changed_bootstrap_inputs_publish_unsealed_intent_for_supervisor(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_sealable_native_runtime(bootstrap, tmp_path, monkeypatch)
    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0
    sealed = _seal_bootstrapped_runtime(
        bootstrap,
        hermes_home,
        env_file,
    )
    env_file.write_text(
        env_file.read_text(encoding="utf-8").replace(
            "HEALTHMES_DECISION_HERMES_MODEL=decision-model",
            "HEALTHMES_DECISION_HERMES_MODEL=decision-model-v2",
        ),
        encoding="utf-8",
    )

    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0

    manifest_path = _decision_home(hermes_home) / "runtime-manifest.json"
    changed = load_runtime_manifest(manifest_path)
    assert changed.sealed is False
    assert changed.execution_artifacts == ()
    assert changed.runtime_id != sealed.runtime_id
    resealed = _seal_bootstrapped_runtime(
        bootstrap,
        hermes_home,
        env_file,
    )
    assert resealed.sealed is True
    assert resealed.runtime_id != changed.runtime_id


def test_runtime_identity_binds_mcp_inventory_policy_source(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
    tmp_path: Path,
) -> None:
    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0
    decision_home = _decision_home(hermes_home)
    manifest = load_runtime_manifest(
        decision_home / "runtime-manifest.json"
    )
    assert tuple(
        artifact.name for artifact in manifest.control_source_artifacts
    ) == HERMES_RUNTIME_CONTROL_SOURCE_NAMES

    from healthmes import hermes_mcp_inventory

    changed_policy = tmp_path / "hermes_mcp_inventory.py"
    changed_policy.write_bytes(
        Path(hermes_mcp_inventory.__file__).read_bytes()
        + b"\n# changed policy\n"
    )
    provider_environment = {
        name: value
        for name, value in bootstrap.load_env_file(env_file).items()
        if name in HERMES_RUNTIME_PROVIDER_ENV_NAMES and value
    }

    with pytest.raises(
        HermesRuntimeIdentityError,
        match="hermes_runtime_control_source_mismatch",
    ):
        validate_supervised_runtime(
            manifest_path=decision_home / "runtime-manifest.json",
            attestation_key_path=decision_home
            / "runtime-attestation.key",
            hermes_home=decision_home,
            vendor_root=REPO_ROOT / "vendor" / "hermes-agent",
            environment=provider_environment,
            require_sealed=False,
            mcp_inventory_module=changed_policy,
        )


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


def test_existing_correlation_secret_is_preserved(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
) -> None:
    env_file.write_text(
        env_file.read_text(encoding="utf-8")
        + "HEALTHMES_DECISION_CORRELATION_SECRET="
        + "c" * 64
        + "\n",
        encoding="utf-8",
    )

    assert run_bootstrap(bootstrap, hermes_home, env_file) == 0

    env = bootstrap.load_env_file(env_file)
    assert env["HEALTHMES_DECISION_CORRELATION_SECRET"] == "c" * 64


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


def test_short_existing_correlation_secret_fails_closed(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
) -> None:
    jobs_file = hermes_home / "cron" / "jobs.json"
    jobs_file.parent.mkdir(parents=True)
    jobs_file.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "legacy-origin",
                        "name": "healthmes-weekly-plan",
                        "origin": {
                            "source": (
                                bootstrap.HEALTHMES_CRON_ORIGIN_SOURCE
                            ),
                            "version": 1,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    env_file.write_text(
        env_file.read_text(encoding="utf-8")
        + "HEALTHMES_DECISION_CORRELATION_SECRET=short\n",
        encoding="utf-8",
    )
    before_env = env_file.read_bytes()
    before_jobs = jobs_file.read_bytes()

    with pytest.raises(ValueError, match="at least 32 characters"):
        run_bootstrap(bootstrap, hermes_home, env_file)

    assert env_file.read_bytes() == before_env
    assert jobs_file.read_bytes() == before_jobs
    assert not _decision_home(hermes_home).exists()


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


def test_docker_rerun_preserves_equivalent_container_seal(
    bootstrap,
    hermes_home: Path,
    env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert run_bootstrap(
        bootstrap,
        hermes_home,
        env_file,
        "--mode",
        "docker",
    ) == 0
    manifest_path = _decision_home(hermes_home) / "runtime-manifest.json"
    sealed = _fake_container_seal(load_runtime_manifest(manifest_path))
    write_runtime_manifest(manifest_path, sealed)
    before = manifest_path.read_bytes()

    def reject_host_execution_artifact_probe(*_args, **_kwargs):
        raise AssertionError(
            "host bootstrap must not inspect container execution paths"
        )

    monkeypatch.setattr(
        hermes_runtime_identity,
        "runtime_execution_artifacts",
        reject_host_execution_artifact_probe,
    )

    assert run_bootstrap(
        bootstrap,
        hermes_home,
        env_file,
        "--mode",
        "docker",
    ) == 0

    assert manifest_path.read_bytes() == before
    assert load_runtime_manifest(manifest_path) == sealed


def test_docker_runtime_seal_can_be_explicitly_refreshed(
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
    manifest_path = _decision_home(hermes_home) / "runtime-manifest.json"
    sealed = _fake_container_seal(load_runtime_manifest(manifest_path))
    write_runtime_manifest(manifest_path, sealed)

    assert run_bootstrap(
        bootstrap,
        hermes_home,
        env_file,
        "--mode",
        "docker",
        "--refresh-runtime-seal",
    ) == 0

    refreshed = load_runtime_manifest(manifest_path)
    assert refreshed.sealed is False
    assert refreshed.execution_artifacts == ()
    assert refreshed.runtime_id != sealed.runtime_id


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
