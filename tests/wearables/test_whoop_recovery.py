from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from healthmes.wearables.whoop_recovery import (
    WHOOP_RECOVERY_ALGORITHM,
    calculate_whoop_recovery_package,
    whoop_label,
)

AS_OF = date(2026, 8, 22)
TIMEZONE = ZoneInfo("Asia/Seoul")
CYCLE_ID = "cycle-2026-08-22"


def _recovery_row(
    value: float = 70,
    *,
    record_id: str | None = "recovery-current",
    recorded_at: str = "2026-08-22T07:00:00+09:00",
    cycle_id: str | None = CYCLE_ID,
    category: str = "recovery",
    provider: str = "whoop",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "provider": provider,
        "category": category,
        "recorded_at": recorded_at,
        "value": value,
        "components": {},
    }
    if record_id is not None:
        row["id"] = record_id
    if cycle_id is not None:
        row["components"]["cycle_id"] = {"qualifier": cycle_id}
    return row


def _day_strain_row(
    value: float = 9,
    *,
    record_id: str | None = "day-strain-current",
    recorded_at: str = "2026-08-22T18:00:00+09:00",
    updated_at: str = "2026-08-22T09:05:00+00:00",
    cycle_id: str | None = CYCLE_ID,
    category: str = "day_strain",
    provider: str = "whoop",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "provider": provider,
        "category": category,
        "recorded_at": recorded_at,
        "value": value,
        "components": {
            "cycle_updated_at": {"qualifier": updated_at},
        },
    }
    if record_id is not None:
        row["id"] = record_id
    if cycle_id is not None:
        row["components"]["cycle_id"] = {"qualifier": cycle_id}
    return row


def _calculate(
    recovery_rows: list[dict[str, Any]] | None = None,
    day_strain_rows: list[dict[str, Any]] | None = None,
    **kwargs: Any,
):
    return calculate_whoop_recovery_package(
        recovery_rows if recovery_rows is not None else [_recovery_row()],
        (
            day_strain_rows
            if day_strain_rows is not None
            else [_day_strain_row()]
        ),
        as_of=AS_OF,
        timezone=TIMEZONE,
        **kwargs,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (-0.01, None),
        (0, "red"),
        (33, "red"),
        (33.5, None),
        (34, "yellow"),
        (66, "yellow"),
        (66.5, None),
        (67, "green"),
        (100, "green"),
        (100.01, None),
        (float("nan"), None),
        (float("inf"), None),
    ],
)
def test_recovery_uses_exact_sake_boundaries(
    value: float,
    expected: str | None,
) -> None:
    assert whoop_label("recovery", value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (-0.01, None),
        (0, "light"),
        (9.5, "light"),
        (9.999, "light"),
        (10, "moderate"),
        (13.5, "moderate"),
        (13.999, "moderate"),
        (14, "high"),
        (17.5, "high"),
        (17.999, "high"),
        (18, "all_out"),
        (21, "all_out"),
        (21.001, None),
        (float("nan"), None),
        (float("-inf"), None),
    ],
)
def test_day_strain_uses_exact_sake_intervals(
    value: float,
    expected: str | None,
) -> None:
    assert whoop_label("day_strain", value) == expected


def test_current_local_day_wins_over_past_row_with_later_revision() -> None:
    past = _day_strain_row(
        19,
        record_id="past-later-revision",
        recorded_at="2026-08-21T20:00:00+09:00",
        updated_at="2026-08-23T12:00:00+00:00",
        cycle_id="cycle-past",
    )
    current = _day_strain_row(
        10,
        record_id="current-earlier-revision",
        updated_at="2026-08-22T08:00:00+00:00",
    )

    result = _calculate(day_strain_rows=[past, current])

    assert result.public["status"] == "ok"
    assert result.public["day_strain"] == {
        "status": "ok",
        "source_category": "day_strain",
        "label": "moderate",
        "freshness": "current_day",
        "stale_days": 0,
        "confidence": "high",
    }
    assert {item["record_id"] for item in result.provenance} == {
        "recovery-current",
        "current-earlier-revision",
    }


def test_malformed_past_revision_does_not_defeat_valid_current_day() -> None:
    malformed_past = _day_strain_row(
        19,
        record_id="past-malformed-revision",
        recorded_at="2026-08-21T20:00:00+09:00",
        updated_at="not-a-timestamp",
        cycle_id="cycle-past",
    )
    current = _day_strain_row(
        14,
        record_id="current-valid-revision",
    )

    result = _calculate(day_strain_rows=[malformed_past, current])

    assert result.public["status"] == "ok"
    assert result.public["day_strain"]["label"] == "high"
    assert result.public["limitations"] == []
    assert result.provenance[1]["record_id"] == "current-valid-revision"


