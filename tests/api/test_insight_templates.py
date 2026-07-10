"""Unit tests for the pure insight-template computations (threshold gates)."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from healthmes.api.insight_templates import (
    MIN_STRESS_SAMPLES,
    StressSample,
    WorkoutEvent,
    compute_activity_type_vs_stress,
    compute_stress_by_calendar_keyword,
    compute_stress_by_hour,
    compute_stress_by_weekday,
)


@dataclass
class FakeEvent:
    """Bare CalendarEventLike (naive datetimes, like sqlite round-trips)."""

    summary: str | None
    start_at: datetime
    end_at: datetime


def _samples(count: int, *, hours: tuple[int, ...] = (9, 14, 20)) -> list[StressSample]:
    base = datetime(2026, 7, 6, tzinfo=UTC)
    return [
        StressSample(
            timestamp=base + timedelta(days=i % 3, hours=hours[i % len(hours)], minutes=i),
            value=50.0,
        )
        for i in range(count)
    ]


def test_hour_template_requires_min_samples():
    assert compute_stress_by_hour(_samples(MIN_STRESS_SAMPLES - 1)) is None
    assert compute_stress_by_hour(_samples(MIN_STRESS_SAMPLES)) is not None


def test_hour_template_requires_distinct_hours():
    single_hour = _samples(MIN_STRESS_SAMPLES, hours=(9,))
    assert compute_stress_by_hour(single_hour) is None


def test_weekday_template_requires_two_weekdays():
    base = datetime(2026, 7, 6, tzinfo=UTC)  # all samples on one Monday
    same_day = [
        StressSample(timestamp=base + timedelta(minutes=10 * i), value=40.0)
        for i in range(MIN_STRESS_SAMPLES)
    ]
    assert compute_stress_by_weekday(same_day) is None
    assert compute_stress_by_weekday(_samples(MIN_STRESS_SAMPLES)) is not None


def test_keyword_template_needs_repeated_keyword_and_handles_naive_event_times():
    samples = _samples(MIN_STRESS_SAMPLES)
    one_off_events = [
        FakeEvent("Dentist appointment", datetime(2026, 7, 6, 9), datetime(2026, 7, 6, 10)),
        FakeEvent("Focus block", datetime(2026, 7, 7, 9), datetime(2026, 7, 7, 10)),
    ]
    assert compute_stress_by_calendar_keyword(samples, one_off_events) is None

    # Same keyword on two events (naive datetimes are treated as UTC).
    repeated = [
        FakeEvent("Standup meeting", datetime(2026, 7, 6, 9, 0), datetime(2026, 7, 6, 11, 0)),
        FakeEvent("Review meeting", datetime(2026, 7, 7, 9, 0), datetime(2026, 7, 7, 11, 0)),
    ]
    result = compute_stress_by_calendar_keyword(samples, repeated)
    assert result is not None
    assert result.evidence["top_keyword"] == "meeting"


def test_activity_template_requires_two_qualifying_workouts_per_type():
    samples = [
        StressSample(datetime(2026, 7, 6, 15, 0, tzinfo=UTC), 60.0),
        StressSample(datetime(2026, 7, 6, 15, 30, tzinfo=UTC), 40.0),
        StressSample(datetime(2026, 7, 6, 19, 0, tzinfo=UTC), 30.0),
        StressSample(datetime(2026, 7, 6, 19, 30, tzinfo=UTC), 30.0),
    ] + _samples(MIN_STRESS_SAMPLES)

    lone_workout = [
        WorkoutEvent(
            "running",
            datetime(2026, 7, 6, 16, 0, tzinfo=UTC),
            datetime(2026, 7, 6, 18, 0, tzinfo=UTC),
        )
    ]
    assert compute_activity_type_vs_stress(samples, lone_workout) is None


def test_activity_template_returns_none_without_workouts():
    assert compute_activity_type_vs_stress(_samples(MIN_STRESS_SAMPLES), []) is None
