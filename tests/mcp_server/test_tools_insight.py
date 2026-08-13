"""Tests for the get_stress_timeline and compare_impact MCP tools.

Everything is hand-computed against the pinned KST (UTC+9) timezone of the
mcp_env fixture: the stress samples, calendar events, and app-usage buckets
are seeded in UTC and every expectation is stated in local time — a UTC/local
mix-up anywhere in the join breaks these numbers.
"""

import datetime as dt
import uuid

import pytest
from fastmcp.exceptions import ToolError
from sqlalchemy import select

from healthmes.store import (
    CalendarEventMirror,
    CalendarSource,
    FoodLog,
    Task,
)

DAY = "2026-07-08"  # local (KST) test day = UTC [07-07 15:00, 07-08 15:00)


def seed_event(
    store_factory, summary: str, start_utc: dt.datetime, end_utc: dt.datetime
) -> None:
    with store_factory() as session:
        session.add(
            CalendarEventMirror(
                external_id=uuid.uuid4().hex,
                calendar_source=CalendarSource.GOOGLE,
                summary=summary,
                start_at=start_utc,
                end_at=end_utc,
            )
        )
        session.commit()


def seed_food(store_factory, description: str, logged_at_utc: dt.datetime) -> None:
    with store_factory() as session:
        session.add(FoodLog(logged_at=logged_at_utc, description=description))
        session.commit()


def seed_usage(store_factory, bucket_start_utc: dt.datetime, **fields) -> None:
    from healthmes.store import AppUsageSample

    with store_factory() as session:
        session.add(
            AppUsageSample(
                device_id=fields.pop("device_id", "pixel-1"),
                bucket_start=bucket_start_utc,
                app_package=fields.pop("app_package", "com.instagram.android"),
                foreground_seconds=fields.pop("foreground_seconds", 600),
                launches=fields.pop("launches", 3),
                category=fields.pop("category", "social"),
            )
        )
        session.commit()


