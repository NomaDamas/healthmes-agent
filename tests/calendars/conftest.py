"""Fixtures for the calendar-sync test suite.

Everything is offline: in-memory sqlite (real ``create_db_engine`` settings)
plus a scriptable fake backend implementing the ``CalendarBackend`` protocol.
No network, no Docker, no credentials.
"""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from healthmes.calendars.base import EventDraft, ExternalEvent, SyncState
from healthmes.store import Base, CalendarSource, create_db_engine


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory sqlite engine with the full schema created."""
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session


class FakeCalendarBackend:
    """Scriptable in-memory ``CalendarBackend`` for mirror-service tests.

    ``queue_changes`` enqueues one ``list_changes`` result per sync run;
    with an empty queue the backend reports "no changes" and echoes the
    received state. All write calls are recorded for assertions.
    """

    def __init__(self, source: CalendarSource = CalendarSource.GOOGLE) -> None:
        self.source = source
        self._batches: list[tuple[list[ExternalEvent], SyncState]] = []
        self.received_sync_states: list[SyncState | None] = []
        self.created_drafts: list[EventDraft] = []
        self.update_calls: list[dict[str, object]] = []
        self.delete_calls: list[str] = []
        self._create_counter = 0

    def queue_changes(self, events: list[ExternalEvent], sync_state: SyncState) -> None:
        self._batches.append((list(events), dict(sync_state)))

    def list_changes(
        self, sync_state: SyncState | None
    ) -> tuple[list[ExternalEvent], SyncState]:
        self.received_sync_states.append(None if sync_state is None else dict(sync_state))
        if self._batches:
            return self._batches.pop(0)
        return [], dict(sync_state or {})

    def create_event(self, draft: EventDraft) -> ExternalEvent:
        self._create_counter += 1
        self.created_drafts.append(draft)
        return ExternalEvent(
            external_id=f"{self.source.value}-agent-{self._create_counter}",
            summary=draft.summary,
            start_at=draft.start_at,
            end_at=draft.end_at,
            is_agent_created=True,
            agent_task_id=draft.agent_task_id,
            etag=f"etag-created-{self._create_counter}",
        )

    def update_event(
        self,
        external_id: str,
        *,
        summary: str | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        description: str | None = None,
    ) -> ExternalEvent:
        self.update_calls.append(
            {
                "external_id": external_id,
                "summary": summary,
                "start_at": start_at,
                "end_at": end_at,
                "description": description,
            }
        )
        assert start_at is not None and end_at is not None, "service always moves with both times"
        return ExternalEvent(
            external_id=external_id,
            summary=summary or "moved block",
            start_at=start_at,
            end_at=end_at,
            is_agent_created=True,
            etag="etag-updated",
        )

    def delete_event(self, external_id: str) -> None:
        self.delete_calls.append(external_id)


@pytest.fixture
def fake_backend() -> FakeCalendarBackend:
    return FakeCalendarBackend()


@pytest.fixture
def fake_backend_factory() -> type[FakeCalendarBackend]:
    return FakeCalendarBackend


def utc(*args: int) -> datetime:
    """Shorthand aware-UTC datetime for test data."""
    return datetime(*args, tzinfo=UTC)


@pytest.fixture
def make_event() -> object:
    """Factory for live ``ExternalEvent`` values with compact defaults."""

    def _make(
        external_id: str,
        *,
        summary: str | None = "Team standup",
        start: datetime | None = None,
        end: datetime | None = None,
        is_agent_created: bool = False,
        agent_task_id=None,
        etag: str | None = "etag-1",
        deleted: bool = False,
    ) -> ExternalEvent:
        if not deleted:
            start = start or utc(2026, 7, 9, 9, 0)
            end = end or utc(2026, 7, 9, 9, 30)
        return ExternalEvent(
            external_id=external_id,
            summary=summary,
            start_at=start,
            end_at=end,
            is_agent_created=is_agent_created,
            agent_task_id=agent_task_id,
            etag=etag,
            deleted=deleted,
        )

    return _make
