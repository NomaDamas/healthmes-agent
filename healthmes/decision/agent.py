"""HealthMes-owned natural-language decision orchestration."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import Future as ConcurrentFuture
from concurrent.futures import InvalidStateError as ConcurrentInvalidStateError
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy.orm import Session

from healthmes.decision.access import (
    AccessAuditEntry,
    ContextAccessLayer,
    ContextAccessPolicy,
    ContextAccessTurn,
)
from healthmes.decision.contracts import (
    ContextQuery,
    ContextResult,
    ContextStatus,
    DecisionDraft,
    DecisionPersistenceIntent,
    DecisionRequest,
    DecisionStatus,
    ExecutionScope,
    PrivacyLevel,
    RuntimeMetadata,
    SourceRef,
    ToolCallRecord,
    ToolCallStatus,
)
from healthmes.decision.providers import (
    ContextParameterFormat,
    ContextParameterSpec,
    validate_context_parameters,
)
from healthmes.decision.runtime import (
    ContextToolCall,
    DecisionRuntime,
    DecisionRuntimeContractError,
    DecisionRuntimeTurn,
    DecisionRuntimeUnavailableError,
    DecisionToolCallError,
    DecisionToolSpec,
    RuntimeContextResult,
    RuntimeDecisionContextHints,
    RuntimeDecisionRequest,
    RuntimeRelatedRecord,
    RuntimeResourceBudget,
    RuntimeStepOutput,
    RuntimeToolExchange,
)
from healthmes.decision.validation import strict_model_validate

HEALTHMES_DECISION_SYSTEM_POLICY_VERSION = "healthmes-decision-policy.v4"
HEALTHMES_DECISION_SYSTEM_POLICY = """
You are one interchangeable reasoning runtime for the HealthMes Decision Agent.

HealthMes, not the model runtime, owns authorization, retention, privacy,
exact calculations, source provenance, step/tool budgets, deadlines, and final
persistence. For this one model iteration you must:

1. Interpret the user's natural-language question and choose only from the
   supplied capability catalog. Do not use a fixed question-kind routing table.
2. Return exactly one action: one or more tool calls, or one final
   DecisionDraft. Never return both in the same iteration.
3. Never access a database, provider, registry, filesystem, or network source
   directly. Request HealthMes context only through the declared tool calls.
4. Use prior RuntimeToolExchange results to decide whether another domain or
   narrower time range is necessary. Do not call unrelated domains by default.
5. Treat provider totals, time boundaries, units, and specialized hard limits
   as authoritative. Do not recompute or silently replace them.
6. Treat missing, unknown, unavailable, stale, and partial data as uncertainty,
   never as zero or proof that the user is well.
7. Ask a concrete clarification question when a required candidate amount,
   product identity, time, or user fact is absent.
8. Use only source reference IDs present in prior tool results. A completed
   answer based on context that has source references must cite at least one.
   Never invent, transform, or infer a source reference ID.
9. Keep observations, uncertainty, trade-offs, and the proposed action
   distinct. Do not claim medical diagnosis or certainty.
10. Classify persistence explicitly. Use `none` for simple lookup or
    explanation, `action` for a concrete behavior recommendation, and `risk`
    only for an actionable safety warning. This read-only runtime cannot prove
    a mutation, and `explicit_tracking` is only advisory: HealthMes verifies a
    trusted request flag. Never persist merely because source data was
    consulted.
11. When persistence is requested by the user or your persistence intent is
    not `none`, include a separately written `record_summary` of at most 160
    characters. It must preserve the compact conclusion without copying or
    truncating the answer and must omit raw identifiers and sensitive detail.
12. Keep the final answer concise. Return structured data matching the
    supplied runtime contract. HealthMes validates source references and
    conditionally persists a compact record after this loop.
