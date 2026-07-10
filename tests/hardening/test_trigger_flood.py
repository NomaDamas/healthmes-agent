"""Trigger-flood hardening (docs/PLAN.md §11: a noisy assistant gets muted).

Floods the ``TriggerEvaluator`` with many firings and asserts the alert-noise
mitigations hold at scale: the daily budget caps deliveries (across sweeps
AND within one multi-rule sweep), dedup storms deliver once, suppressed fires
are never redelivered, a crashing rule neither kills the sweep nor duplicates
already-pushed alerts, and the audit trail stays complete. Sessions use
``autoflush=False`` exactly like the production factory
(healthmes/store/session.py::init_engine) — the intra-sweep budget and
crash-isolation guarantees only hold under that setting because the engine
flushes per fire and isolates per-rule exceptions
(healthmes/engine/triggers.py).
"""

from datetime import datetime, time, timedelta

from freezegun import freeze_time
from sqlalchemy import select

from healthmes.config import Settings
from healthmes.engine.rules import TriggerContext, TriggerFire
from healthmes.engine.triggers import HealthSignals, TriggerEvaluator
from healthmes.engine.webhook import WebhookResult
from healthmes.store import TriggerEvent


class RecordingSender:
    """AlertSender double recording every delivery."""

    def __init__(self) -> None:
        self.sent: list[TriggerFire] = []

    def send(self, fire: TriggerFire, *, fired_at: datetime) -> WebhookResult:
        self.sent.append(fire)
        return WebhookResult(ok=True, status_code=202)


class EmptyReader:
    """HealthReader double: flood rules below are store/context independent."""

    def read(self, now: datetime) -> HealthSignals:
        return HealthSignals()


class AlwaysFiringRule:
    """Fires every sweep; the dedup key is produced by ``key_fn``."""

    def __init__(self, rule_id: str, key_fn=None) -> None:
        self.rule_id = rule_id
        self.__name__ = rule_id
        self.calls = 0
        self._key_fn = key_fn

    def __call__(self, ctx: TriggerContext) -> TriggerFire:
        self.calls += 1
        if self._key_fn is not None:
            dedup_key = self._key_fn(ctx, self.calls)
        else:  # fresh key per sweep: dedup never blocks, hygiene gates decide
            dedup_key = f"{self.rule_id}:sweep-{self.calls}"
        return TriggerFire(
            rule_id=self.rule_id,
            dedup_key=dedup_key,
            summary=f"flood observation from {self.rule_id}",
            proposal="flood proposal",
            evidence={"call": self.calls},
        )


def flood_settings(settings: Settings, **overrides) -> Settings:
    """Test settings with quiet hours disabled unless a test re-enables them."""
    updates = {
        "quiet_hours_start": time(3, 0),
        "quiet_hours_end": time(3, 0),  # start == end -> disabled
        "alert_cooldown_minutes": 0,
        "alert_daily_budget": 8,
    }
    updates.update(overrides)
    return settings.model_copy(update=updates)


def make_evaluator(settings: Settings, session_factory, sender, rules) -> TriggerEvaluator:
    return TriggerEvaluator(
        settings,
        session_factory=session_factory,
        health_reader=EmptyReader(),
        alert_sender=sender,
        rules=rules,
    )


def all_events(session_factory) -> list[TriggerEvent]:
    # fired_at is set by the evaluator from the frozen clock (deterministic);
    # created_at is a DB-side default and does not respect freezegun.
    with session_factory() as session:
        return list(session.scalars(select(TriggerEvent).order_by(TriggerEvent.fired_at)))


# ---------------------------------------------------------------------------
# Budget flood: deliveries are capped, records are not lost
# ---------------------------------------------------------------------------


