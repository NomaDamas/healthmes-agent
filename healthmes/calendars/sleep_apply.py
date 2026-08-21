from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import uuid
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from healthmes.activity.locking import (
    activity_write_lock,
    lock_activity_write_plane,
)
from healthmes.calendars.approval import ApprovalCalendar
from healthmes.calendars.base import (
    CalendarBackend,
    CalendarError,
    EventNotFoundError,
    ensure_utc,
)
from healthmes.calendars.sleep_event_rendering import observation_fingerprint
from healthmes.calendars.sleep_mirror import actual_sleep_identity
from healthmes.calendars.sleep_observation import (
    ActualSleepObservation,
    SleepObservationNoOp,
    calendar_observations,
)
from healthmes.calendars.sleep_proposal_state import (
    capture_provider_state,
    redacted_digest,
)
from healthmes.calendars.sleep_reconciliation import (
    SleepCalendarReconciler,
    SleepCalendarResult,
)
from healthmes.calendars.sleep_source import SleepSummaryReader, read_actual_sleep
from healthmes.calendars.write_lock import calendar_write_lock
from healthmes.store import SleepProposalStatus, SleepReconciliationProposal

APPLYING_RECOVERY_DELAY = dt.timedelta(minutes=5)


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
    calendar: ApprovalCalendar,
    now: dt.datetime | None = None,
) -> SleepApplyResult:
    proposal = session.get(
        SleepReconciliationProposal,
        proposal_id,
    )
    if proposal is None:
        return SleepApplyResult(SleepProposalStatus.INVALID, None)
    local_date = proposal.local_date
    selected = await read_actual_sleep(
        reader,
        user_id,
        local_date,
        review_base_url=calendar.review_base_url,
        review_url_builder=calendar.review_url_builder,
    )
    return apply_sleep_proposal_from_observation(
        proposal_id=proposal_id,
        submitted_token=submitted_token,
        local_session_id=local_session_id,
        secret=secret,
        selected=selected,
        session=session,
        calendar=calendar,
        now=now,
    )


def apply_sleep_proposal_from_observation(
    *,
    proposal_id: uuid.UUID,
    submitted_token: str,
    local_session_id: str,
    secret: bytes,
    selected: ActualSleepObservation | SleepObservationNoOp,
    session: Session,
    calendar: ApprovalCalendar,
    now: dt.datetime | None = None,
) -> SleepApplyResult:
    """Apply a proposal using a wearable observation already read."""

    if session.get_bind().dialect.name == "postgresql":
        with calendar_write_lock(session, calendar.backend.source):
            with activity_write_lock():
                return _apply_sleep_proposal_from_observation_locked(
                    proposal_id=proposal_id,
                    submitted_token=submitted_token,
                    local_session_id=local_session_id,
                    secret=secret,
                    selected=selected,
                    session=session,
                    calendar=calendar,
                    now=now,
                )
    return _apply_sleep_proposal_from_observation_locked(
        proposal_id=proposal_id,
        submitted_token=submitted_token,
        local_session_id=local_session_id,
        secret=secret,
        selected=selected,
        session=session,
        calendar=calendar,
        now=now,
    )


