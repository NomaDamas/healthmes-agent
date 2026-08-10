"""Compatibility adapters from legacy resolver requests to decision contracts."""

from __future__ import annotations

from datetime import UTC, date, datetime

from healthmes.activity.contracts import ActivityContextResolveRequest
from healthmes.decision.contracts import (
    CompatibilityPreset,
    DecisionCaller,
    DecisionContextHints,
    DecisionRequest,
    PrivacyLevel,
)


def decision_request_from_activity_context(
    request: ActivityContextResolveRequest,
    *,
    caller: DecisionCaller,
    default_timezone: str,
    question: str | None = None,
    requested_at: datetime | None = None,
    requested_privacy_level: PrivacyLevel = PrivacyLevel.AGGREGATE,
) -> DecisionRequest:
    """Preserve a legacy resolver request as an optional decision preset."""

    related_record_ids = (
        {"nutrition_request": str(request.nutrition_request_id)}
        if request.nutrition_request_id is not None
        else {}
    )
    hints = DecisionContextHints(
        local_date=date.fromisoformat(request.date)
        if request.date is not None
        else None,
        start=request.start,
        end=request.end,
        lookback_days=request.lookback_days,
        related_record_ids=related_record_ids,
    )
    return DecisionRequest.from_compatibility_preset(
        CompatibilityPreset(request.question_kind),
        caller=caller,
        timezone=request.timezone or default_timezone,
        question=question,
        requested_at=requested_at or datetime.now(UTC),
        hints=hints,
        requested_privacy_level=requested_privacy_level,
    )
