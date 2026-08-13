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
    CalendarMutationOperation,
    CalendarMutationStatus,
    CalendarSource,
    DecisionKind,
    EnergyDemand,
    MedicalRecordKind,
    ProposalStatus,
    SleepProposalStatus,
    TaskSource,
)
from healthmes.store.models import (
    AppUsageSample,
    CalendarEventMirror,
    CalendarMutationProposal,
    CognitiveEnergyEstimate,
    DecisionDomainPolicy,
    DecisionRecord,
    FoodLog,
    Insight,
    MedicalRecord,
    MonthlyGoal,
    PurgeJob,
    RawIngestEvent,
    RetentionPolicy,
    ScheduleProposal,
    StorageObject,
    StorageUsageDaily,
    Task,
    TriggerEvent,
    WeeklyGoal,
    WellnessEvent,
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
from healthmes.store.sleep_models import SleepReconciliationProposal

__all__ = [
    # base
    "Base",
    "JSONB",
    "string_enum",
    # enums
    "CalendarSource",
    "CalendarMutationOperation",
    "CalendarMutationStatus",
    "DecisionKind",
    "EnergyDemand",
    "MedicalRecordKind",
    "ProposalStatus",
    "SleepProposalStatus",
    "TaskSource",
    "TASK_DONE_STATUSES",
    "TASK_STATUSES",
    # models
    "AppUsageSample",
    "MonthlyGoal",
    "RawIngestEvent",
    "RetentionPolicy",
    "StorageObject",
    "StorageUsageDaily",
    "PurgeJob",
    "WellnessEvent",
    "CalendarEventMirror",
    "CalendarMutationProposal",
    "CognitiveEnergyEstimate",
    "DecisionDomainPolicy",
    "DecisionRecord",
    "FoodLog",
    "Insight",
    "MedicalRecord",
    "ScheduleProposal",
    "SleepReconciliationProposal",
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
