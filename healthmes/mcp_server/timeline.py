"""Pure stress-timeline logic for the ``get_stress_timeline`` tool.

Turns a raw Garmin stress series into *interpreted intervals* (docs/PLAN.md
1.5: Layer B tools never dump raw series) and joins each interval with its
likely context — calendar event titles and dominant app-usage categories.

All datetimes entering these functions are expected to be timezone-aware and
already converted to the **user's local timezone** by the caller: the plan's
"joins happen in local time" rule is load-bearing (a 23:50 sample belongs to
the local evening, not to the next UTC day). Nothing here talks to the store
or the network.

Fixed, documented policy (so results are reproducible and hand-checkable):

- **Stress bands** are Garmin's official ones (the series is
  ``garmin_stress_level``, 0-100 — ``vendor/open-wearables/backend/app/
  schemas/enums/series_types.py``; ingest already drops negative
  "unmeasurable" values, ``providers/garmin/data_247.py``):
  0-25 rest, 26-50 low, 51-75 medium, 76-100 high.
- **Sample step** = the median gap between consecutive samples of the day
  (capped at ``max_gap_minutes``); an interval ends one step after its last
  sample.
- **Intervals** are maximal runs of same-band samples; a gap larger than
  ``max_gap_minutes`` always starts a new interval (data holes are never
  papered over).
- **Smoothing**: a run shorter than ``min_run_minutes`` that touches its
  predecessor is absorbed into it (single-sample band blips do not deserve
  their own interval); touching same-band runs then merge. Absorbed samples
  keep contributing to ``mean``/``peak`` — evidence is preserved, only the
  labeling is smoothed.
- **App-usage buckets** are attributed to intervals proportionally to time
  overlap, assuming the collector's fixed bucket length
  (``bucket_minutes``, default 60 — the Android collector reports hourly
  buckets, docs/PLAN.md section 7).
"""

import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any

__all__ = [
    "GARMIN_STRESS_BANDS",
    "MAX_GAP_MINUTES",
    "FALLBACK_STEP_MINUTES",
    "MIN_RUN_MINUTES",
    "PROXY_SECTIONS",
    "StressInterval",
    "stress_label",
    "build_stress_intervals",
    "proxy_sections",
    "attach_context",
    "day_coverage",
    "timeline_confidence",
    "serialize_interval",
]

# Garmin's stress bands: (upper bound inclusive, label). Values are 0-100.
GARMIN_STRESS_BANDS: tuple[tuple[int, str], ...] = (
    (25, "rest"),
    (50, "low"),
    (75, "medium"),
    (100, "high"),
)

MAX_GAP_MINUTES = 30.0
FALLBACK_STEP_MINUTES = 15.0
MIN_RUN_MINUTES = 10.0

# Waking-day sections used when only a day-level proxy stress value exists
# (night-HRV / resilience derived — no intraday resolution to segment on).
# The sleeping 00-06 block is deliberately excluded: labeling sleep hours
# with a waking-stress value would be dishonest.
PROXY_SECTIONS: tuple[tuple[int, int, str], ...] = (
    (6, 12, "morning"),
    (12, 18, "afternoon"),
    (18, 24, "evening"),
)


@dataclass(frozen=True, slots=True)
class StressInterval:
    """One interpreted stress interval (local-tz aware datetimes)."""

    start: datetime
    end: datetime
    label: str
    mean: float
    peak: float
    n_samples: int

    @property
    def duration_minutes(self) -> float:
        return (_utc(self.end) - _utc(self.start)).total_seconds() / 60.0


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def stress_label(value: float) -> str:
    """Garmin stress band label for a 0-100 stress value."""
    for upper, label in GARMIN_STRESS_BANDS:
        if value <= upper:
            return label
    return GARMIN_STRESS_BANDS[-1][1]


def _median_step_minutes(points: Sequence[tuple[datetime, float]]) -> float:
    gaps = [
        (_utc(points[i + 1][0]) - _utc(points[i][0])).total_seconds() / 60.0
        for i in range(len(points) - 1)
        if _utc(points[i + 1][0]) > _utc(points[i][0])
    ]
    if not gaps:
        return FALLBACK_STEP_MINUTES
    return min(float(statistics.median(gaps)), MAX_GAP_MINUTES)


@dataclass
class _Run:
    label: str
    points: list[tuple[datetime, float]]
    end: datetime

    @property
    def start(self) -> datetime:
        return self.points[0][0]

    @property
    def duration_minutes(self) -> float:
        return (_utc(self.end) - _utc(self.start)).total_seconds() / 60.0

    def absorb(self, other: "_Run") -> None:
        self.points.extend(other.points)
        self.points.sort(key=lambda item: _utc(item[0]))
        self.end = max((self.end, other.end), key=_utc)


