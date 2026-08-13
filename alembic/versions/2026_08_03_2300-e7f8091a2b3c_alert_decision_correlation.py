from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "e7f8091a2b3c"
down_revision: str | None = "d6e7f8091a2b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "ux_decision_record_trigger_event_id"
FK_NAME = "fk_decision_record_trigger_event_id_trigger_event"


def upgrade() -> None:
    if context.is_offline_mode():
        op.add_column(
            "decision_record",
            sa.Column("trigger_event_id", sa.Uuid(), nullable=True),
        )
        if op.get_context().dialect.name != "sqlite":
            op.create_foreign_key(
                FK_NAME,
                "decision_record",
                "trigger_event",
                ["trigger_event_id"],
                ["id"],
                ondelete="SET NULL",
            )
        op.create_index(
            INDEX_NAME,
            "decision_record",
            ["trigger_event_id"],
            unique=True,
        )
        return

    with op.batch_alter_table("decision_record") as batch:
        batch.add_column(
            sa.Column("trigger_event_id", sa.Uuid(), nullable=True),
        )
        batch.create_foreign_key(
            FK_NAME,
            "trigger_event",
            ["trigger_event_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index(
            INDEX_NAME,
            ["trigger_event_id"],
            unique=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("decision_record") as batch:
        batch.drop_index(INDEX_NAME)
        batch.drop_constraint(FK_NAME, type_="foreignkey")
        batch.drop_column("trigger_event_id")
