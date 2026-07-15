"""Unit tests for the deterministic interpretation layer (pure math, no IO).

The baseline-math cases use hand-computed expected values:

- history [40, 50, 60, 50, 50] -> median 50; mean 50;
  squared deviations 100+0+100+0+0 = 200; sample variance 200/4 = 50;
  sample stdev sqrt(50) ~= 7.0711; so z(40) = (40-50)/7.0711 = -1.4142.
- sleep scores {70, 80, 90} -> nightly debts {30, 20, 10} -> index 20.0.
"""

import datetime as dt
import math

from healthmes.mcp_server import interpret

D = dt.date(2026, 7, 8)


def days_ago(n: int) -> dt.date:
    return D - dt.timedelta(days=n)


class TestPrimitives:
    def test_trailing_median(self):
        assert interpret.trailing_median([1.0, 9.0, 5.0]) == 5.0
        assert interpret.trailing_median([1.0, 2.0, 3.0, 4.0]) == 2.5
        assert interpret.trailing_median([]) is None

    def test_sample_stdev_hand_computed(self):
        stdev = interpret.sample_stdev([40.0, 50.0, 60.0, 50.0, 50.0])
        assert stdev is not None
        assert math.isclose(stdev, math.sqrt(50.0))
        assert interpret.sample_stdev([42.0]) is None

    def test_z_score_hand_computed(self):
        history = [40.0, 50.0, 60.0, 50.0, 50.0]
        z = interpret.z_score(40.0, history)
        assert z is not None
        assert math.isclose(z, -10.0 / math.sqrt(50.0))  # -1.41421...
        assert round(z, 2) == -1.41

    def test_z_score_undefined_for_thin_or_flat_history(self):
        assert interpret.z_score(40.0, [50.0]) is None
        assert interpret.z_score(40.0, [50.0, 50.0, 50.0]) is None  # zero spread

    def test_coverage_and_confidence_bands(self):
        assert interpret.coverage_ratio(7, 14) == 0.5
        assert interpret.confidence_label(10, 14) == "high"  # 0.71 cov, n=10
        assert interpret.confidence_label(6, 14) == "medium"  # 0.43 cov, n=6
        assert interpret.confidence_label(4, 14) == "low"  # n < 5
        assert interpret.confidence_label(0, 0) == "low"

    def test_daily_series_aggregations(self):
        day = dt.datetime(2026, 7, 8, 8, 0, tzinfo=dt.UTC)
        points = [(day, 60.0), (day.replace(hour=20), 80.0)]
        assert interpret.daily_series(points, how="mean") == {D: 70.0}
        assert interpret.daily_series(points, how="max") == {D: 80.0}
        assert interpret.daily_series(points, how="latest") == {D: 80.0}


class TestMetricBaseline:
    def test_hand_computed_baseline_and_z(self):
        daily = {
            D: 40.0,
            days_ago(1): 40.0,
            days_ago(2): 50.0,
            days_ago(3): 60.0,
            days_ago(4): 50.0,
            days_ago(5): 50.0,
        }
        result = interpret.metric_baseline(daily, D)
        assert result["status"] == "ok"
        assert result["current"] == {"date": "2026-07-08", "value": 40.0}
        assert result["baseline_median"] == 50.0
        assert result["delta"] == -10.0
        assert result["delta_pct"] == -20.0
        assert result["z_score"] == -1.41  # hand-computed above
        assert result["n_days"] == 5
        assert result["coverage"] == 0.36  # 5/14
        assert result["confidence"] == "low"

    def test_current_day_never_skews_its_own_baseline(self):
        # A wild current value must not move the baseline median.
        daily = {days_ago(i): 50.0 for i in range(1, 7)} | {D: 500.0}
        result = interpret.metric_baseline(daily, D)
        assert result["baseline_median"] == 50.0
        assert result["current"]["value"] == 500.0

    def test_insufficient_when_below_min_days(self):
        daily = {D: 40.0, days_ago(1): 42.0, days_ago(2): 44.0}  # 2 history days
        result = interpret.metric_baseline(daily, D)
        assert result["status"] == "insufficient_data"
        assert result["reason"] == "need_at_least_5_baseline_days"
        assert result["n_days"] == 2
        assert "baseline_median" not in result

    def test_insufficient_when_no_data(self):
        result = interpret.metric_baseline({}, D)
        assert result["status"] == "insufficient_data"
        assert result["reason"] == "no_data_on_or_before_as_of"

    def test_data_after_as_of_is_ignored(self):
        daily = {D + dt.timedelta(days=1): 99.0}
        result = interpret.metric_baseline(daily, D)
        assert result["status"] == "insufficient_data"


class TestSleepDebt:
    def test_hand_computed_index(self):
        scores = {D: 70.0, days_ago(1): 80.0, days_ago(2): 90.0}
        result = interpret.sleep_debt(scores, D)
        assert result["status"] == "ok"
        assert result["index"] == 20.0  # mean of (30, 20, 10)
        assert result["nights_counted"] == 3
        assert result["coverage"] == 0.43  # 3/7
        assert result["last_night"] == {"date": "2026-07-08", "score": 70.0}

    def test_scores_above_100_never_produce_negative_debt(self):
        scores = {D: 100.0, days_ago(1): 100.0, days_ago(2): 100.0}
        assert interpret.sleep_debt(scores, D)["index"] == 0.0

    def test_insufficient_below_min_nights(self):
        result = interpret.sleep_debt({D: 70.0, days_ago(1): 80.0}, D)
        assert result["status"] == "insufficient_data"
        assert result["reason"] == "need_at_least_3_scored_nights"

    def test_only_trailing_window_nights_count(self):
        scores = {days_ago(i): 50.0 for i in (0, 1, 8, 9, 10)}  # 3 outside 7d window
        result = interpret.sleep_debt(scores, D)
        assert result["nights_counted"] == 2
        assert result["status"] == "insufficient_data"


