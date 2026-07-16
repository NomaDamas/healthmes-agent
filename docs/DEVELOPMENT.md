# Development Guide

HealthMes Agent glue code lives at the repo root (`healthmes/`, `config/`,
`scripts/`, `skills/`, `tests/`). `vendor/` contains read-only upstream
snapshots (`hermes-agent`, `open-wearables`) — **never modify anything under
`vendor/`**; all integration happens via config rendered outside the vendor
trees, the root `Makefile`/`scripts/`, and the root `docker-compose.yml`.
Architecture: `docs/PLAN.md`.

There are two run paths. **Mac-native is the primary one** (this stack is
developed and run directly on macOS); docker compose is the alternative for
a full one-command stack.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (manages Python 3.12 automatically via
  `.python-version` — no system Python needed). The vendored open-wearables
  backend targets Python 3.13 and asks for uv >= 0.9.17; uv downloads the
  toolchain on demand.
- Homebrew (mac-native path — installs `postgresql@16` + `redis` on demand)
- Docker + Docker Compose v2.24+ (only for the compose path)

## Mac-native quickstart (primary)

```bash
make mac-setup            # brew install postgresql@16 + redis (if missing),
                          # initdb ./data/pg, start pg+redis, create the
                          # open-wearables + healthmes databases, uv sync
install -m 600 .env.example .env  # optional: tweak HEALTHMES_* values
make mac-run              # alembic upgrade head + uvicorn :8100
curl http://localhost:8100/health   # -> {"status":"ok"}
make mac-test             # uv run pytest -q
```

The service exposes `/health`, the REST surface under `/v1/*` (including the
companion-app glance briefing at `/v1/briefing/glance`), the cognitive-energy
forecast at `/cognitive-energy/forecast`, the decision viewer at `/decisions`
+ `/decisions/{id}` (Mermaid, served fully locally), the weekly report at
`/reports/weekly` (+ `.json` twin), the read-only calendar-connection status
page at `/connect` (see "캘린더 연결"), and the Layer-B MCP server
(Streamable HTTP) at exactly `/mcp`.

Background jobs (the 10-minute trigger sweep, the hourly cognitive-energy
persist and the weekly encrypted backup) are all registered at startup but
only run when `HEALTHMES_SCHEDULER_ENABLED=true` — keep it off in tests and
one-off tooling.

Everything is ephemeral and repo-local — postgres runs out of `./data/pg`
via `pg_ctl`, redis daemonizes with `./data/redis.pid`, and **nothing is
registered with `brew services`** (no autostart). Stop the services with
`make mac-services-stop`; all targets are idempotent and re-runnable.

| Target | What |
|---|---|
| `make mac-setup` | one-time bootstrap (safe to re-run): brew pkgs, `initdb` `./data/pg`, create `open-wearables` + `healthmes` DBs/roles, `uv sync` |
| `make mac-services-start` | start ephemeral postgres (`pg_ctl`) + redis (pidfile `./data/redis.pid`) |
| `make mac-services-stop` | stop both (the inverse — leaves data dirs intact) |
| `make mac-services-status` | report what is running |
| `make mac-run` | `alembic upgrade head` + healthmes on `HEALTHMES_PORT` (8100) |
| `make mac-test` | `uv run pytest -q` |
| `make mac-ow` | best-effort native boot of the vendored open-wearables backend (see below) |
| `make mac-ow-worker` | its celery worker (**requires redis** from `mac-services-start`) |
| `make compose-config` | validate `docker-compose.yml` without a daemon |

Notes:

- **Zero-setup mode:** with no `.env`, healthmes defaults to a repo-local
  sqlite database (`sqlite:///./data/healthmes.db`) — `make mac-run` works
  without postgres. For the postgres-backed run, uncomment the
  `postgresql+psycopg://...@localhost:5432/healthmes` line in `.env`
  (database/role are created by `mac-setup`).
- **`make mac-ow`** boots `vendor/open-wearables/backend` natively per its
  own README/start scripts: it source-exports `config/open-wearables.env`
  (falling back to the `.example`, localhost defaults), redirects the venv
  to `./data/ow-backend-venv` (vendor tree stays untouched), runs `uv sync`,
  then `scripts/start/app.sh` (migrations + seeds + `fastapi dev` on :8000
  when `ENVIRONMENT=local`). Requires postgres from `mac-services-start`.
  The svix webhook-registration step retries and is non-fatal (no svix
  server in this stack).
- **Celery worker & redis:** provider syncs and score jobs run in the
  celery worker (`make mac-ow-worker`), which needs redis as broker/result
  backend — start it via `make mac-services-start` first. Without the
  worker the OW API still serves, but no background syncs happen.
- postgresql@16 is keg-only; the scripts call binaries via
  `$(brew --prefix postgresql@16)/bin` — no PATH changes needed.

### Oura OAuth dogfooding (mac-native)

