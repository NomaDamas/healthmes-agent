import base64
import json
from io import BytesIO

import httpx
import pytest
from PIL import Image
from pillow_heif import register_heif_opener
from pydantic import SecretStr

from healthmes.config import Settings
from healthmes.nutrition.contracts import Confidence, EstimateKind, IntakeType
from healthmes.nutrition.schema import (
    VLMEstimate,
    VLMItem,
    VLMNutrient,
)
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

register_heif_opener()


def _write_image(path, *, with_exif=False):
    suffix_format = {
        ".gif": "GIF",
        ".heic": "HEIF",
        ".heif": "HEIF",
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


def test_vlm_estimate_rejects_non_finite_numbers():
    with pytest.raises(ValueError):
        VLMEstimate(
            kind=EstimateKind.EXACT,
            unit="kcal",
            exact=float("inf"),
            evidence_text="Energy infinity kcal",
            estimation_basis="visible_label",
        )


def test_vlm_item_inserts_missing_core_nutrients():
    item = VLMItem(
        intake_type=IntakeType.FOOD,
        name_candidates=["sandwich"],
        serving=VLMEstimate(
            kind=EstimateKind.RANGE,
            unit="g",
            minimum=150,
            maximum=250,
            estimation_basis="visible_portion",
        ),
        caffeine=VLMEstimate(kind=EstimateKind.UNKNOWN, unit="mg"),
        nutrients=[],
        confidence=Confidence.MEDIUM,
    )
    nutrients = {value.nutrient: value for value in item.nutrients}
    assert set(nutrients) == {
        "energy",
        "protein",
        "carbohydrate",
        "fat",
        "fiber",
        "sugar",
        "sodium",
        "caffeine",
    }
    assert nutrients["energy"].amount.kind is EstimateKind.UNKNOWN


def test_vlm_nutrient_rejects_wrong_core_unit_and_duplicate_names():
    with pytest.raises(ValueError, match="energy estimates must use kcal"):
        VLMNutrient(
            nutrient="energy",
            amount=VLMEstimate(kind=EstimateKind.UNKNOWN, unit="kJ"),
            confidence=Confidence.LOW,
        )
    duplicate = VLMNutrient(
        nutrient="protein",
        amount=VLMEstimate(kind=EstimateKind.UNKNOWN, unit="g"),
        confidence=Confidence.LOW,
    )
    with pytest.raises(ValueError, match="duplicate nutrient"):
        VLMItem(
            intake_type=IntakeType.FOOD,
            name_candidates=["sandwich"],
            serving=VLMEstimate(
                kind=EstimateKind.UNKNOWN,
                unit="g",
            ),
            caffeine=VLMEstimate(kind=EstimateKind.UNKNOWN, unit="mg"),
            nutrients=[duplicate, duplicate],
            confidence=Confidence.LOW,
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


def test_ollama_adapter_converts_heic_before_analysis(tmp_path):
    image = tmp_path / "coffee.heic"
    _write_image(image)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"message": {"content": json.dumps(EXTRACTION)}},
        )

    provider = OllamaVisionProvider(
        Settings(_env_file=None),
        transport=httpx.MockTransport(handler),
    )
    provider.analyze(image, allow_remote=False)

    payload = json.loads(requests[0].content)
    converted = base64.b64decode(payload["messages"][1]["images"][0])
    assert converted.startswith(b"\xff\xd8")


def test_remote_ollama_sanitizes_image_metadata(tmp_path):
    image = tmp_path / "coffee.jpg"
    _write_image(image, with_exif=True)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"message": {"content": json.dumps(EXTRACTION)}},
        )

    provider = OllamaVisionProvider(
        Settings(
            nutrition_ollama_base_url="https://ollama.example.test",
            _env_file=None,
        ),
        transport=httpx.MockTransport(handler),
    )
    provider.analyze(image, allow_remote=True)

    payload = json.loads(requests[0].content)
    sanitized = Image.open(
        BytesIO(base64.b64decode(payload["messages"][1]["images"][0]))
    )
    assert sanitized.getexif().get(0x010F) is None


def test_ollama_adapter_analyzes_free_text_without_images():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"message": {"content": json.dumps(EXTRACTION)}},
        )

    provider = OllamaVisionProvider(
        Settings(_env_file=None),
        transport=httpx.MockTransport(handler),
    )
    result = provider.analyze_text(
        "아메리카노 한 잔을 마셨어",
        allow_remote=False,
    )

    assert result.status.value == "insufficient_data"
    payload = json.loads(requests[0].content)
    assert payload["messages"][1] == {
        "role": "user",
        "content": "아메리카노 한 잔을 마셨어",
    }
    assert "images" not in payload["messages"][1]


