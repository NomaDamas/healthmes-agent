from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from healthmes.decision import (
    DecisionBudget,
    DecisionChannelAdapter,
    DecisionChannelRequest,
    DecisionContextHints,
    DecisionIdempotencyConflictError,
    DecisionIngress,
    DecisionResult,
    DecisionRuntimeNotConfiguredError,
    DecisionServiceRequest,
    DecisionStatus,
    ExecutionScope,
    HealthMesDecisionService,
    PersistenceStatus,
    PrivacyLevel,
    RuntimeMetadata,
)
from healthmes.store import (
    Base,
    DecisionRequestReceipt,
    create_db_engine,
)
from healthmes.store.decision_receipts import (
    DecisionReceiptClaimState,
    DecisionReceiptStore,
)

NOW = datetime(2026, 8, 16, 9, tzinfo=UTC)


@pytest.fixture
def service_session_factory(tmp_path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path}/decision-service.db"
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
    )
    yield factory
    engine.dispose()


def _completed_result(
    request,
    *,
    answer: str = "Use the available context and take a short break.",
):
    return DecisionResult(
        request_id=request.request_id,
        turn_id=request.turn_id,
        status=DecisionStatus.COMPLETED,
        answer=answer,
        persistence_status=PersistenceStatus.NOT_REQUIRED,
        runtime=RuntimeMetadata(runtime="test"),
    )


class RecordingEngine:
    def __init__(self) -> None:
        self.requests = []

    async def ask_wellness(self, request):
        self.requests.append(request)
        return _completed_result(request)


class RecordingService:
    def __init__(self, result) -> None:
        self.result = result
        self.submissions = []

    async def ask_wellness(self, submission):
        self.submissions.append(submission)
        return self.result


class MutableClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


