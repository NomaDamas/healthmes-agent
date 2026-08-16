"""Hermes Responses runtime for the single HealthMes decision path."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from concurrent.futures import Future as ConcurrentFuture
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import BoundedSemaphore, Event, Lock, Thread
from time import monotonic
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from healthmes.config import is_loopback_host
from healthmes.decision.agent import DecisionAgentRun
from healthmes.decision.contracts import (
    ContextResult,
    DecisionDraft,
    DecisionRequest,
    DecisionStatus,
    RuntimeMetadata,
    ToolCallRecord,
)
from healthmes.decision.hermes_profile import (
    HERMES_DECISION_RUNTIME_MODEL_NAME,
    HERMES_DECISION_SEARCH_TOOL_ALLOWLIST,
    HERMES_DECISION_SKILL_TOOL_ALLOWLIST,
    HERMES_DECISION_TOOL_DOMAINS,
    HermesDecisionProfileAssertion,
    HermesDecisionProfileError,
)
from healthmes.decision.search import (
    ContextSearchResult,
    DecisionContextSearchSessionService,
    DecisionSearchSessionSnapshot,
)
from healthmes.decision.validation import (
    normalize_untrusted_json,
    strict_model_validate,
)
from healthmes.hermes_mcp_inventory import expected_hermes_mcp_inventory
from healthmes.hermes_runtime_identity import (
    HERMES_RUNTIME_ATTESTATION_PATH,
    HermesDecisionRuntimeManifest,
    HermesRuntimeIdentityError,
    new_attestation_nonce,
    validate_expected_runtime,
    verify_runtime_attestation,
)

HERMES_RESPONSES_PATH = "/v1/responses"
HERMES_MODELS_PATH = "/v1/models"
HERMES_TOOLSETS_PATH = "/v1/toolsets"
HERMES_SESSION_PATH = "/api/sessions/{session_id}"
HERMES_DECISION_DRAFT_SCHEMA = "healthmes.decision-draft.v1"
HERMES_RESPONSES_POLICY_VERSION = "healthmes-responses-policy.v2"
HERMES_WELLNESS_SKILL_CATALOG_SCHEMA = "healthmes-wellness-skills.v1"

_LOGGER = logging.getLogger(__name__)
_MAX_TOOLSETS_RESPONSE_BYTES = 256_000
_MAX_MODELS_RESPONSE_BYTES = 256_000
_MAX_ATTESTATION_RESPONSE_BYTES = 128_000
_MAX_SESSIONS_RESPONSE_BYTES = 1_000_000
_MAX_RESPONSES_RESPONSE_BYTES = 2_000_000
_MAX_TOOL_OUTPUT_BYTES = 1_000_000
_MAX_SSE_EVENT_BYTES = 1_250_000
_MAX_SSE_EVENTS = 2_048
_MAX_HERMES_SESSION_ID_LENGTH = 256
_SESSION_ID_CONTROL = re.compile(r"[\r\n\x00]")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class _DuplicateJsonMember(ValueError):
    """Raised before duplicate JSON object members can collapse."""


def _reject_duplicate_json_members(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonMember(key)
        value[key] = item
    return value


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )


class HermesResponsesError(RuntimeError):
    """Sanitized failure with a stable public reason code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class HermesResponsesTransportError(HermesResponsesError):
    """HTTP or transport failure outside the model transcript."""


class HermesResponsesContractError(HermesResponsesError):
    """Untrusted Hermes response violated the HealthMes contract."""


class HermesToolsetEntry(BaseModel):
    """One native toolset entry from Hermes ``GET /v1/toolsets``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=128)
    label: str = Field(max_length=512)
    description: str = Field(max_length=4_096)
    enabled: bool
    configured: bool
    tools: tuple[str, ...] = Field(default=(), max_length=1_024)


class HermesToolsetsResponse(BaseModel):
    """Authenticated native-tool profile exposed by the API server."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    object: Literal["list"]
    platform: Literal["api_server"]
    data: tuple[HermesToolsetEntry, ...] = ()

    @model_validator(mode="after")
    def require_native_tools_disabled(self) -> HermesToolsetsResponse:
        if any(item.enabled for item in self.data):
            raise ValueError(
                "native Hermes toolsets must be disabled for decisions"
            )
        return self


class HermesModelEntry(BaseModel):
    """One model advertised by the dedicated Hermes API server."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=255)
    object: Literal["model"]
    created: int = Field(ge=0)
    owned_by: Literal["hermes"]
    permission: tuple[dict[str, Any], ...] = ()
    root: str = Field(min_length=1, max_length=255)
    parent: str | None = Field(default=None, max_length=255)


class HermesModelsResponse(BaseModel):
    """Authenticated model-route discovery payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    object: Literal["list"]
    data: tuple[HermesModelEntry, ...] = Field(max_length=256)


class HermesSessionSummary(BaseModel):
    """Bounded session metadata used only by dedicated-state TTL cleanup."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str = Field(min_length=1, max_length=256)
    source: Literal["api_server"]
    started_at: float | int | str | None = None
    last_active: float | int | str | None = None


class HermesSessionsResponse(BaseModel):
    """One bounded page from Hermes ``GET /api/sessions``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    object: Literal["list"]
    data: tuple[HermesSessionSummary, ...] = Field(max_length=200)
    limit: int = Field(ge=0, le=200)
    offset: int = Field(ge=0, le=1_000_000)
    has_more: bool


class HermesResponsesUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_total(self) -> HermesResponsesUsage:
        if self.total_tokens < self.input_tokens + self.output_tokens:
            raise ValueError("Hermes token total is inconsistent")
        return self


class HermesFunctionCallItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["function_call"]
    name: str = Field(min_length=1, max_length=255)
    arguments: str = Field(max_length=256_000)
    call_id: str = Field(min_length=1, max_length=255)


class HermesFunctionOutputItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["function_call_output"]
    call_id: str = Field(min_length=1, max_length=255)
    output: str = Field(max_length=_MAX_TOOL_OUTPUT_BYTES)


class HermesOutputText(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["output_text"]
    text: str = Field(min_length=1, max_length=64_000)


class HermesAssistantMessageItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["message"]
    role: Literal["assistant"]
    content: tuple[HermesOutputText, ...] = Field(min_length=1, max_length=1)


class HermesResponsesResponse(BaseModel):
    """Strict completed response reconstructed from Hermes SSE events."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=255)
    object: Literal["response"]
    status: Literal["completed"]
    created_at: int = Field(ge=0)
    model: str = Field(min_length=1, max_length=255)
    output: tuple[dict[str, Any], ...] = Field(max_length=256)
    usage: HermesResponsesUsage


class HermesDecisionDraftEnvelope(BaseModel):
    """The only final assistant text accepted from Hermes."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    schema_name: Literal["healthmes.decision-draft.v1"] = Field(
        alias="schema"
    )
    decision: DecisionDraft


@dataclass(frozen=True, slots=True)
class HermesResponsesHttpResult:
    """Bounded reconstructed transcript plus its Hermes session id."""

    payload: dict[str, Any]
    session_id: str


@dataclass(frozen=True, slots=True)
class HermesRuntimeAttestationAssertion:
    """Expected local artifacts for one pre-execution runtime proof."""

    manifest_path: Path
    attestation_key_path: Path
    profile_assertion: HermesDecisionProfileAssertion
    expected_origin: str
    expected_model: str
    expected_provider: str
    expected_api_key: str = field(repr=False)
    max_age_seconds: int = 30

    def expected_bundle(
        self,
    ) -> tuple[HermesDecisionRuntimeManifest, bytes]:
        if self.max_age_seconds <= 0 or self.max_age_seconds > 300:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_attestation_window_invalid"
            )
        profile_digest = self.profile_assertion.verify()
        return validate_expected_runtime(
            manifest_path=self.manifest_path,
            attestation_key_path=self.attestation_key_path,
            profile_path=self.profile_assertion.path,
            profile_semantic_digest=profile_digest,
            expected_origin=self.expected_origin,
            expected_model=self.expected_model,
            expected_provider=self.expected_provider,
            expected_api_key=self.expected_api_key,
        )


