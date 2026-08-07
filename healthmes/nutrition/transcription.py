"""Local-only speech transcription for nutrition voice captures."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import httpx

from healthmes.config import Settings


class TranscriptionError(RuntimeError):
    pass


class TranscriptionUnavailable(TranscriptionError):
    pass


class TranscriptionInvalidOutput(TranscriptionError):
    pass


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    text: str
    provider: str
    model: str | None = None


class NutritionTranscriber(Protocol):
    def transcribe(self, audio_path: Path) -> TranscriptionResult: ...


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


class WhisperCppTranscriber:
    """Call the local whisper.cpp HTTP server ``/inference`` endpoint."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = settings.nutrition_whisper_base_url.rstrip("/")
        self.timeout = settings.nutrition_transcription_timeout_seconds
        self.language = settings.nutrition_transcription_language
        self.transport = transport

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        if not _is_loopback_url(self.base_url):
            raise TranscriptionUnavailable(
                "nutrition voice transcription must use a loopback whisper.cpp server"
            )
        try:
            audio = audio_path.read_bytes()
        except OSError as exc:
            raise TranscriptionUnavailable(
                "nutrition voice input could not be read"
            ) from exc
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self.transport,
                trust_env=False,
            ) as client:
                response = client.post(
                    "/inference",
                    files={"file": (audio_path.name, audio)},
                    data={
                        "response_format": "json",
                        "language": self.language,
                        "temperature": "0",
                    },
                )
                response.raise_for_status()
        except (OSError, httpx.HTTPError) as exc:
            raise TranscriptionUnavailable(
                "local whisper.cpp transcription request failed"
            ) from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise TranscriptionInvalidOutput(
                "transcription response is not valid JSON"
            ) from exc
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise TranscriptionInvalidOutput(
                "transcription response does not contain text"
            )
        model = body.get("model") if isinstance(body.get("model"), str) else None
        return TranscriptionResult(
            text=text.strip(),
            provider="whisper.cpp",
            model=model,
        )


def create_nutrition_transcriber(
    settings: Settings,
    *,
    transport: httpx.BaseTransport | None = None,
) -> NutritionTranscriber:
    return WhisperCppTranscriber(settings, transport=transport)