class ClaimObservingReceiptStore(DecisionReceiptStore):
    def __init__(
        self,
        *,
        wait_reached: threading.Event,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.wait_reached = wait_reached

    def claim(self, **kwargs):
        claim = super().claim(**kwargs)
        if claim.state is DecisionReceiptClaimState.WAIT:
            self.wait_reached.set()
        return claim


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
        "da32bf51-cf29-5bc0-ab37-3169f6473b02"
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

    for invalid in ("\0message", "message\n123"):
        with pytest.raises(
            ValidationError,
            match="control characters",
        ):
            DecisionChannelRequest(
                idempotency_key=invalid,
                question="Should I rest?",
                source="future-ios-app",
            )

    with pytest.raises(ValidationError, match="control characters"):
        DecisionChannelRequest(
            idempotency_key="message-123",
            question="Should I rest?",
            source="future\0ios",
        )


@pytest.mark.asyncio
async def test_channel_request_identity_has_no_component_boundary_collision() -> None:
    service = RecordingService(object())
    adapter = DecisionChannelAdapter(service=service)

    await adapter.ask_wellness(
        DecisionChannelRequest(
            idempotency_key="b:c",
            question="First question",
            source="a",
        )
    )
    await adapter.ask_wellness(
        DecisionChannelRequest(
            idempotency_key="c",
            question="Second question",
            source="a:b",
        )
    )

    first, second = service.submissions
    assert first.request_id != second.request_id


class BlockingRecordingEngine(RecordingEngine):
    def __init__(
        self,
        *,
        answer: str = "Use the available context and take a short break.",
    ) -> None:
        super().__init__()
        self.answer = answer
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def ask_wellness(self, request):
        self.requests.append(request)
        self.started.set()
        await self.release.wait()
        return _completed_result(request, answer=self.answer)


class AnswerRecordingEngine(RecordingEngine):
    def __init__(self, answer: str) -> None:
        super().__init__()
        self.answer = answer

    async def ask_wellness(self, request):
        self.requests.append(request)
        return _completed_result(request, answer=self.answer)


class FailedThenCompletedEngine(RecordingEngine):
    async def ask_wellness(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            return DecisionResult(
                request_id=request.request_id,
                turn_id=request.turn_id,
                status=DecisionStatus.FAILED,
                limitations=["runtime_timeout"],
                persistence_status=PersistenceStatus.NOT_REQUIRED,
                runtime=RuntimeMetadata(runtime="test"),
            )
        return _completed_result(request)


class BlockedThenCompletedEngine(RecordingEngine):
    async def ask_wellness(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            return DecisionResult(
                request_id=request.request_id,
                turn_id=request.turn_id,
                status=DecisionStatus.BLOCKED,
                limitations=["hermes_responses_timeout"],
                persistence_status=PersistenceStatus.NOT_REQUIRED,
                runtime=RuntimeMetadata(runtime="test"),
            )
        return _completed_result(request)


class BlockingFailedThenCompletedEngine(FailedThenCompletedEngine):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def ask_wellness(self, request):
        if not self.requests:
            self.requests.append(request)
            self.started.set()
            await self.release.wait()
            return DecisionResult(
                request_id=request.request_id,
                turn_id=request.turn_id,
                status=DecisionStatus.FAILED,
                limitations=["runtime_timeout"],
                persistence_status=PersistenceStatus.NOT_REQUIRED,
                runtime=RuntimeMetadata(runtime="test"),
            )
        self.requests.append(request)
        return _completed_result(request)


class PersistenceFailedThenCompletedEngine(RecordingEngine):
    async def ask_wellness(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            return DecisionResult(
                request_id=request.request_id,
                turn_id=request.turn_id,
                status=DecisionStatus.COMPLETED,
                answer="The answer could not be persisted.",
                persistence_status=PersistenceStatus.FAILED,
                runtime=RuntimeMetadata(runtime="test"),
            )
        return _completed_result(request)


class UnknownThenCompletedEngine(RecordingEngine):
    async def ask_wellness(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            return DecisionResult(
                request_id=request.request_id,
                turn_id=request.turn_id,
                status=DecisionStatus.FAILED,
                limitations=["decision_finalization_outcome_unknown"],
                persistence_status=PersistenceStatus.UNKNOWN,
                runtime=RuntimeMetadata(runtime="test"),
            )
        return _completed_result(request)


@pytest.mark.asyncio
async def test_identical_channel_retries_execute_the_engine_once(
    settings,
    service_session_factory,
) -> None:
    engine = BlockingRecordingEngine()
    service = HealthMesDecisionService(
        settings=settings,
        engine_provider=lambda: engine,
        session_factory_provider=lambda: service_session_factory,
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
async def test_cancelling_one_waiter_preserves_shared_execution(
    settings,
    service_session_factory,
) -> None:
    engine = BlockingRecordingEngine()
    adapter = DecisionChannelAdapter(
        service=HealthMesDecisionService(
            settings=settings,
            engine_provider=lambda: engine,
            session_factory_provider=lambda: service_session_factory,
            clock=lambda: NOW,
        )
    )
    submission = DecisionChannelRequest(
        idempotency_key="shared-request-with-cancelled-waiter",
        question="Should I stop working for today?",
        source="future-ios-app",
    )

    cancelled_waiter = asyncio.create_task(
        adapter.ask_wellness(submission)
    )
    await asyncio.wait_for(engine.started.wait(), timeout=1)
    surviving_waiter = asyncio.create_task(
        adapter.ask_wellness(submission)
    )
    await asyncio.sleep(0)

    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter
    assert not surviving_waiter.done()
    assert len(engine.requests) == 1

    engine.release.set()
    result = await surviving_waiter

    assert result.status is DecisionStatus.COMPLETED
    assert len(engine.requests) == 1


@pytest.mark.asyncio
async def test_sqlite_cross_service_concurrency_and_restart_are_durable(
    settings,
    service_session_factory,
) -> None:
    owner_engine = BlockingRecordingEngine()
    waiting_engine = RecordingEngine()
    wait_reached = threading.Event()
    owner = DecisionChannelAdapter(
        service=HealthMesDecisionService(
            settings=settings,
            engine_provider=lambda: owner_engine,
            session_factory_provider=lambda: service_session_factory,
            clock=lambda: NOW,
        )
    )
    waiting_service = HealthMesDecisionService(
        settings=settings,
        engine_provider=lambda: waiting_engine,
        session_factory_provider=lambda: service_session_factory,
        clock=lambda: NOW,
    )
    waiting_service._receipt_store = ClaimObservingReceiptStore(
        session_factory=service_session_factory,
        lease_duration=timedelta(minutes=5),
        retention=timedelta(days=30),
        wait_reached=wait_reached,
    )
    waiter = DecisionChannelAdapter(
        service=waiting_service
    )
    submission = DecisionChannelRequest(
        idempotency_key="offline-message-1",
        question="Should I take a break?",
        source="future-ios-app",
    )

    first = asyncio.create_task(owner.ask_wellness(submission))
    await asyncio.wait_for(owner_engine.started.wait(), timeout=1)
    delayed_retry = asyncio.create_task(waiter.ask_wellness(submission))
    assert await asyncio.to_thread(wait_reached.wait, 1)
    assert waiting_engine.requests == []

    owner_engine.release.set()
    first_result, retry_result = await asyncio.gather(
        first,
        delayed_retry,
    )
    assert retry_result == first_result
    assert len(owner_engine.requests) == 1
    assert waiting_engine.requests == []

    conflicting_engine = RecordingEngine()
    conflicting = DecisionChannelAdapter(
        service=HealthMesDecisionService(
            settings=settings,
            engine_provider=lambda: conflicting_engine,
            session_factory_provider=lambda: service_session_factory,
            clock=lambda: NOW,
        )
    )
    with pytest.raises(DecisionIdempotencyConflictError):
        await conflicting.ask_wellness(
            submission.model_copy(
                update={"question": "Should I drink coffee?"}
            )
        )
    assert conflicting_engine.requests == []

    restarted_engine = RecordingEngine()
    restarted = DecisionChannelAdapter(
        service=HealthMesDecisionService(
            settings=settings,
            engine_provider=lambda: restarted_engine,
            session_factory_provider=lambda: service_session_factory,
            clock=lambda: NOW,
        )
    )
    restarted_result = await restarted.ask_wellness(submission)

    assert restarted_result == first_result
    assert restarted_engine.requests == []
    with service_session_factory() as session:
        [receipt] = session.scalars(
            select(DecisionRequestReceipt)
        ).all()
        assert receipt.state == "completed"
        assert receipt.owner_token is None
        assert receipt.lease_expires_at is None
        serialized = str(receipt.result_payload)
        assert submission.question not in serialized
        assert "tool_trace" not in serialized


def test_sqlite_expired_lease_preserves_first_committed_result(
    service_session_factory,
) -> None:
    store = DecisionReceiptStore(
        session_factory=service_session_factory,
        lease_duration=timedelta(seconds=1),
        retention=timedelta(days=30),
    )
    request_id = uuid.uuid4()
    fingerprint = "a" * 64
    first_owner = uuid.uuid4()
    takeover_owner = uuid.uuid4()

    first_claim = store.claim(
        request_id=request_id,
        fingerprint=fingerprint,
        owner_token=first_owner,
        now=NOW,
    )
    takeover_claim = store.claim(
        request_id=request_id,
        fingerprint=fingerprint,
        owner_token=takeover_owner,
        now=NOW + timedelta(seconds=2),
    )
    assert first_claim.state is DecisionReceiptClaimState.ACQUIRED
    assert takeover_claim.state is DecisionReceiptClaimState.ACQUIRED

    canonical = {"schema": "test", "winner": "takeover"}
    store.complete(
        request_id=request_id,
        fingerprint=fingerprint,
        owner_token=takeover_owner,
        result_payload=canonical,
        now=NOW + timedelta(seconds=2),
    )
    delayed_completion = store.complete(
        request_id=request_id,
        fingerprint=fingerprint,
        owner_token=first_owner,
        result_payload={"schema": "test", "winner": "expired-owner"},
        now=NOW + timedelta(seconds=3),
    )

    assert delayed_completion.result_payload == canonical


@pytest.mark.asyncio
async def test_sqlite_lease_takeover_returns_one_canonical_service_result(
    settings,
    service_session_factory,
) -> None:
    store = DecisionReceiptStore(
        session_factory=service_session_factory,
        lease_duration=timedelta(seconds=1),
        retention=timedelta(days=30),
    )
    expired_engine = BlockingRecordingEngine(answer="Expired owner answer.")
    takeover_engine = AnswerRecordingEngine("Canonical takeover answer.")
    expired_service = HealthMesDecisionService(
        settings=settings,
        engine_provider=lambda: expired_engine,
        session_factory_provider=lambda: service_session_factory,
        clock=lambda: NOW,
    )
    takeover_service = HealthMesDecisionService(
        settings=settings,
        engine_provider=lambda: takeover_engine,
        session_factory_provider=lambda: service_session_factory,
        clock=lambda: NOW + timedelta(seconds=2),
    )
    expired_service._receipt_store = store
    takeover_service._receipt_store = store
    submission = DecisionServiceRequest(
        request_id=uuid.uuid4(),
        question="Should I take a break?",
        ingress=DecisionIngress.CHANNEL,
        source="future-ios-app",
    )

    expired_call = asyncio.create_task(
        expired_service.ask_wellness(submission)
    )
    await asyncio.wait_for(expired_engine.started.wait(), timeout=1)
    takeover_result = await takeover_service.ask_wellness(submission)
    expired_engine.release.set()
    expired_result = await expired_call

    assert takeover_result.answer == "Canonical takeover answer."
    assert expired_result == takeover_result
    assert len(expired_engine.requests) == 1
    assert len(takeover_engine.requests) == 1


@pytest.mark.asyncio
async def test_stale_owner_waits_for_inflight_canonical_receipt(
    settings,
    service_session_factory,
) -> None:
    clock = MutableClock(NOW)
    store = DecisionReceiptStore(
        session_factory=service_session_factory,
        lease_duration=timedelta(seconds=1),
        retention=timedelta(days=30),
    )
    stale_engine = BlockingRecordingEngine(answer="Stale answer.")
    canonical_engine = BlockingRecordingEngine(
        answer="Canonical answer."
    )
    stale_service = HealthMesDecisionService(
        settings=settings,
        engine_provider=lambda: stale_engine,
        session_factory_provider=lambda: service_session_factory,
        clock=clock,
    )
    canonical_service = HealthMesDecisionService(
        settings=settings,
        engine_provider=lambda: canonical_engine,
        session_factory_provider=lambda: service_session_factory,
        clock=clock,
    )
    stale_service._receipt_store = store
    canonical_service._receipt_store = store
    submission = DecisionServiceRequest(
        request_id=uuid.uuid4(),
        question="Should I take a break?",
        ingress=DecisionIngress.CHANNEL,
        source="future-ios-app",
    )

    stale_call = asyncio.create_task(
        stale_service.ask_wellness(submission)
    )
    await asyncio.wait_for(stale_engine.started.wait(), timeout=1)
    clock.advance(timedelta(seconds=2))
    canonical_call = asyncio.create_task(
        canonical_service.ask_wellness(submission)
    )
    await asyncio.wait_for(canonical_engine.started.wait(), timeout=1)

    stale_engine.release.set()
    await asyncio.sleep(0.05)
    assert not stale_call.done()

    canonical_engine.release.set()
    canonical_result, stale_result = await asyncio.gather(
        canonical_call,
        stale_call,
    )
    assert canonical_result.answer == "Canonical answer."
    assert stale_result == canonical_result


@pytest.mark.asyncio
async def test_failed_result_is_released_and_retried(
    settings,
    service_session_factory,
) -> None:
    engine = BlockingFailedThenCompletedEngine()
    adapter = DecisionChannelAdapter(
        service=HealthMesDecisionService(
            settings=settings,
            engine_provider=lambda: engine,
            session_factory_provider=lambda: service_session_factory,
            clock=lambda: NOW,
        )
    )
    submission = DecisionChannelRequest(
        idempotency_key="retryable-failure-1",
        question="Should I keep working?",
        source="future-ios-app",
    )

    first = asyncio.create_task(adapter.ask_wellness(submission))
    await asyncio.wait_for(engine.started.wait(), timeout=1)
    concurrent = asyncio.create_task(adapter.ask_wellness(submission))
    await asyncio.sleep(0)
    assert len(engine.requests) == 1

    engine.release.set()
    failed, same_failed = await asyncio.gather(first, concurrent)
    completed = await adapter.ask_wellness(submission)

    assert failed.status is DecisionStatus.FAILED
    assert same_failed is failed
    assert completed.status is DecisionStatus.COMPLETED
    assert len(engine.requests) == 2
    with service_session_factory() as session:
        [receipt] = session.scalars(
            select(DecisionRequestReceipt)
        ).all()
        assert receipt.state == "completed"


@pytest.mark.asyncio
async def test_transient_blocked_result_is_released_and_retried(
    settings,
    service_session_factory,
) -> None:
    engine = BlockedThenCompletedEngine()
    adapter = DecisionChannelAdapter(
        service=HealthMesDecisionService(
            settings=settings,
            engine_provider=lambda: engine,
            session_factory_provider=lambda: service_session_factory,
            clock=lambda: NOW,
        )
    )
    submission = DecisionChannelRequest(
        idempotency_key="retryable-blocked-1",
        question="Should I keep working?",
        source="future-ios-app",
    )

    blocked = await adapter.ask_wellness(submission)
    completed = await adapter.ask_wellness(submission)

    assert blocked.status is DecisionStatus.BLOCKED
    assert blocked.limitations == ["hermes_responses_timeout"]
    assert completed.status is DecisionStatus.COMPLETED
    assert len(engine.requests) == 2


@pytest.mark.asyncio
async def test_persistence_failed_result_is_not_cached(
    settings,
    service_session_factory,
) -> None:
    engine = PersistenceFailedThenCompletedEngine()
    adapter = DecisionChannelAdapter(
        service=HealthMesDecisionService(
            settings=settings,
            engine_provider=lambda: engine,
            session_factory_provider=lambda: service_session_factory,
            clock=lambda: NOW,
        )
    )
    submission = DecisionChannelRequest(
        idempotency_key="retryable-persistence-1",
        question="Should this decision be recorded?",
        source="future-ios-app",
    )

    failed = await adapter.ask_wellness(submission)
    completed = await adapter.ask_wellness(submission)

    assert failed.persistence_status is PersistenceStatus.FAILED
    assert completed.status is DecisionStatus.COMPLETED
    assert len(engine.requests) == 2


@pytest.mark.asyncio
async def test_unknown_retry_reuses_first_requested_at(
    settings,
    service_session_factory,
) -> None:
    clock = MutableClock(NOW)
    engine = UnknownThenCompletedEngine()
    adapter = DecisionChannelAdapter(
        service=HealthMesDecisionService(
            settings=settings,
            engine_provider=lambda: engine,
            session_factory_provider=lambda: service_session_factory,
            clock=clock,
        )
    )
    submission = DecisionChannelRequest(
        idempotency_key="unknown-retry-requested-at-1",
        question="Should this decision be recorded?",
        source="future-ios-app",
    )

    unknown = await adapter.ask_wellness(submission)
    clock.advance(timedelta(hours=3))
    completed = await adapter.ask_wellness(submission)

    assert unknown.persistence_status is PersistenceStatus.UNKNOWN
    assert completed.status is DecisionStatus.COMPLETED
    assert [request.requested_at for request in engine.requests] == [
        NOW,
        NOW,
    ]
    with service_session_factory() as session:
        [receipt] = session.scalars(
            select(DecisionRequestReceipt)
        ).all()
        stored_requested_at = receipt.requested_at
        if stored_requested_at.tzinfo is None:
            stored_requested_at = stored_requested_at.replace(
                tzinfo=UTC
            )
        assert stored_requested_at.astimezone(UTC) == NOW


@pytest.mark.asyncio
async def test_durable_receipt_survives_memory_lru_eviction(
    settings,
    service_session_factory,
) -> None:
    engine = RecordingEngine()
    adapter = DecisionChannelAdapter(
        service=HealthMesDecisionService(
            settings=settings,
            engine_provider=lambda: engine,
            session_factory_provider=lambda: service_session_factory,
            clock=lambda: NOW,
        )
    )
    first = DecisionChannelRequest(
        idempotency_key="lru-0",
        question="Question 0",
        source="future-ios-app",
    )
    first_result = await adapter.ask_wellness(first)
    for index in range(1, 258):
        await adapter.ask_wellness(
            DecisionChannelRequest(
                idempotency_key=f"lru-{index}",
                question=f"Question {index}",
                source="future-ios-app",
            )
        )

    replay = await adapter.ask_wellness(first)

    assert replay == first_result
    assert len(engine.requests) == 258


@pytest.mark.asyncio
async def test_completed_receipt_and_memory_cache_expire_at_original_cutoff(
    settings,
    service_session_factory,
) -> None:
    clock = MutableClock(NOW)
    original_engine = RecordingEngine()
    submission = DecisionChannelRequest(
        idempotency_key="bounded-receipt-1",
        question="Should I take a break?",
        source="future-ios-app",
    )
    original = DecisionChannelAdapter(
        service=HealthMesDecisionService(
            settings=settings,
            engine_provider=lambda: original_engine,
            session_factory_provider=lambda: service_session_factory,
            clock=clock,
        )
    )
    first_result = await original.ask_wellness(submission)
    assert len(original_engine.requests) == 1

    clock.advance(timedelta(days=29))
    restarted_engine = RecordingEngine()
    restarted = DecisionChannelAdapter(
        service=HealthMesDecisionService(
            settings=settings,
            engine_provider=lambda: restarted_engine,
            session_factory_provider=lambda: service_session_factory,
            clock=clock,
        )
    )
    replay = await restarted.ask_wellness(submission)
    assert replay == first_result
    assert restarted_engine.requests == []

    clock.advance(timedelta(days=2))
    refreshed = await restarted.ask_wellness(submission)
    assert refreshed.status is DecisionStatus.COMPLETED
    assert len(restarted_engine.requests) == 1


@pytest.mark.asyncio
async def test_channel_idempotency_key_rejects_different_input(
    settings,
    service_session_factory,
) -> None:
    engine = RecordingEngine()
    service = HealthMesDecisionService(
        settings=settings,
        engine_provider=lambda: engine,
        session_factory_provider=lambda: service_session_factory,
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
    service_session_factory,
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
        session_factory_provider=lambda: service_session_factory,
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
        assert returned.request_id == engine.requests[-1].request_id

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
async def test_service_fails_closed_without_a_runtime(
    settings,
    service_session_factory,
) -> None:
    service = HealthMesDecisionService(
        settings=settings,
        engine_provider=lambda: None,
        session_factory_provider=lambda: service_session_factory,
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
    service_session_factory,
) -> None:
    engine = RecordingEngine()
    request_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    service = HealthMesDecisionService(
        settings=settings,
        engine_provider=lambda: engine,
        session_factory_provider=lambda: service_session_factory,
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