class HermesResponsesTransport(Protocol):
    """Documented Hermes HTTP boundary; no vendor Python imports."""

    async def verify_runtime(self, *, timeout_seconds: float) -> None:
        """Verify the signed runtime and live six-tool MCP inventory."""

    async def get_toolsets(self) -> Mapping[str, Any]:
        """Return the authenticated ``GET /v1/toolsets`` payload."""

    async def get_models(self) -> Mapping[str, Any]:
        """Return the authenticated ``GET /v1/models`` payload."""

    async def create_response(
        self,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> HermesResponsesHttpResult:
        """Reverify runtime, then run one complete autonomous agent turn."""

    async def list_sessions(
        self,
        *,
        limit: int,
        offset: int,
    ) -> Mapping[str, Any]:
        """List dedicated API-server sessions for TTL maintenance."""

    async def delete_session(self, session_id: str) -> None:
        """Delete one request-scoped Hermes transcript."""


class HermesHttpResponsesTransport:
    """Bounded HTTP implementation of the Hermes Responses contract."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        discovery_timeout_seconds: float = 5,
        max_response_timeout_seconds: float = 120,
        runtime_attestation: (
            HermesRuntimeAttestationAssertion | None
        ) = None,
        allow_attested_private_http: bool = False,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if discovery_timeout_seconds <= 0:
            raise ValueError("discovery timeout must be positive")
        if max_response_timeout_seconds <= 0:
            raise ValueError("response timeout must be positive")
        self._base_url = _validated_hermes_origin(
            base_url,
            api_key=api_key,
            allow_attested_private_http=(
                allow_attested_private_http
                and runtime_attestation is not None
            ),
        )
        hostname = urlsplit(self._base_url).hostname
        if (
            hostname is None
            or (
                not is_loopback_host(hostname)
                and runtime_attestation is None
            )
        ):
            raise ValueError(
                "remote Hermes runtime requires content-bound attestation"
            )
        self._api_key = api_key.strip() if api_key else None
        self._discovery_timeout_seconds = discovery_timeout_seconds
        self._max_response_timeout_seconds = (
            max_response_timeout_seconds
        )
        self._runtime_attestation = runtime_attestation
        self._http_transport = http_transport

    async def get_toolsets(self) -> Mapping[str, Any]:
        payload, _headers = await self._request_json(
            "GET",
            HERMES_TOOLSETS_PATH,
            timeout_seconds=self._discovery_timeout_seconds,
            max_response_bytes=_MAX_TOOLSETS_RESPONSE_BYTES,
        )
        return payload

    async def get_models(self) -> Mapping[str, Any]:
        payload, _headers = await self._request_json(
            "GET",
            HERMES_MODELS_PATH,
            timeout_seconds=self._discovery_timeout_seconds,
            max_response_bytes=_MAX_MODELS_RESPONSE_BYTES,
        )
        return payload

    async def verify_runtime(self, *, timeout_seconds: float) -> None:
        """Verify the supervisor proof within the caller's remaining budget."""

        bounded_timeout = min(
            timeout_seconds,
            self._discovery_timeout_seconds,
        )
        if bounded_timeout <= 0:
            raise HermesResponsesTransportError(
                "hermes_responses_deadline_expired"
            )
        await self._attest_runtime(timeout_seconds=bounded_timeout)

    async def create_response(
        self,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> HermesResponsesHttpResult:
        bounded_timeout = min(
            timeout_seconds,
            self._max_response_timeout_seconds,
        )
        if bounded_timeout <= 0:
            raise HermesResponsesTransportError(
                "hermes_responses_deadline_expired"
            )
        body = dict(payload)
        if body.get("stream") is not True or body.get("store") is not False:
            raise HermesResponsesTransportError(
                "hermes_streaming_contract_required"
            )
        deadline = monotonic() + bounded_timeout
        await self.verify_runtime(timeout_seconds=bounded_timeout)
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise HermesResponsesTransportError(
                "hermes_responses_deadline_expired"
            )
        return await self._request_sse_response(
            body,
            timeout_seconds=remaining,
        )

    async def _attest_runtime(self, *, timeout_seconds: float) -> None:
        assertion = self._runtime_attestation
        if assertion is None:
            return
        try:
            manifest, key = assertion.expected_bundle()
            nonce = new_attestation_nonce()
            payload, _headers = await self._request_json(
                "POST",
                HERMES_RUNTIME_ATTESTATION_PATH,
                json_body={"nonce": nonce},
                timeout_seconds=timeout_seconds,
                max_response_bytes=_MAX_ATTESTATION_RESPONSE_BYTES,
            )
            verify_runtime_attestation(
                payload,
                expected_manifest=manifest,
                key=key,
                nonce=nonce,
                expected_mcp_inventory=expected_hermes_mcp_inventory(),
                max_age_seconds=assertion.max_age_seconds,
            )
        except (
            HermesDecisionProfileError,
            HermesRuntimeIdentityError,
        ) as exc:
            raise HermesResponsesContractError(str(exc)) from exc
        except HermesResponsesError as exc:
            raise HermesResponsesTransportError(
                "hermes_runtime_attestation_unavailable"
            ) from exc
        except Exception as exc:
            raise HermesResponsesTransportError(
                "hermes_runtime_attestation_unavailable"
            ) from exc

    async def list_sessions(
        self,
        *,
        limit: int,
        offset: int,
    ) -> Mapping[str, Any]:
        if not 1 <= limit <= 200 or not 0 <= offset <= 1_000_000:
            raise ValueError("invalid Hermes session page")
        payload, _headers = await self._request_json(
            "GET",
            "/api/sessions",
            query={
                "source": "api_server",
                "limit": str(limit),
                "offset": str(offset),
                "include_children": "false",
            },
            timeout_seconds=self._discovery_timeout_seconds,
            max_response_bytes=_MAX_SESSIONS_RESPONSE_BYTES,
        )
        return payload

    async def delete_session(self, session_id: str) -> None:
        safe_id = quote(_validated_session_id(session_id), safe="")
        await self._request_json(
            "DELETE",
            HERMES_SESSION_PATH.format(session_id=safe_id),
            timeout_seconds=self._discovery_timeout_seconds,
            max_response_bytes=_MAX_TOOLSETS_RESPONSE_BYTES,
            allow_not_found=True,
        )

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        query: Mapping[str, str] | None = None,
        timeout_seconds: float,
        max_response_bytes: int,
        allow_not_found: bool = False,
    ) -> tuple[dict[str, Any], httpx.Headers]:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
        }
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                headers=headers,
                follow_redirects=False,
                transport=self._http_transport,
            ) as client:
                async with client.stream(
                    method,
                    path,
                    json=json_body,
                    params=query,
                    timeout=timeout_seconds,
                ) as response:
                    if response.is_redirect:
                        raise HermesResponsesTransportError(
                            "hermes_redirect_rejected"
                        )
                    content_encoding = response.headers.get(
                        "content-encoding",
                        "",
                    ).strip().lower()
                    if content_encoding not in {"", "identity"}:
                        raise HermesResponsesTransportError(
                            "hermes_response_encoding_rejected"
                        )
                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        try:
                            declared = int(content_length)
                        except ValueError as exc:
                            raise HermesResponsesTransportError(
                                "hermes_response_invalid"
                            ) from exc
                        if declared > max_response_bytes:
                            raise HermesResponsesTransportError(
                                "hermes_response_too_large"
                            )
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > max_response_bytes:
                            raise HermesResponsesTransportError(
                                "hermes_response_too_large"
                            )
                        body.extend(chunk)
                    if response.status_code == 404 and allow_not_found:
                        return {}, response.headers
                    if response.status_code < 200 or response.status_code >= 300:
                        raise HermesResponsesTransportError(
                            _http_error_code(response.status_code)
                        )
                    try:
                        decoded = json.loads(body)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise HermesResponsesTransportError(
                            "hermes_response_invalid"
                        ) from exc
        except asyncio.CancelledError:
            raise
        except HermesResponsesTransportError:
            raise
        except httpx.TimeoutException as exc:
            raise HermesResponsesTransportError(
                "hermes_responses_timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise HermesResponsesTransportError(
                "hermes_responses_unavailable"
            ) from exc
        if type(decoded) is not dict:
            raise HermesResponsesTransportError(
                "hermes_response_invalid"
            )
        normalized = normalize_untrusted_json(
            decoded,
            max_bytes=max_response_bytes,
        )
        if type(normalized.value) is not dict:
            raise HermesResponsesTransportError(
                "hermes_response_invalid"
            )
        return normalized.value, response.headers

    async def _request_sse_response(
        self,
        json_body: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> HermesResponsesHttpResult:
        headers = {
            "Accept": "text/event-stream",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
        }
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                headers=headers,
                follow_redirects=False,
                transport=self._http_transport,
            ) as client:
                async with client.stream(
                    "POST",
                    HERMES_RESPONSES_PATH,
                    json=dict(json_body),
                    timeout=timeout_seconds,
                ) as response:
                    if response.is_redirect:
                        raise HermesResponsesTransportError(
                            "hermes_redirect_rejected"
                        )
                    if (
                        response.status_code < 200
                        or response.status_code >= 300
                    ):
                        raise HermesResponsesTransportError(
                            _http_error_code(response.status_code)
                        )
                    content_encoding = response.headers.get(
                        "content-encoding",
                        "",
                    ).strip().lower()
                    if content_encoding not in {"", "identity"}:
                        raise HermesResponsesTransportError(
                            "hermes_response_encoding_rejected"
                        )
                    content_type = response.headers.get(
                        "content-type",
                        "",
                    ).split(";", 1)[0].strip().lower()
                    if content_type != "text/event-stream":
                        raise HermesResponsesTransportError(
                            "hermes_sse_content_type_invalid"
                        )
                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        try:
                            declared = int(content_length)
                        except ValueError as exc:
                            raise HermesResponsesTransportError(
                                "hermes_response_invalid"
                            ) from exc
                        if declared > _MAX_RESPONSES_RESPONSE_BYTES:
                            raise HermesResponsesTransportError(
                                "hermes_response_too_large"
                            )
                    session_id = _validated_session_id(
                        response.headers.get("x-hermes-session-id")
                    )
                    accumulator = _HermesResponsesSSEAccumulator()
                    async for event_name, event_data in _iter_sse_events(
                        response
                    ):
                        accumulator.consume(event_name, event_data)
                    return HermesResponsesHttpResult(
                        payload=accumulator.finish(),
                        session_id=session_id,
                    )
        except asyncio.CancelledError:
            # Exiting both stream contexts closes the socket. Vendored Hermes
            # treats that disconnect as an agent.interrupt() signal.
            raise
        except HermesResponsesError:
            raise
        except httpx.TimeoutException as exc:
            raise HermesResponsesTransportError(
                "hermes_responses_timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise HermesResponsesTransportError(
                "hermes_responses_unavailable"
            ) from exc


