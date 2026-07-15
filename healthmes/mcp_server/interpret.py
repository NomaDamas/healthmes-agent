"""Deterministic interpretation of open-wearables data (docs/PLAN.md 1.5, 3).

All numeric judgment lives here as pure functions — never in the LLM. Layer B
MCP tools call these and return interpreted deltas plus explicit
``confidence`` / ``coverage`` fields instead of raw series dumps. When data is
too thin the functions honestly return ``status: "insufficient_data"``.

Fixed policy (documented so results are reproducible and explainable):

- **Baseline** = trailing **median** over the 14 days strictly before the
  current observation (median, not mean — robust to single bad nights).
- **z-score** = ``(current - median(history)) / sample_stdev(history)``.
  Reported only when the history has at least ``MIN_BASELINE_DAYS`` days and
  non-zero spread.
- **Sleep debt** consumes the OW *internal* sleep score (0-100, the 4-factor
  score of ``vendor/open-wearables/backend/app/algorithms/sleep.py`` stored as
  ``HealthScore(category=sleep, provider=internal)`` — never reinvented here).
  Nightly debt = ``max(0, 100 - score)``; the index is the mean nightly debt
  over the trailing 7 nights that have data.
- **Stress** = the native Garmin STRESS score (0-100, higher = more stressed;
  the only provider that ships one, see
  ``backend/app/constants/health_scores.py``). Devices without Garmin fall
  back to the internal resilience score proxy: ``100 - resilience_score``
  (resilience 0-100 sits in ``components.resilience_score`` of
  ``HealthScore(category=resilience, provider=internal)``; the row's ``value``
  is the raw HRV-CV — see ``fill_missing_resilience_scores_task.py``).
- **Coverage** = days with data / days in window. **Confidence** buckets:
  high (coverage >= 0.7 and n >= 10), medium (coverage >= 0.4 and n >= 5),
  else low.
"""

import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any, Literal

# Baseline policy constants (docs/PLAN.md section 3: 14-day trailing median).
BASELINE_WINDOW_DAYS = 14
LONG_BASELINE_WINDOW_DAYS = 90
MIN_BASELINE_DAYS = 5

# Sleep-debt policy constants.
SLEEP_DEBT_WINDOW_DAYS = 7
MIN_SLEEP_DEBT_NIGHTS = 3

# A daily stress/resilience reading older than this is not "today's" state.
STRESS_MAX_STALE_DAYS = 3
# Nocturnal HRV older than this is not a current readiness observation.
HRV_MAX_STALE_DAYS = 3

STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient_data"

DayAggregation = Literal["mean", "max", "latest"]


def trailing_median(values: Iterable[float]) -> float | None:
    """Median of ``values``; None when empty."""
    data = list(values)
    if not data:
        return None
    return float(statistics.median(data))


def sample_stdev(values: Iterable[float]) -> float | None:
    """Sample standard deviation; None when fewer than 2 values."""
    data = list(values)
    if len(data) < 2:
        return None
    return float(statistics.stdev(data))


def z_score(current: float, history: Iterable[float]) -> float | None:
    """z of ``current`` against the history baseline.

    ``(current - median(history)) / sample_stdev(history)``; None when the
    history has fewer than 2 points or zero spread (undefined deviation).
    """
    data = list(history)
    median = trailing_median(data)
    stdev = sample_stdev(data)
    if median is None or stdev is None or stdev == 0:
        return None
    return (current - median) / stdev


def coverage_ratio(n_days: int, window_days: int) -> float:
    """Fraction of window days that have data, rounded to 2 decimals."""
    if window_days <= 0:
        return 0.0
    return round(min(n_days, window_days) / window_days, 2)


def confidence_label(n_days: int, window_days: int) -> str:
    """Bucket measurement coverage into high / medium / low confidence."""
    coverage = 0.0 if window_days <= 0 else n_days / window_days
    if coverage >= 0.7 and n_days >= 10:
        return "high"
    if coverage >= 0.4 and n_days >= MIN_BASELINE_DAYS:
        return "medium"
    return "low"


