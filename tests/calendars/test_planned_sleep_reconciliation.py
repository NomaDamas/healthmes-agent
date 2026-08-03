from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from healthmes.calendars.base import (
    CalendarEventIdentity,
    EventDraft,
    EventNotFoundError,
    ExternalEvent,
    HealthmesEventKind,
    OwnershipError,
    SyncState,
    calendar_identity_external_id,
)
from healthmes.calendars.sleep_mirror import actual_sleep_identity
from healthmes.calendars.sleep_observation import ActualSleepObservation
from healthmes.calendars.sleep_reconciliation import SleepCalendarReconciler
from healthmes.store import CalendarEventMirror, CalendarSource


class PlannedSleepBackend:
    source = CalendarSource.GOOGLE

    def __init__(self) -> None:
        self.delete_calls: list[tuple[str, HealthmesEventKind | None, str | None]] = []
        self.events: dict[str, ExternalEvent] = {}
        self.missing_ids: set[str] = set()
        self.actual_event: ExternalEvent | None = None

    def list_changes(self, sync_state: SyncState | None) -> tuple[list[ExternalEvent], SyncState]:
        return [], dict(sync_state or {})

    def create_event(self, draft: EventDraft) -> ExternalEvent:
        assert draft.identity is not None
        self.actual_event = ExternalEvent(
            external_id=calendar_identity_external_id(
                self.source,
                draft.identity,
            ),
            summary=draft.summary,
            description=draft.description,
            start_at=draft.start_at,
            end_at=draft.end_at,
            is_agent_created=True,
            identity=draft.identity,
            healthmes_kind=HealthmesEventKind.ACTUAL_SLEEP,
            etag='"actual"',
        )
        self.events[self.actual_event.external_id] = self.actual_event
        return self.actual_event

    def read_event(self, external_id: str) -> ExternalEvent:
        if external_id in self.missing_ids:
            raise EventNotFoundError(external_id)
        try:
            return self.events[external_id]
        except KeyError as exc:
            raise EventNotFoundError(external_id) from exc

    def update_event(
        self,
        external_id: str,
        *,
        summary: str | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        description: str | None = None,
        expected_etag: str | None = None,
    ) -> ExternalEvent:
        raise AssertionError("planned-sleep tests do not correct the actual event")

    def delete_event(
        self,
        external_id: str,
        *,
        expected_kind: HealthmesEventKind | None = None,
        expected_etag: str | None = None,
    ) -> None:
        if external_id in self.missing_ids:
            raise EventNotFoundError(external_id)
        event = self.read_event(external_id)
        if event.identity is None or event.identity.kind is not expected_kind:
            raise OwnershipError(f"remote kind changed for {external_id}")
        if expected_etag is not None and event.etag != expected_etag:
            raise OwnershipError(f"remote etag changed for {external_id}")
        self.delete_calls.append((external_id, expected_kind, expected_etag))
        del self.events[external_id]


@pytest.fixture
def backend() -> PlannedSleepBackend:
    return PlannedSleepBackend()


@pytest.fixture
def observation() -> ActualSleepObservation:
    return ActualSleepObservation(
        local_date=date(2026, 7, 26),
        provider="oura",
        source_key="oura:2026-07-26",
        start_at=datetime(2026, 7, 25, 23, tzinfo=UTC),
        end_at=datetime(2026, 7, 26, 7, tzinfo=UTC),
        duration_minutes=420,
        time_in_bed_minutes=480,
    )


def actual_external_id(observation: ActualSleepObservation) -> str:
    return calendar_identity_external_id(
        CalendarSource.GOOGLE,
        actual_sleep_identity(observation),
    )


def planned_identity(observation: ActualSleepObservation) -> CalendarEventIdentity:
    return CalendarEventIdentity(
        kind=HealthmesEventKind.PLANNED_SLEEP,
        source="planner",
        source_key=f"proposal:{observation.local_date.isoformat()}",
    )