class TestGetStressTimeline:
    async def test_hand_computed_timeseries_join(
        self, mcp_client, mcp_env, call_tool, store_factory
    ):
        """Garmin samples spanning local midnight + calendar/app context."""
        # 00:00 KST on the test day (= 15:00Z the previous UTC day).
        mcp_env.add_stress_sample("2026-07-07T15:00:00Z", 10)
        # 09:00-09:45 KST rest run.
        for minute, value in ((0, 20), (15, 22), (30, 24), (45, 20)):
            mcp_env.add_stress_sample(f"2026-07-08T00:{minute:02d}:00Z", value)
        # 10:00, 10:15 KST medium run.
        mcp_env.add_stress_sample("2026-07-08T01:00:00Z", 60)
        mcp_env.add_stress_sample("2026-07-08T01:15:00Z", 65)
        # 13:00, 13:15 KST high run (after a >30min gap).
        mcp_env.add_stress_sample("2026-07-08T04:00:00Z", 80)
        mcp_env.add_stress_sample("2026-07-08T04:15:00Z", 90)
        # Decoys outside the local day: 23:45 KST on 07-07 and 00:00 KST on 07-09.
        mcp_env.add_stress_sample("2026-07-07T14:45:00Z", 99)
        mcp_env.add_stress_sample("2026-07-08T15:00:00Z", 99)

        # Calendar: 10:00-10:30 KST -> overlaps only the medium interval.
        seed_event(
            store_factory,
            "Quarterly review",
            dt.datetime(2026, 7, 8, 1, 0, tzinfo=dt.UTC),
            dt.datetime(2026, 7, 8, 1, 30, tzinfo=dt.UTC),
        )
        # Usage: hourly bucket at 13:00 KST, 1200s social -> 50% overlap with
        # the 30-minute high interval = 10 attributed minutes.
        seed_usage(
            store_factory,
            dt.datetime(2026, 7, 8, 4, 0, tzinfo=dt.UTC),
            foreground_seconds=1200,
        )

        result = await call_tool(mcp_client, "get_stress_timeline", {"date": DAY})
        assert result["status"] == "ok"
        assert result["date"] == DAY
        assert result["timezone"] == "UTC+09:00"
        assert result["source"] == "garmin_stress_timeseries"
        assert result["day_level_stress"] is None
        assert result["coverage"] == round((15 + 60 + 30 + 30) / 1440, 2)
        assert result["confidence"] == "low"

        intervals = result["intervals"]
        assert [i["stress_level"] for i in intervals] == [
            "rest",
            "rest",
            "medium",
            "high",
        ]
        # The 15:00Z sample landed on local midnight — not on the next UTC day.
        assert intervals[0]["window"]["start"] == "2026-07-08T00:00:00+09:00"
        assert intervals[0]["n_samples"] == 1
        assert intervals[1]["window"] == {
            "start": "2026-07-08T09:00:00+09:00",
            "end": "2026-07-08T10:00:00+09:00",
            "duration_minutes": 60.0,
        }
        assert intervals[1]["stress_mean"] == 21.5
        assert intervals[1]["likely_context"] == []  # event starts at 10:00
        assert intervals[2]["likely_context"] == ["event: Quarterly review"]
        assert intervals[2]["stress_mean"] == 62.5
        assert intervals[3]["likely_context"] == ["apps: social (10 min)"]
        assert intervals[3]["stress_peak"] == 90.0
        # Decoy values never leak into any interval.
        assert all(i["stress_peak"] != 99.0 for i in intervals)

    async def test_night_hrv_proxy_sections_when_no_timeseries(
        self, mcp_client, mcp_env, call_tool, store_factory
    ):
        mcp_env.add_score(
            "resilience",
            "internal",
            "2026-07-08T01:00:00Z",  # 10:00 KST -> local day 07-08
            0.15,
            components={"resilience_score": {"value": 60}},
        )
        # 14:00-15:00 KST -> the afternoon section's context.
        seed_event(
            store_factory,
            "Board meeting",
            dt.datetime(2026, 7, 8, 5, 0, tzinfo=dt.UTC),
            dt.datetime(2026, 7, 8, 6, 0, tzinfo=dt.UTC),
        )

        result = await call_tool(mcp_client, "get_stress_timeline", {"date": DAY})
        assert result["status"] == "ok"
        assert result["source"] == "night_hrv_resilience_proxy"
        assert result["coverage"] is None
        assert result["confidence"] == "medium"
        assert result["day_level_stress"] == {
            "value": 40.0,  # 100 - resilience score 60
            "observed_on": "2026-07-08",
            "stale_days": 0,
        }

        intervals = result["intervals"]
        assert [i["window"]["start"] for i in intervals] == [
            "2026-07-08T06:00:00+09:00",
            "2026-07-08T12:00:00+09:00",
            "2026-07-08T18:00:00+09:00",
        ]
        assert all(i["stress_level"] == "low" for i in intervals)
        assert all(i["stress_mean"] == 40.0 for i in intervals)
        assert all(i["n_samples"] == 0 for i in intervals)
        assert intervals[0]["likely_context"] == []
        assert intervals[1]["likely_context"] == ["event: Board meeting"]

    async def test_insufficient_when_no_stress_signal_at_all(
        self, mcp_client, call_tool
    ):
        result = await call_tool(mcp_client, "get_stress_timeline", {"date": DAY})
        assert result["status"] == "insufficient_data"
        assert result["intervals"] == []
        assert result["confidence"] == "low"
        assert "reason" in result

    async def test_rejects_malformed_date(self, mcp_client):
        with pytest.raises(ToolError, match="date"):
            await mcp_client.call_tool("get_stress_timeline", {"date": "last tuesday"})