class _HermesResponsesSSEAccumulator:
    """Rebuild the untruncated current-turn transcript from SSE done events."""

    def __init__(self) -> None:
        self._next_sequence = 0
        self._created: dict[str, Any] | None = None
        self._terminal: HermesResponsesResponse | None = None
        self._done_items: list[dict[str, Any]] = []
        self._added_items: dict[int, tuple[str, str]] = {}
        self._done_indexes: set[int] = set()
        self._text_done: tuple[str, int, str] | None = None

    def consume(self, event_name: str, raw_data: str) -> None:
        if self._terminal is not None:
            raise HermesResponsesContractError(
                "hermes_sse_event_after_terminal"
            )
        event = _parse_sse_json(raw_data)
        if event.get("type") != event_name:
            raise HermesResponsesContractError(
                "hermes_sse_event_type_mismatch"
            )
        sequence = event.get("sequence_number")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence != self._next_sequence
        ):
            raise HermesResponsesContractError(
                "hermes_sse_sequence_invalid"
            )
        self._next_sequence += 1
        if self._next_sequence > _MAX_SSE_EVENTS:
            raise HermesResponsesTransportError(
                "hermes_response_too_large"
            )

        if event_name == "response.created":
            self._consume_created(event)
            return
        if self._created is None:
            raise HermesResponsesContractError(
                "hermes_sse_created_missing"
            )
        if event_name == "response.output_item.added":
            self._consume_item_added(event)
        elif event_name == "response.output_item.done":
            self._consume_item_done(event)
        elif event_name == "response.output_text.delta":
            self._consume_text_delta(event)
        elif event_name == "response.output_text.done":
            self._consume_text_done(event)
        elif event_name == "response.completed":
            self._consume_completed(event)
        elif event_name == "response.failed":
            self._consume_failed(event)
        else:
            raise HermesResponsesContractError(
                "hermes_sse_event_not_allowed"
            )

    def finish(self) -> dict[str, Any]:
        terminal = self._terminal
        if self._created is None or terminal is None:
            raise HermesResponsesContractError(
                "hermes_sse_terminal_missing"
            )
        if not self._done_items:
            raise HermesResponsesContractError(
                "hermes_sse_transcript_missing"
            )
        if self._done_items[-1].get("type") != "message":
            raise HermesResponsesContractError(
                "hermes_final_message_invalid"
            )
        if self._text_done is None:
            raise HermesResponsesContractError(
                "hermes_sse_text_done_missing"
            )
        payload = terminal.model_dump(mode="python", round_trip=True)
        payload["output"] = [
            dict(item) for item in self._done_items
        ]
        return payload

    def _consume_created(self, event: Mapping[str, Any]) -> None:
        if self._created is not None or set(event) != {
            "type",
            "response",
            "sequence_number",
        }:
            raise HermesResponsesContractError(
                "hermes_sse_created_invalid"
            )
        response = _strict_mapping(
            event.get("response"),
            code="hermes_sse_created_invalid",
        )
        if set(response) != {
            "id",
            "object",
            "status",
            "created_at",
            "model",
            "output",
        }:
            raise HermesResponsesContractError(
                "hermes_sse_created_invalid"
            )
        response_id = response.get("id")
        model = response.get("model")
        created_at = response.get("created_at")
        if (
            not isinstance(response_id, str)
            or not 1 <= len(response_id) <= 255
            or response.get("object") != "response"
            or response.get("status") != "in_progress"
            or isinstance(created_at, bool)
            or not isinstance(created_at, int)
            or created_at < 0
            or not isinstance(model, str)
            or not 1 <= len(model) <= 255
            or response.get("output") != []
        ):
            raise HermesResponsesContractError(
                "hermes_sse_created_invalid"
            )
        self._created = dict(response)

    def _consume_item_added(self, event: Mapping[str, Any]) -> None:
        if set(event) != {
            "type",
            "output_index",
            "item",
            "sequence_number",
        }:
            raise HermesResponsesContractError(
                "hermes_sse_item_invalid"
            )
        index = _sse_output_index(event)
        item = _strict_mapping(
            event.get("item"),
            code="hermes_sse_item_invalid",
        )
        item_id = item.get("id")
        item_type = item.get("type")
        if (
            index in self._added_items
            or not isinstance(item_id, str)
            or not 1 <= len(item_id) <= 255
            or item_type
            not in {"function_call", "function_call_output", "message"}
        ):
            raise HermesResponsesContractError(
                "hermes_sse_item_invalid"
            )
        self._added_items[index] = (item_id, item_type)

    def _consume_item_done(self, event: Mapping[str, Any]) -> None:
        if set(event) != {
            "type",
            "output_index",
            "item",
            "sequence_number",
        }:
            raise HermesResponsesContractError(
                "hermes_sse_item_invalid"
            )
        index = _sse_output_index(event)
        item = _strict_mapping(
            event.get("item"),
            code="hermes_sse_item_invalid",
        )
        item_id = item.get("id")
        item_type = item.get("type")
        if (
            index in self._done_indexes
            or self._added_items.get(index) != (item_id, item_type)
        ):
            raise HermesResponsesContractError(
                "hermes_sse_item_invalid"
            )
        if item_type == "function_call":
            normalized = _normalize_sse_function_call(item)
        elif item_type == "function_call_output":
            normalized = _normalize_sse_function_output(item)
        elif item_type == "message":
            normalized = _normalize_sse_message(item)
            if self._text_done is None:
                raise HermesResponsesContractError(
                    "hermes_sse_text_done_missing"
                )
            text_item_id, text_index, text = self._text_done
            if (
                text_item_id != item_id
                or text_index != index
                or normalized["content"][0]["text"] != text
            ):
                raise HermesResponsesContractError(
                    "hermes_sse_text_mismatch"
                )
        else:
            raise HermesResponsesContractError(
                "hermes_sse_item_invalid"
            )
        if any(
            existing.get("type") == "message"
            for existing in self._done_items
        ):
            raise HermesResponsesContractError(
                "hermes_transcript_order_invalid"
            )
        self._done_indexes.add(index)
        self._done_items.append(normalized)

    def _consume_text_delta(self, event: Mapping[str, Any]) -> None:
        if set(event) != {
            "type",
            "item_id",
            "output_index",
            "content_index",
            "delta",
            "logprobs",
            "sequence_number",
        }:
            raise HermesResponsesContractError(
                "hermes_sse_text_invalid"
            )
        _validate_sse_text_coordinates(event)
        delta = event.get("delta")
        if not isinstance(delta, str):
            raise HermesResponsesContractError(
                "hermes_sse_text_invalid"
            )

    def _consume_text_done(self, event: Mapping[str, Any]) -> None:
        if set(event) != {
            "type",
            "item_id",
            "output_index",
            "content_index",
            "text",
            "logprobs",
            "sequence_number",
        }:
            raise HermesResponsesContractError(
                "hermes_sse_text_invalid"
            )
        item_id, index = _validate_sse_text_coordinates(event)
        text = event.get("text")
        if (
            self._text_done is not None
            or not isinstance(text, str)
            or not 1 <= len(text) <= 64_000
        ):
            raise HermesResponsesContractError(
                "hermes_sse_text_invalid"
            )
        self._text_done = (item_id, index, text)

    def _consume_completed(self, event: Mapping[str, Any]) -> None:
        if set(event) != {
            "type",
            "response",
            "sequence_number",
        }:
            raise HermesResponsesContractError(
                "hermes_sse_terminal_invalid"
            )
        try:
            terminal = strict_model_validate(
                HermesResponsesResponse,
                event.get("response"),
            )
        except Exception as exc:
            raise HermesResponsesContractError(
                "hermes_sse_terminal_invalid"
            ) from exc
        created = self._created
        if created is None or (
            terminal.id != created["id"]
            or terminal.created_at != created["created_at"]
            or terminal.model != created["model"]
        ):
            raise HermesResponsesContractError(
                "hermes_sse_terminal_mismatch"
            )
        if set(self._added_items) != self._done_indexes:
            raise HermesResponsesContractError(
                "hermes_sse_item_incomplete"
            )
        self._terminal = terminal

    def _consume_failed(self, event: Mapping[str, Any]) -> None:
        if set(event) != {
            "type",
            "response",
            "sequence_number",
        }:
            raise HermesResponsesContractError(
                "hermes_sse_terminal_invalid"
            )
        response = _strict_mapping(
            event.get("response"),
            code="hermes_sse_terminal_invalid",
        )
        created = self._created
        if (
            created is None
            or response.get("id") != created["id"]
            or response.get("object") != "response"
            or response.get("status") != "failed"
            or response.get("created_at") != created["created_at"]
            or response.get("model") != created["model"]
        ):
            raise HermesResponsesContractError(
                "hermes_sse_terminal_mismatch"
            )
        raise HermesResponsesContractError("hermes_response_failed")


async def _iter_sse_events(
    response: httpx.Response,
) -> AsyncIterator[tuple[str, str]]:
    total_bytes = 0
    pending = bytearray()
    event_name: str | None = None
    data_lines: list[bytes] = []
    event_bytes = 0

    async for chunk in response.aiter_bytes():
        total_bytes += len(chunk)
        if total_bytes > _MAX_RESPONSES_RESPONSE_BYTES:
            raise HermesResponsesTransportError(
                "hermes_response_too_large"
            )
        pending.extend(chunk)
        while True:
            newline = pending.find(b"\n")
            if newline < 0:
                if len(pending) + event_bytes > _MAX_SSE_EVENT_BYTES:
                    raise HermesResponsesTransportError(
                        "hermes_response_too_large"
                    )
                break
            raw_line = bytes(pending[:newline])
            del pending[: newline + 1]
            if raw_line.endswith(b"\r"):
                raw_line = raw_line[:-1]
            event_bytes += len(raw_line) + 1
            if event_bytes > _MAX_SSE_EVENT_BYTES:
                raise HermesResponsesTransportError(
                    "hermes_response_too_large"
                )
            if not raw_line:
                if event_name is None and not data_lines:
                    event_bytes = 0
                    continue
                if event_name is None or not data_lines:
                    raise HermesResponsesContractError(
                        "hermes_sse_frame_invalid"
                    )
                try:
                    data = b"\n".join(data_lines).decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise HermesResponsesContractError(
                        "hermes_sse_frame_invalid"
                    ) from exc
                yield event_name, data
                event_name = None
                data_lines = []
                event_bytes = 0
                continue
            if raw_line.startswith(b":"):
                continue
            field, separator, value = raw_line.partition(b":")
            if not separator:
                raise HermesResponsesContractError(
                    "hermes_sse_frame_invalid"
                )
            if value.startswith(b" "):
                value = value[1:]
            if field == b"event":
                if event_name is not None:
                    raise HermesResponsesContractError(
                        "hermes_sse_frame_invalid"
                    )
                try:
                    event_name = value.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise HermesResponsesContractError(
                        "hermes_sse_frame_invalid"
                    ) from exc
                if not event_name:
                    raise HermesResponsesContractError(
                        "hermes_sse_frame_invalid"
                    )
            elif field == b"data":
                data_lines.append(value)
            else:
                raise HermesResponsesContractError(
                    "hermes_sse_frame_invalid"
                )
    if pending or event_name is not None or data_lines or event_bytes:
        raise HermesResponsesContractError(
            "hermes_sse_frame_incomplete"
        )


def _parse_sse_json(raw: str) -> dict[str, Any]:
    try:
        return _parse_json_object(
            raw,
            code="hermes_sse_event_invalid",
            max_bytes=_MAX_SSE_EVENT_BYTES,
        )
    except HermesResponsesContractError:
        raise
    except Exception as exc:
        raise HermesResponsesContractError(
            "hermes_sse_event_invalid"
        ) from exc


def _strict_mapping(
    value: Any,
    *,
    code: str,
) -> Mapping[str, Any]:
    if type(value) is not dict or any(
        not isinstance(key, str) for key in value
    ):
        raise HermesResponsesContractError(code)
    return value


