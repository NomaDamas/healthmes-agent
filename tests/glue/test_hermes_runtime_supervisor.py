"""Lifecycle regressions for the HealthMes-owned Hermes supervisor."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import os
import signal
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient

from healthmes.hermes_runtime_identity import HermesRuntimeIdentityError
from healthmes.hermes_runtime_supervisor import (
    HermesRuntimeLauncherIdentity,
    HermesRuntimeProcess,
    HermesRuntimeProcessIdentity,
    HermesRuntimeShutdownBudgetRecord,
    HermesRuntimeSupervisorConfig,
    _build_supervisor_server,
    _DarwinProcBsdInfo,
    _exclusive_file_lock,
    _next_restart_backoff,
    _parse_pydantic_float,
    _probe_darwin_process_group_members,
    _probe_darwin_process_snapshot,
    _probe_linux_process_group_members,
    _probe_process_group_members,
    _ProcessGroupMember,
    _run_runtime_process_action,
    _run_runtime_process_group_probe,
    _RuntimeShutdownBudgetPublication,
    _signal_process_group_member,
    capture_runtime_launcher_identity,
    capture_runtime_supervisor_identity,
    create_supervisor_app,
    load_runtime_shutdown_budget,
)
from healthmes.hermes_runtime_supervisor import main as supervisor_main


def _supervisor_config(
    tmp_path: Path,
    *,
    unhealthy_threshold: int = 2,
) -> HermesRuntimeSupervisorConfig:
    return HermesRuntimeSupervisorConfig(
        hermes_home=tmp_path / "home",
        manifest_path=tmp_path / "manifest.json",
        attestation_key_path=tmp_path / "attestation.key",
        vendor_root=tmp_path / "vendor",
        health_check_interval_seconds=0.005,
        health_check_timeout_seconds=0.005,
        unhealthy_threshold=unhealthy_threshold,
        restart_backoff_initial_seconds=0.005,
        restart_backoff_max_seconds=0.01,
    )


def _group_member(pid: int, token: str) -> _ProcessGroupMember:
    return _ProcessGroupMember(pid=pid, start_token=token)


@pytest.mark.parametrize("pid", (1, 2_147_483_648))
def test_runtime_identities_reject_pid_outside_managed_range(
    pid: int,
) -> None:
    with pytest.raises(ValueError, match="outside the managed range"):
        HermesRuntimeProcessIdentity(
            pid=pid,
            start_token="linux:1",
        )
    with pytest.raises(ValueError, match="outside the managed range"):
        HermesRuntimeLauncherIdentity(
            pid=pid,
            start_token="linux:1",
            service_nonce="service",
        )


@pytest.mark.parametrize("pid", (2, 2_147_483_647))
def test_runtime_identities_accept_managed_pid_boundaries(
    pid: int,
) -> None:
    assert HermesRuntimeProcessIdentity(
        pid=pid,
        start_token="linux:1",
    ).pid == pid
    assert HermesRuntimeLauncherIdentity(
        pid=pid,
        start_token="linux:1",
        service_nonce="service",
    ).pid == pid


def _install_lifecycle_harness(
    process: HermesRuntimeProcess,
    monkeypatch: pytest.MonkeyPatch,
    *,
    launch_failures: int = 0,
    health_results: list[bool | Exception] | None = None,
) -> tuple[list[Any], asyncio.Event]:
    children: list[Any] = []
    launched = asyncio.Event()
    remaining_failures = launch_failures
    results = list(health_results or [True])

    async def launch_child() -> None:
        nonlocal remaining_failures
        if remaining_failures:
            remaining_failures -= 1
            raise RuntimeError("simulated launch failure")
        child = SimpleNamespace(returncode=None)
        children.append(child)
        process._process = child
        process._state = object()  # type: ignore[assignment]
        process._launch_argv = ("fake-hermes",)
        process._healthy = True
        launched.set()

    async def stop_child() -> None:
        process._process = None
        process._state = None
        process._launch_argv = None
        process._healthy = False

    async def probe_child_health() -> bool:
        if len(results) > 1:
            result = results.pop(0)
        else:
            result = results[0]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(process, "_launch_child", launch_child)
    monkeypatch.setattr(process, "_stop_child", stop_child)
    monkeypatch.setattr(
        process,
        "_probe_child_health",
        probe_child_health,
    )
    return children, launched


@pytest.mark.asyncio
async def test_observable_start_recovers_initial_launch_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = HermesRuntimeProcess(_supervisor_config(tmp_path))
    children, launched = _install_lifecycle_harness(
        process,
        monkeypatch,
        launch_failures=1,
    )

    await process.start_observable()
    await asyncio.wait_for(launched.wait(), timeout=1)

    assert len(children) == 1
    assert process._monitor_task is not None
    assert not process._monitor_task.done()
    await process.aclose()


@pytest.mark.asyncio
async def test_watchdog_recovers_a_dead_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = HermesRuntimeProcess(_supervisor_config(tmp_path))
    children, launched = _install_lifecycle_harness(
        process,
        monkeypatch,
    )
    await process.start_observable()
    await asyncio.wait_for(launched.wait(), timeout=1)
    launched.clear()

    children[0].returncode = 9
    await asyncio.wait_for(launched.wait(), timeout=1)

    assert len(children) == 2
    assert process._process is children[1]
    assert process._healthy is True
    await process.aclose()


@pytest.mark.asyncio
async def test_watchdog_recovers_a_hung_child_after_bounded_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = HermesRuntimeProcess(
        _supervisor_config(tmp_path, unhealthy_threshold=2)
    )
    children, launched = _install_lifecycle_harness(
        process,
        monkeypatch,
        health_results=[False, False, True],
    )
    await process.start_observable()
    await asyncio.wait_for(launched.wait(), timeout=1)
    launched.clear()

    await asyncio.wait_for(launched.wait(), timeout=1)

    assert len(children) == 2
    assert process._process is children[1]
    assert process._healthy is True
    await process.aclose()


@pytest.mark.asyncio
async def test_watchdog_restarts_immediately_on_runtime_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = HermesRuntimeProcess(
        _supervisor_config(tmp_path, unhealthy_threshold=10)
    )
    children, launched = _install_lifecycle_harness(
        process,
        monkeypatch,
        health_results=[
            HermesRuntimeIdentityError(
                "hermes_runtime_identity_changed"
            ),
            True,
        ],
    )
    await process.start_observable()
    await asyncio.wait_for(launched.wait(), timeout=1)
    launched.clear()

    await asyncio.wait_for(launched.wait(), timeout=1)

    assert len(children) == 2
    assert process._process is children[1]
    await process.aclose()


@pytest.mark.asyncio
async def test_response_leases_share_generation_and_prioritize_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = HermesRuntimeProcess(
        replace(
            _supervisor_config(Path("/unused")),
            max_concurrent_responses=2,
        )
    )
    child = SimpleNamespace(returncode=None)
    state = object()
    next_state = object()
    process._process = child
    process._state = state  # type: ignore[assignment]
    process._launch_argv = ("fake-hermes",)
    process._child_generation = 7
    process._healthy = True
    process._lifecycle_state = "running"

    async def attest_pinned_generation(
        *,
        expected: object,
        generation: int,
    ) -> object:
        assert generation == process._child_generation
        return expected

    monkeypatch.setattr(
        process,
        "_attest_pinned_generation",
        attest_pinned_generation,
    )
    first, second = await asyncio.gather(
        process.acquire_response_lease(),
        process.acquire_response_lease(),
    )
    third = asyncio.create_task(process.acquire_response_lease())
    await asyncio.sleep(0)
    assert not third.done()

    replacement_entered = asyncio.Event()
    allow_replacement = asyncio.Event()

    async def replace_child() -> None:
        async with process._exclusive_child_generation():
            replacement_entered.set()
            await allow_replacement.wait()
            process._child_generation += 1
            process._state = next_state  # type: ignore[assignment]

    replacement = asyncio.create_task(replace_child())

    async def wait_for_replacement_to_queue() -> None:
        while process._waiting_child_writers != 1:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_replacement_to_queue(), timeout=1)

    assert first.generation == 7
    assert second.generation == 7
    assert not replacement_entered.is_set()

    await first.release()
    await first.release()
    await asyncio.sleep(0)
    assert not replacement_entered.is_set()
    assert not third.done()

    await second.release()
    await asyncio.wait_for(replacement_entered.wait(), timeout=1)
    assert not third.done()

    allow_replacement.set()
    await asyncio.wait_for(replacement, timeout=1)
    third_lease = await asyncio.wait_for(third, timeout=1)

    assert third_lease.generation == 8
    assert third_lease.state is next_state
    assert process._child_generation == 8
    await third_lease.release()


@pytest.mark.asyncio
async def test_closing_rejects_waiting_and_new_leases_while_old_lease_drains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = HermesRuntimeProcess(
        replace(
            _supervisor_config(tmp_path),
            max_concurrent_responses=1,
        )
    )
    child = SimpleNamespace(returncode=None)
    state = object()
    process._process = child
    process._state = state  # type: ignore[assignment]
    process._launch_argv = ("fake-hermes",)
    process._healthy = True
    process._lifecycle_state = "running"

    async def attest_pinned_generation(
        *,
        expected: object,
        generation: int,
    ) -> object:
        assert generation == process._child_generation
        return expected

    async def stop_child() -> None:
        process._process = None
        process._state = None
        process._launch_argv = None
        process._healthy = False

    monkeypatch.setattr(
        process,
        "_attest_pinned_generation",
        attest_pinned_generation,
    )
    monkeypatch.setattr(process, "_stop_child", stop_child)

    active = await process.acquire_response_lease()
    waiting_reader = asyncio.create_task(
        process.acquire_response_lease()
    )
    await asyncio.sleep(0)
    assert not waiting_reader.done()

    monitor_entered = asyncio.Event()

    async def queued_monitor_writer() -> None:
        async with process._exclusive_child_generation():
            monitor_entered.set()

    monitor = asyncio.create_task(queued_monitor_writer())
    process._monitor_task = monitor

    async def wait_for_monitor_to_queue() -> None:
        while process._waiting_child_writers != 1:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_monitor_to_queue(), timeout=1)
    close_task = asyncio.create_task(process.aclose())

    with pytest.raises(
        HermesRuntimeIdentityError,
        match="hermes_runtime_response_admission_closed",
    ):
        await asyncio.wait_for(waiting_reader, timeout=1)
    with pytest.raises(
        HermesRuntimeIdentityError,
        match="hermes_runtime_response_admission_closed",
    ):
        await asyncio.wait_for(
            process.acquire_response_lease(),
            timeout=1,
        )

    assert process._lifecycle_state == "closing"
    assert process._process is child
    assert not monitor_entered.is_set()
    assert not close_task.done()

    await active.release()
    await asyncio.wait_for(close_task, timeout=1)

    assert monitor.cancelled()
    assert process._process is None
    assert process._lifecycle_state == "closed"


@pytest.mark.asyncio
async def test_concurrent_close_prevents_watchdog_resurrection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for iteration in range(100):
        process = HermesRuntimeProcess(
            _supervisor_config(tmp_path / str(iteration))
        )
        children, _ = _install_lifecycle_harness(
            process,
            monkeypatch,
        )
        await process._lifecycle_lock.acquire()
        try:
            close_task = asyncio.create_task(process.aclose())
            await asyncio.sleep(0)
            start_task = asyncio.create_task(process.start_observable())
            await asyncio.sleep(0)
            process._lifecycle_lock.release()

            await asyncio.wait_for(
                asyncio.gather(close_task, start_task),
                timeout=1,
            )
            await asyncio.sleep(0)

            assert children == []
            assert process._monitor_task is None
            assert process._lifecycle_state == "closed"
        finally:
            if process._lifecycle_lock.locked():
                process._lifecycle_lock.release()
            await process.aclose()


@pytest.mark.asyncio
async def test_concurrent_close_prevents_direct_start_resurrection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for iteration in range(100):
        process = HermesRuntimeProcess(
            _supervisor_config(tmp_path / str(iteration))
        )
        children, _ = _install_lifecycle_harness(
            process,
            monkeypatch,
        )
        await process._lifecycle_lock.acquire()
        try:
            close_task = asyncio.create_task(process.aclose())
            await asyncio.sleep(0)
            start_task = asyncio.create_task(process.start())
            await asyncio.sleep(0)
            process._lifecycle_lock.release()

            await asyncio.wait_for(
                asyncio.gather(close_task, start_task),
                timeout=1,
            )

            assert children == []
            assert process._monitor_task is None
            assert process._lifecycle_state == "closed"
        finally:
            if process._lifecycle_lock.locked():
                process._lifecycle_lock.release()
            await process.aclose()


@pytest.mark.asyncio
async def test_close_stops_child_launched_by_inflight_direct_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = HermesRuntimeProcess(_supervisor_config(tmp_path))
    children: list[Any] = []
    launch_entered = asyncio.Event()
    allow_launch = asyncio.Event()

    async def launch_child() -> None:
        launch_entered.set()
        await allow_launch.wait()
        child = SimpleNamespace(returncode=None)
        children.append(child)
        process._process = child
        process._state = object()  # type: ignore[assignment]
        process._launch_argv = ("fake-hermes",)
        process._healthy = True

    async def stop_child() -> None:
        process._process = None
        process._state = None
        process._launch_argv = None
        process._healthy = False

    monkeypatch.setattr(process, "_launch_child", launch_child)
    monkeypatch.setattr(process, "_stop_child", stop_child)

    start_task = asyncio.create_task(process.start())
    await asyncio.wait_for(launch_entered.wait(), timeout=1)
    close_task = asyncio.create_task(process.aclose())
    await asyncio.sleep(0)

    assert process._lifecycle_state == "closing"
    allow_launch.set()
    await asyncio.wait_for(
        asyncio.gather(start_task, close_task),
        timeout=1,
    )

    assert len(children) == 1
    assert process._process is None
    assert process._monitor_task is None
    assert process._lifecycle_state == "closed"


@pytest.mark.asyncio
async def test_closed_supervisor_rejects_all_restart_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = HermesRuntimeProcess(_supervisor_config(tmp_path))
    children, _ = _install_lifecycle_harness(process, monkeypatch)

    await process.aclose()
    await process.start()
    await process.start_observable()
    await asyncio.sleep(0)

    assert children == []
    assert process._monitor_task is None
    assert process._lifecycle_state == "closed"


@pytest.mark.asyncio
async def test_close_failure_is_retryable_without_publishing_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = HermesRuntimeProcess(_supervisor_config(tmp_path))
    child = SimpleNamespace(returncode=None)
    process._process = child
    process._state = object()  # type: ignore[assignment]
    process._launch_argv = ("fake-hermes",)
    process._healthy = True
    process._lifecycle_state = "running"
    attempts = 0

    async def stop_child() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated teardown failure")
        process._process = None
        process._state = None
        process._launch_argv = None
        process._healthy = False

    monkeypatch.setattr(process, "_stop_child", stop_child)

    with pytest.raises(RuntimeError, match="simulated teardown failure"):
        await process.aclose()

    assert process._lifecycle_state == "close_failed"
    assert process._process is child
    await process.start()
    await process.start_observable()
    assert process._monitor_task is None
    assert process._process is child

    await process.aclose()

    assert attempts == 2
    assert process._process is None
    assert process._lifecycle_state == "closed"


@pytest.mark.asyncio
async def test_cancelled_close_waits_for_child_teardown_before_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for iteration in range(50):
        process = HermesRuntimeProcess(
            _supervisor_config(tmp_path / str(iteration))
        )
        child = SimpleNamespace(returncode=None)
        process._process = child
        process._state = object()  # type: ignore[assignment]
        process._launch_argv = ("fake-hermes",)
        process._healthy = True
        stop_started = asyncio.Event()
        allow_stop = asyncio.Event()

        async def stop_child() -> None:
            stop_started.set()
            await allow_stop.wait()
            process._process = None
            process._state = None
            process._launch_argv = None
            process._healthy = False

        monkeypatch.setattr(process, "_stop_child", stop_child)
        close_task = asyncio.create_task(process.aclose())
        await asyncio.wait_for(stop_started.wait(), timeout=1)

        for _ in range(3):
            close_task.cancel()
            await asyncio.sleep(0)
            assert process._lifecycle_state == "closing"
            assert process._process is child

        allow_stop.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(close_task, timeout=1)

        assert process._process is None
        assert process._monitor_task is None
        assert process._lifecycle_state == "closed"


@pytest.mark.asyncio
async def test_cancelled_close_waiting_for_response_lease_cannot_orphan_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = HermesRuntimeProcess(_supervisor_config(tmp_path))
    child = SimpleNamespace(returncode=None)
    process._process = child
    process._state = object()  # type: ignore[assignment]
    process._launch_argv = ("fake-hermes",)
    process._healthy = True
    process._lifecycle_state = "running"

    async def stop_child() -> None:
        process._process = None
        process._state = None
        process._launch_argv = None
        process._healthy = False

    async def attest_pinned_generation(
        *,
        expected: object,
        generation: int,
    ) -> object:
        assert generation == process._child_generation
        return expected

    monkeypatch.setattr(process, "_stop_child", stop_child)
    monkeypatch.setattr(
        process,
        "_attest_pinned_generation",
        attest_pinned_generation,
    )
    lease = await process.acquire_response_lease()
    try:
        close_task = asyncio.create_task(process.aclose())
        while process._lifecycle_state != "closing":
            await asyncio.sleep(0)
        close_task.cancel()
        await asyncio.sleep(0)

        assert process._lifecycle_state == "closing"
        assert process._process is child
    finally:
        await lease.release()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(close_task, timeout=1)

    assert process._process is None
    assert process._lifecycle_state == "closed"


@pytest.mark.asyncio
async def test_child_kill_wait_is_bounded_and_close_reports_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = {_group_member(4242, "leader")}

    def probe(
        _pgid: int,
        _timeout_seconds: float,
    ) -> frozenset[_ProcessGroupMember]:
        return frozenset(group)

    process = HermesRuntimeProcess(
        replace(
            _supervisor_config(tmp_path),
            child_term_timeout_seconds=0.001,
            child_kill_timeout_seconds=0.001,
        ),
        process_group_probe=probe,
    )
    never_exits = asyncio.Event()

    class StuckProcess:
        returncode = None
        pid = 4242

        def __init__(self) -> None:
            self.wait_calls = 0

        async def wait(self) -> int:
            self.wait_calls += 1
            await never_exits.wait()
            return 0

    child = StuckProcess()
    process._process = child  # type: ignore[assignment]
    process._state = object()  # type: ignore[assignment]
    process._launch_argv = ("fake-hermes",)
    process._healthy = True
    process._lifecycle_state = "running"
    signals: list[tuple[int, signal.Signals]] = []

    def kill_member(pid: int, sent: signal.Signals) -> None:
        signals.append((pid, sent))
        if sent == signal.SIGKILL:
            group.clear()

    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor._signal_process_group_member",
        lambda member, sent: (
            kill_member(member.pid, sent) is None
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="hermes_runtime_child_kill_timeout",
    ):
        await asyncio.wait_for(process.aclose(), timeout=1)

    assert signals == [
        (4242, signal.SIGTERM),
        (4242, signal.SIGKILL),
    ]
    assert child.wait_calls == 1
    assert process._process is child
    assert process._state is None
    assert process._lifecycle_state == "close_failed"


@pytest.mark.asyncio
async def test_sigterm_checks_descendants_and_sigkills_the_remaining_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader = _group_member(4242, "leader")
    descendant = _group_member(4243, "descendant")
    group = {leader, descendant}

    def probe(
        _pgid: int,
        _timeout_seconds: float,
    ) -> frozenset[_ProcessGroupMember]:
        return frozenset(group)

    process = HermesRuntimeProcess(
        replace(
            _supervisor_config(tmp_path),
            child_term_timeout_seconds=0.001,
            child_kill_timeout_seconds=0.01,
        ),
        process_group_probe=probe,
    )

    class Child:
        returncode: int | None = None
        pid = 4242

        async def wait(self) -> int:
            return 0

    child = Child()
    process._process = child  # type: ignore[assignment]
    process._state = object()  # type: ignore[assignment]
    process._launch_argv = ("fake-hermes",)
    process._healthy = True
    process._lifecycle_state = "running"
    signals: list[tuple[int, signal.Signals]] = []

    def kill_member(pid: int, sent: signal.Signals) -> None:
        signals.append((pid, sent))
        if pid == leader.pid and sent == signal.SIGTERM:
            child.returncode = 0
            group.discard(leader)
        elif sent == signal.SIGKILL:
            group.clear()

    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor._signal_process_group_member",
        lambda member, sent: (
            kill_member(member.pid, sent) is None
        ),
    )

    await process.aclose()

    assert signals == [
        (leader.pid, signal.SIGTERM),
        (descendant.pid, signal.SIGTERM),
        (descendant.pid, signal.SIGKILL),
    ]
    assert group == set()
    assert process._process is None
    assert process._lifecycle_state == "closed"


@pytest.mark.asyncio
async def test_reused_process_group_is_never_signaled_or_reported_drained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = {_group_member(4242, "original")}

    def probe(
        _pgid: int,
        _timeout_seconds: float,
    ) -> frozenset[_ProcessGroupMember]:
        return frozenset(group)

    process = HermesRuntimeProcess(
        replace(
            _supervisor_config(tmp_path),
            child_term_timeout_seconds=0.01,
            child_kill_timeout_seconds=0.01,
        ),
        process_group_probe=probe,
    )
    child = SimpleNamespace(returncode=None, pid=4242)
    process._process = child
    process._state = object()  # type: ignore[assignment]
    process._launch_argv = ("fake-hermes",)
    process._healthy = True
    process._lifecycle_state = "running"
    signals: list[signal.Signals] = []

    def replace_after_term(_pid: int, sent: signal.Signals) -> None:
        signals.append(sent)
        if sent == signal.SIGTERM:
            group.clear()
            group.add(_group_member(4242, "reused"))

    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor._signal_process_group_member",
        lambda member, sent: (
            replace_after_term(member.pid, sent) is None
        ),
    )

    with pytest.raises(
        HermesRuntimeIdentityError,
        match="hermes_runtime_child_group_identity_changed",
    ):
        await process.aclose()

    assert signals == [signal.SIGTERM]
    assert process._process is child
    assert process._lifecycle_state == "close_failed"


@pytest.mark.asyncio
async def test_post_sigkill_group_verification_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = _group_member(4242, "leader")

    def probe(
        _pgid: int,
        _timeout_seconds: float,
    ) -> frozenset[_ProcessGroupMember]:
        return frozenset({member})

    process = HermesRuntimeProcess(
        replace(
            _supervisor_config(tmp_path),
            child_term_timeout_seconds=0.001,
            child_kill_timeout_seconds=0.001,
        ),
        process_group_probe=probe,
    )
    child = SimpleNamespace(returncode=None, pid=4242)
    process._process = child
    process._state = object()  # type: ignore[assignment]
    process._launch_argv = ("fake-hermes",)
    process._healthy = True
    process._lifecycle_state = "running"
    signals: list[signal.Signals] = []
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor._signal_process_group_member",
        lambda _member, sent: (
            signals.append(sent) is None
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="hermes_runtime_child_kill_timeout",
    ):
        await asyncio.wait_for(process.aclose(), timeout=1)

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert process._process is child
    assert process._lifecycle_state == "close_failed"


@pytest.mark.asyncio
async def test_verified_descendant_is_stopped_after_leader_exits_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader = _group_member(4242, "leader")
    descendant = _group_member(4243, "descendant")
    group = {descendant}

    def probe(
        _pgid: int,
        _timeout_seconds: float,
    ) -> frozenset[_ProcessGroupMember]:
        return frozenset(group)

    process = HermesRuntimeProcess(
        replace(
            _supervisor_config(tmp_path),
            child_term_timeout_seconds=0.01,
            child_kill_timeout_seconds=0.01,
        ),
        process_group_probe=probe,
    )

    class ExitedLeader:
        returncode = 0
        pid = leader.pid

        async def wait(self) -> int:
            return 0

    child = ExitedLeader()
    process._process = child  # type: ignore[assignment]
    process._child_pgid = leader.pid
    process._known_child_group_members = frozenset(
        {leader, descendant}
    )
    process._state = object()  # type: ignore[assignment]
    process._launch_argv = ("fake-hermes",)
    process._healthy = True
    process._lifecycle_state = "running"
    signals: list[tuple[int, signal.Signals]] = []

    def kill_member(pid: int, sent: signal.Signals) -> None:
        signals.append((pid, sent))
        group.discard(descendant)

    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor._signal_process_group_member",
        lambda member, sent: (
            kill_member(member.pid, sent) is None
        ),
    )
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.os.killpg",
        lambda *_args: pytest.fail("numeric PGID must never be signaled"),
    )

    await process.aclose()

    assert signals == [(descendant.pid, signal.SIGTERM)]
    assert process._process is None
    assert process._lifecycle_state == "closed"


@pytest.mark.asyncio
async def test_leader_exit_before_first_snapshot_still_cleans_descendant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader = _group_member(4242, "leader")
    descendant = _group_member(4243, "descendant")
    group = {descendant}
    probes: list[int] = []

    def probe(
        pgid: int,
        _timeout_seconds: float,
    ) -> frozenset[_ProcessGroupMember]:
        probes.append(pgid)
        return frozenset(group)

    process = HermesRuntimeProcess(
        replace(
            _supervisor_config(tmp_path),
            child_term_timeout_seconds=0.01,
            child_kill_timeout_seconds=0.01,
        ),
        process_group_probe=probe,
    )

    class ExitedLeader:
        returncode = 0
        pid = leader.pid

        async def wait(self) -> int:
            return 0

    child = ExitedLeader()
    process._process = child  # type: ignore[assignment]
    process._child_pgid = leader.pid
    process._state = object()  # type: ignore[assignment]
    process._launch_argv = ("fake-hermes",)
    process._healthy = True
    process._lifecycle_state = "running"
    signals: list[tuple[int, signal.Signals]] = []

    def signal_member(
        member: _ProcessGroupMember,
        sent: signal.Signals,
    ) -> bool:
        signals.append((member.pid, sent))
        group.discard(descendant)
        return True

    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor._signal_process_group_member",
        signal_member,
    )

    await process.aclose()

    assert probes
    assert set(probes) == {leader.pid}
    assert signals == [(descendant.pid, signal.SIGTERM)]
    assert process._process is None
    assert process._lifecycle_state == "closed"


@pytest.mark.asyncio
async def test_os_exited_leader_with_lagging_returncode_cleans_descendant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader = _group_member(4242, "leader")
    descendant = _group_member(4243, "descendant")
    group = {descendant}
    probes: list[frozenset[_ProcessGroupMember]] = []

    def probe(
        _pgid: int,
        _timeout_seconds: float,
    ) -> frozenset[_ProcessGroupMember]:
        snapshot = frozenset(group)
        probes.append(snapshot)
        return snapshot

    process = HermesRuntimeProcess(
        replace(
            _supervisor_config(tmp_path),
            child_term_timeout_seconds=0.05,
            child_kill_timeout_seconds=0.05,
        ),
        process_group_probe=probe,
    )

    class LaggingExitedLeader:
        returncode: int | None = None
        pid = leader.pid

        def __init__(self) -> None:
            self.wait_calls = 0

        async def wait(self) -> int:
            self.wait_calls += 1
            self.returncode = 0
            return 0

    child = LaggingExitedLeader()
    process._process = child  # type: ignore[assignment]
    process._child_pgid = leader.pid
    process._state = object()  # type: ignore[assignment]
    process._launch_argv = ("fake-hermes",)
    process._healthy = True
    process._lifecycle_state = "running"
    signals: list[tuple[int, signal.Signals]] = []

    def signal_member(
        member: _ProcessGroupMember,
        sent: signal.Signals,
    ) -> bool:
        signals.append((member.pid, sent))
        group.discard(descendant)
        return True

    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor._signal_process_group_member",
        signal_member,
    )
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.os.killpg",
        lambda *_args: pytest.fail("numeric PGID must never be signaled"),
    )

    await process.aclose()

    assert child.wait_calls >= 1
    assert probes[0] == frozenset({descendant})
    assert signals == [(descendant.pid, signal.SIGTERM)]
    assert process._process is None
    assert process._lifecycle_state == "closed"


@pytest.mark.asyncio
async def test_leader_exit_before_first_snapshot_probes_empty_group(
    tmp_path: Path,
) -> None:
    probes: list[int] = []

    def probe(
        pgid: int,
        _timeout_seconds: float,
    ) -> frozenset[_ProcessGroupMember]:
        probes.append(pgid)
        return frozenset()

    process = HermesRuntimeProcess(
        _supervisor_config(tmp_path),
        process_group_probe=probe,
    )
    child = SimpleNamespace(returncode=0, pid=4242)
    process._process = child
    process._child_pgid = child.pid
    process._state = object()  # type: ignore[assignment]
    process._launch_argv = ("fake-hermes",)
    process._healthy = True
    process._lifecycle_state = "running"

    await process.aclose()

    assert probes == [child.pid] * 4
    assert process._process is None
    assert process._lifecycle_state == "closed"


@pytest.mark.asyncio
async def test_single_empty_group_scan_is_not_treated_as_drained(
    tmp_path: Path,
) -> None:
    member = _group_member(4242, "leader")
    probes = 0

    def probe(
        _pgid: int,
        _timeout_seconds: float,
    ) -> frozenset[_ProcessGroupMember]:
        nonlocal probes
        probes += 1
        if probes == 1:
            return frozenset()
        return frozenset({member})

    process = HermesRuntimeProcess(
        _supervisor_config(tmp_path),
        process_group_probe=probe,
    )
    process._child_pgid = member.pid
    process._known_child_group_members = frozenset({member})

    drained = await process._wait_for_child_group_exit(
        deadline=asyncio.get_running_loop().time() + 0.2,
    )

    assert drained is False
    assert probes >= 2


@pytest.mark.asyncio
async def test_empty_pre_reap_snapshot_never_adopts_reused_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unrelated = _group_member(5252, "unrelated-reused-group")
    probes = 0

    def probe(
        _pgid: int,
        _timeout_seconds: float,
    ) -> frozenset[_ProcessGroupMember]:
        nonlocal probes
        probes += 1
        if probes == 1:
            return frozenset()
        return frozenset({unrelated})

    process = HermesRuntimeProcess(
        _supervisor_config(tmp_path),
        process_group_probe=probe,
    )
    child = SimpleNamespace(returncode=0, pid=4242)
    process._process = child
    process._child_pgid = child.pid
    process._state = object()  # type: ignore[assignment]
    process._launch_argv = ("fake-hermes",)
    process._healthy = True
    process._lifecycle_state = "running"
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor._signal_process_group_member",
        lambda member, sent: (
            signals.append((member.pid, sent)) is None
        ),
    )
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.os.killpg",
        lambda *_args: pytest.fail("numeric PGID must never be signaled"),
    )

    for _ in range(2):
        with pytest.raises(
            HermesRuntimeIdentityError,
            match="hermes_runtime_child_group_identity_changed",
        ):
            await process.aclose()

    assert probes == 2
    assert signals == []
    assert process._known_child_group_members == frozenset()
    assert process._child_group_identity_lost is True
    assert process._process is child
    assert process._lifecycle_state == "close_failed"


@pytest.mark.asyncio
async def test_group_reuse_between_probe_and_signal_is_not_signaled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader = _group_member(4242, "leader")
    probes = 0

    def probe(
        _pgid: int,
        _timeout_seconds: float,
    ) -> frozenset[_ProcessGroupMember]:
        nonlocal probes
        probes += 1
        if probes == 1:
            return frozenset({leader})
        return frozenset({_group_member(4242, "reused")})

    process = HermesRuntimeProcess(
        replace(
            _supervisor_config(tmp_path),
            child_term_timeout_seconds=0.01,
            child_kill_timeout_seconds=0.01,
        ),
        process_group_probe=probe,
    )
    child = SimpleNamespace(returncode=None, pid=leader.pid)
    process._process = child
    process._child_pgid = leader.pid
    process._known_child_group_members = frozenset({leader})
    process._state = object()  # type: ignore[assignment]
    process._launch_argv = ("fake-hermes",)
    process._healthy = True
    process._lifecycle_state = "running"
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.os.kill",
        lambda pid, sent: signals.append((pid, sent)),
    )
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.os.killpg",
        lambda *_args: pytest.fail("numeric PGID must never be signaled"),
    )

    with pytest.raises(
        HermesRuntimeIdentityError,
        match="hermes_runtime_child_group_identity_changed",
    ):
        await process.aclose()

    assert signals == []
    assert process._lifecycle_state == "close_failed"


def test_linux_pidfd_signal_stays_bound_after_final_identity_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = _group_member(4242, "linux:12345")
    pidfd_calls: list[tuple[int, int]] = []
    signals: list[tuple[int, signal.Signals | int]] = []
    closed: list[int] = []
    live_token = member.start_token

    def open_pidfd(pid: int, flags: int) -> int:
        pidfd_calls.append((pid, flags))
        return 91

    def send_pidfd(
        descriptor: int,
        sent: signal.Signals | int,
        _siginfo: None,
        _flags: int,
    ) -> None:
        nonlocal live_token
        signals.append((descriptor, sent))
        if sent == signal.SIGTERM:
            # Model PID reuse in the exact interval after the final token
            # probe. The pidfd remains bound to the already-open process.
            live_token = "linux:reused"

    monkeypatch.setattr(os, "pidfd_open", open_pidfd, raising=False)
    monkeypatch.setattr(
        signal,
        "pidfd_send_signal",
        send_pidfd,
        raising=False,
    )
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor._probe_process_start_token",
        lambda pid, expected_style=None: (
            live_token
            if pid == member.pid and expected_style == "linux"
            else None
        ),
    )
    monkeypatch.setattr(os, "close", lambda fd: closed.append(fd))
    monkeypatch.setattr(
        os,
        "kill",
        lambda *_args: pytest.fail("numeric PID must not be signaled"),
    )

    assert _signal_process_group_member(member, signal.SIGTERM)
    assert pidfd_calls == [(member.pid, 0)]
    assert signals == [(91, signal.SIGTERM)]
    assert closed == [91]
    assert live_token == "linux:reused"


def test_linux_without_pidfd_fails_closed_without_numeric_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = _group_member(4242, "linux:12345")
    monkeypatch.setattr(os, "pidfd_open", None, raising=False)
    monkeypatch.setattr(
        signal,
        "pidfd_send_signal",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        os,
        "kill",
        lambda *_args: pytest.fail("uncertain numeric PID was signaled"),
    )

    with pytest.raises(
        HermesRuntimeIdentityError,
        match="hermes_runtime_child_pidfd_unavailable",
    ):
        _signal_process_group_member(member, signal.SIGTERM)


def test_runtime_stop_linux_without_pidfd_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "linux:12345"
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.sys.platform",
        "linux",
    )
    monkeypatch.setattr(
        Path,
        "is_dir",
        lambda path: str(path) == "/proc",
    )
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor._probe_process_start_token",
        lambda pid, expected_style=None: (
            token if pid == 4242 and expected_style == "linux" else None
        ),
    )
    monkeypatch.setattr(os, "pidfd_open", None, raising=False)
    monkeypatch.setattr(
        signal,
        "pidfd_send_signal",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        os,
        "kill",
        lambda *_args: pytest.fail("unverified Linux PID was signaled"),
    )

    assert (
        _run_runtime_process_action(
            action="signal",
            pid=4242,
            start_token=token,
        )
        == 5
    )
    assert "hermes_runtime_child_pidfd_unavailable" in capsys.readouterr().err


def test_runtime_stop_unreadable_linux_proc_identity_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.sys.platform",
        "linux",
    )
    monkeypatch.setattr(
        Path,
        "is_dir",
        lambda path: str(path) == "/proc",
    )
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError("denied")
        ),
    )
    monkeypatch.setattr(
        os,
        "kill",
        lambda *_args: pytest.fail("unverified Linux PID was signaled"),
    )

    assert (
        _run_runtime_process_action(
            action="probe",
            pid=4242,
            start_token="linux:12345",
        )
        == 5
    )
    assert (
        "hermes_runtime_linux_process_identity_unavailable"
        in capsys.readouterr().err
    )


def test_runtime_stop_linux_without_proc_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.sys.platform",
        "linux",
    )
    monkeypatch.setattr(
        Path,
        "is_dir",
        lambda _path: False,
    )
    monkeypatch.setattr(
        os,
        "kill",
        lambda *_args: pytest.fail("unverified Linux PID was signaled"),
    )

    assert (
        _run_runtime_process_action(
            action="signal",
            pid=4242,
            start_token="linux:12345",
        )
        == 5
    )
    assert (
        "hermes_runtime_supervisor_proc_unavailable"
        in capsys.readouterr().err
    )


def test_runtime_stop_unsupported_platform_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.sys.platform",
        "freebsd14",
    )
    monkeypatch.setattr(
        os,
        "kill",
        lambda *_args: pytest.fail("unsupported platform was signaled"),
    )

    assert (
        _run_runtime_process_action(
            action="signal",
            pid=4242,
            start_token="darwin:1786915200:123456",
        )
        == 5
    )
    assert (
        "hermes_runtime_supervisor_platform_mismatch"
        in capsys.readouterr().err
    )


def test_runtime_process_wait_returns_after_exact_identity_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states = iter(("live", "gone"))
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor._runtime_process_identity_state",
        lambda _identity: next(states),
    )
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.time.sleep",
        lambda _seconds: None,
    )

    assert (
        _run_runtime_process_action(
            action="wait",
            pid=4242,
            start_token="darwin:1786915200:123456",
            timeout_seconds=1,
        )
        == 0
    )


def test_runtime_process_wait_timeout_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    times = iter((0.0, 2.0))
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor._runtime_process_identity_state",
        lambda _identity: "live",
    )
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.time.monotonic",
        lambda: next(times),
    )

    assert (
        _run_runtime_process_action(
            action="wait",
            pid=4242,
            start_token="darwin:1786915200:123456",
            timeout_seconds=1,
        )
        == 6
    )
    assert (
        "hermes_runtime_supervisor_wait_timeout"
        in capsys.readouterr().err
    )


def test_generic_ps_identity_is_never_signaled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = _group_member(
        4242,
        "ps:Mon Aug 17 12:00:00 2026",
    )
    monkeypatch.setattr(
        os,
        "kill",
        lambda *_args: pytest.fail("generic ps identity was signaled"),
    )

    with pytest.raises(
        HermesRuntimeIdentityError,
        match="hermes_runtime_child_group_identity_unsupported",
    ):
        _signal_process_group_member(member, signal.SIGTERM)


def test_runtime_stop_generic_ps_identity_is_never_signaled(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        os,
        "kill",
        lambda *_args: pytest.fail("generic ps identity was signaled"),
    )

    assert (
        _run_runtime_process_action(
            action="signal",
            pid=4242,
            start_token="ps:Mon Aug 17 12:00:00 2026",
        )
        == 5
    )
    assert (
        "runtime process start token is invalid"
        in capsys.readouterr().err
    )


def test_unsupported_platform_group_probe_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.sys.platform",
        "freebsd14",
    )
    monkeypatch.setattr(
        os,
        "kill",
        lambda *_args: pytest.fail("unsupported platform was signaled"),
    )

    with pytest.raises(
        HermesRuntimeIdentityError,
        match="hermes_runtime_child_group_platform_unsupported",
    ):
        _probe_process_group_members(4242, 1)


def test_linux_without_proc_group_identity_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_exists = Path.exists
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.sys.platform",
        "linux",
    )
    monkeypatch.setattr(
        Path,
        "exists",
        lambda path: (
            False
            if str(path) == "/proc/self/stat"
            else original_exists(path)
        ),
    )
    monkeypatch.setattr(
        os,
        "kill",
        lambda *_args: pytest.fail("unverified Linux PID was signaled"),
    )

    with pytest.raises(
        HermesRuntimeIdentityError,
        match="hermes_runtime_child_group_proc_unavailable",
    ):
        _probe_process_group_members(4242, 1)


@pytest.mark.parametrize(
    "read_error",
    (
        PermissionError(errno.EACCES, "denied"),
        OSError(errno.EIO, "I/O failure"),
    ),
)
def test_linux_group_probe_unreadable_stat_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    read_error: OSError,
) -> None:
    original_iterdir = Path.iterdir
    original_read_text = Path.read_text

    def fake_iterdir(path: Path):
        if path == Path("/proc"):
            return iter((Path("/proc/4242"),))
        return original_iterdir(path)

    def fake_read_text(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> str:
        if path == Path("/proc/4242/stat"):
            raise read_error
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "iterdir", fake_iterdir)
    monkeypatch.setattr(Path, "read_text", fake_read_text)

    with pytest.raises(
        HermesRuntimeIdentityError,
        match="hermes_runtime_child_group_proc_unreadable",
    ):
        _probe_linux_process_group_members(4242, 1)


def test_linux_group_probe_only_treats_missing_stat_as_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_iterdir = Path.iterdir
    original_read_text = Path.read_text

    def fake_iterdir(path: Path):
        if path == Path("/proc"):
            return iter((Path("/proc/4242"),))
        return original_iterdir(path)

    def fake_read_text(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> str:
        if path == Path("/proc/4242/stat"):
            raise FileNotFoundError(errno.ENOENT, "gone")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "iterdir", fake_iterdir)
    monkeypatch.setattr(Path, "read_text", fake_read_text)

    assert _probe_linux_process_group_members(4242, 1) == frozenset()


def test_darwin_group_probe_uses_trusted_ps_and_minimal_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(
        arguments: list[str],
        **kwargs: Any,
    ) -> SimpleNamespace:
        calls.append((arguments, kwargs))
        return SimpleNamespace(
            stdout="4242 4000\n5000 5000\n",
            stderr="",
        )

    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.subprocess.run",
        fake_run,
    )
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor._probe_darwin_process_snapshot",
        lambda pid: (
            (4000, "darwin:1786915200:123456")
            if pid == 4242
            else pytest.fail("non-target process was inspected")
        ),
    )

    assert _probe_darwin_process_group_members(4000, 1) == frozenset(
        {
            _group_member(
                4242,
                "darwin:1786915200:123456",
            )
        }
    )
    assert calls == [
        (
            ["/bin/ps", "-axo", "pid=,pgid="],
            {
                "check": True,
                "capture_output": True,
                "text": True,
                "env": {
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PATH": "/usr/bin:/bin",
                },
                "timeout": 1,
            },
        )
    ]


@pytest.mark.parametrize(
    ("stdout", "stderr", "error"),
    (
        ("", "", "ps_output_invalid"),
        ("4242\n", "", "ps_output_invalid"),
        ("4242 4242", "", "ps_output_invalid"),
        ("4242 4242 extra\n", "", "ps_output_invalid"),
        ("not-a-pid 4242\n", "", "ps_output_invalid"),
        ("0 4242\n", "", "ps_output_invalid"),
        (
            "4242 4242\n4242 4242\n",
            "",
            "ps_output_duplicate",
        ),
        (
            "4242 4242\nbad partial row\n",
            "",
            "ps_output_invalid",
        ),
        (
            "4242 4242\n",
            "unexpected warning\n",
            "ps_output_invalid",
        ),
    ),
)
def test_darwin_group_probe_rejects_malformed_or_partial_ps_output(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    stderr: str,
    error: str,
) -> None:
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout=stdout,
            stderr=stderr,
        ),
    )
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor._probe_darwin_process_snapshot",
        lambda _pid: (4242, "darwin:1786915200:123456"),
    )

    with pytest.raises(
        HermesRuntimeIdentityError,
        match=error,
    ):
        _probe_darwin_process_group_members(4242, 1)


def test_darwin_group_probe_rejects_ps_and_libproc_pgid_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout="4242 4000\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor._probe_darwin_process_snapshot",
        lambda _pid: (5000, "darwin:1786915200:123456"),
    )

    with pytest.raises(
        HermesRuntimeIdentityError,
        match="ps_output_inconsistent",
    ):
        _probe_darwin_process_group_members(4000, 1)


def test_darwin_group_probe_rejects_target_that_disappears_from_libproc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout="4242 4000\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor._probe_darwin_process_snapshot",
        lambda _pid: None,
    )

    with pytest.raises(
        HermesRuntimeIdentityError,
        match="ps_output_inconsistent",
    ):
        _probe_darwin_process_group_members(4000, 1)


def test_runtime_launcher_group_probe_reports_nonempty_without_signalling(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor._probe_process_group_members",
        lambda _pgid, _timeout: frozenset(
            {_group_member(4242, "darwin:1786915200:123456")}
        ),
    )
    monkeypatch.setattr(
        os,
        "kill",
        lambda *_args: pytest.fail("group proof must never signal"),
    )

    assert (
        _run_runtime_process_group_probe(
            pgid=4242,
            timeout_seconds=1,
        )
        == 6
    )
    assert (
        "hermes_runtime_launcher_group_not_empty"
        in capsys.readouterr().err
    )


def test_linux_launcher_group_probe_rechecks_transient_empty_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = _group_member(4242, "linux:12345")
    snapshots = iter((frozenset(), frozenset({member})))
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.sys.platform",
        "linux",
    )
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor._probe_process_group_members",
        lambda *_args: next(snapshots),
    )

    assert (
        _run_runtime_process_group_probe(
            pgid=4242,
            timeout_seconds=1,
        )
        == 6
    )


def test_runtime_launcher_group_probe_fails_closed_on_unknown_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_probe(
        _pgid: int,
        _timeout: float,
    ) -> frozenset[_ProcessGroupMember]:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_child_group_ps_output_invalid"
        )

    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor._probe_process_group_members",
        fail_probe,
    )

    assert (
        _run_runtime_process_group_probe(
            pgid=4242,
            timeout_seconds=1,
        )
        == 5
    )
    assert (
        "hermes_runtime_child_group_ps_output_invalid"
        in capsys.readouterr().err
    )


def test_darwin_libproc_identity_uses_microsecond_start_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeLibproc:
        @staticmethod
        def proc_pidinfo(
            pid: int,
            _flavor: int,
            _argument: int,
            buffer: object,
            size: int,
        ) -> int:
            information = ctypes.cast(
                buffer,
                ctypes.POINTER(_DarwinProcBsdInfo),
            ).contents
            information.pbi_pid = pid
            information.pbi_pgid = 4000
            information.pbi_start_tvsec = 1_786_915_200
            information.pbi_start_tvusec = 123_456
            return size

    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor._load_darwin_libproc",
        lambda: FakeLibproc(),
    )

    assert _probe_darwin_process_snapshot(4242) == (
        4000,
        "darwin:1786915200:123456",
    )


def test_darwin_unprovable_identity_is_never_signaled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = _group_member(4242, "darwin:1786915200:123456")

    def unavailable(
        _pid: int,
        *,
        expected_style: str | None = None,
    ) -> str | None:
        assert expected_style == "darwin"
        raise HermesRuntimeIdentityError(
            "hermes_runtime_darwin_identity_unavailable"
        )

    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor._probe_process_start_token",
        unavailable,
    )
    monkeypatch.setattr(
        os,
        "kill",
        lambda *_args: pytest.fail("unproven numeric PID was signaled"),
    )

    with pytest.raises(
        HermesRuntimeIdentityError,
        match="hermes_runtime_darwin_identity_unavailable",
    ):
        _signal_process_group_member(member, signal.SIGTERM)


def test_restart_backoff_is_exponential_and_capped() -> None:
    assert _next_restart_backoff(0, initial=0.25, maximum=1) == 0.25
    assert _next_restart_backoff(0.25, initial=0.25, maximum=1) == 0.5
    assert _next_restart_backoff(0.5, initial=0.25, maximum=1) == 1
    assert _next_restart_backoff(1, initial=0.25, maximum=1) == 1


@pytest.mark.parametrize(
    "field_name",
    (
        "startup_timeout_seconds",
        "mcp_probe_timeout_seconds",
        "health_check_interval_seconds",
        "health_check_timeout_seconds",
        "restart_backoff_initial_seconds",
        "restart_backoff_max_seconds",
        "child_term_timeout_seconds",
        "child_kill_timeout_seconds",
        "decision_timeout_seconds",
    ),
)
@pytest.mark.parametrize(
    "value",
    (float("nan"), float("inf"), float("-inf")),
    ids=("nan", "positive-infinity", "negative-infinity"),
)
def test_supervisor_config_rejects_nonfinite_lifecycle_values(
    tmp_path: Path,
    field_name: str,
    value: float,
) -> None:
    with pytest.raises(ValueError, match="must be finite and positive"):
        replace(
            _supervisor_config(tmp_path),
            **{field_name: value},
        )


@pytest.mark.parametrize(
    "field_name",
    (
        "startup_timeout_seconds",
        "mcp_probe_timeout_seconds",
        "health_check_interval_seconds",
        "health_check_timeout_seconds",
        "restart_backoff_initial_seconds",
        "restart_backoff_max_seconds",
        "child_term_timeout_seconds",
        "child_kill_timeout_seconds",
        "decision_timeout_seconds",
    ),
)
@pytest.mark.parametrize("value", (0, -1))
def test_supervisor_config_rejects_nonpositive_lifecycle_values(
    tmp_path: Path,
    field_name: str,
    value: float,
) -> None:
    with pytest.raises(ValueError, match="must be finite and positive"):
        replace(
            _supervisor_config(tmp_path),
            **{field_name: value},
        )


def test_supervisor_config_rejects_inverted_restart_backoff(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ValueError,
        match="restart backoff max must be at least the initial delay",
    ):
        replace(
            _supervisor_config(tmp_path),
            restart_backoff_initial_seconds=2,
            restart_backoff_max_seconds=1,
        )


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    (
        (
            "child_term_timeout_seconds",
            10.001,
            "child TERM timeout must be at most 10 seconds",
        ),
        (
            "child_kill_timeout_seconds",
            5.001,
            "child KILL timeout must be at most 5 seconds",
        ),
        (
            "decision_timeout_seconds",
            300.001,
            "decision timeout must be at most 300 seconds",
        ),
    ),
)
def test_supervisor_config_rejects_child_shutdown_timeout_above_bound(
    tmp_path: Path,
    field_name: str,
    value: float,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(
            _supervisor_config(tmp_path),
            **{field_name: value},
        )


@pytest.mark.parametrize(
    "flag",
    (
        "--startup-timeout",
        "--mcp-probe-timeout",
        "--health-check-interval",
        "--health-check-timeout",
        "--restart-backoff-initial",
        "--restart-backoff-max",
        "--child-term-timeout",
        "--child-kill-timeout",
        "--decision-timeout",
    ),
)
@pytest.mark.parametrize(
    "value",
    ("nan", "inf", "-inf"),
)
def test_supervisor_cli_rejects_nonfinite_lifecycle_values(
    tmp_path: Path,
    flag: str,
    value: str,
) -> None:
    with pytest.raises(SystemExit, match="must be finite and positive"):
        supervisor_main(
            [
                "--hermes-home",
                str(tmp_path / "home"),
                "--vendor-root",
                str(tmp_path / "vendor"),
                f"{flag}={value}",
            ]
        )


@pytest.mark.parametrize(
    "flag",
    (
        "--startup-timeout",
        "--mcp-probe-timeout",
        "--health-check-interval",
        "--health-check-timeout",
        "--restart-backoff-initial",
        "--restart-backoff-max",
        "--child-term-timeout",
        "--child-kill-timeout",
        "--decision-timeout",
    ),
)
@pytest.mark.parametrize("value", ("0", "-1"))
def test_supervisor_cli_rejects_nonpositive_lifecycle_values(
    tmp_path: Path,
    flag: str,
    value: str,
) -> None:
    with pytest.raises(SystemExit, match="must be finite and positive"):
        supervisor_main(
            [
                "--hermes-home",
                str(tmp_path / "home"),
                "--vendor-root",
                str(tmp_path / "vendor"),
                f"{flag}={value}",
            ]
        )


@pytest.mark.parametrize("value", (0, 129))
def test_supervisor_config_rejects_invalid_max_concurrent_responses(
    tmp_path: Path,
    value: int,
) -> None:
    with pytest.raises(
        ValueError,
        match="max concurrent responses must be between 1 and 128",
    ):
        replace(
            _supervisor_config(tmp_path),
            max_concurrent_responses=value,
        )


@pytest.mark.parametrize("value", ("0", "129"))
def test_supervisor_cli_rejects_invalid_max_concurrent_responses(
    tmp_path: Path,
    value: str,
) -> None:
    with pytest.raises(
        SystemExit,
        match="max concurrent responses must be between 1 and 128",
    ):
        supervisor_main(
            [
                "--hermes-home",
                str(tmp_path / "home"),
                "--vendor-root",
                str(tmp_path / "vendor"),
                f"--max-concurrent-responses={value}",
            ]
        )


def test_supervisor_cli_rejects_inverted_restart_backoff(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        SystemExit,
        match="restart backoff max must be at least the initial delay",
    ):
        supervisor_main(
            [
                "--hermes-home",
                str(tmp_path / "home"),
                "--vendor-root",
                str(tmp_path / "vendor"),
                "--restart-backoff-initial=2",
                "--restart-backoff-max=1",
            ]
        )


@pytest.mark.parametrize(
    ("flag", "value", "message"),
    (
        (
            "--child-term-timeout",
            "10.001",
            "child TERM timeout must be at most 10 seconds",
        ),
        (
            "--child-kill-timeout",
            "5.001",
            "child KILL timeout must be at most 5 seconds",
        ),
        (
            "--decision-timeout",
            "300.001",
            "decision timeout must be at most 300 seconds",
        ),
    ),
)
def test_supervisor_cli_rejects_child_shutdown_timeout_above_bound(
    tmp_path: Path,
    flag: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(SystemExit, match=message):
        supervisor_main(
            [
                "--hermes-home",
                str(tmp_path / "home"),
                "--vendor-root",
                str(tmp_path / "vendor"),
                f"{flag}={value}",
            ]
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("60", 60.0),
        ("60.0", 60.0),
        ("6e1", 60.0),
        ("+60", 60.0),
        (" 60 ", 60.0),
        ("1_0", 10.0),
    ),
)
def test_shutdown_budget_numbers_follow_pydantic_numeric_formats(
    value: str,
    expected: float,
) -> None:
    assert _parse_pydantic_float(value, label="test") == expected


def test_supervisor_persists_the_validated_startup_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget_path = tmp_path / "runtime" / "stop-budget"
    captured: list[HermesRuntimeSupervisorConfig] = []
    published: list[HermesRuntimeShutdownBudgetRecord] = []

    class FakeServer:
        def __init__(
            self,
            publication: _RuntimeShutdownBudgetPublication,
        ) -> None:
            self.publication = publication

        def run(self) -> None:
            assert not budget_path.exists()
            self.publication.publish()
            published.append(load_runtime_shutdown_budget(budget_path))
            self.publication.remove_if_owned()

    def build_server(
        _controller: HermesRuntimeProcess,
        config: HermesRuntimeSupervisorConfig,
        *,
        shutdown_budget_publication: (
            _RuntimeShutdownBudgetPublication | None
        ) = None,
    ) -> FakeServer:
        captured.append(config)
        assert shutdown_budget_publication is not None
        return FakeServer(shutdown_budget_publication)

    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor._build_supervisor_server",
        build_server,
    )
    monkeypatch.setenv(
        "HEALTHMES_DECISION_HERMES_MAX_ITERATION_TIMEOUT_SECONDS",
        "1",
    )

    supervisor_main(
        [
            "--hermes-home",
            str(tmp_path / "home"),
            "--vendor-root",
            str(tmp_path / "vendor"),
            "--decision-timeout=6e1",
            "--child-term-timeout=1_0",
            "--child-kill-timeout=+5",
            "--shutdown-budget-path",
            str(budget_path),
        ]
    )

    assert len(captured) == 1
    assert captured[0].decision_timeout_seconds == 60
    assert captured[0].shutdown_budget.drain_timeout_seconds == 75
    assert len(published) == 1
    assert published[0].drain_timeout_seconds == 75
    assert published[0].supervisor_pid == os.getpid()
    assert published[0].supervisor_start_token.startswith(
        ("linux:", "darwin:")
    )
    assert published[0].launcher_pid == published[0].supervisor_pid
    assert (
        published[0].launcher_start_token
        == published[0].supervisor_start_token
    )
    assert not budget_path.exists()


def test_supervisor_main_preserves_budget_without_cleanup_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget_path = tmp_path / "runtime" / "stop-budget"
    publications: list[_RuntimeShutdownBudgetPublication] = []

    class FailingServer:
        def __init__(
            self,
            publication: _RuntimeShutdownBudgetPublication,
        ) -> None:
            self.publication = publication

        def run(self) -> None:
            self.publication.publish()
            raise RuntimeError("controller cleanup failed")

    def build_server(
        _controller: HermesRuntimeProcess,
        _config: HermesRuntimeSupervisorConfig,
        *,
        shutdown_budget_publication: (
            _RuntimeShutdownBudgetPublication | None
        ) = None,
    ) -> FailingServer:
        assert shutdown_budget_publication is not None
        publications.append(shutdown_budget_publication)
        return FailingServer(shutdown_budget_publication)

    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor._build_supervisor_server",
        build_server,
    )

    with pytest.raises(RuntimeError, match="controller cleanup failed"):
        supervisor_main(
            [
                "--hermes-home",
                str(tmp_path / "home"),
                "--vendor-root",
                str(tmp_path / "vendor"),
                "--shutdown-budget-path",
                str(budget_path),
            ]
        )

    assert len(publications) == 1
    assert load_runtime_shutdown_budget(budget_path) == (
        publications[0].record
    )
    publications[0].remove_if_owned()


def test_publication_preserves_malformed_existing_shutdown_budget(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime" / "stop-budget"
    path.parent.mkdir(parents=True)
    malformed = b"version\t3\ntruncated"
    path.write_bytes(malformed)
    supervisor = capture_runtime_supervisor_identity()
    launcher = capture_runtime_launcher_identity(
        {},
        supervisor_identity=supervisor,
    )
    publication = _RuntimeShutdownBudgetPublication(
        path=path,
        record=HermesRuntimeShutdownBudgetRecord(
            drain_timeout_seconds=75,
            launcher_pid=launcher.pid,
            launcher_start_token=launcher.start_token,
            launcher_service_nonce=launcher.service_nonce,
            supervisor_pid=supervisor.pid,
            supervisor_start_token=supervisor.start_token,
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="refusing to overwrite it without explicit repair",
    ):
        publication.publish()

    assert path.read_bytes() == malformed
    publication.remove_if_owned()
    assert path.read_bytes() == malformed


@pytest.mark.parametrize(
    "path_kind",
    ("symlink", "fifo", "directory", "hardlink"),
)
def test_shutdown_budget_reader_rejects_unsafe_file_types(
    tmp_path: Path,
    path_kind: str,
) -> None:
    path = tmp_path / "runtime" / "stop-budget"
    path.parent.mkdir(parents=True)
    payload = b"version\t3\n"
    if path_kind == "symlink":
        source = tmp_path / "budget-source"
        source.write_bytes(payload)
        path.symlink_to(source)
    elif path_kind == "fifo":
        os.mkfifo(path)
    elif path_kind == "directory":
        path.mkdir()
    else:
        source = tmp_path / "budget-source"
        source.write_bytes(payload)
        os.link(source, path)

    with pytest.raises(ValueError):
        load_runtime_shutdown_budget(path)


def test_shutdown_budget_reader_rejects_oversized_file_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime" / "stop-budget"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x" * 1025)
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.os.read",
        lambda *_args: pytest.fail(
            "oversized shutdown budget must be rejected before reading"
        ),
    )

    with pytest.raises(ValueError, match="shutdown budget is invalid"):
        load_runtime_shutdown_budget(path)


def test_shutdown_budget_reader_rejects_fifo_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime" / "stop-budget"
    path.parent.mkdir(parents=True)
    os.mkfifo(path)
    real_open = os.open

    def guarded_open(
        candidate: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
    ) -> int:
        if Path(candidate) == path:
            pytest.fail("unsafe shutdown budget path must not be opened")
        return real_open(candidate, flags, mode)

    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.os.open",
        guarded_open,
    )

    with pytest.raises(ValueError):
        load_runtime_shutdown_budget(path)


def test_shutdown_budget_reader_rejects_wrong_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime" / "stop-budget"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"version\t3\n")
    owner = os.geteuid()
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.os.geteuid",
        lambda: owner + 1,
    )

    with pytest.raises(ValueError):
        load_runtime_shutdown_budget(path)


@pytest.mark.parametrize(
    "path_kind",
    ("symlink", "fifo", "hardlink"),
)
def test_shutdown_budget_lock_rejects_unsafe_file_types(
    tmp_path: Path,
    path_kind: str,
) -> None:
    path = tmp_path / "runtime" / "stop-budget"
    path.parent.mkdir(parents=True)
    lock_path = path.with_name(f"{path.name}.lock")
    source = tmp_path / "lock-source"
    source.write_bytes(b"")
    if path_kind == "symlink":
        lock_path.symlink_to(source)
    elif path_kind == "fifo":
        os.mkfifo(lock_path)
    else:
        os.link(source, lock_path)

    with pytest.raises(RuntimeError):
        with _exclusive_file_lock(path):
            pytest.fail("unsafe lock path must never be acquired")


def test_shutdown_budget_lock_rejects_path_inode_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime" / "stop-budget"
    path.parent.mkdir(parents=True)
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.write_bytes(b"")
    real_lstat = os.lstat

    def mismatched_lstat(candidate: os.PathLike[str] | str):
        metadata = real_lstat(candidate)
        if Path(candidate) != lock_path:
            return metadata
        fields = list(metadata)
        fields[1] += 1
        return os.stat_result(fields)

    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.os.lstat",
        mismatched_lstat,
    )

    with pytest.raises(RuntimeError):
        with _exclusive_file_lock(path):
            pytest.fail("mismatched lock inode must never be acquired")


@pytest.mark.parametrize("version", (1, 2))
def test_legacy_shutdown_budget_remains_readable_for_owner_protection(
    tmp_path: Path,
    version: int,
) -> None:
    path = tmp_path / "runtime" / "stop-budget"
    lines = [
        f"version\t{version}",
        "drain_timeout_seconds\t75",
        "supervisor_pid\t4242",
        "supervisor_start_token\tps:Mon Aug 17 12:00:00 2026",
        "service_nonce\tservice-nonce",
    ]
    if version == 2:
        lines.append("publication_instance_nonce\tpublication-nonce")
    path.parent.mkdir(parents=True)
    path.write_text("\n".join((*lines, "")), encoding="ascii")

    record = load_runtime_shutdown_budget(path)

    assert record.drain_timeout_seconds == 75
    assert record.launcher_identity == HermesRuntimeLauncherIdentity(
        pid=4242,
        start_token="ps:Mon Aug 17 12:00:00 2026",
        service_nonce="service-nonce",
    )


def _write_legacy_budget_owner(
    path: Path,
    *,
    pid: int,
    start_token: str,
) -> bytes:
    payload = "\n".join(
        (
            "version\t2",
            "drain_timeout_seconds\t75",
            f"supervisor_pid\t{pid}",
            f"supervisor_start_token\t{start_token}",
            "service_nonce\tlegacy-service",
            "publication_instance_nonce\tlegacy-publication",
            "",
        )
    ).encode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _new_shutdown_budget_publication(
    path: Path,
) -> _RuntimeShutdownBudgetPublication:
    supervisor = capture_runtime_supervisor_identity()
    launcher = capture_runtime_launcher_identity(
        {},
        supervisor_identity=supervisor,
    )
    return _RuntimeShutdownBudgetPublication(
        path=path,
        record=HermesRuntimeShutdownBudgetRecord(
            drain_timeout_seconds=75,
            launcher_pid=launcher.pid,
            launcher_start_token=launcher.start_token,
            launcher_service_nonce=launcher.service_nonce,
            supervisor_pid=supervisor.pid,
            supervisor_start_token=supervisor.start_token,
        ),
    )


@pytest.mark.parametrize(
    "probe_error",
    (
        OSError("simulated ps execution failure"),
        subprocess.TimeoutExpired(cmd=("ps",), timeout=1),
    ),
    ids=("os-error", "timeout"),
)
def test_publication_preserves_live_legacy_owner_when_ps_probe_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe_error: BaseException,
) -> None:
    path = tmp_path / "runtime" / "stop-budget"
    original = _write_legacy_budget_owner(
        path,
        pid=os.getpid(),
        start_token="ps:Mon Aug 17 12:00:00 2026",
    )
    publication = _new_shutdown_budget_publication(path)

    def fail_ps_probe(*_args: Any, **_kwargs: Any) -> Any:
        raise probe_error

    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.subprocess.run",
        fail_ps_probe,
    )

    with pytest.raises(
        RuntimeError,
        match="owner identity is unprovable",
    ):
        publication.publish()

    assert path.read_bytes() == original
    publication.remove_if_owned()
    assert path.read_bytes() == original


def test_publication_replaces_legacy_budget_only_after_owner_is_proved_gone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime" / "stop-budget"
    legacy_pid = 987_654_321
    _write_legacy_budget_owner(
        path,
        pid=legacy_pid,
        start_token="ps:Mon Aug 17 12:00:00 2026",
    )
    publication = _new_shutdown_budget_publication(path)
    real_kill = os.kill

    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ("ps",))
        ),
    )

    def prove_absent(pid: int, sent_signal: int) -> None:
        if pid == legacy_pid and sent_signal == 0:
            raise ProcessLookupError
        real_kill(pid, sent_signal)

    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.os.kill",
        prove_absent,
    )

    publication.publish()

    assert load_runtime_shutdown_budget(path) == publication.record
    publication.remove_if_owned()
    assert not path.exists()


def test_publication_preserves_legacy_budget_after_identity_token_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime" / "stop-budget"
    original = _write_legacy_budget_owner(
        path,
        pid=os.getpid(),
        start_token="ps:Mon Aug 17 12:00:00 2026",
    )
    publication = _new_shutdown_budget_publication(path)
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=("ps",),
            returncode=0,
            stdout="Tue Aug 18 12:00:00 2026\n",
            stderr="",
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="owner identity is unprovable",
    ):
        publication.publish()

    assert path.read_bytes() == original
    publication.remove_if_owned()
    assert path.read_bytes() == original


def test_publication_rejects_matching_live_legacy_budget_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime" / "stop-budget"
    expected_token = "ps:Mon Aug 17 12:00:00 2026"
    original = _write_legacy_budget_owner(
        path,
        pid=os.getpid(),
        start_token=expected_token,
    )
    publication = _new_shutdown_budget_publication(path)
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=("ps",),
            returncode=0,
            stdout=f"{expected_token.removeprefix('ps:')}\n",
            stderr="",
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="shutdown budget already has a live owner",
    ):
        publication.publish()

    assert path.read_bytes() == original
    publication.remove_if_owned()
    assert path.read_bytes() == original


def test_publication_preserves_native_owner_until_absence_is_proved(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime" / "stop-budget"
    supervisor = capture_runtime_supervisor_identity()
    launcher = capture_runtime_launcher_identity(
        {},
        supervisor_identity=supervisor,
    )
    token_kind, _, token_value = supervisor.start_token.partition(":")
    if token_kind == "linux":
        changed_token = f"linux:{int(token_value) + 1}"
    else:
        seconds, microseconds = token_value.split(":", 1)
        changed_token = (
            f"darwin:{seconds}:{(int(microseconds) + 1) % 1_000_000:06d}"
        )
    existing = HermesRuntimeShutdownBudgetRecord(
        drain_timeout_seconds=75,
        launcher_pid=launcher.pid,
        launcher_start_token=launcher.start_token,
        launcher_service_nonce=launcher.service_nonce,
        supervisor_pid=supervisor.pid,
        supervisor_start_token=changed_token,
    )
    path.parent.mkdir(parents=True)
    original = existing.to_bytes()
    path.write_bytes(original)
    publication = _new_shutdown_budget_publication(path)

    with pytest.raises(
        RuntimeError,
        match="owner disappearance is not proven",
    ):
        publication.publish()

    assert path.read_bytes() == original


def test_inherited_launcher_identity_must_match_live_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = HermesRuntimeProcessIdentity(
        pid=5252,
        start_token="darwin:1786915200:123456",
    )
    identity = HermesRuntimeLauncherIdentity(
        pid=4242,
        start_token="ps:Mon Aug 17 12:00:00 2026",
        service_nonce="service-nonce",
    )
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor._probe_process_start_token",
        lambda pid, expected_style=None: (
            identity.start_token if pid == identity.pid else None
        ),
    )

    captured = capture_runtime_launcher_identity(
        {
            "HEALTHMES_SERVICE_PID": str(identity.pid),
            "HEALTHMES_SERVICE_START_TOKEN": identity.start_token,
            "HEALTHMES_SERVICE_NONCE": identity.service_nonce,
        },
        supervisor_identity=supervisor,
    )

    assert captured == identity


@pytest.mark.parametrize(
    "pid",
    ("1", "04242", "+4242", " 4242", "4242 ", "2147483648", "9" * 128),
)
def test_inherited_launcher_identity_rejects_noncanonical_pid(
    pid: str,
) -> None:
    supervisor = HermesRuntimeProcessIdentity(
        pid=5252,
        start_token="darwin:1786915200:123456",
    )

    with pytest.raises(ValueError, match="launcher PID is invalid"):
        capture_runtime_launcher_identity(
            {
                "HEALTHMES_SERVICE_PID": pid,
                "HEALTHMES_SERVICE_START_TOKEN": (
                    "ps:Mon Aug 17 12:00:00 2026"
                ),
                "HEALTHMES_SERVICE_NONCE": "service-nonce",
            },
            supervisor_identity=supervisor,
        )


@pytest.mark.asyncio
async def test_uvicorn_shutdown_keeps_budget_when_controller_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime" / "stop-budget"
    supervisor = capture_runtime_supervisor_identity()
    launcher = capture_runtime_launcher_identity(
        {},
        supervisor_identity=supervisor,
    )
    publication = _RuntimeShutdownBudgetPublication(
        path=path,
        record=HermesRuntimeShutdownBudgetRecord(
            drain_timeout_seconds=75,
            launcher_pid=launcher.pid,
            launcher_start_token=launcher.start_token,
            launcher_service_nonce=launcher.service_nonce,
            supervisor_pid=supervisor.pid,
            supervisor_start_token=supervisor.start_token,
        ),
    )

    controller = HermesRuntimeProcess(_supervisor_config(tmp_path))
    controller._lifecycle_state = "running"

    async def fail_descendant_cleanup() -> None:
        raise RuntimeError("descendant cleanup failed")

    monkeypatch.setattr(
        controller,
        "_stop_child",
        fail_descendant_cleanup,
    )

    server = _build_supervisor_server(
        controller,
        _supervisor_config(tmp_path),
        shutdown_budget_publication=publication,
    )
    publication.publish()
    with pytest.raises(RuntimeError, match="descendant cleanup failed"):
        await server._shutdown_coordinator.aclose()

    async def noop_shutdown(
        _server: uvicorn.Server,
        sockets: list[Any] | None = None,
    ) -> None:
        del sockets

    monkeypatch.setattr(uvicorn.Server, "shutdown", noop_shutdown)
    await server.shutdown()

    assert controller._lifecycle_state == "close_failed"
    assert load_runtime_shutdown_budget(path) == publication.record
    publication.remove_if_owned()


@pytest.mark.asyncio
async def test_uvicorn_shutdown_removes_budget_after_verified_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime" / "stop-budget"
    supervisor = capture_runtime_supervisor_identity()
    launcher = capture_runtime_launcher_identity(
        {},
        supervisor_identity=supervisor,
    )
    publication = _RuntimeShutdownBudgetPublication(
        path=path,
        record=HermesRuntimeShutdownBudgetRecord(
            drain_timeout_seconds=75,
            launcher_pid=launcher.pid,
            launcher_start_token=launcher.start_token,
            launcher_service_nonce=launcher.service_nonce,
            supervisor_pid=supervisor.pid,
            supervisor_start_token=supervisor.start_token,
        ),
    )

    class CleanController:
        def begin_closing(self) -> None:
            return None

        async def aclose(self) -> None:
            return None

    server = _build_supervisor_server(
        CleanController(),  # type: ignore[arg-type]
        _supervisor_config(tmp_path),
        shutdown_budget_publication=publication,
    )
    publication.publish()
    await server._shutdown_coordinator.aclose()

    async def noop_shutdown(
        _server: uvicorn.Server,
        sockets: list[Any] | None = None,
    ) -> None:
        del sockets

    monkeypatch.setattr(uvicorn.Server, "shutdown", noop_shutdown)
    await server.shutdown()

    assert not path.exists()


@pytest.mark.asyncio
async def test_failed_competing_startup_cannot_replace_ready_owner_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime" / "stop-budget"
    supervisor = capture_runtime_supervisor_identity()
    launcher = capture_runtime_launcher_identity(
        {},
        supervisor_identity=supervisor,
    )
    inherited_record = HermesRuntimeShutdownBudgetRecord(
        drain_timeout_seconds=75,
        launcher_pid=launcher.pid,
        launcher_start_token=launcher.start_token,
        launcher_service_nonce=launcher.service_nonce,
        supervisor_pid=supervisor.pid,
        supervisor_start_token=supervisor.start_token,
    )
    ready_publication = _RuntimeShutdownBudgetPublication(
        path=path,
        record=inherited_record,
    )
    competing_publication = _RuntimeShutdownBudgetPublication(
        path=path,
        record=inherited_record,
    )
    assert (
        ready_publication.record.supervisor_identity
        == competing_publication.record.supervisor_identity
    )
    assert (
        ready_publication.record.publication_instance_nonce
        != competing_publication.record.publication_instance_nonce
    )
    controller = SimpleNamespace()
    config = _supervisor_config(tmp_path)

    async def ready_startup(
        server: uvicorn.Server,
        sockets: list[Any] | None = None,
    ) -> None:
        del sockets
        assert (
            load_runtime_shutdown_budget(path)
            == ready_publication.record
        )
        server.started = True

    monkeypatch.setattr(uvicorn.Server, "startup", ready_startup)
    ready_server = _build_supervisor_server(
        controller,  # type: ignore[arg-type]
        config,
        shutdown_budget_publication=ready_publication,
    )
    await ready_server.startup()
    assert load_runtime_shutdown_budget(path) == ready_publication.record

    failed_startup_called = False

    async def failed_startup(
        _server: uvicorn.Server,
        sockets: list[Any] | None = None,
    ) -> None:
        nonlocal failed_startup_called
        del sockets
        failed_startup_called = True
        raise OSError("port already owned")

    monkeypatch.setattr(uvicorn.Server, "startup", failed_startup)
    competing_server = _build_supervisor_server(
        controller,  # type: ignore[arg-type]
        config,
        shutdown_budget_publication=competing_publication,
    )
    with pytest.raises(
        RuntimeError,
        match="shutdown budget already has a live owner",
    ):
        await competing_server.startup()
    competing_publication.remove_if_owned()

    assert failed_startup_called is False
    assert load_runtime_shutdown_budget(path) == ready_publication.record
    ready_publication.remove_if_owned()
    assert not path.exists()


def test_unpublished_competitor_cannot_delete_identical_budget_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime" / "stop-budget"
    supervisor = capture_runtime_supervisor_identity()
    launcher = capture_runtime_launcher_identity(
        {},
        supervisor_identity=supervisor,
    )
    inherited_record = HermesRuntimeShutdownBudgetRecord(
        drain_timeout_seconds=75,
        launcher_pid=launcher.pid,
        launcher_start_token=launcher.start_token,
        launcher_service_nonce=launcher.service_nonce,
        supervisor_pid=supervisor.pid,
        supervisor_start_token=supervisor.start_token,
    )
    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.secrets.token_hex",
        lambda _length: "forced-publication-collision",
    )
    ready_publication = _RuntimeShutdownBudgetPublication(
        path=path,
        record=inherited_record,
    )
    failed_competitor = _RuntimeShutdownBudgetPublication(
        path=path,
        record=inherited_record,
    )
    assert ready_publication.record == failed_competitor.record

    ready_publication.publish()
    failed_competitor.remove_if_owned()

    assert load_runtime_shutdown_budget(path) == ready_publication.record
    ready_publication.remove_if_owned()
    assert not path.exists()


def test_parent_health_remains_observable_while_runtime_recovers() -> None:
    class RecoveringController:
        observable_started = False

        @property
        def state(self):
            raise HermesRuntimeIdentityError(
                "hermes_runtime_child_not_running"
            )

        def revalidate(self):
            return self.state

        async def attest(self):
            return self.state

        async def start(self) -> None:
            raise AssertionError("blocking startup must not be used")

        async def start_observable(self) -> None:
            self.observable_started = True

        async def aclose(self) -> None:
            return None

    controller = RecoveringController()
    with TestClient(create_supervisor_app(controller)) as client:  # type: ignore[arg-type]
        response = client.get("/healthmes/runtime-health")

    assert controller.observable_started is True
    assert response.status_code == 503
