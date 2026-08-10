"""ActivityWatch localhost adapter with AFK filtering and incremental cursors."""

from __future__ import annotations

import hashlib
import ipaddress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.activity.aggregation import (
    rebuild_affected_days,
    summary_raw_provenance_complete,
)
from healthmes.activity.contracts import (
    ActivityBatchIn,
    ActivityBatchOut,
    ActivityCapability,
    ActivityCollectionStatusUpdate,
    ActivityPermissionStatus,
    ActivityWatchImportRequest,
    AppIntervalRecord,
)
from healthmes.activity.identity import scoped_source_record_id
from healthmes.activity.privacy import collection_gate, filter_records
from healthmes.activity.repository import (
    APP_INTERVAL_EVENT,
    ActivityLocalScope,
    event_bounds,
    event_is_expired,
    event_scopes,
    get_control_payload,
    parse_optional_datetime,
    record_scopes,
    tombstoned_record_ids,
    update_collection_status,
    update_cursor,
)
from healthmes.activity.service import (
    ActivityCollectionBlockedError,
    ActivityIngestResult,
    ActivitySummaryProvenanceError,
    ingest_activity_batch,
)
from healthmes.store import WellnessEvent

ACTIVITYWATCH_PROVIDER = "activitywatch"
DEFAULT_LOOKBACK = timedelta(days=1)
CURSOR_OVERLAP = timedelta(minutes=5)
MAX_FUTURE_SKEW = timedelta(minutes=1)
RECONCILIATION_LOOKBACK = timedelta(days=7)
IMPORT_BATCH_SIZE = 5000


class ActivityWatchError(RuntimeError):
    pass


class ActivityWatchRequestError(ActivityWatchError):
    """A deterministic caller range error, not an upstream source failure."""


def _validate_import_range(
    *,
    start: datetime,
    end: datetime,
    current: datetime,
) -> None:
    if start >= end:
        raise ActivityWatchRequestError(
            "ActivityWatch import start must be before end"
        )
    if end > current + MAX_FUTURE_SKEW:
        raise ActivityWatchRequestError(
            "ActivityWatch import end cannot be in the future"
        )
    if end - start > timedelta(days=7):
        raise ActivityWatchRequestError(
            "one ActivityWatch import cannot exceed 7 days"
        )


def validate_loopback_base_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "http":
        raise ActivityWatchError("ActivityWatch base_url must use loopback HTTP")
    host = parsed.hostname
    if host is None:
        raise ActivityWatchError("ActivityWatch base_url has no hostname")
    if host == "localhost":
        return value.rstrip("/")
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise ActivityWatchError("ActivityWatch base_url must be loopback-only")
    except ValueError as exc:
        raise ActivityWatchError("ActivityWatch base_url must be loopback-only") from exc
    return value.rstrip("/")


class ActivityWatchClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.base_url = validate_loopback_base_url(base_url)
        self.transport = transport
        self.timeout = timeout

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            transport=self.transport,
            timeout=self.timeout,
        )

    def list_buckets(self) -> dict[str, dict[str, Any]]:
        with self._client() as client:
            response = client.get("/api/0/buckets/")
            response.raise_for_status()
        payload = _response_json(response, response_kind="buckets")
        if not isinstance(payload, dict):
            raise ActivityWatchError("ActivityWatch buckets response must be an object")
        if any(not isinstance(value, dict) for value in payload.values()):
            raise ActivityWatchError(
                "ActivityWatch buckets response contains a malformed bucket"
            )
        return {str(key): value for key, value in payload.items()}

    def get_events(
        self,
        bucket_id: str,
        *,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        with self._client() as client:
            response = client.get(
                f"/api/0/buckets/{bucket_id}/events",
                params={
                    "start": start.astimezone(UTC).isoformat(),
                    "end": end.astimezone(UTC).isoformat(),
                    "limit": -1,
                },
            )
            response.raise_for_status()
        payload = _response_json(response, response_kind="events")
        if not isinstance(payload, list):
            raise ActivityWatchError("ActivityWatch events response must be a list")
        if any(not isinstance(value, dict) for value in payload):
            raise ActivityWatchError(
                "ActivityWatch events response contains a malformed event"
            )
        return list(payload)


def _response_json(
    response: httpx.Response,
    *,
    response_kind: str,
) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise ActivityWatchError(
            f"ActivityWatch {response_kind} response is not valid JSON"
        ) from exc


def _bucket_type(value: dict[str, Any]) -> str:
    return str(value.get("type") or "").casefold()


def _select_bucket(
    buckets: dict[str, dict[str, Any]],
    *,
    explicit_id: str | None,
    accepted_types: set[str],
) -> str | None:
    if explicit_id is not None:
        if explicit_id not in buckets:
            raise ActivityWatchError(f"ActivityWatch bucket {explicit_id!r} was not found")
        return explicit_id
    candidates = [
        (key, value)
        for key, value in buckets.items()
        if _bucket_type(value) in accepted_types
    ]
    if not candidates:
        return None

    def bucket_rank(item: tuple[str, dict[str, Any]]) -> tuple[datetime, datetime, str]:
        key, value = item
        last_updated = parse_optional_datetime(value.get("last_updated"))
        created = parse_optional_datetime(value.get("created"))
        floor = datetime.min.replace(tzinfo=UTC)
        return last_updated or floor, created or floor, key

    return max(candidates, key=bucket_rank)[0]


@dataclass(frozen=True, slots=True)
class AWSpan:
    event_id: str
    start: datetime
    end: datetime
    data: dict[str, Any]


def _event_span(
    value: object,
    *,
    event_kind: str,
) -> AWSpan:
    if not isinstance(value, dict):
        raise ActivityWatchError(
            f"ActivityWatch {event_kind} event is malformed"
        )
    timestamp = value.get("timestamp")
    duration = value.get("duration")
    data = value.get("data")
    if (
        not isinstance(timestamp, str)
        or isinstance(duration, bool)
        or not isinstance(duration, int | float)
        or not isinstance(data, dict)
    ):
        raise ActivityWatchError(
            f"ActivityWatch {event_kind} event is malformed"
        )
    try:
        duration_seconds = float(duration)
        if not isfinite(duration_seconds) or duration_seconds <= 0:
            raise ValueError
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        start = parsed.astimezone(UTC)
        end = start + timedelta(seconds=duration_seconds)
    except (OverflowError, ValueError):
        raise ActivityWatchError(
            f"ActivityWatch {event_kind} event is malformed"
        ) from None
    safe_data = {key: data[key] for key in ("app", "status") if isinstance(data.get(key), str)}
    raw_id = value.get("id")
    if raw_id is None:
        raw_id = hashlib.sha256(
            f"{timestamp}|{sorted(safe_data.items())}".encode()
        ).hexdigest()[:24]
    return AWSpan(str(raw_id), start, end, safe_data)


def _normalized_interval_record(**values: Any) -> AppIntervalRecord:
    try:
        return AppIntervalRecord(**values)
    except (OverflowError, ValidationError, ValueError) as exc:
        raise ActivityWatchError(
            "ActivityWatch event cannot be represented by the canonical contract"
        ) from exc


def _window_spans(events: list[dict[str, Any]]) -> list[AWSpan]:
    spans: list[AWSpan] = []
    for value in events:
        span = _event_span(value, event_kind="window")
        app = span.data.get("app")
        if not isinstance(app, str) or not app.strip():
            raise ActivityWatchError(
                "ActivityWatch window event is malformed"
            )
        spans.append(span)
    return spans


def _afk_spans(events: list[dict[str, Any]]) -> list[AWSpan]:
    spans: list[AWSpan] = []
    for value in events:
        span = _event_span(value, event_kind="AFK")
        status = str(span.data.get("status") or "").casefold()
        if status not in {"afk", "not-afk"}:
            raise ActivityWatchError(
                "ActivityWatch AFK event is malformed"
            )
        spans.append(span)
    return spans


def _intersections(
    span: AWSpan,
    filters: list[AWSpan],
) -> list[tuple[datetime, datetime, str]]:
    if not filters:
        return [(span.start, span.end, "no-afk")]
    intersections: list[tuple[datetime, datetime, str]] = []
    for candidate in filters:
        if str(candidate.data.get("status") or "").casefold() != "not-afk":
            continue
        start = max(span.start, candidate.start)
        end = min(span.end, candidate.end)
        if end > start:
            intersections.append((start, end, candidate.event_id))
    return intersections


def _continuous_afk_coverage_end(
    events: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> datetime:
    cursor = start
    spans = sorted(
        _afk_spans(events),
        key=lambda span: (span.start, span.end, span.event_id),
    )
    for span in spans:
        clipped_start = max(start, span.start)
        clipped_end = min(end, span.end)
        if clipped_end <= cursor:
            continue
        if clipped_start > cursor:
            return cursor
        cursor = clipped_end
        if cursor >= end:
            return end
    return cursor


def normalize_activitywatch_events(
    *,
    device_id: str,
    window_bucket_id: str,
    window_events: list[dict[str, Any]],
    afk_bucket_id: str | None,
    afk_events: list[dict[str, Any]],
    range_start: datetime | None = None,
    range_end: datetime | None = None,
    launch_start: datetime | None = None,
) -> list[AppIntervalRecord]:
    if (range_start is None) != (range_end is None):
        raise ActivityWatchError("ActivityWatch normalization range must be complete")
    if range_start is not None:
        range_start = range_start.astimezone(UTC)
        range_end = range_end.astimezone(UTC)
        if range_start >= range_end:
            raise ActivityWatchError("ActivityWatch normalization range is empty")
        launch_start = (
            launch_start.astimezone(UTC)
            if launch_start is not None
            else range_start
        )
        if not range_start <= launch_start <= range_end:
            raise ActivityWatchError(
                "ActivityWatch launch boundary must be inside the normalization range"
            )
    elif launch_start is not None:
        raise ActivityWatchError(
            "ActivityWatch launch boundary requires a normalization range"
        )
    windows = sorted(
        _window_spans(window_events),
        key=lambda span: (span.start, span.end, span.event_id),
    )
    afk = sorted(
        _afk_spans(afk_events),
        key=lambda span: (span.start, span.end, span.event_id),
    )
    if afk_bucket_id is not None and not afk:
        return []
    records: list[AppIntervalRecord] = []
    for window in windows:
        app = window.data["app"]
        bounded_start = (
            max(window.start, range_start)
            if range_start is not None
            else window.start
        )
        bounded_end = (
            min(window.end, range_end)
            if range_end is not None
            else window.end
        )
        if bounded_end <= bounded_start:
            continue
        bounded_window = AWSpan(
            event_id=window.event_id,
            start=bounded_start,
            end=bounded_end,
            data=window.data,
        )
        intersections = _intersections(bounded_window, afk)
        source_group_id = scoped_source_record_id(
            prefix="window-group",
            device_id=device_id,
            source_record_id=f"{window_bucket_id}|{window.event_id}",
        )
        launch_in_range = (
            range_start is None
            or range_end is None
            or launch_start <= window.start < range_end
        )
        # window.data["title"] is intentionally never copied out of this scope.
        for index, (start, end, segment_identity) in enumerate(intersections):
            identity = hashlib.sha256(
                (
                    f"{window_bucket_id}|{window.event_id}|"
                    f"{segment_identity}"
                ).encode()
            ).hexdigest()[:32]
            records.append(
                _normalized_interval_record(
                    source_record_id=scoped_source_record_id(
                        prefix="window",
                        device_id=device_id,
                        source_record_id=identity,
                    ),
                    source_group_id=source_group_id,
                    start_at=start,
                    end_at=end,
                    state="active",
                    app_id=app.strip(),
                    category="desktop",
                    launches=1 if index == 0 and launch_in_range else 0,
                )
            )
    if afk_bucket_id is not None:
        for span in afk:
            if str(span.data.get("status") or "").casefold() != "afk":
                continue
            bounded_start = (
                max(span.start, range_start)
                if range_start is not None
                else span.start
            )
            bounded_end = (
                min(span.end, range_end)
                if range_end is not None
                else span.end
            )
            if bounded_end <= bounded_start:
                continue
            identity = hashlib.sha256(
                f"{afk_bucket_id}|{span.event_id}".encode()
            ).hexdigest()[:32]
            records.append(
                _normalized_interval_record(
                    source_record_id=scoped_source_record_id(
                        prefix="afk",
                        device_id=device_id,
                        source_record_id=identity,
                    ),
                    start_at=bounded_start,
                    end_at=bounded_end,
                    state="idle",
                    app_id=None,
                    category=None,
                    launches=0,
                )
            )
    records.sort(key=lambda value: (value.start_at, value.source_record_id))
    return records


@dataclass(frozen=True, slots=True)
class ActivityWatchReconciliation:
    records: tuple[AppIntervalRecord, ...]
    preserved_records: tuple[AppIntervalRecord, ...]
    affected_scopes: frozenset[ActivityLocalScope]


def _range_scopes(
    *,
    start: datetime,
    end: datetime,
    timezone: str,
) -> set[ActivityLocalScope]:
    zone = ZoneInfo(timezone)
    first = start.astimezone(zone).date()
    last = (end - timedelta(microseconds=1)).astimezone(zone).date()
    return {
        ActivityLocalScope(
            day=first + timedelta(days=offset),
            timezone=timezone,
        )
        for offset in range((last - first).days + 1)
    }


def _stored_interval_record(event: WellnessEvent) -> AppIntervalRecord:
    payload = event.payload if isinstance(event.payload, dict) else {}
    start, end = event_bounds(event)
    try:
        return AppIntervalRecord(
            source_record_id=str(event.source_record_id),
            source_group_id=payload.get("source_group_id"),
            start_at=start,
            end_at=end,
            state=str(payload.get("state")),
            app_id=payload.get("app_id"),
            category=payload.get("category"),
            launches=int(payload.get("launches") or 0),
        )
    except (TypeError, ValueError) as exc:
        raise ActivityWatchError(
            "stored ActivityWatch interval is not a valid canonical record"
        ) from exc


def _fragment_record(
    record: AppIntervalRecord,
    *,
    device_id: str,
    start: datetime,
    end: datetime,
    keep_launch: bool,
) -> AppIntervalRecord:
    digest = hashlib.sha256(
        (
            f"{record.source_record_id}|{start.isoformat()}|"
            f"{end.isoformat()}"
        ).encode()
    ).hexdigest()[:32]
    return record.model_copy(
        update={
            "source_record_id": scoped_source_record_id(
                prefix="aw-fragment",
                device_id=device_id,
                source_record_id=digest,
            ),
            "start_at": start,
            "end_at": end,
            "launches": record.launches if keep_launch else 0,
        }
    )


def _same_interval_semantics(
    left: AppIntervalRecord,
    right: AppIntervalRecord,
) -> bool:
    return (
        left.state == right.state
        and left.app_id == right.app_id
        and left.category == right.category
    )


def _reconcile_group_launches(
    *,
    existing: list[AppIntervalRecord],
    reconciled: list[AppIntervalRecord],
    preserved: list[AppIntervalRecord],
    range_start: datetime,
) -> tuple[list[AppIntervalRecord], list[AppIntervalRecord]]:
    existing_by_group: dict[str, list[AppIntervalRecord]] = {}
    for record in existing:
        if record.source_group_id is not None:
            existing_by_group.setdefault(record.source_group_id, []).append(record)

    reconciled_output = list(reconciled)
    preserved_output = list(preserved)
    groups = {
        record.source_group_id
        for record in (*reconciled_output, *preserved_output)
        if record.source_group_id is not None
    }
    for group_id in groups:
        old_launches = max(
            (
                record.launches
                for record in existing_by_group.get(group_id, ())
            ),
            default=0,
        )
        indexed = [
            ("reconciled", index, record)
            for index, record in enumerate(reconciled_output)
            if record.source_group_id == group_id
        ]
        indexed.extend(
            ("preserved", index, record)
            for index, record in enumerate(preserved_output)
            if record.source_group_id == group_id
        )
        launch_count = max(
            old_launches,
            max((record.launches for _, _, record in indexed), default=0),
        )
        if launch_count == 0:
            continue

        external_left_owner = sorted(
            (
                record
                for record in existing_by_group.get(group_id, ())
                if record.end_at <= range_start and record.launches > 0
            ),
            key=lambda record: (record.start_at, record.source_record_id),
        )
        outside_left = sorted(
            (
                entry
                for entry in indexed
                if entry[2].start_at < range_start
                and entry[2].launches > 0
            ),
            key=lambda entry: (entry[2].start_at, entry[2].source_record_id),
        )
        incoming_owner = sorted(
            (
                entry
                for entry in indexed
                if entry[0] == "reconciled" and entry[2].launches > 0
            ),
            key=lambda entry: (entry[2].start_at, entry[2].source_record_id),
        )
        incoming_fallback = sorted(
            (entry for entry in indexed if entry[0] == "reconciled"),
            key=lambda entry: (entry[2].start_at, entry[2].source_record_id),
        )
        owner = (
            None
            if external_left_owner
            else outside_left[0]
            if outside_left
            else incoming_owner[0]
            if incoming_owner
            else incoming_fallback[0]
            if old_launches > 0 and incoming_fallback
            else None
        )
        for output_kind, index, record in indexed:
            launches = (
                launch_count
                if owner is not None
                and output_kind == owner[0]
                and index == owner[1]
                else 0
            )
            replacement = record.model_copy(update={"launches": launches})
            if output_kind == "reconciled":
                reconciled_output[index] = replacement
            else:
                preserved_output[index] = replacement
    return reconciled_output, preserved_output


def _reconcile_activitywatch_range(
    session: Session,
    *,
    device_id: str,
    timezone: str,
    start: datetime,
    end: datetime,
    records: list[AppIntervalRecord],
    now: datetime,
) -> ActivityWatchReconciliation:
    incoming_by_id: dict[str, AppIntervalRecord] = {}
    for record in records:
        previous = incoming_by_id.get(record.source_record_id)
        if previous is not None and previous != record:
            raise ActivityWatchError(
                "ActivityWatch returned conflicting rows for one source identity"
            )
        incoming_by_id[record.source_record_id] = record

    candidate_rows = [
        row
        for row in session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == APP_INTERVAL_EVENT,
                WellnessEvent.source_provider == ACTIVITYWATCH_PROVIDER,
                WellnessEvent.source_device == device_id,
                WellnessEvent.observed_at >= start - RECONCILIATION_LOOKBACK,
                WellnessEvent.observed_at < end,
            )
        )
        if not event_is_expired(row, now=now)
    ]
    existing_rows: list[WellnessEvent] = []
    existing_records: list[AppIntervalRecord] = []
    launch_context_records: list[AppIntervalRecord] = []
    for row in candidate_rows:
        record = _stored_interval_record(row)
        _, row_end = event_bounds(row)
        if row_end > start:
            existing_rows.append(row)
            existing_records.append(record)
        elif (
            row_end == start
            and record.source_group_id is not None
            and record.launches > 0
        ):
            # A prior repair may have left the launch owner immediately before
            # this range. It participates only in ownership reconciliation:
            # the non-overlapping row itself must not be deleted or rewritten.
            launch_context_records.append(record)
    # The requested range is authoritative even when both the source response
    # and retained raw rows are empty. A long-lived summary may still include
    # now-expired raw evidence, so every covered local day must be prevalidated.
    affected_scopes = _range_scopes(
        start=start,
        end=end,
        timezone=timezone,
    )
    affected_scopes.update(
        scope
        for record in incoming_by_id.values()
        for scope in record_scopes(record, timezone)
    )
    affected_scopes.update(
        scope
        for row in existing_rows
        for scope in event_scopes(row)
    )
    incomplete = [
        scope
        for scope in sorted(affected_scopes)
        if not summary_raw_provenance_complete(
            session,
            day=scope.day,
            timezone=scope.timezone,
            now=now,
        )
    ]
    if incomplete:
        raise ActivitySummaryProvenanceError(
            "ActivityWatch range replacement requires retained raw provenance "
            f"for {len(incomplete)} summary scope(s)"
        )

    preserved: list[AppIntervalRecord] = []
    for row, existing in zip(existing_rows, existing_records, strict=True):
        left = (
            (existing.start_at, min(existing.end_at, start))
            if existing.start_at < start
            else None
        )
        right = (
            (max(existing.start_at, end), existing.end_at)
            if existing.end_at > end
            else None
        )
        incoming = incoming_by_id.get(existing.source_record_id)
        if incoming is None:
            session.delete(row)
            if left is not None and left[1] > left[0]:
                preserved.append(
                    _fragment_record(
                        existing,
                        device_id=device_id,
                        start=left[0],
                        end=left[1],
                        keep_launch=True,
                    )
                )
            if right is not None and right[1] > right[0]:
                preserved.append(
                    _fragment_record(
                        existing,
                        device_id=device_id,
                        start=right[0],
                        end=right[1],
                        keep_launch=False,
                    )
                )
            continue

        same_semantics = _same_interval_semantics(existing, incoming)
        if (
            left is not None
            and left[1] == incoming.start_at
            and same_semantics
        ):
            incoming = incoming.model_copy(
                update={
                    "start_at": left[0],
                    "launches": max(existing.launches, incoming.launches),
                }
            )
        elif left is not None and left[1] > left[0]:
            preserved.append(
                _fragment_record(
                    existing,
                    device_id=device_id,
                    start=left[0],
                    end=left[1],
                    keep_launch=True,
                )
            )
            if existing.launches > 0:
                incoming = incoming.model_copy(update={"launches": 0})
        elif same_semantics and existing.launches > incoming.launches:
            incoming = incoming.model_copy(
                update={"launches": existing.launches}
            )
        if (
            right is not None
            and incoming.end_at == right[0]
            and same_semantics
        ):
            incoming = incoming.model_copy(update={"end_at": right[1]})
        elif right is not None and right[1] > right[0]:
            preserved.append(
                _fragment_record(
                    existing,
                    device_id=device_id,
                    start=right[0],
                    end=right[1],
                    keep_launch=False,
                )
            )
        incoming_by_id[existing.source_record_id] = incoming

    session.flush()
    reconciled = sorted(
        incoming_by_id.values(),
        key=lambda value: (value.start_at, value.source_record_id),
    )
    preserved.sort(key=lambda value: (value.start_at, value.source_record_id))
    reconciled, preserved = _reconcile_group_launches(
        existing=[*existing_records, *launch_context_records],
        reconciled=reconciled,
        preserved=preserved,
        range_start=start,
    )
    return ActivityWatchReconciliation(
        records=tuple(reconciled),
        preserved_records=tuple(preserved),
        affected_scopes=frozenset(affected_scopes),
    )


