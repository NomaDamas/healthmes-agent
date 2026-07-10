"""Food-log capture endpoints (docs/PLAN.md §2 ``food_log``, §8).

The Telegram capture skill calls the MCP tool ``log_food``; this REST surface
is the same write path for direct integrations and the list view for weekly
reviews. ``description`` is the (LLM-generated) text; media stays on disk
under ``HEALTHMES_DATA_DIR/media/`` and only its relative path is stored.
"""

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from healthmes.api.common import UTCDateTime, utc_now
from healthmes.api.pagination import Page, PageParamsDep, paginate
from healthmes.store import FoodLog
from healthmes.store.session import SessionDep

router = APIRouter(prefix="/v1/food-logs", tags=["food"])

MealType = Literal["breakfast", "lunch", "dinner", "snack"]


class FoodLogCreate(BaseModel):
    """Request body for logging a meal/snack."""

    description: str = Field(min_length=1, max_length=4000)
    logged_at: UTCDateTime | None = Field(
        default=None, description="Capture time; defaults to now (UTC)."
    )
    media_path: str | None = Field(
        default=None,
        max_length=500,
        description="Path of the captured photo, relative to HEALTHMES_DATA_DIR/media/.",
    )
    meal_type: MealType | None = None
    source: str | None = Field(
        default=None,
        max_length=32,
        description="Capture channel, e.g. 'telegram' or 'api'.",
    )


class FoodLogOut(BaseModel):
    """Food log entry as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    logged_at: datetime
    description: str
    media_path: str | None
    meal_type: str | None
    source: str | None


@router.post("", status_code=status.HTTP_201_CREATED)
def create_food_log(body: FoodLogCreate, session: SessionDep) -> FoodLogOut:
    """Record one food log entry."""
    entry = FoodLog(
        logged_at=body.logged_at or utc_now(),
        description=body.description,
        media_path=body.media_path,
        meal_type=body.meal_type,
        source=body.source,
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    return FoodLogOut.model_validate(entry)


@router.get("")
def list_food_logs(
    session: SessionDep,
    page: PageParamsDep,
    start: UTCDateTime | None = None,
    end: UTCDateTime | None = None,
    meal_type: MealType | None = None,
) -> Page[FoodLogOut]:
    """List food logs, newest first (optional ``logged_at`` range + meal type)."""
    stmt = select(FoodLog).order_by(FoodLog.logged_at.desc(), FoodLog.created_at.desc())
    if start is not None:
        stmt = stmt.where(FoodLog.logged_at >= start)
    if end is not None:
        stmt = stmt.where(FoodLog.logged_at < end)
    if meal_type is not None:
        stmt = stmt.where(FoodLog.meal_type == meal_type)
    rows, meta = paginate(session, stmt, page)
    return Page(data=[FoodLogOut.model_validate(row) for row in rows], pagination=meta)
