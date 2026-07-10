"""Per-rule unit tests: fire, non-fire, and dedup-key stability.

Rules are pure functions of TriggerContext, so no store or fakes are needed.
A non-UTC fixed timezone is used to catch accidental UTC/date coupling.
"""

from datetime import UTC, datetime, timedelta, timezone

from healthmes.engine.rules import (
    ALL_RULES,
    AfternoonLoad,
    DeadlineTask,
    RecoverySnapshot,
    RuleThresholds,
    ScheduleChange,
    StressSnapshot,
    TriggerContext,
    deadline_risk,
    low_recovery_heavy_afternoon,
    schedule_changed,
    stress_spike_vs_baseline,
)

TZ = timezone(timedelta(hours=9))
NOW = datetime(2026, 7, 9, 14, 0, tzinfo=TZ)


def make_ctx(**kwargs) -> TriggerContext:
    kwargs.setdefault("now", NOW)
    return TriggerContext(**kwargs)


def make_stress(
    recent_value: float = 85.0,
    baseline_median: float | None = 55.0,
    baseline_days: int = 10,
    recent_at: datetime | None = None,
) -> StressSnapshot:
    return StressSnapshot(
        recent_value=recent_value,
        recent_at=recent_at if recent_at is not None else NOW - timedelta(minutes=30),
        baseline_median=baseline_median,
        baseline_days=baseline_days,
    )


def make_change(
    external_id: str = "evt-1",
    *,
    is_agent_created: bool = False,
    conflicts: tuple[str, ...] = (),
    fingerprint: str = "google:evt-1:etag-1",
    kind: str = "moved",
) -> ScheduleChange:
    return ScheduleChange(
        external_id=external_id,
        calendar_source="google",
        summary=f"Event {external_id}",
        kind=kind,
        starts_at=NOW + timedelta(hours=1),
        ends_at=NOW + timedelta(hours=2),
        is_agent_created=is_agent_created,
        conflicts=conflicts,
        fingerprint=fingerprint,
    )


def make_task(
    task_id: str = "t-1",
    *,
    deadline: datetime | None = None,
    est_minutes: int | None = 120,
    scheduled_minutes: int = 0,
    status: str = "todo",
) -> DeadlineTask:
    return DeadlineTask(
        task_id=task_id,
        title=f"Task {task_id}",
        deadline=deadline if deadline is not None else NOW + timedelta(hours=24),
        est_minutes=est_minutes,
        scheduled_minutes=scheduled_minutes,
        status=status,
    )


# ---------------------------------------------------------------------------
# stress_spike_vs_baseline
# ---------------------------------------------------------------------------


class TestStressSpikeVsBaseline:
    def test_fires_on_spike_above_floor_and_baseline(self) -> None:
        fire = stress_spike_vs_baseline(make_ctx(stress=make_stress()))
        assert fire is not None
        assert fire.rule_id == "stress_spike_vs_baseline"
        assert fire.dedup_key == "stress_spike_vs_baseline:2026-07-09"
        assert fire.evidence["recent_value"] == 85.0
        assert fire.evidence["baseline_median"] == 55.0
        assert fire.evidence["ratio"] == 1.55
        assert fire.evidence["baseline_days"] == 10
        assert "85" in fire.summary

    def test_no_fire_without_signal(self) -> None:
        assert stress_spike_vs_baseline(make_ctx()) is None
        assert stress_spike_vs_baseline(make_ctx(stress=make_stress(baseline_median=None))) is None

    def test_no_fire_with_thin_baseline(self) -> None:
        ctx = make_ctx(stress=make_stress(baseline_days=2))
        assert stress_spike_vs_baseline(ctx) is None

    def test_no_fire_below_absolute_floor(self) -> None:
        # Huge relative jump but absolute value is unremarkable.
        ctx = make_ctx(stress=make_stress(recent_value=65.0, baseline_median=30.0))
        assert stress_spike_vs_baseline(ctx) is None

    def test_no_fire_below_baseline_ratio(self) -> None:
        # High absolute value but normal for this person.
        ctx = make_ctx(stress=make_stress(recent_value=75.0, baseline_median=70.0))
        assert stress_spike_vs_baseline(ctx) is None

    def test_dedup_key_is_stable_within_day_and_changes_across_days(self) -> None:
        first = stress_spike_vs_baseline(make_ctx(stress=make_stress()))
        second = stress_spike_vs_baseline(
            make_ctx(now=NOW + timedelta(hours=3), stress=make_stress(recent_value=90.0))
        )
        next_day = stress_spike_vs_baseline(
            make_ctx(now=NOW + timedelta(days=1), stress=make_stress())
        )
        assert first is not None and second is not None and next_day is not None
        assert first.dedup_key == second.dedup_key
        assert next_day.dedup_key != first.dedup_key


