from __future__ import annotations

from contextlib import contextmanager
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
from healthmes.calendars.sleep_event_rendering import description
from healthmes.calendars.sleep_observation import (
    ACTUAL_SLEEP_IDENTITY_SOURCE,
    ActualSleepObservation,
    actual_sleep_source_key,
)
from healthmes.calendars.sleep_preview import preview_sleep_reconciliation
from healthmes.calendars.sleep_reconciliation import (
    SleepCalendarAction,
    SleepCalendarReconciler,
    observation_fingerprint,
)
from healthmes.calendars.state import InMemorySyncStateStore
from healthmes.calendars.sync import CalendarMirrorService
from healthmes.store import CalendarEventMirror, CalendarSource


def actual_sleep_identity_for(
    observation: ActualSleepObservation,
) -> CalendarEventIdentity:
    return CalendarEventIdentity(
        kind=HealthmesEventKind.ACTUAL_SLEEP,
        source=ACTUAL_SLEEP_IDENTITY_SOURCE,
        source_key=actual_sleep_source_key(observation.local_date),
    )


def actual_sleep_external_id(
    observation: ActualSleepObservation,
) -> str:
    return calendar_identity_external_id(
        CalendarSource.GOOGLE,
        actual_sleep_identity_for(observation),
    )


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
        assert draft.identity is not None
        self.event = ExternalEvent(
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
            description=description,
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


class MultiEventBackend(RecordingCalendarBackend):
    def __init__(self) -> None:
        super().__init__()
        self.events: dict[str, ExternalEvent] = {}

    def create_event(self, draft: EventDraft) -> ExternalEvent:
        event = super().create_event(draft)
        self.events[event.external_id] = event
        return event

    def read_event(self, external_id: str) -> ExternalEvent:
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
        current = self.read_event(external_id)
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
        event = ExternalEvent(
            external_id=external_id,
            summary=summary,
            description=description,
            start_at=start_at,
            end_at=end_at,
            is_agent_created=True,
            identity=current.identity,
            etag='"updated"',
        )
        self.events[external_id] = event
        return event

    def delete_event(
        self,
        external_id: str,
        *,
        expected_kind: HealthmesEventKind | None = None,
        expected_etag: str | None = None,
    ) -> None:
        event = self.read_event(external_id)
        assert expected_kind is HealthmesEventKind.ACTUAL_SLEEP
        assert event.etag == expected_etag
        self.delete_calls.append(external_id)
        del self.events[external_id]


@pytest.fixture
def backend() -> RecordingCalendarBackend:
    return RecordingCalendarBackend()


@pytest.fixture
def observation() -> ActualSleepObservation:
    return ActualSleepObservation(
        local_date=date(2026, 7, 26),
        provider="oura",
        source_key=actual_sleep_source_key(date(2026, 7, 26)),
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
        source=ACTUAL_SLEEP_IDENTITY_SOURCE,
        source_key="actual_sleep:2026-07-26",
    )
    assert draft.description == description(observation)
    mirror = session.query(CalendarEventMirror).one()
    assert mirror.external_id == actual_sleep_external_id(observation)
    assert mirror.healthmes_kind == "actual_sleep"
    assert mirror.healthmes_source == ACTUAL_SLEEP_IDENTITY_SOURCE
    assert mirror.healthmes_source_key == "actual_sleep:2026-07-26"
    assert mirror.sleep_provider == "oura"
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
            external_id=actual_sleep_external_id(observation),
            calendar_source=CalendarSource.GOOGLE,
            summary="수면 (실제)",
            start_at=observation.start_at,
            end_at=observation.end_at,
            is_agent_created=True,
            healthmes_kind=HealthmesEventKind.ACTUAL_SLEEP.value,
            healthmes_source=ACTUAL_SLEEP_IDENTITY_SOURCE,
            healthmes_source_key=observation.source_key,
            observation_fingerprint=observation_fingerprint(observation),
            sleep_local_date=observation.local_date,
            sleep_provider=observation.provider,
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
    assert (
        session.query(CalendarEventMirror).one().external_id
        == actual_sleep_external_id(observation)
    )


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


def test_mirror_loss_adopts_matching_deterministic_remote_without_retry_loop(
    session,
    observation: ActualSleepObservation,
) -> None:
    class ExistingRemoteBackend(RecordingCalendarBackend):
        def create_event(self, draft: EventDraft) -> ExternalEvent:
            self.created_drafts.append(draft)
            raise CalendarConflictError("deterministic identity already exists")

    backend = ExistingRemoteBackend()
    backend.identity = actual_sleep_identity_for(observation)
    backend.event = ExternalEvent(
        external_id=actual_sleep_external_id(observation),
        summary="수면 (실제)",
        description=description(observation),
        start_at=observation.start_at,
        end_at=observation.end_at,
        is_agent_created=True,
        identity=backend.identity,
        etag='"remote"',
    )

    first = SleepCalendarReconciler(session, backend).reconcile(observation)
    second = SleepCalendarReconciler(session, backend).reconcile(observation)

    assert first.action is SleepCalendarAction.CREATED
    assert second.action is SleepCalendarAction.NOOP
    assert len(backend.created_drafts) == 1
    mirror = session.query(CalendarEventMirror).one()
    assert mirror.external_id == actual_sleep_external_id(observation)
    assert mirror.healthmes_source_key == observation.source_key


def test_create_rejects_success_response_with_wrong_deterministic_id(
    session,
    observation: ActualSleepObservation,
) -> None:
    class WrongIdBackend(RecordingCalendarBackend):
        def create_event(self, draft: EventDraft) -> ExternalEvent:
            created = super().create_event(draft)
            return replace(created, external_id="wrong-provider-id")

    with pytest.raises(OwnershipError):
        SleepCalendarReconciler(
            session,
            WrongIdBackend(),
        ).reconcile(observation)

    pending = session.query(CalendarEventMirror).one()
    assert pending.external_id == actual_sleep_external_id(observation)
    assert pending.status == "healthmes_pending_create"
    assert pending.observation_fingerprint == observation_fingerprint(observation)


def test_source_key_lock_spans_pending_commit_remote_create_and_finalize(
    session_factory,
    observation: ActualSleepObservation,
    monkeypatch,
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

    @contextmanager
    def recording_lock(_session, _source):
        timeline.append("lock")
        try:
            yield
        finally:
            timeline.append("unlock")

    monkeypatch.setattr(
        "healthmes.calendars.sleep_reconciliation.calendar_write_lock",
        recording_lock,
    )

    # When
    with session_factory() as session:
        result = SleepCalendarReconciler(
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
    identity = actual_sleep_identity_for(observation)
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
        description=description(observation),
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
            "external_id": actual_sleep_external_id(observation),
            "summary": "수면 (실제)",
            "start_at": corrected.start_at,
            "end_at": corrected.end_at,
            "description": description(corrected),
            "expected_etag": '"created"',
        }
    ]
    mirror = session.query(CalendarEventMirror).one()
    assert mirror.external_id == actual_sleep_external_id(observation)
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
    latest = replace(
        corrected,
        provider="garmin",
        end_at=datetime(2026, 7, 26, 7, 45, tzinfo=UTC),
        duration_minutes=465,
        time_in_bed_minutes=525,
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
            latest,
            backend,
        )
    with session_factory() as retry_session:
        result = SleepCalendarReconciler(retry_session, backend).reconcile(latest)
        recovered = retry_session.query(CalendarEventMirror).one()

    # Then
    assert preview["action"] == "would_update"
    assert result.action is SleepCalendarAction.UPDATED
    assert len(backend.update_calls) == 2
    assert recovered.status is None
    assert recovered.etag == '"updated"'
    assert recovered.observation_fingerprint == observation_fingerprint(latest)
    assert recovered.sleep_provider == "garmin"
    assert recovered.sleep_duration_minutes == 465
    assert recovered.sleep_time_in_bed_minutes == 525
    assert coerce_utc(recovered.end_at) == latest.end_at


def test_metrics_only_pending_recovery_does_not_trust_unrelated_etag_change(
    session,
    backend: RecordingCalendarBackend,
    observation: ActualSleepObservation,
) -> None:
    SleepCalendarReconciler(session, backend).reconcile(observation)
    corrected = replace(
        observation,
        duration_minutes=435,
        time_in_bed_minutes=495,
    )
    mirror = session.query(CalendarEventMirror).one()
    mirror.observation_fingerprint = observation_fingerprint(corrected)
    mirror.sleep_duration_minutes = corrected.duration_minutes
    mirror.sleep_time_in_bed_minutes = corrected.time_in_bed_minutes
    mirror.status = "healthmes_pending_update"
    mirror.etag = '"created"'
    session.commit()
    assert backend.event is not None
    backend.event = replace(
        backend.event,
        etag='"unrelated-change"',
        description=description(observation),
    )

    result = SleepCalendarReconciler(session, backend).reconcile(corrected)

    assert result.action is SleepCalendarAction.UPDATED
    assert backend.update_calls[-1]["expected_etag"] == '"unrelated-change"'
    assert backend.update_calls[-1]["description"] == description(corrected)
    recovered = session.query(CalendarEventMirror).one()
    assert recovered.sleep_duration_minutes == 435
    assert recovered.sleep_time_in_bed_minutes == 495
    assert recovered.status is None


def test_update_rejects_success_response_with_wrong_description(
    session,
    observation: ActualSleepObservation,
) -> None:
    class WrongContentBackend(RecordingCalendarBackend):
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
            updated = super().update_event(
                external_id,
                summary=summary,
                start_at=start_at,
                end_at=end_at,
                description=description,
                expected_etag=expected_etag,
            )
            return replace(updated, description="provider-dropped-fingerprint")

    backend = WrongContentBackend()
    SleepCalendarReconciler(session, backend).reconcile(observation)
    corrected = replace(
        observation,
        duration_minutes=435,
        time_in_bed_minutes=495,
    )

    with pytest.raises(CalendarConflictError, match="differs from intent"):
        SleepCalendarReconciler(session, backend).reconcile(corrected)

    pending = session.query(CalendarEventMirror).one()
    assert pending.status == "healthmes_pending_update"
    assert pending.observation_fingerprint == observation_fingerprint(corrected)
    assert pending.sleep_duration_minutes == corrected.duration_minutes


def test_pending_create_recovers_original_intent_before_newer_correction(
    session,
    backend: RecordingCalendarBackend,
    observation: ActualSleepObservation,
) -> None:
    original_fingerprint = observation_fingerprint(observation)
    identity = CalendarEventIdentity(
        kind=HealthmesEventKind.ACTUAL_SLEEP,
        source=ACTUAL_SLEEP_IDENTITY_SOURCE,
        source_key=observation.source_key,
    )
    session.add(
        CalendarEventMirror(
            external_id=calendar_identity_external_id(
                CalendarSource.GOOGLE,
                identity,
            ),
            calendar_source=CalendarSource.GOOGLE,
            summary="수면 (실제)",
            start_at=observation.start_at,
            end_at=observation.end_at,
            is_agent_created=True,
            healthmes_kind=identity.kind.value,
            healthmes_source=identity.source,
            healthmes_source_key=identity.source_key,
            observation_fingerprint=original_fingerprint,
            sleep_local_date=observation.local_date,
            sleep_provider=observation.provider,
            sleep_duration_minutes=observation.duration_minutes,
            sleep_time_in_bed_minutes=observation.time_in_bed_minutes,
            status="healthmes_pending_create",
        )
    )
    session.commit()
    latest = replace(
        observation,
        provider="garmin",
        end_at=datetime(2026, 7, 26, 7, 30, tzinfo=UTC),
        duration_minutes=450,
        time_in_bed_minutes=510,
    )

    result = SleepCalendarReconciler(session, backend).reconcile(latest)

    assert result.action is SleepCalendarAction.UPDATED
    assert len(backend.created_drafts) == 1
    assert backend.created_drafts[0].end_at == observation.end_at
    assert len(backend.update_calls) == 1
    assert backend.update_calls[0]["end_at"] == latest.end_at
    mirror = session.query(CalendarEventMirror).one()
    assert mirror.observation_fingerprint == observation_fingerprint(latest)
    assert mirror.sleep_provider == "garmin"
    assert mirror.sleep_duration_minutes == 450
    assert mirror.sleep_time_in_bed_minutes == 510


def test_provider_winner_switch_updates_same_canonical_event(
    session,
    backend: RecordingCalendarBackend,
    observation: ActualSleepObservation,
) -> None:
    reconciler = SleepCalendarReconciler(session, backend)
    created = reconciler.reconcile(observation)
    switched = replace(
        observation,
        provider="garmin",
        start_at=datetime(2026, 7, 25, 22, 45, tzinfo=UTC),
        end_at=datetime(2026, 7, 26, 7, 15, tzinfo=UTC),
        duration_minutes=450,
        time_in_bed_minutes=510,
    )

    updated = reconciler.reconcile(switched)

    assert created.external_id == updated.external_id
    assert len(backend.created_drafts) == 1
    assert len(backend.update_calls) == 1
    assert backend.identity == CalendarEventIdentity(
        kind=HealthmesEventKind.ACTUAL_SLEEP,
        source=ACTUAL_SLEEP_IDENTITY_SOURCE,
        source_key=actual_sleep_source_key(observation.local_date),
    )
    mirror = session.query(CalendarEventMirror).one()
    assert mirror.sleep_provider == "garmin"
    assert mirror.healthmes_source == ACTUAL_SLEEP_IDENTITY_SOURCE


def test_legacy_provider_identity_is_replaced_by_canonical_event(
    session,
    observation: ActualSleepObservation,
) -> None:
    backend = MultiEventBackend()
    legacy_identity = CalendarEventIdentity(
        kind=HealthmesEventKind.ACTUAL_SLEEP,
        source="oura",
        source_key="oura:2026-07-26",
    )
    legacy_external_id = calendar_identity_external_id(
        CalendarSource.GOOGLE,
        legacy_identity,
    )
    backend.events[legacy_external_id] = ExternalEvent(
        external_id=legacy_external_id,
        summary="수면 (실제)",
        start_at=observation.start_at,
        end_at=observation.end_at,
        is_agent_created=True,
        identity=legacy_identity,
        etag='"legacy"',
    )
    session.add(
        CalendarEventMirror(
            external_id=legacy_external_id,
            calendar_source=CalendarSource.GOOGLE,
            summary="수면 (실제)",
            start_at=observation.start_at,
            end_at=observation.end_at,
            is_agent_created=True,
            healthmes_kind=legacy_identity.kind.value,
            healthmes_source=legacy_identity.source,
            healthmes_source_key=legacy_identity.source_key,
            observation_fingerprint=observation_fingerprint(observation),
            sleep_local_date=observation.local_date,
            sleep_provider="oura",
            sleep_duration_minutes=observation.duration_minutes,
            sleep_time_in_bed_minutes=observation.time_in_bed_minutes,
            etag='"legacy"',
        )
    )
    session.commit()
    switched = replace(
        observation,
        provider="garmin",
        end_at=datetime(2026, 7, 26, 7, 30, tzinfo=UTC),
        duration_minutes=450,
    )

    result = SleepCalendarReconciler(session, backend).reconcile(switched)

    assert result.action is SleepCalendarAction.CREATED
    assert len(backend.created_drafts) == 1
    assert backend.update_calls == []
    assert backend.delete_calls == [legacy_external_id]
    mirror = session.query(CalendarEventMirror).one()
    assert mirror.external_id == actual_sleep_external_id(observation)
    assert mirror.healthmes_source_key == observation.source_key
    assert mirror.sleep_provider == "garmin"


def test_legacy_provider_identity_with_canonical_source_key_is_deleted(
    session,
    observation: ActualSleepObservation,
) -> None:
    backend = MultiEventBackend()
    legacy_identity = CalendarEventIdentity(
        kind=HealthmesEventKind.ACTUAL_SLEEP,
        source="oura",
        source_key=observation.source_key,
    )
    legacy_external_id = calendar_identity_external_id(
        CalendarSource.GOOGLE,
        legacy_identity,
    )
    backend.events[legacy_external_id] = ExternalEvent(
        external_id=legacy_external_id,
        summary="수면 (실제)",
        description=description(observation),
        start_at=observation.start_at,
        end_at=observation.end_at,
        is_agent_created=True,
        identity=legacy_identity,
        etag='"legacy"',
    )
    session.add(
        CalendarEventMirror(
            external_id=legacy_external_id,
            calendar_source=CalendarSource.GOOGLE,
            summary="수면 (실제)",
            start_at=observation.start_at,
            end_at=observation.end_at,
            is_agent_created=True,
            healthmes_kind=legacy_identity.kind.value,
            healthmes_source=legacy_identity.source,
            healthmes_source_key=legacy_identity.source_key,
            observation_fingerprint=observation_fingerprint(observation),
            sleep_local_date=observation.local_date,
            sleep_provider=observation.provider,
            sleep_duration_minutes=observation.duration_minutes,
            sleep_time_in_bed_minutes=observation.time_in_bed_minutes,
            etag='"legacy"',
        )
    )
    session.commit()

    result = SleepCalendarReconciler(session, backend).reconcile(observation)

    assert result.action is SleepCalendarAction.CREATED
    assert backend.delete_calls == [legacy_external_id]
    [mirror] = session.query(CalendarEventMirror).all()
    assert mirror.external_id == actual_sleep_external_id(observation)


def test_multiple_legacy_provider_rows_collapse_to_current_winner(
    session,
    observation: ActualSleepObservation,
) -> None:
    backend = MultiEventBackend()
    switched = replace(
        observation,
        provider="garmin",
        start_at=datetime(2026, 7, 25, 22, 45, tzinfo=UTC),
        end_at=datetime(2026, 7, 26, 7, 15, tzinfo=UTC),
        duration_minutes=450,
        time_in_bed_minutes=510,
    )
    identities = {
        "oura": CalendarEventIdentity(
            kind=HealthmesEventKind.ACTUAL_SLEEP,
            source="oura",
            source_key="oura:2026-07-26",
        ),
        "garmin": CalendarEventIdentity(
            kind=HealthmesEventKind.ACTUAL_SLEEP,
            source="garmin",
            source_key="garmin:2026-07-26",
        ),
    }
    observations = {
        "oura": observation,
        "garmin": switched,
    }
    legacy_external_ids: dict[str, str] = {}
    for provider, identity in identities.items():
        external_id = calendar_identity_external_id(
            CalendarSource.GOOGLE,
            identity,
        )
        legacy_external_ids[provider] = external_id
        row_observation = observations[provider]
        backend.events[external_id] = ExternalEvent(
            external_id=external_id,
            summary="수면 (실제)",
            start_at=row_observation.start_at,
            end_at=row_observation.end_at,
            is_agent_created=True,
            identity=identity,
            etag=f'"{external_id}"',
        )
        session.add(
            CalendarEventMirror(
                external_id=external_id,
                calendar_source=CalendarSource.GOOGLE,
                summary="수면 (실제)",
                start_at=row_observation.start_at,
                end_at=row_observation.end_at,
                is_agent_created=True,
                healthmes_kind=identity.kind.value,
                healthmes_source=identity.source,
                healthmes_source_key=identity.source_key,
                observation_fingerprint=observation_fingerprint(row_observation),
                sleep_local_date=row_observation.local_date,
                sleep_provider=row_observation.provider,
                sleep_duration_minutes=row_observation.duration_minutes,
                sleep_time_in_bed_minutes=row_observation.time_in_bed_minutes,
                etag=f'"{external_id}"',
            )
        )
    session.commit()

    result = SleepCalendarReconciler(session, backend).reconcile(switched)

    assert result.action is SleepCalendarAction.CREATED
    assert set(backend.delete_calls) == set(legacy_external_ids.values())
    assert set(backend.events) == {actual_sleep_external_id(observation)}
    [mirror] = session.query(CalendarEventMirror).all()
    assert mirror.external_id == actual_sleep_external_id(observation)
    assert mirror.sleep_provider == "garmin"


def test_duplicate_cleanup_waits_for_primary_remote_validation(
    session,
    observation: ActualSleepObservation,
) -> None:
    backend = MultiEventBackend()
    switched = replace(observation, provider="garmin")
    identities = (
        CalendarEventIdentity(
            kind=HealthmesEventKind.ACTUAL_SLEEP,
            source="oura",
            source_key="oura:2026-07-26",
        ),
        CalendarEventIdentity(
            kind=HealthmesEventKind.ACTUAL_SLEEP,
            source="garmin",
            source_key="garmin:2026-07-26",
        ),
    )
    for identity in identities:
        external_id = calendar_identity_external_id(
            CalendarSource.GOOGLE,
            identity,
        )
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
                observation_fingerprint=observation_fingerprint(switched),
                sleep_local_date=observation.local_date,
                sleep_provider=identity.source,
                sleep_duration_minutes=observation.duration_minutes,
                sleep_time_in_bed_minutes=observation.time_in_bed_minutes,
                etag='"legacy"',
            )
        )
    session.commit()
    backend.identity = CalendarEventIdentity(
        kind=HealthmesEventKind.PLANNED_SLEEP,
        source="planner",
        source_key="proposal:changed",
    )
    backend.event = ExternalEvent(
        external_id=calendar_identity_external_id(
            CalendarSource.GOOGLE,
            identities[1],
        ),
        summary="수면 (실제)",
        start_at=observation.start_at,
        end_at=observation.end_at,
        is_agent_created=True,
        identity=backend.identity,
        etag='"legacy"',
    )
    backend.events[
        calendar_identity_external_id(
            CalendarSource.GOOGLE,
            identities[0],
        )
    ] = ExternalEvent(
        external_id=calendar_identity_external_id(
            CalendarSource.GOOGLE,
            identities[0],
        ),
        summary="수면 (실제)",
        start_at=observation.start_at,
        end_at=observation.end_at,
        is_agent_created=True,
        identity=identities[0],
        etag='"legacy"',
    )
    backend.events[backend.event.external_id] = backend.event

    with pytest.raises(OwnershipError):
        SleepCalendarReconciler(session, backend).reconcile(switched)

    assert backend.delete_calls == []
    assert session.query(CalendarEventMirror).count() == 2


