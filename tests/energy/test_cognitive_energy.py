"""Cognitive-energy engine unit tests (docs/PLAN.md §3, Phase 2).

Hand-computed vectors for the full-signal and missing-signal (renormalization)
cases, the components-sum-to-score invariant, and every factor builder.
Everything here is pure — no store, no network, no clock.
"""

import datetime as dt

import pytest

from healthmes.engine.cognitive_energy import (
    FACTOR_SPECS,
    STATUS_INSUFFICIENT,
    STATUS_OK,
    FactorSignal,
    MissingSignal,
    UsageBucket,
    charge_signal,
    compute_estimate,
    digest_ow_rows,
    fragmentation_signal,
    hrv_signal,
    meeting_load_signal,
    sleep_debt_signal,
    stress_signal,
)

UTC = dt.UTC
AS_OF = dt.date(2026, 7, 9)
WS = dt.datetime(2026, 7, 9, 14, 0, tzinfo=UTC)
WE = dt.datetime(2026, 7, 9, 15, 0, tzinfo=UTC)


def _at(hour: int, minute: int = 0) -> dt.datetime:
    """2026-07-09 HH:MM UTC (the vector day)."""
    return dt.datetime(2026, 7, 9, hour, minute, tzinfo=UTC)


def _by_name(estimate) -> dict[str, dict]:
    return {item["name"]: item for item in estimate.components}


def _vector_signals() -> list[FactorSignal]:
    """The hand vector: severities/charge chosen for clean arithmetic."""
    return [
        FactorSignal("sleep_debt", 0.2, {"index": 20.0}),
        FactorSignal("stress", 0.4, {"value": 40.0}),
        FactorSignal("hrv_deviation", 0.5, {"z_score": -1.25}),
        FactorSignal("body_battery", 0.8, {"normalized_value": 80.0}),
        FactorSignal("meeting_load", 0.45, {"booked_minutes": 30.0}),
        FactorSignal("fragmentation", 0.5, {"distracting_launches": 6}),
    ]


# ---------------------------------------------------------------------------
# Score composition: hand-computed vectors
# ---------------------------------------------------------------------------


class TestFullSignalVector:
    """All six factors present -> plan formula with the base weights.

    base = 100 - bonus budget (0.10 * 100) = 90
    score = 90 - 30*0.2 - 20*0.4 - 15*0.5 + 10*0.8 - 15*0.45 - 10*0.5
          = 90 - 6 - 8 - 7.5 + 8 - 6.75 - 5 = 64.75 -> 65
    """

    def test_score(self) -> None:
        estimate = compute_estimate(WS, WE, _vector_signals())
        assert estimate.status == STATUS_OK
        assert estimate.score_exact == pytest.approx(64.75)
        assert estimate.score == 65

    def test_component_contributions(self) -> None:
        estimate = compute_estimate(WS, WE, _vector_signals())
        components = _by_name(estimate)
        assert components["base"]["contribution"] == pytest.approx(90.0)
        assert components["sleep_debt_penalty"]["contribution"] == pytest.approx(-6.0)
        assert components["stress_penalty"]["contribution"] == pytest.approx(-8.0)
        assert components["hrv_deviation_penalty"]["contribution"] == pytest.approx(-7.5)
        assert components["body_battery_bonus"]["contribution"] == pytest.approx(8.0)
        assert components["meeting_load_penalty"]["contribution"] == pytest.approx(-6.75)
        assert components["fragmentation_penalty"]["contribution"] == pytest.approx(-5.0)

    def test_weights_are_the_base_weights(self) -> None:
        estimate = compute_estimate(WS, WE, _vector_signals())
        components = _by_name(estimate)
        assert components["base"]["weight"] is None
        assert components["sleep_debt_penalty"]["weight"] == pytest.approx(0.30)
        assert components["stress_penalty"]["weight"] == pytest.approx(0.20)
        assert components["hrv_deviation_penalty"]["weight"] == pytest.approx(0.15)
        assert components["body_battery_bonus"]["weight"] == pytest.approx(0.10)
        assert components["meeting_load_penalty"]["weight"] == pytest.approx(0.15)
        assert components["fragmentation_penalty"]["weight"] == pytest.approx(0.10)
        assert components["base"]["raw"]["renormalized"] is False

    def test_every_factor_lands_in_components_with_required_fields(self) -> None:
        estimate = compute_estimate(WS, WE, _vector_signals())
        names = [item["name"] for item in estimate.components]
        assert names == [
            "base",
            "sleep_debt_penalty",
            "stress_penalty",
            "hrv_deviation_penalty",
            "body_battery_bonus",
            "meeting_load_penalty",
            "fragmentation_penalty",
        ]
        for item in estimate.components:
            assert set(item) >= {"name", "weight", "raw", "contribution"}
            assert isinstance(item["raw"], dict)


