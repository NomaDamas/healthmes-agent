"""HR arousal-hint interpretation pipeline (healthmes/mcp_server/arousal.py).

Deterministic synthetic-day fixtures: a quiet afternoon with elevated HR must
produce exactly one hint; movement, workouts, night hours, thin baselines,
and sparse coverage must all degrade honestly instead of inventing hints.
"""

import datetime as dt
from zoneinfo import ZoneInfo

from healthmes.mcp_server import arousal

TZ = ZoneInfo("Asia/Seoul")
DAY = dt.date(2026, 7, 20)


def _t(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(DAY, dt.time(hour, minute), tzinfo=TZ)


def _steps_still(start_h: int, end_h: int, every_min: int = 5):
    """Zero-step buckets across [start_h, end_h)."""
    out = []
    at = _t(start_h)
    while at < _t(end_h):
        out.append((at, 0.0))
        at += dt.timedelta(minutes=every_min)
    return out


def _hr_flat(start_h: int, end_h: int, bpm: float, every_min: int = 2):
    out = []
    at = _t(start_h)
    while at < _t(end_h):
        out.append((at, bpm))
        at += dt.timedelta(minutes=every_min)
    return out


RHR_14D = [(DAY - dt.timedelta(days=i), 58.0) for i in range(1, 15)]


def _build(hr, steps, workouts=(), rhr=RHR_14D, truncated=False, meals=()):
    return arousal.build_arousal_hints(
        day=DAY,
        tz=TZ,
        hr_samples=hr,
        step_samples=steps,
        workout_spans=list(workouts),
        daily_rhr=rhr,
        truncated=truncated,
        context_for=lambda start, end: [],
        meals=list(meals),
    )


# --- happy path --------------------------------------------------------------


def test_sustained_quiet_elevation_yields_one_hint():
    # Quiet 13-17h; HR 60 except a 14:00-14:30 stretch at 74 (58+12=70 threshold).
    hr = _hr_flat(13, 14, 60.0) + _hr_flat(14, 15, 74.0) + _hr_flat(15, 17, 61.0)
    payload = _build(hr, _steps_still(13, 17))

    assert payload["status"] == "ok"
    assert payload["baseline_bpm"] == 58.0
    assert len(payload["intervals"]) == 1
    hint = payload["intervals"][0]
    assert hint["minutes"] >= arousal.MIN_HINT_MINUTES
    assert hint["hr_peak"] == 74.0
    assert hint["elevation_bpm"] == 16.0
    assert "14:00" in hint["start"]


def test_brief_spike_below_min_duration_is_ignored():
    hr = _hr_flat(13, 17, 60.0)
    # 6-minute spike only (min is 10)
    hr += [(_t(14, m), 75.0) for m in range(0, 6, 2)]
    payload = _build(sorted(hr), _steps_still(13, 17))
    assert payload["status"] == "ok"
    assert payload["intervals"] == []


def test_meal_log_appears_as_context():
    hr = _hr_flat(14, 15, 75.0) + _hr_flat(13, 14, 60.0) + _hr_flat(15, 17, 60.0)
    payload = _build(
        sorted(hr),
        _steps_still(13, 17),
        meals=[(_t(13, 40), "아이스 아메리카노")],
    )
    [hint] = payload["intervals"]
    assert any("아메리카노" in c for c in hint["likely_context"])


# --- honest degradation ------------------------------------------------------


def test_movement_disqualifies_windows():
    # Same elevated HR but the user was walking: steps > threshold everywhere.
    hr = _hr_flat(13, 17, 80.0)
    steps = [(at, 120.0) for at, _ in _steps_still(13, 17)]
    payload = _build(hr, steps)
    assert payload["status"] == "insufficient_data"
    assert payload["reason"] == "quiet_window_coverage_too_low"


def test_workout_span_is_masked():
    hr = _hr_flat(13, 17, 80.0)
    payload = _build(
        hr, _steps_still(13, 17), workouts=[(_t(12, 30), _t(17, 30))]
    )
    assert payload["status"] == "insufficient_data"


def test_night_hours_are_excluded_as_likely_sleep():
    # Elevated quiet HR at 3am must not produce a daytime hint.
    hr = [(dt.datetime.combine(DAY, dt.time(3, m), tzinfo=TZ), 80.0) for m in range(0, 59, 2)]
    steps = [(dt.datetime.combine(DAY, dt.time(3, m), tzinfo=TZ), 0.0) for m in range(0, 59, 5)]
    payload = _build(hr, steps)
    assert payload["status"] == "insufficient_data"


def test_thin_rhr_baseline_refuses():
    hr = _hr_flat(13, 17, 75.0)
    payload = _build(hr, _steps_still(13, 17), rhr=RHR_14D[:3])
    assert payload["status"] == "insufficient_data"
    assert payload["reason"] == "resting_hr_baseline_too_thin"


def test_truncated_series_refuses():
    payload = _build(_hr_flat(13, 17, 60.0), _steps_still(13, 17), truncated=True)
    assert payload["status"] == "insufficient_data"
    assert payload["reason"] == "hr_or_steps_timeseries_truncated"


def test_no_hr_refuses():
    payload = _build([], _steps_still(13, 17))
    assert payload["status"] == "insufficient_data"


def test_confidence_capped_at_medium_and_low_when_sparse():
    # Coverage 13-17h quiet out of 7-23h waking ≈ 0.25 → low
    hr = _hr_flat(13, 17, 60.0)
    payload = _build(hr, _steps_still(13, 17))
    assert payload["status"] == "ok"
    assert payload["confidence"] == "low"
    # Broad quiet coverage (8-22h) → medium, never high
    hr_all = _hr_flat(8, 22, 60.0)
    payload2 = _build(hr_all, _steps_still(8, 22))
    assert payload2["confidence"] == "medium"


def test_step_gaps_do_not_invent_stillness():
    # Only two step samples 4h apart: bucket coverage is bounded to 15min each,
    # so quiet coverage stays below the floor.
    hr = _hr_flat(13, 17, 60.0)
    steps = [(_t(13), 0.0), (_t(17), 0.0)]
    payload = _build(hr, steps)
    assert payload["status"] == "insufficient_data"


# --- adversarial cases from independent review -------------------------------


def test_interleaved_dips_drag_mean_below_threshold():
    # 72bpm highs interleaved with 55bpm dips: old logic discarded dips and
    # fabricated a "sustained 72" hint; the mean over ALL samples (61.5 < 70)
    # must kill it.
    hr = []
    for m in range(0, 26, 2):
        hr.append((_t(14, m), 72.0 if (m // 2) % 2 == 0 else 55.0))
    hr += _hr_flat(13, 14, 60.0) + _hr_flat(15, 17, 60.0)
    payload = _build(sorted(hr), _steps_still(13, 17))
    assert payload["status"] == "ok"
    assert payload["intervals"] == []


def test_sparse_evening_samples_cannot_claim_full_coverage():
    # Five HR samples in 22:47–22:55 only: observation spans are bounded, so
    # coverage over the 16h waking window must be tiny, not 1.0.
    hr = [(_t(22, m), 60.0) for m in (47, 49, 51, 53, 55)]
    payload = _build(hr, _steps_still(7, 23))
    assert payload["status"] == "insufficient_data"
    assert payload["reason"] == "quiet_window_coverage_too_low"


def test_workout_starting_between_buckets_still_masks():
    # Still pair at 10:00/10:05 with a workout starting 10:01: the workout
    # span must be subtracted from the window, not sampled at bucket times.
    steps = [(_t(10, 0), 0.0), (_t(10, 5), 0.0), (_t(10, 10), 0.0), (_t(10, 15), 0.0)]
    hr = [(_t(10, m), 90.0) for m in range(0, 16, 2)]
    payload = _build(hr, steps, workouts=[(_t(10, 1), _t(11, 0))])
    assert payload["status"] == "insufficient_data"


def test_single_isolated_still_sample_proves_nothing():
    from healthmes.mcp_server.arousal import quiet_windows

    windows = quiet_windows([(_t(13), 0.0)], [], DAY, TZ)
    assert windows == []


def test_hints_never_bridge_across_quiet_windows():
    # Movement at 13:20 splits stillness into two windows; elevated HR across
    # the whole stretch must yield per-window hints, never one spanning 13:20.
    steps = _steps_still(13, 17)
    steps = [(at, 200.0 if at == _t(13, 20) else v) for at, v in steps]
    hr = _hr_flat(13, 17, 75.0)
    payload = _build(hr, steps)
    assert payload["status"] == "ok"
    for hint in payload["intervals"]:
        crosses = hint["start"] < _t(13, 20).isoformat() < hint["end"]
        assert not crosses


def test_separated_elevated_runs_never_fuse():
    # Two 12-min elevated runs (85bpm) separated by 20 min of normal HR:
    # verification round reproduced one false 40+min "sustained" hint because
    # the chain-wide mean stayed above threshold. Runs must stay separate.
    hr = _hr_flat(13, 17, 60.0)
    hr += [(_t(14, m), 85.0) for m in range(0, 13, 2)]
    hr += [(_t(14, 32 + m), 85.0) for m in range(0, 13, 2)]
    payload = _build(sorted(hr), _steps_still(13, 17))
    assert payload["status"] == "ok"
    assert len(payload["intervals"]) == 2
    for hint in payload["intervals"]:
        assert hint["minutes"] <= 15


def test_legitimate_run_not_suppressed_by_later_normal_stretch():
    # Inverse of fusing: one clean 12-min run must survive even though the
    # rest of the afternoon is normal (chain-wide mean would be below
    # threshold if the whole chain were judged together).
    hr = _hr_flat(13, 17, 60.0) + [(_t(15, m), 85.0) for m in range(0, 13, 2)]
    payload = _build(sorted(hr), _steps_still(13, 17))
    assert payload["status"] == "ok"
    assert len(payload["intervals"]) == 1
    assert payload["intervals"][0]["hr_peak"] == 85.0
