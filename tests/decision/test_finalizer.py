from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session, sessionmaker

import healthmes.decision.finalizer as finalizer_module
from healthmes.activity.locking import activity_write_lock
from healthmes.decision import (
    DECISION_PAYLOAD_SCHEMA,
    DECISION_RECORD_SCHEMA,
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
    DecisionRequest,
    DecisionResult,
    DecisionStatus,
    DomainAccessGrant,
    ExecutionScope,
    FreshnessStatus,
    HealthMesDecisionEngine,
    PersistenceStatus,
    RuntimeMetadata,
    SourceRef,
    ToolCallRecord,
    ToolCallStatus,
    decision_result_from_record,
)
from healthmes.decision.access import _current_source_content_digest
from healthmes.store import (
    Base,
    DecisionKind,
    DecisionRecord,
    WellnessEvent,
    create_db_engine,
)

NOW = datetime(2026, 8, 12, 6, tzinfo=UTC)
WINDOW_START = NOW - timedelta(hours=2)
WINDOW_END = NOW - timedelta(hours=1)


class StoredNutritionProvider:
    metadata = ContextProviderMetadata(
        provider_id="nutrition",
        domain="nutrition",
        description="Stored nutrition context for finalizer tests.",
        capabilities=(
            ContextCapability(
                capability="nutrition.summary",
                description="Return a stored nutrition summary.",
                granularities=("summary",),
                query_fields=("start", "end", "timezone"),
                output_fields=("caffeine_mg",),
                max_lookback_days=7,
                sensitivity="nutrition",
                freshness_expectation="Stored event timestamp.",
            ),
        ),
    )

    async def query(self, session, query, *, now):
        raise AssertionError("finalization must not call providers")


class StoredWearableProvider:
    metadata = ContextProviderMetadata(
        provider_id="wearable",
        domain="wearable",
        description="External wearable context for finalizer tests.",
        capabilities=(
            ContextCapability(
                capability="wearable.readiness",
                description="Return an external readiness snapshot.",
                granularities=("summary",),
                query_fields=("start", "end", "timezone"),
                output_fields=("score",),
                max_lookback_days=7,
                sensitivity="wearable",
                freshness_expectation="External snapshot timestamp.",
            ),
        ),
    )

    async def query(self, session, query, *, now):
        raise AssertionError("finalization must not call providers")


@pytest.fixture
def persistence():
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield engine, factory
    finally:
        engine.dispose()


def _request(
    *,
    question: str = "Should I take a break now?",
    request_id: uuid.UUID | None = None,
    turn_id: uuid.UUID | None = None,
) -> DecisionRequest:
    return DecisionRequest(
        request_id=request_id or uuid.uuid4(),
        turn_id=turn_id or uuid.uuid4(),
        question=question,
        requested_at=NOW,
        timezone="UTC",
        caller=DecisionCaller(
            principal_id="owner",
            authenticated=True,
            execution_scope=ExecutionScope.LOCAL,
        ),
    )


def _policy(
    *,
    domain: str = "nutrition",
    enabled: bool = True,
) -> ContextAccessPolicy:
    return ContextAccessPolicy(
        owner_principal_id="owner",
        grants=(
            DomainAccessGrant(
                domain=domain,
                enabled=enabled,
            ),
        ),
    )


def _event(
    session: Session,
    *,
    expires_at: datetime | None = None,
) -> WellnessEvent:
    row = WellnessEvent(
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
        expires_at=expires_at,
        payload={
            "window": {
                "start": WINDOW_START.isoformat(),
                "end": WINDOW_END.isoformat(),
            },
            "caffeine_mg": 80,
        },
        derived_from=None,
    )
    session.add(row)
    session.commit()
    return row


