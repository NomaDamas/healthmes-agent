#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$REPO_ROOT/data"
RUNTIME_DIR="$DATA_DIR/runtime"
HEALTHMES_PID="$RUNTIME_DIR/healthmes.pid"
OW_PID="$RUNTIME_DIR/open-wearables.pid"
WORKER_PID="$RUNTIME_DIR/open-wearables-worker.pid"
HEALTHMES_LOG="$RUNTIME_DIR/healthmes.log"
OW_LOG="$RUNTIME_DIR/open-wearables.log"
WORKER_LOG="$RUNTIME_DIR/open-wearables-worker.log"
DASHBOARD_URL="${HEALTHMES_DASHBOARD_URL:-http://127.0.0.1:${HEALTHMES_PORT:-8100}/sleep}"

info() { printf '[healthmes] %s\n' "$*"; }
die() { printf '[healthmes] %s\n' "$*" >&2; exit 1; }

pid_running() {
    local file=$1 pid
    [ -f "$file" ] || return 1
    pid="$(<"$file")"
    [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
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

stop_process() {
    local name=$1 pid_file=$2 pid
    if ! pid_running "$pid_file"; then
        rm -f "$pid_file"
        info "$name stopped"
        return
    fi
    pid="$(<"$pid_file")"
    kill "$pid"
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.2
    done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid"
    rm -f "$pid_file"
    info "$name stopped"
}

cmd_install() {
    bash "$REPO_ROOT/scripts/dev_mac.sh" setup
    info "installed; run: $0 start"
}

cmd_update() {
    git -C "$REPO_ROOT" diff --quiet || die "working tree has changes; commit or stash first"
    git -C "$REPO_ROOT" diff --cached --quiet || die "index has changes; commit or stash first"
    git -C "$REPO_ROOT" pull --ff-only
    bash "$REPO_ROOT/scripts/dev_mac.sh" setup
    info "updated"
}

cmd_start() {
    bash "$REPO_ROOT/scripts/dev_mac.sh" services-start
    start_process "Open Wearables" "$OW_PID" "$OW_LOG" \
        "exec bash '$REPO_ROOT/scripts/dev_mac.sh' ow"
    start_process "Open Wearables worker" "$WORKER_PID" "$WORKER_LOG" \
        "exec bash '$REPO_ROOT/scripts/dev_mac.sh' ow-worker"
    start_process "HealthMes" "$HEALTHMES_PID" "$HEALTHMES_LOG" \
        "exec bash '$REPO_ROOT/scripts/dev_mac.sh' run"
    info "dashboard: $DASHBOARD_URL"
}

cmd_stop() {
    stop_process "HealthMes" "$HEALTHMES_PID"
    stop_process "Open Wearables worker" "$WORKER_PID"
    stop_process "Open Wearables" "$OW_PID"
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
uninstall) cmd_uninstall "${2:-}" ;;
*) usage ;;
esac
