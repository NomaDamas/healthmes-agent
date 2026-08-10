import datetime as dt
import json
import uuid

from fastmcp import Client, FastMCP
from sqlalchemy import select

from healthmes.activity.aggregation import rebuild_day_summaries
from healthmes.activity.contracts import (
    ActivityBatchIn,
    ActivityCapability,
    ActivityPlatform,
    AppIntervalRecord,
)
from healthmes.activity.mcp import register_activity_tools
from healthmes.activity.repository import DAY_SUMMARY_EVENT
from healthmes.activity.service import ingest_activity_batch
from healthmes.mcp_server import server as server_module
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
from healthmes.store import WellnessEvent


def _seed_activity(store_factory, pinned_tz) -> None:
    with store_factory() as session:
        ingest_activity_batch(
            session,
            ActivityBatchIn(
                source_provider="mcp-test-desktop",
                source_device="mcp-desktop-1",
                platform=ActivityPlatform.MACOS,
                capability=ActivityCapability.DETAILED,
                timezone="Asia/Seoul",
                records=[
                    AppIntervalRecord(
                        source_record_id="mcp-idle-before",
                        start_at=dt.datetime(2026, 7, 31, 15, tzinfo=dt.UTC),
                        end_at=dt.datetime(2026, 8, 1, 1, tzinfo=dt.UTC),
                        state="idle",
                    ),
                    AppIntervalRecord(
                        source_record_id="mcp-activity",
                        start_at=dt.datetime(2026, 8, 1, 1, tzinfo=dt.UTC),
                        end_at=dt.datetime(2026, 8, 1, 2, tzinfo=dt.UTC),
                        state="active",
                        app_id="private.mcp.app",
                        category="productivity",
                        launches=3,
                    ),
                    AppIntervalRecord(
                        source_record_id="mcp-idle-after",
                        start_at=dt.datetime(2026, 8, 1, 2, tzinfo=dt.UTC),
                        end_at=dt.datetime(2026, 8, 1, 15, tzinfo=dt.UTC),
                        state="idle",
                    ),
                ],
            ),
            rebuild_summaries=False,
        )
        rebuild_day_summaries(
            session,
            day=dt.date(2026, 8, 1),
            timezone=pinned_tz,
        )
        session.commit()


