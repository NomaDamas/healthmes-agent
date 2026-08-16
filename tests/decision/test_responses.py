from __future__ import annotations

import asyncio
import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from healthmes.app import create_app
from healthmes.decision import (
    HERMES_DECISION_MCP_TOOL_NAMES,
    HERMES_DECISION_NATIVE_TOOLSET_DENYLIST,
    AccessAuditEntry,
    AccessOutcome,
    ContextQuery,
    ContextResult,
    ContextSearchAccessAudit,
    ContextSearchResult,
    ContextStatus,
    DecisionBudget,
    DecisionCaller,
    DecisionRequest,
    DecisionSearchBudgetUsage,
    DecisionStatus,
    ExecutionScope,
    HermesDecisionProfileAssertion,
    HermesHttpResponsesTransport,
    HermesResponsesContractError,
    HermesResponsesDecisionAgent,
    HermesResponsesHttpResult,
    HermesResponsesTransportError,
    PrivacyLevel,
    SourceRef,
    ToolCallRecord,
    ToolCallStatus,
    source_ref_id,
)
from healthmes.decision.responses import (
    HERMES_DECISION_DRAFT_SCHEMA,
    HERMES_MODELS_PATH,
    HERMES_RESPONSES_PATH,
    HERMES_SESSION_PATH,
    HERMES_TOOLSETS_PATH,
)
from healthmes.mcp_server import server as mcp_server

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)
MODEL = "decision-model"
PROVIDER = "decision-provider"
DECISION_SESSION_ID = "ds_" + "a" * 32
HERMES_SESSION_ID = "hermes-session-1"


def _request() -> DecisionRequest:
    return DecisionRequest(
        question="Should I take a break now?",
        requested_at=NOW,
        timezone="UTC",
        caller=DecisionCaller(
            principal_id="owner",
            authenticated=True,
            execution_scope=ExecutionScope.LOCAL,
            channel="rest",
        ),
        budget=DecisionBudget(
            max_steps=4,
            max_tool_calls=4,
            max_source_refs=20,
            max_context_bytes=64_000,
        ),
    )


def _toolsets(*, native_enabled: bool = False) -> dict:
    return {
        "object": "list",
        "platform": "api_server",
        "data": [
            {
                "name": "terminal",
                "label": "Terminal",
                "description": "Native terminal tools.",
                "enabled": native_enabled,
                "configured": True,
                "tools": ["terminal"],
            }
        ],
    }


def _models(*, include_route: bool = True) -> dict:
    data = [
        {
            "id": "healthmes-decision-runtime",
            "object": "model",
            "created": int(NOW.timestamp()),
            "owned_by": "hermes",
            "permission": [],
            "root": "healthmes-decision-runtime",
            "parent": None,
        }
    ]
    if include_route:
        data.append(
            {
                "id": MODEL,
                "object": "model",
                "created": int(NOW.timestamp()),
                "owned_by": "hermes",
                "permission": [],
                "root": MODEL,
                "parent": "healthmes-decision-runtime",
            }
        )
    return {"object": "list", "data": data}


def _decision_profile(
    path: Path,
    *,
    tools: tuple[str, ...] = HERMES_DECISION_MCP_TOOL_NAMES,
    extra_server: bool = False,
    enabled: bool | None = None,
    profile_api_key: str = "k" * 64,
    expected_api_key: str | None = None,
) -> HermesDecisionProfileAssertion:
    servers: dict[str, dict] = {
        "healthmes": {
            "url": "http://127.0.0.1:8100/mcp",
            "tools": {"include": list(tools)},
        }
    }
    if enabled is not None:
        servers["healthmes"]["enabled"] = enabled
    if extra_server:
        servers["open_wearables"] = {
            "command": "unsafe",
        }
    path.write_text(
        yaml.safe_dump(
            {
                "platforms": {
                    "api_server": {
                        "enabled": True,
                        "extra": {
                            "key": profile_api_key,
                            "model_name": "healthmes-decision-runtime",
                            "model_routes": {
                                MODEL: {
                                    "model": MODEL,
                                    "provider": PROVIDER,
                                }
                            },
                        },
                    }
                },
                "platform_toolsets": {
                    "api_server": ["healthmes"],
                },
                "agent": {
                    "disabled_toolsets": sorted(
                        HERMES_DECISION_NATIVE_TOOLSET_DENYLIST
                    )
                },
                "mcp_servers": servers,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return HermesDecisionProfileAssertion(
        path,
        expected_model=MODEL,
        expected_provider=PROVIDER,
        expected_api_key=expected_api_key or profile_api_key,
    )


def _final_response(
    decision: dict,
    *,
    output_prefix: list[dict] | None = None,
) -> dict:
    envelope = {
        "schema": HERMES_DECISION_DRAFT_SCHEMA,
        "decision": decision,
    }
    return {
        "id": "resp_test",
        "object": "response",
        "status": "completed",
        "created_at": int(NOW.timestamp()),
        "model": MODEL,
        "output": [
            *(output_prefix or []),
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(envelope),
                    }
                ],
            },
        ],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        },
    }


