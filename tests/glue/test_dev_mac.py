"""Contract pins for scripts/dev_mac.sh (mac-native tooling).

Shell scripts get no import-time checking, so the invariants that protect
the read-only vendor tree are pinned here as text/syntax assertions.
"""

import hashlib
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
import yaml

from healthmes.hermes_runtime_supervisor import (
    load_runtime_shutdown_budget,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "dev_mac.sh"
LOCAL_SCRIPT = REPO_ROOT / "scripts" / "healthmes_local.sh"
NATIVE_IDENTITY_HELPER = (
    REPO_ROOT / "scripts" / "runtime_native_identity.py"
)
LAUNCH_AGENT_TEMPLATE = REPO_ROOT / "config" / "com.healthmes.local.plist.in"
MAKEFILE = REPO_ROOT / "Makefile"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
DEVELOPMENT_DOC = REPO_ROOT / "docs" / "DEVELOPMENT.md"
README = REPO_ROOT / "README.md"
FAKE_MANAGED_PID = "2147483646"


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
    pid: str = FAKE_MANAGED_PID,
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
    pid: str = FAKE_MANAGED_PID,
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
    phase: str = "spawned",
    pid: str = FAKE_MANAGED_PID,
    nonce: str = "abc123",
    owner_pid: str = "999998",
    owner_start_token: str = "ps:Mon Aug 17 11:00:00 2026",
    owner_nonce: str = "owner123",
    created_at_epoch: int = 1,
    updated_at_epoch: int = 1,
    version: int = 2,
) -> Path:
    lease = runtime_dir / "hermes-decision-startup-lease"
    lease.mkdir(mode=0o700)
    if version == 1:
        state = "pending" if phase == "intent" else phase
        fields = (
            "version\t1",
            f"state\t{state}",
            f"launcher_service_nonce\t{nonce}",
        )
        if state == "spawned":
            fields += (f"launcher_pid\t{pid}",)
    else:
        fields = (
            "version\t2",
            f"phase\t{phase}",
            f"created_at_epoch\t{created_at_epoch}",
            f"updated_at_epoch\t{updated_at_epoch}",
            f"launcher_service_nonce\t{nonce}",
            f"startup_owner_pid\t{owner_pid}",
            f"startup_owner_start_token\t{owner_start_token}",
            f"startup_owner_nonce\t{owner_nonce}",
        )
        if phase in {"spawned", "identity_verified"} or (
            phase == "failed" and pid
        ):
            fields += (f"launcher_pid\t{pid}",)
        if phase == "identity_verified":
            fields += (
                "launcher_start_token\t"
                "ps:Mon Aug  3 12:00:00 2026",
            )
    (lease / "record").write_text(
        "\n".join((*fields, "")),
        encoding="ascii",
    )
    return lease


def _write_decision_lifecycle_lock(
    runtime_dir: Path,
    *,
    operation: str = "start",
    owner_pid: str = "999999",
    owner_start_token: str = "ps:Mon Aug 17 09:00:00 2026",
    owner_nonce: str = "lockowner123",
    acquired_at_epoch: int = 1,
    updated_at_epoch: int | None = None,
    phase: str = "acquired",
    script_sha256: str = "a" * 64,
    version: int = 1,
) -> Path:
    lock = runtime_dir / "hermes-decision-lifecycle-lock"
    lock.mkdir(mode=0o700)
    if version == 1:
        fields = (
            "version\t1",
            f"operation\t{operation}",
            f"owner_pid\t{owner_pid}",
            f"owner_start_token\t{owner_start_token}",
            f"owner_nonce\t{owner_nonce}",
            f"acquired_at_epoch\t{acquired_at_epoch}",
        )
    else:
        fields = (
            "version\t2",
            f"operation\t{operation}",
            f"phase\t{phase}",
            f"owner_pid\t{owner_pid}",
            f"owner_start_token\t{owner_start_token}",
            f"owner_nonce\t{owner_nonce}",
            f"acquired_at_epoch\t{acquired_at_epoch}",
            "updated_at_epoch\t"
            f"{updated_at_epoch or acquired_at_epoch}",
            "script_contract_version\t2",
            f"script_sha256\t{script_sha256}",
        )
    (lock / "record").write_text(
        "\n".join((*fields, "")),
        encoding="ascii",
    )
    return lock


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
    shutil.copy2(
        NATIVE_IDENTITY_HELPER,
        scripts / "runtime_native_identity.py",
    )
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
        if [ "$requested_pid" != "$current_pid" ]; then
            case " ${FAKE_ABSENT_PIDS:-} " in
            *" $requested_pid "*) exit 1 ;;
            esac
            case "$field" in
            pid) printf '%s\\n' "$requested_pid" ;;
            pgid) printf '%s\\n' "$requested_pid" ;;
            comm) printf '/bin/bash\\n' ;;
            lstart) printf 'Mon Aug 17 10:00:00 2026\\n' ;;
            command) printf '/bin/bash healthmes_local.sh test-owner\\n' ;;
            *) exit 1 ;;
            esac
            exit 0
        fi
        [ -f "$FAKE_PROCESS_STATE/alive" ] || exit 1
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
        if [ "${FAKE_SLEEP_BEHAVIOR:-}" = "publish-late-budget" ] \
            && [ ! -f "$FAKE_STOP_BUDGET" ]; then
            cp "$FAKE_LATE_STOP_BUDGET" "$FAKE_STOP_BUDGET"
        fi
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

    pid = FAKE_MANAGED_PID
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
        "FAKE_ABSENT_PIDS": "999998 999999",
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
        "HEALTHMES_NATIVE_IDENTITY_PYTHON_BIN": sys.executable,
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
    timeout: float | None = None,
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
        timeout=timeout,
    )


def _run_native_identity_helper(
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(NATIVE_IDENTITY_HELPER),
            *arguments,
        ],
        check=check,
        capture_output=True,
        text=True,
    )


def _event_lines(harness: dict[str, object]) -> list[str]:
    return Path(harness["event_log"]).read_text(encoding="utf-8").splitlines()


def _nonreturning_ps(tmp_path: Path) -> Path:
    path = tmp_path / "nonreturning-ps"
    _write_executable(
        path,
        """
        #!/usr/bin/env bash
        /bin/sleep 30
        """,
    )
    return path


def _install_generic_start_case(
    harness: dict[str, object],
    *,
    service_name: str,
    pid_variable: str,
    log_variable: str,
    command: str,
) -> None:
    local_script = Path(harness["local_script"])
    script = local_script.read_text(encoding="utf-8")
    test_case = textwrap.dedent(
        f"""
        case "${{1:-}}" in
        __test_start_generic)
            start_process "{service_name}" \
                "{pid_variable}" "{log_variable}" \
                "{command}"
            ;;
        __test_stop_generic)
            stop_process "{service_name}" "{pid_variable}"
            ;;
        """
    ).lstrip()
    local_script.write_text(
        script.replace('case "${1:-}" in\n', test_case, 1),
        encoding="utf-8",
    )


def _malformed_ps(tmp_path: Path) -> Path:
    path = tmp_path / "malformed-ps"
    _write_executable(
        path,
        """
        #!/usr/bin/env bash
        printf 'malformed\\trow\\n'
        """,
    )
    return path


def _oversized_ps(tmp_path: Path) -> Path:
    path = tmp_path / "oversized-ps"
    _write_executable(
        path,
        """
        #!/usr/bin/env bash
        head -c 131072 /dev/zero | tr '\\0' x
        """,
    )
    return path


