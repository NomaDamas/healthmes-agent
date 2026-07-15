"""Per-backend sync-state and pending-diff persistence (docs/PLAN.md §6).

The mirror service treats sync state as an opaque JSON blob owned by each
backend. Losing it is always safe — the next run performs a full window sync,
the mirror upserts idempotently, and ``_reconcile_tombstones`` recovers any
deletions missed while the cursor was gone — so a small JSON file under
``Settings.data_dir`` is sufficient (local-first, survives restarts, easy to
inspect and to wipe for a forced resync).

Two concurrency hazards this module must avoid (the Google 5-minute poll and
the CalDAV 10-minute poll can overlap in one process):

- **Cross-source clobber.** State and journal live in PER-SOURCE files, never
  one shared document, so a CalDAV write can never drop a concurrent Google
  write — a read-modify-write of a shared file loses the other source's update.
- **Torn temp files.** Every write goes to a UNIQUE temp name (pid + random
  token) then ``os.replace``; two concurrent writers never collide on a fixed
  ``.tmp`` sibling, and a crash mid-write never leaves a torn file at the
  target path.

The **pending-diff journal** closes an at-least-once gap. The mirror commit is
idempotent, so once it lands the diff can no longer be re-derived, yet the
cursor save (or the whole process) may still fail before the diff reaches the
``schedule_changed`` trigger. The service journals the diff BEFORE the mirror
commit and clears it only AFTER the cursor advances; a leftover journal is
replayed on the next run.
"""

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from healthmes.calendars.base import SyncState
from healthmes.store.enums import CalendarSource

__all__ = [
    "DiffPayload",
    "FilePendingDiffStore",
    "FileSyncStateStore",
    "InMemoryPendingDiffStore",
    "InMemorySyncStateStore",
    "PendingDiffStore",
    "SyncStateStore",
]

logger = logging.getLogger(__name__)

#: JSON-serializable ``SyncDiff.to_payload()`` document (journal contents).
DiffPayload = dict[str, Any]


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write ``data`` as JSON to ``path`` via a unique temp + ``os.replace``.

    The temp name embeds the pid and a random token so two writers (different
    sources, possibly different processes) never collide on a shared ``.tmp``
    sibling; ``os.replace`` makes the swap atomic so a crash mid-write never
    leaves a torn file at ``path``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(
        json.dumps(data, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    os.replace(tmp_path, path)


def _read_json_dict(path: Path) -> dict[str, Any] | None:
    """Return the JSON object at ``path``; ``None`` when missing or corrupt.

    A corrupted/unreadable file degrades to "never synced" (full resync)
    instead of failing the sync loop.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("corrupted calendar state file %s; ignoring", path)
        return None
    return data if isinstance(data, dict) else None


# --- sync-state (opaque change cursor) --------------------------------------


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
    """Per-source JSON store under a directory, each file written atomically.

    Layout: one file per source (``sync_state.google.json`` /
    ``sync_state.caldav.json``) under the store directory. Separate files mean
    overlapping polls of different sources never clobber each other, and each
    write uses a unique temp name so concurrent writers never collide (see
    :func:`_atomic_write_json`). A corrupted/unreadable file degrades to
    "never synced" (full resync).
    """

    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> "FileSyncStateStore":
        """Store under the local-first data dir (``Settings.data_dir``)."""
        return cls(Path(data_dir) / "calendars")

    @property
    def directory(self) -> Path:
        return self._dir

    def path_for(self, source: CalendarSource) -> Path:
        """The per-source state file (never shared across sources)."""
        return self._dir / f"sync_state.{source.value}.json"

    def load(self, source: CalendarSource) -> SyncState | None:
        return _read_json_dict(self.path_for(source))

    def save(self, source: CalendarSource, state: SyncState) -> None:
        _atomic_write_json(self.path_for(source), dict(state))

    def clear(self, source: CalendarSource | None = None) -> None:
        """Drop one source's state (or all) to force a full resync."""
        if source is None:
            if self._dir.exists():
                for stale in self._dir.glob("sync_state.*.json"):
                    stale.unlink(missing_ok=True)
            return
        self.path_for(source).unlink(missing_ok=True)


# --- pending-diff journal (at-least-once diff delivery) ---------------------


@runtime_checkable
class PendingDiffStore(Protocol):
    """Journals one source's not-yet-delivered ``SyncDiff`` payload.

    Written before the mirror commit and cleared after the cursor advances so
    a diff whose cursor save failed is replayed (not lost) on the next run.
    """

    def load(self, source: CalendarSource) -> DiffPayload | None:
        """Return the journaled diff payload for ``source``; ``None`` if none."""
        ...

    def save(self, source: CalendarSource, payload: DiffPayload) -> None:
        """Persist ``payload`` as the pending diff for ``source``."""
        ...

    def clear(self, source: CalendarSource) -> None:
        """Drop the pending diff for ``source`` (delivery confirmed)."""
        ...


class InMemoryPendingDiffStore:
    """Dict-backed journal for tests and one-off tooling (deep-copies on I/O)."""

    def __init__(self) -> None:
        self._payloads: dict[str, DiffPayload] = {}

    def load(self, source: CalendarSource) -> DiffPayload | None:
        payload = self._payloads.get(source.value)
        return json.loads(json.dumps(payload)) if payload is not None else None

    def save(self, source: CalendarSource, payload: DiffPayload) -> None:
        self._payloads[source.value] = json.loads(json.dumps(payload, default=str))

    def clear(self, source: CalendarSource) -> None:
        self._payloads.pop(source.value, None)


class FilePendingDiffStore:
    """Per-source JSON journal under a directory (``pending_diff.<source>.json``).

    Uses the same per-source + unique-temp-name discipline as
    :class:`FileSyncStateStore` so the two enabled backends never clobber each
    other's journal.
    """

    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> "FilePendingDiffStore":
        return cls(Path(data_dir) / "calendars")

    @property
    def directory(self) -> Path:
        return self._dir

    def path_for(self, source: CalendarSource) -> Path:
        return self._dir / f"pending_diff.{source.value}.json"

    def load(self, source: CalendarSource) -> DiffPayload | None:
        return _read_json_dict(self.path_for(source))

    def save(self, source: CalendarSource, payload: DiffPayload) -> None:
        _atomic_write_json(self.path_for(source), payload)

    def clear(self, source: CalendarSource) -> None:
        self.path_for(source).unlink(missing_ok=True)
