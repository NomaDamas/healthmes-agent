"""anchor decision receipt retention to trusted server receive time

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-17 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op
from healthmes.store.base import JSONB

revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "decision_request_receipt"
_BASIS_INDEX = "ix_decision_request_receipt_retention_basis_at"
_STATE_CONSTRAINT = (
    "ck_decision_request_receipt_state_payload_consistent"
)
_GENERATION_CONSTRAINT = (
    "ck_decision_request_receipt_lease_generation_positive"
)
_STATE_EXPRESSION = (
    "("
    "state = 'pending' "
    "AND owner_token IS NOT NULL "
    "AND lease_expires_at IS NOT NULL "
    "AND result_payload IS NULL "
    "AND result_expires_at IS NULL"
    ") OR ("
    "state = 'completed' "
    "AND owner_token IS NULL "
    "AND lease_expires_at IS NULL "
    "AND result_payload IS NOT NULL "
    "AND result_expires_at IS NOT NULL"
    ") OR ("
    "state = 'tombstone' "
    "AND owner_token IS NULL "
    "AND lease_expires_at IS NULL "
    "AND result_payload IS NULL "
    "AND result_expires_at IS NULL"
    ")"
)


def _receipt_table(*, include_retention_basis: bool) -> sa.Table:
    """Return the frozen post-a1 receipt shape for SQLite offline DDL."""

    metadata = sa.MetaData()
    columns: list[sa.SchemaItem] = [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column(
            "request_fingerprint",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("owner_token", sa.Uuid(), nullable=True),
        sa.Column(
            "lease_generation",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("result_payload", JSONB, nullable=True),
        sa.Column(
            "result_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    ]
    if include_retention_basis:
        columns.append(
            sa.Column(
                "retention_basis_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            )
        )
    columns.extend(
        [
            sa.Column(
                "expires_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
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
            sa.CheckConstraint(
                _STATE_EXPRESSION,
                name=_STATE_CONSTRAINT,
            ),
            sa.CheckConstraint(
                "lease_generation >= 1",
                name=_GENERATION_CONSTRAINT,
            ),
            sa.PrimaryKeyConstraint(
                "id",
                name="pk_decision_request_receipt",
            ),
            sa.UniqueConstraint(
                "request_id",
                name="uq_decision_request_receipt_request_id",
            ),
        ]
    )
    table = sa.Table(_TABLE, metadata, *columns)
    for column in (
        "request_id",
        "state",
        "owner_token",
        "lease_expires_at",
        "result_expires_at",
        "expires_at",
    ):
        sa.Index(
            f"ix_decision_request_receipt_{column}",
            table.c[column],
        )
    if include_retention_basis:
        sa.Index(
            _BASIS_INDEX,
            table.c.retention_basis_at,
        )
    return table


def _add_retention_basis() -> None:
    column = sa.Column(
        "retention_basis_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )
    if op.get_context().dialect.name == "sqlite":
        batch_options: dict[str, object] = {"recreate": "always"}
        if context.is_offline_mode():
            batch_options["copy_from"] = _receipt_table(
                include_retention_basis=False
            )
        with op.batch_alter_table(
            _TABLE,
            **batch_options,
        ) as batch:
            batch.add_column(column)
            batch.create_index(
                _BASIS_INDEX,
                ["retention_basis_at"],
            )
        return

    op.add_column(_TABLE, column)
    op.create_index(
        _BASIS_INDEX,
        _TABLE,
        ["retention_basis_at"],
    )


def _backfill_retention_basis() -> None:
    op.execute(
        sa.text(
            "UPDATE decision_request_receipt "
            "SET retention_basis_at = created_at"
        )
    )


def _clamp_sensitive_result_expiry() -> None:
    if op.get_context().dialect.name == "sqlite":
        op.execute(
            sa.text(
                "UPDATE decision_request_receipt "
                "SET result_expires_at = CASE "
                "WHEN EXISTS ("
                "SELECT 1 FROM retention_policy "
                "WHERE data_class = 'decision' AND enabled = 1 "
                "AND retention_days IS NOT NULL"
                ") THEN min("
                "result_expires_at, "
                "expires_at, "
                "datetime(retention_basis_at, '+' || ("
                "SELECT retention_days FROM retention_policy "
                "WHERE data_class = 'decision' AND enabled = 1 "
                "AND retention_days IS NOT NULL LIMIT 1"
                ") || ' days')"
                ") ELSE min(result_expires_at, expires_at) END "
                "WHERE state = 'completed'"
            )
        )
    else:
        op.execute(
            sa.text(
                "UPDATE decision_request_receipt "
                "SET result_expires_at = CASE "
                "WHEN EXISTS ("
                "SELECT 1 FROM retention_policy "
                "WHERE data_class = 'decision' AND enabled "
                "AND retention_days IS NOT NULL"
                ") THEN LEAST("
                "result_expires_at, "
                "expires_at, "
                "retention_basis_at + ("
                "SELECT retention_days * INTERVAL '1 day' "
                "FROM retention_policy "
                "WHERE data_class = 'decision' AND enabled "
                "AND retention_days IS NOT NULL LIMIT 1"
                ")"
                ") ELSE LEAST(result_expires_at, expires_at) END "
                "WHERE state = 'completed'"
            )
        )

    op.execute(
        sa.text(
            "UPDATE decision_request_receipt "
            "SET state = 'tombstone', "
            "owner_token = NULL, "
            "lease_expires_at = NULL, "
            "result_payload = NULL, "
            "result_expires_at = NULL "
            "WHERE state = 'completed' "
            "AND result_expires_at <= CURRENT_TIMESTAMP"
        )
    )


def upgrade() -> None:
    _add_retention_basis()
    _backfill_retention_basis()
    _clamp_sensitive_result_expiry()


def downgrade() -> None:
    raise RuntimeError(
        "decision receipt retention bases are forward-only because reverting "
        "would let client semantic timestamps extend sensitive replay data"
    )
