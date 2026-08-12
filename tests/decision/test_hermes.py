from __future__ import annotations

import ast
import asyncio
import copy
import inspect
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from healthmes.decision import (
    HERMES_MODEL_ITERATION_CONTRACT,
    ContextParameterFormat,
    ContextParameterSpec,
    ContextParameterType,
    DecisionDraft,
    DecisionRuntime,
    DecisionRuntimeContractError,
    DecisionRuntimeTurn,
    DecisionRuntimeUnavailableError,
    DecisionStatus,
    DecisionToolSpec,
    HermesHttpIterationTransport,
    HermesModelIterationRequest,
    HermesRuntimeAdapter,
    HermesRuntimeCapability,
    PrivacyLevel,
    ProvenanceSupport,
    RuntimeDecisionRequest,
    RuntimeMetadata,
    RuntimeResourceBudget,
)

NOW = datetime(2026, 8, 11, 9, tzinfo=UTC)
REQUEST_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
TURN_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
PROVIDER = "test-provider"


def _capabilities(
    *,
    skills_api: bool = True,
    endpoint_overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    endpoint = {
        "method": "POST",
        "path": "/v1/model/iterations",
        "contract": HERMES_MODEL_ITERATION_CONTRACT,
        "max_model_calls": 1,
        "tool_execution": "caller",
        "session_mutation": False,
        "supports": {
            "system_policy": True,
            "tool_allowlist": True,
            "conversation_snapshot": True,
            "structured_output": True,
            "usage": True,
            "external_deadline": True,
        },
    }
    endpoint.update(endpoint_overrides or {})
    return {
        "object": "hermes.api_server.capabilities",
        "platform": "hermes-agent",
        "model": "test-model",
        "runtime": {
            "mode": "split_runtime",
            "tool_execution": "caller",
            "split_runtime": True,
        },
        "features": {
            "model_iteration": True,
            "skills_api": skills_api,
        },
        "endpoints": {"model_iteration": endpoint},
    }


def _tool() -> DecisionToolSpec:
    return DecisionToolSpec(
        capability="activity.summary",
        provider_id="activity",
        domain="activity",
        description="Summarize device activity for a bounded period.",
        granularities=("summary", "daily"),
        query_fields=("start", "end", "timezone", "fields", "limit"),
        output_fields=("active_seconds", "break_count"),
        parameters=("include_private", "record_ref"),
        parameter_specs=(
            ContextParameterSpec(
                name="include_private",
                value_type=ContextParameterType.BOOLEAN,
            ),
            ContextParameterSpec(
                name="record_ref",
                value_type=ContextParameterType.STRING,
                max_length=36,
                format=ContextParameterFormat.RELATED_RECORD_REF,
            ),
        ),
        privacy_levels=(
            PrivacyLevel.AGGREGATE,
            PrivacyLevel.IDENTITY,
        ),
        max_lookback_days=30,
        max_rows=100,
        provenance=ProvenanceSupport.STABLE,
        freshness_expectation="Current through the last completed import.",
    )


def _turn(
    *,
    tools: tuple[DecisionToolSpec, ...] | None = None,
) -> DecisionRuntimeTurn:
    return DecisionRuntimeTurn(
        request_id=REQUEST_ID,
        turn_id=TURN_ID,
        request=RuntimeDecisionRequest(
            question="Why was my focus fragmented this morning?",
            requested_at=NOW,
            timezone="Asia/Seoul",
            requested_privacy_level=PrivacyLevel.IDENTITY,
        ),
        system_policy="Mandatory HealthMes policy.",
        system_policy_version="healthmes-policy.test",
        tools=tools if tools is not None else (_tool(),),
        step_number=1,
        remaining_steps=4,
        resource_budget=RuntimeResourceBudget(
            max_tool_calls=8,
            remaining_tool_calls=7,
            max_source_refs=200,
            remaining_source_refs=198,
            max_context_bytes=256_000,
            remaining_context_bytes=250_000,
        ),
        deadline_ms=4_500,
    )


def _draft_response(
    request: HermesModelIterationRequest,
    *,
    answer: str = "Take a short break before the next focus block.",
) -> dict[str, Any]:
    return {
        "object": "hermes.model_iteration.response",
        "contract_version": HERMES_MODEL_ITERATION_CONTRACT,
        "request_id": str(request.request_id),
        "turn_id": str(request.turn_id),
        "request_fingerprint": request.request_fingerprint,
        "step_number": request.step_number,
        "finish_reason": "structured_output",
        "tool_calls": [],
        "output": DecisionDraft(
            status=DecisionStatus.COMPLETED,
            answer=answer,
        ).model_dump(mode="json"),
        "usage": {"input_tokens": 120, "output_tokens": 30},
        "model": "test-model",
        "provider": request.provider,
    }


class FakeTransport:
    def __init__(
        self,
        *,
        capabilities: Mapping[str, Any] | None = None,
        response_factory: Callable[
            [HermesModelIterationRequest],
            Mapping[str, Any],
        ]
        | None = None,
    ) -> None:
        self.capabilities = copy.deepcopy(
            capabilities if capabilities is not None else _capabilities()
        )
        self.response_factory = response_factory or _draft_response
        self.capability_calls = 0
        self.requests: list[HermesModelIterationRequest] = []
        self.endpoints: list[str] = []
        self.timeouts: list[float] = []

    async def get_capabilities(self) -> Mapping[str, Any]:
        self.capability_calls += 1
        return copy.deepcopy(self.capabilities)

    async def run_model_iteration(
        self,
        *,
        endpoint: str,
        request: HermesModelIterationRequest,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        self.endpoints.append(endpoint)
        self.requests.append(request.model_copy(deep=True))
        self.timeouts.append(timeout_seconds)
        return copy.deepcopy(self.response_factory(request))


@pytest.mark.parametrize(
    ("model", "provider"),
    [
        ("", PROVIDER),
        ("   ", PROVIDER),
        ("test-model", ""),
        ("test-model", "   "),
        (None, PROVIDER),
        ("test-model", None),
    ],
)
def test_adapter_requires_explicit_model_and_provider(model, provider):
    with pytest.raises(ValueError):
        HermesRuntimeAdapter(
            transport=FakeTransport(),
            model=model,
            provider=provider,
        )


def test_adapter_rejects_synchronous_transport_methods_before_calling():
    class SynchronousTaskTransport:
        def __init__(self) -> None:
            self.calls = 0

        def get_capabilities(self):
            self.calls += 1
            return asyncio.create_task(asyncio.sleep(60))

        async def run_model_iteration(
            self,
            *,
            endpoint,
            request,
            timeout_seconds,
        ):
            del endpoint, request, timeout_seconds
            return {}

    transport = SynchronousTaskTransport()

    with pytest.raises(
        TypeError,
        match="transport methods must be async functions",
    ):
        HermesRuntimeAdapter(
            transport=transport,
            model="test-model",
            provider=PROVIDER,
        )

    assert transport.calls == 0


def test_adapter_rejects_marked_synchronous_transport_methods():
    class MarkedSynchronousTaskTransport:
        def __init__(self) -> None:
            self.calls = 0

        def get_capabilities(self):
            self.calls += 1
            return asyncio.create_task(asyncio.sleep(60))

        get_capabilities = inspect.markcoroutinefunction(
            get_capabilities
        )

        async def run_model_iteration(
            self,
            *,
            endpoint,
            request,
            timeout_seconds,
        ):
            del endpoint, request, timeout_seconds
            return {}

    transport = MarkedSynchronousTaskTransport()

    with pytest.raises(
        TypeError,
        match="transport methods must be async functions",
    ):
        HermesRuntimeAdapter(
            transport=transport,
            model="test-model",
            provider=PROVIDER,
        )

    assert transport.calls == 0


def test_adapter_rejects_callable_with_forged_async_code_object():
    async def async_marker():
        return None

    class ForgedAsyncCallable:
        __code__ = async_marker.__code__

        def __init__(self) -> None:
            self.calls = 0

        def __call__(self):
            self.calls += 1
            return asyncio.create_task(asyncio.sleep(60))

    class ForgedTransport:
        def __init__(self) -> None:
            self.get_capabilities = ForgedAsyncCallable()

        async def run_model_iteration(
            self,
            *,
            endpoint,
            request,
            timeout_seconds,
        ):
            del endpoint, request, timeout_seconds
            return {}

    transport = ForgedTransport()

    with pytest.raises(
        TypeError,
        match="transport methods must be async functions",
    ):
        HermesRuntimeAdapter(
            transport=transport,
            model="test-model",
            provider=PROVIDER,
        )

    assert transport.get_capabilities.calls == 0


@pytest.mark.asyncio
async def test_transport_methods_are_resolved_once_without_toctou():
    class StatefulPropertyTransport:
        def __init__(self) -> None:
            self.capability_lookups = 0
            self.iteration_lookups = 0

        @property
        def get_capabilities(self):
            self.capability_lookups += 1
            if self.capability_lookups == 1:
                return self._get_capabilities
            return self._sync_get_capabilities

        @property
        def run_model_iteration(self):
            self.iteration_lookups += 1
            if self.iteration_lookups == 1:
                return self._run_model_iteration
            return self._sync_run_model_iteration

        async def _get_capabilities(self):
            return _capabilities()

        async def _run_model_iteration(
            self,
            *,
            endpoint,
            request,
            timeout_seconds,
        ):
            del endpoint, timeout_seconds
            return _draft_response(request)

        def _sync_get_capabilities(self):
            raise AssertionError("stateful property was resolved twice")

        def _sync_run_model_iteration(self, **kwargs):
            del kwargs
            raise AssertionError("stateful property was resolved twice")

    transport = StatefulPropertyTransport()
    adapter = HermesRuntimeAdapter(
        transport=transport,
        model="test-model",
        provider=PROVIDER,
    )

    result = await adapter.next_step(_turn())

    assert result.draft is not None
    assert transport.capability_lookups == 1
    assert transport.iteration_lookups == 1


@pytest.mark.asyncio
async def test_current_server_agent_capability_is_explicitly_unavailable():
    transport = FakeTransport(
        capabilities={
            "object": "hermes.api_server.capabilities",
            "model": "vendor-model",
            "runtime": {
                "mode": "server_agent",
                "tool_execution": "server",
                "split_runtime": False,
            },
            "features": {
                "chat_completions": True,
                "responses_api": True,
                "skills_api": True,
            },
            "endpoints": {
                "chat_completions": {
                    "method": "POST",
                    "path": "/v1/chat/completions",
                }
            },
        }
    )
    adapter = HermesRuntimeAdapter(
        transport=transport,
        model="test-model",
        provider=PROVIDER,
    )

    status = await adapter.capability_status()

    assert status.available is False
    assert status.endpoint is None
    assert status.reason_codes == (
        "hermes_single_iteration_not_advertised",
    )
    with pytest.raises(
        DecisionRuntimeUnavailableError,
        match="hermes_single_iteration_not_advertised",
    ):
        await adapter.next_step(_turn())
    assert transport.requests == []


@pytest.mark.asyncio
async def test_adapter_propagates_policy_allowlist_ids_privacy_and_budgets():
    def response(request: HermesModelIterationRequest) -> Mapping[str, Any]:
        return {
            "object": "hermes.model_iteration.response",
            "contract_version": HERMES_MODEL_ITERATION_CONTRACT,
            "request_id": str(request.request_id),
            "turn_id": str(request.turn_id),
            "request_fingerprint": request.request_fingerprint,
            "step_number": request.step_number,
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "call_id": "call_activity_1",
                    "name": request.allowed_tools[0],
                    "arguments": {
                        "granularity": "daily",
                        "privacy_level": "identity",
                        "limit": 20,
                        "parameters": {
                            "include_private": False,
                            "record_ref": "rr_0123456789abcdef",
                        },
                        "purpose": "Check focus fragmentation.",
                    },
                }
            ],
            "output": None,
            "usage": {"input_tokens": 300, "output_tokens": 50},
            "model": "test-model",
            "provider": request.provider,
        }

    transport = FakeTransport(response_factory=response)
    adapter = HermesRuntimeAdapter(
        transport=transport,
        model="test-model",
        provider=PROVIDER,
    )
    turn = _turn()

    result = await adapter.next_step(turn)

    assert isinstance(adapter, DecisionRuntime)
    assert result.draft is None
    assert result.tool_calls[0]["capability"] == "activity.summary"
    assert result.tool_calls[0]["privacy_level"] is PrivacyLevel.IDENTITY
    assert result.metadata == RuntimeMetadata(
        runtime="hermes",
        model="test-model",
        provider=PROVIDER,
        input_tokens=300,
        output_tokens=50,
    )
    assert transport.endpoints == ["/v1/model/iterations"]
    assert 0 < transport.timeouts[0] <= 4.5

    sent = transport.requests[0]
    assert sent.request_id == REQUEST_ID
    assert sent.turn_id == TURN_ID
    assert sent.system_policy == turn.system_policy
    assert sent.system_policy_version == turn.system_policy_version
    assert sent.privacy_scope is PrivacyLevel.IDENTITY
    assert sent.resource_budget == turn.resource_budget
    assert 1 <= sent.deadline_ms <= turn.deadline_ms
    assert sent.model == "test-model"
    assert sent.provider == PROVIDER
    assert sent.turn_snapshot.request == turn.request
    assert sent.turn_snapshot.history == turn.history
    assert sent.allowed_tools == tuple(tool.name for tool in sent.tools)
    assert len(sent.allowed_tools) == 1
    assert sent.tools[0].metadata["capability"] == "activity.summary"
    assert "capability" not in sent.tools[0].input_schema["properties"]
    parameter_schema = sent.tools[0].input_schema["properties"][
        "parameters"
    ]
    assert parameter_schema["properties"]["record_ref"]["pattern"] == (
        r"^rr_[0-9a-f]{16}$"
    )


