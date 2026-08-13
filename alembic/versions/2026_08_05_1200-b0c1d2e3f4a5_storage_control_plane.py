"""add unified wellness storage control plane

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
Create Date: 2026-08-05 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from healthmes.store.base import JSONB

revision: str = "b0c1d2e3f4a5"
down_revision: str | Sequence[str] | None = "a9b0c1d2e3f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _common() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "retention_policy",
        *_common(),
        sa.Column("data_class", sa.String(64), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("data_class", name="uq_retention_policy_data_class"),
    )
    op.create_index(op.f("ix_retention_policy_data_class"), "retention_policy", ["data_class"])
    op.create_table(
        "storage_object",
        *_common(),
        sa.Column("data_class", sa.String(64), nullable=False),
        sa.Column("relative_path", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(255), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("retention_policy_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("safe_to_purge", sa.Boolean(), nullable=False),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["retention_policy_id"], ["retention_policy.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("relative_path", name="uq_storage_object_relative_path"),
    )
    for column in (
        "data_class",
        "sha256",
        "retention_policy_id",
        "expires_at",
        "safe_to_purge",
        "purged_at",
    ):
        op.create_index(op.f(f"ix_storage_object_{column}"), "storage_object", [column])
    op.create_table(
        "wellness_event",
        *_common(),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("source_provider", sa.String(64), nullable=False),
        sa.Column("source_device", sa.String(255), nullable=True),
        sa.Column("source_record_id", sa.String(255), nullable=False),
        sa.Column("capture_method", sa.String(32), nullable=False),
        sa.Column("quality_flags", JSONB, nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("coverage", sa.Float(), nullable=True),
        sa.Column("sensitivity", sa.String(32), nullable=False),
        sa.Column("consent_scope", sa.String(64), nullable=False),
        sa.Column("retention_policy_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("raw_object_id", sa.Uuid(), nullable=True),
        sa.Column("derived_from", JSONB, nullable=True),
        sa.ForeignKeyConstraint(["raw_object_id"], ["storage_object.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["retention_policy_id"], ["retention_policy.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_provider", "source_record_id", name="uq_wellness_event_source_record"
        ),
    )
    for column in (
        "event_type",
        "observed_at",
        "recorded_at",
        "source_provider",
        "retention_policy_id",
        "expires_at",
        "raw_object_id",
    ):
        op.create_index(op.f(f"ix_wellness_event_{column}"), "wellness_event", [column])
    op.create_table(
        "storage_usage_daily",
        *_common(),
        sa.Column("measured_on", sa.Date(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("data_class", sa.String(64), nullable=False),
        sa.Column("bytes_used", sa.Integer(), nullable=False),
        sa.Column("object_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "measured_on", "provider", "data_class", name="uq_storage_usage_daily_measurement"
        ),
    )
    op.create_index(
        op.f("ix_storage_usage_daily_measured_on"), "storage_usage_daily", ["measured_on"]
    )
    op.create_index(
        op.f("ix_storage_usage_daily_data_class"), "storage_usage_daily", ["data_class"]
    )
    op.create_table(
        "purge_job",
        *_common(),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("candidates", sa.Integer(), nullable=False),
        sa.Column("deleted", sa.Integer(), nullable=False),
        sa.Column("bytes_reclaimed", sa.Integer(), nullable=False),
        sa.Column("detail", JSONB, nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_purge_job_started_at"), "purge_job", ["started_at"])
    op.create_index(op.f("ix_purge_job_status"), "purge_job", ["status"])


def downgrade() -> None:
    for table in (
        "purge_job",
        "storage_usage_daily",
        "wellness_event",
        "storage_object",
        "retention_policy",
    ):
        op.drop_table(table)