@pytest.mark.parametrize("signal", ["recovery", "day_strain"])
def test_any_matching_malformed_recorded_at_fails_closed(
    signal: str,
) -> None:
    malformed = (
        _recovery_row(
            record_id="malformed-recovery",
            recorded_at="not-a-timestamp",
        )
        if signal == "recovery"
        else _day_strain_row(
            record_id="malformed-day-strain",
            recorded_at="not-a-timestamp",
        )
    )
    recovery_rows = (
        [_recovery_row(), malformed]
        if signal == "recovery"
        else [_recovery_row()]
    )
    day_strain_rows = (
        [_day_strain_row(), malformed]
        if signal == "day_strain"
        else [_day_strain_row()]
    )

    result = _calculate(
        recovery_rows=recovery_rows,
        day_strain_rows=day_strain_rows,
    )

    assert result.public["status"] == "insufficient_data"
    assert (
        result.public[signal]["reason"]
        == "unparseable_recorded_at"
    )
    assert all(
        item["record_id"]
        != (
            "malformed-recovery"
            if signal == "recovery"
            else "malformed-day-strain"
        )
        for item in result.provenance
    )


def test_retention_cutoff_is_applied_before_signal_selection() -> None:
    cutoff = datetime(2026, 8, 21, 23, tzinfo=UTC)

    result = _calculate(retained_after=cutoff)

    assert result.public["status"] == "insufficient_data"
    assert result.public["recovery"]["reason"] == "no_whoop_recovery"
    assert result.public["day_strain"]["status"] == "ok"
    assert {item["metric"] for item in result.provenance} == {
        "day_strain"
    }


@pytest.mark.parametrize("conflicting", [False, True])
def test_latest_tie_is_ambiguous_even_for_exact_duplicates(
    conflicting: bool,
) -> None:
    first = _day_strain_row(
        10,
        record_id="latest-a",
        updated_at="2026-08-22T10:00:00+00:00",
    )
    second = deepcopy(first)
    if conflicting:
        second["id"] = "latest-b"
        second["value"] = 18

    result = _calculate(day_strain_rows=[first, second])

    assert result.public["status"] == "insufficient_data"
    assert result.public["day_strain"]["status"] == "insufficient_data"
    assert (
        result.public["day_strain"]["reason"]
        == "ambiguous_latest_row"
    )
    assert result.conflicting_duplicate_rows is True
    assert "wearable_conflicting_duplicate_rows" in result.public[
        "limitations"
    ]
    assert {item["metric"] for item in result.provenance} == {"recovery"}


@pytest.mark.parametrize(
    (
        "recovery_truncated",
        "day_strain_truncated",
        "failed_signal",
        "retained_metric",
        "specific_limitation",
    ),
    [
        (
            True,
            False,
            "recovery",
            "day_strain",
            "whoop_recovery_source_truncated",
        ),
        (
            False,
            True,
            "day_strain",
            "recovery",
            "whoop_day_strain_source_truncated",
        ),
    ],
)
def test_each_primary_signal_has_an_independent_truncation_fence(
    recovery_truncated: bool,
    day_strain_truncated: bool,
    failed_signal: str,
    retained_metric: str,
    specific_limitation: str,
) -> None:
    result = _calculate(
        recovery_truncated=recovery_truncated,
        day_strain_truncated=day_strain_truncated,
    )

    other_signal = (
        "day_strain" if failed_signal == "recovery" else "recovery"
    )
    assert result.public["status"] == "insufficient_data"
    assert result.public[failed_signal]["reason"] == "truncated_source"
    assert result.public[other_signal]["status"] == "ok"
    assert {item["metric"] for item in result.provenance} == {
        retained_metric
    }
    assert specific_limitation in result.public["limitations"]
    assert (
        "wearable_upstream_page_limit_reached"
        in result.public["limitations"]
    )
    opposite_limitation = (
        "whoop_day_strain_source_truncated"
        if recovery_truncated
        else "whoop_recovery_source_truncated"
    )
    assert opposite_limitation not in result.public["limitations"]


@pytest.mark.parametrize(
    ("recovery_cycle", "day_strain_cycle", "reason"),
    [
        (None, CYCLE_ID, "cycle_id_missing"),
        (CYCLE_ID, None, "cycle_id_missing"),
        ("cycle-recovery", "cycle-strain", "cycle_id_mismatch"),
    ],
)
def test_package_fails_closed_for_missing_or_mismatched_cycle_linkage(
    recovery_cycle: str | None,
    day_strain_cycle: str | None,
    reason: str,
) -> None:
    result = _calculate(
        recovery_rows=[_recovery_row(cycle_id=recovery_cycle)],
        day_strain_rows=[
            _day_strain_row(cycle_id=day_strain_cycle)
        ],
    )

    assert result.public["status"] == "insufficient_data"
    assert result.public["recovery"]["status"] == "ok"
    assert result.public["day_strain"]["status"] == "ok"
    assert result.public["cycle_linkage"] == {
        "status": "insufficient_data",
        "reason": reason,
    }
    assert reason in result.public["limitations"]


