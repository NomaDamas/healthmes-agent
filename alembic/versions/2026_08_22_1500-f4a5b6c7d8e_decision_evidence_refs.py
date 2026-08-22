"""store private decision evidence references

Revision ID: f4a5b6c7d8e
Revises: e3f4a5b6c7d8
Create Date: 2026-08-22 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from healthmes.store.base import JSONB

revision: str = "f4a5b6c7d8e"
down_revision: str | Sequence[str] | None = "e3f4a5b6c7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "decision_record",
        sa.Column("evidence_refs", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("decision_record", "evidence_refs")
