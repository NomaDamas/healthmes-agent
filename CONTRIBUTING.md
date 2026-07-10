# Contributing to HealthMes Agent

Two kinds of contributions are expected here, and both are first-class:

- **Engineering** — glue code in `healthmes/`, tests, infra.
- **Domain expertise** — healthcare indicators, decision metrics, and skills.
  If that is you, start with the Korean onboarding guide:
  [`docs/EXPERT-ONBOARDING.ko.md`](docs/EXPERT-ONBOARDING.ko.md), then
  [`docs/EXTENDING.md`](docs/EXTENDING.md). You can ship a complete
  contribution (a skill) without writing any Python.

## Ground rules

1. **`vendor/` is read-only.** Both upstreams are vendored verbatim for
   upstream sync; all glue lives at the repo root (`healthmes/`, `skills/`,
   `config/`, `scripts/`, `apps/`).
2. **Python via uv only** (`uv sync`, `uv run …`), Python 3.12, deps managed
   exclusively in `pyproject.toml` + `uv.lock`.
3. **Code, comments, and docstrings in English.** Discussion and
   domain-facing docs may be Korean (this repo's experts work in Korean).
4. **Determinism boundary**: MCP tools state deterministic facts
   (interpreted deltas + confidence); skills hold judgment; the LLM never
   computes a metric. Health tools return aggregates, never raw series
   dumps (privacy + hallucination control — see `docs/PLAN.md` §1.5/§9).
5. **Honest data handling**: missing data is `insufficient_data`, not an
   error and not a guess. Advice must be gated on `confidence`.

## Dev loop

```bash
make mac-setup        # once: brew postgres/redis (ephemeral), uv sync
make mac-run          # sqlite zero-setup boot on :8100
make mac-test         # full offline suite (must be green)
uv run ruff check .   # lint (CI-enforced)
```

Every metric/rule needs a hand-computed test vector under `tests/` — that
vector is the contract your change keeps forever. See existing patterns in
`tests/mcp_server/` (httpx MockTransport + sqlite fixtures).

## PR flow

- Branch from `main` (`feat/…`, `fix/…`, `docs/…`, `skill/…`).
- CI must pass (ubuntu lint+tests+compose render, compose boot smoke,
  macOS `make mac-test`).
- `main` is protected: **1 approving review required**, linear history
  (rebase or squash — no merge commits), conversations resolved.
- Heads-up: this clone carries an `upstream` remote pointing at the vendor's
  repo — always pass `--repo NomaDamas/healthmes-agent` to `gh`, or issues
  and PRs will land on the wrong project.

## Proposing domain work

Use the issue forms (they structure exactly what a reviewer needs):

- **Metric proposal** — a new indicator/derived metric with its
  interpretation rule and confidence conditions.
- **Skill proposal** — a new judgment procedure ("when X and Y, advise Z")
  over existing tools.

Skill starter template: [`docs/templates/SKILL.md`](docs/templates/SKILL.md)
(copy into `skills/<your-skill>/SKILL.md`; never edit `$HERMES_HOME`
directly — `scripts/bootstrap.py` owns installation).