@pytest.mark.asyncio
async def test_adapter_converts_structured_draft_and_usage():
    transport = FakeTransport()
    adapter = HermesRuntimeAdapter(
        transport=transport,
        model="test-model",
        provider=PROVIDER,
    )

    result = await adapter.next_step(_turn(tools=()))

    assert result.tool_calls == ()
    assert result.draft is not None
    assert result.draft.status is DecisionStatus.COMPLETED
    assert result.draft.answer == (
        "Take a short break before the next focus block."
    )
    assert result.metadata.input_tokens == 120
    assert result.metadata.output_tokens == 30
    assert transport.requests[0].allowed_tools == ()


@pytest.mark.parametrize(
    "coercion",
    [
        "step_number_bool",
        "input_tokens_bool",
        "output_tokens_string",
        "proposed_action_string",
        "confidence_string",
        "tool_limit_string",
        "missing_provider",
        "non_json_scalar",
    ],
)
@pytest.mark.asyncio
async def test_adapter_rejects_json_scalar_coercion_and_missing_identity(
    coercion,
):
    def response(request: HermesModelIterationRequest) -> Mapping[str, Any]:
        payload = _draft_response(request)
        if coercion == "step_number_bool":
            payload["step_number"] = True
        elif coercion == "input_tokens_bool":
            payload["usage"]["input_tokens"] = True
        elif coercion == "output_tokens_string":
            payload["usage"]["output_tokens"] = "3"
        elif coercion == "proposed_action_string":
            payload["output"]["proposed_action"] = "false"
        elif coercion == "confidence_string":
            payload["output"]["confidence"] = "0.5"
        elif coercion == "tool_limit_string":
            payload.update(
                finish_reason="tool_calls",
                tool_calls=[
                    {
                        "call_id": "call_coerced_limit",
                        "name": request.allowed_tools[0],
                        "arguments": {"limit": "7"},
                    }
                ],
                output=None,
            )
        elif coercion == "missing_provider":
            payload.pop("provider")
        elif coercion == "non_json_scalar":
            payload["output"]["answer"] = object()
        else:
            raise AssertionError(f"unsupported coercion case: {coercion}")
        return payload

    adapter = HermesRuntimeAdapter(
        transport=FakeTransport(response_factory=response),
        model="test-model",
        provider=PROVIDER,
    )

    expected_error = (
        DecisionRuntimeUnavailableError
        if coercion == "non_json_scalar"
        else DecisionRuntimeContractError
    )
    expected_code = (
        "hermes_transport_contract_invalid"
        if coercion == "non_json_scalar"
        else "hermes_response_contract_invalid"
    )
    with pytest.raises(expected_error, match=expected_code):
        await adapter.next_step(_turn())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (
            lambda payload, request: payload.update(
                request_id=str(uuid.uuid4())
            ),
            "hermes_response_correlation_mismatch",
        ),
        (
            lambda payload, request: payload.update(
                model="forged-model"
            ),
            "hermes_response_model_mismatch",
        ),
        (
            lambda payload, request: payload.update(
                request_fingerprint="0" * 64
            ),
            "hermes_response_fingerprint_mismatch",
        ),
        (
            lambda payload, request: payload.update(
                tool_results=[{"name": request.allowed_tools[0]}]
            ),
            "hermes_response_contract_invalid",
        ),
        (
            lambda payload, request: payload.update(
                finish_reason="tool_calls",
                tool_calls=[
                    {
                        "name": request.allowed_tools[0],
                        "arguments": {},
                    }
                ],
                output=None,
            ),
            "hermes_response_contract_invalid",
        ),
    ],
)
async def test_adapter_rejects_mismatched_or_forged_response_envelopes(
    mutate,
    expected_code,
):
    def response(request: HermesModelIterationRequest) -> Mapping[str, Any]:
        payload = _draft_response(request)
        mutate(payload, request)
        return payload

    adapter = HermesRuntimeAdapter(
        transport=FakeTransport(response_factory=response),
        model="test-model",
        provider=PROVIDER,
    )

    with pytest.raises(
        DecisionRuntimeContractError,
        match=expected_code,
    ):
        await adapter.next_step(_turn())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name_factory", "arguments", "expected_code"),
    [
        (
            lambda request: "unknown_tool",
            {},
            "hermes_tool_not_allowlisted",
        ),
        (
            lambda request: request.allowed_tools[0],
            {"capability": "wearable.summary"},
            "hermes_tool_capability_forged",
        ),
    ],
)
async def test_adapter_rejects_unknown_or_forged_tool_calls(
    name_factory,
    arguments,
    expected_code,
):
    def response(request: HermesModelIterationRequest) -> Mapping[str, Any]:
        return {
            "object": "hermes.model_iteration.response",
            "contract_version": HERMES_MODEL_ITERATION_CONTRACT,
            "request_id": str(request.request_id),
            "turn_id": str(request.turn_id),
            "request_fingerprint": request.request_fingerprint,
            "step_number": request.step_number,
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "call_id": "call_1",
                    "name": name_factory(request),
                    "arguments": arguments,
                }
            ],
            "output": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "model": "test-model",
            "provider": request.provider,
        }

    adapter = HermesRuntimeAdapter(
        transport=FakeTransport(response_factory=response),
        model="test-model",
        provider=PROVIDER,
    )

    with pytest.raises(
        DecisionRuntimeContractError,
        match=expected_code,
    ):
        await adapter.next_step(_turn())


