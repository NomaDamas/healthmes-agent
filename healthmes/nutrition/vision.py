"""Local-first Ollama adapter for structured intake-photo extraction."""

from __future__ import annotations

import base64
import ipaddress
import json
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

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
                raise VisionUnavailable(
                    "remote vision requires explicit allow_remote_vision=true"
                )
            if urlparse(self.base_url).scheme != "https":
                raise VisionUnavailable("remote vision endpoints must use HTTPS")
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
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
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self.transport,
            ) as client:
                response = client.post("/api/chat", json=payload)
                response.raise_for_status()
                body = response.json()
        except (OSError, httpx.HTTPError, ValueError) as exc:
            raise VisionUnavailable(f"vision provider unavailable: {exc}") from exc

        content = body.get("message", {}).get("content") if isinstance(body, dict) else None
        if not isinstance(content, str):
            raise VisionInvalidOutput("vision response is missing message.content")
        try:
            return VLMExtraction.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise VisionInvalidOutput(f"vision response failed schema validation: {exc}") from exc
