import json
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from healthmes.calendars import creds
from healthmes.calendars.state import (
    InMemorySyncHealthStore,
    SyncCoverageKind,
)
from healthmes.config import Settings
from healthmes.decision import (
    ActivityContextProvider,
    CalendarContextProvider,
    ContextCapability,
    ContextCoverage,
    ContextFreshness,
    ContextProviderMetadata,
    ContextProviderRegistry,
    ContextQuery,
    ContextResult,
    ContextStatus,
    CoverageStatus,
    DisabledProviderError,
    DuplicateCapabilityError,
    DuplicateProviderError,
    FreshnessStatus,
    NutritionContextProvider,
    UnknownCapabilityError,
    UnknownProviderError,
    WearableContextProvider,
    build_context_provider_registry,
)
from healthmes.store import CalendarEventMirror, WellnessEvent
from healthmes.store.enums import CalendarSource

NOW = datetime(2026, 8, 10, 12, tzinfo=UTC)
DAY_START = datetime(2026, 8, 10, tzinfo=UTC)
DAY_END = DAY_START + timedelta(days=1)


def _calendar_settings(tmp_path) -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        data_dir=tmp_path / "data",
        timezone="UTC",
        _env_file=None,
    )


def _connect_google(
    settings: Settings,
    *,
    refresh_token: str,
) -> str:
    path = settings.data_dir / "google" / "calendar_token.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "authorized_user",
                "refresh_token": refresh_token,
                "client_id": "client-id",
                "client_secret": "client-secret",
            }
        ),
        encoding="utf-8",
    )
    generation = creds.calendar_account_generation(
        settings,
        CalendarSource.GOOGLE,
    )
    assert generation is not None
    return generation


def _wellness_event(
    *,
    event_type: str,
    source_provider: str,
    source_record_id: str,
    observed_at: datetime = DAY_START,
    recorded_at: datetime = NOW,
    payload: dict | None = None,
) -> WellnessEvent:
    return WellnessEvent(
        event_type=event_type,
        schema_version=1,
        observed_at=observed_at,
        recorded_at=recorded_at,
        timezone="UTC",
        source_provider=source_provider,
        source_device="test-device",
        source_record_id=source_record_id,
        capture_method="test",
        quality_flags={},
        confidence=1.0,
        coverage=1.0,
        sensitivity="wellness",
        consent_scope="personal",
        expires_at=None,
        payload=payload or {},
        derived_from=None,
    )


