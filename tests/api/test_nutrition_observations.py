"""Photo -> sake observation -> retention-aware storage API tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from healthmes.nutrition.contracts import (
    Confidence,
    EstimateKind,
    IntakeType,
    ObservationStatus,
    observation_from_payload,
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

pytestmark = pytest.mark.usefixtures("fixture_clock")

JPEG = b"\xff\xd8\xff\xe0synthetic-coffee"


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


def test_analyze_persists_sake_payload_and_reclassifies_media(
    client, session
):
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
    nutrients = {
        value["nutrient"]: value
        for value in body["items"][0]["nutrients"]
    }
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
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.observation.v1"
        )
    )
    assert event is not None
    assert event.payload["capture"]["media_path"] == ""
    assert event.payload["warnings"] == []
    assert event.payload["items"][0]["serving"]["evidence_text"] is None
    assert event.payload["items"][0]["caffeine"]["evidence_text"] is None
    assert event.payload["items"][0]["label_text_candidates"] == []
    raw_event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == "nutrition.observation-raw.v1"
        )
    )
    assert raw_event is not None
    assert raw_event.payload["observation"] == body
    assert event.source_provider == "sake-vlm"
    assert event.expires_at is not None
    obj = session.scalar(
        select(StorageObject).where(StorageObject.relative_path == media_path)
    )
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
        row.data_class: row.retention_days
        for row in session.scalars(select(RetentionPolicy))
    }
    assert policies["nutrition_media"] == 7
    assert policies["nutrition_observation"] == 90
    assert policies["nutrition_confirmation"] is None


def test_photo_analysis_accepts_stable_fixed_offset_timezone(client) -> None:
    client.app.state.nutrition_vision_provider = FakeVision()
    media_path = _upload(client)

    response = client.post(
        "/v1/nutrition-observations/analyze",
        json=_request(
            media_path,
            timezone="UTC+09:00",
        ),
    )

    assert response.status_code == 201
    assert response.json()["capture"]["timezone"] == "UTC+09:00"


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
    assert len(
        list(
            session.scalars(
                select(WellnessEvent).where(
                    WellnessEvent.event_type == "nutrition.observation.v1"
                )
            )
        )
    ) == 1


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

    client.app.state.nutrition_vision_provider = FakeVision(
        error=VisionInvalidOutput("bad schema")
    )
    invalid = client.post(
        "/v1/nutrition-observations/analyze",
        json=_request(media_path),
    )
    assert invalid.status_code == 502
    assert invalid.json()["error"]["code"] == "vision_invalid_output"
    assert list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == "nutrition.observation.v1"
            )
        )
    ) == []


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
            "status": "confirmed",
            "source": "ios-app",
            "items": [{"item_index": 0, "caffeine_mg": 180}],
        },
    )
    assert confirmed.status_code == 201

    daily = client.post(
        "/v1/nutrition-observations/daily-confirmations",
        json={
            "local_date": "2026-08-06",
            "timezone": "Asia/Seoul",
            "observation_ids": [observation_id],
            "total_intake_complete": True,
            "source": "ios-app",
        },
    )
    assert daily.status_code == 201

    events = list(
        session.scalars(
            select(WellnessEvent).order_by(WellnessEvent.recorded_at)
        )
    )
    assert {
        event.event_type for event in events
    } == {
        "nutrition.observation.v1",
        "nutrition.observation-raw.v1",
        "nutrition.confirmation.v1",
        "nutrition.daily-confirmation.v1",
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
            "local_date": "2026-08-06",
            "timezone": "Asia/Seoul",
            "observation_ids": observation_ids[:1],
            "total_intake_complete": True,
            "source": "desktop-web",
        },
    )

    assert response.status_code == 422
    assert (
        response.json()["error"]["code"]
        == "invalid_daily_nutrition_confirmation"
    )
    assert "every observation" in response.text


def test_expired_photo_is_deleted_without_deleting_observation(
    client, session, settings
):
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
    obj = session.scalar(
        select(StorageObject).where(StorageObject.relative_path == media_path)
    )
    assert obj is not None
    assert obj.purged_at is not None
    observation = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.source_record_id == observation_id
        )
    )
    assert observation is not None
    assert observation.expires_at is not None
    assert observation.expires_at.replace(tzinfo=UTC) > datetime(
        2026, 8, 14, 8, 30, tzinfo=UTC
    )


def test_photo_raw_evidence_expires_with_media(client, session, settings):
    secret = "Medication X 50 mg"
    extraction = _extraction()
    extraction.warnings = [secret]
    extraction.items[0].warnings = [secret]
    extraction.items[0].label_text_candidates = [secret]
    extraction.items[0].serving.evidence_text = secret
    client.app.state.nutrition_vision_provider = FakeVision(
        extraction=extraction
    )
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

    fetched = client.get(
        f"/v1/nutrition-observations/{observation_id}"
    )
    assert fetched.status_code == 200
    assert secret not in str(fetched.json())
    assert fetched.json()["capture"]["media_path"] == ""


def test_expired_observation_cannot_be_read_or_confirmed(
    client,
    session,
    fixture_clock,
):
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
    event.expires_at = fixture_clock()
    session.commit()

    assert (
        client.get(
            f"/v1/nutrition-observations/{observation_id}"
        ).status_code
        == 404
    )
    assert client.get("/v1/nutrition-observations").json() == []
    confirmed = client.post(
        f"/v1/nutrition-observations/{observation_id}/confirm",
        json={
            "status": "confirmed",
            "source": "test",
            "items": [{"item_index": 0, "caffeine_mg": 180}],
        },
    )
    assert confirmed.status_code == 404