def _sse_body(response: dict) -> bytes:
    sequence = 0
    frames: list[str] = []

    def emit(event_type: str, payload: dict) -> None:
        nonlocal sequence
        event = {
            "type": event_type,
            **payload,
            "sequence_number": sequence,
        }
        sequence += 1
        frames.append(
            f"event: {event_type}\n"
            f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
        )

    emit(
        "response.created",
        {
            "response": {
                "id": response["id"],
                "object": "response",
                "status": "in_progress",
                "created_at": response["created_at"],
                "model": response["model"],
                "output": [],
            }
        },
    )
    for output_index, item in enumerate(response["output"]):
        item_id = f"item-{output_index}"
        item_type = item["type"]
        if item_type == "function_call":
            added = {
                "id": item_id,
                **item,
                "status": "in_progress",
            }
            done = {**added, "status": "completed"}
        elif item_type == "function_call_output":
            output = [
                {
                    "type": "input_text",
                    "text": item["output"],
                }
            ]
            added = {
                "id": item_id,
                "type": item_type,
                "call_id": item["call_id"],
                "output": output,
                "status": "completed",
            }
            done = dict(added)
        else:
            text = item["content"][0]["text"]
            added = {
                "id": item_id,
                "type": "message",
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            }
            emit(
                "response.output_item.added",
                {"output_index": output_index, "item": added},
            )
            emit(
                "response.output_text.done",
                {
                    "item_id": item_id,
                    "output_index": output_index,
                    "content_index": 0,
                    "text": text,
                    "logprobs": [],
                },
            )
            done = {
                "id": item_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": item["content"],
            }
            emit(
                "response.output_item.done",
                {"output_index": output_index, "item": done},
            )
            continue
        emit(
            "response.output_item.added",
            {"output_index": output_index, "item": added},
        )
        emit(
            "response.output_item.done",
            {"output_index": output_index, "item": done},
        )
    emit("response.completed", {"response": response})
    return "".join(frames).encode()


def _empty_snapshot() -> SimpleNamespace:
    return SimpleNamespace(
        session_id=DECISION_SESSION_ID,
        source_refs=(),
        access_trace=(),
        tool_trace=(),
    )


def _activity_trace(
    record_id: str = "2026-08-16",
    *,
    active_seconds: int = 3_600,
) -> tuple[
    ToolCallRecord,
    ContextSearchResult,
    SourceRef,
]:
    source_ref = SourceRef(
        reference_id=source_ref_id(
            domain="activity",
            resource_type="activity_summary",
            source_provider="healthmes",
            record_id=record_id,
        ),
        domain="activity",
        resource_type="activity_summary",
        record_id=record_id,
        source_provider="healthmes",
        observed_start=NOW - timedelta(hours=1),
        observed_end=NOW,
        collected_at=NOW,
    )
    query = ContextQuery(
        provider_id="activity",
        capability="activity.summary",
        start=NOW - timedelta(hours=1),
        end=NOW,
        timezone="UTC",
        fields=["active_seconds"],
        privacy_level=PrivacyLevel.AGGREGATE,
        limit=10,
    )
    context = ContextResult(
        query_id=query.query_id,
        provider_id=query.provider_id,
        capability=query.capability,
        status=ContextStatus.OK,
        payload={"active_seconds": active_seconds},
        source_refs=[source_ref],
    )
    audit = AccessAuditEntry(
        query_id=query.query_id,
        provider_id=query.provider_id,
        capability=query.capability,
        outcome=AccessOutcome.ALLOWED,
        occurred_at=NOW,
        requested_privacy_level=PrivacyLevel.AGGREGATE,
        effective_privacy_level=PrivacyLevel.AGGREGATE,
        requested_start=query.start,
        requested_end=query.end,
        effective_start=query.start,
        effective_end=query.end,
        requested_limit=query.limit,
        effective_limit=query.limit,
        source_ref_ids=(source_ref.reference_id,),
        payload_bytes=32,
    )
    search_result = ContextSearchResult(
        **context.model_dump(mode="python", round_trip=True),
        access_audit=ContextSearchAccessAudit(
            **audit.model_dump(mode="python", round_trip=True),
            budget=DecisionSearchBudgetUsage(
                tool_calls_used=1,
                tool_calls_limit=4,
                context_bytes_used=32,
                context_bytes_limit=64_000,
                source_refs_used=1,
                source_refs_limit=20,
            ),
        ),
    )
    trace = ToolCallRecord(
        query=query,
        status=ToolCallStatus.COMPLETED,
        started_at=NOW,
        finished_at=NOW,
        result=context,
    )
    return trace, search_result, source_ref


class _SearchService:
    def __init__(self, snapshot: SimpleNamespace) -> None:
        self.snapshot = snapshot
        self.begun = 0
        self.finished = 0
        self.aborted = 0
        self.closed = 0

    def begin(self, _request: DecisionRequest) -> SimpleNamespace:
        self.begun += 1
        return SimpleNamespace(session_id=self.snapshot.session_id)

    def finish(self, session_id: str) -> SimpleNamespace:
        assert session_id == self.snapshot.session_id
        self.finished += 1
        return self.snapshot

    def abort(self, session_id: str) -> SimpleNamespace:
        assert session_id == self.snapshot.session_id
        self.aborted += 1
        return self.snapshot

    def close(self) -> None:
        self.closed += 1


