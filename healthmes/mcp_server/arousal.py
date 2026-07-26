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

1. ``quiet_windows``  — waking-hour stretches evidenced by **consecutive**
   still step samples (a lone still bucket proves nothing), with workout
   spans subtracted, night hours excluded as likely sleep (approximation,
   documented; sleep-event masking is a future refinement — hence the
   confidence cap).
2. ``resting_baseline`` — median of the last 14 daily resting-HR values
   (provider-computed RHR; needs ``MIN_BASELINE_DAYS`` to exist).
3. ``arousal_hint_intervals`` — per quiet window, sustained elevated spans
   whose **mean over every sample in the span (dips included)** clears the
   threshold; data gaps break spans instead of being bridged.
4. ``build_arousal_hints`` — the response payload with observation-based
   coverage, capped confidence, and honest ``insufficient_data``.

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
MAX_STILL_PAIR_GAP_MINUTES = 15  # consecutive still samples further apart don't chain
MIN_HINT_MINUTES = 10  # sustained elevation required for one hint
HINT_GAP_TOLERANCE_MINUTES = 3  # max spacing between samples inside one span
AROUSAL_MARGIN_BPM = 12.0  # elevation over resting baseline that counts
MIN_BASELINE_DAYS = 7  # fewer RHR days → insufficient_data
WAKING_START_HOUR = 7  # local; earlier is treated as likely sleep
WAKING_END_HOUR = 23
HR_OBSERVATION_SPAN_MINUTES = 6  # one HR sample vouches for at most this span
MIN_COVERAGE_FOR_MEDIUM = 0.30  # of the waking window observed while quiet
MIN_COVERAGE = 0.10  # below → insufficient_data
MEAL_CONTEXT_WINDOW_MINUTES = 45  # food_log entries this close are context

STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient_data"

Span = tuple[dt.datetime, dt.datetime]


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


def _merge_spans(spans: list[Span]) -> list[Span]:
    merged: list[Span] = []
    for start, end in sorted(spans):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _subtract_spans(spans: list[Span], blocks: list[Span]) -> list[Span]:
    """``spans`` minus every ``blocks`` overlap (both need not be merged)."""
    result = list(spans)
    for b_start, b_end in _merge_spans(list(blocks)):
        next_result: list[Span] = []
        for start, end in result:
            if b_end <= start or end <= b_start:
                next_result.append((start, end))
                continue
            if start < b_start:
                next_result.append((start, b_start))
            if b_end < end:
                next_result.append((b_end, end))
        result = next_result
    return [(start, end) for start, end in result if end > start]


def waking_window(day: dt.date, tz: dt.tzinfo) -> Span:
    """The local waking span used for coverage and likely-sleep exclusion."""
    return (
        dt.datetime.combine(day, dt.time(WAKING_START_HOUR), tzinfo=tz),
        dt.datetime.combine(day, dt.time(WAKING_END_HOUR), tzinfo=tz),
    )


def quiet_windows(
    step_samples: list[tuple[dt.datetime, float]],
    workout_spans: list[Span],
    day: dt.date,
    tz: dt.tzinfo,
) -> list[Span]:
    """Waking stretches evidenced still, outside workouts.

    Stillness needs **two consecutive** still samples: a span is added only
    between adjacent samples that are both ≤ threshold and no further apart
    than the pairing cap — so gaps in step data are *unknown*, not quiet,
    and a lone still bucket proves nothing. Workout spans are subtracted
    from the result (not merely sampled at bucket timestamps), so a workout
    starting between two buckets still masks its minutes.
    """
    wake_start, wake_end = waking_window(day, tz)
    pair_gap = dt.timedelta(minutes=MAX_STILL_PAIR_GAP_MINUTES)

    ordered = sorted(step_samples)
    still_pairs: list[Span] = []
    for (at_a, steps_a), (at_b, steps_b) in zip(ordered, ordered[1:]):
        if steps_a > QUIET_STEP_THRESHOLD or steps_b > QUIET_STEP_THRESHOLD:
            continue
        if at_b - at_a > pair_gap:
            continue
        start = max(at_a, wake_start)
        end = min(at_b, wake_end)
        if end > start:
            still_pairs.append((start, end))

    quiet = _subtract_spans(_merge_spans(still_pairs), list(workout_spans))
    min_span = dt.timedelta(minutes=MIN_QUIET_MINUTES)
    return [(start, end) for start, end in quiet if end - start >= min_span]


