# HealthMes Agent

HealthMes Agent is a **proactive, health-aware personal assistant**: it reads
your wearable data (11 providers via open-wearables), your calendar and your
app usage, estimates your cognitive energy hour by hour, plans your week
around it, and **messages you first** on Telegram when something needs to
change — every proactive decision explorable as a flowchart in the browser.

It is glue around two unmodified vendored upstreams:

- `vendor/hermes-agent/` — agent runtime (skills, memory, cron, Telegram
  gateway, MCP client, Claude API)
- `vendor/open-wearables/` — wearable data plane (Garmin/Oura/Fitbit/Whoop/
  Polar/Suunto/Ultrahuman/Strava/Apple/Google/Samsung; sleep/stress/HRV
  scores; FastAPI + Postgres + Celery; its own MCP server)

Everything HealthMes adds lives at the repo root (`healthmes/`, `skills/`,
`config/`, `scripts/`, `apps/`), talking to the vendors only over their
public contracts (REST, MCP, webhook, rendered config). Architecture and
rationale: [`docs/PLAN.md`](docs/PLAN.md).

```
Telegram (phone + watch)          decision viewer (web)
        │  chat/push                     ▲ links in alerts
┌───────▼──────────┐    MCP    ┌─────────┴─────────────┐
│  agent plane     │◄─────────►│  healthmes service    │
│  hermes-agent    │◄──webhook─│  FastAPI + /mcp       │
│  (vendored)      │           │  store·engines·sync   │
└───────┬──────────┘           └─────────┬─────────────┘
        │ MCP (stdio)                    │ REST (read-only)
┌───────▼─────────────────────────────── ▼──────────────┐
│  data plane — open-wearables (vendored)               │
└───────────────────────────────────────────────────────┘
```

## What works today

**Data & domain (Phase 0–1)**
- Dedicated `healthmes` database (Postgres or zero-setup sqlite) with its own
  models + alembic migrations: weekly goals, tasks, schedule proposals,
  calendar mirror, food logs, app-usage samples, energy estimates, decision
  records, insights, medical records, trigger events.
- REST surface under `/v1/*` plus a Streamable-HTTP MCP server at exactly
  `/mcp` (the URL the Hermes gateway registers).
- Calendar sync (`healthmes/calendars/`): Google Calendar (syncToken
  increments) and iCloud CalDAV (ctag/etag), ownership-split conflict
  philosophy — the agent only writes its own tagged blocks. With
  `HEALTHMES_GOOGLE_CALENDAR_ENABLED` / `HEALTHMES_CALDAV_ENABLED` (and the
  scheduler on) the service polls every 5/10 minutes and writes
  user-accepted schedule proposals to the calendar, advancing them to
  `pushed`.
- Bearer-token auth over the whole HTTP surface (REST, viewer pages, `/mcp`)
  once `HEALTHMES_API_TOKEN` is set; non-loopback binds refuse to start
  without it, so medical data is never network-readable unauthenticated.
- Proactive alert loop (`healthmes/engine/`): deterministic 10-minute trigger
  sweep (stress spike vs baseline, low recovery + heavy afternoon, external
  schedule changes, deadline risk) → HMAC-signed webhook → Hermes → Telegram.
  Alert hygiene built in: per-rule cooldown, daily budget, quiet hours,
  dedup keys, per-rule crash isolation.
- Hermes bootstrap (`scripts/bootstrap.py`): renders the gateway config,
  copy-installs `skills/`, registers morning/evening/weekly cron briefings.

**Cognitive energy & explainability (Phase 2)**
- Rule-based, fully explainable energy engine (`healthmes/engine/
  cognitive_energy.py`): sleep debt (open-wearables' own internal sleep
  score, never reimplemented), time-weighted stress (or HRV/resilience proxy
  without a Garmin), nightly HRV vs personal 14-day baseline, body-battery
  bonus, meeting load, app fragmentation. Missing signals drop out and
  weights renormalize; components always sum exactly to the score.
- Hourly persist job + `GET /cognitive-energy/forecast?date=` (24 windows
  with full component breakdowns).
- 14 MCP tools the agent decides with: `get_health_scores`,
  `get_daily_readiness_context`, `get_personal_baselines`,
  `get_cognitive_energy_forecast`, `get_stress_timeline` (stress segments
  joined with calendar + app usage), `compare_impact` (does factor X move
  metric Y for me?), task/schedule CRUD (`list_tasks`, `upsert_task`,
  `get_schedule`, `propose_schedule_blocks`), `log_food`,
  `create_medical_record`, `list_medical_records`, `record_decision` —
  all returning interpreted deltas with confidence/coverage, honest
  `insufficient_data` when signals are thin.
- Decision viewer: every proactive decision is a `decision_record` tree
  rendered as a Mermaid flowchart at `/decisions/{id}` (vendored Mermaid,
  no CDN), with a paginated index at `/decisions`.
