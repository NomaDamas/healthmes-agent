from __future__ import annotations

import os
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

import healthmes.engine.triggers as trigger_module
from healthmes.config import Settings
from healthmes.engine.decision_dispatch import DecisionDispatchResult
from healthmes.engine.triggers import HealthSignals, TriggerEvaluator, TriggerFire
from healthmes.storage import update_retention_policy
from healthmes.store import Base, create_db_engine
from healthmes.store.models import TriggerEvent


class EmptyHealthReader:
    def read(self, now: datetime) -> HealthSignals:
        return HealthSignals()


class CountingSender:
    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def send(
        self,
        fire,
        *,
        fired_at,
        trigger_event_id,
    ) -> DecisionDispatchResult:
        with self._lock:
            self.calls += 1
        return DecisionDispatchResult(
            ok=True,
            status_code=202,
            channel="test",
        )


class AppAvailableSender:
    requires_reasoning = True

    def send(
        self,
        fire,
        *,
        fired_at,
        trigger_event_id,
    ) -> DecisionDispatchResult:
        del fire, fired_at, trigger_event_id
        return DecisionDispatchResult(
            ok=False,
            status_code=204,
            ready_for_native=True,
            channel="app_poll",
            message="Generated answer awaiting app display.",
        )


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_postgres_fresh_fire_deduplicates_concurrent_evaluators() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_test_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    sender = CountingSender()
    now = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
    ready = threading.Barrier(2)

    def concurrent_rule(_context) -> TriggerFire:
        ready.wait(timeout=5)
        return TriggerFire(
            rule_id="concurrent_fresh_fire",
            dedup_key="concurrent_fresh_fire:1",
            summary="observation",
            proposal="proposal",
            evidence={},
        )

    settings = Settings(
        database_url=database_url,
        native_alert_delivery=False,
        scheduler_enabled=False,
        _env_file=None,
    )

    def sweep_once():
        return TriggerEvaluator(
            settings,
            session_factory=factory,
            health_reader=EmptyHealthReader(),
            alert_sender=sender,
            rules=(concurrent_rule,),
            now_provider=lambda: now,
        ).evaluate_once()

    try:
        reports = []
        errors = []

        def run() -> None:
            try:
                reports.append(sweep_once())
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        workers = [
            threading.Thread(target=run, name=f"fresh-fire-{index}")
            for index in range(2)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        assert all(not worker.is_alive() for worker in workers)
        assert not errors
        assert sender.calls == 1
        assert sorted(report.outcomes[0].status for report in reports) == [
            "deduplicated",
            "pushed",
        ]
        with factory() as session:
            events = session.scalars(sa.select(TriggerEvent)).all()
            assert len(events) == 1
            assert events[0].alert_sent is True
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_postgres_pending_dispatch_has_one_concurrent_owner() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_test_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    stale_listed = threading.Event()
    delivery_done = threading.Event()
    sender = CountingSender()
    now = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)

    with factory() as session:
        session.add(
            TriggerEvent(
                fired_at=now,
                rule_id="concurrent_dispatch",
                dedup_key="concurrent_dispatch:1",
                alert_sent=False,
                payload={
                    "summary": "observation",
                    "proposal": "proposal",
                    "evidence": {},
                    "push": {"state": "dispatching", "channel": "delivery"},
                },
            )
        )
        session.commit()

    @event.listens_for(engine, "after_cursor_execute")
    def align_pending_lists(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        normalized = " ".join(statement.lower().split())
        if (
            threading.current_thread().name == "delayed-dispatcher"
            and normalized.startswith("select")
            and "trigger_event.alert_sent is false" in normalized
            and "order by trigger_event.fired_at" in normalized
            and "for update" not in normalized
        ):
            stale_listed.set()
            assert delivery_done.wait(timeout=5)

    settings = Settings(
        database_url=database_url,
        native_alert_delivery=False,
        scheduler_enabled=False,
        _env_file=None,
    )

    def sweep_once():
        return TriggerEvaluator(
            settings,
            session_factory=factory,
            health_reader=EmptyHealthReader(),
            alert_sender=sender,
            rules=(),
            now_provider=lambda: now,
        ).evaluate_once()

    try:
        delayed_report = []
        delayed_error = []

        def run_delayed() -> None:
            try:
                delayed_report.append(sweep_once())
            except BaseException as exc:  # pragma: no cover - surfaced below
                delayed_error.append(exc)

        delayed = threading.Thread(target=run_delayed, name="delayed-dispatcher")
        delayed.start()
        assert stale_listed.wait(timeout=5)
        immediate_report = sweep_once()
        delivery_done.set()
        delayed.join(timeout=10)
        assert not delayed.is_alive()
        assert not delayed_error

        reports = [immediate_report, *delayed_report]
        assert sender.calls == 1
        assert sum(report.count("pushed") for report in reports) == 1
        with factory() as session:
            [stored] = session.scalars(sa.select(TriggerEvent)).all()
            assert stored.alert_sent is True
    finally:
        event.remove(engine, "after_cursor_execute", align_pending_lists)
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_postgres_retention_shrink_after_final_check_scrubs_answer(
    monkeypatch,
) -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_alert_retention_{uuid.uuid4().hex}"
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
    )
    now = datetime(2026, 8, 17, 9, tzinfo=UTC)
    fired_at = now - timedelta(days=2)
    settings = Settings(
        database_url=database_url,
        native_alert_delivery=True,
        scheduler_enabled=False,
        _env_file=None,
    )
    original_check = trigger_module.alert_retention_is_expired
    post_reasoning_check = threading.Event()
    release_finalization = threading.Event()
    retention_done = threading.Event()
    check_lock = threading.Lock()
    check_count = 0
    errors: list[BaseException] = []

    def block_after_final_check(session, trigger_event, *, now):
        nonlocal check_count
        expired = original_check(
            session,
            trigger_event,
            now=now,
        )
        with check_lock:
            check_count += 1
            should_block = check_count == 2
        if should_block:
            post_reasoning_check.set()
            if not release_finalization.wait(timeout=5):
                raise TimeoutError("test did not release alert finalization")
        return expired

    monkeypatch.setattr(
        trigger_module,
        "alert_retention_is_expired",
        block_after_final_check,
    )
    try:
        with factory() as session:
            update_retention_policy(
                session,
                "alert",
                "7d",
                now=now,
            )
            session.commit()

        evaluator = TriggerEvaluator(
            settings,
            session_factory=factory,
            health_reader=EmptyHealthReader(),
            alert_sender=AppAvailableSender(),
            rules=(),
            now_provider=lambda: now,
        )

        def evaluate() -> None:
            try:
                evaluator.dispatch_fire(
                    TriggerFire(
                        rule_id="retention_race",
                        dedup_key="retention_race:1",
                        summary="A generated answer is ready.",
                        proposal="Surface the answer.",
                        evidence={},
                    ),
                    fired_at=fired_at,
                )
            except BaseException as exc:
                errors.append(exc)

        def shorten_retention() -> None:
            try:
                with factory() as session:
                    update_retention_policy(
                        session,
                        "alert",
                        "1d",
                        now=now,
                    )
                    session.commit()
            except BaseException as exc:
                errors.append(exc)
            finally:
                retention_done.set()

        evaluation_thread = threading.Thread(target=evaluate)
        evaluation_thread.start()
        assert post_reasoning_check.wait(timeout=5)

        retention_thread = threading.Thread(target=shorten_retention)
        retention_thread.start()
        assert not retention_done.wait(timeout=0.1)
        release_finalization.set()
        evaluation_thread.join(timeout=10)
        retention_thread.join(timeout=10)

        assert not evaluation_thread.is_alive()
        assert not retention_thread.is_alive()
        assert errors == []
        with factory() as session:
            [stored] = session.scalars(sa.select(TriggerEvent)).all()
            assert stored.alert_sent is False
            assert "message" not in stored.payload
            assert stored.payload["push"]["state"] == "expired"
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()