def _source_ref(event: WellnessEvent) -> SourceRef:
    ref = SourceRef(
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
    session = object_session(event)
    assert session is not None
    content_digest = _current_source_content_digest(session, ref)
    assert content_digest is not None
    return ref.model_copy(
        update={"content_digest": content_digest},
        deep=True,
    )


def _query() -> ContextQuery:
    return ContextQuery(
        provider_id="nutrition",
        capability="nutrition.summary",
        start=WINDOW_START,
        end=NOW,
        timezone="UTC",
    )


def _run(
    request: DecisionRequest,
    refs: list[SourceRef],
    *,
    used_ids: list[str] | None = None,
    proposed_action: bool = True,
    status: DecisionStatus = DecisionStatus.COMPLETED,
    query: ContextQuery | None = None,
    payload: dict[str, int] | None = None,
    runtime: RuntimeMetadata | None = None,
    answer: str | None = None,
    context_status: ContextStatus = ContextStatus.OK,
    result_limitations: list[str] | None = None,
) -> DecisionAgentRun:
    query = query or _query()
    result = ContextResult(
        query_id=query.query_id,
        provider_id=query.provider_id,
        capability=query.capability,
        status=context_status,
        payload=payload or {"caffeine_mg": 80},
        source_refs=refs,
        freshness=ContextFreshness(
            status=FreshnessStatus.CURRENT,
            as_of=NOW,
            age_seconds=0,
        ),
        coverage=ContextCoverage(
            status=CoverageStatus.COMPLETE,
            ratio=1,
        ),
        limitations=result_limitations or [],
    )
    record = ToolCallRecord(
        query=query,
        status=ToolCallStatus.COMPLETED,
        started_at=NOW,
        finished_at=NOW,
        result=result,
    )
    draft_kwargs = {
        "status": status,
        "used_source_ref_ids": (
            used_ids
            if used_ids is not None
            else [item.reference_id for item in refs]
        ),
    }
    if status is DecisionStatus.COMPLETED:
        draft_kwargs.update(
            {
                "answer": answer
                or "Take a short break before choosing more caffeine.",
                "proposed_action": proposed_action,
                "confidence": 0.8,
                "uncertainty": "Only the retained context was considered.",
            }
        )
    elif status is DecisionStatus.NEEDS_CLARIFICATION:
        draft_kwargs["clarification_question"] = (
            "Which drink are you considering?"
        )
    return DecisionAgentRun(
        request_id=request.request_id,
        turn_id=request.turn_id,
        draft=DecisionDraft(**draft_kwargs),
        source_refs=tuple(refs),
        runtime=runtime
        or RuntimeMetadata(
            runtime="scripted",
            model="decision-test-v1",
            input_tokens=12,
            output_tokens=8,
        ),
        steps_used=1,
        tool_trace=(record,),
        system_policy_version="healthmes-decision-policy.test",
        started_at=NOW,
        finished_at=NOW,
    )


def _finalizer(
    factory,
    *,
    policy: ContextAccessPolicy | None = None,
    policy_resolver: (
        Callable[[DecisionRequest], ContextAccessPolicy] | None
    ) = None,
    registry: ContextProviderRegistry | None = None,
    timeout_seconds: float = 5,
    max_workers: int = 8,
) -> DecisionFinalizer:
    registry = registry or ContextProviderRegistry(
        (StoredNutritionProvider(),)
    )
    return DecisionFinalizer(
        access_layer=ContextAccessLayer(
            registry,
            clock=lambda: NOW,
        ),
        session_factory=factory,
        policy_resolver=(
            policy_resolver
            or (lambda _request: policy or _policy())
        ),
        timeout_seconds=timeout_seconds,
        max_workers=max_workers,
        clock=lambda: NOW,
    )


def _payload_digest(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_source_backed_action_is_atomically_persisted(persistence):
    _engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()

    result = _finalizer(factory).finalize(
        request,
        _run(request, [ref]),
    )

    assert result.status is DecisionStatus.COMPLETED
    assert result.proposed_action is True
    assert result.persistence_status is PersistenceStatus.PERSISTED
    assert result.decision_record_id is not None
    assert result.source_refs == [ref]
    with factory() as session:
        row = session.scalars(sa.select(DecisionRecord)).one()
        assert row.id == result.decision_record_id
        assert row.kind is DecisionKind.INSIGHT
        assert row.decision_request_id == request.request_id
        assert row.decision_turn_id == request.turn_id
        assert row.decision_request_fingerprint is not None
        assert row.tokens == 20
        assert row.tree["schema"] == DECISION_RECORD_SCHEMA
        assert row.tree["healthmes"] == {
            "status": "completed",
            "proposed_action": True,
            "validated_source_count": 1,
            "confidence": 0.8,
            "persistence_status": "persisted",
        }
        assert row.decision_payload is not None
        assert row.decision_payload["schema"] == DECISION_PAYLOAD_SCHEMA
        assert row.decision_payload["result"][
            "decision_record_id"
        ] == str(row.id)
        assert row.decision_payload["source_refs"] == [
            ref.model_dump(mode="json")
        ]
        visible = str(row.tree)
        assert request.question not in visible
        assert ref.reference_id not in visible
        assert str(ref.record_id) not in visible
        assert "query_id" not in visible
        assert "tool_trace" not in visible


def test_source_backed_information_is_persisted_without_action(persistence):
    _engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()

    result = _finalizer(factory).finalize(
        request,
        _run(request, [ref], proposed_action=False),
    )

    assert result.status is DecisionStatus.COMPLETED
    assert result.proposed_action is False
    assert result.persistence_status is PersistenceStatus.PERSISTED
    with factory() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(DecisionRecord)
        ) == 1


def test_tool_result_limitations_are_preserved_when_model_omits_them(
    persistence,
):
    _engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()

    result = _finalizer(factory).finalize(
        request,
        _run(
            request,
            [ref],
            context_status=ContextStatus.PARTIAL,
            result_limitations=["context_stale"],
        ),
    )

    assert result.status is DecisionStatus.COMPLETED
    assert result.limitations == ["context_stale"]
    with factory() as session:
        row = session.scalars(sa.select(DecisionRecord)).one()
        assert row.decision_payload is not None
        assert row.decision_payload["result"]["limitations"] == [
            "context_stale"
        ]


def test_safe_tool_error_is_preserved_without_exposing_unknown_error(
    persistence,
):
    _engine, factory = persistence
    request = _request()
    base_run = _run(
        request,
        [],
        used_ids=[],
        proposed_action=False,
        status=DecisionStatus.NEEDS_CLARIFICATION,
    )
    safe_error = ToolCallRecord(
        query=_query(),
        status=ToolCallStatus.FAILED,
        started_at=NOW,
        finished_at=NOW,
        error_code="tool_execution_failed",
    )
    unknown_error = ToolCallRecord(
        query=_query(),
        status=ToolCallStatus.FAILED,
        started_at=NOW,
        finished_at=NOW,
        error_code="calendar_timeout",
    )
    run = base_run.model_copy(
        update={"tool_trace": (safe_error, unknown_error)},
        deep=True,
    )

    result = _finalizer(factory).finalize(request, run)

    assert result.status is DecisionStatus.NEEDS_CLARIFICATION
    assert "tool_execution_failed" in result.limitations
    assert "calendar_timeout" not in result.limitations


def test_public_record_redacts_internal_source_reference_ids(
    persistence,
):
    _engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request(question="private question must stay in audit data")
    answer = f"Take a break because {ref.reference_id} supports it."

    result = _finalizer(factory).finalize(
        request,
        _run(request, [ref], answer=answer),
    )

    assert result.answer == answer
    with factory() as session:
        row = session.scalars(sa.select(DecisionRecord)).one()
        assert ref.reference_id not in row.summary
        assert ref.reference_id not in str(row.tree)
        assert row.decision_payload is not None
        assert (
            row.decision_payload["result"]["answer"]
            == answer
        )
        assert (
            row.decision_payload["request"]["question"]
            == request.question
        )


def test_clarification_is_not_persisted(persistence):
    _engine, factory = persistence
    request = _request()
    run = _run(
        request,
        [],
        used_ids=[],
        proposed_action=False,
        status=DecisionStatus.NEEDS_CLARIFICATION,
    )

    result = _finalizer(factory).finalize(request, run)

    assert result.status is DecisionStatus.NEEDS_CLARIFICATION
    assert result.persistence_status is PersistenceStatus.NOT_REQUIRED
    with factory() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(DecisionRecord)
        ) == 0


def test_only_refs_selected_by_the_model_are_persisted(persistence):
    _engine, factory = persistence
    with factory() as session:
        first = _source_ref(_event(session))
        second = _source_ref(_event(session))
    request = _request()

    result = _finalizer(factory).finalize(
        request,
        _run(
            request,
            [first, second],
            used_ids=[second.reference_id],
        ),
    )

    assert result.source_refs == [second]
    with factory() as session:
        row = session.scalars(sa.select(DecisionRecord)).one()
        assert row.decision_payload is not None
        assert [
            item["reference_id"]
            for item in row.decision_payload["source_refs"]
        ] == [second.reference_id]


def test_forged_reference_not_in_tool_trace_fails_closed(persistence):
    _engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()
    forged = "sr_" + "0" * 32

    result = _finalizer(factory).finalize(
        request,
        _run(request, [ref], used_ids=[forged]),
    )

    assert result.status is DecisionStatus.FAILED
    assert result.proposed_action is False
    assert result.persistence_status is PersistenceStatus.NOT_REQUIRED
    assert "decision_source_ref_not_in_tool_trace" in result.limitations
    with factory() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(DecisionRecord)
        ) == 0


@pytest.mark.parametrize(
    "forged",
    (
        "SR_" + "0" * 32,
        "sR_" + "a" * 32,
    ),
)
def test_mixed_case_source_ref_in_answer_fails_closed(
    persistence,
    forged,
):
    _engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()

    result = _finalizer(factory).finalize(
        request,
        _run(
            request,
            [ref],
            answer=f"Use this unsupported provenance token: {forged}",
        ),
    )

    assert result.status is DecisionStatus.FAILED
    assert result.proposed_action is False
    assert result.persistence_status is PersistenceStatus.NOT_REQUIRED
    assert (
        "decision_text_contains_unvalidated_source_ref"
        in result.limitations
    )
    assert result.source_refs == []
    assert result.tool_trace == []
    with factory() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(DecisionRecord)
        ) == 0


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("delete", "source_ref_record_missing"),
        ("expire", "source_ref_expired"),
    ),
)
def test_deleted_or_expired_ref_cannot_be_finalized(
    persistence,
    mutation,
    reason,
):
    _engine, factory = persistence
    with factory() as session:
        event = _event(session)
        ref = _source_ref(event)
        if mutation == "delete":
            session.delete(event)
        else:
            event.expires_at = NOW - timedelta(seconds=1)
        session.commit()
    request = _request()

    result = _finalizer(factory).finalize(
        request,
        _run(request, [ref]),
    )

    assert result.status is DecisionStatus.FAILED
    assert result.persistence_status is PersistenceStatus.FAILED
    assert reason in result.limitations
    assert "decision_source_ref_revalidation_failed" in result.limitations
    with factory() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(DecisionRecord)
        ) == 0


