import uuid
from datetime import UTC, datetime, timedelta

from healthmes.decision import (
    ContextAccessLayer,
    ContextAccessPolicy,
    ContextProviderRegistry,
    ContextQuery,
    ContextStatus,
    DecisionCaller,
    DecisionRequest,
    DomainAccessGrant,
    ExecutionScope,
    NutritionContextProvider,
)
from healthmes.nutrition.intake_contracts import (
    CaptureModality,
    DecisionScope,
    IntakeDecisionRequest,
    IntakeIntent,
    IntakeInteraction,
    IntakeOutcome,
    IntakeOutcomeStatus,
    StructuredIntakeSnapshot,
    decision_request_to_payload,
    interaction_to_payload,
    outcome_to_payload,
)
from healthmes.nutrition.intake_query import search_intake_history
from healthmes.store import WellnessEvent


def _interaction_event(
    *,
    index: int,
    observed_at: datetime,
) -> WellnessEvent:
    interaction_id = uuid.uuid4()
    interaction = IntakeInteraction(
        interaction_id=interaction_id,
        operation_fingerprint=f"{index + 1:064x}",
        intent=IntakeIntent.LOG_CONSUMED,
        modality=CaptureModality.TEXT,
        observed_at=observed_at,
        recorded_at=observed_at + timedelta(minutes=1),
        timezone="UTC",
        source="decision-scan-test",
        source_text=f"meal-{index}",
        media_path=None,
        nutrition_observation_id=None,
        items=(),
    )
    return WellnessEvent(
        event_type="nutrition.interaction.v1",
        schema_version=1,
        observed_at=observed_at,
        recorded_at=interaction.recorded_at,
        timezone="UTC",
        source_provider="nutrition-interaction",
        source_device="decision-scan-test",
        source_record_id=str(interaction_id),
        capture_method="text",
        quality_flags={},
        confidence=1,
        coverage=1,
        sensitivity="wellness",
        consent_scope="personal",
        expires_at=None,
        payload=interaction_to_payload(interaction),
        derived_from=None,
    )


def test_intake_history_applies_time_filter_and_database_scan_budget(
    session,
):
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    in_range = [
        _interaction_event(
            index=index,
            observed_at=start + timedelta(hours=9 + index),
        )
        for index in range(3)
    ]
    out_of_range = [
        _interaction_event(
            index=10 + index,
            observed_at=end + timedelta(days=1, hours=index),
        )
        for index in range(5)
    ]
    session.add_all([*in_range, *out_of_range])
    session.flush()

    result = search_intake_history(
        session,
        start=start,
        end=end,
        limit=1,
        max_scan_records=2,
    )

    assert result["count"] == 1
    assert result["records"][0]["interaction_id"] == str(
        in_range[-1].source_record_id
    )
    assert result["truncated"] is True
    assert result["coverage"]["scan_limit"] == 2
    assert result["coverage"]["scanned_event_rows"] == 2
    assert result["coverage"]["scanned_records"] == 2
    assert result["limitations"] == [
        "nutrition_history_scan_limit_reached"
    ]


async def test_fallback_history_uses_retained_snapshot_observation_time(
    session,
):
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    outcome_interaction_id = uuid.uuid4()
    request_interaction_id = uuid.uuid4()
    outcome_id = uuid.uuid4()
    request_id = uuid.uuid4()
    outcome_snapshot = StructuredIntakeSnapshot(
        interaction_id=outcome_interaction_id,
        intent=IntakeIntent.LOG_CONSUMED,
        modality=CaptureModality.TEXT,
        observed_at=start + timedelta(hours=9),
        timezone="UTC",
        source="decision-scan-test",
        nutrition_observation_id=None,
        items=(),
    )
    outcome = IntakeOutcome(
        outcome_id=outcome_id,
        operation_fingerprint="a" * 64,
        interaction_id=outcome_interaction_id,
        status=IntakeOutcomeStatus.NOT_CONSUMED,
        confirmed_at=end + timedelta(days=3),
        source="decision-scan-test",
        intake_snapshot=outcome_snapshot,
    )
    candidate = {
        "interaction_id": str(request_interaction_id),
        "intent": IntakeIntent.ASK_BEFORE_INTAKE.value,
        "modality": CaptureModality.TEXT.value,
        "observed_at": (start + timedelta(hours=10)).isoformat(),
        "timezone": "UTC",
        "source": "decision-scan-test",
        "nutrition_observation_id": None,
        "nutrition_review_id": None,
        "items": [],
        "warnings": [],
        "schema_version": "structured-intake-snapshot-v1",
        "recorded_at": None,
        "source_text": None,
        "media_path": None,
        "resolved_items": [],
        "is_confirmed_intake": False,
        "raw_capture_available": False,
    }
    request = IntakeDecisionRequest(
        request_id=request_id,
        operation_fingerprint="b" * 64,
        interaction_id=request_interaction_id,
        scope=DecisionScope.DAILY_NUTRITION,
        requested_at=end + timedelta(days=4),
        source="decision-scan-test",
        context_snapshot={"candidate": candidate},
    )
    outcome_event = WellnessEvent(
        event_type="nutrition.intake-outcome.v1",
        schema_version=1,
        observed_at=outcome.confirmed_at,
        recorded_at=outcome.confirmed_at,
        timezone="UTC",
        source_provider="nutrition-intake-outcome",
        source_device="decision-scan-test",
        source_record_id=str(outcome_id),
        capture_method="manual",
        quality_flags={},
        confidence=1,
        coverage=1,
        sensitivity="wellness",
        consent_scope="personal",
        expires_at=None,
        payload=outcome_to_payload(outcome),
        derived_from=None,
    )
    request_event = WellnessEvent(
        event_type="nutrition.decision-request.v1",
        schema_version=1,
        observed_at=request.requested_at,
        recorded_at=request.requested_at,
        timezone="UTC",
        source_provider="nutrition-decision-request",
        source_device="decision-scan-test",
        source_record_id=str(request_id),
        capture_method="manual",
        quality_flags={},
        confidence=1,
        coverage=1,
        sensitivity="wellness",
        consent_scope="personal",
        expires_at=None,
        payload=decision_request_to_payload(request),
        derived_from=None,
    )
    session.add_all((outcome_event, request_event))
    session.flush()

    result = search_intake_history(
        session,
        start=start,
        end=end,
        limit=10,
        max_scan_records=10,
    )

    assert result["count"] == 2
    assert {
        record["interaction_id"] for record in result["records"]
    } == {
        str(outcome_interaction_id),
        str(request_interaction_id),
    }
    assert result["truncated"] is False

    access_now = end + timedelta(days=5)
    layer = ContextAccessLayer(
        ContextProviderRegistry((NutritionContextProvider(),)),
        clock=lambda: access_now,
    )
    turn = layer.start_turn(
        DecisionRequest(
            question="What nutrition records occurred in this window?",
            requested_at=access_now,
            timezone="UTC",
            caller=DecisionCaller(
                principal_id="owner",
                authenticated=True,
                execution_scope=ExecutionScope.LOCAL,
            ),
        ),
        policy=ContextAccessPolicy(
            owner_principal_id="owner",
            grants=(DomainAccessGrant(domain="nutrition"),),
        ),
    )

    context = await turn.query(
        session,
        ContextQuery(
            provider_id="nutrition",
            capability="nutrition.intake-history",
            start=start,
            end=end,
            limit=10,
        ),
    )

    assert context.status is not ContextStatus.DENIED
    assert {ref.record_id for ref in context.source_refs} == {
        str(outcome_event.id),
        str(request_event.id),
    }
