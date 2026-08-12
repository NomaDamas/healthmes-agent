"""Replaceable Hermes transport for one HealthMes-owned model iteration."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import ipaddress
import json
import re
import uuid
from collections.abc import Callable, Mapping
from threading import Lock
from time import monotonic
from types import FunctionType, MethodType
from typing import Any, Literal, Protocol, runtime_checkable
from urllib.parse import unquote, urlsplit

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

from healthmes.decision.contracts import (
    DecisionDraft,
    PrivacyLevel,
    RuntimeMetadata,
)
from healthmes.decision.providers import (
    ContextParameterFormat,
    ContextParameterSpec,
    ContextParameterType,
)
from healthmes.decision.runtime import (
    ContextToolCall,
    DecisionRuntimeContractError,
    DecisionRuntimeTurn,
    DecisionRuntimeUnavailableError,
    DecisionToolSpec,
    RuntimeDecisionRequest,
    RuntimeResourceBudget,
    RuntimeStepOutput,
    RuntimeToolExchange,
)
from healthmes.decision.validation import (
    NormalizedJson,
    normalize_untrusted_json,
    strict_json_model_validate,
    strict_model_validate,
)

HERMES_MODEL_ITERATION_CONTRACT = "hermes.model-iteration.v1"
HERMES_MODEL_ITERATION_FEATURE = "model_iteration"
HERMES_MODEL_ITERATION_ENDPOINT = "model_iteration"
HERMES_CAPABILITIES_PATH = "/v1/capabilities"
HERMES_MODEL_ITERATION_PATH = "/v1/model/iterations"
HERMES_MODEL_ITERATION_OBJECT = "hermes.model_iteration.request"
HERMES_MODEL_ITERATION_RESPONSE_OBJECT = "hermes.model_iteration.response"

_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CALL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_REQUEST_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_MAX_HTTP_RESPONSE_BYTES = 2_000_000
_REQUIRED_SUPPORTS = (
    "system_policy",
    "tool_allowlist",
    "conversation_snapshot",
    "structured_output",
    "usage",
    "external_deadline",
)


def _raise_if_task_cancellation_requested() -> None:
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


def _is_native_async_method(value: Any, *, owner: Any) -> bool:
    return (
        isinstance(value, MethodType)
        and value.__self__ is owner
        and isinstance(value.__func__, FunctionType)
        and bool(value.__func__.__code__.co_flags & inspect.CO_COROUTINE)
    )


def _clean_reason_code(value: str) -> str:
    cleaned = value.strip().casefold()
    if _REASON_CODE.fullmatch(cleaned) is None:
        raise ValueError("Hermes reason code is invalid")
    return cleaned


class HermesRuntimeCapability(BaseModel):
    """Result of probing Hermes for the required split-runtime hook."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    available: bool
    endpoint: str | None = Field(default=None, max_length=255)
    model: str | None = Field(default=None, max_length=128)
    reason_codes: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        cleaned = tuple(_clean_reason_code(item) for item in value)
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("Hermes reason codes must be unique")
        return cleaned

    @model_validator(mode="after")
    def validate_status(self) -> HermesRuntimeCapability:
        if self.available:
            if self.endpoint is None or self.reason_codes:
                raise ValueError(
                    "available Hermes capability requires only an endpoint"
                )
        elif not self.reason_codes:
            raise ValueError(
                "unavailable Hermes capability requires a reason code"
            )
        return self


class HermesModelToolDefinition(BaseModel):
    """One caller-executed virtual function exposed for a model iteration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["function"] = "function"
    name: str = Field(pattern=_TOOL_NAME.pattern)
    description: str = Field(min_length=1, max_length=3_000)
    input_schema: dict[str, JsonValue]
    metadata: dict[str, JsonValue]


class HermesTurnSnapshot(BaseModel):
    """Minimum HealthMes state passed to the stateless model call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request: RuntimeDecisionRequest
    history: tuple[RuntimeToolExchange, ...] = Field(
        default=(),
        max_length=32,
    )