# ---------------------------------------------------------------------------
# low_recovery_heavy_afternoon
# ---------------------------------------------------------------------------


def make_recovery(value: float = 30.0) -> RecoverySnapshot:
    return RecoverySnapshot(
        value=value, category="body_battery", provider="garmin", recorded_at=NOW
    )


def make_afternoon(busy_minutes: int = 240, event_count: int = 4) -> AfternoonLoad:
    return AfternoonLoad(
        day=NOW.date(),
        busy_minutes=busy_minutes,
        event_count=event_count,
        summaries=("Design review", "1:1"),
    )


class TestLowRecoveryHeavyAfternoon:
    def test_fires_on_low_recovery_and_heavy_afternoon(self) -> None:
        ctx = make_ctx(recovery=make_recovery(), afternoon=make_afternoon())
        fire = low_recovery_heavy_afternoon(ctx)
        assert fire is not None
        assert fire.rule_id == "low_recovery_heavy_afternoon"
        assert fire.dedup_key == "low_recovery_heavy_afternoon:2026-07-09"
        assert fire.evidence["recovery_value"] == 30.0
        assert fire.evidence["afternoon_busy_minutes"] == 240
        assert fire.evidence["afternoon_events"] == ["Design review", "1:1"]

    def test_no_fire_when_either_signal_missing(self) -> None:
        assert low_recovery_heavy_afternoon(make_ctx(recovery=make_recovery())) is None
        assert low_recovery_heavy_afternoon(make_ctx(afternoon=make_afternoon())) is None

    def test_no_fire_when_recovery_is_fine(self) -> None:
        ctx = make_ctx(recovery=make_recovery(value=55.0), afternoon=make_afternoon())
        assert low_recovery_heavy_afternoon(ctx) is None

    def test_no_fire_when_afternoon_is_light(self) -> None:
        ctx = make_ctx(recovery=make_recovery(), afternoon=make_afternoon(busy_minutes=90))
        assert low_recovery_heavy_afternoon(ctx) is None

    def test_boundary_values_fire(self) -> None:
        thresholds = RuleThresholds()
        ctx = make_ctx(
            recovery=make_recovery(value=thresholds.low_recovery_max_value),
            afternoon=make_afternoon(busy_minutes=thresholds.heavy_afternoon_min_busy_minutes),
        )
        assert low_recovery_heavy_afternoon(ctx) is not None


# ---------------------------------------------------------------------------
# schedule_changed
# ---------------------------------------------------------------------------


