#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$REPO_ROOT/data"
RUNTIME_DIR="$DATA_DIR/runtime"
HEALTHMES_PID="$RUNTIME_DIR/healthmes.pid"
OW_PID="$RUNTIME_DIR/open-wearables.pid"
WORKER_PID="$RUNTIME_DIR/open-wearables-worker.pid"
BEAT_PID="$RUNTIME_DIR/open-wearables-beat.pid"
HEALTHMES_LOG="$RUNTIME_DIR/healthmes.log"
OW_LOG="$RUNTIME_DIR/open-wearables.log"
WORKER_LOG="$RUNTIME_DIR/open-wearables-worker.log"
BEAT_LOG="$RUNTIME_DIR/open-wearables-beat.log"
DASHBOARD_URL="${HEALTHMES_DASHBOARD_URL:-http://127.0.0.1:${HEALTHMES_PORT:-8100}/sleep}"
LAUNCH_AGENT_LABEL="com.healthmes.local"
LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"
LAUNCH_AGENT_PLIST="$LAUNCH_AGENT_DIR/$LAUNCH_AGENT_LABEL.plist"
LAUNCH_AGENT_TEMPLATE="$REPO_ROOT/config/$LAUNCH_AGENT_LABEL.plist.in"
HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
HERMES_GATEWAY_LABEL="ai.hermes.gateway"

info() { printf '[healthmes] %s\n' "$*"; }
die() { printf '[healthmes] %s\n' "$*" >&2; exit 1; }

