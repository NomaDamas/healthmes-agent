"""ActivityWatch localhost adapter with AFK filtering and incremental cursors."""

from __future__ import annotations

import hashlib
import ipaddress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from healthmes.activity.aggregation import rebuild_affected_days
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
from healthmes.activity.privacy import collection_gate
from healthmes.activity.repository import (
    get_control_payload,
    parse_optional_datetime,
    update_collection_status,
    update_cursor,
)
from healthmes.activity.service import (
    ActivityCollectionBlockedError,
    ActivityIngestResult,
    ingest_activity_batch,
)

ACTIVITYWATCH_PROVIDER = "activitywatch"
DEFAULT_LOOKBACK = timedelta(days=1)
CURSOR_OVERLAP = timedelta(minutes=5)


class ActivityWatchError(RuntimeError):
    pass


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
        payload = response.json()
        if not isinstance(payload, dict):
            raise ActivityWatchError("ActivityWatch buckets response must be an object")
        return {str(key): value for key, value in payload.items() if isinstance(value, dict)}

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
        payload = response.json()
        if not isinstance(payload, list):
            raise ActivityWatchError("ActivityWatch events response must be a list")
        return [value for value in payload if isinstance(value, dict)]


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
    candidates = sorted(
        key for key, value in buckets.items() if _bucket_type(value) in accepted_types
    )
    if not candidates:
        return None
    return candidates[-1]


@dataclass(frozen=True, slots=True)
class AWSpan:
    event_id: str
    start: datetime
    end: datetime
    data: dict[str, Any]


def _event_span(value: dict[str, Any]) -> AWSpan | None:
    timestamp = value.get("timestamp")
    duration = value.get("duration")
    data = value.get("data")
    if not isinstance(timestamp, str) or not isinstance(duration, int | float):
        return None
    if not isinstance(data, dict) or duration <= 0:
        return None
    try:
        start = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None
    end = start + timedelta(seconds=float(duration))
    safe_data = {key: data[key] for key in ("app", "status") if isinstance(data.get(key), str)}
    raw_id = value.get("id")
    if raw_id is None:
        raw_id = hashlib.sha256(
            f"{timestamp}|{duration}|{sorted(safe_data.items())}".encode()
        ).hexdigest()[:24]
    return AWSpan(str(raw_id), start, end, safe_data)


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


def normalize_activitywatch_events(
    *,
    device_id: str,
    window_bucket_id: str,
    window_events: list[dict[str, Any]],
    afk_bucket_id: str | None,
    afk_events: list[dict[str, Any]],
) -> list[AppIntervalRecord]:
    windows = [span for value in window_events if (span := _event_span(value)) is not None]
    afk = [span for value in afk_events if (span := _event_span(value)) is not None]
    records: list[AppIntervalRecord] = []
    for window in windows:
        app = window.data.get("app")
        if not isinstance(app, str) or not app.strip():
            continue
        # window.data["title"] is intentionally never copied out of this scope.
        for index, (start, end, afk_id) in enumerate(_intersections(window, afk)):
            identity = hashlib.sha256(
                f"{window_bucket_id}|{window.event_id}|{afk_id}|{index}".encode()
            ).hexdigest()[:32]
            records.append(
                AppIntervalRecord(
                    source_record_id=scoped_source_record_id(
                        prefix="window",
                        device_id=device_id,
                        source_record_id=identity,
                    ),
                    start_at=start,
                    end_at=end,
                    state="active",
                    app_id=app.strip(),
                    category="desktop",
                    launches=1 if index == 0 else 0,
                )
            )
    if afk_bucket_id is not None:
        for span in afk:
            if str(span.data.get("status") or "").casefold() != "afk":
                continue
            identity = hashlib.sha256(f"{afk_bucket_id}|{span.event_id}".encode()).hexdigest()[:32]
            records.append(
                AppIntervalRecord(
                    source_record_id=scoped_source_record_id(
                        prefix="afk",
                        device_id=device_id,
                        source_record_id=identity,
                    ),
                    start_at=span.start,
                    end_at=span.end,
                    state="idle",
                    app_id=None,
                    category=None,
                    launches=0,
                )
            )
    records.sort(key=lambda value: (value.start_at, value.source_record_id))
    return records


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
    cursor_key = f"activitywatch:{window_bucket}"
    cursor = parse_optional_datetime((state.get("cursors") or {}).get(cursor_key))
    start = request.start_at or (
        cursor - CURSOR_OVERLAP if cursor is not None else current - DEFAULT_LOOKBACK
    )
    end = request.end_at or current
    if start >= end:
        raise ActivityWatchError("ActivityWatch import start must be before end")
    window_events = aw.get_events(window_bucket, start=start, end=end)
    afk_events = aw.get_events(afk_bucket, start=start, end=end) if afk_bucket is not None else []
    records = normalize_activitywatch_events(
        device_id=request.device_id,
        window_bucket_id=window_bucket,
        window_events=window_events,
        afk_bucket_id=afk_bucket,
        afk_events=afk_events,
    )
    if not records:
        update_collection_status(
            session,
            request.device_id,
            ActivityCollectionStatusUpdate(
                platform=request.platform,
                capability=ActivityCapability.DETAILED,
                permission_status=ActivityPermissionStatus.GRANTED,
                status_reason="no_events_in_window",
                last_collected_at=end,
                last_uploaded_at=current,
                queue_depth=0,
            ),
            now=current,
        )
        update_cursor(
            session,
            request.device_id,
            cursor_key,
            end.isoformat(),
            platform=request.platform,
            now=current,
        )
        return ActivityIngestResult(
            response=ActivityBatchOut(
                accepted=0,
                created=0,
                updated=0,
                duplicates=0,
                excluded=0,
                affected_dates=[],
            ),
            records=(),
        )
    created = updated = duplicates = excluded = 0
    accepted_records: list[AppIntervalRecord] = []
    affected_dates: set[str] = set()
    for offset in range(0, len(records), 5000):
        batch = ActivityBatchIn(
            source_provider=ACTIVITYWATCH_PROVIDER,
            source_device=request.device_id,
            platform=request.platform,
            capability=ActivityCapability.DETAILED,
            timezone=request.timezone,
            collected_at=current,
            collection_revision=int(state.get("config_revision", 0)),
            records=records[offset : offset + 5000],
        )
        chunk = ingest_activity_batch(
            session,
            batch,
            now=current,
            rebuild_summaries=False,
        )
        created += chunk.response.created
        updated += chunk.response.updated
        duplicates += chunk.response.duplicates
        excluded += chunk.response.excluded
        accepted_records.extend(chunk.records)
        affected_dates.update(chunk.response.affected_dates)
    if affected_dates:
        rebuild_affected_days(
            session,
            days=[datetime.fromisoformat(value).date() for value in affected_dates],
            timezone=request.timezone,
        )
    result = ActivityIngestResult(
        response=ActivityBatchOut(
            accepted=len(accepted_records),
            created=created,
            updated=updated,
            duplicates=duplicates,
            excluded=excluded,
            affected_dates=sorted(affected_dates),
        ),
        records=tuple(accepted_records),
    )
    update_cursor(
        session,
        request.device_id,
        cursor_key,
        end.isoformat(),
        platform=request.platform,
        now=current,
    )
    if afk_bucket is not None:
        update_cursor(
            session,
            request.device_id,
            f"activitywatch:{afk_bucket}",
            end.isoformat(),
            platform=request.platform,
            now=current,
        )
    return result
