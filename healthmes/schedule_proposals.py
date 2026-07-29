import datetime as dt
import uuid

from sqlalchemy import update
from sqlalchemy.orm import Session

from healthmes.calendars.adjustments_logic import verify_reply_handle
from healthmes.store.enums import ProposalStatus
from healthmes.store.models import ScheduleProposal


class ScheduleProposalResolutionError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def resolve_schedule_proposal(
    session: Session,
    proposal_id: uuid.UUID,
    target: ProposalStatus,
    reply_handle: str | None,
    handle_secret: str,
    *,
    now: dt.datetime | None = None,
) -> ScheduleProposal:
    proposal = session.get(ScheduleProposal, proposal_id)
    if proposal is None:
        raise ScheduleProposalResolutionError("not_found")
    if proposal.status is not ProposalStatus.PROPOSED:
        raise ScheduleProposalResolutionError("not_proposed")
    if (
        not reply_handle
        or proposal.reply_handle_digest is None
        or not verify_reply_handle(
            reply_handle,
            proposal.reply_handle_digest,
            handle_secret,
        )
    ):
        raise ScheduleProposalResolutionError("invalid_handle")
    expires_at = (
        proposal.expires_at.replace(tzinfo=dt.UTC)
        if proposal.expires_at is not None and proposal.expires_at.tzinfo is None
        else proposal.expires_at
    )
    current = now or dt.datetime.now(dt.UTC)
    if expires_at is None or current >= expires_at:
        raise ScheduleProposalResolutionError("expired")
    result = session.execute(
        update(ScheduleProposal)
        .where(
            ScheduleProposal.id == proposal_id,
            ScheduleProposal.status == ProposalStatus.PROPOSED,
        )
        .values(status=target)
    )
    if result.rowcount != 1:
        session.expire(proposal)
        raise ScheduleProposalResolutionError("not_proposed")
    session.flush()
    session.refresh(proposal)
    return proposal
