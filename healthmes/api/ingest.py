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
from dataclasses import dataclass
from typing import Literal

import anyio.to_thread
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.activity.locking import global_write_plane_guard
from healthmes.durable_files import (
    DurableFileIdentity,
    durable_unlink,
    verify_regular_file,
)
from healthmes.ingest import (
    IngestForwardError,
    forward_sdk_sync,
    store_raw,
    transform_hae,
)
from healthmes.storage import index_raw_ingest
from healthmes.store import RawIngestEvent, StorageObject, WellnessEvent
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
    status_persistence_uncertain: bool = False


@dataclass(frozen=True, slots=True)
class _RawIngestState:
    raw_id: object
    path: str
    sha256: str
    size_bytes: int
    parse_status: str
    forward_status: str
    forward_detail: str | None
    records_forwarded: int


@dataclass(frozen=True, slots=True)
class _HealthKitStatusUpdate:
    ack_state: _RawIngestState
    persistence_uncertain: bool


@dataclass(frozen=True, slots=True)
class _RawIngestPersistence:
    state: _RawIngestState
    persistence_uncertain: bool


def _raw_state(row: RawIngestEvent) -> _RawIngestState:
    return _RawIngestState(
        raw_id=row.id,
        path=row.path,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        parse_status=row.parse_status,
        forward_status=row.forward_status,
        forward_detail=row.forward_detail,
        records_forwarded=row.records_forwarded,
    )


def _load_raw_state(bind, raw_id) -> _RawIngestState | None:
    with Session(bind=bind) as verification:
        row = verification.get(RawIngestEvent, raw_id)
        return None if row is None else _raw_state(row)


def _ack(
    state: _RawIngestState,
    *,
    status_persistence_uncertain: bool = False,
) -> IngestAck:
    parse_status = (
        state.parse_status
        if state.parse_status in {"parsed", "stored_unparsed"}
        else "stored_unparsed"
    )
    return IngestAck(
        raw_id=str(state.raw_id),
        sha256=state.sha256,
        size_bytes=state.size_bytes,
        parse_status=parse_status,  # type: ignore[arg-type]
        forward_status=state.forward_status,
        records_forwarded=state.records_forwarded,
        status_persistence_uncertain=status_persistence_uncertain,
    )


def _healthkit_result_state(
    initial: _RawIngestState,
    *,
    parse_status: str,
    forward_status: str,
    forward_detail: str | None,
    records_forwarded: int,
) -> _RawIngestState:
    return _RawIngestState(
        raw_id=initial.raw_id,
        path=initial.path,
        sha256=initial.sha256,
        size_bytes=initial.size_bytes,
        parse_status=parse_status,
        forward_status=forward_status,
        forward_detail=forward_detail,
        records_forwarded=records_forwarded,
    )


def _healthkit_status_matches(
    persisted: _RawIngestState | None,
    expected: _RawIngestState,
) -> bool:
    return (
        persisted is not None
        and persisted.raw_id == expected.raw_id
        and persisted.parse_status == expected.parse_status
        and persisted.forward_status == expected.forward_status
        and persisted.forward_detail == expected.forward_detail
        and persisted.records_forwarded == expected.records_forwarded
    )


