"""Phase-2 focus insight template (docs/PLAN.md Phase 2, constrained by §11).

One fixed deterministic template — no free-form data mining:

- ``focus_drop_by_hour``  per-hour-of-day cognitive-energy dips vs the period
  baseline, joined with co-occurring factors from the local store: short sleep
  (the engine-recorded sleep-debt index inside the dip windows), high
  app-switch frequency (``app_usage_sample`` launches inside the dip windows)
  and meeting-dense mornings (calendar-mirror events before noon on dip days).

The statement follows the plan's example wording
("14–16시 집중 저하: 수면 부족 + Slack 시간당 9회 실행")::

    14-16h focus drop (energy 47 vs baseline 72, UTC): sleep deficit +
    Slack 9 launches/hour.

Inputs are persisted ``cognitive_energy_estimate`` rows (the hourly engine
windows of docs/PLAN.md §3 — only sufficient-data windows are ever persisted),
``app_usage_sample`` rows and mirrored calendar events. Everything here is a
pure function of those rows; hours/days are bucketed in the caller-supplied
``tz`` (the user's timezone, matching the stress templates; default UTC keeps
the pure functions deterministic in isolation). The result carries
``evidence`` (the full per-hour table plus every factor check, qualified or
not) and a coverage-based ``confidence``. With too little data the template
answers ``insufficient_data`` and with a flat profile ``no_dip_detected`` — a
dip is never invented, and co-occurring factors alone can never create one.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo
from typing import Any, Protocol

from healthmes.api.common import ensure_utc
from healthmes.api.insight_templates import (
    FULL_COVERAGE_DAYS,
    CalendarEventLike,
    SkippedTemplate,
    TemplateResult,
    _coverage_confidence,
    _mean,
)

__all__ = [
    "KIND_FOCUS_DROP_BY_HOUR",
    "EnergyEstimateLike",
    "AppUsageLike",
    "compute_focus_drop",
]

KIND_FOCUS_DROP_BY_HOUR = "focus_drop_by_hour"

# Minimum-data gates (skip with ``insufficient_data`` below these).
MIN_ENERGY_WINDOWS = 24
MIN_ENERGY_DAYS = 2
MIN_WINDOWS_PER_HOUR = 2  # an hour-of-day bucket needs repeated observations
MIN_QUALIFIED_HOURS = 6  # need a real daily profile to call anything a dip

# An hour of day is a dip when its mean score sits at least this many points
# below the baseline (the mean over all windows in qualified hour buckets).
DIP_POINTS = 10.0

# Co-occurring factor thresholds (each factor is reported with its numbers
# either way; only qualifying factors enter the statement).
SLEEP_COMPONENT_NAME = "sleep_debt_penalty"
SLEEP_DEBT_INDEX_THRESHOLD = 25.0  # engine debt index = trailing mean(100 - sleep score)
LAUNCH_RATE_THRESHOLD = 6.0  # top app's launches per dip hour
MORNING_START_HOUR = 6
MORNING_END_HOUR = 12  # exclusive
MORNING_EVENTS_THRESHOLD = 3.0  # mean events starting per dip-day morning

# Reverse-DNS package segments that never make a readable app label.
_GENERIC_SEGMENTS = frozenset(
    {
        "com",
        "org",
        "net",
        "io",
        "co",
        "app",
        "apps",
        "android",
        "mobile",
        "beta",
        "lite",
        "free",
        "client",
    }
)


class EnergyEstimateLike(Protocol):
    """Structural type matched by ``healthmes.store.CognitiveEnergyEstimate``."""

    window_start: datetime
    window_end: datetime
    score: int
    components: dict[str, Any]


class AppUsageLike(Protocol):
    """Structural type matched by ``healthmes.store.AppUsageSample``."""

    bucket_start: datetime
    app_package: str
    launches: int


@dataclass(frozen=True)
class _Window:
    """One estimate window normalised (aware UTC) for aggregation."""

    start: datetime
    end: datetime
    hour: int
    day: date
    score: float
    sleep_debt_index: float | None


def _sleep_debt_index(components: Any) -> float | None:
    """The engine-recorded sleep-debt index of one window, when present.

    Reads the ``sleep_debt_penalty`` item of the persisted components payload
    (``{"version": 1, "items": [{name, kind, weight, raw, contribution}], ...}``,
    see :meth:`healthmes.engine.cognitive_energy.EnergyEstimate.components_payload`);
    ``raw.index`` is the trailing mean of ``100 - sleep score``.
    """
    if not isinstance(components, dict):
        return None
    for item in components.get("items") or ():
        if not isinstance(item, dict) or item.get("name") != SLEEP_COMPONENT_NAME:
            continue
        raw = item.get("raw")
        index = raw.get("index") if isinstance(raw, dict) else None
        if isinstance(index, int | float):
            return float(index)
    return None


def _normalize(estimates: Iterable[EnergyEstimateLike], tz: tzinfo) -> list[_Window]:
    windows: list[_Window] = []
    for row in estimates:
        start = ensure_utc(row.window_start)
        local_start = start.astimezone(tz)
        windows.append(
            _Window(
                start=start,
                end=ensure_utc(row.window_end),
                hour=local_start.hour,
                day=local_start.date(),
                score=float(row.score),
                sleep_debt_index=_sleep_debt_index(row.components),
            )
        )
    windows.sort(key=lambda window: window.start)
    return windows


def _app_label(package: str) -> str:
    """Readable app name from a (reverse-DNS) package string.

    The last non-generic segment wins: ``com.Slack`` -> ``Slack``,
    ``com.instagram.android`` -> ``Instagram``. Deterministic, no lookup table.
    """
    for segment in reversed([part for part in package.split(".") if part]):
        if segment.lower() not in _GENERIC_SEGMENTS:
            return segment.capitalize() if segment.islower() else segment
    return package


def _consecutive_blocks(hours: Sequence[int]) -> list[list[int]]:
    """Group sorted hours into runs of consecutive integers.

    Runs do not wrap across UTC midnight (23 and 0 stay separate blocks) —
    acceptable for v1 and keeps the block label unambiguous.
    """
    blocks: list[list[int]] = []
    current: list[int] = []
    for hour in hours:
        if current and hour == current[-1] + 1:
            current.append(hour)
        else:
            if current:
                blocks.append(current)
            current = [hour]
    if current:
        blocks.append(current)
    return blocks


def _sleep_factor(dip_windows: Sequence[_Window]) -> dict[str, Any]:
    """Short-sleep check: mean engine sleep-debt index across the dip windows."""
    values = [w.sleep_debt_index for w in dip_windows if w.sleep_debt_index is not None]
    if not values:
        return {
            "qualified": False,
            "status": "no_data",
            "threshold": SLEEP_DEBT_INDEX_THRESHOLD,
        }
    mean_index = _mean(values)
    return {
        "qualified": mean_index >= SLEEP_DEBT_INDEX_THRESHOLD,
        "mean_debt_index": round(mean_index, 1),
        "n_windows": len(values),
        "threshold": SLEEP_DEBT_INDEX_THRESHOLD,
    }


def _app_factor(
    dip_windows: Sequence[_Window], usage: Sequence[AppUsageLike], tz: tzinfo
) -> dict[str, Any]:
    """App-switch check: launches per hour inside the dip windows.

    A dip window only enters the denominator when its (local) day reported
    any usage at all (the collector was active) — days without a companion
    device never dilute the rate. Buckets are matched by ``bucket_start``
    falling inside a dip window, the same bucket-start convention as the
    engine's fragmentation factor.
    """
    buckets = [
        (ensure_utc(bucket.bucket_start), bucket.app_package, int(bucket.launches))
        for bucket in usage
    ]
    usage_days = {started.astimezone(tz).date() for started, _, _ in buckets}
    covered = [w for w in dip_windows if w.day in usage_days]
    hours_covered = sum((w.end - w.start).total_seconds() for w in covered) / 3600.0
    if not covered or hours_covered <= 0:
        return {"qualified": False, "status": "no_data", "threshold": LAUNCH_RATE_THRESHOLD}

    by_app: dict[str, int] = defaultdict(int)
    for started, package, launches in buckets:
        if any(w.start <= started < w.end for w in covered):
            by_app[package] += launches
    base = {
        "windows_covered": len(covered),
        "hours_covered": round(hours_covered, 2),
        "threshold": LAUNCH_RATE_THRESHOLD,
    }
    if not by_app:
        return {
            "qualified": False,
            "launches": 0,
            "launches_per_hour": 0.0,
            "total_launches": 0,
            "total_launches_per_hour": 0.0,
            **base,
        }
    top_package = min(by_app, key=lambda package: (-by_app[package], package))
    rate = by_app[top_package] / hours_covered
    total = sum(by_app.values())
    return {
        "qualified": rate >= LAUNCH_RATE_THRESHOLD,
        "app_package": top_package,
        "app_label": _app_label(top_package),
        "launches": by_app[top_package],
        "launches_per_hour": round(rate, 1),
        "total_launches": total,
        "total_launches_per_hour": round(total / hours_covered, 1),
        **base,
    }


def _meeting_factor(
    dip_days: Sequence[date], events: Iterable[CalendarEventLike], tz: tzinfo
) -> dict[str, Any]:
    """Meeting-dense-morning check: mean events starting before noon (local)."""
    starts = [ensure_utc(event.start_at).astimezone(tz) for event in events]
    if not starts:
        return {"qualified": False, "status": "no_data", "threshold": MORNING_EVENTS_THRESHOLD}
    per_day = {day: 0 for day in dip_days}
    for started in starts:
        if started.date() in per_day and MORNING_START_HOUR <= started.hour < MORNING_END_HOUR:
            per_day[started.date()] += 1
    mean_per_day = _mean(list(per_day.values()))
    return {
        "qualified": mean_per_day >= MORNING_EVENTS_THRESHOLD,
        "mean_morning_events": round(mean_per_day, 2),
        "n_days": len(dip_days),
        "morning_hours": f"{MORNING_START_HOUR:02d}:00-{MORNING_END_HOUR:02d}:00",
        "threshold": MORNING_EVENTS_THRESHOLD,
    }


def compute_focus_drop(
    estimates: Sequence[EnergyEstimateLike],
    usage: Sequence[AppUsageLike],
    events: Sequence[CalendarEventLike],
    tz: tzinfo = UTC,
) -> TemplateResult | SkippedTemplate:
    """Per-hour-of-day energy-dip insight with co-occurring factors.

    Hour-of-day buckets (in ``tz``, the user's timezone) with at least
    ``MIN_WINDOWS_PER_HOUR`` windows form the profile; the baseline is the
    mean score over those windows. Dip hours sit ``DIP_POINTS`` or more below
    the baseline; the contiguous dip block with the largest total deficit
    (ties -> earliest) is reported, with sleep / app-switching /
    morning-meeting factors checked over its windows.
    """
    windows = _normalize(estimates, tz)
    days = {window.day for window in windows}
    by_hour: dict[int, list[_Window]] = defaultdict(list)
    for window in windows:
        by_hour[window.hour].append(window)
    qualified = {
        hour: bucket for hour, bucket in by_hour.items() if len(bucket) >= MIN_WINDOWS_PER_HOUR
    }
    if (
        len(windows) < MIN_ENERGY_WINDOWS
        or len(days) < MIN_ENERGY_DAYS
        or len(qualified) < MIN_QUALIFIED_HOURS
    ):
        return SkippedTemplate(KIND_FOCUS_DROP_BY_HOUR)

    profile_windows = [window for bucket in qualified.values() for window in bucket]
    baseline = _mean([window.score for window in profile_windows])
    hour_means = {hour: _mean([w.score for w in bucket]) for hour, bucket in qualified.items()}
    deficits = {hour: baseline - mean for hour, mean in hour_means.items()}
    dip_hours = sorted(hour for hour, deficit in deficits.items() if deficit >= DIP_POINTS)
    if not dip_hours:
        return SkippedTemplate(KIND_FOCUS_DROP_BY_HOUR, "no_dip_detected")

    blocks = _consecutive_blocks(dip_hours)
    block = max(blocks, key=lambda hours: (sum(deficits[hour] for hour in hours), -hours[0]))
    dip_windows = [window for hour in block for window in qualified[hour]]
    dip_mean = _mean([window.score for window in dip_windows])
    dip_days = sorted({window.day for window in dip_windows})
    label = f"{block[0]:02d}-{block[-1] + 1:02d}h"

    factors = {
        "sleep_deficit": _sleep_factor(dip_windows),
        "app_switching": _app_factor(dip_windows, usage, tz),
        "meeting_dense_mornings": _meeting_factor(dip_days, events, tz),
    }
    phrases: list[str] = []
    if factors["sleep_deficit"]["qualified"]:
        phrases.append("sleep deficit")
    app = factors["app_switching"]
    if app["qualified"]:
        phrases.append(f"{app['app_label']} {app['launches_per_hour']:.0f} launches/hour")
    if factors["meeting_dense_mornings"]["qualified"]:
        phrases.append("meeting-dense mornings")
    factor_text = " + ".join(phrases) if phrases else "no co-occurring factor identified"

    statement = (
        f"{label} focus drop (energy {dip_mean:.0f} vs baseline {baseline:.0f}, {tz}): "
        f"{factor_text}."
    )
    evidence = {
        "timezone": str(tz),
        "baseline_mean": round(baseline, 2),
        "dip_threshold_points": DIP_POINTS,
        "n_windows": len(profile_windows),
        "n_windows_total": len(windows),
        "n_days": len(days),
        "by_hour": {
            str(hour): {"mean": round(hour_means[hour], 2), "n": len(qualified[hour])}
            for hour in sorted(qualified)
        },
        "unqualified_hours": {
            str(hour): len(bucket)
            for hour, bucket in sorted(by_hour.items())
            if hour not in qualified
        },
        "dip_hours": dip_hours,
        "block": {
            "label": label,
            "start_hour": block[0],
            "end_hour": block[-1] + 1,
            "hours": list(block),
            "n_windows": len(dip_windows),
            "mean": round(dip_mean, 2),
            "deficit": round(baseline - dip_mean, 2),
            "days": [day.isoformat() for day in dip_days],
        },
        "factors": factors,
    }
    confidence = _coverage_confidence(len(days), FULL_COVERAGE_DAYS)
    return TemplateResult(KIND_FOCUS_DROP_BY_HOUR, statement, evidence, confidence)
