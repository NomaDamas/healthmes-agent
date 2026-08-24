from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from healthmes.calendars.adjustments_types import (
    CONFIDENCE_RANK,
    DEFAULT_FRESHNESS,
    MAX_EVIDENCE_CLOCK_SKEW,
    MIN_ORIGINAL_DURATION,
    MIN_START_LEAD,
    EligibilityResult,
    HealthEvidenceResult,
)
from healthmes.calendars.base import (
    coerce_utc,
    ensure_utc,
)
from healthmes.engine.rules import RuleThresholds
from healthmes.mcp_server.interpret import normalize_recovery
from healthmes.store.enums import (
    CalendarSource,
)
from healthmes.store.models import LEGACY_CALENDAR_ACCOUNT_GENERATION


def evaluate_event_eligibility(
    event: Any,
    *,
    now: datetime,
    local_date: date,
    timezone: tzinfo,
    already_proposed: bool = False,
) -> EligibilityResult:
    reasons: list[str] = []
    source = _attr(event, "calendar_source", CalendarSource.GOOGLE)
    start = coerce_utc(_attr(event, "start_at"))
    end = coerce_utc(_attr(event, "end_at"))
    local_start = start.astimezone(timezone)
    local_end = end.astimezone(timezone)
    account_generation = _attr(
        event,
        "connection_generation",
        _attr(event, "account_generation", None),
    )

    if _source_value(source) != CalendarSource.GOOGLE.value:
        reasons.append("unsupported_source")
    if (
        not isinstance(account_generation, str)
        or not account_generation.strip()
        or account_generation == LEGACY_CALENDAR_ACCOUNT_GENERATION
    ):
        reasons.append("missing_account_generation")
    if bool(_attr(event, "is_agent_created", False)):
        reasons.append("agent_owned_path_only")
    if not bool(_attr(event, "organizer_self", False)):
        reasons.append("not_self_organized")
    if bool(_attr(event, "has_attendees", False)):
        reasons.append("has_attendees")
    if bool(_attr(event, "is_recurring", False)):
        reasons.append("recurring")
    if bool(_attr(event, "is_all_day", False)):
        reasons.append("all_day")
    if (_attr(event, "event_type", "default") or "default") != "default":
        reasons.append("unsupported_event_type")
    if bool(_attr(event, "is_locked", False)):
        reasons.append("locked")
    if str(_attr(event, "status", "") or "").lower() == "cancelled":
        reasons.append("cancelled")
    if start < ensure_utc(now) + MIN_START_LEAD:
        reasons.append("too_soon")
    if local_start.date() != local_date or local_end.date() != local_date:
        reasons.append("not_today")
    if end - start < MIN_ORIGINAL_DURATION:
        reasons.append("too_short")
    if already_proposed:
        reasons.append("already_proposed")
    if not _attr(event, "etag", None):
        reasons.append("missing_etag")
    return EligibilityResult(eligible=not reasons, reasons=tuple(reasons))


