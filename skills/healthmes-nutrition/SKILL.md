---
name: healthmes-nutrition
description: Capture photo, text, or voice-transcript food context; keep observations separate from consumption; and build evidence-bound wellness decisions.
version: 2.1.0
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
- Voice requires a transcript produced locally before capture. This skill does
  not claim that HealthMes transcribed the audio.
- Captured history is not proof that every meal in a day was recorded.
- Every interaction-engine write requires a caller-generated UUID
  `operation_id`. Reuse it only for an exact retry; never reuse it for changed
  input.
- Raw text, transcripts, and media paths are short-lived capture data.
  Confirmed intake and decision requests retain sanitized structured snapshots
  without free-form notes or evidence excerpts.

## Interaction tools

| Tool | Purpose |
|---|---|
| `mcp__healthmes__get_recent_nutrition_observations` | Read photo nutrition estimates, provenance, and latest owner review |
| `mcp__healthmes__review_photo_nutrition_observation` | Confirm, fully correct, or reject one photo nutrition observation |
| `mcp__healthmes__capture_intake_interaction` | Store photo, exact text, or local voice-transcript context with an explicit intent |
| `mcp__healthmes__confirm_intake_outcome` | Store consumed, not-consumed, or cancelled from the owner's exact reply |
| `mcp__healthmes__search_intake_records` | Search reusable records by time, intent, modality, nutrient, confirmation, or text |
| `mcp__healthmes__request_intake_decision` | Persist a decision request and receive its candidate/history/evidence context |
| `mcp__healthmes__get_intake_decision_context` | Reload one stored decision context |
| `mcp__healthmes__record_intake_decision` | Persist the result with exact evidence event IDs and limitations |

## Sake caffeine tools

| Tool | Purpose |
|---|---|
| `mcp__healthmes__get_caffeine_observations` | Read photo-derived caffeine estimates and provenance |
| `mcp__healthmes__confirm_photo_caffeine_observation` | Confirm, correct, or reject each photo caffeine value |
| `mcp__healthmes__confirm_photo_caffeine_day` | Confirm whether the displayed photo records cover the whole local day |
| `mcp__healthmes__get_known_caffeine_intake_for_day` | Return caffeine total only after both caffeine confirmation layers |

## Required procedure

1. Preserve the owner's intent. Use `log_consumed`, `ask_before_intake`,
   `inspect_only`, `plan_future`, or `compare_option`; never infer a different
   intent from the media alone.
2. For a photo, read the observation and preserve exact/range/unknown. If the
   owner corrects it, write a complete replacement with
   `review_photo_nutrition_observation` and a fresh `operation_id` before
   capture.
3. Capture the interaction. For photo, pass the existing
   `nutrition_observation_id`. For voice, pass the local media token and exact
   local transcript. For text, preserve the owner's exact text.
4. If the owner is logging consumption, ask for exact confirmation and call
   `confirm_intake_outcome`. Do not silently turn the capture into intake.
5. If the owner asks before eating, call `request_intake_decision`. Use only
   the returned candidate, confirmed history, evidence IDs, and explicit
   limitations.
6. Record the result with `record_intake_decision`. If the owner later eats
   the candidate, separately call `confirm_intake_outcome(status="consumed")`.
7. For caffeine quantity decisions, keep using the sake confirmation flow.
   Continue only when `get_known_caffeine_intake_for_day` reports both
   `status: known` and `total_intake_complete: true`.
8. Generate one new `operation_id` for each logical write and include it in
   the trusted proof. Preserve the same ID only when retrying the exact input.

## Sake confirmation procedure

1. Read the selected day with `get_caffeine_observations`.
2. Show each photo observation separately, preserving exact/range/unknown,
   warnings, provenance, and observation ID.
3. Ask the owner to confirm, correct, or reject each caffeine value. Never
   choose a point inside a range on the owner's behalf.
4. Call `confirm_photo_caffeine_observation` only from the owner's exact live
   reply and its gateway-issued trusted proof.
5. Ask whether the displayed observations represent all caffeine consumed on
   that local day.
6. Call `confirm_photo_caffeine_day` with the exact observation IDs shown and
   the owner's exact completeness reply.

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
