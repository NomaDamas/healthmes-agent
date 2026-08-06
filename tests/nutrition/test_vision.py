import json

import httpx
import pytest
from pydantic import SecretStr

from healthmes.config import Settings
from healthmes.nutrition.contracts import EstimateKind
from healthmes.nutrition.schema import VLMEstimate
from healthmes.nutrition.vision import (
    AnthropicVisionProvider,
    GeminiVisionProvider,
    OllamaVisionProvider,
    OpenAIVisionProvider,
    VisionInvalidOutput,
    VisionUnavailable,
    XAIVisionProvider,
    create_vision_provider,
)

EXTRACTION = {
    "status": "insufficient_data",
    "confidence": "low",
    "warnings": ["label unreadable"],
    "items": [],
}


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
            json={"message": {"content": json.dumps(EXTRACTION)}},
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
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
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
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    with pytest.raises(VisionUnavailable, match="HTTPS"):
        provider.analyze(image, allow_remote=True)


def test_invalid_provider_json_is_controlled(tmp_path):
    image = tmp_path / "coffee.jpg"
    image.write_bytes(b"synthetic")
    provider = OllamaVisionProvider(
        Settings(_env_file=None),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"message": {"content": "not json"}})
        ),
    )
    with pytest.raises(VisionInvalidOutput):
        provider.analyze(image, allow_remote=False)


def test_openai_adapter_uses_responses_schema_and_disables_storage(tmp_path):
    image = tmp_path / "coffee.jpg"
    image.write_bytes(b"synthetic")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(EXTRACTION),
                            }
                        ],
                    }
                ]
            },
        )

    provider = OpenAIVisionProvider(
        base_url="https://api.openai.test",
        api_key=SecretStr("openai-secret"),
        model="gpt-5.6-sol",
        timeout=10,
        transport=httpx.MockTransport(handler),
    )

    result = provider.analyze(image, allow_remote=True)

    assert result.status.value == "insufficient_data"
    request = requests[0]
    payload = json.loads(request.content)
    assert request.url.path == "/v1/responses"
    assert request.headers["authorization"] == "Bearer openai-secret"
    assert payload["store"] is False
    assert payload["reasoning"] == {"effort": "none"}
    assert payload["input"][0]["content"][1]["detail"] == "original"
    assert payload["input"][0]["content"][1]["image_url"].startswith("data:image/jpeg;base64,")
    assert payload["text"]["format"]["strict"] is True


def test_gemini_adapter_uses_inline_image_and_response_schema(tmp_path):
    image = tmp_path / "coffee.png"
    image.write_bytes(b"synthetic")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": json.dumps(EXTRACTION)}]}}]},
        )

    provider = GeminiVisionProvider(
        base_url="https://gemini.test/v1beta",
        api_key=SecretStr("gemini-secret"),
        model="gemini-3.6-flash",
        timeout=10,
        transport=httpx.MockTransport(handler),
    )

    result = provider.analyze(image, allow_remote=True)

    assert result.status.value == "insufficient_data"
    request = requests[0]
    payload = json.loads(request.content)
    assert request.url.path == "/v1beta/models/gemini-3.6-flash:generateContent"
    assert request.headers["x-goog-api-key"] == "gemini-secret"
    inline = payload["contents"][0]["parts"][1]["inlineData"]
    assert inline["mimeType"] == "image/png"
    assert payload["generationConfig"]["responseMimeType"] == "application/json"
    assert payload["generationConfig"]["responseJsonSchema"]["type"] == "object"


def test_anthropic_adapter_uses_vision_block_and_output_schema(tmp_path):
    image = tmp_path / "coffee.webp"
    image.write_bytes(b"synthetic")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": json.dumps(EXTRACTION)}]},
        )

    provider = AnthropicVisionProvider(
        base_url="https://anthropic.test",
        api_key=SecretStr("anthropic-secret"),
        model="claude-fable-5",
        timeout=10,
        transport=httpx.MockTransport(handler),
    )

    result = provider.analyze(image, allow_remote=True)

    assert result.status.value == "insufficient_data"
    request = requests[0]
    payload = json.loads(request.content)
    assert request.url.path == "/v1/messages"
    assert request.headers["x-api-key"] == "anthropic-secret"
    assert request.headers["anthropic-version"] == "2023-06-01"
    image_block = payload["messages"][0]["content"][0]
    assert image_block["source"]["media_type"] == "image/webp"
    assert payload["output_config"]["format"]["type"] == "json_schema"


