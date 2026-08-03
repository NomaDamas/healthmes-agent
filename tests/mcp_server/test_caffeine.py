from dataclasses import fields, replace
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from typing import cast

import pytest

from healthmes.mcp_server.caffeine import propose_caffeine
from healthmes.mcp_server.caffeine_contract import (
    BaselineFreshness,
    BoundedCaffeineRecommendation,
    CaffeineContraindication,
    CaffeineMg,
    CaffeineProductForm,
    CaffeineProposal,
    CaffeineProposalFacts,
    CaffeineProposalReason,
    CaffeineProposalRequest,
    CaffeineProposalStatus,
    CaffeineRecommendationBasis,
    CaffeineSafetyContext,
    CaffeineTiming,
    CalendarEventId,
    PersonalDailyCaffeineLimit,
    PersonalEventCaffeineBaseline,
    PopulationDailyCaffeineGuardrail,
    ProposalConfidence,
    SingleDoseCaffeineGuardrail,
    SleepEvidence,
    SleepFreshness,
    SupportedPopulationStatus,
)

KST = timezone(timedelta(hours=9))
EVENT_ID = CalendarEventId("coffee-chat-2026-07-31")


class ExplodingTimezone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        raise RuntimeError("invalid external timezone")

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None


def _event_baseline(amount_mg: int = 100) -> PersonalEventCaffeineBaseline:
    return PersonalEventCaffeineBaseline(
        event_id=EVENT_ID,
        amount_mg=CaffeineMg(amount_mg),
        source="user_confirmed",
        source_key="event-baseline:coffee-chat-2026-07-31",
        confirmed_at=datetime(2026, 7, 31, 12, tzinfo=KST),
        freshness=BaselineFreshness.CURRENT,
    )


def _valid_request() -> CaffeineProposalRequest:
    return CaffeineProposalRequest(
        event_id=EVENT_ID,
        sleep=SleepEvidence(
            local_date=date(2026, 7, 31),
            duration_minutes=374,
            provider="oura",
            source_key="oura:2026-07-31",
            freshness=SleepFreshness.CURRENT,
        ),
        consumed_today_mg=CaffeineMg(100),
        total_intake_complete=True,
        population_daily_guardrail=PopulationDailyCaffeineGuardrail(
            amount_mg=CaffeineMg(400),
            source="fda_population_guidance",
        ),
        personal_daily_limit=PersonalDailyCaffeineLimit(
            amount_mg=CaffeineMg(300),
            source="user_profile",
        ),
        single_dose_guardrail=SingleDoseCaffeineGuardrail(
            amount_mg=CaffeineMg(200),
            source="efsa_population_guidance",
        ),
        personal_event_baseline=_event_baseline(),
        timing=CaffeineTiming(
            intended_consumption_at=datetime(2026, 7, 31, 13, tzinfo=KST),
            target_sleep_at=datetime(2026, 7, 31, 23, tzinfo=KST),
            cutoff_before_sleep=timedelta(hours=6),
        ),
        safety_context=CaffeineSafetyContext(
            population_status=SupportedPopulationStatus.CONFIRMED_ADULT,
        ),
        product_form=CaffeineProductForm.BEVERAGE_OR_FOOD,
    )


def test_valid_inputs_return_personalized_bounded_proposal() -> None:
    request = _valid_request()

    result = propose_caffeine(request)

    assert result == CaffeineProposal(
        status=CaffeineProposalStatus.PROPOSAL,
        facts=CaffeineProposalFacts(
            request=request,
            effective_daily_ceiling_mg=CaffeineMg(300),
            remaining_daily_allowance_mg=CaffeineMg(200),
        ),
        recommendation=BoundedCaffeineRecommendation(
            maximum_additional_mg=CaffeineMg(200),
            suggested_additional_mg=CaffeineMg(100),
            basis=CaffeineRecommendationBasis.PERSONAL_EVENT_BASELINE,
        ),
        confidence=ProposalConfidence.MEDIUM,
        reason=CaffeineProposalReason.PERSONAL_EVENT_BASELINE_APPLIED,
    )


