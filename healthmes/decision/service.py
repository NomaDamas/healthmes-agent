"""Canonical application ingress for every free-form wellness decision."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
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


class DecisionRuntimeNotConfiguredError(RuntimeError):
    """Raised when an ingress is used without a configured decision engine."""


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

    request_id: uuid.UUID | None = None
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

        request = self.build_request(submission)
        engine = self._engine_provider()
        if engine is None:
            raise DecisionRuntimeNotConfiguredError(
                "HealthMes decision runtime is not configured"
            )
        return await engine.ask_wellness(request)


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
                request_id=submission.request_id,
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


def _caller_channel(submission: DecisionServiceRequest) -> str:
    if submission.ingress is DecisionIngress.REST:
        return DecisionIngress.REST.value
    assert submission.source is not None
    return f"{submission.ingress.value}:{submission.source}"
