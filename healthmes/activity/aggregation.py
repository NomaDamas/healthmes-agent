"""Deterministic hourly/daily summaries over canonical activity events."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.activity.repository import (
    ACTIVITY_DAILY_CLASS,
    ACTIVITY_HOURLY_CLASS,
    APP_HOUR_EVENT,
    APP_INTERVAL_EVENT,
    DAY_SUMMARY_EVENT,
    HOUR_SUMMARY_EVENT,
    RAW_EVENT_TYPES,
    SUMMARY_PROVIDER,
    as_utc,
    ensure_activity_policies,
    upsert_summary_event,
)
from healthmes.store import WellnessEvent

LATE_START_HOUR = 22
LATE_END_HOUR = 6
BASELINE_DAYS = 7
MIN_BASELINE_DAYS = 3


def timezone_name(value: str | tzinfo) -> str:
    if isinstance(value, str):
        return value
    key = getattr(value, "key", None)
    return str(key) if key else str(value)


def local_day_bounds(day: date, timezone: str | tzinfo) -> tuple[datetime, datetime]:
    tz = ZoneInfo(timezone) if isinstance(timezone, str) else timezone
    start = datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC)
    return start, end


def _hour_windows(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Return one-hour UTC spans from a local-day boundary.

    Local midnight can be at a half-hour UTC offset. Advancing from that
    boundary in elapsed hours preserves correct 23/24/25-hour DST days.
    """
    windows: list[tuple[datetime, datetime]] = []
    cursor = as_utc(start)
    limit = as_utc(end)
    while cursor < limit:
        next_cursor = min(cursor + timedelta(hours=1), limit)
        windows.append((cursor, next_cursor))
        cursor = next_cursor
    return windows


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return as_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _round_minutes(seconds: float) -> float:
    return round(seconds / 60.0, 2)


def _union_seconds(
    spans: Iterable[tuple[datetime, datetime]],
    *,
    start: datetime,
    end: datetime,
) -> float:
    clipped = sorted(
        (
            max(as_utc(span_start), start),
            min(as_utc(span_end), end),
        )
        for span_start, span_end in spans
        if min(as_utc(span_end), end) > max(as_utc(span_start), start)
    )
    total = 0.0
    merged_start: datetime | None = None
    merged_end: datetime | None = None
    for span_start, span_end in clipped:
        if merged_start is None:
            merged_start, merged_end = span_start, span_end
            continue
        assert merged_end is not None
        if span_start <= merged_end:
            merged_end = max(merged_end, span_end)
            continue
        total += (merged_end - merged_start).total_seconds()
        merged_start, merged_end = span_start, span_end
    if merged_start is not None and merged_end is not None:
        total += (merged_end - merged_start).total_seconds()
    return total


def _device_key(device_id: str | None) -> str:
    value = device_id or "unknown-device"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


@dataclass(slots=True)
class DeviceHour:
    device_key: str
    platforms: set[str] = field(default_factory=set)
    capabilities: set[str] = field(default_factory=set)
    active_seconds: float = 0.0
    idle_seconds: float = 0.0
    category_seconds: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    launches: int = 0
    first_activity_at: datetime | None = None
    last_activity_at: datetime | None = None
    active_spans: list[tuple[datetime, datetime]] = field(default_factory=list)
    idle_spans: list[tuple[datetime, datetime]] = field(default_factory=list)
    coverage_spans: list[tuple[datetime, datetime]] = field(default_factory=list)
    known_coverage_seconds: float | None = None
    has_interval_data: bool = False
    has_idle_data: bool = False
    has_unknown_coverage: bool = False
    source_overflow: bool = False
    evidence_ids: list[str] = field(default_factory=list)


def _query_raw_events(
    session: Session,
    *,
    start: datetime,
    end: datetime,
) -> list[WellnessEvent]:
    # Intervals are capped at 24 hours by the ingest contract; the pad catches
    # an interval that starts before the requested window and overlaps it.
    return list(
        session.scalars(
            select(WellnessEvent)
            .where(
                WellnessEvent.event_type.in_(RAW_EVENT_TYPES),
                WellnessEvent.observed_at >= start - timedelta(hours=24),
                WellnessEvent.observed_at < end,
            )
            .order_by(WellnessEvent.observed_at, WellnessEvent.id)
        )
    )


