"""``GET /connect`` — read-only calendar-connection status page.

Pins: 200 with per-calendar connected/not-connected state derived from fake
creds files, the exact CLI commands for the not-connected ones, NO secret in
the markup (no app password, no token contents, not even the username), and
viewer-page gating (401 bare / 200 with the derived ?token= or the bearer —
same posture as /decisions).
"""

import json
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from healthmes.api.auth import viewer_token
from healthmes.api.connect import build_oura_card
from healthmes.app import create_app
from healthmes.calendars import creds
from healthmes.mcp_server.ow_client import OWClientError

TOKEN = "connect-page-api-token"
APP_PASSWORD = "abcd-efgh-ijkl-mnop"
REFRESH_TOKEN = "fake-refresh-token-value"
OW_API_KEY = "fake-open-wearables-api-key"


class FakeOpenWearables:
    def __init__(self, connections, sleep_rows=(), *, sleep_error=False):
        self.connections = connections
        self.sleep_rows = list(sleep_rows)
        self.sleep_error = sleep_error

    async def list_users(self, *, search=None, limit=100):
        return {"items": [{"id": "private-user-id"}]}

    async def get_connections(self, user_id):
        assert user_id == "private-user-id"
        return self.connections

    async def collect_sleep_summaries(self, user_id, start_date, end_date):
        assert user_id == "private-user-id"
        if self.sleep_error:
            raise OWClientError("redacted upstream failure")
        return self.sleep_rows


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


