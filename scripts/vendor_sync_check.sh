#!/usr/bin/env bash
# vendor_sync_check.sh — read-only vendor drift report (upstream sync dry run).
#
# docs/PLAN.md §10 Phase 3 / §11: vendor/ holds read-only upstream snapshots;
# before replacing one wholesale, run this drill against a fresh upstream
# checkout to see exactly what a sync would change. The script NEVER writes —
# it only diffs and prints a report.
#
# Usage:
#   scripts/vendor_sync_check.sh <vendor-name> <upstream-checkout-path>
#   scripts/vendor_sync_check.sh --list
#
#   <vendor-name>            directory under vendor/ (e.g. open-wearables)
#   <upstream-checkout-path> local clone of the matching upstream repository
#
# Example:
#   git clone --depth 1 https://github.com/<upstream>/open-wearables /tmp/ow-upstream
#   scripts/vendor_sync_check.sh open-wearables /tmp/ow-upstream
#
# Exit codes: 0 = in sync, 1 = drift found, 2 = usage/input error.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR_ROOT="$REPO_ROOT/vendor"

# VCS internals and generated/derived artifacts: not part of the sync surface.
EXCLUDES=(
    ".git"
    ".DS_Store"
    "__pycache__"
    "*.pyc"
    ".venv"
    "venv"
    "node_modules"
    ".pytest_cache"
    ".ruff_cache"
    ".mypy_cache"
    "dist"
    "build"
    "*.egg-info"
)

usage() {
    sed -n '2,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 2
}

list_vendors() {
    echo "Vendored upstreams under vendor/:"
    for dir in "$VENDOR_ROOT"/*/; do
        [ -d "$dir" ] && echo "  $(basename "$dir")"
    done
    exit 0
}

case "${1:-}" in
"" | -h | --help) usage ;;
--list) list_vendors ;;
esac

[ $# -eq 2 ] || usage

VENDOR_NAME="$1"
UPSTREAM_PATH="$2"
VENDOR_DIR="$VENDOR_ROOT/$VENDOR_NAME"

if [ ! -d "$VENDOR_DIR" ]; then
    echo "error: no such vendored tree: vendor/$VENDOR_NAME" >&2
    echo "       (run with --list to see the available names)" >&2
    exit 2
fi
if [ ! -d "$UPSTREAM_PATH" ]; then
    echo "error: upstream checkout not found: $UPSTREAM_PATH" >&2
    exit 2
fi
UPSTREAM_PATH="$(cd "$UPSTREAM_PATH" && pwd)"

# Header: identify both sides as precisely as possible.
echo "== vendor sync drill (dry run, read-only) =="
echo "vendored : vendor/$VENDOR_NAME"
echo "upstream : $UPSTREAM_PATH"
if git -C "$UPSTREAM_PATH" rev-parse --git-dir >/dev/null 2>&1; then
    upstream_rev="$(git -C "$UPSTREAM_PATH" rev-parse --short HEAD 2>/dev/null || true)"
    upstream_branch="$(git -C "$UPSTREAM_PATH" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    echo "upstream rev : ${upstream_rev:-unknown} (${upstream_branch:-detached})"
fi
echo

exclude_args=()
for pattern in "${EXCLUDES[@]}"; do
    exclude_args+=(-x "$pattern")
done

# diff -rq exits 1 on differences; capture output without tripping set -e.
set +e
raw_diff="$(LC_ALL=C diff -rq "${exclude_args[@]}" "$VENDOR_DIR" "$UPSTREAM_PATH" 2>&1)"
diff_status=$?
set -e
if [ "$diff_status" -gt 1 ]; then
    echo "error: diff failed:" >&2
    echo "$raw_diff" >&2
    exit 2
fi

# Classify: local-only (would be deleted by a sync), upstream-only (would be
# added), changed (would be replaced). Paths are printed relative to each root.
local_only="$(printf '%s\n' "$raw_diff" | sed -n "s|^Only in $VENDOR_DIR\(.*\): \(.*\)|.\1/\2|p" | sed 's|^\./||')"
upstream_only="$(printf '%s\n' "$raw_diff" | sed -n "s|^Only in $UPSTREAM_PATH\(.*\): \(.*\)|.\1/\2|p" | sed 's|^\./||')"
changed="$(printf '%s\n' "$raw_diff" | sed -n "s|^Files $VENDOR_DIR/\(.*\) and .* differ$|\1|p")"

count_lines() { [ -n "$1" ] && printf '%s\n' "$1" | wc -l | tr -d ' ' || echo 0; }
n_local="$(count_lines "$local_only")"
n_upstream="$(count_lines "$upstream_only")"
n_changed="$(count_lines "$changed")"

print_section() {
    local title="$1" body="$2" count="$3"
    echo "-- $title ($count)"
    if [ "$count" -eq 0 ]; then
        echo "   (none)"
    else
        printf '%s\n' "$body" | sed 's/^/   /'
    fi
    echo
}

print_section "changed files (sync would replace)" "$changed" "$n_changed"
print_section "only in vendor/$VENDOR_NAME (sync would delete)" "$local_only" "$n_local"
print_section "only upstream (sync would add)" "$upstream_only" "$n_upstream"

total=$((n_changed + n_local + n_upstream))
if [ "$total" -eq 0 ]; then
    echo "RESULT: vendor/$VENDOR_NAME is in sync with the upstream checkout."
    exit 0
fi

echo "RESULT: drift detected — $total path(s) differ ($n_changed changed, $n_local local-only, $n_upstream upstream-only)."
echo
echo "Next steps (see docs/DEVELOPMENT.md, 'Vendor upstream sync drill'):"
echo "  1. Review the changed list against the coupling surface (docs/PLAN.md §11):"
echo "     open-wearables REST v1 + MCP tool names; hermes config/skill/cron/webhook contracts."
echo "  2. To sync: replace vendor/$VENDOR_NAME wholesale in a dedicated commit"
echo "     (never hand-edit files under vendor/)."
echo "  3. Re-run the offline suite + compose validation: uv run pytest -q && docker compose config -q"
exit 1
