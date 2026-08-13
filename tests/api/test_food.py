"""Tests for the food-log router."""

from datetime import UTC, datetime


def test_create_food_log_with_explicit_time(client, parse_utc):
    response = client.post(
        "/v1/food-logs",
        json={
            "description": "Bibimbap with extra vegetables",
            "logged_at": "2026-07-08T12:30:00Z",
            "meal_type": "lunch",
            "media_path": "media/2026/07/08/lunch.jpg",
            "source": "telegram",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["description"] == "Bibimbap with extra vegetables"
    assert body["meal_type"] == "lunch"
    assert body["media_path"] == "media/2026/07/08/lunch.jpg"
    assert parse_utc(body["logged_at"]).isoformat() == "2026-07-08T12:30:00+00:00"


def test_create_food_log_defaults_logged_at_to_now(client, parse_utc):
    # freezegun breaks FastAPI's lazy route compilation (FakeDate vs pydantic
    # schema generation), so bound the default value with real clock reads.
    before = datetime.now(UTC)
    response = client.post("/v1/food-logs", json={"description": "Morning yogurt"})
    after = datetime.now(UTC)

    assert response.status_code == 201
    logged_at = parse_utc(response.json()["logged_at"])
    assert before <= logged_at <= after


def test_create_food_log_requires_description(client):
    response = client.post("/v1/food-logs", json={"meal_type": "snack"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_create_food_log_rejects_unknown_meal_type(client):
    response = client.post("/v1/food-logs", json={"description": "Tea", "meal_type": "brunch"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_create_food_log_accepts_uploaded_media_path(client):
    """Issue #10 capture loop: upload → log with the returned token → serve."""
    uploaded = client.post(
        "/v1/media", files={"file": ("meal.jpg", b"\xff\xd8fake-jpeg", "image/jpeg")}
    )
    assert uploaded.status_code == 201
    media_path = uploaded.json()["media_path"]

    created = client.post(
        "/v1/food-logs",
        json={"description": "Kimchi stew with rice", "media_path": media_path},
    )

    assert created.status_code == 201
    assert created.json()["media_path"] == media_path
    listed = client.get("/v1/food-logs").json()["data"][0]
    assert listed["media_path"] == media_path
    assert client.get(f"/v1/media/{media_path}").status_code == 200


def test_list_food_logs_range_filter_newest_first(client):
    for day, description in ((7, "Older"), (8, "Middle"), (9, "Newest")):
        client.post(
            "/v1/food-logs",
            json={
                "description": description,
                "logged_at": f"2026-07-{day:02d}T12:00:00Z",
            },
        )

    response = client.get(
        "/v1/food-logs",
        params={"start": "2026-07-08T00:00:00Z", "end": "2026-07-10T00:00:00Z"},
    )

    body = response.json()
    assert [entry["description"] for entry in body["data"]] == ["Newest", "Middle"]
    assert body["pagination"]["total_count"] == 2
