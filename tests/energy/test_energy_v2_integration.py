"""v2 factor integration: engine end-to-end, OW reader fetches, forecast API.

Covers the Phase-6 wiring seams that the pure unit vectors cannot:

- ``CognitiveEnergyEngine`` consuming a v2-extended ``OwRows`` produces all
  twelve components summing exactly to the score (and persists them under the
  unchanged components-payload schema);
- a **legacy v1-shaped** ``OwRows`` keeps the estimate byte-identical to the
  v1 engine — no v2 components, no v2 missing entries (the frozen contract of
  the pre-v2 tests);
- ``OwEnergyReader`` fetches the v2 timeseries + menstrual-cycle rows with the
  documented windows and **degrades them individually** (a backend without
  those routes only skips the v2 factors, never the whole read);
- ``OWClient.collect_menstrual_cycles`` follows cursor pagination;
- ``GET /cognitive-energy/forecast`` surfaces the new components verbatim.

No network, Docker, or credentials anywhere (httpx.MockTransport + sqlite).
"""

import datetime as dt

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from freezegun import freeze_time

from healthmes.api import energy
from healthmes.engine.cognitive_energy import (
    STATUS_OK,
    OwEnergyReader,
    OwRows,
)

UTC = dt.UTC
AS_OF = dt.date(2026, 7, 9)

V2_COMPONENT_NAMES = [
    "menstrual_phase_adjustment",
    "sunlight_bonus",
    "noise_penalty",
    "alcohol_penalty",
    "hydration_penalty",
]
ALL_COMPONENT_NAMES = [
    "base",
    "sleep_debt_penalty",
    "stress_penalty",
    "hrv_deviation_penalty",
    "body_battery_bonus",
    "meeting_load_penalty",
    "fragmentation_penalty",
    *V2_COMPONENT_NAMES,
]


def _series_row(
    timestamp: str, series_type: str, value: float, *, is_daily_total: bool = False
) -> dict:
    return {
        "timestamp": timestamp,
        "zone_offset": None,
        "type": series_type,
        "value": value,
        "unit": "any",
        "source": {"provider": "apple_health"},
        "is_daily_total": is_daily_total,
    }


@pytest.fixture
def v2_series_rows() -> list[dict]:
    """The v2 hand vector as vendor TimeSeriesSample rows.

    daylight 90/120 -> charge 0.75; noise mean 67.5 dB -> severity 0.5;
    alcohol 2 + 1 drinks in the evening window -> severity 0.75; hydration
    1500 vs 2000 baseline -> severity 0.5.
    """
    rows = [
        _series_row("2026-07-08T00:00:00+00:00", "time_in_daylight", 90.0, is_daily_total=True),
        _series_row("2026-07-09T10:00:00+00:00", "environmental_audio_exposure", 65.0),
        _series_row("2026-07-09T11:00:00+00:00", "environmental_audio_exposure", 70.0),
        _series_row("2026-07-08T20:00:00+00:00", "number_of_alcoholic_beverages", 2.0),
        _series_row("2026-07-09T00:30:00+00:00", "number_of_alcoholic_beverages", 1.0),
        _series_row("2026-07-08T12:00:00+00:00", "hydration", 1500.0),
    ]
    rows += [
        _series_row(
            f"{(dt.date(2026, 6, 24) + dt.timedelta(days=i)).isoformat()}T12:00:00+00:00",
            "hydration",
            2000.0,
        )
        for i in range(14)
    ]
    return rows


@pytest.fixture
def v2_cycle_rows() -> list[dict]:
    """One cycle record: day 20 of 28 on 2026-07-09 -> derived luteal (0.35)."""
    return [
        {
            "id": "0b7a4c58-0000-0000-0000-000000000001",
            "start_time": "2026-06-20T00:00:00Z",
            "end_time": "2026-07-18T00:00:00Z",
            "source": {"provider": "garmin"},
            "cycle_length": 28,
            "period_length": 5,
        }
    ]


@pytest.fixture
def full_v2_ow_rows(full_signal_ow_rows, v2_series_rows, v2_cycle_rows) -> OwRows:
    """The v1 vector rows extended with the fetched v2 rows."""
    return OwRows(
        full_signal_ow_rows.score_rows,
        full_signal_ow_rows.sleep_rows,
        series_rows=tuple(v2_series_rows),
        cycle_rows=tuple(v2_cycle_rows),
    )


