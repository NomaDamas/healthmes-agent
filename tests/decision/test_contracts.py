"""Contract tests for the HealthMes-owned decision layer."""

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from healthmes.activity.contracts import ActivityContextResolveRequest
from healthmes.decision import (
    CompatibilityPreset,
    ContextCoverage,
    ContextFreshness,
    ContextQuery,
    ContextResult,
    ContextStatus,
    CoverageStatus,
    DecisionCaller,
    DecisionContextHints,
    DecisionDraft,
    DecisionPersistenceIntent,
    DecisionRequest,
    DecisionResult,
    DecisionStatus,
    ExecutionScope,
    FreshnessStatus,
    PersistenceStatus,
    RuntimeMetadata,
    SourceRef,
    ToolCallRecord,
    ToolCallStatus,
    decision_request_from_activity_context,
    source_ref_id,
)

T0 = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)


def _caller() -> DecisionCaller:
    return DecisionCaller(
        principal_id="local-owner",
        authenticated=True,
        execution_scope=ExecutionScope.LOCAL,
        session_id="session-1",
        channel="test",
    )


def _query() -> ContextQuery:
    return ContextQuery(
        provider_id="activity",
        capability="activity.summary",
        start=T0,
        end=T1,
        timezone="Asia/Seoul",
        fields=["active_seconds", "coverage"],
        parameters={"include_baseline": True},
        purpose="Understand recent focus conditions",
    )


def _source_ref(
    *,
    record_id: str = "event-1",
    domain: str = "activity",
    resource_type: str = "hour_summary",
    source_provider: str = "activitywatch",
) -> SourceRef:
    return SourceRef(
        domain=domain,
        resource_type=resource_type,
        record_id=record_id,
        source_provider=source_provider,
        observed_start=T0,
        observed_end=T1,
        schema_version=1,
        derived_by="activity.hour-summary.v1",
        freshness=FreshnessStatus.CURRENT,
        coverage=0.8,
        sensitivity="activity-aggregate",
    )


def _context_result(
    query: ContextQuery,
    source_ref: SourceRef,
) -> ContextResult:
    return ContextResult(
        query_id=query.query_id,
        provider_id=query.provider_id,
        capability=query.capability,
        status=ContextStatus.OK,
        payload={"active_seconds": 1_800, "coverage": 0.8},
        source_refs=[source_ref],
        freshness=ContextFreshness(
            status=FreshnessStatus.CURRENT,
            as_of=T1,
            age_seconds=0,
        ),
        coverage=ContextCoverage(
            status=CoverageStatus.PARTIAL,
            ratio=0.8,
        ),
        limitations=["window_titles_omitted"],
    )


def test_natural_language_request_roundtrips_without_required_preset():
    request = DecisionRequest(
        question="Why was my focus fragmented today?",
        requested_at=T0,
        timezone="Asia/Seoul",
        persistence_requested=True,
        caller=_caller(),
        hints=DecisionContextHints(
            local_date=T0.date(),
            start=T0,
            end=T1,
            related_record_ids={
                "nutrition_request": str(uuid.uuid4())
            },
        ),
    )

    restored = DecisionRequest.model_validate_json(request.model_dump_json())

    assert restored == request
    assert restored.requested_at == T0
    assert restored.persistence_requested is True
    assert restored.compatibility_preset is None
    assert "compatibility_preset" not in (
        DecisionRequest.model_json_schema().get("required") or []
    )


@pytest.mark.parametrize("value", ("true", "yes", "on", 1))
def test_decision_request_rejects_non_boolean_persistence_consent(value):
    with pytest.raises(ValidationError):
        DecisionRequest(
            question="Track this decision.",
            requested_at=T0,
            timezone="UTC",
            persistence_requested=value,
            caller=_caller(),
        )


def test_compatibility_preset_is_optional_metadata():
    request = DecisionRequest.from_compatibility_preset(
        CompatibilityPreset.CAFFEINE_FOR_FOCUS,
        caller=_caller(),
        timezone="UTC",
        requested_at=T0,
    )

    assert request.compatibility_preset is CompatibilityPreset.CAFFEINE_FOR_FOCUS
    assert "caffeine" in request.question.casefold()


