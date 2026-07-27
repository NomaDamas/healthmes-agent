from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from healthmes.calendars.base import (
    CalendarEventIdentity,
    EventDraft,
    ExternalEvent,
    HealthmesEventKind,
    OwnershipError,
    SyncState,
    coerce_utc,
)
from healthmes.calendars.sleep_observation import ActualSleepObservation
from healthmes.calendars.sleep_reconciliation import (
    SleepCalendarAction,
    SleepCalendarReconciler,
)
from healthmes.store import CalendarEventMirror, CalendarSource


class RecordingCalendarBackend:
    source = CalendarSource.GOOGLE

    def __init__(self) -> None:
        self.created_drafts: list[EventDraft] = []
        self.update_calls: list[dict[str, object]] = []
        self.delete_calls: list[str] = []
        self.identity: CalendarEventIdentity | None = None

    def list_changes(self, sync_state: SyncState | None) -> tuple[list[ExternalEvent], SyncState]:
        return [], dict(sync_state or {})

    def create_event(self, draft: EventDraft) -> ExternalEvent:
        self.created_drafts.append(draft)
        self.identity = draft.identity
        return ExternalEvent(
            external_id="sleep-1",
            summary=draft.summary,
            start_at=draft.start_at,
            end_at=draft.end_at,
            is_agent_created=True,
            identity=draft.identity,
            etag='"created"',
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
        assert start_at is not None
        assert end_at is not None
        return ExternalEvent(
            external_id=external_id,
            summary=summary,
            start_at=start_at,
            end_at=end_at,
            is_agent_created=True,
            identity=self.identity,
            etag='"updated"',
        )

    def delete_event(
        self,
        external_id: str,
        *,
        expected_kind: HealthmesEventKind | None = None,
    ) -> None:
        self.delete_calls.append(external_id)


@pytest.fixture
def backend() -> RecordingCalendarBackend:
    return RecordingCalendarBackend()


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


def test_creates_owned_actual_sleep_with_source_identity(
    session,
    backend: RecordingCalendarBackend,
    observation: ActualSleepObservation,
) -> None:
    # Given
    reconciler = SleepCalendarReconciler(session, backend)

    # When
    result = reconciler.reconcile(observation)

    # Then
    assert result.action is SleepCalendarAction.CREATED
    assert len(backend.created_drafts) == 1
    draft = backend.created_drafts[0]
    assert draft.summary == "수면 (실제)"
    assert draft.identity == CalendarEventIdentity(
        kind=HealthmesEventKind.ACTUAL_SLEEP,
        source="oura",
        source_key="oura:2026-07-26",
    )
    assert draft.description == ("Actual sleep: 420 min\nTime in bed: 480 min\nSource: oura")
    mirror = session.query(CalendarEventMirror).one()
    assert mirror.external_id == "sleep-1"
    assert mirror.healthmes_kind == "actual_sleep"
    assert mirror.healthmes_source == "oura"
    assert mirror.healthmes_source_key == "oura:2026-07-26"
    assert mirror.observation_fingerprint == result.observation_fingerprint
    assert mirror.sleep_local_date == observation.local_date
    assert mirror.sleep_duration_minutes == observation.duration_minutes
    assert mirror.sleep_time_in_bed_minutes == observation.time_in_bed_minutes


def test_identical_replay_is_write_free(
    session,
    backend: RecordingCalendarBackend,
    observation: ActualSleepObservation,
) -> None:
    # Given
    reconciler = SleepCalendarReconciler(session, backend)
    created = reconciler.reconcile(observation)

    # When
    replayed = reconciler.reconcile(observation)

    # Then
    assert created.action is SleepCalendarAction.CREATED
    assert replayed.action is SleepCalendarAction.NOOP
    assert len(backend.created_drafts) == 1
    assert backend.update_calls == []
    assert session.query(CalendarEventMirror).count() == 1


def test_provider_correction_updates_existing_event(
    session,
    backend: RecordingCalendarBackend,
    observation: ActualSleepObservation,
) -> None:
    # Given
    reconciler = SleepCalendarReconciler(session, backend)
    created = reconciler.reconcile(observation)
    corrected = replace(
        observation,
        end_at=datetime(2026, 7, 26, 7, 30, tzinfo=UTC),
        duration_minutes=450,
        time_in_bed_minutes=510,
    )

    # When
    result = reconciler.reconcile(corrected)

    # Then
    assert result.action is SleepCalendarAction.UPDATED
    assert result.external_id == created.external_id
    assert len(backend.created_drafts) == 1
    assert backend.update_calls == [
        {
            "external_id": "sleep-1",
            "summary": "수면 (실제)",
            "start_at": corrected.start_at,
            "end_at": corrected.end_at,
            "description": ("Actual sleep: 450 min\nTime in bed: 510 min\nSource: oura"),
        }
    ]
    mirror = session.query(CalendarEventMirror).one()
    assert mirror.external_id == "sleep-1"
    assert coerce_utc(mirror.end_at) == corrected.end_at
    assert mirror.etag == '"updated"'
    assert mirror.observation_fingerprint == result.observation_fingerprint
    assert mirror.sleep_duration_minutes == 450
    assert mirror.sleep_time_in_bed_minutes == 510


def test_refuses_source_key_collision_with_unowned_event(
    session,
    backend: RecordingCalendarBackend,
    observation: ActualSleepObservation,
) -> None:
    # Given
    session.add(
        CalendarEventMirror(
            external_id="external-1",
            calendar_source=CalendarSource.GOOGLE,
            summary="Routine",
            start_at=observation.start_at,
            end_at=observation.end_at,
            is_agent_created=False,
            healthmes_kind="actual_sleep",
            healthmes_source="oura",
            healthmes_source_key=observation.source_key,
            observation_fingerprint="forged",
        )
    )
    session.commit()

    # When / Then
    with pytest.raises(OwnershipError):
        SleepCalendarReconciler(session, backend).reconcile(observation)
    assert backend.created_drafts == []
    assert backend.update_calls == []


def test_source_key_is_unique_within_configured_calendar(
    session,
    observation: ActualSleepObservation,
) -> None:
    # Given
    for external_id in ("sleep-1", "sleep-2"):
        session.add(
            CalendarEventMirror(
                external_id=external_id,
                calendar_source=CalendarSource.GOOGLE,
                start_at=observation.start_at,
                end_at=observation.end_at,
                healthmes_source_key=observation.source_key,
            )
        )

    # When / Then
    with pytest.raises(IntegrityError):
        session.commit()
