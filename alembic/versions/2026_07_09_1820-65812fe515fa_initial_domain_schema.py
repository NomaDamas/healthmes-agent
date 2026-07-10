"""initial domain schema

All 11 healthmes domain tables (docs/PLAN.md §2). Types are deliberately
portable so the same migration runs on postgres (full stack) and sqlite
(zero-setup mac-native path):

- ``JSONB`` variant: native JSONB on postgres, plain JSON on sqlite.
- ``sa.Uuid()``: native UUID on postgres, CHAR(32) on sqlite.
- Enums: plain VARCHAR (``native_enum=False``), values stored as strings.
- ``server_default=sa.func.now()`` compiles per-dialect
  (``now()`` / ``CURRENT_TIMESTAMP``).

Revision ID: 65812fe515fa
Revises:
Create Date: 2026-07-09 18:20:04.603573

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "65812fe515fa"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Inlined (not imported from healthmes.store.base) so this migration stays a
# frozen snapshot even if the application-level type aliases evolve.
JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _base_columns() -> list[sa.Column]:
    """id/created_at/updated_at shared by every table (healthmes.store.base.Base)."""
    return [
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
    ]


def upgrade() -> None:
    id_col, created_at, updated_at = _base_columns()
    op.create_table(
        "weekly_goal",
        id_col,
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        created_at,
        updated_at,
        sa.PrimaryKeyConstraint("id", name=op.f("pk_weekly_goal")),
    )
    op.create_index(op.f("ix_weekly_goal_week_start"), "weekly_goal", ["week_start"])

    id_col, created_at, updated_at = _base_columns()
    op.create_table(
        "task",
        id_col,
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("goal_id", sa.Uuid(), nullable=True),
        sa.Column("est_minutes", sa.Integer(), nullable=True),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "energy_demand",
            sa.Enum("low", "med", "high", name="energydemand", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "source",
            sa.Enum("user", "agent", name="tasksource", native_enum=False, length=32),
            nullable=False,
        ),
        created_at,
        updated_at,
        sa.ForeignKeyConstraint(
            ["goal_id"],
            ["weekly_goal.id"],
            name=op.f("fk_task_goal_id_weekly_goal"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task")),
    )
    op.create_index(op.f("ix_task_deadline"), "task", ["deadline"])
    op.create_index(op.f("ix_task_goal_id"), "task", ["goal_id"])

    id_col, created_at, updated_at = _base_columns()
    op.create_table(
        "decision_record",
        id_col,
        sa.Column(
            "kind",
            sa.Enum(
                "schedule_change",
                "alert",
                "insight",
                "capture",
                name="decisionkind",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("tree", JSONB, nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("llm_model", sa.String(length=64), nullable=True),
        sa.Column("tokens", sa.Integer(), nullable=True),
        created_at,
        updated_at,
        sa.PrimaryKeyConstraint("id", name=op.f("pk_decision_record")),
    )
    op.create_index(op.f("ix_decision_record_kind"), "decision_record", ["kind"])

    id_col, created_at, updated_at = _base_columns()
    op.create_table(
        "calendar_event_mirror",
        id_col,
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column(
            "calendar_source",
            sa.Enum("google", "caldav", name="calendarsource", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_agent_created", sa.Boolean(), nullable=False),
        sa.Column("agent_task_id", sa.Uuid(), nullable=True),
        sa.Column("etag", sa.String(length=255), nullable=True),
        sa.Column("sync_token", sa.String(length=255), nullable=True),
        created_at,
        updated_at,
        sa.ForeignKeyConstraint(
            ["agent_task_id"],
            ["task.id"],
            name=op.f("fk_calendar_event_mirror_agent_task_id_task"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calendar_event_mirror")),
        sa.UniqueConstraint(
            "calendar_source",
            "external_id",
            name="uq_calendar_event_mirror_source_external_id",
        ),
    )
    op.create_index(
        op.f("ix_calendar_event_mirror_agent_task_id"),
        "calendar_event_mirror",
        ["agent_task_id"],
    )
    op.create_index(
        op.f("ix_calendar_event_mirror_start_at"), "calendar_event_mirror", ["start_at"]
    )

    id_col, created_at, updated_at = _base_columns()
    op.create_table(
        "schedule_proposal",
        id_col,
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("proposed_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("proposed_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "proposed",
                "accepted",
                "pushed",
                "declined",
                name="proposalstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("decision_record_id", sa.Uuid(), nullable=True),
        created_at,
        updated_at,
        sa.ForeignKeyConstraint(
            ["decision_record_id"],
            ["decision_record.id"],
            name=op.f("fk_schedule_proposal_decision_record_id_decision_record"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["task.id"],
            name=op.f("fk_schedule_proposal_task_id_task"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schedule_proposal")),
    )
    op.create_index(op.f("ix_schedule_proposal_status"), "schedule_proposal", ["status"])
    op.create_index(op.f("ix_schedule_proposal_task_id"), "schedule_proposal", ["task_id"])

    id_col, created_at, updated_at = _base_columns()
    op.create_table(
        "food_log",
        id_col,
        sa.Column("logged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("media_path", sa.Text(), nullable=True),
        sa.Column("meal_type", sa.String(length=32), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=True),
        created_at,
        updated_at,
        sa.PrimaryKeyConstraint("id", name=op.f("pk_food_log")),
    )
    op.create_index(op.f("ix_food_log_logged_at"), "food_log", ["logged_at"])

    id_col, created_at, updated_at = _base_columns()
    op.create_table(
        "app_usage_sample",
        id_col,
        sa.Column("device_id", sa.String(length=64), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("app_package", sa.String(length=255), nullable=False),
        sa.Column("foreground_seconds", sa.Integer(), nullable=False),
        sa.Column("launches", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=True),
        created_at,
        updated_at,
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_usage_sample")),
        sa.UniqueConstraint(
            "device_id",
            "bucket_start",
            "app_package",
            name="uq_app_usage_sample_device_bucket_app",
        ),
    )
    op.create_index(op.f("ix_app_usage_sample_bucket_start"), "app_usage_sample", ["bucket_start"])

    id_col, created_at, updated_at = _base_columns()
    op.create_table(
        "cognitive_energy_estimate",
        id_col,
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("components", JSONB, nullable=False),
        sa.Column("inputs_snapshot", JSONB, nullable=True),
        created_at,
        updated_at,
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cognitive_energy_estimate")),
    )
    op.create_index(
        op.f("ix_cognitive_energy_estimate_window_start"),
        "cognitive_energy_estimate",
        ["window_start"],
    )

    id_col, created_at, updated_at = _base_columns()
    op.create_table(
        "insight",
        id_col,
        sa.Column("period", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("evidence", JSONB, nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        created_at,
        updated_at,
        sa.PrimaryKeyConstraint("id", name=op.f("pk_insight")),
    )
    op.create_index(op.f("ix_insight_period"), "insight", ["period"])

    id_col, created_at, updated_at = _base_columns()
    op.create_table(
        "medical_record",
        id_col,
        sa.Column(
            "kind",
            sa.Enum(
                "medication", "symptom", name="medicalrecordkind", native_enum=False, length=32
            ),
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("media_path", sa.Text(), nullable=True),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("context", JSONB, nullable=True),
        created_at,
        updated_at,
        sa.PrimaryKeyConstraint("id", name=op.f("pk_medical_record")),
    )

    id_col, created_at, updated_at = _base_columns()
    op.create_table(
        "trigger_event",
        id_col,
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rule_id", sa.String(length=64), nullable=False),
        sa.Column("payload", JSONB, nullable=True),
        sa.Column("alert_sent", sa.Boolean(), nullable=False),
        sa.Column("dedup_key", sa.String(length=255), nullable=True),
        created_at,
        updated_at,
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trigger_event")),
    )
    op.create_index(op.f("ix_trigger_event_dedup_key"), "trigger_event", ["dedup_key"])
    op.create_index(op.f("ix_trigger_event_fired_at"), "trigger_event", ["fired_at"])
    op.create_index(op.f("ix_trigger_event_rule_id"), "trigger_event", ["rule_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_trigger_event_rule_id"), table_name="trigger_event")
    op.drop_index(op.f("ix_trigger_event_fired_at"), table_name="trigger_event")
    op.drop_index(op.f("ix_trigger_event_dedup_key"), table_name="trigger_event")
    op.drop_table("trigger_event")
    op.drop_table("medical_record")
    op.drop_index(op.f("ix_insight_period"), table_name="insight")
    op.drop_table("insight")
    op.drop_index(
        op.f("ix_cognitive_energy_estimate_window_start"),
        table_name="cognitive_energy_estimate",
    )
    op.drop_table("cognitive_energy_estimate")
    op.drop_index(op.f("ix_app_usage_sample_bucket_start"), table_name="app_usage_sample")
    op.drop_table("app_usage_sample")
    op.drop_index(op.f("ix_food_log_logged_at"), table_name="food_log")
    op.drop_table("food_log")
    op.drop_index(op.f("ix_schedule_proposal_task_id"), table_name="schedule_proposal")
    op.drop_index(op.f("ix_schedule_proposal_status"), table_name="schedule_proposal")
    op.drop_table("schedule_proposal")
    op.drop_index(op.f("ix_calendar_event_mirror_start_at"), table_name="calendar_event_mirror")
    op.drop_index(
        op.f("ix_calendar_event_mirror_agent_task_id"), table_name="calendar_event_mirror"
    )
    op.drop_table("calendar_event_mirror")
    op.drop_index(op.f("ix_decision_record_kind"), table_name="decision_record")
    op.drop_table("decision_record")
    op.drop_index(op.f("ix_task_goal_id"), table_name="task")
    op.drop_index(op.f("ix_task_deadline"), table_name="task")
    op.drop_table("task")
    op.drop_index(op.f("ix_weekly_goal_week_start"), table_name="weekly_goal")
    op.drop_table("weekly_goal")
