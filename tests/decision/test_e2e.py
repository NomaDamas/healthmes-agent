from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from freezegun import freeze_time
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from healthmes.activity.contracts import (
    ActivityBatchIn,
    ActivityCapability,
    ActivityPlatform,
    ActivityState,
    AppIntervalRecord,
)
from healthmes.activity.service import ingest_activity_batch
from healthmes.calendars.state import (
    FileSyncHealthStore,
    SyncCoverageKind,
)
from healthmes.config import Settings
from healthmes.decision import (
    ContextAccessLayer,
    ContextAccessPolicy,
    ContextStatus,
    DatabaseDecisionPolicyResolver,
    DecisionCaller,
    DecisionDraft,
    DecisionFinalizer,
    DecisionPersistenceIntent,
    DecisionRequest,
    DecisionStatus,
    DomainAccessGrant,
    ExecutionScope,
    HealthMesDecisionEngine,
    PersistenceStatus,
    RuntimeMetadata,
    RuntimeStepOutput,
    build_context_provider_registry,
    ensure_decision_domain_policies,
    update_decision_domain_policy,
)
from healthmes.decision.agent import HealthMesDecisionAgent
from healthmes.nutrition.contracts import (
    Confidence,
    DailyIntakeConfirmation,
    Estimate,
    EstimateKind,
)
from healthmes.nutrition.intake_contracts import (
    CaptureModality,
    EvidenceOrigin,
    IntakeIntent,
    IntakeInteraction,
    IntakeOutcome,
    IntakeOutcomeStatus,
    NormalizedIntakeItem,
    NutrientFact,
)
from healthmes.nutrition.intake_service import (
    create_interaction,
    persist_outcome,
)
from healthmes.nutrition.repository import persist_daily_confirmation
from healthmes.store import (
    Base,
    CalendarEventMirror,
    CalendarSource,
    DecisionRecord,
    create_db_engine,
)
from healthmes.wearables.provenance import (
    persist_open_wearables_observation,
)

NOW = datetime(2026, 8, 12, 6, tzinfo=UTC)
DAY = date(2026, 8, 12)
DAY_START = datetime(2026, 8, 12, tzinfo=UTC)
QUESTION = (
    "오늘 집중이 흐트러지고 피곤한데, "
    "100mg 카페인 커피를 더 마시면서 계속 일해도 될까?"
)
CALENDAR_ACCOUNT_GENERATION = "a" * 32


def _policy() -> ContextAccessPolicy:
    return ContextAccessPolicy(
        owner_principal_id="owner",
        grants=tuple(
            DomainAccessGrant(domain=domain)
            for domain in (
                "activity",
                "nutrition",
                "wearable",
                "calendar",
            )
        ),
    )


def _request() -> DecisionRequest:
    return DecisionRequest(
        question=QUESTION,
        requested_at=NOW,
        timezone="UTC",
        caller=DecisionCaller(
            principal_id="owner",
            authenticated=True,
            execution_scope=ExecutionScope.LOCAL,
        ),
    )


def _build_stepwise_test_engine(
    *,
    runtime,
    session_factory,
    policy_resolver,
    calendar_sync_health_store=None,
    calendar_sources=(),
    calendar_source_resolver=None,
    calendar_account_generation_resolver=None,
    timeout_seconds=60,
    clock=None,
) -> HealthMesDecisionEngine:
    """Compose the legacy stepwise loop only for deterministic core tests."""

    registry = build_context_provider_registry(
        session_factory=session_factory,
        calendar_sync_health_store=calendar_sync_health_store,
        calendar_sources=calendar_sources,
        calendar_source_resolver=calendar_source_resolver,
        calendar_account_generation_resolver=(
            calendar_account_generation_resolver
        ),
    )
    access_layer = ContextAccessLayer(
        registry,
        clock=clock,
        calendar_sync_health_store=calendar_sync_health_store,
        calendar_sources=calendar_sources,
        calendar_source_resolver=calendar_source_resolver,
        calendar_account_generation_resolver=(
            calendar_account_generation_resolver
        ),
    )
    return HealthMesDecisionEngine(
        agent=HealthMesDecisionAgent(
            access_layer=access_layer,
            runtime=runtime,
            session_factory=session_factory,
            policy_resolver=policy_resolver,
            timeout_seconds=timeout_seconds,
            clock=clock,
        ),
        finalizer=DecisionFinalizer(
            access_layer=access_layer,
            session_factory=session_factory,
            policy_resolver=policy_resolver,
            clock=clock,
        ),
    )