def import_activitywatch(
    session: Session,
    request: ActivityWatchImportRequest,
    *,
    client: ActivityWatchClient | None = None,
    now: datetime | None = None,
) -> ActivityIngestResult:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    state = get_control_payload(session, request.device_id, platform=request.platform)
    gate = collection_gate(state, now=current)
    if not gate.allowed:
        raise ActivityCollectionBlockedError(gate.reason or "collection_blocked")
    if request.end_at is not None and request.end_at > current + MAX_FUTURE_SKEW:
        raise ActivityWatchRequestError(
            "ActivityWatch import end cannot be in the future"
        )
    if request.start_at is not None:
        _validate_import_range(
            start=request.start_at,
            end=request.end_at or current,
            current=current,
        )
    aw = client or ActivityWatchClient(str(request.base_url))
    buckets = aw.list_buckets()
    window_bucket = _select_bucket(
        buckets,
        explicit_id=request.window_bucket_id,
        accepted_types={"currentwindow", "window"},
    )
    if window_bucket is None:
        raise ActivityWatchError("no ActivityWatch window bucket is available")
    afk_bucket = _select_bucket(
        buckets,
        explicit_id=request.afk_bucket_id,
        accepted_types={"afkstatus", "afk"},
    )
    if afk_bucket is None:
        raise ActivityWatchError(
            "ActivityWatch AFK bucket is unavailable; import was not advanced"
        )
    cursor_key = f"activitywatch:{window_bucket}"
    cursor = parse_optional_datetime((state.get("cursors") or {}).get(cursor_key))
    start = request.start_at or (
        cursor - CURSOR_OVERLAP if cursor is not None else current - DEFAULT_LOOKBACK
    )
    end = request.end_at or current
    _validate_import_range(start=start, end=end, current=current)
    window_events = aw.get_events(window_bucket, start=start, end=end)
    afk_events = aw.get_events(afk_bucket, start=start, end=end)
    coverage_end = _continuous_afk_coverage_end(
        afk_events,
        start=start,
        end=end,
    )
    if coverage_end < end:
        raise ActivityWatchError(
            "ActivityWatch AFK coverage is unavailable or incomplete; "
            "import was not advanced"
        )
    records = normalize_activitywatch_events(
        device_id=request.device_id,
        window_bucket_id=window_bucket,
        window_events=window_events,
        afk_bucket_id=afk_bucket,
        afk_events=afk_events,
        range_start=start,
        range_end=end,
        # Reconciliation, not the cursor boundary, owns launch de-duplication.
        # A source event first discovered inside the overlap must keep its
        # launch, while an already stored event is replaced/group-reconciled
        # back to exactly one launch.
        launch_start=start,
    )
    filtered, excluded, _ = filter_records(records, state, now=current)
    tombstoned_ids = tombstoned_record_ids(
        session,
        source_provider=ACTIVITYWATCH_PROVIDER,
        device_id=request.device_id,
        records=filtered,
    )
    accepted_source_records = [
        record
        for record in filtered
        if (
            isinstance(record, AppIntervalRecord)
            and record.source_record_id not in tombstoned_ids
        )
    ]
    created = updated = duplicates = 0
    with session.begin_nested():
        reconciliation = _reconcile_activitywatch_range(
            session,
            device_id=request.device_id,
            timezone=request.timezone,
            start=start,
            end=end,
            records=accepted_source_records,
            now=current,
        )
        prevalidated_scopes = set(reconciliation.affected_scopes)
        changed_scopes = set(reconciliation.affected_scopes)
        for preserved_offset in range(
            0,
            len(reconciliation.preserved_records),
            IMPORT_BATCH_SIZE,
        ):
            preserved_batch = ActivityBatchIn(
                source_provider=ACTIVITYWATCH_PROVIDER,
                source_device=request.device_id,
                platform=request.platform,
                capability=ActivityCapability.DETAILED,
                timezone=request.timezone,
                collected_at=current,
                collection_revision=int(state.get("config_revision", 0)),
                records=list(
                    reconciliation.preserved_records[
                        preserved_offset : preserved_offset + IMPORT_BATCH_SIZE
                    ]
                ),
            )
            chunk = ingest_activity_batch(
                session,
                preserved_batch,
                allow_replace=True,
                now=current,
                already_filtered=True,
                rebuild_summaries=False,
                prevalidated_summary_scopes=prevalidated_scopes,
            )
            changed_scopes.update(chunk.changed_scopes)
            prevalidated_scopes.update(chunk.changed_scopes)

        accepted_records: list[AppIntervalRecord] = []
        for offset in range(
            0,
            len(reconciliation.records),
            IMPORT_BATCH_SIZE,
        ):
            batch = ActivityBatchIn(
                source_provider=ACTIVITYWATCH_PROVIDER,
                source_device=request.device_id,
                platform=request.platform,
                capability=ActivityCapability.DETAILED,
                timezone=request.timezone,
                collected_at=current,
                collection_revision=int(state.get("config_revision", 0)),
                records=list(
                    reconciliation.records[offset : offset + IMPORT_BATCH_SIZE]
                ),
            )
            chunk = ingest_activity_batch(
                session,
                batch,
                allow_replace=True,
                now=current,
                already_filtered=True,
                rebuild_summaries=False,
                prevalidated_summary_scopes=prevalidated_scopes,
            )
            created += chunk.response.created
            updated += chunk.response.updated
            duplicates += chunk.response.duplicates
            changed_scopes.update(chunk.changed_scopes)
            prevalidated_scopes.update(chunk.changed_scopes)
            accepted_records.extend(
                record
                for record in chunk.records
                if isinstance(record, AppIntervalRecord)
            )

        if changed_scopes:
            by_timezone: dict[str, set[date]] = {}
            for scope in changed_scopes:
                by_timezone.setdefault(scope.timezone, set()).add(scope.day)
            for timezone, days in by_timezone.items():
                rebuild_affected_days(
                    session,
                    days=days,
                    timezone=timezone,
                    force_rebuild=True,
                    now=current,
                )

        update_collection_status(
            session,
            request.device_id,
            ActivityCollectionStatusUpdate(
                platform=request.platform,
                capability=ActivityCapability.DETAILED,
                permission_status=ActivityPermissionStatus.GRANTED,
                status_reason=(
                    "no_events_in_window"
                    if not records
                    else "all_events_suppressed"
                    if not accepted_source_records
                    else None
                ),
                status_observed_at=current,
                last_collected_at=max(
                    end,
                    parse_optional_datetime(state.get("last_collected_at"))
                    or end,
                ),
                last_uploaded_at=current,
                queue_depth=0,
            ),
            now=current,
        )
        explicit_range = request.start_at is not None
        cursor_values = state.get("cursors") or {}
        for key in (cursor_key, f"activitywatch:{afk_bucket}"):
            existing_cursor = parse_optional_datetime(cursor_values.get(key))
            if explicit_range and existing_cursor is not None:
                continue
            if existing_cursor is not None and existing_cursor >= end:
                continue
            update_cursor(
                session,
                request.device_id,
                key,
                end.isoformat(),
                platform=request.platform,
                now=current,
            )
        return ActivityIngestResult(
            response=ActivityBatchOut(
                accepted=len(accepted_records),
                created=created,
                updated=updated,
                duplicates=duplicates,
                excluded=excluded,
                tombstoned=len(tombstoned_ids),
                affected_dates=sorted(
                    {
                        scope.day.isoformat()
                        for scope in changed_scopes
                    }
                ),
            ),
            records=tuple(accepted_records),
            changed_scopes=frozenset(changed_scopes),
        )
