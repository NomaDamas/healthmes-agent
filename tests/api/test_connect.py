"""``GET /connect`` — read-only calendar-connection status page.

Pins: 200 with per-calendar connected/not-connected state derived from fake
creds files, the exact CLI commands for the not-connected ones, NO secret in
the markup (no app password, no token contents, not even the username), and
viewer-page gating (401 bare / 200 with the derived ?token= or the bearer —
same posture as /decisions).
"""

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from healthmes.api.auth import viewer_token
from healthmes.app import create_app
from healthmes.calendars import creds

TOKEN = "connect-page-api-token"
APP_PASSWORD = "abcd-efgh-ijkl-mnop"
REFRESH_TOKEN = "fake-refresh-token-value"


@pytest.fixture
def client(app):
    """Status page served by the shared api-test app (tokenless settings)."""
    with TestClient(app) as test_client:
        yield test_client


def connect_google(data_dir) -> None:
    token = data_dir / "google" / "calendar_token.json"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(
        json.dumps(
            {
                "type": "authorized_user",
                "refresh_token": REFRESH_TOKEN,
                "client_id": "x.apps.googleusercontent.com",
                "client_secret": "fake-client-secret-value",
            }
        ),
        encoding="utf-8",
    )


def connect_icloud(data_dir) -> None:
    creds.save_caldav_credentials(
        data_dir,
        username="me@icloud.com",
        app_password=APP_PASSWORD,
        url="https://caldav.icloud.com",
    )


def test_not_connected_shows_exact_commands(client) -> None:
    response = client.get("/connect")
    assert response.status_code == 200
    text = response.text
    assert "미연결" in text
    assert "uv run healthmes connect google" in text
    assert "uv run healthmes connect icloud --username you@icloud.com" in text
    # The one-time Google prerequisite and the iCloud app-password source.
    assert "console.cloud.google.com" in text
    assert "appleid.apple.com" in text


def test_connected_states_render_without_secrets(client, settings) -> None:
    connect_google(settings.data_dir)
    connect_icloud(settings.data_dir)
    response = client.get("/connect")
    assert response.status_code == 200
    text = response.text
    assert text.count("연결됨") == 2
    assert "미연결" not in text
    # No secret material — and not even the account identifier — renders.
    assert APP_PASSWORD not in text
    assert REFRESH_TOKEN not in text
    assert "fake-client-secret-value" not in text
    assert "me@icloud.com" not in text


def test_mixed_state_renders_per_calendar(client, settings) -> None:
    connect_icloud(settings.data_dir)
    text = client.get("/connect").text
    assert "연결됨" in text and "미연결" in text
    assert "uv run healthmes connect google" in text
    assert "uv run healthmes connect icloud" not in text  # connected: no command


def test_gating_matches_viewer_pages(settings) -> None:
    secured = settings.model_copy(update={"api_token": SecretStr(TOKEN)})
    with TestClient(create_app(secured)) as client:
        assert client.get("/connect").status_code == 401
        assert client.get("/connect", params={"token": "wrong"}).status_code == 401

        via_viewer_token = client.get("/connect", params={"token": viewer_token(TOKEN)})
        assert via_viewer_token.status_code == 200
        assert TOKEN not in via_viewer_token.text  # raw API token never renders

        via_bearer = client.get(
            "/connect", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert via_bearer.status_code == 200


def test_landing_links_to_connect(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert 'href="/connect"' in response.text