def test_flood_across_sweeps_daily_budget_caps_deliveries(settings, session_factory) -> None:
    """12 fresh fires over 12 sweeps in one day -> exactly `budget` deliveries."""
    tight = flood_settings(settings, alert_daily_budget=3)
    sender = RecordingSender()
    rule = AlwaysFiringRule("flood_rule")
    evaluator = make_evaluator(tight, session_factory, sender, rules=(rule,))

    statuses: list[str] = []
    with freeze_time("2026-07-09 09:00:00") as frozen:
        for _ in range(12):
            report = evaluator.evaluate_once()
            statuses.extend(outcome.status for outcome in report.outcomes)
            frozen.tick(timedelta(minutes=10))

    assert len(sender.sent) == 3  # hard cap on deliveries
    assert statuses == ["pushed"] * 3 + ["suppressed"] * 9

    # No fire is lost: every firing has an audit row, capped pushes are
    # recorded with the budget as the suppression reason.
    events = all_events(session_factory)
    assert len(events) == 12
    assert [event.alert_sent for event in events] == [True] * 3 + [False] * 9
    for suppressed in events[3:]:
        assert suppressed.payload["push"]["suppressed_reason"] == "daily_budget"
    # Audit rows keep the full notification grammar inputs.
    for event in events:
        assert event.payload["summary"]
        assert event.payload["proposal"]
        assert event.dedup_key is not None
    assert len({event.dedup_key for event in events}) == 12  # all distinct


def test_dedup_storm_same_key_delivers_once(settings, session_factory) -> None:
    """The same logical occurrence re-firing for hours -> one row, one push."""
    calm = flood_settings(settings, alert_daily_budget=100)
    sender = RecordingSender()
    rule = AlwaysFiringRule("storm_rule", key_fn=lambda ctx, call: "storm_rule:occurrence-1")
    evaluator = make_evaluator(calm, session_factory, sender, rules=(rule,))

    statuses: list[str] = []
    with freeze_time("2026-07-09 09:00:00") as frozen:
        for _ in range(30):
            report = evaluator.evaluate_once()
            statuses.extend(outcome.status for outcome in report.outcomes)
            frozen.tick(timedelta(minutes=10))

    assert len(sender.sent) == 1
    assert statuses == ["pushed"] + ["deduplicated"] * 29
    assert len(all_events(session_factory)) == 1  # dedup keeps the trail single


def test_suppressed_fires_are_not_redelivered_and_do_not_consume_budget(
    settings, session_factory
) -> None:
    """Noise control beats redelivery (triggers.py contract).

    A fire suppressed by quiet hours is recorded and its dedup key is burned:
    after the window it must NOT be delivered late. And because the budget
    counts only ``alert_sent`` rows, the suppressed fire must not consume it.
    """
    night = flood_settings(
        settings,
        alert_daily_budget=1,
        quiet_hours_start=time(22, 30),
        quiet_hours_end=time(7, 0),
    )
    sender = RecordingSender()
    stable = AlwaysFiringRule("night_rule", key_fn=lambda ctx, call: "night_rule:occurrence-1")
    evaluator = make_evaluator(night, session_factory, sender, rules=(stable,))

    with freeze_time("2026-07-09 23:00:00") as frozen:  # inside quiet hours
        report = evaluator.evaluate_once()
        assert [outcome.status for outcome in report.outcomes] == ["suppressed"]
        assert report.outcomes[0].reason == "quiet_hours"

        frozen.move_to("2026-07-10 08:00:00")  # outside the window
        report = evaluator.evaluate_once()
        assert [outcome.status for outcome in report.outcomes] == ["deduplicated"]
        assert sender.sent == []  # never delivered late

        # The suppressed fire did not consume the (budget=1) daily budget:
        # a genuinely new occurrence still gets through.
        fresh = AlwaysFiringRule("morning_rule")
        morning = make_evaluator(night, session_factory, sender, rules=(fresh,))
        report = morning.evaluate_once()
        assert [outcome.status for outcome in report.outcomes] == ["pushed"]

    assert len(sender.sent) == 1


def test_flood_budget_resets_at_local_midnight(settings, session_factory) -> None:
    """A capped day must not mute the next day (budget is per calendar day)."""
    tight = flood_settings(settings, alert_daily_budget=2)
    sender = RecordingSender()
    rule = AlwaysFiringRule("two_day_rule")
    evaluator = make_evaluator(tight, session_factory, sender, rules=(rule,))

    with freeze_time("2026-07-09 20:00:00") as frozen:
        for _ in range(5):  # day 1 flood: 2 pushed, 3 suppressed
            evaluator.evaluate_once()
            frozen.tick(timedelta(minutes=10))
        assert len(sender.sent) == 2

        frozen.move_to("2026-07-10 09:00:00")  # next day, fresh budget
        report = evaluator.evaluate_once()
        assert [outcome.status for outcome in report.outcomes] == ["pushed"]
    assert len(sender.sent) == 3


