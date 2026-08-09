"""Process-local serialization for the self-hosted activity write plane."""

from __future__ import annotations

from contextlib import contextmanager
from threading import RLock

_ACTIVITY_WRITE_LOCK = RLock()


@contextmanager
def activity_write_lock():
    """Serialize activity ingest, retention, deletion, and summary writes."""
    with _ACTIVITY_WRITE_LOCK:
        yield