@pytest.mark.asyncio
async def test_skill_discovery_flag_does_not_change_runtime_policy_request():
    transports = [
        FakeTransport(capabilities=_capabilities(skills_api=value))
        for value in (False, True)
    ]
    adapters = [
        HermesRuntimeAdapter(
            transport=transport,
            model="test-model",
            provider=PROVIDER,
        )
        for transport in transports
    ]

    await adapters[0].next_step(_turn())
    await adapters[1].next_step(_turn())

    first = transports[0].requests[0].model_dump(mode="json")
    second = transports[1].requests[0].model_dump(mode="json")
    assert first == second
    assert "skill" not in first
    assert "skills" not in first


@pytest.mark.asyncio
async def test_adapter_forwards_and_validates_provider_selection():
    def response(request: HermesModelIterationRequest) -> Mapping[str, Any]:
        payload = _draft_response(request)
        payload["provider"] = "test-provider"
        return payload

    transport = FakeTransport(response_factory=response)
    adapter = HermesRuntimeAdapter(
        transport=transport,
        model="test-model",
        provider=PROVIDER,
    )

    result = await adapter.next_step(_turn())

    assert result.draft is not None
    assert transport.requests[0].provider == PROVIDER
    assert result.metadata.provider == PROVIDER

    def mismatched_response(
        request: HermesModelIterationRequest,
    ) -> Mapping[str, Any]:
        payload = _draft_response(request)
        payload["provider"] = "other-provider"
        return payload

    mismatched = HermesRuntimeAdapter(
        transport=FakeTransport(
            response_factory=mismatched_response
        ),
        model="test-model",
        provider=PROVIDER,
    )
    with pytest.raises(
        DecisionRuntimeContractError,
        match="hermes_response_provider_mismatch",
    ):
        await mismatched.next_step(_turn())


@pytest.mark.asyncio
async def test_available_capability_is_cached_across_iterations():
    transport = FakeTransport()
    adapter = HermesRuntimeAdapter(
        transport=transport,
        model="test-model",
        provider=PROVIDER,
    )

    await adapter.next_step(_turn())
    await adapter.next_step(_turn())

    assert transport.capability_calls == 1
    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_request_fingerprint_rejects_stale_cached_response():
    class StaleResponseTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.cached_response: Mapping[str, Any] | None = None

        async def run_model_iteration(
            self,
            *,
            endpoint: str,
            request: HermesModelIterationRequest,
            timeout_seconds: float,
        ) -> Mapping[str, Any]:
            self.endpoints.append(endpoint)
            self.requests.append(request.model_copy(deep=True))
            self.timeouts.append(timeout_seconds)
            if self.cached_response is None:
                self.cached_response = _draft_response(
                    request,
                    answer="Answer for question A.",
                )
            return copy.deepcopy(self.cached_response)

    transport = StaleResponseTransport()
    adapter = HermesRuntimeAdapter(
        transport=transport,
        model="test-model",
        provider=PROVIDER,
    )
    first_turn = _turn()
    second_turn = first_turn.model_copy(
        update={
            "request": first_turn.request.model_copy(
                update={"question": "QUESTION_B"}
            )
        }
    )

    first = await adapter.next_step(first_turn)
    with pytest.raises(
        DecisionRuntimeContractError,
        match="hermes_response_fingerprint_mismatch",
    ):
        await adapter.next_step(second_turn)

    assert first.draft is not None
    assert (
        transport.requests[0].request_fingerprint
        != transport.requests[1].request_fingerprint
    )


