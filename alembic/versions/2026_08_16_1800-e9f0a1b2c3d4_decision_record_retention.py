"""apply the decision retention class to Wellness DecisionRecords

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-08-16 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "e9f0a1b2c3d4"
down_revision: str | Sequence[str] | None = "d8e9f0a1b2c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _decision_expiry_expression() -> str:
    if context.get_context().dialect.name == "postgresql":
        expiry = (
            "decision_record.created_at + "
            "(retention_policy.retention_days * INTERVAL '1 day')"
        )
    else:
        expiry = (
            "datetime(decision_record.created_at, "
            "'+' || retention_policy.retention_days || ' days')"
        )
    return (
        "(SELECT CASE "
        "WHEN retention_policy.enabled "
        "AND retention_policy.retention_days IS NOT NULL "
        f"THEN {expiry} ELSE NULL END "
        "FROM retention_policy "
        "WHERE retention_policy.data_class = 'decision' "
        "LIMIT 1)"
    )


def upgrade() -> None:
    op.add_column(
        "decision_record",
        sa.Column(
            "retention_basis_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "decision_record",
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        op.f("ix_decision_record_retention_basis_at"),
        "decision_record",
        ["retention_basis_at"],
    )
    op.create_index(
        op.f("ix_decision_record_expires_at"),
        "decision_record",
        ["expires_at"],
    )
    op.execute(
        sa.text(
            "UPDATE decision_record "
            "SET retention_basis_at = created_at, "
            f"expires_at = {_decision_expiry_expression()} "
            "WHERE decision_request_id IS NOT NULL"
        )
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_decision_record_expires_at"),
        table_name="decision_record",
    )
    op.drop_index(
        op.f("ix_decision_record_retention_basis_at"),
        table_name="decision_record",
    )
    op.drop_column("decision_record", "expires_at")
    op.drop_column("decision_record", "retention_basis_at")
