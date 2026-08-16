"""Skill docs vs the vendor MCP tool-naming contract.

Hermes registers MCP tools as ``mcp__<server>__<tool>`` (double underscores:
``MCP_TOOL_NAME_PREFIX`` + ``_MCP_NAME_DELIM`` in
vendor/hermes-agent/tools/mcp_tool.py::mcp_prefixed_tool_name). The skill
documents teach the agent these names, so a drift here produces an agent
that calls tools which do not exist. The constants are parsed out of the
vendor source text (importing the module would drag in the whole Hermes
runtime) so this test fails if upstream ever changes the convention.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_TOOL_PY = REPO_ROOT / "vendor" / "hermes-agent" / "tools" / "mcp_tool.py"
SKILL_MDS = sorted((REPO_ROOT / "skills").glob("*/SKILL.md"))
EXTENDING_DOC = REPO_ROOT / "docs" / "EXTENDING.md"
EXPERT_ONBOARDING_DOC = REPO_ROOT / "docs" / "EXPERT-ONBOARDING.ko.md"

# The product decision runtime exposes one HealthMes MCP boundary.
SERVERS = ("healthmes",)


def _vendor_constant(name: str) -> str:
    match = re.search(
        rf'^{name}\s*=\s*"([^"]+)"', MCP_TOOL_PY.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert match, f"{name} not found in {MCP_TOOL_PY}"
    return match.group(1)


def test_vendor_convention_is_double_underscore() -> None:
    """Guards the premise itself: prefix ``mcp__``, delimiter ``__``."""
    assert _vendor_constant("MCP_TOOL_NAME_PREFIX") == "mcp__"
    assert _vendor_constant("_MCP_NAME_DELIM") == "__"


@pytest.mark.parametrize("skill_md", SKILL_MDS, ids=lambda p: p.parent.name)
def test_skill_docs_use_registry_tool_names(skill_md: Path) -> None:
    text = skill_md.read_text(encoding="utf-8")
    prefix = _vendor_constant("MCP_TOOL_NAME_PREFIX")
    delim = _vendor_constant("_MCP_NAME_DELIM")

    # Single-underscore forms (mcp_healthmes_x, mcp_<server>_<tool>) never
    # existed in the registry — an agent taught them calls nonexistent tools.
    # `(?<!_)` / `(?!_)` pin exactly ONE underscore on each side so the
    # correct double-underscore names do not match their own substrings.
    single_underscore = re.compile(
        r"(?<!_)mcp_(?:healthmes|open_wearables|<server>)_(?!_)"
    )
    assert not single_underscore.search(text), (
        f"{skill_md} documents single-underscore MCP names; the registry "
        f"convention is {prefix}<server>{delim}<tool> "
        f"(vendor mcp_prefixed_tool_name)"
    )

    # And each skill documents at least one correctly-formed name for a
    # server registered by config/hermes-config.yaml.tmpl.
    valid_starts = tuple(f"`{prefix}{server}{delim}" for server in (*SERVERS, "<server>"))
    assert any(start in text for start in valid_starts), (
        f"{skill_md} documents no {prefix}<server>{delim}<tool> names "
        f"for servers {SERVERS}"
    )


def test_skill_dirs_all_checked() -> None:
    assert [path.parent.name for path in SKILL_MDS] == [
        "doctor-visit-summary",
        "healthmes-caffeine",
        "healthmes-capture",
        "healthmes-nutrition",
        "healthmes-nutrition-decision",
        "healthmes-planner",
        "healthmes-sleep",
        "healthmes-stress",
        "healthmes-wellness-decision",
    ]


def test_product_skills_never_call_open_wearables_directly() -> None:
    for skill_md in SKILL_MDS:
        text = skill_md.read_text(encoding="utf-8")
        assert "mcp__open_wearables__" not in text, (
            f"{skill_md} bypasses the HealthMes MCP boundary"
        )


def test_authoring_docs_keep_decision_and_command_audits_separate() -> None:
    extending = EXTENDING_DOC.read_text(encoding="utf-8")
    onboarding = EXPERT_ONBOARDING_DOC.read_text(encoding="utf-8")

    assert "actual mutation is audited by its separate command workflow" in extending
    assert "mutations keep their audit in the separate command workflow" in extending
    assert "실제 mutation은 별도 command workflow가 자체 audit를 소유" in onboarding
    assert "실제 mutation은 별도\n  command workflow의 audit" in onboarding
    assert "material risk warnings, actual mutations" not in extending
    assert "recommendations, mutations, material risk warnings" not in extending
    assert "행동 변경 제안, 실제 mutation" not in onboarding
    assert "행동 변경 제안, mutation, 중요 위험 경고" not in onboarding


def test_authoring_docs_use_reviewed_wellness_skills_as_templates() -> None:
    extending = EXTENDING_DOC.read_text(encoding="utf-8")
    onboarding = EXPERT_ONBOARDING_DOC.read_text(encoding="utf-8")
    reviewed = (
        "healthmes-wellness-decision",
        "healthmes-nutrition-decision",
        "healthmes-caffeine",
        "healthmes-sleep",
        "healthmes-stress",
    )

    for skill_name in reviewed:
        assert skill_name in extending
        assert skill_name in onboarding
    assert (
        "`healthmes-planner` is a separate bounded command-workflow example"
        in extending
    )
    assert "`healthmes-planner`는 별도 bounded command workflow의 예시" in onboarding
