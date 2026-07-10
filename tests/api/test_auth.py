"""Bearer-token auth tests: the whole surface (REST + /mcp) is gated.

Pins the PLAN §9 fix: with ``HEALTHMES_API_TOKEN`` set, no anonymous LAN peer
can read medical records / health context or write through /mcp; the Android
collector's ``Authorization: Bearer`` header is actually verified; and
decision-viewer links stay tappable via the derived read-only ?token=.
Without a configured token the middleware is absent (loopback dev path) and
the serve entrypoint refuses non-loopback binds.
"""

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from healthmes.__main__ import check_bind_safety
from healthmes.api.auth import viewer_token, viewer_url
from healthmes.app import create_app
from healthmes.store import Base
from healthmes.store.session import get_engine

TOKEN = "test-api-token-123"


@contextmanager
def app_client(settings):
    """TestClient over the real app factory + schema on the lifespan engine."""
    with TestClient(create_app(settings)) as client:
        Base.metadata.create_all(get_engine())
        yield client

PAYLOAD = {
    "device_id": "android-abc123",
    "samples": [
        {
            "bucket_start": "2026-07-09T10:00:00Z",
            "app_package": "com.slack",
            "foreground_seconds": 600,
            "launches": 4,
            "category": "productivity",
        }
    ],
}


@pytest.fixture
def secured_client(settings):
    # model_copy skips validation, so the SecretStr must be passed explicitly.
    secured = settings.model_copy(update={"api_token": SecretStr(TOKEN)})
    with app_client(secured) as client:
        yield client


def bearer(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestTokenRequired:
    def test_rest_read_rejected_without_token(self, secured_client) -> None:
        response = secured_client.get("/v1/medical-records")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_rest_read_allowed_with_token(self, secured_client) -> None:
        response = secured_client.get("/v1/medical-records", headers=bearer())
        assert response.status_code == 200

    def test_wrong_token_rejected(self, secured_client) -> None:
        response = secured_client.get("/v1/tasks", headers=bearer("wrong"))
        assert response.status_code == 401

    def test_android_ingest_header_is_verified(self, secured_client) -> None:
        # The collector sends Authorization: Bearer <token> (IngestClient.kt);
        # the server now actually checks it.
        anonymous = secured_client.post("/v1/app-usage/batch", json=PAYLOAD)
        assert anonymous.status_code == 401

        authorized = secured_client.post(
            "/v1/app-usage/batch", json=PAYLOAD, headers=bearer()
        )
        assert authorized.status_code == 200
        assert authorized.json()["accepted"] == 1

    def test_mcp_endpoint_is_gated(self, secured_client) -> None:
        anonymous = secured_client.post("/mcp", json={})
        assert anonymous.status_code == 401
        # With the token the request reaches the MCP app (its own protocol
        # errors are fine — anything but the auth 401 envelope).
        authorized = secured_client.post("/mcp", json={}, headers=bearer())
        assert authorized.status_code != 401

    def test_health_probe_stays_open(self, secured_client) -> None:
        response = secured_client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestViewerLinks:
    def test_viewer_page_accepts_derived_query_token(self, secured_client) -> None:
        token = viewer_token(TOKEN)
        # Auth passes (route then 404s on the unknown id — not 401).
        response = secured_client.get(
            "/decisions/00000000-0000-0000-0000-000000000000",
            params={"token": token},
        )
        assert response.status_code == 404

    def test_viewer_page_rejects_missing_or_wrong_query_token(self, secured_client) -> None:
        bare = secured_client.get("/decisions/00000000-0000-0000-0000-000000000000")
        assert bare.status_code == 401
        wrong = secured_client.get(
            "/decisions/00000000-0000-0000-0000-000000000000",
            params={"token": "nope"},
        )
        assert wrong.status_code == 401

    def test_query_token_never_authorizes_the_api(self, secured_client) -> None:
        # The derived viewer credential is read-only for viewer pages; it must
        # not open /v1 or /mcp.
        response = secured_client.get(
            "/v1/medical-records", params={"token": viewer_token(TOKEN)}
        )
        assert response.status_code == 401

    def test_viewer_token_is_derived_and_stable(self) -> None:
        assert viewer_token(TOKEN) == viewer_token(TOKEN)
        assert viewer_token(TOKEN) != viewer_token("other")
        assert TOKEN not in viewer_token(TOKEN)

    def test_viewer_url_is_the_single_construction_point(self, settings) -> None:
        # Tokenless (loopback dev): plain public-base link, no query credential.
        assert viewer_url(settings, "/decisions/abc") == (
            "http://healthmes.test:8100/decisions/abc"
        )
        # Token configured: the derived read-only credential is embedded —
        # never the API token itself. Every emitter (decision viewer, glance
        # deep links, weekly report, MCP record_decision) goes through here.
        secured = settings.model_copy(update={"api_token": SecretStr(TOKEN)})
        url = viewer_url(secured, "/reports/weekly")
        assert url == (
            f"http://healthmes.test:8100/reports/weekly?token={viewer_token(TOKEN)}"
        )
        assert TOKEN not in url


class TestNoTokenConfigured:
    def test_loopback_dev_path_stays_open(self, settings) -> None:
        with app_client(settings) as client:
            assert client.get("/v1/tasks").status_code == 200

    def test_serve_refuses_non_loopback_bind_without_token(self, settings) -> None:
        lan = settings.model_copy(update={"host": "0.0.0.0"})
        error = check_bind_safety(lan)
        assert error is not None
        assert "HEALTHMES_API_TOKEN" in error

    def test_serve_allows_loopback_without_token(self, settings) -> None:
        assert check_bind_safety(settings) is None

    def test_serve_allows_non_loopback_with_token(self, settings) -> None:
        lan = settings.model_copy(
            update={"host": "0.0.0.0", "api_token": SecretStr(TOKEN)}
        )
        assert check_bind_safety(lan) is None
