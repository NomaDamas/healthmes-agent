"""Bounded PostgreSQL locking helpers for destructive migrations."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import Connection

_POSTGRES_DOWNGRADE_LOCK_TIMEOUT = "5s"
_POSTGRES_LOCK_NOT_AVAILABLE = "55P03"


def acquire_postgres_downgrade_lock(
    bind: Connection,
    lock_statement: str,
    *,
    resource: str,
) -> None:
    """Acquire one transaction-scoped downgrade lock with a bounded wait."""
    bind.execute(
        sa.text(
            "SET LOCAL lock_timeout = "
            f"'{_POSTGRES_DOWNGRADE_LOCK_TIMEOUT}'"
        )
    )
    try:
        bind.execute(sa.text(lock_statement))
    except sa.exc.DBAPIError as exc:
        original = exc.orig
        sqlstate = getattr(original, "sqlstate", None) or getattr(
            original,
            "pgcode",
            None,
        )
        if sqlstate != _POSTGRES_LOCK_NOT_AVAILABLE:
            raise
        raise RuntimeError(
            "could not acquire the PostgreSQL downgrade safety lock for "
            f"{resource} within {_POSTGRES_DOWNGRADE_LOCK_TIMEOUT}; retry "
            "after concurrent writers or migrations finish"
        ) from exc
