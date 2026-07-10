"""Unit tests for the pure Phase-2 focus template (gates, dips, factors).

Standard two-day profile (Mon 2026-07-06, Tue 2026-07-07, UTC), hand-computed:
hours 8-13 and 16-19 score 75/79 (mean 77, x10 hours), hour 14 scores 52/48
(mean 50), hour 15 scores 46/42 (mean 44) -> 24 windows, baseline
(10*154 + 100 + 88)/24 = 72.0, dip hours {14, 15} (deficits 22/28), block
"14-16h" with dip mean (52+48+46+42)/4 = 47.0.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from healthmes.api.insight_focus import (
    KIND_FOCUS_DROP_BY_HOUR,
    _app_label,
    compute_focus_drop,
)
from healthmes.api.insight_templates import SkippedTemplate, TemplateResult


@dataclass
class FakeEstimate:
    """Bare EnergyEstimateLike (CognitiveEnergyEstimate shape)."""

    window_start: datetime
    window_end: datetime
    score: int
    components: dict[str, Any]


@dataclass
class FakeUsage:
    """Bare AppUsageLike (AppUsageSample shape)."""

    bucket_start: datetime
    app_package: str
    launches: int


@dataclass
class FakeEvent:
    """Bare CalendarEventLike (naive datetimes, like sqlite round-trips)."""

    summary: str | None
    start_at: datetime
    end_at: datetime


def estimate(day: int, hour: int, score: int, sleep_index: float | None = None) -> FakeEstimate:
    start = datetime(2026, 7, 6 + day, hour, tzinfo=UTC)
    items: list[dict[str, Any]] = []
    if sleep_index is not None:
        items = [
            {
                "name": "sleep_debt_penalty",
                "kind": "penalty",
                "weight": 0.3,
                "raw": {"index": sleep_index},
                "contribution": -0.3 * sleep_index,
            }
        ]
    return FakeEstimate(
        window_start=start,
        window_end=start + timedelta(hours=1),
        score=score,
        components={"version": 1, "items": items, "score_exact": float(score)},
    )


DIP_PROFILE: dict[int, tuple[int, int]] = {
    hour: (75, 79) for hour in (8, 9, 10, 11, 12, 13, 16, 17, 18, 19)
}
DIP_PROFILE[14] = (52, 48)
DIP_PROFILE[15] = (46, 42)


def profile(
    hour_scores: dict[int, tuple[int, int]] = DIP_PROFILE,
    sleep_index: tuple[float | None, float | None] = (None, None),
) -> list[FakeEstimate]:
    return [
        estimate(day, hour, scores[day], sleep_index[day])
        for day in (0, 1)
        for hour, scores in sorted(hour_scores.items())
    ]


# ---------------------------------------------------------------------------
# Minimum-data gates (no false positives on sparse data)
# ---------------------------------------------------------------------------


def test_gate_minimum_windows():
    result = compute_focus_drop(profile()[:-1], [], [])  # 23 windows

    assert isinstance(result, SkippedTemplate)
    assert result.kind == KIND_FOCUS_DROP_BY_HOUR
    assert result.reason == "insufficient_data"
    assert isinstance(compute_focus_drop(profile(), [], []), TemplateResult)


def test_gate_single_day():
    one_day = [estimate(0, hour, 75) for hour in range(24)]  # 24 windows, 1 day

    result = compute_focus_drop(one_day, [], [])

    assert isinstance(result, SkippedTemplate)
    assert result.reason == "insufficient_data"


def test_gate_qualified_hours():
    # 24 windows over 2 days but only 5 hour buckets observed twice.
    rows = [estimate(day, hour, 75) for day in (0, 1) for hour in range(5)]
    rows += [estimate(0, hour, 75) for hour in range(5, 19)]
    assert len(rows) == 24

    result = compute_focus_drop(rows, [], [])

    assert isinstance(result, SkippedTemplate)
    assert result.reason == "insufficient_data"


def test_flat_profile_is_no_dip_not_insufficient():
    flat = profile({hour: (75, 75) for hour in range(8, 20)})

    result = compute_focus_drop(flat, [], [])

    assert isinstance(result, SkippedTemplate)
    assert result.reason == "no_dip_detected"


# ---------------------------------------------------------------------------
# Dip detection and block selection
# ---------------------------------------------------------------------------


def test_dip_without_factors_states_so():
    result = compute_focus_drop(profile(), [], [])

    assert isinstance(result, TemplateResult)
    assert result.statement == (
        "14-16h focus drop (energy 47 vs baseline 72, UTC): no co-occurring factor identified."
    )
    assert result.confidence == 0.143
    assert result.evidence["baseline_mean"] == 72.0
    assert result.evidence["dip_hours"] == [14, 15]
    assert result.evidence["block"]["label"] == "14-16h"
    assert result.evidence["block"]["mean"] == 47.0
    assert result.evidence["block"]["deficit"] == 25.0
    factors = result.evidence["factors"]
    assert factors["sleep_deficit"] == {"qualified": False, "status": "no_data", "threshold": 25.0}
    assert factors["app_switching"] == {"qualified": False, "status": "no_data", "threshold": 6.0}
    assert factors["meeting_dense_mornings"] == {
        "qualified": False,
        "status": "no_data",
        "threshold": 3.0,
    }


def test_block_selection_prefers_largest_total_deficit():
    # Nine normal hours mean 78 (77/79); dips at 9 (56) and 14-15 (50/44):
    # baseline = (9*78 + 56 + 50 + 44)/12 = 852/12 = 71.0
    # deficits: h9 = 15, h14 = 21, h15 = 27 -> block [14, 15] wins (48 > 15).
    scores = {hour: (77, 79) for hour in (8, 10, 11, 12, 13, 16, 17, 18, 19)}
    scores[9] = (55, 57)
    scores[14] = (49, 51)
    scores[15] = (43, 45)

    result = compute_focus_drop(profile(scores), [], [])

    assert isinstance(result, TemplateResult)
    assert result.statement.startswith("14-16h focus drop (energy 47 vs baseline 71, UTC)")
    assert result.evidence["dip_hours"] == [9, 14, 15]
    assert result.evidence["block"]["hours"] == [14, 15]


def test_block_tie_prefers_earliest():
    # Ten normal hours mean 78; hours 9 and 14 both mean 54:
    # baseline = (10*78 + 54 + 54)/12 = 888/12 = 74.0, both deficits 20.0.
    scores = {hour: (77, 79) for hour in (8, 10, 11, 12, 13, 15, 16, 17, 18, 19)}
    scores[9] = (53, 55)
    scores[14] = (53, 55)

    result = compute_focus_drop(profile(scores), [], [])

    assert isinstance(result, TemplateResult)
    assert result.evidence["block"]["label"] == "09-10h"
    assert result.statement.startswith("09-10h focus drop (energy 54 vs baseline 74, UTC)")


# ---------------------------------------------------------------------------
# Co-occurring factors
# ---------------------------------------------------------------------------


def test_sleep_factor_thresholds():
    qualified = compute_focus_drop(profile(sleep_index=(30.0, 26.0)), [], [])
    assert isinstance(qualified, TemplateResult)
    assert "sleep deficit" in qualified.statement
    assert qualified.evidence["factors"]["sleep_deficit"] == {
        "qualified": True,
        "mean_debt_index": 28.0,  # (30+30+26+26)/4
        "n_windows": 4,
        "threshold": 25.0,
    }

    mild = compute_focus_drop(profile(sleep_index=(24.0, 24.0)), [], [])
    assert isinstance(mild, TemplateResult)
    assert "sleep deficit" not in mild.statement
    assert mild.evidence["factors"]["sleep_deficit"]["qualified"] is False
    assert mild.evidence["factors"]["sleep_deficit"]["mean_debt_index"] == 24.0


def test_app_factor_counts_only_usage_covered_days():
    # Usage exists on Tuesday only -> Monday's dip windows stay out of the
    # denominator: Slack 8+6 = 14 launches over 2 covered hours = 7.0/hour.
    usage = [
        FakeUsage(datetime(2026, 7, 7, 14, 0, tzinfo=UTC), "com.Slack", 8),
        FakeUsage(datetime(2026, 7, 7, 15, 0, tzinfo=UTC), "com.Slack", 6),
        FakeUsage(datetime(2026, 7, 7, 14, 30, tzinfo=UTC), "com.instagram.android", 2),
    ]

    result = compute_focus_drop(profile(), usage, [])

    assert isinstance(result, TemplateResult)
    assert "Slack 7 launches/hour" in result.statement
    assert result.evidence["factors"]["app_switching"] == {
        "qualified": True,
        "app_package": "com.Slack",
        "app_label": "Slack",
        "launches": 14,
        "launches_per_hour": 7.0,
        "total_launches": 16,
        "total_launches_per_hour": 8.0,
        "windows_covered": 2,
        "hours_covered": 2.0,
        "threshold": 6.0,
    }


def test_app_factor_below_threshold_stays_out_of_statement():
    usage = [
        FakeUsage(datetime(2026, 7, 6, 14, 0, tzinfo=UTC), "com.Slack", 5),
        FakeUsage(datetime(2026, 7, 7, 15, 0, tzinfo=UTC), "com.Slack", 5),
    ]

    result = compute_focus_drop(profile(), usage, [])

    assert isinstance(result, TemplateResult)
    assert "launches/hour" not in result.statement
    app = result.evidence["factors"]["app_switching"]
    assert app["qualified"] is False
    assert app["launches_per_hour"] == 2.5  # 10 launches / 4 covered hours


def test_all_three_factors_join_in_plan_order_with_naive_event_times():
    # Mornings: Mon 07:00/08:00/09:00/11:30 = 4, Tue 08:30/09:30/10:30 = 3
    # -> mean 3.5 >= 3.0; the 12:00 lunch and 14:30 afternoon block never count.
    events = [
        FakeEvent("Standup", datetime(2026, 7, 6, 7, 0), datetime(2026, 7, 6, 7, 30)),
        FakeEvent("Sync", datetime(2026, 7, 6, 8, 0), datetime(2026, 7, 6, 8, 30)),
        FakeEvent("Review", datetime(2026, 7, 6, 9, 0), datetime(2026, 7, 6, 9, 45)),
        FakeEvent("Interview", datetime(2026, 7, 6, 11, 30), datetime(2026, 7, 6, 12, 0)),
        FakeEvent("Standup", datetime(2026, 7, 7, 8, 30), datetime(2026, 7, 7, 9, 0)),
        FakeEvent("Planning", datetime(2026, 7, 7, 9, 30), datetime(2026, 7, 7, 10, 30)),
        FakeEvent("Retro", datetime(2026, 7, 7, 10, 30), datetime(2026, 7, 7, 11, 0)),
        FakeEvent("Lunch", datetime(2026, 7, 7, 12, 0), datetime(2026, 7, 7, 13, 0)),
        FakeEvent("Afternoon block", datetime(2026, 7, 6, 14, 30), datetime(2026, 7, 6, 15, 0)),
    ]
    usage = [
        FakeUsage(datetime(2026, 7, 6, 14, 0, tzinfo=UTC), "com.Slack", 9),
        FakeUsage(datetime(2026, 7, 6, 15, 0, tzinfo=UTC), "com.Slack", 9),
        FakeUsage(datetime(2026, 7, 7, 14, 0, tzinfo=UTC), "com.Slack", 9),
        FakeUsage(datetime(2026, 7, 7, 15, 0, tzinfo=UTC), "com.Slack", 9),
    ]

    result = compute_focus_drop(profile(sleep_index=(30.0, 26.0)), usage, events)

    assert isinstance(result, TemplateResult)
    assert result.statement == (
        "14-16h focus drop (energy 47 vs baseline 72, UTC): "
        "sleep deficit + Slack 9 launches/hour + meeting-dense mornings."
    )
    meetings = result.evidence["factors"]["meeting_dense_mornings"]
    assert meetings == {
        "qualified": True,
        "mean_morning_events": 3.5,
        "n_days": 2,
        "morning_hours": "06:00-12:00",
        "threshold": 3.0,
    }


def test_app_label_heuristic():
    assert _app_label("com.Slack") == "Slack"
    assert _app_label("com.slack") == "Slack"
    assert _app_label("com.instagram.android") == "Instagram"
    assert _app_label("Slack") == "Slack"
    assert _app_label("com.android") == "com.android"  # nothing readable -> raw