""".strip()

_PRIVACY_RANK = {
    PrivacyLevel.AGGREGATE: 0,
    PrivacyLevel.IDENTITY: 1,
    PrivacyLevel.SCOPED_RAW: 2,
}
_BUDGET_FAILURES = {
    "decision_tool_call_budget_exhausted",
    "turn_context_byte_budget_exhausted",
    "turn_source_ref_budget_exhausted",
    "turn_tool_call_budget_exhausted",
}
_INTERNAL_TOOL_FAILURES = {
    "access_policy_resolution_failed",
    "invalid_provider_query",
    "provider_contract_violation",
    "provider_execution_failed",
    "tool_execution_failed",
}
_ACTIVE_CONSENT_FAILURES = frozenset(
    {
        "caller_not_authenticated",
        "caller_not_policy_owner",
        "domain_consent_changed",
        "domain_consent_denied",
        "execution_scope_denied",
    }
)

SessionFactory = Callable[[], AbstractContextManager[Session]]
AccessPolicyResolver = Callable[[DecisionRequest], ContextAccessPolicy]


def _as_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )


def _wall_finished_at(started_at: datetime) -> datetime:
    return max(datetime.now(UTC), _as_utc(started_at))


class _HardDeadlineExceeded(RuntimeError):
    pass


class _ChildTaskCancelled(RuntimeError):
    pass


class _WorkerUnavailable(RuntimeError):
    pass


def _ensure_before_deadline(deadline: float) -> None:
    if monotonic() >= deadline:
        raise _HardDeadlineExceeded


def _consume_task_result(task: asyncio.Future[Any]) -> None:
    try:
        task.result()
    except BaseException:
        pass


async def _await_before_deadline[T](
    future: ConcurrentFuture[T],
    *,
    deadline: float,
) -> T:
    """Await a worker future without trusting its cancellation behavior."""

    task = asyncio.wrap_future(future)
    remaining = deadline - monotonic()
    if remaining <= 0:
        future.cancel()
        task.add_done_callback(_consume_task_result)
        raise _HardDeadlineExceeded

    try:
        done, _ = await asyncio.wait((task,), timeout=remaining)
    except asyncio.CancelledError:
        future.cancel()
        task.add_done_callback(_consume_task_result)
        raise

    if task in done:
        try:
            result = task.result()
        except asyncio.CancelledError as exc:
            raise _ChildTaskCancelled from exc
        if monotonic() > deadline:
            raise _HardDeadlineExceeded
        return result

    future.cancel()
    task.add_done_callback(_consume_task_result)
    raise _HardDeadlineExceeded


class _IsolatedAsyncWorker:
    """Runs one agent's turns on one stable, isolated event loop.

    A hard timeout quarantines this worker permanently. The owning agent never
    replaces it, so uninterruptible sync code can orphan at most one daemon
    thread per agent and later requests fail fast instead of leaking workers.
    """

    def __init__(self) -> None:
        self._ready = Event()
        self._closing = Event()
        self._quarantined = Event()
        self._cancel_pending = Event()
        self._timeout_cancel_pending = Event()
        self._stopped = Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._turn_lock: asyncio.Lock | None = None
        self._startup_error: BaseException | None = None
        self._active = 0
        self._thread = Thread(
            target=self._thread_main,
            name="healthmes-decision-worker",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=5):
            self.close()
            raise RuntimeError("decision worker did not start")
        if self._startup_error is not None or self._loop is None:
            raise RuntimeError("decision worker failed to start") from (
                self._startup_error
            )

    async def run[T](
        self,
        operation_factory: Callable[[], Awaitable[T]],
        *,
        deadline: float,
    ) -> T:
        await self._wait_until_ready(deadline=deadline)
        await self._resolve_timed_out_turn(deadline=deadline)
        self.ensure_available()
        loop = self._loop
        assert loop is not None
        started = Event()
        finished = Event()
        future = self._submit(
            operation_factory,
            started=started,
            finished=finished,
        )
        try:
            return await self._await_submitted_turn(
                future,
                deadline=deadline,
                started=started,
            )
        except _HardDeadlineExceeded:
            if self._cancel_pending.is_set():
                self.quarantine()
            elif started.is_set() and not finished.is_set():
                self._timeout_cancel_pending.set()
                if finished.is_set():
                    self._timeout_cancel_pending.clear()
            raise
        except asyncio.CancelledError:
            if started.is_set() and not finished.is_set():
                self._cancel_pending.set()
                if finished.is_set():
                    self._cancel_pending.clear()
            raise

    async def _await_submitted_turn[T](
        self,
        future: ConcurrentFuture[T],
        *,
        deadline: float,
        started: Event,
    ) -> T:
        waiter = asyncio.create_task(
            _await_before_deadline(
                future,
                deadline=deadline,
            )
        )
        try:
            while True:
                done, _ = await asyncio.wait(
                    (waiter,),
                    timeout=0.005,
                )
                if waiter in done:
                    return waiter.result()
                if (
                    not started.is_set()
                    and self._timeout_cancel_pending.is_set()
                ):
                    await self._resolve_timed_out_turn(
                        deadline=deadline
                    )
                    self.ensure_available()
        finally:
            if not waiter.done():
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)

    async def _resolve_timed_out_turn(self, *, deadline: float) -> None:
        if not self._timeout_cancel_pending.is_set():
            return
        grace_deadline = min(deadline, monotonic() + 0.05)
        while self._timeout_cancel_pending.is_set():
            remaining = grace_deadline - monotonic()
            if remaining <= 0:
                self.quarantine()
                raise _WorkerUnavailable(
                    "timed-out decision turn did not stop"
                )
            await asyncio.sleep(min(remaining, 0.001))

    async def _wait_until_ready(self, *, deadline: float) -> None:
        while not self._ready.is_set():
            remaining = deadline - monotonic()
            if remaining <= 0:
                self.quarantine()
                raise _HardDeadlineExceeded
            await asyncio.sleep(min(remaining, 0.001))
        if self._startup_error is not None or self._loop is None:
            raise _WorkerUnavailable(
                "decision worker failed to start"
            ) from self._startup_error

    def _submit[T](
        self,
        operation_factory: Callable[[], Awaitable[T]],
        *,
        started: Event,
        finished: Event,
    ) -> ConcurrentFuture[T]:
        """Create the coroutine only after the worker loop accepts the turn."""

        loop = self._loop
        if loop is None:
            raise _WorkerUnavailable("decision worker is unavailable")
        result: ConcurrentFuture[T] = ConcurrentFuture()
        task_holder: list[asyncio.Task[T]] = []

        def cancel_on_worker(done: ConcurrentFuture[T]) -> None:
            if not done.cancelled():
                return

            def cancel_task() -> None:
                if task_holder and not task_holder[0].done():
                    task_holder[0].cancel()

            try:
                loop.call_soon_threadsafe(cancel_task)
            except RuntimeError:
                pass

        def complete(task: asyncio.Task[T]) -> None:
            try:
                if result.done():
                    _consume_task_result(task)
                elif task.cancelled():
                    result.cancel()
                else:
                    try:
                        value = task.result()
                    except BaseException as exc:
                        try:
                            result.set_exception(exc)
                        except ConcurrentInvalidStateError:
                            pass
                    else:
                        try:
                            result.set_result(value)
                        except ConcurrentInvalidStateError:
                            pass
            finally:
                if (
                    self._closing.is_set()
                    or self._quarantined.is_set()
                ):
                    self._stop_if_idle()

        def start() -> None:
            if result.cancelled():
                return
            try:
                task = loop.create_task(
                    self._execute(
                        operation_factory,
                        started=started,
                        finished=finished,
                    )
                )
            except BaseException as exc:
                if not result.done():
                    result.set_exception(exc)
                return
            task_holder.append(task)
            task.add_done_callback(complete)

        result.add_done_callback(cancel_on_worker)
        try:
            loop.call_soon_threadsafe(start)
        except RuntimeError as exc:
            result.set_exception(exc)
        return result

    def ensure_available(self) -> None:
        if (
            self._loop is None
            or self._closing.is_set()
            or self._quarantined.is_set()
            or self._stopped.is_set()
        ):
            raise _WorkerUnavailable("decision worker is unavailable")

    def ensure_startable(self) -> None:
        if (
            self._closing.is_set()
            or self._quarantined.is_set()
            or self._stopped.is_set()
            or (
                self._ready.is_set()
                and (
                    self._startup_error is not None
                    or self._loop is None
                )
            )
        ):
            raise _WorkerUnavailable("decision worker is unavailable")

    def quarantine(self) -> None:
        if self._quarantined.is_set():
            return
        self._quarantined.set()
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._stop_if_idle)
        except RuntimeError:
            pass

    def close(self) -> None:
        if self._closing.is_set():
            return
        self._closing.set()
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._stop_if_idle)
        except RuntimeError:
            pass

    async def _execute[T](
        self,
        operation_factory: Callable[[], Awaitable[T]],
        *,
        started: Event,
        finished: Event,
    ) -> T:
        turn_lock = self._turn_lock
        if turn_lock is None:
            raise _WorkerUnavailable("decision worker is unavailable")
        async with turn_lock:
            self.ensure_available()
            started.set()
            self._active += 1
            try:
                return await operation_factory()
            finally:
                finished.set()
                self._cancel_pending.clear()
                self._timeout_cancel_pending.clear()
                self._active -= 1

    def _stop_if_idle(self) -> None:
        if self._active == 0 and (
            self._closing.is_set() or self._quarantined.is_set()
        ):
            asyncio.get_running_loop().stop()

    def _thread_main(self) -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._turn_lock = asyncio.Lock()
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
            self._stopped.set()
            return

        self._ready.set()
        if self._closing.is_set() or self._quarantined.is_set():
            loop.call_soon(self._stop_if_idle)
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.close()
            self._stopped.set()


class DecisionAgentRun(BaseModel):
    """Pre-finalization output of one HealthMes decision turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: uuid.UUID
    turn_id: uuid.UUID
    draft: DecisionDraft
    source_refs: tuple[SourceRef, ...] = Field(default=(), max_length=500)
    runtime: RuntimeMetadata
    steps_used: int = Field(default=0, ge=0, le=32)
    tool_trace: tuple[ToolCallRecord, ...] = Field(
        default=(),
        max_length=64,
    )
    access_trace: tuple[AccessAuditEntry, ...] = Field(
        default=(),
        max_length=64,
    )
    system_policy_version: str = Field(min_length=1, max_length=128)
    started_at: AwareDatetime
    finished_at: AwareDatetime

    @field_validator("started_at", "finished_at", mode="after")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _as_utc(value)

    @model_validator(mode="after")
    def validate_run(self) -> DecisionAgentRun:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not be before started_at")
        reference_ids = [item.reference_id for item in self.source_refs]
        if len(reference_ids) != len(set(reference_ids)):
            raise ValueError("source_refs must be unique")
        if self.draft.status in {
            DecisionStatus.COMPLETED,
            DecisionStatus.NEEDS_CLARIFICATION,
        } and self.steps_used == 0:
            raise ValueError("successful runtime output requires a step")
        return self


