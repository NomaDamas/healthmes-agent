"""Evaluator tests: dedup uniqueness, alert hygiene gates, store-driven context.

Time control uses freezegun with wall-clock times only (tz_offset=0), so the
tests are independent of the machine timezone: the evaluator derives every
cutoff from the same frozen local clock. All persisted datetimes are
normalized to UTC by the engine.
"""

from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest
from freezegun import freeze_time
from sqlalchemy import select

from healthmes.config import Settings
from healthmes.engine.rules import (
    RecoverySnapshot,
    StressSnapshot,
    TriggerContext,
    TriggerFire,
    deadline_risk,
    low_recovery_heavy_afternoon,
    schedule_changed,
    stress_spike_vs_baseline,
)
from healthmes.engine.triggers import (
    HealthSignals,
    OwHealthReader,
    TriggerEvaluator,
    _normalize_recovery,
    default_now_provider,
    is_in_quiet_hours,
)
from healthmes.engine.webhook import WebhookResult
from healthmes.store.enums import CalendarSource, ProposalStatus
from healthmes.store.models import CalendarEventMirror, ScheduleProposal, Task, TriggerEvent


def local_now() -> datetime:
    """The evaluator's own clock convention (frozen by freezegun)."""
    return datetime.now().astimezone()


class TestDefaultNowProvider:
    """Alert hygiene must run on the *user's* clock (docs/PLAN.md §11).

    Docker containers run UTC while compose forwards HEALTHMES_TIMEZONE —
    evaluating quiet hours / daily budget / afternoon window on the server
    clock would suppress alerts through the user's working day and push at
    2-3am local.
    """

    def test_uses_settings_timezone(self, settings) -> None:
        import zoneinfo

        seoul = settings.model_copy(update={"timezone": "Asia/Seoul"})
        now = default_now_provider(seoul)()
        assert now.tzinfo == zoneinfo.ZoneInfo("Asia/Seoul")

    def test_evaluator_default_clock_is_settings_timezone(self, settings) -> None:
        import zoneinfo

        seoul = settings.model_copy(update={"timezone": "Asia/Seoul"})
        evaluator = TriggerEvaluator(seoul, health_reader=FakeHealthReader())
        assert evaluator._now().tzinfo == zoneinfo.ZoneInfo("Asia/Seoul")

    def test_quiet_hours_evaluated_in_user_timezone(self, settings) -> None:
        """02:30 UTC = 11:30 KST — inside the UTC-clock quiet window but the
        middle of the user's working day; alerts must NOT be suppressed."""
        import zoneinfo

        seoul = settings.model_copy(update={"timezone": "Asia/Seoul"})
        with freeze_time("2026-07-09 02:30:00"):  # UTC instant
            now = default_now_provider(seoul)()
            assert now.tzinfo == zoneinfo.ZoneInfo("Asia/Seoul")
            assert now.hour == 11  # 11:30 KST
            assert not is_in_quiet_hours(
                now.time(), seoul.quiet_hours_start, seoul.quiet_hours_end
            )

    def test_unset_timezone_falls_back_to_machine_local(self, settings) -> None:
        machine = settings.model_copy(update={"timezone": None})
        now = default_now_provider(machine)()
        assert now.tzinfo is not None  # aware, machine-local

    def test_invalid_timezone_raises_loudly(self, settings) -> None:
        import zoneinfo

        broken = settings.model_copy(update={"timezone": "Mars/Olympus_Mons"})
        with pytest.raises(zoneinfo.ZoneInfoNotFoundError):
            default_now_provider(broken)


def utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC)


