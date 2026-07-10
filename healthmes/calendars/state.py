"""Per-backend sync-state persistence (Google syncToken / CalDAV ctag+etags).

The mirror service treats sync state as an opaque JSON blob owned by each
backend. Losing it is always safe — the next run performs a full window sync
and the mirror upserts idempotently — so a small JSON file under
``Settings.data_dir`` is sufficient (local-first, survives restarts, easy to
inspect and to wipe for a forced resync).
"""

import json
import logging
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from healthmes.calendars.base import SyncState
from healthmes.store.enums import CalendarSource

__all__ = [
    "FileSyncStateStore",
    "InMemorySyncStateStore",
    "SyncStateStore",
]

logger = logging.getLogger(__name__)


@runtime_checkable
class SyncStateStore(Protocol):
    """Loads/saves the opaque change cursor for one calendar source."""

    def load(self, source: CalendarSource) -> SyncState | None:
        """Return the persisted state for ``source``; ``None`` if never synced."""
        ...

    def save(self, source: CalendarSource, state: SyncState) -> None:
        """Persist ``state`` for ``source`` (replacing any previous state)."""
        ...


class InMemorySyncStateStore:
    """Dict-backed store for tests and one-off tooling (nothing persisted)."""

    def __init__(self) -> None:
        self._states: dict[str, SyncState] = {}

    def load(self, source: CalendarSource) -> SyncState | None:
        state = self._states.get(source.value)
        return dict(state) if state is not None else None

    def save(self, source: CalendarSource, state: SyncState) -> None:
        self._states[source.value] = dict(state)

    def clear(self, source: CalendarSource | None = None) -> None:
        """Drop one source's state (or all) to force a full resync."""
        if source is None:
            self._states.clear()
        else:
            self._states.pop(source.value, None)


class FileSyncStateStore:
    """JSON-file store keyed by calendar source, written atomically.

    Layout: ``{"google": {...}, "caldav": {...}}``. Writes go through a
    temp file + ``os.replace`` so a crash never leaves a torn file; a
    corrupted/unreadable file degrades to "never synced" (full resync)
    instead of failing the sync loop.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> "FileSyncStateStore":
        """Store under the local-first data dir (``Settings.data_dir``)."""
        return cls(Path(data_dir) / "calendars" / "sync_state.json")

    @property
    def path(self) -> Path:
        return self._path

    def load(self, source: CalendarSource) -> SyncState | None:
        state = self._read_all().get(source.value)
        return dict(state) if isinstance(state, dict) else None

    def save(self, source: CalendarSource, state: SyncState) -> None:
        states = self._read_all()
        states[source.value] = dict(state)
        self._write_all(states)

    def clear(self, source: CalendarSource | None = None) -> None:
        """Drop one source's state (or all) to force a full resync."""
        if source is None:
            self._write_all({})
            return
        states = self._read_all()
        if states.pop(source.value, None) is not None:
            self._write_all(states)

    def _read_all(self) -> dict[str, SyncState]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("corrupted sync-state file %s; forcing full resync", self._path)
            return {}
        return data if isinstance(data, dict) else {}

    def _write_all(self, states: dict[str, SyncState]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(states, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
        os.replace(tmp_path, self._path)