def _sse_output_index(event: Mapping[str, Any]) -> int:
    index = event.get("output_index")
    if (
        isinstance(index, bool)
        or not isinstance(index, int)
        or not 0 <= index <= 255
    ):
        raise HermesResponsesContractError(
            "hermes_sse_item_invalid"
        )
    return index


def _validate_sse_text_coordinates(
    event: Mapping[str, Any],
) -> tuple[str, int]:
    item_id = event.get("item_id")
    index = _sse_output_index(event)
    if (
        not isinstance(item_id, str)
        or not 1 <= len(item_id) <= 255
        or event.get("content_index") != 0
        or event.get("logprobs") != []
    ):
        raise HermesResponsesContractError(
            "hermes_sse_text_invalid"
        )
    return item_id, index


def _normalize_sse_function_call(
    item: Mapping[str, Any],
) -> dict[str, Any]:
    if set(item) != {
        "id",
        "type",
        "status",
        "name",
        "call_id",
        "arguments",
    } or item.get("status") != "completed":
        raise HermesResponsesContractError(
            "hermes_sse_item_invalid"
        )
    payload = {
        key: item[key]
        for key in ("type", "name", "arguments", "call_id")
    }
    try:
        return strict_model_validate(
            HermesFunctionCallItem,
            payload,
        ).model_dump(mode="python")
    except Exception as exc:
        raise HermesResponsesContractError(
            "hermes_sse_item_invalid"
        ) from exc


def _normalize_sse_function_output(
    item: Mapping[str, Any],
) -> dict[str, Any]:
    if set(item) != {
        "id",
        "type",
        "call_id",
        "output",
        "status",
    } or item.get("status") != "completed":
        raise HermesResponsesContractError(
            "hermes_sse_item_invalid"
        )
    raw_output = item.get("output")
    if (
        not isinstance(raw_output, list)
        or len(raw_output) != 1
        or type(raw_output[0]) is not dict
        or set(raw_output[0]) != {"type", "text"}
        or raw_output[0].get("type") != "input_text"
    ):
        raise HermesResponsesContractError(
            "hermes_sse_item_invalid"
        )
    payload = {
        "type": "function_call_output",
        "call_id": item.get("call_id"),
        "output": raw_output[0].get("text"),
    }
    try:
        return strict_model_validate(
            HermesFunctionOutputItem,
            payload,
        ).model_dump(mode="python")
    except Exception as exc:
        raise HermesResponsesContractError(
            "hermes_sse_item_invalid"
        ) from exc


def _normalize_sse_message(
    item: Mapping[str, Any],
) -> dict[str, Any]:
    if set(item) != {
        "id",
        "type",
        "status",
        "role",
        "content",
    } or item.get("status") != "completed":
        raise HermesResponsesContractError(
            "hermes_sse_item_invalid"
        )
    payload = {
        key: item[key]
        for key in ("type", "role", "content")
    }
    try:
        return strict_model_validate(
            HermesAssistantMessageItem,
            payload,
        ).model_dump(mode="python")
    except Exception as exc:
        raise HermesResponsesContractError(
            "hermes_sse_item_invalid"
        ) from exc


class _SyncPhaseCapacityError(RuntimeError):
    pass


def _consume_async_future(future: asyncio.Future[Any]) -> None:
    try:
        future.exception()
    except BaseException:
        pass


class _BoundedSyncPhaseRunner:
    """Run blocking phases on bounded daemon workers without extending deadlines."""

    def __init__(self, *, max_workers: int) -> None:
        if not 1 <= max_workers <= 64:
            raise ValueError("sync phase max_workers must be between 1 and 64")
        self._slots = BoundedSemaphore(max_workers)
        self._lock = Lock()
        self._active: set[ConcurrentFuture[Any]] = set()
        self._closed = Event()

    async def run[T](
        self,
        operation: Callable[[], T],
        *,
        deadline: float,
        late_result: Callable[[T], None] | None = None,
    ) -> T:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError
        future = self._submit(operation)
        wrapped = asyncio.wrap_future(future)
        try:
            result = await asyncio.wait_for(
                asyncio.shield(wrapped),
                timeout=remaining,
            )
        except TimeoutError:
            self._observe_late(future, late_result=late_result)
            wrapped.add_done_callback(_consume_async_future)
            raise
        except asyncio.CancelledError:
            self._observe_late(future, late_result=late_result)
            wrapped.add_done_callback(_consume_async_future)
            raise
        if monotonic() > deadline:
            if late_result is not None:
                self._call_late_result(late_result, result)
            raise TimeoutError
        return result

    def submit_detached(
        self,
        operation: Callable[[], Any],
        *,
        label: str,
    ) -> bool:
        try:
            future = self._submit(operation)
        except _SyncPhaseCapacityError:
            _LOGGER.error(
                "Hermes detached sync phase capacity exhausted: %s",
                label,
            )
            return False

        def completed(done: ConcurrentFuture[Any]) -> None:
            try:
                done.result()
            except BaseException:
                _LOGGER.exception(
                    "Hermes detached sync phase failed: %s",
                    label,
                )

        future.add_done_callback(completed)
        return True

    async def aclose(self, *, timeout_seconds: float) -> None:
        self._closed.set()
        deadline = monotonic() + timeout_seconds
        while True:
            with self._lock:
                active = tuple(self._active)
            if not active:
                return
            remaining = deadline - monotonic()
            if remaining <= 0:
                _LOGGER.error(
                    "Hermes sync phase shutdown left %d daemon worker(s)",
                    len(active),
                )
                return
            await asyncio.sleep(min(0.01, remaining))

    def close(self) -> None:
        self._closed.set()

    def _submit[T](
        self,
        operation: Callable[[], T],
    ) -> ConcurrentFuture[T]:
        if self._closed.is_set() or not self._slots.acquire(blocking=False):
            raise _SyncPhaseCapacityError
        future: ConcurrentFuture[T] = ConcurrentFuture()
        with self._lock:
            if self._closed.is_set():
                self._slots.release()
                raise _SyncPhaseCapacityError
            self._active.add(future)

        def worker() -> None:
            try:
                result = operation()
            except BaseException as exc:
                try:
                    future.set_exception(exc)
                finally:
                    with self._lock:
                        self._active.discard(future)
                    self._slots.release()
            else:
                try:
                    future.set_result(result)
                finally:
                    with self._lock:
                        self._active.discard(future)
                    self._slots.release()

        thread = Thread(
            target=worker,
            name="healthmes-hermes-sync-phase",
            daemon=True,
        )
        try:
            thread.start()
        except BaseException:
            with self._lock:
                self._active.discard(future)
            self._slots.release()
            raise
        return future

    @staticmethod
    def _observe_late[T](
        future: ConcurrentFuture[T],
        *,
        late_result: Callable[[T], None] | None,
    ) -> None:
        def completed(done: ConcurrentFuture[T]) -> None:
            try:
                result = done.result()
            except BaseException:
                return
            if late_result is not None:
                _BoundedSyncPhaseRunner._call_late_result(
                    late_result,
                    result,
                )

        future.add_done_callback(completed)

    @staticmethod
    def _call_late_result[T](
        handler: Callable[[T], None],
        result: T,
    ) -> None:
        try:
            handler(result)
        except BaseException:
            _LOGGER.exception("Hermes late sync result cleanup failed")


@dataclass(frozen=True, slots=True)
class HermesSessionCleanupStatus:
    """Observable state for response-detached Hermes transcript cleanup."""

    scheduled: int
    succeeded: int
    failed: int
    pending: int


