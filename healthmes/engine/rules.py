"""Pure trigger rules: deterministic functions from context to fire-or-not.

Every proactive alert in HealthMes is gated by one of these rules — the LLM
never decides on its own to push (docs/PLAN.md section 4 and section 11).
Each rule is a pure function ``(TriggerContext) -> TriggerFire | None``:

- no I/O — ``healthmes/engine/triggers.py`` assembles the context from the
  store and the open-wearables client and applies cooldown/budget/quiet-hours
  gating afterwards;
- a stable ``dedup_key`` — the same logical occurrence always produces the
  same key, so the evaluator can drop repeats across 10-minute sweeps;
- a JSON-serializable ``evidence`` dict — persisted in
  ``trigger_event.payload`` and pre-filled into the decision tree as the
  deterministic "considered inputs" (docs/PLAN.md section 5).
"""

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

__all__ = [
    "TriggerFire",
    "StressSnapshot",
    "RecoverySnapshot",
    "AfternoonLoad",
    "ScheduleChange",
    "DeadlineTask",
    "RuleThresholds",
    "TriggerContext",
    "TriggerRule",
    "ALL_RULES",
    "stress_spike_vs_baseline",
    "low_recovery_heavy_afternoon",
    "schedule_changed",
    "deadline_risk",
]


@dataclass(frozen=True, slots=True)
class TriggerFire:
    """One rule firing, ready to be persisted and pushed as an alert.

    ``summary`` and ``proposal`` are single lines feeding the notification
    grammar (docs/PLAN.md section 8.5): observation / evidence / proposal.
    """

    rule_id: str
    dedup_key: str
    summary: str
    proposal: str
    evidence: dict[str, Any]


# ---------------------------------------------------------------------------
# Context inputs (assembled by triggers.py, consumed read-only by the rules)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StressSnapshot:
    """Recent stress reading vs. the personal trailing baseline.

    Values are on the Garmin stress scale 0-100 (the only native STRESS
    provider — vendor/open-wearables/backend/app/constants/health_scores.py);
    non-Garmin devices substitute an HRV-derived proxy (``source`` says which).
    """

    recent_value: float
    recent_at: datetime
    baseline_median: float | None
    baseline_days: int
    source: str = "garmin_stress"


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    """Today's best recovery-like score, normalized to 0-100 (higher=better).

    Backed by BODY_BATTERY / RECOVERY / READINESS health scores; the reader
    normalizes provider scales before building this snapshot.
    """

    value: float
    category: str
    provider: str | None
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class AfternoonLoad:
    """Booked calendar load for the *remaining* afternoon of ``day``.

    ``busy_minutes`` sums event overlap with the remaining afternoon window;
    parallel bookings count twice on purpose (they proxy context switching).
    """

    day: date
    busy_minutes: int
    event_count: int
    summaries: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScheduleChange:
    """One entry of the calendar sync diff (docs/PLAN.md section 6).

    Produced by the trigger context builder from recently-updated
    ``calendar_event_mirror`` rows. ``conflicts`` lists labels of agent blocks
    or accepted proposals the changed event now overlaps; ``fingerprint``
    identifies this exact revision (etag / updated timestamp) so an unchanged
    diff never re-fires.
    """

    external_id: str
    calendar_source: str
    summary: str | None
    kind: str  # "created" | "moved" | "cancelled"
    starts_at: datetime | None
    ends_at: datetime | None
    is_agent_created: bool
    conflicts: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class DeadlineTask:
    """A not-done task inside the deadline horizon, with its scheduled cover.

    ``scheduled_minutes`` counts future accepted/pushed proposal minutes for
    the task; the rule compares it against ``est_minutes`` to spot shortfalls.
    """

    task_id: str
    title: str
    deadline: datetime
    est_minutes: int | None
    scheduled_minutes: int
    status: str


@dataclass(frozen=True, slots=True)
class RuleThresholds:
    """Tunable rule constants (deterministic defaults, no ML).

    Kept here rather than in ``Settings`` so rules stay self-contained and
    unit-testable; the evaluator may inject a customized instance.
    """

    # stress_spike_vs_baseline (Garmin stress scale 0-100)
    stress_spike_min_value: float = 70.0
    stress_spike_baseline_ratio: float = 1.3
    stress_min_baseline_days: int = 3

    # low_recovery_heavy_afternoon (normalized 0-100 recovery, minutes booked)
    low_recovery_max_value: float = 40.0
    heavy_afternoon_min_busy_minutes: int = 180
    afternoon_start_hour: int = 12
    afternoon_end_hour: int = 18

    # deadline_risk
    deadline_risk_horizon_hours: int = 48


