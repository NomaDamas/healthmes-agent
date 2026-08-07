"""Local-first adapters for structured intake-photo extraction."""

from __future__ import annotations

import base64
import copy
import ipaddress
import json
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import SecretStr, ValidationError

from healthmes.config import Settings
from healthmes.nutrition.schema import (
    SYSTEM_PROMPT,
    USER_PROMPT,
    VLMExtraction,
)


class VisionError(RuntimeError):
    pass


class VisionUnavailable(VisionError):
    pass


class VisionInvalidOutput(VisionError):
    pass


class VisionProvider(Protocol):
    provider_name: str
    model: str
    model_digest: str | None

    def analyze(self, image_path: Path, *, allow_remote: bool) -> VLMExtraction: ...


def _is_loopback_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


_MIME_TYPES = {
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
_REMOTE_INPUT_MIME_TYPES = frozenset({"image/gif", "image/jpeg", "image/png", "image/webp"})
_SANITIZED_MIME_TYPES = frozenset({"image/jpeg", "image/png"})
_REMOTE_MAX_DIMENSION = 4096
_STRICT_EXTRACTION_SCHEMA: dict[str, Any] | None = None
_ANTHROPIC_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "format",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "pattern",
    }
)


def _image_mime_type(image_path: Path) -> str:
    mime_type = _MIME_TYPES.get(image_path.suffix.lower())
    if mime_type is None:
        raise VisionUnavailable("vision provider does not support this image type")
    return mime_type


def _image_bytes(image_path: Path) -> tuple[str, bytes]:
    mime_type = _image_mime_type(image_path)
    try:
        return mime_type, image_path.read_bytes()
    except OSError as exc:
        raise VisionUnavailable("vision input could not be read") from exc


def _sanitize_remote_image(image_path: Path) -> tuple[str, bytes]:
    try:
        with Image.open(image_path) as source:
            source.seek(0)
            image = ImageOps.exif_transpose(source)
            image.thumbnail(
                (_REMOTE_MAX_DIMENSION, _REMOTE_MAX_DIMENSION),
                Image.Resampling.LANCZOS,
            )
            output = BytesIO()
            has_alpha = image.mode in {"LA", "RGBA"} or (
                image.mode == "P" and "transparency" in image.info
            )
            if has_alpha:
                image.convert("RGBA").save(output, format="PNG", optimize=True)
                mime_type = "image/png"
            else:
                image.convert("RGB").save(
                    output,
                    format="JPEG",
                    quality=95,
                    optimize=True,
                )
                mime_type = "image/jpeg"
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise VisionUnavailable("vision input could not be sanitized") from exc
    return mime_type, output.getvalue()


def _data_url(mime_type: str, image: bytes) -> str:
    encoded = base64.b64encode(image).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _secret(value: SecretStr) -> str:
    return value.get_secret_value().strip()


def _remote_guard(
    *,
    allow_remote: bool,
    base_url: str,
    api_key: str,
    input_mime_type: str,
) -> None:
    if not allow_remote:
        raise VisionUnavailable("remote vision requires explicit allow_remote_vision=true")
    if urlparse(base_url).scheme != "https":
        raise VisionUnavailable("remote vision endpoints must use HTTPS")
    if not api_key:
        raise VisionUnavailable("selected remote vision provider is not configured")
    if input_mime_type not in _REMOTE_INPUT_MIME_TYPES:
        raise VisionUnavailable(
            f"selected remote vision provider does not support {input_mime_type}"
        )


def _strict_extraction_schema() -> dict[str, Any]:
    global _STRICT_EXTRACTION_SCHEMA
    if _STRICT_EXTRACTION_SCHEMA is not None:
        return copy.deepcopy(_STRICT_EXTRACTION_SCHEMA)

    schema = VLMExtraction.model_json_schema()

    def normalize(node: object) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["required"] = list(properties)
                node["additionalProperties"] = False
            for value in node.values():
                normalize(value)
        elif isinstance(node, list):
            for value in node:
                normalize(value)

    normalize(schema)
    _STRICT_EXTRACTION_SCHEMA = schema
    return copy.deepcopy(schema)


def _anthropic_extraction_schema() -> dict[str, Any]:
    schema = _strict_extraction_schema()

    def normalize(node: object) -> None:
        if isinstance(node, dict):
            for key in _ANTHROPIC_UNSUPPORTED_SCHEMA_KEYS:
                node.pop(key, None)
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    for property_schema in value.values():
                        normalize(property_schema)
                else:
                    normalize(value)
        elif isinstance(node, list):
            for value in node:
                normalize(value)

    normalize(schema)
    return schema


