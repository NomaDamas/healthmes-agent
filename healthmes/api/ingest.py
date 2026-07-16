"""Raw-first ingest receiver (docs/PLAN.md §13).

``POST /v1/ingest/healthkit`` is the continuous-collection bridge: point any
HealthKit auto-export app (Health Auto Export et al.) at it and the phone
pushes health data on a schedule — no HealthMes app code involved. The body
is stored verbatim first (that alone makes the request a success), then
best-effort mapped and forwarded into open-wearables so the energy loop sees
it. ``POST /v1/ingest/raw`` accepts anything from any future source.

Bearer auth comes from the global /v1 middleware (healthmes/api/auth.py).
Responses are 202 whenever the raw payload is durable — parse and forward
outcomes are reported in the body, never as request failures.
"""

import json
import logging
from typing import Literal

import anyio.to_thread

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from healthmes.ingest import (
    IngestForwardError,
    forward_sdk_sync,
    store_raw,
    transform_hae,
)
from healthmes.store.session import SessionDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/ingest", tags=["ingest"])


class IngestAck(BaseModel):
    """What happened to one accepted payload (raw storage is the contract)."""

    raw_id: str
    sha256: str
    size_bytes: int
    parse_status: Literal["parsed", "stored_unparsed"]
    forward_status: str
    records_forwarded: int


async def _read_capped_body(request: Request) -> bytes:
    """Read the body without ever buffering more than the cap.

    The Content-Length fast-path rejects declared oversizes before reading;
    the streaming loop bounds chunked (or lying) senders.
    """
    settings = request.app.state.settings
    limit = settings.ingest_max_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail=f"payload exceeds {limit} bytes")
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > limit:
            raise HTTPException(status_code=413, detail=f"payload exceeds {limit} bytes")
    if not chunks:
        raise HTTPException(status_code=400, detail="empty body")
    return bytes(chunks)


@router.post("/healthkit", status_code=202)
async def ingest_healthkit(request: Request, session: SessionDep) -> IngestAck:
    """Store a HealthKit bridge push verbatim, then map+forward best-effort."""
    settings = request.app.state.settings
    body = await _read_capped_body(request)

    event = store_raw(
        settings,
        source="healthkit-bridge",
        content_type=request.headers.get("content-type"),
        body=body,
    )
    # Raw-first durability: index row committed BEFORE any interpretation —
    # a crash below leaves a findable row, never an orphaned file.
    session.add(event)
    session.commit()

    payload = None
    try:
        payload = json.loads(body)
        event.parse_status = "parsed"
    except (json.JSONDecodeError, UnicodeDecodeError):
        event.parse_status = "stored_unparsed"

    records: list[dict] = transform_hae(payload) if payload is not None else []
    user_id = (settings.ow_user_id or "").strip()
    if not records:
        event.forward_status = "nothing_mapped"
    elif not user_id:
        event.forward_status = "skipped_no_user"
    else:
        transport = getattr(request.app.state, "ingest_transport", None)
        try:
            # Thread pool: the sync HTTP client must not stall the event loop.
            await anyio.to_thread.run_sync(
                lambda: forward_sdk_sync(
                    settings, records, user_id=user_id, transport=transport
                )
            )
            # "queued": open-wearables ack'd (202) and parses asynchronously —
            # not a claim that the records are already normalized.
            event.forward_status = "queued"
            event.records_forwarded = len(records)
        except IngestForwardError as exc:
            # Raw is durable; the forward can be replayed from it later.
            event.forward_status = "forward_failed"
            event.forward_detail = str(exc)[:255]
            logger.warning("ingest forward failed (raw kept at %s): %s", event.path, exc)

    session.commit()
    return IngestAck(
        raw_id=str(event.id),
        sha256=event.sha256,
        size_bytes=event.size_bytes,
        parse_status=event.parse_status,  # type: ignore[arg-type]
        forward_status=event.forward_status,
        records_forwarded=event.records_forwarded,
    )


@router.post("/raw", status_code=202)
async def ingest_raw(
    request: Request,
    session: SessionDep,
    source: str = Query(
        default="unknown",
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
        description="Slug naming the sender (e.g. 'garmin-csv', 'sleep-diary').",
    ),
) -> IngestAck:
    """Store any payload verbatim — no parsing, no forwarding, never rejected."""
    settings = request.app.state.settings
    body = await _read_capped_body(request)
    event = store_raw(
        settings,
        source=source,
        content_type=request.headers.get("content-type"),
        body=body,
    )
    event.parse_status = "stored_unparsed"
    event.forward_status = "not_applicable"
    session.add(event)
    session.commit()
    return IngestAck(
        raw_id=str(event.id),
        sha256=event.sha256,
        size_bytes=event.size_bytes,
        parse_status="stored_unparsed",
        forward_status=event.forward_status,
        records_forwarded=0,
    )
