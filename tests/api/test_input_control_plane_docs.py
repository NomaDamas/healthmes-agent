"""Contract checks for the device-team input settings documentation."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INPUT_CONTROL_PLANE_DOC = REPO_ROOT / "docs" / "INPUT-CONTROL-PLANE.ko.md"


def test_input_settings_docs_pin_the_compare_and_swap_flow() -> None:
    text = INPUT_CONTROL_PLANE_DOC.read_text(encoding="utf-8")
    section = text.split(
        "### 필수 설정 저장 흐름: GET -> 편집 -> If-Match PUT -> 재조회·재적용",
        maxsplit=1,
    )[1].split("## 3. 설정 scope", maxsplit=1)[0]

    required_contracts = (
        "GET /v1/inputs/{source_id}",
        "base_descriptor",
        "base_etag",
        "pending_patch",
        "If-Match: <base_etag>",
        "428 input_settings_revision_required",
        "409 input_settings_revision_conflict",
        "latest_descriptor",
        "latest_etag",
        "current_descriptor",
        "current_etag",
        "stale descriptor 전체로 덮어쓰지 않는다",
    )
    for contract in required_contracts:
        assert contract in section

    assert section.index("GET /v1/inputs/{source_id}") < section.index("If-Match: <base_etag>")
    assert section.index("409 input_settings_revision_conflict") < (
        section.index("latest_descriptor")
    )


def test_input_settings_docs_pin_error_codes_and_success_etag_adoption() -> None:
    text = INPUT_CONTROL_PLANE_DOC.read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    assert '"code": "input_settings_revision_required"' in text
    assert '"code": "input_settings_revision_conflict"' in text
    assert "서버 설정은 변경되지 않음" in normalized
    assert "응답 `ETag`를 새 `current_etag`로 저장" in normalized
    assert "PUT 응답 descriptor와 ETag를 다음 편집의 정본" in normalized
