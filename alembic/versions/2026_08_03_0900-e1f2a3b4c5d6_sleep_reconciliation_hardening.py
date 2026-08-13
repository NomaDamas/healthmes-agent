from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "calendar_event_mirror",
        sa.Column("sleep_provider", sa.String(length=255), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE calendar_event_mirror "
            "SET sleep_provider = healthmes_source "
            "WHERE is_agent_created = true "
            "AND healthmes_kind = 'actual_sleep'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE calendar_event_mirror SET "
            "healthmes_kind = NULL, "
            "healthmes_source = NULL, "
            "healthmes_source_key = NULL, "
            "observation_fingerprint = NULL, "
            "sleep_local_date = NULL, "
            "sleep_provider = NULL, "
            "sleep_duration_minutes = NULL, "
            "sleep_time_in_bed_minutes = NULL "
            "WHERE is_agent_created = false"
        )
    )


def downgrade() -> None:
    op.drop_column("calendar_event_mirror", "sleep_provider")