class HermesResponsesDecisionAgent:
    """Use one Hermes autonomous turn and HealthMes search session."""

    def __init__(
        self,
        *,
        transport: HermesResponsesTransport,
        search_service: DecisionContextSearchSessionService,
        model: str,
        provider: str,
        timeout_seconds: float = 60,
        cleanup_attempts: int = 2,
        cleanup_timeout_seconds: float = 5,
        cleanup_retry_seconds: float = 0.05,
        sync_phase_max_workers: int = 8,
        sync_phase_shutdown_timeout_seconds: float = 0.25,
        session_ttl_seconds: float = 900,
        session_purge_interval_seconds: float = 60,
        session_purge_max_pages: int = 5,
        profile_assertion: HermesDecisionProfileAssertion | None = None,
        owns_search_service: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not 1 <= cleanup_attempts <= 5:
            raise ValueError("cleanup_attempts must be between 1 and 5")
        if cleanup_timeout_seconds <= 0 or cleanup_timeout_seconds > 30:
            raise ValueError(
                "cleanup_timeout_seconds must be greater than 0 and at "
                "most 30"
            )
        if cleanup_retry_seconds < 0 or cleanup_retry_seconds > 1:
            raise ValueError(
                "cleanup_retry_seconds must be between 0 and 1"
            )
        if not 1 <= sync_phase_max_workers <= 64:
            raise ValueError(
                "sync_phase_max_workers must be between 1 and 64"
            )
        if (
            sync_phase_shutdown_timeout_seconds <= 0
            or sync_phase_shutdown_timeout_seconds > 30
        ):
            raise ValueError(
                "sync_phase_shutdown_timeout_seconds must be greater "
                "than 0 and at most 30"
            )
        if session_ttl_seconds <= timeout_seconds:
            raise ValueError(
                "session_ttl_seconds must exceed the decision timeout"
            )
        if (
            session_purge_interval_seconds <= 0
            or session_purge_interval_seconds > session_ttl_seconds
        ):
            raise ValueError(
                "session purge interval must be positive and no greater "
                "than the session TTL"
            )
        if not 1 <= session_purge_max_pages <= 10:
            raise ValueError(
                "session_purge_max_pages must be between 1 and 10"
            )
        self._transport = transport
        self._search_service = search_service
        self._model = _identity(model, "model")
        self._provider = _identity(provider, "provider")
        self._timeout_seconds = timeout_seconds
        self._cleanup_attempts = cleanup_attempts
        self._cleanup_timeout_seconds = cleanup_timeout_seconds
        self._cleanup_retry_seconds = cleanup_retry_seconds
        self._sync_phase_shutdown_timeout_seconds = (
            sync_phase_shutdown_timeout_seconds
        )
        self._session_ttl_seconds = session_ttl_seconds
        self._session_purge_interval_seconds = (
            session_purge_interval_seconds
        )
        self._session_purge_max_pages = session_purge_max_pages
        self._profile_assertion = profile_assertion
        self._profile_digest: str | None = None
        self._tool_allowlist = HERMES_DECISION_SEARCH_TOOL_ALLOWLIST
        self._owns_search_service = owns_search_service
        self._clock = clock or (lambda: datetime.now(UTC))
        self._closed = Event()
        self._profile_lock: asyncio.Lock | None = None
        self._profile_verified = False
        self._maintenance_stop: asyncio.Event | None = None
        self._maintenance_task: asyncio.Task[None] | None = None
        self._cleanup_tasks: set[asyncio.Task[bool]] = set()
        self._cleanup_state_lock = Lock()
        self._cleanup_scheduled = 0
        self._cleanup_succeeded = 0
        self._cleanup_failed = 0
        self._sync_runner = _BoundedSyncPhaseRunner(
            max_workers=sync_phase_max_workers
        )
        self._session_purge_offset = 0
        self._metadata = RuntimeMetadata(
            runtime="hermes",
            model=self._model,
            provider=self._provider,
        )

    async def start(self) -> None:
        """Probe profile, live inventory, native tools, and model route."""

        if self._closed.is_set():
            raise HermesResponsesTransportError(
                "hermes_responses_runtime_closed"
            )
        if self._profile_verified:
            return
        if self._profile_lock is None:
            self._profile_lock = asyncio.Lock()
        async with self._profile_lock:
            if self._profile_verified:
                return
            deadline = monotonic() + self._timeout_seconds
            try:
                if self._profile_assertion is not None:
                    details = await self._sync_runner.run(
                        self._profile_assertion.verify_details,
                        deadline=deadline,
                    )
                    self._profile_digest = details.semantic_digest
                    self._tool_allowlist = details.full_tool_names
                await _before_deadline(
                    self._transport.verify_runtime(
                        timeout_seconds=_remaining_before_deadline(
                            deadline
                        ),
                    ),
                    deadline,
                )
                raw_toolsets, raw_models = await _before_deadline(
                    asyncio.gather(
                        self._transport.get_toolsets(),
                        self._transport.get_models(),
                    ),
                    deadline,
                )
                strict_model_validate(
                    HermesToolsetsResponse,
                    raw_toolsets,
                )
                models = strict_model_validate(
                    HermesModelsResponse,
                    raw_models,
                )
                _validate_model_route(models, expected_model=self._model)
                if self._profile_assertion is not None:
                    await _before_deadline(
                        self._purge_expired_sessions(),
                        deadline,
                    )
            except TimeoutError as exc:
                raise HermesResponsesTransportError(
                    "hermes_tool_profile_timeout"
                ) from exc
            except _SyncPhaseCapacityError as exc:
                raise HermesResponsesTransportError(
                    "hermes_sync_phase_capacity_exhausted"
                ) from exc
            except HermesDecisionProfileError as exc:
                raise HermesResponsesContractError(str(exc)) from exc
            except HermesResponsesError:
                raise
            except (ValidationError, ValueError, TypeError) as exc:
                raise HermesResponsesContractError(
                    "hermes_tool_profile_unsafe"
                ) from exc
            except Exception as exc:
                raise HermesResponsesTransportError(
                    "hermes_tool_profile_unavailable"
                ) from exc
            self._profile_verified = True
            if self._profile_assertion is not None:
                self._maintenance_stop = asyncio.Event()
                self._maintenance_task = asyncio.create_task(
                    self._session_maintenance_loop(),
                    name="healthmes-hermes-session-maintenance",
                )

    async def ask(self, raw_request: DecisionRequest) -> DecisionAgentRun:
        """Execute one request under one absolute pre-finalization deadline."""

        started_at = _utc(self._clock())
        deadline = monotonic() + self._timeout_seconds
        try:
            request = strict_model_validate(DecisionRequest, raw_request)
        except Exception:
            return self._failure_run(
                request_id=getattr(raw_request, "request_id", uuid.uuid4()),
                turn_id=getattr(raw_request, "turn_id", uuid.uuid4()),
                started_at=started_at,
                code="invalid_decision_request",
            )
        if self._closed.is_set():
            return self._failure_run(
                request=request,
                started_at=started_at,
                code="hermes_responses_runtime_closed",
                status=DecisionStatus.BLOCKED,
            )
        try:
            await _before_deadline(self.start(), deadline)
        except HermesResponsesError as exc:
            return self._failure_run(
                request=request,
                started_at=started_at,
                code=exc.code,
                status=DecisionStatus.BLOCKED,
            )
        except TimeoutError:
            return self._failure_run(
                request=request,
                started_at=started_at,
                code="hermes_tool_profile_timeout",
                status=DecisionStatus.BLOCKED,
            )

        handle: Any | None = None
        try:
            handle = await self._sync_runner.run(
                lambda: self._search_service.begin(request),
                deadline=deadline,
                late_result=self._abort_late_search_handle,
            )
        except TimeoutError:
            return self._failure_run(
                request=request,
                started_at=started_at,
                code="hermes_responses_timeout",
                status=DecisionStatus.BLOCKED,
            )
        except _SyncPhaseCapacityError:
            return self._failure_run(
                request=request,
                started_at=started_at,
                code="hermes_sync_phase_capacity_exhausted",
                status=DecisionStatus.BLOCKED,
            )
        except Exception:
            return self._failure_run(
                request=request,
                started_at=started_at,
                code="decision_search_session_unavailable",
            )

        session_id: str | None = None
        snapshot: DecisionSearchSessionSnapshot | None = None
        try:
            remaining = _remaining_before_deadline(deadline)
            response_request = _responses_request(
                request,
                decision_session_id=handle.session_id,
                model=self._model,
                profile_digest=self._profile_digest,
                tool_allowlist=self._tool_allowlist,
            )
            response = await _before_deadline(
                self._transport.create_response(
                    response_request,
                    timeout_seconds=remaining,
                ),
                deadline,
            )
            session_id = response.session_id
            snapshot = await self._sync_runner.run(
                lambda: self._search_service.finish(handle.session_id),
                deadline=deadline,
            )
            run = await self._sync_runner.run(
                lambda: _run_from_response(
                    request=request,
                    raw_response=response.payload,
                    snapshot=snapshot,
                    expected_model=self._model,
                    provider=self._provider,
                    started_at=started_at,
                    finished_at=_utc(self._clock()),
                    tool_allowlist=self._tool_allowlist,
                ),
                deadline=deadline,
            )
        except asyncio.CancelledError:
            if snapshot is None:
                self._detach_search_abort(handle.session_id)
            raise
        except TimeoutError:
            if snapshot is None:
                self._detach_search_abort(handle.session_id)
            run = self._failure_run(
                request=request,
                started_at=started_at,
                code="hermes_responses_timeout",
                snapshot=snapshot,
            )
        except _SyncPhaseCapacityError:
            if snapshot is None:
                self._detach_search_abort(handle.session_id)
            run = self._failure_run(
                request=request,
                started_at=started_at,
                code="hermes_sync_phase_capacity_exhausted",
                status=DecisionStatus.BLOCKED,
                snapshot=snapshot,
            )
        except HermesResponsesError as exc:
            if snapshot is None:
                snapshot = await self._abort_search_before_deadline(
                    handle.session_id,
                    deadline=deadline,
                )
            run = self._failure_run(
                request=request,
                started_at=started_at,
                code=exc.code,
                status=(
                    DecisionStatus.BLOCKED
                    if exc.code.endswith(
                        ("unavailable", "timeout", "deadline_expired")
                    )
                    else DecisionStatus.FAILED
                ),
                snapshot=snapshot,
            )
        except Exception:
            if snapshot is None:
                snapshot = await self._abort_search_before_deadline(
                    handle.session_id,
                    deadline=deadline,
                )
            run = self._failure_run(
                request=request,
                started_at=started_at,
                code="hermes_responses_execution_failed",
                snapshot=snapshot,
            )
        finally:
            if session_id is not None:
                self._schedule_cleanup(session_id)
        return run

    async def _abort_search_before_deadline(
        self,
        session_id: str,
        *,
        deadline: float,
    ) -> DecisionSearchSessionSnapshot | None:
        try:
            return await self._sync_runner.run(
                lambda: self._search_service.abort(session_id),
                deadline=deadline,
            )
        except (TimeoutError, _SyncPhaseCapacityError):
            self._detach_search_abort(session_id)
        except Exception:
            return None
        return None

    def _abort_late_search_handle(self, handle: Any) -> None:
        session_id = getattr(handle, "session_id", None)
        if isinstance(session_id, str):
            _abort_search_session(self._search_service, session_id)

    def _detach_search_abort(self, session_id: str) -> None:
        self._sync_runner.submit_detached(
            lambda: self._search_service.abort(session_id),
            label="decision-search-abort",
        )

    def _schedule_cleanup(self, session_id: str) -> None:
        task = asyncio.create_task(
            self._tracked_cleanup_session(session_id),
            name=f"healthmes-hermes-session-cleanup-{session_id[:24]}",
        )
        with self._cleanup_state_lock:
            self._cleanup_scheduled += 1
            self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_task_finished)

    async def _tracked_cleanup_session(self, session_id: str) -> bool:
        succeeded = False
        try:
            succeeded = await self._cleanup_session(session_id)
            return succeeded
        finally:
            with self._cleanup_state_lock:
                if succeeded:
                    self._cleanup_succeeded += 1
                else:
                    self._cleanup_failed += 1

    def _cleanup_task_finished(self, task: asyncio.Task[bool]) -> None:
        with self._cleanup_state_lock:
            self._cleanup_tasks.discard(task)
        try:
            task.result()
        except BaseException:
            pass

    def cleanup_status(self) -> HermesSessionCleanupStatus:
        """Return bounded cleanup counters without exposing session IDs."""

        with self._cleanup_state_lock:
            return HermesSessionCleanupStatus(
                scheduled=self._cleanup_scheduled,
                succeeded=self._cleanup_succeeded,
                failed=self._cleanup_failed,
                pending=sum(
                    not task.done() for task in self._cleanup_tasks
                ),
            )

    async def _cleanup_session(
        self,
        session_id: str,
    ) -> bool:
        deadline = monotonic() + self._cleanup_timeout_seconds
        for attempt in range(self._cleanup_attempts):
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(
                    self._transport.delete_session(session_id),
                    timeout=remaining,
                )
                return True
            except asyncio.CancelledError:
                raise
            except Exception:
                if attempt + 1 >= self._cleanup_attempts:
                    break
                delay = min(
                    self._cleanup_retry_seconds,
                    max(0.0, deadline - monotonic()),
                )
                if delay:
                    await asyncio.sleep(delay)
        _LOGGER.error("Hermes decision session cleanup failed")
        return False

    async def _purge_expired_sessions(self) -> None:
        cutoff = _utc(self._clock()) - timedelta(
            seconds=self._session_ttl_seconds
        )
        expired_ids: list[str] = []
        page_size = 200
        reached_end = False
        next_offset = self._session_purge_offset
        for page_index in range(self._session_purge_max_pages):
            offset = self._session_purge_offset + page_index * page_size
            if offset > 1_000_000:
                reached_end = True
                break
            raw_page = await self._transport.list_sessions(
                limit=page_size,
                offset=offset,
            )
            try:
                page = strict_model_validate(
                    HermesSessionsResponse,
                    raw_page,
                )
            except Exception as exc:
                raise HermesResponsesContractError(
                    "hermes_session_list_invalid"
                ) from exc
            if page.limit != page_size or page.offset != offset:
                raise HermesResponsesContractError(
                    "hermes_session_list_invalid"
                )
            for session in page.data:
                active_at = _session_timestamp(
                    session.last_active
                    if session.last_active is not None
                    else session.started_at
                )
                if active_at is None:
                    raise HermesResponsesContractError(
                        "hermes_session_list_invalid"
                    )
                if active_at < cutoff:
                    expired_ids.append(session.id)
            next_offset = offset + page_size
            if not page.has_more:
                reached_end = True
                break
        for session_id in expired_ids:
            await self._transport.delete_session(session_id)
        if reached_end:
            self._session_purge_offset = 0
        else:
            # Deleted rows precede the next offset and shift unseen rows left.
            self._session_purge_offset = max(
                0,
                next_offset - len(expired_ids),
            )

    async def _session_maintenance_loop(self) -> None:
        stop = self._maintenance_stop
        if stop is None:
            return
        while not stop.is_set():
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=self._session_purge_interval_seconds,
                )
            except TimeoutError:
                pass
            if stop.is_set():
                return
            try:
                await self._purge_expired_sessions()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception(
                    "Hermes decision session TTL maintenance failed"
                )

    async def aclose(self) -> None:
        self._closed.set()
        if self._maintenance_stop is not None:
            self._maintenance_stop.set()
        task = self._maintenance_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._drain_cleanup_tasks()
        await self._sync_runner.aclose(
            timeout_seconds=self._sync_phase_shutdown_timeout_seconds
        )
        if self._owns_search_service:
            self._search_service.close()

    async def _drain_cleanup_tasks(self) -> None:
        with self._cleanup_state_lock:
            tasks = tuple(self._cleanup_tasks)
        if not tasks:
            return
        _done, pending = await asyncio.wait(
            tasks,
            timeout=self._cleanup_timeout_seconds,
        )
        for cleanup in pending:
            cleanup.cancel()
        if pending:
            _cancelled, still_pending = await asyncio.wait(
                pending,
                timeout=min(0.1, self._cleanup_timeout_seconds),
            )
            if still_pending:
                _LOGGER.error(
                    "Hermes cleanup shutdown left %d task(s) pending",
                    len(still_pending),
                )

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self._maintenance_stop is not None:
            self._maintenance_stop.set()
        task = self._maintenance_task
        if task is not None and not task.done():
            task.cancel()
        with self._cleanup_state_lock:
            cleanup_tasks = tuple(self._cleanup_tasks)
        for cleanup in cleanup_tasks:
            cleanup.cancel()
        self._sync_runner.close()
        if self._owns_search_service:
            self._search_service.close()

    def _failure_run(
        self,
        request: DecisionRequest | None = None,
        *,
        request_id: uuid.UUID | None = None,
        turn_id: uuid.UUID | None = None,
        started_at: datetime,
        code: str,
        status: DecisionStatus = DecisionStatus.FAILED,
        snapshot: DecisionSearchSessionSnapshot | None = None,
    ) -> DecisionAgentRun:
        if request is not None:
            request_id = request.request_id
            turn_id = request.turn_id
        if request_id is None or turn_id is None:
            raise ValueError("failure run requires request identifiers")
        tool_trace = _snapshot_tool_trace(snapshot)
        return DecisionAgentRun(
            request_id=request_id,
            turn_id=turn_id,
            draft=DecisionDraft(
                status=status,
                limitations=[code],
            ),
            source_refs=(
                snapshot.source_refs if snapshot is not None else ()
            ),
            runtime=self._metadata,
            steps_used=0,
            tool_trace=tool_trace,
            access_trace=(
                snapshot.access_trace if snapshot is not None else ()
            ),
            system_policy_version=HERMES_RESPONSES_POLICY_VERSION,
            started_at=started_at,
            finished_at=_utc(self._clock()),
        )


