from __future__ import annotations

import logging
from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy import select

from healthmes.calendars.base import (
    CalendarConflictError,
    CalendarEventIdentity,
    EventNotFoundError,
    ExternalEvent,
    HealthmesEventKind,
)
from healthmes.calendars.sleep_job import (
    build_sleep_reconciliation_job,
    reconcile_recent_sleep,
)
from healthmes.store import CalendarEventMirror, CalendarSource


class SleepReader:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.requests: list[tuple[str, str, str]] = []

    async def collect_sleep_summaries(
        self,
        user_id: str,
        start_date: str,
        end_date: str,
    ) -> list[dict[str, object]]:
        self.requests.append((user_id, start_date, end_date))
        return self.rows


class FailingSleepReader:
    async def collect_sleep_summaries(
        self,
        user_id: str,
        start_date: str,
        end_date: str,
    ) -> list[dict[str, object]]:
        request = httpx.Request(
            "GET",
            f"https://wearables.invalid/api/v1/users/{user_id}/summaries/sleep",
        )
        response = httpx.Response(500, request=request)
        raise httpx.HTTPStatusError(
            f"failed for {user_id}",
            request=request,
            response=response,
        )


class ReadOnlyBackend:
    source = CalendarSource.GOOGLE

    def __init__(self, events: dict[str, ExternalEvent] | None = None) -> None:
        self.events = events or {}

    def read_event(self, external_id: str) -> ExternalEvent:
        try:
            return self.events[external_id]
        except KeyError as exc:
            raise EventNotFoundError(external_id) from exc


def _summary(*, duration: int = 420, wake_hour: int = 7) -> dict[str, object]:
    return {
        "date": "2026-07-26",
        "source": {"provider": "oura"},
        "start_time": "2026-07-25T23:00:00+09:00",
        "end_time": f"2026-07-26T{wake_hour:02d}:00:00+09:00",
        "duration_minutes": duration,
        "time_in_bed_minutes": duration + 30,
    }


async def test_runtime_create_replay_and_provider_correction(
    session_factory,
    fake_backend,
) -> None:
    # Given
    reader = SleepReader([_summary()])

    # When
    created = await reconcile_recent_sleep(
        target_date=date(2026, 7, 26),
        calendar_source=CalendarSource.GOOGLE,
        client=reader,
        user_id="redacted-user",
        session_factory=session_factory,
        backend=fake_backend,
    )
    replayed = await reconcile_recent_sleep(
        target_date=date(2026, 7, 26),
        calendar_source=CalendarSource.GOOGLE,
        client=reader,
        user_id="redacted-user",
        session_factory=session_factory,
        backend=fake_backend,
    )
    reader.rows = [_summary(duration=450, wake_hour=8)]
    corrected = await reconcile_recent_sleep(
        target_date=date(2026, 7, 26),
        calendar_source=CalendarSource.GOOGLE,
        client=reader,
        user_id="redacted-user",
        session_factory=session_factory,
        backend=fake_backend,
    )

    # Then
    assert [created["action"], replayed["action"], corrected["action"]] == [
        "created",
        "noop",
        "updated",
    ]
    assert len(fake_backend.created_drafts) == 1
    assert len(fake_backend.update_calls) == 1
    assert reader.requests == [
        ("redacted-user", "2026-07-26", "2026-07-27"),
        ("redacted-user", "2026-07-26", "2026-07-27"),
        ("redacted-user", "2026-07-26", "2026-07-27"),
    ]


