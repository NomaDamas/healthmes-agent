---
name: healthmes-capture
description: "Route Telegram photos, text, and voice into review-first nutrition interactions or local medical-lite medication/symptom records."
version: 2.0.0
author: HealthMes Agent
license: MIT
metadata:
  hermes:
    tags: [Health, Food, Medical, Capture, Logging, Telegram, Vision]
    related_skills: [doctor-visit-summary]
---

# HealthMes Capture

The Telegram bot IS the capture app: there is no separate UI. When the user
sends a photo or a voice note (from phone; the watch contributes voice quick
replies), you classify it and use the HealthMes MCP tools. Nutrition capture
must keep analysis separate from actual consumption. Medical capture keeps
its existing local record flow.

## Data access rules (non-negotiable)

- Persist ONLY via MCP tools on the `healthmes` server (registered as
  `mcp__healthmes__<tool>` with double underscores, e.g.
  `mcp__healthmes__analyze_intake_capture`).
  Never call REST APIs directly — bypassing MCP breaks the decision-record
  chain.
- Store the media by its LOCAL file path. Inbound Telegram media is already
  saved to disk by the Hermes gateway and referenced in the message (e.g. a
  `MEDIA:<path>` tag or attachment path) — pass that path through; never
  re-upload, never inline image bytes into the database.
- Raw media never leaves the machine except for the model call that
  describes it. Summarize, then persist.

## Tool inventory

| Tool (healthmes server) | Use for |
|---|---|
| `get_recent_nutrition_observations` | Read structured photo observations and their current review state |
| `review_photo_nutrition_observation` | Store the owner's explicit confirm/correct/reject review of a photo |
| `analyze_intake_capture` | Analyze owner text or a local voice token into an unconfirmed interaction |
| `capture_intake_interaction` | Create an interaction from an owner-reviewed photo observation |
| `confirm_intake_outcome` | Store consumed/not-consumed/cancelled only from the owner's exact reply |
| `create_medical_record` | Persist a medication/symptom capture (Step 3); the tool attaches the health-context snapshot itself; pass `record_id` for one-tap corrections |
| `record_decision` | Record non-obvious capture decisions (kind: `capture`) |

## When to use

- The user sends a photo of food, a meal description, or a voice note about
  something they ate or drank.
- The user sends a photo of medication (pill packs, prescriptions,
  supplement bottles) or of a symptom (rash, swelling, injury), or a voice
  note describing a symptom or a medication they took.
- The user corrects a just-logged entry ("that was lunch, not a snack",
  "that's a supplement, not a prescription").
- A photo/voice note arrives with no text: classify it first (step 1).

## When NOT to use

- Scheduling, goals, alerts → `healthmes-planner` skill.
- General health questions → answer directly with MCP tools.
- Screenshots of calendars/apps → not a capture; treat as conversation
  context.

## Step 1 — classify the capture

Look at the media (and any caption/transcript) and pick ONE branch:

1. **Food or drink** → Food path (Step 2). This includes plated meals,
   packaged snacks, drinks, menus photographed at order time, and voice
   notes like "just had two slices of pizza".
2. **Medication or symptom** (pill packs, prescriptions, supplement
   bottles, rashes, injuries, "my head has been pounding since lunch")
   → Medical path (Step 3). Never write medical content into a nutrition
   interaction.
3. **Neither** → say briefly what you saw and ask what they'd like done.
   Do not log anything.

If genuinely ambiguous between food and medical (e.g. supplements), ask one
short question rather than guessing.

## Step 2 — Food path

1. **Preserve intent.** Use `log_consumed` only when the owner is trying to
   record something they ate or drank. Use `ask_before_intake` for "can I
   have this?", and `inspect_only` for analysis without a decision. A photo
   alone never proves intent or consumption.
2. **Create the observation/interaction.**
   - Photo: use only the exact `NutritionObservation` ID supplied by the
     trusted media-ingestion adapter. Read it with
     `get_recent_nutrition_observations`. Do not guess that a nearby
     observation belongs to this photo.
   - Text: call `analyze_intake_capture(modality="text")` with the owner's
     exact words.
   - Voice: pass the local audio token to
     `analyze_intake_capture(modality="voice")`; HealthMes performs local
     transcription before nutrition analysis.
   - If a Telegram photo has only a filesystem path and no trusted
     observation ID, stop before writing. Explain that the nutrition photo
     ingestion adapter has not produced an observation yet and ask the
     owner to retry from a supported capture surface or provide a text
     description. Never fall back to a legacy compatibility writer.
3. **Show the structured result.** Preserve item names, serving
   exact/range/unknown values, nutrient confidence, warnings, and
   provenance. Ask one short question that makes both review and intended
   outcome explicit:

   ```
   분석: 비빔밥 1그릇(추정) + 달걀 1개.
   이 내용대로 "먹음"으로 기록할까요? [먹음] [수정] [먹지 않음] [취소]
   ```