def _responses_request(
    request: DecisionRequest,
    *,
    decision_session_id: str,
    model: str,
    profile_digest: str | None,
    tool_allowlist: frozenset[str],
) -> dict[str, Any]:
    request_payload = {
        "schema": "healthmes.decision-request.v1",
        "request_id": str(request.request_id),
        "turn_id": str(request.turn_id),
        "question": request.question,
        "requested_at": request.requested_at.isoformat(),
        "timezone": request.timezone,
        "requested_privacy_level": request.requested_privacy_level.value,
        "persistence_requested": request.persistence_requested,
        "caller": {
            "channel": request.caller.channel,
            "execution_scope": request.caller.execution_scope.value,
        },
        "hints": request.hints.model_dump(mode="json", round_trip=True),
        "budget": request.budget.model_dump(mode="json", round_trip=True),
        "decision_session_id": decision_session_id,
    }
    search_tools = sorted(
        tool_allowlist & HERMES_DECISION_SEARCH_TOOL_ALLOWLIST
    )
    skill_tools = sorted(
        tool_allowlist & HERMES_DECISION_SKILL_TOOL_ALLOWLIST
    )
    instructions = (
        "You are the only LLM reasoning loop for one HealthMes wellness "
        "decision. Choose zero or more of exactly these tools as needed: "
        + ", ".join(sorted(tool_allowlist))
        + ". Search tools "
        + ", ".join(search_tools)
        + " MUST include decision_session_id exactly as provided in the "
        "request. "
        + (
            "The read-only skill tools "
            + ", ".join(skill_tools)
            + " do not accept decision_session_id; use them only for reviewed "
            "wellness guidance. "
            if skill_tools
            else ""
        )
        + "Never call native Hermes tools, another "
        "MCP server, mutation tools, memory, filesystem, network, shell, "
        "skills_list, or skill_view. Use tool results as authoritative. "
        "Missing, partial, stale, denied, and unavailable data are not zero. "
        "After any tool calls, return exactly one JSON object and no markdown "
        "or prose. The object must have exactly two keys: "
        '{"schema":"healthmes.decision-draft.v1","decision":...}. '
        "The decision value must contain only DecisionDraft fields: status, "
        "answer, record_summary, proposed_action, persistence_intent, "
        "used_source_ref_ids, limitations, clarification_question, "
        "confidence, uncertainty, and follow_up_question. "
        "persistence_intent is required on every response and must be one of "
        "none, action, risk, mutation, or explicit_tracking. When the result "
        "may be retained because persistence_intent is not none or the "
        "request has persistence_requested=true, record_summary is required. "
        "It must be a privacy-minimized conclusion of at most 160 characters, "
        "not a truncation of the answer, and must omit raw identifiers and "
        "sensitive detail. Use only source reference IDs returned by tools. "
        "A proposed action requires at least one source reference. Ask one "
        "concrete clarification question when a required candidate amount, "
        "identity, time, or user fact is missing. Do not diagnose disease."
    )
    payload: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": [
            {
                "role": "user",
                "content": json.dumps(
                    request_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            }
        ],
        "store": False,
        "stream": True,
    }
    if profile_digest is not None:
        payload["metadata"] = {
            "healthmes_request_id": str(request.request_id),
            "healthmes_turn_id": str(request.turn_id),
            "healthmes_profile_digest": profile_digest,
        }
    return payload