def _seed_fixed_offset_caffeine_request(store_factory) -> uuid.UUID:
    timezone = "UTC+09:00"
    consumed_at = dt.datetime.fromisoformat(
        "2026-08-01T09:00:00+09:00"
    )

    def item(name: str, amount_mg: float) -> NormalizedIntakeItem:
        return NormalizedIntakeItem(
            name=name,
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
                        exact=amount_mg,
                        estimation_basis="owner_statement",
                    ),
                    confidence=Confidence.HIGH,
                    origin=EvidenceOrigin.USER,
                ),
            ),
            confidence=Confidence.HIGH,
        )

    consumed_id = uuid.uuid4()
    outcome_id = uuid.uuid4()
    candidate_id = uuid.uuid4()
    request_id = uuid.uuid4()
    candidate_at = dt.datetime.fromisoformat(
        "2026-08-01T15:00:00+09:00"
    )
    with store_factory() as session:
        settings = server_module._active_settings()
        create_interaction(
            session,
            settings,
            IntakeInteraction(
                interaction_id=consumed_id,
                operation_fingerprint="e" * 64,
                intent=IntakeIntent.LOG_CONSUMED,
                modality=CaptureModality.TEXT,
                observed_at=consumed_at,
                recorded_at=consumed_at,
                timezone=timezone,
                source="mcp-activity-test",
                source_text="80 mg caffeine coffee",
                media_path=None,
                nutrition_observation_id=None,
                items=(item("morning coffee", 80),),
            ),
        )
        persist_outcome(
            session,
            IntakeOutcome(
                outcome_id=outcome_id,
                operation_fingerprint="f" * 64,
                interaction_id=consumed_id,
                status=IntakeOutcomeStatus.CONSUMED,
                confirmed_at=consumed_at + dt.timedelta(minutes=1),
                source="mcp-activity-test",
                consumed_at=consumed_at,
            ),
        )
        persist_daily_confirmation(
            session,
            DailyIntakeConfirmation(
                confirmation_id=uuid.uuid4(),
                local_date=dt.date(2026, 8, 1),
                timezone=timezone,
                observation_ids=(),
                outcome_ids=(outcome_id,),
                total_intake_complete=True,
                confirmed_at=consumed_at + dt.timedelta(minutes=2),
                source="mcp-activity-test",
            ),
        )
        create_interaction(
            session,
            settings,
            IntakeInteraction(
                interaction_id=candidate_id,
                operation_fingerprint="1" * 64,
                intent=IntakeIntent.ASK_BEFORE_INTAKE,
                modality=CaptureModality.TEXT,
                observed_at=candidate_at,
                recorded_at=candidate_at,
                timezone=timezone,
                source="mcp-activity-test",
                source_text="Can I drink this coffee?",
                media_path=None,
                nutrition_observation_id=None,
                items=(item("afternoon coffee", 100),),
            ),
        )
        persist_decision_request(
            session,
            IntakeDecisionRequest(
                request_id=request_id,
                operation_fingerprint="2" * 64,
                interaction_id=candidate_id,
                scope=DecisionScope.CAFFEINE_SLEEP,
                requested_at=candidate_at,
                source="mcp-activity-test",
                intended_consumption_at=candidate_at,
            ),
        )
        session.commit()
    return request_id


async def test_activity_context_tools_return_identity_free_stored_summaries(
    mcp_client,
    call_tool,
    store_factory,
    pinned_tz,
) -> None:
    _seed_activity(store_factory, pinned_tz)

    summary = await call_tool(
        mcp_client,
        "get_activity_summary",
        {"date": "2026-08-01"},
    )
    focus = await call_tool(
        mcp_client,
        "get_focus_context",
        {
            "start": "2026-08-01T01:00:00Z",
            "end": "2026-08-01T02:00:00Z",
        },
    )
    overwork = await call_tool(
        mcp_client,
        "get_overwork_context",
        {"date": "2026-08-01", "lookback_days": 7},
    )

    assert summary["status"] == "ok"
    assert summary["timezone"] == "UTC+09:00"
    assert summary["total_active_minutes"] == 60.0
    assert focus["status"] == "ok"
    assert focus["metrics"]["total_active_minutes"] == 60.0
    assert overwork["status"] == "ok"
    assert overwork["risk_level"] == "not_elevated"
    serialized = json.dumps({"summary": summary, "focus": focus, "overwork": overwork})
    assert "private.mcp.app" not in serialized


