"""Hand-computed unit tests for the pure stress-timeline logic.

All datetimes use a fixed non-UTC timezone (KST, UTC+9) as the "local"
timezone — the module treats them opaquely, but non-UTC values catch any
accidental UTC assumptions in the arithmetic.
"""

import datetime as dt

from healthmes.mcp_server import timeline

KST = dt.timezone(dt.timedelta(hours=9))
DAY = dt.date(2026, 7, 8)


def at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 8, hour, minute, tzinfo=KST)


class TestStressLabel:
    def test_garmin_band_boundaries(self):
        assert timeline.stress_label(0) == "rest"
        assert timeline.stress_label(25) == "rest"
        assert timeline.stress_label(26) == "low"
        assert timeline.stress_label(50) == "low"
        assert timeline.stress_label(51) == "medium"
        assert timeline.stress_label(75) == "medium"
        assert timeline.stress_label(76) == "high"
        assert timeline.stress_label(100) == "high"


class TestBuildStressIntervals:
    def test_hand_computed_runs_with_15min_cadence(self):
        """Rest -> medium -> (gap) -> high, step inferred as the median 15 min."""
        samples = [
            (at(9, 0), 20.0),
            (at(9, 15), 22.0),
            (at(9, 30), 24.0),
            (at(9, 45), 20.0),
            (at(10, 0), 60.0),
            (at(10, 15), 65.0),
            (at(13, 0), 80.0),
            (at(13, 15), 90.0),
        ]
        intervals = timeline.build_stress_intervals(samples)
        assert len(intervals) == 3

        rest, medium, high = intervals
        assert (rest.start, rest.end, rest.label) == (at(9, 0), at(10, 0), "rest")
        assert rest.mean == 21.5
        assert rest.peak == 24.0
        assert rest.n_samples == 4
        assert rest.duration_minutes == 60.0

        assert (medium.start, medium.end, medium.label) == (at(10, 0), at(10, 30), "medium")
        assert medium.mean == 62.5
        assert medium.n_samples == 2

        assert (high.start, high.end, high.label) == (at(13, 0), at(13, 30), "high")
        assert high.mean == 85.0
        assert high.peak == 90.0

    def test_gap_splits_even_same_band_runs(self):
        samples = [
            (at(9, 0), 20.0),
            (at(9, 15), 20.0),
            (at(13, 0), 22.0),
            (at(13, 15), 24.0),
        ]
        intervals = timeline.build_stress_intervals(samples)
        assert [interval.label for interval in intervals] == ["rest", "rest"]
        assert intervals[0].end == at(9, 30)  # last sample + 15min step
        assert intervals[1].start == at(13, 0)

    def test_short_blip_absorbed_and_neighbors_merged(self):
        """A single 3-min 'high' sample inside a rest run does not split it."""
        samples = (
            [(at(10, minute), 20.0) for minute in (0, 3, 6, 9)]
            + [(at(10, 12), 90.0)]
            + [(at(10, minute), 20.0) for minute in (15, 18, 21)]
        )
        intervals = timeline.build_stress_intervals(samples)
        assert len(intervals) == 1
        merged = intervals[0]
        assert merged.label == "rest"
        assert (merged.start, merged.end) == (at(10, 0), at(10, 24))
        assert merged.n_samples == 8
        assert merged.mean == (20.0 * 7 + 90.0) / 8  # 28.75 — evidence preserved
        assert merged.peak == 90.0

    def test_short_first_run_folds_forward(self):
        samples = [(at(10, 0), 90.0)] + [
            (at(10, minute), 20.0) for minute in (3, 6, 9, 12, 15)
        ]
        intervals = timeline.build_stress_intervals(samples)
        assert len(intervals) == 1
        assert intervals[0].label == "rest"
        assert (intervals[0].start, intervals[0].end) == (at(10, 0), at(10, 18))
        assert intervals[0].n_samples == 6
        assert intervals[0].peak == 90.0

    def test_negative_unmeasurable_values_dropped_and_empty_ok(self):
        assert timeline.build_stress_intervals([]) == []
        assert timeline.build_stress_intervals([(at(9, 0), -1.0), (at(9, 15), -2.0)]) == []

    def test_single_sample_uses_fallback_step(self):
        intervals = timeline.build_stress_intervals([(at(9, 0), 30.0)])
        assert len(intervals) == 1
        assert intervals[0].end == at(9, 15)  # FALLBACK_STEP_MINUTES
        assert intervals[0].label == "low"


