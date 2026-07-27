from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from healthmes.calendars.base import (
    EventDraft,
    EventNotFoundError,
    ExternalEvent,
    HealthmesEventKind,
    OwnershipError,
    SyncState,
)
from healthmes.calendars.sleep_observation import ActualSleepObservation
from healthmes.calendars.sleep_reconciliation import SleepCalendarReconciler
from healthmes.store import CalendarEventMirror, CalendarSource


class PlannedSleepBackend:
    source = CalendarSource.GOOGLE

    def __init__(self) -> None:
        self.delete_calls: list[tuple[str, HealthmesEventKind | None, str | None]] = []
        self.remote_kinds: dict[str, HealthmesEventKind] = {}
        self.missing_ids: set[str] = set()
        self.actual_event: ExternalEvent | None = None

    def list_changes(self, sync_state: SyncState | None) -> tuple[list[ExternalEvent], SyncState]:
        return [], dict(sync_state or {})

    def create_event(self, draft: EventDraft) -> ExternalEvent:
        self.actual_event = ExternalEvent(
            external_id="actual-1",
            summary=draft.summary,
            start_at=draft.start_at,
            end_at=draft.end_at,
            is_agent_created=True,
            identity=draft.identity,
            healthmes_kind=HealthmesEventKind.ACTUAL_SLEEP,
            etag='"actual"',
        )
        return self.actual_event

    def read_event(self, external_id: str) -> ExternalEvent:
        if self.actual_event is None or self.actual_event.external_id != external_id:
            raise EventNotFoundError(external_id)
        return self.actual_event

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
        if self.remote_kinds.get(external_id) is not expected_kind:
            raise OwnershipError(f"remote kind changed for {external_id}")
        self.delete_calls.append((external_id, expected_kind, expected_etag))


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


def add_mirror(
    session,
    observation: ActualSleepObservation,
    *,
    external_id: str,
    owned: bool,
    kind: str | None,
    summary: str,
) -> None:
    session.add(
        CalendarEventMirror(
            external_id=external_id,
            calendar_source=CalendarSource.GOOGLE,
            summary=summary,
            start_at=observation.start_at,
            end_at=observation.end_at,
            is_agent_created=owned,
            healthmes_kind=kind,
            healthmes_source="planner" if kind is not None else None,
            healthmes_source_key=(
                f"planner:{observation.local_date.isoformat()}" if kind is not None else None
            ),
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
    add_mirror(
        session,
        observation,
        external_id="planned-1",
        owned=True,
        kind="planned_sleep",
        summary="수면 (계획)",
    )
    backend.remote_kinds["planned-1"] = HealthmesEventKind.PLANNED_SLEEP

    # When
    result = SleepCalendarReconciler(session, backend).reconcile(observation)

    # Then
    assert result.deleted_planned_external_ids == ("planned-1",)
    assert backend.delete_calls == [
        ("planned-1", HealthmesEventKind.PLANNED_SLEEP, '"planned-v1"')
    ]
    assert {row.external_id for row in session.query(CalendarEventMirror).all()} == {"actual-1"}


def test_replay_does_not_redelete_planned_sleep(
    session,
    backend: PlannedSleepBackend,
    observation: ActualSleepObservation,
) -> None:
    # Given
    add_mirror(
        session,
        observation,
        external_id="planned-1",
        owned=True,
        kind="planned_sleep",
        summary="수면 (계획)",
    )
    backend.remote_kinds["planned-1"] = HealthmesEventKind.PLANNED_SLEEP
    reconciler = SleepCalendarReconciler(session, backend)
    reconciler.reconcile(observation)

    # When
    replay = reconciler.reconcile(observation)

    # Then
    assert replay.deleted_planned_external_ids == ()
    assert backend.delete_calls == [
        ("planned-1", HealthmesEventKind.PLANNED_SLEEP, '"planned-v1"')
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


def test_remote_kind_change_blocks_stale_mirror_deletion(
    session,
    backend: PlannedSleepBackend,
    observation: ActualSleepObservation,
) -> None:
    # Given
    add_mirror(
        session,
        observation,
        external_id="planned-1",
        owned=True,
        kind="planned_sleep",
        summary="수면 (계획)",
    )
    backend.remote_kinds["planned-1"] = HealthmesEventKind.ACTUAL_SLEEP

    # When / Then
    with pytest.raises(OwnershipError):
        SleepCalendarReconciler(session, backend).reconcile(observation)
    assert session.query(CalendarEventMirror).filter_by(external_id="planned-1").one()


def test_remote_missing_planned_sleep_prunes_stale_mirror(
    session,
    backend: PlannedSleepBackend,
    observation: ActualSleepObservation,
) -> None:
    # Given
    add_mirror(
        session,
        observation,
        external_id="planned-1",
        owned=True,
        kind="planned_sleep",
        summary="수면 (계획)",
    )
    backend.missing_ids.add("planned-1")

    # When
    result = SleepCalendarReconciler(session, backend).reconcile(observation)

    # Then
    assert result.deleted_planned_external_ids == ("planned-1",)
    assert (
        session.query(CalendarEventMirror).filter_by(external_id="planned-1").one_or_none() is None
    )