pid_running() {
    local file=$1 pid
    [ -f "$file" ] || return 1
    pid="$(<"$file")"
    [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

descendant_pids() {
    local parent=$1 child
    for child in $(pgrep -P "$parent" 2>/dev/null || true); do
        printf '%s\n' "$child"
        descendant_pids "$child"
    done
}

start_process() {
    local name=$1 pid_file=$2 log_file=$3 command=$4
    if pid_running "$pid_file"; then
        info "$name already running (pid $(<"$pid_file"))"
        return
    fi
    mkdir -p "$RUNTIME_DIR"
    (
        cd "$REPO_ROOT"
        nohup bash -lc "$command" >>"$log_file" 2>&1 &
        printf '%s\n' "$!" >"$pid_file"
    )
    sleep 1
    pid_running "$pid_file" || die "$name failed to start; see $log_file"
    info "$name started (pid $(<"$pid_file"))"
}

open_wearables_listener_pid() {
    local -a pids
    local pid parent candidate
    while read -r pid; do
        [ -n "$pid" ] && pids+=("$pid")
    done < <(lsof -nP -iTCP:"${API_PORT:-8000}" -sTCP:LISTEN -t 2>/dev/null)
    [ "${#pids[@]}" -gt 0 ] || return
    for pid in "${pids[@]}"; do
        parent="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')"
        for candidate in "${pids[@]}"; do
            if [ "$parent" = "$candidate" ]; then
                printf '%s\n' "$pid"
                return
            fi
        done
    done
    printf '%s\n' "${pids[0]}"
}

open_wearables_listener_is_managed() {
    local pid=$1 parent command
    while [[ "$pid" =~ ^[0-9]+$ ]] && [ "$pid" -gt 1 ]; do
        command="$(ps -o command= -p "$pid" 2>/dev/null || true)"
        [[ "$command" == *"fastapi dev app/main.py"* || "$command" == *"fastapi run app/main.py"* ]] && return 0
        parent="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')"
        [ "$parent" != "$pid" ] || break
        pid="$parent"
    done
    return 1
}

start_open_wearables() {
    local listener_pid
    if pid_running "$OW_PID"; then
        info "Open Wearables already running (pid $(<"$OW_PID"))"
        return
    fi
    listener_pid="$(open_wearables_listener_pid)"
    if [ -n "$listener_pid" ]; then
        open_wearables_listener_is_managed "$listener_pid" \
            || die "Open Wearables port ${API_PORT:-8000} is already owned by pid $listener_pid"
        printf '%s\n' "$listener_pid" >"$OW_PID"
        info "Open Wearables listener adopted (pid $listener_pid)"
        return
    fi
    start_process "Open Wearables" "$OW_PID" "$OW_LOG" \
        "exec bash '$REPO_ROOT/scripts/dev_mac.sh' ow"
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        listener_pid="$(open_wearables_listener_pid)"
        if [ -n "$listener_pid" ]; then
            printf '%s\n' "$listener_pid" >"$OW_PID"
            info "Open Wearables listener adopted (pid $listener_pid)"
            return
        fi
        sleep 1
    done
    die "Open Wearables did not open port ${API_PORT:-8000}; see $OW_LOG"
}

stop_process() {
    local name=$1 pid_file=$2 pid child process_pid still_running
    local -a pids
    if ! pid_running "$pid_file"; then
        rm -f "$pid_file"
        info "$name stopped"
        return
    fi
    pid="$(<"$pid_file")"
    pids=("$pid")
    while read -r child; do
        [ -n "$child" ] && pids+=("$child")
    done < <(descendant_pids "$pid")
    kill "${pids[@]}" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        still_running=false
        for process_pid in "${pids[@]}"; do
            if kill -0 "$process_pid" 2>/dev/null; then
                still_running=true
                break
            fi
        done
        [ "$still_running" = false ] && break
        sleep 0.2
    done
    for process_pid in "${pids[@]}"; do
        kill -0 "$process_pid" 2>/dev/null && kill -9 "$process_pid"
    done
    rm -f "$pid_file"
    info "$name stopped"
}

load_runtime_env() {
    set -a
    [ -f "$REPO_ROOT/.env" ] && source "$REPO_ROOT/.env"
    [ -f "$REPO_ROOT/.env.local" ] && source "$REPO_ROOT/.env.local"
    [ -f "$REPO_ROOT/config/open-wearables.env" ] \
        && source "$REPO_ROOT/config/open-wearables.env"
    set +a
}

sync_hermes_calendar_adjustment_secret() {
    local hermes_env="$HERMES_HOME_DIR/.env" secret
    [ -r "$hermes_env" ] || return
    secret="$({ awk -F= '$1 == "HEALTHMES_CALENDAR_ADJUSTMENT_SECRET" { print substr($0, index($0, "=") + 1); exit }' "$hermes_env"; })"
    [ -n "$secret" ] || return
    export HEALTHMES_CALENDAR_ADJUSTMENT_SECRET="$secret"
}

resolve_ow_api_key() {
    [ -n "${HEALTHMES_OW_API_KEY:-}" ] && return
    local psql_bin api_key
    if command -v psql >/dev/null 2>&1; then
        psql_bin="$(command -v psql)"
    else
        psql_bin="$(brew --prefix postgresql@16)/bin/psql"
    fi
    api_key="$(
        PGPASSWORD="${DB_PASSWORD:-open-wearables}" "$psql_bin" \
            -X -q -t -A \
            -h "${DB_HOST:-localhost}" \
            -p "${DB_PORT:-5432}" \
            -U "${DB_USER:-open-wearables}" \
            -d "${DB_NAME:-open-wearables}" \
            -c "SELECT id FROM api_key ORDER BY created_at LIMIT 1"
    )"
    [ -n "$api_key" ] || die "Open Wearables API key not found"
    export HEALTHMES_OW_API_KEY="$api_key"
}

sync_hermes_ow_api_key() {
    local config_path="$HERMES_HOME_DIR/config.yaml" result
    [ -f "$config_path" ] || return
    result="$(
        uv run python - "$config_path" <<'PY'
import os
import sys
import tempfile
from pathlib import Path

import yaml

path = Path(sys.argv[1])
config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
env = config.setdefault("mcp_servers", {}).setdefault(
    "open_wearables", {}
).setdefault("env", {})
key = os.environ["HEALTHMES_OW_API_KEY"]
if env.get("OPEN_WEARABLES_API_KEY") == key:
    print("unchanged")
    raise SystemExit
env["OPEN_WEARABLES_API_KEY"] = key
fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    yaml.safe_dump(config, stream, sort_keys=False, allow_unicode=True)
os.chmod(temp_name, 0o600)
os.replace(temp_name, path)
print("updated")
PY
    )"
    if [ "$result" != "updated" ]; then
        return 0
    fi
    info "synchronized Open Wearables credential into Hermes"
    if launchctl print "gui/$UID/$HERMES_GATEWAY_LABEL" >/dev/null 2>&1; then
        launchctl kickstart -k "gui/$UID/$HERMES_GATEWAY_LABEL"
        info "restarted Hermes gateway to reload MCP credentials"
    fi
}

install_launch_agent() {
    [ -f "$LAUNCH_AGENT_TEMPLATE" ] || die "missing launch agent template"
    mkdir -p "$LAUNCH_AGENT_DIR" "$RUNTIME_DIR"
    local escaped_repo temp_plist bootstrap_log bootstrapped
    escaped_repo="${REPO_ROOT//&/\\&}"
    temp_plist="$(mktemp)"
    sed "s|__REPO_ROOT__|$escaped_repo|g" "$LAUNCH_AGENT_TEMPLATE" >"$temp_plist"
    install -m 644 "$temp_plist" "$LAUNCH_AGENT_PLIST"
    rm -f "$temp_plist"
    launchctl bootout "gui/$UID/$LAUNCH_AGENT_LABEL" >/dev/null 2>&1 || true
    bootstrap_log="$(mktemp)"
    bootstrapped=false
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if launchctl bootstrap "gui/$UID" "$LAUNCH_AGENT_PLIST" \
            2>"$bootstrap_log"; then
            bootstrapped=true
            break
        fi
        sleep 0.5
    done
    if [ "$bootstrapped" != true ]; then
        cat "$bootstrap_log" >&2
        rm -f "$bootstrap_log"
        die "failed to register login launch agent"
    fi
    rm -f "$bootstrap_log"
    launchctl enable "gui/$UID/$LAUNCH_AGENT_LABEL"
    launchctl kickstart -k "gui/$UID/$LAUNCH_AGENT_LABEL"
    info "login launch agent installed ($LAUNCH_AGENT_LABEL)"
}

