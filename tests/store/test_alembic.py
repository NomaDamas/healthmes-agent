"""Alembic tests: offline SQL rendering, a real sqlite upgrade, model parity.

All runs go through the repo-root ``alembic.ini`` + ``alembic/env.py`` with the
URL injected programmatically (``sqlalchemy.url``), so no environment variables,
network, or running database are needed — postgres is exercised via *offline*
rendering, which never connects.
"""

import io
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.orm import sessionmaker

from healthmes.config import Settings
from healthmes.schedule_proposals import resolution_token, verify_resolution_token
from healthmes.storage import run_storage_maintenance
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

    def test_render_marks_head_revision(self):
        rendered = _render_offline_upgrade("sqlite:///offline-render.db")
        assert "INSERT INTO alembic_version" in rendered
        assert "healthmes_food_log_offline_guard" in rendered
        assert "SELECT COUNT(*) FROM food_log" in rendered

    def test_legacy_cleanup_downgrade_renders_for_both_dialects(self):
        urls = (
            "sqlite:///offline-render.db",
            "postgresql+psycopg://healthmes:healthmes@localhost:5432/healthmes",
        )
        for url in urls:
            rendered = _render_offline_legacy_cleanup_downgrade(url)
            assert "ROW_NUMBER() OVER" in rendered
            assert "ux_calendar_event_mirror_source_healthmes_source_key" in rendered


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
                ) == "e3f4a5b6c7d8"
        finally:
            engine.dispose()

    def test_food_log_backfill_preserves_originals_and_retention(self, tmp_path):
        database_url = f"sqlite:///{tmp_path / 'food-log-backfill.db'}"
        config = _config(database_url)
        command.upgrade(config, "c1d2e3f4a5b6")
        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        food_log = sa.Table("food_log", metadata, autoload_with=engine)
        storage_object = sa.Table("storage_object", metadata, autoload_with=engine)
        first_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        second_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
        media_id = uuid.UUID("33333333-3333-3333-3333-333333333333")
        logged_at = datetime(2024, 2, 29, 23, 59, 58, tzinfo=UTC)
        created_at = datetime(2024, 3, 1, 0, 0, 1, tzinfo=UTC)
        updated_at = datetime(2024, 3, 2, 1, 2, 3, tzinfo=UTC)
        original_rows = [
            {
                "id": first_id,
                "logged_at": logged_at,
                "description": "  김치찌개 🍲  ",
                "media_path": "media/shared.jpg",
                "meal_type": None,
                "source": None,
                "created_at": created_at,
                "updated_at": updated_at,
            },
            {
                "id": second_id,
                "logged_at": logged_at + timedelta(minutes=1),
                "description": "\tLatte\n",
                "media_path": "media/shared.jpg",
                "meal_type": "snack",
                "source": "ios",
                "created_at": created_at + timedelta(seconds=1),
                "updated_at": updated_at + timedelta(seconds=1),
            },
        ]
        with engine.begin() as connection:
            connection.execute(
                storage_object.insert(),
                {
                    "id": media_id.hex,
                    "data_class": "media",
                    "relative_path": "media/shared.jpg",
                    "content_type": "image/jpeg",
                    "size_bytes": 123,
                    "sha256": "a" * 64,
                    "retention_basis_at": created_at,
                    "expires_at": created_at + timedelta(days=1),
                    "safe_to_purge": True,
                    "purged_at": None,
                    "created_at": created_at,
                    "updated_at": updated_at,
                },
            )
            connection.execute(
                food_log.insert(),
                [
                    {**row, "id": row["id"].hex}
                    for row in original_rows
                ],
            )
        engine.dispose()

        command.upgrade(config, "d2e3f4a5b6c7")

        engine = sa.create_engine(database_url)
        try:
            metadata = sa.MetaData()
            food_log = sa.Table("food_log", metadata, autoload_with=engine)
            wellness_event = sa.Table("wellness_event", metadata, autoload_with=engine)
            storage_object = sa.Table("storage_object", metadata, autoload_with=engine)
            retention_policy = sa.Table(
                "retention_policy", metadata, autoload_with=engine
            )
            with engine.connect() as connection:
                archives = connection.execute(
                    sa.select(wellness_event).where(
                        wellness_event.c.event_type == "legacy.food-log.v1"
                    )
                ).mappings().all()
                events = connection.execute(sa.select(wellness_event)).mappings().all()
                media = connection.execute(
                    sa.select(storage_object).where(
                        storage_object.c.id == media_id.hex
                    )
                ).mappings().one()
                raw_events = [
                    row
                    for row in events
                    if row["event_type"] == "nutrition.raw-capture.v1"
                ]
                policies = {
                    row["id"]: row
                    for row in connection.execute(
                        sa.select(retention_policy)
                    ).mappings()
                }
                assert len(archives) == 2
                assert len(events) == 10
                assert all(row["payload"]["sha256"] for row in archives)
                assert all(row["expires_at"] is None for row in archives)
                assert all(
                    policies[row["retention_policy_id"]]["data_class"]
                    == "legacy_food_log_archive"
                    for row in archives
                )
                archived_originals = {
                    row["source_record_id"]: row["payload"]["original"]
                    for row in archives
                }
                assert archived_originals[str(first_id)]["description"] == "  김치찌개 🍲  "
                assert archived_originals[str(first_id)]["meal_type"] is None
                assert archived_originals[str(first_id)]["source"] is None
                assert archived_originals[str(second_id)]["description"] == "\tLatte\n"
                assert media["expires_at"] == (
                    created_at + timedelta(days=1)
                ).replace(tzinfo=None)
                assert media["retention_basis_at"] == created_at.replace(
                    tzinfo=None
                )
                assert media["safe_to_purge"] is True
                assert all(row["raw_object_id"] is None for row in raw_events)
                assert all(row["retention_policy_id"] is not None for row in events)
                assert all(
                    policies[row["retention_policy_id"]]["data_class"]
                    == "nutrition_raw_capture"
                    for row in raw_events
                )
                assert all(row["expires_at"] is not None for row in raw_events)
                interactions = [
                    row
                    for row in events
                    if row["event_type"] == "nutrition.interaction.v1"
                ]
                assert all(
                    row["payload"]["modality"] == "text"
                    and row["payload"]["nutrition_observation_id"] is None
                    for row in interactions
                )
                second_interaction = next(
                    row
                    for row in interactions
                    if row["source_record_id"] == str(second_id)
                )
                assert (
                    second_interaction["payload"]["items"][0]["meal_type"]
                    == "snack"
                )
                assert connection.scalar(sa.select(sa.func.count()).select_from(food_log)) == 2
        finally:
            engine.dispose()

        command.upgrade(config, "head")
        engine = sa.create_engine(database_url)
        try:
            assert not sa.inspect(engine).has_table("food_log")
            maintenance_session = sessionmaker(bind=engine)()
            try:
                run_storage_maintenance(
                    maintenance_session,
                    Settings(
                        database_url=database_url,
                        data_dir=tmp_path / "storage",
                    ),
                    now=datetime(2030, 1, 1, tzinfo=UTC),
                )
                maintenance_session.commit()
            finally:
                maintenance_session.close()
            metadata = sa.MetaData()
            wellness_event = sa.Table(
                "wellness_event",
                metadata,
                autoload_with=engine,
            )
            with engine.connect() as connection:
                surviving_archives = connection.execute(
                    sa.select(wellness_event).where(
                        wellness_event.c.source_provider
                        == "legacy-food-log-archive"
                    )
                ).mappings().all()
                assert len(surviving_archives) == 2
                assert all(row["expires_at"] is None for row in surviving_archives)
        finally:
            engine.dispose()

        command.downgrade(config, "d2e3f4a5b6c7")
        engine = sa.create_engine(database_url)
        try:
            metadata = sa.MetaData()
            food_log = sa.Table("food_log", metadata, autoload_with=engine)
            with engine.connect() as connection:
                restored = connection.execute(
                    sa.select(food_log).order_by(food_log.c.id)
                ).mappings().all()
            assert [
                {
                    "id": str(uuid.UUID(str(row["id"]))),
                    "logged_at": row["logged_at"],
                    "description": row["description"],
                    "media_path": row["media_path"],
                    "meal_type": row["meal_type"],
                    "source": row["source"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in restored
            ] == [
                {
                    "id": str(row["id"]),
                    "logged_at": row["logged_at"].replace(tzinfo=None),
                    "description": row["description"],
                    "media_path": row["media_path"],
                    "meal_type": row["meal_type"],
                    "source": row["source"],
                    "created_at": row["created_at"].replace(tzinfo=None),
                    "updated_at": row["updated_at"].replace(tzinfo=None),
                }
                for row in original_rows
            ]
        finally:
            engine.dispose()

        command.downgrade(config, "c1d2e3f4a5b6")
        engine = sa.create_engine(database_url)
        try:
            metadata = sa.MetaData()
            food_log = sa.Table("food_log", metadata, autoload_with=engine)
            wellness_event = sa.Table("wellness_event", metadata, autoload_with=engine)
            storage_object = sa.Table("storage_object", metadata, autoload_with=engine)
            with engine.connect() as connection:
                restored = connection.execute(
                    sa.select(food_log).order_by(food_log.c.id)
                ).mappings().all()
                restored_media = connection.execute(
                    sa.select(storage_object).where(
                        storage_object.c.id == media_id.hex
                    )
                ).mappings().one()
                assert [row["description"] for row in restored] == [
                    "  김치찌개 🍲  ",
                    "\tLatte\n",
                ]
                assert restored_media["expires_at"] == (
                    created_at + timedelta(days=1)
                ).replace(tzinfo=None)
                assert restored_media["retention_basis_at"] == created_at.replace(
                    tzinfo=None
                )
                assert restored_media["safe_to_purge"] is True
                assert connection.scalar(
                    sa.select(sa.func.count()).select_from(wellness_event)
                ) == 0
        finally:
            engine.dispose()

    def test_food_log_downgrade_refuses_missing_archive(self, tmp_path):
        database_url = f"sqlite:///{tmp_path / 'food-log-missing-archive.db'}"
        config = _config(database_url)
        command.upgrade(config, "c1d2e3f4a5b6")
        engine = sa.create_engine(database_url)
        food_log = sa.Table(
            "food_log",
            sa.MetaData(),
            autoload_with=engine,
        )
        legacy_id = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        now = datetime(2026, 8, 8, 12, tzinfo=UTC)
        with engine.begin() as connection:
            connection.execute(
                food_log.insert(),
                {
                    "id": legacy_id.hex,
                    "logged_at": now,
                    "description": "Dinner",
                    "media_path": None,
                    "meal_type": "dinner",
                    "source": "ios",
                    "created_at": now,
                    "updated_at": now,
                },
            )
        engine.dispose()
        command.upgrade(config, "head")

        engine = sa.create_engine(database_url)
        wellness_event = sa.Table(
            "wellness_event",
            sa.MetaData(),
            autoload_with=engine,
        )
        with engine.begin() as connection:
            connection.execute(
                sa.delete(wellness_event).where(
                    wellness_event.c.source_provider
                    == "legacy-food-log-archive"
                )
            )
        engine.dispose()

        with pytest.raises(RuntimeError, match="missing archive"):
            command.downgrade(config, "d2e3f4a5b6c7")

        engine = sa.create_engine(database_url)
        try:
            assert not sa.inspect(engine).has_table("food_log")
            with engine.connect() as connection:
                assert connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                ) == "e3f4a5b6c7d8"
        finally:
            engine.dispose()

    def test_food_log_removal_refuses_a_corrupt_archive(self, tmp_path):
        database_url = f"sqlite:///{tmp_path / 'food-log-corrupt.db'}"
        config = _config(database_url)
        command.upgrade(config, "c1d2e3f4a5b6")
        engine = sa.create_engine(database_url)
        metadata = sa.MetaData()
        food_log = sa.Table("food_log", metadata, autoload_with=engine)
        now = datetime(2026, 8, 8, 12, tzinfo=UTC)
        with engine.begin() as connection:
            connection.execute(
                food_log.insert(),
                {
                    "id": uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa").hex,
                    "logged_at": now,
                    "description": "Lunch",
                    "media_path": None,
                    "meal_type": "lunch",
                    "source": "ios",
                    "created_at": now,
                    "updated_at": now,
                },
            )
        engine.dispose()
        command.upgrade(config, "d2e3f4a5b6c7")

        engine = sa.create_engine(database_url)
        wellness_event = sa.Table(
            "wellness_event",
            sa.MetaData(),
            autoload_with=engine,
        )
        with engine.begin() as connection:
            archive = connection.execute(
                sa.select(wellness_event).where(
                    wellness_event.c.source_provider
                    == "legacy-food-log-archive"
                )
            ).mappings().one()
            corrupted = dict(archive["payload"])
            corrupted["sha256"] = "0" * 64
            connection.execute(
                sa.update(wellness_event)
                .where(wellness_event.c.id == archive["id"])
                .values(payload=corrupted)
            )
        engine.dispose()

        with pytest.raises(RuntimeError, match="checksum mismatch"):
            command.upgrade(config, "head")

        engine = sa.create_engine(database_url)
        try:
            assert sa.inspect(engine).has_table("food_log")
            with engine.connect() as connection:
                assert connection.scalar(
                    sa.text("SELECT version_num FROM alembic_version")
                ) == "d2e3f4a5b6c7"
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