def resting_baseline(daily_rhr: list[tuple[dt.date, float]]) -> dict[str, Any] | None:
    """Median resting HR over the trailing window; None when too thin."""
    values = [value for _, value in daily_rhr if value > 0]
    if len(values) < MIN_BASELINE_DAYS:
        return None
    return {
        "median_bpm": statistics.median(values),
        "days": len(values),
    }


def _window_chains(
    samples: list[tuple[dt.datetime, float]], window: Span
) -> list[list[tuple[dt.datetime, float]]]:
    """Contiguous sample chains inside one window (data gaps break chains)."""
    gap = dt.timedelta(minutes=HINT_GAP_TOLERANCE_MINUTES)
    inside = [item for item in samples if window[0] <= item[0] < window[1]]
    chains: list[list[tuple[dt.datetime, float]]] = []
    for item in inside:
        if chains and item[0] - chains[-1][-1][0] <= gap:
            chains[-1].append(item)
        else:
            chains.append([item])
    return chains


def arousal_hint_intervals(
    hr_samples: list[tuple[dt.datetime, float]],
    quiet: list[Span],
    baseline_bpm: float,
) -> list[HintInterval]:
    """Sustained quiet-time elevation, judged over every sample in the span.

    Each quiet window is processed independently (hints never bridge
    windows). Within a window, contiguous sample chains (gaps ≤ tolerance)
    are scanned for spans running from one above-threshold sample to a later
    one; a span counts only when it is long enough AND the mean of **all**
    its samples — dips included — clears the threshold. Interleaved
    below-threshold readings therefore drag the mean down and kill the hint
    instead of being silently discarded.
    """
    threshold = baseline_bpm + AROUSAL_MARGIN_BPM
    min_len = dt.timedelta(minutes=MIN_HINT_MINUTES)

    hints: list[HintInterval] = []
    for window in quiet:
        for chain in _window_chains(sorted(hr_samples), window):
            elevated_indices = [
                index for index, (_, value) in enumerate(chain) if value >= threshold
            ]
            if not elevated_indices:
                continue
            span = chain[elevated_indices[0] : elevated_indices[-1] + 1]
            start, end = span[0][0], span[-1][0]
            if end - start < min_len:
                continue
            values = [value for _, value in span]
            mean = sum(values) / len(values)
            if mean < threshold:
                continue
            hints.append(
                HintInterval(
                    start=start,
                    end=end,
                    hr_mean=mean,
                    hr_peak=max(values),
                    baseline_bpm=baseline_bpm,
                )
            )
    return hints


def observation_spans(
    hr_samples: list[tuple[dt.datetime, float]],
) -> list[Span]:
    """Spans genuinely vouched for by HR samples.

    Each sample covers until the next one, bounded by the expected cadence —
    so five samples in twelve minutes cover ~twelve minutes, never a whole
    afternoon, and internal gaps stay uncovered.
    """
    bound = dt.timedelta(minutes=HR_OBSERVATION_SPAN_MINUTES)
    ordered = sorted(at for at, _ in hr_samples)
    spans: list[Span] = []
    for index, at in enumerate(ordered):
        if index + 1 < len(ordered):
            end = min(ordered[index + 1], at + bound)
        else:
            end = at + bound
        spans.append((at, end))
    return _merge_spans(spans)


def _intersect(a: list[Span], b: list[Span]) -> list[Span]:
    out: list[Span] = []
    for a_start, a_end in a:
        for b_start, b_end in b:
            start, end = max(a_start, b_start), min(a_end, b_end)
            if end > start:
                out.append((start, end))
    return _merge_spans(out)


def quiet_coverage(
    quiet: list[Span],
    hr_samples: list[tuple[dt.datetime, float]],
    day: dt.date,
    tz: dt.tzinfo,
) -> float:
    """Fraction of the waking window both quiet AND actually HR-observed."""
    wake_start, wake_end = waking_window(day, tz)
    waking_seconds = (wake_end - wake_start).total_seconds()
    if waking_seconds <= 0:
        return 0.0
    observed = _intersect(quiet, observation_spans(hr_samples))
    covered = sum((end - start).total_seconds() for start, end in observed)
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
    workout_spans: list[Span],
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
