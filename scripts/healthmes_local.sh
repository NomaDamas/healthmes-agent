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
HERMES_DECISION_STARTUP_LEASE="$RUNTIME_DIR/hermes-decision-startup-lease"
HERMES_DECISION_LIFECYCLE_LOCK="$RUNTIME_DIR/hermes-decision-lifecycle-lock"
HERMES_DECISION_TRANSITION_LOCK="$DATA_DIR/.hermes-decision-runtime-transition.lock"
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
DATE_BIN="${HEALTHMES_DATE_BIN:-date}"
RUNTIME_PYTHON_BIN="${HEALTHMES_RUNTIME_PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
NATIVE_IDENTITY_HELPER="${HEALTHMES_NATIVE_IDENTITY_HELPER:-$REPO_ROOT/scripts/runtime_native_identity.py}"
NATIVE_IDENTITY_PYTHON_BIN="${HEALTHMES_NATIVE_IDENTITY_PYTHON_BIN:-}"
MAX_DECISION_RUNTIME_DRAIN_SECONDS=315
DECISION_RUNTIME_SHUTDOWN_MARGIN_SECONDS=2
DECISION_RUNTIME_LIFECYCLE_LOCK_WAIT_SECONDS=10
DECISION_RUNTIME_LIFECYCLE_LOCK_STALE_GRACE_SECONDS=2
DECISION_RUNTIME_STARTUP_RECOVERY_GRACE_SECONDS=3
DECISION_RUNTIME_PS_PROBE_TIMEOUT_SECONDS=1
MAX_DECISION_RUNTIME_STOP_BUDGET_BYTES=1024
DECISION_RUNTIME_LIFECYCLE_RECORD_VERSION=2
DECISION_RUNTIME_LIFECYCLE_CONTRACT_VERSION=2
MAX_DECISION_RUNTIME_TERM_WAIT_SECONDS=$((
    MAX_DECISION_RUNTIME_DRAIN_SECONDS
    + DECISION_RUNTIME_SHUTDOWN_MARGIN_SECONDS
))
DECISION_RUNTIME_LIFECYCLE_LOCK_HELD=false
DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_PID=
DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_START_TOKEN=
DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_NONCE=
DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_OPERATION=
DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_PHASE=
DECISION_RUNTIME_LIFECYCLE_INITIAL_SCRIPT_SHA256=
DECISION_RUNTIME_DURABLE_MUTATION_STARTED=false
DECISION_RUNTIME_POST_RELEASE_RMDIR_RUNTIME=false
DECISION_RUNTIME_POST_RELEASE_RMDIR_DATA=false

info() { printf '[healthmes] %s\n' "$*"; }
die() { printf '[healthmes] %s\n' "$*" >&2; exit 1; }

identity_file() {
    printf '%s.identity\n' "$1"
}

clear_process_identity() {
    local pid_file=$1
    rm -f "$pid_file" "$(identity_file "$pid_file")"
}

write_unverified_process_pid() {
    local pid_file=$1 pid=$2 temp
    temp="$(mktemp "$RUNTIME_DIR/.process-pid.XXXXXX")"
    if ! printf '%s\n' "$pid" >"$temp" \
        || ! mv "$temp" "$pid_file"; then
        rm -f "$temp"
        return 1
    fi
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
    local pid=$1 field=$2 deadline=${3:-} timeout value
    timeout="$(bounded_ps_timeout_seconds "$deadline")" || return 1
    value="$(run_native_identity_helper \
        ps-value \
        --ps-bin "$PS_BIN" \
        --pid "$pid" \
        --field "$field" \
        --timeout-seconds "$timeout" \
        2>/dev/null)" || return 1
    value="$(trim_whitespace "$value")"
    [ -n "$value" ] || return 1
    printf '%s\n' "$value"
}

bounded_ps_timeout_seconds() {
    local deadline=${1:-} remaining
    if [ -z "$deadline" ]; then
        printf '%s\n' "$DECISION_RUNTIME_PS_PROBE_TIMEOUT_SECONDS"
        return
    fi
    [[ "$deadline" =~ ^[0-9]+$ ]] || return 5
    remaining=$((deadline - SECONDS))
    [ "$remaining" -gt 0 ] || return 5
    if [ "$remaining" -lt "$DECISION_RUNTIME_PS_PROBE_TIMEOUT_SECONDS" ]; then
        printf '%s\n' "$remaining"
    else
        printf '%s\n' "$DECISION_RUNTIME_PS_PROBE_TIMEOUT_SECONDS"
    fi
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
    local pid=$1 deadline=${2:-} timeout output key value extra status
    local seen_pid= seen_pgid= seen_comm= seen_lstart= seen_command=
    valid_managed_pid "$pid" || return 5
    timeout="$(bounded_ps_timeout_seconds "$deadline")" || return 5
    if output="$(run_native_identity_helper \
        ps-snapshot \
        --ps-bin "$PS_BIN" \
        --pid "$pid" \
        --timeout-seconds "$timeout" \
        2>/dev/null)"; then
        :
    else
        status=$?
        [ "$status" -eq 3 ] && return 3
        return 5
    fi
    SNAPSHOT_PID=
    SNAPSHOT_PGID=
    SNAPSHOT_EXECUTABLE=
    SNAPSHOT_START_TIME=
    SNAPSHOT_COMMAND=
    while IFS=$'\t' read -r key value extra; do
        [ -z "$extra" ] || return 5
        case "$key" in
        pid)
            [ -z "$seen_pid" ] || return 5
            SNAPSHOT_PID=$value
            seen_pid=1
            ;;
        pgid)
            [ -z "$seen_pgid" ] || return 5
            SNAPSHOT_PGID=$value
            seen_pgid=1
            ;;
        comm)
            [ -z "$seen_comm" ] || return 5
            SNAPSHOT_EXECUTABLE=$value
            seen_comm=1
            ;;
        lstart)
            [ -z "$seen_lstart" ] || return 5
            SNAPSHOT_START_TIME=$value
            seen_lstart=1
            ;;
        command)
            [ -z "$seen_command" ] || return 5
            SNAPSHOT_COMMAND=$value
            seen_command=1
            ;;
        *)
            return 5
            ;;
        esac
    done <<<"$output"
    [ -n "$seen_pid" ] \
        && [ -n "$seen_pgid" ] \
        && [ -n "$seen_comm" ] \
        && [ -n "$seen_lstart" ] \
        && [ -n "$seen_command" ] \
        || return 5
}

process_identity_matches() {
    local pid_file=$1 deadline=${2:-} marker status file
    file="$(identity_file "$pid_file")"
    if load_process_identity "$pid_file"; then
        :
    else
        if [ ! -e "$pid_file" ] && [ ! -L "$pid_file" ] \
            && [ ! -e "$file" ] && [ ! -L "$file" ]; then
            return 3
        fi
        if [ -f "$pid_file" ] && [ ! -L "$pid_file" ]; then
            local stored_pid
            stored_pid="$(<"$pid_file")"
            if [ "$stored_pid" = 0 ] || [ "$stored_pid" = 1 ]; then
                return 4
            fi
        fi
        return 5
    fi
    if load_process_snapshot "$PROCESS_PID" "$deadline"; then
        :
    else
        status=$?
        return "$status"
    fi
    marker="healthmes_local.sh __service_runner $PROCESS_NONCE "

    if [ "$SNAPSHOT_PID" = "$PROCESS_PID" ] \
        && [ "$SNAPSHOT_PGID" = "$PROCESS_PGID" ] \
        && [ "$SNAPSHOT_EXECUTABLE" = "$PROCESS_EXECUTABLE" ] \
        && [ "$SNAPSHOT_START_TIME" = "$PROCESS_START_TIME" ] \
        && [[ "$SNAPSHOT_COMMAND" == *"$marker"* ]]; then
        return 0
    fi
    return 4
}

captured_process_identity_matches() {
    local pid=$1 executable=$2 start_time=$3 nonce=$4
    local deadline=${5:-} marker status
    if load_process_snapshot "$pid" "$deadline"; then
        :
    else
        status=$?
        return "$status"
    fi
    marker="healthmes_local.sh __service_runner $nonce "

    if [ "$SNAPSHOT_PID" = "$pid" ] \
        && [ "$SNAPSHOT_PGID" = "$pid" ] \
        && [ "$SNAPSHOT_EXECUTABLE" = "$executable" ] \
        && [ "$SNAPSHOT_START_TIME" = "$start_time" ] \
        && [[ "$SNAPSHOT_COMMAND" == *"$marker"* ]]; then
        return 0
    fi
    return 4
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
    local pid_file=$1 pid=$2 nonce=$3 deadline=${4:-} marker status
    if load_process_snapshot "$pid" "$deadline"; then
        :
    else
        status=$?
        return "$status"
    fi
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

current_epoch() {
    local value
    value="$("$DATE_BIN" +%s 2>/dev/null)" || return 1
    [[ "$value" =~ ^[1-9][0-9]*$ ]] || return 1
    printf '%s\n' "$value"
}

resolve_native_identity_python() {
    if [ -n "$NATIVE_IDENTITY_PYTHON_BIN" ]; then
        [ -x "$NATIVE_IDENTITY_PYTHON_BIN" ] || return 1
        printf '%s\n' "$NATIVE_IDENTITY_PYTHON_BIN"
        return
    fi
    if [ -x "$RUNTIME_PYTHON_BIN" ]; then
        printf '%s\n' "$RUNTIME_PYTHON_BIN"
        return
    fi
    command -v python3 2>/dev/null || return 1
}

run_native_identity_helper() {
    local python_bin
    [ -f "$NATIVE_IDENTITY_HELPER" ] \
        && [ ! -L "$NATIVE_IDENTITY_HELPER" ] \
        || return 5
    python_bin="$(resolve_native_identity_python)" || return 5
    "$python_bin" "$NATIVE_IDENTITY_HELPER" "$@"
}

capture_native_process_start_token() {
    local pid=$1 token
    NATIVE_PROCESS_START_TOKEN=
    token="$(run_native_identity_helper capture "$pid")" || return $?
    [[ "$token" =~ ^(linux:[1-9][0-9]*|darwin:[1-9][0-9]*:[0-9]{6})$ ]] \
        || return 5
    NATIVE_PROCESS_START_TOKEN=$token
}

native_process_start_token_status() {
    local pid=$1 expected_start_token=$2 status
    if run_native_identity_helper check "$pid" "$expected_start_token"; then
        return 0
    else
        status=$?
    fi
    case "$status" in
    3 | 4) return 3 ;;
    *) return 5 ;;
    esac
}

current_lifecycle_script_sha256() {
    local digest
    digest="$(run_native_identity_helper \
        sha256 "$REPO_ROOT/scripts/healthmes_local.sh")" || return $?
    [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || return 5
    printf '%s\n' "$digest"
}

initialize_lifecycle_script_generation() {
    [ -z "$DECISION_RUNTIME_LIFECYCLE_INITIAL_SCRIPT_SHA256" ] \
        || return 0
    DECISION_RUNTIME_LIFECYCLE_INITIAL_SCRIPT_SHA256="$(
        current_lifecycle_script_sha256
    )" || die "decision runtime lifecycle script identity is unavailable"
}

assert_lifecycle_script_generation_unchanged() {
    local current
    initialize_lifecycle_script_generation
    current="$(current_lifecycle_script_sha256)" \
        || die "decision runtime lifecycle script identity became unreadable"
    [ "$current" = "$DECISION_RUNTIME_LIFECYCLE_INITIAL_SCRIPT_SHA256" ] \
        || die "healthmes_local.sh changed while this command waited for the lifecycle lock; rerun the command with the current script"
}

probe_ps_value() {
    local pid=$1 field=$2 deadline=${3:-} timeout output status
    PROBED_PS_VALUE=
    timeout="$(bounded_ps_timeout_seconds "$deadline")" || return 5
    if output="$(run_native_identity_helper \
        ps-value \
        --ps-bin "$PS_BIN" \
        --pid "$pid" \
        --field "$field" \
        --timeout-seconds "$timeout" \
        2>/dev/null)"; then
        :
    else
        status=$?
        [ "$status" -eq 3 ] && return 3
        return 5
    fi
    output="$(trim_whitespace "$output")"
    [ -n "$output" ] \
        && [[ "$output" != *$'\n'* ]] \
        && [[ "$output" != *$'\t'* ]] \
        || return 5
    PROBED_PS_VALUE=$output
}

process_start_token_status() {
    local pid=$1 expected_start_token=$2 deadline=${3:-} status
    valid_managed_pid "$pid" || return 5
    case "$expected_start_token" in
    linux:* | darwin:*)
        native_process_start_token_status "$pid" "$expected_start_token"
        return
        ;;
    ps:*) ;;
    *) return 5 ;;
    esac
    if probe_ps_value "$pid" pid "$deadline"; then
        [ "$PROBED_PS_VALUE" = "$pid" ] || return 5
    else
        status=$?
        [ "$status" -eq 3 ] && return 3
        return 5
    fi
    if probe_ps_value "$pid" lstart "$deadline"; then
        [ "ps:$PROBED_PS_VALUE" = "$expected_start_token" ] \
            && return 0
        # Legacy ps tokens are timezone/locale formatted. A mismatch while
        # the PID exists is unknown, not proof that the former owner exited.
        return 5
    else
        status=$?
        [ "$status" -eq 3 ] && return 3
        return 5
    fi
}

epoch_age_at_least() {
    local timestamp=$1 minimum_age=$2 now
    now="$(current_epoch)" || return 2
    [ "$now" -ge "$timestamp" ] || return 2
    [ $((now - timestamp)) -ge "$minimum_age" ]
}

rename_directory_exclusive() {
    local source=$1 target=$2 expected_record_sha256=${3:-}
    local -a arguments=(
        rename-exclusive
        "$source"
        "$target"
        --lock-path
        "$HERMES_DECISION_TRANSITION_LOCK"
    )
    if [ -n "$expected_record_sha256" ]; then
        arguments+=(
            --expected-record-sha256
            "$expected_record_sha256"
        )
    fi
    run_native_identity_helper "${arguments[@]}"
}

managed_record_sha256() {
    run_native_identity_helper sha256 "$1"
}

replace_managed_record_generation() {
    local source=$1 target=$2 expected_record_sha256=$3
    run_native_identity_helper \
        replace-record \
        "$source" \
        "$target" \
        --lock-path "$HERMES_DECISION_TRANSITION_LOCK" \
        --expected-record-sha256 "$expected_record_sha256"
}

publish_interrupted_managed_record() {
    local source=$1 target=$2 expected_source_sha256=$3
    run_native_identity_helper \
        publish-record \
        "$source" \
        "$target" \
        --lock-path "$HERMES_DECISION_TRANSITION_LOCK" \
        --expected-source-sha256 "$expected_source_sha256"
}

record_directory_has_only_managed_entries() {
    local directory=$1
    [ -d "$directory" ] && [ ! -L "$directory" ] || return 1
    (
        local entry name
        shopt -s dotglob nullglob
        for entry in "$directory"/*; do
            name=${entry##*/}
            case "$name" in
            record | .record.*)
                [ -f "$entry" ] && [ ! -L "$entry" ] || exit 1
                ;;
            *)
                exit 1
                ;;
            esac
        done
    )
}

