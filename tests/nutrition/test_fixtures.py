import json
from pathlib import Path

from healthmes.api.nutrition_observations import AnalyzeNutritionPhoto
from healthmes.nutrition.schema import VLMExtraction

FIXTURES = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "nutrition"
    / "photo_observations.json"
)


def test_synthetic_photo_observation_fixtures_match_shared_contract():
    rows = json.loads(FIXTURES.read_text())

    assert len(rows) >= 6
    assert len({row["id"] for row in rows}) == len(rows)
    for row in rows:
        AnalyzeNutritionPhoto(
            media_path="media/2026/08/0123456789abcdef0123456789abcdef.jpg",
            allow_remote_vision=False,
            **row["request"],
        )
        VLMExtraction.model_validate(row["extraction"])


def test_fixtures_contain_no_embedded_image_or_personal_location():
    text = FIXTURES.read_text()

    assert "base64" not in text.lower()
    assert "data:image" not in text.lower()
    assert '"latitude"' not in text
    assert '"longitude"' not in text
