"""v2 factor unit tests (docs/PLAN.md §1.5 "reserved as v2 factors", Phase 6).

Hand-computed vectors for every v2 factor builder (present and
absent-with-renormalization), the eleven-signal full vector, the
components-sum-to-score regression over the extended factor set, and the v2
row digestion. Everything here is pure — no store, no network, no clock.

Series names are the vendor vocabulary
(vendor/open-wearables/backend/app/schemas/enums/series_types.py):
time_in_daylight / environmental_audio_exposure /
number_of_alcoholic_beverages / hydration; the menstrual factor consumes
MenstrualCycleRecord rows (routes/v1/events.py::list_menstrual_cycles).
"""

import datetime as dt

import pytest

from healthmes.engine.cognitive_energy import (
    _V1_FACTOR_SPECS,
    _V2_FACTOR_SPECS,
    FACTOR_SPECS,
    STATUS_OK,
    FactorSignal,
    MissingSignal,
    SeriesPoint,
    alcohol_signal,
    compute_estimate,
    digest_ow_rows,
    hydration_signal,
    menstrual_phase_signal,
    noise_signal,
    sunlight_signal,
)

UTC = dt.UTC
AS_OF = dt.date(2026, 7, 9)
WS = dt.datetime(2026, 7, 9, 14, 0, tzinfo=UTC)
WE = dt.datetime(2026, 7, 9, 15, 0, tzinfo=UTC)


FACTOR_SPECS_BY_TERM = {spec.term: spec for spec in FACTOR_SPECS}


def _by_name(estimate) -> dict[str, dict]:
    return {item["name"]: item for item in estimate.components}


def _v1_vector_signals() -> list[FactorSignal]:
    """The v1 hand vector of test_cognitive_energy.py (unchanged severities)."""
    return [
        FactorSignal("sleep_debt", 0.2, {"index": 20.0}),
        FactorSignal("stress", 0.4, {"value": 40.0}),
        FactorSignal("hrv_deviation", 0.5, {"z_score": -1.25}),
        FactorSignal("body_battery", 0.8, {"normalized_value": 80.0}),
        FactorSignal("meeting_load", 0.45, {"booked_minutes": 30.0}),
        FactorSignal("fragmentation", 0.5, {"distracting_launches": 6}),
    ]


def _v2_vector_signals() -> list[FactorSignal]:
    """The v2 hand vector: severities/charge chosen for clean arithmetic."""
    return [
        FactorSignal("menstrual_phase", 0.35, {"phase": "luteal"}),
        FactorSignal("sunlight", 0.75, {"daylight_minutes": 90.0}),
        FactorSignal("noise", 0.5, {"mean_db": 67.5}),
        FactorSignal("alcohol", 0.75, {"drinks": 3.0}),
        FactorSignal("hydration", 0.5, {"deficit_fraction": 0.25}),
    ]


# ---------------------------------------------------------------------------
# Weight policy invariants
# ---------------------------------------------------------------------------


class TestWeightPolicy:
    def test_v1_anchor_still_sums_to_one(self) -> None:
        # Backward-compat anchor: estimates without any v2 signal keep the
        # original shares (weight / 1.0 == the declared base weight).
        assert sum(s.base_weight for s in _V1_FACTOR_SPECS) == pytest.approx(1.0)

    def test_v2_weights_are_small_and_documented(self) -> None:
        by_key = {s.key: s for s in _V2_FACTOR_SPECS}
        assert {s.key for s in _V2_FACTOR_SPECS} == {
            "menstrual_phase",
            "sunlight",
            "noise",
            "alcohol",
            "hydration",
        }
        assert by_key["menstrual_phase"].base_weight == pytest.approx(0.06)
        assert by_key["sunlight"].base_weight == pytest.approx(0.05)
        assert by_key["noise"].base_weight == pytest.approx(0.04)
        assert by_key["alcohol"].base_weight == pytest.approx(0.06)
        assert by_key["hydration"].base_weight == pytest.approx(0.04)
        # Adjunct context: combined, the v2 factors stay a small fraction.
        assert sum(s.base_weight for s in _V2_FACTOR_SPECS) == pytest.approx(0.25)
        assert all(s.base_weight <= 0.10 for s in _V2_FACTOR_SPECS)

    def test_terms_and_kinds(self) -> None:
        by_key = {s.key: s for s in FACTOR_SPECS}
        assert by_key["menstrual_phase"].term == "menstrual_phase_adjustment"
        assert by_key["sunlight"].term == "sunlight_bonus"
        assert by_key["noise"].term == "noise_penalty"
        assert by_key["alcohol"].term == "alcohol_penalty"
        assert by_key["hydration"].term == "hydration_penalty"
        assert by_key["sunlight"].kind == "bonus"
        for key in ("menstrual_phase", "noise", "alcohol", "hydration"):
            assert by_key[key].kind == "penalty"