@pytest.mark.asyncio
async def test_failed_capability_refresh_invalidates_success_cache():
    transport = FakeTransport()
    adapter = HermesRuntimeAdapter(
        transport=transport,
        model="test-model",
        provider=PROVIDER,
    )

    assert (await adapter.capability_status()).available is True
    transport.capabilities = _capabilities(
        endpoint_overrides={"tool_execution": "server"}
    )
    refreshed = await adapter.capability_status(refresh=True)

    assert refreshed.available is False
    with pytest.raises(
        DecisionRuntimeUnavailableError,
        match="hermes_single_iteration_contract_unsupported",
    ):
        await adapter.next_step(_turn())
    assert transport.requests == []
    assert transport.capability_calls == 3


@pytest.mark.asyncio
async def test_concurrent_refresh_supersedes_older_capability_probe():
    class RacingCapabilityTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.first_probe_started = asyncio.Event()
            self.release_first_probe = asyncio.Event()

        async def get_capabilities(self) -> Mapping[str, Any]:
            self.capability_calls += 1
            if self.capability_calls == 1:
                self.first_probe_started.set()
                await self.release_first_probe.wait()
                return copy.deepcopy(_capabilities())
            return copy.deepcopy(
                _capabilities(
                    endpoint_overrides={"tool_execution": "server"}
                )
            )

    transport = RacingCapabilityTransport()
    adapter = HermesRuntimeAdapter(
        transport=transport,
        model="test-model",
        provider=PROVIDER,
    )
    older_probe = asyncio.create_task(adapter.capability_status())
    await transport.first_probe_started.wait()

    refreshed = await adapter.capability_status(refresh=True)
    transport.release_first_probe.set()
    superseded = await older_probe
    next_status = await adapter.capability_status()

    assert refreshed.available is False
    assert refreshed.reason_codes == ("hermes_transport_busy",)
    assert superseded.available is False
    assert superseded.reason_codes == (
        "hermes_capability_probe_superseded",
    )
    assert next_status.available is False
    assert transport.capability_calls == 2
    assert transport.requests == []


@pytest.mark.asyncio
async def test_completed_refresh_supersedes_probe_started_during_refresh():
    class RefreshAuthorityTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.refresh_started = asyncio.Event()
            self.release_refresh = asyncio.Event()

        async def get_capabilities(self) -> Mapping[str, Any]:
            self.capability_calls += 1
            if self.capability_calls == 1:
                self.refresh_started.set()
                await self.release_refresh.wait()
            return copy.deepcopy(
                _capabilities(
                    endpoint_overrides={"tool_execution": "server"}
                )
            )

    transport = RefreshAuthorityTransport()
    adapter = HermesRuntimeAdapter(
        transport=transport,
        model="test-model",
        provider=PROVIDER,
    )
    refresh = asyncio.create_task(
        adapter.capability_status(refresh=True)
    )
    await transport.refresh_started.wait()

    concurrent = await adapter.capability_status()
    transport.release_refresh.set()
    refreshed = await refresh
    next_status = await adapter.capability_status()

    assert concurrent.available is False
    assert concurrent.reason_codes == ("hermes_transport_busy",)
    assert refreshed.available is False
    assert next_status.available is False
    assert transport.capability_calls == 2
    assert transport.requests == []


def test_capability_cache_fence_is_atomic_across_threads(monkeypatch):
    class TrackedLock:
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self.refresh_waiting = threading.Event()

        def __enter__(self):
            if threading.current_thread().name == "refresh-thread":
                self.refresh_waiting.set()
            self._lock.acquire()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            del exc_type, exc_value, traceback
            self._lock.release()

    class CrossThreadCapabilityTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.call_lock = threading.Lock()
            self.refresh_transport_started = threading.Event()

        async def get_capabilities(self) -> Mapping[str, Any]:
            with self.call_lock:
                self.capability_calls += 1
                call_number = self.capability_calls
            if call_number == 1:
                return copy.deepcopy(_capabilities())
            self.refresh_transport_started.set()
            return copy.deepcopy(
                _capabilities(
                    endpoint_overrides={"tool_execution": "server"}
                )
            )

    transport = CrossThreadCapabilityTransport()
    adapter = HermesRuntimeAdapter(
        transport=transport,
        model="test-model",
        provider=PROVIDER,
    )
    tracked_lock = TrackedLock()
    adapter._capability_lock = tracked_lock
    cache_copy_started = threading.Event()
    release_cache_copy = threading.Event()
    original_model_copy = HermesRuntimeCapability.model_copy

    def blocking_model_copy(self, *args, **kwargs):
        if self.available and not cache_copy_started.is_set():
            cache_copy_started.set()
            assert release_cache_copy.wait(timeout=2)
        return original_model_copy(self, *args, **kwargs)

    monkeypatch.setattr(
        HermesRuntimeCapability,
        "model_copy",
        blocking_model_copy,
    )
    probe_results: list[HermesRuntimeCapability] = []
    refresh_results: list[HermesRuntimeCapability] = []

    def run_probe() -> None:
        probe_results.append(asyncio.run(adapter.capability_status()))

    def run_refresh() -> None:
        refresh_results.append(
            asyncio.run(adapter.capability_status(refresh=True))
        )

    probe_thread = threading.Thread(target=run_probe)
    probe_thread.start()
    assert cache_copy_started.wait(timeout=2)

    refresh_thread = threading.Thread(
        target=run_refresh,
        name="refresh-thread",
    )
    refresh_thread.start()
    assert tracked_lock.refresh_waiting.wait(timeout=2)
    release_cache_copy.set()
    probe_thread.join(timeout=2)
    refresh_thread.join(timeout=2)

    assert not probe_thread.is_alive()
    assert not refresh_thread.is_alive()
    assert probe_results[0].available is True
    assert refresh_results[0].available is False
    final_status = asyncio.run(adapter.capability_status())
    assert final_status.available is False
    assert transport.capability_calls == 3
    assert transport.requests == []


@pytest.mark.asyncio
async def test_capability_probe_rejects_near_miss_contract():
    transport = FakeTransport(
        capabilities=_capabilities(
            endpoint_overrides={
                "tool_execution": "server",
                "max_model_calls": 2,
            }
        )
    )
    adapter = HermesRuntimeAdapter(
        transport=transport,
        model="test-model",
        provider=PROVIDER,
    )

    status = await adapter.capability_status()

    assert status.available is False
    assert status.reason_codes == (
        "hermes_single_iteration_contract_unsupported",
    )


@pytest.mark.parametrize("max_model_calls", [True, 1.0])
@pytest.mark.asyncio
async def test_capability_requires_exact_integer_model_call_limit(
    max_model_calls,
):
    adapter = HermesRuntimeAdapter(
        transport=FakeTransport(
            capabilities=_capabilities(
                endpoint_overrides={
                    "max_model_calls": max_model_calls,
                }
            )
        ),
        model="test-model",
        provider=PROVIDER,
    )

    status = await adapter.capability_status()

    assert status.available is False
    assert status.reason_codes == (
        "hermes_single_iteration_contract_unsupported",
    )