def daily_series(
    points: Iterable[tuple[datetime, float]],
    how: DayAggregation = "latest",
) -> dict[date, float]:
    """Collapse timestamped samples into one value per (UTC) day.

    - ``mean``: average of the day's samples (e.g. Garmin stress readings)
    - ``max``: maximum (e.g. internal sleep score — main sleep dominates naps)
    - ``latest``: value with the greatest timestamp that day
    """
    by_day: dict[date, list[tuple[datetime, float]]] = defaultdict(list)
    for recorded_at, value in points:
        by_day[recorded_at.date()].append((recorded_at, value))

    out: dict[date, float] = {}
    for day, samples in by_day.items():
        if how == "mean":
            out[day] = sum(v for _, v in samples) / len(samples)
        elif how == "max":
            out[day] = max(v for _, v in samples)
        else:
            out[day] = max(samples, key=lambda item: item[0])[1]
    return out


def metric_baseline(
    daily_values: Mapping[date, float],
    as_of: date,
    *,
    window_days: int = BASELINE_WINDOW_DAYS,
    min_days: int = MIN_BASELINE_DAYS,
    max_stale_days: int | None = None,
) -> dict[str, Any]:
    """Current value vs trailing-median baseline for one daily metric.

    ``current`` is the most recent value on or before ``as_of``; the baseline
    window is the ``window_days`` days strictly before the current
    observation's day (so the observation never skews its own baseline).
    """
    current_days = sorted(day for day in daily_values if day <= as_of)
    if not current_days:
        return {
            "status": STATUS_INSUFFICIENT,
            "reason": "no_data_on_or_before_as_of",
            "window_days": window_days,
            "n_days": 0,
            "coverage": 0.0,
            "confidence": "low",
        }

    current_day = current_days[-1]
    current_value = float(daily_values[current_day])
    window_start = current_day - timedelta(days=window_days)
    history = [
        float(value)
        for day, value in sorted(daily_values.items())
        if window_start <= day < current_day
    ]
    n_days = len(history)
    result: dict[str, Any] = {
        "current": {"date": current_day.isoformat(), "value": round(current_value, 2)},
        "window_days": window_days,
        "n_days": n_days,
        "coverage": coverage_ratio(n_days, window_days),
        "confidence": confidence_label(n_days, window_days),
    }
    stale_days = (as_of - current_day).days
    if max_stale_days is not None and stale_days > max_stale_days:
        result["status"] = STATUS_INSUFFICIENT
        result["reason"] = f"current_reading_stale_gt_{max_stale_days}_days"
        result["stale_days"] = stale_days
        return result
    if n_days < min_days:
        result["status"] = STATUS_INSUFFICIENT
        result["reason"] = f"need_at_least_{min_days}_baseline_days"
        return result

    median = trailing_median(history)
    assert median is not None  # n_days >= min_days >= 1
    z = z_score(current_value, history)
    delta = current_value - median
    result["status"] = STATUS_OK
    result["baseline_median"] = round(median, 2)
    result["delta"] = round(delta, 2)
    result["delta_pct"] = round(delta / median * 100, 1) if median != 0 else None
    result["z_score"] = round(z, 2) if z is not None else None
    return result


def sleep_debt(
    scores_by_day: Mapping[date, float],
    as_of: date,
    *,
    window_days: int = SLEEP_DEBT_WINDOW_DAYS,
    min_nights: int = MIN_SLEEP_DEBT_NIGHTS,
) -> dict[str, Any]:
    """Sleep-debt index from the OW internal sleep score (0-100 per night).

    Nights considered: ``(as_of - window_days, as_of]`` keyed by the score's
    ``recorded_at`` date (the vendor task stamps scores at wake time, so last
    night's score carries today's date). Index = mean of
    ``max(0, 100 - score)`` over nights with data; 0 = fully rested,
    100 = maximal debt.
    """
    window_start = as_of - timedelta(days=window_days)
    nights = {
        day: float(score)
        for day, score in scores_by_day.items()
        if window_start < day <= as_of
    }
    n_nights = len(nights)
    result: dict[str, Any] = {
        "window_days": window_days,
        "nights_counted": n_nights,
        "coverage": coverage_ratio(n_nights, window_days),
        "confidence": confidence_label(n_nights, window_days),
    }
    if n_nights < min_nights:
        result["status"] = STATUS_INSUFFICIENT
        result["reason"] = f"need_at_least_{min_nights}_scored_nights"
        return result

    debts = [max(0.0, 100.0 - score) for score in nights.values()]
    last_night = max(nights)
    result["status"] = STATUS_OK
    result["index"] = round(sum(debts) / len(debts), 1)
    result["last_night"] = {
        "date": last_night.isoformat(),
        "score": round(nights[last_night], 1),
    }
    return result


