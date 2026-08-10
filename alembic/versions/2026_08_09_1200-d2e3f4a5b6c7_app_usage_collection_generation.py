"""segment Android usage rows by collection generation

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-08-09 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "d2e3f4a5b6c7"
down_revision: str | Sequence[str] | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _app_usage_table(*, include_generation: bool) -> sa.Table:
    """Frozen table shape for SQLite batch migrations and offline SQL."""
    metadata = sa.MetaData()
    columns: list[sa.SchemaItem] = [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.String(length=64), nullable=False),
    ]
    if include_generation:
        columns.append(
            sa.Column(
                "collection_generation",
                sa.BigInteger(),
                server_default="0",
                nullable=False,
            )
        )
    columns.extend(
        [
            sa.Column(
                "bucket_start",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column("app_package", sa.String(length=255), nullable=False),
            sa.Column("foreground_seconds", sa.Integer(), nullable=False),
            sa.Column("launches", sa.Integer(), nullable=False),
            sa.Column("category", sa.String(length=64), nullable=True),
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
            sa.PrimaryKeyConstraint("id", name="pk_app_usage_sample"),
        ]
    )
    unique_columns = ["device_id"]
    if include_generation:
        unique_columns.append("collection_generation")
    unique_columns.extend(["bucket_start", "app_package"])
    columns.append(
        sa.UniqueConstraint(
            *unique_columns,
            name=(
                "uq_app_usage_sample_device_generation_bucket_app"
                if include_generation
                else "uq_app_usage_sample_device_bucket_app"
            ),
        )
    )
    table = sa.Table("app_usage_sample", metadata, *columns)
    sa.Index("ix_app_usage_sample_bucket_start", table.c.bucket_start)
    return table


def _assert_downgrade_is_lossless() -> None:
    if context.is_offline_mode():
        return
    duplicate = op.get_bind().execute(
        sa.text(
            """
            SELECT 1
            FROM app_usage_sample
            GROUP BY device_id, bucket_start, app_package
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "cannot downgrade app_usage_sample without losing collection generations"
        )


def upgrade() -> None:
    with op.batch_alter_table(
        "app_usage_sample",
        copy_from=_app_usage_table(include_generation=False),
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "collection_generation",
                sa.BigInteger(),
                server_default="0",
                nullable=False,
            )
        )
        batch_op.drop_constraint(
            "uq_app_usage_sample_device_bucket_app",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_app_usage_sample_device_generation_bucket_app",
            [
                "device_id",
                "collection_generation",
                "bucket_start",
                "app_package",
            ],
        )


def downgrade() -> None:
    _assert_downgrade_is_lossless()
    with op.batch_alter_table(
        "app_usage_sample",
        copy_from=_app_usage_table(include_generation=True),
    ) as batch_op:
        batch_op.drop_constraint(
            "uq_app_usage_sample_device_generation_bucket_app",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_app_usage_sample_device_bucket_app",
            ["device_id", "bucket_start", "app_package"],
        )
        batch_op.drop_column("collection_generation")
