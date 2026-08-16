from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from healthmes.engine.briefings import (
    SCHEDULED_BRIEFING_SPECS,
    build_scheduled_briefing_fire,
    build_scheduled_briefing_job,
)
from healthmes.engine.decision_dispatch import DecisionDispatchResult
from healthmes.store import TriggerEvent

NOW = datetime(2026, 8, 16, 8, tzinfo=UTC)


class RecordingDecisionSender:
    requires_reasoning = True

    def __init__(self) -> None:
        self.fires = []

    def send(self, fire, *, fired_at, trigger_event_id):
        self.fires.append((fire, fired_at, trigger_event_id))
        return DecisionDispatchResult(
            ok=True,
            status_code=200,
            ready_for_native=True,
            channel="native",
            message=f"Completed {fire.rule_id}.",
        )


def test_daily_and_weekly_dedup_periods_are_local() -> None:
    timezone = ZoneInfo("Asia/Seoul")
    morning, _, weekly = SCHEDULED_BRIEFING_SPECS

    daily = build_scheduled_briefing_fire(
        morning,
        fired_at=NOW,
        timezone=timezone,
    )
    weekly_fire = build_scheduled_briefing_fire(
        weekly,
        fired_at=NOW,
        timezone=timezone,
    )

    assert daily.dedup_key == "scheduled_briefing.morning:2026-08-16"
    assert weekly_fire.dedup_key == "scheduled_briefing.weekly:2026-W33"
    assert daily.evidence["timezone"] == "Asia/Seoul"
    assert set(daily.evidence) == {
        "briefing_kind",
        "scheduled_local_time",
        "timezone",
    }


def test_scheduled_job_uses_durable_trigger_outbox_once(
    settings,
    session_factory,
) -> None:
    sender = RecordingDecisionSender()
    job = build_scheduled_briefing_job(
        settings,
        spec=SCHEDULED_BRIEFING_SPECS[0],
        alert_sender=sender,
        now_provider=lambda: NOW,
        session_factory=session_factory,
    )

    job()
    job()

    assert len(sender.fires) == 1
    with session_factory() as session:
        events = session.scalars(select(TriggerEvent)).all()
        assert len(events) == 1
        assert events[0].rule_id == "scheduled_briefing.morning"
        assert events[0].payload["summary"].startswith(
            "Prepare the user's morning wellness briefing"
        )
