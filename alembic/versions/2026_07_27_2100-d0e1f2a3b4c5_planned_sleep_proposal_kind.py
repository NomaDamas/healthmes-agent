from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d0e1f2a3b4c5"
down_revision: str | None = "c9d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "schedule_proposal",
        sa.Column("healthmes_kind", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("schedule_proposal", "healthmes_kind")