def _post_json(
    *,
    base_url: str,
    path: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None,
    timeout: float,
    transport: httpx.BaseTransport | None,
) -> dict[str, Any]:
    try:
        with httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers=headers,
        ) as client:
            response = client.post(path, json=payload)
            response.raise_for_status()
    except (OSError, httpx.HTTPError) as exc:
        raise VisionUnavailable("vision provider request failed") from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise VisionInvalidOutput("vision response is not valid JSON") from exc
    if not isinstance(body, dict):
        raise VisionInvalidOutput("vision response must be a JSON object")
    return body


def _validate_content(content: object) -> VLMExtraction:
    if not isinstance(content, str):
        raise VisionInvalidOutput("vision response is missing structured text")
    try:
        return VLMExtraction.model_validate(json.loads(content))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise VisionInvalidOutput("vision response failed schema validation") from exc


class _RemoteVisionProvider:
    provider_name: str
    max_image_bytes: int

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        model: str,
        timeout: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = _secret(api_key)
        self.model = model
        self.model_digest = None
        self.timeout = timeout
        self.transport = transport

    def _input(self, image_path: Path, *, allow_remote: bool) -> tuple[str, bytes]:
        input_mime_type = _image_mime_type(image_path)
        _remote_guard(
            allow_remote=allow_remote,
            base_url=self.base_url,
            api_key=self.api_key,
            input_mime_type=input_mime_type,
        )
        mime_type, image = _sanitize_remote_image(image_path)
        if mime_type not in _SANITIZED_MIME_TYPES:
            raise VisionUnavailable(f"selected remote vision provider does not support {mime_type}")
        if len(image) > self.max_image_bytes:
            raise VisionUnavailable("sanitized image exceeds the selected provider size limit")
        return mime_type, image

    def _response_model(
        self,
        body: dict[str, Any],
        *,
        model_key: str = "model",
        fingerprint_key: str | None = None,
    ) -> None:
        actual_model = body.get(model_key)
        if isinstance(actual_model, str) and actual_model.strip():
            self.model = actual_model.strip()
        if fingerprint_key is not None:
            fingerprint = body.get(fingerprint_key)
            if isinstance(fingerprint, str) and fingerprint.strip():
                self.model_digest = fingerprint.strip()


class OllamaVisionProvider:
    provider_name = "ollama"

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = settings.nutrition_ollama_base_url.rstrip("/")
        self.model = settings.nutrition_vision_model
        self.model_digest = settings.nutrition_vision_model_digest
        self.timeout = settings.nutrition_vision_timeout_seconds
        self.transport = transport

    def analyze(self, image_path: Path, *, allow_remote: bool) -> VLMExtraction:
        if not _is_loopback_url(self.base_url):
            if not allow_remote:
                raise VisionUnavailable("remote vision requires explicit allow_remote_vision=true")
            if urlparse(self.base_url).scheme != "https":
                raise VisionUnavailable("remote vision endpoints must use HTTPS")
        _, image = _image_bytes(image_path)
        encoded = base64.b64encode(image).decode("ascii")
        payload = {
            "model": self.model,
            "stream": False,
            "format": VLMExtraction.model_json_schema(),
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": USER_PROMPT,
                    "images": [encoded],
                },
            ],
            "options": {"temperature": 0},
        }
        body = _post_json(
            base_url=self.base_url,
            path="/api/chat",
            payload=payload,
            headers=None,
            timeout=self.timeout,
            transport=self.transport,
        )

        actual_model = body.get("model")
        if isinstance(actual_model, str) and actual_model.strip():
            self.model = actual_model.strip()
        digest = body.get("model_digest")
        if isinstance(digest, str) and digest.strip():
            self.model_digest = digest.strip()
        content = body.get("message", {}).get("content") if isinstance(body, dict) else None
        return _validate_content(content)


class OpenAIVisionProvider(_RemoteVisionProvider):
    provider_name = "openai"
    max_image_bytes = 20 * 1024 * 1024

    def analyze(self, image_path: Path, *, allow_remote: bool) -> VLMExtraction:
        mime_type, image = self._input(image_path, allow_remote=allow_remote)
        body = _post_json(
            base_url=self.base_url,
            path="/v1/responses",
            payload={
                "model": self.model,
                "store": False,
                "reasoning": {"effort": "none"},
                "instructions": SYSTEM_PROMPT,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": USER_PROMPT},
                            {
                                "type": "input_image",
                                "image_url": _data_url(mime_type, image),
                                "detail": "original",
                            },
                        ],
                    }
                ],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "nutrition_observation",
                        "strict": True,
                        "schema": _strict_extraction_schema(),
                    }
                },
            },
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
            transport=self.transport,
        )
        self._response_model(
            body,
            fingerprint_key="system_fingerprint",
        )
        output = body.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "output_text":
                        return _validate_content(block.get("text"))
        raise VisionInvalidOutput("vision response is missing structured text")