remove_managed_record_directory() {
    local directory=$1
    record_directory_has_only_managed_entries "$directory" || return 1
    (
        local entry
        shopt -s dotglob nullglob
        for entry in "$directory"/*; do
            rm -f -- "$entry" || exit 1
        done
    ) || return 1
    rmdir "$directory"
}

allocate_absent_runtime_directory_path() {
    local template=$1 candidate
    candidate="$(mktemp -d "$RUNTIME_DIR/$template")" || return 1
    rmdir "$candidate" || return 1
    printf '%s\n' "$candidate"
}

retire_managed_record_directory() {
    local directory=$1 template=$2 expected_record_sha256=$3
    local retired status attempts=0
    record_directory_has_only_managed_entries "$directory" || return 1
    while [ "$attempts" -lt 3 ]; do
        retired="$(allocate_absent_runtime_directory_path "$template")" \
            || return 1
        if rename_directory_exclusive \
            "$directory" "$retired" "$expected_record_sha256"; then
            if ! remove_managed_record_directory "$retired"; then
                info "preserving retired runtime record directory after cleanup failure: $retired"
            fi
            return 0
        else
            status=$?
        fi
        [ "$status" -eq 6 ] || return 1
        attempts=$((attempts + 1))
    done
    return 1
}

decision_runtime_lifecycle_lock_record() {
    printf '%s/record\n' "$HERMES_DECISION_LIFECYCLE_LOCK"
}

decision_runtime_lifecycle_lock_exists() {
    [ -e "$HERMES_DECISION_LIFECYCLE_LOCK" ] \
        || [ -L "$HERMES_DECISION_LIFECYCLE_LOCK" ]
}

load_decision_runtime_lifecycle_lock() {
    local record_override=${1:-} record key value extra
    local version= operation= phase= owner_pid= owner_start_token=
    local owner_nonce= acquired_at_epoch= updated_at_epoch=
    local script_contract_version= script_sha256=
    local seen_version= seen_operation= seen_phase= seen_owner_pid=
    local seen_owner_start_token= seen_owner_nonce=
    local seen_acquired_at_epoch= seen_updated_at_epoch=
    local seen_script_contract_version= seen_script_sha256=

    DECISION_RUNTIME_LIFECYCLE_LOCK_STATUS=missing
    DECISION_RUNTIME_LIFECYCLE_LOCK_VERSION=
    DECISION_RUNTIME_LIFECYCLE_LOCK_OPERATION=
    DECISION_RUNTIME_LIFECYCLE_LOCK_PHASE=
    DECISION_RUNTIME_LIFECYCLE_LOCK_PID=
    DECISION_RUNTIME_LIFECYCLE_LOCK_START_TOKEN=
    DECISION_RUNTIME_LIFECYCLE_LOCK_NONCE=
    DECISION_RUNTIME_LIFECYCLE_LOCK_ACQUIRED_AT=
    DECISION_RUNTIME_LIFECYCLE_LOCK_UPDATED_AT=
    DECISION_RUNTIME_LIFECYCLE_LOCK_SCRIPT_CONTRACT_VERSION=
    DECISION_RUNTIME_LIFECYCLE_LOCK_SCRIPT_SHA256=
    DECISION_RUNTIME_LIFECYCLE_LOCK_STATUS=invalid
    if [ -n "$record_override" ]; then
        record=$record_override
    else
        decision_runtime_lifecycle_lock_exists || {
            DECISION_RUNTIME_LIFECYCLE_LOCK_STATUS=missing
            return 0
        }
        [ -d "$HERMES_DECISION_LIFECYCLE_LOCK" ] \
            && [ ! -L "$HERMES_DECISION_LIFECYCLE_LOCK" ] \
            || return 0
        record="$(decision_runtime_lifecycle_lock_record)"
    fi
    [ -f "$record" ] && [ ! -L "$record" ] || return 0

    while IFS=$'\t' read -r key value extra; do
        [ -z "$extra" ] || return 0
        case "$key" in
        version)
            [ -z "$seen_version" ] || return 0
            version=$value
            seen_version=1
            ;;
        operation)
            [ -z "$seen_operation" ] || return 0
            operation=$value
            seen_operation=1
            ;;
        phase)
            [ -z "$seen_phase" ] || return 0
            phase=$value
            seen_phase=1
            ;;
        owner_pid)
            [ -z "$seen_owner_pid" ] || return 0
            owner_pid=$value
            seen_owner_pid=1
            ;;
        owner_start_token)
            [ -z "$seen_owner_start_token" ] || return 0
            owner_start_token=$value
            seen_owner_start_token=1
            ;;
        owner_nonce)
            [ -z "$seen_owner_nonce" ] || return 0
            owner_nonce=$value
            seen_owner_nonce=1
            ;;
        acquired_at_epoch)
            [ -z "$seen_acquired_at_epoch" ] || return 0
            acquired_at_epoch=$value
            seen_acquired_at_epoch=1
            ;;
        updated_at_epoch)
            [ -z "$seen_updated_at_epoch" ] || return 0
            updated_at_epoch=$value
            seen_updated_at_epoch=1
            ;;
        script_contract_version)
            [ -z "$seen_script_contract_version" ] || return 0
            script_contract_version=$value
            seen_script_contract_version=1
            ;;
        script_sha256)
            [ -z "$seen_script_sha256" ] || return 0
            script_sha256=$value
            seen_script_sha256=1
            ;;
        *)
            return 0
            ;;
        esac
    done <"$record"

    if [ "$version" = 1 ]; then
        [ -n "$seen_version" ] \
            && [ -n "$seen_operation" ] \
            && [ -n "$seen_owner_pid" ] \
            && [ -n "$seen_owner_start_token" ] \
            && [ -n "$seen_owner_nonce" ] \
            && [ -n "$seen_acquired_at_epoch" ] \
            && [ -z "$seen_phase$seen_updated_at_epoch" ] \
            && [ -z "$seen_script_contract_version$seen_script_sha256" ] \
            && [[ "$operation" =~ ^(start|stop|update|install)$ ]] \
            && valid_managed_pid "$owner_pid" \
            && [[ "$owner_start_token" == ps:* ]] \
            && [[ "$owner_nonce" =~ ^[A-Za-z0-9-]+$ ]] \
            && [[ "$acquired_at_epoch" =~ ^[1-9][0-9]*$ ]] \
            || return 0
        phase=legacy
        updated_at_epoch=$acquired_at_epoch
    else
        [ "$version" = "$DECISION_RUNTIME_LIFECYCLE_RECORD_VERSION" ] \
            && [ -n "$seen_version" ] \
            && [ -n "$seen_operation" ] \
            && [ -n "$seen_phase" ] \
            && [ -n "$seen_owner_pid" ] \
            && [ -n "$seen_owner_start_token" ] \
            && [ -n "$seen_owner_nonce" ] \
            && [ -n "$seen_acquired_at_epoch" ] \
            && [ -n "$seen_updated_at_epoch" ] \
            && [ -n "$seen_script_contract_version" ] \
            && [ -n "$seen_script_sha256" ] \
            && [[ "$operation" =~ ^(start|stop|update|install|uninstall)$ ]] \
            && [[ "$phase" =~ ^(acquired|preflight|stopping|pulling|setup|restarting|unloading|services_stop|cleanup|complete|repair_required)$ ]] \
            && valid_managed_pid "$owner_pid" \
            && [[ "$owner_start_token" =~ ^(linux:[1-9][0-9]*|darwin:[1-9][0-9]*:[0-9]{6})$ ]] \
            && [[ "$owner_nonce" =~ ^[A-Za-z0-9-]+$ ]] \
            && [[ "$acquired_at_epoch" =~ ^[1-9][0-9]*$ ]] \
            && [[ "$updated_at_epoch" =~ ^[1-9][0-9]*$ ]] \
            && [ "$updated_at_epoch" -ge "$acquired_at_epoch" ] \
            && [ "$script_contract_version" = "$DECISION_RUNTIME_LIFECYCLE_CONTRACT_VERSION" ] \
            && [[ "$script_sha256" =~ ^[0-9a-f]{64}$ ]] \
            || return 0
    fi

    DECISION_RUNTIME_LIFECYCLE_LOCK_STATUS=valid
    DECISION_RUNTIME_LIFECYCLE_LOCK_VERSION=$version
    DECISION_RUNTIME_LIFECYCLE_LOCK_OPERATION=$operation
    DECISION_RUNTIME_LIFECYCLE_LOCK_PHASE=$phase
    DECISION_RUNTIME_LIFECYCLE_LOCK_PID=$owner_pid
    DECISION_RUNTIME_LIFECYCLE_LOCK_START_TOKEN=$owner_start_token
    DECISION_RUNTIME_LIFECYCLE_LOCK_NONCE=$owner_nonce
    DECISION_RUNTIME_LIFECYCLE_LOCK_ACQUIRED_AT=$acquired_at_epoch
    DECISION_RUNTIME_LIFECYCLE_LOCK_UPDATED_AT=$updated_at_epoch
    DECISION_RUNTIME_LIFECYCLE_LOCK_SCRIPT_CONTRACT_VERSION=$script_contract_version
    DECISION_RUNTIME_LIFECYCLE_LOCK_SCRIPT_SHA256=$script_sha256
}

decision_runtime_lifecycle_lock_generation() {
    [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_STATUS" = valid ] || return 1
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_VERSION" \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_OPERATION" \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_PHASE" \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_PID" \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_START_TOKEN" \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_NONCE" \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_ACQUIRED_AT" \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_UPDATED_AT" \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_SCRIPT_CONTRACT_VERSION" \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_SCRIPT_SHA256"
}

write_decision_runtime_lifecycle_lock_record() {
    local record=$1 operation=$2 phase=$3 owner_pid=$4
    local owner_start_token=$5 owner_nonce=$6 acquired_at_epoch=$7
    local updated_at_epoch=$8 script_contract_version=$9
    local script_sha256=${10} expected_record_sha256=${11:-} temp
    temp="$(mktemp "$RUNTIME_DIR/.lifecycle-lock-write.XXXXXX")"
    if ! {
        printf 'version\t%s\n' "$DECISION_RUNTIME_LIFECYCLE_RECORD_VERSION"
        printf 'operation\t%s\n' "$operation"
        printf 'phase\t%s\n' "$phase"
        printf 'owner_pid\t%s\n' "$owner_pid"
        printf 'owner_start_token\t%s\n' "$owner_start_token"
        printf 'owner_nonce\t%s\n' "$owner_nonce"
        printf 'acquired_at_epoch\t%s\n' "$acquired_at_epoch"
        printf 'updated_at_epoch\t%s\n' "$updated_at_epoch"
        printf 'script_contract_version\t%s\n' "$script_contract_version"
        printf 'script_sha256\t%s\n' "$script_sha256"
    } >"$temp"; then
        rm -f "$temp"
        return 1
    fi
    if [ -n "$expected_record_sha256" ]; then
        if ! replace_managed_record_generation \
            "$temp" "$record" "$expected_record_sha256"; then
            rm -f "$temp"
            return 1
        fi
    elif ! mv "$temp" "$record"; then
        rm -f "$temp"
        return 1
    fi
}

write_decision_runtime_lifecycle_lock() {
    local record
    record="$(decision_runtime_lifecycle_lock_record)"
    write_decision_runtime_lifecycle_lock_record "$record" "$@"
}

remove_decision_runtime_lifecycle_lock_generation() {
    local expected_generation=$1 record expected_record_sha256
    load_decision_runtime_lifecycle_lock
    [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_STATUS" = valid ] \
        && [ "$(decision_runtime_lifecycle_lock_generation)" = "$expected_generation" ] \
        || return 1
    record="$(decision_runtime_lifecycle_lock_record)"
    expected_record_sha256="$(managed_record_sha256 "$record")" \
        || return 1
    load_decision_runtime_lifecycle_lock
    [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_STATUS" = valid ] \
        && [ "$(decision_runtime_lifecycle_lock_generation)" = "$expected_generation" ] \
        || return 1
    retire_managed_record_directory \
        "$HERMES_DECISION_LIFECYCLE_LOCK" \
        ".lifecycle-lock-retired.XXXXXX" \
        "$expected_record_sha256"
}

operation_requires_durable_lifecycle_journal() {
    [[ "$1" =~ ^(update|install|uninstall)$ ]]
}

loaded_lifecycle_lock_is_owned_by_current_process() {
    [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_STATUS" = valid ] \
        && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_VERSION" = "$DECISION_RUNTIME_LIFECYCLE_RECORD_VERSION" ] \
        && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_PID" = "$DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_PID" ] \
        && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_START_TOKEN" = "$DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_START_TOKEN" ] \
        && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_NONCE" = "$DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_NONCE" ] \
        && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_OPERATION" = "$DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_OPERATION" ]
}

rewrite_loaded_decision_runtime_lifecycle_phase() {
    local expected_generation=$1 phase=$2 now record
    local expected_record_sha256
    load_decision_runtime_lifecycle_lock
    [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_STATUS" = valid ] \
        && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_VERSION" = "$DECISION_RUNTIME_LIFECYCLE_RECORD_VERSION" ] \
        && [ "$(decision_runtime_lifecycle_lock_generation)" = "$expected_generation" ] \
        || return 1
    record="$(decision_runtime_lifecycle_lock_record)"
    expected_record_sha256="$(managed_record_sha256 "$record")" \
        || return 1
    load_decision_runtime_lifecycle_lock
    [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_STATUS" = valid ] \
        && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_VERSION" = "$DECISION_RUNTIME_LIFECYCLE_RECORD_VERSION" ] \
        && [ "$(decision_runtime_lifecycle_lock_generation)" = "$expected_generation" ] \
        || return 1
    now="$(current_epoch)" || return 1
    write_decision_runtime_lifecycle_lock \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_OPERATION" \
        "$phase" \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_PID" \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_START_TOKEN" \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_NONCE" \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_ACQUIRED_AT" \
        "$now" \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_SCRIPT_CONTRACT_VERSION" \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_SCRIPT_SHA256" \
        "$expected_record_sha256"
}

rewrite_loaded_decision_runtime_lifecycle_script_generation() {
    local expected_generation=$1 phase=$2 script_sha256=$3 now record
    local expected_record_sha256
    load_decision_runtime_lifecycle_lock
    [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_STATUS" = valid ] \
        && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_VERSION" = "$DECISION_RUNTIME_LIFECYCLE_RECORD_VERSION" ] \
        && [ "$(decision_runtime_lifecycle_lock_generation)" = "$expected_generation" ] \
        || return 1
    record="$(decision_runtime_lifecycle_lock_record)"
    expected_record_sha256="$(managed_record_sha256 "$record")" \
        || return 1
    load_decision_runtime_lifecycle_lock
    [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_STATUS" = valid ] \
        && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_VERSION" = "$DECISION_RUNTIME_LIFECYCLE_RECORD_VERSION" ] \
        && [ "$(decision_runtime_lifecycle_lock_generation)" = "$expected_generation" ] \
        || return 1
    now="$(current_epoch)" || return 1
    write_decision_runtime_lifecycle_lock \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_OPERATION" \
        "$phase" \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_PID" \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_START_TOKEN" \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_NONCE" \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_ACQUIRED_AT" \
        "$now" \
        "$DECISION_RUNTIME_LIFECYCLE_CONTRACT_VERSION" \
        "$script_sha256" \
        "$expected_record_sha256"
}

set_decision_runtime_lifecycle_phase() {
    local phase=$1 expected_generation
    [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_HELD" = true ] || return 1
    load_decision_runtime_lifecycle_lock
    loaded_lifecycle_lock_is_owned_by_current_process || return 1
    expected_generation="$(decision_runtime_lifecycle_lock_generation)"
    rewrite_loaded_decision_runtime_lifecycle_phase \
        "$expected_generation" "$phase" || return 1
    DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_PHASE=$phase
}

mark_loaded_decision_runtime_lifecycle_repair_required() {
    local expected_generation=$1
    load_decision_runtime_lifecycle_lock
    [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_STATUS" = valid ] \
        && [ "$(decision_runtime_lifecycle_lock_generation)" = "$expected_generation" ] \
        && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_VERSION" = "$DECISION_RUNTIME_LIFECYCLE_RECORD_VERSION" ] \
        || return 1
    [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_PHASE" = repair_required ] \
        && return 0
    rewrite_loaded_decision_runtime_lifecycle_phase \
        "$expected_generation" repair_required
}

mark_owned_decision_runtime_lifecycle_repair_required() {
    local expected_generation
    load_decision_runtime_lifecycle_lock
    loaded_lifecycle_lock_is_owned_by_current_process || return 1
    expected_generation="$(decision_runtime_lifecycle_lock_generation)"
    mark_loaded_decision_runtime_lifecycle_repair_required \
        "$expected_generation" || return 1
    DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_PHASE=repair_required
}

recover_orphaned_decision_runtime_lifecycle_lock() {
    local expected_generation=$1 deadline=${2:-} status
    load_decision_runtime_lifecycle_lock
    [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_STATUS" = valid ] \
        && [ "$(decision_runtime_lifecycle_lock_generation)" = "$expected_generation" ] \
        || return 1
    if process_start_token_status \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_PID" \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_START_TOKEN" \
        "$deadline"; then
        return 1
    else
        status=$?
    fi
    [ "$status" -eq 3 ] || return 1
    epoch_age_at_least \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_UPDATED_AT" \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_STALE_GRACE_SECONDS" \
        || return 1
    if operation_requires_durable_lifecycle_journal \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_OPERATION"; then
        if [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_VERSION" = "$DECISION_RUNTIME_LIFECYCLE_RECORD_VERSION" ] \
            && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_PHASE" = complete ]; then
            remove_decision_runtime_lifecycle_lock_generation \
                "$expected_generation"
            return
        fi
        if [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_VERSION" = "$DECISION_RUNTIME_LIFECYCLE_RECORD_VERSION" ]; then
            mark_loaded_decision_runtime_lifecycle_repair_required \
                "$expected_generation" || return 1
        fi
        return 2
    fi
    [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_PHASE" != repair_required ] \
        || return 2
    remove_decision_runtime_lifecycle_lock_generation "$expected_generation"
}

find_interrupted_decision_runtime_lifecycle_record() {
    local entry count=0
    local -a candidates=(
        "$HERMES_DECISION_LIFECYCLE_LOCK"/.record.*
        "$RUNTIME_DIR"/.lifecycle-lock-record.*
    )
    INTERRUPTED_DECISION_RUNTIME_RECORD=
    [ -d "$HERMES_DECISION_LIFECYCLE_LOCK" ] \
        && [ ! -L "$HERMES_DECISION_LIFECYCLE_LOCK" ] \
        && [ ! -e "$(decision_runtime_lifecycle_lock_record)" ] \
        && [ ! -L "$(decision_runtime_lifecycle_lock_record)" ] \
        || return 1
    record_directory_has_only_managed_entries \
        "$HERMES_DECISION_LIFECYCLE_LOCK" || return 2
    for entry in "${candidates[@]}"; do
        [ -e "$entry" ] || [ -L "$entry" ] || continue
        [ -f "$entry" ] && [ ! -L "$entry" ] || {
            return 2
        }
        count=$((count + 1))
        INTERRUPTED_DECISION_RUNTIME_RECORD=$entry
    done
    [ "$count" -eq 1 ] || return 2
}

recover_interrupted_decision_runtime_lifecycle_record() {
    local deadline=${1:-} candidate generation owner_pid
    local owner_start_token updated_at
    local status record candidate_sha256
    find_interrupted_decision_runtime_lifecycle_record || return $?
    candidate=$INTERRUPTED_DECISION_RUNTIME_RECORD
    load_decision_runtime_lifecycle_lock "$candidate"
    [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_STATUS" = valid ] || return 2
    generation="$(decision_runtime_lifecycle_lock_generation)" || return 2
    owner_pid=$DECISION_RUNTIME_LIFECYCLE_LOCK_PID
    owner_start_token=$DECISION_RUNTIME_LIFECYCLE_LOCK_START_TOKEN
    updated_at=$DECISION_RUNTIME_LIFECYCLE_LOCK_UPDATED_AT
    if process_start_token_status \
        "$owner_pid" "$owner_start_token" "$deadline"; then
        return 3
    else
        status=$?
    fi
    [ "$status" -eq 3 ] || return 5
    epoch_age_at_least \
        "$updated_at" \
        "$DECISION_RUNTIME_LIFECYCLE_LOCK_STALE_GRACE_SECONDS" \
        || return 4
    candidate_sha256="$(managed_record_sha256 "$candidate")" \
        || return 2
    load_decision_runtime_lifecycle_lock "$candidate"
    [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_STATUS" = valid ] \
        && [ "$(decision_runtime_lifecycle_lock_generation)" = "$generation" ] \
        || return 2
    record="$(decision_runtime_lifecycle_lock_record)"
    if publish_interrupted_managed_record \
        "$candidate" "$record" "$candidate_sha256"; then
        :
    else
        status=$?
        [ "$status" -eq 6 ] || return 2
        load_decision_runtime_lifecycle_lock
        [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_STATUS" = valid ] \
            && [ "$(decision_runtime_lifecycle_lock_generation)" = "$generation" ] \
            || return 2
        return 0
    fi
    load_decision_runtime_lifecycle_lock
    [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_STATUS" = valid ] \
        && [ "$(decision_runtime_lifecycle_lock_generation)" = "$generation" ]
}

acquire_decision_runtime_lifecycle_lock() {
    local operation=$1 owner_pid owner_start_token
    local owner_nonce acquired_at_epoch attempts=0 existing_generation status
    local current_script_sha256 recovery_status stage lifecycle_deadline
    [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_HELD" = false ] \
        || die "decision runtime lifecycle lock is already held"
    [[ "$operation" =~ ^(start|stop|update|install|uninstall)$ ]] \
        || die "invalid decision runtime lifecycle operation"
    mkdir -p "$RUNTIME_DIR"
    initialize_lifecycle_script_generation
    owner_pid=$$
    capture_native_process_start_token "$owner_pid" \
        || die "decision runtime lifecycle owner identity is unreadable"
    owner_start_token=$NATIVE_PROCESS_START_TOKEN
    owner_nonce="$(new_service_nonce)" \
        || die "failed to generate decision runtime lifecycle nonce"
    lifecycle_deadline=$((
        SECONDS + DECISION_RUNTIME_LIFECYCLE_LOCK_WAIT_SECONDS
    ))
    while [ "$attempts" -le "$DECISION_RUNTIME_LIFECYCLE_LOCK_WAIT_SECONDS" ]; do
        if [ "$attempts" -gt 0 ] \
            && [ "$SECONDS" -ge "$lifecycle_deadline" ]; then
            die "decision runtime lifecycle lock could not be recovered within the bounded wait; preserving it"
        fi
        assert_lifecycle_script_generation_unchanged
        load_decision_runtime_lifecycle_lock
        if [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_STATUS" = missing ]; then
            acquired_at_epoch="$(current_epoch)" \
                || die "failed to read decision runtime lifecycle clock"
            current_script_sha256="$(current_lifecycle_script_sha256)" \
                || die "decision runtime lifecycle script identity became unreadable before lock publication"
            if [ "$current_script_sha256" != "$DECISION_RUNTIME_LIFECYCLE_INITIAL_SCRIPT_SHA256" ]; then
                die "healthmes_local.sh changed before lifecycle lock publication; rerun the command with the current script"
            fi
            stage="$(mktemp -d "$RUNTIME_DIR/.lifecycle-lock-stage.XXXXXX")" \
                || die "failed to allocate decision runtime lifecycle staging directory"
            if ! write_decision_runtime_lifecycle_lock_record \
                "$stage/record" \
                "$operation" \
                acquired \
                "$owner_pid" \
                "$owner_start_token" \
                "$owner_nonce" \
                "$acquired_at_epoch" \
                "$acquired_at_epoch" \
                "$DECISION_RUNTIME_LIFECYCLE_CONTRACT_VERSION" \
                "$DECISION_RUNTIME_LIFECYCLE_INITIAL_SCRIPT_SHA256"; then
                remove_managed_record_directory "$stage" 2>/dev/null || true
                die "decision runtime lifecycle owner record could not be staged"
            fi
            if rename_directory_exclusive \
                "$stage" "$HERMES_DECISION_LIFECYCLE_LOCK"; then
                :
            else
                status=$?
                remove_managed_record_directory "$stage" 2>/dev/null || true
                if [ "$status" -eq 6 ]; then
                    attempts=$((attempts + 1))
                    continue
                fi
                die "decision runtime lifecycle lock publication is unavailable; refusing non-atomic acquisition"
            fi
            DECISION_RUNTIME_LIFECYCLE_LOCK_HELD=true
            DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_PID=$owner_pid
            DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_START_TOKEN=$owner_start_token
            DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_NONCE=$owner_nonce
            DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_OPERATION=$operation
            DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_PHASE=acquired
            return
        fi

        case "$DECISION_RUNTIME_LIFECYCLE_LOCK_STATUS" in
        invalid)
            if recover_interrupted_decision_runtime_lifecycle_record \
                "$lifecycle_deadline"; then
                info "restored an interrupted decision runtime lifecycle owner record"
                attempts=$((attempts + 1))
                continue
            else
                status=$?
            fi
            case "$status" in
            3)
                if [ "$attempts" -ge "$DECISION_RUNTIME_LIFECYCLE_LOCK_WAIT_SECONDS" ]; then
                    die "decision runtime lifecycle publication is still owned by a live process; preserving its orphan record"
                fi
                ;;
            4)
                ;;
            5)
                die "decision runtime lifecycle orphan owner identity is unknown; preserving it"
                ;;
            *)
                die "decision runtime lifecycle lock is malformed or has no provable owner; preserving it"
                ;;
            esac
            ;;
        valid)
            existing_generation="$(decision_runtime_lifecycle_lock_generation)"
            if [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_PHASE" = repair_required ]; then
                die "decision runtime lifecycle ${DECISION_RUNTIME_LIFECYCLE_LOCK_OPERATION} transaction requires explicit repair; preserving $HERMES_DECISION_LIFECYCLE_LOCK"
            fi
            if process_start_token_status \
                "$DECISION_RUNTIME_LIFECYCLE_LOCK_PID" \
                "$DECISION_RUNTIME_LIFECYCLE_LOCK_START_TOKEN" \
                "$lifecycle_deadline"; then
                if [ "$attempts" -ge "$DECISION_RUNTIME_LIFECYCLE_LOCK_WAIT_SECONDS" ]; then
                    die "decision runtime lifecycle lock is still owned by live ${DECISION_RUNTIME_LIFECYCLE_LOCK_OPERATION} pid ${DECISION_RUNTIME_LIFECYCLE_LOCK_PID}; timed out without mutating runtime state"
                fi
            else
                status=$?
                case "$status" in
                3)
                    if recover_orphaned_decision_runtime_lifecycle_lock \
                        "$existing_generation" \
                        "$lifecycle_deadline"; then
                        info "recovered stale decision runtime lifecycle lock without signalling its former PID"
                        attempts=$((attempts + 1))
                        continue
                    else
                        recovery_status=$?
                        if [ "$recovery_status" -eq 2 ]; then
                            die "orphaned decision runtime ${DECISION_RUNTIME_LIFECYCLE_LOCK_OPERATION} transaction requires explicit repair; preserving $HERMES_DECISION_LIFECYCLE_LOCK"
                        fi
                    fi
                    ;;
                *)
                    die "decision runtime lifecycle lock owner identity is unknown; preserving the lock"
                    ;;
                esac
            fi
            ;;
        esac
        if [ "$attempts" -ge "$DECISION_RUNTIME_LIFECYCLE_LOCK_WAIT_SECONDS" ]; then
            die "decision runtime lifecycle lock could not be recovered within the bounded wait; preserving it"
        fi
        "$SLEEP_BIN" 1
        attempts=$((attempts + 1))
    done
    die "decision runtime lifecycle lock acquisition timed out"
}

release_decision_runtime_lifecycle_lock() {
    local expected_generation
    [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_HELD" = true ] || return 0
    load_decision_runtime_lifecycle_lock
    loaded_lifecycle_lock_is_owned_by_current_process || return 1
    expected_generation="$(decision_runtime_lifecycle_lock_generation)"
    remove_decision_runtime_lifecycle_lock_generation "$expected_generation" \
        || return 1
    DECISION_RUNTIME_LIFECYCLE_LOCK_HELD=false
}

release_decision_runtime_lifecycle_lock_on_exit() {
    local status=$?
    trap - EXIT
    if [ "$status" -ne 0 ] \
        && operation_requires_durable_lifecycle_journal \
            "$DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_OPERATION" \
        && [ "$DECISION_RUNTIME_DURABLE_MUTATION_STARTED" = true ]; then
        if ! mark_owned_decision_runtime_lifecycle_repair_required; then
            printf '%s\n' \
                "[healthmes] failed to mark the interrupted decision runtime transaction repair-required; preserving its existing lifecycle journal" \
                >&2
        else
            printf '%s\n' \
                "[healthmes] decision runtime ${DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_OPERATION} failed after mutation began; preserving a repair-required lifecycle journal" \
                >&2
        fi
        exit "$status"
    fi
    if ! release_decision_runtime_lifecycle_lock; then
        printf '%s\n' \
            "[healthmes] decision runtime lifecycle lock release failed; preserving its diagnostic" \
            >&2
        [ "$status" -ne 0 ] || status=1
    fi
    exit "$status"
}

finish_decision_runtime_post_release_cleanup() {
    if [ "$DECISION_RUNTIME_POST_RELEASE_RMDIR_RUNTIME" = true ]; then
        rmdir "$RUNTIME_DIR" 2>/dev/null || true
    fi
    if [ "$DECISION_RUNTIME_POST_RELEASE_RMDIR_DATA" = true ]; then
        rmdir "$DATA_DIR" 2>/dev/null || true
    fi
    DECISION_RUNTIME_POST_RELEASE_RMDIR_RUNTIME=false
    DECISION_RUNTIME_POST_RELEASE_RMDIR_DATA=false
}

run_owned_decision_runtime_lifecycle_command() {
    "$@"
    set_decision_runtime_lifecycle_phase complete \
        || die "decision runtime lifecycle completion could not be journaled; preserving the lock"
    release_decision_runtime_lifecycle_lock \
        || die "decision runtime lifecycle lock ownership changed before release; preserving the lock"
    trap - EXIT
    finish_decision_runtime_post_release_cleanup
}

with_decision_runtime_lifecycle_lock() {
    local operation=$1
    shift
    if [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_HELD" = true ]; then
        "$@"
        return
    fi
    DECISION_RUNTIME_DURABLE_MUTATION_STARTED=false
    DECISION_RUNTIME_POST_RELEASE_RMDIR_RUNTIME=false
    DECISION_RUNTIME_POST_RELEASE_RMDIR_DATA=false
    acquire_decision_runtime_lifecycle_lock "$operation"
    trap release_decision_runtime_lifecycle_lock_on_exit EXIT
    # Keep errexit active inside lifecycle commands. Running a shell function
    # as an `if` condition disables errexit throughout that function and can
    # hide a failed pull, setup, service stop, or cleanup behind later success.
    # The EXIT trap releases non-mutating failures and journals interrupted
    # durable mutations as repair_required.
    run_owned_decision_runtime_lifecycle_command "$@"
}

decision_runtime_startup_lease_record() {
    printf '%s/record\n' "$HERMES_DECISION_STARTUP_LEASE"
}

decision_runtime_startup_lease_exists() {
    [ -e "$HERMES_DECISION_STARTUP_LEASE" ] \
        || [ -L "$HERMES_DECISION_STARTUP_LEASE" ]
}

load_decision_runtime_startup_lease() {
    local record_override=${1:-} record key value extra
    local version= state= phase= launcher_service_nonce= launcher_pid=
    local launcher_start_token= created_at_epoch= updated_at_epoch=
    local startup_owner_pid= startup_owner_start_token= startup_owner_nonce=
    local seen_version= seen_state= seen_phase=
    local seen_launcher_service_nonce= seen_launcher_pid=
    local seen_launcher_start_token= seen_created_at_epoch=
    local seen_updated_at_epoch= seen_startup_owner_pid=
    local seen_startup_owner_start_token= seen_startup_owner_nonce=

    DECISION_RUNTIME_STARTUP_LEASE_STATUS=missing
    DECISION_RUNTIME_STARTUP_LEASE_VERSION=
    DECISION_RUNTIME_STARTUP_LEASE_STATE=
    DECISION_RUNTIME_STARTUP_LEASE_PHASE=
    DECISION_RUNTIME_STARTUP_LEASE_PID=
    DECISION_RUNTIME_STARTUP_LEASE_START_TOKEN=
    DECISION_RUNTIME_STARTUP_LEASE_SERVICE_NONCE=
    DECISION_RUNTIME_STARTUP_LEASE_CREATED_AT=
    DECISION_RUNTIME_STARTUP_LEASE_UPDATED_AT=
    DECISION_RUNTIME_STARTUP_LEASE_OWNER_PID=
    DECISION_RUNTIME_STARTUP_LEASE_OWNER_START_TOKEN=
    DECISION_RUNTIME_STARTUP_LEASE_OWNER_NONCE=
    DECISION_RUNTIME_STARTUP_LEASE_STATUS=invalid
    if [ -n "$record_override" ]; then
        record=$record_override
    else
        decision_runtime_startup_lease_exists || {
            DECISION_RUNTIME_STARTUP_LEASE_STATUS=missing
            return 0
        }
        [ -d "$HERMES_DECISION_STARTUP_LEASE" ] \
            && [ ! -L "$HERMES_DECISION_STARTUP_LEASE" ] \
            || return 0
        record="$(decision_runtime_startup_lease_record)"
    fi
    [ -f "$record" ] && [ ! -L "$record" ] || return 0

    while IFS=$'\t' read -r key value extra; do
        [ -z "$extra" ] || return 0
        case "$key" in
        version)
            [ -z "$seen_version" ] || return 0
            version=$value
            seen_version=1
            ;;
        state)
            [ -z "$seen_state" ] || return 0
            state=$value
            seen_state=1
            ;;
        phase)
            [ -z "$seen_phase" ] || return 0
            phase=$value
            seen_phase=1
            ;;
        launcher_service_nonce)
            [ -z "$seen_launcher_service_nonce" ] || return 0
            launcher_service_nonce=$value
            seen_launcher_service_nonce=1
            ;;
        launcher_pid)
            [ -z "$seen_launcher_pid" ] || return 0
            launcher_pid=$value
            seen_launcher_pid=1
            ;;
        launcher_start_token)
            [ -z "$seen_launcher_start_token" ] || return 0
            launcher_start_token=$value
            seen_launcher_start_token=1
            ;;
        created_at_epoch)
            [ -z "$seen_created_at_epoch" ] || return 0
            created_at_epoch=$value
            seen_created_at_epoch=1
            ;;
        updated_at_epoch)
            [ -z "$seen_updated_at_epoch" ] || return 0
            updated_at_epoch=$value
            seen_updated_at_epoch=1
            ;;
        startup_owner_pid)
            [ -z "$seen_startup_owner_pid" ] || return 0
            startup_owner_pid=$value
            seen_startup_owner_pid=1
            ;;
        startup_owner_start_token)
            [ -z "$seen_startup_owner_start_token" ] || return 0
            startup_owner_start_token=$value
            seen_startup_owner_start_token=1
            ;;
        startup_owner_nonce)
            [ -z "$seen_startup_owner_nonce" ] || return 0
            startup_owner_nonce=$value
            seen_startup_owner_nonce=1
            ;;
        *)
            return 0
            ;;
        esac
    done <"$record"

    if [ "$version" = 1 ]; then
        [ -n "$seen_version" ] \
            && [ -n "$seen_state" ] \
            && [ -n "$seen_launcher_service_nonce" ] \
            && [ -z "$seen_phase$seen_launcher_start_token" ] \
            && [ -z "$seen_created_at_epoch$seen_updated_at_epoch" ] \
            && [ -z "$seen_startup_owner_pid" ] \
            && [ -z "$seen_startup_owner_start_token" ] \
            && [ -z "$seen_startup_owner_nonce" ] \
            && [[ "$launcher_service_nonce" =~ ^[A-Za-z0-9-]+$ ]] \
            || return 0
        case "$state" in
        pending)
            [ -z "$seen_launcher_pid" ] || return 0
            ;;
        spawned)
            [ -n "$seen_launcher_pid" ] \
                && valid_managed_pid "$launcher_pid" \
                || return 0
            ;;
        *)
            return 0
            ;;
        esac
        DECISION_RUNTIME_STARTUP_LEASE_STATUS=$state
        DECISION_RUNTIME_STARTUP_LEASE_VERSION=1
        DECISION_RUNTIME_STARTUP_LEASE_STATE=$state
        DECISION_RUNTIME_STARTUP_LEASE_PHASE=$state
        DECISION_RUNTIME_STARTUP_LEASE_PID=$launcher_pid
        DECISION_RUNTIME_STARTUP_LEASE_SERVICE_NONCE=$launcher_service_nonce
        return
    fi

    [ "$version" = 2 ] \
        && [ -n "$seen_version" ] \
        && [ -z "$seen_state" ] \
        && [ -n "$seen_phase" ] \
        && [ -n "$seen_launcher_service_nonce" ] \
        && [ -n "$seen_created_at_epoch" ] \
        && [ -n "$seen_updated_at_epoch" ] \
        && [ -n "$seen_startup_owner_pid" ] \
        && [ -n "$seen_startup_owner_start_token" ] \
        && [ -n "$seen_startup_owner_nonce" ] \
        && [[ "$launcher_service_nonce" =~ ^[A-Za-z0-9-]+$ ]] \
        && [[ "$created_at_epoch" =~ ^[1-9][0-9]*$ ]] \
        && [[ "$updated_at_epoch" =~ ^[1-9][0-9]*$ ]] \
        && [ "$updated_at_epoch" -ge "$created_at_epoch" ] \
        && valid_managed_pid "$startup_owner_pid" \
        && [[ "$startup_owner_start_token" =~ ^(linux:[1-9][0-9]*|darwin:[1-9][0-9]*:[0-9]{6}|ps:.+)$ ]] \
        && [[ "$startup_owner_nonce" =~ ^[A-Za-z0-9-]+$ ]] \
        || return 0
    case "$phase" in
    intent)
        [ -z "$seen_launcher_pid" ] \
            && [ -z "$seen_launcher_start_token" ] \
            || return 0
        ;;
    failed)
        { [ -z "$seen_launcher_pid" ] \
            || { valid_managed_pid "$launcher_pid" \
                && [ -n "$seen_launcher_pid" ]; }; } \
            && [ -z "$seen_launcher_start_token" ] \
            || return 0
        ;;
    spawned)
        [ -n "$seen_launcher_pid" ] \
            && valid_managed_pid "$launcher_pid" \
            && [ -z "$seen_launcher_start_token" ] \
            || return 0
        ;;
    identity_verified)
        [ -n "$seen_launcher_pid" ] \
            && valid_managed_pid "$launcher_pid" \
            && [ -n "$seen_launcher_start_token" ] \
            && [[ "$launcher_start_token" == ps:* ]] \
            || return 0
        ;;
    *)
        return 0
        ;;
    esac

    DECISION_RUNTIME_STARTUP_LEASE_STATUS=$phase
    DECISION_RUNTIME_STARTUP_LEASE_VERSION=2
    DECISION_RUNTIME_STARTUP_LEASE_STATE=$phase
    DECISION_RUNTIME_STARTUP_LEASE_PHASE=$phase
    DECISION_RUNTIME_STARTUP_LEASE_PID=$launcher_pid
    DECISION_RUNTIME_STARTUP_LEASE_START_TOKEN=$launcher_start_token
    DECISION_RUNTIME_STARTUP_LEASE_SERVICE_NONCE=$launcher_service_nonce
    DECISION_RUNTIME_STARTUP_LEASE_CREATED_AT=$created_at_epoch
    DECISION_RUNTIME_STARTUP_LEASE_UPDATED_AT=$updated_at_epoch
    DECISION_RUNTIME_STARTUP_LEASE_OWNER_PID=$startup_owner_pid
    DECISION_RUNTIME_STARTUP_LEASE_OWNER_START_TOKEN=$startup_owner_start_token
    DECISION_RUNTIME_STARTUP_LEASE_OWNER_NONCE=$startup_owner_nonce
}

write_decision_runtime_startup_lease_record() {
    local record=$1 expected_record_sha256=${2:-} temp
    [ "$DECISION_RUNTIME_STARTUP_LEASE_VERSION" = 2 ] || return 1
    temp="$(mktemp "$RUNTIME_DIR/.startup-lease-write.XXXXXX")"
    if ! {
        printf 'version\t2\n'
        printf 'phase\t%s\n' "$DECISION_RUNTIME_STARTUP_LEASE_PHASE"
        printf 'created_at_epoch\t%s\n' \
            "$DECISION_RUNTIME_STARTUP_LEASE_CREATED_AT"
        printf 'updated_at_epoch\t%s\n' \
            "$DECISION_RUNTIME_STARTUP_LEASE_UPDATED_AT"
        printf 'launcher_service_nonce\t%s\n' \
            "$DECISION_RUNTIME_STARTUP_LEASE_SERVICE_NONCE"
        printf 'startup_owner_pid\t%s\n' \
            "$DECISION_RUNTIME_STARTUP_LEASE_OWNER_PID"
        printf 'startup_owner_start_token\t%s\n' \
            "$DECISION_RUNTIME_STARTUP_LEASE_OWNER_START_TOKEN"
        printf 'startup_owner_nonce\t%s\n' \
            "$DECISION_RUNTIME_STARTUP_LEASE_OWNER_NONCE"
        if [ -n "$DECISION_RUNTIME_STARTUP_LEASE_PID" ]; then
            printf 'launcher_pid\t%s\n' \
                "$DECISION_RUNTIME_STARTUP_LEASE_PID"
        fi
        if [ -n "$DECISION_RUNTIME_STARTUP_LEASE_START_TOKEN" ]; then
            printf 'launcher_start_token\t%s\n' \
                "$DECISION_RUNTIME_STARTUP_LEASE_START_TOKEN"
        fi
    } >"$temp"; then
        rm -f "$temp"
        return 1
    fi
    if [ -n "$expected_record_sha256" ]; then
        if ! replace_managed_record_generation \
            "$temp" "$record" "$expected_record_sha256"; then
            rm -f "$temp"
            return 1
        fi
    elif ! mv "$temp" "$record"; then
        rm -f "$temp"
        return 1
    fi
}

write_decision_runtime_startup_lease() {
    local expected_record_sha256=${1:-} record
    record="$(decision_runtime_startup_lease_record)"
    write_decision_runtime_startup_lease_record \
        "$record" "$expected_record_sha256"
}

create_decision_runtime_startup_lease() {
    local launcher_service_nonce=$1 now stage status
    [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_HELD" = true ] \
        && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_OPERATION" = start ] \
        || die "decision runtime startup requires the cross-process lifecycle lock"
    load_decision_runtime_startup_lease
    if [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" != missing ]; then
        die "decision runtime startup lease already exists; stop or inspect the existing generation first"
    fi
    now="$(current_epoch)" || die "failed to read startup lease clock"
    DECISION_RUNTIME_STARTUP_LEASE_VERSION=2
    DECISION_RUNTIME_STARTUP_LEASE_STATUS=intent
    DECISION_RUNTIME_STARTUP_LEASE_STATE=intent
    DECISION_RUNTIME_STARTUP_LEASE_PHASE=intent
    DECISION_RUNTIME_STARTUP_LEASE_PID=
    DECISION_RUNTIME_STARTUP_LEASE_START_TOKEN=
    DECISION_RUNTIME_STARTUP_LEASE_SERVICE_NONCE=$launcher_service_nonce
    DECISION_RUNTIME_STARTUP_LEASE_CREATED_AT=$now
    DECISION_RUNTIME_STARTUP_LEASE_UPDATED_AT=$now
    DECISION_RUNTIME_STARTUP_LEASE_OWNER_PID=$DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_PID
    DECISION_RUNTIME_STARTUP_LEASE_OWNER_START_TOKEN=$DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_START_TOKEN
    DECISION_RUNTIME_STARTUP_LEASE_OWNER_NONCE=$DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_NONCE
    stage="$(mktemp -d "$RUNTIME_DIR/.startup-lease-stage.XXXXXX")" \
        || die "failed to allocate decision runtime startup staging directory"
    if ! write_decision_runtime_startup_lease_record "$stage/record"; then
        remove_managed_record_directory "$stage" 2>/dev/null || true
        die "failed to stage decision runtime startup intent"
    fi
    if rename_directory_exclusive \
        "$stage" "$HERMES_DECISION_STARTUP_LEASE"; then
        return
    else
        status=$?
    fi
    remove_managed_record_directory "$stage" 2>/dev/null || true
    [ "$status" -eq 6 ] \
        && die "decision runtime startup lease already exists; preserving the competing generation"
    die "decision runtime startup lease publication is unavailable; refusing non-atomic startup"
}

mark_decision_runtime_startup_spawned() {
    local launcher_service_nonce=$1 launcher_pid=$2 now
    local expected_generation expected_record_sha256 record
    load_decision_runtime_startup_lease
    [ "$DECISION_RUNTIME_STARTUP_LEASE_VERSION" = 2 ] \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" = intent ] \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_SERVICE_NONCE" = "$launcher_service_nonce" ] \
        || return 1
    valid_managed_pid "$launcher_pid" || return 1
    expected_generation="$(decision_runtime_startup_lease_generation)" \
        || return 1
    record="$(decision_runtime_startup_lease_record)"
    expected_record_sha256="$(managed_record_sha256 "$record")" \
        || return 1
    load_decision_runtime_startup_lease
    [ "$DECISION_RUNTIME_STARTUP_LEASE_VERSION" = 2 ] \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" = intent ] \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_SERVICE_NONCE" = "$launcher_service_nonce" ] \
        && [ "$(decision_runtime_startup_lease_generation)" = "$expected_generation" ] \
        || return 1
    now="$(current_epoch)" || return 1
    DECISION_RUNTIME_STARTUP_LEASE_STATUS=spawned
    DECISION_RUNTIME_STARTUP_LEASE_STATE=spawned
    DECISION_RUNTIME_STARTUP_LEASE_PHASE=spawned
    DECISION_RUNTIME_STARTUP_LEASE_PID=$launcher_pid
    DECISION_RUNTIME_STARTUP_LEASE_UPDATED_AT=$now
    write_decision_runtime_startup_lease "$expected_record_sha256"
}

mark_decision_runtime_startup_identity_verified() {
    local launcher_service_nonce=$1 launcher_pid=$2
    local launcher_start_token=$3 now expected_generation
    local expected_record_sha256 record
    load_decision_runtime_startup_lease
    [ "$DECISION_RUNTIME_STARTUP_LEASE_VERSION" = 2 ] \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" = spawned ] \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_SERVICE_NONCE" = "$launcher_service_nonce" ] \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_PID" = "$launcher_pid" ] \
        && [[ "$launcher_start_token" == ps:* ]] \
        || return 1
    expected_generation="$(decision_runtime_startup_lease_generation)" \
        || return 1
    record="$(decision_runtime_startup_lease_record)"
    expected_record_sha256="$(managed_record_sha256 "$record")" \
        || return 1
    load_decision_runtime_startup_lease
    [ "$DECISION_RUNTIME_STARTUP_LEASE_VERSION" = 2 ] \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" = spawned ] \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_SERVICE_NONCE" = "$launcher_service_nonce" ] \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_PID" = "$launcher_pid" ] \
        && [ "$(decision_runtime_startup_lease_generation)" = "$expected_generation" ] \
        || return 1
    now="$(current_epoch)" || return 1
    DECISION_RUNTIME_STARTUP_LEASE_STATUS=identity_verified
    DECISION_RUNTIME_STARTUP_LEASE_STATE=identity_verified
    DECISION_RUNTIME_STARTUP_LEASE_PHASE=identity_verified
    DECISION_RUNTIME_STARTUP_LEASE_START_TOKEN=$launcher_start_token
    DECISION_RUNTIME_STARTUP_LEASE_UPDATED_AT=$now
    write_decision_runtime_startup_lease "$expected_record_sha256"
}

mark_decision_runtime_startup_failed() {
    local launcher_service_nonce=$1 launcher_pid=${2:-} now
    local expected_generation expected_record_sha256 record
    load_decision_runtime_startup_lease
    [ "$DECISION_RUNTIME_STARTUP_LEASE_VERSION" = 2 ] \
        && { [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" = intent ] \
            || [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" = spawned ]; } \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_SERVICE_NONCE" = "$launcher_service_nonce" ] \
        || return 1
    if [ -n "$launcher_pid" ]; then
        valid_managed_pid "$launcher_pid" || return 1
        if [ -n "$DECISION_RUNTIME_STARTUP_LEASE_PID" ] \
            && [ "$DECISION_RUNTIME_STARTUP_LEASE_PID" != "$launcher_pid" ]; then
            return 1
        fi
    fi
    expected_generation="$(decision_runtime_startup_lease_generation)" \
        || return 1
    record="$(decision_runtime_startup_lease_record)"
    expected_record_sha256="$(managed_record_sha256 "$record")" \
        || return 1
    load_decision_runtime_startup_lease
    [ "$DECISION_RUNTIME_STARTUP_LEASE_VERSION" = 2 ] \
        && { [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" = intent ] \
            || [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" = spawned ]; } \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_SERVICE_NONCE" = "$launcher_service_nonce" ] \
        && [ "$(decision_runtime_startup_lease_generation)" = "$expected_generation" ] \
        || return 1
    if [ -n "$launcher_pid" ] \
        && [ -n "$DECISION_RUNTIME_STARTUP_LEASE_PID" ] \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_PID" != "$launcher_pid" ]; then
        return 1
    fi
    now="$(current_epoch)" || return 1
    DECISION_RUNTIME_STARTUP_LEASE_STATUS=failed
    DECISION_RUNTIME_STARTUP_LEASE_STATE=failed
    DECISION_RUNTIME_STARTUP_LEASE_PHASE=failed
    DECISION_RUNTIME_STARTUP_LEASE_PID=$launcher_pid
    DECISION_RUNTIME_STARTUP_LEASE_START_TOKEN=
    DECISION_RUNTIME_STARTUP_LEASE_UPDATED_AT=$now
    write_decision_runtime_startup_lease "$expected_record_sha256"
}

complete_decision_runtime_startup_lease() {
    local launcher_service_nonce=$1 launcher_pid=$2 expected_generation
    load_decision_runtime_startup_lease
    [ "$DECISION_RUNTIME_STARTUP_LEASE_VERSION" = 2 ] \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" = identity_verified ] \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_SERVICE_NONCE" = "$launcher_service_nonce" ] \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_PID" = "$launcher_pid" ] \
        || return 1
    load_process_identity "$HERMES_DECISION_PID" \
        && [ "$PROCESS_PID" = "$launcher_pid" ] \
        && [ "$PROCESS_NONCE" = "$launcher_service_nonce" ] \
        && [ "ps:$PROCESS_START_TIME" = "$DECISION_RUNTIME_STARTUP_LEASE_START_TOKEN" ] \
        || return 1
    expected_generation="$(decision_runtime_startup_lease_generation)"
    remove_decision_runtime_startup_lease_generation "$expected_generation"
}

cancel_pending_decision_runtime_startup_lease() {
    local launcher_service_nonce=$1 expected_generation
    load_decision_runtime_startup_lease
    [ "$DECISION_RUNTIME_STARTUP_LEASE_VERSION" = 2 ] \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" = intent ] \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_SERVICE_NONCE" = "$launcher_service_nonce" ] \
        || return 1
    expected_generation="$(decision_runtime_startup_lease_generation)"
    remove_decision_runtime_startup_lease_generation "$expected_generation"
}

decision_runtime_startup_lease_generation() {
    [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" != missing ] \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" != invalid ] \
        || return 1
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$DECISION_RUNTIME_STARTUP_LEASE_VERSION" \
        "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" \
        "$DECISION_RUNTIME_STARTUP_LEASE_PID" \
        "$DECISION_RUNTIME_STARTUP_LEASE_START_TOKEN" \
        "$DECISION_RUNTIME_STARTUP_LEASE_SERVICE_NONCE" \
        "$DECISION_RUNTIME_STARTUP_LEASE_CREATED_AT" \
        "$DECISION_RUNTIME_STARTUP_LEASE_UPDATED_AT" \
        "$DECISION_RUNTIME_STARTUP_LEASE_OWNER_PID" \
        "$DECISION_RUNTIME_STARTUP_LEASE_OWNER_START_TOKEN" \
        "$DECISION_RUNTIME_STARTUP_LEASE_OWNER_NONCE"
}

remove_decision_runtime_startup_lease_generation() {
    local expected_generation=$1 record expected_record_sha256
    load_decision_runtime_startup_lease
    [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" != missing ] \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" != invalid ] \
        && [ "$(decision_runtime_startup_lease_generation)" = "$expected_generation" ] \
        || return 1
    record="$(decision_runtime_startup_lease_record)"
    expected_record_sha256="$(managed_record_sha256 "$record")" \
        || return 1
    load_decision_runtime_startup_lease
    [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" != missing ] \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" != invalid ] \
        && [ "$(decision_runtime_startup_lease_generation)" = "$expected_generation" ] \
        || return 1
    retire_managed_record_directory \
        "$HERMES_DECISION_STARTUP_LEASE" \
        ".startup-lease-retired.XXXXXX" \
        "$expected_record_sha256"
}

decision_runtime_startup_lease_generation_matches() {
    local launcher_pid=$1 launcher_service_nonce=$2
    [ "$DECISION_RUNTIME_STARTUP_LEASE_SERVICE_NONCE" = "$launcher_service_nonce" ] \
        || return 1
    case "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" in
    intent)
        return 0
        ;;
    failed)
        [ -z "$DECISION_RUNTIME_STARTUP_LEASE_PID" ] \
            || [ "$DECISION_RUNTIME_STARTUP_LEASE_PID" = "$launcher_pid" ]
        ;;
    pending)
        [ -f "$HERMES_DECISION_PID" ] \
            && [ ! -L "$HERMES_DECISION_PID" ] \
            && [ "$(<"$HERMES_DECISION_PID")" = "$launcher_pid" ]
        ;;
    spawned | identity_verified)
        [ "$DECISION_RUNTIME_STARTUP_LEASE_PID" = "$launcher_pid" ]
        ;;
    *)
        return 1
        ;;
    esac
}

decision_runtime_startup_lease_matches_launcher() {
    local launcher_pid=$1 launcher_service_nonce=$2
    local lease_status=${3:-$DECISION_RUNTIME_STARTUP_LEASE_STATUS}
    local lease_pid=${4:-$DECISION_RUNTIME_STARTUP_LEASE_PID}
    local lease_service_nonce=${5:-$DECISION_RUNTIME_STARTUP_LEASE_SERVICE_NONCE}
    local stored_pid
    [ "$lease_service_nonce" = "$launcher_service_nonce" ] \
        || return 1
    case "$lease_status" in
    intent)
        return 0
        ;;
    failed)
        [ -z "$lease_pid" ] || [ "$lease_pid" = "$launcher_pid" ]
        ;;
    spawned | identity_verified)
        [ "$lease_pid" = "$launcher_pid" ]
        ;;
    pending)
        [ -f "$HERMES_DECISION_PID" ] \
            && [ ! -L "$HERMES_DECISION_PID" ] \
            || return 1
        stored_pid="$(<"$HERMES_DECISION_PID")"
        [ "$stored_pid" = "$launcher_pid" ]
        ;;
    *)
        return 1
        ;;
    esac
}

run_service_runner() {
    local nonce=$1 command=$2 child status service_pid service_start_token
    [ -n "$nonce" ] && [ "${HEALTHMES_SERVICE_NONCE:-}" = "$nonce" ] \
        || die "invalid service runner nonce"
    service_pid=$$
    if [ -n "${HEALTHMES_DECISION_STARTUP_LEASE_PATH:-}" ] \
        || [ -n "${HEALTHMES_DECISION_STARTUP_PID_FILE:-}" ]; then
        [ "${HEALTHMES_DECISION_STARTUP_LEASE_PATH:-}" = "$HERMES_DECISION_STARTUP_LEASE" ] \
            && [ "${HEALTHMES_DECISION_STARTUP_PID_FILE:-}" = "$HERMES_DECISION_PID" ] \
            || die "invalid decision runtime startup publication path"
        if ! mark_decision_runtime_startup_spawned "$nonce" "$service_pid"; then
            mark_decision_runtime_startup_failed \
                "$nonce" "$service_pid" 2>/dev/null || true
            die "failed to bind decision runtime startup lease to the managed Bash launcher"
        fi
        if ! write_unverified_process_pid "$HERMES_DECISION_PID" "$service_pid"; then
            mark_decision_runtime_startup_failed \
                "$nonce" "$service_pid" 2>/dev/null || true
            die "failed to publish decision runtime launcher PID tombstone"
        fi
    fi
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

wait_for_decision_runtime_launcher_publication() {
    local launcher_service_nonce=$1 launcher_pid=$2 stored_pid
    local attempts=0
    while [ "$attempts" -lt 50 ]; do
        load_decision_runtime_startup_lease
        if [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" = spawned ] \
            && [ "$DECISION_RUNTIME_STARTUP_LEASE_SERVICE_NONCE" = "$launcher_service_nonce" ] \
            && [ "$DECISION_RUNTIME_STARTUP_LEASE_PID" = "$launcher_pid" ] \
            && [ -f "$HERMES_DECISION_PID" ] \
            && [ ! -L "$HERMES_DECISION_PID" ]; then
            stored_pid="$(<"$HERMES_DECISION_PID")"
            [ "$stored_pid" = "$launcher_pid" ] && return 0
        fi
        case "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" in
        intent) ;;
        *) return 1 ;;
        esac
        "$SLEEP_BIN" 0.1
        attempts=$((attempts + 1))
    done
    return 1
}

start_process() {
    local name=$1 pid_file=$2 log_file=$3 command=$4
    local startup_lease=${5:-}
    local nonce pid identity_status
    if pid_running "$pid_file"; then
        info "$name already running (pid $PROCESS_PID)"
        return
    else
        identity_status=$?
        if [ "$identity_status" -eq 5 ]; then
            die "$name process identity is unknown; preserving metadata"
        fi
    fi
    mkdir -p "$RUNTIME_DIR"
    nonce="$(new_service_nonce)" || die "failed to generate $name service nonce"
    if [ -n "$startup_lease" ]; then
        [ "$startup_lease" = "$HERMES_DECISION_STARTUP_LEASE" ] \
            || die "unsupported startup lease path"
        create_decision_runtime_startup_lease "$nonce"
        if pid_running "$pid_file"; then
            cancel_pending_decision_runtime_startup_lease "$nonce" \
                || die "$name is running but the competing startup lease could not be released"
            info "$name already running (pid $PROCESS_PID)"
            return
        else
            identity_status=$?
            if [ "$identity_status" -eq 5 ]; then
                cancel_pending_decision_runtime_startup_lease "$nonce" \
                    || die "$name process identity is unknown and the competing startup lease could not be released"
                die "$name process identity is unknown; preserving metadata"
            fi
        fi
    fi
    if [ -f "$pid_file" ] || [ -f "$(identity_file "$pid_file")" ]; then
        if [ -n "$startup_lease" ]; then
            cancel_pending_decision_runtime_startup_lease "$nonce" \
                || die "$name has unverified launcher metadata and the new startup lease could not be released"
            die "$name has unverified launcher metadata; preserving it for stop/recovery"
        fi
        info "$name stale process identity ignored"
        clear_process_identity "$pid_file"
    fi
    if ! (
        cd "$REPO_ROOT"
        set -m
        if [ -n "$startup_lease" ]; then
            nohup env \
                HEALTHMES_SERVICE_NONCE="$nonce" \
                HEALTHMES_DECISION_STARTUP_LEASE_PATH="$startup_lease" \
                HEALTHMES_DECISION_STARTUP_PID_FILE="$pid_file" \
                "$BASH_BIN" "$REPO_ROOT/scripts/healthmes_local.sh" \
                __service_runner "$nonce" "$command" >>"$log_file" 2>&1 &
        else
            nohup env HEALTHMES_SERVICE_NONCE="$nonce" \
                "$BASH_BIN" "$REPO_ROOT/scripts/healthmes_local.sh" \
                __service_runner "$nonce" "$command" >>"$log_file" 2>&1 &
        fi
        pid=$!
        if [ -n "$startup_lease" ]; then
            wait_for_decision_runtime_launcher_publication "$nonce" "$pid" \
                || exit 1
        else
            write_unverified_process_pid "$pid_file" "$pid" || exit 1
        fi
        set +m
    ); then
        die "$name launcher publication failed; preserving the startup lease and any PID tombstone"
    fi
    pid="$(<"$pid_file")"
    "$SLEEP_BIN" 1
    if ! capture_process_identity "$pid_file" "$pid" "$nonce"; then
        if [ -n "$startup_lease" ]; then
            die "$name launcher identity is unknown; preserving the startup lease and PID tombstone"
        fi
        clear_process_identity "$pid_file"
        die "$name failed identity verification; see $log_file"
    fi
    if [ -n "$startup_lease" ]; then
        if ! mark_decision_runtime_startup_identity_verified \
            "$nonce" "$pid" "ps:$SNAPSHOT_START_TIME"; then
            die "$name identity was captured but startup lease ownership changed; preserving runtime metadata"
        fi
        if ! complete_decision_runtime_startup_lease "$nonce" "$pid"; then
            die "$name started but startup lease ownership changed; preserving runtime metadata"
        fi
    fi
    info "$name started (pid $pid)"
}

signal_process_group() {
    local signal=$1 pid_file=$2 pid status
    if process_identity_matches "$pid_file"; then
        :
    else
        status=$?
        return "$status"
    fi
    pid=$PROCESS_PID
    "$KILL_BIN" -s "$signal" "-$pid"
}

load_decision_runtime_stop_bounds() {
    local ps_deadline=${1:-} key value extra payload read_status
    local version= drain_timeout= launcher_pid= launcher_start_token=
    local launcher_service_nonce= supervisor_pid=
    local supervisor_start_token= service_nonce=
    local publication_instance_nonce=
    local seen_version= seen_drain_timeout= seen_launcher_pid=
    local seen_launcher_start_token= seen_launcher_service_nonce=
    local seen_supervisor_pid= seen_supervisor_start_token=
    local seen_service_nonce=
    local seen_publication_instance_nonce=
    DECISION_RUNTIME_TERM_WAIT_SECONDS=$MAX_DECISION_RUNTIME_TERM_WAIT_SECONDS
    DECISION_RUNTIME_KILL_WAIT_SECONDS=1
    DECISION_RUNTIME_BUDGET_STATUS=missing
    DECISION_RUNTIME_LAUNCHER_MATCHES=false
    DECISION_RUNTIME_SUPERVISOR_PID=
    DECISION_RUNTIME_SUPERVISOR_START_TOKEN=
    DECISION_RUNTIME_BUDGET_DRAIN_SECONDS=
    DECISION_RUNTIME_BUDGET_LAUNCHER_PID=
    DECISION_RUNTIME_BUDGET_LAUNCHER_START_TOKEN=
    DECISION_RUNTIME_BUDGET_LAUNCHER_SERVICE_NONCE=
    DECISION_RUNTIME_BUDGET_PUBLICATION_NONCE=
    if payload="$(run_native_identity_helper \
        read-shutdown-budget \
        "$HERMES_DECISION_STOP_BUDGET" \
        --max-bytes "$MAX_DECISION_RUNTIME_STOP_BUDGET_BYTES" \
        --max-drain-seconds "$MAX_DECISION_RUNTIME_DRAIN_SECONDS" \
        2>/dev/null)"; then
        :
    else
        read_status=$?
        if [ "$read_status" -eq 3 ]; then
            return 0
        fi
        info "ignoring malformed or unsafe decision runtime stop budget"
        DECISION_RUNTIME_BUDGET_STATUS=invalid
        return 0
    fi
    while IFS=$'\t' read -r key value extra; do
        [ -z "$extra" ] || {
            info "ignoring malformed decision runtime stop budget"
            DECISION_RUNTIME_BUDGET_STATUS=invalid
            return 0
        }
        case "$key" in
        version)
            [ -z "$seen_version" ] || {
                info "ignoring malformed decision runtime stop budget"
                DECISION_RUNTIME_BUDGET_STATUS=invalid
                return 0
            }
            version=$value
            seen_version=1
            ;;
        drain_timeout_seconds)
            [ -z "$seen_drain_timeout" ] || {
                info "ignoring malformed decision runtime stop budget"
                DECISION_RUNTIME_BUDGET_STATUS=invalid
                return 0
            }
            drain_timeout=$value
            seen_drain_timeout=1
            ;;
        launcher_pid)
            [ -z "$seen_launcher_pid" ] || {
                info "ignoring malformed decision runtime stop budget"
                DECISION_RUNTIME_BUDGET_STATUS=invalid
                return 0
            }
            launcher_pid=$value
            seen_launcher_pid=1
            ;;
        launcher_start_token)
            [ -z "$seen_launcher_start_token" ] || {
                info "ignoring malformed decision runtime stop budget"
                DECISION_RUNTIME_BUDGET_STATUS=invalid
                return 0
            }
            launcher_start_token=$value
            seen_launcher_start_token=1
            ;;
        launcher_service_nonce)
            [ -z "$seen_launcher_service_nonce" ] || {
                info "ignoring malformed decision runtime stop budget"
                DECISION_RUNTIME_BUDGET_STATUS=invalid
                return 0
            }
            launcher_service_nonce=$value
            seen_launcher_service_nonce=1
            ;;
        supervisor_pid)
            [ -z "$seen_supervisor_pid" ] || {
                info "ignoring malformed decision runtime stop budget"
                DECISION_RUNTIME_BUDGET_STATUS=invalid
                return 0
            }
            supervisor_pid=$value
            seen_supervisor_pid=1
            ;;
        supervisor_start_token)
            [ -z "$seen_supervisor_start_token" ] || {
                info "ignoring malformed decision runtime stop budget"
                DECISION_RUNTIME_BUDGET_STATUS=invalid
                return 0
            }
            supervisor_start_token=$value
            seen_supervisor_start_token=1
            ;;
        service_nonce)
            [ -z "$seen_service_nonce" ] || {
                info "ignoring malformed decision runtime stop budget"
                DECISION_RUNTIME_BUDGET_STATUS=invalid
                return 0
            }
            service_nonce=$value
            seen_service_nonce=1
            ;;
        publication_instance_nonce)
            [ -z "$seen_publication_instance_nonce" ] || {
                info "ignoring malformed decision runtime stop budget"
                DECISION_RUNTIME_BUDGET_STATUS=invalid
                return 0
            }
            publication_instance_nonce=$value
            seen_publication_instance_nonce=1
            ;;
        *)
            info "ignoring malformed decision runtime stop budget"
            DECISION_RUNTIME_BUDGET_STATUS=invalid
            return 0
            ;;
        esac
    done <<<"$payload"
    if ! [[ "$drain_timeout" =~ ^[1-9][0-9]*$ ]] \
        || [ "$drain_timeout" -gt "$MAX_DECISION_RUNTIME_DRAIN_SECONDS" ]; then
        info "ignoring stale or invalid decision runtime stop budget"
        DECISION_RUNTIME_BUDGET_STATUS=invalid
        return 0
    fi
    if [ "$version" = 3 ]; then
        if [ -z "$seen_version" ] \
            || [ -z "$seen_drain_timeout" ] \
            || [ -z "$seen_launcher_pid" ] \
            || [ -z "$seen_launcher_start_token" ] \
            || [ -z "$seen_launcher_service_nonce" ] \
            || [ -z "$seen_supervisor_pid" ] \
            || [ -z "$seen_supervisor_start_token" ] \
            || [ -z "$seen_publication_instance_nonce" ] \
            || [ -n "$seen_service_nonce" ] \
            || ! [[ "$launcher_pid" =~ ^[1-9][0-9]*$ ]] \
            || [ "$launcher_pid" -le 1 ] \
            || [[ "$launcher_start_token" != *:* ]] \
            || ! [[ "$launcher_service_nonce" =~ ^[A-Za-z0-9-]+$ ]] \
            || ! [[ "$supervisor_pid" =~ ^[1-9][0-9]*$ ]] \
            || [ "$supervisor_pid" -le 1 ] \
            || ! [[ "$supervisor_start_token" =~ ^(linux|darwin):.+$ ]] \
            || ! [[ "$publication_instance_nonce" =~ ^[A-Za-z0-9-]+$ ]]; then
            info "ignoring stale or invalid decision runtime stop budget"
            DECISION_RUNTIME_BUDGET_STATUS=invalid
            return 0
        fi
        DECISION_RUNTIME_BUDGET_STATUS=v3
        DECISION_RUNTIME_BUDGET_DRAIN_SECONDS=$drain_timeout
        DECISION_RUNTIME_BUDGET_LAUNCHER_PID=$launcher_pid
        DECISION_RUNTIME_BUDGET_LAUNCHER_START_TOKEN=$launcher_start_token
        DECISION_RUNTIME_BUDGET_LAUNCHER_SERVICE_NONCE=$launcher_service_nonce
        DECISION_RUNTIME_BUDGET_PUBLICATION_NONCE=$publication_instance_nonce
        DECISION_RUNTIME_SUPERVISOR_PID=$supervisor_pid
        DECISION_RUNTIME_SUPERVISOR_START_TOKEN=$supervisor_start_token
        DECISION_RUNTIME_TERM_WAIT_SECONDS=$((
            drain_timeout
            + DECISION_RUNTIME_SHUTDOWN_MARGIN_SECONDS
        ))
        if process_identity_matches \
            "$HERMES_DECISION_PID" "$ps_deadline" \
            && [ "$launcher_pid" = "$PROCESS_PID" ] \
            && [ "$launcher_start_token" = "ps:$PROCESS_START_TIME" ] \
            && [ "$launcher_service_nonce" = "$PROCESS_NONCE" ]; then
            DECISION_RUNTIME_LAUNCHER_MATCHES=true
        fi
        return 0
    fi
    if { [ "$version" = 1 ] || [ "$version" = 2 ]; } \
        && [ -n "$seen_version" ] \
        && [ -n "$seen_drain_timeout" ] \
        && [ -n "$seen_supervisor_pid" ] \
        && [ -n "$seen_supervisor_start_token" ] \
        && [ -n "$seen_service_nonce" ] \
        && [ -z "$seen_launcher_pid$seen_launcher_start_token" ] \
        && [ -z "$seen_launcher_service_nonce" ] \
        && [[ "$supervisor_pid" =~ ^[1-9][0-9]*$ ]] \
        && [ "$supervisor_pid" -gt 1 ] \
        && [[ "$supervisor_start_token" == *:* ]] \
        && [[ "$service_nonce" =~ ^[A-Za-z0-9-]+$ ]] \
        && { { [ "$version" = 1 ] \
            && [ -z "$seen_publication_instance_nonce" ]; } \
            || { [ "$version" = 2 ] \
            && [ -n "$seen_publication_instance_nonce" ] \
            && [[ "$publication_instance_nonce" =~ ^[A-Za-z0-9-]+$ ]]; }; }; then
        DECISION_RUNTIME_BUDGET_STATUS=legacy
        if process_identity_matches \
            "$HERMES_DECISION_PID" "$ps_deadline" \
            && [ "$supervisor_pid" = "$PROCESS_PID" ] \
            && [ "$supervisor_start_token" = "ps:$PROCESS_START_TIME" ] \
            && [ "$service_nonce" = "$PROCESS_NONCE" ]; then
            DECISION_RUNTIME_LAUNCHER_MATCHES=true
        fi
        return 0
    fi
    info "ignoring stale or invalid decision runtime stop budget"
    DECISION_RUNTIME_BUDGET_STATUS=invalid
}

decision_runtime_budget_matches_launcher_snapshot() {
    local launcher_pid=$1 launcher_start_token=$2 launcher_service_nonce=$3
    [ "$DECISION_RUNTIME_BUDGET_STATUS" = v3 ] \
        && [ "$DECISION_RUNTIME_BUDGET_LAUNCHER_PID" = "$launcher_pid" ] \
        && [ "$DECISION_RUNTIME_BUDGET_LAUNCHER_START_TOKEN" = "$launcher_start_token" ] \
        && [ "$DECISION_RUNTIME_BUDGET_LAUNCHER_SERVICE_NONCE" = "$launcher_service_nonce" ]
}

decision_runtime_budget_generation() {
    [ "$DECISION_RUNTIME_BUDGET_STATUS" = v3 ] || return 1
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$DECISION_RUNTIME_BUDGET_DRAIN_SECONDS" \
        "$DECISION_RUNTIME_BUDGET_LAUNCHER_PID" \
        "$DECISION_RUNTIME_BUDGET_LAUNCHER_START_TOKEN" \
        "$DECISION_RUNTIME_BUDGET_LAUNCHER_SERVICE_NONCE" \
        "$DECISION_RUNTIME_SUPERVISOR_PID" \
        "$DECISION_RUNTIME_SUPERVISOR_START_TOKEN" \
        "$DECISION_RUNTIME_BUDGET_PUBLICATION_NONCE"
}

runtime_process_identity_action() {
    local action=$1
    local timeout_seconds=${2:-}
    local -a helper_args=(
        -m healthmes.hermes_runtime_supervisor
        --runtime-process-action "$action"
        --runtime-process-pid "$DECISION_RUNTIME_SUPERVISOR_PID"
        --runtime-process-start-token
        "$DECISION_RUNTIME_SUPERVISOR_START_TOKEN"
    )
    [ -x "$RUNTIME_PYTHON_BIN" ] \
        || die "runtime identity helper is unavailable: $RUNTIME_PYTHON_BIN"
    if [ -n "$timeout_seconds" ]; then
        helper_args+=(
            --runtime-process-timeout "$timeout_seconds"
        )
    fi
    "$RUNTIME_PYTHON_BIN" "${helper_args[@]}"
}

runtime_launcher_group_is_empty() {
    local pgid=$1 deadline=${2:-} timeout
    [ -x "$RUNTIME_PYTHON_BIN" ] \
        || die "runtime identity helper is unavailable: $RUNTIME_PYTHON_BIN"
    timeout="$(bounded_ps_timeout_seconds "$deadline")" || return 5
    "$RUNTIME_PYTHON_BIN" \
        -m healthmes.hermes_runtime_supervisor \
        --runtime-process-group-pgid "$pgid" \
        --runtime-process-timeout "$timeout"
}

wait_for_decision_runtime_exit() {
    local timeout_seconds=$1 status
    if runtime_process_identity_action wait "$timeout_seconds"; then
        :
    else
        status=$?
        case "$status" in
        4) die "decision runtime supervisor PID was reused while waiting; refusing unverified cleanup" ;;
        5) die "decision runtime supervisor identity cannot be verified while waiting" ;;
        6) return 1 ;;
        *) die "decision runtime supervisor wait failed with status $status" ;;
        esac
    fi
    if [ ! -f "$(identity_file "$HERMES_DECISION_PID")" ]; then
        # A matching startup lease and v3 budget own an unverified PID
        # tombstone. The caller validates that generation after the
        # supervisor proves cleanup; there is no wrapper identity to probe.
        return 0
    fi
    if process_identity_matches "$HERMES_DECISION_PID"; then
        # The wrapper normally exits as soon as its supervised Python process
        # does. Bound this final reap check without consuming the drain budget.
        "$SLEEP_BIN" 1
        if process_identity_matches "$HERMES_DECISION_PID"; then
            die "managed launcher remained alive after its Python supervisor exited"
        else
            status=$?
            case "$status" in
            3 | 4) ;;
            *) die "managed launcher identity became unknown after its Python supervisor exited; preserving metadata" ;;
            esac
        fi
    else
        status=$?
        case "$status" in
        3 | 4) ;;
        *) die "managed launcher identity is unknown after its Python supervisor exited; preserving metadata" ;;
        esac
    fi
    return 0
}

wait_for_process_exit() {
    local pid_file=$1 timeout_seconds=$2 polls status
    polls=$timeout_seconds
    while [ "$polls" -gt 0 ]; do
        if process_identity_matches "$pid_file"; then
            :
        else
            status=$?
            case "$status" in
            3 | 4) return 0 ;;
            *) return 5 ;;
            esac
        fi
        "$SLEEP_BIN" 1
        polls=$((polls - 1))
    done
    if process_identity_matches "$pid_file"; then
        return 6
    fi
    status=$?
    case "$status" in
    3 | 4) return 0 ;;
    *) return 5 ;;
    esac
}

stop_process() {
    local name=$1 pid_file=$2
    local term_wait_seconds=${3:-2}
    local kill_wait_seconds=${4:-1}
    local allow_force_kill=${5:-true}
    local status
    if process_identity_matches "$pid_file"; then
        :
    else
        status=$?
        case "$status" in
        3 | 4) ;;
        *) die "$name process identity is unknown; preserving metadata" ;;
        esac
        clear_process_identity "$pid_file"
        info "$name stopped"
        return
    fi
    if signal_process_group TERM "$pid_file" 2>/dev/null; then
        :
    else
        status=$?
        case "$status" in
        3 | 4)
            clear_process_identity "$pid_file"
            info "$name stopped"
            return
            ;;
        5) die "$name process identity became unknown before SIGTERM; preserving metadata" ;;
        *) die "$name could not be signalled with SIGTERM; preserving metadata" ;;
        esac
    fi
    if wait_for_process_exit "$pid_file" "$term_wait_seconds"; then
        clear_process_identity "$pid_file"
        info "$name stopped"
        return
    else
        status=$?
        [ "$status" -eq 6 ] \
            || die "$name process identity became unknown after SIGTERM; preserving metadata"
    fi
    if [ "$allow_force_kill" != true ]; then
        die "$name did not stop within ${term_wait_seconds}s; refusing to orphan its child process group"
    fi
    if signal_process_group KILL "$pid_file" 2>/dev/null; then
        :
    else
        status=$?
        case "$status" in
        3 | 4)
            clear_process_identity "$pid_file"
            info "$name stopped"
            return
            ;;
        5) die "$name process identity became unknown before SIGKILL; preserving metadata" ;;
        *) die "$name could not be signalled with SIGKILL; preserving metadata" ;;
        esac
    fi
    if wait_for_process_exit "$pid_file" "$kill_wait_seconds"; then
        :
    else
        status=$?
        if [ "$status" -eq 5 ]; then
            die "$name process identity became unknown after SIGKILL; preserving metadata"
        fi
        die "$name remained alive ${kill_wait_seconds}s after SIGKILL"
    fi
    clear_process_identity "$pid_file"
    info "$name stopped"
}

stop_decision_launcher_without_budget() {
    local launcher_pid=$1 launcher_executable=$2
    local launcher_start_time=$3 launcher_service_nonce=$4
    local status
    if captured_process_identity_matches \
        "$launcher_pid" \
        "$launcher_executable" \
        "$launcher_start_time" \
        "$launcher_service_nonce"; then
        :
    else
        status=$?
        case "$status" in
        3 | 4) die "decision runtime launcher identity changed before shutdown handoff; preserving metadata" ;;
        *) die "decision runtime launcher identity is unknown before shutdown handoff; preserving metadata" ;;
        esac
    fi
    "$KILL_BIN" -s TERM "-$launcher_pid" \
        || die "failed to signal verified decision runtime launcher; preserving metadata"
    local polls=$MAX_DECISION_RUNTIME_TERM_WAIT_SECONDS
    while true; do
        if captured_process_identity_matches \
            "$launcher_pid" \
            "$launcher_executable" \
            "$launcher_start_time" \
            "$launcher_service_nonce"; then
            [ "$polls" -gt 0 ] \
                || die "Hermes decision runtime did not stop within ${MAX_DECISION_RUNTIME_TERM_WAIT_SECONDS}s; refusing to orphan its child process group"
            "$SLEEP_BIN" 1
            polls=$((polls - 1))
            continue
        else
            status=$?
        fi
        case "$status" in
        3 | 4) return 0 ;;
        *) die "decision runtime launcher identity became unknown during shutdown; preserving metadata" ;;
        esac
    done
}

startup_lease_owner_identity_status() {
    local deadline=${1:-}
    [ "$DECISION_RUNTIME_STARTUP_LEASE_VERSION" = 2 ] || return 5
    process_start_token_status \
        "$DECISION_RUNTIME_STARTUP_LEASE_OWNER_PID" \
        "$DECISION_RUNTIME_STARTUP_LEASE_OWNER_START_TOKEN" \
        "$deadline"
}

startup_lease_launcher_identity_status() {
    local deadline=${1:-}
    local pid=$DECISION_RUNTIME_STARTUP_LEASE_PID status marker
    valid_managed_pid "$pid" || return 5
    if load_process_snapshot "$pid" "$deadline"; then
        :
    else
        status=$?
        [ "$status" -eq 3 ] && return 3
        return 5
    fi
    marker="healthmes_local.sh __service_runner $DECISION_RUNTIME_STARTUP_LEASE_SERVICE_NONCE "
    if [ "$SNAPSHOT_PID" = "$pid" ] \
        && [ "$SNAPSHOT_PGID" = "$pid" ] \
        && [ "${SNAPSHOT_EXECUTABLE##*/}" = "bash" ] \
        && [[ "$SNAPSHOT_COMMAND" == *"$marker"* ]]; then
        return 0
    fi
    return 4
}

