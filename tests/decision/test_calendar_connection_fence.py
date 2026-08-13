from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from healthmes.calendars import creds
from healthmes.calendars.state import (
    InMemorySyncHealthStore,
    SyncCoverageKind,
)
from healthmes.config import Settings
from healthmes.decision import (
    ContextAccessLayer,
    ContextAccessPolicy,
    ContextCoverage,
    ContextFreshness,
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
    PersistenceStatus,
    RuntimeMetadata,
    ToolCallRecord,
    ToolCallStatus,
)
from healthmes.decision.domain_providers import CalendarContextProvider
from healthmes.store import (
    Base,
    CalendarEventMirror,
    CalendarSource,
    DecisionRecord,
    create_db_engine,
)

NOW = datetime(2026, 8, 12, 6, tzinfo=UTC)


def _settings(tmp_path, database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        data_dir=tmp_path / "data",
        timezone="UTC",
        _env_file=None,
    )


def _connect_google(settings: Settings) -> None:
    path = settings.data_dir / "google" / "calendar_token.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "authorized_user",
                "refresh_token": "refresh-token",
                "client_id": "client-id",
                "client_secret": "client-secret",
            }
        ),
        encoding="utf-8",
    )


def _policy() -> ContextAccessPolicy:
    return ContextAccessPolicy(
        owner_principal_id="owner",
        grants=(DomainAccessGrant(domain="calendar"),),
    )


def _request() -> DecisionRequest:
    return DecisionRequest(
        question="Should I take a break before my next meeting?",
        requested_at=NOW,
        timezone="UTC",
        caller=DecisionCaller(
            principal_id="owner",
            authenticated=True,
            execution_scope=ExecutionScope.LOCAL,
        ),
    )