class TestProxySections:
    def test_waking_sections_carry_the_day_level_value(self):
        sections = timeline.proxy_sections(DAY, KST, 40.0)
        assert [(s.start, s.end) for s in sections] == [
            (at(6, 0), at(12, 0)),
            (at(12, 0), at(18, 0)),
            (at(18, 0), dt.datetime(2026, 7, 9, 0, 0, tzinfo=KST)),
        ]
        assert all(s.label == "low" for s in sections)
        assert all(s.mean == 40.0 for s in sections)
        assert all(s.n_samples == 0 for s in sections)


class TestAttachContext:
    def test_events_and_dominant_categories_hand_case(self):
        events = [
            (at(13, 15), at(13, 45), "Standup"),
            (at(11, 0), at(12, 0), "Lunch"),  # outside the window
        ]
        usage = [
            (at(13, 0), "social", 1200),  # 50% overlap -> 10 min
            (at(12, 0), "games", 3600),  # bucket ends at 13:00 -> 0 overlap
            (at(13, 0), "news", 240),  # 50% overlap -> 2 min < 5 min floor
        ]
        context = timeline.attach_context(at(13, 0), at(13, 30), events, usage)
        assert context == ["event: Standup", "apps: social (10 min)"]

    def test_untitled_events_and_uncategorized_apps(self):
        context = timeline.attach_context(
            at(9, 0),
            at(10, 0),
            [(at(9, 0), at(9, 30), None)],
            [(at(9, 0), None, 600)],
        )
        assert context == ["event: (untitled)", "apps: uncategorized (10 min)"]

    def test_event_cap(self):
        events = [(at(9, 0), at(10, 0), f"Meeting {i}") for i in range(6)]
        context = timeline.attach_context(at(9, 0), at(10, 0), events, [])
        assert len(context) == 4  # max_events

    def test_category_ranking_is_by_minutes_then_name(self):
        usage = [
            (at(9, 0), "social", 1200),  # 20 min
            (at(9, 0), "video", 1800),  # 30 min
            (at(9, 0), "news", 600),  # 10 min -> cut by max_categories=2
        ]
        context = timeline.attach_context(at(9, 0), at(10, 0), [], usage)
        assert context == ["apps: video (30 min)", "apps: social (20 min)"]


class TestCoverageAndConfidence:
    def test_day_coverage_and_buckets(self):
        intervals = timeline.build_stress_intervals(
            [(at(9, 0), 20.0), (at(9, 15), 20.0), (at(9, 30), 20.0)]
        )
        assert timeline.day_coverage(intervals) == round(45 / 1440, 2)
        assert timeline.timeline_confidence(0.6) == "high"
        assert timeline.timeline_confidence(0.59) == "medium"
        assert timeline.timeline_confidence(0.3) == "medium"
        assert timeline.timeline_confidence(0.29) == "low"

    def test_serialize_interval_shape(self):
        interval = timeline.build_stress_intervals([(at(9, 0), 30.0)])[0]
        payload = timeline.serialize_interval(interval, ["event: Standup"])
        assert payload == {
            "window": {
                "start": "2026-07-08T09:00:00+09:00",
                "end": "2026-07-08T09:15:00+09:00",
                "duration_minutes": 15.0,
            },
            "stress_level": "low",
            "stress_mean": 30.0,
            "stress_peak": 30.0,
            "n_samples": 1,
            "likely_context": ["event: Standup"],
        }
