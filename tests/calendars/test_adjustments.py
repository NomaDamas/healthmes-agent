from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from healthmes.api.decision_html import render_decision_html
from healthmes.calendars.adjustments import (
    APPLYING_RECONCILE_DELAY,
    AdjustmentError,
    AdjustmentOperation,
    AdjustmentStatus,
    AmbiguousProviderResult,
    CalendarAdjustmentService,
    InMemoryAdjustmentRepository,
    SqlAlchemyAdjustmentRepository,
    digest_reply_handle,
    evaluate_event_eligibility,
    evaluate_health_evidence,
    initial_decision_tree,
    issue_reply_handle,
    make_shorten_snapshot,
    morning_dedup_key,
    outcome_decision_tree,
    proposal_dedup_key,
    redacted_receipt,
    validate_shorten_change,
    verify_reply_handle,
)
from healthmes.calendars.base import (
    CalendarConflictError,
    CalendarError,
    ConfirmedExternalTimeChange,
    ExternalEvent,
)
from healthmes.calendars.state import InMemorySyncStateStore
from healthmes.calendars.sync import CalendarMirrorService
from healthmes.store import Base, create_db_engine
from healthmes.store.enums import CalendarMutationStatus, CalendarSource
from healthmes.store.models import (
    CalendarEventMirror,
    CalendarMutationProposal,
    DecisionRecord,
    TriggerEvent,
)

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 7, 21, 22, 0, tzinfo=UTC)
LOCAL_DAY = date(2026, 7, 22)
SECRET = "test-secret"
HANDLE = "reply-handle-fixture"


def event(**overrides):
    data = {
        "id": uuid.uuid4(),
        "external_id": "evt-fixture",
        "calendar_source": CalendarSource.GOOGLE,
        "summary": "Recovery-safe focus block",
        "start_at": datetime(2026, 7, 22, 5, 0, tzinfo=UTC),
        "end_at": datetime(2026, 7, 22, 6, 0, tzinfo=UTC),
        "is_agent_created": False,
        "organizer_self": True,
        "has_attendees": False,
        "is_recurring": False,
        "event_type": "default",
        "is_all_day": False,
        "is_locked": False,
        "status": "confirmed",
        "etag": '"etag-v1"',
    }
    data.update(overrides)
    return data


def health_context(**overrides):
    data = {
        "sleep_debt": {
            "status": "ok",
            "confidence": "medium",
            "observed_at": NOW - timedelta(hours=6),
        },
        "charge": {
            "status": "ok",
            "confidence": "medium",
            "observed_at": NOW - timedelta(hours=2),
            "value": 32.0,
        },
    }
    data.update(overrides)
    return data


class FakeStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class FakeWriter:
    def __init__(
        self, *, mode: str = "success", read_mode: str | None = None, status_code: int | None = None
    ) -> None:
        self.mode = mode
        self.read_mode = read_mode or mode
        self.status_code = status_code
        self.patch_calls: list[ConfirmedExternalTimeChange] = []
        self.read_calls: list[str] = []

    def apply_confirmed_external_time_change(
        self, change: ConfirmedExternalTimeChange
    ) -> ExternalEvent:
        self.patch_calls.append(change)
        if self.mode == "conflict":
            raise CalendarConflictError("provider 412")
        if self.mode == "ambiguous":
            raise AmbiguousProviderResult("timeout")
        if self.mode == "status":
            assert self.status_code is not None
            raise FakeStatusError(self.status_code)
        if self.mode == "calendar_error":
            raise CalendarError("provider returned a mismatched 200 response")
        if self.mode == "mismatch":
            return external_event(end_at=change.original_end_at)
        return external_event(end_at=change.proposed_end_at)

    def read_event(self, external_id: str) -> ExternalEvent:
        self.read_calls.append(external_id)
        if self.read_mode == "read_proposed":
            proposal = next(iter(self.patch_calls), None)
            assert proposal is not None
            return external_event(end_at=proposal.proposed_end_at)
        if self.read_mode == "restart_proposed":
            return external_event(end_at=datetime(2026, 7, 22, 5, 30, tzinfo=UTC))
        if self.read_mode == "read_fails":
            raise RuntimeError("read failed")
        return external_event(end_at=datetime(2026, 7, 22, 6, 0, tzinfo=UTC))