async def test_provider_failure_keeps_planned_sleep_and_pending_actual_for_retry(
    session_factory,
    fake_backend,
    monkeypatch,
) -> None:
    # Given
    reader = SleepReader([_summary()])
    planned = ExternalEvent(
        external_id="planned-provider-failure",
        summary="수면 (계획)",
        start_at=datetime(2026, 7, 25, 13, tzinfo=UTC),
        end_at=datetime(2026, 7, 26, 1, tzinfo=UTC),
        is_agent_created=True,
        identity=CalendarEventIdentity(
            kind=HealthmesEventKind.PLANNED_SLEEP,
            source="planner",
            source_key="proposal:provider-failure",
        ),
        healthmes_kind=HealthmesEventKind.PLANNED_SLEEP,
        etag='"planned-v1"',
    )
    fake_backend.events[planned.external_id] = planned
    with session_factory() as session:
        session.add(
            CalendarEventMirror(
                external_id=planned.external_id,
                calendar_source=CalendarSource.GOOGLE,
                summary=planned.summary,
                start_at=planned.start_at,
                end_at=planned.end_at,
                is_agent_created=True,
                healthmes_kind=HealthmesEventKind.PLANNED_SLEEP.value,
                healthmes_source="planner",
                healthmes_source_key="proposal:provider-failure",
                etag=planned.etag,
            )
        )
        session.commit()
    create_event = fake_backend.create_event
    attempts = 0

    def fail_once(draft):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("provider unavailable")
        return create_event(draft)

    monkeypatch.setattr(fake_backend, "create_event", fail_once)
    with pytest.raises(RuntimeError):
        await reconcile_recent_sleep(
            target_date=date(2026, 7, 26),
            calendar_source=CalendarSource.GOOGLE,
            client=reader,
            user_id="redacted-user",
            session_factory=session_factory,
            backend=fake_backend,
        )
    with session_factory() as session:
        rows_after_failure = list(session.scalars(select(CalendarEventMirror)))
    assert planned.external_id in fake_backend.events
    assert len(rows_after_failure) == 2
    assert {row.status for row in rows_after_failure} == {
        None,
        "healthmes_pending_create",
    }

    # When
    result = await reconcile_recent_sleep(
        target_date=date(2026, 7, 26),
        calendar_source=CalendarSource.GOOGLE,
        client=reader,
        user_id="redacted-user",
        session_factory=session_factory,
        backend=fake_backend,
    )

    # Then
    assert result["action"] == "created"
    assert attempts == 2
    with session_factory() as session:
        row = session.query(CalendarEventMirror).one()
        assert row.status != "healthmes_pending_create"
        assert row.healthmes_kind == HealthmesEventKind.ACTUAL_SLEEP.value
    assert planned.external_id not in fake_backend.events


async def test_dry_run_is_exact_redacted_and_mutation_free(
    session_factory,
    fake_backend,
) -> None:
    # Given
    reader = SleepReader([_summary()])

    # When
    preview = await reconcile_recent_sleep(
        target_date=date(2026, 7, 26),
        calendar_source=CalendarSource.GOOGLE,
        client=reader,
        user_id="must-not-leak",
        session_factory=session_factory,
        backend=fake_backend,
        dry_run=True,
    )

    # Then
    assert preview == {
        "status": "preview",
        "action": "would_create",
        "calendar": "google",
        "local_date": "2026-07-26",
        "summary": "Oura 수면 세션",
        "start": "2026-07-25T14:00:00+00:00",
        "wake_time": "2026-07-25T22:00:00+00:00",
        "duration_minutes": 420,
        "time_in_bed_minutes": 450,
        "non_sleep_minutes": 30,
        "source": "oura",
        "planned_sleep_replacements": 0,
    }
    assert "must-not-leak" not in repr(preview)
    assert fake_backend.created_drafts == []
    with session_factory() as session:
        assert list(session.scalars(select(CalendarEventMirror))) == []


async def test_incomplete_payload_is_explicit_noop(
    session_factory,
    fake_backend,
) -> None:
    # Given
    reader = SleepReader(
        [
            {
                "date": "2026-07-26",
                "source": {"provider": "oura"},
                "duration_minutes": 420,
            }
        ]
    )

    # When
    result = await reconcile_recent_sleep(
        target_date=date(2026, 7, 26),
        calendar_source=CalendarSource.GOOGLE,
        client=reader,
        user_id="redacted-user",
        session_factory=session_factory,
        backend=fake_backend,
        dry_run=True,
    )

    # Then
    assert result == {
        "status": "noop",
        "reason": "incomplete",
        "calendar": "google",
        "local_date": "2026-07-26",
    }


