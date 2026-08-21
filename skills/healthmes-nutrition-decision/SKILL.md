---
name: healthmes-nutrition-decision
description: Read retained nutrition and caffeine context for one wellness decision without capturing, confirming, or mutating intake records.
version: 1.0.0
---

# HealthMes Nutrition Decision

Use this reviewed procedure only inside a read-only HealthMes wellness
decision turn. Food capture, owner confirmation, correction, and intake
mutation belong to a separate command workflow.

## Read-only tools

- Use `mcp__healthmes__search_nutrition` with
  `nutrition.intake-history` for confirmed retained intake history.
- Use `mcp__healthmes__search_nutrition` with
  `nutrition.caffeine-ledger` for the local-day caffeine ledger and its
  completeness state.
- Use `mcp__healthmes__search_nutrition` with
  `nutrition.decision-context` only when the DecisionRequest already names
  the selected candidate request ID.

Do not call capture, review, confirmation, outcome, or generic decision-record
mutation tools from this runtime. If the user asks to change nutrition data,
return a concise limitation so the caller can start the separate command
workflow.

## Interpretation boundary

- Observation is not consumption.
- Model-estimated nutrients are not owner-confirmed facts.
- Unknown, partial, or unquantified intake is not zero.
- A daily caffeine total is known only when the returned ledger explicitly
  reports both `status: known` and `total_intake_complete: true`.
- Preserve exact values, ranges, units, confidence, provenance, local-day
  boundaries, and source reference IDs returned by HealthMes.
- For a candidate food or drink, use the structured candidate result. Do not
  request photo bytes or a voice transcript in the final decision turn.

## Cross-domain use

Start with Nutrition when the question concerns a meal, nutrient, caffeine,
or a candidate food. Add Activity, Wearable, Calendar, or current-time context
only when it can materially change the answer. For example, a caffeine choice
may require confirmed intake, candidate caffeine, local time, intended sleep
time, recent sleep or readiness, and current workload. Missing required facts
must produce a limitation or clarification rather than a guessed total.
