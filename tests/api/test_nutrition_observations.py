"""Photo -> sake observation -> retention-aware storage API tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from healthmes.nutrition.contracts import (
    Confidence,
    EstimateKind,
    IntakeType,
    ObservationStatus,
    daily_confirmation_from_payload,
    observation_from_payload,
)
from healthmes.nutrition.repository import (
    latest_caffeine_confirmations,
    latest_nutrition_reviews,
)
from healthmes.nutrition.schema import (
    VLMEstimate,
    VLMExtraction,
    VLMItem,
    VLMNutrient,
)
from healthmes.nutrition.vision import VisionInvalidOutput, VisionUnavailable
from healthmes.storage import run_storage_maintenance
from healthmes.store import RetentionPolicy, StorageObject, WellnessEvent

JPEG = b"\xff\xd8\xff\xe0synthetic-coffee"
KST = timezone(timedelta(hours=9))


class FakeVision:
    provider_name = "fixture"
    model = "fixture-v1"
    model_digest = "sha256:fixture"

    def __init__(self, extraction=None, error=None):
        self.extraction = extraction or _extraction()
        self.error = error
        self.calls = []

    def analyze(self, image_path, *, allow_remote):
        self.calls.append((image_path, allow_remote))
        if self.error is not None:
            raise self.error
        return self.extraction


def _extraction() -> VLMExtraction:
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
                nutrients=[
                    VLMNutrient(
                        nutrient="energy",
                        amount=VLMEstimate(
                            kind=EstimateKind.RANGE,
                            unit="kcal",
                            minimum=120,
                            maximum=180,
                            estimation_basis="product_type_and_container",
                        ),
                        confidence=Confidence.MEDIUM,
                    ),
                    VLMNutrient(
                        nutrient="protein",
                        amount=VLMEstimate(
                            kind=EstimateKind.RANGE,
                            unit="g",
                            minimum=4,
                            maximum=8,
                            estimation_basis="product_type_and_container",
                        ),
                        confidence=Confidence.MEDIUM,
                    ),
                    VLMNutrient(
                        nutrient="vitamin_b12",
                        amount=VLMEstimate(
                            kind=EstimateKind.UNKNOWN,
                            unit="mcg",
                        ),
                        confidence=Confidence.LOW,
                    ),
                ],
                label_text_candidates=["355 mL", "Caffeine 180 mg"],
                product_code_candidates=[],
                confidence=Confidence.HIGH,
                warnings=[],
            )
        ],
    )


def _upload(client, content=JPEG, content_type="image/jpeg"):
    response = client.post(
        "/v1/media",
        files={"file": ("coffee.jpg", content, content_type)},
    )
    assert response.status_code == 201
    return response.json()["media_path"]


def _request(media_path, **overrides):
    payload = {
        "media_path": media_path,
        "captured_at": "2026-08-06T08:30:00+09:00",
        "timezone": "Asia/Seoul",
        "source": "ios-app-photo",
        "location": None,
        "metadata_provenance": {
            "captured_at": "app",
            "timezone": "app",
            "location": "unavailable",
        },
        "allow_remote_vision": False,
    }
    payload.update(overrides)
    return payload


def test_analyze_persists_sake_payload_and_reclassifies_media(client, session):
    provider = FakeVision()
    client.app.state.nutrition_vision_provider = provider
    media_path = _upload(client)

    response = client.post(
        "/v1/nutrition-observations/analyze",
        json=_request(media_path),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["capture"]["media_path"] == media_path
    assert body["items"][0]["caffeine"]["exact"] == 180
    nutrients = {value["nutrient"]: value for value in body["items"][0]["nutrients"]}
    assert set(nutrients) >= {
        "energy",
        "protein",
        "carbohydrate",
        "fat",
        "fiber",
        "sugar",
        "sodium",
        "caffeine",
        "vitamin_b12",
    }
    assert nutrients["energy"]["amount"]["minimum"] == 120
    assert nutrients["carbohydrate"]["amount"]["kind"] == "unknown"
    assert body["confirmation_status"] == "unconfirmed"
    assert body["vision"]["model_digest"] == "sha256:fixture"

    session.expire_all()
    event = session.scalar(
        select(WellnessEvent).where(WellnessEvent.event_type == "nutrition.observation.v1")
    )
    assert event is not None
    assert event.payload["capture"]["media_path"] == ""
    assert event.payload["warnings"] == []
    assert event.payload["items"][0]["serving"]["evidence_text"] is None
    assert event.payload["items"][0]["caffeine"]["evidence_text"] is None
    assert event.payload["items"][0]["label_text_candidates"] == []
    raw_event = session.scalar(
        select(WellnessEvent).where(WellnessEvent.event_type == "nutrition.observation-raw.v1")
    )
    assert raw_event is not None
    assert raw_event.payload["observation"] == body
    assert event.source_provider == "sake-vlm"
    assert event.expires_at is not None
    obj = session.scalar(select(StorageObject).where(StorageObject.relative_path == media_path))
    assert obj is not None
    assert obj.data_class == "nutrition_media"
    assert obj.safe_to_purge is True
    assert obj.retention_policy_id is not None

    storage = client.get("/v1/storage/settings").json()
    assert storage["usage"]["nutrition_media"] == {
        "bytes": len(JPEG),
        "objects": 1,
    }
    policies = {
        row.data_class: row.retention_days for row in session.scalars(select(RetentionPolicy))
    }
    assert policies["nutrition_media"] == 7
    assert policies["nutrition_observation"] == 90
    assert policies["nutrition_confirmation"] is None


def test_analysis_is_idempotent_per_uploaded_media(client, session):
    provider = FakeVision()
    client.app.state.nutrition_vision_provider = provider
    media_path = _upload(client)
    payload = _request(media_path)

    first = client.post("/v1/nutrition-observations/analyze", json=payload)
    second = client.post("/v1/nutrition-observations/analyze", json=payload)

    assert first.status_code == second.status_code == 201
    assert first.json()["observation_id"] == second.json()["observation_id"]
    assert len(provider.calls) == 1
    assert (
        len(
            list(
                session.scalars(
                    select(WellnessEvent).where(
                        WellnessEvent.event_type == "nutrition.observation.v1"
                    )
                )
            )
        )
        == 1
    )


def test_legacy_caffeine_only_observation_payload_remains_readable():
    observation = _extraction().items[0].to_domain()
    payload = {
        "observation_id": "d8951321-e120-4d99-8f3f-ddf70cd9ce01",
        "capture": {
            "media_path": "media/legacy.jpg",
            "captured_at": "2026-08-06T00:00:00Z",
            "timezone": "UTC",
            "source": "legacy",
            "location": None,
            "metadata_provenance": {
                "captured_at": "fixture",
                "timezone": "fixture",
                "location": "unavailable",
            },
        },
        "status": "usable",
        "confidence": "high",
        "warnings": [],
        "items": [
            {
                "intake_type": observation.intake_type.value,
                "name_candidates": list(observation.name_candidates),
                "category": observation.category,
                "serving": {
                    "kind": "exact",
                    "unit": "ml",
                    "exact": 355,
                    "evidence_text": "355 mL",
                    "estimation_basis": "visible_label",
                },
                "caffeine": {
                    "kind": "exact",
                    "unit": "mg",
                    "exact": 180,
                    "evidence_text": "Caffeine 180 mg",
                    "estimation_basis": "visible_label",
                },
                "label_text_candidates": [],
                "product_code_candidates": [],
                "confidence": "high",
                "warnings": [],
            }
        ],
        "vision": {
            "provider": "ollama",
            "model": "legacy-model",
            "model_digest": None,
            "prompt_version": "photo-intake-v1",
            "schema_version": "nutrition-observation-v1",
            "analyzed_at": "2026-08-06T00:00:01Z",
        },
        "confirmation_status": "unconfirmed",
    }
    parsed = observation_from_payload(payload)
    assert parsed.items[0].nutrients == ()
    assert parsed.items[0].caffeine.exact == 180


def test_capture_context_rejects_timezone_offset_mismatch(client):
    media_path = _upload(client)
    response = client.post(
        "/v1/nutrition-observations/analyze",
        json=_request(media_path, captured_at="2026-08-06T08:30:00+00:00"),
    )
    assert response.status_code == 422
    assert "offset conflicts" in response.text


def test_capture_context_rejects_far_future_timestamp(client):
    media_path = _upload(client)
    response = client.post(
        "/v1/nutrition-observations/analyze",
        json=_request(
            media_path,
            captured_at="2099-01-01T00:00:00Z",
            timezone="UTC",
        ),
    )
    assert response.status_code == 422
    assert "more than 5 minutes in the future" in response.text


def test_analysis_rejects_non_image_media(client):
    media_path = _upload(client, content=b"voice", content_type="audio/mpeg")
    response = client.post(
        "/v1/nutrition-observations/analyze",
        json=_request(media_path),
    )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"


def test_provider_failures_do_not_create_observation(client, session):
    media_path = _upload(client)
    client.app.state.nutrition_vision_provider = FakeVision(
        error=VisionUnavailable("local model unavailable")
    )
    unavailable = client.post(
        "/v1/nutrition-observations/analyze",
        json=_request(media_path),
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "vision_unavailable"

    client.app.state.nutrition_vision_provider = FakeVision(error=VisionInvalidOutput("bad schema"))
    invalid = client.post(
        "/v1/nutrition-observations/analyze",
        json=_request(media_path),
    )
    assert invalid.status_code == 502
    assert invalid.json()["error"]["code"] == "vision_invalid_output"
    assert (
        list(
            session.scalars(
                select(WellnessEvent).where(WellnessEvent.event_type == "nutrition.observation.v1")
            )
        )
        == []
    )


def test_confirmation_and_daily_completeness_are_separate_events(client, session):
    client.app.state.nutrition_vision_provider = FakeVision()
    media_path = _upload(client)
    observation = client.post(
        "/v1/nutrition-observations/analyze",
        json=_request(media_path),
    ).json()
    observation_id = observation["observation_id"]

    confirmed = client.post(
        f"/v1/nutrition-observations/{observation_id}/confirm",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "confirmed",
            "source": "ios-app",
            "items": [{"item_index": 0, "caffeine_mg": 180}],
        },
    )
    assert confirmed.status_code == 201

    daily = client.post(
        "/v1/nutrition-observations/daily-confirmations",
        json={
            "operation_id": str(uuid.uuid4()),
            "local_date": "2026-08-06",
            "timezone": "Asia/Seoul",
            "observation_ids": [observation_id],
            "total_intake_complete": True,
            "source": "ios-app",
        },
    )
    assert daily.status_code == 201

    events = list(session.scalars(select(WellnessEvent).order_by(WellnessEvent.recorded_at)))
    assert {event.event_type for event in events} == {
        "nutrition.observation.v1",
        "nutrition.observation-raw.v1",
        "nutrition.confirmation.v1",
        "nutrition.daily-confirmation.v1",
        "nutrition.operation.v1",
    }
    observation_event = next(
        event for event in events if event.event_type == "nutrition.observation.v1"
    )
    assert observation_event.payload["confirmation_status"] == "unconfirmed"
    confirmation_events = [
        event
        for event in events
        if event.event_type
        in {
            "nutrition.confirmation.v1",
            "nutrition.daily-confirmation.v1",
        }
    ]
    assert all(event.expires_at is None for event in confirmation_events)


def test_nutrition_mutations_require_caller_owned_operation_ids(
    client,
):
    client.app.state.nutrition_vision_provider = FakeVision()
    observation_id = client.post(
        "/v1/nutrition-observations/analyze",
        json=_request(_upload(client)),
    ).json()["observation_id"]

    confirmation = client.post(
        f"/v1/nutrition-observations/{observation_id}/confirm",
        json={
            "status": "confirmed",
            "source": "legacy-ios-app",
            "items": [{"item_index": 0, "caffeine_mg": 180}],
        },
    )
    review = client.post(
        f"/v1/nutrition-observations/{observation_id}/review",
        json={
            "status": "confirmed",
            "source": "legacy-ios-app",
        },
    )
    daily = client.post(
        "/v1/nutrition-observations/daily-confirmations",
        json={
            "local_date": "2026-08-06",
            "timezone": "Asia/Seoul",
            "observation_ids": [observation_id],
            "outcome_ids": [],
            "total_intake_complete": True,
            "source": "legacy-ios-app",
        },
    )

    assert confirmation.status_code == review.status_code == daily.status_code == 422
    assert all(
        response.json()["error"]["detail"][0]["loc"][-1] == "operation_id"
        for response in (confirmation, review, daily)
    )


def test_caffeine_confirmation_operation_is_idempotent_and_tombstoned(
    client,
    session,
):
    client.app.state.nutrition_vision_provider = FakeVision()
    observation_id = client.post(
        "/v1/nutrition-observations/analyze",
        json=_request(_upload(client)),
    ).json()["observation_id"]
    operation_id = str(uuid.uuid4())
    body = {
        "operation_id": operation_id,
        "status": "confirmed",
        "source": "ios-app",
        "items": [{"item_index": 0, "caffeine_mg": 180}],
    }
    path = f"/v1/nutrition-observations/{observation_id}/confirm"

    first = client.post(path, json=body)
    retry = client.post(path, json=body)

    assert first.status_code == retry.status_code == 201
    assert first.json()["confirmation_id"] == operation_id
    assert retry.json()["confirmation_id"] == operation_id

    conflict = client.post(
        path,
        json={
            **body,
            "items": [{"item_index": 0, "caffeine_mg": 181}],
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == ("nutrition_confirmation_operation_conflict")

    confirmation_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.confirmation.v1",
            WellnessEvent.source_record_id == f"caffeine-confirmation:{operation_id}",
        )
    )
    marker = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.operation.v1",
            WellnessEvent.payload["operation_kind"].as_string() == "caffeine_confirmation",
        )
    )
    assert confirmation_event is not None
    assert marker is not None
    assert marker.expires_at is None

    session.delete(confirmation_event)
    session.commit()
    expired_retry = client.post(path, json=body)
    assert expired_retry.status_code == 409
    assert "expired caffeine confirmation cannot be retried" in (expired_retry.text)


def test_nutrition_review_operation_marker_survives_result_removal(
    client,
    session,
):
    client.app.state.nutrition_vision_provider = FakeVision()
    observation_id = client.post(
        "/v1/nutrition-observations/analyze",
        json=_request(_upload(client)),
    ).json()["observation_id"]
    operation_id = str(uuid.uuid4())
    body = {
        "operation_id": operation_id,
        "status": "confirmed",
        "source": "desktop-web",
    }
    path = f"/v1/nutrition-observations/{observation_id}/review"

    first = client.post(path, json=body)
    retry = client.post(path, json=body)

    assert first.status_code == retry.status_code == 201
    assert first.json()["review_id"] == operation_id
    assert retry.json()["review_id"] == operation_id

    conflict = client.post(path, json={**body, "status": "rejected"})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == ("nutrition_review_operation_conflict")

    review_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.review.v1",
            WellnessEvent.source_record_id == f"nutrition-review:{operation_id}",
        )
    )
    marker = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.operation.v1",
            WellnessEvent.payload["operation_kind"].as_string() == "nutrition_review",
        )
    )
    assert review_event is not None
    assert marker is not None
    assert marker.expires_at is None

    session.delete(review_event)
    session.commit()
    expired_retry = client.post(path, json=body)
    assert expired_retry.status_code == 409
    assert "expired nutrition review cannot be retried" in expired_retry.text


def test_delayed_mutation_retries_do_not_rollback_newer_corrections(
    client,
    session,
):
    client.app.state.nutrition_vision_provider = FakeVision()
    observation_id = client.post(
        "/v1/nutrition-observations/analyze",
        json=_request(_upload(client)),
    ).json()["observation_id"]
    observation_uuid = uuid.UUID(observation_id)

    first_confirmation = {
        "operation_id": str(uuid.uuid4()),
        "status": "confirmed",
        "source": "ios-app",
        "items": [{"item_index": 0, "caffeine_mg": 180}],
    }
    corrected_confirmation = {
        **first_confirmation,
        "operation_id": str(uuid.uuid4()),
        "status": "corrected",
        "items": [{"item_index": 0, "caffeine_mg": 160}],
    }
    confirmation_path = f"/v1/nutrition-observations/{observation_id}/confirm"
    assert client.post(confirmation_path, json=first_confirmation).status_code == 201
    assert client.post(confirmation_path, json=corrected_confirmation).status_code == 201
    assert client.post(confirmation_path, json=first_confirmation).status_code == 201

    first_review = {
        "operation_id": str(uuid.uuid4()),
        "status": "confirmed",
        "source": "desktop-web",
    }
    corrected_review = {
        **first_review,
        "operation_id": str(uuid.uuid4()),
        "status": "rejected",
    }
    review_path = f"/v1/nutrition-observations/{observation_id}/review"
    assert client.post(review_path, json=first_review).status_code == 201
    assert client.post(review_path, json=corrected_review).status_code == 201
    assert client.post(review_path, json=first_review).status_code == 201

    latest_confirmation = latest_caffeine_confirmations(
        session,
        {observation_uuid},
    )[observation_uuid]
    latest_review = latest_nutrition_reviews(
        session,
        {observation_uuid},
    )[observation_uuid]
    assert latest_confirmation.confirmation_id == uuid.UUID(corrected_confirmation["operation_id"])
    assert latest_confirmation.items[0].caffeine_mg == 160
    assert latest_review.review_id == uuid.UUID(corrected_review["operation_id"])
    assert latest_review.status.value == "rejected"


def test_daily_confirmation_operation_id_is_idempotent_and_order_safe(
    client,
    session,
):
    client.app.state.nutrition_vision_provider = FakeVision()
    observation_id = client.post(
        "/v1/nutrition-observations/analyze",
        json=_request(_upload(client)),
    ).json()["observation_id"]
    first_operation_id = str(uuid.uuid4())
    first_body = {
        "operation_id": first_operation_id,
        "local_date": "2026-08-06",
        "timezone": "Asia/Seoul",
        "observation_ids": [observation_id],
        "outcome_ids": [],
        "total_intake_complete": True,
        "source": "ios-app",
    }

    first = client.post(
        "/v1/nutrition-observations/daily-confirmations",
        json=first_body,
    )
    retry = client.post(
        "/v1/nutrition-observations/daily-confirmations",
        json=first_body,
    )
    assert first.status_code == retry.status_code == 201
    assert first.json()["confirmation_id"] == first_operation_id
    assert retry.json()["confirmation_id"] == first_operation_id

    conflict = client.post(
        "/v1/nutrition-observations/daily-confirmations",
        json={**first_body, "total_intake_complete": False},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "operation_id_conflict"

    correction_operation_id = str(uuid.uuid4())
    correction = client.post(
        "/v1/nutrition-observations/daily-confirmations",
        json={
            **first_body,
            "operation_id": correction_operation_id,
            "total_intake_complete": False,
        },
    )
    assert correction.status_code == 201
    assert (
        client.post(
            "/v1/nutrition-observations/daily-confirmations",
            json=first_body,
        ).status_code
        == 201
    )

    daily_events = list(
        session.scalars(
            select(WellnessEvent)
            .where(WellnessEvent.event_type == "nutrition.daily-confirmation.v1")
            .order_by(
                WellnessEvent.recorded_at.desc(),
                WellnessEvent.created_at.desc(),
            )
        )
    )
    markers = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == "nutrition.operation.v1",
                WellnessEvent.payload["operation_kind"].as_string() == "daily_intake_confirmation",
            )
        )
    )
    assert len(daily_events) == len(markers) == 2
    latest = daily_confirmation_from_payload(daily_events[0].payload)
    assert str(latest.confirmation_id) == correction_operation_id
    assert latest.total_intake_complete is False


def test_confirmation_operation_id_is_scoped_by_write_kind(
    client,
    session,
):
    client.app.state.nutrition_vision_provider = FakeVision()
    observation_id = client.post(
        "/v1/nutrition-observations/analyze",
        json=_request(_upload(client)),
    ).json()["observation_id"]
    operation_id = str(uuid.uuid4())

    caffeine = client.post(
        f"/v1/nutrition-observations/{observation_id}/confirm",
        json={
            "operation_id": operation_id,
            "status": "confirmed",
            "source": "ios-app",
            "items": [{"item_index": 0, "caffeine_mg": 180}],
        },
    )
    review = client.post(
        f"/v1/nutrition-observations/{observation_id}/review",
        json={
            "operation_id": operation_id,
            "status": "confirmed",
            "source": "ios-app",
        },
    )
    daily = client.post(
        "/v1/nutrition-observations/daily-confirmations",
        json={
            "operation_id": operation_id,
            "local_date": "2026-08-06",
            "timezone": "Asia/Seoul",
            "observation_ids": [observation_id],
            "outcome_ids": [],
            "total_intake_complete": True,
            "source": "ios-app",
        },
    )

    assert caffeine.status_code == review.status_code == daily.status_code == 201
    result_record_ids = set(
        session.scalars(
            select(WellnessEvent.source_record_id).where(
                WellnessEvent.event_type.in_(
                    (
                        "nutrition.confirmation.v1",
                        "nutrition.review.v1",
                        "nutrition.daily-confirmation.v1",
                    )
                )
            )
        )
    )
    assert result_record_ids == {
        f"caffeine-confirmation:{operation_id}",
        f"nutrition-review:{operation_id}",
        f"daily-confirmation:{operation_id}",
    }
    marker_record_ids = set(
        session.scalars(
            select(WellnessEvent.source_record_id).where(
                WellnessEvent.event_type == "nutrition.operation.v1",
                WellnessEvent.payload["operation_id"].as_string() == operation_id,
            )
        )
    )
    assert marker_record_ids == result_record_ids


@pytest.mark.parametrize(
    "write_kind",
    ("caffeine", "review", "daily"),
)
def test_prefixed_write_retry_reads_markerless_legacy_uuid_result(
    client,
    session,
    settings,
    write_kind,
):
    client.app.state.nutrition_vision_provider = FakeVision()
    observation_id = client.post(
        "/v1/nutrition-observations/analyze",
        json=_request(_upload(client)),
    ).json()["observation_id"]
    operation_id = str(uuid.uuid4())
    if write_kind == "caffeine":
        event_type = "nutrition.confirmation.v1"
        prefix = "caffeine-confirmation"
        path = f"/v1/nutrition-observations/{observation_id}/confirm"
        body = {
            "operation_id": operation_id,
            "status": "confirmed",
            "source": "ios-app",
            "items": [{"item_index": 0, "caffeine_mg": 180}],
        }
        conflicting_body = {
            **body,
            "items": [{"item_index": 0, "caffeine_mg": 181}],
        }
    elif write_kind == "review":
        event_type = "nutrition.review.v1"
        prefix = "nutrition-review"
        path = f"/v1/nutrition-observations/{observation_id}/review"
        body = {
            "operation_id": operation_id,
            "status": "confirmed",
            "source": "ios-app",
        }
        conflicting_body = {**body, "status": "rejected"}
    else:
        event_type = "nutrition.daily-confirmation.v1"
        prefix = "daily-confirmation"
        path = "/v1/nutrition-observations/daily-confirmations"
        body = {
            "operation_id": operation_id,
            "local_date": "2026-08-06",
            "timezone": "Asia/Seoul",
            "observation_ids": [observation_id],
            "outcome_ids": [],
            "total_intake_complete": True,
            "source": "ios-app",
        }
        conflicting_body = {
            **body,
            "total_intake_complete": False,
        }
    assert client.post(path, json=body).status_code == 201

    result = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == event_type,
            WellnessEvent.source_record_id == f"{prefix}:{operation_id}",
        )
    )
    marker = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.operation.v1",
            WellnessEvent.source_record_id == f"{prefix}:{operation_id}",
        )
    )
    assert result is not None
    assert marker is not None
    result.source_record_id = operation_id
    result.expires_at = datetime.now(UTC) + timedelta(days=1)
    session.delete(marker)
    session.commit()

    run_storage_maintenance(
        session,
        settings,
        now=datetime.now(UTC),
    )
    session.commit()
    session.expire_all()

    marker = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.operation.v1",
            WellnessEvent.source_record_id == f"{prefix}:{operation_id}",
        )
    )
    assert marker is not None
    assert marker.payload["operation_fingerprint"] is None
    assert marker.payload["legacy_backfill"] is True

    retry = client.post(path, json=body)
    conflict = client.post(path, json=conflicting_body)

    assert retry.status_code == 201
    assert conflict.status_code == 409
    assert (
        len(
            list(
                session.scalars(
                    select(WellnessEvent).where(
                        WellnessEvent.event_type == event_type,
                    )
                )
            )
        )
        == 1
    )

    operation_id_field = "review_id" if write_kind == "review" else "confirmation_id"
    result.payload = {
        **result.payload,
        operation_id_field: str(uuid.uuid4()),
    }
    session.commit()

    mismatched_payload = client.post(path, json=body)

    assert mismatched_payload.status_code == 409
    assert "conflict" in mismatched_payload.json()["error"]["code"]

    result.payload = {
        **result.payload,
        operation_id_field: "malformed-operation-id",
    }
    session.commit()

    malformed_payload = client.post(path, json=body)

    assert malformed_payload.status_code == 409
    assert "conflict" in malformed_payload.json()["error"]["code"]


def test_quarantined_photo_confirmation_is_not_authoritative(
    client,
    session,
):
    client.app.state.nutrition_vision_provider = FakeVision()
    observation_id = client.post(
        "/v1/nutrition-observations/analyze",
        json=_request(_upload(client)),
    ).json()["observation_id"]
    response = client.post(
        f"/v1/nutrition-observations/{observation_id}/confirm",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "confirmed",
            "source": "ios-app",
            "items": [{"item_index": 0, "caffeine_mg": 180}],
        },
    )
    assert response.status_code == 201
    result = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.confirmation.v1",
        )
    )
    assert result is not None
    result.quality_flags = {"maintenance_quarantine": "legacy_operation_result_identity_invalid"}
    session.commit()

    known = client.get("/v1/nutrition-observations")
    assert known.status_code == 200
    from healthmes.nutrition.query import known_caffeine_for_day

    ledger = known_caffeine_for_day(
        session,
        local_date=datetime(2026, 8, 6, tzinfo=UTC).date(),
        timezone="Asia/Seoul",
    )
    assert ledger["confirmed_caffeine_mg"] == 0
    assert ledger["reviewed_count"] == 0


def test_quarantined_photo_review_and_daily_confirmation_are_ignored(
    client,
    session,
):
    client.app.state.nutrition_vision_provider = FakeVision()
    observation_id = client.post(
        "/v1/nutrition-observations/analyze",
        json=_request(_upload(client)),
    ).json()["observation_id"]
    confirmation = client.post(
        f"/v1/nutrition-observations/{observation_id}/confirm",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "confirmed",
            "source": "ios-app",
            "items": [{"item_index": 0, "caffeine_mg": 180}],
        },
    )
    review = client.post(
        f"/v1/nutrition-observations/{observation_id}/review",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "confirmed",
            "source": "ios-app",
        },
    )
    daily = client.post(
        "/v1/nutrition-observations/daily-confirmations",
        json={
            "operation_id": str(uuid.uuid4()),
            "local_date": "2026-08-06",
            "timezone": "Asia/Seoul",
            "observation_ids": [observation_id],
            "outcome_ids": [],
            "total_intake_complete": True,
            "source": "ios-app",
        },
    )
    assert confirmation.status_code == review.status_code == daily.status_code == 201
    rows = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type.in_(
                    (
                        "nutrition.review.v1",
                        "nutrition.daily-confirmation.v1",
                    )
                )
            )
        )
    )
    assert len(rows) == 2
    for row in rows:
        row.quality_flags = {"maintenance_quarantine": "legacy_operation_result_identity_invalid"}
    session.commit()

    fetched_review = client.get(f"/v1/nutrition-observations/{observation_id}/review")
    assert fetched_review.status_code == 200
    assert fetched_review.json()["review"] is None

    from healthmes.nutrition.query import known_caffeine_for_day

    ledger = known_caffeine_for_day(
        session,
        local_date=datetime(2026, 8, 6, tzinfo=UTC).date(),
        timezone="Asia/Seoul",
    )
    assert ledger["confirmed_caffeine_mg"] == 180
    assert ledger["total_intake_complete"] is False


def test_photo_operation_retry_rejects_processing_marker(
    client,
    session,
):
    client.app.state.nutrition_vision_provider = FakeVision()
    observation_id = client.post(
        "/v1/nutrition-observations/analyze",
        json=_request(_upload(client)),
    ).json()["observation_id"]
    operation_id = str(uuid.uuid4())
    path = f"/v1/nutrition-observations/{observation_id}/confirm"
    body = {
        "operation_id": operation_id,
        "status": "confirmed",
        "source": "ios-app",
        "items": [{"item_index": 0, "caffeine_mg": 180}],
    }
    assert client.post(path, json=body).status_code == 201
    marker = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.operation.v1",
            WellnessEvent.source_record_id == f"caffeine-confirmation:{operation_id}",
        )
    )
    assert marker is not None
    marker.payload = {
        **marker.payload,
        "operation_state": "processing",
    }
    session.commit()

    retry = client.post(path, json=body)

    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "nutrition_confirmation_operation_conflict"


def test_confirmation_requires_every_real_observation_item(client):
    client.app.state.nutrition_vision_provider = FakeVision()
    media_path = _upload(client)
    observation_id = client.post(
        "/v1/nutrition-observations/analyze",
        json=_request(media_path),
    ).json()["observation_id"]

    response = client.post(
        f"/v1/nutrition-observations/{observation_id}/confirm",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "confirmed",
            "source": "desktop-web",
            "items": [{"item_index": 1, "caffeine_mg": 180}],
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_nutrition_confirmation"


def test_complete_day_confirmation_requires_every_observation(client):
    client.app.state.nutrition_vision_provider = FakeVision()
    observation_ids = []
    for _ in range(2):
        media_path = _upload(client)
        observation_ids.append(
            client.post(
                "/v1/nutrition-observations/analyze",
                json=_request(media_path),
            ).json()["observation_id"]
        )

    response = client.post(
        "/v1/nutrition-observations/daily-confirmations",
        json={
            "operation_id": str(uuid.uuid4()),
            "local_date": "2026-08-06",
            "timezone": "Asia/Seoul",
            "observation_ids": observation_ids[:1],
            "total_intake_complete": True,
            "source": "desktop-web",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_daily_nutrition_confirmation"
    assert "every observation" in response.text


def test_expired_photo_is_deleted_without_deleting_observation(client, session, settings):
    client.app.state.nutrition_vision_provider = FakeVision()
    media_path = _upload(client)
    observation_id = client.post(
        "/v1/nutrition-observations/analyze",
        json=_request(media_path),
    ).json()["observation_id"]
    photo = settings.data_dir / media_path

    report = run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 14, 8, 30, tzinfo=UTC),
    )
    session.commit()
    session.expire_all()

    assert report.deleted == 1
    assert not photo.exists()
    obj = session.scalar(select(StorageObject).where(StorageObject.relative_path == media_path))
    assert obj is not None
    assert obj.purged_at is not None
    observation = session.scalar(
        select(WellnessEvent).where(WellnessEvent.source_record_id == observation_id)
    )
    assert observation is not None
    assert observation.expires_at is not None
    assert observation.expires_at.replace(tzinfo=UTC) > datetime(2026, 8, 14, 8, 30, tzinfo=UTC)


def test_photo_raw_evidence_expires_with_media(client, session, settings):
    secret = "Medication X 50 mg"
    extraction = _extraction()
    extraction.warnings = [secret]
    extraction.items[0].warnings = [secret]
    extraction.items[0].label_text_candidates = [secret]
    extraction.items[0].serving.evidence_text = secret
    client.app.state.nutrition_vision_provider = FakeVision(extraction=extraction)
    media_path = _upload(client)
    created = client.post(
        "/v1/nutrition-observations/analyze",
        json=_request(media_path),
    )
    assert created.status_code == 201
    observation_id = created.json()["observation_id"]
    assert secret in str(created.json())

    run_storage_maintenance(
        session,
        settings,
        now=datetime(2026, 8, 14, 8, 30, tzinfo=UTC),
    )
    session.commit()

    fetched = client.get(f"/v1/nutrition-observations/{observation_id}")
    assert fetched.status_code == 200
    assert secret not in str(fetched.json())
    assert fetched.json()["capture"]["media_path"] == ""


def test_expired_observation_cannot_be_read_or_confirmed(client, session):
    client.app.state.nutrition_vision_provider = FakeVision()
    media_path = _upload(client)
    created = client.post(
        "/v1/nutrition-observations/analyze",
        json=_request(media_path),
    )
    assert created.status_code == 201
    observation_id = created.json()["observation_id"]
    event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.observation.v1",
            WellnessEvent.source_record_id == observation_id,
        )
    )
    assert event is not None
    event.expires_at = datetime.now(UTC)
    session.commit()

    assert client.get(f"/v1/nutrition-observations/{observation_id}").status_code == 404
    assert client.get("/v1/nutrition-observations").json() == []
    confirmed = client.post(
        f"/v1/nutrition-observations/{observation_id}/confirm",
        json={
            "operation_id": str(uuid.uuid4()),
            "status": "confirmed",
            "source": "test",
            "items": [{"item_index": 0, "caffeine_mg": 180}],
        },
    )
    assert confirmed.status_code == 404
