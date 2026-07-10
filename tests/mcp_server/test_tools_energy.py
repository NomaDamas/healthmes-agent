"""Tests for the get_cognitive_energy_forecast MCP tool.

The tool serves the user's **local** day (the env is pinned to KST, UTC+9),
so a local day spans two UTC days: the fake engine below records which UTC
days were requested and the test hand-computes which windows must survive
the local-day filter. A separate test runs the *real* engine wiring
(store session factory + MockTransport-backed reader, no network) against a
seeded persisted row.
"""

import datetime as dt

from healthmes.engine.cognitive_energy import WindowSlot
from healthmes.mcp_server import server as server_module
from healthmes.store import CognitiveEnergyEstimate

KST = dt.timezone(dt.timedelta(hours=9))

OK_COMPONENTS = (
    {"name": "base", "kind": "base", "weight": None, "raw": {}, "contribution": 90.0},
    {
        "name": "sleep_debt_penalty",
        "kind": "penalty",
        "weight": 0.6,
        "raw": {"index": 20.0},
        "contribution": -12.0,
    },
    {
        "name": "stress_penalty",
        "kind": "penalty",
        "weight": 0.4,
        "raw": {"value": 40.0},
        "contribution": -8.0,
    },
)


def make_slot(day: dt.date, hour: int, score: int | None, status: str = "ok") -> WindowSlot:
    start = dt.datetime(day.year, day.month, day.day, hour, tzinfo=dt.UTC)
    return WindowSlot(
        window_start=start,
        window_end=start + dt.timedelta(hours=1),
        source="persisted",
        status=status,
        score=score,
        score_exact=float(score) if score is not None else None,
        components=OK_COMPONENTS if status == "ok" else (),
    )


class FakeEngine:
    def __init__(self, slots_by_day: dict[dt.date, list[WindowSlot]]) -> None:
        self.slots_by_day = slots_by_day
        self.calls: list[dt.date] = []

    def forecast_day(self, day: dt.date) -> list[WindowSlot]:
        self.calls.append(day)
        return self.slots_by_day.get(day, [])


class TestForecastLocalDayJoin:
    async def test_hand_computed_local_day_spans_two_utc_days(
        self, mcp_client, call_tool
    ):
        """Local 2026-07-08 (KST) = UTC [07-07 15:00, 07-08 15:00)."""
        day1, day2 = dt.date(2026, 7, 7), dt.date(2026, 7, 8)
        engine = FakeEngine(
            {
                day1: [make_slot(day1, hour, 30 + hour) for hour in range(24)],
                day2: [make_slot(day2, hour, 50 + hour) for hour in range(24)],
            }
        )
        server_module.set_energy_engine(engine)

        result = await call_tool(
            mcp_client, "get_cognitive_energy_forecast", {"date": "2026-07-08"}
        )
        assert engine.calls == [day1, day2]
        assert result["status"] == "ok"
        assert result["date"] == "2026-07-08"
        assert result["timezone"] == "UTC+09:00"

        windows = result["windows"]
        assert len(windows) == 24
        # First local hour is UTC 07-07 15:00 (score 30 + 15 = 45).
        assert windows[0]["start"] == "2026-07-08T00:00:00+09:00"
        assert windows[0]["end"] == "2026-07-08T01:00:00+09:00"
        assert windows[0]["score"] == 45
        # Last local hour is UTC 07-08 14:00 (score 50 + 14 = 64).
        assert windows[-1]["start"] == "2026-07-08T23:00:00+09:00"
        assert windows[-1]["end"] == "2026-07-09T00:00:00+09:00"
        assert windows[-1]["score"] == 64
        assert [w["start"] for w in windows] == sorted(w["start"] for w in windows)
        # UTC hours before the local day never leak in (07-07 scores 30..44).
        assert min(w["score"] for w in windows) == 45

        # Components are compact (no raw evidence dump) and preserved in order.
        assert windows[0]["components"] == [
            {"name": "base", "kind": "base", "weight": None, "contribution": 90.0},
            {
                "name": "sleep_debt_penalty",
                "kind": "penalty",
                "weight": 0.6,
                "contribution": -12.0,
            },
            {
                "name": "stress_penalty",
                "kind": "penalty",
                "weight": 0.4,
                "contribution": -8.0,
            },
        ]

        summary = result["summary"]
        assert summary["windows_total"] == 24
        assert summary["windows_ok"] == 24
        assert summary["best"] == {"start": "2026-07-08T23:00:00+09:00", "score": 64}
        assert summary["worst"] == {"start": "2026-07-08T00:00:00+09:00", "score": 45}
        assert summary["health_factors_present"] == [
            "sleep_debt_penalty",
            "stress_penalty",
        ]
        # Full window coverage (high) but only 2 OW factors (medium) -> medium.
        assert result["confidence_detail"] == {
            "window_coverage": "high",
            "health_factor_richness": "medium",
        }
        assert result["confidence"] == "medium"

    async def test_insufficient_when_no_window_has_signals(self, mcp_client, call_tool):
        day1, day2 = dt.date(2026, 7, 7), dt.date(2026, 7, 8)
        engine = FakeEngine(
            {
                day: [make_slot(day, hour, None, status="insufficient_data") for hour in range(24)]
                for day in (day1, day2)
            }
        )
        server_module.set_energy_engine(engine)
        result = await call_tool(
            mcp_client, "get_cognitive_energy_forecast", {"date": "2026-07-08"}
        )
        assert result["status"] == "insufficient_data"
        assert result["confidence"] == "low"
        assert result["summary"]["windows_ok"] == 0
        assert result["summary"]["best"] is None
        assert all(w["score"] is None for w in result["windows"])
        assert len(result["windows"]) == 24


class TestForecastDefaultWiring:
    async def test_persisted_row_is_served_through_the_real_engine(
        self, mcp_client, call_tool, store_factory, pinned_tz
    ):
        """No engine override: the tool builds the engine from runtime state
        (injected store factory + MockTransport ow client) and consumes the
        persisted cognitive_energy_estimate row; hours without data stay
        honest insufficient_data."""
        today_local = dt.datetime.now(pinned_tz).date()
        window_start = dt.datetime.combine(
            today_local, dt.time(hour=12), tzinfo=pinned_tz
        ).astimezone(dt.UTC)
        with store_factory() as session:
            session.add(
                CognitiveEnergyEstimate(
                    window_start=window_start,
                    window_end=window_start + dt.timedelta(hours=1),
                    score=77,
                    components={
                        "version": 1,
                        "items": [
                            {
                                "name": "base",
                                "kind": "base",
                                "weight": None,
                                "raw": {},
                                "contribution": 77.0,
                            }
                        ],
                        "score_exact": 77.0,
                    },
                    inputs_snapshot={"engine": "cognitive_energy_v1"},
                )
            )
            session.commit()

        result = await call_tool(mcp_client, "get_cognitive_energy_forecast", {})
        assert result["date"] == today_local.isoformat()
        assert result["status"] == "ok"  # the persisted window carries the day

        noon = f"{today_local.isoformat()}T12:00:00+09:00"
        by_start = {window["start"]: window for window in result["windows"]}
        assert noon in by_start
        assert by_start[noon]["score"] == 77
        assert by_start[noon]["source"] == "persisted"
        assert by_start[noon]["status"] == "ok"
        # Empty fake OW + inactive calendar + no usage: every other hour is
        # computed and honestly insufficient (never a fabricated score).
        others = [w for start, w in by_start.items() if start != noon]
        assert others and all(w["status"] == "insufficient_data" for w in others)
        assert all(w["source"] == "computed" for w in others)