# ---------------------------------------------------------------------------
# Score composition: the eleven-signal hand vector
# ---------------------------------------------------------------------------


class TestAllSignalsVector:
    """All eleven factors present.

    Total relative weight = 1.25, so shares are base/1.25:
    sleep 0.24, stress 0.16, hrv 0.12, battery 0.08, meeting 0.12, frag 0.08,
    menstrual 0.048, sunlight 0.04, noise 0.032, alcohol 0.048, hydration 0.032.
    Bonus budget = (0.08 + 0.04) * 100 = 12 -> base 88.
    score = 88 - 24*0.2 - 16*0.4 - 12*0.5 + 8*0.8 - 12*0.45 - 8*0.5
              - 4.8*0.35 + 4*0.75 - 3.2*0.5 - 4.8*0.75 - 3.2*0.5
          = 88 - 4.8 - 6.4 - 6 + 6.4 - 5.4 - 4 - 1.68 + 3 - 1.6 - 3.6 - 1.6
          = 62.32 -> 62
    """

    def _signals(self) -> list[FactorSignal]:
        return _v1_vector_signals() + _v2_vector_signals()

    def test_score(self) -> None:
        estimate = compute_estimate(WS, WE, self._signals())
        assert estimate.status == STATUS_OK
        assert estimate.score_exact == pytest.approx(62.32)
        assert estimate.score == 62

    def test_component_contributions(self) -> None:
        components = _by_name(compute_estimate(WS, WE, self._signals()))
        assert components["base"]["contribution"] == pytest.approx(88.0)
        assert components["sleep_debt_penalty"]["contribution"] == pytest.approx(-4.8)
        assert components["stress_penalty"]["contribution"] == pytest.approx(-6.4)
        assert components["hrv_deviation_penalty"]["contribution"] == pytest.approx(-6.0)
        assert components["body_battery_bonus"]["contribution"] == pytest.approx(6.4)
        assert components["meeting_load_penalty"]["contribution"] == pytest.approx(-5.4)
        assert components["fragmentation_penalty"]["contribution"] == pytest.approx(-4.0)
        assert components["menstrual_phase_adjustment"]["contribution"] == pytest.approx(-1.68)
        assert components["sunlight_bonus"]["contribution"] == pytest.approx(3.0)
        assert components["noise_penalty"]["contribution"] == pytest.approx(-1.6)
        assert components["alcohol_penalty"]["contribution"] == pytest.approx(-3.6)
        assert components["hydration_penalty"]["contribution"] == pytest.approx(-1.6)

    def test_component_order_and_required_fields(self) -> None:
        estimate = compute_estimate(WS, WE, self._signals())
        assert [item["name"] for item in estimate.components] == [
            "base",
            "sleep_debt_penalty",
            "stress_penalty",
            "hrv_deviation_penalty",
            "body_battery_bonus",
            "meeting_load_penalty",
            "fragmentation_penalty",
            "menstrual_phase_adjustment",
            "sunlight_bonus",
            "noise_penalty",
            "alcohol_penalty",
            "hydration_penalty",
        ]
        for item in estimate.components:
            assert set(item) >= {"name", "weight", "raw", "contribution"}
            assert isinstance(item["raw"], dict)

    def test_renormalized_shares(self) -> None:
        components = _by_name(compute_estimate(WS, WE, self._signals()))
        # Relative weights: shares = base_weight / 1.25.
        assert components["sleep_debt_penalty"]["weight"] == pytest.approx(0.24)
        assert components["menstrual_phase_adjustment"]["weight"] == pytest.approx(0.048)
        assert components["sunlight_bonus"]["weight"] == pytest.approx(0.04)
        assert components["noise_penalty"]["weight"] == pytest.approx(0.032)
        assert components["alcohol_penalty"]["weight"] == pytest.approx(0.048)
        assert components["hydration_penalty"]["weight"] == pytest.approx(0.032)
        assert components["base"]["raw"]["renormalized"] is True

    def test_v1_only_signals_stay_bit_identical_and_unflagged(self) -> None:
        # The frozen v1 regression: adding the v2 specs must not move a v1-only
        # estimate by a single bit (weights sum to the 1.0 anchor).
        estimate = compute_estimate(WS, WE, _v1_vector_signals())
        assert estimate.score_exact == pytest.approx(64.75)
        assert estimate.score == 65
        components = _by_name(estimate)
        assert components["sleep_debt_penalty"]["weight"] == pytest.approx(0.30)
        assert components["base"]["raw"]["renormalized"] is False