startup_lease_matches_loaded_v3_budget() {
    [ "$DECISION_RUNTIME_BUDGET_STATUS" = v3 ] \
        && decision_runtime_startup_lease_matches_launcher \
            "$DECISION_RUNTIME_BUDGET_LAUNCHER_PID" \
            "$DECISION_RUNTIME_BUDGET_LAUNCHER_SERVICE_NONCE"
}

find_interrupted_decision_runtime_startup_record() {
    local entry count=0
    local -a candidates=(
        "$HERMES_DECISION_STARTUP_LEASE"/.record.*
        "$RUNTIME_DIR"/.startup-lease-record.*
    )
    INTERRUPTED_DECISION_RUNTIME_RECORD=
    [ -d "$HERMES_DECISION_STARTUP_LEASE" ] \
        && [ ! -L "$HERMES_DECISION_STARTUP_LEASE" ] \
        && [ ! -e "$(decision_runtime_startup_lease_record)" ] \
        && [ ! -L "$(decision_runtime_startup_lease_record)" ] \
        || return 1
    record_directory_has_only_managed_entries \
        "$HERMES_DECISION_STARTUP_LEASE" || return 2
    for entry in "${candidates[@]}"; do
        [ -e "$entry" ] || [ -L "$entry" ] || continue
        [ -f "$entry" ] && [ ! -L "$entry" ] || return 2
        count=$((count + 1))
        INTERRUPTED_DECISION_RUNTIME_RECORD=$entry
    done
    [ "$count" -eq 1 ] || return 2
}

