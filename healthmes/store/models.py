"""Domain models for the dedicated healthmes database (docs/PLAN.md §2).

All 11 tables of the plan. Model/typing conventions follow
``vendor/open-wearables/backend/app/models/health_score.py``: ``Mapped``
annotations resolved through the base ``type_annotation_map`` / ``Annotated``
aliases, explicit ``__tablename__``, named unique constraints. Every model
additionally inherits ``id``/``created_at``/``updated_at`` from ``Base``.

Media files (photos/voice notes) live under ``HEALTHMES_DATA_DIR/media/``;
only relative paths are stored here (``media_path`` columns).
"""

import uuid
from datetime import date, datetime

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from healthmes.store.base import Base, JSONDict, str_32, str_64, str_255
from healthmes.store.enums import (
    CalendarSource,
    DecisionKind,
    EnergyDemand,
    MedicalRecordKind,
    ProposalStatus,
    TaskSource,
)

__all__ = [
    "WeeklyGoal",
    "Task",
    "CalendarEventMirror",
    "ScheduleProposal",
    "FoodLog",
    "AppUsageSample",
    "CognitiveEnergyEstimate",
    "DecisionRecord",
    "Insight",
    "MedicalRecord",
    "TriggerEvent",
]


class WeeklyGoal(Base):
    """A user-stated goal for one week; the planner decomposes it into tasks."""

    __tablename__ = "weekly_goal"

    week_start: Mapped[date] = mapped_column(index=True)
    title: Mapped[str]
    priority: Mapped[int] = mapped_column(default=0)
    status: Mapped[str_32] = mapped_column(default="active")


class Task(Base):
    """A schedulable unit of work, user-dumped or agent-derived from a goal."""

    __tablename__ = "task"

    title: Mapped[str]
    goal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("weekly_goal.id", ondelete="SET NULL"), index=True
    )
    est_minutes: Mapped[int | None]
    deadline: Mapped[datetime | None] = mapped_column(index=True)
    energy_demand: Mapped[EnergyDemand] = mapped_column(default=EnergyDemand.MED)
    status: Mapped[str_32] = mapped_column(default="todo")
    source: Mapped[TaskSource] = mapped_column(default=TaskSource.USER)


class CalendarEventMirror(Base):
    """Local mirror of an external calendar event (docs/PLAN.md §6).

    The external calendar owns every event the agent did not create; the agent
    only writes/moves its own blocks (``is_agent_created``). ``start_at`` /
    ``end_at`` map the plan's start/end (``end`` is a reserved SQL keyword).
    """

    __tablename__ = "calendar_event_mirror"
    __table_args__ = (
        UniqueConstraint(
            "calendar_source",
            "external_id",
            name="uq_calendar_event_mirror_source_external_id",
        ),
    )

    external_id: Mapped[str_255]
    calendar_source: Mapped[CalendarSource]
    summary: Mapped[str | None]
    start_at: Mapped[datetime] = mapped_column(index=True)
    end_at: Mapped[datetime]
    is_agent_created: Mapped[bool] = mapped_column(default=False)
    agent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("task.id", ondelete="SET NULL"), index=True
    )
    etag: Mapped[str_255 | None]
    sync_token: Mapped[str_255 | None]


class ScheduleProposal(Base):
    """An agent-proposed time block for a task (propose-then-confirm gate)."""

    __tablename__ = "schedule_proposal"

    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("task.id", ondelete="CASCADE"), index=True
    )
    proposed_start: Mapped[datetime]
    proposed_end: Mapped[datetime]
    status: Mapped[ProposalStatus] = mapped_column(default=ProposalStatus.PROPOSED, index=True)
    decision_record_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("decision_record.id", ondelete="SET NULL")
    )


class FoodLog(Base):
    """A captured meal/snack with an LLM-generated description (docs/PLAN.md §8)."""

    __tablename__ = "food_log"

    logged_at: Mapped[datetime] = mapped_column(index=True)
    description: Mapped[str]
    media_path: Mapped[str | None]
    meal_type: Mapped[str_32 | None]
    source: Mapped[str_32 | None]


class AppUsageSample(Base):
    """One app's foreground usage within a time bucket, from a companion device."""

    __tablename__ = "app_usage_sample"
    __table_args__ = (
        UniqueConstraint(
            "device_id",
            "bucket_start",
            "app_package",
            name="uq_app_usage_sample_device_bucket_app",
        ),
    )

    device_id: Mapped[str_64]
    bucket_start: Mapped[datetime] = mapped_column(index=True)
    app_package: Mapped[str_255]
    foreground_seconds: Mapped[int] = mapped_column(default=0)
    launches: Mapped[int] = mapped_column(default=0)
    category: Mapped[str_64 | None]


