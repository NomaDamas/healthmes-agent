from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

import healthmes.nutrition.intake_service as intake_service_module
from healthmes.config import Settings
from healthmes.mcp_server import server as server_module
from healthmes.nutrition.intake_contracts import (
    CaptureModality,
    DecisionScope,
    IntakeDecisionRequest,
    IntakeIntent,
    IntakeInteraction,
    IntakeInteractionReview,
    IntakeReviewStatus,
)
from healthmes.nutrition.intake_service import (
    create_interaction,
    operation_fingerprint,
    persist_decision_request,
    persist_interaction_review,
)
from healthmes.nutrition.ledger_lock import lock_nutrition_ledger
from healthmes.nutrition.query import known_caffeine_for_day
from healthmes.storage import (
    register_storage_object,
    update_retention_policy,
)
from healthmes.store import (
    Base,
    StorageObject,
    WellnessEvent,
    create_db_engine,
)


@pytest.fixture
def postgres_nutrition_store(
    tmp_path,
    settings: Settings,
) -> Iterator[tuple[sessionmaker[Session], Settings]]:
    database_url = os.environ.get("HEALTHMES_TEST_POSTGRES_URL")
    if database_url is None:
        pytest.skip("requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL")
    admin_engine = create_db_engine(database_url)
    schema = f"hm_test_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    postgres_settings = settings.model_copy(
        update={
            "database_url": database_url,
            "data_dir": tmp_path / "data",
        }
    )
    try:
        yield factory, postgres_settings
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


def _interaction(
    *,
    interaction_id: uuid.UUID,
    intent: IntakeIntent,
    recorded_at: datetime,
) -> IntakeInteraction:
    return IntakeInteraction(
        interaction_id=interaction_id,
        operation_fingerprint=operation_fingerprint({"interaction_id": str(interaction_id)}),
        intent=intent,
        modality=CaptureModality.TEXT,
        observed_at=recorded_at - timedelta(minutes=1),
        recorded_at=recorded_at,
        timezone="UTC",
        source="postgres-test",
        source_text="candidate beverage",
        media_path=None,
        nutrition_observation_id=None,
        items=(),
    )


def test_postgres_nutrition_ledger_lock_serializes_transactions(
    postgres_nutrition_store,
) -> None:
    factory, _settings = postgres_nutrition_store
    attempted = threading.Event()
    acquired = threading.Event()

    def wait_for_lock() -> None:
        with factory() as session:
            attempted.set()
            lock_nutrition_ledger(session)
            acquired.set()
            session.commit()

    with factory() as holder:
        lock_nutrition_ledger(holder)
        worker = threading.Thread(target=wait_for_lock)
        worker.start()
        assert attempted.wait(timeout=5)
        assert not acquired.wait(timeout=0.25)
        holder.commit()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert acquired.is_set()


def test_postgres_known_caffeine_locks_ledger_before_first_select(
    postgres_nutrition_store,
) -> None:
    factory, _settings = postgres_nutrition_store
    engine = factory.kw["bind"]
    statements: list[str] = []

    def capture_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(" ".join(statement.lower().split()))

    sa.event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        with factory() as session:
            known_caffeine_for_day(
                session,
                local_date=datetime.now(UTC).date(),
                timezone="UTC",
            )
    finally:
        sa.event.remove(
            engine,
            "before_cursor_execute",
            capture_statement,
        )

    assert statements
    assert statements[0].startswith("select pg_advisory_xact_lock")


def test_postgres_production_caffeine_lock_order_is_canonical(
    postgres_nutrition_store,
    monkeypatch,
) -> None:
    factory, settings = postgres_nutrition_store
    now = datetime.now(UTC).replace(microsecond=0)
    primary_id = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    comparison_low = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    comparison_high = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    request_id = uuid.uuid4()
    with factory() as session:
        for interaction_id, intent in (
            (primary_id, IntakeIntent.ASK_BEFORE_INTAKE),
            (comparison_low, IntakeIntent.COMPARE_OPTION),
            (comparison_high, IntakeIntent.COMPARE_OPTION),
        ):
            create_interaction(
                session,
                settings,
                _interaction(
                    interaction_id=interaction_id,
                    intent=intent,
                    recorded_at=now,
                ),
            )
        session.commit()
        persist_decision_request(
            session,
            IntakeDecisionRequest(
                request_id=request_id,
                operation_fingerprint=operation_fingerprint({"request_id": str(request_id)}),
                interaction_id=primary_id,
                scope=DecisionScope.CAFFEINE_SLEEP,
                requested_at=now,
                source="postgres-test",
                intended_consumption_at=now + timedelta(hours=1),
                compare_interaction_ids=(
                    comparison_high,
                    comparison_low,
                ),
            ),
        )
        session.commit()

    calls: list[tuple[str, uuid.UUID | None]] = []
    runtime_ledger_lock = server_module.lock_nutrition_ledger
    runtime_candidate_lock = intake_service_module.lock_interaction_transition_state

    def observe_ledger_lock(session: Session) -> None:
        calls.append(("ledger", None))
        runtime_ledger_lock(session)

    def observe_candidate_lock(
        session: Session,
        interaction_id: uuid.UUID,
        *,
        allow_legacy_without_marker: bool = False,
    ) -> None:
        calls.append(("candidate", interaction_id))
        runtime_candidate_lock(
            session,
            interaction_id,
            allow_legacy_without_marker=allow_legacy_without_marker,
        )

    monkeypatch.setattr(
        server_module,
        "lock_nutrition_ledger",
        observe_ledger_lock,
    )
    monkeypatch.setattr(
        intake_service_module,
        "lock_interaction_transition_state",
        observe_candidate_lock,
    )

    with factory() as session:
        server_module._lock_live_caffeine_decision_request(
            session,
            request_id,
        )
        session.commit()

    assert calls[0] == ("ledger", None)
    assert [interaction_id for kind, interaction_id in calls if kind == "candidate"] == sorted(
        {primary_id, comparison_low, comparison_high},
        key=lambda value: value.bytes,
    )