class _TurnProgress:
    """Thread-safe progress snapshot used when the API deadline wins."""

    def __init__(
        self,
        *,
        fallback_request_id: uuid.UUID,
        fallback_turn_id: uuid.UUID,
        fallback_started_at: datetime,
    ) -> None:
        self._lock = Lock()
        self._request_id = fallback_request_id
        self._turn_id = fallback_turn_id
        self._started_at = fallback_started_at
        self._steps_started = 0
        self._executor: _ToolExecutor | None = None
        self._access_turn: ContextAccessTurn | None = None
        self._closed = False

    def set_validated_request(self, request: DecisionRequest) -> None:
        with self._lock:
            self._request_id = request.request_id
            self._turn_id = request.turn_id

    def set_started_at(self, started_at: datetime) -> None:
        with self._lock:
            self._started_at = started_at

    def attach(
        self,
        *,
        executor: _ToolExecutor,
        access_turn: ContextAccessTurn,
    ) -> None:
        with self._lock:
            self._executor = executor
            self._access_turn = access_turn
            closed = self._closed
        if closed:
            executor.close()

    def start_step(self, step_number: int) -> None:
        with self._lock:
            self._steps_started = max(self._steps_started, step_number)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            executor = self._executor
        if executor is not None:
            executor.close()

    def snapshot(self) -> _TurnProgressSnapshot:
        with self._lock:
            request_id = self._request_id
            turn_id = self._turn_id
            started_at = self._started_at
            steps_started = self._steps_started
            executor = self._executor
            access_turn = self._access_turn
        return _TurnProgressSnapshot(
            request_id=request_id,
            turn_id=turn_id,
            started_at=started_at,
            steps_started=steps_started,
            tool_trace=executor.trace if executor is not None else (),
            access_trace=(
                access_turn.trace if access_turn is not None else ()
            ),
        )


@dataclass(frozen=True)
class _TurnProgressSnapshot:
    request_id: uuid.UUID
    turn_id: uuid.UUID
    started_at: datetime
    steps_started: int
    tool_trace: tuple[ToolCallRecord, ...]
    access_trace: tuple[AccessAuditEntry, ...]


@dataclass(frozen=True)
class _RelatedRecordBinding:
    reference: str
    domain: str
    identity: str
    record_ids: tuple[str, ...]


@dataclass(frozen=True)
class _RuntimeRecordAlias:
    reference: str
    record_ids: tuple[str, ...]
    uuid_value: uuid.UUID | None


@dataclass(frozen=True)
class _RuntimeAliasMatcher:
    pattern: re.Pattern[str] | None
    references: tuple[str, ...]


@dataclass(frozen=True)
class _ExecutedTool:
    call: ContextToolCall
    result: ContextResult


