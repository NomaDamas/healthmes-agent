"""Canonical application ingress for every free-form wellness decision."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import Lock
from typing import Any, Literal, Protocol

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from healthmes.config import Settings, resolve_timezone
from healthmes.decision.composition import (
    resolve_decision_execution_scope,
)
from healthmes.decision.contracts import (
    DecisionBudget,
    DecisionCaller,
    DecisionContextHints,
    DecisionRequest,
    DecisionResult,
    DecisionStatus,
    PersistenceStatus,
    PrivacyLevel,
)
from healthmes.decision.engine import DecisionEngineClosedError
from healthmes.decision.execution import DecisionExecutionControl
from healthmes.decision.finalizer import (
    decision_request_timezone_from_record,
)
from healthmes.store.decision_receipts import (
    DecisionReceiptClaimState,
    DecisionReceiptConflictError,
    DecisionReceiptExpiredError,
    DecisionReceiptOwnershipError,
    DecisionReceiptStore,
)
from healthmes.store.decision_records import (
    decision_record_is_available_at,
)
from healthmes.store.models import DecisionRecord

_CHANNEL_REQUEST_NAMESPACE = uuid.UUID(
    "ed5fcd43-39c0-4fb4-b968-57455f1fc9bf"
)
_REST_REQUEST_NAMESPACE = uuid.UUID(
    "d8fcb625-cade-45a4-b27f-63a6c06c9719"
)
_DECISION_RECEIPT_RETENTION = timedelta(days=30)
_DECISION_RECEIPT_SCHEMA = "healthmes.decision-receipt.v2"
_LEGACY_DECISION_RECEIPT_SCHEMA = "healthmes.decision-receipt.v1"
_DECISION_SERVICE_FINGERPRINT_CONTEXT = (
    b"healthmes-decision-service-request-fingerprint-v1\x00"
)
_MIN_FINGERPRINT_KEY_BYTES = 32


def _consume_task_result(task: asyncio.Future[Any]) -> None:
    try:
        task.exception()
    except BaseException:
        pass


class DecisionRuntimeNotConfiguredError(RuntimeError):
    """Raised when an ingress is used without a configured decision engine."""


class DecisionIdempotencyConflictError(RuntimeError):
    """Raised when one request ID is reused for different decision input."""


class DecisionIdempotencyExpiredError(RuntimeError):
    """Raised when a retained identity no longer has a replayable result."""


class DecisionIdempotencyUnavailableError(RuntimeError):
    """Raised when a durable request cannot safely converge yet."""


class DecisionRecoveryNotFoundError(RuntimeError):
    """Raised when request-ID recovery has no currently retained record."""


class DecisionIngress(StrEnum):
    """Trusted product surfaces that may submit free-form reasoning."""

    REST = "rest"
    CHANNEL = "channel"
    PROACTIVE = "proactive"
    SCHEDULED = "scheduled"


class DecisionServiceRequest(BaseModel):
    """UI-neutral request accepted by the canonical decision service."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: uuid.UUID | None = None
    question: str = Field(min_length=1, max_length=8_000)
    ingress: DecisionIngress
    source: str | None = Field(default=None, min_length=1, max_length=48)
    session_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
    )
    requested_at: AwareDatetime | None = None
    requested_privacy_level: PrivacyLevel = PrivacyLevel.AGGREGATE
    persistence_requested: StrictBool = False
    budget: DecisionBudget = Field(default_factory=DecisionBudget)
    hints: DecisionContextHints = Field(
        default_factory=DecisionContextHints
    )

    @model_validator(mode="after")
    def validate_ingress_source(self) -> DecisionServiceRequest:
        if self.ingress is DecisionIngress.REST:
            if self.source is not None:
                raise ValueError("REST ingress cannot override its source")
        elif self.source is None:
            raise ValueError(
                "channel, proactive, and scheduled ingress require a source"
            )
        return self


