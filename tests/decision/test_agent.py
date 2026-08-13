from __future__ import annotations

import asyncio
import re
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import sessionmaker

import healthmes.decision.agent as decision_agent_module
from healthmes.decision import (
    ContextAccessLayer,
    ContextAccessPolicy,
    ContextCapability,
    ContextCoverage,
    ContextFreshness,
    ContextParameterFormat,
    ContextParameterSpec,
    ContextParameterType,
    ContextProviderMetadata,
    ContextProviderRegistry,
    ContextResult,
    ContextStatus,
    CoverageStatus,
    DecisionBudget,
    DecisionCaller,
    DecisionContextHints,
    DecisionDraft,
    DecisionRequest,
    DecisionRuntimeContractError,
    DecisionRuntimeTurn,
    DecisionRuntimeUnavailableError,
    DecisionStatus,
    DomainAccessGrant,
    ExecutionScope,
    FreshnessStatus,
    HealthMesDecisionAgent,
    NutritionContextProvider,
    PrivacyLevel,
    ProvenanceSupport,
    RuntimeDecisionRequest,
    RuntimeMetadata,
    RuntimeStepOutput,
    SourceRef,
    ToolCallStatus,
    source_ref_id,
)
from healthmes.store import Base, WellnessEvent, create_db_engine

NOW = datetime(2026, 8, 11, 6, tzinfo=UTC)


class StubProvider:
    def __init__(
        self,
        *,
        domain: str,
        coverage: ContextCoverage | None = None,
        privacy_levels: tuple[PrivacyLevel, ...] = (
            PrivacyLevel.AGGREGATE,
        ),
        supports_raw: bool = False,
        result_factory: Callable[[Any, datetime], ContextResult] | None = None,
    ) -> None:
        self.calls = []
        self._coverage = coverage or ContextCoverage(
            status=CoverageStatus.COMPLETE,
            ratio=1,
        )
        self._result_factory = result_factory
        self.metadata = ContextProviderMetadata(
            provider_id=domain,
            domain=domain,
            description=f"{domain} test provider.",
            capabilities=(
                ContextCapability(
                    capability=f"{domain}.summary",
                    description=f"{domain} summary.",
                    granularities=("summary",),
                    query_fields=(
                        "start",
                        "end",
                        "timezone",
                        "fields",
                        "limit",
                    ),
                    output_fields=("status", "value"),
                    max_lookback_days=30,
                    privacy_levels=privacy_levels,
                    sensitivity=domain,
                    supports_raw=supports_raw,
                    provenance=ProvenanceSupport.PARTIAL,
                    freshness_expectation="Test context.",
                ),
            ),
        )

    async def query(self, session, query, *, now):
        self.calls.append(query)
        if self._result_factory is not None:
            return self._result_factory(query, now)
        return ContextResult(
            query_id=query.query_id,
            provider_id=query.provider_id,
            capability=query.capability,
            status=ContextStatus.OK,
            payload={"status": "ok", "value": 1},
            freshness=ContextFreshness(
                status=FreshnessStatus.CURRENT,
                as_of=now,
            ),
            coverage=self._coverage,
        )


class CapturingRuntime:
    metadata = RuntimeMetadata(runtime="scripted", model="capture-v1")

    def __init__(self) -> None:
        self.turns: list[DecisionRuntimeTurn] = []

    async def next_step(self, turn):
        self.turns.append(turn)
        return RuntimeStepOutput(
            draft=DecisionDraft(
                status=DecisionStatus.NEEDS_CLARIFICATION,
                clarification_question="What should I inspect?",
            ),
            metadata=self.metadata,
        )


class AdaptiveRuntime:
    metadata = RuntimeMetadata(runtime="scripted", model="adaptive-v1")

    def __init__(self) -> None:
        self.capabilities: list[str] = []

    async def next_step(self, turn):
        if not turn.history:
            self.capabilities.append("activity.summary")
            return RuntimeStepOutput(
                tool_calls=({"capability": "activity.summary"},),
                metadata=self.metadata,
            )

        activity = turn.history[0].results[0]
        if (
            activity.coverage.status is not CoverageStatus.COMPLETE
            and len(turn.history) == 1
        ):
            self.capabilities.append("wearable.summary")
            return RuntimeStepOutput(
                tool_calls=({"capability": "wearable.summary"},),
                metadata=self.metadata,
            )

        return RuntimeStepOutput(
            draft=DecisionDraft(
                status=DecisionStatus.COMPLETED,
                answer="I checked the context selected from the results.",
                uncertainty="Coverage determines whether more context is used.",
            ),
            metadata=self.metadata,
        )


@pytest.fixture
def session_factory():
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


def _request(
    *,
    budget: DecisionBudget | None = None,
    privacy: PrivacyLevel = PrivacyLevel.AGGREGATE,
    question: str = "Why is my focus different today?",
) -> DecisionRequest:
    return DecisionRequest(
        question=question,
        requested_at=NOW,
        timezone="UTC",
        caller=DecisionCaller(
            principal_id="owner",
            authenticated=True,
            execution_scope=ExecutionScope.LOCAL,
        ),
        requested_privacy_level=privacy,
        budget=budget or DecisionBudget(),
    )


def _policy(
    *domains: str,
    max_privacy: PrivacyLevel = PrivacyLevel.AGGREGATE,
) -> ContextAccessPolicy:
    return ContextAccessPolicy(
        owner_principal_id="owner",
        grants=tuple(
            DomainAccessGrant(
                domain=domain,
                max_privacy_level=max_privacy,
            )
            for domain in domains
        ),
    )


class MutablePolicyResolver:
    def __init__(self, *domains: str) -> None:
        self._lock = threading.Lock()
        self._state = {
            domain: {"enabled": True, "revision": 1}
            for domain in domains
        }

    def __call__(
        self,
        _request: DecisionRequest,
    ) -> ContextAccessPolicy:
        with self._lock:
            state = {
                domain: values.copy()
                for domain, values in self._state.items()
            }
        return ContextAccessPolicy(
            owner_principal_id="owner",
            grants=tuple(
                DomainAccessGrant(
                    domain=domain,
                    enabled=bool(values["enabled"]),
                    revision=int(values["revision"]),
                )
                for domain, values in state.items()
            ),
        )

    def set_enabled(self, domain: str, enabled: bool) -> None:
        with self._lock:
            values = self._state[domain]
            if values["enabled"] != enabled:
                values["enabled"] = enabled
                values["revision"] = int(values["revision"]) + 1


def _agent(
    session_factory,
    *,
    providers,
    runtime,
    policy: ContextAccessPolicy,
    timeout_seconds: float = 1,
) -> HealthMesDecisionAgent:
    registry = ContextProviderRegistry(providers)
    return HealthMesDecisionAgent(
        access_layer=ContextAccessLayer(
            registry,
            clock=lambda: NOW,
        ),
        runtime=runtime,
        session_factory=session_factory,
        policy_resolver=lambda request: policy,
        timeout_seconds=timeout_seconds,
        clock=lambda: NOW,
    )


def _assert_gateway_attested_source_ref(
    actual: SourceRef,
    expected: SourceRef,
) -> None:
    assert actual.model_copy(
        update={"content_digest": None},
        deep=True,
    ) == expected
    assert actual.content_digest is not None
    assert re.fullmatch(r"[0-9a-f]{64}", actual.content_digest)


async def test_agent_injects_policy_and_consent_filtered_catalog(
    session_factory,
):
    runtime = CapturingRuntime()
    agent = _agent(
        session_factory,
        providers=(
            StubProvider(domain="activity"),
            StubProvider(domain="wearable"),
        ),
        runtime=runtime,
        policy=_policy("activity"),
    )

    result = await agent.ask(_request())

    assert result.draft.status is DecisionStatus.NEEDS_CLARIFICATION
    assert result.steps_used == 1
    assert len(runtime.turns) == 1
    turn = runtime.turns[0]
    assert "Return exactly one action" in turn.system_policy
    assert "fixed question-kind routing table" in turn.system_policy
    assert "missing, unknown, unavailable, stale, and partial data" in (
        turn.system_policy
    )
    assert [tool.capability for tool in turn.tools] == [
        "activity.summary"
    ]
    assert turn.step_number == 1
    assert turn.remaining_steps == _request().budget.max_steps
    assert turn.request_id != result.request_id
    assert turn.turn_id != result.turn_id
    assert turn.request_id.version == 4
    assert turn.turn_id.version == 4
    assert turn.resource_budget.max_tool_calls == (
        _request().budget.max_tool_calls
    )
    assert turn.resource_budget.remaining_tool_calls == (
        _request().budget.max_tool_calls
    )
    assert turn.resource_budget.max_source_refs == (
        _request().budget.max_source_refs
    )
    assert turn.resource_budget.remaining_context_bytes == (
        _request().budget.max_context_bytes
    )
    assert 1 <= turn.deadline_ms <= 1_000


@pytest.mark.parametrize(
    ("runtime_error", "status", "limitation"),
    [
        (
            DecisionRuntimeUnavailableError(
                "hermes_single_iteration_not_advertised"
            ),
            DecisionStatus.BLOCKED,
            "hermes_single_iteration_not_advertised",
        ),
        (
            DecisionRuntimeContractError(
                "hermes_response_contract_invalid"
            ),
            DecisionStatus.FAILED,
            "runtime_contract_violation",
        ),
    ],
)
async def test_agent_maps_runtime_boundary_errors_without_fallback(
    session_factory,
    runtime_error,
    status,
    limitation,
):
    class FailingRuntime:
        metadata = RuntimeMetadata(
            runtime="hermes",
            model="test-model",
        )

        async def next_step(self, turn):
            raise runtime_error

    agent = _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=FailingRuntime(),
        policy=_policy("activity"),
    )

    result = await agent.ask(_request())

    assert result.draft.status is status
    assert result.draft.limitations == [limitation]
    assert result.steps_used == 1
    assert result.tool_trace == ()


async def test_consent_revoked_between_tool_calls_blocks_next_provider(
    session_factory,
):
    resolver = MutablePolicyResolver("activity", "wearable")

    class RevokingActivityProvider(StubProvider):
        async def query(self, session, query, *, now):
            result = await super().query(session, query, now=now)
            resolver.set_enabled("wearable", False)
            return result

    class TwoToolRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            if not turn.history:
                return RuntimeStepOutput(
                    tool_calls=(
                        {"capability": "activity.summary"},
                        {"capability": "wearable.summary"},
                    ),
                    metadata=self.metadata,
                )
            raise AssertionError(
                "consent revocation must terminate before another model step"
            )

    activity = RevokingActivityProvider(domain="activity")
    wearable = StubProvider(domain="wearable")
    registry = ContextProviderRegistry((activity, wearable))
    agent = HealthMesDecisionAgent(
        access_layer=ContextAccessLayer(
            registry,
            clock=lambda: NOW,
        ),
        runtime=TwoToolRuntime(),
        session_factory=session_factory,
        policy_resolver=resolver,
        timeout_seconds=1,
        clock=lambda: NOW,
    )

    result = await agent.ask(_request())
    agent.close()

    assert result.draft.status is DecisionStatus.BLOCKED
    assert result.draft.limitations == ["domain_consent_denied"]
    assert len(activity.calls) == 1
    assert wearable.calls == []
    assert [record.status for record in result.tool_trace] == [
        ToolCallStatus.COMPLETED,
        ToolCallStatus.DENIED,
    ]


@pytest.mark.parametrize("reenable_before_release", (False, True))
async def test_consent_revision_change_during_provider_discards_result(
    session_factory,
    reenable_before_release,
):
    resolver = MutablePolicyResolver("activity")
    started = threading.Event()
    release = threading.Event()

    class BlockingProvider(StubProvider):
        async def query(self, session, query, *, now):
            started.set()
            assert release.wait(timeout=5)
            return await super().query(session, query, now=now)

    class ToolRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            if not turn.history:
                return RuntimeStepOutput(
                    tool_calls=({"capability": "activity.summary"},),
                    metadata=self.metadata,
                )
            raise AssertionError(
                "a changed consent revision must terminate the turn"
            )

    provider = BlockingProvider(domain="activity")
    registry = ContextProviderRegistry((provider,))
    agent = HealthMesDecisionAgent(
        access_layer=ContextAccessLayer(
            registry,
            clock=lambda: NOW,
        ),
        runtime=ToolRuntime(),
        session_factory=session_factory,
        policy_resolver=resolver,
        timeout_seconds=1,
        clock=lambda: NOW,
    )
    task = asyncio.create_task(agent.ask(_request()))
    assert await asyncio.to_thread(started.wait, 1)
    resolver.set_enabled("activity", False)
    if reenable_before_release:
        resolver.set_enabled("activity", True)
    release.set()
    result = await task
    agent.close()

    assert result.draft.status is DecisionStatus.BLOCKED
    assert result.draft.limitations == ["domain_consent_changed"]
    assert len(provider.calls) == 1
    assert len(result.tool_trace) == 1
    record = result.tool_trace[0]
    assert record.status is ToolCallStatus.DENIED
    assert record.result is not None
    assert record.result.status is ContextStatus.DENIED
    assert record.result.source_refs == []
    assert record.result.limitations == ["domain_consent_changed"]


