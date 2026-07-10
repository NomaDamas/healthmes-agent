#!/usr/bin/env bash
# Mac-native development tooling for the HealthMes Agent stack (primary run
# path — docker compose is the alternative, see docs/DEVELOPMENT.md).
#
# Everything is ephemeral and repo-local:
#   - postgres runs out of ./data/pg via pg_ctl (postmaster.pid inside),
#   - redis daemonizes with ./data/redis.pid,
#   - NO `brew services start` — nothing survives this repo or autostarts.
#
# Subcommands (wrapped 1:1 by the root Makefile):
#   setup            brew-install postgresql@16 + redis if missing, initdb
#                    ./data/pg, start services, create the open-wearables +
#                    healthmes databases, uv sync. Idempotent.
#   services-start   start postgres + redis (no-op when already running)
#   services-stop    stop postgres + redis started by services-start
#   services-status  report what is running
#   run              alembic upgrade head (when alembic.ini exists) + uvicorn
#                    healthmes on HEALTHMES_PORT (default 8100)
#   test             uv run pytest -q
#   ow               best-effort native boot of vendor/open-wearables backend
#                    (uv sync + scripts/start/app.sh; migrations + seeds +
#                    `fastapi dev` when ENVIRONMENT=local)
#   ow-worker        celery worker of the open-wearables backend
#                    (requires redis from services-start)
#
# Vendor grounding (vendor/ is read-only — the venv of the vendored backend is
# redirected outside the vendor tree via UV_PROJECT_ENVIRONMENT):
#   - boot commands: vendor/open-wearables/backend/README.md +
#     scripts/start/app.sh / worker.sh
#   - env names (DB_HOST, REDIS_HOST, ...): vendor backend app/config.py;
#     process env overrides the vendored config/.env (pydantic-settings).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$REPO_ROOT/data"
PG_DATA="$DATA_DIR/pg"
PG_LOG="$DATA_DIR/pg.log"
REDIS_PID="$DATA_DIR/redis.pid"
REDIS_LOG="$DATA_DIR/redis.log"
REDIS_DIR="$DATA_DIR/redis"

# Ports match .env.example / docker-compose defaults; override via env.
DB_PORT="${DB_PORT:-5432}"
REDIS_PORT="${REDIS_PORT:-6379}"

# Database names/roles match config/open-wearables.env.example and
# scripts/initdb/01-create-healthmes-db.sh (the docker path equivalent).
OW_DB_NAME="${OW_DB_NAME:-open-wearables}"
OW_DB_USER="${OW_DB_USER:-open-wearables}"
OW_DB_PASSWORD="${OW_DB_PASSWORD:-open-wearables}"
HEALTHMES_DB_NAME="${HEALTHMES_DB_NAME:-healthmes}"
HEALTHMES_DB_USER="${HEALTHMES_DB_USER:-healthmes}"
HEALTHMES_DB_PASSWORD="${HEALTHMES_DB_PASSWORD:-healthmes}"

PG_SUPERUSER="${PG_SUPERUSER:-$USER}"

OW_BACKEND_DIR="$REPO_ROOT/vendor/open-wearables/backend"
OW_ENV_FILE="$REPO_ROOT/config/open-wearables.env"
OW_ENV_EXAMPLE="$REPO_ROOT/config/open-wearables.env.example"
# Keep the vendored backend's venv OUT of the read-only vendor tree.
OW_VENV_DIR="$DATA_DIR/ow-backend-venv"

info() { printf '\033[1;34m[dev_mac]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[dev_mac]\033[0m %s\n' "$*" >&2; }
die() {
    printf '\033[1;31m[dev_mac]\033[0m %s\n' "$*" >&2
    exit 1
}

require_brew() {
    command -v brew >/dev/null 2>&1 || die "Homebrew is required: https://brew.sh"
}

brew_ensure() {
    # brew_ensure <formula> — install once, never `brew services start`.
    local formula=$1
    if brew list --formula "$formula" >/dev/null 2>&1; then
        info "$formula already installed"
    else
        info "installing $formula (one-time)..."
        brew install "$formula"
    fi
}

pg_bin() {
    # postgresql@16 is keg-only: binaries are not linked onto PATH.
    echo "$(brew --prefix postgresql@16)/bin"
}

redis_server_bin() {
    command -v redis-server >/dev/null 2>&1 && { command -v redis-server; return; }
    echo "$(brew --prefix)/bin/redis-server"
}

redis_cli_bin() {
    command -v redis-cli >/dev/null 2>&1 && { command -v redis-cli; return; }
    echo "$(brew --prefix)/bin/redis-cli"
}

