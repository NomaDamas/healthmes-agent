"""Durable one-time confirmation gate for owner-authored nutrition facts."""

from __future__ import annotations

import hmac
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.calendars.adjustments_logic import digest_reply_handle
from healthmes.nutrition.ledger_lock import lock_nutrition_ledger
from healthmes.nutrition.operation_integrity import (
    is_sha256_digest,
    result_payload_digest,
)
from healthmes.store import WellnessEvent

CONFIRMATION_EVENT = "nutrition.pending-confirmation.v1"
CONFIRMATION_PROVIDER = "nutrition-confirmation-gate"
CONFIRMATION_SCHEMA = "nutrition-confirmation-gate-v1"
CONFIRMATION_TOMBSTONE_EVENT = "nutrition.confirmation-tombstone.v1"
CONFIRMATION_TOMBSTONE_PROVIDER = "nutrition-confirmation-tombstone"
CONFIRMATION_TOMBSTONE_SCHEMA = "nutrition-confirmation-tombstone-v1"
CONFIRMATION_TTL = timedelta(minutes=60)
TERMINAL_STATES = frozenset({"resolved", "cancelled", "invalidated"})
_OPAQUE_TERMINAL_FIELD = "opaque_terminal"


class NutritionConfirmationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PendingNutritionConfirmation:
    event: WellnessEvent
    action_id: uuid.UUID
    action: str
    snapshot: dict[str, Any]
    snapshot_sha256: str
    summary: dict[str, Any]
    reply_handle_digest: str
    state: str
    expires_at: datetime
    result: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class PreparedNutritionConfirmation:
    confirmation: PendingNutritionConfirmation
    reply_handle: str | None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _confirmation_handle(
    secret: str,
    *,
    action_id: uuid.UUID,
    snapshot_sha256: str,
) -> str:
    material = f"{action_id}:{snapshot_sha256}".encode()
    return hmac.new(secret.encode("utf-8"), material, "sha256").hexdigest()[:32]


def _snapshot_digest(action: str, snapshot: Mapping[str, Any]) -> str:
    return result_payload_digest(
        {
            "action": action,
            "snapshot": dict(snapshot),
        }
    )


def _request_digest(snapshot: Mapping[str, Any]) -> str:
    arguments = snapshot.get("arguments")
    return result_payload_digest(
        {
            "arguments": dict(arguments) if isinstance(arguments, dict) else None,
        }
    )


def _terminal_tombstone(
    session: Session,
    action_id: uuid.UUID,
) -> WellnessEvent | None:
    return session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == CONFIRMATION_TOMBSTONE_EVENT,
            WellnessEvent.source_provider == CONFIRMATION_TOMBSTONE_PROVIDER,
            WellnessEvent.source_record_id == str(action_id),
        )
    )


def _validated_terminal_tombstone_payload(
    event: WellnessEvent,
    *,
    action_id: uuid.UUID,
) -> dict[str, Any]:
    payload = event.payload
    if (
        event.event_type != CONFIRMATION_TOMBSTONE_EVENT
        or event.source_provider != CONFIRMATION_TOMBSTONE_PROVIDER
        or event.source_record_id != str(action_id)
        or not isinstance(payload, dict)
        or payload.get("schema_version") != CONFIRMATION_TOMBSTONE_SCHEMA
        or payload.get("action_id") != str(action_id)
        or not is_sha256_digest(payload.get("request_sha256"))
        or payload.get("state") not in TERMINAL_STATES
        or not is_sha256_digest(payload.get("result_sha256"))
    ):
        raise NutritionConfirmationError("stored nutrition confirmation tombstone is malformed")
    return payload


def _validate_terminal_tombstone(
    event: WellnessEvent,
    *,
    action_id: uuid.UUID,
    action: str,
    request_sha256: str,
    state: str | None = None,
    result_sha256: str | None = None,
) -> None:
    payload = _validated_terminal_tombstone_payload(
        event,
        action_id=action_id,
    )
    if payload.get(_OPAQUE_TERMINAL_FIELD) is True:
        return
    if payload.get("action") != action or payload.get("request_sha256") != request_sha256:
        raise NutritionConfirmationError("stored nutrition confirmation tombstone is malformed")
    if state is not None and payload.get("state") != state:
        raise NutritionConfirmationError(
            "nutrition confirmation was already consumed with a different result"
        )
    if result_sha256 is not None and payload.get("result_sha256") != result_sha256:
        raise NutritionConfirmationError(
            "nutrition confirmation was already consumed with a different result"
        )


