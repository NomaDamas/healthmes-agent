"""Medical-record viewing endpoints (docs/PLAN.md §2 ``medical_record``, §8).

Local viewing ONLY: there are no REST write endpoints — records are created
exclusively through the ``create_medical_record`` MCP tool (the Telegram
capture skill), which attaches the capture-time health-context snapshot.

Privacy contract (docs/PLAN.md §8/§9): medical data never leaves this
machine. These endpoints exist for the local browser / manual inspection on
localhost; media stays on disk under ``HEALTHMES_DATA_DIR`` with only its
path stored, and nothing here is pushed to any external channel.
"""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Select, select

from healthmes.api.common import UTCDateTime
from healthmes.api.errors import not_found
from healthmes.api.pagination import Page, PageParamsDep, paginate
from healthmes.store import MedicalRecord, MedicalRecordKind
from healthmes.store.session import SessionDep

router = APIRouter(prefix="/v1/medical-records", tags=["medical"])


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
