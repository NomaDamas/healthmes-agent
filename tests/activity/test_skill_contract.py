from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = REPO_ROOT / "docs" / "contracts" / "HEALTHMES-ACTIVITY-WELLNESS-SKILL.ko.md"


def test_activity_skill_contract_is_runtime_independent_and_complete() -> None:
    text = CONTRACT.read_text(encoding="utf-8")

    for heading in (
        "## 1. Capability boundary",
        "## 2. Normative tools",
        "## 3. Question routing",
        "## 4. Response contract",
        "## 5. Specialized-policy boundary",
        "## 6. Privacy contract",
        "## 7. Failure behavior",
        "## 8. Adapter acceptance",
    ):
        assert heading in text

    for tool in (
        "get_activity_summary",
        "get_focus_context",
        "get_overwork_context",
        "resolve_wellness_context",
    ):
        assert f"`{tool}" in text

    for response_field in (
        "`status`",
        "`decision_ready`",
        "`candidate_ledger_complete`",
        "`source_refs`",
        "`evidence_ids`",
        "`freshness`",
        "`coverage`",
        "`limitations`",
        "`boundaries`",
    ):
        assert response_field in text

    assert "`vendor/hermes-agent/` 수정" in text
    assert "REST를 직접 호출" in text
    assert "runtime-neutral canonical name" in text
    assert "Compatibility resolver contract: `decision_ready=false`." in text
    assert "`decision_ready`를 항상 `false`로" in text
    assert "`source_refs`는 응답에 존재할 때 adapter가 삭제, 재작성 또는" in text
    assert "`candidate_ledger_complete`는 최종 판단 상태가 아니다." in text
    assert (
        "`candidate_ledger_complete`를 최종 승인이나 `decision_ready=true`로"
        in text
    )
    assert "mcp__healthmes__" not in text


def test_activity_contract_is_indexed_from_product_documents() -> None:
    relative = "contracts/HEALTHMES-ACTIVITY-WELLNESS-SKILL.ko.md"

    assert relative in (REPO_ROOT / "docs" / "ACTIVITY-WELLNESS-MVP.ko.md").read_text(
        encoding="utf-8"
    )
    assert relative in (REPO_ROOT / "docs" / "PLAN.md").read_text(encoding="utf-8")
    assert "HEALTHMES-ACTIVITY-WELLNESS-SKILL.ko.md" in (
        REPO_ROOT / "docs" / "MOAT-CROSS-DOMAIN-WELLNESS-CONTEXT.ko.md"
    ).read_text(encoding="utf-8")
