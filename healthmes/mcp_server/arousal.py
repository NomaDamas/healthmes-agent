"""HR-based daytime arousal hints for non-Garmin wearables (PLAN §13 follow-up).

Garmin is the only provider that measures intraday stress natively (§1.5).
Every other watch (Apple, Galaxy, …) still measures **continuous heart
rate**, and a heart rate well above the personal resting baseline during a
*quiet* stretch (no steps, no workout, waking hours) is an honest, weaker
signal: physiological arousal. It cannot distinguish stress from caffeine,
heat, or excitement — so this module deliberately produces *hints*, never
stress claims, and the skill layer is contractually forbidden from basing a
plan decision on hints alone.

Pipeline (pure functions, no I/O — the MCP layer fetches):

1. ``quiet_windows``  — waking-hour stretches with ~zero steps, outside
   workout spans. Night hours are excluded as likely sleep (approximation,
   documented; sleep-event masking is a future refinement — hence the
   confidence cap).
2. ``resting_baseline`` — median + spread of the last 14 daily resting-HR
   values (provider-computed RHR; needs ``MIN_BASELINE_DAYS`` to exist).
3. ``arousal_hint_intervals`` — sustained (≥ ``MIN_HINT_MINUTES``) runs of
   quiet-window HR at or above baseline + margin.
4. ``build_arousal_hints`` — the response payload with coverage, capped
   confidence, and honest ``insufficient_data``.

All thresholds are named constants and are expert-tunable placeholders per
the PLAN convention — values chosen to be conservative (few false hints)
rather than sensitive.
"""

import datetime as dt
import statistics
from dataclasses import dataclass
from typing import Any

# Expert-tunable placeholders (PLAN §1.5 confidence-boundary convention).
QUIET_STEP_THRESHOLD = 5  # steps per sample bucket at or below → "still"
MIN_QUIET_MINUTES = 15  # a stillness stretch shorter than this is ignored
MIN_HINT_MINUTES = 10  # sustained elevation required for one hint
HINT_GAP_TOLERANCE_MINUTES = 3  # brief dips/missing samples inside a run
AROUSAL_MARGIN_BPM = 12.0  # elevation over resting baseline that counts
MIN_BASELINE_DAYS = 7  # fewer RHR days → insufficient_data
WAKING_START_HOUR = 7  # local; earlier is treated as likely sleep
WAKING_END_HOUR = 23
MIN_COVERAGE_FOR_MEDIUM = 0.30  # of the waking window observed while quiet
MIN_COVERAGE = 0.10  # below → insufficient_data
MEAL_CONTEXT_WINDOW_MINUTES = 45  # food_log entries this close are context

STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient_data"


@dataclass(frozen=True)
class HintInterval:
    """One sustained quiet-time HR elevation (hint-grade, never a claim)."""

    start: dt.datetime
    end: dt.datetime
    hr_mean: float
    hr_peak: float
    baseline_bpm: float

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60.0


