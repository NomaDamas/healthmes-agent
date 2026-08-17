"""Canonical deployment contracts for the optional decision runtime."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
DECISION_BIND_SOURCE = REPO_ROOT / "data" / "hermes" / "decision"
README_FILE = REPO_ROOT / "README.md"
DEVELOPMENT_DOC = REPO_ROOT / "docs" / "DEVELOPMENT.md"


def _environment(entries: list[str]) -> dict[str, str]:
    return {
        name: value
        for name, separator, value in (
            entry.partition("=") for entry in entries
        )
        if separator
    }


def test_compose_keeps_decision_runtime_out_of_core_stack() -> None:
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    services = compose["services"]
    healthmes = services["healthmes"]
    runtime = services["hermes-decision"]

    assert "hermes" not in services
    assert runtime["profiles"] == ["decision"]
    assert "hermes-decision" not in healthmes["depends_on"]
    assert healthmes["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-c",
        (
            "import urllib.request; "
            "urllib.request.urlopen("
            "'http://127.0.0.1:8100/health', timeout=2).read()"
        ),
    ]
    assert runtime["depends_on"] == {
        "healthmes": {"condition": "service_healthy"}
    }

    healthmes_env = _environment(healthmes["environment"])
    assert healthmes_env["HEALTHMES_DECISION_HERMES_BASE_URL"] == (
        "${HEALTHMES_DECISION_HERMES_BASE_URL:-}"
    )
    assert healthmes_env["HEALTHMES_DECISION_HERMES_MODEL"] == (
        "${HEALTHMES_DECISION_HERMES_MODEL:-}"
    )
    assert healthmes_env["HEALTHMES_DECISION_HERMES_PROVIDER"] == (
        "${HEALTHMES_DECISION_HERMES_PROVIDER:-}"
    )


def test_decision_bind_source_exists_before_core_compose_boot() -> None:
    """Prevent Docker from creating a root-owned bootstrap destination."""

    assert DECISION_BIND_SOURCE.is_dir()
    assert (DECISION_BIND_SOURCE / ".gitkeep").is_file()


def test_compose_launches_only_healthmes_owned_supervisor_surface() -> None:
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    runtime = compose["services"]["hermes-decision"]

    assert "env_file" not in runtime
    assert runtime["entrypoint"] == [
        "/opt/hermes/.venv/bin/python",
        "-m",
        "healthmes.hermes_runtime_supervisor",
    ]
    assert runtime["command"] == [
        "--hermes-home",
        "/opt/data",
        "--vendor-root",
        "/opt/hermes",
    ]
    assert runtime["volumes"] == [
        "./data/hermes/decision:/opt/data",
        "./healthmes:/opt/healthmes/healthmes:ro",
    ]
    runtime_env = _environment(runtime["environment"])
    assert runtime_env["HERMES_HOME"] == "/opt/data"
    assert runtime_env["HEALTHMES_DECISION_RUNTIME_PORT"] == "8645"
    assert runtime_env[
        "HEALTHMES_DECISION_RUNTIME_MAX_CONCURRENT_RESPONSES"
    ] == "${HEALTHMES_DECISION_MAX_PENDING_REQUESTS:-8}"
    assert runtime["stop_grace_period"] == "6m"
    assert "TELEGRAM_BOT_TOKEN" not in runtime_env
    assert "HEALTHMES_HERMES_WEBHOOK_SECRET" not in runtime_env
    serialized = yaml.safe_dump(runtime)
    assert "/v1/model/iterations" not in serialized
    assert "gateway run" not in serialized


def test_readme_uses_canonical_profile_gated_decision_launch() -> None:
    readme = README_FILE.read_text(encoding="utf-8")
    docker_section = readme.split("### Docker alternative", 1)[1].split(
        "### Runtime diagnostics",
        1,
    )[0]
    bootstrap = "uv run python scripts/bootstrap.py --mode docker"
    launch = "docker compose --profile decision up -d --build"

    assert bootstrap in docker_section
    assert launch in docker_section
    assert docker_section.index(bootstrap) < docker_section.index(launch)
    assert "`HEALTHMES_DECISION_HERMES_MODEL`" in docker_section
    assert "`HEALTHMES_DECISION_HERMES_PROVIDER`" in docker_section
    assert "`HERMES_MODEL`/`HERMES_PROVIDER`" not in readme


def test_runtime_refresh_uses_bounded_full_drain_timeout() -> None:
    development = " ".join(
        DEVELOPMENT_DOC.read_text(encoding="utf-8").split()
    )

    assert "docker compose stop --timeout 360 hermes-decision" in development
    assert "maximum supported 300-second decision response" in development
    assert "10-second child" in development


@pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="docker CLI is not installed",
)
def test_compose_cli_profiles_keep_core_secret_free(tmp_path: Path) -> None:
    version = subprocess.run(
        ["docker", "compose", "version"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if version.returncode != 0:
        pytest.skip("docker compose plugin is not available")
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")

    core = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(empty_env),
            "config",
            "--services",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    decision = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(empty_env),
            "--profile",
            "decision",
            "config",
            "--services",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "healthmes" in core.stdout.splitlines()
    assert "hermes-decision" not in core.stdout.splitlines()
    assert "hermes-decision" in decision.stdout.splitlines()
