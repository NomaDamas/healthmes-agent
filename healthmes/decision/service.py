"""Canonical application ingress for every free-form wellness decision."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
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
    PrivacyLevel,
)

_CHANNEL_REQUEST_NAMESPACE = uuid.UUID(
    "ed5fcd43-39c0-4fb4-b968-57455f1fc9bf"
)
_MAX_COMPLETED_IDEMPOTENT_REQUESTS = 256


class DecisionRuntimeNotConfiguredError(RuntimeError):
    """Raised when an ingress is used without a configured decision engine."""


class DecisionIdempotencyConflictError(RuntimeError):
    """Raised when one request ID is reused for different decision input."""


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

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError(
                "idempotency_key must not contain surrounding whitespace"
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


@dataclass(frozen=True, slots=True)
class _ActiveDecision:
    fingerprint: str
    task: asyncio.Task[DecisionResult]


@dataclass(frozen=True, slots=True)
class _CompletedDecision:
    fingerprint: str
    result: DecisionResult


class HealthMesDecisionService:
    """Build server-owned DecisionRequests and call the one runtime engine."""

    def __init__(
        self,
        *,
        settings: Settings,
        engine_provider: Callable[[], DecisionEngine | None],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(engine_provider):
            raise TypeError("engine_provider must be callable")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._settings = settings
        self._engine_provider = engine_provider
        self._clock = clock or (lambda: datetime.now(UTC))
        self._idempotency_lock = Lock()
        self._active_idempotent_requests: dict[
            uuid.UUID,
            _ActiveDecision,
        ] = {}
        self._completed_idempotent_requests: OrderedDict[
            uuid.UUID,
            _CompletedDecision,
        ] = OrderedDict()

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
        fingerprint = _service_request_fingerprint(submission)
        loop = asyncio.get_running_loop()
        with self._idempotency_lock:
            completed = self._completed_idempotent_requests.get(
                request_id
            )
            if completed is not None:
                _require_matching_idempotency_fingerprint(
                    completed.fingerprint,
                    fingerprint,
                )
                self._completed_idempotent_requests.move_to_end(
                    request_id
                )
                return completed.result

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
                coroutine = self._execute(submission)
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
        return await asyncio.shield(task)

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
        return await engine.ask_wellness(request)

    def _finish_idempotent_request(
        self,
        request_id: uuid.UUID,
        fingerprint: str,
        task: asyncio.Task[DecisionResult],
    ) -> None:
        try:
            result = task.result()
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
            self._completed_idempotent_requests[request_id] = (
                _CompletedDecision(
                    fingerprint=fingerprint,
                    result=result,
                )
            )
            self._completed_idempotent_requests.move_to_end(request_id)
            while (
                len(self._completed_idempotent_requests)
                > _MAX_COMPLETED_IDEMPOTENT_REQUESTS
            ):
                self._completed_idempotent_requests.popitem(last=False)


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
    return uuid.uuid5(
        _CHANNEL_REQUEST_NAMESPACE,
        f"{submission.source}\0{submission.idempotency_key}",
    )


def _service_request_fingerprint(
    submission: DecisionServiceRequest,
) -> str:
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
    return hashlib.sha256(encoded).hexdigest()


def _require_matching_idempotency_fingerprint(
    stored: str,
    received: str,
) -> None:
    if stored != received:
        raise DecisionIdempotencyConflictError(
            "decision request id was reused with different input"
        )


def _caller_channel(submission: DecisionServiceRequest) -> str:
    if submission.ingress is DecisionIngress.REST:
        return DecisionIngress.REST.value
    assert submission.source is not None
    return f"{submission.ingress.value}:{submission.source}"
