"""Phase-1 template-SQL correlation insights (docs/PLAN.md Phase 1).

Four fixed templates — no free-form data mining:

- ``stress_by_hour``            stress level by hour of day (user's timezone)
- ``stress_by_weekday``         stress level by weekday (user's timezone)
- ``stress_by_calendar_keyword`` stress during calendar events grouped by
                                 keywords from mirrored event summaries
- ``activity_type_vs_stress``   stress change after workouts, by workout type

Everything here is a pure, deterministic function of its inputs: stress
samples and workouts come from the shared open-wearables client
(:mod:`healthmes.mcp_server.ow_client`, via :mod:`healthmes.api.insights`),
calendar events from the local store. Hour/weekday bucketing happens in the
caller-supplied ``tz`` (the user's timezone — a 22:00 KST stress peak must
not be reported as "13:00"); the default UTC keeps the pure functions
deterministic in isolation. Each result carries ``evidence`` (the full
aggregation table) and a ``confidence`` in [0, 1] derived from data coverage;
templates with too little data are skipped with reason ``insufficient_data``
instead of guessing.
"""

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, Protocol

from healthmes.api.common import ensure_utc

__all__ = [
    "KIND_STRESS_BY_HOUR",
    "KIND_STRESS_BY_WEEKDAY",
    "KIND_STRESS_BY_CALENDAR_KEYWORD",
    "KIND_ACTIVITY_TYPE_VS_STRESS",
    "ALL_KINDS",
    "StressSample",
    "WorkoutEvent",
    "TemplateResult",
    "SkippedTemplate",
    "CalendarEventLike",
    "compute_stress_by_hour",
    "compute_stress_by_weekday",
    "compute_stress_by_calendar_keyword",
    "compute_activity_type_vs_stress",
    "compute_all",
]


@dataclass(frozen=True)
class StressSample:
    """One stress time-series sample (timestamp normalised to aware UTC)."""

    timestamp: datetime
    value: float


@dataclass(frozen=True)
class WorkoutEvent:
    """One workout event (times normalised to aware UTC)."""

    workout_type: str
    start_time: datetime
    end_time: datetime

KIND_STRESS_BY_HOUR = "stress_by_hour"
KIND_STRESS_BY_WEEKDAY = "stress_by_weekday"
KIND_STRESS_BY_CALENDAR_KEYWORD = "stress_by_calendar_keyword"
KIND_ACTIVITY_TYPE_VS_STRESS = "activity_type_vs_stress"
ALL_KINDS: tuple[str, ...] = (
    KIND_STRESS_BY_HOUR,
    KIND_STRESS_BY_WEEKDAY,
    KIND_STRESS_BY_CALENDAR_KEYWORD,
    KIND_ACTIVITY_TYPE_VS_STRESS,
)

# Minimum-data gates (skip with ``insufficient_data`` below these).
MIN_STRESS_SAMPLES = 12
MIN_DISTINCT_HOURS = 3
MIN_DISTINCT_WEEKDAYS = 2
MIN_KEYWORD_EVENTS = 2
MIN_KEYWORD_SAMPLES = 4
MIN_WINDOW_SAMPLES = 2
MIN_WORKOUTS_PER_TYPE = 2

# Confidence = covered amount / "full coverage" amount, capped at 1.0.
FULL_COVERAGE_DAYS = 14
FULL_KEYWORD_SAMPLES = 30
FULL_WORKOUTS = 10

# Stress window compared around each workout (before start / after end).
WORKOUT_WINDOW = timedelta(hours=2)

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

_WORD_RE = re.compile(r"[a-z0-9]+")
# Tiny English stopword list for calendar-summary keyword extraction.
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "this",
        "that",
        "your",
        "our",
        "new",
        "all",
        "into",
        "over",
        "about",
        "weekly",
        "daily",
    }
)


class CalendarEventLike(Protocol):
    """Structural type matched by ``healthmes.store.CalendarEventMirror``."""

    summary: str | None
    start_at: datetime
    end_at: datetime


@dataclass(frozen=True)
class TemplateResult:
    """One computed insight, ready to persist as an ``insight`` row."""

    kind: str
    statement: str
    evidence: dict[str, Any]
    confidence: float


@dataclass(frozen=True)
class SkippedTemplate:
    """A template that did not have enough data to say anything honest."""

    kind: str
    reason: str = "insufficient_data"


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _coverage_confidence(covered: float, full: float) -> float:
    return round(min(1.0, covered / full), 3)


def _distinct_days(samples: Sequence[StressSample], tz: tzinfo) -> int:
    return len({sample.timestamp.astimezone(tz).date() for sample in samples})


