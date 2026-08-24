"""Fail-closed validation for the dedicated Hermes decision profile."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from healthmes.hermes_mcp_inventory import (
    HERMES_DECISION_MCP_SERVER,
    HERMES_DECISION_MCP_TOOL_NAMES,
)

HERMES_DECISION_RUNTIME_MODEL_NAME = "healthmes-decision-runtime"
HERMES_DECISION_SEARCH_MCP_TOOL_NAMES = (
    "search_activity",
    "search_calendar",
    "search_nutrition",
    "search_wearable",
)
HERMES_DECISION_SKILL_MCP_TOOL_NAMES = (
    "list_wellness_skills",
    "read_wellness_skill",
)
HERMES_DECISION_TOOL_DOMAINS: dict[str, str] = {
    "mcp__healthmes__search_activity": "activity",
    "mcp__healthmes__search_nutrition": "nutrition",
    "mcp__healthmes__search_calendar": "calendar",
    "mcp__healthmes__search_wearable": "wearable",
}
HERMES_DECISION_SEARCH_TOOL_ALLOWLIST = frozenset(
    HERMES_DECISION_TOOL_DOMAINS
)
HERMES_DECISION_SKILL_TOOL_ALLOWLIST = frozenset(
    f"mcp__{HERMES_DECISION_MCP_SERVER}__{name}"
    for name in HERMES_DECISION_SKILL_MCP_TOOL_NAMES
)
HERMES_DECISION_TOOL_ALLOWLIST = (
    HERMES_DECISION_SEARCH_TOOL_ALLOWLIST
    | HERMES_DECISION_SKILL_TOOL_ALLOWLIST
)
_HERMES_DECISION_EXACT_MCP_TOOL_PROFILE = frozenset(
    HERMES_DECISION_MCP_TOOL_NAMES
)

# The API-server platform is explicitly limited to the HealthMes MCP server.
# This second deny list prevents a later platform-toolset edit from silently
# enabling a currently known native bundle. GET /v1/toolsets catches future
# native/plugin bundles that are not represented here.
HERMES_DECISION_NATIVE_TOOLSET_DENYLIST = frozenset(
    {
        "browser",
        "clarify",
        "code_execution",
        "computer_use",
        "context_engine",
        "cronjob",
        "delegation",
        "discord",
        "discord_admin",
        "file",
        "homeassistant",
        "image_gen",
        "memory",
        "session_search",
        "skills",
        "spotify",
        "terminal",
        "todo",
        "tts",
        "video",
        "video_gen",
        "vision",
        "web",
        "x_search",
        "yuanbao",
    }
)

_MAX_PROFILE_BYTES = 256_000
_PROFILE_DIGEST_SCHEMA = "healthmes.hermes-decision-profile.v2"


class HermesDecisionProfileError(ValueError):
    """Raised when a rendered Hermes profile is not decision-safe."""


@dataclass(frozen=True, slots=True)
class HermesDecisionProfileDetails:
    """Verified semantic digest and exact MCP surface."""

    semantic_digest: str
    mcp_tool_names: tuple[str, ...]
    full_tool_names: frozenset[str]


@dataclass(frozen=True, slots=True)
class HermesDecisionProfileAssertion:
    """Validate a local deployment artifact before contacting Hermes."""

    path: Path
    expected_model: str | None = None
    expected_provider: str | None = None
    expected_api_key: str | None = None

    def verify(self) -> str:
        return self.verify_details().semantic_digest

    def verify_details(self) -> HermesDecisionProfileDetails:
        """Validate and return the exact tool surface bound by the profile."""

        path = self.path.expanduser()
        try:
            if not path.is_file():
                raise HermesDecisionProfileError(
                    "hermes_decision_profile_missing"
                )
            raw = path.read_bytes()
        except HermesDecisionProfileError:
            raise
        except OSError as exc:
            raise HermesDecisionProfileError(
                "hermes_decision_profile_unreadable"
            ) from exc
        if len(raw) > _MAX_PROFILE_BYTES:
            raise HermesDecisionProfileError(
                "hermes_decision_profile_too_large"
            )
        try:
            loaded = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise HermesDecisionProfileError(
                "hermes_decision_profile_invalid"
            ) from exc
        profile = _mapping(
            loaded,
            code="hermes_decision_profile_invalid",
        )
        asserted = _asserted_profile(
            profile,
            expected_model=self.expected_model,
            expected_provider=self.expected_provider,
            expected_api_key=self.expected_api_key,
        )
        mcp_tool_names = tuple(asserted["mcp"]["tools"])
        encoded = json.dumps(
            asserted,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return HermesDecisionProfileDetails(
            semantic_digest=hashlib.sha256(encoded).hexdigest(),
            mcp_tool_names=mcp_tool_names,
            full_tool_names=frozenset(
                f"mcp__{HERMES_DECISION_MCP_SERVER}__{name}"
                for name in mcp_tool_names
            ),
        )


def _asserted_profile(
    profile: Mapping[str, Any],
    *,
    expected_model: str | None,
    expected_provider: str | None,
    expected_api_key: str | None,
) -> dict[str, Any]:
    platforms = _mapping(
        profile.get("platforms"),
        code="hermes_decision_profile_platform_invalid",
    )
    api_server = _mapping(
        platforms.get("api_server"),
        code="hermes_decision_profile_platform_invalid",
    )
    if api_server.get("enabled") is not True:
        raise HermesDecisionProfileError(
            "hermes_decision_profile_platform_invalid"
        )
    for name, raw_platform in platforms.items():
        if name == "api_server":
            continue
        platform = _mapping(
            raw_platform,
            code="hermes_decision_profile_platform_invalid",
        )
        if platform.get("enabled") is True:
            raise HermesDecisionProfileError(
                "hermes_decision_profile_not_dedicated"
            )
    extra = _mapping(
        api_server.get("extra"),
        code="hermes_decision_profile_platform_invalid",
    )
    api_key = extra.get("key")
    if not isinstance(api_key, str) or len(api_key.strip()) < 32:
        raise HermesDecisionProfileError(
            "hermes_decision_profile_auth_invalid"
        )
    if (
        not isinstance(expected_api_key, str)
        or len(expected_api_key.strip()) < 32
        or not hmac.compare_digest(
            api_key.strip(),
            expected_api_key.strip(),
        )
    ):
        raise HermesDecisionProfileError(
            "hermes_decision_profile_auth_mismatch"
        )
    if extra.get("model_name") != HERMES_DECISION_RUNTIME_MODEL_NAME:
        raise HermesDecisionProfileError(
            "hermes_decision_profile_model_route_invalid"
        )
    model_routes = _mapping(
        extra.get("model_routes"),
        code="hermes_decision_profile_model_route_invalid",
    )
    if (
        not isinstance(expected_model, str)
        or not expected_model.strip()
        or not isinstance(expected_provider, str)
        or not expected_provider.strip()
    ):
        raise HermesDecisionProfileError(
            "hermes_decision_profile_model_route_invalid"
        )
    expected_model = expected_model.strip()
    expected_provider = expected_provider.strip()
    if set(model_routes) != {expected_model}:
        raise HermesDecisionProfileError(
            "hermes_decision_profile_model_route_invalid"
        )
    route = _mapping(
        model_routes.get(expected_model),
        code="hermes_decision_profile_model_route_invalid",
    )
    if (
        route.get("model") != expected_model
        or route.get("provider") != expected_provider
    ):
        raise HermesDecisionProfileError(
            "hermes_decision_profile_model_route_invalid"
        )

    compression = _mapping(
        profile.get("compression"),
        code="hermes_decision_profile_compression_invalid",
    )
    if compression.get("in_place") is not True:
        raise HermesDecisionProfileError(
            "hermes_decision_profile_compression_invalid"
        )

    platform_toolsets = _mapping(
        profile.get("platform_toolsets"),
        code="hermes_decision_profile_toolsets_invalid",
    )
    if platform_toolsets.get("api_server") != [
        HERMES_DECISION_MCP_SERVER
    ]:
        raise HermesDecisionProfileError(
            "hermes_decision_profile_toolsets_invalid"
        )

    agent = _mapping(
        profile.get("agent"),
        code="hermes_decision_profile_native_deny_invalid",
    )
    raw_disabled = agent.get("disabled_toolsets")
    if not isinstance(raw_disabled, list) or any(
        not isinstance(item, str) for item in raw_disabled
    ):
        raise HermesDecisionProfileError(
            "hermes_decision_profile_native_deny_invalid"
        )
    disabled = frozenset(raw_disabled)
    if not HERMES_DECISION_NATIVE_TOOLSET_DENYLIST.issubset(disabled):
        raise HermesDecisionProfileError(
            "hermes_decision_profile_native_deny_invalid"
        )

    servers = _mapping(
        profile.get("mcp_servers"),
        code="hermes_decision_profile_mcp_invalid",
    )
    if set(servers) != {HERMES_DECISION_MCP_SERVER}:
        raise HermesDecisionProfileError(
            "hermes_decision_profile_mcp_invalid"
        )
    healthmes = _mapping(
        servers.get(HERMES_DECISION_MCP_SERVER),
        code="hermes_decision_profile_mcp_invalid",
    )
    if healthmes.get("enabled", True) is not True:
        raise HermesDecisionProfileError(
            "hermes_decision_profile_mcp_invalid"
        )
    raw_url = healthmes.get("url")
    if not isinstance(raw_url, str):
        raise HermesDecisionProfileError(
            "hermes_decision_profile_mcp_invalid"
        )
    parsed_url = urlsplit(raw_url.strip())
    if (
        parsed_url.scheme not in {"http", "https"}
        or parsed_url.hostname is None
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise HermesDecisionProfileError(
            "hermes_decision_profile_mcp_invalid"
        )
    filters = _mapping(
        healthmes.get("tools"),
        code="hermes_decision_profile_mcp_invalid",
    )
    if (
        filters.get("resources") is not False
        or filters.get("prompts") is not False
    ):
        raise HermesDecisionProfileError(
            "hermes_decision_profile_mcp_invalid"
        )
    include = filters.get("include")
    exclude = filters.get("exclude")
    if (
        not isinstance(include, list)
        or any(not isinstance(item, str) for item in include)
        or frozenset(include)
        != _HERMES_DECISION_EXACT_MCP_TOOL_PROFILE
        or len(include) != len(set(include))
        or exclude not in (None, [], ())
    ):
        raise HermesDecisionProfileError(
            "hermes_decision_profile_mcp_invalid"
        )

    return {
        "schema": _PROFILE_DIGEST_SCHEMA,
        "platform": "api_server",
        "runtime_model_name": HERMES_DECISION_RUNTIME_MODEL_NAME,
        "model_route": {
            "alias": expected_model,
            "model": expected_model,
            "provider": expected_provider,
        },
        "compression": {"in_place": True},
        "platform_toolsets": [HERMES_DECISION_MCP_SERVER],
        "native_disabled": sorted(
            HERMES_DECISION_NATIVE_TOOLSET_DENYLIST
        ),
        "mcp": {
            "server": HERMES_DECISION_MCP_SERVER,
            "origin": (
                f"{parsed_url.scheme}://{parsed_url.netloc}"
                f"{parsed_url.path.rstrip('/')}"
            ),
            "resources": False,
            "prompts": False,
            "tools": sorted(include),
        },
    }


def _mapping(value: Any, *, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HermesDecisionProfileError(code)
    if any(not isinstance(key, str) for key in value):
        raise HermesDecisionProfileError(code)
    return value
