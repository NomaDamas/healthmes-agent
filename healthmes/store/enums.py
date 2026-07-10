"""Str-valued enums for the healthmes domain models (docs/PLAN.md §2).

Convention follows ``vendor/open-wearables/backend/app/schemas/enums/`` (e.g.
``health_score_category.py``): plain :class:`enum.StrEnum` classes whose values
are the strings persisted in the database.
"""

from enum import StrEnum


class EnergyDemand(StrEnum):
    """Cognitive-energy demand of a task (drives schedule placement)."""

    LOW = "low"
    MED = "med"
    HIGH = "high"


class TaskSource(StrEnum):
    """Who created a task: the user directly or the agent (e.g. goal decomposition)."""

    USER = "user"
    AGENT = "agent"


# Task.status vocabulary — single source of truth for BOTH write surfaces of
# the task table: the REST state machine (healthmes/api/tasks.py, which
# additionally constrains transitions) and the MCP tools
# (healthmes/mcp_server/server.py). The column itself is a free str_32;
# "scheduled" marks a task whose block was placed via the propose-then-confirm
# gate. Kept as a frozenset (not a StrEnum) because the surfaces validate raw
# strings.
TASK_STATUSES = frozenset({"todo", "scheduled", "in_progress", "done", "cancelled"})

# Terminal statuses, hidden by default from open-task listings.
TASK_DONE_STATUSES = frozenset({"done", "cancelled"})


class CalendarSource(StrEnum):
    """External calendar backend a mirrored event comes from (docs/PLAN.md §6)."""

    GOOGLE = "google"
    CALDAV = "caldav"


class ProposalStatus(StrEnum):
    """Lifecycle of a schedule proposal (propose-then-confirm gate)."""

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    PUSHED = "pushed"
    DECLINED = "declined"


class DecisionKind(StrEnum):
    """What kind of agent decision a decision_record explains (docs/PLAN.md §5)."""

    SCHEDULE_CHANGE = "schedule_change"
    ALERT = "alert"
    INSIGHT = "insight"
    CAPTURE = "capture"


class MedicalRecordKind(StrEnum):
    """Medical-lite capture category (docs/PLAN.md §8)."""

    MEDICATION = "medication"
    SYMPTOM = "symptom"