async def test_runtime_request_omits_caller_and_record_identifiers(
    session_factory,
):
    runtime = CapturingRuntime()
    agent = _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=runtime,
        policy=_policy("activity").model_copy(
            update={"owner_principal_id": "private-principal-123"}
        ),
    )
    request = DecisionRequest(
        question="Should I change my plan?",
        requested_at=NOW,
        timezone="Asia/Seoul",
        caller=DecisionCaller(
            principal_id="private-principal-123",
            authenticated=True,
            execution_scope=ExecutionScope.LOCAL,
            session_id="private-session-456",
            channel="private-channel",
        ),
        requested_privacy_level=PrivacyLevel.IDENTITY,
        hints=DecisionContextHints(
            local_date=NOW.date(),
            start=NOW,
            end=NOW.replace(hour=7),
            lookback_days=7,
            related_record_ids={
                "activity_capture": "private-record-789",
            },
        ),
    )

    result = await agent.ask(request)

    assert result.draft.status is DecisionStatus.NEEDS_CLARIFICATION
    turn_request = runtime.turns[0].request
    assert isinstance(turn_request, RuntimeDecisionRequest)
    assert turn_request.question == request.question
    assert turn_request.requested_at == request.requested_at
    assert turn_request.timezone == request.timezone
    assert turn_request.requested_privacy_level is PrivacyLevel.IDENTITY
    assert turn_request.hints.has_related_records is True
    assert turn_request.hints.related_domains == ("activity",)
    serialized = turn_request.model_dump_json()
    assert "private-principal-123" not in serialized
    assert "private-session-456" not in serialized
    assert "private-channel" not in serialized
    assert "private-record-789" not in serialized
    assert str(request.request_id) not in serialized
    assert str(request.turn_id) not in serialized
    assert not hasattr(turn_request, "caller")
    runtime_turn = runtime.turns[0]
    assert runtime_turn.request_id != request.request_id
    assert runtime_turn.turn_id != request.turn_id
    serialized_turn = runtime_turn.model_dump_json()
    assert str(request.request_id) not in serialized_turn
    assert str(request.turn_id) not in serialized_turn


async def test_related_record_is_exposed_only_as_turn_scoped_alias(
    session_factory,
):
    actual_request_id = "AbCdEfAb-cDeF-4aBc-8DeF-aBcDeFaBcDeF"
    canonical_request_id = str(uuid.UUID(actual_request_id))
    alternate_request_id = uuid.UUID(actual_request_id).hex.upper()
    uppercase_urn = uuid.UUID(actual_request_id).urn.upper()

    class RelatedRecordProvider:
        def __init__(self) -> None:
            self.calls = []
            self.metadata = ContextProviderMetadata(
                provider_id="nutrition",
                domain="nutrition",
                description="Selected nutrition decision context.",
                capabilities=(
                    ContextCapability(
                        capability="nutrition.decision-context",
                        description="Selected nutrition context.",
                        granularities=("summary",),
                        query_fields=("timezone",),
                        output_fields=("status", "request", "message"),
                        nested_output_fields=("request_id",),
                        identity_fields=("request_id",),
                        parameters=("request_id",),
                        parameter_specs=(
                            ContextParameterSpec(
                                name="request_id",
                                value_type=ContextParameterType.STRING,
                                required=True,
                                min_length=36,
                                max_length=36,
                                format=ContextParameterFormat.UUID,
                                accepts_related_record_ref=True,
                            ),
                        ),
                        max_lookback_days=30,
                        privacy_levels=(
                            PrivacyLevel.AGGREGATE,
                            PrivacyLevel.IDENTITY,
                        ),
                        sensitivity="nutrition",
                        provenance=ProvenanceSupport.PARTIAL,
                        freshness_expectation="Selected stored context.",
                    ),
                ),
            )

        async def query(self, session, query, *, now):
            del session
            self.calls.append(query)
            return ContextResult(
                query_id=query.query_id,
                provider_id=query.provider_id,
                capability=query.capability,
                status=ContextStatus.OK,
                payload={
                    "status": "ok",
                    "request": {"request_id": actual_request_id},
                    "message": (
                        f"selected request {actual_request_id} is ready"
                    ),
                },
                freshness=ContextFreshness(
                    status=FreshnessStatus.CURRENT,
                    as_of=now,
                ),
                coverage=ContextCoverage(
                    status=CoverageStatus.COMPLETE,
                    ratio=1,
                ),
            )

    class AliasRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        def __init__(self) -> None:
            self.serialized_turns: list[str] = []
            self.reference: str | None = None

        async def next_step(self, turn):
            serialized = turn.model_dump_json()
            self.serialized_turns.append(serialized)
            assert actual_request_id not in serialized
            assert alternate_request_id not in serialized
            assert uppercase_urn not in serialized
            if not turn.history:
                related = turn.request.hints.related_records
                assert len(related) == 1
                self.reference = related[0].reference
                assert related[0].domain == "nutrition"
                assert turn.request.question == (
                    "Inspect selected records "
                    f"{self.reference}, {self.reference}, "
                    f"and {self.reference}"
                )
                tool = turn.tools[0]
                assert tool.capability == "nutrition.decision-context"
                parameter = tool.parameter_specs[0]
                assert (
                    parameter.format
                    is ContextParameterFormat.RELATED_RECORD_REF
                )
                assert parameter.allowed_values == (self.reference,)
                return RuntimeStepOutput(
                    tool_calls=(
                        {
                            "capability": tool.capability,
                            "privacy_level": "identity",
                            "parameters": {
                                "request_id": self.reference,
                            },
                        },
                    ),
                    metadata=self.metadata,
                )
            assert (
                turn.history[0].tool_calls[0]
                .parameters["request_id"]
                == self.reference
            )
            context = turn.history[0].results[0]
            assert context.payload["request"]["request_id"] == self.reference
            assert self.reference in context.payload["message"]
            assert not hasattr(context, "source_refs")
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer="The selected context was retrieved by alias.",
                ),
                metadata=self.metadata,
            )

    provider = RelatedRecordProvider()
    runtime = AliasRuntime()
    request = _request(
        privacy=PrivacyLevel.IDENTITY,
        question=(
            "Inspect selected records "
            f"{actual_request_id}, {alternate_request_id}, "
            f"and {uppercase_urn}"
        ),
    ).model_copy(
        update={
            "hints": DecisionContextHints(
                related_record_ids={
                    "nutrition_request": actual_request_id,
                    "nutrition_capture": alternate_request_id,
                    "nutrition_candidate": uppercase_urn,
                }
            )
        }
    )

    result = await _agent(
        session_factory,
        providers=(provider,),
        runtime=runtime,
        policy=_policy(
            "nutrition",
            max_privacy=PrivacyLevel.IDENTITY,
        ),
    ).ask(request)

    assert result.draft.status is DecisionStatus.COMPLETED
    assert runtime.reference is not None
    assert runtime.reference.startswith("rr_")
    assert provider.calls[0].parameters == {
        "request_id": canonical_request_id
    }
    assert result.tool_trace[0].query.parameters == {
        "request_id": canonical_request_id
    }


async def test_unbound_related_record_ids_are_redacted_from_question(
    session_factory,
):
    unknown_id = "short-id"
    unauthorized_id = str(uuid.uuid4())

    class RedactionRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            serialized = turn.model_dump_json()
            assert unknown_id not in serialized
            assert unauthorized_id not in serialized
            assert turn.request.hints.has_related_records is True
            assert turn.request.hints.related_records == ()
            assert turn.request.hints.related_domains == ()
            assert turn.request.question.count("rr_") == 2
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.NEEDS_CLARIFICATION,
                    clarification_question=(
                        "Which retained record should I inspect?"
                    ),
                ),
                metadata=self.metadata,
            )

    request = _request(
        question=(
            f"Compare {unknown_id} with {unauthorized_id}"
        )
    ).model_copy(
        update={
            "hints": DecisionContextHints(
                related_record_ids={
                    "unknown_record": unknown_id,
                    "nutrition_request": unauthorized_id,
                }
            )
        }
    )
    result = await _agent(
        session_factory,
        providers=(
            StubProvider(domain="activity"),
            StubProvider(domain="nutrition"),
        ),
        runtime=RedactionRuntime(),
        policy=_policy("activity"),
    ).ask(request)

    assert result.draft.status is DecisionStatus.NEEDS_CLARIFICATION


async def test_overlapping_related_ids_are_replaced_atomically(
    session_factory,
):
    short_numeric = "1234"
    containing_uuid = "12345678-9abc-def0-1234-56789abcdef0"
    short_text = "abc"
    containing_text = "abc-def"

    class OverlapRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            assert re.fullmatch(
                (
                    r"Compare rr_[0-9a-f]{16} with rr_[0-9a-f]{16} "
                    r"and rr_[0-9a-f]{16} with rr_[0-9a-f]{16}"
                ),
                turn.request.question,
            )
            references = re.findall(
                r"rr_[0-9a-f]{16}",
                turn.request.question,
            )
            assert len(references) == len(set(references)) == 4
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.NEEDS_CLARIFICATION,
                    clarification_question=(
                        "Which retained record should I inspect?"
                    ),
                ),
                metadata=self.metadata,
            )

    request = _request(
        question=(
            f"Compare {containing_uuid} with {short_numeric} "
            f"and {containing_text} with {short_text}"
        )
    ).model_copy(
        update={
            "hints": DecisionContextHints(
                related_record_ids={
                    "first_unknown": short_numeric,
                    "second_unknown": containing_uuid,
                    "third_unknown": short_text,
                    "fourth_unknown": containing_text,
                }
            )
        }
    )
    result = await _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=OverlapRuntime(),
        policy=_policy("activity"),
    ).ask(request)

    assert result.draft.status is DecisionStatus.NEEDS_CLARIFICATION


async def test_related_record_aliasing_handles_uuid_prefixes_and_punctuation(
    session_factory,
):
    canonical_uuid = "12345678-9abc-def0-1234-56789abcdef0"
    uuid_prefixed_text = f"{canonical_uuid}-capture-secret"
    noncanonical_uuid = "1-23456789abcdef0123456789abcdef0"
    punctuation_id = "/abc/"
    assert uuid.UUID(noncanonical_uuid)

    class EdgeCaseRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            serialized = turn.model_dump_json()
            for record_id in (
                canonical_uuid,
                uuid_prefixed_text,
                noncanonical_uuid,
                punctuation_id,
            ):
                assert record_id not in serialized
            assert "capture-secret" not in serialized
            assert turn.request.question.count("rr_") == 4
            assert re.fullmatch(
                (
                    r"Compare rr_[0-9a-f]{16} with rr_[0-9a-f]{16}; "
                    r"inspect rr_[0-9a-f]{16}; prefixrr_[0-9a-f]{16}"
                ),
                turn.request.question,
            )
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.NEEDS_CLARIFICATION,
                    clarification_question=(
                        "Which retained record should I inspect?"
                    ),
                ),
                metadata=self.metadata,
            )

    request = _request(
        question=(
            f"Compare {uuid_prefixed_text} with {canonical_uuid}; "
            f"inspect {noncanonical_uuid}; prefix{punctuation_id}"
        )
    ).model_copy(
        update={
            "hints": DecisionContextHints(
                related_record_ids={
                    "first_unknown": canonical_uuid,
                    "second_unknown": uuid_prefixed_text,
                    "third_unknown": noncanonical_uuid,
                    "fourth_unknown": punctuation_id,
                }
            )
        }
    )
    result = await _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=EdgeCaseRuntime(),
        policy=_policy("activity"),
    ).ask(request)

    assert result.draft.status is DecisionStatus.NEEDS_CLARIFICATION