@dataclass(frozen=True, slots=True)
class TriggerContext:
    """Everything a rule may look at for one evaluation sweep.

    ``now`` is timezone-aware local time; date-scoped dedup keys derive from
    it. Missing signals are ``None``/empty — rules must skip, not guess
    (providers vary wildly in coverage, docs/PLAN.md section 1.5).
    """

    now: datetime
    stress: StressSnapshot | None = None
    recovery: RecoverySnapshot | None = None
    afternoon: AfternoonLoad | None = None
    schedule_changes: tuple[ScheduleChange, ...] = ()
    deadline_tasks: tuple[DeadlineTask, ...] = ()
    thresholds: RuleThresholds = field(default_factory=RuleThresholds)


TriggerRule = Callable[[TriggerContext], TriggerFire | None]


def _digest(parts: Iterable[str]) -> str:
    """Stable short digest of an (order-insensitive) set of strings."""
    material = "\n".join(sorted(parts))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def stress_spike_vs_baseline(ctx: TriggerContext) -> TriggerFire | None:
    """Fire when the latest stress reading spikes above the personal baseline.

    Requires an absolute floor (a "spike" from 20 to 30 is noise) AND a
    relative jump vs. the trailing baseline median, with enough baseline days
    to trust the comparison. Deduplicated per local day.
    """
    stress = ctx.stress
    thresholds = ctx.thresholds
    if stress is None or stress.baseline_median is None or stress.baseline_median <= 0:
        return None
    if stress.baseline_days < thresholds.stress_min_baseline_days:
        return None
    if stress.recent_value < thresholds.stress_spike_min_value:
        return None
    ratio = stress.recent_value / stress.baseline_median
    if ratio < thresholds.stress_spike_baseline_ratio:
        return None

    return TriggerFire(
        rule_id="stress_spike_vs_baseline",
        dedup_key=f"stress_spike_vs_baseline:{ctx.now.date().isoformat()}",
        summary=(
            f"Stress is {stress.recent_value:.0f}/100, "
            f"{ratio:.1f}x the {stress.baseline_days}-day baseline "
            f"of {stress.baseline_median:.0f}."
        ),
        proposal=(
            "Suggest a short recovery break now and consider moving the next "
            "high-focus block."
        ),
        evidence={
            "recent_value": round(stress.recent_value, 1),
            "recent_at": _iso(stress.recent_at),
            "baseline_median": round(stress.baseline_median, 1),
            "baseline_days": stress.baseline_days,
            "ratio": round(ratio, 2),
            "source": stress.source,
        },
    )


def low_recovery_heavy_afternoon(ctx: TriggerContext) -> TriggerFire | None:
    """Fire when today's recovery is low but the remaining afternoon is heavy.

    The mismatch between body state and booked load is what makes this
    actionable: the agent should propose lightening the afternoon.
    Deduplicated per local day.
    """
    recovery = ctx.recovery
    afternoon = ctx.afternoon
    thresholds = ctx.thresholds
    if recovery is None or afternoon is None:
        return None
    if recovery.value > thresholds.low_recovery_max_value:
        return None
    if afternoon.busy_minutes < thresholds.heavy_afternoon_min_busy_minutes:
        return None

    return TriggerFire(
        rule_id="low_recovery_heavy_afternoon",
        dedup_key=f"low_recovery_heavy_afternoon:{afternoon.day.isoformat()}",
        summary=(
            f"Recovery is low ({recovery.value:.0f}/100 {recovery.category}) "
            f"with {afternoon.busy_minutes} booked minutes left this afternoon "
            f"({afternoon.event_count} events)."
        ),
        proposal=(
            "Propose lightening the afternoon: move or shorten non-critical "
            "blocks and keep only low-energy work."
        ),
        evidence={
            "recovery_value": round(recovery.value, 1),
            "recovery_category": recovery.category,
            "recovery_provider": recovery.provider,
            "recovery_recorded_at": _iso(recovery.recorded_at),
            "afternoon_busy_minutes": afternoon.busy_minutes,
            "afternoon_event_count": afternoon.event_count,
            "afternoon_events": list(afternoon.summaries),
        },
    )


