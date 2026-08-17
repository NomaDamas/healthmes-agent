from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

import healthmes.decision.finalizer as finalizer_module
import healthmes.storage.service as storage_service
from healthmes.activity.locking import (
    lock_activity_write_plane,
    postgres_activity_write_plane_guard,
)
from healthmes.calendars import creds
from healthmes.calendars.state import (
    InMemorySyncHealthStore,
    SyncCoverageKind,
)
from healthmes.config import Settings
from healthmes.decision import (
    CalendarContextProvider,
    ContextAccessLayer,
    ContextAccessPolicy,
    ContextCapability,
    ContextCoverage,
    ContextFreshness,
    ContextProviderMetadata,
    ContextProviderRegistry,
    ContextQuery,
    ContextResult,
    ContextStatus,
    CoverageStatus,
    DecisionAgentRun,
    DecisionCaller,
    DecisionDraft,
    DecisionFinalizer,
    DecisionPersistenceIntent,
    DecisionRequest,
    DecisionStatus,
    DomainAccessGrant,
    ExecutionScope,
    FreshnessStatus,
    PersistenceStatus,
    RuntimeMetadata,
    SourceRef,
    ToolCallRecord,
    ToolCallStatus,
    decision_result_from_record,
)
from healthmes.engine.decision_dispatch import DecisionDispatchResult
from healthmes.engine.triggers import (
    HealthSignals,
    TriggerEvaluator,
    TriggerFire,
)
from healthmes.storage import update_retention_policy
from healthmes.store import (
    Base,
    CalendarEventMirror,
    CalendarSource,
    DecisionRecord,
    TriggerEvent,
    WellnessEvent,
    create_db_engine,
)

_POSTGRES_URL = os.environ.get("HEALTHMES_TEST_POSTGRES_URL")
_POSTGRES_SKIP_REASON = (
    "requires a disposable PostgreSQL URL in "
    "HEALTHMES_TEST_POSTGRES_URL"
)
_ACTIVITY_WRITE_PLANE_KEY = "healthmes:activity:write-plane:v1"

pytestmark = pytest.mark.skipif(
    not _POSTGRES_URL,
    reason=_POSTGRES_SKIP_REASON,
)

NOW = datetime(2026, 8, 12, 6, tzinfo=UTC)
WINDOW_START = NOW - timedelta(hours=2)
WINDOW_END = NOW - timedelta(hours=1)
FINGERPRINT_KEY = b"test-decision-fingerprint-key-32-bytes"


@dataclass(frozen=True, slots=True)
class _PostgresStore:
    engine: Engine
    factory: sessionmaker[Session]
    schema: str


class _StoredNutritionProvider:
    metadata = ContextProviderMetadata(
        provider_id="nutrition",
        domain="nutrition",
        description="Stored nutrition context for PostgreSQL finalizer tests.",
        capabilities=(
            ContextCapability(
                capability="nutrition.summary",
                description="Return one retained nutrition observation.",
                granularities=("summary",),
                query_fields=("start", "end", "timezone"),
                output_fields=("caffeine_mg",),
                max_lookback_days=7,
                sensitivity="nutrition",
                freshness_expectation="Stored event timestamp.",
            ),
        ),
    )

    def __init__(self, source_ref: SourceRef) -> None:
        self._source_ref = source_ref

    async def query(
        self,
        session: Session,
        query: ContextQuery,
        *,
        now: datetime,
    ) -> ContextResult:
        del session
        return ContextResult(
            query_id=query.query_id,
            provider_id=query.provider_id,
            capability=query.capability,
            status=ContextStatus.OK,
            payload={"caffeine_mg": 80},
            source_refs=[self._source_ref],
            freshness=ContextFreshness(
                status=FreshnessStatus.CURRENT,
                as_of=now,
                age_seconds=0,
            ),
            coverage=ContextCoverage(
                status=CoverageStatus.COMPLETE,
                ratio=1,
            ),
        )


@contextmanager
def _postgres_store(
    *,
    pool_size: int = 4,
    pool_timeout: float = 5,
) -> Iterator[_PostgresStore]:
    assert _POSTGRES_URL is not None
    admin_engine = create_db_engine(_POSTGRES_URL)
    schema = f"hm_decision_{uuid.uuid4().hex}"
    quoted_schema = admin_engine.dialect.identifier_preparer.quote(schema)
    schema_created = False
    engine: Engine | None = None
    try:
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f"CREATE SCHEMA {quoted_schema}"))
        schema_created = True
        engine = create_db_engine(
            _POSTGRES_URL,
            connect_args={"options": f"-csearch_path={schema}"},
            pool_size=pool_size,
            max_overflow=0,
            pool_timeout=pool_timeout,
        )
        Base.metadata.create_all(engine)
        with engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT current_schema()")) == schema
        yield _PostgresStore(
            engine=engine,
            factory=sessionmaker(
                bind=engine,
                autocommit=False,
                autoflush=False,
                expire_on_commit=False,
            ),
            schema=schema,
        )
    finally:
        if engine is not None:
            engine.dispose()
        if schema_created:
            with admin_engine.begin() as connection:
                connection.execute(
                    sa.text(f"DROP SCHEMA {quoted_schema} CASCADE")
                )
        admin_engine.dispose()


def _request(
    *,
    request_id: uuid.UUID | None = None,
    turn_id: uuid.UUID | None = None,
) -> DecisionRequest:
    return DecisionRequest(
        request_id=request_id or uuid.uuid4(),
        turn_id=turn_id or uuid.uuid4(),
        question="Should I take a break before having more caffeine?",
        requested_at=NOW,
        timezone="UTC",
        caller=DecisionCaller(
            principal_id="owner",
            authenticated=True,
            execution_scope=ExecutionScope.LOCAL,
            channel="proactive:postgres-test",
        ),
    )