def _verify_raw_ingest_commit(
    bind,
    *,
    raw_id,
    storage_object_id,
    wellness_event_id,
    relative_path: str,
    destination,
    publication_identity: DurableFileIdentity,
    expected_content_type: str | None,
    expected_size: int,
    expected_sha256: str,
) -> bool | None:
    """Confirm all raw-ingest references after an ambiguous commit."""
    try:
        with Session(bind=bind) as verification:
            raw = verification.get(RawIngestEvent, raw_id)
            storage = verification.get(StorageObject, storage_object_id)
            wellness = verification.get(WellnessEvent, wellness_event_id)
            by_path = verification.scalar(
                select(StorageObject).where(StorageObject.relative_path == relative_path)
            )
    except Exception:
        logger.exception(
            "could not verify ambiguous raw-ingest commit for %s; retaining bytes",
            relative_path,
        )
        return None
    rows = (raw, storage, wellness, by_path)
    if all(row is None for row in rows):
        return False
    if (
        raw is not None
        and storage is not None
        and wellness is not None
        and by_path is not None
        and raw.path == relative_path
        and storage.id == storage_object_id
        and storage.relative_path == relative_path
        and by_path.id == storage_object_id
        and storage.data_class == "raw_payload"
        and by_path.data_class == "raw_payload"
        and storage.content_type == expected_content_type
        and by_path.content_type == expected_content_type
        and storage.size_bytes == expected_size
        and by_path.size_bytes == expected_size
        and storage.sha256 == expected_sha256
        and by_path.sha256 == expected_sha256
        and storage.purged_at is None
        and by_path.purged_at is None
        and raw.content_type == expected_content_type
        and raw.size_bytes == expected_size
        and raw.sha256 == expected_sha256
        and wellness.id == wellness_event_id
        and wellness.source_provider == raw.source
        and wellness.source_record_id == str(raw.id)
        and wellness.raw_object_id == storage.id
    ):
        try:
            verify_regular_file(
                destination,
                publication_identity,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
            )
        except OSError:
            logger.exception(
                "ambiguous raw-ingest commit references a changed file "
                "generation for %s; retaining bytes",
                relative_path,
            )
            return None
        return True
    logger.error(
        "ambiguous raw-ingest commit produced partial/conflicting references "
        "for %s; retaining bytes",
        relative_path,
    )
    return None


def _rollback_after_commit_error(session: Session, *, operation: str) -> None:
    try:
        session.rollback()
    except Exception:
        logger.exception(
            "failed to reset session after ambiguous %s commit",
            operation,
        )


def _durably_remove_uncommitted_raw(
    path,
    *,
    operation: str,
    expected: DurableFileIdentity | None = None,
) -> bool:
    try:
        durable_unlink(path, missing_ok=True, expected=expected)
    except OSError:
        logger.exception(
            "failed to durably clean up raw-ingest bytes after %s; "
            "retaining any surviving copy",
            operation,
        )
        return False
    return True


def _remove_raw_after_failed_commit(
    *,
    raw_id,
    storage_object_id,
    wellness_event_id,
    relative_path: str,
    staged,
    destination,
    publication_identity: DurableFileIdentity,
    expected_content_type: str | None,
    expected_size: int,
    expected_sha256: str,
    verification_bind,
    commit_attempted: bool,
    destination_durable: bool,
) -> bool:
    if not commit_attempted:
        destination_removed = _durably_remove_uncommitted_raw(
            destination,
            operation="pre-commit failure",
            expected=publication_identity,
        )
        if destination_removed:
            _durably_remove_uncommitted_raw(
                staged,
                operation="pre-commit failure",
                expected=publication_identity,
            )
        return False
    outcome = _verify_raw_ingest_commit(
        verification_bind,
        raw_id=raw_id,
        storage_object_id=storage_object_id,
        wellness_event_id=wellness_event_id,
        relative_path=relative_path,
        destination=destination,
        publication_identity=publication_identity,
        expected_content_type=expected_content_type,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
    )
    if outcome is False:
        logger.warning(
            "raw-ingest commit is not yet visible for %s; retaining bytes",
            relative_path,
        )
    elif outcome is None:
        logger.warning(
            "raw-ingest commit outcome is unknown for %s; retaining bytes",
            relative_path,
        )
    elif destination_durable:
        _durably_remove_uncommitted_raw(
            staged,
            operation="confirmed commit",
            expected=publication_identity,
        )
    return outcome is True


