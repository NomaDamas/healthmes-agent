"""Transport-neutral sake intake-observation contract.

The structured observation is the canonical representation. HealthMes stores
it intact inside ``WellnessEvent.payload`` instead of flattening it into the
legacy ``FoodLog`` model.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import TypeAdapter


class MetadataSource(StrEnum):
    APP = "app"
    EXIF = "exif"
    USER = "user"
    FIXTURE = "fixture"
    UNAVAILABLE = "unavailable"


class ObservationStatus(StrEnum):
    USABLE = "usable"
    AMBIGUOUS = "ambiguous"
    INSUFFICIENT_DATA = "insufficient_data"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class EstimateKind(StrEnum):
    EXACT = "exact"
    RANGE = "range"
    UNKNOWN = "unknown"


class IntakeType(StrEnum):
    FOOD = "food"
    BEVERAGE = "beverage"
    SUPPLEMENT = "supplement"
    MEDICATION = "medication"
    UNKNOWN = "unknown"


class ConfirmationStatus(StrEnum):
    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    CORRECTED = "corrected"


@dataclass(frozen=True, slots=True)
class Location:
    latitude: float
    longitude: float
    accuracy_meters: float | None = None


@dataclass(frozen=True, slots=True)
class CaptureContext:
    media_path: str
    captured_at: datetime
    timezone: str
    source: str
    location: Location | None
    metadata_provenance: dict[str, MetadataSource]


@dataclass(frozen=True, slots=True)
class Estimate:
    kind: EstimateKind
    unit: str
    exact: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    evidence_text: str | None = None
    estimation_basis: str | None = None


@dataclass(frozen=True, slots=True)
class NutrientEstimate:
    nutrient: str
    amount: Estimate
    confidence: Confidence = Confidence.LOW


@dataclass(frozen=True, slots=True)
class IntakeItem:
    intake_type: IntakeType
    name_candidates: tuple[str, ...]
    category: str | None
    serving: Estimate
    caffeine: Estimate
    nutrients: tuple[NutrientEstimate, ...] = ()
    label_text_candidates: tuple[str, ...] = ()
    product_code_candidates: tuple[str, ...] = ()
    confidence: Confidence = Confidence.LOW
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VisionProvenance:
    provider: str
    model: str
    model_digest: str | None
    prompt_version: str
    schema_version: str
    analyzed_at: datetime


@dataclass(frozen=True, slots=True)
class NutritionObservation:
    observation_id: uuid.UUID
    capture: CaptureContext
    status: ObservationStatus
    confidence: Confidence
    warnings: tuple[str, ...]
    items: tuple[IntakeItem, ...]
    vision: VisionProvenance
    confirmation_status: ConfirmationStatus = ConfirmationStatus.UNCONFIRMED


@dataclass(frozen=True, slots=True)
class ConfirmedCaffeineItem:
    item_index: int
    caffeine_mg: float


@dataclass(frozen=True, slots=True)
class CaffeineConfirmation:
    confirmation_id: uuid.UUID
    observation_id: uuid.UUID
    status: ConfirmationStatus
    confirmed_at: datetime
    source: str
    items: tuple[ConfirmedCaffeineItem, ...] = ()


@dataclass(frozen=True, slots=True)
class ReviewedNutritionItem:
    item_index: int
    name: str
    intake_type: IntakeType
    serving: Estimate
    nutrients: tuple[NutrientEstimate, ...]
    confidence: Confidence = Confidence.HIGH
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NutritionReview:
    review_id: uuid.UUID
    observation_id: uuid.UUID
    status: ConfirmationStatus
    reviewed_at: datetime
    source: str
    items: tuple[ReviewedNutritionItem, ...] = ()


@dataclass(frozen=True, slots=True)
class DailyIntakeConfirmation:
    confirmation_id: uuid.UUID
    local_date: date
    timezone: str
    observation_ids: tuple[uuid.UUID, ...]
    total_intake_complete: bool
    confirmed_at: datetime
    source: str


_OBSERVATION_ADAPTER = TypeAdapter(NutritionObservation)
_CAFFEINE_CONFIRMATION_ADAPTER = TypeAdapter(CaffeineConfirmation)
_NUTRITION_REVIEW_ADAPTER = TypeAdapter(NutritionReview)
_DAILY_CONFIRMATION_ADAPTER = TypeAdapter(DailyIntakeConfirmation)


def observation_to_payload(value: NutritionObservation) -> dict[str, Any]:
    return _OBSERVATION_ADAPTER.dump_python(value, mode="json")


def observation_from_payload(value: dict[str, Any]) -> NutritionObservation:
    return _OBSERVATION_ADAPTER.validate_python(value)


def caffeine_confirmation_to_payload(value: CaffeineConfirmation) -> dict[str, Any]:
    return _CAFFEINE_CONFIRMATION_ADAPTER.dump_python(value, mode="json")


def caffeine_confirmation_from_payload(value: dict[str, Any]) -> CaffeineConfirmation:
    return _CAFFEINE_CONFIRMATION_ADAPTER.validate_python(value)


def nutrition_review_to_payload(value: NutritionReview) -> dict[str, Any]:
    return _NUTRITION_REVIEW_ADAPTER.dump_python(value, mode="json")


def nutrition_review_from_payload(value: dict[str, Any]) -> NutritionReview:
    return _NUTRITION_REVIEW_ADAPTER.validate_python(value)


def daily_confirmation_to_payload(value: DailyIntakeConfirmation) -> dict[str, Any]:
    return _DAILY_CONFIRMATION_ADAPTER.dump_python(value, mode="json")


def daily_confirmation_from_payload(value: dict[str, Any]) -> DailyIntakeConfirmation:
    return _DAILY_CONFIRMATION_ADAPTER.validate_python(value)


def confidence_score(value: Confidence) -> float:
    return {
        Confidence.HIGH: 0.9,
        Confidence.MEDIUM: 0.6,
        Confidence.LOW: 0.3,
    }[value]
