"""Tests for the task CRUD + status-transition router."""


def _create_task(client, **overrides):
    payload = {"title": "Write report", **overrides}
    return client.post("/v1/tasks", json=payload)


def test_create_task_defaults(client):
    response = _create_task(client)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "todo"
    assert body["energy_demand"] == "med"
    assert body["source"] == "user"
    assert body["goal_id"] is None
    assert body["deadline"] is None


def test_create_task_with_existing_goal(client):
    goal_id = client.post("/v1/goals", json={"week_start": "2026-07-06", "title": "G"}).json()["id"]

    response = _create_task(client, goal_id=goal_id, energy_demand="high")

    assert response.status_code == 201
    assert response.json()["goal_id"] == goal_id
    assert response.json()["energy_demand"] == "high"


def test_create_task_with_missing_goal_is_invalid_reference(client):
    response = _create_task(client, goal_id="00000000-0000-0000-0000-000000000000")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_reference"


def test_create_task_validation_error(client):
    response = _create_task(client, est_minutes=0)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_status_transition_happy_path(client):
    task_id = _create_task(client).json()["id"]

    first = client.post(f"/v1/tasks/{task_id}/status", json={"status": "in_progress"})
    assert first.status_code == 200
    assert first.json()["status"] == "in_progress"

    second = client.post(f"/v1/tasks/{task_id}/status", json={"status": "done"})
    assert second.json()["status"] == "done"


def test_invalid_status_transition_is_409(client):
    task_id = _create_task(client).json()["id"]
    client.post(f"/v1/tasks/{task_id}/status", json={"status": "done"})

    response = client.post(f"/v1/tasks/{task_id}/status", json={"status": "in_progress"})

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "invalid_transition"
    assert error["detail"] == {"current": "done", "requested": "in_progress"}


def test_unknown_status_value_is_validation_error(client):
    task_id = _create_task(client).json()["id"]

    response = client.post(f"/v1/tasks/{task_id}/status", json={"status": "paused"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_patch_cannot_change_status_and_clears_nullable_fields(client):
    task_id = _create_task(client, deadline="2026-07-10T09:00:00Z").json()["id"]

    response = client.patch(
        f"/v1/tasks/{task_id}",
        json={"title": "Updated", "deadline": None, "status": "done"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Updated"
    assert body["deadline"] is None
    assert body["status"] == "todo"  # extra "status" key is ignored


def test_list_tasks_filters_by_status_and_due_before(client, parse_utc):
    early = _create_task(client, title="early", deadline="2026-07-08T09:00:00Z").json()
    _create_task(client, title="late", deadline="2026-07-20T09:00:00Z")
    no_deadline = _create_task(client, title="no-deadline").json()
    client.post(f"/v1/tasks/{no_deadline['id']}/status", json={"status": "cancelled"})

    todo_only = client.get("/v1/tasks", params={"status": "todo"}).json()
    assert [t["title"] for t in todo_only["data"]] == ["early", "late"]

    due = client.get("/v1/tasks", params={"due_before": "2026-07-09T00:00:00Z"}).json()
    assert [t["id"] for t in due["data"]] == [early["id"]]
    assert parse_utc(due["data"][0]["deadline"]).isoformat() == "2026-07-08T09:00:00+00:00"


def test_delete_task_then_404(client):
    task_id = _create_task(client).json()["id"]

    assert client.delete(f"/v1/tasks/{task_id}").status_code == 204
    missing = client.get(f"/v1/tasks/{task_id}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
