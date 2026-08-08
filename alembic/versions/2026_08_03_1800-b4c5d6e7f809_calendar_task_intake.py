from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "b4c5d6e7f809"
down_revision: str | None = "a3b4c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _calendar_source_enum() -> sa.Enum:
    return sa.Enum(
        "google",
        "caldav",
        name="calendarsource",
        native_enum=False,
        length=32,
    )


def upgrade() -> None:
    if context.is_offline_mode():
        op.add_column(
            "calendar_event_mirror",
            sa.Column("intake_task_id", sa.Uuid(), nullable=True),
        )
        op.add_column(
            "calendar_event_mirror",
            sa.Column(
                "intake_opted_out",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
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
        op.add_column(
            "schedule_proposal",
            sa.Column(
                "intake_calendar_source",
                _calendar_source_enum(),
                nullable=True,
            ),
        )
        op.add_column(
            "schedule_proposal",
            sa.Column("intake_external_id", sa.String(length=255), nullable=True),
        )
        op.add_column(
            "schedule_proposal",
            sa.Column("intake_revision", sa.String(length=255), nullable=True),
        )
    else:
        inspector = sa.inspect(op.get_bind())
        mirror_columns = {
            item["name"]
            for item in inspector.get_columns("calendar_event_mirror")
        }
        mirror_indexes = {
            item["name"]
            for item in inspector.get_indexes("calendar_event_mirror")
        }
        mirror_foreign_keys = {
            item["name"]
            for item in inspector.get_foreign_keys("calendar_event_mirror")
        }
        with op.batch_alter_table("calendar_event_mirror") as batch:
            if "intake_task_id" not in mirror_columns:
                batch.add_column(
                    sa.Column("intake_task_id", sa.Uuid(), nullable=True)
                )
            if "intake_opted_out" not in mirror_columns:
                batch.add_column(
                    sa.Column(
                        "intake_opted_out",
                        sa.Boolean(),
                        nullable=False,
                        server_default=sa.false(),
                    )
                )
            if (
                op.f("ix_calendar_event_mirror_intake_task_id")
                not in mirror_indexes
            ):
                batch.create_index(
                    op.f("ix_calendar_event_mirror_intake_task_id"),
                    ["intake_task_id"],
                    unique=False,
                )
            if (
                op.f("fk_calendar_event_mirror_intake_task_id_task")
                not in mirror_foreign_keys
            ):
                batch.create_foreign_key(
                    op.f("fk_calendar_event_mirror_intake_task_id_task"),
                    "task",
                    ["intake_task_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
        proposal_columns = {
            item["name"]
            for item in inspector.get_columns("schedule_proposal")
        }
        with op.batch_alter_table("schedule_proposal") as batch:
            if "intake_calendar_source" not in proposal_columns:
                batch.add_column(
                    sa.Column(
                        "intake_calendar_source",
                        _calendar_source_enum(),
                        nullable=True,
                    )
                )
            if "intake_external_id" not in proposal_columns:
                batch.add_column(
                    sa.Column(
                        "intake_external_id",
                        sa.String(length=255),
                        nullable=True,
                    )
                )
            if "intake_revision" not in proposal_columns:
                batch.add_column(
                    sa.Column(
                        "intake_revision",
                        sa.String(length=255),
                        nullable=True,
                    )
                )


def downgrade() -> None:
    if context.is_offline_mode():
        op.drop_column("schedule_proposal", "intake_revision")
        op.drop_column("schedule_proposal", "intake_external_id")
        op.drop_column("schedule_proposal", "intake_calendar_source")
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
        op.drop_column("calendar_event_mirror", "intake_opted_out")
    else:
        with op.batch_alter_table("calendar_event_mirror") as batch:
            batch.drop_constraint(
                op.f("fk_calendar_event_mirror_intake_task_id_task"),
                type_="foreignkey",
            )
            batch.drop_index(op.f("ix_calendar_event_mirror_intake_task_id"))
            batch.drop_column("intake_task_id")
            batch.drop_column("intake_opted_out")
        with op.batch_alter_table("schedule_proposal") as batch:
            batch.drop_column("intake_revision")
            batch.drop_column("intake_external_id")
            batch.drop_column("intake_calendar_source")
