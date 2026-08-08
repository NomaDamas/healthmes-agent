from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from math import ceil, isfinite
from typing import Any

from healthmes.mcp_server.caffeine import propose_caffeine
from healthmes.mcp_server.caffeine_contract import (
    BaselineFreshness,
    CaffeineContraindication,
    CaffeineMg,
    CaffeineProductForm,
    CaffeineProposalReason,
    CaffeineProposalRequest,
    CaffeineSafetyContext,
    CaffeineTiming,
    CalendarEventId,
    CandidateCaffeineEvidence,
    PersonalDailyCaffeineLimit,
    PersonalEventCaffeineBaseline,
    PopulationDailyCaffeineGuardrail,
    SingleDoseCaffeineGuardrail,
    SleepEvidence,
    SleepFreshness,
    SupportedPopulationStatus,
)

POPULATION_DAILY_GUARDRAIL_MG = CaffeineMg(400)
SINGLE_DOSE_GUARDRAIL_MG = CaffeineMg(200)
FDA_SOURCE = "fda_population_guidance"
EFSA_SOURCE = "efsa_population_guidance"
USER_SOURCE = "user_confirmed_via_agent"
BASELINE_FRESHNESS_WINDOW = timedelta(hours=24)


def select_sleep_evidence(
    rows: list[dict[str, Any]],
    *,
    event_day: date,
    today: date,
) -> tuple[SleepEvidence | None, str | None]:
    candidates: list[SleepEvidence] = []
    for row in rows:
        if row.get("date") != event_day.isoformat():
            continue
        duration = row.get("duration_minutes")
        source = row.get("source")
        provider = source.get("provider") if isinstance(source, dict) else None
        device = source.get("device") if isinstance(source, dict) else None
        if type(duration) is not int or duration <= 0 or not isinstance(provider, str):
            continue
        source_key = ":".join(
            part
            for part in (
                "sleep-summary",
                provider.strip(),
                str(device).strip() if device else None,
                event_day.isoformat(),
            )
            if part
        )
        candidates.append(
            SleepEvidence(
                local_date=event_day,
                duration_minutes=duration,
                provider=provider,
                source_key=source_key,
                freshness=(SleepFreshness.CURRENT if event_day == today else SleepFreshness.STALE),
            )
        )
    if not candidates:
        return None, "no_complete_sleep_summary"
    if len(candidates) > 1:
        return None, "ambiguous_sleep_summary"
    return candidates[0], None


def build_request(
    *,
    event_id: str | None,
    intended_consumption_at: datetime | None,
    observed_at: datetime,
    sleep: SleepEvidence | None,
    caffeine_intake: dict[str, Any] | None,
    personal_daily_limit_mg: int,
    personal_event_baseline_mg: int | None,
    baseline_confirmed_at: datetime | None,
    target_sleep_at: datetime | None,
    cutoff_before_sleep_hours: float,
    population_status: SupportedPopulationStatus,
    contraindications: list[CaffeineContraindication],
    product_form: CaffeineProductForm,
    candidate_caffeine: CandidateCaffeineEvidence | None = None,
    candidate_required: bool = False,
) -> CaffeineProposalRequest:
    consumed_today_mg, total_intake_complete = _stored_caffeine_values(caffeine_intake)
    baseline = None
    if (
        event_id is not None
        and personal_event_baseline_mg is not None
        and baseline_confirmed_at is not None
    ):
        baseline = PersonalEventCaffeineBaseline(
            event_id=CalendarEventId(event_id),
            amount_mg=CaffeineMg(personal_event_baseline_mg),
            source=USER_SOURCE,
            source_key=f"event-baseline:{event_id}:{baseline_confirmed_at.isoformat()}",
            confirmed_at=baseline_confirmed_at,
            freshness=_baseline_freshness(
                confirmed_at=baseline_confirmed_at,
                intended_consumption_at=intended_consumption_at,
                observed_at=observed_at,
            ),
        )
    timing = None
    if intended_consumption_at is not None and target_sleep_at is not None:
        timing = CaffeineTiming(
            intended_consumption_at=intended_consumption_at,
            target_sleep_at=target_sleep_at,
            cutoff_before_sleep=timedelta(hours=cutoff_before_sleep_hours),
        )
    return CaffeineProposalRequest(
        event_id=CalendarEventId(event_id) if event_id is not None else None,
        sleep=sleep,
        consumed_today_mg=consumed_today_mg,
        total_intake_complete=total_intake_complete,
        population_daily_guardrail=PopulationDailyCaffeineGuardrail(
            POPULATION_DAILY_GUARDRAIL_MG,
            FDA_SOURCE,
        ),
        personal_daily_limit=PersonalDailyCaffeineLimit(
            CaffeineMg(personal_daily_limit_mg),
            USER_SOURCE,
        ),
        single_dose_guardrail=SingleDoseCaffeineGuardrail(
            SINGLE_DOSE_GUARDRAIL_MG,
            EFSA_SOURCE,
        ),
        personal_event_baseline=baseline,
        timing=timing,
        safety_context=CaffeineSafetyContext(
            population_status=population_status,
            contraindications=frozenset(contraindications),
        ),
        product_form=product_form,
        candidate_caffeine=candidate_caffeine,
        candidate_required=candidate_required,
    )