def external_event(*, end_at: datetime) -> ExternalEvent:
    return ExternalEvent(
        external_id="evt-fixture",
        summary="Recovery-safe focus block",
        start_at=datetime(2026, 7, 22, 5, 0, tzinfo=UTC),
        end_at=end_at,
        is_agent_created=False,
        etag='"etag-v2"',
        organizer_self=True,
        has_attendees=False,
        is_recurring=False,
        event_type="default",
        is_all_day=False,
        is_locked=False,
        status="confirmed",
    )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"calendar_source": CalendarSource.CALDAV}, "unsupported_source"),
        ({"is_agent_created": True}, "agent_owned_path_only"),
        ({"organizer_self": False}, "not_self_organized"),
        ({"has_attendees": True}, "has_attendees"),
        ({"is_recurring": True}, "recurring"),
        ({"is_all_day": True}, "all_day"),
        ({"event_type": "workingLocation"}, "unsupported_event_type"),
        ({"is_locked": True}, "locked"),
        ({"status": "cancelled"}, "cancelled"),
        ({"start_at": NOW + timedelta(minutes=30)}, "too_soon"),
        ({"start_at": datetime(2026, 7, 23, 5, tzinfo=UTC)}, "not_today"),
        ({"end_at": datetime(2026, 7, 22, 5, 45, tzinfo=UTC)}, "too_short"),
        ({"etag": None}, "missing_etag"),
    ],
)
def test_event_eligibility_rejects_unsafe_cases(overrides, reason) -> None:
    result = evaluate_event_eligibility(
        event(**overrides), now=NOW, local_date=LOCAL_DAY, timezone=KST
    )

    assert not result.eligible
    assert reason in result.reasons


def test_event_eligibility_accepts_baseline_and_existing_dedup_rejects() -> None:
    result = evaluate_event_eligibility(event(), now=NOW, local_date=LOCAL_DAY, timezone=KST)
    duplicate = evaluate_event_eligibility(
        event(), now=NOW, local_date=LOCAL_DAY, timezone=KST, already_proposed=True
    )

    assert result.eligible
    assert duplicate.reasons == ("already_proposed",)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"proposed_start_at": datetime(2026, 7, 22, 5, 15, tzinfo=UTC)}, "start_at"),
        ({"proposed_end_at": datetime(2026, 7, 22, 6, 15, tzinfo=UTC)}, "shorten"),
        ({"proposed_end_at": datetime(2026, 7, 22, 5, 45, tzinfo=UTC)}, "30 minutes"),
        (
            {
                "original_end_at": datetime(2026, 7, 22, 5, 45, tzinfo=UTC),
                "proposed_end_at": datetime(2026, 7, 22, 5, 15, tzinfo=UTC),
            },
            "at least 60",
        ),
        (
            {
                "original_start_at": datetime(2026, 7, 22, 5, 0),
                "proposed_start_at": datetime(2026, 7, 22, 5, 0),
            },
            "naive",
        ),
        ({"operation": "move"}, "SHORTEN"),
    ],
)
def test_validate_shorten_change_rejects_invalid_invariants(overrides, match) -> None:
    kwargs = {
        "external_event_id": "evt-fixture",
        "original_start_at": datetime(2026, 7, 22, 5, 0, tzinfo=UTC),
        "original_end_at": datetime(2026, 7, 22, 6, 0, tzinfo=UTC),
        "proposed_start_at": datetime(2026, 7, 22, 5, 0, tzinfo=UTC),
        "proposed_end_at": datetime(2026, 7, 22, 5, 30, tzinfo=UTC),
        "expected_etag": '"etag-v1"',
        "operation": AdjustmentOperation.SHORTEN,
    }
    kwargs.update(overrides)

    with pytest.raises(AdjustmentError, match=match):
        validate_shorten_change(**kwargs)


def test_validate_shorten_change_accepts_expected_delta() -> None:
    change = validate_shorten_change(
        external_event_id="evt-fixture",
        original_start_at=datetime(2026, 7, 22, 5, 0, tzinfo=UTC),
        original_end_at=datetime(2026, 7, 22, 6, 0, tzinfo=UTC),
        proposed_start_at=datetime(2026, 7, 22, 5, 0, tzinfo=UTC),
        proposed_end_at=datetime(2026, 7, 22, 5, 30, tzinfo=UTC),
        expected_etag='"etag-v1"',
    )

    assert change.proposed_end_at == datetime(2026, 7, 22, 5, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("context_overrides", "busy", "eligible_count", "reason"),
    [
        (
            {"sleep_debt": {"status": "insufficient_data", "confidence": "high"}},
            240,
            1,
            "missing_sleep",
        ),
        (
            {"sleep_debt": {"status": "ok", "confidence": "low", "observed_at": NOW}},
            240,
            1,
            "low_confidence_sleep",
        ),
        (
            {
                "sleep_debt": {
                    "status": "ok",
                    "confidence": "medium",
                    "observed_at": NOW - timedelta(days=2),
                }
            },
            240,
            1,
            "stale_sleep",
        ),
        ({"charge": None}, 240, 1, "missing_recovery"),
        (
            {"charge": {"status": "ok", "confidence": "low", "observed_at": NOW, "value": 32}},
            240,
            1,
            "low_confidence_recovery",
        ),
        (
            {"charge": {"status": "ok", "confidence": "medium", "observed_at": NOW, "value": 55}},
            240,
            1,
            "no_nudge_needed",
        ),
        ({}, 120, 1, "afternoon_not_heavy"),
        ({}, 240, 0, "no_eligible_event"),
    ],
)
def test_health_evidence_gate_failures(context_overrides, busy, eligible_count, reason) -> None:
    result = evaluate_health_evidence(
        health_context(**context_overrides),
        local_date=LOCAL_DAY,
        now=NOW,
        afternoon_busy_minutes=busy,
        eligible_event_count=eligible_count,
    )

    assert not result.allowed
    assert result.reason == reason