class _Transport:
    def __init__(
        self,
        response: dict,
        *,
        native_enabled: bool = False,
        include_model_route: bool = True,
        cleanup_failures: int = 0,
        sessions: list[dict] | None = None,
    ) -> None:
        self.response = response
        self.native_enabled = native_enabled
        self.include_model_route = include_model_route
        self.cleanup_failures = cleanup_failures
        self.sessions = list(sessions or [])
        self.toolset_calls = 0
        self.model_calls = 0
        self.session_list_calls: list[tuple[int, int]] = []
        self.response_calls: list[dict] = []
        self.deleted_sessions: list[str] = []

    async def get_toolsets(self) -> dict:
        self.toolset_calls += 1
        return _toolsets(native_enabled=self.native_enabled)

    async def get_models(self) -> dict:
        self.model_calls += 1
        return _models(include_route=self.include_model_route)

    async def create_response(
        self,
        payload,
        *,
        timeout_seconds: float,
    ) -> HermesResponsesHttpResult:
        assert timeout_seconds > 0
        self.response_calls.append(dict(payload))
        return HermesResponsesHttpResult(
            payload=self.response,
            session_id=HERMES_SESSION_ID,
        )

    async def list_sessions(
        self,
        *,
        limit: int,
        offset: int,
    ) -> dict:
        self.session_list_calls.append((limit, offset))
        page = self.sessions[offset : offset + limit]
        return {
            "object": "list",
            "data": page,
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < len(self.sessions),
        }

    async def delete_session(self, session_id: str) -> None:
        self.deleted_sessions.append(session_id)
        if len(self.deleted_sessions) <= self.cleanup_failures:
            raise RuntimeError("transient cleanup failure")


def _tool_items(
    search_result: ContextSearchResult,
    *,
    call_id: str,
    trace: ToolCallRecord,
) -> list[dict]:
    return [
        {
            "type": "function_call",
            "name": "mcp__healthmes__search_activity",
            "arguments": json.dumps(
                {
                    "decision_session_id": DECISION_SESSION_ID,
                    "capability": trace.query.capability,
                    "start": trace.query.start.isoformat(),
                    "end": trace.query.end.isoformat(),
                    "granularity": trace.query.granularity,
                    "fields": trace.query.fields,
                    "privacy_level": trace.query.privacy_level.value,
                    "limit": trace.query.limit,
                }
            ),
            "call_id": call_id,
        },
        {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(
                {
                    "structuredContent": search_result.model_dump(
                        mode="json",
                        round_trip=True,
                    )
                }
            ),
        },
    ]


@pytest.mark.asyncio
async def test_agent_uses_one_responses_call_and_cleans_session() -> None:
    transport = _Transport(
        _final_response(
            {
                "status": "completed",
                "answer": "Take a short break.",
            }
        ),
        cleanup_failures=1,
    )
    search = _SearchService(_empty_snapshot())
    agent = HermesResponsesDecisionAgent(
        transport=transport,
        search_service=search,  # type: ignore[arg-type]
        model=MODEL,
        provider=PROVIDER,
        timeout_seconds=5,
        cleanup_retry_seconds=0,
        clock=lambda: NOW,
    )

    run = await agent.ask(_request())

    assert run.draft.status is DecisionStatus.COMPLETED
    assert run.draft.answer == "Take a short break."
    assert run.steps_used == 1
    assert transport.toolset_calls == 1
    assert transport.model_calls == 1
    assert len(transport.response_calls) == 1
    payload = transport.response_calls[0]
    assert payload["store"] is False
    assert payload["stream"] is True
    assert "previous_response_id" not in payload
    assert "conversation" not in payload
    serialized_request = json.loads(payload["input"][0]["content"])
    assert serialized_request["request_id"] == str(run.request_id)
    assert serialized_request["decision_session_id"] == DECISION_SESSION_ID
    assert serialized_request["caller"] == {
        "channel": "rest",
        "execution_scope": "local",
    }
    assert transport.deleted_sessions == [
        HERMES_SESSION_ID,
        HERMES_SESSION_ID,
    ]
    assert search.finished == 1
    assert search.aborted == 0


