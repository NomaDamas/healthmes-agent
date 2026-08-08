from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date, timedelta
from typing import Any, Protocol

from pydantic import ValidationError

from healthmes.calendars.sleep_observation import (
    ActualSleepObservation,
    DetailedSleepSessionPayload,
    SleepObservationNoOp,
    SleepObservationNoOpReason,
    SleepStageIntervalPayload,
    SleepSummaryPayload,
    select_actual_sleep,
    split_observation_at_awake_intervals,
)


class SleepSummaryReader(Protocol):
    async def collect_sleep_summaries(
        self,
        user_id: str,
        start_date: str,
        end_date: str,
    ) -> Sequence[Mapping[str, Any]]: ...


async def read_actual_sleep(
    reader: SleepSummaryReader,
    user_id: str,
    target_date: date,
    *,
    review_base_url: str | None = None,
    review_url_builder: Callable[[date], str] | None = None,
) -> ActualSleepObservation | SleepObservationNoOp:
    rows = await reader.collect_sleep_summaries(
        user_id,
        target_date.isoformat(),
        (target_date + timedelta(days=1)).isoformat(),
    )
    selected = select_actual_sleep_rows(rows, target_date)
    if isinstance(selected, SleepObservationNoOp):
        return selected
    if review_url_builder is not None:
        selected = replace(
            selected,
            review_url=review_url_builder(target_date),
        )
    elif review_base_url is not None:
        selected = replace(
            selected,
            review_url=(
                f"{review_base_url.rstrip('/')}/sleep?date={target_date.isoformat()}"
            ),
        )
    collect_sessions = getattr(reader, "collect_sleep_sessions", None)
    if collect_sessions is None:
        return selected
    session_rows = await collect_sessions(
        user_id,
        target_date.isoformat(),
        (target_date + timedelta(days=1)).isoformat(),
    )
    sessions = _valid_detailed_sleep_sessions(session_rows)
    if not sessions:
        return selected
    return split_observation_at_awake_intervals(selected, sessions)


def _valid_detailed_sleep_sessions(
    rows: object,
) -> tuple[DetailedSleepSessionPayload, ...]:
    if not isinstance(rows, Sequence) or isinstance(rows, str | bytes | bytearray):
        return ()
    sessions: list[DetailedSleepSessionPayload] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        normalized = dict(row)
        interval_rows = row.get("sleep_stage_intervals")
        if isinstance(interval_rows, Sequence) and not isinstance(
            interval_rows,
            str | bytes | bytearray,
        ):
            valid_intervals = []
            for interval in interval_rows:
                try:
                    valid_intervals.append(
                        SleepStageIntervalPayload.model_validate(interval)
                    )
                except ValidationError:
                    continue
            normalized["sleep_stage_intervals"] = valid_intervals
        elif interval_rows is not None:
            normalized["sleep_stage_intervals"] = []
        try:
            sessions.append(DetailedSleepSessionPayload.model_validate(normalized))
        except ValidationError:
            continue
    return tuple(sessions)


def select_actual_sleep_rows(
    rows: Sequence[object],
    target_date: date,
) -> ActualSleepObservation | SleepObservationNoOp:
    summaries: list[SleepSummaryPayload] = []
    invalid_target_row = False
    for row in rows:
        try:
            summaries.append(SleepSummaryPayload.model_validate(row))
        except ValidationError:
            if isinstance(row, Mapping):
                invalid_target_row |= row.get("date") in (
                    target_date,
                    target_date.isoformat(),
                )
    selected = select_actual_sleep(tuple(summaries), target_date)
    if (
        invalid_target_row
        and isinstance(selected, SleepObservationNoOp)
        and selected.reason in {
            SleepObservationNoOpReason.MISSING,
            SleepObservationNoOpReason.STALE,
        }
    ):
        return SleepObservationNoOp(reason=SleepObservationNoOpReason.INCOMPLETE)
    return selected
