from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

import healthmes.app as app_module
import healthmes.store.decision_receipts as decision_receipts_module
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
from healthmes.storage import update_retention_policy
from healthmes.store import Base, DecisionRequestReceipt, create_db_engine
from healthmes.store.decision_receipts import (
    DecisionReceiptClaimState,
    DecisionReceiptStore,
    purge_expired_decision_receipts,
)

NOW = datetime(2026, 8, 16, 9, tzinfo=UTC)


def _completed(
    request,
    *,
    answer: str = "Take a short break.",
) -> DecisionResult:
    return DecisionResult(
        request_id=request.request_id,
        turn_id=request.turn_id,
        status=DecisionStatus.COMPLETED,
        answer=answer,
        persistence_status=PersistenceStatus.NOT_REQUIRED,
        runtime=RuntimeMetadata(runtime="test"),
    )


class BlockingEngine:
    def __init__(
        self,
        *,
        answer: str = "Take a short break.",
    ) -> None:
        self.requests = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.answer = answer

    async def ask_wellness(self, request):
        self.requests.append(request)
        self.started.set()
        await self.release.wait()
        return _completed(request, answer=self.answer)


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
        return _completed(request)


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


@contextmanager
def _isolated_postgres_factory():
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
    try:
        yield factory
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(f'DROP SCHEMA "{schema}" CASCADE')
            )
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires HEALTHMES_TEST_POSTGRES_URL",
)
def test_postgres_receipt_maintenance_row_lock_wait_is_bounded(
    monkeypatch,
) -> None:
    with _isolated_postgres_factory() as factory:
        now = NOW
        with factory() as session:
            receipt = DecisionRequestReceipt(
                request_id=uuid.uuid4(),
                request_fingerprint="a" * 64,
                requested_at=now,
                state="completed",
                owner_token=None,
                lease_generation=1,
                lease_expires_at=None,
                result_payload={
                    "schema": "healthmes.decision-receipt.v1",
                    "result": {
                        "answer": "A bounded transient result.",
                        "persistence_status": "not_required",
                    },
                },
                result_expires_at=now + timedelta(days=1),
                retention_basis_at=now,
                expires_at=now + timedelta(days=30),
            )
            session.add(receipt)
            session.commit()
            receipt_id = receipt.id

        blocker = factory()
        try:
            blocker.execute(
                sa.select(DecisionRequestReceipt)
                .where(DecisionRequestReceipt.id == receipt_id)
                .with_for_update()
            ).scalar_one()
            monkeypatch.setattr(
                app_module,
                "get_session_factory",
                lambda: factory,
            )

            started = time.monotonic()
            with pytest.raises(
                TimeoutError,
                match="decision receipt maintenance",
            ):
                app_module._run_mandatory_decision_receipt_maintenance(
                    max_rows=1,
                    timeout_seconds=0.2,
                )
            elapsed = time.monotonic() - started
            assert 0.1 <= elapsed < 1
        finally:
            blocker.rollback()
            blocker.close()


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
        first_claim = store.claim(
            request_id=takeover_request_id,
            fingerprint=takeover_fingerprint,
            owner_token=first_owner,
            now=NOW,
        )
        takeover_claim = store.claim(
            request_id=takeover_request_id,
            fingerprint=takeover_fingerprint,
            owner_token=takeover_owner,
            now=NOW + timedelta(seconds=2),
        )
        assert first_claim.state is DecisionReceiptClaimState.ACQUIRED
        assert takeover_claim.state is DecisionReceiptClaimState.ACQUIRED
        assert first_claim.lease_generation is not None
        assert takeover_claim.lease_generation is not None
        canonical = {
            "schema": "healthmes.decision-receipt.v2",
            "kind": "transient_result",
            "result": {"winner": "takeover"},
        }
        store.complete(
            request_id=takeover_request_id,
            fingerprint=takeover_fingerprint,
            owner_token=takeover_owner,
            lease_generation=takeover_claim.lease_generation,
            result_payload=canonical,
            now=NOW + timedelta(seconds=2),
        )
        assert (
            store.complete(
                request_id=takeover_request_id,
                fingerprint=takeover_fingerprint,
                owner_token=first_owner,
                lease_generation=first_claim.lease_generation,
                result_payload={
                    "schema": "healthmes.decision-receipt.v2",
                    "kind": "transient_result",
                    "result": {"winner": "expired-owner"},
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
        future_claim = store.claim(
            request_id=future_request_id,
            fingerprint="c" * 64,
            owner_token=future_owner,
            now=NOW,
        )
        assert future_claim.lease_generation is not None
        store.complete(
            request_id=future_request_id,
            fingerprint="c" * 64,
            owner_token=future_owner,
            lease_generation=future_claim.lease_generation,
            result_payload={"schema": "test", "state": "future"},
            now=NOW,
        )
        expired_claim = store.claim(
            request_id=expired_request_id,
            fingerprint="d" * 64,
            owner_token=expired_owner,
            now=NOW - timedelta(days=30),
        )
        assert expired_claim.lease_generation is not None
        store.complete(
            request_id=expired_request_id,
            fingerprint="d" * 64,
            owner_token=expired_owner,
            lease_generation=expired_claim.lease_generation,
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


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires HEALTHMES_TEST_POSTGRES_URL",
)
@pytest.mark.asyncio
async def test_postgres_unknown_retry_reuses_first_requested_at(
    settings,
) -> None:
    with _isolated_postgres_factory() as factory:
        clock = MutableClock(NOW)
        runtime = UnknownThenCompletedEngine()
        adapter = DecisionChannelAdapter(
            service=HealthMesDecisionService(
                settings=settings,
                engine_provider=lambda: runtime,
                session_factory_provider=lambda: factory,
                clock=clock,
            )
        )
        submission = DecisionChannelRequest(
            idempotency_key="postgres-unknown-requested-at-1",
            question="Should I keep working?",
            source="future-ios-app",
        )

        unknown = await adapter.ask_wellness(submission)
        clock.advance(timedelta(hours=3))
        completed = await adapter.ask_wellness(submission)

        assert unknown.persistence_status is PersistenceStatus.UNKNOWN
        assert completed.status is DecisionStatus.COMPLETED
        assert [
            request.requested_at for request in runtime.requests
        ] == [NOW, NOW]
        with factory() as session:
            [receipt] = session.scalars(
                sa.select(DecisionRequestReceipt)
            ).all()
            assert receipt.requested_at == NOW


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires HEALTHMES_TEST_POSTGRES_URL",
)
def test_postgres_takeover_preserves_first_server_retention_basis() -> None:
    with _isolated_postgres_factory() as factory:
        with factory() as session:
            update_retention_policy(
                session,
                "decision",
                "1d",
                now=NOW,
            )
            session.commit()

        store = DecisionReceiptStore(
            session_factory=factory,
            lease_duration=timedelta(seconds=1),
            retention=timedelta(days=30),
        )
        request_id = uuid.uuid4()
        fingerprint = "f" * 64
        semantic_requested_at = NOW + timedelta(days=365)
        first = store.claim(
            request_id=request_id,
            fingerprint=fingerprint,
            owner_token=uuid.uuid4(),
            now=NOW,
            requested_at=semantic_requested_at,
        )
        takeover_owner = uuid.uuid4()
        takeover = store.claim(
            request_id=request_id,
            fingerprint=fingerprint,
            owner_token=takeover_owner,
            now=NOW + timedelta(seconds=2),
            requested_at=semantic_requested_at,
        )

        assert first.lease_generation == 1
        assert takeover.lease_generation == 2
        completion = store.complete(
            request_id=request_id,
            fingerprint=fingerprint,
            owner_token=takeover_owner,
            lease_generation=2,
            result_payload={"schema": "test", "winner": "takeover"},
            now=NOW + timedelta(seconds=2),
        )

        assert completion.expires_at == NOW + timedelta(minutes=15)
        with factory() as session:
            receipt = session.scalar(
                sa.select(DecisionRequestReceipt).where(
                    DecisionRequestReceipt.request_id == request_id
                )
            )
            assert receipt is not None
            assert receipt.retention_basis_at == NOW
            assert receipt.requested_at == semantic_requested_at
            assert receipt.result_expires_at == (
                NOW + timedelta(minutes=15)
            )


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires HEALTHMES_TEST_POSTGRES_URL",
)
@pytest.mark.asyncio
async def test_postgres_stale_owner_waits_for_canonical_result(
    settings,
) -> None:
    with _isolated_postgres_factory() as factory:
        clock = MutableClock(NOW)
        store = DecisionReceiptStore(
            session_factory=factory,
            lease_duration=timedelta(seconds=1),
            retention=timedelta(days=30),
        )
        stale_engine = BlockingEngine(answer="Stale answer.")
        canonical_engine = BlockingEngine(answer="Canonical answer.")
        stale_service = HealthMesDecisionService(
            settings=settings,
            engine_provider=lambda: stale_engine,
            session_factory_provider=lambda: factory,
            clock=clock,
        )
        canonical_service = HealthMesDecisionService(
            settings=settings,
            engine_provider=lambda: canonical_engine,
            session_factory_provider=lambda: factory,
            clock=clock,
        )
        stale_service._receipt_store = store
        canonical_service._receipt_store = store
        stale = DecisionChannelAdapter(service=stale_service)
        canonical = DecisionChannelAdapter(service=canonical_service)
        submission = DecisionChannelRequest(
            idempotency_key="postgres-stale-owner-1",
            question="Should I take a break?",
            source="future-ios-app",
        )

        stale_call = asyncio.create_task(
            stale.ask_wellness(submission)
        )
        await asyncio.wait_for(stale_engine.started.wait(), timeout=2)
        clock.advance(timedelta(seconds=2))
        canonical_call = asyncio.create_task(
            canonical.ask_wellness(submission)
        )
        await asyncio.wait_for(
            canonical_engine.started.wait(),
            timeout=2,
        )

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


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires HEALTHMES_TEST_POSTGRES_URL",
)
@pytest.mark.asyncio
async def test_postgres_retention_shrink_serializes_with_receipt_completion(
    monkeypatch,
) -> None:
    with _isolated_postgres_factory() as factory:
        with factory() as session:
            update_retention_policy(
                session,
                "decision",
                "30d",
                now=NOW,
            )
            session.commit()

        store = DecisionReceiptStore(
            session_factory=factory,
            lease_duration=timedelta(days=3),
            retention=timedelta(days=30),
        )
        request_id = uuid.uuid4()
        owner_token = uuid.uuid4()
        fingerprint = "a" * 64
        claim = store.claim(
            request_id=request_id,
            fingerprint=fingerprint,
            owner_token=owner_token,
            now=NOW - timedelta(days=2),
            requested_at=NOW + timedelta(days=365),
        )
        assert claim.lease_generation is not None

        policy_read = threading.Event()
        finish_completion = threading.Event()
        original_result_expiry = decision_receipts_module._result_expiry

        def blocking_result_expiry(
            session,
            *,
            receipt,
            result_payload=None,
        ):
            deadline = original_result_expiry(
                session,
                receipt=receipt,
                result_payload=result_payload,
            )
            policy_read.set()
            if not finish_completion.wait(timeout=5):
                raise TimeoutError(
                    "test did not release retention calculation"
                )
            return deadline

        monkeypatch.setattr(
            decision_receipts_module,
            "_result_expiry",
            blocking_result_expiry,
        )
        completion = asyncio.create_task(
            asyncio.to_thread(
                store.complete,
                request_id=request_id,
                fingerprint=fingerprint,
                owner_token=owner_token,
                lease_generation=claim.lease_generation,
                result_payload={
                    "schema": "test",
                    "answer": "sensitive",
                },
                now=NOW,
            )
        )
        assert await asyncio.to_thread(policy_read.wait, 2)

        def shorten_retention() -> None:
            with factory() as session:
                update_retention_policy(
                    session,
                    "decision",
                    "1d",
                    now=NOW,
                )
                session.commit()

        retention_update = asyncio.create_task(
            asyncio.to_thread(shorten_retention)
        )
        await asyncio.sleep(0.05)
        assert not retention_update.done()
        finish_completion.set()
        await asyncio.gather(completion, retention_update)

        with factory() as session:
            receipt = session.scalar(
                sa.select(DecisionRequestReceipt).where(
                    DecisionRequestReceipt.request_id == request_id
                )
            )
            assert receipt is not None
            assert receipt.state == "tombstone"
            assert receipt.result_payload is None
            assert receipt.result_expires_at is None
