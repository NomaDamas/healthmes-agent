#!/usr/bin/env python3
"""Compact HealthMes state snapshot for the Hermes cron briefings.

docs/PLAN.md section 4 (time-driven briefings): each briefing job registers
this file via the ``script:`` field of vendor/hermes-agent/cron/jobs.py::
create_job. Before the agent runs, the scheduler executes the script and
injects its stdout into the prompt as a "## Script Output" context block
(vendor/hermes-agent/cron/scheduler.py::_run_job_script + the injection in
the job runner), saving the agent one MCP round-trip per fact it would
otherwise have to fetch. The agent still verifies through the MCP tools;
this is pre-fetched context, not a data-access bypass.

Execution environment — why this file is deliberately boring:

- The vendor scheduler runs it with *its own* interpreter (``sys.executable``)
  under a sanitized environment, so the healthmes venv is NOT importable:
  standard library only.
- Relative ``script:`` paths resolve under ``$HERMES_HOME/scripts/`` and must
  stay inside that directory (path-traversal guard in ``_run_job_script``),
  so scripts/bootstrap.py installs a copy of this file there.
- Empty stdout makes the scheduler skip the run entirely and a non-zero exit
  turns the briefing into an error report, so this script ALWAYS prints one
  line of JSON and exits 0 — an unreachable service degrades to
  ``{"status": "unavailable", ...}`` and the agent falls back to MCP tools.

Base-URL resolution (localhost-native default; the docker in-cluster value is
injected by bootstrap through the sidecar, never hardcoded here):

1. ``HEALTHMES_BASE_URL`` environment variable
2. ``base_url`` key of ``healthmes_snapshot.json`` next to this file
   (written by scripts/bootstrap.py from the same context that renders
   the hermes config template)
3. ``http://localhost:8100``

API-token resolution mirrors it (``HEALTHMES_API_TOKEN`` env var, then the
sidecar's ``api_token``): when the healthmes surface is bearer-protected the
requests carry ``Authorization: Bearer <token>`` — without it every section
would 401 and the snapshot would degrade to unavailable.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://localhost:8100"
SIDECAR_NAME = "healthmes_snapshot.json"
REQUEST_TIMEOUT_SECONDS = 5.0

# Caps keep the injected context block small (it rides inside the briefing
# prompt); the agent pulls anything deeper through the MCP tools.
MAX_TASKS = 20
MAX_EVENTS = 20
MAX_PROPOSALS = 10

_OPEN_TASK_STATUSES = ("todo", "scheduled", "in_progress")

FetchFn = Callable[[str], Any]


def _read_sidecar(script_path: Path) -> dict[str, Any]:
    sidecar = script_path.resolve().parent / SIDECAR_NAME
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def resolve_base_url(script_path: Path, env: Mapping[str, str]) -> str:
    """Resolve the HealthMes base URL: env var > sidecar file > localhost."""
    from_env = str(env.get("HEALTHMES_BASE_URL", "")).strip()
    if from_env:
        return from_env.rstrip("/")
    configured = str(_read_sidecar(script_path).get("base_url", "")).strip()
    if configured:
        return configured.rstrip("/")
    return DEFAULT_BASE_URL


def resolve_api_token(script_path: Path, env: Mapping[str, str]) -> str:
    """Resolve the bearer token: env var > sidecar file > '' (auth disabled)."""
    from_env = str(env.get("HEALTHMES_API_TOKEN", "")).strip()
    if from_env:
        return from_env
    return str(_read_sidecar(script_path).get("api_token", "")).strip()


def build_fetch(api_token: str = "") -> FetchFn:
    """Default fetcher; sends the bearer header when a token is configured."""

    def _fetch(url: str) -> Any:
        # GET *url* and parse the JSON body (proxies bypassed: loopback calls).
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        headers = {"Accept": "application/json"}
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        request = urllib.request.Request(url, headers=headers)
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))

    return _fetch


def _drop_none(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if value is not None}


def _page_items(payload: Any) -> tuple[list[dict[str, Any]], int]:
    """Unpack the healthmes list envelope ({"data": [...], "pagination": ...})."""
    if not isinstance(payload, dict):
        return [], 0
    data = payload.get("data")
    items = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
    pagination = payload.get("pagination")
    total = len(items)
    if isinstance(pagination, dict) and isinstance(pagination.get("total_count"), int):
        total = pagination["total_count"]
    return items, total


def _open_tasks_section(fetch: FetchFn, base_url: str) -> dict[str, Any]:
    """Open (not done/cancelled) tasks, deadline-first, capped."""
    query = urllib.parse.urlencode({"limit": 200})
    items, _total = _page_items(fetch(f"{base_url}/v1/tasks?{query}"))
    open_items = [item for item in items if item.get("status") in _OPEN_TASK_STATUSES]
    compact = [
        _drop_none(
            {
                "title": item.get("title"),
                "status": item.get("status"),
                "deadline": item.get("deadline"),
                "energy_demand": item.get("energy_demand"),
                "est_minutes": item.get("est_minutes"),
            }
        )
        for item in open_items[:MAX_TASKS]
    ]
    return {"total": len(open_items), "items": compact}


def _events_section(
    fetch: FetchFn, base_url: str, start: datetime, end: datetime
) -> dict[str, Any]:
    """Mirrored calendar events overlapping [start, end), capped."""
    query = urllib.parse.urlencode(
        {"start": start.isoformat(), "end": end.isoformat(), "limit": MAX_EVENTS}
    )
    items, total = _page_items(fetch(f"{base_url}/v1/schedule/events?{query}"))
    compact = [
        _drop_none(
            {
                "summary": item.get("summary"),
                "start_at": item.get("start_at"),
                "end_at": item.get("end_at"),
                "calendar_source": item.get("calendar_source"),
                "is_agent_created": item.get("is_agent_created"),
            }
        )
        for item in items[:MAX_EVENTS]
    ]
    return {"total": total, "items": compact}


def _proposals_section(fetch: FetchFn, base_url: str) -> dict[str, Any]:
    """Schedule proposals still awaiting a user decision, capped."""
    query = urllib.parse.urlencode({"status": "proposed", "limit": MAX_PROPOSALS})
    items, total = _page_items(fetch(f"{base_url}/v1/schedule/proposals?{query}"))
    compact = [
        _drop_none(
            {
                "task_id": item.get("task_id"),
                "proposed_start": item.get("proposed_start"),
                "proposed_end": item.get("proposed_end"),
            }
        )
        for item in items[:MAX_PROPOSALS]
    ]
    return {"total": total, "items": compact}


def _energy_section(fetch: FetchFn, base_url: str) -> dict[str, Any]:
    """Today's cognitive-energy forecast, reduced to scored windows."""
    payload = fetch(f"{base_url}/cognitive-energy/forecast")
    if not isinstance(payload, dict):
        return {"status": "unavailable"}
    windows = payload.get("windows")
    scored = [
        {"start": item.get("window_start"), "score": item.get("score")}
        for item in (windows if isinstance(windows, list) else [])
        if isinstance(item, dict) and item.get("status") == "ok"
    ]
    return {"status": payload.get("status"), "windows": scored}