def test_permission_or_provider_change_blocks_finalization(persistence):
    _engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()

    revoked = _finalizer(
        factory,
        policy=_policy(enabled=False),
    ).finalize(request, _run(request, [ref]))

    registry = ContextProviderRegistry((StoredNutritionProvider(),))
    registry.set_enabled("nutrition", enabled=False)
    disabled_request = request.model_copy(
        update={
            "request_id": uuid.uuid4(),
            "turn_id": uuid.uuid4(),
        }
    )
    disabled = _finalizer(
        factory,
        registry=registry,
    ).finalize(
        disabled_request,
        _run(disabled_request, [ref]),
    )

    assert revoked.status is DecisionStatus.FAILED
    assert "domain_consent_denied" in revoked.limitations
    assert disabled.status is DecisionStatus.FAILED
    assert "provider_disabled" in disabled.limitations


def test_policy_is_resolved_again_inside_finalization_transaction(
    persistence,
):
    _engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()
    policies = iter((_policy(), _policy(enabled=False)))
    calls = 0

    def resolve(_request: DecisionRequest) -> ContextAccessPolicy:
        nonlocal calls
        calls += 1
        return next(policies)

    result = _finalizer(
        factory,
        policy_resolver=resolve,
    ).finalize(request, _run(request, [ref]))

    assert calls == 2
    assert result.status is DecisionStatus.FAILED
    assert result.persistence_status is PersistenceStatus.FAILED
    assert "domain_consent_denied" in result.limitations
    with factory() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(DecisionRecord)
        ) == 0


def test_policy_resolution_failure_without_used_refs_fails_closed(
    persistence,
):
    _engine, factory = persistence
    request = _request()

    def fail_policy(_request: DecisionRequest) -> ContextAccessPolicy:
        raise RuntimeError("policy store unavailable")

    result = _finalizer(
        factory,
        policy_resolver=fail_policy,
    ).finalize(
        request,
        _run(
            request,
            [],
            used_ids=[],
            proposed_action=False,
            status=DecisionStatus.NEEDS_CLARIFICATION,
        ),
    )

    assert result.status is DecisionStatus.FAILED
    assert result.persistence_status is PersistenceStatus.NOT_REQUIRED
    assert result.limitations == ["access_policy_resolution_failed"]
    with factory() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(DecisionRecord)
        ) == 0


class FailingCommitSession(Session):
    def commit(self) -> None:
        raise RuntimeError("injected commit failure")


class CommitTrackingSession(Session):
    commit_calls = 0

    def commit(self) -> None:
        type(self).commit_calls += 1
        super().commit()


class SlowDecisionFlushSession(CommitTrackingSession):
    def flush(self, objects=None) -> None:
        if any(
            isinstance(item, DecisionRecord)
            for item in self.new
        ):
            time.sleep(0.1)
        super().flush(objects)


def test_storage_failure_rolls_back_and_never_returns_action(persistence):
    engine, factory = persistence
    failing_factory = sessionmaker(
        bind=engine,
        class_=FailingCommitSession,
        expire_on_commit=False,
    )
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()

    result = _finalizer(failing_factory).finalize(
        request,
        _run(request, [ref]),
    )

    assert result.status is DecisionStatus.FAILED
    assert result.proposed_action is False
    assert result.persistence_status is PersistenceStatus.FAILED
    assert "decision_record_persistence_failed" in result.limitations
    with factory() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(DecisionRecord)
        ) == 0


def test_payload_work_past_deadline_never_reaches_commit(
    persistence,
    monkeypatch,
):
    engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()
    original_payload = finalizer_module._decision_payload

    def slow_payload(*args, **kwargs):
        time.sleep(0.1)
        return original_payload(*args, **kwargs)

    CommitTrackingSession.commit_calls = 0
    tracked_factory = sessionmaker(
        bind=engine,
        class_=CommitTrackingSession,
        expire_on_commit=False,
    )
    monkeypatch.setattr(
        finalizer_module,
        "_decision_payload",
        slow_payload,
    )

    finalizer = _finalizer(
        tracked_factory,
        timeout_seconds=0.05,
    )
    result = finalizer.finalize(request, _run(request, [ref]))
    finalizer.close()

    assert result.status is DecisionStatus.FAILED
    assert result.persistence_status is PersistenceStatus.FAILED
    assert result.limitations == ["decision_finalization_timeout"]
    assert CommitTrackingSession.commit_calls == 0
    with factory() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(DecisionRecord)
        ) == 0


def test_flush_work_past_deadline_is_rolled_back_before_commit(
    persistence,
):
    engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()
    SlowDecisionFlushSession.commit_calls = 0
    slow_factory = sessionmaker(
        bind=engine,
        class_=SlowDecisionFlushSession,
        expire_on_commit=False,
    )

    finalizer = _finalizer(
        slow_factory,
        timeout_seconds=0.05,
    )
    result = finalizer.finalize(request, _run(request, [ref]))
    finalizer.close()

    assert result.status is DecisionStatus.FAILED
    assert result.persistence_status is PersistenceStatus.FAILED
    assert result.limitations == ["decision_finalization_timeout"]
    assert SlowDecisionFlushSession.commit_calls == 0
    with factory() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(DecisionRecord)
        ) == 0


@pytest.mark.asyncio
async def test_aclose_drains_timed_out_worker_before_static_pool_dispose(
    persistence,
):
    _engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()
    flush_started = threading.Event()
    release_flush = threading.Event()
    session_closed = threading.Event()

    class BlockingFlushSession(Session):
        def flush(self, objects=None) -> None:
            if any(
                isinstance(item, DecisionRecord)
                for item in self.new
            ):
                flush_started.set()
                if not release_flush.wait(timeout=5):
                    raise TimeoutError("test flush was not released")
            super().flush(objects)

        def close(self) -> None:
            try:
                super().close()
            finally:
                session_closed.set()

    slow_factory = sessionmaker(
        bind=_engine,
        class_=BlockingFlushSession,
        expire_on_commit=False,
    )
    finalizer = _finalizer(
        slow_factory,
        timeout_seconds=0.05,
    )

    result = await finalizer.afinalize(
        request,
        _run(request, [ref]),
    )
    assert flush_started.is_set()
    assert result.status is DecisionStatus.FAILED
    assert result.persistence_status is PersistenceStatus.FAILED
    assert result.limitations == ["decision_finalization_timeout"]

    closing = asyncio.create_task(finalizer.aclose())
    await asyncio.sleep(0.02)
    assert closing.done() is False

    release_flush.set()
    await asyncio.wait_for(closing, timeout=1)
    assert session_closed.is_set()

    with factory() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(DecisionRecord)
        ) == 0