def _ensure_terminal_tombstone(
    session: Session,
    confirmation: PendingNutritionConfirmation,
    *,
    state: str,
    result: Mapping[str, Any],
    completed_at: datetime,
) -> None:
    request_sha256 = _request_digest(confirmation.snapshot)
    result_sha256 = result_payload_digest(result)
    existing = _terminal_tombstone(session, confirmation.action_id)
    if existing is not None:
        _validate_terminal_tombstone(
            existing,
            action_id=confirmation.action_id,
            action=confirmation.action,
            request_sha256=request_sha256,
            state=state,
            result_sha256=result_sha256,
        )
        return
    session.add(
        WellnessEvent(
            event_type=CONFIRMATION_TOMBSTONE_EVENT,
            schema_version=1,
            observed_at=completed_at,
            recorded_at=completed_at,
            timezone=None,
            source_provider=CONFIRMATION_TOMBSTONE_PROVIDER,
            source_device=None,
            source_record_id=str(confirmation.action_id),
            capture_method="system",
            quality_flags=None,
            confidence=None,
            coverage=None,
            sensitivity="wellness",
            consent_scope="personal",
            retention_policy_id=None,
            expires_at=None,
            payload={
                "schema_version": CONFIRMATION_TOMBSTONE_SCHEMA,
                "action_id": str(confirmation.action_id),
                "action": confirmation.action,
                "request_sha256": request_sha256,
                "state": state,
                "result_sha256": result_sha256,
                "completed_at": completed_at.isoformat(),
            },
            raw_object_id=None,
            derived_from=None,
        )
    )
    session.flush()


def _ensure_invalid_terminal_tombstone(
    session: Session,
    event: WellnessEvent,
    *,
    completed_at: datetime,
) -> None:
    try:
        action_id = uuid.UUID(event.source_record_id)
    except (TypeError, ValueError):
        return
    existing = _terminal_tombstone(session, action_id)
    if existing is not None:
        _validated_terminal_tombstone_payload(
            existing,
            action_id=action_id,
        )
        return
    request_sha256 = result_payload_digest(
        {
            "action_id": str(action_id),
            "reason": "stored_confirmation_malformed",
        }
    )
    result_sha256 = result_payload_digest(
        {
            "status": "invalidated",
            "operation_id": str(action_id),
            "reason": "stored_confirmation_malformed",
        }
    )
    session.add(
        WellnessEvent(
            event_type=CONFIRMATION_TOMBSTONE_EVENT,
            schema_version=1,
            observed_at=completed_at,
            recorded_at=completed_at,
            timezone=None,
            source_provider=CONFIRMATION_TOMBSTONE_PROVIDER,
            source_device=None,
            source_record_id=str(action_id),
            capture_method="system",
            quality_flags=None,
            confidence=None,
            coverage=None,
            sensitivity="wellness",
            consent_scope="personal",
            retention_policy_id=None,
            expires_at=None,
            payload={
                "schema_version": CONFIRMATION_TOMBSTONE_SCHEMA,
                "action_id": str(action_id),
                "action": "stored_confirmation_malformed",
                "request_sha256": request_sha256,
                "state": "invalidated",
                "result_sha256": result_sha256,
                "completed_at": completed_at.isoformat(),
                _OPAQUE_TERMINAL_FIELD: True,
            },
            raw_object_id=None,
            derived_from=None,
        )
    )
    session.flush()