class TestPerFactorRenormalization:
    """Each v2 factor against the sleep anchor: present, then dropped.

    With {sleep_debt 0.30, factor w} present the factor share is w/(0.30+w);
    dropping the factor renormalizes sleep to the full 100-point budget.
    """

    ANCHOR = FactorSignal("sleep_debt", 0.2, {})

    @pytest.mark.parametrize(
        ("signal", "term", "expected_share", "expected_contribution"),
        [
            pytest.param(
                FactorSignal("menstrual_phase", 0.6, {}),
                "menstrual_phase_adjustment",
                0.06 / 0.36,  # = 1/6 -> 16.6667 max points
                -(0.06 / 0.36) * 100 * 0.6,  # = -10.0
                id="menstrual",
            ),
            pytest.param(
                FactorSignal("sunlight", 0.75, {}),
                "sunlight_bonus",
                0.05 / 0.35,  # = 1/7 -> 14.2857 bonus budget
                +(0.05 / 0.35) * 100 * 0.75,  # = +10.714...
                id="sunlight",
            ),
            pytest.param(
                FactorSignal("noise", 0.5, {}),
                "noise_penalty",
                0.04 / 0.34,
                -(0.04 / 0.34) * 100 * 0.5,
                id="noise",
            ),
            pytest.param(
                FactorSignal("alcohol", 0.75, {}),
                "alcohol_penalty",
                0.06 / 0.36,
                -(0.06 / 0.36) * 100 * 0.75,  # = -12.5
                id="alcohol",
            ),
            pytest.param(
                FactorSignal("hydration", 1.0, {}),
                "hydration_penalty",
                0.04 / 0.34,
                -(0.04 / 0.34) * 100 * 1.0,
                id="hydration",
            ),
        ],
    )
    def test_present_share_and_contribution(
        self, signal: FactorSignal, term: str, expected_share: float, expected_contribution: float
    ) -> None:
        estimate = compute_estimate(WS, WE, [self.ANCHOR, signal])
        components = _by_name(estimate)
        assert components[term]["weight"] == pytest.approx(expected_share)
        assert components[term]["contribution"] == pytest.approx(expected_contribution)
        assert components["sleep_debt_penalty"]["weight"] == pytest.approx(
            0.30 / (0.30 + FACTOR_SPECS_BY_TERM[term].base_weight)
        )
        assert components["base"]["raw"]["renormalized"] is True
        total = sum(item["contribution"] for item in estimate.components)
        assert total == pytest.approx(estimate.score_exact, abs=1e-9)

    @pytest.mark.parametrize(
        ("key", "term", "reason"),
        [
            ("menstrual_phase", "menstrual_phase_adjustment", "no_cycle_data"),
            ("sunlight", "sunlight_bonus", "no_recent_daylight_data"),
            ("noise", "noise_penalty", "no_recent_noise_data"),
            ("alcohol", "alcohol_penalty", "no_alcohol_logs_in_lookback"),
            ("hydration", "hydration_penalty", "no_recent_hydration_data"),
        ],
    )
    def test_absent_factor_renormalizes_to_anchor(
        self, key: str, term: str, reason: str
    ) -> None:
        estimate = compute_estimate(WS, WE, [self.ANCHOR], [MissingSignal(key, reason)])
        components = _by_name(estimate)
        assert term not in components
        # The lone anchor owns the whole budget: 100 - 100 * 0.2 = 80.
        assert components["sleep_debt_penalty"]["weight"] == pytest.approx(1.0)
        assert estimate.score_exact == pytest.approx(80.0)
        assert estimate.inputs_snapshot["missing_signals"] == [
            {"name": key, "reason": reason}
        ]
        assert components["base"]["raw"]["factors_missing"] == [
            {"name": key, "reason": reason}
        ]


