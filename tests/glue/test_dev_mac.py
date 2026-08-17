"""Contract pins for scripts/dev_mac.sh (mac-native tooling).

Shell scripts get no import-time checking, so the invariants that protect
the read-only vendor tree are pinned here as text/syntax assertions.
"""

import os
import plistlib
import re
import shutil
import signal
import subprocess
import textwrap
import time
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "dev_mac.sh"
LOCAL_SCRIPT = REPO_ROOT / "scripts" / "healthmes_local.sh"
LAUNCH_AGENT_TEMPLATE = REPO_ROOT / "config" / "com.healthmes.local.plist.in"
MAKEFILE = REPO_ROOT / "Makefile"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
DEVELOPMENT_DOC = REPO_ROOT / "docs" / "DEVELOPMENT.md"
README = REPO_ROOT / "README.md"


def _function_body(text: str, name: str) -> str:
    match = re.search(rf"^{name}\(\)\s*\{{\n(.*?)^\}}", text, re.MULTILINE | re.DOTALL)
    assert match, f"{name}() not found in {SCRIPT}"
    return match.group(1)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def _write_process_identity(
    runtime_dir: Path,
    *,
    process_name: str = "healthmes",
    pid: str = "4242",
    executable: str = "/bin/bash",
    start_time: str = "Mon Aug  3 12:00:00 2026",
    nonce: str = "abc123",
) -> Path:
    pid_file = runtime_dir / f"{process_name}.pid"
    pid_file.write_text(f"{pid}\n", encoding="utf-8")
    pid_file.with_suffix(".pid.identity").write_text(
        "\n".join(
            (
                f"pid\t{pid}",
                f"pgid\t{pid}",
                f"executable\t{executable}",
                f"start_time\t{start_time}",
                f"nonce\t{nonce}",
                "",
            )
        ),
        encoding="utf-8",
    )
    return pid_file


def _write_decision_stop_budget(
    runtime_dir: Path,
    *,
    drain_timeout_seconds: int,
    pid: str = "4242",
    start_time: str = "Mon Aug  3 12:00:00 2026",
    nonce: str = "abc123",
    supervisor_pid: str = "4343",
    supervisor_start_token: str = "darwin:1786915200:123456",
    publication_instance_nonce: str = "publication123",
    version: int = 3,
    filename: str = "hermes-decision-stop-budget",
) -> Path:
    path = runtime_dir / filename
    if version == 3:
        fields = (
            f"version\t{version}",
            f"drain_timeout_seconds\t{drain_timeout_seconds}",
            f"launcher_pid\t{pid}",
            f"launcher_start_token\tps:{start_time}",
            f"launcher_service_nonce\t{nonce}",
            f"supervisor_pid\t{supervisor_pid}",
            f"supervisor_start_token\t{supervisor_start_token}",
            "publication_instance_nonce\t"
            f"{publication_instance_nonce}",
            "",
        )
    elif version == 2:
        fields = (
            f"version\t{version}",
            f"drain_timeout_seconds\t{drain_timeout_seconds}",
            f"supervisor_pid\t{pid}",
            f"supervisor_start_token\tps:{start_time}",
            f"service_nonce\t{nonce}",
            "publication_instance_nonce\t"
            f"{publication_instance_nonce}",
            "",
        )
    else:
        fields = (
            f"version\t{version}",
            f"drain_timeout_seconds\t{drain_timeout_seconds}",
            f"supervisor_pid\t{pid}",
            f"supervisor_start_token\tps:{start_time}",
            f"service_nonce\t{nonce}",
            "",
        )
    path.write_text(
        "\n".join(fields),
        encoding="ascii",
    )
    return path


def _write_decision_startup_lease(
    runtime_dir: Path,
    *,
    state: str = "spawned",
    pid: str = "4242",
    nonce: str = "abc123",
) -> Path:
    lease = runtime_dir / "hermes-decision-startup-lease"
    lease.mkdir(mode=0o700)
    fields = (
        "version\t1",
        f"state\t{state}",
        f"launcher_service_nonce\t{nonce}",
    )
    if state == "spawned":
        fields += (f"launcher_pid\t{pid}",)
    (lease / "record").write_text(
        "\n".join((*fields, "")),
        encoding="ascii",
    )
    return lease