class TestMissingSignalRenormalization:
    """Dropped terms renormalize the remaining base weights (plan-mandated).

    Present: sleep 0.30, stress 0.20, hrv 0.15, meeting 0.15 (sum 0.80)
    -> shares 0.375 / 0.25 / 0.1875 / 0.1875, no bonus so base = 100.
    score = 100 - 37.5*0.2 - 25*0.4 - 18.75*0.5 - 18.75*0.45
          = 100 - 7.5 - 10 - 9.375 - 8.4375 = 64.6875 -> 65
    """

    @staticmethod
    def _signals() -> list[FactorSignal]:
        return [s for s in _vector_signals() if s.key not in ("body_battery", "fragmentation")]

    @staticmethod
    def _missing() -> list[MissingSignal]:
        return [
            MissingSignal("body_battery", "no_fresh_charge_score"),
            MissingSignal("fragmentation", "no_app_usage_data"),
        ]

    def test_renormalized_weights(self) -> None:
        estimate = compute_estimate(WS, WE, self._signals(), self._missing())
        components = _by_name(estimate)
        assert components["sleep_debt_penalty"]["weight"] == pytest.approx(0.375)
        assert components["stress_penalty"]["weight"] == pytest.approx(0.25)
        assert components["hrv_deviation_penalty"]["weight"] == pytest.approx(0.1875)
        assert components["meeting_load_penalty"]["weight"] == pytest.approx(0.1875)
        assert components["base"]["raw"]["renormalized"] is True

    def test_score(self) -> None:
        estimate = compute_estimate(WS, WE, self._signals(), self._missing())
        components = _by_name(estimate)
        assert components["base"]["contribution"] == pytest.approx(100.0)
        assert components["sleep_debt_penalty"]["contribution"] == pytest.approx(-7.5)
        assert components["stress_penalty"]["contribution"] == pytest.approx(-10.0)
        assert components["hrv_deviation_penalty"]["contribution"] == pytest.approx(-9.375)
        assert components["meeting_load_penalty"]["contribution"] == pytest.approx(-8.4375)
        assert estimate.score_exact == pytest.approx(64.6875)
        assert estimate.score == 65

    def test_dropped_terms_are_absent_but_recorded(self) -> None:
        estimate = compute_estimate(WS, WE, self._signals(), self._missing())
        names = {item["name"] for item in estimate.components}
        assert "body_battery_bonus" not in names
        assert "fragmentation_penalty" not in names
        assert estimate.inputs_snapshot["missing_signals"] == [
            {"name": "body_battery", "reason": "no_fresh_charge_score"},
            {"name": "fragmentation", "reason": "no_app_usage_data"},
        ]
        base = _by_name(estimate)["base"]
        assert base["raw"]["factors_missing"] == estimate.inputs_snapshot["missing_signals"]