recover_interrupted_decision_runtime_startup_record() {
    local deadline=${1:-} candidate generation owner_status record status
    local candidate_sha256
    find_interrupted_decision_runtime_startup_record || return $?
    candidate=$INTERRUPTED_DECISION_RUNTIME_RECORD
    load_decision_runtime_startup_lease "$candidate"
    [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" != missing ] \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" != invalid ] \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_VERSION" = 2 ] \
        || return 2
    generation="$(decision_runtime_startup_lease_generation)" || return 2
    if startup_lease_owner_identity_status "$deadline"; then
        return 3
    else
        owner_status=$?
    fi
    [ "$owner_status" -eq 3 ] || return 5
    epoch_age_at_least \
        "$DECISION_RUNTIME_STARTUP_LEASE_UPDATED_AT" \
        "$DECISION_RUNTIME_STARTUP_RECOVERY_GRACE_SECONDS" \
        || return 4
    candidate_sha256="$(managed_record_sha256 "$candidate")" \
        || return 2
    load_decision_runtime_startup_lease "$candidate"
    [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" != missing ] \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" != invalid ] \
        && [ "$(decision_runtime_startup_lease_generation)" = "$generation" ] \
        || return 2
    record="$(decision_runtime_startup_lease_record)"
    if publish_interrupted_managed_record \
        "$candidate" "$record" "$candidate_sha256"; then
        :
    else
        status=$?
        [ "$status" -eq 6 ] || return 2
        load_decision_runtime_startup_lease
        [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" != missing ] \
            && [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" != invalid ] \
            && [ "$(decision_runtime_startup_lease_generation)" = "$generation" ] \
            || return 2
        return 0
    fi
    load_decision_runtime_startup_lease
    [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" != missing ] \
        && [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" != invalid ] \
        && [ "$(decision_runtime_startup_lease_generation)" = "$generation" ]
}

