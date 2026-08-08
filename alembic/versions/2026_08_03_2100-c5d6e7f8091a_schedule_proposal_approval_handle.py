from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "c5d6e7f8091a"
down_revision: str | None = "b4c5d6e7f809"
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
        op.create_index(
            op.f("ix_schedule_proposal_reply_handle_digest"),
            "schedule_proposal",
            ["reply_handle_digest"],
            unique=True,
        )
    else:
        inspector = sa.inspect(op.get_bind())
        proposal_columns = {
            item["name"]
            for item in inspector.get_columns("schedule_proposal")
        }
        proposal_indexes = {
            item["name"]
            for item in inspector.get_indexes("schedule_proposal")
        }
        with op.batch_alter_table("schedule_proposal") as batch:
            if "reply_handle_digest" not in proposal_columns:
                batch.add_column(
                    sa.Column(
                        "reply_handle_digest",
                        sa.String(length=255),
                        nullable=True,
                    )
                )
            if "expires_at" not in proposal_columns:
                batch.add_column(
                    sa.Column("expires_at", sa.DateTime(), nullable=True)
                )
            if op.f("ix_schedule_proposal_expires_at") not in proposal_indexes:
                batch.create_index(
                    op.f("ix_schedule_proposal_expires_at"),
                    ["expires_at"],
                    unique=False,
                )
            if (
                op.f("ix_schedule_proposal_reply_handle_digest")
                not in proposal_indexes
            ):
                batch.create_index(
                    op.f("ix_schedule_proposal_reply_handle_digest"),
                    ["reply_handle_digest"],
                    unique=True,
                )

    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            UPDATE schedule_proposal
            SET status = 'invalidated'
            WHERE status = 'proposed'
              AND proposed_start <= CURRENT_TIMESTAMP
            """
        )
        op.execute(
            """
            UPDATE schedule_proposal
            SET reply_handle_digest =
                    md5(random()::text || clock_timestamp()::text || id::text),
                expires_at = LEAST(
                    proposed_start,
                    CURRENT_TIMESTAMP + INTERVAL '1 day'
                )
            WHERE status = 'proposed'
              AND (reply_handle_digest IS NULL OR expires_at IS NULL)
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            UPDATE schedule_proposal
            SET status = 'invalidated'
            WHERE status = 'proposed'
              AND proposed_start <= CURRENT_TIMESTAMP
            """
        )
        op.execute(
            """
            UPDATE schedule_proposal
            SET reply_handle_digest = lower(hex(randomblob(32))),
                expires_at = MIN(
                    proposed_start,
                    datetime('now', '+1 day')
                )
            WHERE status = 'proposed'
              AND (reply_handle_digest IS NULL OR expires_at IS NULL)
            """
        )


def downgrade() -> None:
    if context.is_offline_mode():
        op.drop_index(
            op.f("ix_schedule_proposal_reply_handle_digest"),
            table_name="schedule_proposal",
        )
        op.drop_index(
            op.f("ix_schedule_proposal_expires_at"),
            table_name="schedule_proposal",
        )
        op.drop_column("schedule_proposal", "expires_at")
        op.drop_column("schedule_proposal", "reply_handle_digest")
    else:
        with op.batch_alter_table("schedule_proposal") as batch:
            batch.drop_index(op.f("ix_schedule_proposal_reply_handle_digest"))
            batch.drop_index(op.f("ix_schedule_proposal_expires_at"))
            batch.drop_column("expires_at")
            batch.drop_column("reply_handle_digest")
