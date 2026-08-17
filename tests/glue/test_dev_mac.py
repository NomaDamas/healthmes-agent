"""Contract pins for scripts/dev_mac.sh (mac-native tooling).

Shell scripts get no import-time checking, so the invariants that protect
the read-only vendor tree are pinned here as text/syntax assertions.
"""

import plistlib
import re
import shutil
import subprocess
import textwrap
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
    publication_instance_nonce: str = "publication123",
    version: int = 2,
) -> Path:
    path = runtime_dir / "hermes-decision-stop-budget"
    path.write_text(
        "\n".join(
            (
                f"version\t{version}",
                f"drain_timeout_seconds\t{drain_timeout_seconds}",
                f"supervisor_pid\t{pid}",
                f"supervisor_start_token\tps:{start_time}",
                f"service_nonce\t{nonce}",
                "publication_instance_nonce\t"
                f"{publication_instance_nonce}",
                "",
            )
        ),
        encoding="ascii",
    )
    return path


def _local_runtime_harness(tmp_path: Path) -> dict[str, object]:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    runtime = repo / "data" / "runtime"
    home = tmp_path / "home"
    fake_bin = tmp_path / "fake-bin"
    process_state = tmp_path / "process-state"
    for directory in (scripts, runtime, home / "Library" / "LaunchAgents", fake_bin):
        directory.mkdir(parents=True, exist_ok=True)
    process_state.mkdir()

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
        fake_bin / "curl",
        """
        #!/usr/bin/env bash
        printf 'curl %s\\n' "$*" >>"$FAKE_EVENT_LOG"
        exit 1
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

    env = {
        "HOME": str(home),
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "FAKE_EVENT_LOG": str(event_log),
        "FAKE_PROCESS_STATE": str(process_state),
        "HEALTHMES_DEV_MAC_SCRIPT": str(dev_mac),
        "HEALTHMES_LAUNCHCTL_BIN": str(fake_bin / "launchctl"),
        "HEALTHMES_PS_BIN": str(fake_bin / "ps"),
        "HEALTHMES_KILL_BIN": str(fake_bin / "kill"),
        "HEALTHMES_SLEEP_BIN": str(fake_bin / "sleep"),
    }
    return {
        "env": env,
        "event_log": event_log,
        "local_script": local_script,
        "process_state": process_state,
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
    assert "healthmes.hermes_runtime_supervisor" in start_body
    assert "--shutdown-budget-path" in start_body
    assert '"$HERMES_DECISION_STOP_BUDGET"' in start_body
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
    assert 'service_status "Hermes decision runtime"' in status_body


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
    assert "kill -s TERM -4242" in events
    assert "kill -s KILL -4242" not in events
    assert events.count("sleep 1") == 5
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
    assert "kill -s TERM -4242" in events
    assert events.count("sleep 1") == 0
    assert not budget.exists()


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
    assert "ignoring stale or invalid decision runtime stop budget" in (
        result.stdout
    )
    assert _event_lines(harness).count("sleep 1") == 317
    assert budget.exists()


def test_decision_stop_treats_v1_budget_as_stale(
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
        version=1,
    )

    result = _run_local_runtime(
        harness,
        "stop",
        check=False,
    )

    assert result.returncode != 0
    assert "ignoring stale or invalid decision runtime stop budget" in (
        result.stdout
    )
    assert _event_lines(harness).count("sleep 1") == 317
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
    _write_process_identity(Path(harness["runtime"]), pid=pid)

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

    _run_local_runtime(harness, "status")

    assert pid_file.exists()
    assert pid_file.with_suffix(".pid.identity").exists()
    assert not any(event.startswith("kill ") for event in _event_lines(harness))


def test_start_cleanup_only_discards_untrusted_identity_files() -> None:
    text = LOCAL_SCRIPT.read_text(encoding="utf-8")
    start_body = _function_body(text, "start_process")

    assert "clear_process_identity" in start_body
    assert "signal_process_group" not in start_body
    assert "KILL_BIN" not in start_body