def test_health_evidence_gate_accepts_fresh_low_recovery() -> None:
    result = evaluate_health_evidence(
        health_context(),
        local_date=LOCAL_DAY,
        now=NOW,
        afternoon_busy_minutes=240,
        eligible_event_count=1,
    )

    assert result.allowed
    assert result.facts["recovery_value_bucket"] == "low"


def test_health_evidence_accepts_daily_readiness_charge_entries() -> None:
    context = health_context(
        charge={
            "status": "ok",
            "confidence": "medium",
            "entries": [
                {
                    "category": "recovery",
                    "provider": "polar",
                    "value": 2.0,
                    "observed_on": LOCAL_DAY.isoformat(),
                }
            ],
        }
    )

    result = evaluate_health_evidence(
        context,
        local_date=LOCAL_DAY,
        now=NOW,
        afternoon_busy_minutes=240,
        eligible_event_count=1,
    )

    assert result.allowed
    assert result.facts["recovery_value_bucket"] == "very_low"


def test_reply_handle_digest_is_one_time_plaintext_boundary() -> None:
    pair = issue_reply_handle(SECRET, handle_factory=lambda: HANDLE)

    assert pair.plaintext == HANDLE
    assert pair.digest == digest_reply_handle(HANDLE, SECRET)
    assert HANDLE not in pair.digest
    assert verify_reply_handle(HANDLE, pair.digest, SECRET)
    assert not verify_reply_handle("wrong", pair.digest, SECRET)


def test_evaluate_creates_one_proposal_and_claims_trigger_without_backend_write() -> None:
    repo = InMemoryAdjustmentRepository()
    service = CalendarAdjustmentService(repo, handle_secret=SECRET, clock=lambda: NOW)

    result = service.evaluate_morning_calendar_nudge(
        local_date=LOCAL_DAY,
        timezone=KST,
        health_context=health_context(),
        candidates=[event()],
        afternoon_busy_minutes=240,
        handle_factory=lambda: HANDLE,
    )
    second = service.evaluate_morning_calendar_nudge(
        local_date=LOCAL_DAY,
        timezone=KST,
        health_context=health_context(),
        candidates=[event()],
        afternoon_busy_minutes=240,
        handle_factory=lambda: "new-handle",
    )

    assert result.outcome == "proposed"
    assert result.reply_handle == HANDLE
    assert second.outcome == "deduplicated"
    assert len(repo.proposals) == 1
    proposal = repo.get_proposal(result.proposal_id)
    assert proposal is not None
    assert proposal.reply_handle_digest == digest_reply_handle(HANDLE, SECRET)
    assert HANDLE not in repr(proposal)
    trigger = repo.trigger_events[morning_dedup_key(LOCAL_DAY)]
    assert trigger["payload"]["outcome"] == "proposed"


def test_no_action_health_failure_is_deduped_and_has_no_proposal() -> None:
    repo = InMemoryAdjustmentRepository()
    service = CalendarAdjustmentService(repo, handle_secret=SECRET, clock=lambda: NOW)

    result = service.evaluate_morning_calendar_nudge(
        local_date=LOCAL_DAY,
        timezone=KST,
        health_context=health_context(
            charge={"status": "ok", "confidence": "medium", "observed_at": NOW, "value": 80}
        ),
        candidates=[event()],
        afternoon_busy_minutes=240,
    )

    assert result.outcome == "no_action"
    assert result.reason == "no_nudge_needed"
    assert repo.proposals == {}
    trigger = repo.trigger_events[morning_dedup_key(LOCAL_DAY)]
    assert trigger["payload"]["reason"] == "no_nudge_needed"
    decision_id = uuid.UUID(trigger["payload"]["decision_record_id"])
    assert result.decision_record_id == decision_id
    assert repo.decision_records[decision_id]["detail"] == {
        "outcome": "no_action",
        "reason": "no_nudge_needed",
    }