def _policy() -> ContextAccessPolicy:
    return ContextAccessPolicy(
        owner_principal_id="owner",
        grants=(DomainAccessGrant(domain="nutrition"),),
    )


def _query() -> ContextQuery:
    return ContextQuery(
        provider_id="nutrition",
        capability="nutrition.summary",
        start=WINDOW_START,
        end=NOW,
        timezone="UTC",
    )


def _event(session: Session) -> WellnessEvent:
    event = WellnessEvent(
        event_type="nutrition.observation.v1",
        schema_version=1,
        observed_at=WINDOW_START,
        recorded_at=WINDOW_START + timedelta(minutes=1),
        timezone="UTC",
        source_provider="healthmes-intake",
        source_device=None,
        source_record_id=uuid.uuid4().hex,
        capture_method="text",
        quality_flags={},
        confidence=0.9,
        coverage=1.0,
        sensitivity="nutrition",
        consent_scope="personal",
        expires_at=None,
        payload={
            "window": {
                "start": WINDOW_START.isoformat(),
                "end": WINDOW_END.isoformat(),
            },
            "caffeine_mg": 80,
        },
        derived_from=None,
    )
    session.add(event)
    session.commit()
    return event


def _unattested_source_ref(event: WellnessEvent) -> SourceRef:
    return SourceRef(
        domain="nutrition",
        resource_type=event.event_type,
        record_id=str(event.id),
        source_provider=event.source_provider,
        observed_start=event.observed_at,
        observed_end=WINDOW_END,
        schema_version=event.schema_version,
        derived_by="nutrition.summary.v1",
        freshness=FreshnessStatus.CURRENT,
        coverage=event.coverage,
        sensitivity=event.sensitivity,
    )


def _attested_context(
    factory: sessionmaker[Session],
    request: DecisionRequest,
    event: WellnessEvent,
) -> tuple[ContextQuery, ContextResult, tuple]:
    source_ref = _unattested_source_ref(event)
    registry = ContextProviderRegistry(
        (_StoredNutritionProvider(source_ref),)
    )
    access_layer = ContextAccessLayer(registry, clock=lambda: NOW)
    turn = access_layer.start_turn(request, policy=_policy())
    query = _query()
    with factory() as session:
        result = asyncio.run(turn.query(session, query))
        session.rollback()
    assert result.status is ContextStatus.OK
    assert len(result.source_refs) == 1
    assert result.source_refs[0].content_digest is not None
    return query, result, turn.trace


def _run(
    request: DecisionRequest,
    query: ContextQuery,
    context: ContextResult,
    access_trace: tuple,
) -> DecisionAgentRun:
    source_refs = tuple(context.source_refs)
    return DecisionAgentRun(
        request_id=request.request_id,
        turn_id=request.turn_id,
        draft=DecisionDraft(
            status=DecisionStatus.COMPLETED,
            answer="Take a short break before choosing more caffeine.",
            proposed_action=True,
            persistence_intent=DecisionPersistenceIntent.ACTION,
            used_source_ref_ids=[
                source_ref.reference_id for source_ref in source_refs
            ],
            confidence=0.8,
            uncertainty="Only retained nutrition context was considered.",
        ),
        source_refs=source_refs,
        runtime=RuntimeMetadata(
            runtime="scripted",
            model="decision-postgres-test-v1",
            input_tokens=12,
            output_tokens=8,
        ),
        steps_used=1,
        tool_trace=(
            ToolCallRecord(
                query=query,
                status=ToolCallStatus.COMPLETED,
                started_at=NOW,
                finished_at=NOW,
                result=context,
            ),
        ),
        access_trace=access_trace,
        system_policy_version="healthmes-decision-policy.postgres-test",
        started_at=NOW,
        finished_at=NOW,
    )


def _finalizer(
    factory: sessionmaker[Session],
    source_ref: SourceRef,
    *,
    policy_resolver=None,
    timeout_seconds: float = 5,
) -> DecisionFinalizer:
    registry = ContextProviderRegistry(
        (_StoredNutritionProvider(source_ref),)
    )
    return DecisionFinalizer(
        access_layer=ContextAccessLayer(
            registry,
            clock=lambda: NOW,
        ),
        session_factory=factory,
        policy_resolver=(
            policy_resolver or (lambda _request: _policy())
        ),
        fingerprint_key=FINGERPRINT_KEY,
        timeout_seconds=timeout_seconds,
        clock=lambda: NOW,
    )


def _decision_fixture(
    store: _PostgresStore,
) -> tuple[
    DecisionRequest,
    DecisionAgentRun,
    DecisionFinalizer,
    uuid.UUID,
]:
    with store.factory() as session:
        event = _event(session)
        event_id = event.id
    request = _request()
    query, context, access_trace = _attested_context(
        store.factory,
        request,
        event,
    )
    run = _run(request, query, context, access_trace)
    return (
        request,
        run,
        _finalizer(store.factory, context.source_refs[0]),
        event_id,
    )


def test_concurrent_same_request_finalization_returns_one_record_id(
    monkeypatch,
) -> None:
    with _postgres_store(pool_size=2) as store:
        request, run, finalizer, _event_id = _decision_fixture(store)
        start = threading.Barrier(2, timeout=5)
        monkeypatch.setattr(
            "healthmes.decision.finalizer.activity_write_lock",
            lambda **_kwargs: nullcontext(),
        )

        def finalize_once():
            start.wait()
            return finalizer.finalize(request, run)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(finalize_once) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]

        assert {
            result.persistence_status for result in results
        } == {PersistenceStatus.PERSISTED}
        record_ids = {
            result.decision_record_id for result in results
        }
        assert len(record_ids) == 1
        assert None not in record_ids
        with store.factory() as session:
            assert session.scalar(
                sa.select(sa.func.count()).select_from(DecisionRecord)
            ) == 1