def candidate_caffeine_from_context(
    context: dict[str, Any] | None,
    *,
    decision_request_id: str,
) -> tuple[CandidateCaffeineEvidence | None, str | None]:
    if not isinstance(context, dict) or context.get("status") != "ok":
        return None, "candidate_decision_context_unavailable"
    request = context.get("request")
    if not isinstance(request, dict) or request.get("scope") != "caffeine_sleep":
        return None, "candidate_decision_scope_is_not_caffeine_sleep"
    candidate = context.get("candidate")
    if not isinstance(candidate, dict):
        return None, "candidate_interaction_unavailable"
    if candidate.get("intent") not in {
        "ask_before_intake",
        "plan_future",
        "compare_option",
    }:
        return None, "candidate_intent_is_not_prospective"
    if candidate.get("is_confirmed_intake") is True:
        return None, "candidate_is_already_consumed"
    review = candidate.get("latest_review")
    if not isinstance(review, dict):
        return None, "candidate_nutrition_requires_owner_review"
    if review.get("status") == "rejected":
        return None, "candidate_nutrition_review_rejected"
    if review.get("status") not in {"confirmed", "corrected"}:
        return None, "candidate_nutrition_review_invalid"
    items = candidate.get("resolved_items")
    if not isinstance(items, list):
        return None, "candidate_nutrition_items_unavailable"

    if not items:
        return None, "candidate_caffeine_missing"

    total_mg = 0.0
    for item in items:
        if not isinstance(item, dict):
            return None, "candidate_nutrition_items_invalid"
        nutrients = item.get("nutrients")
        if not isinstance(nutrients, list):
            return None, "candidate_nutrition_items_invalid"
        caffeine_facts = [
            fact
            for fact in nutrients
            if isinstance(fact, dict) and str(fact.get("nutrient", "")).casefold() == "caffeine"
        ]
        if len(caffeine_facts) != 1:
            return (
                None,
                "candidate_caffeine_requires_exact_user_or_label_mg",
            )
        fact = caffeine_facts[0]
        amount = fact.get("amount")
        exact = amount.get("exact") if isinstance(amount, dict) else None
        if (
            fact.get("origin") not in {"user", "label"}
            or not isinstance(amount, dict)
            or amount.get("kind") != "exact"
            or str(amount.get("unit", "")).casefold() != "mg"
            or isinstance(exact, bool)
            or not isinstance(exact, int | float)
            or not isfinite(exact)
            or exact < 0
        ):
            return (
                None,
                "candidate_caffeine_requires_exact_user_or_label_mg",
            )
        total_mg += exact
        if not isfinite(total_mg):
            return None, "candidate_caffeine_total_invalid"
    interaction_id = candidate.get("interaction_id")
    if not isinstance(interaction_id, str) or not interaction_id.strip():
        return None, "candidate_interaction_unavailable"
    return (
        CandidateCaffeineEvidence(
            interaction_id=interaction_id,
            amount_mg=CaffeineMg(ceil(total_mg)),
            source="confirmed_intake_decision_context",
            source_key=(
                f"intake-decision-request:{decision_request_id}:candidate:{interaction_id}"
            ),
        ),
        None,
    )


def _stored_caffeine_values(
    caffeine_intake: dict[str, Any] | None,
) -> tuple[CaffeineMg | None, bool]:
    if (
        not isinstance(caffeine_intake, dict)
        or caffeine_intake.get("status") != "known"
        or caffeine_intake.get("total_intake_complete") is not True
    ):
        return None, False
    amount = caffeine_intake.get("confirmed_caffeine_mg")
    if (
        isinstance(amount, bool)
        or not isinstance(amount, int | float)
        or not isfinite(amount)
        or amount < 0
    ):
        return None, False
    # The bounded decision contract uses whole milligrams. Rounding confirmed
    # intake upward avoids overstating the remaining allowance.
    return CaffeineMg(ceil(amount)), True


def _baseline_freshness(
    *,
    confirmed_at: datetime,
    intended_consumption_at: datetime | None,
    observed_at: datetime,
) -> BaselineFreshness:
    if intended_consumption_at is None:
        return BaselineFreshness.STALE
    if confirmed_at.tzinfo is None or confirmed_at.utcoffset() is None:
        return BaselineFreshness.STALE
    if intended_consumption_at.tzinfo is None or intended_consumption_at.utcoffset() is None:
        return BaselineFreshness.STALE
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        return BaselineFreshness.STALE
    try:
        confirmed_at_utc = confirmed_at.astimezone(UTC)
        if confirmed_at_utc > observed_at.astimezone(UTC):
            return BaselineFreshness.STALE
        age = intended_consumption_at.astimezone(UTC) - confirmed_at_utc
    except (OverflowError, ValueError):
        return BaselineFreshness.STALE
    if timedelta(0) <= age <= BASELINE_FRESHNESS_WINDOW:
        return BaselineFreshness.CURRENT
    return BaselineFreshness.STALE