def test_workout_strain_is_ignored_in_favor_of_cycle_day_strain() -> None:
    workout = _day_strain_row(
        20,
        record_id="workout-strain",
        category="strain",
        updated_at="2026-08-22T12:00:00+00:00",
    )
    cycle_day_strain = _day_strain_row(
        9,
        record_id="cycle-day-strain",
        updated_at="2026-08-22T08:00:00+00:00",
    )

    result = _calculate(
        day_strain_rows=[workout, cycle_day_strain]
    )

    assert result.public["status"] == "ok"
    assert result.public["day_strain"]["label"] == "light"
    assert result.public["level"] == "basic"
    assert {item["record_id"] for item in result.provenance} == {
        "recovery-current",
        "cycle-day-strain",
    }


def test_workout_strain_cannot_replace_missing_cycle_day_strain() -> None:
    result = _calculate(
        day_strain_rows=[
            _day_strain_row(
                20,
                record_id="workout-only",
                category="strain",
            )
        ]
    )

    assert result.public["status"] == "insufficient_data"
    assert (
        result.public["day_strain"]["reason"]
        == "no_whoop_day_strain"
    )
    assert {item["metric"] for item in result.provenance} == {"recovery"}


@pytest.mark.parametrize("missing_signal", ["recovery", "day_strain"])
def test_selected_rows_without_real_source_ids_fail_closed(
    missing_signal: str,
) -> None:
    recovery = _recovery_row(
        record_id=None if missing_signal == "recovery" else "recovery-id"
    )
    day_strain = _day_strain_row(
        record_id=(
            None if missing_signal == "day_strain" else "day-strain-id"
        )
    )

    result = _calculate(
        recovery_rows=[recovery],
        day_strain_rows=[day_strain],
    )

    assert result.public["status"] == "insufficient_data"
    assert (
        result.public[missing_signal]["reason"]
        == "source_record_id_missing"
    )
    retained_metric = (
        "day_strain" if missing_signal == "recovery" else "recovery"
    )
    assert {item["metric"] for item in result.provenance} == {
        retained_metric
    }
    assert all(item["record_id"] for item in result.provenance)


@pytest.mark.parametrize(
    ("id_field", "record_id"),
    [
        ("id", "upstream-id"),
        ("record_id", "upstream-record-id"),
        ("health_score_id", "upstream-health-score-id"),
        ("score_id", "upstream-score-id"),
    ],
)
def test_supported_upstream_id_fields_produce_stable_provenance(
    id_field: str,
    record_id: str,
) -> None:
    recovery = _recovery_row(record_id=None)
    recovery[id_field] = f"  {record_id}  "

    result = _calculate(recovery_rows=[recovery])

    assert result.public["status"] == "ok"
    assert result.provenance[0] == {
        "record_id": record_id,
        "source_provider": "open-wearables",
        "upstream_provider": "whoop",
        "resource_type": "health_score",
        "metric": "recovery",
        "metric_definition": "whoop.recovery-score.0-to-100.v1",
        "observed_at": "2026-08-21T22:00:00+00:00",
        "revision_at": "2026-08-21T22:00:00+00:00",
        "cycle_id": CYCLE_ID,
        "raw_value": 70.0,
        "schema_version": 1,
        "derived_by": WHOOP_RECOVERY_ALGORITHM,
    }


def test_private_provenance_defines_each_selected_metric() -> None:
    result = _calculate()

    assert {
        item["metric"]: item["metric_definition"]
        for item in result.provenance
    } == {
        "recovery": "whoop.recovery-score.0-to-100.v1",
        "day_strain": (
            "whoop.cycle-cumulative-day-strain.0-to-21.v1"
        ),
    }


def test_selected_stale_row_keeps_private_provenance() -> None:
    result = _calculate(
        recovery_rows=[
            _recovery_row(
                record_id="stale-recovery",
                recorded_at="2026-08-21T07:00:00+09:00",
                cycle_id="cycle-stale",
            )
        ]
    )

    assert result.public["status"] == "insufficient_data"
    assert (
        result.public["recovery"]["reason"]
        == "not_current_local_day"
    )
    provenance = {
        item["metric"]: item for item in result.provenance
    }
    assert provenance["recovery"]["record_id"] == "stale-recovery"
    assert provenance["recovery"]["raw_value"] == 70
    assert provenance["recovery"]["cycle_id"] == "cycle-stale"


