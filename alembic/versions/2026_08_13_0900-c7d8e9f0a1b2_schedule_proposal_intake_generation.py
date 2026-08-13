"""bind timed-intake proposals to one calendar account generation

Revision ID: c7d8e9f0a1b2
Revises: b6c7d8e9f0a1
Create Date: 2026-08-13 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c7d8e9f0a1b2"
down_revision: str | Sequence[str] | None = "b6c7d8e9f0a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UNRESOLVED_GENERATION = "calendar_intake_generation_unresolved"


def upgrade() -> None:
    with op.batch_alter_table("schedule_proposal") as batch:
        batch.add_column(
            sa.Column(
                "intake_account_generation",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column(
                "invalidation_reason",
                sa.String(length=64),
                nullable=True,
            )
        )

    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE schedule_proposal "
            "SET intake_account_generation = ("
            "  SELECT MIN(calendar_event_mirror.connection_generation) "
            "  FROM calendar_event_mirror "
            "  WHERE calendar_event_mirror.calendar_source = "
            "        schedule_proposal.intake_calendar_source "
            "    AND calendar_event_mirror.external_id = "
            "        schedule_proposal.intake_external_id"
            ") "
            "WHERE intake_calendar_source IS NOT NULL "
            "  AND intake_external_id IS NOT NULL "
            "  AND intake_revision IS NOT NULL "
            "  AND ("
            "    SELECT COUNT(*) FROM calendar_event_mirror "
            "    WHERE calendar_event_mirror.calendar_source = "
            "          schedule_proposal.intake_calendar_source "
            "      AND calendar_event_mirror.external_id = "
            "          schedule_proposal.intake_external_id"
            "  ) = 1"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE schedule_proposal "
            "SET status = 'invalidated', "
            "    invalidation_reason = :reason "
            "WHERE status IN ('proposed', 'accepted') "
            "  AND ("
            "    intake_calendar_source IS NOT NULL "
            "    OR intake_external_id IS NOT NULL "
            "    OR intake_revision IS NOT NULL"
            "  ) "
            "  AND intake_account_generation IS NULL"
        ),
        {"reason": _UNRESOLVED_GENERATION},
    )


def downgrade() -> None:
    with op.batch_alter_table("schedule_proposal") as batch:
        batch.drop_column("invalidation_reason")
        batch.drop_column("intake_account_generation")
