from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SleepCalendarAction(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    NOOP = "noop"


@dataclass(frozen=True, slots=True)
class SleepCalendarResult:
    action: SleepCalendarAction
    external_id: str
    observation_fingerprint: str
    external_ids: tuple[str, ...] = ()
    deleted_planned_external_ids: tuple[str, ...] = ()
    deleted_actual_external_ids: tuple[str, ...] = ()
    planned_sleep_cleanup_pending: int = 0
    invalidated_schedule_proposal_ids: tuple[str, ...] = ()


def created_sleep_result(
    external_id: str,
    fingerprint: str,
) -> SleepCalendarResult:
    return SleepCalendarResult(
        action=SleepCalendarAction.CREATED,
        external_id=external_id,
        observation_fingerprint=fingerprint,
    )


def updated_sleep_result(
    external_id: str,
    fingerprint: str,
) -> SleepCalendarResult:
    return SleepCalendarResult(
        action=SleepCalendarAction.UPDATED,
        external_id=external_id,
        observation_fingerprint=fingerprint,
    )
