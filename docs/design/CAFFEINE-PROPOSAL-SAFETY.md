# Caffeine proposal safety contract

The caffeine decision function produces a bounded preparation proposal. It does
not determine a safe dose, prescribe caffeine, or provide medical advice.

## Inputs stay distinct

- `population_daily_guardrail` is a sourced population reference.
- `personal_daily_limit` is a user-provided ceiling, not a medically validated
  allowance.
- `single_dose_guardrail` independently caps one proposal.
- `personal_event_baseline` is the user's previously confirmed amount for the
  exact target event. The event ID is the binding key; the non-empty source key
  is opaque provenance retained for audit. An exact suggestion requires both,
  plus an aware confirmation time and current freshness.
- `consumed_today_mg` must represent complete verified intake from drinks,
  food, supplements, and medications.
- `sleep.local_date` and `timing.intended_consumption_at` must use the same
  user-local day convention. Adapters normalize both before calling the pure
  function; a mismatch fails closed as stale sleep.

The deterministic bounds are:

```text
daily_remaining_mg =
  max(0, min(population_daily_guardrail_mg, personal_daily_limit_mg)
         - verified_total_consumed_today_mg)

maximum_additional_mg =
  min(single_dose_guardrail_mg, daily_remaining_mg)
```

An exact `suggested_additional_mg` is the smaller of that maximum and the
current event-bound baseline. Without that baseline, the result may expose only
the upper bound.

## Fail-closed boundaries

The function returns `insufficient_data` or a noop with no numeric
recommendation instead of an exact suggestion when required evidence is missing, stale, contradictory, or
outside the supported path. This includes incomplete intake, missing timing,
unconfirmed adult population status, declared contraindications, sleep-cutoff
conflicts, and pure powder or highly concentrated caffeine products.

Short sleep can suppress a proposal but never increases it. The function does
not infer that caffeine caused a sleep outcome. A `0 mg` noop and an
`insufficient_data` result are normal successful outcomes.

The function is pure: it does not query providers, mutate Calendar, create an
approval object, or depend on current time. Provider adapters and user-facing
delivery belong to later integration issues.

Numeric suppression applies to the recommendation fields. The separate facts
object retains supplied evidence and bounds so callers can explain why a
proposal was suppressed; delivery adapters must apply their own privacy rules
before rendering or logging those facts.

## Evidence behind the guardrails

The product treats these references as population-level constraints, not
individual recommendations:

- [FDA overview for caffeine and most adults](https://www.fda.gov/consumers/consumer-updates/spilling-beans-how-much-caffeine-too-much)
- [EFSA caffeine assessment](https://www.efsa.europa.eu/en/topics/topic/caffeine)
- [ACOG guidance for pregnancy](https://www.acog.org/clinical/clinical-guidance/committee-opinion/articles/2010/08/moderate-caffeine-consumption-during-pregnancy)
- [FDA warning on pure and highly concentrated caffeine](https://www.fda.gov/food/information-select-dietary-supplement-ingredients-and-other-substances/fda-warns-consumers-about-pure-and-highly-concentrated-caffeine)
- [Drake et al. on caffeine timing and sleep](https://pmc.ncbi.nlm.nih.gov/articles/PMC3805807/)

See [issue #82](https://github.com/NomaDamas/healthmes-agent/issues/82) for
the acceptance criteria and research discussion that established this contract.
