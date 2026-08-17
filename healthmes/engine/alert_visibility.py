"""Shared visibility contract for delivered and app-poll wellness alerts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.store import (
    DecisionRecord,
    RetentionPolicy,
    TriggerEvent,
)

APP_AVAILABLE_STATE = "app_available"
APP_POLL_CHANNEL = "app_poll"
DELIVERED_STATE = "delivered"
APP_AVAILABLE_DELIVERY_STATE = "app_available"
EXPIRED_STATE = "expired"
DEFAULT_ALERT_RETENTION_DAYS = 7

AlertDeliveryState = Literal["delivered", "app_available"]


def alert_delivery_state(
    event: TriggerEvent,
) -> AlertDeliveryState | None:
    """Return the honest user-visible delivery state, if any."""

    payload = event.payload
    if isinstance(payload, dict):
        push = payload.get("push")
        if (
            isinstance(push, dict)
            and push.get("state") == EXPIRED_STATE
        ):
            return None
    if event.alert_sent:
        return DELIVERED_STATE
    if not isinstance(payload, dict):
        return None
    message = payload.get("message")
    push = payload.get("push")
    if (
        isinstance(message, str)
        and bool(message.strip())
        and isinstance(push, dict)
        and push.get("sent") is False
        and push.get("state") == APP_AVAILABLE_STATE
        and push.get("channel") == APP_POLL_CHANNEL
    ):
        return APP_AVAILABLE_DELIVERY_STATE
    return None


def is_user_visible_alert(
    session: Session,
    event: TriggerEvent,
    *,
    now: datetime,
) -> bool:
    """Return whether shipped app APIs may display this trigger event."""

    if alert_delivery_state(event) is None:
        return False
    current = _as_utc(now)
    alert_deadline = _alert_retention_deadline(session, event)
    if alert_deadline is not None and alert_deadline <= current:
        return False
    payload = event.payload
    return not (
        isinstance(payload, dict)
        and isinstance(payload.get("message"), str)
        and _linked_decision_is_expired(
            session,
            event,
            now=current,
        )
    )


def expire_trigger_event_answers(
    session: Session,
    *,
    now: datetime,
    dry_run: bool = False,
) -> int:
    """Remove retained LLM answers once alert or decision retention expires."""

    current = _as_utc(now)
    candidates = [
        event
        for event in session.scalars(select(TriggerEvent))
        if isinstance(event.payload, dict)
        and isinstance(event.payload.get("message"), str)
        and trigger_answer_is_expired(
            session,
            event,
            now=current,
        )
    ]
    if dry_run:
        return len(candidates)
    for event in candidates:
        payload = dict(event.payload or {})
        payload.pop("message", None)
        payload.pop("decision", None)
        payload.pop("decision_record_id", None)
        payload["push"] = {
            "state": EXPIRED_STATE,
            "expired_at": current.isoformat(),
        }
        event.payload = payload
    session.flush()
    return len(candidates)


def trigger_answer_is_expired(
    session: Session,
    event: TriggerEvent,
    *,
    now: datetime,
) -> bool:
    current = _as_utc(now)
    alert_deadline = _alert_retention_deadline(
        session,
        event,
    )
    if alert_deadline is not None and alert_deadline <= current:
        return True

    return _linked_decision_is_expired(
        session,
        event,
        now=current,
    )


def _linked_decision_is_expired(
    session: Session,
    event: TriggerEvent,
    *,
    now: datetime,
) -> bool:
    record, expected_record = _linked_decision_record(
        session,
        event,
    )
    if expected_record and record is None:
        return True
    return (
        record is not None
        and record.expires_at is not None
        and _as_utc(record.expires_at) <= _as_utc(now)
    )


def _alert_retention_deadline(
    session: Session,
    event: TriggerEvent,
) -> datetime | None:
    policy = session.scalar(
        select(RetentionPolicy).where(
            RetentionPolicy.data_class == "alert"
        )
    )
    if policy is None:
        retention_days = DEFAULT_ALERT_RETENTION_DAYS
        enabled = True
    else:
        retention_days = policy.retention_days
        enabled = policy.enabled
    if not enabled or retention_days is None:
        return None
    return _as_utc(event.fired_at) + timedelta(
        days=retention_days
    )


def _linked_decision_record(
    session: Session,
    event: TriggerEvent,
) -> tuple[DecisionRecord | None, bool]:
    payload = event.payload if isinstance(event.payload, dict) else {}
    raw_record_id = payload.get("decision_record_id")
    if raw_record_id is None:
        decision = payload.get("decision")
        if isinstance(decision, dict):
            raw_record_id = decision.get("record_id")
    if isinstance(raw_record_id, str):
        try:
            record_id = uuid.UUID(raw_record_id)
        except ValueError:
            return None, True
        return session.get(DecisionRecord, record_id), True
    record = session.scalar(
        select(DecisionRecord)
        .where(DecisionRecord.trigger_event_id == event.id)
        .limit(1)
    )
    return record, record is not None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
