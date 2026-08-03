import datetime as dt
import hashlib
import hmac
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


def resolution_token(
    proposal: ScheduleProposal,
    handle_secret: str,
    target: ProposalStatus,
) -> str | None:
    if proposal.reply_handle_digest is None or proposal.expires_at is None:
        return None
    if target not in {ProposalStatus.ACCEPTED, ProposalStatus.DECLINED}:
        return None
    expires_at = (
        proposal.expires_at.replace(tzinfo=dt.UTC)
        if proposal.expires_at.tzinfo is None
        else proposal.expires_at.astimezone(dt.UTC)
    )
    payload = (
        "healthmes-api:schedule-proposal-resolution:v1:"
        f"{proposal.id}:{target.value}:{proposal.reply_handle_digest}:"
        f"{expires_at.isoformat()}"
    )
    return hmac.new(
        handle_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_resolution_token(
    token: str,
    proposal: ScheduleProposal,
    handle_secret: str,
    target: ProposalStatus,
) -> bool:
    expected = resolution_token(proposal, handle_secret, target)
    return expected is not None and hmac.compare_digest(token, expected)


def resolve_schedule_proposal(
    session: Session,
    proposal_id: uuid.UUID,
    target: ProposalStatus,
    reply_handle: str | None,
    handle_secret: str,
    *,
    now: dt.datetime | None = None,
    allow_reply_handle: bool = True,
    allow_resolution_token: bool = False,
) -> ScheduleProposal:
    proposal = session.get(ScheduleProposal, proposal_id)
    if proposal is None:
        raise ScheduleProposalResolutionError("not_found")
    if proposal.status is not ProposalStatus.PROPOSED:
        raise ScheduleProposalResolutionError("not_proposed")
    reply_handle_valid = bool(
        allow_reply_handle
        and reply_handle
        and proposal.reply_handle_digest is not None
        and verify_reply_handle(
            reply_handle,
            proposal.reply_handle_digest,
            handle_secret,
        )
    )
    resolution_token_valid = bool(
        allow_resolution_token
        and reply_handle
        and verify_resolution_token(
            reply_handle,
            proposal,
            handle_secret,
            target,
        )
    )
    if not reply_handle_valid and not resolution_token_valid:
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
