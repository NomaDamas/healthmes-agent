#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$REPO_ROOT/data"
RUNTIME_DIR="$DATA_DIR/runtime"
HEALTHMES_PID="$RUNTIME_DIR/healthmes.pid"
OW_PID="$RUNTIME_DIR/open-wearables.pid"
WORKER_PID="$RUNTIME_DIR/open-wearables-worker.pid"
BEAT_PID="$RUNTIME_DIR/open-wearables-beat.pid"
HERMES_DECISION_PID="$RUNTIME_DIR/hermes-decision.pid"
HERMES_DECISION_VENV="$RUNTIME_DIR/hermes-decision-venv"
HERMES_DECISION_STOP_BUDGET="$RUNTIME_DIR/hermes-decision-stop-budget"
HEALTHMES_LOG="$RUNTIME_DIR/healthmes.log"
OW_LOG="$RUNTIME_DIR/open-wearables.log"
WORKER_LOG="$RUNTIME_DIR/open-wearables-worker.log"
BEAT_LOG="$RUNTIME_DIR/open-wearables-beat.log"
HERMES_DECISION_LOG="$RUNTIME_DIR/hermes-decision.log"
DASHBOARD_URL="${HEALTHMES_DASHBOARD_URL:-http://127.0.0.1:${HEALTHMES_PORT:-8100}/sleep}"
DEV_MAC_SCRIPT="${HEALTHMES_DEV_MAC_SCRIPT:-$REPO_ROOT/scripts/dev_mac.sh}"
LAUNCH_AGENT_LABEL="com.healthmes.local"
LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"
LAUNCH_AGENT_PLIST="$LAUNCH_AGENT_DIR/$LAUNCH_AGENT_LABEL.plist"
LAUNCH_AGENT_TEMPLATE="$REPO_ROOT/config/$LAUNCH_AGENT_LABEL.plist.in"
LAUNCHCTL_BIN="${HEALTHMES_LAUNCHCTL_BIN:-launchctl}"
PS_BIN="${HEALTHMES_PS_BIN:-ps}"
KILL_BIN="${HEALTHMES_KILL_BIN:-/bin/kill}"
SLEEP_BIN="${HEALTHMES_SLEEP_BIN:-sleep}"
BASH_BIN="${HEALTHMES_BASH_BIN:-/bin/bash}"
UUIDGEN_BIN="${HEALTHMES_UUIDGEN_BIN:-uuidgen}"
MAX_DECISION_RUNTIME_DRAIN_SECONDS=315

info() { printf '[healthmes] %s\n' "$*"; }
die() { printf '[healthmes] %s\n' "$*" >&2; exit 1; }

identity_file() {
    printf '%s.identity\n' "$1"
}

clear_process_identity() {
    local pid_file=$1
    rm -f "$pid_file" "$(identity_file "$pid_file")"
}

valid_managed_pid() {
    local pid=$1
    [[ "$pid" =~ ^[0-9]+$ ]] && [ "$pid" -gt 1 ]
}

trim_whitespace() {
    local value=$1
    while [[ "$value" == [[:space:]]* ]]; do
        value="${value#?}"
    done
    while [[ "$value" == *[[:space:]] ]]; do
        value="${value%?}"
    done
    printf '%s\n' "$value"
}

ps_value() {
    local pid=$1 field=$2 value
    value="$("$PS_BIN" -ww -p "$pid" -o "$field=" 2>/dev/null)" || return 1
    value="$(trim_whitespace "$value")"
    [ -n "$value" ] || return 1
    printf '%s\n' "$value"
}