clear_stale_startup_artifacts() {
    local expected_generation=$1 launcher_pid=${2:-}
    local launcher_service_nonce=$3 stored_pid
    if [ -f "$(identity_file "$HERMES_DECISION_PID")" ]; then
        load_process_identity "$HERMES_DECISION_PID" \
            && [ -n "$launcher_pid" ] \
            && [ "$PROCESS_PID" = "$launcher_pid" ] \
            && [ "$PROCESS_NONCE" = "$launcher_service_nonce" ] \
            || die "stale startup launcher identity conflicts with its lease; preserving metadata"
    elif [ -f "$HERMES_DECISION_PID" ]; then
        [ ! -L "$HERMES_DECISION_PID" ] \
            || die "stale startup PID tombstone is unsafe; preserving metadata"
        stored_pid="$(<"$HERMES_DECISION_PID")"
        [ -n "$launcher_pid" ] && [ "$stored_pid" = "$launcher_pid" ] \
            || die "stale startup PID tombstone conflicts with its lease; preserving metadata"
    fi
    remove_decision_runtime_startup_lease_generation "$expected_generation" \
        || die "stale startup lease changed during recovery; preserving its diagnostic"
    if [ -f "$HERMES_DECISION_PID" ] \
        || [ -f "$(identity_file "$HERMES_DECISION_PID")" ]; then
        clear_process_identity "$HERMES_DECISION_PID"
    fi
}