async def test_token_related_id_does_not_rewrite_unrelated_substrings(
    session_factory,
):
    record_id = "abc"
    request = _request(
        question="Inspect abc, not xabcx or cabcaffeine."
    ).model_copy(
        update={
            "hints": DecisionContextHints(
                related_record_ids={"unknown_record": record_id}
            )
        }
    )
    aliases = decision_agent_module._runtime_record_aliases(
        request,
        related_records=(),
    )
    aliased_payload = decision_agent_module._alias_runtime_value(
        {
            "abc": "abc",
            "xabcx": "cabcaffeine",
        },
        aliases=aliases,
    )
    reference = aliases[0].reference

    assert aliased_payload == {
        reference: reference,
        "xabcx": "cabcaffeine",
    }

    class BoundaryRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            assert re.fullmatch(
                (
                    r"Inspect rr_[0-9a-f]{16}, "
                    r"not xabcx or cabcaffeine\."
                ),
                turn.request.question,
            )
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.NEEDS_CLARIFICATION,
                    clarification_question=(
                        "Which retained record should I inspect?"
                    ),
                ),
                metadata=self.metadata,
            )

    result = await _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=BoundaryRuntime(),
        policy=_policy("activity"),
    ).ask(request)

    assert result.draft.status is DecisionStatus.NEEDS_CLARIFICATION


async def test_uuid_alias_redacts_unlisted_noncanonical_equivalents(
    session_factory,
):
    canonical_uuid = "12345678-9abc-def0-1234-56789abcdef0"
    noncanonical_uuid = "1-23456789abcdef0123456789abcdef0"
    request = _request(
        question=f"Inspect {noncanonical_uuid} now."
    ).model_copy(
        update={
            "hints": DecisionContextHints(
                related_record_ids={
                    "unknown_record": canonical_uuid,
                }
            )
        }
    )
    aliases = decision_agent_module._runtime_record_aliases(
        request,
        related_records=(),
    )
    reference = aliases[0].reference

    assert decision_agent_module._alias_runtime_value(
        {
            "record_id": f"selected {noncanonical_uuid}",
            "unrelated": f"x{noncanonical_uuid}x",
        },
        aliases=aliases,
    ) == {
        "record_id": f"selected {reference}",
        "unrelated": f"x{noncanonical_uuid}x",
    }

    class NoncanonicalRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            assert noncanonical_uuid not in turn.request.question
            assert re.fullmatch(
                r"Inspect rr_[0-9a-f]{16} now\.",
                turn.request.question,
            )
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.NEEDS_CLARIFICATION,
                    clarification_question=(
                        "Which retained record should I inspect?"
                    ),
                ),
                metadata=self.metadata,
            )

    result = await _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=NoncanonicalRuntime(),
        policy=_policy("activity"),
    ).ask(request)

    assert result.draft.status is DecisionStatus.NEEDS_CLARIFICATION


@pytest.mark.parametrize("value", ("{" * 800, "-" * 16_000))
def test_uuid_alias_matcher_is_bounded_on_adversarial_input(value):
    request = _request().model_copy(
        update={
            "hints": DecisionContextHints(
                related_record_ids={
                    f"unknown_record_{index}": str(uuid.uuid4())
                    for index in range(32)
                }
            )
        }
    )
    aliases = decision_agent_module._runtime_record_aliases(
        request,
        related_records=(),
    )
    started = time.monotonic()

    result = decision_agent_module._alias_runtime_value(
        value,
        aliases=aliases,
    )
    elapsed = time.monotonic() - started

    assert result == value
    assert elapsed < 2


async def test_related_record_uses_provider_compatible_uuid_format(
    session_factory,
):
    parsed_id = uuid.uuid4()
    hex_record_id = parsed_id.hex

    class HexUuidProvider:
        def __init__(self) -> None:
            self.calls = []
            self.metadata = ContextProviderMetadata(
                provider_id="nutrition",
                domain="nutrition",
                description="Hex-only selected nutrition record.",
                capabilities=(
                    ContextCapability(
                        capability="nutrition.hex-record",
                        description="Read one selected hex UUID record.",
                        granularities=("summary",),
                        query_fields=("timezone",),
                        output_fields=("status",),
                        parameters=("request_id",),
                        parameter_specs=(
                            ContextParameterSpec(
                                name="request_id",
                                value_type=ContextParameterType.STRING,
                                required=True,
                                min_length=32,
                                max_length=32,
                                format=ContextParameterFormat.UUID,
                                accepts_related_record_ref=True,
                            ),
                        ),
                        max_lookback_days=30,
                        sensitivity="nutrition",
                        provenance=ProvenanceSupport.PARTIAL,
                        freshness_expectation="Selected stored context.",
                    ),
                ),
            )

        async def query(self, session, query, *, now):
            del session
            self.calls.append(query)
            return ContextResult(
                query_id=query.query_id,
                provider_id=query.provider_id,
                capability=query.capability,
                status=ContextStatus.OK,
                payload={"status": "ok"},
                freshness=ContextFreshness(
                    status=FreshnessStatus.CURRENT,
                    as_of=now,
                ),
                coverage=ContextCoverage(
                    status=CoverageStatus.COMPLETE,
                    ratio=1,
                ),
            )

    class HexRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            if not turn.history:
                reference = (
                    turn.request.hints.related_records[0].reference
                )
                return RuntimeStepOutput(
                    tool_calls=(
                        {
                            "capability": "nutrition.hex-record",
                            "parameters": {"request_id": reference},
                        },
                    ),
                    metadata=self.metadata,
                )
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer="The selected record is available.",
                ),
                metadata=self.metadata,
            )

    provider = HexUuidProvider()
    request = _request(
        question=f"Inspect {str(parsed_id)}"
    ).model_copy(
        update={
            "hints": DecisionContextHints(
                related_record_ids={
                    "nutrition_request": hex_record_id,
                }
            )
        }
    )
    result = await _agent(
        session_factory,
        providers=(provider,),
        runtime=HexRuntime(),
        policy=_policy("nutrition"),
    ).ask(request)

    assert result.draft.status is DecisionStatus.COMPLETED
    assert provider.calls[0].parameters == {
        "request_id": hex_record_id
    }


async def test_production_nutrition_history_keeps_related_id_aliased(
    session_factory,
    monkeypatch,
):
    actual_request_id = str(uuid.uuid4())
    observed_at = NOW - timedelta(hours=1)
    with session_factory() as session:
        event = WellnessEvent(
            event_type="nutrition.decision-request.v1",
            schema_version=1,
            observed_at=observed_at,
            recorded_at=observed_at,
            timezone="UTC",
            source_provider="nutrition-decision-request",
            source_device=None,
            source_record_id=actual_request_id,
            capture_method="test",
            quality_flags={},
            confidence=1,
            coverage=1,
            sensitivity="nutrition",
            consent_scope="personal",
            retention_policy_id=None,
            expires_at=None,
            payload={"status": "ok"},
            raw_object_id=None,
            derived_from=None,
        )
        session.add(event)
        session.commit()
        event_id = str(event.id)

    monkeypatch.setattr(
        "healthmes.decision.domain_providers.nutrition_decision_context",
        lambda *args, **kwargs: {
            "status": "ok",
            "request": {
                "request_id": actual_request_id,
                "scope": "daily_nutrition",
                "question": "Should I have this private meal?",
                "requested_at": observed_at.isoformat(),
                "intended_consumption_at": NOW.isoformat(),
            },
            "candidate": {
                "is_confirmed_intake": False,
                "resolved_items": [],
            },
            "comparison_candidates": [],
            "confirmed_intake_history": [],
            "history_window": {
                "start": (NOW - timedelta(days=1)).isoformat(),
                "end": NOW.isoformat(),
                "lookback_days": 1,
                "coverage": "captured_records_only",
                "query": {"complete": True},
            },
            "specialized_evidence": {"caffeine": None},
            "evidence_event_ids": [event_id],
            "boundaries": {
                "candidate_is_not_consumed": True,
                "history_is_not_complete_day_proof": True,
                "medical_safety_requires_separate_policy": True,
            },
        },
    )

    class ProductionAliasRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        def __init__(self) -> None:
            self.reference: str | None = None

        async def next_step(self, turn):
            assert actual_request_id not in turn.model_dump_json()
            if not turn.history:
                self.reference = (
                    turn.request.hints.related_records[0].reference
                )
                return RuntimeStepOutput(
                    tool_calls=(
                        {
                            "capability": "nutrition.decision-context",
                            "privacy_level": "identity",
                            "parameters": {
                                "request_id": self.reference,
                            },
                        },
                    ),
                    metadata=self.metadata,
                )
            context = turn.history[0].results[0]
            assert context.payload["request"]["request_id"] == self.reference
            assert context.source_ref_ids
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer="The selected nutrition context is available.",
                    used_source_ref_ids=[context.source_ref_ids[0]],
                ),
                metadata=self.metadata,
            )

    runtime = ProductionAliasRuntime()
    request = _request(privacy=PrivacyLevel.IDENTITY).model_copy(
        update={
            "hints": DecisionContextHints(
                related_record_ids={
                    "nutrition_request": actual_request_id,
                }
            )
        }
    )
    result = await _agent(
        session_factory,
        providers=(NutritionContextProvider(),),
        runtime=runtime,
        policy=_policy(
            "nutrition",
            max_privacy=PrivacyLevel.IDENTITY,
        ),
    ).ask(request)

    assert result.draft.status is DecisionStatus.COMPLETED
    assert runtime.reference is not None
    assert result.tool_trace[0].query.parameters == {
        "request_id": actual_request_id
    }


async def test_model_tool_parameters_are_typed_before_provider_execution(
    session_factory,
):
    class TypedProvider:
        def __init__(self) -> None:
            self.calls = []
            self.metadata = ContextProviderMetadata(
                provider_id="nutrition",
                domain="nutrition",
                description="Typed nutrition history.",
                capabilities=(
                    ContextCapability(
                        capability="nutrition.intake-history",
                        description="Typed nutrition history.",
                        granularities=("summary",),
                        query_fields=("timezone",),
                        output_fields=("status",),
                        parameters=("confirmed_only",),
                        parameter_specs=(
                            ContextParameterSpec(
                                name="confirmed_only",
                                value_type=ContextParameterType.BOOLEAN,
                            ),
                        ),
                        max_lookback_days=30,
                        sensitivity="nutrition",
                        provenance=ProvenanceSupport.PARTIAL,
                        freshness_expectation="Current retained history.",
                    ),
                ),
            )

        async def query(self, session, query, *, now):
            del session, now
            self.calls.append(query)
            raise AssertionError("invalid parameters reached provider")

    class WrongTypeRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            assert (
                turn.tools[0].parameter_specs[0].value_type
                is ContextParameterType.BOOLEAN
            )
            return RuntimeStepOutput(
                tool_calls=(
                    {
                        "capability": "nutrition.intake-history",
                        "parameters": {"confirmed_only": "false"},
                    },
                ),
                metadata=self.metadata,
            )

    provider = TypedProvider()
    result = await _agent(
        session_factory,
        providers=(provider,),
        runtime=WrongTypeRuntime(),
        policy=_policy("nutrition"),
    ).ask(_request())

    assert result.draft.status is DecisionStatus.FAILED
    assert result.draft.limitations == ["malformed_tool_arguments"]
    assert provider.calls == []


async def test_catalog_only_advertises_raw_when_effectively_allowed(
    session_factory,
):
    provider = StubProvider(
        domain="activity",
        privacy_levels=(
            PrivacyLevel.AGGREGATE,
            PrivacyLevel.SCOPED_RAW,
        ),
        supports_raw=True,
    )
    aggregate_runtime = CapturingRuntime()
    aggregate_agent = _agent(
        session_factory,
        providers=(provider,),
        runtime=aggregate_runtime,
        policy=_policy(
            "activity",
            max_privacy=PrivacyLevel.SCOPED_RAW,
        ),
    )

    await aggregate_agent.ask(_request())

    aggregate_tool = aggregate_runtime.turns[0].tools[0]
    assert aggregate_tool.privacy_levels == (PrivacyLevel.AGGREGATE,)
    assert aggregate_tool.supports_raw is False

    raw_runtime = CapturingRuntime()
    raw_agent = _agent(
        session_factory,
        providers=(provider,),
        runtime=raw_runtime,
        policy=_policy(
            "activity",
            max_privacy=PrivacyLevel.SCOPED_RAW,
        ),
    )

    await raw_agent.ask(_request(privacy=PrivacyLevel.SCOPED_RAW))

    raw_tool = raw_runtime.turns[0].tools[0]
    assert PrivacyLevel.SCOPED_RAW in raw_tool.privacy_levels
    assert raw_tool.supports_raw is True