def _weekly_report_section(fetch: FetchFn, base_url: str) -> dict[str, Any]:
    """The server-built weekly report link (Sunday briefing, PLAN §8.5).

    Only the server knows the *public* base URL and the derived read-only
    viewer credential (healthmes/api/auth.py — server code builds
    credentialed viewer links, never the LLM), so the link is lifted verbatim
    from the report payload's ``report_url``. The snapshot's own ``base_url``
    may be cluster-internal (docker: http://healthmes:8100) and must never
    leak into a phone-tappable message.
    """
    payload = fetch(f"{base_url}/reports/weekly.json")
    url = payload.get("report_url") if isinstance(payload, dict) else None
    if not isinstance(url, str) or not url:
        raise ValueError("weekly report payload carried no report_url")
    return {"url": url}


def collect_snapshot(
    base_url: str,
    *,
    fetch: FetchFn | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the snapshot; every section degrades independently.

    Top-level ``status``: ``ok`` (all sections fetched), ``partial`` (some),
    or ``unavailable`` (none — service down). The agent is told to treat an
    unavailable snapshot as "fetch state through the MCP tools yourself".
    """
    fetch = fetch or build_fetch()
    now = now or datetime.now(UTC)
    # Deterministic UTC-midnight window: today + tomorrow. The snapshot
    # carries explicit ISO bounds so the agent never has to guess the frame.
    window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_end = window_start + timedelta(hours=48)

    sections: dict[str, Any] = {}
    errors: dict[str, str] = {}

    builders: tuple[tuple[str, Callable[[], dict[str, Any]]], ...] = (
        ("open_tasks", lambda: _open_tasks_section(fetch, base_url)),
        ("events", lambda: _events_section(fetch, base_url, window_start, window_end)),
        ("pending_proposals", lambda: _proposals_section(fetch, base_url)),
        ("energy_forecast", lambda: _energy_section(fetch, base_url)),
        ("weekly_report", lambda: _weekly_report_section(fetch, base_url)),
    )
    for name, build in builders:
        try:
            sections[name] = build()
        except Exception as exc:  # degrade per-section, never crash a briefing
            errors[name] = f"{type(exc).__name__}: {exc}"

    if not sections:
        status = "unavailable"
    elif errors:
        status = "partial"
    else:
        status = "ok"

    snapshot: dict[str, Any] = {
        "kind": "healthmes_state_snapshot",
        "generated_at": now.isoformat(),
        "status": status,
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        **sections,
    }
    if errors:
        snapshot["errors"] = errors
        snapshot["note"] = "sections above failed; fetch that state via the MCP tools"
    return snapshot


def main() -> int:
    script_path = Path(__file__)
    base_url = resolve_base_url(script_path, os.environ)
    fetch = build_fetch(resolve_api_token(script_path, os.environ))
    snapshot = collect_snapshot(base_url, fetch=fetch)
    print(json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