def test_consumed_at_daily_limit_returns_zero_mg_noop() -> None:
    request = replace(_valid_request(), consumed_today_mg=CaffeineMg(300))

    result = propose_caffeine(request)

    assert result.status is CaffeineProposalStatus.NOOP
    assert result.facts.remaining_daily_allowance_mg == 0
    assert result.recommendation == BoundedCaffeineRecommendation(
        maximum_additional_mg=CaffeineMg(0),
        suggested_additional_mg=CaffeineMg(0),
        basis=CaffeineRecommendationBasis.NO_ADDITIONAL_CAFFEINE,
    )
    assert result.reason is CaffeineProposalReason.DAILY_LIMIT_REACHED


def test_consumed_above_daily_limit_clamps_to_zero_noop() -> None:
    request = replace(_valid_request(), consumed_today_mg=CaffeineMg(350))

    result = propose_caffeine(request)

    assert result.status is CaffeineProposalStatus.NOOP
    assert result.facts.remaining_daily_allowance_mg == 0
    assert result.recommendation.maximum_additional_mg == 0
    assert result.recommendation.suggested_additional_mg == 0
    assert result.reason is CaffeineProposalReason.DAILY_LIMIT_EXCEEDED


def test_single_dose_guardrail_caps_additional_amount() -> None:
    request = replace(
        _valid_request(),
        consumed_today_mg=CaffeineMg(0),
        personal_daily_limit=PersonalDailyCaffeineLimit(
            amount_mg=CaffeineMg(400),
            source="user_profile",
        ),
        single_dose_guardrail=SingleDoseCaffeineGuardrail(
            amount_mg=CaffeineMg(150),
            source="configured_population_guidance",
        ),
        personal_event_baseline=_event_baseline(180),
    )

    result = propose_caffeine(request)

    assert result.facts.remaining_daily_allowance_mg == 400
    assert result.recommendation.maximum_additional_mg == 150
    assert result.recommendation.suggested_additional_mg == 150


@pytest.mark.parametrize(
    "baseline",
    [
        None,
        replace(_event_baseline(), freshness=BaselineFreshness.STALE),
    ],
)
def test_unavailable_event_baseline_returns_upper_bound_only(
    baseline: PersonalEventCaffeineBaseline | None,
) -> None:
    request = replace(_valid_request(), personal_event_baseline=baseline)

    result = propose_caffeine(request)

    assert result.status is CaffeineProposalStatus.PROPOSAL
    assert result.recommendation == BoundedCaffeineRecommendation(
        maximum_additional_mg=CaffeineMg(200),
        suggested_additional_mg=None,
        basis=CaffeineRecommendationBasis.UPPER_BOUND_ONLY,
    )
    assert result.reason is CaffeineProposalReason.PERSONAL_EVENT_BASELINE_UNAVAILABLE


@pytest.mark.parametrize(
    "baseline",
    [
        replace(_event_baseline(), event_id=CalendarEventId("another-event")),
        replace(_event_baseline(), source=""),
        replace(_event_baseline(), source_key=""),
        replace(_event_baseline(), confirmed_at=datetime(2026, 7, 31, 12)),
        replace(
            _event_baseline(),
            confirmed_at=datetime(2026, 7, 31, 14, tzinfo=KST),
        ),
    ],
)
def test_unbound_event_baseline_never_returns_exact_suggestion(
    baseline: PersonalEventCaffeineBaseline,
) -> None:
    result = propose_caffeine(replace(_valid_request(), personal_event_baseline=baseline))

    assert result.status is CaffeineProposalStatus.PROPOSAL
    assert result.recommendation.maximum_additional_mg == 200
    assert result.recommendation.suggested_additional_mg is None
    assert result.reason is CaffeineProposalReason.PERSONAL_EVENT_BASELINE_UNAVAILABLE


