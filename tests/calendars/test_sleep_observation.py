from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from healthmes.calendars.sleep_observation import (
    ActualSleepObservation,
    SleepObservationNoOp,
    SleepObservationNoOpReason,
    SleepSessionPayload,
    SleepSourcePayload,
    SleepSummaryPayload,
    actual_sleep_source_key,
    select_actual_sleep,
)
from healthmes.calendars.sleep_source import select_actual_sleep_rows

TARGET_DATE = date(2026, 7, 26)


def session(
    start_hour: int,
    end_hour: int,
    duration_minutes: int,
    *,
    is_nap: bool = False,
) -> SleepSessionPayload:
    return SleepSessionPayload(
        start_time=datetime(2026, 7, 26, start_hour, tzinfo=UTC),
        end_time=datetime(2026, 7, 26, end_hour, tzinfo=UTC),
        duration_minutes=duration_minutes,
        is_nap=is_nap,
    )


def summary(
    *,
    provider: str = "oura",
    local_date: date = TARGET_DATE,
    sessions: tuple[SleepSessionPayload, ...] | None = None,
    duration_minutes: int | None = 420,
    time_in_bed_minutes: int | None = 480,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> SleepSummaryPayload:
    return SleepSummaryPayload(
        date=local_date,
        source=SleepSourcePayload(provider=provider),
        start_time=start_time,
        end_time=end_time,
        duration_minutes=duration_minutes,
        time_in_bed_minutes=time_in_bed_minutes,
        sessions=sessions,
    )


def test_selects_longest_non_nap_session_and_preserves_aggregate_duration() -> None:
    # Given
    short_main = session(1, 2, 60)
    longest_main = session(3, 9, 360)
    nap = session(14, 15, 60, is_nap=True)

    # When
    result = select_actual_sleep(
        (
            summary(
                sessions=(short_main, longest_main, nap),
                duration_minutes=420,
                time_in_bed_minutes=480,
            ),
        ),
        TARGET_DATE,
    )

    # Then
    assert result == ActualSleepObservation(
        local_date=TARGET_DATE,
        provider="oura",
        source_key=actual_sleep_source_key(TARGET_DATE),
        start_at=longest_main.start_time,
        end_at=longest_main.end_time,
        duration_minutes=420,
        time_in_bed_minutes=480,
    )


def test_selects_longest_candidate_across_provider_summaries() -> None:
    # Given
    shorter = summary(provider="garmin", sessions=(session(1, 5, 240),))
    longer = summary(provider="oura", sessions=(session(2, 9, 420),))

    # When
    result = select_actual_sleep((shorter, longer), TARGET_DATE)

    # Then
    assert isinstance(result, ActualSleepObservation)
    assert result.provider == "oura"
    assert result.start_at == datetime(2026, 7, 26, 2, tzinfo=UTC)


def test_malformed_non_mapping_history_does_not_hide_target_sleep() -> None:
    target = summary(
        start_time=datetime(2026, 7, 25, 23, tzinfo=UTC),
        end_time=datetime(2026, 7, 26, 7, tzinfo=UTC),
    )

    result = select_actual_sleep_rows(
        ("malformed historical row", target.model_dump()),
        TARGET_DATE,
    )

    assert isinstance(result, ActualSleepObservation)
    assert result.local_date == TARGET_DATE


@pytest.mark.parametrize(
    ("summaries", "reason"),
    [
        ((), SleepObservationNoOpReason.MISSING),
        (
            (summary(local_date=date(2026, 7, 25), sessions=(session(1, 8, 420),)),),
            SleepObservationNoOpReason.STALE,
        ),
        (
            (summary(sessions=(session(13, 14, 60, is_nap=True),)),),
            SleepObservationNoOpReason.NAP_ONLY,
        ),
        (
            (summary(sessions=(session(1, 8, 420),), duration_minutes=None),),
            SleepObservationNoOpReason.INCOMPLETE,
        ),
    ],
)
def test_returns_explicit_noop_for_unusable_data(
    summaries: tuple[SleepSummaryPayload, ...],
    reason: SleepObservationNoOpReason,
) -> None:
    # Given / When
    result = select_actual_sleep(summaries, TARGET_DATE)

    # Then
    assert result == SleepObservationNoOp(reason=reason)


def test_returns_incomplete_when_main_session_has_naive_time() -> None:
    # Given
    naive = SleepSessionPayload(
        start_time=datetime(2026, 7, 25, 23),
        end_time=datetime(2026, 7, 26, 7),
        duration_minutes=420,
        is_nap=False,
    )

    # When
    result = select_actual_sleep((summary(sessions=(naive,)),), TARGET_DATE)

    # Then
    assert result == SleepObservationNoOp(reason=SleepObservationNoOpReason.INCOMPLETE)


def test_returns_ambiguous_when_longest_candidates_tie_on_different_spans() -> None:
    # Given
    oura = summary(provider="oura", sessions=(session(1, 8, 420),))
    garmin = summary(provider="garmin", sessions=(session(2, 9, 420),))

    # When
    result = select_actual_sleep((oura, garmin), TARGET_DATE)

    # Then
    assert result == SleepObservationNoOp(reason=SleepObservationNoOpReason.AMBIGUOUS)


def test_uses_top_level_span_when_sessions_are_unavailable() -> None:
    # Given
    start_at = datetime(2026, 7, 25, 23, tzinfo=UTC)
    end_at = datetime(2026, 7, 26, 7, tzinfo=UTC)

    # When
    result = select_actual_sleep(
        (summary(sessions=None, start_time=start_at, end_time=end_at),),
        TARGET_DATE,
    )

    # Then
    assert isinstance(result, ActualSleepObservation)
    assert result.start_at == start_at
    assert result.end_at == end_at


def test_normalizes_open_wearables_summary_payload() -> None:
    # Given / When
    payload = SleepSummaryPayload.model_validate(
        {
            "date": "2026-07-26",
            "source": {"provider": "oura", "record_count": 1},
            "start_time": "2026-07-25T23:00:00Z",
            "end_time": "2026-07-26T07:00:00Z",
            "duration_minutes": 420,
            "time_in_bed_minutes": 480,
            "sessions": [
                {
                    "start_time": "2026-07-25T23:00:00Z",
                    "end_time": "2026-07-26T07:00:00Z",
                    "duration_minutes": 420,
                    "is_nap": False,
                    "zone_offset": "+00:00",
                }
            ],
        }
    )

    # Then
    assert payload.date == TARGET_DATE
    assert payload.source.provider == "oura"
    assert payload.sessions is not None
    assert payload.sessions[0].start_time == datetime(2026, 7, 25, 23, tzinfo=UTC)


def test_spring_forward_candidate_duration_uses_utc_elapsed_time() -> None:
    timezone = ZoneInfo("America/New_York")
    spring_forward = SleepSessionPayload(
        start_time=datetime(2026, 3, 8, 0, 30, tzinfo=timezone),
        end_time=datetime(2026, 3, 8, 4, 30, tzinfo=timezone),
        duration_minutes=240,
        is_nap=False,
    )
    longer_real_session = SleepSessionPayload(
        start_time=datetime(2026, 3, 8, 6, 0, tzinfo=UTC),
        end_time=datetime(2026, 3, 8, 9, 20, tzinfo=UTC),
        duration_minutes=200,
        is_nap=False,
    )
    target = date(2026, 3, 8)

    result = select_actual_sleep(
        (
            summary(
                provider="oura",
                local_date=target,
                sessions=(spring_forward,),
                duration_minutes=180,
                time_in_bed_minutes=240,
            ),
            summary(
                provider="garmin",
                local_date=target,
                sessions=(longer_real_session,),
                duration_minutes=200,
                time_in_bed_minutes=220,
            ),
        ),
        target,
    )

    assert isinstance(result, ActualSleepObservation)
    assert result.provider == "garmin"


def test_fall_back_fold_accepts_positive_utc_span() -> None:
    timezone = ZoneInfo("America/New_York")
    folded = SleepSessionPayload(
        start_time=datetime(2026, 11, 1, 1, 30, tzinfo=timezone, fold=0),
        end_time=datetime(2026, 11, 1, 1, 15, tzinfo=timezone, fold=1),
        duration_minutes=45,
        is_nap=False,
    )
    target = date(2026, 11, 1)

    result = select_actual_sleep(
        (
            summary(
                local_date=target,
                sessions=(folded,),
                duration_minutes=45,
                time_in_bed_minutes=60,
            ),
        ),
        target,
    )

    assert isinstance(result, ActualSleepObservation)
    assert result.start_at.astimezone(UTC) < result.end_at.astimezone(UTC)