def test_finalization_and_decision_retention_share_postgres_fence(
    monkeypatch,
) -> None:
    with _postgres_store(pool_size=3) as store:
        request, run, finalizer, _event_id = _decision_fixture(store)
        finalizer_entered = threading.Event()
        release_finalizer = threading.Event()
        updater_started = threading.Event()
        updater_pid: list[int] = []
        results = []
        failures: list[BaseException] = []
        original_apply = finalizer_module.apply_decision_retention

        def paused_apply(session, row, *, basis_at):
            retained = original_apply(
                session,
                row,
                basis_at=basis_at,
            )
            finalizer_entered.set()
            assert release_finalizer.wait(timeout=10)
            return retained

        monkeypatch.setattr(
            finalizer_module,
            "apply_decision_retention",
            paused_apply,
        )
        monkeypatch.setattr(
            finalizer_module,
            "activity_write_lock",
            lambda **_kwargs: nullcontext(),
        )

        def finalize() -> None:
            try:
                results.append(finalizer.finalize(request, run))
            except BaseException as exc:
                failures.append(exc)

        def shrink_retention() -> None:
            with store.factory() as session:
                updater_pid.append(
                    int(
                        session.scalar(
                            sa.text("SELECT pg_backend_pid()")
                        )
                    )
                )
                updater_started.set()
                try:
                    update_retention_policy(
                        session,
                        "decision",
                        "1d",
                        now=NOW + timedelta(days=1),
                    )
                    session.commit()
                except BaseException as exc:
                    session.rollback()
                    failures.append(exc)

        finalizer_worker = threading.Thread(target=finalize)
        retention_worker = threading.Thread(target=shrink_retention)
        try:
            finalizer_worker.start()
            assert finalizer_entered.wait(timeout=10)
            retention_worker.start()
            assert updater_started.wait(timeout=5)

            deadline = time.monotonic() + 5
            waiting_for_advisory_lock = False
            while time.monotonic() < deadline:
                with store.factory() as observer:
                    wait_event = observer.execute(
                        sa.text(
                            "SELECT wait_event_type, wait_event "
                            "FROM pg_stat_activity WHERE pid = :pid"
                        ),
                        {"pid": updater_pid[0]},
                    ).one_or_none()
                if wait_event is not None and tuple(wait_event) == (
                    "Lock",
                    "advisory",
                ):
                    waiting_for_advisory_lock = True
                    break
                time.sleep(0.05)

            assert waiting_for_advisory_lock
        finally:
            release_finalizer.set()
            finalizer_worker.join(timeout=10)
            retention_worker.join(timeout=10)

        assert not finalizer_worker.is_alive()
        assert not retention_worker.is_alive()
        assert failures == []
        assert len(results) == 1
        assert (
            results[0].persistence_status
            is PersistenceStatus.PERSISTED
        )
        with store.factory() as session:
            assert session.scalar(
                sa.select(sa.func.count()).select_from(DecisionRecord)
            ) == 0


def test_dispatch_finalizer_and_retention_do_not_form_lock_cycle(
    monkeypatch,
) -> None:
    with _postgres_store(pool_size=5) as store:
        with store.factory() as session:
            update_retention_policy(
                session,
                "decision",
                "30d",
                now=NOW,
            )
            session.commit()
        request, run, finalizer, _event_id = _decision_fixture(store)
        retention_holds_write_plane = threading.Event()
        release_retention = threading.Event()
        finalizer_waiting_for_write_plane = threading.Event()
        retention_done = threading.Event()
        dispatch_results = []
        finalized_results = []
        failures: list[BaseException] = []
        original_trigger_lock = (
            storage_service.lock_trigger_events_for_retention
        )
        original_guard = (
            finalizer_module.postgres_activity_write_plane_guard
        )

        def pause_before_trigger_locks(session: Session) -> None:
            retention_holds_write_plane.set()
            if not release_retention.wait(timeout=10):
                raise TimeoutError("retention lock release was not signalled")
            original_trigger_lock(session)

        @contextmanager
        def observe_finalizer_guard(
            bind,
            *,
            timeout_seconds: float,
        ):
            finalizer_waiting_for_write_plane.set()
            with original_guard(
                bind,
                timeout_seconds=timeout_seconds,
            ) as connection:
                yield connection

        monkeypatch.setattr(
            storage_service,
            "lock_trigger_events_for_retention",
            pause_before_trigger_locks,
        )
        monkeypatch.setattr(
            finalizer_module,
            "postgres_activity_write_plane_guard",
            observe_finalizer_guard,
        )

        class EmptyHealthReader:
            def read(self, now: datetime) -> HealthSignals:
                del now
                return HealthSignals()

        class FinalizingSender:
            requires_reasoning = True

            def send(
                self,
                fire: TriggerFire,
                *,
                fired_at: datetime,
                trigger_event_id: uuid.UUID,
            ) -> DecisionDispatchResult:
                del fire, fired_at, trigger_event_id
                result = finalizer.finalize(request, run)
                finalized_results.append(result)
                return DecisionDispatchResult(
                    ok=False,
                    status_code=204,
                    ready_for_native=True,
                    channel="app_poll",
                    message=result.answer,
                    decision_record_id=result.decision_record_id,
                    decision_request_id=result.request_id,
                    decision_turn_id=result.turn_id,
                    proposed_action=result.proposed_action,
                )

        assert _POSTGRES_URL is not None
        evaluator = TriggerEvaluator(
            Settings(
                database_url=_POSTGRES_URL,
                native_alert_delivery=True,
                scheduler_enabled=False,
                _env_file=None,
            ),
            session_factory=store.factory,
            health_reader=EmptyHealthReader(),
            alert_sender=FinalizingSender(),
            rules=(),
            now_provider=lambda: NOW,
        )

        def shrink_retention() -> None:
            try:
                with store.factory() as session:
                    update_retention_policy(
                        session,
                        "decision",
                        "14d",
                        now=NOW,
                    )
                    session.commit()
            except BaseException as exc:
                failures.append(exc)
            finally:
                retention_done.set()

        def dispatch() -> None:
            try:
                dispatch_results.append(
                    evaluator.dispatch_fire(
                        TriggerFire(
                            rule_id="dispatch-finalizer-retention",
                            dedup_key=(
                                "dispatch-finalizer-retention:1"
                            ),
                            summary="A proactive answer is ready.",
                            proposal="Surface the retained answer.",
                            evidence={},
                        ),
                        fired_at=NOW,
                    )
                )
            except BaseException as exc:
                failures.append(exc)

        retention_worker = threading.Thread(
            target=shrink_retention,
            name="retention-write-plane-owner",
        )
        dispatch_worker = threading.Thread(
            target=dispatch,
            name="dispatch-finalizer",
        )
        try:
            retention_worker.start()
            assert retention_holds_write_plane.wait(timeout=5)
            dispatch_worker.start()
            assert finalizer_waiting_for_write_plane.wait(timeout=5)

            release_retention.set()
            retention_worker.join(timeout=10)
            dispatch_worker.join(timeout=10)

            assert not retention_worker.is_alive()
            assert not dispatch_worker.is_alive()
            assert retention_done.is_set()
            assert failures == []
            assert len(finalized_results) == 1
            assert (
                finalized_results[0].persistence_status
                is PersistenceStatus.PERSISTED
            )
            assert len(dispatch_results) == 1
            assert dispatch_results[0].status == "available"
            with store.factory() as session:
                [trigger] = session.scalars(
                    sa.select(TriggerEvent)
                ).all()
                [record] = session.scalars(
                    sa.select(DecisionRecord)
                ).all()
                assert trigger.payload["message"] == (
                    finalized_results[0].answer
                )
                assert trigger.payload["push"]["state"] == "app_available"
                assert trigger.dispatch_owner_token is None
                assert trigger.dispatch_lease_expires_at is None
                assert record.trigger_event_id == trigger.id
        finally:
            release_retention.set()
            retention_worker.join(timeout=5)
            dispatch_worker.join(timeout=5)


