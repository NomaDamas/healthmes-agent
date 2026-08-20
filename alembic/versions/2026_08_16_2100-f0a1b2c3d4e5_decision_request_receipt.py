"""add bounded decision request idempotency receipts

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-08-16 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op
from healthmes.store.base import JSONB
from healthmes.store.migration_safety import (
    acquire_postgres_downgrade_lock,
)

revision: str = "f0a1b2c3d4e5"
down_revision: str | Sequence[str] | None = "e9f0a1b2c3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "decision_request_receipt",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column(
            "request_fingerprint",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("owner_token", sa.Uuid(), nullable=True),
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("result_payload", JSONB, nullable=True),
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
            "("
            "state = 'pending' "
            "AND owner_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL "
            "AND result_payload IS NULL"
            ") OR ("
            "state = 'completed' "
            "AND owner_token IS NULL "
            "AND lease_expires_at IS NULL "
            "AND result_payload IS NOT NULL"
            ")",
            name="state_payload_consistent",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_decision_request_receipt",
        ),
        sa.UniqueConstraint(
            "request_id",
            name="uq_decision_request_receipt_request_id",
        ),
    )
    for column in (
        "request_id",
        "state",
        "owner_token",
        "lease_expires_at",
        "expires_at",
    ):
        op.create_index(
            op.f(f"ix_decision_request_receipt_{column}"),
            "decision_request_receipt",
            [column],
        )


def _assert_downgrade_is_lossless() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "offline downgrade cannot verify decision request receipts; "
            "run the downgrade online"
        )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        acquire_postgres_downgrade_lock(
            bind,
            "LOCK TABLE decision_request_receipt IN ACCESS EXCLUSIVE MODE",
            resource="Decision Agent request receipts",
        )
    existing = bind.execute(
        sa.text("SELECT 1 FROM decision_request_receipt LIMIT 1")
    ).first()
    if existing is not None:
        raise RuntimeError(
            "cannot downgrade decision_request_receipt without losing "
            "durable idempotency results"
        )


def downgrade() -> None:
    _assert_downgrade_is_lossless()
    op.drop_table("decision_request_receipt")
