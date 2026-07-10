"""Limit/offset pagination shared by every list endpoint.

Response shape loosely follows open-wearables'
``app/schemas/utils/pagination.py`` (``data`` + ``pagination`` keys) but uses
plain limit/offset (max 200) instead of cursors — the healthmes tables are
small and single-user.
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

__all__ = ["MAX_PAGE_SIZE", "PageParams", "PageParamsDep", "PageMeta", "Page", "paginate"]

MAX_PAGE_SIZE = 200


@dataclass(frozen=True)
class PageParams:
    """Validated limit/offset pair extracted from query params."""

    limit: int
    offset: int


def _page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageParams:
    return PageParams(limit=limit, offset=offset)


PageParamsDep = Annotated[PageParams, Depends(_page_params)]


class PageMeta(BaseModel):
    """Pagination block of a list response."""

    total_count: int
    limit: int
    offset: int
    has_more: bool


class Page[ItemT](BaseModel):
    """Generic list envelope: ``{"data": [...], "pagination": {...}}``."""

    data: list[ItemT]
    pagination: PageMeta


def paginate(session: Session, stmt: Select, params: PageParams) -> tuple[list, PageMeta]:
    """Run ``stmt`` with limit/offset and compute the pagination metadata.

    ``stmt`` must be a complete (filtered + ordered) select of ORM entities;
    the count subquery drops the ordering.
    """
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total_count = session.scalar(count_stmt) or 0
    rows = list(session.scalars(stmt.limit(params.limit).offset(params.offset)).all())
    meta = PageMeta(
        total_count=total_count,
        limit=params.limit,
        offset=params.offset,
        has_more=params.offset + len(rows) < total_count,
    )
    return rows, meta
