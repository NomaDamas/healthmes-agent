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
from typing import Protocol

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)
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
from healthmes.store.decision_receipts import (
    DecisionReceiptClaimState,
    DecisionReceiptConflictError,
    DecisionReceiptExpiredError,
    DecisionReceiptOwnershipError,
    DecisionReceiptStore,
)

_CHANNEL_REQUEST_NAMESPACE = uuid.UUID(
    "ed5fcd43-39c0-4fb4-b968-57455f1fc9bf"
)
_REST_REQUEST_NAMESPACE = uuid.UUID(
    "d8fcb625-cade-45a4-b27f-63a6c06c9719"
)
_DECISION_RECEIPT_RETENTION = timedelta(days=30)
_DECISION_RECEIPT_SCHEMA = "healthmes.decision-receipt.v1"
_DECISION_SERVICE_FINGERPRINT_CONTEXT = (
    b"healthmes-decision-service-request-fingerprint-v1\x00"
)
_MIN_FINGERPRINT_KEY_BYTES = 32


class DecisionRuntimeNotConfiguredError(RuntimeError):
    """Raised when an ingress is used without a configured decision engine."""


class DecisionIdempotencyConflictError(RuntimeError):
    """Raised when one request ID is reused for different decision input."""


class DecisionIdempotencyExpiredError(RuntimeError):
    """Raised when a retained identity no longer has a replayable result."""


class DecisionIdempotencyUnavailableError(RuntimeError):
    """Raised when a durable request cannot safely converge yet."""


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


class DecisionService(Protocol):
    """Canonical service surface allowed behind product ingress adapters."""

    async def ask_wellness(
        self,
        submission: DecisionServiceRequest,
    ) -> DecisionResult: ...


@dataclass(slots=True)
class _ActiveDecision:
    fingerprint: str
    task: asyncio.Task[_IdempotentExecution]
    waiters: int = 0


@dataclass(frozen=True, slots=True)
class _IdempotentExecution:
    result: DecisionResult
    cache_expires_at: datetime | None


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
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(engine_provider):
            raise TypeError("engine_provider must be callable")
        if not callable(session_factory_provider):
            raise TypeError("session_factory_provider must be callable")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._settings = settings
        self._engine_provider = engine_provider
        self._session_factory_provider = session_factory_provider
        self._clock = clock or (lambda: datetime.now(UTC))
        self._idempotency_lock = Lock()
        self._receipt_store: DecisionReceiptStore | None = None
        self._active_idempotent_requests: dict[
            uuid.UUID,
            _ActiveDecision,
        ] = {}

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
                        self._active_idempotent_requests.pop(
                            request_id,
                            None,
                        )
            if cancel_orphaned_task:
                task.cancel()

    async def _execute(
        self,
        submission: DecisionServiceRequest,
    ) -> DecisionResult:
        request = self.build_request(submission)
        engine = self._engine_provider()
        if engine is None:
            raise DecisionRuntimeNotConfiguredError(
                "HealthMes decision runtime is not configured"
            )
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
                        result=_result_from_receipt(claim.result_payload),
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
            try:
                result = await self._execute(frozen_submission)
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
                    canonical = await self._converge_after_lease_loss(
                        store,
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
                return _IdempotentExecution(
                    result=_result_from_receipt(
                        completion.result_payload
                    ),
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

    async def _converge_after_lease_loss(
        self,
        store: DecisionReceiptStore,
        *,
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
                    result=_result_from_receipt(
                        observation.result_payload
                    ),
                    cache_expires_at=_as_utc(
                        observation.expires_at
                    ),
                )
            if observation.lease_expired:
                return None
            await asyncio.sleep(observation.retry_after_seconds)

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
            active = self._active_idempotent_requests.get(request_id)
            if active is None or active.task is not task:
                return
            self._active_idempotent_requests.pop(request_id, None)
            del execution, fingerprint


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
    return {
        "schema": _DECISION_RECEIPT_SCHEMA,
        "result": result.model_dump(
            mode="json",
            round_trip=True,
            exclude={"tool_trace"},
        ),
    }


def _result_from_receipt(payload: dict) -> DecisionResult:
    if payload.get("schema") != _DECISION_RECEIPT_SCHEMA:
        raise RuntimeError("unsupported decision receipt schema")
    raw_result = payload.get("result")
    if not isinstance(raw_result, dict):
        raise RuntimeError("decision receipt result is invalid")
    return DecisionResult.model_validate(
        {**raw_result, "tool_trace": []}
    )


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
