"""Task CRUD + explicit status transitions (docs/PLAN.md §2 ``task``).

Tasks always start at ``todo``; status changes go through
``POST /v1/tasks/{id}/status`` so the transition rules stay authoritative
(``PATCH`` deliberately cannot touch ``status``). Disallowed transitions
answer 409 with code ``invalid_transition``.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from starlette.status import HTTP_422_UNPROCESSABLE_CONTENT

from healthmes.api.common import UTCDateTime
from healthmes.api.errors import APIError, invalid_transition, not_found
from healthmes.api.pagination import Page, PageParamsDep, paginate
from healthmes.store import TASK_STATUSES, EnergyDemand, Task, TaskSource, WeeklyGoal
from healthmes.store.session import SessionDep

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])

# The Literal mirrors the store's shared TASK_STATUSES vocabulary (also
# enforced by the MCP task tools) — Literal for OpenAPI, guarded below.
TaskStatus = Literal["todo", "scheduled", "in_progress", "done", "cancelled"]

# state -> allowed next states (same-state "transitions" are rejected too).
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "todo": frozenset({"scheduled", "in_progress", "done", "cancelled"}),
    "scheduled": frozenset({"todo", "in_progress", "done", "cancelled"}),
    "in_progress": frozenset({"todo", "done", "cancelled"}),
    "done": frozenset({"todo"}),  # reopen
    "cancelled": frozenset({"todo"}),  # reopen
}

# The two write surfaces of the task table must never drift apart again.
assert set(ALLOWED_TRANSITIONS) == TASK_STATUSES


class TaskCreate(BaseModel):
    """Request body for creating a task (always created in status ``todo``)."""

    title: str = Field(min_length=1, max_length=500)
    goal_id: uuid.UUID | None = None
    est_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    deadline: UTCDateTime | None = None
    energy_demand: EnergyDemand = EnergyDemand.MED
    source: TaskSource = TaskSource.USER


class TaskUpdate(BaseModel):
    """Partial update. ``goal_id``/``est_minutes``/``deadline`` may be cleared
    with explicit ``null``; ``status`` is excluded on purpose (use the status
    endpoint)."""

    title: str | None = Field(default=None, min_length=1, max_length=500)
    goal_id: uuid.UUID | None = None
    est_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    deadline: UTCDateTime | None = None
    energy_demand: EnergyDemand | None = None


class TaskStatusChange(BaseModel):
    """Request body of the status-transition endpoint."""

    status: TaskStatus


class TaskOut(BaseModel):
    """Task as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    goal_id: uuid.UUID | None
    est_minutes: int | None
    deadline: datetime | None
    energy_demand: EnergyDemand
    status: str
    source: TaskSource


def _get_task_or_404(session: SessionDep, task_id: uuid.UUID) -> Task:
    task = session.get(Task, task_id)
    if task is None:
        raise not_found("task", task_id)
    return task


def _check_goal_reference(session: SessionDep, goal_id: uuid.UUID | None) -> None:
    if goal_id is not None and session.get(WeeklyGoal, goal_id) is None:
        raise APIError(
            HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_reference",
            f"weekly_goal {goal_id} does not exist",
        )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_task(body: TaskCreate, session: SessionDep) -> TaskOut:
    """Create a task in status ``todo``."""
    _check_goal_reference(session, body.goal_id)
    task = Task(**body.model_dump())
    session.add(task)
    session.commit()
    session.refresh(task)
    return TaskOut.model_validate(task)


@router.get("")
def list_tasks(
    session: SessionDep,
    page: PageParamsDep,
    status_filter: Annotated[TaskStatus | None, Query(alias="status")] = None,
    goal_id: uuid.UUID | None = None,
    source: TaskSource | None = None,
    energy_demand: EnergyDemand | None = None,
    due_before: UTCDateTime | None = None,
) -> Page[TaskOut]:
    """List tasks ordered by deadline (nulls last), then creation time."""
    stmt = select(Task).order_by(Task.deadline.asc().nulls_last(), Task.created_at)
    if status_filter is not None:
        stmt = stmt.where(Task.status == status_filter)
    if goal_id is not None:
        stmt = stmt.where(Task.goal_id == goal_id)
    if source is not None:
        stmt = stmt.where(Task.source == source)
    if energy_demand is not None:
        stmt = stmt.where(Task.energy_demand == energy_demand)
    if due_before is not None:
        stmt = stmt.where(Task.deadline.is_not(None), Task.deadline < due_before)
    rows, meta = paginate(session, stmt, page)
    return Page(data=[TaskOut.model_validate(row) for row in rows], pagination=meta)


@router.get("/{task_id}")
def get_task(task_id: uuid.UUID, session: SessionDep) -> TaskOut:
    """Fetch one task by id."""
    return TaskOut.model_validate(_get_task_or_404(session, task_id))


@router.patch("/{task_id}")
def update_task(task_id: uuid.UUID, body: TaskUpdate, session: SessionDep) -> TaskOut:
    """Partially update a task (not its status)."""
    task = _get_task_or_404(session, task_id)
    changes = body.model_dump(exclude_unset=True)
    if "goal_id" in changes:
        _check_goal_reference(session, changes["goal_id"])
    for field, value in changes.items():
        if field in ("title", "energy_demand") and value is None:
            continue  # non-nullable columns: explicit null is ignored
        setattr(task, field, value)
    session.commit()
    session.refresh(task)
    return TaskOut.model_validate(task)


@router.post("/{task_id}/status")
def change_task_status(task_id: uuid.UUID, body: TaskStatusChange, session: SessionDep) -> TaskOut:
    """Apply a status transition; 409 ``invalid_transition`` when disallowed."""
    task = _get_task_or_404(session, task_id)
    allowed = ALLOWED_TRANSITIONS.get(task.status, frozenset())
    if body.status not in allowed:
        raise invalid_transition("task", task.status, body.status)
    task.status = body.status
    session.commit()
    session.refresh(task)
    return TaskOut.model_validate(task)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(task_id: uuid.UUID, session: SessionDep) -> None:
    """Delete a task (its proposals cascade; mirrored events keep NULL task id)."""
    task = _get_task_or_404(session, task_id)
    session.delete(task)
    session.commit()