def _apply_sleep_proposal_from_observation_locked(
    *,
    proposal_id: uuid.UUID,
    submitted_token: str,
    local_session_id: str,
    secret: bytes,
    selected: ActualSleepObservation | SleepObservationNoOp,
    session: Session,
    calendar: ApprovalCalendar,
    now: dt.datetime | None = None,
) -> SleepApplyResult:
    backend = calendar.backend
    statement = (
        sa.select(SleepReconciliationProposal)
        .where(SleepReconciliationProposal.id == proposal_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if session.get_bind().dialect.name == "postgresql":
        lock_activity_write_plane(session)
        proposal = session.scalar(statement)
    else:
        proposal = session.scalar(statement)
    if proposal is None:
        return SleepApplyResult(SleepProposalStatus.INVALID, None)
    current_time = now or dt.datetime.now(dt.UTC)
    if proposal.status not in {
        SleepProposalStatus.PENDING,
        SleepProposalStatus.APPLYING,
    }:
        return SleepApplyResult(proposal.status, proposal.receipt)
    if backend.source is not proposal.calendar_source:
        return _close(
            session,
            proposal,
            SleepProposalStatus.CONFLICTED,
            current_time,
        )
    if (
        proposal.provider_state.get("account_generation")
        != calendar.account_generation
    ):
        return _close(
            session,
            proposal,
            SleepProposalStatus.CONFLICTED,
            current_time,
        )
    expected = approval_token(proposal, local_session_id, secret)
    if not hmac.compare_digest(submitted_token, expected):
        return _close(
            session,
            proposal,
            SleepProposalStatus.INVALID,
            current_time,
        )
    recovering = proposal.status is SleepProposalStatus.APPLYING
    if recovering and not _applying_is_stale(proposal, current_time):
        return SleepApplyResult(
            SleepProposalStatus.APPLYING,
            proposal.receipt,
        )
    if not recovering and current_time >= _aware(proposal.expires_at):
        return _close(
            session,
            proposal,
            SleepProposalStatus.EXPIRED,
            current_time,
        )

    if isinstance(selected, SleepObservationNoOp):
        return _close(session, proposal, SleepProposalStatus.CONFLICTED, current_time)
    if observation_fingerprint(selected) != proposal.observation_fingerprint:
        return _close(session, proposal, SleepProposalStatus.CONFLICTED, current_time)
    if (
        not recovering
        and capture_provider_state(session, calendar, selected)
        != proposal.provider_state
    ):
        return _close(session, proposal, SleepProposalStatus.CONFLICTED, current_time)

    claimed, owns_claim = _claim_apply(session, proposal, current_time)
    if not owns_claim or claimed.status is not SleepProposalStatus.APPLYING:
        return SleepApplyResult(claimed.status, claimed.receipt)
    claim_updated_at = current_time
    try:
        result = SleepCalendarReconciler(
            session,
            backend,
            account_generation=calendar.account_generation,
        ).reconcile(selected)
        receipt = _read_back(backend, selected, result)
        return _mark_applied(
            session,
            claimed.id,
            receipt,
            current_time,
            claim_updated_at,
        )
    except SQLAlchemyError:
        session.rollback()
        recovered = _recover_exact_apply(
            session,
            claimed.id,
            selected,
            backend,
            claimed.provider_state,
            calendar.account_generation,
            current_time,
            claim_updated_at,
        )
        if recovered is not None:
            return recovered
        raise
    except (CalendarError, SleepReadBackError):
        session.rollback()
        raise


def decline_sleep_proposal(
    session: Session,
    proposal_id: uuid.UUID,
    now: dt.datetime | None = None,
) -> SleepApplyResult:
    if session.get_bind().dialect.name == "postgresql":
        with activity_write_lock():
            lock_activity_write_plane(session)
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
    else:
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
    children = calendar_observations(observation)
    external_ids = result.external_ids or (result.external_id,)
    if len(children) != len(external_ids):
        raise SleepReadBackError("calendar read-back event count did not match")
    remotes = []
    for child, external_id in zip(children, external_ids, strict=True):
        remote = backend.read_event(external_id)
        expected_identity = actual_sleep_identity(child)
        if (
            not remote.is_agent_created
            or remote.identity != expected_identity
            or remote.start_at != ensure_utc(child.start_at)
            or remote.end_at != ensure_utc(child.end_at)
        ):
            raise SleepReadBackError(
                "calendar read-back did not match the approved actual sleep"
            )
        remotes.append(remote)
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
        "events": [redacted_digest(external_id) for external_id in external_ids],
        "segments": [
            {
                "start": remote.start_at.isoformat(),
                "wake_time": remote.end_at.isoformat(),
            }
            for remote in remotes
        ],
        "start": ensure_utc(observation.start_at).isoformat(),
        "wake_time": ensure_utc(observation.end_at).isoformat(),
        "planned_sleep_deleted": len(result.deleted_planned_external_ids),
        "stale_actual_sleep_deleted": len(result.deleted_actual_external_ids),
        "read_back_at": dt.datetime.now(dt.UTC).isoformat(),
    }


def _claim_apply(
    session: Session,
    proposal: SleepReconciliationProposal,
    claimed_at: dt.datetime,
) -> tuple[SleepReconciliationProposal, bool]:
    observed_status = proposal.status
    observed_updated_at = proposal.updated_at
    conditions = [
        SleepReconciliationProposal.id == proposal.id,
        SleepReconciliationProposal.status == observed_status,
    ]
    if observed_status is SleepProposalStatus.APPLYING:
        conditions.append(
            SleepReconciliationProposal.updated_at == observed_updated_at
        )
    result = session.execute(
        sa.update(SleepReconciliationProposal)
        .where(*conditions)
        .values(
            status=SleepProposalStatus.APPLYING,
            consumed_at=None,
            updated_at=claimed_at,
        )
    )
    session.commit()
    current = session.get(SleepReconciliationProposal, proposal.id)
    if current is None:
        raise RuntimeError("sleep proposal disappeared while claiming apply")
    if result.rowcount != 1:
        session.refresh(current)
    return current, result.rowcount == 1


def _mark_applied(
    session: Session,
    proposal_id: uuid.UUID,
    receipt: dict[str, Any],
    consumed_at: dt.datetime,
    claim_updated_at: dt.datetime,
) -> SleepApplyResult:
    result = session.execute(
        sa.update(SleepReconciliationProposal)
        .where(
            SleepReconciliationProposal.id == proposal_id,
            SleepReconciliationProposal.status == SleepProposalStatus.APPLYING,
            SleepReconciliationProposal.updated_at == claim_updated_at,
        )
        .values(
            status=SleepProposalStatus.APPLIED,
            consumed_at=consumed_at,
            receipt=receipt,
            updated_at=consumed_at,
        )
    )
    session.commit()
    if result.rowcount == 1:
        return SleepApplyResult(SleepProposalStatus.APPLIED, receipt)
    current = session.get(SleepReconciliationProposal, proposal_id)
    if current is None:
        return SleepApplyResult(SleepProposalStatus.INVALID, None)
    return SleepApplyResult(current.status, current.receipt)


def _recover_exact_apply(
    session: Session,
    proposal_id: uuid.UUID,
    observation: ActualSleepObservation,
    backend: CalendarBackend,
    provider_state: dict[str, Any],
    account_generation: str | None,
    recovered_at: dt.datetime,
    claim_updated_at: dt.datetime,
) -> SleepApplyResult | None:
    try:
        result = SleepCalendarReconciler(
            session,
            backend,
            account_generation=account_generation,
        ).reconcile(observation)
        planned_ids = tuple(
            str(item["external_id"])
            for item in provider_state.get("planned", ())
            if isinstance(item, dict) and item.get("external_id")
        )
        if planned_ids and not result.deleted_planned_external_ids:
            result = dataclass_replace(
                result,
                deleted_planned_external_ids=planned_ids,
            )
        receipt = _read_back(backend, observation, result)
        receipt["recovered"] = True
        return _mark_applied(
            session,
            proposal_id,
            receipt,
            recovered_at,
            claim_updated_at,
        )
    except (CalendarError, SleepReadBackError, SQLAlchemyError):
        session.rollback()
        return None


def _applying_is_stale(
    proposal: SleepReconciliationProposal,
    now: dt.datetime,
) -> bool:
    return now - _aware(proposal.updated_at) >= APPLYING_RECOVERY_DELAY


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
