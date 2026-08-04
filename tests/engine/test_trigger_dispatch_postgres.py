from __future__ import annotations

import os
import threading
import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from healthmes.config import Settings
from healthmes.engine.triggers import HealthSignals, TriggerEvaluator, TriggerFire
from healthmes.engine.webhook import WebhookResult
from healthmes.store import Base, create_db_engine
from healthmes.store.models import TriggerEvent


class EmptyHealthReader:
    def read(self, now: datetime) -> HealthSignals:
        return HealthSignals()


class CountingSender:
    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def send(self, fire, *, fired_at, trigger_event_id) -> WebhookResult:
        with self._lock:
            self.calls += 1
        return WebhookResult(ok=True, status_code=202)


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
                    "push": {"state": "dispatching", "channel": "webhook"},
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