@pytest.mark.asyncio
async def test_agent_rejects_transcript_order_that_disagrees_with_trace() -> None:
    first_trace, first_result, first_ref = _activity_trace(
        "2026-08-16:first",
        active_seconds=1_800,
    )
    second_trace, second_result, second_ref = _activity_trace(
        "2026-08-16:second",
        active_seconds=900,
    )
    transport = _Transport(
        _final_response(
            {
                "status": "completed",
                "answer": "The trace order must be canonical.",
                "used_source_ref_ids": [
                    first_ref.reference_id,
                    second_ref.reference_id,
                ],
            },
            output_prefix=[
                *_tool_items(
                    first_result,
                    call_id="call-1",
                    trace=first_trace,
                ),
                *_tool_items(
                    second_result,
                    call_id="call-2",
                    trace=second_trace,
                ),
            ],
        )
    )
    search = _SearchService(
        SimpleNamespace(
            session_id=DECISION_SESSION_ID,
            source_refs=(first_ref, second_ref),
            access_trace=(
                first_result.access_audit,
                second_result.access_audit,
            ),
            tool_trace=(second_trace, first_trace),
        )
    )
    agent = HermesResponsesDecisionAgent(
        transport=transport,
        search_service=search,  # type: ignore[arg-type]
        model=MODEL,
        provider=PROVIDER,
        timeout_seconds=5,
        clock=lambda: NOW,
    )

    run = await agent.ask(_request())

    assert run.draft.status is DecisionStatus.FAILED
    assert run.draft.limitations == [
        "hermes_tool_trace_order_mismatch"
    ]
    assert search.finished == 1
    assert search.aborted == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output_prefix", "expected_code"),
    [
        (
            [
                {
                    "type": "function_call_output",
                    "call_id": "missing-call",
                    "output": "{}",
                }
            ],
            "hermes_tool_pair_invalid",
        ),
        (
            [
                {
                    "type": "function_call",
                    "name": "mcp__healthmes__search_activity",
                    "arguments": json.dumps(
                        {
                            "decision_session_id": DECISION_SESSION_ID,
                        }
                    ),
                    "call_id": "duplicate-call",
                },
                {
                    "type": "function_call",
                    "name": "mcp__healthmes__search_activity",
                    "arguments": json.dumps(
                        {
                            "decision_session_id": DECISION_SESSION_ID,
                        }
                    ),
                    "call_id": "duplicate-call",
                },
            ],
            "hermes_tool_pair_invalid",
        ),
        (
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {
                                    "schema": (
                                        HERMES_DECISION_DRAFT_SCHEMA
                                    ),
                                    "decision": {
                                        "status": "completed",
                                        "answer": "Too early.",
                                    },
                                }
                            ),
                        }
                    ],
                },
                {
                    "type": "function_call",
                    "name": "mcp__healthmes__search_activity",
                    "arguments": json.dumps(
                        {
                            "decision_session_id": DECISION_SESSION_ID,
                        }
                    ),
                    "call_id": "late-call",
                },
            ],
            "hermes_transcript_order_invalid",
        ),
    ],
)
async def test_agent_rejects_invalid_tool_pairing_and_order(
    output_prefix: list[dict],
    expected_code: str,
) -> None:
    transport = _Transport(
        _final_response(
            {
                "status": "completed",
                "answer": "This transcript is invalid.",
            },
            output_prefix=output_prefix,
        )
    )
    search = _SearchService(_empty_snapshot())
    agent = HermesResponsesDecisionAgent(
        transport=transport,
        search_service=search,  # type: ignore[arg-type]
        model=MODEL,
        provider=PROVIDER,
        timeout_seconds=5,
        clock=lambda: NOW,
    )

    run = await agent.ask(_request())

    assert run.draft.status is DecisionStatus.FAILED
    assert run.draft.limitations == [expected_code]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("final_text", "expected_code"),
    [
        (
            "```json\n"
            '{"schema":"healthmes.decision-draft.v1",'
            '"decision":{"status":"completed","answer":"No."}}\n```',
            "hermes_final_json_invalid",
        ),
        (
            '{"schema":"healthmes.decision-draft.v1",'
            '"decision":{"status":"completed","answer":"No."},'
            '"unexpected":true}',
            "hermes_final_json_invalid",
        ),
        (
            '{"schema":"healthmes.decision-draft.v1",',
            "hermes_final_json_invalid",
        ),
        (
            "x" * 64_001,
            "hermes_response_contract_invalid",
        ),
    ],
)
async def test_agent_rejects_malformed_or_oversized_final_output(
    final_text: str,
    expected_code: str,
) -> None:
    response = _final_response(
        {
            "status": "completed",
            "answer": "This value is replaced.",
        }
    )
    response["output"][-1]["content"][0]["text"] = final_text
    transport = _Transport(response)
    agent = HermesResponsesDecisionAgent(
        transport=transport,
        search_service=_SearchService(_empty_snapshot()),  # type: ignore[arg-type]
        model=MODEL,
        provider=PROVIDER,
        timeout_seconds=5,
        clock=lambda: NOW,
    )

    run = await agent.ask(_request())

    assert run.draft.status is DecisionStatus.FAILED
    assert run.draft.limitations == [expected_code]


@pytest.mark.asyncio
async def test_dedicated_profile_purges_stale_sessions_and_sets_digest(
    tmp_path: Path,
) -> None:
    stale_id = "stale-failed-session"
    fresh_id = "fresh-session"
    transport = _Transport(
        _final_response(
            {
                "status": "completed",
                "answer": "Take a short break.",
            }
        ),
        sessions=[
            {
                "id": fresh_id,
                "source": "api_server",
                "started_at": NOW.timestamp(),
                "last_active": NOW.timestamp(),
            },
            {
                "id": stale_id,
                "source": "api_server",
                "started_at": (NOW - timedelta(hours=1)).timestamp(),
                "last_active": (NOW - timedelta(hours=1)).timestamp(),
            },
        ],
    )
    agent = HermesResponsesDecisionAgent(
        transport=transport,
        search_service=_SearchService(_empty_snapshot()),  # type: ignore[arg-type]
        model=MODEL,
        provider=PROVIDER,
        timeout_seconds=5,
        session_ttl_seconds=900,
        session_purge_interval_seconds=60,
        profile_assertion=_decision_profile(
            tmp_path / "decision-config.yaml"
        ),
        clock=lambda: NOW,
    )

    try:
        run = await agent.ask(_request())
    finally:
        await agent.aclose()

    assert run.draft.status is DecisionStatus.COMPLETED
    assert transport.session_list_calls == [(200, 0)]
    assert transport.deleted_sessions == [
        stale_id,
        HERMES_SESSION_ID,
    ]
    metadata = transport.response_calls[0]["metadata"]
    assert metadata["healthmes_request_id"] == str(run.request_id)
    assert metadata["healthmes_turn_id"] == str(run.turn_id)
    assert len(metadata["healthmes_profile_digest"]) == 64


