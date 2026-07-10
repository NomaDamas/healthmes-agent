"""Wiring tests: router registration, OpenAPI generation, OW client seam."""

from starlette.requests import Request

from healthmes.api import routers
from healthmes.api.insights import get_ow_client
from healthmes.mcp_server.ow_client import OWClient

EXPECTED_PATHS = [
    "/v1/goals",
    "/v1/goals/{goal_id}",
    "/v1/tasks",
    "/v1/tasks/{task_id}",
    "/v1/tasks/{task_id}/status",
    "/v1/schedule/events",
    "/v1/schedule/proposals",
    "/v1/schedule/proposals/{proposal_id}/accept",
    "/v1/schedule/proposals/{proposal_id}/decline",
    "/v1/food-logs",
    "/v1/medical-records",
    "/v1/medical-records/{record_id}",
    "/v1/insights",
    "/v1/insights/recompute",
    "/v1/decisions/{decision_id}",
    "/decisions/{decision_id}",
    "/v1/app-usage/batch",
    "/cognitive-energy/forecast",
    "/v1/briefing/glance",
    "/reports/weekly",
    "/reports/weekly.json",
]


def test_routers_list_covers_all_modules():
    assert len(routers) == 11


def test_openapi_schema_generates_with_all_paths(client):
    response = client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    for path in EXPECTED_PATHS:
        assert path in paths, f"missing path: {path}"


def test_unknown_path_uses_error_envelope(client):
    response = client.get("/v1/nope")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_get_ow_client_falls_back_to_settings(app):
    # No app.state.ow_client injected -> built from Settings (localhost-native
    # defaults in production; test values here). The insights path shares the
    # one OWClient implementation with the MCP tools / engines.
    request = Request({"type": "http", "app": app, "headers": []})

    ow = get_ow_client(request)

    assert isinstance(ow, OWClient)
    assert ow.base_url == "http://open-wearables.test"
    assert ow.headers["X-Open-Wearables-API-Key"] == "test-ow-api-key"


def test_get_ow_client_prefers_injected_client(app, ow_client_factory):
    injected = ow_client_factory(lambda request: None)
    app.state.ow_client = injected
    request = Request({"type": "http", "app": app, "headers": []})

    assert get_ow_client(request) is injected
