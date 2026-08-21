# HealthMes

HealthMes is **source-available, local-first infrastructure for wellness
agents**.
It turns wearable, activity, nutrition, calendar, environment and subjective
signals into permission-aware context that an agent can use to make
explainable, reversible decisions.

The first product built on that infrastructure is a proactive personal
assistant: it estimates cognitive capacity, plans work around it, proposes
schedule or recovery interventions, and records what happened afterward.
The infrastructure is the product boundary; the assistant, native apps and
chat channels are reference experiences on top.

## Platform boundary

```text
CLI / Discord / Telegram / native apps / custom clients
                         |
                         v
┌──────────────── Agent runtime adapters ────────────────┐
│ Hermes today; other runtimes can implement the contract│
│ model loop · sessions · memory · cron · channel I/O    │
└────────────────────────┬───────────────────────────────┘
                         │ MCP / typed tool contract
                         v
┌────────────────── HealthMes core ──────────────────────┐
│ Decision Agent contract · Context Access Layer         │
│ deterministic domain providers · source_refs           │
│ decision/outcome graph · consent · retention · backup  │
└───────────────┬───────────────┬───────────────┬────────┘
                v               v               v
        Open Wearables      Activity        Nutrition /
        + HealthKit         collectors      Calendar / more
```

HealthMes owns the wellness-specific contracts and safety boundaries:

- a common `WellnessEvent` envelope with provenance, freshness, confidence,
  consent, sensitivity and retention;
- deterministic activity, wearable, nutrition, calendar and capacity
  providers that calculate facts instead of asking an LLM to invent numbers;
- a Context Access Layer that limits which data, time range and privacy level
  an agent may read;
- explainable decisions linked to the exact `source_refs`, user response,
  execution and later outcome;
- a Personal Data Node with local storage, deletion controls and
  client-encrypted backup.

HealthMes deliberately delegates commodity infrastructure:

- `vendor/hermes-agent/` provides the current LLM/tool loop, skills, memory,
  cron and multi-channel gateway, including CLI, Discord and Telegram;
- `vendor/open-wearables/` provides wearable integrations and health scores;
- native apps and chat surfaces consume stable HealthMes contracts rather
  than becoming the source of wellness policy.

Both vendored trees are pinned upstream snapshots. HealthMes uses documented
REST, MCP, webhook, configuration, skill and delivery contracts first. If
those extension points cannot safely provide a required capability, a scoped,
separately reviewed vendor patch is allowed. The policy preserves upstream
syncability; it is not a blanket ban. See
[`docs/HEALTHMES-DECISION-AGENT-ARCHITECTURE.ko.md`](docs/HEALTHMES-DECISION-AGENT-ARCHITECTURE.ko.md),
[`docs/PLAN.md`](docs/PLAN.md), and
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md#vendor-patches).

## Why this can compound

Wearable connectors, MCP, chat, automatic scheduling and native widgets are
useful but are not a durable moat by themselves. The potential moat is the
private, user-owned graph accumulated over time:

```text
state + context -> decision -> proposed intervention
                -> accept / edit / reject / ignore
                -> actual behavior -> later wellness and work outcome
```

That graph can answer the harder personal question: **under which conditions
did which intervention actually help this user?** Cross-domain context,
expert protocols and calibrated trust compound around it. This is still a
product hypothesis, not a proven market moat; it requires sustained dogfood
and measured outcomes. See
[`docs/MOAT-CROSS-DOMAIN-WELLNESS-CONTEXT.ko.md`](docs/MOAT-CROSS-DOMAIN-WELLNESS-CONTEXT.ko.md)
and [`docs/COMPETITIVE-LANDSCAPE.ko.md`](docs/COMPETITIVE-LANDSCAPE.ko.md).

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
  `pushed`. Existing external events stay immutable by default; the narrow
  exception is a confirmed morning recovery `SHORTEN` of one eligible Google
  event, resolved from a Telegram live reply (`적용 <handle>` / `그대로
  <handle>`) through the HealthMes MCP contract.
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
  The 07:00 prompt calls the server-owned morning evaluator once, sends its
  display packet and plain-text reply handle to Telegram, then exits without
  waiting; live replies are resolved by the normal Hermes gateway session.