reconcile_stale_decision_runtime_startup() {
    local attempts=0 owner_status launcher_status group_status
    local expected_generation launcher_pid launcher_service_nonce
    local recovery_deadline
    recovery_deadline=$((
        SECONDS + DECISION_RUNTIME_STARTUP_RECOVERY_GRACE_SECONDS
    ))

    load_decision_runtime_startup_lease
    case "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" in
    missing) return ;;
    invalid)
        if recover_interrupted_decision_runtime_startup_record \
            "$recovery_deadline"; then
            info "restored an interrupted decision runtime startup lease record"
            load_decision_runtime_startup_lease
        else
            owner_status=$?
            case "$owner_status" in
            3)
                die "decision runtime startup lease publication is still owned by a live process; preserving its orphan record"
                ;;
            4)
                die "decision runtime startup lease orphan has not passed the bounded recovery grace; preserving it"
                ;;
            5)
                die "decision runtime startup lease orphan owner identity is unknown; preserving it"
                ;;
            *)
                die "decision runtime startup lease is invalid; refusing lifecycle recovery"
                ;;
            esac
        fi
        ;;
    esac
    [ "$DECISION_RUNTIME_STARTUP_LEASE_VERSION" = 2 ] || return 0

    while [ "$attempts" -le "$DECISION_RUNTIME_STARTUP_RECOVERY_GRACE_SECONDS" ]; do
        load_decision_runtime_stop_bounds "$recovery_deadline"
        if [ "$DECISION_RUNTIME_BUDGET_STATUS" = v3 ]; then
            startup_lease_matches_loaded_v3_budget \
                || die "decision runtime stop budget does not match the startup lease generation"
            return
        fi
        [ "$DECISION_RUNTIME_BUDGET_STATUS" != invalid ] \
            || die "decision runtime stop budget is invalid during startup recovery"
        if [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" = identity_verified ]; then
            return
        fi
        if epoch_age_at_least \
            "$DECISION_RUNTIME_STARTUP_LEASE_UPDATED_AT" \
            "$DECISION_RUNTIME_STARTUP_RECOVERY_GRACE_SECONDS"; then
            break
        fi
        if [ "$attempts" -ge "$DECISION_RUNTIME_STARTUP_RECOVERY_GRACE_SECONDS" ]; then
            die "decision runtime startup lease did not become recoverable within the bounded grace; preserving it"
        fi
        [ "$SECONDS" -lt "$recovery_deadline" ] \
            || die "decision runtime startup lease did not become recoverable within the bounded grace; preserving it"
        "$SLEEP_BIN" 1
        attempts=$((attempts + 1))
        load_decision_runtime_startup_lease
        [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" != missing ] \
            && [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" != invalid ] \
            && [ "$DECISION_RUNTIME_STARTUP_LEASE_VERSION" = 2 ] \
            || die "decision runtime startup lease changed during bounded recovery; preserving runtime state"
    done

    if startup_lease_owner_identity_status "$recovery_deadline"; then
        die "decision runtime startup owner is still alive but its publication is incomplete; preserving the lease"
    else
        owner_status=$?
    fi
    [ "$owner_status" -eq 3 ] \
        || die "decision runtime startup owner identity is unknown; preserving the lease"

    load_decision_runtime_stop_bounds "$recovery_deadline"
    if [ "$DECISION_RUNTIME_BUDGET_STATUS" = v3 ]; then
        startup_lease_matches_loaded_v3_budget \
            || die "decision runtime stop budget does not match the startup lease generation"
        return
    fi
    [ "$DECISION_RUNTIME_BUDGET_STATUS" = missing ] \
        || die "decision runtime startup recovery found unusable shutdown evidence; preserving metadata"

    expected_generation="$(decision_runtime_startup_lease_generation)"
    launcher_pid=$DECISION_RUNTIME_STARTUP_LEASE_PID
    launcher_service_nonce=$DECISION_RUNTIME_STARTUP_LEASE_SERVICE_NONCE
    if [ -z "$launcher_pid" ]; then
        [ ! -f "$HERMES_DECISION_PID" ] \
            && [ ! -f "$(identity_file "$HERMES_DECISION_PID")" ] \
            || die "PID metadata appeared before the startup lease published a launcher PID; preserving the conflict"
        clear_stale_startup_artifacts \
            "$expected_generation" "" "$launcher_service_nonce"
        info "recovered stale decision runtime startup intent after verified owner exit"
        return
    fi

    if startup_lease_launcher_identity_status "$recovery_deadline"; then
        if [ -f "$(identity_file "$HERMES_DECISION_PID")" ]; then
            load_process_identity "$HERMES_DECISION_PID" \
                && [ "$PROCESS_PID" = "$launcher_pid" ] \
                && [ "$PROCESS_NONCE" = "$launcher_service_nonce" ] \
                || die "live startup launcher metadata conflicts with its lease; preserving metadata"
        else
            if [ -f "$HERMES_DECISION_PID" ]; then
                [ ! -L "$HERMES_DECISION_PID" ] \
                    && [ "$(<"$HERMES_DECISION_PID")" = "$launcher_pid" ] \
                    || die "live startup PID tombstone conflicts with its lease; preserving metadata"
            fi
            write_process_identity \
                "$HERMES_DECISION_PID" \
                "$launcher_pid" \
                "$launcher_service_nonce"
        fi
        return
    else
        launcher_status=$?
    fi
    case "$launcher_status" in
    3 | 4) ;;
    *)
        die "decision runtime startup launcher identity is unknown; preserving the lease"
        ;;
    esac

    if runtime_launcher_group_is_empty \
        "$launcher_pid" "$recovery_deadline"; then
        :
    else
        group_status=$?
        case "$group_status" in
        6)
            die "dead startup launcher still has untracked process-group members and no v3 budget; preserving the lease"
            ;;
        *)
            die "dead startup launcher process-group state is unknown; preserving the lease"
            ;;
        esac
    fi
    load_decision_runtime_stop_bounds "$recovery_deadline"
    if [ "$DECISION_RUNTIME_BUDGET_STATUS" = v3 ]; then
        startup_lease_matches_loaded_v3_budget \
            || die "late decision runtime stop budget does not match the startup lease generation"
        return
    fi
    [ "$DECISION_RUNTIME_BUDGET_STATUS" = missing ] \
        || die "decision runtime shutdown evidence became unusable during startup recovery"
    clear_stale_startup_artifacts \
        "$expected_generation" "$launcher_pid" "$launcher_service_nonce"
    info "recovered stale decision runtime startup after proving its launcher group empty"
}

clear_decision_launcher_metadata_if_owned() {
    local metadata_present=$1 metadata_valid=$2
    local launcher_pid=$3 launcher_executable=$4
    local launcher_start_time=$5 launcher_service_nonce=$6
    local pid_file_present=false identity_file_present=false

    [ -f "$HERMES_DECISION_PID" ] && pid_file_present=true
    [ -f "$(identity_file "$HERMES_DECISION_PID")" ] \
        && identity_file_present=true
    if [ "$pid_file_present" = false ] \
        && [ "$identity_file_present" = false ]; then
        return
    fi
    [ "$metadata_present" = true ] \
        && [ "$metadata_valid" = true ] \
        || die "decision runtime stopped but launcher metadata ownership changed; preserving metadata"
    load_process_identity "$HERMES_DECISION_PID" \
        || die "decision runtime stopped but launcher metadata became invalid; preserving metadata"
    [ "$PROCESS_PID" = "$launcher_pid" ] \
        && [ "$PROCESS_EXECUTABLE" = "$launcher_executable" ] \
        && [ "$PROCESS_START_TIME" = "$launcher_start_time" ] \
        && [ "$PROCESS_NONCE" = "$launcher_service_nonce" ] \
        || die "decision runtime stopped but launcher metadata generation changed; preserving metadata"
    clear_process_identity "$HERMES_DECISION_PID"
}

clear_decision_runtime_metadata_if_owned() {
    local metadata_present=$1 metadata_valid=$2
    local launcher_pid=$3 launcher_executable=$4
    local launcher_start_time=$5 launcher_service_nonce=$6
    local startup_lease_present=$7 startup_lease_valid=$8
    local startup_lease_status=$9 startup_lease_pid=${10}
    local startup_lease_service_nonce=${11}
    local expected_launcher_pid=${12}
    local expected_launcher_service_nonce=${13}
    local expected_startup_lease_generation=${14:-}
    local current_generation stored_pid

    load_decision_runtime_startup_lease
    if [ "$startup_lease_present" = false ]; then
        [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" = missing ] \
            || die "decision runtime stopped but a new startup lease appeared; preserving runtime metadata"
        clear_decision_launcher_metadata_if_owned \
            "$metadata_present" \
            "$metadata_valid" \
            "$launcher_pid" \
            "$launcher_executable" \
            "$launcher_start_time" \
            "$launcher_service_nonce"
        return
    fi

    [ "$startup_lease_valid" = true ] \
        || die "decision runtime startup lease is invalid; preserving runtime metadata"
    decision_runtime_startup_lease_matches_launcher \
        "$expected_launcher_pid" \
        "$expected_launcher_service_nonce" \
        "$startup_lease_status" \
        "$startup_lease_pid" \
        "$startup_lease_service_nonce" \
        || die "decision runtime startup lease does not own the stopped generation; preserving runtime metadata"
    current_generation="$(decision_runtime_startup_lease_generation)" \
        || die "decision runtime startup lease generation is unavailable"
    [ -n "$expected_startup_lease_generation" ] \
        && [ "$current_generation" = "$expected_startup_lease_generation" ] \
        || die "decision runtime startup lease generation changed during cleanup; preserving runtime metadata"
    decision_runtime_startup_lease_matches_launcher \
        "$expected_launcher_pid" \
        "$expected_launcher_service_nonce" \
        || die "decision runtime startup lease generation changed during cleanup; preserving runtime metadata"

    if [ -f "$(identity_file "$HERMES_DECISION_PID")" ]; then
        load_process_identity "$HERMES_DECISION_PID" \
            || die "decision runtime startup identity became invalid during cleanup; preserving runtime metadata"
        [ "$PROCESS_PID" = "$expected_launcher_pid" ] \
            && [ "$PROCESS_NONCE" = "$expected_launcher_service_nonce" ] \
            || die "decision runtime startup identity changed during cleanup; preserving runtime metadata"
    elif [ -f "$HERMES_DECISION_PID" ]; then
        [ ! -L "$HERMES_DECISION_PID" ] \
            || die "decision runtime startup PID tombstone is unsafe; preserving runtime metadata"
        stored_pid="$(<"$HERMES_DECISION_PID")"
        [ "$stored_pid" = "$expected_launcher_pid" ] \
            || die "decision runtime startup PID tombstone changed during cleanup; preserving runtime metadata"
    fi
    remove_decision_runtime_startup_lease_generation \
        "$expected_startup_lease_generation" \
        || die "decision runtime startup lease changed before removal; preserving its diagnostic"
    if [ -f "$(identity_file "$HERMES_DECISION_PID")" ]; then
        load_process_identity "$HERMES_DECISION_PID" \
            || die "decision runtime startup identity changed after lease removal; preserving runtime metadata"
        [ "$PROCESS_PID" = "$expected_launcher_pid" ] \
            && [ "$PROCESS_NONCE" = "$expected_launcher_service_nonce" ] \
            || die "decision runtime startup identity generation changed after lease removal; preserving runtime metadata"
    elif [ -f "$HERMES_DECISION_PID" ]; then
        [ ! -L "$HERMES_DECISION_PID" ] \
            || die "decision runtime startup PID tombstone became unsafe; preserving runtime metadata"
        stored_pid="$(<"$HERMES_DECISION_PID")"
        [ "$stored_pid" = "$expected_launcher_pid" ] \
            || die "decision runtime startup PID tombstone changed after lease removal; preserving runtime metadata"
    else
        return 0
    fi
    clear_process_identity "$HERMES_DECISION_PID"
}

finish_decision_launcher_handoff() {
    local launcher_pid=$1 launcher_start_token=$2
    local launcher_service_nonce=$3 group_status

    # A late v3 publication wins the handoff and identifies the supervisor.
    load_decision_runtime_stop_bounds
    if [ "$DECISION_RUNTIME_BUDGET_STATUS" != missing ]; then
        return
    fi

    if runtime_launcher_group_is_empty "$launcher_pid"; then
        # The budget is published before Hermes can enter its separate process
        # group. Once the launcher group is empty, no matching publisher can
        # create a new record, so this final read is stable.
        load_decision_runtime_stop_bounds
        return
    else
        group_status=$?
    fi

    # The supervisor may have published while the native group probe ran.
    load_decision_runtime_stop_bounds
    if [ "$DECISION_RUNTIME_BUDGET_STATUS" != missing ]; then
        return
    fi
    case "$group_status" in
    6)
        die "decision runtime launcher exited with untracked descendants and no v3 shutdown record; preserving launcher metadata"
        ;;
    *)
        die "decision runtime launcher-group cleanup cannot be proven; preserving launcher metadata"
        ;;
    esac
}

