from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from healthmes.calendars.base import (
    CalendarConflictError,
    CalendarEventIdentity,
    EventDraft,
    EventNotFoundError,
    ExternalEvent,
    HealthmesEventKind,
    OwnershipError,
    SyncState,
    calendar_identity_external_id,
    coerce_utc,
)
from healthmes.calendars.sleep_observation import ActualSleepObservation
from healthmes.calendars.sleep_preview import preview_sleep_reconciliation
from healthmes.calendars.sleep_reconciliation import (
    SleepCalendarAction,
    SleepCalendarReconciler,
    observation_fingerprint,
)
from healthmes.calendars.state import InMemorySyncStateStore
from healthmes.calendars.sync import CalendarMirrorService
from healthmes.store import CalendarEventMirror, CalendarSource


class RecordingCalendarBackend:
    source = CalendarSource.GOOGLE

    def __init__(self) -> None:
        self.created_drafts: list[EventDraft] = []
        self.update_calls: list[dict[str, object]] = []
        self.delete_calls: list[str] = []
        self.identity: CalendarEventIdentity | None = None
        self.event: ExternalEvent | None = None

    def list_changes(self, sync_state: SyncState | None) -> tuple[list[ExternalEvent], SyncState]:
        return [], dict(sync_state or {})

    def create_event(self, draft: EventDraft) -> ExternalEvent:
        self.created_drafts.append(draft)
        self.identity = draft.identity
        self.event = ExternalEvent(
            external_id="sleep-1",
            summary=draft.summary,
            start_at=draft.start_at,
            end_at=draft.end_at,
            is_agent_created=True,
            identity=draft.identity,
            etag='"created"',
        )
        return self.event

    def read_event(self, external_id: str) -> ExternalEvent:
        if self.event is None:
            raise EventNotFoundError(external_id)
        return replace(self.event, identity=self.identity)

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
        self.update_calls.append(
            {
                "external_id": external_id,
                "summary": summary,
                "start_at": start_at,
                "end_at": end_at,
                "description": description,
                "expected_etag": expected_etag,
            }
        )
        assert start_at is not None
        assert end_at is not None
        self.event = ExternalEvent(
            external_id=external_id,
            summary=summary,
            start_at=start_at,
            end_at=end_at,
            is_agent_created=True,
            identity=self.identity,
            etag='"updated"',
        )
        return self.event

    def delete_event(
        self,
        external_id: str,
        *,
        expected_kind: HealthmesEventKind | None = None,
        expected_etag: str | None = None,
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


def test_create_persists_owned_intent_before_the_provider_write(
    session,
    observation: ActualSleepObservation,
) -> None:
    # Given
    class ObservingBackend(RecordingCalendarBackend):
        def create_event(self, draft: EventDraft) -> ExternalEvent:
            provisional = session.query(CalendarEventMirror).one()
            assert provisional.is_agent_created
            assert provisional.status == "healthmes_pending_create"
            assert provisional.healthmes_source_key == observation.source_key
            return super().create_event(draft)

    backend = ObservingBackend()

    # When
    result = SleepCalendarReconciler(session, backend).reconcile(observation)

    # Then
    assert result.action is SleepCalendarAction.CREATED


def test_pending_create_retries_provider_write_when_remote_is_missing(
    session,
    backend: RecordingCalendarBackend,
    observation: ActualSleepObservation,
) -> None:
    # Given
    session.add(
        CalendarEventMirror(
            external_id="pending-actual",
            calendar_source=CalendarSource.GOOGLE,
            summary="수면 (실제)",
            start_at=observation.start_at,
            end_at=observation.end_at,
            is_agent_created=True,
            healthmes_kind=HealthmesEventKind.ACTUAL_SLEEP.value,
            healthmes_source=observation.provider,
            healthmes_source_key=observation.source_key,
            observation_fingerprint=observation_fingerprint(observation),
            sleep_local_date=observation.local_date,
            sleep_duration_minutes=observation.duration_minutes,
            sleep_time_in_bed_minutes=observation.time_in_bed_minutes,
            status="healthmes_pending_create",
        )
    )
    session.commit()

    # When
    result = SleepCalendarReconciler(session, backend).reconcile(observation)

    # Then
    assert result.action is SleepCalendarAction.CREATED
    assert len(backend.created_drafts) == 1
    assert session.query(CalendarEventMirror).one().external_id == "sleep-1"


def test_pending_create_adopts_a_concurrent_create_only_with_local_intent(
    session,
    observation: ActualSleepObservation,
) -> None:
    # Given
    class ConcurrentCreateBackend(RecordingCalendarBackend):
        def create_event(self, draft: EventDraft) -> ExternalEvent:
            super().create_event(draft)
            raise CalendarConflictError("deterministic identity already exists")

    backend = ConcurrentCreateBackend()

    # When
    result = SleepCalendarReconciler(session, backend).reconcile(observation)

    # Then
    assert result.action is SleepCalendarAction.CREATED
    assert len(backend.created_drafts) == 1
    assert session.query(CalendarEventMirror).one().status is None


def test_source_key_lock_spans_pending_commit_remote_create_and_finalize(
    session_factory,
    observation: ActualSleepObservation,
) -> None:
    # Given
    timeline: list[str] = []

    class LockAwareBackend(RecordingCalendarBackend):
        def create_event(self, draft: EventDraft) -> ExternalEvent:
            with session_factory() as observer:
                pending = observer.scalar(
                    sa.select(CalendarEventMirror).where(
                        CalendarEventMirror.healthmes_source_key
                        == observation.source_key
                    )
                )
                assert pending is not None
                assert pending.status == "healthmes_pending_create"
            timeline.append("provider_create")
            return super().create_event(draft)

    class LockRecordingReconciler(SleepCalendarReconciler):
        def _lock_source_key(self, source_key: str) -> bool:
            timeline.append("lock")
            return True

        def _unlock_source_key(
            self,
            _connection: object,
            source_key: str,
        ) -> None:
            timeline.append("unlock")

    # When
    with session_factory() as session:
        result = LockRecordingReconciler(
            session,
            LockAwareBackend(),
        ).reconcile(observation)
        finalized = session.scalar(
            sa.select(CalendarEventMirror).where(
                CalendarEventMirror.healthmes_source_key == observation.source_key
            )
        )

    # Then
    assert result.action is SleepCalendarAction.CREATED
    assert finalized is not None
    assert finalized.status is None
    assert timeline == ["lock", "provider_create", "unlock"]


def test_sync_between_remote_create_and_local_finalize_keeps_retry_write_free(
    session,
    backend: RecordingCalendarBackend,
    observation: ActualSleepObservation,
) -> None:
    # Given
    identity = CalendarEventIdentity(
        kind=HealthmesEventKind.ACTUAL_SLEEP,
        source=observation.provider,
        source_key=observation.source_key,
    )
    external_id = calendar_identity_external_id(CalendarSource.GOOGLE, identity)
    session.add(
        CalendarEventMirror(
            external_id=external_id,
            calendar_source=CalendarSource.GOOGLE,
            summary="수면 (실제)",
            start_at=observation.start_at,
            end_at=observation.end_at,
            is_agent_created=True,
            healthmes_kind=identity.kind.value,
            healthmes_source=identity.source,
            healthmes_source_key=identity.source_key,
            observation_fingerprint=observation_fingerprint(observation),
            sleep_local_date=observation.local_date,
            sleep_duration_minutes=observation.duration_minutes,
            sleep_time_in_bed_minutes=observation.time_in_bed_minutes,
            status="healthmes_pending_create",
        )
    )
    session.commit()
    remote = ExternalEvent(
        external_id=external_id,
        summary="수면 (실제)",
        start_at=observation.start_at,
        end_at=observation.end_at,
        is_agent_created=True,
        identity=identity,
        etag='"remote-v1"',
        status="confirmed",
    )
    backend.event = remote
    backend.identity = identity
    backend.list_changes = lambda _state: ([remote], {"sync_token": "after-create"})

    # When
    CalendarMirrorService(
        session,
        [backend],
        InMemorySyncStateStore(),
    ).sync_backend(backend)
    result = SleepCalendarReconciler(session, backend).reconcile(observation)

    # Then
    assert result.action is SleepCalendarAction.NOOP
    assert backend.created_drafts == []
    mirror = session.query(CalendarEventMirror).one()
    assert mirror.is_agent_created
    assert mirror.status == "confirmed"


def test_identical_replay_refuses_changed_remote_identity_before_planned_delete(
    session,
    backend: RecordingCalendarBackend,
    observation: ActualSleepObservation,
) -> None:
    # Given
    reconciler = SleepCalendarReconciler(session, backend)
    reconciler.reconcile(observation)
    backend.identity = CalendarEventIdentity(
        kind=HealthmesEventKind.PLANNED_SLEEP,
        source="planner",
        source_key="proposal:changed",
    )

    # When / Then
    with pytest.raises(OwnershipError):
        reconciler.reconcile(observation)
    assert backend.update_calls == []
    assert backend.delete_calls == []


def test_provider_correction_uses_remote_etag_when_mirror_has_none(
    session,
    backend: RecordingCalendarBackend,
    observation: ActualSleepObservation,
) -> None:
    # Given
    reconciler = SleepCalendarReconciler(session, backend)
    reconciler.reconcile(observation)
    mirror = session.query(CalendarEventMirror).one()
    mirror.etag = None
    session.commit()
    corrected = replace(
        observation,
        end_at=datetime(2026, 7, 26, 7, 30, tzinfo=UTC),
        duration_minutes=450,
    )

    # When
    reconciler.reconcile(corrected)

    # Then
    assert backend.update_calls[0]["expected_etag"] == '"created"'


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
    assert backend.update_calls[0]["expected_etag"] == '"created"'
    assert result.external_id == created.external_id
    assert len(backend.created_drafts) == 1
    assert backend.update_calls == [
        {
            "external_id": "sleep-1",
            "summary": "수면 (실제)",
            "start_at": corrected.start_at,
            "end_at": corrected.end_at,
            "description": ("Actual sleep: 450 min\nTime in bed: 510 min\nSource: oura"),
            "expected_etag": '"created"',
        }
    ]
    mirror = session.query(CalendarEventMirror).one()
    assert mirror.external_id == "sleep-1"
    assert coerce_utc(mirror.end_at) == corrected.end_at
    assert mirror.etag == '"updated"'
    assert mirror.observation_fingerprint == result.observation_fingerprint
    assert mirror.sleep_duration_minutes == 450
    assert mirror.sleep_time_in_bed_minutes == 510


def test_provider_correction_recovers_after_remote_update_before_local_finalize(
    session_factory,
    backend: RecordingCalendarBackend,
    observation: ActualSleepObservation,
    monkeypatch,
) -> None:
    # Given
    with session_factory() as session:
        SleepCalendarReconciler(session, backend).reconcile(observation)
    corrected = replace(
        observation,
        end_at=datetime(2026, 7, 26, 7, 30, tzinfo=UTC),
        duration_minutes=450,
        time_in_bed_minutes=510,
    )
    with session_factory() as failing_session:
        real_commit = failing_session.commit

        def fail_after_remote_update() -> None:
            if backend.update_calls:
                raise RuntimeError("simulated local finalize failure")
            real_commit()

        monkeypatch.setattr(failing_session, "commit", fail_after_remote_update)
        with pytest.raises(RuntimeError, match="local finalize failure"):
            SleepCalendarReconciler(failing_session, backend).reconcile(corrected)
        failing_session.rollback()

    # When
    with session_factory() as preview_session:
        pending = preview_session.query(CalendarEventMirror).one()
        assert pending.status == "healthmes_pending_update"
        preview = preview_sleep_reconciliation(
            preview_session,
            CalendarSource.GOOGLE,
            corrected,
            backend,
        )
    with session_factory() as retry_session:
        result = SleepCalendarReconciler(retry_session, backend).reconcile(corrected)
        recovered = retry_session.query(CalendarEventMirror).one()

    # Then
    assert preview["action"] == "noop"
    assert result.action is SleepCalendarAction.UPDATED
    assert len(backend.update_calls) == 1
    assert recovered.status is None
    assert recovered.etag == '"updated"'
    assert recovered.observation_fingerprint == observation_fingerprint(corrected)


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
