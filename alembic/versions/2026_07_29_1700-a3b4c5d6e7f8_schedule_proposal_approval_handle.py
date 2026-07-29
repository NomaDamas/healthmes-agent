from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "a3b4c5d6e7f8"
down_revision: str | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if context.is_offline_mode():
        op.add_column(
            "schedule_proposal",
            sa.Column("reply_handle_digest", sa.String(length=255), nullable=True),
        )
        op.add_column(
            "schedule_proposal",
            sa.Column("expires_at", sa.DateTime(), nullable=True),
        )
        op.create_index(
            op.f("ix_schedule_proposal_expires_at"),
            "schedule_proposal",
            ["expires_at"],
            unique=False,
        )
    else:
        with op.batch_alter_table("schedule_proposal") as batch:
            batch.add_column(sa.Column("reply_handle_digest", sa.String(length=255), nullable=True))
            batch.add_column(sa.Column("expires_at", sa.DateTime(), nullable=True))
            batch.create_index(
                op.f("ix_schedule_proposal_expires_at"),
                ["expires_at"],
                unique=False,
            )


def downgrade() -> None:
    if context.is_offline_mode():
        op.drop_index(
            op.f("ix_schedule_proposal_expires_at"),
            table_name="schedule_proposal",
        )
        op.drop_column("schedule_proposal", "expires_at")
        op.drop_column("schedule_proposal", "reply_handle_digest")
    else:
        with op.batch_alter_table("schedule_proposal") as batch:
            batch.drop_index(op.f("ix_schedule_proposal_expires_at"))
            batch.drop_column("expires_at")
            batch.drop_column("reply_handle_digest")