@pytest.mark.asyncio
async def test_session_maintenance_cursor_reaches_older_pages(
    tmp_path: Path,
) -> None:
    fresh_sessions = [
        {
            "id": f"fresh-{index}",
            "source": "api_server",
            "started_at": NOW.timestamp(),
            "last_active": NOW.timestamp(),
        }
        for index in range(1_001)
    ]
    stale_id = "stale-after-first-five-pages"
    transport = _Transport(
        _final_response(
            {
                "status": "completed",
                "answer": "Unused.",
            }
        ),
        sessions=[
            *fresh_sessions,
            {
                "id": stale_id,
                "source": "api_server",
                "started_at": (NOW - timedelta(hours=1)).timestamp(),
                "last_active": (NOW - timedelta(hours=1)).timestamp(),
            },
        ],
    )
    agent = HermesResponsesDecisionAgent(
        transport=transport,
        search_service=_SearchService(_empty_snapshot()),  # type: ignore[arg-type]
        model=MODEL,
        provider=PROVIDER,
        timeout_seconds=5,
        session_ttl_seconds=900,
        session_purge_interval_seconds=60,
        session_purge_max_pages=5,
        profile_assertion=_decision_profile(
            tmp_path / "decision-config.yaml"
        ),
        clock=lambda: NOW,
    )

    try:
        await agent.start()
        await agent._purge_expired_sessions()
    finally:
        await agent.aclose()

    assert transport.session_list_calls == [
        (200, 0),
        (200, 200),
        (200, 400),
        (200, 600),
        (200, 800),
        (200, 1_000),
    ]
    assert transport.deleted_sessions == [stale_id]


@pytest.mark.asyncio
async def test_dedicated_profile_drift_fails_before_hermes_probe(
    tmp_path: Path,
) -> None:
    transport = _Transport(
        _final_response(
            {
                "status": "completed",
                "answer": "This must not execute.",
            }
        )
    )
    agent = HermesResponsesDecisionAgent(
        transport=transport,
        search_service=_SearchService(_empty_snapshot()),  # type: ignore[arg-type]
        model=MODEL,
        provider=PROVIDER,
        timeout_seconds=5,
        session_ttl_seconds=900,
        profile_assertion=_decision_profile(
            tmp_path / "unsafe.yaml",
            extra_server=True,
        ),
        clock=lambda: NOW,
    )

    with pytest.raises(
        HermesResponsesContractError,
        match="hermes_decision_profile_mcp_invalid",
    ):
        await agent.start()

    assert transport.toolset_calls == 0
    assert transport.session_list_calls == []
    assert transport.response_calls == []


@pytest.mark.asyncio
async def test_dedicated_profile_api_key_mismatch_fails_before_probe(
    tmp_path: Path,
) -> None:
    transport = _Transport(
        _final_response(
            {
                "status": "completed",
                "answer": "This must not execute.",
            }
        )
    )
    agent = HermesResponsesDecisionAgent(
        transport=transport,
        search_service=_SearchService(_empty_snapshot()),  # type: ignore[arg-type]
        model=MODEL,
        provider=PROVIDER,
        timeout_seconds=5,
        session_ttl_seconds=900,
        profile_assertion=_decision_profile(
            tmp_path / "wrong-key.yaml",
            expected_api_key="x" * 64,
        ),
        clock=lambda: NOW,
    )

    with pytest.raises(
        HermesResponsesContractError,
        match="hermes_decision_profile_auth_mismatch",
    ):
        await agent.start()

    assert transport.toolset_calls == 0
    assert transport.model_calls == 0
    assert transport.response_calls == []


@pytest.mark.asyncio
async def test_disabled_healthmes_mcp_fails_before_hermes_probe(
    tmp_path: Path,
) -> None:
    transport = _Transport(
        _final_response(
            {
                "status": "completed",
                "answer": "This must not execute.",
            }
        )
    )
    agent = HermesResponsesDecisionAgent(
        transport=transport,
        search_service=_SearchService(_empty_snapshot()),  # type: ignore[arg-type]
        model=MODEL,
        provider=PROVIDER,
        timeout_seconds=5,
        session_ttl_seconds=900,
        profile_assertion=_decision_profile(
            tmp_path / "disabled.yaml",
            enabled=False,
        ),
        clock=lambda: NOW,
    )

    with pytest.raises(
        HermesResponsesContractError,
        match="hermes_decision_profile_mcp_invalid",
    ):
        await agent.start()

    assert transport.toolset_calls == 0
    assert transport.model_calls == 0
    assert transport.response_calls == []


@pytest.mark.asyncio
async def test_missing_dedicated_profile_fails_before_hermes_probe(
    tmp_path: Path,
) -> None:
    transport = _Transport(
        _final_response(
            {
                "status": "completed",
                "answer": "This must not execute.",
            }
        )
    )
    agent = HermesResponsesDecisionAgent(
        transport=transport,
        search_service=_SearchService(_empty_snapshot()),  # type: ignore[arg-type]
        model=MODEL,
        provider=PROVIDER,
        timeout_seconds=5,
        session_ttl_seconds=900,
        profile_assertion=HermesDecisionProfileAssertion(
            tmp_path / "missing-config.yaml",
            expected_model=MODEL,
            expected_provider=PROVIDER,
            expected_api_key="k" * 64,
        ),
        clock=lambda: NOW,
    )

    with pytest.raises(
        HermesResponsesContractError,
        match="hermes_decision_profile_missing",
    ):
        await agent.start()

    assert transport.toolset_calls == 0
    assert transport.model_calls == 0
    assert transport.session_list_calls == []
    assert transport.response_calls == []


