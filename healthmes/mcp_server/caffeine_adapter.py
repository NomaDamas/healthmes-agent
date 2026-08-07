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
    CaffeineProposalRequest,
    CaffeineSafetyContext,
    CaffeineTiming,
    CalendarEventId,
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
) -> CaffeineProposalRequest:
    consumed_today_mg, total_intake_complete = _stored_caffeine_values(
        caffeine_intake
    )
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
        or not isinstance(amount, (int, float))
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
) -> dict[str, Any]:
    proposal = propose_caffeine(request)
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
        },
        "recommendation": {
            "maximum_additional_mg": proposal.recommendation.maximum_additional_mg,
            "suggested_additional_mg": proposal.recommendation.suggested_additional_mg,
            "basis": proposal.recommendation.basis.value,
        },
        "confidence": proposal.confidence.value,
        "reason": proposal.reason.value,
        "framing": "bounded_preparation_proposal_not_medical_advice",
    }
