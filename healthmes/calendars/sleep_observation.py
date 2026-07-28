from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
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


class SleepStageIntervalPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    stage: str
    start_time: datetime
    end_time: datetime


class DetailedSleepSessionPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    start_time: datetime
    end_time: datetime
    is_nap: bool = False
    sleep_stage_intervals: tuple[SleepStageIntervalPayload, ...] | None = None


@dataclass(frozen=True, slots=True)
class SleepSegment:
    start_at: datetime
    end_at: datetime


@dataclass(frozen=True, slots=True)
class ActualSleepObservation:
    local_date: date
    provider: str
    source_key: str
    start_at: datetime
    end_at: datetime
    duration_minutes: int
    time_in_bed_minutes: int | None
    segments: tuple[SleepSegment, ...] = ()
    review_url: str | None = None


@dataclass(frozen=True, slots=True)
class SleepObservationNoOp:
    reason: SleepObservationNoOpReason


type SleepObservationResult = ActualSleepObservation | SleepObservationNoOp


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
            main_session_minutes = int((end_at - start_at).total_seconds() // 60)
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
                    main_session_minutes=item.duration_minutes,
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
        and end_at > start_at
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
            source_key=f"{provider}:{summary.date.isoformat()}",
            start_at=start_at,
            end_at=end_at,
            duration_minutes=summary.duration_minutes,
            time_in_bed_minutes=summary.time_in_bed_minutes,
        ),
        main_session_minutes=main_session_minutes,
    )


def split_observation_at_awake_intervals(
    observation: ActualSleepObservation,
    sessions: tuple[DetailedSleepSessionPayload, ...],
) -> ActualSleepObservation:
    matching = tuple(
        session
        for session in sessions
        if not session.is_nap
        and session.sleep_stage_intervals
        and session.start_time == observation.start_at
        and session.end_time == observation.end_at
    )
    if len(matching) != 1:
        return observation

    segments: list[SleepSegment] = []
    for interval in sorted(
        matching[0].sleep_stage_intervals or (),
        key=lambda item: item.start_time,
    ):
        if interval.stage.lower() not in {"deep", "light", "rem"}:
            continue
        start_at = max(interval.start_time, observation.start_at)
        end_at = min(interval.end_time, observation.end_at)
        if end_at <= start_at:
            continue
        if segments and start_at <= segments[-1].end_at:
            segments[-1] = SleepSegment(
                start_at=segments[-1].start_at,
                end_at=max(segments[-1].end_at, end_at),
            )
        else:
            segments.append(SleepSegment(start_at=start_at, end_at=end_at))

    return replace(observation, segments=tuple(segments)) if segments else observation


def calendar_observations(
    observation: ActualSleepObservation,
) -> tuple[ActualSleepObservation, ...]:
    if not observation.segments:
        return (observation,)
    return tuple(
        replace(
            observation,
            source_key=(
                observation.source_key
                if index == 0
                else f"{observation.source_key}:segment:{index + 1}"
            ),
            start_at=segment.start_at,
            end_at=segment.end_at,
            duration_minutes=max(
                1,
                int((segment.end_at - segment.start_at).total_seconds() // 60),
            ),
            time_in_bed_minutes=max(
                1,
                int((segment.end_at - segment.start_at).total_seconds() // 60),
            ),
            segments=(),
        )
        for index, segment in enumerate(observation.segments)
        if segment.end_at > segment.start_at
    )
