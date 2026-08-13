"""Device-neutral intake interaction engine integration tests."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from time import sleep
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import healthmes.nutrition.intake_service as intake_service_module
from healthmes.api.intake_interactions import AnalyzeInteractionInput
from healthmes.nutrition.contracts import (
    Confidence,
    EstimateKind,
    IntakeType,
    ObservationStatus,
)
from healthmes.nutrition.intake_contracts import (
    CaptureModality,
    IntakeIntent,
)
from healthmes.nutrition.intake_service import (
    IntakeAnalysisInProgress,
    create_analyzed_interaction,
    operation_fingerprint,
)
from healthmes.nutrition.query import known_caffeine_for_day
from healthmes.nutrition.schema import VLMEstimate, VLMExtraction, VLMItem
from healthmes.nutrition.transcription import TranscriptionResult
from healthmes.nutrition.vision import VisionUnavailable
from healthmes.storage import run_storage_maintenance
from healthmes.store import RetentionPolicy, StorageObject, WellnessEvent

JPEG = b"\xff\xd8\xff\xe0synthetic-coffee"


def _recent_utc() -> str:
    return (datetime.now(UTC) - timedelta(minutes=1)).isoformat()


class FakeVision:
    provider_name = "fixture"
    model = "fixture-v1"
    model_digest = "sha256:fixture"

    def analyze(self, image_path, *, allow_remote):
        return VLMExtraction(
            status=ObservationStatus.USABLE,
            confidence=Confidence.HIGH,
            warnings=[],
            items=[
                VLMItem(
                    intake_type=IntakeType.BEVERAGE,
                    name_candidates=["bottled coffee"],
                    category="coffee",
                    serving=VLMEstimate(
                        kind=EstimateKind.EXACT,
                        unit="ml",
                        exact=355,
                        evidence_text="355 mL",
                        estimation_basis="visible_label",
                    ),
                    caffeine=VLMEstimate(
                        kind=EstimateKind.EXACT,
                        unit="mg",
                        exact=180,
                        evidence_text="Caffeine 180 mg",
                        estimation_basis="visible_label",
                    ),
                    confidence=Confidence.HIGH,
                )
            ],
        )


class FakeAnalysis(FakeVision):
    def __init__(self):
        self.calls = []

    def analyze_text(self, text, *, allow_remote):
        self.calls.append((text, allow_remote))
        return VLMExtraction(
            status=ObservationStatus.USABLE,
            confidence=Confidence.MEDIUM,
            warnings=["owner confirmation recommended"],
            items=[
                VLMItem(
                    intake_type=IntakeType.FOOD,
                    name_candidates=["banana and milk"],
                    category="breakfast",
                    serving=VLMEstimate(
                        kind=EstimateKind.RANGE,
                        unit="g",
                        minimum=250,
                        maximum=400,
                        estimation_basis="owner_portion_description",
                    ),
                    caffeine=VLMEstimate(
                        kind=EstimateKind.UNKNOWN,
                        unit="mg",
                    ),
                    confidence=Confidence.MEDIUM,
                )
            ],
        )


class FakeTranscriber:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio_path):
        self.calls.append(audio_path)
        return TranscriptionResult(
            text="아침에 바나나와 우유를 먹었어",
            provider="fixture-whisper",
            model="fixture-small",
        )


class BlockingAnalysis(FakeAnalysis):
    def __init__(self):
        super().__init__()
        self.entered = Event()
        self.release = Event()

    def analyze_text(self, text, *, allow_remote):
        self.entered.set()
        assert self.release.wait(timeout=5)
        return super().analyze_text(text, allow_remote=allow_remote)


class FailingOnceAnalysis(FakeAnalysis):
    def analyze_text(self, text, *, allow_remote):
        if not self.calls:
            self.calls.append((text, allow_remote))
            raise RuntimeError("fixture provider failed")
        return super().analyze_text(text, allow_remote=allow_remote)


class UnavailableAnalysis(FakeAnalysis):
    def analyze_text(self, text, *, allow_remote):
        raise VisionUnavailable("fixture unavailable")


class SlowAnalysis(FakeAnalysis):
    def analyze_text(self, text, *, allow_remote):
        sleep(0.2)
        return super().analyze_text(text, allow_remote=allow_remote)


def _estimate(
    exact: float, unit: str, *, basis: str = "owner_statement"
) -> dict[str, object]:
    return {
        "kind": "exact",
        "unit": unit,
        "exact": exact,
        "estimation_basis": basis,
    }


def _text_interaction(**overrides) -> dict[str, object]:
    body: dict[str, object] = {
        "operation_id": str(uuid.uuid4()),
        "intent": "log_consumed",
        "modality": "text",
        "observed_at": "2026-08-06T12:30:00+09:00",
        "timezone": "Asia/Seoul",
        "source": "ios-device",
        "source_text": "닭가슴살 샐러드와 라테를 먹었다",
        "items": [
            {
                "name": "닭가슴살 샐러드",
                "intake_type": "food",
                "serving": _estimate(1, "serving"),
                "nutrients": [
                    {
                        "nutrient": "protein",
                        "amount": _estimate(30, "g"),
                        "confidence": "high",
                        "origin": "user",
                        "evidence_text": "단백질 30g",
                    }
                ],
                "confidence": "high",
            }
        ],
    }
    body.update(overrides)
    return body


def _upload(client, content: bytes, content_type: str, name: str) -> str:
    response = client.post(
        "/v1/media",
        files={"file": (name, content, content_type)},
    )
    assert response.status_code == 201
    return response.json()["media_path"]


def _photo_observation(client) -> str:
    client.app.state.nutrition_vision_provider = FakeVision()
    media_path = _upload(client, JPEG, "image/jpeg", "coffee.jpg")
    response = client.post(
        "/v1/nutrition-observations/analyze",
        json={
            "media_path": media_path,
            "captured_at": _recent_utc(),
            "timezone": "UTC",
            "source": "ios-photo",
            "location": None,
            "metadata_provenance": {
                "captured_at": "app",
                "timezone": "app",
                "location": "unavailable",
            },
            "allow_remote_vision": False,
        },
    )
    assert response.status_code == 201
    return response.json()["observation_id"]


def _reviewed_nutrients(*, caffeine: float = 95) -> list[dict[str, object]]:
    values = {
        "energy": (80, "kcal"),
        "protein": (4, "g"),
        "carbohydrate": (12, "g"),
        "fat": (2, "g"),
        "fiber": (0, "g"),
        "sugar": (10, "g"),
        "sodium": (75, "mg"),
        "caffeine": (caffeine, "mg"),
    }
    return [
        {
            "nutrient": nutrient,
            "amount": _estimate(amount, unit, basis="owner_correction"),
            "confidence": "high",
        }
        for nutrient, (amount, unit) in values.items()
    ]


def test_text_capture_and_consumption_are_separate_events(client, session):
    created = client.post(
        "/v1/intake-interactions", json=_text_interaction()
    )
    assert created.status_code == 201
    interaction = created.json()
    interaction_id = interaction["interaction_id"]
    assert interaction["is_confirmed_intake"] is False
    assert interaction["items"][0]["nutrients"][0]["nutrient"] == "protein"

    confirmed = client.post(
        f"/v1/intake-interactions/{interaction_id}/outcomes",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "consumed",
            "source": "ios-device",
            "consumed_at": "2026-08-06T12:30:00+09:00",
        },
    )
    assert confirmed.status_code == 201
    assert confirmed.json()["is_confirmed_intake"] is True

    event_types = list(
        session.scalars(
            select(WellnessEvent.event_type).order_by(WellnessEvent.created_at)
        )
    )
    assert event_types == [
        "nutrition.interaction.v1",
        "nutrition.raw-capture.v1",
        "nutrition.operation.v1",
        "nutrition.intake-outcome.v1",
    ]
    interaction_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.interaction.v1"
        )
    )
    assert interaction_event is not None
    interaction_policy = session.get(
        RetentionPolicy, interaction_event.retention_policy_id
    )
    assert interaction_policy is not None
    assert interaction_policy.data_class == "nutrition_observation"
    assert interaction_event.expires_at is not None
    raw_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.raw-capture.v1"
        )
    )
    assert raw_event is not None
    raw_policy = session.get(RetentionPolicy, raw_event.retention_policy_id)
    assert raw_policy is not None
    assert raw_policy.data_class == "nutrition_raw_capture"
    assert raw_event.payload["source_text"] == (
        "닭가슴살 샐러드와 라테를 먹었다"
    )
    assert interaction_event.payload["source_text"] is None
    outcome_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.intake-outcome.v1"
        )
    )
    assert outcome_event is not None
    snapshot = outcome_event.payload["intake_snapshot"]
    assert snapshot["items"][0]["name"] == "닭가슴살 샐러드"
    assert snapshot["items"][0]["nutrients"][0]["evidence_text"] is None
    assert (
        snapshot["items"][0]["nutrients"][0]["amount"]["evidence_text"]
        is None
    )
    assert outcome_event.payload["note"] is None
    assert "source_text" not in snapshot
    assert "media_path" not in snapshot

    searched = client.get(
        "/v1/intake-interactions",
        params={
            "confirmed_only": "true",
            "nutrient": "protein",
            "query": "샐러드",
        },
    )
    assert searched.status_code == 200
    assert searched.json()["count"] == 1
    assert searched.json()["records"][0]["interaction_id"] == interaction_id


def test_prospective_candidate_builds_context_without_becoming_intake(
    client, session
):
    created = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            intent="ask_before_intake",
            source_text="오후에 라테를 마셔도 될까?",
            items=[
                {
                    "name": "라테",
                    "intake_type": "beverage",
                    "serving": _estimate(1, "cup"),
                    "nutrients": [
                        {
                            "nutrient": "caffeine",
                            "amount": {
                                "kind": "range",
                                "unit": "mg",
                                "minimum": 80,
                                "maximum": 140,
                                "estimation_basis": "agent_estimate",
                            },
                            "confidence": "medium",
                            "origin": "agent",
                        }
                    ],
                    "confidence": "medium",
                }
            ],
        ),
    ).json()
    interaction_id = created["interaction_id"]
    assert created["is_confirmed_intake"] is False

    requested = client.post(
        f"/v1/intake-interactions/{interaction_id}/decision-requests",
        json={
            "operation_id": str(uuid.uuid4()),
            "scope": "caffeine_sleep",
            "source": "android-device",
            "question": "지금 마셔도 될까?",
            "intended_consumption_at": "2026-08-06T16:00:00+09:00",
        },
    )
    assert requested.status_code == 201
    request_id = requested.json()["request_id"]
    session.expire_all()
    candidate_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.interaction.v1"
        )
    )
    assert candidate_event is not None
    candidate_policy = session.get(
        RetentionPolicy, candidate_event.retention_policy_id
    )
    assert candidate_policy is not None
    assert candidate_policy.data_class == "nutrition_observation"
    assert candidate_event.expires_at is not None

    context = client.get(
        f"/v1/intake-interactions/decision-requests/{request_id}/context"
    )
    assert context.status_code == 200
    body = context.json()
    assert body["candidate"]["interaction_id"] == interaction_id
    assert body["candidate"]["is_confirmed_intake"] is False
    assert body["candidate"]["source_text"] is None
    assert body["candidate"]["media_path"] is None
    assert body["boundaries"]["candidate_is_not_consumed"] is True
    assert body["history_window"]["coverage"] == "captured_records_only"
    assert body["history_window"]["lookback_days"] == 14
    assert body["evidence_event_ids"]
    assert len(body["evidence_event_ids"]) >= 2
    assert body["specialized_evidence"]["caffeine"]["status"] == "incomplete"
    assert body["boundaries"]["caffeine_total_intake_complete"] is False

    rejected = client.post(
        f"/v1/intake-interactions/{interaction_id}/decisions",
        json={
            "operation_id": str(uuid.uuid4()),
            "request_id": request_id,
            "status": "proposal",
            "source": "generic-nutrition-agent",
            "summary": "마셔도 됩니다",
            "evidence_event_ids": body["evidence_event_ids"],
            "recommendation": {"drink_now": True},
        },
    )
    assert rejected.status_code == 422
    assert "specialized caffeine policy" in rejected.text

    recorded = client.post(
        f"/v1/intake-interactions/{interaction_id}/decisions",
        json={
            "operation_id": str(uuid.uuid4()),
            "request_id": request_id,
            "status": "insufficient_data",
            "source": "healthmes-caffeine",
            "summary": "카페인 판단 엔진 결과",
            "evidence_event_ids": body["evidence_event_ids"],
            "limitations": ["사진 또는 사용자 제공량은 추정값일 수 있음"],
            "recommendation": {"drink_now": True},
        },
    )
    assert recorded.status_code == 201
    fetched = client.get(f"/v1/intake-interactions/{interaction_id}").json()
    assert fetched["latest_decision"]["status"] == "insufficient_data"
    assert "cannot make an actionable" in fetched["latest_decision"]["summary"]
    assert fetched["latest_decision"]["recommendation"] is None
    assert fetched["is_confirmed_intake"] is False


def test_caffeine_context_includes_confirmed_text_outcome_evidence(
    client, session
):
    consumed = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            source_text="카페인 150mg 커피를 마셨어",
            items=[
                {
                    "name": "커피",
                    "intake_type": "beverage",
                    "serving": _estimate(1, "cup"),
                    "nutrients": [
                        {
                            "nutrient": "caffeine",
                            "amount": _estimate(150, "mg"),
                            "confidence": "high",
                            "origin": "user",
                        }
                    ],
                    "confidence": "high",
                }
            ],
        ),
    )
    assert consumed.status_code == 201
    interaction_id = consumed.json()["interaction_id"]
    outcome = client.post(
        f"/v1/intake-interactions/{interaction_id}/outcomes",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "consumed",
            "source": "ios-device",
            "consumed_at": "2026-08-06T12:30:00+09:00",
        },
    )
    assert outcome.status_code == 201
    daily = client.post(
        "/v1/nutrition-observations/daily-confirmations",
        json={
            "local_date": "2026-08-06",
            "timezone": "Asia/Seoul",
            "observation_ids": [],
            "outcome_ids": [
                outcome.json()["latest_outcome"]["outcome_id"]
            ],
            "total_intake_complete": True,
            "source": "desktop-web",
        },
    )
    assert daily.status_code == 201

    candidate = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            intent="ask_before_intake",
            source_text="오후 커피를 마셔도 될까?",
            items=[
                {
                    "name": "커피",
                    "intake_type": "beverage",
                    "serving": _estimate(1, "cup"),
                    "nutrients": [
                        {
                            "nutrient": "caffeine",
                            "amount": _estimate(100, "mg"),
                            "confidence": "high",
                            "origin": "user",
                        }
                    ],
                    "confidence": "high",
                }
            ],
        ),
    )
    assert candidate.status_code == 201
    requested = client.post(
        f"/v1/intake-interactions/{candidate.json()['interaction_id']}"
        "/decision-requests",
        json={
            "operation_id": str(uuid.uuid4()),
            "scope": "caffeine_sleep",
            "source": "ios-device",
            "intended_consumption_at": "2026-08-06T16:00:00+09:00",
        },
    )
    assert requested.status_code == 201
    context = client.get(
        "/v1/intake-interactions/decision-requests/"
        f"{requested.json()['request_id']}/context"
    )
    assert context.status_code == 200
    caffeine = context.json()["specialized_evidence"]["caffeine"]
    assert caffeine["status"] == "known"
    assert caffeine["confirmed_caffeine_mg"] == 150
    outcome_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.intake-outcome.v1"
        )
    )
    assert outcome_event is not None
    assert str(outcome_event.id) in context.json()["evidence_event_ids"]


def test_fixed_offset_caffeine_request_reaches_cross_domain_resolver(
    client,
) -> None:
    timezone = "UTC+09:00"
    consumed = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            timezone=timezone,
            source_text="카페인 80mg 커피를 마셨어",
            items=[
                {
                    "name": "아침 커피",
                    "intake_type": "beverage",
                    "serving": _estimate(1, "cup"),
                    "nutrients": [
                        {
                            "nutrient": "caffeine",
                            "amount": _estimate(80, "mg"),
                            "confidence": "high",
                            "origin": "user",
                        }
                    ],
                    "confidence": "high",
                }
            ],
        ),
    )
    assert consumed.status_code == 201
    consumed_id = consumed.json()["interaction_id"]
    outcome = client.post(
        f"/v1/intake-interactions/{consumed_id}/outcomes",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "consumed",
            "source": "ios-device",
            "consumed_at": "2026-08-06T12:30:00+09:00",
        },
    )
    assert outcome.status_code == 201

    daily = client.post(
        "/v1/nutrition-observations/daily-confirmations",
        json={
            "local_date": "2026-08-06",
            "timezone": timezone,
            "observation_ids": [],
            "outcome_ids": [
                outcome.json()["latest_outcome"]["outcome_id"]
            ],
            "total_intake_complete": True,
            "source": "desktop-web",
        },
    )
    assert daily.status_code == 201

    candidate = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            intent="ask_before_intake",
            timezone=timezone,
            source_text="오후 커피를 마셔도 될까?",
            items=[
                {
                    "name": "오후 커피",
                    "intake_type": "beverage",
                    "serving": _estimate(1, "cup"),
                    "nutrients": [
                        {
                            "nutrient": "caffeine",
                            "amount": _estimate(100, "mg"),
                            "confidence": "high",
                            "origin": "user",
                        }
                    ],
                    "confidence": "high",
                }
            ],
        ),
    )
    assert candidate.status_code == 201
    requested = client.post(
        f"/v1/intake-interactions/{candidate.json()['interaction_id']}"
        "/decision-requests",
        json={
            "operation_id": str(uuid.uuid4()),
            "scope": "caffeine_sleep",
            "source": "ios-device",
            "intended_consumption_at": "2026-08-06T16:00:00+09:00",
        },
    )
    assert requested.status_code == 201
    request_id = requested.json()["request_id"]

    context = client.get(
        f"/v1/intake-interactions/decision-requests/{request_id}/context"
    )
    assert context.status_code == 200
    assert (
        context.json()["specialized_evidence"]["caffeine"][
            "confirmed_caffeine_mg"
        ]
        == 80
    )
    assert (
        context.json()["specialized_evidence"]["caffeine"]["timezone"]
        == timezone
    )

    resolved = client.post(
        "/v1/wellness-context/resolve",
        json={
            "question_kind": "caffeine_for_focus",
            "date": "2026-08-06",
            "start": "2026-08-06T01:00:00Z",
            "end": "2026-08-06T02:00:00Z",
            "timezone": timezone,
            "nutrition_request_id": request_id,
        },
    )

    assert resolved.status_code == 200
    nutrition = resolved.json()["contexts"]["nutrition"]
    assert nutrition["status"] == "ok"
    assert nutrition["candidate_ledger_complete"] is True
    assert nutrition["decision_ready"] is False
    assert nutrition["context"]["request"]["request_id"] == request_id
    assert (
        nutrition["context"]["specialized_evidence"]["caffeine"][
            "confirmed_caffeine_mg"
        ]
        == 80
    )


def test_high_risk_scope_cannot_store_wellness_proposal(client):
    interaction = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            intent="ask_before_intake",
            source_text="이 음식이 약과 같이 괜찮을까?",
        ),
    ).json()
    interaction_id = interaction["interaction_id"]
    request = client.post(
        f"/v1/intake-interactions/{interaction_id}/decision-requests",
        json={
            "operation_id": str(uuid.uuid4()),
            "scope": "medication_interaction",
            "source": "web-device",
        },
    ).json()
    context = client.get(
        f"/v1/intake-interactions/decision-requests/{request['request_id']}/context"
    ).json()

    rejected = client.post(
        f"/v1/intake-interactions/{interaction_id}/decisions",
        json={
            "operation_id": str(uuid.uuid4()),
            "request_id": request["request_id"],
            "status": "proposal",
            "source": "generic-agent",
            "summary": "복용해도 됩니다",
            "evidence_event_ids": context["evidence_event_ids"],
        },
    )
    assert rejected.status_code == 422
    assert "must remain unsupported" in rejected.text

    unsupported = client.post(
        f"/v1/intake-interactions/{interaction_id}/decisions",
        json={
            "operation_id": str(uuid.uuid4()),
            "request_id": request["request_id"],
            "status": "unsupported",
            "source": "generic-agent",
            "summary": "복용해도 안전합니다",
            "evidence_event_ids": context["evidence_event_ids"],
            "limitations": ["안전하다고 단정하는 잘못된 문구"],
            "recommendation": {"take_with_medication": True},
        },
    )
    assert unsupported.status_code == 201
    fetched = client.get(f"/v1/intake-interactions/{interaction_id}").json()
    assert "does not provide" in fetched["latest_decision"]["summary"]
    assert fetched["latest_decision"]["recommendation"] is None


def test_voice_capture_requires_local_transcript_and_indexes_audio(
    client, session
):
    media_path = _upload(client, b"fake-m4a", "audio/m4a", "meal.m4a")
    missing = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            modality="voice",
            source_text=None,
            media_path=media_path,
            items=[],
        ),
    )
    assert missing.status_code == 422

    created = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            modality="voice",
            source_text="아침에 바나나와 우유를 먹었어",
            media_path=media_path,
            items=[],
        ),
    )
    assert created.status_code == 201
    assert created.json()["modality"] == "voice"
    obj = session.scalar(
        select(StorageObject).where(StorageObject.relative_path == media_path)
    )
    assert obj is not None
    assert obj.data_class == "nutrition_media"


def test_uploaded_media_cannot_be_reused_by_another_capture(client):
    media_path = _upload(client, b"fake-m4a", "audio/m4a", "meal.m4a")
    observed_at = _recent_utc()
    first = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            modality="voice",
            source_text="아침에 바나나와 우유를 먹었어",
            media_path=media_path,
            items=[],
            observed_at=observed_at,
            timezone="UTC",
        ),
    )
    assert first.status_code == 201

    second = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            modality="voice",
            source_text="같은 음성을 다시 연결",
            media_path=media_path,
            items=[],
            observed_at=observed_at,
            timezone="UTC",
        ),
    )

    assert second.status_code == 422
    assert "already belongs to another capture" in second.text


def test_free_text_is_automatically_analyzed_and_retry_is_idempotent(
    client, session
):
    provider = FakeAnalysis()
    client.app.state.nutrition_analysis_provider = provider
    operation_id = str(uuid.uuid4())
    body = {
        "operation_id": operation_id,
        "intent": "log_consumed",
        "modality": "text",
        "observed_at": "2026-08-06T08:30:00+09:00",
        "timezone": "Asia/Seoul",
        "source": "ios-device",
        "source_text": "아침에 바나나와 우유를 먹었어",
        "allow_remote_analysis": False,
    }
    created = client.post("/v1/intake-interactions/analyze", json=body)
    assert created.status_code == 201
    payload = created.json()
    assert payload["items"][0]["name"] == "banana and milk"
    assert payload["items"][0]["nutrients"][0]["origin"] == "agent"
    assert payload["warnings"] == ["owner confirmation recommended"]
    assert payload["analysis_provenance"] == {
        "provider": "fixture",
        "model": "fixture-v1",
        "model_digest": "sha256:fixture",
        "prompt_version": "text-intake-v1",
        "schema_version": "nutrition-observation-v2",
        "analyzed_at": payload["recorded_at"],
        "transcription_provider": None,
        "transcription_model": None,
    }
    assert provider.calls == [
        ("아침에 바나나와 우유를 먹었어", False)
    ]

    retried = client.post("/v1/intake-interactions/analyze", json=body)
    assert retried.status_code == 201
    assert retried.json()["interaction_id"] == operation_id
    assert len(provider.calls) == 1

    conflict = client.post(
        "/v1/intake-interactions/analyze",
        json={**body, "source_text": "점심에 샌드위치를 먹었어"},
    )
    assert conflict.status_code == 409
    assert len(provider.calls) == 1

    event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.interaction.v1",
            WellnessEvent.source_record_id == operation_id,
        )
    )
    assert event is not None
    assert event.capture_method == "text"


def test_concurrent_automatic_retry_does_not_duplicate_model_call(client):
    provider = BlockingAnalysis()
    client.app.state.nutrition_analysis_provider = provider
    body = {
        "operation_id": str(uuid.uuid4()),
        "intent": "log_consumed",
        "modality": "text",
        "observed_at": "2026-08-06T08:30:00+09:00",
        "timezone": "Asia/Seoul",
        "source": "ios-device",
        "source_text": "아침에 바나나와 우유를 먹었어",
        "allow_remote_analysis": False,
    }
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(
            client.post,
            "/v1/intake-interactions/analyze",
            json=body,
        )
        assert provider.entered.wait(timeout=5)
        duplicate = client.post(
            "/v1/intake-interactions/analyze",
            json=body,
        )
        provider.release.set()
        created = first.result(timeout=5)

    assert created.status_code == 201
    assert duplicate.status_code == 409
    assert "already in progress" in duplicate.text
    assert len(provider.calls) == 1


def test_failed_automatic_analysis_releases_idempotency_reservation(client):
    provider = FailingOnceAnalysis()
    client.app.state.nutrition_analysis_provider = provider
    body = {
        "operation_id": str(uuid.uuid4()),
        "intent": "log_consumed",
        "modality": "text",
        "observed_at": "2026-08-06T08:30:00+09:00",
        "timezone": "Asia/Seoul",
        "source": "ios-device",
        "source_text": "아침에 바나나와 우유를 먹었어",
        "allow_remote_analysis": False,
    }
    try:
        client.post("/v1/intake-interactions/analyze", json=body)
    except RuntimeError as exc:
        assert "fixture provider failed" in str(exc)
    else:
        raise AssertionError("fixture failure must escape the test app")

    retried = client.post("/v1/intake-interactions/analyze", json=body)
    assert retried.status_code == 201
    assert len(provider.calls) == 2


def test_analysis_failure_does_not_commit_or_rollback_caller_session(
    client, session
):
    unrelated = RetentionPolicy(
        data_class="unrelated_pending",
        retention_days=1,
        enabled=True,
    )
    session.add(unrelated)

    with pytest.raises(VisionUnavailable, match="fixture unavailable"):
        create_analyzed_interaction(
            session,
            client.app.state.settings,
            operation_id=uuid.uuid4(),
            operation_fingerprint=operation_fingerprint({"fixture": "input"}),
            intent=IntakeIntent.LOG_CONSUMED,
            modality=CaptureModality.TEXT,
            observed_at=datetime(2026, 8, 6, 3, 30, tzinfo=UTC),
            timezone="Asia/Seoul",
            source="test",
            source_text="I ate lunch",
            media_path=None,
            recorded_at=datetime.now(UTC),
            allow_remote_analysis=False,
            provider=UnavailableAnalysis(),
        )

    assert sa_inspect(unrelated).pending is True
    assert unrelated in session.new


def test_analysis_reservation_does_not_commit_flushed_caller_state(
    client, session
):
    unrelated = RetentionPolicy(
        data_class="unrelated_flushed",
        retention_days=1,
        enabled=True,
    )
    session.add(unrelated)
    session.flush()

    with pytest.raises(VisionUnavailable, match="fixture unavailable"):
        create_analyzed_interaction(
            session,
            client.app.state.settings,
            operation_id=uuid.uuid4(),
            operation_fingerprint=operation_fingerprint(
                {"fixture": "flushed-input"}
            ),
            intent=IntakeIntent.LOG_CONSUMED,
            modality=CaptureModality.TEXT,
            observed_at=datetime(2026, 8, 6, 3, 30, tzinfo=UTC),
            timezone="Asia/Seoul",
            source="test",
            source_text="I ate lunch",
            media_path=None,
            recorded_at=datetime.now(UTC),
            allow_remote_analysis=False,
            provider=UnavailableAnalysis(),
        )

    session.rollback()

    with Session(bind=session.get_bind()) as verification_session:
        remaining = verification_session.scalar(
            select(RetentionPolicy).where(
                RetentionPolicy.data_class == "unrelated_flushed"
            )
        )
    assert remaining is None


def test_expired_analysis_lease_can_be_reclaimed(client, session):
    operation_id = uuid.uuid4()
    fingerprint = operation_fingerprint({"fixture": "stale"})
    marker = WellnessEvent(
        event_type="nutrition.operation.v1",
        schema_version=1,
        observed_at=datetime(2026, 8, 6, 1, tzinfo=UTC),
        recorded_at=datetime(2026, 8, 6, 1, tzinfo=UTC),
        timezone=None,
        source_provider="nutrition-operation",
        source_device=None,
        source_record_id=f"interaction:{operation_id}",
        capture_method="system",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=None,
        payload={
            "operation_kind": "intake_interaction",
            "operation_id": str(operation_id),
            "operation_fingerprint": fingerprint,
            "operation_state": "processing",
            "reservation_token": "abandoned",
            "lease_expires_at": "2020-01-01T00:00:00+00:00",
        },
        derived_from=None,
    )
    session.add(marker)
    session.commit()

    interaction = create_analyzed_interaction(
        session,
        client.app.state.settings,
        operation_id=operation_id,
        operation_fingerprint=fingerprint,
        intent=IntakeIntent.LOG_CONSUMED,
        modality=CaptureModality.TEXT,
        observed_at=datetime(2026, 8, 6, 3, 30, tzinfo=UTC),
        timezone="Asia/Seoul",
        source="test",
        source_text="I ate lunch",
        media_path=None,
        recorded_at=datetime.now(UTC),
        allow_remote_analysis=False,
        provider=FakeAnalysis(),
    )
    session.commit()

    assert interaction.interaction_id == operation_id
    session.refresh(marker)
    assert marker.payload["operation_state"] == "completed"
    assert "lease_expires_at" not in marker.payload


def test_expired_analysis_lease_has_one_concurrent_reclaimer(
    client, session
):
    provider = SlowAnalysis()
    client.app.state.nutrition_analysis_provider = provider
    operation_id = str(uuid.uuid4())
    body = {
        "operation_id": operation_id,
        "intent": "log_consumed",
        "modality": "text",
        "observed_at": "2026-08-06T08:30:00+09:00",
        "timezone": "Asia/Seoul",
        "source": "ios-device",
        "source_text": "아침에 바나나와 우유를 먹었어",
        "allow_remote_analysis": False,
    }
    fingerprint = operation_fingerprint(
        AnalyzeInteractionInput.model_validate(body).model_dump(mode="json")
    )
    session.add(
        WellnessEvent(
            event_type="nutrition.operation.v1",
            schema_version=1,
            observed_at=datetime(2026, 8, 6, 1, tzinfo=UTC),
            recorded_at=datetime(2026, 8, 6, 1, tzinfo=UTC),
            timezone=None,
            source_provider="nutrition-operation",
            source_device=None,
            source_record_id=f"interaction:{operation_id}",
            capture_method="system",
            quality_flags=None,
            confidence=None,
            sensitivity="wellness",
            consent_scope="personal",
            retention_policy_id=None,
            expires_at=None,
            payload={
                "operation_kind": "intake_interaction",
                "operation_id": operation_id,
                "operation_fingerprint": fingerprint,
                "operation_state": "processing",
                "reservation_token": "abandoned",
                "lease_expires_at": "2020-01-01T00:00:00+00:00",
            },
            derived_from=None,
        )
    )
    session.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                client.post,
                "/v1/intake-interactions/analyze",
                json=body,
            )
            for _ in range(2)
        ]
        responses = [future.result(timeout=10) for future in futures]

    assert sorted(response.status_code for response in responses) == [201, 409]
    assert len(provider.calls) == 1


def test_final_transaction_rollback_releases_analysis_reservation(
    client, session
):
    operation_id = uuid.uuid4()
    fingerprint = operation_fingerprint({"fixture": "final-rollback"})
    create_analyzed_interaction(
        session,
        client.app.state.settings,
        operation_id=operation_id,
        operation_fingerprint=fingerprint,
        intent=IntakeIntent.LOG_CONSUMED,
        modality=CaptureModality.TEXT,
        observed_at=datetime(2026, 8, 6, 3, 30, tzinfo=UTC),
        timezone="Asia/Seoul",
        source="test",
        source_text="I ate lunch",
        media_path=None,
        recorded_at=datetime.now(UTC),
        allow_remote_analysis=False,
        provider=FakeAnalysis(),
    )

    assert "nutrition_analysis_reservations" in session.info
    session.rollback()

    remaining = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.source_provider == "nutrition-operation",
            WellnessEvent.source_record_id == f"interaction:{operation_id}",
        )
    )
    assert remaining is None, remaining.payload


def test_savepoint_rollback_keeps_analysis_reservation(client, session):
    operation_id = uuid.uuid4()
    fingerprint = operation_fingerprint(
        {"fixture": "savepoint-rollback"}
    )

    class SavepointRollbackAnalysis(FakeAnalysis):
        def analyze_text(self, text, *, allow_remote):
            savepoint = session.begin_nested()
            savepoint.rollback()
            with Session(bind=session.get_bind()) as concurrent_session:
                with pytest.raises(IntakeAnalysisInProgress):
                    create_analyzed_interaction(
                        concurrent_session,
                        client.app.state.settings,
                        operation_id=operation_id,
                        operation_fingerprint=fingerprint,
                        intent=IntakeIntent.LOG_CONSUMED,
                        modality=CaptureModality.TEXT,
                        observed_at=datetime(
                            2026,
                            8,
                            6,
                            3,
                            30,
                            tzinfo=UTC,
                        ),
                        timezone="Asia/Seoul",
                        source="test",
                        source_text="I ate lunch",
                        media_path=None,
                        recorded_at=datetime.now(UTC),
                        allow_remote_analysis=False,
                        provider=FakeAnalysis(),
                    )
            assert "nutrition_analysis_reservations" in session.info
            return super().analyze_text(
                text,
                allow_remote=allow_remote,
            )

    create_analyzed_interaction(
        session,
        client.app.state.settings,
        operation_id=operation_id,
        operation_fingerprint=fingerprint,
        intent=IntakeIntent.LOG_CONSUMED,
        modality=CaptureModality.TEXT,
        observed_at=datetime(2026, 8, 6, 3, 30, tzinfo=UTC),
        timezone="Asia/Seoul",
        source="test",
        source_text="I ate lunch",
        media_path=None,
        recorded_at=datetime.now(UTC),
        allow_remote_analysis=False,
        provider=SavepointRollbackAnalysis(),
    )

    marker = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.source_provider == "nutrition-operation",
            WellnessEvent.source_record_id == f"interaction:{operation_id}",
        )
    )
    assert marker is not None
    assert "reservation_token" not in marker.payload
    assert "nutrition_analysis_reservations" in session.info

    session.rollback()

    with Session(bind=session.get_bind()) as verification_session:
        remaining = verification_session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.source_provider == "nutrition-operation",
                WellnessEvent.source_record_id == f"interaction:{operation_id}",
            )
        )
    assert remaining is None


def test_failed_final_commit_releases_analysis_reservation(client, session):
    operation_id = uuid.uuid4()
    create_analyzed_interaction(
        session,
        client.app.state.settings,
        operation_id=operation_id,
        operation_fingerprint=operation_fingerprint(
            {"fixture": "failed-final-commit"}
        ),
        intent=IntakeIntent.LOG_CONSUMED,
        modality=CaptureModality.TEXT,
        observed_at=datetime(2026, 8, 6, 3, 30, tzinfo=UTC),
        timezone="Asia/Seoul",
        source="test",
        source_text="I ate lunch",
        media_path=None,
        recorded_at=datetime.now(UTC),
        allow_remote_analysis=False,
        provider=FakeAnalysis(),
    )
    session.add(
        WellnessEvent(
            event_type="fixture.conflict",
            schema_version=1,
            observed_at=datetime.now(UTC),
            recorded_at=datetime.now(UTC),
            timezone="UTC",
            source_provider="nutrition-operation",
            source_device=None,
            source_record_id=f"interaction:{operation_id}",
            capture_method="test",
            quality_flags=None,
            confidence=None,
            coverage=None,
            sensitivity="wellness",
            consent_scope="personal",
            retention_policy_id=None,
            expires_at=None,
            payload={"fixture": True},
            raw_object_id=None,
            derived_from=None,
        )
    )

    with pytest.raises(IntegrityError):
        session.commit()

    with Session(bind=session.get_bind()) as verification_session:
        remaining = verification_session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.source_provider == "nutrition-operation",
                WellnessEvent.source_record_id == f"interaction:{operation_id}",
            )
        )
    assert remaining is None
    assert "nutrition_analysis_reservations" not in session.info


def test_expired_sqlite_reservation_rejects_late_owner(client, session):
    operation_id = uuid.uuid4()
    fingerprint = operation_fingerprint({"fixture": "late-owner"})

    class ReclaimedDuringAnalysis(FakeAnalysis):
        def analyze_text(self, text, *, allow_remote):
            key = (id(session.get_bind()), operation_id)
            with (
                intake_service_module._STATIC_ANALYSIS_RESERVATIONS_LOCK
            ):
                reservation = (
                    intake_service_module._STATIC_ANALYSIS_RESERVATIONS[key]
                )
                reservation["lease_expires_at"] = datetime.now(
                    UTC
                ) - timedelta(seconds=1)
            with Session(bind=session.get_bind()) as winner_session:
                create_analyzed_interaction(
                    winner_session,
                    client.app.state.settings,
                    operation_id=operation_id,
                    operation_fingerprint=fingerprint,
                    intent=IntakeIntent.LOG_CONSUMED,
                    modality=CaptureModality.TEXT,
                    observed_at=datetime(
                        2026,
                        8,
                        6,
                        3,
                        30,
                        tzinfo=UTC,
                    ),
                    timezone="Asia/Seoul",
                    source="winner",
                    source_text=text,
                    media_path=None,
                    recorded_at=datetime.now(UTC),
                    allow_remote_analysis=False,
                    provider=FakeAnalysis(),
                )
                winner_session.commit()
            return super().analyze_text(
                text,
                allow_remote=allow_remote,
            )

    with pytest.raises(IntakeAnalysisInProgress, match="stale"):
        create_analyzed_interaction(
            session,
            client.app.state.settings,
            operation_id=operation_id,
            operation_fingerprint=fingerprint,
            intent=IntakeIntent.LOG_CONSUMED,
            modality=CaptureModality.TEXT,
            observed_at=datetime(2026, 8, 6, 3, 30, tzinfo=UTC),
            timezone="Asia/Seoul",
            source="late-owner",
            source_text="I ate lunch",
            media_path=None,
            recorded_at=datetime.now(UTC),
            allow_remote_analysis=False,
            provider=ReclaimedDuringAnalysis(),
        )
    session.rollback()


def test_persisting_sqlite_reservation_cannot_be_reclaimed(
    client, session
):
    operation_id = uuid.uuid4()
    fingerprint = operation_fingerprint({"fixture": "persisting-owner"})
    token = intake_service_module._reserve_interaction_analysis(
        session,
        interaction_id=operation_id,
        operation_fingerprint=fingerprint,
        lease_seconds=60,
    )
    intake_service_module._claim_process_local_persistence(
        session,
        interaction=SimpleNamespace(
            interaction_id=operation_id,
            operation_fingerprint=fingerprint,
        ),
        reservation_token=token,
    )
    key = (id(session.get_bind()), operation_id)
    with intake_service_module._STATIC_ANALYSIS_RESERVATIONS_LOCK:
        intake_service_module._STATIC_ANALYSIS_RESERVATIONS[key][
            "lease_expires_at"
        ] = datetime.now(UTC) - timedelta(seconds=1)

    with Session(bind=session.get_bind()) as contender:
        with pytest.raises(IntakeAnalysisInProgress):
            intake_service_module._reserve_interaction_analysis(
                contender,
                interaction_id=operation_id,
                operation_fingerprint=fingerprint,
                lease_seconds=60,
            )

    intake_service_module._release_interaction_analysis(
        session,
        interaction_id=operation_id,
        reservation_token=token,
    )


def test_persistent_reservation_completion_uses_token_cas(
    client, session, monkeypatch
):
    operation_id = uuid.uuid4()
    fingerprint = operation_fingerprint({"fixture": "persistent-cas"})
    marker = WellnessEvent(
        event_type="nutrition.operation.v1",
        schema_version=1,
        observed_at=datetime.now(UTC),
        recorded_at=datetime.now(UTC),
        timezone=None,
        source_provider="nutrition-operation",
        source_device=None,
        source_record_id=f"interaction:{operation_id}",
        capture_method="system",
        quality_flags=None,
        confidence=None,
        coverage=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=None,
        payload={
            "operation_kind": "intake_interaction",
            "operation_id": str(operation_id),
            "operation_fingerprint": fingerprint,
            "operation_state": "processing",
            "reservation_token": "owner-a",
            "lease_expires_at": "2026-08-06T01:00:00+00:00",
        },
        raw_object_id=None,
        derived_from=None,
    )
    session.add(marker)
    session.commit()
    stale_marker = session.scalar(
        select(WellnessEvent).where(WellnessEvent.id == marker.id)
    )
    assert stale_marker is not None

    with Session(bind=session.get_bind()) as winner:
        winner.execute(
            update(WellnessEvent)
            .where(WellnessEvent.id == marker.id)
            .values(
                payload={
                    **marker.payload,
                    "reservation_token": "owner-b",
                }
            )
        )
        winner.commit()

    monkeypatch.setattr(
        intake_service_module,
        "_uses_process_local_reservations",
        lambda current_session: False,
    )
    with pytest.raises(IntakeAnalysisInProgress, match="stale"):
        intake_service_module._persist_interaction_operation_marker(
            session,
            SimpleNamespace(
                interaction_id=operation_id,
                operation_fingerprint=fingerprint,
                recorded_at=datetime.now(UTC),
            ),
            reservation_token="owner-a",
        )
    session.rollback()


def test_session_close_releases_sqlite_analysis_reservation(client, session):
    operation_id = uuid.uuid4()
    fingerprint = operation_fingerprint({"fixture": "session-close"})
    first = Session(bind=session.get_bind())
    create_analyzed_interaction(
        first,
        client.app.state.settings,
        operation_id=operation_id,
        operation_fingerprint=fingerprint,
        intent=IntakeIntent.LOG_CONSUMED,
        modality=CaptureModality.TEXT,
        observed_at=datetime(2026, 8, 6, 3, 30, tzinfo=UTC),
        timezone="Asia/Seoul",
        source="first",
        source_text="I ate lunch",
        media_path=None,
        recorded_at=datetime.now(UTC),
        allow_remote_analysis=False,
        provider=FakeAnalysis(),
    )
    first.close()

    with Session(bind=session.get_bind()) as retry_session:
        retried = create_analyzed_interaction(
            retry_session,
            client.app.state.settings,
            operation_id=operation_id,
            operation_fingerprint=fingerprint,
            intent=IntakeIntent.LOG_CONSUMED,
            modality=CaptureModality.TEXT,
            observed_at=datetime(2026, 8, 6, 3, 30, tzinfo=UTC),
            timezone="Asia/Seoul",
            source="first",
            source_text="I ate lunch",
            media_path=None,
            recorded_at=datetime.now(UTC),
            allow_remote_analysis=False,
            provider=FakeAnalysis(),
        )
        retry_session.commit()
    assert retried.interaction_id == operation_id


def test_voice_is_transcribed_locally_then_automatically_analyzed(
    client, session
):
    provider = FakeAnalysis()
    transcriber = FakeTranscriber()
    client.app.state.nutrition_analysis_provider = provider
    client.app.state.nutrition_transcriber = transcriber
    media_path = _upload(client, b"fake-m4a", "audio/m4a", "meal.m4a")
    created = client.post(
        "/v1/intake-interactions/analyze",
        json={
            "operation_id": str(uuid.uuid4()),
            "intent": "log_consumed",
            "modality": "voice",
            "observed_at": _recent_utc(),
            "timezone": "UTC",
            "source": "android-device",
            "media_path": media_path,
            "allow_remote_analysis": False,
        },
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["source_text"] == "아침에 바나나와 우유를 먹었어"
    assert payload["analysis_provenance"]["transcription_provider"] == (
        "fixture-whisper"
    )
    assert payload["analysis_provenance"]["transcription_model"] == (
        "fixture-small"
    )
    assert len(transcriber.calls) == 1
    assert provider.calls == [
        ("아침에 바나나와 우유를 먹었어", False)
    ]
    obj = session.scalar(
        select(StorageObject).where(StorageObject.relative_path == media_path)
    )
    assert obj is not None
    assert obj.data_class == "nutrition_media"


def test_photo_adapter_keeps_sake_observation_and_maps_caffeine(
    client, session
):
    observation_id = _photo_observation(client)
    operation_id = str(uuid.uuid4())
    request_body = {
        "operation_id": operation_id,
        "intent": "ask_before_intake",
        "modality": "photo",
        "source": "galaxy-device",
        "source_text": "이 커피를 마셔도 될까?",
        "nutrition_observation_id": observation_id,
    }
    created = client.post(
        "/v1/intake-interactions",
        json=request_body,
    )
    assert created.status_code == 201
    body = created.json()
    assert body["nutrition_observation_id"] == observation_id
    assert body["items"][0]["name"] == "bottled coffee"
    assert body["items"][0]["nutrients"][0]["nutrient"] == "caffeine"
    assert body["items"][0]["nutrients"][0]["amount"]["exact"] == 180
    assert body["is_confirmed_intake"] is False

    observation_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.observation.v1",
            WellnessEvent.source_record_id == observation_id,
        )
    )
    assert observation_event is not None
    session.delete(observation_event)
    session.commit()
    retry = client.post("/v1/intake-interactions", json=request_body)
    assert retry.status_code == 201
    assert retry.json()["interaction_id"] == operation_id
    conflict = client.post(
        "/v1/intake-interactions",
        json={**request_body, "source_text": "다른 질문"},
    )
    assert conflict.status_code == 409


def test_confirmed_photo_review_promotes_nutrients_to_user_origin(client):
    observation_id = _photo_observation(client)
    reviewed = client.post(
        f"/v1/nutrition-observations/{observation_id}/review",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "confirmed",
            "source": "desktop-web",
        },
    )
    assert reviewed.status_code == 201
    created = client.post(
        "/v1/intake-interactions",
        json={
            "operation_id": str(uuid.uuid4()),
            "intent": "log_consumed",
            "modality": "photo",
            "source": "ios-device",
            "nutrition_observation_id": observation_id,
        },
    )
    assert created.status_code == 201
    assert {
        nutrient["origin"]
        for item in created.json()["items"]
        for nutrient in item["nutrients"]
    } == {"user"}


def test_photo_review_correction_flows_into_interaction_search_and_context(
    client, session
):
    observation_id = _photo_observation(client)
    review_body = {
        "operation_id": str(uuid.uuid4()),
        "status": "corrected",
        "source": "desktop-web",
        "items": [
            {
                "item_index": 0,
                "name": "small bottled latte",
                "intake_type": "beverage",
                "serving": _estimate(
                    250, "ml", basis="owner_correction"
                ),
                "nutrients": _reviewed_nutrients(),
                "confidence": "high",
            }
        ],
    }
    reviewed = client.post(
        f"/v1/nutrition-observations/{observation_id}/review",
        json=review_body,
    )
    assert reviewed.status_code == 201
    retry = client.post(
        f"/v1/nutrition-observations/{observation_id}/review",
        json=review_body,
    )
    assert retry.status_code == 201
    assert retry.json()["review_id"] == reviewed.json()["review_id"]
    conflict = client.post(
        f"/v1/nutrition-observations/{observation_id}/review",
        json={
            **review_body,
            "status": "rejected",
            "items": [],
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == (
        "nutrition_review_operation_conflict"
    )
    assert "operation_id was already used" in conflict.text
    review_view = client.get(
        f"/v1/nutrition-observations/{observation_id}/review"
    )
    assert review_view.status_code == 200
    assert review_view.json()["review"]["status"] == "corrected"

    created = client.post(
        "/v1/intake-interactions",
        json={
            "operation_id": str(uuid.uuid4()),
            "intent": "ask_before_intake",
            "modality": "photo",
            "source": "galaxy-device",
            "nutrition_observation_id": observation_id,
        },
    )
    assert created.status_code == 201
    assert created.json()["nutrition_review_id"] == reviewed.json()["review_id"]
    item = created.json()["items"][0]
    assert item["name"] == "small bottled latte"
    nutrients = {
        value["nutrient"]: value for value in item["nutrients"]
    }
    assert nutrients["energy"]["amount"]["exact"] == 80
    assert nutrients["caffeine"]["amount"]["exact"] == 95
    assert nutrients["caffeine"]["origin"] == "user"

    searched = client.get(
        "/v1/intake-interactions",
        params={"nutrient": "energy"},
    )
    assert searched.status_code == 200
    assert searched.json()["records"][0]["resolved_items"][0]["name"] == (
        "small bottled latte"
    )
    decision_request = client.post(
        f"/v1/intake-interactions/{created.json()['interaction_id']}/decision-requests",
        json={
            "operation_id": str(uuid.uuid4()),
            "scope": "daily_nutrition",
            "source": "galaxy-device",
        },
    )
    assert decision_request.status_code == 201
    context = client.get(
        "/v1/intake-interactions/decision-requests/"
        f"{decision_request.json()['request_id']}/context"
    )
    assert context.status_code == 200
    review_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.review.v1"
        )
    )
    assert review_event is not None
    assert str(review_event.id) in context.json()["evidence_event_ids"]
    observation_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.observation.v1",
            WellnessEvent.source_record_id == observation_id,
        )
    )
    assert observation_event is not None
    assert review_event.retention_policy_id == (
        observation_event.retention_policy_id
    )
    assert review_event.expires_at == observation_event.expires_at
    retention_update = client.put(
        "/v1/storage/settings/nutrition_observation",
        json={"preset": "30d"},
    )
    assert retention_update.status_code == 200
    session.expire_all()
    assert review_event.expires_at == observation_event.expires_at

    colliding_review = WellnessEvent(
        event_type="nutrition.review.v1",
        schema_version=1,
        observed_at=review_event.observed_at,
        recorded_at=review_event.recorded_at + timedelta(seconds=1),
        timezone=review_event.timezone,
        source_provider="untrusted-fixture",
        source_device="fixture",
        source_record_id=review_event.source_record_id,
        capture_method="manual",
        quality_flags=review_event.quality_flags,
        confidence=1.0,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=review_event.retention_policy_id,
        expires_at=review_event.expires_at,
        payload=review_event.payload,
        derived_from=review_event.derived_from,
    )
    session.add(colliding_review)
    session.commit()
    latest_review = client.get(
        f"/v1/nutrition-observations/{observation_id}/review"
    )
    assert latest_review.status_code == 200
    assert latest_review.json()["review"]["review_id"] == (
        reviewed.json()["review_id"]
    )

    observation_day = observation_event.observed_at
    if observation_day.tzinfo is None:
        observation_day = observation_day.replace(tzinfo=UTC)
    local_date = observation_day.astimezone(UTC).date()
    daily = client.post(
        "/v1/nutrition-observations/daily-confirmations",
        json={
            "local_date": local_date.isoformat(),
            "timezone": "UTC",
            "observation_ids": [observation_id],
            "total_intake_complete": True,
            "source": "desktop-web",
        },
    )
    assert daily.status_code == 201
    session.expire_all()
    caffeine = known_caffeine_for_day(
        session,
        local_date=local_date,
        timezone="UTC",
    )
    assert caffeine["status"] == "known"
    assert caffeine["confirmed_caffeine_mg"] == 95
    assert caffeine["evidence"][0]["event_type"] == "nutrition.review.v1"

    comparison_primary = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            intent="compare_option",
            source_text="이 식사와 라테를 비교해줘",
        ),
    )
    assert comparison_primary.status_code == 201
    comparison_request = client.post(
        f"/v1/intake-interactions/{comparison_primary.json()['interaction_id']}"
        "/decision-requests",
        json={
            "operation_id": str(uuid.uuid4()),
            "scope": "compare_options",
            "source": "desktop-web",
            "compare_interaction_ids": [created.json()["interaction_id"]],
        },
    )
    assert comparison_request.status_code == 201
    comparison_context = client.get(
        "/v1/intake-interactions/decision-requests/"
        f"{comparison_request.json()['request_id']}/context"
    )
    assert comparison_context.status_code == 200
    assert str(review_event.id) in (
        comparison_context.json()["evidence_event_ids"]
    )
    assert str(colliding_review.id) not in (
        comparison_context.json()["evidence_event_ids"]
    )


def test_rejected_photo_observation_cannot_create_interaction(client):
    observation_id = _photo_observation(client)
    rejected = client.post(
        f"/v1/nutrition-observations/{observation_id}/review",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "rejected",
            "source": "desktop-web",
        },
    )
    assert rejected.status_code == 201

    created = client.post(
        "/v1/intake-interactions",
        json={
            "operation_id": str(uuid.uuid4()),
            "intent": "log_consumed",
            "modality": "photo",
            "source": "galaxy-device",
            "nutrition_observation_id": observation_id,
        },
    )
    assert created.status_code == 422
    assert "rejected nutrition observations" in created.text


def test_corrected_photo_review_requires_every_core_nutrient(client):
    observation_id = _photo_observation(client)
    response = client.post(
        f"/v1/nutrition-observations/{observation_id}/review",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "corrected",
            "source": "desktop-web",
            "items": [
                {
                    "item_index": 0,
                    "name": "small bottled latte",
                    "intake_type": "beverage",
                    "serving": _estimate(
                        250, "ml", basis="owner_correction"
                    ),
                    "nutrients": _reviewed_nutrients()[:-1],
                }
            ],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_nutrition_review"
    assert "missing core nutrients" in response.text


def test_inspect_only_cannot_request_decision(client):
    interaction = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(intent="inspect_only"),
    ).json()
    response = client.post(
        f"/v1/intake-interactions/{interaction['interaction_id']}/decision-requests",
        json={
            "operation_id": str(uuid.uuid4()),
            "scope": "daily_nutrition",
            "source": "ios-device",
        },
    )
    assert response.status_code == 422
    assert "inspect-only" in response.text


def test_latest_not_consumed_outcome_removes_old_consumed_history(client):
    logged = client.post(
        "/v1/intake-interactions", json=_text_interaction()
    ).json()
    logged_id = logged["interaction_id"]
    consumed = client.post(
        f"/v1/intake-interactions/{logged_id}/outcomes",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "consumed",
            "source": "ios-device",
            "consumed_at": "2026-08-06T12:30:00+09:00",
        },
    )
    assert consumed.status_code == 201
    corrected = client.post(
        f"/v1/intake-interactions/{logged_id}/outcomes",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "not_consumed",
            "source": "ios-device",
            "note": "잘못 기록함",
        },
    )
    assert corrected.status_code == 201

    candidate = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            intent="ask_before_intake",
            observed_at="2026-08-06T15:00:00+09:00",
            source_text="저녁 식사를 어떻게 할까?",
            items=[],
        ),
    ).json()
    requested = client.post(
        f"/v1/intake-interactions/{candidate['interaction_id']}/decision-requests",
        json={
            "operation_id": str(uuid.uuid4()),
            "scope": "daily_nutrition",
            "source": "ios-device",
            "intended_consumption_at": "2026-08-06T18:00:00+09:00",
        },
    ).json()
    context = client.get(
        f"/v1/intake-interactions/decision-requests/{requested['request_id']}/context"
    )
    assert context.status_code == 200
    assert context.json()["confirmed_intake_history"] == []


def test_consumed_outcome_rejects_future_timestamp(client):
    interaction = client.post(
        "/v1/intake-interactions", json=_text_interaction()
    ).json()
    response = client.post(
        f"/v1/intake-interactions/{interaction['interaction_id']}/outcomes",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "consumed",
            "source": "ios-device",
            "consumed_at": "2099-01-01T00:00:00Z",
        },
    )
    assert response.status_code == 422
    assert "cannot be in the future" in response.text


def test_operation_id_is_idempotent_and_conflicts_on_different_input(
    client, session
):
    body = _text_interaction()
    first = client.post("/v1/intake-interactions", json=body)
    retry = client.post("/v1/intake-interactions", json=body)
    assert first.status_code == retry.status_code == 201
    assert first.json()["interaction_id"] == retry.json()["interaction_id"]

    changed = {**body, "source_text": "다른 식사"}
    conflict = client.post("/v1/intake-interactions", json=changed)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "operation_id_conflict"

    interaction_id = first.json()["interaction_id"]
    outcome = {
        "operation_id": str(uuid.uuid4()),
        "status": "consumed",
        "source": "ios-device",
        "consumed_at": "2026-08-06T12:30:00+09:00",
    }
    outcome_path = f"/v1/intake-interactions/{interaction_id}/outcomes"
    assert client.post(outcome_path, json=outcome).status_code == 201
    assert client.post(outcome_path, json=outcome).status_code == 201
    outcome_conflict = client.post(
        outcome_path, json={**outcome, "status": "not_consumed", "consumed_at": None}
    )
    assert outcome_conflict.status_code == 409

    request_body = {
        "operation_id": str(uuid.uuid4()),
        "scope": "daily_nutrition",
        "source": "ios-device",
    }
    request_path = (
        f"/v1/intake-interactions/{interaction_id}/decision-requests"
    )
    request = client.post(request_path, json=request_body)
    retry_request = client.post(request_path, json=request_body)
    assert request.status_code == retry_request.status_code == 201
    assert request.json()["request_id"] == retry_request.json()["request_id"]
    request_conflict = client.post(
        request_path, json={**request_body, "lookback_days": 7}
    )
    assert request_conflict.status_code == 409

    request_id = request.json()["request_id"]
    context = client.get(
        f"/v1/intake-interactions/decision-requests/{request_id}/context"
    ).json()
    decision_body = {
        "operation_id": str(uuid.uuid4()),
        "request_id": request_id,
        "status": "insufficient_data",
        "source": "healthmes-agent",
        "summary": "기록 범위가 부족함",
        "evidence_event_ids": context["evidence_event_ids"],
    }
    decision_path = f"/v1/intake-interactions/{interaction_id}/decisions"
    decision = client.post(decision_path, json=decision_body)
    request_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.decision-request.v1",
            WellnessEvent.source_record_id == request_id,
        )
    )
    assert request_event is not None
    session.delete(request_event)
    session.commit()
    retry_decision = client.post(decision_path, json=decision_body)
    assert decision.status_code == retry_decision.status_code == 201
    assert decision.json()["decision_id"] == retry_decision.json()["decision_id"]
    decision_conflict = client.post(
        decision_path, json={**decision_body, "summary": "다른 판단"}
    )
    assert decision_conflict.status_code == 409


def test_decision_context_is_immutable_after_new_outcome(client):
    candidate = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(intent="ask_before_intake"),
    ).json()
    requested = client.post(
        f"/v1/intake-interactions/{candidate['interaction_id']}/decision-requests",
        json={
            "operation_id": str(uuid.uuid4()),
            "scope": "daily_nutrition",
            "source": "ios-device",
        },
    ).json()
    path = (
        "/v1/intake-interactions/decision-requests/"
        f"{requested['request_id']}/context"
    )
    before = client.get(path).json()

    confirmed = client.post(
        f"/v1/intake-interactions/{candidate['interaction_id']}/outcomes",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "consumed",
            "source": "ios-device",
            "consumed_at": "2026-08-06T12:30:00+09:00",
        },
    )
    assert confirmed.status_code == 201
    assert client.get(path).json() == before


def test_search_reports_explicit_truncation_and_coverage(client):
    for name in ("첫 식사", "둘째 식사"):
        created = client.post(
            "/v1/intake-interactions",
            json=_text_interaction(
                source_text=name,
                items=[
                    {
                        "name": name,
                        "intake_type": "food",
                        "serving": _estimate(1, "serving"),
                    }
                ],
            ),
        )
        assert created.status_code == 201

    response = client.get("/v1/intake-interactions", params={"limit": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["truncated"] is True
    assert body["coverage"] == {
        "complete": False,
        "scanned_records": 2,
        "matching_records": 2,
        "result_limit": 1,
    }


def test_expired_raw_capture_cannot_reuse_operation_id(
    client, session
):
    body = _text_interaction()
    created = client.post("/v1/intake-interactions", json=body)
    assert created.status_code == 201
    interaction_id = created.json()["interaction_id"]
    raw_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.interaction.v1",
            WellnessEvent.source_record_id == interaction_id,
        )
    )
    assert raw_event is not None
    session.delete(raw_event)
    session.commit()

    same_retry = client.post("/v1/intake-interactions", json=body)
    assert same_retry.status_code == 409
    assert "expired capture" in same_retry.text
    changed_retry = client.post(
        "/v1/intake-interactions",
        json={**body, "source_text": "완전히 다른 식사"},
    )
    assert changed_retry.status_code == 409
    assert "different input" in changed_retry.text
    assert client.get(f"/v1/intake-interactions/{interaction_id}").status_code == 404


def test_prospective_snapshot_remains_searchable_after_raw_expiry(
    client, session
):
    candidate = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            intent="ask_before_intake",
            source_text="이 수프를 먹어도 될까?",
            items=[
                {
                    "name": "토마토 수프",
                    "intake_type": "food",
                    "serving": _estimate(1, "bowl"),
                }
            ],
        ),
    ).json()
    request_body = {
        "operation_id": str(uuid.uuid4()),
        "scope": "daily_nutrition",
        "source": "ios-device",
    }
    request_path = (
        f"/v1/intake-interactions/{candidate['interaction_id']}/decision-requests"
    )
    requested = client.post(request_path, json=request_body)
    assert requested.status_code == 201
    raw_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.interaction.v1",
            WellnessEvent.source_record_id == candidate["interaction_id"],
        )
    )
    assert raw_event is not None
    session.delete(raw_event)
    session.commit()

    searched = client.get(
        "/v1/intake-interactions", params={"query": "토마토 수프"}
    )
    assert searched.status_code == 200
    assert searched.json()["count"] == 1
    record = searched.json()["records"][0]
    assert record["interaction_id"] == candidate["interaction_id"]
    assert record["raw_capture_available"] is False
    assert record["source_text"] is None

    retried = client.post(request_path, json=request_body)
    assert retried.status_code == 201
    assert retried.json()["request_id"] == requested.json()["request_id"]


def test_raw_text_expires_independently_from_structured_interaction(
    client, session
):
    body = _text_interaction()
    created = client.post("/v1/intake-interactions", json=body)
    assert created.status_code == 201
    interaction_id = created.json()["interaction_id"]

    report = run_storage_maintenance(
        session,
        client.app.state.settings,
        now=datetime(2026, 8, 21, 12, tzinfo=UTC),
    )
    session.commit()

    assert report.errors == ()
    assert session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.raw-capture.v1",
            WellnessEvent.source_record_id == interaction_id,
        )
    ) is None
    structured = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.interaction.v1",
            WellnessEvent.source_record_id == interaction_id,
        )
    )
    assert structured is not None
    assert structured.payload["items"][0]["name"] == "닭가슴살 샐러드"
    fetched = client.get(f"/v1/intake-interactions/{interaction_id}")
    assert fetched.status_code == 200
    assert fetched.json()["source_text"] is None
    assert fetched.json()["raw_capture_available"] is False


def test_legacy_raw_text_is_migrated_or_removed_by_raw_policy(
    client, session
):
    body = _text_interaction(
        observed_at="2026-08-01T12:30:00+09:00",
    )
    created = client.post("/v1/intake-interactions", json=body)
    assert created.status_code == 201
    interaction_id = created.json()["interaction_id"]
    structured = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.interaction.v1",
            WellnessEvent.source_record_id == interaction_id,
        )
    )
    raw = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.raw-capture.v1",
            WellnessEvent.source_record_id == interaction_id,
        )
    )
    raw_policy = session.scalar(
        select(RetentionPolicy).where(
            RetentionPolicy.data_class == "nutrition_raw_capture"
        )
    )
    assert structured is not None
    assert raw is not None
    assert raw_policy is not None
    session.delete(raw)
    structured.payload = {
        **structured.payload,
        "source_text": body["source_text"],
    }
    raw_policy.retention_days = 1
    session.commit()

    run_storage_maintenance(
        session,
        client.app.state.settings,
        now=datetime(2026, 8, 3, 12, tzinfo=UTC),
    )
    session.commit()
    session.expire_all()

    migrated = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.raw-capture.v1",
            WellnessEvent.source_record_id == interaction_id,
        )
    )
    structured = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.interaction.v1",
            WellnessEvent.source_record_id == interaction_id,
        )
    )
    assert migrated is None
    assert structured is not None
    assert structured.payload["source_text"] is None
    fetched = client.get(f"/v1/intake-interactions/{interaction_id}")
    assert fetched.status_code == 200
    assert fetched.json()["source_text"] is None


def test_legacy_warnings_are_migrated_for_the_remaining_raw_ttl(
    client,
    session,
):
    warning = "portion is uncertain"
    item_warning = "milk type is unknown"
    body = _text_interaction(
        observed_at="2026-08-06T12:30:00+09:00",
        warnings=[warning],
    )
    body["items"][0]["warnings"] = [item_warning]
    created = client.post("/v1/intake-interactions", json=body)
    assert created.status_code == 201
    interaction_id = created.json()["interaction_id"]
    structured = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.interaction.v1",
            WellnessEvent.source_record_id == interaction_id,
        )
    )
    raw = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.raw-capture.v1",
            WellnessEvent.source_record_id == interaction_id,
        )
    )
    assert structured is not None
    assert raw is not None
    session.delete(raw)
    payload = dict(structured.payload)
    payload["warnings"] = [warning]
    payload["items"][0]["warnings"] = [item_warning]
    structured.payload = payload
    session.commit()

    run_storage_maintenance(
        session,
        client.app.state.settings,
        now=datetime(2026, 8, 7, 12, tzinfo=UTC),
    )
    session.commit()
    session.expire_all()

    fetched = client.get(f"/v1/intake-interactions/{interaction_id}")
    assert fetched.status_code == 200
    assert fetched.json()["warnings"] == [warning]
    assert fetched.json()["items"][0]["warnings"] == [item_warning]
    structured = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.interaction.v1",
            WellnessEvent.source_record_id == interaction_id,
        )
    )
    assert structured is not None
    assert structured.payload["warnings"] == []
    assert structured.payload["items"][0]["warnings"] == []


def test_raw_retention_removes_duplicate_structured_evidence(
    client, session
):
    owner_text = "95mg은 카페인, 나트륨은 355mg"
    body = _text_interaction(
        observed_at="2026-08-01T12:30:00+09:00",
        source_text=owner_text,
        items=[
            {
                "name": "커피",
                "intake_type": "beverage",
                "serving": {
                    **_estimate(355, "ml"),
                    "evidence_text": owner_text,
                },
                "nutrients": [
                    {
                        "nutrient": "caffeine",
                        "amount": {
                            **_estimate(95, "mg"),
                            "evidence_text": owner_text,
                        },
                        "confidence": "high",
                        "origin": "user",
                        "evidence_text": owner_text,
                    }
                ],
                "confidence": "high",
            }
        ],
    )
    created = client.post("/v1/intake-interactions", json=body)
    assert created.status_code == 201
    interaction_id = created.json()["interaction_id"]

    run_storage_maintenance(
        session,
        client.app.state.settings,
        now=datetime(2026, 8, 20, 12, tzinfo=UTC),
    )
    session.commit()
    structured = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.interaction.v1",
            WellnessEvent.source_record_id == interaction_id,
        )
    )
    raw = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.raw-capture.v1",
            WellnessEvent.source_record_id == interaction_id,
        )
    )

    assert raw is None
    assert structured is not None
    nutrient = structured.payload["items"][0]["nutrients"][0]
    assert structured.payload["source_text"] is None
    assert structured.payload["items"][0]["serving"]["evidence_text"] is None
    assert nutrient["evidence_text"] is None
    assert nutrient["amount"]["evidence_text"] is None


def test_legacy_evidence_only_payload_obeys_raw_retention(
    client, session
):
    body = _text_interaction(
        observed_at="2026-08-01T12:30:00+09:00",
    )
    created = client.post("/v1/intake-interactions", json=body)
    assert created.status_code == 201
    interaction_id = created.json()["interaction_id"]
    structured = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.interaction.v1",
            WellnessEvent.source_record_id == interaction_id,
        )
    )
    raw = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.raw-capture.v1",
            WellnessEvent.source_record_id == interaction_id,
        )
    )
    raw_policy = session.scalar(
        select(RetentionPolicy).where(
            RetentionPolicy.data_class == "nutrition_raw_capture"
        )
    )
    assert structured is not None
    assert raw is not None
    assert raw_policy is not None
    session.delete(raw)
    payload = dict(structured.payload)
    payload["items"][0]["nutrients"][0]["evidence_text"] = body[
        "source_text"
    ]
    payload["items"][0]["nutrients"][0]["amount"][
        "evidence_text"
    ] = body["source_text"]
    structured.payload = payload
    raw_policy.retention_days = 1
    session.commit()

    run_storage_maintenance(
        session,
        client.app.state.settings,
        now=datetime(2026, 8, 3, 12, tzinfo=UTC),
    )
    session.commit()
    session.expire_all()
    structured = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.interaction.v1",
            WellnessEvent.source_record_id == interaction_id,
        )
    )
    assert structured is not None
    nutrient = structured.payload["items"][0]["nutrients"][0]
    assert nutrient["evidence_text"] is None
    assert nutrient["amount"]["evidence_text"] is None


def test_expired_raw_capture_is_hidden_before_maintenance(client):
    created = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            observed_at="2026-07-01T12:30:00+09:00",
            source_text="private expired meal text",
        ),
    )

    assert created.status_code == 201
    assert created.json()["source_text"] is None
    assert created.json()["raw_capture_available"] is False


def test_expired_structured_interaction_cannot_be_read_or_promoted(
    client,
    session,
):
    created = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            observed_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            timezone="UTC",
        ),
    )
    assert created.status_code == 201
    interaction_id = created.json()["interaction_id"]
    structured = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.interaction.v1",
            WellnessEvent.source_record_id == interaction_id,
        )
    )
    assert structured is not None
    structured.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()

    assert (
        client.get(f"/v1/intake-interactions/{interaction_id}").status_code
        == 404
    )
    searched = client.get("/v1/intake-interactions")
    assert searched.status_code == 200
    assert searched.json()["count"] == 0
    outcome = client.post(
        f"/v1/intake-interactions/{interaction_id}/outcomes",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "consumed",
            "source": "test",
            "consumed_at": datetime.now(UTC).isoformat(),
        },
    )
    assert outcome.status_code == 422
    decision = client.post(
        f"/v1/intake-interactions/{interaction_id}/decision-requests",
        json={
            "operation_id": str(uuid.uuid4()),
            "scope": "daily_nutrition",
            "source": "test",
        },
    )
    assert decision.status_code == 422


def test_expired_snapshots_cannot_restore_an_interaction(client, session):
    created = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(),
    )
    assert created.status_code == 201
    interaction_id = created.json()["interaction_id"]
    outcome = client.post(
        f"/v1/intake-interactions/{interaction_id}/outcomes",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "consumed",
            "source": "test",
            "consumed_at": "2026-08-06T12:30:00+09:00",
        },
    )
    assert outcome.status_code == 201
    requested = client.post(
        f"/v1/intake-interactions/{interaction_id}/decision-requests",
        json={
            "operation_id": str(uuid.uuid4()),
            "scope": "daily_nutrition",
            "source": "test",
        },
    )
    assert requested.status_code == 201
    request_id = requested.json()["request_id"]
    events = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type.in_(
                    (
                        "nutrition.interaction.v1",
                        "nutrition.intake-outcome.v1",
                        "nutrition.decision-request.v1",
                    )
                )
            )
        )
    )
    for event in events:
        event.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()

    assert (
        client.get(f"/v1/intake-interactions/{interaction_id}").status_code
        == 404
    )
    assert client.get("/v1/intake-interactions").json()["count"] == 0
    assert (
        client.get(
            f"/v1/intake-interactions/decision-requests/{request_id}/context"
        ).status_code
        == 404
    )


def test_expired_direct_capture_retry_returns_conflict(client, session):
    body = _text_interaction()
    created = client.post("/v1/intake-interactions", json=body)
    assert created.status_code == 201
    event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.interaction.v1",
            WellnessEvent.source_record_id
            == created.json()["interaction_id"],
        )
    )
    assert event is not None
    event.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()

    retried = client.post("/v1/intake-interactions", json=body)

    assert retried.status_code == 409
    assert "expired intake interaction cannot be retried" in retried.text


def test_capture_outside_structured_retention_window_is_rejected(
    client,
    session,
):
    updated = client.put(
        "/v1/storage/settings/nutrition_observation",
        json={"preset": "1d"},
    )
    assert updated.status_code == 200

    created = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            observed_at="2026-07-01T03:30:00Z",
            timezone="UTC",
        ),
    )

    assert created.status_code == 422
    assert "outside the structured retention window" in created.text
    assert session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.interaction.v1"
        )
    ) is None


def test_far_future_capture_is_rejected(client):
    created = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            observed_at="2099-01-01T00:00:00Z",
            timezone="UTC",
        ),
    )
    assert created.status_code == 422
    assert "more than 5 minutes in the future" in created.text


def test_expired_nutrition_media_url_is_not_served(client):
    media_path = _upload(
        client,
        b"private-audio",
        "audio/m4a",
        "meal.m4a",
    )
    created = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            modality="voice",
            observed_at="2026-07-01T12:30:00+09:00",
            media_path=media_path,
        ),
    )

    assert created.status_code == 201
    assert created.json()["media_path"] is None
    fetched = client.get(f"/v1/media/{media_path}")
    assert fetched.status_code == 404
    updated = client.put(
        "/v1/storage/settings/nutrition_media",
        json={"preset": "1d"},
    )
    assert updated.status_code == 200
    assert client.get(f"/v1/media/{media_path}").status_code == 404
    extended = client.put(
        "/v1/storage/settings/nutrition_media",
        json={"preset": "90d"},
    )
    assert extended.status_code == 200
    assert client.get(f"/v1/media/{media_path}").status_code == 404


def test_provider_warnings_cannot_preserve_raw_owner_text(client, session):
    owner_text = "private owner text: medication X at 8pm"

    class WarningEchoAnalysis(FakeAnalysis):
        def analyze_text(self, text, *, allow_remote):
            extraction = super().analyze_text(
                text,
                allow_remote=allow_remote,
            )
            extraction.warnings = [owner_text]
            extraction.items[0].warnings = [owner_text]
            return extraction

    client.app.state.nutrition_analysis_provider = WarningEchoAnalysis()
    created = client.post(
        "/v1/intake-interactions/analyze",
        json={
            "operation_id": str(uuid.uuid4()),
            "intent": "log_consumed",
            "modality": "text",
            "observed_at": "2026-07-01T03:30:00Z",
            "timezone": "UTC",
            "source": "test",
            "source_text": owner_text,
            "media_path": None,
            "allow_remote_analysis": False,
        },
    )

    assert created.status_code == 201
    payload = created.json()
    assert payload["source_text"] is None
    assert payload["warnings"] == []
    assert payload["items"][0]["warnings"] == []
    event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.interaction.v1",
            WellnessEvent.source_record_id == payload["interaction_id"],
        )
    )
    assert event is not None
    assert owner_text not in str(event.payload)
    assert owner_text not in str(event.quality_flags)


def test_raw_warnings_never_copy_into_unlimited_snapshots(client, session):
    owner_text = "private owner text: medication X at 8pm"

    class WarningEchoAnalysis(FakeAnalysis):
        def analyze_text(self, text, *, allow_remote):
            extraction = super().analyze_text(
                text,
                allow_remote=allow_remote,
            )
            extraction.warnings = [owner_text]
            extraction.items[0].warnings = [owner_text]
            return extraction

    client.app.state.nutrition_analysis_provider = WarningEchoAnalysis()
    observed_at = datetime.now(UTC).replace(microsecond=0)
    created = client.post(
        "/v1/intake-interactions/analyze",
        json={
            "operation_id": str(uuid.uuid4()),
            "intent": "log_consumed",
            "modality": "text",
            "observed_at": observed_at.isoformat(),
            "timezone": "UTC",
            "source": "test",
            "source_text": owner_text,
            "media_path": None,
            "allow_remote_analysis": False,
        },
    )
    assert created.status_code == 201
    interaction_id = created.json()["interaction_id"]
    assert created.json()["warnings"] == [owner_text]

    outcome = client.post(
        f"/v1/intake-interactions/{interaction_id}/outcomes",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "consumed",
            "source": "test",
            "consumed_at": observed_at.isoformat(),
        },
    )
    assert outcome.status_code == 201
    decision_request = client.post(
        f"/v1/intake-interactions/{interaction_id}/decision-requests",
        json={
            "operation_id": str(uuid.uuid4()),
            "scope": "daily_nutrition",
            "source": "test",
            "question": "How does this fit today?",
        },
    )
    assert decision_request.status_code == 201

    durable_events = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type.in_(
                    (
                        "nutrition.intake-outcome.v1",
                        "nutrition.decision-request.v1",
                    )
                )
            )
        )
    )
    assert len(durable_events) == 2
    assert all(event.expires_at is None for event in durable_events)
    assert all(owner_text not in str(event.payload) for event in durable_events)


def test_maintenance_scrubs_legacy_warnings_from_durable_snapshots(
    client,
    session,
):
    secret = "private medication warning"
    created = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(),
    )
    assert created.status_code == 201
    interaction_id = created.json()["interaction_id"]
    assert client.post(
        f"/v1/intake-interactions/{interaction_id}/outcomes",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "consumed",
            "source": "test",
            "consumed_at": "2026-08-06T12:30:00+09:00",
        },
    ).status_code == 201
    assert client.post(
        f"/v1/intake-interactions/{interaction_id}/decision-requests",
        json={
            "operation_id": str(uuid.uuid4()),
            "scope": "daily_nutrition",
            "source": "test",
        },
    ).status_code == 201
    durable_events = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type.in_(
                    (
                        "nutrition.intake-outcome.v1",
                        "nutrition.decision-request.v1",
                    )
                )
            )
        )
    )
    for event in durable_events:
        payload = dict(event.payload)
        payload["warnings"] = [secret]
        event.payload = payload
    session.commit()

    run_storage_maintenance(
        session,
        client.app.state.settings,
        now=datetime.now(UTC),
    )
    session.commit()
    session.expire_all()

    assert all(secret not in str(event.payload) for event in durable_events)