def _overlap_fraction(
    source_start: datetime,
    source_end: datetime,
    window_start: datetime,
    window_end: datetime,
) -> float:
    overlap = (min(source_end, window_end) - max(source_start, window_start)).total_seconds()
    duration = (source_end - source_start).total_seconds()
    return max(0.0, overlap) / duration if duration > 0 else 0.0


def _touch_activity_bounds(
    target: DeviceHour,
    start: datetime,
    end: datetime,
) -> None:
    target.first_activity_at = (
        start if target.first_activity_at is None else min(target.first_activity_at, start)
    )
    target.last_activity_at = (
        end if target.last_activity_at is None else max(target.last_activity_at, end)
    )


def _apply_interval_event(
    target: DeviceHour,
    event: WellnessEvent,
    *,
    start: datetime,
    end: datetime,
) -> None:
    payload = event.payload
    interval_start = _parse_datetime(payload.get("start_at"))
    interval_end = _parse_datetime(payload.get("end_at"))
    if interval_start is None or interval_end is None:
        return
    clipped_start = max(interval_start, start)
    clipped_end = min(interval_end, end)
    if clipped_end <= clipped_start:
        return
    target.has_interval_data = True
    target.coverage_spans.append((clipped_start, clipped_end))
    target.evidence_ids.append(str(event.id))
    platform = payload.get("platform")
    if isinstance(platform, str):
        target.platforms.add(platform)
    capability = payload.get("capability")
    if isinstance(capability, str):
        target.capabilities.add(capability)
    state = payload.get("state")
    duration = (clipped_end - clipped_start).total_seconds()
    if state == "active":
        target.active_seconds += duration
        target.active_spans.append((clipped_start, clipped_end))
        category = payload.get("category")
        target.category_seconds[str(category or "uncategorized")] += duration
        if interval_start >= start and interval_start < end:
            target.launches += int(payload.get("launches") or 0)
        _touch_activity_bounds(target, clipped_start, clipped_end)
    elif state in {"idle", "locked"}:
        target.idle_seconds += duration
        target.idle_spans.append((clipped_start, clipped_end))
        target.has_idle_data = True


def _apply_hour_events(
    target: DeviceHour,
    events: list[WellnessEvent],
    *,
    start: datetime,
    end: datetime,
) -> None:
    if target.has_interval_data:
        return
    window_seconds = (end - start).total_seconds()
    for event in events:
        payload = event.payload
        bucket_start = _parse_datetime(payload.get("bucket_start"))
        if bucket_start is None:
            continue
        bucket_end = bucket_start + timedelta(hours=1)
        fraction = _overlap_fraction(bucket_start, bucket_end, start, end)
        if fraction <= 0:
            continue
        target.evidence_ids.append(str(event.id))
        platform = payload.get("platform")
        if isinstance(platform, str):
            target.platforms.add(platform)
        capability = payload.get("capability")
        if isinstance(capability, str):
            target.capabilities.add(capability)
        source_active = float(payload.get("foreground_seconds") or 0)
        if source_active > 3600:
            target.source_overflow = True
        active = source_active * fraction
        target.active_seconds += active
        category = payload.get("category")
        target.category_seconds[str(category or "uncategorized")] += active
        if bucket_start >= start and bucket_start < end:
            target.launches += int(payload.get("launches") or 0)
        if active > 0:
            _touch_activity_bounds(target, max(bucket_start, start), min(bucket_end, end))
        coverage = payload.get("coverage_seconds")
        if isinstance(coverage, int | float):
            known = min(window_seconds, max(0.0, float(coverage) * fraction))
            target.known_coverage_seconds = max(
                target.known_coverage_seconds or 0.0,
                known,
            )
        else:
            target.has_unknown_coverage = True

    if target.active_seconds > window_seconds:
        target.source_overflow = True
        scale = window_seconds / target.active_seconds
        target.active_seconds = window_seconds
        target.category_seconds = {
            key: value * scale for key, value in target.category_seconds.items()
        }


def _longest_span_minutes(spans: Iterable[tuple[datetime, datetime]]) -> float | None:
    ordered = sorted(spans)
    if not ordered:
        return None
    longest = 0.0
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= current_end + timedelta(seconds=60):
            current_end = max(current_end, end)
            continue
        longest = max(longest, (current_end - current_start).total_seconds())
        current_start, current_end = start, end
    longest = max(longest, (current_end - current_start).total_seconds())
    return _round_minutes(longest)