def _persist_raw_ingest(
    bind,
    settings,
    *,
    source: str,
    content_type: str | None,
    body: bytes,
) -> _RawIngestPersistence:
    """Persist raw bytes and references without using the async event loop."""
    with global_write_plane_guard(bind) as guard_connection:
        writer_bind = guard_connection if guard_connection is not None else bind
        publication = store_raw(
            settings,
            source=source,
            content_type=content_type,
            body=body,
        )
        event = publication.event
        event.parse_status = "stored_unparsed"
        event.forward_status = (
            "pending" if source == "healthkit-bridge" else "not_applicable"
        )
        commit_attempted = False
        wellness: WellnessEvent | None = None
        raw_id = None
        storage_object_id = None
        wellness_event_id = None
        committed_state: _RawIngestState | None = None
        relative_path = event.path
        expected_content_type = event.content_type
        expected_size = event.size_bytes
        expected_sha256 = event.sha256
        verify_regular_file(
            publication.destination,
            publication.identity,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )
        try:
            with Session(bind=writer_bind) as writer:
                writer.add(event)
                wellness = index_raw_ingest(writer, settings, event)
                raw_id = event.id
                storage_object_id = wellness.raw_object_id
                wellness_event_id = wellness.id
                # Build the response fallback while the row is still attached.
                # SQLAlchemy expires it after commit, and a post-commit
                # verification outage must not turn durable acceptance into 500.
                committed_state = _raw_state(event)
                verify_regular_file(
                    publication.destination,
                    publication.identity,
                    expected_size=expected_size,
                    expected_sha256=expected_sha256,
                )
                commit_attempted = True
                writer.commit()
        except BaseException:
            if (
                wellness is None
                or raw_id is None
                or storage_object_id is None
                or wellness_event_id is None
            ):
                destination_removed = _durably_remove_uncommitted_raw(
                    publication.destination,
                    operation="index registration failure",
                    expected=publication.identity,
                )
                if destination_removed:
                    _durably_remove_uncommitted_raw(
                        publication.staged,
                        operation="index registration failure",
                        expected=publication.identity,
                    )
                raise
            committed = _remove_raw_after_failed_commit(
                raw_id=raw_id,
                storage_object_id=storage_object_id,
                wellness_event_id=wellness_event_id,
                relative_path=relative_path,
                staged=publication.staged,
                destination=publication.destination,
                publication_identity=publication.identity,
                expected_content_type=expected_content_type,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
                verification_bind=writer_bind,
                commit_attempted=commit_attempted,
                destination_durable=publication.destination_durable,
            )
            if not committed:
                raise
        persistence_uncertain = not publication.destination_durable
        try:
            state = _load_raw_state(writer_bind, raw_id)
        except Exception:
            logger.exception(
                "raw-ingest commit succeeded but its index could not be "
                "reloaded for %s; returning the committed state",
                relative_path,
            )
            state = None
            persistence_uncertain = True
        if state is None:
            # ``event`` was flushed and the commit returned (or was verified
            # after an ambiguous acknowledgement), so its fields are the
            # durable fallback even when a verification read is unavailable.
            assert committed_state is not None
            state = committed_state
            persistence_uncertain = True
        verify_regular_file(
            publication.destination,
            publication.identity,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )
        if publication.destination_durable:
            _durably_remove_uncommitted_raw(
                publication.staged,
                operation="successful commit",
                expected=publication.identity,
            )
        else:
            logger.warning(
                "retaining raw-ingest staging generation for startup "
                "reconciliation after an unconfirmed destination fsync: %s",
                relative_path,
            )
        return _RawIngestPersistence(state, persistence_uncertain)


