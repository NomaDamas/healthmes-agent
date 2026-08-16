"""Hermes Responses runtime for the single HealthMes decision path."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event
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
    HERMES_DECISION_TOOL_ALLOWLIST,
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

HERMES_RESPONSES_PATH = "/v1/responses"
HERMES_MODELS_PATH = "/v1/models"
HERMES_TOOLSETS_PATH = "/v1/toolsets"
HERMES_SESSION_PATH = "/api/sessions/{session_id}"
HERMES_DECISION_DRAFT_SCHEMA = "healthmes.decision-draft.v1"
HERMES_RESPONSES_POLICY_VERSION = "healthmes-responses-policy.v1"

_LOGGER = logging.getLogger(__name__)
_MAX_TOOLSETS_RESPONSE_BYTES = 256_000
_MAX_MODELS_RESPONSE_BYTES = 256_000
_MAX_SESSIONS_RESPONSE_BYTES = 1_000_000
_MAX_RESPONSES_RESPONSE_BYTES = 2_000_000
_MAX_TOOL_OUTPUT_BYTES = 1_000_000
_MAX_HERMES_SESSION_ID_LENGTH = 256
_SESSION_ID_CONTROL = re.compile(r"[\r\n\x00]")


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
    """Strict non-streaming response emitted by vendored Hermes."""

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
    """Bounded JSON body plus the request-scoped Hermes session id."""

    payload: dict[str, Any]
    session_id: str


class HermesResponsesTransport(Protocol):
    """Documented Hermes HTTP boundary; no vendor Python imports."""

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
        """Run exactly one complete Hermes autonomous agent turn."""

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
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if discovery_timeout_seconds <= 0:
            raise ValueError("discovery timeout must be positive")
        if max_response_timeout_seconds <= 0:
            raise ValueError("response timeout must be positive")
        self._base_url = _validated_hermes_origin(
            base_url,
            api_key=api_key,
        )
        self._api_key = api_key.strip() if api_key else None
        self._discovery_timeout_seconds = discovery_timeout_seconds
        self._max_response_timeout_seconds = (
            max_response_timeout_seconds
        )
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
        body, headers = await self._request_json(
            "POST",
            HERMES_RESPONSES_PATH,
            json_body=dict(payload),
            timeout_seconds=bounded_timeout,
            max_response_bytes=_MAX_RESPONSES_RESPONSE_BYTES,
        )
        session_id = _validated_session_id(
            headers.get("x-hermes-session-id")
        )
        return HermesResponsesHttpResult(
            payload=body,
            session_id=session_id,
        )

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
        self._session_ttl_seconds = session_ttl_seconds
        self._session_purge_interval_seconds = (
            session_purge_interval_seconds
        )
        self._session_purge_max_pages = session_purge_max_pages
        self._profile_assertion = profile_assertion
        self._profile_digest: str | None = None
        self._owns_search_service = owns_search_service
        self._clock = clock or (lambda: datetime.now(UTC))
        self._closed = Event()
        self._profile_lock: asyncio.Lock | None = None
        self._profile_verified = False
        self._maintenance_stop: asyncio.Event | None = None
        self._maintenance_task: asyncio.Task[None] | None = None
        self._session_purge_offset = 0
        self._metadata = RuntimeMetadata(
            runtime="hermes",
            model=self._model,
            provider=self._provider,
        )

    async def start(self) -> None:
        """Probe the authenticated native-tool profile before decisions."""

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
            try:
                if self._profile_assertion is not None:
                    self._profile_digest = (
                        self._profile_assertion.verify()
                    )
                raw_toolsets, raw_models = await asyncio.gather(
                    self._transport.get_toolsets(),
                    self._transport.get_models(),
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
                    await self._purge_expired_sessions()
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
        """Execute one request through Hermes and return finalizer input."""

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

        try:
            handle = self._search_service.begin(request)
        except Exception:
            return self._failure_run(
                request=request,
                started_at=started_at,
                code="decision_search_session_unavailable",
            )

        session_id: str | None = None
        snapshot: DecisionSearchSessionSnapshot | None = None
        cleanup_failed = False
        try:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise HermesResponsesTransportError(
                    "hermes_responses_deadline_expired"
                )
            response_request = _responses_request(
                request,
                decision_session_id=handle.session_id,
                model=self._model,
                profile_digest=self._profile_digest,
            )
            response = await _before_deadline(
                self._transport.create_response(
                    response_request,
                    timeout_seconds=remaining,
                ),
                deadline,
            )
            session_id = response.session_id
            snapshot = self._search_service.finish(handle.session_id)
            run = _run_from_response(
                request=request,
                raw_response=response.payload,
                snapshot=snapshot,
                expected_model=self._model,
                provider=self._provider,
                started_at=started_at,
                finished_at=_utc(self._clock()),
            )
        except asyncio.CancelledError:
            snapshot = _preserve_or_abort_search_session(
                snapshot,
                service=self._search_service,
                session_id=handle.session_id,
            )
            raise
        except TimeoutError:
            snapshot = _preserve_or_abort_search_session(
                snapshot,
                service=self._search_service,
                session_id=handle.session_id,
            )
            run = self._failure_run(
                request=request,
                started_at=started_at,
                code="hermes_responses_timeout",
                snapshot=snapshot,
            )
        except HermesResponsesError as exc:
            snapshot = _preserve_or_abort_search_session(
                snapshot,
                service=self._search_service,
                session_id=handle.session_id,
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
            snapshot = _preserve_or_abort_search_session(
                snapshot,
                service=self._search_service,
                session_id=handle.session_id,
            )
            run = self._failure_run(
                request=request,
                started_at=started_at,
                code="hermes_responses_execution_failed",
                snapshot=snapshot,
            )
        finally:
            if session_id is not None:
                cleanup_failed = not await self._cleanup_session(
                    session_id,
                )

        if cleanup_failed:
            limitations = list(run.draft.limitations)
            if "hermes_session_cleanup_failed" not in limitations:
                limitations.append("hermes_session_cleanup_failed")
            run = run.model_copy(
                update={
                    "draft": run.draft.model_copy(
                        update={"limitations": limitations},
                        deep=True,
                    )
                },
                deep=True,
            )
        return run

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
        if self._closed.is_set():
            task = self._maintenance_task
            if task is not None and not task.done():
                await asyncio.gather(task, return_exceptions=True)
            return
        self._closed.set()
        if self._maintenance_stop is not None:
            self._maintenance_stop.set()
        task = self._maintenance_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._owns_search_service:
            self._search_service.close()

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self._maintenance_stop is not None:
            self._maintenance_stop.set()
        task = self._maintenance_task
        if task is not None and not task.done():
            task.cancel()
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
) -> dict[str, Any]:
    request_payload = {
        "schema": "healthmes.decision-request.v1",
        "request_id": str(request.request_id),
        "turn_id": str(request.turn_id),
        "question": request.question,
        "requested_at": request.requested_at.isoformat(),
        "timezone": request.timezone,
        "requested_privacy_level": request.requested_privacy_level.value,
        "caller": {
            "channel": request.caller.channel,
            "execution_scope": request.caller.execution_scope.value,
        },
        "hints": request.hints.model_dump(mode="json", round_trip=True),
        "budget": request.budget.model_dump(mode="json", round_trip=True),
        "decision_session_id": decision_session_id,
    }
    instructions = (
        "You are the only LLM reasoning loop for one HealthMes wellness "
        "decision. Choose zero or more of exactly these tools as needed: "
        + ", ".join(sorted(HERMES_DECISION_TOOL_ALLOWLIST))
        + ". Every tool call MUST include decision_session_id exactly as "
        "provided in the request. Never call native Hermes tools, another "
        "MCP server, mutation tools, memory, filesystem, network, shell, "
        "skills_list, or skill_view. Use tool results as authoritative. "
        "Missing, partial, stale, denied, and unavailable data are not zero. "
        "After any tool calls, return exactly one JSON object and no markdown "
        "or prose. The object must have exactly two keys: "
        '{"schema":"healthmes.decision-draft.v1","decision":...}. '
        "The decision value must contain only DecisionDraft fields: status, "
        "answer, proposed_action, used_source_ref_ids, limitations, "
        "clarification_question, confidence, uncertainty, and "
        "follow_up_question. Use only source reference IDs returned by tools. "
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
        "stream": False,
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
    )
    tool_trace = _snapshot_tool_trace(snapshot)
    if len(function_pairs) != len(tool_trace):
        raise HermesResponsesContractError(
            "hermes_tool_trace_mismatch"
        )

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
    for call, output in function_pairs:
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
                if call.name not in HERMES_DECISION_TOOL_ALLOWLIST:
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
                if arguments.get("decision_session_id") != decision_session_id:
                    raise HermesResponsesContractError(
                        "hermes_decision_session_mismatch"
                    )
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
    try:
        return strict_model_validate(ContextSearchResult, payload)
    except Exception as exc:
        raise HermesResponsesContractError(
            "hermes_tool_output_invalid"
        ) from exc


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
        decoder = json.JSONDecoder()
        value, end = decoder.raw_decode(raw.lstrip())
        trailing = raw.lstrip()[end:]
    except (UnicodeEncodeError, json.JSONDecodeError) as exc:
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


def _preserve_or_abort_search_session(
    snapshot: DecisionSearchSessionSnapshot | None,
    *,
    service: DecisionContextSearchSessionService,
    session_id: str,
) -> DecisionSearchSessionSnapshot | None:
    if snapshot is not None:
        return snapshot
    return _abort_search_session(service, session_id)


async def _before_deadline[T](
    operation: Awaitable[T],
    deadline: float,
) -> T:
    remaining = deadline - monotonic()
    if remaining <= 0:
        if hasattr(operation, "close"):
            operation.close()
        raise TimeoutError
    async with asyncio.timeout(remaining):
        return await operation


def _validated_hermes_origin(
    value: str,
    *,
    api_key: str | None,
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
    if not loopback and parsed.scheme != "https":
        raise ValueError("remote Hermes base_url must use HTTPS")
    if not loopback and not (api_key and api_key.strip()):
        raise ValueError("remote Hermes base_url requires an api_key")
    path = parsed.path.rstrip("/")
    return urlunsplit(
        (parsed.scheme, parsed.netloc, path, "", "")
    )


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