**Cognitive energy & explainability (Phase 2)**
- Rule-based, fully explainable energy engine (`healthmes/engine/
  cognitive_energy.py`): sleep debt (open-wearables' own internal sleep
  score, never reimplemented), time-weighted stress (or HRV/resilience proxy
  without a Garmin), nightly HRV vs personal 14-day baseline, body-battery
  bonus, meeting load, app fragmentation. Missing signals drop out and
  weights renormalize; components always sum exactly to the score.
- Hourly persist job + `GET /cognitive-energy/forecast?date=` (24 windows
  with full component breakdowns).
- MCP tools the agent decides with: `get_health_scores`,
  `get_daily_readiness_context`, `get_personal_baselines`,
  `get_cognitive_energy_forecast`, `get_stress_timeline` (stress segments
  joined with calendar + app usage), `compare_impact` (does factor X move
  metric Y for me?), task/schedule CRUD (`list_tasks`, `upsert_task`,
  `get_schedule`, `propose_schedule_blocks`), `log_food`,
  `create_medical_record`, `list_medical_records`, `record_decision` —
  all returning interpreted deltas with confidence/coverage, honest
  `insufficient_data` when signals are thin.
- Sake nutrition evidence slice: uploaded food/drink photos are analyzed into
  the versioned `NutritionObservation` contract by local Ollama (default) or an
  explicitly authorized OpenAI, Gemini, Anthropic, or xAI provider. The
  structured payload is stored intact as a `WellnessEvent`; photo, observation,
  and confirmation each have independent retention. The current bounded slice
  extracts caffeine evidence only, not full nutrition. A device-neutral intake
  engine wraps photo observations and accepts exact text entries or locally
  produced voice transcripts without treating any capture as consumed. REST
  and MCP adapters preserve intent, explicit consumption outcomes, reusable
  nutrient facts, prospective decision requests, evidence references, and
  agent decisions. Raw text/transcripts/media remain short-lived while durable
  outcomes and decision requests retain sanitized structured snapshots. Writes
  use caller-owned idempotency UUIDs plus permanent non-content tombstones that
  prevent reuse after raw expiry, and decision context is immutable after
  request creation. Automatic text nutrition extraction and server-side voice
  transcription are not implemented. The original caffeine tools still return
  a daily total only after every item and the complete local day are explicitly
  confirmed; generic caffeine decisions cannot emit actionable proposals.
- Decision viewer: every proactive decision is a `decision_record` tree
  rendered as a Mermaid flowchart at `/decisions/{id}` (vendored Mermaid,
  no CDN), with a paginated index at `/decisions`.