def test_local_ollama_disables_environment_proxy(monkeypatch):
    original_client = httpx.Client
    captured_kwargs = {}

    def guarded_client(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", guarded_client)
    provider = OllamaVisionProvider(
        Settings(_env_file=None),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"message": {"content": json.dumps(EXTRACTION)}},
            )
        ),
    )

    provider.analyze_text("라테 한 잔", allow_remote=False)

    assert captured_kwargs["trust_env"] is False


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


def test_openai_adapter_analyzes_text_with_explicit_opt_in():
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
        api_key=SecretStr("secret"),
        model="model",
        timeout=10,
        transport=httpx.MockTransport(handler),
    )
    provider.analyze_text("라테 한 잔", allow_remote=True)
    payload = json.loads(requests[0].content)
    assert payload["input"] == "라테 한 잔"
    assert payload["store"] is False


@pytest.mark.parametrize("method", ["photo", "text"])
def test_ollama_rejects_non_object_message(tmp_path, method):
    provider = OllamaVisionProvider(
        Settings(_env_file=None),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"message": []},
            )
        ),
    )
    image = tmp_path / "meal.jpg"
    _write_image(image)

    with pytest.raises(
        VisionInvalidOutput,
        match="message must be an object",
    ):
        if method == "photo":
            provider.analyze(image, allow_remote=False)
        else:
            provider.analyze_text("coffee", allow_remote=False)


def test_text_adapter_accepts_exact_value_only_when_owner_stated_it():
    extraction = {
        **EXTRACTION,
        "status": "usable",
        "confidence": "high",
        "warnings": [],
        "items": [
            {
                "intake_type": "beverage",
                "name_candidates": [],
                "category": "coffee",
                "serving": {
                    "kind": "exact",
                    "unit": "ml",
                    "exact": 355,
                    "estimation_basis": "owner_statement",
                    "evidence_text": "커피 355ml",
                },
                "caffeine": {"kind": "unknown", "unit": "mg"},
                "nutrients": [],
                "confidence": "high",
                "warnings": [],
            }
        ],
    }

    def provider_for(payload):
        return OpenAIVisionProvider(
            base_url="https://api.openai.test",
            api_key=SecretStr("secret"),
            model="model",
            timeout=10,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "output": [
                            {
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": json.dumps(payload),
                                    }
                                ]
                            }
                        ]
                    },
                )
            ),
        )

    accepted = provider_for(extraction).analyze_text(
        "커피 355ml 마셨어",
        allow_remote=True,
    )
    assert accepted.items[0].serving.exact == 355

    with pytest.raises(VisionInvalidOutput, match="owner statement"):
        provider_for(extraction).analyze_text(
            "커피 한 잔 마셨어",
            allow_remote=True,
        )


def test_text_adapter_requires_exact_value_in_its_own_evidence():
    extraction = {
        **EXTRACTION,
        "status": "usable",
        "confidence": "high",
        "warnings": [],
        "items": [
            {
                "intake_type": "beverage",
                "name_candidates": ["coffee"],
                "category": "coffee",
                "serving": {
                    "kind": "exact",
                    "unit": "ml",
                    "exact": 95,
                    "estimation_basis": "owner_statement",
                    "evidence_text": "카페인 95mg",
                },
                "caffeine": {
                    "kind": "exact",
                    "unit": "mg",
                    "exact": 355,
                    "estimation_basis": "owner_statement",
                    "evidence_text": "커피 355ml",
                },
                "nutrients": [],
                "confidence": "high",
                "warnings": [],
            }
        ],
    }
    provider = OpenAIVisionProvider(
        base_url="https://api.openai.test",
        api_key=SecretStr("secret"),
        model="model",
        timeout=10,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps(extraction),
                                }
                            ]
                        }
                    ]
                },
            )
        ),
    )

    with pytest.raises(
        VisionInvalidOutput,
        match="matching numeric owner statement",
    ):
        provider.analyze_text(
            "커피 355ml, 카페인 95mg",
            allow_remote=True,
        )


