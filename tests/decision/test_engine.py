from __future__ import annotations

import asyncio
import gc
import threading
import warnings
from datetime import UTC, datetime

import pytest

from healthmes.decision import (
    DecisionCaller,
    DecisionEngineBusyError,
    DecisionEngineClosedError,
    DecisionRequest,
    DecisionResult,
    DecisionStatus,
    ExecutionScope,
    HealthMesDecisionEngine,
    PersistenceStatus,
    RuntimeMetadata,
)

NOW = datetime(2026, 8, 12, 6, tzinfo=UTC)


def _request() -> DecisionRequest:
    return DecisionRequest(
        question="Should I keep working?",
        requested_at=NOW,
        timezone="UTC",
        caller=DecisionCaller(
            principal_id="owner",
            authenticated=True,
            execution_scope=ExecutionScope.LOCAL,
        ),
    )


def _result(request: DecisionRequest) -> DecisionResult:
    return DecisionResult(
        request_id=request.request_id,
        turn_id=request.turn_id,
        status=DecisionStatus.COMPLETED,
        answer="Take a short break.",
        persistence_status=PersistenceStatus.NOT_REQUIRED,
        runtime=RuntimeMetadata(
            runtime="scripted",
            model="engine-test-v1",
        ),
    )


class _StubAgent:
    def __init__(self) -> None:
        self.closed = False
        self.close_calls = 0

    async def ask(self, request: DecisionRequest) -> object:
        return request

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


class _BlockingFinalizer:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def finalize(
        self,
        request: DecisionRequest,
        _run: object,
    ) -> DecisionResult:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test finalizer was not released")
        return _result(request)


class _BlockingAsyncFinalizer:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.abort_calls = 0

    async def afinalize(
        self,
        request: DecisionRequest,
        _run: object,
    ) -> DecisionResult:
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()
        return _result(request)

    def abort_active(self) -> None:
        self.abort_calls += 1


class _UnknownCommitFinalizer:
    def __init__(self) -> None:
        self.shutdown_started = False
        self.drain_started = asyncio.Event()
        self.release = asyncio.Event()

    async def afinalize(
        self,
        request: DecisionRequest,
        _run: object,
    ) -> DecisionResult:
        return DecisionResult(
            request_id=request.request_id,
            turn_id=request.turn_id,
            status=DecisionStatus.FAILED,
            limitations=["decision_finalization_outcome_unknown"],
            persistence_status=PersistenceStatus.UNKNOWN,
            runtime=RuntimeMetadata(runtime="test-finalizer"),
        )

    def begin_shutdown(self) -> None:
        self.shutdown_started = True

    async def adrain(self) -> None:
        assert self.shutdown_started is True
        self.drain_started.set()
        await self.release.wait()


async def test_aclose_rejects_new_requests_and_drains_accepted_work() -> None:
    agent = _StubAgent()
    finalizer = _BlockingFinalizer()
    engine = HealthMesDecisionEngine(
        agent=agent,  # type: ignore[arg-type]
        finalizer=finalizer,  # type: ignore[arg-type]
    )
    accepted = asyncio.create_task(engine.ask_wellness(_request()))
    assert await asyncio.to_thread(finalizer.started.wait, 1)

    closing = asyncio.create_task(engine.aclose())
    await asyncio.sleep(0)

    with pytest.raises(DecisionEngineClosedError):
        await engine.ask_wellness(_request())
    assert closing.done() is False
    assert agent.closed is False

    finalizer.release.set()
    result = await accepted
    await closing

    assert result.status is DecisionStatus.COMPLETED
    assert agent.closed is True
    await engine.aclose()


async def test_aclose_drains_commit_worker_after_unknown_response() -> None:
    agent = _StubAgent()
    finalizer = _UnknownCommitFinalizer()
    engine = HealthMesDecisionEngine(
        agent=agent,  # type: ignore[arg-type]
        finalizer=finalizer,  # type: ignore[arg-type]
    )

    result = await engine.ask_wellness(_request())
    assert result.persistence_status is PersistenceStatus.UNKNOWN

    closing = asyncio.create_task(engine.aclose())
    await asyncio.wait_for(finalizer.drain_started.wait(), timeout=1)

    assert closing.done() is False
    assert agent.closed is False

    finalizer.release.set()
    await closing

    assert agent.closed is True
    await engine.aclose()


async def test_caller_cancellation_does_not_cancel_finalization() -> None:
    agent = _StubAgent()
    finalizer = _BlockingFinalizer()
    engine = HealthMesDecisionEngine(
        agent=agent,  # type: ignore[arg-type]
        finalizer=finalizer,  # type: ignore[arg-type]
    )
    caller = asyncio.create_task(engine.ask_wellness(_request()))
    assert await asyncio.to_thread(finalizer.started.wait, 1)

    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    closing = asyncio.create_task(engine.aclose())
    await asyncio.sleep(0)
    assert closing.done() is False

    finalizer.release.set()
    await closing

    assert agent.closed is True


async def test_capacity_rejects_excess_requests_without_starting_them() -> None:
    agent = _StubAgent()
    finalizer = _BlockingFinalizer()
    engine = HealthMesDecisionEngine(
        agent=agent,  # type: ignore[arg-type]
        finalizer=finalizer,  # type: ignore[arg-type]
        max_pending_requests=1,
    )
    accepted = asyncio.create_task(engine.ask_wellness(_request()))
    assert await asyncio.to_thread(finalizer.started.wait, 1)

    with pytest.raises(DecisionEngineBusyError):
        await engine.ask_wellness(_request())

    finalizer.release.set()
    assert (await accepted).status is DecisionStatus.COMPLETED
    await engine.aclose()


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_pending_requests"):
        HealthMesDecisionEngine(
            agent=_StubAgent(),  # type: ignore[arg-type]
            finalizer=_BlockingFinalizer(),  # type: ignore[arg-type]
            max_pending_requests=0,
        )


