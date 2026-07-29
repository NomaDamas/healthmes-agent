"""Contract pins for scripts/dev_mac.sh (mac-native tooling).

Shell scripts get no import-time checking, so the invariants that protect
the read-only vendor tree are pinned here as text/syntax assertions.
"""

import plistlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "dev_mac.sh"
LOCAL_SCRIPT = REPO_ROOT / "scripts" / "healthmes_local.sh"
LAUNCH_AGENT_TEMPLATE = REPO_ROOT / "config" / "com.healthmes.local.plist.in"


def _function_body(text: str, name: str) -> str:
    match = re.search(rf"^{name}\(\)\s*\{{\n(.*?)^\}}", text, re.MULTILINE | re.DOTALL)
    assert match, f"{name}() not found in {SCRIPT}"
    return match.group(1)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_script_parses() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_ow_env_freezes_uv_against_vendor_lock_rewrites() -> None:
    """load_ow_env must export UV_FROZEN=1: `uv sync` and the bare `uv run`
    inside vendor scripts/start/*.sh would otherwise be allowed to rewrite
    vendor/open-wearables/backend/uv.lock on pyproject drift — a write into
    the read-only vendor tree. (The hermes config template and the ow-mcp
    compose service pass --frozen explicitly; this is the same guarantee for
    the mac-native path.)"""
    body = _function_body(SCRIPT.read_text(encoding="utf-8"), "load_ow_env")
    assert re.search(r"^\s*export UV_FROZEN=1\s*$", body, re.MULTILINE)
    # The venv redirect that keeps the vendored backend's venv out of vendor/.
    assert 'export UV_PROJECT_ENVIRONMENT="$OW_VENV_DIR"' in body


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_single_entry_local_script_parses_and_exposes_runtime_commands() -> None:
    subprocess.run(["bash", "-n", str(LOCAL_SCRIPT)], check=True)
    text = LOCAL_SCRIPT.read_text(encoding="utf-8")
    for command in (
        "install",
        "update",
        "start",
        "stop",
        "status",
        "open",
        "uninstall",
    ):
        assert re.search(rf"^{command}\) ", text, re.MULTILINE)
    assert 'open "$DASHBOARD_URL"' in text
    assert re.search(r"^daemon\) ", text, re.MULTILINE)
    stop_process_body = _function_body(text, "stop_process")
    assert "descendant_pids" in stop_process_body
    assert 'kill "${pids[@]}"' in stop_process_body


def test_install_registers_keepalive_login_launch_agent() -> None:
    text = LOCAL_SCRIPT.read_text(encoding="utf-8")
    install_body = _function_body(text, "cmd_install")
    assert "install_launch_agent" in install_body
    launch_agent_body = _function_body(text, "install_launch_agent")
    assert "for _ in 1 2 3 4 5 6 7 8 9 10" in launch_agent_body
    assert 'die "failed to register login launch agent"' in launch_agent_body
    daemon_body = _function_body(text, "cmd_daemon")
    assert "trap 'stop_apps; exit 0' INT TERM" in daemon_body
    assert "trap 'stop_apps' EXIT" not in daemon_body
    assert "services-stop" not in daemon_body
    assert "while true" in daemon_body
    assert daemon_body.count("cmd_start") == 2
    template = LAUNCH_AGENT_TEMPLATE.read_text(encoding="utf-8")
    assert "<key>RunAtLoad</key>" in template
    assert "<key>KeepAlive</key>" in template
    assert "<key>PATH</key>" in template
    assert "<key>LANG</key>" in template
    assert "<key>LC_ALL</key>" in template
    assert template.count("<string>en_US.UTF-8</string>") == 2
    assert "<string>daemon</string>" in template


def test_launch_agent_enables_scheduler_for_background_calendar_polling() -> None:
    template = plistlib.loads(LAUNCH_AGENT_TEMPLATE.read_bytes())
    environment = template["EnvironmentVariables"]
    assert environment["HEALTHMES_SCHEDULER_ENABLED"] == "true"


def test_uninstall_keeps_data_unless_delete_data_is_explicit() -> None:
    text = LOCAL_SCRIPT.read_text(encoding="utf-8")
    body = _function_body(text, "cmd_uninstall")
    assert '[ "${1:-}" = "--delete-data" ]' in body
    assert '[ "$DATA_DIR" = "$REPO_ROOT/data" ]' in body
    assert 'rm -rf "$DATA_DIR"' in body
