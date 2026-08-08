---
name: healthmes-nutrition
description: Automatically analyze photo, free-text, or local-voice food context; keep observations separate from consumption; and build evidence-bound wellness decisions.
version: 2.3.0
author: HealthMes Agent
license: MIT
metadata:
  hermes:
    tags: [Health, Nutrition, Food, Caffeine, Vision, Voice, Decision]
    related_skills: [healthmes-capture]
---

# HealthMes Nutrition Interaction

Use this skill for food or drink capture and food-related wellness questions.
The engine has no UI. It stores device-neutral interactions that later mobile,
web, or agent surfaces can render.

## Evidence boundary

- A photo, text entry, or voice transcript is an observation, not consumption.
- `log_consumed` is the owner's intent, not proof that the outcome was stored.
- Only `confirm_intake_outcome(status="consumed")` creates known intake.
- Nutrient facts retain `origin`, `confidence`, exact/range/unknown, and units.
- Photo analysis extracts serving plus core nutrition: energy, protein,
  carbohydrate, fat, fiber, sugar, sodium, and caffeine. Additional nutrients
  use the same generic contract.
- A photo nutrition review can confirm, fully correct, or reject the VLM
  observation without overwriting its original provenance.
- After any photo, text, or voice interaction is created,
  `review_intake_interaction` can confirm, fully correct, or reject its
  structured nutrients. The review is a new event; it never overwrites the
  original interaction.
- Free text can be automatically structured by the configured nutrition
  provider. Voice is transcribed only by the configured loopback whisper.cpp
  server and then follows the same text-analysis path.
- Captured history is not proof that every meal in a day was recorded.
- Photo analysis is idempotent by media identity and request fingerprint.
  Every nutrition mutation requires a caller-generated UUID `operation_id`,
  including photo caffeine confirmation, full photo review, interaction
  capture/review/outcome/decision, and daily coverage confirmation. It is an
  idempotency key within that write kind, not one global nutrition ID.
  Generate a fresh UUID for every logical mutation; reuse it only for an exact
  retry of the same kind and input.
- Technical non-content operation markers outlive retained result events and
  contain only operation identity, request fingerprint, and completion state.
  Separate wellness review/outcome transition revisions preserve ordering and
  terminal outcome state, so expiry never makes an old candidate reviewable or
  actionable again.
- Raw text, transcripts, and media paths are short-lived capture data.
  Confirmed intake and decision requests retain sanitized structured snapshots
  without free-form notes or evidence excerpts.

## Interaction tools

| Tool | Purpose |
|---|---|
| `mcp__healthmes__get_recent_nutrition_observations` | Read photo nutrition estimates, provenance, and latest owner review |
| `mcp__healthmes__review_photo_nutrition_observation` | Idempotently confirm, fully correct, or reject one photo nutrition observation with `operation_id` |
| `mcp__healthmes__analyze_intake_capture` | Automatically structure owner free text or a local voice capture with an explicit intent |
| `mcp__healthmes__capture_intake_interaction` | Store a photo observation or caller-supplied reviewed structured nutrition |
| `mcp__healthmes__review_intake_interaction` | Confirm, fully correct, or reject one interaction's structured nutrients before consumption or a decision |
| `mcp__healthmes__confirm_intake_outcome` | Store consumed, not-consumed, or cancelled from the owner's exact reply |
| `mcp__healthmes__search_intake_records` | Search reusable records by time, intent, modality, nutrient, confirmation, or text |
| `mcp__healthmes__request_intake_decision` | Persist a decision request and receive its candidate/history/evidence context |
| `mcp__healthmes__get_intake_decision_context` | Reload one stored decision context |
| `mcp__healthmes__record_intake_decision` | Persist the result with exact evidence event IDs and limitations |

## Sake caffeine tools

| Tool | Purpose |
|---|---|
| `mcp__healthmes__get_caffeine_observations` | Read photo-derived caffeine estimates and provenance |
| `mcp__healthmes__confirm_photo_caffeine_observation` | Idempotently confirm, correct, or reject each photo caffeine value with `operation_id` |
| `mcp__healthmes__confirm_photo_caffeine_day` | Idempotently bind all displayed photo IDs and latest text/voice/photo outcome IDs for the local day with `operation_id` |
| `mcp__healthmes__get_known_caffeine_intake_for_day` | Return a unified caffeine ledger only after nutrient values and whole-day coverage are confirmed |
| `mcp__healthmes__get_caffeine_proposal` | Assess one confirmed prospective caffeine candidate or the legacy exact-event preparation path |

## Required procedure

1. Preserve the owner's intent. Use `log_consumed`, `ask_before_intake`,
   `inspect_only`, `plan_future`, or `compare_option`; never infer a different
   intent from the media alone.
2. For a photo, read the observation and preserve exact/range/unknown.
   `review_photo_nutrition_observation` is optional pre-capture review of the
   stored photo observation. It is not a substitute for the modality-neutral
   interaction review used after capture.
