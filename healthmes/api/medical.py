"""Medical-record endpoints (docs/PLAN.md §2 ``medical_record``, §8).

Two write paths share one contract: the ``create_medical_record`` MCP tool
(the Telegram capture skill) and — since issue #10 — ``POST`` here for the
native companion apps' camera / voice-memo capture shortcuts. Both attach the
same capture-time health-context snapshot server-side (the REST handler
reuses the MCP module's helper, so the semantics cannot drift), and both
degrade the snapshot honestly instead of ever failing the capture.

Privacy contract (docs/PLAN.md §8/§9): medical data never leaves this
machine. The whole surface sits behind the bearer gate, apps talk only to the
user's own paired instance, media stays on disk under ``HEALTHMES_DATA_DIR``
with only its path stored (upload via ``POST /v1/media`` first), and nothing
here is pushed to any external channel.
"""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Select, select

from healthmes.api.common import UTCDateTime
from healthmes.api.errors import not_found
from healthmes.api.pagination import Page, PageParamsDep, paginate
from healthmes.store import MedicalRecord, MedicalRecordKind
from healthmes.store.session import SessionDep

router = APIRouter(prefix="/v1/medical-records", tags=["medical"])


class MedicalRecordCreate(BaseModel):
    """Request body for a native capture (issue #10 camera / voice-memo shortcuts)."""

    kind: MedicalRecordKind
    description: str = Field(
        min_length=1,
        max_length=4000,
        description="Structured text derived from the photo/voice note (what is "
        "legible/stated — never guessed drug names, never diagnosis).",
    )
    media_path: str | None = Field(
        default=None,
        max_length=500,
        description="Path of the stored photo/voice file relative to "
        "HEALTHMES_DATA_DIR, as returned by POST /v1/media (never raw bytes).",
    )
    transcript: str | None = Field(
        default=None,
        description="Voice-note transcript when the capture was spoken.",
    )
    context: dict[str, Any] | None = Field(
        default=None,
        description="Capture metadata (e.g. {'source': 'ios-app-photo'}), stored "
        "under the record's 'capture' context key. Never pass health data — the "
        "server attaches its own deterministic snapshot under 'health'.",
    )

    @field_validator("description")
    @classmethod
    def _description_not_blank(cls, value: str) -> str:
        # Same rule as the MCP tool: a whitespace-only capture is junk data.
        if not value.strip():
            raise ValueError("description must not be blank")
        return value


class MedicalRecordOut(BaseModel):
    """Medical record as returned by the detail endpoint (full context)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: MedicalRecordKind
    description: str
    media_path: str | None
    transcript: str | None
    context: dict[str, Any] | None
    created_at: datetime


class MedicalRecordSummaryOut(BaseModel):
    """List-view projection (``context`` omitted — the snapshot can be large)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: MedicalRecordKind
    description: str
    media_path: str | None
    transcript: str | None
    created_at: datetime


def _list_stmt(kind: MedicalRecordKind | None) -> Select:
    """Newest-first medical-record select (id tiebreak keeps pagination stable)."""
    stmt = select(MedicalRecord).order_by(
        MedicalRecord.created_at.desc(), MedicalRecord.id.desc()
    )
    if kind is not None:
        stmt = stmt.where(MedicalRecord.kind == kind)
    return stmt


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_medical_record(body: MedicalRecordCreate, session: SessionDep) -> MedicalRecordOut:
    """Persist a medical-lite capture with the capture-time health snapshot.

    The REST twin of the ``create_medical_record`` MCP tool: the server
    attaches the same deterministic health-context snapshot for today under
    the record's ``health`` context key, degrading to ``{"status":
    "unavailable"}`` when open-wearables cannot be reached — the capture
    itself never fails for infrastructure reasons.
    """
    # Deliberate reuse of the MCP module's snapshot helper — the single
    # source of the snapshot + degradation semantics, so REST and Telegram
    # captures cannot drift apart. Function-local import mirrors server.py's
    # viewer_url import: the api and mcp_server layers stay off each other's
    # module import paths (healthmes/mcp_server/server.py::record_decision).
    from healthmes.mcp_server import server as mcp_server

    snapshot = await mcp_server._capture_health_context()
    stored_context: dict[str, Any] = {mcp_server.MEDICAL_HEALTH_CONTEXT_KEY: snapshot}
    if body.context is not None:
        stored_context[mcp_server.MEDICAL_CAPTURE_CONTEXT_KEY] = body.context
    record = MedicalRecord(
        kind=body.kind,
        description=body.description,
        media_path=body.media_path,
        transcript=body.transcript,
        context=stored_context,
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return MedicalRecordOut.model_validate(record)


@router.get("")
def list_medical_records(
    session: SessionDep,
    page: PageParamsDep,
    kind: MedicalRecordKind | None = None,
    start: UTCDateTime | None = None,
    end: UTCDateTime | None = None,
) -> Page[MedicalRecordSummaryOut]:
    """List medical records, newest first (optional kind + ``created_at`` range)."""
    stmt = _list_stmt(kind)
    if start is not None:
        stmt = stmt.where(MedicalRecord.created_at >= start)
    if end is not None:
        stmt = stmt.where(MedicalRecord.created_at < end)
    rows, meta = paginate(session, stmt, page)
    return Page(
        data=[MedicalRecordSummaryOut.model_validate(row) for row in rows],
        pagination=meta,
    )


@router.get("/{record_id}")
def get_medical_record(record_id: uuid.UUID, session: SessionDep) -> MedicalRecordOut:
    """One medical record with its full capture-time context snapshot."""
    record = session.get(MedicalRecord, record_id)
    if record is None:
        raise not_found("medical_record", record_id)
    return MedicalRecordOut.model_validate(record)
