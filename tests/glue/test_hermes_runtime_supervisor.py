"""Lifecycle regressions for the HealthMes-owned Hermes supervisor."""

from __future__ import annotations

import asyncio
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
    create_supervisor_app,
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
async def test_response_lease_blocks_child_generation_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = HermesRuntimeProcess(_supervisor_config(tmp_path))
    child = SimpleNamespace(returncode=None)
    state = object()
    process._process = child
    process._state = state  # type: ignore[assignment]
    process._launch_argv = ("fake-hermes",)
    process._child_generation = 7
    process._healthy = True

    async def attest_locked():
        return state

    monkeypatch.setattr(process, "_attest_locked", attest_locked)
    lease = await process.acquire_response_lease()
    replacement_waiting = asyncio.Event()
    replaced = asyncio.Event()

    async def replace_child() -> None:
        replacement_waiting.set()
        async with process._child_lock:
            process._child_generation += 1
            replaced.set()

    replacement = asyncio.create_task(replace_child())
    await asyncio.wait_for(replacement_waiting.wait(), timeout=1)
    await asyncio.sleep(0)

    assert lease.generation == 7
    assert not replaced.is_set()

    lease.release()
    lease.release()
    await asyncio.wait_for(replacement, timeout=1)

    assert replaced.is_set()
    assert process._child_generation == 8


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
async def test_cancelled_close_waiting_for_child_lock_cannot_orphan_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = HermesRuntimeProcess(_supervisor_config(tmp_path))
    child = SimpleNamespace(returncode=None)
    process._process = child
    process._state = object()  # type: ignore[assignment]
    process._launch_argv = ("fake-hermes",)
    process._healthy = True

    async def stop_child() -> None:
        process._process = None
        process._state = None
        process._launch_argv = None
        process._healthy = False

    monkeypatch.setattr(process, "_stop_child", stop_child)
    await process._child_lock.acquire()
    try:
        close_task = asyncio.create_task(process.aclose())
        while process._lifecycle_state != "closing":
            await asyncio.sleep(0)
        close_task.cancel()
        await asyncio.sleep(0)

        assert process._lifecycle_state == "closing"
        assert process._process is child
    finally:
        process._child_lock.release()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(close_task, timeout=1)

    assert process._process is None
    assert process._lifecycle_state == "closed"


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
    "flag",
    (
        "--startup-timeout",
        "--mcp-probe-timeout",
        "--health-check-interval",
        "--health-check-timeout",
        "--restart-backoff-initial",
        "--restart-backoff-max",
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