@pytest.mark.parametrize(
    ("signal", "value"),
    (("recovery", 101), ("day_strain", 22)),
)
def test_selected_finite_out_of_range_row_keeps_private_provenance(
    signal: str,
    value: float,
) -> None:
    result = _calculate(
        recovery_rows=(
            [_recovery_row(value, record_id="invalid-recovery")]
            if signal == "recovery"
            else [_recovery_row()]
        ),
        day_strain_rows=(
            [_day_strain_row(value, record_id="invalid-day-strain")]
            if signal == "day_strain"
            else [_day_strain_row()]
        ),
    )

    assert result.public["status"] == "insufficient_data"
    assert (
        result.public[signal]["reason"]
        == "raw_value_out_of_range"
    )
    provenance = {
        item["metric"]: item for item in result.provenance
    }
    assert provenance[signal]["record_id"] == (
        "invalid-recovery"
        if signal == "recovery"
        else "invalid-day-strain"
    )
    assert provenance[signal]["raw_value"] == value


@pytest.mark.parametrize(
    (
        "recovery_value",
        "day_strain_value",
        "expected_level",
        "default_minutes",
        "choices",
        "rest_is_option",
        "walk_states",
    ),
    [
        (
            70,
            9,
            "basic",
            10,
            [10, 20, 30],
            False,
            [(10, "recommended"), (20, "offered"), (30, "offered")],
        ),
        (
            70,
            14,
            "enhanced",
            20,
            [10, 20, 30],
            False,
            [(10, "offered"), (20, "recommended"), (30, "offered")],
        ),
        (
            50,
            9,
            "enhanced",
            20,
            [10, 20, 30],
            False,
            [(10, "offered"), (20, "recommended"), (30, "offered")],
        ),
        (
            20,
            9,
            "priority",
            None,
            [10],
            True,
            [(10, "offered")],
        ),
        (
            50,
            18,
            "priority",
            None,
            [10],
            True,
            [(10, "offered")],
        ),
    ],
)
def test_action_package_matrix_preserves_sake_recommendations(
    recovery_value: float,
    day_strain_value: float,
    expected_level: str,
    default_minutes: int | None,
    choices: list[int],
    rest_is_option: bool,
    walk_states: list[tuple[int, str]],
) -> None:
    result = _calculate(
        recovery_rows=[_recovery_row(recovery_value)],
        day_strain_rows=[_day_strain_row(day_strain_value)],
    )

    assert result.public["status"] == "ok"
    assert result.public["level"] == expected_level
    assert result.public["routine_basis"] == "whoop_primary_signals"
    assert result.public["walk"] == {
        "default_minutes": default_minutes,
        "choices_minutes": choices,
        "rest_is_option": rest_is_option,
        "pace": "comfortable_conversational",
    }
    walk_actions = [
        action
        for action in result.public["actions"]
        if action["kind"] == "walk"
    ]
    assert [
        (action["duration_minutes"], action["state"])
        for action in walk_actions
    ] == walk_states
    _assert_common_actions_and_no_completion_tracking(result.public)


def test_insufficient_package_is_manual_optional_and_not_completion_tracking() -> None:
    result = _calculate(recovery_rows=[], day_strain_rows=[])

    assert result.public["status"] == "insufficient_data"
    assert result.public["level"] is None
    assert result.public["routine_basis"] == "manual_optional"
    assert result.public["walk"] == {
        "default_minutes": None,
        "choices_minutes": [10],
        "rest_is_option": True,
        "pace": "comfortable_conversational",
    }
    assert [
        (action["duration_minutes"], action["state"])
        for action in result.public["actions"]
        if action["kind"] == "walk"
    ] == [(10, "offered")]
    assert result.provenance == ()
    _assert_common_actions_and_no_completion_tracking(result.public)


def _assert_common_actions_and_no_completion_tracking(
    package: dict[str, Any],
) -> None:
    water = [
        action
        for action in package["actions"]
        if action["kind"] == "drink_water"
    ]
    sleep = [
        action
        for action in package["actions"]
        if action["kind"] == "sleep_preparation"
    ]
    assert water == [
        {
            "kind": "drink_water",
            "state": "recommended",
            "duration_minutes": None,
            "advance_minutes": None,
        }
    ]
    assert sleep == [
        {
            "kind": "sleep_preparation",
            "state": "recommended",
            "duration_minutes": None,
            "advance_minutes": 30,
        }
    ]
    forbidden = {
        "completed",
        "completed_at",
        "completion",
        "completion_status",
        "tracked",
    }
    assert forbidden.isdisjoint(package["walk"])
    for action in package["actions"]:
        assert forbidden.isdisjoint(action)