def test_text_adapter_binds_same_unit_values_to_nutrient_evidence():
    extraction = {
        **EXTRACTION,
        "status": "usable",
        "confidence": "high",
        "warnings": [],
        "items": [
            {
                "intake_type": "beverage",
                "name_candidates": ["coffee"],
                "category": "coffee",
                "serving": {
                    "kind": "range",
                    "unit": "ml",
                    "minimum": 300,
                    "maximum": 400,
                    "estimation_basis": "owner_portion_description",
                },
                "caffeine": {
                    "kind": "exact",
                    "unit": "mg",
                    "exact": 355,
                    "estimation_basis": "owner_statement",
                    "evidence_text": "95mg은 카페인, 나트륨은 355mg",
                },
                "nutrients": [
                    {
                        "nutrient": "sodium",
                        "amount": {
                            "kind": "exact",
                            "unit": "mg",
                            "exact": 95,
                            "estimation_basis": "owner_statement",
                            "evidence_text": "95mg은 카페인, 나트륨은 355mg",
                        },
                        "confidence": "high",
                    }
                ],
                "confidence": "high",
                "warnings": [],
            }
        ],
    }
    provider = OpenAIVisionProvider(
        base_url="https://api.openai.test",
        api_key=SecretStr("secret"),
        model="model",
        timeout=10,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps(extraction),
                                }
                            ]
                        }
                    ]
                },
            )
        ),
    )

    with pytest.raises(
        VisionInvalidOutput,
        match="matching numeric owner statement",
    ):
        provider.analyze_text(
            "95mg은 카페인, 나트륨은 355mg",
            allow_remote=True,
        )


def test_text_adapter_binds_exact_values_to_the_correct_item():
    owner_text = "coffee caffeine 95mg, green tea caffeine 30mg"
    extraction = {
        **EXTRACTION,
        "status": "usable",
        "confidence": "high",
        "warnings": [],
        "items": [
            {
                "intake_type": "beverage",
                "name_candidates": ["coffee"],
                "category": "coffee",
                "serving": {
                    "kind": "unknown",
                    "unit": "ml",
                },
                "caffeine": {
                    "kind": "exact",
                    "unit": "mg",
                    "exact": 30,
                    "estimation_basis": "owner_statement",
                    "evidence_text": owner_text,
                },
                "nutrients": [],
                "confidence": "high",
                "warnings": [],
            },
            {
                "intake_type": "beverage",
                "name_candidates": [],
                "category": "tea",
                "serving": {
                    "kind": "unknown",
                    "unit": "ml",
                },
                "caffeine": {
                    "kind": "exact",
                    "unit": "mg",
                    "exact": 95,
                    "estimation_basis": "owner_statement",
                    "evidence_text": owner_text,
                },
                "nutrients": [],
                "confidence": "high",
                "warnings": [],
            },
        ],
    }
    provider = OpenAIVisionProvider(
        base_url="https://api.openai.test",
        api_key=SecretStr("secret"),
        model="model",
        timeout=10,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps(extraction),
                                }
                            ]
                        }
                    ]
                },
            )
        ),
    )

    with pytest.raises(
        VisionInvalidOutput,
        match="matching numeric owner statement",
    ):
        provider.analyze_text(owner_text, allow_remote=True)


def test_text_adapter_binds_exact_serving_to_the_correct_item():
    owner_text = "coffee 355ml, green tea 250ml"
    extraction = {
        **EXTRACTION,
        "status": "usable",
        "confidence": "high",
        "warnings": [],
        "items": [
            {
                "intake_type": "beverage",
                "name_candidates": ["coffee"],
                "category": "coffee",
                "serving": {
                    "kind": "exact",
                    "unit": "ml",
                    "exact": 250,
                    "estimation_basis": "owner_statement",
                    "evidence_text": "green tea 250ml",
                },
                "caffeine": {"kind": "unknown", "unit": "mg"},
                "nutrients": [],
                "confidence": "high",
                "warnings": [],
            },
            {
                "intake_type": "beverage",
                "name_candidates": ["green tea"],
                "category": "tea",
                "serving": {
                    "kind": "exact",
                    "unit": "ml",
                    "exact": 355,
                    "estimation_basis": "owner_statement",
                    "evidence_text": "coffee 355ml",
                },
                "caffeine": {"kind": "unknown", "unit": "mg"},
                "nutrients": [],
                "confidence": "high",
                "warnings": [],
            },
        ],
    }
    provider = OpenAIVisionProvider(
        base_url="https://api.openai.test",
        api_key=SecretStr("secret"),
        model="model",
        timeout=10,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps(extraction),
                                }
                            ]
                        }
                    ]
                },
            )
        ),
    )

    with pytest.raises(
        VisionInvalidOutput,
        match="matching numeric owner statement",
    ):
        provider.analyze_text(owner_text, allow_remote=True)


