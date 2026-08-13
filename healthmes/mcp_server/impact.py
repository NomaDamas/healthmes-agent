"""Pure before/after delta aggregation for the ``compare_impact`` tool.

"Does factor X agree with me?" is answered deterministically (docs/PLAN.md
1.5): occurrences of a factor are collected from tagged records (food logs,
calendar events, done tasks, workouts), a metric is compared before vs after
each occurrence, and only the aggregate (n, mean delta, spread, confidence)
is returned — the LLM never sees raw series and never invents statistics.

Timezone rule (load-bearing): occurrence *days* are the user's local calendar
days. Nightly metrics join on local dates; two occurrences that share a UTC
date but fall on different local days are different days here.

Fixed policy:

- **Nightly metrics** (sleep score, nocturnal HRV, sleep duration, resting
  HR): the vendor stamps a night's record at wake time, so the night
  *following* local day D carries date D+1 and the night *before* carries
  date D. ``delta = value(D+1) - value(D)``. Occurrences are deduplicated to
  one per local day — the same night pair must never be counted twice.
- **Intraday metrics** (Garmin stress): mean of the series in the
  ``pre_hours`` before the occurrence start vs the ``post_hours`` after the
  occurrence end; each side needs ``min_samples`` samples or the occurrence
  is skipped (counted, never guessed).
- **Minimum evidence**: fewer than ``MIN_PAIRED_OBSERVATIONS`` paired
  observations is an honest ``insufficient_data``.
- **Confidence** is a pure function of n: >= 10 high, >= 5 medium, else low.
  (Coverage-style confidence does not fit here — occurrences are sparse
  events, not daily readings.)
"""

import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

__all__ = [
    "MIN_PAIRED_OBSERVATIONS",
    "Occurrence",
    "matches",
    "dedupe_by_local_day",
    "nightly_deltas",
    "window_mean",
    "summarize_deltas",
    "confidence_from_n",
]

MIN_PAIRED_OBSERVATIONS = 3


@dataclass(frozen=True, slots=True)
class Occurrence:
    """One tagged occurrence of the factor (aware UTC datetimes).

    ``end == start`` for instantaneous records (a food log); spans (calendar
    events, workouts) keep their real end so "after" starts after the
    occurrence is over.
    """

    source: str  # "food_log" | "calendar" | "task" | "workout"
    label: str
    start: datetime
    end: datetime


def matches(factor: str, text: str | None) -> bool:
    """Case-insensitive substring match of the factor against a record text."""
    if not text:
        return False
    return factor.strip().lower() in text.lower()


def dedupe_by_local_day(
    occurrences: Iterable[Occurrence], tz: tzinfo
) -> dict[date, Occurrence]:
    """Earliest occurrence per **local** calendar day (nightly-metric join key)."""
    by_day: dict[date, Occurrence] = {}
    for occurrence in occurrences:
        day = occurrence.start.astimezone(tz).date()
        current = by_day.get(day)
        if current is None or occurrence.start < current.start:
            by_day[day] = occurrence
    return by_day


def nightly_deltas(
    occurrences_by_day: Mapping[date, Occurrence],
    daily_values: Mapping[date, float],
) -> tuple[list[dict[str, Any]], int]:
    """Per-occurrence-day (after-night - before-night) deltas.

    ``daily_values`` must be keyed by the user's local dates (wake-time
    stamped: day D holds the night that *ended* on the morning of D).
    Returns (rows, skipped) — a day missing either side is skipped, never
    interpolated. Row ``before``/``after``/``delta`` are unrounded floats.
    """
    rows: list[dict[str, Any]] = []
    skipped = 0
    for day in sorted(occurrences_by_day):
        occurrence = occurrences_by_day[day]
        before = daily_values.get(day)
        after = daily_values.get(day + timedelta(days=1))
        if before is None or after is None:
            skipped += 1
            continue
        rows.append(
            {
                "occurred_on": day.isoformat(),
                "source": occurrence.source,
                "label": occurrence.label,
                "before": float(before),
                "after": float(after),
                "delta": float(after) - float(before),
            }
        )
    return rows, skipped


def window_mean(
    samples: Sequence[tuple[datetime, float]],
    start: datetime,
    end: datetime,
    *,
    min_samples: int = 1,
) -> tuple[float, int] | None:
    """(mean, n) of sample values with ``start <= ts < end``; None below min_samples."""
    values = [value for ts, value in samples if start <= ts < end]
    if len(values) < min_samples:
        return None
    return sum(values) / len(values), len(values)


def summarize_deltas(deltas: Sequence[float]) -> dict[str, Any]:
    """n / mean / sample-stdev spread / min / max of the deltas (unrounded)."""
    n = len(deltas)
    if n == 0:
        return {
            "n": 0,
            "mean_delta": None,
            "stdev_delta": None,
            "min_delta": None,
            "max_delta": None,
        }
    return {
        "n": n,
        "mean_delta": sum(deltas) / n,
        "stdev_delta": float(statistics.stdev(deltas)) if n >= 2 else None,
        "min_delta": min(deltas),
        "max_delta": max(deltas),
    }


def confidence_from_n(n: int) -> str:
    """Confidence bucket for an event-based aggregate: 10+ high, 5+ medium."""
    if n >= 10:
        return "high"
    if n >= 5:
        return "medium"
    return "low"