- Insights: template-based aggregations only (no freeform mining), including
  the focus template ("14–16h focus drop: sleep deficit + Slack 9
  launches/hour").
- Android usage collector ([`apps/android-usage/`](apps/android-usage/)):
  minimal Kotlin companion app (pairing + toggle) that buckets
  `UsageStatsManager` events hourly and uploads to
  `POST /v1/app-usage/batch` every 30 minutes. iOS is deliberately skipped
  (OS sandbox); the engine renormalizes without the signal.

**Medical-lite & backups (Phase 3)**
- Capture via Telegram (no new app): the `healthmes-capture` skill routes
  photos/voice to `log_food` or `create_medical_record` (medication/symptom)
  with an LLM-written description, media path and a capture-time health
  snapshot; one-tap correction preserves the original. Medical data never
  leaves the machine except the description text sent to the LLM;
  `doctor-visit-summary` assembles a local briefing file for appointments.
- Local-first encrypted backups (`healthmes/backup/`): versioned snapshot
  envelope (healthmes DB dump, optional open-wearables dump, media tree,
  Hermes state) → tar → age encryption (passphrase). `healthmes backup
  create/list/restore` CLI + weekly scheduler job. The `BackupProvider`
  protocol is the seam for a future `RemoteVaultProvider` (ciphertext-only
  server) — see [`docs/BACKUP.md`](docs/BACKUP.md).
- Hardening: restore drills, trigger-flood tests, CI (linux + macos),
  vendor-drift report (`scripts/vendor_sync_check.sh`).

**Skills** (`skills/`, copied into the Hermes home by bootstrap):
`healthmes-planner` (goal dump → task breakdown → energy-aware block
proposals → decision recording), `healthmes-capture` (food + medical),
`doctor-visit-summary`.

## Quickstart (mac-native, primary path)

Requires [uv](https://docs.astral.sh/uv/) and Homebrew; everything is
ephemeral and repo-local (no `brew services`, no autostart).

```bash
make mac-setup            # brew postgresql@16 + redis if missing, initdb,
                          # create DBs, uv sync
cp .env.example .env      # optional: sqlite works with zero config
make mac-run              # alembic upgrade head + service on :8100
curl http://localhost:8100/health
make mac-test             # full offline test suite
make mac-services-stop    # stop the ephemeral postgres + redis
```

With no `.env` at all, the service runs against a repo-local sqlite file —
`make mac-run` alone is a working single-process demo. The full experience
(Telegram agent + wearable syncs) needs the credentials matrix in
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) and
`uv run python scripts/bootstrap.py` for the Hermes side.

### Docker alternative

```bash
cp .env.example .env
cp config/open-wearables.env.example config/open-wearables.env
docker compose up -d --build     # postgres, redis, open-wearables (+worker,
                                 # +mcp), healthmes, hermes gateway
```

Set `HEALTHMES_TIMEZONE` (e.g. `Asia/Seoul`) in `.env` for the compose path —
container clocks are UTC. The compose path also **requires**
`HEALTHMES_API_TOKEN` (the container binds 0.0.0.0 and publishes the port;
the service refuses to start unauthenticated on a non-loopback bind).

### CLI chat & choosing your LLM

The same agent is available from the terminal (no Telegram needed) via the
vendor CLI, against the same skills and MCP tools — see the CLI section of
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md). Claude is only the default
model: any of the ~29 vendor provider plugins (OpenAI, Gemini, OpenRouter,
Ollama, Bedrock, …) can be selected with `HERMES_MODEL`/`HERMES_PROVIDER` in
`.env` — all HealthMes glue is provider-agnostic.

### Extending with domain knowledge

Healthcare experts can add judgment procedures as **skills** (one markdown
file, no code), new metrics as **Layer B MCP tools**, and correlation
reports as **insight templates** — plus a local QA workflow to challenge the
agent's decisions. See [`docs/EXTENDING.md`](docs/EXTENDING.md).

### Backups

```bash
export HEALTHMES_BACKUP_PASSPHRASE='...'   # or set it in .env
uv run healthmes backup create             # age-encrypted snapshot
uv run healthmes backup list
uv run healthmes backup restore <name>     # dry-run; add --yes to apply
```

## Repository layout

- `healthmes/` — the glue service: `store/`, `engine/`, `calendars/`,
  `mcp_server/`, `api/`, `backup/`
- `skills/` — Hermes skills; `apps/android-usage/` — usage collector
- `config/`, `scripts/`, `alembic/`, `tests/`, `docs/`
- `vendor/hermes-agent/`, `vendor/open-wearables/` — read-only upstreams,
  never modified

Developer guide (run paths, credentials, tests, CI, vendor sync):
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

## References

This project is based on and references:

- Hermes Agent: https://github.com/NousResearch/hermes-agent
- open-wearables: https://github.com/the-momentum/open-wearables

The open-wearables code is kept in a separate folder so wearable data
integration work can be developed without mixing it into the Hermes runtime
base.

## License

HealthMes Agent is available for non-commercial use under the project license
in `LICENSE`.

Commercial use requires a separate paid commercial license from the project
owner. See `LICENSE` for details.

This repository includes code derived from Hermes Agent by Nous Research and
open-wearables by Momentum, both released under the MIT License, and vendors
the Mermaid diagram library (MIT). Original notices are preserved in
`THIRD_PARTY_NOTICES.md`.
