"""store the retention basis for indexed payloads

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
Create Date: 2026-08-06 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: str | Sequence[str] | None = "b0c1d2e3f4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "storage_object",
        sa.Column(
            "retention_basis_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        op.f("ix_storage_object_retention_basis_at"),
        "storage_object",
        ["retention_basis_at"],
    )
    op.execute(
        """
        UPDATE wellness_event
        SET raw_object_id = NULL
        WHERE id IN (
            SELECT id
            FROM (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY event_type, raw_object_id
                        ORDER BY created_at, id
                    ) AS ownership_rank
                FROM wellness_event
                WHERE raw_object_id IS NOT NULL
            ) AS ranked_ownership
            WHERE ownership_rank > 1
        )
        """
    )
    op.create_index(
        "ux_wellness_event_event_type_raw_object_id",
        "wellness_event",
        ["event_type", "raw_object_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ux_wellness_event_event_type_raw_object_id",
        table_name="wellness_event",
    )
    op.drop_index(
        op.f("ix_storage_object_retention_basis_at"),
        table_name="storage_object",
    )
    op.drop_column("storage_object", "retention_basis_at")