@pytest.mark.asyncio
async def test_cancelled_aclose_defers_teardown_until_worker_cleanup(
    persistence,
):
    engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()
    flush_started = threading.Event()
    release_flush = threading.Event()
    session_closed = threading.Event()
    teardown_started = threading.Event()
    teardown_before_cleanup = threading.Event()

    class BlockingFlushSession(Session):
        def flush(self, objects=None) -> None:
            if any(
                isinstance(item, DecisionRecord)
                for item in self.new
            ):
                flush_started.set()
                if not release_flush.wait(timeout=5):
                    raise TimeoutError("test flush was not released")
            super().flush(objects)

        def close(self) -> None:
            try:
                super().close()
            finally:
                session_closed.set()

    slow_factory = sessionmaker(
        bind=engine,
        class_=BlockingFlushSession,
        expire_on_commit=False,
    )
    finalizer = _finalizer(
        slow_factory,
        timeout_seconds=0.05,
    )

    result = await finalizer.afinalize(
        request,
        _run(request, [ref]),
    )
    assert flush_started.is_set()
    assert result.status is DecisionStatus.FAILED
    assert result.persistence_status is PersistenceStatus.FAILED

    async def close_then_teardown() -> None:
        try:
            await finalizer.aclose()
        finally:
            if not session_closed.is_set():
                teardown_before_cleanup.set()
            else:
                engine.dispose()
            teardown_started.set()

    closing = asyncio.create_task(close_then_teardown())
    await asyncio.sleep(0.02)
    closing.cancel()
    await asyncio.sleep(0.02)
    try:
        assert closing.done() is False
        assert teardown_started.is_set() is False
        closing.cancel()
        await asyncio.sleep(0.02)
        assert closing.done() is False
        assert teardown_started.is_set() is False
    finally:
        release_flush.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, timeout=1)
    assert session_closed.is_set()
    assert teardown_started.is_set()
    assert teardown_before_cleanup.is_set() is False


@pytest.mark.asyncio
async def test_finalizer_sync_close_is_rejected_inside_event_loop(
    persistence,
):
    _engine, factory = persistence
    finalizer = _finalizer(factory)

    with pytest.raises(
        RuntimeError,
        match="await aclose",
    ):
        finalizer.close()

    await finalizer.aclose()


def test_slow_session_commit_after_timeout_cannot_create_late_record(
    tmp_path,
):
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'slow-pre-commit.db'}"
    )
    Base.metadata.create_all(engine)
    normal_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with normal_factory() as session:
        ref = _source_ref(_event(session))
    request = _request()
    commit_started = threading.Event()
    release_commit = threading.Event()
    session_closed = threading.Event()

    class SlowCommitSession(Session):
        def commit(self) -> None:
            commit_started.set()
            if not release_commit.wait(timeout=5):
                raise TimeoutError("test commit was not released")
            super().commit()

        def close(self) -> None:
            try:
                super().close()
            finally:
                session_closed.set()

    slow_factory = sessionmaker(
        bind=engine,
        class_=SlowCommitSession,
        expire_on_commit=False,
    )
    try:
        started = time.monotonic()
        result = _finalizer(
            slow_factory,
            timeout_seconds=0.2,
        ).finalize(request, _run(request, [ref]))
        elapsed = time.monotonic() - started

        assert 0.15 <= elapsed < 1
        assert commit_started.is_set()
        assert result.status is DecisionStatus.FAILED
        assert result.persistence_status is PersistenceStatus.FAILED
        assert result.limitations == ["decision_finalization_timeout"]
        with normal_factory() as session:
            assert session.scalar(
                sa.select(sa.func.count()).select_from(DecisionRecord)
            ) == 0
    finally:
        release_commit.set()
        assert session_closed.wait(timeout=5)

    with normal_factory() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(DecisionRecord)
        ) == 0
    engine.dispose()


def test_slow_database_commit_returns_unknown_then_can_be_recovered(
    tmp_path,
    monkeypatch,
):
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'slow-dbapi-commit.db'}"
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()
    commit_started = threading.Event()
    release_commit = threading.Event()
    commit_finished = threading.Event()
    session_closed = threading.Event()
    original_do_commit = engine.dialect.do_commit

    class CommitCompletionSession(Session):
        def close(self) -> None:
            try:
                super().close()
            finally:
                session_closed.set()

    finalization_factory = sessionmaker(
        bind=engine,
        class_=CommitCompletionSession,
        expire_on_commit=False,
    )

    def slow_do_commit(dbapi_connection) -> None:
        commit_started.set()
        if not release_commit.wait(timeout=5):
            raise TimeoutError("test DBAPI commit was not released")
        original_do_commit(dbapi_connection)
        commit_finished.set()

    monkeypatch.setattr(engine.dialect, "do_commit", slow_do_commit)
    finalizer = _finalizer(
        finalization_factory,
        timeout_seconds=0.2,
    )
    drain_thread: threading.Thread | None = None
    try:
        result = finalizer.finalize(
            request,
            _run(request, [ref]),
        )

        assert commit_started.is_set()
        assert result.status is DecisionStatus.FAILED
        assert result.persistence_status is PersistenceStatus.UNKNOWN
        assert result.decision_record_id is None
        assert result.limitations == [
            "decision_finalization_outcome_unknown"
        ]
        with factory() as session:
            assert session.scalar(
                sa.select(sa.func.count()).select_from(DecisionRecord)
            ) == 0
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

    with factory() as session:
        row = session.scalars(sa.select(DecisionRecord)).one()
        recovered = decision_result_from_record(row)
    assert recovered.status is DecisionStatus.COMPLETED
    assert recovered.persistence_status is PersistenceStatus.PERSISTED
    assert recovered.request_id == request.request_id
    assert recovered.decision_record_id == row.id
    engine.dispose()


def test_committed_record_with_late_cleanup_returns_unknown_then_recovers(
    tmp_path,
):
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'late-cleanup.db'}"
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()
    close_started = threading.Event()
    release_close = threading.Event()
    session_closed = threading.Event()

    class SlowCleanupSession(Session):
        def flush(self, objects=None) -> None:
            if any(
                isinstance(item, DecisionRecord)
                for item in self.new
            ):
                self.info["committed_decision"] = True
            super().flush(objects)

        def close(self) -> None:
            try:
                if self.info.get("committed_decision"):
                    close_started.set()
                    if not release_close.wait(timeout=5):
                        raise TimeoutError(
                            "test session cleanup was not released"
                        )
                super().close()
            finally:
                if self.info.get("committed_decision"):
                    session_closed.set()

    finalization_factory = sessionmaker(
        bind=engine,
        class_=SlowCleanupSession,
        expire_on_commit=False,
    )
    finalizer = _finalizer(
        finalization_factory,
        timeout_seconds=0.2,
    )
    try:
        result = finalizer.finalize(
            request,
            _run(request, [ref]),
        )

        assert close_started.is_set()
        assert result.status is DecisionStatus.FAILED
        assert result.persistence_status is PersistenceStatus.UNKNOWN
        assert result.decision_record_id is None
        assert result.limitations == [
            "decision_finalization_outcome_unknown"
        ]
    finally:
        release_close.set()
        assert session_closed.wait(timeout=5)
        finalizer.close()

    with factory() as session:
        row = session.scalars(sa.select(DecisionRecord)).one()
        recovered = decision_result_from_record(row)
    assert recovered.status is DecisionStatus.COMPLETED
    assert recovered.persistence_status is PersistenceStatus.PERSISTED
    assert recovered.request_id == request.request_id
    assert recovered.decision_record_id == row.id
    engine.dispose()


def test_commit_completion_cannot_reverse_unknown_response_contract(
    persistence,
):
    _engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()
    run = _run(request, [ref])
    persisted = DecisionResult(
        request_id=request.request_id,
        turn_id=request.turn_id,
        status=DecisionStatus.COMPLETED,
        answer="Take a short break.",
        proposed_action=True,
        source_refs=[ref],
        persistence_status=PersistenceStatus.PERSISTED,
        decision_record_id=uuid.uuid4(),
        runtime=run.runtime,
    )
    control = finalizer_module._FinalizationControl(
        finalizer_module.steady_time() + 1
    )
    control.enter_commit()

    assert (
        control.expire()
        is finalizer_module._FinalizationPhase.OUTCOME_UNKNOWN
    )
    control.mark_committed(persisted)
    control.signal_done()

    phase, stored_result = control.snapshot()
    assert phase is finalizer_module._FinalizationPhase.OUTCOME_UNKNOWN
    assert stored_result == persisted

    result = _finalizer(factory)._supervised_result(
        request,
        run,
        control,
    )
    assert result.status is DecisionStatus.FAILED
    assert result.persistence_status is PersistenceStatus.UNKNOWN
    assert result.decision_record_id is None
    assert result.limitations == [
        "decision_finalization_outcome_unknown"
    ]


