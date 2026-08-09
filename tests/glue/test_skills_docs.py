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
WELLNESS_VISUALIZER_MD = REPO_ROOT / "skills" / "healthmes-wellness-visualizer" / "SKILL.md"
APPLE_UNIFIED_PRODUCT_MD = REPO_ROOT / "docs" / "design" / "APPLE-UNIFIED-PRODUCT.ko.md"
WELLNESS_VISUALIZATION_DESIGN_MD = (
    REPO_ROOT / "docs" / "design" / "WELLNESS-VISUALIZATION-SKILL.ko.md"
)

# The two MCP servers registered by config/hermes-config.yaml.tmpl.
SERVERS = ("healthmes", "open_wearables")
WELLNESS_VISUALIZATION_KINDS = {
    "capacity_bar",
    "energy_curve",
    "calendar_canvas",
    "schedule_comparison",
    "proposal_preview",
    "time_series",
    "baseline_band",
    "comparison_bar",
    "factor_contribution",
    "event_aligned_trend",
    "goal_trajectory",
    "decision_outcome",
}


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
    single_underscore = re.compile(r"(?<!_)mcp_(?:healthmes|open_wearables|<server>)_(?!_)")
    assert not single_underscore.search(text), (
        f"{skill_md} documents single-underscore MCP names; the registry "
        f"convention is {prefix}<server>{delim}<tool> "
        f"(vendor mcp_prefixed_tool_name)"
    )

    # And each skill documents at least one correctly-formed name for a
    # server registered by config/hermes-config.yaml.tmpl.
    valid_starts = tuple(f"`{prefix}{server}{delim}" for server in (*SERVERS, "<server>"))
    assert any(start in text for start in valid_starts), (
        f"{skill_md} documents no {prefix}<server>{delim}<tool> names for servers {SERVERS}"
    )


def test_skill_dirs_all_checked() -> None:
    assert [path.parent.name for path in SKILL_MDS] == [
        "doctor-visit-summary",
        "healthmes-caffeine",
        "healthmes-capture",
        "healthmes-nutrition",
        "healthmes-planner",
        "healthmes-sleep",
        "healthmes-stress",
        "healthmes-wellness-visualizer",
    ]


def _wellness_visualizer_text() -> str:
    text = WELLNESS_VISUALIZER_MD.read_text(encoding="utf-8")
    return re.sub(r"\s+", " ", text)


def test_wellness_visualizer_covers_supported_visualization_vocabulary() -> None:
    text = _wellness_visualizer_text()

    for kind in WELLNESS_VISUALIZATION_KINDS:
        assert f"`{kind}`" in text

    assert "Use only the following supported kinds" in text
    assert "at most one supporting" in text


def test_wellness_visualizer_is_mcp_only_and_presentation_only() -> None:
    text = _wellness_visualizer_text()

    assert "existing HealthMes MCP tools" in text
    assert "Never call raw REST endpoints" in text
    assert "Never recompute readiness" in text
    assert "presentation layer, not a second decision engine" in text
    assert "The visualizer never mutates a calendar directly" in text
    assert not re.search(r"https?://|/v1/", text)


def test_wellness_visualizer_fails_closed_and_discloses_evidence_quality() -> None:
    text = _wellness_visualizer_text()

    assert "Return `insufficient_data`" in text
    assert "missing, stale, truncated, low-confidence, mismatched" in text
    assert "sample size not reported" in text
    assert "confidence and sample size are visible" in text
    assert "Missing data is never zero" in text
    assert "different units on one axis" in text


def test_wellness_visualizer_forbids_causal_claims_from_correlation() -> None:
    text = _wellness_visualizer_text()

    assert "Describe the result as an association or temporal pattern" in text
    assert "Never claim that a meal" in text
    assert 'Allowed: "was associated with"' in text
    assert 'Forbidden without an authoritative causal contract: "caused"' in text
    assert "One event is an observation, not a personal rule" in text


def test_wellness_visualizer_preserves_calendar_approval_and_outcome_chain() -> None:
    text = _wellness_visualizer_text()

    required_tools = {
        "mcp__healthmes__propose_schedule_blocks",
        "mcp__healthmes__resolve_schedule_proposal",
        "mcp__healthmes__resolve_calendar_adjustment",
        "mcp__healthmes__record_decision",
    }
    for tool_name in required_tools:
        assert f"`{tool_name}`" in text

    assert "Native iPhone, Mac, and Watch clients use the authenticated REST" in text
    assert "Hermes or Telegram surfaces may use" in text
    assert "must not infer it from list position, title, or time" in text
    assert "visualizer itself must not call it" in text
    assert "Approval is not proof" in text


def test_wellness_visualizer_covers_user_and_proactive_invocation() -> None:
    text = _wellness_visualizer_text()

    assert "### User-initiated" in text
    assert "### Proactive" in text
    assert "Treat the trigger as a hint, not proof" in text
    assert "The trigger owner records any required no-action decision" in text
    assert "the visualizer must not create a decision record" in text
    assert "accepts `source: proactive` only with an exact active" in text
    assert "Informational proactive delivery without a proposal" in text
    assert "Never turn a proactive scene into an automatic calendar write" in text


def test_wellness_design_docs_match_current_scene_contract() -> None:
    product = APPLE_UNIFIED_PRODUCT_MD.read_text(encoding="utf-8")
    design = WELLNESS_VISUALIZATION_DESIGN_MD.read_text(encoding="utf-8")

    assert "별도의 scene API를 추가하지 않는다" not in product
    assert "`POST /v1/wellness/scenes`" in product
    assert "`proposal_preview`" in product
    assert "operation과 source event identity" in product
    assert "생성 scene은 핵심 시각화를 최대 2개" in product

    assert "대표 fixture와 golden scene" not in design
    assert "fixture/golden scene snapshot이 아니라" in design
    assert "sleep timeline" not in design
    assert "time_series + caffeine event annotation" in design
    assert "`proposal_preview`" in design
    assert "proposal 없는 정보형 proactive delivery" in design
    assert "모든 플랫폼에서 한 생성 scene의 기본 상한" in design
    assert "최대 4개 primary visualization" not in design
