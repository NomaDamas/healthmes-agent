"""Calendar mirror service (docs/PLAN.md section 6).

Pulls changes from every configured :class:`CalendarBackend`, upserts them
into ``calendar_event_mirror``, persists each backend's sync state, and
returns a structured :class:`SyncDiff` that the trigger engine turns into the
``schedule_changed`` proactive alert.

Ownership split (conflict philosophy that avoids the sync swamp):

- The external calendar wins for **every** event: mirror rows are always
  overwritten with the external state, including agent-created events a user
  edited externally (those surface in ``SyncDiff.agent_modified`` so the
  agent can re-plan and alert — the product behavior the plan wants).
- The agent may only create tagged events and move/delete rows with
  ``is_agent_created=True``; anything else raises :class:`OwnershipError`
  before any backend call (backends additionally verify the tag remotely).

Bootstrap semantics: the first-ever sync of a backend (no persisted sync
state) adopts the whole window silently — with no previous state there are
no "changes" to report, and reporting them would fire one giant spurious
alert (docs/PLAN.md section 11: alert noise is the top product risk).
"""

import logging
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.calendars.base import (
    CalendarBackend,
    CalendarError,
    EventDraft,
    EventNotFoundError,
    ExternalEvent,
    OwnershipError,
    coerce_utc,
    ensure_utc,
)
from healthmes.calendars.state import SyncStateStore
from healthmes.store.enums import CalendarSource
from healthmes.store.models import CalendarEventMirror, Task

__all__ = [
    "CalendarMirrorService",
    "ChangeKind",
    "EventChange",
    "SyncDiff",
]

logger = logging.getLogger(__name__)


class ChangeKind(StrEnum):
    """What externally happened to a mirrored event."""

    CREATED = "created"
    MOVED = "moved"  # start/end changed
    MODIFIED = "modified"  # content changed, times identical
    DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class EventChange:
    """One externally-observed change, with before/after times for re-planning."""

    calendar_source: CalendarSource
    external_id: str
    kind: ChangeKind
    summary: str | None
    is_agent_created: bool
    old_start_at: datetime | None = None
    old_end_at: datetime | None = None
    new_start_at: datetime | None = None
    new_end_at: datetime | None = None

    def to_payload(self) -> dict[str, object]:
        """JSON-safe dict for trigger payloads / webhook bodies."""
        payload = asdict(self)
        payload["calendar_source"] = self.calendar_source.value
        payload["kind"] = self.kind.value
        for key in ("old_start_at", "old_end_at", "new_start_at", "new_end_at"):
            value = payload[key]
            payload[key] = value.isoformat() if isinstance(value, datetime) else None
        return payload


@dataclass(slots=True)
class SyncDiff:
    """Structured mirror diff consumed by the ``schedule_changed`` trigger.

    ``created``/``moved``/``deleted`` carry non-agent events (the external
    world changed around the plan); ``agent_modified`` carries agent-created
    events a user changed or removed externally (external wins — the agent
    must re-plan its own blocks).
    """

    created: list[EventChange] = field(default_factory=list)
    moved: list[EventChange] = field(default_factory=list)
    deleted: list[EventChange] = field(default_factory=list)
    agent_modified: list[EventChange] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.created or self.moved or self.deleted or self.agent_modified)

    def extend(self, other: "SyncDiff") -> None:
        self.created.extend(other.created)
        self.moved.extend(other.moved)
        self.deleted.extend(other.deleted)
        self.agent_modified.extend(other.agent_modified)

    def to_payload(self) -> dict[str, object]:
        """JSON-safe dict for trigger payloads / webhook bodies."""
        return {
            "created": [change.to_payload() for change in self.created],
            "moved": [change.to_payload() for change in self.moved],
            "deleted": [change.to_payload() for change in self.deleted],
            "agent_modified": [change.to_payload() for change in self.agent_modified],
        }