@pytest.mark.parametrize("mutation", ("delete", "content"))
def test_stale_retry_after_source_change_fails_closed(
    mutation: str,
) -> None:
    with _postgres_store() as store:
        request, run, finalizer, event_id = _decision_fixture(store)
        first = finalizer.finalize(request, run)
        assert first.persistence_status is PersistenceStatus.PERSISTED

        with store.factory() as session:
            event = session.get(WellnessEvent, event_id)
            assert event is not None
            if mutation == "delete":
                session.delete(event)
            else:
                event.payload = {
                    **event.payload,
                    "caffeine_mg": 300,
                }
            session.commit()

        retry_request = request.model_copy(
            update={"turn_id": uuid.uuid4()}
        )
        retry_run = run.model_copy(
            update={
                "turn_id": retry_request.turn_id,
                "draft": run.draft,
            },
            deep=True,
        )
        retry = finalizer.finalize(retry_request, retry_run)

        assert retry.status is DecisionStatus.FAILED
        assert retry.proposed_action is False
        assert retry.persistence_status is PersistenceStatus.FAILED
        assert "decision_source_ref_revalidation_failed" in retry.limitations
        expected = (
            "source_ref_record_missing"
            if mutation == "delete"
            else "source_ref_content_changed"
        )
        assert expected in retry.limitations
        assert retry.tool_trace == []
        with store.factory() as session:
            assert session.scalar(
                sa.select(sa.func.count()).select_from(DecisionRecord)
            ) == 1


def test_postgres_activity_write_plane_guard_timeout_is_bounded() -> None:
    with _postgres_store(pool_size=2) as store:
        with store.factory() as blocker:
            lock_activity_write_plane(blocker)
            started = time.monotonic()
            with pytest.raises(
                TimeoutError,
                match="timed out waiting for the activity write plane",
            ):
                with postgres_activity_write_plane_guard(
                    store.engine,
                    timeout_seconds=0.2,
                    poll_seconds=0.02,
                ):
                    pytest.fail("guard unexpectedly acquired")
            elapsed = time.monotonic() - started
            assert 0.15 <= elapsed < 2
            blocker.rollback()

        with postgres_activity_write_plane_guard(
            store.engine,
            timeout_seconds=1,
            poll_seconds=0.02,
        ) as connection:
            assert connection is not None


def test_postgres_finalizer_advisory_lock_timeout_is_auditable() -> None:
    with _postgres_store(pool_size=2) as store:
        request, run, _finalizer_instance, _event_id = (
            _decision_fixture(store)
        )
        finalizer = _finalizer(
            store.factory,
            run.source_refs[0],
            timeout_seconds=0.2,
        )
        with store.factory() as blocker:
            lock_activity_write_plane(blocker)
            started = time.monotonic()
            result = finalizer.finalize(request, run)
            elapsed = time.monotonic() - started
            assert 0.15 <= elapsed < 2
            assert result.status is DecisionStatus.FAILED
            assert result.persistence_status is PersistenceStatus.FAILED
            assert result.limitations == [
                "decision_finalization_timeout"
            ]
            blocker.rollback()

        retry = _finalizer(
            store.factory,
            run.source_refs[0],
        ).finalize(request, run)
        assert retry.persistence_status is PersistenceStatus.PERSISTED