@pytest.mark.parametrize(
    ("runtime_overrides", "path"),
    [
        ({"mode": "server_agent"}, "/v1/model/iterations"),
        ({"tool_execution": "server"}, "/v1/model/iterations"),
        ({"split_runtime": False}, "/v1/model/iterations"),
        ({}, "/v1/chat/completions"),
        ({}, "/v1/responses"),
    ],
)
@pytest.mark.asyncio
async def test_capability_probe_rejects_root_or_path_contradictions(
    runtime_overrides,
    path,
):
    capabilities = _capabilities()
    capabilities["runtime"].update(runtime_overrides)
    capabilities["endpoints"]["model_iteration"]["path"] = path
    adapter = HermesRuntimeAdapter(
        transport=FakeTransport(capabilities=capabilities),
        model="test-model",
        provider=PROVIDER,
    )

    status = await adapter.capability_status()

    assert status.available is False
    assert status.reason_codes == (
        "hermes_single_iteration_contract_unsupported",
    )


@pytest.mark.asyncio
async def test_required_nested_parameters_are_required_at_tool_root():
    required_tool = _tool().model_copy(
        update={
            "parameters": ("record_ref",),
            "parameter_specs": (
                ContextParameterSpec(
                    name="record_ref",
                    value_type=ContextParameterType.STRING,
                    required=True,
                    max_length=36,
                    format=ContextParameterFormat.RELATED_RECORD_REF,
                ),
            ),
        }
    )
    transport = FakeTransport()
    adapter = HermesRuntimeAdapter(
        transport=transport,
        model="test-model",
        provider=PROVIDER,
    )

    await adapter.next_step(_turn(tools=(required_tool,)))

    schema = transport.requests[0].tools[0].input_schema
    assert schema["required"] == ["parameters"]
    assert schema["properties"]["parameters"]["required"] == ["record_ref"]


@pytest.mark.parametrize("slow_phase", ["capability", "iteration"])
@pytest.mark.asyncio
async def test_direct_adapter_call_enforces_turn_deadline(slow_phase):
    class SlowTransport(FakeTransport):
        async def get_capabilities(self) -> Mapping[str, Any]:
            if slow_phase == "capability":
                await asyncio.sleep(0.2)
            return await super().get_capabilities()

        async def run_model_iteration(
            self,
            *,
            endpoint: str,
            request: HermesModelIterationRequest,
            timeout_seconds: float,
        ) -> Mapping[str, Any]:
            if slow_phase == "iteration":
                await asyncio.sleep(0.2)
            return await super().run_model_iteration(
                endpoint=endpoint,
                request=request,
                timeout_seconds=timeout_seconds,
            )

    adapter = HermesRuntimeAdapter(
        transport=SlowTransport(),
        model="test-model",
        provider=PROVIDER,
    )
    turn = _turn().model_copy(update={"deadline_ms": 10})

    with pytest.raises(
        DecisionRuntimeUnavailableError,
        match="hermes_iteration_deadline_expired",
    ):
        await asyncio.wait_for(adapter.next_step(turn), timeout=0.1)


@pytest.mark.asyncio
async def test_adapter_rejects_result_after_transport_suppresses_timeout():
    class CancellationSuppressingTransport(FakeTransport):
        async def run_model_iteration(
            self,
            *,
            endpoint: str,
            request: HermesModelIterationRequest,
            timeout_seconds: float,
        ) -> Mapping[str, Any]:
            del endpoint, timeout_seconds
            try:
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                await asyncio.sleep(0.02)
            return _draft_response(request)

    adapter = HermesRuntimeAdapter(
        transport=CancellationSuppressingTransport(),
        model="test-model",
        provider=PROVIDER,
    )
    turn = _turn().model_copy(update={"deadline_ms": 10})

    with pytest.raises(
        DecisionRuntimeUnavailableError,
        match="hermes_iteration_deadline_expired",
    ):
        await asyncio.wait_for(adapter.next_step(turn), timeout=0.1)