def _persistence(tmp_path, name: str):
    database_url = f"sqlite+pysqlite:///{tmp_path / name}"
    db_engine = create_db_engine(database_url)
    Base.metadata.create_all(db_engine)
    return (
        database_url,
        db_engine,
        sessionmaker(bind=db_engine, expire_on_commit=False),
    )


def _seed_activity(session: Session) -> None:
    intervals = (
        (0, 0, 1, 0, ActivityState.ACTIVE, 10),
        (1, 0, 1, 15, ActivityState.IDLE, 0),
        (1, 15, 2, 0, ActivityState.ACTIVE, 15),
        (2, 0, 2, 5, ActivityState.IDLE, 0),
        (2, 5, 3, 0, ActivityState.ACTIVE, 15),
        (3, 0, 3, 10, ActivityState.LOCKED, 0),
        (3, 10, 4, 0, ActivityState.ACTIVE, 15),
        (4, 0, 4, 10, ActivityState.IDLE, 0),
        (4, 10, 5, 0, ActivityState.ACTIVE, 15),
        (5, 0, 6, 0, ActivityState.IDLE, 0),
    )
    records = []
    for index, (
        start_hour,
        start_minute,
        end_hour,
        end_minute,
        state,
        launches,
    ) in enumerate(intervals):
        records.append(
            AppIntervalRecord(
                source_record_id=f"decision-e2e-activity-{index}",
                start_at=DAY_START
                + timedelta(hours=start_hour, minutes=start_minute),
                end_at=DAY_START
                + timedelta(hours=end_hour, minutes=end_minute),
                state=state,
                app_id=(
                    "com.healthmes.private-editor"
                    if state is ActivityState.ACTIVE
                    else None
                ),
                category=(
                    "productivity"
                    if state is ActivityState.ACTIVE
                    else None
                ),
                launches=launches,
            )
        )

    ingest_activity_batch(
        session,
        ActivityBatchIn(
            source_provider="decision-e2e-desktop",
            source_device="decision-e2e-mac",
            platform=ActivityPlatform.MACOS,
            capability=ActivityCapability.DETAILED,
            timezone="UTC",
            collected_at=NOW,
            records=records,
        ),
        now=NOW,
        update_permission_status=True,
    )


def _seed_nutrition(session: Session, settings: Settings) -> None:
    consumed_at = DAY_START + timedelta(hours=2)
    interaction_id = uuid.uuid4()
    outcome_id = uuid.uuid4()
    item = NormalizedIntakeItem(
        name="morning coffee",
        intake_type="beverage",
        serving=Estimate(
            kind=EstimateKind.EXACT,
            unit="cup",
            exact=1,
            estimation_basis="owner_statement",
        ),
        nutrients=(
            NutrientFact(
                nutrient="caffeine",
                amount=Estimate(
                    kind=EstimateKind.EXACT,
                    unit="mg",
                    exact=80,
                    estimation_basis="owner_statement",
                ),
                confidence=Confidence.HIGH,
                origin=EvidenceOrigin.USER,
            ),
        ),
        confidence=Confidence.HIGH,
    )
    create_interaction(
        session,
        settings,
        IntakeInteraction(
            interaction_id=interaction_id,
            operation_fingerprint="a" * 64,
            intent=IntakeIntent.LOG_CONSUMED,
            modality=CaptureModality.TEXT,
            observed_at=consumed_at,
            recorded_at=consumed_at,
            timezone="UTC",
            source="decision-e2e",
            source_text="I drank a coffee with 80 mg caffeine.",
            media_path=None,
            nutrition_observation_id=None,
            items=(item,),
        ),
    )
    persist_outcome(
        session,
        IntakeOutcome(
            outcome_id=outcome_id,
            operation_fingerprint="b" * 64,
            interaction_id=interaction_id,
            status=IntakeOutcomeStatus.CONSUMED,
            confirmed_at=consumed_at + timedelta(minutes=1),
            source="decision-e2e",
            consumed_at=consumed_at,
        ),
    )
    persist_daily_confirmation(
        session,
        DailyIntakeConfirmation(
            confirmation_id=uuid.uuid4(),
            local_date=DAY,
            timezone="UTC",
            observation_ids=(),
            outcome_ids=(outcome_id,),
            total_intake_complete=True,
            confirmed_at=consumed_at + timedelta(minutes=2),
            source="decision-e2e",
        ),
    )


