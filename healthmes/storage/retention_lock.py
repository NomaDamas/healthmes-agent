"""Transaction-scoped retention policy serialization."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from typing import Any

from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from healthmes.local_database_lock import acquire_local_database_lock
from healthmes.store import RetentionPolicy

_SESSION_LOCK_KEY = "retention_policy_locks"


def _validate_lock_order(
    held: Mapping[str, object],
    pending: list[str],
) -> None:
    if not held or not pending:
        return
    held_data_classes = sorted(held)
    if pending[0] < held_data_classes[-1]:
        raise RuntimeError("retention policy locks must be acquired in canonical order")


def _acquire_process_locks(
    session: Session,
    data_classes: list[str],
) -> None:
    held = session.info.setdefault(_SESSION_LOCK_KEY, {})
    pending = [value for value in data_classes if value not in held]
    _validate_lock_order(held, pending)
    acquire_local_database_lock(session)
    for data_class in pending:
        held[data_class] = True


def _postgres_creation_lock_id(data_class: str) -> int:
    digest = sha256(f"healthmes-retention-policy:{data_class}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _lock_postgres_policies(
    session: Session,
    defaults: Mapping[str, int | None],
    data_classes: list[str],
) -> dict[str, RetentionPolicy]:
    held = session.info.setdefault(_SESSION_LOCK_KEY, {})
    pending = [value for value in data_classes if value not in held]
    _validate_lock_order(held, pending)
    rows: dict[str, RetentionPolicy] = {}
    for data_class in pending:
        policy = session.scalar(
            select(RetentionPolicy)
            .where(RetentionPolicy.data_class == data_class)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if policy is None:
            session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": _postgres_creation_lock_id(data_class)},
            )
            policy = session.scalar(
                select(RetentionPolicy)
                .where(RetentionPolicy.data_class == data_class)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if policy is None:
                policy = RetentionPolicy(
                    data_class=data_class,
                    retention_days=defaults[data_class],
                    enabled=True,
                )
                session.add(policy)
                session.flush()
        held[data_class] = True
        rows[data_class] = policy
    for data_class in data_classes:
        if data_class in rows:
            continue
        policy = session.scalar(
            select(RetentionPolicy).where(RetentionPolicy.data_class == data_class)
        )
        if policy is None:  # pragma: no cover - held lock owns the invariant
            raise RuntimeError(f"missing locked retention policy: {data_class}")
        rows[data_class] = policy
    return rows


def lock_retention_policies(
    session: Session,
    defaults: Mapping[str, int | None],
) -> dict[str, RetentionPolicy]:
    """Lock policy rows in canonical order and create missing rows safely."""

    data_classes = sorted(set(defaults))
    if not data_classes:
        return {}
    if any(not value for value in data_classes):
        raise ValueError("retention data classes must not be empty")
    if session.get_bind().dialect.name == "postgresql":
        return _lock_postgres_policies(
            session,
            defaults,
            data_classes,
        )

    _acquire_process_locks(session, data_classes)
    rows: dict[str, RetentionPolicy] = {}
    for data_class in data_classes:
        policy = session.scalar(
            select(RetentionPolicy)
            .where(RetentionPolicy.data_class == data_class)
            .execution_options(populate_existing=True)
        )
        if policy is None:
            policy = RetentionPolicy(
                data_class=data_class,
                retention_days=defaults[data_class],
                enabled=True,
            )
            session.add(policy)
            session.flush()
        rows[data_class] = policy
    return rows


def _clear_held_policy_locks(session: Session) -> None:
    session.info.pop(_SESSION_LOCK_KEY, None)


@event.listens_for(Session, "after_commit")
def _clear_committed_locks(session: Session) -> None:
    if not session.in_nested_transaction():
        _clear_held_policy_locks(session)


@event.listens_for(Session, "after_soft_rollback")
def _clear_rolled_back_locks(
    session: Session,
    previous_transaction: Any,
) -> None:
    if not previous_transaction.nested:
        _clear_held_policy_locks(session)


@event.listens_for(Session, "after_transaction_end")
def _clear_closed_locks(session: Session, transaction: Any) -> None:
    if transaction.parent is None and not session.in_transaction():
        _clear_held_policy_locks(session)