def test_yes_applies_once_and_receipt_is_redacted() -> None:
    repo, proposal_id = create_pending()
    service = CalendarAdjustmentService(repo, handle_secret=SECRET, clock=lambda: NOW)
    writer = FakeWriter()

    result = service.resolve_calendar_adjustment(
        proposal_id, response="yes", reply_handle=HANDLE, writer=writer, response_channel="telegram"
    )
    replay = service.resolve_calendar_adjustment(
        proposal_id, response="yes", reply_handle=HANDLE, writer=writer, response_channel="telegram"
    )

    assert result.status == AdjustmentStatus.APPLIED
    assert replay.status == AdjustmentStatus.APPLIED
    assert len(writer.patch_calls) == 1
    assert writer.patch_calls[0].proposed_end_at == datetime(2026, 7, 22, 5, 30, tzinfo=UTC)
    assert repo.get_proposal(proposal_id).response_channel == "telegram"
    assert_sensitive_values_absent(
        result.receipt, HANDLE, "evt-fixture", '"etag-v1"', "Recovery-safe"
    )


@pytest.mark.parametrize("handle", [None, "wrong"])
def test_missing_or_wrong_handle_never_writes(handle) -> None:
    repo, proposal_id = create_pending()
    service = CalendarAdjustmentService(repo, handle_secret=SECRET, clock=lambda: NOW)
    writer = FakeWriter()

    result = service.resolve_calendar_adjustment(
        proposal_id, response="yes", reply_handle=handle, writer=writer
    )

    assert result.status == AdjustmentStatus.PENDING
    assert writer.patch_calls == []


def test_no_and_expiry_are_terminal_without_provider_calls() -> None:
    repo, declined_id = create_pending()
    service = CalendarAdjustmentService(repo, handle_secret=SECRET, clock=lambda: NOW)
    writer = FakeWriter()

    declined = service.resolve_calendar_adjustment(
        declined_id, response="no", reply_handle=HANDLE, writer=writer
    )
    late_repo, expired_id = create_pending()
    late_service = CalendarAdjustmentService(
        late_repo,
        handle_secret=SECRET,
        clock=lambda: datetime(2026, 7, 22, 4, 46, tzinfo=UTC),
    )
    expired = late_service.resolve_calendar_adjustment(
        expired_id, response="yes", reply_handle=HANDLE, writer=writer
    )

    assert declined.status == AdjustmentStatus.DECLINED
    assert expired.status == AdjustmentStatus.EXPIRED
    assert repo.get_proposal(declined_id).outcome_decision_record_id is not None
    assert repo.get_proposal(declined_id).consumed_at == NOW
    assert late_repo.get_proposal(expired_id).outcome_decision_record_id is not None
    assert writer.patch_calls == []


def test_yes_wins_racing_no_without_second_terminal_transition() -> None:
    repo, proposal_id = create_pending()
    service = CalendarAdjustmentService(repo, handle_secret=SECRET, clock=lambda: NOW)

    class RacingWriter(FakeWriter):
        def apply_confirmed_external_time_change(self, change):
            self.patch_calls.append(change)
            declined = service.resolve_calendar_adjustment(
                proposal_id,
                response="no",
                reply_handle=HANDLE,
                writer=self,
            )
            assert declined.status == AdjustmentStatus.APPLYING
            return external_event(end_at=change.proposed_end_at)

    writer = RacingWriter()
    result = service.resolve_calendar_adjustment(
        proposal_id,
        response="yes",
        reply_handle=HANDLE,
        writer=writer,
    )

    assert result.status == AdjustmentStatus.APPLIED
    assert repo.get_proposal(proposal_id).status == AdjustmentStatus.APPLIED
    assert len(writer.patch_calls) == 1


def test_yes_wins_racing_expiry_without_overwriting_applying() -> None:
    repo, proposal_id = create_pending()
    service = CalendarAdjustmentService(repo, handle_secret=SECRET, clock=lambda: NOW)
    late_service = CalendarAdjustmentService(
        repo,
        handle_secret=SECRET,
        clock=lambda: NOW + timedelta(hours=1),
    )

    class RacingWriter(FakeWriter):
        def apply_confirmed_external_time_change(self, change):
            self.patch_calls.append(change)
            expired = late_service.resolve_calendar_adjustment(
                proposal_id,
                response="yes",
                reply_handle=HANDLE,
                writer=self,
            )
            assert expired.status == AdjustmentStatus.APPLYING
            return external_event(end_at=change.proposed_end_at)

    writer = RacingWriter()
    result = service.resolve_calendar_adjustment(
        proposal_id,
        response="yes",
        reply_handle=HANDLE,
        writer=writer,
    )

    assert result.status == AdjustmentStatus.APPLIED
    assert repo.get_proposal(proposal_id).status == AdjustmentStatus.APPLIED
    assert len(writer.patch_calls) == 1


def test_maintenance_skips_fresh_applying_while_provider_write_is_active() -> None:
    repo, proposal_id = create_pending()
    service = CalendarAdjustmentService(repo, handle_secret=SECRET, clock=lambda: NOW)

    class RacingWriter(FakeWriter):
        def apply_confirmed_external_time_change(self, change):
            self.patch_calls.append(change)
            assert service.expire_and_reconcile_adjustments(self) == []
            assert self.read_calls == []
            return external_event(end_at=change.proposed_end_at)

    writer = RacingWriter()
    result = service.resolve_calendar_adjustment(
        proposal_id,
        response="yes",
        reply_handle=HANDLE,
        writer=writer,
    )

    assert result.status == AdjustmentStatus.APPLIED
    assert len(writer.patch_calls) == 1
    assert writer.read_calls == []


