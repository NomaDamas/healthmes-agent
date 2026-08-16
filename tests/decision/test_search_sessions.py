from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from healthmes.decision import (
    AbortedDecisionSearchSessionError,
    ContextAccessLayer,
    ContextAccessPolicy,
    ContextCapability,
    ContextCoverage,
    ContextFreshness,
    ContextProviderMetadata,
    ContextProviderRegistry,
    ContextResult,
    ContextStatus,
    CoverageStatus,
    DecisionBudget,
    DecisionCaller,
    DecisionContextSearchSessionService,
    DecisionRequest,
    DecisionSearchBudgetError,
    DecisionSearchQueryError,
    DecisionSearchSessionState,
    DomainAccessGrant,
    ExecutionScope,
    ExpiredDecisionSearchSessionError,
    FinishedDecisionSearchSessionError,
    FreshnessStatus,
    PrivacyLevel,
    ProvenanceSupport,
    SourceRef,
    ToolCallStatus,
    UnknownDecisionSearchSessionError,
)
from healthmes.store import Base, WellnessEvent, create_db_engine

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)


class MutableClock:
    def __init__(self) -> None:
        self.wall = NOW
        self.monotonic = 100.0

    def now(self) -> datetime:
        return self.wall

    def tick(self) -> float:
        return self.monotonic


