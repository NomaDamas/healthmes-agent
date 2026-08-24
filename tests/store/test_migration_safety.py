"""Bounded PostgreSQL lock contracts for destructive Alembic downgrades."""

from __future__ import annotations

import importlib.util
import os
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic.config import Config

from alembic import command
from healthmes.store import migration_safety as migration_safety_mod
from healthmes.store.migration_safety import (
    acquire_postgres_downgrade_lock,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _config(database_url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


class _OriginalDatabaseError(Exception):
    def __init__(
        self,
        *,
        sqlstate: str | None = None,
        pgcode: str | None = None,
    ) -> None:
        super().__init__("injected database error")
        self.sqlstate = sqlstate
        self.pgcode = pgcode


class _RecordingBind:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(
        self,
        *,
        failure: sa.exc.DBAPIError | None = None,
    ) -> None:
        self.failure = failure
        self.statements: list[str] = []

    def execute(self, statement):
        rendered = str(statement)
        self.statements.append(rendered)
        if rendered.startswith("LOCK TABLE") and self.failure is not None:
            raise self.failure
        return SimpleNamespace(first=lambda: None)


def _database_error(
    *,
    sqlstate: str | None = None,
    pgcode: str | None = None,
) -> sa.exc.OperationalError:
    return sa.exc.OperationalError(
        "LOCK TABLE example IN ACCESS EXCLUSIVE MODE",
        {},
        _OriginalDatabaseError(sqlstate=sqlstate, pgcode=pgcode),
    )


def test_postgres_downgrade_lock_sets_transaction_local_timeout() -> None:
    bind = _RecordingBind()

    acquire_postgres_downgrade_lock(
        bind,
        "LOCK TABLE example IN ACCESS EXCLUSIVE MODE",
        resource="example data",
    )

    assert bind.statements == [
        "SET LOCAL lock_timeout = '5s'",
        "LOCK TABLE example IN ACCESS EXCLUSIVE MODE",
    ]


@pytest.mark.parametrize(
    ("sqlstate", "pgcode"),
    (("55P03", None), (None, "55P03")),
)
def test_postgres_downgrade_lock_timeout_is_actionable(
    sqlstate,
    pgcode,
) -> None:
    bind = _RecordingBind(
        failure=_database_error(sqlstate=sqlstate, pgcode=pgcode)
    )

    with pytest.raises(
        RuntimeError,
        match=(
            "could not acquire the PostgreSQL downgrade safety lock for "
            "example data within 5s"
        ),
    ):
        acquire_postgres_downgrade_lock(
            bind,
            "LOCK TABLE example IN ACCESS EXCLUSIVE MODE",
            resource="example data",
        )


def test_postgres_downgrade_lock_preserves_unrelated_database_error() -> None:
    failure = _database_error(sqlstate="08006")
    bind = _RecordingBind(failure=failure)

    with pytest.raises(sa.exc.OperationalError) as raised:
        acquire_postgres_downgrade_lock(
            bind,
            "LOCK TABLE example IN ACCESS EXCLUSIVE MODE",
            resource="example data",
        )

    assert raised.value is failure


_MIGRATION_LOCK_CASES = (
    (
        "2026_08_12_0900-f4a5b6c7d8e9_decision_agent_correlation.py",
        "_assert_downgrade_is_lossless",
        "LOCK TABLE decision_record IN ACCESS EXCLUSIVE MODE",
        "f4a5b6c7d8e9",
        "e3f4a5b6c7d8",
        "decision_record",
    ),
    (
        "2026_08_12_1800-a5b6c7d8e9f0_decision_domain_policy.py",
        "_assert_downgrade_is_lossless",
        "LOCK TABLE decision_domain_policy IN ACCESS EXCLUSIVE MODE",
        "a5b6c7d8e9f0",
        "f4a5b6c7d8e9",
        "decision_domain_policy",
    ),
    (
        "2026_08_12_2100-b6c7d8e9f0a1_calendar_account_generation.py",
        "_assert_account_generation_downgrade_is_safe",
        "LOCK TABLE calendar_event_mirror, "
        "calendar_mutation_proposal IN ACCESS EXCLUSIVE MODE",
        "b6c7d8e9f0a1",
        "a5b6c7d8e9f0",
        "calendar_event_mirror",
    ),
    (
        "2026_08_14_1200-d8e9f0a1b2c3_wearable_retention_class.py",
        "_assert_downgrade_is_lossless",
        "LOCK TABLE retention_policy, wellness_event "
        "IN ACCESS EXCLUSIVE MODE",
        "d8e9f0a1b2c3",
        "c7d8e9f0a1b2",
        "retention_policy",
    ),
    (
        "2026_08_16_1800-e9f0a1b2c3d4_decision_record_retention.py",
        "_assert_downgrade_is_safe",
        "LOCK TABLE retention_policy, decision_record "
        "IN ACCESS EXCLUSIVE MODE",
        "e9f0a1b2c3d4",
        "d8e9f0a1b2c3",
        "decision_record",
    ),
    (
        "2026_08_16_2100-f0a1b2c3d4e5_decision_request_receipt.py",
        "_assert_downgrade_is_lossless",
        "LOCK TABLE decision_request_receipt IN ACCESS EXCLUSIVE MODE",
        "f0a1b2c3d4e5",
        "e9f0a1b2c3d4",
        "decision_request_receipt",
    ),
    (
        "2026_08_18_1200-d4e5f6a7b8c9_storage_object_file_cleanup.py",
        "_assert_downgrade_is_lossless",
        "LOCK TABLE storage_object IN ACCESS EXCLUSIVE MODE",
        "d4e5f6a7b8c9",
        "c3d4e5f6a7b8",
        "storage_object",
    ),
)


@pytest.mark.parametrize(
    (
        "filename",
        "assertion_name",
        "lock_statement",
        "_source_revision",
        "_target_revision",
        "_blocking_table",
    ),
    _MIGRATION_LOCK_CASES,
)
def test_destructive_migration_lock_waits_are_bounded(
    monkeypatch,
    filename,
    assertion_name,
    lock_statement,
    _source_revision,
    _target_revision,
    _blocking_table,
) -> None:
    path = REPO_ROOT / "alembic" / "versions" / filename
    module_name = f"_healthmes_test_migration_{path.stem}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bind = _RecordingBind(failure=_database_error(sqlstate="55P03"))
    monkeypatch.setattr(
        module,
        "context",
        SimpleNamespace(is_offline_mode=lambda: False),
    )
    monkeypatch.setattr(
        module,
        "op",
        SimpleNamespace(get_bind=lambda: bind),
    )

    with pytest.raises(
        RuntimeError,
        match="could not acquire the PostgreSQL downgrade safety lock",
    ):
        getattr(module, assertion_name)()

    assert bind.statements == [
        "SET LOCAL lock_timeout = '5s'",
        lock_statement,
    ]


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason=(
        "requires a disposable PostgreSQL URL in "
        "HEALTHMES_TEST_POSTGRES_URL"
    ),
)
def test_real_postgres_downgrade_lock_wait_is_bounded() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = sa.create_engine(database_url)
    schema = f"hm_migration_lock_{uuid.uuid4().hex}"
    quoted_schema = admin_engine.dialect.identifier_preparer.quote(schema)
    separator = "&" if "?" in database_url else "?"
    schema_url = (
        f"{database_url}{separator}options=-csearch_path={schema}"
    )
    scoped_engine = sa.create_engine(schema_url)
    try:
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f"CREATE SCHEMA {quoted_schema}"))
        with scoped_engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE lock_target (id integer)"))

        with scoped_engine.connect() as blocker:
            blocker_transaction = blocker.begin()
            blocker.execute(
                sa.text(
                    "LOCK TABLE lock_target IN ACCESS EXCLUSIVE MODE"
                )
            )
            try:
                started = time.monotonic()
                with scoped_engine.begin() as waiter:
                    with pytest.raises(
                        RuntimeError,
                        match=(
                            "could not acquire the PostgreSQL downgrade "
                            "safety lock for test table within 5s"
                        ),
                    ):
                        acquire_postgres_downgrade_lock(
                            waiter,
                            "LOCK TABLE lock_target "
                            "IN ACCESS EXCLUSIVE MODE",
                            resource="test table",
                        )
                elapsed = time.monotonic() - started
                assert 4.0 <= elapsed < 10.0
            finally:
                blocker_transaction.rollback()
    finally:
        scoped_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE")
            )
        admin_engine.dispose()