async def test_registry_catalog_uses_validated_metadata_snapshot(
    session_factory,
):
    provider = StubProvider(domain="activity")
    registry = ContextProviderRegistry((provider,))
    original_capability = provider.metadata.capabilities[0]
    provider.metadata = provider.metadata.model_copy(
        update={
            "capabilities": (
                original_capability.model_copy(
                    update={"granularities": ()}
                ),
            )
        }
    )
    runtime = CapturingRuntime()
    agent = HealthMesDecisionAgent(
        access_layer=ContextAccessLayer(
            registry,
            clock=lambda: NOW,
        ),
        runtime=runtime,
        session_factory=session_factory,
        policy_resolver=lambda request: _policy("activity"),
        clock=lambda: NOW,
    )

    result = await agent.ask(_request())

    assert result.draft.status is DecisionStatus.NEEDS_CLARIFICATION
    assert runtime.turns[0].tools[0].granularities == ("summary",)
    agent.close()


async def test_runtime_receives_no_gateway_or_step_callbacks(
    session_factory,
):
    provider = StubProvider(domain="activity")

    class BoundaryRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            assert not hasattr(turn, "call_tool")
            assert not hasattr(turn, "invoker")
            assert not hasattr(turn, "consume_step")
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer="No context was needed.",
                ),
                metadata=self.metadata,
            )

    result = await _agent(
        session_factory,
        providers=(provider,),
        runtime=BoundaryRuntime(),
        policy=_policy("activity"),
    ).ask(_request())

    assert result.draft.status is DecisionStatus.COMPLETED
    assert provider.calls == []
    assert result.tool_trace == ()


async def test_runtime_and_provider_share_one_turn_owned_worker_loop(
    session_factory,
):
    main_thread = threading.get_ident()
    locations: list[tuple[str, int, int]] = []

    class LocatedProvider(StubProvider):
        async def query(self, session, query, *, now):
            locations.append(
                (
                    "provider",
                    threading.get_ident(),
                    id(asyncio.get_running_loop()),
                )
            )
            return await super().query(session, query, now=now)

    class LocatedRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            locations.append(
                (
                    "runtime",
                    threading.get_ident(),
                    id(asyncio.get_running_loop()),
                )
            )
            if not turn.history:
                return RuntimeStepOutput(
                    tool_calls=({"capability": "activity.summary"},),
                    metadata=self.metadata,
                )
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer="The isolated turn completed.",
                ),
                metadata=self.metadata,
            )

    result = await _agent(
        session_factory,
        providers=(LocatedProvider(domain="activity"),),
        runtime=LocatedRuntime(),
        policy=_policy("activity"),
    ).ask(_request())

    assert result.draft.status is DecisionStatus.COMPLETED
    assert [kind for kind, _, _ in locations] == [
        "runtime",
        "provider",
        "runtime",
    ]
    assert {thread_id for _, thread_id, _ in locations} != {main_thread}
    assert len({thread_id for _, thread_id, _ in locations}) == 1
    assert len({loop_id for _, _, loop_id in locations}) == 1


async def test_runtime_and_provider_keep_stable_loop_across_requests(
    session_factory,
):
    runtime_loop: int | None = None
    provider_loop: int | None = None

    class LoopBoundProvider(StubProvider):
        async def query(self, session, query, *, now):
            nonlocal provider_loop
            current_loop = id(asyncio.get_running_loop())
            if provider_loop is None:
                provider_loop = current_loop
            elif provider_loop != current_loop:
                raise RuntimeError("provider event loop changed")
            return await super().query(session, query, now=now)

    class LoopBoundRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            nonlocal runtime_loop
            current_loop = id(asyncio.get_running_loop())
            if runtime_loop is None:
                runtime_loop = current_loop
            elif runtime_loop != current_loop:
                raise RuntimeError("runtime event loop changed")
            if not turn.history:
                return RuntimeStepOutput(
                    tool_calls=({"capability": "activity.summary"},),
                    metadata=self.metadata,
                )
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer="The stable worker loop completed the request.",
                ),
                metadata=self.metadata,
            )

    agent = _agent(
        session_factory,
        providers=(LoopBoundProvider(domain="activity"),),
        runtime=LoopBoundRuntime(),
        policy=_policy("activity"),
    )

    first = await agent.ask(_request())
    second = await agent.ask(
        _request().model_copy(
            update={
                "request_id": uuid.uuid4(),
                "turn_id": uuid.uuid4(),
            }
        )
    )

    assert first.draft.status is DecisionStatus.COMPLETED
    assert second.draft.status is DecisionStatus.COMPLETED
    assert runtime_loop == provider_loop
    agent.close()


async def test_same_question_adapts_tool_path_to_first_result(
    session_factory,
):
    complete_runtime = AdaptiveRuntime()
    complete_agent = _agent(
        session_factory,
        providers=(
            StubProvider(domain="activity"),
            StubProvider(domain="wearable"),
        ),
        runtime=complete_runtime,
        policy=_policy("activity", "wearable"),
    )
    partial_runtime = AdaptiveRuntime()
    partial_agent = _agent(
        session_factory,
        providers=(
            StubProvider(
                domain="activity",
                coverage=ContextCoverage(
                    status=CoverageStatus.PARTIAL,
                    ratio=0.4,
                ),
            ),
            StubProvider(domain="wearable"),
        ),
        runtime=partial_runtime,
        policy=_policy("activity", "wearable"),
    )
    question = _request()

    complete = await complete_agent.ask(question)
    partial = await partial_agent.ask(
        question.model_copy(
            update={
                "request_id": uuid.uuid4(),
                "turn_id": uuid.uuid4(),
            }
        )
    )

    assert complete.draft.status is DecisionStatus.COMPLETED
    assert partial.draft.status is DecisionStatus.COMPLETED
    assert complete.steps_used == 2
    assert partial.steps_used == 3
    assert complete_runtime.capabilities == ["activity.summary"]
    assert partial_runtime.capabilities == [
        "activity.summary",
        "wearable.summary",
    ]


async def test_multiple_tools_in_one_iteration_use_one_step_and_two_calls(
    session_factory,
):
    class BatchRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            if not turn.history:
                return RuntimeStepOutput(
                    tool_calls=(
                        {"capability": "activity.summary"},
                        {"capability": "wearable.summary"},
                    ),
                    metadata=self.metadata,
                )
            assert len(turn.history) == 1
            assert len(turn.history[0].results) == 2
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer="Both requested contexts were available.",
                ),
                metadata=self.metadata,
            )

    result = await _agent(
        session_factory,
        providers=(
            StubProvider(domain="activity"),
            StubProvider(domain="wearable"),
        ),
        runtime=BatchRuntime(),
        policy=_policy("activity", "wearable"),
    ).ask(_request())

    assert result.steps_used == 2
    assert len(result.tool_trace) == 2


async def test_coffee_question_combines_candidate_time_sleep_and_strain(
    session_factory,
):
    def value_result(value):
        def factory(query, now):
            return ContextResult(
                query_id=query.query_id,
                provider_id=query.provider_id,
                capability=query.capability,
                status=ContextStatus.OK,
                payload={"status": "ok", "value": value},
                freshness=ContextFreshness(
                    status=FreshnessStatus.CURRENT,
                    as_of=now,
                ),
                coverage=ContextCoverage(
                    status=CoverageStatus.COMPLETE,
                    ratio=1,
                ),
            )

        return factory

    class CoffeeRuntime:
        metadata = RuntimeMetadata(runtime="scripted", model="coffee-v1")

        def __init__(self) -> None:
            self.capabilities: list[str] = []

        async def next_step(self, turn):
            if not turn.history:
                self.capabilities.append("nutrition.summary")
                return RuntimeStepOutput(
                    tool_calls=({"capability": "nutrition.summary"},),
                    metadata=self.metadata,
                )

            if len(turn.history) == 1:
                candidate = turn.history[0].results[0]
                assert candidate.payload["value"] == 120
                assert turn.request.requested_at == NOW
                self.capabilities.append("wearable.summary")
                return RuntimeStepOutput(
                    tool_calls=({"capability": "wearable.summary"},),
                    metadata=self.metadata,
                )

            sleep = turn.history[1].results[0]
            if len(turn.history) == 2 and sleep.payload["value"] < 6:
                self.capabilities.append("activity.summary")
                return RuntimeStepOutput(
                    tool_calls=({"capability": "activity.summary"},),
                    metadata=self.metadata,
                )

            assert turn.history[2].results[0].payload["value"] == 180
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer=(
                        "The candidate has caffeine, sleep was short, and "
                        "recent work strain was high."
                    ),
                    uncertainty=(
                        "The exact serving and individual sensitivity still "
                        "matter."
                    ),
                ),
                metadata=self.metadata,
            )

    runtime = CoffeeRuntime()
    agent = _agent(
        session_factory,
        providers=(
            StubProvider(
                domain="nutrition",
                result_factory=value_result(120),
            ),
            StubProvider(
                domain="wearable",
                result_factory=value_result(5.5),
            ),
            StubProvider(
                domain="activity",
                result_factory=value_result(180),
            ),
            StubProvider(
                domain="calendar",
                result_factory=value_result(0),
            ),
        ),
        runtime=runtime,
        policy=_policy(
            "nutrition",
            "wearable",
            "activity",
            "calendar",
        ),
    )

    result = await agent.ask(
        _request(question="Can I drink this coffee now?")
    )

    assert result.draft.status is DecisionStatus.COMPLETED
    assert runtime.capabilities == [
        "nutrition.summary",
        "wearable.summary",
        "activity.summary",
    ]
    assert "calendar.summary" not in runtime.capabilities
    assert result.steps_used == 4


async def test_agent_collects_only_gateway_returned_source_refs(
    session_factory,
):
    with session_factory() as session:
        event = WellnessEvent(
            event_type="nutrition.observation.v1",
            schema_version=1,
            observed_at=NOW,
            recorded_at=NOW,
            timezone="UTC",
            source_provider="nutrition",
            source_device=None,
            source_record_id=uuid.uuid4().hex,
            capture_method="test",
            quality_flags={},
            confidence=1,
            coverage=1,
            sensitivity="nutrition",
            consent_scope="personal",
            retention_policy_id=None,
            expires_at=None,
            payload={"status": "ok", "value": 80},
            raw_object_id=None,
            derived_from=None,
        )
        session.add(event)
        session.commit()

    source_ref = SourceRef(
        domain="nutrition",
        resource_type=event.event_type,
        record_id=str(event.id),
        source_provider=event.source_provider,
        observed_start=NOW,
        schema_version=event.schema_version,
        derived_by="nutrition.test.v1",
        freshness=FreshnessStatus.CURRENT,
        coverage=event.coverage,
        sensitivity=event.sensitivity,
    )

    def result_factory(query, now):
        return ContextResult(
            query_id=query.query_id,
            provider_id=query.provider_id,
            capability=query.capability,
            status=ContextStatus.OK,
            payload={"status": "ok", "value": 80},
            source_refs=[source_ref],
            freshness=ContextFreshness(
                status=FreshnessStatus.CURRENT,
                as_of=now,
            ),
            coverage=ContextCoverage(
                status=CoverageStatus.COMPLETE,
                ratio=1,
            ),
        )

    class SourceRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            if not turn.history:
                return RuntimeStepOutput(
                    tool_calls=({"capability": "nutrition.summary"},),
                    metadata=self.metadata,
                )
            reference_id = (
                turn.history[0].results[0].source_ref_ids[0]
            )
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer="The recorded intake is available.",
                    proposed_action=True,
                    used_source_ref_ids=[reference_id],
                ),
                metadata=self.metadata,
            )

    agent = _agent(
        session_factory,
        providers=(
            StubProvider(
                domain="nutrition",
                result_factory=result_factory,
            ),
        ),
        runtime=SourceRuntime(),
        policy=_policy("nutrition"),
    )

    result = await agent.ask(_request())

    assert len(result.source_refs) == 1
    _assert_gateway_attested_source_ref(
        result.source_refs[0],
        source_ref,
    )
    assert result.draft.used_source_ref_ids == [
        source_ref.reference_id
    ]