class SearchProvider:
    metadata = ContextProviderMetadata(
        provider_id="nutrition-search-test",
        domain="nutrition",
        description="Deterministic provider for decision search session tests.",
        capabilities=(
            ContextCapability(
                capability="nutrition.search-test",
                description="Return bounded test records through the gateway.",
                granularities=("summary", "record"),
                query_fields=(
                    "start",
                    "end",
                    "timezone",
                    "fields",
                    "limit",
                ),
                output_fields=("status", "count", "records", "value"),
                nested_output_fields=(
                    "records",
                    "name",
                    "source_text",
                    "value",
                ),
                identity_fields=("name",),
                raw_fields=("source_text",),
                limit_output_fields=("records",),
                max_lookback_days=90,
                privacy_levels=(
                    PrivacyLevel.AGGREGATE,
                    PrivacyLevel.IDENTITY,
                ),
                sensitivity="nutrition",
                provenance=ProvenanceSupport.PARTIAL,
                freshness_expectation="Test data is current.",
            ),
        ),
    )

    def __init__(self) -> None:
        self.status = ContextStatus.OK
        self.payload_factory = lambda query: {
            "status": "ok",
            "count": query.limit,
            "records": [
                {
                    "name": f"private-{index}",
                    "source_text": f"raw-{index}",
                    "value": index,
                }
                for index in range(query.limit)
            ],
        }
        self.source_refs: list[SourceRef] = []
        self.queries = []

    async def query(self, session, query, *, now):
        del session
        self.queries.append(query)
        return ContextResult(
            query_id=query.query_id,
            provider_id=query.provider_id,
            capability=query.capability,
            status=self.status,
            payload=self.payload_factory(query),
            source_refs=list(self.source_refs),
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


class CommitAttemptProvider(SearchProvider):
    async def query(self, session, query, *, now):
        self.queries.append(query)
        backing_session = session._session
        backing_session.add(
            WellnessEvent(
                event_type="nutrition.read-only-escape.v1",
                schema_version=1,
                observed_at=now,
                recorded_at=now,
                timezone=query.timezone,
                source_provider="read-only-escape",
                source_device=None,
                source_record_id=str(uuid.uuid4()),
                capture_method="test",
                quality_flags={},
                confidence=1,
                coverage=1,
                sensitivity="nutrition",
                consent_scope="personal",
                expires_at=now + timedelta(days=1),
                payload={},
                raw_object_id=None,
                derived_from=None,
            )
        )
        backing_session.commit()
        raise AssertionError("read-only database transaction allowed commit")


class CancellationProvider(SearchProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()

    async def query(self, session, query, *, now):
        del session, now
        self.queries.append(query)
        self.started.set()
        try:
            await self.release.wait()
        finally:
            self.cancelled.set()
        raise AssertionError("cancelled provider call resumed unexpectedly")


class BlockingProvider(SearchProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def query(self, session, query, *, now):
        self.started.set()
        await self.release.wait()
        return await super().query(session, query, now=now)


@pytest.fixture
def store_factory() -> sessionmaker[Session]:
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    yield factory
    engine.dispose()


def _request(
    *,
    budget: DecisionBudget | None = None,
    privacy: PrivacyLevel = PrivacyLevel.AGGREGATE,
) -> DecisionRequest:
    return DecisionRequest(
        question="Search the relevant retained health context.",
        requested_at=NOW,
        timezone="UTC",
        caller=DecisionCaller(
            principal_id="owner",
            authenticated=True,
            execution_scope=ExecutionScope.LOCAL,
        ),
        requested_privacy_level=privacy,
        budget=budget or DecisionBudget(),
    )


def _policy(
    *,
    enabled: bool = True,
    privacy: PrivacyLevel = PrivacyLevel.AGGREGATE,
    max_rows: int = 250,
) -> ContextAccessPolicy:
    return ContextAccessPolicy(
        owner_principal_id="owner",
        grants=(
            DomainAccessGrant(
                domain="nutrition",
                enabled=enabled,
                max_privacy_level=privacy,
                execution_scopes=(ExecutionScope.LOCAL,),
                consent_scopes=("personal",),
            ),
        ),
        max_rows_per_query=max_rows,
        allow_external_provenance=False,
    )


def _service(
    store_factory: sessionmaker[Session],
    provider: SearchProvider,
    policy_holder: list[ContextAccessPolicy],
    clock: MutableClock,
    *,
    ttl_seconds: float = 60,
    max_active_sessions: int = 32,
) -> DecisionContextSearchSessionService:
    return DecisionContextSearchSessionService(
        access_layer=ContextAccessLayer(
            ContextProviderRegistry((provider,)),
            clock=clock.now,
        ),
        session_factory=store_factory,
        policy_resolver=lambda _request: policy_holder[0],
        ttl_seconds=ttl_seconds,
        max_active_sessions=max_active_sessions,
        clock=clock.now,
        monotonic_clock=clock.tick,
    )


def _seed_nutrition_source_ref(
    store_factory: sessionmaker[Session],
    *,
    observed_at: datetime,
    expires_at: datetime | None = None,
) -> SourceRef:
    interaction_id = uuid.uuid4()
    event = WellnessEvent(
        event_type="nutrition.interaction.v1",
        schema_version=1,
        observed_at=observed_at,
        recorded_at=observed_at + timedelta(minutes=1),
        timezone="UTC",
        source_provider="nutrition-interaction",
        source_device=None,
        source_record_id=str(interaction_id),
        capture_method="test",
        quality_flags={},
        confidence=1,
        coverage=1,
        sensitivity="nutrition",
        consent_scope="personal",
        expires_at=expires_at,
        payload={
            "interaction_id": str(interaction_id),
            "observed_at": observed_at.isoformat(),
        },
        raw_object_id=None,
        derived_from=None,
    )
    with store_factory() as session:
        session.add(event)
        session.commit()
        event_id = event.id
    return SourceRef(
        domain="nutrition",
        resource_type=event.event_type,
        record_id=str(event_id),
        source_provider=event.source_provider,
        observed_start=observed_at,
        collected_at=event.recorded_at,
        derived_by="nutrition.intake-history.v1",
        freshness=FreshnessStatus.STALE if expires_at else FreshnessStatus.CURRENT,
        coverage=1,
        sensitivity="nutrition",
    )


async def _search(
    service: DecisionContextSearchSessionService,
    session_id: str,
    **overrides: Any,
):
    arguments = {
        "domain": "nutrition",
        "capability": "nutrition.search-test",
        "start": NOW - timedelta(days=1),
        "end": NOW,
        "granularity": "summary",
        "limit": 1,
    }
    arguments.update(overrides)
    return await service.search(session_id, **arguments)


async def test_repeated_calls_share_one_context_access_budget(
    store_factory,
) -> None:
    clock = MutableClock()
    provider = SearchProvider()
    service = _service(
        store_factory,
        provider,
        [_policy()],
        clock,
    )
    handle = service.begin(
        _request(
            budget=DecisionBudget(
                max_tool_calls=1,
                max_steps=1,
            )
        )
    )

    first = await _search(service, handle.session_id)
    with pytest.raises(
        DecisionSearchBudgetError,
        match="decision_search_tool_call_budget_exhausted",
    ):
        await _search(
            service,
            handle.session_id,
            fields=("count",),
        )

    assert first.status is ContextStatus.PARTIAL
    assert first.access_audit.budget.tool_calls_used == 1
    snapshot = service.inspect(handle.session_id)
    assert len(snapshot.tool_trace) == 1
    assert snapshot.budget.tool_calls_used == 1
    finished = service.finish(handle.session_id)
    assert finished.state is DecisionSearchSessionState.FINISHED


def test_unknown_and_expired_sessions_are_distinct(
    store_factory,
) -> None:
    clock = MutableClock()
    service = _service(
        store_factory,
        SearchProvider(),
        [_policy()],
        clock,
        ttl_seconds=5,
    )

    with pytest.raises(UnknownDecisionSearchSessionError):
        service.inspect("dss_" + "x" * 43)

    handle = service.begin(_request())
    clock.monotonic += 6
    clock.wall += timedelta(seconds=6)
    with pytest.raises(ExpiredDecisionSearchSessionError):
        service.inspect(handle.session_id)


async def test_in_flight_expiry_preserves_failure_audit_snapshot(
    store_factory,
) -> None:
    provider = BlockingProvider()
    service = DecisionContextSearchSessionService(
        access_layer=ContextAccessLayer(
            ContextProviderRegistry((provider,)),
            clock=lambda: datetime.now(UTC),
        ),
        session_factory=store_factory,
        policy_resolver=lambda _request: _policy(),
        ttl_seconds=0.02,
    )
    handle = service.begin(_request())

    with pytest.raises(ExpiredDecisionSearchSessionError):
        await _search(service, handle.session_id)

    snapshot = service.inspect(handle.session_id)
    assert snapshot.state is DecisionSearchSessionState.EXPIRED
    assert snapshot.budget.tool_calls_used == 1
    assert len(snapshot.tool_trace) == 1
    assert snapshot.tool_trace[0].status is ToolCallStatus.FAILED
    assert (
        snapshot.tool_trace[0].error_code
        == "decision_search_session_expired"
    )
    assert len(snapshot.access_trace) == 1
    assert snapshot.access_trace[0].reason_codes == (
        "decision_search_session_expired",
    )
    assert (
        snapshot.access_trace[0].query_id
        == snapshot.tool_trace[0].query.query_id
    )

    finished = service.finish(handle.session_id)
    assert finished.tool_trace == snapshot.tool_trace
    assert finished.access_trace == snapshot.access_trace
    with pytest.raises(ExpiredDecisionSearchSessionError):
        service.inspect(handle.session_id)


def test_finished_aborted_and_closed_sessions_fail_closed(
    store_factory,
) -> None:
    clock = MutableClock()
    service = _service(
        store_factory,
        SearchProvider(),
        [_policy()],
        clock,
    )

    finished_handle = service.begin(_request())
    assert (
        service.finish(finished_handle.session_id).state
        is DecisionSearchSessionState.FINISHED
    )
    with pytest.raises(FinishedDecisionSearchSessionError):
        service.inspect(finished_handle.session_id)

    aborted_handle = service.begin(_request())
    assert (
        service.abort(aborted_handle.session_id).state
        is DecisionSearchSessionState.ABORTED
    )
    with pytest.raises(AbortedDecisionSearchSessionError):
        service.inspect(aborted_handle.session_id)

    active_handle = service.begin(_request())
    service.close()
    with pytest.raises(AbortedDecisionSearchSessionError):
        service.inspect(active_handle.session_id)
    with pytest.raises(AbortedDecisionSearchSessionError):
        service.begin(_request())


async def test_consent_and_privacy_remain_gateway_enforced(
    store_factory,
) -> None:
    clock = MutableClock()
    provider = SearchProvider()
    policy_holder = [_policy()]
    service = _service(
        store_factory,
        provider,
        policy_holder,
        clock,
    )
    handle = service.begin(
        _request(privacy=PrivacyLevel.IDENTITY)
    )

    aggregate = await _search(service, handle.session_id)
    serialized = aggregate.model_dump_json()
    assert aggregate.status is ContextStatus.PARTIAL
    assert "private-0" not in serialized
    assert "raw-0" not in serialized
    assert "privacy_fields_redacted" in aggregate.limitations

    identity = await _search(
        service,
        handle.session_id,
        privacy_level=PrivacyLevel.IDENTITY,
        fields=("records",),
    )
    assert identity.status is ContextStatus.DENIED
    assert identity.limitations == ["domain_privacy_consent_denied"]

    policy_holder[0] = _policy(enabled=False)
    denied = await _search(
        service,
        handle.session_id,
        fields=("value",),
    )
    assert denied.status is ContextStatus.DENIED
    assert denied.limitations == ["domain_consent_denied"]


async def test_disabled_provider_is_an_audited_gateway_denial(
    store_factory,
) -> None:
    clock = MutableClock()
    provider = SearchProvider()
    service = _service(
        store_factory,
        provider,
        [_policy()],
        clock,
    )
    handle = service.begin(_request())
    service.access_layer.registry.set_enabled(
        provider.metadata.provider_id,
        enabled=False,
    )

    result = await _search(service, handle.session_id)

    assert result.status is ContextStatus.DENIED
    assert result.limitations == ["provider_disabled"]
    assert result.access_audit.reason_codes == ("provider_disabled",)
    assert result.access_audit.budget.tool_calls_used == 1
    assert provider.queries == []


async def test_range_limit_and_encoded_byte_bounds_use_access_layer(
    store_factory,
) -> None:
    clock = MutableClock()
    provider = SearchProvider()
    service = _service(
        store_factory,
        provider,
        [_policy(max_rows=2)],
        clock,
    )
    handle = service.begin(_request())

    bounded = await _search(
        service,
        handle.session_id,
        start=NOW - timedelta(days=90),
        limit=5,
    )
    assert len(bounded.payload["records"]) == 2
    assert bounded.access_audit.effective_limit == 2
    assert "query_limit_trimmed" in bounded.limitations

    with pytest.raises(DecisionSearchQueryError):
        await _search(
            service,
            handle.session_id,
            start=NOW - timedelta(days=91),
        )

    provider.payload_factory = lambda _query: {
        "status": "ok",
        "value": "x" * 2_000,
        "records": [],
    }
    byte_limited_handle = service.begin(
        _request(
            budget=DecisionBudget(max_context_bytes=4_096),
        )
    )
    with pytest.raises(
        DecisionSearchBudgetError,
        match="decision_search_context_byte_budget_exhausted",
    ):
        await _search(
            service,
            byte_limited_handle.session_id,
            fields=("value",),
        )
    byte_snapshot = service.inspect(byte_limited_handle.session_id)
    assert byte_snapshot.tool_trace[-1].status is ToolCallStatus.FAILED
    assert (
        byte_snapshot.tool_trace[-1].error_code
        == "turn_context_byte_budget_exhausted"
    )
    assert len(byte_snapshot.access_trace) == 1
    assert (
        byte_snapshot.access_trace[0].query_id
        == byte_snapshot.tool_trace[-1].query.query_id
    )
    assert byte_snapshot.access_trace[0].payload_bytes > 0
    assert len(
        json.dumps(
            byte_snapshot.model_dump(mode="json", round_trip=True),
            separators=(",", ":"),
        ).encode()
    ) <= byte_snapshot.budget.context_bytes_limit


async def test_snapshot_preserves_server_canonical_query_and_effective_query(
    store_factory,
) -> None:
    clock = MutableClock()
    provider = SearchProvider()
    service = _service(
        store_factory,
        provider,
        [_policy(max_rows=2)],
        clock,
    )
    handle = service.begin(_request())

    await _search(
        service,
        handle.session_id,
        start=NOW - timedelta(days=90),
        limit=5,
        fields=("records",),
        parameters={},
    )

    trace = service.inspect(handle.session_id).tool_trace
    assert len(trace) == 1
    assert trace[0].query.limit == 5
    assert trace[0].query.fields == ["records"]
    assert trace[0].query.timezone == "UTC"
    assert trace[0].effective_query is not None
    assert trace[0].effective_query.limit == 2
    assert trace[0].effective_query.parameters == {}


async def test_source_ref_budget_is_shared_and_snapshot_order_is_stable(
    store_factory,
) -> None:
    clock = MutableClock()
    provider = SearchProvider()
    first_refs = [
        _seed_nutrition_source_ref(
            store_factory,
            observed_at=NOW - timedelta(hours=offset),
        )
        for offset in (2, 3)
    ]
    provider.source_refs = list(reversed(first_refs))
    service = _service(
        store_factory,
        provider,
        [_policy()],
        clock,
    )
    handle = service.begin(
        _request(
            budget=DecisionBudget(max_source_refs=2),
        )
    )

    first = await _search(service, handle.session_id)
    assert first.access_audit.budget.source_refs_used == 2
    snapshot = service.inspect(handle.session_id)
    assert [ref.reference_id for ref in snapshot.source_refs] == sorted(
        ref.reference_id for ref in first_refs
    )

    provider.source_refs = [
        _seed_nutrition_source_ref(
            store_factory,
            observed_at=NOW - timedelta(hours=4),
        )
    ]
    exhausted = await _search(
        service,
        handle.session_id,
        fields=("value",),
    )

    assert exhausted.status is ContextStatus.DENIED
    assert exhausted.limitations == [
        "turn_source_ref_budget_exhausted"
    ]
    assert exhausted.source_refs == []
    assert exhausted.access_audit.budget.source_refs_used == 2


async def test_empty_unavailable_denied_and_partial_remain_distinct(
    store_factory,
) -> None:
    clock = MutableClock()
    provider = SearchProvider()
    service = _service(
        store_factory,
        provider,
        [_policy()],
        clock,
    )
    handle = service.begin(_request())

    provider.payload_factory = lambda _query: {}
    provider.status = ContextStatus.UNAVAILABLE
    unavailable = await _search(service, handle.session_id)
    assert unavailable.status is ContextStatus.UNAVAILABLE
    assert unavailable.payload == {}

    provider.status = ContextStatus.DENIED
    denied = await _search(
        service,
        handle.session_id,
        fields=("count",),
    )
    assert denied.status is ContextStatus.DENIED
    assert denied.payload == {}

    provider.payload_factory = lambda _query: {
        "status": "ok",
        "value": 1,
    }
    provider.status = ContextStatus.PARTIAL
    partial = await _search(
        service,
        handle.session_id,
        fields=("value",),
    )
    assert partial.status is ContextStatus.PARTIAL

    provider.status = ContextStatus.PARTIAL
    provider.payload_factory = lambda _query: {
        "status": "insufficient_data",
    }
    empty = await _search(
        service,
        handle.session_id,
        fields=("status",),
    )
    assert empty.status is ContextStatus.PARTIAL
    assert empty.payload == {"status": "insufficient_data"}
    assert "source_refs_unavailable" in empty.limitations


async def test_expired_source_refs_are_denied_and_search_does_not_mutate(
    store_factory,
) -> None:
    clock = MutableClock()
    provider = SearchProvider()
    provider.source_refs = [
        _seed_nutrition_source_ref(
            store_factory,
            observed_at=NOW - timedelta(hours=2),
            expires_at=NOW - timedelta(minutes=1),
        )
    ]
    service = _service(
        store_factory,
        provider,
        [_policy()],
        clock,
    )
    handle = service.begin(_request())
    with store_factory() as session:
        before = session.scalar(
            select(func.count()).select_from(WellnessEvent)
        )

    result = await _search(service, handle.session_id)

    assert result.status is ContextStatus.DENIED
    assert "source_ref_expired" in result.limitations
    assert result.source_refs == []
    with store_factory() as session:
        after = session.scalar(
            select(func.count()).select_from(WellnessEvent)
        )
    assert after == before


async def test_database_read_only_transaction_blocks_provider_commit(
    store_factory,
) -> None:
    clock = MutableClock()
    provider = CommitAttemptProvider()
    service = _service(
        store_factory,
        provider,
        [_policy()],
        clock,
    )
    handle = service.begin(_request())

    result = await _search(service, handle.session_id)

    assert result.status is ContextStatus.FAILED
    assert result.limitations == ["provider_execution_failed"]
    with store_factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(WellnessEvent)
            )
            == 0
        )


async def test_cancelled_tool_call_is_audited_and_cannot_finish_normally(
    store_factory,
) -> None:
    clock = MutableClock()
    provider = CancellationProvider()
    service = _service(
        store_factory,
        provider,
        [_policy()],
        clock,
    )
    handle = service.begin(_request())
    task = asyncio.create_task(
        _search(service, handle.session_id)
    )
    await provider.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await provider.cancelled.wait()

    snapshot = service.inspect(handle.session_id)
    assert snapshot.state is DecisionSearchSessionState.ABORTED
    assert snapshot.budget.tool_calls_used == 1
    assert len(snapshot.tool_trace) == 1
    assert snapshot.tool_trace[0].status is ToolCallStatus.FAILED
    assert (
        snapshot.tool_trace[0].error_code
        == "decision_search_tool_call_cancelled"
    )
    assert len(snapshot.access_trace) == 1
    assert snapshot.access_trace[0].reason_codes == (
        "decision_search_tool_call_cancelled",
    )

    with pytest.raises(AbortedDecisionSearchSessionError):
        await _search(
            service,
            handle.session_id,
            fields=("count",),
        )

    finished = service.finish(handle.session_id)
    assert finished.state is DecisionSearchSessionState.ABORTED
    assert finished.tool_trace == snapshot.tool_trace
    assert finished.access_trace == snapshot.access_trace
    with pytest.raises(AbortedDecisionSearchSessionError):
        service.inspect(handle.session_id)


async def test_explicit_abort_preserves_in_flight_tool_and_access_trace(
    store_factory,
) -> None:
    clock = MutableClock()
    provider = BlockingProvider()
    service = _service(
        store_factory,
        provider,
        [_policy()],
        clock,
    )
    handle = service.begin(_request())
    task = asyncio.create_task(
        _search(service, handle.session_id)
    )
    await provider.started.wait()

    aborted = service.abort(handle.session_id)
    assert aborted.state is DecisionSearchSessionState.ABORTED
    provider.release.set()
    with pytest.raises(AbortedDecisionSearchSessionError):
        await task

    snapshot = service.inspect(handle.session_id)
    assert snapshot.state is DecisionSearchSessionState.ABORTED
    assert snapshot.budget.tool_calls_used == 1
    assert len(snapshot.tool_trace) == 1
    assert snapshot.tool_trace[0].status is ToolCallStatus.FAILED
    assert (
        snapshot.tool_trace[0].error_code
        == "decision_search_session_aborted"
    )
    assert len(snapshot.access_trace) == 1
    assert snapshot.access_trace[0].reason_codes == (
        "decision_search_session_aborted",
    )
    assert (
        snapshot.access_trace[0].query_id
        == snapshot.tool_trace[0].query.query_id
    )

    finished = service.finish(handle.session_id)
    assert finished.state is DecisionSearchSessionState.ABORTED
    assert finished.tool_trace == snapshot.tool_trace
    assert finished.access_trace == snapshot.access_trace
    with pytest.raises(AbortedDecisionSearchSessionError):
        service.inspect(handle.session_id)


async def test_explicit_abort_releases_session_capacity_after_unwind(
    store_factory,
) -> None:
    clock = MutableClock()
    provider = BlockingProvider()
    service = _service(
        store_factory,
        provider,
        [_policy()],
        clock,
        max_active_sessions=1,
    )
    handle = service.begin(_request())
    task = asyncio.create_task(
        _search(service, handle.session_id)
    )
    await provider.started.wait()

    service.abort(handle.session_id)
    provider.release.set()
    with pytest.raises(AbortedDecisionSearchSessionError):
        await task

    replacement = service.begin(_request())
    assert replacement.session_id != handle.session_id
    service.abort(replacement.session_id)


async def test_explicit_abort_wins_when_provider_reaches_session_timeout(
    store_factory,
) -> None:
    provider = BlockingProvider()
    service = DecisionContextSearchSessionService(
        access_layer=ContextAccessLayer(
            ContextProviderRegistry((provider,)),
            clock=lambda: datetime.now(UTC),
        ),
        session_factory=store_factory,
        policy_resolver=lambda _request: _policy(),
        ttl_seconds=0.02,
    )
    handle = service.begin(_request())
    task = asyncio.create_task(
        _search(service, handle.session_id)
    )
    await provider.started.wait()

    service.abort(handle.session_id)
    with pytest.raises(AbortedDecisionSearchSessionError):
        await task

    snapshot = service.inspect(handle.session_id)
    assert snapshot.state is DecisionSearchSessionState.ABORTED
    assert len(snapshot.tool_trace) == 1
    assert (
        snapshot.tool_trace[0].error_code
        == "decision_search_session_aborted"
    )
    assert len(snapshot.access_trace) == 1
    assert snapshot.access_trace[0].reason_codes == (
        "decision_search_session_aborted",
    )

    finished = service.finish(handle.session_id)
    assert finished.state is DecisionSearchSessionState.ABORTED
    with pytest.raises(AbortedDecisionSearchSessionError):
        service.inspect(handle.session_id)


async def test_abort_wins_race_with_result_snapshot_commit(
    store_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MutableClock()
    service = _service(
        store_factory,
        SearchProvider(),
        [_policy()],
        clock,
    )
    handle = service.begin(_request())
    original_project = service._project_accepted_result

    def abort_after_projection(record, *, audit, tool_record):
        projected = original_project(
            record,
            audit=audit,
            tool_record=tool_record,
        )
        service.abort(record.session_id)
        return projected

    monkeypatch.setattr(
        service,
        "_project_accepted_result",
        abort_after_projection,
    )

    with pytest.raises(AbortedDecisionSearchSessionError):
        await _search(service, handle.session_id)

    snapshot = service.inspect(handle.session_id)
    assert snapshot.state is DecisionSearchSessionState.ABORTED
    assert len(snapshot.tool_trace) == 1
    assert snapshot.tool_trace[0].status is ToolCallStatus.FAILED
    assert snapshot.tool_trace[0].result is None
    assert (
        snapshot.tool_trace[0].error_code
        == "decision_search_session_aborted"
    )
    assert len(snapshot.access_trace) == 1
    assert (
        snapshot.access_trace[0].query_id
        == snapshot.tool_trace[0].query.query_id
    )