def _merge_spans(
    spans: list[tuple[dt.datetime, dt.datetime]],
) -> list[tuple[dt.datetime, dt.datetime]]:
    merged: list[tuple[dt.datetime, dt.datetime]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def waking_window(day: dt.date, tz: dt.tzinfo) -> tuple[dt.datetime, dt.datetime]:
    """The local waking span used for coverage and likely-sleep exclusion."""
    return (
        dt.datetime.combine(day, dt.time(WAKING_START_HOUR), tzinfo=tz),
        dt.datetime.combine(day, dt.time(WAKING_END_HOUR), tzinfo=tz),
    )


def quiet_windows(
    step_samples: list[tuple[dt.datetime, float]],
    workout_spans: list[tuple[dt.datetime, dt.datetime]],
    day: dt.date,
    tz: dt.tzinfo,
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Waking stretches that are still (≈no steps) and outside workouts.

    Step samples are bucketed observations ``(local_time, steps)``; a gap in
    step data is NOT treated as stillness — only observed ≤threshold buckets
    qualify, so missing data lowers coverage instead of inventing quiet.
    """
    wake_start, wake_end = waking_window(day, tz)
    workout_merged = _merge_spans(list(workout_spans))

    ordered = sorted(step_samples)
    still: list[tuple[dt.datetime, dt.datetime]] = []
    for index, (at, steps) in enumerate(ordered):
        if steps > QUIET_STEP_THRESHOLD:
            continue
        if not (wake_start <= at < wake_end):
            continue
        if any(start <= at < end for start, end in workout_merged):
            continue
        # A bucket covers until the next sample (bounded to 15 min so sparse
        # data cannot claim long stillness).
        if index + 1 < len(ordered):
            span_end = min(ordered[index + 1][0], at + dt.timedelta(minutes=15))
        else:
            span_end = at + dt.timedelta(minutes=15)
        still.append((at, min(span_end, wake_end)))

    merged = _merge_spans(still)
    min_span = dt.timedelta(minutes=MIN_QUIET_MINUTES)
    return [(start, end) for start, end in merged if end - start >= min_span]


def resting_baseline(daily_rhr: list[tuple[dt.date, float]]) -> dict[str, Any] | None:
    """Median resting HR over the trailing window; None when too thin."""
    values = [value for _, value in daily_rhr if value > 0]
    if len(values) < MIN_BASELINE_DAYS:
        return None
    return {
        "median_bpm": statistics.median(values),
        "days": len(values),
    }


def arousal_hint_intervals(
    hr_samples: list[tuple[dt.datetime, float]],
    quiet: list[tuple[dt.datetime, dt.datetime]],
    baseline_bpm: float,
) -> list[HintInterval]:
    """Sustained quiet-time HR runs at or above baseline + margin."""
    threshold = baseline_bpm + AROUSAL_MARGIN_BPM
    gap = dt.timedelta(minutes=HINT_GAP_TOLERANCE_MINUTES)
    min_len = dt.timedelta(minutes=MIN_HINT_MINUTES)

    in_quiet = [
        (at, value)
        for at, value in sorted(hr_samples)
        if any(start <= at < end for start, end in quiet)
    ]

    hints: list[HintInterval] = []
    run: list[tuple[dt.datetime, float]] = []
    for at, value in in_quiet:
        if value >= threshold:
            if run and at - run[-1][0] > gap:
                _flush_run(run, baseline_bpm, min_len, hints)
                run = []
            run.append((at, value))
        elif run and at - run[-1][0] <= gap:
            continue  # brief dip inside the tolerance window
        else:
            _flush_run(run, baseline_bpm, min_len, hints)
            run = []
    _flush_run(run, baseline_bpm, min_len, hints)
    return hints


def _flush_run(
    run: list[tuple[dt.datetime, float]],
    baseline_bpm: float,
    min_len: dt.timedelta,
    out: list[HintInterval],
) -> None:
    if len(run) < 2:
        return
    start, end = run[0][0], run[-1][0]
    if end - start < min_len:
        return
    values = [value for _, value in run]
    out.append(
        HintInterval(
            start=start,
            end=end,
            hr_mean=sum(values) / len(values),
            hr_peak=max(values),
            baseline_bpm=baseline_bpm,
        )
    )


def quiet_coverage(
    quiet: list[tuple[dt.datetime, dt.datetime]],
    hr_samples: list[tuple[dt.datetime, float]],
    day: dt.date,
    tz: dt.tzinfo,
) -> float:
    """Fraction of the waking window that is quiet AND has HR observations."""
    wake_start, wake_end = waking_window(day, tz)
    waking_seconds = (wake_end - wake_start).total_seconds()
    if waking_seconds <= 0:
        return 0.0
    hr_times = sorted(at for at, _ in hr_samples)
    covered = 0.0
    for start, end in quiet:
        inside = [at for at in hr_times if start <= at < end]
        if len(inside) >= 2:
            covered += (min(end, inside[-1] + dt.timedelta(minutes=5)) - start).total_seconds()
    return max(0.0, min(1.0, covered / waking_seconds))


def meal_context(
    hint: HintInterval, meals: list[tuple[dt.datetime, str]]
) -> list[str]:
    """Food/caffeine log entries close enough to be plausible context."""
    window = dt.timedelta(minutes=MEAL_CONTEXT_WINDOW_MINUTES)
    labels = []
    for logged_at, description in meals:
        if hint.start - window <= logged_at <= hint.end + window:
            labels.append(f"meal/log: {description}")
    return labels


def build_arousal_hints(
    *,
    day: dt.date,
    tz: dt.tzinfo,
    hr_samples: list[tuple[dt.datetime, float]],
    step_samples: list[tuple[dt.datetime, float]],
    workout_spans: list[tuple[dt.datetime, dt.datetime]],
    daily_rhr: list[tuple[dt.date, float]],
    truncated: bool,
    context_for: Any,
    meals: list[tuple[dt.datetime, str]],
) -> dict[str, Any]:
    """The ``arousal_hints`` payload attached to ``get_stress_timeline``.

    ``context_for(start, end) -> list[str]`` supplies calendar/app-usage
    context (the caller closes over its already-fetched rows); meal-log
    context is appended here. Always returns a dict — failure modes are
    honest statuses, never exceptions.
    """

    def insufficient(reason: str) -> dict[str, Any]:
        return {
            "status": STATUS_INSUFFICIENT,
            "reason": reason,
            "confidence": "low",
            "coverage": 0.0,
            "intervals": [],
            "note": (
                "Arousal hints need continuous HR, step stillness evidence, "
                "and a resting-HR baseline; they are hints about quiet-time "
                "HR elevation, never measured stress."
            ),
        }

    if truncated:
        return insufficient("hr_or_steps_timeseries_truncated")
    baseline = resting_baseline(daily_rhr)
    if baseline is None:
        return insufficient("resting_hr_baseline_too_thin")
    if not hr_samples:
        return insufficient("no_heart_rate_samples")
    if not step_samples:
        return insufficient("no_step_samples_for_stillness")

    quiet = quiet_windows(step_samples, workout_spans, day, tz)
    coverage = quiet_coverage(quiet, hr_samples, day, tz)
    if not quiet or coverage < MIN_COVERAGE:
        return insufficient("quiet_window_coverage_too_low")

    hints = arousal_hint_intervals(hr_samples, quiet, baseline["median_bpm"])
    confidence = "medium" if coverage >= MIN_COVERAGE_FOR_MEDIUM else "low"

    return {
        "status": STATUS_OK,
        "confidence": confidence,  # hint-grade: capped at medium by design
        "coverage": round(coverage, 3),
        "baseline_bpm": round(baseline["median_bpm"], 1),
        "baseline_days": baseline["days"],
        "margin_bpm": AROUSAL_MARGIN_BPM,
        "intervals": [
            {
                "start": hint.start.isoformat(),
                "end": hint.end.isoformat(),
                "minutes": round(hint.minutes, 1),
                "hr_mean": round(hint.hr_mean, 1),
                "hr_peak": round(hint.hr_peak, 1),
                "elevation_bpm": round(hint.hr_mean - hint.baseline_bpm, 1),
                "likely_context": context_for(hint.start, hint.end)
                + meal_context(hint, meals),
            }
            for hint in hints
        ],
        "note": (
            "Quiet-time HR elevation over the personal resting baseline. "
            "Caffeine, heat, illness, or excitement produce the same signal — "
            "these are arousal hints, not measured stress, and cannot alone "
            "justify a plan change."
        ),
    }
