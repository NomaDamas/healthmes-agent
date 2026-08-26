# 🧠 HealthMes Agent

**Open, local-first infrastructure for wellness agents.**

HealthMes turns wearable, activity, nutrition, calendar, environment, and
subjective signals into explainable decisions and practical next steps. It
gives the same wellness context to a web workspace, iPhone, Apple Watch,
macOS, Android, Wear OS, Windows, and Telegram without handing your data to a
third-party relay.

> HealthMes is personal software, not a medical device and not a substitute
> for professional medical advice.

## 🧭 Index

- [What is HealthMes?](#-what-is-healthmes)
- [Everything That Is Built](#-everything-that-is-built)
- [Product Gallery](#-product-gallery)
- [Quick Start](#-quick-start)
- [Choose Your Path](#-choose-your-path)
- [Companion Apps](#-companion-apps)
- [How It Works](#-how-it-works)
- [Data Sources and Integrations](#-data-sources-and-integrations)
- [Security and Privacy](#-security-and-privacy)
- [Build Matrix](#-build-matrix)
- [Documentation](#-documentation)

## 🌿 What is HealthMes?

HealthMes is the glue plane between personal data and wellness automation:

```text
signals → normalized context → explainable decision → user action → outcome
```

The system combines:

- **Context:** wearable recovery, sleep, HRV, stress, body battery, activity,
  nutrition, calendar load, environment, app fragmentation, and manual
  captures.
- **Reasoning:** deterministic energy and trigger engines plus one canonical
  free-form reasoning path through the Hermes runtime.
- **Action:** bounded proposals, exact Yes/No/Speak actions, schedule changes,
  capture flows, reports, and proactive notifications.
- **Inspection:** decision records, evidence, confidence, coverage, and local
  Mermaid flowcharts instead of opaque recommendations.

## ✅ Everything That Is Built

| Surface | Implemented product experience |
|---|---|
| 🌐 Web workspace | Integrated dashboard with overview, calendar, insights, decisions, agent channels, setup, input controls, reports, and decision flowchart viewer |
| 🧠 Decision runtime | `POST /v1/wellness-decisions` → `HealthMesDecisionService` → Hermes `/v1/responses` → filtered HealthMes MCP tools |
| 📱 iPhone | Full wellness canvas, Today/Plan/Explore/Settings flows, voice and editable-text dock, calendar blocks, capture, reports, bounded decisions, notifications, widgets, and Live Activities |
| ⌚ Apple Watch | iPhone-paired compact decision remote, notification actions, spoken-command relay, watch app, and complications |
| 🖥️ macOS | Full SwiftUI workspace, local threads, sidebar and inspector, menu bar glance/popover, bounded voice/text commands, widgets, notifications, and ambient screensaver |
| 🤖 Android | Compose companion with Home, Report, Capture, Proposals, Settings, real notification actions, ongoing focus block, and an ETag-aware widget |
| ⌚ Wear OS | Standalone pairing activity, cache-first ProtoLayout briefing tile, energy complication, and phone notification bridge |
| 🪟 Windows | .NET 8 tray/flyout, toast actions, privacy-aware `.scr` screensaver, DPAPI pairing, and Widgets Board integration point |
| ✈️ Telegram + Hermes | Guaranteed-delivery channel, proactive briefings, capture skill, cron scheduling, and agent chat |
| 💾 Storage | Local SQLite or PostgreSQL, Redis-backed runtime paths, retention controls, age-encrypted backups, and optional ciphertext-only remote vault |

### One Canonical Decision Path

The native clients and web UI consume the same decision contract. The reasoning
runtime exposes six read-oriented tools:

```text
POST /v1/wellness-decisions
        ↓
HealthMesDecisionService
        ↓
Hermes /v1/responses
        ↓
Filtered HealthMes MCP
        ├─ search_activity
        ├─ search_nutrition
        ├─ search_calendar
        ├─ search_wearable
        ├─ list_wellness_skills
        └─ read_wellness_skill
```

Every response can carry evidence, confidence, coverage, and
`insufficient_data` rather than inventing certainty.

## 🖼️ Product Gallery

The repository includes representative UI evidence for the unified product:

| Web | iPhone |
|---|---|
| ![HealthMes web dashboard](artifacts/apple-unified-dashboard/web-dashboard.png) | ![HealthMes iPhone Today](artifacts/apple-unified-dashboard/iphone-today.png) |

| macOS | Apple Watch |
|---|---|
| ![HealthMes macOS dashboard](artifacts/apple-unified-dashboard/macos-dashboard.png) | ![HealthMes Apple Watch remote](artifacts/apple-unified-dashboard/watch-42mm.png) |

## ⚡ Quick Start

The fastest path starts a local SQLite-backed service with no PostgreSQL,
Redis, wearable credentials, or Telegram configuration.

### 1. Install and start

Requirements: Python 3.12+ and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
make mac-run
```

### 2. Verify the service

In a second terminal:

```bash
curl http://localhost:8100/health
```

Expected:

```json
{"status":"ok"}
```

### 3. Open the workspace

| URL | Use |
|---|---|
| `http://localhost:8100/dashboard` | Main HealthMes workspace |
| `http://localhost:8100/docs` | OpenAPI documentation |
| `http://localhost:8100/decisions` | Decision history and flowcharts |
| `http://localhost:8100/reports/weekly` | Human-readable weekly report |
| `http://localhost:8100/reports/weekly.json` | Machine-readable weekly report |
| `http://localhost:8100/connect` | Calendar and integration connection |

Stop the service with `Ctrl-C`.

### 4. Try the API

```bash
curl http://localhost:8100/v1/briefing/glance
curl http://localhost:8100/v1/alerts
curl http://localhost:8100/v1/setup/readiness
```

The same glance, alerts, and weekly-report contracts are used by the native
companion apps.

## 🧩 Choose Your Path

| Goal | Start here | Result |
|---|---|---|
| Explore locally | `uv sync && make mac-run` | Credential-free SQLite demo |
| Use the full Mac-native stack | `make mac-setup` | PostgreSQL, Redis, migrations, and service |
| Run everything in containers | [Docker Compose](#-docker-compose) | HealthMes, open-wearables, Redis, PostgreSQL, and Hermes |
| Add wearable data | [Development guide](docs/DEVELOPMENT.md) | Provider OAuth, sync workers, and credentials |
| Install a companion | [Companion Apps](#-companion-apps) | Pair an iPhone, Mac, Android, Watch, Wear OS, or Windows client |
| Receive proactive messages | [Telegram and Hermes](#telegram-and-hermes) | Briefings, capture, and agent chat |

## 🛠️ Full Mac-Native Setup

The macOS path is the primary local development path. It installs PostgreSQL
16 and Redis with Homebrew without registering global services. Runtime data
stays under `./data/`.

```bash
make mac-setup
install -m 600 .env.example .env
make mac-run
```

Useful commands:

```bash
make mac-services-status
make mac-services-stop
make mac-test
make mac-ow
make mac-ow-worker
make mac-ow-beat
```

The complete wearable path needs PostgreSQL and multiple long-running
processes. Provider OAuth and dogfooding instructions are in
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

## 🐳 Docker Compose

```bash
install -m 600 .env.example .env
install -m 600 config/open-wearables.env.example config/open-wearables.env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put the generated token in `.env` before starting:

```dotenv
HEALTHMES_API_TOKEN=paste-a-new-token-here
HEALTHMES_TIMEZONE=Asia/Seoul
```

HealthMes binds to all interfaces in Compose and refuses to start without a
bearer token.

```bash
docker compose up -d --build
curl http://localhost:8100/health
docker compose ps
docker compose logs -f healthmes
docker compose down
```

For Hermes credentials and the full compose walkthrough, see
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md#full-stack-docker-compose-alternative-path).

## 📲 Companion Apps

All native clients pair with your own HealthMes instance and share the same
bearer-authenticated APIs. They use ETag/304 caching and pinned fixtures so
glance surfaces remain consistent across platforms.

### 🍎 iPhone and Apple Watch

Source: [`apps/ios-companion/`](apps/ios-companion/)

- iPhone wellness canvas with Today, Plan, Explore, Settings, calendar
  blocks, goals, tasks, reports, capture, and decision details.
- Voice and editable-text command dock for bounded wellness actions.
- Local notifications from `/v1/alerts` with exact `proposal_id` routing and
  **Yes / No / Speak** actions.
- Decision Live Activity with action buttons and explicit expiration states.
- Focus-block Live Activity with timer and progress.
- Home and Lock Screen widgets for energy, next blocks, and alerts.
- Apple Watch remote paired through WatchConnectivity, with compact decision
  actions, notification actions, spoken-command relay, app, and complication.
- First-party HealthKit collector on iPhone with a pairing-scoped encrypted
  outbox, stable `Idempotency-Key`, retry, durable hash/size ACK, and anchor
  advancement only after accepted forwarding state.
- Real device pairing requires HTTPS. Loopback HTTP is for local development
  only.

### 🖥️ macOS

Source: [`apps/macos-companion/`](apps/macos-companion/)

- Full SwiftUI workspace with sidebar, channels, local workspace threads,
  inspector, calendar, goal, task, decision, and report views.
- Bounded voice/text commands and local workspace state.
- `HealthMesMac` main app plus menu bar glance/popover.
- `HealthMesMacWidgets` WidgetKit extension.
- `HealthMesSaver` `.saver` ambient briefing with a privacy toggle that
  removes health-derived values while retaining schedule and freshness.
- Notification manager polls `/v1/alerts`, follows the proposal grammar, and
  supports exact Yes/No actions with outcome handling.
- Setup can install/manage a local runtime and generate a one-time QR pairing
  code.

### 🤖 Android and Wear OS

Source: [`apps/android-usage/`](apps/android-usage/)

- `:companion`: Jetpack Compose app with Home, Report, Capture, Proposals, and
  Settings.
- Home energy score, energy curve, next blocks, alerts, and decision links.
- Camera/photo/voice capture flows.
- Real notification buttons: **Apply / Adjust / Keep**, routed to the exact
  proposal endpoint with explicit 409 already-resolved handling.
- Ongoing focus-block notification with countdown, bridged from phone to Wear.
- Glance widget with a 15-minute ETag-aware refresh.
- `:wear`: standalone pairing activity, cache-first ProtoLayout briefing tile,
  energy complication, and compact wearable briefing.
- `:app`: Android UsageStats collector with encrypted pairing, collection
  generation, privacy boundary, and HTTPS-only telemetry origin.

### 🪟 Windows

Source: [`apps/windows-companion/`](apps/windows-companion/)

- .NET 8 solution with portable `HealthMes.Glance.Core`.
- Tray icon, flyout briefing, toast notifications, and proposal actions.
- `.scr` screensaver supporting `/s`, `/p`, and `/c` modes.
- Privacy toggle for removing health-derived values from ambient surfaces.
- DPAPI-backed pairing and secure local configuration.
- Widgets Board provider integration point is present; packaging/signing is
  intentionally deferred because it requires an MSIX/signing environment.

### ✈️ Telegram and Hermes

HealthMes remains paired to your local instance while Hermes provides the
agent runtime, skills, memory, cron, Telegram gateway, and MCP client.

```bash
uv run python scripts/bootstrap.py --dry-run
uv run python scripts/bootstrap.py
```

Bootstrap renders Hermes configuration outside `vendor/`, installs HealthMes
skills, and registers morning, evening, and weekly briefings.

For terminal chat without Telegram:

```bash
cd vendor/hermes-agent
HERMES_HOME=~/.hermes \
  UV_PROJECT_ENVIRONMENT=../../data/hermes-venv \
  uv run --frozen --no-dev --extra messaging hermes
```

Telegram is the guaranteed-delivery channel. Native notifications are
polling- and OS-budgeted companions, not a replacement for Telegram delivery.

## 🔌 How It Works

```text
┌──────────────────────────────────────────────────────────────────────┐
│ Inputs                                                               │
│ HealthKit · wearables · UsageStats · calendar · nutrition · capture  │
└──────────────────────────────┬───────────────────────────────────────┘
                               │ normalize, store, retain
┌──────────────────────────────▼───────────────────────────────────────┐
│ HealthMes local service                                              │
│ energy engines · triggers · goals/tasks · alerts · reports · MCP     │
└───────────────┬──────────────────────────────┬───────────────────────┘
                │ REST / JSON                  │ MCP / responses
┌───────────────▼───────────────┐  ┌───────────▼───────────────────────┐
│ Web + native clients           │  │ Hermes agent runtime              │
│ dashboard · iPhone · Watch    │  │ skills · memory · cron · Telegram  │
│ macOS · Android · Wear · Win  │  └─────────────────────────────────────┘
└───────────────┬───────────────┘
                │ bounded action with exact proposal identity
┌───────────────▼──────────────────────────────────────────────────────┐
│ User-visible outcome                                                 │
│ decision viewer · notification action · calendar proposal · report    │
└───────────────────────────────────────────────────────────────────────┘
```

HealthMes-owned code lives at the repository root and communicates with two
unmodified vendored upstreams through documented contracts:

- `vendor/open-wearables/`: wearable data plane and provider integrations.
- `vendor/hermes-agent/`: agent runtime, skills, memory, cron, Telegram, and
  MCP client.

Do not modify either vendored tree in HealthMes tasks.

## 🧺 Data Sources and Integrations

- Wearable recovery, sleep, activity, HRV, stress, and body-battery data
  through `open-wearables`.
- First-party Apple HealthKit ingestion with encrypted delivery buffering.
- Android UsageStats for app-fragmentation and focus context.
- Google Calendar and iCloud CalDAV connection flows.
- Nutrition, medication, symptom, and subjective capture.
- Environment and schedule context.
- Input control plane at `/v1/inputs`, `/v1/inputs/{source_id}`, and
  `PUT /v1/inputs/{source_id}/settings`.
- Settings hub at `/v1/settings/hub` and setup readiness at
  `/v1/setup/readiness`.
- Strong ETag and `If-Match` concurrency controls for mutable settings and
  proposals.

Provider credentials, OAuth setup, and integration-specific requirements are
documented in [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

## 🔒 Security and Privacy

- Local-first by default: no analytics, no third-party relay, and no hosted
  wellness profile required.
- Non-loopback network binds require a bearer token.
- Native secrets use platform storage: Keychain, DPAPI, or
  EncryptedSharedPreferences.
- Real iPhone pairing requires HTTPS.
- HealthKit forwarding uses pairing-scoped encryption, stable idempotency, and
  durable acknowledgements.
- Local backups are age-encrypted; remote replication stores ciphertext only
  in S3-compatible storage such as S3, R2, or MinIO.
- Calendar mutations and wellness proposals stop at explicit confirmation
  boundaries.
- Never commit `.env`, OAuth client secrets, bearer tokens, refresh tokens, or
  raw health payloads.

Create and manage backups with:

```bash
export HEALTHMES_BACKUP_PASSPHRASE='use-a-password-manager'
uv run healthmes backup create
uv run healthmes backup list
uv run healthmes backup restore <name>       # dry-run
uv run healthmes backup restore <name> --yes # apply
```

Read [`docs/BACKUP.md`](docs/BACKUP.md) before enabling remote replication.
Losing the passphrase means losing access to the encrypted backups.

## 🧪 Build Matrix

| Surface | Implementation | Verification status |
|---|---|---|
| Web and API | Python, FastAPI, local store, MCP | Offline unit, API, contract, and integration coverage |
| iPhone and Watch | SwiftUI, HealthKit, WatchConnectivity, WidgetKit, ActivityKit | Build and contract coverage; real hardware, signing, background-budget, and notification QA remain device-specific |
| macOS | SwiftUI, WidgetKit, UserNotifications, screensaver target | Build and contract coverage; signing and OS behavior remain device-specific |
| Android and Wear | Kotlin, Compose, Glance, ProtoLayout, UsageStats | Build and fixture/contract coverage; hardware and OS-budget QA remain device-specific |
| Windows | .NET 8, WinUI/tray/toasts, screensaver | Build and core coverage; MSIX/signing and Widgets Board packaging remain environment-specific |
| Telegram and Hermes | Hermes runtime, HealthMes skills, MCP | Bootstrap and contract coverage; external provider behavior depends on configured credentials |

The product surfaces above are implemented. Hardware, signing, OS
notification delivery, background execution budgets, and some Apple Screen
Time capability paths still require device-specific QA.

## 📚 Documentation

| Need | Document |
|---|---|
| Architecture and product rationale | [`docs/PLAN.md`](docs/PLAN.md) |
| Wellness runtime architecture | [`docs/HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md`](docs/HEALTHMES-WELLNESS-RUNTIME-ARCHITECTURE.ko.md) |
| Apple main integration | [`docs/APPLE-MAIN-INTEGRATION.ko.md`](docs/APPLE-MAIN-INTEGRATION.ko.md) |
| Development, credentials, integrations, and tests | [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) |
| Extend metrics, skills, or insight templates | [`docs/EXTENDING.md`](docs/EXTENDING.md) |
| Backup format and remote vault | [`docs/BACKUP.md`](docs/BACKUP.md) |
| Storage architecture | [`docs/STORAGE-ARCHITECTURE.ko.md`](docs/STORAGE-ARCHITECTURE.ko.md) |
| Healthcare expert onboarding | [`docs/EXPERT-ONBOARDING.ko.md`](docs/EXPERT-ONBOARDING.ko.md) |
| iPhone and Watch companion | [`apps/ios-companion/README.md`](apps/ios-companion/README.md) |
| macOS companion | [`apps/macos-companion/README.md`](apps/macos-companion/README.md) |
| Android and Wear companion | [`apps/android-usage/README.md`](apps/android-usage/README.md) |
| Windows companion | [`apps/windows-companion/README.md`](apps/windows-companion/README.md) |
| Contribution workflow | [`CONTRIBUTING.md`](CONTRIBUTING.md) |

Main directories:

```text
healthmes/     FastAPI service, store, engines, calendars, MCP, backups
skills/        HealthMes skills copied into the Hermes home
apps/          Android, iOS, macOS, and Windows companions
config/        Root-owned integration configuration
scripts/       Bootstrap, local development, sync, and maintenance tools
alembic/       HealthMes database migrations
tests/         Offline unit, API, contract, and integration tests
vendor/        Read-only upstream snapshots
```

## License

HealthMes Agent is available for non-commercial use under the project license
in [`LICENSE`](LICENSE). Commercial use requires a separate written license
from the project owner.

This repository includes code derived from Hermes Agent by Nous Research and
open-wearables by Momentum, plus the Mermaid library. Original notices are
preserved in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
