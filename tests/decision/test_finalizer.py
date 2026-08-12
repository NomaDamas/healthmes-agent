from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session, sessionmaker

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
) -> DecisionAgentRun:
    query = query or _query()
    result = ContextResult(
        query_id=query.query_id,
        provider_id=query.provider_id,
        capability=query.capability,
        status=ContextStatus.OK,
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


class FailingCommitSession(Session):
    def commit(self) -> None:
        raise RuntimeError("injected commit failure")


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
    engine.close()

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