# ---------------------------------------------------------------------------
# Engine end-to-end (fake reader, real digestion + composition + persist)
# ---------------------------------------------------------------------------


class TestEngineWithV2Rows:
    """Full-vector expectation (see test_cognitive_energy_v2 for the algebra):

    shares = base_weight / 1.25; bonus budget 12 -> base 88;
    88 - 4.8 - 6.4 - 6 + 6.4 - 5.4 - 4 - 1.68 + 3 - 1.6 - 3.6 - 1.6
    = 62.32 -> 62.
    """

    @freeze_time("2026-07-09 14:23:00")
    def test_all_twelve_components_and_the_hand_score(
        self, energy_engine_factory, full_v2_ow_rows, seed_vector_store
    ) -> None:
        seed_vector_store()
        engine = energy_engine_factory(full_v2_ow_rows)
        estimate = engine.compute_window()

        assert estimate.status == STATUS_OK
        assert [item["name"] for item in estimate.components] == ALL_COMPONENT_NAMES
        assert estimate.score_exact == pytest.approx(62.32)
        assert estimate.score == 62
        assert estimate.inputs_snapshot["missing_signals"] == []

        by_name = {item["name"]: item for item in estimate.components}
        assert by_name["menstrual_phase_adjustment"]["contribution"] == pytest.approx(-1.68)
        assert by_name["menstrual_phase_adjustment"]["raw"]["phase"] == "luteal"
        assert by_name["sunlight_bonus"]["contribution"] == pytest.approx(3.0)
        assert by_name["noise_penalty"]["contribution"] == pytest.approx(-1.6)
        assert by_name["alcohol_penalty"]["contribution"] == pytest.approx(-3.6)
        assert by_name["hydration_penalty"]["contribution"] == pytest.approx(-1.6)

        total = sum(item["contribution"] for item in estimate.components)
        assert total == pytest.approx(estimate.score_exact, abs=1e-9)

        v2_info = estimate.inputs_snapshot["ow"]["v2"]
        assert v2_info["series_rows"]["hydration_days"] == 15
        assert v2_info["cycle_rows"] == {"cycle_records": 1}

    @freeze_time("2026-07-09 14:23:00")
    def test_every_component_has_name_weight_raw_contribution(
        self, energy_engine_factory, full_v2_ow_rows, seed_vector_store
    ) -> None:
        seed_vector_store()
        estimate = energy_engine_factory(full_v2_ow_rows).compute_window()
        for item in estimate.components:
            assert set(item) >= {"name", "kind", "weight", "raw", "contribution"}
            assert isinstance(item["raw"], dict)

    @freeze_time("2026-07-09 14:23:00")
    def test_persisted_payload_carries_v2_items_under_schema_v1(
        self, energy_engine_factory, session_factory, full_v2_ow_rows, seed_vector_store
    ) -> None:
        from sqlalchemy import select

        from healthmes.store import CognitiveEnergyEstimate

        seed_vector_store()
        energy_engine_factory(full_v2_ow_rows).persist_current_window()

        with session_factory() as session:
            (row,) = session.scalars(select(CognitiveEnergyEstimate)).all()
        payload = row.components
        assert payload["version"] == 1  # item schema unchanged, only new names
        assert [item["name"] for item in payload["items"]] == ALL_COMPONENT_NAMES
        assert sum(i["contribution"] for i in payload["items"]) == pytest.approx(
            payload["score_exact"], abs=1e-9
        )
        assert row.score == 62

    @freeze_time("2026-07-09 14:23:00")
    def test_fetched_but_thin_v2_rows_drop_with_reasons(
        self, energy_engine_factory, full_signal_ow_rows, seed_vector_store
    ) -> None:
        """Empty (but fetched) v2 rows: every v2 factor drops with a reason
        and the score falls back to the exact v1 vector (weights renormalize
        over the v1 six, which sum to the 1.0 anchor)."""
        seed_vector_store()
        rows = OwRows(
            full_signal_ow_rows.score_rows,
            full_signal_ow_rows.sleep_rows,
            series_rows=(),
            cycle_rows=(),
        )
        estimate = energy_engine_factory(rows).compute_window()
        assert estimate.score_exact == pytest.approx(64.75)  # the v1 hand vector
        assert estimate.score == 65
        reasons = {
            item["name"]: item["reason"]
            for item in estimate.inputs_snapshot["missing_signals"]
        }
        assert reasons == {
            "menstrual_phase": "no_cycle_data",
            "sunlight": "no_recent_daylight_data",
            "noise": "no_recent_noise_data",
            "alcohol": "no_alcohol_logs_in_lookback",
            "hydration": "no_data_on_or_before_as_of",
        }