def stress_context(
    garmin_stress_by_day: Mapping[date, float],
    resilience_score_by_day: Mapping[date, float],
    as_of: date,
    *,
    max_stale_days: int = STRESS_MAX_STALE_DAYS,
) -> dict[str, Any]:
    """Resolve the day's stress level (0-100, higher = more stressed).

    Prefers the native Garmin STRESS score; devices without one fall back to
    ``100 - internal resilience score``. Readings older than
    ``max_stale_days`` are ignored (yesterday-ish state is still useful,
    last week's is not).
    """

    def _latest_within(series: Mapping[date, float]) -> tuple[date, float] | None:
        candidates = {
            day: value
            for day, value in series.items()
            if day <= as_of and (as_of - day).days <= max_stale_days
        }
        if not candidates:
            return None
        day = max(candidates)
        return day, float(candidates[day])

    garmin = _latest_within(garmin_stress_by_day)
    if garmin is not None:
        day, value = garmin
        return {
            "status": STATUS_OK,
            "source": "garmin_stress",
            "value": round(value, 1),
            "scale": "0-100, higher = more stressed",
            "observed_on": day.isoformat(),
            "stale_days": (as_of - day).days,
            "confidence": "high" if day == as_of else "medium",
        }

    resilience = _latest_within(resilience_score_by_day)
    if resilience is not None:
        day, score = resilience
        return {
            "status": STATUS_OK,
            "source": "internal_resilience_proxy",
            "value": round(max(0.0, min(100.0, 100.0 - score)), 1),
            "scale": "0-100, higher = more stressed (100 - resilience score)",
            "observed_on": day.isoformat(),
            "stale_days": (as_of - day).days,
            "confidence": "medium" if day == as_of else "low",
        }

    return {
        "status": STATUS_INSUFFICIENT,
        "reason": "no_garmin_stress_and_no_internal_resilience_within_window",
        "confidence": "low",
    }


def choose_stress_series(
    garmin_by_day: Mapping[date, float],
    proxy_by_day: Mapping[date, float],
    as_of: date,
    *,
    max_stale_days: int = STRESS_MAX_STALE_DAYS,
) -> tuple[dict[date, float], str]:
    """Choose one complete stress series without mixing providers."""

    def _latest(series: Mapping[date, float]) -> date | None:
        candidates = [day for day in series if day <= as_of]
        return max(candidates) if candidates else None

    garmin_day = _latest(garmin_by_day)
    proxy_day = _latest(proxy_by_day)
    if garmin_day is not None and (as_of - garmin_day).days <= max_stale_days:
        return dict(garmin_by_day), "garmin"
    if proxy_day is not None and (as_of - proxy_day).days <= max_stale_days:
        return dict(proxy_by_day), "proxy"
    if garmin_day is None and proxy_day is None:
        return {}, "none"
    if proxy_day is None or (garmin_day is not None and garmin_day >= proxy_day):
        return dict(garmin_by_day), "garmin"
    return dict(proxy_by_day), "proxy"


def overall_confidence(blocks: Iterable[Mapping[str, Any]]) -> str:
    """Weakest-link confidence across the blocks that produced a result."""
    order = {"high": 2, "medium": 1, "low": 0}
    labels = [
        str(block.get("confidence", "low"))
        for block in blocks
        if block.get("status") == STATUS_OK
    ]
    if not labels:
        return "low"
    return min(labels, key=lambda label: order.get(label, 0))


# ---------------------------------------------------------------------------
# open-wearables row digestion (HealthScoreResponse / SleepSummary / ...)
#
# Shared domain logic: the MCP tools (healthmes/mcp_server/server.py), the
# cognitive-energy engine (healthmes/engine/cognitive_energy.py) and the
# trigger sweep all digest the same vendor REST row shapes — one copy lives
# here so consumers never reach into another module's privates.
# ---------------------------------------------------------------------------


