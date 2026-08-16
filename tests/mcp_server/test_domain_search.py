from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastmcp.exceptions import ToolError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from healthmes.decision import (
    ActivityContextProvider,
    CalendarContextProvider,
    ContextAccessLayer,
    ContextCoverage,
    ContextFreshness,
    ContextProviderMetadata,
    ContextProviderRegistry,
    ContextResult,
    ContextStatus,
    CoverageStatus,
    DatabaseDecisionPolicyResolver,
    DecisionBudget,
    DecisionCaller,
    DecisionContextSearchSessionService,
    DecisionRequest,
    ExecutionScope,
    FreshnessStatus,
    NutritionContextProvider,
    SourceRef,
    WearableContextProvider,
    ensure_decision_domain_policies,
    update_decision_domain_policy,
)
from healthmes.hermes_mcp_inventory import (
    HERMES_DECISION_MCP_TOOL_NAMES,
    expected_hermes_mcp_inventory,
    schema_digests_from_mcp_tools,
    validate_model_visible_mcp_inventory,
)
from healthmes.mcp_server import server as server_module
from healthmes.store import WellnessEvent

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)
SEARCH_CAPABILITIES = {
    "search_activity": {
        "activity.summary",
        "activity.focus",
        "activity.overwork",
        "activity.recovery",
        "activity.timeline",
    },
    "search_nutrition": {
        "nutrition.intake-history",
        "nutrition.caffeine-ledger",
        "nutrition.decision-context",
    },
    "search_calendar": {
        "calendar.day-summary",
        "calendar.busy-intervals",
        "calendar.available-windows",
        "calendar.event-detail",
    },
    "search_wearable": {
        "wearable.readiness",
        "wearable.sleep",
        "wearable.recovery",
        "wearable.stress",
        "wearable.metric-detail",
    },
}
SEARCH_PROPERTIES = {
    "search_activity": {
        "decision_session_id",
        "capability",
        "start",
        "end",
        "date",
        "lookback_days",
        "cursor",
        "device_id",
        "platform",
        "granularity",
        "fields",
        "privacy_level",
        "limit",
    },
    "search_nutrition": {
        "decision_session_id",
        "capability",
        "start",
        "end",
        "date",
        "confirmed_only",
        "intent",
        "modality",
        "nutrient",
        "text_query",
        "request_id",
        "granularity",
        "fields",
        "privacy_level",
        "limit",
    },
    "search_calendar": {
        "decision_session_id",
        "capability",
        "start",
        "end",
        "date",
        "minimum_minutes",
        "cursor",
        "granularity",
        "fields",
        "privacy_level",
        "limit",
    },
    "search_wearable": {
        "decision_session_id",
        "capability",
        "start",
        "end",
        "date",
        "cursor",
        "kind",
        "metric",
        "granularity",
        "fields",
        "privacy_level",
        "limit",
    },
}
RESULT_KEYS = {
    "query_id",
    "provider_id",
    "capability",
    "status",
    "payload",
    "source_refs",
    "raw_sources",
    "observed_start",
    "observed_end",
    "collected_at",
    "freshness",
    "coverage",
    "limitations",
    "truncated",
    "next_cursor",
    "access_audit",
}
AUDIT_KEYS = {
    "query_id",
    "provider_id",
    "capability",
    "outcome",
    "occurred_at",
    "reason_codes",
    "redacted_paths",
    "requested_privacy_level",
    "effective_privacy_level",
    "requested_start",
    "requested_end",
    "effective_start",
    "effective_end",
    "requested_limit",
    "effective_limit",
    "source_ref_ids",
    "payload_bytes",
    "budget",
}


class MutableClock:
    def __init__(self) -> None:
        self.wall = NOW
        self.monotonic = 100.0

    def now(self) -> datetime:
        return self.wall

    def tick(self) -> float:
        return self.monotonic


class RollbackProbeProvider:
    def __init__(self, metadata: ContextProviderMetadata) -> None:
        self.metadata = metadata
        self.queries = []

    async def query(self, session, query, *, now):
        self.queries.append(query)
        session.add(
            WellnessEvent(
                event_type=f"{self.metadata.domain}.mcp-search-probe.v1",
                schema_version=1,
                observed_at=now,
                recorded_at=now,
                timezone=query.timezone,
                source_provider=f"{self.metadata.domain}-search-probe",
                source_device=None,
                source_record_id=str(uuid.uuid4()),
                capture_method="test",
                quality_flags={},
                confidence=1,
                coverage=1,
                sensitivity=self.metadata.domain,
                consent_scope="personal",
                expires_at=now + timedelta(days=1),
                payload={},
                raw_object_id=None,
                derived_from=None,
            )
        )
        session.flush()
        return ContextResult(
            query_id=query.query_id,
            provider_id=query.provider_id,
            capability=query.capability,
            status=ContextStatus.UNAVAILABLE,
            freshness=ContextFreshness(
                status=FreshnessStatus.UNAVAILABLE,
            ),
            coverage=ContextCoverage(
                status=CoverageStatus.UNAVAILABLE,
            ),
        )