def _coverage_payload(
    devices: Iterable[DeviceHour],
    *,
    start: datetime,
    end: datetime,
    window_seconds: float,
) -> dict[str, Any]:
    rows = list(devices)
    known = [
        (
            _union_seconds(row.coverage_spans, start=start, end=end)
            if row.coverage_spans
            else row.known_coverage_seconds
        )
        for row in rows
    ]
    known_values = [min(window_seconds, value) for value in known if value is not None]
    has_unknown = any(row.has_unknown_coverage for row in rows)
    if not known_values:
        return {
            "status": "unknown" if rows else "no_data",
            "ratio": None,
            "known_seconds": 0,
            "expected_seconds": int(window_seconds),
            "unknown_device_count": len(rows) if rows else 0,
        }
    # Cross-device coverage overlaps cannot be reconstructed from hourly
    # aggregate sources. The maximum known device coverage is an honest lower
    # bound that never exceeds wall-clock time.
    known_seconds = max(known_values)
    ratio = min(1.0, known_seconds / window_seconds) if window_seconds else 0.0
    return {
        "status": ("partial_known" if has_unknown else "complete" if ratio >= 0.95 else "partial"),
        "ratio": round(ratio, 4),
        "known_seconds": int(round(known_seconds)),
        "expected_seconds": int(window_seconds),
        "unknown_device_count": sum(row.has_unknown_coverage for row in rows),
    }


def summarize_window(
    events: Iterable[WellnessEvent],
    *,
    start: datetime,
    end: datetime,
    timezone: str | tzinfo,
) -> dict[str, Any]:
    start = as_utc(start)
    end = as_utc(end)
    name = timezone_name(timezone)
    zone = ZoneInfo(timezone) if isinstance(timezone, str) else timezone
    by_device: dict[str, DeviceHour] = {}
    interval_events: dict[str, list[WellnessEvent]] = defaultdict(list)
    hour_events: dict[str, list[WellnessEvent]] = defaultdict(list)
    for event in events:
        key = _device_key(event.source_device)
        by_device.setdefault(key, DeviceHour(device_key=key))
        if event.event_type == APP_INTERVAL_EVENT:
            interval_events[key].append(event)
        elif event.event_type == APP_HOUR_EVENT:
            hour_events[key].append(event)

    for key, target in by_device.items():
        for event in interval_events[key]:
            _apply_interval_event(target, event, start=start, end=end)
        if target.active_spans:
            active_union = _union_seconds(target.active_spans, start=start, end=end)
            if target.active_seconds > active_union and target.active_seconds > 0:
                scale = active_union / target.active_seconds
                target.category_seconds = {
                    category: seconds * scale
                    for category, seconds in target.category_seconds.items()
                }
            target.active_seconds = active_union
        if target.idle_spans:
            target.idle_seconds = _union_seconds(
                target.idle_spans,
                start=start,
                end=end,
            )
        _apply_hour_events(target, hour_events[key], start=start, end=end)
        if target.coverage_spans:
            target.known_coverage_seconds = _union_seconds(
                target.coverage_spans,
                start=start,
                end=end,
            )

    devices = [
        row
        for row in by_device.values()
        if row.evidence_ids or row.active_seconds or row.idle_seconds
    ]
    total_active = sum(row.active_seconds for row in devices)
    categories: dict[str, float] = defaultdict(float)
    for row in devices:
        for category, seconds in row.category_seconds.items():
            categories[category] += seconds
    first = min(
        (row.first_activity_at for row in devices if row.first_activity_at is not None),
        default=None,
    )
    last = max(
        (row.last_activity_at for row in devices if row.last_activity_at is not None),
        default=None,
    )
    precise_spans = [span for row in devices for span in row.active_spans]
    idle_known = any(row.has_idle_data for row in devices)
    source_coverage = _coverage_payload(
        devices,
        start=start,
        end=end,
        window_seconds=(end - start).total_seconds(),
    )
    limitations: list[str] = []
    if len(devices) > 1:
        limitations.append("cross_device_overlap_not_deduplicated")
    if any(row.has_unknown_coverage for row in devices):
        limitations.append("source_coverage_unknown_for_some_devices")
    if any(hour_events.values()):
        limitations.append("hourly_aggregate_cannot_reconstruct_exact_focus_blocks")
    if any(row.source_overflow for row in devices):
        limitations.append("source_reported_seconds_exceeded_bucket")
    local_start = start.astimezone(zone)
    return {
        "status": "ok" if devices else "insufficient_data",
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "timezone": name,
            "local_date": local_start.date().isoformat(),
            "local_hour": local_start.hour,
            "utc_offset_minutes": int(local_start.utcoffset().total_seconds() / 60),
        },
        "total_active_minutes": _round_minutes(total_active),
        "category_minutes": {
            key: _round_minutes(value) for key, value in sorted(categories.items())
        },
        "app_launches_or_switches": sum(row.launches for row in devices),
        "longest_active_block_minutes": _longest_span_minutes(precise_spans),
        "idle_and_break_minutes": (
            _round_minutes(sum(row.idle_seconds for row in devices)) if idle_known else None
        ),
        "first_activity_at": first.isoformat() if first is not None else None,
        "last_activity_at": last.isoformat() if last is not None else None,
        "source_coverage": source_coverage,
        "device_count": len(devices),
        "platforms": sorted({platform for row in devices for platform in row.platforms}),
        "capabilities": sorted({capability for row in devices for capability in row.capabilities}),
        "limitations": sorted(set(limitations)),
        "_evidence_event_ids": sorted(
            {event_id for row in devices for event_id in row.evidence_ids}
        ),
        "_active_spans": [
            (span_start.isoformat(), span_end.isoformat()) for span_start, span_end in precise_spans
        ],
    }