def test_xai_adapter_uses_chat_schema_and_disables_storage(tmp_path):
    image = tmp_path / "coffee.jpeg"
    image.write_bytes(b"synthetic")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(EXTRACTION)}}]},
        )

    provider = XAIVisionProvider(
        base_url="https://xai.test",
        api_key=SecretStr("xai-secret"),
        model="grok-4.5",
        timeout=10,
        transport=httpx.MockTransport(handler),
    )

    result = provider.analyze(image, allow_remote=True)

    assert result.status.value == "insufficient_data"
    request = requests[0]
    payload = json.loads(request.content)
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer xai-secret"
    assert payload["store"] is False
    image_url = payload["messages"][1]["content"][1]["image_url"]
    assert image_url["detail"] == "high"
    assert image_url["url"].startswith("data:image/jpeg;base64,")
    assert payload["response_format"]["json_schema"]["strict"] is True


@pytest.mark.parametrize(
    ("provider_name", "expected_type", "expected_model"),
    [
        ("ollama", OllamaVisionProvider, "qwen3-vl:4b-instruct"),
        ("openai", OpenAIVisionProvider, "gpt-5.6-sol"),
        ("gemini", GeminiVisionProvider, "gemini-3.6-flash"),
        ("anthropic", AnthropicVisionProvider, "claude-fable-5"),
        ("xai", XAIVisionProvider, "grok-4.5"),
    ],
)
def test_provider_factory_uses_configured_provider(provider_name, expected_type, expected_model):
    provider = create_vision_provider(
        Settings(nutrition_vision_provider=provider_name, _env_file=None)
    )

    assert isinstance(provider, expected_type)
    assert provider.model == expected_model


def test_remote_adapter_requires_request_opt_in_before_network(tmp_path):
    image = tmp_path / "coffee.jpg"
    image.write_bytes(b"synthetic")
    provider = OpenAIVisionProvider(
        base_url="https://api.openai.test",
        api_key=SecretStr("openai-secret"),
        model="gpt-5.6-sol",
        timeout=10,
        transport=httpx.MockTransport(lambda request: pytest.fail("network must not be called")),
    )

    with pytest.raises(VisionUnavailable, match="explicit"):
        provider.analyze(image, allow_remote=False)


def test_remote_adapter_requires_api_key_before_network(tmp_path):
    image = tmp_path / "coffee.jpg"
    image.write_bytes(b"synthetic")
    provider = OpenAIVisionProvider(
        base_url="https://api.openai.test",
        api_key=SecretStr(""),
        model="gpt-5.6-sol",
        timeout=10,
        transport=httpx.MockTransport(lambda request: pytest.fail("network must not be called")),
    )

    with pytest.raises(VisionUnavailable, match="not configured"):
        provider.analyze(image, allow_remote=True)


def test_remote_adapter_requires_https_before_network(tmp_path):
    image = tmp_path / "coffee.jpg"
    image.write_bytes(b"synthetic")
    provider = OpenAIVisionProvider(
        base_url="http://api.openai.test",
        api_key=SecretStr("openai-secret"),
        model="gpt-5.6-sol",
        timeout=10,
        transport=httpx.MockTransport(lambda request: pytest.fail("network must not be called")),
    )

    with pytest.raises(VisionUnavailable, match="HTTPS"):
        provider.analyze(image, allow_remote=True)


def test_remote_adapter_rejects_heic_without_sending_image(tmp_path):
    image = tmp_path / "coffee.heic"
    image.write_bytes(b"synthetic")
    provider = OpenAIVisionProvider(
        base_url="https://api.openai.test",
        api_key=SecretStr("openai-secret"),
        model="gpt-5.6-sol",
        timeout=10,
        transport=httpx.MockTransport(lambda request: pytest.fail("network must not be called")),
    )

    with pytest.raises(VisionUnavailable, match="image/heic"):
        provider.analyze(image, allow_remote=True)


def test_provider_failure_does_not_expose_secret_or_image(tmp_path):
    image = tmp_path / "coffee.jpg"
    image.write_bytes(b"sensitive-image-bytes")
    provider = OpenAIVisionProvider(
        base_url="https://api.openai.test",
        api_key=SecretStr("openai-secret"),
        model="gpt-5.6-sol",
        timeout=10,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                500,
                text="upstream failed",
            )
        ),
    )

    with pytest.raises(VisionUnavailable) as captured:
        provider.analyze(image, allow_remote=True)

    message = str(captured.value)
    assert "openai-secret" not in message
    assert "sensitive-image-bytes" not in message