def _stored_confirmation(event: WellnessEvent) -> PendingNutritionConfirmation:
    payload = event.payload
    if (
        event.event_type != CONFIRMATION_EVENT
        or event.source_provider != CONFIRMATION_PROVIDER
        or not isinstance(payload, dict)
        or payload.get("schema_version") != CONFIRMATION_SCHEMA
    ):
        raise NutritionConfirmationError("stored nutrition confirmation is malformed")
    try:
        action_id = uuid.UUID(str(payload["action_id"]))
        action = payload["action"]
        snapshot = payload["snapshot"]
        snapshot_sha256 = payload["snapshot_sha256"]
        summary = payload["summary"]
        reply_handle_digest = payload["reply_handle_digest"]
        state = payload["state"]
        expires_at = datetime.fromisoformat(str(payload["expires_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise NutritionConfirmationError("stored nutrition confirmation is malformed") from exc
    if (
        event.source_record_id != str(action_id)
        or not isinstance(action, str)
        or not action.strip()
        or not isinstance(snapshot, dict)
        or not isinstance(summary, dict)
        or not is_sha256_digest(snapshot_sha256)
        or not is_sha256_digest(reply_handle_digest)
        or state not in {"pending", *TERMINAL_STATES}
        or expires_at.tzinfo is None
        or event.expires_at is None
        or _as_utc(event.expires_at) != _as_utc(expires_at)
        or _snapshot_digest(action, snapshot) != snapshot_sha256
    ):
        raise NutritionConfirmationError("stored nutrition confirmation is malformed")
    result = payload.get("result")
    result_sha256 = payload.get("result_sha256")
    if result is not None:
        if (
            not isinstance(result, dict)
            or not is_sha256_digest(result_sha256)
            or result_payload_digest(result) != result_sha256
        ):
            raise NutritionConfirmationError("stored nutrition confirmation result is malformed")
    elif result_sha256 is not None:
        raise NutritionConfirmationError("stored nutrition confirmation result is malformed")
    if (state == "pending") != (result is None):
        raise NutritionConfirmationError("stored nutrition confirmation result is malformed")
    return PendingNutritionConfirmation(
        event=event,
        action_id=action_id,
        action=action,
        snapshot=dict(snapshot),
        snapshot_sha256=snapshot_sha256,
        summary=dict(summary),
        reply_handle_digest=reply_handle_digest,
        state=state,
        expires_at=_as_utc(expires_at),
        result=dict(result) if result is not None else None,
    )


def prepare_nutrition_confirmation(
    session: Session,
    *,
    action_id: uuid.UUID,
    action: str,
    snapshot: Mapping[str, Any],
    summary: Mapping[str, Any],
    handle_secret: str,
    source: str,
    now: datetime | None = None,
    ttl: timedelta = CONFIRMATION_TTL,
) -> PreparedNutritionConfirmation:
    """Persist an immutable snapshot and return its reproducible plaintext handle."""

    lock_nutrition_ledger(session)
    prepared_at = _as_utc(now or datetime.now(UTC))
    if not action.strip() or len(action) > 64:
        raise NutritionConfirmationError(
            "nutrition confirmation action must contain between 1 and 64 characters"
        )
    if not source.strip() or len(source) > 255:
        raise NutritionConfirmationError(
            "nutrition confirmation source must contain between 1 and 255 characters"
        )
    if ttl <= timedelta(0):
        raise NutritionConfirmationError("nutrition confirmation ttl must be positive")
    canonical_snapshot = dict(snapshot)
    canonical_summary = dict(summary)
    snapshot_sha256 = _snapshot_digest(action, canonical_snapshot)
    request_sha256 = _request_digest(canonical_snapshot)

    existing = session.scalar(
        select(WellnessEvent).where(
            WellnessEvent.event_type == CONFIRMATION_EVENT,
            WellnessEvent.source_provider == CONFIRMATION_PROVIDER,
            WellnessEvent.source_record_id == str(action_id),
        )
    )
    if existing is not None:
        confirmation = _stored_confirmation(existing)
        stored_arguments = confirmation.snapshot.get("arguments")
        incoming_arguments = canonical_snapshot.get("arguments")
        same_request = (
            stored_arguments == incoming_arguments
            if isinstance(stored_arguments, dict) and isinstance(incoming_arguments, dict)
            else confirmation.snapshot_sha256 == snapshot_sha256
        )
        if confirmation.action != action or not same_request:
            raise NutritionConfirmationError(
                "nutrition confirmation operation_id was already used with different input"
            )
        reply_handle = _confirmation_handle(
            handle_secret,
            action_id=action_id,
            snapshot_sha256=confirmation.snapshot_sha256,
        )
        reply_handle_digest = digest_reply_handle(reply_handle, handle_secret)
        if not hmac.compare_digest(
            confirmation.reply_handle_digest,
            reply_handle_digest,
        ):
            raise NutritionConfirmationError("stored nutrition confirmation handle is malformed")
        if confirmation.state in TERMINAL_STATES:
            assert confirmation.result is not None
            _ensure_terminal_tombstone(
                session,
                confirmation,
                state=confirmation.state,
                result=confirmation.result,
                completed_at=_as_utc(confirmation.event.recorded_at),
            )
        return PreparedNutritionConfirmation(
            confirmation=confirmation,
            reply_handle=reply_handle if confirmation.state == "pending" else None,
        )

    tombstone = _terminal_tombstone(session, action_id)
    if tombstone is not None:
        _validate_terminal_tombstone(
            tombstone,
            action_id=action_id,
            action=action,
            request_sha256=request_sha256,
        )
        raise NutritionConfirmationError(
            "nutrition confirmation operation_id is terminal and cannot be reused"
        )

    reply_handle = _confirmation_handle(
        handle_secret,
        action_id=action_id,
        snapshot_sha256=snapshot_sha256,
    )
    reply_handle_digest = digest_reply_handle(reply_handle, handle_secret)
    expires_at = prepared_at + ttl
    event = WellnessEvent(
        event_type=CONFIRMATION_EVENT,
        schema_version=1,
        observed_at=prepared_at,
        recorded_at=prepared_at,
        timezone=None,
        source_provider=CONFIRMATION_PROVIDER,
        source_device=source,
        source_record_id=str(action_id),
        capture_method="confirmation-gate",
        quality_flags=None,
        confidence=None,
        coverage=None,
        sensitivity="wellness",
        consent_scope="personal",
        retention_policy_id=None,
        expires_at=expires_at,
        payload={
            "schema_version": CONFIRMATION_SCHEMA,
            "action_id": str(action_id),
            "action": action,
            "snapshot": canonical_snapshot,
            "snapshot_sha256": snapshot_sha256,
            "summary": canonical_summary,
            "reply_handle_digest": reply_handle_digest,
            "state": "pending",
            "prepared_at": prepared_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "result": None,
            "result_sha256": None,
        },
        raw_object_id=None,
        derived_from={"snapshot_sha256": snapshot_sha256},
    )
    session.add(event)
    session.flush()
    return PreparedNutritionConfirmation(
        confirmation=_stored_confirmation(event),
        reply_handle=reply_handle,
    )


def nutrition_confirmation_by_handle(
    session: Session,
    *,
    reply_handle: str,
    handle_secret: str,
) -> PendingNutritionConfirmation | None:
    """Resolve an opaque handle without ever persisting its plaintext form."""

    digest = digest_reply_handle(reply_handle, handle_secret)
    statement = (
        select(WellnessEvent)
        .where(
            WellnessEvent.event_type == CONFIRMATION_EVENT,
            WellnessEvent.source_provider == CONFIRMATION_PROVIDER,
            WellnessEvent.payload["reply_handle_digest"].as_string() == digest,
        )
        .limit(2)
    )
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update()
    rows = list(session.scalars(statement))
    if len(rows) != 1:
        return None
    confirmation = _stored_confirmation(rows[0])
    expected_handle = _confirmation_handle(
        handle_secret,
        action_id=confirmation.action_id,
        snapshot_sha256=confirmation.snapshot_sha256,
    )
    expected_digest = digest_reply_handle(expected_handle, handle_secret)
    if not hmac.compare_digest(
        confirmation.reply_handle_digest,
        expected_digest,
    ):
        raise NutritionConfirmationError("stored nutrition confirmation handle is malformed")
    if not hmac.compare_digest(expected_digest, digest):
        return None
    return confirmation


def complete_nutrition_confirmation(
    session: Session,
    confirmation: PendingNutritionConfirmation,
    *,
    state: str,
    result: Mapping[str, Any],
    now: datetime | None = None,
) -> PendingNutritionConfirmation:
    if state not in TERMINAL_STATES:
        raise NutritionConfirmationError("unsupported nutrition confirmation state")
    current = _stored_confirmation(confirmation.event)
    if current.state in TERMINAL_STATES:
        if current.state != state or current.result != dict(result):
            raise NutritionConfirmationError(
                "nutrition confirmation was already consumed with a different result"
            )
        assert current.result is not None
        _ensure_terminal_tombstone(
            session,
            current,
            state=current.state,
            result=current.result,
            completed_at=_as_utc(current.event.recorded_at),
        )
        return current
    if current.state != "pending":
        raise NutritionConfirmationError("nutrition confirmation is not pending")
    completed_at = _as_utc(now or datetime.now(UTC))
    canonical_result = dict(result)
    confirmation.event.payload = {
        **confirmation.event.payload,
        "state": state,
        "completed_at": completed_at.isoformat(),
        "result": canonical_result,
        "result_sha256": result_payload_digest(canonical_result),
    }
    confirmation.event.recorded_at = completed_at
    session.flush()
    completed = _stored_confirmation(confirmation.event)
    _ensure_terminal_tombstone(
        session,
        completed,
        state=state,
        result=canonical_result,
        completed_at=completed_at,
    )
    return completed


def finalize_expired_nutrition_confirmations(
    session: Session,
    *,
    now: datetime,
) -> None:
    """Leave permanent non-content tombstones before expired gates are purged."""

    lock_nutrition_ledger(session)
    current = _as_utc(now)
    events = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == CONFIRMATION_EVENT,
                WellnessEvent.source_provider == CONFIRMATION_PROVIDER,
                WellnessEvent.expires_at.is_not(None),
                WellnessEvent.expires_at <= current,
            )
        )
    )
    for event in events:
        try:
            confirmation = _stored_confirmation(event)
        except NutritionConfirmationError:
            _ensure_invalid_terminal_tombstone(
                session,
                event,
                completed_at=current,
            )
            session.delete(event)
            continue
        if confirmation.state == "pending":
            result = {
                "status": "invalidated",
                "action": confirmation.action,
                "operation_id": str(confirmation.action_id),
                "reason": "confirmation_expired",
            }
            complete_nutrition_confirmation(
                session,
                confirmation,
                state="invalidated",
                result=result,
                now=current,
            )
            continue
        assert confirmation.result is not None
        _ensure_terminal_tombstone(
            session,
            confirmation,
            state=confirmation.state,
            result=confirmation.result,
            completed_at=_as_utc(confirmation.event.recorded_at),
        )
    session.flush()
