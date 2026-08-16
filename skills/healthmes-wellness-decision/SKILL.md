---
name: healthmes-wellness-decision
description: Combine only the Activity, Nutrition, Wearable, Calendar, and reviewed domain guidance needed for one natural-language wellness question.
version: 1.0.0
---

# HealthMes Wellness Decision

Use this skill for free-form questions that may cross more than one HealthMes
wellness domain. It explains how to use the product-owned decision contract;
it does not replace HealthMes access policy, retention, calculations, source
validation, or persistence code.

## One reasoning path

- The current HealthMes DecisionRequest is the only reasoning ingress.
- Use only tools exposed by the filtered `healthmes` MCP server.
- Do not call Open Wearables directly, issue raw REST/SQL/filesystem requests,
  use native web/search tools, or start a second agent conversation.
- Do not call capture, calendar mutation, settings mutation, task mutation,
  medical-record mutation, or generic `record_decision` tools from this
  read-only decision turn.

## Select evidence autonomously

1. Interpret the user's actual question. Do not map it to a fixed
   `question_kind` table.
2. Start with the smallest relevant domain search:
   - `mcp__healthmes__search_activity`
   - `mcp__healthmes__search_nutrition`
   - `mcp__healthmes__search_calendar`
   - `mcp__healthmes__search_wearable`
3. Inspect status, freshness, coverage, limitations, and source references.
   Add another domain only when it can resolve a material uncertainty.
4. Use `mcp__healthmes__list_wellness_skills` and
   `mcp__healthmes__read_wellness_skill` when a reviewed domain procedure is
   relevant. Never treat skill prose as data or as permission to bypass a
   HealthMes tool boundary.
5. Ask one concrete clarification when an essential candidate product,
   serving, time, intended action, or user fact is missing.

For a question such as "Should I drink this coffee to keep focusing?", first
identify the structured candidate caffeine estimate and today's confirmed
nutrition ledger. Add current time, sleep/readiness, recent activity strain,
or Calendar load only when they affect the answer. A photo may be sent to the
intake VLM before this decision turn; use its structured candidate result and
source reference here rather than copying image bytes into the final decision.

## Source and uncertainty rules

- Use only source reference IDs returned by tools in this turn.
- Missing, partial, stale, unavailable, or unknown data is not zero.
- Provider calculations, units, local-day boundaries, and specialist hard
  limits are authoritative.
- Distinguish observation, interpretation, uncertainty, and proposed action.
- Do not diagnose, prescribe, or claim that a consumer wearable proves a
  medical condition.

## Persistence classification

Return exactly one persistence intent:

- `none`: simple lookup, explanation, or summary.
- `action`: a concrete behavior recommendation the user may follow.
- `risk`: an actionable safety warning that should remain inspectable.
- `mutation`: reserved for a separate confirmed command workflow. This
  read-only decision runtime cannot establish it and must normally use
  `none` instead.
- `explicit_tracking`: advisory only when the user asked HealthMes to retain
  this result; HealthMes independently checks the trusted request flag.

Consulting source data alone is never a reason to persist. The model cannot
force storage by choosing an intent. HealthMes derives the effective intent
from a completed actionable result or the trusted `persistence_requested`
request flag, validates source references, and writes only a compact
DecisionRecord when required.

## Final contract

Return only the strict `healthmes.decision-draft.v1` JSON envelope requested by
the runtime. Keep the answer concise, include only actually used source
reference IDs, state material limitations, and never add prose or a code fence
outside the JSON object.

`record_summary` is an optional compatibility field, not a persistence
authority. If supplied, keep it under 160 characters and omit raw identifiers,
transcripts, media content, and detailed tool payloads. HealthMes ignores this
free text for durable storage and derives a fixed category-only summary from
the verified persistence intent.
