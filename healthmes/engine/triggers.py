"""Trigger evaluation loop: context assembly, rule sweep, alert hygiene, push.

Runs every ``TRIGGER_INTERVAL_MINUTES`` from the in-service APScheduler
(docs/PLAN.md section 4). One sweep:

1. read health signals through the mcp_server ow_client (open-wearables REST
   stays in that module — nothing is duplicated here) and load the schedule /
   task context from the healthmes store;
2. evaluate the pure rules in ``healthmes/engine/rules.py`` (each rule is
   exception-isolated: one broken rule cannot abort the sweep or roll back
   fires that were already pushed);
3. for each fire: drop it if its ``dedup_key`` was ever recorded (application
   -level uniqueness over ``trigger_event.dedup_key`` — keys embed their
   temporal scope, e.g. the local date or a diff fingerprint), otherwise
   persist a ``trigger_event`` row;
4. gate the push with the alert-hygiene settings (quiet hours, per-rule
   cooldown, daily alert budget — docs/PLAN.md section 11) and, if allowed,
   POST it to the Hermes gateway via ``healthmes/engine/webhook.py``.
   ``alert_sent`` marks the alert as delivered to the user: True after a
   confirmed 2xx webhook push, OR — when ``native_alert_delivery`` is on —
   whenever a fire passes the hygiene gates, so the native companion apps
   surface it via /v1/alerts + glance even without Telegram. Suppressed and
   webhook-only-failed pushes keep the row with the reason in
   ``payload["push"]`` and are NOT retried for the same dedup key (noise
   control beats redelivery).

Datetime convention: everything is normalized to UTC before it is persisted
or bound into a query, so comparisons behave identically on postgres
(timestamptz) and sqlite (naive ISO strings).
"""

import asyncio
import inspect
import logging
import statistics
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from healthmes.config import Settings, resolve_timezone
from healthmes.engine.rules import (
    ALL_RULES,
    AfternoonLoad,
    DeadlineTask,
    RecoverySnapshot,
    RuleThresholds,
    ScheduleChange,
    StressSnapshot,
    TriggerContext,
    TriggerFire,
    TriggerRule,
)
from healthmes.engine.webhook import HermesWebhookSender, WebhookResult
from healthmes.mcp_server import interpret
from healthmes.store.enums import ProposalStatus
from healthmes.store.models import CalendarEventMirror, ScheduleProposal, Task, TriggerEvent
from healthmes.store.session import session_scope

__all__ = [
    "TRIGGER_INTERVAL_MINUTES",
    "HealthSignals",
    "HealthReader",
    "AlertSender",
    "OwHealthReader",
    "FireOutcome",
    "EvaluationReport",
    "TriggerEvaluator",
    "build_trigger_job",
    "default_now_provider",
    "is_in_quiet_hours",
]

logger = logging.getLogger(__name__)

TRIGGER_INTERVAL_MINUTES = 10
# Sync-diff lookback; overlaps the sweep interval on purpose (dedup keys make
# double-processing harmless, gaps would silently drop changes).
SCHEDULE_DIFF_LOOKBACK_MINUTES = TRIGGER_INTERVAL_MINUTES + 5
# A stress sample only counts as "recent" this long (stale data must not
# trigger a spike alert hours later).
STRESS_RECENCY = timedelta(hours=3)
STRESS_BASELINE_DAYS = 14
# Terminal task statuses excluded from deadline risk (Task.status is a free
# str_32 with default "todo"; these two are the terminal conventions).
_TERMINAL_TASK_STATUSES = ("done", "cancelled")

_SUPPRESS_QUIET_HOURS = "quiet_hours"
_SUPPRESS_COOLDOWN = "cooldown"
_SUPPRESS_DAILY_BUDGET = "daily_budget"
_SUPPRESS_PUSH_FAILED = "push_failed"