async def test_runtime_receives_gateway_derived_completeness_times(
    session_factory,
):
    observed = NOW - timedelta(hours=2)
    observed_end = NOW - timedelta(hours=1)
    recorded = NOW - timedelta(minutes=30)
    with session_factory() as session:
        event = WellnessEvent(
            event_type="activity.hour.v1",
            schema_version=1,
            observed_at=observed,
            recorded_at=recorded,
            timezone="UTC",
            source_provider="activity",
            source_device=None,
            source_record_id=uuid.uuid4().hex,
            capture_method="test",
            quality_flags={},
            confidence=1,
            coverage=1,
            sensitivity="activity",
            consent_scope="personal",
            retention_policy_id=None,
            expires_at=None,
            payload={
                "window": {
                    "start": observed.isoformat(),
                    "end": observed_end.isoformat(),
                },
                "status": "ok",
                "value": 1,
            },
            raw_object_id=None,
            derived_from=None,
        )
        session.add(event)
        session.commit()

    source_ref = SourceRef(
        domain="activity",
        resource_type=event.event_type,
        record_id=str(event.id),
        source_provider=event.source_provider,
        observed_start=observed,
        observed_end=observed_end,
        schema_version=event.schema_version,
        coverage=event.coverage,
        sensitivity=event.sensitivity,
    )

    def result_factory(query, now):
        return ContextResult(
            query_id=query.query_id,
            provider_id=query.provider_id,
            capability=query.capability,
            status=ContextStatus.OK,
            payload={"status": "ok", "value": 1},
            source_refs=[source_ref],
            observed_start=NOW - timedelta(days=10),
            observed_end=NOW - timedelta(days=9),
            collected_at=NOW - timedelta(days=9),
            freshness=ContextFreshness(
                status=FreshnessStatus.CURRENT,
                as_of=now,
            ),
            coverage=ContextCoverage(
                status=CoverageStatus.COMPLETE,
                ratio=1,
            ),
        )

    class CompletenessRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            if not turn.history:
                return RuntimeStepOutput(
                    tool_calls=({"capability": "activity.summary"},),
                    metadata=self.metadata,
                )
            context = turn.history[0].results[0]
            assert context.observed_start == observed
            assert context.observed_end == observed_end
            assert context.collected_at == recorded
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer="The activity context is available.",
                    used_source_ref_ids=[source_ref.reference_id],
                ),
                metadata=self.metadata,
            )

    result = await _agent(
        session_factory,
        providers=(
            StubProvider(
                domain="activity",
                result_factory=result_factory,
            ),
        ),
        runtime=CompletenessRuntime(),
        policy=_policy("activity"),
    ).ask(_request())

    assert result.draft.status is DecisionStatus.COMPLETED


@pytest.mark.parametrize(
    ("provider_status", "persisted"),
    [
        (ContextStatus.OK, True),
        (ContextStatus.PARTIAL, True),
        (ContextStatus.DENIED, False),
        (ContextStatus.FAILED, False),
        (ContextStatus.UNAVAILABLE, False),
    ],
)
async def test_provider_writes_commit_only_for_usable_context_results(
    session_factory,
    provider_status,
    persisted,
):
    sentinel_id = uuid.uuid4()

    class TransactionalProvider(StubProvider):
        async def query(self, session, query, *, now):
            self.calls.append(query)
            session.add(
                WellnessEvent(
                    id=sentinel_id,
                    event_type="nutrition.transaction-probe.v1",
                    schema_version=1,
                    observed_at=now,
                    recorded_at=now,
                    timezone="UTC",
                    source_provider="transaction-probe",
                    source_device=None,
                    source_record_id=str(sentinel_id),
                    capture_method="test",
                    quality_flags={},
                    confidence=1,
                    coverage=1,
                    sensitivity="nutrition",
                    consent_scope="personal",
                    retention_policy_id=None,
                    expires_at=None,
                    payload={"status": provider_status.value},
                    raw_object_id=None,
                    derived_from=None,
                )
            )
            session.flush()
            return ContextResult(
                query_id=query.query_id,
                provider_id=query.provider_id,
                capability=query.capability,
                status=provider_status,
                freshness=ContextFreshness(
                    status=(
                        FreshnessStatus.CURRENT
                        if provider_status
                        in {ContextStatus.OK, ContextStatus.PARTIAL}
                        else FreshnessStatus.UNAVAILABLE
                    ),
                    as_of=(
                        now
                        if provider_status
                        in {ContextStatus.OK, ContextStatus.PARTIAL}
                        else None
                    ),
                ),
                coverage=ContextCoverage(
                    status=(
                        CoverageStatus.COMPLETE
                        if provider_status
                        in {ContextStatus.OK, ContextStatus.PARTIAL}
                        else CoverageStatus.UNAVAILABLE
                    ),
                    ratio=(
                        1
                        if provider_status
                        in {ContextStatus.OK, ContextStatus.PARTIAL}
                        else None
                    ),
                ),
                limitations=(
                    []
                    if provider_status is ContextStatus.OK
                    else ["transaction_probe_result"]
                ),
            )

    class OneQueryRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            if not turn.history:
                return RuntimeStepOutput(
                    tool_calls=({"capability": "nutrition.summary"},),
                    metadata=self.metadata,
                )
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.NEEDS_CLARIFICATION,
                    clarification_question="Should I inspect another source?",
                ),
                metadata=self.metadata,
            )

    await _agent(
        session_factory,
        providers=(TransactionalProvider(domain="nutrition"),),
        runtime=OneQueryRuntime(),
        policy=_policy("nutrition"),
    ).ask(_request())

    with session_factory() as session:
        assert (session.get(WellnessEvent, sentinel_id) is not None) is persisted


async def test_malformed_provider_result_rolls_back_provider_writes(
    session_factory,
):
    sentinel_id = uuid.uuid4()

    class MalformedTransactionalProvider(StubProvider):
        async def query(self, session, query, *, now):
            self.calls.append(query)
            session.add(
                WellnessEvent(
                    id=sentinel_id,
                    event_type="nutrition.transaction-probe.v1",
                    schema_version=1,
                    observed_at=now,
                    recorded_at=now,
                    timezone="UTC",
                    source_provider="transaction-probe",
                    source_device=None,
                    source_record_id=str(sentinel_id),
                    capture_method="test",
                    quality_flags={},
                    confidence=1,
                    coverage=1,
                    sensitivity="nutrition",
                    consent_scope="personal",
                    retention_policy_id=None,
                    expires_at=None,
                    payload={"status": "malformed"},
                    raw_object_id=None,
                    derived_from=None,
                )
            )
            session.flush()
            valid = ContextResult(
                query_id=query.query_id,
                provider_id=query.provider_id,
                capability=query.capability,
                status=ContextStatus.OK,
            )
            return valid.model_copy(
                update={"provider_id": "wrong-provider"}
            )

    class OneQueryRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            if not turn.history:
                return RuntimeStepOutput(
                    tool_calls=({"capability": "nutrition.summary"},),
                    metadata=self.metadata,
                )
            assert turn.history[0].results[0].status is ContextStatus.FAILED
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.NEEDS_CLARIFICATION,
                    clarification_question="Can I retry the failed source?",
                ),
                metadata=self.metadata,
            )

    await _agent(
        session_factory,
        providers=(MalformedTransactionalProvider(domain="nutrition"),),
        runtime=OneQueryRuntime(),
        policy=_policy("nutrition"),
    ).ask(_request())

    with session_factory() as session:
        assert session.get(WellnessEvent, sentinel_id) is None


async def test_completed_context_answer_must_declare_a_source_ref(
    session_factory,
):
    with session_factory() as session:
        event = WellnessEvent(
            event_type="activity.hour.v1",
            schema_version=1,
            observed_at=NOW,
            recorded_at=NOW,
            timezone="UTC",
            source_provider="activity",
            source_device=None,
            source_record_id=uuid.uuid4().hex,
            capture_method="test",
            quality_flags={},
            confidence=1,
            coverage=1,
            sensitivity="activity",
            consent_scope="personal",
            retention_policy_id=None,
            expires_at=None,
            payload={"status": "ok", "value": 1},
            raw_object_id=None,
            derived_from=None,
        )
        session.add(event)
        session.commit()

    source_ref = SourceRef(
        domain="activity",
        resource_type=event.event_type,
        record_id=str(event.id),
        source_provider=event.source_provider,
        observed_start=NOW,
        schema_version=event.schema_version,
        coverage=event.coverage,
        sensitivity=event.sensitivity,
    )

    def result_factory(query, now):
        return ContextResult(
            query_id=query.query_id,
            provider_id=query.provider_id,
            capability=query.capability,
            status=ContextStatus.OK,
            payload={"status": "ok", "value": 1},
            source_refs=[source_ref],
            freshness=ContextFreshness(
                status=FreshnessStatus.CURRENT,
                as_of=now,
            ),
            coverage=ContextCoverage(
                status=CoverageStatus.COMPLETE,
                ratio=1,
            ),
        )

    class OmittingRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            if not turn.history:
                return RuntimeStepOutput(
                    tool_calls=({"capability": "activity.summary"},),
                    metadata=self.metadata,
                )
            assert turn.history[0].results[0].source_ref_ids == (
                source_ref.reference_id,
            )
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer="This answer silently omitted its source.",
                ),
                metadata=self.metadata,
            )

    result = await _agent(
        session_factory,
        providers=(
            StubProvider(
                domain="activity",
                result_factory=result_factory,
            ),
        ),
        runtime=OmittingRuntime(),
        policy=_policy("activity"),
    ).ask(_request())

    assert result.draft.status is DecisionStatus.FAILED
    assert result.draft.limitations == ["runtime_source_refs_omitted"]
    assert len(result.source_refs) == 1
    _assert_gateway_attested_source_ref(
        result.source_refs[0],
        source_ref,
    )


async def test_provider_cannot_smuggle_model_copy_forged_source_ref(
    session_factory,
):
    with session_factory() as session:
        event = WellnessEvent(
            event_type="nutrition.observation.v1",
            schema_version=1,
            observed_at=NOW,
            recorded_at=NOW,
            timezone="UTC",
            source_provider="nutrition",
            source_device=None,
            source_record_id=uuid.uuid4().hex,
            capture_method="test",
            quality_flags={},
            confidence=1,
            coverage=1,
            sensitivity="nutrition",
            consent_scope="personal",
            retention_policy_id=None,
            expires_at=None,
            payload={"status": "ok", "value": 80},
            raw_object_id=None,
            derived_from=None,
        )
        session.add(event)
        session.commit()

    valid_ref = SourceRef(
        domain="nutrition",
        resource_type=event.event_type,
        record_id=str(event.id),
        source_provider=event.source_provider,
        observed_start=NOW,
        schema_version=event.schema_version,
        sensitivity=event.sensitivity,
    )
    forged_ref = valid_ref.model_copy(
        update={"reference_id": "sr_" + ("0" * 32)}
    )

    def forged_result(query, now):
        valid_result = ContextResult(
            query_id=query.query_id,
            provider_id=query.provider_id,
            capability=query.capability,
            status=ContextStatus.OK,
            payload={"status": "ok", "value": 80},
            source_refs=[valid_ref],
        )
        return valid_result.model_copy(
            update={"source_refs": [forged_ref]}
        )

    class InspectingRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            if not turn.history:
                return RuntimeStepOutput(
                    tool_calls=({"capability": "nutrition.summary"},),
                    metadata=self.metadata,
                )
            raise AssertionError(
                "a provider contract failure must terminate the turn"
            )

    result = await _agent(
        session_factory,
        providers=(
            StubProvider(
                domain="nutrition",
                result_factory=forged_result,
            ),
        ),
        runtime=InspectingRuntime(),
        policy=_policy("nutrition"),
    ).ask(_request())

    assert result.draft.status is DecisionStatus.FAILED
    assert result.draft.limitations == [
        "provider_contract_violation"
    ]
    assert result.source_refs == ()


