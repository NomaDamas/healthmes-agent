---
name: healthmes-capture
description: "Turn channel-neutral photo, voice, or text captures into structured local food and medical-lite records."
version: 1.1.0
author: HealthMes Agent
license: MIT
metadata:
  hermes:
    tags: [Health, Food, Medical, Capture, Logging, Channel, Vision]
    related_skills: [doctor-visit-summary]
---

# HealthMes Capture

This is a bounded capture workflow, not a free-form wellness reasoning
ingress. A future app or channel wrapper may supply a photo, local media path,
voice transcript, or text capture. Classify it, produce a structured
description, and persist only the domain record through HealthMes tools.
The wrapper owns its UI; this Skill does not install Telegram, mobile, or web
inbound behavior.

## Data access rules (non-negotiable)

- Persist ONLY via MCP tools on the `healthmes` server (registered as
  `mcp__healthmes__<tool>` with double underscores, e.g.
  `mcp__healthmes__log_food` — vendor/hermes-agent/tools/mcp_tool.py).
  Never call REST APIs directly — bypassing MCP breaks the product data,
  retention, and provenance boundary.
- Store media only by the LOCAL file path supplied by the trusted capture
  wrapper (for example a `MEDIA:<path>` tag or attachment path). Pass that
  path through; never re-upload or inline image bytes into the database.
- Raw media never leaves the machine except for the model call that
  describes it. Summarize, then persist.

## Tool inventory

| Tool (healthmes server) | Use for |
|---|---|
| `log_food` | Persist a food log: structured description, media path, timestamp, meal type |
| `create_medical_record` | Persist a medication/symptom capture (Step 3); the tool attaches the health-context snapshot itself; pass `record_id` for one-tap corrections |
| `get_daily_readiness_context` | Health-context snapshot to attach at food-log time |

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
   → Medical path (Step 3). Never write medical content into `log_food`.
3. **Neither** → say briefly what you saw and ask what they'd like done.
   Do not log anything.

If genuinely ambiguous between food and medical (e.g. supplements), ask one
short question rather than guessing.

## Step 2 — Food path

1. **Describe.** From the photo (vision) or voice note (transcript), build a
   structured description:
   - items with rough portions ("bibimbap, 1 bowl; fried egg, 1; kimchi,
     small side")
   - preparation if visible (fried/grilled/raw)
   - drink if present
   - qualitative flags only when clear (very salty / dessert / alcohol)
   Do NOT invent calorie counts or exact grams. If the image is unclear,
   describe what is visible and mark uncertainty in the description.
2. **Determine timestamp and meal type.** Default `logged_at` to the message
   time (the user usually captures while eating). If the user says
   otherwise ("this was breakfast"), honor it. Infer `meal_type`
   (breakfast/lunch/dinner/snack) from local time of `logged_at` unless
   stated.
3. **Snapshot health context.** Call `get_daily_readiness_context` for the
   log date and pass its compact result as the health-context snapshot
   argument of `log_food`. If the tool reports `insufficient_data`, pass
   that through honestly — never fabricate a context.
4. **Persist.** Call `log_food` with: the structured description, the media
   path (photo) and/or transcript (voice), `logged_at` (ISO 8601, user's
   local timezone), `meal_type`, `source` (`photo` / `voice` / `text`), and
   the health-context snapshot.
5. **Confirm with one-tap correction.** Reply with ONE short message:

   ```
   Logged: bibimbap (1 bowl) + fried egg — lunch, 12:40.
   Reply 1 to fix the description, 2 to change meal type/time, 3 to delete.
   ```

   A channel wrapper may render native actions; the numbered plain-text
   fallback must always be present. Apply a correction reply immediately by
   updating the same food-log entry (call `log_food` in its correction/update
   form for the entry you just created — keep the original media path), then
   confirm in one line. Never make the user re-send the photo.

Keep the whole exchange to two messages in the normal case (capture →
confirmation). No lectures, no unsolicited nutrition advice; if the day's
context makes one observation genuinely useful ("3rd coffee after a
low-sleep night"), keep it to one clause inside the confirmation.

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
  messages, scheduled briefings, or compact wellness decision records —
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
   capture metadata only (e.g. `{"source": "channel-photo",
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

## Storage boundary

Routine capture and correction write only their domain record. They never
create a DecisionRecord, and this command profile has no generic
decision-record writer. If the user asks a separate wellness question about
the capture, send that question through the canonical HealthMes decision
ingress; its finalizer alone decides whether a compact record is warranted.
