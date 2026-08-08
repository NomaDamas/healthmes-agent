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
- `candidate_caffeine` is one prospective meal, food, or drink. Every item in
  that candidate must contain exactly one caffeine fact with an exact `mg`
  amount and `user` or `label` origin. A caffeine-free item therefore carries
  explicit `0 mg`; a missing item value makes the whole candidate incomplete.
  The interaction-level owner review is required even when the caller supplied
  a direct `user` or `label` value; direct values merely skip model
  reanalysis. VLM/agent estimates, ranges, unknowns, missing or rejected
  reviews, and already-consumed interactions fail closed.
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

candidate_total_after_intake_mg =
  verified_total_consumed_today_mg + confirmed_candidate_mg
```

An exact `suggested_additional_mg` is the smaller of that maximum and the
current event-bound baseline. Without that baseline, the result may expose only
the upper bound.

For a confirmed prospective candidate, the function does not invent a smaller
serving:

```text
candidate_mg <= maximum_additional_mg
  -> proposal
  -> suggested_additional_mg = candidate_mg
  -> basis = confirmed_candidate

candidate_mg > maximum_additional_mg
  -> noop
  -> reason = candidate_exceeds_bounded_limit
```

The candidate route does not require a Calendar event. The caller supplies an
`intake_decision_request_id`; the adapter reloads that immutable context and
uses its intended consumption time. If a Calendar event is also supplied, its
local day must agree with the candidate time.

The request snapshot also pins the candidate's IANA timezone. Sleep retrieval,
intended time parsing, and the confirmed daily ledger use that same timezone.
If the runtime timezone changes after request creation, the old request is
rejected and the caller must create a new one.

The intended consumption time must still be actionable. Request creation and
proposal execution reject a timestamp more than five minutes in the past. The
adapter checks once before wearable I/O and again inside the final storage
transaction, so a request that expires while the tool is running cannot return
an actionable proposal.

The request also pins the candidate review revision. Any later confirmation,
correction, or rejection invalidates the old request even though its audit
snapshot remains immutable. Proposal execution checks this review state and
terminal outcome state both before external wearable I/O and immediately
before returning. The final check acquires the unified nutrition-ledger lock,
then locks the primary and every comparison interaction in canonical UUID
order before comparing the server-owned daily ledger. Review and outcome
writers use the same ledger-first ordering, so a concurrent candidate change
cannot split those validations.

Today's intake remains server-owned. Callers cannot pass a replacement total.
The adapter reads the unified caffeine ledger, which becomes known only after
each consumed outcome or legacy photo value is quantified and the whole local
day is explicitly confirmed. A prospective photo linked to an intake
interaction is excluded from that consumed ledger until a consumed outcome is
stored.

## Fail-closed boundaries

The function returns `insufficient_data` or a noop with no numeric
recommendation instead of an exact suggestion when required evidence is missing, stale, contradictory, or
outside the supported path. This includes incomplete intake, missing timing,
unconfirmed adult population status, declared contraindications, sleep-cutoff
conflicts, unconfirmed candidate nutrition, and pure powder or highly
concentrated caffeine products.

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