def test_duplicate_cleanup_refuses_remote_etag_change(
    session,
    observation: ActualSleepObservation,
) -> None:
    backend = MultiEventBackend()
    canonical_identity = actual_sleep_identity_for(observation)
    canonical_external_id = calendar_identity_external_id(
        CalendarSource.GOOGLE,
        canonical_identity,
    )
    legacy_identity = CalendarEventIdentity(
        kind=HealthmesEventKind.ACTUAL_SLEEP,
        source="oura",
        source_key="oura:2026-07-26",
    )
    legacy_external_id = calendar_identity_external_id(
        CalendarSource.GOOGLE,
        legacy_identity,
    )
    for external_id, identity, etag in (
        (canonical_external_id, canonical_identity, '"canonical"'),
        (legacy_external_id, legacy_identity, '"remote-changed"'),
    ):
        backend.events[external_id] = ExternalEvent(
            external_id=external_id,
            summary="수면 (실제)",
            description=description(observation),
            start_at=observation.start_at,
            end_at=observation.end_at,
            is_agent_created=True,
            identity=identity,
            etag=etag,
        )
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
                sleep_provider=observation.provider,
                sleep_duration_minutes=observation.duration_minutes,
                sleep_time_in_bed_minutes=observation.time_in_bed_minutes,
                etag=(
                    etag
                    if identity == canonical_identity
                    else '"mirror-before-external-edit"'
                ),
            )
        )
    session.commit()

    with pytest.raises(CalendarConflictError, match="changed after sync"):
        SleepCalendarReconciler(session, backend).reconcile(observation)

    assert backend.delete_calls == []
    assert session.query(CalendarEventMirror).count() == 2


