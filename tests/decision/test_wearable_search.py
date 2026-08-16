from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from healthmes.decision.access import ContextAccessLayer
from healthmes.decision.contracts import (
    ContextStatus,
    DecisionCaller,
    DecisionRequest,
    ExecutionScope,
)
from healthmes.decision.domain_providers import WearableContextProvider
from healthmes.decision.policy import (
    DatabaseDecisionPolicyResolver,
    ensure_decision_domain_policies,
    update_decision_domain_policy,
)
from healthmes.decision.providers import ContextProviderRegistry
from healthmes.decision.search import (
    DecisionContextSearchSessionService,
)
from healthmes.store import Base, WellnessEvent, create_db_engine
from healthmes.wearables.provenance import (
    OPEN_WEARABLES_OBSERVATION_EVENT_TYPE,
    OPEN_WEARABLES_QUERY_EVENT_TYPE,
)
from healthmes.wearables.search import (
    WearableSearchFetch,
    WearableSearchRequest,
)

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)
DETAIL_START = datetime(2026, 8, 16, 8, tzinfo=UTC)
DETAIL_END = DETAIL_START + timedelta(hours=1)


def _request() -> DecisionRequest:
    return DecisionRequest(
        question="Use the wearable context needed for this decision.",
        requested_at=NOW,
        timezone="UTC",
        caller=DecisionCaller(
            principal_id="owner",
            authenticated=True,
            execution_scope=ExecutionScope.LOCAL,
        ),
    )


def _file_store(tmp_path, name: str):
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / name}"
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    with factory() as session:
        ensure_decision_domain_policies(session, "owner")
        session.commit()
    return engine, factory


def _service(
    factory: sessionmaker[Session],
    provider: WearableContextProvider,
    *,
    clock: Callable[[], datetime] = lambda: NOW,
) -> DecisionContextSearchSessionService:
    return DecisionContextSearchSessionService(
        access_layer=ContextAccessLayer(
            ContextProviderRegistry((provider,)),
            clock=clock,
        ),
        session_factory=factory,
        policy_resolver=DatabaseDecisionPolicyResolver(
            session_factory=factory,
            owner_principal_id="owner",
            execution_scope=ExecutionScope.LOCAL,
        ),
        clock=clock,
    )


async def test_file_backed_first_use_daily_snapshot_survives_read_only_search(
    tmp_path,
) -> None:
    engine, factory = _file_store(tmp_path, "daily-first-use.db")
    calls: list[date] = []

    async def reader(day: date) -> dict:
        calls.append(day)
        return {
            "status": "ok",
            "date": day.isoformat(),
            "stress": {
                "status": "ok",
                "value": 38,
                "recorded_at": "2026-08-16T08:00:00+00:00",
            },
            "freshness": {
                "recorded_at": "2026-08-16T08:00:00+00:00",
                "status": "current",
            },
            "coverage": {"ratio": 1.0},
            "limitations": [],
        }

    service = _service(
        factory,
        WearableContextProvider(
            reader,
            snapshot_session_factory=factory,
        ),
    )
    try:
        handle = service.begin(_request())
        result = await service.search(
            handle.session_id,
            domain="wearable",
            capability="wearable.stress",
            parameters={"date": "2026-08-16"},
        )
        snapshot = service.finish(handle.session_id)

        assert calls == [date(2026, 8, 16)]
        assert result.status is ContextStatus.PARTIAL
        assert result.payload["stress"]["value"] == 38
        assert "wearable_snapshot_persistence_failed" not in result.limitations
        assert "wearable_snapshot_writer_unavailable" not in result.limitations
        assert len(result.source_refs) == 1
        source_ref = result.source_refs[0]
        assert (
            source_ref.resource_type
            == OPEN_WEARABLES_OBSERVATION_EVENT_TYPE
        )
        assert source_ref.content_digest is not None
        assert snapshot.source_refs == (source_ref,)
        with factory() as observer:
            event = observer.get(
                WellnessEvent,
                UUID(source_ref.record_id),
            )
            assert event is not None
            assert event.event_type == OPEN_WEARABLES_OBSERVATION_EVENT_TYPE
    finally:
        service.close()
        engine.dispose()


