"""Round-trip and constraint tests for all 11 domain models on sqlite.

Datetimes are naive UTC constants: sqlite has no timezone storage, so aware
values would come back naive and break exact equality.
"""

import uuid
from datetime import date, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from healthmes.store import (
    AppUsageSample,
    CalendarEventMirror,
    CalendarSource,
    CognitiveEnergyEstimate,
    DecisionKind,
    DecisionRecord,
    EnergyDemand,
    FoodLog,
    Insight,
    MedicalRecord,
    MedicalRecordKind,
    ProposalStatus,
    ScheduleProposal,
    Task,
    TaskSource,
    TriggerEvent,
    WeeklyGoal,
)

MONDAY = date(2026, 7, 6)
T0 = datetime(2026, 7, 6, 9, 0, 0)
T1 = datetime(2026, 7, 6, 10, 30, 0)


def _roundtrip(session, instance):
    """Persist ``instance`` and return it freshly loaded from the database.

    ``expire_all`` (rather than expunge) keeps previously returned instances
    usable while still forcing every subsequent attribute access through a
    real SELECT.
    """
    session.add(instance)
    session.commit()
    instance_id = instance.id
    session.expire_all()
    loaded = session.get(type(instance), instance_id)
    assert loaded is not None
    return loaded


class TestBaseColumns:
    def test_id_created_updated_populate(self, session):
        goal = _roundtrip(session, WeeklyGoal(week_start=MONDAY, title="ship the store"))
        assert isinstance(goal.id, uuid.UUID)
        assert isinstance(goal.created_at, datetime)
        assert isinstance(goal.updated_at, datetime)

    def test_ids_are_unique_per_row(self, session):
        a = _roundtrip(session, WeeklyGoal(week_start=MONDAY, title="a"))
        b = _roundtrip(session, WeeklyGoal(week_start=MONDAY, title="b"))
        assert a.id != b.id

    def test_id_column_is_first_and_timestamps_last(self):
        names = [column.name for column in Task.__table__.columns]
        assert names[0] == "id"
        assert names[-2:] == ["created_at", "updated_at"]


class TestWeeklyGoal:
    def test_roundtrip(self, session):
        goal = _roundtrip(
            session,
            WeeklyGoal(week_start=MONDAY, title="finish thesis chapter", priority=2),
        )
        assert goal.week_start == MONDAY
        assert goal.title == "finish thesis chapter"
        assert goal.priority == 2
        assert goal.status == "active"  # default


class TestTask:
    def test_roundtrip(self, session):
        goal = _roundtrip(session, WeeklyGoal(week_start=MONDAY, title="goal"))
        task = _roundtrip(
            session,
            Task(
                title="write draft",
                goal_id=goal.id,
                est_minutes=90,
                deadline=T1,
                energy_demand=EnergyDemand.HIGH,
                status="todo",
                source=TaskSource.AGENT,
            ),
        )
        assert task.title == "write draft"
        assert task.goal_id == goal.id
        assert task.est_minutes == 90
        assert task.deadline == T1
        assert task.energy_demand is EnergyDemand.HIGH
        assert task.source is TaskSource.AGENT

    def test_defaults(self, session):
        task = _roundtrip(session, Task(title="minimal"))
        assert task.goal_id is None
        assert task.est_minutes is None
        assert task.deadline is None
        assert task.energy_demand is EnergyDemand.MED
        assert task.status == "todo"
        assert task.source is TaskSource.USER

    def test_goal_delete_sets_goal_id_null(self, session):
        goal_id = _roundtrip(session, WeeklyGoal(week_start=MONDAY, title="goal")).id
        task_id = _roundtrip(session, Task(title="orphan-to-be", goal_id=goal_id)).id
        session.delete(session.get(WeeklyGoal, goal_id))
        session.commit()
        session.expire_all()
        assert session.get(Task, task_id).goal_id is None


