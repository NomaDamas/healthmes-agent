"""Device-neutral intake interaction engine integration tests."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from threading import Event
from time import sleep
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import healthmes.nutrition.intake_service as intake_service_module
from healthmes.api import intake_interactions as intake_api_module
from healthmes.api.intake_interactions import AnalyzeInteractionInput
from healthmes.nutrition.contracts import (
    Confidence,
    Estimate,
    EstimateKind,
    IntakeType,
    ObservationStatus,
)
from healthmes.nutrition.intake_contracts import (
    NUTRIENT_PROVENANCE_VERIFIED_FIELD,
    CaptureModality,
    EvidenceOrigin,
    IntakeIntent,
    IntakeInteractionReview,
    IntakeOutcome,
    IntakeOutcomeStatus,
    IntakeReviewStatus,
)
from healthmes.nutrition.intake_service import (
    IntakeAnalysisInProgress,
    IntakeInteractionError,
    IntakeStorageIntegrityError,
    create_analyzed_interaction,
    get_interaction,
    operation_fingerprint,
    persist_interaction_review,
    persist_outcome,
)
from healthmes.nutrition.operation_integrity import result_payload_digest
from healthmes.nutrition.query import known_caffeine_for_day
from healthmes.nutrition.repository import (
    InvalidInteractionTransitionChain,
    latest_interaction_transitions,
)
from healthmes.nutrition.schema import VLMEstimate, VLMExtraction, VLMItem
from healthmes.nutrition.transcription import TranscriptionResult
from healthmes.nutrition.vision import VisionUnavailable
from healthmes.storage import run_storage_maintenance
from healthmes.store import RetentionPolicy, StorageObject, WellnessEvent

JPEG = b"\xff\xd8\xff\xe0synthetic-coffee"
KST = timezone(timedelta(hours=9))


def _future_intended_consumption() -> str:
    return (
        (datetime.now(UTC) + timedelta(hours=1)).astimezone(KST).replace(microsecond=0).isoformat()
    )


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


def _estimate(exact: float, unit: str, *, basis: str = "owner_statement") -> dict[str, object]:
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
            "captured_at": "2026-08-06T08:30:00+09:00",
            "timezone": "Asia/Seoul",
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
    created = client.post("/v1/intake-interactions", json=_text_interaction())
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
        session.scalars(select(WellnessEvent.event_type).order_by(WellnessEvent.created_at))
    )
    assert event_types == [
        "nutrition.interaction.v1",
        "nutrition.raw-capture.v1",
        "nutrition.operation.v1",
        "nutrition.intake-outcome.v1",
        "nutrition.operation.v1",
        "nutrition.interaction-transition.v1",
    ]
    interaction_event = session.scalar(
        select(WellnessEvent).where(WellnessEvent.event_type == "nutrition.interaction.v1")
    )
    assert interaction_event is not None
    interaction_policy = session.get(RetentionPolicy, interaction_event.retention_policy_id)
    assert interaction_policy is not None
    assert interaction_policy.data_class == "nutrition_observation"
    assert interaction_event.expires_at is not None
    raw_event = session.scalar(
        select(WellnessEvent).where(WellnessEvent.event_type == "nutrition.raw-capture.v1")
    )
    assert raw_event is not None
    raw_policy = session.get(RetentionPolicy, raw_event.retention_policy_id)
    assert raw_policy is not None
    assert raw_policy.data_class == "nutrition_raw_capture"
    assert raw_event.payload["source_text"] == ("닭가슴살 샐러드와 라테를 먹었다")
    assert interaction_event.payload["source_text"] is None
    outcome_event = session.scalar(
        select(WellnessEvent).where(WellnessEvent.event_type == "nutrition.intake-outcome.v1")
    )
    assert outcome_event is not None
    snapshot = outcome_event.payload["intake_snapshot"]
    assert snapshot["items"][0]["name"] == "닭가슴살 샐러드"
    assert snapshot["items"][0]["nutrients"][0]["evidence_text"] is None
    assert snapshot["items"][0]["nutrients"][0]["amount"]["evidence_text"] is None
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


def test_prospective_candidate_builds_context_without_becoming_intake(client, session):
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
            "intended_consumption_at": _future_intended_consumption(),
        },
    )
    assert requested.status_code == 201
    request_id = requested.json()["request_id"]
    session.expire_all()
    candidate_event = session.scalar(
        select(WellnessEvent).where(WellnessEvent.event_type == "nutrition.interaction.v1")
    )
    assert candidate_event is not None
    candidate_policy = session.get(RetentionPolicy, candidate_event.retention_policy_id)
    assert candidate_policy is not None
    assert candidate_policy.data_class == "nutrition_observation"
    assert candidate_event.expires_at is not None

    context = client.get(f"/v1/intake-interactions/decision-requests/{request_id}/context")
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


def test_review_promotes_agent_candidate_to_user_confirmed_context(
    client,
    session,
):
    created = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            intent="ask_before_intake",
            source_text="이 커피는 카페인 120mg 정도야",
            items=[
                {
                    "name": "커피",
                    "intake_type": "beverage",
                    "serving": _estimate(1, "cup"),
                    "nutrients": [
                        {
                            "nutrient": "caffeine",
                            "amount": _estimate(120, "mg"),
                            "confidence": "high",
                            "origin": "agent",
                        }
                    ],
                    "confidence": "high",
                }
            ],
        ),
    )
    assert created.status_code == 201
    interaction_id = created.json()["interaction_id"]
    review_operation_id = str(uuid.uuid4())
    review_body = {
        "operation_id": review_operation_id,
        "status": "confirmed",
        "source": "ios-device",
        "corrected_items": [],
    }
    reviewed = client.post(
        f"/v1/intake-interactions/{interaction_id}/review",
        json=review_body,
    )
    assert reviewed.status_code == 201
    assert reviewed.json()["latest_review"]["status"] == "confirmed"
    assert reviewed.json()["resolved_items"][0]["nutrients"][0]["origin"] == "user"
    retry = client.post(
        f"/v1/intake-interactions/{interaction_id}/review",
        json=review_body,
    )
    assert retry.status_code == 201
    assert retry.json()["latest_review"]["review_id"] == review_operation_id
    conflict = client.post(
        f"/v1/intake-interactions/{interaction_id}/review",
        json={**review_body, "status": "rejected"},
    )
    assert conflict.status_code == 409

    requested = client.post(
        f"/v1/intake-interactions/{interaction_id}/decision-requests",
        json={
            "operation_id": str(uuid.uuid4()),
            "scope": "caffeine_sleep",
            "source": "ios-device",
            "intended_consumption_at": _future_intended_consumption(),
        },
    )
    assert requested.status_code == 201
    context = client.get(
        f"/v1/intake-interactions/decision-requests/{requested.json()['request_id']}/context"
    ).json()
    candidate = context["candidate"]
    assert candidate["latest_review"]["status"] == "confirmed"
    assert candidate["resolved_items"][0]["nutrients"][0]["origin"] == "user"
    review_event = session.scalar(
        select(WellnessEvent).where(WellnessEvent.event_type == "nutrition.interaction-review.v1")
    )
    assert review_event is not None
    interaction_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.interaction.v1",
            WellnessEvent.source_record_id == interaction_id,
        )
    )
    assert interaction_event is not None
    assert review_event.observed_at == interaction_event.observed_at
    assert review_event.recorded_at > review_event.observed_at
    assert str(review_event.id) in context["evidence_event_ids"]
    retention_update = client.put(
        "/v1/storage/settings/nutrition_observation",
        json={"preset": "30d"},
    )
    assert retention_update.status_code == 200
    session.expire_all()
    assert review_event.expires_at == interaction_event.expires_at


def test_review_is_rejected_after_interaction_has_an_outcome(client):
    interaction = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(intent="ask_before_intake"),
    ).json()
    interaction_id = interaction["interaction_id"]
    outcome = client.post(
        f"/v1/intake-interactions/{interaction_id}/outcomes",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "not_consumed",
            "source": "ios-device",
        },
    )
    assert outcome.status_code == 201

    review = client.post(
        f"/v1/intake-interactions/{interaction_id}/review",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "confirmed",
            "source": "ios-device",
        },
    )

    assert review.status_code == 422
    assert "with an outcome cannot be reviewed" in review.text


def test_sqlite_review_commit_serializes_outcome_snapshot(
    client,
    session,
    session_factory,
):
    created = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(intent="ask_before_intake"),
    ).json()
    interaction_id = uuid.UUID(created["interaction_id"])
    interaction = get_interaction(session, interaction_id)
    assert interaction is not None
    original_item = interaction.items[0]
    corrected_fact = replace(
        original_item.nutrients[0],
        amount=Estimate(
            kind=EstimateKind.EXACT,
            unit="g",
            exact=45,
            estimation_basis="owner_statement",
        ),
        origin=EvidenceOrigin.USER,
        evidence_text=None,
    )
    corrected_item = replace(
        original_item,
        nutrients=(corrected_fact,),
    )
    review = IntakeInteractionReview(
        review_id=uuid.uuid4(),
        operation_fingerprint=operation_fingerprint({"fixture": "review-first"}),
        interaction_id=interaction_id,
        status=IntakeReviewStatus.CORRECTED,
        reviewed_at=datetime.now(UTC),
        source="test",
        items=(corrected_item,),
    )
    outcome = IntakeOutcome(
        outcome_id=uuid.uuid4(),
        operation_fingerprint=operation_fingerprint({"fixture": "outcome-after-review"}),
        interaction_id=interaction_id,
        status=IntakeOutcomeStatus.NOT_CONSUMED,
        confirmed_at=datetime.now(UTC),
        source="test",
    )
    review_flushed = Event()
    outcome_started = Event()
    release_review = Event()

    def write_review() -> None:
        with session_factory() as writer:
            persist_interaction_review(writer, review)
            review_flushed.set()
            assert release_review.wait(timeout=5)
            writer.commit()

    def write_outcome() -> None:
        assert review_flushed.wait(timeout=5)
        with session_factory() as writer:
            writer.connection().exec_driver_sql("PRAGMA busy_timeout=1")
            outcome_started.set()
            persist_outcome(writer, outcome)
            writer.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        review_future = pool.submit(write_review)
        outcome_future = pool.submit(write_outcome)
        assert review_flushed.wait(timeout=5)
        assert outcome_started.wait(timeout=5)
        sleep(0.05)
        release_review.set()
        review_future.result(timeout=5)
        outcome_future.result(timeout=5)

    session.expire_all()
    outcome_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.intake-outcome.v1",
            WellnessEvent.source_record_id == str(outcome.outcome_id),
        )
    )
    assert outcome_event is not None
    snapshot_nutrient = outcome_event.payload["intake_snapshot"]["items"][0]["nutrients"][0]
    assert snapshot_nutrient["amount"]["exact"] == 45
    assert snapshot_nutrient["origin"] == "user"


def test_sqlite_outcome_commit_closes_concurrent_review(
    client,
    session,
    session_factory,
):
    created = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(intent="ask_before_intake"),
    ).json()
    interaction_id = uuid.UUID(created["interaction_id"])
    outcome = IntakeOutcome(
        outcome_id=uuid.uuid4(),
        operation_fingerprint=operation_fingerprint({"fixture": "outcome-first"}),
        interaction_id=interaction_id,
        status=IntakeOutcomeStatus.NOT_CONSUMED,
        confirmed_at=datetime.now(UTC),
        source="test",
    )
    review = IntakeInteractionReview(
        review_id=uuid.uuid4(),
        operation_fingerprint=operation_fingerprint({"fixture": "review-after-outcome"}),
        interaction_id=interaction_id,
        status=IntakeReviewStatus.CONFIRMED,
        reviewed_at=datetime.now(UTC),
        source="test",
    )
    outcome_flushed = Event()
    review_started = Event()
    release_outcome = Event()

    def write_outcome() -> None:
        with session_factory() as writer:
            persist_outcome(writer, outcome)
            outcome_flushed.set()
            assert release_outcome.wait(timeout=5)
            writer.commit()

    def write_review() -> str:
        assert outcome_flushed.wait(timeout=5)
        with session_factory() as writer:
            writer.connection().exec_driver_sql("PRAGMA busy_timeout=1")
            review_started.set()
            try:
                persist_interaction_review(writer, review)
            except IntakeInteractionError as exc:
                writer.rollback()
                return str(exc)
            raise AssertionError("terminal outcome must close review")

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcome_future = pool.submit(write_outcome)
        review_future = pool.submit(write_review)
        assert outcome_flushed.wait(timeout=5)
        assert review_started.wait(timeout=5)
        sleep(0.05)
        release_outcome.set()
        outcome_future.result(timeout=5)
        review_error = review_future.result(timeout=5)

    assert "with an outcome cannot be reviewed" in review_error
    session.expire_all()
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == "nutrition.interaction-review.v1",
                WellnessEvent.source_record_id == str(review.review_id),
            )
        )
        is None
    )


def test_latest_review_uses_transition_revision_and_does_not_resurface(
    client,
    session,
):
    created = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(intent="ask_before_intake"),
    ).json()
    interaction_id = uuid.UUID(created["interaction_id"])
    first = IntakeInteractionReview(
        review_id=uuid.uuid4(),
        operation_fingerprint=operation_fingerprint({"fixture": "review-revision-1"}),
        interaction_id=interaction_id,
        status=IntakeReviewStatus.CONFIRMED,
        reviewed_at=datetime(2026, 8, 6, 5, tzinfo=UTC),
        source="test",
    )
    second = IntakeInteractionReview(
        review_id=uuid.uuid4(),
        operation_fingerprint=operation_fingerprint({"fixture": "review-revision-2"}),
        interaction_id=interaction_id,
        status=IntakeReviewStatus.REJECTED,
        reviewed_at=datetime(2026, 8, 6, 4, tzinfo=UTC),
        source="test",
    )

    persist_interaction_review(session, first)
    session.commit()
    persist_interaction_review(session, second)
    session.commit()

    current = client.get(f"/v1/intake-interactions/{interaction_id}").json()
    assert current["latest_review"]["review_id"] == str(second.review_id)
    assert current["resolved_items"] == []

    second_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.interaction-review.v1",
            WellnessEvent.source_record_id == str(second.review_id),
        )
    )
    assert second_event is not None
    session.delete(second_event)
    session.commit()

    current = client.get(f"/v1/intake-interactions/{interaction_id}")
    assert current.status_code == 500
    assert current.json()["error"]["code"] == ("intake_storage_integrity_error")
    assert "review result is unavailable" in current.text


def test_latest_outcome_uses_transition_revision_and_does_not_resurface(
    client,
    session,
):
    created = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
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
    ).json()
    interaction_id = uuid.UUID(created["interaction_id"])
    first = IntakeOutcome(
        outcome_id=uuid.uuid4(),
        operation_fingerprint=operation_fingerprint({"fixture": "outcome-revision-1"}),
        interaction_id=interaction_id,
        status=IntakeOutcomeStatus.CONSUMED,
        confirmed_at=datetime(2026, 8, 6, 5, tzinfo=UTC),
        source="test",
        consumed_at=datetime(2026, 8, 6, 4, tzinfo=UTC),
    )
    second = IntakeOutcome(
        outcome_id=uuid.uuid4(),
        operation_fingerprint=operation_fingerprint({"fixture": "outcome-revision-2"}),
        interaction_id=interaction_id,
        status=IntakeOutcomeStatus.NOT_CONSUMED,
        confirmed_at=datetime(2026, 8, 6, 3, tzinfo=UTC),
        source="test",
    )

    persist_outcome(session, first)
    session.commit()
    persist_outcome(session, second)
    session.commit()

    current = client.get(f"/v1/intake-interactions/{interaction_id}").json()
    assert current["latest_outcome"]["outcome_id"] == str(second.outcome_id)
    assert current["latest_outcome"]["status"] == "not_consumed"
    caffeine = known_caffeine_for_day(
        session,
        local_date=date(2026, 8, 6),
        timezone="Asia/Seoul",
    )
    assert caffeine["confirmed_caffeine_mg"] == 0
    assert caffeine["evidence"][0]["outcome_id"] == str(second.outcome_id)
    assert caffeine["evidence"][0]["status"] == "not_consumed"

    second_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.intake-outcome.v1",
            WellnessEvent.source_record_id == str(second.outcome_id),
        )
    )
    assert second_event is not None
    session.delete(second_event)
    session.commit()

    current = client.get(f"/v1/intake-interactions/{interaction_id}")
    assert current.status_code == 500
    assert current.json()["error"]["code"] == ("intake_storage_integrity_error")
    assert "outcome result is unavailable" in current.text
    confirmed = client.get(
        "/v1/intake-interactions",
        params={"confirmed_only": True},
    )
    assert confirmed.status_code == 500
    caffeine = known_caffeine_for_day(
        session,
        local_date=date(2026, 8, 6),
        timezone="Asia/Seoul",
    )
    assert caffeine["status"] == "incomplete"
    assert caffeine["confirmed_caffeine_mg"] == 0
    assert caffeine["evidence"] == []
    assert caffeine["unavailable_outcome_operation_ids"] == [str(second.outcome_id)]


def test_expired_prospective_consumption_cannot_reconfirm_empty_day(
    client,
    session,
):
    created = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            intent="ask_before_intake",
            items=[
                {
                    "name": "커피",
                    "intake_type": "beverage",
                    "serving": _estimate(1, "cup"),
                    "nutrients": [
                        {
                            "nutrient": "caffeine",
                            "amount": _estimate(120, "mg"),
                            "confidence": "high",
                            "origin": "user",
                        }
                    ],
                    "confidence": "high",
                }
            ],
        ),
    ).json()
    interaction_id = created["interaction_id"]
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
    outcome_id = outcome.json()["latest_outcome"]["outcome_id"]
    initial_confirmation = client.post(
        "/v1/nutrition-observations/daily-confirmations",
        json={
            "operation_id": str(uuid.uuid4()),
            "local_date": "2026-08-06",
            "timezone": "Asia/Seoul",
            "observation_ids": [],
            "outcome_ids": [outcome_id],
            "total_intake_complete": True,
            "source": "desktop-web",
        },
    )
    assert initial_confirmation.status_code == 201

    outcome_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.intake-outcome.v1",
            WellnessEvent.source_record_id == outcome_id,
        )
    )
    assert outcome_event is not None
    session.delete(outcome_event)
    session.commit()

    empty_reconfirmation = client.post(
        "/v1/nutrition-observations/daily-confirmations",
        json={
            "operation_id": str(uuid.uuid4()),
            "local_date": "2026-08-06",
            "timezone": "Asia/Seoul",
            "observation_ids": [],
            "outcome_ids": [],
            "total_intake_complete": True,
            "source": "desktop-web",
        },
    )
    assert empty_reconfirmation.status_code == 422
    assert "result payload is unavailable" in empty_reconfirmation.text

    ledger = known_caffeine_for_day(
        session,
        local_date=date(2026, 8, 6),
        timezone="Asia/Seoul",
    )
    assert ledger["status"] == "incomplete"
    assert ledger["total_intake_complete"] is False
    assert ledger["confirmed_caffeine_mg"] == 0
    assert ledger["unavailable_outcome_operation_ids"] == [outcome_id]


def test_followup_outcome_inherits_previous_nutrition_correction(
    client,
):
    created = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
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
    ).json()
    interaction_id = created["interaction_id"]
    corrected_item = {
        "name": "커피",
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
    first = client.post(
        f"/v1/intake-interactions/{interaction_id}/outcomes",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "consumed",
            "source": "ios-device",
            "consumed_at": "2026-08-06T12:30:00+09:00",
            "corrected_items": [corrected_item],
        },
    )
    assert first.status_code == 201
    assert first.json()["resolved_items"][0]["nutrients"][0]["amount"]["exact"] == 80

    second = client.post(
        f"/v1/intake-interactions/{interaction_id}/outcomes",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "consumed",
            "source": "ios-device",
            "consumed_at": "2026-08-06T13:00:00+09:00",
        },
    )
    assert second.status_code == 201
    assert second.json()["resolved_items"][0]["nutrients"][0]["amount"]["exact"] == 80


def test_caffeine_decision_requires_snapshot_time_in_interaction_timezone(
    client,
):
    interaction = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(intent="ask_before_intake"),
    ).json()
    path = f"/v1/intake-interactions/{interaction['interaction_id']}/decision-requests"
    base = {
        "operation_id": str(uuid.uuid4()),
        "scope": "caffeine_sleep",
        "source": "ios-device",
    }

    missing = client.post(path, json=base)
    assert missing.status_code == 422
    assert "require intended_consumption_at" in missing.text

    conflicting = client.post(
        path,
        json={
            **base,
            "operation_id": str(uuid.uuid4()),
            "intended_consumption_at": "2026-08-06T16:00:00-07:00",
        },
    )
    assert conflicting.status_code == 422
    assert "UTC offset conflicts with the interaction timezone" in (conflicting.text)

    stale = client.post(
        path,
        json={
            **base,
            "operation_id": str(uuid.uuid4()),
            "intended_consumption_at": (datetime.now(UTC) - timedelta(minutes=10))
            .astimezone(KST)
            .isoformat(),
        },
    )
    assert stale.status_code == 422
    assert "cannot be more than 5 minutes in the past" in stale.text

    within_clock_skew = client.post(
        path,
        json={
            **base,
            "operation_id": str(uuid.uuid4()),
            "intended_consumption_at": (datetime.now(UTC) - timedelta(minutes=1))
            .astimezone(KST)
            .isoformat(),
        },
    )
    assert within_clock_skew.status_code == 201


def test_caffeine_context_includes_confirmed_text_outcome_evidence(
    client,
    session,
    monkeypatch,
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
    review = client.post(
        f"/v1/intake-interactions/{interaction_id}/review",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "confirmed",
            "source": "ios-device",
            "corrected_items": [],
        },
    )
    assert review.status_code == 201
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
            "operation_id": str(uuid.uuid4()),
            "local_date": "2026-08-06",
            "timezone": "Asia/Seoul",
            "observation_ids": [],
            "outcome_ids": [outcome.json()["latest_outcome"]["outcome_id"]],
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
    request_now = datetime(2026, 8, 6, 15, tzinfo=KST).astimezone(UTC)
    monkeypatch.setattr(
        intake_api_module,
        "utc_now",
        lambda: request_now,
    )
    requested = client.post(
        f"/v1/intake-interactions/{candidate.json()['interaction_id']}/decision-requests",
        json={
            "operation_id": str(uuid.uuid4()),
            "scope": "caffeine_sleep",
            "source": "ios-device",
            "intended_consumption_at": "2026-08-06T16:00:00+09:00",
        },
    )
    assert requested.status_code == 201
    context = client.get(
        f"/v1/intake-interactions/decision-requests/{requested.json()['request_id']}/context"
    )
    assert context.status_code == 200
    caffeine = context.json()["specialized_evidence"]["caffeine"]
    assert caffeine["status"] == "known"
    assert caffeine["confirmed_caffeine_mg"] == 150
    outcome_event = session.scalar(
        select(WellnessEvent).where(WellnessEvent.event_type == "nutrition.intake-outcome.v1")
    )
    assert outcome_event is not None
    assert str(outcome_event.id) in context.json()["evidence_event_ids"]


def test_unreviewed_caller_claimed_caffeine_is_not_known_intake(
    client,
    session,
):
    consumed = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            source_text="카페인 500mg 커피를 마셨어",
            items=[
                {
                    "name": "커피",
                    "intake_type": "beverage",
                    "serving": _estimate(1, "cup"),
                    "nutrients": [
                        {
                            "nutrient": "caffeine",
                            "amount": _estimate(500, "mg"),
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
    outcome = client.post(
        f"/v1/intake-interactions/{consumed.json()['interaction_id']}/outcomes",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "consumed",
            "source": "ios-device",
            "consumed_at": "2026-08-06T12:30:00+09:00",
        },
    )
    assert outcome.status_code == 201
    outcome_id = outcome.json()["latest_outcome"]["outcome_id"]
    daily = client.post(
        "/v1/nutrition-observations/daily-confirmations",
        json={
            "operation_id": str(uuid.uuid4()),
            "local_date": "2026-08-06",
            "timezone": "Asia/Seoul",
            "observation_ids": [],
            "outcome_ids": [outcome_id],
            "total_intake_complete": True,
            "source": "desktop-web",
        },
    )
    assert daily.status_code == 201

    session.expire_all()
    caffeine = known_caffeine_for_day(
        session,
        local_date=date(2026, 8, 6),
        timezone="Asia/Seoul",
    )
    assert caffeine["status"] == "incomplete"
    assert caffeine["confirmed_caffeine_mg"] == 0
    assert caffeine["unquantified_outcome_ids"] == [outcome_id]

    outcome_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.intake-outcome.v1",
            WellnessEvent.source_record_id == outcome_id,
        )
    )
    marker = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.operation.v1",
            WellnessEvent.source_record_id == f"intake-outcome:{outcome_id}",
        )
    )
    assert outcome_event is not None
    assert marker is not None
    legacy_payload = deepcopy(outcome_event.payload)
    legacy_payload.pop(NUTRIENT_PROVENANCE_VERIFIED_FIELD)
    legacy_payload["intake_snapshot"]["items"][0]["nutrients"][0]["origin"] = "user"
    outcome_event.payload = legacy_payload
    session.delete(marker)
    session.commit()

    run_storage_maintenance(
        session,
        client.app.state.settings,
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    session.commit()
    session.expire_all()
    legacy_caffeine = known_caffeine_for_day(
        session,
        local_date=date(2026, 8, 6),
        timezone="Asia/Seoul",
    )
    assert legacy_caffeine["status"] == "incomplete"
    assert legacy_caffeine["confirmed_caffeine_mg"] == 0
    assert legacy_caffeine["unquantified_outcome_ids"] == [outcome_id]


def test_unresolved_log_consumed_interaction_blocks_complete_day(
    client,
    session,
):
    interaction = client.post(
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
    assert interaction.status_code == 201
    interaction_id = interaction.json()["interaction_id"]

    daily = client.post(
        "/v1/nutrition-observations/daily-confirmations",
        json={
            "operation_id": str(uuid.uuid4()),
            "local_date": "2026-08-06",
            "timezone": "Asia/Seoul",
            "observation_ids": [],
            "outcome_ids": [],
            "total_intake_complete": True,
            "source": "desktop-web",
        },
    )

    assert daily.status_code == 422
    assert "requires an outcome for every log_consumed interaction" in (daily.text)
    session.expire_all()
    known = known_caffeine_for_day(
        session,
        local_date=date(2026, 8, 6),
        timezone="Asia/Seoul",
    )
    assert known["status"] == "incomplete"
    assert known["total_intake_complete"] is False
    assert known["unresolved_log_consumed_interaction_ids"] == [interaction_id]


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


def test_voice_capture_requires_local_transcript_and_indexes_audio(client, session):
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
    obj = session.scalar(select(StorageObject).where(StorageObject.relative_path == media_path))
    assert obj is not None
    assert obj.data_class == "nutrition_media"


def test_uploaded_media_cannot_be_reused_by_another_capture(client):
    media_path = _upload(client, b"fake-m4a", "audio/m4a", "meal.m4a")
    first = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            modality="voice",
            source_text="아침에 바나나와 우유를 먹었어",
            media_path=media_path,
            items=[],
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
        ),
    )

    assert second.status_code == 422
    assert "already belongs to another capture" in second.text


def test_free_text_is_automatically_analyzed_and_retry_is_idempotent(client, session):
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
    assert provider.calls == [("아침에 바나나와 우유를 먹었어", False)]

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


def test_analysis_failure_does_not_commit_or_rollback_caller_session(client, session):
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


def test_analysis_reservation_does_not_commit_flushed_caller_state(client, session):
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
            operation_fingerprint=operation_fingerprint({"fixture": "flushed-input"}),
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
            select(RetentionPolicy).where(RetentionPolicy.data_class == "unrelated_flushed")
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


def test_expired_analysis_lease_has_one_concurrent_reclaimer(client, session):
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


def test_final_transaction_rollback_releases_analysis_reservation(client, session):
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
    fingerprint = operation_fingerprint({"fixture": "savepoint-rollback"})

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
        operation_fingerprint=operation_fingerprint({"fixture": "failed-final-commit"}),
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
            with intake_service_module._STATIC_ANALYSIS_RESERVATIONS_LOCK:
                reservation = intake_service_module._STATIC_ANALYSIS_RESERVATIONS[key]
                reservation["lease_expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
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


def test_persisting_sqlite_reservation_cannot_be_reclaimed(client, session):
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
        intake_service_module._STATIC_ANALYSIS_RESERVATIONS[key]["lease_expires_at"] = datetime.now(
            UTC
        ) - timedelta(seconds=1)

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


def test_persistent_reservation_completion_uses_token_cas(client, session, monkeypatch):
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
    stale_marker = session.scalar(select(WellnessEvent).where(WellnessEvent.id == marker.id))
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


def test_voice_is_transcribed_locally_then_automatically_analyzed(client, session):
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
            "observed_at": "2026-08-06T08:30:00+09:00",
            "timezone": "Asia/Seoul",
            "source": "android-device",
            "media_path": media_path,
            "allow_remote_analysis": False,
        },
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["source_text"] == "아침에 바나나와 우유를 먹었어"
    assert payload["analysis_provenance"]["transcription_provider"] == ("fixture-whisper")
    assert payload["analysis_provenance"]["transcription_model"] == ("fixture-small")
    assert len(transcriber.calls) == 1
    assert provider.calls == [("아침에 바나나와 우유를 먹었어", False)]
    obj = session.scalar(select(StorageObject).where(StorageObject.relative_path == media_path))
    assert obj is not None
    assert obj.data_class == "nutrition_media"


def test_photo_adapter_keeps_sake_observation_and_maps_caffeine(client, session):
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
        nutrient["origin"] for item in created.json()["items"] for nutrient in item["nutrients"]
    } == {"user"}


def test_photo_review_correction_flows_into_interaction_search_and_context(client, session):
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
                "serving": _estimate(250, "ml", basis="owner_correction"),
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
    assert conflict.json()["error"]["code"] == ("nutrition_review_operation_conflict")
    assert "operation_id was already used" in conflict.text
    review_view = client.get(f"/v1/nutrition-observations/{observation_id}/review")
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
    nutrients = {value["nutrient"]: value for value in item["nutrients"]}
    assert nutrients["energy"]["amount"]["exact"] == 80
    assert nutrients["caffeine"]["amount"]["exact"] == 95
    assert nutrients["caffeine"]["origin"] == "user"

    searched = client.get(
        "/v1/intake-interactions",
        params={"nutrient": "energy"},
    )
    assert searched.status_code == 200
    assert searched.json()["records"][0]["resolved_items"][0]["name"] == ("small bottled latte")
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
        f"/v1/intake-interactions/decision-requests/{decision_request.json()['request_id']}/context"
    )
    assert context.status_code == 200
    review_event = session.scalar(
        select(WellnessEvent).where(WellnessEvent.event_type == "nutrition.review.v1")
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
    assert review_event.retention_policy_id == (observation_event.retention_policy_id)
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
    latest_review = client.get(f"/v1/nutrition-observations/{observation_id}/review")
    assert latest_review.status_code == 200
    assert latest_review.json()["review"]["review_id"] == (reviewed.json()["review_id"])

    outcome = client.post(
        f"/v1/intake-interactions/{created.json()['interaction_id']}/outcomes",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "consumed",
            "source": "galaxy-device",
            "consumed_at": "2026-08-06T12:30:00+09:00",
        },
    )
    assert outcome.status_code == 201
    outcome_id = outcome.json()["latest_outcome"]["outcome_id"]
    daily = client.post(
        "/v1/nutrition-observations/daily-confirmations",
        json={
            "operation_id": str(uuid.uuid4()),
            "local_date": "2026-08-06",
            "timezone": "Asia/Seoul",
            "observation_ids": [],
            "outcome_ids": [outcome_id],
            "total_intake_complete": True,
            "source": "desktop-web",
        },
    )
    assert daily.status_code == 201
    session.expire_all()
    caffeine = known_caffeine_for_day(
        session,
        local_date=date(2026, 8, 6),
        timezone="Asia/Seoul",
    )
    assert caffeine["status"] == "known"
    assert caffeine["confirmed_caffeine_mg"] == 95
    assert caffeine["evidence"][0]["event_type"] == ("nutrition.intake-outcome.v1")
    assert caffeine["evidence"][0]["outcome_id"] == outcome_id
    session.rollback()

    comparison_primary = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            intent="compare_option",
            source_text="이 식사와 라테를 비교해줘",
        ),
    )
    assert comparison_primary.status_code == 201
    comparison_request = client.post(
        f"/v1/intake-interactions/{comparison_primary.json()['interaction_id']}/decision-requests",
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
    assert str(review_event.id) in (comparison_context.json()["evidence_event_ids"])
    assert str(colliding_review.id) not in (comparison_context.json()["evidence_event_ids"])


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
                    "serving": _estimate(250, "ml", basis="owner_correction"),
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
    logged = client.post("/v1/intake-interactions", json=_text_interaction()).json()
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
    interaction = client.post("/v1/intake-interactions", json=_text_interaction()).json()
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


def test_operation_id_is_idempotent_and_conflicts_on_different_input(client, session):
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
    request_path = f"/v1/intake-interactions/{interaction_id}/decision-requests"
    request = client.post(request_path, json=request_body)
    retry_request = client.post(request_path, json=request_body)
    assert request.status_code == retry_request.status_code == 201
    assert request.json()["request_id"] == retry_request.json()["request_id"]
    request_conflict = client.post(request_path, json={**request_body, "lookback_days": 7})
    assert request_conflict.status_code == 409

    request_id = request.json()["request_id"]
    context = client.get(f"/v1/intake-interactions/decision-requests/{request_id}/context").json()
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
    decision_conflict = client.post(decision_path, json={**decision_body, "summary": "다른 판단"})
    assert decision_conflict.status_code == 409


def test_transition_runtime_uses_canonical_source_namespace(
    client,
    session,
):
    interaction = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(intent="ask_before_intake"),
    ).json()
    interaction_id = uuid.UUID(interaction["interaction_id"])
    foreign_interaction_id = uuid.uuid4()
    rogue_transition = WellnessEvent(
        event_type="nutrition.interaction-transition.v1",
        schema_version=1,
        observed_at=datetime.now(UTC),
        recorded_at=datetime.now(UTC),
        timezone="Asia/Seoul",
        source_provider="nutrition-interaction-transition",
        source_device=None,
        source_record_id=f"{foreign_interaction_id}:1",
        capture_method="system",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=None,
        payload={
            "interaction_id": str(interaction_id),
            "revision": 1,
            "mutation_kind": "outcome",
            "operation_id": str(uuid.uuid4()),
            "mutation_status": "consumed",
        },
        derived_from={"interaction_id": str(interaction_id)},
    )
    session.add(rogue_transition)
    session.commit()

    assert (
        intake_service_module.terminal_outcome_status(
            session,
            interaction_id,
        )
        is None
    )
    assert (
        latest_interaction_transitions(
            session,
            mutation_kind="outcome",
            interaction_ids={interaction_id},
        )
        == {}
    )

    outcome_id = str(uuid.uuid4())
    response = client.post(
        f"/v1/intake-interactions/{interaction_id}/outcomes",
        json={
            "operation_id": outcome_id,
            "status": "not_consumed",
            "source": "ios-device",
        },
    )

    assert response.status_code == 201
    canonical = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.source_provider == "nutrition-interaction-transition",
            WellnessEvent.source_record_id == f"{interaction_id}:1",
        )
    )
    assert canonical is not None
    assert canonical.payload["operation_id"] == outcome_id
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.source_provider == "nutrition-interaction-transition",
                WellnessEvent.source_record_id == f"{interaction_id}:2",
            )
        )
        is None
    )


@pytest.mark.parametrize(
    "corruption",
    (
        "operation_id",
        "mutation_kind",
        "mutation_status",
        "revision_gap",
    ),
)
def test_transition_runtime_rejects_invalid_semantic_chain(
    client,
    session,
    corruption,
):
    interaction = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(intent="ask_before_intake"),
    ).json()
    interaction_id = uuid.UUID(interaction["interaction_id"])
    revision = 2 if corruption == "revision_gap" else 1
    mutation_kind = "unknown" if corruption == "mutation_kind" else "outcome"
    mutation_status = "unknown" if corruption == "mutation_status" else "consumed"
    operation_id = "not-a-uuid" if corruption == "operation_id" else str(uuid.uuid4())
    event = WellnessEvent(
        event_type="nutrition.interaction-transition.v1",
        schema_version=1,
        observed_at=datetime.now(UTC),
        recorded_at=datetime.now(UTC),
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
            "interaction_id": str(interaction_id),
            "revision": revision,
            "mutation_kind": mutation_kind,
            "operation_id": operation_id,
            "mutation_status": mutation_status,
        },
        derived_from={"interaction_id": str(interaction_id)},
    )
    session.add(event)
    session.commit()

    with pytest.raises(
        IntakeInteractionError,
        match="invalid interaction transition chain",
    ):
        intake_service_module.terminal_outcome_status(
            session,
            interaction_id,
        )
    with pytest.raises(InvalidInteractionTransitionChain):
        latest_interaction_transitions(
            session,
            mutation_kind="outcome",
            interaction_ids={interaction_id},
        )
    with pytest.raises(
        IntakeInteractionError,
        match="invalid interaction transition chain",
    ):
        intake_service_module._next_interaction_transition_revision(
            session,
            interaction_id,
        )

    response = client.post(
        f"/v1/intake-interactions/{interaction_id}/outcomes",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "not_consumed",
            "source": "ios-device",
        },
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "intake_storage_integrity_error"
    assert "invalid interaction transition chain" in response.text
    namespace_record_ids = list(
        session.scalars(
            select(WellnessEvent.source_record_id).where(
                WellnessEvent.source_provider == "nutrition-interaction-transition",
                WellnessEvent.source_record_id.like(f"{interaction_id}:%"),
            )
        )
    )
    assert namespace_record_ids == [f"{interaction_id}:{revision}"]


def test_transition_runtime_rejects_review_after_outcome(
    client,
    session,
):
    interaction = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(intent="ask_before_intake"),
    ).json()
    interaction_id = uuid.UUID(interaction["interaction_id"])
    recorded_at = datetime.now(UTC)

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
                "interaction_id": str(interaction_id),
                "revision": revision,
                "mutation_kind": mutation_kind,
                "operation_id": str(uuid.uuid4()),
                "mutation_status": mutation_status,
            },
            derived_from={"interaction_id": str(interaction_id)},
        )

    session.add_all(
        (
            transition(1, "outcome", "not_consumed"),
            transition(2, "review", "confirmed"),
        )
    )
    session.commit()

    with pytest.raises(
        IntakeInteractionError,
        match="invalid interaction transition chain",
    ):
        intake_service_module.terminal_outcome_status(
            session,
            interaction_id,
        )
    with pytest.raises(InvalidInteractionTransitionChain):
        latest_interaction_transitions(
            session,
            mutation_kind="review",
            interaction_ids={interaction_id},
        )

    for path in (
        f"/v1/intake-interactions/{interaction_id}",
        "/v1/intake-interactions",
    ):
        response = client.get(path)
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "intake_storage_integrity_error"

    response = client.post(
        f"/v1/intake-interactions/{interaction_id}/outcomes",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "cancelled",
            "source": "ios-device",
        },
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "intake_storage_integrity_error"


@pytest.mark.parametrize(
    (
        "mutation_kind",
        "write_path",
        "write_body",
        "event_type",
        "operation_prefix",
        "mutated_status",
    ),
    (
        (
            "review",
            "review",
            {
                "status": "confirmed",
                "source": "ios-device",
                "corrected_items": [],
            },
            "nutrition.interaction-review.v1",
            "intake-review",
            "rejected",
        ),
        (
            "outcome",
            "outcomes",
            {
                "status": "consumed",
                "source": "ios-device",
                "consumed_at": "2026-08-06T12:30:00+09:00",
            },
            "nutrition.intake-outcome.v1",
            "intake-outcome",
            "not_consumed",
        ),
    ),
)
def test_transition_status_must_match_result_payload(
    client,
    session,
    mutation_kind,
    write_path,
    write_body,
    event_type,
    operation_prefix,
    mutated_status,
):
    interaction = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            intent=("ask_before_intake" if mutation_kind == "review" else "log_consumed")
        ),
    ).json()
    interaction_id = interaction["interaction_id"]
    operation_id = str(uuid.uuid4())
    response = client.post(
        f"/v1/intake-interactions/{interaction_id}/{write_path}",
        json={"operation_id": operation_id, **write_body},
    )
    assert response.status_code == 201
    result = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == event_type,
            WellnessEvent.source_record_id == operation_id,
        )
    )
    marker = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.source_provider == "nutrition-operation",
            WellnessEvent.source_record_id == f"{operation_prefix}:{operation_id}",
        )
    )
    assert result is not None
    assert marker is not None
    result.payload = {
        **result.payload,
        "status": mutated_status,
        "consumed_at": (None if mutation_kind == "outcome" else result.payload.get("consumed_at")),
    }
    marker.payload = {
        **marker.payload,
        "result_payload_sha256": result_payload_digest(result.payload),
    }
    session.commit()

    fetched = client.get(f"/v1/intake-interactions/{interaction_id}")

    assert fetched.status_code == 500
    assert fetched.json()["error"]["code"] == "intake_storage_integrity_error"


@pytest.mark.parametrize("mutation_kind", ("review", "outcome"))
def test_quarantined_interaction_result_is_not_authoritative(
    client,
    session,
    mutation_kind,
):
    interaction = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            intent=("ask_before_intake" if mutation_kind == "review" else "log_consumed")
        ),
    ).json()
    interaction_id = interaction["interaction_id"]
    if mutation_kind == "review":
        response = client.post(
            f"/v1/intake-interactions/{interaction_id}/review",
            json={
                "operation_id": str(uuid.uuid4()),
                "status": "confirmed",
                "source": "ios-device",
            },
        )
        event_type = "nutrition.interaction-review.v1"
    else:
        response = client.post(
            f"/v1/intake-interactions/{interaction_id}/outcomes",
            json={
                "operation_id": str(uuid.uuid4()),
                "status": "consumed",
                "source": "ios-device",
                "consumed_at": "2026-08-06T12:30:00+09:00",
            },
        )
        event_type = "nutrition.intake-outcome.v1"
    assert response.status_code == 201
    result = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == event_type,
        )
    )
    assert result is not None
    result.quality_flags = {"maintenance_quarantine": "legacy_operation_result_identity_invalid"}
    session.commit()

    fetched = client.get(f"/v1/intake-interactions/{interaction_id}")

    assert fetched.status_code == 500
    assert fetched.json()["error"]["code"] == "intake_storage_integrity_error"


def test_malformed_stored_interaction_payload_returns_storage_integrity_error(
    client,
    session,
):
    interaction = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(),
    ).json()
    event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.interaction.v1",
            WellnessEvent.source_record_id == interaction["interaction_id"],
        )
    )
    assert event is not None
    event.payload = {**event.payload, "items": "malformed"}
    session.commit()

    fetched = client.get(f"/v1/intake-interactions/{interaction['interaction_id']}")

    assert fetched.status_code == 500
    assert fetched.json()["error"]["code"] == "intake_storage_integrity_error"


@pytest.mark.parametrize(
    (
        "operation_kind",
        "operation_prefix",
        "event_type",
        "source_provider",
        "operation_id_field",
    ),
    (
        (
            "intake_interaction_review",
            "intake-review",
            "nutrition.interaction-review.v1",
            "nutrition-intake-review",
            "review_id",
        ),
        (
            "intake_outcome",
            "intake-outcome",
            "nutrition.intake-outcome.v1",
            "nutrition-intake-outcome",
            "outcome_id",
        ),
        (
            "intake_decision_request",
            "intake-decision-request",
            "nutrition.decision-request.v1",
            "nutrition-decision-request",
            "request_id",
        ),
        (
            "intake_decision",
            "intake-decision",
            "nutrition.decision.v1",
            "nutrition-decision",
            "decision_id",
        ),
    ),
)
@pytest.mark.parametrize("corruption", ("payload_identity", "quarantine"))
def test_completed_operation_retry_rejects_invalid_result_identity(
    session,
    operation_kind,
    operation_prefix,
    event_type,
    source_provider,
    operation_id_field,
    corruption,
):
    operation_id = uuid.uuid4()
    fingerprint = operation_fingerprint({"fixture": operation_kind})
    recorded_at = datetime.now(UTC)
    event = WellnessEvent(
        event_type=event_type,
        schema_version=1,
        observed_at=recorded_at,
        recorded_at=recorded_at,
        timezone="Asia/Seoul",
        source_provider=source_provider,
        source_device="legacy-fixture",
        source_record_id=str(operation_id),
        capture_method="manual",
        quality_flags=(
            {"maintenance_quarantine": "legacy_operation_result_identity_invalid"}
            if corruption == "quarantine"
            else None
        ),
        confidence=1.0,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=recorded_at + timedelta(days=1),
        payload={
            operation_id_field: str(
                uuid.uuid4() if corruption == "payload_identity" else operation_id
            ),
            "operation_fingerprint": fingerprint,
        },
        derived_from=None,
    )
    session.add(event)
    session.commit()

    with pytest.raises(
        intake_service_module.IntakeOperationConflict,
        match="identity is invalid",
    ):
        intake_service_module._existing_completed_operation_result(
            session,
            operation_id=operation_id,
            operation_fingerprint=fingerprint,
            operation_kind=operation_kind,
            operation_name=operation_kind,
            operation_prefix=operation_prefix,
            result_event_type=event_type,
            result_source_provider=source_provider,
        )


def test_completed_operation_retry_rejects_processing_marker(
    session,
):
    operation_id = uuid.uuid4()
    fingerprint = operation_fingerprint({"fixture": "processing-marker"})
    recorded_at = datetime.now(UTC)
    result = WellnessEvent(
        event_type="nutrition.decision.v1",
        schema_version=1,
        observed_at=recorded_at,
        recorded_at=recorded_at,
        timezone=None,
        source_provider="nutrition-decision",
        source_device="legacy-fixture",
        source_record_id=str(operation_id),
        capture_method="agent",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=recorded_at + timedelta(days=1),
        payload={
            "decision_id": str(operation_id),
            "operation_fingerprint": fingerprint,
        },
        derived_from=None,
    )
    marker = WellnessEvent(
        event_type="nutrition.operation.v1",
        schema_version=1,
        observed_at=recorded_at,
        recorded_at=recorded_at,
        timezone=None,
        source_provider="nutrition-operation",
        source_device=None,
        source_record_id=f"intake-decision:{operation_id}",
        capture_method="system",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=None,
        payload={
            "operation_kind": "intake_decision",
            "operation_id": str(operation_id),
            "operation_fingerprint": fingerprint,
            "operation_state": "processing",
        },
        derived_from=None,
    )
    session.add_all((result, marker))
    session.commit()

    with pytest.raises(
        intake_service_module.IntakeOperationConflict,
        match="same operation scope",
    ):
        intake_service_module._existing_completed_operation_result(
            session,
            operation_id=operation_id,
            operation_fingerprint=fingerprint,
            operation_kind="intake_decision",
            operation_name="intake decision",
            operation_prefix="intake-decision",
            result_event_type="nutrition.decision.v1",
            result_source_provider="nutrition-decision",
        )


@pytest.mark.parametrize(
    (
        "operation_kind",
        "operation_prefix",
        "event_type",
        "source_provider",
        "operation_id_field",
    ),
    (
        (
            "intake_interaction_review",
            "intake-review",
            "nutrition.interaction-review.v1",
            "nutrition-intake-review",
            "review_id",
        ),
        (
            "intake_outcome",
            "intake-outcome",
            "nutrition.intake-outcome.v1",
            "nutrition-intake-outcome",
            "outcome_id",
        ),
        (
            "intake_decision_request",
            "intake-decision-request",
            "nutrition.decision-request.v1",
            "nutrition-decision-request",
            "request_id",
        ),
        (
            "intake_decision",
            "intake-decision",
            "nutrition.decision.v1",
            "nutrition-decision",
            "decision_id",
        ),
    ),
)
def test_completed_operation_retry_rejects_result_payload_digest_mismatch(
    session,
    operation_kind,
    operation_prefix,
    event_type,
    source_provider,
    operation_id_field,
):
    operation_id = uuid.uuid4()
    fingerprint = operation_fingerprint({"fixture": operation_kind})
    recorded_at = datetime.now(UTC)
    original_payload = {
        operation_id_field: str(operation_id),
        "operation_fingerprint": fingerprint,
        "status": "original",
    }
    result = WellnessEvent(
        event_type=event_type,
        schema_version=1,
        observed_at=recorded_at,
        recorded_at=recorded_at,
        timezone=None,
        source_provider=source_provider,
        source_device="fixture",
        source_record_id=str(operation_id),
        capture_method="manual",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=recorded_at + timedelta(days=1),
        payload={**original_payload, "status": "tampered"},
        derived_from=None,
    )
    marker = WellnessEvent(
        event_type="nutrition.operation.v1",
        schema_version=1,
        observed_at=recorded_at,
        recorded_at=recorded_at,
        timezone=None,
        source_provider="nutrition-operation",
        source_device=None,
        source_record_id=f"{operation_prefix}:{operation_id}",
        capture_method="system",
        quality_flags=None,
        confidence=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=None,
        payload={
            "operation_kind": operation_kind,
            "operation_id": str(operation_id),
            "operation_fingerprint": fingerprint,
            "operation_state": "completed",
            "result_payload_sha256": result_payload_digest(original_payload),
        },
        derived_from=None,
    )
    session.add_all((result, marker))
    session.commit()

    with pytest.raises(
        IntakeStorageIntegrityError,
        match="result payload digest",
    ):
        intake_service_module._existing_completed_operation_result(
            session,
            operation_id=operation_id,
            operation_fingerprint=fingerprint,
            operation_kind=operation_kind,
            operation_name=operation_kind,
            operation_prefix=operation_prefix,
            result_event_type=event_type,
            result_source_provider=source_provider,
        )


def test_completed_write_ids_cannot_be_reused_after_results_are_deleted(
    client,
    session,
):
    interaction = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(intent="ask_before_intake"),
    ).json()
    interaction_id = interaction["interaction_id"]

    review_body = {
        "operation_id": str(uuid.uuid4()),
        "status": "confirmed",
        "source": "ios-device",
        "corrected_items": [],
    }
    review_path = f"/v1/intake-interactions/{interaction_id}/review"
    assert client.post(review_path, json=review_body).status_code == 201
    review_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.interaction-review.v1",
            WellnessEvent.source_record_id == review_body["operation_id"],
        )
    )
    assert review_event is not None
    session.delete(review_event)
    session.commit()
    assert client.post(review_path, json=review_body).status_code == 409

    interaction = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(intent="ask_before_intake"),
    ).json()
    interaction_id = interaction["interaction_id"]
    request_body = {
        "operation_id": str(uuid.uuid4()),
        "scope": "daily_nutrition",
        "source": "ios-device",
    }
    request_path = f"/v1/intake-interactions/{interaction_id}/decision-requests"
    requested = client.post(request_path, json=request_body)
    assert requested.status_code == 201
    request_id = requested.json()["request_id"]
    context = client.get(f"/v1/intake-interactions/decision-requests/{request_id}/context").json()
    decision_body = {
        "operation_id": str(uuid.uuid4()),
        "request_id": request_id,
        "status": "insufficient_data",
        "source": "healthmes-agent",
        "summary": "기록 범위가 부족함",
        "evidence_event_ids": context["evidence_event_ids"],
    }
    decision_path = f"/v1/intake-interactions/{interaction_id}/decisions"
    assert client.post(decision_path, json=decision_body).status_code == 201
    decision_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.decision.v1",
            WellnessEvent.source_record_id == decision_body["operation_id"],
        )
    )
    assert decision_event is not None
    session.delete(decision_event)
    session.commit()
    assert client.post(decision_path, json=decision_body).status_code == 409

    request_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.decision-request.v1",
            WellnessEvent.source_record_id == request_body["operation_id"],
        )
    )
    assert request_event is not None
    session.delete(request_event)
    session.commit()
    assert client.post(request_path, json=request_body).status_code == 409

    outcome_body = {
        "operation_id": str(uuid.uuid4()),
        "status": "not_consumed",
        "source": "ios-device",
    }
    outcome_path = f"/v1/intake-interactions/{interaction_id}/outcomes"
    assert client.post(outcome_path, json=outcome_body).status_code == 201
    outcome_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.intake-outcome.v1",
            WellnessEvent.source_record_id == outcome_body["operation_id"],
        )
    )
    assert outcome_event is not None
    session.delete(outcome_event)
    session.commit()
    assert client.post(outcome_path, json=outcome_body).status_code == 409


def test_maintenance_backfills_terminal_outcome_before_result_deletion(
    client,
    session,
):
    interaction = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(intent="ask_before_intake"),
    ).json()
    interaction_id = interaction["interaction_id"]
    outcome_body = {
        "operation_id": str(uuid.uuid4()),
        "status": "not_consumed",
        "source": "ios-device",
    }
    outcome_path = f"/v1/intake-interactions/{interaction_id}/outcomes"
    assert client.post(outcome_path, json=outcome_body).status_code == 201
    marker = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.source_provider == "nutrition-operation",
            WellnessEvent.source_record_id == f"intake-outcome:{outcome_body['operation_id']}",
        )
    )
    outcome_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.intake-outcome.v1",
            WellnessEvent.source_record_id == outcome_body["operation_id"],
        )
    )
    transitions = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == "nutrition.interaction-transition.v1",
                WellnessEvent.payload["interaction_id"].as_string() == interaction_id,
            )
        )
    )
    assert marker is not None
    assert outcome_event is not None
    session.delete(marker)
    for transition in transitions:
        session.delete(transition)
    outcome_event.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()

    run_storage_maintenance(
        session,
        client.app.state.settings,
        now=datetime.now(UTC),
    )
    session.commit()
    session.expire_all()

    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == "nutrition.intake-outcome.v1",
                WellnessEvent.source_record_id == outcome_body["operation_id"],
            )
        )
        is None
    )
    marker = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.source_provider == "nutrition-operation",
            WellnessEvent.source_record_id == f"intake-outcome:{outcome_body['operation_id']}",
        )
    )
    assert marker is not None
    assert marker.payload["legacy_backfill"] is True
    assert set(marker.payload) == {
        "operation_kind",
        "operation_id",
        "operation_fingerprint",
        "operation_state",
        "result_payload_sha256",
        "legacy_backfill",
    }
    transition = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.interaction-transition.v1",
            WellnessEvent.source_provider == "nutrition-interaction-transition",
            WellnessEvent.payload["interaction_id"].as_string() == interaction_id,
            WellnessEvent.payload["operation_id"].as_string() == outcome_body["operation_id"],
        )
    )
    assert transition is not None
    assert transition.payload["mutation_kind"] == "outcome"
    assert transition.payload["mutation_status"] == "not_consumed"
    assert transition.payload["legacy_backfill"] is True
    assert client.post(outcome_path, json=outcome_body).status_code == 409

    review = client.post(
        f"/v1/intake-interactions/{interaction_id}/review",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "confirmed",
            "source": "ios-device",
        },
    )
    assert review.status_code == 422
    caffeine_request = client.post(
        f"/v1/intake-interactions/{interaction_id}/decision-requests",
        json={
            "operation_id": str(uuid.uuid4()),
            "scope": "caffeine_sleep",
            "source": "ios-device",
            "intended_consumption_at": "2026-08-08T16:00:00+09:00",
        },
    )
    assert caffeine_request.status_code == 422


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
    path = f"/v1/intake-interactions/decision-requests/{requested['request_id']}/context"
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


def test_decision_context_snapshots_candidate_transition_versions(client):
    primary = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(intent="compare_option"),
    ).json()
    comparison = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            intent="compare_option",
            source_text="두 번째 식사 후보",
        ),
    ).json()
    primary_review_id = str(uuid.uuid4())
    comparison_review_id = str(uuid.uuid4())
    for interaction_id, review_id in (
        (primary["interaction_id"], primary_review_id),
        (comparison["interaction_id"], comparison_review_id),
    ):
        reviewed = client.post(
            f"/v1/intake-interactions/{interaction_id}/review",
            json={
                "operation_id": review_id,
                "status": "confirmed",
                "source": "ios-device",
            },
        )
        assert reviewed.status_code == 201

    requested = client.post(
        f"/v1/intake-interactions/{primary['interaction_id']}/decision-requests",
        json={
            "operation_id": str(uuid.uuid4()),
            "scope": "compare_options",
            "source": "ios-device",
            "compare_interaction_ids": [comparison["interaction_id"]],
        },
    )
    assert requested.status_code == 201
    context = client.get(
        f"/v1/intake-interactions/decision-requests/{requested.json()['request_id']}/context"
    ).json()

    assert context["candidate_version"] == {
        "interaction_id": primary["interaction_id"],
        "latest_review_operation_id": primary_review_id,
        "latest_outcome_operation_id": None,
    }
    assert context["comparison_candidate_versions"] == [
        {
            "interaction_id": comparison["interaction_id"],
            "latest_review_operation_id": comparison_review_id,
            "latest_outcome_operation_id": None,
        }
    ]


@pytest.mark.parametrize("mutation_kind", ("review", "outcome"))
def test_decision_rejects_stale_candidate_snapshot(
    client,
    mutation_kind,
):
    candidate = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(intent="ask_before_intake"),
    ).json()
    interaction_id = candidate["interaction_id"]
    requested = client.post(
        f"/v1/intake-interactions/{interaction_id}/decision-requests",
        json={
            "operation_id": str(uuid.uuid4()),
            "scope": "daily_nutrition",
            "source": "ios-device",
        },
    ).json()
    context = client.get(
        f"/v1/intake-interactions/decision-requests/{requested['request_id']}/context"
    ).json()

    if mutation_kind == "review":
        mutation = client.post(
            f"/v1/intake-interactions/{interaction_id}/review",
            json={
                "operation_id": str(uuid.uuid4()),
                "status": "confirmed",
                "source": "ios-device",
            },
        )
    else:
        mutation = client.post(
            f"/v1/intake-interactions/{interaction_id}/outcomes",
            json={
                "operation_id": str(uuid.uuid4()),
                "status": "not_consumed",
                "source": "ios-device",
            },
        )
    assert mutation.status_code == 201

    decision = client.post(
        f"/v1/intake-interactions/{interaction_id}/decisions",
        json={
            "operation_id": str(uuid.uuid4()),
            "request_id": requested["request_id"],
            "status": "insufficient_data",
            "source": "healthmes-agent",
            "summary": "기록 범위가 부족함",
            "evidence_event_ids": context["evidence_event_ids"],
        },
    )

    assert decision.status_code == 409
    assert "decision request is stale" in decision.text


def test_compare_decision_rejects_stale_comparison_snapshot(client):
    primary = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(intent="compare_option"),
    ).json()
    comparison = client.post(
        "/v1/intake-interactions",
        json=_text_interaction(
            intent="compare_option",
            source_text="두 번째 식사 후보",
        ),
    ).json()
    requested = client.post(
        f"/v1/intake-interactions/{primary['interaction_id']}/decision-requests",
        json={
            "operation_id": str(uuid.uuid4()),
            "scope": "compare_options",
            "source": "ios-device",
            "compare_interaction_ids": [comparison["interaction_id"]],
        },
    ).json()
    context = client.get(
        f"/v1/intake-interactions/decision-requests/{requested['request_id']}/context"
    ).json()
    reviewed = client.post(
        f"/v1/intake-interactions/{comparison['interaction_id']}/review",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "confirmed",
            "source": "ios-device",
        },
    )
    assert reviewed.status_code == 201

    decision = client.post(
        f"/v1/intake-interactions/{primary['interaction_id']}/decisions",
        json={
            "operation_id": str(uuid.uuid4()),
            "request_id": requested["request_id"],
            "status": "insufficient_data",
            "source": "healthmes-agent",
            "summary": "비교 후보가 변경됨",
            "evidence_event_ids": context["evidence_event_ids"],
        },
    )

    assert decision.status_code == 409
    assert "decision request is stale" in decision.text


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


def test_expired_raw_capture_cannot_reuse_operation_id(client, session):
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


def test_prospective_snapshot_remains_searchable_after_raw_expiry(client, session):
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
    request_path = f"/v1/intake-interactions/{candidate['interaction_id']}/decision-requests"
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

    searched = client.get("/v1/intake-interactions", params={"query": "토마토 수프"})
    assert searched.status_code == 200
    assert searched.json()["count"] == 1
    record = searched.json()["records"][0]
    assert record["interaction_id"] == candidate["interaction_id"]
    assert record["raw_capture_available"] is False
    assert record["source_text"] is None

    retried = client.post(request_path, json=request_body)
    assert retried.status_code == 201
    assert retried.json()["request_id"] == requested.json()["request_id"]


def test_raw_text_expires_independently_from_structured_interaction(client, session):
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
    assert (
        session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == "nutrition.raw-capture.v1",
                WellnessEvent.source_record_id == interaction_id,
            )
        )
        is None
    )
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


def test_legacy_raw_text_is_migrated_or_removed_by_raw_policy(client, session):
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
        select(RetentionPolicy).where(RetentionPolicy.data_class == "nutrition_raw_capture")
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


def test_raw_retention_removes_duplicate_structured_evidence(client, session):
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


def test_legacy_evidence_only_payload_obeys_raw_retention(client, session):
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
        select(RetentionPolicy).where(RetentionPolicy.data_class == "nutrition_raw_capture")
    )
    assert structured is not None
    assert raw is not None
    assert raw_policy is not None
    session.delete(raw)
    payload = dict(structured.payload)
    payload["items"][0]["nutrients"][0]["evidence_text"] = body["source_text"]
    payload["items"][0]["nutrients"][0]["amount"]["evidence_text"] = body["source_text"]
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

    assert client.get(f"/v1/intake-interactions/{interaction_id}").status_code == 404
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

    assert client.get(f"/v1/intake-interactions/{interaction_id}").status_code == 404
    assert client.get("/v1/intake-interactions").json()["count"] == 0
    assert (
        client.get(f"/v1/intake-interactions/decision-requests/{request_id}/context").status_code
        == 404
    )


def test_expired_direct_capture_retry_returns_conflict(client, session):
    body = _text_interaction()
    created = client.post("/v1/intake-interactions", json=body)
    assert created.status_code == 201
    event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.interaction.v1",
            WellnessEvent.source_record_id == created.json()["interaction_id"],
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
    assert (
        session.scalar(
            select(WellnessEvent).where(WellnessEvent.event_type == "nutrition.interaction.v1")
        )
        is None
    )


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
    assert (
        client.post(
            f"/v1/intake-interactions/{interaction_id}/outcomes",
            json={
                "operation_id": str(uuid.uuid4()),
                "status": "consumed",
                "source": "test",
                "consumed_at": "2026-08-06T12:30:00+09:00",
            },
        ).status_code
        == 201
    )
    assert (
        client.post(
            f"/v1/intake-interactions/{interaction_id}/decision-requests",
            json={
                "operation_id": str(uuid.uuid4()),
                "scope": "daily_nutrition",
                "source": "test",
            },
        ).status_code
        == 201
    )
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
    marker_ids = {
        (
            f"intake-outcome:{event.payload['outcome_id']}"
            if event.event_type == "nutrition.intake-outcome.v1"
            else f"intake-decision-request:{event.payload['request_id']}"
        )
        for event in durable_events
    }
    for marker in session.scalars(
        select(WellnessEvent).where(
            WellnessEvent.source_provider == "nutrition-operation",
            WellnessEvent.source_record_id.in_(marker_ids),
        )
    ):
        session.delete(marker)
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