class _FifthProvider:
    metadata = ContextProviderMetadata(
        provider_id="mood",
        domain="mood",
        description="Test-only fifth provider.",
        capabilities=(
            ContextCapability(
                capability="mood.summary",
                description="Test mood summary.",
                granularities=("summary",),
                output_fields=("score",),
                max_lookback_days=1,
                sensitivity="mood",
                freshness_expectation="Test snapshot.",
            ),
        ),
    )

    async def query(self, session, query, *, now):
        del session
        return ContextResult(
            query_id=query.query_id,
            provider_id=query.provider_id,
            capability=query.capability,
            status=ContextStatus.OK,
            payload={"score": 7},
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


def test_default_registry_discovers_four_domains_and_metadata():
    registry = build_context_provider_registry()

    descriptors = registry.discover()

    assert [item.metadata.provider_id for item in descriptors] == [
        "activity",
        "calendar",
        "nutrition",
        "wearable",
    ]
    assert {
        item.metadata.domain for item in descriptors
    } == {"activity", "calendar", "nutrition", "wearable"}
    descriptor, capability = registry.capability("activity.focus")
    assert descriptor.enabled is True
    assert capability.max_lookback_days == 1
    assert capability.supports_raw is False
    assert capability.sensitivity == "activity-aggregate"


async def test_fifth_provider_registers_and_executes_without_central_routing(
    session,
):
    registry = build_context_provider_registry()
    registry.register(_FifthProvider())
    query = ContextQuery(
        provider_id="mood",
        capability="mood.summary",
    )

    result = await registry.execute(session, query, now=NOW)

    assert result.status is ContextStatus.OK
    assert result.payload == {"score": 7}
    assert registry.discover(domain="mood")[0].metadata.provider_id == "mood"


def test_registry_rejects_duplicate_provider_and_capability():
    registry = ContextProviderRegistry((_FifthProvider(),))

    with pytest.raises(DuplicateProviderError):
        registry.register(_FifthProvider())

    class CollisionProvider:
        metadata = ContextProviderMetadata(
            provider_id="mood-secondary",
            domain="mood",
            description="Collision.",
            capabilities=(
                ContextCapability(
                    capability="mood.summary",
                    description="Collision.",
                    granularities=("summary",),
                    max_lookback_days=1,
                    sensitivity="mood",
                    freshness_expectation="Test.",
                ),
            ),
        )

        async def query(self, session, query, *, now):
            raise AssertionError

    with pytest.raises(DuplicateCapabilityError):
        registry.register(CollisionProvider())


async def test_unknown_disabled_and_wrong_capability_fail_closed(session):
    registry = ContextProviderRegistry((_FifthProvider(),))
    registry.set_enabled("mood", enabled=False)

    with pytest.raises(DisabledProviderError):
        await registry.execute(
            session,
            ContextQuery(
                provider_id="mood",
                capability="mood.summary",
            ),
            now=NOW,
        )
    with pytest.raises(UnknownProviderError):
        registry.descriptor("missing")
    with pytest.raises(UnknownCapabilityError):
        registry.capability("missing.summary")
    with pytest.raises(UnknownProviderError):
        await registry.execute(
            session,
            ContextQuery(
                provider_id="missing",
                capability="missing.summary",
            ),
            now=NOW,
        )


async def test_provider_exception_is_normalized_without_leaking_details(
    session,
):
    class BrokenProvider(_FifthProvider):
        async def query(self, session, query, *, now):
            raise RuntimeError("secret upstream detail")

    registry = ContextProviderRegistry((BrokenProvider(),))

    result = await registry.execute(
        session,
        ContextQuery(
            provider_id="mood",
            capability="mood.summary",
        ),
        now=NOW,
    )

    assert result.status is ContextStatus.FAILED
    assert result.payload == {}
    assert result.limitations == ["provider_execution_failed"]
    assert "secret" not in result.model_dump_json()


async def test_activity_adapter_returns_typed_stable_wellness_source_ref(
    session,
    monkeypatch,
):
    event = _wellness_event(
        event_type="activity.day-summary.v1",
        source_provider="healthmes-activity",
        source_record_id="activity-day-2026-08-10",
        payload={
            "window": {
                "start": DAY_START.isoformat(),
                "end": DAY_END.isoformat(),
            }
        },
    )
    session.add(event)
    session.flush()

    def summary(*args, **kwargs):
        return {
            "status": "ok",
            "date": "2026-08-10",
            "timezone": "UTC",
            "total_active_minutes": 120,
            "source_coverage": {"ratio": 1.0},
            "evidence_ids": [str(event.id)],
            "freshness": {
                "recorded_at": NOW.isoformat(),
                "status": "stored_summary",
            },
            "limitations": [],
        }

    monkeypatch.setattr(
        "healthmes.decision.domain_providers.activity_summary_context",
        summary,
    )
    registry = ContextProviderRegistry((ActivityContextProvider(),))
    query = ContextQuery(
        provider_id="activity",
        capability="activity.summary",
        parameters={"date": "2026-08-10"},
    )

    result = await registry.execute(session, query, now=NOW)

    assert result.status is ContextStatus.OK
    assert result.payload["total_active_minutes"] == 120
    assert result.coverage.status is CoverageStatus.COMPLETE
    assert len(result.source_refs) == 1
    assert result.source_refs[0].record_id == str(event.id)
    assert result.source_refs[0].source_provider == "healthmes-activity"
    assert result.source_refs[0].observed_end == DAY_END
    assert result.source_refs[0].collected_at == NOW
    assert result.observed_start == DAY_START
    assert result.observed_end == DAY_END
    assert result.collected_at == NOW


async def test_activity_event_id_never_falls_back_to_source_record_id(
    session,
    monkeypatch,
):
    missing_event_id = str(uuid.uuid4())
    decoy = _wellness_event(
        event_type="activity.day-summary.v1",
        source_provider="healthmes-activity",
        source_record_id=missing_event_id,
        payload={
            "window": {
                "start": DAY_START.isoformat(),
                "end": DAY_END.isoformat(),
            }
        },
    )
    session.add(decoy)
    session.flush()

    monkeypatch.setattr(
        "healthmes.decision.domain_providers.activity_summary_context",
        lambda *args, **kwargs: {
            "status": "ok",
            "date": "2026-08-10",
            "timezone": "UTC",
            "total_active_minutes": 120,
            "source_coverage": {"ratio": 1.0},
            "evidence_ids": [missing_event_id],
            "freshness": {
                "recorded_at": NOW.isoformat(),
                "status": "stored_summary",
            },
            "limitations": [],
        },
    )
    registry = ContextProviderRegistry((ActivityContextProvider(),))

    result = await registry.execute(
        session,
        ContextQuery(
            provider_id="activity",
            capability="activity.summary",
            parameters={"date": "2026-08-10"},
        ),
        now=NOW,
    )

    assert result.source_refs == []
    assert result.limitations == ["provenance_incomplete"]


async def test_nutrition_adapter_returns_only_structured_capture_context(
    session,
    monkeypatch,
):
    interaction_id = str(uuid.uuid4())
    id_collision = _wellness_event(
        event_type="nutrition.interaction.v1",
        source_provider="nutrition-interaction",
        source_record_id="different-interaction",
    )
    id_collision.id = uuid.UUID(interaction_id)
    raw_event = _wellness_event(
        event_type="nutrition.raw-capture.v1",
        source_provider="nutrition-raw-capture",
        source_record_id=interaction_id,
    )
    structured_event = _wellness_event(
        event_type="nutrition.interaction.v1",
        source_provider="nutrition-interaction",
        source_record_id=interaction_id,
    )
    session.add_all((id_collision, raw_event, structured_event))
    session.flush()

    def history(*args, **kwargs):
        return {
            "status": "ok",
            "count": 1,
            "records": [
                {
                    "interaction_id": interaction_id,
                    "observed_at": DAY_START.isoformat(),
                    "recorded_at": NOW.isoformat(),
                    "source_text": "private meal description",
                    "media_path": "/private/image.jpg",
                    "resolved_items": [{"name": "meal", "nutrients": []}],
                }
            ],
            "truncated": False,
            "coverage": {
                "complete": True,
                "matching_records": 1,
            },
        }

    monkeypatch.setattr(
        "healthmes.decision.domain_providers.search_intake_history",
        history,
    )
    registry = ContextProviderRegistry((NutritionContextProvider(),))
    query = ContextQuery(
        provider_id="nutrition",
        capability="nutrition.intake-history",
        start=DAY_START,
        end=DAY_END,
    )

    result = await registry.execute(session, query, now=NOW)

    assert result.status is ContextStatus.OK
    assert result.coverage.status is CoverageStatus.COMPLETE
    assert len(result.source_refs) == 1
    assert result.source_refs[0].record_id == str(structured_event.id)
    assert result.source_refs[0].record_id != str(raw_event.id)
    assert result.source_refs[0].record_id != str(id_collision.id)
    record = result.payload["records"][0]
    assert "source_text" not in record
    assert "media_path" not in record
    assert record["resolved_items"][0]["name"] == "meal"


async def test_nutrition_decision_event_id_never_uses_semantic_fallback(
    session,
    monkeypatch,
):
    request_id = uuid.uuid4()
    missing_event_id = str(uuid.uuid4())
    decoy = _wellness_event(
        event_type="nutrition.decision-request.v1",
        source_provider="nutrition-decision-request",
        source_record_id=missing_event_id,
    )
    session.add(decoy)
    session.flush()

    monkeypatch.setattr(
        "healthmes.decision.domain_providers.nutrition_decision_context",
        lambda *args, **kwargs: {
            "status": "ok",
            "request": {
                "request_id": str(request_id),
                "scope": "daily_nutrition",
                "requested_at": NOW.isoformat(),
            },
            "candidate": {"is_confirmed_intake": False},
            "comparison_candidates": [],
            "confirmed_intake_history": [],
            "history_window": {
                "start": DAY_START.isoformat(),
                "end": NOW.isoformat(),
                "lookback_days": 1,
                "query": {"complete": True},
            },
            "specialized_evidence": {"caffeine": None},
            "evidence_event_ids": [missing_event_id],
            "boundaries": {},
        },
    )
    registry = ContextProviderRegistry((NutritionContextProvider(),))

    result = await registry.execute(
        session,
        ContextQuery(
            provider_id="nutrition",
            capability="nutrition.decision-context",
            start=DAY_START,
            end=NOW,
            parameters={"request_id": str(request_id)},
        ),
        now=NOW,
    )

    assert result.source_refs == []
    assert result.limitations == ["provenance_incomplete"]


async def test_nutrition_provider_rejects_string_boolean_parameter(
    session,
):
    registry = ContextProviderRegistry((NutritionContextProvider(),))

    result = await registry.execute(
        session,
        ContextQuery(
            provider_id="nutrition",
            capability="nutrition.intake-history",
            start=DAY_START,
            end=NOW,
            parameters={"confirmed_only": "false"},
        ),
        now=NOW,
    )

    assert result.status is ContextStatus.FAILED
    assert result.limitations == ["invalid_provider_query"]


async def test_nutrition_freshness_uses_absolute_time_across_offsets(
    session,
    monkeypatch,
):
    earlier_id = str(uuid.uuid4())
    later_id = str(uuid.uuid4())
    earlier_event = _wellness_event(
        event_type="nutrition.interaction.v1",
        source_provider="nutrition-interaction",
        source_record_id=earlier_id,
    )
    later_event = _wellness_event(
        event_type="nutrition.interaction.v1",
        source_provider="nutrition-interaction",
        source_record_id=later_id,
    )
    session.add_all((earlier_event, later_event))
    session.flush()

    monkeypatch.setattr(
        "healthmes.decision.domain_providers.search_intake_history",
        lambda *args, **kwargs: {
            "status": "ok",
            "count": 2,
            "records": [
                {
                    "interaction_id": earlier_id,
                    "observed_at": DAY_START.isoformat(),
                    "recorded_at": "2026-08-10T09:30:00+09:00",
                    "resolved_items": [],
                    "is_confirmed_intake": False,
                },
                {
                    "interaction_id": later_id,
                    "observed_at": DAY_START.isoformat(),
                    "recorded_at": "2026-08-10T01:00:00+00:00",
                    "resolved_items": [],
                    "is_confirmed_intake": False,
                },
            ],
            "truncated": False,
            "coverage": {"complete": True},
        },
    )
    registry = ContextProviderRegistry((NutritionContextProvider(),))

    result = await registry.execute(
        session,
        ContextQuery(
            provider_id="nutrition",
            capability="nutrition.intake-history",
            start=DAY_START,
            end=DAY_END,
        ),
        now=NOW,
    )

    assert result.freshness.as_of == datetime(
        2026,
        8,
        10,
        1,
        tzinfo=UTC,
    )


async def test_wearable_adapter_normalizes_upstream_source_reference(session):
    async def reader(day: date):
        return {
            "status": "ok",
            "date": day.isoformat(),
            "stress": {
                "status": "ok",
                "value": 42,
                "recorded_at": "2026-08-10T08:00:00+00:00",
            },
            "source_refs": [
                {
                    "domain": "wearable",
                    "record_id": "score-1",
                    "source_provider": "open-wearables",
                    "upstream_provider": "garmin",
                    "resource_type": "health_score",
                    "observed_at": "2026-08-10T08:00:00+00:00",
                    "schema_version": 1,
                    "derived_by": "open-wearables.daily-readiness.v1",
                }
            ],
            "freshness": {
                "recorded_at": "2026-08-10T08:00:00+00:00",
                "status": "derived_from_readiness_blocks",
            },
            "coverage": {
                "status": "readiness_blocks",
                "ratio": 1.0,
            },
            "limitations": [],
        }

    snapshot_factory = sessionmaker(
        bind=session.get_bind(),
        expire_on_commit=False,
    )
    registry = ContextProviderRegistry(
        (
            WearableContextProvider(
                reader,
                snapshot_session_factory=snapshot_factory,
            ),
        )
    )
    query = ContextQuery(
        provider_id="wearable",
        capability="wearable.stress",
        parameters={"date": "2026-08-10"},
    )

    result = await registry.execute(session, query, now=NOW)

    assert result.status is ContextStatus.OK
    assert result.payload["stress"]["value"] == 42
    assert len(result.source_refs) == 1
    assert result.source_refs[0].record_id != "score-1"
    assert (
        result.source_refs[0].source_provider
        == "healthmes-open-wearables-mirror"
    )
    assert (
        result.source_refs[0].resource_type
        == "wearable.open-wearables-observation.v1"
    )
    assert result.source_refs[0].collected_at == NOW
    assert "wearable_source_refs_are_readiness_level" in result.limitations


async def test_calendar_adapter_returns_merged_aggregate_without_titles(
    session,
):
    row = CalendarEventMirror(
        external_id="meeting-1",
        calendar_source=CalendarSource.GOOGLE,
        summary="Private meeting title",
        start_at=datetime(2026, 8, 10, 9, tzinfo=UTC),
        end_at=datetime(2026, 8, 10, 10, tzinfo=UTC),
        is_all_day=False,
    )
    session.add(row)
    session.flush()
    registry = ContextProviderRegistry(
        (
            CalendarContextProvider(
                sources=(CalendarSource.GOOGLE,),
            ),
        )
    )
    query = ContextQuery(
        provider_id="calendar",
        capability="calendar.busy-intervals",
        start=datetime(2026, 8, 10, 8, tzinfo=UTC),
        end=datetime(2026, 8, 10, 12, tzinfo=UTC),
        granularity="window",
    )

    result = await registry.execute(session, query, now=NOW)

    assert result.status is ContextStatus.OK
    assert result.payload["busy_minutes"] == 60
    assert result.payload["intervals"] == [
        {
            "start": "2026-08-10T09:00:00+00:00",
            "end": "2026-08-10T10:00:00+00:00",
        }
    ]
    assert "Private meeting title" not in result.model_dump_json()
    assert result.source_refs[0].record_id == str(row.id)


async def test_calendar_dynamic_connection_filters_and_revokes_mirror_rows(
    session,
):
    google = CalendarEventMirror(
        external_id="connected-google",
        calendar_source=CalendarSource.GOOGLE,
        summary="Connected calendar title",
        start_at=datetime(2026, 8, 10, 9, tzinfo=UTC),
        end_at=datetime(2026, 8, 10, 10, tzinfo=UTC),
        is_all_day=False,
    )
    caldav = CalendarEventMirror(
        external_id="disconnected-caldav",
        calendar_source=CalendarSource.CALDAV,
        summary="Disconnected calendar title",
        start_at=datetime(2026, 8, 10, 10, tzinfo=UTC),
        end_at=datetime(2026, 8, 10, 11, tzinfo=UTC),
        is_all_day=False,
    )
    session.add_all((google, caldav))
    session.flush()
    connected = {CalendarSource.GOOGLE}
    provider = CalendarContextProvider(
        source_resolver=lambda: tuple(connected),
    )
    registry = ContextProviderRegistry((provider,))
    query = ContextQuery(
        provider_id="calendar",
        capability="calendar.day-summary",
        parameters={"date": "2026-08-10"},
    )

    result = await registry.execute(session, query, now=NOW)

    assert result.status is ContextStatus.OK
    assert result.payload["event_count"] == 1
    assert [ref.record_id for ref in result.source_refs] == [
        str(google.id)
    ]
    assert str(caldav.id) not in result.model_dump_json()

    connected.clear()
    revoked = await registry.execute(session, query, now=NOW)

    assert revoked.status is ContextStatus.UNAVAILABLE
    assert revoked.payload == {}
    assert revoked.source_refs == []
    assert revoked.limitations == ["calendar_not_connected"]


async def test_calendar_connection_resolver_failure_is_unavailable(session):
    def fail() -> tuple[CalendarSource, ...]:
        raise OSError("credential state unavailable")

    provider = CalendarContextProvider(source_resolver=fail)

    result = await ContextProviderRegistry((provider,)).execute(
        session,
        ContextQuery(
            provider_id="calendar",
            capability="calendar.day-summary",
            parameters={"date": "2026-08-10"},
        ),
        now=NOW,
    )

    assert result.status is ContextStatus.UNAVAILABLE
    assert result.payload == {}
    assert result.source_refs == []
    assert result.limitations == [
        "calendar_connection_state_unavailable"
    ]


async def test_calendar_provider_requires_first_sync_and_current_generation(
    session,
    tmp_path,
):
    settings = _calendar_settings(tmp_path)
    first_generation = _connect_google(
        settings,
        refresh_token="first-refresh-token",
    )
    first = CalendarEventMirror(
        external_id="first-account-event",
        calendar_source=CalendarSource.GOOGLE,
        connection_generation=first_generation,
        summary="First account private title",
        start_at=datetime(2026, 8, 10, 9, tzinfo=UTC),
        end_at=datetime(2026, 8, 10, 10, tzinfo=UTC),
        is_all_day=False,
    )
    session.add(first)
    session.flush()
    health = InMemorySyncHealthStore()
    provider = CalendarContextProvider(
        settings=settings,
        sync_health_store=health,
    )
    registry = ContextProviderRegistry((provider,))
    query = ContextQuery(
        provider_id="calendar",
        capability="calendar.day-summary",
        parameters={"date": "2026-08-10"},
    )

    before_sync = await registry.execute(session, query, now=NOW)

    assert before_sync.status is ContextStatus.UNAVAILABLE
    assert before_sync.source_refs == []
    assert before_sync.limitations == ["calendar_account_not_synced"]

    health.record_success(
        CalendarSource.GOOGLE,
        NOW,
        event_count=1,
        coverage_kind=SyncCoverageKind.FULL_COLLECTION,
        account_generation=first_generation,
    )
    visible = await registry.execute(session, query, now=NOW)

    assert visible.payload["event_count"] == 1
    assert [ref.record_id for ref in visible.source_refs] == [
        str(first.id)
    ]

    second_generation = _connect_google(
        settings,
        refresh_token="second-refresh-token",
    )
    assert second_generation != first_generation
    after_reconnect = await registry.execute(session, query, now=NOW)

    assert after_reconnect.status is ContextStatus.UNAVAILABLE
    assert after_reconnect.source_refs == []
    assert after_reconnect.limitations == [
        "calendar_account_not_synced"
    ]

    second = CalendarEventMirror(
        external_id="second-account-event",
        calendar_source=CalendarSource.GOOGLE,
        connection_generation=second_generation,
        summary="Second account private title",
        start_at=datetime(2026, 8, 10, 11, tzinfo=UTC),
        end_at=datetime(2026, 8, 10, 12, tzinfo=UTC),
        is_all_day=False,
    )
    session.add(second)
    session.flush()
    health.record_success(
        CalendarSource.GOOGLE,
        NOW + timedelta(minutes=1),
        event_count=1,
        coverage_kind=SyncCoverageKind.FULL_COLLECTION,
        account_generation=second_generation,
    )
    reconnected = await registry.execute(session, query, now=NOW)

    assert reconnected.payload["event_count"] == 1
    assert [ref.record_id for ref in reconnected.source_refs] == [
        str(second.id)
    ]
    assert str(first.id) not in reconnected.model_dump_json()


async def test_calendar_empty_success_is_distinct_from_never_synced(
    session,
):
    health = InMemorySyncHealthStore()
    health.record_success(
        CalendarSource.GOOGLE,
        NOW - timedelta(minutes=1),
        event_count=0,
        coverage_kind=SyncCoverageKind.BOUNDED_WINDOW,
        coverage_start=DAY_START - timedelta(days=9),
        coverage_end=DAY_END + timedelta(days=20),
    )
    provider = CalendarContextProvider(
        sync_health_store=health,
        sources=(CalendarSource.GOOGLE,),
    )
    registry = ContextProviderRegistry((provider,))

    result = await registry.execute(
        session,
        ContextQuery(
            provider_id="calendar",
            capability="calendar.day-summary",
            parameters={"date": "2026-08-10"},
        ),
        now=NOW,
    )

    assert result.status is ContextStatus.PARTIAL
    assert result.payload["status"] == "empty_success"
    assert result.payload["event_count"] == 0
    assert result.coverage.status is CoverageStatus.COMPLETE
    assert result.coverage.ratio == 1
    assert result.freshness.as_of == NOW - timedelta(minutes=1)
    assert result.observed_start == DAY_START
    assert result.observed_end == DAY_END
    assert result.collected_at == NOW - timedelta(minutes=1)
    assert "calendar_never_synced" not in result.limitations
    assert "calendar_mirror_completeness_unknown" not in result.limitations
    assert "calendar_query_outside_sync_coverage" not in result.limitations


async def test_calendar_empty_query_outside_sync_coverage_is_not_complete(
    session,
):
    health = InMemorySyncHealthStore()
    health.record_success(
        CalendarSource.GOOGLE,
        NOW - timedelta(minutes=1),
        event_count=0,
        coverage_kind=SyncCoverageKind.BOUNDED_WINDOW,
        coverage_start=DAY_START,
        coverage_end=DAY_END,
    )
    provider = CalendarContextProvider(
        sync_health_store=health,
        sources=(CalendarSource.GOOGLE,),
    )

    result = await ContextProviderRegistry((provider,)).execute(
        session,
        ContextQuery(
            provider_id="calendar",
            capability="calendar.day-summary",
            parameters={"date": "2026-07-01"},
        ),
        now=NOW,
    )

    assert result.status is ContextStatus.PARTIAL
    assert result.payload["status"] == "insufficient_data"
    assert result.coverage.status is CoverageStatus.PARTIAL
    assert result.coverage.ratio == 0
    assert result.observed_start is None
    assert result.observed_end is None
    assert "calendar_query_outside_sync_coverage" in result.limitations


async def test_calendar_multi_source_freshness_uses_oldest_watermark(
    session,
):
    health = InMemorySyncHealthStore()
    google_success = NOW - timedelta(minutes=1)
    caldav_success = NOW - timedelta(days=3)
    for source, succeeded_at in (
        (CalendarSource.GOOGLE, google_success),
        (CalendarSource.CALDAV, caldav_success),
    ):
        health.record_success(
            source,
            succeeded_at,
            event_count=0,
            coverage_kind=SyncCoverageKind.FULL_COLLECTION,
        )
    provider = CalendarContextProvider(
        sync_health_store=health,
        sources=(CalendarSource.GOOGLE, CalendarSource.CALDAV),
    )

    result = await ContextProviderRegistry((provider,)).execute(
        session,
        ContextQuery(
            provider_id="calendar",
            capability="calendar.day-summary",
            parameters={"date": "2026-08-10"},
        ),
        now=NOW,
    )

    assert result.payload["status"] == "empty_success"
    assert result.coverage.status is CoverageStatus.COMPLETE
    assert result.freshness.as_of == caldav_success
    assert result.collected_at == caldav_success


async def test_calendar_never_synced_does_not_claim_an_empty_day(
    session,
):
    provider = CalendarContextProvider(
        sync_health_store=InMemorySyncHealthStore(),
        sources=(CalendarSource.GOOGLE,),
    )
    registry = ContextProviderRegistry((provider,))

    result = await registry.execute(
        session,
        ContextQuery(
            provider_id="calendar",
            capability="calendar.day-summary",
            parameters={"date": "2026-08-10"},
        ),
        now=NOW,
    )

    assert result.status is ContextStatus.PARTIAL
    assert result.payload["status"] == "insufficient_data"
    assert result.coverage.status is CoverageStatus.UNKNOWN
    assert result.freshness.status is FreshnessStatus.UNAVAILABLE
    assert "calendar_never_synced" in result.limitations


async def test_calendar_recent_failure_marks_retained_rows_partial(
    session,
):
    row = CalendarEventMirror(
        external_id="meeting-after-failure",
        calendar_source=CalendarSource.GOOGLE,
        summary="Private retained meeting",
        start_at=datetime(2026, 8, 10, 9, tzinfo=UTC),
        end_at=datetime(2026, 8, 10, 10, tzinfo=UTC),
        is_all_day=False,
    )
    session.add(row)
    session.flush()
    health = InMemorySyncHealthStore()
    health.record_success(
        CalendarSource.GOOGLE,
        NOW - timedelta(minutes=10),
        event_count=1,
        coverage_kind=SyncCoverageKind.FULL_COLLECTION,
    )
    health.record_failure(
        CalendarSource.GOOGLE,
        NOW - timedelta(minutes=1),
        error_code="calendar_timeout",
    )
    provider = CalendarContextProvider(
        sync_health_store=health,
        sources=(CalendarSource.GOOGLE,),
    )
    registry = ContextProviderRegistry((provider,))

    result = await registry.execute(
        session,
        ContextQuery(
            provider_id="calendar",
            capability="calendar.day-summary",
            parameters={"date": "2026-08-10"},
        ),
        now=NOW,
    )

    assert result.status is ContextStatus.PARTIAL
    assert result.payload["event_count"] == 1
    assert result.source_refs[0].record_id == str(row.id)
    assert result.freshness.as_of == NOW - timedelta(minutes=10)
    assert "calendar_recent_sync_failure" in result.limitations
    assert "Private retained meeting" not in result.model_dump_json()


async def test_adapter_rejects_undeclared_fields_as_failed_context(session):
    registry = ContextProviderRegistry((ActivityContextProvider(),))

    result = await registry.execute(
        session,
        ContextQuery(
            provider_id="activity",
            capability="activity.summary",
            fields=["window_title"],
            parameters={"date": "2026-08-10"},
        ),
        now=NOW,
    )

    assert result.status is ContextStatus.FAILED
    assert result.limitations == ["invalid_provider_query"]
