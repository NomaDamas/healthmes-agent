"""Transaction-scoped serialization for the unified nutrition ledger."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from healthmes.local_database_lock import acquire_local_database_lock

_POSTGRES_ADVISORY_LOCK_ID = 0x484D45534E555452


def lock_nutrition_ledger(session: Session) -> None:
    """Hold the single-user nutrition ledger lock until transaction end."""

    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _POSTGRES_ADVISORY_LOCK_ID},
        )
        return
    acquire_local_database_lock(session)
