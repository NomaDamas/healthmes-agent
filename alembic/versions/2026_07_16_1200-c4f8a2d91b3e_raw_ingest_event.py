"""raw_ingest_event — append-only index of the raw-first ingest store

PLAN §13 (owner decision 2026-07-16): payloads accepted by /v1/ingest/* are
written verbatim to HEALTHMES_DATA_DIR/raw_ingest/ before any parsing; this
table records where each landed and what best-effort interpretation did.
Types follow the initial-schema portability rules (postgres + sqlite).

Revision ID: c4f8a2d91b3e
Revises: 65812fe515fa
Create Date: 2026-07-16 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4f8a2d91b3e"
down_revision: str | None = "65812fe515fa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "raw_ingest_event",
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
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("path", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("parse_status", sa.String(length=32), nullable=False),
        sa.Column("forward_status", sa.String(length=32), nullable=False),
        sa.Column("forward_detail", sa.String(length=255), nullable=True),
        sa.Column("records_forwarded", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_raw_ingest_event")),
    )
    op.create_index(
        op.f("ix_raw_ingest_event_received_at"),
        "raw_ingest_event",
        ["received_at"],
    )
    op.create_index(op.f("ix_raw_ingest_event_source"), "raw_ingest_event", ["source"])
    op.create_index(op.f("ix_raw_ingest_event_sha256"), "raw_ingest_event", ["sha256"])


def downgrade() -> None:
    op.drop_index(op.f("ix_raw_ingest_event_sha256"), table_name="raw_ingest_event")
    op.drop_index(op.f("ix_raw_ingest_event_source"), table_name="raw_ingest_event")
    op.drop_index(op.f("ix_raw_ingest_event_received_at"), table_name="raw_ingest_event")
    op.drop_table("raw_ingest_event")