def _run_from_response(
    *,
    request: DecisionRequest,
    raw_response: Mapping[str, Any],
    snapshot: DecisionSearchSessionSnapshot,
    expected_model: str,
    provider: str,
    started_at: datetime,
    finished_at: datetime,
    tool_allowlist: frozenset[str],
) -> DecisionAgentRun:
    try:
        response = strict_model_validate(
            HermesResponsesResponse,
            raw_response,
        )
    except Exception as exc:
        raise HermesResponsesContractError(
            "hermes_response_contract_invalid"
        ) from exc
    if response.model != expected_model:
        raise HermesResponsesContractError(
            "hermes_response_model_mismatch"
        )
    function_pairs, final_text = _validate_transcript(
        response.output,
        decision_session_id=snapshot.session_id,
        tool_allowlist=tool_allowlist,
    )
    search_pairs = tuple(
        pair
        for pair in function_pairs
        if pair[0].name in HERMES_DECISION_SEARCH_TOOL_ALLOWLIST
    )
    skill_pairs = tuple(
        pair
        for pair in function_pairs
        if pair[0].name in HERMES_DECISION_SKILL_TOOL_ALLOWLIST
    )
    tool_trace = _snapshot_tool_trace(snapshot)
    if len(search_pairs) != len(tool_trace):
        raise HermesResponsesContractError(
            "hermes_tool_trace_mismatch"
        )
    for call, output in skill_pairs:
        _validate_skill_tool_pair(call, output)

    trace_by_query = {
        str(record.query.query_id): record for record in tool_trace
    }
    if len(trace_by_query) != len(tool_trace):
        raise HermesResponsesContractError(
            "hermes_tool_trace_mismatch"
        )
    transcript_ref_ids: set[str] = set()
    seen_queries: set[str] = set()
    transcript_query_ids: list[str] = []
    for call, output in search_pairs:
        result = _parse_tool_output(output.output)
        query_id = str(result.query_id)
        record = trace_by_query.get(query_id)
        if record is None or query_id in seen_queries:
            raise HermesResponsesContractError(
                "hermes_tool_trace_mismatch"
            )
        seen_queries.add(query_id)
        transcript_query_ids.append(query_id)
        expected_domain = HERMES_DECISION_TOOL_DOMAINS[call.name]
        if not result.capability.startswith(f"{expected_domain}."):
            raise HermesResponsesContractError(
                "hermes_tool_domain_mismatch"
            )
        _validate_tool_arguments_against_query(
            call,
            record.query,
            decision_session_id=snapshot.session_id,
        )
        if record.result is None:
            raise HermesResponsesContractError(
                "hermes_tool_trace_mismatch"
            )
        transcript_context = ContextResult.model_validate(
            result.model_dump(
                mode="python",
                round_trip=True,
                exclude={"access_audit"},
            )
        )
        if transcript_context != record.result:
            raise HermesResponsesContractError(
                "hermes_tool_output_mismatch"
            )
        transcript_ref_ids.update(
            item.reference_id for item in result.source_refs
        )
    if seen_queries != set(trace_by_query):
        raise HermesResponsesContractError(
            "hermes_tool_trace_mismatch"
        )
    if transcript_query_ids != [
        str(record.query.query_id) for record in tool_trace
    ]:
        raise HermesResponsesContractError(
            "hermes_tool_trace_order_mismatch"
        )

    envelope = _parse_final_draft(final_text)
    available_ref_ids = {
        source_ref.reference_id for source_ref in snapshot.source_refs
    }
    if transcript_ref_ids != available_ref_ids:
        raise HermesResponsesContractError(
            "hermes_source_ref_transcript_mismatch"
        )
    used_ref_ids = set(envelope.decision.used_source_ref_ids)
    if not used_ref_ids.issubset(available_ref_ids):
        raise HermesResponsesContractError(
            "hermes_source_ref_fabricated"
        )
    if (
        envelope.decision.status is DecisionStatus.COMPLETED
        and available_ref_ids
        and not used_ref_ids
    ):
        raise HermesResponsesContractError(
            "hermes_source_refs_omitted"
        )
    return DecisionAgentRun(
        request_id=request.request_id,
        turn_id=request.turn_id,
        draft=envelope.decision.model_copy(deep=True),
        source_refs=tuple(
            source_ref.model_copy(deep=True)
            for source_ref in snapshot.source_refs
        ),
        runtime=RuntimeMetadata(
            runtime="hermes",
            model=response.model,
            provider=provider,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        ),
        steps_used=1,
        tool_trace=tool_trace,
        access_trace=tuple(
            entry.model_copy(deep=True)
            for entry in snapshot.access_trace
        ),
        system_policy_version=HERMES_RESPONSES_POLICY_VERSION,
        started_at=started_at,
        finished_at=finished_at,
    )


def _validate_transcript(
    raw_items: tuple[dict[str, Any], ...],
    *,
    decision_session_id: str,
    tool_allowlist: frozenset[str],
) -> tuple[
    tuple[tuple[HermesFunctionCallItem, HermesFunctionOutputItem], ...],
    str,
]:
    calls: dict[str, HermesFunctionCallItem] = {}
    completed: set[str] = set()
    pairs: list[
        tuple[HermesFunctionCallItem, HermesFunctionOutputItem]
    ] = []
    final_messages: list[HermesAssistantMessageItem] = []
    final_seen = False

    for raw_item in raw_items:
        if type(raw_item) is not dict:
            raise HermesResponsesContractError(
                "hermes_response_contract_invalid"
            )
        item_type = raw_item.get("type")
        try:
            if item_type == "function_call":
                if final_seen:
                    raise HermesResponsesContractError(
                        "hermes_transcript_order_invalid"
                    )
                call = strict_model_validate(
                    HermesFunctionCallItem,
                    raw_item,
                )
                if call.name not in tool_allowlist:
                    raise HermesResponsesContractError(
                        "hermes_tool_not_allowed"
                    )
                if call.call_id in calls:
                    raise HermesResponsesContractError(
                        "hermes_tool_pair_invalid"
                    )
                arguments = _parse_json_object(
                    call.arguments,
                    code="hermes_tool_arguments_invalid",
                    max_bytes=256_000,
                )
                if (
                    call.name in HERMES_DECISION_SEARCH_TOOL_ALLOWLIST
                    and arguments.get("decision_session_id")
                    != decision_session_id
                ):
                    raise HermesResponsesContractError(
                        "hermes_decision_session_mismatch"
                    )
                if call.name in HERMES_DECISION_SKILL_TOOL_ALLOWLIST:
                    _validate_skill_tool_arguments(call.name, arguments)
                calls[call.call_id] = call
            elif item_type == "function_call_output":
                if final_seen:
                    raise HermesResponsesContractError(
                        "hermes_transcript_order_invalid"
                    )
                output = strict_model_validate(
                    HermesFunctionOutputItem,
                    raw_item,
                )
                call = calls.get(output.call_id)
                if call is None or output.call_id in completed:
                    raise HermesResponsesContractError(
                        "hermes_tool_pair_invalid"
                    )
                completed.add(output.call_id)
                pairs.append((call, output))
            elif item_type == "message":
                message = strict_model_validate(
                    HermesAssistantMessageItem,
                    raw_item,
                )
                final_seen = True
                final_messages.append(message)
            else:
                raise HermesResponsesContractError(
                    "hermes_response_contract_invalid"
                )
        except ValidationError as exc:
            raise HermesResponsesContractError(
                "hermes_response_contract_invalid"
            ) from exc
    if set(calls) != completed:
        raise HermesResponsesContractError("hermes_tool_pair_invalid")
    if len(final_messages) != 1 or not raw_items:
        raise HermesResponsesContractError(
            "hermes_final_message_invalid"
        )
    if raw_items[-1].get("type") != "message":
        raise HermesResponsesContractError(
            "hermes_final_message_invalid"
        )
    return tuple(pairs), final_messages[0].content[0].text


def _parse_tool_output(raw_output: str) -> ContextSearchResult:
    payload = _unwrap_tool_output(raw_output)
    try:
        return strict_model_validate(ContextSearchResult, payload)
    except Exception as exc:
        raise HermesResponsesContractError(
            "hermes_tool_output_invalid"
        ) from exc


def _unwrap_tool_output(raw_output: str) -> dict[str, Any]:
    outer = _parse_json_object(
        raw_output,
        code="hermes_tool_output_invalid",
        max_bytes=_MAX_TOOL_OUTPUT_BYTES,
    )
    if "error" in outer:
        raise HermesResponsesContractError("hermes_tool_output_error")
    if "structuredContent" in outer:
        payload: Any = outer["structuredContent"]
    elif "result" in outer:
        payload = outer["result"]
    else:
        raise HermesResponsesContractError(
            "hermes_tool_output_invalid"
        )
    if type(payload) is str:
        payload = _parse_json_object(
            payload,
            code="hermes_tool_output_invalid",
            max_bytes=_MAX_TOOL_OUTPUT_BYTES,
        )
    if type(payload) is not dict:
        raise HermesResponsesContractError(
            "hermes_tool_output_invalid"
        )
    return payload


def _validate_skill_tool_arguments(
    tool_name: str,
    arguments: Mapping[str, Any],
) -> None:
    if tool_name == "mcp__healthmes__list_wellness_skills":
        if arguments:
            raise HermesResponsesContractError(
                "hermes_skill_tool_arguments_invalid"
            )
        return
    if tool_name == "mcp__healthmes__read_wellness_skill":
        name = arguments.get("name")
        if (
            set(arguments) != {"name"}
            or not isinstance(name, str)
            or not name.strip()
            or len(name) > 255
        ):
            raise HermesResponsesContractError(
                "hermes_skill_tool_arguments_invalid"
            )
        return
    raise HermesResponsesContractError("hermes_tool_not_allowed")


def _validate_skill_tool_pair(
    call: HermesFunctionCallItem,
    output: HermesFunctionOutputItem,
) -> None:
    arguments = _parse_json_object(
        call.arguments,
        code="hermes_skill_tool_arguments_invalid",
        max_bytes=256_000,
    )
    _validate_skill_tool_arguments(call.name, arguments)
    payload = _unwrap_tool_output(output.output)
    if payload.get("schema") != HERMES_WELLNESS_SKILL_CATALOG_SCHEMA:
        raise HermesResponsesContractError(
            "hermes_skill_tool_output_invalid"
        )
    if call.name == "mcp__healthmes__list_wellness_skills":
        skills = payload.get("skills")
        if set(payload) != {"schema", "skills"} or not isinstance(
            skills, list
        ):
            raise HermesResponsesContractError(
                "hermes_skill_tool_output_invalid"
            )
        names: set[str] = set()
        for metadata in skills:
            name = _validate_skill_metadata(metadata)
            if name in names:
                raise HermesResponsesContractError(
                    "hermes_skill_tool_output_invalid"
                )
            names.add(name)
        return
    if call.name != "mcp__healthmes__read_wellness_skill":
        raise HermesResponsesContractError("hermes_tool_not_allowed")
    if set(payload) != {"schema", "skill", "content"}:
        raise HermesResponsesContractError(
            "hermes_skill_tool_output_invalid"
        )
    metadata = payload.get("skill")
    content = payload.get("content")
    name = _validate_skill_metadata(metadata)
    if name != arguments["name"] or not isinstance(content, str):
        raise HermesResponsesContractError(
            "hermes_skill_tool_output_invalid"
        )
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise HermesResponsesContractError(
            "hermes_skill_tool_output_invalid"
        ) from exc
    if (
        len(encoded) != metadata["bytes"]
        or hashlib.sha256(encoded).hexdigest() != metadata["sha256"]
    ):
        raise HermesResponsesContractError(
            "hermes_skill_tool_output_invalid"
        )


def _validate_skill_metadata(raw: Any) -> str:
    if type(raw) is not dict or set(raw) not in (
        {"name", "description", "sha256", "bytes"},
        {"name", "description", "sha256", "bytes", "version"},
    ):
        raise HermesResponsesContractError(
            "hermes_skill_tool_output_invalid"
        )
    name = raw.get("name")
    description = raw.get("description")
    digest = raw.get("sha256")
    byte_count = raw.get("bytes")
    version = raw.get("version")
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > 255
        or not isinstance(description, str)
        or not description.strip()
        or len(description) > 4_096
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or isinstance(byte_count, bool)
        or not isinstance(byte_count, int)
        or not 0 < byte_count <= 64_000
        or (
            "version" in raw
            and (
                not isinstance(version, str)
                or not version.strip()
                or len(version) > 255
            )
        )
    ):
        raise HermesResponsesContractError(
            "hermes_skill_tool_output_invalid"
        )
    return name


