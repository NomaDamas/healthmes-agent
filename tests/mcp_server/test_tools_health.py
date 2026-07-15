"""Tests for the open-wearables-backed MCP tools (fake REST via MockTransport).

The readiness/baseline expectations are hand-computed — see
test_interpret.py for the arithmetic (z = -1.41, sleep-debt index 20.0).
"""

import datetime as dt

import pytest
from fastmcp.exceptions import ToolError

from healthmes.mcp_server import server as server_module

AS_OF = "2026-07-08"
D = dt.date(2026, 7, 8)


def _seed_readiness_fixture(fake_ow) -> None:
    """A small but realistic OW dataset around 2026-07-08."""
    # Internal 4-factor sleep scores (recorded at wake time).
    fake_ow.add_score(
        "sleep",
        "internal",
        "2026-07-08T07:10:00Z",
        70,
        components={"duration": {"value": 60}, "stages": {"value": 75}},
    )
    fake_ow.add_score("sleep", "internal", "2026-07-07T07:05:00Z", 80)
    fake_ow.add_score("sleep", "internal", "2026-07-06T06:55:00Z", 90)
    # Internal resilience: value = raw HRV-CV, 0-100 score in components.
    fake_ow.add_score(
        "resilience",
        "internal",
        "2026-07-08T00:00:00Z",
        0.12,
        components={
            "resilience_score": {"value": 65},
            "days_counted": {"value": 12},
            "metric_type": {"qualifier": "RMSSD"},
        },
    )
    # Garmin body battery for the charge block.
    fake_ow.add_score("body_battery", "garmin", "2026-07-08T06:30:00Z", 55)
    # Nightly HRV (RMSSD) in the sleep summaries: current 40 vs [40,50,60,50,50].
    for day, value in [
        ("2026-07-08", 40.0),
        ("2026-07-07", 40.0),
        ("2026-07-06", 50.0),
        ("2026-07-05", 60.0),
        ("2026-07-04", 50.0),
        ("2026-07-03", 50.0),
    ]:
        fake_ow.add_sleep_summary(day, avg_hrv_rmssd_ms=value, duration_minutes=420)
    # One workout yesterday.
    fake_ow.add_workout(
        "2026-07-07T18:00:00Z",
        end_time="2026-07-07T19:00:00Z",
        duration_seconds=3600,
        calories_kcal=500.0,
        avg_heart_rate_bpm=150,
        type="running",
    )


class TestGetHealthScores:
    async def test_groups_and_interprets_by_category_provider(
        self, mcp_client, mcp_env, call_tool
    ):
        # Three in-window days so the Defect-4 min-days-with-data gate is met
        # (values chosen so mean/min/max are unchanged from the 2-day fixture).
        mcp_env.add_score("stress", "garmin", "2026-07-06T12:00:00Z", 40, qualifier="low")
        mcp_env.add_score("stress", "garmin", "2026-07-07T12:00:00Z", 30, qualifier="calm")
        mcp_env.add_score("stress", "garmin", "2026-07-08T12:00:00Z", 50, qualifier="medium")
        # Out-of-window row that must not leak into the stats.
        mcp_env.add_score("stress", "garmin", "2026-06-20T12:00:00Z", 99)
        mcp_env.add_score(
            "resilience",
            "internal",
            "2026-07-08T00:00:00Z",
            0.12,
            components={"resilience_score": {"value": 65}},
        )
        result = await call_tool(
            mcp_client, "get_health_scores", {"range": "7d", "end_date": AS_OF}
        )
        assert result["status"] == "ok"
        assert result["window"] == {
            "start_date": "2026-07-02",
            "end_date": "2026-07-08",
            "days": 7,
        }

        stress = result["scores"]["stress:garmin"]
        assert stress["latest"] == {"date": "2026-07-08", "value": 50.0, "qualifier": "medium"}
        assert stress["mean"] == 40.0
        assert stress["min"] == 30.0
        assert stress["max"] == 50.0
        assert stress["n_samples"] == 3
        assert stress["days_with_data"] == 3
        assert stress["coverage"] == 0.43  # 3/7
        assert stress["confidence"] == "low"

        # Internal resilience is normalized to the 0-100 score, raw CV kept.
        resilience = result["scores"]["resilience:internal"]
        assert resilience["value_kind"] == "resilience_score_0_100"
        assert resilience["latest"]["value"] == 65.0
        assert resilience["latest"]["hrv_cv"] == 0.12

    async def test_insufficient_data_when_no_rows(self, mcp_client, call_tool):
        result = await call_tool(
            mcp_client, "get_health_scores", {"range": "7d", "end_date": AS_OF}
        )
        assert result["status"] == "insufficient_data"
        assert result["scores"] == {}

    async def test_rejects_unknown_category_and_bad_range(self, mcp_client):
        with pytest.raises(ToolError, match="Unknown categories"):
            await mcp_client.call_tool(
                "get_health_scores", {"categories": ["mood"], "end_date": AS_OF}
            )
        with pytest.raises(ToolError, match="range"):
            await mcp_client.call_tool("get_health_scores", {"range": "7 days"})


