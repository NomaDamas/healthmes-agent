"""normalize the Decision Agent domain policy revision constraint

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-08-21 13:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "e5f6a7b8c9d0"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "decision_domain_policy"
_CANONICAL = "ck_decision_domain_policy_revision_positive"
_LEGACY = (
    "ck_decision_domain_policy_"
    "ck_decision_domain_policy_revision_positive"
)
# PostgreSQL truncates identifiers longer than 63 bytes using SQLAlchemy's
# deterministic four-character hash suffix.
_LEGACY_POSTGRES = (
    "ck_decision_domain_policy_ck_decision_domain_policy_rev_2495"
)
_EXPRESSION = "revision >= 1"


def _policy_table(*, check_name: str) -> sa.Table:
    """Return the frozen d4 policy shape for SQLite batch DDL."""

    metadata = sa.MetaData()
    table = sa.Table(
        _TABLE,
        metadata,
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
            _EXPRESSION,
            name=check_name,
        ),
        sa.UniqueConstraint(
            "owner_principal_id",
            "domain",
            name="uq_decision_domain_policy_owner_domain",
        ),
    )
    sa.Index(
        "ix_decision_domain_policy_owner_principal_id",
        table.c.owner_principal_id,
    )
    sa.Index(
        "ix_decision_domain_policy_domain",
        table.c.domain,
    )
    return table


def _sqlite_replace_constraint(
    *,
    source_name: str,
    target_name: str,
) -> None:
    bind = op.get_bind()
    if not context.is_offline_mode():
        # Pysqlite does not physically BEGIN for DDL. Reserve the writer so
        # this rebuild and any later revision in the Alembic command roll back
        # as one transaction.
        bind.execute(
            sa.text(
                "UPDATE decision_domain_policy "
                "SET revision = revision "
                "WHERE 0"
            )
        )
    with op.batch_alter_table(
        _TABLE,
        copy_from=_policy_table(check_name=source_name),
        recreate="always",
    ) as batch:
        batch.drop_constraint(
            op.f(source_name),
            type_="check",
        )
        batch.create_check_constraint(
            op.f(target_name),
            _EXPRESSION,
        )


def _postgres_rename_constraint(
    *,
    source_name: str,
    target_name: str,
) -> None:
    preparer = op.get_bind().dialect.identifier_preparer
    table = preparer.quote(_TABLE)
    source = preparer.quote(source_name)
    target = preparer.quote(target_name)
    op.execute(
        sa.text(
            f"ALTER TABLE {table} "
            f"RENAME CONSTRAINT {source} TO {target}"
        )
    )


def _online_constraint_names(bind: sa.Connection) -> set[str]:
    return {
        str(item["name"])
        for item in sa.inspect(bind).get_check_constraints(_TABLE)
        if item["name"] is not None
    }


def _upgrade_online(bind: sa.Connection) -> None:
    names = _online_constraint_names(bind)
    if _CANONICAL in names:
        return
    if bind.dialect.name == "sqlite":
        if _LEGACY not in names:
            raise RuntimeError(
                "decision_domain_policy has neither the published legacy "
                "nor canonical revision constraint"
            )
        _sqlite_replace_constraint(
            source_name=_LEGACY,
            target_name=_CANONICAL,
        )
        return
    if bind.dialect.name == "postgresql":
        legacy_name = next(
            (
                name
                for name in (_LEGACY_POSTGRES, _LEGACY)
                if name in names
            ),
            None,
        )
        if legacy_name is None:
            raise RuntimeError(
                "decision_domain_policy has neither the published legacy "
                "nor canonical revision constraint"
            )
        _postgres_rename_constraint(
            source_name=legacy_name,
            target_name=_CANONICAL,
        )
        return
    raise RuntimeError(
        "decision domain policy constraint normalization supports only "
        "SQLite and PostgreSQL"
    )


def _downgrade_online(bind: sa.Connection) -> None:
    names = _online_constraint_names(bind)
    if bind.dialect.name == "sqlite":
        if _LEGACY in names and _CANONICAL not in names:
            return
        if _CANONICAL not in names:
            raise RuntimeError(
                "decision_domain_policy has neither the canonical nor "
                "published legacy revision constraint"
            )
        _sqlite_replace_constraint(
            source_name=_CANONICAL,
            target_name=_LEGACY,
        )
        return
    if bind.dialect.name == "postgresql":
        if _LEGACY_POSTGRES in names and _CANONICAL not in names:
            return
        if _CANONICAL not in names:
            raise RuntimeError(
                "decision_domain_policy has neither the canonical nor "
                "published legacy revision constraint"
            )
        _postgres_rename_constraint(
            source_name=_CANONICAL,
            target_name=_LEGACY_POSTGRES,
        )
        return
    raise RuntimeError(
        "decision domain policy constraint normalization supports only "
        "SQLite and PostgreSQL"
    )


def upgrade() -> None:
    bind = op.get_bind()
    if context.is_offline_mode():
        if bind.dialect.name == "sqlite":
            _sqlite_replace_constraint(
                source_name=_LEGACY,
                target_name=_CANONICAL,
            )
            return
        if bind.dialect.name == "postgresql":
            _postgres_rename_constraint(
                source_name=_LEGACY_POSTGRES,
                target_name=_CANONICAL,
            )
            return
        raise RuntimeError(
            "decision domain policy constraint normalization supports only "
            "SQLite and PostgreSQL"
        )
    _upgrade_online(bind)


def downgrade() -> None:
    bind = op.get_bind()
    if context.is_offline_mode():
        if bind.dialect.name == "sqlite":
            _sqlite_replace_constraint(
                source_name=_CANONICAL,
                target_name=_LEGACY,
            )
            return
        if bind.dialect.name == "postgresql":
            _postgres_rename_constraint(
                source_name=_CANONICAL,
                target_name=_LEGACY_POSTGRES,
            )
            return
        raise RuntimeError(
            "decision domain policy constraint normalization supports only "
            "SQLite and PostgreSQL"
        )
    _downgrade_online(bind)
