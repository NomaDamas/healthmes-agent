from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

import healthmes.decision.domain_providers as domain_providers
from healthmes.decision.access import (
    ContextAccessLayer,
    ContextAccessPolicy,
    DomainAccessGrant,
)
from healthmes.decision.contracts import (
    ContextQuery,
    ContextStatus,
    CoverageStatus,
    DecisionCaller,
    DecisionRequest,
    ExecutionScope,
)
from healthmes.decision.domain_providers import (
    InvalidContextCursorError,
    WearableContextProvider,
)
from healthmes.decision.policy import (
    DatabaseDecisionPolicyResolver,
    ensure_decision_domain_policies,
    update_decision_domain_policy,
)
from healthmes.decision.providers import ContextProviderRegistry
from healthmes.decision.search import (
    DecisionContextSearchSessionService,
)
from healthmes.storage import update_retention_policy
from healthmes.store import (
    Base,
    RetentionPolicy,
    WellnessEvent,
    create_db_engine,
)
from healthmes.wearables.provenance import (
    OPEN_WEARABLES_OBSERVATION_EVENT_TYPE,
    OPEN_WEARABLES_QUERY_EVENT_TYPE,
    OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
    open_wearables_retention_policy_binding,
    persist_open_wearables_query_snapshot,
    retained_open_wearables_query_snapshots,
    wearable_query_snapshot_from_event,
)
from healthmes.wearables.search import (
    BoundedOpenWearablesSearch,
    WearableSearchFetch,
    WearableSearchRequest,
)

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)
DETAIL_START = datetime(2026, 8, 16, 8, tzinfo=UTC)
DETAIL_END = DETAIL_START + timedelta(hours=1)
DETAIL_DATE_CASES = (
    (
        "wearable.health-scores",
        "record",
        {"category": "stress"},
    ),
    (
        "wearable.summaries",
        "summary",
        {"summary_kind": "activity"},
    ),
    ("wearable.workouts", "record", {}),
    (
        "wearable.timeseries",
        "series",
        {
            "series_type": "heart_rate",
            "resolution": "1hour",
        },
    ),
)
FALLBACK_CURSOR_CASES = (
    (
        "wearable.health-scores",
        "record",
        DETAIL_START,
        DETAIL_END,
        {"category": "stress"},
        (
            {
                "category": "stress",
                "recorded_at": "2026-08-16T08:05:00+00:00",
                "provider": "garmin",
                "value": 41,
            },
            {
                "category": "stress",
                "recorded_at": "2026-08-16T08:10:00+00:00",
                "provider": "garmin",
                "value": 42,
            },
        ),
    ),
    (
        "wearable.summaries",
        "summary",
        datetime(2026, 8, 14, tzinfo=UTC),
        datetime(2026, 8, 16, tzinfo=UTC),
        {"summary_kind": "activity"},
        (
            {
                "summary_kind": "activity",
                "date": "2026-08-14",
                "provider": "apple_health",
                "steps": 7000,
            },
            {
                "summary_kind": "activity",
                "date": "2026-08-15",
                "provider": "apple_health",
                "steps": 8000,
            },
        ),
    ),
    (
        "wearable.workouts",
        "record",
        DETAIL_START,
        DETAIL_END,
        {},
        (
            {
                "workout_type": "walking",
                "start_time": "2026-08-16T08:05:00+00:00",
                "end_time": "2026-08-16T08:15:00+00:00",
                "provider": "apple_health",
                "duration_seconds": 600,
            },
            {
                "workout_type": "walking",
                "start_time": "2026-08-16T08:20:00+00:00",
                "end_time": "2026-08-16T08:30:00+00:00",
                "provider": "apple_health",
                "duration_seconds": 600,
            },
        ),
    ),
    (
        "wearable.timeseries",
        "series",
        DETAIL_START,
        DETAIL_END,
        {
            "series_type": "heart_rate",
            "resolution": "1min",
        },
        (
            {
                "timestamp": "2026-08-16T08:05:00+00:00",
                "series_type": "heart_rate",
                "value": 70,
                "unit": "bpm",
                "provider": "apple_health",
            },
            {
                "timestamp": "2026-08-16T08:10:00+00:00",
                "series_type": "heart_rate",
                "value": 72,
                "unit": "bpm",
                "provider": "apple_health",
            },
        ),
    ),
)


def _detail_timeseries_fetch() -> WearableSearchFetch:
    return WearableSearchFetch(
        records=(
            {
                "timestamp": "2026-08-16T08:05:00+00:00",
                "series_type": "heart_rate",
                "value": 72,
                "unit": "bpm",
                "provider": "apple_health",
            },
            {
                "timestamp": "2026-08-16T08:10:00+00:00",
                "series_type": "heart_rate",
                "value": 76,
                "unit": "bpm",
                "provider": "apple_health",
            },
        ),
    )


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


