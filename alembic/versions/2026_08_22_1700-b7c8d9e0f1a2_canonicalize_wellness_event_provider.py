"""canonicalize wellness event source providers

Revision ID: b7c8d9e0f1a2
Revises: a6b7c8d9e0f1
Create Date: 2026-08-22 17:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op
from healthmes.store.base import JSONB

revision: str = "b7c8d9e0f1a2"
down_revision: str | Sequence[str] | None = "a6b7c8d9e0f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "wellness_event"
_RAW_TABLE = "raw_ingest_event"
_CHECK = "ck_wellness_event_source_provider_canonical"
_RAW_CHECK = "ck_raw_ingest_event_source_canonical"
_COLLISION_INDEX = "ux_wellness_event_canonical_source_collision_guard"
_ALLOWED_PROVIDER_CHARACTERS = (
    "abcdefghijklmnopqrstuvwxyz0123456789._-"
)
_ALLOWED_FIRST_PROVIDER_CHARACTERS = (
    "abcdefghijklmnopqrstuvwxyz0123456789"
)
_ALLOWED_INPUT_PROVIDER_CHARACTERS = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    f"{_ALLOWED_PROVIDER_CHARACTERS}"
)
_ALLOWED_FIRST_INPUT_PROVIDER_CHARACTERS = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    f"{_ALLOWED_FIRST_PROVIDER_CHARACTERS}"
)


def _ascii_lower_sql(column: str) -> str:
    expression = column
    for uppercase, lowercase in zip(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "abcdefghijklmnopqrstuvwxyz",
        strict=True,
    ):
        expression = f"replace({expression}, '{uppercase}', '{lowercase}')"
    return expression


def _allowed_characters_removed_sql(
    column: str,
    *,
    allowed: str = _ALLOWED_PROVIDER_CHARACTERS,
) -> str:
    expression = column
    for character in allowed:
        expression = f"replace({expression}, '{character}', '')"
    return expression


_CHECK_EXPRESSION = (
    "source_provider = trim(source_provider) "
    "AND length(source_provider) BETWEEN 1 AND 64 "
    "AND source_provider = substr(source_provider, 1, 64) "
    "AND length("
    f"{_allowed_characters_removed_sql(
        'substr(source_provider, 1, 1)',
        allowed=_ALLOWED_FIRST_PROVIDER_CHARACTERS,
    )}"
    ") = 0 "
    "AND length("
    f"{_allowed_characters_removed_sql('source_provider')}"
    ") = 0"
)


_VALID_INPUT_EXPRESSION = (
    "length(trim(source_provider)) BETWEEN 1 AND 64 "
    "AND trim(source_provider) = "
    "substr(trim(source_provider), 1, 64) "
    "AND length("
    f"{_allowed_characters_removed_sql(
        'substr(trim(source_provider), 1, 1)',
        allowed=_ALLOWED_FIRST_INPUT_PROVIDER_CHARACTERS,
    )}"
    ") = 0 "
    "AND length("
    f"{_allowed_characters_removed_sql(
        'trim(source_provider)',
        allowed=_ALLOWED_INPUT_PROVIDER_CHARACTERS,
    )}"
    ") = 0"
)
_CANONICAL_PROVIDER_EXPRESSION = _ascii_lower_sql(
    "trim(source_provider)"
)
_RAW_VALID_INPUT_EXPRESSION = _VALID_INPUT_EXPRESSION.replace(
    "source_provider",
    "source",
)
_RAW_CANONICAL_PROVIDER_EXPRESSION = _ascii_lower_sql("trim(source)")
_RAW_CHECK_EXPRESSION = _CHECK_EXPRESSION.replace(
    "source_provider",
    "source",
)


def _wellness_event_table(*, include_check: bool) -> sa.Table:
    """Return the frozen pre/post migration shape for SQLite offline DDL."""

    metadata = sa.MetaData()
    schema_items: list[sa.SchemaItem] = [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column(
            "source_provider",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("source_device", sa.String(length=255), nullable=True),
        sa.Column(
            "source_record_id",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            "capture_method",
            sa.String(length=32),
            nullable=False,
        ),
        sa.Column("quality_flags", JSONB, nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("coverage", sa.Float(), nullable=True),
        sa.Column(
            "sensitivity",
            sa.String(length=32),
            nullable=False,
        ),
        sa.Column(
            "consent_scope",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("retention_policy_id", sa.Uuid(), nullable=True),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("raw_object_id", sa.Uuid(), nullable=True),
        sa.Column("derived_from", JSONB, nullable=True),
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
        sa.ForeignKeyConstraint(
            ["retention_policy_id"],
            ["retention_policy.id"],
            name=(
                "fk_wellness_event_retention_policy_id_"
                "retention_policy"
            ),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["raw_object_id"],
            ["storage_object.id"],
            name="fk_wellness_event_raw_object_id_storage_object",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_wellness_event"),
        sa.UniqueConstraint(
            "source_provider",
            "source_record_id",
            name="uq_wellness_event_source_record",
        ),
    ]
    if include_check:
        schema_items.append(
            sa.CheckConstraint(
                _CHECK_EXPRESSION,
                name=_CHECK,
            )
        )
    table = sa.Table(_TABLE, metadata, *schema_items)
    for column in (
        "event_type",
        "observed_at",
        "recorded_at",
        "source_provider",
        "retention_policy_id",
        "expires_at",
        "raw_object_id",
    ):
        sa.Index(f"ix_wellness_event_{column}", table.c[column])
    sa.Index(
        "ux_wellness_event_event_type_raw_object_id",
        table.c.event_type,
        table.c.raw_object_id,
        unique=True,
    )
    return table


def _raw_ingest_event_table(*, include_check: bool) -> sa.Table:
    """Return the frozen pre/post migration shape for SQLite offline DDL."""

    metadata = sa.MetaData()
    schema_items: list[sa.SchemaItem] = [
        sa.Column("id", sa.Uuid(), nullable=False),
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
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("path", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("parse_status", sa.String(length=32), nullable=False),
        sa.Column("forward_status", sa.String(length=32), nullable=False),
        sa.Column("forward_detail", sa.String(length=255), nullable=True),
        sa.Column("records_forwarded", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_raw_ingest_event"),
    ]
    if include_check:
        schema_items.append(
            sa.CheckConstraint(
                _RAW_CHECK_EXPRESSION,
                name=_RAW_CHECK,
            )
        )
    table = sa.Table(_RAW_TABLE, metadata, *schema_items)
    for column in ("received_at", "source", "sha256"):
        sa.Index(f"ix_raw_ingest_event_{column}", table.c[column])
    return table


def _alter_check(
    *,
    table_name: str,
    check_name: str,
    expression: str,
    copy_from: sa.Table,
    create: bool,
) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        options: dict[str, object] = {"recreate": "always"}
        if context.is_offline_mode():
            options["copy_from"] = copy_from
        with op.batch_alter_table(table_name, **options) as batch:
            if create:
                batch.create_check_constraint(
                    op.f(check_name),
                    expression,
                )
            else:
                batch.drop_constraint(
                    op.f(check_name),
                    type_="check",
                )
        return
    if create:
        op.create_check_constraint(
            op.f(check_name),
            table_name,
            expression,
        )
    else:
        op.drop_constraint(
            op.f(check_name),
            table_name,
            type_="check",
        )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite" and not context.is_offline_mode():
        # Pysqlite defers BEGIN until the first write. Start the transaction
        # before collision detection so every migration stage is atomic.
        bind.execute(
            sa.text(
                "UPDATE wellness_event "
                "SET updated_at = updated_at "
                "WHERE 0"
            )
        )

    # This temporary expression index is the fail-closed collision detector.
    # It refuses Manual/manual keys before any row is normalized or deleted.
    op.execute(
        sa.text(
            f"CREATE UNIQUE INDEX {_COLLISION_INDEX} "
            "ON wellness_event "
            f"({_CANONICAL_PROVIDER_EXPRESSION}, source_record_id)"
        )
    )
    op.execute(
        sa.text(
            "UPDATE raw_ingest_event "
            "SET source = CASE "
            f"WHEN {_RAW_VALID_INPUT_EXPRESSION} "
            f"THEN {_RAW_CANONICAL_PROVIDER_EXPRESSION} "
            "ELSE NULL END "
            f"WHERE source <> {_RAW_CANONICAL_PROVIDER_EXPRESSION} "
            f"OR NOT ({_RAW_VALID_INPUT_EXPRESSION})"
        )
    )
    op.execute(
        sa.text(
            "UPDATE wellness_event "
            "SET source_provider = CASE "
            f"WHEN {_VALID_INPUT_EXPRESSION} "
            f"THEN {_CANONICAL_PROVIDER_EXPRESSION} "
            "ELSE NULL END "
            f"WHERE source_provider <> {_CANONICAL_PROVIDER_EXPRESSION} "
            f"OR NOT ({_VALID_INPUT_EXPRESSION})"
        )
    )
    # Raw-ingest rows and their wellness index must retain one provider
    # identity. UUID text is dashed on PostgreSQL and compact on SQLite.
    op.execute(
        sa.text(
            "UPDATE wellness_event "
            "SET source_provider = NULL "
            "WHERE event_type = 'raw_ingest' "
            "AND EXISTS ("
            "SELECT 1 FROM raw_ingest_event "
            "WHERE replace(CAST(raw_ingest_event.id AS TEXT), '-', '') "
            "= replace(wellness_event.source_record_id, '-', '') "
            "AND raw_ingest_event.source "
            "<> wellness_event.source_provider"
            ")"
        )
    )
    op.drop_index(_COLLISION_INDEX, table_name=_TABLE)
    _alter_check(
        table_name=_RAW_TABLE,
        check_name=_RAW_CHECK,
        expression=_RAW_CHECK_EXPRESSION,
        copy_from=_raw_ingest_event_table(include_check=False),
        create=True,
    )
    _alter_check(
        table_name=_TABLE,
        check_name=_CHECK,
        expression=_CHECK_EXPRESSION,
        copy_from=_wellness_event_table(include_check=False),
        create=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite" and not context.is_offline_mode():
        # Keep the batch rebuild atomic with any later downgrade revision.
        # Without a physical BEGIN, a later fail-closed downgrade can leave
        # SQLite's _alembic_tmp_wellness_event table behind.
        bind.execute(
            sa.text(
                "UPDATE wellness_event "
                "SET updated_at = updated_at "
                "WHERE 0"
            )
        )
    _alter_check(
        table_name=_TABLE,
        check_name=_CHECK,
        expression=_CHECK_EXPRESSION,
        copy_from=_wellness_event_table(include_check=True),
        create=False,
    )
    _alter_check(
        table_name=_RAW_TABLE,
        check_name=_RAW_CHECK,
        expression=_RAW_CHECK_EXPRESSION,
        copy_from=_raw_ingest_event_table(include_check=True),
        create=False,
    )
