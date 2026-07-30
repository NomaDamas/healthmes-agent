from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, timedelta
from typing import Any, Protocol

from pydantic import ValidationError

from healthmes.calendars.sleep_observation import (
    ActualSleepObservation,
    DetailedSleepSessionPayload,
    SleepObservationNoOp,
    SleepObservationNoOpReason,
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
) -> ActualSleepObservation | SleepObservationNoOp:
    rows = await reader.collect_sleep_summaries(
        user_id,
        target_date.isoformat(),
        (target_date + timedelta(days=1)).isoformat(),
    )
    selected = select_actual_sleep_rows(rows, target_date)
    if isinstance(selected, SleepObservationNoOp):
        return selected
    if review_base_url is not None:
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
    try:
        sessions = tuple(
            DetailedSleepSessionPayload.model_validate(row) for row in session_rows
        )
    except ValidationError:
        return selected
    return split_observation_at_awake_intervals(selected, sessions)


def select_actual_sleep_rows(
    rows: Sequence[Mapping[str, Any]],
    target_date: date,
) -> ActualSleepObservation | SleepObservationNoOp:
    try:
        summaries = tuple(SleepSummaryPayload.model_validate(row) for row in rows)
    except ValidationError:
        return SleepObservationNoOp(reason=SleepObservationNoOpReason.INCOMPLETE)
    return select_actual_sleep(summaries, target_date)