pg_running() {
    [ -d "$PG_DATA" ] && "$(pg_bin)/pg_ctl" -D "$PG_DATA" status >/dev/null 2>&1
}

redis_running() {
    [ -f "$REDIS_PID" ] && kill -0 "$(cat "$REDIS_PID")" 2>/dev/null
}

psql_super() {
    # psql as the initdb superuser against the maintenance DB (TCP, trust auth).
    "$(pg_bin)/psql" -h 127.0.0.1 -p "$DB_PORT" -U "$PG_SUPERUSER" -d postgres \
        -v ON_ERROR_STOP=1 -qtAX "$@"
}

ensure_pg_initdb() {
    if [ -f "$PG_DATA/PG_VERSION" ]; then
        info "postgres data dir already initialized ($PG_DATA)"
        return
    fi
    if [ -d "$PG_DATA" ]; then
        die "$PG_DATA exists but has no PG_VERSION (partial initdb?). Remove it and re-run."
    fi
    info "initializing postgres data dir at $PG_DATA"
    mkdir -p "$DATA_DIR"
    # trust auth: local dev only; the dir lives inside the repo and is gitignored.
    "$(pg_bin)/initdb" -D "$PG_DATA" -U "$PG_SUPERUSER" \
        --encoding=UTF8 --locale=C --auth=trust >>"$PG_LOG" 2>&1
}

start_postgres() {
    if pg_running; then
        info "postgres already running (port check: $DB_PORT)"
        return
    fi
    [ -f "$PG_DATA/PG_VERSION" ] || die "no postgres data dir — run '$0 setup' first"
    info "starting postgres on port $DB_PORT (data: $PG_DATA)"
    # -k: keep unix sockets inside the data dir (nothing global touched).
    "$(pg_bin)/pg_ctl" -D "$PG_DATA" -l "$PG_LOG" -w \
        -o "-p $DB_PORT -k '$PG_DATA'" start
}

stop_postgres() {
    if pg_running; then
        info "stopping postgres"
        "$(pg_bin)/pg_ctl" -D "$PG_DATA" -m fast -w stop
    else
        info "postgres not running"
    fi
}

start_redis() {
    if redis_running; then
        info "redis already running (pid $(cat "$REDIS_PID"))"
        return
    fi
    rm -f "$REDIS_PID"
    mkdir -p "$REDIS_DIR"
    info "starting redis on port $REDIS_PORT (pidfile: $REDIS_PID)"
    "$(redis_server_bin)" --port "$REDIS_PORT" --daemonize yes \
        --pidfile "$REDIS_PID" --logfile "$REDIS_LOG" --dir "$REDIS_DIR"
    # daemonize returns before the pidfile exists; wait briefly.
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        redis_running && break
        sleep 0.2
    done
    redis_running || die "redis failed to start — see $REDIS_LOG"
}

stop_redis() {
    if redis_running; then
        local pid
        pid="$(cat "$REDIS_PID")"
        info "stopping redis (pid $pid)"
        if command -v "$(redis_cli_bin)" >/dev/null 2>&1; then
            "$(redis_cli_bin)" -p "$REDIS_PORT" shutdown nosave 2>/dev/null || kill "$pid" 2>/dev/null || true
        else
            kill "$pid" 2>/dev/null || true
        fi
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.2
        done
    else
        info "redis not running"
    fi
    rm -f "$REDIS_PID"
}

ensure_role() {
    # ensure_role <role> <password> [extra role options]
    local role=$1 password=$2 options=${3:-}
    if [ "$(psql_super -c "SELECT 1 FROM pg_roles WHERE rolname = '$role'")" = "1" ]; then
        info "role '$role' already exists"
    else
        info "creating role '$role'"
        psql_super -c "CREATE ROLE \"$role\" WITH LOGIN $options PASSWORD '$password'"
    fi
}

ensure_database() {
    # ensure_database <dbname> <owner>
    local db=$1 owner=$2
    if [ "$(psql_super -c "SELECT 1 FROM pg_database WHERE datname = '$db'")" = "1" ]; then
        info "database '$db' already exists"
    else
        info "creating database '$db' (owner: $owner)"
        psql_super -c "CREATE DATABASE \"$db\" OWNER \"$owner\""
    fi
}

