import json

import httpx
import pytest

from healthmes.config import Settings
from healthmes.nutrition.contracts import EstimateKind
from healthmes.nutrition.schema import VLMEstimate
from healthmes.nutrition.vision import (
    OllamaVisionProvider,
    VisionInvalidOutput,
    VisionUnavailable,
)


def test_exact_estimate_requires_visible_label_evidence():
    with pytest.raises(ValueError, match="visible-label"):
        VLMEstimate(
            kind=EstimateKind.EXACT,
            unit="mg",
            exact=180,
            estimation_basis="visual_guess",
        )


def test_ollama_adapter_sends_schema_and_validates_output(tmp_path):
    image = tmp_path / "coffee.jpg"
    image.write_bytes(b"synthetic")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": json.dumps(
                        {
                            "status": "insufficient_data",
                            "confidence": "low",
                            "warnings": ["label unreadable"],
                            "items": [],
                        }
                    )
                }
            },
        )

    settings = Settings(
        nutrition_ollama_base_url="http://127.0.0.1:11434",
        nutrition_vision_model="qwen3-vl:4b-instruct",
        _env_file=None,
    )
    provider = OllamaVisionProvider(
        settings,
        transport=httpx.MockTransport(handler),
    )

    result = provider.analyze(image, allow_remote=False)

    assert result.status.value == "insufficient_data"
    payload = json.loads(requests[0].content)
    assert payload["model"] == "qwen3-vl:4b-instruct"
    assert payload["stream"] is False
    assert payload["format"]["type"] == "object"
    assert payload["messages"][1]["images"]


def test_non_loopback_vision_requires_explicit_permission(tmp_path):
    image = tmp_path / "coffee.jpg"
    image.write_bytes(b"synthetic")
    settings = Settings(
        nutrition_ollama_base_url="https://vision.example.test",
        _env_file=None,
    )
    provider = OllamaVisionProvider(
        settings,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500)
        ),
    )
    with pytest.raises(VisionUnavailable, match="explicit"):
        provider.analyze(image, allow_remote=False)


def test_non_loopback_vision_requires_https_even_with_permission(tmp_path):
    image = tmp_path / "coffee.jpg"
    image.write_bytes(b"synthetic")
    settings = Settings(
        nutrition_ollama_base_url="http://vision.example.test",
        _env_file=None,
    )
    provider = OllamaVisionProvider(
        settings,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500)
        ),
    )
    with pytest.raises(VisionUnavailable, match="HTTPS"):
        provider.analyze(image, allow_remote=True)


def test_invalid_provider_json_is_controlled(tmp_path):
    image = tmp_path / "coffee.jpg"
    image.write_bytes(b"synthetic")
    provider = OllamaVisionProvider(
        Settings(_env_file=None),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"message": {"content": "not json"}}
            )
        ),
    )
    with pytest.raises(VisionInvalidOutput):
        provider.analyze(image, allow_remote=False)
