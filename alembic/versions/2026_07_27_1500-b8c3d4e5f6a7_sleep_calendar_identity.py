from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8c3d4e5f6a7"
down_revision: str | None = "a7c9e2f41b6d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "calendar_event_mirror",
        sa.Column("healthmes_kind", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "calendar_event_mirror",
        sa.Column("healthmes_source", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "calendar_event_mirror",
        sa.Column("healthmes_source_key", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "calendar_event_mirror",
        sa.Column("observation_fingerprint", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ux_calendar_event_mirror_source_healthmes_source_key",
        "calendar_event_mirror",
        ["calendar_source", "healthmes_source_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ux_calendar_event_mirror_source_healthmes_source_key",
        table_name="calendar_event_mirror",
    )
    op.drop_column("calendar_event_mirror", "observation_fingerprint")
    op.drop_column("calendar_event_mirror", "healthmes_source_key")
    op.drop_column("calendar_event_mirror", "healthmes_source")
    op.drop_column("calendar_event_mirror", "healthmes_kind")
