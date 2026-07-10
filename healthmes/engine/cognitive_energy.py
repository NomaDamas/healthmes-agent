"""Cognitive-energy engine v1 — pure, explainable, rule-based (docs/PLAN.md §3).

The score for one hourly window is::

    score = 100
      - sleep_debt_penalty      (OW *internal* sleep score consumed verbatim — the
                                 4-factor score of vendor/open-wearables/backend/
                                 app/algorithms/sleep.py, never reimplemented here)
      - stress_penalty          (time-weighted Garmin STRESS — the only provider
                                 with a native one, constants/health_scores.py —
                                 else the internal resilience proxy 100 - score)
      - hrv_deviation_penalty   (tonight's nocturnal HRV vs the personal 14-day
                                 trailing-median baseline; SDNN and RMSSD variants
                                 are never mixed)
      + body_battery_bonus      (BODY_BATTERY / READINESS / RECOVERY when present,
                                 provider scales normalized to 0-100)
      - meeting_load_penalty    (calendar_event_mirror: booked minutes + context
                                 switches inside the window)
      - fragmentation_penalty   (app_usage_sample: distracting-app launches in the
                                 trailing hour — only when usage data exists)

Design contract (all plan-mandated):

- **Missing signals drop their term and the remaining base weights renormalize**
  (iOS users have no app usage, Fitbit/Strava have no sleep). The renormalized
  weight share is what each component reports.
- **Every present factor lands in ``components``** with name / weight / raw /
  contribution — these become the decision tree's "considered inputs" nodes.
  The first component is the ``base`` budget term, so the components sum
  *exactly* to the score: ``score_exact = sum(c["contribution"])`` and the
  persisted integer ``score = round(score_exact)``. By construction
  ``score_exact`` is bounded to [0, 100]: the present factors' renormalized
  weights total 100 points, penalties can at most spend their share and the
  bonus can at most refill its share (``base = 100 - bonus_budget``).
- **Baselines are 14-day trailing medians** computed by the mcp_server helpers
  (:mod:`healthmes.mcp_server.interpret` — imported, never duplicated). They are
  pure functions of the (UTC) calendar day, so they change exactly once per
  night — the plan's "recomputed nightly" without any extra state.
- **HRV is night-only**: nocturnal averages from the open-wearables sleep
  summaries (daytime spot measurements are noise).

Execution: the hourly persist job plugs into the scheduler hook
(:func:`healthmes.engine.scheduler.register_energy_job`) via
:func:`build_energy_job`; on-demand compute is exposed through
:class:`CognitiveEnergyEngine` (consumed by ``healthmes/api/energy.py``).
"""

import asyncio
import inspect
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from healthmes.config import Settings
from healthmes.mcp_server import interpret
from healthmes.store.models import (
    AppUsageSample,
    CalendarEventMirror,
    CognitiveEnergyEstimate,
)
from healthmes.store.session import session_scope

__all__ = [
    "FACTOR_SPECS",
    "FACTORS",
    "STATUS_OK",
    "STATUS_INSUFFICIENT",
    "FactorSpec",
    "FactorSignal",
    "MissingSignal",
    "EnergyEstimate",
    "WindowSlot",
    "OwRows",
    "OwDigest",
    "UsageBucket",
    "StoreDayContext",
    "OwEnergyReader",
    "CognitiveEnergyEngine",
    "sleep_debt_signal",
    "stress_signal",
    "hrv_signal",
    "charge_signal",
    "meeting_load_signal",
    "fragmentation_signal",
    "compute_estimate",
    "digest_ow_rows",
    "load_store_day_context",
    "build_energy_job",
]

logger = logging.getLogger(__name__)

STATUS_OK = interpret.STATUS_OK
STATUS_INSUFFICIENT = interpret.STATUS_INSUFFICIENT

WINDOW_MINUTES = 60

# --- factor weight policy (fractions of the full six-signal set) -------------
# Rationale: sleep is the strongest single predictor of next-day cognition;
# stress and HRV split the autonomic picture; the charge score is partly
# redundant with them (Garmin derives body battery from stress/HRV) so it gets
# a modest share; calendar load and app fragmentation are behavioral terms.


@dataclass(frozen=True, slots=True)
class FactorSpec:
    """One term of the score formula."""

    key: str  # internal signal key
    term: str  # component name (the plan's term name)
    kind: str  # "penalty" | "bonus"
    base_weight: float  # share of the full six-signal set


FACTOR_SPECS: tuple[FactorSpec, ...] = (
    FactorSpec("sleep_debt", "sleep_debt_penalty", "penalty", 0.30),
    FactorSpec("stress", "stress_penalty", "penalty", 0.20),
    FactorSpec("hrv_deviation", "hrv_deviation_penalty", "penalty", 0.15),
    FactorSpec("body_battery", "body_battery_bonus", "bonus", 0.10),
    FactorSpec("meeting_load", "meeting_load_penalty", "penalty", 0.15),
    FactorSpec("fragmentation", "fragmentation_penalty", "penalty", 0.10),
)
FACTORS: dict[str, FactorSpec] = {spec.key: spec for spec in FACTOR_SPECS}
assert abs(sum(spec.base_weight for spec in FACTOR_SPECS) - 1.0) < 1e-9

