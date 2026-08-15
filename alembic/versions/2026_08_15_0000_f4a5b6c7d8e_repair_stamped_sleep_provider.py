"""repair stamped calendar mirror schemas missing sleep provider

Revision ID: f4a5b6c7d8e
Revises: e3f4a5b6c7d8
Create Date: 2026-08-15 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "f4a5b6c7d8e"
down_revision: str | Sequence[str] | None = "e3f4a5b6c7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if context.is_offline_mode():
        op.add_column(
            "calendar_event_mirror",
            sa.Column("sleep_provider", sa.String(length=255), nullable=True),
        )
        return

    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("calendar_event_mirror")
    }
    if "sleep_provider" not in columns:
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


def downgrade() -> None:
    pass
