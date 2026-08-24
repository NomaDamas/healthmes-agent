"""Contract tests for the dedicated Hermes decision profile."""

from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

from healthmes.decision import (
    HERMES_DECISION_MCP_TOOL_NAMES,
    HermesDecisionProfileAssertion,
)

TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "hermes-decision-config.yaml.tmpl"
)


def _render(tmp_path: Path) -> tuple[Path, dict]:
    rendered = Environment(
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    ).from_string(TEMPLATE_PATH.read_text(encoding="utf-8")).render(
        hermes_model="decision-model",
        hermes_provider="openai",
        hermes_model_base_url="",
        hermes_model_api_key="",
        decision_hermes_model="decision-model",
        decision_hermes_provider="openai",
        decision_hermes_model_base_url="",
        decision_hermes_model_api_key="",
        decision_hermes_host="127.0.0.1",
        decision_hermes_port="8645",
        decision_hermes_api_key="k" * 64,
        healthmes_mcp_url="http://127.0.0.1:8100/mcp",
        healthmes_api_token="healthmes-token",
    )
    path = tmp_path / "config.yaml"
    path.write_text(rendered, encoding="utf-8")
    return path, yaml.safe_load(rendered)


def test_template_is_a_dedicated_filtered_decision_profile(
    tmp_path: Path,
) -> None:
    path, config = _render(tmp_path)

    digest = HermesDecisionProfileAssertion(
        path,
        expected_model="decision-model",
        expected_provider="openai",
        expected_api_key="k" * 64,
    ).verify()

    assert len(digest) == 64
    assert set(config["platforms"]) == {"api_server"}
    assert config["model"] == {
        "default": "decision-model",
        "provider": "openai",
    }
    assert config["compression"] == {"in_place": True}
    assert config["platforms"]["api_server"]["extra"]["model_routes"] == {
        "decision-model": {
            "model": "decision-model",
            "provider": "openai",
        }
    }
    assert config["platform_toolsets"] == {
        "api_server": ["healthmes"]
    }
    assert set(config["mcp_servers"]) == {"healthmes"}
    assert (
        config["mcp_servers"]["healthmes"]["tools"]["resources"] is False
    )
    assert (
        config["mcp_servers"]["healthmes"]["tools"]["prompts"] is False
    )
    assert config["mcp_servers"]["healthmes"]["tools"]["include"] == list(
        HERMES_DECISION_MCP_TOOL_NAMES
    )
    assert config["mcp_servers"]["healthmes"]["headers"] == {
        "Authorization": "Bearer healthmes-token"
    }
    assert "open_wearables" not in config["mcp_servers"]


def test_profile_digest_changes_when_the_healthmes_origin_changes(
    tmp_path: Path,
) -> None:
    first_path, _config = _render(tmp_path)
    first = HermesDecisionProfileAssertion(
        first_path,
        expected_model="decision-model",
        expected_provider="openai",
        expected_api_key="k" * 64,
    ).verify()
    second_path = tmp_path / "second.yaml"
    second_text = first_path.read_text(encoding="utf-8").replace(
        "http://127.0.0.1:8100/mcp",
        "https://healthmes.example.com/mcp",
    )
    second_path.write_text(second_text, encoding="utf-8")

    second = HermesDecisionProfileAssertion(
        second_path,
        expected_model="decision-model",
        expected_provider="openai",
        expected_api_key="k" * 64,
    ).verify()

    assert first != second
