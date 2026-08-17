"""Authenticated REST adapter for the HealthMes Decision Agent."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Request, Response, status
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    model_validator,
)
from sqlalchemy import select

from healthmes.activity.locking import (
    activity_write_lock,
    lock_activity_write_plane,
)
from healthmes.api.common import utc_now
from healthmes.api.errors import APIError
from healthmes.decision import (
    DECISION_DOMAINS,
    DecisionContextHints,
    DecisionEngineBusyError,
    DecisionEngineClosedError,
    DecisionIdempotencyConflictError,
    DecisionIngress,
    DecisionResult,
    DecisionRuntimeNotConfiguredError,
    DecisionServiceRequest,
    DecisionStatus,
    ExecutionScope,
    PersistenceStatus,
    RuntimeMetadata,
    SourceRef,
    decision_rest_request_id,
    decision_result_from_record,
    list_decision_domain_policies,
    resolve_decision_execution_scope,
    update_decision_domain_policy,
)
from healthmes.store import DecisionRecord
from healthmes.store.decision_records import (
    decision_record_is_available_at,
)
from healthmes.store.session import SessionDep

router = APIRouter(prefix="/v1/wellness-decisions", tags=["decisions"])

_SERVICE_UNAVAILABLE_REASON_CODES = frozenset(
    {
        "access_policy_resolution_failed",
        "caller_not_authenticated",
        "caller_not_policy_owner",
        "decision_record_contract_invalid",
        "decision_finalization_capacity_exhausted",
        "decision_finalization_timeout",
        "decision_record_persistence_failed",
        "decision_request_id_conflict",
        "decision_run_request_mismatch",
        "decision_source_ref_not_in_tool_trace",
        "decision_source_ref_trace_conflict",
        "decision_source_refs_omitted",
        "decision_stored_source_context_changed",
        "decision_stored_source_contract_changed",
        "decision_text_contains_unvalidated_source_ref",
        "decision_turn_closed",
        "decision_turn_id_conflict",
        "duplicate_tool_call",
        "invalid_decision_finalization_input",
        "invalid_decision_request",
        "invalid_provider_query",
        "malformed_tool_arguments",
        "provider_catalog_invalid",
        "provider_contract_violation",
        "provider_execution_failed",
        "tool_execution_failed",
        "unknown_tool",
    }
)


class WellnessDecisionTimeHints(BaseModel):
    """Narrow caller-provided time constraints, never routing instructions."""

    model_config = ConfigDict(extra="forbid")

    local_date: date | None = None
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    lookback_days: int | None = Field(default=None, ge=1, le=90)

    @model_validator(mode="after")
    def validate_range(self) -> WellnessDecisionTimeHints:
        if (self.start is None) != (self.end is None):
            raise ValueError("start and end must be provided together")
        if self.start is not None and self.end is not None:
            if self.start >= self.end:
                raise ValueError("start must be before end")
        return self

    def to_domain(self) -> DecisionContextHints:
        return DecisionContextHints(
            local_date=self.local_date,
            start=self.start,
            end=self.end,
            lookback_days=self.lookback_days,
        )


class WellnessDecisionInput(BaseModel):
    """Public input: natural language plus optional bounded time hints only."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=8_000)
    persistence_requested: StrictBool = False
    hints: WellnessDecisionTimeHints = Field(
        default_factory=WellnessDecisionTimeHints
    )


class WellnessDecisionOutput(BaseModel):
    """Privacy-minimized decision result for device and web adapters."""

    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID
    turn_id: uuid.UUID
    status: DecisionStatus
    answer: str | None = None
    proposed_action: bool
    source_refs: list[SourceRef]
    limitations: list[str]
    clarification_question: str | None = None
    confidence: float | None = None
    uncertainty: str | None = None
    follow_up_question: str | None = None
    persistence_status: PersistenceStatus
    decision_record_id: uuid.UUID | None = None
    runtime: RuntimeMetadata

    @classmethod
    def from_result(
        cls,
        result: DecisionResult,
    ) -> WellnessDecisionOutput:
        return cls.model_validate(
            result.model_dump(
                mode="python",
                exclude={"tool_trace"},
            )
        )


class DecisionDomainSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: Literal[
        "activity",
        "nutrition",
        "wearable",
        "calendar",
    ]
    enabled: bool


class DecisionDomainSettingsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_scope: ExecutionScope
    domains: list[DecisionDomainSetting]


class DecisionDomainSettingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


def _runtime_unavailable(result: DecisionResult) -> tuple[str, ...]:
    if result.status not in {
        DecisionStatus.BLOCKED,
        DecisionStatus.FAILED,
    }:
        return ()
    return tuple(
        limitation
        for limitation in result.limitations
        if limitation.startswith(("hermes_", "runtime_"))
    )


def _service_unavailable(result: DecisionResult) -> tuple[str, ...]:
    if result.status not in {
        DecisionStatus.BLOCKED,
        DecisionStatus.FAILED,
    }:
        return ()
    return tuple(
        limitation
        for limitation in result.limitations
        if limitation in _SERVICE_UNAVAILABLE_REASON_CODES
    )


async def _wait_for_http_disconnect(request: Request) -> None:
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return