class _ToolExecutor:
    """HealthMes-only gateway executor; runtimes never receive this object."""

    def __init__(
        self,
        *,
        request: DecisionRequest,
        tools: tuple[DecisionToolSpec, ...],
        access_turn: ContextAccessTurn,
        related_records: tuple[_RelatedRecordBinding, ...],
        provider_parameter_specs: Mapping[
            str,
            tuple[ContextParameterSpec, ...],
        ],
        session_factory: SessionFactory,
        policy_resolver: AccessPolicyResolver,
        clock: Callable[[], datetime],
        deadline: float,
    ) -> None:
        self.request = request
        self._tools = {item.capability: item for item in tools}
        self._access_turn = access_turn
        self._related_records = {
            item.reference: item for item in related_records
        }
        self._provider_parameter_specs = {
            capability: {
                spec.name: spec
                for spec in parameter_specs
            }
            for capability, parameter_specs
            in provider_parameter_specs.items()
        }
        self._session_factory = session_factory
        self._policy_resolver = policy_resolver
        self._clock = clock
        self._deadline = deadline
        self._fingerprints: set[str] = set()
        self._trace: list[ToolCallRecord] = []
        self._attempts = 0
        self._closed = Event()
        self._state_lock = Lock()
        self._fatal_code: str | None = None
        self._fatal_status: DecisionStatus | None = None

    @property
    def trace(self) -> tuple[ToolCallRecord, ...]:
        with self._state_lock:
            return tuple(self._trace)

    @property
    def fatal_code(self) -> str | None:
        with self._state_lock:
            return self._fatal_code

    @property
    def fatal_status(self) -> DecisionStatus | None:
        with self._state_lock:
            return self._fatal_status

    def close(self) -> None:
        self._closed.set()

    def _ensure_active(self) -> None:
        if self._closed.is_set():
            raise DecisionToolCallError("decision_turn_closed")
        _ensure_before_deadline(self._deadline)

    async def invoke(
        self,
        raw_call: ContextToolCall | Mapping[str, Any],
    ) -> _ExecutedTool:
        self._ensure_active()

        started_at = _as_utc(self._clock())
        with self._state_lock:
            self._attempts += 1
            attempts = self._attempts
        try:
            call = strict_model_validate(
                ContextToolCall,
                raw_call,
            )
        except ValidationError as exc:
            self._set_fatal(
                "malformed_tool_arguments",
                DecisionStatus.FAILED,
            )
            raise DecisionToolCallError(
                "malformed_tool_arguments"
            ) from exc
        self._ensure_active()

        spec = self._tools.get(call.capability)
        provider_id = (
            spec.provider_id if spec is not None else "unregistered"
        )
        parameters = dict(call.parameters)
        if spec is not None:
            try:
                parameters = validate_context_parameters(
                    parameters,
                    spec.parameter_specs,
                )
                for parameter_spec in spec.parameter_specs:
                    if (
                        parameter_spec.format
                        is not ContextParameterFormat.RELATED_RECORD_REF
                        or parameter_spec.name not in parameters
                    ):
                        continue
                    reference = parameters[parameter_spec.name]
                    if not isinstance(reference, str):
                        raise ValueError(
                            "related record reference must be a string"
                        )
                    binding = self._related_records.get(reference)
                    if binding is None:
                        raise ValueError(
                            "related record reference is unknown"
                        )
                    provider_spec = self._provider_parameter_specs.get(
                        call.capability,
                        {},
                    ).get(parameter_spec.name)
                    if provider_spec is None:
                        raise ValueError(
                            "related record provider schema is unavailable"
                        )
                    parameters[parameter_spec.name] = (
                        _provider_related_record_id(
                            binding,
                            parameter_spec=provider_spec,
                        )
                    )
            except ValueError as exc:
                self._set_fatal(
                    "malformed_tool_arguments",
                    DecisionStatus.FAILED,
                )
                raise DecisionToolCallError(
                    "malformed_tool_arguments"
                ) from exc
        self._ensure_active()
        try:
            query = ContextQuery(
                provider_id=provider_id,
                capability=call.capability,
                start=call.start,
                end=call.end,
                timezone=self.request.timezone,
                granularity=call.granularity,
                fields=list(call.fields),
                privacy_level=call.privacy_level,
                limit=call.limit,
                parameters=parameters,
                purpose=call.purpose,
            )
        except ValidationError as exc:
            self._set_fatal(
                "malformed_tool_arguments",
                DecisionStatus.FAILED,
            )
            raise DecisionToolCallError(
                "malformed_tool_arguments"
            ) from exc
        self._ensure_active()

        if attempts > self.request.budget.max_tool_calls:
            return self._failed_execution(
                call,
                query,
                started_at=started_at,
                code="decision_tool_call_budget_exhausted",
                status=DecisionStatus.BLOCKED,
            )
        if spec is None:
            return self._failed_execution(
                call,
                query,
                started_at=started_at,
                code="unknown_tool",
                status=DecisionStatus.FAILED,
            )

        try:
            policy_before = self._resolve_current_policy()
        except DecisionToolCallError as exc:
            return self._failed_execution(
                call,
                query,
                started_at=started_at,
                code=exc.code,
                status=DecisionStatus.FAILED,
            )
        denial = _tool_policy_denial(
            self.request,
            policy_before,
            domain=spec.domain,
        )
        if denial is not None:
            return self._denied_execution(
                call,
                query,
                started_at=started_at,
                code=denial,
            )
        self._access_turn.update_policy(policy_before)
        policy_fingerprint = _tool_policy_fingerprint(
            policy_before,
            domain=spec.domain,
        )
        self._ensure_active()

        fingerprint_payload = call.model_dump(
            mode="json",
            exclude={"purpose"},
        )
        fingerprint_payload["fields"] = sorted(
            fingerprint_payload["fields"]
        )
        fingerprint = json.dumps(
            fingerprint_payload,
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        with self._state_lock:
            duplicate = fingerprint in self._fingerprints
            if not duplicate:
                self._fingerprints.add(fingerprint)
        if duplicate:
            return self._failed_execution(
                call,
                query,
                started_at=started_at,
                code="duplicate_tool_call",
                status=DecisionStatus.FAILED,
            )
        try:
            with self._session_factory() as session:
                self._ensure_active()
                try:
                    raw_result = await self._access_turn.query(
                        session,
                        query,
                        ensure_active=self._ensure_active,
                    )
                    result = strict_model_validate(
                        ContextResult,
                        raw_result,
                    )
                    self._ensure_active()
                    try:
                        policy_after = self._resolve_current_policy()
                    except DecisionToolCallError as exc:
                        result = self._access_turn.deny(
                            query,
                            reason_codes=(exc.code,),
                        )
                        self._set_fatal(
                            exc.code,
                            DecisionStatus.FAILED,
                        )
                    else:
                        self._access_turn.update_policy(policy_after)
                        if (
                            _tool_policy_fingerprint(
                                policy_after,
                                domain=spec.domain,
                            )
                            != policy_fingerprint
                        ):
                            result = self._access_turn.deny(
                                query,
                                reason_codes=(
                                    "domain_consent_changed",
                                ),
                            )
                            self._set_fatal(
                                "domain_consent_changed",
                                DecisionStatus.BLOCKED,
                            )
                    if result.status in {
                        ContextStatus.OK,
                        ContextStatus.PARTIAL,
                    }:
                        session.commit()
                    else:
                        session.rollback()
                except BaseException:
                    session.rollback()
                    raise
            self._ensure_active()
        except (
            asyncio.CancelledError,
            _HardDeadlineExceeded,
            DecisionToolCallError,
        ):
            raise
        except Exception:
            result = ContextResult(
                query_id=query.query_id,
                provider_id=query.provider_id,
                capability=query.capability,
                status=ContextStatus.FAILED,
                limitations=["tool_execution_failed"],
            )

        self._ensure_active()

        finished_at = _as_utc(self._clock())
        if result.status is ContextStatus.DENIED:
            call_status = ToolCallStatus.DENIED
            error_code = None
        elif result.status is ContextStatus.FAILED:
            call_status = ToolCallStatus.FAILED
            error_code = (
                result.limitations[0]
                if result.limitations
                else "tool_execution_failed"
            )
        else:
            call_status = ToolCallStatus.COMPLETED
            error_code = None
        record = ToolCallRecord(
                query=query,
                status=call_status,
                started_at=started_at,
                finished_at=finished_at,
                result=result,
                error_code=error_code,
        )
        with self._state_lock:
            self._trace.append(record)
        budget_code = next(
            (
                code
                for code in result.limitations
                if code in _BUDGET_FAILURES
            ),
            None,
        )
        if budget_code is not None:
            self._set_fatal(
                budget_code,
                DecisionStatus.BLOCKED,
            )
        elif (
            consent_code := next(
                (
                    code
                    for code in result.limitations
                    if code in _ACTIVE_CONSENT_FAILURES
                ),
                None,
            )
        ) is not None:
            self._set_fatal(
                consent_code,
                DecisionStatus.BLOCKED,
            )
        elif result.status is ContextStatus.FAILED:
            fatal_code = (
                error_code
                if error_code in _INTERNAL_TOOL_FAILURES
                else "tool_execution_failed"
            )
            self._set_fatal(
                fatal_code,
                DecisionStatus.FAILED,
            )
        elif "duplicate_tool_call" in result.limitations:
            self._set_fatal(
                "duplicate_tool_call",
                DecisionStatus.FAILED,
            )
        return _ExecutedTool(call=call, result=result)

    def _resolve_current_policy(self) -> ContextAccessPolicy:
        self._ensure_active()
        try:
            policy = strict_model_validate(
                ContextAccessPolicy,
                self._policy_resolver(self.request),
            )
        except Exception as exc:
            raise DecisionToolCallError(
                "access_policy_resolution_failed"
            ) from exc
        self._ensure_active()
        policy_error = _request_policy_error(self.request, policy)
        if policy_error is not None:
            raise DecisionToolCallError(policy_error)
        return policy

    def _denied_execution(
        self,
        call: ContextToolCall,
        query: ContextQuery,
        *,
        started_at: datetime,
        code: str,
    ) -> _ExecutedTool:
        self._set_fatal(code, DecisionStatus.BLOCKED)
        result = self._access_turn.deny(
            query,
            reason_codes=(code,),
        )
        record = ToolCallRecord(
            query=query,
            status=ToolCallStatus.DENIED,
            started_at=started_at,
            finished_at=_as_utc(self._clock()),
            result=result,
        )
        with self._state_lock:
            self._trace.append(record)
        return _ExecutedTool(call=call, result=result)

    def _failed_execution(
        self,
        call: ContextToolCall,
        query: ContextQuery,
        *,
        started_at: datetime,
        code: str,
        status: DecisionStatus,
    ) -> _ExecutedTool:
        self._set_fatal(code, status)
        result = ContextResult(
            query_id=query.query_id,
            provider_id=query.provider_id,
            capability=query.capability,
            status=ContextStatus.FAILED,
            limitations=[code],
        )
        record = ToolCallRecord(
                query=query,
                status=ToolCallStatus.FAILED,
                started_at=started_at,
                finished_at=_as_utc(self._clock()),
                result=result,
                error_code=code,
        )
        with self._state_lock:
            self._trace.append(record)
        return _ExecutedTool(call=call, result=result)

    def _set_fatal(
        self,
        code: str,
        status: DecisionStatus,
    ) -> None:
        with self._state_lock:
            if self._fatal_code is None:
                self._fatal_code = code
                self._fatal_status = status


class HealthMesDecisionAgent:
    """HealthMes-owned entrypoint for adaptive natural-language decisions."""

    def __init__(
        self,
        *,
        access_layer: ContextAccessLayer,
        runtime: DecisionRuntime,
        session_factory: SessionFactory,
        policy_resolver: AccessPolicyResolver,
        timeout_seconds: float = 60,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._access_layer = access_layer
        self._runtime = runtime
        self._session_factory = session_factory
        self._policy_resolver = policy_resolver
        self._timeout_seconds = timeout_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._runtime_metadata = strict_model_validate(
            RuntimeMetadata,
            runtime.metadata,
        )
        self._worker_lock = Lock()
        self._closed = Event()
        self._worker = _IsolatedAsyncWorker()

    def close(self) -> None:
        """Stop this long-lived agent's runtime worker when it is idle."""

        if self._closed.is_set():
            return
        self._closed.set()
        with self._worker_lock:
            worker = self._worker
        worker.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _runtime_worker(self) -> _IsolatedAsyncWorker:
        if self._closed.is_set():
            raise _WorkerUnavailable("decision agent is closed")
        with self._worker_lock:
            self._worker.ensure_startable()
            return self._worker

    async def ask(self, request: DecisionRequest) -> DecisionAgentRun:
        deadline = monotonic() + self._timeout_seconds
        progress = _TurnProgress(
            fallback_request_id=uuid.uuid4(),
            fallback_turn_id=uuid.uuid4(),
            fallback_started_at=datetime.now(UTC),
        )
        fallback_metadata = self._runtime_metadata
        try:
            worker = self._runtime_worker()
        except _WorkerUnavailable:
            snapshot = progress.snapshot()
            return self._failure_run(
                request_id=snapshot.request_id,
                turn_id=snapshot.turn_id,
                started_at=snapshot.started_at,
                metadata=fallback_metadata,
                code="runtime_worker_unavailable",
                finished_at=_wall_finished_at(snapshot.started_at),
            )
        except Exception:
            snapshot = progress.snapshot()
            return self._failure_run(
                request_id=snapshot.request_id,
                turn_id=snapshot.turn_id,
                started_at=snapshot.started_at,
                metadata=fallback_metadata,
                code="runtime_execution_failed",
                finished_at=_wall_finished_at(snapshot.started_at),
            )
        try:
            return await worker.run(
                lambda: self._run_turn(
                    request,
                    progress=progress,
                    deadline=deadline,
                ),
                deadline=deadline,
            )
        except asyncio.CancelledError:
            raise
        except _HardDeadlineExceeded:
            snapshot = progress.snapshot()
            return self._failure_run(
                request_id=snapshot.request_id,
                turn_id=snapshot.turn_id,
                started_at=snapshot.started_at,
                metadata=fallback_metadata,
                code="runtime_timeout",
                steps_used=snapshot.steps_started,
                tool_trace=snapshot.tool_trace,
                access_trace=snapshot.access_trace,
                finished_at=_wall_finished_at(snapshot.started_at),
            )
        except _WorkerUnavailable:
            snapshot = progress.snapshot()
            return self._failure_run(
                request_id=snapshot.request_id,
                turn_id=snapshot.turn_id,
                started_at=snapshot.started_at,
                metadata=fallback_metadata,
                code="runtime_worker_unavailable",
                steps_used=snapshot.steps_started,
                tool_trace=snapshot.tool_trace,
                access_trace=snapshot.access_trace,
                finished_at=_wall_finished_at(snapshot.started_at),
            )
        except Exception:
            snapshot = progress.snapshot()
            return self._failure_run(
                request_id=snapshot.request_id,
                turn_id=snapshot.turn_id,
                started_at=snapshot.started_at,
                metadata=fallback_metadata,
                code="runtime_execution_failed",
                steps_used=snapshot.steps_started,
                tool_trace=snapshot.tool_trace,
                access_trace=snapshot.access_trace,
                finished_at=_wall_finished_at(snapshot.started_at),
            )
        finally:
            progress.close()

    async def _run_turn(
        self,
        raw_request: DecisionRequest,
        *,
        progress: _TurnProgress,
        deadline: float,
    ) -> DecisionAgentRun:
        fallback_metadata = self._runtime_metadata
        _ensure_before_deadline(deadline)
        try:
            request = strict_model_validate(
                DecisionRequest,
                raw_request,
            )
        except Exception:
            snapshot = progress.snapshot()
            return self._failure_run(
                request_id=snapshot.request_id,
                turn_id=snapshot.turn_id,
                started_at=snapshot.started_at,
                metadata=fallback_metadata,
                code="invalid_decision_request",
            )

        _ensure_before_deadline(deadline)
        progress.set_validated_request(request)
        try:
            started_at = _as_utc(self._clock())
        except Exception:
            snapshot = progress.snapshot()
            return self._failure_run(
                request_id=snapshot.request_id,
                turn_id=snapshot.turn_id,
                started_at=snapshot.started_at,
                metadata=fallback_metadata,
                code="runtime_execution_failed",
            )
        _ensure_before_deadline(deadline)
        progress.set_started_at(started_at)

        try:
            policy = strict_model_validate(
                ContextAccessPolicy,
                self._policy_resolver(request),
            )
        except Exception:
            return self._failure_run(
                request,
                started_at=started_at,
                metadata=fallback_metadata,
                code="access_policy_resolution_failed",
            )
        _ensure_before_deadline(deadline)
        policy_error = _request_policy_error(request, policy)
        if policy_error is not None:
            return self._failure_run(
                request,
                started_at=started_at,
                metadata=fallback_metadata,
                code=policy_error,
                status=DecisionStatus.BLOCKED,
            )

        try:
            related_records = _related_record_bindings(
                self._access_layer,
                request=request,
                policy=policy,
            )
            tools = _tool_catalog(
                self._access_layer,
                request=request,
                policy=policy,
                related_records=related_records,
            )
            provider_parameter_specs = _provider_parameter_specs(
                self._access_layer,
                tools=tools,
            )
            access_turn = self._access_layer.start_turn(
                request,
                policy=policy,
                reject_duplicate_effective_queries=True,
            )
        except Exception:
            return self._failure_run(
                request,
                started_at=started_at,
                metadata=fallback_metadata,
                code="provider_catalog_invalid",
            )
        _ensure_before_deadline(deadline)
        executor = _ToolExecutor(
            request=request,
            tools=tools,
            access_turn=access_turn,
            related_records=related_records,
            provider_parameter_specs=provider_parameter_specs,
            session_factory=self._session_factory,
            policy_resolver=self._policy_resolver,
            clock=self._clock,
            deadline=deadline,
        )
        progress.attach(
            executor=executor,
            access_turn=access_turn,
        )
        try:
            return await self._drive(
                request,
                started_at=started_at,
                tools=tools,
                related_records=related_records,
                access_turn=access_turn,
                executor=executor,
                progress=progress,
                deadline=deadline,
            )
        finally:
            executor.close()

    async def _drive(
        self,
        request: DecisionRequest,
        *,
        started_at: datetime,
        tools: tuple[DecisionToolSpec, ...],
        related_records: tuple[_RelatedRecordBinding, ...],
        access_turn: ContextAccessTurn,
        executor: _ToolExecutor,
        progress: _TurnProgress,
        deadline: float,
    ) -> DecisionAgentRun:
        fallback_metadata = self._runtime_metadata
        _ensure_before_deadline(deadline)
        history: list[RuntimeToolExchange] = []
        runtime_aliases = _runtime_record_aliases(
            request,
            related_records=related_records,
        )
        runtime_request = _runtime_request(
            request,
            related_records=related_records,
            runtime_aliases=runtime_aliases,
        )
        # Caller-controlled identifiers never cross the runtime boundary.
        runtime_request_id = uuid.uuid4()
        runtime_turn_id = uuid.uuid4()

        for step_number in range(1, request.budget.max_steps + 1):
            _ensure_before_deadline(deadline)
            progress.start_step(step_number)
            runtime_turn = DecisionRuntimeTurn(
                request_id=runtime_request_id,
                turn_id=runtime_turn_id,
                request=runtime_request.model_copy(deep=True),
                system_policy=HEALTHMES_DECISION_SYSTEM_POLICY,
                system_policy_version=(
                    HEALTHMES_DECISION_SYSTEM_POLICY_VERSION
                ),
                tools=tuple(
                    tool.model_copy(deep=True) for tool in tools
                ),
                history=tuple(
                    exchange.model_copy(deep=True)
                    for exchange in history
                ),
                step_number=step_number,
                remaining_steps=(
                    request.budget.max_steps - step_number + 1
                ),
                resource_budget=RuntimeResourceBudget(
                    max_tool_calls=request.budget.max_tool_calls,
                    remaining_tool_calls=max(
                        0,
                        request.budget.max_tool_calls
                        - access_turn.calls_used,
                    ),
                    max_source_refs=request.budget.max_source_refs,
                    remaining_source_refs=max(
                        0,
                        request.budget.max_source_refs
                        - access_turn.source_refs_used,
                    ),
                    max_context_bytes=request.budget.max_context_bytes,
                    remaining_context_bytes=max(
                        0,
                        request.budget.max_context_bytes
                        - access_turn.context_bytes_used,
                    ),
                ),
                deadline_ms=max(
                    1,
                    int((deadline - monotonic()) * 1_000),
                ),
            )

            try:
                _ensure_before_deadline(deadline)
                raw_output = await self._runtime.next_step(runtime_turn)
                _ensure_before_deadline(deadline)
                output = strict_model_validate(
                    RuntimeStepOutput,
                    raw_output,
                )
                _ensure_before_deadline(deadline)
            except asyncio.CancelledError:
                raise
            except _HardDeadlineExceeded:
                raise
            except DecisionRuntimeUnavailableError as exc:
                return self._failure_run(
                    request,
                    started_at=started_at,
                    metadata=fallback_metadata,
                    code=exc.code,
                    status=DecisionStatus.BLOCKED,
                    steps_used=step_number,
                    tool_trace=executor.trace,
                    access_trace=access_turn.trace,
                )
            except DecisionRuntimeContractError:
                return self._failure_run(
                    request,
                    started_at=started_at,
                    metadata=fallback_metadata,
                    code="runtime_contract_violation",
                    steps_used=step_number,
                    tool_trace=executor.trace,
                    access_trace=access_turn.trace,
                )
            except ValidationError:
                return self._failure_run(
                    request,
                    started_at=started_at,
                    metadata=fallback_metadata,
                    code="runtime_contract_violation",
                    steps_used=step_number,
                    tool_trace=executor.trace,
                    access_trace=access_turn.trace,
                )
            except Exception:
                return self._failure_run(
                    request,
                    started_at=started_at,
                    metadata=fallback_metadata,
                    code="runtime_execution_failed",
                    steps_used=step_number,
                    tool_trace=executor.trace,
                    access_trace=access_turn.trace,
                )

            if output.metadata.runtime != fallback_metadata.runtime:
                return self._failure_run(
                    request,
                    started_at=started_at,
                    metadata=fallback_metadata,
                    code="runtime_identity_mismatch",
                    steps_used=step_number,
                    tool_trace=executor.trace,
                    access_trace=access_turn.trace,
                )
            if (
                fallback_metadata.model is not None
                and output.metadata.model != fallback_metadata.model
            ):
                return self._failure_run(
                    request,
                    started_at=started_at,
                    metadata=fallback_metadata,
                    code="runtime_identity_mismatch",
                    steps_used=step_number,
                    tool_trace=executor.trace,
                    access_trace=access_turn.trace,
                )
            if (
                fallback_metadata.provider is not None
                and output.metadata.provider != fallback_metadata.provider
            ):
                return self._failure_run(
                    request,
                    started_at=started_at,
                    metadata=fallback_metadata,
                    code="runtime_identity_mismatch",
                    steps_used=step_number,
                    tool_trace=executor.trace,
                    access_trace=access_turn.trace,
                )

            if output.draft is not None:
                source_refs = _source_refs(executor.trace)
                available_ids = {
                    source_ref.reference_id
                    for source_ref in source_refs
                }
                if not set(
                    output.draft.used_source_ref_ids
                ).issubset(available_ids):
                    return self._failure_run(
                        request,
                        started_at=started_at,
                        metadata=output.metadata,
                        code="runtime_source_ref_mismatch",
                        steps_used=step_number,
                        tool_trace=executor.trace,
                        access_trace=access_turn.trace,
                    )
                if (
                    output.draft.status is DecisionStatus.COMPLETED
                    and source_refs
                    and not output.draft.used_source_ref_ids
                ):
                    return self._failure_run(
                        request,
                        started_at=started_at,
                        metadata=output.metadata,
                        code="runtime_source_refs_omitted",
                        steps_used=step_number,
                        tool_trace=executor.trace,
                        access_trace=access_turn.trace,
                    )
                return DecisionAgentRun(
                    request_id=request.request_id,
                    turn_id=request.turn_id,
                    draft=output.draft.model_copy(deep=True),
                    source_refs=source_refs,
                    runtime=output.metadata.model_copy(deep=True),
                    steps_used=step_number,
                    tool_trace=executor.trace,
                    access_trace=access_turn.trace,
                    system_policy_version=(
                        HEALTHMES_DECISION_SYSTEM_POLICY_VERSION
                    ),
                    started_at=started_at,
                    finished_at=_as_utc(self._clock()),
                )

            executions: list[_ExecutedTool] = []
            for raw_call in output.tool_calls:
                try:
                    _ensure_before_deadline(deadline)
                    execution = await executor.invoke(raw_call)
                    _ensure_before_deadline(deadline)
                except asyncio.CancelledError:
                    raise
                except _HardDeadlineExceeded:
                    raise
                except DecisionToolCallError as exc:
                    return self._failure_run(
                        request,
                        started_at=started_at,
                        metadata=output.metadata,
                        code=exc.code,
                        steps_used=step_number,
                        tool_trace=executor.trace,
                        access_trace=access_turn.trace,
                    )
                except Exception:
                    return self._failure_run(
                        request,
                        started_at=started_at,
                        metadata=output.metadata,
                        code="tool_execution_failed",
                        steps_used=step_number,
                        tool_trace=executor.trace,
                        access_trace=access_turn.trace,
                    )
                executions.append(execution)
                if executor.fatal_code is not None:
                    return self._failure_run(
                        request,
                        started_at=started_at,
                        metadata=output.metadata,
                        code=executor.fatal_code,
                        status=(
                            executor.fatal_status
                            or DecisionStatus.FAILED
                        ),
                        steps_used=step_number,
                        tool_trace=executor.trace,
                        access_trace=access_turn.trace,
                    )

            history.append(
                RuntimeToolExchange(
                    step_number=step_number,
                    tool_calls=tuple(
                        execution.call.model_copy(deep=True)
                        for execution in executions
                    ),
                    results=tuple(
                        _runtime_context_result(
                            execution.result,
                            runtime_aliases=runtime_aliases,
                        )
                        for execution in executions
                    ),
                )
            )

        return self._failure_run(
            request,
            started_at=started_at,
            metadata=fallback_metadata,
            code="decision_step_budget_exhausted",
            status=DecisionStatus.BLOCKED,
            steps_used=request.budget.max_steps,
            tool_trace=executor.trace,
            access_trace=access_turn.trace,
        )

    def _failure_run(
        self,
        request: DecisionRequest | None = None,
        *,
        request_id: uuid.UUID | None = None,
        turn_id: uuid.UUID | None = None,
        started_at: datetime,
        metadata: RuntimeMetadata,
        code: str,
        status: DecisionStatus = DecisionStatus.FAILED,
        steps_used: int = 0,
        tool_trace: tuple[ToolCallRecord, ...] = (),
        access_trace: tuple[AccessAuditEntry, ...] = (),
        finished_at: datetime | None = None,
    ) -> DecisionAgentRun:
        if request is not None:
            request_id = request.request_id
            turn_id = request.turn_id
        if request_id is None or turn_id is None:
            raise ValueError(
                "failure runs require a request or explicit request IDs"
            )
        return DecisionAgentRun(
            request_id=request_id,
            turn_id=turn_id,
            draft=DecisionDraft(
                status=status,
                persistence_intent=DecisionPersistenceIntent.NONE,
                limitations=[code],
            ),
            source_refs=_source_refs(tool_trace),
            runtime=metadata,
            steps_used=steps_used,
            tool_trace=tool_trace,
            access_trace=access_trace,
            system_policy_version=(
                HEALTHMES_DECISION_SYSTEM_POLICY_VERSION
            ),
            started_at=started_at,
            finished_at=(
                _as_utc(finished_at)
                if finished_at is not None
                else _as_utc(self._clock())
            ),
        )


def _runtime_request(
    request: DecisionRequest,
    *,
    related_records: tuple[_RelatedRecordBinding, ...],
    runtime_aliases: tuple[_RuntimeRecordAlias, ...],
) -> RuntimeDecisionRequest:
    related_domains = {
        item.domain for item in related_records
    }
    return RuntimeDecisionRequest(
        question=_alias_runtime_value(
            request.question,
            aliases=runtime_aliases,
        ),
        requested_at=request.requested_at,
        timezone=request.timezone,
        requested_privacy_level=request.requested_privacy_level,
        persistence_requested=request.persistence_requested,
        hints=RuntimeDecisionContextHints(
            local_date=request.hints.local_date,
            start=request.hints.start,
            end=request.hints.end,
            lookback_days=request.hints.lookback_days,
            has_related_records=bool(
                request.hints.related_record_ids
            ),
            related_domains=tuple(sorted(related_domains)),
            related_records=tuple(
                RuntimeRelatedRecord(
                    reference=item.reference,
                    domain=item.domain,
                )
                for item in related_records
            ),
        ),
    )


def _related_record_bindings(
    access_layer: ContextAccessLayer,
    *,
    request: DecisionRequest,
    policy: ContextAccessPolicy,
) -> tuple[_RelatedRecordBinding, ...]:
    allowed_domains = {
        descriptor.metadata.domain
        for descriptor in access_layer.registry.discover()
        if (
            (grant := policy.grant(descriptor.metadata.domain))
            is not None
            and grant.enabled
            and request.caller.execution_scope in grant.execution_scopes
        )
    }
    candidates: dict[str, dict[str, Any]] = {}
    for key, record_id in sorted(
        request.hints.related_record_ids.items()
    ):
        matching_domains = tuple(
            domain
            for domain in sorted(allowed_domains)
            if (
                key == domain
                or key.startswith(f"{domain}_")
                or key.endswith(f"_{domain}")
            )
        )
        if len(matching_domains) != 1:
            continue
        canonical_id, parsed, identity = _canonical_record_id(record_id)
        candidate = candidates.setdefault(
            identity,
            {
                "domains": set(),
                "record_ids": [],
            },
        )
        candidate["domains"].add(matching_domains[0])
        variants = [record_id]
        if parsed is not None:
            variants.extend(
                (
                    canonical_id,
                    parsed.hex,
                    parsed.urn,
                )
            )
        for variant in variants:
            if variant not in candidate["record_ids"]:
                candidate["record_ids"].append(variant)

    bindings: list[_RelatedRecordBinding] = []
    used_references: set[str] = set()
    for identity in sorted(candidates):
        candidate = candidates[identity]
        domains = candidate["domains"]
        if len(domains) != 1:
            continue
        while True:
            reference = f"rr_{uuid.uuid4().hex[:16]}"
            if reference not in used_references:
                used_references.add(reference)
                break
        bindings.append(
            _RelatedRecordBinding(
                reference=reference,
                domain=next(iter(domains)),
                identity=identity,
                record_ids=tuple(candidate["record_ids"]),
            )
        )
    return tuple(bindings)


def _request_policy_error(
    request: DecisionRequest,
    policy: ContextAccessPolicy,
) -> str | None:
    if not request.caller.authenticated:
        return "caller_not_authenticated"
    if request.caller.principal_id != policy.owner_principal_id:
        return "caller_not_policy_owner"
    return None


def _tool_policy_denial(
    request: DecisionRequest,
    policy: ContextAccessPolicy,
    *,
    domain: str,
) -> str | None:
    policy_error = _request_policy_error(request, policy)
    if policy_error is not None:
        return policy_error
    grant = policy.grant(domain)
    if grant is None or not grant.enabled:
        return "domain_consent_denied"
    if request.caller.execution_scope not in grant.execution_scopes:
        return "execution_scope_denied"
    return None


def _tool_policy_fingerprint(
    policy: ContextAccessPolicy,
    *,
    domain: str,
) -> str:
    grant = policy.grant(domain)
    payload = {
        "owner_principal_id": policy.owner_principal_id,
        "grant": (
            grant.model_dump(mode="json", round_trip=True)
            if grant is not None
            else None
        ),
        "max_query_days": policy.max_query_days,
        "max_rows_per_query": policy.max_rows_per_query,
        "max_payload_bytes_per_query": (
            policy.max_payload_bytes_per_query
        ),
        "max_source_refs_per_query": (
            policy.max_source_refs_per_query
        ),
        "trim_overlong_queries": policy.trim_overlong_queries,
        "allow_external_provenance": (
            policy.allow_external_provenance
        ),
    }
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _tool_catalog(
    access_layer: ContextAccessLayer,
    *,
    request: DecisionRequest,
    policy: ContextAccessPolicy,
    related_records: tuple[_RelatedRecordBinding, ...],
) -> tuple[DecisionToolSpec, ...]:
    tools: list[DecisionToolSpec] = []
    for descriptor in access_layer.registry.discover():
        grant = policy.grant(descriptor.metadata.domain)
        if (
            grant is None
            or not grant.enabled
            or request.caller.execution_scope not in grant.execution_scopes
        ):
            continue
        for capability in descriptor.metadata.capabilities:
            parameter_specs = _runtime_parameter_specs(
                capability.parameter_specs,
                domain=descriptor.metadata.domain,
                related_records=related_records,
            )
            if parameter_specs is None:
                continue
            privacy_levels = tuple(
                level
                for level in capability.privacy_levels
                if _PRIVACY_RANK[level]
                <= _PRIVACY_RANK[request.requested_privacy_level]
                and _PRIVACY_RANK[level]
                <= _PRIVACY_RANK[grant.max_privacy_level]
                and not (
                    level is PrivacyLevel.SCOPED_RAW
                    and request.caller.execution_scope
                    is ExecutionScope.HOSTED
                    and not grant.allow_hosted_raw
                )
            )
            if not privacy_levels:
                continue
            tools.append(
                DecisionToolSpec(
                    capability=capability.capability,
                    provider_id=descriptor.metadata.provider_id,
                    domain=descriptor.metadata.domain,
                    description=capability.description,
                    granularities=capability.granularities,
                    query_fields=capability.query_fields,
                    output_fields=capability.output_fields,
                    parameters=capability.parameters,
                    parameter_specs=parameter_specs,
                    privacy_levels=privacy_levels,
                    max_lookback_days=min(
                        capability.max_lookback_days,
                        policy.max_query_days,
                    ),
                    max_rows=policy.max_rows_per_query,
                    supports_raw=(
                        capability.supports_raw
                        and PrivacyLevel.SCOPED_RAW in privacy_levels
                    ),
                    allows_future=capability.allows_future,
                    provenance=capability.provenance,
                    freshness_expectation=(
                        capability.freshness_expectation
                    ),
                )
            )
    return tuple(sorted(tools, key=lambda item: item.capability))


def _provider_parameter_specs(
    access_layer: ContextAccessLayer,
    *,
    tools: tuple[DecisionToolSpec, ...],
) -> dict[str, tuple[ContextParameterSpec, ...]]:
    allowed_capabilities = {tool.capability for tool in tools}
    specs: dict[str, tuple[ContextParameterSpec, ...]] = {}
    for descriptor in access_layer.registry.discover():
        for capability in descriptor.metadata.capabilities:
            if capability.capability not in allowed_capabilities:
                continue
            specs[capability.capability] = tuple(
                parameter.model_copy(deep=True)
                for parameter in capability.parameter_specs
            )
    if set(specs) != allowed_capabilities:
        raise ValueError("provider parameter schemas are incomplete")
    return specs


def _provider_related_record_id(
    binding: _RelatedRecordBinding,
    *,
    parameter_spec: ContextParameterSpec,
) -> str:
    for record_id in binding.record_ids:
        try:
            validate_context_parameters(
                {parameter_spec.name: record_id},
                (parameter_spec,),
            )
        except ValueError:
            continue
        return record_id
    raise ValueError(
        "related record does not satisfy the provider parameter schema"
    )


def _runtime_parameter_specs(
    specs: tuple[ContextParameterSpec, ...],
    *,
    domain: str,
    related_records: tuple[_RelatedRecordBinding, ...],
) -> tuple[ContextParameterSpec, ...] | None:
    references = tuple(
        item.reference
        for item in related_records
        if item.domain == domain
    )
    runtime_specs: list[ContextParameterSpec] = []
    for spec in specs:
        if not spec.accepts_related_record_ref:
            runtime_specs.append(spec.model_copy(deep=True))
            continue
        if spec.required and not references:
            return None
        runtime_specs.append(
            strict_model_validate(
                ContextParameterSpec,
                spec.model_copy(
                    update={
                        "min_length": 19,
                        "max_length": 19,
                        "allowed_values": references,
                        "format": (
                            ContextParameterFormat.RELATED_RECORD_REF
                        ),
                        "accepts_related_record_ref": False,
                    }
                ),
            )
        )
    return tuple(runtime_specs)


def _source_refs(
    trace: tuple[ToolCallRecord, ...],
) -> tuple[SourceRef, ...]:
    refs: dict[str, SourceRef] = {}
    for record in trace:
        if record.result is None:
            continue
        for source_ref in record.result.source_refs:
            refs.setdefault(source_ref.reference_id, source_ref)
    return tuple(refs.values())


def _runtime_context_result(
    result: ContextResult,
    *,
    runtime_aliases: tuple[_RuntimeRecordAlias, ...],
) -> RuntimeContextResult:
    """Remove storage identities that the model runtime does not need."""

    return RuntimeContextResult(
        provider_id=result.provider_id,
        capability=result.capability,
        status=result.status,
        payload=_alias_runtime_value(
            result.payload,
            aliases=runtime_aliases,
        ),
        source_ref_ids=tuple(
            source_ref.reference_id for source_ref in result.source_refs
        ),
        observed_start=result.observed_start,
        observed_end=result.observed_end,
        collected_at=result.collected_at,
        freshness=result.freshness.model_copy(deep=True),
        coverage=result.coverage.model_copy(deep=True),
        limitations=tuple(result.limitations),
        truncated=result.truncated,
    )


def _runtime_record_aliases(
    request: DecisionRequest,
    *,
    related_records: tuple[_RelatedRecordBinding, ...],
) -> tuple[_RuntimeRecordAlias, ...]:
    binding_references: dict[str, str] = {}
    used_references: set[str] = set()
    for binding in related_records:
        binding_references[binding.identity] = binding.reference
        used_references.add(binding.reference)

    records: dict[str, dict[str, Any]] = {}
    for record_id in request.hints.related_record_ids.values():
        canonical_id, parsed, identity = _canonical_record_id(record_id)
        record = records.setdefault(
            identity,
            {
                "record_ids": set(),
                "uuid_value": parsed,
            },
        )
        record["record_ids"].add(record_id)
        if parsed is not None:
            record["record_ids"].update(
                (canonical_id, parsed.hex, parsed.urn)
            )

    aliases: list[_RuntimeRecordAlias] = []
    for identity in sorted(records):
        record = records[identity]
        reference = binding_references.get(identity)
        if reference is None:
            while True:
                reference = f"rr_{uuid.uuid4().hex[:16]}"
                if reference not in used_references:
                    used_references.add(reference)
                    break
        aliases.append(
            _RuntimeRecordAlias(
                reference=reference,
                record_ids=tuple(
                    sorted(
                        record["record_ids"],
                        key=lambda item: (
                            -len(item),
                            item.casefold(),
                            item,
                        ),
                    )
                ),
                uuid_value=record["uuid_value"],
            )
        )
    return tuple(aliases)


def _canonical_record_id(
    record_id: str,
) -> tuple[str, uuid.UUID | None, str]:
    try:
        parsed = _parse_record_uuid(record_id)
    except ValueError:
        return record_id, None, f"text:{record_id}"
    return str(parsed), parsed, f"uuid:{parsed.hex}"


def _parse_record_uuid(value: str) -> uuid.UUID:
    candidate = value
    if candidate.casefold().startswith("urn:uuid:"):
        candidate = candidate[len("urn:uuid:") :]
    return uuid.UUID(candidate)


def _alias_runtime_value(
    value: Any,
    *,
    aliases: tuple[_RuntimeRecordAlias, ...],
    matcher: _RuntimeAliasMatcher | None = None,
) -> Any:
    if matcher is None:
        matcher = _runtime_alias_matcher(aliases)
    if isinstance(value, str):
        for alias in aliases:
            if value in alias.record_ids:
                return alias.reference
            if alias.uuid_value is not None:
                try:
                    if _parse_record_uuid(value) == alias.uuid_value:
                        return alias.reference
                except ValueError:
                    pass
        if matcher.pattern is None:
            return value

        def replace(match: re.Match[str]) -> str:
            group = match.lastgroup
            if group is None:
                raise ValueError("related record alias match is invalid")
            return matcher.references[int(group.removeprefix("alias_"))]

        return matcher.pattern.sub(replace, value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            aliased_key = _alias_runtime_value(
                str(key),
                aliases=aliases,
                matcher=matcher,
            )
            if aliased_key in result:
                raise ValueError(
                    "related record alias produced a duplicate payload key"
                )
            result[aliased_key] = _alias_runtime_value(
                item,
                aliases=aliases,
                matcher=matcher,
            )
        return result
    if isinstance(value, list):
        return [
            _alias_runtime_value(
                item,
                aliases=aliases,
                matcher=matcher,
            )
            for item in value
        ]
    return value


def _runtime_alias_matcher(
    aliases: tuple[_RuntimeRecordAlias, ...],
) -> _RuntimeAliasMatcher:
    variants: list[tuple[str, str, bool]] = []
    seen: dict[tuple[str, bool], str] = {}
    for alias in aliases:
        case_insensitive = alias.uuid_value is not None
        for record_id in alias.record_ids:
            identity = (
                record_id.casefold()
                if case_insensitive
                else record_id
            )
            key = (identity, case_insensitive)
            existing_reference = seen.get(key)
            if (
                existing_reference is not None
                and existing_reference != alias.reference
            ):
                raise ValueError(
                    "related record aliases contain a conflicting variant"
                )
            if existing_reference is not None:
                continue
            seen[key] = alias.reference
            variants.append(
                (record_id, alias.reference, case_insensitive)
            )
    variants.sort(
        key=lambda item: (
            -len(item[0]),
            item[0].casefold(),
            item[0],
            item[1],
        )
    )
    long_variants = [
        item for item in variants if len(item[0]) >= 32
    ]
    short_variants = [
        item for item in variants if len(item[0]) < 32
    ]
    expressions: list[tuple[str, str]] = []
    for record_id, reference, case_insensitive in long_variants:
        expressions.append(
            (
                _literal_alias_expression(
                    record_id,
                    case_insensitive=case_insensitive,
                ),
                reference,
            )
        )
    for alias in aliases:
        if alias.uuid_value is None:
            continue
        expressions.append(
            (
                _uuid_alias_expression(alias.uuid_value),
                alias.reference,
            )
        )
    for record_id, reference, case_insensitive in short_variants:
        expressions.append(
            (
                _literal_alias_expression(
                    record_id,
                    case_insensitive=case_insensitive,
                ),
                reference,
            )
        )
    parts: list[str] = []
    references: list[str] = []
    for index, (expression, reference) in enumerate(expressions):
        parts.append(f"(?P<alias_{index}>{expression})")
        references.append(reference)
    return _RuntimeAliasMatcher(
        pattern=re.compile("|".join(parts)) if parts else None,
        references=tuple(references),
    )


def _literal_alias_expression(
    record_id: str,
    *,
    case_insensitive: bool,
) -> str:
    expression = re.escape(record_id)
    if record_id[0].isascii() and (
        record_id[0].isalnum() or record_id[0] == "_"
    ):
        expression = r"(?<![A-Za-z0-9_])" + expression
    if record_id[-1].isascii() and (
        record_id[-1].isalnum() or record_id[-1] == "_"
    ):
        expression += r"(?![A-Za-z0-9_])"
    return f"(?i:{expression})" if case_insensitive else expression


def _uuid_alias_expression(value: uuid.UUID) -> str:
    digits = r"-*".join(re.escape(item) for item in value.hex)
    core = digits
    forms = (
        r"urn:uuid:\{" + core + r"\}",
        r"\{urn:uuid:" + core + r"\}",
        r"urn:uuid:" + core,
        r"\{" + core + r"\}",
        core,
    )
    return (
        r"(?i:(?<![A-Za-z0-9_])(?:"
        + "|".join(forms)
        + r")(?![A-Za-z0-9_]))"
    )
