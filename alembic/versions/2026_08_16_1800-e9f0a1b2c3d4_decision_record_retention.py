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

_V1_SCHEMA = "healthmes.decision-private.v1"
_V2_SCHEMA = "healthmes.decision-private.v2"


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


def _legacy_payload_predicate() -> str:
    dialect = context.get_context().dialect.name
    if dialect == "postgresql":
        schema_value = "decision_payload ->> 'schema'"
    elif dialect == "sqlite":
        schema_value = "json_extract(decision_payload, '$.schema')"
    else:
        raise RuntimeError(
            "decision retention migration supports only sqlite and postgresql"
        )
    return f"{schema_value} IN ('{_V1_SCHEMA}', '{_V2_SCHEMA}')"


def _delete_legacy_private_payloads() -> None:
    predicate = _legacy_payload_predicate()
    legacy_ids = (
        "SELECT id FROM decision_record "
        "WHERE decision_request_id IS NOT NULL "
        f"AND {predicate}"
    )
    op.execute(
        sa.text(
            "UPDATE schedule_proposal "
            "SET decision_record_id = NULL "
            f"WHERE decision_record_id IN ({legacy_ids})"
        )
    )
    for column in (
        "proposal_decision_record_id",
        "outcome_decision_record_id",
    ):
        op.execute(
            sa.text(
                "UPDATE calendar_mutation_proposal "
                f"SET {column} = NULL "
                f"WHERE {column} IN ({legacy_ids})"
            )
        )
    op.execute(
        sa.text(
            "DELETE FROM decision_record "
            "WHERE decision_request_id IS NOT NULL "
            f"AND {predicate}"
        )
    )


def _assert_downgrade_is_safe() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "offline downgrade cannot verify decision retention; "
            "run the downgrade online"
        )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "LOCK TABLE retention_policy, decision_record "
                "IN ACCESS EXCLUSIVE MODE"
            )
        )
    elif bind.dialect.name == "sqlite":
        # Acquire SQLite's database-wide writer lock before checking.
        bind.execute(
            sa.text(
                "UPDATE retention_policy "
                "SET enabled = enabled "
                "WHERE 0"
            )
        )

    finite_policy = bind.execute(
        sa.text(
            "SELECT 1 FROM retention_policy "
            "WHERE data_class = 'decision' "
            "AND enabled "
            "AND retention_days IS NOT NULL "
            "LIMIT 1"
        )
    ).first()
    finite_record = bind.execute(
        sa.text(
            "SELECT 1 FROM decision_record "
            "WHERE expires_at IS NOT NULL "
            "LIMIT 1"
        )
    ).first()
    if finite_policy is not None or finite_record is not None:
        raise RuntimeError(
            "cannot downgrade decision retention while a finite decision "
            "policy or finite-retention DecisionRecord exists"
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
    _delete_legacy_private_payloads()
    op.execute(
        sa.text(
            "UPDATE decision_record "
            "SET retention_basis_at = created_at, "
            f"expires_at = {_decision_expiry_expression()} "
            "WHERE decision_request_id IS NOT NULL"
        )
    )


def downgrade() -> None:
    _assert_downgrade_is_safe()
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