def test_postgres_finalizer_row_lock_timeout_is_auditable() -> None:
    with _postgres_store(pool_size=2) as store:
        request, run, _finalizer_instance, event_id = (
            _decision_fixture(store)
        )
        finalizer = _finalizer(
            store.factory,
            run.source_refs[0],
            timeout_seconds=0.2,
        )
        with store.factory() as blocker:
            blocker.scalar(
                sa.select(WellnessEvent)
                .where(WellnessEvent.id == event_id)
                .with_for_update()
            )
            started = time.monotonic()
            result = finalizer.finalize(request, run)
            elapsed = time.monotonic() - started
            assert 0.15 <= elapsed < 2
            assert result.status is DecisionStatus.FAILED
            assert result.persistence_status is PersistenceStatus.FAILED
            assert result.limitations == [
                "decision_finalization_timeout"
            ]
            blocker.rollback()

        retry = _finalizer(
            store.factory,
            run.source_refs[0],
        ).finalize(request, run)
        assert retry.persistence_status is PersistenceStatus.PERSISTED


def test_postgres_calendar_aggregate_rows_are_locked_during_finalization(
    ) -> None:
    with _postgres_store(pool_size=2) as store:
        with store.factory() as session:
            row = CalendarEventMirror(
                external_id="postgres-calendar-aggregate-lock",
                calendar_source=CalendarSource.GOOGLE,
                summary="Private meeting",
                start_at=NOW + timedelta(hours=1),
                end_at=NOW + timedelta(hours=2),
                is_all_day=False,
                created_at=NOW - timedelta(minutes=2),
                updated_at=NOW - timedelta(minutes=1),
            )
            session.add(row)
            session.commit()
            row_id = row.id

        policy = ContextAccessPolicy(
            owner_principal_id="owner",
            grants=(DomainAccessGrant(domain="calendar"),),
        )
        request = _request()
        query = ContextQuery(
            provider_id="calendar",
            capability="calendar.day-summary",
            parameters={"date": NOW.date().isoformat()},
            timezone="UTC",
        )
        access_layer = ContextAccessLayer(
            ContextProviderRegistry(
                (
                    CalendarContextProvider(
                        sources=(CalendarSource.GOOGLE,),
                    ),
                )
            ),
            clock=lambda: NOW,
        )
        turn = access_layer.start_turn(request, policy=policy)
        with store.factory() as session:
            context = asyncio.run(turn.query(session, query))
            session.rollback()
        assert len(context.source_refs) == 1
        assert context.source_refs[0].record_id.startswith("aggregate:v1:")
        run = _run(request, query, context, turn.trace)
        finalizer = DecisionFinalizer(
            access_layer=access_layer,
            session_factory=store.factory,
            policy_resolver=lambda _request: policy,
            fingerprint_key=FINGERPRINT_KEY,
            timeout_seconds=0.2,
            clock=lambda: NOW,
        )

        with store.factory() as blocker:
            blocker.scalar(
                sa.select(CalendarEventMirror)
                .where(CalendarEventMirror.id == row_id)
                .with_for_update()
            )
            started = time.monotonic()
            result = finalizer.finalize(request, run)
            elapsed = time.monotonic() - started
            assert 0.15 <= elapsed < 2
            assert result.status is DecisionStatus.FAILED
            assert result.persistence_status is PersistenceStatus.FAILED
            assert "decision_finalization_timeout" in result.limitations
            blocker.rollback()

        retry = finalizer.finalize(request, run)
        assert retry.persistence_status is PersistenceStatus.PERSISTED