class _HermesModelIterationPayload(BaseModel):
    """Canonical one-provider-call payload before correlation hashing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    object: Literal[HERMES_MODEL_ITERATION_OBJECT] = (
        HERMES_MODEL_ITERATION_OBJECT
    )
    contract_version: Literal[HERMES_MODEL_ITERATION_CONTRACT] = (
        HERMES_MODEL_ITERATION_CONTRACT
    )
    request_id: uuid.UUID
    turn_id: uuid.UUID
    step_number: int = Field(ge=1, le=32)
    remaining_steps: int = Field(ge=1, le=32)
    deadline_ms: int = Field(ge=1)
    model: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=128)
    system_policy: str = Field(min_length=1, max_length=16_000)
    system_policy_version: str = Field(min_length=1, max_length=128)
    privacy_scope: PrivacyLevel
    resource_budget: RuntimeResourceBudget
    turn_snapshot: HermesTurnSnapshot
    tools: tuple[HermesModelToolDefinition, ...] = Field(
        default=(),
        max_length=1_024,
    )
    allowed_tools: tuple[str, ...] = Field(
        default=(),
        max_length=1_024,
    )
    structured_output_schema: dict[str, JsonValue]

    @field_validator("model", "provider")
    @classmethod
    def validate_runtime_selection(
        cls,
        value: str,
        info,
    ) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{info.field_name} must not be blank")
        return cleaned

    @model_validator(mode="after")
    def validate_tool_allowlist(self) -> _HermesModelIterationPayload:
        names = tuple(tool.name for tool in self.tools)
        if len(names) != len(set(names)):
            raise ValueError("Hermes tool names must be unique")
        if names != self.allowed_tools:
            raise ValueError(
                "Hermes allowed_tools must exactly match supplied tools"
            )
        return self


class HermesModelIterationRequest(_HermesModelIterationPayload):
    """Generic one-provider-call request proposed for upstream Hermes."""

    request_fingerprint: str = Field(
        pattern=_REQUEST_FINGERPRINT.pattern
    )

    @model_validator(mode="after")
    def validate_request_fingerprint(self) -> HermesModelIterationRequest:
        if self.request_fingerprint != _canonical_request_fingerprint(self):
            raise ValueError(
                "Hermes request fingerprint does not match its payload"
            )
        return self


class HermesModelToolCall(BaseModel):
    """Unexecuted function call returned by exactly one model iteration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str = Field(pattern=_CALL_ID.pattern)
    name: str = Field(pattern=_TOOL_NAME.pattern)
    arguments: dict[str, JsonValue] = Field(
        default_factory=dict,
        max_length=64,
    )


class HermesModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class HermesModelIterationResponse(BaseModel):
    """Untrusted response envelope returned by the upstream Hermes hook."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    object: Literal[HERMES_MODEL_ITERATION_RESPONSE_OBJECT] = (
        HERMES_MODEL_ITERATION_RESPONSE_OBJECT
    )
    contract_version: Literal[HERMES_MODEL_ITERATION_CONTRACT] = (
        HERMES_MODEL_ITERATION_CONTRACT
    )
    request_id: uuid.UUID
    turn_id: uuid.UUID
    request_fingerprint: str = Field(
        pattern=_REQUEST_FINGERPRINT.pattern
    )
    step_number: int = Field(ge=1, le=32)
    finish_reason: Literal["tool_calls", "structured_output"]
    tool_calls: tuple[HermesModelToolCall, ...] = Field(
        default=(),
        max_length=32,
    )
    output: dict[str, JsonValue] | None = None
    usage: HermesModelUsage
    model: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=128)

    @field_validator("model", "provider")
    @classmethod
    def validate_runtime_identity(
        cls,
        value: str,
        info,
    ) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{info.field_name} must not be blank")
        return cleaned

    @model_validator(mode="after")
    def validate_single_action(self) -> HermesModelIterationResponse:
        has_calls = bool(self.tool_calls)
        has_output = self.output is not None
        if has_calls == has_output:
            raise ValueError(
                "Hermes iteration must return tool calls or structured output"
            )
        expected_reason = (
            "tool_calls" if has_calls else "structured_output"
        )
        if self.finish_reason != expected_reason:
            raise ValueError(
                "Hermes finish_reason does not match the returned action"
            )
        call_ids = tuple(call.call_id for call in self.tool_calls)
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("Hermes tool call IDs must be unique")
        return self


class HermesTransportError(RuntimeError):
    """Sanitized failure raised by a Hermes transport implementation."""

    def __init__(self, code: str) -> None:
        self.code = _clean_reason_code(code)
        super().__init__(self.code)


@runtime_checkable
class HermesIterationTransport(Protocol):
    """Documented transport boundary; no Hermes Python imports are allowed."""

    async def get_capabilities(self) -> dict[str, Any]: ...

    async def run_model_iteration(
        self,
        *,
        endpoint: str,
        request: HermesModelIterationRequest,
        timeout_seconds: float,
    ) -> dict[str, Any]: ...


class HermesHttpIterationTransport:
    """HTTP transport that discovers, but never guesses, a Hermes hook."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        discovery_timeout_seconds: float = 5,
        max_iteration_timeout_seconds: float = 120,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = _validate_base_url(base_url)
        if discovery_timeout_seconds <= 0:
            raise ValueError("discovery_timeout_seconds must be positive")
        if max_iteration_timeout_seconds <= 0:
            raise ValueError(
                "max_iteration_timeout_seconds must be positive"
            )
        self._api_key = api_key.strip() if api_key else None
        if self._api_key == "":
            self._api_key = None
        parsed_base_url = urlsplit(self._base_url)
        assert parsed_base_url.hostname is not None
        if (
            not _is_loopback_host(parsed_base_url.hostname)
            and self._api_key is None
        ):
            raise ValueError("remote Hermes base_url requires an api_key")
        self._discovery_timeout_seconds = discovery_timeout_seconds
        self._max_iteration_timeout_seconds = (
            max_iteration_timeout_seconds
        )
        self._http_transport = http_transport

    async def get_capabilities(self) -> dict[str, Any]:
        return await self._request_json(
            "GET",
            HERMES_CAPABILITIES_PATH,
            timeout_seconds=self._discovery_timeout_seconds,
        )

    async def run_model_iteration(
        self,
        *,
        endpoint: str,
        request: HermesModelIterationRequest,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        path = _validate_endpoint_path(endpoint)
        effective_timeout = min(
            timeout_seconds,
            self._max_iteration_timeout_seconds,
        )
        if effective_timeout <= 0:
            raise HermesTransportError("hermes_iteration_deadline_expired")
        return await self._request_json(
            "POST",
            path,
            payload=request.model_dump(mode="json"),
            timeout_seconds=effective_timeout,
            idempotency_key=f"hm-{request.request_fingerprint}",
        )

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        timeout_seconds: float,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                headers=headers,
                timeout=timeout_seconds,
                follow_redirects=False,
                transport=self._http_transport,
                trust_env=False,
            ) as client:
                request_kwargs = (
                    {"json": payload} if payload is not None else {}
                )
                async with client.stream(
                    method,
                    path,
                    **request_kwargs,
                ) as response:
                    if response.status_code in {401, 403}:
                        raise HermesTransportError(
                            "hermes_transport_unauthorized"
                        )
                    if (
                        response.status_code < 200
                        or response.status_code >= 300
                    ):
                        raise HermesTransportError(
                            "hermes_transport_rejected"
                        )
                    content_encoding = response.headers.get(
                        "Content-Encoding",
                        "",
                    ).strip().casefold()
                    if content_encoding not in {"", "identity"}:
                        raise HermesTransportError(
                            "hermes_response_compression_unsupported"
                        )
                    content = bytearray()
                    if response.is_stream_consumed:
                        if len(response.content) > _MAX_HTTP_RESPONSE_BYTES:
                            raise HermesTransportError(
                                "hermes_response_too_large"
                            )
                        content.extend(response.content)
                    else:
                        async for chunk in response.aiter_raw(
                            chunk_size=64 * 1024
                        ):
                            if (
                                len(content) + len(chunk)
                                > _MAX_HTTP_RESPONSE_BYTES
                            ):
                                raise HermesTransportError(
                                    "hermes_response_too_large"
                                )
                            content.extend(chunk)
        except asyncio.CancelledError:
            raise
        except HermesTransportError:
            raise
        except httpx.HTTPError:
            transport_error = "hermes_transport_unreachable"
        else:
            transport_error = None

        if transport_error is not None:
            raise HermesTransportError(transport_error)

        try:
            decoded = json.loads(
                content,
                object_pairs_hook=_unique_json_object,
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ):
            raise HermesTransportError("hermes_response_invalid") from None
        if not isinstance(decoded, dict):
            raise HermesTransportError("hermes_response_invalid")
        return decoded


