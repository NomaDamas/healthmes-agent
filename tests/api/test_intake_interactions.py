"""Device-neutral intake interaction engine integration tests."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from healthmes.nutrition.contracts import (
    Confidence,
    EstimateKind,
    IntakeType,
    ObservationStatus,
)
from healthmes.nutrition.schema import VLMEstimate, VLMExtraction, VLMItem
from healthmes.store import RetentionPolicy, StorageObject, WellnessEvent

JPEG = b"\xff\xd8\xff\xe0synthetic-coffee"


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
