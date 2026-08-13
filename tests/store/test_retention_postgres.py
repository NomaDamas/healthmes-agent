from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from healthmes.activity.locking import lock_activity_write_plane
from healthmes.activity.repository import ensure_activity_policies
from healthmes.app import _initialize_activity_storage
from healthmes.storage import (
    ensure_default_policies,
    run_storage_maintenance,
)
from healthmes.storage.service import DEFAULT_RETENTION
from healthmes.store import Base, RetentionPolicy, create_db_engine


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_concurrent_first_startup_initializes_default_policies_once() -> None:
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
    start = threading.Barrier(2)
    observed_counts: list[int] = []
    failures: list[BaseException] = []

    def initialize() -> None:
        with factory() as session:
            try:
                start.wait(timeout=5)
                policies = ensure_default_policies(session)
                session.commit()
            except BaseException as exc:
                session.rollback()
                failures.append(exc)
            else:
                observed_counts.append(len(policies))

    workers = [threading.Thread(target=initialize) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        assert all(not worker.is_alive() for worker in workers)
        assert failures == []
        assert observed_counts == [
            len(DEFAULT_RETENTION),
            len(DEFAULT_RETENTION),
        ]
        with factory() as session:
            rows = list(
                session.scalars(
                    sa.select(RetentionPolicy).order_by(
                        RetentionPolicy.data_class
                    )
                )
            )
            assert [row.data_class for row in rows] == sorted(
                DEFAULT_RETENTION
            )
    finally:
        for worker in workers:
            worker.join(timeout=5)
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.mark.parametrize("bootstrap_path", ("startup", "maintenance"))
@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_activity_bootstrap_takes_write_plane_before_policy_inserts(
    settings,
    bootstrap_path,
) -> None:
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
    worker_started = threading.Event()
    worker_pid: list[int] = []
    failures: list[BaseException] = []

    def bootstrap() -> None:
        with factory() as session:
            worker_pid.append(
                int(session.scalar(sa.text("SELECT pg_backend_pid()")))
            )
            worker_started.set()
            try:
                if bootstrap_path == "startup":
                    _initialize_activity_storage(session, timezone="UTC")
                else:
                    run_storage_maintenance(
                        session,
                        settings,
                        now=datetime(2026, 8, 10, 12, tzinfo=UTC),
                    )
                session.commit()
            except BaseException as exc:
                session.rollback()
                failures.append(exc)

    worker: threading.Thread | None = None
    try:
        with factory() as first:
            lock_activity_write_plane(first)
            worker = threading.Thread(target=bootstrap)
            worker.start()
            assert worker_started.wait(timeout=5)

            deadline = time.monotonic() + 5
            waiting_for_advisory_lock = False
            while time.monotonic() < deadline and worker.is_alive():
                wait_event = first.execute(
                    sa.text(
                        "SELECT wait_event_type, wait_event "
                        "FROM pg_stat_activity WHERE pid = :pid"
                    ),
                    {"pid": worker_pid[0]},
                ).one_or_none()
                if wait_event is not None and tuple(wait_event) == (
                    "Lock",
                    "advisory",
                ):
                    waiting_for_advisory_lock = True
                    break
                time.sleep(0.05)

            assert waiting_for_advisory_lock
            policies = ensure_activity_policies(first)
            assert set(policies) == {
                "activity_raw",
                "activity_hourly",
                "activity_daily",
            }
            first.commit()
            worker.join(timeout=10)

        assert worker is not None
        assert not worker.is_alive()
        assert failures == []
        with factory() as session:
            assert session.scalar(
                sa.select(sa.func.count()).select_from(RetentionPolicy)
            ) == len(DEFAULT_RETENTION)
    finally:
        if worker is not None:
            worker.join(timeout=5)
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()