def test_postgres_calendar_aggregate_blocks_phantom_insert_until_commit(
) -> None:
    with _postgres_store(pool_size=3) as store:
        policy = ContextAccessPolicy(
            owner_principal_id="owner",
            grants=(DomainAccessGrant(domain="calendar"),),
        )
        request = _request()
        query = ContextQuery(
            provider_id="calendar",
            capability="calendar.day-summary",
            parameters={"date": NOW.date().isoformat()},
            timezone="UTC",
        )
        access_layer = ContextAccessLayer(
            ContextProviderRegistry(
                (
                    CalendarContextProvider(
                        sources=(CalendarSource.GOOGLE,),
                    ),
                )
            ),
            clock=lambda: NOW,
            calendar_sources=(CalendarSource.GOOGLE,),
        )
        turn = access_layer.start_turn(request, policy=policy)
        with store.factory() as session:
            context = asyncio.run(turn.query(session, query))
            session.rollback()
        assert context.payload["event_count"] == 0
        assert len(context.source_refs) == 1
        run = _run(request, query, context, turn.trace)

        finalizer_flushed = threading.Event()
        release_finalizer = threading.Event()
        insert_attempted = threading.Event()
        insert_finished = threading.Event()
        errors: list[BaseException] = []
        results = []

        class BlockingFlushSession(Session):
            blocked = False

            def flush(self, objects=None) -> None:
                super().flush(objects)
                if (
                    not type(self).blocked
                    and any(
                        isinstance(item, DecisionRecord)
                        for item in self.identity_map.values()
                    )
                ):
                    type(self).blocked = True
                    finalizer_flushed.set()
                    if not release_finalizer.wait(timeout=5):
                        raise TimeoutError(
                            "test finalizer release was not signalled"
                        )

        finalizer_factory = sessionmaker(
            bind=store.engine,
            class_=BlockingFlushSession,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
        finalizer = DecisionFinalizer(
            access_layer=access_layer,
            session_factory=finalizer_factory,
            policy_resolver=lambda _request: policy,
            fingerprint_key=FINGERPRINT_KEY,
            timeout_seconds=5,
            clock=lambda: NOW,
        )

        def finalize() -> None:
            try:
                results.append(finalizer.finalize(request, run))
            except BaseException as exc:
                errors.append(exc)

        def insert_phantom() -> None:
            try:
                assert finalizer_flushed.wait(timeout=5)
                with store.factory() as session:
                    session.add(
                        CalendarEventMirror(
                            external_id="postgres-calendar-phantom",
                            calendar_source=CalendarSource.GOOGLE,
                            summary="Concurrent meeting",
                            start_at=NOW + timedelta(hours=1),
                            end_at=NOW + timedelta(hours=2),
                            is_all_day=False,
                            created_at=NOW,
                            updated_at=NOW,
                        )
                    )
                    insert_attempted.set()
                    session.commit()
                insert_finished.set()
            except BaseException as exc:
                errors.append(exc)

        finalizer_thread = threading.Thread(target=finalize)
        insert_thread = threading.Thread(target=insert_phantom)
        try:
            finalizer_thread.start()
            assert finalizer_flushed.wait(timeout=5)
            insert_thread.start()
            assert insert_attempted.wait(timeout=5)
            time.sleep(0.2)
            assert not insert_finished.is_set()

            release_finalizer.set()
            finalizer_thread.join(timeout=10)
            insert_thread.join(timeout=10)

            assert not finalizer_thread.is_alive()
            assert not insert_thread.is_alive()
            assert errors == []
            assert len(results) == 1
            assert (
                results[0].persistence_status
                is PersistenceStatus.PERSISTED
            )
            assert insert_finished.is_set()
        finally:
            release_finalizer.set()
            finalizer_thread.join(timeout=5)
            insert_thread.join(timeout=5)


def test_postgres_finalizer_statement_timeout_is_auditable() -> None:
    class SlowPolicyResolver:
        def __call__(self, _request):
            return _policy()

        def resolve_in_session(
            self,
            _request,
            session,
            *,
            lock,
        ):
            if lock:
                session.execute(sa.text("SELECT pg_sleep(1)"))
            return _policy()

    with _postgres_store(pool_size=2) as store:
        request, run, _finalizer_instance, _event_id = (
            _decision_fixture(store)
        )
        result = _finalizer(
            store.factory,
            run.source_refs[0],
            policy_resolver=SlowPolicyResolver(),
            timeout_seconds=0.2,
        ).finalize(request, run)

        assert result.status is DecisionStatus.FAILED
        assert result.persistence_status is PersistenceStatus.FAILED
        assert result.limitations == ["decision_finalization_timeout"]
        retry = _finalizer(
            store.factory,
            run.source_refs[0],
        ).finalize(request, run)
        assert retry.persistence_status is PersistenceStatus.PERSISTED


def test_postgres_calendar_visibility_change_after_flush_rolls_back(
    tmp_path,
) -> None:
    with _postgres_store(pool_size=2) as store:
        settings = Settings(
            database_url=str(_POSTGRES_URL),
            data_dir=tmp_path / "data",
            timezone="UTC",
            _env_file=None,
        )
        token_path = (
            settings.data_dir / "google" / "calendar_token.json"
        )
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(
            json.dumps(
                {
                    "type": "authorized_user",
                    "refresh_token": "postgres-calendar-refresh-token",
                    "client_id": "client-id",
                    "client_secret": "client-secret",
                }
            ),
            encoding="utf-8",
        )
        account_generation = creds.calendar_account_generation(
            settings,
            CalendarSource.GOOGLE,
        )
        assert account_generation is not None
        health = InMemorySyncHealthStore()
        health.record_success(
            CalendarSource.GOOGLE,
            NOW,
            event_count=1,
            coverage_kind=SyncCoverageKind.FULL_COLLECTION,
            account_generation=account_generation,
        )
        with store.factory() as session:
            row = CalendarEventMirror(
                external_id="postgres-calendar-finalizer",
                calendar_source=CalendarSource.GOOGLE,
                connection_generation=account_generation,
                summary="Private PostgreSQL meeting",
                start_at=NOW + timedelta(hours=1),
                end_at=NOW + timedelta(hours=2),
                is_all_day=False,
                created_at=NOW - timedelta(minutes=2),
                updated_at=NOW - timedelta(minutes=1),
            )
            session.add(row)
            session.commit()
            row_id = row.id

        policy = ContextAccessPolicy(
            owner_principal_id="owner",
            grants=(DomainAccessGrant(domain="calendar"),),
        )
        request = _request()
        query = ContextQuery(
            provider_id="calendar",
            capability="calendar.day-summary",
            parameters={"date": NOW.date().isoformat()},
            timezone="UTC",
        )
        provider = CalendarContextProvider(
            settings=settings,
            sync_health_store=health,
        )
        access_layer = ContextAccessLayer(
            ContextProviderRegistry((provider,)),
            clock=lambda: NOW,
            calendar_settings=settings,
            calendar_sync_health_store=health,
        )
        turn = access_layer.start_turn(request, policy=policy)
        with store.factory() as session:
            context = asyncio.run(turn.query(session, query))
            session.rollback()
        assert context.status in {
            ContextStatus.OK,
            ContextStatus.PARTIAL,
        }
        assert len(context.source_refs) == 1
        assert context.source_refs[0].record_id.startswith("aggregate:v1:")
        assert str(row_id) not in context.model_dump_json()
        run = _run(request, query, context, turn.trace)

        class SyncChangingSession(Session):
            changed = False

            def flush(self, objects=None) -> None:
                super().flush(objects)
                if (
                    not type(self).changed
                    and any(
                        isinstance(item, DecisionRecord)
                        for item in self.identity_map.values()
                    )
                ):
                    type(self).changed = True
                    health.record_success(
                        CalendarSource.GOOGLE,
                        NOW + timedelta(minutes=1),
                        event_count=0,
                        coverage_kind=(
                            SyncCoverageKind.FULL_COLLECTION
                        ),
                        account_generation="f" * 64,
                    )

        changing_factory = sessionmaker(
            bind=store.engine,
            class_=SyncChangingSession,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
        finalizer = DecisionFinalizer(
            access_layer=access_layer,
            session_factory=changing_factory,
            policy_resolver=lambda _request: policy,
            fingerprint_key=FINGERPRINT_KEY,
            clock=lambda: NOW,
        )

        result = finalizer.finalize(request, run)

        assert result.status is DecisionStatus.FAILED
        assert result.persistence_status is PersistenceStatus.FAILED
        assert "calendar_visibility_changed" in result.limitations
        with store.factory() as session:
            assert session.scalar(
                sa.select(sa.func.count()).select_from(DecisionRecord)
            ) == 0


def test_postgres_finalizer_rejects_calendar_row_expired_before_maintenance(
    ) -> None:
    with _postgres_store(pool_size=2) as store:
        with store.factory() as session:
            update_retention_policy(
                session,
                "calendar_mirror",
                "1d",
                now=NOW,
            )
            row = CalendarEventMirror(
                external_id="postgres-calendar-expired-before-maintenance",
                calendar_source=CalendarSource.GOOGLE,
                summary="Retained during context query",
                start_at=NOW - timedelta(hours=2),
                end_at=NOW - timedelta(hours=1),
                is_all_day=False,
                created_at=NOW - timedelta(hours=3),
                updated_at=NOW - timedelta(hours=1),
            )
            session.add(row)
            session.commit()
            row_id = row.id

        policy = ContextAccessPolicy(
            owner_principal_id="owner",
            grants=(DomainAccessGrant(domain="calendar"),),
        )
        request = _request()
        query = ContextQuery(
            provider_id="calendar",
            capability="calendar.day-summary",
            parameters={"date": NOW.date().isoformat()},
            timezone="UTC",
        )
        provider = CalendarContextProvider(
            sources=(CalendarSource.GOOGLE,),
        )
        access_layer = ContextAccessLayer(
            ContextProviderRegistry((provider,)),
            clock=lambda: NOW,
        )
        turn = access_layer.start_turn(request, policy=policy)
        with store.factory() as session:
            context = asyncio.run(turn.query(session, query))
            session.rollback()
        assert context.status in {
            ContextStatus.OK,
            ContextStatus.PARTIAL,
        }
        assert len(context.source_refs) == 1
        assert context.source_refs[0].record_id.startswith("aggregate:v1:")
        assert str(row_id) not in context.model_dump_json()
        run = _run(request, query, context, turn.trace)
        finalizer = DecisionFinalizer(
            access_layer=access_layer,
            session_factory=store.factory,
            policy_resolver=lambda _request: policy,
            fingerprint_key=FINGERPRINT_KEY,
            clock=lambda: NOW + timedelta(days=2),
        )

        result = finalizer.finalize(request, run)

        assert result.status is DecisionStatus.FAILED
        assert result.persistence_status is PersistenceStatus.FAILED
        assert "source_ref_content_changed" in result.limitations
        with store.factory() as session:
            assert session.get(CalendarEventMirror, row_id) is not None
            assert session.scalar(
                sa.select(sa.func.count()).select_from(DecisionRecord)
            ) == 0


def test_initial_guard_commit_failure_releases_advisory_lock(
    monkeypatch,
) -> None:
    with _postgres_store(pool_size=1) as store:
        original_commit = sa.engine.Connection.commit
        commit_failed = False

        def fail_initial_commit(connection):
            nonlocal commit_failed
            if not commit_failed:
                commit_failed = True
                raise RuntimeError("injected initial guard commit failure")
            return original_commit(connection)

        monkeypatch.setattr(
            sa.engine.Connection,
            "commit",
            fail_initial_commit,
        )
        with pytest.raises(
            RuntimeError,
            match="injected initial guard commit failure",
        ):
            with postgres_activity_write_plane_guard(
                store.engine,
                timeout_seconds=1,
                poll_seconds=0.02,
            ):
                pytest.fail("guard unexpectedly yielded")

        assert commit_failed is True
        with postgres_activity_write_plane_guard(
            store.engine,
            timeout_seconds=1,
            poll_seconds=0.02,
        ) as connection:
            assert connection is not None


def test_guard_result_failure_after_acquisition_releases_advisory_lock(
    monkeypatch,
) -> None:
    with _postgres_store(pool_size=1) as store:
        original_scalar = sa.engine.Connection.scalar
        result_failed = False

        def fail_after_acquisition(
            connection,
            statement,
            *args,
            **kwargs,
        ):
            nonlocal result_failed
            result = original_scalar(
                connection,
                statement,
                *args,
                **kwargs,
            )
            if (
                not result_failed
                and "pg_try_advisory_lock" in str(statement)
                and result is True
            ):
                result_failed = True
                raise RuntimeError(
                    "injected advisory lock result failure"
                )
            return result

        monkeypatch.setattr(
            sa.engine.Connection,
            "scalar",
            fail_after_acquisition,
        )
        with pytest.raises(
            RuntimeError,
            match="injected advisory lock result failure",
        ):
            with postgres_activity_write_plane_guard(
                store.engine,
                timeout_seconds=1,
                poll_seconds=0.02,
            ):
                pytest.fail("guard unexpectedly yielded")

        assert result_failed is True
        with postgres_activity_write_plane_guard(
            store.engine,
            timeout_seconds=1,
            poll_seconds=0.02,
        ) as connection:
            assert connection is not None


def test_pool_size_one_finalization_does_not_deadlock() -> None:
    with _postgres_store(
        pool_size=1,
        pool_timeout=1,
    ) as store:
        request, run, finalizer, _event_id = _decision_fixture(store)
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(
                finalizer.finalize,
                request,
                run,
            ).result(timeout=5)

        assert result.status is DecisionStatus.COMPLETED
        assert result.persistence_status is PersistenceStatus.PERSISTED
        with store.factory() as session:
            assert session.scalar(
                sa.select(sa.func.count()).select_from(DecisionRecord)
            ) == 1


def test_success_releases_advisory_lock_and_restores_isolation() -> None:
    with _postgres_store(pool_size=1) as store:
        with store.engine.connect() as connection:
            baseline_isolation = connection.get_isolation_level()

        request, run, finalizer, _event_id = _decision_fixture(store)
        result = finalizer.finalize(request, run)
        assert result.persistence_status is PersistenceStatus.PERSISTED

        with store.engine.connect() as connection:
            assert connection.get_isolation_level() == baseline_isolation
            assert (
                connection.scalar(sa.text("SHOW transaction_isolation"))
                == baseline_isolation.casefold()
            )
            assert connection.scalar(
                sa.text(
                    "SELECT count(*) FROM pg_locks "
                    "WHERE pid = pg_backend_pid() "
                    "AND locktype = 'advisory'"
                )
            ) == 0
            assert connection.scalar(
                sa.text(
                    "SELECT pg_try_advisory_lock("
                    "hashtextextended(:write_plane_key, 0))"
                ),
                {"write_plane_key": _ACTIVITY_WRITE_PLANE_KEY},
            ) is True
            assert connection.scalar(
                sa.text(
                    "SELECT pg_advisory_unlock("
                    "hashtextextended(:write_plane_key, 0))"
                ),
                {"write_plane_key": _ACTIVITY_WRITE_PLANE_KEY},
            ) is True


def test_slow_postgres_commit_returns_unknown_then_recovers(
    monkeypatch,
) -> None:
    with _postgres_store(pool_size=2) as store:
        request, run, _finalizer_instance, _event_id = (
            _decision_fixture(store)
        )
        commit_started = threading.Event()
        release_commit = threading.Event()
        commit_finished = threading.Event()
        session_closed = threading.Event()
        original_do_commit = store.engine.dialect.do_commit

        class CommitCompletionSession(Session):
            def close(self) -> None:
                try:
                    super().close()
                finally:
                    session_closed.set()

        finalization_factory = sessionmaker(
            bind=store.engine,
            class_=CommitCompletionSession,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )

        def slow_transaction_commit(dbapi_connection) -> None:
            if not dbapi_connection.autocommit:
                commit_started.set()
                if not release_commit.wait(timeout=5):
                    raise TimeoutError(
                        "test PostgreSQL commit was not released"
                    )
            original_do_commit(dbapi_connection)
            if commit_started.is_set():
                commit_finished.set()

        monkeypatch.setattr(
            store.engine.dialect,
            "do_commit",
            slow_transaction_commit,
        )
        finalizer = _finalizer(
            finalization_factory,
            run.source_refs[0],
            timeout_seconds=0.2,
        )
        drain_thread: threading.Thread | None = None
        try:
            result = finalizer.finalize(request, run)

            assert commit_started.is_set()
            assert result.status is DecisionStatus.FAILED
            assert result.persistence_status is PersistenceStatus.UNKNOWN
            assert result.decision_record_id is None
            assert result.limitations == [
                "decision_finalization_outcome_unknown"
            ]
            drain_thread = threading.Thread(
                target=lambda: asyncio.run(finalizer.adrain())
            )
            drain_thread.start()
            time.sleep(0.02)
            assert drain_thread.is_alive()
        finally:
            release_commit.set()
            assert commit_finished.wait(timeout=5)
            assert session_closed.wait(timeout=5)
            if drain_thread is not None:
                drain_thread.join(timeout=5)
                assert not drain_thread.is_alive()

        with store.factory() as session:
            row = session.scalars(sa.select(DecisionRecord)).one()
            recovered = decision_result_from_record(row)
        assert recovered.status is DecisionStatus.COMPLETED
        assert recovered.persistence_status is PersistenceStatus.PERSISTED
        assert recovered.request_id == request.request_id
        assert recovered.decision_record_id == row.id


def test_cleanup_failure_does_not_reverse_committed_success(
    monkeypatch,
) -> None:
    with _postgres_store(pool_size=1) as store:
        original_execute = sa.engine.Connection.execute
        original_invalidate = sa.engine.Connection.invalidate
        unlock_failed = False
        invalidate_failed = False

        def fail_unlock(connection, statement, *args, **kwargs):
            nonlocal unlock_failed
            if (
                not unlock_failed
                and "pg_advisory_unlock" in str(statement)
            ):
                unlock_failed = True
                raise RuntimeError("injected unlock failure")
            return original_execute(connection, statement, *args, **kwargs)

        def fail_invalidate(connection, exception=None):
            nonlocal invalidate_failed
            if not invalidate_failed:
                invalidate_failed = True
                raise RuntimeError("injected invalidate failure")
            return original_invalidate(connection, exception)

        monkeypatch.setattr(sa.engine.Connection, "execute", fail_unlock)
        monkeypatch.setattr(
            sa.engine.Connection,
            "invalidate",
            fail_invalidate,
        )
        request, run, finalizer, _event_id = _decision_fixture(store)

        result = finalizer.finalize(request, run)

        assert result.status is DecisionStatus.COMPLETED
        assert result.persistence_status is PersistenceStatus.PERSISTED
        assert result.decision_record_id is not None
        assert unlock_failed is True
        assert invalidate_failed is True
        with store.factory() as session:
            assert session.scalar(
                sa.select(sa.func.count()).select_from(DecisionRecord)
            ) == 1
        with postgres_activity_write_plane_guard(
            store.engine,
            timeout_seconds=1,
            poll_seconds=0.02,
        ) as connection:
            assert connection is not None