def test_snapshot_conflict_blocks_provider_write() -> None:
    repo, proposal_id = create_pending()
    service = CalendarAdjustmentService(repo, handle_secret=SECRET, clock=lambda: NOW)
    writer = FakeWriter()

    result = service.resolve_calendar_adjustment(
        proposal_id,
        response="yes",
        reply_handle=HANDLE,
        writer=writer,
        mirror_snapshot=event(etag='"etag-v2"'),
    )

    assert result.status == AdjustmentStatus.CONFLICTED
    assert writer.patch_calls == []


def test_provider_412_is_terminal_conflict_without_retry() -> None:
    repo, proposal_id = create_pending()
    service = CalendarAdjustmentService(repo, handle_secret=SECRET, clock=lambda: NOW)
    writer = FakeWriter(mode="conflict")

    result = service.resolve_calendar_adjustment(
        proposal_id, response="yes", reply_handle=HANDLE, writer=writer
    )

    assert result.status == AdjustmentStatus.CONFLICTED
    assert len(writer.patch_calls) == 1


def test_provider_200_parse_mismatch_is_unknown_without_read_retry() -> None:
    repo, proposal_id = create_pending()
    service = CalendarAdjustmentService(repo, handle_secret=SECRET, clock=lambda: NOW)
    writer = FakeWriter(mode="calendar_error")

    result = service.resolve_calendar_adjustment(
        proposal_id, response="yes", reply_handle=HANDLE, writer=writer
    )

    assert result.status == AdjustmentStatus.UNKNOWN
    assert len(writer.patch_calls) == 1
    assert writer.read_calls == []
    assert result.receipt["provider_code"] == "provider_200_mismatch"


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 410])
def test_known_provider_statuses_are_terminal_failed_without_read_retry(status_code) -> None:
    repo, proposal_id = create_pending()
    service = CalendarAdjustmentService(repo, handle_secret=SECRET, clock=lambda: NOW)
    writer = FakeWriter(mode="status", status_code=status_code)

    result = service.resolve_calendar_adjustment(
        proposal_id, response="yes", reply_handle=HANDLE, writer=writer
    )

    assert result.status == AdjustmentStatus.FAILED
    assert len(writer.patch_calls) == 1
    assert writer.read_calls == []
    assert result.receipt["provider_code"] == f"provider_{status_code}"


@pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504])
def test_transient_provider_statuses_use_read_only_reconcile(status_code) -> None:
    repo, proposal_id = create_pending()
    service = CalendarAdjustmentService(repo, handle_secret=SECRET, clock=lambda: NOW)
    writer = FakeWriter(mode="status", read_mode="read_proposed", status_code=status_code)

    result = service.resolve_calendar_adjustment(
        proposal_id, response="yes", reply_handle=HANDLE, writer=writer
    )

    assert result.status == AdjustmentStatus.APPLIED_RECOVERED
    assert len(writer.patch_calls) == 1
    assert writer.read_calls == ["evt-fixture"]
    assert result.receipt["provider_code"] == f"provider_{status_code}"


@pytest.mark.parametrize(
    ("mode", "status"),
    [
        ("read_proposed", AdjustmentStatus.APPLIED_RECOVERED),
        ("ambiguous", AdjustmentStatus.UNKNOWN),
        ("read_fails", AdjustmentStatus.UNKNOWN),
    ],
)
def test_ambiguous_provider_result_uses_one_patch_then_read_only_reconcile(mode, status) -> None:
    repo, proposal_id = create_pending()
    service = CalendarAdjustmentService(repo, handle_secret=SECRET, clock=lambda: NOW)
    writer = FakeWriter(mode="ambiguous", read_mode=mode)

    result = service.resolve_calendar_adjustment(
        proposal_id, response="yes", reply_handle=HANDLE, writer=writer
    )

    assert result.status == status
    assert len(writer.patch_calls) == 1
    assert writer.read_calls == ["evt-fixture"]


@pytest.mark.parametrize(
    ("mode", "status"),
    [
        ("restart_proposed", AdjustmentStatus.APPLIED_RECOVERED),
        ("ambiguous", AdjustmentStatus.UNKNOWN),
        ("read_fails", AdjustmentStatus.UNKNOWN),
    ],
)
def test_restart_recovery_for_applying_uses_get_without_extra_patch(mode, status) -> None:
    repo, proposal_id = create_pending()
    proposal = repo.get_proposal(proposal_id)
    proposal.status = AdjustmentStatus.APPLYING
    proposal.attempt_id = uuid.uuid4()
    proposal.updated_at = NOW - APPLYING_RECONCILE_DELAY
    service = CalendarAdjustmentService(repo, handle_secret=SECRET, clock=lambda: NOW)
    writer = FakeWriter(mode=mode)

    results = service.expire_and_reconcile_adjustments(writer)

    assert [result.status for result in results] == [status]
    assert writer.patch_calls == []
    assert writer.read_calls == ["evt-fixture"]


