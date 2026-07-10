"""Hand-computed unit tests for the pure compare-impact delta logic.

The local-day dedupe tests pin a fixed non-UTC timezone (KST, UTC+9): two
occurrences on the same UTC date can be different *local* days, and that
distinction is the whole point of the tool's timezone rule.
"""

import datetime as dt

from healthmes.mcp_server import impact

KST = dt.timezone(dt.timedelta(hours=9))


def occurrence(
    iso_utc: str, source: str = "food_log", label: str = "x"
) -> impact.Occurrence:
    at = dt.datetime.fromisoformat(iso_utc).replace(tzinfo=dt.UTC)
    return impact.Occurrence(source, label, at, at)


class TestMatches:
    def test_case_insensitive_substring(self):
        assert impact.matches("wine", "Red WINE with dinner")
        assert impact.matches("WINE", "wine tasting")
        assert not impact.matches("wine", "green tea")
        assert not impact.matches("wine", None)
        assert not impact.matches("wine", "")


class TestDedupeByLocalDay:
    def test_same_utc_date_can_be_two_local_days(self):
        early = occurrence("2026-07-03T14:00:00", label="a")  # 23:00 KST on 07-03
        late = occurrence("2026-07-03T16:00:00", label="b")  # 01:00 KST on 07-04
        by_day = impact.dedupe_by_local_day([late, early], KST)
        assert set(by_day) == {dt.date(2026, 7, 3), dt.date(2026, 7, 4)}
        assert by_day[dt.date(2026, 7, 3)].label == "a"
        assert by_day[dt.date(2026, 7, 4)].label == "b"
        # The same two instants collapse to one day in UTC.
        assert set(impact.dedupe_by_local_day([late, early], dt.UTC)) == {
            dt.date(2026, 7, 3)
        }

    def test_earliest_occurrence_wins_within_a_day(self):
        first = occurrence("2026-07-03T03:00:00", label="first")  # 12:00 KST
        second = occurrence("2026-07-03T04:00:00", label="second")  # 13:00 KST
        by_day = impact.dedupe_by_local_day([second, first], KST)
        assert by_day[dt.date(2026, 7, 3)].label == "first"


class TestNightlyDeltas:
    def test_after_minus_before_and_honest_skips(self):
        by_day = {
            dt.date(2026, 7, 3): occurrence("2026-07-03T12:00:00", label="wine"),
            dt.date(2026, 7, 5): occurrence("2026-07-05T12:00:00", label="wine"),
            dt.date(2026, 7, 6): occurrence("2026-07-06T12:00:00", label="wine"),
        }
        daily = {
            dt.date(2026, 7, 3): 80.0,
            dt.date(2026, 7, 4): 70.0,
            dt.date(2026, 7, 5): 75.0,
            # 07-06 missing -> both the 07-05 after-night and the 07-06
            # before-night are unavailable.
            dt.date(2026, 7, 7): 55.0,
        }
        rows, skipped = impact.nightly_deltas(by_day, daily)
        assert skipped == 2
        assert len(rows) == 1
        assert rows[0]["occurred_on"] == "2026-07-03"
        assert rows[0]["before"] == 80.0
        assert rows[0]["after"] == 70.0
        assert rows[0]["delta"] == -10.0


class TestWindowMean:
    SAMPLES = [
        (dt.datetime(2026, 7, 8, 1, 30, tzinfo=dt.UTC), 30.0),
        (dt.datetime(2026, 7, 8, 2, 0, tzinfo=dt.UTC), 32.0),
        (dt.datetime(2026, 7, 8, 2, 30, tzinfo=dt.UTC), 34.0),
        (dt.datetime(2026, 7, 8, 3, 0, tzinfo=dt.UTC), 99.0),  # == end, excluded
    ]

    def test_half_open_window_and_mean(self):
        result = impact.window_mean(
            self.SAMPLES,
            dt.datetime(2026, 7, 8, 1, 0, tzinfo=dt.UTC),
            dt.datetime(2026, 7, 8, 3, 0, tzinfo=dt.UTC),
        )
        assert result == (32.0, 3)

    def test_min_samples_gate(self):
        result = impact.window_mean(
            self.SAMPLES,
            dt.datetime(2026, 7, 8, 1, 0, tzinfo=dt.UTC),
            dt.datetime(2026, 7, 8, 3, 0, tzinfo=dt.UTC),
            min_samples=4,
        )
        assert result is None


class TestSummaries:
    def test_summarize_deltas_hand_case(self):
        stats = impact.summarize_deltas([-10.0, -15.0, -5.0])
        assert stats["n"] == 3
        assert stats["mean_delta"] == -10.0
        assert stats["stdev_delta"] == 5.0
        assert stats["min_delta"] == -15.0
        assert stats["max_delta"] == -5.0

    def test_degenerate_sizes(self):
        assert impact.summarize_deltas([])["n"] == 0
        assert impact.summarize_deltas([])["mean_delta"] is None
        one = impact.summarize_deltas([4.0])
        assert one["n"] == 1
        assert one["mean_delta"] == 4.0
        assert one["stdev_delta"] is None

    def test_confidence_buckets(self):
        assert impact.confidence_from_n(2) == "low"
        assert impact.confidence_from_n(4) == "low"
        assert impact.confidence_from_n(5) == "medium"
        assert impact.confidence_from_n(9) == "medium"
        assert impact.confidence_from_n(10) == "high"
