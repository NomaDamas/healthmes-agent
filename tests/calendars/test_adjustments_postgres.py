from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from healthmes.calendars.adjustments import (
    SqlAlchemyAdjustmentRepository,
    morning_dedup_key,
)
from healthmes.store import Base, create_db_engine
from healthmes.store.models import TriggerEvent

NOW = datetime(2026, 7, 21, 22, 0, tzinfo=UTC)
LOCAL_DAY = date(2026, 7, 22)


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_postgres_daily_dedup_claim_allows_only_one_concurrent_claim() -> None:
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
    select_gate = threading.Barrier(2, timeout=5)
    gate_enabled = threading.Event()
    dedup_key = morning_dedup_key(LOCAL_DAY)

    @event.listens_for(engine, "after_cursor_execute")
    def wait_after_both_claim_selects(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        if "pg_advisory_xact_lock" in statement:
            gate_enabled.clear()
            return
        if (
            gate_enabled.is_set()
            and statement.lstrip().upper().startswith("SELECT")
            and "trigger_event.id" in statement
            and "trigger_event.dedup_key" in statement
        ):
            select_gate.wait()

    def claim_once() -> bool:
        with factory() as session:
            claimed = SqlAlchemyAdjustmentRepository(session).claim_daily_evaluation(
                dedup_key,
                {"outcome": "evaluating"},
                NOW,
            )
            session.commit()
            return claimed

    try:
        gate_enabled.set()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim_once), pool.submit(claim_once)]
            claims = [future.result(timeout=10) for future in futures]
        gate_enabled.clear()

        with factory() as session:
            row_count = session.scalar(
                sa.select(sa.func.count())
                .select_from(TriggerEvent)
                .where(TriggerEvent.dedup_key == dedup_key)
            )

        assert sorted(claims) == [False, True]
        assert row_count == 1
    finally:
        event.remove(engine, "after_cursor_execute", wait_after_both_claim_selects)
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()
