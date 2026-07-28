from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any, Protocol

from pydantic import ValidationError

from healthmes.calendars.sleep_observation import (
    ActualSleepObservation,
    SleepObservationNoOp,
    SleepObservationNoOpReason,
    SleepSummaryPayload,
    select_actual_sleep,
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
) -> ActualSleepObservation | SleepObservationNoOp:
    rows = await reader.collect_sleep_summaries(
        user_id,
        target_date.isoformat(),
        (target_date + timedelta(days=1)).isoformat(),
    )
    try:
        summaries = tuple(SleepSummaryPayload.model_validate(row) for row in rows)
    except ValidationError:
        return SleepObservationNoOp(reason=SleepObservationNoOpReason.INCOMPLETE)
    return select_actual_sleep(summaries, target_date)