async def test_dry_run_counts_only_owned_planned_sleep(
    session_factory,
) -> None:
    # Given
    with session_factory() as session:
        session.add_all(
            [
                CalendarEventMirror(
                    external_id="planned",
                    calendar_source=CalendarSource.GOOGLE,
                    summary="수면 (계획)",
                    start_at=datetime(2026, 7, 25, 13, tzinfo=UTC),
                    end_at=datetime(2026, 7, 25, 23, tzinfo=UTC),
                    is_agent_created=True,
                    healthmes_kind="planned_sleep",
                ),
                CalendarEventMirror(
                    external_id="routine",
                    calendar_source=CalendarSource.GOOGLE,
                    summary="Routine",
                    start_at=datetime(2026, 7, 25, 13, tzinfo=UTC),
                    end_at=datetime(2026, 7, 25, 23, tzinfo=UTC),
                    is_agent_created=False,
                ),
            ]
        )
        session.commit()

    # When
    backend = ReadOnlyBackend(
        {
            "planned": ExternalEvent(
                external_id="planned",
                summary="수면 (계획)",
                start_at=datetime(2026, 7, 25, 13, tzinfo=UTC),
                end_at=datetime(2026, 7, 25, 23, tzinfo=UTC),
                is_agent_created=True,
                identity=CalendarEventIdentity(
                    kind=HealthmesEventKind.PLANNED_SLEEP,
                    source="planner",
                    source_key="proposal:planned",
                ),
                healthmes_kind=HealthmesEventKind.PLANNED_SLEEP,
            )
        }
    )

    preview = await reconcile_recent_sleep(
        target_date=date(2026, 7, 26),
        calendar_source=CalendarSource.GOOGLE,
        client=SleepReader([_summary()]),
        user_id="redacted-user",
        session_factory=session_factory,
        backend=backend,
        dry_run=True,
    )

    # Then
    assert preview["planned_sleep_replacements"] == 1


async def test_dry_run_blocks_unowned_actual_sleep_mirror(
    session_factory,
) -> None:
    with session_factory() as session:
        session.add(
            CalendarEventMirror(
                external_id="spoofed",
                calendar_source=CalendarSource.GOOGLE,
                summary="수면 (실제)",
                start_at=datetime(2026, 7, 25, 14, tzinfo=UTC),
                end_at=datetime(2026, 7, 25, 22, tzinfo=UTC),
                is_agent_created=False,
                healthmes_source_key="oura:2026-07-26",
                observation_fingerprint="stale",
            )
        )
        session.commit()

    preview = await reconcile_recent_sleep(
        target_date=date(2026, 7, 26),
        calendar_source=CalendarSource.GOOGLE,
        client=SleepReader([_summary(duration=450)]),
        user_id="redacted-user",
        session_factory=session_factory,
        backend=ReadOnlyBackend(),
        dry_run=True,
    )

    assert preview["action"] == "blocked"
    assert preview["reason"] == "ownership_mismatch"
    assert preview["planned_sleep_replacements"] == 0