async def test_runtime_cannot_invent_source_reference_ids(
    session_factory,
):
    with session_factory() as session:
        event = WellnessEvent(
            event_type="activity.hour.v1",
            schema_version=1,
            observed_at=NOW,
            recorded_at=NOW,
            timezone="UTC",
            source_provider="activity",
            source_device=None,
            source_record_id=uuid.uuid4().hex,
            capture_method="test",
            quality_flags={},
            confidence=1,
            coverage=1,
            sensitivity="activity",
            consent_scope="personal",
            retention_policy_id=None,
            expires_at=None,
            payload={"status": "ok", "value": 1},
            raw_object_id=None,
            derived_from=None,
        )
        session.add(event)
        session.commit()

    real_ref = SourceRef(
        domain="activity",
        resource_type=event.event_type,
        record_id=str(event.id),
        source_provider=event.source_provider,
        observed_start=NOW,
        schema_version=event.schema_version,
        coverage=event.coverage,
        sensitivity=event.sensitivity,
    )
    forged_id = source_ref_id(
        domain="activity",
        resource_type="activity.hour.v1",
        source_provider="activity",
        record_id="forged",
    )

    def result_factory(query, now):
        return ContextResult(
            query_id=query.query_id,
            provider_id=query.provider_id,
            capability=query.capability,
            status=ContextStatus.OK,
            payload={"status": "ok", "value": 1},
            source_refs=[real_ref],
        )

    class ForgingRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            if not turn.history:
                return RuntimeStepOutput(
                    tool_calls=({"capability": "activity.summary"},),
                    metadata=self.metadata,
                )
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer="This action cites a forged reference.",
                    proposed_action=True,
                    used_source_ref_ids=[forged_id],
                ),
                metadata=self.metadata,
            )

    result = await _agent(
        session_factory,
        providers=(
            StubProvider(
                domain="activity",
                result_factory=result_factory,
            ),
        ),
        runtime=ForgingRuntime(),
        policy=_policy("activity"),
    ).ask(_request())

    assert result.draft.status is DecisionStatus.FAILED
    assert result.draft.limitations == ["runtime_source_ref_mismatch"]
    assert len(result.source_refs) == 1
    _assert_gateway_attested_source_ref(
        result.source_refs[0],
        real_ref,
    )


async def test_disallowed_privacy_is_denied_by_access_layer(
    session_factory,
):
    provider = StubProvider(
        domain="activity",
        privacy_levels=(
            PrivacyLevel.AGGREGATE,
            PrivacyLevel.IDENTITY,
        ),
    )

    class IdentityRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            if not turn.history:
                return RuntimeStepOutput(
                    tool_calls=(
                        {
                            "capability": "activity.summary",
                            "privacy_level": "identity",
                        },
                    ),
                    metadata=self.metadata,
                )
            self.result = turn.history[0].results[0]
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer="Identity access was not available.",
                ),
                metadata=self.metadata,
            )

    runtime = IdentityRuntime()
    agent = _agent(
        session_factory,
        providers=(provider,),
        runtime=runtime,
        policy=_policy("activity"),
    )

    result = await agent.ask(
        _request(privacy=PrivacyLevel.IDENTITY)
    )

    assert runtime.result.status is ContextStatus.DENIED
    assert runtime.result.limitations == (
        "domain_privacy_consent_denied",
    )
    assert result.draft.status is DecisionStatus.COMPLETED
    assert result.tool_trace[0].status is ToolCallStatus.DENIED


@pytest.mark.parametrize(
    ("mode", "expected", "status"),
    (
        ("unknown", "unknown_tool", DecisionStatus.FAILED),
        ("duplicate", "duplicate_tool_call", DecisionStatus.FAILED),
        (
            "step_budget",
            "decision_step_budget_exhausted",
            DecisionStatus.BLOCKED,
        ),
        (
            "tool_budget",
            "decision_tool_call_budget_exhausted",
            DecisionStatus.BLOCKED,
        ),
    ),
)
async def test_agent_fails_closed_on_invalid_or_looping_tool_calls(
    session_factory,
    mode,
    expected,
    status,
):
    class InvalidRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            if mode == "unknown":
                return RuntimeStepOutput(
                    tool_calls=({"capability": "unknown.summary"},),
                    metadata=self.metadata,
                )
            if not turn.history:
                return RuntimeStepOutput(
                    tool_calls=({"capability": "activity.summary"},),
                    metadata=self.metadata,
                )
            second_call = (
                {"capability": "activity.summary"}
                if mode == "duplicate"
                else {
                    "capability": "activity.summary",
                    "fields": ("value",),
                }
            )
            return RuntimeStepOutput(
                tool_calls=(second_call,),
                metadata=self.metadata,
            )

    budget = (
        DecisionBudget(max_steps=1, max_tool_calls=8)
        if mode == "step_budget"
        else DecisionBudget(max_steps=8, max_tool_calls=1)
        if mode == "tool_budget"
        else DecisionBudget()
    )
    agent = _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=InvalidRuntime(),
        policy=_policy("activity"),
    )

    result = await agent.ask(_request(budget=budget))

    assert result.draft.status is status
    assert result.draft.limitations == [expected]


async def test_duplicate_detection_ignores_purpose_and_field_order(
    session_factory,
):
    class SemanticDuplicateRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            if not turn.history:
                return RuntimeStepOutput(
                    tool_calls=(
                        {
                            "capability": "activity.summary",
                            "fields": ("status", "value"),
                            "purpose": "Initial check",
                        },
                    ),
                    metadata=self.metadata,
                )
            return RuntimeStepOutput(
                tool_calls=(
                    {
                        "capability": "activity.summary",
                        "fields": ("value", "status"),
                        "purpose": "Same lookup with new wording",
                    },
                ),
                metadata=self.metadata,
            )

    result = await _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=SemanticDuplicateRuntime(),
        policy=_policy("activity"),
    ).ask(_request())

    assert result.draft.status is DecisionStatus.FAILED
    assert result.draft.limitations == ["duplicate_tool_call"]
    assert len(result.tool_trace) == 2


async def test_duplicate_detection_uses_gateway_effective_query(
    session_factory,
):
    class NormalizedDuplicateRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            return RuntimeStepOutput(
                tool_calls=(
                    {
                        "capability": "activity.summary",
                        "limit": 999,
                        "purpose": "First oversized request",
                    },
                    {
                        "capability": "activity.summary",
                        "limit": 1_000,
                        "purpose": "Same effective request",
                    },
                ),
                metadata=self.metadata,
            )

    provider = StubProvider(domain="activity")
    result = await _agent(
        session_factory,
        providers=(provider,),
        runtime=NormalizedDuplicateRuntime(),
        policy=_policy("activity"),
    ).ask(_request())

    assert result.draft.status is DecisionStatus.FAILED
    assert result.draft.limitations == ["duplicate_tool_call"]
    assert len(provider.calls) == 1
    assert provider.calls[0].limit == 250
    assert len(result.tool_trace) == 2
    assert result.tool_trace[1].result is not None
    assert result.tool_trace[1].result.limitations == [
        "duplicate_tool_call"
    ]


async def test_turn_context_budget_exhaustion_stops_the_loop(
    session_factory,
):
    large_value = "x" * 600

    def large_result(query, now):
        return ContextResult(
            query_id=query.query_id,
            provider_id=query.provider_id,
            capability=query.capability,
            status=ContextStatus.OK,
            payload={"status": "ok", "value": large_value},
            freshness=ContextFreshness(
                status=FreshnessStatus.CURRENT,
                as_of=now,
            ),
            coverage=ContextCoverage(
                status=CoverageStatus.COMPLETE,
                ratio=1,
            ),
        )

    class ContextHungryRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            if not turn.history:
                return RuntimeStepOutput(
                    tool_calls=({"capability": "activity.summary"},),
                    metadata=self.metadata,
                )
            return RuntimeStepOutput(
                tool_calls=(
                    {
                        "capability": "activity.summary",
                        "fields": ("value",),
                    },
                ),
                metadata=self.metadata,
            )

    result = await _agent(
        session_factory,
        providers=(
            StubProvider(
                domain="activity",
                result_factory=large_result,
            ),
        ),
        runtime=ContextHungryRuntime(),
        policy=_policy("activity"),
    ).ask(
        _request(
            budget=DecisionBudget(
                max_context_bytes=1_024,
                max_tool_calls=8,
            )
        )
    )

    assert result.draft.status is DecisionStatus.BLOCKED
    assert result.draft.limitations == [
        "turn_context_byte_budget_exhausted"
    ]
    assert len(result.tool_trace) == 2


async def test_one_step_budget_cannot_include_tool_and_later_final(
    session_factory,
):
    class BypassRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        def __init__(self) -> None:
            self.calls = 0

        async def next_step(self, turn):
            self.calls += 1
            if not turn.history:
                return RuntimeStepOutput(
                    tool_calls=({"capability": "activity.summary"},),
                    metadata=self.metadata,
                )
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer="This second iteration must not run.",
                ),
                metadata=self.metadata,
            )

    runtime = BypassRuntime()
    result = await _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=runtime,
        policy=_policy("activity"),
    ).ask(
        _request(
            budget=DecisionBudget(max_steps=1, max_tool_calls=8)
        )
    )

    assert result.draft.status is DecisionStatus.BLOCKED
    assert result.draft.limitations == [
        "decision_step_budget_exhausted"
    ]
    assert runtime.calls == 1
    assert len(result.tool_trace) == 1


async def test_runtime_cannot_return_tools_and_final_in_same_step(
    session_factory,
):
    class BypassRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            return {
                "tool_calls": [{"capability": "activity.summary"}],
                "draft": {
                    "status": "completed",
                    "answer": "This output tries to bypass the loop.",
                },
                "metadata": self.metadata.model_dump(mode="json"),
            }

    provider = StubProvider(domain="activity")
    result = await _agent(
        session_factory,
        providers=(provider,),
        runtime=BypassRuntime(),
        policy=_policy("activity"),
    ).ask(_request())

    assert result.draft.status is DecisionStatus.FAILED
    assert result.draft.limitations == [
        "runtime_contract_violation"
    ]
    assert provider.calls == []


async def test_malformed_tool_arguments_fail_explicitly(session_factory):
    class MalformedRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            return RuntimeStepOutput(
                tool_calls=({"limit": "many"},),
                metadata=self.metadata,
            )

    agent = _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=MalformedRuntime(),
        policy=_policy("activity"),
    )

    result = await agent.ask(_request())

    assert result.draft.status is DecisionStatus.FAILED
    assert result.draft.limitations == ["malformed_tool_arguments"]


async def test_normalized_duplicate_parameter_keys_are_malformed(
    session_factory,
):
    class DuplicateParameterRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            return RuntimeStepOutput(
                tool_calls=(
                    {
                        "capability": "activity.summary",
                        "parameters": {
                            "Lookback": 1,
                            "lookback": 2,
                        },
                    },
                ),
                metadata=self.metadata,
            )

    agent = _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=DuplicateParameterRuntime(),
        policy=_policy("activity"),
    )

    result = await agent.ask(_request())

    assert result.draft.status is DecisionStatus.FAILED
    assert result.draft.limitations == ["malformed_tool_arguments"]


async def test_invalid_access_policy_fails_before_runtime_execution(
    session_factory,
):
    runtime = CapturingRuntime()
    registry = ContextProviderRegistry(
        (StubProvider(domain="activity"),)
    )
    agent = HealthMesDecisionAgent(
        access_layer=ContextAccessLayer(
            registry,
            clock=lambda: NOW,
        ),
        runtime=runtime,
        session_factory=session_factory,
        policy_resolver=lambda request: object(),
        clock=lambda: NOW,
    )

    result = await agent.ask(_request())

    assert result.draft.status is DecisionStatus.FAILED
    assert result.draft.limitations == [
        "access_policy_resolution_failed"
    ]
    assert runtime.turns == []


async def test_request_validation_respects_whole_turn_deadline(
    session_factory,
):
    class SlowCopy:
        def __deepcopy__(self, memo):
            del memo
            time.sleep(0.2)
            return "Why was validation delayed?"

    request = _request()
    invalid_copy = request.model_copy(
        update={"question": SlowCopy()}
    )
    agent = _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=CapturingRuntime(),
        policy=_policy("activity"),
        timeout_seconds=0.01,
    )
    started = time.monotonic()

    with pytest.warns(UserWarning, match="Pydantic serializer warnings"):
        result = await agent.ask(invalid_copy)
    elapsed = time.monotonic() - started

    assert result.draft.limitations == ["runtime_timeout"]
    assert result.request_id != request.request_id
    assert result.turn_id != request.turn_id
    assert elapsed < 0.15
    await asyncio.sleep(0.25)
    agent.close()