def evaluate_health_evidence(
    context: Mapping[str, Any],
    *,
    local_date: date,
    now: datetime,
    afternoon_busy_minutes: int,
    eligible_event_count: int,
    freshness: timedelta = DEFAULT_FRESHNESS,
    thresholds: RuleThresholds | None = None,
) -> HealthEvidenceResult:
    thresholds = thresholds or RuleThresholds()
    sleep = _mapping(context.get("sleep_debt"))
    if sleep.get("status") != "ok":
        return HealthEvidenceResult(False, "missing_sleep")
    if _confidence(sleep.get("confidence")) < CONFIDENCE_RANK["medium"]:
        return HealthEvidenceResult(False, "low_confidence_sleep")
    sleep_freshness = _freshness_failure(
        sleep, local_date=local_date, now=now, freshness=freshness
    )
    if sleep_freshness is not None:
        return HealthEvidenceResult(False, f"{sleep_freshness}_sleep")

    hrv_block = _mapping(context.get("nocturnal_hrv") or context.get("hrv"))
    charge_block = _mapping(
        context.get("charge")
        or context.get("body_battery")
        or context.get("readiness")
        or context.get("recovery")
    )
    recovery_blocks = [hrv_block, charge_block]
    confidence_qualified = [
        block
        for block in recovery_blocks
        if block.get("status") == "ok"
        and _confidence(block.get("confidence")) >= CONFIDENCE_RANK["medium"]
    ]
    recovery_freshness = {
        id(block): _freshness_failure(
            block, local_date=local_date, now=now, freshness=freshness
        )
        for block in confidence_qualified
    }
    usable_recovery = [
        block for block in confidence_qualified if recovery_freshness[id(block)] is None
    ]
    if not usable_recovery:
        if any(block for block in recovery_blocks):
            if not confidence_qualified:
                return HealthEvidenceResult(False, "low_confidence_recovery")
            if any(
                recovery_freshness[id(block)] == "future"
                for block in confidence_qualified
            ):
                return HealthEvidenceResult(False, "future_recovery")
            return HealthEvidenceResult(False, "stale_recovery")
        return HealthEvidenceResult(False, "missing_recovery")

    charge_is_usable = any(block is charge_block for block in usable_recovery)
    recovery_value = _recovery_value([charge_block]) if charge_is_usable else None
    if recovery_value is None:
        return HealthEvidenceResult(False, "missing_recovery_score")
    if recovery_value > thresholds.low_recovery_max_value:
        return HealthEvidenceResult(False, "no_nudge_needed")
    if afternoon_busy_minutes < thresholds.heavy_afternoon_min_busy_minutes:
        return HealthEvidenceResult(False, "afternoon_not_heavy")
    if eligible_event_count < 1:
        return HealthEvidenceResult(False, "no_eligible_event")

    return HealthEvidenceResult(
        True,
        facts={
            "sleep_confidence": sleep.get("confidence"),
            "recovery_confidence": usable_recovery[0].get("confidence"),
            "recovery_value_bucket": _bucket_recovery(recovery_value),
            "afternoon_busy_minutes": afternoon_busy_minutes,
        },
    )


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _source_value(source: Any) -> str:
    return str(getattr(source, "value", source))


def _operation_value(operation: Any) -> str:
    return str(getattr(operation, "value", operation)).lower()


def _confidence(value: Any) -> int:
    return CONFIDENCE_RANK.get(str(value or "low").lower(), 0)


def _freshness_failure(
    block: Mapping[str, Any], *, local_date: date, now: datetime, freshness: timedelta
) -> str | None:
    observed = _observed_at(block)
    last_night = _mapping(block.get("last_night"))
    if observed is None:
        observed = _observed_at(last_night)
    if observed is None:
        observed_date = (
            block.get("date")
            or block.get("observed_date")
            or block.get("freshest_date")
            or last_night.get("date")
        )
        if observed_date is None:
            entry_dates = [
                str(entry.get("observed_on"))
                for entry in block.get("entries", ())
                if isinstance(entry, Mapping) and entry.get("observed_on")
            ]
            observed_date = max(entry_dates, default=None)
        return None if str(observed_date) == local_date.isoformat() else "stale"
    age = ensure_utc(now) - observed
    if age < -MAX_EVIDENCE_CLOCK_SKEW:
        return "future"
    if age > freshness:
        return "stale"
    return None


def _observed_at(block: Mapping[str, Any]) -> datetime | None:
    for key in ("observed_at", "recorded_at", "freshest_at", "as_of"):
        value = block.get(key)
        if isinstance(value, datetime):
            return ensure_utc(value)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is not None:
                return ensure_utc(parsed)
    return None


def _recovery_value(blocks: Sequence[Mapping[str, Any]]) -> float | None:
    for block in blocks:
        for key in ("recovery_value", "value", "score"):
            value = block.get(key)
            if isinstance(value, int | float):
                return float(value)
        for entry in block.get("entries", ()):
            if not isinstance(entry, Mapping):
                continue
            value = entry.get("value")
            category = entry.get("category")
            if isinstance(value, int | float) and isinstance(category, str):
                return normalize_recovery(
                    category,
                    str(entry.get("provider")) if entry.get("provider") is not None else None,
                    float(value),
                )
    return None


def _bucket_recovery(value: float) -> str:
    if value <= 20:
        return "very_low"
    if value <= 40:
        return "low"
    if value <= 70:
        return "medium"
    return "high"