class TestStressContext:
    def test_prefers_native_garmin_stress(self):
        result = interpret.stress_context({D: 62.0}, {D: 80.0}, D)
        assert result["status"] == "ok"
        assert result["source"] == "garmin_stress"
        assert result["value"] == 62.0
        assert result["stale_days"] == 0
        assert result["confidence"] == "high"

    def test_falls_back_to_resilience_proxy(self):
        result = interpret.stress_context({}, {D: 65.0}, D)
        assert result["source"] == "internal_resilience_proxy"
        assert result["value"] == 35.0  # 100 - 65
        assert result["confidence"] == "medium"

    def test_stale_readings_lower_confidence_then_expire(self):
        two_days_ago = days_ago(2)
        result = interpret.stress_context({two_days_ago: 40.0}, {}, D)
        assert result["stale_days"] == 2
        assert result["confidence"] == "medium"
        expired = interpret.stress_context({days_ago(4): 40.0}, {}, D)
        assert expired["status"] == "insufficient_data"

    def test_insufficient_when_no_source(self):
        result = interpret.stress_context({}, {}, D)
        assert result["status"] == "insufficient_data"
        assert result["confidence"] == "low"


class TestOverallConfidence:
    def test_weakest_confirmed_block_wins(self):
        blocks = [
            {"status": "ok", "confidence": "high"},
            {"status": "ok", "confidence": "medium"},
            {"status": "insufficient_data", "confidence": "low"},
        ]
        assert interpret.overall_confidence(blocks) == "medium"

    def test_all_insufficient_is_low(self):
        assert interpret.overall_confidence([{"status": "insufficient_data"}]) == "low"
        assert interpret.overall_confidence([]) == "low"


class TestMetricBaselineStaleness:
    """Defect 5: an optional max_stale_days ceiling on the 'current' reading.

    Without the ceiling the latest value on/before as_of is treated as current
    no matter how old; the readiness call site passes a ceiling so a stale HRV
    is not reported as today's state. Other callers pass no ceiling and keep
    the original behavior.
    """

    def test_stale_current_is_insufficient_only_when_gated(self):
        # Latest reading is 7 days before as_of, with 5 prior history days.
        daily = {days_ago(n): 50.0 for n in (7, 8, 9, 10, 11, 12)}
        # Ungated (default): enough history -> ok (baseline behavior preserved).
        assert interpret.metric_baseline(daily, D)["status"] == "ok"
        # Gated at 3 days: the current reading is stale -> honest insufficient.
        gated = interpret.metric_baseline(daily, D, max_stale_days=3)
        assert gated["status"] == "insufficient_data"
        assert gated["reason"] == "current_reading_stale_gt_3_days"
        assert gated["stale_days"] == 7
        assert gated["current"] == {"date": days_ago(7).isoformat(), "value": 50.0}

    def test_freshness_gate_boundary(self):
        fresh = {days_ago(n): 50.0 for n in (3, 4, 5, 6, 7, 8)}  # latest exactly 3d old
        assert interpret.metric_baseline(fresh, D, max_stale_days=3)["status"] == "ok"
        stale = {days_ago(n): 50.0 for n in (4, 5, 6, 7, 8, 9)}  # latest 4d old
        result = interpret.metric_baseline(stale, D, max_stale_days=3)
        assert result["status"] == "insufficient_data"
        assert result["stale_days"] == 4


class TestChooseStressSeries:
    """Defect 6: pick ONE stress series by freshness near as_of, never mix."""

    def test_fresh_garmin_wins(self):
        series, which = interpret.choose_stress_series({D: 60.0}, {D: 40.0}, D)
        assert which == "garmin"
        assert series == {D: 60.0}

    def test_stale_garmin_yields_to_fresh_proxy(self):
        # Garmin reading is 60 days old; the proxy is today -> proxy must win
        # (a stale Garmin reading must NOT suppress a fresh proxy).
        series, which = interpret.choose_stress_series({days_ago(60): 40.0}, {D: 35.0}, D)
        assert which == "proxy"
        assert series == {D: 35.0}

    def test_both_empty_is_none(self):
        assert interpret.choose_stress_series({}, {}, D) == ({}, "none")

    def test_neither_fresh_prefers_most_recent_then_garmin_on_tie(self):
        newer_g = interpret.choose_stress_series({days_ago(5): 1.0}, {days_ago(10): 2.0}, D)
        assert newer_g[1] == "garmin"
        newer_p = interpret.choose_stress_series({days_ago(10): 1.0}, {days_ago(5): 2.0}, D)
        assert newer_p[1] == "proxy"
        tie = interpret.choose_stress_series({days_ago(5): 1.0}, {days_ago(5): 2.0}, D)
        assert tie[1] == "garmin"
