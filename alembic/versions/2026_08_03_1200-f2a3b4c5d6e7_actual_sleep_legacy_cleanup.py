from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _quarantine_downgrade_identity_conflicts() -> None:
    op.execute(
        sa.text(
            """
            UPDATE calendar_event_mirror
            SET is_agent_created = false,
                agent_task_id = NULL,
                healthmes_kind = NULL,
                healthmes_source = NULL,
                healthmes_source_key = NULL,
                observation_fingerprint = NULL,
                sleep_local_date = NULL,
                sleep_provider = NULL,
                sleep_duration_minutes = NULL,
                sleep_time_in_bed_minutes = NULL
            WHERE id IN (
                SELECT id
                FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY calendar_source, healthmes_source_key
                               ORDER BY
                                   CASE
                                       WHEN healthmes_kind = 'actual_sleep'
                                        AND healthmes_source = 'open-wearables'
                                       THEN 0
                                       ELSE 1
                                   END,
                                   id
                           ) AS conflict_rank
                    FROM calendar_event_mirror
                    WHERE healthmes_source_key IS NOT NULL
                ) AS ranked_identity_conflicts
                WHERE conflict_rank > 1
            )
            """
        )
    )


def upgrade() -> None:
    op.drop_index(
        "ux_calendar_event_mirror_source_healthmes_source_key",
        table_name="calendar_event_mirror",
    )
    op.create_index(
        "ux_calendar_event_mirror_calendar_identity",
        "calendar_event_mirror",
        [
            "calendar_source",
            "healthmes_kind",
            "healthmes_source",
            "healthmes_source_key",
        ],
        unique=True,
    )
    op.create_index(
        "ix_calendar_event_mirror_actual_sleep_cleanup",
        "calendar_event_mirror",
        ["calendar_source", "healthmes_kind", "sleep_local_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_calendar_event_mirror_actual_sleep_cleanup",
        table_name="calendar_event_mirror",
    )
    op.drop_index(
        "ux_calendar_event_mirror_calendar_identity",
        table_name="calendar_event_mirror",
    )
    _quarantine_downgrade_identity_conflicts()
    op.create_index(
        "ux_calendar_event_mirror_source_healthmes_source_key",
        "calendar_event_mirror",
        ["calendar_source", "healthmes_source_key"],
        unique=True,
    )
