import json
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from healthmes.activity import resolver as resolver_module
from healthmes.activity.contracts import (
    ActivityBatchIn,
    ActivityCapability,
    ActivityContextResolveRequest,
    ActivityPlatform,
    AppHourRecord,
    AppIntervalRecord,
)
from healthmes.activity.repository import DAY_SUMMARY_EVENT
from healthmes.activity.resolver import (
    WellnessContextRangeError,
    _context_ready_for_preset,
    calendar_context,
    nutrition_context,
    resolve_wellness_context,
)
from healthmes.activity.service import ingest_activity_batch
from healthmes.calendars.base import HealthmesEventKind
from healthmes.calendars.visibility import CalendarVisibility
from healthmes.nutrition.contracts import (
    Confidence,
    DailyIntakeConfirmation,
    Estimate,
    EstimateKind,
)
from healthmes.nutrition.intake_contracts import (
    CaptureModality,
    DecisionScope,
    EvidenceOrigin,
    IntakeDecisionRequest,
    IntakeIntent,
    IntakeInteraction,
    IntakeOutcome,
    IntakeOutcomeStatus,
    NormalizedIntakeItem,
    NutrientFact,
)
from healthmes.nutrition.intake_service import (
    create_interaction,
    persist_decision_request,
    persist_outcome,
)
from healthmes.nutrition.repository import persist_daily_confirmation
from healthmes.store import CalendarEventMirror, CalendarSource, WellnessEvent


