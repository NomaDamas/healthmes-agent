from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "7dbf6b51f4c8"
down_revision: str | None = "c4f8a2d91b3e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column(
        "calendar_event_mirror",
        sa.Column(
            "organizer_self",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "calendar_event_mirror",
        sa.Column(
            "has_attendees",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "calendar_event_mirror",
        sa.Column(
            "is_recurring",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "calendar_event_mirror",
        sa.Column("event_type", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "calendar_event_mirror",
        sa.Column(
            "is_all_day",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "calendar_event_mirror",
        sa.Column(
            "is_locked",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "calendar_event_mirror",
        sa.Column("status", sa.String(length=32), nullable=True),
    )

    op.create_table(
        "calendar_mutation_proposal",
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
            sa.Enum("google", "caldav", name="calendarsource", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("mirror_event_id", sa.Uuid(), nullable=True),
        sa.Column("external_event_id", sa.String(length=255), nullable=False),
        sa.Column(
            "operation",
            sa.Enum(
                "shorten",
                name="calendarmutationoperation",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("original_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("original_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("proposed_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("proposed_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_etag", sa.String(length=255), nullable=False),
        sa.Column("protected_fingerprint", sa.String(length=255), nullable=False),
        sa.Column("reply_handle_digest", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_id", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
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
            ),
            nullable=False,
        ),
        sa.Column("dedup_key", sa.String(length=255), nullable=False),
        sa.Column("proposal_decision_record_id", sa.Uuid(), nullable=True),
        sa.Column("outcome_decision_record_id", sa.Uuid(), nullable=True),
        sa.Column("response_channel", sa.String(length=32), nullable=True),
        sa.Column("receipt", JSONB, nullable=True),
        sa.ForeignKeyConstraint(
            ["mirror_event_id"],
            ["calendar_event_mirror.id"],
            name=op.f("fk_calendar_mutation_proposal_mirror_event_id_calendar_event_mirror"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["outcome_decision_record_id"],
            ["decision_record.id"],
            name=op.f("fk_calendar_mutation_proposal_outcome_decision_record_id_decision_record"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_decision_record_id"],
            ["decision_record.id"],
            name=op.f("fk_calendar_mutation_proposal_proposal_decision_record_id_decision_record"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calendar_mutation_proposal")),
        sa.UniqueConstraint(
            "attempt_id",
            name="uq_calendar_mutation_proposal_attempt_id",
        ),
        sa.UniqueConstraint(
            "dedup_key",
            name="uq_calendar_mutation_proposal_dedup_key",
        ),
    )
    op.create_index(
        op.f("ix_calendar_mutation_proposal_calendar_source"),
        "calendar_mutation_proposal",
        ["calendar_source"],
    )
    op.create_index(
        op.f("ix_calendar_mutation_proposal_mirror_event_id"),
        "calendar_mutation_proposal",
        ["mirror_event_id"],
    )
    op.create_index(
        op.f("ix_calendar_mutation_proposal_expires_at"),
        "calendar_mutation_proposal",
        ["expires_at"],
    )
    op.create_index(
        op.f("ix_calendar_mutation_proposal_attempt_id"),
        "calendar_mutation_proposal",
        ["attempt_id"],
    )
    op.create_index(
        op.f("ix_calendar_mutation_proposal_status"),
        "calendar_mutation_proposal",
        ["status"],
    )
    op.create_index(
        op.f("ix_calendar_mutation_proposal_proposal_decision_record_id"),
        "calendar_mutation_proposal",
        ["proposal_decision_record_id"],
    )
    op.create_index(
        op.f("ix_calendar_mutation_proposal_outcome_decision_record_id"),
        "calendar_mutation_proposal",
        ["outcome_decision_record_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_calendar_mutation_proposal_outcome_decision_record_id"),
        table_name="calendar_mutation_proposal",
    )
    op.drop_index(
        op.f("ix_calendar_mutation_proposal_proposal_decision_record_id"),
        table_name="calendar_mutation_proposal",
    )
    op.drop_index(
        op.f("ix_calendar_mutation_proposal_status"),
        table_name="calendar_mutation_proposal",
    )
    op.drop_index(
        op.f("ix_calendar_mutation_proposal_attempt_id"),
        table_name="calendar_mutation_proposal",
    )
    op.drop_index(
        op.f("ix_calendar_mutation_proposal_expires_at"),
        table_name="calendar_mutation_proposal",
    )
    op.drop_index(
        op.f("ix_calendar_mutation_proposal_mirror_event_id"),
        table_name="calendar_mutation_proposal",
    )
    op.drop_index(
        op.f("ix_calendar_mutation_proposal_calendar_source"),
        table_name="calendar_mutation_proposal",
    )
    op.drop_table("calendar_mutation_proposal")

    op.drop_column("calendar_event_mirror", "status")
    op.drop_column("calendar_event_mirror", "is_locked")
    op.drop_column("calendar_event_mirror", "is_all_day")
    op.drop_column("calendar_event_mirror", "event_type")
    op.drop_column("calendar_event_mirror", "is_recurring")
    op.drop_column("calendar_event_mirror", "has_attendees")
    op.drop_column("calendar_event_mirror", "organizer_self")