def _local_runtime_harness(tmp_path: Path) -> dict[str, object]:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    runtime = repo / "data" / "runtime"
    home = tmp_path / "home"
    fake_bin = tmp_path / "fake-bin"
    process_state = tmp_path / "process-state"
    supervisor_state = process_state / "supervisor"
    for directory in (scripts, runtime, home / "Library" / "LaunchAgents", fake_bin):
        directory.mkdir(parents=True, exist_ok=True)
    process_state.mkdir()
    supervisor_state.mkdir()

    local_script = scripts / "healthmes_local.sh"
    shutil.copy2(LOCAL_SCRIPT, local_script)
    dev_mac = scripts / "dev_mac.sh"
    _write_executable(
        dev_mac,
        """
        #!/usr/bin/env bash
        printf 'dev_mac %s\\n' "$*" >>"$FAKE_EVENT_LOG"
        """,
    )
    event_log = tmp_path / "events.log"
    event_log.touch()

    _write_executable(
        fake_bin / "launchctl",
        """
        #!/usr/bin/env bash
        printf 'launchctl %s\\n' "$*" >>"$FAKE_EVENT_LOG"
        if [ "${1:-}" = "print" ]; then
            exit "${FAKE_LAUNCHCTL_PRINT_STATUS:-1}"
        fi
        """,
    )
    _write_executable(
        fake_bin / "ps",
        """
        #!/usr/bin/env bash
        printf 'ps %s\\n' "$*" >>"$FAKE_EVENT_LOG"
        [ -f "$FAKE_PROCESS_STATE/alive" ] || exit 1
        requested_pid=
        field=
        while [ "$#" -gt 0 ]; do
            case "$1" in
            -p)
                shift
                requested_pid=${1:-}
                ;;
            -o)
                shift
                field=${1%=}
                ;;
            esac
            shift
        done
        current_pid="$(<"$FAKE_PROCESS_STATE/pid")"
        [ "$requested_pid" = "$current_pid" ] || exit 1
        case "$field" in
        pid) printf '%s\\n' "$current_pid" ;;
        pgid) cat "$FAKE_PROCESS_STATE/pgid" ;;
        comm) cat "$FAKE_PROCESS_STATE/executable" ;;
        lstart) cat "$FAKE_PROCESS_STATE/start_time" ;;
        command) cat "$FAKE_PROCESS_STATE/command" ;;
        *) exit 1 ;;
        esac
        """,
    )
    _write_executable(
        fake_bin / "kill",
        """
        #!/usr/bin/env bash
        printf 'kill %s\\n' "$*" >>"$FAKE_EVENT_LOG"
        if [ "${1:-}" = "-s" ] && [ "${2:-}" = "TERM" ]; then
            case "${FAKE_TERM_BEHAVIOR:-stay}" in
            exit)
                rm -f "$FAKE_PROCESS_STATE/alive"
                rm -f "$FAKE_SUPERVISOR_STATE/alive"
                ;;
            late-budget-fail)
                cp "$FAKE_LATE_STOP_BUDGET" "$FAKE_STOP_BUDGET"
                rm -f "$FAKE_PROCESS_STATE/alive"
                rm -f "$FAKE_SUPERVISOR_STATE/alive"
                ;;
            late-budget-success)
                cp "$FAKE_LATE_STOP_BUDGET" "$FAKE_STOP_BUDGET"
                rm -f "$FAKE_STOP_BUDGET"
                rm -f "$FAKE_PROCESS_STATE/alive"
                rm -f "$FAKE_SUPERVISOR_STATE/alive"
                ;;
            wrapper-exit-only)
                rm -f "$FAKE_PROCESS_STATE/alive"
                ;;
            reuse)
                printf '%s\\n' "Mon Aug  3 12:01:00 2026" \
                    >"$FAKE_PROCESS_STATE/start_time"
                ;;
            esac
        fi
        if [ "${1:-}" = "-s" ] && [ "${2:-}" = "KILL" ]; then
            case "${FAKE_KILL_BEHAVIOR:-exit}" in
            exit)
                rm -f "$FAKE_PROCESS_STATE/alive"
                ;;
            reuse)
                printf '%s\\n' "Mon Aug  3 12:02:00 2026" \
                    >"$FAKE_PROCESS_STATE/start_time"
                ;;
            esac
        fi
        """,
    )
    _write_executable(
        fake_bin / "sleep",
        """
        #!/usr/bin/env bash
        printf 'sleep %s\\n' "$*" >>"$FAKE_EVENT_LOG"
        """,
    )
    _write_executable(
        fake_bin / "runtime-python",
        """
        #!/usr/bin/env bash
        printf 'runtime-python %s\\n' "$*" >>"$FAKE_EVENT_LOG"
        action=
        pid=
        token=
        timeout=
        group_pgid=
        while [ "$#" -gt 0 ]; do
            case "$1" in
            --runtime-process-action)
                shift
                action=${1:-}
                ;;
            --runtime-process-pid)
                shift
                pid=${1:-}
                ;;
            --runtime-process-start-token)
                shift
                token=${1:-}
                ;;
            --runtime-process-timeout)
                shift
                timeout=${1:-}
                ;;
            --runtime-process-group-pgid)
                shift
                group_pgid=${1:-}
                ;;
            esac
            shift
        done
        if [ -n "$group_pgid" ]; then
            [ "$group_pgid" = "$(<"$FAKE_PROCESS_STATE/pgid")" ] || exit 5
            case "${FAKE_GROUP_PROBE_BEHAVIOR:-auto}" in
            empty)
                exit 0
                ;;
            nonempty)
                exit 6
                ;;
            publish-late-nonempty)
                cp "$FAKE_LATE_STOP_BUDGET" "$FAKE_STOP_BUDGET"
                exit 6
                ;;
            unknown)
                exit 5
                ;;
            auto)
                if [ -f "$FAKE_PROCESS_STATE/alive" ] \
                    || [ -f "$FAKE_SUPERVISOR_STATE/alive" ]; then
                    exit 6
                fi
                exit 0
                ;;
            *)
                exit 5
                ;;
            esac
        fi
        if [ ! -f "$FAKE_SUPERVISOR_STATE/alive" ]; then
            [ "$action" = "wait" ] && exit 0
            exit 3
        fi
        [ "$pid" = "$(<"$FAKE_SUPERVISOR_STATE/pid")" ] || exit 4
        [ "$token" = "$(<"$FAKE_SUPERVISOR_STATE/start_token")" ] || exit 4
        if [ "$action" = "wait" ]; then
            [ -n "$timeout" ] || exit 5
            exit 6
        fi
        if [ "$action" = "signal" ]; then
            case "${FAKE_SUPERVISOR_TERM_BEHAVIOR:-exit}" in
            exit)
                rm -f "$FAKE_SUPERVISOR_STATE/alive"
                rm -f "$FAKE_PROCESS_STATE/alive"
                if [ "${FAKE_SUPERVISOR_CLEANUP_PROOF:-remove}" = "remove" ]; then
                    rm -f "$FAKE_STOP_BUDGET"
                fi
                ;;
            supervisor-exit-only)
                rm -f "$FAKE_SUPERVISOR_STATE/alive"
                ;;
            replace-generation)
                cp "$FAKE_LATE_STOP_BUDGET" "$FAKE_STOP_BUDGET"
                rm -f "$FAKE_SUPERVISOR_STATE/alive"
                rm -f "$FAKE_PROCESS_STATE/alive"
                ;;
            replace-launcher-metadata)
                rm -f "$FAKE_STOP_BUDGET"
                rm -f "$FAKE_SUPERVISOR_STATE/alive"
                rm -f "$FAKE_PROCESS_STATE/alive"
                printf '%s\\n' "5252" >"$FAKE_DECISION_PID"
                {
                    printf 'pid\\t5252\\n'
                    printf 'pgid\\t5252\\n'
                    printf 'executable\\t/bin/bash\\n'
                    printf 'start_time\\tMon Aug  3 12:10:00 2026\\n'
                    printf 'nonce\\tcompeting-generation\\n'
                } >"$FAKE_DECISION_IDENTITY"
                ;;
            esac
        fi
        """,
    )
    _write_executable(
        fake_bin / "curl",
        """
        #!/usr/bin/env bash
        printf 'curl %s\\n' "$*" >>"$FAKE_EVENT_LOG"
        exit 1
        """,
    )
    _write_executable(
        fake_bin / "uuidgen",
        """
        #!/usr/bin/env bash
        printf 'abc123\\n'
        """,
    )

    pid = "4242"
    nonce = "abc123"
    (process_state / "alive").touch()
    for name, value in (
        ("pid", pid),
        ("pgid", pid),
        ("executable", "/bin/bash"),
        ("start_time", "Mon Aug  3 12:00:00 2026"),
        (
            "command",
            f"/bin/bash {local_script} __service_runner {nonce} test-command",
        ),
    ):
        (process_state / name).write_text(f"{value}\n", encoding="utf-8")
    (supervisor_state / "alive").touch()
    (supervisor_state / "pid").write_text("4343\n", encoding="utf-8")
    (supervisor_state / "start_token").write_text(
        "darwin:1786915200:123456\n",
        encoding="utf-8",
    )

    env = {
        "HOME": str(home),
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "FAKE_EVENT_LOG": str(event_log),
        "FAKE_PROCESS_STATE": str(process_state),
        "FAKE_SUPERVISOR_STATE": str(supervisor_state),
        "FAKE_STOP_BUDGET": str(
            runtime / "hermes-decision-stop-budget"
        ),
        "FAKE_DECISION_PID": str(
            runtime / "hermes-decision.pid"
        ),
        "FAKE_DECISION_IDENTITY": str(
            runtime / "hermes-decision.pid.identity"
        ),
        "FAKE_LATE_STOP_BUDGET": str(
            process_state / "late-stop-budget"
        ),
        "HEALTHMES_DEV_MAC_SCRIPT": str(dev_mac),
        "HEALTHMES_LAUNCHCTL_BIN": str(fake_bin / "launchctl"),
        "HEALTHMES_PS_BIN": str(fake_bin / "ps"),
        "HEALTHMES_KILL_BIN": str(fake_bin / "kill"),
        "HEALTHMES_SLEEP_BIN": str(fake_bin / "sleep"),
        "HEALTHMES_UUIDGEN_BIN": str(fake_bin / "uuidgen"),
        "HEALTHMES_RUNTIME_PYTHON_BIN": str(
            fake_bin / "runtime-python"
        ),
    }
    return {
        "env": env,
        "event_log": event_log,
        "local_script": local_script,
        "process_state": process_state,
        "supervisor_state": supervisor_state,
        "runtime": runtime,
    }