def test_unpublished_commit_success_becomes_unknown_at_deadline(
    persistence,
    monkeypatch,
):
    _engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()
    run = _run(request, [ref])
    persisted = DecisionResult(
        request_id=request.request_id,
        turn_id=request.turn_id,
        status=DecisionStatus.COMPLETED,
        answer="Take a short break.",
        proposed_action=True,
        source_refs=[ref],
        persistence_status=PersistenceStatus.PERSISTED,
        decision_record_id=uuid.uuid4(),
        runtime=run.runtime,
    )
    current_time = [10.0]
    monkeypatch.setattr(
        finalizer_module,
        "steady_time",
        lambda: current_time[0],
    )
    control = finalizer_module._FinalizationControl(11.0)
    control.enter_commit()
    control.mark_committed(persisted)
    current_time[0] = 12.0
    control.signal_done()

    phase, stored_result = control.snapshot()
    assert phase is finalizer_module._FinalizationPhase.COMMITTED
    assert stored_result == persisted
    assert control.done.is_set()

    result = _finalizer(factory)._supervised_result(
        request,
        run,
        control,
    )
    phase, stored_result = control.snapshot()
    assert phase is finalizer_module._FinalizationPhase.OUTCOME_UNKNOWN
    assert stored_result == persisted
    assert result.status is DecisionStatus.FAILED
    assert result.persistence_status is PersistenceStatus.UNKNOWN
    assert result.decision_record_id is None
    assert result.limitations == [
        "decision_finalization_outcome_unknown"
    ]


def test_timely_published_commit_remains_success_after_caller_delay(
    persistence,
    monkeypatch,
):
    _engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()
    run = _run(request, [ref])
    persisted = DecisionResult(
        request_id=request.request_id,
        turn_id=request.turn_id,
        status=DecisionStatus.COMPLETED,
        answer="Take a short break.",
        proposed_action=True,
        source_refs=[ref],
        persistence_status=PersistenceStatus.PERSISTED,
        decision_record_id=uuid.uuid4(),
        runtime=run.runtime,
    )
    current_time = [10.0]
    monkeypatch.setattr(
        finalizer_module,
        "steady_time",
        lambda: current_time[0],
    )
    control = finalizer_module._FinalizationControl(11.0)
    control.enter_commit()
    control.mark_committed(persisted)
    assert (
        control.expire()
        is finalizer_module._FinalizationPhase.COMMITTED
    )
    control.signal_done()
    current_time[0] = 12.0

    result = _finalizer(factory)._supervised_result(
        request,
        run,
        control,
    )
    phase, stored_result = control.snapshot()
    assert phase is finalizer_module._FinalizationPhase.COMMITTED
    assert stored_result == persisted
    assert result == persisted


def test_publication_at_deadline_uses_unknown_recovery_contract(
    persistence,
    monkeypatch,
):
    _engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()
    run = _run(request, [ref])
    persisted = DecisionResult(
        request_id=request.request_id,
        turn_id=request.turn_id,
        status=DecisionStatus.COMPLETED,
        answer="Take a short break.",
        proposed_action=True,
        source_refs=[ref],
        persistence_status=PersistenceStatus.PERSISTED,
        decision_record_id=uuid.uuid4(),
        runtime=run.runtime,
    )
    current_time = [10.0]
    monkeypatch.setattr(
        finalizer_module,
        "steady_time",
        lambda: current_time[0],
    )
    control = finalizer_module._FinalizationControl(11.0)
    control.enter_commit()
    control.mark_committed(persisted)
    current_time[0] = 11.0
    control.signal_done()

    result = _finalizer(factory)._supervised_result(
        request,
        run,
        control,
    )
    phase, stored_result = control.snapshot()
    assert phase is finalizer_module._FinalizationPhase.OUTCOME_UNKNOWN
    assert stored_result == persisted
    assert result.status is DecisionStatus.FAILED
    assert result.persistence_status is PersistenceStatus.UNKNOWN
    assert result.decision_record_id is None
    assert result.limitations == [
        "decision_finalization_outcome_unknown"
    ]


def test_finalizer_capacity_is_bounded_and_auditable(persistence):
    _engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    first_request = _request()
    second_request = _request()
    entered = threading.Event()
    release = threading.Event()
    first_result: list[DecisionResult] = []

    def blocking_policy(_request: DecisionRequest) -> ContextAccessPolicy:
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("test policy was not released")
        return _policy()

    finalizer = _finalizer(
        factory,
        policy_resolver=blocking_policy,
        timeout_seconds=2,
        max_workers=1,
    )

    worker = threading.Thread(
        target=lambda: first_result.append(
            finalizer.finalize(
                first_request,
                _run(first_request, [ref]),
            )
        )
    )
    worker.start()
    try:
        assert entered.wait(timeout=1)
        rejected = finalizer.finalize(
            second_request,
            _run(second_request, [ref]),
        )
        assert rejected.status is DecisionStatus.FAILED
        assert rejected.persistence_status is PersistenceStatus.FAILED
        assert rejected.limitations == [
            "decision_finalization_capacity_exhausted"
        ]
    finally:
        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(first_result) == 1
    assert (
        first_result[0].persistence_status
        is PersistenceStatus.PERSISTED
    )


def test_completed_result_releases_capacity_before_becoming_visible(
    persistence,
):
    _engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    first_request = _request()
    second_request = _request()
    finalizer = _finalizer(
        factory,
        timeout_seconds=2,
        max_workers=1,
    )
    semaphore = finalizer._worker_slots
    done_state_at_release: list[bool] = []
    lock_state_at_release: list[bool] = []

    class ReleaseGate:
        def acquire(self, *args, **kwargs):
            return semaphore.acquire(*args, **kwargs)

        def release(self) -> None:
            lock_state_at_release.append(
                finalizer._workers_lock.locked()
            )
            controls = tuple(finalizer._active_controls)
            assert len(controls) == 1
            done_state_at_release.append(controls[0].done.is_set())
            semaphore.release()

    finalizer._worker_slots = ReleaseGate()
    first_result: list[DecisionResult] = []
    first = threading.Thread(
        target=lambda: first_result.append(
            finalizer.finalize(
                first_request,
                _run(first_request, [ref]),
            )
        )
    )
    first.start()
    first.join(timeout=5)

    assert not first.is_alive()
    assert len(first_result) == 1
    assert lock_state_at_release == [True]
    assert done_state_at_release == [False]
    assert (
        first_result[0].persistence_status
        is PersistenceStatus.PERSISTED
    )

    second = finalizer.finalize(
        second_request,
        _run(second_request, [ref]),
    )
    finalizer.close()
    assert second.persistence_status is PersistenceStatus.PERSISTED