class HermesRuntimeAdapter:
    """DecisionRuntime backed only by Hermes' advertised one-call hook."""

    def __init__(
        self,
        *,
        transport: HermesIterationTransport,
        model: str,
        provider: str,
    ) -> None:
        try:
            get_capabilities = transport.get_capabilities
            run_model_iteration = transport.run_model_iteration
        except AttributeError:
            raise TypeError(
                "transport must implement HermesIterationTransport"
            ) from None
        if not all(
            (
                _is_native_async_method(
                    get_capabilities,
                    owner=transport,
                ),
                _is_native_async_method(
                    run_model_iteration,
                    owner=transport,
                ),
            )
        ):
            raise TypeError(
                "Hermes transport methods must be async functions"
            )
        if not isinstance(model, str) or not model.strip():
            raise ValueError("Hermes model must be 1 to 128 characters")
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("Hermes provider must be 1 to 128 characters")
        if len(model.strip()) > 128:
            raise ValueError("Hermes model must be 1 to 128 characters")
        if len(provider.strip()) > 128:
            raise ValueError("Hermes provider must be 1 to 128 characters")
        self._transport = transport
        self._get_capabilities = get_capabilities
        self._run_model_iteration = run_model_iteration
        self._metadata = RuntimeMetadata(
            runtime="hermes",
            model=model.strip(),
            provider=provider.strip(),
        )
        self._capability_lock = Lock()
        self._capability_cache: HermesRuntimeCapability | None = None
        self._capability_generation = 0
        self._transport_task_lock = Lock()
        self._detached_transport_tasks: set[asyncio.Future[Any]] = set()
        self._quarantined_transport_refs: list[Any] = []
        self._transport_permanently_quarantined = False
        self._transport_operation_active = False

    @property
    def metadata(self) -> RuntimeMetadata:
        return self._metadata.model_copy(deep=True)

    async def capability_status(
        self,
        *,
        refresh: bool = False,
        deadline_at: float | None = None,
    ) -> HermesRuntimeCapability:
        """Return explicit availability without falling back to full agent chat."""

        _raise_if_task_cancellation_requested()
        with self._transport_task_lock:
            transport_quarantined = (
                self._transport_permanently_quarantined
                or bool(self._detached_transport_tasks)
            )
        if transport_quarantined:
            return HermesRuntimeCapability(
                available=False,
                reason_codes=("hermes_transport_quarantined",),
            )
        with self._capability_lock:
            if refresh:
                self._invalidate_capability_cache_locked()
            if not refresh and self._capability_cache is not None:
                return self._capability_cache.model_copy(deep=True)
            probe_generation = self._capability_generation
        try:
            raw = await self._run_transport_operation(
                self._get_capabilities,
                deadline_at=deadline_at,
            )
            _raise_if_task_cancellation_requested()
        except asyncio.CancelledError:
            if refresh:
                with self._capability_lock:
                    if (
                        probe_generation
                        == self._capability_generation
                    ):
                        self._invalidate_capability_cache_locked()
            raise
        except HermesTransportError as exc:
            status = HermesRuntimeCapability(
                available=False,
                reason_codes=(exc.code,),
            )
        except TimeoutError:
            raise
        except Exception:
            status = HermesRuntimeCapability(
                available=False,
                reason_codes=("hermes_capabilities_unreachable",),
            )
        else:
            status = _parse_capabilities(raw.value)
        with self._capability_lock:
            if status.reason_codes == (
                "hermes_transport_contract_invalid",
            ):
                return status
            if probe_generation != self._capability_generation:
                return HermesRuntimeCapability(
                    available=False,
                    reason_codes=(
                        "hermes_capability_probe_superseded",
                    ),
                )
            if refresh:
                self._invalidate_capability_cache_locked()
            if status.available:
                self._capability_cache = status.model_copy(deep=True)
        return status

    def _invalidate_capability_cache(self) -> None:
        with self._capability_lock:
            self._invalidate_capability_cache_locked()

    def _invalidate_capability_cache_locked(self) -> None:
        self._capability_generation += 1
        self._capability_cache = None

    async def _run_transport_operation(
        self,
        operation_factory: Callable[[], Any],
        *,
        deadline_at: float | None = None,
    ) -> NormalizedJson:
        self._reserve_transport_operation()
        try:
            operation = operation_factory()
            if not inspect.iscoroutine(operation):
                self._permanently_quarantine_transport(operation)
                raise HermesTransportError(
                    "hermes_transport_contract_invalid"
                )
            task = asyncio.create_task(operation)
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    self._inspect_completed_transport_task(task)
                else:
                    task.cancel()
                    self._detach_transport_task(task)
                raise
            if inspect.isawaitable(result):
                self._permanently_quarantine_transport(result)
                raise HermesTransportError(
                    "hermes_transport_contract_invalid"
                )
            try:
                normalized = normalize_untrusted_json(
                    result,
                    max_bytes=_MAX_HTTP_RESPONSE_BYTES,
                    deadline=deadline_at,
                )
            except (TypeError, ValueError, OverflowError, RecursionError):
                raise HermesTransportError(
                    "hermes_transport_contract_invalid"
                ) from None
            if type(normalized.value) is not dict:
                raise HermesTransportError(
                    "hermes_transport_contract_invalid"
                )
            return normalized
        finally:
            self._release_transport_operation()

    def _reserve_transport_operation(self) -> None:
        with self._transport_task_lock:
            if self._transport_permanently_quarantined:
                raise HermesTransportError(
                    "hermes_transport_quarantined"
                )
            if self._detached_transport_tasks:
                raise HermesTransportError(
                    "hermes_transport_quarantined"
                )
            if self._transport_operation_active:
                raise HermesTransportError("hermes_transport_busy")
            self._transport_operation_active = True

    def _release_transport_operation(self) -> None:
        with self._transport_task_lock:
            self._transport_operation_active = False

    def _detach_transport_task(
        self,
        task: asyncio.Future[Any],
    ) -> None:
        with self._transport_task_lock:
            self._transport_operation_active = False
            self._detached_transport_tasks.add(task)
        self._invalidate_capability_cache()

        def consume_result(completed: asyncio.Future[Any]) -> None:
            try:
                self._inspect_completed_transport_task(completed)
            finally:
                with self._transport_task_lock:
                    self._detached_transport_tasks.discard(completed)

        task.add_done_callback(consume_result)

    def _inspect_completed_transport_task(
        self,
        task: asyncio.Future[Any],
    ) -> None:
        try:
            result = task.result()
        except BaseException:
            return
        if inspect.isawaitable(result):
            self._permanently_quarantine_transport(result)

    def _permanently_quarantine_transport(
        self,
        value: Any,
    ) -> None:
        with self._transport_task_lock:
            self._transport_permanently_quarantined = True
            self._quarantined_transport_refs.append(value)
        self._invalidate_capability_cache()
        if isinstance(value, asyncio.Future):
            try:
                loop = value.get_loop()
                if not loop.is_closed():
                    loop.call_soon_threadsafe(
                        self._cancel_and_consume_future,
                        value,
                    )
            except BaseException:
                pass
            self._cancel_and_consume_future(value)
        elif inspect.iscoroutine(value):
            try:
                value.close()
            except BaseException:
                pass

    @staticmethod
    def _cancel_and_consume_future(
        future: asyncio.Future[Any],
    ) -> None:
        try:
            future.add_done_callback(
                HermesRuntimeAdapter._consume_future_result
            )
        except BaseException:
            pass
        try:
            future.cancel()
        except BaseException:
            pass
        HermesRuntimeAdapter._consume_future_result(future)

    @staticmethod
    def _consume_future_result(
        future: asyncio.Future[Any],
    ) -> None:
        try:
            future.exception()
        except BaseException:
            pass

    async def next_step(
        self,
        turn: DecisionRuntimeTurn,
    ) -> RuntimeStepOutput:
        started_at = monotonic()
        validated_turn: DecisionRuntimeTurn | None = None
        try:
            validated_turn = strict_model_validate(
                DecisionRuntimeTurn,
                turn,
            )
        except (TypeError, ValidationError, ValueError):
            pass
        if validated_turn is None:
            raise DecisionRuntimeContractError(
                "hermes_turn_contract_invalid"
            )

        deadline_at = started_at + validated_turn.deadline_ms / 1_000
        remaining_seconds = deadline_at - monotonic()
        if remaining_seconds <= 0:
            raise DecisionRuntimeUnavailableError(
                "hermes_iteration_deadline_expired"
            )
        try:
            async with asyncio.timeout(remaining_seconds):
                capability = await self.capability_status(
                    deadline_at=deadline_at,
                )
        except TimeoutError:
            self._invalidate_capability_cache()
            raise DecisionRuntimeUnavailableError(
                "hermes_iteration_deadline_expired"
            ) from None
        if monotonic() >= deadline_at:
            self._invalidate_capability_cache()
            raise DecisionRuntimeUnavailableError(
                "hermes_iteration_deadline_expired"
            )
        _raise_if_task_cancellation_requested()
        if not capability.available or capability.endpoint is None:
            code = (
                capability.reason_codes[0]
                if capability.reason_codes
                else "hermes_single_iteration_unavailable"
            )
            raise DecisionRuntimeUnavailableError(code)

        remaining_deadline_ms = int(
            (deadline_at - monotonic()) * 1_000
        )
        if remaining_deadline_ms <= 0:
            raise DecisionRuntimeUnavailableError(
                "hermes_iteration_deadline_expired"
            )
        model = self._metadata.model
        provider = self._metadata.provider
        if model is None or provider is None:
            raise DecisionRuntimeContractError(
                "hermes_runtime_selection_missing"
            )
        request, tool_names = _iteration_request(
            validated_turn,
            model=model,
            provider=provider,
            deadline_ms=remaining_deadline_ms,
        )
        runtime_error_code: str | None = None
        try:
            remaining_seconds = min(
                request.deadline_ms / 1_000,
                deadline_at - monotonic(),
            )
            if remaining_seconds <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining_seconds):
                raw_response = await self._run_transport_operation(
                    lambda: self._run_model_iteration(
                        endpoint=capability.endpoint,
                        request=request,
                        timeout_seconds=(
                            request.deadline_ms / 1_000
                        ),
                    ),
                    deadline_at=deadline_at,
                )
                _raise_if_task_cancellation_requested()
            if monotonic() >= deadline_at:
                raise TimeoutError
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            runtime_error_code = "hermes_iteration_deadline_expired"
        except HermesTransportError as exc:
            runtime_error_code = exc.code
        except Exception:
            runtime_error_code = "hermes_iteration_unavailable"

        if runtime_error_code is not None:
            raise DecisionRuntimeUnavailableError(runtime_error_code)

        try:
            if monotonic() >= deadline_at:
                raise DecisionRuntimeUnavailableError(
                    "hermes_iteration_deadline_expired"
                )
            response = strict_json_model_validate(
                HermesModelIterationResponse,
                raw_response,
            )
            output = self._convert_response(
                validated_turn,
                response=response,
                tool_names=tool_names,
                request_fingerprint=request.request_fingerprint,
            )
            if monotonic() >= deadline_at:
                raise DecisionRuntimeUnavailableError(
                    "hermes_iteration_deadline_expired"
                )
            return output
        except DecisionRuntimeUnavailableError:
            raise
        except DecisionRuntimeContractError:
            raise
        except (TypeError, ValidationError, ValueError):
            pass
        raise DecisionRuntimeContractError(
            "hermes_response_contract_invalid"
        )

    def _convert_response(
        self,
        turn: DecisionRuntimeTurn,
        *,
        response: HermesModelIterationResponse,
        tool_names: Mapping[str, str],
        request_fingerprint: str,
    ) -> RuntimeStepOutput:
        if (
            response.request_id != turn.request_id
            or response.turn_id != turn.turn_id
            or response.step_number != turn.step_number
        ):
            raise DecisionRuntimeContractError(
                "hermes_response_correlation_mismatch"
            )
        if response.request_fingerprint != request_fingerprint:
            raise DecisionRuntimeContractError(
                "hermes_response_fingerprint_mismatch"
            )
        if response.model != self._metadata.model:
            raise DecisionRuntimeContractError(
                "hermes_response_model_mismatch"
            )
        if response.provider != self._metadata.provider:
            raise DecisionRuntimeContractError(
                "hermes_response_provider_mismatch"
            )

        metadata = RuntimeMetadata(
            runtime="hermes",
            model=response.model,
            provider=response.provider,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        if response.output is not None:
            draft = strict_json_model_validate(
                DecisionDraft,
                response.output,
            )
            return RuntimeStepOutput(
                draft=draft,
                metadata=metadata,
            )

        canonical_calls: list[dict[str, Any]] = []
        for call in response.tool_calls:
            capability = tool_names.get(call.name)
            if capability is None:
                raise DecisionRuntimeContractError(
                    "hermes_tool_not_allowlisted"
                )
            if "capability" in call.arguments:
                raise DecisionRuntimeContractError(
                    "hermes_tool_capability_forged"
                )
            canonical = strict_json_model_validate(
                ContextToolCall,
                {
                    "capability": capability,
                    **call.arguments,
                },
            )
            canonical_calls.append(
                canonical.model_dump(mode="python")
            )
        return RuntimeStepOutput(
            tool_calls=tuple(canonical_calls),
            metadata=metadata,
        )


def _parse_capabilities(raw: dict[str, Any]) -> HermesRuntimeCapability:
    if type(raw) is not dict:
        return HermesRuntimeCapability(
            available=False,
            reason_codes=("hermes_capabilities_invalid",),
        )
    features = raw.get("features")
    if not isinstance(features, Mapping):
        return HermesRuntimeCapability(
            available=False,
            reason_codes=("hermes_capabilities_invalid",),
        )
    model = raw.get("model")
    safe_model = (
        model.strip()
        if (
            isinstance(model, str)
            and model.strip()
            and len(model.strip()) <= 128
        )
        else None
    )
    if features.get(HERMES_MODEL_ITERATION_FEATURE) is not True:
        return HermesRuntimeCapability(
            available=False,
            model=safe_model,
            reason_codes=(
                "hermes_single_iteration_not_advertised",
            ),
        )

    runtime = raw.get("runtime")
    runtime_valid = (
        isinstance(runtime, Mapping)
        and runtime.get("mode") == "split_runtime"
        and runtime.get("tool_execution") == "caller"
        and runtime.get("split_runtime") is True
    )
    if not runtime_valid:
        return HermesRuntimeCapability(
            available=False,
            model=safe_model,
            reason_codes=(
                "hermes_single_iteration_contract_unsupported",
            ),
        )

    endpoints = raw.get("endpoints")
    endpoint = (
        endpoints.get(HERMES_MODEL_ITERATION_ENDPOINT)
        if isinstance(endpoints, Mapping)
        else None
    )
    if not isinstance(endpoint, Mapping):
        return HermesRuntimeCapability(
            available=False,
            model=safe_model,
            reason_codes=("hermes_iteration_endpoint_invalid",),
        )
    supports = endpoint.get("supports")
    supports_required = (
        isinstance(supports, Mapping)
        and all(supports.get(name) is True for name in _REQUIRED_SUPPORTS)
    )
    contract_valid = (
        endpoint.get("method") == "POST"
        and endpoint.get("path") == HERMES_MODEL_ITERATION_PATH
        and endpoint.get("contract") == HERMES_MODEL_ITERATION_CONTRACT
        and type(endpoint.get("max_model_calls")) is int
        and endpoint.get("max_model_calls") == 1
        and endpoint.get("tool_execution") == "caller"
        and endpoint.get("session_mutation") is False
        and supports_required
    )
    if not contract_valid:
        return HermesRuntimeCapability(
            available=False,
            model=safe_model,
            reason_codes=(
                "hermes_single_iteration_contract_unsupported",
            ),
        )
    try:
        path = _validate_endpoint_path(endpoint.get("path"))
    except (TypeError, ValueError):
        return HermesRuntimeCapability(
            available=False,
            model=safe_model,
            reason_codes=("hermes_iteration_endpoint_invalid",),
        )
    return HermesRuntimeCapability(
        available=True,
        endpoint=path,
        model=safe_model,
    )


def _canonical_request_fingerprint(
    request: _HermesModelIterationPayload,
) -> str:
    payload = request.model_dump(
        mode="json",
        round_trip=True,
        exclude={"request_fingerprint"},
    )
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _iteration_request(
    turn: DecisionRuntimeTurn,
    *,
    model: str,
    provider: str,
    deadline_ms: int,
) -> tuple[HermesModelIterationRequest, dict[str, str]]:
    definitions: list[HermesModelToolDefinition] = []
    tool_names: dict[str, str] = {}
    for spec in turn.tools:
        name = _hermes_tool_name(spec.capability)
        if name in tool_names:
            raise DecisionRuntimeContractError(
                "hermes_tool_name_collision"
            )
        tool_names[name] = spec.capability
        definitions.append(_tool_definition(spec, name=name))

    tools = tuple(definitions)
    payload = _HermesModelIterationPayload(
        request_id=turn.request_id,
        turn_id=turn.turn_id,
        step_number=turn.step_number,
        remaining_steps=turn.remaining_steps,
        deadline_ms=deadline_ms,
        model=model,
        provider=provider,
        system_policy=turn.system_policy,
        system_policy_version=turn.system_policy_version,
        privacy_scope=turn.request.requested_privacy_level,
        resource_budget=turn.resource_budget.model_copy(deep=True),
        turn_snapshot=HermesTurnSnapshot(
            request=turn.request.model_copy(deep=True),
            history=tuple(
                item.model_copy(deep=True) for item in turn.history
            ),
        ),
        tools=tools,
        allowed_tools=tuple(tool.name for tool in tools),
        structured_output_schema=DecisionDraft.model_json_schema(),
    )
    request = HermesModelIterationRequest(
        **payload.model_dump(mode="python", round_trip=True),
        request_fingerprint=_canonical_request_fingerprint(payload),
    )
    return request, tool_names


def _hermes_tool_name(capability: str) -> str:
    digest = hashlib.sha256(capability.encode("utf-8")).hexdigest()[:32]
    return f"hmctx_{digest}"


def _tool_definition(
    spec: DecisionToolSpec,
    *,
    name: str,
) -> HermesModelToolDefinition:
    description = (
        f"[{spec.capability}] {spec.description} "
        f"Freshness: {spec.freshness_expectation}"
    )
    metadata: dict[str, JsonValue] = {
        "capability": spec.capability,
        "provider_id": spec.provider_id,
        "domain": spec.domain,
        "output_fields": list(spec.output_fields),
        "max_lookback_days": spec.max_lookback_days,
        "max_rows": spec.max_rows,
        "supports_raw": spec.supports_raw,
        "allows_future": spec.allows_future,
        "provenance": spec.provenance.value,
    }
    return HermesModelToolDefinition(
        name=name,
        description=description,
        input_schema=_tool_input_schema(spec),
        metadata=metadata,
    )


def _tool_input_schema(spec: DecisionToolSpec) -> dict[str, JsonValue]:
    properties: dict[str, JsonValue] = {
        "granularity": {
            "type": "string",
            "enum": list(spec.granularities),
        },
        "privacy_level": {
            "type": "string",
            "enum": [item.value for item in spec.privacy_levels],
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": spec.max_rows,
        },
        "purpose": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500,
        },
    }
    query_fields = set(spec.query_fields)
    if "start" in query_fields:
        properties["start"] = {
            "type": "string",
            "format": "date-time",
        }
    if "end" in query_fields:
        properties["end"] = {
            "type": "string",
            "format": "date-time",
        }
    if "fields" in query_fields:
        field_items: dict[str, JsonValue] = {"type": "string"}
        if spec.output_fields:
            field_items["enum"] = list(spec.output_fields)
        properties["fields"] = {
            "type": "array",
            "items": field_items,
            "uniqueItems": True,
            "maxItems": 64,
        }
    if spec.parameter_specs:
        parameter_properties = {
            parameter.name: _parameter_schema(parameter)
            for parameter in spec.parameter_specs
        }
        required = [
            parameter.name
            for parameter in spec.parameter_specs
            if parameter.required
        ]
        parameter_schema: dict[str, JsonValue] = {
            "type": "object",
            "properties": parameter_properties,
            "additionalProperties": False,
        }
        if required:
            parameter_schema["required"] = required
        properties["parameters"] = parameter_schema
    schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if any(parameter.required for parameter in spec.parameter_specs):
        schema["required"] = ["parameters"]
    return schema