def test_nonempty_baseline_source_key_is_audit_provenance() -> None:
    baseline = replace(_event_baseline(), source_key="audit-record:another-opaque-id")

    result = propose_caffeine(replace(_valid_request(), personal_event_baseline=baseline))

    assert result.status is CaffeineProposalStatus.PROPOSAL
    assert result.recommendation.suggested_additional_mg == 100
    assert result.reason is CaffeineProposalReason.PERSONAL_EVENT_BASELINE_APPLIED


def test_negative_consumed_caffeine_is_invalid() -> None:
    request = replace(_valid_request(), consumed_today_mg=CaffeineMg(-10))

    result = propose_caffeine(request)

    assert result.status is CaffeineProposalStatus.INVALID_INPUT
    assert result.facts.effective_daily_ceiling_mg is None
    assert result.facts.remaining_daily_allowance_mg is None
    assert result.recommendation.maximum_additional_mg is None
    assert result.recommendation.suggested_additional_mg is None
    assert result.reason is CaffeineProposalReason.INVALID_CONSUMED_CAFFEINE


@pytest.mark.parametrize("limit_mg", [0, -10])
def test_non_positive_personal_limit_is_invalid(limit_mg: int) -> None:
    request = replace(
        _valid_request(),
        personal_daily_limit=PersonalDailyCaffeineLimit(
            amount_mg=CaffeineMg(limit_mg),
            source="user_profile",
        ),
    )

    result = propose_caffeine(request)

    assert result.status is CaffeineProposalStatus.INVALID_INPUT
    assert result.recommendation.maximum_additional_mg is None
    assert result.recommendation.suggested_additional_mg is None
    assert result.reason is CaffeineProposalReason.INVALID_PERSONAL_LIMIT


def test_missing_calendar_event_returns_insufficient_data() -> None:
    result = propose_caffeine(replace(_valid_request(), event_id=None))

    assert result.status is CaffeineProposalStatus.INSUFFICIENT_DATA
    assert result.recommendation.maximum_additional_mg is None
    assert result.recommendation.suggested_additional_mg is None
    assert result.reason is CaffeineProposalReason.MISSING_TARGET_EVENT


def test_missing_sleep_returns_insufficient_data() -> None:
    result = propose_caffeine(replace(_valid_request(), sleep=None))

    assert result.status is CaffeineProposalStatus.INSUFFICIENT_DATA
    assert result.recommendation.maximum_additional_mg is None
    assert result.recommendation.suggested_additional_mg is None
    assert result.reason is CaffeineProposalReason.MISSING_SLEEP


def test_stale_sleep_returns_insufficient_data() -> None:
    request = _valid_request()
    stale_sleep = replace(request.sleep, freshness=SleepFreshness.STALE)

    result = propose_caffeine(replace(request, sleep=stale_sleep))

    assert result.status is CaffeineProposalStatus.INSUFFICIENT_DATA
    assert result.recommendation.maximum_additional_mg is None
    assert result.recommendation.suggested_additional_mg is None
    assert result.reason is CaffeineProposalReason.STALE_SLEEP


@pytest.mark.parametrize("sleep_date", [date(2026, 7, 30), date(2099, 1, 1)])
def test_sleep_date_must_match_proposal_date(sleep_date: date) -> None:
    request = _valid_request()
    sleep = replace(request.sleep, local_date=sleep_date)

    result = propose_caffeine(replace(request, sleep=sleep))

    assert result.status is CaffeineProposalStatus.INSUFFICIENT_DATA
    assert result.recommendation.maximum_additional_mg is None
    assert result.recommendation.suggested_additional_mg is None
    assert result.reason is CaffeineProposalReason.STALE_SLEEP


def test_timing_from_another_local_day_convention_fails_closed() -> None:
    request = _valid_request()
    timing = CaffeineTiming(
        intended_consumption_at=datetime(2026, 7, 30, 16, tzinfo=UTC),
        target_sleep_at=datetime(2026, 7, 31, 14, tzinfo=UTC),
        cutoff_before_sleep=timedelta(hours=6),
    )

    result = propose_caffeine(replace(request, timing=timing))

    assert result.status is CaffeineProposalStatus.INSUFFICIENT_DATA
    assert result.recommendation.maximum_additional_mg is None
    assert result.recommendation.suggested_additional_mg is None
    assert result.reason is CaffeineProposalReason.STALE_SLEEP


