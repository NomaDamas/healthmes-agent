"""Lifecycle regressions for the HealthMes-owned Hermes supervisor."""

from __future__ import annotations

import asyncio
import signal
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from healthmes.hermes_runtime_identity import HermesRuntimeIdentityError
from healthmes.hermes_runtime_supervisor import (
    HermesRuntimeProcess,
    HermesRuntimeSupervisorConfig,
    _next_restart_backoff,
    _parse_pydantic_float,
    _ProcessGroupMember,
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

    def kill_group(pid: int, sent: signal.Signals) -> None:
        signals.append((pid, sent))
        if sent == signal.SIGKILL:
            group.clear()

    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.os.killpg",
        kill_group,
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
    signals: list[signal.Signals] = []

    def kill_group(_pid: int, sent: signal.Signals) -> None:
        signals.append(sent)
        if sent == signal.SIGTERM:
            child.returncode = 0
            group.discard(leader)
        else:
            group.clear()

    monkeypatch.setattr(
        "healthmes.hermes_runtime_supervisor.os.killpg",
        kill_group,
    )

    await process.aclose()

    assert signals == [signal.SIGTERM, signal.SIGKILL]
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
        "healthmes.hermes_runtime_supervisor.os.killpg",
        replace_after_term,
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
        "healthmes.hermes_runtime_supervisor.os.killpg",
        lambda _pid, sent: signals.append(sent),
    )

    with pytest.raises(
        RuntimeError,
        match="hermes_runtime_child_kill_timeout",
    ):
        await asyncio.wait_for(process.aclose(), timeout=1)

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert process._process is child
    assert process._lifecycle_state == "close_failed"


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

    class FakeServer:
        def run(self) -> None:
            return None

    def build_server(
        _controller: HermesRuntimeProcess,
        config: HermesRuntimeSupervisorConfig,
    ) -> FakeServer:
        captured.append(config)
        return FakeServer()

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
    assert load_runtime_shutdown_budget(budget_path) == 75
    assert budget_path.read_bytes() == b"75\n"


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