@pytest.mark.parametrize("cancelled_phase", ["capability", "iteration"])
@pytest.mark.asyncio
async def test_adapter_preserves_externally_requested_cancellation(
    cancelled_phase,
):
    class CancellationSuppressingTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.phase_started = asyncio.Event()
            self.phase_finished = asyncio.Event()

        async def get_capabilities(self) -> Mapping[str, Any]:
            self.capability_calls += 1
            if cancelled_phase == "capability":
                self.phase_started.set()
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    task = asyncio.current_task()
                    assert task is not None
                    task.uncancel()
                self.phase_finished.set()
            return copy.deepcopy(self.capabilities)

        async def run_model_iteration(
            self,
            *,
            endpoint: str,
            request: HermesModelIterationRequest,
            timeout_seconds: float,
        ) -> Mapping[str, Any]:
            self.endpoints.append(endpoint)
            self.requests.append(request.model_copy(deep=True))
            self.timeouts.append(timeout_seconds)
            if cancelled_phase == "iteration":
                self.phase_started.set()
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    task = asyncio.current_task()
                    assert task is not None
                    task.uncancel()
                self.phase_finished.set()
            return copy.deepcopy(self.response_factory(request))

    transport = CancellationSuppressingTransport()
    adapter = HermesRuntimeAdapter(
        transport=transport,
        model="test-model",
        provider=PROVIDER,
    )
    task = asyncio.create_task(adapter.next_step(_turn()))
    await transport.phase_started.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(transport.phase_finished.wait(), timeout=1)
    await asyncio.sleep(0)
    if cancelled_phase == "capability":
        assert transport.requests == []
    else:
        assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_stuck_cancelled_transport_is_bounded_and_quarantined():
    class StuckCancellationTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.iteration_started = asyncio.Event()
            self.release_iteration = asyncio.Event()
            self.active_iterations = 0
            self.max_active_iterations = 0

        async def run_model_iteration(
            self,
            *,
            endpoint: str,
            request: HermesModelIterationRequest,
            timeout_seconds: float,
        ) -> Mapping[str, Any]:
            self.endpoints.append(endpoint)
            self.requests.append(request.model_copy(deep=True))
            self.timeouts.append(timeout_seconds)
            if len(self.requests) > 1:
                return copy.deepcopy(self.response_factory(request))
            self.active_iterations += 1
            self.max_active_iterations = max(
                self.max_active_iterations,
                self.active_iterations,
            )
            self.iteration_started.set()
            try:
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    task = asyncio.current_task()
                    assert task is not None
                    task.uncancel()
                    await self.release_iteration.wait()
                return copy.deepcopy(self.response_factory(request))
            finally:
                self.active_iterations -= 1

    transport = StuckCancellationTransport()
    adapter = HermesRuntimeAdapter(
        transport=transport,
        model="test-model",
        provider=PROVIDER,
    )
    short_turn = _turn().model_copy(update={"deadline_ms": 10})

    with pytest.raises(
        DecisionRuntimeUnavailableError,
        match="hermes_iteration_deadline_expired",
    ):
        await adapter.next_step(short_turn)
    await transport.iteration_started.wait()

    for _ in range(3):
        started = asyncio.get_running_loop().time()
        with pytest.raises(
            DecisionRuntimeUnavailableError,
            match="hermes_transport_quarantined",
        ):
            await adapter.next_step(_turn())
        assert asyncio.get_running_loop().time() - started < 0.1

    assert len(transport.requests) == 1
    assert transport.active_iterations == 1
    assert transport.max_active_iterations == 1
    assert len(adapter._detached_transport_tasks) == 1

    transport.release_iteration.set()
    for _ in range(100):
        if not adapter._detached_transport_tasks:
            break
        await asyncio.sleep(0.001)

    assert transport.active_iterations == 0
    assert adapter._detached_transport_tasks == set()
    recovered = await adapter.next_step(_turn())
    assert recovered.draft is not None
    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_nested_transport_task_is_owned_and_quarantined():
    class NestedTaskTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.nested_started = asyncio.Event()
            self.release_nested = asyncio.Event()
            self.active_nested = 0
            self.max_active_nested = 0

        async def run_model_iteration(
            self,
            *,
            endpoint: str,
            request: HermesModelIterationRequest,
            timeout_seconds: float,
        ):
            self.endpoints.append(endpoint)
            self.requests.append(request.model_copy(deep=True))
            self.timeouts.append(timeout_seconds)

            async def nested():
                self.active_nested += 1
                self.max_active_nested = max(
                    self.max_active_nested,
                    self.active_nested,
                )
                self.nested_started.set()
                try:
                    try:
                        await asyncio.sleep(60)
                    except asyncio.CancelledError:
                        task = asyncio.current_task()
                        assert task is not None
                        task.uncancel()
                        await self.release_nested.wait()
                    return _draft_response(request)
                finally:
                    self.active_nested -= 1

            return asyncio.create_task(nested())

    transport = NestedTaskTransport()
    adapter = HermesRuntimeAdapter(
        transport=transport,
        model="test-model",
        provider=PROVIDER,
    )

    with pytest.raises(
        DecisionRuntimeUnavailableError,
        match="hermes_transport_contract_invalid",
    ):
        await adapter.next_step(_turn())
    await transport.nested_started.wait()

    for _ in range(3):
        with pytest.raises(
            DecisionRuntimeUnavailableError,
            match="hermes_transport_quarantined",
        ):
            await adapter.next_step(_turn())

    assert len(transport.requests) == 1
    assert transport.active_nested == 1
    assert transport.max_active_nested == 1
    assert adapter._detached_transport_tasks == set()
    assert adapter._transport_permanently_quarantined is True
    assert len(adapter._quarantined_transport_refs) == 1

    transport.release_nested.set()
    for _ in range(100):
        if transport.active_nested == 0:
            break
        await asyncio.sleep(0.001)

    assert transport.active_nested == 0
    assert adapter._detached_transport_tasks == set()
    with pytest.raises(
        DecisionRuntimeUnavailableError,
        match="hermes_transport_quarantined",
    ):
        await adapter.next_step(_turn())
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_cancellation_at_nested_result_permanently_quarantines():
    adapter = HermesRuntimeAdapter(
        transport=FakeTransport(),
        model="test-model",
        provider=PROVIDER,
    )
    nested_started = asyncio.Event()
    release_nested = asyncio.Event()
    parent_task: asyncio.Task[Any] | None = None

    async def nested():
        nested_started.set()
        try:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                assert task is not None
                task.uncancel()
                await release_nested.wait()
        finally:
            pass

    async def operation():
        nested_task = asyncio.create_task(nested())
        assert parent_task is not None
        asyncio.get_running_loop().call_soon(parent_task.cancel)
        return nested_task

    parent_task = asyncio.create_task(
        adapter._run_transport_operation(operation)
    )
    await nested_started.wait()

    with pytest.raises(asyncio.CancelledError):
        await parent_task

    assert adapter._transport_permanently_quarantined is True
    assert len(adapter._quarantined_transport_refs) == 1
    release_nested.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_foreign_loop_future_is_bounded_by_permanent_quarantine():
    ready = threading.Event()
    foreign_done = threading.Event()
    holder: list[asyncio.Task[None]] = []
    foreign_loop = asyncio.new_event_loop()

    async def foreign_work() -> None:
        try:
            await asyncio.sleep(60)
        finally:
            foreign_done.set()

    def run_foreign_loop() -> None:
        asyncio.set_event_loop(foreign_loop)

        def start_work() -> None:
            holder.append(foreign_loop.create_task(foreign_work()))
            ready.set()

        foreign_loop.call_soon(start_work)
        foreign_loop.run_forever()

    thread = threading.Thread(target=run_foreign_loop)
    thread.start()
    assert ready.wait(timeout=2)

    class ForeignFutureTransport(FakeTransport):
        async def get_capabilities(self):
            return holder[0]

    adapter = HermesRuntimeAdapter(
        transport=ForeignFutureTransport(),
        model="test-model",
        provider=PROVIDER,
    )
    try:
        status = await adapter.capability_status()

        assert status.available is False
        assert status.reason_codes == (
            "hermes_transport_contract_invalid",
        )
        assert adapter._transport_permanently_quarantined is True
        assert len(adapter._quarantined_transport_refs) == 1
        assert await asyncio.to_thread(foreign_done.wait, 1)
        assert holder[0].cancelled()
        quarantined = await adapter.capability_status()
        assert quarantined.reason_codes == (
            "hermes_transport_quarantined",
        )
    finally:
        foreign_loop.call_soon_threadsafe(foreign_loop.stop)
        thread.join(timeout=2)
        foreign_loop.close()


@pytest.mark.asyncio
async def test_stopped_foreign_loop_task_is_cancelled_before_resume():
    ready = threading.Event()
    resume = threading.Event()
    stopped = threading.Event()
    side_effect = threading.Event()
    holder: list[asyncio.Task[None]] = []
    foreign_loop = asyncio.new_event_loop()

    async def foreign_work() -> None:
        side_effect.set()

    def run_foreign_loop() -> None:
        asyncio.set_event_loop(foreign_loop)

        def create_then_stop() -> None:
            holder.append(foreign_loop.create_task(foreign_work()))
            ready.set()
            foreign_loop.stop()

        foreign_loop.call_soon(create_then_stop)
        foreign_loop.run_forever()
        stopped.set()
        assert resume.wait(timeout=2)
        foreign_loop.call_later(0.05, foreign_loop.stop)
        foreign_loop.run_forever()

    thread = threading.Thread(target=run_foreign_loop)
    thread.start()
    assert ready.wait(timeout=2)
    assert stopped.wait(timeout=2)

    class StoppedForeignFutureTransport(FakeTransport):
        async def get_capabilities(self):
            return holder[0]

    adapter = HermesRuntimeAdapter(
        transport=StoppedForeignFutureTransport(),
        model="test-model",
        provider=PROVIDER,
    )
    try:
        status = await adapter.capability_status()

        assert status.reason_codes == (
            "hermes_transport_contract_invalid",
        )
        resume.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert holder[0].cancelled()
        assert not side_effect.is_set()
    finally:
        resume.set()
        if thread.is_alive():
            foreign_loop.call_soon_threadsafe(foreign_loop.stop)
            thread.join(timeout=2)
        foreign_loop.close()