def _parameter_schema(
    spec: ContextParameterSpec,
) -> dict[str, JsonValue]:
    if spec.value_type is ContextParameterType.BOOLEAN:
        return {"type": "boolean"}
    if spec.value_type is ContextParameterType.INTEGER:
        return {
            "type": "integer",
            "minimum": spec.minimum,
            "maximum": spec.maximum,
        }

    schema: dict[str, JsonValue] = {
        "type": "string",
        "maxLength": spec.max_length,
    }
    if spec.min_length is not None:
        schema["minLength"] = spec.min_length
    if spec.allowed_values:
        schema["enum"] = list(spec.allowed_values)
    if spec.format is ContextParameterFormat.DATE:
        schema["format"] = "date"
    elif spec.format is ContextParameterFormat.UUID:
        schema["format"] = "uuid"
    elif spec.format is ContextParameterFormat.RELATED_RECORD_REF:
        schema["pattern"] = r"^rr_[0-9a-f]{16}$"
    return schema


def _validate_base_url(value: str) -> str:
    cleaned = value.strip()
    if any(
        ord(character) < 32 or ord(character) == 127
        for character in cleaned
    ):
        raise ValueError("Hermes base_url contains control characters")
    parsed = urlsplit(cleaned)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Hermes base_url must be an HTTP(S) origin")
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise ValueError("remote Hermes base_url must use HTTPS")
    return cleaned.rstrip("/")


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_endpoint_path(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("Hermes endpoint path must be a string")
    path = value.strip()
    if (
        "\\" in path
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in path
        )
    ):
        raise ValueError("Hermes endpoint path is invalid")
    parsed = urlsplit(path)
    decoded_path = unquote(parsed.path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or ".." in decoded_path.split("/")
        or len(path) > 255
    ):
        raise ValueError("Hermes endpoint path is invalid")
    return path


def _unique_json_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value
