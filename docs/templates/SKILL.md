---
name: your-skill-name
description: One sentence — the clinical question this skill answers and when the agent should reach for it.
---

<!--
Starter template for a HealthMes skill. Copy this file to
skills/<your-skill-name>/SKILL.md (the directory name must match `name:`),
then run: uv run python scripts/bootstrap.py

Rules that reviews enforce (docs/EXTENDING.md §1):
- Call tools by their REGISTERED names: mcp__healthmes__<tool>
  (double underscores). Direct Open Wearables tools are not exposed to the
  product decision runtime.
- Never instruct raw REST calls — decision data access goes through bounded
  HealthMes MCP tools so source references can be validated.
- Do not call a generic record_decision tool. HealthMes validates sources and
  conditionally writes a compact record after the runtime returns.
- Gate advice on confidence: on "low" or "insufficient_data", say the data
  is too thin — never give categorical advice.
- Proactive messages follow the notification grammar (PLAN.md §8.5):
  observation line → evidence line → proposal → one-tap choices → why-link.
-->

# When to use

Describe the situations (user questions, alert types, briefing sections)
where the agent should apply this skill — and when it should NOT.

# Data to gather

1. `mcp__healthmes__search_wearable` with a bounded date range and metrics — …
2. `mcp__healthmes__search_activity` or another domain search only when the
   question needs it; check coverage before drawing a conclusion.

# Judgment procedure

1. If <condition on the interpreted values> AND confidence is "high" →
   recommend <action>, phrased as observation/evidence/proposal.
2. If <other condition> → …
3. If confidence is "low" or any input is insufficient_data → say exactly
   what data is missing and how to get it (e.g. wear the watch overnight).

# After deciding

- Return the strict `healthmes.decision-draft.v1` envelope requested by the
  system instructions.
- If the result may be retained, provide a privacy-minimized
  `record_summary` of at most 160 characters. Do not truncate the full answer
  or include raw identifiers and payload detail.
- Do not mutate calendar, settings, tasks, food records, or medical records
  from the decision-read runtime. A separately confirmed mutation workflow
  owns those actions.
