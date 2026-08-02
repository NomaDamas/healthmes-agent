from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import NewType

CalendarEventId = NewType("CalendarEventId", str)
CaffeineMg = NewType("CaffeineMg", int)


class EvidenceFreshness(StrEnum):
    CURRENT = "current"
    STALE = "stale"


SleepFreshness = EvidenceFreshness
BaselineFreshness = EvidenceFreshness


class CaffeineProposalStatus(StrEnum):
    PROPOSAL = "proposal"
    NOOP = "noop"
    INSUFFICIENT_DATA = "insufficient_data"
    INVALID_INPUT = "invalid_input"


class ProposalConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class CaffeineProposalReason(StrEnum):
    PERSONAL_EVENT_BASELINE_APPLIED = "personal_event_baseline_applied"
    PERSONAL_EVENT_BASELINE_UNAVAILABLE = "personal_event_baseline_unavailable"
    DAILY_LIMIT_REACHED = "daily_limit_reached"
    DAILY_LIMIT_EXCEEDED = "daily_limit_exceeded"
    MISSING_TARGET_EVENT = "missing_target_event"
    MISSING_SLEEP = "missing_sleep"
    STALE_SLEEP = "stale_sleep"
    MISSING_TOTAL_INTAKE = "missing_total_intake"
    INCOMPLETE_TOTAL_INTAKE = "incomplete_total_intake"
    MISSING_TIMING = "missing_timing"
    UNKNOWN_POPULATION = "unknown_population"
    WITHIN_SLEEP_CUTOFF = "within_sleep_cutoff"
    UNSUPPORTED_POPULATION = "unsupported_population"
    CLINICIAN_GUIDANCE_REQUIRED = "clinician_guidance_required"
    UNSUPPORTED_PRODUCT_FORM = "unsupported_product_form"
    INCOMPLETE_SLEEP_PROVENANCE = "incomplete_sleep_provenance"
    INVALID_CONSUMED_CAFFEINE = "invalid_consumed_caffeine"
    INVALID_PERSONAL_LIMIT = "invalid_personal_limit"
    INVALID_INPUT = "invalid_input"


class CaffeineRecommendationBasis(StrEnum):
    PERSONAL_EVENT_BASELINE = "personal_event_baseline"
    UPPER_BOUND_ONLY = "upper_bound_only"
    NO_ADDITIONAL_CAFFEINE = "no_additional_caffeine"
    UNAVAILABLE = "unavailable"


class SupportedPopulationStatus(StrEnum):
    CONFIRMED_ADULT = "confirmed_adult"
    MINOR = "minor"
    UNKNOWN = "unknown"


class CaffeineContraindication(StrEnum):
    PREGNANCY_OR_BREASTFEEDING = "pregnancy_or_breastfeeding"
    TRYING_TO_BECOME_PREGNANT = "trying_to_become_pregnant"
    RELEVANT_MEDICATION_OR_CONDITION = "relevant_medication_or_condition"
    PRONOUNCED_SENSITIVITY = "pronounced_sensitivity"
    ADVERSE_SYMPTOMS = "adverse_symptoms"


class CaffeineProductForm(StrEnum):
    BEVERAGE_OR_FOOD = "beverage_or_food"
    PURE_POWDER = "pure_powder"
    HIGHLY_CONCENTRATED_LIQUID = "highly_concentrated_liquid"


@dataclass(frozen=True, slots=True)
class SleepEvidence:
    local_date: date | None
    duration_minutes: int | None
    provider: str | None
    source_key: str | None
    freshness: EvidenceFreshness


@dataclass(frozen=True, slots=True)
class SourcedCaffeineLimit:
    amount_mg: CaffeineMg
    source: str


@dataclass(frozen=True, slots=True)
class PopulationDailyCaffeineGuardrail(SourcedCaffeineLimit):
    pass


@dataclass(frozen=True, slots=True)
class PersonalDailyCaffeineLimit(SourcedCaffeineLimit):
    pass


@dataclass(frozen=True, slots=True)
class SingleDoseCaffeineGuardrail(SourcedCaffeineLimit):
    pass


@dataclass(frozen=True, slots=True)
class PersonalEventCaffeineBaseline:
    event_id: CalendarEventId
    amount_mg: CaffeineMg
    source: str
    source_key: str
    confirmed_at: datetime
    freshness: EvidenceFreshness


@dataclass(frozen=True, slots=True)
class CaffeineTiming:
    intended_consumption_at: datetime
    target_sleep_at: datetime
    cutoff_before_sleep: timedelta


@dataclass(frozen=True, slots=True)
class CaffeineSafetyContext:
    population_status: SupportedPopulationStatus
    contraindications: frozenset[CaffeineContraindication] = frozenset()


@dataclass(frozen=True, slots=True)
class CaffeineProposalRequest:
    event_id: CalendarEventId | None
    sleep: SleepEvidence | None
    consumed_today_mg: CaffeineMg | None
    total_intake_complete: bool
    population_daily_guardrail: PopulationDailyCaffeineGuardrail
    personal_daily_limit: PersonalDailyCaffeineLimit
    single_dose_guardrail: SingleDoseCaffeineGuardrail
    personal_event_baseline: PersonalEventCaffeineBaseline | None
    timing: CaffeineTiming | None
    safety_context: CaffeineSafetyContext
    product_form: CaffeineProductForm


@dataclass(frozen=True, slots=True)
class CaffeineProposalFacts:
    request: CaffeineProposalRequest | None
    effective_daily_ceiling_mg: CaffeineMg | None
    remaining_daily_allowance_mg: CaffeineMg | None


@dataclass(frozen=True, slots=True)
class BoundedCaffeineRecommendation:
    maximum_additional_mg: CaffeineMg | None
    suggested_additional_mg: CaffeineMg | None
    basis: CaffeineRecommendationBasis


@dataclass(frozen=True, slots=True)
class CaffeineProposal:
    status: CaffeineProposalStatus
    facts: CaffeineProposalFacts
    recommendation: BoundedCaffeineRecommendation
    confidence: ProposalConfidence
    reason: CaffeineProposalReason
