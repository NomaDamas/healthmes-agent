from __future__ import annotations

import threading

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

import healthmes.app as app_module
from healthmes.app import _initialize_activity_storage
from healthmes.storage import ensure_default_policies
from healthmes.storage import service as storage_service
from healthmes.storage.service import DEFAULT_RETENTION
from healthmes.store import Base, RetentionPolicy, create_db_engine


def test_concurrent_sqlite_default_policy_bootstrap_is_idempotent(
    tmp_path,
    monkeypatch,
) -> None:
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'retention-bootstrap.db'}"
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
    )
    before_lock = threading.Barrier(2)
    real_lock = storage_service.lock_activity_write_plane
    failures: list[BaseException] = []
    observed: list[tuple[str, ...]] = []

    def synchronized_lock(session) -> None:
        before_lock.wait(timeout=5)
        real_lock(session)

    monkeypatch.setattr(
        storage_service,
        "lock_activity_write_plane",
        synchronized_lock,
    )

    def bootstrap() -> None:
        with factory() as session:
            try:
                policies = ensure_default_policies(session)
                session.commit()
            except BaseException as exc:
                session.rollback()
                failures.append(exc)
            else:
                observed.append(
                    tuple(row.data_class for row in policies)
                )

    workers = [threading.Thread(target=bootstrap) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        assert all(not worker.is_alive() for worker in workers)
        assert failures == []
        assert observed == [
            tuple(sorted(DEFAULT_RETENTION)),
            tuple(sorted(DEFAULT_RETENTION)),
        ]
        with factory() as session:
            rows = tuple(
                session.scalars(
                    sa.select(RetentionPolicy).order_by(
                        RetentionPolicy.data_class
                    )
                )
            )
            assert tuple(row.data_class for row in rows) == tuple(
                sorted(DEFAULT_RETENTION)
            )
    finally:
        for worker in workers:
            worker.join(timeout=5)
        engine.dispose()


def test_activity_startup_explicitly_bootstraps_retention_policies(
    tmp_path,
    monkeypatch,
) -> None:
    engine = create_db_engine(
        f"sqlite+pysqlite:///{tmp_path / 'startup-bootstrap.db'}"
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
    )
    monkeypatch.setattr(
        app_module,
        "backfill_android_canonical_events",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        app_module,
        "migrate_activity_summary_derivations",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        app_module,
        "ensure_decision_domain_policies",
        lambda *_args, **_kwargs: (),
    )

    try:
        with factory() as session:
            _initialize_activity_storage(
                session,
                timezone="UTC",
                decision_owner_principal_id="owner",
            )
            session.commit()
        with factory() as session:
            assert (
                session.scalar(
                    sa.select(sa.func.count()).select_from(
                        RetentionPolicy
                    )
                )
                == len(DEFAULT_RETENTION)
            )
    finally:
        engine.dispose()
