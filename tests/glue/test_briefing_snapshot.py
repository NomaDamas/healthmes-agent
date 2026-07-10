"""Tests for scripts/healthmes_briefing_snapshot.py (PLAN §4 script injection).

The vendor cron scheduler runs the script with its own interpreter under a
sanitized env, so the contract under test is deliberately narrow: stdlib-only
import, one line of JSON on stdout, exit code 0 even when the healthmes
service is unreachable (empty stdout would make the scheduler skip the
briefing; a non-zero exit turns it into an error report).

No network: fetch is faked in-process, and the subprocess test points at a
loopback port that is bound-then-closed so the connection is refused locally.
"""

import json
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "healthmes_briefing_snapshot.py"

NOW = datetime(2026, 7, 9, 7, 0, tzinfo=UTC)


def page(items: list[dict], total: int | None = None) -> dict:
    return {
        "data": items,
        "pagination": {
            "total_count": total if total is not None else len(items),
            "limit": 50,
            "offset": 0,
            "has_more": False,
        },
    }


@pytest.fixture
def fake_fetch():
    """Path-keyed fake of the healthmes REST surface."""

    def fetch(url: str):
        if "/v1/tasks" in url:
            return page(
                [
                    {
                        "id": "t1",
                        "title": "Write report",
                        "status": "todo",
                        "deadline": "2026-07-10T09:00:00+00:00",
                        "energy_demand": "high",
                        "est_minutes": 90,
                        "goal_id": None,
                        "source": "user",
                    },
                    {
                        "id": "t2",
                        "title": "Old chore",
                        "status": "done",
                        "deadline": None,
                        "energy_demand": "low",
                        "est_minutes": None,
                        "goal_id": None,
                        "source": "user",
                    },
                ]
            )
        if "/v1/schedule/events" in url:
            return page(
                [
                    {
                        "id": "e1",
                        "external_id": "ext-1",
                        "summary": "Team sync",
                        "start_at": "2026-07-09T10:00:00+00:00",
                        "end_at": "2026-07-09T10:30:00+00:00",
                        "calendar_source": "google",
                        "is_agent_created": False,
                        "agent_task_id": None,
                    }
                ]
            )
        if "/v1/schedule/proposals" in url:
            return page([])
        if "/cognitive-energy/forecast" in url:
            return {
                "date": "2026-07-09",
                "status": "ok",
                "baseline_window_days": 14,
                "windows": [
                    {
                        "window_start": "2026-07-09T09:00:00+00:00",
                        "status": "ok",
                        "score": 78,
                    },
                    {
                        "window_start": "2026-07-09T14:00:00+00:00",
                        "status": "insufficient_data",
                        "score": None,
                    },
                ],
            }
        raise AssertionError(f"unexpected url: {url}")

    return fetch


def test_collect_snapshot_shape(snapshot_script, fake_fetch):
    snap = snapshot_script.collect_snapshot(
        "http://healthmes.test:8100", fetch=fake_fetch, now=NOW
    )
    assert snap["kind"] == "healthmes_state_snapshot"
    assert snap["status"] == "ok"
    assert "errors" not in snap
    # Deterministic UTC-midnight window with explicit ISO bounds.
    assert snap["window"] == {
        "start": "2026-07-09T00:00:00+00:00",
        "end": "2026-07-11T00:00:00+00:00",
    }

    # Done/cancelled tasks are filtered out; kept items are compact
    # (selected keys only, None values dropped).
    assert snap["open_tasks"]["total"] == 1
    assert snap["open_tasks"]["items"] == [
        {
            "title": "Write report",
            "status": "todo",
            "deadline": "2026-07-10T09:00:00+00:00",
            "energy_demand": "high",
            "est_minutes": 90,
        }
    ]
    assert snap["events"]["total"] == 1
    assert snap["events"]["items"][0]["summary"] == "Team sync"
    assert "agent_task_id" not in snap["events"]["items"][0]
    assert snap["pending_proposals"] == {"total": 0, "items": []}
    # Only status=ok windows survive, reduced to start+score.
    assert snap["energy_forecast"] == {
        "status": "ok",
        "windows": [{"start": "2026-07-09T09:00:00+00:00", "score": 78}],
    }


def test_snapshot_caps_item_counts(snapshot_script):
    many = [
        {"title": f"task {i}", "status": "todo", "energy_demand": "low"} for i in range(50)
    ]

    def fetch(url: str):
        if "/v1/tasks" in url:
            return page(many)
        return page([])

    snap = snapshot_script.collect_snapshot("http://x", fetch=fetch, now=NOW)
    assert snap["open_tasks"]["total"] == 50
    assert len(snap["open_tasks"]["items"]) == snapshot_script.MAX_TASKS


