import json

import httpx
import pytest

from healthmes.config import Settings
from healthmes.nutrition.transcription import (
    TranscriptionInvalidOutput,
    TranscriptionUnavailable,
    WhisperCppTranscriber,
)


def test_whisper_cpp_transcribes_audio_on_loopback(tmp_path):
    audio = tmp_path / "meal.m4a"
    audio.write_bytes(b"voice-bytes")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "text": "아침에 바나나와 우유를 먹었어",
                "model": "ggml-small",
            },
        )

    transcriber = WhisperCppTranscriber(
        Settings(_env_file=None),
        transport=httpx.MockTransport(handler),
    )
    result = transcriber.transcribe(audio)

    assert result.text == "아침에 바나나와 우유를 먹었어"
    assert result.provider == "whisper.cpp"
    assert result.model == "ggml-small"
    assert requests[0].url.path == "/inference"
    assert b"voice-bytes" in requests[0].content


def test_whisper_cpp_disables_environment_proxy(tmp_path, monkeypatch):
    audio = tmp_path / "meal.wav"
    audio.write_bytes(b"voice")
    original_client = httpx.Client
    captured_kwargs = {}

    def guarded_client(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", guarded_client)
    transcriber = WhisperCppTranscriber(
        Settings(_env_file=None),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"text": "coffee"},
            )
        ),
    )

    transcriber.transcribe(audio)

    assert captured_kwargs["trust_env"] is False


def test_whisper_cpp_rejects_non_loopback_endpoint_before_read(tmp_path):
    transcriber = WhisperCppTranscriber(
        Settings(
            nutrition_whisper_base_url="https://speech.example.test",
            _env_file=None,
        ),
        transport=httpx.MockTransport(
            lambda request: pytest.fail("network must not be called")
        ),
    )
    with pytest.raises(TranscriptionUnavailable, match="loopback"):
        transcriber.transcribe(tmp_path / "missing.m4a")


def test_whisper_cpp_rejects_missing_transcript(tmp_path):
    audio = tmp_path / "meal.wav"
    audio.write_bytes(b"voice")
    transcriber = WhisperCppTranscriber(
        Settings(_env_file=None),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=json.dumps({"text": "   "}).encode(),
                headers={"content-type": "application/json"},
            )
        ),
    )
    with pytest.raises(TranscriptionInvalidOutput):
        transcriber.transcribe(audio)
