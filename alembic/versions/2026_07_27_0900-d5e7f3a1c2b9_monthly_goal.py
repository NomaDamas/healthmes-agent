"""monthly_goal — the goal layer above weekly goals (PLAN §14)

Owner requirement 2026-07-27: planning must see the month, not only the
week. Adds the ``monthly_goal`` table (same lean shape as ``weekly_goal``)
and a nullable ``weekly_goal.monthly_goal_id`` link (SET NULL on delete —
losing the month never deletes the week's goals). Types follow the
initial-schema portability rules (postgres + sqlite).

Revision ID: d5e7f3a1c2b9
Revises: c4f8a2d91b3e
Create Date: 2026-07-27 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "d5e7f3a1c2b9"
down_revision: str | None = "c4f8a2d91b3e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "monthly_goal",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("month_start", sa.Date(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monthly_goal")),
    )
    op.create_index(op.f("ix_monthly_goal_month_start"), "monthly_goal", ["month_start"])

    # Two paths, one result:
    # - offline (--sql) render has no connection to reflect against, so emit a
    #   plain ADD COLUMN (+ named FK on dialects that can ALTER ADD CONSTRAINT);
    # - live runs use batch mode, which on sqlite rebuilds the table via
    #   reflection so the FK really lands and metadata compare sees no drift.
    # Live runs use batch mode (sqlite rebuilds via reflection and lands the
    # named FK; postgres emits plain ALTERs). Offline sqlite scripts emit a
    # bare ADD COLUMN instead: a reflection-free copy_from rebuild proved
    # unable to reproduce the named PK/index faithfully (review 2026-07-27),
    # and sqlite does not enforce ALTER-added FKs anyway — the documented
    # offline-sqlite gap is the FK constraint only, never data. Offline
    # postgres keeps the named FK.
    if context.is_offline_mode():
        op.add_column(
            "weekly_goal", sa.Column("monthly_goal_id", sa.Uuid(), nullable=True)
        )
        if op.get_context().dialect.name != "sqlite":
            op.create_foreign_key(
                op.f("fk_weekly_goal_monthly_goal_id_monthly_goal"),
                "weekly_goal",
                "monthly_goal",
                ["monthly_goal_id"],
                ["id"],
                ondelete="SET NULL",
            )
    else:
        with op.batch_alter_table("weekly_goal") as batch:
            batch.add_column(sa.Column("monthly_goal_id", sa.Uuid(), nullable=True))
            batch.create_foreign_key(
                op.f("fk_weekly_goal_monthly_goal_id_monthly_goal"),
                "monthly_goal",
                ["monthly_goal_id"],
                ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    if context.is_offline_mode():
        if op.get_context().dialect.name != "sqlite":
            op.drop_constraint(
                op.f("fk_weekly_goal_monthly_goal_id_monthly_goal"),
                "weekly_goal",
                type_="foreignkey",
            )
        op.drop_column("weekly_goal", "monthly_goal_id")
    else:
        with op.batch_alter_table("weekly_goal") as batch:
            batch.drop_constraint(
                op.f("fk_weekly_goal_monthly_goal_id_monthly_goal"), type_="foreignkey"
            )
            batch.drop_column("monthly_goal_id")
    # NOTE: shared drops below run in BOTH offline and live paths — an early
    # return here once left monthly_goal behind (review).
    op.drop_index(op.f("ix_monthly_goal_month_start"), table_name="monthly_goal")
    op.drop_table("monthly_goal")
