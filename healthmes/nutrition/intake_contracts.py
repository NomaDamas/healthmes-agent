"""Device-neutral contracts for nutrition capture and decision context.

The capture modality and the owner's intent are independent dimensions. A
photo, text entry, or voice transcript first becomes an observation. It only
becomes known intake after an explicit owner outcome records consumption.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import TypeAdapter

from healthmes.nutrition.contracts import Confidence, Estimate


class CaptureModality(StrEnum):
    PHOTO = "photo"
    TEXT = "text"
    VOICE = "voice"


class IntakeIntent(StrEnum):
    LOG_CONSUMED = "log_consumed"
    ASK_BEFORE_INTAKE = "ask_before_intake"
    INSPECT_ONLY = "inspect_only"
    PLAN_FUTURE = "plan_future"
    COMPARE_OPTION = "compare_option"


class IntakeOutcomeStatus(StrEnum):
    CONSUMED = "consumed"
    NOT_CONSUMED = "not_consumed"
    CANCELLED = "cancelled"


class DecisionScope(StrEnum):
    CAFFEINE_SLEEP = "caffeine_sleep"
    DAILY_NUTRITION = "daily_nutrition"
    MEAL_TIMING = "meal_timing"
    ACTIVITY_FUELING = "activity_fueling"
    COMPARE_OPTIONS = "compare_options"
    GLUCOSE_IMPACT = "glucose_impact"
    ALLERGY_SAFETY = "allergy_safety"
    MEDICATION_INTERACTION = "medication_interaction"


class DecisionStatus(StrEnum):
    PROPOSAL = "proposal"
    NOOP = "noop"
    INSUFFICIENT_DATA = "insufficient_data"
    UNSUPPORTED = "unsupported"


class EvidenceOrigin(StrEnum):
    USER = "user"
    VLM = "vlm"
    AGENT = "agent"
    LABEL = "label"


@dataclass(frozen=True, slots=True)
class NutrientFact:
    nutrient: str
    amount: Estimate
    confidence: Confidence
    origin: EvidenceOrigin
    evidence_text: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedIntakeItem:
    name: str
    intake_type: str
    serving: Estimate
    nutrients: tuple[NutrientFact, ...] = ()
    confidence: Confidence = Confidence.LOW
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IntakeInteraction:
    interaction_id: uuid.UUID
    operation_fingerprint: str
    intent: IntakeIntent
    modality: CaptureModality
    observed_at: datetime
    recorded_at: datetime
    timezone: str
    source: str
    source_text: str | None
    media_path: str | None
    nutrition_observation_id: uuid.UUID | None
    items: tuple[NormalizedIntakeItem, ...]
    nutrition_review_id: uuid.UUID | None = None
    warnings: tuple[str, ...] = ()
    schema_version: str = "intake-interaction-v1"


@dataclass(frozen=True, slots=True)
class StructuredIntakeSnapshot:
    """Durable nutrition facts without raw text, transcript, or media paths."""

    interaction_id: uuid.UUID
    intent: IntakeIntent
    modality: CaptureModality
    observed_at: datetime
    timezone: str
    source: str
    nutrition_observation_id: uuid.UUID | None
    items: tuple[NormalizedIntakeItem, ...]
    nutrition_review_id: uuid.UUID | None = None
    warnings: tuple[str, ...] = ()
    schema_version: str = "structured-intake-snapshot-v1"


@dataclass(frozen=True, slots=True)
class IntakeOutcome:
    outcome_id: uuid.UUID
    operation_fingerprint: str
    interaction_id: uuid.UUID
    status: IntakeOutcomeStatus
    confirmed_at: datetime
    source: str
    consumed_at: datetime | None = None
    corrected_items: tuple[NormalizedIntakeItem, ...] = ()
    note: str | None = None
    intake_snapshot: StructuredIntakeSnapshot | None = None
    schema_version: str = "intake-outcome-v1"


@dataclass(frozen=True, slots=True)
class IntakeDecisionRequest:
    request_id: uuid.UUID
    operation_fingerprint: str
    interaction_id: uuid.UUID
    scope: DecisionScope
    requested_at: datetime
    source: str
    question: str | None = None
    intended_consumption_at: datetime | None = None
    compare_interaction_ids: tuple[uuid.UUID, ...] = ()
    lookback_days: int = 14
    context_snapshot: dict[str, Any] | None = None
    schema_version: str = "intake-decision-request-v1"


@dataclass(frozen=True, slots=True)
class IntakeDecision:
    decision_id: uuid.UUID
    operation_fingerprint: str
    request_id: uuid.UUID
    interaction_id: uuid.UUID
    scope: DecisionScope
    status: DecisionStatus
    decided_at: datetime
    source: str
    summary: str
    evidence_event_ids: tuple[uuid.UUID, ...]
    limitations: tuple[str, ...] = ()
    recommendation: dict[str, Any] | None = None
    schema_version: str = "intake-decision-v1"


_INTERACTION_ADAPTER = TypeAdapter(IntakeInteraction)
_OUTCOME_ADAPTER = TypeAdapter(IntakeOutcome)
_DECISION_REQUEST_ADAPTER = TypeAdapter(IntakeDecisionRequest)
_DECISION_ADAPTER = TypeAdapter(IntakeDecision)


def interaction_to_payload(value: IntakeInteraction) -> dict[str, Any]:
    return _INTERACTION_ADAPTER.dump_python(value, mode="json")


def interaction_from_payload(value: dict[str, Any]) -> IntakeInteraction:
    return _INTERACTION_ADAPTER.validate_python(value)


def outcome_to_payload(value: IntakeOutcome) -> dict[str, Any]:
    return _OUTCOME_ADAPTER.dump_python(value, mode="json")


def outcome_from_payload(value: dict[str, Any]) -> IntakeOutcome:
    return _OUTCOME_ADAPTER.validate_python(value)


def decision_request_to_payload(value: IntakeDecisionRequest) -> dict[str, Any]:
    return _DECISION_REQUEST_ADAPTER.dump_python(value, mode="json")


def decision_request_from_payload(value: dict[str, Any]) -> IntakeDecisionRequest:
    return _DECISION_REQUEST_ADAPTER.validate_python(value)


def decision_to_payload(value: IntakeDecision) -> dict[str, Any]:
    return _DECISION_ADAPTER.dump_python(value, mode="json")


def decision_from_payload(value: dict[str, Any]) -> IntakeDecision:
    return _DECISION_ADAPTER.validate_python(value)
