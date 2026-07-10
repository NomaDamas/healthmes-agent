---
name: your-skill-name
description: One sentence — the clinical question this skill answers and when the agent should reach for it.
---

<!--
Starter template for a HealthMes skill. Copy this file to
skills/<your-skill-name>/SKILL.md (the directory name must match `name:`),
then run: uv run python scripts/bootstrap.py

Rules that reviews enforce (docs/EXTENDING.md §1):
- Call tools by their REGISTERED names: mcp__healthmes__<tool> /
  mcp__open_wearables__<tool> (double underscores).
- Never instruct raw REST calls — data access goes through MCP tools only,
  so every decision stays reconstructable in the decision tree.
- Always record the decision (mcp__healthmes__record_decision) after a
  recommendation, and put the returned viewer_url in the message.
- Gate advice on confidence: on "low" or "insufficient_data", say the data
  is too thin — never give categorical advice.
- Proactive messages follow the notification grammar (PLAN.md §8.5):
  observation line → evidence line → proposal → one-tap choices → why-link.
-->

# When to use

Describe the situations (user questions, alert types, briefing sections)
where the agent should apply this skill — and when it should NOT.

# Data to gather

1. `mcp__healthmes__get_daily_readiness_context` with `date=today` — …
2. `mcp__open_wearables__get_timeseries` with `types=[…]` over the last N
   days — … (check provider coverage first: not every device has every
   signal; see docs/EXPERT-ONBOARDING.ko.md §1)

# Judgment procedure

1. If <condition on the interpreted values> AND confidence is "high" →
   recommend <action>, phrased as observation/evidence/proposal.
2. If <other condition> → …
3. If confidence is "low" or any input is insufficient_data → say exactly
   what data is missing and how to get it (e.g. wear the watch overnight).

# After deciding

- Call `mcp__healthmes__record_decision` with the tree of inputs → rules →
  chosen option; include the returned viewer_url as the "왜 이 판단?" link.
- If the recommendation changes the schedule, use
  `mcp__healthmes__propose_schedule_blocks` (propose-then-confirm — never
  write the calendar directly).
