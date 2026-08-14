"""separate Open Wearables retention from generic wellness events

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-08-14 12:00:00
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import timedelta

import sqlalchemy as sa

from alembic import context, op

revision: str = "d8e9f0a1b2c3"
down_revision: str | Sequence[str] | None = "c7d8e9f0a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GENERIC_CLASS = "normalized"
_WEARABLE_CLASS = "wearable_normalized"
_WEARABLE_PROVIDER = "healthmes-open-wearables-mirror"
_OFFLINE_POLICY_ID = "d8e9f0a1-b2c3-4d5e-8f90-a1b2c3d4e5f6"

retention_policy = sa.table(
    "retention_policy",
    sa.column("id", sa.Uuid()),
    sa.column("data_class", sa.String(length=64)),
    sa.column("retention_days", sa.Integer()),
    sa.column("enabled", sa.Boolean()),
)
wellness_event = sa.table(
    "wellness_event",
    sa.column("id", sa.Uuid()),
    sa.column("observed_at", sa.DateTime(timezone=True)),
    sa.column("source_provider", sa.String(length=64)),
    sa.column("retention_policy_id", sa.Uuid()),
    sa.column("expires_at", sa.DateTime(timezone=True)),
)


def _policy(bind, data_class: str):
    return bind.execute(
        sa.select(
            retention_policy.c.id,
            retention_policy.c.retention_days,
            retention_policy.c.enabled,
        ).where(retention_policy.c.data_class == data_class)
    ).mappings().one_or_none()


def _ensure_wearable_policy(bind):
    existing = _policy(bind, _WEARABLE_CLASS)
    if existing is not None:
        return existing
    generic = _policy(bind, _GENERIC_CLASS)
    policy_id = uuid.UUID(_OFFLINE_POLICY_ID)
    bind.execute(
        retention_policy.insert().values(
            id=policy_id,
            data_class=_WEARABLE_CLASS,
            retention_days=(
                generic["retention_days"] if generic is not None else 30
            ),
            enabled=generic["enabled"] if generic is not None else True,
        )
    )
    return _policy(bind, _WEARABLE_CLASS)


def _ensure_generic_policy(bind):
    existing = _policy(bind, _GENERIC_CLASS)
    if existing is not None:
        return existing
    wearable = _policy(bind, _WEARABLE_CLASS)
    policy_id = uuid.uuid4()
    bind.execute(
        retention_policy.insert().values(
            id=policy_id,
            data_class=_GENERIC_CLASS,
            retention_days=(
                wearable["retention_days"] if wearable is not None else 30
            ),
            enabled=wearable["enabled"] if wearable is not None else True,
        )
    )
    return _policy(bind, _GENERIC_CLASS)


def _move_wearable_events(
    bind,
    policy,
    *,
    preserve_earlier_expiry: bool = False,
) -> None:
    rows = list(
        bind.execute(
            sa.select(
                wellness_event.c.id,
                wellness_event.c.observed_at,
                wellness_event.c.expires_at,
            ).where(
                wellness_event.c.source_provider == _WEARABLE_PROVIDER
            )
        ).mappings()
    )
    for row in rows:
        expires_at = None
        if policy["enabled"] and policy["retention_days"] is not None:
            expires_at = row["observed_at"] + timedelta(
                days=policy["retention_days"]
            )
        existing_expiry = row["expires_at"]
        if (
            preserve_earlier_expiry
            and existing_expiry is not None
            and (expires_at is None or existing_expiry < expires_at)
        ):
            expires_at = existing_expiry
        bind.execute(
            wellness_event.update()
            .where(wellness_event.c.id == row["id"])
            .values(
                retention_policy_id=policy["id"],
                expires_at=expires_at,
            )
        )


def _offline_expiry_expression(data_class: str) -> str:
    policy = (
        "(SELECT retention_days, enabled FROM retention_policy "
        f"WHERE data_class = '{data_class}')"
    )
    dialect = context.get_context().dialect.name
    if dialect == "postgresql":
        expiry = (
            "wellness_event.observed_at + "
            "(retention_policy.retention_days * INTERVAL '1 day')"
        )
    else:
        expiry = (
            "datetime(wellness_event.observed_at, "
            "'+' || retention_policy.retention_days || ' days')"
        )
    return (
        "(SELECT CASE "
        "WHEN retention_policy.enabled "
        "AND retention_policy.retention_days IS NOT NULL "
        f"THEN {expiry} ELSE NULL END FROM {policy} AS retention_policy)"
    )


def _offline_uuid_literal(value: str) -> str:
    processor = sa.Uuid().literal_processor(
        context.get_context().dialect
    )
    if processor is None:
        raise RuntimeError("offline UUID literal rendering is unavailable")
    return processor(uuid.UUID(value))


def _offline_move_wearable_events(
    data_class: str,
    *,
    preserve_earlier_expiry: bool = False,
) -> None:
    target_expiry = _offline_expiry_expression(data_class)
    expiry_assignment = target_expiry
    if preserve_earlier_expiry:
        expiry_assignment = (
            "CASE WHEN wellness_event.expires_at IS NOT NULL "
            f"AND ({target_expiry} IS NULL "
            f"OR wellness_event.expires_at < {target_expiry}) "
            "THEN wellness_event.expires_at "
            f"ELSE {target_expiry} END"
        )
    op.execute(
        sa.text(
            "UPDATE wellness_event "
            "SET retention_policy_id = ("
            "SELECT id FROM retention_policy "
            f"WHERE data_class = '{data_class}'"
            "), "
            f"expires_at = {expiry_assignment} "
            f"WHERE source_provider = '{_WEARABLE_PROVIDER}' "
            "AND EXISTS ("
            "SELECT 1 FROM retention_policy "
            f"WHERE data_class = '{data_class}'"
            ")"
        )
    )


def _offline_upgrade() -> None:
    wearable_policy_id = _offline_uuid_literal(_OFFLINE_POLICY_ID)
    op.execute(
        sa.text(
            "INSERT INTO retention_policy "
            "(id, data_class, retention_days, enabled) "
            f"SELECT {wearable_policy_id}, '{_WEARABLE_CLASS}', "
            "retention_days, enabled "
            "FROM retention_policy "
            f"WHERE data_class = '{_GENERIC_CLASS}' "
            "AND NOT EXISTS ("
            "SELECT 1 FROM retention_policy "
            f"WHERE data_class = '{_WEARABLE_CLASS}'"
            ")"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO retention_policy "
            "(id, data_class, retention_days, enabled) "
            f"SELECT {wearable_policy_id}, '{_WEARABLE_CLASS}', 30, TRUE "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM retention_policy "
            f"WHERE data_class = '{_WEARABLE_CLASS}'"
            ")"
        )
    )
    _offline_move_wearable_events(
        _WEARABLE_CLASS,
        preserve_earlier_expiry=True,
    )


def _assert_downgrade_is_lossless() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "offline downgrade cannot verify wearable retention policy; "
            "run the downgrade online"
        )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "LOCK TABLE retention_policy, wellness_event "
                "IN ACCESS EXCLUSIVE MODE"
            )
        )
    elif bind.dialect.name == "sqlite":
        bind.execute(
            sa.text(
                "UPDATE retention_policy "
                "SET enabled = enabled "
                "WHERE 0"
            )
        )

    generic = _policy(bind, _GENERIC_CLASS)
    wearable = _policy(bind, _WEARABLE_CLASS)
    if (
        generic is not None
        and wearable is not None
        and (
            generic["retention_days"] != wearable["retention_days"]
            or generic["enabled"] != wearable["enabled"]
        )
    ):
        raise RuntimeError(
            "cannot downgrade wearable retention without losing its "
            "dedicated retention policy"
        )


def upgrade() -> None:
    if context.is_offline_mode():
        _offline_upgrade()
        return
    bind = op.get_bind()
    wearable = _ensure_wearable_policy(bind)
    assert wearable is not None
    _move_wearable_events(
        bind,
        wearable,
        preserve_earlier_expiry=True,
    )


def downgrade() -> None:
    _assert_downgrade_is_lossless()
    bind = op.get_bind()
    generic = _ensure_generic_policy(bind)
    assert generic is not None
    _move_wearable_events(
        bind,
        generic,
        preserve_earlier_expiry=True,
    )
    bind.execute(
        retention_policy.delete().where(
            retention_policy.c.id == uuid.UUID(_OFFLINE_POLICY_ID),
            retention_policy.c.data_class == _WEARABLE_CLASS,
        )
    )