def _seed_wearable(
    session: Session,
    *,
    local_day: date,
) -> None:
    persist_open_wearables_observation(
        session,
        normalized_context={
            "status": "ok",
            "date": local_day.isoformat(),
            "actual_sleep": {
                "status": "ok",
                "local_date": local_day.isoformat(),
                "start": (DAY_START - timedelta(hours=5)).isoformat(),
                "wake_time": DAY_START.isoformat(),
                "duration_minutes": 300,
                "recorded_at": (
                    DAY_START + timedelta(minutes=5)
                ).isoformat(),
            },
            "sleep_debt": {
                "status": "ok",
                "index": 40,
                "last_night": {
                    "duration_minutes": 300,
                    "recorded_at": (
                        DAY_START + timedelta(minutes=5)
                    ).isoformat(),
                },
            },
            "freshness": {
                "recorded_at": (
                    DAY_START + timedelta(minutes=5)
                ).isoformat(),
                "status": "current",
            },
            "coverage": {"ratio": 1.0},
            "limitations": [],
        },
        local_day=local_day,
        timezone="UTC",
        collected_at=NOW - timedelta(minutes=5),
        now=NOW,
    )


def _seed_calendar(
    session: Session,
    sync_health_store: FileSyncHealthStore,
    account_generation: str = CALENDAR_ACCOUNT_GENERATION,
) -> None:
    session.add(
        CalendarEventMirror(
            external_id="decision-e2e-meeting",
            calendar_source=CalendarSource.GOOGLE,
            connection_generation=account_generation,
            summary="Private strategy meeting",
            start_at=NOW + timedelta(hours=1),
            end_at=NOW + timedelta(hours=3),
            is_all_day=False,
            created_at=NOW - timedelta(minutes=2),
            updated_at=NOW - timedelta(minutes=1),
        )
    )
    sync_health_store.record_success(
        CalendarSource.GOOGLE,
        NOW - timedelta(minutes=1),
        event_count=1,
        coverage_kind=SyncCoverageKind.FULL_COLLECTION,
        account_generation=account_generation,
    )


