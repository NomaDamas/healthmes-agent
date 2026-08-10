"""Decision records for schedule proposal resolution and calendar execution."""

from __future__ import annotations

from sqlalchemy.orm import Session

from healthmes.store import (
    CalendarSource,
    DecisionKind,
    DecisionRecord,
    ProposalStatus,
    ScheduleProposal,
)


def record_resolution_outcome(
    session: Session,
    proposal: ScheduleProposal,
    target: ProposalStatus,
) -> DecisionRecord:
    surface = proposal.decision_surface or "api"
    record = DecisionRecord(
        kind=DecisionKind.SCHEDULE_CHANGE,
        summary=f"Schedule proposal {target.value} on {surface}",
        tree={
            "id": "schedule_proposal_resolution",
            "type": "action",
            "label": "owner resolved schedule proposal",
            "detail": {
                "proposal_id": str(proposal.id),
                "initial_decision_record_id": (
                    str(proposal.decision_record_id)
                    if proposal.decision_record_id is not None
                    else None
                ),
                "status": target.value,
                "surface": surface,
                "decided_at": (
                    proposal.decided_at.isoformat()
                    if proposal.decided_at is not None
                    else None
                ),
                "calendar_write": False,
            },
            "children": [],
        },
        llm_model=None,
        tokens=None,
    )
    session.add(record)
    return record


def record_calendar_push_outcome(
    session: Session,
    proposal: ScheduleProposal,
    source: CalendarSource,
    *,
    provider_event_id: str,
    reused_existing: bool,
) -> DecisionRecord:
    record = DecisionRecord(
        kind=DecisionKind.SCHEDULE_CHANGE,
        summary=f"Approved schedule proposal pushed to {source.value}",
        tree={
            "id": "schedule_proposal_calendar_push",
            "type": "action",
            "label": "approved schedule change applied",
            "detail": {
                "proposal_id": str(proposal.id),
                "initial_decision_record_id": (
                    str(proposal.decision_record_id)
                    if proposal.decision_record_id is not None
                    else None
                ),
                "status": ProposalStatus.PUSHED.value,
                "calendar_source": source.value,
                "provider_event_id": provider_event_id,
                "reused_existing": reused_existing,
                "calendar_write": True,
            },
            "children": [],
        },
        llm_model=None,
        tokens=None,
    )
    session.add(record)
    return record


def record_invalidation_outcome(
    session: Session,
    proposal: ScheduleProposal,
    *,
    reason: str,
) -> DecisionRecord:
    record = DecisionRecord(
        kind=DecisionKind.SCHEDULE_CHANGE,
        summary=f"Schedule proposal invalidated: {reason}",
        tree={
            "id": "schedule_proposal_invalidation",
            "type": "action",
            "label": "schedule proposal could not be applied",
            "detail": {
                "proposal_id": str(proposal.id),
                "initial_decision_record_id": (
                    str(proposal.decision_record_id)
                    if proposal.decision_record_id is not None
                    else None
                ),
                "status": ProposalStatus.INVALIDATED.value,
                "reason": reason,
                "calendar_write": False,
            },
            "children": [],
        },
        llm_model=None,
        tokens=None,
    )
    session.add(record)
    return record