def _decision_fixture(
    tmp_path,
) -> tuple[
    Settings,
    sessionmaker[Session],
    InMemorySyncHealthStore,
    ContextAccessLayer,
    DecisionRequest,
    DecisionAgentRun,
]:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'calendar-fence.db'}"
    settings = _settings(tmp_path, database_url)
    _connect_google(settings)
    account_generation = creds.calendar_account_generation(
        settings,
        CalendarSource.GOOGLE,
    )
    assert account_generation is not None
    sync_health_store = InMemorySyncHealthStore()
    sync_health_store.record_success(
        CalendarSource.GOOGLE,
        NOW - timedelta(minutes=1),
        event_count=1,
        coverage_kind=SyncCoverageKind.FULL_COLLECTION,
        account_generation=account_generation,
    )
    engine = create_db_engine(database_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(
            CalendarEventMirror(
                external_id="calendar-fence-event",
                calendar_source=CalendarSource.GOOGLE,
                connection_generation=account_generation,
                summary="Private meeting title",
                start_at=NOW + timedelta(hours=1),
                end_at=NOW + timedelta(hours=2),
                is_all_day=False,
                created_at=NOW - timedelta(minutes=2),
                updated_at=NOW - timedelta(minutes=1),
            )
        )
        session.commit()

    registry = ContextProviderRegistry(
        (
            CalendarContextProvider(
                settings=settings,
                sync_health_store=sync_health_store,
            ),
        )
    )
    access_layer = ContextAccessLayer(
        registry,
        clock=lambda: NOW,
        calendar_settings=settings,
        calendar_sync_health_store=sync_health_store,
    )
    request = _request()
    query = ContextQuery(
        provider_id="calendar",
        capability="calendar.day-summary",
        parameters={"date": NOW.date().isoformat()},
        timezone="UTC",
    )
    turn = access_layer.start_turn(request, policy=_policy())
    with factory() as session:
        context = asyncio.run(turn.query(session, query))
        session.rollback()
    assert context.status in {ContextStatus.OK, ContextStatus.PARTIAL}
    assert len(context.source_refs) == 1
    assert context.source_refs[0].content_digest is not None
    run = DecisionAgentRun(
        request_id=request.request_id,
        turn_id=request.turn_id,
        draft=DecisionDraft(
            status=DecisionStatus.COMPLETED,
            answer="Take a short break before the next meeting.",
            proposed_action=True,
            used_source_ref_ids=[
                context.source_refs[0].reference_id
            ],
            confidence=0.8,
            uncertainty="Only retained calendar load was considered.",
        ),
        source_refs=tuple(context.source_refs),
        runtime=RuntimeMetadata(
            runtime="scripted",
            model="calendar-fence-test-v1",
        ),
        steps_used=1,
        tool_trace=(
            ToolCallRecord(
                query=query,
                status=ToolCallStatus.COMPLETED,
                started_at=NOW,
                finished_at=NOW,
                result=ContextResult(
                    query_id=query.query_id,
                    provider_id=query.provider_id,
                    capability=query.capability,
                    status=context.status,
                    payload=context.payload,
                    source_refs=context.source_refs,
                    freshness=context.freshness
                    or ContextFreshness(
                        status=FreshnessStatus.CURRENT,
                        as_of=NOW,
                        age_seconds=0,
                    ),
                    coverage=context.coverage
                    or ContextCoverage(
                        status=CoverageStatus.COMPLETE,
                        ratio=1,
                    ),
                    observed_start=context.observed_start,
                    observed_end=context.observed_end,
                    collected_at=context.collected_at,
                    limitations=context.limitations,
                ),
            ),
        ),
        access_trace=turn.trace,
        system_policy_version="calendar-fence-test",
        started_at=NOW,
        finished_at=NOW,
    )
    return (
        settings,
        factory,
        sync_health_store,
        access_layer,
        request,
        run,
    )


def test_finalization_commits_before_racing_calendar_disconnect(
    tmp_path,
) -> None:
    (
        settings,
        factory,
        _sync_health_store,
        access_layer,
        request,
        run,
    ) = _decision_fixture(tmp_path)
    finalizer_inside = threading.Event()
    disconnect_attempted = threading.Event()
    calls = 0

    def resolve(_request: DecisionRequest) -> ContextAccessPolicy:
        nonlocal calls
        calls += 1
        if calls == 2:
            finalizer_inside.set()
            assert disconnect_attempted.wait(timeout=5)
        return _policy()

    finalizer = DecisionFinalizer(
        access_layer=access_layer,
        session_factory=factory,
        policy_resolver=resolve,
        clock=lambda: NOW,
    )
    result_holder = []
    errors = []

    def finalize() -> None:
        try:
            result_holder.append(finalizer.finalize(request, run))
        except BaseException as exc:
            errors.append(exc)

    def disconnect() -> None:
        try:
            assert finalizer_inside.wait(timeout=5)
            disconnect_attempted.set()
            with factory() as session:
                with creds.calendar_connection_write(
                    session,
                    CalendarSource.GOOGLE,
                ):
                    assert creds.delete_google_token(
                        settings.data_dir
                    )
        except BaseException as exc:
            errors.append(exc)

    finalizer_thread = threading.Thread(target=finalize)
    disconnect_thread = threading.Thread(target=disconnect)
    try:
        finalizer_thread.start()
        disconnect_thread.start()
        finalizer_thread.join(timeout=10)
        disconnect_thread.join(timeout=10)

        assert not finalizer_thread.is_alive()
        assert not disconnect_thread.is_alive()
        assert errors == []
        assert len(result_holder) == 1
        result = result_holder[0]
        assert result.status is DecisionStatus.COMPLETED
        assert result.persistence_status is PersistenceStatus.PERSISTED
        assert creds.google_connected(settings.data_dir) is False
        with factory() as session:
            assert session.scalar(
                sa.select(sa.func.count()).select_from(DecisionRecord)
            ) == 1
    finally:
        finalizer_thread.join(timeout=5)
        disconnect_thread.join(timeout=5)
        factory.kw["bind"].dispose()


def test_calendar_disconnect_finishes_before_racing_finalization(
    tmp_path,
) -> None:
    (
        settings,
        factory,
        _sync_health_store,
        access_layer,
        request,
        run,
    ) = _decision_fixture(tmp_path)
    policy_resolved = threading.Event()

    def resolve(_request: DecisionRequest) -> ContextAccessPolicy:
        policy_resolved.set()
        return _policy()

    finalizer = DecisionFinalizer(
        access_layer=access_layer,
        session_factory=factory,
        policy_resolver=resolve,
        clock=lambda: NOW,
    )
    result_holder = []
    errors = []

    def finalize() -> None:
        try:
            result_holder.append(finalizer.finalize(request, run))
        except BaseException as exc:
            errors.append(exc)

    finalizer_thread = threading.Thread(target=finalize)
    try:
        with factory() as session:
            with creds.calendar_connection_write(
                session,
                CalendarSource.GOOGLE,
            ):
                assert creds.delete_google_token(settings.data_dir)
                finalizer_thread.start()
                assert policy_resolved.wait(timeout=5)

        finalizer_thread.join(timeout=10)
        assert not finalizer_thread.is_alive()
        assert errors == []
        assert len(result_holder) == 1
        result = result_holder[0]
        assert result.status is DecisionStatus.FAILED
        assert result.persistence_status is PersistenceStatus.FAILED
        assert "calendar_source_disconnected" in result.limitations
        with factory() as session:
            assert session.scalar(
                sa.select(sa.func.count()).select_from(DecisionRecord)
            ) == 0
    finally:
        finalizer_thread.join(timeout=5)
        factory.kw["bind"].dispose()


def test_calendar_sync_visibility_change_after_flush_rolls_back_decision(
    tmp_path,
) -> None:
    (
        _settings_value,
        factory,
        sync_health_store,
        access_layer,
        request,
        run,
    ) = _decision_fixture(tmp_path)

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
                sync_health_store.record_success(
                    CalendarSource.GOOGLE,
                    NOW,
                    event_count=0,
                    coverage_kind=SyncCoverageKind.FULL_COLLECTION,
                    account_generation="f" * 64,
                )

    changing_factory = sessionmaker(
        bind=factory.kw["bind"],
        class_=SyncChangingSession,
        expire_on_commit=False,
    )
    finalizer = DecisionFinalizer(
        access_layer=access_layer,
        session_factory=changing_factory,
        policy_resolver=lambda _request: _policy(),
        clock=lambda: NOW,
    )

    result = finalizer.finalize(request, run)

    assert result.status is DecisionStatus.FAILED
    assert result.persistence_status is PersistenceStatus.FAILED
    assert "calendar_visibility_changed" in result.limitations
    with factory() as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(DecisionRecord)
        ) == 0
    factory.kw["bind"].dispose()
