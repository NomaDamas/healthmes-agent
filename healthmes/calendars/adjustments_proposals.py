from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from healthmes.calendars.adjustments_policy import (
    _attr,
    _operation_value,
    _source_value,
)
from healthmes.calendars.adjustments_types import (
    HANDLE_TTL,
    MORNING_NUDGE_RULE_ID,
    SHORTEN_MINUTES,
    START_SAFETY_LEAD,
    AdjustmentError,
    AdjustmentOperation,
    CalendarAccountGenerationChanged,
    CalendarAdjustmentWriter,
    HandlePair,
    ProposalSnapshot,
    StoredAdjustmentProposal,
)
from healthmes.calendars.base import (
    ConfirmedExternalTimeChange,
    ExternalEvent,
    coerce_utc,
    ensure_utc,
)
from healthmes.store.enums import (
    CalendarSource,
)


def validate_shorten_change(
    *,
    external_event_id: str,
    original_start_at: datetime,
    original_end_at: datetime,
    proposed_start_at: datetime,
    proposed_end_at: datetime,
    expected_etag: str,
    timezone: tzinfo,
    operation: AdjustmentOperation | str = AdjustmentOperation.SHORTEN,
) -> ConfirmedExternalTimeChange:
    if _operation_value(operation) != AdjustmentOperation.SHORTEN.value:
        raise AdjustmentError("v0 supports only SHORTEN")
    try:
        change = ConfirmedExternalTimeChange(
            external_event_id=external_event_id,
            original_start_at=original_start_at,
            original_end_at=original_end_at,
            proposed_start_at=proposed_start_at,
            proposed_end_at=proposed_end_at,
            expected_etag=expected_etag,
        )
    except ValueError as exc:
        raise AdjustmentError(str(exc)) from exc
    local_dates = {
        change.original_start_at.astimezone(timezone).date(),
        change.original_end_at.astimezone(timezone).date(),
        change.proposed_start_at.astimezone(timezone).date(),
        change.proposed_end_at.astimezone(timezone).date(),
    }
    if len(local_dates) != 1:
        raise AdjustmentError("v0 external time changes must remain on one local date")
    return change


def make_shorten_snapshot(event: Any, *, timezone: tzinfo) -> ProposalSnapshot:
    start = coerce_utc(_attr(event, "start_at"))
    end = coerce_utc(_attr(event, "end_at"))
    proposed_end = end - timedelta(minutes=SHORTEN_MINUTES)
    change = validate_shorten_change(
        external_event_id=str(_attr(event, "external_id")),
        original_start_at=start,
        original_end_at=end,
        proposed_start_at=start,
        proposed_end_at=proposed_end,
        expected_etag=str(_attr(event, "etag")),
        timezone=timezone,
    )
    calendar_source = CalendarSource(
        _source_value(
            _attr(event, "calendar_source", CalendarSource.GOOGLE)
        )
    )
    account_generation = _attr(
        event,
        "connection_generation",
        _attr(event, "account_generation", None),
    )
    return ProposalSnapshot(
        calendar_source=calendar_source,
        account_generation=(
            str(account_generation) if account_generation is not None else None
        ),
        mirror_event_id=_attr(event, "id", _attr(event, "external_id")),
        external_event_id=change.external_event_id,
        operation=AdjustmentOperation.SHORTEN,
        original_start_at=change.original_start_at,
        original_end_at=change.original_end_at,
        proposed_start_at=change.proposed_start_at,
        proposed_end_at=change.proposed_end_at,
        expected_etag=change.expected_etag,
        protected_fingerprint=protected_event_fingerprint(event),
        dedup_key=proposal_dedup_key(
            event, timezone=timezone, operation=AdjustmentOperation.SHORTEN
        ),
        event_label=_attr(event, "summary", None),
        local_timezone=str(timezone),
        local_original_start_at=start.astimezone(timezone).isoformat(),
        local_original_end_at=end.astimezone(timezone).isoformat(),
        local_proposed_end_at=proposed_end.astimezone(timezone).isoformat(),
    )


def proposal_expiry(event_start_at: datetime, created_at: datetime) -> datetime:
    created = ensure_utc(created_at)
    start = ensure_utc(event_start_at)
    return min(created + HANDLE_TTL, start - START_SAFETY_LEAD)


