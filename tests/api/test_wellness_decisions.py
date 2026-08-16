from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import sessionmaker

from healthmes.api.auth import viewer_token
from healthmes.app import create_app
from healthmes.decision import (
    ContextAccessLayer,
    ContextAccessPolicy,
    ContextCapability,
    ContextProviderMetadata,
    ContextProviderRegistry,
    ContextQuery,
    ContextResult,
    ContextStatus,
    DecisionBudget,
    DecisionEngineBusyError,
    DecisionEngineClosedError,
    DecisionFinalizer,
    DecisionResult,
    DecisionStatus,
    DomainAccessGrant,
    ExecutionScope,
    HealthMesDecisionAgent,
    HealthMesDecisionEngine,
    HermesResponsesTransportError,
    PersistenceStatus,
    PrivacyLevel,
    RuntimeMetadata,
    RuntimeStepOutput,
    ToolCallRecord,
    ToolCallStatus,
)
from healthmes.store import (
    Base,
    DecisionKind,
    DecisionRecord,
    create_db_engine,
    session_scope,
)
from tests.decision.test_e2e import (
    DAY,
    NOW,
)

TOKEN = "wellness-decision-api-token"
MODEL = "test-model"
PROVIDER = "test-provider"


def _bearer(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _secured_settings(settings, **updates):
    return settings.model_copy(
        update={
            "api_token": SecretStr(TOKEN),
            "decision_owner_principal_id": "rest-owner",
            **updates,
        }
    )


def _trace_with_private_payload() -> ToolCallRecord:
    query = ContextQuery(
        provider_id="activity",
        capability="activity.summary",
        start=NOW - timedelta(hours=1),
        end=NOW,
        timezone="UTC",
    )
    return ToolCallRecord(
        query=query,
        status=ToolCallStatus.COMPLETED,
        started_at=NOW,
        finished_at=NOW,
        result=ContextResult(
            query_id=query.query_id,
            provider_id=query.provider_id,
            capability=query.capability,
            status=ContextStatus.OK,
            payload={
                "private_app_name": "secret-window-title",
                "active_seconds": 600,
            },
        ),
    )


class RecordingDecisionEngine:
    def __init__(self) -> None:
        self.requests = []

    async def ask_wellness(self, request):
        self.requests.append(request)
        return DecisionResult(
            request_id=request.request_id,
            turn_id=request.turn_id,
            status=DecisionStatus.COMPLETED,
            answer="Take a short break.",
            runtime=RuntimeMetadata(
                runtime="scripted",
                model="api-boundary-v1",
            ),
            tool_trace=[_trace_with_private_payload()],
        )


class ClosingDecisionEngine:
    async def ask_wellness(self, _request):
        raise DecisionEngineClosedError(
            "HealthMes decision engine is closing"
        )


class BusyDecisionEngine:
    async def ask_wellness(self, _request):
        raise DecisionEngineBusyError(
            "HealthMes decision engine is at capacity"
        )


class FailedRuntimeDecisionEngine:
    def __init__(
        self,
        limitation: str,
        *,
        status: DecisionStatus = DecisionStatus.FAILED,
    ) -> None:
        self.limitation = limitation
        self.status = status

    async def ask_wellness(self, request):
        return DecisionResult(
            request_id=request.request_id,
            turn_id=request.turn_id,
            status=self.status,
            limitations=[self.limitation],
            runtime=RuntimeMetadata(
                runtime="hermes",
                model=MODEL,
                provider=PROVIDER,
            ),
        )


class UnknownPersistenceDecisionEngine:
    async def ask_wellness(self, request):
        return DecisionResult(
            request_id=request.request_id,
            turn_id=request.turn_id,
            status=DecisionStatus.FAILED,
            limitations=["decision_finalization_outcome_unknown"],
            persistence_status=PersistenceStatus.UNKNOWN,
            runtime=RuntimeMetadata(runtime="healthmes-finalizer"),
        )


class MissingResponsesTransport:
    def __init__(self) -> None:
        self.response_calls = 0

    async def get_toolsets(self) -> dict[str, Any]:
        return {
            "object": "list",
            "platform": "api_server",
            "data": [],
        }

    async def get_models(self) -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": MODEL,
                    "object": "model",
                    "created": 1,
                    "owned_by": "hermes",
                    "permission": [],
                    "root": MODEL,
                    "parent": "healthmes-decision-runtime",
                }
            ],
        }

    async def create_response(
        self,
        _payload,
        *,
        timeout_seconds: float,
    ):
        assert timeout_seconds > 0
        self.response_calls += 1
        raise HermesResponsesTransportError(
            "hermes_responses_endpoint_missing"
        )

    async def delete_session(self, _session_id: str) -> None:
        raise AssertionError("no Hermes session was created")


