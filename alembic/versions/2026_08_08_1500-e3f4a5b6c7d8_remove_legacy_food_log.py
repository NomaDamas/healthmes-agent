"""remove the verified legacy food_log contract

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-08-08 15:00:00
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from alembic import context, op

revision: str = "e3f4a5b6c7d8"
down_revision: str | Sequence[str] | None = "d2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ARCHIVE_PROVIDER = "legacy-food-log-archive"
MIGRATION_SCHEMA = "legacy-food-log-archive-v1"
REQUIRED_EVENT_TYPES = {
    "nutrition.interaction.v1",
    "nutrition.raw-capture.v1",
    "nutrition.operation.v1",
    "nutrition.intake-outcome.v1",
}


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _aware(value).isoformat()


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _checksum(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _legacy_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "migration_schema": MIGRATION_SCHEMA,
        "id": str(row["id"]),
        "logged_at": _iso(row["logged_at"]),
        "description": row["description"],
        "media_path": row["media_path"],
        "meal_type": row["meal_type"],
        "source": row["source"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _database_uuid(value: str, dialect_name: str) -> uuid.UUID | str:
    parsed = uuid.UUID(value)
    return parsed.hex if dialect_name == "sqlite" else parsed


def _validate_archive(bind: sa.Connection) -> None:
    metadata = sa.MetaData()
    food_log = sa.Table("food_log", metadata, autoload_with=bind)
    wellness_event = sa.Table("wellness_event", metadata, autoload_with=bind)
    source_rows = list(bind.execute(sa.select(food_log)).mappings())
    archives = list(
        bind.execute(
            sa.select(wellness_event).where(
                wellness_event.c.source_provider == ARCHIVE_PROVIDER
            )
        ).mappings()
    )
    archive_by_id = {str(row["source_record_id"]): row for row in archives}
    migrated_types: dict[str, set[str]] = {}
    for row in bind.execute(sa.select(wellness_event)).mappings():
        legacy_id = (row["derived_from"] or {}).get("legacy_food_log_id")
        if legacy_id is not None:
            migrated_types.setdefault(str(legacy_id), set()).add(row["event_type"])

    if len(archive_by_id) != len(source_rows):
        raise RuntimeError("FoodLog removal blocked: archive row count mismatch")
    for source in source_rows:
        legacy_id = str(uuid.UUID(str(source["id"])))
        archive = archive_by_id.get(legacy_id)
        if archive is None:
            raise RuntimeError(f"FoodLog removal blocked: missing archive for {legacy_id}")
        payload = archive["payload"] or {}
        original = payload.get("original")
        if (
            not isinstance(original, dict)
            or original.get("migration_schema") != MIGRATION_SCHEMA
            or payload.get("sha256") != _checksum(original)
            or payload.get("sha256") != _checksum(_legacy_payload(source))
        ):
            raise RuntimeError(
                f"FoodLog removal blocked: archive checksum mismatch for {legacy_id}"
            )
        missing = REQUIRED_EVENT_TYPES - migrated_types.get(legacy_id, set())
        if missing:
            raise RuntimeError(
                f"FoodLog removal blocked: incomplete ledger for {legacy_id}: "
                f"{sorted(missing)}"
            )


def _create_food_log_table() -> None:
    op.create_table(
        "food_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("logged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("media_path", sa.Text(), nullable=True),
        sa.Column("meal_type", sa.String(length=32), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_food_log")),
    )
    op.create_index(op.f("ix_food_log_logged_at"), "food_log", ["logged_at"])


def _restored_rows(bind: sa.Connection) -> list[dict[str, Any]]:
    wellness_event = sa.Table(
        "wellness_event",
        sa.MetaData(),
        autoload_with=bind,
    )
    events = list(bind.execute(sa.select(wellness_event)).mappings())
    archives = [
        row for row in events if row["source_provider"] == ARCHIVE_PROVIDER
    ]
    durable_legacy_ids = {
        str(legacy_id)
        for row in events
        if (legacy_id := (row["derived_from"] or {}).get("legacy_food_log_id"))
        is not None
    }
    archive_ids = [str(row["source_record_id"]) for row in archives]
    if len(set(archive_ids)) != len(archive_ids):
        raise RuntimeError("FoodLog downgrade blocked: duplicate archive")
    missing = durable_legacy_ids - set(archive_ids)
    if missing:
        raise RuntimeError(
            "FoodLog downgrade blocked: missing archive for "
            f"{sorted(missing)}"
        )

    restored: list[dict[str, Any]] = []
    for archive in archives:
        payload = archive["payload"] or {}
        original = payload.get("original")
        archive_id = str(archive["source_record_id"])
        if (
            not isinstance(original, dict)
            or original.get("migration_schema") != MIGRATION_SCHEMA
            or str(uuid.UUID(str(original.get("id"))))
            != str(uuid.UUID(archive_id))
            or payload.get("sha256") != _checksum(original)
        ):
            raise RuntimeError("FoodLog downgrade blocked: corrupt archive")
        restored.append(
            {
                "id": _database_uuid(original["id"], bind.dialect.name),
                "logged_at": datetime.fromisoformat(original["logged_at"]),
                "description": original["description"],
                "media_path": original["media_path"],
                "meal_type": original["meal_type"],
                "source": original["source"],
                "created_at": datetime.fromisoformat(original["created_at"]),
                "updated_at": datetime.fromisoformat(original["updated_at"]),
            }
        )
    return restored


def upgrade() -> None:
    if context.is_offline_mode():
        op.execute("DROP TABLE IF EXISTS healthmes_food_log_offline_guard")
        op.execute(
            "CREATE TABLE healthmes_food_log_offline_guard "
            "(row_count INTEGER CHECK (row_count = 0))"
        )
        op.execute(
            "INSERT INTO healthmes_food_log_offline_guard (row_count) "
            "SELECT COUNT(*) FROM food_log"
        )
        op.execute("DROP TABLE healthmes_food_log_offline_guard")
    else:
        _validate_archive(op.get_bind())
    op.drop_index(op.f("ix_food_log_logged_at"), table_name="food_log")
    op.drop_table("food_log")


def downgrade() -> None:
    if context.is_offline_mode():
        _create_food_log_table()
        op.execute("-- Archived FoodLog rows require an online Alembic downgrade.")
        return

    bind = op.get_bind()
    restored = _restored_rows(bind)
    _create_food_log_table()
    metadata = sa.MetaData()
    food_log = sa.Table("food_log", metadata, autoload_with=bind)
    if restored:
        bind.execute(sa.insert(food_log), restored)