class CalendarMirrorService:
    """Syncs external calendars into ``calendar_event_mirror`` and guards writes.

    The service owns transaction boundaries: every public method commits on
    success. Run it inside ``healthmes.store.session_scope`` (or hand it a
    dedicated session) from the poll loop; on errors the session is left to
    the caller's rollback.
    """

    def __init__(
        self,
        session: Session,
        backends: Iterable[CalendarBackend],
        state_store: SyncStateStore,
    ) -> None:
        self._session = session
        self._backends: dict[CalendarSource, CalendarBackend] = {}
        for backend in backends:
            if backend.source in self._backends:
                raise CalendarError(f"duplicate backend for source {backend.source.value!r}")
            self._backends[backend.source] = backend
        self._state_store = state_store

    # -- pull / diff -------------------------------------------------------

    def sync_all(self) -> SyncDiff:
        """Sync every configured backend; merged diff in registration order."""
        diff = SyncDiff()
        for backend in self._backends.values():
            diff.extend(self.sync_backend(backend))
        return diff

    def sync_backend(self, backend: CalendarBackend) -> SyncDiff:
        """Pull one backend's changes, upsert the mirror, persist sync state."""
        source = backend.source
        previous_state = self._state_store.load(source)
        bootstrap = previous_state is None
        events, new_state = backend.list_changes(previous_state)

        diff = SyncDiff()
        for event in events:
            if event.deleted:
                self._apply_deletion(source, event, diff)
            else:
                self._apply_upsert(source, event, diff, bootstrap=bootstrap)

        # Commit rows first, then persist the cursor: a crash in between
        # re-delivers the same changes, and upserts are idempotent.
        self._session.commit()
        self._state_store.save(source, new_state)
        if diff.has_changes:
            logger.info(
                "calendar sync %s: +%d created, %d moved, -%d deleted, %d agent-modified",
                source.value,
                len(diff.created),
                len(diff.moved),
                len(diff.deleted),
                len(diff.agent_modified),
            )
        return diff

    def _apply_upsert(
        self,
        source: CalendarSource,
        event: ExternalEvent,
        diff: SyncDiff,
        *,
        bootstrap: bool,
    ) -> None:
        assert event.start_at is not None and event.end_at is not None  # live event
        row = self._get_row(source, event.external_id)
        if row is None:
            self._session.add(
                CalendarEventMirror(
                    external_id=event.external_id,
                    calendar_source=source,
                    summary=event.summary,
                    start_at=event.start_at,
                    end_at=event.end_at,
                    is_agent_created=event.is_agent_created,
                    agent_task_id=self._resolve_task_id(event.agent_task_id),
                    etag=event.etag,
                )
            )
            # Agent-tagged events without a row are re-adopted silently (the
            # row normally pre-exists from create_agent_event); bootstrap
            # adopts everything silently.
            if not bootstrap and not event.is_agent_created:
                diff.created.append(
                    EventChange(
                        calendar_source=source,
                        external_id=event.external_id,
                        kind=ChangeKind.CREATED,
                        summary=event.summary,
                        is_agent_created=False,
                        new_start_at=event.start_at,
                        new_end_at=event.end_at,
                    )
                )
            return

        old_start = coerce_utc(row.start_at)
        old_end = coerce_utc(row.end_at)
        moved = old_start != event.start_at or old_end != event.end_at
        content_changed = (row.summary or None) != (event.summary or None)

        if not moved and not content_changed:
            # Byte-identical re-delivery (410 full resync, lost sync-state
            # file, crash between commit and cursor save): write NOTHING.
            # Assigning equal values still dirties the row on sqlite (stored
            # datetimes load naive, event values are aware), and any UPDATE
            # bumps updated_at — which the trigger sweep reads as an external
            # change (triggers.py::_load_schedule_changes contract:
            # updated_at moves only when the event actually changed).
            return

        # External wins for every event, including agent-created ones.
        row.summary = event.summary
        row.start_at = event.start_at
        row.end_at = event.end_at
        row.etag = event.etag
        if row.agent_task_id is None and event.agent_task_id is not None:
            row.agent_task_id = self._resolve_task_id(event.agent_task_id)

        change = EventChange(
            calendar_source=source,
            external_id=event.external_id,
            kind=ChangeKind.MOVED if moved else ChangeKind.MODIFIED,
            summary=event.summary,
            is_agent_created=row.is_agent_created,
            old_start_at=old_start,
            old_end_at=old_end,
            new_start_at=event.start_at,
            new_end_at=event.end_at,
        )
        if row.is_agent_created:
            diff.agent_modified.append(change)
        elif moved:
            diff.moved.append(change)
        # Non-agent content-only edits are mirrored silently: they do not
        # affect the schedule, so they must not feed the trigger.

    def _apply_deletion(
        self, source: CalendarSource, event: ExternalEvent, diff: SyncDiff
    ) -> None:
        row = self._get_row(source, event.external_id)
        if row is None:
            return  # never mirrored (or already pruned) — nothing changed for us
        change = EventChange(
            calendar_source=source,
            external_id=event.external_id,
            kind=ChangeKind.DELETED,
            summary=row.summary,
            is_agent_created=row.is_agent_created,
            old_start_at=coerce_utc(row.start_at),
            old_end_at=coerce_utc(row.end_at),
        )
        self._session.delete(row)
        if change.is_agent_created:
            diff.agent_modified.append(change)
        else:
            diff.deleted.append(change)

    # -- ownership-guarded agent writes -------------------------------------

    def create_agent_event(
        self, source: CalendarSource, draft: EventDraft
    ) -> CalendarEventMirror:
        """Create a tagged agent block remotely and mirror it immediately."""
        backend = self._backend_for(source)
        created = backend.create_event(draft)
        assert created.start_at is not None and created.end_at is not None
        row = CalendarEventMirror(
            external_id=created.external_id,
            calendar_source=source,
            summary=created.summary,
            start_at=created.start_at,
            end_at=created.end_at,
            is_agent_created=True,
            agent_task_id=self._resolve_task_id(draft.agent_task_id),
            etag=created.etag,
        )
        self._session.add(row)
        self._session.commit()
        return row

    def move_agent_event(
        self,
        source: CalendarSource,
        external_id: str,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> CalendarEventMirror:
        """Move an agent-created block; refuses to touch external events."""
        start_at = ensure_utc(start_at)
        end_at = ensure_utc(end_at)
        if end_at <= start_at:
            raise ValueError("end_at must be after start_at")
        row = self._get_owned_row(source, external_id)
        updated = self._backend_for(source).update_event(
            external_id, start_at=start_at, end_at=end_at
        )
        row.start_at = updated.start_at or start_at
        row.end_at = updated.end_at or end_at
        row.etag = updated.etag
        self._session.commit()
        return row

    def delete_agent_event(self, source: CalendarSource, external_id: str) -> None:
        """Delete an agent-created block; refuses to touch external events."""
        row = self._get_owned_row(source, external_id)
        self._backend_for(source).delete_event(external_id)
        self._session.delete(row)
        self._session.commit()

    # -- internals -----------------------------------------------------------

    def _get_owned_row(self, source: CalendarSource, external_id: str) -> CalendarEventMirror:
        row = self._get_row(source, external_id)
        if row is None:
            raise EventNotFoundError(
                f"no mirrored {source.value} event with external_id {external_id!r}"
            )
        if not row.is_agent_created:
            raise OwnershipError(
                f"{source.value} event {external_id!r} was not created by the agent; "
                "the external calendar owns it (docs/PLAN.md section 6)"
            )
        return row

    def _resolve_task_id(self, task_id: uuid.UUID | None) -> uuid.UUID | None:
        """Keep the task FK only when the task exists locally.

        Ownership tags travel through external systems; a tag pointing at a
        task we no longer (or never) had must not break the sync transaction.
        """
        if task_id is None:
            return None
        return task_id if self._session.get(Task, task_id) is not None else None

    def _get_row(self, source: CalendarSource, external_id: str) -> CalendarEventMirror | None:
        statement = select(CalendarEventMirror).where(
            CalendarEventMirror.calendar_source == source,
            CalendarEventMirror.external_id == external_id,
        )
        return self._session.execute(statement).scalar_one_or_none()

    def _backend_for(self, source: CalendarSource) -> CalendarBackend:
        backend = self._backends.get(source)
        if backend is None:
            raise CalendarError(f"no calendar backend configured for source {source.value!r}")
        return backend