def compute_stress_by_hour(
    samples: Sequence[StressSample], tz: tzinfo = UTC
) -> TemplateResult | None:
    """Mean stress per hour of day in ``tz``; statement highlights the peak hour."""
    by_hour: dict[int, list[float]] = defaultdict(list)
    for sample in samples:
        by_hour[sample.timestamp.astimezone(tz).hour].append(sample.value)
    if len(samples) < MIN_STRESS_SAMPLES or len(by_hour) < MIN_DISTINCT_HOURS:
        return None

    means = {hour: _mean(values) for hour, values in by_hour.items()}
    overall = _mean([sample.value for sample in samples])
    peak_hour = min(means, key=lambda hour: (-means[hour], hour))
    low_hour = min(means, key=lambda hour: (means[hour], hour))
    evidence = {
        "by_hour": {
            str(hour): {"mean": round(means[hour], 2), "n": len(by_hour[hour])}
            for hour in sorted(by_hour)
        },
        "overall_mean": round(overall, 2),
        "n_samples": len(samples),
        "peak_hour": peak_hour,
        "low_hour": low_hour,
        "timezone": str(tz),
    }
    statement = (
        f"Stress peaks around {peak_hour:02d}:00 {tz} "
        f"(avg {means[peak_hour]:.0f} vs overall {overall:.0f})."
    )
    confidence = _coverage_confidence(_distinct_days(samples, tz), FULL_COVERAGE_DAYS)
    return TemplateResult(KIND_STRESS_BY_HOUR, statement, evidence, confidence)


def compute_stress_by_weekday(
    samples: Sequence[StressSample], tz: tzinfo = UTC
) -> TemplateResult | None:
    """Mean stress per weekday in ``tz``; statement names the peak weekday."""
    by_weekday: dict[int, list[float]] = defaultdict(list)
    for sample in samples:
        by_weekday[sample.timestamp.astimezone(tz).weekday()].append(sample.value)
    if len(samples) < MIN_STRESS_SAMPLES or len(by_weekday) < MIN_DISTINCT_WEEKDAYS:
        return None

    means = {weekday: _mean(values) for weekday, values in by_weekday.items()}
    overall = _mean([sample.value for sample in samples])
    peak = min(means, key=lambda weekday: (-means[weekday], weekday))
    evidence = {
        "by_weekday": {
            WEEKDAY_KEYS[weekday]: {
                "mean": round(means[weekday], 2),
                "n": len(by_weekday[weekday]),
            }
            for weekday in sorted(by_weekday)
        },
        "overall_mean": round(overall, 2),
        "n_samples": len(samples),
        "peak_weekday": WEEKDAY_KEYS[peak],
        "timezone": str(tz),
    }
    statement = (
        f"Stress is highest on {WEEKDAY_NAMES[peak]} "
        f"(avg {means[peak]:.0f} vs overall {overall:.0f})."
    )
    confidence = _coverage_confidence(_distinct_days(samples, tz), FULL_COVERAGE_DAYS)
    return TemplateResult(KIND_STRESS_BY_WEEKDAY, statement, evidence, confidence)


def _summary_keywords(summary: str) -> set[str]:
    return {
        word
        for word in _WORD_RE.findall(summary.lower())
        if len(word) >= 3 and word not in _STOPWORDS
    }


def compute_stress_by_calendar_keyword(
    samples: Sequence[StressSample],
    events: Iterable[CalendarEventLike],
) -> TemplateResult | None:
    """Mean stress during events sharing a summary keyword vs period baseline.

    Only keywords appearing in at least ``MIN_KEYWORD_EVENTS`` events, with at
    least ``MIN_KEYWORD_SAMPLES`` in-event stress samples, qualify. The
    statement reports the keyword with the largest absolute delta.
    """
    if len(samples) < MIN_STRESS_SAMPLES:
        return None

    events_by_keyword: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    for event in events:
        if not event.summary:
            continue
        window = (ensure_utc(event.start_at), ensure_utc(event.end_at))
        for keyword in _summary_keywords(event.summary):
            events_by_keyword[keyword].append(window)

    baseline = _mean([sample.value for sample in samples])
    rows: list[dict[str, Any]] = []
    for keyword, windows in events_by_keyword.items():
        if len(windows) < MIN_KEYWORD_EVENTS:
            continue
        matched = [
            sample.value
            for sample in samples
            if any(start <= sample.timestamp < end for start, end in windows)
        ]
        if len(matched) < MIN_KEYWORD_SAMPLES:
            continue
        mean_during = _mean(matched)
        rows.append(
            {
                "keyword": keyword,
                "n_events": len(windows),
                "n_samples": len(matched),
                "mean": round(mean_during, 2),
                "delta": round(mean_during - baseline, 2),
            }
        )
    if not rows:
        return None

    rows.sort(key=lambda row: (-abs(row["delta"]), row["keyword"]))
    top = rows[0]
    direction = "higher" if top["delta"] > 0 else "lower" if top["delta"] < 0 else "similar"
    evidence = {
        "baseline_mean": round(baseline, 2),
        "n_samples_total": len(samples),
        "keywords": rows,
        "top_keyword": top["keyword"],
    }
    statement = (
        f"Calendar events mentioning '{top['keyword']}' coincide with {direction} stress "
        f"({top['mean']:.0f} vs baseline {baseline:.0f}, {top['n_events']} events)."
    )
    confidence = _coverage_confidence(top["n_samples"], FULL_KEYWORD_SAMPLES)
    return TemplateResult(KIND_STRESS_BY_CALENDAR_KEYWORD, statement, evidence, confidence)


