from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from healthmes.calendars.base import CalendarBackend
from healthmes.calendars.sleep_event_rendering import observation_fingerprint
from healthmes.calendars.sleep_observation import SleepObservationNoOp
from healthmes.calendars.sleep_preview import preview_sleep_reconciliation
from healthmes.calendars.sleep_proposal_state import (
    capture_provider_state,
    redacted_provider_guard,
)
from healthmes.calendars.sleep_source import SleepSummaryReader, read_actual_sleep
from healthmes.store import (
    CalendarSource,
    SleepProposalStatus,
    SleepReconciliationProposal,
)

PROPOSAL_TTL = dt.timedelta(minutes=15)


async def prepare_sleep_proposal(
    *,
    target_date: dt.date,
    calendar_source: CalendarSource,
    reader: SleepSummaryReader,
    user_id: str,
    session: Session,
    backend: CalendarBackend,
    now: dt.datetime | None = None,
) -> SleepReconciliationProposal:
    selected = await read_actual_sleep(reader, user_id, target_date)
    created_at = now or dt.datetime.now(dt.UTC)
    if isinstance(selected, SleepObservationNoOp):
        snapshot: dict[str, Any] = {
            "status": "noop",
            "reason": selected.reason.value,
            "calendar": calendar_source.value,
            "local_date": target_date.isoformat(),
        }
        return _persist(
            session,
            calendar_source=calendar_source,
            local_date=target_date,
            source_key=f"oura:{target_date.isoformat()}",
            fingerprint=hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
            snapshot=snapshot,
            provider_state={"actual": None, "planned": []},
            status=SleepProposalStatus.NOOP,
            created_at=created_at,
        )

    preview = preview_sleep_reconciliation(
        session,
        calendar_source,
        selected,
        backend,
    )
    provider_state = capture_provider_state(session, backend, selected)
    snapshot = {**preview, "provider_guard": redacted_provider_guard(provider_state)}
    status = {
        "blocked": SleepProposalStatus.BLOCKED,
        "noop": SleepProposalStatus.NOOP,
    }.get(str(preview["action"]), SleepProposalStatus.PENDING)
    return _persist(
        session,
        calendar_source=calendar_source,
        local_date=target_date,
        source_key=selected.source_key,
        fingerprint=observation_fingerprint(selected),
        snapshot=snapshot,
        provider_state=provider_state,
        status=status,
        created_at=created_at,
    )


def _persist(
    session: Session,
    *,
    calendar_source: CalendarSource,
    local_date: dt.date,
    source_key: str,
    fingerprint: str,
    snapshot: dict[str, Any],
    provider_state: dict[str, Any],
    status: SleepProposalStatus,
    created_at: dt.datetime,
) -> SleepReconciliationProposal:
    base_key = hashlib.sha256(
        json.dumps(
            {
                "calendar": calendar_source.value,
                "fingerprint": fingerprint,
                "provider_state": provider_state,
                "snapshot": snapshot,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    current = session.scalar(
        sa.select(SleepReconciliationProposal)
        .where(
            SleepReconciliationProposal.dedup_key.like(f"{base_key}%"),
            SleepReconciliationProposal.status == SleepProposalStatus.PENDING,
        )
        .order_by(SleepReconciliationProposal.created_at.desc())
    )
    if current is not None:
        return current
    count = session.scalar(
        sa.select(sa.func.count())
        .select_from(SleepReconciliationProposal)
        .where(SleepReconciliationProposal.dedup_key.like(f"{base_key}%"))
    )
    dedup_key = base_key if not count else f"{base_key}:{count}"
    proposal = SleepReconciliationProposal(
        calendar_source=calendar_source,
        local_date=local_date,
        source_key=source_key,
        observation_fingerprint=fingerprint,
        snapshot=snapshot,
        provider_state=provider_state,
        status=status,
        expires_at=created_at + PROPOSAL_TTL,
        consumed_at=None,
        receipt=None,
        dedup_key=dedup_key,
    )
    session.add(proposal)
    session.commit()
    session.refresh(proposal)
    return proposal