@pytest.mark.parametrize(
    "sleep",
    [
        replace(_valid_request().sleep, provider=None),
        replace(_valid_request().sleep, source_key=None),
        replace(_valid_request().sleep, local_date=None),
        replace(_valid_request().sleep, duration_minutes=None),
    ],
)
def test_incomplete_sleep_provenance_returns_insufficient_data(
    sleep: SleepEvidence,
) -> None:
    result = propose_caffeine(replace(_valid_request(), sleep=sleep))

    assert result.status is CaffeineProposalStatus.INSUFFICIENT_DATA
    assert result.recommendation.maximum_additional_mg is None
    assert result.recommendation.suggested_additional_mg is None
    assert result.reason is CaffeineProposalReason.INCOMPLETE_SLEEP_PROVENANCE


@pytest.mark.parametrize(
    ("proposal_request", "reason"),
    [
        (
            replace(_valid_request(), consumed_today_mg=None),
            CaffeineProposalReason.MISSING_TOTAL_INTAKE,
        ),
        (
            replace(_valid_request(), total_intake_complete=False),
            CaffeineProposalReason.INCOMPLETE_TOTAL_INTAKE,
        ),
        (replace(_valid_request(), timing=None), CaffeineProposalReason.MISSING_TIMING),
        (
            replace(
                _valid_request(),
                safety_context=CaffeineSafetyContext(
                    population_status=SupportedPopulationStatus.UNKNOWN,
                ),
            ),
            CaffeineProposalReason.UNKNOWN_POPULATION,
        ),
    ],
)
def test_other_missing_required_data_returns_insufficient_data(
    proposal_request: CaffeineProposalRequest,
    reason: CaffeineProposalReason,
) -> None:
    result = propose_caffeine(proposal_request)

    assert result.status is CaffeineProposalStatus.INSUFFICIENT_DATA
    assert result.recommendation.maximum_additional_mg is None
    assert result.recommendation.suggested_additional_mg is None
    assert result.reason is reason


@pytest.mark.parametrize(
    "proposal_request",
    [
        replace(_valid_request(), event_id=CalendarEventId("")),
        replace(
            _valid_request(),
            sleep=replace(_valid_request().sleep, duration_minutes=0),
        ),
        replace(
            _valid_request(),
            population_daily_guardrail=PopulationDailyCaffeineGuardrail(
                amount_mg=CaffeineMg(0),
                source="invalid",
            ),
        ),
        replace(
            _valid_request(),
            single_dose_guardrail=SingleDoseCaffeineGuardrail(
                amount_mg=CaffeineMg(0),
                source="invalid",
            ),
        ),
        replace(
            _valid_request(),
            personal_event_baseline=replace(_event_baseline(), amount_mg=CaffeineMg(0)),
        ),
        replace(
            _valid_request(),
            timing=CaffeineTiming(
                intended_consumption_at=datetime(2026, 7, 31, 13),
                target_sleep_at=datetime(2026, 7, 31, 23),
                cutoff_before_sleep=timedelta(hours=6),
            ),
        ),
    ],
)
def test_invalid_or_contradictory_inputs_return_invalid(
    proposal_request: CaffeineProposalRequest,
) -> None:
    result = propose_caffeine(proposal_request)

    assert result.status is CaffeineProposalStatus.INVALID_INPUT
    assert result.recommendation.maximum_additional_mg is None
    assert result.recommendation.suggested_additional_mg is None
    assert result.reason is CaffeineProposalReason.INVALID_INPUT