def planned_external_id(observation: ActualSleepObservation) -> str:
    return calendar_identity_external_id(
        CalendarSource.GOOGLE,
        planned_identity(observation),
    )


def add_remote_planned_sleep(
    backend: PlannedSleepBackend,
    observation: ActualSleepObservation,
    *,
    external_id: str | None = None,
    identity: CalendarEventIdentity | None = None,
    etag: str = '"planned-v1"',
) -> str:
    identity = identity or planned_identity(observation)
    external_id = external_id or calendar_identity_external_id(
        CalendarSource.GOOGLE,
        identity,
    )
    backend.events[external_id] = ExternalEvent(
        external_id=external_id,
        summary="수면 (계획)",
        start_at=observation.start_at,
        end_at=observation.end_at,
        is_agent_created=True,
        identity=identity,
        healthmes_kind=identity.kind,
        etag=etag,
    )
    return external_id


def add_mirror(
    session,
    observation: ActualSleepObservation,
    *,
    external_id: str,
    owned: bool,
    kind: str | None,
    summary: str,
) -> None:
    identity = planned_identity(observation) if kind is not None else None
    session.add(
        CalendarEventMirror(
            external_id=external_id,
            calendar_source=CalendarSource.GOOGLE,
            summary=summary,
            start_at=observation.start_at,
            end_at=observation.end_at,
            is_agent_created=owned,
            healthmes_kind=kind,
            healthmes_source=identity.source if identity is not None else None,
            healthmes_source_key=identity.source_key if identity is not None else None,
            etag='"planned-v1"',
        )
    )
    session.commit()


def test_replaces_overlapping_owned_planned_sleep(
    session,
    backend: PlannedSleepBackend,
    observation: ActualSleepObservation,
) -> None:
    # Given
    external_id = planned_external_id(observation)
    add_mirror(
        session,
        observation,
        external_id=external_id,
        owned=True,
        kind="planned_sleep",
        summary="수면 (계획)",
    )
    add_remote_planned_sleep(backend, observation)

    # When
    result = SleepCalendarReconciler(session, backend).reconcile(observation)

    # Then
    assert result.deleted_planned_external_ids == (external_id,)
    assert backend.delete_calls == [
        (external_id, HealthmesEventKind.PLANNED_SLEEP, '"planned-v1"')
    ]
    assert {
        row.external_id for row in session.query(CalendarEventMirror).all()
    } == {actual_external_id(observation)}


def test_replay_does_not_redelete_planned_sleep(
    session,
    backend: PlannedSleepBackend,
    observation: ActualSleepObservation,
) -> None:
    # Given
    external_id = planned_external_id(observation)
    add_mirror(
        session,
        observation,
        external_id=external_id,
        owned=True,
        kind="planned_sleep",
        summary="수면 (계획)",
    )
    add_remote_planned_sleep(backend, observation)
    reconciler = SleepCalendarReconciler(session, backend)
    reconciler.reconcile(observation)

    # When
    replay = reconciler.reconcile(observation)

    # Then
    assert replay.deleted_planned_external_ids == ()
    assert backend.delete_calls == [
        (external_id, HealthmesEventKind.PLANNED_SLEEP, '"planned-v1"')
    ]


def test_preserves_external_routine_and_untagged_sleep_title(
    session,
    backend: PlannedSleepBackend,
    observation: ActualSleepObservation,
) -> None:
    # Given
    add_mirror(
        session,
        observation,
        external_id="routine-1",
        owned=False,
        kind=None,
        summary="Routine",
    )
    add_mirror(
        session,
        observation,
        external_id="title-only-1",
        owned=True,
        kind=None,
        summary="sleep",
    )

    # When
    result = SleepCalendarReconciler(session, backend).reconcile(observation)

    # Then
    assert result.deleted_planned_external_ids == ()
    assert backend.delete_calls == []
    remaining = {row.external_id for row in session.query(CalendarEventMirror).all()}
    assert {"routine-1", "title-only-1"} <= remaining