def test_postgres_retention_update_serializes_with_object_writer(
    postgres_nutrition_store,
) -> None:
    factory, settings = postgres_nutrition_store
    with factory() as session:
        update_retention_policy(session, "media", "7d")
        session.commit()

    observed_at = datetime.now(UTC).replace(microsecond=0)
    writer_ready = threading.Event()
    release_writer = threading.Event()
    updater_started = threading.Event()
    updater_finished = threading.Event()
    writer_errors: list[BaseException] = []
    updater_errors: list[BaseException] = []
    object_ids: list[uuid.UUID] = []

    def write_object() -> None:
        try:
            with factory() as session:
                obj = register_storage_object(
                    session,
                    settings,
                    relative_path="media/postgres-retention-race.jpg",
                    data_class="media",
                    content_type="image/jpeg",
                    size_bytes=10,
                    observed_at=observed_at,
                )
                object_ids.append(obj.id)
                writer_ready.set()
                assert release_writer.wait(timeout=5)
                session.commit()
        except BaseException as exc:
            writer_errors.append(exc)

    def update_policy() -> None:
        assert writer_ready.wait(timeout=5)
        updater_started.set()
        try:
            with factory() as session:
                update_retention_policy(session, "media", "1d")
                session.commit()
        except BaseException as exc:
            updater_errors.append(exc)
        finally:
            updater_finished.set()

    writer = threading.Thread(target=write_object)
    updater = threading.Thread(target=update_policy)
    writer.start()
    updater.start()
    assert writer_ready.wait(timeout=5)
    assert updater_started.wait(timeout=5)
    assert not updater_finished.wait(timeout=0.25)
    release_writer.set()
    writer.join(timeout=5)
    updater.join(timeout=5)

    assert not writer.is_alive()
    assert not updater.is_alive()
    assert writer_errors == []
    assert updater_errors == []
    assert len(object_ids) == 1
    with factory() as session:
        obj = session.get(StorageObject, object_ids[0])
        assert obj is not None
        assert obj.expires_at.astimezone(UTC) == (observed_at + timedelta(days=1))


def test_postgres_candidate_locks_cover_comparison_and_review_uses_ledger_lock(
    postgres_nutrition_store,
) -> None:
    factory, settings = postgres_nutrition_store
    now = datetime.now(UTC).replace(microsecond=0)
    primary_id = uuid.uuid4()
    comparison_id = uuid.uuid4()
    unrelated_id = uuid.uuid4()
    request_id = uuid.uuid4()
    with factory() as session:
        for interaction_id, intent in (
            (primary_id, IntakeIntent.ASK_BEFORE_INTAKE),
            (comparison_id, IntakeIntent.COMPARE_OPTION),
            (unrelated_id, IntakeIntent.ASK_BEFORE_INTAKE),
        ):
            create_interaction(
                session,
                settings,
                _interaction(
                    interaction_id=interaction_id,
                    intent=intent,
                    recorded_at=now,
                ),
            )
            session.commit()
        persist_decision_request(
            session,
            IntakeDecisionRequest(
                request_id=request_id,
                operation_fingerprint=operation_fingerprint({"request_id": str(request_id)}),
                interaction_id=primary_id,
                scope=DecisionScope.CAFFEINE_SLEEP,
                requested_at=now,
                source="postgres-test",
                intended_consumption_at=now + timedelta(hours=1),
                compare_interaction_ids=(comparison_id,),
            ),
        )
        session.commit()

    row_attempted = threading.Event()
    row_acquired = threading.Event()
    review_attempted = threading.Event()
    review_finished = threading.Event()
    review_id = uuid.uuid4()

    def lock_comparison_marker_directly() -> None:
        with factory() as session:
            row_attempted.set()
            marker = session.scalar(
                sa.select(WellnessEvent)
                .where(
                    WellnessEvent.source_provider == "nutrition-operation",
                    WellnessEvent.source_record_id == f"interaction:{comparison_id}",
                )
                .with_for_update()
            )
            assert marker is not None
            row_acquired.set()
            session.rollback()

    def write_unrelated_review() -> None:
        with factory() as session:
            review_attempted.set()
            persist_interaction_review(
                session,
                IntakeInteractionReview(
                    review_id=review_id,
                    operation_fingerprint=operation_fingerprint({"review_id": str(review_id)}),
                    interaction_id=unrelated_id,
                    status=IntakeReviewStatus.CONFIRMED,
                    reviewed_at=now,
                    source="postgres-test",
                ),
            )
            session.commit()
            review_finished.set()

    with factory() as holder:
        server_module._lock_live_caffeine_decision_request(
            holder,
            request_id,
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            row_future = pool.submit(lock_comparison_marker_directly)
            review_future = pool.submit(write_unrelated_review)
            assert row_attempted.wait(timeout=5)
            assert review_attempted.wait(timeout=5)
            assert not row_acquired.wait(timeout=0.25)
            assert not review_finished.wait(timeout=0.25)
            holder.commit()
            row_future.result(timeout=5)
            review_future.result(timeout=5)

    assert row_acquired.is_set()
    assert review_finished.is_set()
    with factory() as session:
        stored_review = session.scalar(
            sa.select(WellnessEvent).where(
                WellnessEvent.event_type == "nutrition.interaction-review.v1",
                WellnessEvent.source_record_id == str(review_id),
            )
        )
    assert stored_review is not None