class SensitiveNutritionProvider:
    metadata = NutritionContextProvider.metadata

    def __init__(self, source_ref: SourceRef) -> None:
        self.source_ref = source_ref
        self.queries = []

    async def query(self, session, query, *, now):
        del session
        self.queries.append(query)
        return ContextResult(
            query_id=query.query_id,
            provider_id=query.provider_id,
            capability=query.capability,
            status=ContextStatus.OK,
            payload={
                "status": "ok",
                "count": 1,
                "records": [
                    {
                        "interaction_id": "private-interaction",
                        "name": "Private meal",
                        "source_text": "raw package text",
                        "recorded_at": self.source_ref.collected_at.isoformat(),
                    }
                ],
            },
            source_refs=[self.source_ref],
            freshness=ContextFreshness(
                status=FreshnessStatus.CURRENT,
                as_of=now,
                age_seconds=0,
            ),
            coverage=ContextCoverage(
                status=CoverageStatus.COMPLETE,
                ratio=1,
            ),
            truncated=True,
            next_cursor="opaque-next-page",
        )


def _schema_value(schema: dict) -> dict:
    return next(
        (
            branch
            for branch in schema.get("anyOf", ())
            if branch.get("type") != "null"
        ),
        schema,
    )


def _request(*, max_tool_calls: int = 8) -> DecisionRequest:
    return DecisionRequest(
        question="Search retained domain context.",
        requested_at=NOW,
        timezone="UTC",
        caller=DecisionCaller(
            principal_id="owner",
            authenticated=True,
            execution_scope=ExecutionScope.LOCAL,
        ),
        budget=DecisionBudget(
            max_steps=max_tool_calls,
            max_tool_calls=max_tool_calls,
        ),
    )


def _database_service(
    store_factory: sessionmaker[Session],
    providers,
    clock: MutableClock,
    *,
    ttl_seconds: float = 60,
) -> DecisionContextSearchSessionService:
    with store_factory() as session:
        ensure_decision_domain_policies(session, "owner")
        session.commit()
    return DecisionContextSearchSessionService(
        access_layer=ContextAccessLayer(
            ContextProviderRegistry(providers),
            clock=clock.now,
        ),
        session_factory=store_factory,
        policy_resolver=DatabaseDecisionPolicyResolver(
            session_factory=store_factory,
            owner_principal_id="owner",
            execution_scope=ExecutionScope.LOCAL,
        ),
        ttl_seconds=ttl_seconds,
        clock=clock.now,
        monotonic_clock=clock.tick,
    )


def _seed_nutrition_source_ref(
    store_factory: sessionmaker[Session],
) -> SourceRef:
    interaction_id = uuid.uuid4()
    observed_at = NOW - timedelta(hours=1)
    recorded_at = observed_at + timedelta(minutes=1)
    with store_factory() as session:
        event = WellnessEvent(
            event_type="nutrition.interaction.v1",
            schema_version=1,
            observed_at=observed_at,
            recorded_at=recorded_at,
            timezone="UTC",
            source_provider="nutrition-interaction",
            source_device=None,
            source_record_id=str(interaction_id),
            capture_method="test",
            quality_flags={},
            confidence=1,
            coverage=1,
            sensitivity="nutrition",
            consent_scope="personal",
            expires_at=NOW + timedelta(days=1),
            payload={
                "interaction_id": str(interaction_id),
                "observed_at": observed_at.isoformat(),
            },
            raw_object_id=None,
            derived_from=None,
        )
        session.add(event)
        session.flush()
        event_id = event.id
        session.commit()
    return SourceRef(
        domain="nutrition",
        resource_type="nutrition.interaction.v1",
        record_id=str(event_id),
        source_provider="nutrition-interaction",
        observed_start=observed_at,
        collected_at=recorded_at,
        derived_by="nutrition.intake-history.v1",
        freshness=FreshnessStatus.CURRENT,
        coverage=1,
        sensitivity="nutrition",
    )


