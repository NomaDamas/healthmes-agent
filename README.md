# HealthMes Agent

HealthMes Agent is a **proactive, health-aware personal assistant**: it reads
your wearable data (11 providers via open-wearables), your calendar and your
app usage, estimates your cognitive energy hour by hour, plans your week
around it, and surfaces proactive alerts through a HealthMes-owned delivery
stream when something needs to change — retained decisions are explorable as
flowcharts in the browser.

It is glue around two unmodified vendored upstreams:

- `vendor/hermes-agent/` — agent runtime (skills, memory, cron, Telegram
  gateway, MCP client, Claude API)
- `vendor/open-wearables/` — wearable data plane (Garmin/Oura/Fitbit/Whoop/
  Polar/Suunto/Ultrahuman/Strava/Apple/Google/Samsung; sleep/stress/HRV
  scores; FastAPI + Postgres + Celery; its own MCP server)

Everything HealthMes adds lives at the repo root (`healthmes/`, `skills/`,
`config/`, `scripts/`, `apps/`), talking to the vendors only over their
public contracts (REST, MCP, rendered config, and bounded delivery
interfaces). Architecture and rationale: [`docs/PLAN.md`](docs/PLAN.md).

Canonical PR #138 target:

```text
App / Web / channel adapter / proactive trigger
        |
        v
HealthMes decision ingress + product contract
        |
        v
Hermes /v1/responses autonomous LLM/tool loop
        |
        | one filtered product MCP server
        v
HealthMes MCP
        |
        +-- Activity / Nutrition / Calendar
        +-- Wearable adapter --REST--> open-wearables data plane
        |
        v
source validation + conditional compact decision record
```

PR #138 implements this runtime boundary. Exact capability limits, external
Apple prerequisites, and verification criteria are tracked in
[`docs/HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md`](docs/HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md).

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
  schedule changes, deadline risk) routes wellness reasoning through the same
  internal `HealthMesDecisionService` ingress as interactive questions.
  Completed results are placed in the durable alert/native-delivery path; the
  retired parallel reasoning route is no longer shipped.
  Alert hygiene is already built in: per-rule cooldown, daily budget, quiet
  hours, dedup keys, and per-rule crash isolation.
- Hermes decision bootstrap (`scripts/bootstrap.py`): renders and attests an
  isolated `$HERMES_HOME/decision` profile for the single Responses-based
  wellness reasoning path. The profile requires in-place compression so one
  request keeps a stable session ID for exact cleanup. Bootstrap no longer
  installs HealthMes reasoning into the general Hermes home; during migration
  it removes only legacy HealthMes-owned cron reasoning jobs and preserves all
  user or otherwise unowned cron jobs.

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
  `create_medical_record`, `list_medical_records` — all returning interpreted
  deltas with confidence/coverage, honest `insufficient_data` when signals
  are thin. The generic MCP decision writer was removed: only the finalizer
  may persist free-form wellness decisions, while bounded command workflows
  retain their own audit writes.
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
- Decision viewer: proactive actions, actionable risks, and explicit tracking
  that pass finalization may produce a compact `decision_record` rendered as
  a Mermaid flowchart at `/decisions/{id}` (vendored Mermaid, no CDN), with a
  paginated index at `/decisions`. Routine lookup/capture responses do not
  create one.
