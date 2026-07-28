from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from healthmes.calendars.base import (
    CalendarBackend,
    CalendarError,
    EventNotFoundError,
    HealthmesEventKind,
    ensure_utc,
)
from healthmes.calendars.sleep_event_rendering import observation_fingerprint
from healthmes.calendars.sleep_observation import ActualSleepObservation, SleepObservationNoOp
from healthmes.calendars.sleep_proposal_state import capture_provider_state, redacted_digest
from healthmes.calendars.sleep_reconciliation import SleepCalendarReconciler, SleepCalendarResult
from healthmes.calendars.sleep_source import SleepSummaryReader, read_actual_sleep
from healthmes.store import SleepProposalStatus, SleepReconciliationProposal


@dataclass(frozen=True, slots=True)
class SleepApplyResult:
    status: SleepProposalStatus
    receipt: dict[str, Any] | None


class SleepReadBackError(RuntimeError):
    pass


def approval_token(
    proposal: SleepReconciliationProposal,
    session_id: str,
    secret: bytes,
) -> str:
    message = "\x1f".join(
        (str(proposal.id), proposal.dedup_key, proposal.expires_at.isoformat(), session_id)
    )
    return hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()


async def apply_sleep_proposal(
    *,
    proposal_id: uuid.UUID,
    submitted_token: str,
    local_session_id: str,
    secret: bytes,
    reader: SleepSummaryReader,
    user_id: str,
    session: Session,
    backend: CalendarBackend,
    now: dt.datetime | None = None,
) -> SleepApplyResult:
    proposal = session.get(
        SleepReconciliationProposal,
        proposal_id,
        with_for_update=True,
    )
    if proposal is None:
        return SleepApplyResult(SleepProposalStatus.INVALID, None)
    current_time = now or dt.datetime.now(dt.UTC)
    if proposal.status is not SleepProposalStatus.PENDING:
        return SleepApplyResult(proposal.status, proposal.receipt)
    if backend.source is not proposal.calendar_source:
        return _close(session, proposal, SleepProposalStatus.CONFLICTED, current_time)
    expected = approval_token(proposal, local_session_id, secret)
    if not hmac.compare_digest(submitted_token, expected):
        return _close(session, proposal, SleepProposalStatus.INVALID, current_time)
    expires_at = _aware(proposal.expires_at)
    if current_time >= expires_at:
        return _close(session, proposal, SleepProposalStatus.EXPIRED, current_time)

    selected = await read_actual_sleep(reader, user_id, proposal.local_date)
    if isinstance(selected, SleepObservationNoOp):
        return _close(session, proposal, SleepProposalStatus.CONFLICTED, current_time)
    if observation_fingerprint(selected) != proposal.observation_fingerprint:
        return _close(session, proposal, SleepProposalStatus.CONFLICTED, current_time)
    if capture_provider_state(session, backend, selected) != proposal.provider_state:
        return _close(session, proposal, SleepProposalStatus.CONFLICTED, current_time)

    proposal.status = SleepProposalStatus.APPLYING
    session.commit()
    try:
        result = SleepCalendarReconciler(session, backend).reconcile(selected)
        receipt = _read_back(backend, selected, result)
    except (CalendarError, SleepReadBackError, SQLAlchemyError):
        proposal.status = SleepProposalStatus.FAILED
        proposal.consumed_at = current_time
        session.commit()
        raise
    proposal.status = SleepProposalStatus.APPLIED
    proposal.consumed_at = current_time
    proposal.receipt = receipt
    session.commit()
    return SleepApplyResult(SleepProposalStatus.APPLIED, receipt)


def decline_sleep_proposal(
    session: Session,
    proposal_id: uuid.UUID,
    now: dt.datetime | None = None,
) -> SleepApplyResult:
    proposal = session.get(
        SleepReconciliationProposal,
        proposal_id,
        with_for_update=True,
    )
    if proposal is None:
        return SleepApplyResult(SleepProposalStatus.INVALID, None)
    if proposal.status is not SleepProposalStatus.PENDING:
        return SleepApplyResult(proposal.status, proposal.receipt)
    return _close(
        session,
        proposal,
        SleepProposalStatus.DECLINED,
        now or dt.datetime.now(dt.UTC),
    )


def _read_back(
    backend: CalendarBackend,
    observation: ActualSleepObservation,
    result: SleepCalendarResult,
) -> dict[str, Any]:
    remote = backend.read_event(result.external_id)
    if (
        not remote.is_agent_created
        or remote.identity is None
        or remote.identity.kind is not HealthmesEventKind.ACTUAL_SLEEP
        or remote.start_at != ensure_utc(observation.start_at)
        or remote.end_at != ensure_utc(observation.end_at)
    ):
        raise SleepReadBackError(
            "calendar read-back did not match the approved actual sleep"
        )
    for external_id in result.deleted_planned_external_ids:
        try:
            backend.read_event(external_id)
        except EventNotFoundError:
            continue
        raise SleepReadBackError("planned sleep still exists after delete")
    return {
        "verified": True,
        "action": result.action.value,
        "calendar": backend.source.value,
        "event": redacted_digest(result.external_id),
        "start": remote.start_at.isoformat(),
        "wake_time": remote.end_at.isoformat(),
        "planned_sleep_deleted": len(result.deleted_planned_external_ids),
        "read_back_at": dt.datetime.now(dt.UTC).isoformat(),
    }


def _close(
    session: Session,
    proposal: SleepReconciliationProposal,
    status: SleepProposalStatus,
    consumed_at: dt.datetime,
) -> SleepApplyResult:
    proposal.status = status
    proposal.consumed_at = consumed_at
    session.commit()
    return SleepApplyResult(status, None)


def _aware(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)