@pytest.mark.parametrize(
    "proposal_request",
    [
        replace(_valid_request(), consumed_today_mg=cast(CaffeineMg, float("nan"))),
        replace(_valid_request(), consumed_today_mg=cast(CaffeineMg, 12.5)),
        replace(
            _valid_request(),
            population_daily_guardrail=PopulationDailyCaffeineGuardrail(
                amount_mg=cast(CaffeineMg, "400"),
                source="invalid_runtime_value",
            ),
        ),
        replace(
            _valid_request(),
            sleep=replace(_valid_request().sleep, provider=cast(str, 7)),
        ),
        replace(
            _valid_request(),
            personal_event_baseline=replace(
                _event_baseline(),
                source=cast(str, 7),
            ),
        ),
        replace(
            _valid_request(),
            total_intake_complete=cast(bool, "false"),
        ),
    ],
)
def test_malformed_runtime_values_fail_closed(
    proposal_request: CaffeineProposalRequest,
) -> None:
    result = propose_caffeine(proposal_request)

    assert result.status is CaffeineProposalStatus.INVALID_INPUT
    assert result.facts.effective_daily_ceiling_mg is None
    assert result.facts.remaining_daily_allowance_mg is None
    assert result.recommendation.maximum_additional_mg is None
    assert result.recommendation.suggested_additional_mg is None
    assert result.reason is CaffeineProposalReason.INVALID_INPUT


def test_missing_request_object_fails_closed() -> None:
    result = propose_caffeine(None)

    assert result.status is CaffeineProposalStatus.INVALID_INPUT
    assert result.facts.request is None
    assert result.recommendation.maximum_additional_mg is None
    assert result.recommendation.suggested_additional_mg is None


def test_exceptional_timing_timezone_fails_closed() -> None:
    request = _valid_request()
    timing = replace(
        request.timing,
        intended_consumption_at=datetime(2026, 7, 31, 13, tzinfo=ExplodingTimezone()),
    )

    result = propose_caffeine(replace(request, timing=timing))

    assert result.status is CaffeineProposalStatus.INVALID_INPUT
    assert result.recommendation.maximum_additional_mg is None
    assert result.recommendation.suggested_additional_mg is None


def test_exceptional_baseline_timezone_removes_exact_suggestion() -> None:
    request = _valid_request()
    baseline = replace(
        _event_baseline(),
        confirmed_at=datetime(2026, 7, 31, 12, tzinfo=ExplodingTimezone()),
    )

    result = propose_caffeine(replace(request, personal_event_baseline=baseline))

    assert result.status is CaffeineProposalStatus.PROPOSAL
    assert result.recommendation.maximum_additional_mg == 200
    assert result.recommendation.suggested_additional_mg is None
    assert result.reason is CaffeineProposalReason.PERSONAL_EVENT_BASELINE_UNAVAILABLE


def test_same_inputs_always_return_same_result() -> None:
    first = propose_caffeine(_valid_request())
    second = propose_caffeine(_valid_request())

    assert first == second


def test_output_preserves_safety_boundary() -> None:
    request = _valid_request()

    result = propose_caffeine(request)

    assert result.facts.request.personal_daily_limit == request.personal_daily_limit
    assert result.facts.request.personal_daily_limit.source == "user_profile"
    assert {field.name for field in fields(CaffeineProposal)} == {
        "status",
        "facts",
        "recommendation",
        "confidence",
        "reason",
    }
    assert {field.name for field in fields(BoundedCaffeineRecommendation)} == {
        "maximum_additional_mg",
        "suggested_additional_mg",
        "basis",
    }
    output_text_values = (
        result.status.value,
        result.reason.value,
        result.confidence.value,
        result.recommendation.basis.value,
        result.facts.request.personal_daily_limit.source,
    )
    forbidden_claims = ("safe", "안전", "medical", "의료")
    assert all(
        claim not in text.lower() for text in output_text_values for claim in forbidden_claims
    )


