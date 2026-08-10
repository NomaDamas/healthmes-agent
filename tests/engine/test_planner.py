from datetime import UTC, datetime

from sqlalchemy import select

from healthmes.engine.planner import propose_next_safe_block
from healthmes.store import (
    CalendarEventMirror,
    CalendarSource,
    CognitiveEnergyEstimate,
    DecisionRecord,
    EnergyDemand,
    ProposalStatus,
    ScheduleProposal,
    Task,
    TriggerEvent,
)


def utc(hour: int, minute: int = 0, day: int = 11) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=UTC)


def connected(settings):
    return settings.model_copy(
        update={
            "caldav_enabled": True,
            "timezone": "UTC",
        }
    )


def test_medium_task_uses_best_conflict_free_energy_slot(
    settings,
    session_factory,
) -> None:
    with session_factory() as session:
        task = Task(
            title="Write launch brief",
            est_minutes=60,
            deadline=utc(18),
            energy_demand=EnergyDemand.MED,
            status="todo",
        )
        session.add(task)
        session.add(
            CalendarEventMirror(
                external_id="busy-morning",
                calendar_source=CalendarSource.CALDAV,
                summary="Fixed meeting",
                start_at=utc(9),
                end_at=utc(11),
            )
        )
        session.add_all(
            [
                CognitiveEnergyEstimate(
                    window_start=utc(11),
                    window_end=utc(12),
                    score=52,
                    components={},
                ),
                CognitiveEnergyEstimate(
                    window_start=utc(13),
                    window_end=utc(14),
                    score=82,
                    components={},
                ),
            ]
        )
        session.commit()

        result = propose_next_safe_block(
            session,
            connected(settings),
            now=utc(8),
        )
        session.commit()

        assert result.status == "proposed"
        proposal = session.get(ScheduleProposal, result.proposal_id)
        assert proposal is not None
        assert proposal.proposed_start.replace(tzinfo=UTC) == utc(13)
        assert proposal.proposed_end.replace(tzinfo=UTC) == utc(14)
        assert proposal.status is ProposalStatus.PROPOSED
        assert proposal.reply_handle_digest
        decision = session.get(DecisionRecord, proposal.decision_record_id)
        assert decision is not None
        assert decision.llm_model is None
        assert decision.tree["detail"]["calendar_write"] is False
        trigger = session.get(TriggerEvent, decision.trigger_event_id)
        assert trigger is not None
        assert trigger.alert_sent is False
        assert trigger.payload["push"] == {"state": "pending_hygiene"}


def test_high_energy_task_fails_closed_without_complete_energy_data(
    settings,
    session_factory,
) -> None:
    with session_factory() as session:
        task = Task(
            title="Deep work",
            est_minutes=90,
            deadline=utc(18),
            energy_demand=EnergyDemand.HIGH,
            status="todo",
        )
        session.add(task)
        session.commit()

        result = propose_next_safe_block(
            session,
            connected(settings),
            now=utc(8),
        )

        assert result.status == "skipped"
        assert result.reason == "no_safe_slot"
        assert list(session.scalars(select(ScheduleProposal))) == []
        assert list(session.scalars(select(DecisionRecord))) == []


def test_low_energy_task_can_use_earliest_free_slot_without_health_score(
    settings,
    session_factory,
) -> None:
    with session_factory() as session:
        task = Task(
            title="File receipts",
            est_minutes=30,
            deadline=utc(18),
            energy_demand=EnergyDemand.LOW,
            status="todo",
        )
        session.add(task)
        session.commit()

        result = propose_next_safe_block(
            session,
            connected(settings),
            now=utc(8),
        )
        session.commit()

        proposal = session.get(ScheduleProposal, result.proposal_id)
        assert proposal is not None
        assert proposal.proposed_start.replace(tzinfo=UTC) == utc(8, 30)
        assert proposal.proposed_end.replace(tzinfo=UTC) == utc(9)


def test_existing_active_proposal_deduplicates_task(
    settings,
    session_factory,
) -> None:
    with session_factory() as session:
        task = Task(
            title="Prepare report",
            est_minutes=30,
            deadline=utc(18),
            energy_demand=EnergyDemand.LOW,
            status="todo",
        )
        session.add(task)
        session.flush()
        session.add(
            ScheduleProposal(
                task_id=task.id,
                proposed_start=utc(10),
                proposed_end=utc(10, 30),
                status=ProposalStatus.PROPOSED,
                expires_at=utc(9),
            )
        )
        session.commit()

        result = propose_next_safe_block(
            session,
            connected(settings),
            now=utc(8),
        )

        assert result.reason == "no_schedulable_task"
        assert len(list(session.scalars(select(ScheduleProposal)))) == 1


def test_planner_skips_when_no_calendar_writer_is_connected(
    settings,
    session_factory,
) -> None:
    with session_factory() as session:
        session.add(
            Task(
                title="Prepare report",
                est_minutes=30,
                deadline=utc(18),
                energy_demand=EnergyDemand.LOW,
                status="todo",
            )
        )
        session.commit()

        result = propose_next_safe_block(session, settings, now=utc(8))

        assert result.reason == "no_calendar_writer"


def test_task_after_deadline_is_not_proposed(settings, session_factory) -> None:
    with session_factory() as session:
        session.add(
            Task(
                title="Already late",
                est_minutes=30,
                deadline=utc(7),
                energy_demand=EnergyDemand.LOW,
                status="todo",
            )
        )
        session.commit()

        result = propose_next_safe_block(
            session,
            connected(settings),
            now=utc(8),
        )

        assert result.reason == "no_safe_slot"