class TestCompareImpactNightly:
    def _seed_sleep_scores(self, mcp_env, values: dict[str, float]) -> None:
        """Internal sleep scores recorded at 07:00 local (KST) wake time."""
        for day, value in values.items():
            mcp_env.add_score("sleep", "internal", f"{day}T07:00:00+09:00", value)

    async def test_hand_computed_wine_vs_sleep_score(
        self, mcp_client, mcp_env, call_tool, store_factory
    ):
        seed_food(
            store_factory,
            "Red wine with dinner",
            dt.datetime(2026, 7, 3, 12, 0, tzinfo=dt.UTC),  # 21:00 KST 07-03
        )
        seed_food(  # same local day -> deduplicated, earliest kept
            store_factory, "More wine", dt.datetime(2026, 7, 3, 13, 0, tzinfo=dt.UTC)
        )
        seed_food(
            store_factory, "Wine tasting", dt.datetime(2026, 7, 5, 11, 30, tzinfo=dt.UTC)
        )
        seed_food(  # never matches
            store_factory, "Green tea", dt.datetime(2026, 7, 4, 12, 0, tzinfo=dt.UTC)
        )
        seed_event(
            store_factory,
            "Wine bar with Alex",
            dt.datetime(2026, 7, 6, 10, 0, tzinfo=dt.UTC),  # 19:00 KST 07-06
            dt.datetime(2026, 7, 6, 11, 0, tzinfo=dt.UTC),
        )
        self._seed_sleep_scores(
            mcp_env,
            {
                "2026-07-03": 80,
                "2026-07-04": 70,
                "2026-07-05": 75,
                "2026-07-06": 60,
                "2026-07-07": 55,
            },
        )
        # A provider score that must lose to the internal series.
        mcp_env.add_score("sleep", "oura", "2026-07-04T07:00:00+09:00", 1)

        result = await call_tool(
            mcp_client,
            "compare_impact",
            {"factor": "wine", "metric": "sleep_score", "window": "7d", "end_date": DAY},
        )
        assert result["status"] == "ok"
        assert result["window"] == {
            "start_date": "2026-07-02",
            "end_date": "2026-07-08",
            "days": 7,
            "timezone": "UTC+09:00",
        }
        assert result["metric"] == {
            "name": "sleep_score",
            "kind": "nightly",
            "unit": "score_0_100",
            "higher_is_better": True,
            "source": "internal_sleep_score",
        }
        assert result["occurrences"] == {
            "matched_by_source": {"food_log": 3, "calendar": 1, "task": 0, "workout": 0},
            "total_matched": 4,
            "used": 3,  # 3 distinct local days after dedupe
            "paired": 3,
            "skipped_no_metric": 0,
            "truncated": False,
        }
        # Deltas: 07-03: 70-80=-10, 07-05: 60-75=-15, 07-06: 55-60=-5.
        assert result["effect"] == {
            "n": 3,
            "mean_delta": -10.0,
            "stdev_delta": 5.0,
            "min_delta": -15.0,
            "max_delta": -5.0,
        }
        assert result["confidence"] == "low"
        assert result["note"] == "observational association, not causation"
        first = result["examples"][0]
        assert first["occurred_on"] == "2026-07-03"
        assert first["source"] == "food_log"
        assert first["label"] == "Red wine with dinner"
        assert (first["before"], first["after"], first["delta"]) == (80.0, 70.0, -10.0)

    async def test_local_day_join_not_utc_day(
        self, mcp_client, mcp_env, call_tool, store_factory
    ):
        """Occurrences after local midnight belong to the *next* local day.

        All three food logs are ~01:00 KST, i.e. still the previous UTC date;
        joining on UTC days would pair them with the decoy 0-score night and
        produce wildly different deltas.
        """
        for iso in (
            "2026-07-03T16:00:00",  # 01:00 KST 07-04
            "2026-07-05T16:30:00",  # 01:30 KST 07-06
            "2026-07-06T17:00:00",  # 02:00 KST 07-07
        ):
            seed_food(
                store_factory,
                "Soju night",
                dt.datetime.fromisoformat(iso).replace(tzinfo=dt.UTC),
            )
        self._seed_sleep_scores(
            mcp_env,
            {
                "2026-07-03": 0,  # UTC-day decoy: used only by a wrong join
                "2026-07-04": 70,
                "2026-07-05": 75,
                "2026-07-06": 60,
                "2026-07-07": 65,
                "2026-07-08": 70,
            },
        )
        result = await call_tool(
            mcp_client,
            "compare_impact",
            {"factor": "soju", "metric": "sleep_score", "window": "7d", "end_date": DAY},
        )
        assert result["status"] == "ok"
        # Local days 07-04/07-06/07-07: (75-70), (65-60), (70-65) = +5 each.
        assert result["effect"]["n"] == 3
        assert result["effect"]["mean_delta"] == 5.0
        assert result["effect"]["stdev_delta"] == 0.0
        assert [example["occurred_on"] for example in result["examples"]] == [
            "2026-07-04",
            "2026-07-06",
            "2026-07-07",
        ]

    async def test_insufficient_below_min_pairs_with_source_counts(
        self, mcp_client, mcp_env, call_tool, store_factory
    ):
        """Done tasks and workouts count as occurrences; too few pairs is honest."""
        with store_factory() as session:
            session.add(
                Task(
                    title="Morning run",
                    status="done",
                    updated_at=dt.datetime(2026, 7, 6, 9, 0, tzinfo=dt.UTC),
                )
            )
            session.add(  # not done -> never an occurrence
                Task(
                    title="Plan run route",
                    status="todo",
                    updated_at=dt.datetime(2026, 7, 6, 9, 0, tzinfo=dt.UTC),
                )
            )
            session.commit()
        mcp_env.add_workout(
            "2026-07-05T10:00:00Z", end_time="2026-07-05T11:00:00Z", type="running"
        )
        mcp_env.add_workout(  # type does not match
            "2026-07-04T10:00:00Z", end_time="2026-07-04T11:00:00Z", type="cycling"
        )

        result = await call_tool(
            mcp_client,
            "compare_impact",
            {"factor": "run", "metric": "sleep_score", "window": "7d", "end_date": DAY},
        )
        assert result["status"] == "insufficient_data"
        assert result["reason"] == "need_at_least_3_paired_observations"
        assert result["occurrences"]["matched_by_source"] == {
            "food_log": 0,
            "calendar": 0,
            "task": 1,
            "workout": 1,
        }
        assert result["occurrences"]["paired"] == 0
        assert result["occurrences"]["skipped_no_metric"] == 2
        assert result["confidence"] == "low"
        assert "effect" not in result

    async def test_hrv_metric_reports_variant(
        self, mcp_client, mcp_env, call_tool, store_factory
    ):
        for day in ("2026-07-03", "2026-07-05", "2026-07-06"):
            seed_event(
                store_factory,
                "Evening meditation",
                dt.datetime.fromisoformat(f"{day}T10:00:00").replace(tzinfo=dt.UTC),
                dt.datetime.fromisoformat(f"{day}T10:30:00").replace(tzinfo=dt.UTC),
            )
        for day, value in (
            ("2026-07-03", 40.0),
            ("2026-07-04", 46.0),
            ("2026-07-05", 50.0),
            ("2026-07-06", 44.0),
            ("2026-07-07", 50.0),
        ):
            mcp_env.add_sleep_summary(day, avg_hrv_rmssd_ms=value)

        result = await call_tool(
            mcp_client,
            "compare_impact",
            {"factor": "meditation", "metric": "hrv", "window": "7d", "end_date": DAY},
        )
        assert result["status"] == "ok"
        assert result["metric"]["variant"] == "rmssd"
        assert result["metric"]["source"] == "sleep_summaries.avg_hrv_rmssd_ms"
        # Deltas: +6 (07-03), -6 (07-05), +6 (07-06) -> mean 2.0.
        assert result["effect"]["n"] == 3
        assert result["effect"]["mean_delta"] == 2.0