async def test_cancelled_first_close_cannot_abandon_shutdown() -> None:
    agent = _StubAgent()
    finalizer = _BlockingFinalizer()
    engine = HealthMesDecisionEngine(
        agent=agent,  # type: ignore[arg-type]
        finalizer=finalizer,  # type: ignore[arg-type]
    )
    accepted = asyncio.create_task(engine.ask_wellness(_request()))
    assert await asyncio.to_thread(finalizer.started.wait, 1)

    first_close = asyncio.create_task(engine.aclose())
    await asyncio.sleep(0)
    first_close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_close

    second_close = asyncio.create_task(engine.aclose())
    await asyncio.sleep(0)
    assert second_close.done() is False
    assert agent.closed is False

    finalizer.release.set()
    assert (await accepted).status is DecisionStatus.COMPLETED
    await second_close

    assert agent.closed is True
    assert agent.close_calls == 1


async def test_aclose_aborts_stuck_async_finalization_within_bound() -> None:
    agent = _StubAgent()
    finalizer = _BlockingAsyncFinalizer()
    engine = HealthMesDecisionEngine(
        agent=agent,  # type: ignore[arg-type]
        finalizer=finalizer,  # type: ignore[arg-type]
        shutdown_timeout_seconds=0.1,
        shutdown_cancel_grace_seconds=0.05,
    )
    accepted = asyncio.create_task(engine.ask_wellness(_request()))
    await asyncio.wait_for(finalizer.started.wait(), timeout=1)

    started = asyncio.get_running_loop().time()
    await engine.aclose()
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.5
    assert finalizer.abort_calls == 1
    assert finalizer.cancelled.is_set()
    assert agent.closed is True
    with pytest.raises(asyncio.CancelledError):
        await accepted
    await engine.aclose()


async def test_concurrent_and_repeated_close_share_one_shutdown() -> None:
    agent = _StubAgent()

    class ImmediateFinalizer:
        def finalize(
            self,
            request: DecisionRequest,
            _run: object,
        ) -> DecisionResult:
            return _result(request)

    engine = HealthMesDecisionEngine(
        agent=agent,  # type: ignore[arg-type]
        finalizer=ImmediateFinalizer(),  # type: ignore[arg-type]
    )

    await asyncio.gather(engine.aclose(), engine.aclose(), engine.aclose())
    await engine.aclose()

    assert agent.closed is True
    assert agent.close_calls == 1


async def test_close_failure_is_stable_and_agent_close_runs_once() -> None:
    class FailingAgent(_StubAgent):
        def close(self) -> None:
            super().close()
            raise RuntimeError("agent close failed")

    class UnusedFinalizer:
        def finalize(
            self,
            request: DecisionRequest,
            _run: object,
        ) -> DecisionResult:
            raise AssertionError(request)

    agent = FailingAgent()
    engine = HealthMesDecisionEngine(
        agent=agent,  # type: ignore[arg-type]
        finalizer=UnusedFinalizer(),  # type: ignore[arg-type]
    )

    first, second = await asyncio.gather(
        engine.aclose(),
        engine.aclose(),
        return_exceptions=True,
    )
    assert isinstance(first, RuntimeError)
    assert isinstance(second, RuntimeError)
    assert str(first) == str(second) == "agent close failed"

    with pytest.raises(RuntimeError, match="agent close failed"):
        await engine.aclose()
    with pytest.raises(DecisionEngineClosedError):
        await engine.ask_wellness(_request())
    assert agent.closed is True
    assert agent.close_calls == 1


async def test_malformed_request_does_not_leak_a_coroutine() -> None:
    agent = _StubAgent()

    class UnusedFinalizer:
        def finalize(
            self,
            request: DecisionRequest,
            _run: object,
        ) -> DecisionResult:
            raise AssertionError(request)

    engine = HealthMesDecisionEngine(
        agent=agent,  # type: ignore[arg-type]
        finalizer=UnusedFinalizer(),  # type: ignore[arg-type]
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(TypeError, match="DecisionRequest"):
            await engine.ask_wellness(object())  # type: ignore[arg-type]
        gc.collect()

    assert not any(
        "was never awaited" in str(item.message)
        for item in caught
    )
    await engine.aclose()


async def test_sync_close_is_rejected_inside_an_event_loop() -> None:
    agent = _StubAgent()

    class ImmediateFinalizer:
        def finalize(
            self,
            request: DecisionRequest,
            _run: object,
        ) -> DecisionResult:
            return _result(request)

    engine = HealthMesDecisionEngine(
        agent=agent,  # type: ignore[arg-type]
        finalizer=ImmediateFinalizer(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="await aclose"):
        engine.close()

    await engine.aclose()
    assert agent.closed is True


def test_sync_close_works_without_a_running_event_loop() -> None:
    agent = _StubAgent()

    class UnusedFinalizer:
        def finalize(
            self,
            request: DecisionRequest,
            _run: object,
        ) -> DecisionResult:
            raise AssertionError(request)

    engine = HealthMesDecisionEngine(
        agent=agent,  # type: ignore[arg-type]
        finalizer=UnusedFinalizer(),  # type: ignore[arg-type]
    )

    engine.close()
    engine.close()

    assert agent.closed is True
    assert agent.close_calls == 1