def _parse_final_draft(raw_text: str) -> HermesDecisionDraftEnvelope:
    if "```" in raw_text:
        raise HermesResponsesContractError(
            "hermes_final_json_invalid"
        )
    try:
        payload = _parse_json_object(
            raw_text,
            code="hermes_final_json_invalid",
            max_bytes=64_000,
        )
        if set(payload) != {"schema", "decision"} or payload.get(
            "schema"
        ) != HERMES_DECISION_DRAFT_SCHEMA:
            raise HermesResponsesContractError(
                "hermes_final_json_invalid"
            )
        decision = payload.get("decision")
        if type(decision) is not dict:
            raise HermesResponsesContractError(
                "hermes_final_json_invalid"
            )
        if "persistence_intent" not in decision:
            raise HermesResponsesContractError(
                "hermes_persistence_intent_missing"
            )
        return strict_model_validate(
            HermesDecisionDraftEnvelope,
            payload,
        )
    except HermesResponsesContractError:
        raise
    except Exception as exc:
        raise HermesResponsesContractError(
            "hermes_final_json_invalid"
        ) from exc


def _parse_json_object(
    raw: str,
    *,
    code: str,
    max_bytes: int,
) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise HermesResponsesContractError(code)
    encoded = raw.encode("utf-8")
    if len(encoded) > max_bytes:
        raise HermesResponsesContractError(code)
    try:
        decoder = json.JSONDecoder(
            object_pairs_hook=_reject_duplicate_json_members
        )
        value, end = decoder.raw_decode(raw.lstrip())
        trailing = raw.lstrip()[end:]
    except (
        UnicodeEncodeError,
        json.JSONDecodeError,
        _DuplicateJsonMember,
    ) as exc:
        raise HermesResponsesContractError(code) from exc
    if trailing.strip() or type(value) is not dict:
        raise HermesResponsesContractError(code)
    try:
        normalized = normalize_untrusted_json(
            value,
            max_bytes=max_bytes,
        )
    except Exception as exc:
        raise HermesResponsesContractError(code) from exc
    if type(normalized.value) is not dict:
        raise HermesResponsesContractError(code)
    return normalized.value


def _snapshot_tool_trace(
    snapshot: DecisionSearchSessionSnapshot | None,
) -> tuple[ToolCallRecord, ...]:
    if snapshot is None:
        return ()
    raw_trace = getattr(snapshot, "tool_trace", ())
    try:
        return tuple(
            strict_model_validate(ToolCallRecord, item)
            for item in raw_trace
        )
    except Exception as exc:
        raise HermesResponsesContractError(
            "decision_search_trace_invalid"
        ) from exc


def _validate_model_route(
    models: HermesModelsResponse,
    *,
    expected_model: str,
) -> None:
    matches = [item for item in models.data if item.id == expected_model]
    if (
        len(matches) != 1
        or matches[0].root != expected_model
        or matches[0].parent != HERMES_DECISION_RUNTIME_MODEL_NAME
    ):
        raise HermesResponsesContractError(
            "hermes_model_route_unavailable"
        )


def _validate_tool_arguments_against_query(
    call: HermesFunctionCallItem,
    query,
    *,
    decision_session_id: str,
) -> None:
    arguments = _parse_json_object(
        call.arguments,
        code="hermes_tool_arguments_invalid",
        max_bytes=256_000,
    )
    expected_domain = HERMES_DECISION_TOOL_DOMAINS[call.name]
    if query.provider_id != expected_domain:
        raise HermesResponsesContractError(
            "hermes_tool_arguments_trace_mismatch"
        )
    normalized = _normalize_tool_arguments(
        arguments,
        tool_name=call.name,
        decision_session_id=decision_session_id,
    )
    canonical = {
        "capability": query.capability,
        "start": query.start,
        "end": query.end,
        "granularity": query.granularity,
        "fields": tuple(query.fields),
        "privacy_level": query.privacy_level.value,
        "limit": query.limit,
        "parameters": dict(query.parameters),
    }
    if normalized != canonical:
        raise HermesResponsesContractError(
            "hermes_tool_arguments_trace_mismatch"
        )


def _normalize_tool_arguments(
    arguments: Mapping[str, Any],
    *,
    tool_name: str,
    decision_session_id: str,
) -> dict[str, Any]:
    common = {
        "decision_session_id",
        "capability",
        "start",
        "end",
        "granularity",
        "fields",
        "privacy_level",
        "limit",
    }
    parameter_keys: dict[str, dict[str, str]] = {
        "mcp__healthmes__search_activity": {
            "date": "date",
            "lookback_days": "lookback_days",
        },
        "mcp__healthmes__search_nutrition": {
            "date": "date",
            "confirmed_only": "confirmed_only",
            "intent": "intent",
            "modality": "modality",
            "nutrient": "nutrient",
            "text_query": "query",
            "request_id": "request_id",
        },
        "mcp__healthmes__search_calendar": {
            "date": "date",
            "minimum_minutes": "minimum_minutes",
        },
        "mcp__healthmes__search_wearable": {
            "date": "date",
        },
    }
    mapping = parameter_keys.get(tool_name)
    if mapping is None or set(arguments) - common - set(mapping):
        raise HermesResponsesContractError(
            "hermes_tool_arguments_trace_mismatch"
        )
    if arguments.get("decision_session_id") != decision_session_id:
        raise HermesResponsesContractError(
            "hermes_decision_session_mismatch"
        )
    capability = arguments.get("capability")
    if not isinstance(capability, str) or not capability:
        raise HermesResponsesContractError(
            "hermes_tool_arguments_trace_mismatch"
        )
    fields = arguments.get("fields", ())
    if fields is None:
        fields = ()
    if not isinstance(fields, (list, tuple)) or any(
        not isinstance(item, str) for item in fields
    ):
        raise HermesResponsesContractError(
            "hermes_tool_arguments_trace_mismatch"
        )
    limit = arguments.get("limit", 100)
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise HermesResponsesContractError(
            "hermes_tool_arguments_trace_mismatch"
        )
    parameters = {
        canonical_key: arguments[input_key]
        for input_key, canonical_key in mapping.items()
        if arguments.get(input_key) is not None
    }
    return {
        "capability": capability,
        "start": _tool_argument_datetime(arguments.get("start")),
        "end": _tool_argument_datetime(arguments.get("end")),
        "granularity": arguments.get("granularity", "summary"),
        "fields": tuple(fields),
        "privacy_level": arguments.get(
            "privacy_level",
            "aggregate",
        ),
        "limit": limit,
        "parameters": parameters,
    }


def _tool_argument_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HermesResponsesContractError(
            "hermes_tool_arguments_trace_mismatch"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HermesResponsesContractError(
            "hermes_tool_arguments_trace_mismatch"
        ) from exc
    if parsed.tzinfo is None:
        raise HermesResponsesContractError(
            "hermes_tool_arguments_trace_mismatch"
        )
    return _utc(parsed)


def _abort_search_session(
    service: DecisionContextSearchSessionService,
    session_id: str,
) -> DecisionSearchSessionSnapshot | None:
    try:
        return service.abort(session_id)
    except Exception:
        return None


async def _before_deadline[T](
    operation: Awaitable[T],
    deadline: float,
) -> T:
    try:
        remaining = _remaining_before_deadline(deadline)
    except TimeoutError:
        cancel = getattr(operation, "cancel", None)
        if callable(cancel):
            cancel()
        else:
            close = getattr(operation, "close", None)
            if callable(close):
                close()
        raise
    async with asyncio.timeout(remaining):
        return await operation


def _remaining_before_deadline(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _validated_hermes_origin(
    value: str,
    *,
    api_key: str | None,
    allow_attested_private_http: bool = False,
) -> str:
    if not isinstance(value, str):
        raise TypeError("Hermes base_url must be a string")
    if _SESSION_ID_CONTROL.search(value):
        raise ValueError("Hermes base_url contains control characters")
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Hermes base_url must be an HTTP(S) origin")
    loopback = is_loopback_host(parsed.hostname)
    private_http = (
        parsed.scheme == "http"
        and allow_attested_private_http
        and _is_private_runtime_host(parsed.hostname)
    )
    if not loopback and parsed.scheme != "https" and not private_http:
        raise ValueError("remote Hermes base_url must use HTTPS")
    if not loopback and not (api_key and api_key.strip()):
        raise ValueError("remote Hermes base_url requires an api_key")
    path = parsed.path.rstrip("/")
    return urlunsplit(
        (parsed.scheme, parsed.netloc, path, "", "")
    )


def _is_private_runtime_host(host: str) -> bool:
    """Allow attested cleartext only on a private service address."""

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Docker Compose service names are single-label DNS records on the
        # stack bridge. Public DNS names still require HTTPS.
        return "." not in host and host.lower() != "localhost"
    return address.is_private or address.is_loopback or address.is_link_local


def _validated_session_id(value: str | None) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_HERMES_SESSION_ID_LENGTH
        or _SESSION_ID_CONTROL.search(value)
    ):
        raise HermesResponsesTransportError(
            "hermes_session_id_invalid"
        )
    return value


def _session_timestamp(
    value: float | int | str | None,
) -> datetime | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        return _utc(datetime.fromisoformat(cleaned.replace("Z", "+00:00")))
    except ValueError:
        try:
            return datetime.fromtimestamp(float(cleaned), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None


def _http_error_code(status_code: int) -> str:
    if status_code in {401, 403}:
        return "hermes_authentication_failed"
    if status_code == 404:
        return "hermes_responses_endpoint_missing"
    if status_code == 409:
        return "hermes_responses_conflict"
    if status_code == 413:
        return "hermes_response_too_large"
    if status_code == 429:
        return "hermes_responses_busy"
    if status_code >= 500:
        return "hermes_responses_unavailable"
    return "hermes_response_rejected"


def _identity(value: str, label: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 128:
        raise ValueError(f"Hermes {label} must be 1 to 128 characters")
    return cleaned
