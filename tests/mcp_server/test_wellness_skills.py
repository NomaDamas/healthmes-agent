from __future__ import annotations

import hashlib
import re
import tomllib
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from healthmes.mcp_server import wellness_skills as wellness_skills_module
from healthmes.mcp_server.wellness_skills import (
    CANONICAL_WELLNESS_DECISION_TOOLS,
    REVIEWED_WELLNESS_SKILLS,
    WELLNESS_SKILL_CATALOG_VERSION,
    WellnessSkillCatalogError,
    read_reviewed_wellness_skill,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_TOOL_REFERENCE = re.compile(
    r"\bmcp__(?P<server>[A-Za-z0-9_-]+)__"
    r"(?P<tool>[A-Za-z0-9_-]+)\b"
)


@pytest.mark.asyncio
async def test_catalog_lists_only_reviewed_skills(
    mcp_client,
    call_tool,
):
    result = await call_tool(
        mcp_client,
        "list_wellness_skills",
    )

    assert result["schema"] == WELLNESS_SKILL_CATALOG_VERSION
    assert tuple(
        item["name"] for item in result["skills"]
    ) == REVIEWED_WELLNESS_SKILLS
    assert all(item["bytes"] > 0 for item in result["skills"])
    assert all(len(item["sha256"]) == 64 for item in result["skills"])


@pytest.mark.asyncio
async def test_catalog_returns_digest_attested_content(
    mcp_client,
    call_tool,
):
    result = await call_tool(
        mcp_client,
        "read_wellness_skill",
        {"name": "healthmes-wellness-decision"},
    )

    content = result["content"]
    assert result["schema"] == WELLNESS_SKILL_CATALOG_VERSION
    assert result["skill"]["name"] == "healthmes-wellness-decision"
    assert result["skill"]["sha256"] == hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest()
    assert "Do not map it to a fixed" in content
    assert "mcp__healthmes__search_activity" in content


@pytest.mark.asyncio
async def test_catalog_exposes_read_only_nutrition_guidance(
    mcp_client,
    call_tool,
):
    result = await call_tool(
        mcp_client,
        "read_wellness_skill",
        {"name": "healthmes-nutrition-decision"},
    )

    content = result["content"]
    assert "mcp__healthmes__search_nutrition" in content
    assert "Do not call capture" in content
    assert "mcp__healthmes__capture_intake_interaction" not in content
    assert "healthmes-nutrition" not in REVIEWED_WELLNESS_SKILLS


def test_reviewed_skills_are_packaged_and_available_to_docker_source():
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    force_include = pyproject["tool"]["hatch"]["build"]["targets"][
        "wheel"
    ]["force-include"]

    assert force_include == {
        f"skills/{name}": f"healthmes/_wellness_skills/{name}"
        for name in REVIEWED_WELLNESS_SKILLS
    }
    dockerignore = {
        line.strip()
        for line in (REPO_ROOT / ".dockerignore").read_text(
            encoding="utf-8"
        ).splitlines()
    }
    assert "skills/" not in dockerignore


def test_reviewed_skill_catalog_matches_canonical_tool_allowlist():
    allowed = set(CANONICAL_WELLNESS_DECISION_TOOLS)
    referenced: set[str] = set()

    for name in REVIEWED_WELLNESS_SKILLS:
        content = read_reviewed_wellness_skill(name)["content"]
        matches = tuple(MCP_TOOL_REFERENCE.finditer(content))
        assert all(
            match.group("server") == "healthmes"
            for match in matches
        )
        skill_tools = {
            match.group("tool")
            for match in matches
        }
        assert skill_tools <= allowed
        referenced.update(skill_tools)

    assert referenced == allowed


@pytest.mark.parametrize(
    ("reference", "error_code"),
    (
        (
            "mcp__healthmes__record_decision",
            "wellness_skill_tool_not_allowed",
        ),
        (
            "mcp__open_wearables__get_sleep",
            "wellness_skill_mcp_server_not_allowed",
        ),
    ),
)
def test_catalog_rejects_noncanonical_mcp_references(
    monkeypatch,
    reference,
    error_code,
):
    content = (
        "---\n"
        "name: healthmes-caffeine\n"
        "description: Invalid reviewed skill fixture.\n"
        "---\n\n"
        f"Call `{reference}`.\n"
    ).encode()
    monkeypatch.setattr(
        wellness_skills_module,
        "_read_skill_bytes",
        lambda _name: content,
    )

    with pytest.raises(WellnessSkillCatalogError, match=error_code):
        read_reviewed_wellness_skill("healthmes-caffeine")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    (
        "../healthmes-planner",
        "healthmes-planner",
        "/tmp/SKILL.md",
        "healthmes-wellness-decision/SKILL.md",
    ),
)
async def test_catalog_rejects_unreviewed_or_path_like_names(
    mcp_client,
    name,
):
    with pytest.raises(ToolError, match="wellness_skill_not_reviewed"):
        await mcp_client.call_tool(
            "read_wellness_skill",
            {"name": name},
        )
