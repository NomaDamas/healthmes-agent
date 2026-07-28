from __future__ import annotations

import hashlib

from healthmes.calendars.base import (
    CalendarEventIdentity,
    EventDraft,
    ensure_utc,
)
from healthmes.calendars.sleep_observation import ActualSleepObservation

ACTUAL_SLEEP_SUMMARY = "Oura 수면 세션"
LEGACY_ACTUAL_SLEEP_SUMMARY = "수면 (실제)"


def observation_fingerprint(observation: ActualSleepObservation) -> str:
    values = (
        observation.local_date.isoformat(),
        observation.provider,
        observation.source_key,
        ensure_utc(observation.start_at).isoformat(),
        ensure_utc(observation.end_at).isoformat(),
        str(observation.duration_minutes),
        str(observation.time_in_bed_minutes),
        str(observation.review_url),
        *(
            f"{ensure_utc(segment.start_at).isoformat()}:{ensure_utc(segment.end_at).isoformat()}"
            for segment in observation.segments
        ),
    )
    return hashlib.sha256("\x1f".join(values).encode()).hexdigest()


def event_draft(
    observation: ActualSleepObservation,
    identity: CalendarEventIdentity,
) -> EventDraft:
    return EventDraft(
        summary=ACTUAL_SLEEP_SUMMARY,
        start_at=observation.start_at,
        end_at=observation.end_at,
        description=description(observation),
        identity=identity,
    )


def description(observation: ActualSleepObservation) -> str:
    review_link = (
        f"\nReview or update in HealthMes: {observation.review_url}"
        if observation.review_url
        else ""
    )
    if observation.time_in_bed_minutes is None:
        return (
            "Oura bedtime window: unavailable\n"
            f"Actual sleep: {observation.duration_minutes} min\n"
            "Non-sleep within window: unavailable\n"
            f"Source: {observation.provider}"
            f"{review_link}"
        )
    non_sleep_minutes = (
        observation.time_in_bed_minutes - observation.duration_minutes
    )
    return (
        f"Oura bedtime window: {observation.time_in_bed_minutes} min\n"
        f"Actual sleep: {observation.duration_minutes} min\n"
        f"Non-sleep within window: {non_sleep_minutes} min\n"
        f"Source: {observation.provider}"
        f"{review_link}"
    )