@pytest.mark.asyncio
async def test_dedicated_profile_rejects_malformed_session_page(
    tmp_path: Path,
) -> None:
    class MalformedSessionTransport(_Transport):
        async def list_sessions(
            self,
            *,
            limit: int,
            offset: int,
        ) -> dict:
            self.session_list_calls.append((limit, offset))
            return {
                "object": "list",
                "data": "not-a-list",
                "limit": limit,
                "offset": offset,
                "has_more": False,
            }

    transport = MalformedSessionTransport(
        _final_response(
            {
                "status": "completed",
                "answer": "This must not execute.",
            }
        )
    )
    agent = HermesResponsesDecisionAgent(
        transport=transport,
        search_service=_SearchService(_empty_snapshot()),  # type: ignore[arg-type]
        model=MODEL,
        provider=PROVIDER,
        timeout_seconds=5,
        session_ttl_seconds=900,
        profile_assertion=_decision_profile(
            tmp_path / "decision-config.yaml"
        ),
        clock=lambda: NOW,
    )

    with pytest.raises(
        HermesResponsesContractError,
        match="hermes_session_list_invalid",
    ):
        await agent.start()

    assert transport.toolset_calls == 1
    assert transport.response_calls == []


@pytest.mark.asyncio
async def test_agent_timeout_aborts_search_session() -> None:
    class SlowTransport(_Transport):
        async def create_response(
            self,
            payload,
            *,
            timeout_seconds: float,
        ) -> HermesResponsesHttpResult:
            self.response_calls.append(dict(payload))
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    transport = SlowTransport(
        _final_response(
            {
                "status": "completed",
                "answer": "This must time out.",
            }
        )
    )
    search = _SearchService(_empty_snapshot())
    agent = HermesResponsesDecisionAgent(
        transport=transport,
        search_service=search,  # type: ignore[arg-type]
        model=MODEL,
        provider=PROVIDER,
        timeout_seconds=0.01,
        session_ttl_seconds=1,
        session_purge_interval_seconds=0.5,
        clock=lambda: NOW,
    )

    run = await agent.ask(_request())

    assert run.draft.limitations == ["hermes_responses_timeout"]
    assert search.finished == 0
    assert search.aborted == 1
    assert transport.deleted_sessions == []


@pytest.mark.asyncio
async def test_agent_cancellation_aborts_search_session() -> None:
    started = asyncio.Event()

    class CancelledTransport(_Transport):
        async def create_response(
            self,
            payload,
            *,
            timeout_seconds: float,
        ) -> HermesResponsesHttpResult:
            self.response_calls.append(dict(payload))
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    transport = CancelledTransport(
        _final_response(
            {
                "status": "completed",
                "answer": "This must be cancelled.",
            }
        )
    )
    search = _SearchService(_empty_snapshot())
    agent = HermesResponsesDecisionAgent(
        transport=transport,
        search_service=search,  # type: ignore[arg-type]
        model=MODEL,
        provider=PROVIDER,
        timeout_seconds=5,
        clock=lambda: NOW,
    )

    task = asyncio.create_task(agent.ask(_request()))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert search.finished == 0
    assert search.aborted == 1
    assert transport.deleted_sessions == []


@pytest.mark.asyncio
async def test_agent_consumes_canonical_trace_and_validates_source_refs() -> None:
    trace, search_result, source_ref = _activity_trace()
    tool_output = json.dumps(
        {
            "structuredContent": search_result.model_dump(
                mode="json",
                round_trip=True,
            )
        }
    )
    transport = _Transport(
        _final_response(
            {
                "status": "completed",
                "answer": "A break is reasonable after one active hour.",
                "proposed_action": True,
                "used_source_ref_ids": [source_ref.reference_id],
            },
            output_prefix=[
                {
                    "type": "function_call",
                    "name": "mcp__healthmes__search_activity",
                    "arguments": json.dumps(
                            {
                                "decision_session_id": DECISION_SESSION_ID,
                                "capability": "activity.summary",
                                "start": trace.query.start.isoformat(),
                                "end": trace.query.end.isoformat(),
                                "granularity": trace.query.granularity,
                                "fields": trace.query.fields,
                                "privacy_level": (
                                    trace.query.privacy_level.value
                                ),
                                "limit": trace.query.limit,
                            }
                    ),
                    "call_id": "call-1",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": tool_output,
                },
            ],
        )
    )
    snapshot = SimpleNamespace(
        session_id=DECISION_SESSION_ID,
        source_refs=(source_ref,),
        access_trace=(search_result.access_audit,),
        tool_trace=(trace,),
    )
    search = _SearchService(snapshot)
    agent = HermesResponsesDecisionAgent(
        transport=transport,
        search_service=search,  # type: ignore[arg-type]
        model=MODEL,
        provider=PROVIDER,
        timeout_seconds=5,
        clock=lambda: NOW,
    )

    run = await agent.ask(_request())

    assert run.draft.status is DecisionStatus.COMPLETED
    assert run.draft.used_source_ref_ids == [source_ref.reference_id]
    assert run.source_refs == (source_ref,)
    assert run.tool_trace == (trace,)
    assert run.access_trace == (search_result.access_audit,)