class FakeAlertSender:
    """Recording AlertSender double; outcome switchable per test."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[tuple[TriggerFire, datetime]] = []

    def send(self, fire: TriggerFire, *, fired_at: datetime) -> WebhookResult:
        self.sent.append((fire, fired_at))
        if self.ok:
            return WebhookResult(ok=True, status_code=202)
        return WebhookResult(ok=False, status_code=502, detail="gateway unavailable")


class FakeHealthReader:
    """HealthReader double returning canned signals."""

    def __init__(self, signals: HealthSignals | None = None) -> None:
        self.signals = signals if signals is not None else HealthSignals()

    def read(self, now: datetime) -> HealthSignals:
        return self.signals


@pytest.fixture
def alert_sender() -> FakeAlertSender:
    return FakeAlertSender()


class CountingRule:
    """Rule stub that fires every sweep with a fresh dedup key."""

    __name__ = "counting_rule"

    def __init__(self, rule_id: str = "counting_rule") -> None:
        self.rule_id = rule_id
        self.calls = 0

    def __call__(self, ctx: TriggerContext) -> TriggerFire:
        self.calls += 1
        return TriggerFire(
            rule_id=self.rule_id,
            dedup_key=f"{self.rule_id}:{self.calls}",
            summary="observation",
            proposal="proposal",
            evidence={"call": self.calls},
        )


def fixed_rule(ctx: TriggerContext) -> TriggerFire:
    """Rule stub with a constant dedup key (same logical occurrence)."""
    return TriggerFire(
        rule_id="fixed_rule",
        dedup_key="fixed_rule:occurrence-1",
        summary="observation",
        proposal="proposal",
        evidence={},
    )


def make_evaluator(
    settings: Settings,
    session_factory,
    sender: FakeAlertSender,
    *,
    rules,
    reader: Any | None = None,
) -> TriggerEvaluator:
    return TriggerEvaluator(
        settings,
        session_factory=session_factory,
        health_reader=reader if reader is not None else FakeHealthReader(),
        alert_sender=sender,
        rules=rules,
        # Pin the evaluator to the same frozen machine-local clock the test
        # data is built with (the default provider follows Settings.timezone,
        # covered by TestDefaultNowProvider).
        now_provider=local_now,
    )


def all_events(session_factory) -> list[TriggerEvent]:
    with session_factory() as session:
        return session.scalars(select(TriggerEvent).order_by(TriggerEvent.fired_at)).all()


# ---------------------------------------------------------------------------
# Fresh fire -> persist + push; dedup_key uniqueness
# ---------------------------------------------------------------------------


def test_fresh_fire_is_persisted_and_pushed(settings, session_factory, alert_sender) -> None:
    with freeze_time("2026-07-09 14:00:00"):
        now = local_now()
        reader = FakeHealthReader(
            HealthSignals(
                stress=StressSnapshot(
                    recent_value=85.0,
                    recent_at=utc(now - timedelta(minutes=20)),
                    baseline_median=55.0,
                    baseline_days=10,
                )
            )
        )
        evaluator = make_evaluator(
            settings,
            session_factory,
            alert_sender,
            rules=(stress_spike_vs_baseline,),
            reader=reader,
        )
        report = evaluator.evaluate_once()

    assert [outcome.status for outcome in report.outcomes] == ["pushed"]
    assert len(alert_sender.sent) == 1
    fire, fired_at = alert_sender.sent[0]
    assert fire.rule_id == "stress_spike_vs_baseline"
    assert fired_at.tzinfo is not None

    [event] = all_events(session_factory)
    assert event.rule_id == "stress_spike_vs_baseline"
    assert event.dedup_key == "stress_spike_vs_baseline:2026-07-09"
    assert event.alert_sent is True
    assert event.payload["summary"] == fire.summary
    assert event.payload["evidence"]["recent_value"] == 85.0
    assert event.payload["push"] == {"sent": True, "status_code": 202}


def test_dedup_key_is_unique_across_sweeps(settings, session_factory, alert_sender) -> None:
    with freeze_time("2026-07-09 14:00:00") as frozen:
        evaluator = make_evaluator(
            settings, session_factory, alert_sender, rules=(fixed_rule,)
        )
        first = evaluator.evaluate_once()
        frozen.tick(timedelta(minutes=90))  # well past the cooldown
        second = evaluator.evaluate_once()

    assert [o.status for o in first.outcomes] == ["pushed"]
    assert [o.status for o in second.outcomes] == ["deduplicated"]
    assert len(alert_sender.sent) == 1
    assert len(all_events(session_factory)) == 1  # uniqueness: no second row


def test_failed_push_is_recorded_and_not_retried(settings, session_factory) -> None:
    sender = FakeAlertSender(ok=False)
    with freeze_time("2026-07-09 14:00:00") as frozen:
        evaluator = make_evaluator(settings, session_factory, sender, rules=(fixed_rule,))
        first = evaluator.evaluate_once()
        frozen.tick(timedelta(minutes=10))
        second = evaluator.evaluate_once()

    assert [o.status for o in first.outcomes] == ["push_failed"]
    assert [o.status for o in second.outcomes] == ["deduplicated"]
    assert len(sender.sent) == 1
    [event] = all_events(session_factory)
    assert event.alert_sent is False
    assert event.payload["push"]["suppressed_reason"] == "push_failed"
    assert event.payload["push"]["status_code"] == 502


# ---------------------------------------------------------------------------
# Alert hygiene: cooldown / daily budget / quiet hours (freezegun-driven)
# ---------------------------------------------------------------------------


def test_cooldown_suppresses_same_rule_then_releases(
    settings, session_factory, alert_sender
) -> None:
    rule = CountingRule()
    with freeze_time("2026-07-09 10:00:00") as frozen:
        evaluator = make_evaluator(settings, session_factory, alert_sender, rules=(rule,))
        first = evaluator.evaluate_once()
        frozen.tick(timedelta(minutes=10))  # inside the 60-minute cooldown
        second = evaluator.evaluate_once()
        frozen.tick(timedelta(minutes=51))  # past the cooldown window
        third = evaluator.evaluate_once()

    assert [o.status for o in first.outcomes] == ["pushed"]
    assert [o.status for o in second.outcomes] == ["suppressed"]
    assert second.outcomes[0].reason == "cooldown"
    assert [o.status for o in third.outcomes] == ["pushed"]
    assert len(alert_sender.sent) == 2

    events = all_events(session_factory)
    assert len(events) == 3  # suppressed firings are recorded, not pushed
    assert [e.alert_sent for e in events] == [True, False, True]
    assert events[1].payload["push"]["suppressed_reason"] == "cooldown"


def test_cooldown_is_per_rule(settings, session_factory, alert_sender) -> None:
    rule_a = CountingRule("rule_a")
    rule_b = CountingRule("rule_b")
    with freeze_time("2026-07-09 10:00:00") as frozen:
        evaluator = make_evaluator(settings, session_factory, alert_sender, rules=(rule_a,))
        assert [o.status for o in evaluator.evaluate_once().outcomes] == ["pushed"]
        frozen.tick(timedelta(minutes=5))
        # A different rule is not blocked by rule_a's cooldown.
        other = make_evaluator(settings, session_factory, alert_sender, rules=(rule_b,))
        assert [o.status for o in other.evaluate_once().outcomes] == ["pushed"]


def test_daily_budget_suppresses_and_resets_next_day(settings, session_factory) -> None:
    tight = settings.model_copy(
        update={"alert_daily_budget": 1, "alert_cooldown_minutes": 0}
    )
    sender = FakeAlertSender()
    rule = CountingRule()
    with freeze_time("2026-07-09 10:00:00") as frozen:
        evaluator = make_evaluator(tight, session_factory, sender, rules=(rule,))
        first = evaluator.evaluate_once()
        frozen.tick(timedelta(minutes=10))
        second = evaluator.evaluate_once()
        frozen.move_to("2026-07-10 10:00:00")
        third = evaluator.evaluate_once()

    assert [o.status for o in first.outcomes] == ["pushed"]
    assert [o.status for o in second.outcomes] == ["suppressed"]
    assert second.outcomes[0].reason == "daily_budget"
    assert [o.status for o in third.outcomes] == ["pushed"]
    assert len(sender.sent) == 2


def test_zero_budget_records_but_never_pushes(settings, session_factory) -> None:
    muted = settings.model_copy(update={"alert_daily_budget": 0})
    sender = FakeAlertSender()
    with freeze_time("2026-07-09 10:00:00"):
        evaluator = make_evaluator(muted, session_factory, sender, rules=(fixed_rule,))
        report = evaluator.evaluate_once()
    assert [o.status for o in report.outcomes] == ["suppressed"]
    assert sender.sent == []
    assert len(all_events(session_factory)) == 1


def test_quiet_hours_suppress_pushes(settings, session_factory) -> None:
    sender = FakeAlertSender()
    rule = CountingRule()
    with freeze_time("2026-07-09 23:00:00") as frozen:  # inside 22:30-07:00
        evaluator = make_evaluator(settings, session_factory, sender, rules=(rule,))
        late_night = evaluator.evaluate_once()
        frozen.move_to("2026-07-10 06:30:00")  # still inside (wraps midnight)
        early = evaluator.evaluate_once()
        frozen.move_to("2026-07-10 12:00:00")  # outside
        midday = evaluator.evaluate_once()

    assert [o.status for o in late_night.outcomes] == ["suppressed"]
    assert late_night.outcomes[0].reason == "quiet_hours"
    assert [o.status for o in early.outcomes] == ["suppressed"]
    assert early.outcomes[0].reason == "quiet_hours"
    assert [o.status for o in midday.outcomes] == ["pushed"]
    assert len(sender.sent) == 1


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (time(23, 0), True),
        (time(3, 0), True),
        (time(6, 59), True),
        (time(7, 0), False),
        (time(22, 30), True),
        (time(22, 29), False),
        (time(12, 0), False),
    ],
)
def test_is_in_quiet_hours_wrapping_window(moment: time, expected: bool) -> None:
    assert is_in_quiet_hours(moment, time(22, 30), time(7, 0)) is expected


def test_is_in_quiet_hours_plain_and_disabled_windows() -> None:
    assert is_in_quiet_hours(time(13, 30), time(13, 0), time(14, 0)) is True
    assert is_in_quiet_hours(time(14, 0), time(13, 0), time(14, 0)) is False
    # start == end -> zero-length window -> disabled.
    assert is_in_quiet_hours(time(13, 0), time(13, 0), time(13, 0)) is False


# ---------------------------------------------------------------------------
# Store-driven context: schedule_changed and deadline_risk end-to-end
# ---------------------------------------------------------------------------


def test_schedule_changed_fires_from_calendar_mirror_diff(
    settings, session_factory, alert_sender
) -> None:
    with freeze_time("2026-07-09 14:00:00") as frozen:
        now = local_now()
        with session_factory() as session:
            task = Task(title="Write report", est_minutes=120)
            session.add(task)
            session.flush()
            session.add(
                ScheduleProposal(
                    task_id=task.id,
                    proposed_start=utc(now + timedelta(hours=4)),
                    proposed_end=utc(now + timedelta(hours=5)),
                    status=ProposalStatus.ACCEPTED,
                )
            )
            # Agent block the user moved externally 5 minutes ago.
            session.add(
                CalendarEventMirror(
                    external_id="agent-1",
                    calendar_source=CalendarSource.GOOGLE,
                    summary="Deep work",
                    start_at=utc(now + timedelta(hours=2)),
                    end_at=utc(now + timedelta(hours=3)),
                    is_agent_created=True,
                    etag="etag-2",
                    created_at=utc(now - timedelta(days=2)),
                    updated_at=utc(now - timedelta(minutes=5)),
                )
            )
            # New external event colliding with the accepted proposal.
            session.add(
                CalendarEventMirror(
                    external_id="ext-9",
                    calendar_source=CalendarSource.CALDAV,
                    summary="Dentist",
                    start_at=utc(now + timedelta(hours=4)),
                    end_at=utc(now + timedelta(hours=4, minutes=30)),
                    is_agent_created=False,
                    created_at=utc(now - timedelta(minutes=3)),
                    updated_at=utc(now - timedelta(minutes=3)),
                )
            )
            session.commit()

        evaluator = make_evaluator(
            settings, session_factory, alert_sender, rules=(schedule_changed,)
        )
        report = evaluator.evaluate_once()

        assert [o.status for o in report.outcomes] == ["pushed"]
        fire = report.outcomes[0].fire
        changes = {c["external_id"]: c for c in fire.evidence["changes"]}
        assert changes["agent-1"]["is_agent_created"] is True
        assert changes["agent-1"]["kind"] == "moved"
        assert changes["ext-9"]["kind"] == "created"
        assert changes["ext-9"]["conflicts"] == ["proposal: Write report"]

        # Same diff five minutes later: same dedup key -> dropped.
        frozen.tick(timedelta(minutes=5))
        again = evaluator.evaluate_once()
        assert [o.status for o in again.outcomes] == ["deduplicated"]

        # Once the rows age out of the sync-diff lookback there is no fire.
        frozen.tick(timedelta(minutes=30))
        silent = evaluator.evaluate_once()
        assert silent.outcomes == ()

    assert len(all_events(session_factory)) == 1


def test_untouched_external_events_do_not_fire(settings, session_factory, alert_sender) -> None:
    with freeze_time("2026-07-09 14:00:00"):
        now = local_now()
        with session_factory() as session:
            # Fresh external event that conflicts with nothing (e.g. initial
            # sync import) must not alert.
            session.add(
                CalendarEventMirror(
                    external_id="ext-1",
                    calendar_source=CalendarSource.GOOGLE,
                    summary="Lunch",
                    start_at=utc(now + timedelta(hours=1)),
                    end_at=utc(now + timedelta(hours=2)),
                    is_agent_created=False,
                    created_at=utc(now - timedelta(minutes=2)),
                    updated_at=utc(now - timedelta(minutes=2)),
                )
            )
            session.commit()
        evaluator = make_evaluator(
            settings, session_factory, alert_sender, rules=(schedule_changed,)
        )
        assert evaluator.evaluate_once().outcomes == ()
        assert alert_sender.sent == []


def test_deadline_risk_fires_until_task_is_covered(
    settings, session_factory, alert_sender
) -> None:
    with freeze_time("2026-07-09 10:00:00") as frozen:
        now = local_now()
        with session_factory() as session:
            task = Task(
                title="Ship report",
                est_minutes=120,
                deadline=utc(now + timedelta(hours=24)),
            )
            session.add(task)
            session.flush()
            task_id = task.id
            session.add(
                ScheduleProposal(
                    task_id=task_id,
                    proposed_start=utc(now + timedelta(hours=2)),
                    proposed_end=utc(now + timedelta(hours=3)),
                    status=ProposalStatus.ACCEPTED,
                )
            )
            session.commit()

        evaluator = make_evaluator(settings, session_factory, alert_sender, rules=(deadline_risk,))
        report = evaluator.evaluate_once()
        assert [o.status for o in report.outcomes] == ["pushed"]
        [task_evidence] = report.outcomes[0].fire.evidence["tasks"]
        assert task_evidence["est_minutes"] == 120
        assert task_evidence["scheduled_minutes"] == 60
        assert task_evidence["shortfall_minutes"] == 60

        # Cover the estimate with another accepted block -> rule goes quiet.
        with session_factory() as session:
            session.add(
                ScheduleProposal(
                    task_id=task_id,
                    proposed_start=utc(now + timedelta(hours=4)),
                    proposed_end=utc(now + timedelta(hours=6)),
                    status=ProposalStatus.PUSHED,
                )
            )
            session.commit()
        frozen.tick(timedelta(minutes=10))
        assert evaluator.evaluate_once().outcomes == ()


def test_deadline_blocks_after_deadline_do_not_count(
    settings, session_factory, alert_sender
) -> None:
    with freeze_time("2026-07-09 10:00:00"):
        now = local_now()
        with session_factory() as session:
            task = Task(
                title="Prep demo",
                est_minutes=60,
                deadline=utc(now + timedelta(hours=6)),
            )
            session.add(task)
            session.flush()
            # 120 accepted minutes, but scheduled entirely after the deadline.
            session.add(
                ScheduleProposal(
                    task_id=task.id,
                    proposed_start=utc(now + timedelta(hours=8)),
                    proposed_end=utc(now + timedelta(hours=10)),
                    status=ProposalStatus.ACCEPTED,
                )
            )
            session.commit()
        evaluator = make_evaluator(settings, session_factory, alert_sender, rules=(deadline_risk,))
        report = evaluator.evaluate_once()
        assert [o.status for o in report.outcomes] == ["pushed"]
        [task_evidence] = report.outcomes[0].fire.evidence["tasks"]
        assert task_evidence["scheduled_minutes"] == 0


def test_low_recovery_heavy_afternoon_uses_remaining_afternoon_load(
    settings, session_factory, alert_sender
) -> None:
    with freeze_time("2026-07-09 12:30:00"):
        now = local_now()
        with session_factory() as session:
            half_hour = timedelta(minutes=30)
            events = [
                # Overlaps the remaining window 12:30-18:00 by 30 minutes.
                ("Standup overrun", now - timedelta(hours=1, minutes=30), now + half_hour),
                ("Design review", now + half_hour, now + timedelta(hours=2, minutes=30)),
                ("Customer call", now + timedelta(hours=2, minutes=30), now + timedelta(hours=4)),
            ]
            for index, (summary, start, end) in enumerate(events):
                session.add(
                    CalendarEventMirror(
                        external_id=f"evt-{index}",
                        calendar_source=CalendarSource.GOOGLE,
                        summary=summary,
                        start_at=utc(start),
                        end_at=utc(end),
                    )
                )
            session.commit()

        reader = FakeHealthReader(
            HealthSignals(
                recovery=RecoverySnapshot(
                    value=30.0, category="body_battery", provider="garmin", recorded_at=utc(now)
                )
            )
        )
        evaluator = make_evaluator(
            settings,
            session_factory,
            alert_sender,
            rules=(low_recovery_heavy_afternoon,),
            reader=reader,
        )
        report = evaluator.evaluate_once()

    assert [o.status for o in report.outcomes] == ["pushed"]
    fire = report.outcomes[0].fire
    assert fire.evidence["afternoon_busy_minutes"] == 30 + 120 + 90
    assert fire.evidence["afternoon_event_count"] == 3


# ---------------------------------------------------------------------------
# OwHealthReader: signals through the (fake) mcp_server ow_client
# ---------------------------------------------------------------------------


NOW_UTC = datetime(2026, 7, 9, 14, 0, tzinfo=UTC)


def score_row(recorded_at: datetime, value: float, provider: str = "garmin") -> dict[str, Any]:
    return {"recorded_at": recorded_at.isoformat(), "value": value, "provider": provider}


class FakeOwClient:
    """Sync stand-in for healthmes.mcp_server.ow_client.OWClient.

    Response envelopes mirror the real routes: /users returns
    OldPaginatedResponse (``items``), /health-scores returns
    PaginatedResponse (``data``).
    """

    def __init__(self, scores_by_category: dict[str, list[dict[str, Any]]]) -> None:
        self.scores_by_category = scores_by_category
        self.calls: list[tuple[str, str | None]] = []

    def list_users(self, **kwargs: Any) -> dict[str, Any]:
        return {"items": [{"id": "user-1", "name": "Test User"}], "total": 1}

    def get_health_scores(
        self,
        user_id: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        category: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append((user_id, category))
        return {"data": self.scores_by_category.get(category or "", [])}


class AsyncFakeOwClient(FakeOwClient):
    """Async variant matching the real OWClient (vendor api_client style)."""

    async def list_users(self, **kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
        return super().list_users(**kwargs)

    async def get_health_scores(  # type: ignore[override]
        self, *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        return super().get_health_scores(*args, **kwargs)


def stress_history() -> list[dict[str, Any]]:
    rows = [score_row(NOW_UTC - timedelta(minutes=30), 85.0)]  # recent spike
    rows.append(score_row(NOW_UTC - timedelta(hours=5), 80.0))  # today but stale
    for day in range(1, 8):  # 7 baseline days, daily mean 50
        rows.append(score_row(NOW_UTC - timedelta(days=day, hours=1), 45.0))
        rows.append(score_row(NOW_UTC - timedelta(days=day, hours=2), 55.0))
    return rows


def test_reader_builds_stress_snapshot(settings) -> None:
    client = FakeOwClient({"stress": stress_history()})
    signals = OwHealthReader(settings, client=client).read(NOW_UTC)

    stress = signals.stress
    assert stress is not None
    assert stress.recent_value == 85.0
    assert stress.recent_at == NOW_UTC - timedelta(minutes=30)
    assert stress.baseline_median == 50.0
    assert stress.baseline_days == 7
    # user id resolved once via get_users and reused for every category.
    assert {call[0] for call in client.calls} == {"user-1"}


def test_reader_requires_recent_sample_and_baseline(settings) -> None:
    stale_only = [score_row(NOW_UTC - timedelta(hours=5), 90.0)]
    signals = OwHealthReader(settings, client=FakeOwClient({"stress": stale_only})).read(NOW_UTC)
    assert signals.stress is None

    no_baseline = [score_row(NOW_UTC - timedelta(minutes=10), 90.0)]
    signals = OwHealthReader(settings, client=FakeOwClient({"stress": no_baseline})).read(NOW_UTC)
    assert signals.stress is None


def test_reader_prefers_body_battery_then_falls_back(settings) -> None:
    both = FakeOwClient(
        {
            "body_battery": [score_row(NOW_UTC - timedelta(hours=1), 55.0)],
            "readiness": [score_row(NOW_UTC - timedelta(hours=1), 8.0, provider="polar")],
        }
    )
    recovery = OwHealthReader(settings, client=both).read(NOW_UTC).recovery
    assert recovery is not None
    assert recovery.category == "body_battery"
    assert recovery.value == 55.0

    readiness_only = FakeOwClient(
        {"readiness": [score_row(NOW_UTC - timedelta(hours=1), 8.0, provider="polar")]}
    )
    recovery = OwHealthReader(settings, client=readiness_only).read(NOW_UTC).recovery
    assert recovery is not None
    assert recovery.category == "readiness"
    assert recovery.value == 80.0  # polar readiness 0-10 -> 0-100


def test_reader_supports_async_vendor_style_client(settings) -> None:
    client = AsyncFakeOwClient({"stress": stress_history()})
    signals = OwHealthReader(settings, client=client).read(NOW_UTC)
    assert signals.stress is not None
    assert signals.stress.recent_value == 85.0


def test_reader_degrades_to_empty_signals_on_failure(settings) -> None:
    class ExplodingClient:
        def list_users(self, **kwargs: Any) -> None:
            raise RuntimeError("backend down")

    signals = OwHealthReader(settings, client=ExplodingClient()).read(NOW_UTC)
    assert signals == HealthSignals()

    # Without an injected client the lazy default is the real OWClient
    # pointed at the unreachable test base URL - the connect failure must
    # degrade the same way, never raise (and never require network).
    signals = OwHealthReader(settings).read(NOW_UTC)
    assert signals == HealthSignals()


def test_reader_honors_configured_ow_user_id(settings) -> None:
    """Settings.ow_user_id pins the subject — discovery never runs.

    The trigger sweep previously ignored the configured id and read users[0],
    which on a multi-user backend could fire an alert on someone else's data.
    """

    class NoDiscoveryClient(FakeOwClient):
        def list_users(self, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("discovery must not run when ow_user_id is configured")

    pinned = settings.model_copy(update={"ow_user_id": "pinned-user"})
    client = NoDiscoveryClient({"stress": stress_history()})
    signals = OwHealthReader(pinned, client=client).read(NOW_UTC)

    assert signals.stress is not None
    assert {call[0] for call in client.calls} == {"pinned-user"}


def test_reader_degrades_when_multiple_users_and_no_pin(settings) -> None:
    """Two users + no configured id: degrade (empty signals) rather than
    silently alerting on the newest user's data (users[0] is created_at desc
    in the vendor route)."""

    class TwoUserClient(FakeOwClient):
        def list_users(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "items": [{"id": "partner-2"}, {"id": "user-1"}],
                "total": 2,
            }

    client = TwoUserClient({"stress": stress_history()})
    signals = OwHealthReader(settings, client=client).read(NOW_UTC)

    assert signals == HealthSignals()
    assert client.calls == []  # never queried anyone's scores


@pytest.mark.parametrize(
    ("category", "provider", "value", "expected"),
    [
        ("body_battery", "garmin", 55.0, 55.0),
        ("recovery", "whoop", 67.0, 67.0),
        ("recovery", "polar", 1.0, 0.0),
        ("recovery", "polar", 6.0, 100.0),
        ("recovery", "polar", 3.5, 50.0),
        ("readiness", "polar", 8.0, 80.0),
        ("readiness", "oura", 72.0, 72.0),
        ("recovery", "whoop", 140.0, 100.0),  # clamped
    ],
)
def test_normalize_recovery_scales(category, provider, value, expected) -> None:
    # Scales per vendor HEALTH_SCORE_RANGES (backend/app/constants/health_scores.py).
    assert _normalize_recovery(category, provider, value) == pytest.approx(expected)
