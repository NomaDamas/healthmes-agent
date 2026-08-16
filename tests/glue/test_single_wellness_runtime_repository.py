"""Repository invariants for the single HealthMes wellness runtime."""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_compose_has_no_direct_open_wearables_mcp_service() -> None:
    compose = yaml.safe_load(
        (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    services = compose["services"]

    assert "ow-mcp" not in services
    assert {
        name for name in services if name.startswith("hermes")
    } == {"hermes-decision"}


def test_only_dedicated_healthmes_mcp_profile_is_shipped() -> None:
    assert not (REPO_ROOT / "config" / "hermes-config.yaml.tmpl").exists()
    template = (
        REPO_ROOT / "config" / "hermes-decision-config.yaml.tmpl"
    ).read_text(encoding="utf-8")

    assert "\n  healthmes:\n" in template
    assert "\n  open_wearables:\n" not in template
    assert "mcp__open_wearables__" not in template


def test_bootstrap_and_env_do_not_offer_direct_ow_mcp_controls() -> None:
    bootstrap = (REPO_ROOT / "scripts" / "bootstrap.py").read_text(
        encoding="utf-8"
    )
    env_example = (REPO_ROOT / ".env.example").read_text(
        encoding="utf-8"
    )

    for forbidden in (
        "OW_MCP_DIR",
        "OW_MCP_PORT",
        "OW_MCP_UV_CACHE_DIR",
        "OW_MCP_VENV_DIR",
    ):
        assert forbidden not in bootstrap
        assert forbidden not in env_example