class FailingNutritionProvider:
    metadata = ContextProviderMetadata(
        provider_id="nutrition",
        domain="nutrition",
        description="Provider used to verify real REST failure propagation.",
        capabilities=(
            ContextCapability(
                capability="nutrition.summary",
                description="Read a nutrition summary.",
                granularities=("summary",),
                query_fields=("timezone",),
                output_fields=("caffeine_mg",),
                max_lookback_days=7,
                sensitivity="nutrition",
                freshness_expectation="Current retained nutrition context.",
            ),
        ),
    )

    async def query(self, _session, _query, *, now):
        del now
        raise RuntimeError("private provider failure")


class InvalidNutritionProvider(FailingNutritionProvider):
    async def query(self, _session, _query, *, now):
        del now
        raise ValueError("private invalid provider query")


class ProviderFailureRuntime:
    metadata = RuntimeMetadata(
        runtime="scripted",
        model="provider-failure-e2e-v1",
    )

    async def next_step(self, turn):
        if not turn.history:
            return RuntimeStepOutput(
                tool_calls=({"capability": "nutrition.summary"},),
                metadata=self.metadata,
            )
        raise AssertionError(
            "a failed provider call must terminate before another LLM step"
        )


def test_rest_contract_is_server_owned_and_hides_internal_trace(
    settings,
) -> None:
    secured = _secured_settings(settings)
    engine = RecordingDecisionEngine()
    app = create_app(secured)

    with TestClient(
        app,
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    ) as client:
        app.state.decision_engine = engine
        response = client.post(
            "/v1/wellness-decisions",
            headers=_bearer(),
            json={
                "question": " Should I keep working? ",
                "persistence_requested": True,
                "hints": {"local_date": DAY.isoformat()},
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "Take a short break."
    assert "tool_trace" not in body
    assert "private_app_name" not in response.text
    assert "secret-window-title" not in response.text

    assert len(engine.requests) == 1
    request = engine.requests[0]
    assert request.question == "Should I keep working?"
    assert request.caller.principal_id == "rest-owner"
    assert request.caller.authenticated is True
    assert request.caller.execution_scope is ExecutionScope.LOCAL
    assert request.caller.channel == "rest"
    assert request.requested_privacy_level is PrivacyLevel.AGGREGATE
    assert request.persistence_requested is True
    assert request.budget == DecisionBudget()
    assert request.hints.local_date == DAY
    assert request.hints.related_record_ids == {}


@pytest.mark.parametrize("value", ("true", "yes", "on", 1))
def test_rest_rejects_non_boolean_persistence_consent(
    settings,
    value,
) -> None:
    secured = _secured_settings(settings)
    engine = RecordingDecisionEngine()
    app = create_app(secured)

    with TestClient(
        app,
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    ) as client:
        app.state.decision_engine = engine
        response = client.post(
            "/v1/wellness-decisions",
            headers=_bearer(),
            json={
                "question": "Track this decision.",
                "persistence_requested": value,
            },
        )

    assert response.status_code == 422
    assert engine.requests == []


def test_hosted_scope_is_server_owned_even_for_loopback_hermes(
    settings,
) -> None:
    hosted = _secured_settings(
        settings,
        decision_execution_scope="hosted",
    )
    engine = RecordingDecisionEngine()
    app = create_app(hosted)

    with TestClient(
        app,
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    ) as client:
        app.state.decision_engine = engine
        response = client.post(
            "/v1/wellness-decisions",
            headers=_bearer(),
            json={"question": "Should I keep working?"},
        )

    assert response.status_code == 200
    assert len(engine.requests) == 1
    assert (
        engine.requests[0].caller.execution_scope
        is ExecutionScope.HOSTED
    )


def test_domain_settings_bootstrap_update_and_persist(
    settings,
) -> None:
    configured = _secured_settings(
        settings,
        database_url=(
            f"sqlite+pysqlite:///"
            f"{settings.data_dir / 'decision-settings.db'}"
        ),
    )
    app = create_app(configured)

    with TestClient(
        app,
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    ) as client:
        initial = client.get(
            "/v1/wellness-decisions/settings",
            headers=_bearer(),
        )
        disabled = client.put(
            "/v1/wellness-decisions/settings/nutrition",
            headers=_bearer(),
            json={"enabled": False},
        )
        persisted = client.get(
            "/v1/wellness-decisions/settings",
            headers=_bearer(),
        )

    assert initial.status_code == 200
    assert initial.json() == {
        "execution_scope": "local",
        "domains": [
            {"domain": "activity", "enabled": True},
            {"domain": "nutrition", "enabled": True},
            {"domain": "wearable", "enabled": True},
            {"domain": "calendar", "enabled": True},
        ],
    }
    assert disabled.status_code == 200
    assert disabled.json() == {
        "domain": "nutrition",
        "enabled": False,
    }
    assert persisted.json()["domains"][1] == {
        "domain": "nutrition",
        "enabled": False,
    }

    restarted = create_app(configured)
    with TestClient(
        restarted,
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    ) as client:
        after_restart = client.get(
            "/v1/wellness-decisions/settings",
            headers=_bearer(),
        )

    assert after_restart.json()["domains"][1] == {
        "domain": "nutrition",
        "enabled": False,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("caller", {"principal_id": "attacker"}),
        ("owner_principal_id", "attacker"),
        ("requested_privacy_level", "scoped_raw"),
        ("budget", {"max_steps": 32}),
        ("grants", [{"domain": "medical"}]),
        ("execution_scope", "hosted"),
        ("request_id", str(uuid.uuid4())),
    ],
)
def test_callers_cannot_override_identity_privacy_budget_or_domains(
    settings,
    field,
    value,
) -> None:
    secured = _secured_settings(settings)
    engine = RecordingDecisionEngine()
    app = create_app(secured)

    with TestClient(
        app,
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    ) as client:
        app.state.decision_engine = engine
        response = client.post(
            "/v1/wellness-decisions",
            headers=_bearer(),
            json={
                "question": "Should I keep working?",
                field: value,
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert engine.requests == []


def test_decision_post_requires_full_bearer_not_viewer_token(
    settings,
) -> None:
    secured = _secured_settings(settings)
    app = create_app(secured)
    payload = {"question": "Should I keep working?"}

    with TestClient(
        app,
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    ) as client:
        anonymous = client.post(
            "/v1/wellness-decisions",
            json=payload,
        )
        wrong = client.post(
            "/v1/wellness-decisions",
            headers=_bearer("wrong"),
            json=payload,
        )
        viewer = client.post(
            "/v1/wellness-decisions",
            params={"token": viewer_token(TOKEN)},
            json=payload,
        )
        authorized = client.post(
            "/v1/wellness-decisions",
            headers=_bearer(),
            json=payload,
        )

    assert anonymous.status_code == 401
    assert wrong.status_code == 401
    assert viewer.status_code == 401
    assert authorized.status_code == 503
    assert (
        authorized.json()["error"]["code"]
        == "decision_runtime_not_configured"
    )


def test_request_contract_errors_are_422_before_runtime_lookup(
    settings,
) -> None:
    app = create_app(settings)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    ) as client:
        blank = client.post(
            "/v1/wellness-decisions",
            json={"question": "   "},
        )
        overlong_range = client.post(
            "/v1/wellness-decisions",
            json={
                "question": "Summarize this period.",
                "hints": {
                    "start": "2026-01-01T00:00:00Z",
                    "end": "2026-05-01T00:00:00Z",
                },
            },
        )

    assert blank.status_code == 422
    assert overlong_range.status_code == 422
    assert blank.json()["error"]["code"] == "validation_error"
    assert overlong_range.json()["error"]["code"] == "validation_error"


def test_closing_engine_returns_503(settings) -> None:
    app = create_app(settings)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    ) as client:
        app.state.decision_engine = ClosingDecisionEngine()
        response = client.post(
            "/v1/wellness-decisions",
            json={"question": "Should I keep working?"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "decision_engine_closing"


def test_busy_engine_returns_429(settings) -> None:
    app = create_app(settings)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    ) as client:
        app.state.decision_engine = BusyDecisionEngine()
        response = client.post(
            "/v1/wellness-decisions",
            json={"question": "Should I keep working?"},
        )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "decision_engine_busy"


@pytest.mark.parametrize(
    "limitation",
    (
        "runtime_contract_violation",
        "runtime_identity_mismatch",
        "runtime_execution_failed",
    ),
)
def test_runtime_failures_return_503(
    settings,
    limitation,
) -> None:
    app = create_app(settings)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    ) as client:
        app.state.decision_engine = FailedRuntimeDecisionEngine(
            limitation
        )
        response = client.post(
            "/v1/wellness-decisions",
            json={"question": "Should I keep working?"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == (
        "decision_runtime_unavailable"
    )
    assert response.json()["error"]["detail"] == {
        "reason_codes": [limitation],
    }


@pytest.mark.parametrize(
    "limitation",
    (
        "access_policy_resolution_failed",
        "caller_not_authenticated",
        "caller_not_policy_owner",
        "provider_catalog_invalid",
        "tool_execution_failed",
        "decision_turn_closed",
        "unknown_tool",
        "malformed_tool_arguments",
        "duplicate_tool_call",
        "decision_finalization_capacity_exhausted",
        "decision_record_persistence_failed",
    ),
)
def test_internal_decision_failures_return_503(
    settings,
    limitation,
) -> None:
    app = create_app(settings)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    ) as client:
        app.state.decision_engine = FailedRuntimeDecisionEngine(
            limitation
        )
        response = client.post(
            "/v1/wellness-decisions",
            json={"question": "Should I keep working?"},
        )

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "decision_service_unavailable",
        "message": (
            "HealthMes could not complete the decision because a required "
            "internal component was unavailable."
        ),
        "detail": {"reason_codes": [limitation]},
    }


def test_finalization_timeout_returns_service_unavailable_503(
    settings,
) -> None:
    app = create_app(settings)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    ) as client:
        app.state.decision_engine = FailedRuntimeDecisionEngine(
            "decision_finalization_timeout"
        )
        response = client.post(
            "/v1/wellness-decisions",
            json={"question": "Should I keep working?"},
        )

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "decision_service_unavailable",
        "message": (
            "HealthMes could not complete the decision because a required "
            "internal component was unavailable."
        ),
        "detail": {
            "reason_codes": ["decision_finalization_timeout"],
        },
    }