class TestDailyReadinessContext:
    async def test_full_context_hand_computed(self, mcp_client, mcp_env, call_tool):
        _seed_readiness_fixture(mcp_env)
        result = await call_tool(mcp_client, "get_daily_readiness_context", {"date": AS_OF})

        assert result["status"] == "ok"
        assert result["date"] == AS_OF
        assert result["baseline_window_days"] == 14

        sleep = result["sleep_debt"]
        assert sleep["status"] == "ok"
        assert sleep["index"] == 20.0  # mean of debts (30, 20, 10)
        assert sleep["nights_counted"] == 3
        assert sleep["last_night"] == {"date": AS_OF, "score": 70.0}
        assert sleep["source"] == "internal_sleep_score"

        hrv = result["hrv"]
        assert hrv["status"] == "ok"
        assert hrv["variant"] == "rmssd"
        assert hrv["current"] == {"date": AS_OF, "value": 40.0}
        assert hrv["baseline_median"] == 50.0
        assert hrv["z_score"] == -1.41  # hand-computed
        assert hrv["delta_pct"] == -20.0
        assert hrv["n_days"] == 5

        stress = result["stress"]
        assert stress["source"] == "internal_resilience_proxy"  # no Garmin stress
        assert stress["value"] == 35.0  # 100 - 65
        assert stress["observed_on"] == AS_OF

        charge = result["charge"]
        assert charge["status"] == "ok"
        assert charge["confidence"] == "high"
        assert charge["entries"] == [
            {
                "category": "body_battery",
                "provider": "garmin",
                "value": 55.0,
                "qualifier": None,
                "observed_on": AS_OF,
            }
        ]

        load = result["yesterday_load"]
        assert load["date"] == "2026-07-07"
        assert load["workouts"] == 1
        assert load["total_minutes"] == 60.0
        assert load["total_calories_kcal"] == 500.0
        assert load["max_avg_heart_rate_bpm"] == 150
        assert load["types"] == ["running"]

        # Weakest confirmed block (sleep debt / hrv are low-coverage) wins.
        assert result["confidence"] == "low"

    async def test_native_garmin_stress_wins_over_proxy(self, mcp_client, mcp_env, call_tool):
        _seed_readiness_fixture(mcp_env)
        mcp_env.add_score("stress", "garmin", "2026-07-08T12:00:00Z", 62)
        result = await call_tool(mcp_client, "get_daily_readiness_context", {"date": AS_OF})
        assert result["stress"]["source"] == "garmin_stress"
        assert result["stress"]["value"] == 62.0

    async def test_insufficient_data_is_reported_honestly(self, mcp_client, call_tool):
        result = await call_tool(mcp_client, "get_daily_readiness_context", {"date": AS_OF})
        assert result["status"] == "insufficient_data"
        for block in ("sleep_debt", "hrv", "stress", "charge"):
            assert result[block]["status"] == "insufficient_data", block
        assert result["confidence"] == "low"
        # A day with no workouts is still a valid (rest-day) observation.
        assert result["yesterday_load"]["workouts"] == 0


