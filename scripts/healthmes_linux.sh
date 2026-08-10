#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_NAME="healthmes-compose.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$UNIT_DIR/$UNIT_NAME"
UNIT_TEMPLATE="$REPO_ROOT/config/systemd/$UNIT_NAME.in"
DOCKER_BIN="${HEALTHMES_DOCKER_BIN:-$(command -v docker || true)}"
SYSTEMCTL_BIN="${HEALTHMES_SYSTEMCTL_BIN:-$(command -v systemctl || true)}"
COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.linux.yml)
DOCKER_READY_ATTEMPTS="${HEALTHMES_DOCKER_READY_ATTEMPTS:-30}"
DOCKER_READY_DELAY_SECONDS="${HEALTHMES_DOCKER_READY_DELAY_SECONDS:-2}"

die() { printf '[healthmes] %s\n' "$*" >&2; exit 1; }
info() { printf '[healthmes] %s\n' "$*"; }

require_tools() {
    [ -n "$DOCKER_BIN" ] || die "docker is required"
    "$DOCKER_BIN" compose version >/dev/null 2>&1 \
        || die "docker compose v2 is required"
    [ -n "$SYSTEMCTL_BIN" ] || die "systemd is required"
}

wait_for_docker() {
    [ -n "$DOCKER_BIN" ] || die "docker is required"
    attempt=1
    while [ "$attempt" -le "$DOCKER_READY_ATTEMPTS" ]; do
        if "$DOCKER_BIN" info >/dev/null 2>&1; then
            return 0
        fi
        sleep "$DOCKER_READY_DELAY_SECONDS"
        attempt=$((attempt + 1))
    done
    die "docker did not become ready after $DOCKER_READY_ATTEMPTS attempts"
}

install_unit() {
    require_tools
    [ -f "$UNIT_TEMPLATE" ] || die "missing systemd unit template"
    mkdir -p "$UNIT_DIR"
    escaped_repo="${REPO_ROOT//&/\\&}"
    escaped_docker="${DOCKER_BIN//&/\\&}"
    escaped_script="${BASH_SOURCE[0]//&/\\&}"
    temporary="$(mktemp)"
    sed \
        -e "s|__REPO_ROOT__|$escaped_repo|g" \
        -e "s|__DOCKER__|$escaped_docker|g" \
        -e "s|__LINUX_SCRIPT__|$escaped_script|g" \
        "$UNIT_TEMPLATE" >"$temporary"
    install -m 644 "$temporary" "$UNIT_PATH"
    rm -f "$temporary"
    "$SYSTEMCTL_BIN" --user daemon-reload
    "$SYSTEMCTL_BIN" --user enable --now "$UNIT_NAME"
    info "installed $UNIT_NAME"
    if command -v loginctl >/dev/null 2>&1 \
        && ! loginctl show-user "$USER" -p Linger --value 2>/dev/null \
            | grep -qx yes; then
        info "action required: run 'sudo loginctl enable-linger $USER' for boot-before-login"
    fi
}

case "${1:-}" in
wait-for-docker)
    wait_for_docker
    ;;
install)
    install_unit
    ;;
update)
    require_tools
    "$DOCKER_BIN" compose "${COMPOSE_FILES[@]}" pull
    "$DOCKER_BIN" compose "${COMPOSE_FILES[@]}" build --pull
    "$SYSTEMCTL_BIN" --user restart "$UNIT_NAME"
    ;;
start)
    require_tools
    "$SYSTEMCTL_BIN" --user start "$UNIT_NAME"
    ;;
stop)
    require_tools
    "$SYSTEMCTL_BIN" --user stop "$UNIT_NAME"
    ;;
status)
    require_tools
    "$SYSTEMCTL_BIN" --user --no-pager status "$UNIT_NAME"
    "$DOCKER_BIN" compose "${COMPOSE_FILES[@]}" ps
    ;;
uninstall)
    require_tools
    "$SYSTEMCTL_BIN" --user disable --now "$UNIT_NAME" 2>/dev/null || true
    rm -f "$UNIT_PATH"
    "$SYSTEMCTL_BIN" --user daemon-reload
    info "runtime removed; Docker volumes and personal data were kept"
    ;;
*)
    die "usage: $0 wait-for-docker|install|update|start|stop|status|uninstall"
    ;;
esac
