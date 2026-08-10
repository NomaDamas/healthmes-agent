import datetime as dt
import hashlib
import hmac
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from healthmes.calendars.adjustments_logic import verify_reply_handle
from healthmes.schedule_outcomes import (
    record_invalidation_outcome,
    record_resolution_outcome,
)
from healthmes.store.enums import ProposalStatus
from healthmes.store.models import ScheduleProposal


class ScheduleProposalResolutionError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _proposal_expiry(proposal: ScheduleProposal) -> dt.datetime | None:
    expires_at = proposal.expires_at
    return _as_utc(expires_at) if expires_at is not None else None


def _raise_transition_conflict(
    session: Session,
    proposal: ScheduleProposal,
    current: dt.datetime,
) -> None:
    session.expire(proposal)
    expires_at = _proposal_expiry(proposal)
    if proposal.status is ProposalStatus.PROPOSED and (expires_at is None or current >= expires_at):
        raise ScheduleProposalResolutionError("expired")
    raise ScheduleProposalResolutionError("not_proposed")


def _transition_locked_postgres(
    session: Session,
    proposal_id: uuid.UUID,
    target: ProposalStatus,
    now: dt.datetime | None,
    surface: str | None = None,
) -> ScheduleProposal:
    proposal = session.scalar(
        select(ScheduleProposal)
        .where(ScheduleProposal.id == proposal_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if proposal is None:
        raise ScheduleProposalResolutionError("not_found")
    current = _as_utc(
        now or session.scalar(select(func.clock_timestamp())) or dt.datetime.now(dt.UTC)
    )
    expires_at = _proposal_expiry(proposal)
    if proposal.status is not ProposalStatus.PROPOSED:
        raise ScheduleProposalResolutionError("not_proposed")
    if expires_at is None or current >= expires_at:
        raise ScheduleProposalResolutionError("expired")
    proposal.status = target
    if surface is not None:
        proposal.decided_at = current
        proposal.decision_surface = surface
    session.flush()
    session.refresh(proposal)
    return proposal


def _transition_compare_and_swap(
    session: Session,
    proposal: ScheduleProposal,
    target: ProposalStatus,
    current: dt.datetime,
    surface: str | None = None,
) -> ScheduleProposal:
    values: dict[str, object] = {"status": target}
    if surface is not None:
        values.update(
            decided_at=current,
            decision_surface=surface,
        )
    result = session.execute(
        update(ScheduleProposal)
        .where(
            ScheduleProposal.id == proposal.id,
            ScheduleProposal.status == ProposalStatus.PROPOSED,
            ScheduleProposal.expires_at.is_not(None),
            ScheduleProposal.expires_at > current,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        _raise_transition_conflict(session, proposal, current)
    session.flush()
    session.refresh(proposal)
    return proposal


def resolution_token(
    proposal: ScheduleProposal,
    handle_secret: str,
    target: ProposalStatus,
) -> str | None:
    if proposal.reply_handle_digest is None or proposal.expires_at is None:
        return None
    if target not in {ProposalStatus.ACCEPTED, ProposalStatus.DECLINED}:
        return None
    expires_at = _proposal_expiry(proposal)
    assert expires_at is not None
    expires_at = expires_at.astimezone(dt.UTC)
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
    surface: str | None = None,
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
    expires_at = _proposal_expiry(proposal)
    current = _as_utc(now or dt.datetime.now(dt.UTC))
    normalized_surface = normalize_decision_surface(surface)
    if expires_at is None or current >= expires_at:
        raise ScheduleProposalResolutionError("expired")
    if session.get_bind().dialect.name == "postgresql":
        resolved = _transition_locked_postgres(
            session,
            proposal_id,
            target,
            now,
            normalized_surface,
        )
    else:
        resolved = _transition_compare_and_swap(
            session,
            proposal,
            target,
            current,
            normalized_surface,
        )
    record_resolution_outcome(session, resolved, target)
    session.flush()
    return resolved


def normalize_decision_surface(value: str | None) -> str:
    normalized = (value or "api").strip().lower().replace("-", "_")
    allowed = {
        "api",
        "apple_notification",
        "ios_notification",
        "apple_watch_notification",
        "ios_app",
        "android_notification",
        "wear_os_notification",
        "android_app",
        "web",
        "telegram",
        "slack",
        "discord",
    }
    return normalized if normalized in allowed else "api"


def invalidate_schedule_proposal(
    session: Session,
    proposal_id: uuid.UUID,
    *,
    now: dt.datetime | None = None,
) -> ScheduleProposal:
    proposal = session.get(ScheduleProposal, proposal_id)
    if proposal is None:
        raise ScheduleProposalResolutionError("not_found")
    current = _as_utc(now or dt.datetime.now(dt.UTC))
    if session.get_bind().dialect.name == "postgresql":
        invalidated = _transition_locked_postgres(
            session,
            proposal_id,
            ProposalStatus.INVALIDATED,
            now,
        )
    else:
        invalidated = _transition_compare_and_swap(
            session,
            proposal,
            ProposalStatus.INVALIDATED,
            current,
        )
    record_invalidation_outcome(session, invalidated, reason="system_invalidation")
    session.flush()
    return invalidated
