from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from healthmes.decision import (
    DecisionBudget,
    DecisionChannelAdapter,
    DecisionChannelRequest,
    DecisionContextHints,
    DecisionIdempotencyConflictError,
    DecisionIngress,
    DecisionRuntimeNotConfiguredError,
    DecisionServiceRequest,
    ExecutionScope,
    HealthMesDecisionService,
    PrivacyLevel,
)

NOW = datetime(2026, 8, 16, 9, tzinfo=UTC)


class RecordingEngine:
    def __init__(self) -> None:
        self.requests = []

    async def ask_wellness(self, request):
        self.requests.append(request)
        return request


class RecordingService:
    def __init__(self, result) -> None:
        self.result = result
        self.submissions = []

    async def ask_wellness(self, submission):
        self.submissions.append(submission)
        return self.result


@pytest.mark.asyncio
async def test_channel_adapter_forwards_contract_to_canonical_service_once() -> None:
    result = object()
    service = RecordingService(result)
    adapter = DecisionChannelAdapter(service=service)
    requested_at = datetime(2026, 8, 16, 10, 30, tzinfo=UTC)
    budget = DecisionBudget(
        max_tool_calls=4,
        max_source_refs=25,
        max_context_bytes=8_000,
    )
    hints = DecisionContextHints(
        local_date=requested_at.date(),
        related_record_ids={"nutrition": "capture-123"},
    )

    returned = await adapter.ask_wellness(
        DecisionChannelRequest(
            idempotency_key="ios-message-123",
            question="Can I have coffee before the next meeting?",
            source="future-ios-app",
            session_id="device-session-42",
            requested_at=requested_at,
            requested_privacy_level=PrivacyLevel.IDENTITY,
            persistence_requested=True,
            budget=budget,
            hints=hints,
        )
    )

    assert returned is result
    assert len(service.submissions) == 1
    [submission] = service.submissions
    assert submission.request_id == uuid.UUID(
        "2e8fd434-5b44-50c6-9ce8-ad6a2d333b08"
    )
    assert submission == DecisionServiceRequest(
        request_id=submission.request_id,
        question="Can I have coffee before the next meeting?",
        ingress=DecisionIngress.CHANNEL,
        source="future-ios-app",
        session_id="device-session-42",
        requested_at=requested_at,
        requested_privacy_level=PrivacyLevel.IDENTITY,
        persistence_requested=True,
        budget=budget,
        hints=hints,
    )


def test_channel_adapter_requires_a_stable_inbound_idempotency_key() -> None:
    with pytest.raises(ValidationError, match="idempotency_key"):
        DecisionChannelRequest.model_validate(
            {
                "question": "Should I rest?",
                "source": "future-ios-app",
            }
        )

    with pytest.raises(ValidationError, match="surrounding whitespace"):
        DecisionChannelRequest(
            idempotency_key=" message-123 ",
            question="Should I rest?",
            source="future-ios-app",
        )


class BlockingRecordingEngine(RecordingEngine):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def ask_wellness(self, request):
        self.requests.append(request)
        self.started.set()
        await self.release.wait()
        return request


@pytest.mark.asyncio
async def test_identical_channel_retries_execute_the_engine_once(
    settings,
) -> None:
    engine = BlockingRecordingEngine()
    service = HealthMesDecisionService(
        settings=settings,
        engine_provider=lambda: engine,
        clock=lambda: NOW,
    )
    adapter = DecisionChannelAdapter(service=service)
    submission = DecisionChannelRequest(
        idempotency_key="telegram-update-987",
        question="Should I stop working for today?",
        source="telegram",
        session_id="owner-chat",
    )

    first = asyncio.create_task(adapter.ask_wellness(submission))
    await asyncio.wait_for(engine.started.wait(), timeout=1)
    retry = asyncio.create_task(adapter.ask_wellness(submission))
    await asyncio.sleep(0)

    assert len(engine.requests) == 1
    engine.release.set()
    first_result, retry_result = await asyncio.gather(first, retry)
    cached_result = await adapter.ask_wellness(submission)

    assert first_result is retry_result is cached_result
    assert len(engine.requests) == 1


