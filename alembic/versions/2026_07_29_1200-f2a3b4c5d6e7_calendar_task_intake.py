from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if context.is_offline_mode():
        op.add_column(
            "calendar_event_mirror",
            sa.Column("intake_task_id", sa.Uuid(), nullable=True),
        )
        op.create_index(
            op.f("ix_calendar_event_mirror_intake_task_id"),
            "calendar_event_mirror",
            ["intake_task_id"],
            unique=False,
        )
        if op.get_context().dialect.name != "sqlite":
            op.create_foreign_key(
                op.f("fk_calendar_event_mirror_intake_task_id_task"),
                "calendar_event_mirror",
                "task",
                ["intake_task_id"],
                ["id"],
                ondelete="SET NULL",
            )
    else:
        with op.batch_alter_table("calendar_event_mirror") as batch:
            batch.add_column(sa.Column("intake_task_id", sa.Uuid(), nullable=True))
            batch.create_index(
                op.f("ix_calendar_event_mirror_intake_task_id"),
                ["intake_task_id"],
                unique=False,
            )
            batch.create_foreign_key(
                op.f("fk_calendar_event_mirror_intake_task_id_task"),
                "task",
                ["intake_task_id"],
                ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    if context.is_offline_mode():
        if op.get_context().dialect.name != "sqlite":
            op.drop_constraint(
                op.f("fk_calendar_event_mirror_intake_task_id_task"),
                "calendar_event_mirror",
                type_="foreignkey",
            )
        op.drop_index(
            op.f("ix_calendar_event_mirror_intake_task_id"),
            table_name="calendar_event_mirror",
        )
        op.drop_column("calendar_event_mirror", "intake_task_id")
    else:
        with op.batch_alter_table("calendar_event_mirror") as batch:
            batch.drop_constraint(
                op.f("fk_calendar_event_mirror_intake_task_id_task"),
                type_="foreignkey",
            )
            batch.drop_index(op.f("ix_calendar_event_mirror_intake_task_id"))
            batch.drop_column("intake_task_id")
