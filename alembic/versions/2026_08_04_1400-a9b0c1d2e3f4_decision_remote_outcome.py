"""record decision-remote outcome metadata

Revision ID: a9b0c1d2e3f4
Revises: f8091a2b3c4d
Create Date: 2026-08-04 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9b0c1d2e3f4"
down_revision: str | Sequence[str] | None = "f8091a2b3c4d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "schedule_proposal",
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "schedule_proposal",
        sa.Column("decision_surface", sa.String(length=32), nullable=True),
    )
    op.create_index(
        op.f("ix_schedule_proposal_decided_at"),
        "schedule_proposal",
        ["decided_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_schedule_proposal_decided_at"),
        table_name="schedule_proposal",
    )
    op.drop_column("schedule_proposal", "decision_surface")
    op.drop_column("schedule_proposal", "decided_at")
