---
name: doctor-visit-summary
description: "Assemble a local doctor-visit briefing (medication list, symptom timeline with photo paths, health-score context) as one markdown file under the HealthMes data dir — nothing uploaded."
version: 1.0.0
author: HealthMes Agent
license: MIT
metadata:
  hermes:
    tags: [Health, Medical, Briefing, Summary, Local-first, Privacy]
    related_skills: [healthmes-capture]
---

# Doctor Visit Summary

Before an appointment, assemble everything the doctor will ask about —
current medications, a symptom timeline with the photos to show, and the
objective health context around those symptoms — into ONE markdown file on
local disk. The user opens or shows the file themselves; you only tell them
where it is.

This is a filing aid, not a medical opinion. You compile records; you never
diagnose, never rank likely causes, never suggest treatments.

## Privacy rules (non-negotiable)

- **Nothing is uploaded, ever.** The briefing is a local file under the
  HealthMes data directory. Never attach it to a message, never paste its
  full content into the chat, never send it (or any photo it references) to
  any channel, API, or person — not even the user's own Telegram. If the
  user asks you to send it somewhere, decline and point them at the local
  file path; sharing is a manual act they perform themselves.
- **Photos are referenced by local path only.** Link the stored image files
  where they live on disk. Never re-upload or re-describe a photo — use the
  structured description captured at log time.
- **Only description text re-enters the model.** `list_medical_records`
  already enforces this (it returns descriptions, never transcripts). Do
  not try to recover transcripts or raw media content by other means.
- **Confirm without content.** Your chat reply after generating the file
  contains the file path and section counts — no medication names, no
  symptom details.

## Data access rules

- ALL data comes from MCP tools on the `healthmes` server (registered as
  `mcp__healthmes__<tool>` with double underscores —
  vendor/hermes-agent/tools/mcp_tool.py). NEVER call the HealthMes or
  open-wearables REST APIs directly (no `curl`, no HTTP from scripts) —
  bypassing MCP breaks the decision-record chain.
- The briefing file is written with your file tools (`write_file`), under
  the data directory reported by `list_medical_records` (`data_dir` in its
  response). If that path is unusable from this process, fall back to the
  `HEALTHMES_DATA_DIR` environment variable (e.g. from
  `${HERMES_HOME:-~/.hermes}/.env`), then ask the user — never guess a
  location outside the data directory.

## Tool inventory

| Tool (healthmes server) | Use for |
|---|---|
| `list_medical_records` | The records: `kind` = `medication` / `symptom`, trailing `range` (e.g. `90d`, max 365d), `include_context: true` for capture-time health snapshots; returns `data_dir` for path resolution |
| `get_health_scores` | Aggregated score context over the window (sleep, stress, readiness, recovery) with confidence/coverage |
| `get_personal_baselines` | Current deviation vs 14/90-day personal baselines for headline metrics |
| `get_daily_readiness_context` | Today's state (useful for a same-day appointment) |
| `record_decision` | One record (kind: `capture`) noting the briefing was generated — generic labels only |

## When to use

- "I have a doctor's appointment Thursday — can you prep a summary?"
- "Make a list of my medications for the clinic."
- "Summarize my symptoms from the last month for my dermatologist."

## When NOT to use

- Logging a new medication/symptom → `healthmes-capture` skill.
- "What's wrong with me?" / interpretation of symptoms → decline the
  interpretation; offer this briefing instead.
- Emergencies ("severe chest pain right now") → do not build a file; tell
  the user to contact emergency services immediately.

## Procedure

1. **Scope the window.** Ask (or infer from the request) which period the
   visit concerns. Default to the trailing 90 days. If the user names a
   specialty or complaint, note it for section emphasis — never for
   filtering out records without asking.
2. **Pull medications.** `list_medical_records` with `kind: "medication"`,
   the chosen `range`, `include_context: false`.
   - Deduplicate repeated captures of the same medication (same
     name/strength): one line each, with first-seen and last-seen dates and
     capture count.
   - Preserve `[illegible]` markers and uncertainty from the descriptions —
     never "clean up" a drug name into a guess.
   - Skip records whose description is `[deleted by user]`.
3. **Pull the symptom timeline.** `list_medical_records` with
   `kind: "symptom"`, same `range`, `include_context: true`. Records arrive
   oldest first — keep that order. For each entry:
   - date (from `recorded_at`), the structured description as captured;
   - the photo, if any, as a markdown image/link using its local path —
     resolve a relative `media_path` against the returned `data_dir`;
   - one compact line of objective state at capture time from the record's
     stored health snapshot (`context.health`), e.g. sleep debt, stress
     level, HRV vs baseline — only blocks whose `status` is ok; if the
     snapshot is `unavailable`/`insufficient_data`, write "no health data
     captured" rather than inventing.
4. **Add the health-score context.** For the same window:
   - `get_health_scores` (default categories) — summarize per category:
     latest, mean, min–max, and trend direction, with its confidence.
   - `get_personal_baselines` — current deviation vs the 14-day baseline
     for headline metrics (sleep duration/score, HRV, resting HR).
   - Report only what the tools return; carry `confidence`/`coverage`
     into the text ("low confidence: 4 of 30 days"). Omit categories that
     return `insufficient_data` — list them under "Not enough data".
5. **Render ONE markdown file.** Use the template below. Write it with
   your file tools to `{data_dir}/exports/doctor-visit-YYYY-MM-DD.md`
   (today's date; create the `exports/` directory if needed; add `-2`,
   `-3`… if the file exists).
6. **Record and confirm.** `record_decision` (kind: `capture`, generic
   labels — e.g. inputs "medication records: 4, symptom records: 7,
   window 90d", action "wrote doctor-visit briefing file"; NO medical
   content). Then reply in chat with only: the file path, the window, and
   the section counts.

## Briefing template

```markdown
# Doctor visit briefing — {YYYY-MM-DD}

Window: {start_date} → {end_date} ({days} days). Compiled locally by
HealthMes from self-logged captures and wearable summaries. Self-reported;
not a medical record from a provider.

## Current medications ({n})
| Medication (as captured) | First seen | Last seen | Captures | Notes |
|---|---|---|---|---|
| ... | ... | ... | ... | dose/frequency as stated, [illegible] kept |

## Symptom timeline ({n} entries)
### {YYYY-MM-DD} — {short label from the description}
- As captured: {structured description, verbatim}
- Photo: ![symptom photo]({absolute local path})   <!-- omit line if none -->
- State that day: {e.g. "stress high (78), sleep debt 3.2h, HRV −12% vs
  baseline" or "no health data captured"}

## Health context ({window})
- Sleep: ...    - Stress: ...    - Readiness/Recovery: ...
- Deviations vs personal baseline: ...
- Not enough data: {categories honestly omitted}

## Questions to ask   <!-- only if the user dictated any; never invent -->
- ...
```

Write the briefing in the user's language; keep the structure. Everything
in the file must trace to a tool result or the user's own words — if a
section is empty, say "none captured" instead of filling it.
