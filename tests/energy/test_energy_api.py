"""GET /cognitive-energy/forecast tests (docs/PLAN.md §3 + Phase-2 verification).

The plan-required invariant is asserted for every window: the component
contributions sum to the score. The app under test is a minimal FastAPI with
the energy router mounted and an injected engine (fake OW reader + in-memory
sqlite store + fixed clock) — no network, no lifespan side effects.
"""

import datetime as dt

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from healthmes.api import energy
from healthmes.engine.cognitive_energy import OwRows

UTC = dt.UTC


@pytest.fixture
def client_factory(settings):
    """Build a TestClient over a minimal app with an injected energy engine."""

    def make(engine_obj) -> TestClient:
        app = FastAPI()
        app.state.settings = settings
        app.state.energy_engine = engine_obj
        app.include_router(energy.router)
        return TestClient(app)

    return make


@pytest.fixture
def vector_client(
    client_factory, energy_engine_factory, full_signal_ow_rows, seed_vector_store, vector_now
):
    """Client + engine over the hand vector (now frozen at 14:23 UTC)."""
    seed_vector_store()
    engine = energy_engine_factory(full_signal_ow_rows, now=vector_now)
    return client_factory(engine), engine


class TestForecastShape:
    def test_returns_24_hourly_windows(self, vector_client) -> None:
        client, _ = vector_client
        response = client.get("/cognitive-energy/forecast", params={"date": "2026-07-09"})
        assert response.status_code == 200
        body = response.json()
        assert body["date"] == "2026-07-09"
        assert body["status"] == "ok"
        assert body["baseline_window_days"] == 14
        assert len(body["windows"]) == 24
        starts = [window["window_start"] for window in body["windows"]]
        assert starts[0].startswith("2026-07-09T00:00:00")
        assert starts[14].startswith("2026-07-09T14:00:00")
        assert starts == sorted(starts)

    def test_v1_alias_serves_the_same_forecast(self, vector_client) -> None:
        client, _ = vector_client
        primary = client.get("/cognitive-energy/forecast", params={"date": "2026-07-09"})
        alias = client.get("/v1/cognitive-energy/forecast", params={"date": "2026-07-09"})
        assert alias.status_code == 200
        assert alias.json() == primary.json()

    def test_invalid_date_is_422(self, vector_client) -> None:
        client, _ = vector_client
        response = client.get("/cognitive-energy/forecast", params={"date": "not-a-date"})
        assert response.status_code == 422

    def test_default_date_is_today(self, client_factory, energy_engine_factory) -> None:
        client = client_factory(energy_engine_factory(OwRows()))
        response = client.get("/cognitive-energy/forecast")
        assert response.status_code == 200
        assert response.json()["date"] == dt.datetime.now(UTC).date().isoformat()


class TestComponentsSumToScore:
    """The plan's Phase-2 verification: components of the response sum to the score."""

    def test_every_ok_window_components_sum_to_score(self, vector_client) -> None:
        client, _ = vector_client
        body = client.get(
            "/cognitive-energy/forecast", params={"date": "2026-07-09"}
        ).json()
        ok_windows = [w for w in body["windows"] if w["status"] == "ok"]
        assert ok_windows, "vector day must produce scored windows"
        for window in ok_windows:
            total = sum(item["contribution"] for item in window["components"])
            assert total == pytest.approx(window["score_exact"], abs=1e-9)
            assert abs(window["score"] - total) <= 0.5
            assert window["score"] == round(window["score_exact"])


class TestForecastContent:
    def test_current_window_matches_the_hand_vector(self, vector_client) -> None:
        client, _ = vector_client
        body = client.get(
            "/cognitive-energy/forecast", params={"date": "2026-07-09"}
        ).json()
        window = body["windows"][14]  # 14:00-15:00 UTC
        assert window["source"] == "computed"
        assert window["status"] == "ok"
        assert window["score"] == 65
        assert window["score_exact"] == pytest.approx(64.75)
        assert [item["name"] for item in window["components"]] == [
            "base",
            "sleep_debt_penalty",
            "stress_penalty",
            "hrv_deviation_penalty",
            "body_battery_bonus",
            "meeting_load_penalty",
            "fragmentation_penalty",
        ]

    def test_future_windows_drop_fragmentation_and_renormalize(self, vector_client) -> None:
        client, _ = vector_client
        body = client.get(
            "/cognitive-energy/forecast", params={"date": "2026-07-09"}
        ).json()
        window = body["windows"][16]  # 16:00 > now (14:23) -> behavior unknowable
        names = [item["name"] for item in window["components"]]
        assert "fragmentation_penalty" not in names
        base = next(item for item in window["components"] if item["name"] == "base")
        assert base["raw"]["renormalized"] is True
        assert {"name": "fragmentation", "reason": "window_in_future"} in base["raw"][
            "factors_missing"
        ]
        # Renormalized shares: 0.30/0.9, 0.20/0.9, 0.15/0.9, 0.10/0.9, 0.15/0.9.
        weights = {item["name"]: item["weight"] for item in window["components"]}
        assert weights["sleep_debt_penalty"] == pytest.approx(0.30 / 0.9)
        assert weights["body_battery_bonus"] == pytest.approx(0.10 / 0.9)
        # 88.889 - 6.667 - 8.889 - 8.333 + 8.889 - 0 = 73.889 -> 74
        assert window["score"] == 74

    def test_persisted_window_is_served_verbatim(self, vector_client) -> None:
        client, engine = vector_client
        persisted = engine.persist_current_window()  # writes the 14:00 row
        assert persisted.status == "ok"

        body = client.get(
            "/cognitive-energy/forecast", params={"date": "2026-07-09"}
        ).json()
        window = body["windows"][14]
        assert window["source"] == "persisted"
        assert window["score"] == 65
        assert window["score_exact"] == pytest.approx(64.75)
        total = sum(item["contribution"] for item in window["components"])
        assert total == pytest.approx(window["score_exact"], abs=1e-9)
        # All other windows stay on-demand.
        assert {w["source"] for i, w in enumerate(body["windows"]) if i != 14} == {"computed"}

    def test_day_without_any_signal_is_insufficient(
        self, client_factory, energy_engine_factory, vector_now
    ) -> None:
        client = client_factory(energy_engine_factory(OwRows(), now=vector_now))
        body = client.get(
            "/cognitive-energy/forecast", params={"date": "2026-07-09"}
        ).json()
        assert body["status"] == "insufficient_data"
        assert all(window["status"] == "insufficient_data" for window in body["windows"])
        assert all(window["score"] is None for window in body["windows"])
        assert all(window["components"] == [] for window in body["windows"])
