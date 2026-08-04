from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class SleepObservationNoOpReason(StrEnum):
    MISSING = "missing"
    STALE = "stale"
    INCOMPLETE = "incomplete"
    NAP_ONLY = "nap_only"
    AMBIGUOUS = "ambiguous"


class SleepSourcePayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str


class SleepSessionPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    start_time: datetime
    end_time: datetime
    duration_minutes: int | None = None
    is_nap: bool = False


class SleepSummaryPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: date
    source: SleepSourcePayload
    start_time: datetime | None = None
    end_time: datetime | None = None
    duration_minutes: int | None = None
    time_in_bed_minutes: int | None = None
    sessions: tuple[SleepSessionPayload, ...] | None = None


@dataclass(frozen=True, slots=True)
class ActualSleepObservation:
    local_date: date
    provider: str
    source_key: str
    start_at: datetime
    end_at: datetime
    duration_minutes: int
    time_in_bed_minutes: int | None


@dataclass(frozen=True, slots=True)
class SleepObservationNoOp:
    reason: SleepObservationNoOpReason


type SleepObservationResult = ActualSleepObservation | SleepObservationNoOp

ACTUAL_SLEEP_IDENTITY_SOURCE = "open-wearables"


def actual_sleep_source_key(local_date: date) -> str:
    return f"actual_sleep:{local_date.isoformat()}"


@dataclass(frozen=True, slots=True)
class _SleepCandidate:
    observation: ActualSleepObservation
    main_session_minutes: int


def select_actual_sleep(
    summaries: tuple[SleepSummaryPayload, ...],
    target_date: date,
) -> SleepObservationResult:
    if not summaries:
        return SleepObservationNoOp(reason=SleepObservationNoOpReason.MISSING)

    current = tuple(summary for summary in summaries if summary.date == target_date)
    if not current:
        return SleepObservationNoOp(reason=SleepObservationNoOpReason.STALE)

    candidates: list[_SleepCandidate] = []
    all_sessions_are_naps = True
    for summary in current:
        provider = summary.source.provider.strip()
        if not provider or summary.duration_minutes is None or summary.duration_minutes <= 0:
            all_sessions_are_naps = False
            continue
        if (
            summary.time_in_bed_minutes is None
            or summary.time_in_bed_minutes < summary.duration_minutes
        ):
            all_sessions_are_naps = False
            continue

        sessions = summary.sessions
        if sessions is None:
            all_sessions_are_naps = False
            start_at = summary.start_time
            end_at = summary.end_time
            if not _valid_span(start_at, end_at):
                continue
            assert start_at is not None
            assert end_at is not None
            main_session_minutes = _elapsed_minutes(start_at, end_at)
            candidates.append(
                _candidate(
                    summary,
                    provider=provider,
                    start_at=start_at,
                    end_at=end_at,
                    main_session_minutes=main_session_minutes,
                )
            )
            continue

        main_sessions = tuple(item for item in sessions if not item.is_nap)
        if not main_sessions:
            continue
        all_sessions_are_naps = False
        for item in main_sessions:
            if item.duration_minutes is None or item.duration_minutes <= 0:
                continue
            if not _valid_span(item.start_time, item.end_time):
                continue
            candidates.append(
                _candidate(
                    summary,
                    provider=provider,
                    start_at=item.start_time,
                    end_at=item.end_time,
                    main_session_minutes=_elapsed_minutes(
                        item.start_time,
                        item.end_time,
                    ),
                )
            )

    if not candidates:
        reason = (
            SleepObservationNoOpReason.NAP_ONLY
            if all_sessions_are_naps
            else SleepObservationNoOpReason.INCOMPLETE
        )
        return SleepObservationNoOp(reason=reason)

    longest_minutes = max(candidate.main_session_minutes for candidate in candidates)
    longest = tuple(
        candidate for candidate in candidates if candidate.main_session_minutes == longest_minutes
    )
    observations = {candidate.observation for candidate in longest}
    if len(observations) != 1:
        return SleepObservationNoOp(reason=SleepObservationNoOpReason.AMBIGUOUS)
    return observations.pop()


def _valid_span(start_at: datetime | None, end_at: datetime | None) -> bool:
    return (
        start_at is not None
        and end_at is not None
        and start_at.tzinfo is not None
        and end_at.tzinfo is not None
        and end_at.astimezone(UTC) > start_at.astimezone(UTC)
    )


def _elapsed_minutes(start_at: datetime, end_at: datetime) -> int:
    return int(
        (
            end_at.astimezone(UTC) - start_at.astimezone(UTC)
        ).total_seconds()
        // 60
    )


def _candidate(
    summary: SleepSummaryPayload,
    *,
    provider: str,
    start_at: datetime,
    end_at: datetime,
    main_session_minutes: int,
) -> _SleepCandidate:
    assert summary.duration_minutes is not None
    return _SleepCandidate(
        observation=ActualSleepObservation(
            local_date=summary.date,
            provider=provider,
            source_key=actual_sleep_source_key(summary.date),
            start_at=start_at,
            end_at=end_at,
            duration_minutes=summary.duration_minutes,
            time_in_bed_minutes=summary.time_in_bed_minutes,
        ),
        main_session_minutes=main_session_minutes,
    )