stop_decision_runtime() {
    local wrapper_alive=false launcher_metadata_present=false
    local launcher_metadata_valid=false
    local launcher_pid= launcher_executable= launcher_start_time=
    local launcher_start_token= launcher_service_nonce=
    local startup_lease_present=false startup_lease_valid=false
    local startup_lease_status= startup_lease_pid=
    local startup_lease_service_nonce= startup_lease_generation=
    local budget_launcher_pid= budget_launcher_service_nonce=

    [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_HELD" = true ] \
        || die "decision runtime stop requires the cross-process lifecycle lock"
    reconcile_stale_decision_runtime_startup
    load_decision_runtime_startup_lease
    if [ "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" != missing ]; then
        startup_lease_present=true
        startup_lease_status=$DECISION_RUNTIME_STARTUP_LEASE_STATUS
        startup_lease_pid=$DECISION_RUNTIME_STARTUP_LEASE_PID
        startup_lease_service_nonce=$DECISION_RUNTIME_STARTUP_LEASE_SERVICE_NONCE
        startup_lease_generation="$(decision_runtime_startup_lease_generation)" \
            || die "decision runtime startup lease generation is unavailable"
        case "$DECISION_RUNTIME_STARTUP_LEASE_STATUS" in
        pending | spawned | intent | failed | identity_verified)
            startup_lease_valid=true
            ;;
        esac
    fi

    if [ -f "$HERMES_DECISION_PID" ] \
        || [ -f "$(identity_file "$HERMES_DECISION_PID")" ]; then
        launcher_metadata_present=true
        if load_process_identity "$HERMES_DECISION_PID"; then
            launcher_metadata_valid=true
            launcher_pid=$PROCESS_PID
            launcher_executable=$PROCESS_EXECUTABLE
            launcher_start_time=$PROCESS_START_TIME
            launcher_start_token="ps:$PROCESS_START_TIME"
            launcher_service_nonce=$PROCESS_NONCE
            if captured_process_identity_matches \
                "$launcher_pid" \
                "$launcher_executable" \
                "$launcher_start_time" \
                "$launcher_service_nonce"; then
                wrapper_alive=true
            fi
        fi
    fi
    if [ "$startup_lease_present" = true ] \
        && [ "$startup_lease_valid" = true ] \
        && [ "$launcher_metadata_valid" = true ]; then
        decision_runtime_startup_lease_matches_launcher \
            "$launcher_pid" \
            "$launcher_service_nonce" \
            "$startup_lease_status" \
            "$startup_lease_pid" \
            "$startup_lease_service_nonce" \
            || die "decision runtime startup lease does not match the verified launcher generation"
    fi
    load_decision_runtime_stop_bounds

    while true; do
        if [ "$DECISION_RUNTIME_BUDGET_STATUS" = v3 ]; then
            budget_launcher_pid=$DECISION_RUNTIME_BUDGET_LAUNCHER_PID
            budget_launcher_service_nonce=$DECISION_RUNTIME_BUDGET_LAUNCHER_SERVICE_NONCE
            if [ "$startup_lease_present" = true ]; then
                [ "$startup_lease_valid" = true ] \
                    || die "decision runtime startup lease is invalid; refusing unverified shutdown"
                decision_runtime_startup_lease_matches_launcher \
                    "$DECISION_RUNTIME_BUDGET_LAUNCHER_PID" \
                    "$DECISION_RUNTIME_BUDGET_LAUNCHER_SERVICE_NONCE" \
                    "$startup_lease_status" \
                    "$startup_lease_pid" \
                    "$startup_lease_service_nonce" \
                    || die "decision runtime stop budget does not match the startup lease generation"
            fi
            if [ "$launcher_metadata_valid" = true ] \
                && ! decision_runtime_budget_matches_launcher_snapshot \
                    "$launcher_pid" \
                    "$launcher_start_token" \
                    "$launcher_service_nonce"; then
                die "decision runtime stop budget does not match the managed launcher generation"
            fi
            stop_verified_decision_runtime "$wrapper_alive"
            clear_decision_runtime_metadata_if_owned \
                "$launcher_metadata_present" \
                "$launcher_metadata_valid" \
                "$launcher_pid" \
                "$launcher_executable" \
                "$launcher_start_time" \
                "$launcher_service_nonce" \
                "$startup_lease_present" \
                "$startup_lease_valid" \
                "$startup_lease_status" \
                "$startup_lease_pid" \
                "$startup_lease_service_nonce" \
                "$budget_launcher_pid" \
                "$budget_launcher_service_nonce" \
                "$startup_lease_generation"
            info "Hermes decision runtime stopped"
            return
        fi
        if [ "$DECISION_RUNTIME_BUDGET_STATUS" = legacy ]; then
            [ "$startup_lease_present" = false ] \
                || die "legacy decision runtime budget cannot be reconciled with an active startup lease"
            [ "$wrapper_alive" = true ] \
                || die "legacy decision runtime budget cannot identify a surviving Python supervisor"
            [ "$DECISION_RUNTIME_LAUNCHER_MATCHES" = true ] \
                || die "legacy decision runtime budget does not match the managed launcher"
            stop_process \
                "Hermes decision runtime" \
                "$HERMES_DECISION_PID" \
                "$DECISION_RUNTIME_TERM_WAIT_SECONDS" \
                "$DECISION_RUNTIME_KILL_WAIT_SECONDS" \
                false
            rm -f "$HERMES_DECISION_STOP_BUDGET"
            return
        fi
        if [ "$DECISION_RUNTIME_BUDGET_STATUS" = invalid ]; then
            die "decision runtime stop budget is invalid; refusing unverified shutdown"
        fi
        if [ "$startup_lease_present" = true ] \
            && [ "$startup_lease_valid" = false ]; then
            die "decision runtime startup lease is invalid and no v3 supervisor record is available; preserving metadata"
        fi
        if [ "$launcher_metadata_present" = false ]; then
            [ "$startup_lease_present" = false ] \
                || die "decision runtime startup is unresolved and no v3 supervisor record is available; preserving the startup lease"
            info "Hermes decision runtime stopped"
            return
        fi
        if [ "$launcher_metadata_valid" != true ]; then
            if [ "$startup_lease_present" = true ]; then
                die "decision runtime launcher identity is unverified and no v3 supervisor record is available; preserving the startup lease and PID tombstone"
            fi
            die "decision runtime launcher metadata is invalid and no v3 supervisor record is available; preserving metadata"
        fi

        if [ "$wrapper_alive" = true ]; then
            # Close the startup race immediately before signalling the exact
            # launcher generation captured at the beginning of stop.
            load_decision_runtime_stop_bounds
            if [ "$DECISION_RUNTIME_BUDGET_STATUS" != missing ]; then
                continue
            fi
            stop_decision_launcher_without_budget \
                "$launcher_pid" \
                "$launcher_executable" \
                "$launcher_start_time" \
                "$launcher_service_nonce"
            wrapper_alive=false
        fi

        finish_decision_launcher_handoff \
            "$launcher_pid" \
            "$launcher_start_token" \
            "$launcher_service_nonce"
        if [ "$DECISION_RUNTIME_BUDGET_STATUS" = missing ]; then
            clear_decision_runtime_metadata_if_owned \
                "$launcher_metadata_present" \
                "$launcher_metadata_valid" \
                "$launcher_pid" \
                "$launcher_executable" \
                "$launcher_start_time" \
                "$launcher_service_nonce" \
                "$startup_lease_present" \
                "$startup_lease_valid" \
                "$startup_lease_status" \
                "$startup_lease_pid" \
                "$startup_lease_service_nonce" \
                "$launcher_pid" \
                "$launcher_service_nonce" \
                "$startup_lease_generation"
            info "Hermes decision runtime stopped"
            return
        fi
        if [ "$DECISION_RUNTIME_BUDGET_STATUS" = v3 ]; then
            decision_runtime_budget_matches_launcher_snapshot \
                "$launcher_pid" \
                "$launcher_start_token" \
                "$launcher_service_nonce" \
                || die "decision runtime shutdown budget generation changed during launcher handoff; preserving shutdown budget and launcher metadata"
            continue
        fi
        die "decision runtime shutdown evidence appeared malformed during launcher handoff; preserving launcher metadata"
    done
}

stop_verified_decision_runtime() {
    local wrapper_alive=$1 signal_status supervisor_live=false
    local active_generation
    active_generation="$(decision_runtime_budget_generation)" \
        || die "decision runtime v3 shutdown generation is unavailable"

    if runtime_process_identity_action probe; then
        supervisor_live=true
    else
        signal_status=$?
        case "$signal_status" in
        3) ;;
        4) die "decision runtime supervisor PID was reused; refusing unverified cleanup" ;;
        *) die "decision runtime supervisor identity cannot be verified" ;;
        esac
    fi
    if [ "$supervisor_live" = true ]; then
        if [ "$wrapper_alive" = true ]; then
            info "signaling verified Python supervisor through native identity"
        else
            info "managed launcher unavailable; signaling verified Python supervisor"
        fi
        if runtime_process_identity_action signal; then
            :
        else
            signal_status=$?
            case "$signal_status" in
            3) ;;
            4) die "decision runtime supervisor PID was reused before signal; refusing unverified cleanup" ;;
            5) die "decision runtime supervisor identity cannot be verified for signal" ;;
            *) die "failed to signal verified decision runtime supervisor" ;;
            esac
        fi
    fi
    if ! wait_for_decision_runtime_exit \
        "$DECISION_RUNTIME_TERM_WAIT_SECONDS"; then
        die "Hermes decision runtime did not stop within ${DECISION_RUNTIME_TERM_WAIT_SECONDS}s; refusing to orphan its child process group"
    fi

    load_decision_runtime_stop_bounds
    if [ "$DECISION_RUNTIME_BUDGET_STATUS" = missing ]; then
        return
    fi
    if [ "$DECISION_RUNTIME_BUDGET_STATUS" = v3 ]; then
        if [ "$(decision_runtime_budget_generation)" != "$active_generation" ]; then
            die "decision runtime shutdown budget generation changed during stop; preserving shutdown budget and launcher metadata"
        fi
        die "decision runtime supervisor exited without proving Hermes descendant cleanup; preserving shutdown budget and launcher metadata"
    fi
    die "decision runtime shutdown evidence became invalid during stop; preserving launcher metadata"
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
    set_decision_runtime_lifecycle_phase preflight \
        || die "failed to journal install preflight"
    DECISION_RUNTIME_DURABLE_MUTATION_STARTED=true
    set_decision_runtime_lifecycle_phase stopping \
        || die "failed to journal install shutdown"
    stop_launch_agent
    load_runtime_env
    stop_decision_runtime
    set_decision_runtime_lifecycle_phase setup \
        || die "failed to journal install setup"
    bash "$DEV_MAC_SCRIPT" setup
    set_decision_runtime_lifecycle_phase restarting \
        || die "failed to journal install launch-agent registration"
    install_launch_agent
    info "installed and configured to start at login"
}

loaded_decision_runtime_update_handoff_matches() {
    local expected_version=$1 expected_owner_pid=$2
    local expected_owner_start_token=$3 expected_owner_nonce=$4
    local expected_acquired_at=$5 expected_updated_at=$6
    local expected_contract_version=$7 expected_script_sha256=$8
    [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_STATUS" = valid ] \
        && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_VERSION" = "$expected_version" ] \
        && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_OPERATION" = update ] \
        && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_PHASE" = pulling ] \
        && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_PID" = "$expected_owner_pid" ] \
        && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_START_TOKEN" = "$expected_owner_start_token" ] \
        && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_NONCE" = "$expected_owner_nonce" ] \
        && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_ACQUIRED_AT" = "$expected_acquired_at" ] \
        && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_UPDATED_AT" = "$expected_updated_at" ] \
        && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_SCRIPT_CONTRACT_VERSION" = "$expected_contract_version" ] \
        && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_SCRIPT_SHA256" = "$expected_script_sha256" ]
}

reexec_updated_local_script_if_needed() {
    [ "$#" -eq 9 ] \
        || die "invalid decision runtime update handoff arguments"
    local restart_launch_agent=$1 expected_version=$2
    local expected_owner_pid=$3 expected_owner_start_token=$4
    local expected_owner_nonce=$5 expected_acquired_at=$6
    local expected_updated_at=$7 expected_contract_version=$8
    local expected_old_script_sha256=$9 current_script_sha256

    current_script_sha256="$(current_lifecycle_script_sha256)" \
        || die "updated healthmes_local.sh identity is unreadable; preserving the lifecycle journal"
    [ "$current_script_sha256" != "$expected_old_script_sha256" ] \
        || return 0
    [ "${HEALTHMES_INTERNAL_UPDATE_HANDOFF_DEPTH:-0}" = 0 ] \
        || die "recursive decision runtime update handoff refused; preserving the lifecycle journal"
    export HEALTHMES_INTERNAL_UPDATE_HANDOFF_DEPTH=1
    if ! exec "$BASH_BIN" "$REPO_ROOT/scripts/healthmes_local.sh" \
        __resume_update_after_pull \
        "$restart_launch_agent" \
        "$expected_version" \
        "$expected_owner_pid" \
        "$expected_owner_start_token" \
        "$expected_owner_nonce" \
        "$expected_acquired_at" \
        "$expected_updated_at" \
        "$expected_contract_version" \
        "$expected_old_script_sha256" \
        "$current_script_sha256"; then
        die "failed to execute updated healthmes_local.sh; preserving the lifecycle journal"
    fi
}

cmd_update_after_pull() {
    local restart_launch_agent=$1
    [ "$restart_launch_agent" = true ] \
        || [ "$restart_launch_agent" = false ] \
        || die "invalid update restart state"
    bash "$DEV_MAC_SCRIPT" setup
    if [ "$restart_launch_agent" = true ]; then
        set_decision_runtime_lifecycle_phase restarting \
            || die "failed to journal update restart"
        start_launch_agent
    fi
    info "updated"
}

resume_decision_runtime_update_after_pull() {
    [ "$#" -eq 10 ] \
        || die "invalid decision runtime update handoff arguments"
    local restart_launch_agent=$1 expected_version=$2
    local expected_owner_pid=$3 expected_owner_start_token=$4
    local expected_owner_nonce=$5 expected_acquired_at=$6
    local expected_updated_at=$7 expected_contract_version=$8
    local expected_old_script_sha256=$9
    local expected_new_script_sha256=${10}
    local current_script_sha256 expected_generation

    [ "${HEALTHMES_INTERNAL_UPDATE_HANDOFF_DEPTH:-0}" = 1 ] \
        || die "decision runtime update handoff was not started by the update holder"
    [ "$restart_launch_agent" = true ] \
        || [ "$restart_launch_agent" = false ] \
        || die "invalid update restart state"
    [ "$expected_version" = "$DECISION_RUNTIME_LIFECYCLE_RECORD_VERSION" ] \
        || die "decision runtime update handoff record version is incompatible; preserving the lifecycle journal"
    [ "$expected_contract_version" = "$DECISION_RUNTIME_LIFECYCLE_CONTRACT_VERSION" ] \
        || die "decision runtime update handoff contract version is incompatible; preserving the lifecycle journal"
    [[ "$expected_old_script_sha256" =~ ^[0-9a-f]{64}$ ]] \
        && [[ "$expected_new_script_sha256" =~ ^[0-9a-f]{64}$ ]] \
        && [ "$expected_old_script_sha256" != "$expected_new_script_sha256" ] \
        || die "decision runtime update handoff script identities are invalid; preserving the lifecycle journal"
    [ "$expected_owner_pid" = "$$" ] \
        || die "decision runtime update handoff changed process identity; preserving the lifecycle journal"
    capture_native_process_start_token "$$" \
        || die "decision runtime update handoff owner identity is unreadable; preserving the lifecycle journal"
    [ "$NATIVE_PROCESS_START_TOKEN" = "$expected_owner_start_token" ] \
        || die "decision runtime update handoff owner identity changed; preserving the lifecycle journal"
    current_script_sha256="$(current_lifecycle_script_sha256)" \
        || die "updated healthmes_local.sh identity is unreadable; preserving the lifecycle journal"
    [ "$current_script_sha256" = "$expected_new_script_sha256" ] \
        || die "updated healthmes_local.sh changed during handoff; preserving the lifecycle journal"

    load_decision_runtime_lifecycle_lock
    loaded_decision_runtime_update_handoff_matches \
        "$expected_version" \
        "$expected_owner_pid" \
        "$expected_owner_start_token" \
        "$expected_owner_nonce" \
        "$expected_acquired_at" \
        "$expected_updated_at" \
        "$expected_contract_version" \
        "$expected_old_script_sha256" \
        || die "decision runtime update lifecycle generation changed during handoff; preserving the lifecycle journal"
    expected_generation="$(decision_runtime_lifecycle_lock_generation)"

    DECISION_RUNTIME_LIFECYCLE_LOCK_HELD=true
    DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_PID=$expected_owner_pid
    DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_START_TOKEN=$expected_owner_start_token
    DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_NONCE=$expected_owner_nonce
    DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_OPERATION=update
    DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_PHASE=pulling
    DECISION_RUNTIME_LIFECYCLE_INITIAL_SCRIPT_SHA256=$current_script_sha256
    DECISION_RUNTIME_DURABLE_MUTATION_STARTED=true
    DECISION_RUNTIME_POST_RELEASE_RMDIR_RUNTIME=false
    DECISION_RUNTIME_POST_RELEASE_RMDIR_DATA=false
    trap release_decision_runtime_lifecycle_lock_on_exit EXIT
    rewrite_loaded_decision_runtime_lifecycle_script_generation \
        "$expected_generation" setup "$current_script_sha256" \
        || die "failed to hand off the decision runtime update lifecycle journal"
    DECISION_RUNTIME_LIFECYCLE_LOCK_OWNER_PHASE=setup
    unset HEALTHMES_INTERNAL_UPDATE_HANDOFF_DEPTH
    run_owned_decision_runtime_lifecycle_command \
        cmd_update_after_pull "$restart_launch_agent"
}

