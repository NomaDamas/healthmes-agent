"""Calendar poll-job tests: enablement wiring, sync runs, proposal push.

These pin the production entry point of the calendar plane (docs/PLAN.md §6):
the settings flags actually build jobs, each run syncs the mirror, and the
write backend advances accepted proposals to ``pushed`` by writing tagged
agent blocks — the contract promised by healthmes/api/schedule.py.
"""

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from healthmes.calendars.base import (
    CalendarConflictError,
    CalendarEventIdentity,
    EventDraft,
    EventNotFoundError,
    HealthmesEventKind,
)
from healthmes.calendars.intake import intake_calendar_tasks, intake_revision
from healthmes.calendars.jobs import (
    _proposal_identity,
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
    TaskSource,
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

    def test_both_enabled_builds_both_with_icloud_as_writer(self, settings) -> None:
        enabled = settings.model_copy(
            update={"google_calendar_enabled": True, "caldav_enabled": True}
        )
        specs = build_calendar_jobs(enabled)
        assert [spec.source for spec in specs] == [
            CalendarSource.CALDAV,
            CalendarSource.GOOGLE,
        ]
        assert [spec.interval_minutes for spec in specs] == [
            enabled.caldav_poll_minutes,
            enabled.google_poll_minutes,
        ]
        assert write_source(enabled) is CalendarSource.CALDAV

    def test_caldav_only_is_the_writer(self, settings) -> None:
        enabled = settings.model_copy(update={"caldav_enabled": True})
        assert write_source(enabled) is CalendarSource.CALDAV
        (spec,) = build_calendar_jobs(enabled)
        assert spec.interval_minutes == enabled.caldav_poll_minutes


class TestJobRun:
    def test_intake_opt_out_is_sticky_until_hm_is_readded(self, session) -> None:
        mirror = CalendarEventMirror(
            external_id="hm-opt-out",
            calendar_source=CalendarSource.GOOGLE,
            summary="[HM] Write launch notes",
            start_at=utc(2026, 8, 4, 9),
            end_at=utc(2026, 8, 4, 10),
            organizer_self=True,
            event_type="default",
            etag="etag-1",
        )
        session.add(mirror)
        session.commit()

        [created] = intake_calendar_tasks(
            session,
            CalendarSource.GOOGLE,
            UTC,
        )
        session.commit()
        original_task_id = created.id
        assert created.source is TaskSource.USER

        mirror.summary = "Write launch notes"
        mirror.etag = "etag-2"
        [cancelled] = intake_calendar_tasks(
            session,
            CalendarSource.GOOGLE,
            UTC,
        )
        session.commit()
        assert cancelled.id == original_task_id
        assert cancelled.status == "cancelled"
        assert mirror.intake_task_id is None
        assert mirror.intake_opted_out is True

        assert intake_calendar_tasks(
            session,
            CalendarSource.GOOGLE,
            UTC,
        ) == ()
        assert len(session.scalars(select(Task)).all()) == 1

        mirror.summary = "[HM] Write revised launch notes"
        mirror.etag = "etag-3"
        [readded] = intake_calendar_tasks(
            session,
            CalendarSource.GOOGLE,
            UTC,
        )
        session.commit()
        assert readded.id != original_task_id
        assert readded.title == "Write revised launch notes"
        assert mirror.intake_task_id == readded.id
        assert mirror.intake_opted_out is False

    def test_timed_intake_event_is_adopted_without_provider_create(
        self,
        session,
        fake_backend,
    ) -> None:
        task = Task(title="Write launch notes", source=TaskSource.USER)
        session.add(task)
        session.flush()
        mirror = CalendarEventMirror(
            external_id="hm-timed",
            calendar_source=CalendarSource.GOOGLE,
            summary="[HM] Write launch notes",
            start_at=utc(2026, 8, 4, 9),
            end_at=utc(2026, 8, 4, 10),
            organizer_self=True,
            event_type="default",
            etag="etag-1",
            intake_task_id=task.id,
        )
        session.add(mirror)
        session.commit()
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=mirror.start_at,
            proposed_end=mirror.end_at,
            status=ProposalStatus.ACCEPTED,
            intake_calendar_source=mirror.calendar_source,
            intake_external_id=mirror.external_id,
            intake_revision=intake_revision(mirror),
        )
        session.add(proposal)
        session.commit()
        service = CalendarMirrorService(
            session,
            [fake_backend],
            InMemorySyncStateStore(),
        )

        assert push_accepted_proposals(
            service,
            session,
            fake_backend.source,
        ) == 1
        assert fake_backend.created_drafts == []
        assert session.get(ScheduleProposal, proposal.id).status is (
            ProposalStatus.PUSHED
        )
        assert session.get(Task, task.id).status == "scheduled"
        assert mirror.is_agent_created is False

    def test_changed_timed_intake_invalidates_instead_of_creating_duplicate(
        self,
        session,
        fake_backend,
    ) -> None:
        task = Task(title="Write launch notes", source=TaskSource.USER)
        session.add(task)
        session.flush()
        mirror = CalendarEventMirror(
            external_id="hm-changed",
            calendar_source=CalendarSource.GOOGLE,
            summary="[HM] Write launch notes",
            start_at=utc(2026, 8, 4, 9),
            end_at=utc(2026, 8, 4, 10),
            organizer_self=True,
            event_type="default",
            etag="etag-1",
            intake_task_id=task.id,
        )
        session.add(mirror)
        session.commit()
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=mirror.start_at,
            proposed_end=mirror.end_at,
            status=ProposalStatus.ACCEPTED,
            intake_calendar_source=mirror.calendar_source,
            intake_external_id=mirror.external_id,
            intake_revision=intake_revision(mirror),
        )
        session.add(proposal)
        session.commit()

        mirror.end_at = mirror.end_at + timedelta(minutes=30)
        mirror.etag = "etag-2"
        session.commit()
        service = CalendarMirrorService(
            session,
            [fake_backend],
            InMemorySyncStateStore(),
        )

        assert push_accepted_proposals(
            service,
            session,
            fake_backend.source,
        ) == 0
        assert fake_backend.created_drafts == []
        assert session.get(ScheduleProposal, proposal.id).status is (
            ProposalStatus.INVALIDATED
        )

    def test_all_day_intake_still_creates_provider_agnostic_output_block(
        self,
        session,
        fake_backend_factory,
    ) -> None:
        task = Task(title="Prepare launch", source=TaskSource.USER)
        session.add(task)
        session.flush()
        session.add(
            CalendarEventMirror(
                external_id="hm-all-day",
                calendar_source=CalendarSource.GOOGLE,
                summary="[HM] Prepare launch",
                start_at=utc(2026, 8, 4),
                end_at=utc(2026, 8, 5),
                organizer_self=True,
                event_type="default",
                is_all_day=True,
                etag="etag-1",
                intake_task_id=task.id,
            )
        )
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=utc(2026, 8, 4, 9),
            proposed_end=utc(2026, 8, 4, 10),
            status=ProposalStatus.ACCEPTED,
        )
        session.add(proposal)
        session.commit()
        caldav_backend = fake_backend_factory(CalendarSource.CALDAV)
        service = CalendarMirrorService(
            session,
            [caldav_backend],
            InMemorySyncStateStore(),
        )

        assert push_accepted_proposals(
            service,
            session,
            CalendarSource.CALDAV,
        ) == 1
        assert [draft.summary for draft in caldav_backend.created_drafts] == [
            "Prepare launch"
        ]
        assert session.get(ScheduleProposal, proposal.id).status is (
            ProposalStatus.PUSHED
        )

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

    def test_job_commits_intake_before_proposal_push(
        self,
        settings,
        session_factory,
        fake_backend,
        make_event,
    ) -> None:
        fake_backend.queue_changes(
            [
                make_event(
                    "hm-intake",
                    summary="[HM] Persist imported task",
                    organizer_self=True,
                    event_type="default",
                )
            ],
            {"sync_token": "tok-1"},
        )
        job = build_calendar_job(
            settings,
            fake_backend.source,
            is_write_backend=True,
            backend_factory=lambda: fake_backend,
            session_factory=session_factory,
            state_store=InMemorySyncStateStore(),
        )

        assert job() is not None

        with session_factory() as persisted:
            task = persisted.scalars(select(Task)).one()
            mirror = persisted.scalars(select(CalendarEventMirror)).one()
            assert task.title == "Persist imported task"
            assert mirror.intake_task_id == task.id

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

    def test_confirmed_planned_sleep_is_written_with_reconciliation_identity(
        self, session, fake_backend
    ) -> None:
        task = Task(title="Night rest")
        session.add(task)
        session.flush()
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=utc(2026, 7, 10, 23, 0),
            proposed_end=utc(2026, 7, 11, 7, 0),
            status=ProposalStatus.ACCEPTED,
            healthmes_kind=HealthmesEventKind.PLANNED_SLEEP.value,
        )
        session.add(proposal)
        session.commit()
        service = CalendarMirrorService(
            session, [fake_backend], InMemorySyncStateStore()
        )

        assert push_accepted_proposals(service, session, fake_backend.source) == 1

        [draft] = fake_backend.created_drafts
        assert draft.identity is not None
        assert draft.identity.kind is HealthmesEventKind.PLANNED_SLEEP
        assert draft.identity.source == "planner"
        assert draft.identity.source_key == f"proposal:{proposal.id}"
        mirror = session.scalars(select(CalendarEventMirror)).one()
        assert mirror.healthmes_kind == HealthmesEventKind.PLANNED_SLEEP.value

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

    def test_sleep_change_invalidates_accepted_proposal_before_push(
        self,
        session,
        fake_backend,
    ) -> None:
        task = Task(title="Too early after corrected sleep")
        session.add(task)
        session.flush()
        session.add_all(
            [
                ScheduleProposal(
                    task_id=task.id,
                    proposed_start=utc(2026, 7, 10, 6),
                    proposed_end=utc(2026, 7, 10, 7),
                    status=ProposalStatus.ACCEPTED,
                ),
                CalendarEventMirror(
                    external_id="actual-sleep",
                    calendar_source=CalendarSource.GOOGLE,
                    summary="수면 (실제)",
                    start_at=utc(2026, 7, 9, 23),
                    end_at=utc(2026, 7, 10, 7, 30),
                    is_agent_created=True,
                    healthmes_kind=HealthmesEventKind.ACTUAL_SLEEP.value,
                    sleep_local_date=utc(2026, 7, 10).date(),
                ),
            ]
        )
        session.commit()
        service = CalendarMirrorService(
            session,
            [fake_backend],
            InMemorySyncStateStore(),
        )

        pushed = push_accepted_proposals(
            service,
            session,
            fake_backend.source,
        )

        assert pushed == 0
        assert fake_backend.created_drafts == []
        proposal = session.scalars(select(ScheduleProposal)).one()
        assert proposal.status is ProposalStatus.INVALIDATED

    def test_invalidation_never_deletes_another_proposals_pushed_block(
        self,
        session,
        fake_backend,
    ) -> None:
        task = Task(title="Shared task")
        session.add(task)
        session.flush()
        start = utc(2026, 7, 10, 6)
        end = utc(2026, 7, 10, 7)
        pushed_proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=start,
            proposed_end=end,
            status=ProposalStatus.PUSHED,
        )
        accepted_proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=start,
            proposed_end=end,
            status=ProposalStatus.ACCEPTED,
        )
        session.add_all([pushed_proposal, accepted_proposal])
        session.commit()
        service = CalendarMirrorService(
            session,
            [fake_backend],
            InMemorySyncStateStore(),
        )
        pushed_row = service.create_agent_event(
            fake_backend.source,
            EventDraft(
                summary=task.title,
                start_at=start,
                end_at=end,
                agent_task_id=task.id,
                identity=_proposal_identity(pushed_proposal),
            ),
        )
        session.add(
            CalendarEventMirror(
                external_id="actual-sleep",
                calendar_source=CalendarSource.GOOGLE,
                summary="수면 (실제)",
                start_at=utc(2026, 7, 9, 23),
                end_at=utc(2026, 7, 10, 7, 30),
                is_agent_created=True,
                healthmes_kind=HealthmesEventKind.ACTUAL_SLEEP.value,
                sleep_local_date=utc(2026, 7, 10).date(),
            )
        )
        session.commit()

        assert push_accepted_proposals(
            service,
            session,
            fake_backend.source,
        ) == 0

        assert session.get(ScheduleProposal, accepted_proposal.id).status is (
            ProposalStatus.INVALIDATED
        )
        assert session.get(ScheduleProposal, pushed_proposal.id).status is (
            ProposalStatus.PUSHED
        )
        assert fake_backend.delete_calls == []
        assert pushed_row.external_id in fake_backend.events

    def test_post_create_sleep_recheck_conditionally_rolls_back_exact_block(
        self,
        session,
        fake_backend,
        monkeypatch,
    ) -> None:
        task = Task(title="Concurrent wake correction")
        session.add(task)
        session.flush()
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=utc(2026, 7, 10, 8),
            proposed_end=utc(2026, 7, 10, 9),
            status=ProposalStatus.ACCEPTED,
        )
        session.add(proposal)
        session.commit()
        service = CalendarMirrorService(
            session,
            [fake_backend],
            InMemorySyncStateStore(),
        )
        checks = iter((None, "concurrent actual-sleep overlap"))
        monkeypatch.setattr(
            "healthmes.calendars.jobs.actual_sleep_violation",
            lambda *_args, **_kwargs: next(checks),
        )

        assert push_accepted_proposals(
            service,
            session,
            fake_backend.source,
        ) == 0

        assert session.get(ScheduleProposal, proposal.id).status is (
            ProposalStatus.INVALIDATED
        )
        assert len(fake_backend.created_drafts) == 1
        assert len(fake_backend.delete_calls) == 1
        assert fake_backend.delete_expected_kinds == [
            HealthmesEventKind.SCHEDULE_BLOCK
        ]
        assert session.scalars(select(CalendarEventMirror)).all() == []

    def test_push_waits_for_write_lock_without_open_session_transaction(
        self,
        session,
        fake_backend,
        monkeypatch,
    ) -> None:
        task = Task(title="Connection-safe push")
        session.add(task)
        session.flush()
        session.add(
            ScheduleProposal(
                task_id=task.id,
                proposed_start=utc(2026, 7, 10, 9),
                proposed_end=utc(2026, 7, 10, 10),
                status=ProposalStatus.ACCEPTED,
            )
        )
        session.commit()
        lock_entries: list[bool] = []

        @contextmanager
        def assert_connection_free(waiting_session, _source):
            lock_entries.append(waiting_session.in_transaction())
            yield

        monkeypatch.setattr(
            "healthmes.calendars.jobs.calendar_write_lock",
            assert_connection_free,
        )
        service = CalendarMirrorService(
            session,
            [fake_backend],
            InMemorySyncStateStore(),
        )

        assert push_accepted_proposals(
            service,
            session,
            fake_backend.source,
        ) == 1
        assert lock_entries == [False]

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
                identity=_proposal_identity(
                    session.scalars(select(ScheduleProposal)).one()
                ),
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

    def test_crash_recovery_refuses_remote_block_with_changed_identity(
        self,
        session,
        fake_backend,
    ) -> None:
        task = Task(title="Protected block")
        session.add(task)
        session.flush()
        proposed_start = utc(2026, 7, 10, 9)
        proposed_end = utc(2026, 7, 10, 10)
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=proposed_start,
            proposed_end=proposed_end,
            status=ProposalStatus.ACCEPTED,
        )
        session.add(proposal)
        session.commit()
        service = CalendarMirrorService(
            session,
            [fake_backend],
            InMemorySyncStateStore(),
        )
        created = service.create_agent_event(
            fake_backend.source,
            EventDraft(
                summary=task.title,
                start_at=proposed_start,
                end_at=proposed_end,
                agent_task_id=task.id,
                identity=_proposal_identity(proposal),
            ),
        )
        fake_backend.events[created.external_id] = replace(
            fake_backend.events[created.external_id],
            identity=CalendarEventIdentity(
                kind=HealthmesEventKind.SCHEDULE_BLOCK,
                source="planner",
                source_key="proposal:another",
            ),
        )

        assert push_accepted_proposals(
            service,
            session,
            fake_backend.source,
        ) == 0

        assert session.get(ScheduleProposal, proposal.id).status is (
            ProposalStatus.ACCEPTED
        )
        assert len(fake_backend.created_drafts) == 1

    def test_sleep_change_after_remote_create_crash_removes_block_and_invalidates(
        self,
        session,
        fake_backend,
    ) -> None:
        task = Task(title="Block invalidated after corrected sleep")
        session.add(task)
        session.flush()
        proposed_start = utc(2026, 7, 10, 6)
        proposed_end = utc(2026, 7, 10, 7)
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=proposed_start,
            proposed_end=proposed_end,
            status=ProposalStatus.ACCEPTED,
        )
        session.add(proposal)
        session.commit()
        service = CalendarMirrorService(
            session,
            [fake_backend],
            InMemorySyncStateStore(),
        )
        created = service.create_agent_event(
            fake_backend.source,
            EventDraft(
                summary=task.title,
                start_at=proposed_start,
                end_at=proposed_end,
                agent_task_id=task.id,
                identity=_proposal_identity(proposal),
            ),
        )
        session.add(
            CalendarEventMirror(
                external_id="actual-sleep",
                calendar_source=CalendarSource.GOOGLE,
                summary="수면 (실제)",
                start_at=utc(2026, 7, 9, 23),
                end_at=utc(2026, 7, 10, 7, 30),
                is_agent_created=True,
                healthmes_kind=HealthmesEventKind.ACTUAL_SLEEP.value,
                sleep_local_date=utc(2026, 7, 10).date(),
            )
        )
        session.commit()

        pushed = push_accepted_proposals(service, session, fake_backend.source)

        assert pushed == 0
        assert fake_backend.delete_calls == [created.external_id]
        assert created.external_id not in fake_backend.events
        assert session.get(ScheduleProposal, proposal.id).status is (
            ProposalStatus.INVALIDATED
        )
        assert {
            row.external_id
            for row in session.scalars(select(CalendarEventMirror)).all()
        } == {"actual-sleep"}

    def test_failed_crash_recovery_delete_leaves_proposal_retryable(
        self,
        session,
        fake_backend,
        monkeypatch,
    ) -> None:
        task = Task(title="Retry invalidated block cleanup")
        session.add(task)
        session.flush()
        proposed_start = utc(2026, 7, 10, 6)
        proposed_end = utc(2026, 7, 10, 7)
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=proposed_start,
            proposed_end=proposed_end,
            status=ProposalStatus.ACCEPTED,
        )
        session.add(proposal)
        session.commit()
        service = CalendarMirrorService(
            session,
            [fake_backend],
            InMemorySyncStateStore(),
        )
        created = service.create_agent_event(
            fake_backend.source,
            EventDraft(
                summary=task.title,
                start_at=proposed_start,
                end_at=proposed_end,
                agent_task_id=task.id,
                identity=_proposal_identity(proposal),
            ),
        )
        session.add(
            CalendarEventMirror(
                external_id="actual-sleep",
                calendar_source=CalendarSource.GOOGLE,
                summary="수면 (실제)",
                start_at=utc(2026, 7, 9, 23),
                end_at=utc(2026, 7, 10, 7, 30),
                is_agent_created=True,
                healthmes_kind=HealthmesEventKind.ACTUAL_SLEEP.value,
                sleep_local_date=utc(2026, 7, 10).date(),
            )
        )
        session.commit()

        def conflict_delete(*args, **kwargs) -> None:
            raise CalendarConflictError("remote changed")

        monkeypatch.setattr(fake_backend, "delete_event", conflict_delete)

        assert push_accepted_proposals(service, session, fake_backend.source) == 0
        assert session.get(ScheduleProposal, proposal.id).status is (
            ProposalStatus.ACCEPTED
        )
        assert session.scalar(
            select(CalendarEventMirror).where(
                CalendarEventMirror.external_id == created.external_id
            )
        ) is not None

    def test_remote_delete_local_commit_crash_recovers_and_invalidates(
        self,
        session_factory,
        fake_backend,
        monkeypatch,
    ) -> None:
        with session_factory() as setup_session:
            task = Task(title="Recover invalidated block cleanup")
            setup_session.add(task)
            setup_session.flush()
            proposed_start = utc(2026, 7, 10, 6)
            proposed_end = utc(2026, 7, 10, 7)
            proposal = ScheduleProposal(
                task_id=task.id,
                proposed_start=proposed_start,
                proposed_end=proposed_end,
                status=ProposalStatus.ACCEPTED,
            )
            setup_session.add(proposal)
            setup_session.commit()
            service = CalendarMirrorService(
                setup_session,
                [fake_backend],
                InMemorySyncStateStore(),
            )
            created = service.create_agent_event(
                fake_backend.source,
                EventDraft(
                    summary=task.title,
                    start_at=proposed_start,
                    end_at=proposed_end,
                    agent_task_id=task.id,
                    identity=_proposal_identity(proposal),
                ),
            )
            setup_session.add(
                CalendarEventMirror(
                    external_id="actual-sleep",
                    calendar_source=CalendarSource.GOOGLE,
                    summary="수면 (실제)",
                    start_at=utc(2026, 7, 9, 23),
                    end_at=utc(2026, 7, 10, 7, 30),
                    is_agent_created=True,
                    healthmes_kind=HealthmesEventKind.ACTUAL_SLEEP.value,
                    sleep_local_date=utc(2026, 7, 10).date(),
                )
            )
            setup_session.commit()
            proposal_id = proposal.id
            created_external_id = created.external_id

        original_delete = fake_backend.delete_event

        def strict_delete(
            external_id,
            *,
            expected_kind=None,
            expected_etag=None,
        ) -> None:
            if external_id not in fake_backend.events:
                fake_backend.delete_calls.append(external_id)
                raise EventNotFoundError(external_id)
            original_delete(
                external_id,
                expected_kind=expected_kind,
                expected_etag=expected_etag,
            )

        monkeypatch.setattr(fake_backend, "delete_event", strict_delete)
        with session_factory() as failing_session:
            service = CalendarMirrorService(
                failing_session,
                [fake_backend],
                InMemorySyncStateStore(),
            )
            real_commit = failing_session.commit

            def fail_after_remote_delete() -> None:
                if created_external_id not in fake_backend.events:
                    raise RuntimeError("simulated local delete commit failure")
                real_commit()

            monkeypatch.setattr(failing_session, "commit", fail_after_remote_delete)
            assert (
                push_accepted_proposals(
                    service,
                    failing_session,
                    fake_backend.source,
                )
                == 0
            )
            failing_session.rollback()

        with session_factory() as retry_session:
            service = CalendarMirrorService(
                retry_session,
                [fake_backend],
                InMemorySyncStateStore(),
            )
            assert (
                push_accepted_proposals(
                    service,
                    retry_session,
                    fake_backend.source,
                )
                == 0
            )
            assert retry_session.get(ScheduleProposal, proposal_id).status is (
                ProposalStatus.INVALIDATED
            )
            assert retry_session.scalar(
                select(CalendarEventMirror).where(
                    CalendarEventMirror.external_id == created_external_id
                )
            ) is None
        assert fake_backend.delete_calls == [created_external_id]


@pytest.mark.parametrize("source", [CalendarSource.GOOGLE, CalendarSource.CALDAV])
def test_job_ids_are_per_source(source) -> None:
    assert calendar_job_id(source) == f"healthmes-calendar-{source.value}"