3. Capture the interaction. For photo, pass the existing
   `nutrition_observation_id` to `capture_intake_interaction`. For free text,
   call `analyze_intake_capture` with the owner's exact text. For voice, pass
   only the local audio token to `analyze_intake_capture`; HealthMes creates
   the local transcript and structured nutrients.
4. If any nutrient used by consumption or a decision has `origin=vlm|agent`,
   ask the owner to confirm, fully correct, or reject it. Call
   `review_intake_interaction` with a fresh `operation_id`. `confirmed` sends
   no items; `corrected` sends the complete replacement; `rejected` sends no
   items. Create a new decision request after every review because prior
   request snapshots remain immutable.
   Once any outcome exists, do not review that interaction again. Correct a
   consumed record through a new outcome, or create a new interaction to
   revisit a not-consumed/cancelled candidate.
5. If the owner is logging consumption, ask for exact confirmation and call
   `confirm_intake_outcome`. Do not silently turn the capture into intake.
6. If the owner asks before eating, call `request_intake_decision`. Use only
   the returned candidate, confirmed history, evidence IDs, and explicit
   limitations. A prospective photo is not part of today's consumed ledger.
7. For a caffeine candidate, pass that request ID to
   `get_caffeine_proposal` as `intake_decision_request_id`. `event_id` may be
   null. The request must contain an intended time whose offset matches the
   interaction IANA timezone. The candidate must contain exact owner- or
   label-confirmed `mg`. Never override the stored time during proposal.
8. Record the result with `record_intake_decision`. If the owner later eats
   the candidate, separately call `confirm_intake_outcome(status="consumed")`.
9. For caffeine quantity decisions, review model-derived nutrient values before
   confirming consumption. Confirm the day with every returned photo
   observation whose `legacy_daily_confirmation_eligible` is true and every
   latest intake outcome ID; a later correction requires a new day
   confirmation.
   Continue only when `get_known_caffeine_intake_for_day` reports both
   `status: known` and `total_intake_complete: true`.
10. Generate one new `operation_id` for each logical write and include it in
   the trusted proof. Preserve the same ID only when retrying the exact input.

## Question routing

- “What did I consume today?” is a ledger read. Call
  `get_known_caffeine_intake_for_day`; do not send the question to a VLM and
  do not accept a caller-supplied replacement total.
- “I consumed this” is `log_consumed` followed by an explicit consumed
  outcome.
- “Can I consume this?” is a prospective intent followed by interaction
  review, a decision request, and the specialized policy. It is not an
  outcome.
- Direct structured `origin=user|label` exact mg does not require model
  reanalysis, but the prospective decision path still requires an explicit
  interaction owner review. A free-text model extraction remains
  `origin=agent` until that review confirms or corrects it.

## Sake confirmation procedure

1. Read the selected day with both `get_caffeine_observations` and
   `get_known_caffeine_intake_for_day`.
2. Show each legacy-ledger-eligible photo observation separately, preserving
   exact/range/unknown, warnings, provenance, and observation ID. Do not ask
   the owner to confirm a prospective interaction as consumed.
3. Ask the owner to confirm, correct, or reject each caffeine value. Never
   choose a point inside a range on the owner's behalf.
4. Generate a fresh caller-owned `operation_id`, then call
   `confirm_photo_caffeine_observation` only from the owner's exact live reply
   and its gateway-issued trusted proof. Reuse that ID only for an exact retry
   of the same confirmation.
5. For text, voice, or photo outcomes with model-derived caffeine, use the
   owner's exact correction as `corrected_items` when confirming the outcome;
   consumption confirmation alone does not confirm a model nutrient estimate.
6. Reload `get_known_caffeine_intake_for_day` after all reviews and outcome
   changes. Collect every `outcome_id` from its evidence along with every
   `legacy_daily_confirmation_eligible` photo observation ID shown.
7. Ask whether those displayed records represent all caffeine consumed on that
   local day.
8. Generate a fresh caller-owned `operation_id`, then call
   `confirm_photo_caffeine_day` with it, the exact observation IDs, exact
   latest outcome IDs, and the owner's completeness reply. Reuse that ID only
   for an exact retry of the same write.

## Safety rules

- Never turn unknown into zero or choose a point inside a range.
- Never describe agent/VLM nutrient estimates as user-confirmed nutrient facts.
- `allergy_safety` and `medication_interaction` must be recorded as
  `unsupported`; do not generate a wellness proposal for those scopes.
- A caffeine decision also requires independent sleep, timing, population,
  contraindication, product-form, and personal-limit evidence.
- The generic `caffeine_sleep` engine may store only `insufficient_data` or
  `unsupported`. A specialized validated policy owns proposals and no-op
  decisions. Generic caffeine results never retain a recommendation or
  caller-authored actionable summary.
- If required context is missing, record `insufficient_data`, not a guess.