def test_dedup_and_decision_surfaces_are_stable_and_redacted() -> None:
    candidate = event()
    snapshot = make_shorten_snapshot(candidate, timezone=KST)
    proposal_key = proposal_dedup_key(
        candidate, timezone=KST, operation=AdjustmentOperation.SHORTEN
    )
    tree = initial_decision_tree(snapshot, {"sleep_confidence": "medium"})
    receipt = redacted_receipt(status=AdjustmentStatus.APPLIED, provider_code="provider_200")
    stored = next(iter(create_pending()[0].proposals.values()))
    outcome = outcome_decision_tree(stored, receipt)

    assert snapshot.dedup_key == proposal_key
    assert morning_dedup_key(LOCAL_DAY) == "morning_calendar_nudge:2026-07-22"
    action_detail = tree["children"][-1]["detail"]
    assert action_detail["event_label"] == "Recovery-safe focus block"
    assert action_detail["before"]["start_at"].endswith("+09:00")
    assert action_detail["after"]["end_at"].endswith("+09:00")
    assert_sensitive_values_absent(tree, HANDLE, "evt-fixture", '"etag-v1"')
    assert_sensitive_values_absent(outcome, HANDLE, "evt-fixture", '"etag-v1"')


def test_sqlalchemy_repository_persists_proposal_trigger_and_decisions(session) -> None:
    mirror = CalendarEventMirror(
        external_id="evt-fixture",
        calendar_source=CalendarSource.GOOGLE,
        summary="Recovery-safe focus block",
        start_at=datetime(2026, 7, 22, 5, 0, tzinfo=UTC),
        end_at=datetime(2026, 7, 22, 6, 0, tzinfo=UTC),
        is_agent_created=False,
        agent_task_id=None,
        etag='"etag-v1"',
        sync_token=None,
        organizer_self=True,
        has_attendees=False,
        is_recurring=False,
        event_type="default",
        is_all_day=False,
        is_locked=False,
        status="confirmed",
    )
    session.add(mirror)
    session.flush()
    repo = SqlAlchemyAdjustmentRepository(session)
    service = CalendarAdjustmentService(repo, handle_secret=SECRET, clock=lambda: NOW)

    result = service.evaluate_morning_calendar_nudge(
        local_date=LOCAL_DAY,
        timezone=KST,
        health_context=health_context(),
        candidates=[mirror],
        afternoon_busy_minutes=240,
        handle_factory=lambda: HANDLE,
    )
    writer = FakeWriter()
    resolved = service.resolve_calendar_adjustment(
        result.proposal_id,
        response="yes",
        reply_handle=HANDLE,
        writer=writer,
        response_channel="telegram",
    )
    session.flush()

    proposal = session.get(CalendarMutationProposal, result.proposal_id)
    trigger = session.query(TriggerEvent).filter_by(dedup_key=morning_dedup_key(LOCAL_DAY)).one()
    decisions = session.query(DecisionRecord).order_by(DecisionRecord.created_at).all()
    assert proposal.status is CalendarMutationStatus.APPLIED
    assert proposal.reply_handle_digest == digest_reply_handle(HANDLE, SECRET)
    assert proposal.consumed_at is not None
    assert proposal.attempt_id is not None
    assert proposal.response_channel == "telegram"
    assert proposal.receipt == resolved.receipt
    assert trigger.payload["outcome"] == "proposed"
    assert len(decisions) == 2
    assert decisions[0].tree["detail"]["proposal_id"] == str(proposal.id)
    assert decisions[0].tree["children"][0]["label"] == "health evidence"
    assert decisions[1].tree["detail"]["receipt"]["status"] == "applied"
    assert mirror.end_at == datetime(2026, 7, 22, 5, 30, tzinfo=UTC)
    assert mirror.etag == '"etag-v2"'
    assert len(writer.patch_calls) == 1
    assert_sensitive_values_absent(proposal.receipt, HANDLE, "evt-fixture", '"etag-v1"')