load_process_identity() {
    local pid_file=$1 file key value stored_pid
    file="$(identity_file "$pid_file")"
    [ -f "$pid_file" ] && [ -f "$file" ] || return 1

    PROCESS_PID=
    PROCESS_PGID=
    PROCESS_EXECUTABLE=
    PROCESS_START_TIME=
    PROCESS_NONCE=
    while IFS=$'\t' read -r key value; do
        case "$key" in
        pid) PROCESS_PID=$value ;;
        pgid) PROCESS_PGID=$value ;;
        executable) PROCESS_EXECUTABLE=$value ;;
        start_time) PROCESS_START_TIME=$value ;;
        nonce) PROCESS_NONCE=$value ;;
        esac
    done <"$file"
    stored_pid="$(<"$pid_file")"

    valid_managed_pid "$PROCESS_PID" \
        && [ "$stored_pid" = "$PROCESS_PID" ] \
        && [ "$PROCESS_PGID" = "$PROCESS_PID" ] \
        && [ "${PROCESS_EXECUTABLE##*/}" = "bash" ] \
        && [ -n "$PROCESS_START_TIME" ] \
        && [[ "$PROCESS_NONCE" =~ ^[A-Za-z0-9-]+$ ]]
}

load_process_snapshot() {
    local pid=$1
    valid_managed_pid "$pid" || return 1
    SNAPSHOT_PID="$(ps_value "$pid" pid)" || return 1
    SNAPSHOT_PGID="$(ps_value "$pid" pgid)" || return 1
    SNAPSHOT_EXECUTABLE="$(ps_value "$pid" comm)" || return 1
    SNAPSHOT_START_TIME="$(ps_value "$pid" lstart)" || return 1
    SNAPSHOT_COMMAND="$(ps_value "$pid" command)" || return 1
}

process_identity_matches() {
    local pid_file=$1 marker
    load_process_identity "$pid_file" || return 1
    load_process_snapshot "$PROCESS_PID" || return 1
    marker="healthmes_local.sh __service_runner $PROCESS_NONCE "

    [ "$SNAPSHOT_PID" = "$PROCESS_PID" ] \
        && [ "$SNAPSHOT_PGID" = "$PROCESS_PGID" ] \
        && [ "$SNAPSHOT_EXECUTABLE" = "$PROCESS_EXECUTABLE" ] \
        && [ "$SNAPSHOT_START_TIME" = "$PROCESS_START_TIME" ] \
        && [[ "$SNAPSHOT_COMMAND" == *"$marker"* ]]
}

write_process_identity() {
    local pid_file=$1 pid=$2 nonce=$3 file temp pid_temp
    file="$(identity_file "$pid_file")"
    temp="$(mktemp "$RUNTIME_DIR/.process-identity.XXXXXX")"
    pid_temp="$(mktemp "$RUNTIME_DIR/.process-pid.XXXXXX")"
    umask 077
    {
        printf 'pid\t%s\n' "$pid"
        printf 'pgid\t%s\n' "$SNAPSHOT_PGID"
        printf 'executable\t%s\n' "$SNAPSHOT_EXECUTABLE"
        printf 'start_time\t%s\n' "$SNAPSHOT_START_TIME"
        printf 'nonce\t%s\n' "$nonce"
    } >"$temp"
    printf '%s\n' "$pid" >"$pid_temp"
    mv "$temp" "$file"
    mv "$pid_temp" "$pid_file"
}

capture_process_identity() {
    local pid_file=$1 pid=$2 nonce=$3 marker
    load_process_snapshot "$pid" || return 1
    marker="healthmes_local.sh __service_runner $nonce "
    [ "$SNAPSHOT_PID" = "$pid" ] \
        && [ "$SNAPSHOT_PGID" = "$pid" ] \
        && [ "${SNAPSHOT_EXECUTABLE##*/}" = "bash" ] \
        && [[ "$SNAPSHOT_COMMAND" == *"$marker"* ]] \
        || return 1
    write_process_identity "$pid_file" "$pid" "$nonce"
}

pid_running() {
    process_identity_matches "$1"
}

new_service_nonce() {
    local nonce
    nonce="$("$UUIDGEN_BIN")" || return 1
    nonce="${nonce//-/}"
    [[ "$nonce" =~ ^[A-Za-z0-9]+$ ]] || return 1
    printf '%s\n' "$nonce"
}