class TestLegacyV1RowsStayByteIdentical:
    """The frozen backward-compat contract: a v1-shaped OwRows (no v2 fields)
    must produce exactly the v1 component list, an empty missing list and the
    v1 score — the v2 factors are skipped, not reported."""

    @freeze_time("2026-07-09 14:23:00")
    def test_v1_rows_produce_the_v1_estimate(
        self, energy_engine_factory, full_signal_ow_rows, seed_vector_store
    ) -> None:
        seed_vector_store()
        estimate = energy_engine_factory(full_signal_ow_rows).compute_window()
        assert [item["name"] for item in estimate.components] == [
            "base",
            "sleep_debt_penalty",
            "stress_penalty",
            "hrv_deviation_penalty",
            "body_battery_bonus",
            "meeting_load_penalty",
            "fragmentation_penalty",
        ]
        assert estimate.score == 65
        assert estimate.inputs_snapshot["missing_signals"] == []
        v2_info = estimate.inputs_snapshot["ow"]["v2"]
        assert v2_info == {"series_rows": "not_fetched", "cycle_rows": "not_fetched"}


# ---------------------------------------------------------------------------
# OwEnergyReader: v2 fetches and per-fetch degradation (MockTransport)
# ---------------------------------------------------------------------------


def _make_ow_client(handler) -> object:
    from healthmes.mcp_server.ow_client import OWClient

    return OWClient(
        base_url="http://open-wearables.test",
        api_key="test-ow-api-key",
        transport=httpx.MockTransport(handler),
    )


def _page(data: list[dict]) -> dict:
    return {"data": data, "pagination": {"next_cursor": None, "has_more": False}}


def _run(awaitable):
    import asyncio

    return asyncio.run(awaitable)


class TestOwEnergyReaderV2:
    def test_fetches_series_and_cycles_with_documented_windows(
        self, settings, v2_series_rows, v2_cycle_rows
    ) -> None:
        timeseries_calls: list[dict] = []
        cycle_calls: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            params = request.url.params
            if path == "/api/v1/users":
                return httpx.Response(
                    200, json={"items": [{"id": "user-1"}], "total": 1, "page": 1, "limit": 2}
                )
            if path.endswith("/health-scores") or path.endswith("/summaries/sleep"):
                return httpx.Response(200, json=_page([]))
            if path.endswith("/timeseries"):
                timeseries_calls.append(
                    {
                        "types": params.get_list("types"),
                        "start": params["start_time"],
                        "end": params["end_time"],
                    }
                )
                return httpx.Response(200, json=_page(list(v2_series_rows)))
            if path.endswith("/events/menstrual-cycles"):
                cycle_calls.append(
                    {"start": params["start_date"], "end": params["end_date"]}
                )
                return httpx.Response(200, json=_page(list(v2_cycle_rows)))
            return httpx.Response(404, json={"detail": "not found"})

        reader = OwEnergyReader(settings, client=_make_ow_client(handler))
        rows = _run(reader.read(AS_OF))

        assert rows.status == "ok"
        assert rows.series_rows is not None and len(rows.series_rows) > 0
        assert rows.cycle_rows == tuple(v2_cycle_rows)

        # Two timeseries calls: short fresh-signal window + long baseline window.
        assert timeseries_calls[0]["types"] == [
            "time_in_daylight",
            "environmental_audio_exposure",
        ]
        assert timeseries_calls[0]["start"] == "2026-07-07"  # as_of - 2
        assert timeseries_calls[1]["types"] == [
            "hydration",
            "number_of_alcoholic_beverages",
        ]
        assert timeseries_calls[1]["start"] == "2026-06-23"  # as_of - 16
        assert all(call["end"] == "2026-07-10" for call in timeseries_calls)
        assert cycle_calls == [{"start": "2026-05-10", "end": "2026-07-10"}]  # as_of - 60

    def test_backend_without_v2_routes_degrades_only_the_v2_factors(
        self, settings, make_score_row
    ) -> None:
        """404 on the v2 endpoints (older backend): the read stays OK with the
        v1 rows; the v2 fields are honestly None ("not fetched")."""
        score_row = make_score_row("stress", "garmin", "2026-07-09T10:00:00+00:00", 40)

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/api/v1/users":
                return httpx.Response(
                    200, json={"items": [{"id": "user-1"}], "total": 1, "page": 1, "limit": 2}
                )
            if path.endswith("/health-scores"):
                return httpx.Response(200, json=_page([score_row]))
            if path.endswith("/summaries/sleep"):
                return httpx.Response(200, json=_page([]))
            return httpx.Response(404, json={"detail": "not found"})

        reader = OwEnergyReader(settings, client=_make_ow_client(handler))
        rows = _run(reader.read(AS_OF))

        assert rows.status == "ok"
        assert rows.score_rows == (score_row,)
        assert rows.series_rows is None
        assert rows.cycle_rows is None

    def test_v1_failure_still_degrades_the_whole_read(self, settings) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "kaboom"})

        reader = OwEnergyReader(settings, client=_make_ow_client(handler))
        rows = _run(reader.read(AS_OF))
        assert rows.status == "unavailable"
        assert rows.series_rows is None
        assert rows.cycle_rows is None