class TestComponentsSumToScore:
    """The plan-required invariant: components sum to the score."""

    @pytest.mark.parametrize(
        "signals",
        [
            pytest.param(_vector_signals(), id="full-signal"),
            pytest.param(
                [s for s in _vector_signals() if s.key not in ("body_battery", "fragmentation")],
                id="renormalized",
            ),
            pytest.param([FactorSignal("body_battery", 0.8, {})], id="bonus-only"),
            pytest.param(
                [
                    FactorSignal("sleep_debt", 1.0, {}),
                    FactorSignal("stress", 1.0, {}),
                    FactorSignal("hrv_deviation", 1.0, {}),
                    FactorSignal("body_battery", 0.0, {}),
                    FactorSignal("meeting_load", 1.0, {}),
                    FactorSignal("fragmentation", 1.0, {}),
                ],
                id="worst-case-floor",
            ),
        ],
    )
    def test_components_sum_to_score(self, signals: list[FactorSignal]) -> None:
        estimate = compute_estimate(WS, WE, signals)
        total = sum(item["contribution"] for item in estimate.components)
        assert total == pytest.approx(estimate.score_exact, abs=1e-9)
        assert abs(estimate.score - estimate.score_exact) <= 0.5
        assert estimate.score == round(estimate.score_exact)
        assert 0 <= estimate.score <= 100

    def test_worst_case_is_zero_best_case_is_hundred(self) -> None:
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


class TestComputeEstimateEdges:
    def test_no_signals_is_insufficient_never_a_fake_100(self) -> None:
        missing = [MissingSignal(spec.key, "ow_unavailable") for spec in FACTOR_SPECS]
        estimate = compute_estimate(WS, WE, [], missing)
        assert estimate.status == STATUS_INSUFFICIENT
        assert estimate.score is None
        assert estimate.score_exact is None
        assert estimate.components == ()
        assert len(estimate.inputs_snapshot["missing_signals"]) == len(FACTOR_SPECS)

    def test_bonus_only_score_equals_battery(self) -> None:
        estimate = compute_estimate(WS, WE, [FactorSignal("body_battery", 0.8, {})])
        # The bonus owns the whole 100-point budget: base 0 + 100 * 0.8.
        assert estimate.score_exact == pytest.approx(80.0)
        assert estimate.score == 80

    def test_unknown_factor_key_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown factor key"):
            compute_estimate(WS, WE, [FactorSignal("caffeine", 0.5, {})])

    def test_duplicate_factor_key_raises(self) -> None:
        signals = [FactorSignal("stress", 0.5, {}), FactorSignal("stress", 0.6, {})]
        with pytest.raises(ValueError, match="duplicate factor key"):
            compute_estimate(WS, WE, signals)

    def test_components_payload_shape(self) -> None:
        estimate = compute_estimate(WS, WE, _vector_signals())
        payload = estimate.components_payload()
        assert payload["version"] == 1
        assert payload["score_exact"] == estimate.score_exact
        assert [item["name"] for item in payload["items"]] == [
            item["name"] for item in estimate.components
        ]


# ---------------------------------------------------------------------------
# Factor builders
# ---------------------------------------------------------------------------


class TestSleepDebtSignal:
    def test_vector(self) -> None:
        scores = {dt.date(2026, 7, day): 80.0 for day in range(3, 10)}
        signal = sleep_debt_signal(scores, AS_OF, source="internal_sleep_score")
        assert isinstance(signal, FactorSignal)
        assert signal.value == pytest.approx(0.2)
        assert signal.raw["index"] == pytest.approx(20.0)
        assert signal.raw["nights_counted"] == 7
        assert signal.raw["source"] == "internal_sleep_score"

    def test_too_few_nights_is_missing(self) -> None:
        scores = {dt.date(2026, 7, 9): 80.0, dt.date(2026, 7, 8): 70.0}
        signal = sleep_debt_signal(scores, AS_OF)
        assert isinstance(signal, MissingSignal)
        assert signal.key == "sleep_debt"


