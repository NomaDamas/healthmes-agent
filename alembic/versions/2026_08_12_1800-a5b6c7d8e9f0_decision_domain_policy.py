"""add Decision Agent domain consent policy

Revision ID: a5b6c7d8e9f0
Revises: f4a5b6c7d8e9
Create Date: 2026-08-12 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op
from healthmes.store.migration_safety import (
    acquire_postgres_downgrade_lock,
)

revision: str = "a5b6c7d8e9f0"
down_revision: str | Sequence[str] | None = "f4a5b6c7d8e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _assert_downgrade_is_lossless() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "offline downgrade cannot verify Decision Agent domain consent; "
            "run the downgrade online"
        )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Keep the consent check and table drop in one protected migration
        # transaction. Otherwise a concurrent settings request could revoke a
        # domain after the check and have that choice erased by the downgrade.
        acquire_postgres_downgrade_lock(
            bind,
            "LOCK TABLE decision_domain_policy IN ACCESS EXCLUSIVE MODE",
            resource="Decision Agent domain consent",
        )
    elif bind.dialect.name == "sqlite":
        # SQLAlchemy defers SQLite BEGIN until the first write. Reserve the
        # database before checking consent so a concurrent settings request
        # cannot revoke a domain between this SELECT and the table drop.
        bind.execute(
            sa.text(
                "UPDATE decision_domain_policy "
                "SET revision = revision "
                "WHERE 0"
            )
        )
    disabled = bind.execute(
        sa.text(
            """
            SELECT 1
            FROM decision_domain_policy
            WHERE enabled = false
               OR revision <> 1
            LIMIT 1
            """
        )
    ).first()
    if disabled is not None:
        raise RuntimeError(
            "cannot downgrade decision_domain_policy without losing "
            "disabled Decision Agent domain consent or consent history"
        )


def upgrade() -> None:
    op.create_table(
        "decision_domain_policy",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "owner_principal_id",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            "domain",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "revision",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_decision_domain_policy",
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name="ck_decision_domain_policy_revision_positive",
        ),
        sa.UniqueConstraint(
            "owner_principal_id",
            "domain",
            name="uq_decision_domain_policy_owner_domain",
        ),
    )
    op.create_index(
        "ix_decision_domain_policy_owner_principal_id",
        "decision_domain_policy",
        ["owner_principal_id"],
        unique=False,
    )
    op.create_index(
        "ix_decision_domain_policy_domain",
        "decision_domain_policy",
        ["domain"],
        unique=False,
    )


def downgrade() -> None:
    _assert_downgrade_is_lossless()
    op.drop_index(
        "ix_decision_domain_policy_domain",
        table_name="decision_domain_policy",
    )
    op.drop_index(
        "ix_decision_domain_policy_owner_principal_id",
        table_name="decision_domain_policy",
    )
    op.drop_table("decision_domain_policy")
