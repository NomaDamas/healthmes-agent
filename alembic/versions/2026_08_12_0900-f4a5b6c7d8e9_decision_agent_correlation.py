"""add Decision Agent persistence correlation

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-08-12 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op
from healthmes.store.base import JSONB
from healthmes.store.migration_safety import (
    acquire_postgres_downgrade_lock,
)

revision: str = "f4a5b6c7d8e9"
down_revision: str | Sequence[str] | None = "e3f4a5b6c7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REQUEST_INDEX = "ux_decision_record_decision_request_id"
TURN_INDEX = "ux_decision_record_decision_turn_id"
CORRELATION_CHECK = "decision_agent_correlation_complete"
CORRELATION_EXPRESSION = (
    "("
    "decision_request_id IS NULL "
    "AND decision_turn_id IS NULL "
    "AND decision_request_fingerprint IS NULL "
    "AND decision_payload IS NULL "
    "AND decision_payload_digest IS NULL"
    ") OR ("
    "decision_request_id IS NOT NULL "
    "AND decision_turn_id IS NOT NULL "
    "AND decision_request_fingerprint IS NOT NULL "
    "AND decision_payload IS NOT NULL "
    "AND decision_payload_digest IS NOT NULL"
    ")"
)


def _decision_record_table(
    *,
    include_agent_correlation: bool,
) -> sa.Table:
    """Return the frozen pre/post migration shape for offline SQLite."""

    metadata = sa.MetaData()
    columns: list[sa.SchemaItem] = [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("tree", JSONB, nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("llm_model", sa.String(length=64), nullable=True),
        sa.Column("tokens", sa.Integer(), nullable=True),
        sa.Column("trigger_event_id", sa.Uuid(), nullable=True),
    ]
    if include_agent_correlation:
        columns.extend(
            (
                sa.Column(
                    "decision_request_id",
                    sa.Uuid(),
                    nullable=True,
                ),
                sa.Column(
                    "decision_turn_id",
                    sa.Uuid(),
                    nullable=True,
                ),
                sa.Column(
                    "decision_request_fingerprint",
                    sa.String(length=64),
                    nullable=True,
                ),
                sa.Column("decision_payload", JSONB, nullable=True),
                sa.Column(
                    "decision_payload_digest",
                    sa.String(length=64),
                    nullable=True,
                ),
            )
        )
    columns.extend(
        (
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
                ["trigger_event_id"],
                ["trigger_event.id"],
                name=(
                    "fk_decision_record_trigger_event_id_trigger_event"
                ),
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint(
                "id",
                name="pk_decision_record",
            ),
        )
    )
    if include_agent_correlation:
        columns.append(
            sa.CheckConstraint(
                CORRELATION_EXPRESSION,
                name=(
                    "ck_decision_record_"
                    "decision_agent_correlation_complete"
                ),
            )
        )
    table = sa.Table("decision_record", metadata, *columns)
    sa.Index("ix_decision_record_kind", table.c.kind)
    sa.Index(
        "ux_decision_record_trigger_event_id",
        table.c.trigger_event_id,
        unique=True,
    )
    if include_agent_correlation:
        sa.Index(
            REQUEST_INDEX,
            table.c.decision_request_id,
            unique=True,
        )
        sa.Index(
            TURN_INDEX,
            table.c.decision_turn_id,
            unique=True,
        )
    return table


def _assert_downgrade_is_lossless() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "offline downgrade cannot verify Decision Agent records; "
            "run the downgrade online"
        )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Keep the losslessness check and destructive DDL in one protected
        # migration transaction. Without this lock, a concurrent finalizer can
        # commit correlated audit data after the check but before column drop.
        acquire_postgres_downgrade_lock(
            bind,
            "LOCK TABLE decision_record IN ACCESS EXCLUSIVE MODE",
            resource="Decision Agent records",
        )
    correlated = bind.execute(
        sa.text(
            """
            SELECT 1
            FROM decision_record
            WHERE decision_request_id IS NOT NULL
               OR decision_turn_id IS NOT NULL
               OR decision_request_fingerprint IS NOT NULL
               OR decision_payload IS NOT NULL
               OR decision_payload_digest IS NOT NULL
            LIMIT 1
            """
        )
    ).first()
    if correlated is not None:
        raise RuntimeError(
            "cannot downgrade decision_record without losing "
            "Decision Agent audit and idempotency data"
        )


def upgrade() -> None:
    columns = (
        sa.Column("decision_request_id", sa.Uuid(), nullable=True),
        sa.Column("decision_turn_id", sa.Uuid(), nullable=True),
        sa.Column(
            "decision_request_fingerprint",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column("decision_payload", JSONB, nullable=True),
        sa.Column(
            "decision_payload_digest",
            sa.String(length=64),
            nullable=True,
        ),
    )
    if context.is_offline_mode():
        if op.get_context().dialect.name == "sqlite":
            with op.batch_alter_table(
                "decision_record",
                copy_from=_decision_record_table(
                    include_agent_correlation=False
                ),
                recreate="always",
            ) as batch:
                for column in columns:
                    batch.add_column(column)
                batch.create_check_constraint(
                    CORRELATION_CHECK,
                    CORRELATION_EXPRESSION,
                )
                batch.create_index(
                    REQUEST_INDEX,
                    ["decision_request_id"],
                    unique=True,
                )
                batch.create_index(
                    TURN_INDEX,
                    ["decision_turn_id"],
                    unique=True,
                )
            return
        for column in columns:
            op.add_column("decision_record", column)
        op.create_check_constraint(
            CORRELATION_CHECK,
            "decision_record",
            CORRELATION_EXPRESSION,
        )
        op.create_index(
            REQUEST_INDEX,
            "decision_record",
            ["decision_request_id"],
            unique=True,
        )
        op.create_index(
            TURN_INDEX,
            "decision_record",
            ["decision_turn_id"],
            unique=True,
        )
        return

    with op.batch_alter_table("decision_record") as batch:
        for column in columns:
            batch.add_column(column)
        batch.create_check_constraint(
            CORRELATION_CHECK,
            CORRELATION_EXPRESSION,
        )
        batch.create_index(
            REQUEST_INDEX,
            ["decision_request_id"],
            unique=True,
        )
        batch.create_index(
            TURN_INDEX,
            ["decision_turn_id"],
            unique=True,
        )


def downgrade() -> None:
    _assert_downgrade_is_lossless()
    if context.is_offline_mode():
        if op.get_context().dialect.name == "sqlite":
            with op.batch_alter_table(
                "decision_record",
                copy_from=_decision_record_table(
                    include_agent_correlation=True
                ),
                recreate="always",
            ) as batch:
                batch.drop_index(TURN_INDEX)
                batch.drop_index(REQUEST_INDEX)
                batch.drop_constraint(
                    CORRELATION_CHECK,
                    type_="check",
                )
                batch.drop_column("decision_request_fingerprint")
                batch.drop_column("decision_payload_digest")
                batch.drop_column("decision_payload")
                batch.drop_column("decision_turn_id")
                batch.drop_column("decision_request_id")
            return
        op.drop_index(TURN_INDEX, table_name="decision_record")
        op.drop_index(REQUEST_INDEX, table_name="decision_record")
        op.drop_constraint(
            CORRELATION_CHECK,
            "decision_record",
            type_="check",
        )
        op.drop_column(
            "decision_record",
            "decision_request_fingerprint",
        )
        op.drop_column("decision_record", "decision_payload_digest")
        op.drop_column("decision_record", "decision_payload")
        op.drop_column("decision_record", "decision_turn_id")
        op.drop_column("decision_record", "decision_request_id")
        return

    with op.batch_alter_table("decision_record") as batch:
        batch.drop_index(TURN_INDEX)
        batch.drop_index(REQUEST_INDEX)
        batch.drop_constraint(CORRELATION_CHECK, type_="check")
        batch.drop_column("decision_request_fingerprint")
        batch.drop_column("decision_payload_digest")
        batch.drop_column("decision_payload")
        batch.drop_column("decision_turn_id")
        batch.drop_column("decision_request_id")