class TestPersonalBaselines:
    async def test_baselines_with_hand_computed_values(self, mcp_client, mcp_env, call_tool):
        _seed_readiness_fixture(mcp_env)
        result = await call_tool(
            mcp_client,
            "get_personal_baselines",
            {"metrics": ["hrv_rmssd_ms", "sleep_score", "stress"], "as_of": AS_OF},
        )
        assert result["status"] == "ok"

        hrv = result["metrics"]["hrv_rmssd_ms"]
        assert hrv["status"] == "ok"
        assert hrv["unit"] == "ms"
        assert hrv["baseline_median"] == 50.0
        assert hrv["z_score"] == -1.41
        assert hrv["baseline_90d"] == {"median": 50.0, "n_days": 5, "window_days": 90}

        # Only 2 nights of history before the current sleep score -> honest no.
        sleep_score = result["metrics"]["sleep_score"]
        assert sleep_score["status"] == "insufficient_data"
        assert sleep_score["n_days"] == 2
        assert sleep_score["source"] == "internal_sleep_score"

        # No Garmin stress: proxy series exists only for one day.
        stress = result["metrics"]["stress"]
        assert stress["status"] == "insufficient_data"
        assert stress["source"] == "internal_resilience_proxy(100-resilience_score)"
        assert stress["current"]["value"] == 35.0

    async def test_unknown_metric_is_rejected(self, mcp_client):
        with pytest.raises(ToolError, match="Unknown metrics"):
            await mcp_client.call_tool("get_personal_baselines", {"metrics": ["vibes"]})

    async def test_all_empty_is_insufficient(self, mcp_client, call_tool):
        result = await call_tool(mcp_client, "get_personal_baselines", {"as_of": AS_OF})
        assert result["status"] == "insufficient_data"
        for entry in result["metrics"].values():
            assert entry["status"] == "insufficient_data"


class TestUserResolution:
    async def test_auto_discovers_single_user_when_not_configured(
        self, mcp_client, mcp_env, call_tool
    ):
        server_module.set_ow_user_id(None)  # drop the pinned id from the fixture
        result = await call_tool(
            mcp_client, "get_health_scores", {"range": "7d", "end_date": AS_OF}
        )
        assert result["status"] == "insufficient_data"  # no data, but resolved + queried
        assert any(req.url.path == "/api/v1/users" for req in mcp_env.requests)

    async def test_ambiguous_users_raise_clear_error(self, mcp_client, mcp_env):
        server_module.set_ow_user_id(None)
        mcp_env.users.append({"id": "11111111-2222-3333-4444-555555555555"})
        with pytest.raises(ToolError, match="HEALTHMES_OW_USER_ID"):
            await mcp_client.call_tool("get_health_scores", {"end_date": AS_OF})

    async def test_ow_failure_surfaces_as_tool_error(self, mcp_client, mcp_env):
        mcp_env.users.clear()  # /users still works; scores path will 401
        server_module.set_ow_client(
            server_module.OWClient(
                base_url="http://open-wearables.test",
                api_key="wrong-key",
                transport=mcp_env.transport(),
            )
        )
        with pytest.raises(ToolError, match="open-wearables API error"):
            await mcp_client.call_tool("get_health_scores", {"end_date": AS_OF})