def test_remote_kind_change_leaves_stale_mirror_for_safe_cleanup(
    session,
    backend: PlannedSleepBackend,
    observation: ActualSleepObservation,
) -> None:
    # Given
    external_id = planned_external_id(observation)
    add_mirror(
        session,
        observation,
        external_id=external_id,
        owned=True,
        kind="planned_sleep",
        summary="수면 (계획)",
    )
    changed_identity = CalendarEventIdentity(
        kind=HealthmesEventKind.ACTUAL_SLEEP,
        source="planner",
        source_key=planned_identity(observation).source_key,
    )
    add_remote_planned_sleep(
        backend,
        observation,
        external_id=external_id,
        identity=changed_identity,
    )

    # When
    result = SleepCalendarReconciler(session, backend).reconcile(observation)

    # Then
    assert result.planned_sleep_cleanup_pending == 1
    assert backend.delete_calls == []
    assert session.query(CalendarEventMirror).filter_by(external_id=external_id).one()


def test_copied_planned_identity_is_never_deleted(
    session,
    backend: PlannedSleepBackend,
    observation: ActualSleepObservation,
) -> None:
    copied_external_id = "copied-planned-sleep"
    add_mirror(
        session,
        observation,
        external_id=copied_external_id,
        owned=True,
        kind="planned_sleep",
        summary="수면 (계획)",
    )
    add_remote_planned_sleep(
        backend,
        observation,
        external_id=copied_external_id,
    )

    result = SleepCalendarReconciler(session, backend).reconcile(observation)

    assert result.planned_sleep_cleanup_pending == 1
    assert backend.delete_calls == []
    assert session.query(CalendarEventMirror).filter_by(
        external_id=copied_external_id
    ).one()


def test_remote_missing_planned_sleep_prunes_stale_mirror(
    session,
    backend: PlannedSleepBackend,
    observation: ActualSleepObservation,
) -> None:
    # Given
    external_id = planned_external_id(observation)
    add_mirror(
        session,
        observation,
        external_id=external_id,
        owned=True,
        kind="planned_sleep",
        summary="수면 (계획)",
    )
    backend.missing_ids.add(external_id)

    # When
    result = SleepCalendarReconciler(session, backend).reconcile(observation)

    # Then
    assert result.deleted_planned_external_ids == (external_id,)
    assert (
        session.query(CalendarEventMirror)
        .filter_by(external_id=external_id)
        .one_or_none()
        is None
    )


def test_remote_delete_then_local_commit_failure_recovers_on_retry(
    session_factory,
    backend: PlannedSleepBackend,
    observation: ActualSleepObservation,
    monkeypatch,
) -> None:
    external_id = planned_external_id(observation)
    with session_factory() as session:
        add_mirror(
            session,
            observation,
            external_id=external_id,
            owned=True,
            kind="planned_sleep",
            summary="수면 (계획)",
        )
    add_remote_planned_sleep(backend, observation)
    original_delete = backend.delete_event

    def delete_then_disappear(*args, **kwargs) -> None:
        original_delete(*args, **kwargs)
        backend.missing_ids.add(external_id)

    monkeypatch.setattr(backend, "delete_event", delete_then_disappear)
    with session_factory() as failing_session:
        real_commit = failing_session.commit

        def fail_after_remote_delete() -> None:
            if backend.delete_calls:
                raise RuntimeError("simulated local cleanup commit failure")
            real_commit()

        monkeypatch.setattr(failing_session, "commit", fail_after_remote_delete)
        with pytest.raises(RuntimeError, match="cleanup commit failure"):
            SleepCalendarReconciler(failing_session, backend).reconcile(observation)
        failing_session.rollback()

    with session_factory() as retry_session:
        result = SleepCalendarReconciler(retry_session, backend).reconcile(observation)
        remaining = {
            row.external_id
            for row in retry_session.query(CalendarEventMirror).all()
        }

    assert result.deleted_planned_external_ids == (external_id,)
    assert remaining == {actual_external_id(observation)}
