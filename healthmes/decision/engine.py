"""Public HealthMes decision entrypoint combining reasoning and finalization."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable
from threading import Lock
from typing import Any, Protocol

from healthmes.decision.agent import DecisionAgentRun
from healthmes.decision.contracts import DecisionRequest, DecisionResult
from healthmes.decision.finalizer import DecisionFinalizer

_LOGGER = logging.getLogger(__name__)
_DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 66.0
_DEFAULT_SHUTDOWN_CANCEL_GRACE_SECONDS = 0.25


class DecisionEngineClosedError(RuntimeError):
    """Raised when a new decision is submitted after shutdown begins."""


class DecisionEngineBusyError(RuntimeError):
    """Raised when the bounded Decision Agent admission queue is full."""


class DecisionAgent(Protocol):
    """Minimal agent contract owned by the public Decision Engine."""

    def ask(
        self,
        request: DecisionRequest,
    ) -> Awaitable[DecisionAgentRun]: ...

    def close(self) -> None: ...


class PersistedDecisionFinalizer(Protocol):
    """Finalizer surface needed for safe idempotent pointer replay."""

    def revalidate_persisted(
        self,
        request: DecisionRequest,
        decision_record_id: uuid.UUID,
    ) -> DecisionResult: ...

    async def arevalidate_persisted(
        self,
        request: DecisionRequest,
        decision_record_id: uuid.UUID,
    ) -> DecisionResult: ...


class _RequestPhase:
    """Coordinate caller cancellation with the finalization commit boundary."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._finalization_started = False
        self._cancellation_requested = False

    def begin_finalization(self) -> bool:
        """Enter the durable phase unless caller cancellation won the race."""

        with self._lock:
            if self._cancellation_requested:
                return False
            self._finalization_started = True
            return True

    def cancel_reasoning(
        self,
        task: asyncio.Task[DecisionResult],
    ) -> bool:
        """Cancel only while the request is still inside model reasoning."""

        with self._lock:
            if self._finalization_started:
                return False
            self._cancellation_requested = True
            task.cancel()
            return True


def _consume_task_result(task: asyncio.Future[Any]) -> None:
    try:
        task.exception()
    except BaseException:
        pass