def _update_healthkit_state(
    bind,
    initial: _RawIngestState,
    *,
    parse_status: str,
    forward_status: str,
    forward_detail: str | None,
    records_forwarded: int,
) -> _HealthKitStatusUpdate:
    """Best-effort status update; raw durability already succeeded."""
    result = _healthkit_result_state(
        initial,
        parse_status=parse_status,
        forward_status=forward_status,
        forward_detail=forward_detail,
        records_forwarded=records_forwarded,
    )
    try:
        with global_write_plane_guard(bind) as guard_connection:
            writer_bind = guard_connection if guard_connection is not None else bind
            with Session(bind=writer_bind) as writer:
                row = writer.get(RawIngestEvent, initial.raw_id)
                if row is None:
                    logger.warning(
                        "healthkit status row disappeared for %s; returning "
                        "current result with uncertain persistence",
                        initial.raw_id,
                    )
                    return _HealthKitStatusUpdate(result, True)
                row.parse_status = parse_status
                row.forward_status = forward_status
                row.forward_detail = forward_detail
                row.records_forwarded = records_forwarded
                try:
                    writer.commit()
                except BaseException:
                    _rollback_after_commit_error(
                        writer,
                        operation="healthkit status",
                    )
                    verified = _load_raw_state(writer_bind, initial.raw_id)
                    if _healthkit_status_matches(verified, result):
                        logger.warning(
                            "healthkit status commit acknowledgement was "
                            "ambiguous for %s, but the current result was "
                            "verified as durable",
                            initial.raw_id,
                        )
                        return _HealthKitStatusUpdate(result, False)
                    logger.warning(
                        "healthkit status commit could not be verified after "
                        "raw durability for %s; returning current result with "
                        "uncertain persistence",
                        initial.raw_id,
                    )
                    return _HealthKitStatusUpdate(result, True)
            verified = _load_raw_state(writer_bind, initial.raw_id)
            if _healthkit_status_matches(verified, result):
                return _HealthKitStatusUpdate(result, False)
            logger.warning(
                "healthkit status commit completed but the current result "
                "could not be verified for %s; returning it with uncertain "
                "persistence",
                initial.raw_id,
            )
            return _HealthKitStatusUpdate(result, True)
    except Exception:
        logger.exception(
            "healthkit status update could not complete for %s; raw is durable "
            "and the current result will be returned with uncertain persistence",
            initial.raw_id,
        )
        return _HealthKitStatusUpdate(result, True)


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
        if len(chunks) + len(chunk) > limit:
            raise HTTPException(status_code=413, detail=f"payload exceeds {limit} bytes")
        chunks.extend(chunk)
    if not chunks:
        raise HTTPException(status_code=400, detail="empty body")
    return bytes(chunks)


@router.post("/healthkit", status_code=202)
async def ingest_healthkit(request: Request, session: SessionDep) -> IngestAck:
    """Store a HealthKit bridge push verbatim, then map+forward best-effort."""
    settings = request.app.state.settings
    body = await _read_capped_body(request)

    bind = session.get_bind()
    persisted = await anyio.to_thread.run_sync(
        lambda: _persist_raw_ingest(
            bind,
            settings,
            source="healthkit-bridge",
            content_type=request.headers.get("content-type"),
            body=body,
        )
    )
    initial = persisted.state

    payload = None
    try:
        payload = json.loads(body)
        parse_status = "parsed"
    except (json.JSONDecodeError, UnicodeDecodeError):
        parse_status = "stored_unparsed"

    records: list[dict] = transform_hae(payload) if payload is not None else []
    user_id = (settings.ow_user_id or "").strip()
    forward_detail: str | None = None
    records_forwarded = 0
    if not records:
        forward_status = "nothing_mapped"
    elif not user_id:
        forward_status = "skipped_no_user"
    else:
        transport = getattr(request.app.state, "ingest_transport", None)
        try:
            # Thread pool: the sync HTTP client must not stall the event loop.
            await anyio.to_thread.run_sync(
                lambda: forward_sdk_sync(settings, records, user_id=user_id, transport=transport)
            )
            # "queued": open-wearables ack'd (202) and parses asynchronously —
            # not a claim that the records are already normalized.
            forward_status = "queued"
            records_forwarded = len(records)
        except IngestForwardError as exc:
            # Raw is durable; the forward can be replayed from it later.
            forward_status = "forward_failed"
            forward_detail = str(exc)[:255]
            logger.warning(
                "ingest forward failed (raw kept at %s): %s",
                initial.path,
                exc,
            )

    status_update = await anyio.to_thread.run_sync(
        lambda: _update_healthkit_state(
            bind,
            initial,
            parse_status=parse_status,
            forward_status=forward_status,
            forward_detail=forward_detail,
            records_forwarded=records_forwarded,
        )
    )
    return _ack(
        status_update.ack_state,
        status_persistence_uncertain=(
            persisted.persistence_uncertain
            or status_update.persistence_uncertain
        ),
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
    persisted = await anyio.to_thread.run_sync(
        lambda: _persist_raw_ingest(
            session.get_bind(),
            settings,
            source=source,
            content_type=request.headers.get("content-type"),
            body=body,
        )
    )
    return _ack(
        persisted.state,
        status_persistence_uncertain=persisted.persistence_uncertain,
    )