def test_sections_degrade_independently(snapshot_script, fake_fetch):
    def flaky(url: str):
        if "/cognitive-energy/" in url:
            raise OSError("connection refused")
        return fake_fetch(url)

    snap = snapshot_script.collect_snapshot("http://x", fetch=flaky, now=NOW)
    assert snap["status"] == "partial"
    assert "energy_forecast" not in snap
    assert "OSError" in snap["errors"]["energy_forecast"]
    assert "MCP tools" in snap["note"]
    assert snap["open_tasks"]["total"] == 1  # healthy sections survive


def test_all_sections_down_is_unavailable_not_a_crash(snapshot_script):
    def dead(url: str):
        raise OSError("connection refused")

    snap = snapshot_script.collect_snapshot("http://x", fetch=dead, now=NOW)
    assert snap["status"] == "unavailable"
    assert set(snap["errors"]) == {
        "open_tasks",
        "events",
        "pending_proposals",
        "energy_forecast",
    }


def test_base_url_resolution_order(snapshot_script, tmp_path):
    script_copy = tmp_path / "healthmes_briefing_snapshot.py"
    script_copy.write_text(SCRIPT_PATH.read_text())

    # 3. Default: localhost-native (the hard rule — never a docker hostname).
    assert (
        snapshot_script.resolve_base_url(script_copy, {})
        == "http://localhost:8100"
    )

    # 2. Sidecar written by bootstrap next to the installed copy.
    (tmp_path / "healthmes_snapshot.json").write_text(
        json.dumps({"base_url": "http://healthmes:8100/"})
    )
    assert (
        snapshot_script.resolve_base_url(script_copy, {})
        == "http://healthmes:8100"
    )

    # 1. Env var beats the sidecar.
    env = {"HEALTHMES_BASE_URL": "http://127.0.0.1:9999/"}
    assert (
        snapshot_script.resolve_base_url(script_copy, env)
        == "http://127.0.0.1:9999"
    )

    # Corrupt sidecar falls through to the default instead of raising.
    (tmp_path / "healthmes_snapshot.json").write_text("{not json")
    assert (
        snapshot_script.resolve_base_url(script_copy, {})
        == "http://localhost:8100"
    )


def test_api_token_resolution_order_and_bearer_header(snapshot_script, tmp_path):
    script_copy = tmp_path / "healthmes_briefing_snapshot.py"
    script_copy.write_text(SCRIPT_PATH.read_text())

    # Default: no token (auth disabled on the loopback dev path).
    assert snapshot_script.resolve_api_token(script_copy, {}) == ""

    # Sidecar written by bootstrap next to the installed copy.
    (tmp_path / "healthmes_snapshot.json").write_text(
        json.dumps({"base_url": "http://healthmes:8100", "api_token": "sidecar-tok"})
    )
    assert snapshot_script.resolve_api_token(script_copy, {}) == "sidecar-tok"

    # Env var beats the sidecar.
    env = {"HEALTHMES_API_TOKEN": "env-tok"}
    assert snapshot_script.resolve_api_token(script_copy, env) == "env-tok"

    # build_fetch attaches the bearer header iff a token is present.
    import urllib.request

    captured: dict[str, str] = {}

    class _Resp:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _Opener:
        def open(self, request, timeout=None):
            captured.update(request.headers)
            return _Resp()

    original = urllib.request.build_opener
    urllib.request.build_opener = lambda *a, **kw: _Opener()
    try:
        snapshot_script.build_fetch("tok-123")("http://x/v1/tasks")
        assert captured.get("Authorization") == "Bearer tok-123"
        captured.clear()
        snapshot_script.build_fetch("")("http://x/v1/tasks")
        assert "Authorization" not in captured
    finally:
        urllib.request.build_opener = original


def _refused_port() -> int:
    """A loopback port that is closed right now (bind, read, release)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_cli_prints_one_json_line_and_exits_zero_when_unreachable():
    """The scheduler contract end-to-end: run the file exactly like the
    vendor does (bare interpreter, no repo venv on the path) against a
    refused loopback port — still exit 0 with one line of parseable JSON."""
    port = _refused_port()
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        capture_output=True,
        text=True,
        timeout=60,
        env={"HEALTHMES_BASE_URL": f"http://127.0.0.1:{port}", "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1  # empty stdout would silently skip the briefing
    snap = json.loads(lines[0])
    assert snap["kind"] == "healthmes_state_snapshot"
    assert snap["status"] == "unavailable"