@pytest.mark.asyncio
async def test_contract_failure_preserves_finished_server_trace() -> None:
    trace, search_result, source_ref = _activity_trace()
    fabricated = "sr_" + "0" * 32
    transport = _Transport(
        _final_response(
            {
                "status": "completed",
                "answer": "Unsupported answer.",
                "used_source_ref_ids": [fabricated],
            },
            output_prefix=[
                {
                    "type": "function_call",
                    "name": "mcp__healthmes__search_activity",
                    "arguments": json.dumps(
                            {
                                "decision_session_id": DECISION_SESSION_ID,
                                "capability": "activity.summary",
                                "start": trace.query.start.isoformat(),
                                "end": trace.query.end.isoformat(),
                                "granularity": trace.query.granularity,
                                "fields": trace.query.fields,
                                "privacy_level": (
                                    trace.query.privacy_level.value
                                ),
                                "limit": trace.query.limit,
                            }
                    ),
                    "call_id": "call-1",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": json.dumps(
                        {
                            "structuredContent": (
                                search_result.model_dump(
                                    mode="json",
                                    round_trip=True,
                                )
                            )
                        }
                    ),
                },
            ],
        )
    )
    search = _SearchService(
        SimpleNamespace(
            session_id=DECISION_SESSION_ID,
            source_refs=(source_ref,),
            access_trace=(search_result.access_audit,),
            tool_trace=(trace,),
        )
    )
    agent = HermesResponsesDecisionAgent(
        transport=transport,
        search_service=search,  # type: ignore[arg-type]
        model=MODEL,
        provider=PROVIDER,
        timeout_seconds=5,
        clock=lambda: NOW,
    )

    run = await agent.ask(_request())

    assert run.draft.status is DecisionStatus.FAILED
    assert run.draft.limitations == ["hermes_source_ref_fabricated"]
    assert run.source_refs == (source_ref,)
    assert run.tool_trace == (trace,)
    assert search.finished == 1
    assert search.aborted == 0


@pytest.mark.asyncio
async def test_startup_rejects_enabled_native_toolsets_before_response() -> None:
    transport = _Transport(
        _final_response(
            {
                "status": "completed",
                "answer": "This must not execute.",
            }
        ),
        native_enabled=True,
    )
    search = _SearchService(_empty_snapshot())
    agent = HermesResponsesDecisionAgent(
        transport=transport,
        search_service=search,  # type: ignore[arg-type]
        model=MODEL,
        provider=PROVIDER,
        timeout_seconds=5,
        clock=lambda: NOW,
    )

    run = await agent.ask(_request())

    assert run.draft.status is DecisionStatus.BLOCKED
    assert run.draft.limitations == ["hermes_tool_profile_unsafe"]
    assert transport.toolset_calls == 1
    assert transport.response_calls == []
    assert search.begun == 0


@pytest.mark.asyncio
async def test_startup_rejects_missing_model_route_before_response() -> None:
    transport = _Transport(
        _final_response(
            {
                "status": "completed",
                "answer": "This must not execute.",
            }
        ),
        include_model_route=False,
    )
    agent = HermesResponsesDecisionAgent(
        transport=transport,
        search_service=_SearchService(_empty_snapshot()),  # type: ignore[arg-type]
        model=MODEL,
        provider=PROVIDER,
        timeout_seconds=5,
        clock=lambda: NOW,
    )

    run = await agent.ask(_request())

    assert run.draft.status is DecisionStatus.BLOCKED
    assert run.draft.limitations == ["hermes_model_route_unavailable"]
    assert transport.toolset_calls == 1
    assert transport.model_calls == 1
    assert transport.response_calls == []


@pytest.mark.asyncio
async def test_agent_rejects_tool_arguments_that_disagree_with_trace() -> None:
    trace, search_result, source_ref = _activity_trace()
    transport = _Transport(
        _final_response(
            {
                "status": "completed",
                "answer": "This transcript is not canonical.",
                "used_source_ref_ids": [source_ref.reference_id],
            },
            output_prefix=[
                {
                    "type": "function_call",
                    "name": "mcp__healthmes__search_activity",
                    "arguments": json.dumps(
                        {
                            "decision_session_id": DECISION_SESSION_ID,
                            "capability": "activity.summary",
                            "limit": 99,
                        }
                    ),
                    "call_id": "call-1",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": json.dumps(
                        {
                            "structuredContent": (
                                search_result.model_dump(
                                    mode="json",
                                    round_trip=True,
                                )
                            )
                        }
                    ),
                },
            ],
        )
    )
    search = _SearchService(
        SimpleNamespace(
            session_id=DECISION_SESSION_ID,
            source_refs=(source_ref,),
            access_trace=(search_result.access_audit,),
            tool_trace=(trace,),
        )
    )
    agent = HermesResponsesDecisionAgent(
        transport=transport,
        search_service=search,  # type: ignore[arg-type]
        model=MODEL,
        provider=PROVIDER,
        timeout_seconds=5,
        clock=lambda: NOW,
    )

    run = await agent.ask(_request())

    assert run.draft.status is DecisionStatus.FAILED
    assert run.draft.limitations == [
        "hermes_tool_arguments_trace_mismatch"
    ]