class TestStressSignal:
    def test_time_weighted_garmin(self) -> None:
        garmin = {
            dt.date(2026, 7, 9): 40.0,  # weight 1
            dt.date(2026, 7, 8): 60.0,  # weight 0.5
            dt.date(2026, 7, 7): 80.0,  # weight 0.25
        }
        signal = stress_signal(garmin, {}, AS_OF)
        assert isinstance(signal, FactorSignal)
        # (40 + 30 + 20) / 1.75 = 51.428571...
        assert signal.value == pytest.approx(90.0 / 1.75 / 100.0)
        assert signal.raw["source"] == "garmin_stress"
        assert len(signal.raw["days_used"]) == 3

    def test_resilience_proxy_fallback(self) -> None:
        resilience = {dt.date(2026, 7, 9): 70.0}
        signal = stress_signal({}, resilience, AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.value == pytest.approx(0.3)  # 100 - 70 = 30 stress
        assert signal.raw["source"] == "internal_resilience_proxy"

    def test_garmin_preferred_over_proxy(self) -> None:
        signal = stress_signal(
            {dt.date(2026, 7, 9): 40.0}, {dt.date(2026, 7, 9): 10.0}, AS_OF
        )
        assert isinstance(signal, FactorSignal)
        assert signal.raw["source"] == "garmin_stress"

    def test_stale_readings_are_missing(self) -> None:
        garmin = {dt.date(2026, 7, 5): 40.0}  # 4 days old > 3-day staleness policy
        signal = stress_signal(garmin, {}, AS_OF)
        assert isinstance(signal, MissingSignal)
        assert signal.reason == "no_recent_stress_or_resilience"


class TestHrvSignal:
    BASELINE = {
        dt.date(2026, 7, 2): 46.0,
        dt.date(2026, 7, 3): 46.0,
        dt.date(2026, 7, 4): 46.0,
        dt.date(2026, 7, 5): 50.0,
        dt.date(2026, 7, 6): 54.0,
        dt.date(2026, 7, 7): 54.0,
        dt.date(2026, 7, 8): 54.0,
    }

    def test_below_baseline_vector(self) -> None:
        series = {**self.BASELINE, dt.date(2026, 7, 9): 45.0}
        signal = hrv_signal(series, {}, AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.raw["variant"] == "rmssd"
        assert signal.raw["z_score"] == pytest.approx(-1.25)
        assert signal.value == pytest.approx(0.5)  # 1.25 / 2.5

    def test_above_baseline_is_present_with_zero_severity(self) -> None:
        series = {**self.BASELINE, dt.date(2026, 7, 9): 55.0}
        signal = hrv_signal(series, {}, AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.value == 0.0

    def test_variant_with_more_nights_wins_and_never_mixes(self) -> None:
        sdnn = {**self.BASELINE, dt.date(2026, 7, 9): 45.0}
        rmssd = {dt.date(2026, 7, 8): 40.0, dt.date(2026, 7, 9): 41.0}
        signal = hrv_signal(rmssd, sdnn, AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.raw["variant"] == "sdnn"
        assert signal.raw["baseline_median"] == pytest.approx(50.0)

    def test_stale_last_night_is_missing(self) -> None:
        series = dict(self.BASELINE)  # freshest night is 07-08... as_of 07-09 is fine
        signal = hrv_signal(series, {}, dt.date(2026, 7, 11))  # now 3 days later
        assert isinstance(signal, MissingSignal)
        assert signal.reason == "no_recent_nocturnal_hrv"

    def test_zero_spread_baseline_is_missing(self) -> None:
        series = {dt.date(2026, 7, day): 50.0 for day in range(2, 10)}
        signal = hrv_signal(series, {}, AS_OF)
        assert isinstance(signal, MissingSignal)
        assert signal.reason == "undefined_hrv_deviation_zero_spread"

    def test_no_data_is_missing(self) -> None:
        signal = hrv_signal({}, {}, AS_OF)
        assert isinstance(signal, MissingSignal)
        assert signal.reason == "no_nocturnal_hrv"


class TestChargeSignal:
    def test_body_battery_preferred(self) -> None:
        points = {
            "body_battery": ((dt.datetime(2026, 7, 9, 6, 30, tzinfo=UTC), 80.0, "garmin"),),
            "recovery": ((dt.datetime(2026, 7, 9, 7, 0, tzinfo=UTC), 99.0, "whoop"),),
        }
        signal = charge_signal(points, AS_OF)
        assert isinstance(signal, FactorSignal)
        assert signal.raw["category"] == "body_battery"
        assert signal.value == pytest.approx(0.8)

    def test_polar_readiness_scale_normalized(self) -> None:
        points = {
            "readiness": ((dt.datetime(2026, 7, 9, 7, 0, tzinfo=UTC), 8.0, "polar"),),
        }
        signal = charge_signal(points, AS_OF)
        assert isinstance(signal, FactorSignal)
        # Polar readiness is 0-10 (vendor HEALTH_SCORE_RANGES) -> 80/100.
        assert signal.raw["normalized_value"] == pytest.approx(80.0)
        assert signal.value == pytest.approx(0.8)

    def test_stale_readings_are_missing(self) -> None:
        points = {
            "body_battery": ((dt.datetime(2026, 7, 7, 6, 30, tzinfo=UTC), 80.0, "garmin"),),
        }
        signal = charge_signal(points, AS_OF)
        assert isinstance(signal, MissingSignal)
        assert signal.reason == "no_fresh_charge_score"


class TestMeetingLoadSignal:
    def test_vector_window(self) -> None:
        events = [(_at(14, 0), _at(14, 30))]
        signal = meeting_load_signal(events, WS, WE, calendar_active=True)
        assert isinstance(signal, FactorSignal)
        assert signal.raw["booked_minutes"] == pytest.approx(30.0)
        assert signal.raw["context_switches"] == 1
        assert signal.value == pytest.approx(0.45)  # 0.7*0.5 + 0.3*(1/3)

    def test_overlapping_events_not_double_counted(self) -> None:
        events = [(_at(14, 0), _at(14, 30)), (_at(14, 15), _at(14, 45))]
        signal = meeting_load_signal(events, WS, WE, calendar_active=True)
        assert isinstance(signal, FactorSignal)
        assert signal.raw["booked_minutes"] == pytest.approx(45.0)  # union, not 60
        assert signal.raw["context_switches"] == 2

    def test_event_started_before_window_is_no_switch(self) -> None:
        events = [(_at(13, 30), _at(14, 20))]
        signal = meeting_load_signal(events, WS, WE, calendar_active=True)
        assert isinstance(signal, FactorSignal)
        assert signal.raw["booked_minutes"] == pytest.approx(20.0)
        assert signal.raw["context_switches"] == 0

    def test_free_hour_on_active_calendar_is_zero_severity(self) -> None:
        signal = meeting_load_signal([], WS, WE, calendar_active=True)
        assert isinstance(signal, FactorSignal)
        assert signal.value == 0.0

    def test_inactive_calendar_is_missing(self) -> None:
        signal = meeting_load_signal([], WS, WE, calendar_active=False)
        assert isinstance(signal, MissingSignal)
        assert signal.reason == "calendar_mirror_inactive"


class TestFragmentationSignal:
    NOW = dt.datetime(2026, 7, 9, 14, 23, tzinfo=UTC)

    def test_vector_window(self) -> None:
        usage = [UsageBucket(_at(13, 0), "com.instagram.android", 6, "social")]
        signal = fragmentation_signal(usage, WS, self.NOW)
        assert isinstance(signal, FactorSignal)
        assert signal.raw["distracting_launches"] == 6
        assert signal.value == pytest.approx(0.5)  # 6 / 12

    def test_only_distracting_categories_count(self) -> None:
        # Category labels are the Android collector's wire vocabulary
        # (UsageSnapshotReader.kt::categoryOf): "game", not "games".
        usage = [
            UsageBucket(_at(13, 0), "com.instagram.android", 6, "social"),
            UsageBucket(_at(13, 30), "com.jetbrains.ide", 9, "productivity"),
            UsageBucket(_at(13, 30), "com.supercell.clashroyale", 3, "game"),
            UsageBucket(_at(12, 30), "com.tiktok", 9, "video"),
        ]
        signal = fragmentation_signal(usage, WS, self.NOW)
        assert isinstance(signal, FactorSignal)
        # 12:30 is outside the trailing hour; productivity never counts.
        assert signal.raw["distracting_launches"] == 9
        assert signal.value == pytest.approx(0.75)
        assert signal.raw["by_app"] == {
            "com.instagram.android": 6,
            "com.supercell.clashroyale": 3,
        }

    def test_collector_vocabulary_matches_distracting_set(self) -> None:
        # The Android collector emits exactly these labels (or null); the
        # distracting set must be a subset so no filter entry is dead.
        collector_labels = {
            "game",
            "audio",
            "video",
            "image",
            "social",
            "news",
            "maps",
            "productivity",
            "accessibility",
        }
        from healthmes.engine.cognitive_energy import DISTRACTING_CATEGORIES

        assert DISTRACTING_CATEGORIES <= collector_labels

    def test_game_launches_count_as_distracting(self) -> None:
        # 15 game launches in the trailing hour: the exact regression the
        # 'games' != 'game' mismatch silently zeroed out.
        usage = [UsageBucket(_at(13, 30), "com.supercell.clashroyale", 15, "game")]
        signal = fragmentation_signal(usage, WS, self.NOW)
        assert isinstance(signal, FactorSignal)
        assert signal.raw["distracting_launches"] == 15
        assert signal.value == 1.0  # 15 > FRAGMENTATION_MAX_LAUNCHES clamps to max

    def test_future_window_is_missing(self) -> None:
        usage = [UsageBucket(_at(13, 0), "com.instagram.android", 6, "social")]
        signal = fragmentation_signal(usage, _at(16, 0), self.NOW)
        assert isinstance(signal, MissingSignal)
        assert signal.reason == "window_in_future"

    def test_no_usage_data_is_missing(self) -> None:
        signal = fragmentation_signal([], WS, self.NOW)
        assert isinstance(signal, MissingSignal)
        assert signal.reason == "no_app_usage_data"

    def test_quiet_hour_with_reporting_device_is_zero_severity(self) -> None:
        usage = [UsageBucket(_at(9, 0), "com.instagram.android", 6, "social")]
        signal = fragmentation_signal(usage, WS, self.NOW)
        assert isinstance(signal, FactorSignal)
        assert signal.value == 0.0


# ---------------------------------------------------------------------------
# open-wearables row digestion (reuses the mcp_server helpers)
# ---------------------------------------------------------------------------


class TestDigestOwRows:
    def test_vector_rows(self, full_signal_ow_rows) -> None:
        digest = digest_ow_rows(
            full_signal_ow_rows.score_rows, full_signal_ow_rows.sleep_rows, AS_OF
        )
        assert digest.sleep_score_source == "internal_sleep_score"
        assert digest.sleep_scores_by_day == {
            dt.date(2026, 7, day): 80.0 for day in range(3, 10)
        }
        assert digest.garmin_stress_by_day == {AS_OF: 40.0}
        assert len(digest.rmssd_by_day) == 8
        assert digest.sdnn_by_day == {}
        assert set(digest.charge_points) == {"body_battery"}
        ((recorded_at, value, provider),) = digest.charge_points["body_battery"]
        assert (value, provider) == (80.0, "garmin")
        assert recorded_at == dt.datetime(2026, 7, 9, 6, 30, tzinfo=UTC)

    def test_internal_resilience_components_are_read(self, make_score_row) -> None:
        rows = [
            make_score_row(
                "resilience",
                "internal",
                "2026-07-09T06:00:00+00:00",
                5.2,  # raw HRV-CV in `value`
                components={"resilience_score": {"value": 70}},
            )
        ]
        digest = digest_ow_rows(rows, [], AS_OF)
        assert digest.resilience_by_day == {AS_OF: 70.0}

    def test_provider_sleep_score_fallback(self, make_score_row) -> None:
        rows = [make_score_row("sleep", "oura", "2026-07-09T07:00:00+00:00", 72)]
        digest = digest_ow_rows(rows, [], AS_OF)
        assert digest.sleep_score_source == "provider_sleep_score"
        assert digest.sleep_scores_by_day == {AS_OF: 72.0}