@pytest.mark.parametrize(
    (
        "_filename",
        "_assertion_name",
        "_lock_statement",
        "source_revision",
        "target_revision",
        "blocking_table",
    ),
    _MIGRATION_LOCK_CASES,
)
@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason=(
        "requires a disposable PostgreSQL URL in "
        "HEALTHMES_TEST_POSTGRES_URL"
    ),
)
def test_real_postgres_migration_timeout_rolls_back_schema(
    monkeypatch,
    _filename,
    _assertion_name,
    _lock_statement,
    source_revision,
    target_revision,
    blocking_table,
) -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = sa.create_engine(database_url)
    schema = f"hm_migration_rollback_{uuid.uuid4().hex}"
    quoted_schema = admin_engine.dialect.identifier_preparer.quote(schema)
    separator = "&" if "?" in database_url else "?"
    schema_url = (
        f"{database_url}{separator}options=-csearch_path={schema}"
    )
    config = _config(schema_url)
    scoped_engine = sa.create_engine(schema_url)
    try:
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f"CREATE SCHEMA {quoted_schema}"))
        command.upgrade(config, source_revision)
        monkeypatch.setattr(
            migration_safety_mod,
            "_POSTGRES_DOWNGRADE_LOCK_TIMEOUT",
            "100ms",
        )

        with scoped_engine.connect() as blocker:
            blocker_transaction = blocker.begin()
            blocker.execute(
                sa.text(
                    f"LOCK TABLE {blocking_table} IN ACCESS SHARE MODE"
                )
            )
            try:
                started = time.monotonic()
                with pytest.raises(
                    RuntimeError,
                    match=(
                        "could not acquire the PostgreSQL downgrade "
                        "safety lock"
                    ),
                ) as raised:
                    command.downgrade(config, target_revision)
                elapsed = time.monotonic() - started
                assert 0.05 <= elapsed < 3.0
                database_error = raised.value.__cause__
                assert isinstance(database_error, sa.exc.DBAPIError)
                assert (
                    getattr(database_error.orig, "sqlstate", None)
                    or getattr(database_error.orig, "pgcode", None)
                ) == "55P03"
            finally:
                blocker_transaction.rollback()

        with scoped_engine.connect() as connection:
            assert connection.scalar(
                sa.text("SELECT version_num FROM alembic_version")
            ) == source_revision
        assert blocking_table in sa.inspect(scoped_engine).get_table_names()
    finally:
        scoped_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE")
            )
        admin_engine.dispose()