def test_unknown_commit_outcome_returns_202_and_recovery_location(
    settings,
) -> None:
    app = create_app(settings)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    ) as client:
        app.state.decision_engine = UnknownPersistenceDecisionEngine()
        response = client.post(
            "/v1/wellness-decisions",
            json={"question": "Should I keep working?"},
        )

        assert response.status_code == 202
        body = response.json()
        location = (
            f"/v1/wellness-decisions/{body['request_id']}"
        )
        assert response.headers["Location"] == location
        assert body["status"] == "failed"
        assert body["persistence_status"] == "unknown"
        assert body["decision_record_id"] is None
        assert body["limitations"] == [
            "decision_finalization_outcome_unknown"
        ]
        pending = client.get(location)

    assert pending.status_code == 404
    assert pending.json()["error"]["code"] == (
        "wellness_decision_not_found"
    )


def test_decision_recovery_returns_404_for_unknown_request(
    settings,
) -> None:
    request_id = uuid.uuid4()
    app = create_app(settings)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    ) as client:
        response = client.get(
            f"/v1/wellness-decisions/{request_id}"
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == (
        "wellness_decision_not_found"
    )


def test_decision_recovery_rejects_corrupt_persisted_payload(
    settings,
) -> None:
    request_id = uuid.uuid4()
    app = create_app(settings)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    ) as client:
        with session_scope() as session:
            session.add(
                DecisionRecord(
                    kind=DecisionKind.INSIGHT,
                    tree={},
                    summary="Corrupt decision fixture",
                    llm_model=None,
                    tokens=None,
                    decision_request_id=request_id,
                    decision_turn_id=uuid.uuid4(),
                    decision_request_fingerprint="0" * 64,
                    decision_payload={"schema": "invalid"},
                    decision_payload_digest="0" * 64,
                )
            )

        response = client.get(
            f"/v1/wellness-decisions/{request_id}"
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == (
        "decision_record_contract_invalid"
    )


def test_real_provider_failure_reaches_rest_as_503(settings) -> None:
    database = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(database)
    factory = sessionmaker(
        bind=database,
        expire_on_commit=False,
    )
    registry = ContextProviderRegistry((FailingNutritionProvider(),))
    access_layer = ContextAccessLayer(registry, clock=lambda: NOW)
    policy = ContextAccessPolicy(
        owner_principal_id="rest-owner",
        grants=(DomainAccessGrant(domain="nutrition"),),
    )
    agent = HealthMesDecisionAgent(
        access_layer=access_layer,
        runtime=ProviderFailureRuntime(),
        session_factory=factory,
        policy_resolver=lambda _request: policy,
        timeout_seconds=5,
        clock=lambda: NOW,
    )
    engine = HealthMesDecisionEngine(
        agent=agent,
        finalizer=DecisionFinalizer(
            access_layer=access_layer,
            session_factory=factory,
            policy_resolver=lambda _request: policy,
            clock=lambda: NOW,
        ),
    )
    app = create_app(_secured_settings(settings))
    try:
        with TestClient(
            app,
            base_url="http://127.0.0.1:8100",
            client=("127.0.0.1", 43123),
        ) as client:
            app.state.decision_engine = engine
            response = client.post(
                "/v1/wellness-decisions",
                headers=_bearer(),
                json={"question": "How much caffeine did I have?"},
            )

        assert response.status_code == 503
        assert response.json()["error"] == {
            "code": "decision_service_unavailable",
            "message": (
                "HealthMes could not complete the decision because a required "
                "internal component was unavailable."
            ),
            "detail": {
                "reason_codes": ["provider_execution_failed"],
            },
        }
        assert "private provider failure" not in response.text
    finally:
        engine.close()
        database.dispose()


def test_real_invalid_provider_query_reaches_rest_as_503(settings) -> None:
    database = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(database)
    factory = sessionmaker(
        bind=database,
        expire_on_commit=False,
    )
    registry = ContextProviderRegistry((InvalidNutritionProvider(),))
    access_layer = ContextAccessLayer(registry, clock=lambda: NOW)
    policy = ContextAccessPolicy(
        owner_principal_id="rest-owner",
        grants=(DomainAccessGrant(domain="nutrition"),),
    )
    agent = HealthMesDecisionAgent(
        access_layer=access_layer,
        runtime=ProviderFailureRuntime(),
        session_factory=factory,
        policy_resolver=lambda _request: policy,
        timeout_seconds=5,
        clock=lambda: NOW,
    )
    engine = HealthMesDecisionEngine(
        agent=agent,
        finalizer=DecisionFinalizer(
            access_layer=access_layer,
            session_factory=factory,
            policy_resolver=lambda _request: policy,
            clock=lambda: NOW,
        ),
    )
    app = create_app(_secured_settings(settings))
    try:
        with TestClient(
            app,
            base_url="http://127.0.0.1:8100",
            client=("127.0.0.1", 43123),
        ) as client:
            app.state.decision_engine = engine
            response = client.post(
                "/v1/wellness-decisions",
                headers=_bearer(),
                json={"question": "How much caffeine did I have?"},
            )

        assert response.status_code == 503
        assert response.json()["error"] == {
            "code": "decision_service_unavailable",
            "message": (
                "HealthMes could not complete the decision because a "
                "required internal component was unavailable."
            ),
            "detail": {
                "reason_codes": ["invalid_provider_query"],
            },
        }
        assert "private invalid provider query" not in response.text
    finally:
        engine.close()
        database.dispose()


@pytest.mark.parametrize(
    ("status_value", "limitation"),
    (
        (
            DecisionStatus.BLOCKED,
            "decision_step_budget_exhausted",
        ),
        (
            DecisionStatus.BLOCKED,
            "decision_tool_call_budget_exhausted",
        ),
        (
            DecisionStatus.BLOCKED,
            "turn_context_byte_budget_exhausted",
        ),
        (
            DecisionStatus.FAILED,
            "domain_consent_denied",
        ),
        (
            DecisionStatus.FAILED,
            "decision_source_ref_revalidation_failed",
        ),
    ),
)
def test_expected_safe_decision_stops_remain_structured_results(
    settings,
    status_value,
    limitation,
) -> None:
    app = create_app(settings)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    ) as client:
        app.state.decision_engine = FailedRuntimeDecisionEngine(
            limitation,
            status=status_value,
        )
        response = client.post(
            "/v1/wellness-decisions",
            json={"question": "Should I keep working?"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == status_value
    assert response.json()["limitations"] == [limitation]


def test_missing_hermes_responses_endpoint_fails_closed(
    settings,
) -> None:
    transport = MissingResponsesTransport()
    configured = settings.model_copy(
        update={
            "decision_hermes_base_url": "http://127.0.0.1:8644",
            "decision_hermes_model": MODEL,
            "decision_hermes_provider": PROVIDER,
        }
    )
    app = create_app(
        configured,
        decision_transport=transport,
    )

    with TestClient(
        app,
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    ) as client:
        response = client.post(
            "/v1/wellness-decisions",
            json={"question": "Should I keep working?"},
        )

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "decision_runtime_unavailable",
        "message": (
            "Hermes does not currently provide the required "
            "decision runtime."
        ),
        "detail": {
            "reason_codes": [
                "hermes_responses_endpoint_missing",
            ]
        },
    }
    assert transport.response_calls == 1
