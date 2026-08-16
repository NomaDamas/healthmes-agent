"""Public HealthMes decision entrypoint combining reasoning and finalization."""

from __future__ import annotations

import asyncio
import logging
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


def _consume_task_result(task: asyncio.Future[Any]) -> None:
    try:
        task.exception()
    except BaseException:
        pass


class HealthMesDecisionEngine:
    """Own one natural-language decision through durable finalization.

    Public calls are bound to one caller event loop while requests are active.
    Caller cancellation does not cancel the underlying decision: shutdown must
    still be able to drain an accepted request through source validation and
    DecisionRecord persistence.
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
    ) -> DecisionResult:
        run = await self._agent.ask(request)
        async_finalize = getattr(self._finalizer, "afinalize", None)
        if callable(async_finalize):
            return await async_finalize(request, run)
        return await asyncio.to_thread(
            self._finalizer.finalize,
            request,
            run,
        )

    def _track_request(
        self,
        loop: asyncio.AbstractEventLoop,
        request: DecisionRequest,
    ) -> asyncio.Task[DecisionResult]:
        if not isinstance(request, DecisionRequest):
            raise TypeError("request must be a DecisionRequest")
        task_name = f"healthmes-decision-{request.request_id}"
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
            request_coroutine = self._run_request(request)
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
            return task

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

        task = self._track_request(asyncio.get_running_loop(), request)
        return await asyncio.shield(task)

    async def ask(self, request: DecisionRequest) -> DecisionResult:
        """Compatibility wrapper for the canonical ``ask_wellness`` API."""

        return await self.ask_wellness(request)

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