- Insights: template-based aggregations only (no freeform mining), including
  the focus template ("14–16h focus drop: sleep deficit + Slack 9
  launches/hour").
- Activity Wellness MVP (`healthmes/activity/`): Android's Kotlin collector
  reads `UsageStatsManager`, uploads ordered provisional/final hourly
  snapshots, and stores them in the same `WellnessEvent` data plane used by
  other wellness inputs. Desktop ActivityWatch data can be imported through
  the bounded localhost adapter and scheduled through the HealthMes engine.
  The iOS companion now has Screen Time report contracts, a Keychain-derived
  stable collector identity, source-side privacy exclusions, and one
  single-flight/bounded-outbox pipeline used after authorization, on
  foreground activation, pairing/configuration changes, and Screen Time
  background refresh. Critical authorization/configuration/timezone changes
  queue one fresh rerun; background expiration cancels the service pipeline
  only when no foreground waiter shares it. The backup-excluded retry outbox
  is capped at 8 entries/16 MiB and expires after 14 days across restart and
  offline retry.
  The normal build selects an unavailable adapter. The explicit
  `HealthMesCompanionScreenTimeOptIn` build probes the selected SDK and
  compiles the real collector only when Apple's App & Website Usage export
  symbols are available; unsupported SDKs fail closed without inventing zero
  usage. Production collection still requires a supporting SDK/OS, Apple
  capability approval, matching signed provisioning, device-UI opt-in, and
  real-iPhone validation. Retention, deletion,
  cross-device-aware hourly/daily aggregation, focus/overwork/recovery
  context, REST and MCP surfaces are implemented. The fixed `question_kind`
  resolver remains compatibility-only. The product has one free-form
  reasoning path: `POST /v1/wellness-decisions` delegates one complete
  autonomous turn to Hermes `/v1/responses`; Hermes selects only the six
  read-only decision tools exposed by the unified HealthMes MCP server.
  HealthMes validates the canonical tool trace and returned source
  references, then persists a compact DecisionRecord only for actions,
  actionable risks, or explicit tracking. The retired split-runtime adapter
  and public builder have been removed.
  See `docs/HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md`. The authenticated
  REST boundary exposes
  per-domain `activity` / `nutrition` / `wearable` / `calendar` consent,
  explicit local-versus-hosted execution scope, bounded request admission,
  and cancellation-safe shutdown that drains accepted finalization before
  MCP or database teardown. Finalization has a configurable total deadline
  (`HEALTHMES_DECISION_FINALIZATION_TIMEOUT_SECONDS`, default 5 seconds)
  across policy lookup, process/SQLite/PostgreSQL locks, source revalidation,
  payload construction, flush, and the final pre-commit boundary. A timeout
  before commit begins fails closed as an auditable HTTP 503 and permanently
  fences late writes. If the deadline expires after commit begins, HealthMes
  returns HTTP 202 with `persistence_status=unknown` and a request-ID recovery
  location rather than guessing whether the database committed. The finalizer
  keeps tracking that irreversible commit worker, and application shutdown
  drains its session/connection cleanup before database disposal. Code that
  owns the response only publishes committed success when session cleanup,
  worker-capacity release, and active-worker removal all finish before the
  deadline; a later completion remains HTTP 202 and is recovered by request ID.
  Code that owns a `DecisionFinalizer` directly must likewise call `close()` or
  await `aclose()` before disposing its session-factory engine. `aclose()`
  defers caller cancellation until accepted workers have released those
  resources. Every context tool call re-reads domain consent before and after
  provider work,
  including revision changes that briefly disable and re-enable a domain.
  Calendar poll, sleep scheduler, and sleep web paths cache backends only for
  the current credential generation under the shared connection fence, so a
  completed disconnect cannot leave a stale client doing remote work.
  Migrations refuse to discard disabled domain-consent choices.
- Unified input control plane (`healthmes/inputs/`): desktop and future mobile
  settings UIs can enumerate Android, ActivityWatch, iPhone Screen Time,
  nutrition capture, the raw-first HealthKit bridge, Open Wearables, Google
  Calendar, and iCloud Calendar through `GET /v1/inputs`.
  `PUT /v1/inputs/{source_id}/settings` composes the existing per-device
  collection controls for activity collectors, per-domain Decision Agent
  consent, and per-data-class `1d/7d/14d/30d/90d/forever` retention policies
  without creating a second settings store. New multi-platform desktop
  instances persist the caller-supplied platform and reject later platform
  conflicts instead of discarding the field. Other inputs expose only the
  connection and sync actions their real adapters enforce; the API does not
  invent a generic enable switch. The UI contract and scope rules are in
  `docs/INPUT-CONTROL-PLANE.ko.md`.

**Medical-lite & backups (Phase 3)**
- UI-neutral capture commands accept food, medication, and symptom records
  with media paths, transcripts, and a capture-time health snapshot.
  `healthmes-capture` documents how a bounded channel workflow may map
  photos/voice to those commands, but PR #138 does not install a Telegram or
  device inbound adapter. Routine capture does not create a DecisionRecord.
  `doctor-visit-summary` assembles a local briefing file without creating one.
- Local-first encrypted partial backups (`healthmes/backup/`): versioned
  snapshot envelope (healthmes DB dump, raw ingest, optional open-wearables
  dump, media tree, optional Hermes state) → tar → age encryption
  (passphrase). It does not currently include `.env`, external OAuth
  credentials, or an Open Wearables DB unless its dump URL is configured, so
  it is not yet a complete Personal Data Node disaster-recovery image.
  When Open Wearables runtime access is configured without
  `HEALTHMES_OW_DATABASE_URL`, creation still succeeds but emits an explicit
  partial-backup warning; the manifest and storage status report that the
  Open Wearables database is not recoverable from that snapshot.
  `healthmes backup
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
  APNs/FCM relay. The durable `/v1/alerts` stream is the current product-owned
  delivery surface; guaranteed real-time push is future work. All
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
only** — no third-party SDKs, no analytics, and no push relay (polling only).
Visuals stay
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

**Skills** (`skills/`, exposed through the reviewed HealthMes catalog or used
by separate bounded command workflows; bootstrap does not install them into
the isolated decision profile):
`healthmes-planner` (goal dump → task breakdown → energy-aware block
proposals → finalizer-classified result), `healthmes-capture` (food + medical),
`healthmes-caffeine` (exact event + current sleep + explicit user bounds →
read-only bounded preparation proposal),
`healthmes-nutrition` (photo observation review + caffeine confirmation),
`healthmes-sleep` (sleep/readiness evidence → cautious daily intensity
decision), `healthmes-stress` (source-aware stress/recovery evidence →
keep/reconsider/insufficient-data decision), `doctor-visit-summary`.

## Quickstart (mac-native, primary path)

Requires [uv](https://docs.astral.sh/uv/) and Homebrew; everything is
repo-local without `brew services`; `scripts/healthmes_local.sh install`
registers a per-user macOS LaunchAgent so the stack starts at login and is
kept alive, including the Open Wearables worker and periodic sync scheduler.

```bash
make mac-setup            # brew postgresql@16 + redis if missing, initdb,
                          # create DBs, uv sync
install -m 600 .env.example .env  # optional: sqlite works with zero config
make mac-run              # alembic upgrade head + service on :8100
curl http://localhost:8100/health
make mac-test             # full offline test suite
make mac-services-stop    # stop the ephemeral postgres + redis
```

With no `.env` at all, the service runs against a repo-local sqlite file —
`make mac-run` alone is a working single-process demo. The full experience
(wellness reasoning + wearable syncs) needs the credentials matrix in
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) and
`uv run python scripts/bootstrap.py` for the Hermes side.

### Docker alternative

Before bootstrap, set `HEALTHMES_API_TOKEN`,
`HEALTHMES_DECISION_HERMES_MODEL`,
`HEALTHMES_DECISION_HERMES_PROVIDER`, and the matching provider API key in
`.env`. Bootstrap requires both decision model variables and writes the
dedicated runtime profile used by Compose.

```bash
install -m 600 .env.example .env
install -m 600 config/open-wearables.env.example config/open-wearables.env
uv run python scripts/bootstrap.py --mode docker
docker compose --profile decision up -d --build
```

The `decision` profile starts the HealthMes-owned `hermes-decision`
supervisor in addition to postgres, redis, Open Wearables, and HealthMes.
The core Open Wearables services include `ow-beat`, the Celery Beat process
that schedules periodic provider syncs.
Omit that profile only when intentionally running the core data/MCP stack
without wellness reasoning.

Set `HEALTHMES_TIMEZONE` (e.g. `Asia/Seoul`) in `.env` for the compose path —
container clocks are UTC. The compose path also **requires**
`HEALTHMES_API_TOKEN` (the container binds 0.0.0.0 and publishes the port;
the service refuses to start unauthenticated on a non-loopback bind).

### Runtime diagnostics & choosing your LLM

Product wellness questions must use `POST /v1/wellness-decisions`; this is
the single HealthMes ingress that owns source validation and finalization.
Each POST requires a stable `Idempotency-Key` for that logical request;
retries reuse the key, while different input under the same key is rejected.
HealthMes starts its core `/health` and `/mcp` surfaces before the optional
Hermes decision runtime. The first decision lazily validates the runtime and
retries verification on a later request after a transient failure.
The vendor `hermes` / `hermes chat` CLI remains available for isolated Hermes
runtime diagnostics, but calling it directly is not an equivalent HealthMes
wellness path and must not be used for product QA. See
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md). The model can be selected with
`HEALTHMES_DECISION_HERMES_MODEL` /
`HEALTHMES_DECISION_HERMES_PROVIDER` in `.env`; HealthMes keeps the product
contract provider-agnostic.

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
- `vendor/hermes-agent/`, `vendor/open-wearables/` — read-only upstreams,
  never modified

Developer guide (run paths, credentials, tests, CI, vendor sync):
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

Activity telemetry architecture, cross-domain moat, canonical single wellness
runtime, and the current runtime-independent compatibility contract:
[`docs/ACTIVITY-WELLNESS-MVP.ko.md`](docs/ACTIVITY-WELLNESS-MVP.ko.md),
[`docs/MOAT-CROSS-DOMAIN-WELLNESS-CONTEXT.ko.md`](docs/MOAT-CROSS-DOMAIN-WELLNESS-CONTEXT.ko.md),
[`docs/HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md`](docs/HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md),
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

This repository includes code derived from Hermes Agent by Nous Research and
open-wearables by Momentum, both released under the MIT License, and vendors
the Mermaid diagram library (MIT). Original notices are preserved in
`THIRD_PARTY_NOTICES.md`.
