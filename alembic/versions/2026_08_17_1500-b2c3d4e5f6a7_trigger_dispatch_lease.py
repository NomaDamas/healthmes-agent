"""add durable generation-safe trigger dispatch leases

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-17 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op
from healthmes.store.base import JSONB

revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "trigger_event"
_LEASE_CHECK = "ck_trigger_event_dispatch_lease_consistent"
_GENERATION_CHECK = "ck_trigger_event_dispatch_generation_nonnegative"
_LEASE_INDEX = "ix_trigger_event_dispatch_lease_expires_at"
_LEASE_EXPRESSION = (
    "("
    "dispatch_owner_token IS NULL "
    "AND dispatch_lease_expires_at IS NULL"
    ") OR ("
    "dispatch_owner_token IS NOT NULL "
    "AND dispatch_lease_expires_at IS NOT NULL"
    ")"
)


def _trigger_event_table(*, include_dispatch_lease: bool) -> sa.Table:
    """Return the frozen pre/post shape for offline SQLite batch DDL."""

    metadata = sa.MetaData()
    columns: list[sa.SchemaItem] = [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "fired_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("rule_id", sa.String(length=64), nullable=False),
        sa.Column("payload", JSONB, nullable=True),
        sa.Column("alert_sent", sa.Boolean(), nullable=False),
        sa.Column("dedup_key", sa.String(length=255), nullable=True),
    ]
    if include_dispatch_lease:
        columns.extend(
            (
                sa.Column(
                    "dispatch_owner_token",
                    sa.Uuid(),
                    nullable=True,
                ),
                sa.Column(
                    "dispatch_generation",
                    sa.Integer(),
                    server_default=sa.text("0"),
                    nullable=False,
                ),
                sa.Column(
                    "dispatch_lease_expires_at",
                    sa.DateTime(timezone=True),
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
            sa.PrimaryKeyConstraint("id", name="pk_trigger_event"),
        )
    )
    if include_dispatch_lease:
        columns.extend(
            (
                sa.CheckConstraint(
                    _LEASE_EXPRESSION,
                    name=_LEASE_CHECK,
                ),
                sa.CheckConstraint(
                    "dispatch_generation >= 0",
                    name=_GENERATION_CHECK,
                ),
            )
        )
    table = sa.Table(_TABLE, metadata, *columns)
    sa.Index("ix_trigger_event_fired_at", table.c.fired_at)
    sa.Index("ix_trigger_event_rule_id", table.c.rule_id)
    sa.Index("ix_trigger_event_dedup_key", table.c.dedup_key)
    if include_dispatch_lease:
        sa.Index(
            _LEASE_INDEX,
            table.c.dispatch_lease_expires_at,
        )
    return table


def _columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column(
            "dispatch_owner_token",
            sa.Uuid(),
            nullable=True,
        ),
        sa.Column(
            "dispatch_generation",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "dispatch_lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def _upgrade_offline() -> None:
    if op.get_context().dialect.name == "sqlite":
        with op.batch_alter_table(
            _TABLE,
            copy_from=_trigger_event_table(
                include_dispatch_lease=False,
            ),
            recreate="always",
        ) as batch:
            for column in _columns():
                batch.add_column(column)
            batch.create_check_constraint(
                "dispatch_lease_consistent",
                _LEASE_EXPRESSION,
            )
            batch.create_check_constraint(
                "dispatch_generation_nonnegative",
                "dispatch_generation >= 0",
            )
            batch.create_index(
                _LEASE_INDEX,
                ["dispatch_lease_expires_at"],
            )
        return
    for column in _columns():
        op.add_column(_TABLE, column)
    op.create_check_constraint(
        "dispatch_lease_consistent",
        _TABLE,
        _LEASE_EXPRESSION,
    )
    op.create_check_constraint(
        "dispatch_generation_nonnegative",
        _TABLE,
        "dispatch_generation >= 0",
    )
    op.create_index(
        _LEASE_INDEX,
        _TABLE,
        ["dispatch_lease_expires_at"],
    )


def _upgrade_online(bind: sa.Connection) -> None:
    inspector = sa.inspect(bind)
    columns = {
        item["name"]
        for item in inspector.get_columns(_TABLE)
    }
    with op.batch_alter_table(_TABLE) as batch:
        for column in _columns():
            if column.name not in columns:
                batch.add_column(column)

    inspector = sa.inspect(bind)
    checks = {
        item["name"]
        for item in inspector.get_check_constraints(_TABLE)
    }
    with op.batch_alter_table(_TABLE) as batch:
        if _LEASE_CHECK not in checks:
            batch.create_check_constraint(
                "dispatch_lease_consistent",
                _LEASE_EXPRESSION,
            )
        if _GENERATION_CHECK not in checks:
            batch.create_check_constraint(
                "dispatch_generation_nonnegative",
                "dispatch_generation >= 0",
            )

    indexes = {
        item["name"]
        for item in sa.inspect(bind).get_indexes(_TABLE)
    }
    if _LEASE_INDEX not in indexes:
        op.create_index(
            _LEASE_INDEX,
            _TABLE,
            ["dispatch_lease_expires_at"],
        )


def upgrade() -> None:
    if context.is_offline_mode():
        _upgrade_offline()
        return
    _upgrade_online(op.get_bind())


def downgrade() -> None:
    raise RuntimeError(
        "trigger dispatch leases are forward-only because removing ownership "
        "generations can allow stale workers to publish duplicate results"
    )
