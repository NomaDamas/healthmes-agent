from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "sleep_reconciliation_proposal",
        sa.Column("calendar_source", sa.String(length=32), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("observation_fingerprint", sa.String(length=255), nullable=False),
        sa.Column("snapshot", JSONB, nullable=False),
        sa.Column("provider_state", JSONB, nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("receipt", JSONB, nullable=True),
        sa.Column("dedup_key", sa.String(length=255), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sleep_reconciliation_proposal"),
        sa.UniqueConstraint(
            "dedup_key",
            name="uq_sleep_reconciliation_proposal_dedup_key",
        ),
    )
    op.create_index(
        op.f("ix_sleep_reconciliation_proposal_calendar_source"),
        "sleep_reconciliation_proposal",
        ["calendar_source"],
    )
    op.create_index(
        op.f("ix_sleep_reconciliation_proposal_local_date"),
        "sleep_reconciliation_proposal",
        ["local_date"],
    )
    op.create_index(
        op.f("ix_sleep_reconciliation_proposal_status"),
        "sleep_reconciliation_proposal",
        ["status"],
    )
    op.create_index(
        op.f("ix_sleep_reconciliation_proposal_expires_at"),
        "sleep_reconciliation_proposal",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_table("sleep_reconciliation_proposal")