def _seed_activity(session) -> None:
    ingest_activity_batch(
        session,
        ActivityBatchIn(
            source_provider="test-desktop",
            source_device="desktop-1",
            platform=ActivityPlatform.MACOS,
            capability=ActivityCapability.DETAILED,
            timezone="UTC",
            records=[
                AppIntervalRecord(
                    source_record_id="resolver-activity",
                    start_at=datetime(2026, 8, 1, 9, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                    state="active",
                    app_id="private.app.identity",
                    category="productivity",
                    launches=2,
                )
            ],
        ),
    )


def _seed_fixed_offset_caffeine_request(
    session,
    settings,
) -> uuid.UUID:
    timezone = "UTC+09:00"
    consumed_id = uuid.uuid4()
    consumed_at = datetime.fromisoformat("2026-08-01T09:00:00+09:00")
    caffeine_item = NormalizedIntakeItem(
        name="morning coffee",
        intake_type="beverage",
        serving=Estimate(
            kind=EstimateKind.EXACT,
            unit="cup",
            exact=1,
            estimation_basis="owner_statement",
        ),
        nutrients=(
            NutrientFact(
                nutrient="caffeine",
                amount=Estimate(
                    kind=EstimateKind.EXACT,
                    unit="mg",
                    exact=80,
                    estimation_basis="owner_statement",
                ),
                confidence=Confidence.HIGH,
                origin=EvidenceOrigin.USER,
            ),
        ),
        confidence=Confidence.HIGH,
    )
    create_interaction(
        session,
        settings,
        IntakeInteraction(
            interaction_id=consumed_id,
            operation_fingerprint="a" * 64,
            intent=IntakeIntent.LOG_CONSUMED,
            modality=CaptureModality.TEXT,
            observed_at=consumed_at,
            recorded_at=consumed_at,
            timezone=timezone,
            source="resolver-test",
            source_text="80 mg caffeine coffee",
            media_path=None,
            nutrition_observation_id=None,
            items=(caffeine_item,),
        ),
    )
    outcome_id = uuid.uuid4()
    persist_outcome(
        session,
        IntakeOutcome(
            outcome_id=outcome_id,
            operation_fingerprint="b" * 64,
            interaction_id=consumed_id,
            status=IntakeOutcomeStatus.CONSUMED,
            confirmed_at=consumed_at + timedelta(minutes=1),
            source="resolver-test",
            consumed_at=consumed_at,
        ),
    )
    persist_daily_confirmation(
        session,
        DailyIntakeConfirmation(
            confirmation_id=uuid.uuid4(),
            local_date=date(2026, 8, 1),
            timezone=timezone,
            observation_ids=(),
            outcome_ids=(outcome_id,),
            total_intake_complete=True,
            confirmed_at=consumed_at + timedelta(minutes=2),
            source="resolver-test",
        ),
    )

    candidate_id = uuid.uuid4()
    candidate_at = datetime.fromisoformat("2026-08-01T15:00:00+09:00")
    candidate_item = NormalizedIntakeItem(
        name="afternoon coffee",
        intake_type="beverage",
        serving=caffeine_item.serving,
        nutrients=(
            NutrientFact(
                nutrient="caffeine",
                amount=Estimate(
                    kind=EstimateKind.EXACT,
                    unit="mg",
                    exact=100,
                    estimation_basis="owner_statement",
                ),
                confidence=Confidence.HIGH,
                origin=EvidenceOrigin.USER,
            ),
        ),
        confidence=Confidence.HIGH,
    )
    create_interaction(
        session,
        settings,
        IntakeInteraction(
            interaction_id=candidate_id,
            operation_fingerprint="c" * 64,
            intent=IntakeIntent.ASK_BEFORE_INTAKE,
            modality=CaptureModality.TEXT,
            observed_at=candidate_at,
            recorded_at=candidate_at,
            timezone=timezone,
            source="resolver-test",
            source_text="Can I drink this coffee?",
            media_path=None,
            nutrition_observation_id=None,
            items=(candidate_item,),
        ),
    )
    request_id = uuid.uuid4()
    persist_decision_request(
        session,
        IntakeDecisionRequest(
            request_id=request_id,
            operation_fingerprint="d" * 64,
            interaction_id=candidate_id,
            scope=DecisionScope.CAFFEINE_SLEEP,
            requested_at=candidate_at,
            source="resolver-test",
            intended_consumption_at=candidate_at,
        ),
    )
    return request_id


async def test_single_domain_request_does_not_read_unselected_wearables(session) -> None:
    _seed_activity(session)

    async def must_not_run(day: date):
        raise AssertionError(f"wearable reader must not run for {day}")

    result = await resolve_wellness_context(
        session,
        ActivityContextResolveRequest(
            question_kind="activity_summary",
            date="2026-08-01",
        ),
        default_timezone="UTC",
        wearable_reader=must_not_run,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    assert result["status"] == "ok"
    assert result["selected_domains"] == ["activity"]
    assert result["not_selected_domains"] == [
        "wearable",
        "calendar",
        "nutrition",
        "time",
    ]
    assert result["contexts"]["activity"]["total_active_minutes"] == 60.0
    assert "private.app.identity" not in json.dumps(result)


async def test_context_ready_accepts_complete_activity_summary(session) -> None:
    records = [
        AppHourRecord(
            source_record_id=f"complete-hour-{hour}",
            bucket_start=datetime(2026, 8, 1, hour, tzinfo=UTC),
            app_id="browser",
            foreground_seconds=600,
            coverage_seconds=3600,
            bucket_complete=True,
        )
        for hour in range(6)
    ]
    ingest_activity_batch(
        session,
        ActivityBatchIn(
            source_provider="resolver-complete-hour",
            source_device="phone-complete",
            platform=ActivityPlatform.ANDROID,
            capability=ActivityCapability.AGGREGATE,
            timezone="UTC",
            collected_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
            records=records,
        ),
    )

    result = await resolve_wellness_context(
        session,
        ActivityContextResolveRequest(
            question_kind="activity_summary",
            date="2026-08-01",
        ),
        default_timezone="UTC",
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    assert result["context_ready"] is True
    assert result["context_readiness_blockers"] == []


async def test_context_ready_rejects_provisional_activity(session) -> None:
    ingest_activity_batch(
        session,
        ActivityBatchIn(
            source_provider="resolver-provisional-hour",
            source_device="phone-provisional",
            platform=ActivityPlatform.ANDROID,
            capability=ActivityCapability.AGGREGATE,
            timezone="UTC",
            collected_at=datetime(2026, 8, 1, 10, 30, tzinfo=UTC),
            records=[
                AppHourRecord(
                    source_record_id="provisional-hour",
                    bucket_start=datetime(2026, 8, 1, 10, tzinfo=UTC),
                    app_id="browser",
                    foreground_seconds=600,
                    coverage_seconds=1800,
                    bucket_complete=False,
                )
            ],
        ),
    )

    result = await resolve_wellness_context(
        session,
        ActivityContextResolveRequest(
            question_kind="activity_summary",
            date="2026-08-01",
        ),
        default_timezone="UTC",
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    assert result["context_ready"] is False
    assert {
        "activity.provisional_activity",
        "activity.coverage_unknown",
    } <= set(result["context_readiness_blockers"])


async def test_context_ready_rejects_missing_freshness(
    session,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        resolver_module,
        "activity_summary_context",
        lambda *args, **kwargs: {
            "status": "ok",
            "evidence_ids": ["summary-1"],
            "source_coverage": {"status": "known", "ratio": 1.0},
            "freshness": {
                "recorded_at": None,
                "status": "unavailable",
            },
            "limitations": [],
        },
    )

    result = await resolve_wellness_context(
        session,
        ActivityContextResolveRequest(
            question_kind="activity_summary",
            date="2026-08-01",
        ),
        default_timezone="UTC",
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    assert result["context_ready"] is False
    assert "activity.freshness_not_usable" in result[
        "context_readiness_blockers"
    ]


def test_context_ready_checks_every_compound_activity_child() -> None:
    ready_child = {
        "status": "ok",
        "evidence_ids": ["activity-1"],
        "coverage": {"status": "known", "ratio": 1.0},
        "freshness": {
            "recorded_at": "2026-08-01T11:00:00+00:00",
            "status": "stored_summary",
        },
        "limitations": [],
    }
    incomplete_child = {
        **ready_child,
        "coverage": {"status": "unknown", "ratio": None},
        "limitations": ["coverage_unknown"],
    }

    incomplete_ready, incomplete_blockers = _context_ready_for_preset(
        "overwork",
        ("activity",),
        {
            "activity": {
                "status": "ok",
                "focus": ready_child,
                "overwork": incomplete_child,
            }
        },
        day=date(2026, 8, 1),
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
        timezone=UTC,
    )
    complete_ready, complete_blockers = _context_ready_for_preset(
        "overwork",
        ("activity",),
        {
            "activity": {
                "status": "ok",
                "focus": ready_child,
                "overwork": ready_child,
            }
        },
        day=date(2026, 8, 1),
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
        timezone=UTC,
    )

    assert incomplete_ready is False
    assert "activity.overwork.coverage_unknown" in incomplete_blockers
    assert complete_ready is True
    assert complete_blockers == []


@pytest.mark.parametrize(
    ("freshness", "coverage", "expected_blocker"),
    [
        (
            "2026-08-01T08:00:00+00:00",
            {"status": "unknown", "ratio": None},
            "wearable.coverage_unknown",
        ),
        (
            "2020-01-01T08:00:00+00:00",
            {"status": "readiness_blocks", "ratio": 1.0},
            "wearable.freshness_outside_requested_day",
        ),
    ],
)
def test_context_ready_rejects_unknown_or_stale_wearable_context(
    freshness,
    coverage,
    expected_blocker,
) -> None:
    ready, blockers = _context_ready_for_preset(
        "recovery",
        ("wearable",),
        {
            "wearable": {
                "status": "ok",
                "source_refs": [{"record_id": "wearable-1"}],
                "freshness": {
                    "recorded_at": freshness,
                    "status": "derived_from_readiness_blocks",
                },
                "coverage": coverage,
            }
        },
        day=date(2026, 8, 1),
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
        timezone=UTC,
    )

    assert ready is False
    assert expected_blocker in blockers


async def test_resolver_uses_actual_fixed_offset_caffeine_request(
    session,
    settings,
) -> None:
    request_id = _seed_fixed_offset_caffeine_request(session, settings)

    result = await resolve_wellness_context(
        session,
        ActivityContextResolveRequest(
            question_kind="caffeine_for_focus",
            date="2026-08-01",
            start=datetime(2026, 8, 1, 1, tzinfo=UTC),
            end=datetime(2026, 8, 1, 2, tzinfo=UTC),
            timezone="UTC+09:00",
            nutrition_request_id=request_id,
        ),
        default_timezone="UTC",
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    nutrition = result["contexts"]["nutrition"]
    assert nutrition["status"] == "ok"
    assert nutrition["candidate_ledger_complete"] is True
    assert nutrition["decision_ready"] is False
    assert result["decision_ready"] is False
    assert nutrition["context"]["request"]["request_id"] == str(request_id)
    assert (
        nutrition["context"]["specialized_evidence"]["caffeine"][
            "confirmed_caffeine_mg"
        ]
        == 80
    )


async def test_one_domain_failure_does_not_erase_other_focus_context(session) -> None:
    _seed_activity(session)

    async def broken_wearable(day: date):
        raise RuntimeError(f"wearable offline for {day}")

    result = await resolve_wellness_context(
        session,
        ActivityContextResolveRequest(
            question_kind="focus",
            date="2026-08-01",
            start=datetime(2026, 8, 1, 9, tzinfo=UTC),
            end=datetime(2026, 8, 1, 10, tzinfo=UTC),
        ),
        default_timezone="UTC",
        wearable_reader=broken_wearable,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    assert result["status"] == "partial"
    assert result["selected_domains"] == ["activity", "wearable", "calendar"]
    assert result["domain_statuses"]["activity"] == "ok"
    assert result["contexts"]["wearable"]["status"] == "unavailable"
    assert result["contexts"]["wearable"]["reason"] == "RuntimeError"
    assert result["contexts"]["calendar"]["status"] == "unavailable"
    assert (
        result["contexts"]["calendar"]["reason"]
        == "calendar_visibility_not_configured"
    )
    assert result["evidence"]
    assert result["freshness"]["activity"]["status"] == "retained_raw_window"
    assert "open_wearables_context_unavailable" in result["limitations"]


async def test_caffeine_for_focus_selects_bounded_cross_domain_context(session) -> None:
    _seed_activity(session)

    async def wearable(day: date):
        return {
            "status": "ok",
            "date": day.isoformat(),
            "sleep": {"status": "ok", "duration_minutes": 420},
            "evidence_ids": ["wearable-readiness-1"],
            "freshness": {
                "recorded_at": "2026-08-01T08:00:00+00:00",
                "status": "open_wearables_summary",
            },
            "coverage": {"status": "partial", "ratio": 0.8},
            "limitations": [],
        }

    result = await resolve_wellness_context(
        session,
        ActivityContextResolveRequest(
            question_kind="caffeine_for_focus",
            date="2026-08-01",
            start=datetime(2026, 8, 1, 9, tzinfo=UTC),
            end=datetime(2026, 8, 1, 10, tzinfo=UTC),
            lookback_days=7,
        ),
        default_timezone="UTC",
        wearable_reader=wearable,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    assert result["selected_domains"] == [
        "activity",
        "wearable",
        "calendar",
        "nutrition",
        "time",
    ]
    assert result["contexts"]["activity"]["focus"]["status"] == "ok"
    assert result["contexts"]["nutrition"]["kind"] == "confirmed_caffeine_ledger"
    assert result["contexts"]["nutrition"]["context"]["confirmed_caffeine_mg"] == 0.0
    assert result["contexts"]["nutrition"]["status"] == "insufficient_data"
    assert result["decision_ready"] is False
    assert result["contexts"]["time"]["local_now"] == "2026-08-01T12:00:00+00:00"
    assert {
        "specialized_policy_numbers_are_not_recomputed",
        "missing_data_is_not_zero",
        "association_is_not_causation",
        "context_only_not_a_final_wellness_decision",
    }.issubset(result["boundaries"])
    assert "context_resolver_does_not_recalculate_caffeine_policy" in result["limitations"]
    assert "private.app.identity" not in json.dumps(result)


async def test_caffeine_for_focus_supports_public_fixed_offset_timezone(session) -> None:
    result = await resolve_wellness_context(
        session,
        ActivityContextResolveRequest(
            question_kind="caffeine_for_focus",
            date="2026-08-01",
            start=datetime(2026, 8, 1, 1, tzinfo=UTC),
            end=datetime(2026, 8, 1, 2, tzinfo=UTC),
            timezone="UTC+09:00",
        ),
        default_timezone="UTC",
        now=datetime(2026, 8, 1, 3, tzinfo=UTC),
    )

    assert result["timezone"] == "UTC+09:00"
    assert result["contexts"]["nutrition"]["kind"] == "confirmed_caffeine_ledger"
    assert result["contexts"]["nutrition"]["context"]["local_date"] == "2026-08-01"
    assert result["contexts"]["nutrition"]["context"]["timezone"] == "UTC+09:00"
    assert result["contexts"]["time"]["local_now"] == "2026-08-01T12:00:00+09:00"


async def test_resolver_uses_injected_now_for_an_implicit_local_date(session) -> None:
    result = await resolve_wellness_context(
        session,
        ActivityContextResolveRequest(question_kind="activity_summary"),
        default_timezone="Asia/Seoul",
        now=datetime(2026, 8, 1, 16, tzinfo=UTC),
    )

    assert result["date"] == "2026-08-02"
    assert result["timezone"] == "Asia/Seoul"


@pytest.mark.parametrize("question_kind", ("overwork", "caffeine_for_focus"))
async def test_resolver_uses_injected_now_for_overwork_expiry(
    session,
    question_kind,
) -> None:
    for observed_day in (
        date(2026, 7, 29),
        date(2026, 7, 30),
        date(2026, 7, 31),
        date(2026, 8, 1),
    ):
        start = datetime.combine(observed_day, datetime.min.time(), tzinfo=UTC)
        ingest_activity_batch(
            session,
            ActivityBatchIn(
                source_provider="resolver-expiry",
                source_device="desktop-expiry",
                platform=ActivityPlatform.MACOS,
                capability=ActivityCapability.DETAILED,
                timezone="UTC",
                records=[
                    AppIntervalRecord(
                        source_record_id=f"resolver-expiry-{observed_day}",
                        start_at=start,
                        end_at=start + timedelta(hours=12),
                        state="active",
                        app_id="editor",
                    )
                ],
            ),
            now=start + timedelta(hours=13),
        )

    wall_clock_expired_at = datetime.now(UTC) - timedelta(minutes=1)
    assert wall_clock_expired_at > datetime(2026, 8, 1, 12, tzinfo=UTC)
    daily_rows = list(
        session.scalars(
            select(WellnessEvent).where(
                WellnessEvent.event_type == DAY_SUMMARY_EVENT,
            )
        )
    )
    assert len(daily_rows) == 4
    for row in daily_rows:
        row.expires_at = wall_clock_expired_at
    session.flush()

    request = ActivityContextResolveRequest(
        question_kind=question_kind,
        date="2026-08-01",
        start=(
            datetime(2026, 8, 1, 9, tzinfo=UTC)
            if question_kind == "caffeine_for_focus"
            else None
        ),
        end=(
            datetime(2026, 8, 1, 10, tzinfo=UTC)
            if question_kind == "caffeine_for_focus"
            else None
        ),
    )
    result = await resolve_wellness_context(
        session,
        request,
        default_timezone="UTC",
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )
    activity = result["contexts"]["activity"]
    overwork = (
        activity["overwork"]
        if question_kind == "caffeine_for_focus"
        else activity
    )

    assert overwork["status"] == "ok"
    assert overwork["metrics"]["lookback_baseline_delta"] == {
        "status": "ok",
        "days_with_data": 3,
        "lookback_days": 7,
        "baseline_minutes": 720.0,
        "delta_minutes": 0.0,
        "delta_percent": 0.0,
    }


async def test_resolver_rejects_a_window_from_another_local_date(session) -> None:
    with pytest.raises(WellnessContextRangeError, match="same local day"):
        await resolve_wellness_context(
            session,
            ActivityContextResolveRequest(
                question_kind="focus",
                date="2026-08-01",
                start=datetime(2026, 8, 2, 9, tzinfo=UTC),
                end=datetime(2026, 8, 2, 10, tzinfo=UTC),
            ),
            default_timezone="UTC",
            now=datetime(2026, 8, 2, 12, tzinfo=UTC),
        )


async def test_wearable_context_from_another_day_is_not_combined(session) -> None:
    _seed_activity(session)

    async def wrong_day(day: date):
        return {
            "status": "ok",
            "date": "2026-08-02",
            "evidence_ids": ["wrong-day"],
        }

    result = await resolve_wellness_context(
        session,
        ActivityContextResolveRequest(
            question_kind="focus",
            date="2026-08-01",
            start=datetime(2026, 8, 1, 9, tzinfo=UTC),
            end=datetime(2026, 8, 1, 10, tzinfo=UTC),
        ),
        default_timezone="UTC",
        wearable_reader=wrong_day,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    assert result["contexts"]["wearable"]["status"] == "insufficient_data"
    assert result["contexts"]["wearable"]["reason"] == "wearable_context_date_mismatch"
    assert result["contexts"]["wearable"]["evidence_ids"] == []
    assert "wrong-day" not in json.dumps(result)


async def test_wearable_context_adds_coverage_freshness_and_evidence_limitation(
    session,
) -> None:
    _seed_activity(session)

    async def sparse_readiness(day: date):
        return {
            "status": "ok",
            "date": day.isoformat(),
            "sleep_debt": {
                "status": "ok",
                "recorded_at": "2026-08-01T07:00:00+00:00",
            },
            "hrv": {
                "status": "insufficient_data",
                "recorded_at": "2026-08-01T08:00:00+00:00",
            },
        }

    result = await resolve_wellness_context(
        session,
        ActivityContextResolveRequest(
            question_kind="focus",
            date="2026-08-01",
            start=datetime(2026, 8, 1, 9, tzinfo=UTC),
            end=datetime(2026, 8, 1, 10, tzinfo=UTC),
        ),
        default_timezone="UTC",
        wearable_reader=sparse_readiness,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )
    wearable = result["contexts"]["wearable"]

    assert wearable["coverage"] == {
        "status": "readiness_blocks",
        "ratio": 0.5,
        "usable_blocks": 1,
        "total_blocks": 2,
    }
    assert wearable["freshness"] == {
        "recorded_at": "2026-08-01T08:00:00+00:00",
        "status": "derived_from_readiness_blocks",
    }
    assert "wearable_readiness_evidence_ids_unavailable" in wearable["limitations"]


def test_fresh_charge_cannot_mask_stale_used_sleep_block() -> None:
    wearable = resolver_module._normalize_wearable_context(
        {
            "status": "ok",
            "date": "2026-08-01",
            "sleep_debt": {
                "status": "ok",
                "recorded_at": "2026-07-20T07:00:00+00:00",
            },
            "charge": {
                "status": "ok",
                "freshest_at": "2026-08-01T09:00:00+00:00",
            },
            "source_refs": [
                {
                    "domain": "wearable",
                    "record_id": "wearable-freshness-regression",
                    "source_provider": "open-wearables",
                    "upstream_provider": "garmin",
                    "resource_type": "health_score",
                    "observed_at": "2026-08-01T09:00:00+00:00",
                    "schema_version": 1,
                    "derived_by": "open-wearables.daily-readiness.v1",
                }
            ],
            "freshness": {
                "recorded_at": "2026-08-01T09:00:00+00:00",
                "status": "derived_from_readiness_blocks",
            },
            "coverage": {
                "status": "readiness_blocks",
                "ratio": 1.0,
            },
        },
        day=date(2026, 8, 1),
    )

    ready, blockers = _context_ready_for_preset(
        "recovery",
        ("wearable",),
        {"wearable": wearable},
        day=date(2026, 8, 1),
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
        timezone=UTC,
    )

    assert ready is False
    assert (
        "wearable.sleep_debt.freshness_outside_requested_day"
        in blockers
    )
    assert "wearable.charge.freshness_outside_requested_day" not in blockers


async def test_wearable_source_refs_are_allowlisted_and_drive_evidence_ids(
    session,
) -> None:
    _seed_activity(session)

    async def readiness(day: date):
        return {
            "status": "ok",
            "date": day.isoformat(),
            "sleep_debt": {
                "status": "ok",
                "recorded_at": "2026-08-01T07:00:00+00:00",
            },
            "source_refs": [
                {
                    "domain": "wearable",
                    "record_id": "score-1",
                    "source_provider": "open-wearables",
                    "upstream_provider": "garmin",
                    "resource_type": "health_score",
                    "observed_at": "2026-08-01T07:00:00+00:00",
                    "schema_version": 1,
                    "derived_by": "open-wearables.daily-readiness.v1",
                    "private_payload": {"token": "must-not-leak"},
                },
                {
                    "domain": "nutrition",
                    "record_id": "wrong-domain",
                    "source_provider": "open-wearables",
                    "resource_type": "health_score",
                    "observed_at": "2026-08-01T07:00:00+00:00",
                    "schema_version": 1,
                },
                {
                    "domain": "wearable",
                    "record_id": "conflicted",
                    "source_provider": "open-wearables",
                    "upstream_provider": "garmin",
                    "resource_type": "health_score",
                    "observed_at": "2026-08-01T06:00:00+00:00",
                    "schema_version": 1,
                    "derived_by": "open-wearables.daily-readiness.v1",
                },
                {
                    "domain": "wearable",
                    "record_id": "conflicted",
                    "source_provider": "open-wearables",
                    "upstream_provider": "oura",
                    "resource_type": "health_score",
                    "observed_at": "2026-08-01T07:00:00+00:00",
                    "schema_version": 1,
                    "derived_by": "open-wearables.daily-readiness.v1",
                },
                {
                    "domain": "wearable",
                    "record_id": "invalid-date",
                    "source_provider": "open-wearables",
                    "upstream_provider": "garmin",
                    "resource_type": "health_score",
                    "observed_at": "not-a-date",
                    "schema_version": 1,
                    "derived_by": "open-wearables.daily-readiness.v1",
                },
            ],
            "evidence_ids": ["spoofed-legacy-id"],
            "freshness": {
                "recorded_at": "2026-08-01T07:00:00+00:00",
                "status": "derived_from_readiness_blocks",
            },
            "coverage": {"status": "readiness_blocks", "ratio": 1.0},
            "limitations": [],
        }

    result = await resolve_wellness_context(
        session,
        ActivityContextResolveRequest(
            question_kind="focus",
            date="2026-08-01",
            start=datetime(2026, 8, 1, 9, tzinfo=UTC),
            end=datetime(2026, 8, 1, 10, tzinfo=UTC),
        ),
        default_timezone="UTC",
        wearable_reader=readiness,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )
    wearable = result["contexts"]["wearable"]

    assert wearable["source_refs"] == [
        {
            "domain": "wearable",
            "record_id": "score-1",
            "source_provider": "open-wearables",
            "upstream_provider": "garmin",
            "resource_type": "health_score",
            "observed_at": "2026-08-01T07:00:00+00:00",
            "schema_version": 1,
            "derived_by": "open-wearables.daily-readiness.v1",
        }
    ]
    assert wearable["evidence_ids"] == ["score-1"]
    assert result["source_refs"] == wearable["source_refs"]
    assert "private_payload" not in json.dumps(result)
    assert "wrong-domain" not in json.dumps(result)
    assert "conflicted" not in json.dumps(result)


async def test_invalid_supplied_wearable_source_refs_cannot_fall_back_to_legacy_ids(
    session,
) -> None:
    _seed_activity(session)

    async def readiness(day: date):
        return {
            "status": "ok",
            "date": day.isoformat(),
            "sleep_debt": {
                "status": "ok",
                "recorded_at": "2026-08-01T07:00:00+00:00",
            },
            "source_refs": [
                {
                    "domain": "nutrition",
                    "record_id": "wrong-domain",
                    "source_provider": "open-wearables",
                    "resource_type": "health_score",
                    "observed_at": "2026-08-01T07:00:00+00:00",
                    "schema_version": 1,
                },
                {
                    "domain": "wearable",
                    "record_id": "bad-date",
                    "source_provider": "open-wearables",
                    "resource_type": "health_score",
                    "observed_at": "not-a-date",
                    "schema_version": 1,
                },
            ],
            "evidence_ids": ["forged-legacy-id"],
            "freshness": {
                "recorded_at": "2026-08-01T07:00:00+00:00",
                "status": "derived_from_readiness_blocks",
            },
            "coverage": {"status": "readiness_blocks", "ratio": 1.0},
        }

    result = await resolve_wellness_context(
        session,
        ActivityContextResolveRequest(
            question_kind="recovery",
            date="2026-08-01",
        ),
        default_timezone="UTC",
        wearable_reader=readiness,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )
    wearable = result["contexts"]["wearable"]

    assert wearable["source_refs"] == []
    assert wearable["evidence_ids"] == []
    assert "forged-legacy-id" not in json.dumps(result)
    assert result["context_ready"] is False
    assert (
        "wearable.source_refs_missing"
        in result["context_readiness_blockers"]
    )
    assert "invalid-date" not in json.dumps(result)
    assert "spoofed-legacy-id" not in json.dumps(result)


@pytest.mark.parametrize(
    ("resource_type", "observed_at"),
    (
        ("health_score", "2030-01-01T07:00:00+00:00"),
        ("health_score", "2026-07-20T07:00:00+00:00"),
        ("sleep_summary", "2026-08-02"),
    ),
)
async def test_wearable_source_refs_outside_request_time_are_not_usable(
    session,
    resource_type: str,
    observed_at: str,
) -> None:
    _seed_activity(session)

    async def readiness(day: date):
        return {
            "status": "ok",
            "date": day.isoformat(),
            "sleep_debt": {
                "status": "ok",
                "recorded_at": "2026-08-01T07:00:00+00:00",
            },
            "source_refs": [
                {
                    "domain": "wearable",
                    "record_id": "temporally-invalid",
                    "source_provider": "open-wearables",
                    "upstream_provider": "garmin",
                    "resource_type": resource_type,
                    "observed_at": observed_at,
                    "schema_version": 1,
                    "derived_by": "open-wearables.daily-readiness.v1",
                }
            ],
            "freshness": {
                "recorded_at": "2026-08-01T07:00:00+00:00",
                "status": "derived_from_readiness_blocks",
            },
            "coverage": {"status": "readiness_blocks", "ratio": 1.0},
        }

    result = await resolve_wellness_context(
        session,
        ActivityContextResolveRequest(
            question_kind="recovery",
            date="2026-08-01",
        ),
        default_timezone="UTC",
        wearable_reader=readiness,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    wearable = result["contexts"]["wearable"]
    assert wearable["source_refs"] == []
    assert wearable["evidence_ids"] == []
    assert result["context_ready"] is False
    assert (
        "wearable.source_refs_missing"
        in result["context_readiness_blockers"]
    )
    assert "temporally-invalid" not in json.dumps(result)


def test_previous_night_sleep_summary_source_ref_remains_valid() -> None:
    wearable = resolver_module._normalize_wearable_context(
        {
            "status": "ok",
            "date": "2026-08-01",
            "actual_sleep": {
                "status": "ok",
                "recorded_at": "2026-08-01T08:00:00+00:00",
            },
            "source_refs": [
                {
                    "domain": "wearable",
                    "record_id": "sleep-summary-previous-night",
                    "source_provider": "open-wearables",
                    "upstream_provider": "oura",
                    "resource_type": "sleep_summary",
                    "observed_at": "2026-07-31",
                    "schema_version": 1,
                    "derived_by": "open-wearables.daily-readiness.v1",
                }
            ],
            "freshness": {
                "recorded_at": "2026-08-01T08:00:00+00:00",
                "status": "derived_from_readiness_blocks",
            },
            "coverage": {"status": "readiness_blocks", "ratio": 1.0},
        },
        day=date(2026, 8, 1),
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
        timezone=UTC,
    )

    assert wearable["evidence_ids"] == ["sleep-summary-previous-night"]


def test_calendar_context_excludes_all_day_and_actual_sleep_rows(session) -> None:
    generation = "resolver-visible-generation"
    session.add_all(
        [
            CalendarEventMirror(
                external_id="work",
                calendar_source=CalendarSource.GOOGLE,
                connection_generation=generation,
                summary="Work",
                start_at=datetime(2026, 8, 1, 9, tzinfo=UTC),
                end_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
            ),
            CalendarEventMirror(
                external_id="all-day",
                calendar_source=CalendarSource.GOOGLE,
                connection_generation=generation,
                summary="Holiday",
                start_at=datetime(2026, 8, 1, tzinfo=UTC),
                end_at=datetime(2026, 8, 2, tzinfo=UTC),
                is_all_day=True,
            ),
            CalendarEventMirror(
                external_id="actual-sleep",
                calendar_source=CalendarSource.GOOGLE,
                connection_generation=generation,
                summary="Sleep",
                start_at=datetime(2026, 8, 1, 1, tzinfo=UTC),
                end_at=datetime(2026, 8, 1, 8, tzinfo=UTC),
                healthmes_kind=HealthmesEventKind.ACTUAL_SLEEP.value,
            ),
        ]
    )
    session.flush()

    context = calendar_context(
        session,
        day=date(2026, 8, 1),
        timezone="UTC",
        visibility=CalendarVisibility(
            {CalendarSource.GOOGLE: generation}
        ),
    )

    assert context["event_count"] == 1
    assert context["busy_minutes"] == 60.0


def _nutrition_snapshot(
    *,
    scope: str = "caffeine_sleep",
    intended_at: str = "2026-08-01T12:00:00+00:00",
    complete: bool = True,
    include_candidate: bool = True,
    candidate_amount: dict | None = None,
    ledger_status: str | None = None,
    ledger_date: str = "2026-08-01",
    ledger_timezone: str = "UTC",
    confirmed_caffeine_mg: object = 0.0,
) -> dict:
    amount = (
        candidate_amount
        if candidate_amount is not None
        else {"kind": "exact", "exact": 100, "unit": "mg"}
    )
    return {
        "request": {
            "scope": scope,
            "requested_at": intended_at,
            "intended_consumption_at": intended_at,
        },
        "candidate": {
            "resolved_items": (
                [
                    {
                        "nutrients": [
                            {
                                "nutrient": "caffeine",
                                "amount": amount,
                            }
                        ]
                    }
                ]
                if include_candidate
                else []
            )
        },
        "specialized_evidence": {
            "caffeine": {
                "status": (
                    ledger_status
                    if ledger_status is not None
                    else "known"
                    if complete
                    else "incomplete"
                ),
                "local_date": ledger_date,
                "timezone": ledger_timezone,
                "confirmed_caffeine_mg": confirmed_caffeine_mg,
                "total_intake_complete": complete,
            }
        },
        "boundaries": {"caffeine_total_intake_complete": complete},
        "evidence_event_ids": ["nutrition-evidence"],
        "limitations": [],
    }


def test_caffeine_nutrition_context_is_ready_only_with_complete_matching_evidence(
    session,
    monkeypatch,
) -> None:
    import healthmes.activity.resolver as resolver_module

    monkeypatch.setattr(
        resolver_module,
        "nutrition_decision_context",
        lambda session, request_id: _nutrition_snapshot(),
    )

    result = nutrition_context(
        session,
        day=date(2026, 8, 1),
        timezone="UTC",
        request_id=uuid.uuid4(),
    )

    assert result["status"] == "ok"
    assert result["candidate_ledger_complete"] is True
    assert result["decision_ready"] is False
    assert result["evidence_ids"] == ["nutrition-evidence"]


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    (
        (
            _nutrition_snapshot(scope="general_wellness"),
            "nutrition_request_scope_is_not_caffeine_sleep",
        ),
        (
            _nutrition_snapshot(intended_at="2026-08-02T12:00:00+00:00"),
            "nutrition_request_date_mismatch",
        ),
        (
            _nutrition_snapshot(complete=False),
            "caffeine_day_not_confirmed_complete",
        ),
        (
            _nutrition_snapshot(ledger_status="incomplete"),
            "caffeine_ledger_status_not_known",
        ),
        (
            _nutrition_snapshot(ledger_date="2026-07-31"),
            "caffeine_ledger_date_mismatch",
        ),
        (
            _nutrition_snapshot(ledger_timezone="Asia/Seoul"),
            "caffeine_ledger_timezone_mismatch",
        ),
        (
            _nutrition_snapshot(confirmed_caffeine_mg=None),
            "caffeine_ledger_amount_missing",
        ),
        (
            _nutrition_snapshot(confirmed_caffeine_mg=float("nan")),
            "caffeine_ledger_amount_missing",
        ),
        (
            _nutrition_snapshot(include_candidate=False),
            "candidate_caffeine_estimate_missing",
        ),
        (
            _nutrition_snapshot(
                candidate_amount={"kind": "unknown", "unit": "mg"}
            ),
            "candidate_caffeine_estimate_missing",
        ),
        (
            _nutrition_snapshot(
                candidate_amount={
                    "kind": "exact",
                    "exact": 100,
                    "unit": "g",
                }
            ),
            "candidate_caffeine_estimate_missing",
        ),
        (
            _nutrition_snapshot(
                candidate_amount={"kind": "exact", "unit": "mg"}
            ),
            "candidate_caffeine_estimate_missing",
        ),
    ),
)
def test_caffeine_nutrition_context_fails_closed(
    session,
    monkeypatch,
    snapshot: dict,
    reason: str,
) -> None:
    import healthmes.activity.resolver as resolver_module

    monkeypatch.setattr(
        resolver_module,
        "nutrition_decision_context",
        lambda session, request_id: snapshot,
    )

    result = nutrition_context(
        session,
        day=date(2026, 8, 1),
        timezone="UTC",
        request_id=uuid.uuid4(),
    )

    assert result["status"] == "insufficient_data"
    assert result["decision_ready"] is False
    assert result["reason"] == reason


async def test_wearable_context_recovers_nested_timestamp_from_unavailable_freshness(
    session,
) -> None:
    _seed_activity(session)

    async def nested_readiness(day: date):
        return {
            "status": "ok",
            "date": day.isoformat(),
            "actual_sleep": {
                "status": "ok",
                "last_night": {
                    "recorded_at": "2026-08-01T08:00:00+00:00",
                },
            },
            "freshness": {
                "recorded_at": None,
                "status": "unavailable",
            },
        }

    result = await resolve_wellness_context(
        session,
        ActivityContextResolveRequest(
            question_kind="focus",
            date="2026-08-01",
            start=datetime(2026, 8, 1, 9, tzinfo=UTC),
            end=datetime(2026, 8, 1, 10, tzinfo=UTC),
        ),
        default_timezone="UTC",
        wearable_reader=nested_readiness,
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    assert result["contexts"]["wearable"]["freshness"] == {
        "recorded_at": "2026-08-01T08:00:00+00:00",
        "status": "derived_from_readiness_blocks",
    }