class AdaptiveCrossDomainRuntime:
    metadata = RuntimeMetadata(
        runtime="scripted",
        model="cross-domain-e2e-v1",
    )

    def __init__(self) -> None:
        self.capabilities: list[str] = []
        self.activity_result_had_sources = False

    async def next_step(self, turn):
        if not turn.history:
            assert turn.request.question == QUESTION
            self.capabilities.append("activity.focus")
            return RuntimeStepOutput(
                tool_calls=(
                    {
                        "capability": "activity.focus",
                        "start": DAY_START,
                        "end": NOW,
                        "granularity": "window",
                        "purpose": "Inspect current focus fragmentation.",
                    },
                ),
                metadata=self.metadata,
            )

        if len(turn.history) == 1:
            activity = turn.history[0].results[0]
            self.activity_result_had_sources = bool(
                activity.source_ref_ids
            )
            if (
                activity.status not in {
                    ContextStatus.OK,
                    ContextStatus.PARTIAL,
                }
                or not activity.source_ref_ids
            ):
                return RuntimeStepOutput(
                    draft=DecisionDraft(
                        status=DecisionStatus.NEEDS_CLARIFICATION,
                        clarification_question=(
                            "활동 기록이 없어요. 오늘 얼마나 오래 "
                            "일했는지 알려줄래?"
                        ),
                        uncertainty="활동 기록을 0으로 간주하지 않았습니다.",
                    ),
                    metadata=self.metadata,
                )
            self.capabilities.append("wearable.sleep")
            return RuntimeStepOutput(
                tool_calls=(
                    {
                        "capability": "wearable.sleep",
                        "parameters": {"date": DAY.isoformat()},
                        "purpose": "Check whether fatigue follows short sleep.",
                    },
                ),
                metadata=self.metadata,
            )

        if len(turn.history) == 2:
            sleep = turn.history[1].results[0]
            assert sleep.payload["actual_sleep"]["duration_minutes"] == 300
            self.capabilities.append("calendar.day-summary")
            return RuntimeStepOutput(
                tool_calls=(
                    {
                        "capability": "calendar.day-summary",
                        "parameters": {"date": DAY.isoformat()},
                        "purpose": "Check remaining workload today.",
                    },
                ),
                metadata=self.metadata,
            )

        if len(turn.history) == 3:
            calendar = turn.history[2].results[0]
            assert calendar.payload["event_count"] == 1
            self.capabilities.append("nutrition.caffeine-ledger")
            return RuntimeStepOutput(
                tool_calls=(
                    {
                        "capability": "nutrition.caffeine-ledger",
                        "parameters": {"date": DAY.isoformat()},
                        "purpose": "Check confirmed caffeine already consumed.",
                    },
                ),
                metadata=self.metadata,
            )

        assert len(turn.history) == 4
        nutrition = turn.history[3].results[0]
        assert nutrition.payload["confirmed_caffeine_mg"] == 80
        used_source_ref_ids = [
            source_ref_id
            for exchange in turn.history
            for result in exchange.results
            for source_ref_id in result.source_ref_ids
        ]
        return RuntimeStepOutput(
            draft=DecisionDraft(
                status=DecisionStatus.COMPLETED,
                answer=(
                    "오늘 확정 섭취량 80mg에 후보 100mg을 더하면 "
                    "180mg입니다. 짧은 수면, 집중 분절, 남은 회의를 "
                    "함께 보면 지금은 추가 카페인보다 먼저 쉬는 편이 "
                    "낫습니다."
                ),
                proposed_action=True,
                persistence_intent=DecisionPersistenceIntent.ACTION,
                used_source_ref_ids=used_source_ref_ids,
                confidence=0.8,
                uncertainty=(
                    "개인 카페인 민감도와 정확한 제품 함량은 "
                    "추가로 확인해야 합니다."
                ),
            ),
            metadata=self.metadata,
        )


class CalendarRevocationRuntime:
    metadata = RuntimeMetadata(
        runtime="scripted",
        model="calendar-revocation-e2e-v1",
    )

    def __init__(self, connected: set[CalendarSource]) -> None:
        self._connected = connected

    async def next_step(self, turn):
        if not turn.history:
            return RuntimeStepOutput(
                tool_calls=(
                    {
                        "capability": "calendar.day-summary",
                        "parameters": {"date": DAY.isoformat()},
                        "purpose": "Check today's retained calendar load.",
                    },
                ),
                metadata=self.metadata,
            )

        result = turn.history[0].results[0]
        assert result.status in {
            ContextStatus.OK,
            ContextStatus.PARTIAL,
        }
        assert result.source_ref_ids
        self._connected.clear()
        return RuntimeStepOutput(
            draft=DecisionDraft(
                status=DecisionStatus.COMPLETED,
                answer="The calendar looked busy before access was revoked.",
                proposed_action=True,
                persistence_intent=DecisionPersistenceIntent.ACTION,
                used_source_ref_ids=list(result.source_ref_ids),
                confidence=0.7,
                uncertainty="Calendar access changed during the decision.",
            ),
            metadata=self.metadata,
        )