def test_shutdown_cannot_expire_prepared_worker_completion(
    persistence,
):
    _engine, factory = persistence
    request = _request()
    finalizer = _finalizer(
        factory,
        timeout_seconds=2,
        max_workers=1,
    )
    lock = finalizer._workers_lock
    completion_waiting = threading.Event()
    allow_completion = threading.Event()

    class CompletionGate:
        def __enter__(self):
            if threading.current_thread().name.startswith(
                "healthmes-finalizer-"
            ):
                completion_waiting.set()
                if not allow_completion.wait(timeout=5):
                    raise TimeoutError(
                        "test completion was not allowed"
                    )
            lock.acquire()
            return self

        def __exit__(self, *_exc_info) -> None:
            lock.release()

    finalizer._workers_lock = CompletionGate()
    results: list[DecisionResult] = []
    worker = threading.Thread(
        target=lambda: results.append(
            finalizer.finalize(
                request,
                _run(
                    request,
                    [],
                    status=DecisionStatus.NEEDS_CLARIFICATION,
                ),
            )
        )
    )
    worker.start()
    assert completion_waiting.wait(timeout=1)

    shutdown = threading.Thread(target=finalizer.begin_shutdown)
    shutdown.start()
    try:
        shutdown.join(timeout=1)
        assert not shutdown.is_alive()
        assert results == []
    finally:
        allow_completion.set()
        worker.join(timeout=5)
        shutdown.join(timeout=5)

    assert not worker.is_alive()
    assert not shutdown.is_alive()
    assert len(results) == 1
    assert results[0].status is DecisionStatus.NEEDS_CLARIFICATION
    assert results[0].persistence_status is PersistenceStatus.NOT_REQUIRED


def test_finalizer_shutdown_seals_admission_and_fences_precommit(
    persistence,
):
    _engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    first_request = _request()
    second_request = _request()
    entered = threading.Event()
    release = threading.Event()
    first_result: list[DecisionResult] = []

    def blocking_policy(_request: DecisionRequest) -> ContextAccessPolicy:
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("test policy was not released")
        return _policy()

    finalizer = _finalizer(
        factory,
        policy_resolver=blocking_policy,
        timeout_seconds=2,
        max_workers=2,
    )
    worker = threading.Thread(
        target=lambda: first_result.append(
            finalizer.finalize(
                first_request,
                _run(first_request, [ref]),
            )
        )
    )
    worker.start()
    try:
        assert entered.wait(timeout=1)
        finalizer.begin_shutdown()
        rejected = finalizer.finalize(
            second_request,
            _run(second_request, [ref]),
        )
        assert rejected.status is DecisionStatus.FAILED
        assert rejected.persistence_status is PersistenceStatus.FAILED
        assert rejected.limitations == [
            "decision_finalization_timeout"
        ]
    finally:
        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(first_result) == 1
    assert first_result[0].status is DecisionStatus.FAILED
    assert first_result[0].persistence_status is PersistenceStatus.FAILED
    assert first_result[0].limitations == [
        "decision_finalization_timeout"
    ]
    with factory() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(DecisionRecord)
        ) == 0


def test_retry_returns_one_logical_record_and_conflicting_request_fails(
    persistence,
):
    _engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()
    finalizer = _finalizer(factory)
    first = finalizer.finalize(request, _run(request, [ref]))

    retry_request = request.model_copy(update={"turn_id": uuid.uuid4()})
    retry = finalizer.finalize(
        retry_request,
        _run(retry_request, [ref]),
    )
    conflicting = request.model_copy(
        update={
            "turn_id": uuid.uuid4(),
            "question": "Use the same ID for different content.",
        }
    )
    conflict = finalizer.finalize(
        conflicting,
        _run(conflicting, [ref]),
    )

    assert retry.decision_record_id == first.decision_record_id
    assert retry.turn_id == first.turn_id
    assert retry.persistence_status is PersistenceStatus.PERSISTED
    assert conflict.status is DecisionStatus.FAILED
    assert conflict.persistence_status is PersistenceStatus.FAILED
    assert "decision_request_id_conflict" in conflict.limitations
    with factory() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(DecisionRecord)
        ) == 1


@pytest.mark.parametrize("mutation", ("delete", "expire", "change"))
def test_retry_revalidates_current_source_state(
    persistence,
    mutation,
):
    _engine, factory = persistence
    with factory() as session:
        event = _event(session)
        event_id = event.id
        ref = _source_ref(event)
    request = _request()
    finalizer = _finalizer(factory)
    first = finalizer.finalize(request, _run(request, [ref]))
    assert first.persistence_status is PersistenceStatus.PERSISTED

    with factory() as session:
        event = session.get(WellnessEvent, event_id)
        assert event is not None
        if mutation == "delete":
            session.delete(event)
        elif mutation == "expire":
            event.expires_at = NOW - timedelta(seconds=1)
        else:
            event.payload = {**event.payload, "caffeine_mg": 300}
        session.commit()

    retry_request = request.model_copy(
        update={"turn_id": uuid.uuid4()}
    )
    retry = finalizer.finalize(
        retry_request,
        _run(retry_request, [ref]),
    )

    assert retry.status is DecisionStatus.FAILED
    assert retry.proposed_action is False
    assert retry.persistence_status is PersistenceStatus.FAILED
    assert "decision_source_ref_revalidation_failed" in retry.limitations
    expected = {
        "delete": "source_ref_record_missing",
        "expire": "source_ref_expired",
        "change": "source_ref_content_changed",
    }[mutation]
    assert expected in retry.limitations
    assert retry.tool_trace == []
    with factory() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(DecisionRecord)
        ) == 1


def test_retry_revalidates_current_permission_and_provider_state(
    persistence,
):
    _engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()
    first = _finalizer(factory).finalize(
        request,
        _run(request, [ref]),
    )
    assert first.persistence_status is PersistenceStatus.PERSISTED

    retry_request = request.model_copy(
        update={"turn_id": uuid.uuid4()}
    )
    revoked = _finalizer(
        factory,
        policy=_policy(enabled=False),
    ).finalize(
        retry_request,
        _run(retry_request, [ref]),
    )
    registry = ContextProviderRegistry((StoredNutritionProvider(),))
    registry.set_enabled("nutrition", enabled=False)
    disabled = _finalizer(
        factory,
        registry=registry,
    ).finalize(
        retry_request,
        _run(retry_request, [ref]),
    )

    assert revoked.status is DecisionStatus.FAILED
    assert revoked.proposed_action is False
    assert "domain_consent_denied" in revoked.limitations
    assert revoked.tool_trace == []
    assert disabled.status is DecisionStatus.FAILED
    assert disabled.proposed_action is False
    assert "provider_disabled" in disabled.limitations
    assert disabled.tool_trace == []


def test_turn_id_cannot_be_reused_by_another_request(persistence):
    _engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    first_request = _request()
    finalizer = _finalizer(factory)
    first = finalizer.finalize(
        first_request,
        _run(first_request, [ref]),
    )
    conflicting_request = _request(turn_id=first_request.turn_id)

    conflict = finalizer.finalize(
        conflicting_request,
        _run(conflicting_request, [ref]),
    )

    assert first.persistence_status is PersistenceStatus.PERSISTED
    assert conflict.status is DecisionStatus.FAILED
    assert conflict.persistence_status is PersistenceStatus.FAILED
    assert "decision_turn_id_conflict" in conflict.limitations
    with factory() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(DecisionRecord)
        ) == 1


def test_corrupted_private_payload_makes_retry_fail_closed(persistence):
    _engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()
    finalizer = _finalizer(factory)
    first = finalizer.finalize(request, _run(request, [ref]))
    assert first.decision_record_id is not None
    with factory() as session:
        row = session.get(DecisionRecord, first.decision_record_id)
        assert row is not None
        row.decision_payload = {
            "schema": DECISION_PAYLOAD_SCHEMA,
            "request_fingerprint": row.decision_request_fingerprint,
            "result": {"status": "completed"},
        }
        session.commit()

    retry_request = request.model_copy(
        update={"turn_id": uuid.uuid4()}
    )
    retry = finalizer.finalize(
        retry_request,
        _run(retry_request, [ref]),
    )

    assert retry.status is DecisionStatus.FAILED
    assert retry.proposed_action is False
    assert retry.persistence_status is PersistenceStatus.FAILED
    assert "decision_record_contract_invalid" in retry.limitations
    with factory() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(DecisionRecord)
        ) == 1