async def _run_until_http_disconnect[T](
    request: Request,
    operation: Awaitable[T],
) -> T:
    """Cancel active reasoning when the actual ASGI client disconnects."""

    decision_task = asyncio.create_task(operation)
    disconnect_task = asyncio.create_task(
        _wait_for_http_disconnect(request)
    )
    try:
        done, _pending = await asyncio.wait(
            (decision_task, disconnect_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if decision_task in done:
            return decision_task.result()

        decision_task.cancel()
        try:
            await decision_task
        except asyncio.CancelledError:
            pass
        raise asyncio.CancelledError
    finally:
        if not decision_task.done():
            decision_task.cancel()
        if not disconnect_task.done():
            disconnect_task.cancel()
        await asyncio.gather(
            decision_task,
            disconnect_task,
            return_exceptions=True,
        )


@router.get(
    "/settings",
    response_model=DecisionDomainSettingsOutput,
)
def get_wellness_decision_settings(
    request: Request,
    session: SessionDep,
) -> DecisionDomainSettingsOutput:
    """Return current server-owned runtime scope and per-domain consent."""

    settings = request.app.state.settings
    rows = {
        row.domain: row.enabled
        for row in list_decision_domain_policies(
            session,
            settings.decision_owner_principal_id,
        )
    }
    return DecisionDomainSettingsOutput(
        execution_scope=resolve_decision_execution_scope(settings),
        domains=[
            DecisionDomainSetting(
                domain=domain,
                enabled=rows.get(domain, False),
            )
            for domain in DECISION_DOMAINS
        ],
    )


@router.put(
    "/settings/{domain}",
    response_model=DecisionDomainSetting,
)
def put_wellness_decision_setting(
    domain: Literal[
        "activity",
        "nutrition",
        "wearable",
        "calendar",
    ],
    body: DecisionDomainSettingUpdate,
    request: Request,
    session: SessionDep,
) -> DecisionDomainSetting:
    """Atomically change one domain consent switch without a UI dependency."""

    with activity_write_lock():
        lock_activity_write_plane(session)
        row = update_decision_domain_policy(
            session,
            request.app.state.settings.decision_owner_principal_id,
            domain,
            enabled=body.enabled,
        )
        session.commit()
    return DecisionDomainSetting(
        domain=row.domain,
        enabled=row.enabled,
    )


@router.get(
    "/{request_id}",
    response_model=WellnessDecisionOutput,
)
def get_wellness_decision_result(
    request_id: uuid.UUID,
    session: SessionDep,
) -> WellnessDecisionOutput:
    """Recover a committed result after a 202 outcome-unknown response."""

    now = utc_now()
    record = session.scalar(
        select(DecisionRecord).where(
            DecisionRecord.decision_request_id == request_id,
            decision_record_is_available_at(now),
        )
    )
    if record is None:
        raise APIError(
            status.HTTP_404_NOT_FOUND,
            "wellness_decision_not_found",
            "Wellness decision is not available.",
        )
    try:
        result = decision_result_from_record(record, now=now)
    except ValueError as exc:
        raise APIError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "decision_record_contract_invalid",
            "The stored wellness decision could not be verified.",
        ) from exc
    return WellnessDecisionOutput.from_result(result)


@router.post("", response_model=WellnessDecisionOutput)
async def create_wellness_decision(
    body: WellnessDecisionInput,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=255,
        ),
    ],
) -> WellnessDecisionOutput:
    """Run one server-owned, aggregate-only local decision turn."""

    try:
        service_request = DecisionServiceRequest(
            request_id=decision_rest_request_id(
                owner_principal_id=(
                    request.app.state.settings.decision_owner_principal_id
                ),
                idempotency_key=idempotency_key,
            ),
            question=body.question,
            ingress=DecisionIngress.REST,
            persistence_requested=body.persistence_requested,
            hints=body.hints.to_domain(),
        )
    except ValidationError as exc:
        raise APIError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_error",
            "Request validation failed",
            detail=exc.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            ),
        ) from exc

    decision_service = request.app.state.decision_service
    try:
        result = await _run_until_http_disconnect(
            request,
            decision_service.ask_wellness(service_request),
        )
    except ValidationError as exc:
        raise APIError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_error",
            "Request validation failed",
            detail=exc.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            ),
        ) from exc
    except DecisionRuntimeNotConfiguredError as exc:
        raise APIError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "decision_runtime_not_configured",
            "The HealthMes decision runtime is not configured.",
        ) from exc
    except DecisionEngineBusyError as exc:
        raise APIError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "decision_engine_busy",
            "The HealthMes decision engine is at capacity.",
        ) from exc
    except DecisionEngineClosedError as exc:
        raise APIError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "decision_engine_closing",
            "The HealthMes decision engine is shutting down.",
        ) from exc
    except DecisionIdempotencyConflictError as exc:
        raise APIError(
            status.HTTP_409_CONFLICT,
            "decision_idempotency_conflict",
            "Idempotency-Key was already used for different input.",
        ) from exc

    if result.persistence_status is PersistenceStatus.UNKNOWN:
        response.status_code = status.HTTP_202_ACCEPTED
        response.headers["Location"] = (
            f"/v1/wellness-decisions/{result.request_id}"
        )
        return WellnessDecisionOutput.from_result(result)

    unavailable = _runtime_unavailable(result)
    if unavailable:
        raise APIError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "decision_runtime_unavailable",
            "Hermes does not currently provide the required decision runtime.",
            detail={"reason_codes": list(unavailable)},
        )
    unavailable = _service_unavailable(result)
    if unavailable:
        raise APIError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "decision_service_unavailable",
            "HealthMes could not complete the decision because a required "
            "internal component was unavailable.",
            detail={"reason_codes": list(unavailable)},
        )
    return WellnessDecisionOutput.from_result(result)
