"""Render-tests for config/hermes-config.yaml.tmpl.

The rendered output must be valid YAML whose keys match the vendor parsers:
- platforms.telegram / platforms.webhook (vendor gateway/config.py,
  gateway/platforms/webhook.py)
- mcp_servers stdio (command/args/env) and url transports
  (vendor tools/mcp_tool.py)
"""

from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "hermes-config.yaml.tmpl"
)

FULL_CONTEXT = {
    "telegram_bot_token": "123456:test-token",
    "telegram_home_chat_id": "987654321",
    "telegram_home_chat_name": "Me",
    "telegram_allowed_user_ids": ["987654321"],
    "hermes_webhook_port": 8644,
    "hermes_webhook_secret": "hmac-secret",
    "healthmes_alert_prompt": "Alert {rule_id}: {summary}",
    "ow_mcp_dir": "/opt/vendor/open-wearables-mcp",
    "ow_base_url": "http://ow-backend:8000",
    "ow_api_key": "ow-key",
    "ow_mcp_venv_dir": "/opt/data/ow-mcp-venv",
    "ow_mcp_uv_cache_dir": "/opt/data/uv-cache",
    "healthmes_mcp_url": "http://healthmes:8100/mcp",
    "healthmes_api_token": "hm-bearer-token",
    "hermes_model": "gpt-5.4",
    "hermes_provider": "openai-codex",
    "hermes_model_base_url": "",
}

MINIMAL_CONTEXT = {
    "telegram_bot_token": "123456:test-token",
    "hermes_webhook_secret": "hmac-secret",
    "ow_api_key": "ow-key",
}


def render(context: dict) -> dict:
    env = Environment(undefined=StrictUndefined, keep_trailing_newline=True)
    # Optional variables must survive StrictUndefined via | default(..., true).
    for optional in FULL_CONTEXT:
        context.setdefault(optional, "")
    rendered = env.from_string(TEMPLATE_PATH.read_text()).render(**context)
    return yaml.safe_load(rendered)


@pytest.fixture(params=["full", "minimal"])
def config(request) -> dict:
    context = dict(FULL_CONTEXT if request.param == "full" else MINIMAL_CONTEXT)
    return render(context)


def test_telegram_platform_keys(config: dict) -> None:
    telegram = config["platforms"]["telegram"]
    assert telegram["enabled"] is True
    assert telegram["token"] == "123456:test-token"
    # allow_from lives under extra (adapter reads config.extra["allow_from"]).
    assert isinstance(telegram["extra"]["allow_from"], list)


def test_home_channel_only_rendered_when_chat_id_set() -> None:
    full = render(dict(FULL_CONTEXT))
    home = full["platforms"]["telegram"]["home_channel"]
    # HomeChannel.from_dict requires platform + chat_id keys.
    assert home["platform"] == "telegram"
    assert home["chat_id"] == "987654321"

    minimal = render(dict(MINIMAL_CONTEXT))
    assert "home_channel" not in minimal["platforms"]["telegram"]


def test_webhook_route_matches_adapter_contract(config: dict) -> None:
    webhook = config["platforms"]["webhook"]
    assert webhook["enabled"] is True
    extra = webhook["extra"]
    assert extra["port"] == 8644

    # Route name is the /webhooks/{route_name} URL path segment that
    # healthmes/engine/triggers.py POSTs to.
    route = extra["routes"]["healthmes-alerts"]
    # Per-route HMAC secret is mandatory (WebhookAdapter.connect validates).
    assert route["secret"] == "hmac-secret"
    assert isinstance(route["prompt"], str) and route["prompt"].strip()
    assert route["skills"] == ["healthmes-planner"]
    assert route["deliver"] == "telegram"


def test_mcp_servers_stdio_and_url_transports(config: dict) -> None:
    servers = config["mcp_servers"]

    ow = servers["open_wearables"]
    # stdio transport: command + args (+ env), per mcp_tool.py example config.
    # `start` is the [project.scripts] entry of vendor/open-wearables/mcp.
    assert ow["command"] == "uv"
    assert ow["args"][0] == "run"
    assert ow["args"][-1] == "start"
    assert "OPEN_WEARABLES_API_URL" in ow["env"]
    assert ow["env"]["OPEN_WEARABLES_API_KEY"] == "ow-key"

    hm = servers["healthmes"]
    # Streamable HTTP transport: url key.
    assert hm["url"].endswith("/mcp")
    assert "command" not in hm


def test_healthmes_bearer_header_only_rendered_with_token() -> None:
    """mcp_tool.py url transports accept a `headers:` mapping; the bearer
    header must appear iff the healthmes surface is token-protected."""
    full = render(dict(FULL_CONTEXT))
    assert full["mcp_servers"]["healthmes"]["headers"] == {
        "Authorization": "Bearer hm-bearer-token"
    }

    minimal = render(dict(MINIMAL_CONTEXT))
    assert "headers" not in minimal["mcp_servers"]["healthmes"]


def test_native_localhost_defaults() -> None:
    """With no explicit endpoints the template must default to localhost —
    docker service hostnames only ever arrive via injected context."""
    cfg = render(dict(MINIMAL_CONTEXT))
    ow_env = cfg["mcp_servers"]["open_wearables"]["env"]
    assert ow_env["OPEN_WEARABLES_API_URL"] == "http://localhost:8000"
    assert cfg["mcp_servers"]["healthmes"]["url"] == "http://localhost:8100/mcp"


def test_model_block_only_rendered_when_selected() -> None:
    """LLM selection is optional: omitted -> no `model:` key (vendor
    auto-defaults to Anthropic); set -> root model.default/provider per
    vendor hermes_cli/config.py::_normalize_root_model_keys."""
    minimal = render(dict(MINIMAL_CONTEXT))
    assert "model" not in minimal

    full = render(dict(FULL_CONTEXT))
    assert full["model"] == {"default": "gpt-5.4", "provider": "openai-codex"}

    with_base = render(dict(FULL_CONTEXT, hermes_model_base_url="http://localhost:11434/v1"))
    assert with_base["model"]["base_url"] == "http://localhost:11434/v1"


def test_default_alert_prompt_renders_clean() -> None:
    """The built-in alert prompt keeps webhook placeholders intact and does
    not leak template-source indentation into the message body."""
    cfg = render(dict(MINIMAL_CONTEXT))
    prompt = cfg["platforms"]["webhook"]["extra"]["routes"]["healthmes-alerts"]["prompt"]
    assert "{rule_id}" in prompt
    # The full agent instruction (record_decision steps, notification grammar)
    # arrives via the payload's `prompt` field — a plain string placeholder
    # renders uncapped, unlike {__raw__} which the gateway truncates to 4000
    # chars of indent-2 JSON and could clip evidence on large fires
    # (vendor gateway/platforms/webhook.py::_render_prompt).
    assert "{prompt}" in prompt
    assert "{__raw__}" not in prompt
    for line in prompt.splitlines():
        assert line == line.lstrip(), f"indented line leaked into prompt: {line!r}"