class ActivityConsentRevocationRuntime:
    metadata = RuntimeMetadata(
        runtime="scripted",
        model="activity-consent-revocation-e2e-v1",
    )

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    async def next_step(self, turn):
        if not turn.history:
            return RuntimeStepOutput(
                tool_calls=(
                    {
                        "capability": "activity.focus",
                        "start": DAY_START,
                        "end": NOW,
                        "granularity": "window",
                        "purpose": "Inspect focus before owner consent changes.",
                    },
                ),
                metadata=self.metadata,
            )

        result = turn.history[0].results[0]
        assert result.source_ref_ids
        with self._factory() as session:
            update_decision_domain_policy(
                session,
                "owner",
                "activity",
                enabled=False,
            )
            session.commit()
        return RuntimeStepOutput(
            draft=DecisionDraft(
                status=DecisionStatus.COMPLETED,
                answer="Activity consent changed during this decision.",
                proposed_action=True,
                persistence_intent=DecisionPersistenceIntent.ACTION,
                used_source_ref_ids=list(result.source_ref_ids),
                confidence=0.5,
                uncertainty="The owner revoked activity access.",
            ),
            metadata=self.metadata,
        )


async def test_natural_language_decision_uses_four_real_domains_and_persists(
    tmp_path,
    settings: Settings,
) -> None:
    database_url, db_engine, factory = _persistence(
        tmp_path,
        "decision-e2e.db",
    )
    calendar_health = FileSyncHealthStore.for_data_dir(
        tmp_path / "calendar-state"
    )
    runtime = AdaptiveCrossDomainRuntime()
    request = _request()
    assert request.compatibility_preset is None

    try:
        with freeze_time(NOW, real_asyncio=True):
            with factory() as session:
                _seed_activity(session)
                _seed_nutrition(session, settings)
                _seed_wearable(
                    session,
                    local_day=datetime.now(UTC).date(),
                )
                _seed_calendar(session, calendar_health)
                session.commit()

            decision_engine = _build_stepwise_test_engine(
                runtime=runtime,
                session_factory=factory,
                policy_resolver=lambda _request: _policy(),
                calendar_sync_health_store=calendar_health,
                calendar_sources=(CalendarSource.GOOGLE,),
                calendar_account_generation_resolver=(
                    lambda _source: CALENDAR_ACCOUNT_GENERATION
                ),
                timeout_seconds=5,
                clock=lambda: NOW,
            )
            async with decision_engine:
                result = await decision_engine.ask_wellness(request)

        assert runtime.activity_result_had_sources is True
        assert runtime.capabilities == [
            "activity.focus",
            "wearable.sleep",
            "calendar.day-summary",
            "nutrition.caffeine-ledger",
        ]
        assert result.status is DecisionStatus.COMPLETED
        assert result.persistence_status is PersistenceStatus.PERSISTED
        assert result.decision_record_id is not None
        assert {record.query.provider_id for record in result.tool_trace} == {
            "activity",
            "wearable",
            "calendar",
            "nutrition",
        }
        assert {source_ref.domain for source_ref in result.source_refs} == {
            "activity",
            "wearable",
            "calendar",
            "nutrition",
        }
        assert all(
            source_ref.content_digest is not None
            for source_ref in result.source_refs
        )
        tool_source_ref_ids = {
            source_ref.reference_id
            for record in result.tool_trace
            if record.result is not None
            for source_ref in record.result.source_refs
        }
        assert {
            source_ref.reference_id
            for source_ref in result.source_refs
        }.issubset(tool_source_ref_ids)

        db_engine.dispose()
        reopened = create_db_engine(database_url)
        reopened_factory = sessionmaker(
            bind=reopened,
            expire_on_commit=False,
        )
        try:
            with reopened_factory() as session:
                row = session.get(
                    DecisionRecord,
                    result.decision_record_id,
                )
                assert row is not None
                assert row.decision_request_id == request.request_id
                assert "compatibility_preset" not in (
                    row.decision_payload["request"]
                )
        finally:
            reopened.dispose()
    finally:
        db_engine.dispose()