def _assert_identity_check_immediately_before(events: list[str], signal_line: str) -> None:
    signal_index = events.index(signal_line)
    checks = events[signal_index - 5 : signal_index]
    assert len(checks) == 5
    for field in ("pid=", "pgid=", "comm=", "lstart=", "command="):
        assert any(event.startswith("ps ") and field in event for event in checks)


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="native lifecycle identity supports Linux and macOS",
)
def test_native_lifecycle_identity_is_stable_across_timezone_and_locale() -> None:
    tokens: set[str] = set()
    for timezone, locale in (
        ("UTC0", "C"),
        ("Asia/Seoul", "C.UTF-8"),
    ):
        result = subprocess.run(
            [
                sys.executable,
                str(NATIVE_IDENTITY_HELPER),
                "capture",
                str(os.getpid()),
            ],
            env={
                **os.environ,
                "TZ": timezone,
                "LANG": locale,
                "LC_ALL": locale,
            },
            check=True,
            capture_output=True,
            text=True,
        )
        tokens.add(result.stdout.strip())

    assert len(tokens) == 1
    token = tokens.pop()
    assert token.startswith(("linux:", "darwin:"))
    assert not token.startswith("ps:")


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="native atomic directory publication supports Linux and macOS",
)
def test_native_record_transitions_are_atomic_and_generation_checked(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    runtime = data / "runtime"
    runtime.mkdir(parents=True)
    transition_lock = data / ".runtime-transition.lock"
    canonical = runtime / "canonical"
    first_stage = runtime / ".stage-first"
    first_stage.mkdir()
    (first_stage / "record").write_text("first\n", encoding="ascii")

    _run_native_identity_helper(
        "rename-exclusive",
        str(first_stage),
        str(canonical),
        "--lock-path",
        str(transition_lock),
    )

    competing_stage = runtime / ".stage-competing"
    competing_stage.mkdir()
    (competing_stage / "record").write_text(
        "competing\n",
        encoding="ascii",
    )
    conflict = _run_native_identity_helper(
        "rename-exclusive",
        str(competing_stage),
        str(canonical),
        "--lock-path",
        str(transition_lock),
        check=False,
    )
    assert conflict.returncode == 6
    assert (canonical / "record").read_text(encoding="ascii") == "first\n"

    first_digest = hashlib.sha256(b"first\n").hexdigest()
    replacement = runtime / ".record-replacement"
    replacement.write_text("second\n", encoding="ascii")
    _run_native_identity_helper(
        "replace-record",
        str(replacement),
        str(canonical / "record"),
        "--lock-path",
        str(transition_lock),
        "--expected-record-sha256",
        first_digest,
    )
    assert (canonical / "record").read_text(encoding="ascii") == "second\n"

    stale_replacement = runtime / ".record-stale"
    stale_replacement.write_text("stale\n", encoding="ascii")
    stale_replace = _run_native_identity_helper(
        "replace-record",
        str(stale_replacement),
        str(canonical / "record"),
        "--lock-path",
        str(transition_lock),
        "--expected-record-sha256",
        first_digest,
        check=False,
    )
    assert stale_replace.returncode == 7
    assert (canonical / "record").read_text(encoding="ascii") == "second\n"

    retired = runtime / ".retired-generation"
    stale_retire = _run_native_identity_helper(
        "rename-exclusive",
        str(canonical),
        str(retired),
        "--lock-path",
        str(transition_lock),
        "--expected-record-sha256",
        first_digest,
        check=False,
    )
    assert stale_retire.returncode == 7
    assert canonical.is_dir()

    second_digest = hashlib.sha256(b"second\n").hexdigest()
    _run_native_identity_helper(
        "rename-exclusive",
        str(canonical),
        str(retired),
        "--lock-path",
        str(transition_lock),
        "--expected-record-sha256",
        second_digest,
    )
    assert not canonical.exists()
    assert (retired / "record").read_text(encoding="ascii") == "second\n"

    next_stage = runtime / ".stage-next"
    next_stage.mkdir()
    (next_stage / "record").write_text("next\n", encoding="ascii")
    _run_native_identity_helper(
        "rename-exclusive",
        str(next_stage),
        str(canonical),
        "--lock-path",
        str(transition_lock),
    )
    assert (canonical / "record").read_text(encoding="ascii") == "next\n"


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="native atomic directory publication supports Linux and macOS",
)
def test_native_concurrent_publishers_choose_one_complete_generation(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    runtime = data / "runtime"
    runtime.mkdir(parents=True)
    transition_lock = data / ".runtime-transition.lock"
    canonical = runtime / "canonical"
    stages: list[Path] = []
    processes: list[subprocess.Popen[str]] = []
    for index in range(2):
        stage = runtime / f".stage-{index}"
        stage.mkdir()
        (stage / "record").write_text(
            f"generation-{index}\n",
            encoding="ascii",
        )
        stages.append(stage)
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    str(NATIVE_IDENTITY_HELPER),
                    "rename-exclusive",
                    str(stage),
                    str(canonical),
                    "--lock-path",
                    str(transition_lock),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )

    results = [process.communicate(timeout=5) for process in processes]
    returncodes = sorted(process.returncode for process in processes)

    assert returncodes == [0, 6], results
    assert (canonical / "record").read_text(encoding="ascii") in {
        "generation-0\n",
        "generation-1\n",
    }
    assert sum(stage.exists() for stage in stages) == 1


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
    assert (
        "trap 'with_decision_runtime_lifecycle_lock stop stop_apps; "
        "exit 0' INT TERM"
    ) in daemon_body
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
    service_runner_body = _function_body(text, "run_service_runner")
    write_identity_body = _function_body(text, "write_process_identity")
    assert start_process_body.index(
        "create_decision_runtime_startup_lease"
    ) < start_process_body.index("nohup env")
    assert service_runner_body.index(
        "mark_decision_runtime_startup_spawned"
    )
    decision_publication = service_runner_body[
        service_runner_body.index(
            "mark_decision_runtime_startup_spawned"
        ) : service_runner_body.index(
            'elif [ -n "${HEALTHMES_GENERIC_STARTUP_PID_FILE:-}" ]'
        )
    ]
    assert "write_unverified_process_pid" not in decision_publication
    assert write_identity_body.index(
        'mv "$pid_temp" "$pid_file"'
    ) < write_identity_body.index('mv "$temp" "$file"')
    assert "wait_for_decision_runtime_launcher_publication" in (
        start_process_body
    )
    assert "preserving the startup lease" in start_process_body
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
    update_after_pull_body = _function_body(
        text,
        "cmd_update_after_pull",
    )
    start_body = _function_body(text, "start_apps")

    assert install_body.index("stop_decision_runtime") < install_body.index(
        '"$DEV_MAC_SCRIPT" setup'
    )
    assert update_body.index("stop_decision_runtime") < update_body.index(
        'git -C "$REPO_ROOT" pull --ff-only'
    )
    assert update_body.index("stop_decision_runtime") < update_body.index(
        'cmd_update_after_pull "$restart_launch_agent"'
    )
    assert '"$DEV_MAC_SCRIPT" setup' in update_after_pull_body
    assert start_body.index("stop_decision_runtime") < start_body.index(
        "uv sync --frozen --no-dev"
    )
    assert start_body.index("stop_decision_runtime") < start_body.index(
        "scripts/bootstrap.py"
    )


def test_decision_runtime_mutations_use_one_cross_process_lifecycle_lock() -> None:
    text = LOCAL_SCRIPT.read_text(encoding="utf-8")
    for command, operation in (
        ("install", "install"),
        ("update", "update"),
        ("start", "start"),
        ("stop", "stop"),
    ):
        assert re.search(
            rf"^{command}\) with_decision_runtime_lifecycle_lock "
            rf"{operation} cmd_{command} ;;$",
            text,
            re.MULTILINE,
        )
    assert re.search(
        r"^uninstall\) with_decision_runtime_lifecycle_lock "
        r'uninstall cmd_uninstall "\$\{2:-\}" ;;$',
        text,
        re.MULTILINE,
    )
    update_body = _function_body(text, "cmd_update")
    assert update_body.index("stop_decision_runtime") < update_body.index(
        'git -C "$REPO_ROOT" pull --ff-only'
    )
    lock_writer = _function_body(
        text,
        "write_decision_runtime_lifecycle_lock_record",
    )
    for field in (
        "operation",
        "phase",
        "owner_pid",
        "owner_start_token",
        "owner_nonce",
        "acquired_at_epoch",
        "updated_at_epoch",
        "script_contract_version",
        "script_sha256",
    ):
        assert f"printf '{field}\\t%s\\n'" in lock_writer
    assert "capture_native_process_start_token" in _function_body(
        text,
        "acquire_decision_runtime_lifecycle_lock",
    )
    lock_recovery = _function_body(
        text,
        "recover_orphaned_decision_runtime_lifecycle_lock",
    )
    assert "process_start_token_status" in lock_recovery
    assert "KILL_BIN" not in lock_recovery


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
        "serializes every decision-runtime mutation with the atomic "
        "directory lock `data/runtime/hermes-decision-lifecycle-lock/`"
    ) in development
    assert (
        "`update` holds the lock from the initial decision stop "
        "through `git pull`, setup, and generation handoff"
    ) in development
    assert (
        "a native OS start token (`/proc` start ticks on Linux or "
        "`libproc` start seconds/microseconds on macOS)"
    ) in development
    assert (
        "The token is independent of timezone and locale"
        in development
    )
    assert (
        "If the digest changed, it replaces itself with the newly "
        "pulled script by `exec` without changing its PID or releasing "
        "the lifecycle lock"
    ) in development
    assert (
        "validates the native owner start token, nonce, exact "
        "`pulling` journal generation, prior digest, and compatible "
        "lifecycle contract"
    ) in development
    assert (
        "A dead non-complete `update`, `install`, or `uninstall` "
        "journal is instead atomically advanced to `repair_required`"
    ) in development
    assert (
        "`uninstall` holds it across LaunchAgent unload, application "
        "stop, `services-stop`, and runtime/local-data cleanup"
    ) in development
    assert (
        "Durable subcommands run with Bash `errexit` active"
        in development
    )
    assert (
        "the transaction remains `repair_required`"
        in development
    )
    assert (
        "publishes the whole directory with an OS-native exclusive rename "
        "under the permanent transition mutex"
    ) in development
    assert (
        "Canonical lock directories are therefore never intentionally "
        "visible without a complete record"
    ) in development
    assert (
        "Removal similarly verifies the record digest and atomically retires "
        "the whole directory"
    ) in development
    assert (
        "An ownerless empty directory contains no identity evidence, so it "
        "remains `unknown`"
    ) in development
    assert (
        "The transition mutex is intentionally retained across uninstall"
    ) in development
    assert (
        "start atomically creates the version-2 "
        "`data/runtime/hermes-decision-startup-lease/` before spawning"
    ) in development
    assert (
        "That lease record is the only initial Decision Runtime publication"
    ) in development
    assert "Before publishing the numeric PID" in development
    assert "must be at least 2" in development
    assert (
        "One failed or unreadable identity query is unknown, not evidence "
        "that the process is absent"
    ) in development
    assert (
        "stop/update cannot report success or delete metadata while a "
        "startup generation remains unverified"
    ) in development
    assert (
        "`status` reads lifecycle lock, startup lease, v3 budget, and "
        "launcher metadata before reporting liveness"
    ) in development
    assert (
        "Generation conflicts win over a live launcher and are reported "
        "as `unknown`"
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
        "An existing malformed record is preserved byte-for-byte"
        in development
    )
    assert (
        "a failed, timed-out, or empty `ps` probe is unknown while the "
        "numeric PID still exists"
    ) in development
    assert (
        "A formatted token mismatch for a still-live numeric PID is also "
        "unknown"
    ) in development
    assert (
        "Replacement is allowed only after numeric process absence is "
        "positively proved"
    ) in development
    assert (
        "Every configured `PS_BIN` invocation used by lifecycle acquisition "
        "or startup recovery runs in a separate process group"
    ) in development
    assert (
        "records larger than 1 KiB fail closed"
        in development
    )
    assert (
        "two consecutive, independently enumerated empty `/proc` group "
        "observations"
    ) in development
    assert (
        "the supervisor first reaps that exact subprocess handle"
        in development
    )
    assert (
        "unreadable `/proc` process records, malformed Darwin process "
        "listings, and unprovable identities fail closed"
    ) in development


