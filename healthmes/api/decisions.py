"""Decision-record viewer routes (docs/PLAN.md §5, §8.5).

JSON surface (agent / tooling):

- ``GET /v1/decisions`` — paginated list, newest first (optional ``kind``).
- ``GET /v1/decisions/{id}`` — one record with its full tree (stable contract).
- ``GET /decisions/{id}.json`` — the same payload at the viewer-adjacent URL
  (append ``.json`` to any alert link).

HTML surface (every Telegram alert links ``{public_base_url}/decisions/{id}``):

- ``GET /`` — static landing shell (links only, no data).
- ``GET /decisions`` — paginated index page, newest first — the weekly-report
  entry point.
- ``GET /decisions/{id}`` — decision viewer (interactive tree + Mermaid
  flowchart), rendered by :mod:`healthmes.api.decision_html` (Jinja templates
  + escaped tree).
- ``GET /static/mermaid.min.js`` — vendored Mermaid bundle served locally
  (no CDN, local-first).

Decision records are written by the agent through the ``record_decision`` MCP
tool; there are no REST write endpoints.
"""

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Select, select
from starlette import status

from healthmes.api.decision_html import (
    render_decision_html,
    render_decision_list_html,
    render_index_html,
    render_not_found_html,
)
from healthmes.api.errors import not_found
from healthmes.api.pagination import Page, PageParamsDep, paginate
from healthmes.store import DecisionKind, DecisionRecord
from healthmes.store.session import SessionDep

router = APIRouter(tags=["decisions"])

STATIC_DIR = Path(__file__).resolve().parent / "static"
_MERMAID_ASSET = "mermaid.min.js"


class DecisionOut(BaseModel):
    """Decision record as returned by the JSON API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: DecisionKind
    summary: str
    tree: dict[str, Any]
    llm_model: str | None
    tokens: int | None
    created_at: datetime


class DecisionSummaryOut(BaseModel):
    """List-view projection of a decision record (``tree`` omitted — can be large)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: DecisionKind
    summary: str
    llm_model: str | None
    tokens: int | None
    created_at: datetime


def _list_stmt(kind: DecisionKind | None) -> Select:
    """Newest-first decision select (id tiebreak keeps pagination stable)."""
    stmt = select(DecisionRecord).order_by(
        DecisionRecord.created_at.desc(), DecisionRecord.id.desc()
    )
    if kind is not None:
        stmt = stmt.where(DecisionRecord.kind == kind)
    return stmt


def _load_decision(session: SessionDep, decision_id: uuid.UUID) -> DecisionRecord:
    record = session.get(DecisionRecord, decision_id)
    if record is None:
        raise not_found("decision_record", decision_id)
    return record


@router.get("/v1/decisions")
def list_decisions(
    session: SessionDep,
    page: PageParamsDep,
    kind: DecisionKind | None = None,
) -> Page[DecisionSummaryOut]:
    """List decision records, newest first (optional ``kind`` filter)."""
    rows, meta = paginate(session, _list_stmt(kind), page)
    return Page(data=[DecisionSummaryOut.model_validate(row) for row in rows], pagination=meta)


@router.get("/v1/decisions/{decision_id}")
def get_decision(decision_id: uuid.UUID, session: SessionDep) -> DecisionOut:
    """JSON view of one decision record's tree."""
    return DecisionOut.model_validate(_load_decision(session, decision_id))


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index_page(request: Request) -> HTMLResponse:
    """Static landing shell: product one-liner + links to the surfaces.

    Presentation only — no store reads, no credentials in the markup (safe
    under any auth posture; the shared bearer middleware applies unchanged).
    Settings feed only the seasonal backdrop (user-tz month), never tokens.
    """
    return HTMLResponse(render_index_html(settings=request.app.state.settings))


@router.get("/decisions", response_class=HTMLResponse)
def list_decisions_page(
    request: Request,
    session: SessionDep,
    page: PageParamsDep,
    kind: DecisionKind | None = None,
) -> HTMLResponse:
    """Human-facing decision index, newest first (weekly-report entry point)."""
    rows, meta = paginate(session, _list_stmt(kind), page)
    html = render_decision_list_html(
        rows,
        meta,
        kind=kind.value if kind is not None else None,
        settings=request.app.state.settings,
    )
    return HTMLResponse(html)


# Registered before ``/decisions/{decision_id}`` so the ``.json`` suffix wins
# route matching (the plain route's ``[^/]+`` param would swallow it and turn
# the request into a 422).
@router.get("/decisions/{decision_id}.json")
def get_decision_json_view(decision_id: uuid.UUID, session: SessionDep) -> DecisionOut:
    """Same payload as ``GET /v1/decisions/{id}``, reachable from any viewer link."""
    return DecisionOut.model_validate(_load_decision(session, decision_id))


@router.get("/decisions/{decision_id}", response_class=HTMLResponse)
def view_decision(
    decision_id: uuid.UUID, session: SessionDep, request: Request
) -> HTMLResponse:
    """Human-facing decision page: interactive tree + Mermaid view + detail panel."""
    settings = request.app.state.settings
    record = session.get(DecisionRecord, decision_id)
    if record is None:
        return HTMLResponse(
            render_not_found_html(str(decision_id), settings=settings),
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return HTMLResponse(render_decision_html(record, settings=settings))


@router.get("/static/mermaid.min.js", include_in_schema=False)
def get_mermaid_asset() -> FileResponse:
    """Serve the vendored Mermaid bundle (docs/PLAN.md §5: no CDN, local-first)."""
    path = STATIC_DIR / _MERMAID_ASSET
    if not path.is_file():
        raise not_found("static asset", _MERMAID_ASSET)
    return FileResponse(
        path,
        media_type="text/javascript",
        headers={"Cache-Control": "public, max-age=86400"},
    )