async def test_runtime_stops_when_remote_actual_sleep_identity_is_blocked(
    session_factory,
    fake_backend,
) -> None:
    # Given
    reader = SleepReader([_summary()])
    created = await reconcile_recent_sleep(
        target_date=date(2026, 7, 26),
        calendar_source=CalendarSource.GOOGLE,
        client=reader,
        user_id="redacted-user",
        session_factory=session_factory,
        backend=fake_backend,
    )
    assert created["action"] == "created"
    actual_id = next(iter(fake_backend.events))
    actual = fake_backend.events[actual_id]
    fake_backend.events[actual_id] = ExternalEvent(
        external_id=actual_id,
        summary="Changed identity",
        start_at=actual.start_at,
        end_at=actual.end_at,
        is_agent_created=True,
        identity=CalendarEventIdentity(
            kind=HealthmesEventKind.PLANNED_SLEEP,
            source="planner",
            source_key="proposal:changed",
        ),
        etag=actual.etag,
    )
    planned = ExternalEvent(
        external_id="planned",
        summary="수면 (계획)",
        start_at=datetime(2026, 7, 25, 13, tzinfo=UTC),
        end_at=datetime(2026, 7, 25, 23, tzinfo=UTC),
        is_agent_created=True,
        identity=CalendarEventIdentity(
            kind=HealthmesEventKind.PLANNED_SLEEP,
            source="planner",
            source_key="proposal:planned",
        ),
        etag='"planned-v1"',
    )
    fake_backend.events[planned.external_id] = planned
    with session_factory() as session:
        session.add(
            CalendarEventMirror(
                external_id=planned.external_id,
                calendar_source=CalendarSource.GOOGLE,
                summary=planned.summary,
                start_at=planned.start_at,
                end_at=planned.end_at,
                is_agent_created=True,
                healthmes_kind=HealthmesEventKind.PLANNED_SLEEP.value,
                healthmes_source="planner",
                healthmes_source_key="proposal:planned",
                etag=planned.etag,
            )
        )
        session.commit()

    # When
    result = await reconcile_recent_sleep(
        target_date=date(2026, 7, 26),
        calendar_source=CalendarSource.GOOGLE,
        client=reader,
        user_id="redacted-user",
        session_factory=session_factory,
        backend=fake_backend,
    )

    # Then
    assert result["status"] == "blocked"
    assert result["reason"] == "ownership_mismatch"
    assert fake_backend.update_calls == []
    assert fake_backend.delete_calls == []
    with session_factory() as session:
        assert session.scalar(
            select(CalendarEventMirror).where(
                CalendarEventMirror.external_id == planned.external_id
            )
        ) is not None


async def test_remote_kind_changed_planned_sleep_blocks_dry_run_and_live(
    session_factory,
) -> None:
    with session_factory() as session:
        session.add(
            CalendarEventMirror(
                external_id="planned",
                calendar_source=CalendarSource.GOOGLE,
                summary="수면 (계획)",
                start_at=datetime(2026, 7, 25, 13, tzinfo=UTC),
                end_at=datetime(2026, 7, 25, 23, tzinfo=UTC),
                is_agent_created=True,
                healthmes_kind="planned_sleep",
                healthmes_source="planner",
                healthmes_source_key="proposal:planned",
            )
        )
        session.commit()
    backend = ReadOnlyBackend(
        {
            "planned": ExternalEvent(
                external_id="planned",
                summary="Changed",
                start_at=datetime(2026, 7, 25, 13, tzinfo=UTC),
                end_at=datetime(2026, 7, 25, 23, tzinfo=UTC),
                is_agent_created=True,
                healthmes_kind=HealthmesEventKind.ACTUAL_SLEEP,
            )
        }
    )

    preview = await reconcile_recent_sleep(
        target_date=date(2026, 7, 26),
        calendar_source=CalendarSource.GOOGLE,
        client=SleepReader([_summary()]),
        user_id="redacted-user",
        session_factory=session_factory,
        backend=backend,
        dry_run=True,
    )

    assert preview["action"] == "blocked"
    assert preview["reason"] == "planned_sleep_ownership_mismatch"
    assert preview["planned_sleep_replacements"] == 0

    live = await reconcile_recent_sleep(
        target_date=date(2026, 7, 26),
        calendar_source=CalendarSource.GOOGLE,
        client=SleepReader([_summary()]),
        user_id="redacted-user",
        session_factory=session_factory,
        backend=backend,
    )

    assert live["status"] == "blocked"
    with session_factory() as session:
        assert session.scalar(
            select(CalendarEventMirror).where(
                CalendarEventMirror.external_id == "planned"
            )
        ) is not None