def test_legacy_activity_request_preserves_context_without_becoming_required():
    nutrition_request_id = uuid.uuid4()
    legacy = ActivityContextResolveRequest(
        question_kind="caffeine_for_focus",
        date="2026-08-10",
        start=T0,
        end=T1,
        lookback_days=14,
        nutrition_request_id=nutrition_request_id,
        timezone="Asia/Seoul",
    )

    request = decision_request_from_activity_context(
        legacy,
        caller=_caller(),
        default_timezone="UTC",
        requested_at=T0,
    )

    assert request.compatibility_preset is CompatibilityPreset.CAFFEINE_FOR_FOCUS
    assert request.timezone == "Asia/Seoul"
    assert request.hints.local_date == T0.date()
    assert request.hints.start == T0
    assert request.hints.end == T1
    assert request.hints.lookback_days == 14
    assert request.hints.related_record_ids == {
        "nutrition_request": str(nutrition_request_id)
    }


@pytest.mark.parametrize(
    ("model", "payload"),
    (
        (
            DecisionRequest,
            {
                "question": "test",
                "requested_at": "2026-08-10T00:00:00",
                "timezone": "UTC",
                "caller": {
                    "principal_id": "owner",
                    "authenticated": True,
                    "execution_scope": "local",
                },
            },
        ),
        (
            ContextQuery,
            {
                "provider_id": "activity",
                "capability": "activity.summary",
                "start": "2026-08-10T00:00:00",
                "end": "2026-08-10T01:00:00Z",
            },
        ),
        (
            SourceRef,
            {
                "domain": "activity",
                "resource_type": "hour_summary",
                "record_id": "event-1",
                "source_provider": "activitywatch",
                "observed_start": "2026-08-10T00:00:00",
            },
        ),
    ),
)
def test_contracts_reject_naive_datetimes(model, payload):
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_query_rejects_partial_or_reversed_ranges():
    with pytest.raises(ValidationError, match="provided together"):
        ContextQuery(
            provider_id="activity",
            capability="activity.summary",
            start=T0,
        )
    with pytest.raises(ValidationError, match="start must be before end"):
        ContextQuery(
            provider_id="activity",
            capability="activity.summary",
            start=T1,
            end=T0,
        )


def test_query_accepts_one_25_hour_local_day():
    query = ContextQuery(
        provider_id="activity",
        capability="activity.summary",
        start=datetime(2025, 11, 2, 4, tzinfo=UTC),
        end=datetime(2025, 11, 3, 5, tzinfo=UTC),
        timezone="America/New_York",
    )

    assert query.end - query.start == timedelta(hours=25)


def test_request_and_query_share_the_same_90_local_day_dst_boundary():
    timezone = "America/New_York"
    zone = ZoneInfo(timezone)
    start = datetime(2026, 8, 9, 8, tzinfo=zone)
    end = datetime(2026, 11, 7, 8, tzinfo=zone)

    request = DecisionRequest(
        question="Use the full retained context window.",
        requested_at=T0,
        timezone=timezone,
        caller=_caller(),
        hints=DecisionContextHints(start=start, end=end),
    )
    query = ContextQuery(
        provider_id="nutrition",
        capability="nutrition.decision-context",
        start=start,
        end=end,
        timezone=timezone,
    )

    assert request.hints.end - request.hints.start == timedelta(
        days=90,
        hours=1,
    )
    assert query.end - query.start == timedelta(days=90, hours=1)

    overlong_end = end + timedelta(microseconds=1)
    with pytest.raises(ValidationError, match="90 local days"):
        DecisionRequest(
            question="This range is one local instant too long.",
            requested_at=T0,
            timezone=timezone,
            caller=_caller(),
            hints=DecisionContextHints(
                start=start,
                end=overlong_end,
            ),
        )
    with pytest.raises(ValidationError, match="90 days"):
        ContextQuery(
            provider_id="nutrition",
            capability="nutrition.decision-context",
            start=start,
            end=overlong_end,
            timezone=timezone,
        )


def test_source_ref_id_is_stable_and_bound_to_provider_identity():
    source_ref = _source_ref()

    assert source_ref.reference_id == source_ref_id(
        domain="activity",
        resource_type="hour_summary",
        source_provider="activitywatch",
        record_id="event-1",
    )
    assert source_ref.reference_id.startswith("sr_")
    assert len(source_ref.reference_id) == 35
    assert (
        _source_ref(source_provider="android-usage").reference_id
        != source_ref.reference_id
    )
    assert (
        SourceRef.model_validate_json(source_ref.model_dump_json())
        == source_ref
    )
    assert source_ref_id(
        domain=" Activity ",
        resource_type=" HOUR_SUMMARY ",
        source_provider=" ActivityWatch ",
        record_id=" event-1 ",
    ) == source_ref.reference_id


