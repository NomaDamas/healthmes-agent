"""Lifecycle regressions for the HealthMes-owned Hermes supervisor."""

from __future__ import annotations

import asyncio
import ctypes
import os
import signal
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
    _next_restart_backoff,
    _parse_pydantic_float,
    _probe_darwin_process_snapshot,
    _probe_process_group_members,
    _ProcessGroupMember,
    _run_runtime_process_action,
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
        server.started = True

    monkeypatch.setattr(uvicorn.Server, "startup", ready_startup)
    ready_server = _build_supervisor_server(
        controller,  # type: ignore[arg-type]
        config,
        shutdown_budget_publication=ready_publication,
    )
    await ready_server.startup()
    assert load_runtime_shutdown_budget(path) == ready_publication.record

    async def failed_startup(
        _server: uvicorn.Server,
        sockets: list[Any] | None = None,
    ) -> None:
        del sockets
        raise OSError("port already owned")

    monkeypatch.setattr(uvicorn.Server, "startup", failed_startup)
    competing_server = _build_supervisor_server(
        controller,  # type: ignore[arg-type]
        config,
        shutdown_budget_publication=competing_publication,
    )
    with pytest.raises(OSError, match="port already owned"):
        await competing_server.startup()
    competing_publication.remove_if_owned()

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