async def test_stale_planned_sleep_etag_blocks_dry_run_and_live(
    session_factory,
) -> None:
    with session_factory() as session:
        session.add(
            CalendarEventMirror(
                external_id="planned",
                calendar_source=CalendarSource.GOOGLE,
                summary="수면 (계획)",
                start_at=datetime(2026, 7, 25, 13, tzinfo=UTC),
                end_at=datetime(2026, 7, 25, 23, tzinfo=UTC),
                is_agent_created=True,
                healthmes_kind="planned_sleep",
                healthmes_source="planner",
                healthmes_source_key="proposal:planned",
                etag='"mirror-v1"',
            )
        )
        session.commit()
    backend = ReadOnlyBackend(
        {
            "planned": ExternalEvent(
                external_id="planned",
                summary="수면 (계획)",
                start_at=datetime(2026, 7, 25, 13, tzinfo=UTC),
                end_at=datetime(2026, 7, 25, 23, tzinfo=UTC),
                is_agent_created=True,
                healthmes_kind=HealthmesEventKind.PLANNED_SLEEP,
                etag='"remote-v2"',
            )
        }
    )

    preview = await reconcile_recent_sleep(
        target_date=date(2026, 7, 26),
        calendar_source=CalendarSource.GOOGLE,
        client=SleepReader([_summary()]),
        user_id="redacted-user",
        session_factory=session_factory,
        backend=backend,
        dry_run=True,
    )

    assert preview["action"] == "blocked"
    assert preview["reason"] == "planned_sleep_changed"
    assert preview["planned_sleep_replacements"] == 0

    live = await reconcile_recent_sleep(
        target_date=date(2026, 7, 26),
        calendar_source=CalendarSource.GOOGLE,
        client=SleepReader([_summary()]),
        user_id="redacted-user",
        session_factory=session_factory,
        backend=backend,
    )

    assert live["status"] == "blocked"
    with session_factory() as session:
        assert session.scalar(
            select(CalendarEventMirror).where(
                CalendarEventMirror.external_id == "planned"
            )
        ) is not None


async def test_planned_sleep_drift_after_actual_create_surfaces_retryable_cleanup(
    session_factory,
    fake_backend,
    monkeypatch,
) -> None:
    # Given
    planned = ExternalEvent(
        external_id="planned-race",
        summary="수면 (계획)",
        start_at=datetime(2026, 7, 25, 13, tzinfo=UTC),
        end_at=datetime(2026, 7, 26, 1, tzinfo=UTC),
        is_agent_created=True,
        identity=CalendarEventIdentity(
            kind=HealthmesEventKind.PLANNED_SLEEP,
            source="planner",
            source_key="proposal:race",
        ),
        healthmes_kind=HealthmesEventKind.PLANNED_SLEEP,
        etag='"planned-v1"',
    )
    fake_backend.events[planned.external_id] = planned
    with session_factory() as session:
        session.add(
            CalendarEventMirror(
                external_id=planned.external_id,
                calendar_source=CalendarSource.GOOGLE,
                summary=planned.summary,
                start_at=planned.start_at,
                end_at=planned.end_at,
                is_agent_created=True,
                healthmes_kind=HealthmesEventKind.PLANNED_SLEEP.value,
                healthmes_source="planner",
                healthmes_source_key="proposal:race",
                etag=planned.etag,
            )
        )
        session.commit()

    def changed_before_delete(*_args, **_kwargs) -> None:
        raise CalendarConflictError("planned sleep changed after preview")

    monkeypatch.setattr(fake_backend, "delete_event", changed_before_delete)

    # When
    result = await reconcile_recent_sleep(
        target_date=date(2026, 7, 26),
        calendar_source=CalendarSource.GOOGLE,
        client=SleepReader([_summary()]),
        user_id="redacted-user",
        session_factory=session_factory,
        backend=fake_backend,
    )

    # Then
    assert result["status"] == "cleanup_pending"
    assert result["action"] == "created"
    assert result["planned_sleep_cleanup_pending"] == 1
    assert len(fake_backend.created_drafts) == 1
    assert planned.external_id in fake_backend.events
    with session_factory() as session:
        rows = list(session.scalars(select(CalendarEventMirror)))
    assert {row.healthmes_kind for row in rows} == {
        HealthmesEventKind.ACTUAL_SLEEP.value,
        HealthmesEventKind.PLANNED_SLEEP.value,
    }


