"""Shared visibility contract for delivered and app-poll wellness alerts."""

from __future__ import annotations

from typing import Literal

from healthmes.store import TriggerEvent

APP_AVAILABLE_STATE = "available"
APP_POLL_CHANNEL = "app_poll"
DELIVERED_STATE = "delivered"
APP_AVAILABLE_DELIVERY_STATE = "app_available"

AlertDeliveryState = Literal["delivered", "app_available"]


def alert_delivery_state(
    event: TriggerEvent,
) -> AlertDeliveryState | None:
    """Return the honest user-visible delivery state, if any."""

    if event.alert_sent:
        return DELIVERED_STATE
    payload = event.payload
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


def is_user_visible_alert(event: TriggerEvent) -> bool:
    """Return whether shipped app APIs may display this trigger event."""

    return alert_delivery_state(event) is not None
