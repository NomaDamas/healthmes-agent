from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision: str = "d6e7f8091a2b"
down_revision: str | None = "c5d6e7f8091a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    if context.is_offline_mode():
        return

    inspector = sa.inspect(op.get_bind())
    mirror_indexes = {
        item["name"]
        for item in inspector.get_indexes("calendar_event_mirror")
    }
    legacy_index = "ux_calendar_event_mirror_source_healthmes_source_key"
    identity_index = "ux_calendar_event_mirror_calendar_identity"
    cleanup_index = "ix_calendar_event_mirror_actual_sleep_cleanup"

    if legacy_index in mirror_indexes:
        op.drop_index(legacy_index, table_name="calendar_event_mirror")
    if identity_index not in mirror_indexes:
        op.create_index(
            identity_index,
            "calendar_event_mirror",
            [
                "calendar_source",
                "healthmes_kind",
                "healthmes_source",
                "healthmes_source_key",
            ],
            unique=True,
        )
    if cleanup_index not in mirror_indexes:
        op.create_index(
            cleanup_index,
            "calendar_event_mirror",
            ["calendar_source", "healthmes_kind", "sleep_local_date"],
            unique=False,
        )

    if inspector.has_table("sleep_reconciliation_proposal"):
        return
    op.create_table(
        "sleep_reconciliation_proposal",
        sa.Column("calendar_source", sa.String(length=32), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("observation_fingerprint", sa.String(length=255), nullable=False),
        sa.Column("snapshot", JSONB, nullable=False),
        sa.Column("provider_state", JSONB, nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("receipt", JSONB, nullable=True),
        sa.Column("dedup_key", sa.String(length=255), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_sleep_reconciliation_proposal",
        ),
        sa.UniqueConstraint(
            "dedup_key",
            name="uq_sleep_reconciliation_proposal_dedup_key",
        ),
    )
    for column in ("calendar_source", "local_date", "status", "expires_at"):
        op.create_index(
            op.f(f"ix_sleep_reconciliation_proposal_{column}"),
            "sleep_reconciliation_proposal",
            [column],
        )


def downgrade() -> None:
    # Fresh installations own these artifacts in f2/a3; their downgrades
    # remove them. This bridge only repairs databases stamped by the older
    # feature branch that reused those revision IDs.
    pass
