"""Alembic tests: offline SQL rendering, a real sqlite upgrade, model parity.

All runs go through the repo-root ``alembic.ini`` + ``alembic/env.py`` with the
URL injected programmatically (``sqlalchemy.url``), so no environment variables,
network, or running database are needed — postgres is exercised via *offline*
rendering, which never connects.
"""

import io
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
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
    ScheduleProposal,
    Task,
    session_scope,
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
                ) == "f4a5b6c7d8e9"
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
                ) == "f4a5b6c7d8e9"
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