COMPONENTS_VERSION = 1

# HRV deviation: a nocturnal HRV z-score of -HRV_Z_FLOOR vs the personal
# baseline is maximal severity; positive deviations are never penalized.
HRV_Z_FLOOR = 2.5
HRV_MAX_STALE_DAYS = 1  # "tonight": the most recent night must be as_of or the day before

# Stress: exponential time weighting (half-life one day) over readings no
# older than the interpret staleness policy.
STRESS_HALF_LIFE_DAYS = 1.0

# Charge score: freshest reading of today/yesterday, most-direct category first.
CHARGE_PREFERENCE = ("body_battery", "readiness", "recovery")
CHARGE_MAX_STALE_DAYS = 1

# Meeting load: booked fraction of the window plus context switches
# (event starts inside the window; 3+ starts per hour = maximal switching).
MEETING_BOOKED_WEIGHT = 0.7
MEETING_SWITCH_WEIGHT = 0.3
MEETING_MAX_SWITCHES_PER_WINDOW = 3
# The calendar signal exists only while the mirror is actively synced: any
# mirrored event within this many days around the target day counts as active.
CALENDAR_ACTIVE_LOOKAROUND_DAYS = 7

# Fragmentation: distracting-app launches in the trailing hour;
# 12+ launches/hour = maximal fragmentation. The category vocabulary is
# whatever the Android usage collector emits — the distracting subset of
# apps/android-usage .../usage/UsageSnapshotReader.kt::categoryOf, which maps
# ApplicationInfo categories to exactly {game, audio, video, image, social,
# news, maps, productivity, accessibility}. Uncategorized apps never count.
FRAGMENTATION_LOOKBACK_MINUTES = 60
FRAGMENTATION_MAX_LAUNCHES = 12
USAGE_PRESENCE_LOOKBACK_HOURS = 24
DISTRACTING_CATEGORIES = frozenset({"game", "social", "news", "video"})


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _ensure_utc(value: datetime) -> datetime:
    """Aware datetimes are converted to UTC; naive ones (sqlite reads) are UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _clamp01(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def _floor_hour(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)


def _run_maybe_async(value: Any) -> Any:
    """Drive an awaitable to completion from sync (scheduler / sync handler) code.

    Sync handlers run in worker threads without an event loop, so
    ``asyncio.run`` is safe; plain values (sync fakes) pass through.
    """
    if inspect.isawaitable(value):

        async def _await() -> Any:
            return await value

        return asyncio.run(_await())
    return value


# ---------------------------------------------------------------------------
# Signal model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FactorSignal:
    """One present factor: a normalized value in [0, 1] plus its evidence.

    ``value`` is the severity for penalty factors (0 = perfect, 1 = worst)
    and the charge fraction for the bonus factor (1 = fully charged).
    """

    key: str
    value: float
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MissingSignal:
    """A factor whose term is dropped (weights renormalize without it)."""

    key: str
    reason: str


@dataclass(frozen=True, slots=True)
class EnergyEstimate:
    """The engine output for one window (persisted to cognitive_energy_estimate)."""

    window_start: datetime
    window_end: datetime
    status: str  # STATUS_OK | STATUS_INSUFFICIENT
    score: int | None  # round(score_exact), None when insufficient
    score_exact: float | None  # sum of all component contributions
    components: tuple[dict[str, Any], ...]
    inputs_snapshot: dict[str, Any]

    def components_payload(self) -> dict[str, Any]:
        """The JSONB dict stored in ``cognitive_energy_estimate.components``."""
        return {
            "version": COMPONENTS_VERSION,
            "items": [dict(item) for item in self.components],
            "score_exact": self.score_exact,
        }


@dataclass(frozen=True, slots=True)
class WindowSlot:
    """One forecast window: a persisted row or an on-demand estimate."""

    window_start: datetime
    window_end: datetime
    source: str  # "persisted" | "computed"
    status: str
    score: int | None
    score_exact: float | None
    components: tuple[dict[str, Any], ...]


# ---------------------------------------------------------------------------
# Factor builders (pure)
# ---------------------------------------------------------------------------


def sleep_debt_signal(
    sleep_scores_by_day: Mapping[date, float],
    as_of: date,
    *,
    source: str | None = None,
) -> FactorSignal | MissingSignal:
    """Sleep-debt severity from the OW internal sleep score (consumed verbatim).

    Uses :func:`healthmes.mcp_server.interpret.sleep_debt` (mean nightly
    ``100 - score`` over the trailing 7 nights); severity = index / 100.
    """
    block = interpret.sleep_debt(sleep_scores_by_day, as_of)
    if block.get("status") != STATUS_OK:
        return MissingSignal("sleep_debt", str(block.get("reason", "insufficient_sleep_scores")))
    severity = _clamp01(float(block["index"]) / 100.0)
    return FactorSignal(
        "sleep_debt",
        severity,
        {
            "source": source or "internal_sleep_score",
            "index": block["index"],
            "nights_counted": block["nights_counted"],
            "window_days": block["window_days"],
            "last_night": block["last_night"],
            "coverage": block["coverage"],
            "confidence": block["confidence"],
            "severity": severity,
        },
    )


def _time_weighted(
    daily: Mapping[date, float],
    as_of: date,
    *,
    max_stale_days: int = interpret.STRESS_MAX_STALE_DAYS,
    half_life_days: float = STRESS_HALF_LIFE_DAYS,
) -> tuple[float, list[dict[str, Any]]] | None:
    """Exponentially time-weighted mean of a daily series ending at ``as_of``.

    Weight of a reading aged ``d`` days is ``0.5 ** (d / half_life_days)``;
    readings older than ``max_stale_days`` are ignored. None when nothing is
    recent enough.
    """
    usable = {
        day: float(value)
        for day, value in daily.items()
        if day <= as_of and (as_of - day).days <= max_stale_days
    }
    if not usable:
        return None
    weighted_sum = 0.0
    weight_total = 0.0
    days_used: list[dict[str, Any]] = []
    for day in sorted(usable):
        weight = 0.5 ** ((as_of - day).days / half_life_days)
        weighted_sum += weight * usable[day]
        weight_total += weight
        days_used.append(
            {"date": day.isoformat(), "value": round(usable[day], 1), "weight": round(weight, 4)}
        )
    return weighted_sum / weight_total, days_used


def stress_signal(
    garmin_stress_by_day: Mapping[date, float],
    resilience_score_by_day: Mapping[date, float],
    as_of: date,
) -> FactorSignal | MissingSignal:
    """Time-weighted stress severity: native Garmin, else the resilience proxy.

    The proxy is ``100 - internal resilience score`` (0-100, higher = more
    stressed) — same convention as ``interpret.stress_context``.
    """
    weighted = _time_weighted(garmin_stress_by_day, as_of)
    source = "garmin_stress"
    if weighted is None:
        proxy = {
            day: _clamp01((100.0 - float(score)) / 100.0) * 100.0
            for day, score in resilience_score_by_day.items()
        }
        weighted = _time_weighted(proxy, as_of)
        source = "internal_resilience_proxy"
    if weighted is None:
        return MissingSignal("stress", "no_recent_stress_or_resilience")
    value, days_used = weighted
    severity = _clamp01(value / 100.0)
    return FactorSignal(
        "stress",
        severity,
        {
            "source": source,
            "value": round(value, 1),
            "scale": "0-100, higher = more stressed",
            "half_life_days": STRESS_HALF_LIFE_DAYS,
            "max_stale_days": interpret.STRESS_MAX_STALE_DAYS,
            "days_used": days_used,
            "severity": severity,
        },
    )


def hrv_signal(
    rmssd_by_day: Mapping[date, float],
    sdnn_by_day: Mapping[date, float],
    as_of: date,
) -> FactorSignal | MissingSignal:
    """Tonight's nocturnal HRV vs the personal 14-day trailing-median baseline.

    The variant with more nights wins (ties -> RMSSD, matching the mcp_server
    readiness tool); variants are never mixed, so each keeps its own baseline.
    Only below-baseline deviations are penalized: severity =
    ``clamp(-z / HRV_Z_FLOOR, 0, 1)``.
    """
    if not rmssd_by_day and not sdnn_by_day:
        return MissingSignal("hrv_deviation", "no_nocturnal_hrv")
    variant = "rmssd" if len(rmssd_by_day) >= len(sdnn_by_day) else "sdnn"
    series = rmssd_by_day if variant == "rmssd" else sdnn_by_day
    block = interpret.metric_baseline(series, as_of)
    if block.get("status") != STATUS_OK:
        return MissingSignal(
            "hrv_deviation", str(block.get("reason", "insufficient_hrv_baseline"))
        )
    current_day = date.fromisoformat(block["current"]["date"])
    if (as_of - current_day).days > HRV_MAX_STALE_DAYS:
        return MissingSignal("hrv_deviation", "no_recent_nocturnal_hrv")
    z_score = block.get("z_score")
    if z_score is None:
        return MissingSignal("hrv_deviation", "undefined_hrv_deviation_zero_spread")
    severity = _clamp01(max(0.0, -float(z_score)) / HRV_Z_FLOOR)
    return FactorSignal(
        "hrv_deviation",
        severity,
        {
            "source": "nocturnal (sleep summaries)",
            "variant": variant,
            "unit": "ms",
            "current": block["current"],
            "baseline_median": block["baseline_median"],
            "delta": block["delta"],
            "delta_pct": block["delta_pct"],
            "z_score": z_score,
            "z_floor": HRV_Z_FLOOR,
            "n_days": block["n_days"],
            "window_days": block["window_days"],
            "confidence": block["confidence"],
            "severity": severity,
        },
    )


def charge_signal(
    charge_points: Mapping[str, Sequence[tuple[datetime, float, str | None]]],
    as_of: date,
) -> FactorSignal | MissingSignal:
    """Freshest charge-style score (body battery > readiness > recovery).

    Provider scales are normalized to 0-100 via the shared
    ``interpret.normalize_recovery`` (vendor HEALTH_SCORE_RANGES); the factor
    value is the charge fraction ``normalized / 100``.
    """
    for category in CHARGE_PREFERENCE:
        fresh = [
            (recorded_at, value, provider)
            for recorded_at, value, provider in charge_points.get(category, ())
            if 0 <= (as_of - recorded_at.date()).days <= CHARGE_MAX_STALE_DAYS
        ]
        if not fresh:
            continue
        recorded_at, value, provider = max(fresh, key=lambda item: item[0])
        normalized = interpret.normalize_recovery(category, provider, float(value))
        charge = _clamp01(normalized / 100.0)
        return FactorSignal(
            "body_battery",
            charge,
            {
                "category": category,
                "provider": provider,
                "value": float(value),
                "normalized_value": round(normalized, 1),
                "scale": "0-100, higher = more charged",
                "observed_on": recorded_at.date().isoformat(),
                "stale_days": (as_of - recorded_at.date()).days,
                "preference_order": " > ".join(CHARGE_PREFERENCE),
                "charge": charge,
            },
        )
    return MissingSignal("body_battery", "no_fresh_charge_score")


def _union_minutes(intervals: Sequence[tuple[datetime, datetime]]) -> float:
    """Total minutes covered by the union of intervals (overlaps not double-counted)."""
    total = 0.0
    current_start: datetime | None = None
    current_end: datetime | None = None
    for start, end in sorted(intervals):
        if current_end is None or start > current_end:
            if current_start is not None and current_end is not None:
                total += (current_end - current_start).total_seconds() / 60.0
            current_start, current_end = start, end
        elif end > current_end:
            current_end = end
    if current_start is not None and current_end is not None:
        total += (current_end - current_start).total_seconds() / 60.0
    return total


def meeting_load_signal(
    events: Sequence[tuple[datetime, datetime]],
    window_start: datetime,
    window_end: datetime,
    *,
    calendar_active: bool,
) -> FactorSignal | MissingSignal:
    """Booked time + context switches inside the window (calendar mirror).

    Missing while the mirror is inactive (no synced events around the day) —
    an empty window on an *active* calendar is genuinely free time, severity 0.
    Context switches = events *starting* inside the window; booked minutes are
    the union of event overlaps (parallel meetings not double-counted).
    """
    if not calendar_active:
        return MissingSignal("meeting_load", "calendar_mirror_inactive")
    window_minutes = (window_end - window_start).total_seconds() / 60.0
    clipped: list[tuple[datetime, datetime]] = []
    switches = 0
    for start, end in events:
        if start < window_end and end > window_start:
            clipped.append((max(start, window_start), min(end, window_end)))
            if window_start <= start < window_end:
                switches += 1
    booked_minutes = _union_minutes(clipped)
    booked_fraction = _clamp01(booked_minutes / window_minutes)
    switch_fraction = _clamp01(switches / MEETING_MAX_SWITCHES_PER_WINDOW)
    severity = _clamp01(
        MEETING_BOOKED_WEIGHT * booked_fraction + MEETING_SWITCH_WEIGHT * switch_fraction
    )
    return FactorSignal(
        "meeting_load",
        severity,
        {
            "source": "calendar_event_mirror",
            "booked_minutes": round(booked_minutes, 1),
            "window_minutes": round(window_minutes, 1),
            "booked_fraction": round(booked_fraction, 4),
            "events_overlapping": len(clipped),
            "context_switches": switches,
            "switch_fraction": round(switch_fraction, 4),
            "booked_weight": MEETING_BOOKED_WEIGHT,
            "switch_weight": MEETING_SWITCH_WEIGHT,
            "severity": severity,
        },
    )


@dataclass(frozen=True, slots=True)
class UsageBucket:
    """One app_usage_sample row reduced to what the engine needs."""

    bucket_start: datetime
    app_package: str
    launches: int
    category: str | None


def fragmentation_signal(
    usage: Sequence[UsageBucket],
    window_start: datetime,
    now: datetime,
) -> FactorSignal | MissingSignal:
    """Distracting-app launch frequency in the trailing hour (when data exists).

    Missing for future windows (behavior cannot be forecast) and when the
    device reported nothing in the trailing 24 h (iOS users / no companion
    app — the plan's renormalization case).
    """
    if window_start > now:
        return MissingSignal("fragmentation", "window_in_future")
    presence_cutoff = window_start - timedelta(hours=USAGE_PRESENCE_LOOKBACK_HOURS)
    window_end = window_start + timedelta(minutes=WINDOW_MINUTES)
    if not any(presence_cutoff <= bucket.bucket_start < window_end for bucket in usage):
        return MissingSignal("fragmentation", "no_app_usage_data")
    lookback_start = window_start - timedelta(minutes=FRAGMENTATION_LOOKBACK_MINUTES)
    by_app: dict[str, int] = {}
    launches = 0
    for bucket in usage:
        if not lookback_start <= bucket.bucket_start < window_start:
            continue
        if (bucket.category or "").lower() not in DISTRACTING_CATEGORIES:
            continue
        launches += bucket.launches
        by_app[bucket.app_package] = by_app.get(bucket.app_package, 0) + bucket.launches
    severity = _clamp01(launches / FRAGMENTATION_MAX_LAUNCHES)
    top_apps = dict(sorted(by_app.items(), key=lambda item: -item[1])[:5])
    return FactorSignal(
        "fragmentation",
        severity,
        {
            "source": "app_usage_sample",
            "distracting_launches": launches,
            "lookback_minutes": FRAGMENTATION_LOOKBACK_MINUTES,
            "max_launches": FRAGMENTATION_MAX_LAUNCHES,
            "by_app": top_apps,
            "distracting_categories": sorted(DISTRACTING_CATEGORIES),
            "severity": severity,
        },
    )


# ---------------------------------------------------------------------------
# Score composition (pure)
# ---------------------------------------------------------------------------


def compute_estimate(
    window_start: datetime,
    window_end: datetime,
    signals: Sequence[FactorSignal],
    missing: Sequence[MissingSignal] = (),
    *,
    generated_at: datetime | None = None,
    snapshot_extra: Mapping[str, Any] | None = None,
) -> EnergyEstimate:
    """Combine factor signals into a 0-100 score with exact-sum components.

    Present factors' base weights renormalize to a 100-point budget; each
    penalty spends ``share * severity`` points, the bonus refills
    ``share * charge`` points of a base that starts at ``100 - bonus_budget``.
    With no present signal at all the estimate is honestly
    ``insufficient_data`` (never a fake 100).
    """
    window_start = _ensure_utc(window_start)
    window_end = _ensure_utc(window_end)
    generated_at = _ensure_utc(generated_at) if generated_at else datetime.now(UTC)

    seen: set[str] = set()
    for signal in signals:
        if signal.key not in FACTORS:
            raise ValueError(f"unknown factor key {signal.key!r}")
        if signal.key in seen:
            raise ValueError(f"duplicate factor key {signal.key!r}")
        seen.add(signal.key)

    missing_payload = [{"name": item.key, "reason": item.reason} for item in missing]
    snapshot: dict[str, Any] = {
        "engine": "cognitive_energy_v1",
        "as_of": window_start.date().isoformat(),
        "generated_at": generated_at.isoformat(),
        "missing_signals": missing_payload,
        **(dict(snapshot_extra) if snapshot_extra else {}),
    }

    if not signals:
        return EnergyEstimate(
            window_start=window_start,
            window_end=window_end,
            status=STATUS_INSUFFICIENT,
            score=None,
            score_exact=None,
            components=(),
            inputs_snapshot=snapshot,
        )

    by_key = {signal.key: signal for signal in signals}
    ordered = [spec for spec in FACTOR_SPECS if spec.key in by_key]
    total_weight = sum(spec.base_weight for spec in ordered)
    renormalized = len(ordered) < len(FACTOR_SPECS)

    bonus_budget = sum(
        spec.base_weight / total_weight * 100.0 for spec in ordered if spec.kind == "bonus"
    )
    base_points = 100.0 - bonus_budget

    components: list[dict[str, Any]] = [
        {
            "name": "base",
            "kind": "base",
            "weight": None,
            "raw": {
                "formula": "score = base + sum(term contributions)",
                "penalty_budget_points": round(100.0 - bonus_budget, 4),
                "bonus_budget_points": round(bonus_budget, 4),
                "renormalized": renormalized,
                "factors_present": [spec.key for spec in ordered],
                "factors_missing": missing_payload,
            },
            "contribution": base_points,
        }
    ]
    for spec in ordered:
        signal = by_key[spec.key]
        value = _clamp01(float(signal.value))
        share = spec.base_weight / total_weight
        max_points = share * 100.0
        contribution = max_points * value if spec.kind == "bonus" else -max_points * value
        components.append(
            {
                "name": spec.term,
                "kind": spec.kind,
                "weight": share,
                "raw": {
                    **signal.raw,
                    "base_weight": spec.base_weight,
                    "max_points": round(max_points, 4),
                    "normalized_input": value,
                },
                "contribution": contribution,
            }
        )

    score_exact = sum(item["contribution"] for item in components)
    # Bounded [0, 100] by construction; the clamp is a pure float-noise guard.
    score_exact = min(max(score_exact, 0.0), 100.0)
    return EnergyEstimate(
        window_start=window_start,
        window_end=window_end,
        status=STATUS_OK,
        score=round(score_exact),
        score_exact=score_exact,
        components=tuple(components),
        inputs_snapshot=snapshot,
    )


# ---------------------------------------------------------------------------
# open-wearables row digestion (reuses the mcp_server row helpers)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OwDigest:
    """Daily series digested from raw open-wearables REST rows."""

    sleep_scores_by_day: dict[date, float]
    sleep_score_source: str | None
    garmin_stress_by_day: dict[date, float]
    resilience_by_day: dict[date, float]
    rmssd_by_day: dict[date, float]
    sdnn_by_day: dict[date, float]
    charge_points: dict[str, tuple[tuple[datetime, float, str | None], ...]]


def digest_ow_rows(
    score_rows: Sequence[Mapping[str, Any]],
    sleep_rows: Sequence[Mapping[str, Any]],
    as_of: date,
) -> OwDigest:
    """Digest health-score + sleep-summary rows into the engine's daily series.

    Row digestion is the shared pure-function layer of
    :mod:`healthmes.mcp_server.interpret` (one copy, public names — never
    another module's privates).
    """
    sleep_scores, sleep_source = interpret.sleep_score_series(score_rows)
    garmin_stress = interpret.daily_series(
        interpret.score_points(score_rows, "stress", provider="garmin"), how="mean"
    )
    resilience = interpret.daily_series(
        interpret.resilience_score_points(score_rows), how="latest"
    )
    rmssd = interpret.summary_daily_values(sleep_rows, "avg_hrv_rmssd_ms", as_of)
    sdnn = interpret.summary_daily_values(sleep_rows, "avg_hrv_sdnn_ms", as_of)

    charge: dict[str, list[tuple[datetime, float, str | None]]] = {}
    for row in score_rows:
        category = row.get("category")
        if category not in CHARGE_PREFERENCE:
            continue
        recorded_at = interpret.parse_recorded_at(row.get("recorded_at"))
        value = interpret.as_float(row.get("value"))
        if recorded_at is None or value is None:
            continue
        provider = row.get("provider")
        charge.setdefault(str(category), []).append(
            (recorded_at, value, str(provider) if provider is not None else None)
        )

    return OwDigest(
        sleep_scores_by_day=sleep_scores,
        sleep_score_source=sleep_source,
        garmin_stress_by_day=garmin_stress,
        resilience_by_day=resilience,
        rmssd_by_day=rmssd,
        sdnn_by_day=sdnn,
        charge_points={key: tuple(points) for key, points in charge.items()},
    )


# ---------------------------------------------------------------------------
# Store-side context (calendar mirror + app usage)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoreDayContext:
    """Everything the per-window factors need from the healthmes store."""

    events: tuple[tuple[datetime, datetime], ...]
    calendar_active: bool
    usage: tuple[UsageBucket, ...]


def load_store_day_context(session: Session, day: date) -> StoreDayContext:
    """Prefetch one (UTC) day's calendar events and app-usage buckets."""
    day_start = datetime.combine(day, time.min, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)

    event_rows = session.scalars(
        select(CalendarEventMirror)
        .where(
            CalendarEventMirror.start_at < day_end,
            CalendarEventMirror.end_at > day_start,
        )
        .order_by(CalendarEventMirror.start_at)
    ).all()
    events = tuple(
        (_ensure_utc(row.start_at), _ensure_utc(row.end_at)) for row in event_rows
    )

    active_start = day_start - timedelta(days=CALENDAR_ACTIVE_LOOKAROUND_DAYS)
    active_end = day_end + timedelta(days=CALENDAR_ACTIVE_LOOKAROUND_DAYS)
    calendar_active = (
        session.scalar(
            select(CalendarEventMirror.id)
            .where(
                CalendarEventMirror.start_at < active_end,
                CalendarEventMirror.end_at > active_start,
            )
            .limit(1)
        )
        is not None
    )

    usage_start = day_start - timedelta(
        hours=USAGE_PRESENCE_LOOKBACK_HOURS, minutes=FRAGMENTATION_LOOKBACK_MINUTES
    )
    usage_rows = session.scalars(
        select(AppUsageSample)
        .where(
            AppUsageSample.bucket_start >= usage_start,
            AppUsageSample.bucket_start < day_end,
        )
        .order_by(AppUsageSample.bucket_start)
    ).all()
    usage = tuple(
        UsageBucket(
            bucket_start=_ensure_utc(row.bucket_start),
            app_package=row.app_package,
            launches=row.launches,
            category=row.category,
        )
        for row in usage_rows
    )
    return StoreDayContext(events=events, calendar_active=calendar_active, usage=usage)


# ---------------------------------------------------------------------------
# open-wearables reader (network boundary; degrades, never raises)
# ---------------------------------------------------------------------------

# History fetched per compute: baseline window + sleep-debt window headroom.
OW_FETCH_DAYS = interpret.BASELINE_WINDOW_DAYS + 7

OW_STATUS_OK = "ok"
OW_STATUS_UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class OwRows:
    """Raw open-wearables rows for one compute (empty when unavailable)."""

    score_rows: tuple[dict[str, Any], ...] = ()
    sleep_rows: tuple[dict[str, Any], ...] = ()
    status: str = OW_STATUS_OK
    detail: str | None = None


class EnergyDataReader(Protocol):
    """Anything that can produce ``OwRows`` for a day (sync or async)."""

    def read(self, as_of: date) -> Any: ...  # returns OwRows or Awaitable[OwRows]


class OwEnergyReader:
    """Reads health scores + sleep summaries through the mcp_server ow_client.

    All REST mechanics (base URL, API-key header, pagination) live in
    ``healthmes/mcp_server/ow_client.py``; this class only scopes the fetch
    window and degrades to empty rows on any failure so the hourly loop and
    the forecast endpoint keep working while the backend is down.
    """

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client
        self._user_id: str | None = None
        self._warned = False

    def _ensure_client(self) -> Any:
        if self._client is None:
            # Deferred import mirrors OwHealthReader: the REST client belongs
            # to the mcp_server scope and must not be an import-time dependency.
            from healthmes.mcp_server.ow_client import OWClient

            self._client = OWClient.from_settings(self._settings)
        return self._client

    async def _resolve_user_id(self, client: Any) -> str:
        """Shared single-user policy (settings/env pin, else exactly-one).

        Never "the first user": a second account on the backend would
        silently mix subjects in the persisted energy history (the table has
        no user column). Ambiguity degrades via the read() error path.
        """
        if self._user_id is not None:
            return self._user_id
        from healthmes.mcp_server.ow_client import resolve_single_user_id

        self._user_id = await resolve_single_user_id(client, self._settings)
        return self._user_id

    async def read(self, as_of: date) -> OwRows:
        """Health-score + sleep-summary rows covering the baseline history."""
        fetch_start = as_of - timedelta(days=OW_FETCH_DAYS)
        end_exclusive = as_of + timedelta(days=1)
        try:
            client = self._ensure_client()
            user_id = await self._resolve_user_id(client)
            score_rows = await client.collect_health_scores(
                user_id,
                start_date=fetch_start.isoformat(),
                end_date=end_exclusive.isoformat(),
            )
            sleep_rows = await client.collect_sleep_summaries(
                user_id, fetch_start.isoformat(), end_exclusive.isoformat()
            )
            self._warned = False
            return OwRows(tuple(score_rows), tuple(sleep_rows), OW_STATUS_OK)
        except Exception as exc:  # degrade, never break the loop
            if not self._warned:
                logger.warning(
                    "open-wearables unavailable for the energy engine (%s: %s); "
                    "health factors drop and weights renormalize.",
                    type(exc).__name__,
                    exc,
                )
                self._warned = True
            return OwRows(status=OW_STATUS_UNAVAILABLE, detail=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Engine service (on-demand compute + hourly persist + day forecast)
# ---------------------------------------------------------------------------

# Factors that come from open-wearables (dropped together when it is down).
_OW_FACTOR_KEYS = ("sleep_debt", "stress", "hrv_deviation", "body_battery")


class CognitiveEnergyEngine:
    """Computes, persists and serves cognitive-energy estimates.

    All collaborators are injectable for tests; defaults wire the real store
    session factory and the ow_client-backed reader.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: sessionmaker[Session] | None = None,
        ow_reader: EnergyDataReader | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._reader: EnergyDataReader = (
            ow_reader if ow_reader is not None else OwEnergyReader(settings)
        )
        self._now = now_provider if now_provider is not None else lambda: datetime.now(UTC)

    # -- public API ----------------------------------------------------------

    def compute_window(self, window_start: datetime | None = None) -> EnergyEstimate:
        """On-demand estimate for one hourly window (default: the current hour)."""
        now = _ensure_utc(self._now())
        start = _floor_hour(_ensure_utc(window_start) if window_start else now)
        end = start + timedelta(minutes=WINDOW_MINUTES)
        day = start.date()

        ow = _run_maybe_async(self._reader.read(day))
        digest = digest_ow_rows(ow.score_rows, ow.sleep_rows, day)
        with session_scope(self._session_factory) as session:
            ctx = load_store_day_context(session, day)
        return self._estimate_window(digest, ow, ctx, start, end, now)

    def persist_current_window(self) -> EnergyEstimate:
        """Compute the current hour and upsert it (the hourly scheduler job body).

        Insufficient estimates are returned but never persisted (the score
        column is NOT NULL and a fake number would poison the history).
        """
        estimate = self.compute_window()
        if estimate.status != STATUS_OK:
            logger.info(
                "Energy window %s not persisted: %s",
                estimate.window_start.isoformat(),
                estimate.status,
            )
            return estimate
        with session_scope(self._session_factory) as session:
            self._upsert(session, estimate)
        logger.info(
            "Energy window %s persisted with score %s",
            estimate.window_start.isoformat(),
            estimate.score,
        )
        return estimate

    def forecast_day(self, day: date) -> list[WindowSlot]:
        """All 24 hourly windows of a (UTC) day: persisted rows + on-demand fills.

        Persisted rows are authoritative for their windows (they saw the
        signals live); the remaining hours are computed on demand — future
        hours naturally drop the fragmentation term.
        """
        now = _ensure_utc(self._now())
        day_start = datetime.combine(day, time.min, tzinfo=UTC)
        day_end = day_start + timedelta(days=1)

        with session_scope(self._session_factory) as session:
            rows = session.scalars(
                select(CognitiveEnergyEstimate)
                .where(
                    CognitiveEnergyEstimate.window_start >= day_start,
                    CognitiveEnergyEstimate.window_start < day_end,
                )
                .order_by(CognitiveEnergyEstimate.window_start)
            ).all()
            persisted: dict[datetime, WindowSlot] = {}
            for row in rows:
                start = _floor_hour(_ensure_utc(row.window_start))
                payload = row.components or {}
                persisted[start] = WindowSlot(
                    window_start=_ensure_utc(row.window_start),
                    window_end=_ensure_utc(row.window_end),
                    source="persisted",
                    status=STATUS_OK,
                    score=row.score,
                    score_exact=payload.get("score_exact"),
                    components=tuple(payload.get("items", ())),
                )
            ctx = load_store_day_context(session, day)

        windows = [
            (day_start + timedelta(hours=hour), day_start + timedelta(hours=hour + 1))
            for hour in range(24)
        ]
        need_compute = [w for w in windows if w[0] not in persisted]
        digest: OwDigest | None = None
        ow = OwRows()
        if need_compute:
            ow = _run_maybe_async(self._reader.read(day))
            digest = digest_ow_rows(ow.score_rows, ow.sleep_rows, day)

        slots: list[WindowSlot] = []
        for start, end in windows:
            if start in persisted:
                slots.append(persisted[start])
                continue
            assert digest is not None
            estimate = self._estimate_window(digest, ow, ctx, start, end, now)
            slots.append(
                WindowSlot(
                    window_start=estimate.window_start,
                    window_end=estimate.window_end,
                    source="computed",
                    status=estimate.status,
                    score=estimate.score,
                    score_exact=estimate.score_exact,
                    components=estimate.components,
                )
            )
        return slots

    # -- internals ------------------------------------------------------------

    def _estimate_window(
        self,
        digest: OwDigest,
        ow: OwRows,
        ctx: StoreDayContext,
        window_start: datetime,
        window_end: datetime,
        now: datetime,
    ) -> EnergyEstimate:
        as_of = window_start.date()
        signals: list[FactorSignal] = []
        missing: list[MissingSignal] = []

        def take(result: FactorSignal | MissingSignal) -> None:
            if isinstance(result, FactorSignal):
                signals.append(result)
            else:
                missing.append(result)

        if ow.status == OW_STATUS_OK:
            take(
                sleep_debt_signal(
                    digest.sleep_scores_by_day, as_of, source=digest.sleep_score_source
                )
            )
            take(stress_signal(digest.garmin_stress_by_day, digest.resilience_by_day, as_of))
            take(hrv_signal(digest.rmssd_by_day, digest.sdnn_by_day, as_of))
            take(charge_signal(digest.charge_points, as_of))
        else:
            missing.extend(MissingSignal(key, "ow_unavailable") for key in _OW_FACTOR_KEYS)

        take(
            meeting_load_signal(
                ctx.events, window_start, window_end, calendar_active=ctx.calendar_active
            )
        )
        take(fragmentation_signal(ctx.usage, window_start, now))

        recent_sleep = sorted(digest.sleep_scores_by_day.items())[-8:]
        snapshot_extra: dict[str, Any] = {
            "ow": {
                "status": ow.status,
                **({"detail": ow.detail} if ow.detail else {}),
                "sleep_score_source": digest.sleep_score_source,
                "sleep_scores_by_day": {d.isoformat(): v for d, v in recent_sleep},
                "stress_days": {
                    "garmin": len(digest.garmin_stress_by_day),
                    "resilience": len(digest.resilience_by_day),
                },
                "hrv_days": {"rmssd": len(digest.rmssd_by_day), "sdnn": len(digest.sdnn_by_day)},
                "charge_readings": {
                    category: len(points) for category, points in digest.charge_points.items()
                },
            },
            "store": {
                "calendar_active": ctx.calendar_active,
                "events_in_day": len(ctx.events),
                "usage_buckets_seen": len(ctx.usage),
            },
        }
        return compute_estimate(
            window_start,
            window_end,
            signals,
            missing,
            generated_at=now,
            snapshot_extra=snapshot_extra,
        )

    def _upsert(self, session: Session, estimate: EnergyEstimate) -> None:
        """Insert or update the row for this exact window (job re-runs are idempotent)."""
        assert estimate.score is not None
        existing = session.scalars(
            select(CognitiveEnergyEstimate).where(
                CognitiveEnergyEstimate.window_start == estimate.window_start
            )
        ).first()
        if existing is not None:
            existing.window_end = estimate.window_end
            existing.score = estimate.score
            existing.components = estimate.components_payload()
            existing.inputs_snapshot = estimate.inputs_snapshot
            return
        session.add(
            CognitiveEnergyEstimate(
                window_start=estimate.window_start,
                window_end=estimate.window_end,
                score=estimate.score,
                components=estimate.components_payload(),
                inputs_snapshot=estimate.inputs_snapshot,
            )
        )


def build_energy_job(
    settings: Settings,
    *,
    engine_factory: Callable[[], CognitiveEnergyEngine] | None = None,
) -> Callable[[], None]:
    """Zero-arg hourly persist callable for the scheduler hook (PLAN §3).

    Wiring (composition root, see needs.app_wiring)::

        scheduler = create_scheduler(settings)
        register_energy_job(scheduler, build_energy_job(settings))
        start_scheduler(settings, scheduler=scheduler)

    The engine is constructed lazily on the first run so importing this module
    never touches the store or the network; exceptions are contained per run.
    """
    engine: CognitiveEnergyEngine | None = None

    def run_energy_persist() -> None:
        nonlocal engine
        try:
            if engine is None:
                if engine_factory is not None:
                    engine = engine_factory()
                else:
                    engine = CognitiveEnergyEngine(settings)
            engine.persist_current_window()
        except Exception:
            logger.exception("Cognitive-energy persist failed; next hour will retry.")

    return run_energy_persist
