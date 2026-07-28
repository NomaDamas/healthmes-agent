from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from healthmes.calendars.approval import ApprovalCalendar
from healthmes.calendars.sleep_apply import (
    apply_sleep_proposal,
    approval_token,
    decline_sleep_proposal,
)
from healthmes.calendars.sleep_proposal_state import redacted_digest
from healthmes.calendars.sleep_proposals import prepare_sleep_proposal
from healthmes.store import (
    CalendarSource,
    SleepProposalStatus,
    SleepReconciliationProposal,
)
from tests.calendars.conftest import FakeCalendarBackend


class SleepReader:
    def __init__(self, rows):
        self.rows = rows

    async def collect_sleep_summaries(self, user_id, start_date, end_date):
        assert user_id == "redacted-user"
        return self.rows


class SplitSleepReader(SleepReader):
    async def collect_sleep_sessions(self, user_id, start_date, end_date):
        assert user_id == "redacted-user"
        return [
            {
                "start_time": "2026-07-25T23:00:00+09:00",
                "end_time": "2026-07-26T07:00:00+09:00",
                "is_nap": False,
                "sleep_stage_intervals": [
                    {
                        "stage": "awake",
                        "start_time": "2026-07-25T23:00:00+09:00",
                        "end_time": "2026-07-25T23:30:00+09:00",
                    },
                    {
                        "stage": "light",
                        "start_time": "2026-07-25T23:30:00+09:00",
                        "end_time": "2026-07-26T01:00:00+09:00",
                    },
                    {
                        "stage": "awake",
                        "start_time": "2026-07-26T01:00:00+09:00",
                        "end_time": "2026-07-26T02:00:00+09:00",
                    },
                    {
                        "stage": "rem",
                        "start_time": "2026-07-26T02:00:00+09:00",
                        "end_time": "2026-07-26T07:00:00+09:00",
                    },
                ],
            }
        ]


def approval_calendar(backend: FakeCalendarBackend) -> ApprovalCalendar:
    return ApprovalCalendar(backend, backend.approval_target)


def summary(*, wake="2026-07-26T07:00:00+09:00", duration=420):
    return {
        "date": "2026-07-26",
        "source": {"provider": "oura"},
        "start_time": "2026-07-25T23:00:00+09:00",
        "end_time": wake,
        "duration_minutes": duration,
        "time_in_bed_minutes": 450,
    }


@pytest.mark.asyncio
async def test_prepare_freezes_redacted_snapshot_without_calendar_write(
    session,
    fake_backend,
) -> None:
    proposal = await prepare_sleep_proposal(
        target_date=date(2026, 7, 26),
        calendar_source=CalendarSource.GOOGLE,
        reader=SleepReader([summary()]),
        user_id="redacted-user",
        session=session,
        calendar=approval_calendar(fake_backend),
        now=datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
    )

    assert proposal.status is SleepProposalStatus.PENDING
    assert proposal.snapshot["action"] == "would_create"
    assert proposal.snapshot["provider_guard"] == {
        "target": redacted_digest(fake_backend.approval_target),
        "actual": None,
        "planned": [],
    }
    assert proposal.provider_state == {
        "target": fake_backend.approval_target,
        "actual": None,
        "planned": [],
    }
    assert fake_backend.created_drafts == []
    assert fake_backend.update_calls == []
    assert fake_backend.delete_calls == []


@pytest.mark.asyncio
async def test_invalid_decline_and_expired_approval_never_write(
    session,
    fake_backend,
) -> None:
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    proposal = await prepare_sleep_proposal(
        target_date=date(2026, 7, 26),
        calendar_source=CalendarSource.GOOGLE,
        reader=SleepReader([summary()]),
        user_id="redacted-user",
        session=session,
        calendar=approval_calendar(fake_backend),
        now=now,
    )

    invalid = await apply_sleep_proposal(
        proposal_id=proposal.id,
        submitted_token="wrong",
        local_session_id="local-session",
        secret=b"secret",
        reader=SleepReader([summary()]),
        user_id="redacted-user",
        session=session,
        calendar=approval_calendar(fake_backend),
        now=now,
    )
    assert invalid.status is SleepProposalStatus.INVALID
    assert fake_backend.created_drafts == []

    retry = await prepare_sleep_proposal(
        target_date=date(2026, 7, 26),
        calendar_source=CalendarSource.GOOGLE,
        reader=SleepReader([summary()]),
        user_id="redacted-user",
        session=session,
        calendar=approval_calendar(fake_backend),
        now=now,
    )
    declined = decline_sleep_proposal(session, retry.id, now)
    assert declined.status is SleepProposalStatus.DECLINED
    assert fake_backend.created_drafts == []

    expired = await prepare_sleep_proposal(
        target_date=date(2026, 7, 26),
        calendar_source=CalendarSource.GOOGLE,
        reader=SleepReader([summary()]),
        user_id="redacted-user",
        session=session,
        calendar=approval_calendar(fake_backend),
        now=now,
    )
    token = approval_token(expired, "local-session", b"secret")
    result = await apply_sleep_proposal(
        proposal_id=expired.id,
        submitted_token=token,
        local_session_id="local-session",
        secret=b"secret",
        reader=SleepReader([summary()]),
        user_id="redacted-user",
        session=session,
        calendar=approval_calendar(fake_backend),
        now=now + timedelta(minutes=16),
    )
    assert result.status is SleepProposalStatus.EXPIRED
    assert fake_backend.created_drafts == []


