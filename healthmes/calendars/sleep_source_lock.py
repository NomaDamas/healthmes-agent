from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from healthmes.store.enums import CalendarSource


@dataclass(frozen=True, slots=True)
class AdvisoryLockReleaseError(RuntimeError):
    source_key: str

    def __str__(self) -> str:
        return f"PostgreSQL advisory lock was not held for {self.source_key!r}"


def lock_sleep_source_key(
    session: Session,
    calendar_source: CalendarSource,
    source_key: str,
) -> Connection | None:
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return None
    engine = bind.engine if isinstance(bind, Connection) else bind
    connection = engine.connect()
    try:
        connection.execute(
            sa.text("SELECT pg_advisory_lock(hashtextextended(:source_key, 0))"),
            {"source_key": f"{calendar_source.value}:{source_key}"},
        )
    except SQLAlchemyError:
        connection.close()
        raise
    return connection


def unlock_sleep_source_key(
    connection: Connection,
    calendar_source: CalendarSource,
    source_key: str,
) -> None:
    try:
        released = connection.scalar(
            sa.text("SELECT pg_advisory_unlock(hashtextextended(:source_key, 0))"),
            {"source_key": f"{calendar_source.value}:{source_key}"},
        )
        if released is not True:
            raise AdvisoryLockReleaseError(source_key)
    finally:
        connection.close()
