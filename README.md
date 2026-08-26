# HealthMes Agent

**A local-first health assistant that turns wearable and calendar signals into
clear, explainable next steps.**

HealthMes watches your health context, estimates cognitive energy throughout
the day, and can proactively message you on Telegram when your plan should
change. Every recommendation is backed by the data available to the service
and can be opened as a decision flowchart.

> HealthMes is a personal software project, not a medical device or a
> substitute for professional medical advice.

## Why HealthMes

- **Proactive:** detects recovery, stress, schedule, and deadline signals
  instead of waiting for a question.
- **Explainable:** uses deterministic energy and trigger engines with
  inspectable component scores and decision records.
- **Local-first:** keeps the service, database, media, and backups on your
  machine by default.
- **Provider-agnostic:** reads wearable data through
  [`open-wearables`](https://github.com/the-momentum/open-wearables) and can
  use the LLM provider supported by the Hermes runtime.
- **Safe by default:** HTTP access is local by default; network binds require
  a bearer token, and calendar changes use an explicit confirmation boundary.

## Try It In 5 Minutes

This is the smallest working demo. It starts the HealthMes API against a
repo-local SQLite database; PostgreSQL, Redis, wearable sync, and Telegram are
not required.

### 1. Install `uv`

Install [`uv`](https://docs.astral.sh/uv/) if it is not already available.
HealthMes requires Python 3.12 or newer; `uv` can manage the project
environment and interpreter.

### 2. Start the service

```bash
uv sync
make mac-run
```

Leave the server running, then open a second terminal:

```bash
curl http://localhost:8100/health
```

Expected response:

```json
{"status":"ok"}
```

Open these local surfaces in a browser:

- `http://localhost:8100/docs` — API documentation
- `http://localhost:8100/decisions` — explainable decision records
- `http://localhost:8100/reports/weekly` — weekly report

Stop the API with `Ctrl-C`. If you used the PostgreSQL/Redis setup below,
stop those services with:

```bash
make mac-services-stop
```

## Choose Your Setup

| Goal | Start here | What you get |
|---|---|---|
| Explore the API locally | `uv sync && make mac-run` | SQLite-backed HealthMes service |
| Run the complete local stack | `make mac-setup` then `make mac-run` | PostgreSQL, Redis, migrations, and the service |
| Run the container stack | Docker section below | PostgreSQL, Redis, open-wearables, HealthMes, and Hermes |
| Connect real health data | Development guide | Provider OAuth, sync workers, and credentials |
| Use Telegram proactively | Hermes bootstrap section | Skills, cron briefings, webhook alerts, and chat |

## Full Mac-Native Setup

The mac-native path is the primary development path. It installs
`postgresql@16` and Redis with Homebrew, but does not use `brew services` or
register global services. Runtime data stays under `./data/`.

```bash
make mac-setup
install -m 600 .env.example .env
make mac-run
```

Verify the service:

```bash
curl http://localhost:8100/health
```

Useful commands:

```bash
make mac-services-status   # show PostgreSQL and Redis state
make mac-services-stop    # stop repo-local services
make mac-test              # run the offline test suite
make mac-ow                # start the open-wearables API
make mac-ow-worker         # start its Celery worker
make mac-ow-beat           # start its periodic scheduler
```

The full wearable path needs PostgreSQL and multiple long-running processes.
Run `mac-ow`, `mac-ow-worker`, and `mac-ow-beat` in separate terminals. The
provider OAuth and dogfooding steps are in
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

## Docker Compose

Use Docker when you want the complete multi-service stack or do not want to
install the native PostgreSQL/Redis tools.

```bash
install -m 600 .env.example .env
install -m 600 config/open-wearables.env.example config/open-wearables.env
```

Set an API token before starting. Compose binds HealthMes to all interfaces,
so it refuses to start without one:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put the generated value in `.env`:

```dotenv
HEALTHMES_API_TOKEN=paste-a-new-token-here
HEALTHMES_TIMEZONE=Asia/Seoul
```

Start the stack:

```bash
docker compose up -d --build
curl http://localhost:8100/health
```

Check service state and logs:

```bash
docker compose ps
docker compose logs -f healthmes
```

Stop containers without removing named volumes:

```bash
docker compose down
```

For a full compose walkthrough, including Hermes credentials and wearable
provider setup, see
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md#full-stack-docker-compose-alternative-path).

## Add Real Data And Telegram

The five-minute demo is intentionally credential-free. Add integrations only
after the local service is working.

1. **Wearables:** configure an `open-wearables` provider and verify its API
   credentials. Oura OAuth is documented as a concrete example in the
   [development guide](docs/DEVELOPMENT.md#oura-oauth-dogfooding-mac-native).
2. **Calendar:** use `uv run healthmes connect google` or the iCloud CalDAV
   flow documented in the
   [calendar section](docs/DEVELOPMENT.md#캘린더-연결-calendar-connect).
3. **Telegram and Hermes:** fill in the required provider keys and run:

   ```bash
   uv run python scripts/bootstrap.py --dry-run
   uv run python scripts/bootstrap.py
   ```

   Bootstrap renders Hermes configuration outside `vendor/`, copies the
   HealthMes skills, and registers morning, evening, and weekly briefings.
4. **CLI chat without Telegram:** use the same configured agent from the
   terminal:

   ```bash
   cd vendor/hermes-agent
   HERMES_HOME=~/.hermes \
     UV_PROJECT_ENVIRONMENT=../../data/hermes-venv \
     uv run --frozen --no-dev --extra messaging hermes
   ```

Required credentials and the provider matrix are listed in
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md#real-credentials-what-needs-what).
Never commit `.env`, OAuth client secrets, bearer tokens, refresh tokens, or
raw health payloads.

## What It Includes

### Health and planning

- Wearable health scores and daily readiness context through
  `open-wearables`.
- Explainable cognitive-energy forecasts with sleep, stress/HRV, body
  battery, meeting load, app fragmentation, and optional signals.
- Weekly goals, tasks, energy-aware schedule proposals, and calendar mirrors.
- Deterministic proactive triggers for recovery, stress, schedule changes, and
  deadline risk.

### Explainability and capture

- Decision records rendered as local Mermaid flowcharts at `/decisions/{id}`.
- Weekly reports at `/reports/weekly` and `/reports/weekly.json`.
- Telegram capture for food, medication, and symptoms through the
  `healthmes-capture` skill.
- Evidence-aware MCP tools that report confidence, coverage, and
  `insufficient_data` instead of inventing certainty.

### Apps and glance surfaces

Native companions use the same bearer-authenticated contracts and pair with
your own HealthMes instance:

| Surface | Location | Purpose |
|---|---|---|
| Android + Wear OS | [`apps/android-usage/`](apps/android-usage/) | Briefing, weekly report, capture, widgets, Wear tile, usage collection |
| iOS + watchOS | [`apps/ios-companion/`](apps/ios-companion/) | Briefing, capture, notifications, widgets, watch app, complications |
| macOS | [`apps/macos-companion/`](apps/macos-companion/) | Menu bar briefing, widgets, ambient screensaver |
| Windows | [`apps/windows-companion/`](apps/windows-companion/) | Tray briefing, notifications, widgets-board card, screensaver |

Build and verification status for each platform lives in its app README.
Visual notification and watch UX remains deliberately placeholder-labeled;
see [`docs/design/WATCH-NOTIFICATIONS.ko.md`](docs/design/WATCH-NOTIFICATIONS.ko.md).

### Backups

HealthMes can create local age-encrypted snapshots containing the HealthMes
database, optional wearable database dump, media, and Hermes state:

```bash
export HEALTHMES_BACKUP_PASSPHRASE='use-a-password-manager'
uv run healthmes backup create
uv run healthmes backup list
uv run healthmes backup restore <name>       # dry-run
uv run healthmes backup restore <name> --yes # apply
```

An S3-compatible remote vault can replicate ciphertext-only snapshots. Read
[`docs/BACKUP.md`](docs/BACKUP.md) before enabling remote replication. Losing
the passphrase means losing access to the encrypted backups.

## How It Fits Together

```text
wearables ──REST──> HealthMes service ──MCP──> Hermes Agent ──> Telegram
                         │       │
                         │       ├── REST/MCP API
                         │       ├── decision viewer
                         │       └── weekly report
                         └── local database + encrypted backups
```

HealthMes is the glue plane around two unmodified vendored upstreams:

- `vendor/open-wearables/` — wearable data plane and provider integrations.
- `vendor/hermes-agent/` — agent runtime, skills, memory, cron, Telegram
  gateway, and MCP client.

HealthMes-owned code lives at the repository root and communicates with those
upstreams through documented REST, MCP, webhook, and rendered-configuration
contracts. Do not modify either vendored tree.

## Repository Guide

| Need | Document |
|---|---|
| Architecture and product rationale | [`docs/PLAN.md`](docs/PLAN.md) |
| Development, credentials, integrations, tests, and CI | [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) |
| Add metrics, skills, or insight templates | [`docs/EXTENDING.md`](docs/EXTENDING.md) |
| Healthcare expert onboarding | [`docs/EXPERT-ONBOARDING.ko.md`](docs/EXPERT-ONBOARDING.ko.md) |
| Backup format and remote-vault contract | [`docs/BACKUP.md`](docs/BACKUP.md) |
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

## Development Checks

Run the checks used by CI before opening a change:

```bash
uv run ruff check .
uv run pytest -q
docker compose config -q
make mac-test
```

CI covers Linux lint/tests, macOS native tests, compose validation, migration
rendering, and a compose boot smoke test. Tests do not call external services.

## License

HealthMes Agent is available for non-commercial use under the project license
in [`LICENSE`](LICENSE). Commercial use requires a separate written license
from the project owner.

This repository includes code derived from Hermes Agent by Nous Research and
open-wearables by Momentum, plus the Mermaid library. Original notices are
preserved in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
