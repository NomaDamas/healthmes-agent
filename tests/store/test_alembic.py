"""Alembic tests: offline SQL rendering, a real sqlite upgrade, model parity.

All runs go through the repo-root ``alembic.ini`` + ``alembic/env.py`` with the
URL injected programmatically (``sqlalchemy.url``), so no environment variables,
network, or running database are needed — postgres is exercised via *offline*
rendering, which never connects.
"""

import io
import logging
import os
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.orm import sessionmaker

from alembic import command
from healthmes.schedule_proposals import resolution_token, verify_resolution_token
from healthmes.store import (
    Base,
    DecisionKind,
    DecisionRecord,
    ProposalStatus,
    RetentionPolicy,
    ScheduleProposal,
    Task,
    session_scope,
)
from healthmes.wearables.provenance import (
    persist_open_wearables_observation,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_TABLES = {
    "weekly_goal",
    "task",
    "calendar_event_mirror",
    "calendar_mutation_proposal",
    "sleep_reconciliation_proposal",
    "schedule_proposal",
    "food_log",
    "app_usage_sample",
    "cognitive_energy_estimate",
    "decision_record",
    "decision_domain_policy",
    "insight",
    "medical_record",
    "trigger_event",
    "raw_ingest_event",
    "retention_policy",
    "storage_object",
    "wellness_event",
    "storage_usage_daily",
    "purge_job",
}


def _config(database_url: str, buffer: io.StringIO | None = None) -> Config:
    # Offline --sql output goes to Config.output_buffer (stdout is only for
    # command chatter), so route both into the capture buffer when given.
    kwargs = {"stdout": buffer, "output_buffer": buffer} if buffer is not None else {}
    config = Config(str(REPO_ROOT / "alembic.ini"), **kwargs)
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _render_offline_upgrade(database_url: str) -> str:
    buffer = io.StringIO()
    command.upgrade(_config(database_url, buffer=buffer), "head", sql=True)
    return buffer.getvalue()


def _render_offline_legacy_cleanup_downgrade(database_url: str) -> str:
    buffer = io.StringIO()
    command.downgrade(
        _config(database_url, buffer=buffer),
        "f2a3b4c5d6e7:e1f2a3b4c5d6",
        sql=True,
    )
    return buffer.getvalue()


def _render_offline_app_usage_generation_downgrade(database_url: str) -> str:
    buffer = io.StringIO()
    command.downgrade(
        _config(database_url, buffer=buffer),
        "d2e3f4a5b6c7:c1d2e3f4a5b6",
        sql=True,
    )
    return buffer.getvalue()


def _render_offline_app_usage_snapshot_downgrade(database_url: str) -> str:
    buffer = io.StringIO()
    command.downgrade(
        _config(database_url, buffer=buffer),
        "e3f4a5b6c7d8:d2e3f4a5b6c7",
        sql=True,
    )
    return buffer.getvalue()


def _render_offline_decision_agent_downgrade(
    database_url: str,
) -> str:
    buffer = io.StringIO()
    command.downgrade(
        _config(database_url, buffer=buffer),
        "f4a5b6c7d8e9:e3f4a5b6c7d8",
        sql=True,
    )
    return buffer.getvalue()


def _render_offline_decision_policy_downgrade(
    database_url: str,
) -> str:
    buffer = io.StringIO()
    command.downgrade(
        _config(database_url, buffer=buffer),
        "a5b6c7d8e9f0:f4a5b6c7d8e9",
        sql=True,
    )
    return buffer.getvalue()


def _render_offline_calendar_generation_downgrade(
    database_url: str,
) -> str:
    buffer = io.StringIO()
    command.downgrade(
        _config(database_url, buffer=buffer),
        "b6c7d8e9f0a1:a5b6c7d8e9f0",
        sql=True,
    )
    return buffer.getvalue()


def _render_offline_wearable_retention_upgrade(
    database_url: str,
) -> str:
    buffer = io.StringIO()
    command.upgrade(
        _config(database_url, buffer=buffer),
        "c7d8e9f0a1b2:d8e9f0a1b2c3",
        sql=True,
    )
    return buffer.getvalue()


def _render_offline_wearable_retention_downgrade(
    database_url: str,
) -> str:
    buffer = io.StringIO()
    command.downgrade(
        _config(database_url, buffer=buffer),
        "d8e9f0a1b2c3:c7d8e9f0a1b2",
        sql=True,
    )
    return buffer.getvalue()


def test_migration_graph_has_single_head():
    script = ScriptDirectory.from_config(_config("sqlite://"))

    assert len(script.get_heads()) == 1


def test_migration_keeps_existing_application_loggers_enabled():
    logger = logging.getLogger("healthmes.calendars.sleep_job")
    logger.disabled = False

    _render_offline_upgrade("sqlite:///offline-render.db")

    assert logger.disabled is False


class TestOfflineRender:
    def test_sqlite_render_creates_all_tables(self):
        rendered = _render_offline_upgrade("sqlite:///offline-render.db")
        for table in EXPECTED_TABLES:
            assert f"CREATE TABLE {table}" in rendered
        assert "CREATE TABLE alembic_version" in rendered
        # sqlite gets plain JSON, not the postgres variant
        assert "JSONB" not in rendered

    def test_postgres_render_uses_native_types(self):
        rendered = _render_offline_upgrade(
            "postgresql+psycopg://healthmes:healthmes@localhost:5432/healthmes"
        )
        for table in EXPECTED_TABLES:
            assert f"CREATE TABLE {table}" in rendered
        assert "JSONB" in rendered  # portable JSON variant became JSONB
        assert "UUID" in rendered  # sa.Uuid became native UUID
        # enums stay portable VARCHAR: no postgres CREATE TYPE
        assert "CREATE TYPE" not in rendered

    @pytest.mark.parametrize(
        "database_url",
        (
            "sqlite:///offline-render.db",
            "postgresql+psycopg://healthmes:healthmes@localhost:5432/healthmes",
        ),
    )
    def test_decision_agent_offline_render_keeps_correlation_invariant(
        self,
        database_url,
    ):
        rendered = _render_offline_upgrade(database_url)

        assert (
            "decision_agent_correlation_complete"
            in rendered
        )
        assert "decision_payload_digest" in rendered
        if database_url.startswith("sqlite"):
            assert "_alembic_tmp_decision_record" in rendered

    @pytest.mark.parametrize(
        "database_url",
        (
            "sqlite:///offline-render.db",
            "postgresql+psycopg://healthmes:healthmes@localhost:5432/healthmes",
        ),
    )
    def test_decision_agent_offline_downgrade_is_refused(
        self,
        database_url,
    ):
        with pytest.raises(
            RuntimeError,
            match="offline downgrade cannot verify Decision Agent records",
        ):
            _render_offline_decision_agent_downgrade(database_url)

    @pytest.mark.parametrize(
        "database_url",
        (
            "sqlite:///offline-render.db",
            "postgresql+psycopg://healthmes:healthmes@localhost:5432/healthmes",
        ),
    )
    def test_decision_policy_offline_downgrade_is_refused(
        self,
        database_url,
    ):
        with pytest.raises(
            RuntimeError,
            match=(
                "offline downgrade cannot verify Decision Agent "
                "domain consent"
            ),
        ):
            _render_offline_decision_policy_downgrade(database_url)

    @pytest.mark.parametrize(
        "database_url",
        (
            "sqlite:///offline-render.db",
            "postgresql+psycopg://healthmes:healthmes@localhost:5432/healthmes",
        ),
    )
    def test_calendar_generation_offline_downgrade_is_refused_before_ddl(
        self,
        database_url,
    ):
        buffer = io.StringIO()
        with pytest.raises(
            RuntimeError,
            match=(
                "offline downgrade cannot verify Calendar "
                "account-generation safety"
            ),
        ):
            command.downgrade(
                _config(database_url, buffer=buffer),
                "b6c7d8e9f0a1:a5b6c7d8e9f0",
                sql=True,
            )
        rendered = buffer.getvalue()
        assert "DROP COLUMN" not in rendered
        assert "DROP TABLE" not in rendered
        assert "ALTER TABLE calendar_" not in rendered

    @pytest.mark.parametrize(
        "database_url",
        (
            "sqlite:///offline-render.db",
            "postgresql+psycopg://healthmes:healthmes@localhost:5432/healthmes",
        ),
    )
    def test_calendar_generation_offline_upgrade_renders_safety_contract(
        self,
        database_url,
    ):
        rendered = _render_offline_upgrade(database_url)

        assert "connection_generation" in rendered
        assert "__legacy_unbound__" in rendered
        assert "ck_calendar_mutation_proposal_active_generation" in rendered
        if database_url.startswith("sqlite"):
            assert "_alembic_tmp_calendar_event_mirror" in rendered

    @pytest.mark.parametrize(
        "database_url",
        (
            "sqlite:///offline-render.db",
            "postgresql+psycopg://healthmes:healthmes@localhost:5432/healthmes",
        ),
    )
    def test_wearable_retention_offline_downgrade_requires_online_check(
        self,
        database_url,
    ):
        with pytest.raises(
            RuntimeError,
            match=(
                "offline downgrade cannot verify wearable "
                "retention policy"
            ),
        ):
            _render_offline_wearable_retention_downgrade(
                database_url
            )

    def test_wearable_retention_offline_uuid_matches_each_dialect(self):
        sqlite_sql = _render_offline_wearable_retention_upgrade(
            "sqlite:///offline-render.db"
        )
        postgres_sql = _render_offline_wearable_retention_upgrade(
            "postgresql+psycopg://healthmes:healthmes@localhost:5432/healthmes"
        )

        assert "d8e9f0a1b2c34d5e8f90a1b2c3d4e5f6" in sqlite_sql
        assert "d8e9f0a1-b2c3-4d5e-8f90-a1b2c3d4e5f6" not in sqlite_sql
        assert "d8e9f0a1-b2c3-4d5e-8f90-a1b2c3d4e5f6" in postgres_sql

    def test_render_marks_head_revision(self):
        rendered = _render_offline_upgrade("sqlite:///offline-render.db")
        assert "INSERT INTO alembic_version" in rendered

    def test_legacy_cleanup_downgrade_renders_for_both_dialects(self):
        urls = (
            "sqlite:///offline-render.db",
            "postgresql+psycopg://healthmes:healthmes@localhost:5432/healthmes",
        )
        for url in urls:
            rendered = _render_offline_legacy_cleanup_downgrade(url)
            assert "ROW_NUMBER() OVER" in rendered
            assert "ux_calendar_event_mirror_source_healthmes_source_key" in rendered

    def test_app_usage_generation_downgrade_renders_for_both_dialects(self):
        sqlite_sql = _render_offline_app_usage_generation_downgrade(
            "sqlite:///offline-render.db"
        )
        postgres_sql = _render_offline_app_usage_generation_downgrade(
            "postgresql+psycopg://healthmes:healthmes@localhost:5432/healthmes"
        )

        assert "_alembic_tmp_app_usage_sample" in sqlite_sql
        assert "DROP TABLE app_usage_sample" in sqlite_sql
        assert "DROP COLUMN collection_generation" in postgres_sql

    @pytest.mark.parametrize(
        "database_url",
        (
            "sqlite:///offline-render.db",
            "postgresql+psycopg://healthmes:healthmes@localhost:5432/healthmes",
        ),
    )
    def test_app_usage_snapshot_downgrade_requires_online_check(
        self,
        database_url,
    ):
        with pytest.raises(
            RuntimeError,
            match="offline downgrade cannot verify Android snapshot state",
        ):
            _render_offline_app_usage_snapshot_downgrade(database_url)


class TestSqliteUpgrade:
    def test_upgrade_creates_schema_and_is_usable(self, tmp_path):
        database_url = f"sqlite:///{tmp_path / 'migrated.db'}"
        command.upgrade(_config(database_url), "head")

        engine = sa.create_engine(database_url)
        try:
            inspector = sa.inspect(engine)
            assert EXPECTED_TABLES <= set(inspector.get_table_names())
            assert "alembic_version" in inspector.get_table_names()

            # The migrated schema (not create_all) accepts real ORM writes.
            factory = sessionmaker(bind=engine)
            with session_scope(factory) as session:
                session.add(Task(title="smoke"))
                session.add(
                    DecisionRecord(
                        kind=DecisionKind.INSIGHT,
                        tree={"id": "root", "children": []},
                        summary="smoke",
                    )
                )
            with factory() as session:
                task = session.scalars(sa.select(Task)).one()
                assert task.title == "smoke"
                assert task.status == "todo"
                record = session.scalars(sa.select(DecisionRecord)).one()
                assert record.kind is DecisionKind.INSIGHT
                assert record.tree == {"id": "root", "children": []}
        finally:
            engine.dispose()

    def test_upgrade_matches_model_metadata(self, tmp_path):
        """Autogenerate against the migrated database must produce an empty diff."""
        database_url = f"sqlite:///{tmp_path / 'parity.db'}"
        command.upgrade(_config(database_url), "head")

        engine = sa.create_engine(database_url)
        try:
            with engine.connect() as connection:
                context = MigrationContext.configure(connection)
                diff = compare_metadata(context, Base.metadata)
            assert diff == []
        finally:
            engine.dispose()

    def test_wearable_retention_offline_sql_supports_orm_writes(
        self,
        tmp_path,
    ):
        database_path = tmp_path / "wearable-retention-offline.db"
        database_url = f"sqlite:///{database_path}"
        command.upgrade(_config(database_url), "c7d8e9f0a1b2")
        rendered = _render_offline_wearable_retention_upgrade(
            database_url
        )

        connection = sqlite3.connect(database_path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(rendered)
            assert connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall() == []
        finally:
            connection.close()

        engine = sa.create_engine(database_url)
        factory = sessionmaker(
            bind=engine,
            autocommit=False,
            autoflush=False,
        )
        try:
            with factory() as session:
                snapshot = persist_open_wearables_observation(
                    session,
                    normalized_context={
                        "date": "2026-08-14",
                        "status": "ok",
                    },
                    local_day=date(2026, 8, 14),
                    timezone="UTC",
                    collected_at=datetime(
                        2026,
                        8,
                        14,
                        12,
                        tzinfo=UTC,
                    ),
                    now=datetime(
                        2026,
                        8,
                        14,
                        12,
                        tzinfo=UTC,
                    ),
                )
                session.commit()
                policy = session.scalar(
                    sa.select(RetentionPolicy).where(
                        RetentionPolicy.data_class
                        == "wearable_normalized"
                    )
                )
                assert policy is not None
                assert snapshot.content_event_id is not None
                assert policy.id.hex == (
                    "d8e9f0a1b2c34d5e8f90a1b2c3d4e5f6"
                )
            with engine.connect() as connection:
                assert connection.exec_driver_sql(
                    "PRAGMA foreign_key_check"
                ).all() == []
        finally:
            engine.dispose()

    def test_wearable_retention_migration_round_trip(self, tmp_path):
        database_url = f"sqlite:///{tmp_path / 'wearable-retention.db'}"
        config = _config(database_url)
        command.upgrade(config, "c7d8e9f0a1b2")

        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        retention_policy = sa.Table(
            "retention_policy",
            metadata,
            autoload_with=engine,
        )
        wellness_event = sa.Table(
            "wellness_event",
            metadata,
            autoload_with=engine,
        )
        generic_policy_id = uuid.uuid4().hex
        wearable_event_id = uuid.uuid4().hex
        generic_event_id = uuid.uuid4().hex
        observed_at = datetime(2026, 8, 1, 9, tzinfo=UTC)
        # The migration must never resurrect data by extending an existing
        # shorter expiry to the new policy duration.
        original_expiry = observed_at + timedelta(days=3)
        with engine.begin() as connection:
            connection.execute(
                retention_policy.insert().values(
                    id=generic_policy_id,
                    data_class="normalized",
                    retention_days=14,
                    enabled=True,
                )
            )
            base_event = {
                "schema_version": 1,
                "observed_at": observed_at,
                "recorded_at": observed_at,
                "timezone": "UTC",
                "source_device": None,
                "capture_method": "import",
                "sensitivity": "wellness",
                "consent_scope": "personal",
                "retention_policy_id": generic_policy_id,
                "expires_at": original_expiry,
                "payload": {},
            }
            connection.execute(
                wellness_event.insert(),
                [
                    {
                        **base_event,
                        "id": wearable_event_id,
                        "event_type": "wearable.sleep.v1",
                        "source_provider": (
                            "healthmes-open-wearables-mirror"
                        ),
                        "source_record_id": "wearable:sleep:1",
                    },
                    {
                        **base_event,
                        "id": generic_event_id,
                        "event_type": "nutrition.meal.v1",
                        "source_provider": "healthmes-nutrition",
                        "source_record_id": "nutrition:meal:1",
                    },
                ],
            )
        engine.dispose()

        command.upgrade(config, "head")

        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        migrated_policy = sa.Table(
            "retention_policy",
            metadata,
            autoload_with=engine,
        )
        migrated_event = sa.Table(
            "wellness_event",
            metadata,
            autoload_with=engine,
        )
        with engine.connect() as connection:
            policies = {
                row.data_class: row
                for row in connection.execute(sa.select(migrated_policy))
            }
            wearable_policy = policies["wearable_normalized"]
            assert wearable_policy.retention_days == 14
            assert wearable_policy.enabled is True

            events = {
                row.id: row
                for row in connection.execute(sa.select(migrated_event))
            }
            wearable = events[wearable_event_id]
            generic = events[generic_event_id]
            assert wearable.retention_policy_id == wearable_policy.id
            assert wearable.expires_at == original_expiry.replace(tzinfo=None)
            assert generic.retention_policy_id == generic_policy_id
            assert generic.expires_at == original_expiry.replace(tzinfo=None)
        engine.dispose()

        command.downgrade(config, "c7d8e9f0a1b2")

        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        restored_policy = sa.Table(
            "retention_policy",
            metadata,
            autoload_with=engine,
        )
        restored_event = sa.Table(
            "wellness_event",
            metadata,
            autoload_with=engine,
        )
        try:
            with engine.connect() as connection:
                assert connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(restored_policy)
                    .where(
                        restored_policy.c.data_class
                        == "wearable_normalized"
                    )
                ) == 0
                restored = connection.execute(
                    sa.select(restored_event).where(
                        restored_event.c.id == wearable_event_id
                    )
                ).one()
                assert restored.retention_policy_id == generic_policy_id
                assert restored.expires_at == original_expiry.replace(
                    tzinfo=None
                )
                assert connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                ) == "c7d8e9f0a1b2"
        finally:
            engine.dispose()

    def test_wearable_retention_downgrade_recreates_missing_generic_policy(
        self,
        tmp_path,
    ):
        database_url = (
            f"sqlite:///{tmp_path / 'wearable-retention-no-generic.db'}"
        )
        config = _config(database_url)
        command.upgrade(config, "c7d8e9f0a1b2")

        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        retention_policy = sa.Table(
            "retention_policy",
            metadata,
            autoload_with=engine,
        )
        wellness_event = sa.Table(
            "wellness_event",
            metadata,
            autoload_with=engine,
        )
        legacy_policy_id = uuid.uuid4().hex
        wearable_event_id = uuid.uuid4().hex
        observed_at = datetime(2026, 8, 1, 9, tzinfo=UTC)
        with engine.begin() as connection:
            connection.execute(
                retention_policy.insert().values(
                    id=legacy_policy_id,
                    data_class="legacy_wearable",
                    retention_days=3,
                    enabled=True,
                )
            )
            connection.execute(
                wellness_event.insert().values(
                    id=wearable_event_id,
                    event_type="wearable.sleep.v1",
                    schema_version=1,
                    observed_at=observed_at,
                    recorded_at=observed_at,
                    timezone="UTC",
                    source_provider="healthmes-open-wearables-mirror",
                    source_device=None,
                    source_record_id="wearable:sleep:no-generic",
                    capture_method="import",
                    sensitivity="wellness",
                    consent_scope="personal",
                    retention_policy_id=legacy_policy_id,
                    expires_at=observed_at + timedelta(days=3),
                    payload={},
                )
            )
        engine.dispose()

        command.upgrade(config, "head")
        command.downgrade(config, "c7d8e9f0a1b2")

        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        restored_policy = sa.Table(
            "retention_policy",
            metadata,
            autoload_with=engine,
        )
        restored_event = sa.Table(
            "wellness_event",
            metadata,
            autoload_with=engine,
        )
        try:
            with engine.connect() as connection:
                policies = {
                    row.data_class: row
                    for row in connection.execute(
                        sa.select(restored_policy)
                    )
                }
                assert "wearable_normalized" not in policies
                generic = policies["normalized"]
                assert generic.retention_days == 30
                assert generic.enabled is True
                event = connection.execute(
                    sa.select(restored_event).where(
                        restored_event.c.id == wearable_event_id
                    )
                ).one()
                assert event.retention_policy_id == generic.id
                assert event.expires_at == (
                    observed_at + timedelta(days=3)
                ).replace(tzinfo=None)
        finally:
            engine.dispose()

    def test_wearable_retention_downgrade_refuses_preexisting_policy_loss(
        self,
        tmp_path,
    ):
        database_url = (
            f"sqlite:///{tmp_path / 'wearable-retention-existing.db'}"
        )
        config = _config(database_url)
        command.upgrade(config, "c7d8e9f0a1b2")

        engine = sa.create_engine(database_url)
        retention_policy = sa.Table(
            "retention_policy",
            sa.MetaData(),
            autoload_with=engine,
        )
        generic_policy_id = uuid.uuid4().hex
        wearable_policy_id = uuid.uuid4().hex
        with engine.begin() as connection:
            connection.execute(
                retention_policy.insert(),
                [
                    {
                        "id": generic_policy_id,
                        "data_class": "normalized",
                        "retention_days": 30,
                        "enabled": True,
                    },
                    {
                        "id": wearable_policy_id,
                        "data_class": "wearable_normalized",
                        "retention_days": 1,
                        "enabled": True,
                    },
                ],
            )
        engine.dispose()

        command.upgrade(config, "head")
        with pytest.raises(
            RuntimeError,
            match=(
                "cannot downgrade wearable retention without losing "
                "its dedicated retention policy"
            ),
        ):
            command.downgrade(config, "c7d8e9f0a1b2")

        engine = sa.create_engine(database_url)
        restored_policy = sa.Table(
            "retention_policy",
            sa.MetaData(),
            autoload_with=engine,
        )
        try:
            with engine.connect() as connection:
                wearable = connection.execute(
                    sa.select(restored_policy).where(
                        restored_policy.c.data_class
                        == "wearable_normalized"
                    )
                ).one()
                assert wearable.id == wearable_policy_id
                assert wearable.retention_days == 1
                assert wearable.enabled is True
                assert connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                ) == "d8e9f0a1b2c3"
        finally:
            engine.dispose()

    def test_wearable_retention_downgrade_refuses_changed_owned_policy(
        self,
        tmp_path,
    ):
        database_url = (
            f"sqlite:///{tmp_path / 'wearable-retention-no-extension.db'}"
        )
        config = _config(database_url)
        command.upgrade(config, "c7d8e9f0a1b2")

        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        retention_policy = sa.Table(
            "retention_policy",
            metadata,
            autoload_with=engine,
        )
        wellness_event = sa.Table(
            "wellness_event",
            metadata,
            autoload_with=engine,
        )
        generic_policy_id = uuid.uuid4().hex
        wearable_event_id = uuid.uuid4().hex
        observed_at = datetime(2026, 8, 1, 9, tzinfo=UTC)
        with engine.begin() as connection:
            connection.execute(
                retention_policy.insert().values(
                    id=generic_policy_id,
                    data_class="normalized",
                    retention_days=30,
                    enabled=True,
                )
            )
            connection.execute(
                wellness_event.insert().values(
                    id=wearable_event_id,
                    event_type="wearable.sleep.v1",
                    schema_version=1,
                    observed_at=observed_at,
                    recorded_at=observed_at,
                    timezone="UTC",
                    source_provider="healthmes-open-wearables-mirror",
                    source_device=None,
                    source_record_id="wearable:sleep:short-expiry",
                    capture_method="import",
                    sensitivity="wellness",
                    consent_scope="personal",
                    retention_policy_id=generic_policy_id,
                    expires_at=observed_at + timedelta(days=30),
                    payload={},
                )
            )
        engine.dispose()

        command.upgrade(config, "head")

        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        retention_policy = sa.Table(
            "retention_policy",
            metadata,
            autoload_with=engine,
        )
        wellness_event = sa.Table(
            "wellness_event",
            metadata,
            autoload_with=engine,
        )
        short_expiry = observed_at + timedelta(days=1)
        with engine.begin() as connection:
            wearable_policy_id = connection.scalar(
                sa.select(retention_policy.c.id).where(
                    retention_policy.c.data_class
                    == "wearable_normalized"
                )
            )
            connection.execute(
                retention_policy.update()
                .where(retention_policy.c.id == wearable_policy_id)
                .values(retention_days=1)
            )
            connection.execute(
                wellness_event.update()
                .where(wellness_event.c.id == wearable_event_id)
                .values(expires_at=short_expiry)
            )
        engine.dispose()

        with pytest.raises(
            RuntimeError,
            match=(
                "cannot downgrade wearable retention without losing "
                "its dedicated retention policy"
            ),
        ):
            command.downgrade(config, "c7d8e9f0a1b2")

        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        restored_event = sa.Table(
            "wellness_event",
            metadata,
            autoload_with=engine,
        )
        try:
            with engine.connect() as connection:
                event = connection.execute(
                    sa.select(restored_event).where(
                        restored_event.c.id == wearable_event_id
                    )
                ).one()
                assert event.retention_policy_id == wearable_policy_id
                assert event.expires_at == short_expiry.replace(tzinfo=None)
                assert connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                ) == "d8e9f0a1b2c3"
        finally:
            engine.dispose()

    def test_calendar_generation_upgrade_quarantines_legacy_rows(
        self,
        tmp_path,
    ):
        database_url = f"sqlite:///{tmp_path / 'calendar-generation.db'}"
        config = _config(database_url)
        command.upgrade(config, "a5b6c7d8e9f0")

        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        mirror = sa.Table(
            "calendar_event_mirror",
            metadata,
            autoload_with=engine,
        )
        proposal = sa.Table(
            "calendar_mutation_proposal",
            metadata,
            autoload_with=engine,
        )
        mirror_id = uuid.uuid4().hex
        start_at = datetime(2026, 8, 12, 9, tzinfo=UTC)
        end_at = start_at + timedelta(hours=1)
        base_proposal = {
            "calendar_source": "google",
            "mirror_event_id": mirror_id,
            "external_event_id": "legacy-event",
            "operation": "shorten",
            "original_start_at": start_at,
            "original_end_at": end_at,
            "proposed_start_at": start_at,
            "proposed_end_at": end_at - timedelta(minutes=15),
            "expected_etag": '"legacy-etag"',
            "protected_fingerprint": "legacy-fingerprint",
            "reply_handle_digest": "legacy-reply",
            "expires_at": end_at,
        }
        try:
            with engine.begin() as connection:
                connection.execute(
                    mirror.insert().values(
                        id=mirror_id,
                        external_id="legacy-event",
                        calendar_source="google",
                        summary="Legacy event",
                        start_at=start_at,
                        end_at=end_at,
                        is_agent_created=False,
                    )
                )
                connection.execute(
                    proposal.insert(),
                    [
                        {
                            **base_proposal,
                            "id": uuid.uuid4().hex,
                            "status": "pending",
                            "dedup_key": "legacy-pending",
                        },
                        {
                            **base_proposal,
                            "id": uuid.uuid4().hex,
                            "status": "applying",
                            "dedup_key": "legacy-applying",
                        },
                        {
                            **base_proposal,
                            "id": uuid.uuid4().hex,
                            "status": "applied",
                            "dedup_key": "legacy-applied",
                        },
                    ],
                )
        finally:
            engine.dispose()

        command.upgrade(config, "head")

        engine = sa.create_engine(database_url)
        try:
            inspector = sa.inspect(engine)
            mirror_columns = {
                item["name"]: item
                for item in inspector.get_columns("calendar_event_mirror")
            }
            checks = {
                item["name"]
                for item in inspector.get_check_constraints(
                    "calendar_mutation_proposal"
                )
            }
            assert mirror_columns["connection_generation"]["nullable"] is False
            assert "ck_calendar_mutation_proposal_active_generation" in checks

            migrated_mirror = sa.Table(
                "calendar_event_mirror",
                sa.MetaData(),
                autoload_with=engine,
            )
            migrated_proposal = sa.Table(
                "calendar_mutation_proposal",
                sa.MetaData(),
                autoload_with=engine,
            )
            with engine.begin() as connection:
                assert connection.scalar(
                    sa.select(migrated_mirror.c.connection_generation)
                    .where(migrated_mirror.c.id == mirror_id)
                ) == "__legacy_unbound__"
                statuses = {
                    row.dedup_key: row.status
                    for row in connection.execute(
                        sa.select(
                            migrated_proposal.c.dedup_key,
                            migrated_proposal.c.status,
                        )
                    )
                }
                assert statuses == {
                    "legacy-pending": "conflicted",
                    "legacy-applying": "unknown",
                    "legacy-applied": "applied",
                }
                with connection.begin_nested():
                    with pytest.raises(sa.exc.IntegrityError):
                        connection.execute(
                            migrated_mirror.insert().values(
                                id=uuid.uuid4().hex,
                                external_id="legacy-event",
                                calendar_source="google",
                                start_at=start_at,
                                end_at=end_at,
                                is_agent_created=False,
                            )
                        )

            with engine.begin() as connection:
                connection.execute(
                    migrated_mirror.insert().values(
                        id=uuid.uuid4().hex,
                        external_id="legacy-event",
                        calendar_source="google",
                        connection_generation="reconnected-account",
                        start_at=start_at,
                        end_at=end_at,
                        is_agent_created=False,
                    )
                )
        finally:
            engine.dispose()

    def test_schedule_intake_generation_upgrade_binds_or_invalidates_legacy_rows(
        self,
        tmp_path,
    ):
        database_url = (
            f"sqlite:///{tmp_path / 'schedule-intake-generation.db'}"
        )
        config = _config(database_url)
        command.upgrade(config, "b6c7d8e9f0a1")

        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        task = sa.Table("task", metadata, autoload_with=engine)
        mirror = sa.Table(
            "calendar_event_mirror",
            metadata,
            autoload_with=engine,
        )
        proposal = sa.Table(
            "schedule_proposal",
            metadata,
            autoload_with=engine,
        )
        start_at = datetime(2026, 8, 13, 9, tzinfo=UTC)
        end_at = start_at + timedelta(hours=1)
        task_ids = {
            name: uuid.uuid4().hex
            for name in ("bound", "ambiguous", "incomplete")
        }
        proposal_ids = {
            name: uuid.uuid4().hex
            for name in task_ids
        }
        try:
            with engine.begin() as connection:
                connection.execute(
                    task.insert(),
                    [
                        {
                            "id": task_id,
                            "title": name,
                            "status": "todo",
                            "source": "user",
                            "energy_demand": "med",
                        }
                        for name, task_id in task_ids.items()
                    ],
                )
                connection.execute(
                    mirror.insert(),
                    [
                        {
                            "id": uuid.uuid4().hex,
                            "external_id": "bound-event",
                            "calendar_source": "google",
                            "connection_generation": "google-account-a",
                            "start_at": start_at,
                            "end_at": end_at,
                            "is_agent_created": False,
                        },
                        *[
                            {
                                "id": uuid.uuid4().hex,
                                "external_id": "ambiguous-event",
                                "calendar_source": "google",
                                "connection_generation": generation,
                                "start_at": start_at,
                                "end_at": end_at,
                                "is_agent_created": False,
                            }
                            for generation in (
                                "google-account-a",
                                "google-account-b",
                            )
                        ],
                    ],
                )
                connection.execute(
                    proposal.insert(),
                    [
                        {
                            "id": proposal_ids["bound"],
                            "task_id": task_ids["bound"],
                            "proposed_start": start_at,
                            "proposed_end": end_at,
                            "status": "proposed",
                            "intake_calendar_source": "google",
                            "intake_external_id": "bound-event",
                            "intake_revision": "revision-bound",
                        },
                        {
                            "id": proposal_ids["ambiguous"],
                            "task_id": task_ids["ambiguous"],
                            "proposed_start": start_at,
                            "proposed_end": end_at,
                            "status": "accepted",
                            "intake_calendar_source": "google",
                            "intake_external_id": "ambiguous-event",
                            "intake_revision": "revision-ambiguous",
                        },
                        {
                            "id": proposal_ids["incomplete"],
                            "task_id": task_ids["incomplete"],
                            "proposed_start": start_at,
                            "proposed_end": end_at,
                            "status": "proposed",
                            "intake_calendar_source": None,
                            "intake_external_id": "missing-source",
                            "intake_revision": None,
                        },
                    ],
                )
        finally:
            engine.dispose()

        command.upgrade(config, "head")

        engine = sa.create_engine(database_url)
        try:
            migrated = sa.Table(
                "schedule_proposal",
                sa.MetaData(),
                autoload_with=engine,
            )
            with engine.connect() as connection:
                rows = {
                    row.id: row
                    for row in connection.execute(
                        sa.select(migrated).where(
                            migrated.c.id.in_(proposal_ids.values())
                        )
                    )
                }
                bound = rows[proposal_ids["bound"]]
                assert bound.intake_account_generation == "google-account-a"
                assert bound.status == "proposed"
                assert bound.invalidation_reason is None
                for name in ("ambiguous", "incomplete"):
                    unresolved = rows[proposal_ids[name]]
                    assert unresolved.intake_account_generation is None
                    assert unresolved.status == "invalidated"
                    assert (
                        unresolved.invalidation_reason
                        == "calendar_intake_generation_unresolved"
                    )
                assert connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                ) == "d8e9f0a1b2c3"
        finally:
            engine.dispose()

        command.downgrade(config, "b6c7d8e9f0a1")
        engine = sa.create_engine(database_url)
        try:
            columns = {
                item["name"]
                for item in sa.inspect(engine).get_columns(
                    "schedule_proposal"
                )
            }
            assert "intake_account_generation" not in columns
            assert "invalidation_reason" not in columns
        finally:
            engine.dispose()

    def test_calendar_generation_downgrade_failure_preserves_state(
        self,
        tmp_path,
    ):
        database_url = f"sqlite:///{tmp_path / 'calendar-downgrade.db'}"
        config = _config(database_url)
        command.upgrade(config, "head")

        engine = sa.create_engine(database_url)
        proposal = sa.Table(
            "calendar_mutation_proposal",
            sa.MetaData(),
            autoload_with=engine,
        )
        proposal_id = uuid.uuid4().hex
        start_at = datetime(2026, 8, 12, 9, tzinfo=UTC)
        try:
            with engine.begin() as connection:
                connection.execute(
                    proposal.insert().values(
                        id=proposal_id,
                        calendar_source="google",
                        account_generation="connected-account",
                        external_event_id="active-event",
                        operation="shorten",
                        original_start_at=start_at,
                        original_end_at=start_at + timedelta(hours=1),
                        proposed_start_at=start_at,
                        proposed_end_at=start_at + timedelta(minutes=45),
                        expected_etag='"etag"',
                        protected_fingerprint="fingerprint",
                        reply_handle_digest="reply",
                        expires_at=start_at + timedelta(hours=1),
                        status="pending",
                        dedup_key="active-proposal",
                    )
                )
        finally:
            engine.dispose()

        with pytest.raises(
            RuntimeError,
            match="calendar mutation proposals are still active",
        ):
            command.downgrade(config, "a5b6c7d8e9f0")

        engine = sa.create_engine(database_url)
        try:
            inspector = sa.inspect(engine)
            assert "account_generation" in {
                item["name"]
                for item in inspector.get_columns(
                    "calendar_mutation_proposal"
                )
            }
            assert "connection_generation" in {
                item["name"]
                for item in inspector.get_columns("calendar_event_mirror")
            }
            with engine.begin() as connection:
                assert connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                ) == "b6c7d8e9f0a1"
                assert connection.scalar(
                    sa.select(proposal.c.status).where(
                        proposal.c.id == proposal_id
                    )
                ) == "pending"
                connection.execute(
                    proposal.update()
                    .where(proposal.c.id == proposal_id)
                    .values(status="conflicted")
                )
        finally:
            engine.dispose()

        command.downgrade(config, "a5b6c7d8e9f0")
        engine = sa.create_engine(database_url)
        try:
            inspector = sa.inspect(engine)
            assert "account_generation" not in {
                item["name"]
                for item in inspector.get_columns(
                    "calendar_mutation_proposal"
                )
            }
            assert "connection_generation" not in {
                item["name"]
                for item in inspector.get_columns("calendar_event_mirror")
            }
            with engine.connect() as connection:
                assert connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                ) == "a5b6c7d8e9f0"
                assert connection.scalar(
                    sa.text(
                        "SELECT status FROM calendar_mutation_proposal "
                        "WHERE id = :id"
                    ),
                    {"id": proposal_id},
                ) == "conflicted"
        finally:
            engine.dispose()

    def test_calendar_generation_downgrade_waits_for_sqlite_writer(
        self,
        tmp_path,
    ):
        database_url = f"sqlite:///{tmp_path / 'calendar-race.db'}"
        config = _config(database_url)
        command.upgrade(config, "head")

        engine = sa.create_engine(
            database_url,
            connect_args={"timeout": 5},
        )
        proposal = sa.Table(
            "calendar_mutation_proposal",
            sa.MetaData(),
            autoload_with=engine,
        )
        proposal_id = uuid.uuid4().hex
        start_at = datetime(2026, 8, 12, 9, tzinfo=UTC)
        try:
            with engine.connect() as writer:
                transaction = writer.begin()
                writer.execute(
                    proposal.insert().values(
                        id=proposal_id,
                        calendar_source="google",
                        account_generation="connected-account",
                        external_event_id="racing-event",
                        operation="shorten",
                        original_start_at=start_at,
                        original_end_at=start_at + timedelta(hours=1),
                        proposed_start_at=start_at,
                        proposed_end_at=start_at + timedelta(minutes=45),
                        expected_etag='"etag"',
                        protected_fingerprint="fingerprint",
                        reply_handle_digest="reply-race",
                        expires_at=start_at + timedelta(hours=1),
                        status="pending",
                        dedup_key="racing-proposal",
                    )
                )
                with ThreadPoolExecutor(max_workers=1) as pool:
                    downgrade = pool.submit(
                        command.downgrade,
                        _config(database_url),
                        "a5b6c7d8e9f0",
                    )
                    time.sleep(0.2)
                    assert downgrade.done() is False
                    transaction.commit()
                    with pytest.raises(
                        RuntimeError,
                        match="calendar mutation proposals are still active",
                    ):
                        downgrade.result(timeout=5)
        finally:
            engine.dispose()

        engine = sa.create_engine(database_url)
        try:
            with engine.connect() as connection:
                assert connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                ) == "b6c7d8e9f0a1"
                assert connection.scalar(
                    sa.text(
                        "SELECT status FROM calendar_mutation_proposal "
                        "WHERE id = :id"
                    ),
                    {"id": proposal_id},
                ) == "pending"
        finally:
            engine.dispose()

    def test_decision_agent_migration_preserves_legacy_rows_and_constraints(
        self,
        tmp_path,
    ):
        database_url = f"sqlite:///{tmp_path / 'decision-agent.db'}"
        config = _config(database_url)
        command.upgrade(config, "e3f4a5b6c7d8")

        engine = sa.create_engine(database_url)
        legacy_id = uuid.uuid4().hex
        legacy = sa.Table(
            "decision_record",
            sa.MetaData(),
            autoload_with=engine,
        )
        with engine.begin() as connection:
            connection.execute(
                legacy.insert().values(
                    id=legacy_id,
                    kind="insight",
                    tree={
                        "id": "legacy",
                        "type": "llm_step",
                        "label": "legacy decision",
                        "children": [],
                    },
                    summary="legacy decision",
                )
            )
        engine.dispose()

        command.upgrade(config, "head")

        engine = sa.create_engine(database_url)
        correlated_id = uuid.uuid4().hex
        try:
            inspector = sa.inspect(engine)
            columns = {
                item["name"]
                for item in inspector.get_columns("decision_record")
            }
            indexes = {
                item["name"]: item
                for item in inspector.get_indexes("decision_record")
            }
            checks = {
                item["name"]
                for item in inspector.get_check_constraints(
                    "decision_record"
                )
            }
            assert {
                "decision_request_id",
                "decision_turn_id",
                "decision_request_fingerprint",
                "decision_payload",
                "decision_payload_digest",
            } <= columns
            assert indexes[
                "ux_decision_record_decision_request_id"
            ]["unique"]
            assert indexes[
                "ux_decision_record_decision_turn_id"
            ]["unique"]
            assert (
                "ck_decision_record_decision_agent_correlation_complete"
                in checks
            )

            migrated = sa.Table(
                "decision_record",
                sa.MetaData(),
                autoload_with=engine,
            )
            with engine.begin() as connection:
                preserved = connection.execute(
                    sa.select(migrated).where(
                        migrated.c.id == legacy_id
                    )
                ).one()
                assert preserved.decision_request_id is None
                assert preserved.decision_turn_id is None
                assert preserved.decision_request_fingerprint is None
                assert preserved.decision_payload is None
                assert preserved.decision_payload_digest is None

                request_id = uuid.uuid4().hex
                turn_id = uuid.uuid4().hex
                connection.execute(
                    migrated.insert().values(
                        id=correlated_id,
                        kind="insight",
                        tree={
                            "id": "new",
                            "type": "llm_step",
                            "label": "new decision",
                            "children": [],
                        },
                        summary="new decision",
                        decision_request_id=request_id,
                        decision_turn_id=turn_id,
                        decision_request_fingerprint="f" * 64,
                        decision_payload={
                            "schema": "healthmes.decision-private.v1"
                        },
                        decision_payload_digest="d" * 64,
                    )
                )

            with pytest.raises(sa.exc.IntegrityError):
                with engine.begin() as connection:
                    connection.execute(
                        migrated.insert().values(
                            id=uuid.uuid4().hex,
                            kind="insight",
                            tree={
                                "id": "partial",
                                "type": "llm_step",
                                "label": "invalid partial correlation",
                                "children": [],
                            },
                            summary="invalid partial correlation",
                        decision_request_id=uuid.uuid4().hex,
                    )
                )
        finally:
            engine.dispose()

        with pytest.raises(
            RuntimeError,
            match="without losing Decision Agent audit and idempotency data",
        ):
            command.downgrade(config, "e3f4a5b6c7d8")

        engine = sa.create_engine(database_url)
        try:
            preserved = sa.Table(
                "decision_record",
                sa.MetaData(),
                autoload_with=engine,
            )
            with engine.begin() as connection:
                assert connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                ) == "f4a5b6c7d8e9"
                assert connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(preserved)
                    .where(preserved.c.id == correlated_id)
                ) == 1
                connection.execute(
                    preserved.delete().where(
                        preserved.c.id == correlated_id
                    )
                )
        finally:
            engine.dispose()

        command.downgrade(config, "e3f4a5b6c7d8")

        engine = sa.create_engine(database_url)
        try:
            columns = {
                item["name"]
                for item in sa.inspect(engine).get_columns(
                    "decision_record"
                )
            }
            assert "decision_request_id" not in columns
            legacy = sa.Table(
                "decision_record",
                sa.MetaData(),
                autoload_with=engine,
            )
            with engine.connect() as connection:
                assert connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(legacy)
                    .where(legacy.c.id == legacy_id)
                ) == 1
        finally:
            engine.dispose()

    def test_downgrade_base_drops_all_tables(self, tmp_path):
        database_url = f"sqlite:///{tmp_path / 'down.db'}"
        config = _config(database_url)
        command.upgrade(config, "head")
        command.downgrade(config, "base")

        engine = sa.create_engine(database_url)
        try:
            tables = set(sa.inspect(engine).get_table_names())
            assert tables & EXPECTED_TABLES == set()
        finally:
            engine.dispose()

    def test_decision_policy_downgrade_preserves_disabled_consent(
        self,
        tmp_path,
    ):
        database_url = f"sqlite:///{tmp_path / 'decision-policy.db'}"
        config = _config(database_url)
        command.upgrade(config, "head")

        engine = sa.create_engine(database_url)
        table = sa.Table(
            "decision_domain_policy",
            sa.MetaData(),
            autoload_with=engine,
        )
        policy_id = uuid.uuid4().hex
        try:
            with engine.begin() as connection:
                connection.execute(
                    table.insert().values(
                        id=policy_id,
                        owner_principal_id="owner",
                        domain="calendar",
                        enabled=False,
                    )
                )
        finally:
            engine.dispose()

        with pytest.raises(
            RuntimeError,
            match="without losing disabled Decision Agent domain consent",
        ):
            command.downgrade(config, "f4a5b6c7d8e9")

        engine = sa.create_engine(database_url)
        try:
            with engine.begin() as connection:
                assert connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                ) == "a5b6c7d8e9f0"
                assert connection.scalar(
                    sa.select(table.c.enabled).where(
                        table.c.id == policy_id
                    )
                ) is False
                connection.execute(
                    table.update()
                    .where(table.c.id == policy_id)
                    .values(enabled=True)
                )
        finally:
            engine.dispose()

        command.downgrade(config, "f4a5b6c7d8e9")
        command.upgrade(config, "head")
        engine = sa.create_engine(database_url)
        try:
            inspector = sa.inspect(engine)
            assert "decision_domain_policy" in inspector.get_table_names()
            with engine.connect() as connection:
                assert connection.scalar(
                    sa.select(sa.func.count()).select_from(
                        sa.Table(
                            "decision_domain_policy",
                            sa.MetaData(),
                            autoload_with=connection,
                        )
                    )
                ) == 0
        finally:
            engine.dispose()

    def test_upgrade_is_idempotent_at_head(self, tmp_path):
        database_url = f"sqlite:///{tmp_path / 'twice.db'}"
        config = _config(database_url)
        command.upgrade(config, "head")
        command.upgrade(config, "head")  # no-op, must not raise

    def test_app_usage_generation_migration_preserves_rows_and_refuses_loss(
        self,
        tmp_path,
    ):
        database_url = f"sqlite:///{tmp_path / 'app-usage-generation.db'}"
        config = _config(database_url)
        command.upgrade(config, "c1d2e3f4a5b6")

        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        legacy = sa.Table(
            "app_usage_sample",
            metadata,
            autoload_with=engine,
        )
        bucket = datetime(2026, 8, 9, 12, tzinfo=UTC)
        with engine.begin() as connection:
            connection.execute(
                legacy.insert().values(
                    id=uuid.uuid4().hex,
                    device_id="android-migration",
                    bucket_start=bucket,
                    app_package="com.example.editor",
                    foreground_seconds=900,
                    launches=2,
                )
            )
        engine.dispose()

        command.upgrade(config, "head")

        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        migrated = sa.Table(
            "app_usage_sample",
            metadata,
            autoload_with=engine,
        )
        assert "collection_generation" in migrated.c
        assert "bucket_complete" in migrated.c
        assert "snapshot_sequence" in migrated.c
        with engine.begin() as connection:
            assert connection.scalar(
                sa.select(migrated.c.collection_generation)
            ) == 0
            assert connection.scalar(
                sa.select(migrated.c.bucket_complete)
            ) is False
            assert connection.scalar(
                sa.select(migrated.c.snapshot_sequence)
            ) == 0
            connection.execute(
                migrated.insert().values(
                    id=uuid.uuid4().hex,
                    device_id="android-migration",
                    collection_generation=1,
                    bucket_start=bucket,
                    app_package="com.example.editor",
                    foreground_seconds=300,
                    launches=1,
                )
            )
        engine.dispose()

        # e3 -> d2 removes only snapshot fields, so cross-generation rows are
        # still lossless at this boundary.
        command.downgrade(config, "d2e3f4a5b6c7")

        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        generation_rows = sa.Table(
            "app_usage_sample",
            metadata,
            autoload_with=engine,
        )
        assert "collection_generation" in generation_rows.c
        assert "bucket_complete" not in generation_rows.c
        assert "snapshot_sequence" not in generation_rows.c
        with engine.begin() as connection:
            assert connection.scalar(
                sa.text("SELECT version_num FROM alembic_version")
            ) == "d2e3f4a5b6c7"
            assert connection.scalar(
                sa.select(sa.func.count()).select_from(generation_rows)
            ) == 2
        engine.dispose()

        # d2 -> c1 is the boundary that would collapse generations.
        with pytest.raises(RuntimeError, match="without losing collection generations"):
            command.downgrade(config, "c1d2e3f4a5b6")

        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        preserved = sa.Table(
            "app_usage_sample",
            metadata,
            autoload_with=engine,
        )
        assert "collection_generation" in preserved.c
        assert "bucket_complete" not in preserved.c
        assert "snapshot_sequence" not in preserved.c
        with engine.begin() as connection:
            assert connection.scalar(
                sa.text("SELECT version_num FROM alembic_version")
            ) == "d2e3f4a5b6c7"
            assert connection.scalar(
                sa.select(sa.func.count()).select_from(preserved)
            ) == 2
            connection.execute(
                preserved.delete().where(
                    preserved.c.collection_generation == 1
                )
            )
        engine.dispose()

        command.upgrade(config, "head")

        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        snapshot_rows = sa.Table(
            "app_usage_sample",
            metadata,
            autoload_with=engine,
        )
        with engine.begin() as connection:
            connection.execute(
                snapshot_rows.update().values(
                    bucket_complete=True,
                    snapshot_sequence=42,
                )
            )
        engine.dispose()

        with pytest.raises(
            RuntimeError,
            match="without losing Android snapshot state",
        ):
            command.downgrade(config, "d2e3f4a5b6c7")

        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        snapshot_preserved = sa.Table(
            "app_usage_sample",
            metadata,
            autoload_with=engine,
        )
        assert "bucket_complete" in snapshot_preserved.c
        assert "snapshot_sequence" in snapshot_preserved.c
        with engine.begin() as connection:
            assert connection.scalar(
                sa.text("SELECT version_num FROM alembic_version")
            ) == "e3f4a5b6c7d8"
            assert connection.execute(
                sa.select(
                    snapshot_preserved.c.bucket_complete,
                    snapshot_preserved.c.snapshot_sequence,
                )
            ).one() == (True, 42)
            connection.execute(
                snapshot_preserved.update().values(
                    bucket_complete=False,
                    snapshot_sequence=0,
                )
            )
            wellness_event = sa.Table(
                "wellness_event",
                sa.MetaData(),
                autoload_with=connection,
            )
            now = datetime(2026, 8, 10, tzinfo=UTC)
            connection.execute(
                wellness_event.insert().values(
                    id=uuid.uuid4().hex,
                    event_type="activity.android-bucket-snapshot.v1",
                    schema_version=1,
                    observed_at=now,
                    recorded_at=now,
                    timezone="UTC",
                    source_provider="android-usage",
                    source_device="android-empty-manifest",
                    source_record_id="snapshot:empty-manifest",
                    capture_method="derived",
                    sensitivity="activity-control",
                    consent_scope="personal",
                    payload={
                        "bucket_complete": True,
                        "snapshot_sequence": 1,
                        "app_count": 0,
                    },
                )
            )
        engine.dispose()

        with pytest.raises(
            RuntimeError,
            match="without losing Android snapshot state",
        ):
            command.downgrade(config, "d2e3f4a5b6c7")

        engine = sa.create_engine(database_url)
        with engine.begin() as connection:
            assert connection.scalar(
                sa.text("SELECT version_num FROM alembic_version")
            ) == "e3f4a5b6c7d8"
            connection.execute(
                sa.text(
                    "DELETE FROM wellness_event "
                    "WHERE event_type = 'activity.android-bucket-snapshot.v1'"
                )
            )
        engine.dispose()

        command.downgrade(config, "c1d2e3f4a5b6")

        engine = sa.create_engine(database_url)
        try:
            inspector = sa.inspect(engine)
            assert "collection_generation" not in {
                column["name"]
                for column in inspector.get_columns("app_usage_sample")
            }
            with engine.connect() as connection:
                assert connection.scalar(
                    sa.text("SELECT COUNT(*) FROM app_usage_sample")
                ) == 1
        finally:
            engine.dispose()

    def test_legacy_pending_schedule_proposal_is_backfilled_and_resolvable(
        self,
        tmp_path,
    ):
        database_url = f"sqlite:///{tmp_path / 'legacy-proposal.db'}"
        config = _config(database_url)
        command.upgrade(config, "b4c5d6e7f809")
        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        task_table = sa.Table("task", metadata, autoload_with=engine)
        proposal_table = sa.Table(
            "schedule_proposal",
            metadata,
            autoload_with=engine,
        )
        task_id = "1" * 32
        proposal_id = "2" * 32
        expired_task_id = "3" * 32
        expired_proposal_id = "4" * 32
        now = datetime.now(UTC)
        pending_start = now + timedelta(days=2)
        expired_start = now - timedelta(days=2)
        with engine.begin() as connection:
            connection.execute(
                task_table.insert(),
                [
                    {
                        "id": task_id,
                        "title": "Legacy pending task",
                        "energy_demand": "med",
                        "status": "todo",
                        "source": "user",
                    },
                    {
                        "id": expired_task_id,
                        "title": "Expired legacy task",
                        "energy_demand": "med",
                        "status": "todo",
                        "source": "user",
                    },
                ],
            )
            connection.execute(
                proposal_table.insert(),
                [
                    {
                        "id": proposal_id,
                        "task_id": task_id,
                        "proposed_start": pending_start,
                        "proposed_end": pending_start + timedelta(hours=1),
                        "status": "proposed",
                    },
                    {
                        "id": expired_proposal_id,
                        "task_id": expired_task_id,
                        "proposed_start": expired_start,
                        "proposed_end": expired_start + timedelta(hours=1),
                        "status": "proposed",
                    },
                ],
            )
        engine.dispose()

        command.upgrade(config, "head")

        engine = sa.create_engine(database_url)
        try:
            factory = sessionmaker(bind=engine)
            with factory() as session:
                proposal = session.get(
                    ScheduleProposal,
                    uuid.UUID("22222222-2222-2222-2222-222222222222"),
                )
                assert proposal is not None
                assert proposal.reply_handle_digest
                assert proposal.expires_at is not None
                secret = "legacy-proposal-test-secret-at-least-32-characters"
                accept_token = resolution_token(
                    proposal,
                    secret,
                    ProposalStatus.ACCEPTED,
                )
                decline_token = resolution_token(
                    proposal,
                    secret,
                    ProposalStatus.DECLINED,
                )
                assert accept_token
                assert decline_token
                assert accept_token != decline_token
                assert verify_resolution_token(
                    accept_token,
                    proposal,
                    secret,
                    ProposalStatus.ACCEPTED,
                )
                assert not verify_resolution_token(
                    accept_token,
                    proposal,
                    secret,
                    ProposalStatus.DECLINED,
                )
                expired = session.get(
                    ScheduleProposal,
                    uuid.UUID("44444444-4444-4444-4444-444444444444"),
                )
                assert expired is not None
                assert expired.status is ProposalStatus.INVALIDATED
                assert expired.reply_handle_digest is None
                assert expired.expires_at is None
        finally:
            engine.dispose()

    def test_old_feature_revision_stamp_upgrades_without_duplicate_columns(
        self,
        tmp_path,
    ):
        database_url = f"sqlite:///{tmp_path / 'old-feature-stamp.db'}"
        config = _config(database_url)
        command.upgrade(config, "e1f2a3b4c5d6")

        engine = sa.create_engine(database_url)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE calendar_event_mirror "
                "ADD COLUMN intake_task_id CHAR(32)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX ix_calendar_event_mirror_intake_task_id "
                "ON calendar_event_mirror (intake_task_id)"
            )
            connection.exec_driver_sql(
                "ALTER TABLE schedule_proposal "
                "ADD COLUMN reply_handle_digest VARCHAR(255)"
            )
            connection.exec_driver_sql(
                "ALTER TABLE schedule_proposal ADD COLUMN expires_at DATETIME"
            )
            connection.exec_driver_sql(
                "CREATE INDEX ix_schedule_proposal_expires_at "
                "ON schedule_proposal (expires_at)"
            )
            connection.exec_driver_sql(
                "UPDATE alembic_version SET version_num = 'a3b4c5d6e7f8'"
            )
        engine.dispose()

        command.upgrade(config, "head")

        engine = sa.create_engine(database_url)
        try:
            inspector = sa.inspect(engine)
            assert inspector.get_table_names()
            assert inspector.has_table("sleep_reconciliation_proposal")
            mirror_columns = {
                item["name"]
                for item in inspector.get_columns("calendar_event_mirror")
            }
            proposal_columns = {
                item["name"]
                for item in inspector.get_columns("schedule_proposal")
            }
            mirror_indexes = {
                item["name"]
                for item in inspector.get_indexes("calendar_event_mirror")
            }
            assert {"intake_task_id", "intake_opted_out"} <= mirror_columns
            assert {
                "intake_calendar_source",
                "intake_external_id",
                "intake_revision",
                "reply_handle_digest",
                "expires_at",
            } <= proposal_columns
            assert "ux_calendar_event_mirror_calendar_identity" in mirror_indexes
            assert "ix_calendar_event_mirror_actual_sleep_cleanup" in mirror_indexes
            with engine.connect() as connection:
                assert connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                ) == "d8e9f0a1b2c3"
        finally:
            engine.dispose()

    def test_sleep_hardening_migration_cleans_untrusted_identity(self, tmp_path):
        database_url = f"sqlite:///{tmp_path / 'sleep-hardening.db'}"
        config = _config(database_url)
        command.upgrade(config, "d0e1f2a3b4c5")
        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        mirror = sa.Table(
            "calendar_event_mirror",
            metadata,
            autoload_with=engine,
        )
        start = datetime(2026, 7, 25, 23, tzinfo=UTC)
        end = datetime(2026, 7, 26, 7, tzinfo=UTC)
        with engine.begin() as connection:
            connection.execute(
                mirror.insert(),
                [
                    {
                        "id": "a" * 32,
                        "external_id": "forged",
                        "calendar_source": "google",
                        "summary": "Forged",
                        "start_at": start,
                        "end_at": end,
                        "is_agent_created": False,
                        "healthmes_kind": "actual_sleep",
                        "healthmes_source": "oura",
                        "healthmes_source_key": "oura:2026-07-26",
                        "observation_fingerprint": "forged",
                        "sleep_local_date": start.date(),
                        "sleep_duration_minutes": 420,
                        "sleep_time_in_bed_minutes": 480,
                    },
                    {
                        "id": "b" * 32,
                        "external_id": "owned",
                        "calendar_source": "google",
                        "summary": "Owned",
                        "start_at": start,
                        "end_at": end,
                        "is_agent_created": True,
                        "healthmes_kind": "actual_sleep",
                        "healthmes_source": "oura",
                        "healthmes_source_key": "oura:2026-07-27",
                        "observation_fingerprint": None,
                        "sleep_local_date": end.date(),
                        "sleep_duration_minutes": 420,
                        "sleep_time_in_bed_minutes": 480,
                    },
                ],
            )
        engine.dispose()

        command.upgrade(config, "head")

        engine = sa.create_engine(database_url)
        try:
            with engine.connect() as connection:
                rows = {
                    row.external_id: row
                    for row in connection.execute(
                        sa.text(
                            "SELECT external_id, is_agent_created, "
                            "healthmes_kind, healthmes_source, "
                            "healthmes_source_key, observation_fingerprint, "
                            "sleep_local_date, sleep_provider, "
                            "sleep_duration_minutes, "
                            "sleep_time_in_bed_minutes "
                            "FROM calendar_event_mirror"
                        )
                    )
                }
            forged = rows["forged"]
            assert forged.healthmes_kind is None
            assert forged.healthmes_source is None
            assert forged.healthmes_source_key is None
            assert forged.observation_fingerprint is None
            assert forged.sleep_local_date is None
            assert forged.sleep_provider is None
            assert forged.sleep_duration_minutes is None
            assert forged.sleep_time_in_bed_minutes is None
            assert rows["owned"].sleep_provider == "oura"
        finally:
            engine.dispose()

    def test_legacy_cleanup_migration_preserves_identity_and_adds_index(
        self,
        tmp_path,
    ):
        database_url = f"sqlite:///{tmp_path / 'legacy-cleanup.db'}"
        config = _config(database_url)
        command.upgrade(config, "e1f2a3b4c5d6")
        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        mirror = sa.Table(
            "calendar_event_mirror",
            metadata,
            autoload_with=engine,
        )
        source_key = "actual_sleep:2024-01-02"
        with engine.begin() as connection:
            connection.execute(
                mirror.insert(),
                {
                    "id": "c" * 32,
                    "external_id": "legacy-provider-specific",
                    "calendar_source": "google",
                    "summary": "Legacy sleep",
                    "start_at": datetime(2024, 1, 1, 23, tzinfo=UTC),
                    "end_at": datetime(2024, 1, 2, 7, tzinfo=UTC),
                    "is_agent_created": True,
                    "healthmes_kind": "actual_sleep",
                    "healthmes_source": "oura",
                    "healthmes_source_key": source_key,
                    "sleep_local_date": datetime(2024, 1, 2, tzinfo=UTC).date(),
                    "sleep_provider": "oura",
                    "sleep_duration_minutes": 420,
                    "sleep_time_in_bed_minutes": 480,
                },
            )
        engine.dispose()

        command.upgrade(config, "head")

        engine = sa.create_engine(database_url)
        try:
            with engine.connect() as connection:
                source = connection.scalar(
                    sa.text(
                        "SELECT healthmes_source FROM calendar_event_mirror "
                        "WHERE external_id = 'legacy-provider-specific'"
                    )
                )
            indexes = {
                item["name"]
                for item in sa.inspect(engine).get_indexes(
                    "calendar_event_mirror"
                )
            }
            assert source == "oura"
            assert "ix_calendar_event_mirror_actual_sleep_cleanup" in indexes
            assert "ux_calendar_event_mirror_calendar_identity" in indexes
            assert (
                "ux_calendar_event_mirror_source_healthmes_source_key"
                not in indexes
            )
        finally:
            engine.dispose()

    def test_legacy_cleanup_downgrade_quarantines_source_key_conflicts(
        self,
        tmp_path,
    ):
        database_url = f"sqlite:///{tmp_path / 'legacy-cleanup-downgrade.db'}"
        config = _config(database_url)
        command.upgrade(config, "head")
        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        mirror = sa.Table(
            "calendar_event_mirror",
            metadata,
            autoload_with=engine,
        )
        source_key = "actual_sleep:2024-01-02"
        common = {
            "calendar_source": "google",
            "summary": "Sleep",
            "start_at": datetime(2024, 1, 1, 23, tzinfo=UTC),
            "end_at": datetime(2024, 1, 2, 7, tzinfo=UTC),
            "is_agent_created": True,
            "healthmes_kind": "actual_sleep",
            "healthmes_source_key": source_key,
            "sleep_local_date": datetime(2024, 1, 2, tzinfo=UTC).date(),
            "sleep_duration_minutes": 420,
        }
        with engine.begin() as connection:
            connection.execute(
                mirror.insert(),
                [
                    {
                        **common,
                        "id": "c" * 32,
                        "external_id": "canonical",
                        "healthmes_source": "open-wearables",
                        "sleep_provider": "oura",
                    },
                    {
                        **common,
                        "id": "d" * 32,
                        "external_id": "legacy",
                        "healthmes_source": "oura",
                        "sleep_provider": "oura",
                    },
                ],
            )
        engine.dispose()

        command.downgrade(config, "e1f2a3b4c5d6")

        engine = sa.create_engine(database_url)
        try:
            with engine.connect() as connection:
                rows = {
                    row.external_id: row
                    for row in connection.execute(
                        sa.text(
                            "SELECT external_id, is_agent_created, "
                            "healthmes_kind, healthmes_source, "
                            "healthmes_source_key, sleep_local_date "
                            "FROM calendar_event_mirror"
                        )
                    )
                }
            indexes = {
                item["name"]
                for item in sa.inspect(engine).get_indexes(
                    "calendar_event_mirror"
                )
            }
            assert rows["canonical"].healthmes_source_key == source_key
            assert rows["legacy"].is_agent_created == 0
            assert rows["legacy"].healthmes_kind is None
            assert rows["legacy"].healthmes_source is None
            assert rows["legacy"].healthmes_source_key is None
            assert rows["legacy"].sleep_local_date is None
            assert "ux_calendar_event_mirror_source_healthmes_source_key" in indexes
        finally:
            engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason=(
        "requires a disposable PostgreSQL URL in "
        "HEALTHMES_TEST_POSTGRES_URL"
    ),
)
def test_postgres_wearable_retention_migration_round_trip() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = sa.create_engine(database_url)
    schema = f"hm_wearable_retention_{uuid.uuid4().hex}"
    quoted_schema = admin_engine.dialect.identifier_preparer.quote(
        schema
    )
    separator = "&" if "?" in database_url else "?"
    schema_url = (
        f"{database_url}{separator}options=-csearch_path={schema}"
    )
    config = _config(schema_url)
    generic_policy_id = uuid.uuid4()
    wearable_event_id = uuid.uuid4()
    observed_at = datetime(2026, 8, 1, 9, tzinfo=UTC)
    original_expiry = observed_at + timedelta(days=3)
    try:
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(f"CREATE SCHEMA {quoted_schema}")
            )

        command.upgrade(config, "c7d8e9f0a1b2")
        scoped_engine = sa.create_engine(schema_url)
        try:
            policy = sa.Table(
                "retention_policy",
                sa.MetaData(),
                autoload_with=scoped_engine,
            )
            event = sa.Table(
                "wellness_event",
                sa.MetaData(),
                autoload_with=scoped_engine,
            )
            with scoped_engine.begin() as connection:
                connection.execute(
                    policy.insert().values(
                        id=generic_policy_id,
                        data_class="normalized",
                        retention_days=14,
                        enabled=True,
                    )
                )
                connection.execute(
                    event.insert().values(
                        id=wearable_event_id,
                        event_type="wearable.sleep.v1",
                        schema_version=1,
                        observed_at=observed_at,
                        recorded_at=observed_at,
                        timezone="UTC",
                        source_provider=(
                            "healthmes-open-wearables-mirror"
                        ),
                        source_device=None,
                        source_record_id="wearable:postgres:1",
                        capture_method="import",
                        sensitivity="wellness",
                        consent_scope="personal",
                        retention_policy_id=generic_policy_id,
                        expires_at=original_expiry,
                        payload={},
                    )
                )
        finally:
            scoped_engine.dispose()

        command.upgrade(config, "head")
        scoped_engine = sa.create_engine(schema_url)
        try:
            policy = sa.Table(
                "retention_policy",
                sa.MetaData(),
                autoload_with=scoped_engine,
            )
            event = sa.Table(
                "wellness_event",
                sa.MetaData(),
                autoload_with=scoped_engine,
            )
            with scoped_engine.connect() as connection:
                wearable_policy = connection.execute(
                    sa.select(policy).where(
                        policy.c.data_class == "wearable_normalized"
                    )
                ).one()
                migrated = connection.execute(
                    sa.select(event).where(
                        event.c.id == wearable_event_id
                    )
                ).one()
                assert wearable_policy.retention_days == 14
                assert wearable_policy.enabled is True
                assert (
                    migrated.retention_policy_id
                    == wearable_policy.id
                )
                assert migrated.expires_at == original_expiry
                assert connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                ) == "d8e9f0a1b2c3"
        finally:
            scoped_engine.dispose()

        command.downgrade(config, "c7d8e9f0a1b2")
        scoped_engine = sa.create_engine(schema_url)
        try:
            policy = sa.Table(
                "retention_policy",
                sa.MetaData(),
                autoload_with=scoped_engine,
            )
            event = sa.Table(
                "wellness_event",
                sa.MetaData(),
                autoload_with=scoped_engine,
            )
            with scoped_engine.connect() as connection:
                restored = connection.execute(
                    sa.select(event).where(
                        event.c.id == wearable_event_id
                    )
                ).one()
                assert (
                    restored.retention_policy_id
                    == generic_policy_id
                )
                assert restored.expires_at == original_expiry
                assert connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(policy)
                    .where(
                        policy.c.data_class
                        == "wearable_normalized"
                    )
                ) == 0
                assert connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                ) == "c7d8e9f0a1b2"
        finally:
            scoped_engine.dispose()
    finally:
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(
                    f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE"
                )
            )
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason=(
        "requires a disposable PostgreSQL URL in "
        "HEALTHMES_TEST_POSTGRES_URL"
    ),
)
def test_postgres_calendar_generation_migration_is_lossless() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = sa.create_engine(database_url)
    schema = f"hm_calendar_generation_{uuid.uuid4().hex}"
    quoted_schema = admin_engine.dialect.identifier_preparer.quote(
        schema
    )
    separator = "&" if "?" in database_url else "?"
    schema_url = (
        f"{database_url}{separator}options=-csearch_path={schema}"
    )
    config = _config(schema_url)
    mirror_id = uuid.uuid4()
    start_at = datetime(2026, 8, 12, 9, tzinfo=UTC)
    end_at = start_at + timedelta(hours=1)
    try:
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(f"CREATE SCHEMA {quoted_schema}")
            )

        command.upgrade(config, "a5b6c7d8e9f0")
        scoped_engine = sa.create_engine(schema_url)
        try:
            legacy_mirror = sa.Table(
                "calendar_event_mirror",
                sa.MetaData(),
                autoload_with=scoped_engine,
            )
            legacy_proposal = sa.Table(
                "calendar_mutation_proposal",
                sa.MetaData(),
                autoload_with=scoped_engine,
            )
            base_proposal = {
                "calendar_source": "google",
                "mirror_event_id": mirror_id,
                "external_event_id": "legacy-event",
                "operation": "shorten",
                "original_start_at": start_at,
                "original_end_at": end_at,
                "proposed_start_at": start_at,
                "proposed_end_at": end_at - timedelta(minutes=15),
                "expected_etag": '"legacy-etag"',
                "protected_fingerprint": "legacy-fingerprint",
                "reply_handle_digest": "legacy-reply",
                "expires_at": end_at,
            }
            with scoped_engine.begin() as connection:
                connection.execute(
                    legacy_mirror.insert().values(
                        id=mirror_id,
                        external_id="legacy-event",
                        calendar_source="google",
                        summary="Legacy event",
                        start_at=start_at,
                        end_at=end_at,
                        is_agent_created=False,
                    )
                )
                connection.execute(
                    legacy_proposal.insert(),
                    [
                        {
                            **base_proposal,
                            "id": uuid.uuid4(),
                            "status": "pending",
                            "dedup_key": "legacy-pending",
                        },
                        {
                            **base_proposal,
                            "id": uuid.uuid4(),
                            "status": "applying",
                            "dedup_key": "legacy-applying",
                        },
                    ],
                )
        finally:
            scoped_engine.dispose()

        command.upgrade(config, "head")
        scoped_engine = sa.create_engine(schema_url)
        active_id = uuid.uuid4()
        raced_id = uuid.uuid4()
        reconnected_id = uuid.uuid4()
        try:
            inspector = sa.inspect(scoped_engine)
            mirror_columns = {
                item["name"]: item
                for item in inspector.get_columns("calendar_event_mirror")
            }
            checks = {
                item["name"]
                for item in inspector.get_check_constraints(
                    "calendar_mutation_proposal"
                )
            }
            assert mirror_columns["connection_generation"]["nullable"] is False
            assert "ck_calendar_mutation_proposal_active_generation" in checks

            mirror = sa.Table(
                "calendar_event_mirror",
                sa.MetaData(),
                autoload_with=scoped_engine,
            )
            proposal = sa.Table(
                "calendar_mutation_proposal",
                sa.MetaData(),
                autoload_with=scoped_engine,
            )
            with scoped_engine.begin() as connection:
                statuses = {
                    row.dedup_key: row.status
                    for row in connection.execute(
                        sa.select(
                            proposal.c.dedup_key,
                            proposal.c.status,
                        )
                    )
                }
                assert statuses == {
                    "legacy-pending": "conflicted",
                    "legacy-applying": "unknown",
                }
                assert connection.scalar(
                    sa.select(mirror.c.connection_generation)
                    .where(mirror.c.id == mirror_id)
                ) == "__legacy_unbound__"
                connection.execute(
                    mirror.insert().values(
                        id=reconnected_id,
                        external_id="legacy-event",
                        calendar_source="google",
                        connection_generation="reconnected-account",
                        start_at=start_at,
                        end_at=end_at,
                        is_agent_created=False,
                    )
                )
                connection.execute(
                    proposal.insert().values(
                        id=active_id,
                        calendar_source="google",
                        account_generation="connected-account",
                        external_event_id="active-event",
                        operation="shorten",
                        original_start_at=start_at,
                        original_end_at=end_at,
                        proposed_start_at=start_at,
                        proposed_end_at=end_at - timedelta(minutes=15),
                        expected_etag='"active-etag"',
                        protected_fingerprint="active-fingerprint",
                        reply_handle_digest="active-reply",
                        expires_at=end_at,
                        status="pending",
                        dedup_key="active-proposal",
                    )
                )

            with pytest.raises(sa.exc.IntegrityError):
                with scoped_engine.begin() as connection:
                    connection.execute(
                        mirror.insert().values(
                            id=uuid.uuid4(),
                            external_id="null-generation",
                            calendar_source="google",
                            connection_generation=None,
                            start_at=start_at,
                            end_at=end_at,
                            is_agent_created=False,
                        )
                    )

            with pytest.raises(
                RuntimeError,
                match=(
                    "multiple account generations share one "
                    "provider event id"
                ),
            ):
                command.downgrade(config, "a5b6c7d8e9f0")

            with scoped_engine.begin() as connection:
                assert connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                ) == "d8e9f0a1b2c3"
                assert connection.scalar(
                    sa.select(proposal.c.status).where(
                        proposal.c.id == active_id
                    )
                ) == "pending"
                assert connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(mirror)
                    .where(mirror.c.id == reconnected_id)
                ) == 1
                connection.execute(
                    mirror.delete().where(mirror.c.id == reconnected_id)
                )

            with pytest.raises(
                RuntimeError,
                match="calendar mutation proposals are still active",
            ):
                command.downgrade(config, "a5b6c7d8e9f0")

            with scoped_engine.begin() as connection:
                assert connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                ) == "d8e9f0a1b2c3"
                connection.execute(
                    proposal.update()
                    .where(proposal.c.id == active_id)
                    .values(status="conflicted")
                )

            with scoped_engine.connect() as writer:
                transaction = writer.begin()
                writer.execute(
                    proposal.insert().values(
                        id=raced_id,
                        calendar_source="google",
                        account_generation="connected-account",
                        external_event_id="racing-event",
                        operation="shorten",
                        original_start_at=start_at,
                        original_end_at=end_at,
                        proposed_start_at=start_at,
                        proposed_end_at=end_at - timedelta(minutes=15),
                        expected_etag='"racing-etag"',
                        protected_fingerprint="racing-fingerprint",
                        reply_handle_digest="racing-reply",
                        expires_at=end_at,
                        status="pending",
                        dedup_key="racing-proposal",
                    )
                )
                with ThreadPoolExecutor(max_workers=1) as pool:
                    downgrade = pool.submit(
                        command.downgrade,
                        _config(schema_url),
                        "a5b6c7d8e9f0",
                    )
                    time.sleep(0.2)
                    assert downgrade.done() is False
                    transaction.commit()
                    with pytest.raises(
                        RuntimeError,
                        match="calendar mutation proposals are still active",
                    ):
                        downgrade.result(timeout=5)

            with scoped_engine.begin() as connection:
                assert connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                ) == "d8e9f0a1b2c3"
                assert connection.scalar(
                    sa.select(proposal.c.status).where(
                        proposal.c.id == raced_id
                    )
                ) == "pending"
                connection.execute(
                    proposal.update()
                    .where(proposal.c.id == raced_id)
                    .values(status="conflicted")
                )
        finally:
            scoped_engine.dispose()

        command.downgrade(config, "a5b6c7d8e9f0")
        scoped_engine = sa.create_engine(schema_url)
        try:
            inspector = sa.inspect(scoped_engine)
            assert "account_generation" not in {
                item["name"]
                for item in inspector.get_columns(
                    "calendar_mutation_proposal"
                )
            }
            assert "connection_generation" not in {
                item["name"]
                for item in inspector.get_columns("calendar_event_mirror")
            }
            with scoped_engine.connect() as connection:
                assert connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                ) == "a5b6c7d8e9f0"
                assert connection.scalar(
                    sa.text(
                        "SELECT COUNT(*) FROM calendar_event_mirror "
                        "WHERE id = :id"
                    ),
                    {"id": mirror_id},
                ) == 1
        finally:
            scoped_engine.dispose()
    finally:
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(
                    f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE"
                )
            )
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason=(
        "requires a disposable PostgreSQL URL in "
        "HEALTHMES_TEST_POSTGRES_URL"
    ),
)
def test_postgres_decision_agent_migration_round_trip() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = sa.create_engine(database_url)
    schema = f"hm_alembic_{uuid.uuid4().hex}"
    quoted_schema = admin_engine.dialect.identifier_preparer.quote(
        schema
    )
    separator = "&" if "?" in database_url else "?"
    schema_url = (
        f"{database_url}{separator}options=-csearch_path={schema}"
    )
    config = _config(schema_url)
    try:
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(f"CREATE SCHEMA {quoted_schema}")
            )

        command.upgrade(config, "e3f4a5b6c7d8")
        scoped_engine = sa.create_engine(schema_url)
        legacy_id = uuid.uuid4()
        try:
            legacy = sa.Table(
                "decision_record",
                sa.MetaData(),
                autoload_with=scoped_engine,
            )
            with scoped_engine.begin() as connection:
                connection.execute(
                    legacy.insert().values(
                        id=legacy_id,
                        kind="insight",
                        tree={
                            "id": "legacy",
                            "type": "llm_step",
                            "label": "legacy decision",
                            "children": [],
                        },
                        summary="legacy decision",
                    )
                )
        finally:
            scoped_engine.dispose()

        command.upgrade(config, "head")
        correlated_id = uuid.uuid4()
        scoped_engine = sa.create_engine(schema_url)
        try:
            inspector = sa.inspect(scoped_engine)
            columns = {
                item["name"]
                for item in inspector.get_columns("decision_record")
            }
            checks = {
                item["name"]
                for item in inspector.get_check_constraints(
                    "decision_record"
                )
            }
            indexes = {
                item["name"]: item
                for item in inspector.get_indexes("decision_record")
            }
            assert {
                "decision_request_id",
                "decision_turn_id",
                "decision_request_fingerprint",
                "decision_payload",
                "decision_payload_digest",
            } <= columns
            assert (
                "ck_decision_record_decision_agent_correlation_complete"
                in checks
            )
            assert indexes[
                "ux_decision_record_decision_request_id"
            ]["unique"]
            assert indexes[
                "ux_decision_record_decision_turn_id"
            ]["unique"]

            migrated = sa.Table(
                "decision_record",
                sa.MetaData(),
                autoload_with=scoped_engine,
            )
            with scoped_engine.begin() as connection:
                preserved = connection.execute(
                    sa.select(migrated).where(
                        migrated.c.id == legacy_id
                    )
                ).one()
                assert preserved.decision_request_id is None
                assert preserved.decision_payload_digest is None
                connection.execute(
                    migrated.insert().values(
                        id=correlated_id,
                        kind="insight",
                        tree={
                            "id": "new",
                            "type": "llm_step",
                            "label": "new decision",
                            "children": [],
                        },
                        summary="new decision",
                        decision_request_id=uuid.uuid4(),
                        decision_turn_id=uuid.uuid4(),
                        decision_request_fingerprint="f" * 64,
                        decision_payload={
                            "schema": "healthmes.decision-private.v1"
                        },
                        decision_payload_digest="d" * 64,
                    )
                )

            with pytest.raises(sa.exc.IntegrityError):
                with scoped_engine.begin() as connection:
                    connection.execute(
                        migrated.insert().values(
                            id=uuid.uuid4(),
                            kind="insight",
                            tree={
                                "id": "partial",
                                "type": "llm_step",
                                "label": "invalid partial correlation",
                                "children": [],
                            },
                            summary="invalid partial correlation",
                            decision_request_id=uuid.uuid4(),
                        )
                    )
        finally:
            scoped_engine.dispose()

        with pytest.raises(
            RuntimeError,
            match="without losing Decision Agent audit and idempotency data",
        ):
            command.downgrade(config, "e3f4a5b6c7d8")

        scoped_engine = sa.create_engine(schema_url)
        try:
            preserved = sa.Table(
                "decision_record",
                sa.MetaData(),
                autoload_with=scoped_engine,
            )
            with scoped_engine.begin() as connection:
                assert connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                ) == "d8e9f0a1b2c3"
                assert connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(preserved)
                    .where(preserved.c.id == correlated_id)
                ) == 1
                connection.execute(
                    preserved.delete().where(
                        preserved.c.id == correlated_id
                    )
                )
        finally:
            scoped_engine.dispose()

        raced_id = uuid.uuid4()
        scoped_engine = sa.create_engine(schema_url)
        try:
            migrated = sa.Table(
                "decision_record",
                sa.MetaData(),
                autoload_with=scoped_engine,
            )
            with scoped_engine.connect() as writer:
                transaction = writer.begin()
                writer.execute(
                    migrated.insert().values(
                        id=raced_id,
                        kind="insight",
                        tree={
                            "id": "raced",
                            "type": "llm_step",
                            "label": "concurrent decision",
                            "children": [],
                        },
                        summary="concurrent decision",
                        decision_request_id=uuid.uuid4(),
                        decision_turn_id=uuid.uuid4(),
                        decision_request_fingerprint="a" * 64,
                        decision_payload={
                            "schema": "healthmes.decision-private.v1"
                        },
                        decision_payload_digest="b" * 64,
                    )
                )
                with ThreadPoolExecutor(max_workers=1) as pool:
                    downgrade = pool.submit(
                        command.downgrade,
                        _config(schema_url),
                        "e3f4a5b6c7d8",
                    )
                    time.sleep(0.2)
                    assert downgrade.done() is False
                    transaction.commit()
                    with pytest.raises(
                        RuntimeError,
                        match=(
                            "without losing Decision Agent audit and "
                            "idempotency data"
                        ),
                    ):
                        downgrade.result(timeout=5)

            with scoped_engine.begin() as connection:
                assert connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                ) == "d8e9f0a1b2c3"
                assert connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(migrated)
                    .where(migrated.c.id == raced_id)
                ) == 1
                connection.execute(
                    migrated.delete().where(migrated.c.id == raced_id)
                )
        finally:
            scoped_engine.dispose()

        command.downgrade(config, "e3f4a5b6c7d8")
        scoped_engine = sa.create_engine(schema_url)
        try:
            inspector = sa.inspect(scoped_engine)
            columns = {
                item["name"]
                for item in inspector.get_columns("decision_record")
            }
            assert "decision_request_id" not in columns
            legacy = sa.Table(
                "decision_record",
                sa.MetaData(),
                autoload_with=scoped_engine,
            )
            with scoped_engine.connect() as connection:
                assert connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(legacy)
                    .where(legacy.c.id == legacy_id)
                ) == 1
        finally:
            scoped_engine.dispose()
    finally:
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(
                    f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE"
                )
            )
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason=(
        "requires a disposable PostgreSQL URL in "
        "HEALTHMES_TEST_POSTGRES_URL"
    ),
)
def test_postgres_decision_policy_downgrade_is_lossless() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = sa.create_engine(database_url)
    schema = f"hm_policy_{uuid.uuid4().hex}"
    quoted_schema = admin_engine.dialect.identifier_preparer.quote(
        schema
    )
    separator = "&" if "?" in database_url else "?"
    schema_url = (
        f"{database_url}{separator}options=-csearch_path={schema}"
    )
    config = _config(schema_url)
    try:
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(f"CREATE SCHEMA {quoted_schema}")
            )

        command.upgrade(config, "head")
        scoped_engine = sa.create_engine(schema_url)
        policy_id = uuid.uuid4()
        try:
            policy = sa.Table(
                "decision_domain_policy",
                sa.MetaData(),
                autoload_with=scoped_engine,
            )
            with scoped_engine.begin() as connection:
                connection.execute(
                    policy.insert().values(
                        id=policy_id,
                        owner_principal_id="owner",
                        domain="calendar",
                        enabled=False,
                    )
                )
        finally:
            scoped_engine.dispose()

        with pytest.raises(
            RuntimeError,
            match="without losing disabled Decision Agent domain consent",
        ):
            command.downgrade(config, "f4a5b6c7d8e9")

        scoped_engine = sa.create_engine(schema_url)
        try:
            policy = sa.Table(
                "decision_domain_policy",
                sa.MetaData(),
                autoload_with=scoped_engine,
            )
            with scoped_engine.begin() as connection:
                assert connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                ) == "d8e9f0a1b2c3"
                assert connection.scalar(
                    sa.select(policy.c.enabled).where(
                        policy.c.id == policy_id
                    )
                ) is False
                connection.execute(
                    policy.update()
                    .where(policy.c.id == policy_id)
                    .values(enabled=True)
                )

            with scoped_engine.connect() as writer:
                transaction = writer.begin()
                writer.execute(
                    policy.update()
                    .where(policy.c.id == policy_id)
                    .values(enabled=False)
                )
                with ThreadPoolExecutor(max_workers=1) as pool:
                    downgrade = pool.submit(
                        command.downgrade,
                        _config(schema_url),
                        "f4a5b6c7d8e9",
                    )
                    time.sleep(0.2)
                    assert downgrade.done() is False
                    transaction.commit()
                    with pytest.raises(
                        RuntimeError,
                        match=(
                            "without losing disabled Decision Agent "
                            "domain consent"
                        ),
                    ):
                        downgrade.result(timeout=5)

            with scoped_engine.begin() as connection:
                assert connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                ) == "d8e9f0a1b2c3"
                assert connection.scalar(
                    sa.select(policy.c.enabled).where(
                        policy.c.id == policy_id
                    )
                ) is False
                connection.execute(
                    policy.update()
                    .where(policy.c.id == policy_id)
                    .values(enabled=True)
                )
        finally:
            scoped_engine.dispose()

        command.downgrade(config, "f4a5b6c7d8e9")
        scoped_engine = sa.create_engine(schema_url)
        try:
            assert (
                "decision_domain_policy"
                not in sa.inspect(scoped_engine).get_table_names()
            )
            with scoped_engine.connect() as connection:
                assert connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                ) == "f4a5b6c7d8e9"
        finally:
            scoped_engine.dispose()

        command.upgrade(config, "head")
        scoped_engine = sa.create_engine(schema_url)
        try:
            policy = sa.Table(
                "decision_domain_policy",
                sa.MetaData(),
                autoload_with=scoped_engine,
            )
            with scoped_engine.connect() as connection:
                assert connection.scalar(
                    sa.select(sa.func.count()).select_from(policy)
                ) == 0
        finally:
            scoped_engine.dispose()
    finally:
        with admin_engine.begin() as connection:
            connection.execute(
                sa.text(
                    f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE"
                )
            )
        admin_engine.dispose()
