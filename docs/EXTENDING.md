# Extending HealthMes — a guide for domain experts

This project is deliberately split so that **healthcare domain knowledge goes
into two small, safe extension points** without touching the engine:

| You want to add… | Extension point | Skill level |
|---|---|---|
| A judgment procedure ("when X and Y, advise Z") | A **skill** — one markdown file | No code |
| A new metric / derived indicator | A **Layer B MCP tool** — one Python function | Python |
| A new correlation report | An **insight template** | Python (SQL-ish) |

The hard rule behind all three (docs/PLAN.md §1.5): **tools state deterministic
facts, skills hold the judgment**. The LLM never computes a metric; it reads
interpreted numbers (with confidence) and reasons about them.

## 1. Adding a skill (no code)

Skills are markdown instruction files the agent loads when planning, capturing,
or answering. Each lives in `skills/<skill-name>/SKILL.md` and follows the
vendor format (see the three existing skills as templates —
`skills/healthmes-planner/SKILL.md` is the richest example).

```markdown
---
name: sleep-apnea-screening
description: Screen weekly data for sleep-apnea risk markers and advise follow-up.
---

# When to use
When the user asks about snoring, daytime sleepiness, or during weekly review.

# Procedure
1. Call mcp__healthmes__get_health_scores with categories=["SLEEP"] for 14 days.
2. Call mcp__open_wearables__get_timeseries with types=["spo2", "respiratory_rate"] …
3. If nightly SpO2 dips below … AND confidence is "high", say …
   If confidence is "low"/"insufficient_data", say the data is too thin — never
   give categorical advice on low confidence.
```

Ground rules for skill authors:

- **Reference tools by their registered names**: `mcp__healthmes__<tool>` and
  `mcp__open_wearables__<tool>` (double underscores).
- **Never instruct raw REST calls** — data access must go through MCP tools so
  every decision stays reconstructable in the decision tree.
- **Always instruct `record_decision`** after a recommendation, so the decision
  viewer can show *why*.
- **Respect confidence**: the tools return `confidence` / `coverage` /
  `insufficient_data` honestly; skills must gate advice on them.
- Multiple skills are welcome — one file per clinical question keeps them
  composable. Register a skill for proactive alerts by listing it in the
  webhook route (`config/hermes-config.yaml.tmpl` → `skills:`) or for
  briefings in `scripts/bootstrap.py::BRIEFING_JOBS`.

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

- **Interactive tool QA from the terminal** — the vendor CLI talks to the same
  MCP tools and skills the Telegram agent uses:

  ```bash
  cd vendor/hermes-agent && \
    HERMES_HOME=~/.hermes UV_PROJECT_ENVIRONMENT=../../data/hermes-venv \
    uv run --frozen --no-dev --extra messaging hermes chat -q \
    "오늘 무리해도 돼? get_daily_readiness_context로 근거 보여줘"
  ```

  (interactive session: `hermes` with no arguments; switch models with
  `--model/--provider` or `HERMES_MODEL`/`HERMES_PROVIDER` in `.env`.)

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

- **Decision audit**: every agent recommendation writes a `decision_record`;
  open `http://localhost:8100/decisions` to review the tree (inputs → rules →
  LLM rationale → action) and challenge the judgment.
- **Regression**: `make mac-test` — add one test per metric with a
  hand-computed vector; that is the contract your metric keeps forever.