class DecisionChannelRequest(BaseModel):
    """UI-neutral payload for a future app or messaging-channel adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    idempotency_key: str = Field(min_length=1, max_length=255)
    question: str = Field(min_length=1, max_length=8_000)
    source: str = Field(min_length=1, max_length=48)
    session_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
    )
    requested_at: AwareDatetime | None = None
    requested_privacy_level: PrivacyLevel = PrivacyLevel.AGGREGATE
    persistence_requested: StrictBool = False
    budget: DecisionBudget = Field(default_factory=DecisionBudget)
    hints: DecisionContextHints = Field(
        default_factory=DecisionContextHints
    )

    @field_validator("idempotency_key", "source")
    @classmethod
    def validate_identity_component(cls, value: str, info) -> str:
        if value != value.strip():
            raise ValueError(
                f"{info.field_name} must not contain surrounding whitespace"
            )
        if not value.isprintable():
            raise ValueError(
                f"{info.field_name} must not contain control characters"
            )
        return value


class DecisionEngine(Protocol):
    """Minimal engine surface used by the application-level service."""

    async def ask_wellness(
        self,
        request: DecisionRequest,
    ) -> DecisionResult: ...

    async def ask_wellness_with_control(
        self,
        request: DecisionRequest,
        execution_control: DecisionExecutionControl,
    ) -> DecisionResult: ...

    async def replay_persisted_decision(
        self,
        request: DecisionRequest,
        decision_record_id: uuid.UUID,
    ) -> DecisionResult: ...


class PersistedDecisionRecovery(Protocol):
    """Hermes-independent surface for revalidating committed decisions."""

    async def arevalidate_persisted(
        self,
        request: DecisionRequest,
        decision_record_id: uuid.UUID,
    ) -> DecisionResult: ...


class DecisionService(Protocol):
    """Canonical service surface allowed behind product ingress adapters."""

    async def ask_wellness(
        self,
        submission: DecisionServiceRequest,
    ) -> DecisionResult: ...

    async def recover_wellness(
        self,
        request_id: uuid.UUID,
    ) -> DecisionResult: ...

    def begin_shutdown(self) -> None: ...

    async def aclose(self) -> None: ...


@dataclass(slots=True)
class _ActiveDecision:
    fingerprint: str
    task: asyncio.Task[_IdempotentExecution]
    execution_control: DecisionExecutionControl | None = None
    waiters: int = 0


@dataclass(frozen=True, slots=True)
class _IdempotentExecution:
    result: DecisionResult
    cache_expires_at: datetime | None


class _TransientReceiptV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["healthmes.decision-receipt.v2"] = Field(
        alias="schema"
    )
    kind: Literal["transient_result"]
    result: DecisionResult


class _PersistedReceiptV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["healthmes.decision-receipt.v2"] = Field(
        alias="schema"
    )
    kind: Literal["decision_record"]
    decision_record_id: uuid.UUID


class HealthMesDecisionService:
    """Build server-owned DecisionRequests and call the one runtime engine."""

    def __init__(
        self,
        *,
        settings: Settings,
        engine_provider: Callable[[], DecisionEngine | None],
        session_factory_provider: Callable[
            [], sessionmaker[Session]
        ],
        recovery_provider: Callable[
            [], PersistedDecisionRecovery | None
        ]
        | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(engine_provider):
            raise TypeError("engine_provider must be callable")
        if not callable(session_factory_provider):
            raise TypeError("session_factory_provider must be callable")
        if recovery_provider is not None and not callable(
            recovery_provider
        ):
            raise TypeError("recovery_provider must be callable")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._settings = settings
        self._engine_provider = engine_provider
        self._session_factory_provider = session_factory_provider
        self._recovery_provider = recovery_provider or (lambda: None)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._idempotency_lock = Lock()
        self._receipt_store: DecisionReceiptStore | None = None
        self._active_idempotent_requests: dict[
            uuid.UUID,
            _ActiveDecision,
        ] = {}
        self._service_tasks: set[
            asyncio.Task[_IdempotentExecution]
        ] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self._closing = False
        self._closed = False

    def build_request(
        self,
        submission: DecisionServiceRequest,
    ) -> DecisionRequest:
        """Translate one trusted ingress into the shared domain contract."""

        if not isinstance(submission, DecisionServiceRequest):
            raise TypeError(
                "submission must be a DecisionServiceRequest"
            )
        requested_at = submission.requested_at or self._clock()
        return DecisionRequest(
            **(
                {"request_id": submission.request_id}
                if submission.request_id is not None
                else {}
            ),
            question=submission.question,
            requested_at=requested_at,
            timezone=str(resolve_timezone(self._settings)),
            caller=DecisionCaller(
                principal_id=(
                    self._settings.decision_owner_principal_id
                ),
                authenticated=True,
                execution_scope=resolve_decision_execution_scope(
                    self._settings
                ),
                session_id=submission.session_id,
                channel=_caller_channel(submission),
            ),
            requested_privacy_level=(
                submission.requested_privacy_level
            ),
            persistence_requested=submission.persistence_requested,
            budget=submission.budget,
            hints=submission.hints,
        )

    async def ask_wellness(
        self,
        submission: DecisionServiceRequest,
    ) -> DecisionResult:
        """Run any product reasoning ingress through the same engine."""

        if not isinstance(submission, DecisionServiceRequest):
            raise TypeError(
                "submission must be a DecisionServiceRequest"
            )
        if submission.request_id is None:
            with self._idempotency_lock:
                self._ensure_accepting_locked()
            return await self._execute(submission)

        request_id = submission.request_id
        fingerprint = _service_request_fingerprint(
            submission,
            key=_service_fingerprint_key(self._settings),
        )
        legacy_fingerprint = _legacy_service_request_fingerprint(
            submission
        )
        loop = asyncio.get_running_loop()
        with self._idempotency_lock:
            self._ensure_accepting_locked()
            self._bind_active_loop_locked(loop)
            active = self._active_idempotent_requests.get(request_id)
            if active is not None:
                _require_matching_idempotency_fingerprint(
                    active.fingerprint,
                    fingerprint,
                )
                if active.task.get_loop() is not loop:
                    raise RuntimeError(
                        "an idempotent decision request is active on "
                        "another event loop"
                    )
                task = active.task
            else:
                coroutine = self._execute_idempotently(
                    submission,
                    fingerprint=fingerprint,
                    legacy_fingerprint=legacy_fingerprint,
                )
                try:
                    task = loop.create_task(
                        coroutine,
                        name=f"healthmes-service-{request_id}",
                    )
                except BaseException:
                    coroutine.close()
                    raise
                self._active_idempotent_requests[request_id] = (
                    _ActiveDecision(
                        fingerprint=fingerprint,
                        task=task,
                    )
                )
                self._loop = loop
                self._service_tasks.add(task)
                task.add_done_callback(
                    lambda done: self._finish_idempotent_request(
                        request_id,
                        fingerprint,
                        done,
                    )
                )
            active = self._active_idempotent_requests[request_id]
            active.waiters += 1

        cancelled = False
        try:
            execution = await asyncio.shield(task)
            return execution.result
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            cancel_orphaned_task = False
            with self._idempotency_lock:
                active = self._active_idempotent_requests.get(
                    request_id
                )
                if active is not None and active.task is task:
                    active.waiters -= 1
                    cancel_orphaned_task = (
                        cancelled
                        and active.waiters == 0
                        and not task.done()
                    )
                    if cancel_orphaned_task:
                        execution_control = active.execution_control
                        if execution_control is None:
                            task.cancel()
                        else:
                            cancel_orphaned_task = (
                                execution_control.cancel_reasoning(task)
                            )
                        if cancel_orphaned_task:
                            self._active_idempotent_requests.pop(
                                request_id,
                                None,
                            )

    async def recover_wellness(
        self,
        request_id: uuid.UUID,
    ) -> DecisionResult:
        """Revalidate a retained result without invoking model reasoning."""

        if not isinstance(request_id, uuid.UUID):
            raise TypeError("request_id must be a UUID")
        with self._idempotency_lock:
            self._ensure_accepting_locked()
        current = _as_utc(self._clock())
        with self._session_factory_provider()() as session:
            identity = session.scalar(
                select(DecisionRecord).where(
                    DecisionRecord.decision_request_id == request_id,
                    decision_record_is_available_at(current),
                )
            )
        if identity is None or identity.decision_turn_id is None:
            raise DecisionRecoveryNotFoundError(
                "wellness decision is not available"
            )

        request = DecisionRequest(
            request_id=request_id,
            turn_id=identity.decision_turn_id,
            question="Recover the previously committed wellness decision.",
            requested_at=current,
            timezone=self._persisted_request_timezone(
                identity,
                now=current,
            ),
            caller=DecisionCaller(
                principal_id=(
                    self._settings.decision_owner_principal_id
                ),
                authenticated=True,
                execution_scope=resolve_decision_execution_scope(
                    self._settings
                ),
                channel=DecisionIngress.REST.value,
            ),
            requested_privacy_level=PrivacyLevel.AGGREGATE,
        )
        result = await self._revalidate_persisted(
            request,
            identity.id,
        )
        if not isinstance(result, DecisionResult):
            raise TypeError("decision replay must return DecisionResult")
        if result.request_id != request_id:
            raise RuntimeError(
                "decision replay returned a different request identity"
            )
        if result.status is DecisionStatus.COMPLETED and (
            result.persistence_status is not PersistenceStatus.PERSISTED
            or result.decision_record_id != identity.id
        ):
            raise RuntimeError(
                "completed decision recovery did not return its stored record"
            )
        return result

    async def _execute(
        self,
        submission: DecisionServiceRequest,
        *,
        execution_control: DecisionExecutionControl | None = None,
    ) -> DecisionResult:
        request = self.build_request(submission)
        engine = self._engine_provider()
        if engine is None:
            raise DecisionRuntimeNotConfiguredError(
                "HealthMes decision runtime is not configured"
            )
        controlled_ask = getattr(
            engine,
            "ask_wellness_with_control",
            None,
        )
        if execution_control is not None and callable(controlled_ask):
            result = await controlled_ask(request, execution_control)
        else:
            if (
                execution_control is not None
                and not execution_control.begin_finalization()
            ):
                raise asyncio.CancelledError
            result = await engine.ask_wellness(request)
        if not isinstance(result, DecisionResult):
            raise TypeError("decision engine must return DecisionResult")
        return result

    async def _execute_idempotently(
        self,
        submission: DecisionServiceRequest,
        *,
        fingerprint: str,
        legacy_fingerprint: str,
    ) -> _IdempotentExecution:
        request_id = submission.request_id
        assert request_id is not None
        service_task = asyncio.current_task()
        assert service_task is not None
        owner_token = uuid.uuid4()
        store = self._get_receipt_store()
        requested_at = _as_utc(
            submission.requested_at or self._clock()
        )

        while True:
            lease_generation: int | None = None
            while True:
                try:
                    claim = await _claim_with_cancellation_cleanup(
                        store,
                        request_id=request_id,
                        fingerprint=fingerprint,
                        legacy_fingerprint=legacy_fingerprint,
                        owner_token=owner_token,
                        now=self._clock(),
                        requested_at=requested_at,
                    )
                except DecisionReceiptConflictError as exc:
                    raise DecisionIdempotencyConflictError(str(exc)) from exc
                except DecisionReceiptExpiredError as exc:
                    raise DecisionIdempotencyExpiredError(str(exc)) from exc
                if claim.state is DecisionReceiptClaimState.COMPLETED:
                    assert claim.result_payload is not None
                    assert claim.expires_at is not None
                    return _IdempotentExecution(
                        result=await self._replay_completed_receipt(
                            submission,
                            requested_at=claim.requested_at,
                            payload=claim.result_payload,
                        ),
                        cache_expires_at=_as_utc(claim.expires_at),
                    )
                if claim.state is DecisionReceiptClaimState.ACQUIRED:
                    assert claim.requested_at is not None
                    assert claim.lease_generation is not None
                    requested_at = _as_utc(claim.requested_at)
                    lease_generation = claim.lease_generation
                    break
                await asyncio.sleep(claim.retry_after_seconds)

            assert lease_generation is not None
            frozen_submission = submission.model_copy(
                update={"requested_at": requested_at}
            )
            execution_control = DecisionExecutionControl()
            try:
                if not self._install_execution_control(
                    request_id=request_id,
                    fingerprint=fingerprint,
                    task=service_task,
                    execution_control=execution_control,
                ):
                    raise asyncio.CancelledError
                result = await self._execute(
                    frozen_submission,
                    execution_control=execution_control,
                )
                if not execution_control.begin_finalization():
                    raise asyncio.CancelledError
                if not _is_terminal_non_retryable(result):
                    await _run_receipt_cleanup(
                        store.release,
                        request_id=request_id,
                        fingerprint=fingerprint,
                        legacy_fingerprint=legacy_fingerprint,
                        owner_token=owner_token,
                        lease_generation=lease_generation,
                        now=self._clock(),
                    )
                    return _IdempotentExecution(
                        result=result,
                        cache_expires_at=None,
                    )
                payload = _result_receipt_payload(result)
                try:
                    completion = await asyncio.to_thread(
                        store.complete,
                        request_id=request_id,
                        fingerprint=fingerprint,
                        legacy_fingerprint=legacy_fingerprint,
                        owner_token=owner_token,
                        lease_generation=lease_generation,
                        result_payload=payload,
                        now=self._clock(),
                    )
                except DecisionReceiptOwnershipError:
                    execution_control = DecisionExecutionControl()
                    if not self._install_execution_control(
                        request_id=request_id,
                        fingerprint=fingerprint,
                        task=service_task,
                        execution_control=execution_control,
                    ):
                        raise asyncio.CancelledError
                    canonical = await self._converge_after_lease_loss(
                        store,
                        submission=submission,
                        request_id=request_id,
                        fingerprint=fingerprint,
                        legacy_fingerprint=legacy_fingerprint,
                    )
                    if canonical is not None:
                        return canonical
                    # The replacement worker also disappeared. Claim a fresh
                    # generation and rerun the engine; never publish the stale
                    # result produced under the lost generation.
                    owner_token = uuid.uuid4()
                    continue
                except DecisionReceiptExpiredError as exc:
                    raise DecisionIdempotencyExpiredError(str(exc)) from exc
                if completion.result_payload == payload:
                    canonical_result = result
                else:
                    canonical_result = await self._replay_completed_receipt(
                        submission,
                        requested_at=requested_at,
                        payload=completion.result_payload,
                    )
                return _IdempotentExecution(
                    result=canonical_result,
                    cache_expires_at=(
                        _as_utc(completion.expires_at)
                        if completion.expires_at is not None
                        else None
                    ),
                )
            except BaseException:
                await _run_receipt_cleanup(
                    store.release,
                    request_id=request_id,
                    fingerprint=fingerprint,
                    legacy_fingerprint=legacy_fingerprint,
                    owner_token=owner_token,
                    lease_generation=lease_generation,
                    now=self._clock(),
                )
                raise

    def _install_execution_control(
        self,
        *,
        request_id: uuid.UUID,
        fingerprint: str,
        task: asyncio.Task[_IdempotentExecution],
        execution_control: DecisionExecutionControl,
    ) -> bool:
        """Publish the current lease attempt's cancellation boundary."""

        with self._idempotency_lock:
            active = self._active_idempotent_requests.get(request_id)
            if (
                active is None
                or active.task is not task
                or active.fingerprint != fingerprint
            ):
                return False
            active.execution_control = execution_control
            if active.waiters > 0:
                return True
            self._active_idempotent_requests.pop(request_id, None)
            return False

    async def _converge_after_lease_loss(
        self,
        store: DecisionReceiptStore,
        *,
        submission: DecisionServiceRequest,
        request_id: uuid.UUID,
        fingerprint: str,
        legacy_fingerprint: str,
    ) -> _IdempotentExecution | None:
        """Return only the durable winner after this worker loses its lease."""

        while True:
            try:
                observation = await asyncio.to_thread(
                    store.observe,
                    request_id=request_id,
                    fingerprint=fingerprint,
                    legacy_fingerprint=legacy_fingerprint,
                    now=self._clock(),
                )
            except DecisionReceiptExpiredError as exc:
                raise DecisionIdempotencyExpiredError(str(exc)) from exc
            except DecisionReceiptOwnershipError as exc:
                raise DecisionIdempotencyUnavailableError(
                    "durable decision receipt disappeared while converging; "
                    "retry the same Idempotency-Key"
                ) from exc
            if (
                observation.state
                is DecisionReceiptClaimState.COMPLETED
            ):
                assert observation.result_payload is not None
                assert observation.expires_at is not None
                return _IdempotentExecution(
                    result=await self._replay_completed_receipt(
                        submission,
                        requested_at=observation.requested_at,
                        payload=observation.result_payload,
                    ),
                    cache_expires_at=_as_utc(
                        observation.expires_at
                    ),
                )
            if observation.lease_expired:
                return None
            await asyncio.sleep(observation.retry_after_seconds)

    async def _replay_completed_receipt(
        self,
        submission: DecisionServiceRequest,
        *,
        requested_at: datetime | None,
        payload: dict,
    ) -> DecisionResult:
        transient = _transient_result_from_receipt(payload)
        if transient is not None:
            return transient

        pointer = _decision_record_id_from_receipt(payload)
        if pointer is None:
            raise RuntimeError("decision receipt payload is invalid")
        frozen_submission = submission.model_copy(
            update={
                "requested_at": _as_utc(
                    requested_at or submission.requested_at or self._clock()
                )
            }
        )
        request = self.build_request(frozen_submission)
        current = _as_utc(self._clock())
        with self._session_factory_provider()() as session:
            stored = session.scalar(
                select(DecisionRecord).where(
                    DecisionRecord.id == pointer,
                    DecisionRecord.decision_request_id
                    == request.request_id,
                    decision_record_is_available_at(current),
                )
            )
        if stored is not None:
            request = request.model_copy(
                update={
                    "timezone": self._persisted_request_timezone(
                        stored,
                        now=current,
                    )
                }
            )
        result = await self._revalidate_persisted(request, pointer)
        if not isinstance(result, DecisionResult):
            raise TypeError(
                "decision replay must return DecisionResult"
            )
        if result.request_id != request.request_id:
            raise RuntimeError(
                "decision replay returned a different request identity"
            )
        if result.status is DecisionStatus.COMPLETED and (
            result.persistence_status is not PersistenceStatus.PERSISTED
            or result.decision_record_id != pointer
        ):
            raise RuntimeError(
                "completed decision replay did not return its stored record"
            )
        return result

    async def _revalidate_persisted(
        self,
        request: DecisionRequest,
        decision_record_id: uuid.UUID,
    ) -> DecisionResult:
        """Prefer the live engine, then use the standalone recovery finalizer."""

        engine = self._engine_provider()
        if engine is not None:
            replay = getattr(engine, "replay_persisted_decision", None)
            if callable(replay):
                return await replay(request, decision_record_id)

        recovery = self._recovery_provider()
        if recovery is None:
            raise DecisionRuntimeNotConfiguredError(
                "HealthMes decision recovery is not configured"
            )
        async_revalidate = getattr(
            recovery,
            "arevalidate_persisted",
            None,
        )
        if callable(async_revalidate):
            return await async_revalidate(request, decision_record_id)
        revalidate = getattr(recovery, "revalidate_persisted", None)
        if callable(revalidate):
            return await asyncio.to_thread(
                revalidate,
                request,
                decision_record_id,
            )
        raise DecisionRuntimeNotConfiguredError(
            "HealthMes decision recovery cannot revalidate stored results"
        )

    def _persisted_request_timezone(
        self,
        record: DecisionRecord,
        *,
        now: datetime,
    ) -> str:
        try:
            return decision_request_timezone_from_record(
                record,
                now=now,
            )
        except ValueError:
            # Keep corrupt records on the normal auditable failure path.
            return str(resolve_timezone(self._settings))

    def _get_receipt_store(self) -> DecisionReceiptStore:
        with self._idempotency_lock:
            if self._receipt_store is None:
                lease_seconds = max(
                    30.0,
                    self._settings.decision_timeout_seconds
                    + self._settings.decision_finalization_timeout_seconds
                    + 30.0,
                )
                self._receipt_store = DecisionReceiptStore(
                    session_factory=self._session_factory_provider(),
                    lease_duration=timedelta(seconds=lease_seconds),
                    retention=_DECISION_RECEIPT_RETENTION,
                )
            return self._receipt_store

    def _finish_idempotent_request(
        self,
        request_id: uuid.UUID,
        fingerprint: str,
        task: asyncio.Task[_IdempotentExecution],
    ) -> None:
        try:
            execution = task.result()
        except BaseException:
            with self._idempotency_lock:
                self._service_tasks.discard(task)
                active = self._active_idempotent_requests.get(
                    request_id
                )
                if active is not None and active.task is task:
                    self._active_idempotent_requests.pop(
                        request_id,
                        None,
                    )
            return

        with self._idempotency_lock:
            self._service_tasks.discard(task)
            active = self._active_idempotent_requests.get(request_id)
            if active is None or active.task is not task:
                return
            self._active_idempotent_requests.pop(request_id, None)
            del execution, fingerprint

    def _ensure_accepting_locked(self) -> None:
        if self._closing or self._closed:
            raise DecisionEngineClosedError(
                "HealthMes decision service is closing"
            )

    def _bind_active_loop_locked(
        self,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Keep concurrently active receipt work on one event loop."""

        completed = {
            task for task in self._service_tasks if task.done()
        }
        self._service_tasks.difference_update(completed)
        if (
            self._service_tasks
            and self._loop is not None
            and self._loop is not loop
        ):
            raise RuntimeError(
                "active HealthMes decision requests belong to another "
                "event loop"
            )
        self._loop = loop

    def begin_shutdown(self) -> None:
        """Reject new work before the engine begins its bounded drain."""

        with self._idempotency_lock:
            self._closing = True

    async def aclose(self) -> None:
        """Drain service-owned receipt work after engine shutdown."""

        loop = asyncio.get_running_loop()
        with self._idempotency_lock:
            shutdown_task = self._shutdown_task
            if shutdown_task is None:
                self._bind_active_loop_locked(loop)
                self._closing = True
                shutdown_task = loop.create_task(
                    self._shutdown(),
                    name="healthmes-decision-service-shutdown",
                )
                shutdown_task.add_done_callback(_consume_task_result)
                self._shutdown_task = shutdown_task
            elif (
                not shutdown_task.done()
                and shutdown_task.get_loop() is not loop
            ):
                raise RuntimeError(
                    "HealthMes decision service shutdown belongs to another "
                    "event loop"
                )

        if shutdown_task.done():
            shutdown_task.result()
            return
        await asyncio.shield(shutdown_task)

    async def _shutdown(self) -> None:
        with self._idempotency_lock:
            active = tuple(self._service_tasks)
        try:
            if active:
                await asyncio.wait(active)
        finally:
            with self._idempotency_lock:
                self._closed = True


class DecisionChannelAdapter:
    """Route a channel message through the canonical decision service once.

    Device and messaging teams may wrap this adapter with their platform
    ingress. They must not add a second LLM loop or call Hermes directly.
    """

    def __init__(self, *, service: DecisionService) -> None:
        if not callable(getattr(service, "ask_wellness", None)):
            raise TypeError("service must provide ask_wellness")
        self._service = service

    async def ask_wellness(
        self,
        submission: DecisionChannelRequest,
    ) -> DecisionResult:
        if not isinstance(submission, DecisionChannelRequest):
            raise TypeError(
                "submission must be a DecisionChannelRequest"
            )
        return await self._service.ask_wellness(
            DecisionServiceRequest(
                request_id=_channel_request_id(submission),
                question=submission.question,
                ingress=DecisionIngress.CHANNEL,
                source=submission.source,
                session_id=submission.session_id,
                requested_at=submission.requested_at,
                requested_privacy_level=(
                    submission.requested_privacy_level
                ),
                persistence_requested=(
                    submission.persistence_requested
                ),
                budget=submission.budget,
                hints=submission.hints,
            )
        )


def _channel_request_id(
    submission: DecisionChannelRequest,
) -> uuid.UUID:
    identity = json.dumps(
        [submission.source, submission.idempotency_key],
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return uuid.uuid5(
        _CHANNEL_REQUEST_NAMESPACE,
        identity,
    )


def decision_rest_request_id(
    *,
    owner_principal_id: str,
    idempotency_key: str,
) -> uuid.UUID:
    """Derive the durable REST receipt identity without storing the key."""

    for field_name, value in (
        ("owner_principal_id", owner_principal_id),
        ("idempotency_key", idempotency_key),
    ):
        if not value:
            raise ValueError(f"{field_name} must not be empty")
        if value != value.strip():
            raise ValueError(
                f"{field_name} must not contain surrounding whitespace"
            )
        if not value.isprintable():
            raise ValueError(
                f"{field_name} must not contain control characters"
            )
    if len(idempotency_key) > 255:
        raise ValueError("idempotency_key must not exceed 255 characters")
    identity = json.dumps(
        [owner_principal_id, idempotency_key],
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return uuid.uuid5(_REST_REQUEST_NAMESPACE, identity)


def _service_request_fingerprint(
    submission: DecisionServiceRequest,
    *,
    key: bytes,
) -> str:
    if len(key) < _MIN_FINGERPRINT_KEY_BYTES:
        raise DecisionRuntimeNotConfiguredError(
            "decision_correlation_secret must contain at least 32 bytes"
        )
    encoded = _service_request_fingerprint_payload(submission)
    return hmac.new(
        key,
        _DECISION_SERVICE_FINGERPRINT_CONTEXT + encoded,
        hashlib.sha256,
    ).hexdigest()


def _legacy_service_request_fingerprint(
    submission: DecisionServiceRequest,
) -> str:
    return hashlib.sha256(
        _service_request_fingerprint_payload(submission)
    ).hexdigest()


def _service_request_fingerprint_payload(
    submission: DecisionServiceRequest,
) -> bytes:
    payload = submission.model_dump(
        mode="json",
        round_trip=True,
        exclude={"request_id"},
    )
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return encoded


def _service_fingerprint_key(settings: Settings) -> bytes:
    return settings.decision_correlation_secret.get_secret_value().encode(
        "utf-8"
    )


def _require_matching_idempotency_fingerprint(
    stored: str,
    received: str,
) -> None:
    if not hmac.compare_digest(stored, received):
        raise DecisionIdempotencyConflictError(
            "decision request id was reused with different input"
        )


def _caller_channel(submission: DecisionServiceRequest) -> str:
    if submission.ingress is DecisionIngress.REST:
        return DecisionIngress.REST.value
    assert submission.source is not None
    return f"{submission.ingress.value}:{submission.source}"


def _is_terminal_non_retryable(result: DecisionResult) -> bool:
    return (
        result.status
        in {
            DecisionStatus.COMPLETED,
            DecisionStatus.NEEDS_CLARIFICATION,
        }
        and result.persistence_status
        in {
            PersistenceStatus.NOT_REQUIRED,
            PersistenceStatus.PERSISTED,
        }
    )


def _result_receipt_payload(result: DecisionResult) -> dict:
    if (
        result.persistence_status is PersistenceStatus.PERSISTED
        and result.decision_record_id is not None
    ):
        return {
            "schema": _DECISION_RECEIPT_SCHEMA,
            "kind": "decision_record",
            "decision_record_id": str(result.decision_record_id),
        }
    return {
        "schema": _DECISION_RECEIPT_SCHEMA,
        "kind": "transient_result",
        "result": result.model_dump(
            mode="json",
            round_trip=True,
            exclude={"tool_trace"},
        ),
    }


def _transient_result_from_receipt(
    payload: dict,
) -> DecisionResult | None:
    schema_name = payload.get("schema")
    if schema_name == _LEGACY_DECISION_RECEIPT_SCHEMA:
        raw_result = payload.get("result")
        if not isinstance(raw_result, dict):
            raise RuntimeError("decision receipt result is invalid")
        if (
            raw_result.get("persistence_status")
            == PersistenceStatus.PERSISTED.value
            and raw_result.get("decision_record_id") is not None
        ):
            return None
        return DecisionResult.model_validate(
            {**raw_result, "tool_trace": []}
        )
    if schema_name != _DECISION_RECEIPT_SCHEMA:
        raise RuntimeError("unsupported decision receipt schema")
    if payload.get("kind") == "decision_record":
        return None
    try:
        parsed = _TransientReceiptV2.model_validate(payload)
    except Exception as exc:
        raise RuntimeError(
            "decision receipt result is invalid"
        ) from exc
    return parsed.result.model_copy(
        update={"tool_trace": []},
        deep=True,
    )


def _decision_record_id_from_receipt(
    payload: dict,
) -> uuid.UUID | None:
    if payload.get("schema") == _LEGACY_DECISION_RECEIPT_SCHEMA:
        raw_result = payload.get("result")
        if not isinstance(raw_result, dict):
            raise RuntimeError("decision receipt result is invalid")
        if (
            raw_result.get("persistence_status")
            != PersistenceStatus.PERSISTED.value
        ):
            return None
        raw_id = raw_result.get("decision_record_id")
        try:
            return uuid.UUID(str(raw_id))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "persisted decision receipt has no record identity"
            ) from exc
    if payload.get("schema") != _DECISION_RECEIPT_SCHEMA:
        return None
    if payload.get("kind") != "decision_record":
        return None
    try:
        return _PersistedReceiptV2.model_validate(
            payload
        ).decision_record_id
    except Exception as exc:
        raise RuntimeError(
            "persisted decision receipt is invalid"
        ) from exc


async def _claim_with_cancellation_cleanup(
    store: DecisionReceiptStore,
    **kwargs,
):
    """Finish a threaded claim and release its exact generation on cancel."""

    task = asyncio.create_task(
        asyncio.to_thread(store.claim, **kwargs)
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as exc:
        cancellation = exc
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancellation = exc
        try:
            claim = task.result()
        except BaseException:
            claim = None
        if (
            claim is not None
            and claim.state is DecisionReceiptClaimState.ACQUIRED
            and claim.lease_generation is not None
        ):
            cleanup_kwargs = {
                key: value
                for key, value in kwargs.items()
                if key not in {"requested_at"}
            }
            cleanup_kwargs["lease_generation"] = (
                claim.lease_generation
            )
            try:
                await _run_receipt_cleanup(
                    store.release,
                    **cleanup_kwargs,
                )
            except asyncio.CancelledError as exc:
                cancellation = exc
        raise cancellation


async def _run_receipt_cleanup(
    operation: Callable[..., None],
    **kwargs,
) -> None:
    task = asyncio.create_task(asyncio.to_thread(operation, **kwargs))
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            cancelled = exc
            if task.done():
                break
    if task.done():
        task.result()
    if cancelled is not None:
        raise cancelled


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