class TestCompareImpactIntraday:
    async def test_hand_computed_stress_deltas_around_espresso(
        self, mcp_client, mcp_env, call_tool, store_factory
    ):
        """Mean stress 2h after minus 2h before each occurrence (3+ samples/side)."""
        occurrences = {
            "2026-07-06": ((40, 40, 40), (70, 70, 70)),  # +30
            "2026-07-07": ((50, 50, 50), (60, 60, 60)),  # +10
            "2026-07-08": ((30, 32, 34), (50, 52, 54)),  # 52 - 32 = +20
        }
        for day, (pre_values, post_values) in occurrences.items():
            occurred = dt.datetime.fromisoformat(f"{day}T03:00:00").replace(tzinfo=dt.UTC)
            seed_food(store_factory, "Double espresso", occurred)  # 12:00 KST
            for offset_min, value in zip((90, 60, 30), pre_values, strict=True):
                sample_at = occurred - dt.timedelta(minutes=offset_min)
                mcp_env.add_stress_sample(sample_at.isoformat(), value)
            for offset_min, value in zip((30, 60, 90), post_values, strict=True):
                sample_at = occurred + dt.timedelta(minutes=offset_min)
                mcp_env.add_stress_sample(sample_at.isoformat(), value)
        # A fourth occurrence with no stress samples at all -> skipped, not guessed.
        seed_food(
            store_factory,
            "Espresso shot",
            dt.datetime(2026, 7, 5, 3, 0, tzinfo=dt.UTC),
        )
        # Out-of-window decoy.
        seed_food(
            store_factory,
            "Espresso decoy",
            dt.datetime(2026, 6, 25, 3, 0, tzinfo=dt.UTC),
        )

        result = await call_tool(
            mcp_client,
            "compare_impact",
            {"factor": "espresso", "metric": "stress", "window": "7d", "end_date": DAY},
        )
        assert result["status"] == "ok"
        assert result["metric"]["kind"] == "intraday"
        assert result["metric"]["higher_is_better"] is False
        assert result["occurrences"]["total_matched"] == 4
        assert result["occurrences"]["used"] == 4
        assert result["occurrences"]["paired"] == 3
        assert result["occurrences"]["skipped_no_metric"] == 1
        assert result["effect"] == {
            "n": 3,
            "mean_delta": 20.0,
            "stdev_delta": 10.0,
            "min_delta": 10.0,
            "max_delta": 30.0,
        }
        first = result["examples"][0]
        assert first["occurred_on"] == "2026-07-06"
        assert first["occurred_at"] == "2026-07-06T12:00:00+09:00"
        assert (first["before"], first["after"], first["delta"]) == (40.0, 70.0, 30.0)


