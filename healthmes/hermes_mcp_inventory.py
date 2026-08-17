"""Canonical live MCP inventory for the dedicated Hermes decision runtime."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

HERMES_DECISION_MCP_SERVER = "healthmes"
HERMES_DECISION_MCP_TOOL_NAMES = (
    "search_activity",
    "search_calendar",
    "search_nutrition",
    "search_wearable",
    "list_wellness_skills",
    "read_wellness_skill",
)
HERMES_MCP_INVENTORY_SCHEMA = "healthmes.hermes-mcp-inventory.v1"

# These hashes bind the exact FastMCP inputSchema exposed to Hermes. A contract
# test compares them with the registered HealthMes server tools.
HERMES_DECISION_MCP_INPUT_SCHEMA_SHA256 = {
    "list_wellness_skills": (
        "99334726611ccf58a148b0814696bfa6fe08c1b2d027e946beccf5a74331c9aa"
    ),
    "read_wellness_skill": (
        "c70d2daf71667bd20bdb983d31b4f01657a3308970b3c3d0d7f3bcf3b5d9bf20"
    ),
    "search_activity": (
        "f9c4b2ae6b785775905cebf5ff4aaab9c1998d2fd3e560c71af918d58dbcdafd"
    ),
    "search_calendar": (
        "e2e394823cfc616e55ecb380b5ca8e1e6c64ced46be58ef7aee05c85be3642c9"
    ),
    "search_nutrition": (
        "989c29458d67f6e9089749b7eb41c3ac8d68843fa0ed0a8327857cfb0a3631a8"
    ),
    "search_wearable": (
        "5208542abe23e47d7c2316c5308fc9df691f1f1713fe1f956c8b7639fac3ec24"
    ),
}


class HermesMcpInventoryError(ValueError):
    """A live model-visible MCP inventory violated the owned contract."""


class HermesMcpToolSchemaDigest(BaseModel):
    """One model-visible MCP tool and its canonical input schema digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=255)
    input_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class HermesMcpToolInventory(BaseModel):
    """Signed, deterministic inventory of the MCP tools visible to Hermes."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    schema_name: Literal["healthmes.hermes-mcp-inventory.v1"] = Field(
        alias="schema"
    )
    server: str = Field(min_length=1, max_length=128)
    tools: tuple[HermesMcpToolSchemaDigest, ...] = Field(
        min_length=1,
        max_length=64,
    )
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_inventory(self) -> HermesMcpToolInventory:
        names = tuple(item.name for item in self.tools)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("MCP inventory tool names must be unique and sorted")
        if self.digest != _inventory_digest(
            server=self.server,
            tools=self.tools,
        ):
            raise ValueError("MCP inventory digest does not match its content")
        return self


def canonical_input_schema_sha256(value: Mapping[str, Any]) -> str:
    """Hash one MCP input schema using a stable JSON representation."""

    if any(not isinstance(key, str) for key in value):
        raise HermesMcpInventoryError(
            "hermes_runtime_mcp_schema_invalid"
        )
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, UnicodeError, ValueError) as exc:
        raise HermesMcpInventoryError(
            "hermes_runtime_mcp_schema_invalid"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def inventory_from_schema_digests(
    schema_digests: Mapping[str, str],
    *,
    server: str = HERMES_DECISION_MCP_SERVER,
) -> HermesMcpToolInventory:
    """Build a structurally valid inventory without applying product policy."""

    if (
        not isinstance(server, str)
        or not server
        or any(not isinstance(name, str) for name in schema_digests)
        or any(
            not isinstance(digest, str)
            for digest in schema_digests.values()
        )
    ):
        raise HermesMcpInventoryError(
            "hermes_runtime_mcp_inventory_invalid"
        )
    tools = tuple(
        HermesMcpToolSchemaDigest(
            name=name,
            input_schema_sha256=schema_digests[name],
        )
        for name in sorted(schema_digests)
    )
    payload_digest = _inventory_digest(server=server, tools=tools)
    return HermesMcpToolInventory(
        schema=HERMES_MCP_INVENTORY_SCHEMA,
        server=server,
        tools=tools,
        digest=payload_digest,
    )


def validate_model_visible_mcp_inventory(
    schema_digests: Mapping[str, str],
) -> HermesMcpToolInventory:
    """Require exactly the six HealthMes tools and their owned schemas."""

    try:
        inventory = inventory_from_schema_digests(schema_digests)
    except Exception as exc:
        if isinstance(exc, HermesMcpInventoryError):
            raise
        raise HermesMcpInventoryError(
            "hermes_runtime_mcp_inventory_invalid"
        ) from exc
    expected_names = frozenset(HERMES_DECISION_MCP_TOOL_NAMES)
    actual_names = frozenset(item.name for item in inventory.tools)
    if (
        actual_names != expected_names
        or len(inventory.tools) != len(HERMES_DECISION_MCP_TOOL_NAMES)
    ):
        raise HermesMcpInventoryError(
            "hermes_runtime_mcp_inventory_mismatch"
        )
    actual = {
        item.name: item.input_schema_sha256 for item in inventory.tools
    }
    if actual != HERMES_DECISION_MCP_INPUT_SCHEMA_SHA256:
        raise HermesMcpInventoryError(
            "hermes_runtime_mcp_schema_mismatch"
        )
    return inventory


def expected_hermes_mcp_inventory() -> HermesMcpToolInventory:
    """Return the canonical model-visible inventory expected in production."""

    return validate_model_visible_mcp_inventory(
        HERMES_DECISION_MCP_INPUT_SCHEMA_SHA256
    )


def schema_digests_from_mcp_tools(
    raw_tools: Sequence[Any],
    *,
    included_names: Sequence[str] = HERMES_DECISION_MCP_TOOL_NAMES,
) -> dict[str, str]:
    """Apply the dedicated profile filter to an untrusted MCP ``tools/list``."""

    if (
        tuple(sorted(included_names))
        != tuple(sorted(HERMES_DECISION_MCP_TOOL_NAMES))
        or len(included_names) != len(set(included_names))
    ):
        raise HermesMcpInventoryError(
            "hermes_runtime_mcp_filter_invalid"
        )
    included = frozenset(included_names)
    selected: dict[str, str] = {}
    if len(raw_tools) > 4_096:
        raise HermesMcpInventoryError(
            "hermes_runtime_mcp_inventory_too_large"
        )
    for raw_tool in raw_tools:
        if isinstance(raw_tool, Mapping):
            name = raw_tool.get("name")
            input_schema = raw_tool.get("inputSchema")
        else:
            name = getattr(raw_tool, "name", None)
            input_schema = getattr(raw_tool, "inputSchema", None)
        if name not in included:
            continue
        if name in selected or not isinstance(input_schema, Mapping):
            raise HermesMcpInventoryError(
                "hermes_runtime_mcp_inventory_invalid"
            )
        selected[name] = canonical_input_schema_sha256(input_schema)
    return selected


def _inventory_digest(
    *,
    server: str,
    tools: tuple[HermesMcpToolSchemaDigest, ...],
) -> str:
    payload = {
        "schema": HERMES_MCP_INVENTORY_SCHEMA,
        "server": server,
        "tools": [
            item.model_dump(mode="json", round_trip=True)
            for item in tools
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()