class TestComponentsSumToScoreV2:
    """The plan invariant over the extended factor set."""

    @pytest.mark.parametrize(
        "signals",
        [
            pytest.param(_v1_vector_signals() + _v2_vector_signals(), id="all-eleven"),
            pytest.param(_v2_vector_signals(), id="v2-only"),
            pytest.param(
                [FactorSignal("sunlight", 0.75, {})], id="sunlight-bonus-only"
            ),
            pytest.param(
                _v1_vector_signals()
                + [s for s in _v2_vector_signals() if s.key != "menstrual_phase"],
                id="eleven-minus-menstrual",
            ),
            pytest.param(
                [
                    FactorSignal(
                        spec.key, 0.0 if spec.kind == "bonus" else 1.0, {}
                    )
                    for spec in FACTOR_SPECS
                ],
                id="worst-case-floor",
            ),
            pytest.param(
                [
                    FactorSignal(
                        spec.key, 1.0 if spec.kind == "bonus" else 0.0, {}
                    )
                    for spec in FACTOR_SPECS
                ],
                id="best-case-ceiling",
            ),
        ],
    )
    def test_components_sum_to_score(self, signals: list[FactorSignal]) -> None:
        estimate = compute_estimate(WS, WE, signals)
        total = sum(item["contribution"] for item in estimate.components)
        assert total == pytest.approx(estimate.score_exact, abs=1e-9)
        assert estimate.score == round(estimate.score_exact)
        assert 0 <= estimate.score <= 100

    def test_worst_is_zero_best_is_hundred_with_all_eleven(self) -> None:
        worst = compute_estimate(
            WS,
            WE,
            [
                FactorSignal(spec.key, 0.0 if spec.kind == "bonus" else 1.0, {})
                for spec in FACTOR_SPECS
            ],
        )
        best = compute_estimate(
            WS,
            WE,
            [
                FactorSignal(spec.key, 1.0 if spec.kind == "bonus" else 0.0, {})
                for spec in FACTOR_SPECS
            ],
        )
        assert worst.score_exact == pytest.approx(0.0, abs=1e-9)
        assert worst.score == 0
        assert best.score_exact == pytest.approx(100.0, abs=1e-9)
        assert best.score == 100


# ---------------------------------------------------------------------------
# Factor builders
# ---------------------------------------------------------------------------


def _cycle_row(start: str, **fields: object) -> dict:
    """A MenstrualCycleRecord-shaped row (routes/v1/events.py)."""
    return {"start_time": start, **fields}


