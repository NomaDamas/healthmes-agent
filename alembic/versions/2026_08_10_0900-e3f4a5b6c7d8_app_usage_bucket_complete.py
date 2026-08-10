"""persist Android app-usage snapshot state

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-08-10 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "e3f4a5b6c7d8"
down_revision: str | Sequence[str] | None = "d2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _assert_downgrade_is_lossless() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "offline downgrade cannot verify Android snapshot state; "
            "run the downgrade online"
        )
    connection = op.get_bind()
    non_default_snapshot = connection.execute(
        sa.text(
            """
            SELECT 1
            FROM app_usage_sample
            WHERE bucket_complete = true
               OR snapshot_sequence <> 0
            LIMIT 1
            """
        )
    ).first()
    snapshot_event = connection.execute(
        sa.text(
            """
            SELECT 1
            FROM wellness_event
            WHERE event_type = 'activity.android-bucket-snapshot.v1'
            LIMIT 1
            """
        )
    ).first()
    if non_default_snapshot is not None or snapshot_event is not None:
        raise RuntimeError(
            "cannot downgrade app_usage_sample without losing Android snapshot state"
        )


def upgrade() -> None:
    op.add_column(
        "app_usage_sample",
        sa.Column(
            "bucket_complete",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        )
    )
    op.add_column(
        "app_usage_sample",
        sa.Column(
            "snapshot_sequence",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
    )


def downgrade() -> None:
    _assert_downgrade_is_lossless()
    op.drop_column("app_usage_sample", "snapshot_sequence")
    op.drop_column("app_usage_sample", "bucket_complete")
