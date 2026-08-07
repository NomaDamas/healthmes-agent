---
name: healthmes-nutrition
description: Review photo-derived intake observations, resolve uncertainty with the owner, and expose only explicitly confirmed caffeine evidence to later decision skills.
version: 1.0.0
author: HealthMes Agent
license: MIT
metadata:
  hermes:
    tags: [Health, Nutrition, Food, Caffeine, Vision, Confirmation]
    related_skills: [healthmes-capture]
---

# HealthMes Nutrition Evidence

Use this skill after HealthMes has analyzed an uploaded food or drink photo.
The VLM output is an unconfirmed observation, never a fact about total daily
intake and never medical advice.

This version is deliberately caffeine-first. It does not extract calories,
macronutrients, micronutrients, ingredients, or recipes. It also does not
analyze text or voice nutrition entries; those require a future capture
normalization contract before they can enter this confirmation workflow.

## Tools

| Tool | Purpose |
|---|---|
| `mcp__healthmes__get_caffeine_observations` | Read the selected local day's estimates, ranges, warnings, and provenance |
| `mcp__healthmes__confirm_photo_caffeine_observation` | Store the owner's exact confirmation, correction, or rejection |
| `mcp__healthmes__confirm_photo_caffeine_day` | Store whether the displayed records cover the complete day |
| `mcp__healthmes__get_known_caffeine_intake_for_day` | Return a total only after both confirmation layers are present |

## Required procedure

1. Read the selected day with
   `mcp__healthmes__get_caffeine_observations`.
2. Show each record separately. Preserve exact/range/unknown, warnings,
   source, model provenance, and observation ID.
3. Ask the owner to confirm or correct each caffeine amount. Do not choose a
   point inside a range on the owner's behalf.
4. Call `mcp__healthmes__confirm_photo_caffeine_observation` only from the
   owner's exact live reply. The trusted-session proof is injected by the
   gateway; never fabricate or reuse one.
5. Ask whether the displayed observations represent all caffeine consumed
   that local day.
6. Call `mcp__healthmes__confirm_photo_caffeine_day` only from that exact
   reply, including the exact observation IDs shown.
7. Read `mcp__healthmes__get_known_caffeine_intake_for_day`. Continue to a
   caffeine decision only when it returns `status: known` and
   `total_intake_complete: true`.

## Safety rules

- Storage presence is not proof of consumption or daily completeness.
- VLM `confidence: high` is still `unconfirmed`.
- Never turn `unknown` into zero.
- Never silently turn a range into an exact number.
- If records conflict, ask one short correction question and stop.
- Do not call a caffeine proposal tool until all its independent sleep,
  timing, population, contraindication, and personal-limit requirements are
  also satisfied.
