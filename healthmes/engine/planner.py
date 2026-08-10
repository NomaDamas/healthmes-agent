"""Deterministic self-hosted schedule proposal planner.

This module fills the local-runtime gap between stored tasks and the existing
``ScheduleProposal`` approval contract. It never writes a calendar directly:
one safe candidate is proposed, the owner approves it, and the existing
calendar job performs the provider write.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from healthmes.calendars.adjustments import HANDLE_TTL, issue_reply_handle
from healthmes.calendars.adjustments_types import START_SAFETY_LEAD
from healthmes.calendars.jobs import write_source
from healthmes.config import Settings, resolve_timezone
from healthmes.engine.cognitive_energy import (
    STATUS_OK,
    CognitiveEnergyEngine,
    WindowSlot,
)
from healthmes.schedule_outcomes import record_invalidation_outcome
from healthmes.store import (
    CalendarEventMirror,
    CognitiveEnergyEstimate,
    DecisionKind,
    DecisionRecord,
    EnergyDemand,
    ProposalStatus,
    ScheduleProposal,
    Task,
    TriggerEvent,
)
from healthmes.store.session import session_scope

logger = logging.getLogger(__name__)

PLANNER_INTERVAL_MINUTES = 10
PLANNING_HORIZON_DAYS = 7
WORKDAY_START = dt.time(8)
WORKDAY_END = dt.time(20)
SLOT_STEP = dt.timedelta(minutes=15)
MIN_START_LEAD = dt.timedelta(minutes=30)
DECLINE_COOLDOWN = dt.timedelta(hours=24)

ENERGY_FLOORS = {
    EnergyDemand.LOW: None,
    EnergyDemand.MED: 45,
    EnergyDemand.HIGH: 65,
}


@dataclass(frozen=True, slots=True)
class PlannerResult:
    status: str
    reason: str
    task_id: UUID | None = None
    proposal_id: UUID | None = None


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _ceil_to_step(value: dt.datetime) -> dt.datetime:
    value = _utc(value)
    seconds = int(SLOT_STEP.total_seconds())
    timestamp = int(value.timestamp())
    rounded = ((timestamp + seconds - 1) // seconds) * seconds
    return dt.datetime.fromtimestamp(rounded, tz=dt.UTC)


def _merge_intervals(
    intervals: list[tuple[dt.datetime, dt.datetime]],
) -> list[tuple[dt.datetime, dt.datetime]]:
    merged: list[tuple[dt.datetime, dt.datetime]] = []
    for start, end in sorted((_utc(start), _utc(end)) for start, end in intervals):
        if end <= start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _overlaps_busy(
    start: dt.datetime,
    end: dt.datetime,
    busy: list[tuple[dt.datetime, dt.datetime]],
) -> bool:
    return any(busy_start < end and busy_end > start for busy_start, busy_end in busy)


def _energy_scores(
    estimates: list[CognitiveEnergyEstimate | WindowSlot],
    start: dt.datetime,
    end: dt.datetime,
) -> list[int]:
    overlapping = sorted(
        (
            max(_utc(row.window_start), start),
            min(_utc(row.window_end), end),
            row.score,
        )
        for row in estimates
        if _utc(row.window_start) < end and _utc(row.window_end) > start
    )
    cursor = start
    scores: list[int] = []
    for window_start, window_end, score in overlapping:
        if window_start > cursor:
            return []
        if window_end > cursor:
            cursor = window_end
            scores.append(score)
        if cursor >= end:
            return scores
    return []


def _candidate_days(
    earliest: dt.datetime,
    latest: dt.datetime,
    timezone: dt.tzinfo,
) -> list[dt.date]:
    first = earliest.astimezone(timezone).date()
    last = latest.astimezone(timezone).date()
    return [first + dt.timedelta(days=offset) for offset in range((last - first).days + 1)]


def _find_slot(
    *,
    task: Task,
    now: dt.datetime,
    timezone: dt.tzinfo,
    busy: list[tuple[dt.datetime, dt.datetime]],
    estimates: list[CognitiveEnergyEstimate],
) -> tuple[dt.datetime, dt.datetime, list[int]] | None:
    if task.est_minutes is None or task.est_minutes <= 0:
        return None

    earliest = _ceil_to_step(now + MIN_START_LEAD)
    horizon_end = earliest + dt.timedelta(days=PLANNING_HORIZON_DAYS)
    if task.deadline is not None:
        horizon_end = min(horizon_end, _utc(task.deadline))
    duration = dt.timedelta(minutes=task.est_minutes)
    if horizon_end - earliest < duration:
        return None

    floor = ENERGY_FLOORS[task.energy_demand]
    candidates: list[tuple[float, dt.datetime, dt.datetime, list[int]]] = []
    for day in _candidate_days(earliest, horizon_end, timezone):
        local_start = dt.datetime.combine(day, WORKDAY_START, tzinfo=timezone)
        local_end = dt.datetime.combine(day, WORKDAY_END, tzinfo=timezone)
        cursor = max(earliest, local_start.astimezone(dt.UTC))
        day_end = min(horizon_end, local_end.astimezone(dt.UTC))
        cursor = _ceil_to_step(cursor)
        while cursor + duration <= day_end:
            end = cursor + duration
            if not _overlaps_busy(cursor, end, busy):
                scores = _energy_scores(estimates, cursor, end)
                if floor is None or (scores and min(scores) >= floor):
                    rank = sum(scores) / len(scores) if scores else -1.0
                    candidates.append((rank, cursor, end, scores))
            cursor += SLOT_STEP

    if not candidates:
        return None
    if floor is None:
        _rank, start, end, scores = min(candidates, key=lambda item: item[1])
    else:
        _rank, start, end, scores = min(
            candidates,
            key=lambda item: (-item[0], item[1]),
        )
    return start, end, scores


def _select_tasks(session: Session, current: dt.datetime) -> list[Task]:
    active_task_ids = select(ScheduleProposal.task_id).where(
        or_(
            ScheduleProposal.status.in_(
                [ProposalStatus.ACCEPTED, ProposalStatus.PUSHED]
            ),
            (
                (ScheduleProposal.status == ProposalStatus.PROPOSED)
                & (ScheduleProposal.expires_at > current)
            ),
            (
                (ScheduleProposal.status == ProposalStatus.DECLINED)
                & (ScheduleProposal.decided_at > current - DECLINE_COOLDOWN)
            ),
        )
    )
    rows = list(
        session.scalars(
            select(Task).where(
                Task.status == "todo",
                Task.est_minutes.is_not(None),
                ~Task.id.in_(active_task_ids),
            )
        ).all()
    )
    rows.sort(
        key=lambda task: (
            task.deadline is None,
            _utc(task.deadline).isoformat() if task.deadline is not None else "",
            {
                EnergyDemand.HIGH: 0,
                EnergyDemand.MED: 1,
                EnergyDemand.LOW: 2,
            }[task.energy_demand],
            _utc(task.created_at).isoformat(),
        )
    )
    return rows


def _decision_tree(
    task: Task,
    start: dt.datetime,
    end: dt.datetime,
    scores: list[int],
    timezone: dt.tzinfo,
) -> dict[str, object]:
    threshold = ENERGY_FLOORS[task.energy_demand]
    return {
        "id": "deterministic_schedule_proposal",
        "type": "rule",
        "label": "smallest safe calendar placement",
        "detail": {
            "planner": "healthmes-deterministic-v1",
            "confirmation_required": True,
            "calendar_write": False,
        },
        "children": [
            {
                "id": "task",
                "type": "input",
                "label": "task constraints",
                "detail": {
                    "task_id": str(task.id),
                    "title": task.title,
                    "estimated_minutes": task.est_minutes,
                    "deadline": (
                        _utc(task.deadline).isoformat()
                        if task.deadline is not None
                        else None
                    ),
                    "energy_demand": task.energy_demand.value,
                },
                "children": [],
            },
            {
                "id": "availability",
                "type": "rule",
                "label": "calendar availability",
                "detail": {
                    "working_hours_local": "08:00-20:00",
                    "slot_step_minutes": int(SLOT_STEP.total_seconds() // 60),
                    "conflict_free": True,
                },
                "children": [],
            },
            {
                "id": "energy",
                "type": "rule",
                "label": "cognitive energy fit",
                "detail": {
                    "required_floor": threshold,
                    "observed_scores": scores,
                    "insufficient_data_allowed": threshold is None,
                },
                "children": [],
            },
            {
                "id": "action",
                "type": "action",
                "label": "ask owner before changing calendar",
                "detail": {
                    "before": None,
                    "after": {
                        "start_at": start.astimezone(timezone).isoformat(),
                        "end_at": end.astimezone(timezone).isoformat(),
                    },
                },
                "children": [],
            },
        ],
    }


def propose_next_safe_block(
    session: Session,
    settings: Settings,
    *,
    now: dt.datetime | None = None,
    forecast: list[WindowSlot] | None = None,
) -> PlannerResult:
    current = _utc(now or dt.datetime.now(dt.UTC))
    if write_source(settings) is None:
        return PlannerResult("skipped", "no_calendar_writer")

    handle_secret = settings.calendar_adjustment_secret.get_secret_value().strip()
    if len(handle_secret) < 32:
        return PlannerResult("skipped", "approval_secret_unavailable")

    timezone = resolve_timezone(settings)
    horizon_end = current + dt.timedelta(days=PLANNING_HORIZON_DAYS + 1)
    busy = _merge_intervals(
        [
            (row.start_at, row.end_at)
            for row in session.scalars(
                select(CalendarEventMirror).where(
                    CalendarEventMirror.start_at < horizon_end,
                    CalendarEventMirror.end_at > current,
                    CalendarEventMirror.is_all_day.is_(False),
                    or_(
                        CalendarEventMirror.status.is_(None),
                        CalendarEventMirror.status != "cancelled",
                    ),
                )
            ).all()
        ]
        + [
            (row.proposed_start, row.proposed_end)
            for row in session.scalars(
                select(ScheduleProposal).where(
                    ScheduleProposal.status.in_(
                        [ProposalStatus.PROPOSED, ProposalStatus.ACCEPTED]
                    ),
                    ScheduleProposal.proposed_start < horizon_end,
                    ScheduleProposal.proposed_end > current,
                )
            ).all()
        ]
    )
    expired = list(
        session.scalars(
            select(ScheduleProposal).where(
                ScheduleProposal.status == ProposalStatus.PROPOSED,
                ScheduleProposal.expires_at <= current,
            )
        ).all()
    )
    for proposal in expired:
        proposal.status = ProposalStatus.INVALIDATED
        record_invalidation_outcome(session, proposal, reason="proposal_expired")

    estimates: list[CognitiveEnergyEstimate | WindowSlot] = list(
        session.scalars(
            select(CognitiveEnergyEstimate).where(
                CognitiveEnergyEstimate.window_start < horizon_end,
                CognitiveEnergyEstimate.window_end > current,
            )
        ).all()
    )
    estimates.extend(
        slot
        for slot in (forecast or [])
        if slot.status == STATUS_OK and slot.score is not None
    )

    tasks = _select_tasks(session, current)
    if not tasks:
        return PlannerResult("skipped", "no_schedulable_task")
    for task in tasks:
        slot = _find_slot(
            task=task,
            now=current,
            timezone=timezone,
            busy=busy,
            estimates=estimates,
        )
        if slot is None:
            continue
        start, end, scores = slot
        trigger = TriggerEvent(
            fired_at=current,
            rule_id="deterministic_planner",
            dedup_key=f"planner:{task.id}:{start.isoformat()}",
            alert_sent=False,
            payload={
                "summary": f"{task.title} 일정을 제안했습니다.",
                "evidence": {
                    "energy": task.energy_demand.value,
                    "duration_minutes": task.est_minutes,
                    "starts_at": start.astimezone(timezone).isoformat(),
                },
                "proposal": "캘린더에 추가할까요?",
                "push": {"state": "pending_hygiene"},
            },
        )
        session.add(trigger)
        session.flush()
        decision = DecisionRecord(
            kind=DecisionKind.SCHEDULE_CHANGE,
            summary=f"Proposed {task.title} at {start.astimezone(timezone):%Y-%m-%d %H:%M}",
            tree=_decision_tree(task, start, end, scores, timezone),
            llm_model=None,
            tokens=None,
            trigger_event_id=trigger.id,
        )
        session.add(decision)
        session.flush()
        handle = issue_reply_handle(handle_secret)
        expires_at = min(current + HANDLE_TTL, start - START_SAFETY_LEAD)
        if expires_at <= current:
            continue
        proposal = ScheduleProposal(
            task_id=task.id,
            proposed_start=start,
            proposed_end=end,
            status=ProposalStatus.PROPOSED,
            decision_record_id=decision.id,
            reply_handle_digest=handle.digest,
            expires_at=expires_at,
        )
        session.add(proposal)
        session.flush()
        return PlannerResult(
            "proposed",
            "safe_slot_found",
            task_id=task.id,
            proposal_id=proposal.id,
        )
    return PlannerResult("skipped", "no_safe_slot", task_id=tasks[0].id)


def build_planner_job(
    settings: Settings,
    *,
    session_factory: sessionmaker[Session] | None = None,
) -> Callable[[], None]:
    energy = CognitiveEnergyEngine(settings, session_factory=session_factory)

    def run() -> None:
        try:
            now = dt.datetime.now(dt.UTC)
            forecast = [
                slot
                for offset in range(PLANNING_HORIZON_DAYS + 1)
                for slot in energy.forecast_day((now + dt.timedelta(days=offset)).date())
            ]
            with session_scope(session_factory) as session:
                result = propose_next_safe_block(
                    session,
                    settings,
                    now=now,
                    forecast=forecast,
                )
            logger.info("Deterministic planner: %s (%s)", result.status, result.reason)
        except Exception:
            logger.exception("Deterministic planner sweep failed; retrying next interval.")

    return run
