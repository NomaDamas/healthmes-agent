"""scope calendar mirror rows to one connected account generation

Revision ID: b6c7d8e9f0a1
Revises: a5b6c7d8e9f0
Create Date: 2026-08-12 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import context, op
from healthmes.store.migration_safety import (
    acquire_postgres_downgrade_lock,
)

revision: str = "b6c7d8e9f0a1"
down_revision: str | Sequence[str] | None = "a5b6c7d8e9f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_ACCOUNT_GENERATION = "__legacy_unbound__"
ACTIVE_PROPOSAL_CHECK = "active_generation"
ACTIVE_PROPOSAL_EXPRESSION = (
    "status NOT IN ('pending', 'applying') "
    "OR (account_generation IS NOT NULL "
    f"AND account_generation <> '{LEGACY_ACCOUNT_GENERATION}')"
)
JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _calendar_source_enum() -> sa.Enum:
    return sa.Enum(
        "google",
        "caldav",
        name="calendarsource",
        native_enum=False,
        length=32,
    )


def _calendar_mutation_operation_enum() -> sa.Enum:
    return sa.Enum(
        "shorten",
        name="calendarmutationoperation",
        native_enum=False,
        length=32,
    )


def _calendar_mutation_status_enum() -> sa.Enum:
    return sa.Enum(
        "pending",
        "applying",
        "applied",
        "applied_recovered",
        "declined",
        "expired",
        "conflicted",
        "failed",
        "failed_no_change",
        "unknown",
        name="calendarmutationstatus",
        native_enum=False,
        length=32,
    )


def _calendar_event_mirror_table(
    *,
    include_generation: bool,
) -> sa.Table:
    """Frozen pre/post revision shape for SQLite batch and offline SQL."""
    metadata = sa.MetaData()
    columns: list[sa.SchemaItem] = [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column(
            "calendar_source",
            _calendar_source_enum(),
            nullable=False,
        ),
    ]
    if include_generation:
        columns.append(
            sa.Column(
                "connection_generation",
                sa.String(length=64),
                server_default=LEGACY_ACCOUNT_GENERATION,
                nullable=False,
            )
        )
    columns.extend(
        [
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column(
                "start_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column(
                "end_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column("is_agent_created", sa.Boolean(), nullable=False),
            sa.Column("agent_task_id", sa.Uuid(), nullable=True),
            sa.Column("etag", sa.String(length=255), nullable=True),
            sa.Column("sync_token", sa.String(length=255), nullable=True),
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
            sa.Column(
                "organizer_self",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
            sa.Column(
                "has_attendees",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
            sa.Column(
                "is_recurring",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
            sa.Column("event_type", sa.String(length=64), nullable=True),
            sa.Column(
                "is_all_day",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
            sa.Column(
                "is_locked",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
            sa.Column("status", sa.String(length=32), nullable=True),
            sa.Column("healthmes_kind", sa.String(length=64), nullable=True),
            sa.Column(
                "healthmes_source",
                sa.String(length=255),
                nullable=True,
            ),
            sa.Column(
                "healthmes_source_key",
                sa.String(length=255),
                nullable=True,
            ),
            sa.Column(
                "observation_fingerprint",
                sa.String(length=255),
                nullable=True,
            ),
            sa.Column("sleep_local_date", sa.Date(), nullable=True),
            sa.Column("sleep_duration_minutes", sa.Integer(), nullable=True),
            sa.Column(
                "sleep_time_in_bed_minutes",
                sa.Integer(),
                nullable=True,
            ),
            sa.Column(
                "sleep_provider",
                sa.String(length=255),
                nullable=True,
            ),
            sa.Column("intake_task_id", sa.Uuid(), nullable=True),
            sa.Column(
                "intake_opted_out",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["agent_task_id"],
                ["task.id"],
                name=(
                    "fk_calendar_event_mirror_agent_task_id_task"
                ),
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["intake_task_id"],
                ["task.id"],
                name=(
                    "fk_calendar_event_mirror_intake_task_id_task"
                ),
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint(
                "id",
                name="pk_calendar_event_mirror",
            ),
        ]
    )
    identity = ["calendar_source"]
    if include_generation:
        identity.append("connection_generation")
    columns.append(
        sa.UniqueConstraint(
            *identity,
            "external_id",
            name=(
                "uq_calendar_event_mirror_source_generation_external_id"
                if include_generation
                else "uq_calendar_event_mirror_source_external_id"
            ),
        )
    )
    table = sa.Table("calendar_event_mirror", metadata, *columns)
    sa.Index(
        "ix_calendar_event_mirror_agent_task_id",
        table.c.agent_task_id,
    )
    sa.Index(
        "ix_calendar_event_mirror_intake_task_id",
        table.c.intake_task_id,
    )
    sa.Index(
        "ix_calendar_event_mirror_sleep_local_date",
        table.c.sleep_local_date,
    )
    sa.Index("ix_calendar_event_mirror_start_at", table.c.start_at)
    sa.Index(
        "ix_calendar_event_mirror_actual_sleep_cleanup",
        table.c.calendar_source,
        table.c.healthmes_kind,
        table.c.sleep_local_date,
    )
    identity_columns = [table.c.calendar_source]
    if include_generation:
        identity_columns.append(table.c.connection_generation)
    identity_columns.extend(
        [
            table.c.healthmes_kind,
            table.c.healthmes_source,
            table.c.healthmes_source_key,
        ]
    )
    sa.Index(
        "ux_calendar_event_mirror_calendar_identity",
        *identity_columns,
        unique=True,
    )
    if include_generation:
        sa.Index(
            "ix_calendar_event_mirror_source_connection_generation",
            table.c.calendar_source,
            table.c.connection_generation,
        )
    return table


def _calendar_mutation_proposal_table(
    *,
    include_generation: bool,
    include_active_check: bool,
) -> sa.Table:
    """Frozen proposal shape after the account-generation column is added."""
    metadata = sa.MetaData()
    columns: list[sa.SchemaItem] = [
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
        sa.Column(
            "calendar_source",
            _calendar_source_enum(),
            nullable=False,
        ),
    ]
    if include_generation:
        columns.append(
            sa.Column(
                "account_generation",
                sa.String(length=64),
                server_default=LEGACY_ACCOUNT_GENERATION,
                nullable=True,
            )
        )
    columns.extend(
        [
            sa.Column("mirror_event_id", sa.Uuid(), nullable=True),
            sa.Column(
                "external_event_id",
                sa.String(length=255),
                nullable=False,
            ),
            sa.Column(
                "operation",
                _calendar_mutation_operation_enum(),
                nullable=False,
            ),
            sa.Column(
                "original_start_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column(
                "original_end_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column(
                "proposed_start_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column(
                "proposed_end_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column(
                "expected_etag",
                sa.String(length=255),
                nullable=False,
            ),
            sa.Column(
                "protected_fingerprint",
                sa.String(length=255),
                nullable=False,
            ),
            sa.Column(
                "reply_handle_digest",
                sa.String(length=255),
                nullable=False,
            ),
            sa.Column(
                "expires_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column(
                "consumed_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "attempt_id",
                sa.String(length=64),
                nullable=True,
            ),
            sa.Column(
                "status",
                _calendar_mutation_status_enum(),
                nullable=False,
            ),
            sa.Column(
                "dedup_key",
                sa.String(length=255),
                nullable=False,
            ),
            sa.Column(
                "proposal_decision_record_id",
                sa.Uuid(),
                nullable=True,
            ),
            sa.Column(
                "outcome_decision_record_id",
                sa.Uuid(),
                nullable=True,
            ),
            sa.Column(
                "response_channel",
                sa.String(length=32),
                nullable=True,
            ),
            sa.Column("receipt", JSONB, nullable=True),
            sa.ForeignKeyConstraint(
                ["mirror_event_id"],
                ["calendar_event_mirror.id"],
                name=(
                    "fk_calendar_mutation_proposal_mirror_event_id_"
                    "calendar_event_mirror"
                ),
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["outcome_decision_record_id"],
                ["decision_record.id"],
                name=(
                    "fk_calendar_mutation_proposal_"
                    "outcome_decision_record_id_decision_record"
                ),
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["proposal_decision_record_id"],
                ["decision_record.id"],
                name=(
                    "fk_calendar_mutation_proposal_"
                    "proposal_decision_record_id_decision_record"
                ),
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint(
                "id",
                name="pk_calendar_mutation_proposal",
            ),
            sa.UniqueConstraint(
                "attempt_id",
                name="uq_calendar_mutation_proposal_attempt_id",
            ),
            sa.UniqueConstraint(
                "dedup_key",
                name="uq_calendar_mutation_proposal_dedup_key",
            ),
        ]
    )
    if include_active_check:
        columns.append(
            sa.CheckConstraint(
                ACTIVE_PROPOSAL_EXPRESSION,
                name=ACTIVE_PROPOSAL_CHECK,
            )
        )
    table = sa.Table("calendar_mutation_proposal", metadata, *columns)
    sa.Index(
        "ix_calendar_mutation_proposal_calendar_source",
        table.c.calendar_source,
    )
    sa.Index(
        "ix_calendar_mutation_proposal_mirror_event_id",
        table.c.mirror_event_id,
    )
    sa.Index(
        "ix_calendar_mutation_proposal_expires_at",
        table.c.expires_at,
    )
    sa.Index(
        "ix_calendar_mutation_proposal_attempt_id",
        table.c.attempt_id,
    )
    sa.Index(
        "ix_calendar_mutation_proposal_status",
        table.c.status,
    )
    sa.Index(
        "ix_calendar_mutation_proposal_proposal_decision_record_id",
        table.c.proposal_decision_record_id,
    )
    sa.Index(
        "ix_calendar_mutation_proposal_outcome_decision_record_id",
        table.c.outcome_decision_record_id,
    )
    return table


def upgrade() -> None:
    with op.batch_alter_table(
        "calendar_event_mirror",
        copy_from=_calendar_event_mirror_table(
            include_generation=False,
        ),
    ) as batch:
        batch.drop_constraint(
            "uq_calendar_event_mirror_source_external_id",
            type_="unique",
        )
        batch.drop_index(
            "ux_calendar_event_mirror_calendar_identity"
        )
        batch.add_column(
            sa.Column(
                "connection_generation",
                sa.String(length=64),
                server_default=LEGACY_ACCOUNT_GENERATION,
                nullable=False,
            )
        )
        batch.create_index(
            "ix_calendar_event_mirror_source_connection_generation",
            ["calendar_source", "connection_generation"],
            unique=False,
        )
        batch.create_unique_constraint(
            "uq_calendar_event_mirror_source_generation_external_id",
            [
                "calendar_source",
                "connection_generation",
                "external_id",
            ],
        )
        batch.create_index(
            "ux_calendar_event_mirror_calendar_identity",
            [
                "calendar_source",
                "connection_generation",
                "healthmes_kind",
                "healthmes_source",
                "healthmes_source_key",
            ],
            unique=True,
        )
    op.add_column(
        "calendar_mutation_proposal",
        sa.Column(
            "account_generation",
            sa.String(length=64),
            server_default=LEGACY_ACCOUNT_GENERATION,
            nullable=True,
        ),
    )
    op.execute(
        sa.text(
            "UPDATE calendar_mutation_proposal "
            "SET status = CASE "
            "WHEN status = 'pending' THEN 'conflicted' "
            "WHEN status = 'applying' THEN 'unknown' "
            "ELSE status END "
            "WHERE account_generation = :legacy_generation "
            "AND status IN ('pending', 'applying')"
        ).bindparams(legacy_generation=LEGACY_ACCOUNT_GENERATION)
    )
    with op.batch_alter_table(
        "calendar_mutation_proposal",
        copy_from=_calendar_mutation_proposal_table(
            include_generation=True,
            include_active_check=False,
        ),
    ) as batch:
        batch.create_check_constraint(
            ACTIVE_PROPOSAL_CHECK,
            ACTIVE_PROPOSAL_EXPRESSION,
        )
        batch.create_index(
            "ix_calendar_mutation_proposal_source_account_generation",
            ["calendar_source", "account_generation"],
            unique=False,
        )


def downgrade() -> None:
    _assert_account_generation_downgrade_is_safe()
    with op.batch_alter_table("calendar_mutation_proposal") as batch:
        batch.drop_constraint(
            ACTIVE_PROPOSAL_CHECK,
            type_="check",
        )
        batch.drop_index(
            "ix_calendar_mutation_proposal_source_account_generation"
        )
        batch.drop_column("account_generation")
    with op.batch_alter_table("calendar_event_mirror") as batch:
        batch.drop_index(
            "ux_calendar_event_mirror_calendar_identity"
        )
        batch.drop_constraint(
            "uq_calendar_event_mirror_source_generation_external_id",
            type_="unique",
        )
        batch.drop_index(
            "ix_calendar_event_mirror_source_connection_generation"
        )
        batch.drop_column("connection_generation")
        batch.create_unique_constraint(
            "uq_calendar_event_mirror_source_external_id",
            ["calendar_source", "external_id"],
        )
        batch.create_index(
            "ux_calendar_event_mirror_calendar_identity",
            [
                "calendar_source",
                "healthmes_kind",
                "healthmes_source",
                "healthmes_source_key",
            ],
            unique=True,
        )


def _assert_account_generation_downgrade_is_safe() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "offline downgrade cannot verify Calendar account-generation "
            "safety; run the downgrade online"
        )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        acquire_postgres_downgrade_lock(
            bind,
            "LOCK TABLE calendar_event_mirror, "
            "calendar_mutation_proposal IN ACCESS EXCLUSIVE MODE",
            resource="Calendar account-generation data",
        )
    elif bind.dialect.name == "sqlite":
        # Reserve the database before the losslessness checks. A concurrent
        # writer must complete first, after which its rows are included below.
        bind.execute(
            sa.text(
                "UPDATE calendar_event_mirror "
                "SET updated_at = updated_at WHERE 0"
            )
        )
        bind.execute(
            sa.text(
                "UPDATE calendar_mutation_proposal "
                "SET updated_at = updated_at WHERE 0"
            )
        )
    duplicate = bind.execute(
        sa.text(
            "SELECT 1 FROM calendar_event_mirror "
            "GROUP BY calendar_source, external_id "
            "HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "unsafe calendar account-generation downgrade: "
            "multiple account generations share one provider event id"
        )
    identity_duplicate = bind.execute(
        sa.text(
            "SELECT 1 FROM calendar_event_mirror "
            "WHERE healthmes_kind IS NOT NULL "
            "AND healthmes_source IS NOT NULL "
            "AND healthmes_source_key IS NOT NULL "
            "GROUP BY calendar_source, healthmes_kind, "
            "healthmes_source, healthmes_source_key "
            "HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if identity_duplicate is not None:
        raise RuntimeError(
            "unsafe calendar account-generation downgrade: "
            "multiple account generations share one HealthMes identity"
        )
    active_proposal = bind.execute(
        sa.text(
            "SELECT 1 FROM calendar_mutation_proposal "
            "WHERE status IN ('pending', 'applying') LIMIT 1"
        )
    ).first()
    if active_proposal is not None:
        raise RuntimeError(
            "unsafe calendar account-generation downgrade: "
            "calendar mutation proposals are still active"
        )