Create an OAuth application in the
[Oura Cloud developer portal](https://cloud.ouraring.com/oauth/applications).
The registered redirect URI must match this value exactly:

```text
http://localhost:8000/api/v1/oauth/oura/callback
```

Copy the root example and put the Oura credentials only in the ignored local
file:

```bash
install -m 600 config/open-wearables.env.example config/open-wearables.env
```

```dotenv
OURA_CLIENT_ID=replace-with-client-id
OURA_CLIENT_SECRET=replace-with-client-secret
OURA_DEFAULT_SCOPE="personal daily heartrate workout session spo2 ring_configuration heart_health"
API_BASE_URL=http://localhost:8000
HISTORICAL_SYNC_ON_CONNECT=false
```

`daily` includes Oura's daily sleep, activity, and readiness summaries. Do not
add the legacy separate `activity` scope when the Oura application UI does not
offer it. Never paste the client secret into Git, screenshots, issue comments,
or chat logs. Setting `HISTORICAL_SYNC_ON_CONNECT=false` disables the vendored
grace-period backfill so the explicit historical task below is the single sync
run being verified.

Start the data plane in separate terminals so the API and its background sync
worker are both present:

```bash
make mac-services-start
make mac-ow
```

```bash
make mac-ow-worker
```

Open <http://localhost:8000/docs> and use the OpenAPI operations in this order:

1. Click **Authorize** and enter the developer email and password from the local
   `ADMIN_EMAIL` and `ADMIN_PASSWORD` settings. The OpenAPI UI calls
   `POST /api/v1/auth/login` for the bearer token; do not copy the token into
   notes or logs.
2. Select or create the local test user with `GET /api/v1/users` or
   `POST /api/v1/users`.
3. Call `GET /api/v1/oauth/{provider}/authorize` with `provider=oura` and the
   selected `user_id`, then open the returned `authorization_url`. Complete
   consent in Oura; Oura redirects to the registered concrete callback URI and
   the callback should report a successful connection.
4. Confirm `GET /api/v1/users/{user_id}/connections` contains an `oura`
   connection with `status: active`.
5. Queue `POST /api/v1/providers/{provider}/users/{user_id}/sync/historical`
   with `provider=oura` and `days=90`. A successful request returns
   `success: true` and a task ID.
6. Confirm the corresponding Oura run reaches `status: success` in
   `GET /api/v1/users/{user_id}/sync/runs`. This proves the Celery worker
   completed the historical task, not merely that the API accepted it.
7. Query `GET /api/v1/users/{user_id}/summaries/sleep` and
   `GET /api/v1/users/{user_id}/health-scores?provider=oura&category=readiness`
   with a date range that covers days present in the Oura account. At least one
   non-empty response proves the personal-date sleep/readiness data path.

Record only sanitized evidence:

```text
repo commit: <commit>
Oura connection: active | blocked
sync: success | failed
sleep/readiness data: returned | empty
first blocker: <first failure or none>
screenshot/log: <redacted path or summary>
```

Do not record credentials, bearer/refresh tokens, authorization codes, email
addresses, user IDs, task IDs, or raw health payloads. A `200` from `/docs`
proves only that the API is serving; it does not prove provider sync unless the
Celery task also completes.

## Run the healthmes service directly

```bash
uv sync                              # if you skipped mac-setup
uv run python -m healthmes           # uvicorn on HEALTHMES_PORT (default 8100)
```

For auto-reload during development:

```bash
uv run uvicorn healthmes.app:create_app --factory --reload --port 8100
```

## Agent plane: Hermes bootstrap

The Hermes gateway is configured entirely from outside `vendor/`:
`scripts/bootstrap.py` renders `config/hermes-config.yaml.tmpl` into
`$HERMES_HOME/config.yaml`, copies `skills/` into `$HERMES_HOME/skills/`
(copies, not symlinks — the vendor skill trust check resolves symlinks and
would log a security warning on every skill load; re-runs resync content),
generates a `HEALTHMES_HERMES_WEBHOOK_SECRET` into `.env` when missing,
installs the briefing state-snapshot script
(`scripts/healthmes_briefing_snapshot.py` + a base-URL sidecar) into
`$HERMES_HOME/scripts/`, and registers the three cron briefings (morning
07:00, evening 21:30, weekly Sunday 18:00) in `$HERMES_HOME/cron/jobs.json`
— each with `script:` set so the vendor scheduler pre-injects a compact
state snapshot into the briefing prompt (docs/PLAN.md §4), saving MCP
round-trips at run time. The snapshot also carries the server-built weekly
report link (`weekly_report.url`, token-embedded via
`healthmes.api.reports.weekly_report_url`); the Sunday prompt instructs the
agent to include it verbatim — the agent never constructs viewer URLs.

```bash
uv run python scripts/bootstrap.py --dry-run     # show what would change
uv run python scripts/bootstrap.py               # native run (HERMES_HOME=~/.hermes)
uv run python scripts/bootstrap.py --mode docker # compose paths (HERMES_HOME=./data/hermes)
```

It is idempotent: re-runs deep-merge the config (your manual keys win, one
backup is kept), resync skill copies, and skip already-registered jobs.
`HERMES_HOME`, `TELEGRAM_HOME_CHAT_ID`, and the other inputs come from the
environment or `.env` (see the bootstrap section of `.env.example`).

Running the gateway natively (verified live on macOS with dummy creds):

```bash
cd vendor/hermes-agent && \
  HERMES_HOME=... UV_PROJECT_ENVIRONMENT=../../data/hermes-venv \
  uv run --frozen --no-dev --extra messaging hermes gateway run
```

Two caveats from live verification: (1) if a supervised hermes service is
installed on the machine (launchd), `hermes gateway run` refuses to start
even for a different `HERMES_HOME` — add `--force`; (2) `uv run` inside
`vendor/hermes-agent` drops a `hermes_agent.egg-info/` directory into the
vendor tree (setuptools metadata, ignored by git) — harmless, delete it if
you want the vendor tree pristine; the venv itself stays outside via
`UV_PROJECT_ENVIRONMENT`.

### CLI chat (same agent, no Telegram needed)

The vendor CLI reads the same `$HERMES_HOME` config bootstrap renders, so the
terminal agent has the identical MCP tools and skills as the Telegram bot:

```bash
cd vendor/hermes-agent && \
  HERMES_HOME=~/.hermes UV_PROJECT_ENVIRONMENT=../../data/hermes-venv \
  uv run --frozen --no-dev --extra messaging hermes            # interactive
# one-shot:
#   ... hermes chat -q "How was my sleep this week?"
```

### Choosing the LLM (not just Claude)

The vendor ships ~29 model-provider plugins
(`vendor/hermes-agent/plugins/model-providers/`: anthropic, openai-codex,
gemini, openrouter, ollama-cloud, bedrock, vertex, deepseek, xai, …).
Selection is config, not code: set `HERMES_MODEL` / `HERMES_PROVIDER`
(optionally `HERMES_MODEL_BASE_URL` for OpenAI-compatible self-hosted
endpoints) in `.env`, re-run bootstrap, and export the matching provider API
key. Omitting both keeps the vendor default (Anthropic Claude). The same
selection drives the gateway, cron briefings, and `hermes chat`; per-run
override: `hermes chat --model … --provider …`. All HealthMes glue
(webhook prompts, skills, MCP tools) is provider-agnostic.

## Backups (local-first, encrypted)

Snapshots bundle the healthmes DB dump, an optional open-wearables pg_dump,
the media tree and the Hermes state, tar them and age-encrypt with a
passphrase (docs/PLAN.md §9; format spec + remote-vault contract in
`docs/BACKUP.md`):

```bash
export HEALTHMES_BACKUP_PASSPHRASE='...'    # or set it in .env
uv run healthmes backup create              # writes {data_dir}/backups/...
uv run healthmes backup list                # needs no passphrase
uv run healthmes backup restore <name>      # dry-run: prints the manifest
uv run healthmes backup restore <name> --yes  # actually replaces live data
```

(`healthmes` is a console script installed by `uv sync`; `uv run python -m
healthmes backup ...` is equivalent. Bare `python -m healthmes` still serves
the API.) Knobs: `HEALTHMES_BACKUP_DIR` (default `{data_dir}/backups`),
`HEALTHMES_OW_DATABASE_URL` (include the open-wearables dump),
`HEALTHMES_HERMES_HOME`/`HERMES_HOME` (include Hermes state),
`--passphrase-file` (keep the passphrase out of argv/history). A weekly
backup job (Sunday 03:30) runs when the scheduler is enabled; without a
passphrase it skips with a warning. **Losing the passphrase means losing the
backups** — there is no recovery path by design.

### Remote vault (S3-compatible, ciphertext-only)

`RemoteVaultProvider` (docs/PLAN.md §9 business seam) replicates the same
age-encrypted envelopes to any S3-compatible bucket — AWS S3, Cloudflare R2
or MinIO. The vault never sees plaintext: snapshots are encrypted before
upload and the provider refuses to upload anything that is not an age
envelope. Configure `HEALTHMES_VAULT_BUCKET` (+ `HEALTHMES_VAULT_ENDPOINT` /
`_ACCESS_KEY_ID` / `_SECRET_ACCESS_KEY` / `_REGION` / `_PREFIX`; see
`.env.example`), then:

```bash
uv run healthmes backup push <name>               # replicate one snapshot
uv run healthmes backup create --provider remote  # create locally + replicate
uv run healthmes backup list --provider remote    # merged local/remote view
uv run healthmes backup restore <name> --provider remote --yes
```

`HEALTHMES_BACKUP_PROVIDER=remote_vault` makes the selector the default for
the CLI **and** the weekly job (which keeps the local snapshot even when
replication fails). Local-first always: restore prefers the local copy and
only downloads when it is missing. Full contract, privacy table and
R2/MinIO/AWS examples: `docs/BACKUP.md` §3.

## Android usage collector

`apps/android-usage/` hosts the Android apps. The original `:app` module is
the minimal usage collector (docs/PLAN.md §7) that feeds
`POST /v1/app-usage/batch` — pairing + toggle UI, hourly
`UsageStatsManager` buckets, WorkManager uploads every 30 minutes. It builds
with its own vendored Gradle wrapper (JDK 17+ and Android SDK platform 35
required; not part of the Python test suite or CI). Build, pairing and
verification steps: `apps/android-usage/README.md`. For phone → Mac uploads
the service must listen on the LAN: set `HEALTHMES_HOST=0.0.0.0` **and**
`HEALTHMES_API_TOKEN=<token>` (serve refuses a non-loopback bind without a
token — the surface carries medical data), then enter the same token in the
app. The fragmentation term of the energy engine activates automatically
once samples arrive; iOS is deliberately not collected.

## Companion & desktop apps (issues #7 · #10 · #11)

Five native surfaces render the briefing: Android phone (+ Wear OS), iOS
(+ watchOS), a macOS menu bar app (+ widgets + screensaver), a Windows tray
app (+ screensaver), and the web pages the service itself serves (decision
viewer, weekly report). All are **local-first**: each app pairs with your
own healthmes instance (base URL + bearer token) and talks to nothing else;
polling only (ETag/304, 5-minute cache floor), no APNs/FCM/WNS relay —
Telegram remains the guaranteed-delivery channel. Visual design stays
deliberately placeholder-labeled — the notification/watch UX belongs to the
healthcare domain expert (`docs/design/WATCH-NOTIFICATIONS.ko.md`; the PLAN
§8.5 notification grammar is the design system). The apps are not part of
the Python suite, but each platform has its own path-filtered CI workflow
(see "Continuous integration"); real-device passes are still owed everywhere
(each README keeps an honest not-verified list).

**Android / Wear OS** — `apps/android-usage/` (same Gradle wrapper as the
collector): `:shared` (contracts: glance, alerts page, weekly report,
proposals, capture requests; ETag-aware client; encrypted pairing),
`:companion` (the full phone app of issue #10 — single-activity Compose:
briefing home + 24h curve, alert history, weekly report, camera/photo/voice
capture, proposal accept/decline with 409 → "already resolved", settings;
plus the home/keyguard widget, §8.5 notifications with real WorkManager
actions and the ongoing focus-block notification that Wear OS bridges to the
wrist) and `:wear` (standalone Wear OS app — ProtoLayout tile + energy
complication, on-watch pairing). Per-module docs, build matrix and device
caveats: `apps/android-usage/README.md`.

```bash
cd apps/android-usage
./gradlew assembleDebug   # all four APKs (:app, :companion, :wear + lib)
./gradlew test            # all JVM unit tests (contract fixtures included)
```

**iOS / watchOS** — `apps/ios-companion/`, an XcodeGen-generated project:
the full iOS app of issue #10 (briefing home, weekly report view, in-app
decision viewer, camera/photos/voice capture, §8.5 local notifications from
BGAppRefreshTask with real accept/decline actions, focus-block Live
Activity), WidgetKit home/lock widgets, watchOS app + complications, XCTest
+ XCUITest bundles (UI tests self-skip without a live paired instance).
Requires Xcode with the iOS **and watchOS** simulator platforms
(`xcodebuild -downloadPlatform watchOS` once, ~3.6 GB) and
`brew install xcodegen`. Simulator-only builds, never signed:

