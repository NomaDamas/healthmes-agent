from __future__ import annotations

import importlib.util
from pathlib import Path

PLUGIN_PATH = (
    Path(__file__).resolve().parents[2] / "plugins" / "healthmes-routing" / "__init__.py"
)


def _load_plugin():
    spec = importlib.util.spec_from_file_location("healthmes_routing", PLUGIN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_telegram_sleep_calendar_update_injects_authoritative_route() -> None:
    plugin = _load_plugin()

    result = plugin._inject_route(
        user_message="7월 29일 Oura 수면 시간을 캘린더에 업데이트해줘.",
        platform="telegram",
    )

    assert result is not None
    context = result["context"]
    assert "prepare_actual_sleep_calendar_update" in context
    assert "Ignore any earlier conversational claim" in context
    assert 'date_basis="night_start"' in context
    assert "without calling a clarification tool" in context


def test_explicit_sleep_times_route_to_oura_summary_date() -> None:
    plugin = _load_plugin()

    result = plugin._inject_route(
        user_message="7/29: 01:23 → 09:01 이 수면 데이터를 캘린더에 업데이트해보자.",
        platform="telegram",
    )

    assert result is not None
    assert 'date_basis="oura_summary"' in result["context"]


def test_refresh_request_forbids_reusing_a_previous_review_url() -> None:
    plugin = _load_plugin()

    result = plugin._inject_route(
        user_message=(
            "7/29 01:23 → 09:01 Oura 수면 기록으로 "
            "캘린더 업데이트 링크 다시 만들어줘."
        ),
        platform="telegram",
    )

    assert result is not None
    context = result["context"]
    assert "Never answer from an earlier tool result" in context
    assert "do not claim a preview or link was refreshed" in context


def test_unrelated_turn_does_not_inject() -> None:
    plugin = _load_plugin()

    assert plugin._inject_route(
        user_message="오늘 Oura 수면 어땠어?",
        platform="telegram",
    ) is None
