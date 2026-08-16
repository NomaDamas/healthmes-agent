# Extending HealthMes — a guide for domain experts

This project is deliberately split so that domain calculation, cross-domain
reasoning, and runtime presentation do not silently replace one another:

| You want to add… | Extension point | Skill level |
|---|---|---|
| A channel workflow or presentation convention | A thin **skill** — one markdown file | No code |
| A deterministic metric or specialist boundary | A **Context Provider / Layer B tool** | Python |
| A cross-domain reasoning contract | The **HealthMes wellness runtime** contract, Skill catalog, and evals | Python + prompt/eval design |
| A new correlation report | An **insight template** | Python (SQL-ish) |

The hard boundary is: domain providers calculate exact facts and specialist
limits; Hermes owns one autonomous LLM/tool loop; HealthMes owns the product
ingress, bounded tools, source validation, and conditional finalization.
Skills only teach the runtime how to use that workflow. A skill file is not an
authorization, retention, privacy, calculation, or persistence enforcement
mechanism. See `docs/HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md`.

## 1. Adding a thin skill (no code)

Skills are markdown instruction files the agent loads when planning, capturing,
or presenting an answer. Each lives in `skills/<skill-name>/SKILL.md` and follows the
vendor format (see the five existing skills as templates —
`skills/healthmes-planner/SKILL.md` is the richest example).

```markdown
---
name: sleep-apnea-screening
description: Screen weekly data for sleep-apnea risk markers and advise follow-up.
---

# When to use
When the user asks about snoring, daytime sleepiness, or during weekly review.

# Procedure
1. Call mcp__healthmes__search_wearable with metrics=["sleep"] for 14 days.
2. If the bounded tool reports that SpO2 or respiratory coverage is unavailable,
   say which source is missing rather than calling Open Wearables directly.
3. If nightly SpO2 dips below … AND confidence is "high", say …
   If confidence is "low"/"insufficient_data", say the data is too thin — never
   give categorical advice on low confidence.
```

Ground rules for skill authors:

- **Do not put mandatory product policy only in a skill.** Authorization,
  retention, privacy, exact calculations, tool budgets, source-reference
  validation, and decision persistence belong to HealthMes code.
- **Reference tools by their registered names**:
  `mcp__healthmes__<tool>` (double underscores). The product runtime does not
  expose direct Open Wearables tools.
- **Never instruct raw REST calls** — decision data access must go through
  bounded HealthMes MCP tools so returned source references can be validated.
- Do not instruct the runtime to call `record_decision` for every lookup.
  Conditional compact persistence after source validation is the target of
  PR #138 issue #164, not the behavior of the current finalizer. Until #164
  is integrated, every completed source-bearing decision is still stored.
  Do not describe the target policy as shipped.
- **Respect confidence**: the tools return `confidence` / `coverage` /
  `insufficient_data` honestly; skills must gate advice on them.
- Multiple skills are welcome — one file per clinical question keeps them
  composable. Add reviewed skills to the read-only wellness catalog used by
  the HealthMes decision ingress. Proactive and scheduled requests use the
  same catalog through the same internal DecisionRequest service; do not
  create a separate direct-Hermes reasoning path.

Install: `uv run python scripts/bootstrap.py` (idempotent; copies the skill
into `$HERMES_HOME/skills/` and resyncs on every re-run).

## 2. Adding a metric (Layer B MCP tool)

Deterministic Python in `healthmes/mcp_server/`:

- `ow_client.py` — typed client for the open-wearables REST v1 (100+ series
  types, health scores, sleep/workout events). Add a fetch helper here if your
  metric needs an endpoint that is not wrapped yet — ground every path in
  `vendor/open-wearables/backend/app/api/routes/v1/`.
- `interpret.py` — pure math: baselines (14-day trailing median), z-scores,
  coverage/confidence bucketing. Put your derivation here as a pure function
  with a hand-computable unit test.
- `server.py` — register the tool on the `FastMCP("healthmes")` instance:

```python
@mcp.tool()
async def get_glucose_stability(date: str) -> dict:
    """Interpreted glucose stability for a day: time-in-range, spikes vs
    personal baseline, confidence."""
    ...
    return {
        "status": "ok",              # or "insufficient_data"
        "time_in_range_pct": 78.2,
        "spikes_vs_baseline": +2,
        "confidence": "medium",      # measurement-condition aware
        "coverage": {"samples": 96, "expected": 288},
    }
```

Design rules (enforced in review):

- Return **interpreted deltas + confidence**, never raw series dumps —
  privacy, token cost, and hallucination control all depend on this.
- Missing data is a **first-class result** (`insufficient_data`), not an error.
- Don't reimplement vendor scoring — open-wearables already computes sleep
  (4-factor) and resilience (HRV-CV) scores; consume them
  (`get_health_scores`).
- HRV variants (SDNN vs RMSSD) must never be mixed across providers;
  baselines are kept per-variant (see `interpret.py`).
- Tests: `tests/mcp_server/` pattern — httpx `MockTransport` fixtures for OW
  responses, sqlite store, hand-computed expected values.

## 3. Adding an insight template

Deterministic correlation reports live in `healthmes/api/insight_templates.py`
(hour-of-day / weekday / calendar-keyword stress) and
`insight_focus.py` (energy-dip factor attribution). Add a template function
returning `insight` rows with `statement`, `evidence` (JSON) and `confidence`;
wire it into the recompute pipeline in `insights.py`. Freeform data mining is
deliberately out of scope (docs/PLAN.md §11) — templates only.

## 4. QA workflow for domain experts

Everything runs locally with zero credentials (sqlite):

```bash
make mac-run                 # boots API + /mcp on :8100
open http://localhost:8100/docs         # REST playground (OpenAPI)
```

- **End-to-end wellness reasoning QA** — call the HealthMes product ingress,
  not the vendor CLI:

  ```bash
  curl -sS http://localhost:8100/v1/wellness-decisions \
    -H 'Content-Type: application/json' \
    -d '{"question":"오늘 무리해도 돼? 필요한 자료를 찾아 근거와 함께 설명해줘."}'
  ```

  Add the configured bearer header when `HEALTHMES_API_TOKEN` is enabled.
  Direct `hermes` / `hermes chat` remains useful for isolated vendor-runtime
  diagnostics, but it bypasses the HealthMes decision ingress, source
  validation, and finalization policy. It must not be used to certify product
  wellness behavior.

- **Direct tool calls without an LLM** (fastest metric QA):

  ```bash
  uv run python - <<'PY'
  import asyncio
  from fastmcp import Client
  from healthmes.mcp_server.server import build_mcp_http_app  # or connect to :8100/mcp

  async def main():
      async with Client("http://localhost:8100/mcp") as c:
          print(await c.call_tool("get_daily_readiness_context", {"date": "2026-07-10"}))
  asyncio.run(main())
  PY
  ```

- **Decision audit**: PR #138 issue #164 targets compact persistence for
  behavior-changing recommendations, mutations, material risk warnings, and
  explicitly tracked decisions; after that target lands, simple lookups will
  remain unpersisted by default.
  Until #164 is integrated, the current finalizer still stores every completed
  source-bearing decision. Open `http://localhost:8100/decisions` to review
  records that the current build retained and challenge the judgment.
- **Regression**: `make mac-test` — add one test per metric with a
  hand-computed vector; that is the contract your metric keeps forever.
