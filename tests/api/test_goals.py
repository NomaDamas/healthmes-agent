"""Tests for the weekly-goal CRUD router."""

import uuid

from healthmes.store import WeeklyGoal


def _create_goal(client, **overrides):
    payload = {"week_start": "2026-07-06", "title": "Ship the API", **overrides}
    return client.post("/v1/goals", json=payload)


def test_create_goal_returns_201_with_defaults(client, session):
    response = _create_goal(client)

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Ship the API"
    assert body["week_start"] == "2026-07-06"
    assert body["priority"] == 0
    assert body["status"] == "active"
    assert session.get(WeeklyGoal, uuid.UUID(body["id"])) is not None


def test_create_goal_missing_title_is_validation_error_envelope(client):
    response = client.post("/v1/goals", json={"week_start": "2026-07-06"})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["message"] == "Request validation failed"
    assert any(item["loc"][-1] == "title" for item in error["detail"])


def test_get_goal_and_404_envelope(client):
    goal_id = _create_goal(client).json()["id"]

    assert client.get(f"/v1/goals/{goal_id}").json()["id"] == goal_id

    missing = client.get("/v1/goals/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


def test_list_goals_filters_and_paginates(client):
    _create_goal(client, title="A", priority=1)
    _create_goal(client, title="B", priority=5)
    _create_goal(client, title="C", week_start="2026-07-13")

    response = client.get("/v1/goals", params={"limit": 2, "offset": 0})
    body = response.json()
    assert body["pagination"] == {
        "total_count": 3,
        "limit": 2,
        "offset": 0,
        "has_more": True,
    }
    # Newest week first, then priority desc.
    assert [g["title"] for g in body["data"]] == ["C", "B"]

    rest = client.get("/v1/goals", params={"limit": 2, "offset": 2}).json()
    assert [g["title"] for g in rest["data"]] == ["A"]
    assert rest["pagination"]["has_more"] is False

    filtered = client.get("/v1/goals", params={"week_start": "2026-07-13"}).json()
    assert [g["title"] for g in filtered["data"]] == ["C"]


def test_list_goals_rejects_limit_above_200(client):
    response = client.get("/v1/goals", params={"limit": 201})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_patch_goal_updates_only_sent_fields(client):
    goal_id = _create_goal(client).json()["id"]

    response = client.patch(f"/v1/goals/{goal_id}", json={"status": "done", "priority": 3})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "done"
    assert body["priority"] == 3
    assert body["title"] == "Ship the API"


def test_patch_goal_rejects_unknown_status(client):
    goal_id = _create_goal(client).json()["id"]

    response = client.patch(f"/v1/goals/{goal_id}", json={"status": "nope"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_delete_goal_then_404(client):
    goal_id = _create_goal(client).json()["id"]

    assert client.delete(f"/v1/goals/{goal_id}").status_code == 204
    assert client.get(f"/v1/goals/{goal_id}").status_code == 404