uninstall_launch_agent() {
    launchctl bootout "gui/$UID/$LAUNCH_AGENT_LABEL" >/dev/null 2>&1 || true
    if [ -f "$LAUNCH_AGENT_PLIST" ]; then
        rm "$LAUNCH_AGENT_PLIST"
        info "login launch agent removed"
    fi
}

cmd_install() {
    bash "$REPO_ROOT/scripts/dev_mac.sh" setup
    install_launch_agent
    info "installed and configured to start at login"
}

cmd_update() {
    git -C "$REPO_ROOT" diff --quiet || die "working tree has changes; commit or stash first"
    git -C "$REPO_ROOT" diff --cached --quiet || die "index has changes; commit or stash first"
    git -C "$REPO_ROOT" pull --ff-only
    bash "$REPO_ROOT/scripts/dev_mac.sh" setup
    info "updated"
}

cmd_start() {
    load_runtime_env
    sync_hermes_calendar_adjustment_secret
    bash "$REPO_ROOT/scripts/dev_mac.sh" services-start
    resolve_ow_api_key
    sync_hermes_ow_api_key
    start_open_wearables
    start_process "Open Wearables worker" "$WORKER_PID" "$WORKER_LOG" \
        "exec bash '$REPO_ROOT/scripts/dev_mac.sh' ow-worker"
    start_process "Open Wearables beat" "$BEAT_PID" "$BEAT_LOG" \
        "exec bash '$REPO_ROOT/scripts/dev_mac.sh' ow-beat"
    start_process "HealthMes" "$HEALTHMES_PID" "$HEALTHMES_LOG" \
        "exec bash '$REPO_ROOT/scripts/dev_mac.sh' run"
    info "dashboard: $DASHBOARD_URL"
}

cmd_daemon() {
    trap 'stop_apps; exit 0' INT TERM
    cmd_start
    while true; do
        sleep 5
        if ! pid_running "$HEALTHMES_PID" \
            || ! pid_running "$OW_PID" \
            || ! pid_running "$WORKER_PID" \
            || ! pid_running "$BEAT_PID"; then
            cmd_start
        fi
    done
}

stop_apps() {
    stop_process "HealthMes" "$HEALTHMES_PID"
    stop_process "Open Wearables beat" "$BEAT_PID"
    stop_process "Open Wearables worker" "$WORKER_PID"
    stop_process "Open Wearables" "$OW_PID"
}

cmd_stop() {
    stop_apps
    bash "$REPO_ROOT/scripts/dev_mac.sh" services-stop
}

service_status() {
    local name=$1 file=$2
    if pid_running "$file"; then
        info "$name: running (pid $(<"$file"))"
    else
        info "$name: stopped"
    fi
}

cmd_status() {
    service_status "HealthMes" "$HEALTHMES_PID"
    service_status "Open Wearables" "$OW_PID"
    service_status "Open Wearables worker" "$WORKER_PID"
    service_status "Open Wearables beat" "$BEAT_PID"
    bash "$REPO_ROOT/scripts/dev_mac.sh" services-status
    if curl --fail --silent --max-time 2 "http://127.0.0.1:${HEALTHMES_PORT:-8100}/health" >/dev/null; then
        info "HealthMes HTTP: ready"
    else
        info "HealthMes HTTP: not ready"
    fi
}

cmd_open() {
    command -v open >/dev/null 2>&1 || die "macOS open command not found"
    open "$DASHBOARD_URL"
}

cmd_uninstall() {
    uninstall_launch_agent
    cmd_stop
    rm -rf "$RUNTIME_DIR"
    if [ "${1:-}" = "--delete-data" ]; then
        [ "$DATA_DIR" = "$REPO_ROOT/data" ] || die "refusing unexpected data path"
        rm -rf "$DATA_DIR"
        info "runtime and local data deleted"
    else
        info "runtime removed; local data kept"
        info "delete it explicitly with: $0 uninstall --delete-data"
    fi
}

usage() {
    printf 'usage: %s install|update|start|stop|status|open|uninstall [--delete-data]\n' "$0"
    exit 1
}

case "${1:-}" in
install) cmd_install ;;
update) cmd_update ;;
start) cmd_start ;;
stop) cmd_stop ;;
status) cmd_status ;;
open) cmd_open ;;
daemon) cmd_daemon ;;
uninstall) cmd_uninstall "${2:-}" ;;
*) usage ;;
esac
