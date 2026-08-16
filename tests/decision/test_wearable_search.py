from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

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
from healthmes.storage import update_retention_policy
from healthmes.store import Base, WellnessEvent, create_db_engine
from healthmes.wearables.provenance import (
    OPEN_WEARABLES_OBSERVATION_EVENT_TYPE,
    OPEN_WEARABLES_QUERY_EVENT_TYPE,
    OPEN_WEARABLES_SNAPSHOT_RETENTION_CLASS,
    persist_open_wearables_query_snapshot,
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
    event = session.get(
        WellnessEvent,
        UUID(result.source_refs[0].record_id),
    )
    assert event is not None
    assert event.payload["retention_basis_at"] == observed_at.isoformat()
    assert event.expires_at.replace(tzinfo=UTC) == (
        observed_at + timedelta(days=30)
    )


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
    assert calls[0].start == NOW - timedelta(days=7)
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


async def test_legacy_private_source_does_not_change_public_cursor(
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

    assert first == second


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
        assert fallback.source_refs == initial.source_refs
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
            == initial_record["provenance"]["source_ref_id"]
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
        assert fallback.source_refs[0].record_id == str(
            snapshot.event_id
        )
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

        assert fallback.status is not ContextStatus.FAILED
        assert fallback.source_refs == initial.source_refs
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