def parse_recorded_at(value: Any) -> datetime | None:
    """Parse a response timestamp into aware UTC (None when absent/invalid)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def as_float(value: Any) -> float | None:
    """Lenient numeric coercion (bools and unparseable values become None)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def localized(
    points: Iterable[tuple[datetime, float]], tz: tzinfo | None
) -> list[tuple[datetime, float]]:
    """Convert point timestamps to ``tz`` (so ``.date()`` keys become local days)."""
    if tz is None:
        return list(points)
    return [(recorded_at.astimezone(tz), value) for recorded_at, value in points]


def score_points(
    rows: Iterable[Mapping[str, Any]],
    category: str,
    *,
    provider: str | None = None,
    exclude_providers: frozenset[str] = frozenset(),
) -> list[tuple[datetime, float]]:
    """(recorded_at, value) points for one health-score category."""
    points: list[tuple[datetime, float]] = []
    for row in rows:
        if row.get("category") != category:
            continue
        row_provider = row.get("provider") or "unknown"
        if provider is not None and row_provider != provider:
            continue
        if row_provider in exclude_providers:
            continue
        recorded_at = parse_recorded_at(row.get("recorded_at"))
        value = as_float(row.get("value"))
        if recorded_at is None or value is None:
            continue
        points.append((recorded_at, value))
    return points


def resilience_score_points(rows: Iterable[Mapping[str, Any]]) -> list[tuple[datetime, float]]:
    """(recorded_at, resilience_score 0-100) from internal resilience rows.

    The row ``value`` is the raw HRV-CV; the 0-100 score lives in
    ``components.resilience_score.value``
    (fill_missing_resilience_scores_task.py).
    """
    points: list[tuple[datetime, float]] = []
    for row in rows:
        if row.get("category") != "resilience" or row.get("provider") != "internal":
            continue
        recorded_at = parse_recorded_at(row.get("recorded_at"))
        components = row.get("components") or {}
        score = as_float((components.get("resilience_score") or {}).get("value"))
        if recorded_at is None or score is None:
            continue
        points.append((recorded_at, score))
    return points


def sleep_score_series(
    rows: Iterable[Mapping[str, Any]],
    *,
    tz: tzinfo | None = None,
) -> tuple[dict[date, float], str | None]:
    """Daily sleep-score series, preferring the internal 4-factor score.

    Falls back to provider sleep scores (max per day so the main sleep beats
    naps) when no internal rows exist; the second element names the source.
    With ``tz`` the series is keyed by the user's *local* dates (a wake-time
    score at 07:00+09:00 belongs to that local day, not to the previous UTC
    day); without it the tranche-1 UTC-day behavior is unchanged.
    """
    rows = list(rows)
    internal = daily_series(
        localized(score_points(rows, "sleep", provider="internal"), tz), how="max"
    )
    if internal:
        return internal, "internal_sleep_score"
    provider = daily_series(
        localized(
            score_points(rows, "sleep", exclude_providers=frozenset({"internal"})), tz
        ),
        how="max",
    )
    if provider:
        return provider, "provider_sleep_score"
    return {}, None


def summary_daily_values(
    rows: Iterable[Mapping[str, Any]], field: str, up_to: date
) -> dict[date, float]:
    """Per-day values of one summary field, keyed by the summary ``date``."""
    out: dict[date, float] = {}
    for row in rows:
        raw_day = row.get("date")
        value = as_float(row.get(field))
        if raw_day is None or value is None:
            continue
        try:
            day = date.fromisoformat(str(raw_day))
        except ValueError:
            continue
        if day <= up_to:
            out[day] = value
    return out


def normalize_recovery(category: str, provider: str | None, value: float) -> float:
    """Normalize provider charge-score scales to 0-100 (higher = better).

    Scales transcribed from vendor/open-wearables/backend/app/constants/
    health_scores.py (HEALTH_SCORE_RANGES): BODY_BATTERY garmin 0-100;
    RECOVERY whoop/suunto 0-100, polar 1-6; READINESS oura 1-100, polar 0-10.
    """
    provider_name = (provider or "").lower()
    if category == "recovery" and provider_name == "polar":
        normalized = (value - 1.0) / 5.0 * 100.0
    elif category == "readiness" and provider_name == "polar":
        normalized = value * 10.0
    else:
        normalized = value
    return min(max(normalized, 0.0), 100.0)