def test_source_ref_rejects_reversed_observation_range():
    with pytest.raises(ValidationError, match="observed_end"):
        SourceRef(
            domain="activity",
            resource_type="hour_summary",
            record_id="event-1",
            source_provider="activitywatch",
            observed_start=T1,
            observed_end=T0,
        )


def test_context_result_completeness_times_are_optional_and_bounded():
    query = _query()
    legacy = ContextResult(
        query_id=query.query_id,
        provider_id=query.provider_id,
        capability=query.capability,
        status=ContextStatus.PARTIAL,
    )
    complete = ContextResult(
        query_id=query.query_id,
        provider_id=query.provider_id,
        capability=query.capability,
        status=ContextStatus.OK,
        observed_start=T0,
        observed_end=T1,
        collected_at=T1,
    )

    assert legacy.observed_start is None
    assert legacy.collected_at is None
    assert complete.observed_start == T0
    assert complete.observed_end == T1
    assert complete.collected_at == T1

    with pytest.raises(ValidationError, match="provided together"):
        ContextResult(
            query_id=query.query_id,
            provider_id=query.provider_id,
            capability=query.capability,
            status=ContextStatus.OK,
            observed_start=T0,
        )
    with pytest.raises(ValidationError, match="must not be before"):
        ContextResult(
            query_id=query.query_id,
            provider_id=query.provider_id,
            capability=query.capability,
            status=ContextStatus.OK,
            observed_start=T1,
            observed_end=T0,
        )


def test_source_ref_rejects_forged_reference_id():
    payload = _source_ref().model_dump(mode="json")
    payload["reference_id"] = "sr_" + ("0" * 32)

    with pytest.raises(ValidationError, match="does not match"):
        SourceRef.model_validate(payload)

    forged_instance = _source_ref().model_copy(
        update={"reference_id": "sr_" + ("f" * 32)}
    )
    with pytest.raises(ValidationError, match="does not match"):
        SourceRef.model_validate(forged_instance)


def test_coverage_distinguishes_zero_from_unknown():
    partial_zero = ContextCoverage(
        status=CoverageStatus.PARTIAL,
        ratio=0,
    )
    unknown = ContextCoverage(status=CoverageStatus.UNKNOWN)

    assert partial_zero.ratio == 0
    assert unknown.ratio is None
    with pytest.raises(ValidationError, match="must not invent"):
        ContextCoverage(
            status=CoverageStatus.UNKNOWN,
            ratio=0,
        )


def test_context_result_rejects_duplicate_or_denied_references():
    query = _query()
    source_ref = _source_ref()

    with pytest.raises(ValidationError, match="unique"):
        ContextResult(
            query_id=query.query_id,
            provider_id=query.provider_id,
            capability=query.capability,
            status=ContextStatus.OK,
            source_refs=[source_ref, source_ref],
        )
    with pytest.raises(ValidationError, match="denied"):
        ContextResult(
            query_id=query.query_id,
            provider_id=query.provider_id,
            capability=query.capability,
            status=ContextStatus.DENIED,
            source_refs=[source_ref],
        )


def test_tool_trace_must_match_query_and_result():
    query = _query()
    source_ref = _source_ref()
    result = _context_result(query, source_ref)
    record = ToolCallRecord(
        query=query,
        status=ToolCallStatus.COMPLETED,
        started_at=T0,
        finished_at=T1,
        result=result,
    )

    assert record.result == result
    with pytest.raises(ValidationError, match="must match"):
        ToolCallRecord(
            query=query,
            status=ToolCallStatus.COMPLETED,
            started_at=T0,
            finished_at=T1,
            result=result.model_copy(
                update={"query_id": uuid.uuid4()}
            ),
        )
    with pytest.raises(ValidationError, match="effective query"):
        ToolCallRecord(
            query=query,
            effective_query=query.model_copy(
                update={"query_id": uuid.uuid4()}
            ),
            status=ToolCallStatus.COMPLETED,
            started_at=T0,
            finished_at=T1,
            result=result,
        )