@pytest.mark.parametrize(
    "mutation",
    (
        "boolean_string",
        "source_refs",
        "runtime",
        "public_tree",
        "public_summary",
    ),
)
def test_checksum_recomputed_contract_tampering_fails_closed(
    persistence,
    mutation,
):
    _engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()
    finalizer = _finalizer(factory)
    first = finalizer.finalize(request, _run(request, [ref]))
    assert first.decision_record_id is not None

    with factory() as session:
        row = session.get(DecisionRecord, first.decision_record_id)
        assert row is not None
        assert row.decision_payload is not None
        payload = copy.deepcopy(row.decision_payload)
        if mutation == "boolean_string":
            payload["result"]["proposed_action"] = "false"
        elif mutation == "source_refs":
            payload["source_refs"] = []
        elif mutation == "runtime":
            payload["run"]["runtime"]["model"] = "tampered-model"
        elif mutation == "public_tree":
            row.tree = {**row.tree, "label": "tampered public tree"}
        else:
            row.summary = "tampered public summary"
        if mutation in {"boolean_string", "source_refs", "runtime"}:
            row.decision_payload = payload
            row.decision_payload_digest = _payload_digest(payload)
        session.commit()

    retry_request = request.model_copy(
        update={"turn_id": uuid.uuid4()}
    )
    retry = finalizer.finalize(
        retry_request,
        _run(retry_request, [ref]),
    )

    assert retry.status is DecisionStatus.FAILED
    assert retry.proposed_action is False
    assert retry.persistence_status is PersistenceStatus.FAILED
    assert "decision_record_contract_invalid" in retry.limitations
    assert retry.source_refs == []
    assert retry.tool_trace == []
    assert retry.runtime.runtime == "healthmes-finalizer"
    assert str(ref.record_id) not in retry.model_dump_json()
    with factory() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(DecisionRecord)
        ) == 1


def test_external_wearable_action_requires_retained_local_source(
    persistence,
):
    _engine, factory = persistence
    ref = SourceRef(
        domain="wearable",
        resource_type="health_score",
        record_id="readiness-2026-08-12",
        source_provider="open-wearables",
        observed_start=WINDOW_START,
        schema_version=1,
        derived_by="open-wearables.daily-readiness.v1",
        freshness=FreshnessStatus.CURRENT,
        sensitivity="wearable",
    )
    query = ContextQuery(
        provider_id="wearable",
        capability="wearable.readiness",
        start=WINDOW_START,
        end=NOW,
        timezone="UTC",
    )
    registry = ContextProviderRegistry((StoredWearableProvider(),))
    request = _request()
    finalizer = _finalizer(
        factory,
        policy=_policy(domain="wearable"),
        registry=registry,
    )

    action = finalizer.finalize(
        request,
        _run(
            request,
            [ref],
            query=query,
            payload={"score": 72},
        ),
    )

    assert action.status is DecisionStatus.FAILED
    assert action.proposed_action is False
    assert action.persistence_status is PersistenceStatus.FAILED
    assert (
        "decision_action_requires_retained_source"
        in action.limitations
    )
    assert (
        "external_source_retention_unverified"
        in action.limitations
    )
    with factory() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(DecisionRecord)
        ) == 0


def test_external_wearable_information_preserves_retention_limitation(
    persistence,
):
    _engine, factory = persistence
    ref = SourceRef(
        domain="wearable",
        resource_type="health_score",
        record_id="readiness-2026-08-12",
        source_provider="open-wearables",
        observed_start=WINDOW_START,
        schema_version=1,
        derived_by="open-wearables.daily-readiness.v1",
        freshness=FreshnessStatus.CURRENT,
        sensitivity="wearable",
    )
    query = ContextQuery(
        provider_id="wearable",
        capability="wearable.readiness",
        start=WINDOW_START,
        end=NOW,
        timezone="UTC",
    )
    request = _request()
    result = _finalizer(
        factory,
        policy=_policy(domain="wearable"),
        registry=ContextProviderRegistry(
            (StoredWearableProvider(),)
        ),
    ).finalize(
        request,
        _run(
            request,
            [ref],
            proposed_action=False,
            query=query,
            payload={"score": 72},
        ),
    )

    assert result.status is DecisionStatus.COMPLETED
    assert result.proposed_action is False
    assert result.persistence_status is PersistenceStatus.PERSISTED
    assert (
        "external_source_retention_unverified"
        in result.limitations
    )


def test_model_and_token_storage_bounds_are_safe(persistence):
    _engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()
    model = "m" * 128
    result = _finalizer(factory).finalize(
        request,
        _run(
            request,
            [ref],
            runtime=RuntimeMetadata(
                runtime="scripted",
                model=model,
                input_tokens=2_147_483_647,
                output_tokens=1,
            ),
        ),
    )

    assert result.persistence_status is PersistenceStatus.PERSISTED
    with factory() as session:
        row = session.scalars(sa.select(DecisionRecord)).one()
        assert row.llm_model == model[:64]
        assert row.tokens is None
        assert row.decision_payload is not None
        assert row.decision_payload["run"]["runtime"]["model"] == model
        assert (
            row.decision_payload["run"]["runtime"]["input_tokens"]
            == 2_147_483_647
        )


def test_concurrent_sqlite_finalization_keeps_one_record(tmp_path):
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'finalizer.db'}"
    )
    Base.metadata.create_all(engine)
    normal_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with normal_factory() as session:
        ref = _source_ref(_event(session))

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    request = _request()
    run = _run(request, [ref])
    results = []
    errors = []
    start = threading.Barrier(2)

    def finalize() -> None:
        try:
            start.wait(timeout=5)
            results.append(_finalizer(factory).finalize(request, run))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    workers = [
        threading.Thread(target=finalize, name=f"finalizer-{index}")
        for index in range(2)
    ]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        assert all(not worker.is_alive() for worker in workers)
        assert not errors
        assert len(results) == 2
        assert {
            item.persistence_status for item in results
        } == {PersistenceStatus.PERSISTED}
        assert len(
            {item.decision_record_id for item in results}
        ) == 1
        with normal_factory() as session:
            assert session.scalar(
                sa.select(sa.func.count()).select_from(DecisionRecord)
            ) == 1
    finally:
        engine.dispose()


def test_finalization_process_lock_timeout_is_auditable(tmp_path):
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'process-timeout.db'}"
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()
    run = _run(request, [ref])
    acquired = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with activity_write_lock():
            acquired.set()
            assert release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    finalizer = _finalizer(
        factory,
        timeout_seconds=0.1,
    )
    try:
        assert acquired.wait(timeout=5)
        started = time.monotonic()
        result = finalizer.finalize(request, run)
        assert 0.05 <= time.monotonic() - started < 1
        assert result.status is DecisionStatus.FAILED
        assert result.persistence_status is PersistenceStatus.FAILED
        assert result.limitations == ["decision_finalization_timeout"]
        finalizer.close()
        with factory() as session:
            assert session.scalar(
                sa.select(sa.func.count()).select_from(DecisionRecord)
            ) == 0
    finally:
        release.set()
        holder.join(timeout=5)

    assert not holder.is_alive()
    retry = _finalizer(factory).finalize(request, run)
    assert retry.persistence_status is PersistenceStatus.PERSISTED
    engine.dispose()


