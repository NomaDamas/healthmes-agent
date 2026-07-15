"""Calendar poll-job tests: enablement wiring, sync runs, proposal push.

These pin the production entry point of the calendar plane (docs/PLAN.md §6):
the settings flags actually build jobs, each run syncs the mirror, and the
write backend advances accepted proposals to ``pushed`` by writing tagged
agent blocks — the contract promised by healthmes/api/schedule.py.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from healthmes.calendars.base import EventDraft
from healthmes.calendars.jobs import (
    build_calendar_job,
    build_calendar_jobs,
    calendar_job_id,
    enabled_sources,
    push_accepted_proposals,
    write_source,
)
from healthmes.calendars.state import InMemoryPendingDiffStore, InMemorySyncStateStore
from healthmes.calendars.sync import CalendarMirrorService
from healthmes.store import (
    CalendarEventMirror,
    CalendarSource,
    ProposalStatus,
    ScheduleProposal,
    Task,
)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


class TestEnablement:
    def test_disabled_flags_build_no_jobs(self, settings) -> None:
        assert build_calendar_jobs(settings) == []
        assert enabled_sources(settings) == ()
        assert write_source(settings) is None

    def test_google_enabled_builds_google_job(self, settings) -> None:
        enabled = settings.model_copy(update={"google_calendar_enabled": True})
        specs = build_calendar_jobs(enabled)
        assert [spec.source for spec in specs] == [CalendarSource.GOOGLE]
        assert specs[0].job_id == calendar_job_id(CalendarSource.GOOGLE)
        assert specs[0].interval_minutes == enabled.google_poll_minutes
        assert write_source(enabled) is CalendarSource.GOOGLE

    def test_both_enabled_builds_both_with_google_as_writer(self, settings) -> None:
        enabled = settings.model_copy(
            update={"google_calendar_enabled": True, "caldav_enabled": True}
        )
        specs = build_calendar_jobs(enabled)
        assert [spec.source for spec in specs] == [
            CalendarSource.GOOGLE,
            CalendarSource.CALDAV,
        ]
        assert [spec.interval_minutes for spec in specs] == [
            enabled.google_poll_minutes,
            enabled.caldav_poll_minutes,
        ]
        assert write_source(enabled) is CalendarSource.GOOGLE

    def test_caldav_only_is_the_writer(self, settings) -> None:
        enabled = settings.model_copy(update={"caldav_enabled": True})
        assert write_source(enabled) is CalendarSource.CALDAV
        (spec,) = build_calendar_jobs(enabled)
        assert spec.interval_minutes == enabled.caldav_poll_minutes


class TestJobRun:
    def test_job_syncs_backend_into_mirror(
        self, settings, session_factory, session, fake_backend, make_event
    ) -> None:
        fake_backend.queue_changes([make_event("meet-1")], {"sync_token": "tok-1"})
        job = build_calendar_job(
            settings,
            fake_backend.source,
            is_write_backend=False,
            backend_factory=lambda: fake_backend,
            session_factory=session_factory,
            state_store=InMemorySyncStateStore(),
        )

        job()

        rows = session.scalars(select(CalendarEventMirror)).all()
        assert [row.external_id for row in rows] == ["meet-1"]
        assert fake_backend.received_sync_states == [None]

    def test_job_returns_deletion_diff(
        self, settings, session_factory, session, fake_backend, make_event
    ) -> None:
        # F4: the poll job must RETURN the SyncDiff so the schedule_changed
        # trigger can consume deletions (which vanish from the mirror and so
        # cannot be re-derived from row updated_at).
        job = build_calendar_job(
            settings,
            fake_backend.source,
            is_write_backend=False,
            backend_factory=lambda: fake_backend,
            session_factory=session_factory,
            state_store=InMemorySyncStateStore(),
            pending_store=InMemoryPendingDiffStore(),
        )
        fake_backend.queue_changes([make_event("meet-1")], {"sync_token": "tok-1"})
        fake_backend.queue_changes(
            [make_event("meet-1", deleted=True, summary=None, etag=None)],
            {"sync_token": "tok-2"},
        )

        bootstrap_diff = job()  # silent adoption, but still a SyncDiff
        assert bootstrap_diff is not None and not bootstrap_diff.has_changes

        deletion_diff = job()
        assert deletion_diff is not None
        assert [change.external_id for change in deletion_diff.deleted] == ["meet-1"]

    def test_write_backend_pushes_accepted_proposals(
        self, settings, session_factory, session, fake_backend
    ) -> None:
        task = Task(title="Write weekly report")
        session.add(task)
        session.flush()
        session.add_all(
            [
                ScheduleProposal(
                    task_id=task.id,
                    proposed_start=utc(2026, 7, 10, 9, 0),
                    proposed_end=utc(2026, 7, 10, 11, 0),
                    status=ProposalStatus.ACCEPTED,
                ),
                ScheduleProposal(
                    task_id=task.id,
                    proposed_start=utc(2026, 7, 11, 9, 0),
                    proposed_end=utc(2026, 7, 11, 10, 0),
                    status=ProposalStatus.PROPOSED,  # not confirmed: never pushed
                ),
            ]
        )
        session.commit()

        job = build_calendar_job(
            settings,
            fake_backend.source,
            is_write_backend=True,
            backend_factory=lambda: fake_backend,
            session_factory=session_factory,
            state_store=InMemorySyncStateStore(),
        )
        job()

        # The accepted proposal became a tagged agent block + a mirror row...
        assert [draft.summary for draft in fake_backend.created_drafts] == [
            "Write weekly report"
        ]
        assert fake_backend.created_drafts[0].agent_task_id == task.id
        session.expire_all()
        mirror = session.scalars(select(CalendarEventMirror)).one()
        assert mirror.is_agent_created
        assert mirror.agent_task_id == task.id
        # ...and advanced to pushed; the unconfirmed one is untouched.
        statuses = {
            proposal.status
            for proposal in session.scalars(select(ScheduleProposal)).all()
        }
        assert statuses == {ProposalStatus.PUSHED, ProposalStatus.PROPOSED}

    def test_read_backend_never_pushes(
        self, settings, session_factory, session, fake_backend
    ) -> None:
        task = Task(title="Read-only backend task")
        session.add(task)
        session.flush()
        session.add(
            ScheduleProposal(
                task_id=task.id,
                proposed_start=utc(2026, 7, 10, 9, 0),
                proposed_end=utc(2026, 7, 10, 10, 0),
                status=ProposalStatus.ACCEPTED,
            )
        )
        session.commit()

        job = build_calendar_job(
            settings,
            fake_backend.source,
            is_write_backend=False,
            backend_factory=lambda: fake_backend,
            session_factory=session_factory,
            state_store=InMemorySyncStateStore(),
        )
        job()

        assert fake_backend.created_drafts == []
        session.expire_all()
        proposal = session.scalars(select(ScheduleProposal)).one()
        assert proposal.status is ProposalStatus.ACCEPTED

    def test_job_contains_backend_failures(self, settings, session_factory) -> None:
        def exploding_factory():
            raise RuntimeError("credentials missing")

        job = build_calendar_job(
            settings,
            CalendarSource.GOOGLE,
            is_write_backend=True,
            backend_factory=exploding_factory,
            session_factory=session_factory,
            state_store=InMemorySyncStateStore(),
        )
        job()  # must not raise — next interval retries

    def test_failed_push_leaves_proposal_accepted_for_retry(
        self, settings, session_factory, session, fake_backend, monkeypatch
    ) -> None:
        task = Task(title="Flaky push")
        session.add(task)
        session.flush()
        session.add(
            ScheduleProposal(
                task_id=task.id,
                proposed_start=utc(2026, 7, 10, 9, 0),
                proposed_end=utc(2026, 7, 10, 10, 0),
                status=ProposalStatus.ACCEPTED,
            )
        )
        session.commit()

        def failing_create(draft):
            raise RuntimeError("backend down")

        monkeypatch.setattr(fake_backend, "create_event", failing_create)
        service = CalendarMirrorService(session, [fake_backend], InMemorySyncStateStore())

        pushed = push_accepted_proposals(service, session, fake_backend.source)

        assert pushed == 0
        session.expire_all()
        proposal = session.scalars(select(ScheduleProposal)).one()
        assert proposal.status is ProposalStatus.ACCEPTED

    def test_crash_after_remote_create_does_not_duplicate_event(
        self, session, fake_backend
    ) -> None:
        # F8: a prior poll created the remote block + mirror row but crashed
        # before flipping the proposal to pushed. The retry must reuse the
        # existing block, not create a second remote event.
        task = Task(title="Write report")
        session.add(task)
        session.flush()
        session.add(
            ScheduleProposal(
                task_id=task.id,
                proposed_start=utc(2026, 7, 10, 9, 0),
                proposed_end=utc(2026, 7, 10, 11, 0),
                status=ProposalStatus.ACCEPTED,
            )
        )
        session.commit()

        service = CalendarMirrorService(session, [fake_backend], InMemorySyncStateStore())
        # Simulate the interrupted prior poll: the block already exists remotely
        # and in the mirror, but the proposal is still ACCEPTED.
        service.create_agent_event(
            fake_backend.source,
            EventDraft(
                summary="Write report",
                start_at=utc(2026, 7, 10, 9, 0),
                end_at=utc(2026, 7, 10, 11, 0),
                agent_task_id=task.id,
            ),
        )
        assert len(fake_backend.created_drafts) == 1

        pushed = push_accepted_proposals(service, session, fake_backend.source)

        assert pushed == 1
        assert len(fake_backend.created_drafts) == 1  # NO second remote create
        session.expire_all()
        assert len(session.scalars(select(CalendarEventMirror)).all()) == 1
        assert session.scalars(select(ScheduleProposal)).one().status is (
            ProposalStatus.PUSHED
        )


@pytest.mark.parametrize("source", [CalendarSource.GOOGLE, CalendarSource.CALDAV])
def test_job_ids_are_per_source(source) -> None:
    assert calendar_job_id(source) == f"healthmes-calendar-{source.value}"