create_databases() {
    # CREATEDB on the open-wearables role: its app.sh auto-creates the 'svix'
    # database (vendor backend scripts/init/create_svix_db.py).
    ensure_role "$OW_DB_USER" "$OW_DB_PASSWORD" "CREATEDB"
    ensure_role "$HEALTHMES_DB_USER" "$HEALTHMES_DB_PASSWORD"
    ensure_database "$OW_DB_NAME" "$OW_DB_USER"
    ensure_database "$HEALTHMES_DB_NAME" "$HEALTHMES_DB_USER"
}

load_ow_env() {
    # Export env for the vendored backend. Process env beats the (absent)
    # vendored config/.env because pydantic-settings prefers real env vars.
    local file="$OW_ENV_FILE"
    if [ ! -f "$file" ]; then
        warn "config/open-wearables.env not found — using the .example defaults."
        warn "For real provider credentials: cp config/open-wearables.env.example config/open-wearables.env"
        file="$OW_ENV_EXAMPLE"
    fi
    info "loading open-wearables env from $file"
    set -a
    # shellcheck disable=SC1090
    . "$file"
    set +a
    export UV_PROJECT_ENVIRONMENT="$OW_VENV_DIR"
    # Never let uv rewrite the vendored uv.lock (vendor/ is read-only):
    # UV_FROZEN=1 makes both our `uv sync` and the bare `uv run` inside the
    # vendor scripts/start/*.sh behave like --frozen — symmetric with the
    # hermes config template and the ow-mcp compose service, which pass
    # --frozen explicitly.
    export UV_FROZEN=1
}

cmd_setup() {
    require_brew
    brew_ensure postgresql@16
    brew_ensure redis
    ensure_pg_initdb
    start_postgres
    start_redis
    create_databases
    info "syncing healthmes python environment (uv sync)"
    (cd "$REPO_ROOT" && uv sync)
    info "setup complete — services are running; stop them with: make mac-services-stop"
}

cmd_services_start() {
    start_postgres
    start_redis
    info "services up (postgres:$DB_PORT, redis:$REDIS_PORT)"
}

cmd_services_stop() {
    stop_redis
    stop_postgres
    info "services stopped"
}

cmd_services_status() {
    if pg_running; then info "postgres: running ($PG_DATA)"; else info "postgres: stopped"; fi
    if redis_running; then info "redis: running (pid $(cat "$REDIS_PID"))"; else info "redis: stopped"; fi
}

cmd_run() {
    cd "$REPO_ROOT"
    mkdir -p "$DATA_DIR"
    if [ -f alembic.ini ]; then
        info "applying healthmes migrations (alembic upgrade head)"
        uv run alembic upgrade head
    else
        info "no alembic.ini yet (healthmes/store not scaffolded) — skipping migrations"
    fi
    info "starting healthmes on port ${HEALTHMES_PORT:-8100}"
    exec uv run python -m healthmes
}

cmd_test() {
    cd "$REPO_ROOT"
    exec uv run pytest -q
}

cmd_ow() {
    # Best-effort native boot per vendor/open-wearables/backend/README.md.
    # Needs postgres (services-start). NOTE: the vendored project targets
    # Python 3.13 (uv downloads it) and its README asks for uv >= 0.9.17
    # (`uv self update` if sync fails on the lockfile).
    load_ow_env
    info "syncing vendored backend deps (venv: $OW_VENV_DIR)"
    if ! (cd "$OW_BACKEND_DIR" && uv sync); then
        die "uv sync failed — vendor README requires uv >= 0.9.17 ('uv self update')"
    fi
    info "booting open-wearables backend (migrations + seeds + API on \${API_PORT:-8000})"
    info "svix webhook registration may retry/skip — no svix server in this stack (non-fatal)"
    cd "$OW_BACKEND_DIR"
    exec bash scripts/start/app.sh
}

cmd_ow_worker() {
    # Celery worker of the vendored backend — REQUIRES redis (services-start)
    # as broker/result backend, and the API/migrations from 'ow' run first.
    load_ow_env
    info "syncing vendored backend deps (venv: $OW_VENV_DIR)"
    (cd "$OW_BACKEND_DIR" && uv sync) || die "uv sync failed — see 'ow' subcommand notes"
    info "starting open-wearables celery worker (broker: redis://\${REDIS_HOST:-localhost}:\${REDIS_PORT:-6379})"
    cd "$OW_BACKEND_DIR"
    exec bash scripts/start/worker.sh
}

usage() {
    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 1
}

case "${1:-}" in
setup) cmd_setup ;;
services-start) cmd_services_start ;;
services-stop) cmd_services_stop ;;
services-status) cmd_services_status ;;
run) cmd_run ;;
test) cmd_test ;;
ow) cmd_ow ;;
ow-worker) cmd_ow_worker ;;
*) usage ;;
esac
