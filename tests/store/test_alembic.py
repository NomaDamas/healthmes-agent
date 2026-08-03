"""Alembic tests: offline SQL rendering, a real sqlite upgrade, model parity.

All runs go through the repo-root ``alembic.ini`` + ``alembic/env.py`` with the
URL injected programmatically (``sqlalchemy.url``), so no environment variables,
network, or running database are needed — postgres is exercised via *offline*
rendering, which never connects.
"""

import io
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.orm import sessionmaker

from alembic import command
from healthmes.store import Base, DecisionKind, DecisionRecord, Task, session_scope

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_TABLES = {
    "weekly_goal",
    "task",
    "calendar_event_mirror",
    "calendar_mutation_proposal",
    "schedule_proposal",
    "food_log",
    "app_usage_sample",
    "cognitive_energy_estimate",
    "decision_record",
    "insight",
    "medical_record",
    "trigger_event",
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


def test_migration_graph_has_single_head():
    script = ScriptDirectory.from_config(_config("sqlite://"))

    assert len(script.get_heads()) == 1


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

    def test_render_marks_head_revision(self):
        rendered = _render_offline_upgrade("sqlite:///offline-render.db")
        assert "INSERT INTO alembic_version" in rendered


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

    def test_upgrade_is_idempotent_at_head(self, tmp_path):
        database_url = f"sqlite:///{tmp_path / 'twice.db'}"
        config = _config(database_url)
        command.upgrade(config, "head")
        command.upgrade(config, "head")  # no-op, must not raise

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