async def test_domain_search_schemas_are_exact_bounded_and_identity_safe(
    mcp_client,
) -> None:
    tools = {
        tool.name: tool
        for tool in await mcp_client.list_tools()
        if tool.name in SEARCH_CAPABILITIES
    }

    assert set(tools) == set(SEARCH_CAPABILITIES)
    for name, tool in tools.items():
        schema = tool.inputSchema
        properties = schema["properties"]
        assert set(properties) == SEARCH_PROPERTIES[name]
        assert set(schema["required"]) == {
            "decision_session_id",
            "capability",
        }
        assert not {
            "owner",
            "owner_id",
            "principal_id",
            "timezone",
            "provider_id",
            "sql",
            "query_sql",
        }.intersection(properties)
        assert set(properties["capability"]["enum"]) == (
            SEARCH_CAPABILITIES[name]
        )
        assert properties["decision_session_id"]["pattern"].startswith(
            "^dss_"
        )
        assert properties["limit"]["minimum"] == 1
        assert properties["limit"]["maximum"] == 1_000

    for name in SEARCH_CAPABILITIES:
        properties = tools[name].inputSchema["properties"]
        assert properties["privacy_level"]["enum"] == [
            "aggregate",
            "identity",
        ]
    for name in (
        "search_activity",
        "search_calendar",
        "search_wearable",
    ):
        properties = tools[name].inputSchema["properties"]
        cursor = _schema_value(properties["cursor"])
        assert cursor["minLength"] == 69
        assert cursor["maxLength"] == 69
        assert cursor["pattern"] == "^hmc1_[0-9a-f]{64}$"
    assert (
        "cursor"
        not in tools["search_nutrition"].inputSchema["properties"]
    )
    activity = tools["search_activity"].inputSchema["properties"]
    assert _schema_value(activity["lookback_days"])["maximum"] == 90
    assert _schema_value(activity["device_id"])["maxLength"] == 255
    assert set(_schema_value(activity["platform"])["enum"]) == {
        "android",
        "ios",
        "linux",
        "macos",
        "unknown",
        "windows",
    }
    calendar = tools["search_calendar"].inputSchema["properties"]
    assert _schema_value(calendar["minimum_minutes"])["maximum"] == 1_440
    nutrition = tools["search_nutrition"].inputSchema["properties"]
    assert _schema_value(nutrition["text_query"])["maxLength"] == 500
    wearable = tools["search_wearable"].inputSchema["properties"]
    assert set(_schema_value(wearable["kind"])["enum"]) == {
        "load",
        "recovery",
        "sleep",
        "stress",
    }
    assert set(_schema_value(wearable["metric"])["enum"]) == {
        "actual_sleep",
        "charge",
        "hrv",
        "sleep_debt",
        "stress",
        "yesterday_load",
    }


async def test_live_healthmes_mcp_schemas_match_decision_runtime_inventory(
    mcp_client,
) -> None:
    tools = await mcp_client.list_tools()
    schema_digests = schema_digests_from_mcp_tools(
        tools,
        included_names=HERMES_DECISION_MCP_TOOL_NAMES,
    )

    assert (
        validate_model_visible_mcp_inventory(schema_digests)
        == expected_hermes_mcp_inventory()
    )


async def test_four_tools_share_one_budget_and_always_roll_back(
    mcp_client,
    call_tool,
    store_factory,
) -> None:
    clock = MutableClock()
    providers = [
        RollbackProbeProvider(ActivityContextProvider.metadata),
        RollbackProbeProvider(NutritionContextProvider.metadata),
        RollbackProbeProvider(CalendarContextProvider.metadata),
        RollbackProbeProvider(WearableContextProvider.metadata),
    ]
    service = _database_service(store_factory, providers, clock)
    server_module.set_decision_search_session_service(service)
    handle = service.begin(_request(max_tool_calls=4))
    calls = (
        (
            "search_activity",
            {
                "decision_session_id": handle.session_id,
                "capability": "activity.summary",
                "date": "2026-08-16",
            },
        ),
        (
            "search_nutrition",
            {
                "decision_session_id": handle.session_id,
                "capability": "nutrition.intake-history",
                "start": "2026-08-15T12:00:00Z",
                "end": "2026-08-16T12:00:00Z",
                "limit": 1_000,
            },
        ),
        (
            "search_calendar",
            {
                "decision_session_id": handle.session_id,
                "capability": "calendar.available-windows",
                "date": "2026-08-16",
                "minimum_minutes": 30,
            },
        ),
        (
            "search_wearable",
            {
                "decision_session_id": handle.session_id,
                "capability": "wearable.readiness",
                "date": "2026-08-16",
            },
        ),
    )

    results = [
        await call_tool(mcp_client, tool_name, arguments)
        for tool_name, arguments in calls
    ]

    assert [result["status"] for result in results] == [
        "failed",
        "failed",
        "failed",
        "failed",
    ]
    assert all(
        "provider_execution_failed" in result["limitations"]
        for result in results
    )
    assert [
        result["access_audit"]["budget"]["tool_calls_used"]
        for result in results
    ] == [1, 2, 3, 4]
    assert results[1]["access_audit"]["effective_limit"] == 250
    assert "query_limit_trimmed" in results[1]["limitations"]
    for result in results:
        assert set(result) == RESULT_KEYS
        assert set(result["access_audit"]) == AUDIT_KEYS
        assert set(result["access_audit"]["budget"]) == {
            "tool_calls_used",
            "tool_calls_limit",
            "context_bytes_used",
            "context_bytes_limit",
            "source_refs_used",
            "source_refs_limit",
        }
        json.dumps(result)

    with pytest.raises(
        ToolError,
        match="decision_search_tool_call_budget_exhausted",
    ):
        await mcp_client.call_tool(
            "search_activity",
            {
                "decision_session_id": handle.session_id,
                "capability": "activity.focus",
                "date": "2026-08-16",
            },
        )
    assert sum(len(provider.queries) for provider in providers) == 4
    assert {
        query.timezone
        for provider in providers
        for query in provider.queries
    } == {"UTC"}
    with store_factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(WellnessEvent)
            )
            == 0
        )