def test_action_draft_requires_valid_source_reference():
    reference_id = _source_ref().reference_id
    draft = DecisionDraft(
        status=DecisionStatus.COMPLETED,
        answer="Take a short break before deciding.",
        proposed_action=True,
        persistence_intent=DecisionPersistenceIntent.ACTION,
        used_source_ref_ids=[reference_id],
    )

    assert draft.used_source_ref_ids == [reference_id]
    with pytest.raises(ValidationError, match="source reference"):
        DecisionDraft(
            status=DecisionStatus.COMPLETED,
            answer="Take a break.",
            proposed_action=True,
            persistence_intent=DecisionPersistenceIntent.ACTION,
        )
    with pytest.raises(ValidationError, match="invalid ID"):
        DecisionDraft(
            status=DecisionStatus.COMPLETED,
            answer="Take a break.",
            used_source_ref_ids=["activity:event-1"],
        )


def test_decision_draft_preserves_uncertainty_and_follow_up():
    draft = DecisionDraft(
        status=DecisionStatus.COMPLETED,
        answer="The current context is mixed.",
        confidence=0.6,
        uncertainty="Sleep coverage is partial.",
        follow_up_question="Should I inspect the previous week too?",
    )

    assert draft.confidence == 0.6
    assert draft.uncertainty == "Sleep coverage is partial."
    assert draft.follow_up_question is not None


def test_persistence_intent_contract_rejects_inconsistent_drafts():
    reference_id = _source_ref().reference_id

    with pytest.raises(ValidationError, match="action and risk persistence"):
        DecisionDraft(
            status=DecisionStatus.COMPLETED,
            answer="No action was proposed.",
            persistence_intent=DecisionPersistenceIntent.ACTION,
            used_source_ref_ids=[reference_id],
        )
    with pytest.raises(ValidationError, match="action and risk persistence"):
        DecisionDraft(
            status=DecisionStatus.COMPLETED,
            answer="This is an important warning.",
            persistence_intent=DecisionPersistenceIntent.RISK,
            used_source_ref_ids=[reference_id],
        )
    with pytest.raises(ValidationError, match="require at least one"):
        DecisionDraft(
            status=DecisionStatus.COMPLETED,
            answer="A mutation was reported.",
            persistence_intent=DecisionPersistenceIntent.MUTATION,
        )
    with pytest.raises(ValidationError, match="only completed"):
        DecisionDraft(
            status=DecisionStatus.NEEDS_CLARIFICATION,
            persistence_intent=(
                DecisionPersistenceIntent.EXPLICIT_TRACKING
            ),
            clarification_question="What should I retain?",
        )


def test_clarification_requires_a_question_and_cannot_propose_action():
    with pytest.raises(ValidationError, match="clarification_question"):
        DecisionDraft(status=DecisionStatus.NEEDS_CLARIFICATION)
    with pytest.raises(ValidationError, match="cannot propose"):
        DecisionDraft(
            status=DecisionStatus.NEEDS_CLARIFICATION,
            clarification_question="What size is the coffee?",
            proposed_action=True,
            used_source_ref_ids=[_source_ref().reference_id],
        )


def test_completed_action_result_requires_persistence():
    query = _query()
    source_ref = _source_ref()
    trace = ToolCallRecord(
        query=query,
        status=ToolCallStatus.COMPLETED,
        started_at=T0,
        finished_at=T1,
        result=_context_result(query, source_ref),
    )
    decision_id = uuid.uuid4()
    result = DecisionResult(
        request_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        status=DecisionStatus.COMPLETED,
        answer="Rest first, then reassess caffeine.",
        proposed_action=True,
        source_refs=[source_ref],
        persistence_status=PersistenceStatus.PERSISTED,
        decision_record_id=decision_id,
        runtime=RuntimeMetadata(runtime="test-runtime", model="scripted-v1"),
        tool_trace=[trace],
    )

    assert result.decision_record_id == decision_id
    with pytest.raises(ValidationError, match="after persistence"):
        DecisionResult(
            request_id=uuid.uuid4(),
            turn_id=uuid.uuid4(),
            status=DecisionStatus.COMPLETED,
            answer="Rest first.",
            proposed_action=True,
            source_refs=[source_ref],
            runtime=RuntimeMetadata(runtime="test-runtime"),
        )


@pytest.mark.parametrize(
    "domain",
    ("activity", "nutrition", "wearable", "calendar"),
)
def test_source_ref_contract_supports_each_initial_domain(domain):
    source_ref = _source_ref(
        domain=domain,
        resource_type=f"{domain}_summary",
        source_provider=f"{domain}-provider",
    )

    assert source_ref.domain == domain
    assert source_ref.reference_id.startswith("sr_")
