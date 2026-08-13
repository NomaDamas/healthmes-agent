"""Read-only, identity-free activity context for decisions and MCP."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any

from sqlalchemy.orm import Session

from healthmes.activity.aggregation import (
    LEGACY_SUMMARY_REASON,
    get_daily_summary,
    legacy_hourly_summary_present,
    list_hourly_summaries,
    local_day_bounds,
    personal_baseline_delta,
    raw_window_summary,
    summary_active_time_range,
    summary_raw_provenance_complete,
    timezone_name,
)
from healthmes.timezones import parse_timezone

FOCUS_FRAGMENTED_LAUNCHES_PER_HOUR = 12.0
FOCUS_SUSTAINED_BLOCK_MINUTES = 45.0
OVERWORK_TOTAL_MINUTES = 600.0
OVERWORK_LONG_BLOCK_MINUTES = 120.0
OVERWORK_LATE_MINUTES = 60.0
OVERWORK_BASELINE_DELTA_MINUTES = 120.0
MIN_CONTEXT_COVERAGE = 0.25


def _recorded_at(value: datetime) -> str:
    return (
        value.replace(tzinfo=UTC).isoformat()
        if value.tzinfo is None
        else value.astimezone(UTC).isoformat()
    )


def _tz(value: str | tzinfo) -> tzinfo:
    return parse_timezone(value)


def _timezone_name(value: str | tzinfo) -> str:
    return timezone_name(value)


def _raw_window_provenance_complete(
    session: Session,
    *,
    start: datetime,
    end: datetime,
    timezone: str | tzinfo,
    now: datetime | None,
) -> bool:
    zone = _tz(timezone)
    first = start.astimezone(zone).date()
    last = (end - timedelta(microseconds=1)).astimezone(zone).date()
    return all(
        summary_raw_provenance_complete(
            session,
            day=first + timedelta(days=offset),
            timezone=timezone,
            now=now,
        )
        for offset in range((last - first).days + 1)
    )


def activity_summary_context(
    session: Session,
    *,
    day: date,
    timezone: str | tzinfo,
    now: datetime | None = None,
) -> dict[str, Any]:
    event, payload = get_daily_summary(
        session,
        day=day,
        timezone=timezone,
        now=now,
    )
    return {
        **payload,
        "evidence_ids": [str(event.id)] if event is not None else [],
        "freshness": (
            {
                "recorded_at": (
                    event.recorded_at.replace(tzinfo=UTC).isoformat()
                    if event.recorded_at.tzinfo is None
                    else event.recorded_at.astimezone(UTC).isoformat()
                ),
                "status": "stored_summary",
            }
            if event is not None
            else {"recorded_at": None, "status": "unavailable"}
        ),
    }


def _combine_hourly(
    rows,
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    if not rows:
        return {
            "status": "insufficient_data",
            "reason": "no_activity_summary",
            "active_minutes": 0.0,
            "active_minutes_upper": 0.0,
            "launches": 0,
            "longest_block": None,
            "coverage": None,
            "evidence_ids": [],
            "freshness": {"recorded_at": None, "status": "unavailable"},
            "limitations": ["missing_is_not_zero"],
        }
    window_start = start.astimezone(UTC)
    window_end = end.astimezone(UTC)
    expected_seconds = (window_end - window_start).total_seconds()
    active = 0.0
    active_upper = 0.0
    launches = 0.0
    longest_values: list[float] = []
    known_seconds = 0.0
    has_known_coverage = False
    limitations: set[str] = set()
    selected_rows = []
    has_partial_row = False
    for row in rows:
        payload = row.payload
        row_start = _summary_window_boundary(
            payload,
            "start",
            fallback=row.observed_at,
        )
        row_end = _summary_window_boundary(
            payload,
            "end",
            fallback=row_start + timedelta(hours=1),
        )
        overlap_start = max(window_start, row_start)
        overlap_end = min(window_end, row_end)
        if overlap_end <= overlap_start:
            continue
        selected_rows.append(row)
        row_seconds = (row_end - row_start).total_seconds()
        overlap_seconds = (overlap_end - overlap_start).total_seconds()
        fraction = overlap_seconds / row_seconds if row_seconds > 0 else 0.0
        if fraction < 1.0:
            has_partial_row = True
            limitations.add("partial_hour_requires_retained_raw_events")
            continue
        row_active, row_active_upper = summary_active_time_range(
            payload
        )
        active += row_active * fraction
        active_upper += row_active_upper * fraction
        launches += int(payload.get("app_launches_or_switches") or 0) * fraction
        longest = payload.get("longest_active_block_minutes")
        if longest is not None:
            longest_values.append(min(float(longest), overlap_seconds / 60.0))
        coverage = payload.get("source_coverage", {})
        if coverage.get("ratio") is not None:
            has_known_coverage = True
            known_seconds += min(
                overlap_seconds,
                float(coverage.get("known_seconds") or 0) * fraction,
            )
        limitations.update(payload.get("limitations", []))
    if not selected_rows or has_partial_row:
        return {
            "status": "insufficient_data",
            "reason": (
                "partial_hour_requires_raw"
                if has_partial_row
                else "no_activity_summary"
            ),
            "active_minutes": 0.0,
            "active_minutes_upper": 0.0,
            "launches": 0,
            "longest_block": None,
            "coverage": None,
            "evidence_ids": [],
            "freshness": {"recorded_at": None, "status": "unavailable"},
            "limitations": ["missing_is_not_zero"],
        }
    coverage = (
        min(1.0, known_seconds / expected_seconds)
        if has_known_coverage and expected_seconds > 0
        else None
    )
    return {
        "status": "ok",
        "active_minutes": round(active, 2),
        "active_minutes_upper": round(active_upper, 2),
        "active_time_range": {
            "lower_bound_minutes": round(active, 2),
            "upper_bound_minutes": round(active_upper, 2),
            "precision": (
                "exact"
                if active_upper - active <= 0.01
                else "bounded"
            ),
        },
        "launches": int(round(launches)),
        "longest_block": max(longest_values) if longest_values else None,
        "coverage": round(coverage, 4) if coverage is not None else None,
        "evidence_ids": [str(row.id) for row in selected_rows],
        "freshness": {
            "recorded_at": max(_recorded_at(row.recorded_at) for row in selected_rows),
            "status": "stored_summary",
        },
        "limitations": sorted(limitations),
    }


def _summary_window_boundary(
    payload: dict[str, Any],
    key: str,
    *,
    fallback: datetime,
) -> datetime:
    raw = payload.get("window", {}).get(key)
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            pass
        else:
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return fallback.replace(tzinfo=UTC) if fallback.tzinfo is None else fallback.astimezone(UTC)


def focus_context(
    session: Session,
    *,
    start: datetime,
    end: datetime,
    timezone: str | tzinfo,
    now: datetime | None = None,
) -> dict[str, Any]:
    name = _timezone_name(timezone)
    raw = raw_window_summary(
        session,
        start=start,
        end=end,
        timezone=timezone,
        now=now,
    )
    if raw["status"] == "ok" and _raw_window_provenance_complete(
        session,
        start=start,
        end=end,
        timezone=timezone,
        now=now,
    ):
        raw_active, raw_active_upper = summary_active_time_range(raw)
        combined = {
            "status": "ok",
            "active_minutes": raw_active,
            "active_minutes_upper": raw_active_upper,
            "active_time_range": raw.get(
                "active_time_range",
                {
                    "lower_bound_minutes": raw_active,
                    "upper_bound_minutes": raw_active_upper,
                    "precision": "exact",
                },
            ),
            "launches": int(raw.get("app_launches_or_switches") or 0),
            "longest_block": raw.get("longest_active_block_minutes"),
            "coverage": raw.get("source_coverage", {}).get("ratio"),
            "evidence_ids": list(raw.get("_evidence_event_ids", [])),
            "freshness": {
                "recorded_at": raw.get("_freshest_recorded_at"),
                "status": "retained_raw_window",
            },
            "limitations": sorted(
                {
                    *raw.get("limitations", []),
                    "exact_window_from_retained_raw_events",
                }
            ),
        }
    else:
        rows = list_hourly_summaries(
            session,
            start=start,
            end=end,
            timezone=timezone,
            now=now,
        )
        combined = _combine_hourly(
            rows,
            start=start,
            end=end,
        )
        if (
            combined.get("reason") == "no_activity_summary"
            and legacy_hourly_summary_present(
                session,
                start=start,
                end=end,
                timezone=timezone,
                now=now,
            )
        ):
            combined["reason"] = LEGACY_SUMMARY_REASON
            combined["limitations"] = sorted(
                {
                    *combined.get("limitations", []),
                    LEGACY_SUMMARY_REASON,
                }
            )
    if (
        combined["status"] != "ok"
        or combined.get(
            "active_minutes_upper",
            combined["active_minutes"],
        )
        <= 0
        or (combined["coverage"] is not None and combined["coverage"] < MIN_CONTEXT_COVERAGE)
    ):
        reason = combined.get("reason", "no_active_minutes")
        if combined["coverage"] is not None and combined["coverage"] < MIN_CONTEXT_COVERAGE:
            reason = "low_source_coverage"
        return {
            "status": "insufficient_data",
            "window": {
                "start": start.astimezone(UTC).isoformat(),
                "end": end.astimezone(UTC).isoformat(),
                "timezone": name,
            },
            "reason": reason,
            "evidence_ids": combined["evidence_ids"],
            "coverage": combined["coverage"],
            "freshness": combined["freshness"],
            "limitations": combined["limitations"],
        }
    active_upper = float(
        combined.get("active_minutes_upper", combined["active_minutes"])
    )
    active_time_bounded = active_upper - combined["active_minutes"] > 0.01
    active_hours = combined["active_minutes"] / 60.0
    launches_per_hour = (
        combined["launches"] / active_hours
        if active_hours > 0 and not active_time_bounded
        else None
    )
    longest = combined["longest_block"]
    if active_time_bounded:
        classification = "mixed_or_unknown"
    elif (
        launches_per_hour is not None
        and launches_per_hour
        >= FOCUS_FRAGMENTED_LAUNCHES_PER_HOUR
    ):
        classification = "fragmented"
    elif longest is not None and longest >= FOCUS_SUSTAINED_BLOCK_MINUTES:
        classification = "sustained"
    else:
        classification = "mixed_or_unknown"
    limitations = list(combined["limitations"])
    if longest is None:
        limitations.append("exact_focus_blocks_unavailable_for_hourly_sources")
    if combined["coverage"] is None:
        limitations.append("coverage_unknown")
    if active_time_bounded:
        if (
            "partial_hourly_activity_time_bounded"
            in combined["limitations"]
        ):
            limitations.append(
                "focus_thresholds_blocked_by_partial_hour_uncertainty"
            )
        else:
            limitations.append(
                "focus_thresholds_blocked_by_cross_device_overlap"
            )
    bounded_reason = None
    if active_time_bounded:
        bounded_reason = (
            "partial_hourly_activity_time_bounded"
            if "partial_hourly_activity_time_bounded"
            in combined["limitations"]
            else "cross_device_activity_time_bounded"
        )
    return {
        "status": "partial" if active_time_bounded else "ok",
        "window": {
            "start": start.astimezone(UTC).isoformat(),
            "end": end.astimezone(UTC).isoformat(),
            "timezone": name,
        },
        "classification": classification,
        "reason": bounded_reason,
        "metrics": {
            "total_active_minutes": combined["active_minutes"],
            "active_time_range": combined["active_time_range"],
            "app_launches_or_switches": combined["launches"],
            "launches_or_switches_per_active_hour": (
                round(launches_per_hour, 2)
                if launches_per_hour is not None
                else None
            ),
            "longest_active_block_minutes": longest,
        },
        "coverage": combined["coverage"],
        "evidence_ids": combined["evidence_ids"],
        "freshness": combined["freshness"],
        "limitations": sorted(set(limitations)),
        "boundary": "association_not_causation",
    }


def overwork_context(
    session: Session,
    *,
    day: date,
    timezone: str | tzinfo,
    lookback_days: int = 7,
    now: datetime | None = None,
) -> dict[str, Any]:
    name = _timezone_name(timezone)
    event, summary = get_daily_summary(
        session,
        day=day,
        timezone=timezone,
        now=now,
    )
    if event is None:
        return {
            **summary,
            "lookback_days": lookback_days,
            "risk_level": "unknown",
            "signals": [],
            "evidence_ids": [],
            "freshness": {"recorded_at": None, "status": "unavailable"},
        }
    coverage_ratio = summary.get("source_coverage", {}).get("ratio")
    if coverage_ratio is not None and float(coverage_ratio) < MIN_CONTEXT_COVERAGE:
        return {
            "status": "insufficient_data",
            "date": day.isoformat(),
            "timezone": name,
            "lookback_days": lookback_days,
            "risk_level": "unknown",
            "reason": "low_source_coverage",
            "signals": [],
            "coverage": summary.get("source_coverage"),
            "evidence_ids": [str(event.id)],
            "freshness": {
                "recorded_at": _recorded_at(event.recorded_at),
                "status": "stored_summary",
            },
            "limitations": sorted(set([*summary.get("limitations", []), "low_source_coverage"])),
        }
    signals: list[dict[str, Any]] = []
    total, total_upper = summary_active_time_range(summary)
    total_bounded = total_upper - total > 0.01
    longest = summary.get("longest_active_block_minutes")
    late = float(summary.get("late_activity_minutes") or 0)
    raw_late_range = summary.get("late_activity_time_range")
    late_upper = (
        float(raw_late_range.get("upper_bound_minutes"))
        if isinstance(raw_late_range, dict)
        and isinstance(
            raw_late_range.get("upper_bound_minutes"),
            int | float,
        )
        and not isinstance(
            raw_late_range.get("upper_bound_minutes"),
            bool,
        )
        else late
    )
    late_upper = max(late, late_upper)
    late_bounded = late_upper - late > 0.01
    baseline = (
        {
            "status": "insufficient_data",
            "reason": "cross_device_activity_time_bounded",
            "lookback_days": lookback_days,
        }
        if total_bounded
        else personal_baseline_delta(
            session,
            day=day,
            timezone=name,
            current_minutes=total,
            lookback_days=lookback_days,
            now=now,
        )
    )
    if total >= OVERWORK_TOTAL_MINUTES:
        signals.append(
            {
                "kind": "high_total_activity",
                "value_minutes": total,
                "threshold_minutes": OVERWORK_TOTAL_MINUTES,
            }
        )
    if longest is not None and float(longest) >= OVERWORK_LONG_BLOCK_MINUTES:
        signals.append(
            {
                "kind": "long_continuous_activity",
                "value_minutes": float(longest),
                "threshold_minutes": OVERWORK_LONG_BLOCK_MINUTES,
            }
        )
    if late >= OVERWORK_LATE_MINUTES:
        signals.append(
            {
                "kind": "late_activity",
                "value_minutes": late,
                "threshold_minutes": OVERWORK_LATE_MINUTES,
            }
        )
    threshold_uncertainties: list[str] = []
    if total < OVERWORK_TOTAL_MINUTES <= total_upper:
        threshold_uncertainties.append("high_total_activity")
    if late < OVERWORK_LATE_MINUTES <= late_upper:
        threshold_uncertainties.append("late_activity")
    delta = baseline.get("delta_minutes") if baseline.get("status") == "ok" else None
    if delta is not None and float(delta) >= OVERWORK_BASELINE_DELTA_MINUTES:
        signals.append(
            {
                "kind": "above_personal_baseline",
                "value_minutes": float(delta),
                "threshold_minutes": OVERWORK_BASELINE_DELTA_MINUTES,
            }
        )
    coverage_unknown = coverage_ratio is None
    threshold_decision_bounded = bool(
        threshold_uncertainties
        or total_bounded
        or late_bounded
    )
    risk = (
        "high"
        if len(signals) >= 2
        else "elevated"
        if signals
        else "unknown"
        if coverage_unknown or threshold_decision_bounded
        else "not_elevated"
    )
    limitations = list(summary.get("limitations", []))
    if coverage_unknown:
        limitations.append("coverage_unknown")
    if threshold_decision_bounded:
        limitations.append(
            "overwork_thresholds_blocked_by_cross_device_overlap"
        )
    return {
        "status": (
            "partial"
            if threshold_decision_bounded
            or (coverage_unknown and signals)
            else "insufficient_data"
            if coverage_unknown
            else "ok"
        ),
        "date": day.isoformat(),
        "timezone": name,
        "lookback_days": lookback_days,
        "risk_level": risk,
        "reason": (
            "unknown_source_coverage"
            if coverage_unknown
            else "cross_device_activity_time_bounded"
            if threshold_decision_bounded
            else None
        ),
        "signals": signals,
        "threshold_uncertainties": threshold_uncertainties,
        "metrics": {
            "total_active_minutes": total,
            "active_time_range": {
                "lower_bound_minutes": total,
                "upper_bound_minutes": total_upper,
                "precision": (
                    "bounded" if total_bounded else "exact"
                ),
            },
            "longest_active_block_minutes": longest,
            "idle_and_break_minutes": summary.get("idle_and_break_minutes"),
            "late_activity_minutes": late,
            "late_activity_time_range": {
                "lower_bound_minutes": late,
                "upper_bound_minutes": late_upper,
                "precision": (
                    "bounded" if late_bounded else "exact"
                ),
            },
            "seven_day_baseline_delta": summary.get("seven_day_baseline_delta"),
            "lookback_baseline_delta": baseline,
        },
        "coverage": summary.get("source_coverage"),
        "evidence_ids": [str(event.id)],
        "freshness": {
            "recorded_at": _recorded_at(event.recorded_at),
            "status": "stored_summary",
        },
        "limitations": sorted(set(limitations)),
        "boundary": "wellness_context_not_diagnosis",
    }


def recovery_activity_context(
    session: Session,
    *,
    day: date,
    timezone: str | tzinfo,
    now: datetime | None = None,
) -> dict[str, Any]:
    summary = activity_summary_context(
        session,
        day=day,
        timezone=timezone,
        now=now,
    )
    if summary["status"] != "ok":
        return {
            "status": "insufficient_data",
            "date": day.isoformat(),
            "timezone": _timezone_name(timezone),
            "reason": "no_activity_summary",
            "evidence_ids": summary["evidence_ids"],
            "freshness": summary["freshness"],
            "coverage": summary.get("source_coverage"),
            "limitations": summary.get("limitations", []),
        }
    active_minutes, active_minutes_upper = (
        summary_active_time_range(summary)
    )
    active_time_bounded = (
        active_minutes_upper - active_minutes > 0.01
    )
    return {
        "status": "partial" if active_time_bounded else "ok",
        "date": day.isoformat(),
        "timezone": _timezone_name(timezone),
        "reason": (
            "cross_device_activity_time_bounded"
            if active_time_bounded
            else None
        ),
        "metrics": {
            "total_active_minutes": active_minutes,
            "active_time_range": {
                "lower_bound_minutes": active_minutes,
                "upper_bound_minutes": active_minutes_upper,
                "precision": (
                    "bounded" if active_time_bounded else "exact"
                ),
            },
            "idle_and_break_minutes": summary["idle_and_break_minutes"],
            "late_activity_minutes": summary["late_activity_minutes"],
            "longest_active_block_minutes": summary["longest_active_block_minutes"],
        },
        "coverage": summary["source_coverage"],
        "evidence_ids": summary["evidence_ids"],
        "freshness": summary["freshness"],
        "limitations": summary.get("limitations", []),
        "boundary": "activity_only_recovery_context",
    }


def default_focus_window(day: date, timezone: str | tzinfo) -> tuple[datetime, datetime]:
    start, end = local_day_bounds(day, timezone)
    return start, min(end, datetime.now(UTC) + timedelta(seconds=1))
