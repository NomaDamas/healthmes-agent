from __future__ import annotations

import hashlib
import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from healthmes.calendars.approval import ApprovalCalendar
from healthmes.calendars.base import HealthmesEventKind, ensure_utc
from healthmes.calendars.sleep_observation import ActualSleepObservation
from healthmes.store.models import CalendarEventMirror


def redacted_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def capture_provider_state(
    session: Session,
    calendar: ApprovalCalendar,
    observation: ActualSleepObservation,
) -> dict[str, Any]:
    backend = calendar.backend
    actual = session.scalar(
        sa.select(CalendarEventMirror).where(
            CalendarEventMirror.calendar_source == backend.source,
            CalendarEventMirror.healthmes_source_key == observation.source_key,
        )
    )
    actual_state: dict[str, Any] | None = None
    if actual is not None:
        remote = backend.read_event(actual.external_id)
        actual_state = {
            "external_id": actual.external_id,
            "etag": remote.etag,
            "identity": redacted_digest(repr(remote.identity)),
        }

    planned_rows = session.scalars(
        sa.select(CalendarEventMirror).where(
            CalendarEventMirror.calendar_source == backend.source,
            CalendarEventMirror.is_agent_created.is_(True),
            CalendarEventMirror.healthmes_kind
            == HealthmesEventKind.PLANNED_SLEEP.value,
            CalendarEventMirror.start_at < ensure_utc(observation.end_at),
            CalendarEventMirror.end_at > ensure_utc(observation.start_at),
        )
    ).all()
    planned: list[dict[str, Any]] = []
    for row in planned_rows:
        remote = backend.read_event(row.external_id)
        planned.append(
            {
                "external_id": row.external_id,
                "etag": remote.etag,
                "identity": redacted_digest(repr(remote.identity)),
            }
        )
    planned.sort(key=lambda item: str(item["external_id"]))
    return {
        "target": calendar.target,
        "actual": actual_state,
        "planned": planned,
    }


def redacted_provider_guard(provider_state: dict[str, Any]) -> dict[str, Any]:
    target = str(provider_state["target"])
    actual = provider_state["actual"]
    planned = provider_state["planned"]
    actual_digest = None
    if isinstance(actual, dict):
        actual_digest = redacted_digest(json.dumps(actual, sort_keys=True))
    planned_digests = [
        redacted_digest(json.dumps(item, sort_keys=True))
        for item in planned
        if isinstance(item, dict)
    ]
    return {
        "target": redacted_digest(target),
        "actual": actual_digest,
        "planned": planned_digests,
    }