class TestCollectMenstrualCycles:
    def test_follows_cursor_pagination(self) -> None:
        pages: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/events/menstrual-cycles")
            cursor = request.url.params.get("cursor")
            pages.append(cursor)
            if cursor is None:
                return httpx.Response(
                    200,
                    json={
                        "data": [{"id": "c1", "start_time": "2026-06-20T00:00:00Z"}],
                        "pagination": {"next_cursor": "page-2", "has_more": True},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "c2", "start_time": "2026-05-20T00:00:00Z"}],
                    "pagination": {"next_cursor": None, "has_more": False},
                },
            )

        client = _make_ow_client(handler)
        rows = _run(
            client.collect_menstrual_cycles("user-1", "2026-05-10", "2026-07-10")
        )
        assert [row["id"] for row in rows] == ["c1", "c2"]
        assert pages == [None, "page-2"]


# ---------------------------------------------------------------------------
# Forecast API surfaces the new components
# ---------------------------------------------------------------------------


class TestForecastSurfacesV2Components:
    @pytest.fixture
    def client(
        self,
        settings,
        energy_engine_factory,
        full_v2_ow_rows,
        seed_vector_store,
        vector_now,
    ) -> TestClient:
        seed_vector_store()
        app = FastAPI()
        app.state.settings = settings
        app.state.energy_engine = energy_engine_factory(full_v2_ow_rows, now=vector_now)
        app.include_router(energy.router)
        return TestClient(app)

    def test_current_window_carries_all_v2_components(self, client: TestClient) -> None:
        body = client.get(
            "/cognitive-energy/forecast", params={"date": "2026-07-09"}
        ).json()
        window = body["windows"][14]  # 14:00-15:00 UTC, the vector window
        assert window["status"] == "ok"
        assert window["score"] == 62
        assert window["score_exact"] == pytest.approx(62.32)
        names = [item["name"] for item in window["components"]]
        assert names == ALL_COMPONENT_NAMES
        by_name = {item["name"]: item for item in window["components"]}
        assert by_name["menstrual_phase_adjustment"]["raw"]["phase"] == "luteal"
        assert by_name["sunlight_bonus"]["contribution"] == pytest.approx(3.0)

    def test_every_ok_window_still_sums_to_its_score(self, client: TestClient) -> None:
        body = client.get(
            "/cognitive-energy/forecast", params={"date": "2026-07-09"}
        ).json()
        ok_windows = [w for w in body["windows"] if w["status"] == "ok"]
        assert ok_windows
        for window in ok_windows:
            total = sum(item["contribution"] for item in window["components"])
            assert total == pytest.approx(window["score_exact"], abs=1e-9)
            assert window["score"] == round(window["score_exact"])
        # v2 components appear in every scored window (they are day-scoped).
        for window in ok_windows:
            names = {item["name"] for item in window["components"]}
            assert set(V2_COMPONENT_NAMES) <= names
