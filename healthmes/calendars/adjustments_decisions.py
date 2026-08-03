from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from healthmes.calendars.adjustments_proposals import provider_revision_fingerprint
from healthmes.calendars.adjustments_types import (
    SHORTEN_MINUTES,
    AdjustmentOperation,
    AdjustmentStatus,
    ProposalSnapshot,
    StoredAdjustmentProposal,
)
from healthmes.calendars.base import (
    ExternalEvent,
)


def redacted_receipt(
    *,
    status: AdjustmentStatus,
    provider_code: str | None = None,
    provider_event: ExternalEvent | None = None,
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "operation": AdjustmentOperation.SHORTEN.value,
        "delta_minutes": SHORTEN_MINUTES,
        "status": status.value,
    }
    if provider_code is not None:
        receipt["provider_code"] = provider_code
    if provider_event is not None and provider_event.etag:
        receipt["provider_result"] = "matched"
        receipt["provider_revision"] = provider_revision_fingerprint(provider_event.etag)
    return receipt


def initial_decision_tree(snapshot: ProposalSnapshot, facts: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": "calendar_adjustment_initial",
        "type": "rule",
        "label": "morning recovery calendar nudge",
        "detail": {
            "proposal_id": None,
            "operation": snapshot.operation.value,
            "delta_minutes": SHORTEN_MINUTES,
        },
        "children": [
            {"id": "evidence", "type": "input", "label": "health evidence", "detail": dict(facts)},
            {
                "id": "limitation",
                "type": "rule",
                "label": "technical eligibility only",
                "detail": {"confirmation_required": True},
            },
            {
                "id": "option_keep",
                "type": "option",
                "label": "keep event unchanged",
                "detail": {},
            },
            {
                "id": "option_shorten",
                "type": "option",
                "label": "shorten event by 30 minutes",
                "detail": {},
            },
            {
                "id": "action",
                "type": "action",
                "label": "ask user to confirm exact calendar change",
                "detail": {
                    "event_label": snapshot.event_label,
                    "timezone": snapshot.local_timezone,
                    "before": {
                        "start_at": snapshot.local_original_start_at,
                        "end_at": snapshot.local_original_end_at,
                    },
                    "after": {
                        "start_at": snapshot.local_original_start_at,
                        "end_at": snapshot.local_proposed_end_at,
                    },
                },
            },
        ],
    }


def no_action_decision_tree(reason: str, facts: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": "calendar_adjustment_no_action",
        "type": "rule",
        "label": "morning recovery calendar nudge skipped",
        "detail": {
            "outcome": "no_action",
            "reason": reason,
        },
        "children": [
            {
                "id": "evidence",
                "type": "input",
                "label": "redacted evaluation evidence",
                "detail": dict(facts),
            },
            {
                "id": "action",
                "type": "action",
                "label": "leave calendar unchanged",
                "detail": {"calendar_write": False},
            },
        ],
    }


def outcome_decision_tree(
    proposal: StoredAdjustmentProposal, receipt: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "id": "calendar_adjustment_outcome",
        "type": "action",
        "label": "calendar adjustment resolved",
        "detail": {
            "initial_decision_record_id": str(proposal.proposal_decision_record_id),
            "proposal_id": str(proposal.id),
            "receipt": dict(receipt),
        },
        "children": [],
    }
