"""Weekly-goal CRUD (docs/PLAN.md §2 ``weekly_goal``).

Goals are the user's week-level intents; the planner skill decomposes them
into tasks. Route conventions follow
``vendor/open-wearables/backend/app/api/routes/v1/`` (sync handlers, typed
query params); handlers commit explicitly (``get_session`` never auto-commits).
"""

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from healthmes.api.errors import not_found
from healthmes.api.pagination import Page, PageParamsDep, paginate
from healthmes.store import WeeklyGoal
from healthmes.store.session import SessionDep

router = APIRouter(prefix="/v1/goals", tags=["goals"])

GoalStatus = Literal["active", "done", "dropped"]


class GoalCreate(BaseModel):
    """Request body for creating a weekly goal."""

    week_start: date = Field(description="Monday (or chosen anchor day) of the goal's week.")
    title: str = Field(min_length=1, max_length=500)
    priority: int = Field(default=0, ge=0, le=10)
    status: GoalStatus = "active"


class GoalUpdate(BaseModel):
    """Partial update; only provided fields are changed."""

    week_start: date | None = None
    title: str | None = Field(default=None, min_length=1, max_length=500)
    priority: int | None = Field(default=None, ge=0, le=10)
    status: GoalStatus | None = None


class GoalOut(BaseModel):
    """Weekly goal as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    week_start: date
    title: str
    priority: int
    status: str


def _get_goal_or_404(session: SessionDep, goal_id: uuid.UUID) -> WeeklyGoal:
    goal = session.get(WeeklyGoal, goal_id)
    if goal is None:
        raise not_found("weekly_goal", goal_id)
    return goal


@router.post("", status_code=status.HTTP_201_CREATED)
def create_goal(body: GoalCreate, session: SessionDep) -> GoalOut:
    """Create a weekly goal."""
    goal = WeeklyGoal(**body.model_dump())
    session.add(goal)
    session.commit()
    session.refresh(goal)
    return GoalOut.model_validate(goal)


@router.get("")
def list_goals(
    session: SessionDep,
    page: PageParamsDep,
    week_start: date | None = None,
    status_filter: Annotated[GoalStatus | None, Query(alias="status")] = None,
) -> Page[GoalOut]:
    """List goals, newest week first (filters: ``week_start``, ``status``)."""
    stmt = select(WeeklyGoal).order_by(
        WeeklyGoal.week_start.desc(), WeeklyGoal.priority.desc(), WeeklyGoal.created_at
    )
    if week_start is not None:
        stmt = stmt.where(WeeklyGoal.week_start == week_start)
    if status_filter is not None:
        stmt = stmt.where(WeeklyGoal.status == status_filter)
    rows, meta = paginate(session, stmt, page)
    return Page(data=[GoalOut.model_validate(row) for row in rows], pagination=meta)


@router.get("/{goal_id}")
def get_goal(goal_id: uuid.UUID, session: SessionDep) -> GoalOut:
    """Fetch one goal by id."""
    return GoalOut.model_validate(_get_goal_or_404(session, goal_id))


@router.patch("/{goal_id}")
def update_goal(goal_id: uuid.UUID, body: GoalUpdate, session: SessionDep) -> GoalOut:
    """Partially update a goal."""
    goal = _get_goal_or_404(session, goal_id)
    # No weekly_goal column is nullable, so explicit nulls are ignored.
    for field, value in body.model_dump(exclude_unset=True, exclude_none=True).items():
        setattr(goal, field, value)
    session.commit()
    session.refresh(goal)
    return GoalOut.model_validate(goal)


@router.delete("/{goal_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_goal(goal_id: uuid.UUID, session: SessionDep) -> None:
    """Delete a goal (tasks keep existing with ``goal_id`` set NULL by the FK)."""
    goal = _get_goal_or_404(session, goal_id)
    session.delete(goal)
    session.commit()
