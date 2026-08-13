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

# The two MCP servers registered by config/hermes-config.yaml.tmpl.
SERVERS = ("healthmes", "open_wearables")


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
        "healthmes-capture",
        "healthmes-planner",
        "healthmes-sleep",
        "healthmes-stress",
        "healthmes-whoop-recovery",
    ]


def test_whoop_recovery_skill_keeps_its_data_and_recording_boundaries() -> None:
    text = (REPO_ROOT / "skills" / "healthmes-whoop-recovery" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "mcp__healthmes__get_whoop_recovery_context" in text
    assert "Never recreate numeric WHOOP\n   thresholds" in text
    assert "workout-specific" in text
    assert "Never offer 20 or 30 minutes" in text
    assert "separate immutable" in text
    assert "do **not** create a\ncompletion record" in text
    assert "v0 leaves completion unrecorded" in text
    assert "Never put raw scores, timestamps" in text


@pytest.mark.parametrize("skill_name", ("healthmes-sleep", "healthmes-stress"))
def test_current_health_skills_fail_closed_without_live_mcp(skill_name: str) -> None:
    text = (REPO_ROOT / "skills" / skill_name / "SKILL.md").read_text(encoding="utf-8")
    assert "fail closed: return `insufficient_data`" in text
    assert "Do not search session memory or local files" in text
    assert "reuse a past observation as current evidence" in text
    assert "Do not\n  record a decision from substituted or stale evidence" in text


@pytest.mark.parametrize(
    ("doc_name", "minimum_commands"),
    (("DEVELOPMENT.md", 2), ("EXTENDING.md", 1)),
)
def test_hermes_runtime_docs_install_mcp_extra(
    doc_name: str, minimum_commands: int
) -> None:
    text = (REPO_ROOT / "docs" / doc_name).read_text(encoding="utf-8")
    assert text.count("--extra messaging --extra mcp") >= minimum_commands