@pytest.mark.asyncio
async def test_invalid_future_cleanup_does_not_log_private_errors():
    private_secret = "PRIVATE-CALLBACK-SECRET"
    loop = asyncio.get_running_loop()
    logged_contexts: list[dict[str, Any]] = []
    original_handler = loop.get_exception_handler()

    class HostileFuture(asyncio.Future):
        def cancel(self, msg=None):
            del msg
            raise ValueError(private_secret)

        def done(self):
            raise ValueError("PRIVATE-DONE-SECRET")

    future = HostileFuture()
    future.set_exception(ValueError(private_secret))

    class HostileFutureTransport(FakeTransport):
        async def get_capabilities(self):
            return future

    loop.set_exception_handler(
        lambda event_loop, context: logged_contexts.append(context)
    )
    try:
        adapter = HermesRuntimeAdapter(
            transport=HostileFutureTransport(),
            model="test-model",
            provider=PROVIDER,
        )

        status = await adapter.capability_status()
        await asyncio.sleep(0)

        assert status.reason_codes == (
            "hermes_transport_contract_invalid",
        )
        assert adapter._transport_permanently_quarantined is True
        assert asyncio.Future.done(future)
        rendered = repr(logged_contexts)
        assert private_secret not in rendered
        assert "PRIVATE-DONE-SECRET" not in rendered
        assert logged_contexts == []
    finally:
        loop.set_exception_handler(original_handler)


@pytest.mark.asyncio
async def test_capability_probe_rejects_custom_mapping_without_calling_it():
    private_secret = "PRIVATE-CAPABILITY-SECRET"
    loop = asyncio.get_running_loop()
    logged_contexts: list[dict[str, Any]] = []
    original_handler = loop.get_exception_handler()

    class HostileCapabilities(Mapping[str, Any]):
        def __getitem__(self, key):
            del key
            raise ValueError(private_secret)

        def __iter__(self):
            raise ValueError(private_secret)

        def __len__(self):
            raise ValueError(private_secret)

        def get(self, key, default=None):
            del key, default
            raise ValueError(private_secret)

    class HostileCapabilityTransport(FakeTransport):
        async def get_capabilities(self):
            return HostileCapabilities()

    loop.set_exception_handler(
        lambda event_loop, context: logged_contexts.append(context)
    )
    try:
        adapter = HermesRuntimeAdapter(
            transport=HostileCapabilityTransport(),
            model="test-model",
            provider=PROVIDER,
        )

        status = await adapter.capability_status()
        await asyncio.sleep(0)

        assert status.reason_codes == (
            "hermes_transport_contract_invalid",
        )
        assert private_secret not in repr(status)
        assert private_secret not in repr(logged_contexts)
        assert logged_contexts == []
    finally:
        loop.set_exception_handler(original_handler)


@pytest.mark.asyncio
async def test_iteration_rejects_hostile_nested_dict_before_items_or_deadline():
    private_secret = "PRIVATE-RESPONSE-SECRET"
    loop = asyncio.get_running_loop()
    logged_contexts: list[dict[str, Any]] = []
    original_handler = loop.get_exception_handler()

    class BlockingDict(dict):
        def items(self):
            threading.Event().wait(0.2)
            raise ValueError(private_secret)

    class HostileResponseTransport(FakeTransport):
        async def run_model_iteration(
            self,
            *,
            endpoint: str,
            request: HermesModelIterationRequest,
            timeout_seconds: float,
        ) -> Mapping[str, Any]:
            self.endpoints.append(endpoint)
            self.requests.append(request.model_copy(deep=True))
            self.timeouts.append(timeout_seconds)
            response = _draft_response(request)
            response["output"] = BlockingDict(response["output"])
            return response

    loop.set_exception_handler(
        lambda event_loop, context: logged_contexts.append(context)
    )
    try:
        adapter = HermesRuntimeAdapter(
            transport=HostileResponseTransport(),
            model="test-model",
            provider=PROVIDER,
        )
        assert (await adapter.capability_status()).available is True
        turn = _turn().model_copy(update={"deadline_ms": 10})
        started = loop.time()

        with pytest.raises(
            DecisionRuntimeUnavailableError,
            match="hermes_transport_contract_invalid",
        ) as error:
            await asyncio.wait_for(adapter.next_step(turn), timeout=0.1)

        elapsed = loop.time() - started
        await asyncio.sleep(0)
        assert elapsed < 0.1
        assert error.value.__cause__ is None
        assert error.value.__context__ is None
        assert private_secret not in repr(error.value)
        assert private_secret not in repr(logged_contexts)
        assert logged_contexts == []
    finally:
        loop.set_exception_handler(original_handler)


@pytest.mark.asyncio
async def test_large_integer_tree_is_rejected_during_bounded_traversal():
    capabilities = _capabilities()
    capabilities["padding"] = [
        (1 << 4_094) - 1
        for _ in range(19_500)
    ]

    class LargeIntegerCapabilityTransport(FakeTransport):
        async def get_capabilities(self):
            return capabilities

    adapter = HermesRuntimeAdapter(
        transport=LargeIntegerCapabilityTransport(),
        model="test-model",
        provider=PROVIDER,
    )
    turn = _turn().model_copy(update={"deadline_ms": 10})
    started = asyncio.get_running_loop().time()

    with pytest.raises(
        DecisionRuntimeUnavailableError,
        match=(
            "hermes_transport_contract_invalid"
            "|hermes_iteration_deadline_expired"
        ),
    ):
        await asyncio.wait_for(adapter.next_step(turn), timeout=0.1)

    assert asyncio.get_running_loop().time() - started < 0.1


@pytest.mark.asyncio
async def test_expired_capability_probe_does_not_seed_success_cache():
    class CancellationSuppressingCapabilityTransport(FakeTransport):
        async def get_capabilities(self) -> Mapping[str, Any]:
            self.capability_calls += 1
            if self.capability_calls == 1:
                try:
                    await asyncio.sleep(0.2)
                except asyncio.CancelledError:
                    await asyncio.sleep(0.02)
            return copy.deepcopy(self.capabilities)

    transport = CancellationSuppressingCapabilityTransport()
    adapter = HermesRuntimeAdapter(
        transport=transport,
        model="test-model",
        provider=PROVIDER,
    )
    expired_turn = _turn().model_copy(update={"deadline_ms": 10})

    with pytest.raises(
        DecisionRuntimeUnavailableError,
        match="hermes_iteration_deadline_expired",
    ):
        await asyncio.wait_for(adapter.next_step(expired_turn), timeout=0.1)

    for _ in range(100):
        if not adapter._detached_transport_tasks:
            break
        await asyncio.sleep(0.001)

    assert adapter._detached_transport_tasks == set()
    result = await adapter.next_step(_turn())

    assert result.draft is not None
    assert transport.capability_calls == 2
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_http_transport_ignores_environment_proxies(
    monkeypatch,
):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    client_trust_env: list[bool | None] = []
    original_client = httpx.AsyncClient

    class RecordingAsyncClient(original_client):
        def __init__(self, *args, **kwargs):
            client_trust_env.append(kwargs.get("trust_env"))
            super().__init__(*args, **kwargs)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "127.0.0.1"
        return httpx.Response(200, json=_capabilities())

    monkeypatch.setattr(httpx, "AsyncClient", RecordingAsyncClient)
    adapter = HermesRuntimeAdapter(
        transport=HermesHttpIterationTransport(
            base_url="http://127.0.0.1:8642",
            http_transport=httpx.MockTransport(handler),
        ),
        model="test-model",
        provider=PROVIDER,
    )

    status = await adapter.capability_status()

    assert status.available is True
    assert client_trust_env == [False]