@pytest.mark.asyncio
async def test_channel_idempotency_key_rejects_different_input(
    settings,
) -> None:
    engine = RecordingEngine()
    service = HealthMesDecisionService(
        settings=settings,
        engine_provider=lambda: engine,
        clock=lambda: NOW,
    )
    adapter = DecisionChannelAdapter(service=service)
    original = DecisionChannelRequest(
        idempotency_key="ios-message-456",
        question="Should I have coffee?",
        source="future-ios-app",
    )
    conflicting = original.model_copy(
        update={"question": "Should I go to sleep?"}
    )

    await adapter.ask_wellness(original)
    with pytest.raises(DecisionIdempotencyConflictError):
        await adapter.ask_wellness(conflicting)

    assert len(engine.requests) == 1


@pytest.mark.asyncio
async def test_all_reasoning_ingresses_use_one_server_owned_service(
    settings,
) -> None:
    engine = RecordingEngine()
    configured = settings.model_copy(
        update={
            "decision_owner_principal_id": "local-owner",
            "decision_execution_scope": "local",
            "timezone": "Asia/Seoul",
        }
    )
    service = HealthMesDecisionService(
        settings=configured,
        engine_provider=lambda: engine,
        clock=lambda: NOW,
    )
    submissions = (
        DecisionServiceRequest(
            question="REST question",
            ingress=DecisionIngress.REST,
        ),
        DecisionServiceRequest(
            question="Channel question",
            ingress=DecisionIngress.CHANNEL,
            source="telegram",
            session_id="channel-session",
        ),
        DecisionServiceRequest(
            question="Proactive question",
            ingress=DecisionIngress.PROACTIVE,
            source="activity-trigger",
        ),
        DecisionServiceRequest(
            question="Scheduled question",
            ingress=DecisionIngress.SCHEDULED,
            source="morning-briefing",
        ),
    )

    for submission in submissions:
        returned = await service.ask_wellness(submission)
        assert returned is engine.requests[-1]

    assert [request.caller.channel for request in engine.requests] == [
        "rest",
        "channel:telegram",
        "proactive:activity-trigger",
        "scheduled:morning-briefing",
    ]
    assert all(
        request.caller.principal_id == "local-owner"
        and request.caller.authenticated is True
        and request.caller.execution_scope is ExecutionScope.LOCAL
        and request.timezone == "Asia/Seoul"
        and request.requested_at == NOW
        and request.requested_privacy_level
        is PrivacyLevel.AGGREGATE
        for request in engine.requests
    )
    assert engine.requests[1].caller.session_id == "channel-session"


@pytest.mark.parametrize(
    ("ingress", "source"),
    (
        (DecisionIngress.REST, "caller-override"),
        (DecisionIngress.CHANNEL, None),
        (DecisionIngress.PROACTIVE, None),
        (DecisionIngress.SCHEDULED, None),
    ),
)
def test_ingress_contract_rejects_ambiguous_sources(
    ingress,
    source,
) -> None:
    with pytest.raises(ValueError):
        DecisionServiceRequest(
            question="Should I rest?",
            ingress=ingress,
            source=source,
        )


@pytest.mark.asyncio
async def test_service_fails_closed_without_a_runtime(settings) -> None:
    service = HealthMesDecisionService(
        settings=settings,
        engine_provider=lambda: None,
        clock=lambda: NOW,
    )

    with pytest.raises(DecisionRuntimeNotConfiguredError):
        await service.ask_wellness(
            DecisionServiceRequest(
                question="Should I rest?",
                ingress=DecisionIngress.REST,
            )
        )


@pytest.mark.asyncio
async def test_service_preserves_server_supplied_idempotency_key(
    settings,
) -> None:
    engine = RecordingEngine()
    request_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    service = HealthMesDecisionService(
        settings=settings,
        engine_provider=lambda: engine,
        clock=lambda: NOW,
    )

    result = await service.ask_wellness(
        DecisionServiceRequest(
            request_id=request_id,
            question="Should I rest?",
            ingress=DecisionIngress.PROACTIVE,
            source="focus-fragmentation",
        )
    )

    assert result.request_id == request_id