def test_runtime_job_requires_a_configured_calendar(settings) -> None:
    assert build_sleep_reconciliation_job(settings) is None


def test_runtime_job_only_proposes_the_recent_local_date_window(
    settings,
    session_factory,
    fake_backend,
) -> None:
    # Given
    reader = SleepReader([_summary()])
    enabled = settings.model_copy(
        update={
            "google_calendar_enabled": True,
            "ow_user_id": "redacted-user",
            "timezone": "Asia/Seoul",
        }
    )
    job = build_sleep_reconciliation_job(
        enabled,
        client=reader,
        backend_factory=lambda: fake_backend,
        session_factory=session_factory,
        date_provider=lambda _timezone: date(2026, 7, 26),
    )
    assert job is not None

    # When
    result = job()

    # Then
    assert result is not None
    assert result["status"] == "ok"
    assert result["window_start"] == "2026-07-24"
    assert result["window_end"] == "2026-07-26"
    assert [entry["proposal_status"] for entry in result["results"]] == [
        "noop",
        "noop",
        "pending",
    ]
    assert result["results"][-1]["action"] == "would_create"
    assert fake_backend.created_drafts == []
    assert fake_backend.update_calls == []
    assert fake_backend.delete_calls == []
    assert reader.requests == [
        ("redacted-user", "2026-07-24", "2026-07-25"),
        ("redacted-user", "2026-07-25", "2026-07-26"),
        ("redacted-user", "2026-07-26", "2026-07-27"),
    ]


def test_runtime_job_redacts_provider_user_id_from_failures(
    settings,
    session_factory,
    fake_backend,
    caplog,
) -> None:
    enabled = settings.model_copy(
        update={
            "google_calendar_enabled": True,
            "ow_user_id": "provider-user-secret",
        }
    )
    job = build_sleep_reconciliation_job(
        enabled,
        client=FailingSleepReader(),
        backend_factory=lambda: fake_backend,
        session_factory=session_factory,
        date_provider=lambda _timezone: date(2026, 7, 26),
    )
    assert job is not None

    with caplog.at_level(logging.ERROR):
        assert job() is None

    assert "provider-user-secret" not in caplog.text
    assert "wearables.invalid" not in caplog.text
    assert "HTTPStatusError" in caplog.text


def test_runtime_job_proposes_a_prior_day_provider_correction_without_applying(
    settings,
    session_factory,
    fake_backend,
) -> None:
    previous = {
        "date": "2026-07-25",
        "source": {"provider": "oura"},
        "start_time": "2026-07-24T23:00:00+09:00",
        "end_time": "2026-07-25T07:00:00+09:00",
        "duration_minutes": 420,
        "time_in_bed_minutes": 450,
    }
    reader = SleepReader([previous, _summary()])
    enabled = settings.model_copy(
        update={
            "google_calendar_enabled": True,
            "ow_user_id": "redacted-user",
            "timezone": "Asia/Seoul",
        }
    )
    job = build_sleep_reconciliation_job(
        enabled,
        client=reader,
        backend_factory=lambda: fake_backend,
        session_factory=session_factory,
        date_provider=lambda _timezone: date(2026, 7, 26),
    )
    assert job is not None
    job()
    reader.rows[0] = {
        **previous,
        "end_time": "2026-07-25T07:30:00+09:00",
        "duration_minutes": 450,
        "time_in_bed_minutes": 480,
    }

    result = job()

    assert result is not None
    assert [entry["action"] for entry in result["results"][1:]] == [
        "would_create",
        "would_create",
    ]
    assert fake_backend.created_drafts == []
    assert fake_backend.update_calls == []
    assert fake_backend.delete_calls == []
