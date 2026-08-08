"""MCP adapter tests for reusable food capture and decision context."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

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
from healthmes.store import WellnessEvent


@pytest.fixture(autouse=True)
def _use_iana_timezone(mcp_env):
    server_module.set_timezone("Asia/Seoul")


@pytest.fixture
def fixed_prospective_clock(monkeypatch):
    fixed_now = datetime(2026, 8, 6, 7, tzinfo=UTC)
    monkeypatch.setattr(server_module, "_utc_now", lambda: fixed_now)


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


async def test_mcp_automatically_analyzes_text_capture(mcp_client, call_tool, monkeypatch):
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
        arguments,
    )

    assert result["interaction"]["items"][0]["name"] == "coffee"
    assert result["interaction"]["analysis_provenance"]["provider"] == ("fixture")
    assert provider.calls == [("커피 한 잔을 마셨어", False)]


async def test_analyzed_capture_fingerprint_canonicalizes_operation_uuid(
    mcp_client,
    call_tool,
    monkeypatch,
) -> None:
    provider = FakeAnalysis()
    monkeypatch.setattr(
        server_module,
        "create_vision_provider",
        lambda settings: provider,
    )
    operation_id = uuid.UUID("99999999-9999-4999-8999-999999999999")
    first_arguments = {
        "operation_id": str(operation_id).upper(),
        "intent": "log_consumed",
        "modality": "text",
        "source_text": "커피 한 잔을 마셨어",
        "observed_at": "2026-08-06T03:30:00Z",
        "media_path": None,
        "allow_remote_analysis": False,
    }
    retry_arguments = {
        **first_arguments,
        "operation_id": str(operation_id),
    }

    first = await call_tool(
        mcp_client,
        "analyze_intake_capture",
        first_arguments,
    )
    retry = await call_tool(
        mcp_client,
        "analyze_intake_capture",
        retry_arguments,
    )

    assert first["interaction"]["interaction_id"] == str(operation_id)
    assert retry["interaction"]["interaction_id"] == str(operation_id)
    assert provider.calls == [("커피 한 잔을 마셨어", False)]


async def test_mcp_reviews_analyzed_candidate_before_decision(
    mcp_client,
    call_tool,
    confirm_nutrition_action,
    monkeypatch,
    fixed_prospective_clock,
):
    provider = FakeAnalysis()
    monkeypatch.setattr(
        server_module,
        "create_vision_provider",
        lambda settings: provider,
    )
    capture_arguments = {
        "operation_id": str(uuid.uuid4()),
        "intent": "ask_before_intake",
        "modality": "text",
        "source_text": "이 커피를 마셔도 될까?",
        "observed_at": "2026-08-06T03:30:00Z",
        "media_path": None,
        "allow_remote_analysis": False,
    }
    captured = await call_tool(
        mcp_client,
        "analyze_intake_capture",
        capture_arguments,
    )
    interaction_id = captured["interaction"]["interaction_id"]
    assert captured["interaction"]["items"][0]["nutrients"][0]["origin"] == "agent"

    review_arguments = {
        "operation_id": str(uuid.uuid4()),
        "interaction_id": interaction_id,
        "status": "corrected",
        "corrected_items": _items("caffeine"),
    }
    reviewed = await confirm_nutrition_action(
        mcp_client,
        "review_intake_interaction",
        review_arguments,
    )
    assert reviewed["interaction"]["latest_review"]["status"] == "corrected"
    assert reviewed["interaction"]["resolved_items"][0]["nutrients"][0]["origin"] == "user"

    request_arguments = {
        "operation_id": str(uuid.uuid4()),
        "interaction_id": interaction_id,
        "scope": "caffeine_sleep",
        "question": "지금 마셔도 될까?",
        "intended_consumption_at": "2026-08-06T16:00:00+09:00",
        "compare_interaction_ids": [],
        "lookback_days": 14,
    }
    context = await call_tool(
        mcp_client,
        "request_intake_decision",
        request_arguments,
    )
    assert context["candidate"]["latest_review"]["status"] == "corrected"
    assert context["candidate"]["resolved_items"][0]["nutrients"][0]["amount"]["exact"] == 120


async def test_mcp_rejects_naive_caffeine_candidate_time(
    mcp_client,
    call_tool,
):
    capture_arguments = {
        "operation_id": str(uuid.uuid4()),
        "intent": "ask_before_intake",
        "modality": "text",
        "source_text": "이 커피를 마셔도 될까?",
        "observed_at": "2026-08-06T03:30:00Z",
        "media_path": None,
        "nutrition_observation_id": None,
        "items": _items("caffeine"),
    }
    captured = await call_tool(
        mcp_client,
        "capture_intake_interaction",
        capture_arguments,
    )
    request_arguments = {
        "operation_id": str(uuid.uuid4()),
        "interaction_id": captured["interaction"]["interaction_id"],
        "scope": "caffeine_sleep",
        "question": "지금 마셔도 될까?",
        "intended_consumption_at": "2026-08-06T07:00:00",
        "compare_interaction_ids": [],
        "lookback_days": 14,
    }

    with pytest.raises(ToolError, match="explicit UTC offset"):
        await call_tool(
            mcp_client,
            "request_intake_decision",
            request_arguments,
        )


async def test_mcp_transcribes_local_voice_before_nutrition_analysis(
    mcp_client,
    call_tool,
    confirm_nutrition_action,
    monkeypatch,
    store_factory,
    fixed_prospective_clock,
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
        "intent": "ask_before_intake",
        "modality": "voice",
        "source_text": None,
        "observed_at": "2026-08-06T03:30:00Z",
        "media_path": media_path,
        "allow_remote_analysis": False,
    }
    result = await call_tool(
        mcp_client,
        "analyze_intake_capture",
        arguments,
    )

    assert result["interaction"]["source_text"] == "커피 한 잔을 마셨어"
    assert (
        result["interaction"]["analysis_provenance"]["transcription_provider"] == "fixture-whisper"
    )
    interaction_id = result["interaction"]["interaction_id"]
    review_arguments = {
        "operation_id": str(uuid.uuid4()),
        "interaction_id": interaction_id,
        "status": "corrected",
        "corrected_items": _items("caffeine"),
    }
    await confirm_nutrition_action(
        mcp_client,
        "review_intake_interaction",
        review_arguments,
    )
    request_arguments = {
        "operation_id": str(uuid.uuid4()),
        "interaction_id": interaction_id,
        "scope": "caffeine_sleep",
        "question": "지금 마셔도 될까?",
        "intended_consumption_at": "2026-08-06T16:00:00+09:00",
        "compare_interaction_ids": [],
        "lookback_days": 14,
    }
    context = await call_tool(
        mcp_client,
        "request_intake_decision",
        request_arguments,
    )
    assert context["candidate"]["modality"] == "voice"
    assert context["candidate"]["resolved_items"][0]["nutrients"][0]["amount"]["exact"] == 120


async def test_text_capture_confirmation_and_search(
    mcp_client,
    call_tool,
    confirm_nutrition_action,
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
        capture_arguments,
    )
    interaction_id = captured["interaction"]["interaction_id"]
    assert captured["interaction"]["is_confirmed_intake"] is False

    outcome_arguments = {
        "operation_id": str(uuid.uuid4()),
        "interaction_id": interaction_id,
        "status": "consumed",
        "consumed_at": "2026-08-06T03:30:00Z",
        "corrected_items": [],
        "note": None,
    }
    confirmed = await confirm_nutrition_action(
        mcp_client,
        "confirm_intake_outcome",
        outcome_arguments,
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


async def test_intake_write_fingerprints_canonicalize_uuid_spelling(
    mcp_client,
    call_tool,
    confirm_nutrition_action,
) -> None:
    interaction_uuid = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    first_capture = {
        "operation_id": str(interaction_uuid).upper(),
        "intent": "ask_before_intake",
        "modality": "text",
        "source_text": "Can I eat this?",
        "observed_at": "2026-08-06T07:00:00Z",
        "media_path": None,
        "nutrition_observation_id": None,
        "items": _items(),
    }
    retry_capture = {
        **first_capture,
        "operation_id": str(interaction_uuid),
    }

    first = await call_tool(
        mcp_client,
        "capture_intake_interaction",
        first_capture,
    )
    retry = await call_tool(
        mcp_client,
        "capture_intake_interaction",
        retry_capture,
    )

    assert first["interaction"]["interaction_id"] == str(interaction_uuid)
    assert retry["interaction"]["interaction_id"] == str(interaction_uuid)

    review_uuid = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    first_review = {
        "operation_id": str(review_uuid).upper(),
        "interaction_id": str(interaction_uuid).upper(),
        "status": "confirmed",
        "corrected_items": [],
    }
    retry_review = {
        **first_review,
        "operation_id": str(review_uuid),
        "interaction_id": str(interaction_uuid),
    }
    await confirm_nutrition_action(
        mcp_client,
        "review_intake_interaction",
        first_review,
    )
    reviewed = await confirm_nutrition_action(
        mcp_client,
        "review_intake_interaction",
        retry_review,
    )
    assert reviewed["interaction"]["latest_review"]["review_id"] == str(review_uuid)

    outcome_uuid = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    first_outcome = {
        "operation_id": str(outcome_uuid).upper(),
        "interaction_id": str(interaction_uuid).upper(),
        "status": "not_consumed",
        "consumed_at": None,
        "corrected_items": [],
        "note": None,
    }
    retry_outcome = {
        **first_outcome,
        "operation_id": str(outcome_uuid),
        "interaction_id": str(interaction_uuid),
    }
    await confirm_nutrition_action(
        mcp_client,
        "confirm_intake_outcome",
        first_outcome,
    )
    confirmed = await confirm_nutrition_action(
        mcp_client,
        "confirm_intake_outcome",
        retry_outcome,
    )
    assert confirmed["interaction"]["latest_outcome"]["outcome_id"] == str(outcome_uuid)


async def test_decision_write_fingerprints_canonicalize_uuid_spelling(
    mcp_client,
    call_tool,
    fixed_prospective_clock,
) -> None:
    interaction_uuid = uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
    capture_arguments = {
        "operation_id": str(interaction_uuid),
        "intent": "ask_before_intake",
        "modality": "text",
        "source_text": "Can I drink this coffee now?",
        "observed_at": "2026-08-06T07:00:00Z",
        "media_path": None,
        "nutrition_observation_id": None,
        "items": _items("caffeine"),
    }
    await call_tool(
        mcp_client,
        "capture_intake_interaction",
        capture_arguments,
    )

    request_uuid = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
    first_request = {
        "operation_id": str(request_uuid).upper(),
        "interaction_id": str(interaction_uuid).upper(),
        "scope": "caffeine_sleep",
        "question": "Can I drink it?",
        "intended_consumption_at": "2026-08-06T16:10:00+09:00",
        "compare_interaction_ids": [],
        "lookback_days": 14,
    }
    retry_request = {
        **first_request,
        "operation_id": str(request_uuid),
        "interaction_id": str(interaction_uuid),
    }
    context = await call_tool(
        mcp_client,
        "request_intake_decision",
        first_request,
    )
    retried_context = await call_tool(
        mcp_client,
        "request_intake_decision",
        retry_request,
    )
    assert context["request"]["request_id"] == str(request_uuid)
    assert retried_context["request"]["request_id"] == str(request_uuid)

    decision_uuid = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
    first_decision = {
        "operation_id": str(decision_uuid).upper(),
        "request_id": str(request_uuid).upper(),
        "status": "insufficient_data",
        "summary": "Known daily caffeine coverage is incomplete.",
        "evidence_event_ids": [value.upper() for value in context["evidence_event_ids"]],
        "limitations": ["complete-day intake is unavailable"],
        "recommendation": None,
    }
    retry_decision = {
        **first_decision,
        "operation_id": str(decision_uuid),
        "request_id": str(request_uuid),
        "evidence_event_ids": context["evidence_event_ids"],
    }
    decision = await call_tool(
        mcp_client,
        "record_intake_decision",
        first_decision,
    )
    retried_decision = await call_tool(
        mcp_client,
        "record_intake_decision",
        retry_decision,
    )
    assert decision["decision_id"] == str(decision_uuid)
    assert retried_decision["decision_id"] == str(decision_uuid)


async def test_prospective_decision_context_and_record(
    mcp_client,
    call_tool,
    fixed_prospective_clock,
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
        capture_arguments,
    )
    interaction_id = captured["interaction"]["interaction_id"]

    request_arguments = {
        "operation_id": str(uuid.uuid4()),
        "interaction_id": interaction_id,
        "scope": "caffeine_sleep",
        "question": "Can I drink it?",
        "intended_consumption_at": "2026-08-06T16:10:00+09:00",
        "compare_interaction_ids": [],
        "lookback_days": 14,
    }
    context = await call_tool(
        mcp_client,
        "request_intake_decision",
        request_arguments,
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
        decision_arguments,
    )
    assert decision["decision_status"] == "insufficient_data"

    fetched = await call_tool(
        mcp_client,
        "search_intake_records",
        {"query": "coffee"},
    )
    assert fetched["records"][0]["latest_decision"]["status"] == "insufficient_data"
    assert fetched["records"][0]["is_confirmed_intake"] is False


async def test_mcp_read_tools_fail_closed_on_invalid_transition_chain(
    mcp_client,
    call_tool,
    store_factory,
    fixed_prospective_clock,
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
        capture_arguments,
    )
    interaction_id = captured["interaction"]["interaction_id"]
    request_arguments = {
        "operation_id": str(uuid.uuid4()),
        "interaction_id": interaction_id,
        "scope": "caffeine_sleep",
        "question": "Can I drink it?",
        "intended_consumption_at": "2026-08-06T16:10:00+09:00",
        "compare_interaction_ids": [],
        "lookback_days": 14,
    }
    context = await call_tool(
        mcp_client,
        "request_intake_decision",
        request_arguments,
    )
    request_id = context["request"]["request_id"]
    recorded_at = datetime(2026, 8, 6, 7, tzinfo=UTC)

    def transition(
        revision: int,
        mutation_kind: str,
        mutation_status: str,
    ) -> WellnessEvent:
        event_at = recorded_at + timedelta(seconds=revision)
        return WellnessEvent(
            event_type="nutrition.interaction-transition.v1",
            schema_version=1,
            observed_at=event_at,
            recorded_at=event_at,
            timezone="Asia/Seoul",
            source_provider="nutrition-interaction-transition",
            source_device=None,
            source_record_id=f"{interaction_id}:{revision}",
            capture_method="system",
            quality_flags=None,
            confidence=None,
            sensitivity="wellness",
            consent_scope="personal",
            retention_policy_id=None,
            expires_at=None,
            payload={
                "interaction_id": interaction_id,
                "revision": revision,
                "mutation_kind": mutation_kind,
                "operation_id": str(uuid.uuid4()),
                "mutation_status": mutation_status,
            },
            derived_from={"interaction_id": interaction_id},
        )

    with store_factory() as session:
        session.add_all(
            (
                transition(1, "outcome", "not_consumed"),
                transition(2, "review", "confirmed"),
            )
        )
        session.commit()

    with pytest.raises(
        ToolError,
        match="invalid interaction transition chain",
    ):
        await call_tool(
            mcp_client,
            "search_intake_records",
            {"query": "coffee"},
        )
    immutable_context = await call_tool(
        mcp_client,
        "get_intake_decision_context",
        {"request_id": request_id},
    )
    assert immutable_context["request"]["request_id"] == request_id


async def test_capture_observation_does_not_require_telegram_proof(
    mcp_client,
    call_tool,
):
    result = await call_tool(
        mcp_client,
        "capture_intake_interaction",
        {
            "operation_id": str(uuid.uuid4()),
            "intent": "log_consumed",
            "modality": "text",
            "source_text": "I ate lunch",
            "observed_at": "2026-08-06T03:30:00Z",
            "items": _items(),
        },
    )

    assert result["interaction"]["is_confirmed_intake"] is False


async def test_analyze_observation_does_not_require_telegram_proof(
    mcp_client,
    call_tool,
    monkeypatch,
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
        "source_text": "I ate lunch",
        "observed_at": "2026-08-06T03:30:00Z",
        "media_path": None,
        "allow_remote_analysis": False,
    }
    result = await call_tool(
        mcp_client,
        "analyze_intake_capture",
        arguments,
    )

    assert result["interaction"]["items"][0]["name"] == "coffee"


async def test_mcp_capture_uses_shared_nutrition_limits(mcp_client, call_tool):
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
            arguments,
        )