class TestCalendarEventMirror:
    def test_roundtrip(self, session):
        task = _roundtrip(session, Task(title="focus block"))
        mirror = _roundtrip(
            session,
            CalendarEventMirror(
                external_id="evt-123",
                calendar_source=CalendarSource.GOOGLE,
                summary="Deep work",
                start_at=T0,
                end_at=T1,
                is_agent_created=True,
                agent_task_id=task.id,
                etag='"etag-1"',
                sync_token="sync-token-9",
            ),
        )
        assert mirror.external_id == "evt-123"
        assert mirror.calendar_source is CalendarSource.GOOGLE
        assert mirror.summary == "Deep work"
        assert mirror.start_at == T0
        assert mirror.end_at == T1
        assert mirror.is_agent_created is True
        assert mirror.agent_task_id == task.id
        assert mirror.etag == '"etag-1"'
        assert mirror.sync_token == "sync-token-9"

    def test_source_external_id_unique(self, session):
        session.add(
            CalendarEventMirror(
                external_id="evt-dup",
                calendar_source=CalendarSource.CALDAV,
                start_at=T0,
                end_at=T1,
            )
        )
        session.commit()
        session.add(
            CalendarEventMirror(
                external_id="evt-dup",
                calendar_source=CalendarSource.CALDAV,
                start_at=T0,
                end_at=T1,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    def test_same_external_id_allowed_across_sources(self, session):
        for source in (CalendarSource.GOOGLE, CalendarSource.CALDAV):
            session.add(
                CalendarEventMirror(
                    external_id="evt-shared",
                    calendar_source=source,
                    start_at=T0,
                    end_at=T1,
                )
            )
        session.commit()

    def test_task_delete_sets_agent_task_id_null(self, session):
        task_id = _roundtrip(session, Task(title="block")).id
        mirror_id = _roundtrip(
            session,
            CalendarEventMirror(
                external_id="evt-1",
                calendar_source=CalendarSource.GOOGLE,
                start_at=T0,
                end_at=T1,
                is_agent_created=True,
                agent_task_id=task_id,
            ),
        ).id
        session.delete(session.get(Task, task_id))
        session.commit()
        session.expire_all()
        assert session.get(CalendarEventMirror, mirror_id).agent_task_id is None


class TestScheduleProposal:
    def test_roundtrip(self, session):
        task = _roundtrip(session, Task(title="t"))
        record = _roundtrip(
            session,
            DecisionRecord(kind=DecisionKind.SCHEDULE_CHANGE, tree={"id": "root"}, summary="s"),
        )
        proposal = _roundtrip(
            session,
            ScheduleProposal(
                task_id=task.id,
                proposed_start=T0,
                proposed_end=T1,
                status=ProposalStatus.ACCEPTED,
                decision_record_id=record.id,
            ),
        )
        assert proposal.task_id == task.id
        assert proposal.proposed_start == T0
        assert proposal.proposed_end == T1
        assert proposal.status is ProposalStatus.ACCEPTED
        assert proposal.decision_record_id == record.id

    def test_status_defaults_to_proposed(self, session):
        task = _roundtrip(session, Task(title="t"))
        proposal = _roundtrip(
            session,
            ScheduleProposal(task_id=task.id, proposed_start=T0, proposed_end=T1),
        )
        assert proposal.status is ProposalStatus.PROPOSED

    def test_requires_existing_task(self, session):
        session.add(ScheduleProposal(task_id=uuid.uuid4(), proposed_start=T0, proposed_end=T1))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    def test_task_delete_cascades(self, session):
        task_id = _roundtrip(session, Task(title="t")).id
        proposal_id = _roundtrip(
            session,
            ScheduleProposal(task_id=task_id, proposed_start=T0, proposed_end=T1),
        ).id
        session.delete(session.get(Task, task_id))
        session.commit()
        session.expunge_all()
        assert session.get(ScheduleProposal, proposal_id) is None


class TestFoodLog:
    def test_roundtrip(self, session):
        log = _roundtrip(
            session,
            FoodLog(
                logged_at=T0,
                description="Bibimbap with extra vegetables, ~650 kcal",
                media_path="media/food/2026-07-06-lunch.jpg",
                meal_type="lunch",
                source="telegram",
            ),
        )
        assert log.logged_at == T0
        assert log.description.startswith("Bibimbap")
        assert log.media_path == "media/food/2026-07-06-lunch.jpg"
        assert log.meal_type == "lunch"
        assert log.source == "telegram"


class TestAppUsageSample:
    def test_roundtrip(self, session):
        sample = _roundtrip(
            session,
            AppUsageSample(
                device_id="pixel-8",
                bucket_start=T0,
                app_package="com.slack",
                foreground_seconds=540,
                launches=9,
                category="communication",
            ),
        )
        assert sample.device_id == "pixel-8"
        assert sample.bucket_start == T0
        assert sample.app_package == "com.slack"
        assert sample.foreground_seconds == 540
        assert sample.launches == 9
        assert sample.category == "communication"

    def test_bucket_unique_per_device_and_app(self, session):
        session.add(AppUsageSample(device_id="d", bucket_start=T0, app_package="a"))
        session.commit()
        session.add(AppUsageSample(device_id="d", bucket_start=T0, app_package="a"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


class TestCognitiveEnergyEstimate:
    def test_roundtrip_with_json_components(self, session):
        components = {
            "sleep_debt_penalty": {"value": -12.0, "weight": 0.4},
            "stress_penalty": {"value": -6.5, "weight": 0.2},
            "body_battery_bonus": {"value": 4.0, "weight": 0.1},
        }
        snapshot = {"sleep_score": 78, "hrv_baseline": {"metric": "rmssd", "days": 14}}
        estimate = _roundtrip(
            session,
            CognitiveEnergyEstimate(
                window_start=T0,
                window_end=T1,
                score=72,
                components=components,
                inputs_snapshot=snapshot,
            ),
        )
        assert estimate.window_start == T0
        assert estimate.window_end == T1
        assert estimate.score == 72
        assert estimate.components == components
        assert estimate.inputs_snapshot == snapshot


class TestDecisionRecord:
    def test_roundtrip_with_recursive_tree(self, session):
        tree = {
            "id": "root",
            "type": "rule",
            "label": "stress spike vs baseline",
            "detail": {"z": 2.3},
            "children": [
                {"id": "in1", "type": "input", "label": "stress=81", "children": []},
                {"id": "act1", "type": "action", "label": "push alert", "children": []},
            ],
        }
        record = _roundtrip(
            session,
            DecisionRecord(
                kind=DecisionKind.ALERT,
                tree=tree,
                summary="Alerted user about afternoon stress spike",
                llm_model="claude-fable-5",
                tokens=1234,
            ),
        )
        assert record.kind is DecisionKind.ALERT
        assert record.tree == tree
        assert record.tree["children"][1]["label"] == "push alert"
        assert record.llm_model == "claude-fable-5"
        assert record.tokens == 1234


class TestInsight:
    def test_roundtrip(self, session):
        insight = _roundtrip(
            session,
            Insight(
                period="2026-W28",
                kind="stress_by_calendar_keyword",
                statement="Stress averages 18 points higher on days with 'standup' events.",
                evidence={"n_days": 12, "delta": 18.2},
                confidence=0.7,
            ),
        )
        assert insight.period == "2026-W28"
        assert insight.kind == "stress_by_calendar_keyword"
        assert "standup" in insight.statement
        assert insight.evidence == {"n_days": 12, "delta": 18.2}
        assert insight.confidence == pytest.approx(0.7)


class TestMedicalRecord:
    def test_roundtrip(self, session):
        record = _roundtrip(
            session,
            MedicalRecord(
                kind=MedicalRecordKind.MEDICATION,
                description="Ibuprofen 200mg, one tablet",
                media_path="media/medical/2026-07-06-pill.jpg",
                transcript="took one ibuprofen for the headache",
                context={"resting_hr": 61, "sleep_score": 74},
            ),
        )
        assert record.kind is MedicalRecordKind.MEDICATION
        assert record.description.startswith("Ibuprofen")
        assert record.media_path == "media/medical/2026-07-06-pill.jpg"
        assert record.transcript == "took one ibuprofen for the headache"
        assert record.context == {"resting_hr": 61, "sleep_score": 74}


class TestTriggerEvent:
    def test_roundtrip(self, session):
        event = _roundtrip(
            session,
            TriggerEvent(
                fired_at=T0,
                rule_id="stress_spike",
                payload={"stress": 81, "baseline": 44},
                alert_sent=True,
                dedup_key="stress_spike:2026-07-06",
            ),
        )
        assert event.fired_at == T0
        assert event.rule_id == "stress_spike"
        assert event.payload == {"stress": 81, "baseline": 44}
        assert event.alert_sent is True
        assert event.dedup_key == "stress_spike:2026-07-06"

    def test_defaults(self, session):
        event = _roundtrip(session, TriggerEvent(fired_at=T0, rule_id="deadline_risk"))
        assert event.alert_sent is False
        assert event.payload is None
        assert event.dedup_key is None

    def test_dedup_key_not_unique(self, session):
        """Same key may fire again later (cooldown is temporal, not a constraint)."""
        for _ in range(2):
            session.add(TriggerEvent(fired_at=T0, rule_id="stress_spike", dedup_key="same-key"))
        session.commit()
        count = session.scalar(
            select(func.count())
            .select_from(TriggerEvent)
            .where(TriggerEvent.dedup_key == "same-key")
        )
        assert count == 2


class TestEnumStorage:
    """Enums round-trip as enum instances but are stored as their raw string values."""

    def test_all_enum_columns_roundtrip_and_store_values(self, session):
        task = _roundtrip(
            session,
            Task(title="t", energy_demand=EnergyDemand.LOW, source=TaskSource.AGENT),
        )
        assert task.energy_demand is EnergyDemand.LOW
        assert task.source is TaskSource.AGENT

        raw = session.execute(
            text("SELECT energy_demand, source FROM task WHERE id = :id"),
            {"id": task.id.hex},
        ).one()
        assert tuple(raw) == ("low", "agent")

        mirror = _roundtrip(
            session,
            CalendarEventMirror(
                external_id="e",
                calendar_source=CalendarSource.CALDAV,
                start_at=T0,
                end_at=T1,
            ),
        )
        assert mirror.calendar_source is CalendarSource.CALDAV
        raw = session.execute(
            text("SELECT calendar_source FROM calendar_event_mirror WHERE id = :id"),
            {"id": mirror.id.hex},
        ).scalar_one()
        assert raw == "caldav"

        proposal = _roundtrip(
            session,
            ScheduleProposal(
                task_id=task.id,
                proposed_start=T0,
                proposed_end=T1,
                status=ProposalStatus.PUSHED,
            ),
        )
        assert proposal.status is ProposalStatus.PUSHED
        raw = session.execute(
            text("SELECT status FROM schedule_proposal WHERE id = :id"),
            {"id": proposal.id.hex},
        ).scalar_one()
        assert raw == "pushed"

        record = _roundtrip(
            session,
            DecisionRecord(kind=DecisionKind.SCHEDULE_CHANGE, tree={}, summary="s"),
        )
        assert record.kind is DecisionKind.SCHEDULE_CHANGE
        raw = session.execute(
            text("SELECT kind FROM decision_record WHERE id = :id"),
            {"id": record.id.hex},
        ).scalar_one()
        assert raw == "schedule_change"

        medical = _roundtrip(
            session, MedicalRecord(kind=MedicalRecordKind.SYMPTOM, description="d")
        )
        assert medical.kind is MedicalRecordKind.SYMPTOM
        raw = session.execute(
            text("SELECT kind FROM medical_record WHERE id = :id"),
            {"id": medical.id.hex},
        ).scalar_one()
        assert raw == "symptom"

    def test_enum_columns_are_varchar_not_native(self):
        column_type = Task.__table__.c.energy_demand.type
        assert column_type.native_enum is False
        assert column_type.length == 32