async def test_resolver_tool_selects_only_activity_for_activity_summary(
    mcp_client,
    call_tool,
    store_factory,
    pinned_tz,
) -> None:
    _seed_activity(store_factory, pinned_tz)

    result = await call_tool(
        mcp_client,
        "resolve_wellness_context",
        {
            "question_kind": "activity_summary",
            "date": "2026-08-01",
        },
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
    assert result["boundaries"] == [
        "specialized_policy_numbers_are_not_recomputed",
        "missing_data_is_not_zero",
        "association_is_not_causation",
        "context_only_not_a_final_wellness_decision",
        "decision_ready_requires_healthmes_decision_agent",
    ]


async def test_resolver_tool_supports_fixed_offset_caffeine_day(
    mcp_client,
    call_tool,
) -> None:
    result = await call_tool(
        mcp_client,
        "resolve_wellness_context",
        {
            "question_kind": "caffeine_for_focus",
            "date": "2026-08-01",
            "start": "2026-08-01T01:00:00Z",
            "end": "2026-08-01T02:00:00Z",
        },
    )

    assert result["timezone"] == "UTC+09:00"
    assert result["contexts"]["nutrition"]["kind"] == "confirmed_caffeine_ledger"
    assert result["contexts"]["nutrition"]["context"]["local_date"] == "2026-08-01"
    assert result["contexts"]["nutrition"]["context"]["timezone"] == "UTC+09:00"


async def test_resolver_tool_uses_actual_fixed_offset_caffeine_request(
    mcp_client,
    call_tool,
    store_factory,
) -> None:
    fixed_settings = server_module._active_settings().model_copy(
        update={"timezone": "UTC+09:00"}
    )
    server_module.set_settings(fixed_settings)
    server_module.set_timezone(None)
    request_id = _seed_fixed_offset_caffeine_request(store_factory)

    result = await call_tool(
        mcp_client,
        "resolve_wellness_context",
        {
            "question_kind": "caffeine_for_focus",
            "date": "2026-08-01",
            "start": "2026-08-01T01:00:00Z",
            "end": "2026-08-01T02:00:00Z",
            "nutrition_request_id": str(request_id),
        },
    )

    nutrition = result["contexts"]["nutrition"]
    assert result["timezone"] == "UTC+09:00"
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


async def test_resolver_tool_drops_unregistered_wearable_fields(
    store_factory,
    pinned_tz,
) -> None:
    local_mcp = FastMCP("activity-wearable-allowlist")

    async def malicious_readiness(date: str | None = None) -> dict:
        return {
            "status": "ok",
            "date": date,
            "raw_timeseries": [{"secret": "mcp-raw-secret"}],
            "hrv": {
                "status": "ok",
                "current": {
                    "date": date,
                    "value": 42.0,
                    "raw_sample": "mcp-nested-secret",
                },
                "baseline_median": 50.0,
                "raw_timeseries": [40, 41, 42],
            },
            "charge": {
                "status": "ok",
                "entries": [
                    {
                        "category": "body_battery",
                        "provider": "garmin",
                        "value": 55.0,
                        "raw_payload": "mcp-entry-secret",
                    }
                ],
            },
            "limitations": [],
        }

    register_activity_tools(
        local_mcp,
        store_session_factory=store_factory,
        timezone_resolver=lambda: pinned_tz,
        readiness_reader=malicious_readiness,
    )

    async with Client(local_mcp) as client:
        result = await client.call_tool(
            "resolve_wellness_context",
            {
                "question_kind": "focus",
                "date": "2026-08-01",
                "start": "2026-08-01T01:00:00Z",
                "end": "2026-08-01T02:00:00Z",
            },
        )
    payload = (
        result.data
        if isinstance(result.data, dict)
        else result.structured_content
    )
    assert isinstance(payload, dict)
    wearable = payload["contexts"]["wearable"]
    assert wearable["hrv"]["current"]["value"] == 42.0
    assert wearable["charge"]["entries"][0]["value"] == 55.0
    serialized = json.dumps(wearable)
    assert "raw_timeseries" not in serialized
    assert "raw_sample" not in serialized
    assert "raw_payload" not in serialized
    assert "mcp-raw-secret" not in serialized


async def test_expired_activity_summary_is_hidden_from_mcp_before_maintenance(
    mcp_client,
    call_tool,
    store_factory,
    pinned_tz,
) -> None:
    _seed_activity(store_factory, pinned_tz)
    with store_factory() as session:
        daily = session.scalar(
            select(WellnessEvent).where(
                WellnessEvent.event_type == DAY_SUMMARY_EVENT
            )
        )
        assert daily is not None
        daily.expires_at = dt.datetime(2026, 8, 2, tzinfo=dt.UTC)
        session.commit()

    result = await call_tool(
        mcp_client,
        "get_activity_summary",
        {"date": "2026-08-01"},
    )

    assert result["status"] == "insufficient_data"
    assert result["reason"] == "no_activity_summary"