def test_quarantines_source_key_collision_with_unowned_event(
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

    result = SleepCalendarReconciler(session, backend).reconcile(observation)

    assert result.action is SleepCalendarAction.CREATED
    assert len(backend.created_drafts) == 1
    assert backend.update_calls == []
    rows = session.query(CalendarEventMirror).order_by(CalendarEventMirror.external_id).all()
    assert len(rows) == 2
    assert not rows[0].is_agent_created
    assert rows[0].healthmes_source_key is None
    assert rows[1].external_id == actual_sleep_external_id(observation)


def test_legacy_history_canonicalizes_dates_outside_recent_window(
    session,
    observation: ActualSleepObservation,
) -> None:
    backend = MultiEventBackend()
    old_observation = replace(
        observation,
        local_date=date(2024, 1, 2),
        source_key=actual_sleep_source_key(date(2024, 1, 2)),
        start_at=datetime(2024, 1, 1, 23, tzinfo=UTC),
        end_at=datetime(2024, 1, 2, 7, tzinfo=UTC),
    )
    for provider in ("oura", "garmin"):
        identity = CalendarEventIdentity(
            kind=HealthmesEventKind.ACTUAL_SLEEP,
            source=provider,
            source_key=f"{provider}:2024-01-02",
        )
        external_id = calendar_identity_external_id(
            CalendarSource.GOOGLE,
            identity,
        )
        backend.events[external_id] = ExternalEvent(
            external_id=external_id,
            summary="수면 (실제)",
            start_at=old_observation.start_at,
            end_at=old_observation.end_at,
            is_agent_created=True,
            identity=identity,
            etag=f'"{provider}"',
        )
        session.add(
            CalendarEventMirror(
                external_id=external_id,
                calendar_source=CalendarSource.GOOGLE,
                summary="수면 (실제)",
                start_at=old_observation.start_at,
                end_at=old_observation.end_at,
                is_agent_created=True,
                healthmes_kind=identity.kind.value,
                healthmes_source=identity.source,
                healthmes_source_key=identity.source_key,
                sleep_local_date=old_observation.local_date,
                sleep_provider=provider,
                sleep_duration_minutes=old_observation.duration_minutes,
                sleep_time_in_bed_minutes=old_observation.time_in_bed_minutes,
                etag=f'"{provider}"',
            )
        )
    session.commit()

    cleanup = SleepCalendarReconciler(
        session,
        backend,
    ).reconcile_legacy_history()

    assert cleanup == {
        "migrated": 1,
        "quarantined": 0,
        "removed_missing": 0,
        "failed": 0,
    }
    [mirror] = session.query(CalendarEventMirror).all()
    assert mirror.external_id == actual_sleep_external_id(old_observation)
    assert mirror.healthmes_source == ACTUAL_SLEEP_IDENTITY_SOURCE
    assert len(backend.delete_calls) == 2


def test_legacy_history_preserves_existing_canonical_observation(
    session,
    observation: ActualSleepObservation,
) -> None:
    backend = MultiEventBackend()
    canonical = replace(
        observation,
        provider="garmin",
        end_at=datetime(2026, 7, 26, 7, 45, tzinfo=UTC),
        duration_minutes=465,
        time_in_bed_minutes=525,
    )
    canonical_identity = actual_sleep_identity_for(canonical)
    canonical_external_id = calendar_identity_external_id(
        CalendarSource.GOOGLE,
        canonical_identity,
    )
    legacy_identity = CalendarEventIdentity(
        kind=HealthmesEventKind.ACTUAL_SLEEP,
        source="oura",
        source_key="oura:2026-07-26",
    )
    legacy_external_id = calendar_identity_external_id(
        CalendarSource.GOOGLE,
        legacy_identity,
    )
    for external_id, identity, stored in (
        (canonical_external_id, canonical_identity, canonical),
        (legacy_external_id, legacy_identity, observation),
    ):
        backend.events[external_id] = ExternalEvent(
            external_id=external_id,
            summary="수면 (실제)",
            description=description(stored),
            start_at=stored.start_at,
            end_at=stored.end_at,
            is_agent_created=True,
            identity=identity,
            etag=f'"{identity.source}"',
        )
        session.add(
            CalendarEventMirror(
                external_id=external_id,
                calendar_source=CalendarSource.GOOGLE,
                summary="수면 (실제)",
                start_at=stored.start_at,
                end_at=stored.end_at,
                is_agent_created=True,
                healthmes_kind=identity.kind.value,
                healthmes_source=identity.source,
                healthmes_source_key=identity.source_key,
                observation_fingerprint=observation_fingerprint(stored),
                sleep_local_date=stored.local_date,
                sleep_provider=stored.provider,
                sleep_duration_minutes=(
                    None
                    if identity == legacy_identity
                    else stored.duration_minutes
                ),
                sleep_time_in_bed_minutes=stored.time_in_bed_minutes,
                etag=f'"{identity.source}"',
            )
        )
    session.commit()

    cleanup = SleepCalendarReconciler(
        session,
        backend,
    ).reconcile_legacy_history()

    assert cleanup["migrated"] == 1
    assert backend.update_calls == []
    assert backend.delete_calls == [legacy_external_id]
    [mirror] = session.query(CalendarEventMirror).all()
    assert mirror.external_id == canonical_external_id
    assert mirror.sleep_provider == "garmin"
    assert mirror.sleep_duration_minutes == 465
    assert coerce_utc(mirror.end_at) == canonical.end_at


def test_legacy_history_refuses_remote_etag_change(
    session,
    observation: ActualSleepObservation,
) -> None:
    backend = MultiEventBackend()
    old_observation = replace(
        observation,
        local_date=date(2024, 1, 2),
        source_key=actual_sleep_source_key(date(2024, 1, 2)),
        start_at=datetime(2024, 1, 1, 23, tzinfo=UTC),
        end_at=datetime(2024, 1, 2, 7, tzinfo=UTC),
    )
    legacy_identity = CalendarEventIdentity(
        kind=HealthmesEventKind.ACTUAL_SLEEP,
        source="oura",
        source_key="oura:2024-01-02",
    )
    legacy_external_id = calendar_identity_external_id(
        CalendarSource.GOOGLE,
        legacy_identity,
    )
    backend.events[legacy_external_id] = ExternalEvent(
        external_id=legacy_external_id,
        summary="Externally changed sleep",
        description=description(old_observation),
        start_at=old_observation.start_at,
        end_at=old_observation.end_at,
        is_agent_created=True,
        identity=legacy_identity,
        etag='"remote-changed"',
    )
    session.add(
        CalendarEventMirror(
            external_id=legacy_external_id,
            calendar_source=CalendarSource.GOOGLE,
            summary="수면 (실제)",
            start_at=old_observation.start_at,
            end_at=old_observation.end_at,
            is_agent_created=True,
            healthmes_kind=legacy_identity.kind.value,
            healthmes_source=legacy_identity.source,
            healthmes_source_key=legacy_identity.source_key,
            observation_fingerprint=observation_fingerprint(old_observation),
            sleep_local_date=old_observation.local_date,
            sleep_provider=old_observation.provider,
            sleep_duration_minutes=old_observation.duration_minutes,
            sleep_time_in_bed_minutes=old_observation.time_in_bed_minutes,
            etag='"mirror-before-external-edit"',
        )
    )
    session.commit()

    cleanup = SleepCalendarReconciler(
        session,
        backend,
    ).reconcile_legacy_history()

    assert cleanup == {
        "migrated": 0,
        "quarantined": 0,
        "removed_missing": 0,
        "failed": 1,
    }
    assert backend.created_drafts == []
    assert backend.delete_calls == []
    assert session.query(CalendarEventMirror).one().external_id == legacy_external_id


def test_preview_reports_canonical_create_for_legacy_identity(
    session,
    observation: ActualSleepObservation,
) -> None:
    backend = MultiEventBackend()
    legacy_identity = CalendarEventIdentity(
        kind=HealthmesEventKind.ACTUAL_SLEEP,
        source="oura",
        source_key="oura:2026-07-26",
    )
    legacy_external_id = calendar_identity_external_id(
        CalendarSource.GOOGLE,
        legacy_identity,
    )
    backend.events[legacy_external_id] = ExternalEvent(
        external_id=legacy_external_id,
        summary="수면 (실제)",
        description=description(observation),
        start_at=observation.start_at,
        end_at=observation.end_at,
        is_agent_created=True,
        identity=legacy_identity,
        etag='"legacy"',
    )
    session.add(
        CalendarEventMirror(
            external_id=legacy_external_id,
            calendar_source=CalendarSource.GOOGLE,
            summary="수면 (실제)",
            start_at=observation.start_at,
            end_at=observation.end_at,
            is_agent_created=True,
            healthmes_kind=legacy_identity.kind.value,
            healthmes_source=legacy_identity.source,
            healthmes_source_key=legacy_identity.source_key,
            observation_fingerprint=observation_fingerprint(observation),
            sleep_local_date=observation.local_date,
            sleep_provider=observation.provider,
            sleep_duration_minutes=observation.duration_minutes,
            sleep_time_in_bed_minutes=observation.time_in_bed_minutes,
            etag='"legacy"',
        )
    )
    session.commit()

    preview = preview_sleep_reconciliation(
        session,
        CalendarSource.GOOGLE,
        observation,
        backend,
    )

    assert preview["action"] == "would_create"


def test_preview_blocks_changed_legacy_remote_identity_before_create(
    session,
    observation: ActualSleepObservation,
) -> None:
    backend = MultiEventBackend()
    legacy_identity = CalendarEventIdentity(
        kind=HealthmesEventKind.ACTUAL_SLEEP,
        source="oura",
        source_key="oura:2026-07-26",
    )
    legacy_external_id = calendar_identity_external_id(
        CalendarSource.GOOGLE,
        legacy_identity,
    )
    backend.events[legacy_external_id] = ExternalEvent(
        external_id=legacy_external_id,
        summary="Changed owner",
        start_at=observation.start_at,
        end_at=observation.end_at,
        is_agent_created=True,
        identity=CalendarEventIdentity(
            kind=HealthmesEventKind.PLANNED_SLEEP,
            source="planner",
            source_key="proposal:changed",
        ),
        etag='"legacy"',
    )
    session.add(
        CalendarEventMirror(
            external_id=legacy_external_id,
            calendar_source=CalendarSource.GOOGLE,
            summary="수면 (실제)",
            start_at=observation.start_at,
            end_at=observation.end_at,
            is_agent_created=True,
            healthmes_kind=legacy_identity.kind.value,
            healthmes_source=legacy_identity.source,
            healthmes_source_key=legacy_identity.source_key,
            observation_fingerprint=observation_fingerprint(observation),
            sleep_local_date=observation.local_date,
            sleep_provider=observation.provider,
            sleep_duration_minutes=observation.duration_minutes,
            sleep_time_in_bed_minutes=observation.time_in_bed_minutes,
            etag='"legacy"',
        )
    )
    session.commit()

    preview = preview_sleep_reconciliation(
        session,
        CalendarSource.GOOGLE,
        observation,
        backend,
    )

    assert preview["action"] == "blocked"
    assert preview["reason"] == "ownership_mismatch"
    assert backend.created_drafts == []
    assert backend.delete_calls == []


def test_preview_blocks_unowned_event_at_canonical_deterministic_id(
    session,
    observation: ActualSleepObservation,
) -> None:
    backend = MultiEventBackend()
    external_id = actual_sleep_external_id(observation)
    backend.events[external_id] = ExternalEvent(
        external_id=external_id,
        summary="User-owned conflict",
        start_at=observation.start_at,
        end_at=observation.end_at,
        is_agent_created=False,
        etag='"external"',
    )
    session.add(
        CalendarEventMirror(
            external_id=external_id,
            calendar_source=CalendarSource.GOOGLE,
            summary="User-owned conflict",
            start_at=observation.start_at,
            end_at=observation.end_at,
            is_agent_created=False,
            etag='"external"',
        )
    )
    session.commit()

    preview = preview_sleep_reconciliation(
        session,
        CalendarSource.GOOGLE,
        observation,
        backend,
    )

    assert preview["action"] == "blocked"
    assert preview["reason"] == "ownership_mismatch"
    assert backend.created_drafts == []


def test_exact_identity_is_unique_within_configured_calendar(
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
                healthmes_kind=HealthmesEventKind.ACTUAL_SLEEP.value,
                healthmes_source=ACTUAL_SLEEP_IDENTITY_SOURCE,
                healthmes_source_key=observation.source_key,
            )
        )

    # When / Then
    with pytest.raises(IntegrityError):
        session.commit()


def test_same_source_key_allows_distinct_legacy_source_identity(
    session,
    observation: ActualSleepObservation,
) -> None:
    for source in (ACTUAL_SLEEP_IDENTITY_SOURCE, "oura"):
        session.add(
            CalendarEventMirror(
                external_id=f"sleep-{source}",
                calendar_source=CalendarSource.GOOGLE,
                start_at=observation.start_at,
                end_at=observation.end_at,
                healthmes_kind=HealthmesEventKind.ACTUAL_SLEEP.value,
                healthmes_source=source,
                healthmes_source_key=observation.source_key,
            )
        )

    session.commit()

    assert session.query(CalendarEventMirror).count() == 2