def test_text_adapter_rejects_serving_owned_by_longer_overlapping_name():
    owner_text = "coffee 355ml, coffee latte 250ml"
    extraction = {
        **EXTRACTION,
        "status": "usable",
        "confidence": "high",
        "warnings": [],
        "items": [
            {
                "intake_type": "beverage",
                "name_candidates": ["coffee"],
                "category": "coffee",
                "serving": {
                    "kind": "exact",
                    "unit": "ml",
                    "exact": 250,
                    "estimation_basis": "owner_statement",
                    "evidence_text": "coffee latte 250ml",
                },
                "caffeine": {"kind": "unknown", "unit": "mg"},
                "nutrients": [],
                "confidence": "high",
                "warnings": [],
            },
            {
                "intake_type": "beverage",
                "name_candidates": ["coffee latte"],
                "category": "coffee",
                "serving": {
                    "kind": "exact",
                    "unit": "ml",
                    "exact": 250,
                    "estimation_basis": "owner_statement",
                    "evidence_text": "coffee latte 250ml",
                },
                "caffeine": {"kind": "unknown", "unit": "mg"},
                "nutrients": [],
                "confidence": "high",
                "warnings": [],
            },
        ],
    }
    provider = OpenAIVisionProvider(
        base_url="https://api.openai.test",
        api_key=SecretStr("secret"),
        model="model",
        timeout=10,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps(extraction),
                                }
                            ]
                        }
                    ]
                },
            )
        ),
    )

    with pytest.raises(
        VisionInvalidOutput,
        match="matching numeric owner statement",
    ):
        provider.analyze_text(owner_text, allow_remote=True)


def test_photo_adapter_accepts_exact_values_bound_to_multiple_items(
    tmp_path,
):
    image = tmp_path / "meal.jpg"
    _write_image(image)
    extraction = {
        **EXTRACTION,
        "status": "usable",
        "confidence": "high",
        "warnings": [],
        "items": [
            {
                "intake_type": "beverage",
                "name_candidates": ["coffee"],
                "category": "coffee",
                "serving": {
                    "kind": "exact",
                    "unit": "ml",
                    "exact": 355,
                    "estimation_basis": "visible_label",
                    "evidence_text": "coffee 355ml",
                },
                "caffeine": {
                    "kind": "exact",
                    "unit": "mg",
                    "exact": 95,
                    "estimation_basis": "visible_label",
                    "evidence_text": "coffee caffeine 95mg",
                },
                "nutrients": [],
                "confidence": "high",
                "warnings": [],
            },
            {
                "intake_type": "beverage",
                "name_candidates": ["green tea"],
                "category": "tea",
                "serving": {
                    "kind": "exact",
                    "unit": "ml",
                    "exact": 250,
                    "estimation_basis": "visible_label",
                    "evidence_text": "green tea 250ml",
                },
                "caffeine": {
                    "kind": "exact",
                    "unit": "mg",
                    "exact": 30,
                    "estimation_basis": "visible_label",
                    "evidence_text": "green tea caffeine 30mg",
                },
                "nutrients": [],
                "confidence": "high",
                "warnings": [],
            },
        ],
    }
    provider = OpenAIVisionProvider(
        base_url="https://api.openai.test",
        api_key=SecretStr("secret"),
        model="model",
        timeout=10,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps(extraction),
                                }
                            ]
                        }
                    ]
                },
            )
        ),
    )

    result = provider.analyze(image, allow_remote=True)

    assert [item.serving.exact for item in result.items] == [355, 250]


def test_photo_adapter_rejects_broad_evidence_nutrient_swap(tmp_path):
    image = tmp_path / "label.jpg"
    _write_image(image)
    evidence = "95mg is caffeine, sodium is 355mg"
    extraction = {
        **EXTRACTION,
        "status": "usable",
        "confidence": "high",
        "warnings": [],
        "items": [
            {
                "intake_type": "beverage",
                "name_candidates": ["coffee"],
                "category": "coffee",
                "serving": {
                    "kind": "range",
                    "unit": "ml",
                    "minimum": 300,
                    "maximum": 400,
                    "estimation_basis": "visible_portion",
                },
                "caffeine": {
                    "kind": "exact",
                    "unit": "mg",
                    "exact": 355,
                    "estimation_basis": "visible_label",
                    "evidence_text": evidence,
                },
                "nutrients": [
                    {
                        "nutrient": "sodium",
                        "amount": {
                            "kind": "exact",
                            "unit": "mg",
                            "exact": 95,
                            "estimation_basis": "visible_label",
                            "evidence_text": evidence,
                        },
                        "confidence": "high",
                    }
                ],
                "confidence": "high",
                "warnings": [],
            }
        ],
    }
    provider = OllamaVisionProvider(
        Settings(_env_file=None),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"message": {"content": json.dumps(extraction)}},
            )
        ),
    )

    with pytest.raises(
        VisionInvalidOutput,
        match="matching visible-label evidence",
    ):
        provider.analyze(image, allow_remote=False)