class GeminiVisionProvider(_RemoteVisionProvider):
    provider_name = "gemini"
    # Gemini inline requests cap the full JSON body at 20 MB. Fourteen MiB of
    # binary expands to about 18.7 MiB in base64, leaving room for the schema.
    max_image_bytes = 14 * 1024 * 1024

    def analyze(self, image_path: Path, *, allow_remote: bool) -> VLMExtraction:
        mime_type, image = self._input(image_path, allow_remote=allow_remote)
        body = _post_json(
            base_url=self.base_url,
            path=f"/models/{self.model}:generateContent",
            payload={
                "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"text": USER_PROMPT},
                            {
                                "inlineData": {
                                    "mimeType": mime_type,
                                    "data": base64.b64encode(image).decode("ascii"),
                                }
                            },
                        ],
                    }
                ],
                "generationConfig": {
                    "temperature": 0,
                    "responseMimeType": "application/json",
                    "responseJsonSchema": _strict_extraction_schema(),
                },
            },
            headers={"x-goog-api-key": self.api_key},
            timeout=self.timeout,
            transport=self.transport,
        )
        self._response_model(body, model_key="modelVersion")
        candidates = body.get("candidates")
        if isinstance(candidates, list) and candidates:
            content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
            parts = content.get("parts") if isinstance(content, dict) else None
            if isinstance(parts, list) and parts:
                part = parts[0]
                return _validate_content(part.get("text") if isinstance(part, dict) else None)
        raise VisionInvalidOutput("vision response is missing structured text")


class AnthropicVisionProvider(_RemoteVisionProvider):
    provider_name = "anthropic"
    max_image_bytes = 5 * 1024 * 1024

    def analyze(self, image_path: Path, *, allow_remote: bool) -> VLMExtraction:
        mime_type, image = self._input(image_path, allow_remote=allow_remote)
        body = _post_json(
            base_url=self.base_url,
            path="/v1/messages",
            payload={
                "model": self.model,
                "max_tokens": 4096,
                "system": SYSTEM_PROMPT,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": mime_type,
                                    "data": base64.b64encode(image).decode("ascii"),
                                },
                            },
                            {"type": "text", "text": USER_PROMPT},
                        ],
                    }
                ],
                "output_config": {
                    "format": {
                        "type": "json_schema",
                        "schema": _anthropic_extraction_schema(),
                    }
                },
            },
            headers={
                "anthropic-version": "2023-06-01",
                "x-api-key": self.api_key,
            },
            timeout=self.timeout,
            transport=self.transport,
        )
        self._response_model(body)
        content = body.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return _validate_content(block.get("text"))
        raise VisionInvalidOutput("vision response is missing structured text")


class XAIVisionProvider(_RemoteVisionProvider):
    provider_name = "xai"
    max_image_bytes = 20 * 1024 * 1024

    def analyze(self, image_path: Path, *, allow_remote: bool) -> VLMExtraction:
        mime_type, image = self._input(image_path, allow_remote=allow_remote)
        body = _post_json(
            base_url=self.base_url,
            path="/v1/chat/completions",
            payload={
                "model": self.model,
                "store": False,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": USER_PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": _data_url(mime_type, image),
                                    "detail": "high",
                                },
                            },
                        ],
                    },
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "nutrition_observation",
                        "strict": True,
                        "schema": _strict_extraction_schema(),
                    },
                },
            },
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
            transport=self.transport,
        )
        self._response_model(
            body,
            fingerprint_key="system_fingerprint",
        )
        choices = body.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            return _validate_content(message.get("content") if isinstance(message, dict) else None)
        raise VisionInvalidOutput("vision response is missing structured text")


def create_vision_provider(
    settings: Settings,
    *,
    transport: httpx.BaseTransport | None = None,
) -> VisionProvider:
    common = {
        "timeout": settings.nutrition_vision_timeout_seconds,
        "transport": transport,
    }
    if settings.nutrition_vision_provider == "ollama":
        return OllamaVisionProvider(settings, transport=transport)
    if settings.nutrition_vision_provider == "openai":
        return OpenAIVisionProvider(
            base_url=settings.nutrition_openai_base_url,
            api_key=settings.nutrition_openai_api_key,
            model=settings.nutrition_openai_model,
            **common,
        )
    if settings.nutrition_vision_provider == "gemini":
        return GeminiVisionProvider(
            base_url=settings.nutrition_gemini_base_url,
            api_key=settings.nutrition_gemini_api_key,
            model=settings.nutrition_gemini_model,
            **common,
        )
    if settings.nutrition_vision_provider == "anthropic":
        return AnthropicVisionProvider(
            base_url=settings.nutrition_anthropic_base_url,
            api_key=settings.nutrition_anthropic_api_key,
            model=settings.nutrition_anthropic_model,
            **common,
        )
    return XAIVisionProvider(
        base_url=settings.nutrition_xai_base_url,
        api_key=settings.nutrition_xai_api_key,
        model=settings.nutrition_xai_model,
        **common,
    )
