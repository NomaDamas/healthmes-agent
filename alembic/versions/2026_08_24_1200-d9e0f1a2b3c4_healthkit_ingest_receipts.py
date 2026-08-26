"""add HealthKit ingest receipts and deletion tombstones

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
Create Date: 2026-08-24 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op
from healthmes.store.base import JSONB
from healthmes.store.migration_safety import acquire_postgres_downgrade_lock

revision: str = "d9e0f1a2b3c4"
down_revision: str | Sequence[str] | None = "c8d9e0f1a2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RECEIPT_TABLE = "healthkit_ingest_receipt"
_TOMBSTONE_TABLE = "healthkit_deletion_tombstone"

_RECEIPT_COLUMNS = {
    "id": False,
    "idempotency_key_digest": False,
    "body_sha256": False,
    "state": False,
    "owner_token": True,
    "lease_generation": False,
    "lease_expires_at": True,
    "raw_id": True,
    "ack_payload": True,
    "created_at": False,
    "updated_at": False,
}
_TOMBSTONE_COLUMNS = {
    "id": False,
    "sample_id": False,
    "sample_type": False,
    "deleted_at": False,
    "raw_id": True,
    "idempotency_key_digest": True,
    "created_at": False,
    "updated_at": False,
}


def _constraint_columns(items: list[dict[str, object]]) -> dict[str, tuple[str, ...]]:
    return {
        str(item["name"]): tuple(str(column) for column in item["column_names"])
        for item in items
        if item.get("name") is not None
    }


def _assert_existing_table_is_compatible(
    inspector: sa.Inspector,
    *,
    table_name: str,
    expected_columns: dict[str, bool],
    expected_unique: dict[str, tuple[str, ...]],
    expected_checks: dict[str, tuple[str, ...]],
) -> None:
    """Reject stamped or partially-created tables before any migration DDL."""

    problems: list[str] = []
    columns = {
        str(column["name"]): bool(column["nullable"])
        for column in inspector.get_columns(table_name)
    }
    if set(columns) != set(expected_columns):
        missing = sorted(set(expected_columns) - set(columns))
        unexpected = sorted(set(columns) - set(expected_columns))
        problems.append(
            f"columns missing={missing!r} unexpected={unexpected!r}"
        )
    else:
        nullable_mismatches = sorted(
            name
            for name, nullable in expected_columns.items()
            if columns[name] is not nullable
        )
        if nullable_mismatches:
            problems.append(
                "column nullability differs for "
                f"{nullable_mismatches!r}"
            )

    primary_key = inspector.get_pk_constraint(table_name)
    if tuple(primary_key.get("constrained_columns") or ()) != ("id",):
        problems.append("primary key must contain only id")

    unique = _constraint_columns(
        inspector.get_unique_constraints(table_name)
    )
    if unique != expected_unique:
        problems.append(
            f"unique constraints expected={expected_unique!r} actual={unique!r}"
        )

    checks = {
        str(item["name"]): str(item.get("sqltext") or "").lower()
        for item in inspector.get_check_constraints(table_name)
        if item.get("name") is not None
    }
    if set(checks) != set(expected_checks):
        problems.append(
            "check constraints expected="
            f"{sorted(expected_checks)!r} actual={sorted(checks)!r}"
        )
    else:
        for name, required_fragments in expected_checks.items():
            expression = checks[name]
            if any(fragment not in expression for fragment in required_fragments):
                problems.append(f"check constraint {name!r} has wrong expression")

    foreign_keys = {
        str(item["name"]): (
            tuple(str(column) for column in item["constrained_columns"]),
            str(item.get("referred_table")),
            tuple(str(column) for column in item["referred_columns"]),
            str((item.get("options") or {}).get("ondelete", "")).upper(),
        )
        for item in inspector.get_foreign_keys(table_name)
        if item.get("name") is not None
    }
    expected_foreign_keys = {
        f"fk_{table_name}_raw_id_raw_ingest_event": (
            ("raw_id",),
            "raw_ingest_event",
            ("id",),
            "SET NULL",
        )
    }
    if foreign_keys != expected_foreign_keys:
        problems.append(
            "foreign keys expected="
            f"{expected_foreign_keys!r} actual={foreign_keys!r}"
        )

    if problems:
        raise RuntimeError(
            f"existing {table_name} table is incomplete or incompatible: "
            + "; ".join(problems)
        )


def upgrade() -> None:
    inspector = None if context.is_offline_mode() else sa.inspect(op.get_bind())
    receipt_exists = (
        inspector is not None
        and inspector.has_table(_RECEIPT_TABLE)
    )
    tombstone_exists = (
        inspector is not None
        and inspector.has_table(_TOMBSTONE_TABLE)
    )
    if inspector is not None:
        if receipt_exists:
            _assert_existing_table_is_compatible(
                inspector,
                table_name=_RECEIPT_TABLE,
                expected_columns=_RECEIPT_COLUMNS,
                expected_unique={
                    "uq_healthkit_ingest_receipt_idempotency_key_digest": (
                        "idempotency_key_digest",
                    )
                },
                expected_checks={
                    "ck_healthkit_ingest_receipt_lease_generation_positive": (
                        "lease_generation",
                        ">=",
                        "1",
                    ),
                    "ck_healthkit_ingest_receipt_state_consistent": (
                        "state",
                        "pending",
                        "completed",
                        "owner_token",
                        "lease_expires_at",
                        "ack_payload",
                    ),
                },
            )
        if tombstone_exists:
            _assert_existing_table_is_compatible(
                inspector,
                table_name=_TOMBSTONE_TABLE,
                expected_columns=_TOMBSTONE_COLUMNS,
                expected_unique={
                    "uq_healthkit_deletion_tombstone_sample_id": (
                        "sample_id",
                    )
                },
                expected_checks={},
            )

    if not receipt_exists:
        op.create_table(
            _RECEIPT_TABLE,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column(
                "idempotency_key_digest",
                sa.String(length=64),
                nullable=False,
            ),
            sa.Column("body_sha256", sa.String(length=64), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("owner_token", sa.Uuid(), nullable=True),
            sa.Column("lease_generation", sa.Integer(), nullable=False),
            sa.Column(
                "lease_expires_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.Column("raw_id", sa.Uuid(), nullable=True),
            sa.Column("ack_payload", JSONB, nullable=True),
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
            sa.CheckConstraint(
                "lease_generation >= 1",
                name="lease_generation_positive",
            ),
            sa.CheckConstraint(
                "("
                "state = 'pending' "
                "AND owner_token IS NOT NULL "
                "AND lease_expires_at IS NOT NULL "
                "AND ack_payload IS NULL"
                ") OR ("
                "state = 'completed' "
                "AND owner_token IS NULL "
                "AND lease_expires_at IS NULL "
                "AND ack_payload IS NOT NULL"
                ")",
                name="state_consistent",
            ),
            sa.ForeignKeyConstraint(
                ["raw_id"],
                ["raw_ingest_event.id"],
                name=(
                    "fk_healthkit_ingest_receipt_raw_id_raw_ingest_event"
                ),
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint(
                "id",
                name="pk_healthkit_ingest_receipt",
            ),
            sa.UniqueConstraint(
                "idempotency_key_digest",
                name="uq_healthkit_ingest_receipt_idempotency_key_digest",
            ),
        )

    if not tombstone_exists:
        op.create_table(
            _TOMBSTONE_TABLE,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("sample_id", sa.String(length=255), nullable=False),
            sa.Column("sample_type", sa.String(length=255), nullable=False),
            sa.Column(
                "deleted_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column("raw_id", sa.Uuid(), nullable=True),
            sa.Column(
                "idempotency_key_digest",
                sa.String(length=64),
                nullable=True,
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
            sa.ForeignKeyConstraint(
                ["raw_id"],
                ["raw_ingest_event.id"],
                name=(
                    "fk_healthkit_deletion_tombstone_raw_id_"
                    "raw_ingest_event"
                ),
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint(
                "id",
                name="pk_healthkit_deletion_tombstone",
            ),
            sa.UniqueConstraint(
                "sample_id",
                name="uq_healthkit_deletion_tombstone_sample_id",
            ),
        )

    # A database created directly from the ORM already has these indexes.
    # Check before creating them so a stamped create_all database can advance.
    existing_indexes: set[str] = set()
    if inspector is not None:
        if receipt_exists:
            existing_indexes.update(
                index["name"]
                for index in inspector.get_indexes(
                    _RECEIPT_TABLE
                )
            )
        if tombstone_exists:
            existing_indexes.update(
                index["name"]
                for index in inspector.get_indexes(
                    _TOMBSTONE_TABLE
                )
            )
    indexes = (
        (
            "ix_healthkit_ingest_receipt_lease_expires_at",
            _RECEIPT_TABLE,
            ["lease_expires_at"],
        ),
        (
            "ix_healthkit_ingest_receipt_raw_id",
            _RECEIPT_TABLE,
            ["raw_id"],
        ),
        (
            "ix_healthkit_deletion_tombstone_deleted_at",
            _TOMBSTONE_TABLE,
            ["deleted_at"],
        ),
        (
            "ix_healthkit_deletion_tombstone_idempotency_key_digest",
            _TOMBSTONE_TABLE,
            ["idempotency_key_digest"],
        ),
        (
            "ix_healthkit_deletion_tombstone_raw_id",
            _TOMBSTONE_TABLE,
            ["raw_id"],
        ),
    )
    for index_name, table_name, columns in indexes:
        if index_name not in existing_indexes:
            op.create_index(index_name, table_name, columns, unique=False)


def _assert_downgrade_is_lossless() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "offline downgrade cannot verify HealthKit sync state; "
            "run the downgrade online"
        )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        acquire_postgres_downgrade_lock(
            bind,
            (
                "LOCK TABLE healthkit_ingest_receipt, "
                "healthkit_deletion_tombstone IN ACCESS EXCLUSIVE MODE"
            ),
            resource="HealthKit ingest receipts and deletion tombstones",
        )
    elif bind.dialect.name == "sqlite":
        bind.execute(
            sa.text(
                "UPDATE healthkit_ingest_receipt "
                "SET lease_generation = lease_generation WHERE 0"
            )
        )
        bind.execute(
            sa.text(
                "UPDATE healthkit_deletion_tombstone "
                "SET sample_id = sample_id WHERE 0"
            )
        )
    populated = bind.execute(
        sa.text("SELECT 1 FROM healthkit_ingest_receipt LIMIT 1")
    ).first() or bind.execute(
        sa.text("SELECT 1 FROM healthkit_deletion_tombstone LIMIT 1")
    ).first()
    if populated is not None:
        raise RuntimeError(
            "cannot downgrade HealthKit sync storage without losing "
            "idempotency receipts or deletion tombstones"
        )


def downgrade() -> None:
    _assert_downgrade_is_lossless()
    op.drop_index(
        "ix_healthkit_deletion_tombstone_raw_id",
        table_name="healthkit_deletion_tombstone",
    )
    op.drop_index(
        "ix_healthkit_deletion_tombstone_idempotency_key_digest",
        table_name="healthkit_deletion_tombstone",
    )
    op.drop_index(
        "ix_healthkit_deletion_tombstone_deleted_at",
        table_name="healthkit_deletion_tombstone",
    )
    op.drop_table("healthkit_deletion_tombstone")
    op.drop_index(
        "ix_healthkit_ingest_receipt_raw_id",
        table_name="healthkit_ingest_receipt",
    )
    op.drop_index(
        "ix_healthkit_ingest_receipt_lease_expires_at",
        table_name="healthkit_ingest_receipt",
    )
    op.drop_table("healthkit_ingest_receipt")