class TestScheduleChanged:
    def test_fires_on_externally_touched_agent_block(self) -> None:
        change = make_change(is_agent_created=True)
        fire = schedule_changed(make_ctx(schedule_changes=(change,)))
        assert fire is not None
        assert fire.rule_id == "schedule_changed"
        assert fire.evidence["changes"][0]["external_id"] == "evt-1"
        assert fire.evidence["changes"][0]["is_agent_created"] is True

    def test_fires_on_external_event_conflicting_with_plan(self) -> None:
        change = make_change(conflicts=("proposal: Write report",))
        fire = schedule_changed(make_ctx(schedule_changes=(change,)))
        assert fire is not None
        assert fire.evidence["changes"][0]["conflicts"] == ["proposal: Write report"]

    def test_no_fire_for_irrelevant_external_changes(self) -> None:
        change = make_change()  # external, no conflicts
        assert schedule_changed(make_ctx(schedule_changes=(change,))) is None
        assert schedule_changed(make_ctx()) is None

    def test_dedup_key_is_order_insensitive_and_revision_sensitive(self) -> None:
        one = make_change("evt-1", is_agent_created=True, fingerprint="google:evt-1:etag-1")
        two = make_change("evt-2", is_agent_created=True, fingerprint="google:evt-2:etag-9")
        fire_ab = schedule_changed(make_ctx(schedule_changes=(one, two)))
        fire_ba = schedule_changed(make_ctx(schedule_changes=(two, one)))
        assert fire_ab is not None and fire_ba is not None
        assert fire_ab.dedup_key == fire_ba.dedup_key

        # A new revision of the same event is a new logical occurrence.
        one_rev2 = make_change("evt-1", is_agent_created=True, fingerprint="google:evt-1:etag-2")
        fire_rev2 = schedule_changed(make_ctx(schedule_changes=(one_rev2, two)))
        assert fire_rev2 is not None
        assert fire_rev2.dedup_key != fire_ab.dedup_key


# ---------------------------------------------------------------------------
# deadline_risk
# ---------------------------------------------------------------------------


class TestDeadlineRisk:
    def test_fires_on_under_scheduled_task_in_horizon(self) -> None:
        task = make_task(est_minutes=120, scheduled_minutes=60)
        fire = deadline_risk(make_ctx(deadline_tasks=(task,)))
        assert fire is not None
        assert fire.rule_id == "deadline_risk"
        assert fire.evidence["tasks"][0]["shortfall_minutes"] == 60

    def test_fires_on_unestimated_task_with_nothing_scheduled(self) -> None:
        task = make_task(est_minutes=None, scheduled_minutes=0)
        assert deadline_risk(make_ctx(deadline_tasks=(task,))) is not None

    def test_no_fire_for_covered_or_far_or_done_tasks(self) -> None:
        covered = make_task(est_minutes=120, scheduled_minutes=120)
        far = make_task(deadline=NOW + timedelta(hours=72))
        done = make_task(status="done")
        unestimated_but_scheduled = make_task(est_minutes=None, scheduled_minutes=30)
        for task in (covered, far, done, unestimated_but_scheduled):
            assert deadline_risk(make_ctx(deadline_tasks=(task,))) is None
        assert deadline_risk(make_ctx()) is None

    def test_overdue_task_still_fires(self) -> None:
        overdue = make_task(deadline=NOW - timedelta(hours=2))
        assert deadline_risk(make_ctx(deadline_tasks=(overdue,))) is not None

    def test_dedup_key_stable_per_task_set_and_deadline_date(self) -> None:
        task_a = make_task("t-a")
        task_b = make_task("t-b")
        fire_ab = deadline_risk(make_ctx(deadline_tasks=(task_a, task_b)))
        fire_ba = deadline_risk(make_ctx(deadline_tasks=(task_b, task_a)))
        assert fire_ab is not None and fire_ba is not None
        assert fire_ab.dedup_key == fire_ba.dedup_key

        moved = make_task("t-a", deadline=NOW + timedelta(hours=40))
        fire_moved = deadline_risk(make_ctx(deadline_tasks=(moved, task_b)))
        assert fire_moved is not None
        assert fire_moved.dedup_key != fire_ab.dedup_key


def test_all_rules_registry_is_complete_and_ordered() -> None:
    assert ALL_RULES == (
        stress_spike_vs_baseline,
        low_recovery_heavy_afternoon,
        schedule_changed,
        deadline_risk,
    )


def test_evidence_is_json_serializable() -> None:
    import json

    ctx = make_ctx(
        stress=make_stress(),
        recovery=make_recovery(),
        afternoon=make_afternoon(),
        schedule_changes=(make_change(is_agent_created=True),),
        deadline_tasks=(make_task(deadline=NOW.astimezone(UTC) + timedelta(hours=4)),),
    )
    for rule in ALL_RULES:
        fire = rule(ctx)
        assert fire is not None, rule
        json.dumps(fire.evidence)  # must not raise