def test_finalization_sqlite_database_lock_timeout_is_auditable(
    tmp_path,
):
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'database-timeout.db'}"
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()
    run = _run(request, [ref])
    finalizer = _finalizer(
        factory,
        timeout_seconds=0.1,
    )

    with factory() as blocker:
        # Simulate an external SQLite writer that does not participate in the
        # HealthMes process/file-lock protocol.
        blocker.execute(sa.text("BEGIN IMMEDIATE"))
        started = time.monotonic()
        result = finalizer.finalize(request, run)
        assert 0.05 <= time.monotonic() - started < 1
        assert result.status is DecisionStatus.FAILED
        assert result.persistence_status is PersistenceStatus.FAILED
        assert result.limitations == ["decision_finalization_timeout"]
        blocker.rollback()

    finalizer.close()
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "PRAGMA busy_timeout"
        ).scalar_one() == 30_000
    retry = _finalizer(factory).finalize(request, run)
    assert retry.persistence_status is PersistenceStatus.PERSISTED
    engine.dispose()


def test_sqlite_source_delete_before_finalization_fails_closed(tmp_path):
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'delete-first.db'}"
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        event = _event(session)
        event_id = event.id
        ref = _source_ref(event)
    delete_ready = threading.Event()
    allow_delete_commit = threading.Event()
    finalizer_started = threading.Event()
    result_holder = []
    errors = []

    def delete_source() -> None:
        try:
            with factory() as session:
                session.execute(sa.text("BEGIN IMMEDIATE"))
                row = session.get(WellnessEvent, event_id)
                assert row is not None
                session.delete(row)
                session.flush()
                delete_ready.set()
                assert allow_delete_commit.wait(timeout=5)
                session.commit()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def resolve(_request: DecisionRequest) -> ContextAccessPolicy:
        finalizer_started.set()
        return _policy()

    request = _request()

    def finalize() -> None:
        try:
            result_holder.append(
                _finalizer(
                    factory,
                    policy_resolver=resolve,
                ).finalize(request, _run(request, [ref]))
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    deleter = threading.Thread(target=delete_source)
    worker = threading.Thread(target=finalize)
    try:
        deleter.start()
        assert delete_ready.wait(timeout=5)
        worker.start()
        assert finalizer_started.wait(timeout=5)
        allow_delete_commit.set()
        deleter.join(timeout=10)
        worker.join(timeout=10)

        assert not deleter.is_alive()
        assert not worker.is_alive()
        assert errors == []
        assert len(result_holder) == 1
        result = result_holder[0]
        assert result.status is DecisionStatus.FAILED
        assert result.persistence_status is PersistenceStatus.FAILED
        assert "source_ref_record_missing" in result.limitations
        with factory() as session:
            assert session.get(WellnessEvent, event_id) is None
            assert session.scalar(
                sa.select(sa.func.count()).select_from(DecisionRecord)
            ) == 0
    finally:
        allow_delete_commit.set()
        deleter.join(timeout=5)
        worker.join(timeout=5)
        engine.dispose()


def test_sqlite_finalization_before_source_delete_keeps_audit_record(
    tmp_path,
):
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'finalize-first.db'}"
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        event = _event(session)
        event_id = event.id
        ref = _source_ref(event)
    transaction_started = threading.Event()
    delete_attempted = threading.Event()
    result_holder = []
    errors = []
    policy_calls = 0

    def resolve(_request: DecisionRequest) -> ContextAccessPolicy:
        nonlocal policy_calls
        policy_calls += 1
        if policy_calls == 2:
            transaction_started.set()
            assert delete_attempted.wait(timeout=5)
        return _policy()

    request = _request()

    def finalize() -> None:
        try:
            result_holder.append(
                _finalizer(
                    factory,
                    policy_resolver=resolve,
                ).finalize(request, _run(request, [ref]))
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def delete_source() -> None:
        try:
            assert transaction_started.wait(timeout=5)
            with factory() as session:
                delete_attempted.set()
                session.execute(sa.text("BEGIN IMMEDIATE"))
                row = session.get(WellnessEvent, event_id)
                assert row is not None
                session.delete(row)
                session.commit()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    worker = threading.Thread(target=finalize)
    deleter = threading.Thread(target=delete_source)
    try:
        worker.start()
        deleter.start()
        worker.join(timeout=10)
        deleter.join(timeout=10)

        assert not worker.is_alive()
        assert not deleter.is_alive()
        assert errors == []
        assert len(result_holder) == 1
        result = result_holder[0]
        assert result.status is DecisionStatus.COMPLETED
        assert result.persistence_status is PersistenceStatus.PERSISTED
        with factory() as session:
            assert session.get(WellnessEvent, event_id) is None
            row = session.scalars(sa.select(DecisionRecord)).one()
            assert row.id == result.decision_record_id
    finally:
        worker.join(timeout=5)
        deleter.join(timeout=5)
        engine.dispose()


def test_legacy_decision_rows_remain_valid_and_correlation_is_all_or_none(
    persistence,
):
    _engine, factory = persistence
    with factory() as session:
        legacy = DecisionRecord(
            kind=DecisionKind.INSIGHT,
            tree={
                "type": "llm_step",
                "label": "legacy",
                "children": [],
            },
            summary="legacy",
        )
        session.add(legacy)
        session.commit()
        assert legacy.decision_request_id is None
        assert legacy.decision_turn_id is None
        assert legacy.decision_request_fingerprint is None

    with factory() as session:
        session.add(
            DecisionRecord(
                kind=DecisionKind.INSIGHT,
                tree={
                    "type": "llm_step",
                    "label": "invalid",
                    "children": [],
                },
                summary="invalid",
                decision_request_id=uuid.uuid4(),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


@pytest.mark.asyncio
async def test_engine_runs_agent_then_finalizer(persistence):
    _engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()
    run = _run(request, [ref])

    class StubAgent:
        def __init__(self):
            self.closed = False

        async def ask(self, received):
            assert received == request
            return run

        def close(self):
            self.closed = True

    agent = StubAgent()
    engine = HealthMesDecisionEngine(
        agent=agent,
        finalizer=_finalizer(factory),
    )

    result = await engine.ask(request)
    await engine.aclose()

    assert result.persistence_status is PersistenceStatus.PERSISTED
    assert agent.closed is True


@pytest.mark.asyncio
async def test_engine_finalization_does_not_block_event_loop(persistence):
    _engine, factory = persistence
    with factory() as session:
        ref = _source_ref(_event(session))
    request = _request()
    run = _run(request, [ref])

    class StubAgent:
        async def ask(self, _received):
            return run

        def close(self):
            return None

    engine = HealthMesDecisionEngine(
        agent=StubAgent(),
        finalizer=_finalizer(factory),
    )
    heartbeat = False

    async def mark_heartbeat() -> None:
        nonlocal heartbeat
        await asyncio.sleep(0.01)
        heartbeat = True

    with activity_write_lock():
        decision_task = asyncio.create_task(engine.ask(request))
        heartbeat_task = asyncio.create_task(mark_heartbeat())
        await asyncio.wait_for(heartbeat_task, timeout=1)
        assert heartbeat is True
        assert decision_task.done() is False

    result = await asyncio.wait_for(decision_task, timeout=2)

    assert result.persistence_status is PersistenceStatus.PERSISTED