4. **Review a photo before capture.** From the owner's exact reply:
   - unchanged result → `review_photo_nutrition_observation(status="confirmed")`
   - corrected result → `status="corrected"` with a complete replacement
     item list
   - wrong photo/result → `status="rejected"` and stop
   Then call `capture_intake_interaction` with that exact observation ID.
   Rejected observations must never become interactions.
5. **Store the outcome separately.** Only an exact live reply authorizes
   `confirm_intake_outcome`:
   - "먹음" → `status="consumed"` and the user-stated consumption time
   - "먹지 않음" → `status="not_consumed"`
   - "취소" → `status="cancelled"`
   Analysis, `log_consumed` intent, a photo review, or silence must never
   create a consumed outcome. Generate a fresh `operation_id` for every
   logical review, interaction, and outcome; reuse an ID only for an exact
   retry.
6. **Confirm in one line.** Include the interaction status and offer a
   one-tap correction. A later correction creates a new explicit outcome
   with corrected structured items; never overwrite model provenance or ask
   the owner to re-send the media.

Keep the whole exchange to two messages in the normal case (capture →
review question → confirmation). No lectures and no unsolicited nutrition
advice.

## Step 3 — Medical path (medication / symptom)

### Privacy rule (non-negotiable, stricter than food)

Medical data NEVER leaves this machine, with exactly one exception: the
capture being described (the photo, the voice note, the user's words) is
sent to the LLM once to produce the structured description text. After that,
only that description text may ever re-enter the model context (e.g. when
`doctor-visit-summary` assembles a briefing). Concretely:

- The media file and the voice transcript stay on local disk / in the local
  database. Never re-upload, re-describe, or quote a transcript later.
- Never include medical content (drug names, symptoms) in proactive
  messages, cron briefings, webhook replies, or `record_decision` trees —
  refer to "your medical log" generically unless the user is the one asking
  about it in this conversation.
- Never route medical content to any non-medical skill, external API, or
  file outside the HealthMes data directory. (`doctor-visit-summary` is the
  one legitimate downstream consumer, and it only ever sees descriptions.)

### Procedure

1. **Pick the kind.** `medication` = pill packs, prescriptions, medicine
   boxes, supplement bottles, "took 400mg ibuprofen". `symptom` = rashes,
   swelling, injuries, pain/nausea/dizziness descriptions ("my head has
   been pounding since lunch"). If one capture contains both ("took X for
   this rash"), log two records. If unsure, ask one short question.
2. **Describe — transcribe, never diagnose.** Build a structured
   description from the photo (vision) or voice note (transcript):
   - Medication: name EXACTLY as printed, strength/dose if legible,
     quantity and stated frequency/timing ("2 tablets, after lunch").
     Never guess or autocomplete a drug name — copy only what is legible
     and mark unreadable parts as `[illegible]`.
   - Symptom: what and where, appearance (size/color if visible), severity
     and onset time as STATED by the user, and any stated trigger. Use the
     user's own words for sensations.
   - No diagnosis, no cause speculation, no treatment advice — capture is a
     filing operation. Mark uncertainty explicitly instead of inventing.
3. **Persist.** Call `create_medical_record` with `kind`, the structured
   `description`, `media_path` (the local path from the inbound message,
   passed through), `transcript` (voice captures), and `context` with
   capture metadata only (e.g. `{"source": "telegram-photo",
   "captured_at": "<message time ISO>", "user_stated_time": "since
   lunch"}`). Do NOT fetch or pass health data yourself: the tool
   deterministically snapshots today's readiness context server-side and
   stores it with the record.
4. **Confirm with one-tap correction.** Same contract as food — ONE short
   confirmation, correctable in one tap:

   ```
   Saved to your medical log: medication — "Tylenol 500mg, 2 tablets" (photo kept locally).
   Reply 1 to fix the description, 2 to switch medication/symptom, 3 to delete.
   ```

   Apply a correction immediately by calling `create_medical_record` again
   with `record_id` set to the id you just received plus the corrected
   `kind`/`description` — the original media, transcript, and capture-time
   health snapshot are preserved automatically. Never make the user re-send
   the photo. (For "3 delete": there is no delete tool; overwrite via
   `record_id` with the description `[deleted by user]` and confirm.)
5. **Stop there.** No lectures, no interpretation of the medication or
   symptom, no "you should see a doctor" unless the user asks. If they want
   a briefing for an appointment, that is the `doctor-visit-summary` skill.

## Decision records

Routine successful captures do not need a `record_decision`. Record one
(kind: `capture`) when you made a judgment worth auditing: ambiguous
classification, a rejected capture, or a correction that changed meaning.
For medical captures, keep decision-tree labels generic ("medical capture:
classification ambiguous, user confirmed medication") — never put drug
names, symptoms, or description text into a decision record.