def is_expired(proposal: StoredAdjustmentProposal, now: datetime) -> bool:
    now_utc = ensure_utc(now)
    return now_utc >= proposal.expires_at or now_utc >= (
        proposal.snapshot.original_start_at - START_SAFETY_LEAD
    )


def issue_reply_handle(
    secret: str, *, handle_factory: Callable[[], str] | None = None
) -> HandlePair:
    plaintext = handle_factory() if handle_factory is not None else secrets.token_urlsafe(32)
    return HandlePair(plaintext=plaintext, digest=digest_reply_handle(plaintext, secret))


def digest_reply_handle(handle: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), handle.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_reply_handle(handle: str, digest: str, secret: str) -> bool:
    return hmac.compare_digest(digest_reply_handle(handle, secret), digest)


def morning_dedup_key(local_date: date) -> str:
    return f"{MORNING_NUDGE_RULE_ID}:{local_date.isoformat()}"


def proposal_dedup_key(
    event: Any, *, timezone: tzinfo, operation: AdjustmentOperation | str
) -> str:
    start = coerce_utc(_attr(event, "start_at"))
    end = coerce_utc(_attr(event, "end_at"))
    proposed_end = end - timedelta(minutes=SHORTEN_MINUTES)
    parts = [
        start.astimezone(timezone).date().isoformat(),
        _source_value(_attr(event, "calendar_source", CalendarSource.GOOGLE)),
        str(
            _attr(
                event,
                "connection_generation",
                _attr(event, "account_generation", ""),
            )
            or ""
        ),
        str(_attr(event, "id", "")),
        str(_attr(event, "external_id", "")),
        str(_attr(event, "etag", "")),
        _operation_value(operation),
        start.isoformat(),
        proposed_end.isoformat(),
    ]
    return "calendar_adjustment:" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def protected_event_fingerprint(event: Any) -> str:
    fields = {
        "summary": _attr(event, "summary", None) or "",
        "organizer_self": bool(_attr(event, "organizer_self", False)),
        "has_attendees": bool(_attr(event, "has_attendees", False)),
        "is_recurring": bool(_attr(event, "is_recurring", False)),
        "event_type": _attr(event, "event_type", "default") or "default",
        "is_all_day": bool(_attr(event, "is_all_day", False)),
        "is_locked": bool(_attr(event, "is_locked", False)),
        "status": _attr(event, "status", None) or "",
    }
    material = "|".join(f"{key}={fields[key]}" for key in sorted(fields))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def provider_revision_fingerprint(etag: str | None) -> str | None:
    if not etag:
        return None
    return hashlib.sha256(etag.encode("utf-8")).hexdigest()


def snapshot_matches(snapshot: ProposalSnapshot, event: Any) -> bool:
    return (
        _source_value(_attr(event, "calendar_source", ""))
        == snapshot.calendar_source.value
        and (
            _attr(
                event,
                "connection_generation",
                _attr(event, "account_generation", None),
            )
            == snapshot.account_generation
        )
        and str(_attr(event, "id", "")) == str(snapshot.mirror_event_id)
        and str(_attr(event, "external_id", ""))
        == snapshot.external_event_id
        and coerce_utc(_attr(event, "start_at"))
        == snapshot.original_start_at
        and coerce_utc(_attr(event, "end_at")) == snapshot.original_end_at
        and _attr(event, "etag", None) == snapshot.expected_etag
        and protected_event_fingerprint(event) == snapshot.protected_fingerprint
    )


def remote_matches_snapshot(snapshot: ProposalSnapshot, event: ExternalEvent) -> bool:
    return (
        event.external_id == snapshot.external_event_id
        and event.start_at == snapshot.proposed_start_at
        and event.end_at == snapshot.proposed_end_at
        and protected_event_fingerprint(event) == snapshot.protected_fingerprint
    )


def read_remote_event(
    writer: CalendarAdjustmentWriter, external_event_id: str
) -> ExternalEvent | None:
    try:
        return writer.read_event(external_event_id)
    except CalendarAccountGenerationChanged:
        raise
    except Exception:  # noqa: BROAD_EXCEPT_OK — provider SDKs expose no shared read-error base.
        return None


def _http_status(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    return status if isinstance(status, int) else None