class TestReviewFindingFixes:
    """Regression tests for the independent-review fixes on the health tools
    (Defects 1, 4, 5, 6, 7, 8). Env is pinned to KST (UTC+9)."""

    async def test_stress_grouped_by_local_day_not_utc(self, mcp_client, mcp_env, call_tool):
        # Defect 1: 2026-07-14T22:00Z is 2026-07-15 07:00 in KST, so it must
        # anchor to local day 07-15 (not UTC day 07-14). Before the fix it was
        # grouped by UTC and reported as a day-stale reading on 07-14.
        mcp_env.add_score("stress", "garmin", "2026-07-14T22:00:00Z", 62)
        result = await call_tool(
            mcp_client, "get_daily_readiness_context", {"date": "2026-07-15"}
        )
        stress = result["stress"]
        assert stress["source"] == "garmin_stress"
        assert stress["value"] == 62.0
        assert stress["observed_on"] == "2026-07-15"  # local day, not 07-14
        assert stress["stale_days"] == 0

    async def test_health_scores_sparse_window_is_insufficient(
        self, mcp_client, mcp_env, call_tool
    ):
        # Defect 4: two days of data in a 14d window is not a confident aggregate,
        # but the partial per-group data is still returned honestly.
        mcp_env.add_score("stress", "garmin", "2026-07-07T12:00:00Z", 30)
        mcp_env.add_score("stress", "garmin", "2026-07-08T12:00:00Z", 50)
        result = await call_tool(
            mcp_client, "get_health_scores", {"range": "14d", "end_date": AS_OF}
        )
        assert result["status"] == "insufficient_data"  # best group has 2 < 3 days
        assert result["truncated"] is False
        assert result["scores"]["stress:garmin"]["days_with_data"] == 2  # data still shown

    async def test_health_scores_truncation_is_insufficient(
        self, mcp_client, mcp_env, call_tool
    ):
        # Defect 8: a truncated offset window must not be presented as "ok".
        for day in (28, 29, 30):
            mcp_env.add_score("stress", "garmin", f"2026-06-{day:02d}T12:00:00Z", 40)
        for day in range(1, 9):  # 11 distinct in-window days in the 14d window
            mcp_env.add_score("stress", "garmin", f"2026-07-{day:02d}T12:00:00Z", 40)
        mcp_env.max_page_size = 1  # 10-page cap hit -> truncated
        result = await call_tool(
            mcp_client, "get_health_scores", {"range": "14d", "end_date": AS_OF}
        )
        assert result["truncated"] is True
        assert result["status"] == "insufficient_data"

    async def test_hrv_stale_current_is_insufficient(self, mcp_client, mcp_env, call_tool):
        # Defect 5: the latest nocturnal HRV is 7 days before as_of -> not current.
        for day in ("2026-06-26", "2026-06-27", "2026-06-28", "2026-06-29",
                    "2026-06-30", "2026-07-01"):
            mcp_env.add_sleep_summary(day, avg_hrv_rmssd_ms=50.0)
        result = await call_tool(mcp_client, "get_daily_readiness_context", {"date": AS_OF})
        hrv = result["hrv"]
        assert hrv["status"] == "insufficient_data"
        assert "stale" in hrv["reason"]
        assert hrv["stale_days"] == 7

    async def test_sleep_debt_requires_internal_score(self, mcp_client, mcp_env, call_tool):
        # Defect 7: provider (oura) sleep scores must NOT be used for sleep debt.
        for day in ("2026-07-06", "2026-07-07", "2026-07-08"):
            mcp_env.add_score("sleep", "oura", f"{day}T07:00:00+09:00", 70)
        result = await call_tool(mcp_client, "get_daily_readiness_context", {"date": AS_OF})
        sleep = result["sleep_debt"]
        assert sleep["status"] == "insufficient_data"
        assert sleep["reason"] == "no_internal_sleep_score"
        assert "index" not in sleep

    async def test_stale_garmin_stress_yields_to_fresh_proxy_baseline(
        self, mcp_client, mcp_env, call_tool
    ):
        # Defect 6: a ~60-day-old Garmin stress must not suppress a fresh proxy
        # (before the fix `if not daily` used the stale Garmin as "current").
        mcp_env.add_score("stress", "garmin", "2026-05-09T12:00:00Z", 80)
        mcp_env.add_score(
            "resilience", "internal", "2026-07-08T00:00:00Z", 0.12,
            components={"resilience_score": {"value": 65}},
        )
        result = await call_tool(
            mcp_client, "get_personal_baselines", {"metrics": ["stress"], "as_of": AS_OF}
        )
        stress = result["metrics"]["stress"]
        assert stress["source"] == "internal_resilience_proxy(100-resilience_score)"
        assert stress["current"]["date"] == "2026-07-08"  # fresh proxy day, not 05-09
        assert stress["current"]["value"] == 35.0  # 100 - 65

