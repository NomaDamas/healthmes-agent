from __future__ import annotations

import hashlib

from healthmes.calendars.base import (
    CalendarEventIdentity,
    EventDraft,
    ensure_utc,
)
from healthmes.calendars.sleep_observation import ActualSleepObservation


def observation_fingerprint(observation: ActualSleepObservation) -> str:
    values = (
        observation.local_date.isoformat(),
        observation.provider,
        observation.source_key,
        ensure_utc(observation.start_at).isoformat(),
        ensure_utc(observation.end_at).isoformat(),
        str(observation.duration_minutes),
        str(observation.time_in_bed_minutes),
    )
    return hashlib.sha256("\x1f".join(values).encode()).hexdigest()


def event_draft(
    observation: ActualSleepObservation,
    identity: CalendarEventIdentity,
) -> EventDraft:
    return EventDraft(
        summary="수면 (실제)",
        start_at=observation.start_at,
        end_at=observation.end_at,
        description=description(observation),
        identity=identity,
    )


def description(observation: ActualSleepObservation) -> str:
    time_in_bed = (
        f"{observation.time_in_bed_minutes} min"
        if observation.time_in_bed_minutes is not None
        else "unavailable"
    )
    return (
        f"Actual sleep: {observation.duration_minutes} min\n"
        f"Time in bed: {time_in_bed}\n"
        f"Source: {observation.provider}"
    )