run_service_runner() {
    local nonce=$1 command=$2 child status service_pid service_start_token
    [ -n "$nonce" ] && [ "${HEALTHMES_SERVICE_NONCE:-}" = "$nonce" ] \
        || die "invalid service runner nonce"
    service_pid=$$
    service_start_token="ps:$(ps_value "$service_pid" lstart)" \
        || die "failed to capture service runner start token"
    trap ':' INT TERM
    env \
        HEALTHMES_SERVICE_PID="$service_pid" \
        HEALTHMES_SERVICE_START_TOKEN="$service_start_token" \
        "$BASH_BIN" -lc "$command" &
    child=$!
    while true; do
        wait "$child" && return 0
        status=$?
        "$KILL_BIN" -0 "$child" 2>/dev/null || return "$status"
        "$SLEEP_BIN" 0.1
    done
}

start_process() {
    local name=$1 pid_file=$2 log_file=$3 command=$4 nonce pid
    if pid_running "$pid_file"; then
        info "$name already running (pid $PROCESS_PID)"
        return
    fi
    if [ -f "$pid_file" ] || [ -f "$(identity_file "$pid_file")" ]; then
        info "$name stale process identity ignored"
        clear_process_identity "$pid_file"
    fi
    mkdir -p "$RUNTIME_DIR"
    nonce="$(new_service_nonce)" || die "failed to generate $name service nonce"
    (
        cd "$REPO_ROOT"
        set -m
        nohup env HEALTHMES_SERVICE_NONCE="$nonce" \
            "$BASH_BIN" "$REPO_ROOT/scripts/healthmes_local.sh" \
            __service_runner "$nonce" "$command" >>"$log_file" 2>&1 &
        printf '%s\n' "$!" >"$pid_file"
        set +m
    )
    pid="$(<"$pid_file")"
    "$SLEEP_BIN" 1
    if ! capture_process_identity "$pid_file" "$pid" "$nonce"; then
        clear_process_identity "$pid_file"
        die "$name failed identity verification; see $log_file"
    fi
    info "$name started (pid $pid)"
}

signal_process_group() {
    local signal=$1 pid_file=$2 pid
    process_identity_matches "$pid_file" || return 1
    pid=$PROCESS_PID
    "$KILL_BIN" -s "$signal" "-$pid"
}

load_decision_runtime_stop_bounds() {
    local key value extra
    local version= drain_timeout= supervisor_pid=
    local supervisor_start_token= service_nonce=
    local seen_version= seen_drain_timeout= seen_supervisor_pid=
    local seen_supervisor_start_token= seen_service_nonce=
    DECISION_RUNTIME_TERM_WAIT_SECONDS=$MAX_DECISION_RUNTIME_DRAIN_SECONDS
    DECISION_RUNTIME_KILL_WAIT_SECONDS=1
    if [ ! -f "$HERMES_DECISION_STOP_BUDGET" ]; then
        return 0
    fi
    process_identity_matches "$HERMES_DECISION_PID" || return 0
    while IFS=$'\t' read -r key value extra; do
        [ -z "$extra" ] || {
            info "ignoring malformed decision runtime stop budget"
            return 0
        }
        case "$key" in
        version)
            [ -z "$seen_version" ] || {
                info "ignoring malformed decision runtime stop budget"
                return 0
            }
            version=$value
            seen_version=1
            ;;
        drain_timeout_seconds)
            [ -z "$seen_drain_timeout" ] || {
                info "ignoring malformed decision runtime stop budget"
                return 0
            }
            drain_timeout=$value
            seen_drain_timeout=1
            ;;
        supervisor_pid)
            [ -z "$seen_supervisor_pid" ] || {
                info "ignoring malformed decision runtime stop budget"
                return 0
            }
            supervisor_pid=$value
            seen_supervisor_pid=1
            ;;
        supervisor_start_token)
            [ -z "$seen_supervisor_start_token" ] || {
                info "ignoring malformed decision runtime stop budget"
                return 0
            }
            supervisor_start_token=$value
            seen_supervisor_start_token=1
            ;;
        service_nonce)
            [ -z "$seen_service_nonce" ] || {
                info "ignoring malformed decision runtime stop budget"
                return 0
            }
            service_nonce=$value
            seen_service_nonce=1
            ;;
        *)
            info "ignoring malformed decision runtime stop budget"
            return 0
            ;;
        esac
    done <"$HERMES_DECISION_STOP_BUDGET"
    if [ "$version" != 1 ] \
        || ! [[ "$drain_timeout" =~ ^[1-9][0-9]*$ ]] \
        || [ "$drain_timeout" -gt "$MAX_DECISION_RUNTIME_DRAIN_SECONDS" ] \
        || [ "$supervisor_pid" != "$PROCESS_PID" ] \
        || [ "$supervisor_start_token" != "ps:$PROCESS_START_TIME" ] \
        || [ "$service_nonce" != "$PROCESS_NONCE" ]; then
        info "ignoring stale or invalid decision runtime stop budget"
        return 0
    fi
    DECISION_RUNTIME_TERM_WAIT_SECONDS=$drain_timeout
}