def test_provider_deletion_preserves_proposal_and_blocks_later_apply(
    session, fake_backend, make_event
) -> None:
    state_store = InMemorySyncStateStore()
    mirror_service = CalendarMirrorService(session, [fake_backend], state_store)
    fake_backend.queue_changes(
        [
            make_event(
                "evt-fixture",
                summary="Recovery-safe focus block",
                start=datetime(2026, 7, 22, 5, 0, tzinfo=UTC),
                end=datetime(2026, 7, 22, 6, 0, tzinfo=UTC),
                etag='"etag-v1"',
                organizer_self=True,
                event_type="default",
                status="confirmed",
            )
        ],
        {"sync_token": "tok-1"},
    )
    mirror_service.sync_backend(fake_backend)
    mirror = session.query(CalendarEventMirror).filter_by(external_id="evt-fixture").one()

    repo = SqlAlchemyAdjustmentRepository(session)
    service = CalendarAdjustmentService(repo, handle_secret=SECRET, clock=lambda: NOW)
    result = service.evaluate_morning_calendar_nudge(
        local_date=LOCAL_DAY,
        timezone=KST,
        health_context=health_context(),
        candidates=[mirror],
        afternoon_busy_minutes=240,
        handle_factory=lambda: HANDLE,
    )
    proposal = session.get(CalendarMutationProposal, result.proposal_id)
    proposal_decision_id = proposal.proposal_decision_record_id

    fake_backend.queue_changes(
        [make_event("evt-fixture", deleted=True, summary=None, etag=None)],
        {"sync_token": "tok-2"},
    )
    mirror_service.sync_backend(fake_backend)
    session.expire_all()

    retained = session.get(CalendarMutationProposal, result.proposal_id)
    assert state_store.load(CalendarSource.GOOGLE) == {"sync_token": "tok-2"}
    assert session.get(CalendarEventMirror, mirror.id) is None
    assert retained.mirror_event_id is None
    assert session.get(DecisionRecord, proposal_decision_id) is not None

    writer = FakeWriter()
    resolved = service.resolve_calendar_adjustment(
        result.proposal_id,
        response="yes",
        reply_handle=HANDLE,
        writer=writer,
        response_channel="telegram",
        mirror_snapshot=None,
    )

    assert resolved.status == AdjustmentStatus.CONFLICTED
    assert writer.patch_calls == []
    assert session.get(CalendarMutationProposal, result.proposal_id).status is (
        CalendarMutationStatus.CONFLICTED
    )


@pytest.mark.parametrize(
    ("context", "candidates", "reason"),
    [
        (
            health_context(charge=None),
            [event()],
            "missing_recovery",
        ),
        (
            health_context(),
            [],
            "no_eligible_event",
        ),
    ],
)
def test_sqlalchemy_no_action_persists_viewer_renderable_decision(
    session, context, candidates, reason
) -> None:
    repo = SqlAlchemyAdjustmentRepository(session)
    service = CalendarAdjustmentService(repo, handle_secret=SECRET, clock=lambda: NOW)

    result = service.evaluate_morning_calendar_nudge(
        local_date=LOCAL_DAY,
        timezone=KST,
        health_context=context,
        candidates=candidates,
        afternoon_busy_minutes=240,
    )
    session.flush()

    trigger = session.query(TriggerEvent).filter_by(dedup_key=morning_dedup_key(LOCAL_DAY)).one()
    decision_id = uuid.UUID(trigger.payload["decision_record_id"])
    decision = session.get(DecisionRecord, decision_id)
    assert result.outcome == "no_action"
    assert result.decision_record_id == decision_id
    assert trigger.payload["reason"] == reason
    assert decision.tree["detail"]["reason"] == reason
    assert "leave calendar unchanged" in render_decision_html(decision)
    assert session.query(CalendarMutationProposal).count() == 0


def test_sqlalchemy_applying_state_commits_before_remote_write(session, session_factory) -> None:
    mirror = persist_mirror(session)
    repo = SqlAlchemyAdjustmentRepository(session)
    service = CalendarAdjustmentService(repo, handle_secret=SECRET, clock=lambda: NOW)
    result = service.evaluate_morning_calendar_nudge(
        local_date=LOCAL_DAY,
        timezone=KST,
        health_context=health_context(),
        candidates=[mirror],
        afternoon_busy_minutes=240,
        handle_factory=lambda: HANDLE,
    )

    class BoundaryCheckingWriter(FakeWriter):
        def apply_confirmed_external_time_change(self, change):
            with session_factory() as reader:
                proposal = reader.get(CalendarMutationProposal, result.proposal_id)
                assert proposal.status is CalendarMutationStatus.APPLYING
                assert proposal.attempt_id is not None
                assert proposal.consumed_at is not None
            return super().apply_confirmed_external_time_change(change)

    writer = BoundaryCheckingWriter()

    resolved = service.resolve_calendar_adjustment(
        result.proposal_id,
        response="yes",
        reply_handle=HANDLE,
        writer=writer,
    )

    assert resolved.status == AdjustmentStatus.APPLIED
    assert len(writer.patch_calls) == 1