- Insights: template-based aggregations only (no freeform mining), including
  the focus template ("14–16h focus drop: sleep deficit + Slack 9
  launches/hour").
- Activity Wellness MVP (`healthmes/activity/`): Android's Kotlin collector
  reads `UsageStatsManager`, uploads ordered provisional/final hourly
  snapshots, and stores them in the same `WellnessEvent` data plane used by
  other wellness inputs. Desktop ActivityWatch data can be imported through
  the bounded localhost adapter; automatic periodic import is not implemented.
  iOS currently exposes only an honest aggregate/unavailable capability
  contract, not a production Screen Time timeline collector. Retention,
  deletion, hourly/daily aggregation, focus/overwork/recovery context, REST
  and MCP surfaces are implemented. The compatibility resolver assembles
  bounded context only; natural-language LLM planning, final wellness
  judgment, automatic DecisionRecord finalization and Hermes adaptation are
  separate Decision Agent work.

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

**Glanceable surfaces & companion apps (Phase 5–7, pre-device plumbing)**
- `GET /v1/briefing/glance`: the compact briefing contract widgets and
  watch faces poll — energy score + 24h curve + freshness confidence, next
  blocks (≤3), alert summary, latest decision link. Strong ETag / 304
  revalidation with a 5-minute cache; bearer-authed like the rest of `/v1`.
- Android companion ([`apps/android-usage/`](apps/android-usage/)): beside
  the `:app` usage collector, `:shared` (contract parser + ETag client +
  encrypted pairing), `:companion` (home/lock-screen Glance widget, 15-min
  WorkManager refresh, a notification channel rendering the PLAN §8.5
  grammar) and `:wear` (Wear OS tile + energy complication, on-watch
  pairing) — `:companion` has since been promoted to the full phone app
  (issue #10, matrix below).
- iOS/watchOS companion ([`apps/ios-companion/`](apps/ios-companion/)):
  XcodeGen project — SwiftUI pairing app (Keychain token, WatchConnectivity
  push to the watch), WidgetKit home + lock-screen widgets, watchOS app and
  accessory complications. Simulator-verified builds and tests; no signing.
  Since grown into the full iOS app (issue #10, matrix below).
- Local-first throughout: the apps pair with **your own** healthmes instance
  (base URL + bearer token) and talk to nothing else; polling only, no
  APNs/FCM relay — Telegram remains the reliable push channel. All
  widget/watch rendering is deliberately placeholder: the notification/watch
  UX design is reserved for the healthcare domain expert
  (worksheet: `docs/design/WATCH-NOTIFICATIONS.ko.md`, issue #7).
- Weekly report at `/reports/weekly` (+ `.json`): energy trend sparkline,
  insights with confidence badges, schedule adherence, alert digest vs
  budget, the week's decisions — shareable via the same derived read-only
  `?token=` link as the decision viewer; the Sunday briefing points at it.
- Cognitive-energy v2 factors: menstrual phase, daylight, noise exposure,
  alcohol, hydration join the engine under the same
  missing-signal-renormalizes rule; weights and thresholds are explicit
  placeholders for the domain expert to tune.
- Remote vault backups (`RemoteVaultProvider`, PLAN §9 business seam):
  replicate age-encrypted snapshot envelopes to any S3-compatible bucket
  (AWS S3 / Cloudflare R2 / MinIO). The vault only ever sees ciphertext —
  the provider refuses to upload anything that is not an age envelope.
  `healthmes backup push` / `--provider remote` / weekly-job selector
  (`HEALTHMES_BACKUP_PROVIDER`) — see [`docs/BACKUP.md`](docs/BACKUP.md).

**Full native apps & desktop glance surfaces (issues #10–#11)**

The glance plumbing above grew into five surfaces, all speaking the same
contracts (`GET /v1/briefing/glance` with ETag/304, `GET /v1/alerts`,
`/reports/weekly.json`, the §8.5 notification grammar, capture via
`POST /v1/media` + food/medical endpoints) against the **paired instance
only** — no third-party SDKs, no analytics, no push relay (polling only;
Telegram stays the guaranteed-delivery channel). Visuals stay
placeholder-labeled for the domain expert
(`docs/design/WATCH-NOTIFICATIONS.ko.md`); information architecture and
plumbing are real and tested.

| Surface | Where | What | Build & test |
|---|---|---|---|
| Android phone + Wear OS | [`apps/android-usage/`](apps/android-usage/) | full Compose app — briefing home + 24h curve, weekly report, camera/voice capture, real ✅/✏️/❌ proposal actions, focus-block ongoing notification bridged to the watch; widgets, Wear tile/complication, `:app` usage collector | `cd apps/android-usage && ./gradlew assembleDebug test` |
| iOS + watchOS | [`apps/ios-companion/`](apps/ios-companion/) | full SwiftUI app — briefing home, weekly report, in-app decision viewer, capture, §8.5 notifications with real actions (BGAppRefreshTask), focus-block Live Activity; home/lock widgets, watch app + complications | `cd apps/ios-companion && xcodegen generate && xcodebuild test …` (README) |
| macOS | [`apps/macos-companion/`](apps/macos-companion/) | menu bar score + popover briefing with real proposal actions, WidgetKit widgets, ambient screensaver (`.saver`) with privacy toggle | `cd apps/macos-companion && xcodegen generate && xcodebuild test …` (README) |
| Windows | [`apps/windows-companion/`](apps/windows-companion/) | tray icon + flyout + §8.5 toasts, screensaver (`.scr`) with privacy toggle, widgets-board card builder (provider deferred — needs MSIX signing) | `dotnet build HealthMes.Companion.sln && dotnet test …` (windows-latest CI) |
| Web (no new UI) | served by healthmes + vendored Hermes web console | decision-viewer flowcharts + weekly report page (the tokenized links every app opens), chat/admin console | part of the Python service (`make mac-test`) |

Accessibility (VoiceOver / TalkBack / keyboard+Narrator basics, Dynamic
Type) and Korean + English localization ship on every surface. Contract
drift is pinned server-side: `tests/api/test_glance_fixtures.py` validates
each platform's glance/alerts/weekly fixtures against the live pydantic
models, so a schema change fails CI before any app breaks. Per-surface
honest verification status (proven by build/test vs. still needs real
hardware) lives in each app's README.

**Skills** (`skills/`, copied into the Hermes home by bootstrap):
`healthmes-planner` (goal dump → task breakdown → energy-aware block
proposals → decision recording), `healthmes-capture` (food + medical),
`healthmes-caffeine` (exact event + current sleep + explicit user bounds →
read-only bounded preparation proposal),
`healthmes-nutrition` (photo observation review + caffeine confirmation),
`healthmes-sleep` (sleep/readiness evidence → cautious daily intensity
decision), `healthmes-stress` (source-aware stress/recovery evidence →
keep/reconsider/insufficient-data decision), `doctor-visit-summary`.

## Quickstart (macOS)

The shortest path needs Git, [Homebrew](https://brew.sh/) and
[uv](https://docs.astral.sh/uv/). It does not need Docker, PostgreSQL, Redis
or an `.env` file.

### 1. Run the HealthMes core locally

From a fresh terminal:

```bash
git clone https://github.com/NomaDamas/healthmes-agent.git
cd healthmes-agent
command -v uv >/dev/null || brew install uv
make mac-run
```

The first run downloads the pinned Python toolchain and dependencies, applies
the local database migrations, then serves HealthMes on
`http://127.0.0.1:8100`. Keep that terminal open. In a second terminal:

```bash
curl http://127.0.0.1:8100/health
# {"status":"ok"}
```

That response confirms that the HealthMes API, local SQLite store and MCP
endpoint are running. Press `Ctrl-C` in the first terminal to stop the
service. All generated state stays under the repository's ignored `data/`
directory.

If port 8100 is already occupied, choose another local port:

```bash
HEALTHMES_PORT=8110 make mac-run
curl http://127.0.0.1:8110/health
```

For the full local data stack, install and start repo-local PostgreSQL and
Redis as well:

```bash
make mac-setup
make mac-run
```

`make mac-setup` is safe to re-run. It installs `postgresql@16` and Redis
through Homebrew when missing, initializes them under `data/`, creates the
HealthMes and Open Wearables databases, and syncs dependencies. It never
registers `brew services`; stop these processes with
`make mac-services-stop`.

Copy `.env.example` only when you are ready to select PostgreSQL, expose the
API to another device, or add wearable, calendar, model or messaging
credentials:

```bash
install -m 600 .env.example .env
```

The zero-config run proves the local HealthMes infrastructure. Live wearable
data, the Hermes agent and external channels require their respective
credentials and the following steps.

### 2. Configure and chat from the terminal

Set one supported model/provider credential in `.env`, then render the
HealthMes MCP servers, skills and briefing jobs into a Hermes home:

```bash
uv run python scripts/bootstrap.py --dry-run
uv run python scripts/bootstrap.py

cd vendor/hermes-agent
HERMES_HOME=~/.hermes \
UV_PROJECT_ENVIRONMENT=../../data/hermes-venv \
uv run --frozen --no-dev --extra messaging hermes
```

The terminal agent uses the same HealthMes tools and skills as messaging
channels. This is the shortest path to validate the agent before creating a
bot.

### 3. Connect Discord or Telegram through Hermes

From `vendor/hermes-agent/`, run the interactive Hermes gateway setup against
the same home, choose Discord or Telegram, then start the gateway:

```bash
HERMES_HOME=~/.hermes \
UV_PROJECT_ENVIRONMENT=../../data/hermes-venv \
uv run --frozen --no-dev --extra messaging hermes setup gateway

HERMES_HOME=~/.hermes \
UV_PROJECT_ENVIRONMENT=../../data/hermes-venv \
uv run --frozen --no-dev --extra messaging hermes gateway run
```

For Discord, enable Message Content Intent, configure an explicit allowlist,
invite the bot, and use `/sethome` in the delivery channel. Hermes can then
chat through Discord with the HealthMes MCP tools and skills.

**Current setup UX is not yet one command.** The core service and terminal
agent are usable from the terminal today, and Hermes can independently connect
Discord. However, HealthMes bootstrap still renders proactive webhook alerts,
scheduled briefings and live approval proofs for Telegram. Discord is therefore
a working interactive agent channel, but it is not yet a complete replacement
for Telegram's proactive HealthMes flow. The exact setup and limitation matrix
is in [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md#discord-via-hermes-current-boundary).

Run the offline suite and stop ephemeral services when finished:

```bash
make mac-test
make mac-services-stop
```

### Docker alternative

```bash
install -m 600 .env.example .env
install -m 600 config/open-wearables.env.example config/open-wearables.env
docker compose up -d --build     # postgres, redis, open-wearables (+worker,
                                 # +ow-beat, +mcp), healthmes, hermes gateway
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
agent's decisions. See [`docs/EXTENDING.md`](docs/EXTENDING.md), the Korean
expert onboarding guide
[`docs/EXPERT-ONBOARDING.ko.md`](docs/EXPERT-ONBOARDING.ko.md) (metric
catalog, skill authoring, real-device QA protocol), and
[`CONTRIBUTING.md`](CONTRIBUTING.md). Proposals go through the
`Metric proposal` / `Skill proposal` issue forms.

### Backups

```bash
export HEALTHMES_BACKUP_PASSPHRASE='...'   # or set it in .env
uv run healthmes backup create             # age-encrypted snapshot
uv run healthmes backup list
uv run healthmes backup restore <name>     # dry-run; add --yes to apply
```

With `HEALTHMES_VAULT_*` configured (S3-compatible bucket — AWS/R2/MinIO),
snapshots can also replicate off-machine as ciphertext only:

```bash
uv run healthmes backup push <name>              # replicate one snapshot
uv run healthmes backup create --provider remote # create + replicate
export HEALTHMES_BACKUP_PROVIDER=remote_vault    # weekly job replicates too
```

## Repository layout

- `healthmes/` — the glue service: `store/`, `engine/`, `calendars/`,
  `mcp_server/`, `api/`, `backup/`
- `skills/` — Hermes skills
- `apps/` — native companions: `android-usage/` (usage collector + phone/
  Wear OS apps), `ios-companion/` (iOS/watchOS), `macos-companion/` (menu
  bar + widgets + screensaver), `windows-companion/` (tray + screensaver,
  .NET 8)
- `config/`, `scripts/`, `alembic/`, `tests/`, `docs/`
- `vendor/hermes-agent/`, `vendor/open-wearables/` — pinned upstream
  snapshots; extension points first, disciplined patches when necessary

Developer guide (run paths, credentials, tests, CI, vendor sync):
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

Activity telemetry architecture, cross-domain moat, HealthMes-owned decision
agent architecture, and the current runtime-independent compatibility contract:
[`docs/ACTIVITY-WELLNESS-MVP.ko.md`](docs/ACTIVITY-WELLNESS-MVP.ko.md),
[`docs/MOAT-CROSS-DOMAIN-WELLNESS-CONTEXT.ko.md`](docs/MOAT-CROSS-DOMAIN-WELLNESS-CONTEXT.ko.md),
[`docs/HEALTHMES-DECISION-AGENT-ARCHITECTURE.ko.md`](docs/HEALTHMES-DECISION-AGENT-ARCHITECTURE.ko.md),
[`docs/contracts/HEALTHMES-ACTIVITY-WELLNESS-SKILL.ko.md`](docs/contracts/HEALTHMES-ACTIVITY-WELLNESS-SKILL.ko.md).

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

This is a custom source-available, non-commercial license; it is not AGPL/GPL
and should not be described as an OSI-approved open-source license.

This repository includes code derived from Hermes Agent by Nous Research and
open-wearables by Momentum, both released under the MIT License, and vendors
the Mermaid diagram library (MIT). Original notices are preserved in
`THIRD_PARTY_NOTICES.md`.