def test_not_connected_shows_exact_commands(client, settings) -> None:
    response = client.get("/connect")
    assert response.status_code == 200
    text = response.text
    assert 'rel="icon"' in text
    assert "data:image/svg+xml" in text
    assert "Oura · Open Wearables" in text
    assert OW_API_KEY not in text
    assert "미연결" in text
    assert "uv run healthmes connect google" in text
    assert "uv run healthmes connect icloud --username you@icloud.com" in text
    assert str(settings.data_dir) not in text
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
    assert str(settings.data_dir) not in text


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
        assert "healthmes_local_session" not in via_viewer_token.headers.get(
            "set-cookie", ""
        )

        via_bearer = client.get(
            "/connect", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert via_bearer.status_code == 200

    with TestClient(
        create_app(secured),
        base_url="http://127.0.0.1:8100",
    ) as loopback:
        assert loopback.get("/connect").status_code == 401
        raw_query = loopback.get(
            "/connect",
            params={"token": TOKEN},
        )
        assert raw_query.status_code == 401
        assert "healthmes_local_session" not in raw_query.headers.get("set-cookie", "")

        viewer_page = loopback.get(
            "/connect",
            params={"token": viewer_token(TOKEN)},
        )
        assert viewer_page.status_code == 200
        assert 'href="http://127.0.0.1:8100/connect/unlock"' in viewer_page.text
        assert 'name="api_token"' not in viewer_page.text
        assert TOKEN not in viewer_page.text
        assert "healthmes_local_session" not in viewer_page.headers.get(
            "set-cookie", ""
        )

        unlock_page = loopback.get("/connect/unlock")
        assert unlock_page.status_code == 200
        assert unlock_page.headers["cache-control"] == "no-store"
        assert unlock_page.headers["referrer-policy"] == "same-origin"
        assert 'action="http://127.0.0.1:8100/connect/unlock"' in unlock_page.text
        assert 'name="api_token"' in unlock_page.text

        rejected = loopback.post(
            "/connect/unlock",
            data={"api_token": "wrong"},
            headers={"Origin": "http://127.0.0.1:8100"},
            follow_redirects=False,
        )
        assert rejected.status_code == 401
        assert "healthmes_local_session" not in rejected.headers.get("set-cookie", "")

        unlocked = loopback.post(
            "/connect/unlock",
            data={"api_token": TOKEN},
            headers={"Origin": "http://127.0.0.1:8100"},
            follow_redirects=False,
        )
        assert unlocked.status_code == 303
        assert unlocked.headers["location"] == "/connect"
        assert TOKEN not in unlocked.headers["location"]
        assert TOKEN not in unlocked.text
        assert "healthmes_local_session" in unlocked.headers["set-cookie"]
        assert loopback.get("/connect").status_code == 200


def test_loopback_proxy_cannot_bootstrap_local_session_from_host_header(settings) -> None:
    secured = settings.model_copy(update={"api_token": SecretStr(TOKEN)})
    with TestClient(
        create_app(secured),
        base_url="http://proxy.test:8100",
    ) as proxied:
        bare = proxied.get("/connect", headers={"Host": "localhost:8100"})
        assert bare.status_code == 401
        assert "healthmes_local_session" not in bare.headers.get("set-cookie", "")

        viewer = proxied.get(
            "/connect",
            params={"token": viewer_token(TOKEN)},
            headers={"Host": "localhost:8100"},
        )
        assert viewer.status_code == 200
        assert "healthmes_local_session" not in viewer.headers.get("set-cookie", "")
        assert 'href="http://127.0.0.1:8100/connect/unlock"' in viewer.text
        assert 'name="api_token"' not in viewer.text
        assert 'action="/connect/google/start"' not in viewer.text

        proxied_unlock_page = proxied.get(
            "/connect/unlock",
            headers={"Host": "localhost:8100"},
        )
        assert proxied_unlock_page.status_code == 200
        assert (
            'action="http://127.0.0.1:8100/connect/unlock"'
            in proxied_unlock_page.text
        )

        rejected_unlock = proxied.post(
            "/connect/unlock",
            data={"api_token": TOKEN},
            headers={
                "Host": "localhost:8100",
                "Origin": "http://proxy.test:8100",
            },
            follow_redirects=False,
        )
        assert rejected_unlock.status_code == 403
        assert "healthmes_local_session" not in rejected_unlock.headers.get(
            "set-cookie", ""
        )

        forged = proxied.post(
            "/connect/google/disconnect",
            data={"csrf": "forged"},
            headers={
                "Host": "localhost:8100",
                "Origin": "http://localhost:8100",
                "Cookie": "healthmes_local_session=forged",
            },
            follow_redirects=False,
        )
        assert forged.status_code == 401


def test_landing_links_to_connect(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert 'href="/connect"' in response.text


class FakeCredentials:
    def to_json(self) -> str:
        return json.dumps(
            {
                "refresh_token": REFRESH_TOKEN,
                "client_id": "client-id",
                "client_secret": "client-secret",
            }
        )


class FakeWebFlow:
    credentials = FakeCredentials()

    def authorization_url(self, **kwargs):
        state = kwargs["state"]
        return f"https://accounts.google.test/authorize?state={state}", state

    def fetch_token(self, *, authorization_response):
        assert "code=one-time-code" in authorization_response


def test_loopback_google_oauth_connect_and_disconnect_without_secret_render(
    app,
    settings,
) -> None:
    client_secret = settings.data_dir / "google" / "client_secret.json"
    client_secret.parent.mkdir(parents=True, exist_ok=True)
    client_secret.write_text('{"installed":{}}', encoding="utf-8")
    app.state.google_oauth_flow_factory = lambda *_args: FakeWebFlow()

    with TestClient(app, base_url="http://127.0.0.1:8100") as local:
        page = local.get("/connect")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
        assert csrf is not None
        assert "이 Mac에서 Google 로그인" in page.text

        started = local.post(
            "/connect/google/start",
            data={"csrf": csrf.group(1)},
            headers={"Origin": "http://127.0.0.1:8100"},
            follow_redirects=False,
        )
        assert started.status_code == 303
        state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]

        callback = local.get(
            f"/connect/google/callback?state={state}&code=one-time-code",
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert callback.headers["location"] == "/connect?google=connected"
        connected = local.get(callback.headers["location"])
        assert "Google OAuth credential 확인됨" in connected.text
        assert REFRESH_TOKEN not in connected.text
        assert "client-secret" not in connected.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', connected.text)
        assert csrf is not None

        reconnecting = local.post(
            "/connect/google/reconnect",
            data={"csrf": csrf.group(1)},
            headers={"Origin": "http://127.0.0.1:8100"},
            follow_redirects=False,
        )
        assert reconnecting.status_code == 303
        assert creds.google_connection_state(settings.data_dir) == "connected"

        disconnected = local.post(
            "/connect/google/disconnect",
            data={"csrf": csrf.group(1)},
            headers={"Origin": "http://127.0.0.1:8100"},
            follow_redirects=False,
        )
        assert disconnected.status_code == 303
        assert creds.google_connection_state(settings.data_dir) == "not_connected"


@pytest.mark.asyncio
async def test_oura_card_reports_missing_api_key(settings) -> None:
    unconfigured = settings.model_copy(update={"ow_api_key": SecretStr("")})

    card = await build_oura_card(unconfigured)

    assert card.connected is False
    assert card.badge_label == "설정 필요"
    assert card.detail == "Open Wearables API key 미설정"


@pytest.mark.asyncio
async def test_oura_card_reports_fresh_active_connection_without_identity(settings) -> None:
    configured = settings.model_copy(update={"ow_api_key": SecretStr(OW_API_KEY)})
    card = await build_oura_card(
        configured,
        client=FakeOpenWearables(
            [
                {
                    "provider": "oura",
                    "provider_user_id": "must-not-render",
                    "provider_username": "private@example.com",
                    "status": "active",
                    "last_synced_at": "2026-07-28T09:30:00Z",
                }
            ],
            [{"date": "2026-07-28"}],
        ),
        now=datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
    )

    assert card.connected is True
    assert card.detail == "Oura 연결됨 · 최신 수면 오늘"
    assert "must-not-render" not in repr(card)
    assert "private@example.com" not in repr(card)
    assert card.notes == ()


@pytest.mark.asyncio
async def test_oura_card_ignores_malformed_upstream_rows(settings) -> None:
    configured = settings.model_copy(update={"ow_api_key": SecretStr(OW_API_KEY)})

    card = await build_oura_card(
        configured,
        client=FakeOpenWearables(
            [
                "malformed-connection",
                {"provider": "oura", "status": "active"},
            ],
            ["malformed-sleep", {"date": "2026-07-28"}],
        ),
        now=datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
    )

    assert card.connected is True
    assert card.detail == "Oura 연결됨 · 최신 수면 오늘"


@pytest.mark.asyncio
async def test_oura_card_reports_stale_connection(settings) -> None:
    configured = settings.model_copy(update={"ow_api_key": SecretStr(OW_API_KEY)})
    card = await build_oura_card(
        configured,
        client=FakeOpenWearables(
            [
                {
                    "provider": "oura",
                    "status": "active",
                    "last_synced_at": "2026-07-10T10:00:00Z",
                }
            ],
            [{"date": "2026-07-10"}],
        ),
        now=datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
    )

    assert card.connected is True
    assert card.detail == "Oura 연결됨 · 최신 수면 18일 전"
    assert card.notes == ("Oura 동기화 데이터가 오래되었습니다.",)


@pytest.mark.asyncio
async def test_oura_card_reports_inactive_and_missing_connections(settings) -> None:
    configured = settings.model_copy(update={"ow_api_key": SecretStr(OW_API_KEY)})

    inactive = await build_oura_card(
        configured,
        client=FakeOpenWearables([{"provider": "oura", "status": "error"}]),
    )
    missing = await build_oura_card(configured, client=FakeOpenWearables([]))

    assert inactive.connected is False
    assert inactive.badge_label == "확인 필요"
    assert inactive.detail == "Oura 연결 비활성"
    assert missing.connected is False
    assert missing.detail == "Oura 미연결"


@pytest.mark.asyncio
async def test_oura_card_reports_malformed_user_discovery_as_redacted_error(
    settings,
) -> None:
    configured = settings.model_copy(update={"ow_api_key": SecretStr(OW_API_KEY)})

    class MalformedUsers(FakeOpenWearables):
        async def list_users(self, *, search=None, limit=100):
            return {"items": ["private@example.com"]}

    card = await build_oura_card(configured, client=MalformedUsers([]))

    assert card.connected is False
    assert card.badge_label == "오류"
    assert card.detail == "Open Wearables 사용자 선택 필요"
    assert "private@example.com" not in repr(card)


@pytest.mark.asyncio
async def test_oura_card_reports_sleep_read_error(settings) -> None:
    configured = settings.model_copy(update={"ow_api_key": SecretStr(OW_API_KEY)})

    card = await build_oura_card(
        configured,
        client=FakeOpenWearables(
            [{"provider": "oura", "status": "active"}],
            sleep_error=True,
        ),
    )

    assert card.connected is False
    assert card.badge_label == "오류"
    assert card.detail == "Open Wearables 수면 데이터 확인 오류"
