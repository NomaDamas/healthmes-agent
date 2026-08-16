"""UI-neutral scheduled wellness briefing jobs for the canonical runtime."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, tzinfo

from sqlalchemy.orm import Session, sessionmaker

from healthmes.config import Settings, resolve_timezone
from healthmes.engine.rules import TriggerFire
from healthmes.engine.triggers import AlertSender, TriggerEvaluator

__all__ = [
    "MORNING_BRIEFING_JOB_ID",
    "EVENING_BRIEFING_JOB_ID",
    "WEEKLY_BRIEFING_JOB_ID",
    "SCHEDULED_BRIEFING_SPECS",
    "ScheduledBriefingSpec",
    "build_scheduled_briefing_fire",
    "build_scheduled_briefing_job",
]

logger = logging.getLogger(__name__)

MORNING_BRIEFING_JOB_ID = "healthmes-wellness-briefing-morning"
EVENING_BRIEFING_JOB_ID = "healthmes-wellness-briefing-evening"
WEEKLY_BRIEFING_JOB_ID = "healthmes-wellness-briefing-weekly"


@dataclass(frozen=True, slots=True)
class ScheduledBriefingSpec:
    name: str
    job_id: str
    hour: int
    minute: int
    day_of_week: str | None = None


SCHEDULED_BRIEFING_SPECS = (
    ScheduledBriefingSpec(
        name="morning",
        job_id=MORNING_BRIEFING_JOB_ID,
        hour=7,
        minute=0,
    ),
    ScheduledBriefingSpec(
        name="evening",
        job_id=EVENING_BRIEFING_JOB_ID,
        hour=21,
        minute=30,
    ),
    ScheduledBriefingSpec(
        name="weekly",
        job_id=WEEKLY_BRIEFING_JOB_ID,
        day_of_week="sun",
        hour=18,
        minute=0,
    ),
)


def build_scheduled_briefing_fire(
    spec: ScheduledBriefingSpec,
    *,
    fired_at: datetime,
    timezone: tzinfo,
) -> TriggerFire:
    """Build a deduplicated signal without prescribing context domains."""

    local = fired_at.astimezone(timezone)
    if spec.day_of_week is None:
        period = local.date().isoformat()
    else:
        iso_year, iso_week, _ = local.isocalendar()
        period = f"{iso_year}-W{iso_week:02d}"
    return TriggerFire(
        rule_id=f"scheduled_briefing.{spec.name}",
        dedup_key=f"scheduled_briefing.{spec.name}:{period}",
        summary=(
            f"Prepare the user's {spec.name} wellness briefing for "
            f"{local.isoformat()}."
        ),
        proposal=(
            "Return only useful, current wellness guidance. Select the "
            "necessary HealthMes domains autonomously and state data gaps."
        ),
        evidence={
            "briefing_kind": spec.name,
            "scheduled_local_time": local.isoformat(),
            "timezone": str(timezone),
        },
    )


def build_scheduled_briefing_job(
    settings: Settings,
    *,
    spec: ScheduledBriefingSpec,
    alert_sender: AlertSender,
    now_provider: Callable[[], datetime] | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> Callable[[], None]:
    """Build one contained scheduler job using the durable trigger outbox."""

    timezone = resolve_timezone(settings)
    clock = now_provider or (
        lambda: datetime.now(resolve_timezone(settings))
    )
    evaluator = TriggerEvaluator(
        settings,
        session_factory=session_factory,
        alert_sender=alert_sender,
        rules=(),
    )

    def run_scheduled_briefing() -> None:
        now = clock()
        try:
            evaluator.dispatch_fire(
                build_scheduled_briefing_fire(
                    spec,
                    fired_at=now,
                    timezone=timezone,
                ),
                fired_at=now,
            )
        except Exception:
            logger.exception(
                "Scheduled %s wellness briefing failed; the durable outbox "
                "will retry accepted fires.",
                spec.name,
            )

    return run_scheduled_briefing