def _summary_source_id(kind: str, timezone: str, observed_at: datetime) -> str:
    zone = hashlib.sha256(timezone.encode("utf-8")).hexdigest()[:12]
    return f"{kind}:{zone}:{as_utc(observed_at).isoformat()}"


def _public_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if not key.startswith("_")}


def _baseline_payload(
    session: Session,
    *,
    day: date,
    timezone: str,
    current_minutes: float,
) -> dict[str, Any]:
    return personal_baseline_delta(
        session,
        day=day,
        timezone=timezone,
        current_minutes=current_minutes,
        lookback_days=BASELINE_DAYS,
    )


def personal_baseline_delta(
    session: Session,
    *,
    day: date,
    timezone: str | tzinfo,
    current_minutes: float,
    lookback_days: int,
) -> dict[str, Any]:
    if not 1 <= lookback_days <= 90:
        raise ValueError("lookback_days must be between 1 and 90")
    name = timezone_name(timezone)
    start_day = day - timedelta(days=lookback_days)
    rows = list(
        session.scalars(
            select(WellnessEvent)
            .where(
                WellnessEvent.event_type == DAY_SUMMARY_EVENT,
                WellnessEvent.source_provider == SUMMARY_PROVIDER,
            )
            .order_by(WellnessEvent.observed_at)
        )
    )
    values: list[float] = []
    for row in rows:
        raw_date = row.payload.get("date")
        if row.payload.get("timezone") != name or not isinstance(raw_date, str):
            continue
        try:
            summary_day = date.fromisoformat(raw_date)
        except ValueError:
            continue
        if start_day <= summary_day < day and row.payload.get("status") == "ok":
            values.append(float(row.payload.get("total_active_minutes", 0)))
    required_days = min(MIN_BASELINE_DAYS, lookback_days)
    if len(values) < required_days:
        return {
            "status": "insufficient_data",
            "days_with_data": len(values),
            "required_days": required_days,
            "lookback_days": lookback_days,
        }
    baseline = sum(values) / len(values)
    delta = current_minutes - baseline
    return {
        "status": "ok",
        "days_with_data": len(values),
        "lookback_days": lookback_days,
        "baseline_minutes": round(baseline, 2),
        "delta_minutes": round(delta, 2),
        "delta_percent": round(delta / baseline * 100, 1) if baseline > 0 else None,
    }


