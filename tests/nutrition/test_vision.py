import base64
import json
from io import BytesIO

import httpx
import pytest
from PIL import Image
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


def _write_image(path, *, with_exif=False):
    suffix_format = {
        ".gif": "GIF",
        ".jpeg": "JPEG",
        ".jpg": "JPEG",
        ".png": "PNG",
        ".webp": "WEBP",
    }
    image = Image.new("RGB", (32, 24), color=(83, 52, 31))
    kwargs = {}
    if with_exif:
        exif = Image.Exif()
        exif[0x010F] = "Sensitive Camera"
        kwargs["exif"] = exif
    image.save(path, format=suffix_format[path.suffix.lower()], **kwargs)


def _assert_strict_schema(schema):
    if isinstance(schema, dict):
        assert "default" not in schema
        properties = schema.get("properties")
        if isinstance(properties, dict):
            assert schema["required"] == list(properties)
            assert schema["additionalProperties"] is False
        for value in schema.values():
            _assert_strict_schema(value)
    elif isinstance(schema, list):
        for value in schema:
            _assert_strict_schema(value)


def _assert_schema_omits(schema, forbidden):
    if isinstance(schema, dict):
        assert not forbidden.intersection(schema)
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                for property_schema in value.values():
                    _assert_schema_omits(property_schema, forbidden)
            else:
                _assert_schema_omits(value, forbidden)
    elif isinstance(schema, list):
        for value in schema:
            _assert_schema_omits(value, forbidden)


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
    _write_image(image, with_exif=True)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "model": "gpt-5.6-sol-2026-08-01",
                "system_fingerprint": "fp_openai",
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
                ],
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
    _assert_strict_schema(payload["text"]["format"]["schema"])
    assert provider.model == "gpt-5.6-sol-2026-08-01"
    assert provider.model_digest == "fp_openai"


def test_gemini_adapter_uses_inline_image_and_response_schema(tmp_path):
    image = tmp_path / "coffee.png"
    _write_image(image)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "modelVersion": "gemini-3.6-flash-20260801",
                "candidates": [{"content": {"parts": [{"text": json.dumps(EXTRACTION)}]}}],
            },
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
    assert inline["mimeType"] == "image/jpeg"
    assert payload["generationConfig"]["responseMimeType"] == "application/json"
    assert payload["generationConfig"]["responseJsonSchema"]["type"] == "object"
    _assert_strict_schema(payload["generationConfig"]["responseJsonSchema"])
    assert provider.model == "gemini-3.6-flash-20260801"


def test_anthropic_adapter_uses_vision_block_and_output_schema(tmp_path):
    image = tmp_path / "coffee.webp"
    _write_image(image)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "model": "claude-fable-5-20260731",
                "content": [{"type": "text", "text": json.dumps(EXTRACTION)}],
            },
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
    assert image_block["source"]["media_type"] == "image/jpeg"
    assert payload["output_config"]["format"]["type"] == "json_schema"
    schema = payload["output_config"]["format"]["schema"]
    _assert_strict_schema(schema)
    _assert_schema_omits(
        schema,
        {
            "format",
            "maxItems",
            "maxLength",
            "maximum",
            "minItems",
            "minLength",
            "minimum",
            "pattern",
        },
    )
    assert provider.model == "claude-fable-5-20260731"


def test_xai_adapter_uses_chat_schema_and_disables_storage(tmp_path):
    image = tmp_path / "coffee.jpeg"
    _write_image(image)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "model": "grok-4.5-20260801",
                "system_fingerprint": "fp_xai",
                "choices": [{"message": {"content": json.dumps(EXTRACTION)}}],
            },
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
    _assert_strict_schema(payload["response_format"]["json_schema"]["schema"])
    assert provider.model == "grok-4.5-20260801"
    assert provider.model_digest == "fp_xai"


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
    _write_image(image)
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


def test_remote_image_is_reencoded_without_exif(tmp_path):
    image = tmp_path / "coffee.jpg"
    _write_image(image, with_exif=True)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(EXTRACTION),
                            }
                        ]
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

    provider.analyze(image, allow_remote=True)

    payload = json.loads(requests[0].content)
    data_url = payload["input"][0]["content"][1]["image_url"]
    encoded = data_url.partition(",")[2]
    with Image.open(BytesIO(base64.b64decode(encoded))) as sent:
        assert not sent.getexif()
        assert max(sent.size) <= 4096


def test_remote_provider_size_limit_is_checked_before_network(tmp_path):
    image = tmp_path / "coffee.jpg"
    _write_image(image)
    provider = AnthropicVisionProvider(
        base_url="https://anthropic.test",
        api_key=SecretStr("anthropic-secret"),
        model="claude-fable-5",
        timeout=10,
        transport=httpx.MockTransport(lambda request: pytest.fail("network must not be called")),
    )
    provider.max_image_bytes = 1

    with pytest.raises(VisionUnavailable, match="size limit"):
        provider.analyze(image, allow_remote=True)


def test_gemini_binary_limit_leaves_room_for_base64_and_json():
    encoded_bytes = 4 * ((GeminiVisionProvider.max_image_bytes + 2) // 3)

    assert encoded_bytes < 20 * 1024 * 1024


def test_http_200_with_invalid_json_is_invalid_output(tmp_path):
    image = tmp_path / "coffee.jpg"
    _write_image(image)
    provider = OpenAIVisionProvider(
        base_url="https://api.openai.test",
        api_key=SecretStr("openai-secret"),
        model="gpt-5.6-sol",
        timeout=10,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=b"not-json",
                headers={"content-type": "application/json"},
            )
        ),
    )

    with pytest.raises(VisionInvalidOutput, match="valid JSON"):
        provider.analyze(image, allow_remote=True)
