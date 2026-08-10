"""Process-local serialization for the self-hosted activity write plane."""

from __future__ import annotations

from contextlib import contextmanager
from threading import RLock

from sqlalchemy import text
from sqlalchemy.orm import Session

_ACTIVITY_WRITE_LOCK = RLock()
_ACTIVITY_WRITE_PLANE_KEY = "healthmes:activity:write-plane:v1"


@contextmanager
def activity_write_lock():
    """Serialize activity ingest, retention, deletion, and summary writes."""
    with _ACTIVITY_WRITE_LOCK:
        yield


def lock_activity_write_plane(session: Session) -> None:
    """Serialize PostgreSQL activity writes across processes and devices."""
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended(:write_plane_key, 0)"
            ")"
        ),
        {"write_plane_key": _ACTIVITY_WRITE_PLANE_KEY},
    )