wait_for_process_exit() {
    local pid_file=$1 timeout_seconds=$2 polls
    polls=$timeout_seconds
    while [ "$polls" -gt 0 ]; do
        process_identity_matches "$pid_file" || return 0
        "$SLEEP_BIN" 1
        polls=$((polls - 1))
    done
    ! process_identity_matches "$pid_file"
}

stop_process() {
    local name=$1 pid_file=$2
    local term_wait_seconds=${3:-2}
    local kill_wait_seconds=${4:-1}
    local allow_force_kill=${5:-true}
    if ! process_identity_matches "$pid_file"; then
        clear_process_identity "$pid_file"
        info "$name stopped"
        return
    fi
    signal_process_group TERM "$pid_file" 2>/dev/null || true
    if wait_for_process_exit "$pid_file" "$term_wait_seconds"; then
        clear_process_identity "$pid_file"
        info "$name stopped"
        return
    fi
    if [ "$allow_force_kill" != true ]; then
        die "$name did not stop within ${term_wait_seconds}s; refusing to orphan its child process group"
    fi
    signal_process_group KILL "$pid_file" 2>/dev/null || true
    if ! wait_for_process_exit "$pid_file" "$kill_wait_seconds"; then
        die "$name remained alive ${kill_wait_seconds}s after SIGKILL"
    fi
    clear_process_identity "$pid_file"
    info "$name stopped"
}

stop_decision_runtime() {
    load_decision_runtime_stop_bounds
    # Hermes owns a separate child process group. Let the supervisor perform
    # its bounded TERM/KILL cleanup; killing only the outer group can orphan it.
    stop_process \
        "Hermes decision runtime" \
        "$HERMES_DECISION_PID" \
        "$DECISION_RUNTIME_TERM_WAIT_SECONDS" \
        "$DECISION_RUNTIME_KILL_WAIT_SECONDS" \
        false
    rm -f "$HERMES_DECISION_STOP_BUDGET"
}