def _run_local_runtime(
    harness: dict[str, object],
    command: str,
    *,
    term_behavior: str = "stay",
    kill_behavior: str = "exit",
    check: bool = True,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(harness["env"])
    env["FAKE_TERM_BEHAVIOR"] = term_behavior
    env["FAKE_KILL_BEHAVIOR"] = kill_behavior
    env["FAKE_SUPERVISOR_TERM_BEHAVIOR"] = term_behavior
    env.update(env_overrides or {})
    return subprocess.run(
        ["bash", str(harness["local_script"]), command],
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


def _event_lines(harness: dict[str, object]) -> list[str]:
    return Path(harness["event_log"]).read_text(encoding="utf-8").splitlines()


def _assert_identity_check_immediately_before(events: list[str], signal_line: str) -> None:
    signal_index = events.index(signal_line)
    checks = events[signal_index - 5 : signal_index]
    assert len(checks) == 5
    for field in ("pid=", "pgid=", "comm=", "lstart=", "command="):
        assert any(event.startswith("ps ") and field in event for event in checks)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_script_parses() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_ow_env_freezes_uv_against_vendor_lock_rewrites() -> None:
    """load_ow_env must export UV_FROZEN=1: `uv sync` and the bare `uv run`
    inside vendor scripts/start/*.sh would otherwise be allowed to rewrite
    vendor/open-wearables/backend/uv.lock on pyproject drift — a write into
    the read-only vendor tree."""
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
    assert "signal_process_group TERM" in stop_process_body
    assert "signal_process_group KILL" in stop_process_body
    assert "clear_process_identity" in stop_process_body


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
    assert daemon_body.count("start_apps") == 2
    template = LAUNCH_AGENT_TEMPLATE.read_text(encoding="utf-8")
    assert "<key>RunAtLoad</key>" in template
    assert "<key>KeepAlive</key>" in template
    assert "<key>PATH</key>" in template
    assert "<key>LANG</key>" in template
    assert "<key>LC_ALL</key>" in template
    assert template.count("<string>en_US.UTF-8</string>") == 2
    assert "<string>daemon</string>" in template
    plist = plistlib.loads(LAUNCH_AGENT_TEMPLATE.read_bytes())
    assert plist["ExitTimeOut"] == 360


def test_native_shutdown_margin_fits_compose_and_launchagent_budgets() -> None:
    text = LOCAL_SCRIPT.read_text(encoding="utf-8")
    assert "MAX_DECISION_RUNTIME_DRAIN_SECONDS=315" in text
    assert "DECISION_RUNTIME_SHUTDOWN_MARGIN_SECONDS=2" in text
    assert "MAX_DECISION_RUNTIME_TERM_WAIT_SECONDS=$((" in text

    plist = plistlib.loads(LAUNCH_AGENT_TEMPLATE.read_bytes())
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    assert plist["ExitTimeOut"] == 360
    assert compose["services"]["hermes-decision"]["stop_grace_period"] == "6m"


def test_launch_agent_enables_scheduler_for_background_calendar_polling() -> None:
    template = plistlib.loads(LAUNCH_AGENT_TEMPLATE.read_bytes())
    environment = template["EnvironmentVariables"]
    assert environment["HEALTHMES_SCHEDULER_ENABLED"] == "true"


def test_local_start_uses_only_optional_dedicated_decision_runtime() -> None:
    text = LOCAL_SCRIPT.read_text(encoding="utf-8")
    start_body = _function_body(text, "start_apps")
    configured_body = _function_body(text, "decision_runtime_configured")
    load_env_body = _function_body(text, "load_runtime_env")

    assert "sync_hermes_ow_api_key" not in text
    assert "HEALTHMES_HERMES_WEBHOOK" not in load_env_body
    assert "HEALTHMES_DECISION_HERMES_MODEL" in configured_body
    assert "HEALTHMES_DECISION_HERMES_PROVIDER" in configured_body
    assert "requires both" in configured_body
    assert "if decision_runtime_configured; then" in start_body
    assert 'UV_PROJECT_ENVIRONMENT="$HERMES_DECISION_VENV"' in start_body
    assert "uv sync --frozen --no-dev" in start_body
    assert "scripts/bootstrap.py" in start_body
    assert '[ -x "$RUNTIME_PYTHON_BIN" ]' in start_body
    assert 'printf -v quoted_python \'%q\' "$RUNTIME_PYTHON_BIN"' in start_body
    assert "uv run python -m healthmes.hermes_runtime_supervisor" not in start_body
    assert "healthmes.hermes_runtime_supervisor" in start_body
    assert "--shutdown-budget-path" in start_body
    assert '"$HERMES_DECISION_STOP_BUDGET"' in start_body
    assert '"$HERMES_DECISION_STARTUP_LEASE"' in start_body
    start_process_body = _function_body(text, "start_process")
    assert start_process_body.index(
        "create_decision_runtime_startup_lease"
    ) < start_process_body.index("nohup env HEALTHMES_SERVICE_NONCE")
    assert start_process_body.index(
        "write_unverified_process_pid"
    ) < start_process_body.index(
        "mark_decision_runtime_startup_spawned"
    )
    assert "preserving the startup lease and PID tombstone" in (
        start_process_body
    )
    assert start_body.index("stop_decision_runtime") < start_body.index(
        "uv sync --frozen --no-dev"
    )
    assert start_body.index("scripts/bootstrap.py") < start_body.index(
        "healthmes.hermes_runtime_supervisor"
    )
    assert start_body.index("uv sync --frozen --no-dev") < start_body.index(
        "scripts/bootstrap.py"
    )
    assert start_body.index("scripts/bootstrap.py") < start_body.index(
        'start_process "Open Wearables"'
    )
    assert start_body.index('start_process "Open Wearables"') < (
        start_body.index('start_process "HealthMes"')
    )
    assert start_body.index('start_process "HealthMes"') < start_body.index(
        "http://127.0.0.1:${HEALTHMES_PORT:-8100}/health"
    )
    assert start_body.index(
        "http://127.0.0.1:${HEALTHMES_PORT:-8100}/health"
    ) < start_body.index(
        "healthmes.hermes_runtime_supervisor"
    )
    assert (
        "Hermes decision runtime disabled (model/provider not configured)"
        in start_body
    )


def test_native_mutation_paths_drain_decision_runtime_first() -> None:
    text = LOCAL_SCRIPT.read_text(encoding="utf-8")
    install_body = _function_body(text, "cmd_install")
    update_body = _function_body(text, "cmd_update")
    start_body = _function_body(text, "start_apps")

    assert install_body.index("stop_decision_runtime") < install_body.index(
        '"$DEV_MAC_SCRIPT" setup'
    )
    assert update_body.index("stop_decision_runtime") < update_body.index(
        'git -C "$REPO_ROOT" pull --ff-only'
    )
    assert update_body.index("stop_decision_runtime") < update_body.index(
        '"$DEV_MAC_SCRIPT" setup'
    )
    assert start_body.index("stop_decision_runtime") < start_body.index(
        "uv sync --frozen --no-dev"
    )
    assert start_body.index("stop_decision_runtime") < start_body.index(
        "scripts/bootstrap.py"
    )


def test_local_runtime_supervises_and_stops_decision_process() -> None:
    text = LOCAL_SCRIPT.read_text(encoding="utf-8")
    daemon_body = _function_body(text, "cmd_daemon")
    stop_body = _function_body(text, "stop_apps")
    status_body = _function_body(text, "cmd_status")

    assert '"$HERMES_DECISION_PID"' in daemon_body
    assert "decision_runtime_configured" in daemon_body
    assert "stop_decision_runtime" in stop_body
    assert "decision_runtime_status" in status_body


def test_local_runtime_starts_and_supervises_open_wearables_beat() -> None:
    dev_text = SCRIPT.read_text(encoding="utf-8")
    local_text = LOCAL_SCRIPT.read_text(encoding="utf-8")

    assert re.search(r"^ow-beat\) ", dev_text, re.MULTILINE)
    beat_body = _function_body(dev_text, "cmd_ow_beat")
    assert "celery -A app.main:celery_app beat -l info" in beat_body
    assert '--schedule "$DATA_DIR/open-wearables-celerybeat-schedule"' in beat_body

    start_body = _function_body(local_text, "start_apps")
    daemon_body = _function_body(local_text, "cmd_daemon")
    stop_body = _function_body(local_text, "stop_apps")
    status_body = _function_body(local_text, "cmd_status")

    assert 'start_process "Open Wearables beat"' in start_body
    assert '"$BEAT_PID"' in daemon_body
    assert 'stop_process "Open Wearables beat"' in stop_body
    assert 'service_status "Open Wearables beat"' in status_body


def test_manual_mac_runtime_exposes_open_wearables_beat_target() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "mac-ow-beat" in makefile.split(".PHONY:", 1)[1].split("\n\n", 1)[0]
    assert re.search(
        r"^mac-ow-beat:.*\n\t\$\(DEV_MAC\) ow-beat$",
        makefile,
        re.MULTILINE,
    )


def test_compose_runtime_starts_open_wearables_beat() -> None:
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    beat = compose["services"]["ow-beat"]

    assert beat["command"] == "scripts/start/beat.sh"
    assert beat["environment"] == ["DB_HOST=postgres", "REDIS_HOST=redis"]
    assert set(beat["depends_on"]) == {"redis", "postgres", "ow-backend"}
    assert beat["restart"] == "on-failure"


def test_runtime_docs_include_open_wearables_beat() -> None:
    development = " ".join(DEVELOPMENT_DOC.read_text(encoding="utf-8").split())
    readme = " ".join(README.read_text(encoding="utf-8").split())

    assert "`make mac-ow-beat`" in development
    assert "Celery Beat" in development
    assert "ow-beat" in readme


def test_runtime_docs_match_fail_closed_shutdown_budget_contract() -> None:
    development = " ".join(
        DEVELOPMENT_DOC.read_text(encoding="utf-8").split()
    )

    assert (
        "before Uvicorn's first startup operation runs the ASGI lifespan "
        "that may launch Hermes in its separate process group"
    ) in development
    assert (
        "Before spawning the managed Bash wrapper, the launcher atomically "
        "creates `data/runtime/hermes-decision-startup-lease/`"
    ) in development
    assert (
        "one failed or unreadable query is unknown, not evidence that the "
        "process is absent"
    ) in development
    assert (
        "stop/update cannot report success or delete metadata while a "
        "startup generation remains unverified"
    ) in development
    assert (
        "`status` reports pending startup or unknown unverified startup, "
        "never `stopped`, while the lease remains"
    ) in development
    assert (
        "Missing-budget stop reports success only after that proof"
        in development
    )
    assert (
        "Empty, partial, extra-column, duplicate, non-numeric, "
        "stderr-bearing, or libproc-inconsistent output is unknown and "
        "fails closed"
    ) in development
    assert (
        "native stop reports success only if the exact v3 record is gone"
        in development
    )
    assert (
        "unreadable `/proc` process records, malformed Darwin process "
        "listings, and unprovable identities fail closed"
    ) in development


def test_uninstall_keeps_data_unless_delete_data_is_explicit() -> None:
    text = LOCAL_SCRIPT.read_text(encoding="utf-8")
    body = _function_body(text, "cmd_uninstall")
    assert '[ "${1:-}" = "--delete-data" ]' in body
    assert '[ "$DATA_DIR" = "$REPO_ROOT/data" ]' in body
    assert 'rm -rf "$DATA_DIR"' in body


def test_stop_disables_keepalive_before_signaling_verified_process_group(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    _write_process_identity(Path(harness["runtime"]))

    _run_local_runtime(harness, "stop")

    events = _event_lines(harness)
    disable = next(
        index for index, event in enumerate(events) if event.startswith("launchctl disable ")
    )
    bootout = next(
        index for index, event in enumerate(events) if event.startswith("launchctl bootout ")
    )
    term = events.index("kill -s TERM -4242")
    hard_kill = events.index("kill -s KILL -4242")
    assert disable < bootout < term < hard_kill
    _assert_identity_check_immediately_before(events, "kill -s TERM -4242")
    _assert_identity_check_immediately_before(events, "kill -s KILL -4242")
    assert not (Path(harness["runtime"]) / "healthmes.pid").exists()
    assert not (Path(harness["runtime"]) / "healthmes.pid.identity").exists()


def test_decision_stop_adds_bounded_margin_to_exact_saved_budget(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    pid_file = _write_process_identity(
        runtime,
        process_name="hermes-decision",
    )
    _write_decision_stop_budget(
        runtime,
        drain_timeout_seconds=3,
    )
    result = _run_local_runtime(
        harness,
        "stop",
        check=False,
        env_overrides={
            "HEALTHMES_DECISION_TIMEOUT_SECONDS": "300",
            "HEALTHMES_DECISION_HERMES_MAX_ITERATION_TIMEOUT_SECONDS": "1",
            "HEALTHMES_DECISION_RUNTIME_CHILD_TERM_TIMEOUT_SECONDS": "10",
            "HEALTHMES_DECISION_RUNTIME_CHILD_KILL_TIMEOUT_SECONDS": "5",
        },
    )

    events = _event_lines(harness)
    assert result.returncode != 0
    assert not any(event.startswith("kill ") for event in events)
    assert any(
        "--runtime-process-action signal" in event
        for event in events
        if event.startswith("runtime-python ")
    )
    assert any(
        "--runtime-process-action wait" in event
        and "--runtime-process-timeout 5" in event
        for event in events
        if event.startswith("runtime-python ")
    )
    assert events.count("sleep 1") == 0
    assert "did not stop within 5s" in result.stderr
    assert "refusing to orphan its child process group" in result.stderr
    assert pid_file.exists()
    assert pid_file.with_suffix(".pid.identity").exists()
    assert (runtime / "hermes-decision-stop-budget").exists()


def test_decision_stop_uses_saved_startup_budget_not_mutable_env(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    _write_process_identity(
        runtime,
        process_name="hermes-decision",
    )
    budget = _write_decision_stop_budget(
        runtime,
        drain_timeout_seconds=2,
    )

    _run_local_runtime(
        harness,
        "stop",
        term_behavior="exit",
        env_overrides={
            "HEALTHMES_DECISION_TIMEOUT_SECONDS": "300",
            "HEALTHMES_DECISION_HERMES_MAX_ITERATION_TIMEOUT_SECONDS": "1",
            "HEALTHMES_DECISION_RUNTIME_CHILD_TERM_TIMEOUT_SECONDS": "10",
            "HEALTHMES_DECISION_RUNTIME_CHILD_KILL_TIMEOUT_SECONDS": "5",
        },
    )

    events = _event_lines(harness)
    assert not any(event.startswith("kill ") for event in events)
    assert any(
        "--runtime-process-action signal" in event
        for event in events
        if event.startswith("runtime-python ")
    )
    assert events.count("sleep 1") == 0
    assert not budget.exists()


def test_decision_stop_rejects_exit_without_descendant_cleanup_proof(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    pid_file = _write_process_identity(
        runtime,
        process_name="hermes-decision",
    )
    budget = _write_decision_stop_budget(
        runtime,
        drain_timeout_seconds=2,
    )

    result = _run_local_runtime(
        harness,
        "stop",
        term_behavior="exit",
        check=False,
        env_overrides={
            "FAKE_SUPERVISOR_CLEANUP_PROOF": "retain",
        },
    )

    assert result.returncode != 0
    assert (
        "supervisor exited without proving Hermes descendant cleanup"
        in result.stderr
    )
    assert "preserving shutdown budget and launcher metadata" in (
        result.stderr
    )
    assert budget.exists()
    assert pid_file.exists()
    assert pid_file.with_suffix(".pid.identity").exists()
    assert not (
        Path(harness["supervisor_state"]) / "alive"
    ).exists()
    assert not (Path(harness["process_state"]) / "alive").exists()


def test_decision_stop_rejects_v3_generation_change_during_cleanup(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    pid_file = _write_process_identity(
        runtime,
        process_name="hermes-decision",
    )
    _write_decision_stop_budget(
        runtime,
        drain_timeout_seconds=2,
        publication_instance_nonce="initial-publication",
    )
    replacement = _write_decision_stop_budget(
        Path(harness["process_state"]),
        drain_timeout_seconds=2,
        publication_instance_nonce="replacement-publication",
        filename="late-stop-budget",
    )

    result = _run_local_runtime(
        harness,
        "stop",
        check=False,
        env_overrides={
            "FAKE_SUPERVISOR_TERM_BEHAVIOR": "replace-generation",
        },
    )

    published_budget = runtime / "hermes-decision-stop-budget"
    assert result.returncode != 0
    assert "budget generation changed during stop" in result.stderr
    assert published_budget.read_bytes() == replacement.read_bytes()
    assert pid_file.exists()
    assert pid_file.with_suffix(".pid.identity").exists()


def test_decision_stop_rechecks_budget_published_during_startup_stop(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    process_state = Path(harness["process_state"])
    pid_file = _write_process_identity(
        runtime,
        process_name="hermes-decision",
    )
    late_budget = _write_decision_stop_budget(
        process_state,
        drain_timeout_seconds=2,
        filename="late-stop-budget",
    )

    result = _run_local_runtime(
        harness,
        "stop",
        term_behavior="late-budget-fail",
        check=False,
    )

    published_budget = runtime / "hermes-decision-stop-budget"
    assert result.returncode != 0
    assert "without proving Hermes descendant cleanup" in result.stderr
    assert published_budget.read_bytes() == late_budget.read_bytes()
    assert pid_file.exists()
    assert pid_file.with_suffix(".pid.identity").exists()
    assert "kill -s TERM -4242" in _event_lines(harness)


def test_decision_stop_accepts_late_budget_only_after_cleanup_removes_it(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    _write_process_identity(
        runtime,
        process_name="hermes-decision",
    )
    _write_decision_stop_budget(
        Path(harness["process_state"]),
        drain_timeout_seconds=2,
        filename="late-stop-budget",
    )

    _run_local_runtime(
        harness,
        "stop",
        term_behavior="late-budget-success",
    )

    assert not (runtime / "hermes-decision-stop-budget").exists()
    assert not (runtime / "hermes-decision.pid").exists()
    assert not (runtime / "hermes-decision.pid.identity").exists()


def test_decision_stop_hands_off_budget_published_during_group_probe(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    _write_process_identity(
        runtime,
        process_name="hermes-decision",
    )
    _write_decision_stop_budget(
        Path(harness["process_state"]),
        drain_timeout_seconds=2,
        filename="late-stop-budget",
    )

    _run_local_runtime(
        harness,
        "stop",
        term_behavior="wrapper-exit-only",
        env_overrides={
            "FAKE_GROUP_PROBE_BEHAVIOR": "publish-late-nonempty",
            "FAKE_SUPERVISOR_TERM_BEHAVIOR": "exit",
        },
    )

    events = _event_lines(harness)
    assert "kill -s TERM -4242" in events
    assert any(
        "--runtime-process-group-pgid 4242" in event
        for event in events
        if event.startswith("runtime-python ")
    )
    assert any(
        "--runtime-process-action signal" in event
        for event in events
        if event.startswith("runtime-python ")
    )
    assert not (runtime / "hermes-decision-stop-budget").exists()
    assert not (runtime / "hermes-decision.pid").exists()
    assert not (runtime / "hermes-decision.pid.identity").exists()


def test_decision_stop_preserves_late_budget_when_handoff_cleanup_fails(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    pid_file = _write_process_identity(
        runtime,
        process_name="hermes-decision",
    )
    late_budget = _write_decision_stop_budget(
        Path(harness["process_state"]),
        drain_timeout_seconds=2,
        filename="late-stop-budget",
    )

    result = _run_local_runtime(
        harness,
        "stop",
        term_behavior="wrapper-exit-only",
        check=False,
        env_overrides={
            "FAKE_GROUP_PROBE_BEHAVIOR": "publish-late-nonempty",
            "FAKE_SUPERVISOR_TERM_BEHAVIOR": "exit",
            "FAKE_SUPERVISOR_CLEANUP_PROOF": "retain",
        },
    )

    published_budget = runtime / "hermes-decision-stop-budget"
    assert result.returncode != 0
    assert "without proving Hermes descendant cleanup" in result.stderr
    assert published_budget.read_bytes() == late_budget.read_bytes()
    assert pid_file.exists()
    assert pid_file.with_suffix(".pid.identity").exists()


def test_decision_stop_fails_closed_when_group_survives_without_budget(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    pid_file = _write_process_identity(
        runtime,
        process_name="hermes-decision",
    )

    result = _run_local_runtime(
        harness,
        "stop",
        term_behavior="wrapper-exit-only",
        check=False,
        env_overrides={
            "FAKE_GROUP_PROBE_BEHAVIOR": "nonempty",
        },
    )

    assert result.returncode != 0
    assert "untracked descendants" in result.stderr
    assert pid_file.exists()
    assert pid_file.with_suffix(".pid.identity").exists()


def test_decision_stop_fails_closed_when_group_probe_is_unknown(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    pid_file = _write_process_identity(
        runtime,
        process_name="hermes-decision",
    )

    result = _run_local_runtime(
        harness,
        "stop",
        term_behavior="wrapper-exit-only",
        check=False,
        env_overrides={
            "FAKE_GROUP_PROBE_BEHAVIOR": "unknown",
        },
    )

    assert result.returncode != 0
    assert "cleanup cannot be proven" in result.stderr
    assert pid_file.exists()
    assert pid_file.with_suffix(".pid.identity").exists()


def test_decision_stop_rejects_late_budget_from_another_launcher_generation(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    pid_file = _write_process_identity(
        runtime,
        process_name="hermes-decision",
    )
    _write_decision_stop_budget(
        Path(harness["process_state"]),
        drain_timeout_seconds=2,
        nonce="other-generation",
        filename="late-stop-budget",
    )

    result = _run_local_runtime(
        harness,
        "stop",
        term_behavior="late-budget-fail",
        check=False,
    )

    assert result.returncode != 0
    assert "generation changed during launcher handoff" in result.stderr
    assert (runtime / "hermes-decision-stop-budget").exists()
    assert pid_file.exists()
    assert pid_file.with_suffix(".pid.identity").exists()


def test_decision_stop_ignores_budget_from_another_service_identity(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    _write_process_identity(
        runtime,
        process_name="hermes-decision",
    )
    budget = _write_decision_stop_budget(
        runtime,
        drain_timeout_seconds=2,
        nonce="different-service",
    )

    result = _run_local_runtime(
        harness,
        "stop",
        check=False,
    )

    assert result.returncode != 0
    assert "stop budget does not match the managed launcher" in (
        result.stderr
    )
    assert not any(
        event.startswith("kill ") for event in _event_lines(harness)
    )
    assert budget.exists()


@pytest.mark.parametrize("version", (1, 2))
def test_decision_stop_uses_conservative_legacy_compatibility_budget(
    tmp_path: Path,
    version: int,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    _write_process_identity(
        runtime,
        process_name="hermes-decision",
    )
    budget = _write_decision_stop_budget(
        runtime,
        drain_timeout_seconds=2,
        version=version,
    )

    result = _run_local_runtime(
        harness,
        "stop",
        check=False,
    )

    assert result.returncode != 0
    assert _event_lines(harness).count("sleep 1") == 317
    assert "kill -s TERM -4242" in _event_lines(harness)
    assert budget.exists()


def test_decision_stop_signals_live_supervisor_after_launcher_dies(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    _write_process_identity(
        runtime,
        process_name="hermes-decision",
    )
    budget = _write_decision_stop_budget(
        runtime,
        drain_timeout_seconds=2,
    )
    (Path(harness["process_state"]) / "alive").unlink()

    _run_local_runtime(
        harness,
        "stop",
        term_behavior="exit",
    )

    events = _event_lines(harness)
    assert any(
        "--runtime-process-action signal" in event
        for event in events
        if event.startswith("runtime-python ")
    )
    assert not any(event.startswith("kill ") for event in events)
    assert not budget.exists()
    assert not (
        Path(harness["supervisor_state"]) / "alive"
    ).exists()


def test_decision_stop_recovers_when_wrapper_metadata_is_missing(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    budget = _write_decision_stop_budget(
        runtime,
        drain_timeout_seconds=2,
    )

    result = _run_local_runtime(
        harness,
        "stop",
        term_behavior="exit",
    )

    events = _event_lines(harness)
    assert "managed launcher unavailable" in result.stdout
    assert any(
        "--runtime-process-action signal" in event
        for event in events
        if event.startswith("runtime-python ")
    )
    assert not budget.exists()


def test_decision_stop_recovers_unverified_startup_from_matching_v3_budget(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    pid_file = runtime / "hermes-decision.pid"
    pid_file.write_text("4242\n", encoding="ascii")
    lease = _write_decision_startup_lease(runtime)
    budget = _write_decision_stop_budget(
        runtime,
        drain_timeout_seconds=2,
    )

    _run_local_runtime(
        harness,
        "stop",
        term_behavior="exit",
    )

    events = _event_lines(harness)
    assert any(
        "--runtime-process-action signal" in event
        for event in events
        if event.startswith("runtime-python ")
    )
    assert not any(event.startswith("kill -s ") for event in events)
    assert not budget.exists()
    assert not pid_file.exists()
    assert not lease.exists()


def test_decision_stop_preserves_unverified_startup_without_v3_budget(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    pid_file = runtime / "hermes-decision.pid"
    pid_file.write_text("4242\n", encoding="ascii")
    lease = _write_decision_startup_lease(runtime)

    result = _run_local_runtime(harness, "stop", check=False)
    status = _run_local_runtime(harness, "status")

    assert result.returncode != 0
    assert "launcher identity is unverified" in result.stderr
    assert "preserving the startup lease and PID tombstone" in result.stderr
    assert (
        "Hermes decision runtime: unknown "
        "(startup launcher identity is unverified; "
        "PID tombstone and lease are preserved)"
        in status.stdout
    )
    assert pid_file.exists()
    assert lease.exists()
    assert not any(
        event.startswith("kill -s ")
        for event in _event_lines(harness)
    )


def test_pending_decision_startup_intent_blocks_stop_and_reports_starting(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    lease = _write_decision_startup_lease(
        Path(harness["runtime"]),
        state="pending",
    )

    result = _run_local_runtime(harness, "stop", check=False)
    status = _run_local_runtime(harness, "status")

    assert result.returncode != 0
    assert "startup is unresolved" in result.stderr
    assert (
        "Hermes decision runtime: starting "
        "(startup intent is published; launcher identity is not yet verified)"
        in status.stdout
    )
    assert lease.exists()
    assert not any(
        event.startswith("kill -s ")
        for event in _event_lines(harness)
    )


def test_unverified_startup_rejects_v3_budget_from_another_generation(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    pid_file = runtime / "hermes-decision.pid"
    pid_file.write_text("4242\n", encoding="ascii")
    lease = _write_decision_startup_lease(runtime)
    budget = _write_decision_stop_budget(
        runtime,
        drain_timeout_seconds=2,
        nonce="other-generation",
    )

    result = _run_local_runtime(harness, "stop", check=False)

    assert result.returncode != 0
    assert "does not match the startup lease generation" in result.stderr
    assert lease.exists()
    assert pid_file.exists()
    assert budget.exists()
    assert not any(
        "--runtime-process-action signal" in event
        for event in _event_lines(harness)
        if event.startswith("runtime-python ")
    )
    assert not any(
        event.startswith("kill -s ")
        for event in _event_lines(harness)
    )


def test_decision_stop_never_deletes_a_competing_launcher_generation(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    budget = _write_decision_stop_budget(
        runtime,
        drain_timeout_seconds=2,
    )

    result = _run_local_runtime(
        harness,
        "stop",
        check=False,
        env_overrides={
            "FAKE_SUPERVISOR_TERM_BEHAVIOR": (
                "replace-launcher-metadata"
            ),
        },
    )

    pid_file = runtime / "hermes-decision.pid"
    identity_file = runtime / "hermes-decision.pid.identity"
    assert result.returncode != 0
    assert "metadata ownership changed" in result.stderr
    assert pid_file.read_text(encoding="utf-8") == "5252\n"
    assert "competing-generation" in identity_file.read_text(
        encoding="utf-8"
    )
    assert not budget.exists()


def test_decision_stop_rejects_reused_supervisor_pid_without_signal(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    budget = _write_decision_stop_budget(
        runtime,
        drain_timeout_seconds=2,
    )
    (Path(harness["supervisor_state"]) / "start_token").write_text(
        "darwin:1786915201:654321\n",
        encoding="utf-8",
    )

    result = _run_local_runtime(harness, "stop", check=False)

    events = _event_lines(harness)
    assert result.returncode != 0
    assert "supervisor PID was reused" in result.stderr
    assert not any(
        "--runtime-process-action signal" in event
        for event in events
        if event.startswith("runtime-python ")
    )
    assert not any(event.startswith("kill ") for event in events)
    assert budget.exists()


def test_decision_stop_does_not_hide_a_surviving_wrapper(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    pid_file = _write_process_identity(
        runtime,
        process_name="hermes-decision",
    )
    budget = _write_decision_stop_budget(
        runtime,
        drain_timeout_seconds=2,
    )

    result = _run_local_runtime(
        harness,
        "stop",
        term_behavior="supervisor-exit-only",
        check=False,
    )

    assert result.returncode != 0
    assert "managed launcher remained alive" in result.stderr
    assert _event_lines(harness).count("sleep 1") == 1
    assert pid_file.exists()
    assert pid_file.with_suffix(".pid.identity").exists()
    assert budget.exists()


def test_generic_stop_bounds_term_and_post_kill_wait(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    pid_file = _write_process_identity(runtime)

    result = _run_local_runtime(
        harness,
        "stop",
        kill_behavior="stay",
        check=False,
    )

    events = _event_lines(harness)
    assert result.returncode != 0
    assert events.count("sleep 1") == 3
    assert events.index("kill -s TERM -4242") < events.index(
        "kill -s KILL -4242"
    )
    assert "remained alive 1s after SIGKILL" in result.stderr
    assert pid_file.exists()
    assert pid_file.with_suffix(".pid.identity").exists()


@pytest.mark.parametrize("pid", ("0", "1"))
def test_stop_rejects_system_pids_without_inspection_or_signal(tmp_path: Path, pid: str) -> None:
    harness = _local_runtime_harness(tmp_path)
    _write_process_identity(
        Path(harness["runtime"]),
        pid=pid,
    )

    _run_local_runtime(harness, "stop")

    events = _event_lines(harness)
    assert not any(event.startswith("ps ") for event in events)
    assert not any(event.startswith("kill ") for event in events)


@pytest.mark.parametrize(
    ("state_file", "replacement"),
    (
        ("executable", "/usr/bin/python3"),
        ("start_time", "Mon Aug  3 12:00:01 2026"),
        (
            "command",
            "/bin/bash /tmp/other.sh __service_runner different test-command",
        ),
    ),
)
def test_stop_does_not_signal_stale_or_forged_process_identity(
    tmp_path: Path,
    state_file: str,
    replacement: str,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    _write_process_identity(Path(harness["runtime"]))
    (Path(harness["process_state"]) / state_file).write_text(f"{replacement}\n", encoding="utf-8")

    _run_local_runtime(harness, "stop")

    assert not any(event.startswith("kill ") for event in _event_lines(harness))


def test_pid_reuse_after_term_blocks_followup_kill(tmp_path: Path) -> None:
    harness = _local_runtime_harness(tmp_path)
    _write_process_identity(Path(harness["runtime"]))

    _run_local_runtime(harness, "stop", term_behavior="reuse")

    events = _event_lines(harness)
    assert "kill -s TERM -4242" in events
    assert "kill -s KILL -4242" not in events
    _assert_identity_check_immediately_before(events, "kill -s TERM -4242")


def test_status_never_signals_a_stale_process(tmp_path: Path) -> None:
    harness = _local_runtime_harness(tmp_path)
    pid_file = _write_process_identity(Path(harness["runtime"]))
    (Path(harness["process_state"]) / "start_time").write_text(
        "Mon Aug  3 12:05:00 2026\n", encoding="utf-8"
    )

    result = _run_local_runtime(harness, "status")

    assert pid_file.exists()
    assert pid_file.with_suffix(".pid.identity").exists()
    assert "HealthMes: stopped" in result.stdout
    assert not any(event.startswith("kill ") for event in _event_lines(harness))


def test_status_preserves_stale_decision_launcher_metadata_as_unknown(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    pid_file = _write_process_identity(
        runtime,
        process_name="hermes-decision",
    )
    (Path(harness["process_state"]) / "start_time").write_text(
        "Mon Aug  3 12:05:00 2026\n",
        encoding="utf-8",
    )

    result = _run_local_runtime(harness, "status")

    assert (
        "Hermes decision runtime: unknown "
        "(launcher metadata remains without a v3 shutdown record)"
        in result.stdout
    )
    assert pid_file.exists()
    assert pid_file.with_suffix(".pid.identity").exists()
    assert not any(event.startswith("kill ") for event in _event_lines(harness))


def test_status_recovers_live_supervisor_when_wrapper_metadata_is_missing(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    budget = _write_decision_stop_budget(
        runtime,
        drain_timeout_seconds=2,
    )

    result = _run_local_runtime(harness, "status")

    assert (
        "Hermes decision runtime: running "
        "(verified supervisor pid 4343; wrapper metadata unavailable)"
        in result.stdout
    )
    assert budget.exists()
    assert not any(event.startswith("kill ") for event in _event_lines(harness))


def test_status_reports_dead_supervisor_cleanup_record_without_signalling(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    budget = _write_decision_stop_budget(
        runtime,
        drain_timeout_seconds=2,
    )
    (Path(harness["supervisor_state"]) / "alive").unlink()

    result = _run_local_runtime(harness, "status")

    assert (
        "Hermes decision runtime: stopped with incomplete cleanup record"
        in result.stdout
    )
    assert budget.exists()
    assert not any(event.startswith("kill ") for event in _event_lines(harness))


def test_start_cleanup_only_discards_untrusted_identity_files() -> None:
    text = LOCAL_SCRIPT.read_text(encoding="utf-8")
    start_body = _function_body(text, "start_process")

    assert "clear_process_identity" in start_body
    assert "signal_process_group" not in start_body
    assert "KILL_BIN" not in start_body


@pytest.mark.skipif(
    shutil.which("sleep") is None,
    reason="sleep is required",
)
def test_decision_start_ps_failure_preserves_live_generation_and_blocks_stop(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    local_script = Path(harness["local_script"])
    runtime = Path(harness["runtime"])
    fake_bin = Path(harness["env"]["PATH"].split(":", 1)[0])
    real_sleep = shutil.which("sleep")
    assert real_sleep is not None

    script = local_script.read_text(encoding="utf-8")
    test_case = textwrap.dedent(
        f"""
        case "${{1:-}}" in
        __test_start_decision)
            start_process "Hermes decision runtime" \
                "$HERMES_DECISION_PID" "$HERMES_DECISION_LOG" \
                "exec {real_sleep} 30" \
                "$HERMES_DECISION_STARTUP_LEASE"
            ;;
        """
    ).lstrip()
    local_script.write_text(
        script.replace('case "${1:-}" in\n', test_case, 1),
        encoding="utf-8",
    )
    failure_marker = tmp_path / "failed-parent-identity-ps"
    fail_once_ps = fake_bin / "fail-parent-identity-ps"
    _write_executable(
        fail_once_ps,
        """
        #!/usr/bin/env bash
        printf 'ps %s\\n' "$*" >>"$FAKE_EVENT_LOG"
        requested_pid=
        field=
        while [ "$#" -gt 0 ]; do
            if [ "$1" = "-p" ]; then
                shift
                requested_pid=${1:-}
            elif [ "$1" = "-o" ]; then
                shift
                field=${1:-}
            fi
            shift
        done
        if [ "$field" = "pid=" ] \
            && [ ! -f "$FAKE_PS_FAILURE_MARKER" ]; then
            : >"$FAKE_PS_FAILURE_MARKER"
            exit 1
        fi
        case "$field" in
        pid=) printf '%s\\n' "$requested_pid" ;;
        pgid=) printf '%s\\n' "$requested_pid" ;;
        comm=) printf '/bin/bash\\n' ;;
        lstart=) printf 'Mon Aug 17 12:00:00 2026\\n' ;;
        command=)
            printf '/bin/bash %s __service_runner abc123 test-command\\n' \
                "$FAKE_LOCAL_SCRIPT"
            ;;
        *) exit 1 ;;
        esac
        """,
    )
    launcher_pid: int | None = None
    env_overrides = {
        "HEALTHMES_PS_BIN": str(fail_once_ps),
        "HEALTHMES_KILL_BIN": "/bin/kill",
        "FAKE_PS_FAILURE_MARKER": str(failure_marker),
        "FAKE_LOCAL_SCRIPT": str(local_script),
    }
    try:
        result = _run_local_runtime(
            harness,
            "__test_start_decision",
            check=False,
            env_overrides=env_overrides,
        )
        pid_file = runtime / "hermes-decision.pid"
        identity_file = runtime / "hermes-decision.pid.identity"
        lease = runtime / "hermes-decision-startup-lease"
        record = lease / "record"

        assert result.returncode != 0
        assert "launcher identity is unknown" in result.stderr
        launcher_pid = int(pid_file.read_text(encoding="ascii"))
        os.kill(launcher_pid, 0)
        assert failure_marker.exists()
        assert not identity_file.exists()
        assert "state\tspawned\n" in record.read_text(encoding="ascii")
        assert f"launcher_pid\t{launcher_pid}\n" in record.read_text(
            encoding="ascii"
        )

        stop = _run_local_runtime(
            harness,
            "stop",
            check=False,
            env_overrides=env_overrides,
        )
        status = _run_local_runtime(
            harness,
            "status",
            env_overrides=env_overrides,
        )

        assert stop.returncode != 0
        assert "launcher identity is unverified" in stop.stderr
        assert pid_file.exists()
        assert lease.exists()
        assert (
            "Hermes decision runtime: unknown "
            "(startup launcher identity is unverified; "
            "PID tombstone and lease are preserved)"
            in status.stdout
        )
        assert not any(
            event.startswith("kill -s ")
            for event in _event_lines(harness)
        )
    finally:
        if launcher_pid is not None:
            try:
                process_group = os.getpgid(launcher_pid)
                if process_group != os.getpgrp():
                    os.killpg(process_group, signal.SIGTERM)
                else:
                    os.kill(launcher_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            for _ in range(100):
                try:
                    os.kill(launcher_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