async def test_unknown_expired_and_input_bounds_fail_before_provider_access(
    mcp_client,
    store_factory,
) -> None:
    clock = MutableClock()
    provider = RollbackProbeProvider(ActivityContextProvider.metadata)
    service = _database_service(
        store_factory,
        (provider,),
        clock,
        ttl_seconds=2,
    )
    server_module.set_decision_search_session_service(service)
    arguments = {
        "decision_session_id": "dss_" + "x" * 43,
        "capability": "activity.overwork",
        "date": "2026-08-16",
    }

    with pytest.raises(
        ToolError,
        match="decision_search_session_unknown",
    ):
        await mcp_client.call_tool("search_activity", arguments)

    handle = service.begin(_request())
    arguments["decision_session_id"] = handle.session_id
    with pytest.raises(ToolError):
        await mcp_client.call_tool(
            "search_activity",
            {**arguments, "lookback_days": 91},
        )
    with pytest.raises(ToolError):
        await mcp_client.call_tool(
            "search_activity",
            {**arguments, "limit": 0},
        )
    with pytest.raises(ToolError):
        await mcp_client.call_tool(
            "search_activity",
            {**arguments, "cursor": "not-an-opaque-cursor"},
        )

    clock.monotonic += 3
    clock.wall += timedelta(seconds=3)
    with pytest.raises(
        ToolError,
        match="decision_search_session_expired",
    ):
        await mcp_client.call_tool("search_activity", arguments)
    assert provider.queries == []


async def test_source_refs_redaction_cursor_and_current_db_consent(
    mcp_client,
    call_tool,
    store_factory,
) -> None:
    clock = MutableClock()
    provider = SensitiveNutritionProvider(
        _seed_nutrition_source_ref(store_factory)
    )
    service = _database_service(store_factory, (provider,), clock)
    server_module.set_decision_search_session_service(service)
    handle = service.begin(_request())
    base_arguments = {
        "decision_session_id": handle.session_id,
        "capability": "nutrition.intake-history",
        "start": "2026-08-15T12:00:00Z",
        "end": "2026-08-16T12:00:00Z",
        "limit": 1,
    }

    with pytest.raises(
        ToolError,
        match="decision_search_query_invalid",
    ):
        await mcp_client.call_tool(
            "search_nutrition",
            {
                "decision_session_id": handle.session_id,
                "capability": "nutrition.decision-context",
                "request_id": str(uuid.uuid4()),
            },
        )
    assert provider.queries == []

    result = await call_tool(
        mcp_client,
        "search_nutrition",
        base_arguments,
    )

    serialized = json.dumps(result)
    assert result["status"] == "partial"
    assert result["truncated"] is True
    assert result["next_cursor"] == "opaque-next-page"
    assert len(result["source_refs"]) == 1
    assert result["access_audit"]["source_ref_ids"] == [
        result["source_refs"][0]["reference_id"]
    ]
    assert result["coverage"] == {"status": "complete", "ratio": 1.0}
    assert result["freshness"]["status"] == "current"
    assert "Private meal" not in serialized
    assert "raw package text" not in serialized
    assert "privacy_fields_redacted" in result["limitations"]

    identity = await call_tool(
        mcp_client,
        "search_nutrition",
        {
            **base_arguments,
            "privacy_level": "identity",
            "fields": ["records"],
        },
    )
    assert identity["status"] == "denied"
    assert identity["limitations"] == [
        "privacy_implicit_escalation_denied"
    ]

    with store_factory() as session:
        update_decision_domain_policy(
            session,
            "owner",
            "nutrition",
            enabled=False,
        )
        session.commit()
    denied = await call_tool(
        mcp_client,
        "search_nutrition",
        {
            "decision_session_id": handle.session_id,
            "capability": "nutrition.caffeine-ledger",
            "date": "2026-08-16",
        },
    )
    assert denied["status"] == "denied"
    assert denied["limitations"] == ["domain_consent_denied"]
    assert denied["payload"] == {}
    assert denied["source_refs"] == []
    assert len(provider.queries) == 1
