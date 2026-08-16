"""Repository invariants for the single HealthMes wellness runtime."""

from collections.abc import Iterator
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_DOCS = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "DEVELOPMENT.md",
    REPO_ROOT / "docs" / "PLAN.md",
    REPO_ROOT / "docs" / "HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md",
    REPO_ROOT / "docs" / "HEALTHMES-DECISION-AGENT-ARCHITECTURE.ko.md",
)
PRODUCTION_TEXT_SURFACES = (
    REPO_ROOT / ".env.example",
    REPO_ROOT / "docker-compose.yml",
    REPO_ROOT / "config" / "hermes-decision-config.yaml.tmpl",
    REPO_ROOT / "healthmes" / "config.py",
    REPO_ROOT / "healthmes" / "decision",
    REPO_ROOT / "healthmes" / "engine",
    REPO_ROOT / "healthmes" / "mcp_server",
    REPO_ROOT / "scripts" / "healthmes_local.sh",
    REPO_ROOT / "skills",
)


def _text_files(path: Path) -> Iterator[Path]:
    if path.is_file():
        yield path
        return
    for candidate in sorted(path.rglob("*")):
        if (
            candidate.is_file()
            and candidate.suffix
            in {".md", ".py", ".tmpl", ".yaml", ".yml"}
            and "__pycache__" not in candidate.parts
        ):
            yield candidate


def _combined_text(paths: tuple[Path, ...]) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for root in paths
        for path in _text_files(root)
    )


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


def test_production_surfaces_have_no_retired_reasoning_entrypoints() -> None:
    text = _combined_text(PRODUCTION_TEXT_SURFACES)

    for forbidden in (
        "/v1/model/iterations",
        "HermesWebhookSender",
        "healthmes.engine.webhook",
        "HEALTHMES_HERMES_WEBHOOK_URL",
        "HEALTHMES_HERMES_WEBHOOK_SECRET",
        "mcp__open_wearables__",
        "mcp__healthmes__record_decision",
    ):
        assert forbidden not in text


def test_generic_decision_writer_is_not_registered_or_documented() -> None:
    server = (
        REPO_ROOT / "healthmes" / "mcp_server" / "server.py"
    ).read_text(encoding="utf-8")
    docs = _combined_text(CANONICAL_DOCS)

    assert "def record_decision(" not in server
    assert "`record_decision`" not in docs
    assert "generic MCP decision writer was removed" in docs


def test_canonical_docs_pin_one_runtime_and_honest_channel_scope() -> None:
    docs = _combined_text(CANONICAL_DOCS)
    normalized = " ".join(docs.split())

    assert "HealthMesDecisionService" in docs
    assert "Hermes /v1/responses" in docs
    assert "DecisionChannelAdapter" in docs
    assert (
        "실제 Telegram/UI inbound는 없고 adapter contract만 있다"
        in normalized
    )
    assert "ow-mcp" not in docs
    assert "HMAC-signed webhook" not in docs
    assert "Telegram remains the guaranteed-delivery channel" not in docs
    assert "The Telegram bot IS the capture app" not in _combined_text(
        (REPO_ROOT / "skills",)
    )


def test_viewer_api_docs_do_not_claim_a_shipped_telegram_channel() -> None:
    text = _combined_text(
        (
            REPO_ROOT / "healthmes" / "api" / "auth.py",
            REPO_ROOT / "healthmes" / "api" / "decisions.py",
        )
    )

    assert "every Telegram alert" not in text
    assert "Telegram alert links" not in text
    assert "Telegram messages or browser history" not in text