@pytest.mark.asyncio
async def test_http_transport_uses_only_documented_responses_endpoints() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        assert request.headers["authorization"] == "Bearer secret"
        assert request.headers["accept-encoding"] == "identity"
        if request.url.path == HERMES_TOOLSETS_PATH:
            return httpx.Response(200, json=_toolsets())
        if request.url.path == HERMES_MODELS_PATH:
            return httpx.Response(200, json=_models())
        if request.url.path == HERMES_RESPONSES_PATH:
            payload = json.loads(request.content)
            assert payload["store"] is False
            assert payload["stream"] is True
            assert request.headers["accept"] == "text/event-stream"
            response = _final_response(
                {
                    "status": "completed",
                    "answer": "Take a short break.",
                }
            )
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "text/event-stream",
                    "X-Hermes-Session-Id": HERMES_SESSION_ID,
                },
                content=_sse_body(response),
            )
        if request.url.path == "/api/sessions":
            assert request.url.params["source"] == "api_server"
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [],
                    "limit": 200,
                    "offset": 0,
                    "has_more": False,
                },
            )
        if request.url.path == HERMES_SESSION_PATH.format(
            session_id=HERMES_SESSION_ID
        ):
            return httpx.Response(200, json={"deleted": True})
        raise AssertionError(f"unexpected endpoint: {request.url.path}")

    transport = HermesHttpResponsesTransport(
        base_url="http://127.0.0.1:8644",
        api_key="secret",
        http_transport=httpx.MockTransport(handler),
    )
    assert await transport.get_toolsets() == _toolsets()
    assert await transport.get_models() == _models()
    result = await transport.create_response(
        {
            "model": MODEL,
            "input": "question",
            "store": False,
            "stream": True,
        },
        timeout_seconds=5,
    )
    sessions = await transport.list_sessions(limit=200, offset=0)
    await transport.delete_session(result.session_id)

    assert result.session_id == HERMES_SESSION_ID
    assert sessions["data"] == []
    assert seen == [
        ("GET", HERMES_TOOLSETS_PATH),
        ("GET", HERMES_MODELS_PATH),
        ("POST", HERMES_RESPONSES_PATH),
        ("GET", "/api/sessions"),
        (
            "DELETE",
            HERMES_SESSION_PATH.format(session_id=HERMES_SESSION_ID),
        ),
    ]


@pytest.mark.asyncio
async def test_http_stream_close_propagates_timeout_cancellation() -> None:
    stream_started = asyncio.Event()
    stream_closed = asyncio.Event()

    class BlockingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            created = {
                "type": "response.created",
                "response": {
                    "id": "resp-timeout",
                    "object": "response",
                    "status": "in_progress",
                    "created_at": int(NOW.timestamp()),
                    "model": MODEL,
                    "output": [],
                },
                "sequence_number": 0,
            }
            yield (
                "event: response.created\n"
                f"data: {json.dumps(created)}\n\n"
            ).encode()
            stream_started.set()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            stream_closed.set()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == HERMES_RESPONSES_PATH
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/event-stream",
                "X-Hermes-Session-Id": HERMES_SESSION_ID,
            },
            stream=BlockingStream(),
        )

    transport = HermesHttpResponsesTransport(
        base_url="http://127.0.0.1:8644",
        api_key="secret",
        http_transport=httpx.MockTransport(handler),
    )
    task = asyncio.create_task(
        transport.create_response(
            {
                "model": MODEL,
                "input": "question",
                "store": False,
                "stream": True,
            },
            timeout_seconds=5,
        )
    )
    await asyncio.wait_for(stream_started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.wait_for(stream_closed.wait(), timeout=1)


@pytest.mark.asyncio
async def test_http_transport_rejects_encoded_responses() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            content=gzip.compress(b"{}"),
        )

    transport = HermesHttpResponsesTransport(
        base_url="http://127.0.0.1:8644",
        api_key="secret",
        http_transport=httpx.MockTransport(handler),
    )

    with pytest.raises(
        HermesResponsesTransportError,
        match="hermes_response_encoding_rejected",
    ):
        await transport.get_toolsets()


def test_production_rest_path_probes_then_uses_one_responses_turn(
    settings,
) -> None:
    transport = _Transport(
        _final_response(
            {
                "status": "completed",
                "answer": "Take a short break.",
            }
        )
    )
    configured = settings.model_copy(
        update={
            "decision_hermes_base_url": "http://127.0.0.1:8644",
            "decision_hermes_model": MODEL,
            "decision_hermes_provider": PROVIDER,
            "decision_timeout_seconds": 5,
        }
    )
    app = create_app(
        configured,
        decision_transport=transport,
        decision_clock=lambda: NOW,
    )

    with TestClient(
        app,
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    ) as client:
        assert transport.toolset_calls == 1
        decision_engine = app.state.decision_engine
        assert decision_engine is not None
        assert (
            decision_engine._agent._search_service
            is mcp_server.get_decision_search_session_service()
        )

        response = client.post(
            "/v1/wellness-decisions",
            json={"question": "Should I take a break now?"},
        )

    assert response.status_code == 200
    assert response.json()["answer"] == "Take a short break."
    assert len(transport.response_calls) == 1
    assert transport.deleted_sessions == [HERMES_SESSION_ID]