async def test_file_backed_detail_search_uses_stable_mirror_ref_and_cursor(
    tmp_path,
) -> None:
    engine, factory = _file_store(tmp_path, "detail-first-use.db")
    calls: list[WearableSearchRequest] = []

    async def search_reader(
        request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        calls.append(request)
        return WearableSearchFetch(
            records=(
                {
                    "timestamp": "2026-08-16T08:05:00+00:00",
                    "series_type": "heart_rate",
                    "value": 72,
                    "unit": "bpm",
                },
                {
                    "timestamp": "2026-08-16T08:10:00+00:00",
                    "series_type": "heart_rate",
                    "value": 76,
                    "unit": "bpm",
                },
            )
        )

    service = _service(
        factory,
        WearableContextProvider(
            search_reader=search_reader,
            snapshot_session_factory=factory,
        ),
    )
    try:
        handle = service.begin(_request())
        first = await service.search(
            handle.session_id,
            domain="wearable",
            capability="wearable.timeseries",
            start=DETAIL_START,
            end=DETAIL_END,
            granularity="series",
            limit=1,
            parameters={
                "series_type": "heart_rate",
                "resolution": "1min",
            },
        )
        assert first.next_cursor is not None
        second = await service.search(
            handle.session_id,
            domain="wearable",
            capability="wearable.timeseries",
            start=DETAIL_START,
            end=DETAIL_END,
            granularity="series",
            limit=1,
            parameters={
                "series_type": "heart_rate",
                "resolution": "1min",
                "cursor": first.next_cursor,
            },
        )
        finished = service.finish(handle.session_id)

        assert len(calls) == 1
        assert first.payload["provenance_mode"] == (
            "live_upstream_mirrored"
        )
        assert second.payload["provenance_mode"] == (
            "retained_local_mirror"
        )
        assert first.payload["records"][0]["value"] == 72
        assert second.payload["records"][0]["value"] == 76
        assert first.source_refs == second.source_refs
        assert len(first.source_refs) == 1
        source_ref = first.source_refs[0]
        assert source_ref.resource_type == OPEN_WEARABLES_QUERY_EVENT_TYPE
        assert source_ref.content_digest is not None
        assert source_ref.observed_start == DETAIL_START
        assert source_ref.observed_end == DETAIL_END
        assert finished.source_refs == (source_ref,)
        with factory() as observer:
            event = observer.get(
                WellnessEvent,
                UUID(source_ref.record_id),
            )
            assert event is not None
            assert event.event_type == OPEN_WEARABLES_QUERY_EVENT_TYPE
            assert event.payload["query"]["parameters"] == {
                "series_type": "heart_rate",
                "resolution": "1min",
            }
            assert observer.scalar(
                select(func.count())
                .select_from(WellnessEvent)
                .where(
                    WellnessEvent.event_type
                    == OPEN_WEARABLES_QUERY_EVENT_TYPE
                )
            ) == 1
    finally:
        service.close()
        engine.dispose()


async def test_detail_search_falls_back_to_exact_retained_query(
    tmp_path,
) -> None:
    engine, factory = _file_store(tmp_path, "detail-fallback.db")
    first_calls = 0

    async def initial_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        nonlocal first_calls
        first_calls += 1
        return WearableSearchFetch(
            records=(
                {
                    "category": "recovery",
                    "recorded_at": "2026-08-16T08:00:00+00:00",
                    "value": 82,
                },
            )
        )

    first_service = _service(
        factory,
        WearableContextProvider(
            search_reader=initial_reader,
            snapshot_session_factory=factory,
        ),
    )
    handle = first_service.begin(_request())
    initial = await first_service.search(
        handle.session_id,
        domain="wearable",
        capability="wearable.health-scores",
        start=DETAIL_START,
        end=DETAIL_END,
        granularity="record",
        parameters={"category": "recovery"},
    )
    first_service.finish(handle.session_id)
    first_service.close()

    fallback_calls = 0

    async def unavailable_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        nonlocal fallback_calls
        fallback_calls += 1
        raise RuntimeError("upstream unavailable")

    fallback_service = _service(
        factory,
        WearableContextProvider(
            search_reader=unavailable_reader,
            snapshot_session_factory=factory,
        ),
    )
    try:
        handle = fallback_service.begin(_request())
        fallback = await fallback_service.search(
            handle.session_id,
            domain="wearable",
            capability="wearable.health-scores",
            start=DETAIL_START,
            end=DETAIL_END,
            granularity="record",
            parameters={"category": "recovery"},
        )

        assert first_calls == 1
        assert fallback_calls == 1
        assert fallback.payload["provenance_mode"] == (
            "retained_local_mirror"
        )
        assert fallback.payload["records"] == initial.payload["records"]
        assert fallback.source_refs == initial.source_refs
        assert "open_wearables_detail_unavailable" in fallback.limitations
        assert (
            "wearable_query_snapshot_fallback_used"
            in fallback.limitations
        )
    finally:
        fallback_service.close()
        engine.dispose()


async def test_wearable_consent_denial_happens_before_upstream_access(
    tmp_path,
) -> None:
    engine, factory = _file_store(tmp_path, "consent-denied.db")
    with factory() as session:
        update_decision_domain_policy(
            session,
            "owner",
            "wearable",
            enabled=False,
        )
        session.commit()
    calls = 0

    async def forbidden_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        nonlocal calls
        calls += 1
        raise AssertionError("upstream must not run without consent")

    service = _service(
        factory,
        WearableContextProvider(
            search_reader=forbidden_reader,
            snapshot_session_factory=factory,
        ),
    )
    try:
        handle = service.begin(_request())
        result = await service.search(
            handle.session_id,
            domain="wearable",
            capability="wearable.workouts",
            start=DETAIL_START,
            end=DETAIL_END,
            granularity="record",
        )

        assert result.status is ContextStatus.DENIED
        assert result.limitations == ["domain_consent_denied"]
        assert result.source_refs == []
        assert calls == 0
        with factory() as observer:
            assert observer.scalar(
                select(func.count())
                .select_from(WellnessEvent)
                .where(
                    WellnessEvent.event_type
                    == OPEN_WEARABLES_QUERY_EVENT_TYPE
                )
            ) == 0
    finally:
        service.close()
        engine.dispose()
