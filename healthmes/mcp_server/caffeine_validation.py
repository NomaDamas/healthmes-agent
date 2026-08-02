from __future__ import annotations

from datetime import date, datetime, timedelta

from healthmes.mcp_server.caffeine_contract import (
    CaffeineContraindication,
    CaffeineProposalReason,
    CaffeineProposalRequest,
    CaffeineSafetyContext,
    CaffeineTiming,
    EvidenceFreshness,
    PersonalDailyCaffeineLimit,
    PersonalEventCaffeineBaseline,
    PopulationDailyCaffeineGuardrail,
    SingleDoseCaffeineGuardrail,
    SleepEvidence,
)


def invalid_reason(request: CaffeineProposalRequest) -> CaffeineProposalReason | None:
    if _malformed(request):
        return CaffeineProposalReason.INVALID_INPUT
    if request.event_id is not None and not request.event_id.strip():
        return CaffeineProposalReason.INVALID_INPUT
    if request.sleep is not None and request.sleep.duration_minutes is not None:
        if request.sleep.duration_minutes <= 0:
            return CaffeineProposalReason.INVALID_INPUT
    if request.consumed_today_mg is not None and request.consumed_today_mg < 0:
        return CaffeineProposalReason.INVALID_CONSUMED_CAFFEINE
    if (
        request.personal_daily_limit.amount_mg <= 0
        or not request.personal_daily_limit.source.strip()
    ):
        return CaffeineProposalReason.INVALID_PERSONAL_LIMIT
    if any(
        limit.amount_mg <= 0 or not limit.source.strip()
        for limit in (
            request.population_daily_guardrail,
            request.single_dose_guardrail,
        )
    ):
        return CaffeineProposalReason.INVALID_INPUT
    if (
        request.personal_event_baseline is not None
        and request.personal_event_baseline.amount_mg <= 0
    ):
        return CaffeineProposalReason.INVALID_INPUT
    if request.timing is not None and _invalid_timing(request.timing):
        return CaffeineProposalReason.INVALID_INPUT
    return None


def current_event_baseline(
    baseline: PersonalEventCaffeineBaseline | None,
    request: CaffeineProposalRequest,
) -> bool:
    timing = request.timing
    try:
        return (
            baseline is not None
            and baseline.freshness is EvidenceFreshness.CURRENT
            and baseline.event_id == request.event_id
            and bool(baseline.source.strip())
            and bool(baseline.source_key.strip())
            and _aware(baseline.confirmed_at)
            and timing is not None
            and baseline.confirmed_at <= timing.intended_consumption_at
        )
    except Exception:
        return False


def within_sleep_cutoff(request: CaffeineProposalRequest) -> bool | None:
    timing = request.timing
    if timing is None:
        return None
    try:
        return timing.target_sleep_at - timing.intended_consumption_at < timing.cutoff_before_sleep
    except Exception:
        return None


def _malformed(request: CaffeineProposalRequest) -> bool:
    sleep = request.sleep
    baseline = request.personal_event_baseline
    timing = request.timing
    safety = request.safety_context
    limits = (
        (request.population_daily_guardrail, PopulationDailyCaffeineGuardrail),
        (request.personal_daily_limit, PersonalDailyCaffeineLimit),
        (request.single_dose_guardrail, SingleDoseCaffeineGuardrail),
    )
    return (
        (request.event_id is not None and type(request.event_id) is not str)
        or (
            sleep is not None
            and (
                type(sleep) is not SleepEvidence
                or (sleep.local_date is not None and type(sleep.local_date) is not date)
                or (sleep.duration_minutes is not None and type(sleep.duration_minutes) is not int)
                or (sleep.provider is not None and type(sleep.provider) is not str)
                or (sleep.source_key is not None and type(sleep.source_key) is not str)
                or type(sleep.freshness) is not EvidenceFreshness
            )
        )
        or (request.consumed_today_mg is not None and type(request.consumed_today_mg) is not int)
        or type(request.total_intake_complete) is not bool
        or any(
            type(limit) is not expected
            or type(limit.amount_mg) is not int
            or type(limit.source) is not str
            for limit, expected in limits
        )
        or (
            baseline is not None
            and (
                type(baseline) is not PersonalEventCaffeineBaseline
                or type(baseline.event_id) is not str
                or type(baseline.amount_mg) is not int
                or type(baseline.source) is not str
                or type(baseline.source_key) is not str
                or type(baseline.confirmed_at) is not datetime
                or type(baseline.freshness) is not EvidenceFreshness
            )
        )
        or (
            timing is not None
            and (
                type(timing) is not CaffeineTiming
                or type(timing.intended_consumption_at) is not datetime
                or type(timing.target_sleep_at) is not datetime
                or type(timing.cutoff_before_sleep) is not timedelta
            )
        )
        or type(safety) is not CaffeineSafetyContext
        or type(safety.contraindications) is not frozenset
        or any(type(item) is not CaffeineContraindication for item in safety.contraindications)
    )


def _invalid_timing(timing: CaffeineTiming) -> bool:
    try:
        return (
            not _aware(timing.intended_consumption_at)
            or not _aware(timing.target_sleep_at)
            or timing.target_sleep_at <= timing.intended_consumption_at
            or timing.cutoff_before_sleep <= timedelta(0)
        )
    except Exception:
        return True


def _aware(value: datetime) -> bool:
    try:
        return value.tzinfo is not None and value.utcoffset() is not None
    except Exception:
        return False
