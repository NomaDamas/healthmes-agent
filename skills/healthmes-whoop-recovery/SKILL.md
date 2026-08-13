---
name: healthmes-whoop-recovery
description: Default entry point for a Korean or English request for today's recovery package, including “오늘 어떻게 회복하지?” even when WHOOP is not named. Use WHOOP Recovery and day strain when available; use general sleep, stress, or work-plan skills only when the user explicitly asks about those topics. Also use when a user asks about WHOOP recovery/day strain or chooses a proposed 10/20/30-minute recovery walk.
---

# WHOOP today recovery package

Give a practical package for today, not a training-clearance decision. This
skill only handles WHOOP Recovery and cumulative WHOOP Cycle day strain.

## Required source and boundary

1. Call `mcp__healthmes__get_whoop_recovery_context` first, using the user's
   requested date when they name one. Use no other source to replace a missing
   primary signal.
2. Treat `recovery` as the morning starting state and `day_strain` as current
   cumulative load. The tool owns raw-score parsing, WHOOP labels, local-date
   checks, freshness, uniqueness, and confidence. Never recreate numeric WHOOP
   thresholds in this skill.
3. `health_scores` category `strain` is workout-specific. Do not use it for
   this decision. The context tool's `day_strain` is the required Cycle signal.
4. You may call `mcp__healthmes__get_daily_readiness_context` once only to add
   last-night sleep or nighttime HRV-direction context after primary data is
   usable. It never changes the level or makes missing primary data usable.

## Decide the recovery level

Only decide a data-based level when the context response is `status: ok` and
both primary signals have `confidence: high`.

| Recovery label | Day-strain label | Level |
| --- | --- | --- |
| green | light or moderate | `basic` |
| green | high or all_out | `enhanced` |
| yellow | light or moderate | `enhanced` |
| yellow | high or all_out | `priority` |
| red | any label | `priority` |

Day strain may raise the level; it never lowers the level implied by Recovery.
Different Recovery and strain directions are normal, not a conflict.

If either primary signal is missing, stale, ambiguous, truncated, or low
confidence, use `insufficient_data`. Say what is unavailable without inventing
a level. Offer the manual basic routine below as an optional, non-data-based
choice.

## Package to present

Every data-based level includes:

- **Water:** Fill one personal water bottle now and finish it before evening.
- **Sleep:** Start getting ready for bed 30 minutes earlier than usual.

Walking is at a comfortable, conversational pace:

| Level | Default and choices |
| --- | --- |
| `basic` | Suggest 10 minutes by default; offer 10, 20, or 30 minutes. |
| `enhanced` | Suggest 20 minutes by default; offer 10, 20, or 30 minutes. |
| `priority` | Offer only a light 10-minute walk or rest. Never offer 20 or 30 minutes. |
| `insufficient_data` | Do not call this a level. Offer the manual basic routine: 10-minute optional easy walk, water, and earlier sleep preparation. |

Do not add food, caffeine/stimulation management, calendar changes, exercise
permission, diagnosis, treatment, or completion tracking.

## Record the recommendation before replying

For every recommendation, including `insufficient_data`, call
`mcp__healthmes__record_decision` with `kind: "insight"` before sending the
response. Use a valid `input -> rule -> option -> action` tree. Persist only:

- source `whoop`, derived Recovery and day-strain labels, freshness bands, and
  confidence;
- chosen level or `insufficient_data`, considered walk choices, and package;
- optional signal *types* (sleep / nighttime HRV direction), never their values.

Never put raw scores, timestamps, identifiers, symptom text, or the tool's
viewer URL inside the record. Include the returned `viewer_url` only in the
requesting user's response as the “왜 이 판단?” link; never log or publish it.

## Selection follow-up

When the user responds to the immediately preceding recommendation with an
allowed choice, for example “20분으로 할게”:

1. Confirm the option was one that was actually offered.
2. Call `mcp__healthmes__record_decision` again with a separate immutable
   `kind: "insight"` record. Its tree should state that the user selected the
   offered walking option and name that option, without health values.
3. Confirm the choice and include the new viewer link only to that user.

If the user says “20분 했어”, acknowledge it warmly but do **not** create a
completion record, mark the walk as completed, or imply that completion was
tracked. Say explicitly that v0 leaves completion unrecorded; for example,
“좋아요. v0에서는 완료 기록은 남기지 않아요. 물병 한 병과 취침 준비를
이어가면 됩니다.” If the option was not offered, especially 20 or 30 minutes
for `priority`, do not record it as a selection; restate the available choices.

## Response shape

Write in the user's language and keep it compact:

1. **Observation:** today's Recovery label and current day-strain label.
2. **Evidence:** date/freshness/confidence; add sleep or HRV only as background.
3. **Today’s package:** walking, water, sleep.
4. **Choices:** permitted walking choices only.
5. **Why:** the private decision viewer link.

For `insufficient_data`, lead with the missing or unreliable signal, then make
the manual routine explicitly optional and non-data-based.

## Medical safety

This is everyday self-management, not medical advice or a judgment that an
activity is safe. Do not diagnose overtraining, illness, cardiovascular
conditions, or sleep disorders. For concerning symptoms or medical decisions,
recommend appropriate professional or urgent local care.
