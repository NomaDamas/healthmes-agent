"""harden decision receipts without mutating the published revision

Revision ID: a1b2c3d4e5f6
Revises: f0a1b2c3d4e5
Create Date: 2026-08-17 09:00:00
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from alembic import context, op
from healthmes.store.base import JSONB

revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "f0a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "decision_request_receipt"
_STATE_CONSTRAINT = "ck_decision_request_receipt_state_payload_consistent"
_GENERATION_CONSTRAINT = "ck_decision_request_receipt_lease_generation_positive"
_STATE_EXPRESSION = (
    "("
    "state = 'pending' "
    "AND owner_token IS NOT NULL "
    "AND lease_expires_at IS NOT NULL "
    "AND result_payload IS NULL "
    "AND result_expires_at IS NULL"
    ") OR ("
    "state = 'completed' "
    "AND owner_token IS NULL "
    "AND lease_expires_at IS NULL "
    "AND result_payload IS NOT NULL "
    "AND result_expires_at IS NOT NULL"
    ") OR ("
    "state = 'tombstone' "
    "AND owner_token IS NULL "
    "AND lease_expires_at IS NULL "
    "AND result_payload IS NULL "
    "AND result_expires_at IS NULL"
    ")"
)


def _column_names(bind: sa.Connection) -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(bind).get_columns(_TABLE)
    }


def _receipt_table(
    *,
    include_requested_at: bool,
    include_hardening: bool,
) -> sa.Table:
    """Frozen canonical f0 shape for SQLite offline batch rendering."""

    metadata = sa.MetaData()
    columns: list[sa.SchemaItem] = [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column(
            "request_fingerprint",
            sa.String(length=64),
            nullable=False,
        ),
    ]
    if include_requested_at:
        columns.append(
            sa.Column(
                "requested_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
    columns.extend(
        [
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("owner_token", sa.Uuid(), nullable=True),
        ]
    )
    if include_hardening:
        columns.append(
            sa.Column(
                "lease_generation",
                sa.Integer(),
                server_default=sa.text("1"),
                nullable=False,
            )
        )
    columns.extend(
        [
            sa.Column(
                "lease_expires_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.Column("result_payload", JSONB, nullable=True),
        ]
    )
    if include_hardening:
        columns.append(
            sa.Column(
                "result_expires_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
    columns.extend(
        [
            sa.Column(
                "expires_at",
                sa.DateTime(timezone=True),
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
            sa.CheckConstraint(
                "("
                "state = 'pending' "
                "AND owner_token IS NOT NULL "
                "AND lease_expires_at IS NOT NULL "
                "AND result_payload IS NULL"
                ") OR ("
                "state = 'completed' "
                "AND owner_token IS NULL "
                "AND lease_expires_at IS NULL "
                "AND result_payload IS NOT NULL"
                ")",
                name=_STATE_CONSTRAINT,
            ),
            sa.PrimaryKeyConstraint(
                "id",
                name="pk_decision_request_receipt",
            ),
            sa.UniqueConstraint(
                "request_id",
                name="uq_decision_request_receipt_request_id",
            ),
        ]
    )
    table = sa.Table(_TABLE, metadata, *columns)
    for column in (
        "request_id",
        "state",
        "owner_token",
        "lease_expires_at",
        "expires_at",
    ):
        sa.Index(
            f"ix_decision_request_receipt_{column}",
            table.c[column],
        )
    return table


def _offline_backfill_result_expiry() -> None:
    dialect = op.get_context().dialect.name
    if dialect == "sqlite":
        decision_deadline = (
            "datetime(requested_at, '+' || ("
            "SELECT retention_days FROM retention_policy "
            "WHERE data_class = 'decision' AND enabled = 1 "
            "AND retention_days IS NOT NULL LIMIT 1"
            ") || ' days')"
        )
        op.execute(
            sa.text(
                "UPDATE decision_request_receipt "
                "SET result_expires_at = CASE "
                "WHEN EXISTS ("
                "SELECT 1 FROM retention_policy "
                "WHERE data_class = 'decision' AND enabled = 1 "
                "AND retention_days IS NOT NULL"
                ") "
                f"THEN min(expires_at, {decision_deadline}) "
                "ELSE expires_at END "
                "WHERE state = 'completed'"
            )
        )
        return
    op.execute(
        sa.text(
            "UPDATE decision_request_receipt "
            "SET result_expires_at = CASE "
            "WHEN EXISTS ("
            "SELECT 1 FROM retention_policy "
            "WHERE data_class = 'decision' AND enabled "
            "AND retention_days IS NOT NULL"
            ") "
            "THEN LEAST("
            "expires_at, "
            "requested_at + ("
            "SELECT retention_days * INTERVAL '1 day' "
            "FROM retention_policy "
            "WHERE data_class = 'decision' AND enabled "
            "AND retention_days IS NOT NULL LIMIT 1"
            ")"
            ") "
            "ELSE expires_at END "
            "WHERE state = 'completed'"
        )
    )


def _offline_tombstone_expired_results() -> None:
    op.execute(
        sa.text(
            "UPDATE decision_request_receipt "
            "SET state = 'tombstone', "
            "owner_token = NULL, "
            "lease_expires_at = NULL, "
            "result_payload = NULL, "
            "result_expires_at = NULL "
            "WHERE state = 'completed' "
            "AND result_expires_at <= CURRENT_TIMESTAMP"
        )
    )


def _upgrade_offline() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "lease_generation",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "result_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.execute(
        sa.text(
            "UPDATE decision_request_receipt "
            "SET requested_at = created_at "
            "WHERE requested_at IS NULL"
        )
    )
    _offline_backfill_result_expiry()
    _offline_tombstone_expired_results()

    if op.get_context().dialect.name == "sqlite":
        with op.batch_alter_table(
            _TABLE,
            copy_from=_receipt_table(
                include_requested_at=True,
                include_hardening=True,
            ),
            recreate="always",
        ) as batch:
            batch.alter_column(
                "requested_at",
                existing_type=sa.DateTime(timezone=True),
                nullable=False,
            )
            batch.drop_constraint(
                op.f(_STATE_CONSTRAINT),
                type_="check",
            )
            batch.create_check_constraint(
                "state_payload_consistent",
                _STATE_EXPRESSION,
            )
            batch.create_check_constraint(
                "lease_generation_positive",
                "lease_generation >= 1",
            )
    else:
        op.alter_column(
            _TABLE,
            "requested_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
        op.drop_constraint(
            _STATE_CONSTRAINT,
            _TABLE,
            type_="check",
        )
        op.create_check_constraint(
            "state_payload_consistent",
            _TABLE,
            _STATE_EXPRESSION,
        )
        op.create_check_constraint(
            "lease_generation_positive",
            _TABLE,
            "lease_generation >= 1",
        )
    op.create_index(
        "ix_decision_request_receipt_result_expires_at",
        _TABLE,
        ["result_expires_at"],
    )


def _add_columns(bind: sa.Connection) -> None:
    columns = _column_names(bind)
    if "requested_at" not in columns:
        with op.batch_alter_table(_TABLE) as batch:
            batch.add_column(
                sa.Column(
                    "requested_at",
                    sa.DateTime(timezone=True),
                    nullable=True,
                )
            )
        receipt = sa.Table(
            _TABLE,
            sa.MetaData(),
            autoload_with=bind,
        )
        bind.execute(
            receipt.update()
            .where(receipt.c.requested_at.is_(None))
            .values(requested_at=receipt.c.created_at)
        )
        with op.batch_alter_table(_TABLE) as batch:
            batch.alter_column(
                "requested_at",
                existing_type=sa.DateTime(timezone=True),
                nullable=False,
            )
        columns.add("requested_at")

    with op.batch_alter_table(_TABLE) as batch:
        if "lease_generation" not in columns:
            batch.add_column(
                sa.Column(
                    "lease_generation",
                    sa.Integer(),
                    server_default=sa.text("1"),
                    nullable=False,
                )
            )
        if "result_expires_at" not in columns:
            batch.add_column(
                sa.Column(
                    "result_expires_at",
                    sa.DateTime(timezone=True),
                    nullable=True,
                )
            )


def _decision_retention_days(bind: sa.Connection) -> int | None:
    inspector = sa.inspect(bind)
    if "retention_policy" not in inspector.get_table_names():
        return None
    policy = sa.Table(
        "retention_policy",
        sa.MetaData(),
        autoload_with=bind,
    )
    row = bind.execute(
        sa.select(policy.c.enabled, policy.c.retention_days)
        .where(policy.c.data_class == "decision")
        .limit(1)
    ).first()
    if row is None or not row.enabled or row.retention_days is None:
        return None
    return int(row.retention_days)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _backfill_result_expiry(bind: sa.Connection) -> list[object]:
    receipt = sa.Table(
        _TABLE,
        sa.MetaData(),
        autoload_with=bind,
    )
    retention_days = _decision_retention_days(bind)
    now = datetime.now(UTC)
    expired_ids: list[object] = []
    rows = bind.execute(
        sa.select(
            receipt.c.id,
            receipt.c.state,
            receipt.c.requested_at,
            receipt.c.result_expires_at,
            receipt.c.expires_at,
        )
    ).mappings()
    for row in rows:
        if row["state"] != "completed":
            continue
        deadline = _as_utc(row["expires_at"])
        if retention_days is not None:
            decision_deadline = _as_utc(
                row["requested_at"]
            ) + timedelta(days=retention_days)
            deadline = min(deadline, decision_deadline)
        if row["result_expires_at"] is not None:
            deadline = min(
                deadline,
                _as_utc(row["result_expires_at"]),
            )
        bind.execute(
            receipt.update()
            .where(receipt.c.id == row["id"])
            .values(result_expires_at=deadline)
        )
        if deadline <= now:
            expired_ids.append(row["id"])
    return expired_ids


def _replace_constraints(bind: sa.Connection) -> None:
    existing = {
        constraint["name"]
        for constraint in sa.inspect(bind).get_check_constraints(_TABLE)
    }
    with op.batch_alter_table(_TABLE) as batch:
        if _STATE_CONSTRAINT in existing:
            batch.drop_constraint(
                op.f(_STATE_CONSTRAINT),
                type_="check",
            )
        batch.create_check_constraint(
            "state_payload_consistent",
            _STATE_EXPRESSION,
        )
        if _GENERATION_CONSTRAINT not in existing:
            batch.create_check_constraint(
                "lease_generation_positive",
                "lease_generation >= 1",
            )


def _tombstone_expired_results(
    bind: sa.Connection,
    expired_ids: list[object],
) -> None:
    if not expired_ids:
        return
    receipt = sa.Table(
        _TABLE,
        sa.MetaData(),
        autoload_with=bind,
    )
    bind.execute(
        receipt.update()
        .where(receipt.c.id.in_(expired_ids))
        .values(
            state="tombstone",
            owner_token=None,
            lease_expires_at=None,
            result_payload=sa.null(),
            result_expires_at=None,
        )
    )


def _ensure_result_expiry_index(bind: sa.Connection) -> None:
    indexes = {
        index["name"]
        for index in sa.inspect(bind).get_indexes(_TABLE)
    }
    name = "ix_decision_request_receipt_result_expires_at"
    if name not in indexes:
        op.create_index(
            name,
            _TABLE,
            ["result_expires_at"],
        )


def upgrade() -> None:
    if context.is_offline_mode():
        _upgrade_offline()
        return
    bind = op.get_bind()
    _add_columns(bind)
    expired_ids = _backfill_result_expiry(bind)
    _replace_constraints(bind)
    _tombstone_expired_results(bind, expired_ids)
    _ensure_result_expiry_index(bind)


def downgrade() -> None:
    raise RuntimeError(
        "decision receipt hardening is forward-only because lease "
        "generations and expired-result tombstones cannot be removed safely"
    )