cmd_update() {
    local restart_launch_agent=false
    local handoff_version handoff_owner_pid handoff_owner_start_token
    local handoff_owner_nonce handoff_acquired_at handoff_updated_at
    local handoff_contract_version handoff_script_sha256
    set_decision_runtime_lifecycle_phase preflight \
        || die "failed to journal update preflight"
    git -C "$REPO_ROOT" diff --quiet || die "working tree has changes; commit or stash first"
    git -C "$REPO_ROOT" diff --cached --quiet || die "index has changes; commit or stash first"
    DECISION_RUNTIME_DURABLE_MUTATION_STARTED=true
    set_decision_runtime_lifecycle_phase stopping \
        || die "failed to journal update shutdown"
    if [ -f "$LAUNCH_AGENT_PLIST" ]; then
        restart_launch_agent=true
        stop_launch_agent
    fi
    load_runtime_env
    stop_decision_runtime
    set_decision_runtime_lifecycle_phase pulling \
        || die "failed to journal update source replacement"
    load_decision_runtime_lifecycle_lock
    loaded_lifecycle_lock_is_owned_by_current_process \
        && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_PHASE" = pulling ] \
        || die "decision runtime update lifecycle journal changed before pull"
    [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_SCRIPT_CONTRACT_VERSION" = "$DECISION_RUNTIME_LIFECYCLE_CONTRACT_VERSION" ] \
        && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_SCRIPT_SHA256" = "$DECISION_RUNTIME_LIFECYCLE_INITIAL_SCRIPT_SHA256" ] \
        || die "decision runtime update lifecycle script generation changed before pull"
    assert_lifecycle_script_generation_unchanged
    handoff_version=$DECISION_RUNTIME_LIFECYCLE_LOCK_VERSION
    handoff_owner_pid=$DECISION_RUNTIME_LIFECYCLE_LOCK_PID
    handoff_owner_start_token=$DECISION_RUNTIME_LIFECYCLE_LOCK_START_TOKEN
    handoff_owner_nonce=$DECISION_RUNTIME_LIFECYCLE_LOCK_NONCE
    handoff_acquired_at=$DECISION_RUNTIME_LIFECYCLE_LOCK_ACQUIRED_AT
    handoff_updated_at=$DECISION_RUNTIME_LIFECYCLE_LOCK_UPDATED_AT
    handoff_contract_version=$DECISION_RUNTIME_LIFECYCLE_LOCK_SCRIPT_CONTRACT_VERSION
    handoff_script_sha256=$DECISION_RUNTIME_LIFECYCLE_LOCK_SCRIPT_SHA256
    git -C "$REPO_ROOT" pull --ff-only
    reexec_updated_local_script_if_needed \
        "$restart_launch_agent" \
        "$handoff_version" \
        "$handoff_owner_pid" \
        "$handoff_owner_start_token" \
        "$handoff_owner_nonce" \
        "$handoff_acquired_at" \
        "$handoff_updated_at" \
        "$handoff_contract_version" \
        "$handoff_script_sha256"
    load_decision_runtime_lifecycle_lock
    loaded_decision_runtime_update_handoff_matches \
        "$handoff_version" \
        "$handoff_owner_pid" \
        "$handoff_owner_start_token" \
        "$handoff_owner_nonce" \
        "$handoff_acquired_at" \
        "$handoff_updated_at" \
        "$handoff_contract_version" \
        "$handoff_script_sha256" \
        || die "decision runtime update lifecycle generation changed during pull"
    set_decision_runtime_lifecycle_phase setup \
        || die "failed to journal update setup"
    cmd_update_after_pull "$restart_launch_agent"
}

start_apps() {
    local decision_home= quoted_budget= quoted_home= quoted_python=
    local quoted_vendor=
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
        [ -x "$RUNTIME_PYTHON_BIN" ] \
            || die "HealthMes runtime Python is unavailable: $RUNTIME_PYTHON_BIN"
        printf -v quoted_budget '%q' "$HERMES_DECISION_STOP_BUDGET"
        printf -v quoted_home '%q' "$decision_home"
        printf -v quoted_python '%q' "$RUNTIME_PYTHON_BIN"
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
            "exec env HERMES_HOME=$quoted_home $quoted_python -m healthmes.hermes_runtime_supervisor --hermes-home $quoted_home --vendor-root $quoted_vendor --shutdown-budget-path $quoted_budget" \
            "$HERMES_DECISION_STARTUP_LEASE"
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
    trap 'with_decision_runtime_lifecycle_lock stop stop_apps; exit 0' INT TERM
    with_decision_runtime_lifecycle_lock start start_apps
    while true; do
        "$SLEEP_BIN" 5
        load_runtime_env
        if ! pid_running "$HEALTHMES_PID" \
            || ! pid_running "$OW_PID" \
            || ! pid_running "$WORKER_PID" \
            || ! pid_running "$BEAT_PID" \
            || { decision_runtime_configured \
                && ! pid_running "$HERMES_DECISION_PID"; }; then
            with_decision_runtime_lifecycle_lock start start_apps
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
    local name=$1 file=$2 status
    if pid_running "$file"; then
        info "$name: running (pid $PROCESS_PID)"
    else
        status=$?
        case "$status" in
        3 | 4) info "$name: stopped" ;;
        *) info "$name: unknown (process identity is unprovable)" ;;
        esac
    fi
}

decision_runtime_status() {
    local probe_status lock_owner_status
    local lock_status lock_generation= lock_operation=
    local lock_pid= lock_start_token= final_lock_status final_lock_generation=
    local startup_lease_status startup_lease_version
    local startup_lease_pid startup_lease_start_token
    local startup_lease_service_nonce startup_lease_generation=
    local metadata_present=false metadata_valid=false
    local metadata_pid= metadata_start_time= metadata_nonce=
    local stored_pid=

    load_decision_runtime_lifecycle_lock
    lock_status=$DECISION_RUNTIME_LIFECYCLE_LOCK_STATUS
    if [ "$lock_status" = valid ]; then
        lock_generation="$(decision_runtime_lifecycle_lock_generation)"
        lock_operation=$DECISION_RUNTIME_LIFECYCLE_LOCK_OPERATION
        lock_pid=$DECISION_RUNTIME_LIFECYCLE_LOCK_PID
        lock_start_token=$DECISION_RUNTIME_LIFECYCLE_LOCK_START_TOKEN
        if process_start_token_status "$lock_pid" "$lock_start_token"; then
            lock_owner_status=live
        else
            probe_status=$?
            case "$probe_status" in
            3) lock_owner_status=absent ;;
            *) lock_owner_status=unknown ;;
            esac
        fi
    fi

    load_decision_runtime_startup_lease
    startup_lease_status=$DECISION_RUNTIME_STARTUP_LEASE_STATUS
    startup_lease_version=$DECISION_RUNTIME_STARTUP_LEASE_VERSION
    startup_lease_pid=$DECISION_RUNTIME_STARTUP_LEASE_PID
    startup_lease_start_token=$DECISION_RUNTIME_STARTUP_LEASE_START_TOKEN
    startup_lease_service_nonce=$DECISION_RUNTIME_STARTUP_LEASE_SERVICE_NONCE
    if [ "$startup_lease_status" != missing ] \
        && [ "$startup_lease_status" != invalid ]; then
        startup_lease_generation="$(decision_runtime_startup_lease_generation)"
    fi

    if [ -f "$HERMES_DECISION_PID" ] \
        || [ -f "$(identity_file "$HERMES_DECISION_PID")" ]; then
        metadata_present=true
        if load_process_identity "$HERMES_DECISION_PID"; then
            metadata_valid=true
            metadata_pid=$PROCESS_PID
            metadata_start_time=$PROCESS_START_TIME
            metadata_nonce=$PROCESS_NONCE
        elif [ -f "$HERMES_DECISION_PID" ] \
            && [ ! -L "$HERMES_DECISION_PID" ] \
            && [ ! -f "$(identity_file "$HERMES_DECISION_PID")" ]; then
            stored_pid="$(<"$HERMES_DECISION_PID")"
        fi
    fi

    load_decision_runtime_stop_bounds

    load_decision_runtime_lifecycle_lock
    final_lock_status=$DECISION_RUNTIME_LIFECYCLE_LOCK_STATUS
    if [ "$final_lock_status" = valid ]; then
        final_lock_generation="$(decision_runtime_lifecycle_lock_generation)"
    fi

    if [ "$lock_status" != "$final_lock_status" ] \
        || { [ "$lock_status" = valid ] \
            && [ "$lock_generation" != "$final_lock_generation" ]; }; then
        info "Hermes decision runtime: unknown (lifecycle lock changed while status was sampled)"
        return
    fi
    if [ "$lock_status" = invalid ]; then
        info "Hermes decision runtime: unknown (lifecycle lock is malformed or has no provable owner)"
        return
    fi
    if [ "$startup_lease_status" = invalid ]; then
        info "Hermes decision runtime: unknown (startup lease is malformed or unsafe)"
        return
    fi
    if [ "$DECISION_RUNTIME_BUDGET_STATUS" = invalid ]; then
        info "Hermes decision runtime: unknown (shutdown budget is malformed or unsafe)"
        return
    fi
    if [ "$lock_status" = valid ] \
        && { [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_PHASE" = repair_required ] \
            || { [ "$lock_owner_status" = absent ] \
                && operation_requires_durable_lifecycle_journal \
                    "$lock_operation" \
                && [ "$DECISION_RUNTIME_LIFECYCLE_LOCK_PHASE" != complete ]; }; }; then
        info "Hermes decision runtime: unknown (lifecycle ${lock_operation} transaction requires explicit repair)"
        return
    fi

    if [ "$DECISION_RUNTIME_BUDGET_STATUS" = v3 ]; then
        if [ "$startup_lease_status" != missing ]; then
            if ! decision_runtime_startup_lease_matches_launcher \
                    "$DECISION_RUNTIME_BUDGET_LAUNCHER_PID" \
                    "$DECISION_RUNTIME_BUDGET_LAUNCHER_SERVICE_NONCE" \
                    "$startup_lease_status" \
                    "$startup_lease_pid" \
                    "$startup_lease_service_nonce" \
                || { [ "$startup_lease_status" = identity_verified ] \
                    && [ "$startup_lease_start_token" != "$DECISION_RUNTIME_BUDGET_LAUNCHER_START_TOKEN" ]; }; then
                info "Hermes decision runtime: unknown (startup lease and shutdown budget generations do not match)"
                return
            fi
        fi
        if [ "$metadata_valid" = true ] \
            && { [ "$metadata_pid" != "$DECISION_RUNTIME_BUDGET_LAUNCHER_PID" ] \
                || [ "ps:$metadata_start_time" != "$DECISION_RUNTIME_BUDGET_LAUNCHER_START_TOKEN" ] \
                || [ "$metadata_nonce" != "$DECISION_RUNTIME_BUDGET_LAUNCHER_SERVICE_NONCE" ]; }; then
            info "Hermes decision runtime: unknown (launcher metadata and shutdown budget generations do not match)"
            return
        fi
        if [ "$metadata_valid" = false ] \
            && [ -n "$stored_pid" ] \
            && { ! valid_managed_pid "$stored_pid" \
                || [ "$stored_pid" != "$DECISION_RUNTIME_BUDGET_LAUNCHER_PID" ]; }; then
            info "Hermes decision runtime: unknown (launcher PID tombstone and shutdown budget generations do not match)"
            return
        fi
    fi

    if [ "$startup_lease_status" != missing ] \
        && [ "$metadata_present" = true ]; then
        if [ "$metadata_valid" = true ]; then
            if ! decision_runtime_startup_lease_matches_launcher \
                "$metadata_pid" \
                "$metadata_nonce" \
                "$startup_lease_status" \
                "$startup_lease_pid" \
                "$startup_lease_service_nonce" \
                || { [ "$startup_lease_status" = identity_verified ] \
                    && [ "$startup_lease_start_token" != "ps:$metadata_start_time" ]; } \
                || { [ "$startup_lease_version" = 2 ] \
                    && [ "$startup_lease_status" = intent ]; }; then
                info "Hermes decision runtime: unknown (startup lease and launcher metadata generations do not match)"
                return
            fi
        elif [ -n "$stored_pid" ]; then
            if [ "$startup_lease_status" != spawned ] \
                && [ "$startup_lease_status" != failed ]; then
                info "Hermes decision runtime: unknown (PID tombstone is inconsistent with startup phase)"
                return
            fi
            if ! valid_managed_pid "$stored_pid" \
                || [ "$stored_pid" != "$startup_lease_pid" ]; then
                info "Hermes decision runtime: unknown (startup lease and PID tombstone generations do not match)"
                return
            fi
        else
            info "Hermes decision runtime: unknown (launcher metadata is malformed)"
            return
        fi
    fi

    if [ "$lock_status" = valid ]; then
        case "$lock_owner_status" in
        absent)
            info "Hermes decision runtime: unknown (orphaned lifecycle ${lock_operation} lock requires recovery)"
            return
            ;;
        unknown)
            info "Hermes decision runtime: unknown (lifecycle lock owner identity is unprovable)"
            return
            ;;
        live)
            case "$lock_operation" in
            start)
                info "Hermes decision runtime: starting (cross-process lifecycle start is in progress)"
                ;;
            stop)
                info "Hermes decision runtime: stopping (cross-process lifecycle stop is in progress)"
                ;;
            update | install | uninstall)
                info "Hermes decision runtime: unknown (lifecycle ${lock_operation} is in progress)"
                ;;
            esac
            return
            ;;
        esac
    fi

    if [ "$DECISION_RUNTIME_BUDGET_STATUS" = v3 ]; then
        if runtime_process_identity_action probe; then
            if [ "$metadata_valid" = true ]; then
                info "Hermes decision runtime: running (launcher pid $metadata_pid; verified supervisor pid $DECISION_RUNTIME_SUPERVISOR_PID)"
            else
                info "Hermes decision runtime: running (verified supervisor pid $DECISION_RUNTIME_SUPERVISOR_PID; wrapper metadata unavailable)"
            fi
            return
        else
            probe_status=$?
        fi
        case "$probe_status" in
        3) info "Hermes decision runtime: stopped with incomplete cleanup record" ;;
        4) info "Hermes decision runtime: unknown (supervisor PID was reused)" ;;
        *) info "Hermes decision runtime: unknown (supervisor identity is unprovable)" ;;
        esac
        return
    fi

    if [ "$DECISION_RUNTIME_BUDGET_STATUS" = legacy ]; then
        if [ "$metadata_valid" = true ] \
            && process_identity_matches "$HERMES_DECISION_PID"; then
            info "Hermes decision runtime: running (legacy launcher pid $PROCESS_PID)"
        else
            info "Hermes decision runtime: unknown (legacy shutdown budget cannot prove a live launcher)"
        fi
        return
    fi

    if [ "$DECISION_RUNTIME_BUDGET_STATUS" = missing ]; then
        if [ "$startup_lease_status" = pending ] \
            || [ "$startup_lease_status" = intent ]; then
            info "Hermes decision runtime: starting (startup intent is published; launcher identity is not yet verified)"
        elif [ "$startup_lease_status" = spawned ] \
            || [ "$startup_lease_status" = failed ]; then
            info "Hermes decision runtime: unknown (startup launcher identity is unverified; PID tombstone and lease are preserved)"
        elif [ "$startup_lease_status" = identity_verified ]; then
            info "Hermes decision runtime: unknown (verified startup lease remains without a shutdown budget)"
        elif [ "$startup_lease_status" != missing ]; then
            info "Hermes decision runtime: unknown (startup lease is malformed or unsafe)"
        elif [ "$metadata_valid" = true ] \
            && process_identity_matches "$HERMES_DECISION_PID"; then
            info "Hermes decision runtime: running (launcher pid $PROCESS_PID; shutdown budget not yet published)"
        elif [ "$metadata_present" = true ]; then
            info "Hermes decision runtime: unknown (launcher metadata remains without a v3 shutdown record)"
        else
            info "Hermes decision runtime: stopped"
        fi
    else
        info "Hermes decision runtime: unknown (shutdown budget is not a usable v3 record)"
    fi
}

cmd_status() {
    decision_runtime_status
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

remove_runtime_contents_except_lifecycle_lock() {
    [ "$RUNTIME_DIR" = "$REPO_ROOT/data/runtime" ] \
        || die "refusing unexpected runtime path"
    (
        local entry
        shopt -s dotglob nullglob
        for entry in "$RUNTIME_DIR"/*; do
            [ "$entry" = "$HERMES_DECISION_LIFECYCLE_LOCK" ] \
                || rm -rf -- "$entry"
        done
    )
}

remove_data_contents_except_runtime() {
    [ "$DATA_DIR" = "$REPO_ROOT/data" ] \
        || die "refusing unexpected data path"
    (
        local entry
        shopt -s dotglob nullglob
        for entry in "$DATA_DIR"/*; do
            [ "$entry" = "$RUNTIME_DIR" ] \
                || [ "$entry" = "$HERMES_DECISION_TRANSITION_LOCK" ] \
                || rm -rf -- "$entry"
        done
    )
}

cmd_uninstall() {
    local delete_data=${1:-}
    [ -z "$delete_data" ] || [ "$delete_data" = "--delete-data" ] \
        || die "usage: $0 uninstall [--delete-data]"
    set_decision_runtime_lifecycle_phase preflight \
        || die "failed to journal uninstall preflight"
    DECISION_RUNTIME_DURABLE_MUTATION_STARTED=true
    set_decision_runtime_lifecycle_phase unloading \
        || die "failed to journal launch-agent removal"
    uninstall_launch_agent
    set_decision_runtime_lifecycle_phase stopping \
        || die "failed to journal uninstall runtime shutdown"
    stop_apps
    set_decision_runtime_lifecycle_phase services_stop \
        || die "failed to journal uninstall service shutdown"
    bash "$DEV_MAC_SCRIPT" services-stop
    set_decision_runtime_lifecycle_phase cleanup \
        || die "failed to journal uninstall data cleanup"
    remove_runtime_contents_except_lifecycle_lock
    DECISION_RUNTIME_POST_RELEASE_RMDIR_RUNTIME=true
    if [ "$delete_data" = "--delete-data" ]; then
        remove_data_contents_except_runtime
        DECISION_RUNTIME_POST_RELEASE_RMDIR_DATA=true
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
install) with_decision_runtime_lifecycle_lock install cmd_install ;;
update) with_decision_runtime_lifecycle_lock update cmd_update ;;
start) with_decision_runtime_lifecycle_lock start cmd_start ;;
stop) with_decision_runtime_lifecycle_lock stop cmd_stop ;;
status) cmd_status ;;
open) cmd_open ;;
daemon) cmd_daemon ;;
uninstall) with_decision_runtime_lifecycle_lock uninstall cmd_uninstall "${2:-}" ;;
__service_runner) run_service_runner "${2:-}" "${3:-}" ;;
__resume_update_after_pull)
    [ "$#" -eq 11 ] \
        || die "invalid decision runtime update handoff invocation"
    resume_decision_runtime_update_after_pull \
        "$2" "$3" "$4" "$5" "$6" "$7" "$8" "$9" "${10}" "${11}"
    ;;
*) usage ;;
esac