load_runtime_env() {
    set -a
    [ -f "$REPO_ROOT/.env" ] && source "$REPO_ROOT/.env"
    [ -f "$REPO_ROOT/.env.local" ] && source "$REPO_ROOT/.env.local"
    [ -f "$REPO_ROOT/config/open-wearables.env" ] \
        && source "$REPO_ROOT/config/open-wearables.env"
    set +a
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

decision_runtime_configured() {
    local model provider
    model="$(trim_whitespace "${HEALTHMES_DECISION_HERMES_MODEL:-}")"
    provider="$(trim_whitespace "${HEALTHMES_DECISION_HERMES_PROVIDER:-}")"
    if [ -z "$model" ] && [ -z "$provider" ]; then
        return 1
    fi
    [ -n "$model" ] && [ -n "$provider" ] \
        || die "decision runtime requires both HEALTHMES_DECISION_HERMES_MODEL and HEALTHMES_DECISION_HERMES_PROVIDER"
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
    "$LAUNCHCTL_BIN" disable "gui/$UID/$LAUNCH_AGENT_LABEL" >/dev/null 2>&1 || true
    "$LAUNCHCTL_BIN" bootout "gui/$UID/$LAUNCH_AGENT_LABEL" >/dev/null 2>&1 || true
    bootstrap_log="$(mktemp)"
    bootstrapped=false
    "$LAUNCHCTL_BIN" enable "gui/$UID/$LAUNCH_AGENT_LABEL"
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if "$LAUNCHCTL_BIN" bootstrap "gui/$UID" "$LAUNCH_AGENT_PLIST" \
            2>"$bootstrap_log"; then
            bootstrapped=true
            break
        fi
        "$SLEEP_BIN" 0.5
    done
    if [ "$bootstrapped" != true ]; then
        cat "$bootstrap_log" >&2
        rm -f "$bootstrap_log"
        die "failed to register login launch agent"
    fi
    rm -f "$bootstrap_log"
    "$LAUNCHCTL_BIN" kickstart -k "gui/$UID/$LAUNCH_AGENT_LABEL"
    info "login launch agent installed ($LAUNCH_AGENT_LABEL)"
}

stop_launch_agent() {
    "$LAUNCHCTL_BIN" disable "gui/$UID/$LAUNCH_AGENT_LABEL" >/dev/null 2>&1 || true
    "$LAUNCHCTL_BIN" bootout "gui/$UID/$LAUNCH_AGENT_LABEL" >/dev/null 2>&1 || true
}

start_launch_agent() {
    "$LAUNCHCTL_BIN" enable "gui/$UID/$LAUNCH_AGENT_LABEL"
    if ! "$LAUNCHCTL_BIN" print "gui/$UID/$LAUNCH_AGENT_LABEL" \
        >/dev/null 2>&1; then
        "$LAUNCHCTL_BIN" bootstrap "gui/$UID" "$LAUNCH_AGENT_PLIST"
    fi
    "$LAUNCHCTL_BIN" kickstart -k "gui/$UID/$LAUNCH_AGENT_LABEL"
    info "login launch agent started ($LAUNCH_AGENT_LABEL)"
}

uninstall_launch_agent() {
    stop_launch_agent
    if [ -f "$LAUNCH_AGENT_PLIST" ]; then
        rm "$LAUNCH_AGENT_PLIST"
        info "login launch agent removed"
    fi
}

cmd_install() {
    stop_launch_agent
    load_runtime_env
    stop_decision_runtime
    bash "$DEV_MAC_SCRIPT" setup
    install_launch_agent
    info "installed and configured to start at login"
}

cmd_update() {
    local restart_launch_agent=false
    git -C "$REPO_ROOT" diff --quiet || die "working tree has changes; commit or stash first"
    git -C "$REPO_ROOT" diff --cached --quiet || die "index has changes; commit or stash first"
    if [ -f "$LAUNCH_AGENT_PLIST" ]; then
        restart_launch_agent=true
        stop_launch_agent
    fi
    load_runtime_env
    stop_decision_runtime
    git -C "$REPO_ROOT" pull --ff-only
    bash "$DEV_MAC_SCRIPT" setup
    if [ "$restart_launch_agent" = true ]; then
        start_launch_agent
    fi
    info "updated"
}

start_apps() {
    local decision_home= quoted_budget= quoted_home= quoted_vendor=
    local decision_enabled=false
    load_runtime_env
    # Stop the old in-memory code before uv can replace the interpreter or
    # bootstrap can publish runtime intent for changed HealthMes sources.
    stop_decision_runtime
    bash "$DEV_MAC_SCRIPT" services-start
    resolve_ow_api_key
    if decision_runtime_configured; then
        decision_enabled=true
        mkdir -p "$RUNTIME_DIR"
        info "syncing dedicated Hermes decision runtime"
        UV_PROJECT_ENVIRONMENT="$HERMES_DECISION_VENV" \
            uv sync --frozen --no-dev \
            --directory "$REPO_ROOT/vendor/hermes-agent"
        uv run python "$REPO_ROOT/scripts/bootstrap.py" --mode native
        load_runtime_env
        [ -n "${HEALTHMES_DECISION_HERMES_PROFILE_PATH:-}" ] \
            || die "bootstrap did not configure the decision profile"
        decision_home="$(dirname "$HEALTHMES_DECISION_HERMES_PROFILE_PATH")"
        printf -v quoted_budget '%q' "$HERMES_DECISION_STOP_BUDGET"
        printf -v quoted_home '%q' "$decision_home"
        printf -v quoted_vendor '%q' "$REPO_ROOT/vendor/hermes-agent"
    else
        info "Hermes decision runtime disabled (model/provider not configured)"
    fi
    start_process "Open Wearables" "$OW_PID" "$OW_LOG" \
        "exec bash '$REPO_ROOT/scripts/dev_mac.sh' ow"
    start_process "Open Wearables worker" "$WORKER_PID" "$WORKER_LOG" \
        "exec bash '$REPO_ROOT/scripts/dev_mac.sh' ow-worker"
    start_process "Open Wearables beat" "$BEAT_PID" "$BEAT_LOG" \
        "exec bash '$REPO_ROOT/scripts/dev_mac.sh' ow-beat"
    start_process "HealthMes" "$HEALTHMES_PID" "$HEALTHMES_LOG" \
        "exec bash '$REPO_ROOT/scripts/dev_mac.sh' run"
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
        if curl --fail --silent --max-time 1 \
            "http://127.0.0.1:${HEALTHMES_PORT:-8100}/health" \
            >/dev/null; then
            break
        fi
        "$SLEEP_BIN" 1
    done
    curl --fail --silent --max-time 1 \
        "http://127.0.0.1:${HEALTHMES_PORT:-8100}/health" \
        >/dev/null \
        || die "HealthMes did not become ready; see $HEALTHMES_LOG"
    if [ "$decision_enabled" = true ]; then
        start_process "Hermes decision runtime" \
            "$HERMES_DECISION_PID" "$HERMES_DECISION_LOG" \
            "exec env HERMES_HOME=$quoted_home uv run python -m healthmes.hermes_runtime_supervisor --hermes-home $quoted_home --vendor-root $quoted_vendor --shutdown-budget-path $quoted_budget"
        for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
            if curl --fail --silent --max-time 1 \
                "http://127.0.0.1:${HEALTHMES_DECISION_RUNTIME_PORT:-8645}/healthmes/runtime-health" \
                >/dev/null; then
                break
            fi
            "$SLEEP_BIN" 1
        done
        curl --fail --silent --max-time 1 \
            "http://127.0.0.1:${HEALTHMES_DECISION_RUNTIME_PORT:-8645}/healthmes/runtime-health" \
            >/dev/null \
            || die "Hermes decision runtime did not become ready; see $HERMES_DECISION_LOG"
    fi
    info "dashboard: $DASHBOARD_URL"
}

cmd_start() {
    if [ -f "$LAUNCH_AGENT_PLIST" ]; then
        start_launch_agent
        return
    fi
    start_apps
}

cmd_daemon() {
    trap 'stop_apps; exit 0' INT TERM
    start_apps
    while true; do
        "$SLEEP_BIN" 5
        load_runtime_env
        if ! pid_running "$HEALTHMES_PID" \
            || ! pid_running "$OW_PID" \
            || ! pid_running "$WORKER_PID" \
            || ! pid_running "$BEAT_PID" \
            || { decision_runtime_configured \
                && ! pid_running "$HERMES_DECISION_PID"; }; then
            start_apps
        fi
    done
}

stop_apps() {
    load_runtime_env
    # Drain decisions while HealthMes MCP remains available to in-flight turns.
    stop_decision_runtime
    stop_process "HealthMes" "$HEALTHMES_PID"
    stop_process "Open Wearables beat" "$BEAT_PID"
    stop_process "Open Wearables worker" "$WORKER_PID"
    stop_process "Open Wearables" "$OW_PID"
}

cmd_stop() {
    stop_launch_agent
    stop_apps
    bash "$DEV_MAC_SCRIPT" services-stop
}

service_status() {
    local name=$1 file=$2
    if pid_running "$file"; then
        info "$name: running (pid $PROCESS_PID)"
    else
        info "$name: stopped"
    fi
}

cmd_status() {
    service_status "Hermes decision runtime" "$HERMES_DECISION_PID"
    service_status "HealthMes" "$HEALTHMES_PID"
    service_status "Open Wearables" "$OW_PID"
    service_status "Open Wearables worker" "$WORKER_PID"
    service_status "Open Wearables beat" "$BEAT_PID"
    bash "$DEV_MAC_SCRIPT" services-status
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
__service_runner) run_service_runner "${2:-}" "${3:-}" ;;
*) usage ;;
esac