def serialize_proposal(
    request: CaffeineProposalRequest,
    *,
    target_event: dict[str, Any] | None,
    sleep_adapter_reason: str | None,
    caffeine_intake: dict[str, Any] | None,
    candidate_adapter_reason: str | None = None,
) -> dict[str, Any]:
    proposal = propose_caffeine(request)
    recommendation: dict[str, Any] = {
        "maximum_additional_mg": proposal.recommendation.maximum_additional_mg,
        "suggested_additional_mg": proposal.recommendation.suggested_additional_mg,
        "basis": proposal.recommendation.basis.value,
    }
    if request.candidate_caffeine is not None:
        if proposal.reason is CaffeineProposalReason.CANDIDATE_WITHIN_BOUNDED_LIMIT:
            candidate_assessment = "within_bounded_limit"
        elif proposal.reason in {
            CaffeineProposalReason.CANDIDATE_EXCEEDS_BOUNDED_LIMIT,
            CaffeineProposalReason.DAILY_LIMIT_REACHED,
            CaffeineProposalReason.DAILY_LIMIT_EXCEEDED,
        }:
            candidate_assessment = "exceeds_bounded_limit"
        else:
            candidate_assessment = "not_evaluated"
        recommendation["candidate_assessment"] = candidate_assessment
    return {
        "status": proposal.status.value,
        "facts": {
            "target_event": target_event,
            "sleep": (
                {
                    "local_date": request.sleep.local_date.isoformat()
                    if request.sleep.local_date is not None
                    else None,
                    "duration_minutes": request.sleep.duration_minutes,
                    "provider": request.sleep.provider,
                    "source_key": request.sleep.source_key,
                    "freshness": request.sleep.freshness.value,
                }
                if request.sleep is not None
                else None
            ),
            "sleep_adapter_reason": sleep_adapter_reason,
            "consumed_today_mg": request.consumed_today_mg,
            "total_intake_complete": request.total_intake_complete,
            "caffeine_intake": caffeine_intake,
            "candidate_caffeine": (
                {
                    "interaction_id": request.candidate_caffeine.interaction_id,
                    "amount_mg": request.candidate_caffeine.amount_mg,
                    "source": request.candidate_caffeine.source,
                    "source_key": request.candidate_caffeine.source_key,
                }
                if request.candidate_caffeine is not None
                else None
            ),
            "candidate_required": request.candidate_required,
            "candidate_adapter_reason": candidate_adapter_reason,
            "population_daily_guardrail": {
                "amount_mg": request.population_daily_guardrail.amount_mg,
                "source": request.population_daily_guardrail.source,
            },
            "personal_daily_limit": {
                "amount_mg": request.personal_daily_limit.amount_mg,
                "source": request.personal_daily_limit.source,
            },
            "single_dose_guardrail": {
                "amount_mg": request.single_dose_guardrail.amount_mg,
                "source": request.single_dose_guardrail.source,
            },
            "personal_event_baseline": (
                {
                    "event_id": request.personal_event_baseline.event_id,
                    "amount_mg": request.personal_event_baseline.amount_mg,
                    "source": request.personal_event_baseline.source,
                    "source_key": request.personal_event_baseline.source_key,
                    "confirmed_at": request.personal_event_baseline.confirmed_at.isoformat(),
                    "freshness": request.personal_event_baseline.freshness.value,
                }
                if request.personal_event_baseline is not None
                else None
            ),
            "timing": (
                {
                    "intended_consumption_at": request.timing.intended_consumption_at.isoformat(),
                    "target_sleep_at": request.timing.target_sleep_at.isoformat(),
                    "cutoff_before_sleep_hours": (
                        request.timing.cutoff_before_sleep.total_seconds() / 3600
                    ),
                }
                if request.timing is not None
                else None
            ),
            "safety_context": {
                "population_status": request.safety_context.population_status.value,
                "contraindications": sorted(
                    item.value for item in request.safety_context.contraindications
                ),
            },
            "product_form": request.product_form.value,
            "effective_daily_ceiling_mg": proposal.facts.effective_daily_ceiling_mg,
            "remaining_daily_allowance_mg": (proposal.facts.remaining_daily_allowance_mg),
            "candidate_total_after_intake_mg": (proposal.facts.candidate_total_after_intake_mg),
        },
        "recommendation": recommendation,
        "confidence": proposal.confidence.value,
        "reason": proposal.reason.value,
        "framing": "bounded_preparation_proposal_not_medical_advice",
    }