def compute_activity_type_vs_stress(
    samples: Sequence[StressSample],
    workouts: Sequence[WorkoutEvent],
) -> TemplateResult | None:
    """Per-workout-type stress delta: after-window mean minus before-window mean.

    For each workout, stress is averaged over ``WORKOUT_WINDOW`` before the
    start and after the end; workouts lacking ``MIN_WINDOW_SAMPLES`` on either
    side are excluded, and a type needs ``MIN_WORKOUTS_PER_TYPE`` qualifying
    workouts to appear.
    """
    if not workouts or len(samples) < MIN_STRESS_SAMPLES:
        return None

    deltas_by_type: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for workout in workouts:
        before_window = (workout.start_time - WORKOUT_WINDOW, workout.start_time)
        after_window = (workout.end_time, workout.end_time + WORKOUT_WINDOW)
        before = [
            sample.value
            for sample in samples
            if before_window[0] <= sample.timestamp < before_window[1]
        ]
        after = [
            sample.value
            for sample in samples
            if after_window[0] <= sample.timestamp < after_window[1]
        ]
        if len(before) < MIN_WINDOW_SAMPLES or len(after) < MIN_WINDOW_SAMPLES:
            continue
        deltas_by_type[workout.workout_type].append((_mean(before), _mean(after)))

    rows: list[dict[str, Any]] = []
    for workout_type, pairs in deltas_by_type.items():
        if len(pairs) < MIN_WORKOUTS_PER_TYPE:
            continue
        deltas = [after - before for before, after in pairs]
        rows.append(
            {
                "type": workout_type,
                "n": len(pairs),
                "mean_before": round(_mean([before for before, _ in pairs]), 2),
                "mean_after": round(_mean([after for _, after in pairs]), 2),
                "mean_delta": round(_mean(deltas), 2),
            }
        )
    if not rows:
        return None

    rows.sort(key=lambda row: (-abs(row["mean_delta"]), row["type"]))
    top = rows[0]
    direction = (
        "lower" if top["mean_delta"] < 0 else "higher" if top["mean_delta"] > 0 else "unchanged"
    )
    window_hours = WORKOUT_WINDOW.total_seconds() / 3600
    evidence = {
        "window_hours": window_hours,
        "types": rows,
        "top_type": top["type"],
        "n_workouts_total": len(workouts),
    }
    statement = (
        f"'{top['type']}' workouts are followed by {direction} stress "
        f"(avg change {top['mean_delta']:+.1f} within {window_hours:.0f}h of finishing, "
        f"n={top['n']})."
    )
    confidence = _coverage_confidence(top["n"], FULL_WORKOUTS)
    return TemplateResult(KIND_ACTIVITY_TYPE_VS_STRESS, statement, evidence, confidence)


def compute_all(
    samples: Sequence[StressSample],
    events: Sequence[CalendarEventLike],
    workouts: Sequence[WorkoutEvent],
    tz: tzinfo = UTC,
) -> tuple[list[TemplateResult], list[SkippedTemplate]]:
    """Run every template; return (computed, skipped) in ``ALL_KINDS`` order.

    ``tz`` is the user's timezone — hour/weekday buckets and day-coverage
    confidence are computed in it (keyword/activity templates compare
    absolute instants and need no conversion).
    """
    outcomes: dict[str, TemplateResult | None] = {
        KIND_STRESS_BY_HOUR: compute_stress_by_hour(samples, tz),
        KIND_STRESS_BY_WEEKDAY: compute_stress_by_weekday(samples, tz),
        KIND_STRESS_BY_CALENDAR_KEYWORD: compute_stress_by_calendar_keyword(samples, events),
        KIND_ACTIVITY_TYPE_VS_STRESS: compute_activity_type_vs_stress(samples, workouts),
    }
    computed = [result for result in outcomes.values() if result is not None]
    skipped = [SkippedTemplate(kind) for kind, result in outcomes.items() if result is None]
    return computed, skipped
