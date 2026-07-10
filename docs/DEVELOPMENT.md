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
cp .env.example .env      # optional: tweak HEALTHMES_* values
make mac-run              # alembic upgrade head + uvicorn :8100
curl http://localhost:8100/health   # -> {"status":"ok"}
make mac-test             # uv run pytest -q
```

The service exposes `/health`, the REST surface under `/v1/*`, the
cognitive-energy forecast at `/cognitive-energy/forecast`, the decision
viewer at `/decisions` + `/decisions/{id}` (Mermaid, served fully locally),
and the Layer-B MCP server (Streamable HTTP) at exactly `/mcp`.

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
`$HERMES_HOME/config.yaml`, symlinks `skills/` into `$HERMES_HOME/skills/`,
generates a `HEALTHMES_HERMES_WEBHOOK_SECRET` into `.env` when missing,
installs the briefing state-snapshot script
(`scripts/healthmes_briefing_snapshot.py` + a base-URL sidecar) into
`$HERMES_HOME/scripts/`, and registers the three cron briefings (morning
07:00, evening 21:30, weekly Sunday 18:00) in `$HERMES_HOME/cron/jobs.json`
— each with `script:` set so the vendor scheduler pre-injects a compact
state snapshot into the briefing prompt (docs/PLAN.md §4), saving MCP
round-trips at run time.

```bash
uv run python scripts/bootstrap.py --dry-run     # show what would change
uv run python scripts/bootstrap.py               # native run (HERMES_HOME=~/.hermes)
uv run python scripts/bootstrap.py --mode docker # compose paths (HERMES_HOME=./data/hermes)
```

It is idempotent: re-runs deep-merge the config (your manual keys win, one
backup is kept) and skip already-registered jobs/symlinks. `HERMES_HOME`,
`TELEGRAM_HOME_CHAT_ID`, and the other inputs come from the environment or
`.env` (see the bootstrap section of `.env.example`).

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

## Android usage collector

`apps/android-usage/` is the minimal Kotlin companion app (docs/PLAN.md §7)
that feeds `POST /v1/app-usage/batch` — pairing + toggle UI, hourly
`UsageStatsManager` buckets, WorkManager uploads every 30 minutes. It builds
with its own vendored Gradle wrapper (JDK 17+ and Android SDK platform 35
required; not part of the Python test suite or CI). Build, pairing and
verification steps: `apps/android-usage/README.md`. For phone → Mac uploads
the service must listen on the LAN: set `HEALTHMES_HOST=0.0.0.0` **and**
`HEALTHMES_API_TOKEN=<token>` (serve refuses a non-loopback bind without a
token — the surface carries medical data), then enter the same token in the
app. The fragmentation term of the energy engine activates automatically
once samples arrive; iOS is deliberately not collected.

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
| Google Calendar mirror | OAuth client secret + one interactive consent | `{HEALTHMES_DATA_DIR}/google/client_secret.json`, then run the installed-app flow once to mint `calendar_token.json`; set `HEALTHMES_GOOGLE_CALENDAR_ENABLED=true` (polled every `HEALTHMES_GOOGLE_POLL_MINUTES` by the in-service scheduler — needs `HEALTHMES_SCHEDULER_ENABLED=true`) |
| Apple Calendar (iCloud CalDAV) mirror | app-specific password from appleid.apple.com | `HEALTHMES_CALDAV_USERNAME` + `HEALTHMES_CALDAV_APP_PASSWORD`; set `HEALTHMES_CALDAV_ENABLED=true` (polled every `HEALTHMES_CALDAV_POLL_MINUTES` — needs `HEALTHMES_SCHEDULER_ENABLED=true`) |
| Proactive alert push (HealthMes -> Hermes) | shared HMAC secret | `HEALTHMES_HERMES_WEBHOOK_SECRET` — generated into `.env` by `scripts/bootstrap.py` |
| Encrypted backups (CLI + weekly job) | a passphrase you choose (and must not lose) | `HEALTHMES_BACKUP_PASSPHRASE` in `.env`, or `--passphrase-file` |
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
cp .env.example .env                                       # tokens/keys
cp config/open-wearables.env.example config/open-wearables.env
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
  api/                REST routes (/v1/*), error envelope, energy forecast,
                      decision viewer (templates/ + vendored Mermaid in static/)
  backup/             local-first encrypted backup seam (age via pyrage) + CLI
alembic/              migrations for the healthmes DB (alembic.ini at repo root)
apps/android-usage/   Kotlin companion app feeding /v1/app-usage/batch (own README)
config/               templates + service env files (rendered copies gitignored)
docs/                 PLAN.md (architecture), BACKUP.md (snapshot format), this guide
scripts/              dev_mac.sh (mac-native tooling), initdb/ (compose),
                      bootstrap.py (hermes), vendor_sync_check.sh (drift report)
skills/               hermes skills (symlinked into HERMES_HOME by bootstrap):
                      healthmes-planner, healthmes-capture, doctor-visit-summary
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
