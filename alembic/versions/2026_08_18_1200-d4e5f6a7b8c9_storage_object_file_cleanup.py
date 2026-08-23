"""track completion of purged storage object file cleanup

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-18 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op
from healthmes.store.base import JSONB
from healthmes.store.migration_safety import (
    acquire_postgres_downgrade_lock,
)

revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLEANUP_CHECK = "storage_object_file_cleanup_consistent"
_CLEANUP_EXPRESSION = (
    "("
    "("
    "file_cleanup_identity IS NULL "
    "OR CAST(file_cleanup_identity AS TEXT) = 'null'"
    ") "
    "AND file_cleanup_completed_at IS NULL"
    ") OR ("
    "purged_at IS NOT NULL "
    "AND file_cleanup_identity IS NOT NULL "
    "AND CAST(file_cleanup_identity AS TEXT) <> 'null'"
    ")"
)


def _legacy_storage_object_table() -> sa.Table:
    """Frozen pre-migration shape for SQLite offline batch DDL."""

    metadata = sa.MetaData()
    columns: list[sa.SchemaItem] = [
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
        sa.Column("data_class", sa.String(length=64), nullable=False),
        sa.Column("relative_path", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("retention_policy_id", sa.Uuid(), nullable=True),
        sa.Column(
            "retention_basis_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("safe_to_purge", sa.Boolean(), nullable=False),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
    ]
    columns.extend(
        (
            sa.ForeignKeyConstraint(
                ["retention_policy_id"],
                ["retention_policy.id"],
                name="fk_storage_object_retention_policy_id_retention_policy",
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_storage_object"),
            sa.UniqueConstraint(
                "relative_path",
                name="uq_storage_object_relative_path",
            ),
        )
    )
    table = sa.Table("storage_object", metadata, *columns)
    for column in (
        "data_class",
        "sha256",
        "retention_policy_id",
        "retention_basis_at",
        "expires_at",
        "safe_to_purge",
        "purged_at",
    ):
        sa.Index(
            f"ix_storage_object_{column}",
            table.c[column],
        )
    return table


def upgrade() -> None:
    bind = op.get_bind()
    if context.is_offline_mode() and bind.dialect.name == "sqlite":
        with op.batch_alter_table(
            "storage_object",
            copy_from=_legacy_storage_object_table(),
            recreate="always",
        ) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "file_cleanup_identity",
                    JSONB,
                    nullable=True,
                )
            )
            batch_op.add_column(
                sa.Column(
                    "file_cleanup_completed_at",
                    sa.DateTime(timezone=True),
                    nullable=True,
                )
            )
            batch_op.create_check_constraint(
                _CLEANUP_CHECK,
                _CLEANUP_EXPRESSION,
            )
            batch_op.create_index(
                "ix_storage_object_file_cleanup_completed_at",
                ["file_cleanup_completed_at"],
            )
        return
    if bind.dialect.name == "sqlite":
        # Pysqlite's legacy transaction mode does not physically BEGIN for
        # DDL. This no-op write starts the transaction that Alembic commits or
        # rolls back around every schema and data change below.
        bind.execute(
            sa.text(
                "UPDATE storage_object "
                "SET updated_at = updated_at "
                "WHERE 0"
            )
        )
    op.add_column(
        "storage_object",
        sa.Column(
            "file_cleanup_identity",
            JSONB,
            nullable=True,
        ),
    )
    op.add_column(
        "storage_object",
        sa.Column(
            "file_cleanup_completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        op.f("ix_storage_object_file_cleanup_completed_at"),
        "storage_object",
        ["file_cleanup_completed_at"],
    )
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("storage_object") as batch_op:
            batch_op.create_check_constraint(
                _CLEANUP_CHECK,
                _CLEANUP_EXPRESSION,
            )
    else:
        op.create_check_constraint(
            _CLEANUP_CHECK,
            "storage_object",
            _CLEANUP_EXPRESSION,
        )
    # Existing purged rows may still have payload bytes after an interrupted
    # legacy one-phase cleanup. Leave both fields NULL so current maintenance
    # can prove absence or capture the exact indexed regular-file generation
    # before acknowledging cleanup.


def _assert_downgrade_is_lossless() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "offline downgrade cannot verify pending storage file cleanup; "
            "run the downgrade online"
        )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        acquire_postgres_downgrade_lock(
            bind,
            "LOCK TABLE storage_object IN ACCESS EXCLUSIVE MODE",
            resource="storage object file cleanup",
        )
    elif bind.dialect.name == "sqlite":
        # SQLAlchemy defers SQLite BEGIN until the first write. Reserve the
        # database before checking pending cleanup so a concurrent retention
        # writer commits first and its metadata cannot be dropped unseen.
        bind.execute(
            sa.text(
                "UPDATE storage_object "
                "SET updated_at = updated_at "
                "WHERE 0"
            )
        )
    pending = bind.execute(
        sa.text(
            "SELECT 1 FROM storage_object "
            "WHERE purged_at IS NOT NULL "
            "AND file_cleanup_completed_at IS NULL "
            "LIMIT 1"
        )
    ).first()
    if pending is not None:
        raise RuntimeError(
            "cannot downgrade storage file cleanup while purged payload "
            "deletion is still pending"
        )


def downgrade() -> None:
    _assert_downgrade_is_lossless()
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("storage_object") as batch_op:
            batch_op.drop_constraint(
                _CLEANUP_CHECK,
                type_="check",
            )
    else:
        op.drop_constraint(
            "storage_object_file_cleanup_consistent",
            "storage_object",
            type_="check",
        )
    op.drop_index(
        op.f("ix_storage_object_file_cleanup_completed_at"),
        table_name="storage_object",
    )
    op.drop_column("storage_object", "file_cleanup_completed_at")
    op.drop_column("storage_object", "file_cleanup_identity")