def _turn(provider: WearableContextProvider):
    return ContextAccessLayer(
        ContextProviderRegistry((provider,)),
        clock=lambda: NOW,
    ).start_turn(
        _request(),
        policy=ContextAccessPolicy(
            owner_principal_id="owner",
            grants=(DomainAccessGrant(domain="wearable"),),
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


def _legacy_health_score_cursor(
    session: Session,
    *,
    query: ContextQuery,
    snapshot_event_id: UUID,
    record_index: int = 0,
) -> str:
    event = session.get(WellnessEvent, snapshot_event_id)
    assert event is not None
    snapshot = wearable_query_snapshot_from_event(
        session,
        event,
        now=NOW,
    )
    assert snapshot is not None
    stored = dict(snapshot.result)
    retained_after, retention_window = (
        domain_providers._stored_wearable_retention_window(
            stored,
            snapshot=snapshot,
            expected_retention_policy=(
                open_wearables_retention_policy_binding(session)
            ),
        )
    )
    raw_records = stored.get("records")
    public_records = [
        domain_providers._normalized_public_wearable_record(record)
        for record in (
            raw_records if isinstance(raw_records, list) else []
        )
        if isinstance(record, dict)
    ]
    normalized = (
        domain_providers.normalize_retained_wearable_health_scores(
            public_records,
            category=str(query.parameters["category"]),
            start=snapshot.start,
            end=snapshot.end,
            retained_after=retained_after,
        )
    )
    records = list(normalized.records)
    record = records[record_index]
    digest = domain_providers._canonical_digest(record)
    occurrence = sum(
        domain_providers._canonical_digest(previous) == digest
        for previous in records[:record_index]
    )
    return domain_providers._opaque_cursor(
        query.capability,
        scope=domain_providers._wearable_cursor_scope(
            query,
            snapshot=snapshot,
            retention_window=retention_window,
            records=records,
            stored=stored,
        ),
        identity={
            "record_digest": digest,
            "occurrence": occurrence,
        },
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


async def test_daily_context_timeout_uses_retained_snapshot(
    tmp_path,
) -> None:
    engine, factory = _file_store(tmp_path, "daily-timeout.db")

    async def initial_reader(day: date) -> dict:
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

    initial_service = _service(
        factory,
        WearableContextProvider(
            initial_reader,
            snapshot_session_factory=factory,
        ),
    )
    handle = initial_service.begin(_request())
    initial = await initial_service.search(
        handle.session_id,
        domain="wearable",
        capability="wearable.stress",
        parameters={"date": "2026-08-16"},
    )
    initial_service.finish(handle.session_id)
    initial_service.close()

    async def stalled_reader(_day: date) -> dict:
        await asyncio.sleep(1)
        raise AssertionError("timeout must cancel the upstream context read")

    fallback_service = _service(
        factory,
        WearableContextProvider(
            stalled_reader,
            snapshot_session_factory=factory,
            upstream_timeout_seconds=0.001,
        ),
    )
    try:
        handle = fallback_service.begin(_request())
        fallback = await fallback_service.search(
            handle.session_id,
            domain="wearable",
            capability="wearable.stress",
            parameters={"date": "2026-08-16"},
        )

        assert fallback.status is ContextStatus.PARTIAL
        assert fallback.payload["stress"]["value"] == 38
        assert fallback.source_refs == initial.source_refs
        assert "open_wearables_context_timeout" in fallback.limitations
        assert "wearable_snapshot_fallback_used" in fallback.limitations
    finally:
        fallback_service.close()
        engine.dispose()


@pytest.mark.parametrize(
    ("capability", "granularity", "parameters"),
    DETAIL_DATE_CASES,
)
async def test_detail_date_shorthand_normalizes_before_provider_search(
    session,
    capability: str,
    granularity: str,
    parameters: dict[str, str],
) -> None:
    calls: list[WearableSearchRequest] = []

    async def search_reader(
        request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        calls.append(request)
        return WearableSearchFetch(records=())

    provider = WearableContextProvider(search_reader=search_reader)
    result = await _turn(provider).query(
        session,
        ContextQuery(
            provider_id="wearable",
            capability=capability,
            granularity=granularity,
            parameters={
                "date": "2026-08-16",
                **parameters,
            },
        ),
    )

    assert result.status is not ContextStatus.FAILED
    assert len(calls) == 1
    assert calls[0].start == datetime(2026, 8, 16, tzinfo=UTC)
    assert calls[0].end == NOW + timedelta(seconds=1)
    assert calls[0].parameters == parameters
    event = session.get(
        WellnessEvent,
        UUID(result.source_refs[0].record_id),
    )
    assert event is not None
    assert event.payload["query"]["parameters"] == parameters


async def test_exact_retention_boundary_persists_by_oldest_record(
    session,
) -> None:
    update_retention_policy(
        session,
        OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
        "30d",
        now=NOW,
    )
    calls: list[WearableSearchRequest] = []
    observed_at = NOW - timedelta(hours=1)

    async def search_reader(
        request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        calls.append(request)
        return WearableSearchFetch(
            records=(
                {
                    "category": "stress",
                    "recorded_at": observed_at.isoformat(),
                    "provider": "garmin",
                    "value": 42,
                },
            )
        )

    provider = WearableContextProvider(search_reader=search_reader)
    result = await provider.query(
        session,
        ContextQuery(
            provider_id="wearable",
            capability="wearable.health-scores",
            start=NOW - timedelta(days=30),
            end=NOW,
            granularity="record",
            parameters={"category": "stress"},
        ),
        now=NOW,
    )

    assert result.status is not ContextStatus.FAILED
    assert calls[0].start == NOW - timedelta(days=30)
    assert calls[0].retained_after == NOW - timedelta(days=30)
    assert "wearable_retention_window_trimmed" in result.limitations
    event = session.get(
        WellnessEvent,
        UUID(result.source_refs[0].record_id),
    )
    assert event is not None
    assert event.payload["retention_basis_at"] == observed_at.isoformat()
    assert event.expires_at.replace(tzinfo=UTC) == (
        observed_at + timedelta(days=30)
    )


class RetentionBoundaryTimeseriesClient:
    async def get_timeseries(self, _user_id, *_args, **_kwargs):
        return {
            "data": [
                {
                    "timestamp": "2026-08-09T12:30:00Z",
                    "type": "steps",
                    "value": 10,
                    "unit": "count",
                    "provider": "apple",
                    "data_source_id": "trusted-sensor",
                }
            ],
            "pagination": {
                "next_cursor": None,
                "has_more": False,
            },
        }


async def test_retention_cutoff_timeseries_bucket_remains_persistable(
    session,
) -> None:
    update_retention_policy(
        session,
        OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
        "7d",
        now=NOW,
    )
    search = BoundedOpenWearablesSearch(
        RetentionBoundaryTimeseriesClient(),  # type: ignore[arg-type]
        lambda: "private-user-id",
    )
    provider = WearableContextProvider(search_reader=search)
    cutoff = NOW - timedelta(days=7)

    result = await provider.query(
        session,
        ContextQuery(
            provider_id="wearable",
            capability="wearable.timeseries",
            start=cutoff,
            end=cutoff + timedelta(hours=1),
            granularity="series",
            parameters={
                "series_type": "steps",
                "resolution": "1hour",
            },
        ),
        now=NOW,
    )

    observed_at = cutoff + timedelta(minutes=30)
    assert result.status is ContextStatus.PARTIAL
    assert result.payload["records"][0]["timestamp"] == (
        observed_at.isoformat()
    )
    assert result.source_refs[0].observed_start == cutoff
    event = session.get(
        WellnessEvent,
        UUID(result.source_refs[0].record_id),
    )
    assert event is not None
    assert event.payload["retention_basis_at"] == observed_at.isoformat()
    assert event.expires_at.replace(tzinfo=UTC) == (
        observed_at + timedelta(days=7)
    )
    assert event.payload["result"]["retention_window"] == {
        "effective_now": NOW.isoformat(),
        "query_start": cutoff.isoformat(),
        "query_end": (cutoff + timedelta(hours=1)).isoformat(),
        "retained_after": cutoff.isoformat(),
        "effective_start": cutoff.isoformat(),
        "effective_start_inclusive": False,
        "effective_end": (cutoff + timedelta(hours=1)).isoformat(),
        "retention_policy": open_wearables_retention_policy_binding(
            session
        ),
    }


async def test_retention_snapshot_expires_during_provider_call(
    session,
) -> None:
    update_retention_policy(
        session,
        OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
        "7d",
        now=NOW,
    )
    cutoff = NOW - timedelta(days=7)
    observed_at = cutoff + timedelta(seconds=1)

    class NearCutoffClient:
        async def get_timeseries(self, _user_id, *_args, **_kwargs):
            return {
                "data": [
                    {
                        "timestamp": observed_at.isoformat(),
                        "type": "steps",
                        "value": 10,
                        "unit": "count",
                        "provider": "apple",
                        "data_source_id": "trusted-sensor",
                    }
                ],
                "pagination": {
                    "next_cursor": None,
                    "has_more": False,
                },
            }

    provider = WearableContextProvider(
        search_reader=BoundedOpenWearablesSearch(
            NearCutoffClient(),  # type: ignore[arg-type]
            lambda: "private-user-id",
        )
    )
    clock_values = iter((NOW, NOW + timedelta(seconds=2)))
    turn = ContextAccessLayer(
        ContextProviderRegistry((provider,)),
        clock=lambda: next(clock_values),
    ).start_turn(
        _request(),
        policy=ContextAccessPolicy(
            owner_principal_id="owner",
            grants=(DomainAccessGrant(domain="wearable"),),
        ),
    )

    result = await turn.query(
        session,
        ContextQuery(
            provider_id="wearable",
            capability="wearable.timeseries",
            start=cutoff,
            end=cutoff + timedelta(hours=1),
            granularity="series",
            parameters={
                "series_type": "steps",
                "resolution": "1hour",
            },
        ),
    )

    assert result.status is ContextStatus.DENIED
    assert result.limitations == ["source_ref_expired"]
    event = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type
            == OPEN_WEARABLES_QUERY_EVENT_TYPE
        )
    )
    assert event is not None
    assert event.expires_at.replace(tzinfo=UTC) == (
        NOW + timedelta(seconds=1)
    )
    assert turn.trace[0].occurred_at == NOW + timedelta(seconds=2)


async def test_conflicting_cross_page_samples_are_partial_not_summed(
    session,
) -> None:
    class ConflictingPagesClient:
        async def get_timeseries(
            self,
            _user_id,
            *_args,
            cursor: str | None,
            **_kwargs,
        ):
            return {
                "data": [
                    {
                        "timestamp": "2026-08-16T08:05:00Z",
                        "type": "steps",
                        "value": 20 if cursor else 10,
                        "unit": "count",
                        "provider": "apple",
                    }
                ],
                "pagination": {
                    "next_cursor": "page-2" if cursor is None else None,
                    "has_more": cursor is None,
                },
            }

    provider = WearableContextProvider(
        search_reader=BoundedOpenWearablesSearch(
            ConflictingPagesClient(),  # type: ignore[arg-type]
            lambda: "private-user-id",
        )
    )

    result = await provider.query(
        session,
        ContextQuery(
            provider_id="wearable",
            capability="wearable.timeseries",
            start=DETAIL_START,
            end=DETAIL_END,
            granularity="series",
            parameters={
                "series_type": "steps",
                "resolution": "1hour",
            },
        ),
        now=NOW,
    )

    assert result.status is ContextStatus.PARTIAL
    assert [
        record["value"] for record in result.payload["records"]
    ] == [10, 20]
    assert "wearable_conflicting_duplicate_rows" in result.limitations
    assert result.coverage.status is CoverageStatus.UNKNOWN


async def test_detail_query_clamps_to_shorter_retention_policy(
    session,
) -> None:
    update_retention_policy(
        session,
        OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
        "7d",
        now=NOW,
    )
    calls: list[WearableSearchRequest] = []

    async def search_reader(
        request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        calls.append(request)
        return WearableSearchFetch(
            records=(
                {
                    "category": "stress",
                    "recorded_at": (
                        NOW - timedelta(hours=1)
                    ).isoformat(),
                    "provider": "garmin",
                    "value": 42,
                },
            )
        )

    provider = WearableContextProvider(search_reader=search_reader)
    result = await provider.query(
        session,
        ContextQuery(
            provider_id="wearable",
            capability="wearable.health-scores",
            start=NOW - timedelta(days=30),
            end=NOW,
            granularity="record",
        ),
        now=NOW,
    )

    assert result.status is ContextStatus.PARTIAL
    assert calls[0].start == NOW - timedelta(days=30)
    assert calls[0].retained_after == NOW - timedelta(days=7)
    assert "wearable_retention_window_trimmed" in result.limitations
    assert result.coverage.status is CoverageStatus.UNKNOWN
    assert result.coverage.ratio is None
    assert result.source_refs[0].observed_start == NOW - timedelta(days=7)
    assert result.source_refs[0].coverage is None
    event = session.get(
        WellnessEvent,
        UUID(result.source_refs[0].record_id),
    )
    assert event is not None
    assert event.coverage is None
    assert event.payload["result"]["status"] == "partial"
    assert "coverage" not in event.payload["result"]


async def test_live_detail_reapplies_retention_time_after_upstream(
    session,
) -> None:
    update_retention_policy(
        session,
        OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
        "7d",
        now=NOW,
    )
    cutoff = NOW - timedelta(days=7)

    async def search_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        return WearableSearchFetch(
            records=(
                {
                    "category": "stress",
                    "recorded_at": (
                        cutoff + timedelta(seconds=1)
                    ).isoformat(),
                    "provider": "garmin",
                    "value": 42,
                },
            )
        )

    result = await WearableContextProvider(
        search_reader=search_reader,
        clock=lambda: NOW + timedelta(seconds=2),
    ).query(
        session,
        ContextQuery(
            provider_id="wearable",
            capability="wearable.health-scores",
            start=cutoff,
            end=cutoff + timedelta(hours=1),
            granularity="record",
            parameters={"category": "stress"},
        ),
        now=NOW,
    )

    live_cutoff = cutoff + timedelta(seconds=2)
    assert result.status is ContextStatus.PARTIAL
    assert result.payload["records"] == []
    assert "wearable_rows_discarded" in result.limitations
    assert result.source_refs[0].observed_start == live_cutoff
    event = session.get(
        WellnessEvent,
        UUID(result.source_refs[0].record_id),
    )
    assert event is not None
    assert event.payload["result"]["retention_window"][
        "effective_now"
    ] == (NOW + timedelta(seconds=2)).isoformat()
    assert event.payload["result"]["retention_window"][
        "retained_after"
    ] == live_cutoff.isoformat()


async def test_fallback_rewrites_records_after_retention_revision_race(
    tmp_path,
) -> None:
    engine, factory = _file_store(
        tmp_path,
        "detail-retention-revision-race.db",
    )
    with factory() as setup:
        update_retention_policy(
            setup,
            OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
            "14d",
            now=NOW,
        )
        setup.commit()

    recent_recorded_at = NOW - timedelta(days=1)

    async def initial_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        return WearableSearchFetch(
            records=(
                {
                    "category": "stress",
                    "recorded_at": recent_recorded_at.isoformat(),
                    "provider": "garmin",
                    "value": 42,
                },
            )
        )

    query = ContextQuery(
        provider_id="wearable",
        capability="wearable.health-scores",
        start=NOW - timedelta(days=12),
        end=NOW,
        granularity="record",
        parameters={"category": "stress"},
    )
    try:
        with factory() as session:
            initial = await WearableContextProvider(
                search_reader=initial_reader,
                snapshot_session_factory=factory,
            ).query(session, query, now=NOW)
        assert len(initial.payload["records"]) == 1

        async def unavailable_reader(
            _request: WearableSearchRequest,
        ) -> WearableSearchFetch:
            with factory() as writer:
                update_retention_policy(
                    writer,
                    OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
                    "7d",
                    now=NOW,
                )
                writer.commit()
            raise RuntimeError("upstream unavailable")

        with factory() as session:
            fallback = await WearableContextProvider(
                search_reader=unavailable_reader,
                snapshot_session_factory=factory,
            ).query(session, query, now=NOW)

        cutoff = NOW - timedelta(days=7)
        assert fallback.status is ContextStatus.PARTIAL
        assert [
            record["recorded_at"]
            for record in fallback.payload["records"]
        ] == [recent_recorded_at.isoformat()]
        assert fallback.payload["window"]["start"] == cutoff.isoformat()
        assert fallback.source_refs[0].observed_start == cutoff
        assert (
            fallback.source_refs[0].record_id
            != initial.source_refs[0].record_id
        )
        assert {
            "open_wearables_detail_unavailable",
            "wearable_query_snapshot_fallback_used",
            "wearable_retention_window_trimmed",
        } <= set(fallback.limitations)
        with factory() as observer:
            event = observer.get(
                WellnessEvent,
                UUID(fallback.source_refs[0].record_id),
            )
            assert event is not None
            assert event.payload["result"]["retention_window"][
                "retention_policy"
            ] == open_wearables_retention_policy_binding(observer)
    finally:
        engine.dispose()


async def test_live_result_filters_records_after_retention_revision_race(
    tmp_path,
) -> None:
    engine, factory = _file_store(
        tmp_path,
        "detail-live-retention-revision-race.db",
    )
    with factory() as setup:
        update_retention_policy(
            setup,
            OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
            "14d",
            now=NOW,
        )
        setup.commit()

    old_recorded_at = NOW - timedelta(days=10)
    recent_recorded_at = NOW - timedelta(days=1)

    async def racing_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        with factory() as writer:
            update_retention_policy(
                writer,
                OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
                "7d",
                now=NOW,
            )
            writer.commit()
        return WearableSearchFetch(
            records=(
                {
                    "category": "stress",
                    "recorded_at": old_recorded_at.isoformat(),
                    "provider": "garmin",
                    "value": 40,
                },
                {
                    "category": "stress",
                    "recorded_at": recent_recorded_at.isoformat(),
                    "provider": "garmin",
                    "value": 42,
                },
            )
        )

    try:
        with factory() as session:
            result = await WearableContextProvider(
                search_reader=racing_reader,
                snapshot_session_factory=factory,
            ).query(
                session,
                ContextQuery(
                    provider_id="wearable",
                    capability="wearable.health-scores",
                    start=NOW - timedelta(days=12),
                    end=NOW,
                    granularity="record",
                    parameters={"category": "stress"},
                ),
                now=NOW,
            )

        cutoff = NOW - timedelta(days=7)
        assert [
            record["recorded_at"]
            for record in result.payload["records"]
        ] == [recent_recorded_at.isoformat()]
        assert result.source_refs[0].observed_start == cutoff
        assert "wearable_rows_discarded" in result.limitations
        with factory() as observer:
            event = observer.get(
                WellnessEvent,
                UUID(result.source_refs[0].record_id),
            )
            assert event is not None
            assert event.payload["result"]["retention_window"][
                "retention_policy"
            ] == open_wearables_retention_policy_binding(observer)
    finally:
        engine.dispose()


async def test_fallback_fails_closed_if_retention_changes_again(
    tmp_path,
) -> None:
    engine, factory = _file_store(
        tmp_path,
        "detail-retention-double-race.db",
    )
    with factory() as setup:
        update_retention_policy(
            setup,
            OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
            "14d",
            now=NOW,
        )
        persist_open_wearables_query_snapshot(
            setup,
            capability="wearable.health-scores",
            start=NOW - timedelta(days=12),
            end=NOW,
            timezone="UTC",
            parameters={"category": "stress"},
            result={
                "status": "ok",
                "records": [
                    {
                        "category": "stress",
                        "recorded_at": (
                            NOW - timedelta(hours=1)
                        ).isoformat(),
                        "provider": "garmin",
                        "value": 40,
                    }
                ],
            },
            collected_at=NOW,
            now=NOW,
        )
        setup.commit()

    async def unavailable_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        with factory() as writer:
            update_retention_policy(
                writer,
                OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
                "7d",
                now=NOW,
            )
            writer.commit()
        raise RuntimeError("upstream unavailable")

    class DoubleRaceProvider(WearableContextProvider):
        def _store_detail_snapshot(self, *args, **kwargs):
            with factory() as writer:
                update_retention_policy(
                    writer,
                    OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
                    "1d",
                    now=NOW,
                )
                writer.commit()
            return super()._store_detail_snapshot(*args, **kwargs)

    try:
        with factory() as session:
            result = await DoubleRaceProvider(
                search_reader=unavailable_reader,
                snapshot_session_factory=factory,
            ).query(
                session,
                ContextQuery(
                    provider_id="wearable",
                    capability="wearable.health-scores",
                    start=NOW - timedelta(days=12),
                    end=NOW,
                    granularity="record",
                    parameters={"category": "stress"},
                ),
                now=NOW,
            )

        assert result.status is ContextStatus.FAILED
        assert result.source_refs == []
        assert result.payload == {}
        assert {
            "open_wearables_detail_unavailable",
            "wearable_query_snapshot_fallback_used",
            "wearable_snapshot_persistence_failed",
        } <= set(result.limitations)
    finally:
        engine.dispose()


async def test_detail_query_fully_before_retention_skips_upstream(
    session,
) -> None:
    update_retention_policy(
        session,
        OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
        "7d",
        now=NOW,
    )
    calls = 0

    async def search_reader(
        request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        nonlocal calls
        calls += 1
        return WearableSearchFetch(records=())

    provider = WearableContextProvider(search_reader=search_reader)
    result = await provider.query(
        session,
        ContextQuery(
            provider_id="wearable",
            capability="wearable.health-scores",
            start=NOW - timedelta(days=20),
            end=NOW - timedelta(days=8),
            granularity="record",
        ),
        now=NOW,
    )

    assert result.status is ContextStatus.UNAVAILABLE
    assert result.limitations == [
        "wearable_query_outside_retention_window"
    ]
    assert calls == 0


async def test_daily_summary_provenance_uses_the_summary_day(
    session,
) -> None:
    day_start = datetime(2026, 8, 10, tzinfo=UTC)

    async def search_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        return WearableSearchFetch(
            records=(
                {
                    "summary_kind": "sleep",
                    "date": "2026-08-10",
                    "provider": "oura",
                    "provider_attribution": "declared",
                    "start_time": "2026-08-09T23:00:00+00:00",
                    "end_time": "2026-08-10T07:00:00+00:00",
                    "duration_minutes": 480,
                },
            )
        )

    result = await WearableContextProvider(
        search_reader=search_reader
    ).query(
        session,
        ContextQuery(
            provider_id="wearable",
            capability="wearable.summaries",
            start=day_start,
            end=day_start + timedelta(days=1),
            granularity="summary",
            parameters={"summary_kind": "sleep"},
        ),
        now=NOW,
    )

    assert result.status is ContextStatus.OK
    assert result.source_refs[0].observed_start == day_start
    assert (
        result.payload["records"][0]["provenance"]["observed_at"]
        == day_start.isoformat()
    )


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
                    "provider": "apple_health",
                },
                {
                    "timestamp": "2026-08-16T08:10:00+00:00",
                    "series_type": "heart_rate",
                    "value": 76,
                    "unit": "bpm",
                    "provider": "apple_health",
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
        first_provenance = first.payload["records"][0]["provenance"]
        second_provenance = second.payload["records"][0]["provenance"]
        assert first_provenance == {
            "source_ref_id": source_ref.reference_id,
            "row_digest": first_provenance["row_digest"],
            "upstream_provider": "open_wearables",
            "wearable_provider": "apple_health",
            "provider_attribution": "declared",
            "observed_at": "2026-08-16T08:05:00+00:00",
            "mode": "live_upstream_mirrored",
        }
        assert len(first_provenance["row_digest"]) == 64
        assert second_provenance == {
            "source_ref_id": source_ref.reference_id,
            "row_digest": second_provenance["row_digest"],
            "upstream_provider": "open_wearables",
            "wearable_provider": "apple_health",
            "provider_attribution": "declared",
            "observed_at": "2026-08-16T08:10:00+00:00",
            "mode": "retained_local_mirror",
        }
        assert len(second_provenance["row_digest"]) == 64
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


async def test_cursor_freezes_retention_window_and_never_refetches(
    tmp_path,
) -> None:
    engine, factory = _file_store(
        tmp_path,
        "detail-retention-cursor.db",
    )
    with factory() as setup:
        update_retention_policy(
            setup,
            OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
            "7d",
            now=NOW,
        )
        setup.commit()

    cutoff = NOW - timedelta(days=7)
    requested_start = cutoff - timedelta(hours=1)
    requested_end = cutoff + timedelta(hours=3)
    calls: list[WearableSearchRequest] = []

    async def search_reader(
        request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        calls.append(request)
        return WearableSearchFetch(
            records=(
                {
                    "timestamp": (
                        cutoff + timedelta(hours=2)
                    ).isoformat(),
                    "series_type": "heart_rate",
                    "value": 72,
                    "unit": "bpm",
                    "provider": "apple_health",
                },
                {
                    "timestamp": (
                        cutoff
                        + timedelta(hours=2, minutes=30)
                    ).isoformat(),
                    "series_type": "heart_rate",
                    "value": 76,
                    "unit": "bpm",
                    "provider": "apple_health",
                },
            )
        )

    current = [NOW]
    service = _service(
        factory,
        WearableContextProvider(
            search_reader=search_reader,
            snapshot_session_factory=factory,
        ),
        clock=lambda: current[0],
    )
    try:
        handle = service.begin(_request())
        first = await service.search(
            handle.session_id,
            domain="wearable",
            capability="wearable.timeseries",
            start=requested_start,
            end=requested_end,
            granularity="series",
            limit=1,
            parameters={
                "series_type": "heart_rate",
                "resolution": "1hour",
            },
        )
        assert first.next_cursor is not None

        current[0] = NOW + timedelta(hours=1)
        second = await service.search(
            handle.session_id,
            domain="wearable",
            capability="wearable.timeseries",
            start=requested_start,
            end=requested_end,
            granularity="series",
            limit=1,
            parameters={
                "series_type": "heart_rate",
                "resolution": "1hour",
                "cursor": first.next_cursor,
            },
        )
        invalid = await service.search(
            handle.session_id,
            domain="wearable",
            capability="wearable.timeseries",
            start=requested_start,
            end=requested_end + timedelta(hours=1),
            granularity="series",
            limit=1,
            parameters={
                "series_type": "heart_rate",
                "resolution": "1hour",
                "cursor": first.next_cursor,
            },
        )

        assert len(calls) == 1
        assert calls[0].start == requested_start
        assert calls[0].end == requested_end
        assert calls[0].retained_after == cutoff
        assert first.payload["records"][0]["value"] == 72
        assert second.payload["records"][0]["value"] == 76
        assert first.source_refs == second.source_refs
        assert second.source_refs[0].observed_start == cutoff
        assert invalid.status is ContextStatus.FAILED
        assert invalid.limitations == ["invalid_provider_query"]
    finally:
        service.close()
        engine.dispose()


async def test_cursor_resolves_snapshot_older_than_newest_32_without_scan(
    session,
    monkeypatch,
) -> None:
    calls = 0

    async def search_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        nonlocal calls
        calls += 1
        value = calls * 100
        return WearableSearchFetch(
            records=(
                {
                    "category": "stress",
                    "recorded_at": (
                        DETAIL_START + timedelta(minutes=5)
                    ).isoformat(),
                    "provider": "garmin",
                    "value": value + 1,
                },
                {
                    "category": "stress",
                    "recorded_at": (
                        DETAIL_START + timedelta(minutes=10)
                    ).isoformat(),
                    "provider": "garmin",
                    "value": value + 2,
                },
            )
        )

    provider = WearableContextProvider(search_reader=search_reader)
    query = ContextQuery(
        provider_id="wearable",
        capability="wearable.health-scores",
        start=DETAIL_START,
        end=DETAIL_END,
        granularity="record",
        limit=1,
        parameters={"category": "stress"},
    )
    base_now = NOW - timedelta(minutes=40)
    first = await provider.query(session, query, now=base_now)
    assert first.next_cursor is not None
    assert first.next_cursor.startswith("hmc2_")
    assert len(first.next_cursor) == 232
    first_event_id = UUID(first.source_refs[0].record_id)
    legacy_cursor = _legacy_health_score_cursor(
        session,
        query=query,
        snapshot_event_id=first_event_id,
    )

    for offset in range(1, 40):
        await provider.query(
            session,
            query,
            now=base_now + timedelta(minutes=offset),
        )

    newest = retained_open_wearables_query_snapshots(
        session,
        capability=query.capability,
        start=DETAIL_START,
        end=DETAIL_END,
        timezone="UTC",
        parameters={"category": "stress"},
        now=NOW,
    )
    assert len(newest) == 32
    assert all(
        str(snapshot.event_id) != first.source_refs[0].record_id
        for snapshot in newest
    )

    original_indexed_lookup = (
        domain_providers
        .retained_open_wearables_query_snapshot_by_event_id
    )
    indexed_lookups = 0

    def indexed_lookup(*args, **kwargs):
        nonlocal indexed_lookups
        indexed_lookups += 1
        return original_indexed_lookup(*args, **kwargs)

    def fail_unbounded_scan(*_args, **_kwargs):
        raise AssertionError("hmc2 must not scan retained snapshots")

    with monkeypatch.context() as patch:
        patch.setattr(
            domain_providers,
            "retained_open_wearables_query_snapshot_by_event_id",
            indexed_lookup,
        )
        patch.setattr(
            domain_providers,
            "retained_open_wearables_query_snapshots",
            fail_unbounded_scan,
        )
        second = await provider.query(
            session,
            query.model_copy(
                update={
                    "parameters": {
                        "category": "stress",
                        "cursor": first.next_cursor,
                    }
                }
            ),
            now=NOW,
        )

    assert calls == 40
    assert indexed_lookups == 1
    assert second.payload["records"][0]["value"] == 102
    assert second.source_refs == first.source_refs

    original_legacy_lookup = (
        domain_providers.retained_open_wearables_query_snapshots
    )
    candidate_limits: list[int] = []

    def bounded_legacy_lookup(*args, **kwargs):
        candidate_limits.append(kwargs["candidate_limit"])
        return original_legacy_lookup(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(
            domain_providers,
            "retained_open_wearables_query_snapshots",
            bounded_legacy_lookup,
        )
        with pytest.raises(
            InvalidContextCursorError,
            match="invalid or.*expired",
        ):
            await provider.query(
                session,
                query.model_copy(
                    update={
                        "parameters": {
                            "category": "stress",
                            "cursor": legacy_cursor,
                        }
                    }
                ),
                now=NOW,
            )
    assert candidate_limits == [32]


async def test_recent_legacy_cursor_is_bounded_and_upgrades_to_hmc2(
    session,
    monkeypatch,
) -> None:
    async def search_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        return WearableSearchFetch(
            records=tuple(
                {
                    "category": "stress",
                    "recorded_at": (
                        DETAIL_START + timedelta(minutes=minute)
                    ).isoformat(),
                    "provider": "garmin",
                    "value": value,
                }
                for minute, value in ((5, 41), (10, 42), (15, 43))
            )
        )

    provider = WearableContextProvider(search_reader=search_reader)
    query = ContextQuery(
        provider_id="wearable",
        capability="wearable.health-scores",
        start=DETAIL_START,
        end=DETAIL_END,
        granularity="record",
        limit=1,
        parameters={"category": "stress"},
    )
    first = await provider.query(session, query, now=NOW)
    legacy_cursor = _legacy_health_score_cursor(
        session,
        query=query,
        snapshot_event_id=UUID(first.source_refs[0].record_id),
    )
    original_lookup = (
        domain_providers.retained_open_wearables_query_snapshots
    )
    candidate_limits: list[int] = []

    def bounded_lookup(*args, **kwargs):
        candidate_limits.append(kwargs["candidate_limit"])
        return original_lookup(*args, **kwargs)

    monkeypatch.setattr(
        domain_providers,
        "retained_open_wearables_query_snapshots",
        bounded_lookup,
    )
    second = await provider.query(
        session,
        query.model_copy(
            update={
                "parameters": {
                    "category": "stress",
                    "cursor": legacy_cursor,
                }
            }
        ),
        now=NOW,
    )

    assert candidate_limits == [32]
    assert second.payload["records"][0]["value"] == 42
    assert second.next_cursor is not None
    assert second.next_cursor.startswith("hmc2_")


async def test_tampered_hmc2_fails_before_any_snapshot_lookup(
    session,
    monkeypatch,
) -> None:
    async def search_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        return _detail_timeseries_fetch()

    provider = WearableContextProvider(
        search_reader=search_reader,
        cursor_signing_key=b"k" * 32,
    )
    query = ContextQuery(
        provider_id="wearable",
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
    first = await provider.query(session, query, now=NOW)
    assert first.next_cursor is not None
    replacement = (
        "0" if first.next_cursor[-1] != "0" else "1"
    )
    tampered = first.next_cursor[:-1] + replacement

    def fail_lookup(*_args, **_kwargs):
        raise AssertionError("tampered cursor must fail before DB lookup")

    monkeypatch.setattr(
        domain_providers,
        "retained_open_wearables_query_snapshot_by_event_id",
        fail_lookup,
    )
    monkeypatch.setattr(
        domain_providers,
        "retained_open_wearables_query_snapshots",
        fail_lookup,
    )
    with pytest.raises(InvalidContextCursorError):
        await provider.query(
            session,
            query.model_copy(
                update={
                    "parameters": {
                        **query.parameters,
                        "cursor": tampered,
                    }
                }
            ),
            now=NOW,
        )


async def test_hmc2_is_invalid_after_snapshot_deletion(
    session,
    monkeypatch,
) -> None:
    async def search_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        return _detail_timeseries_fetch()

    provider = WearableContextProvider(search_reader=search_reader)
    query = ContextQuery(
        provider_id="wearable",
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
    first = await provider.query(session, query, now=NOW)
    assert first.next_cursor is not None
    event = session.get(
        WellnessEvent,
        UUID(first.source_refs[0].record_id),
    )
    assert event is not None
    session.delete(event)
    session.flush()

    def fail_scan(*_args, **_kwargs):
        raise AssertionError("hmc2 deletion must not enter legacy scan")

    monkeypatch.setattr(
        domain_providers,
        "retained_open_wearables_query_snapshots",
        fail_scan,
    )
    with pytest.raises(
        InvalidContextCursorError,
        match="invalid or.*expired",
    ):
        await provider.query(
            session,
            query.model_copy(
                update={
                    "parameters": {
                        **query.parameters,
                        "cursor": first.next_cursor,
                    }
                }
            ),
            now=NOW,
        )


async def test_hmc2_owner_binding_cannot_be_reused(
    session,
    monkeypatch,
) -> None:
    async def search_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        return _detail_timeseries_fetch()

    signing_key = b"k" * 32
    owner_provider = WearableContextProvider(
        search_reader=search_reader,
        cursor_signing_key=signing_key,
        owner_principal_id="owner-a",
    )
    other_owner_provider = WearableContextProvider(
        search_reader=search_reader,
        cursor_signing_key=signing_key,
        owner_principal_id="owner-b",
    )
    query = ContextQuery(
        provider_id="wearable",
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
    first = await owner_provider.query(session, query, now=NOW)
    assert first.next_cursor is not None

    def fail_lookup(*_args, **_kwargs):
        raise AssertionError("owner mismatch must fail before DB lookup")

    monkeypatch.setattr(
        domain_providers,
        "retained_open_wearables_query_snapshot_by_event_id",
        fail_lookup,
    )
    monkeypatch.setattr(
        domain_providers,
        "retained_open_wearables_query_snapshots",
        fail_lookup,
    )
    with pytest.raises(InvalidContextCursorError):
        await other_owner_provider.query(
            session,
            query.model_copy(
                update={
                    "parameters": {
                        **query.parameters,
                        "cursor": first.next_cursor,
                    }
                }
            ),
            now=NOW,
        )


def test_retained_snapshot_order_uses_event_id_as_final_tiebreaker(
    session,
) -> None:
    event_ids: list[UUID] = []
    collected_at = NOW - timedelta(hours=1)
    for value in (41, 42, 43):
        snapshot = persist_open_wearables_query_snapshot(
            session,
            capability="wearable.health-scores",
            start=DETAIL_START,
            end=DETAIL_END,
            timezone="UTC",
            parameters={"category": "stress"},
            result={
                "status": "ok",
                "records": [
                    {
                        "category": "stress",
                        "recorded_at": (
                            DETAIL_START + timedelta(minutes=5)
                        ).isoformat(),
                        "provider": "garmin",
                        "value": value,
                    }
                ],
            },
            collected_at=collected_at,
            now=NOW,
        )
        event_ids.append(snapshot.event_id)
    tied_created_at = NOW - timedelta(days=1)
    for event_id in event_ids:
        event = session.get(WellnessEvent, event_id)
        assert event is not None
        event.created_at = tied_created_at
    session.flush()

    snapshots = retained_open_wearables_query_snapshots(
        session,
        capability="wearable.health-scores",
        start=DETAIL_START,
        end=DETAIL_END,
        timezone="UTC",
        parameters={"category": "stress"},
        now=NOW,
    )

    assert [snapshot.event_id for snapshot in snapshots] == sorted(
        event_ids,
        reverse=True,
    )


@pytest.mark.parametrize("candidate_limit", (0, 33, None, True))
def test_retained_snapshot_candidate_limit_cannot_be_unbounded(
    session,
    candidate_limit,
) -> None:
    with pytest.raises(
        ValueError,
        match="candidate_limit must be between 1 and 32",
    ):
        retained_open_wearables_query_snapshots(
            session,
            capability="wearable.health-scores",
            start=DETAIL_START,
            end=DETAIL_END,
            timezone="UTC",
            parameters={"category": "stress"},
            now=NOW,
            candidate_limit=candidate_limit,
        )


async def test_detail_digest_ignores_private_source_identifiers(
    tmp_path,
) -> None:
    async def fetch_digest(
        database_name: str,
        private_source: str,
    ) -> str:
        engine, factory = _file_store(tmp_path, database_name)

        async def search_reader(
            _request: WearableSearchRequest,
        ) -> WearableSearchFetch:
            return WearableSearchFetch(
                records=(
                    {
                        "timestamp": "2026-08-16T08:05:00+00:00",
                        "series_type": "heart_rate",
                        "value": 72,
                        "unit": "bpm",
                        "provider": "apple_health",
                        "source": {
                            "provider": private_source,
                            "device": "private-device",
                        },
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
            result = await service.search(
                handle.session_id,
                domain="wearable",
                capability="wearable.timeseries",
                start=DETAIL_START,
                end=DETAIL_END,
                granularity="series",
                parameters={
                    "series_type": "heart_rate",
                    "resolution": "1min",
                },
            )
            record = result.payload["records"][0]
            encoded_record = json.dumps(record, sort_keys=True)
            assert private_source not in encoded_record
            assert "private-device" not in encoded_record

            with factory() as observer:
                event = observer.get(
                    WellnessEvent,
                    UUID(result.source_refs[0].record_id),
                )
                assert event is not None
                encoded_snapshot = json.dumps(
                    event.payload,
                    sort_keys=True,
                )
                assert private_source not in encoded_snapshot
                assert "private-device" not in encoded_snapshot
            return str(record["provenance"]["row_digest"])
        finally:
            service.close()
            engine.dispose()

    first = await fetch_digest(
        "detail-private-source-a.db",
        "com.apple.health.bundle-A",
    )
    second = await fetch_digest(
        "detail-private-source-b.db",
        "com.apple.health.bundle-B",
    )

    assert first == second


async def test_private_source_does_not_change_public_cursor_scope_or_identity(
    tmp_path,
) -> None:
    async def fetch_cursor(
        database_name: str,
        private_source: str,
    ) -> str:
        engine, factory = _file_store(tmp_path, database_name)
        with factory() as session:
            persist_open_wearables_query_snapshot(
                session,
                capability="wearable.timeseries",
                start=DETAIL_START,
                end=DETAIL_END,
                timezone="UTC",
                parameters={
                    "series_type": "heart_rate",
                    "resolution": "1min",
                },
                result={
                    "status": "ok",
                    "records": [
                        {
                            "timestamp":
                                "2026-08-16T08:05:00+00:00",
                            "series_type": "heart_rate",
                            "value": 72,
                            "unit": "bpm",
                            "source": {
                                "provider": private_source,
                                "device": "private-device",
                            },
                        },
                        {
                            "timestamp":
                                "2026-08-16T08:10:00+00:00",
                            "series_type": "heart_rate",
                            "value": 76,
                            "unit": "bpm",
                            "provider": "apple_health",
                        },
                    ],
                    "coverage": {"ratio": 1.0},
                    "limitations": [],
                },
                collected_at=NOW,
                now=NOW,
            )
            session.commit()

        async def unavailable_reader(
            _request: WearableSearchRequest,
        ) -> WearableSearchFetch:
            raise RuntimeError("upstream unavailable")

        service = _service(
            factory,
            WearableContextProvider(
                search_reader=unavailable_reader,
                snapshot_session_factory=factory,
            ),
        )
        try:
            handle = service.begin(_request())
            result = await service.search(
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
            assert result.next_cursor is not None
            encoded = json.dumps(result.payload["records"], sort_keys=True)
            assert private_source not in encoded
            assert "private-device" not in encoded
            assert result.payload["records"][0]["provider"] == "unknown"
            return result.next_cursor
        finally:
            service.close()
            engine.dispose()

    first = await fetch_cursor(
        "legacy-private-cursor-a.db",
        "com.apple.health.bundle-A",
    )
    second = await fetch_cursor(
        "legacy-private-cursor-b.db",
        "com.apple.health.bundle-B",
    )

    assert first.startswith("hmc2_")
    assert second.startswith("hmc2_")
    assert first.split("_")[2:4] == second.split("_")[2:4]


async def test_identical_public_wearable_records_paginate_to_completion(
    tmp_path,
) -> None:
    engine, factory = _file_store(tmp_path, "identical-pagination.db")

    async def search_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        record = {
            "timestamp": "2026-08-16T08:05:00+00:00",
            "series_type": "heart_rate",
            "value": 72,
            "unit": "bpm",
            "provider": "apple_health",
        }
        return WearableSearchFetch(
            records=(dict(record), dict(record), dict(record))
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
        cursor: str | None = None
        pages = []
        for _ in range(3):
            parameters = {
                "series_type": "heart_rate",
                "resolution": "1min",
            }
            if cursor is not None:
                parameters["cursor"] = cursor
            page = await service.search(
                handle.session_id,
                domain="wearable",
                capability="wearable.timeseries",
                start=DETAIL_START,
                end=DETAIL_END,
                granularity="series",
                limit=1,
                parameters=parameters,
            )
            pages.append(page)
            cursor = page.next_cursor

        assert [page.payload["count"] for page in pages] == [1, 1, 1]
        assert pages[0].next_cursor is not None
        assert pages[1].next_cursor is not None
        assert pages[0].next_cursor != pages[1].next_cursor
        assert pages[2].next_cursor is None
        assert [
            page.payload["records"][0]["value"]
            for page in pages
        ] == [72, 72, 72]
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
                    "provider": "oura",
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
        assert fallback.status is ContextStatus.PARTIAL
        assert fallback.coverage.status is CoverageStatus.UNKNOWN
        assert fallback.coverage.ratio is None
        assert (
            fallback.source_refs[0].reference_id
            != initial.source_refs[0].reference_id
        )
        assert fallback.source_refs[0].coverage is None
        assert fallback.source_refs[0].collected_at == (
            initial.source_refs[0].collected_at
        )
        initial_record = initial.payload["records"][0]
        fallback_record = fallback.payload["records"][0]
        assert {
            key: value
            for key, value in fallback_record.items()
            if key != "provenance"
        } == {
            key: value
            for key, value in initial_record.items()
            if key != "provenance"
        }
        assert (
            fallback_record["provenance"]["source_ref_id"]
            == fallback.source_refs[0].reference_id
        )
        assert (
            fallback_record["provenance"]["row_digest"]
            == initial_record["provenance"]["row_digest"]
        )
        assert (
            initial_record["provenance"]["mode"]
            == "live_upstream_mirrored"
        )
        assert (
            fallback_record["provenance"]["mode"]
            == "retained_local_mirror"
        )
        assert "open_wearables_detail_unavailable" in fallback.limitations
        assert (
            "wearable_query_snapshot_fallback_used"
            in fallback.limitations
        )
    finally:
        fallback_service.close()
        engine.dispose()


@pytest.mark.parametrize(
    (
        "capability",
        "granularity",
        "start",
        "end",
        "parameters",
        "records",
    ),
    FALLBACK_CURSOR_CASES,
)
async def test_fallback_cursor_preserves_partial_snapshot_state(
    tmp_path,
    capability: str,
    granularity: str,
    start: datetime,
    end: datetime,
    parameters: dict,
    records: tuple[dict, ...],
) -> None:
    database_name = capability.replace(".", "-") + "-fallback-cursor.db"
    engine, factory = _file_store(tmp_path, database_name)

    async def initial_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        return WearableSearchFetch(records=records)

    initial_service = _service(
        factory,
        WearableContextProvider(
            search_reader=initial_reader,
            snapshot_session_factory=factory,
        ),
    )
    handle = initial_service.begin(_request())
    initial = await initial_service.search(
        handle.session_id,
        domain="wearable",
        capability=capability,
        start=start,
        end=end,
        granularity=granularity,
        parameters=parameters,
    )
    initial_service.finish(handle.session_id)
    initial_service.close()

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
        first = await fallback_service.search(
            handle.session_id,
            domain="wearable",
            capability=capability,
            start=start,
            end=end,
            granularity=granularity,
            limit=1,
            parameters=parameters,
        )
        assert first.next_cursor is not None
        second = await fallback_service.search(
            handle.session_id,
            domain="wearable",
            capability=capability,
            start=start,
            end=end,
            granularity=granularity,
            limit=1,
            parameters={
                **parameters,
                "cursor": first.next_cursor,
            },
        )

        assert fallback_calls == 1
        assert first.source_refs == second.source_refs
        assert first.source_refs[0].reference_id != (
            initial.source_refs[0].reference_id
        )
        assert first.source_refs[0].coverage is None
        assert first.source_refs[0].collected_at == (
            initial.source_refs[0].collected_at
        )
        for page in (first, second):
            assert page.status is ContextStatus.PARTIAL
            assert page.coverage.status is CoverageStatus.UNKNOWN
            assert page.coverage.ratio is None
            assert "open_wearables_detail_unavailable" in page.limitations
            assert (
                "wearable_query_snapshot_fallback_used"
                in page.limitations
            )
            assert page.payload["status"] == "partial"
        with factory() as observer:
            event = observer.get(
                WellnessEvent,
                UUID(first.source_refs[0].record_id),
            )
            assert event is not None
            assert event.coverage is None
            assert event.recorded_at.replace(tzinfo=UTC) == (
                initial.source_refs[0].collected_at
            )
            assert event.payload["result"]["status"] == "partial"
            assert event.payload["result"]["coverage"] == {
                "status": "unknown"
            }
            assert set(event.payload["result"]["limitations"]) >= {
                "open_wearables_detail_unavailable",
                "wearable_query_snapshot_fallback_used",
            }
    finally:
        fallback_service.close()
        engine.dispose()


async def test_cursor_is_rejected_after_retention_policy_changes(
    session,
) -> None:
    calls = 0

    async def search_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        nonlocal calls
        calls += 1
        return WearableSearchFetch(
            records=(
                {
                    "category": "stress",
                    "recorded_at": "2026-08-16T08:05:00+00:00",
                    "provider": "garmin",
                    "value": 41,
                },
                {
                    "category": "stress",
                    "recorded_at": "2026-08-16T08:10:00+00:00",
                    "provider": "garmin",
                    "value": 42,
                },
            )
        )

    provider = WearableContextProvider(search_reader=search_reader)
    first = await provider.query(
        session,
        ContextQuery(
            provider_id="wearable",
            capability="wearable.health-scores",
            start=DETAIL_START,
            end=DETAIL_END,
            granularity="record",
            limit=1,
            parameters={"category": "stress"},
        ),
        now=NOW,
    )
    assert first.next_cursor is not None

    update_retention_policy(
        session,
        OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
        "7d",
        now=NOW,
    )

    with pytest.raises(InvalidContextCursorError):
        await provider.query(
            session,
            ContextQuery(
                provider_id="wearable",
                capability="wearable.health-scores",
                start=DETAIL_START,
                end=DETAIL_END,
                granularity="record",
                limit=1,
                parameters={
                    "category": "stress",
                    "cursor": first.next_cursor,
                },
            ),
            now=NOW,
        )
    assert calls == 1


async def test_cursor_reads_retention_change_past_stale_identity_map(
    tmp_path,
) -> None:
    engine, factory = _file_store(
        tmp_path,
        "detail-retention-external-change.db",
    )
    with factory() as setup:
        update_retention_policy(
            setup,
            OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
            "14d",
            now=NOW,
        )
        setup.commit()

    calls = 0

    async def search_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        nonlocal calls
        calls += 1
        return WearableSearchFetch(
            records=(
                {
                    "category": "stress",
                    "recorded_at": "2026-08-16T08:05:00+00:00",
                    "provider": "garmin",
                    "value": 41,
                },
                {
                    "category": "stress",
                    "recorded_at": "2026-08-16T08:10:00+00:00",
                    "provider": "garmin",
                    "value": 42,
                },
            )
        )

    provider = WearableContextProvider(search_reader=search_reader)
    try:
        with factory() as reader_session:
            first = await provider.query(
                reader_session,
                ContextQuery(
                    provider_id="wearable",
                    capability="wearable.health-scores",
                    start=DETAIL_START,
                    end=DETAIL_END,
                    granularity="record",
                    limit=1,
                    parameters={"category": "stress"},
                ),
                now=NOW,
            )
            assert first.next_cursor is not None
            stale_policy = reader_session.scalar(
                select(RetentionPolicy).where(
                    RetentionPolicy.data_class
                    == OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS
                )
            )
            assert stale_policy is not None
            assert stale_policy.retention_days == 14
            reader_session.commit()

            with factory() as writer_session:
                update_retention_policy(
                    writer_session,
                    OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
                    "7d",
                    now=NOW,
                )
                writer_session.commit()

            assert stale_policy.retention_days == 14
            with pytest.raises(InvalidContextCursorError):
                await provider.query(
                    reader_session,
                    ContextQuery(
                        provider_id="wearable",
                        capability="wearable.health-scores",
                        start=DETAIL_START,
                        end=DETAIL_END,
                        granularity="record",
                        limit=1,
                        parameters={
                            "category": "stress",
                            "cursor": first.next_cursor,
                        },
                    ),
                    now=NOW,
                )
        assert calls == 1
    finally:
        engine.dispose()


def test_workout_query_snapshot_accepts_exact_end_and_rejects_overrun(
    session,
) -> None:
    base_result = {
        "status": "ok",
        "records": [
            {
                "workout_type": "running",
                "start_time": (
                    DETAIL_START + timedelta(minutes=5)
                ).isoformat(),
                "end_time": DETAIL_END.isoformat(),
                "provider": "garmin",
                "duration_seconds": 3300,
            }
        ],
        "coverage": {"ratio": 1.0},
        "limitations": [],
    }
    snapshot = persist_open_wearables_query_snapshot(
        session,
        capability="wearable.workouts",
        start=DETAIL_START,
        end=DETAIL_END,
        timezone="UTC",
        parameters={},
        result=base_result,
        collected_at=NOW,
        now=NOW,
    )
    assert snapshot.result["records"][0]["end_time"] == DETAIL_END.isoformat()

    invalid_result = {
        **base_result,
        "records": [
            {
                **base_result["records"][0],
                "end_time": (DETAIL_END + timedelta(seconds=1)).isoformat(),
            }
        ],
    }
    with pytest.raises(
        ValueError,
        match="wearable workout result interval is invalid",
    ):
        persist_open_wearables_query_snapshot(
            session,
            capability="wearable.workouts",
            start=DETAIL_START,
            end=DETAIL_END,
            timezone="UTC",
            parameters={},
            result=invalid_result,
            collected_at=NOW + timedelta(seconds=1),
            now=NOW + timedelta(seconds=1),
        )


async def test_retained_partial_day_summary_is_filtered(
    session,
) -> None:
    persist_open_wearables_query_snapshot(
        session,
        capability="wearable.summaries",
        start=DETAIL_START,
        end=DETAIL_END,
        timezone="UTC",
        parameters={"summary_kind": "sleep"},
        result={
            "status": "ok",
            "records": [
                {
                    "summary_kind": "sleep",
                    "date": "2026-08-16",
                    "provider": "oura",
                    "provider_attribution": "declared",
                    "start_time": "2026-08-15T23:00:00+00:00",
                    "end_time": "2026-08-16T07:00:00+00:00",
                }
            ],
            "coverage": {"ratio": 1.0},
            "limitations": [],
        },
        collected_at=NOW,
        now=NOW,
    )
    session.commit()

    async def unavailable_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        raise RuntimeError("upstream unavailable")

    result = await WearableContextProvider(
        search_reader=unavailable_reader
    ).query(
        session,
        ContextQuery(
            provider_id="wearable",
            capability="wearable.summaries",
            start=DETAIL_START,
            end=DETAIL_END,
            granularity="summary",
            parameters={"summary_kind": "sleep"},
        ),
        now=NOW,
    )

    assert result.status is ContextStatus.PARTIAL
    assert result.payload["records"] == []
    assert result.coverage.status is CoverageStatus.UNKNOWN
    assert "wearable_rows_discarded" in result.limitations
    assert "wearable_summary_window_partial" in result.limitations


async def test_detail_search_reads_legacy_snapshot_without_provider(
    tmp_path,
) -> None:
    engine, factory = _file_store(tmp_path, "detail-legacy-provider.db")
    with factory() as session:
        snapshot = persist_open_wearables_query_snapshot(
            session,
            capability="wearable.health-scores",
            start=DETAIL_START,
            end=DETAIL_END,
            timezone="UTC",
            parameters={"category": "recovery"},
            result={
                "status": "ok",
                "records": [
                    {
                        "category": "recovery",
                        "recorded_at":
                            "2026-08-16T08:00:00+00:00",
                        "value": 82,
                    }
                ],
                "coverage": {"ratio": 1.0},
                "limitations": [],
            },
            collected_at=NOW,
            now=NOW,
        )
        session.commit()

    async def unavailable_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        raise RuntimeError("upstream unavailable")

    service = _service(
        factory,
        WearableContextProvider(
            search_reader=unavailable_reader,
            snapshot_session_factory=factory,
        ),
    )
    try:
        handle = service.begin(_request())
        fallback = await service.search(
            handle.session_id,
            domain="wearable",
            capability="wearable.health-scores",
            start=DETAIL_START,
            end=DETAIL_END,
            granularity="record",
            parameters={"category": "recovery"},
        )

        record = fallback.payload["records"][0]
        assert record["provider"] == "unknown"
        assert record["provider_attribution"] == "legacy_missing"
        assert record["provenance"]["upstream_provider"] == (
            "open_wearables"
        )
        assert record["provenance"]["wearable_provider"] == "unknown"
        assert record["provenance"]["provider_attribution"] == (
            "legacy_missing"
        )
        assert fallback.source_refs[0].record_id != str(snapshot.event_id)
        assert fallback.source_refs[0].coverage is None
        assert fallback.source_refs[0].collected_at == snapshot.collected_at
        assert (
            "wearable_provider_attribution_unavailable"
            in fallback.limitations
        )
    finally:
        service.close()
        engine.dispose()


async def test_retained_timeseries_reapplies_resolution_without_merging(
    tmp_path,
) -> None:
    engine, factory = _file_store(
        tmp_path,
        "detail-retained-timeseries-resolution.db",
    )
    with factory() as session:
        persist_open_wearables_query_snapshot(
            session,
            capability="wearable.timeseries",
            start=DETAIL_START,
            end=DETAIL_END,
            timezone="UTC",
            parameters={
                "series_type": "steps",
                "resolution": "1hour",
            },
            result={
                "status": "ok",
                "records": [
                    {
                        "timestamp": "2026-08-16T08:05:37+00:00",
                        "series_type": "steps",
                        "value": 10,
                        "unit": "count",
                        "provider": "apple_health",
                        "provider_attribution": "source_exact_alias",
                    },
                    {
                        "timestamp": "2026-08-16T08:25:49+00:00",
                        "series_type": "steps",
                        "value": 20,
                        "unit": "count",
                        "provider": "apple_health",
                        "provider_attribution": "source_exact_alias",
                    },
                ],
                "coverage": {"ratio": 1.0},
                "limitations": [],
            },
            collected_at=NOW,
            now=NOW,
        )
        session.commit()

    calls = 0

    async def unavailable_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        nonlocal calls
        calls += 1
        raise RuntimeError("upstream unavailable")

    service = _service(
        factory,
        WearableContextProvider(
            search_reader=unavailable_reader,
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
                "series_type": "steps",
                "resolution": "1hour",
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
                "series_type": "steps",
                "resolution": "1hour",
                "cursor": first.next_cursor,
            },
        )

        assert calls == 1
        assert [
            first.payload["records"][0]["value"],
            second.payload["records"][0]["value"],
        ] == [10, 20]
        assert [
            first.payload["records"][0]["timestamp"],
            second.payload["records"][0]["timestamp"],
        ] == [
            "2026-08-16T08:00:00+00:00",
            "2026-08-16T08:00:00+00:00",
        ]
        assert first.status is ContextStatus.PARTIAL
        assert second.status is ContextStatus.PARTIAL
        assert second.coverage.status is CoverageStatus.UNKNOWN
        assert second.coverage.ratio is None
        assert (
            "wearable_stream_attribution_unavailable"
            in second.limitations
        )
    finally:
        service.close()
        engine.dispose()


async def test_retained_timeseries_exact_boundary_is_partial_without_streams(
    tmp_path,
) -> None:
    engine, factory = _file_store(
        tmp_path,
        "detail-retained-timeseries-exact-boundary.db",
    )
    with factory() as session:
        persist_open_wearables_query_snapshot(
            session,
            capability="wearable.timeseries",
            start=DETAIL_START,
            end=DETAIL_END,
            timezone="UTC",
            parameters={
                "series_type": "steps",
                "resolution": "1hour",
            },
            result={
                "status": "ok",
                "records": [
                    {
                        "timestamp": "2026-08-16T08:00:00+00:00",
                        "series_type": "steps",
                        "value": 10,
                        "unit": "count",
                        "provider": "apple_health",
                        "provider_attribution": "source_exact_alias",
                    },
                    {
                        "timestamp": "2026-08-16T08:00:00+00:00",
                        "series_type": "steps",
                        "value": 20,
                        "unit": "count",
                        "provider": "apple_health",
                        "provider_attribution": "source_exact_alias",
                    },
                ],
                "coverage": {"ratio": 1.0},
                "limitations": [],
            },
            collected_at=NOW,
            now=NOW,
        )
        session.commit()

    async def unavailable_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        raise RuntimeError("upstream unavailable")

    service = _service(
        factory,
        WearableContextProvider(
            search_reader=unavailable_reader,
            snapshot_session_factory=factory,
        ),
    )
    try:
        handle = service.begin(_request())
        result = await service.search(
            handle.session_id,
            domain="wearable",
            capability="wearable.timeseries",
            start=DETAIL_START,
            end=DETAIL_END,
            granularity="series",
            parameters={
                "series_type": "steps",
                "resolution": "1hour",
            },
        )

        assert [record["value"] for record in result.payload["records"]] == [
            10,
            20,
        ]
        assert [
            record["timestamp"] for record in result.payload["records"]
        ] == [
            "2026-08-16T08:00:00+00:00",
            "2026-08-16T08:00:00+00:00",
        ]
        assert result.status is ContextStatus.PARTIAL
        assert result.coverage.status is CoverageStatus.UNKNOWN
        assert result.coverage.ratio is None
        assert (
            "wearable_stream_attribution_unavailable"
            in result.limitations
        )
    finally:
        service.close()
        engine.dispose()


async def test_live_exact_boundary_with_verified_streams_stays_complete(
    tmp_path,
) -> None:
    engine, factory = _file_store(
        tmp_path,
        "detail-live-timeseries-exact-boundary.db",
    )

    async def verified_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        return WearableSearchFetch(
            records=(
                {
                    "timestamp": "2026-08-16T08:00:00+00:00",
                    "series_type": "steps",
                    "value": 10,
                    "unit": "count",
                    "provider": "apple_health",
                    "provider_attribution": "source_exact_alias",
                },
            ),
            stream_attribution_unavailable=False,
        )

    service = _service(
        factory,
        WearableContextProvider(
            search_reader=verified_reader,
            snapshot_session_factory=factory,
        ),
    )
    try:
        handle = service.begin(_request())
        result = await service.search(
            handle.session_id,
            domain="wearable",
            capability="wearable.timeseries",
            start=DETAIL_START,
            end=DETAIL_END,
            granularity="series",
            parameters={
                "series_type": "steps",
                "resolution": "1hour",
            },
        )

        assert result.status is ContextStatus.OK
        assert result.coverage.status is CoverageStatus.COMPLETE
        assert result.coverage.ratio == 1
        assert (
            "wearable_stream_attribution_unavailable"
            not in result.limitations
        )
        assert result.payload["provenance_mode"] == (
            "live_upstream_mirrored"
        )
        with factory() as observer:
            event = observer.get(
                WellnessEvent,
                UUID(result.source_refs[0].record_id),
            )
            assert event is not None
            assert (
                event.payload["result"]["stream_attribution_status"]
                == "verified"
            )
    finally:
        service.close()
        engine.dispose()


class UnalignedWindowTimeseriesClient:
    async def get_timeseries(self, _user_id, *_args, **_kwargs):
        return {
            "data": [
                {
                    "timestamp": "2026-08-16T10:10:00Z",
                    "type": "steps",
                    "value": 10,
                    "unit": "count",
                    "provider": "apple",
                    "data_source_id": "trusted-sensor",
                },
                {
                    "timestamp": "2026-08-16T10:45:00Z",
                    "type": "steps",
                    "value": 20,
                    "unit": "count",
                    "provider": "apple",
                    "data_source_id": "trusted-sensor",
                },
                {
                    "timestamp": "2026-08-16T11:02:00Z",
                    "type": "steps",
                    "value": 30,
                    "unit": "count",
                    "provider": "apple",
                    "data_source_id": "trusted-sensor",
                },
            ],
            "pagination": {
                "next_cursor": None,
                "has_more": False,
            },
        }


async def test_live_unaligned_timeseries_stays_inside_query_window(
    tmp_path,
) -> None:
    start = datetime(2026, 8, 16, 10, 5, tzinfo=UTC)
    end = datetime(2026, 8, 16, 11, 5, tzinfo=UTC)
    engine, factory = _file_store(
        tmp_path,
        "detail-live-timeseries-unaligned-window.db",
    )
    search = BoundedOpenWearablesSearch(
        UnalignedWindowTimeseriesClient(),  # type: ignore[arg-type]
        lambda: "private-user-id",
    )
    service = _service(
        factory,
        WearableContextProvider(
            search_reader=search,
            snapshot_session_factory=factory,
        ),
    )
    try:
        handle = service.begin(_request())
        result = await service.search(
            handle.session_id,
            domain="wearable",
            capability="wearable.timeseries",
            start=start,
            end=end,
            granularity="series",
            parameters={
                "series_type": "steps",
                "resolution": "1hour",
            },
        )

        records = result.payload["records"]
        assert [record["value"] for record in records] == [30, 30]
        assert [record["timestamp"] for record in records] == [
            start.isoformat(),
            "2026-08-16T11:00:00+00:00",
        ]
        assert all(
            start
            <= datetime.fromisoformat(record["timestamp"])
            < end
            for record in records
        )
        assert [
            record["provenance"]["observed_at"] for record in records
        ] == [record["timestamp"] for record in records]
        assert result.status is ContextStatus.OK
        assert result.source_refs[0].observed_start == start
        assert result.source_refs[0].observed_end == end
        with factory() as observer:
            event = observer.get(
                WellnessEvent,
                UUID(result.source_refs[0].record_id),
            )
            assert event is not None
            assert event.payload["retention_basis_at"] == start.isoformat()
            assert [
                record["timestamp"]
                for record in event.payload["result"]["records"]
            ] == [
                start.isoformat(),
                "2026-08-16T11:00:00+00:00",
            ]
    finally:
        service.close()
        engine.dispose()


async def test_retained_unaligned_timeseries_stays_inside_query_window(
    tmp_path,
) -> None:
    start = datetime(2026, 8, 16, 10, 5, tzinfo=UTC)
    end = datetime(2026, 8, 16, 11, 5, tzinfo=UTC)
    engine, factory = _file_store(
        tmp_path,
        "detail-retained-timeseries-unaligned-window.db",
    )
    with factory() as session:
        persist_open_wearables_query_snapshot(
            session,
            capability="wearable.timeseries",
            start=start,
            end=end,
            timezone="UTC",
            parameters={
                "series_type": "steps",
                "resolution": "1hour",
            },
            result={
                "status": "ok",
                "records": [
                    {
                        "timestamp": "2026-08-16T10:10:00+00:00",
                        "series_type": "steps",
                        "value": 30,
                        "unit": "count",
                        "provider": "apple_health",
                        "provider_attribution": "declared",
                    },
                    {
                        "timestamp": "2026-08-16T11:02:00+00:00",
                        "series_type": "steps",
                        "value": 30,
                        "unit": "count",
                        "provider": "apple_health",
                        "provider_attribution": "declared",
                    },
                ],
                "stream_attribution_status": "verified",
                "coverage": {"ratio": 1.0},
                "limitations": [],
            },
            collected_at=NOW,
            now=NOW,
        )
        session.commit()

    async def unavailable_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        raise RuntimeError("upstream unavailable")

    service = _service(
        factory,
        WearableContextProvider(
            search_reader=unavailable_reader,
            snapshot_session_factory=factory,
        ),
    )
    try:
        handle = service.begin(_request())
        result = await service.search(
            handle.session_id,
            domain="wearable",
            capability="wearable.timeseries",
            start=start,
            end=end,
            granularity="series",
            parameters={
                "series_type": "steps",
                "resolution": "1hour",
            },
        )

        records = result.payload["records"]
        assert [record["timestamp"] for record in records] == [
            start.isoformat(),
            "2026-08-16T11:00:00+00:00",
        ]
        assert all(
            start
            <= datetime.fromisoformat(record["timestamp"])
            < end
            for record in records
        )
        assert [
            record["provenance"]["observed_at"] for record in records
        ] == [record["timestamp"] for record in records]
        assert result.source_refs[0].observed_start == start
        assert result.source_refs[0].observed_end == end
    finally:
        service.close()
        engine.dispose()


async def test_detail_search_timeout_uses_exact_retained_query(
    tmp_path,
) -> None:
    engine, factory = _file_store(tmp_path, "detail-timeout.db")

    async def initial_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        return WearableSearchFetch(
            records=(
                {
                    "category": "stress",
                    "recorded_at": "2026-08-16T08:00:00+00:00",
                    "provider": "garmin",
                    "value": 42,
                },
            )
        )

    initial_service = _service(
        factory,
        WearableContextProvider(
            search_reader=initial_reader,
            snapshot_session_factory=factory,
        ),
    )
    handle = initial_service.begin(_request())
    initial = await initial_service.search(
        handle.session_id,
        domain="wearable",
        capability="wearable.health-scores",
        start=DETAIL_START,
        end=DETAIL_END,
        granularity="record",
        parameters={"category": "stress"},
    )
    initial_service.finish(handle.session_id)
    initial_service.close()

    async def stalled_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        await asyncio.sleep(1)
        raise AssertionError("timeout must cancel the upstream search")

    fallback_service = _service(
        factory,
        WearableContextProvider(
            search_reader=stalled_reader,
            snapshot_session_factory=factory,
            upstream_timeout_seconds=0.001,
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
            parameters={"category": "stress"},
        )

        assert fallback.status is ContextStatus.PARTIAL
        assert fallback.coverage.status is CoverageStatus.UNKNOWN
        assert fallback.coverage.ratio is None
        assert (
            fallback.source_refs[0].reference_id
            != initial.source_refs[0].reference_id
        )
        assert fallback.source_refs[0].coverage is None
        assert fallback.source_refs[0].collected_at == (
            initial.source_refs[0].collected_at
        )
        assert "open_wearables_detail_timeout" in fallback.limitations
        assert (
            "wearable_query_snapshot_fallback_used"
            in fallback.limitations
        )
        assert (
            fallback.payload["records"][0]["provenance"]["mode"]
            == "retained_local_mirror"
        )
    finally:
        fallback_service.close()
        engine.dispose()


async def test_detail_search_retains_unknown_provider_as_partial(
    session,
) -> None:
    async def search_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        return WearableSearchFetch(
            records=(
                {
                    "category": "stress",
                    "recorded_at": "2026-08-16T08:00:00+00:00",
                    "value": 99,
                },
                {
                    "category": "stress",
                    "recorded_at": "2026-08-16T08:05:00+00:00",
                    "provider": "garmin",
                    "value": 42,
                },
            )
        )

    provider = WearableContextProvider(search_reader=search_reader)
    result = await provider.query(
        session,
        ContextQuery(
            provider_id="wearable",
            capability="wearable.health-scores",
            start=DETAIL_START,
            end=DETAIL_END,
            granularity="record",
            parameters={"category": "stress"},
        ),
        now=NOW,
    )

    assert result.status is ContextStatus.PARTIAL
    assert result.payload["count"] == 2
    assert [
        record["value"] for record in result.payload["records"]
    ] == [99, 42]
    assert result.payload["records"][0]["provider"] == "unknown"
    assert (
        "wearable_provider_attribution_unavailable"
        in result.limitations
    )


async def test_detail_search_degrades_fully_discarded_page_to_partial(
    session,
) -> None:
    async def search_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        return WearableSearchFetch(
            records=(),
            discarded_rows=2,
        )

    provider = WearableContextProvider(search_reader=search_reader)
    result = await provider.query(
        session,
        ContextQuery(
            provider_id="wearable",
            capability="wearable.health-scores",
            start=DETAIL_START,
            end=DETAIL_END,
            granularity="record",
            parameters={"category": "stress"},
        ),
        now=NOW,
    )

    assert result.status is ContextStatus.PARTIAL
    assert result.payload["status"] == "partial"
    assert result.coverage.status is CoverageStatus.UNKNOWN
    assert result.coverage.ratio is None
    assert "wearable_rows_discarded" in result.limitations


async def test_retained_fully_discarded_page_normalizes_legacy_status(
    session,
) -> None:
    persist_open_wearables_query_snapshot(
        session,
        capability="wearable.health-scores",
        start=DETAIL_START,
        end=DETAIL_END,
        timezone="UTC",
        parameters={"category": "stress"},
        result={
            "status": "empty_success",
            "records": [],
            "limitations": ["wearable_rows_discarded"],
        },
        collected_at=NOW,
        now=NOW,
    )
    session.commit()

    async def unavailable_reader(
        _request: WearableSearchRequest,
    ) -> WearableSearchFetch:
        raise RuntimeError("upstream unavailable")

    provider = WearableContextProvider(search_reader=unavailable_reader)
    result = await provider.query(
        session,
        ContextQuery(
            provider_id="wearable",
            capability="wearable.health-scores",
            start=DETAIL_START,
            end=DETAIL_END,
            granularity="record",
            parameters={"category": "stress"},
        ),
        now=NOW,
    )

    assert result.status is ContextStatus.PARTIAL
    assert result.payload["status"] == "partial"
    assert result.coverage.status is CoverageStatus.UNKNOWN
    assert result.coverage.ratio is None
    assert "wearable_rows_discarded" in result.limitations


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
