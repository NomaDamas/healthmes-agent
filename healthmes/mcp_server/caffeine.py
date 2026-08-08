from __future__ import annotations

from healthmes.mcp_server.caffeine_contract import (
    BoundedCaffeineRecommendation,
    CaffeineMg,
    CaffeineProductForm,
    CaffeineProposal,
    CaffeineProposalFacts,
    CaffeineProposalReason,
    CaffeineProposalRequest,
    CaffeineProposalStatus,
    CaffeineRecommendationBasis,
    EvidenceFreshness,
    ProposalConfidence,
    SupportedPopulationStatus,
)
from healthmes.mcp_server.caffeine_validation import (
    current_event_baseline,
    invalid_reason,
    within_sleep_cutoff,
)


def propose_caffeine(request: CaffeineProposalRequest | None) -> CaffeineProposal:
    if type(request) is not CaffeineProposalRequest:
        return _result(
            None,
            CaffeineProposalStatus.INVALID_INPUT,
            CaffeineProposalReason.INVALID_INPUT,
        )
    if reason := invalid_reason(request):
        return _result(request, CaffeineProposalStatus.INVALID_INPUT, reason)
    if request.candidate_required and request.candidate_caffeine is None:
        return _insufficient(
            request,
            CaffeineProposalReason.MISSING_CANDIDATE_CAFFEINE,
        )
    if request.event_id is None and request.candidate_caffeine is None:
        return _insufficient(request, CaffeineProposalReason.MISSING_TARGET_EVENT)
    if request.sleep is None:
        return _insufficient(request, CaffeineProposalReason.MISSING_SLEEP)
    if not (
        request.sleep.local_date is not None
        and request.sleep.duration_minutes is not None
        and bool(request.sleep.provider and request.sleep.provider.strip())
        and bool(request.sleep.source_key and request.sleep.source_key.strip())
    ):
        return _insufficient(request, CaffeineProposalReason.INCOMPLETE_SLEEP_PROVENANCE)
    if request.sleep.freshness is not EvidenceFreshness.CURRENT:
        return _insufficient(request, CaffeineProposalReason.STALE_SLEEP)
    if request.consumed_today_mg is None:
        return _insufficient(request, CaffeineProposalReason.MISSING_TOTAL_INTAKE)
    if not request.total_intake_complete:
        return _insufficient(request, CaffeineProposalReason.INCOMPLETE_TOTAL_INTAKE)
    if request.timing is None:
        return _insufficient(request, CaffeineProposalReason.MISSING_TIMING)
    if request.sleep.local_date != request.timing.intended_consumption_at.date():
        return _insufficient(request, CaffeineProposalReason.STALE_SLEEP)

    population = request.safety_context.population_status
    if population is SupportedPopulationStatus.MINOR:
        return _blocked(request, CaffeineProposalReason.UNSUPPORTED_POPULATION)
    if population is not SupportedPopulationStatus.CONFIRMED_ADULT:
        return _insufficient(request, CaffeineProposalReason.UNKNOWN_POPULATION)
    if request.safety_context.contraindications:
        return _blocked(request, CaffeineProposalReason.CLINICIAN_GUIDANCE_REQUIRED)
    if request.product_form is not CaffeineProductForm.BEVERAGE_OR_FOOD:
        return _blocked(request, CaffeineProposalReason.UNSUPPORTED_PRODUCT_FORM)
    cutoff = within_sleep_cutoff(request)
    if cutoff is None:
        return _result(
            request,
            CaffeineProposalStatus.INVALID_INPUT,
            CaffeineProposalReason.INVALID_INPUT,
        )
    if cutoff:
        return _blocked(request, CaffeineProposalReason.WITHIN_SLEEP_CUTOFF)

    ceiling = CaffeineMg(
        min(
            request.population_daily_guardrail.amount_mg,
            request.personal_daily_limit.amount_mg,
        )
    )
    raw_remaining = ceiling - request.consumed_today_mg
    remaining = CaffeineMg(max(0, raw_remaining))
    candidate = request.candidate_caffeine
    candidate_total = (
        CaffeineMg(request.consumed_today_mg + candidate.amount_mg)
        if candidate is not None
        else None
    )
    if raw_remaining < 0 or (raw_remaining == 0 and (candidate is None or candidate.amount_mg > 0)):
        reason = (
            CaffeineProposalReason.DAILY_LIMIT_REACHED
            if raw_remaining == 0
            else CaffeineProposalReason.DAILY_LIMIT_EXCEEDED
        )
        return _result(
            request,
            CaffeineProposalStatus.NOOP,
            reason,
            ProposalConfidence.HIGH,
            ceiling,
            remaining,
            CaffeineMg(0),
            CaffeineMg(0),
            CaffeineRecommendationBasis.NO_ADDITIONAL_CAFFEINE,
            candidate_total=candidate_total,
        )

    maximum = CaffeineMg(min(request.single_dose_guardrail.amount_mg, remaining))
    if candidate is not None:
        if candidate.amount_mg > maximum:
            return _result(
                request,
                CaffeineProposalStatus.NOOP,
                CaffeineProposalReason.CANDIDATE_EXCEEDS_BOUNDED_LIMIT,
                ProposalConfidence.HIGH,
                ceiling,
                remaining,
                maximum,
                basis=CaffeineRecommendationBasis.CONFIRMED_CANDIDATE,
                candidate_total=candidate_total,
            )
        return _result(
            request,
            CaffeineProposalStatus.PROPOSAL,
            CaffeineProposalReason.CANDIDATE_WITHIN_BOUNDED_LIMIT,
            ProposalConfidence.HIGH,
            ceiling,
            remaining,
            maximum,
            candidate.amount_mg,
            CaffeineRecommendationBasis.CONFIRMED_CANDIDATE,
            candidate_total=candidate_total,
        )
    baseline = request.personal_event_baseline
    if not current_event_baseline(baseline, request):
        return _result(
            request,
            CaffeineProposalStatus.PROPOSAL,
            CaffeineProposalReason.PERSONAL_EVENT_BASELINE_UNAVAILABLE,
            ProposalConfidence.MEDIUM,
            ceiling,
            remaining,
            maximum,
            basis=CaffeineRecommendationBasis.UPPER_BOUND_ONLY,
        )
    return _result(
        request,
        CaffeineProposalStatus.PROPOSAL,
        CaffeineProposalReason.PERSONAL_EVENT_BASELINE_APPLIED,
        ProposalConfidence.MEDIUM,
        ceiling,
        remaining,
        maximum,
        CaffeineMg(min(baseline.amount_mg, maximum)),
        CaffeineRecommendationBasis.PERSONAL_EVENT_BASELINE,
    )


def _insufficient(
    request: CaffeineProposalRequest,
    reason: CaffeineProposalReason,
) -> CaffeineProposal:
    return _result(request, CaffeineProposalStatus.INSUFFICIENT_DATA, reason)


def _blocked(
    request: CaffeineProposalRequest,
    reason: CaffeineProposalReason,
) -> CaffeineProposal:
    return _result(request, CaffeineProposalStatus.NOOP, reason)


def _result(
    request: CaffeineProposalRequest | None,
    status: CaffeineProposalStatus,
    reason: CaffeineProposalReason,
    confidence: ProposalConfidence = ProposalConfidence.LOW,
    ceiling: CaffeineMg | None = None,
    remaining: CaffeineMg | None = None,
    maximum: CaffeineMg | None = None,
    suggested: CaffeineMg | None = None,
    basis: CaffeineRecommendationBasis = CaffeineRecommendationBasis.UNAVAILABLE,
    candidate_total: CaffeineMg | None = None,
) -> CaffeineProposal:
    facts = CaffeineProposalFacts(
        request,
        ceiling,
        remaining,
        candidate_total,
    )
    recommendation = BoundedCaffeineRecommendation(maximum, suggested, basis)
    return CaffeineProposal(status, facts, recommendation, confidence, reason)