@pytest.mark.parametrize(
    "terminal_status",
    [AdjustmentStatus.DECLINED, AdjustmentStatus.EXPIRED],
)
def test_sqlalchemy_pending_terminal_cas_loses_after_applying(
    tmp_path, terminal_status
) -> None:
    engine = create_db_engine(f"sqlite+pysqlite:///{tmp_path / 'terminal-race.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    try:
        with factory() as seed_session:
            mirror = persist_mirror(seed_session)
            repo = SqlAlchemyAdjustmentRepository(seed_session)
            service = CalendarAdjustmentService(repo, handle_secret=SECRET, clock=lambda: NOW)
            result = service.evaluate_morning_calendar_nudge(
                local_date=LOCAL_DAY,
                timezone=KST,
                health_context=health_context(),
                candidates=[mirror],
                afternoon_busy_minutes=240,
                handle_factory=lambda: HANDLE,
            )
            seed_session.commit()

        with factory() as apply_session:
            apply_repo = SqlAlchemyAdjustmentRepository(apply_session)
            applying = apply_repo.compare_and_mark_applying(
                result.proposal_id,
                expected_digest=digest_reply_handle(HANDLE, SECRET),
                now=NOW,
                attempt_id=uuid.uuid4(),
                response_channel="telegram",
            )
            assert applying is not None
            apply_session.commit()

        with factory() as losing_session:
            losing_repo = SqlAlchemyAdjustmentRepository(losing_session)
            lost = losing_repo.compare_and_mark_terminal(
                result.proposal_id,
                expected_status=AdjustmentStatus.PENDING,
                status=terminal_status,
                receipt=redacted_receipt(status=terminal_status),
                outcome_decision_tree={"id": "losing_terminal"},
                now=NOW,
            )
            assert lost is None
            losing_session.commit()

        with factory() as reader:
            proposal = reader.get(CalendarMutationProposal, result.proposal_id)
            assert proposal.status == AdjustmentStatus.APPLYING
            assert proposal.outcome_decision_record_id is None
            assert reader.query(DecisionRecord).count() == 1
    finally:
        engine.dispose()


def test_sqlite_daily_dedup_claim_uses_begin_immediate_when_not_in_transaction(
    session_factory,
) -> None:
    with session_factory() as fresh_session:
        repo = SqlAlchemyAdjustmentRepository(fresh_session)

        claimed = repo.claim_daily_evaluation(
            morning_dedup_key(LOCAL_DAY), {"outcome": "evaluating"}, NOW
        )

        assert claimed
        assert repo.begin_immediate_attempted


def test_read_remote_event_uses_provider_read_event_name() -> None:
    repo, proposal_id = create_pending()
    proposal = repo.get_proposal(proposal_id)
    proposal.status = AdjustmentStatus.APPLYING
    proposal.updated_at = NOW - APPLYING_RECONCILE_DELAY
    service = CalendarAdjustmentService(repo, handle_secret=SECRET, clock=lambda: NOW)

    class ReadEventOnlyWriter:
        def __init__(self) -> None:
            self.patch_calls = []
            self.read_calls = []

        def apply_confirmed_external_time_change(self, change):
            self.patch_calls.append(change)
            raise AssertionError("restart recovery must not patch")

        def read_event(self, external_id):
            self.read_calls.append(external_id)
            return external_event(end_at=datetime(2026, 7, 22, 5, 30, tzinfo=UTC))

    writer = ReadEventOnlyWriter()

    result = service.expire_and_reconcile_adjustments(writer)

    assert [item.status for item in result] == [AdjustmentStatus.APPLIED_RECOVERED]
    assert writer.read_calls == ["evt-fixture"]
    assert writer.patch_calls == []


def create_pending() -> tuple[InMemoryAdjustmentRepository, uuid.UUID]:
    repo = InMemoryAdjustmentRepository()
    service = CalendarAdjustmentService(repo, handle_secret=SECRET, clock=lambda: NOW)
    result = service.evaluate_morning_calendar_nudge(
        local_date=LOCAL_DAY,
        timezone=KST,
        health_context=health_context(),
        candidates=[event()],
        afternoon_busy_minutes=240,
        handle_factory=lambda: HANDLE,
    )
    assert result.proposal_id is not None
    return repo, result.proposal_id


def persist_mirror(session) -> CalendarEventMirror:
    mirror = CalendarEventMirror(
        external_id="evt-fixture",
        calendar_source=CalendarSource.GOOGLE,
        summary="Recovery-safe focus block",
        start_at=datetime(2026, 7, 22, 5, 0, tzinfo=UTC),
        end_at=datetime(2026, 7, 22, 6, 0, tzinfo=UTC),
        is_agent_created=False,
        agent_task_id=None,
        etag='"etag-v1"',
        sync_token=None,
        organizer_self=True,
        has_attendees=False,
        is_recurring=False,
        event_type="default",
        is_all_day=False,
        is_locked=False,
        status="confirmed",
    )
    session.add(mirror)
    session.flush()
    return mirror


def assert_sensitive_values_absent(surface, *values: str) -> None:
    serialized = repr(surface)
    for value in values:
        assert value not in serialized
