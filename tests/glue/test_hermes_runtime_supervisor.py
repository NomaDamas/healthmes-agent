"""Lifecycle regressions for the HealthMes-owned Hermes supervisor."""

from __future__ import annotations

import asyncio
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


def test_restart_backoff_is_exponential_and_capped() -> None:
    assert _next_restart_backoff(0, initial=0.25, maximum=1) == 0.25
    assert _next_restart_backoff(0.25, initial=0.25, maximum=1) == 0.5
    assert _next_restart_backoff(0.5, initial=0.25, maximum=1) == 1
    assert _next_restart_backoff(1, initial=0.25, maximum=1) == 1


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