class CognitiveEnergyEstimate(Base):
    """Rule-engine cognitive-energy score for a time window (docs/PLAN.md §3).

    ``components`` records every named/weighted factor term (the decision
    tree's "considered inputs"); ``inputs_snapshot`` freezes the raw inputs
    the engine saw.
    """

    __tablename__ = "cognitive_energy_estimate"

    window_start: Mapped[datetime] = mapped_column(index=True)
    window_end: Mapped[datetime]
    score: Mapped[int]
    components: Mapped[JSONDict]
    inputs_snapshot: Mapped[JSONDict | None]


class DecisionRecord(Base):
    """Explainable record of one agent decision (docs/PLAN.md §5).

    ``tree`` holds the recursive node structure
    ``{id, type: input|rule|llm_step|option|action, label, detail, children[]}``;
    deterministic layers pre-fill input/rule nodes, the LLM appends its own.
    """

    __tablename__ = "decision_record"

    kind: Mapped[DecisionKind] = mapped_column(index=True)
    tree: Mapped[JSONDict]
    summary: Mapped[str]
    llm_model: Mapped[str_64 | None]
    tokens: Mapped[int | None]


class Insight(Base):
    """A template-derived correlation statement with its evidence and confidence."""

    __tablename__ = "insight"

    period: Mapped[str_32] = mapped_column(index=True)
    kind: Mapped[str_64]
    statement: Mapped[str]
    evidence: Mapped[JSONDict | None]
    confidence: Mapped[float | None]


class MedicalRecord(Base):
    """Medical-lite capture: medication/symptom photo or voice note (docs/PLAN.md §8)."""

    __tablename__ = "medical_record"

    kind: Mapped[MedicalRecordKind]
    description: Mapped[str]
    media_path: Mapped[str | None]
    transcript: Mapped[str | None]
    context: Mapped[JSONDict | None]


class TriggerEvent(Base):
    """A deterministic trigger-rule firing and whether it was pushed as an alert.

    Dedup is **permanent per key**: the evaluator
    (``healthmes/engine/triggers.py::TriggerEvaluator._process_fire``) never
    persists or pushes a ``dedup_key`` that was ever recorded — rules must
    embed the temporal scope in the key itself (local date, diff
    fingerprint; see ``healthmes/engine/rules.py``), so "fires again later"
    is a *new* key by construction. A static key like ``"my_rule"`` would
    fire exactly once per database lifetime. The column is indexed but not
    DB-unique only because uniqueness is enforced at the application level
    (a NULL-able key and the pre-insert existence check make a DB constraint
    unnecessary). Per-rule cooldown is separate and temporal, keyed on
    ``rule_id`` (docs/PLAN.md §4, §11).
    """

    __tablename__ = "trigger_event"

    fired_at: Mapped[datetime] = mapped_column(index=True)
    rule_id: Mapped[str_64] = mapped_column(index=True)
    payload: Mapped[JSONDict | None]
    alert_sent: Mapped[bool] = mapped_column(default=False)
    dedup_key: Mapped[str_255 | None] = mapped_column(index=True)


class RawIngestEvent(Base):
    """Append-only index of raw payloads accepted by ``POST /v1/ingest/*``.

    Raw-first principle (docs/PLAN.md §13): the verbatim payload is written
    to ``HEALTHMES_DATA_DIR/raw_ingest/`` *before* any parsing, and this row
    records where it landed and what the best-effort interpretation did.
    Unparseable or unmapped payloads are kept, never rejected — long-horizon
    unstructured data becomes interpretable as models improve. Rows are
    never updated after the ingest request finishes and never deleted by
    application code.
    """

    __tablename__ = "raw_ingest_event"

    received_at: Mapped[datetime] = mapped_column(index=True)
    source: Mapped[str_64] = mapped_column(index=True)
    content_type: Mapped[str_255 | None]
    path: Mapped[str_255]
    size_bytes: Mapped[int]
    sha256: Mapped[str_64] = mapped_column(index=True)
    parse_status: Mapped[str_32] = mapped_column(default="stored")
    forward_status: Mapped[str_32] = mapped_column(default="skipped")
    forward_detail: Mapped[str_255 | None]
    records_forwarded: Mapped[int] = mapped_column(default=0)