@pytest.mark.asyncio
async def test_changed_oura_fingerprint_closes_without_write(session, fake_backend) -> None:
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    proposal = await prepare_sleep_proposal(
        target_date=date(2026, 7, 26),
        calendar_source=CalendarSource.GOOGLE,
        reader=SleepReader([summary()]),
        user_id="redacted-user",
        session=session,
        calendar=approval_calendar(fake_backend),
        now=now,
    )
    token = approval_token(proposal, "local-session", b"secret")

    result = await apply_sleep_proposal(
        proposal_id=proposal.id,
        submitted_token=token,
        local_session_id="local-session",
        secret=b"secret",
        reader=SleepReader([summary(wake="2026-07-26T07:30:00+09:00", duration=450)]),
        user_id="redacted-user",
        session=session,
        calendar=approval_calendar(fake_backend),
        now=now,
    )

    assert result.status is SleepProposalStatus.CONFLICTED
    assert fake_backend.created_drafts == []


@pytest.mark.asyncio
async def test_target_calendar_change_closes_without_write(session, fake_backend) -> None:
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    proposal = await prepare_sleep_proposal(
        target_date=date(2026, 7, 26),
        calendar_source=CalendarSource.GOOGLE,
        reader=SleepReader([summary()]),
        user_id="redacted-user",
        session=session,
        calendar=approval_calendar(fake_backend),
        now=now,
    )
    token = approval_token(proposal, "local-session", b"secret")
    wrong_calendar = FakeCalendarBackend(
        CalendarSource.GOOGLE,
        approval_target="other-calendar",
    )

    result = await apply_sleep_proposal(
        proposal_id=proposal.id,
        submitted_token=token,
        local_session_id="local-session",
        secret=b"secret",
        reader=SleepReader([summary()]),
        user_id="redacted-user",
        session=session,
        calendar=approval_calendar(wrong_calendar),
        now=now,
    )

    assert result.status is SleepProposalStatus.CONFLICTED
    assert wrong_calendar.created_drafts == []


@pytest.mark.asyncio
async def test_valid_one_shot_apply_uses_snapshot_and_fresh_read_back(
    session,
    fake_backend,
) -> None:
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    proposal = await prepare_sleep_proposal(
        target_date=date(2026, 7, 26),
        calendar_source=CalendarSource.GOOGLE,
        reader=SleepReader([summary()]),
        user_id="redacted-user",
        session=session,
        calendar=approval_calendar(fake_backend),
        now=now,
    )
    token = approval_token(proposal, "local-session", b"secret")

    result = await apply_sleep_proposal(
        proposal_id=proposal.id,
        submitted_token=token,
        local_session_id="local-session",
        secret=b"secret",
        reader=SleepReader([summary()]),
        user_id="redacted-user",
        session=session,
        calendar=approval_calendar(fake_backend),
        now=now,
    )

    assert result.status is SleepProposalStatus.APPLIED
    assert result.receipt is not None
    assert result.receipt["verified"] is True
    assert result.receipt["event"] != fake_backend.created_drafts[0].identity.source_key
    assert len(fake_backend.created_drafts) == 1
    replay = await apply_sleep_proposal(
        proposal_id=proposal.id,
        submitted_token=token,
        local_session_id="local-session",
        secret=b"secret",
        reader=SleepReader([summary()]),
        user_id="redacted-user",
        session=session,
        calendar=approval_calendar(fake_backend),
        now=now,
    )
    assert replay.status is SleepProposalStatus.APPLIED
    assert len(fake_backend.created_drafts) == 1
    assert session.get(SleepReconciliationProposal, proposal.id).receipt == result.receipt


@pytest.mark.asyncio
async def test_approval_splits_calendar_events_around_awake_intervals(
    session,
    fake_backend,
) -> None:
    now = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    reader = SplitSleepReader([summary()])
    calendar = ApprovalCalendar(
        fake_backend,
        fake_backend.approval_target,
        "http://127.0.0.1:8100",
    )
    proposal = await prepare_sleep_proposal(
        target_date=date(2026, 7, 26),
        calendar_source=CalendarSource.GOOGLE,
        reader=reader,
        user_id="redacted-user",
        session=session,
        calendar=calendar,
        now=now,
    )

    assert proposal.snapshot["action"] == "would_split"
    assert proposal.snapshot["segment_count"] == 2
    assert proposal.snapshot["segments"] == [
        {
            "start": "2026-07-25T14:30:00+00:00",
            "wake_time": "2026-07-25T16:00:00+00:00",
            "duration_minutes": 90,
        },
        {
            "start": "2026-07-25T17:00:00+00:00",
            "wake_time": "2026-07-25T22:00:00+00:00",
            "duration_minutes": 300,
        },
    ]

    result = await apply_sleep_proposal(
        proposal_id=proposal.id,
        submitted_token=approval_token(proposal, "local-session", b"secret"),
        local_session_id="local-session",
        secret=b"secret",
        reader=reader,
        user_id="redacted-user",
        session=session,
        calendar=calendar,
        now=now,
    )

    assert result.status is SleepProposalStatus.APPLIED
    assert result.receipt is not None
    assert len(result.receipt["events"]) == 2
    assert len(fake_backend.created_drafts) == 2
    assert all(
        "http://127.0.0.1:8100/sleep?date=2026-07-26" in (draft.description or "")
        for draft in fake_backend.created_drafts
    )
    assert [
        (draft.start_at.isoformat(), draft.end_at.isoformat())
        for draft in fake_backend.created_drafts
    ] == [
        ("2026-07-25T14:30:00+00:00", "2026-07-25T16:00:00+00:00"),
        ("2026-07-25T17:00:00+00:00", "2026-07-25T22:00:00+00:00"),
    ]
