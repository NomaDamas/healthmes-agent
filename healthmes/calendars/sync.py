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

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.calendars.base import (
    CalendarBackend,
    CalendarConflictError,
    CalendarError,
    CalendarEventIdentity,
    EventDraft,
    EventNotFoundError,
    ExternalEvent,
    OwnershipError,
    calendar_identity_external_id,
    coerce_utc,
    ensure_utc,
    parse_calendar_identity,
)
from healthmes.calendars.planned_sleep_replacement import (
    proposal_for_planner_event,
)
from healthmes.calendars.sleep_mirror import (
    SLEEP_CREATE_PENDING_STATUS,
    SLEEP_UPDATE_PENDING_STATUS,
)
from healthmes.calendars.state import (
    PendingDiffStore,
    SyncStateStore,
    sync_state_account_generation,
    with_sync_state_account_generation,
)
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
        """JSON-safe dict for trigger payloads / webhook bodies / the journal."""
        payload = asdict(self)
        payload["calendar_source"] = self.calendar_source.value
        payload["kind"] = self.kind.value
        for key in ("old_start_at", "old_end_at", "new_start_at", "new_end_at"):
            value = payload[key]
            payload[key] = value.isoformat() if isinstance(value, datetime) else None
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "EventChange":
        """Inverse of :meth:`to_payload` (journal replay round-trip)."""

        def _dt(key: str) -> datetime | None:
            value = payload.get(key)
            return datetime.fromisoformat(value) if isinstance(value, str) else None

        summary = payload.get("summary")
        return cls(
            calendar_source=CalendarSource(payload["calendar_source"]),
            external_id=str(payload["external_id"]),
            kind=ChangeKind(payload["kind"]),
            summary=summary if isinstance(summary, str) else None,
            is_agent_created=bool(payload["is_agent_created"]),
            old_start_at=_dt("old_start_at"),
            old_end_at=_dt("old_end_at"),
            new_start_at=_dt("new_start_at"),
            new_end_at=_dt("new_end_at"),
        )


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
        """JSON-safe dict for trigger payloads / webhook bodies / the journal."""
        return {
            "created": [change.to_payload() for change in self.created],
            "moved": [change.to_payload() for change in self.moved],
            "deleted": [change.to_payload() for change in self.deleted],
            "agent_modified": [change.to_payload() for change in self.agent_modified],
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "SyncDiff":
        """Inverse of :meth:`to_payload` — rebuild a diff from the journal."""

        def _changes(key: str) -> list[EventChange]:
            raw = payload.get(key) or []
            return [EventChange.from_payload(item) for item in raw]  # type: ignore[arg-type]

        return cls(
            created=_changes("created"),
            moved=_changes("moved"),
            deleted=_changes("deleted"),
            agent_modified=_changes("agent_modified"),
        )


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
        pending_store: PendingDiffStore | None = None,
        *,
        account_generation: str | None = None,
    ) -> None:
        self._session = session
        self._backends: dict[CalendarSource, CalendarBackend] = {}
        for backend in backends:
            if backend.source in self._backends:
                raise CalendarError(f"duplicate backend for source {backend.source.value!r}")
            self._backends[backend.source] = backend
        self._state_store = state_store
        self._pending_store = pending_store
        self._account_generation = account_generation

    @property
    def account_generation(self) -> str | None:
        return self._account_generation

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
        if (
            self._account_generation is not None
            and sync_state_account_generation(previous_state)
            != self._account_generation
        ):
            previous_state = None
        bootstrap = previous_state is None
        # Carry forward any diff a previous run journaled but never delivered
        # (its cursor save failed after the idempotent mirror commit landed).
        replayed = self._load_pending(source)
        events, new_state = backend.list_changes(previous_state)

        diff = SyncDiff()
        seen_ids: set[str] = set()
        for event in events:
            seen_ids.add(event.external_id)
            if event.deleted:
                self._apply_deletion(source, event, diff)
            else:
                self._apply_upsert(source, event, diff, bootstrap=bootstrap)

        if bootstrap:
            # A lost/emptied cursor forces a full-window fetch; mirror rows the
            # provider no longer returns were deleted (or slid out of the
            # scheduling window) while we had no cursor — emit tombstones so the
            # deletion is not lost forever (docs/PLAN.md §6).
            self._reconcile_tombstones(source, seen_ids, diff)

        if replayed is not None:
            merged = SyncDiff()
            merged.extend(replayed)
            merged.extend(diff)
            diff = merged

        # Journal the diff BEFORE the mirror commit: once the idempotent upserts
        # land, the diff can no longer be re-derived, so a crash between the
        # commit and the cursor save would otherwise lose a deletion/move the
        # trigger must consume. Cleared only AFTER the cursor advances, so a
        # failed cursor save replays it next run (at-least-once; the trigger
        # dedups replays).
        if diff.has_changes:
            self._save_pending(source, diff)
        self._session.commit()
        if self._account_generation is not None:
            new_state = with_sync_state_account_generation(
                new_state,
                self._account_generation,
            )
        self._state_store.save(source, new_state)
        self._clear_pending(source)
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

    def _load_pending(self, source: CalendarSource) -> SyncDiff | None:
        if self._pending_store is None:
            return None
        payload = self._pending_store.load(source)
        if (
            payload
            and self._account_generation is not None
            and payload.get("_healthmes_account_generation")
            != self._account_generation
        ):
            self._pending_store.clear(source)
            return None
        return SyncDiff.from_payload(payload) if payload else None

    def _save_pending(self, source: CalendarSource, diff: SyncDiff) -> None:
        if self._pending_store is not None:
            payload = diff.to_payload()
            if self._account_generation is not None:
                payload["_healthmes_account_generation"] = (
                    self._account_generation
                )
            self._pending_store.save(source, payload)

    def _clear_pending(self, source: CalendarSource) -> None:
        if self._pending_store is not None:
            self._pending_store.clear(source)

    def _apply_upsert(
        self,
        source: CalendarSource,
        event: ExternalEvent,
        diff: SyncDiff,
        *,
        bootstrap: bool,
    ) -> None:
        assert event.start_at is not None and event.end_at is not None  # live event
        resolved_task_id = self._resolve_task_id(event.agent_task_id)
        row = self._get_row(source, event.external_id)
        canonical_identity = (
            event.identity is not None
            and event.external_id
            == calendar_identity_external_id(source, event.identity)
        )
        if (
            canonical_identity
            and event.identity is not None
            and event.identity.source == "planner"
        ):
            canonical_identity = (
                proposal_for_planner_event(
                    self._session,
                    event.identity,
                    agent_task_id=event.agent_task_id,
                    start_at=event.start_at,
                    end_at=event.end_at,
                )
                is not None
            )
        # An incoming provider event is trusted as agent-created ONLY when it
        # carries the ownership tag AND a task id that resolves to a local Task
        # row. A forged tag alone (or a tag whose task id we never had) must
        # never grant the agent write authority over an event the external
        # calendar really owns — otherwise a hand-crafted ``healthmes=1`` on
        # someone else's meeting would let the agent move/delete it.
        trusted_agent = bool(event.is_agent_created) and (
            canonical_identity
            if event.identity is not None
            else resolved_task_id is not None
        )
        row = self._lock_current_row(source, event.external_id)
        if row is not None and _pending_sleep_intent(row):
            # Reconciliation owns every desired field while a provider write is
            # pending. The next reconciliation pass re-reads the remote event.
            return

        if trusted_agent and event.identity is not None:
            self._quarantine_identity_conflicts(
                source,
                event.identity,
                event.external_id,
            )

        if row is None:
            self._session.add(
                CalendarEventMirror(
                    external_id=event.external_id,
                    calendar_source=source,
                    connection_generation=self._account_generation,
                    summary=event.summary,
                    start_at=event.start_at,
                    end_at=event.end_at,
                    is_agent_created=trusted_agent,
                    agent_task_id=resolved_task_id if trusted_agent else None,
                    etag=event.etag,
                    **_mirror_healthmes_kwargs(event, trusted_agent=trusted_agent),
                    **_mirror_metadata_kwargs(event),
                )
            )
            # Trusted agent-tagged events without a row are re-adopted silently
            # (the row normally pre-exists from create_agent_event); bootstrap
            # adopts everything silently. A forged/untrusted tag is treated as
            # the genuine external creation it is.
            if not bootstrap and not trusted_agent:
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

        snapshot = _mirror_snapshot(row)
        old_start = coerce_utc(row.start_at)
        old_end = coerce_utc(row.end_at)
        moved = old_start != event.start_at or old_end != event.end_at
        content_changed = (row.summary or None) != (event.summary or None)
        metadata_changed = _mirror_metadata_changed(row, event)
        healthmes_changed = _mirror_healthmes_changed(
            row,
            event,
            trusted_agent=trusted_agent,
        )
        # Refresh ownership from the freshly-observed provider state: if the tag
        # was stripped (or its task link no longer resolves) the row flips to
        # external, and what would have been an agent-move is reclassified into
        # the external ``diff.moved`` bucket below.
        ownership_changed = row.is_agent_created != trusted_agent
        generation_changed = (
            self._account_generation is not None
            and row.connection_generation != self._account_generation
        )
        if generation_changed:
            self._retire_intake_task(row)

        if (
            not moved
            and not content_changed
            and not ownership_changed
            and not metadata_changed
            and not healthmes_changed
            and not generation_changed
        ):
            # Byte-identical, same-tag re-delivery (410 full resync, lost
            # sync-state file, crash between commit and cursor save): write
            # NOTHING. Assigning equal values still dirties the row on sqlite
            # (stored datetimes load naive, event values are aware), and any
            # UPDATE bumps updated_at — which the trigger sweep reads as an
            # external change (triggers.py::_load_schedule_changes contract:
            # updated_at moves only when the event actually changed).
            return

        # External wins for every event, including agent-created ones.
        self._cas_update_row(
            row,
            snapshot,
            {
                "summary": event.summary,
                "start_at": event.start_at,
                "end_at": event.end_at,
                "etag": event.etag,
                "is_agent_created": trusted_agent,
                "agent_task_id": resolved_task_id if trusted_agent else None,
                "connection_generation": (
                    self._account_generation
                    if self._account_generation is not None
                    else row.connection_generation
                ),
                "intake_task_id": (
                    None
                    if generation_changed
                    else row.intake_task_id
                ),
                "intake_opted_out": (
                    False
                    if generation_changed
                    else row.intake_opted_out
                ),
                **_mirror_healthmes_kwargs(
                    event,
                    trusted_agent=trusted_agent,
                ),
                **_mirror_metadata_kwargs(event),
            },
        )

        change = EventChange(
            calendar_source=source,
            external_id=event.external_id,
            kind=ChangeKind.MOVED if moved else ChangeKind.MODIFIED,
            summary=event.summary,
            is_agent_created=trusted_agent,
            old_start_at=old_start,
            old_end_at=old_end,
            new_start_at=event.start_at,
            new_end_at=event.end_at,
        )
        if trusted_agent:
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
        row = self._lock_current_row(source, event.external_id)
        if row is None or _pending_sleep_intent(row):
            return
        snapshot = _mirror_snapshot(row)
        self._retire_intake_task(row)
        change = EventChange(
            calendar_source=source,
            external_id=event.external_id,
            kind=ChangeKind.DELETED,
            summary=row.summary,
            is_agent_created=row.is_agent_created,
            old_start_at=coerce_utc(row.start_at),
            old_end_at=coerce_utc(row.end_at),
        )
        self._cas_delete_row(row, snapshot)
        if change.is_agent_created:
            diff.agent_modified.append(change)
        else:
            diff.deleted.append(change)

    def _reconcile_tombstones(
        self, source: CalendarSource, seen_ids: set[str], diff: SyncDiff
    ) -> None:
        """Tombstone mirror rows the provider no longer returns on a full resync.

        Only runs on bootstrap (sync state None/empty), when the backend fetches
        the whole current window. Any pre-existing mirror row for this source
        absent from that fresh set was deleted — or slid past the scheduling
        window — while we had no cursor to observe the deletion notice. Without a
        tombstone the mirror keeps a stale row forever and the ``schedule_changed``
        trigger never learns of the deletion (docs/PLAN.md §6). A true first-ever
        sync has an empty mirror, so this reconcile is a silent no-op then.
        """
        statement = select(CalendarEventMirror).where(
            CalendarEventMirror.calendar_source == source,
            self._generation_predicate(),
        ).order_by(CalendarEventMirror.external_id)
        for row in self._session.execute(statement).scalars().all():
            if row.external_id in seen_ids:
                continue  # freshly upserted, or already handled as a deletion
            row = self._lock_current_row(source, row.external_id)
            if (
                row is None
                or row.external_id in seen_ids
                or _pending_sleep_intent(row)
            ):
                continue
            snapshot = _mirror_snapshot(row)
            self._retire_intake_task(row)
            change = EventChange(
                calendar_source=source,
                external_id=row.external_id,
                kind=ChangeKind.DELETED,
                summary=row.summary,
                is_agent_created=row.is_agent_created,
                old_start_at=coerce_utc(row.start_at),
                old_end_at=coerce_utc(row.end_at),
            )
            self._cas_delete_row(row, snapshot)
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
        existing: CalendarEventMirror | None = None
        if draft.identity is not None:
            external_id = calendar_identity_external_id(source, draft.identity)
            existing = self._get_row(source, external_id)
            self._quarantine_identity_conflicts(
                source,
                draft.identity,
                external_id,
            )
            if existing is not None:
                created = backend.read_event(external_id)
                self._assert_remote_matches_draft(source, created, draft)
            else:
                try:
                    created = backend.create_event(draft)
                except CalendarConflictError:
                    created = backend.read_event(external_id)
                    self._assert_remote_matches_draft(source, created, draft)
        else:
            created = backend.create_event(draft)
        if draft.identity is not None:
            self._assert_remote_matches_draft(source, created, draft)
        assert created.start_at is not None and created.end_at is not None
        identity = created.identity or draft.identity
        row = existing or CalendarEventMirror(
            external_id=created.external_id,
            calendar_source=source,
            connection_generation=self._account_generation,
            start_at=created.start_at,
            end_at=created.end_at,
        )
        row.summary = created.summary
        row.start_at = created.start_at
        row.end_at = created.end_at
        row.is_agent_created = True
        row.agent_task_id = self._resolve_task_id(draft.agent_task_id)
        row.etag = created.etag
        if self._account_generation is not None:
            row.connection_generation = self._account_generation
        row.healthmes_kind = identity.kind.value if identity is not None else None
        row.healthmes_source = identity.source if identity is not None else None
        row.healthmes_source_key = (
            identity.source_key if identity is not None else None
        )
        _apply_mirror_metadata(row, created)
        if existing is None:
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

    def delete_agent_event(
        self,
        source: CalendarSource,
        external_id: str,
        *,
        expected_identity: CalendarEventIdentity | None = None,
    ) -> None:
        """Delete an agent-created block; refuses to touch external events."""
        row = self._get_owned_row(source, external_id)
        expected_etag = row.etag
        if expected_identity is not None:
            if (
                row.healthmes_kind != expected_identity.kind.value
                or row.healthmes_source != expected_identity.source
                or row.healthmes_source_key != expected_identity.source_key
                or row.external_id
                != calendar_identity_external_id(source, expected_identity)
            ):
                raise OwnershipError(
                    f"{source.value} event {external_id!r} is not the expected "
                    "proposal-owned event"
                )
            try:
                remote = self._backend_for(source).read_event(external_id)
            except EventNotFoundError:
                self._session.delete(row)
                self._session.commit()
                return
            if (
                not remote.is_agent_created
                or remote.identity != expected_identity
                or remote.external_id
                != calendar_identity_external_id(source, expected_identity)
            ):
                raise OwnershipError(
                    f"{source.value} event {external_id!r} failed remote identity "
                    "validation"
                )
            expected_etag = remote.etag
        try:
            self._backend_for(source).delete_event(
                external_id,
                expected_kind=(
                    expected_identity.kind if expected_identity is not None else None
                ),
                expected_etag=expected_etag,
            )
        except EventNotFoundError:
            pass
        self._session.delete(row)
        self._session.commit()

    def assert_legacy_agent_event_matches(
        self,
        source: CalendarSource,
        row: CalendarEventMirror,
        draft: EventDraft,
    ) -> None:
        """Validate an identity-less block created by a pre-identity release."""
        if (
            not row.is_agent_created
            or row.calendar_source is not source
            or row.agent_task_id != draft.agent_task_id
            or row.healthmes_kind is not None
            or row.healthmes_source is not None
            or row.healthmes_source_key is not None
        ):
            raise OwnershipError(
                f"{source.value} event {row.external_id!r} is not a legacy "
                "agent-owned block"
            )
        remote = self._backend_for(source).read_event(row.external_id)
        if (
            not remote.is_agent_created
            or remote.identity is not None
            or remote.agent_task_id != draft.agent_task_id
        ):
            raise OwnershipError(
                f"{source.value} event {row.external_id!r} failed legacy "
                "ownership validation"
            )
        if (
            (row.etag is not None and remote.etag != row.etag)
            or remote.summary != draft.summary
            or remote.description != draft.description
            or remote.start_at != draft.start_at
            or remote.end_at != draft.end_at
        ):
            raise CalendarConflictError(
                f"{source.value} event {row.external_id!r} changed after its "
                "legacy proposal write"
            )

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

    def _retire_intake_task(self, row: CalendarEventMirror) -> None:
        if row.intake_task_id is None:
            return
        task = self._session.get(Task, row.intake_task_id)
        if task is not None:
            task.status = "cancelled"

    def _quarantine_identity_conflicts(
        self,
        source: CalendarSource,
        identity: CalendarEventIdentity,
        expected_external_id: str,
    ) -> None:
        conflicts = list(
            self._session.scalars(
                select(CalendarEventMirror).where(
                    CalendarEventMirror.calendar_source == source,
                    self._generation_predicate(),
                    CalendarEventMirror.healthmes_source_key == identity.source_key,
                    CalendarEventMirror.external_id != expected_external_id,
                )
                .order_by(CalendarEventMirror.external_id)
            ).all()
        )
        for conflict in conflicts:
            conflict = self._lock_current_row(source, conflict.external_id)
            if (
                conflict is None
                or conflict.external_id == expected_external_id
                or conflict.healthmes_source_key != identity.source_key
                or _pending_sleep_intent(conflict)
            ):
                continue
            conflict_identity = parse_calendar_identity(
                conflict.healthmes_kind,
                conflict.healthmes_source,
                conflict.healthmes_source_key,
            )
            if (
                conflict_identity is not None
                and conflict.external_id
                == calendar_identity_external_id(source, conflict_identity)
            ):
                continue
            self._cas_update_row(
                conflict,
                _mirror_snapshot(conflict),
                {
                    "is_agent_created": False,
                    "agent_task_id": None,
                    "healthmes_kind": None,
                    "healthmes_source": None,
                    "healthmes_source_key": None,
                    "observation_fingerprint": None,
                    "sleep_local_date": None,
                    "sleep_provider": None,
                    "sleep_duration_minutes": None,
                    "sleep_time_in_bed_minutes": None,
                },
            )

    @staticmethod
    def _assert_remote_matches_draft(
        source: CalendarSource,
        event: ExternalEvent,
        draft: EventDraft,
    ) -> None:
        if (
            draft.identity is None
            or not event.is_agent_created
            or event.identity != draft.identity
            or event.external_id
            != calendar_identity_external_id(
                source,
                draft.identity,
            )
            or event.summary != draft.summary
            or event.description != draft.description
            or event.start_at != draft.start_at
            or event.end_at != draft.end_at
            or event.agent_task_id != draft.agent_task_id
        ):
            raise CalendarConflictError(
                "deterministic calendar identity exists with different content"
            )

    def _get_row(self, source: CalendarSource, external_id: str) -> CalendarEventMirror | None:
        statement = select(CalendarEventMirror).where(
            CalendarEventMirror.calendar_source == source,
            self._generation_predicate(),
            CalendarEventMirror.external_id == external_id,
        )
        return self._session.execute(statement).scalar_one_or_none()

    def _lock_current_row(
        self,
        source: CalendarSource,
        external_id: str,
    ) -> CalendarEventMirror | None:
        statement = (
            select(CalendarEventMirror)
            .where(
                CalendarEventMirror.calendar_source == source,
                self._generation_predicate(),
                CalendarEventMirror.external_id == external_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        with self._session.no_autoflush:
            return self._session.execute(statement).scalar_one_or_none()

    def _generation_predicate(self) -> sa.ColumnElement[bool]:
        if self._account_generation is None:
            return CalendarEventMirror.connection_generation.is_(None)
        return (
            CalendarEventMirror.connection_generation
            == self._account_generation
        )

    def _cas_update_row(
        self,
        row: CalendarEventMirror,
        snapshot: dict[str, object],
        values: dict[str, object],
    ) -> None:
        result = self._session.execute(
            sa.update(CalendarEventMirror)
            .where(*_mirror_snapshot_predicates(snapshot))
            .values(**values, updated_at=sa.func.now())
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise CalendarConflictError(
                f"{row.calendar_source.value} mirror {row.external_id!r} "
                "changed during sync; retry with a fresh provider cursor"
            )
        self._session.expire(row)

    def _cas_delete_row(
        self,
        row: CalendarEventMirror,
        snapshot: dict[str, object],
    ) -> None:
        result = self._session.execute(
            sa.delete(CalendarEventMirror)
            .where(*_mirror_snapshot_predicates(snapshot))
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise CalendarConflictError(
                f"{row.calendar_source.value} mirror {row.external_id!r} "
                "changed during sync; retry with a fresh provider cursor"
            )
        self._session.expunge(row)

    def _backend_for(self, source: CalendarSource) -> CalendarBackend:
        backend = self._backends.get(source)
        if backend is None:
            raise CalendarError(f"no calendar backend configured for source {source.value!r}")
        return backend


_MIRROR_METADATA_FIELDS = (
    "organizer_self",
    "has_attendees",
    "is_recurring",
    "event_type",
    "is_all_day",
    "is_locked",
    "status",
)


_SLEEP_CONTEXT_FIELDS = (
    "observation_fingerprint",
    "sleep_local_date",
    "sleep_provider",
    "sleep_duration_minutes",
    "sleep_time_in_bed_minutes",
)


def _mirror_healthmes_kwargs(
    event: ExternalEvent,
    *,
    trusted_agent: bool,
) -> dict[str, object]:
    if trusted_agent and event.identity is not None:
        identity: dict[str, object] = {
            "healthmes_kind": event.identity.kind.value,
            "healthmes_source": event.identity.source,
            "healthmes_source_key": event.identity.source_key,
        }
    else:
        identity = {
            "healthmes_kind": None,
            "healthmes_source": None,
            "healthmes_source_key": None,
        }
    if not trusted_agent:
        identity.update({field: None for field in _SLEEP_CONTEXT_FIELDS})
    return identity


def _apply_mirror_healthmes(
    row: CalendarEventMirror,
    event: ExternalEvent,
    *,
    trusted_agent: bool,
) -> None:
    for field_name, value in _mirror_healthmes_kwargs(
        event,
        trusted_agent=trusted_agent,
    ).items():
        setattr(row, field_name, value)


def _mirror_healthmes_changed(
    row: CalendarEventMirror,
    event: ExternalEvent,
    *,
    trusted_agent: bool,
) -> bool:
    return any(
        getattr(row, field_name) != value
        for field_name, value in _mirror_healthmes_kwargs(
            event,
            trusted_agent=trusted_agent,
        ).items()
    )


def _mirror_metadata_kwargs(event: ExternalEvent) -> dict[str, object]:
    return {
        field: getattr(event, field)
        for field in _MIRROR_METADATA_FIELDS
        if hasattr(CalendarEventMirror, field)
    }


def _pending_sleep_intent(row: CalendarEventMirror) -> bool:
    return row.status in {
        SLEEP_CREATE_PENDING_STATUS,
        SLEEP_UPDATE_PENDING_STATUS,
    }


def _mirror_snapshot(row: CalendarEventMirror) -> dict[str, object]:
    return {
        column.key: getattr(row, column.key)
        for column in _MIRROR_CAS_COLUMNS
    }


def _mirror_snapshot_predicates(
    snapshot: dict[str, object],
) -> tuple[sa.ColumnElement[bool], ...]:
    predicates: list[sa.ColumnElement[bool]] = []
    for column in _MIRROR_CAS_COLUMNS:
        value = snapshot[column.key]
        predicates.append(column.is_(None) if value is None else column == value)
    return tuple(predicates)


_MIRROR_CAS_COLUMNS = tuple(
    column
    for column in CalendarEventMirror.__table__.columns
    if column.key not in {"created_at", "updated_at"}
)


def _apply_mirror_metadata(row: CalendarEventMirror, event: ExternalEvent) -> None:
    for field_name, value in _mirror_metadata_kwargs(event).items():
        setattr(row, field_name, value)


def _mirror_metadata_changed(row: CalendarEventMirror, event: ExternalEvent) -> bool:
    return any(
        getattr(row, field) != getattr(event, field)
        for field in _MIRROR_METADATA_FIELDS
        if hasattr(CalendarEventMirror, field)
    )