async def test_calendar_revocation_after_query_prevents_decision_persistence(
    tmp_path,
) -> None:
    _database_url, db_engine, factory = _persistence(
        tmp_path,
        "decision-calendar-revocation-e2e.db",
    )
    calendar_health = FileSyncHealthStore.for_data_dir(
        tmp_path / "calendar-revocation-state"
    )
    connected = {CalendarSource.GOOGLE}
    runtime = CalendarRevocationRuntime(connected)

    try:
        with factory() as session:
            _seed_calendar(session, calendar_health)
            session.commit()

        decision_engine = _build_stepwise_test_engine(
            runtime=runtime,
            session_factory=factory,
            policy_resolver=lambda _request: _policy(),
            calendar_sync_health_store=calendar_health,
            calendar_source_resolver=lambda: tuple(connected),
            calendar_account_generation_resolver=(
                lambda _source: CALENDAR_ACCOUNT_GENERATION
            ),
            timeout_seconds=5,
            clock=lambda: NOW,
        )
        async with decision_engine:
            result = await decision_engine.ask_wellness(_request())

        assert result.status is DecisionStatus.FAILED
        assert result.persistence_status is PersistenceStatus.FAILED
        assert "decision_source_ref_revalidation_failed" in (
            result.limitations
        )
        assert "calendar_source_disconnected" in result.limitations
        with factory() as session:
            assert (
                session.scalar(
                    select(func.count()).select_from(DecisionRecord)
                )
                == 0
            )
    finally:
        db_engine.dispose()


async def test_domain_consent_revocation_after_query_blocks_persistence(
    tmp_path,
) -> None:
    _database_url, db_engine, factory = _persistence(
        tmp_path,
        "decision-domain-consent-revocation-e2e.db",
    )
    with factory() as session:
        ensure_decision_domain_policies(session, "owner")
        _seed_activity(session)
        session.commit()
    resolver = DatabaseDecisionPolicyResolver(
        session_factory=factory,
        owner_principal_id="owner",
        execution_scope=ExecutionScope.LOCAL,
    )
    decision_engine = _build_stepwise_test_engine(
        runtime=ActivityConsentRevocationRuntime(factory),
        session_factory=factory,
        policy_resolver=resolver,
        timeout_seconds=5,
        clock=lambda: NOW,
    )

    try:
        async with decision_engine:
            result = await decision_engine.ask_wellness(_request())

        assert result.status is DecisionStatus.FAILED
        assert result.persistence_status is PersistenceStatus.FAILED
        assert "domain_consent_denied" in result.limitations
        with factory() as session:
            assert (
                session.scalar(
                    select(func.count()).select_from(DecisionRecord)
                )
                == 0
            )
    finally:
        db_engine.dispose()


async def test_missing_activity_stops_before_unrelated_domains(
    tmp_path,
) -> None:
    _database_url, db_engine, factory = _persistence(
        tmp_path,
        "decision-empty-e2e.db",
    )
    runtime = AdaptiveCrossDomainRuntime()
    decision_engine = _build_stepwise_test_engine(
        runtime=runtime,
        session_factory=factory,
        policy_resolver=lambda _request: _policy(),
        timeout_seconds=5,
        clock=lambda: NOW,
    )

    try:
        async with decision_engine:
            result = await decision_engine.ask_wellness(_request())

        assert runtime.activity_result_had_sources is False
        assert runtime.capabilities == ["activity.focus"]
        assert result.status is DecisionStatus.NEEDS_CLARIFICATION
        assert result.persistence_status is PersistenceStatus.NOT_REQUIRED
        assert result.source_refs == []
        with factory() as session:
            assert (
                session.scalar(
                    select(func.count()).select_from(DecisionRecord)
                )
                == 0
            )
    finally:
        db_engine.dispose()