async def test_sync_policy_resolution_respects_whole_turn_deadline(
    session_factory,
):
    request = _request()
    runtime = CapturingRuntime()

    def slow_policy_resolver(validated_request):
        assert validated_request == request
        time.sleep(0.2)
        return _policy("activity")

    agent = HealthMesDecisionAgent(
        access_layer=ContextAccessLayer(
            ContextProviderRegistry(
                (StubProvider(domain="activity"),)
            ),
            clock=lambda: NOW,
        ),
        runtime=runtime,
        session_factory=session_factory,
        policy_resolver=slow_policy_resolver,
        timeout_seconds=0.01,
        clock=lambda: NOW,
    )
    started = time.monotonic()

    result = await agent.ask(request)
    elapsed = time.monotonic() - started

    assert result.draft.limitations == ["runtime_timeout"]
    assert result.request_id == request.request_id
    assert result.turn_id == request.turn_id
    assert elapsed < 0.15
    await asyncio.sleep(0.25)
    assert runtime.turns == []
    agent.close()


async def test_sync_catalog_preparation_respects_whole_turn_deadline(
    session_factory,
    monkeypatch,
):
    original_tool_catalog = decision_agent_module._tool_catalog

    def slow_tool_catalog(*args, **kwargs):
        time.sleep(0.2)
        return original_tool_catalog(*args, **kwargs)

    monkeypatch.setattr(
        decision_agent_module,
        "_tool_catalog",
        slow_tool_catalog,
    )
    request = _request()
    runtime = CapturingRuntime()
    agent = _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=runtime,
        policy=_policy("activity"),
        timeout_seconds=0.01,
    )
    started = time.monotonic()

    result = await agent.ask(request)
    elapsed = time.monotonic() - started

    assert result.draft.limitations == ["runtime_timeout"]
    assert result.request_id == request.request_id
    assert result.turn_id == request.turn_id
    assert elapsed < 0.15
    await asyncio.sleep(0.25)
    assert runtime.turns == []
    agent.close()


async def test_expired_deadline_after_session_startup_blocks_provider_io(
    session_factory,
):
    provider = StubProvider(domain="activity")

    class ToolRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            return RuntimeStepOutput(
                tool_calls=({"capability": "activity.summary"},),
                metadata=self.metadata,
            )

    @contextmanager
    def slow_session_factory():
        time.sleep(0.08)
        with session_factory() as session:
            yield session

    agent = HealthMesDecisionAgent(
        access_layer=ContextAccessLayer(
            ContextProviderRegistry((provider,)),
            clock=lambda: NOW,
        ),
        runtime=ToolRuntime(),
        session_factory=slow_session_factory,
        policy_resolver=lambda request: _policy("activity"),
        timeout_seconds=0.01,
        clock=lambda: NOW,
    )

    result = await agent.ask(_request())

    assert result.draft.limitations == ["runtime_timeout"]
    await asyncio.sleep(0.15)
    assert provider.calls == []
    agent.close()


async def test_expired_deadline_after_access_preflight_blocks_provider_io(
    session_factory,
    monkeypatch,
):
    provider = StubProvider(domain="activity")
    original_preflight = (
        decision_agent_module.ContextAccessTurn._preflight
    )

    def slow_preflight(*args, **kwargs):
        time.sleep(0.08)
        return original_preflight(*args, **kwargs)

    monkeypatch.setattr(
        decision_agent_module.ContextAccessTurn,
        "_preflight",
        slow_preflight,
    )

    class ToolRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            return RuntimeStepOutput(
                tool_calls=({"capability": "activity.summary"},),
                metadata=self.metadata,
            )

    agent = _agent(
        session_factory,
        providers=(provider,),
        runtime=ToolRuntime(),
        policy=_policy("activity"),
        timeout_seconds=0.01,
    )

    result = await agent.ask(_request())

    assert result.draft.limitations == ["runtime_timeout"]
    await asyncio.sleep(0.15)
    assert provider.calls == []
    agent.close()


async def test_worker_thread_creation_happens_before_request_deadline(
    session_factory,
    monkeypatch,
):
    original_start = threading.Thread.start

    def delayed_start(thread):
        if thread.name == "healthmes-decision-worker":
            time.sleep(0.2)
        return original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", delayed_start)
    construction_started = time.monotonic()
    agent = _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=CapturingRuntime(),
        policy=_policy("activity"),
        timeout_seconds=0.01,
    )
    construction_elapsed = time.monotonic() - construction_started
    assert agent._worker._ready.wait(1)
    ask_started = time.monotonic()

    result = await agent.ask(_request())
    ask_elapsed = time.monotonic() - ask_started

    assert construction_elapsed >= 0.18
    assert result.draft.status is DecisionStatus.NEEDS_CLARIFICATION
    assert ask_elapsed < 0.15
    agent.close()


async def test_worker_loop_startup_happens_before_request_deadline(
    session_factory,
    monkeypatch,
):
    original_thread_main = (
        decision_agent_module._IsolatedAsyncWorker._thread_main
    )

    def delayed_thread_main(worker):
        time.sleep(0.2)
        original_thread_main(worker)

    monkeypatch.setattr(
        decision_agent_module._IsolatedAsyncWorker,
        "_thread_main",
        delayed_thread_main,
    )
    construction_started = time.monotonic()
    agent = _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=CapturingRuntime(),
        policy=_policy("activity"),
        timeout_seconds=0.01,
    )
    construction_elapsed = time.monotonic() - construction_started
    ask_started = time.monotonic()

    result = await agent.ask(_request())
    ask_elapsed = time.monotonic() - ask_started

    assert construction_elapsed >= 0.18
    assert result.draft.status is DecisionStatus.NEEDS_CLARIFICATION
    assert ask_elapsed < 0.15
    agent.close()


async def test_runtime_timeout_and_contract_failure_are_explicit(
    session_factory,
):
    class SlowRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            await asyncio.sleep(1)

    timeout_agent = _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=SlowRuntime(),
        policy=_policy("activity"),
        timeout_seconds=0.05,
    )

    timeout = await timeout_agent.ask(_request())

    assert timeout.draft.limitations == ["runtime_timeout"]
    assert timeout.steps_used == 1

    class SelfReportingRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            return {
                "draft": {
                    "status": "completed",
                    "answer": "The runtime cannot report its own steps.",
                },
                "metadata": self.metadata.model_dump(mode="json"),
                "steps_used": 1,
            }

    malformed_agent = _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=SelfReportingRuntime(),
        policy=_policy("activity"),
    )
    malformed = await malformed_agent.ask(_request())

    assert malformed.draft.limitations == [
        "runtime_contract_violation"
    ]


async def test_nested_model_copy_runtime_output_is_revalidated(
    session_factory,
):
    valid_draft = DecisionDraft(
        status=DecisionStatus.COMPLETED,
        answer="This initially satisfies the contract.",
    )
    invalid_draft = valid_draft.model_copy(
        update={"status": DecisionStatus.NEEDS_CLARIFICATION}
    )

    class InvalidNestedRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            valid_output = RuntimeStepOutput(
                draft=valid_draft,
                metadata=self.metadata,
            )
            return valid_output.model_copy(
                update={"draft": invalid_draft}
            )

    result = await _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=InvalidNestedRuntime(),
        policy=_policy("activity"),
    ).ask(_request())

    assert result.draft.status is DecisionStatus.FAILED
    assert result.draft.limitations == [
        "runtime_contract_violation"
    ]


async def test_runtime_output_validation_respects_wall_clock_deadline(
    session_factory,
):
    class SlowCopy:
        def __deepcopy__(self, memo):
            del memo
            time.sleep(0.2)
            return "copied"

    class SlowValidationRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            valid_output = RuntimeStepOutput(
                tool_calls=({"capability": "activity.summary"},),
                metadata=self.metadata,
            )
            return valid_output.model_copy(
                update={
                    "tool_calls": (
                        {
                            "capability": "activity.summary",
                            "parameters": {"slow": SlowCopy()},
                        },
                    )
                }
            )

    agent = _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=SlowValidationRuntime(),
        policy=_policy("activity"),
        timeout_seconds=0.01,
    )
    started = time.monotonic()

    result = await agent.ask(_request())
    elapsed = time.monotonic() - started

    assert result.draft.limitations == ["runtime_timeout"]
    assert elapsed < 0.15
    await asyncio.sleep(0.25)
    agent.close()


async def test_sync_blocking_runtime_respects_wall_clock_deadline(
    session_factory,
):
    class SyncBlockingRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            time.sleep(0.2)
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer="This result arrived after the deadline.",
                ),
                metadata=self.metadata,
            )

    started = time.monotonic()
    result = await _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=SyncBlockingRuntime(),
        policy=_policy("activity"),
        timeout_seconds=0.01,
    ).ask(_request())
    elapsed = time.monotonic() - started

    assert result.draft.limitations == ["runtime_timeout"]
    assert elapsed < 0.15
    await asyncio.sleep(0.25)


async def test_timed_out_worker_is_bounded_and_future_calls_fail_fast(
    session_factory,
):
    release = threading.Event()
    started = threading.Event()

    class PermanentlyBlockingRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            started.set()
            release.wait()
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer="This blocked result arrived too late.",
                ),
                metadata=self.metadata,
            )

    before = sum(
        thread.name == "healthmes-decision-worker"
        for thread in threading.enumerate()
    )
    agent = _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=PermanentlyBlockingRuntime(),
        policy=_policy("activity"),
        timeout_seconds=0.01,
    )

    first = await agent.ask(_request())
    assert started.is_set()
    second_started = time.monotonic()
    second = await agent.ask(
        _request().model_copy(
            update={
                "request_id": uuid.uuid4(),
                "turn_id": uuid.uuid4(),
            }
        )
    )
    third = await agent.ask(
        _request().model_copy(
            update={
                "request_id": uuid.uuid4(),
                "turn_id": uuid.uuid4(),
            }
        )
    )
    fail_fast_elapsed = time.monotonic() - second_started
    after = sum(
        thread.name == "healthmes-decision-worker"
        for thread in threading.enumerate()
    )

    assert first.draft.limitations == ["runtime_timeout"]
    assert second.draft.limitations == ["runtime_worker_unavailable"]
    assert third.draft.limitations == ["runtime_worker_unavailable"]
    assert fail_fast_elapsed < 0.1
    assert after <= before + 1

    release.set()
    await asyncio.sleep(0.05)
    agent.close()


async def test_queued_request_detects_stuck_timed_out_turn(
    session_factory,
    monkeypatch,
):
    release = threading.Event()
    runtime_started = threading.Event()
    second_submitted = threading.Event()

    class BlockingRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            runtime_started.set()
            release.wait()
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer="This blocked result arrived too late.",
                ),
                metadata=self.metadata,
            )

    agent = _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=BlockingRuntime(),
        policy=_policy("activity"),
        timeout_seconds=0.3,
    )
    original_submit = agent._worker._submit
    submit_count = 0

    def tracking_submit(*args, **kwargs):
        nonlocal submit_count
        submit_count += 1
        if submit_count == 2:
            second_submitted.set()
        return original_submit(*args, **kwargs)

    monkeypatch.setattr(agent._worker, "_submit", tracking_submit)
    first_task = asyncio.create_task(agent.ask(_request()))
    assert await asyncio.to_thread(runtime_started.wait, 1)
    await asyncio.sleep(0.2)
    second_started = time.monotonic()
    second_task = asyncio.create_task(
        agent.ask(
            _request().model_copy(
                update={
                    "request_id": uuid.uuid4(),
                    "turn_id": uuid.uuid4(),
                }
            )
        )
    )
    assert await asyncio.to_thread(second_submitted.wait, 1)

    first, second = await asyncio.gather(first_task, second_task)
    second_elapsed = time.monotonic() - second_started
    release.set()
    await asyncio.sleep(0.05)
    agent.close()

    assert first.draft.limitations == ["runtime_timeout"]
    assert second.draft.limitations == ["runtime_worker_unavailable"]
    assert second_elapsed < 0.25


async def test_runtime_timeout_wins_when_cancellation_is_suppressed(
    session_factory,
):
    cancelled = threading.Event()

    class SuppressingRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                return RuntimeStepOutput(
                    draft=DecisionDraft(
                        status=DecisionStatus.COMPLETED,
                        answer="This late result must be discarded.",
                    ),
                    metadata=self.metadata,
                )

    result = await _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=SuppressingRuntime(),
        policy=_policy("activity"),
        timeout_seconds=0.01,
    ).ask(_request())

    assert await asyncio.to_thread(cancelled.wait, 1)
    assert result.draft.status is DecisionStatus.FAILED
    assert result.draft.limitations == ["runtime_timeout"]
    assert result.tool_trace == ()