class TestCompareImpactValidation:
    async def test_rejects_unknown_metric(self, mcp_client):
        with pytest.raises(ToolError, match="metric must be one of"):
            await mcp_client.call_tool(
                "compare_impact", {"factor": "wine", "metric": "mood"}
            )

    async def test_rejects_too_short_factor(self, mcp_client):
        with pytest.raises(ToolError, match="factor"):
            await mcp_client.call_tool(
                "compare_impact", {"factor": " x ", "metric": "sleep_score"}
            )

    async def test_rejects_bad_window(self, mcp_client):
        with pytest.raises(ToolError, match="window"):
            await mcp_client.call_tool(
                "compare_impact",
                {"factor": "wine", "metric": "sleep_score", "window": "seven"},
            )


class TestStoreSideEffectsUntouched:
    async def test_compare_impact_never_writes(self, mcp_client, call_tool, store_factory):
        await call_tool(
            mcp_client,
            "compare_impact",
            {"factor": "wine", "metric": "sleep_score", "window": "7d", "end_date": DAY},
        )
        with store_factory() as session:
            assert session.scalars(select(FoodLog)).all() == []


class TestReviewFindingFixes:
    """Regression tests for the independent-review fixes on the insight tools
    (Defects 4, 8, 9). Env is pinned to KST (UTC+9); DAY is the local test day."""

    async def test_impact_dedupes_by_local_day_before_capping(
        self, mcp_client, mcp_env, call_tool, store_factory
    ):
        # Defect 9: 31 wine logs on ONE local day would monopolize the 30-cap and
        # starve the min-n gate; dedupe-by-local-day must run BEFORE the cap so
        # the 4 valid prior days survive. (Old code -> insufficient_data.)
        for minute in range(31):  # all on local day 07-06 (KST) -- the most recent
            seed_food(
                store_factory,
                f"Red wine {minute}",
                dt.datetime(2026, 7, 6, 3, minute, tzinfo=dt.UTC),  # 12:mm KST 07-06
            )
        for day in (1, 2, 3, 4):  # one wine log on each earlier local day
            seed_food(
                store_factory,
                "Red wine dinner",
                dt.datetime(2026, 7, day, 3, 0, tzinfo=dt.UTC),  # 12:00 KST 07-0d
            )
        for day in range(1, 8):  # internal sleep scores 07-01..07-07 so nights pair
            mcp_env.add_score("sleep", "internal", f"2026-07-{day:02d}T07:00:00+09:00", 60 + day)

        result = await call_tool(
            mcp_client,
            "compare_impact",
            {"factor": "wine", "metric": "sleep_score", "window": "30d", "end_date": DAY},
        )
        assert result["status"] == "ok"
        assert result["occurrences"]["total_matched"] == 35  # 31 + 4 raw matches
        assert result["occurrences"]["used"] == 5  # 5 distinct local days survive the cap
        assert result["occurrences"]["truncated"] is False  # 5 distinct days <= 30
        assert result["effect"]["n"] == 5  # every survivor pairs

    async def test_stress_timeline_truncation_is_insufficient(
        self, mcp_client, mcp_env, call_tool
    ):
        # Defect 8: a truncated intraday series must not be presented as a full
        # day. 21 in-window samples, page size 1, default 20-page cap -> truncated.
        for minute in range(21):
            mcp_env.add_stress_sample(f"2026-07-08T00:{minute:02d}:00Z", 20 + minute % 5)
        mcp_env.timeseries_page_size = 1
        result = await call_tool(mcp_client, "get_stress_timeline", {"date": DAY})
        assert result["truncated"] is True
        assert result["status"] == "insufficient_data"
        assert result["reason"] == "stress_timeseries_truncated"

    async def test_stress_timeline_single_sample_is_insufficient(
        self, mcp_client, mcp_env, call_tool
    ):
        # Defect 4: one sample is not a timeline (>= MIN_TIMELINE_SAMPLES required).
        mcp_env.add_stress_sample("2026-07-08T00:00:00Z", 42)  # 09:00 KST, in window
        result = await call_tool(mcp_client, "get_stress_timeline", {"date": DAY})
        assert result["status"] == "insufficient_data"
        assert result["reason"] == "insufficient_stress_samples"
        assert result["intervals"] == []