def rebuild_day_summaries(
    session: Session,
    *,
    day: date,
    timezone: str | tzinfo,
) -> WellnessEvent | None:
    name = timezone_name(timezone)
    start, end = local_day_bounds(day, timezone)
    policies = ensure_activity_policies(session)
    events = _query_raw_events(session, start=start, end=end)
    existing_hourly = [
        row
        for row in session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == HOUR_SUMMARY_EVENT,
                WellnessEvent.source_provider == SUMMARY_PROVIDER,
                WellnessEvent.observed_at >= start,
                WellnessEvent.observed_at < end,
            )
        )
        if row.timezone == name
    ]
    hourly_events: list[WellnessEvent] = []
    all_evidence: set[str] = set()
    hour_payloads: list[dict[str, Any]] = []
    active_spans: list[tuple[datetime, datetime]] = []
    for hour_start, hour_end in _hour_windows(start, end):
        payload = summarize_window(
            events,
            start=hour_start,
            end=hour_end,
            timezone=timezone,
        )
        if payload["status"] != "ok":
            continue
        evidence = sorted(set(payload.pop("_evidence_event_ids")))
        active_spans.extend(
            (datetime.fromisoformat(span_start), datetime.fromisoformat(span_end))
            for span_start, span_end in payload.pop("_active_spans", [])
        )
        event = upsert_summary_event(
            session,
            event_type=HOUR_SUMMARY_EVENT,
            source_record_id=_summary_source_id("hour", name, hour_start),
            observed_at=hour_start,
            timezone=name,
            payload=payload,
            derived_from={
                "raw_event_count": len(evidence),
                "raw_evidence_sha256": hashlib.sha256(
                    "\n".join(evidence).encode("utf-8")
                ).hexdigest(),
            },
            policy=policies[ACTIVITY_HOURLY_CLASS],
        )
        hourly_events.append(event)
        hour_payloads.append(payload)
        all_evidence.update(evidence)
    rebuilt_hourly_ids = {event.id for event in hourly_events}
    for stale in existing_hourly:
        if stale.id not in rebuilt_hourly_ids:
            session.delete(stale)

    existing_day = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.source_provider == SUMMARY_PROVIDER,
            WellnessEvent.source_record_id == _summary_source_id("day", name, start),
        )
    )
    if not hour_payloads:
        if existing_day is not None:
            session.delete(existing_day)
        session.flush()
        return None

    categories: dict[str, float] = defaultdict(float)
    for payload in hour_payloads:
        for category, minutes in payload["category_minutes"].items():
            categories[category] += float(minutes)
    active_minutes = round(
        sum(float(payload["total_active_minutes"]) for payload in hour_payloads),
        2,
    )
    idle_values = [
        float(payload["idle_and_break_minutes"])
        for payload in hour_payloads
        if payload["idle_and_break_minutes"] is not None
    ]
    known_coverage = [
        payload["source_coverage"]
        for payload in hour_payloads
        if payload["source_coverage"].get("ratio") is not None
    ]
    known_seconds = sum(int(coverage["known_seconds"]) for coverage in known_coverage)
    day_expected_seconds = int((end - start).total_seconds())
    coverage_ratio = (
        min(1.0, known_seconds / day_expected_seconds)
        if known_coverage and day_expected_seconds
        else None
    )
    local_tz = ZoneInfo(timezone) if isinstance(timezone, str) else timezone
    late_minutes = sum(
        float(payload["total_active_minutes"])
        for payload in hour_payloads
        if (
            (hour := datetime.fromisoformat(payload["window"]["start"]).astimezone(local_tz).hour)
            >= LATE_START_HOUR
            or hour < LATE_END_HOUR
        )
    )
    first = min(
        (
            datetime.fromisoformat(payload["first_activity_at"])
            for payload in hour_payloads
            if payload["first_activity_at"] is not None
        ),
        default=None,
    )
    last = max(
        (
            datetime.fromisoformat(payload["last_activity_at"])
            for payload in hour_payloads
            if payload["last_activity_at"] is not None
        ),
        default=None,
    )
    longest_values = [
        float(payload["longest_active_block_minutes"])
        for payload in hour_payloads
        if payload["longest_active_block_minutes"] is not None
    ]
    precise_longest = _longest_span_minutes(active_spans)
    limitations = sorted(
        {limitation for payload in hour_payloads for limitation in payload.get("limitations", [])}
    )
    payload = {
        "status": "ok",
        "date": day.isoformat(),
        "timezone": name,
        "total_active_minutes": active_minutes,
        "category_minutes": {key: round(value, 2) for key, value in sorted(categories.items())},
        "app_launches_or_switches": sum(
            int(value["app_launches_or_switches"]) for value in hour_payloads
        ),
        "longest_active_block_minutes": (
            precise_longest
            if precise_longest is not None
            else max(longest_values)
            if longest_values
            else None
        ),
        "idle_and_break_minutes": (round(sum(idle_values), 2) if idle_values else None),
        "late_activity_minutes": round(late_minutes, 2),
        "first_activity_at": first.isoformat() if first is not None else None,
        "last_activity_at": last.isoformat() if last is not None else None,
        "seven_day_baseline_delta": _baseline_payload(
            session,
            day=day,
            timezone=name,
            current_minutes=active_minutes,
        ),
        "source_coverage": {
            "status": (
                "unknown"
                if coverage_ratio is None
                else "complete"
                if coverage_ratio >= 0.95
                else "partial"
            ),
            "ratio": round(coverage_ratio, 4) if coverage_ratio is not None else None,
            "known_seconds": known_seconds,
            "expected_seconds": day_expected_seconds,
            "hours_with_data": len(hour_payloads),
        },
        "device_count": max(int(value["device_count"]) for value in hour_payloads),
        "platforms": sorted(
            {platform for value in hour_payloads for platform in value["platforms"]}
        ),
        "capabilities": sorted(
            {capability for value in hour_payloads for capability in value["capabilities"]}
        ),
        "limitations": limitations,
    }
    evidence_ids = sorted(all_evidence)
    return upsert_summary_event(
        session,
        event_type=DAY_SUMMARY_EVENT,
        source_record_id=_summary_source_id("day", name, start),
        observed_at=start,
        timezone=name,
        payload=payload,
        derived_from={
            "raw_event_count": len(evidence_ids),
            "raw_evidence_sha256": hashlib.sha256(
                "\n".join(evidence_ids).encode("utf-8")
            ).hexdigest(),
            "hour_summary_ids": [str(event.id) for event in hourly_events],
        },
        policy=policies[ACTIVITY_DAILY_CLASS],
    )


