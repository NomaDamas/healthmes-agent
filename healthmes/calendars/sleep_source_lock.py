from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from healthmes.activity.locking import (
    _raise_postgres_advisory_cleanup_failure,
    acquire_postgres_advisory_lock,
    release_postgres_advisory_lock,
)
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
        acquire_postgres_advisory_lock(
            connection,
            f"{calendar_source.value}:{source_key}",
        )
    except Exception as exc:
        try:
            _raise_postgres_advisory_cleanup_failure(
                connection,
                cause=exc,
                context=(
                    "failed to acquire PostgreSQL sleep-source advisory lock"
                ),
            )
        finally:
            connection.close()
    return connection


def unlock_sleep_source_key(
    connection: Connection,
    calendar_source: CalendarSource,
    source_key: str,
) -> None:
    try:
        try:
            released = release_postgres_advisory_lock(
                connection,
                f"{calendar_source.value}:{source_key}",
            )
            if released is not True:
                raise AdvisoryLockReleaseError(source_key)
        except Exception as exc:
            _raise_postgres_advisory_cleanup_failure(
                connection,
                cause=exc,
                context=(
                    "failed to release PostgreSQL sleep-source advisory lock"
                ),
            )
    finally:
        connection.close()