# ---------------------------------------------------------------------------
# Intra-sweep budget + crash isolation (formerly pinned xfail gaps; the
# engine fixes — per-fire flush and per-rule exception isolation — landed in
# healthmes/engine/triggers.py, so these now assert the fixed behavior).
# ---------------------------------------------------------------------------


def test_flood_within_one_sweep_daily_budget_caps_deliveries(settings, session_factory) -> None:
    """6 rules firing in ONE sweep with budget=2 -> only 2 deliveries."""
    tight = flood_settings(settings, alert_daily_budget=2)
    sender = RecordingSender()
    rules = tuple(AlwaysFiringRule(f"simultaneous_{index}") for index in range(6))
    evaluator = make_evaluator(tight, session_factory, sender, rules=rules)

    with freeze_time("2026-07-09 09:00:00"):
        report = evaluator.evaluate_once()

    assert report.count("pushed") == 2
    assert report.count("suppressed") == 4
    assert len(sender.sent) == 2


def test_rule_crash_does_not_duplicate_already_pushed_alerts(settings, session_factory) -> None:
    """A raising rule is isolated: the sweep survives and dedup keys stay burned."""
    stable = flood_settings(settings)
    sender = RecordingSender()
    good = AlwaysFiringRule("good_rule", key_fn=lambda ctx, call: "good_rule:occurrence-1")

    class BrokenRule:
        __name__ = "broken_rule"

        def __init__(self) -> None:
            self.raises = True

        def __call__(self, ctx: TriggerContext) -> None:
            if self.raises:
                raise RuntimeError("rule blew up")
            return None

    broken = BrokenRule()
    evaluator = make_evaluator(stable, session_factory, sender, rules=(good, broken))

    with freeze_time("2026-07-09 09:00:00") as frozen:
        # Sweep 1: good_rule pushes, then broken_rule raises — the exception
        # is isolated per rule, so evaluate_once must NOT raise and the pushed
        # fire's TriggerEvent row must survive (no rollback un-burning its
        # dedup key).
        report = evaluator.evaluate_once()
        assert [outcome.status for outcome in report.outcomes] == ["pushed"]
        assert report.rules_evaluated == 2

        frozen.tick(timedelta(minutes=10))
        broken.raises = False
        report = evaluator.evaluate_once()  # sweep 2: same occurrence deduplicates
        assert [outcome.status for outcome in report.outcomes] == ["deduplicated"]

    same_key_sends = [fire for fire in sender.sent if fire.dedup_key == "good_rule:occurrence-1"]
    assert len(same_key_sends) == 1, "identical alert delivered more than once"
    events = all_events(session_factory)
    assert [event.dedup_key for event in events] == ["good_rule:occurrence-1"]
    assert events[0].alert_sent is True


def test_scheduler_wrapper_survives_a_crashing_sweep(settings) -> None:
    """The 10-minute loop itself must never die from one bad sweep.

    ``build_trigger_job`` wraps evaluator construction and each sweep in a
    try/except; this pins that a crashing sweep cannot take the APScheduler
    thread down (the crash-duplication side effect is pinned above). The
    default-collaborator path uses the process-wide store engine, so it is
    initialised from the hermetic test settings (in-memory sqlite with NO
    schema -> the sweep's store queries raise) and disposed afterwards.
    """
    from healthmes.engine.triggers import build_trigger_job
    from healthmes.store import dispose_engine, init_engine

    init_engine(settings)  # global engine -> :memory: sqlite without tables
    try:
        job = build_trigger_job(flood_settings(settings))
        # OwHealthReader degrades to empty signals without network; the store
        # sweep then raises on the missing schema — the wrapper must swallow it.
        job()  # must not raise
    finally:
        dispose_engine()