def build_stress_intervals(
    samples: Iterable[tuple[datetime, float]],
    *,
    max_gap_minutes: float = MAX_GAP_MINUTES,
    min_run_minutes: float = MIN_RUN_MINUTES,
) -> list[StressInterval]:
    """Collapse a stress sample series into labeled, gap-aware intervals.

    ``samples`` are (aware local datetime, value 0-100) pairs in any order;
    negative values (Garmin "unmeasurable") are dropped defensively even
    though the vendor ingest already filters them.
    """
    points = sorted(
        (
            (ts, float(value))
            for ts, value in samples
            if value is not None and float(value) >= 0.0
        ),
        key=lambda item: _utc(item[0]),
    )
    if not points:
        return []

    step = timedelta(minutes=_median_step_minutes(points))
    max_gap = timedelta(minutes=max_gap_minutes)

    # 1. maximal runs of same-band samples, split on gaps > max_gap.
    runs: list[_Run] = []
    for ts, value in points:
        label = stress_label(value)
        end = (_utc(ts) + step).astimezone(ts.tzinfo)
        if (
            runs
            and runs[-1].label == label
            and _utc(ts) - _utc(runs[-1].points[-1][0]) <= max_gap
        ):
            runs[-1].points.append((ts, value))
            runs[-1].end = end
        else:
            runs.append(_Run(label=label, points=[(ts, value)], end=end))

    # 2. smoothing: absorb short touching runs into their predecessor, then
    #    merge touching same-band neighbors created by the absorption.
    merged: list[_Run] = []
    for run in runs:
        if merged:
            prev = merged[-1]
            touching = _utc(run.start) <= _utc(prev.end)
            if touching and run.label == prev.label:
                prev.absorb(run)
                continue
            if touching and run.duration_minutes < min_run_minutes:
                prev.absorb(run)
                continue
        merged.append(run)
    # The first run cannot absorb backwards; fold it forward when short.
    if (
        len(merged) >= 2
        and merged[0].duration_minutes < min_run_minutes
        and _utc(merged[1].start) <= _utc(merged[0].end)
    ):
        merged[1].absorb(merged[0])
        merged = merged[1:]

    return [
        StressInterval(
            start=run.start,
            end=run.end,
            label=run.label,
            mean=sum(value for _, value in run.points) / len(run.points),
            peak=max(value for _, value in run.points),
            n_samples=len(run.points),
        )
        for run in merged
    ]


def proxy_sections(
    day: date,
    tz: tzinfo,
    stress_value: float,
    *,
    sections: tuple[tuple[int, int, str], ...] = PROXY_SECTIONS,
) -> list[StressInterval]:
    """Waking-day sections all carrying one day-level proxy stress value.

    Used when no intraday stress series exists: the value (typically
    ``100 - resilience score``, night-HRV derived) has no intraday
    resolution, so the sections only exist to be joined with per-section
    context — the honest alternative to inventing an intraday curve.
    """
    intervals: list[StressInterval] = []
    for start_hour, end_hour, _name in sections:
        start = datetime.combine(day, time(hour=start_hour), tzinfo=tz)
        if end_hour == 24:
            end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
        else:
            end = datetime.combine(day, time(hour=end_hour), tzinfo=tz)
        intervals.append(
            StressInterval(
                start=start,
                end=end,
                label=stress_label(stress_value),
                mean=float(stress_value),
                peak=float(stress_value),
                n_samples=0,
            )
        )
    return intervals


def attach_context(
    window_start: datetime,
    window_end: datetime,
    events: Sequence[tuple[datetime, datetime, str | None]],
    usage: Sequence[tuple[datetime, str | None, int]],
    *,
    bucket_minutes: float = 60.0,
    max_events: int = 4,
    max_categories: int = 2,
    min_category_minutes: float = 5.0,
) -> list[str]:
    """Likely-context strings for one interval (calendar titles + app categories).

    ``events`` are (start, end, summary) tuples; ``usage`` are
    (bucket_start, category, foreground_seconds) tuples — all aware, local tz.
    App buckets are attributed proportionally to their overlap with the
    interval (a bucket is ``[bucket_start, bucket_start + bucket_minutes)``).
    """
    context: list[str] = []
    window_start_utc = _utc(window_start)
    window_end_utc = _utc(window_end)

    overlapping = sorted(
        (
            (start, end, summary)
            for start, end, summary in events
            if _utc(start) < window_end_utc and _utc(end) > window_start_utc
        ),
        key=lambda item: (_utc(item[0]), item[2] or ""),
    )
    for _start, _end, summary in overlapping[:max_events]:
        title = (summary or "").strip() or "(untitled)"
        context.append(f"event: {title}")

    bucket_span = timedelta(minutes=bucket_minutes)
    minutes_by_category: dict[str, float] = {}
    for bucket_start, category, foreground_seconds in usage:
        bucket_start_utc = _utc(bucket_start)
        bucket_end_utc = bucket_start_utc + bucket_span
        overlap = min(bucket_end_utc, window_end_utc) - max(
            bucket_start_utc, window_start_utc
        )
        overlap_seconds = overlap.total_seconds()
        if overlap_seconds <= 0:
            continue
        fraction = overlap_seconds / bucket_span.total_seconds()
        key = (category or "uncategorized").lower()
        minutes_by_category[key] = (
            minutes_by_category.get(key, 0.0) + foreground_seconds * fraction / 60.0
        )
    ranked = sorted(minutes_by_category.items(), key=lambda item: (-item[1], item[0]))
    for category, minutes in ranked[:max_categories]:
        if minutes < min_category_minutes:
            continue
        context.append(f"apps: {category} ({round(minutes)} min)")
    return context


def day_coverage(intervals: Sequence[StressInterval]) -> float:
    """Fraction of the 24h day covered by intervals, rounded to 2 decimals."""
    covered = sum(interval.duration_minutes for interval in intervals)
    return round(min(covered / (24.0 * 60.0), 1.0), 2)


def timeline_confidence(coverage: float) -> str:
    """Confidence bucket for an intraday stress timeline by day coverage."""
    if coverage >= 0.6:
        return "high"
    if coverage >= 0.3:
        return "medium"
    return "low"


def serialize_interval(interval: StressInterval, likely_context: list[str]) -> dict[str, Any]:
    """The wire shape of one interval (local ISO datetimes, rounded stats)."""
    return {
        "window": {
            "start": interval.start.isoformat(),
            "end": interval.end.isoformat(),
            "duration_minutes": round(interval.duration_minutes, 1),
        },
        "stress_level": interval.label,
        "stress_mean": round(interval.mean, 1),
        "stress_peak": round(interval.peak, 1),
        "n_samples": interval.n_samples,
        "likely_context": likely_context,
    }