def _ensure_utc(value: datetime) -> datetime:
    """Aware datetimes are converted to UTC; naive ones are assumed UTC.

    (sqlite's DATETIME round-trips as a naive string — this restores the UTC
    convention on read; on postgres timestamptz values come back aware.)
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def is_in_quiet_hours(moment: time, start: time, end: time) -> bool:
    """True when ``moment`` falls inside the do-not-disturb window.

    The window may wrap midnight (e.g. 22:30 -> 07:00). ``start == end``
    means a zero-length window, i.e. quiet hours disabled.
    """
    if start == end:
        return False
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end


# ---------------------------------------------------------------------------
# Health signals via the mcp_server ow_client
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HealthSignals:
    """Interpreted open-wearables signals for one sweep (absent -> None)."""

    stress: StressSnapshot | None = None
    recovery: RecoverySnapshot | None = None


class HealthReader(Protocol):
    """Anything that can produce ``HealthSignals`` for a sweep."""

    def read(self, now: datetime) -> HealthSignals: ...


class AlertSender(Protocol):
    """Anything that can push a fire to the agent plane (webhook by default)."""

    def send(self, fire: TriggerFire, *, fired_at: datetime) -> WebhookResult: ...


def _run_maybe_async(value: Any) -> Any:
    """Support both sync and async ow_client method results.

    The vendor client pattern (vendor/open-wearables/mcp/app/services/
    api_client.py) is async; the evaluator runs in a scheduler worker thread
    with no event loop, so awaitables are driven with ``asyncio.run``.
    """
    if inspect.isawaitable(value):

        async def _await() -> Any:
            return await value

        return asyncio.run(_await())
    return value


def _rows(response: Any) -> list[dict[str, Any]]:
    """Unwrap an open-wearables pagination envelope or a plain list.

    ``{"data": [...]}`` is the PaginatedResponse shape (health-scores etc.);
    ``{"items": [...]}`` is the OldPaginatedResponse shape of ``/users``
    (vendor backend/app/schemas/utils/pagination.py).
    """
    if isinstance(response, dict):
        data = response.get("data")
        if data is None:
            data = response.get("items", [])
    else:
        data = response
    return list(data or [])


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    if isinstance(value, str):
        try:
            return _ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


# Provider charge-scale normalization (vendor HEALTH_SCORE_RANGES) is shared
# domain logic living in healthmes/mcp_server/interpret.py — one copy for the
# trigger sweep, the energy engine and the MCP tools.
_normalize_recovery = interpret.normalize_recovery


class OwHealthReader:
    """Reads rule inputs through the healthmes mcp_server ow_client.

    All open-wearables REST mechanics (base URL, API key header, pagination)
    live in ``healthmes/mcp_server/ow_client.py`` — this class only turns
    client responses into ``StressSnapshot`` / ``RecoverySnapshot``. The
    import is lazy and every failure degrades to empty signals, so the
    10-minute loop keeps serving the store-driven rules even while the
    client module or the backend is unavailable.
    """

    def __init__(
        self,
        settings: Settings,
        client: Any | None = None,
        *,
        stress_recency: timedelta = STRESS_RECENCY,
        baseline_days: int = STRESS_BASELINE_DAYS,
    ) -> None:
        self._settings = settings
        self._client = client
        self._user_id: str | None = None
        self._stress_recency = stress_recency
        self._baseline_days = baseline_days
        self._warned = False

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        # Deferred import: the REST client is owned by the MCP-server scope;
        # importing it lazily keeps engine imports store-only and lets the
        # sweep degrade instead of crash if that module is missing/broken.
        from healthmes.mcp_server.ow_client import OWClient

        self._client = OWClient.from_settings(self._settings)
        return self._client

    def _ensure_user_id(self, client: Any) -> str:
        """Resolve the deployment's subject via the shared single-user policy.

        Settings.ow_user_id / HEALTHMES_OW_USER_ID win; discovery is accepted
        only when the API key sees exactly one user — never "the first user",
        which would silently read someone else's data on a multi-user backend
        (healthmes/mcp_server/ow_client.py::resolve_single_user_id).
        """
        if self._user_id is None:
            from healthmes.mcp_server.ow_client import resolve_single_user_id

            self._user_id = _run_maybe_async(resolve_single_user_id(client, self._settings))
        return self._user_id

    def _get_scores(
        self, client: Any, user_id: str, category: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        response = _run_maybe_async(
            client.get_health_scores(
                user_id=user_id,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                category=category,
            )
        )
        return _rows(response)

    def read(self, now: datetime) -> HealthSignals:
        try:
            client = self._ensure_client()
            user_id = self._ensure_user_id(client)
            stress = self._read_stress(client, user_id, now)
            recovery = self._read_recovery(client, user_id, now)
            self._warned = False
            return HealthSignals(stress=stress, recovery=recovery)
        except Exception as exc:  # degrade, never break the sweep
            if not self._warned:
                logger.warning(
                    "Health signals unavailable (%s: %s); store-driven rules "
                    "continue without them.",
                    type(exc).__name__,
                    exc,
                )
                self._warned = True
            return HealthSignals()

    def _read_stress(self, client: Any, user_id: str, now: datetime) -> StressSnapshot | None:
        start = now - timedelta(days=self._baseline_days)
        rows = self._get_scores(client, user_id, "stress", start, now)

        today: date = now.date()
        recent_value: float | None = None
        recent_at: datetime | None = None
        by_day: dict[date, list[float]] = defaultdict(list)
        for row in rows:
            recorded_at = _parse_dt(row.get("recorded_at"))
            value = row.get("value")
            if recorded_at is None or value is None:
                continue
            local_recorded = recorded_at.astimezone(now.tzinfo)
            if local_recorded.date() >= today:
                if now - recorded_at <= self._stress_recency and (
                    recent_at is None or recorded_at > recent_at
                ):
                    recent_at = recorded_at
                    recent_value = float(value)
            else:
                by_day[local_recorded.date()].append(float(value))

        if recent_value is None or recent_at is None or not by_day:
            return None
        daily_means = [statistics.fmean(values) for values in by_day.values()]
        return StressSnapshot(
            recent_value=recent_value,
            recent_at=recent_at,
            baseline_median=statistics.median(daily_means),
            baseline_days=len(by_day),
            source="garmin_stress",
        )

    def _read_recovery(self, client: Any, user_id: str, now: datetime) -> RecoverySnapshot | None:
        day_start = datetime.combine(now.date(), time(0, 0), tzinfo=now.tzinfo)
        # Preference order: the most direct "charge" signal first.
        for category in ("body_battery", "recovery", "readiness"):
            rows = self._get_scores(client, user_id, category, day_start, now)
            best_at: datetime | None = None
            best: dict[str, Any] | None = None
            for row in rows:
                recorded_at = _parse_dt(row.get("recorded_at"))
                if recorded_at is None or row.get("value") is None:
                    continue
                if best_at is None or recorded_at > best_at:
                    best_at = recorded_at
                    best = row
            if best is not None and best_at is not None:
                provider = best.get("provider")
                return RecoverySnapshot(
                    value=_normalize_recovery(category, provider, float(best["value"])),
                    category=category,
                    provider=str(provider) if provider is not None else None,
                    recorded_at=best_at,
                )
        return None


# ---------------------------------------------------------------------------
# Store-driven context
# ---------------------------------------------------------------------------


def _overlap_minutes(
    start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime
) -> int:
    overlap = min(end_a, end_b) - max(start_a, start_b)
    return max(int(overlap.total_seconds() // 60), 0)


def _load_afternoon(
    session: Session, now: datetime, thresholds: RuleThresholds
) -> AfternoonLoad | None:
    """Booked load for the remaining afternoon (local wall-clock window)."""
    day = now.date()
    afternoon_start = datetime.combine(day, time(thresholds.afternoon_start_hour), now.tzinfo)
    afternoon_end = datetime.combine(day, time(thresholds.afternoon_end_hour), now.tzinfo)
    window_start = max(now, afternoon_start)
    if window_start >= afternoon_end:
        return AfternoonLoad(day=day, busy_minutes=0, event_count=0)

    window_start_utc = _ensure_utc(window_start)
    window_end_utc = _ensure_utc(afternoon_end)
    events = session.scalars(
        select(CalendarEventMirror)
        .where(
            CalendarEventMirror.start_at < window_end_utc,
            CalendarEventMirror.end_at > window_start_utc,
        )
        .order_by(CalendarEventMirror.start_at)
    ).all()

    busy = 0
    summaries: list[str] = []
    for event in events:
        busy += _overlap_minutes(
            _ensure_utc(event.start_at), _ensure_utc(event.end_at), window_start_utc, window_end_utc
        )
        if event.summary and len(summaries) < 5:
            summaries.append(event.summary)
    return AfternoonLoad(
        day=day, busy_minutes=busy, event_count=len(events), summaries=tuple(summaries)
    )


def _conflict_labels(
    session: Session, changed: CalendarEventMirror, start_utc: datetime, end_utc: datetime
) -> tuple[str, ...]:
    """Labels of agent blocks / accepted proposals the changed event overlaps."""
    labels: list[str] = []
    agent_blocks = session.scalars(
        select(CalendarEventMirror).where(
            CalendarEventMirror.is_agent_created.is_(True),
            CalendarEventMirror.id != changed.id,
            CalendarEventMirror.start_at < end_utc,
            CalendarEventMirror.end_at > start_utc,
        )
    ).all()
    labels.extend(f"agent block: {block.summary or block.external_id}" for block in agent_blocks)

    proposals = session.execute(
        select(ScheduleProposal, Task)
        .join(Task, ScheduleProposal.task_id == Task.id)
        .where(
            ScheduleProposal.status.in_([ProposalStatus.ACCEPTED, ProposalStatus.PUSHED]),
            ScheduleProposal.proposed_start < end_utc,
            ScheduleProposal.proposed_end > start_utc,
        )
    ).all()
    labels.extend(f"proposal: {task.title}" for _proposal, task in proposals)
    return tuple(labels)


def _load_schedule_changes(
    session: Session, now: datetime, lookback: timedelta
) -> tuple[ScheduleChange, ...]:
    """Diff of the calendar mirror since the last sweeps (docs/PLAN.md §6).

    The calendar sync scope owns the mirror writes; its contract is that a
    row's ``updated_at`` moves only when the external event actually changed
    (etag/sync-token gated). Deletions are invisible without tombstones and
    are left to the sync layer to re-surface (e.g. by re-creating the row as
    cancelled) — tracked as a known Phase-1 gap.
    """
    cutoff = _ensure_utc(now - lookback)
    rows = session.scalars(
        select(CalendarEventMirror).where(CalendarEventMirror.updated_at >= cutoff)
    ).all()

    changes: list[ScheduleChange] = []
    for row in rows:
        created_at = _ensure_utc(row.created_at)
        updated_at = _ensure_utc(row.updated_at)
        is_new = created_at >= cutoff and updated_at == created_at
        start_utc = _ensure_utc(row.start_at)
        end_utc = _ensure_utc(row.end_at)
        if row.is_agent_created:
            # The user touched an agent block in the external calendar
            # (external wins; only real external edits move updated_at past
            # created_at). Freshly-created agent blocks are the agent's own
            # writes, not a diff.
            if is_new:
                continue
            conflicts: tuple[str, ...] = ()
        else:
            conflicts = _conflict_labels(session, row, start_utc, end_utc)
        changes.append(
            ScheduleChange(
                external_id=row.external_id,
                calendar_source=row.calendar_source.value,
                summary=row.summary,
                kind="created" if is_new else "moved",
                starts_at=start_utc,
                ends_at=end_utc,
                is_agent_created=row.is_agent_created,
                conflicts=conflicts,
                fingerprint=(
                    f"{row.calendar_source.value}:{row.external_id}:"
                    f"{row.etag or updated_at.isoformat()}"
                ),
            )
        )
    return tuple(changes)


def _load_deadline_tasks(
    session: Session, now: datetime, thresholds: RuleThresholds
) -> tuple[DeadlineTask, ...]:
    """Open tasks with a deadline inside the risk horizon + their coverage."""
    now_utc = _ensure_utc(now)
    horizon_utc = _ensure_utc(now + timedelta(hours=thresholds.deadline_risk_horizon_hours))
    tasks = session.scalars(
        select(Task).where(
            Task.deadline.is_not(None),
            Task.deadline <= horizon_utc,
            Task.status.not_in(_TERMINAL_TASK_STATUSES),
        )
    ).all()
    if not tasks:
        return ()

    result: list[DeadlineTask] = []
    for task in tasks:
        assert task.deadline is not None  # filtered above
        deadline_utc = _ensure_utc(task.deadline)
        proposals = session.scalars(
            select(ScheduleProposal).where(
                ScheduleProposal.task_id == task.id,
                ScheduleProposal.status.in_([ProposalStatus.ACCEPTED, ProposalStatus.PUSHED]),
                ScheduleProposal.proposed_end > now_utc,
            )
        ).all()
        # Coverage = future accepted/pushed minutes clipped to [now, deadline];
        # a block scheduled after the deadline does not help meet it.
        scheduled = 0
        for proposal in proposals:
            block_start = max(_ensure_utc(proposal.proposed_start), now_utc)
            block_end = min(_ensure_utc(proposal.proposed_end), deadline_utc)
            if block_end > block_start:
                scheduled += int((block_end - block_start).total_seconds() // 60)
        result.append(
            DeadlineTask(
                task_id=str(task.id),
                title=task.title,
                deadline=deadline_utc,
                est_minutes=task.est_minutes,
                scheduled_minutes=scheduled,
                status=task.status,
            )
        )
    return tuple(result)


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FireOutcome:
    """What happened to one fire in one sweep."""

    fire: TriggerFire
    status: str  # "pushed" | "deduplicated" | "suppressed" | "push_failed"
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    evaluated_at: datetime
    rules_evaluated: int
    outcomes: tuple[FireOutcome, ...]

    def count(self, status: str) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status == status)


def default_now_provider(settings: Settings) -> Callable[[], datetime]:
    """Wall-clock provider in the *user's* timezone (Settings.timezone).

    Alert hygiene is local-time semantics: quiet hours, the daily-budget day
    boundary, the afternoon-load window and per-day dedup keys all derive
    from ``now``. Docker containers run UTC clocks while compose forwards
    ``HEALTHMES_TIMEZONE`` — evaluating on the server clock would invert the
    quiet-hours window for the user (docs/PLAN.md §11's top risk), so the
    timezone must come from settings; unset falls back to the machine's local
    timezone (right on mac-native). An invalid configured name raises loudly
    (``ZoneInfoNotFoundError``) instead of silently guessing.
    """
    tz = resolve_timezone(settings)
    return lambda: datetime.now(tz)


class TriggerEvaluator:
    """Evaluates trigger rules and pushes fresh fires as proactive alerts.

    All collaborators are injectable for tests; defaults wire the real store
    session factory, the ow_client-backed health reader and the Hermes
    webhook sender.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: sessionmaker[Session] | None = None,
        health_reader: HealthReader | None = None,
        alert_sender: AlertSender | None = None,
        rules: Sequence[TriggerRule] | None = None,
        thresholds: RuleThresholds | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._health_reader = (
            health_reader if health_reader is not None else OwHealthReader(settings)
        )
        self._alert_sender = (
            alert_sender if alert_sender is not None else HermesWebhookSender(settings)
        )
        self._rules: tuple[TriggerRule, ...] = tuple(rules) if rules is not None else ALL_RULES
        self._thresholds = thresholds if thresholds is not None else RuleThresholds()
        self._now = now_provider if now_provider is not None else default_now_provider(settings)

    def evaluate_once(self) -> EvaluationReport:
        """Run one sweep: build context, evaluate rules, persist + push fires.

        Per-rule exceptions are isolated (logged, sweep continues): one broken
        rule must never roll back the ``trigger_event`` row of a fire that was
        already pushed — the rollback would un-burn its dedup key and the next
        sweep would deliver the identical alert twice
        (tests/hardening/test_trigger_flood.py).
        """
        now = self._now()
        signals = self._health_reader.read(now)

        outcomes: list[FireOutcome] = []
        with session_scope(self._session_factory) as session:
            context = self._build_context(session, now, signals)
            for rule in self._rules:
                try:
                    fire = rule(context)
                    if fire is None:
                        continue
                    outcomes.append(self._process_fire(session, now, fire))
                except Exception:
                    logger.exception(
                        "Trigger rule %s failed; remaining rules continue.",
                        getattr(rule, "__name__", repr(rule)),
                    )

        report = EvaluationReport(
            evaluated_at=now, rules_evaluated=len(self._rules), outcomes=tuple(outcomes)
        )
        if outcomes:
            logger.info(
                "Trigger sweep: %d fire(s) — pushed=%d deduplicated=%d "
                "suppressed=%d push_failed=%d",
                len(outcomes),
                report.count("pushed"),
                report.count("deduplicated"),
                report.count("suppressed"),
                report.count("push_failed"),
            )
        return report

    def _build_context(
        self, session: Session, now: datetime, signals: HealthSignals
    ) -> TriggerContext:
        return TriggerContext(
            now=now,
            stress=signals.stress,
            recovery=signals.recovery,
            afternoon=_load_afternoon(session, now, self._thresholds),
            schedule_changes=_load_schedule_changes(
                session, now, timedelta(minutes=SCHEDULE_DIFF_LOOKBACK_MINUTES)
            ),
            deadline_tasks=_load_deadline_tasks(session, now, self._thresholds),
            thresholds=self._thresholds,
        )

    def _process_fire(self, session: Session, now: datetime, fire: TriggerFire) -> FireOutcome:
        # Dedup-key uniqueness, application-level: a key that was ever
        # recorded is never persisted or pushed again. Keys embed their
        # temporal scope (date / diff fingerprint), so "again later" is a new
        # key by construction.
        already = session.scalar(
            select(TriggerEvent.id).where(TriggerEvent.dedup_key == fire.dedup_key).limit(1)
        )
        if already is not None:
            return FireOutcome(fire=fire, status="deduplicated")

        payload: dict[str, Any] = {
            "summary": fire.summary,
            "proposal": fire.proposal,
            "evidence": fire.evidence,
        }
        event = TriggerEvent(
            fired_at=_ensure_utc(now),
            rule_id=fire.rule_id,
            dedup_key=fire.dedup_key,
            alert_sent=False,
            payload=payload,
        )
        session.add(event)
        # Flush immediately: the production factory disables autoflush, so
        # without this the hygiene queries below (cooldown, daily budget) and
        # the dedup check of LATER fires in this same sweep would not see this
        # row or the pending ``alert_sent`` updates of earlier fires — N rules
        # firing in one sweep would then blow through the daily budget
        # (pinned by tests/hardening/test_trigger_flood.py).
        session.flush()

        reason = self._push_suppression_reason(session, now, fire)
        if reason is not None:
            event.payload = {**payload, "push": {"suppressed_reason": reason}}
            return FireOutcome(fire=fire, status="suppressed", reason=reason)

        result = self._alert_sender.send(fire, fired_at=now)
        if result.ok:
            event.alert_sent = True
            event.payload = {
                **payload,
                "push": {"sent": True, "status_code": result.status_code, "channel": "webhook"},
            }
            return FireOutcome(fire=fire, status="pushed")

        # Webhook not delivered (unconfigured or failed). With native delivery
        # on, the alert is still surfaced to the companion apps via /v1/alerts +
        # glance polling — the phone/watch get it without Telegram.
        if self._settings.native_alert_delivery:
            event.alert_sent = True
            event.payload = {
                **payload,
                "push": {
                    "sent": True,
                    "channel": "native",
                    "webhook_ok": False,
                    "status_code": result.status_code,
                    "detail": result.detail,
                },
            }
            return FireOutcome(fire=fire, status="pushed")

        event.payload = {
            **payload,
            "push": {
                "suppressed_reason": _SUPPRESS_PUSH_FAILED,
                "status_code": result.status_code,
                "detail": result.detail,
            },
        }
        return FireOutcome(fire=fire, status="push_failed", reason=result.detail)

    def _push_suppression_reason(
        self, session: Session, now: datetime, fire: TriggerFire
    ) -> str | None:
        """Alert hygiene gates, in order: quiet hours, cooldown, daily budget."""
        settings = self._settings
        if is_in_quiet_hours(now.time(), settings.quiet_hours_start, settings.quiet_hours_end):
            return _SUPPRESS_QUIET_HOURS

        if settings.alert_cooldown_minutes > 0:
            cooldown_cutoff = _ensure_utc(
                now - timedelta(minutes=settings.alert_cooldown_minutes)
            )
            recent_push = session.scalar(
                select(TriggerEvent.id)
                .where(
                    TriggerEvent.rule_id == fire.rule_id,
                    TriggerEvent.alert_sent.is_(True),
                    TriggerEvent.fired_at > cooldown_cutoff,
                )
                .limit(1)
            )
            if recent_push is not None:
                return _SUPPRESS_COOLDOWN

        day_start_utc = _ensure_utc(
            datetime.combine(now.date(), time(0, 0), tzinfo=now.tzinfo)
        )
        pushed_today = session.scalar(
            select(func.count())
            .select_from(TriggerEvent)
            .where(TriggerEvent.alert_sent.is_(True), TriggerEvent.fired_at >= day_start_utc)
        )
        if (pushed_today or 0) >= self._settings.alert_daily_budget:
            return _SUPPRESS_DAILY_BUDGET
        return None


def build_trigger_job(settings: Settings) -> Callable[[], None]:
    """Zero-arg job callable for the scheduler (docs/PLAN.md section 4).

    The evaluator (and its default collaborators) is constructed lazily on
    the first run so importing the scheduler module never touches the store
    or the network; exceptions are contained per sweep.
    """
    evaluator: TriggerEvaluator | None = None

    def run_trigger_sweep() -> None:
        nonlocal evaluator
        try:
            if evaluator is None:
                evaluator = TriggerEvaluator(settings)
            evaluator.evaluate_once()
        except Exception:
            logger.exception("Trigger sweep failed; next interval will retry.")

    return run_trigger_sweep