```bash
cd apps/ios-companion
xcodegen generate
xcodebuild -project HealthMesCompanion.xcodeproj -scheme HealthMesCompanion \
  -destination "generic/platform=iOS Simulator" build CODE_SIGNING_ALLOWED=NO
xcodebuild -project HealthMesCompanion.xcodeproj -scheme HealthMesWatchApp \
  -destination "generic/platform=watchOS Simulator" build CODE_SIGNING_ALLOWED=NO
xcodebuild test -project HealthMesCompanion.xcodeproj -scheme HealthMesCompanion \
  -destination "platform=iOS Simulator,name=iPhone 17 Pro" CODE_SIGNING_ALLOWED=NO
```

**macOS** — `apps/macos-companion/` (issue #11), XcodeGen project reusing
`apps/ios-companion/Sources/Shared` verbatim (one contract/client across
Apple platforms): `HealthMesMac` menu bar app (status-item score, popover
briefing with real proposal actions, optional §8.5 notifications),
`HealthMesMacWidgets` WidgetKit extension, `HealthMesSaver` screensaver
bundle with the privacy toggle (hide health numbers — redaction is a tested
data rule). Native, unsigned:

```bash
cd apps/macos-companion
xcodegen generate
xcodebuild -project HealthMesMac.xcodeproj -scheme HealthMesMac \
  -destination "platform=macOS" build CODE_SIGNING_ALLOWED=NO   # + Widgets/Saver schemes
xcodebuild test -project HealthMesMac.xcodeproj -scheme HealthMesMac \
  -destination "platform=macOS" CODE_SIGNING_ALLOWED=NO
```

**Windows** — `apps/windows-companion/` (issue #11), a .NET 8 solution:
`HealthMes.Glance.Core` (portable contracts + ETag client + §8.5 grammar,
xunit-tested on any OS), `HealthMes.Tray` (WinForms tray icon + flyout +
toast balloons), `HealthMes.Screensaver` (`.scr` honoring `/s`, `/p`, `/c`
with the privacy toggle), a widgets-board card builder (the board provider
itself is deferred — MSIX/signing; see `DEFERRED.md`), DPAPI-protected
pairing. There is no Windows toolchain on this Mac: the compile-and-test
proof on real Windows is the `windows-apps.yml` CI job. Locally (any OS —
but the WinForms projects need the **official** .NET 8 SDK, not Homebrew's
`dotnet@8`, which lacks the WindowsDesktop targets):

```bash
cd apps/windows-companion
dotnet build HealthMes.Companion.sln -c Release
dotnet test tests/HealthMes.Glance.Core.Tests -c Release
```

**Cross-platform contract pinning** — every companion pins the app-facing
response schemas in fixture JSON, and a server-side contract change must
update **all platforms' fixtures in the same PR**:

- glance (`healthmes/api/briefing.py` → `GlanceOut`):
  `apps/android-usage/companion/src/test/resources/glance_*.json`,
  `apps/ios-companion/Tests/Fixtures/glance.json`, and their byte-identical
  Windows twins under
  `apps/windows-companion/tests/HealthMes.Glance.Core.Tests/Fixtures/`
- alerts (`healthmes/api/alerts.py` → `Page[AlertOut]`): `alerts_page.json`
  (Android, Windows), `alerts.json` (iOS)
- weekly report (`healthmes/api/reports.py` → `WeeklyReportOut`):
  `weekly_report.json` (Android, iOS; the Windows copy is envelope-only by
  design — its desktop parser types just the envelope)

The rule is enforced by the Python suite: `tests/api/test_glance_fixtures.py`
validates every in-repo fixture against the live server models, so contract
drift fails CI even where the companion suites themselves do not run.

### App-facing REST contracts (issue #10)

Endpoints the apps consume beyond the glance briefing (full request/response
shapes are pinned in `tests/api/`):

| Endpoint | Auth | Contract |
|---|---|---|
| `POST /v1/media` | bearer **only** | `multipart/form-data`, field name exactly `file`; client filename ignored. `Content-Length` **required** (`411` without one — chunked bodies are refused; the size cap is enforced off the header BEFORE the body is received/spooled). Content-type allowlist (jpeg/png/heic/webp images, m4a/mp3/ogg/wav audio; aliases normalized). `201 → {media_path, content_type, bytes}`; `415` (detail.allowed), `413` (cap = `HEALTHMES_MEDIA_MAX_UPLOAD_BYTES`, default 15 MiB; declared length beyond cap + 64 KiB envelope allowance is refused unread), `422` missing `file` field or empty file. Files land under `{data_dir}/media/YYYY/MM/` (UTC sharding). |
| `GET /v1/media/{media_path}` | bearer **or** derived viewer `?token=` (GET/HEAD only) | Serves the upload back (real content type, `Cache-Control: private, max-age=86400, immutable`); decision/report pages and in-app web views can embed via `<img>`/`<audio>`. All path tricks → uniform 404. |
| `POST /v1/medical-records` | bearer | REST twin of the `create_medical_record` MCP tool (the Telegram capture-skill contract): `{kind: medication\|symptom, description, media_path?, transcript?, context?}`. The server attaches the deterministic health snapshot under `context.health` (degrades to `{status: unavailable}` when open-wearables is down — capture never fails for infra reasons); caller context is stored under `context.capture`. |
| `GET /v1/alerts` | bearer | Alert history in glance semantics ("unresolved == recently pushed"): `?hours=1..168` (default 24), paginated `Page` envelope, newest first. Items carry the §8.5 grammar recorded at fire time (`summary`/`evidence`/`proposal`) + `decision_url`; `alerts[0]` agrees verbatim with the glance top alert (test-pinned). |
| `POST /v1/schedule/proposals/{id}/accept` / `/decline` | bearer | The apps' ✅/❌ actions. Second tap → `409 invalid_transition` with `detail {current, requested}` (render "already resolved"); unknown id → 404. |
| `POST /v1/food-logs` | bearer | Accepts `media_path` from `POST /v1/media` (≤500 chars). |

Client caveats worth knowing (all handled by the shipped apps):

- **Timestamp quirk**: store-backed endpoints (schedule proposals,
  food-logs) serialize naive-UTC datetimes (no `Z`), while glance/alerts
  serialize timezone-aware — clients must parse both (the shared parsers
  treat naive as UTC).
- **No alert→proposal linkage yet**: alert items carry no
  `schedule_proposal` id, so notification action buttons act only when
  exactly one proposal is pending (the no-guessing policy of PLAN §11);
  otherwise they route into the app. Lifting this needs a server-side
  linkage field.
- Push relay (APNs/FCM/WNS) is out of scope **by design** — notification
  delivery is OS-budgeted polling; Telegram is the guaranteed channel.

## 캘린더 연결 (calendar connect)

`healthmes connect` is the low-friction onboarding for the two calendar
mirrors (docs/PLAN.md §6). A successful connect stores the credential as
runtime state under `{HEALTHMES_DATA_DIR}` and the sync jobs detect it
automatically — **no `.env` edit needed** (the `HEALTHMES_GOOGLE_CALENDAR_
ENABLED` / `HEALTHMES_CALDAV_*` settings keep working and override the stored
files). Polling itself runs only while the service has
`HEALTHMES_SCHEDULER_ENABLED=true`. Connection status is also served
read-only at `GET /connect` (linked from the landing page as "캘린더 연결";
gated like the other viewer pages, renders no secrets).

### Google Calendar — one-time OAuth client, then one browser login

Honest caveat: Google has no way around a **one-time app registration** for a
personal installed app — you must create your own OAuth client once. After
that, connecting (and re-connecting) is a single browser login.

One-time (Google Cloud Console):

1. Open <https://console.cloud.google.com/> and create (or select) a project.
2. "APIs & Services" → "Library": enable the **Google Calendar API**.
3. "APIs & Services" → "OAuth consent screen": configure it and add your own
   Google account as a test user.
4. "APIs & Services" → "Credentials" → "Create credentials" →
   "OAuth client ID" → application type **Desktop app**.
5. Download the client JSON and save it to
   `{HEALTHMES_DATA_DIR}/google/client_secret.json` (or point
   `HEALTHMES_GOOGLE_CLIENT_SECRET_FILE` at wherever you keep it).

Then, whenever you want to connect:

```bash
uv run healthmes connect google      # opens the browser: log in + consent
```

The token is saved to `{HEALTHMES_DATA_DIR}/google/calendar_token.json`
(owner-only) and the Google poll job is enabled by its presence. If the
client secret is missing, the command prints exactly these setup steps.

### iCloud Calendar (CalDAV) — app-specific password only

1. Create an **app-specific password** at <https://appleid.apple.com>
   (Sign-In and Security → App-Specific Passwords) — never the account
   password.
2. Connect (the password is prompted hidden — it never touches argv or shell
   history — and validated against `caldav.icloud.com` before anything is
   stored):

```bash
uv run healthmes connect icloud --username you@icloud.com
```

On success the credential lands in
`{HEALTHMES_DATA_DIR}/caldav/credentials.json` with mode 600 and the CalDAV
poll job is enabled by its presence.

### Status / disconnect

```bash
uv run healthmes connect status              # which calendars are connected (no secrets)
uv run healthmes connect disconnect google   # remove the stored token
uv run healthmes connect disconnect icloud   # remove the stored credentials
```

Future work (deliberately not built): a hosted "connect with Google" button
in the web UI would require a registered redirect URI on this service plus
web-flow secret handling; the `/connect` page therefore shows status +
instructions only and performs no writes.

## Real credentials — what needs what

Everything in `tests/` runs offline; the features below only come alive with
real credentials. Without them the service still boots and serves — the
corresponding integrations stay inactive.

| Feature | Credential | Where |
|---|---|---|
| Telegram alerts/chat (the 90% UX) | bot token from @BotFather | `TELEGRAM_BOT_TOKEN` in `.env` (used by the hermes gateway) |
| The agent itself (Claude API) | Anthropic API key | `ANTHROPIC_API_KEY` in `.env` (hermes gateway) |
| Health data reads (MCP tools, triggers, insights) | open-wearables API key from its developer portal (`:8000/docs`) | `HEALTHMES_OW_API_KEY` (+ `OPEN_WEARABLES_API_KEY` for the vendored MCP server) |
| Wearable provider syncs | per-provider OAuth apps (Garmin, Oura, ...) | `config/open-wearables.env` (see the vendor backend docs) |
| Google Calendar mirror | OAuth client secret + one interactive consent | one-time client secret to `{HEALTHMES_DATA_DIR}/google/client_secret.json`, then `uv run healthmes connect google` (see "캘린더 연결") — the stored token auto-enables the mirror; `HEALTHMES_GOOGLE_CALENDAR_ENABLED=true` still works (polled every `HEALTHMES_GOOGLE_POLL_MINUTES` — needs `HEALTHMES_SCHEDULER_ENABLED=true`) |
| Apple Calendar (iCloud CalDAV) mirror | app-specific password from appleid.apple.com | `uv run healthmes connect icloud --username <apple-id>` (see "캘린더 연결") — the stored creds file auto-enables the mirror; the env pair `HEALTHMES_CALDAV_USERNAME` + `HEALTHMES_CALDAV_APP_PASSWORD` (+ `HEALTHMES_CALDAV_ENABLED=true`) still works and overrides it (polled every `HEALTHMES_CALDAV_POLL_MINUTES` — needs `HEALTHMES_SCHEDULER_ENABLED=true`) |
| Proactive alert push (HealthMes -> Hermes) | shared HMAC secret | `HEALTHMES_HERMES_WEBHOOK_SECRET` — generated into `.env` by `scripts/bootstrap.py` |
| Encrypted backups (CLI + weekly job) | a passphrase you choose (and must not lose) | `HEALTHMES_BACKUP_PASSPHRASE` in `.env`, or `--passphrase-file` |
| Remote vault replication (ciphertext-only, optional) | S3-compatible bucket + access keys (AWS S3 / Cloudflare R2 / MinIO) | `HEALTHMES_VAULT_BUCKET` (+ `HEALTHMES_VAULT_ENDPOINT`/`_ACCESS_KEY_ID`/`_SECRET_ACCESS_KEY`/`_REGION`/`_PREFIX`); opt in with `HEALTHMES_BACKUP_PROVIDER=remote_vault` or `--provider remote` |
| Companion & desktop apps (Android/Wear/iOS/watchOS/macOS/Windows) | the service's `HEALTHMES_API_TOKEN` (same LAN rule as the collector) | entered in each app's pairing screen together with the base URL |
| Android usage collector | the service's `HEALTHMES_API_TOKEN` (verified server-side; required whenever the service binds beyond loopback) | entered in the app UI; sent as `Authorization: Bearer ...` |
| API/MCP surface auth | bearer token you mint (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`) | `HEALTHMES_API_TOKEN` in `.env`; required for `HEALTHMES_HOST=0.0.0.0` and for docker compose |

Not a credential but environment-shaped: `HEALTHMES_TIMEZONE` (IANA name,
e.g. `Asia/Seoul`) pins the user-local day for MCP joins and boundaries —
leave it unset on mac-native (machine timezone wins), set it for docker
(container clocks are UTC).

## Tests and lint

```bash
uv run pytest -q             # all tests (testpaths=tests, network-free)
uv run ruff check .          # lint (vendor/ excluded)
uv run ruff format healthmes tests   # optional formatting
```

Test conventions: fixtures in `tests/conftest.py` provide `settings`
(in-memory sqlite + dummy endpoints, `.env` disabled), `app`, and `client`.
Put new tests under `tests/<area>/`; pytest runs with
`--import-mode=importlib`, so no `__init__.py` files are needed and file
names may repeat across areas. Tests must not require network, Docker, or
real credentials.

## Full stack (docker compose, alternative path)

```bash
install -m 600 .env.example .env                           # tokens/keys
install -m 600 config/open-wearables.env.example config/open-wearables.env
docker compose config -q      # validate
docker compose up -d --build
```

Services and host ports (all overridable via `.env`):

| Service    | Port | What                                              |
|------------|------|---------------------------------------------------|
| postgres   | 5432 | `open-wearables` DB + dedicated `healthmes` DB (created by `scripts/initdb/`) |
| redis      | 6379 | celery broker/result backend                      |
| ow-backend | 8000 | open-wearables FastAPI (`/docs`)                  |
| ow-worker  | —    | open-wearables celery worker                      |
| ow-mcp     | 8200 | vendor MCP server over Streamable HTTP (see note) |
| healthmes  | 8100 | this repo's service (`/health`, `/v1/*`, `/mcp`, `/decisions`, `/cognitive-energy/forecast`; runs `alembic upgrade head` on start) |
| hermes     | 8644 | hermes gateway webhook receiver (`/webhooks/healthmes-alerts`) |

Smoke test:

```bash
curl http://localhost:8100/health   # healthmes
curl http://localhost:8000/docs     # open-wearables
```

Notes:

- The `healthmes` database/role (`healthmes`/`healthmes`) is created by
  `scripts/initdb/01-create-healthmes-db.sh` on **first** boot of the
  `postgres_data` volume. To re-run it: `docker compose down -v` (destroys
  data) and `up` again. (`make mac-setup` is the native equivalent.)
- Compose injects docker service hostnames (`postgres`, `redis`,
  `ow-backend`, `hermes`) via container `environment:`; code and config
  defaults always stay localhost-native.
- `ow-mcp`: `vendor/open-wearables/mcp` has no Dockerfile and its entrypoint
  defaults to stdio, so compose mounts the vendor dir read-only into the
  official `ghcr.io/astral-sh/uv` Python image and serves it via
  `uv run --frozen fastmcp run app/main.py:mcp --transport http --port 8200`.
  Dependencies are resolved from the mounted `uv.lock` into a
  container-local venv on each start. The *primary* integration path per
  `docs/PLAN.md` is stdio inside the hermes container (the vendor dir is
  also mounted there at `/opt/vendor/open-wearables-mcp`).
- `hermes` deviates from the vendor compose in two documented ways: bridge
  networking instead of `network_mode: host` (service DNS; macOS support),
  and `HERMES_HOME` bind-mounted at `./data/hermes` so `scripts/bootstrap.py`
  (Phase 0, later agent) can render `config/hermes-config.yaml.tmpl` into it
  from the host.
- Hermes config is **generated**: edit `config/hermes-config.yaml.tmpl`, not
  `./data/hermes/config.yaml`.

## Layout

```
healthmes/            service package (FastAPI composition root in app.py, settings in config.py)
  store/              SQLAlchemy models + engine/session singletons (healthmes DB)
  engine/             deterministic engines (trigger rules/sweep, webhook push,
                      cognitive-energy engine, scheduler)
  calendars/          Google / iCloud CalDAV sync backends + mirror service
  mcp_server/         fastmcp Layer-B tools (14), served at exactly /mcp
  api/                REST routes (/v1/*, incl. the glance briefing), error
                      envelope, energy forecast, decision viewer + weekly
                      report (templates/ + vendored Mermaid in static/)
  backup/             local-first encrypted backup seam (age via pyrage) + CLI
                      + S3-compatible remote vault replication
alembic/              migrations for the healthmes DB (alembic.ini at repo root)
apps/android-usage/   usage collector (:app) + Android/Wear companions
                      (:shared/:companion/:wear) — own README
apps/ios-companion/   iOS/watchOS companion (XcodeGen project, own README)
apps/macos-companion/ macOS menu bar app + widgets + screensaver (XcodeGen,
                      reuses ios-companion/Sources/Shared — own README)
apps/windows-companion/ Windows tray app + screensaver + contract core
                      (.NET 8 solution, windows-latest CI — own README)
config/               templates + service env files (rendered copies gitignored)
docs/                 PLAN.md (architecture), BACKUP.md (snapshot format),
                      design/ (domain-expert worksheets, .ko.md), this guide
scripts/              dev_mac.sh (mac-native tooling), initdb/ (compose),
                      bootstrap.py (hermes), vendor_sync_check.sh (drift report)
skills/               hermes skills (copied into HERMES_HOME by bootstrap):
                      healthmes-planner, healthmes-capture, healthmes-sleep,
                      doctor-visit-summary
tests/                pytest suite (network-free)
data/                 runtime state (gitignored): pg, redis, sqlite, media, hermes home
vendor/               read-only upstreams - do not touch
```

## Conventions

- Python 3.12, typed, small modules; model/style conventions follow
  `vendor/open-wearables/backend` (see `docs/PLAN.md` for exact references).
- Everything in code/comments/docstrings is English.
- Dependencies are managed only in `pyproject.toml` + `uv.lock` (`uv add`,
  never pip).
- Settings come from `HEALTHMES_`-prefixed env vars via
  `healthmes.config.Settings`; never read raw `os.environ` in feature code.
- Never hardcode docker service hostnames in code or config defaults —
  every URL/host/port comes from `Settings`/env with localhost-native
  defaults; compose supplies docker values via `environment:`.

## Continuous integration

`.github/workflows/ci.yml` runs on pushes to `main` and on pull requests,
mirroring the run targets:

- **linux** — `uv sync --frozen`, `uv run ruff check .`, `uv run pytest -q`,
  `docker compose config -q` (compose validation without a daemon), and an
  alembic **offline** SQL render of the full migration chain for both the
  postgres and sqlite dialects (no database is ever started).
- **macos** — the mac-native developer entrypoint verbatim: `make mac-test`
  (uv + repo-local sqlite). No Homebrew services are installed or started.
- **compose-smoke** — actually boots the credential-free core of the compose
  stack (`postgres` + `redis` + `healthmes`, built from
  `Dockerfile.healthmes`; a throwaway `HEALTHMES_API_TOKEN` is minted inline
  because compose binds 0.0.0.0), curls `:8100/health` and verifies the
  bearer gate (401 without the token, 200 with) — the live half of the
  PLAN §11 "compose boot + Phase-0 demo query" smoke. The demo-query half
  needs real Telegram/Anthropic/wearable credentials CI does not have; its
  contracts are pinned by the offline test suite instead.

Everything the two test jobs run is reproducible locally with the same
commands; the test suite is offline by convention (see "Tests and lint"
above). The hardening
tests under `tests/hardening/` add a restore drill (backup snapshot →
destroy → restore → reopen the store) and trigger-flood tests pinning the
alert-hygiene guarantees of `docs/PLAN.md` §11 (daily budget, dedup storms,
quiet-hours no-redelivery).

The native apps have their own **path-filtered** workflows (they only run
when the corresponding `apps/` tree or the workflow itself changes; all
support `workflow_dispatch`; nothing is ever signed):

- **`android-apps.yml`** (ubuntu) — `./gradlew` assembles every APK
  (`:app`, `:companion`, `:wear`) and runs the JVM unit-test suites, exactly
  the locally-proven matrix from `apps/android-usage/README.md`. No emulator.
- **`apple-apps.yml`** (macos) — two jobs. `ios`: XcodeGen + unsigned
  simulator builds of the iOS and watchOS schemes, then the XCTest/XCUITest
  suite on an iPhone simulator picked from the runner's newest installed iOS
  runtime (UI tests self-skip without a live paired instance). `macos`:
  XcodeGen + unsigned native builds of the menu bar app, widget extension
  and screensaver schemes, then the XCTest suite. Both jobs run when either
  Apple directory changes, because the macOS targets compile
  `apps/ios-companion/Sources/Shared` verbatim.
- **`windows-apps.yml`** (windows) — the compile-and-test proof for
  `apps/windows-companion` (no Windows toolchain exists on the dev machine):
  `dotnet build` (Release, warnings as errors) + the xunit contract suite +
  publish of the tray app and the `.scr` screensaver as build artifacts.

## Vendor upstream sync drill

`vendor/` holds read-only snapshots of the two upstreams; nothing under it
is ever hand-edited. Upstream sync therefore means **replacing a vendor tree
wholesale in a dedicated commit** — and before doing that, run the dry-run
drift report (docs/PLAN.md §10 Phase 3):

```bash
# 1. Get a fresh upstream checkout anywhere outside the repo:
git clone --depth 1 <upstream-url> /tmp/ow-upstream

# 2. Dry-run the diff (read-only; never writes anything):
scripts/vendor_sync_check.sh open-wearables /tmp/ow-upstream
scripts/vendor_sync_check.sh --list          # names under vendor/
```

The report classifies every path as **changed** (sync would replace),
**only in vendor/** (sync would delete) or **only upstream** (sync would
add), ignoring VCS internals and derived artifacts (`.git`, `__pycache__`,
`node_modules`, virtualenvs, caches). Exit codes: `0` in sync, `1` drift
found, `2` usage error — so the drill is scriptable.

When drift touches the coupling surface (docs/PLAN.md §11 — the
open-wearables REST v1 routes + MCP tool names, and the Hermes
config/skill/cron/webhook contracts), review the glue that pins it before
syncing: `healthmes/mcp_server/`, `healthmes/engine/webhook.py`,
`config/hermes-config.yaml.tmpl`, `scripts/bootstrap.py` and their tests.
After replacing the tree, re-run `uv run pytest -q` and
`docker compose config -q` (CI runs both on the PR).