class HealthMesDecisionEngine:
    """Own one natural-language decision through durable finalization.

    Public calls are bound to one caller event loop while requests are active.
    Caller cancellation stops model reasoning and therefore closes an active
    Hermes Responses stream. Once finalization starts, cancellation no longer
    interrupts the durable commit boundary; shutdown drains that accepted
    finalization exactly as before.
    """

    def __init__(
        self,
        *,
        agent: DecisionAgent,
        finalizer: DecisionFinalizer,
        max_pending_requests: int = 8,
        shutdown_timeout_seconds: float = (
            _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
        ),
        shutdown_cancel_grace_seconds: float = (
            _DEFAULT_SHUTDOWN_CANCEL_GRACE_SECONDS
        ),
    ) -> None:
        if max_pending_requests < 1:
            raise ValueError("max_pending_requests must be positive")
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown timeout must be positive")
        if (
            shutdown_cancel_grace_seconds < 0
            or shutdown_cancel_grace_seconds
            > shutdown_timeout_seconds
        ):
            raise ValueError(
                "shutdown cancel grace must fit within shutdown timeout"
            )
        self._agent = agent
        self._finalizer = finalizer
        self._max_pending_requests = max_pending_requests
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._shutdown_cancel_grace_seconds = (
            shutdown_cancel_grace_seconds
        )
        self._state_lock = Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active: set[asyncio.Task[DecisionResult]] = set()
        self._shutdown_task: asyncio.Task[None] | None = None
        self._closing = False
        self._closed = False

    async def astart(self) -> None:
        """Run an optional runtime readiness probe before serving requests."""

        with self._state_lock:
            if self._closing or self._closed:
                raise DecisionEngineClosedError(
                    "HealthMes decision engine is closing"
                )
        start_agent = getattr(self._agent, "start", None)
        if callable(start_agent):
            await start_agent()

    async def _run_request(
        self,
        request: DecisionRequest,
        phase: _RequestPhase,
    ) -> DecisionResult:
        run = await self._agent.ask(request)
        if not phase.begin_finalization():
            raise asyncio.CancelledError
        async_finalize = getattr(self._finalizer, "afinalize", None)
        if callable(async_finalize):
            return await async_finalize(request, run)
        return await asyncio.to_thread(
            self._finalizer.finalize,
            request,
            run,
        )

    async def _run_persisted_replay(
        self,
        request: DecisionRequest,
        decision_record_id: uuid.UUID,
    ) -> DecisionResult:
        async_revalidate = getattr(
            self._finalizer,
            "arevalidate_persisted",
            None,
        )
        if callable(async_revalidate):
            return await async_revalidate(
                request,
                decision_record_id,
            )
        revalidate = getattr(
            self._finalizer,
            "revalidate_persisted",
            None,
        )
        if not callable(revalidate):
            raise RuntimeError(
                "decision finalizer cannot revalidate persisted results"
            )
        return await asyncio.to_thread(
            revalidate,
            request,
            decision_record_id,
        )

    def _track_request(
        self,
        loop: asyncio.AbstractEventLoop,
        request: DecisionRequest,
    ) -> tuple[asyncio.Task[DecisionResult], _RequestPhase]:
        if not isinstance(request, DecisionRequest):
            raise TypeError("request must be a DecisionRequest")
        task_name = f"healthmes-decision-{request.request_id}"
        phase = _RequestPhase()
        with self._state_lock:
            if self._closing or self._closed:
                raise DecisionEngineClosedError(
                    "HealthMes decision engine is closing"
                )
            if (
                self._loop is not None
                and self._loop is not loop
                and self._active
            ):
                raise RuntimeError(
                    "HealthMes decision engine has active requests on "
                    "another event loop"
                )
            if len(self._active) >= self._max_pending_requests:
                raise DecisionEngineBusyError(
                    "HealthMes decision engine is at capacity"
                )
            self._loop = loop
            request_coroutine = self._run_request(request, phase)
            try:
                task = loop.create_task(
                    request_coroutine,
                    name=task_name,
                )
            except BaseException:
                request_coroutine.close()
                raise
            self._active.add(task)
            task.add_done_callback(self._request_finished)
            return task, phase

    def _request_finished(
        self,
        task: asyncio.Task[DecisionResult],
    ) -> None:
        with self._state_lock:
            self._active.discard(task)
        _consume_task_result(task)

    async def ask_wellness(
        self,
        request: DecisionRequest,
    ) -> DecisionResult:
        """Run one accepted request through reasoning and final persistence."""

        task, phase = self._track_request(
            asyncio.get_running_loop(),
            request,
        )
        try:
            done, _pending = await asyncio.wait((task,))
        except asyncio.CancelledError:
            phase.cancel_reasoning(task)
            raise
        assert task in done
        return task.result()

    async def ask(self, request: DecisionRequest) -> DecisionResult:
        """Compatibility wrapper for the canonical ``ask_wellness`` API."""

        return await self.ask_wellness(request)

    async def replay_persisted_decision(
        self,
        request: DecisionRequest,
        decision_record_id: uuid.UUID,
    ) -> DecisionResult:
        """Revalidate one stored pointer without invoking the LLM again."""

        if not isinstance(request, DecisionRequest):
            raise TypeError("request must be a DecisionRequest")
        if not isinstance(decision_record_id, uuid.UUID):
            raise TypeError("decision_record_id must be a UUID")
        loop = asyncio.get_running_loop()
        with self._state_lock:
            if self._closing or self._closed:
                raise DecisionEngineClosedError(
                    "HealthMes decision engine is closing"
                )
            if (
                self._loop is not None
                and self._loop is not loop
                and self._active
            ):
                raise RuntimeError(
                    "HealthMes decision engine has active requests on "
                    "another event loop"
                )
            if len(self._active) >= self._max_pending_requests:
                raise DecisionEngineBusyError(
                    "HealthMes decision engine is at capacity"
                )
            self._loop = loop
            replay_coroutine = self._run_persisted_replay(
                request,
                decision_record_id,
            )
            try:
                task = loop.create_task(
                    replay_coroutine,
                    name=f"healthmes-replay-{decision_record_id}",
                )
            except BaseException:
                replay_coroutine.close()
                raise
            self._active.add(task)
            task.add_done_callback(self._request_finished)
        try:
            done, _pending = await asyncio.wait((task,))
        except asyncio.CancelledError as exc:
            cancellation = exc
            task.cancel()
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError as repeated:
                    cancellation = repeated
                    if not task.done():
                        task.cancel()
            with self._state_lock:
                self._active.discard(task)
            _consume_task_result(task)
            raise cancellation
        assert task in done
        return task.result()

    async def aclose(self) -> None:
        """Reject new work and await the engine's single shutdown task."""

        loop = asyncio.get_running_loop()
        with self._state_lock:
            shutdown_task = self._shutdown_task
            if shutdown_task is None:
                if (
                    self._loop is not None
                    and self._loop is not loop
                    and self._active
                ):
                    raise RuntimeError(
                        "HealthMes decision engine must be closed on the "
                        "event loop that owns its active requests"
                    )
                self._loop = loop
                self._closing = True
                shutdown_task = loop.create_task(
                    self._shutdown(),
                    name="healthmes-decision-shutdown",
                )
                shutdown_task.add_done_callback(_consume_task_result)
                self._shutdown_task = shutdown_task
            elif not shutdown_task.done() and shutdown_task.get_loop() is not loop:
                raise RuntimeError(
                    "HealthMes decision engine shutdown belongs to another "
                    "event loop"
                )

        if shutdown_task.done():
            shutdown_task.result()
            return
        await asyncio.shield(shutdown_task)

    async def _shutdown(self) -> None:
        with self._state_lock:
            active = tuple(self._active)
        try:
            if active:
                drain_timeout = max(
                    0.0,
                    self._shutdown_timeout_seconds
                    - self._shutdown_cancel_grace_seconds,
                )
                _done, pending = await asyncio.wait(
                    active,
                    timeout=drain_timeout,
                )
                if pending:
                    begin_finalizer_shutdown = getattr(
                        self._finalizer,
                        "begin_shutdown",
                        None,
                    )
                    if callable(begin_finalizer_shutdown):
                        begin_finalizer_shutdown()
                    else:
                        abort_active = getattr(
                            self._finalizer,
                            "abort_active",
                            None,
                        )
                        if callable(abort_active):
                            abort_active()
                    for task in pending:
                        task.cancel()
                    _cancelled, still_pending = await asyncio.wait(
                        pending,
                        timeout=self._shutdown_cancel_grace_seconds,
                    )
                    if still_pending:
                        _LOGGER.error(
                            "Decision Engine shutdown left %d cancelled "
                            "task(s) pending after the bounded grace period",
                            len(still_pending),
                        )
            close_finalizer = getattr(self._finalizer, "aclose", None)
            if callable(close_finalizer):
                await close_finalizer()
            else:
                begin_finalizer_shutdown = getattr(
                    self._finalizer,
                    "begin_shutdown",
                    None,
                )
                if callable(begin_finalizer_shutdown):
                    begin_finalizer_shutdown()
                drain_finalizer = getattr(
                    self._finalizer,
                    "adrain",
                    None,
                )
                if callable(drain_finalizer):
                    await drain_finalizer()
        finally:
            try:
                close_agent = getattr(self._agent, "aclose", None)
                if callable(close_agent):
                    await close_agent()
                else:
                    self._agent.close()
            finally:
                with self._state_lock:
                    self._closed = True

    def close(self) -> None:
        """Synchronously close only when no event loop is running.

        Async applications must use ``await aclose()`` so in-flight requests
        can be drained without deadlocking the caller event loop.
        """

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.aclose())
            return
        raise RuntimeError(
            "HealthMesDecisionEngine.close() cannot run inside an active "
            "event loop; await aclose() instead"
        )

    async def __aenter__(self) -> HealthMesDecisionEngine:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()