def schedule_changed(ctx: TriggerContext) -> TriggerFire | None:
    """Fire when external calendar changes collide with the current plan.

    Consumes the calendar sync diff (docs/PLAN.md section 6): a change is
    plan-relevant when the user externally touched an agent-created block
    (external calendar wins → the agent must re-plan) or when an external
    event now conflicts with agent blocks / accepted proposals. All relevant
    changes of one sweep are aggregated into a single fire; the dedup key is
    a digest of the revision fingerprints, so the same diff never re-fires
    while a genuinely new change produces a new key.
    """
    relevant = tuple(
        change for change in ctx.schedule_changes if change.is_agent_created or change.conflicts
    )
    if not relevant:
        return None

    labels = [change.summary or change.external_id for change in relevant]
    return TriggerFire(
        rule_id="schedule_changed",
        dedup_key=f"schedule_changed:{_digest(change.fingerprint for change in relevant)}",
        summary=(
            f"{len(relevant)} external calendar change(s) affect the current "
            f"plan: {', '.join(labels[:3])}"
            + ("..." if len(labels) > 3 else "")
        ),
        proposal=(
            "Re-plan the affected blocks around the new calendar state and "
            "confirm the updated schedule with the user."
        ),
        evidence={
            "changes": [
                {
                    "external_id": change.external_id,
                    "calendar_source": change.calendar_source,
                    "summary": change.summary,
                    "kind": change.kind,
                    "starts_at": _iso(change.starts_at),
                    "ends_at": _iso(change.ends_at),
                    "is_agent_created": change.is_agent_created,
                    "conflicts": list(change.conflicts),
                }
                for change in relevant
            ],
        },
    )


def deadline_risk(ctx: TriggerContext) -> TriggerFire | None:
    """Fire when tasks near their deadline lack enough scheduled time.

    A task is at risk when its deadline falls inside the horizon and its
    future accepted/pushed blocks cover less than the estimate (or nothing is
    scheduled at all when no estimate exists). One aggregated fire per
    distinct at-risk set; the key includes each task's deadline date so a
    replanned deadline re-fires while a static situation does not.
    """
    thresholds = ctx.thresholds
    horizon = ctx.now + timedelta(hours=thresholds.deadline_risk_horizon_hours)

    at_risk: list[DeadlineTask] = []
    for task in ctx.deadline_tasks:
        if task.status in ("done", "cancelled"):
            continue
        if task.deadline > horizon:
            continue
        if task.est_minutes is None:
            if task.scheduled_minutes > 0:
                continue
        elif task.scheduled_minutes >= task.est_minutes:
            continue
        at_risk.append(task)

    if not at_risk:
        return None

    at_risk.sort(key=lambda task: task.deadline)
    titles = [task.title for task in at_risk]
    return TriggerFire(
        rule_id="deadline_risk",
        dedup_key=(
            "deadline_risk:"
            + _digest(f"{task.task_id}:{task.deadline.date().isoformat()}" for task in at_risk)
        ),
        summary=(
            f"{len(at_risk)} task(s) at risk before their deadline: "
            f"{', '.join(titles[:3])}"
            + ("..." if len(titles) > 3 else "")
        ),
        proposal=(
            "Propose concrete time blocks before the deadlines and ask the "
            "user to confirm or reprioritize."
        ),
        evidence={
            "horizon_hours": thresholds.deadline_risk_horizon_hours,
            "tasks": [
                {
                    "task_id": task.task_id,
                    "title": task.title,
                    "deadline": _iso(task.deadline),
                    "est_minutes": task.est_minutes,
                    "scheduled_minutes": task.scheduled_minutes,
                    "shortfall_minutes": (
                        max(task.est_minutes - task.scheduled_minutes, 0)
                        if task.est_minutes is not None
                        else None
                    ),
                    "status": task.status,
                }
                for task in at_risk
            ],
        },
    )


ALL_RULES: tuple[TriggerRule, ...] = (
    stress_spike_vs_baseline,
    low_recovery_heavy_afternoon,
    schedule_changed,
    deadline_risk,
)