@pytest.mark.asyncio
async def test_transport_failure_does_not_chain_credentials_or_question():
    api_key = "api-key-that-must-not-escape"
    private_question = "PRIVATE QUESTION BODY THAT MUST NOT ESCAPE"

    class LeakingFailureTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(
            self,
            request: httpx.Request,
        ) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json=_capabilities())
            body = (await request.aread()).decode("utf-8")
            authorization = request.headers["Authorization"]
            raise httpx.ConnectError(
                f"{authorization}: {body}",
                request=request,
            )

    transport = HermesHttpIterationTransport(
        base_url="http://127.0.0.1:8642",
        api_key=api_key,
        http_transport=LeakingFailureTransport(),
    )
    adapter = HermesRuntimeAdapter(
        transport=transport,
        model="test-model",
        provider=PROVIDER,
    )
    turn = _turn()
    turn = turn.model_copy(
        update={
            "request": turn.request.model_copy(
                update={"question": private_question}
            )
        }
    )

    with pytest.raises(
        DecisionRuntimeUnavailableError,
        match="hermes_transport_unreachable",
    ) as error:
        await adapter.next_step(turn)

    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    rendered = repr(error.value)
    assert api_key not in rendered
    assert private_question not in rendered


@pytest.mark.asyncio
async def test_contract_failures_do_not_chain_private_validation_inputs():
    private_question = "PRIVATE INVALID TURN QUESTION"
    invalid_turn = _turn().model_dump(mode="python")
    invalid_turn["request"]["question"] = private_question
    invalid_turn["deadline_ms"] = "not-an-integer"
    adapter = HermesRuntimeAdapter(
        transport=FakeTransport(),
        model="test-model",
        provider=PROVIDER,
    )

    with pytest.raises(
        DecisionRuntimeContractError,
        match="hermes_turn_contract_invalid",
    ) as turn_error:
        await adapter.next_step(invalid_turn)

    assert turn_error.value.__cause__ is None
    assert turn_error.value.__context__ is None
    assert private_question not in repr(turn_error.value)

    private_response = "PRIVATE INVALID RESPONSE VALUE"

    def invalid_response(
        request: HermesModelIterationRequest,
    ) -> Mapping[str, Any]:
        payload = _draft_response(request)
        payload["output"]["answer"] = {
            "private": private_response,
        }
        return payload

    response_adapter = HermesRuntimeAdapter(
        transport=FakeTransport(response_factory=invalid_response),
        model="test-model",
        provider=PROVIDER,
    )
    with pytest.raises(
        DecisionRuntimeContractError,
        match="hermes_response_contract_invalid",
    ) as response_error:
        await response_adapter.next_step(_turn())

    assert response_error.value.__cause__ is None
    assert response_error.value.__context__ is None
    assert private_response not in repr(response_error.value)


@pytest.mark.asyncio
async def test_http_transport_probes_authenticated_capabilities_only():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_capabilities())

    transport = HermesHttpIterationTransport(
        base_url="http://127.0.0.1:8642",
        api_key="secret",
        http_transport=httpx.MockTransport(handler),
    )
    adapter = HermesRuntimeAdapter(
        transport=transport,
        model="test-model",
        provider=PROVIDER,
    )

    status = await adapter.capability_status()

    assert status.available is True
    assert status.endpoint == "/v1/model/iterations"
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/v1/capabilities"
    assert seen[0].headers["Authorization"] == "Bearer secret"
    assert seen[0].headers["Accept-Encoding"] == "identity"


@pytest.mark.asyncio
async def test_http_transport_rejects_compression_before_reading_body():
    class NeverReadCompressedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("compressed response body was read")
            yield b""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            stream=NeverReadCompressedStream(),
        )

    adapter = HermesRuntimeAdapter(
        transport=HermesHttpIterationTransport(
            base_url="http://127.0.0.1:8642",
            http_transport=httpx.MockTransport(handler),
        ),
        model="test-model",
        provider=PROVIDER,
    )

    status = await adapter.capability_status()

    assert status.available is False
    assert status.reason_codes == (
        "hermes_response_compression_unsupported",
    )


@pytest.mark.asyncio
async def test_http_transport_stops_stream_at_decoded_response_limit():
    class GuardedOversizeStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index in range(40):
                if index >= 32:
                    raise AssertionError(
                        "transport eagerly consumed the oversized response"
                    )
                yield b"x" * (64 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=GuardedOversizeStream())

    adapter = HermesRuntimeAdapter(
        transport=HermesHttpIterationTransport(
            base_url="http://127.0.0.1:8642",
            http_transport=httpx.MockTransport(handler),
        ),
        model="test-model",
        provider=PROVIDER,
    )

    status = await adapter.capability_status()

    assert status.available is False
    assert status.reason_codes == ("hermes_response_too_large",)


@pytest.mark.asyncio
async def test_http_transport_posts_only_to_advertised_iteration_endpoint():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=_capabilities())
        parsed = HermesModelIterationRequest.model_validate_json(
            request.read().decode("utf-8")
        )
        return httpx.Response(200, json=_draft_response(parsed))

    transport = HermesHttpIterationTransport(
        base_url="http://127.0.0.1:8642",
        api_key="secret",
        http_transport=httpx.MockTransport(handler),
    )
    adapter = HermesRuntimeAdapter(
        transport=transport,
        model="test-model",
        provider=PROVIDER,
    )

    result = await adapter.next_step(_turn())

    assert result.draft is not None
    assert [request.method for request in seen] == ["GET", "POST"]
    assert seen[1].url.path == "/v1/model/iterations"
    assert seen[1].headers["Authorization"] == "Bearer secret"
    parsed_request = HermesModelIterationRequest.model_validate_json(
        seen[1].read().decode("utf-8")
    )
    assert seen[1].headers["Idempotency-Key"] == (
        f"hm-{parsed_request.request_fingerprint}"
    )


@pytest.mark.asyncio
async def test_http_transport_rejects_duplicate_json_keys():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"features":{},"features":{"model_iteration":true}}',
            headers={"Content-Type": "application/json"},
        )

    adapter = HermesRuntimeAdapter(
        transport=HermesHttpIterationTransport(
            base_url="http://127.0.0.1:8642",
            http_transport=httpx.MockTransport(handler),
        ),
        model="test-model",
        provider=PROVIDER,
    )

    status = await adapter.capability_status()

    assert status.available is False
    assert status.reason_codes == ("hermes_response_invalid",)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:8642",
        "file:///tmp/hermes.sock",
        "https://user:secret@example.com",
        "https://example.com/base",
        "https://example.com?token=secret",
    ],
)
def test_http_transport_rejects_unsafe_base_urls(url):
    with pytest.raises(ValueError):
        HermesHttpIterationTransport(base_url=url)


def test_remote_http_transport_requires_authentication():
    with pytest.raises(ValueError, match="requires an api_key"):
        HermesHttpIterationTransport(base_url="https://example.com")

    transport = HermesHttpIterationTransport(
        base_url="https://example.com",
        api_key="secret",
    )
    assert transport is not None


def test_healthmes_hermes_adapter_does_not_import_vendor_internals():
    source_path = (
        Path(__file__).resolve().parents[2]
        / "healthmes"
        / "decision"
        / "hermes.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }

    assert not any(
        name.startswith(("vendor", "run_agent", "agent."))
        for name in imported
    )


def test_hermes_contract_documents_no_fallback_and_upstream_boundary():
    contract_path = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "contracts"
        / "HERMES-MODEL-ITERATION-HOOK.ko.md"
    )
    contract = contract_path.read_text(encoding="utf-8")

    assert "full chat fallback 금지" in contract
    assert "provider/model을 정확히 한 번 호출" in contract
    assert "Skill이 설치됐는지는 보안과 정확성 계약에 영향을 주지 않는다" in contract
    assert "#139 HERMES-UPSTREAM-01" in contract
