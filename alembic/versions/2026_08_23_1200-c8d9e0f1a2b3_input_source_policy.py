"""add persistent input source policy

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-08-23 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op
from healthmes.store.migration_safety import (
    acquire_postgres_downgrade_lock,
)

revision: str = "c8d9e0f1a2b3"
down_revision: str | Sequence[str] | None = "b7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "input_source_policy"
_OWNER_INDEX = "ix_input_source_policy_owner_principal_id"
_SOURCE_INDEX = "ix_input_source_policy_source_id"
_PRIMARY_KEY = "pk_input_source_policy"
_CHECK = "ck_input_source_policy_revision_positive"
_UNIQUE = "uq_input_source_policy_owner_source"
_EXPECTED_COLUMNS = {
    "id",
    "owner_principal_id",
    "source_id",
    "enabled",
    "revision",
    "created_at",
    "updated_at",
}
_EXPECTED_COLUMN_TYPES = {
    "id": sa.Uuid(),
    "owner_principal_id": sa.String(length=255),
    "source_id": sa.String(length=255),
    "enabled": sa.Boolean(),
    "revision": sa.Integer(),
    "created_at": sa.DateTime(timezone=True),
    "updated_at": sa.DateTime(timezone=True),
}
_EXPECTED_INDEXES = {
    _OWNER_INDEX: (("owner_principal_id",), False),
    _SOURCE_INDEX: (("source_id",), False),
}


def _assert_downgrade_is_lossless() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "offline downgrade cannot verify input source settings; "
            "run the downgrade online"
        )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        acquire_postgres_downgrade_lock(
            bind,
            "LOCK TABLE input_source_policy IN ACCESS EXCLUSIVE MODE",
            resource="input source settings",
        )
    elif bind.dialect.name == "sqlite":
        bind.execute(
            sa.text(
                "UPDATE input_source_policy "
                "SET revision = revision "
                "WHERE 0"
            )
        )
    meaningful = bind.execute(
        sa.text(
            """
            SELECT 1
            FROM input_source_policy
            WHERE enabled = false
               OR revision <> 1
            LIMIT 1
            """
        )
    ).first()
    if meaningful is not None:
        raise RuntimeError(
            "cannot downgrade input_source_policy without losing disabled "
            "input sources or source setting history"
        )


def _create_table() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "owner_principal_id",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.String(length=255),
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
            name="pk_input_source_policy",
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name="revision_positive",
        ),
        sa.UniqueConstraint(
            "owner_principal_id",
            "source_id",
            name="uq_input_source_policy_owner_source",
        ),
    )


def _type_signature(
    column_type: sa.types.TypeEngine[object],
    *,
    dialect: sa.engine.Dialect,
) -> str:
    return " ".join(
        str(column_type.compile(dialect=dialect)).upper().split()
    )


def _default_signature(value: object) -> str | None:
    if value is None:
        return None
    normalized = "".join(str(value).strip().casefold().split())
    while (
        len(normalized) >= 2
        and normalized.startswith("(")
        and normalized.endswith(")")
    ):
        normalized = normalized[1:-1]
    normalized = normalized.split("::", maxsplit=1)[0]
    if (
        len(normalized) >= 2
        and normalized[0] == normalized[-1]
        and normalized[0] in {"'", '"'}
    ):
        normalized = normalized[1:-1]
    if normalized in {"current_timestamp", "current_timestamp()", "now()"}:
        return "now"
    return normalized


def _validate_existing_table(bind: sa.Connection) -> set[str]:
    """Accept a table created from current ORM metadata, but no other shape."""

    inspector = sa.inspect(bind)
    columns = {
        str(column["name"]): column
        for column in inspector.get_columns(_TABLE)
    }
    if set(columns) != _EXPECTED_COLUMNS:
        raise RuntimeError(
            "existing input_source_policy has an incompatible column set"
        )
    if any(
        columns[name].get("nullable") is not False
        for name in _EXPECTED_COLUMNS
    ):
        raise RuntimeError(
            "existing input_source_policy has nullable required columns"
        )
    incompatible_types = {
        name
        for name, expected_type in _EXPECTED_COLUMN_TYPES.items()
        if _type_signature(
            columns[name]["type"],
            dialect=bind.dialect,
        )
        != _type_signature(expected_type, dialect=bind.dialect)
    }
    if incompatible_types:
        raise RuntimeError(
            "existing input_source_policy has incompatible column types"
        )
    expected_defaults = {
        "id": None,
        "owner_principal_id": None,
        "source_id": None,
        "enabled": "1"
        if bind.dialect.name == "sqlite"
        else "true",
        "revision": "1",
        "created_at": "now",
        "updated_at": "now",
    }
    if any(
        _default_signature(columns[name].get("default"))
        != expected_default
        for name, expected_default in expected_defaults.items()
    ):
        raise RuntimeError(
            "existing input_source_policy has incompatible server defaults"
        )

    primary_key = inspector.get_pk_constraint(_TABLE)
    if (
        primary_key.get("name") != _PRIMARY_KEY
        or tuple(primary_key.get("constrained_columns") or ()) != ("id",)
    ):
        raise RuntimeError(
            "existing input_source_policy has an incompatible primary key"
        )

    unique_constraints = {
        (
            str(item.get("name")),
            tuple(item.get("column_names") or ()),
        )
        for item in inspector.get_unique_constraints(_TABLE)
    }
    if unique_constraints != {
        (_UNIQUE, ("owner_principal_id", "source_id"))
    }:
        raise RuntimeError(
            "existing input_source_policy has incompatible uniqueness"
        )

    checks = {
        str(item.get("name")): " ".join(
            str(item.get("sqltext") or "").split()
        )
        for item in inspector.get_check_constraints(_TABLE)
    }
    if checks != {_CHECK: "revision >= 1"}:
        raise RuntimeError(
            "existing input_source_policy has incompatible checks"
        )

    indexes = {
        str(item["name"]): (
            tuple(item.get("column_names") or ()),
            bool(item.get("unique", False)),
        )
        for item in inspector.get_indexes(_TABLE)
        if item.get("name") is not None
        and item.get("duplicates_constraint") != _UNIQUE
    }
    if (
        set(indexes) - set(_EXPECTED_INDEXES)
        or any(
            definition != _EXPECTED_INDEXES[name]
            for name, definition in indexes.items()
        )
    ):
        raise RuntimeError(
            "existing input_source_policy has incompatible index definitions"
        )
    return set(indexes)


def upgrade() -> None:
    if context.is_offline_mode():
        _create_table()
        existing_indexes: set[str] = set()
    else:
        bind = op.get_bind()
        inspector = sa.inspect(bind)
        if inspector.has_table(_TABLE):
            existing_indexes = _validate_existing_table(bind)
        else:
            _create_table()
            existing_indexes = set()

    if _OWNER_INDEX not in existing_indexes:
        op.create_index(
            _OWNER_INDEX,
            _TABLE,
            ["owner_principal_id"],
            unique=False,
        )
    if _SOURCE_INDEX not in existing_indexes:
        op.create_index(
            _SOURCE_INDEX,
            _TABLE,
            ["source_id"],
            unique=False,
        )


def downgrade() -> None:
    _assert_downgrade_is_lossless()
    op.drop_index(
        _SOURCE_INDEX,
        table_name=_TABLE,
    )
    op.drop_index(
        _OWNER_INDEX,
        table_name=_TABLE,
    )
    op.drop_table(_TABLE)