class TestMenstrualPhaseSignal:
    def test_reported_phase_used_while_snapshot_current(self) -> None:
        # 2026-06-27 + 12 days -> day_in_cycle 13 == the reported snapshot.
        row = _cycle_row(
            "2026-06-27T00:00:00Z",
            day_in_cycle=13,
            current_phase_type="FOLLICULAR",
            cycle_length=28,
        )
        signal = menstrual_phase_signal([row], AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.value == pytest.approx(0.0)
        assert signal.raw["phase"] == "follicular"
        assert signal.raw["phase_source"] == "provider_reported"
        assert signal.raw["day_in_cycle"] == 13

    def test_reported_ovulation_beats_derived_luteal(self) -> None:
        # Day 15 of 28 derives luteal (> 28-14), but the fresh provider
        # snapshot says ovulation -> severity 0.
        row = _cycle_row(
            "2026-06-25T00:00:00Z",
            day_in_cycle=15,
            current_phase_type="OVULATION",
            cycle_length=28,
        )
        signal = menstrual_phase_signal([row], AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.raw["phase"] == "ovulation"
        assert signal.value == pytest.approx(0.0)

    def test_stale_snapshot_falls_back_to_geometry(self) -> None:
        # Ingested at day 5 ("MENSTRUAL") but now day 20 -> derived luteal.
        row = _cycle_row(
            "2026-06-20T00:00:00Z",
            day_in_cycle=5,
            current_phase_type="MENSTRUAL",
            cycle_length=28,
        )
        signal = menstrual_phase_signal([row], AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.raw["phase"] == "luteal"
        assert signal.raw["phase_source"] == "derived_from_cycle_geometry"
        assert signal.value == pytest.approx(0.35)

    def test_derived_menstrual_days(self) -> None:
        # Day 4 <= period_length 5 -> menstrual 0.6.
        row = _cycle_row("2026-07-06T00:00:00Z", cycle_length=28, period_length=5)
        signal = menstrual_phase_signal([row], AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.raw["phase"] == "menstrual"
        assert signal.value == pytest.approx(0.6)
        assert signal.raw["day_in_cycle"] == 4

    def test_derived_defaults_without_lengths(self) -> None:
        # Day 9 with textbook defaults (28/5) -> follicular 0.
        row = _cycle_row("2026-07-01T00:00:00Z")
        signal = menstrual_phase_signal([row], AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.raw["phase"] == "follicular"
        assert signal.value == pytest.approx(0.0)
        assert signal.raw["cycle_length_days"] == 28
        assert signal.raw["period_length_days"] == 5

    def test_predicted_cycle_length_fallback(self) -> None:
        # No cycle_length, predicted 30: day 20 > 30-14 -> luteal.
        row = _cycle_row("2026-06-20T00:00:00Z", predicted_cycle_length=30)
        signal = menstrual_phase_signal([row], AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.raw["phase"] == "luteal"
        assert signal.raw["cycle_length_days"] == 30

    def test_latest_started_cycle_wins(self) -> None:
        rows = [
            _cycle_row("2026-06-01T00:00:00Z", cycle_length=28),
            _cycle_row("2026-07-06T00:00:00Z", cycle_length=28, period_length=5),
        ]
        signal = menstrual_phase_signal(rows, AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.raw["cycle_start"] == "2026-07-06"
        assert signal.raw["phase"] == "menstrual"

    def test_stale_cycle_record_is_missing(self) -> None:
        # Day 70 of a 28-day cycle: the record no longer covers today.
        row = _cycle_row("2026-05-01T00:00:00Z", cycle_length=28)
        signal = menstrual_phase_signal([row], AS_OF)
        assert isinstance(signal, MissingSignal)
        assert signal.reason == "cycle_record_stale"

    def test_current_provider_snapshot_beats_the_overrun_stale_gate(self) -> None:
        # Period 4 days late (normal variance): 2026-06-08 + AS_OF -> day 32
        # of a predicted 28-day cycle, but the provider synced today — the
        # day_in_cycle snapshot matches the recomputed day, so the reported
        # LUTEAL is trusted per the module policy instead of being dropped
        # as cycle_record_stale.
        row = _cycle_row(
            "2026-06-08T00:00:00Z",
            day_in_cycle=32,
            current_phase_type="LUTEAL",
            predicted_cycle_length=28,
        )
        signal = menstrual_phase_signal([row], AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.raw["phase"] == "luteal"
        assert signal.raw["phase_source"] == "provider_reported"
        assert signal.value == pytest.approx(0.35)
        assert signal.raw["day_in_cycle"] == 32

    def test_overrun_with_outdated_snapshot_is_still_stale(self) -> None:
        # Same overrun day, but the phase snapshot was taken at day 20 —
        # geometry cannot classify a day beyond the cycle it describes.
        row = _cycle_row(
            "2026-06-08T00:00:00Z",
            day_in_cycle=20,
            current_phase_type="LUTEAL",
            predicted_cycle_length=28,
        )
        signal = menstrual_phase_signal([row], AS_OF)
        assert isinstance(signal, MissingSignal)
        assert signal.reason == "cycle_record_stale"

    def test_pregnancy_is_honestly_out_of_scope(self) -> None:
        by_snapshot = menstrual_phase_signal(
            [_cycle_row("2026-06-20T00:00:00Z", pregnancy_snapshot=[{"week": 8}])], AS_OF
        )
        by_phase = menstrual_phase_signal(
            [_cycle_row("2026-06-20T00:00:00Z", current_phase_type="PREGNANCY")], AS_OF
        )
        for signal in (by_snapshot, by_phase):
            assert isinstance(signal, MissingSignal)
            assert signal.reason == "pregnancy_not_modeled"

    def test_future_only_or_empty_is_missing(self) -> None:
        assert isinstance(menstrual_phase_signal([], AS_OF), MissingSignal)
        future = menstrual_phase_signal(
            [_cycle_row("2026-07-15T00:00:00Z")], AS_OF
        )
        assert isinstance(future, MissingSignal)
        assert future.reason == "no_cycle_data"


class TestSunlightSignal:
    def test_yesterday_vector(self) -> None:
        signal = sunlight_signal({dt.date(2026, 7, 8): 90.0}, AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.value == pytest.approx(0.75)  # 90 / 120
        assert signal.raw["observed_on"] == "2026-07-08"
        assert signal.raw["stale_days"] == 1

    def test_two_days_ago_fallback_and_clamp(self) -> None:
        signal = sunlight_signal({dt.date(2026, 7, 7): 150.0}, AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.value == pytest.approx(1.0)  # 150 > target clamps
        assert signal.raw["stale_days"] == 2

    def test_today_partial_total_never_anchors(self) -> None:
        # Only today's (partial) total exists -> missing, not a false zero.
        signal = sunlight_signal({AS_OF: 200.0}, AS_OF)
        assert isinstance(signal, MissingSignal)
        assert signal.reason == "no_recent_daylight_data"

    def test_stale_data_is_missing(self) -> None:
        signal = sunlight_signal({dt.date(2026, 7, 6): 90.0}, AS_OF)
        assert isinstance(signal, MissingSignal)

    def test_zero_minutes_is_present_zero_bonus(self) -> None:
        signal = sunlight_signal({dt.date(2026, 7, 8): 0.0}, AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.value == 0.0


class TestNoiseSignal:
    def test_midpoint_vector(self) -> None:
        signal = noise_signal({AS_OF: 67.5}, AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.value == pytest.approx(0.5)  # (67.5 - 55) / 25

    def test_floor_and_ceiling(self) -> None:
        quiet = noise_signal({AS_OF: 50.0}, AS_OF)
        loud = noise_signal({AS_OF: 90.0}, AS_OF)
        assert isinstance(quiet, FactorSignal) and quiet.value == 0.0
        assert isinstance(loud, FactorSignal) and loud.value == 1.0

    def test_today_preferred_over_yesterday(self) -> None:
        signal = noise_signal({AS_OF: 55.0, dt.date(2026, 7, 8): 80.0}, AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.value == 0.0
        assert signal.raw["stale_days"] == 0

    def test_yesterday_fallback(self) -> None:
        signal = noise_signal({dt.date(2026, 7, 8): 80.0}, AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.value == pytest.approx(1.0)
        assert signal.raw["stale_days"] == 1

    def test_stale_is_missing(self) -> None:
        signal = noise_signal({dt.date(2026, 7, 7): 80.0}, AS_OF)
        assert isinstance(signal, MissingSignal)
        assert signal.reason == "no_recent_noise_data"


def _pt(iso: str, value: float, *, daily: bool = False) -> SeriesPoint:
    return SeriesPoint(dt.datetime.fromisoformat(iso), value, daily)


class TestAlcoholSignal:
    def test_previous_evening_vector(self) -> None:
        points = [
            _pt("2026-07-08T20:00:00+00:00", 2.0),
            _pt("2026-07-09T00:30:00+00:00", 1.0),  # post-midnight still counts
        ]
        signal = alcohol_signal(points, AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.raw["drinks"] == pytest.approx(3.0)
        assert signal.value == pytest.approx(0.75)  # 3 / 4

    def test_daily_total_replaces_intraday_samples(self) -> None:
        points = [
            _pt("2026-07-08T00:00:00+00:00", 3.0, daily=True),
            _pt("2026-07-08T21:00:00+00:00", 2.0),  # must not be re-added
        ]
        signal = alcohol_signal(points, AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.raw["drinks"] == pytest.approx(3.0)
        assert signal.value == pytest.approx(0.75)

    def test_heavy_evening_clamps(self) -> None:
        signal = alcohol_signal([_pt("2026-07-08T22:00:00+00:00", 6.0)], AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.value == 1.0

    def test_sober_evening_for_a_tracking_user_is_zero(self) -> None:
        # A log outside the window proves tracking; the evening itself is 0.
        signal = alcohol_signal([_pt("2026-07-01T20:00:00+00:00", 5.0)], AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.value == 0.0
        assert signal.raw["drinks"] == 0.0

    def test_morning_cutoff_excludes_todays_daytime_drinks(self) -> None:
        signal = alcohol_signal([_pt("2026-07-09T07:00:00+00:00", 2.0)], AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.raw["drinks"] == 0.0

    def test_todays_daily_total_never_counts_as_previous_evening(self) -> None:
        # Providers stamp day-level rows at day start (inside the small-hours
        # window), but a daily total for *today* covers the whole current day
        # — including drinks after the cutoff, which the pinned cutoff test
        # above excludes when they arrive as intraday samples. Same data,
        # same answer: excluded.
        signal = alcohol_signal(
            [_pt("2026-07-09T00:00:00+00:00", 4.0, daily=True)], AS_OF
        )
        assert isinstance(signal, FactorSignal)
        assert signal.raw["drinks"] == 0.0
        assert signal.value == 0.0

    def test_yesterdays_daily_total_still_counts_next_to_todays(self) -> None:
        points = [
            _pt("2026-07-08T00:00:00+00:00", 3.0, daily=True),  # yesterday: counts
            _pt("2026-07-09T00:00:00+00:00", 4.0, daily=True),  # today: excluded
            _pt("2026-07-09T01:00:00+00:00", 1.0),  # small-hours sample: counts
        ]
        signal = alcohol_signal(points, AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.raw["drinks"] == pytest.approx(4.0)  # 3 + 1
        assert signal.value == pytest.approx(1.0)

    def test_no_logs_at_all_is_missing(self) -> None:
        signal = alcohol_signal([], AS_OF)
        assert isinstance(signal, MissingSignal)
        assert signal.reason == "no_alcohol_logs_in_lookback"


class TestHydrationSignal:
    BASELINE = {dt.date(2026, 6, 24) + dt.timedelta(days=i): 2000.0 for i in range(14)}

    def test_half_deficit_is_max_severity(self) -> None:
        series = {**self.BASELINE, dt.date(2026, 7, 8): 1000.0}
        signal = hydration_signal(series, AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.raw["baseline_median"] == pytest.approx(2000.0)
        assert signal.raw["deficit_fraction"] == pytest.approx(0.5)
        assert signal.value == pytest.approx(1.0)

    def test_quarter_deficit_vector(self) -> None:
        series = {**self.BASELINE, dt.date(2026, 7, 8): 1500.0}
        signal = hydration_signal(series, AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.value == pytest.approx(0.5)  # 0.25 / 0.5

    def test_at_or_above_baseline_is_zero(self) -> None:
        at_baseline = hydration_signal({**self.BASELINE, dt.date(2026, 7, 8): 2000.0}, AS_OF)
        above = hydration_signal({**self.BASELINE, dt.date(2026, 7, 8): 2500.0}, AS_OF)
        assert isinstance(at_baseline, FactorSignal) and at_baseline.value == 0.0
        assert isinstance(above, FactorSignal) and above.value == 0.0

    def test_todays_partial_total_never_anchors(self) -> None:
        # metric_baseline is anchored at as_of - 1: today's 100 mL so far must
        # not read as a deficit while yesterday hit the baseline.
        series = {**self.BASELINE, dt.date(2026, 7, 8): 2000.0, AS_OF: 100.0}
        signal = hydration_signal(series, AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.value == 0.0
        assert signal.raw["current"]["date"] == "2026-07-08"

    def test_stale_intake_is_missing(self) -> None:
        series = dict(self.BASELINE)  # freshest complete day is 07-07 (ok) ...
        signal = hydration_signal(series, dt.date(2026, 7, 12))  # ... but not for 07-12
        assert isinstance(signal, MissingSignal)
        assert signal.reason == "no_recent_hydration_data"

    def test_thin_baseline_is_missing(self) -> None:
        series = {
            dt.date(2026, 7, 5): 2000.0,
            dt.date(2026, 7, 6): 2000.0,
            dt.date(2026, 7, 7): 2000.0,
            dt.date(2026, 7, 8): 1000.0,
        }
        signal = hydration_signal(series, AS_OF)
        assert isinstance(signal, MissingSignal)
        assert "baseline_days" in signal.reason

    def test_zero_baseline_is_missing(self) -> None:
        series = {day: 0.0 for day in self.BASELINE}
        series[dt.date(2026, 7, 8)] = 0.0
        signal = hydration_signal(series, AS_OF)
        assert isinstance(signal, MissingSignal)
        assert signal.reason == "zero_hydration_baseline"


# ---------------------------------------------------------------------------
# v2 row digestion
# ---------------------------------------------------------------------------


def _series_row(
    timestamp: str, series_type: str, value: float, *, is_daily_total: bool | None = False
) -> dict:
    """A TimeSeriesSample-shaped row (schemas/responses/activity)."""
    return {
        "timestamp": timestamp,
        "zone_offset": None,
        "type": series_type,
        "value": value,
        "unit": "any",
        "source": {"provider": "apple_health"},
        "is_daily_total": is_daily_total,
    }


class TestDigestV2Rows:
    def test_v1_shaped_call_leaves_v2_fields_none(self) -> None:
        digest = digest_ow_rows([], [], AS_OF)
        assert digest.daylight_by_day is None
        assert digest.noise_db_by_day is None
        assert digest.alcohol_points is None
        assert digest.hydration_by_day is None
        assert digest.cycles is None

    def test_fetched_but_empty_is_empty_not_none(self) -> None:
        digest = digest_ow_rows([], [], AS_OF, series_rows=[], cycle_rows=[])
        assert digest.daylight_by_day == {}
        assert digest.noise_db_by_day == {}
        assert digest.alcohol_points == ()
        assert digest.hydration_by_day == {}
        assert digest.cycles == ()

    def test_series_are_split_by_type_with_vendor_aggregation(self) -> None:
        rows = [
            # daylight: SUM of intraday samples
            _series_row("2026-07-08T10:00:00+00:00", "time_in_daylight", 40.0),
            _series_row("2026-07-08T15:00:00+00:00", "time_in_daylight", 50.0),
            # noise: AVG of samples
            _series_row("2026-07-09T10:00:00+00:00", "environmental_audio_exposure", 65.0),
            _series_row("2026-07-09T11:00:00+00:00", "environmental_audio_exposure", 70.0),
            # alcohol: kept as points for the evening window
            _series_row(
                "2026-07-08T20:00:00+00:00", "number_of_alcoholic_beverages", 2.0
            ),
            # hydration: SUM per day
            _series_row("2026-07-08T08:00:00+00:00", "hydration", 500.0),
            _series_row("2026-07-08T13:00:00+00:00", "hydration", 1000.0),
            # unrelated series types are ignored
            _series_row("2026-07-09T10:00:00+00:00", "steps", 4000.0),
        ]
        digest = digest_ow_rows([], [], AS_OF, series_rows=rows)
        assert digest.daylight_by_day == {dt.date(2026, 7, 8): pytest.approx(90.0)}
        assert digest.noise_db_by_day == {AS_OF: pytest.approx(67.5)}
        assert digest.hydration_by_day == {dt.date(2026, 7, 8): pytest.approx(1500.0)}
        assert digest.alcohol_points is not None and len(digest.alcohol_points) == 1
        (point,) = digest.alcohol_points
        assert point.value == 2.0 and point.is_daily_total is False

    def test_daily_total_row_replaces_intraday_samples(self) -> None:
        rows = [
            _series_row(
                "2026-07-08T00:00:00+00:00", "time_in_daylight", 90.0, is_daily_total=True
            ),
            _series_row("2026-07-08T10:00:00+00:00", "time_in_daylight", 30.0),
        ]
        digest = digest_ow_rows([], [], AS_OF, series_rows=rows)
        assert digest.daylight_by_day == {dt.date(2026, 7, 8): pytest.approx(90.0)}

    def test_bad_rows_are_skipped(self) -> None:
        rows = [
            _series_row("not-a-timestamp", "hydration", 500.0),
            {"type": "hydration", "value": None, "timestamp": "2026-07-08T08:00:00+00:00"},
            _series_row("2026-07-08T09:00:00+00:00", "hydration", 250.0),
        ]
        digest = digest_ow_rows([], [], AS_OF, series_rows=rows)
        assert digest.hydration_by_day == {dt.date(2026, 7, 8): pytest.approx(250.0)}

    def test_cycles_pass_through(self) -> None:
        row = _cycle_row("2026-07-06T00:00:00Z", cycle_length=28)
        digest = digest_ow_rows([], [], AS_OF, cycle_rows=[row])
        assert digest.cycles == (row,)
