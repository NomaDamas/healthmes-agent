"""MCP adapter tests for reusable food capture and decision context."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastmcp.exceptions import ToolError

from healthmes.mcp_server import server as server_module
from healthmes.nutrition.contracts import (
    Confidence,
    EstimateKind,
    IntakeType,
    ObservationStatus,
)
from healthmes.nutrition.schema import VLMEstimate, VLMExtraction, VLMItem
from healthmes.nutrition.transcription import TranscriptionResult
from healthmes.storage import register_storage_object
from healthmes.trusted_session import issue_trusted_session_proof


def _trusted(tool_name, arguments):
    proof = issue_trusted_session_proof(
        "test-calendar-adjustment-secret-32-characters",
        tool_name=tool_name,
        arguments=arguments,
        platform="telegram",
        chat_id="owner-chat",
        user_id="owner-user",
        message_id=str(uuid.uuid4()),
    )
    return {**arguments, "trusted_session_proof": proof}


def _items(nutrient: str = "protein") -> list[dict]:
    return [
        {
            "name": "chicken salad",
            "intake_type": "food",
            "serving": {
                "kind": "exact",
                "unit": "serving",
                "exact": 1,
                "estimation_basis": "owner_statement",
            },
            "nutrients": [
                {
                    "nutrient": nutrient,
                    "amount": {
                        "kind": "exact",
                        "unit": "g" if nutrient == "protein" else "mg",
                        "exact": 30 if nutrient == "protein" else 120,
                        "estimation_basis": "owner_statement",
                    },
                    "confidence": "high",
                    "origin": "user",
                    "evidence_text": f"{nutrient} stated by owner",
                }
            ],
            "confidence": "high",
            "warnings": [],
        }
    ]


class FakeAnalysis:
    provider_name = "fixture"
    model = "fixture-model"
    model_digest = "sha256:fixture"

    def __init__(self):
        self.calls = []

    def analyze_text(self, text, *, allow_remote):
        self.calls.append((text, allow_remote))
        return VLMExtraction(
            status=ObservationStatus.USABLE,
            confidence=Confidence.MEDIUM,
            warnings=[],
            items=[
                VLMItem(
                    intake_type=IntakeType.BEVERAGE,
                    name_candidates=["coffee"],
                    category="coffee",
                    serving=VLMEstimate(
                        kind=EstimateKind.RANGE,
                        unit="ml",
                        minimum=250,
                        maximum=400,
                        estimation_basis="owner_portion_description",
                    ),
                    caffeine=VLMEstimate(
                        kind=EstimateKind.RANGE,
                        unit="mg",
                        minimum=80,
                        maximum=180,
                        estimation_basis="beverage_type",
                    ),
                    confidence=Confidence.MEDIUM,
                )
            ],
        )


class FakeTranscriber:
    def transcribe(self, audio_path):
        return TranscriptionResult(
            text="커피 한 잔을 마셨어",
            provider="fixture-whisper",
            model="fixture-small",
        )


async def test_mcp_automatically_analyzes_text_capture(
    mcp_client, call_tool, monkeypatch
):
    provider = FakeAnalysis()
    monkeypatch.setattr(
        server_module,
        "create_vision_provider",
        lambda settings: provider,
    )
    arguments = {
        "operation_id": str(uuid.uuid4()),
        "intent": "log_consumed",
        "modality": "text",
        "source_text": "커피 한 잔을 마셨어",
        "observed_at": "2026-08-06T03:30:00Z",
        "media_path": None,
        "allow_remote_analysis": False,
    }
    result = await call_tool(
        mcp_client,
        "analyze_intake_capture",
        _trusted("analyze_intake_capture", arguments),
    )

    assert result["interaction"]["items"][0]["name"] == "coffee"
    assert result["interaction"]["analysis_provenance"]["provider"] == (
        "fixture"
    )
    assert provider.calls == [("커피 한 잔을 마셨어", False)]


async def test_mcp_transcribes_local_voice_before_nutrition_analysis(
    mcp_client, call_tool, monkeypatch, store_factory
):
    provider = FakeAnalysis()
    monkeypatch.setattr(
        server_module,
        "create_vision_provider",
        lambda settings: provider,
    )
    monkeypatch.setattr(
        server_module,
        "create_nutrition_transcriber",
        lambda settings: FakeTranscriber(),
    )
    settings = server_module._active_settings()
    media_path = "media/2026/08/meal.m4a"
    target = settings.data_dir / media_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"voice")
    with store_factory() as session:
        register_storage_object(
            session,
            settings,
            relative_path=media_path,
            data_class="media",
            content_type="audio/mp4",
            size_bytes=5,
            observed_at=datetime(2026, 8, 6, 3, 30, tzinfo=UTC),
        )
        session.commit()
    arguments = {
        "operation_id": str(uuid.uuid4()),
        "intent": "log_consumed",
        "modality": "voice",
        "source_text": None,
        "observed_at": "2026-08-06T03:30:00Z",
        "media_path": media_path,
        "allow_remote_analysis": False,
    }
    result = await call_tool(
        mcp_client,
        "analyze_intake_capture",
        _trusted("analyze_intake_capture", arguments),
    )

    assert result["interaction"]["source_text"] == "커피 한 잔을 마셨어"
    assert (
        result["interaction"]["analysis_provenance"][
            "transcription_provider"
        ]
        == "fixture-whisper"
    )


async def test_text_capture_confirmation_and_search(
    mcp_client, call_tool
):
    capture_arguments = {
        "operation_id": str(uuid.uuid4()),
        "intent": "log_consumed",
        "modality": "text",
        "source_text": "I ate a chicken salad",
        "observed_at": "2026-08-06T03:30:00Z",
        "media_path": None,
        "nutrition_observation_id": None,
        "items": _items(),
    }
    captured = await call_tool(
        mcp_client,
        "capture_intake_interaction",
        _trusted("capture_intake_interaction", capture_arguments),
    )
    interaction_id = captured["interaction"]["interaction_id"]
    assert captured["interaction"]["is_confirmed_intake"] is False

    outcome_arguments = {
        "operation_id": str(uuid.uuid4()),
        "interaction_id": interaction_id,
        "status": "consumed",
        "consumed_at": "2026-08-06T03:30:00Z",
        "note": None,
    }
    confirmed = await call_tool(
        mcp_client,
        "confirm_intake_outcome",
        _trusted("confirm_intake_outcome", outcome_arguments),
    )
    assert confirmed["interaction"]["is_confirmed_intake"] is True

    searched = await call_tool(
        mcp_client,
        "search_intake_records",
        {
            "confirmed_only": True,
            "nutrient": "protein",
            "query": "chicken",
        },
    )
    assert searched["count"] == 1
    assert searched["records"][0]["interaction_id"] == interaction_id


async def test_prospective_decision_context_and_record(
    mcp_client, call_tool
):
    capture_arguments = {
        "operation_id": str(uuid.uuid4()),
        "intent": "ask_before_intake",
        "modality": "text",
        "source_text": "Can I drink this coffee now?",
        "observed_at": "2026-08-06T07:00:00Z",
        "media_path": None,
        "nutrition_observation_id": None,
        "items": _items("caffeine"),
    }
    captured = await call_tool(
        mcp_client,
        "capture_intake_interaction",
        _trusted("capture_intake_interaction", capture_arguments),
    )
    interaction_id = captured["interaction"]["interaction_id"]

    request_arguments = {
        "operation_id": str(uuid.uuid4()),
        "interaction_id": interaction_id,
        "scope": "caffeine_sleep",
        "question": "Can I drink it?",
        "intended_consumption_at": "2026-08-06T07:10:00Z",
        "compare_interaction_ids": [],
        "lookback_days": 14,
    }
    context = await call_tool(
        mcp_client,
        "request_intake_decision",
        _trusted("request_intake_decision", request_arguments),
    )
    assert context["candidate"]["is_confirmed_intake"] is False
    assert context["boundaries"]["candidate_is_not_consumed"] is True

    decision_arguments = {
        "operation_id": str(uuid.uuid4()),
        "request_id": context["request"]["request_id"],
        "status": "insufficient_data",
        "summary": "Known daily caffeine coverage is incomplete.",
        "evidence_event_ids": context["evidence_event_ids"],
        "limitations": ["captured records are not complete-day proof"],
        "recommendation": None,
    }
    decision = await call_tool(
        mcp_client,
        "record_intake_decision",
        _trusted("record_intake_decision", decision_arguments),
    )
    assert decision["decision_status"] == "insufficient_data"

    fetched = await call_tool(
        mcp_client,
        "search_intake_records",
        {"query": "coffee"},
    )
    assert fetched["records"][0]["latest_decision"]["status"] == "insufficient_data"
    assert fetched["records"][0]["is_confirmed_intake"] is False


async def test_capture_rejects_missing_trusted_owner_proof(mcp_client):
    with pytest.raises(ToolError, match="trusted_session_proof"):
        await mcp_client.call_tool(
            "capture_intake_interaction",
            {
                "operation_id": str(uuid.uuid4()),
                "intent": "log_consumed",
                "modality": "text",
                "source_text": "I ate lunch",
                "observed_at": "2026-08-06T03:30:00Z",
                "items": [],
            },
        )


async def test_analyze_rejects_missing_or_tampered_owner_proof(
    mcp_client, call_tool
):
    arguments = {
        "operation_id": str(uuid.uuid4()),
        "intent": "log_consumed",
        "modality": "text",
        "source_text": "I ate lunch",
        "observed_at": "2026-08-06T03:30:00Z",
        "media_path": None,
        "allow_remote_analysis": False,
    }
    with pytest.raises(ToolError, match="trusted_session_proof"):
        await mcp_client.call_tool("analyze_intake_capture", arguments)

    trusted = _trusted("analyze_intake_capture", arguments)
    trusted["source_text"] = "tampered text"
    with pytest.raises(ToolError, match="proof"):
        await call_tool(
            mcp_client,
            "analyze_intake_capture",
            trusted,
        )


async def test_mcp_capture_uses_shared_nutrition_limits(
    mcp_client, call_tool
):
    invalid_items = _items()
    invalid_items[0]["serving"]["unit"] = "x" * 33
    arguments = {
        "operation_id": str(uuid.uuid4()),
        "intent": "log_consumed",
        "modality": "text",
        "source_text": "I ate lunch",
        "observed_at": "2026-08-06T03:30:00Z",
        "media_path": None,
        "nutrition_observation_id": None,
        "items": invalid_items,
    }
    with pytest.raises(ToolError, match="estimate unit"):
        await call_tool(
            mcp_client,
            "capture_intake_interaction",
            _trusted("capture_intake_interaction", arguments),
        )
