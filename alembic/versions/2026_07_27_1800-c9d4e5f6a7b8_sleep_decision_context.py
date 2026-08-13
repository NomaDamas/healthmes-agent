from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c9d4e5f6a7b8"
down_revision: str | None = "b8c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "calendar_event_mirror",
        sa.Column("sleep_local_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "calendar_event_mirror",
        sa.Column("sleep_duration_minutes", sa.Integer(), nullable=True),
    )
    op.add_column(
        "calendar_event_mirror",
        sa.Column("sleep_time_in_bed_minutes", sa.Integer(), nullable=True),
    )
    op.create_index(
        op.f("ix_calendar_event_mirror_sleep_local_date"),
        "calendar_event_mirror",
        ["sleep_local_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_calendar_event_mirror_sleep_local_date"),
        table_name="calendar_event_mirror",
    )
    op.drop_column("calendar_event_mirror", "sleep_time_in_bed_minutes")
    op.drop_column("calendar_event_mirror", "sleep_duration_minutes")
    op.drop_column("calendar_event_mirror", "sleep_local_date")
