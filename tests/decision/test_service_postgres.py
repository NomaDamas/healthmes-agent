from __future__ import annotations

import asyncio
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from healthmes.decision import (
    DecisionChannelAdapter,
    DecisionChannelRequest,
    DecisionIdempotencyConflictError,
    DecisionResult,
    DecisionStatus,
    HealthMesDecisionService,
    PersistenceStatus,
    RuntimeMetadata,
)
from healthmes.store import Base, DecisionRequestReceipt, create_db_engine
from healthmes.store.decision_receipts import (
    DecisionReceiptClaimState,
    DecisionReceiptStore,
    purge_expired_decision_receipts,
)

NOW = datetime(2026, 8, 16, 9, tzinfo=UTC)


def _completed(request) -> DecisionResult:
    return DecisionResult(
        request_id=request.request_id,
        turn_id=request.turn_id,
        status=DecisionStatus.COMPLETED,
        answer="Take a short break.",
        persistence_status=PersistenceStatus.NOT_REQUIRED,
        runtime=RuntimeMetadata(runtime="test"),
    )


class BlockingEngine:
    def __init__(self) -> None:
        self.requests = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def ask_wellness(self, request):
        self.requests.append(request)
        self.started.set()
        await self.release.wait()
        return _completed(request)


class RecordingEngine:
    def __init__(self) -> None:
        self.requests = []

    async def ask_wellness(self, request):
        self.requests.append(request)
        return _completed(request)


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
        return _completed(request)


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
        return _completed(request)


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


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires HEALTHMES_TEST_POSTGRES_URL",
)
@pytest.mark.asyncio
async def test_postgres_concurrency_restart_and_failed_retry(
    settings,
) -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_decision_receipt_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
    )

    def adapter_for(runtime):
        return DecisionChannelAdapter(
            service=HealthMesDecisionService(
                settings=settings,
                engine_provider=lambda: runtime,
                session_factory_provider=lambda: factory,
                clock=lambda: NOW,
            )
        )

    owner_engine = BlockingEngine()
    waiting_engine = RecordingEngine()
    wait_reached = threading.Event()
    owner = adapter_for(owner_engine)
    waiting_service = HealthMesDecisionService(
        settings=settings,
        engine_provider=lambda: waiting_engine,
        session_factory_provider=lambda: factory,
        clock=lambda: NOW,
    )
    waiting_service._receipt_store = ClaimObservingReceiptStore(
        session_factory=factory,
        lease_duration=timedelta(minutes=5),
        retention=timedelta(days=30),
        wait_reached=wait_reached,
    )
    waiter = DecisionChannelAdapter(service=waiting_service)
    submission = DecisionChannelRequest(
        idempotency_key="postgres-concurrent-1",
        question="Should I take a break?",
        source="future-ios-app",
    )

    try:
        first = asyncio.create_task(owner.ask_wellness(submission))
        await asyncio.wait_for(owner_engine.started.wait(), timeout=2)
        delayed = asyncio.create_task(waiter.ask_wellness(submission))
        assert await asyncio.to_thread(wait_reached.wait, 2)
        assert waiting_engine.requests == []

        owner_engine.release.set()
        first_result, delayed_result = await asyncio.gather(
            first,
            delayed,
        )
        assert delayed_result == first_result
        assert len(owner_engine.requests) == 1
        assert waiting_engine.requests == []

        conflicting_engine = RecordingEngine()
        with pytest.raises(DecisionIdempotencyConflictError):
            await adapter_for(conflicting_engine).ask_wellness(
                submission.model_copy(
                    update={"question": "Should I drink coffee?"}
                )
            )
        assert conflicting_engine.requests == []

        restarted_engine = RecordingEngine()
        restarted_result = await adapter_for(restarted_engine).ask_wellness(submission)
        assert restarted_result == first_result
        assert restarted_engine.requests == []

        retry_engine = FailedThenCompletedEngine()
        retry_adapter = adapter_for(retry_engine)
        retry_submission = DecisionChannelRequest(
            idempotency_key="postgres-retry-1",
            question="Should I keep working?",
            source="future-ios-app",
        )
        failed = await retry_adapter.ask_wellness(retry_submission)
        completed = await retry_adapter.ask_wellness(retry_submission)
        assert failed.status is DecisionStatus.FAILED
        assert completed.status is DecisionStatus.COMPLETED
        assert len(retry_engine.requests) == 2

        blocked_engine = BlockedThenCompletedEngine()
        blocked_adapter = adapter_for(blocked_engine)
        blocked_submission = DecisionChannelRequest(
            idempotency_key="postgres-blocked-retry-1",
            question="Can the runtime recover?",
            source="future-ios-app",
        )
        blocked = await blocked_adapter.ask_wellness(
            blocked_submission
        )
        recovered = await blocked_adapter.ask_wellness(
            blocked_submission
        )
        assert blocked.status is DecisionStatus.BLOCKED
        assert blocked.limitations == ["hermes_responses_timeout"]
        assert recovered.status is DecisionStatus.COMPLETED
        assert len(blocked_engine.requests) == 2

        store = DecisionReceiptStore(
            session_factory=factory,
            lease_duration=timedelta(seconds=1),
            retention=timedelta(days=30),
        )
        takeover_request_id = uuid.uuid4()
        takeover_fingerprint = "a" * 64
        first_owner = uuid.uuid4()
        takeover_owner = uuid.uuid4()
        assert (
            store.claim(
                request_id=takeover_request_id,
                fingerprint=takeover_fingerprint,
                owner_token=first_owner,
                now=NOW,
            ).state
            is DecisionReceiptClaimState.ACQUIRED
        )
        assert (
            store.claim(
                request_id=takeover_request_id,
                fingerprint=takeover_fingerprint,
                owner_token=takeover_owner,
                now=NOW + timedelta(seconds=2),
            ).state
            is DecisionReceiptClaimState.ACQUIRED
        )
        canonical = {"schema": "test", "winner": "takeover"}
        store.complete(
            request_id=takeover_request_id,
            fingerprint=takeover_fingerprint,
            owner_token=takeover_owner,
            result_payload=canonical,
            now=NOW + timedelta(seconds=2),
        )
        assert (
            store.complete(
                request_id=takeover_request_id,
                fingerprint=takeover_fingerprint,
                owner_token=first_owner,
                result_payload={
                    "schema": "test",
                    "winner": "expired-owner",
                },
                now=NOW + timedelta(seconds=3),
            ).result_payload
            == canonical
        )

        with factory() as session:
            receipts = session.scalars(
                sa.select(DecisionRequestReceipt).order_by(DecisionRequestReceipt.request_id)
            ).all()
            assert len(receipts) == 4
            assert all(row.state == "completed" for row in receipts)

        future_request_id = uuid.uuid4()
        future_owner = uuid.uuid4()
        expired_request_id = uuid.uuid4()
        expired_owner = uuid.uuid4()
        store.claim(
            request_id=future_request_id,
            fingerprint="c" * 64,
            owner_token=future_owner,
            now=NOW,
        )
        store.complete(
            request_id=future_request_id,
            fingerprint="c" * 64,
            owner_token=future_owner,
            result_payload={"schema": "test", "state": "future"},
            now=NOW,
        )
        store.claim(
            request_id=expired_request_id,
            fingerprint="d" * 64,
            owner_token=expired_owner,
            now=NOW - timedelta(days=30),
        )
        store.complete(
            request_id=expired_request_id,
            fingerprint="d" * 64,
            owner_token=expired_owner,
            result_payload={"schema": "test", "state": "expired"},
            now=NOW - timedelta(days=30),
        )

        with factory() as session:
            assert purge_expired_decision_receipts(
                session,
                now=NOW,
                dry_run=True,
            ) == 1
            session.commit()
        with factory() as session:
            assert session.scalar(
                sa.select(DecisionRequestReceipt).where(
                    DecisionRequestReceipt.request_id
                    == expired_request_id
                )
            ) is not None
            assert purge_expired_decision_receipts(
                session,
                now=NOW,
            ) == 1
            session.commit()
        with factory() as session:
            assert session.scalar(
                sa.select(DecisionRequestReceipt).where(
                    DecisionRequestReceipt.request_id
                    == expired_request_id
                )
            ) is None
            assert session.scalar(
                sa.select(DecisionRequestReceipt).where(
                    DecisionRequestReceipt.request_id
                    == future_request_id
                )
            ) is not None
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()