def test_photo_adapter_rejects_exact_value_mismatching_label(tmp_path):
    image = tmp_path / "coffee.jpg"
    _write_image(image)
    extraction = {
        **EXTRACTION,
        "status": "usable",
        "confidence": "high",
        "warnings": [],
        "items": [
            {
                "intake_type": "beverage",
                "name_candidates": ["coffee"],
                "category": "coffee",
                "serving": {
                    "kind": "range",
                    "unit": "ml",
                    "minimum": 300,
                    "maximum": 400,
                    "estimation_basis": "visible_portion",
                },
                "caffeine": {
                    "kind": "exact",
                    "unit": "mg",
                    "exact": 355,
                    "estimation_basis": "visible_label",
                    "evidence_text": "Caffeine 95mg",
                },
                "nutrients": [],
                "confidence": "high",
                "warnings": [],
            }
        ],
    }
    provider = OllamaVisionProvider(
        Settings(_env_file=None),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"message": {"content": json.dumps(extraction)}},
            )
        ),
    )

    with pytest.raises(
        VisionInvalidOutput,
        match="matching visible-label evidence",
    ):
        provider.analyze(image, allow_remote=False)


def test_photo_adapter_rejects_owner_statement_as_exact_evidence(tmp_path):
    image = tmp_path / "coffee.jpg"
    _write_image(image)
    extraction = {
        **EXTRACTION,
        "status": "usable",
        "confidence": "high",
        "warnings": [],
        "items": [
            {
                "intake_type": "beverage",
                "name_candidates": ["coffee"],
                "category": "coffee",
                "serving": {
                    "kind": "exact",
                    "unit": "ml",
                    "exact": 355,
                    "estimation_basis": "owner_statement",
                    "evidence_text": "owner said 355 ml",
                },
                "caffeine": {
                    "kind": "unknown",
                    "unit": "mg",
                },
                "nutrients": [],
                "confidence": "high",
                "warnings": [],
            }
        ],
    }
    provider = OpenAIVisionProvider(
        base_url="https://api.openai.test",
        api_key=SecretStr("secret"),
        model="model",
        timeout=10,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps(extraction),
                                }
                            ]
                        }
                    ]
                },
            )
        ),
    )

    with pytest.raises(VisionInvalidOutput, match="visible-label"):
        provider.analyze(image, allow_remote=True)


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


def test_gemini_adapter_analyzes_text():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": json.dumps(EXTRACTION)}]}}
                ]
            },
        )

    provider = GeminiVisionProvider(
        base_url="https://gemini.test/v1beta",
        api_key=SecretStr("secret"),
        model="model",
        timeout=10,
        transport=httpx.MockTransport(handler),
    )
    provider.analyze_text("샌드위치 하나", allow_remote=True)
    payload = json.loads(requests[0].content)
    assert payload["contents"][0]["parts"] == [{"text": "샌드위치 하나"}]


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


def test_anthropic_adapter_analyzes_text():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": json.dumps(EXTRACTION)}]
            },
        )

    provider = AnthropicVisionProvider(
        base_url="https://anthropic.test",
        api_key=SecretStr("secret"),
        model="model",
        timeout=10,
        transport=httpx.MockTransport(handler),
    )
    provider.analyze_text("단백질 음료", allow_remote=True)
    payload = json.loads(requests[0].content)
    assert payload["messages"] == [
        {"role": "user", "content": "단백질 음료"}
    ]


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


def test_xai_adapter_analyzes_text():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps(EXTRACTION)}}
                ]
            },
        )

    provider = XAIVisionProvider(
        base_url="https://xai.test",
        api_key=SecretStr("secret"),
        model="model",
        timeout=10,
        transport=httpx.MockTransport(handler),
    )
    provider.analyze_text("커피 355ml", allow_remote=True)
    payload = json.loads(requests[0].content)
    assert payload["messages"][1] == {
        "role": "user",
        "content": "커피 355ml",
    }
    assert payload["store"] is False


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

    with pytest.raises(VisionUnavailable, match="explicit"):
        provider.analyze_text("coffee", allow_remote=False)


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


def test_remote_adapter_sanitizes_heic_before_sending_image(tmp_path):
    image = tmp_path / "coffee.heic"
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
    assert data_url.startswith("data:image/jpeg;base64,")
    sanitized = Image.open(
        BytesIO(base64.b64decode(data_url.partition(",")[2]))
    )
    assert sanitized.getexif().get(0x010F) is None


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