def test_short_sleep_never_increases_the_proposal() -> None:
    short_sleep_request = _valid_request()
    longer_sleep_request = replace(
        short_sleep_request,
        sleep=replace(short_sleep_request.sleep, duration_minutes=480),
    )

    short_sleep_result = propose_caffeine(short_sleep_request)
    longer_sleep_result = propose_caffeine(longer_sleep_request)

    assert short_sleep_result.recommendation == longer_sleep_result.recommendation


def test_consumption_within_sleep_cutoff_returns_noop_without_numeric_proposal() -> None:
    request = replace(
        _valid_request(),
        timing=CaffeineTiming(
            intended_consumption_at=datetime(2026, 7, 31, 18, tzinfo=KST),
            target_sleep_at=datetime(2026, 7, 31, 23, tzinfo=KST),
            cutoff_before_sleep=timedelta(hours=6),
        ),
    )

    result = propose_caffeine(request)

    assert result.status is CaffeineProposalStatus.NOOP
    assert result.recommendation.maximum_additional_mg is None
    assert result.recommendation.suggested_additional_mg is None
    assert result.reason is CaffeineProposalReason.WITHIN_SLEEP_CUTOFF


def test_consumption_at_sleep_cutoff_is_allowed() -> None:
    request = replace(
        _valid_request(),
        timing=CaffeineTiming(
            intended_consumption_at=datetime(2026, 7, 31, 17, tzinfo=KST),
            target_sleep_at=datetime(2026, 7, 31, 23, tzinfo=KST),
            cutoff_before_sleep=timedelta(hours=6),
        ),
    )

    result = propose_caffeine(request)

    assert result.status is CaffeineProposalStatus.PROPOSAL
    assert result.recommendation.suggested_additional_mg == 100


def test_minor_returns_noop_without_numeric_proposal() -> None:
    request = replace(
        _valid_request(),
        safety_context=CaffeineSafetyContext(
            population_status=SupportedPopulationStatus.MINOR,
        ),
    )

    result = propose_caffeine(request)

    assert result.status is CaffeineProposalStatus.NOOP
    assert result.recommendation.maximum_additional_mg is None
    assert result.reason is CaffeineProposalReason.UNSUPPORTED_POPULATION


@pytest.mark.parametrize("raw_status", ["minor", "confirmed_adult", "future_status"])
def test_unvalidated_population_value_fails_closed(raw_status: str) -> None:
    request = replace(
        _valid_request(),
        safety_context=CaffeineSafetyContext(
            population_status=cast(SupportedPopulationStatus, raw_status),
        ),
    )

    result = propose_caffeine(request)

    assert result.status is CaffeineProposalStatus.INSUFFICIENT_DATA
    assert result.recommendation.maximum_additional_mg is None
    assert result.recommendation.suggested_additional_mg is None
    assert result.reason is CaffeineProposalReason.UNKNOWN_POPULATION


@pytest.mark.parametrize("contraindication", list(CaffeineContraindication))
def test_declared_contraindication_returns_noop_without_numeric_proposal(
    contraindication: CaffeineContraindication,
) -> None:
    request = replace(
        _valid_request(),
        safety_context=CaffeineSafetyContext(
            population_status=SupportedPopulationStatus.CONFIRMED_ADULT,
            contraindications=frozenset({contraindication}),
        ),
    )

    result = propose_caffeine(request)

    assert result.status is CaffeineProposalStatus.NOOP
    assert result.recommendation.maximum_additional_mg is None
    assert result.reason is CaffeineProposalReason.CLINICIAN_GUIDANCE_REQUIRED


@pytest.mark.parametrize(
    "product_form",
    [CaffeineProductForm.PURE_POWDER, CaffeineProductForm.HIGHLY_CONCENTRATED_LIQUID],
)
def test_high_concentration_product_returns_noop_without_numeric_proposal(
    product_form: CaffeineProductForm,
) -> None:
    request = replace(_valid_request(), product_form=product_form)

    result = propose_caffeine(request)

    assert result.status is CaffeineProposalStatus.NOOP
    assert result.recommendation.maximum_additional_mg is None
    assert result.reason is CaffeineProposalReason.UNSUPPORTED_PRODUCT_FORM