async def test_cooperatively_cancelled_timeout_keeps_worker_reusable(
    session_factory,
):
    cancelled = threading.Event()

    class CooperativeRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        def __init__(self) -> None:
            self.calls = 0

        async def next_step(self, turn):
            self.calls += 1
            if self.calls == 1:
                try:
                    await asyncio.sleep(10)
                finally:
                    cancelled.set()
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer="The worker accepted another healthy turn.",
                ),
                metadata=self.metadata,
            )

    agent = _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=CooperativeRuntime(),
        policy=_policy("activity"),
        timeout_seconds=0.05,
    )

    first = await agent.ask(_request())
    assert await asyncio.to_thread(cancelled.wait, 1)
    second = await agent.ask(
        _request().model_copy(
            update={
                "request_id": uuid.uuid4(),
                "turn_id": uuid.uuid4(),
            }
        )
    )

    assert first.draft.limitations == ["runtime_timeout"]
    assert second.draft.status is DecisionStatus.COMPLETED
    agent.close()


async def test_timed_out_runtime_cannot_mutate_canonical_history(
    session_factory,
):
    cancelled = asyncio.Event()

    class MutatingRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            if not turn.history:
                return RuntimeStepOutput(
                    tool_calls=({"capability": "activity.summary"},),
                    metadata=self.metadata,
                )
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                turn.history[0].results[0].payload["value"] = 999
                cancelled.set()
                return RuntimeStepOutput(
                    draft=DecisionDraft(
                        status=DecisionStatus.COMPLETED,
                        answer="This late mutation must be isolated.",
                    ),
                    metadata=self.metadata,
                )

    result = await _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=MutatingRuntime(),
        policy=_policy("activity"),
        timeout_seconds=0.02,
    ).ask(_request())

    await asyncio.wait_for(cancelled.wait(), timeout=1)
    assert result.draft.limitations == ["runtime_timeout"]
    assert result.tool_trace[0].result is not None
    assert result.tool_trace[0].result.payload["value"] == 1


async def test_runtime_owned_output_is_copied_before_return(
    session_factory,
):
    class RetainingRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        def __init__(self) -> None:
            self.output = RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer="The returned draft must be detached.",
                ),
                metadata=self.metadata,
            )

        async def next_step(self, turn):
            return self.output

    runtime = RetainingRuntime()
    result = await _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=runtime,
        policy=_policy("activity"),
    ).ask(_request())

    assert runtime.output.draft is not None
    runtime.output.draft.limitations.append("late_runtime_mutation")

    assert result.draft.limitations == []


async def test_concurrent_requests_serialize_the_shared_runtime(
    session_factory,
):
    class StatefulRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        def __init__(self) -> None:
            self.current_question: str | None = None

        async def next_step(self, turn):
            self.current_question = turn.request.question
            await asyncio.sleep(0.02)
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer=self.current_question,
                ),
                metadata=self.metadata,
            )

    agent = _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=StatefulRuntime(),
        policy=_policy("activity"),
    )
    request_a = _request(question="QUESTION_A")
    request_b = _request(question="QUESTION_B")

    result_a, result_b = await asyncio.gather(
        agent.ask(request_a),
        agent.ask(request_b),
    )

    assert result_a.draft.answer == "QUESTION_A"
    assert result_b.draft.answer == "QUESTION_B"
    agent.close()


async def test_runtime_cancellation_propagates(session_factory):
    started = threading.Event()

    class BlockingRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        def __init__(self) -> None:
            self.calls = 0

        async def next_step(self, turn):
            self.calls += 1
            if self.calls == 1:
                started.set()
                await asyncio.sleep(10)
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer="The worker remained available after cancellation.",
                ),
                metadata=self.metadata,
            )

    agent = _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=BlockingRuntime(),
        policy=_policy("activity"),
    )
    task = asyncio.create_task(agent.ask(_request()))
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    second = await agent.ask(
        _request().model_copy(
            update={
                "request_id": uuid.uuid4(),
                "turn_id": uuid.uuid4(),
            }
        )
    )

    assert second.draft.status is DecisionStatus.COMPLETED
    agent.close()


async def test_close_during_active_turn_preserves_completed_result(
    session_factory,
):
    started = threading.Event()
    release = threading.Event()

    class ClosingRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            started.set()
            release.wait()
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer="The active turn completed before shutdown.",
                ),
                metadata=self.metadata,
            )

    agent = _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=ClosingRuntime(),
        policy=_policy("activity"),
        timeout_seconds=1,
    )
    task = asyncio.create_task(agent.ask(_request()))
    assert await asyncio.to_thread(started.wait, 1)

    agent.close()
    release.set()
    result = await task

    assert result.draft.status is DecisionStatus.COMPLETED
    assert result.draft.answer == (
        "The active turn completed before shutdown."
    )


async def test_completed_worker_turn_cannot_leave_stale_cancel_marker(
    monkeypatch,
):
    worker = decision_agent_module._IsolatedAsyncWorker()

    async def cancellation_after_worker_completion(future, *, deadline):
        del deadline
        while not future.done():
            await asyncio.sleep(0)
        raise asyncio.CancelledError

    monkeypatch.setattr(
        decision_agent_module,
        "_await_before_deadline",
        cancellation_after_worker_completion,
    )

    with pytest.raises(asyncio.CancelledError):
        await worker.run(
            lambda: asyncio.sleep(0, result="completed"),
            deadline=time.monotonic() + 1,
        )

    assert not worker._cancel_pending.is_set()
    worker.ensure_available()
    worker.close()
    await asyncio.sleep(0.01)


async def test_stuck_cancelled_turn_is_quarantined_after_next_timeout(
    session_factory,
):
    started = threading.Event()
    release = threading.Event()

    class BlockingRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            started.set()
            release.wait()
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer="This cancelled turn must not be reused.",
                ),
                metadata=self.metadata,
            )

    agent = _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=BlockingRuntime(),
        policy=_policy("activity"),
        timeout_seconds=0.02,
    )
    first_task = asyncio.create_task(agent.ask(_request()))
    assert await asyncio.to_thread(started.wait, 1)
    first_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await first_task

    second = await agent.ask(
        _request().model_copy(
            update={
                "request_id": uuid.uuid4(),
                "turn_id": uuid.uuid4(),
            }
        )
    )
    third = await agent.ask(
        _request().model_copy(
            update={
                "request_id": uuid.uuid4(),
                "turn_id": uuid.uuid4(),
            }
        )
    )

    assert second.draft.limitations == ["runtime_timeout"]
    assert third.draft.limitations == ["runtime_worker_unavailable"]

    release.set()
    await asyncio.sleep(0.05)
    agent.close()


async def test_slow_provider_cannot_append_late_result_to_returned_run(
    session_factory,
):
    class SuppressingProvider(StubProvider):
        def __init__(self) -> None:
            super().__init__(domain="activity")
            self.cancelled = asyncio.Event()
            self.finished = asyncio.Event()

        async def query(self, session, query, *, now):
            self.calls.append(query)
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                self.cancelled.set()
            finally:
                self.finished.set()
            return ContextResult(
                query_id=query.query_id,
                provider_id=query.provider_id,
                capability=query.capability,
                status=ContextStatus.OK,
                payload={"status": "late", "value": 1},
            )

    class ToolRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            return RuntimeStepOutput(
                tool_calls=({"capability": "activity.summary"},),
                metadata=self.metadata,
            )

    provider = SuppressingProvider()
    result = await _agent(
        session_factory,
        providers=(provider,),
        runtime=ToolRuntime(),
        policy=_policy("activity"),
        timeout_seconds=0.01,
    ).ask(_request())

    await asyncio.wait_for(provider.finished.wait(), timeout=1)
    assert provider.cancelled.is_set()
    assert result.draft.limitations == ["runtime_timeout"]
    assert result.tool_trace == ()
    assert result.access_trace == ()


async def test_sync_blocking_provider_respects_wall_clock_deadline(
    session_factory,
):
    class SyncBlockingProvider(StubProvider):
        async def query(self, session, query, *, now):
            self.calls.append(query)
            time.sleep(0.2)
            return ContextResult(
                query_id=query.query_id,
                provider_id=query.provider_id,
                capability=query.capability,
                status=ContextStatus.OK,
                payload={"status": "late", "value": 1},
            )

    class ToolRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            return RuntimeStepOutput(
                tool_calls=({"capability": "activity.summary"},),
                metadata=self.metadata,
            )

    provider = SyncBlockingProvider(domain="activity")
    started = time.monotonic()
    result = await _agent(
        session_factory,
        providers=(provider,),
        runtime=ToolRuntime(),
        policy=_policy("activity"),
        timeout_seconds=0.02,
    ).ask(_request())
    elapsed = time.monotonic() - started

    assert result.draft.limitations == ["runtime_timeout"]
    assert elapsed < 0.15
    assert result.tool_trace == ()
    await asyncio.sleep(0.25)


async def test_runtime_identity_mismatch_fails_closed(session_factory):
    class SwappingRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer="This runtime changed identity.",
                ),
                metadata=RuntimeMetadata(runtime="other"),
            )

    result = await _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=SwappingRuntime(),
        policy=_policy("activity"),
    ).ask(_request())

    assert result.draft.status is DecisionStatus.FAILED
    assert result.draft.limitations == ["runtime_identity_mismatch"]


async def test_configured_runtime_model_cannot_change_mid_turn(
    session_factory,
):
    class SwappingModelRuntime:
        metadata = RuntimeMetadata(
            runtime="scripted",
            model="expected-v1",
        )

        async def next_step(self, turn):
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer="This output came from an unexpected model.",
                ),
                metadata=RuntimeMetadata(
                    runtime="scripted",
                    model="spoofed-v2",
                ),
            )

    result = await _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=SwappingModelRuntime(),
        policy=_policy("activity"),
    ).ask(_request())

    assert result.draft.status is DecisionStatus.FAILED
    assert result.draft.limitations == ["runtime_identity_mismatch"]
    assert result.runtime.model == "expected-v1"


async def test_configured_runtime_provider_cannot_change_mid_turn(
    session_factory,
):
    class SwappingProviderRuntime:
        metadata = RuntimeMetadata(
            runtime="scripted",
            model="expected-v1",
            provider="expected-provider",
        )

        async def next_step(self, turn):
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.COMPLETED,
                    answer="This output came from an unexpected provider.",
                ),
                metadata=RuntimeMetadata(
                    runtime="scripted",
                    model="expected-v1",
                    provider="spoofed-provider",
                ),
            )

    result = await _agent(
        session_factory,
        providers=(StubProvider(domain="activity"),),
        runtime=SwappingProviderRuntime(),
        policy=_policy("activity"),
    ).ask(_request())

    assert result.draft.status is DecisionStatus.FAILED
    assert result.draft.limitations == ["runtime_identity_mismatch"]
    assert result.runtime.provider == "expected-provider"


async def test_missing_context_can_produce_clarification_not_zero(
    session_factory,
):
    def no_data(query, now):
        return ContextResult(
            query_id=query.query_id,
            provider_id=query.provider_id,
            capability=query.capability,
            status=ContextStatus.PARTIAL,
            payload={"status": "insufficient_data"},
            freshness=ContextFreshness(
                status=FreshnessStatus.UNKNOWN
            ),
            coverage=ContextCoverage(
                status=CoverageStatus.UNKNOWN
            ),
            limitations=["provider_contract_violation"],
        )

    class ClarifyingRuntime:
        metadata = RuntimeMetadata(runtime="scripted")

        async def next_step(self, turn):
            if not turn.history:
                return RuntimeStepOutput(
                    tool_calls=({"capability": "nutrition.summary"},),
                    metadata=self.metadata,
                )
            context = turn.history[0].results[0]
            assert context.payload == {
                "status": "insufficient_data"
            }
            return RuntimeStepOutput(
                draft=DecisionDraft(
                    status=DecisionStatus.NEEDS_CLARIFICATION,
                    clarification_question=(
                        "What amount and serving size are you considering?"
                    ),
                    uncertainty="The candidate amount is unknown.",
                ),
                metadata=self.metadata,
            )

    agent = _agent(
        session_factory,
        providers=(
            StubProvider(
                domain="nutrition",
                result_factory=no_data,
            ),
        ),
        runtime=ClarifyingRuntime(),
        policy=_policy("nutrition"),
    )

    result = await agent.ask(_request())

    assert result.draft.status is DecisionStatus.NEEDS_CLARIFICATION
    assert "amount" in result.draft.clarification_question
    assert "0" not in result.draft.model_dump_json()
