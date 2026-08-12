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
import re
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Protocol, runtime_checkable

from healthmes.calendars.base import SyncState
from healthmes.store.enums import CalendarSource

__all__ = [
    "CalendarSyncHealth",
    "DiffPayload",
    "FilePendingDiffStore",
    "FileSyncHealthStore",
    "FileSyncStateStore",
    "InMemoryPendingDiffStore",
    "InMemorySyncHealthStore",
    "InMemorySyncStateStore",
    "PendingDiffStore",
    "SyncCoverageKind",
    "SyncHealthOutcome",
    "SyncHealthStatus",
    "SyncHealthStore",
    "SyncStateStore",
    "sync_state_coverage",
    "with_sync_state_coverage",
]

logger = logging.getLogger(__name__)
_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_SYNC_COVERAGE_KEY = "_healthmes_sync_coverage"

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
    try:
        tmp_path.write_text(
            json.dumps(data, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _read_json_dict(path: Path) -> dict[str, Any] | None:
    """Return the JSON object at ``path``; ``None`` when missing or corrupt.

    A corrupted/unreadable file degrades to "never synced" (full resync)
    instead of failing the sync loop.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        logger.warning("calendar state file is unreadable; ignoring")
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("corrupted calendar state file; ignoring")
        return None
    return data if isinstance(data, dict) else None


# --- sync health (non-sensitive operational completeness) ------------------


class SyncHealthOutcome(StrEnum):
    """The most recently completed calendar sync attempt."""

    SUCCESS = "success"
    FAILURE = "failure"


class SyncHealthStatus(StrEnum):
    """Consumer-facing completeness state derived from sync-health history."""

    NEVER_SYNCED = "never_synced"
    EMPTY_SUCCESS = "empty_success"
    SUCCESS = "success"
    RECENT_FAILURE = "recent_failure"


class SyncCoverageKind(StrEnum):
    """How much of one calendar source a successful inbound sync observed."""

    UNKNOWN = "unknown"
    BOUNDED_WINDOW = "bounded_window"
    FULL_COLLECTION = "full_collection"


def with_sync_state_coverage(
    state: SyncState,
    *,
    kind: SyncCoverageKind,
    start: datetime | None = None,
    end: datetime | None = None,
) -> SyncState:
    """Return an opaque backend cursor carrying HealthMes coverage metadata."""
    if kind is SyncCoverageKind.BOUNDED_WINDOW:
        if start is None or end is None:
            raise ValueError("bounded calendar sync coverage requires start and end")
        normalized_start = _utc_timestamp(start, field="start")
        normalized_end = _utc_timestamp(end, field="end")
        if normalized_end <= normalized_start:
            raise ValueError("calendar sync coverage end must be after start")
        coverage: dict[str, str] = {
            "kind": kind.value,
            "start": normalized_start.isoformat(),
            "end": normalized_end.isoformat(),
        }
    else:
        if start is not None or end is not None:
            raise ValueError("only bounded calendar sync coverage accepts bounds")
        coverage = {"kind": kind.value}
    return {**state, _SYNC_COVERAGE_KEY: coverage}


def sync_state_coverage(
    state: SyncState | None,
) -> tuple[SyncCoverageKind, datetime | None, datetime | None]:
    """Decode trusted coverage metadata from an opaque backend cursor.

    Legacy or malformed cursor metadata degrades to ``UNKNOWN``. This forces a
    safe full resync instead of letting an old cursor claim that an unobserved
    empty query window is complete.
    """
    if not state:
        return SyncCoverageKind.UNKNOWN, None, None
    value = state.get(_SYNC_COVERAGE_KEY)
    if not isinstance(value, dict):
        return SyncCoverageKind.UNKNOWN, None, None
    raw_kind = value.get("kind")
    if not isinstance(raw_kind, str):
        return SyncCoverageKind.UNKNOWN, None, None
    try:
        kind = SyncCoverageKind(raw_kind)
    except ValueError:
        return SyncCoverageKind.UNKNOWN, None, None
    if kind is not SyncCoverageKind.BOUNDED_WINDOW:
        if kind is SyncCoverageKind.FULL_COLLECTION:
            return kind, None, None
        return SyncCoverageKind.UNKNOWN, None, None
    raw_start = value.get("start")
    raw_end = value.get("end")
    if not isinstance(raw_start, str) or not isinstance(raw_end, str):
        return SyncCoverageKind.UNKNOWN, None, None
    try:
        start = _utc_timestamp(
            datetime.fromisoformat(raw_start),
            field="start",
        )
        end = _utc_timestamp(
            datetime.fromisoformat(raw_end),
            field="end",
        )
    except ValueError:
        return SyncCoverageKind.UNKNOWN, None, None
    if end <= start:
        return SyncCoverageKind.UNKNOWN, None, None
    return kind, start, end


def _utc_timestamp(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class CalendarSyncHealth:
    """Non-sensitive per-source calendar sync history.

    This state intentionally stores no event title, event body, provider
    response, exception message, URL, account identifier, or credential.
    ``last_success_event_count`` is only the number of locally mirrored rows;
    it distinguishes a successful empty calendar from a source that has never
    completed a sync.
    """

    source: CalendarSource
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error_code: str | None = None
    last_success_event_count: int | None = None
    latest_outcome: SyncHealthOutcome | None = None
    coverage_kind: SyncCoverageKind = SyncCoverageKind.UNKNOWN
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    writeback_last_attempt_at: datetime | None = None
    writeback_last_success_at: datetime | None = None
    writeback_last_failure_at: datetime | None = None
    writeback_last_error_code: str | None = None
    writeback_attempted_count: int | None = None
    writeback_succeeded_count: int | None = None
    writeback_failed_count: int | None = None

    def __post_init__(self) -> None:
        for field in (
            "last_attempt_at",
            "last_success_at",
            "last_failure_at",
            "coverage_start",
            "coverage_end",
            "writeback_last_attempt_at",
            "writeback_last_success_at",
            "writeback_last_failure_at",
        ):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(
                    self,
                    field,
                    _utc_timestamp(value, field=field),
                )
        if self.last_error_code is not None and not _ERROR_CODE_PATTERN.fullmatch(
            self.last_error_code
        ):
            raise ValueError("last_error_code must be a stable machine identifier")
        if self.last_success_event_count is not None:
            if self.last_success_event_count < 0:
                raise ValueError("last_success_event_count must be non-negative")
            if self.last_success_at is None:
                raise ValueError(
                    "last_success_event_count requires last_success_at"
                )
        if self.last_failure_at is None and self.last_error_code is not None:
            raise ValueError("last_error_code requires last_failure_at")
        if self.last_failure_at is not None and self.last_error_code is None:
            raise ValueError("last_failure_at requires last_error_code")
        if (
            self.writeback_last_error_code is not None
            and not _ERROR_CODE_PATTERN.fullmatch(
                self.writeback_last_error_code
            )
        ):
            raise ValueError(
                "writeback_last_error_code must be a stable machine identifier"
            )
        if (
            self.writeback_last_failure_at is None
            and self.writeback_last_error_code is not None
        ):
            raise ValueError(
                "writeback_last_error_code requires writeback_last_failure_at"
            )
        if (
            self.writeback_last_failure_at is not None
            and self.writeback_last_error_code is None
        ):
            raise ValueError(
                "writeback_last_failure_at requires writeback_last_error_code"
            )
        if self.coverage_kind is SyncCoverageKind.BOUNDED_WINDOW:
            if self.coverage_start is None or self.coverage_end is None:
                raise ValueError(
                    "bounded calendar coverage requires start and end"
                )
            if self.coverage_end <= self.coverage_start:
                raise ValueError(
                    "calendar coverage end must be after start"
                )
        elif self.coverage_start is not None or self.coverage_end is not None:
            raise ValueError(
                "calendar coverage bounds require bounded_window"
            )
        for field in (
            "writeback_attempted_count",
            "writeback_succeeded_count",
            "writeback_failed_count",
        ):
            value = getattr(self, field)
            if value is not None and value < 0:
                raise ValueError(f"{field} must be non-negative")
        if (
            self.writeback_attempted_count is not None
            and self.writeback_succeeded_count is not None
            and self.writeback_failed_count is not None
            and self.writeback_succeeded_count
            + self.writeback_failed_count
            > self.writeback_attempted_count
        ):
            raise ValueError(
                "writeback result counts exceed attempted count"
            )
        if (
            self.latest_outcome is SyncHealthOutcome.SUCCESS
            and self.last_success_at is None
        ):
            raise ValueError("success outcome requires last_success_at")
        if (
            self.latest_outcome is SyncHealthOutcome.FAILURE
            and self.last_failure_at is None
        ):
            raise ValueError("failure outcome requires last_failure_at")

    @property
    def status(self) -> SyncHealthStatus:
        if self.latest_outcome is SyncHealthOutcome.FAILURE:
            return SyncHealthStatus.RECENT_FAILURE
        if self.latest_outcome is SyncHealthOutcome.SUCCESS:
            if self.last_success_event_count == 0:
                return SyncHealthStatus.EMPTY_SUCCESS
            return SyncHealthStatus.SUCCESS
        return SyncHealthStatus.NEVER_SYNCED

    def covers(self, start: datetime, end: datetime) -> bool:
        """Return whether the last inbound success observed the whole query."""
        query_start = _utc_timestamp(start, field="start")
        query_end = _utc_timestamp(end, field="end")
        if query_end <= query_start:
            raise ValueError("query coverage end must be after start")
        if self.latest_outcome is not SyncHealthOutcome.SUCCESS:
            return False
        if self.coverage_kind is SyncCoverageKind.FULL_COLLECTION:
            return True
        return (
            self.coverage_kind is SyncCoverageKind.BOUNDED_WINDOW
            and self.coverage_start is not None
            and self.coverage_end is not None
            and self.coverage_start <= query_start
            and self.coverage_end >= query_end
        )

    def to_payload(self) -> dict[str, object]:
        def encoded(value: datetime | None) -> str | None:
            return value.isoformat() if value is not None else None

        return {
            "version": 2,
            "source": self.source.value,
            "last_attempt_at": encoded(self.last_attempt_at),
            "last_success_at": encoded(self.last_success_at),
            "last_failure_at": encoded(self.last_failure_at),
            "last_error_code": self.last_error_code,
            "last_success_event_count": self.last_success_event_count,
            "latest_outcome": (
                self.latest_outcome.value
                if self.latest_outcome is not None
                else None
            ),
            "coverage_kind": self.coverage_kind.value,
            "coverage_start": encoded(self.coverage_start),
            "coverage_end": encoded(self.coverage_end),
            "writeback_last_attempt_at": encoded(
                self.writeback_last_attempt_at
            ),
            "writeback_last_success_at": encoded(
                self.writeback_last_success_at
            ),
            "writeback_last_failure_at": encoded(
                self.writeback_last_failure_at
            ),
            "writeback_last_error_code": self.writeback_last_error_code,
            "writeback_attempted_count": self.writeback_attempted_count,
            "writeback_succeeded_count": self.writeback_succeeded_count,
            "writeback_failed_count": self.writeback_failed_count,
        }

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        expected_source: CalendarSource,
    ) -> "CalendarSyncHealth":
        version = payload.get("version")
        if version not in {1, 2}:
            raise ValueError("unsupported calendar sync-health version")
        if payload.get("source") != expected_source.value:
            raise ValueError("calendar sync-health source mismatch")

        def decoded(field: str) -> datetime | None:
            value = payload.get(field)
            if value is None:
                return None
            if not isinstance(value, str):
                raise ValueError(f"{field} must be an ISO timestamp")
            return datetime.fromisoformat(value)

        raw_error = payload.get("last_error_code")
        if raw_error is not None and not isinstance(raw_error, str):
            raise ValueError("last_error_code must be a string")
        raw_count = payload.get("last_success_event_count")
        if raw_count is not None and (
            not isinstance(raw_count, int) or isinstance(raw_count, bool)
        ):
            raise ValueError("last_success_event_count must be an integer")
        raw_outcome = payload.get("latest_outcome")
        outcome = (
            SyncHealthOutcome(raw_outcome)
            if isinstance(raw_outcome, str)
            else None
        )
        if raw_outcome is not None and outcome is None:
            raise ValueError("latest_outcome must be a known outcome")
        raw_coverage_kind = (
            payload.get("coverage_kind")
            if version == 2
            else SyncCoverageKind.UNKNOWN.value
        )
        if not isinstance(raw_coverage_kind, str):
            raise ValueError("coverage_kind must be a string")
        coverage_kind = SyncCoverageKind(raw_coverage_kind)

        def decoded_count(field: str) -> int | None:
            value = payload.get(field)
            if value is None:
                return None
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{field} must be an integer")
            return value

        raw_writeback_error = payload.get("writeback_last_error_code")
        if (
            raw_writeback_error is not None
            and not isinstance(raw_writeback_error, str)
        ):
            raise ValueError(
                "writeback_last_error_code must be a string"
            )
        return cls(
            source=expected_source,
            last_attempt_at=decoded("last_attempt_at"),
            last_success_at=decoded("last_success_at"),
            last_failure_at=decoded("last_failure_at"),
            last_error_code=raw_error,
            last_success_event_count=raw_count,
            latest_outcome=outcome,
            coverage_kind=coverage_kind,
            coverage_start=(
                decoded("coverage_start") if version == 2 else None
            ),
            coverage_end=(
                decoded("coverage_end") if version == 2 else None
            ),
            writeback_last_attempt_at=(
                decoded("writeback_last_attempt_at")
                if version == 2
                else None
            ),
            writeback_last_success_at=(
                decoded("writeback_last_success_at")
                if version == 2
                else None
            ),
            writeback_last_failure_at=(
                decoded("writeback_last_failure_at")
                if version == 2
                else None
            ),
            writeback_last_error_code=raw_writeback_error,
            writeback_attempted_count=decoded_count(
                "writeback_attempted_count"
            ),
            writeback_succeeded_count=decoded_count(
                "writeback_succeeded_count"
            ),
            writeback_failed_count=decoded_count(
                "writeback_failed_count"
            ),
        )


def _initial_sync_health(source: CalendarSource) -> CalendarSyncHealth:
    return CalendarSyncHealth(source=source)


def _record_attempt(
    state: CalendarSyncHealth | None,
    source: CalendarSource,
    at: datetime,
) -> CalendarSyncHealth:
    current = state or _initial_sync_health(source)
    return replace(current, last_attempt_at=at)


def _record_success(
    state: CalendarSyncHealth | None,
    source: CalendarSource,
    at: datetime,
    event_count: int | None,
    coverage_kind: SyncCoverageKind,
    coverage_start: datetime | None,
    coverage_end: datetime | None,
) -> CalendarSyncHealth:
    current = state or _initial_sync_health(source)
    return replace(
        current,
        last_success_at=at,
        last_success_event_count=event_count,
        latest_outcome=SyncHealthOutcome.SUCCESS,
        coverage_kind=coverage_kind,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
    )


def _record_failure(
    state: CalendarSyncHealth | None,
    source: CalendarSource,
    at: datetime,
    error_code: str,
) -> CalendarSyncHealth:
    current = state or _initial_sync_health(source)
    return replace(
        current,
        last_failure_at=at,
        last_error_code=error_code,
        latest_outcome=SyncHealthOutcome.FAILURE,
    )


def _record_writeback_attempt(
    state: CalendarSyncHealth | None,
    source: CalendarSource,
    at: datetime,
    attempted_count: int,
) -> CalendarSyncHealth:
    current = state or _initial_sync_health(source)
    return replace(
        current,
        writeback_last_attempt_at=at,
        writeback_attempted_count=attempted_count,
        writeback_succeeded_count=None,
        writeback_failed_count=None,
    )


def _record_writeback_result(
    state: CalendarSyncHealth | None,
    source: CalendarSource,
    at: datetime,
    *,
    attempted_count: int,
    succeeded_count: int,
    failed_count: int,
    error_code: str | None,
) -> CalendarSyncHealth:
    current = state or _initial_sync_health(source)
    failed = error_code is not None or failed_count > 0
    resolved_error = (
        error_code
        if error_code is not None
        else "calendar_writeback_partial_failure"
        if failed_count
        else None
    )
    return replace(
        current,
        writeback_last_success_at=(
            at
            if not failed
            else current.writeback_last_success_at
        ),
        writeback_last_failure_at=(
            at
            if failed
            else current.writeback_last_failure_at
        ),
        writeback_last_error_code=(
            resolved_error
            if failed
            else current.writeback_last_error_code
        ),
        writeback_attempted_count=attempted_count,
        writeback_succeeded_count=succeeded_count,
        writeback_failed_count=failed_count,
    )


@runtime_checkable
class SyncHealthStore(Protocol):
    """Stores non-sensitive per-source calendar sync completeness."""

    def load(self, source: CalendarSource) -> CalendarSyncHealth | None:
        """Return health history, or ``None`` when no valid history exists."""
        ...

    def record_attempt(self, source: CalendarSource, at: datetime) -> None:
        """Record an attempt before backend construction or provider I/O."""
        ...

    def record_success(
        self,
        source: CalendarSource,
        at: datetime,
        *,
        event_count: int | None,
        coverage_kind: SyncCoverageKind = SyncCoverageKind.UNKNOWN,
        coverage_start: datetime | None = None,
        coverage_end: datetime | None = None,
    ) -> None:
        """Record a completed sync after its database commit."""
        ...

    def record_writeback_attempt(
        self,
        source: CalendarSource,
        at: datetime,
        *,
        attempted_count: int,
    ) -> None:
        ...

    def record_writeback_result(
        self,
        source: CalendarSource,
        at: datetime,
        *,
        attempted_count: int,
        succeeded_count: int,
        failed_count: int,
        error_code: str | None = None,
    ) -> None:
        ...

    def record_failure(
        self,
        source: CalendarSource,
        at: datetime,
        *,
        error_code: str,
    ) -> None:
        """Record a completed failure using a stable, sanitized code."""
        ...


class InMemorySyncHealthStore:
    """Thread-safe in-memory sync-health store for tests and one-off tools."""

    def __init__(self) -> None:
        self._states: dict[str, CalendarSyncHealth] = {}
        self._locks = {source: RLock() for source in CalendarSource}

    def load(self, source: CalendarSource) -> CalendarSyncHealth | None:
        with self._locks[source]:
            return self._states.get(source.value)

    def record_attempt(self, source: CalendarSource, at: datetime) -> None:
        with self._locks[source]:
            self._states[source.value] = _record_attempt(
                self._states.get(source.value),
                source,
                at,
            )

    def record_success(
        self,
        source: CalendarSource,
        at: datetime,
        *,
        event_count: int | None,
        coverage_kind: SyncCoverageKind = SyncCoverageKind.UNKNOWN,
        coverage_start: datetime | None = None,
        coverage_end: datetime | None = None,
    ) -> None:
        with self._locks[source]:
            self._states[source.value] = _record_success(
                self._states.get(source.value),
                source,
                at,
                event_count,
                coverage_kind,
                coverage_start,
                coverage_end,
            )

    def record_writeback_attempt(
        self,
        source: CalendarSource,
        at: datetime,
        *,
        attempted_count: int,
    ) -> None:
        with self._locks[source]:
            self._states[source.value] = _record_writeback_attempt(
                self._states.get(source.value),
                source,
                at,
                attempted_count,
            )

    def record_writeback_result(
        self,
        source: CalendarSource,
        at: datetime,
        *,
        attempted_count: int,
        succeeded_count: int,
        failed_count: int,
        error_code: str | None = None,
    ) -> None:
        with self._locks[source]:
            self._states[source.value] = _record_writeback_result(
                self._states.get(source.value),
                source,
                at,
                attempted_count=attempted_count,
                succeeded_count=succeeded_count,
                failed_count=failed_count,
                error_code=error_code,
            )

    def record_failure(
        self,
        source: CalendarSource,
        at: datetime,
        *,
        error_code: str,
    ) -> None:
        with self._locks[source]:
            self._states[source.value] = _record_failure(
                self._states.get(source.value),
                source,
                at,
                error_code,
            )


class FileSyncHealthStore:
    """Atomic per-source sync-health files under the local-first data dir."""

    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)
        self._locks = {source: RLock() for source in CalendarSource}

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> "FileSyncHealthStore":
        return cls(Path(data_dir) / "calendars")

    @property
    def directory(self) -> Path:
        return self._dir

    def path_for(self, source: CalendarSource) -> Path:
        return self._dir / f"sync_health.{source.value}.json"

    def _load_unlocked(
        self,
        source: CalendarSource,
    ) -> CalendarSyncHealth | None:
        payload = _read_json_dict(self.path_for(source))
        if payload is None:
            return None
        try:
            return CalendarSyncHealth.from_payload(
                payload,
                expected_source=source,
            )
        except (TypeError, ValueError):
            logger.warning("invalid calendar sync-health state; ignoring")
            return None

    def load(self, source: CalendarSource) -> CalendarSyncHealth | None:
        with self._locks[source]:
            return self._load_unlocked(source)

    def record_attempt(self, source: CalendarSource, at: datetime) -> None:
        with self._locks[source]:
            state = _record_attempt(self._load_unlocked(source), source, at)
            _atomic_write_json(self.path_for(source), state.to_payload())

    def record_success(
        self,
        source: CalendarSource,
        at: datetime,
        *,
        event_count: int | None,
        coverage_kind: SyncCoverageKind = SyncCoverageKind.UNKNOWN,
        coverage_start: datetime | None = None,
        coverage_end: datetime | None = None,
    ) -> None:
        with self._locks[source]:
            state = _record_success(
                self._load_unlocked(source),
                source,
                at,
                event_count,
                coverage_kind,
                coverage_start,
                coverage_end,
            )
            _atomic_write_json(self.path_for(source), state.to_payload())

    def record_writeback_attempt(
        self,
        source: CalendarSource,
        at: datetime,
        *,
        attempted_count: int,
    ) -> None:
        with self._locks[source]:
            state = _record_writeback_attempt(
                self._load_unlocked(source),
                source,
                at,
                attempted_count,
            )
            _atomic_write_json(self.path_for(source), state.to_payload())

    def record_writeback_result(
        self,
        source: CalendarSource,
        at: datetime,
        *,
        attempted_count: int,
        succeeded_count: int,
        failed_count: int,
        error_code: str | None = None,
    ) -> None:
        with self._locks[source]:
            state = _record_writeback_result(
                self._load_unlocked(source),
                source,
                at,
                attempted_count=attempted_count,
                succeeded_count=succeeded_count,
                failed_count=failed_count,
                error_code=error_code,
            )
            _atomic_write_json(self.path_for(source), state.to_payload())

    def record_failure(
        self,
        source: CalendarSource,
        at: datetime,
        *,
        error_code: str,
    ) -> None:
        with self._locks[source]:
            state = _record_failure(
                self._load_unlocked(source),
                source,
                at,
                error_code,
            )
            _atomic_write_json(self.path_for(source), state.to_payload())


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
