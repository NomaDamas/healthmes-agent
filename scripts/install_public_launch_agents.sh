#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/HealthMes"
PYTHON="$REPO_ROOT/.venv/bin/python"
CLOUDFLARED="$(realpath "$(command -v cloudflared)")"
CLOUDFLARE_CONFIG_DIR="$HOME/.cloudflared"
CLOUDFLARE_CONFIG="$CLOUDFLARE_CONFIG_DIR/config.yml"
CLOUDFLARE_CONFIG_TEMPLATE="$REPO_ROOT/config/cloudflared.healthmes-agent.yml.in"

settings_python() {
    (
        cd "$REPO_ROOT"
        env -i \
            HOME="$HOME" \
            PATH="/usr/bin:/bin" \
            "$PYTHON" "$@"
    )
}

if ! (
    settings_python -c \
        'from healthmes.config import Settings; raise SystemExit(not bool(Settings().api_token.get_secret_value().strip()))'
); then
    printf '[healthmes] HEALTHMES_API_TOKEN is required for public hosting\n' >&2
    exit 1
fi
HEALTHMES_PORT="$(
    settings_python -c 'from healthmes.config import Settings; print(Settings().port)'
)"

verify_authenticated_server() {
    settings_python - "$HEALTHMES_PORT" <<'PY'
import sys

import httpx

from healthmes.config import Settings

port = int(sys.argv[1])
token = Settings().api_token.get_secret_value().strip()
base_url = f"http://127.0.0.1:{port}"
try:
    with httpx.Client(base_url=base_url, timeout=2, trust_env=False) as client:
        health = client.get("/health")
        anonymous = client.get("/v1/alerts", params={"limit": 1})
        authenticated = client.get(
            "/v1/alerts",
            params={"limit": 1},
            headers={"Authorization": f"Bearer {token}"},
        )
except httpx.HTTPError:
    raise SystemExit(1)
if health.status_code != 200:
    raise SystemExit(1)
if anonymous.status_code != 401:
    raise SystemExit(1)
if authenticated.status_code != 200:
    raise SystemExit(1)
PY
}

install_cloudflare_config() {
    local credentials temp
    credentials="$CLOUDFLARE_CONFIG_DIR/62003c8c-a4a3-44d1-81fc-4d4118ef7271.json"
    [ -f "$credentials" ] || {
        printf '[healthmes] missing Cloudflare tunnel credentials: %s\n' \
            "$credentials" >&2
        exit 1
    }
    [ -f "$CLOUDFLARE_CONFIG_TEMPLATE" ] || {
        printf '[healthmes] missing Cloudflare config template\n' >&2
        exit 1
    }
    mkdir -p "$CLOUDFLARE_CONFIG_DIR"
    temp="$(mktemp "$CLOUDFLARE_CONFIG_DIR/.config.yml.XXXXXX")"
    "$PYTHON" - "$CLOUDFLARE_CONFIG_TEMPLATE" "$temp" \
        "$HOME" "$HEALTHMES_PORT" <<'PY'
import sys
from pathlib import Path

template, destination, home, port = sys.argv[1:]
text = Path(template).read_text(encoding="utf-8")
text = text.replace("__HOME__", home).replace("__HEALTHMES_PORT__", port)
Path(destination).write_text(text, encoding="utf-8")
PY
    chmod 600 "$temp"
    mv "$temp" "$CLOUDFLARE_CONFIG"
    printf '[healthmes] configured Cloudflare ingress on port %s\n' \
        "$HEALTHMES_PORT"
}

install_agent() {
    local label=$1 template=$2 destination temp bootstrap_log bootstrapped
    destination="$LAUNCH_AGENT_DIR/$label.plist"
    temp="$(mktemp)"
    sed \
        -e "s|__REPO_ROOT__|$REPO_ROOT|g" \
        -e "s|__HOME__|$HOME|g" \
        -e "s|__PYTHON__|$PYTHON|g" \
        -e "s|__CLOUDFLARED__|$CLOUDFLARED|g" \
        "$template" > "$temp"
    plutil -lint "$temp" >/dev/null
    install -m 644 "$temp" "$destination"
    rm -f "$temp"
    launchctl bootout "gui/$UID/$label" >/dev/null 2>&1 || true
    bootstrap_log="$(mktemp)"
    bootstrapped=false
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if launchctl bootstrap "gui/$UID" "$destination" 2>"$bootstrap_log"; then
            bootstrapped=true
            break
        fi
        sleep 0.5
    done
    if [ "$bootstrapped" != true ]; then
        cat "$bootstrap_log" >&2
        rm -f "$bootstrap_log"
        return 1
    fi
    rm -f "$bootstrap_log"
    launchctl kickstart -k "gui/$UID/$label"
    printf '[healthmes] installed %s\n' "$label"
}

remove_agent() {
    local label=$1 destination
    destination="$LAUNCH_AGENT_DIR/$label.plist"
    launchctl bootout "gui/$UID/$label" >/dev/null 2>&1 || true
    rm -f "$destination"
}

healthmes_server_is_managed_elsewhere() {
    if launchctl print "gui/$UID/com.healthmes.local" >/dev/null 2>&1; then
        if ! verify_authenticated_server; then
            printf '[healthmes] com.healthmes.local is not the authenticated HealthMes server on port %s\n' \
                "$HEALTHMES_PORT" >&2
            exit 1
        fi
        printf '[healthmes] reusing com.healthmes.local on port %s\n' "$HEALTHMES_PORT"
        return 0
    fi
    if launchctl print "gui/$UID/com.healthmes.agent.public" >/dev/null 2>&1; then
        return 1
    fi
    if /usr/bin/curl --fail --silent --max-time 2 \
        "http://127.0.0.1:${HEALTHMES_PORT}/health" >/dev/null; then
        if verify_authenticated_server; then
            printf '[healthmes] reusing the authenticated HealthMes server on port %s\n' \
                "$HEALTHMES_PORT"
            return 0
        fi
        printf '[healthmes] refusing unauthenticated or mismatched server on port %s\n' \
            "$HEALTHMES_PORT" >&2
        exit 1
    fi
    return 1
}

wait_for_authenticated_server() {
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
        verify_authenticated_server && return 0
        sleep 0.5
    done
    return 1
}

mkdir -p "$LAUNCH_AGENT_DIR" "$LOG_DIR"
remove_agent "com.healthmes.agent.tunnel"
install_cloudflare_config
if healthmes_server_is_managed_elsewhere; then
    remove_agent "com.healthmes.agent.public"
else
    install_agent \
        "com.healthmes.agent.public" \
        "$REPO_ROOT/config/com.healthmes.agent.public.plist.in"
fi
if ! wait_for_authenticated_server; then
    remove_agent "com.healthmes.agent.public"
    printf '[healthmes] authenticated HealthMes server did not become ready on port %s\n' \
        "$HEALTHMES_PORT" >&2
    exit 1
fi
install_agent \
    "com.healthmes.agent.tunnel" \
    "$REPO_ROOT/config/com.healthmes.agent.tunnel.plist.in"
