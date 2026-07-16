"""Domain models and persistence for the dedicated healthmes database.

SQLAlchemy models + Alembic migrations live here (conventions follow
``vendor/open-wearables/backend/app/models/``). See docs/PLAN.md section 2.

Importing this package registers every model on ``Base.metadata`` (Alembic's
``env.py`` relies on that).
"""

from healthmes.store.base import JSONB, Base, string_enum
from healthmes.store.enums import (
    TASK_DONE_STATUSES,
    TASK_STATUSES,
    CalendarSource,
    DecisionKind,
    EnergyDemand,
    MedicalRecordKind,
    ProposalStatus,
    TaskSource,
)
from healthmes.store.models import (
    AppUsageSample,
    CalendarEventMirror,
    CognitiveEnergyEstimate,
    DecisionRecord,
    FoodLog,
    Insight,
    MedicalRecord,
    RawIngestEvent,
    ScheduleProposal,
    Task,
    TriggerEvent,
    WeeklyGoal,
)
from healthmes.store.session import (
    SessionDep,
    create_db_engine,
    dispose_engine,
    get_engine,
    get_session,
    get_session_factory,
    init_engine,
    session_scope,
)

__all__ = [
    # base
    "Base",
    "JSONB",
    "string_enum",
    # enums
    "CalendarSource",
    "DecisionKind",
    "EnergyDemand",
    "MedicalRecordKind",
    "ProposalStatus",
    "TaskSource",
    "TASK_DONE_STATUSES",
    "TASK_STATUSES",
    # models
    "AppUsageSample",
    "RawIngestEvent",
    "CalendarEventMirror",
    "CognitiveEnergyEstimate",
    "DecisionRecord",
    "FoodLog",
    "Insight",
    "MedicalRecord",
    "ScheduleProposal",
    "Task",
    "TriggerEvent",
    "WeeklyGoal",
    # session
    "SessionDep",
    "create_db_engine",
    "dispose_engine",
    "get_engine",
    "get_session",
    "get_session_factory",
    "init_engine",
    "session_scope",
]