def test_uninstall_keeps_data_unless_delete_data_is_explicit() -> None:
    text = LOCAL_SCRIPT.read_text(encoding="utf-8")
    body = _function_body(text, "cmd_uninstall")
    assert '[ "$delete_data" = "--delete-data" ]' in body
    assert "remove_runtime_contents_except_lifecycle_lock" in body
    assert "remove_data_contents_except_runtime" in body
    assert body.index("uninstall_launch_agent") < body.index("stop_apps")
    assert body.index("stop_apps") < body.index("services-stop")
    assert body.index("services-stop") < body.index(
        "remove_runtime_contents_except_lifecycle_lock"
    )


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
    term = events.index(f"kill -s TERM -{FAKE_MANAGED_PID}")
    hard_kill = events.index(f"kill -s KILL -{FAKE_MANAGED_PID}")
    assert disable < bootout < term < hard_kill
    _assert_identity_check_immediately_before(
        events,
        f"kill -s TERM -{FAKE_MANAGED_PID}",
    )
    _assert_identity_check_immediately_before(
        events,
        f"kill -s KILL -{FAKE_MANAGED_PID}",
    )
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
    assert f"kill -s TERM -{FAKE_MANAGED_PID}" in _event_lines(harness)


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
    assert f"kill -s TERM -{FAKE_MANAGED_PID}" in events
    assert any(
        f"--runtime-process-group-pgid {FAKE_MANAGED_PID}" in event
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


@pytest.mark.parametrize(
    "path_kind",
    ("symlink", "fifo", "hardlink", "oversized"),
)
def test_status_rejects_unsafe_shutdown_budget_paths(
    tmp_path: Path,
    path_kind: str,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    budget = runtime / "hermes-decision-stop-budget"
    source = tmp_path / "unsafe-budget-source"
    if path_kind == "symlink":
        source.write_bytes(b"version\t3\n")
        budget.symlink_to(source)
    elif path_kind == "fifo":
        os.mkfifo(budget)
    elif path_kind == "hardlink":
        source.write_bytes(b"version\t3\n")
        os.link(source, budget)
    else:
        budget.write_bytes(b"x" * 1025)

    result = _run_local_runtime(
        harness,
        "status",
        timeout=4,
    )

    assert (
        "Hermes decision runtime: unknown "
        "(shutdown budget is malformed or unsafe)"
        in result.stdout
    )


@pytest.mark.parametrize(
    "mutation",
    (
        pytest.param(lambda payload: payload + b"\n", id="trailing-blank-line"),
        pytest.param(
            lambda payload: payload.replace(
                b"drain_timeout_seconds\t2\n",
                b"drain_timeout_seconds\t2\n"
                b"drain_timeout_seconds\t2\n",
            ),
            id="duplicate-field",
        ),
        pytest.param(
            lambda payload: payload.replace(
                f"launcher_pid\t{FAKE_MANAGED_PID}\n".encode(),
                f"\nlauncher_pid\t{FAKE_MANAGED_PID}\n".encode(),
            ),
            id="embedded-blank-line",
        ),
        pytest.param(
            lambda payload: payload.replace(b"\n", b"\r\n"),
            id="crlf",
        ),
        pytest.param(
            lambda payload: payload.replace(
                f"launcher_pid\t{FAKE_MANAGED_PID}\n".encode(),
                b"launcher_pid\t1\n",
            ),
            id="launcher-pid-one",
        ),
        pytest.param(
            lambda payload: payload.replace(
                b"supervisor_pid\t4343\n",
                b"supervisor_pid\t1\n",
            ),
            id="supervisor-pid-one",
        ),
    ),
)
def test_shutdown_budget_malformed_corpus_has_python_shell_parity(
    tmp_path: Path,
    mutation,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    budget = _write_decision_stop_budget(
        runtime,
        drain_timeout_seconds=2,
    )
    budget.write_bytes(mutation(budget.read_bytes()))

    with pytest.raises(ValueError, match="shutdown budget is invalid"):
        load_runtime_shutdown_budget(budget)

    native = _run_native_identity_helper(
        "read-shutdown-budget",
        str(budget),
        "--max-bytes",
        "1024",
        "--max-drain-seconds",
        "315",
        check=False,
    )
    result = _run_local_runtime(harness, "status", timeout=4)

    assert native.returncode != 0
    assert (
        "Hermes decision runtime: unknown "
        "(shutdown budget is malformed or unsafe)"
        in result.stdout
    )


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
    assert f"kill -s TERM -{FAKE_MANAGED_PID}" in _event_lines(harness)
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
    pid_file.write_text(f"{FAKE_MANAGED_PID}\n", encoding="ascii")
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
    fake_bin = Path(harness["env"]["PATH"].split(":", 1)[0])
    sleeper = subprocess.Popen(
        ["/bin/sleep", "30"],
        start_new_session=True,
    )
    launcher_pid = str(sleeper.pid)
    pid_file = runtime / "hermes-decision.pid"
    pid_file.write_text(f"{launcher_pid}\n", encoding="ascii")
    lease = _write_decision_startup_lease(
        runtime,
        pid=launcher_pid,
    )
    unknown_launcher_ps = fake_bin / "unknown-launcher-ps"
    _write_executable(
        unknown_launcher_ps,
        """
        #!/usr/bin/env bash
        printf 'ps %s\\n' "$*" >>"$FAKE_EVENT_LOG"
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
        if [ "$requested_pid" = "$FAKE_UNKNOWN_LAUNCHER_PID" ]; then
            printf 'permission denied\\n' >&2
            exit 2
        fi
        case " ${FAKE_ABSENT_PIDS:-} " in
        *" $requested_pid "*) exit 1 ;;
        esac
        case "$field" in
        pid) printf '%s\\n' "$requested_pid" ;;
        lstart) printf 'Mon Aug 17 10:00:00 2026\\n' ;;
        *) exit 1 ;;
        esac
        """,
    )

    env_overrides = {
        "HEALTHMES_PS_BIN": str(unknown_launcher_ps),
        "FAKE_UNKNOWN_LAUNCHER_PID": launcher_pid,
    }
    try:
        result = _run_local_runtime(
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

        assert result.returncode != 0
        assert "startup launcher identity is unknown" in result.stderr
        assert (
            "Hermes decision runtime: unknown "
            "(startup launcher identity is unverified; "
            "startup lease is preserved)"
            in status.stdout
        )
        assert pid_file.exists()
        assert lease.exists()
        assert not any(
            event.startswith("kill -s ")
            for event in _event_lines(harness)
        )
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)


def test_pending_decision_startup_intent_blocks_stop_and_reports_starting(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    lease = _write_decision_startup_lease(
        Path(harness["runtime"]),
        phase="intent",
        version=1,
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
    pid_file.write_text(f"{FAKE_MANAGED_PID}\n", encoding="ascii")
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


def test_stop_cleans_stale_intent_after_verified_startup_owner_exit(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    lease = _write_decision_startup_lease(
        runtime,
        phase="intent",
        pid="",
    )
    (Path(harness["process_state"]) / "alive").unlink()
    (Path(harness["supervisor_state"]) / "alive").unlink()

    result = _run_local_runtime(harness, "stop")

    assert "recovered stale decision runtime startup intent" in result.stdout
    assert not lease.exists()
    assert not any(
        event.startswith("kill ")
        for event in _event_lines(harness)
    )


@pytest.mark.parametrize("phase", ("spawned", "failed"))
def test_stop_cleans_crash_after_launcher_pid_phase_when_group_is_empty(
    tmp_path: Path,
    phase: str,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    lease = _write_decision_startup_lease(runtime, phase=phase)
    (Path(harness["process_state"]) / "alive").unlink()
    (Path(harness["supervisor_state"]) / "alive").unlink()

    result = _run_local_runtime(harness, "stop")

    assert "proving its launcher group empty" in result.stdout
    assert not lease.exists()
    assert not (runtime / "hermes-decision.pid").exists()
    assert not any(
        event.startswith("kill ")
        for event in _event_lines(harness)
    )


def test_stop_recovers_late_matching_v3_budget_without_pid_tombstone(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    now = int(time.time())
    lease = _write_decision_startup_lease(
        runtime,
        phase="spawned",
        created_at_epoch=now,
        updated_at_epoch=now,
    )
    _write_decision_stop_budget(
        Path(harness["process_state"]),
        drain_timeout_seconds=2,
        filename="late-stop-budget",
    )

    _run_local_runtime(
        harness,
        "stop",
        term_behavior="exit",
        env_overrides={"FAKE_SLEEP_BEHAVIOR": "publish-late-budget"},
    )

    events = _event_lines(harness)
    assert any(
        "--runtime-process-action signal" in event
        for event in events
        if event.startswith("runtime-python ")
    )
    assert not any(event.startswith("kill -s ") for event in events)
    assert not lease.exists()
    assert not (runtime / "hermes-decision-stop-budget").exists()


def test_late_v3_budget_with_mismatched_generation_fails_closed(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    now = int(time.time())
    lease = _write_decision_startup_lease(
        runtime,
        phase="spawned",
        created_at_epoch=now,
        updated_at_epoch=now,
    )
    late_budget = _write_decision_stop_budget(
        Path(harness["process_state"]),
        drain_timeout_seconds=2,
        nonce="other-generation",
        filename="late-stop-budget",
    )

    result = _run_local_runtime(
        harness,
        "stop",
        check=False,
        env_overrides={"FAKE_SLEEP_BEHAVIOR": "publish-late-budget"},
    )

    assert result.returncode != 0
    assert "does not match the startup lease generation" in result.stderr
    assert lease.exists()
    assert late_budget.exists()
    assert (runtime / "hermes-decision-stop-budget").exists()
    assert not any(
        "--runtime-process-action signal" in event
        for event in _event_lines(harness)
        if event.startswith("runtime-python ")
    )
    assert not any(
        event.startswith("kill -s ")
        for event in _event_lines(harness)
    )


def test_stop_finishes_identity_verified_phase_after_parent_crash(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    lease = _write_decision_startup_lease(
        runtime,
        phase="identity_verified",
    )
    _write_process_identity(
        runtime,
        process_name="hermes-decision",
    )

    _run_local_runtime(harness, "stop", term_behavior="exit")

    assert not lease.exists()
    assert not (runtime / "hermes-decision.pid").exists()
    assert not (runtime / "hermes-decision.pid.identity").exists()


def test_orphaned_lifecycle_lock_is_recovered_without_numeric_signal(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    lock = _write_decision_lifecycle_lock(Path(harness["runtime"]))

    result = _run_local_runtime(harness, "stop")

    assert "recovered stale decision runtime lifecycle lock" in result.stdout
    assert not lock.exists()
    assert not any(
        event.startswith("kill ")
        for event in _event_lines(harness)
    )


def test_unknown_lifecycle_lock_owner_fails_closed_without_signal(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    fake_bin = Path(harness["env"]["PATH"].split(":", 1)[0])
    owner_pid = str(os.getpid())
    lock = _write_decision_lifecycle_lock(
        runtime,
        owner_pid=owner_pid,
    )
    unknown_owner_ps = fake_bin / "unknown-lock-owner-ps"
    _write_executable(
        unknown_owner_ps,
        """
        #!/usr/bin/env bash
        printf 'ps %s\\n' "$*" >>"$FAKE_EVENT_LOG"
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
        if [ "$requested_pid" = "$FAKE_UNKNOWN_OWNER_PID" ]; then
            printf 'permission denied\\n' >&2
            exit 2
        fi
        case "$field" in
        pid) printf '%s\\n' "$requested_pid" ;;
        lstart) printf 'Mon Aug 17 10:00:00 2026\\n' ;;
        *) exit 1 ;;
        esac
        """,
    )

    result = _run_local_runtime(
        harness,
        "stop",
        check=False,
        env_overrides={
            "HEALTHMES_PS_BIN": str(unknown_owner_ps),
            "FAKE_UNKNOWN_OWNER_PID": owner_pid,
        },
    )

    assert result.returncode != 0
    assert "lifecycle lock owner identity is unknown" in result.stderr
    assert lock.exists()
    assert not any(
        event.startswith("kill ")
        for event in _event_lines(harness)
    )


def test_nonreturning_ps_cannot_exceed_lifecycle_lock_budget(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    lock = _write_decision_lifecycle_lock(
        Path(harness["runtime"]),
        owner_pid=str(os.getpid()),
        owner_start_token="ps:Mon Aug 17 10:00:00 2026",
    )
    started = time.monotonic()

    result = _run_local_runtime(
        harness,
        "stop",
        check=False,
        env_overrides={
            "HEALTHMES_PS_BIN": str(_nonreturning_ps(tmp_path)),
        },
        timeout=4,
    )

    assert time.monotonic() - started < 3
    assert result.returncode != 0
    assert "owner identity is unknown" in result.stderr
    assert lock.exists()


@pytest.mark.parametrize("ps_behavior", ("silent-failure", "hang"))
def test_legacy_lifecycle_lock_preserves_live_owner_when_ps_is_unavailable(
    tmp_path: Path,
    ps_behavior: str,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    lock = _write_decision_lifecycle_lock(
        Path(harness["runtime"]),
        owner_pid=str(os.getpid()),
        owner_start_token="ps:Mon Aug 17 10:00:00 2026",
    )
    ps_bin = (
        Path("/usr/bin/false")
        if ps_behavior == "silent-failure"
        else _nonreturning_ps(tmp_path)
    )

    result = _run_local_runtime(
        harness,
        "stop",
        check=False,
        env_overrides={"HEALTHMES_PS_BIN": str(ps_bin)},
        timeout=4,
    )

    assert result.returncode != 0
    assert "lifecycle lock owner identity is unknown" in result.stderr
    assert lock.exists()
    assert not any(
        event.startswith("kill ")
        for event in _event_lines(harness)
    )


@pytest.mark.parametrize(
    "ps_behavior",
    ("silent-failure", "hang", "malformed"),
)
def test_stop_preserves_live_process_metadata_when_ps_is_unknown(
    tmp_path: Path,
    ps_behavior: str,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    sleeper = subprocess.Popen(
        ["/bin/sleep", "30"],
        start_new_session=True,
    )
    pid_file = _write_process_identity(
        runtime,
        pid=str(sleeper.pid),
    )
    if ps_behavior == "silent-failure":
        ps_bin = Path("/usr/bin/false")
    elif ps_behavior == "hang":
        ps_bin = _nonreturning_ps(tmp_path)
    else:
        ps_bin = _malformed_ps(tmp_path)

    try:
        result = _run_local_runtime(
            harness,
            "stop",
            check=False,
            env_overrides={"HEALTHMES_PS_BIN": str(ps_bin)},
            timeout=8,
        )

        assert result.returncode != 0
        assert "identity is unknown" in result.stderr
        assert pid_file.exists()
        assert pid_file.with_suffix(".pid.identity").exists()
        os.kill(sleeper.pid, 0)
        assert not any(
            event.startswith("kill ")
            for event in _event_lines(harness)
        )
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)


def test_native_ps_probe_rejects_oversized_output(tmp_path: Path) -> None:
    result = _run_native_identity_helper(
        "ps-value",
        "--ps-bin",
        str(_oversized_ps(tmp_path)),
        "--pid",
        str(os.getpid()),
        "--field",
        "command",
        "--timeout-seconds",
        "2",
        check=False,
    )

    assert result.returncode == 5
    assert result.stdout == ""
    assert "native_identity_ps_output_oversized" in result.stderr


def test_incomplete_lifecycle_lock_fails_closed_and_is_preserved(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    lock = Path(harness["runtime"]) / "hermes-decision-lifecycle-lock"
    lock.mkdir(mode=0o700)

    result = _run_local_runtime(harness, "stop", check=False)
    status = _run_local_runtime(harness, "status")

    assert result.returncode != 0
    assert (
        "lifecycle lock is malformed or has no provable owner"
        in result.stderr
    )
    assert (
        "Hermes decision runtime: unknown "
        "(lifecycle lock is malformed or has no provable owner)"
        in status.stdout
    )
    assert lock.exists()
    assert not any(
        event.startswith("kill ")
        for event in _event_lines(harness)
    )


def test_abandoned_transition_artifacts_do_not_block_new_generations(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    artifacts = (
        runtime / ".lifecycle-lock-stage.interrupted",
        runtime / ".lifecycle-lock-retired.interrupted",
        runtime / ".startup-lease-stage.interrupted",
        runtime / ".startup-lease-retired.interrupted",
    )
    for artifact in artifacts:
        artifact.mkdir()
        (artifact / "record").write_text(
            "non-authoritative\n",
            encoding="ascii",
        )

    result = _run_local_runtime(harness, "stop")

    assert result.returncode == 0
    assert all(artifact.exists() for artifact in artifacts)
    assert not (
        runtime / "hermes-decision-lifecycle-lock"
    ).exists()
    assert not (
        runtime / "hermes-decision-startup-lease"
    ).exists()


@pytest.mark.parametrize("orphan_location", ("internal-temp", "external-backup"))
def test_interrupted_lifecycle_record_is_restored_then_recovered(
    tmp_path: Path,
    orphan_location: str,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    lock = _write_decision_lifecycle_lock(runtime)
    orphan = (
        lock / ".record.interrupted"
        if orphan_location == "internal-temp"
        else runtime / ".lifecycle-lock-record.interrupted"
    )
    (lock / "record").replace(orphan)

    result = _run_local_runtime(harness, "stop")

    assert "restored an interrupted decision runtime lifecycle owner record" in (
        result.stdout
    )
    assert "recovered stale decision runtime lifecycle lock" in result.stdout
    assert not lock.exists()
    assert not orphan.exists()


def test_interrupted_lifecycle_record_with_live_owner_is_preserved(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    lock = _write_decision_lifecycle_lock(
        runtime,
        owner_pid=FAKE_MANAGED_PID,
        owner_start_token="ps:Mon Aug  3 12:00:00 2026",
    )
    orphan = runtime / ".lifecycle-lock-record.interrupted"
    (lock / "record").replace(orphan)

    result = _run_local_runtime(harness, "stop", check=False)

    assert result.returncode != 0
    assert "publication is still owned by a live process" in result.stderr
    assert lock.is_dir()
    assert not (lock / "record").exists()
    assert orphan.exists()
    assert not any(
        event.startswith("kill ")
        for event in _event_lines(harness)
    )


def test_interrupted_lifecycle_record_with_unknown_owner_fails_closed(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    lock = _write_decision_lifecycle_lock(
        runtime,
        owner_pid="888888",
    )
    orphan = runtime / ".lifecycle-lock-record.interrupted"
    (lock / "record").replace(orphan)

    result = _run_local_runtime(
        harness,
        "stop",
        check=False,
        env_overrides={
            "HEALTHMES_PS_BIN": str(tmp_path / "missing-ps"),
        },
    )

    assert result.returncode != 0
    assert "orphan owner identity is unknown" in result.stderr
    assert lock.is_dir()
    assert not (lock / "record").exists()
    assert orphan.exists()
    assert not any(
        event.startswith("kill ")
        for event in _event_lines(harness)
    )


@pytest.mark.parametrize("orphan_location", ("internal-temp", "external-backup"))
def test_interrupted_startup_lease_record_is_restored_then_recovered(
    tmp_path: Path,
    orphan_location: str,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    lease = _write_decision_startup_lease(
        runtime,
        phase="intent",
    )
    orphan = (
        lease / ".record.interrupted"
        if orphan_location == "internal-temp"
        else runtime / ".startup-lease-record.interrupted"
    )
    (lease / "record").replace(orphan)

    result = _run_local_runtime(harness, "stop")

    assert "restored an interrupted decision runtime startup lease record" in (
        result.stdout
    )
    assert "recovered stale decision runtime startup intent" in result.stdout
    assert not lease.exists()
    assert not orphan.exists()


def test_interrupted_startup_lease_record_with_unknown_owner_fails_closed(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    lease = _write_decision_startup_lease(
        runtime,
        phase="intent",
        owner_pid="888888",
    )
    orphan = runtime / ".startup-lease-record.interrupted"
    (lease / "record").replace(orphan)

    result = _run_local_runtime(
        harness,
        "stop",
        check=False,
        env_overrides={
            "HEALTHMES_PS_BIN": str(tmp_path / "missing-ps"),
        },
    )

    assert result.returncode != 0
    assert "orphan owner identity is unknown" in result.stderr
    assert lease.is_dir()
    assert not (lease / "record").exists()
    assert orphan.exists()


def test_interrupted_startup_lease_record_with_live_owner_is_preserved(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    lease = _write_decision_startup_lease(
        runtime,
        phase="intent",
        owner_start_token="ps:Mon Aug 17 10:00:00 2026",
    )
    orphan = runtime / ".startup-lease-record.interrupted"
    (lease / "record").replace(orphan)

    result = _run_local_runtime(
        harness,
        "stop",
        check=False,
        env_overrides={"FAKE_ABSENT_PIDS": "999999"},
    )

    assert result.returncode != 0
    assert "publication is still owned by a live process" in result.stderr
    assert lease.is_dir()
    assert not (lease / "record").exists()
    assert orphan.exists()
    assert not any(
        event.startswith("kill ")
        for event in _event_lines(harness)
    )


def test_nonreturning_ps_cannot_exceed_startup_recovery_budget(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    lease = _write_decision_startup_lease(
        Path(harness["runtime"]),
        phase="intent",
        owner_pid=str(os.getpid()),
        owner_start_token="ps:Mon Aug 17 10:00:00 2026",
    )
    started = time.monotonic()

    result = _run_local_runtime(
        harness,
        "stop",
        check=False,
        env_overrides={
            "HEALTHMES_PS_BIN": str(_nonreturning_ps(tmp_path)),
        },
        timeout=4,
    )

    assert time.monotonic() - started < 3
    assert result.returncode != 0
    assert "owner identity is unknown" in result.stderr
    assert lease.exists()


@pytest.mark.parametrize("record_kind", ("lifecycle", "startup"))
def test_multiple_interrupted_record_candidates_fail_closed(
    tmp_path: Path,
    record_kind: str,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    if record_kind == "lifecycle":
        directory = _write_decision_lifecycle_lock(runtime)
        external = runtime / ".lifecycle-lock-record.interrupted"
    else:
        directory = _write_decision_startup_lease(
            runtime,
            phase="intent",
        )
        external = runtime / ".startup-lease-record.interrupted"
    internal = directory / ".record.interrupted"
    (directory / "record").replace(internal)
    external.write_bytes(internal.read_bytes())

    result = _run_local_runtime(harness, "stop", check=False)

    assert result.returncode != 0
    assert "malformed or has no provable owner" in result.stderr \
        or "startup lease is invalid" in result.stderr
    assert directory.is_dir()
    assert not (directory / "record").exists()
    assert internal.exists()
    assert external.exists()


def test_empty_legacy_startup_lease_remains_unknown_and_preserved(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    lease = (
        Path(harness["runtime"])
        / "hermes-decision-startup-lease"
    )
    lease.mkdir(mode=0o700)

    result = _run_local_runtime(harness, "stop", check=False)

    assert result.returncode != 0
    assert "startup lease is invalid" in result.stderr
    assert lease.is_dir()
    assert not (lease / "record").exists()


def test_live_lifecycle_lock_times_out_without_mutation(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    lock = _write_decision_lifecycle_lock(
        Path(harness["runtime"]),
        owner_start_token="ps:Mon Aug 17 10:00:00 2026",
    )

    result = _run_local_runtime(
        harness,
        "stop",
        check=False,
        env_overrides={"FAKE_ABSENT_PIDS": "999998"},
    )

    assert result.returncode != 0
    assert (
        "lifecycle lock is still owned by live start pid 999999"
        in result.stderr
    )
    assert "timed out without mutating runtime state" in result.stderr
    assert lock.exists()
    assert not any(
        event.startswith("kill ")
        for event in _event_lines(harness)
    )


def test_status_reports_generation_conflict_before_live_launcher(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    _write_process_identity(
        runtime,
        process_name="hermes-decision",
    )
    _write_decision_startup_lease(
        runtime,
        nonce="other-generation",
    )
    _write_decision_stop_budget(
        runtime,
        drain_timeout_seconds=2,
    )

    result = _run_local_runtime(harness, "status")

    assert "generations do not match" in result.stdout
    assert "Hermes decision runtime: running" not in result.stdout


@pytest.mark.parametrize(
    ("holder_command", "contender_command"),
    (
        ("start", "stop"),
        ("update", "start"),
        ("uninstall", "start"),
    ),
)
def test_lifecycle_lock_serializes_concurrent_mutations(
    tmp_path: Path,
    holder_command: str,
    contender_command: str,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    local_script = Path(harness["local_script"])
    marker_dir = tmp_path / "lifecycle-markers"
    marker_dir.mkdir()
    release = marker_dir / "release"
    script = local_script.read_text(encoding="utf-8")
    overrides = textwrap.dedent(
        """
        fake_lifecycle_command() {
            local command=$1
            if [ "${FAKE_HOLD_COMMAND:-}" = "$command" ]; then
                : >"$FAKE_MARKER_DIR/holder-entered"
                while [ ! -f "$FAKE_MARKER_DIR/release" ]; do
                    /bin/sleep 0.05
                done
            fi
            : >"$FAKE_MARKER_DIR/entered-$command"
        }
        cmd_start() { fake_lifecycle_command start; }
        cmd_stop() { fake_lifecycle_command stop; }
        cmd_update() { fake_lifecycle_command update; }
        cmd_uninstall() { fake_lifecycle_command uninstall; }
        """
    ).lstrip()
    local_script.write_text(
        script.replace('case "${1:-}" in\n', f"{overrides}\ncase \"${{1:-}}\" in\n", 1),
        encoding="utf-8",
    )
    base_env = {
        **harness["env"],
        "FAKE_MARKER_DIR": str(marker_dir),
        "HEALTHMES_SLEEP_BIN": "/bin/sleep",
    }
    holder_env = {
        **base_env,
        "FAKE_HOLD_COMMAND": holder_command,
    }
    holder = subprocess.Popen(
        ["bash", str(local_script), holder_command],
        env=holder_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    contender: subprocess.Popen[str] | None = None
    try:
        for _ in range(100):
            if (marker_dir / "holder-entered").exists():
                break
            time.sleep(0.01)
        assert (marker_dir / "holder-entered").exists()

        contender = subprocess.Popen(
            ["bash", str(local_script), contender_command],
            env=base_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.2)
        assert not (marker_dir / f"entered-{contender_command}").exists()

        release.touch()
        holder_stdout, holder_stderr = holder.communicate(timeout=5)
        contender_stdout, contender_stderr = contender.communicate(timeout=5)
        assert holder.returncode == 0, (holder_stdout, holder_stderr)
        assert contender.returncode == 0, (
            contender_stdout,
            contender_stderr,
        )
        assert (marker_dir / f"entered-{contender_command}").exists()
    finally:
        release.touch(exist_ok=True)
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=5)
        if contender is not None and contender.poll() is None:
            contender.terminate()
            contender.wait(timeout=5)


@pytest.mark.parametrize("operation", ("update", "install", "uninstall"))
def test_orphaned_durable_lifecycle_transaction_requires_repair(
    tmp_path: Path,
    operation: str,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    native_token = (
        "darwin:1786915200:123456"
        if sys.platform == "darwin"
        else "linux:123456"
    )
    lock = _write_decision_lifecycle_lock(
        runtime,
        operation=operation,
        owner_start_token=native_token,
        version=2,
    )

    result = _run_local_runtime(harness, "stop", check=False)
    status = _run_local_runtime(harness, "status")

    assert result.returncode != 0
    assert "requires explicit repair" in result.stderr
    assert lock.exists()
    record = (lock / "record").read_text(encoding="ascii")
    assert f"operation\t{operation}\n" in record
    assert "phase\trepair_required\n" in record
    assert (
        f"lifecycle {operation} transaction requires explicit repair"
        in status.stdout
    )
    assert not any(
        event.startswith("kill ")
        for event in _event_lines(harness)
    )


def test_failed_update_after_mutation_preserves_repair_journal(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    local_script = Path(harness["local_script"])
    script = local_script.read_text(encoding="utf-8")
    override = textwrap.dedent(
        """
        cmd_update() {
            set_decision_runtime_lifecycle_phase preflight
            DECISION_RUNTIME_DURABLE_MUTATION_STARTED=true
            set_decision_runtime_lifecycle_phase pulling
            return 23
        }
        """
    ).lstrip()
    local_script.write_text(
        script.replace(
            'case "${1:-}" in\n',
            f"{override}\ncase \"${{1:-}}\" in\n",
            1,
        ),
        encoding="utf-8",
    )

    result = _run_local_runtime(harness, "update", check=False)
    blocked = _run_local_runtime(harness, "start", check=False)

    lock = (
        Path(harness["runtime"])
        / "hermes-decision-lifecycle-lock"
    )
    assert result.returncode == 23
    assert lock.exists()
    assert "phase\trepair_required\n" in (
        lock / "record"
    ).read_text(encoding="ascii")
    assert blocked.returncode != 0
    assert "requires explicit repair" in blocked.stderr


@pytest.mark.parametrize("operation", ("update", "install", "uninstall"))
def test_durable_lifecycle_does_not_mask_internal_command_failure(
    tmp_path: Path,
    operation: str,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    local_script = Path(harness["local_script"])
    marker = tmp_path / f"{operation}-continued-after-failure"
    script = local_script.read_text(encoding="utf-8")
    override = textwrap.dedent(
        f"""
        cmd_{operation}() {{
            set_decision_runtime_lifecycle_phase preflight
            DECISION_RUNTIME_DURABLE_MUTATION_STARTED=true
            set_decision_runtime_lifecycle_phase setup
            false
            : >"{marker}"
        }}
        """
    ).lstrip()
    local_script.write_text(
        script.replace(
            'case "${1:-}" in\n',
            f"{override}\ncase \"${{1:-}}\" in\n",
            1,
        ),
        encoding="utf-8",
    )

    result = _run_local_runtime(harness, operation, check=False)

    lock = (
        Path(harness["runtime"])
        / "hermes-decision-lifecycle-lock"
    )
    assert result.returncode != 0
    assert not marker.exists()
    assert lock.exists()
    assert "phase\trepair_required\n" in (
        lock / "record"
    ).read_text(encoding="ascii")


def test_update_reexecutes_new_script_with_same_lifecycle_owner(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    local_script = Path(harness["local_script"])
    marker_dir = tmp_path / "update-handoff-markers"
    marker_dir.mkdir()
    updated_script = tmp_path / "updated-healthmes_local.sh"
    fake_bin = Path(harness["env"]["PATH"].split(":", 1)[0])
    script = local_script.read_text(encoding="utf-8")
    old_overrides = textwrap.dedent(
        """
        stop_launch_agent() {
            : >"$FAKE_MARKER_DIR/launch-agent-stopped"
        }
        load_runtime_env() {
            :
        }
        stop_decision_runtime() {
            printf '%s\\n' "$$" >"$FAKE_MARKER_DIR/pre-pull-pid"
        }
        cmd_update_after_pull() {
            : >"$FAKE_MARKER_DIR/stale-post-pull-code-ran"
        }
        """
    ).lstrip()
    local_script.write_text(
        script.replace(
            'case "${1:-}" in\n',
            f"{old_overrides}\ncase \"${{1:-}}\" in\n",
            1,
        ),
        encoding="utf-8",
    )
    new_overrides = textwrap.dedent(
        """
        cmd_update_after_pull() {
            [ "$1" = true ] || exit 71
            [ "${FAKE_HANDOFF_ENV:-}" = preserved ] || exit 72
            [ -z "${HEALTHMES_INTERNAL_UPDATE_HANDOFF_DEPTH+x}" ] \
                || exit 73
            printf '%s\\n' "$$" >"$FAKE_MARKER_DIR/post-pull-pid"
            cp "$(decision_runtime_lifecycle_lock_record)" \
                "$FAKE_MARKER_DIR/handoff-record"
            : >"$FAKE_MARKER_DIR/new-post-pull-code-ran"
        }
        """
    ).lstrip()
    updated_script.write_text(
        script.replace(
            'case "${1:-}" in\n',
            f"{new_overrides}\ncase \"${{1:-}}\" in\n",
            1,
        )
        + "\n# pulled update generation\n",
        encoding="utf-8",
    )
    updated_script.chmod(0o755)
    _write_executable(
        fake_bin / "git",
        """
        #!/usr/bin/env bash
        printf 'git %s\\n' "$*" >>"$FAKE_EVENT_LOG"
        case " $* " in
        *" pull --ff-only "*)
            cp "$FAKE_UPDATED_LOCAL_SCRIPT" "$FAKE_LOCAL_SCRIPT"
            ;;
        esac
        """,
    )
    launch_agent = (
        Path(harness["env"]["HOME"])
        / "Library"
        / "LaunchAgents"
        / "com.healthmes.local.plist"
    )
    launch_agent.touch()
    environment = {
        **harness["env"],
        "FAKE_MARKER_DIR": str(marker_dir),
        "FAKE_UPDATED_LOCAL_SCRIPT": str(updated_script),
        "FAKE_LOCAL_SCRIPT": str(local_script),
        "FAKE_HANDOFF_ENV": "preserved",
        "HEALTHMES_BASH_BIN": shutil.which("bash") or "/bin/bash",
    }

    result = subprocess.run(
        ["bash", str(local_script), "update"],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (marker_dir / "new-post-pull-code-ran").exists()
    assert not (marker_dir / "stale-post-pull-code-ran").exists()
    assert (marker_dir / "launch-agent-stopped").exists()
    assert (marker_dir / "pre-pull-pid").read_text(
        encoding="ascii"
    ) == (marker_dir / "post-pull-pid").read_text(encoding="ascii")
    handoff_record = (marker_dir / "handoff-record").read_text(
        encoding="ascii"
    )
    expected_digest = hashlib.sha256(
        updated_script.read_bytes()
    ).hexdigest()
    assert "operation\tupdate\n" in handoff_record
    assert "phase\tsetup\n" in handoff_record
    assert "script_contract_version\t2\n" in handoff_record
    assert f"script_sha256\t{expected_digest}\n" in handoff_record
    assert not (
        Path(harness["runtime"])
        / "hermes-decision-lifecycle-lock"
    ).exists()
    assert sum(
        line.startswith("git ") and " pull --ff-only" in line
        for line in _event_lines(harness)
    ) == 1


def test_update_reexec_contract_mismatch_preserves_journal(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    local_script = Path(harness["local_script"])
    updated_script = tmp_path / "incompatible-healthmes_local.sh"
    fake_bin = Path(harness["env"]["PATH"].split(":", 1)[0])
    script = local_script.read_text(encoding="utf-8")
    old_overrides = textwrap.dedent(
        """
        load_runtime_env() {
            :
        }
        stop_decision_runtime() {
            :
        }
        """
    ).lstrip()
    initial_script = script.replace(
        'case "${1:-}" in\n',
        f"{old_overrides}\ncase \"${{1:-}}\" in\n",
        1,
    )
    local_script.write_text(initial_script, encoding="utf-8")
    initial_digest = hashlib.sha256(
        initial_script.encode("utf-8")
    ).hexdigest()
    updated_script.write_text(
        script.replace(
            "DECISION_RUNTIME_LIFECYCLE_CONTRACT_VERSION=2",
            "DECISION_RUNTIME_LIFECYCLE_CONTRACT_VERSION=3",
            1,
        )
        + "\n# incompatible pulled lifecycle contract\n",
        encoding="utf-8",
    )
    updated_script.chmod(0o755)
    _write_executable(
        fake_bin / "git",
        """
        #!/usr/bin/env bash
        printf 'git %s\\n' "$*" >>"$FAKE_EVENT_LOG"
        case " $* " in
        *" pull --ff-only "*)
            cp "$FAKE_UPDATED_LOCAL_SCRIPT" "$FAKE_LOCAL_SCRIPT"
            ;;
        esac
        """,
    )
    environment = {
        **harness["env"],
        "FAKE_UPDATED_LOCAL_SCRIPT": str(updated_script),
        "FAKE_LOCAL_SCRIPT": str(local_script),
        "HEALTHMES_BASH_BIN": shutil.which("bash") or "/bin/bash",
    }

    result = subprocess.run(
        ["bash", str(local_script), "update"],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    lock = (
        Path(harness["runtime"])
        / "hermes-decision-lifecycle-lock"
    )
    assert result.returncode != 0
    assert "handoff contract version is incompatible" in result.stderr
    assert lock.exists()
    record = (lock / "record").read_text(encoding="ascii")
    assert "operation\tupdate\n" in record
    assert "phase\tpulling\n" in record
    assert "script_contract_version\t2\n" in record
    assert f"script_sha256\t{initial_digest}\n" in record
    assert "dev_mac setup" not in _event_lines(harness)


def test_uninstall_runs_launch_service_and_cleanup_under_one_lock(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    data_dir = runtime.parent
    retained_data = data_dir / "retained.db"
    retained_data.write_text("keep", encoding="ascii")
    fake_bin = Path(harness["env"]["PATH"].split(":", 1)[0])
    _write_executable(
        fake_bin / "launchctl",
        """
        #!/usr/bin/env bash
        [ -d "$FAKE_LIFECYCLE_LOCK" ] || exit 91
        printf 'launchctl %s\\n' "$*" >>"$FAKE_EVENT_LOG"
        """,
    )
    _write_executable(
        Path(harness["env"]["HEALTHMES_DEV_MAC_SCRIPT"]),
        """
        #!/usr/bin/env bash
        [ -d "$FAKE_LIFECYCLE_LOCK" ] || exit 92
        printf 'dev_mac %s\\n' "$*" >>"$FAKE_EVENT_LOG"
        """,
    )

    result = _run_local_runtime(
        harness,
        "uninstall",
        env_overrides={
            "FAKE_LIFECYCLE_LOCK": str(
                runtime / "hermes-decision-lifecycle-lock"
            )
        },
    )

    assert result.returncode == 0
    assert retained_data.read_text(encoding="ascii") == "keep"
    assert not runtime.exists()
    assert (
        data_dir / ".hermes-decision-runtime-transition.lock"
    ).is_file()
    events = _event_lines(harness)
    assert any(line.startswith("launchctl disable ") for line in events)
    assert "dev_mac services-stop" in events


def test_uninstall_delete_data_retains_only_permanent_transition_mutex(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    data_dir = runtime.parent
    (data_dir / "delete-me.db").write_text("remove", encoding="ascii")
    environment = {
        **harness["env"],
        "FAKE_TERM_BEHAVIOR": "exit",
        "FAKE_KILL_BEHAVIOR": "exit",
        "FAKE_SUPERVISOR_TERM_BEHAVIOR": "exit",
    }

    result = subprocess.run(
        [
            "bash",
            str(harness["local_script"]),
            "uninstall",
            "--delete-data",
        ],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert not runtime.exists()
    assert sorted(path.name for path in data_dir.iterdir()) == [
        ".hermes-decision-runtime-transition.lock"
    ]


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
    assert events.index(
        f"kill -s TERM -{FAKE_MANAGED_PID}"
    ) < events.index(
        f"kill -s KILL -{FAKE_MANAGED_PID}"
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
    assert not any(
        event.startswith("ps ") and f"-p {pid} " in event
        for event in events
    )
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
    assert f"kill -s TERM -{FAKE_MANAGED_PID}" in events
    assert f"kill -s KILL -{FAKE_MANAGED_PID}" not in events
    _assert_identity_check_immediately_before(
        events,
        f"kill -s TERM -{FAKE_MANAGED_PID}",
    )


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
@pytest.mark.parametrize(
    ("service_name", "pid_variable", "log_variable", "pid_filename", "mode"),
    (
        (
            "HealthMes",
            "$HEALTHMES_PID",
            "$HEALTHMES_LOG",
            "healthmes.pid",
            "failure",
        ),
        (
            "Open Wearables",
            "$OW_PID",
            "$OW_LOG",
            "open-wearables.pid",
            "timeout",
        ),
    ),
)
def test_generic_start_recovers_live_identity_after_ps_is_unknown(
    tmp_path: Path,
    service_name: str,
    pid_variable: str,
    log_variable: str,
    pid_filename: str,
    mode: str,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    fake_bin = Path(harness["env"]["PATH"].split(":", 1)[0])
    real_sleep = shutil.which("sleep")
    assert real_sleep is not None
    _install_generic_start_case(
        harness,
        service_name=service_name,
        pid_variable=pid_variable,
        log_variable=log_variable,
        command=f"exec {real_sleep} 30",
    )
    ps_bin = fake_bin / f"generic-start-{mode}-ps"
    failure_marker = tmp_path / f"generic-start-{mode}-failed-once"
    _write_executable(
        ps_bin,
        """
        #!/usr/bin/env bash
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
            && [ ! -f "$FAKE_GENERIC_PS_FAILURE_MARKER" ]; then
            : >"$FAKE_GENERIC_PS_FAILURE_MARKER"
            case "$FAKE_GENERIC_PS_MODE" in
            failure)
                printf 'transient ps failure\\n' >&2
                exit 2
                ;;
            timeout)
                /bin/sleep 30
                exit 0
                ;;
            esac
        fi
        /bin/kill -0 "$requested_pid" 2>/dev/null || exit 1
        case "$field" in
        pid= | pgid=) printf '%s\\n' "$requested_pid" ;;
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
    pid_file = runtime / pid_filename
    identity_file = pid_file.with_suffix(".pid.identity")
    launcher_pid: int | None = None
    env_overrides = {
        "HEALTHMES_PS_BIN": str(ps_bin),
        "HEALTHMES_KILL_BIN": "/bin/kill",
        "HEALTHMES_SLEEP_BIN": real_sleep,
        "FAKE_GENERIC_PS_MODE": mode,
        "FAKE_GENERIC_PS_FAILURE_MARKER": str(failure_marker),
        "FAKE_LOCAL_SCRIPT": str(harness["local_script"]),
    }
    try:
        first = _run_local_runtime(
            harness,
            "__test_start_generic",
            check=False,
            env_overrides=env_overrides,
            timeout=10,
        )

        assert first.returncode != 0
        assert "launcher identity is unknown" in first.stderr
        assert "preserving PID metadata" in first.stderr
        launcher_pid = int(pid_file.read_text(encoding="ascii"))
        os.kill(launcher_pid, 0)
        assert not identity_file.exists()
        assert (runtime / f"{pid_filename}.recovery").is_dir()

        second = _run_local_runtime(
            harness,
            "__test_start_generic",
            env_overrides=env_overrides,
            timeout=10,
        )

        assert second.returncode == 0
        assert f"{service_name} already running" in second.stdout
        assert int(pid_file.read_text(encoding="ascii")) == launcher_pid
        assert identity_file.exists()
        assert not (runtime / f"{pid_filename}.recovery").exists()
        os.kill(launcher_pid, 0)

        stopped = _run_local_runtime(
            harness,
            "__test_stop_generic",
            env_overrides=env_overrides,
            timeout=10,
        )

        assert stopped.returncode == 0
        assert f"{service_name} stopped" in stopped.stdout
        assert not pid_file.exists()
        assert not identity_file.exists()
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


@pytest.mark.parametrize("identity_state", ("absent", "reused"))
def test_generic_start_clears_only_reliably_stale_pid_tombstones(
    tmp_path: Path,
    identity_state: str,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    runtime = Path(harness["runtime"])
    fake_bin = Path(harness["env"]["PATH"].split(":", 1)[0])
    real_sleep = shutil.which("sleep")
    real_true = shutil.which("true")
    assert real_sleep is not None
    assert real_true is not None
    command = (
        f"exec {real_true}"
        if identity_state == "absent"
        else f"exec {real_sleep} 30"
    )
    _install_generic_start_case(
        harness,
        service_name="HealthMes",
        pid_variable="$HEALTHMES_PID",
        log_variable="$HEALTHMES_LOG",
        command=command,
    )
    ps_bin = fake_bin / f"generic-start-{identity_state}-ps"
    _write_executable(
        ps_bin,
        """
        #!/usr/bin/env bash
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
        if [ "$field" = "lstart=" ]; then
            printf 'Mon Aug 17 12:00:00 2026\\n'
            exit 0
        fi
        if [ "$field" = "pid=" ]; then
            printf '%s\\n' "$requested_pid" >"$FAKE_CAPTURED_PID"
        fi
        if [ "$FAKE_GENERIC_IDENTITY_STATE" = "absent" ]; then
            exit 1
        fi
        case "$field" in
        pid= | pgid=) printf '%s\\n' "$requested_pid" ;;
        comm=) printf '/bin/bash\\n' ;;
        lstart=) printf 'Mon Aug 17 12:00:00 2026\\n' ;;
        command=) printf '/bin/bash unrelated-service-runner\\n' ;;
        *) exit 1 ;;
        esac
        """,
    )
    pid_file = runtime / "healthmes.pid"
    identity_file = pid_file.with_suffix(".pid.identity")
    captured_pid = tmp_path / "captured-generic-start-pid"
    launcher_pid: int | None = None
    env_overrides = {
        "HEALTHMES_PS_BIN": str(ps_bin),
        "HEALTHMES_KILL_BIN": "/bin/kill",
        "FAKE_GENERIC_IDENTITY_STATE": identity_state,
        "FAKE_CAPTURED_PID": str(captured_pid),
    }
    if identity_state == "absent":
        env_overrides["HEALTHMES_SLEEP_BIN"] = real_sleep
    try:
        result = _run_local_runtime(
            harness,
            "__test_start_generic",
            check=False,
            env_overrides=env_overrides,
            timeout=10,
        )

        assert result.returncode != 0
        if identity_state == "absent":
            assert "exited before identity verification" in result.stderr
        else:
            assert "PID was reused before identity verification" in result.stderr
            launcher_pid = int(captured_pid.read_text(encoding="ascii"))
        assert not pid_file.exists()
        assert not identity_file.exists()
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


def test_decision_spawned_lease_is_the_atomic_parent_publication(
    tmp_path: Path,
) -> None:
    harness = _local_runtime_harness(tmp_path)
    local_script = Path(harness["local_script"])
    runtime = Path(harness["runtime"])
    _write_decision_startup_lease(
        runtime,
        phase="spawned",
        pid=FAKE_MANAGED_PID,
        nonce="abc123",
    )
    script = local_script.read_text(encoding="utf-8")
    test_case = textwrap.dedent(
        f"""
        case "${{1:-}}" in
        __test_wait_decision_publication)
            wait_for_decision_runtime_launcher_publication \
                abc123 {FAKE_MANAGED_PID}
            printf 'published\\n'
            ;;
        """
    ).lstrip()
    local_script.write_text(
        script.replace('case "${1:-}" in\n', test_case, 1),
        encoding="utf-8",
    )

    result = _run_local_runtime(
        harness,
        "__test_wait_decision_publication",
    )

    assert result.stdout == "published\n"
    assert not (runtime / "hermes-decision.pid").exists()
    assert not (runtime / "hermes-decision.pid.identity").exists()


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
            with_decision_runtime_lifecycle_lock start \
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
        case " ${FAKE_ABSENT_PIDS:-} " in
        *" $requested_pid "*) exit 1 ;;
        esac
        if [ -n "${FAKE_FORCE_UNKNOWN_PID:-}" ] \
            && [ "$requested_pid" = "$FAKE_FORCE_UNKNOWN_PID" ]; then
            printf 'permission denied\\n' >&2
            exit 2
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
        record_text = record.read_text(encoding="ascii")
        launcher_pid = int(
            next(
                line.split("\t", 1)[1]
                for line in record_text.splitlines()
                if line.startswith("launcher_pid\t")
            )
        )
        os.kill(launcher_pid, 0)
        assert failure_marker.exists()
        assert not pid_file.exists()
        assert not identity_file.exists()
        assert "phase\tspawned\n" in record_text
        assert f"launcher_pid\t{launcher_pid}\n" in record_text
        owner_pid = next(
            line.split("\t", 1)[1]
            for line in record_text.splitlines()
            if line.startswith("startup_owner_pid\t")
        )
        aged_record = re.sub(
            r"^(created_at_epoch|updated_at_epoch)\t[0-9]+$",
            r"\1\t1",
            record.read_text(encoding="ascii"),
            flags=re.MULTILINE,
        )
        record.write_text(aged_record, encoding="ascii")
        stop_env = {
            **env_overrides,
            "FAKE_ABSENT_PIDS": owner_pid,
            "FAKE_FORCE_UNKNOWN_PID": str(launcher_pid),
        }

        stop = _run_local_runtime(
            harness,
            "stop",
            check=False,
            env_overrides=stop_env,
        )
        status = _run_local_runtime(
            harness,
            "status",
            env_overrides=stop_env,
        )

        assert stop.returncode != 0
        assert "startup launcher identity is unknown" in stop.stderr
        assert not pid_file.exists()
        assert lease.exists()
        assert (
            "Hermes decision runtime: unknown "
            "(startup launcher identity is unverified; "
            "startup lease is preserved)"
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