def rebuild_affected_days(
    session: Session,
    *,
    days: Iterable[date],
    timezone: str | tzinfo,
    refresh_following_baselines: bool = True,
) -> list[WellnessEvent]:
    primary = set(days)
    rebuilt: list[WellnessEvent] = []
    for day in sorted(primary):
        event = rebuild_day_summaries(session, day=day, timezone=timezone)
        if event is not None:
            rebuilt.append(event)
    if not refresh_following_baselines:
        return rebuilt

    following = {
        day + timedelta(days=offset) for day in primary for offset in range(1, BASELINE_DAYS + 1)
    } - primary
    for day in sorted(following):
        event = refresh_existing_day_baseline(
            session,
            day=day,
            timezone=timezone,
        )
        if event is None:
            event = rebuild_day_summaries(
                session,
                day=day,
                timezone=timezone,
            )
        if event is not None:
            rebuilt.append(event)
    return rebuilt


def _daily_summary_event(
    session: Session,
    *,
    day: date,
    timezone: str | tzinfo,
) -> WellnessEvent | None:
    name = timezone_name(timezone)
    start, _ = local_day_bounds(day, timezone)
    return session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.source_provider == SUMMARY_PROVIDER,
            WellnessEvent.source_record_id == _summary_source_id("day", name, start),
        )
    )


def refresh_existing_day_baseline(
    session: Session,
    *,
    day: date,
    timezone: str | tzinfo,
) -> WellnessEvent | None:
    event = _daily_summary_event(session, day=day, timezone=timezone)
    if event is None:
        return None
    payload = dict(event.payload)
    payload["seven_day_baseline_delta"] = _baseline_payload(
        session,
        day=day,
        timezone=timezone_name(timezone),
        current_minutes=float(payload.get("total_active_minutes") or 0),
    )
    event.payload = payload
    session.flush()
    return event


def get_daily_summary(
    session: Session,
    *,
    day: date,
    timezone: str | tzinfo,
) -> tuple[WellnessEvent | None, dict[str, Any]]:
    name = timezone_name(timezone)
    row = _daily_summary_event(session, day=day, timezone=timezone)
    if row is None:
        return None, {
            "status": "insufficient_data",
            "date": day.isoformat(),
            "timezone": name,
            "reason": "no_activity_summary",
            "source_coverage": {
                "status": "no_data",
                "ratio": None,
            },
            "limitations": ["missing_is_not_zero"],
        }
    return row, _public_summary(row.payload)


def list_hourly_summaries(
    session: Session,
    *,
    start: datetime,
    end: datetime,
    timezone: str | tzinfo,
) -> list[WellnessEvent]:
    name = timezone_name(timezone)
    rows = list(
        session.scalars(
            select(WellnessEvent)
            .where(
                WellnessEvent.event_type == HOUR_SUMMARY_EVENT,
                WellnessEvent.source_provider == SUMMARY_PROVIDER,
                WellnessEvent.observed_at >= as_utc(start) - timedelta(hours=1),
                WellnessEvent.observed_at < as_utc(end),
            )
            .order_by(WellnessEvent.observed_at)
        )
    )
    window_start = as_utc(start)
    return [
        row
        for row in rows
        if row.timezone == name and as_utc(row.observed_at) + timedelta(hours=1) > window_start
    ]
