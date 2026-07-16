"""CLI tests: ``healthmes connect google|icloud|status|disconnect``.

All offline: the CalDAV validation and the Google OAuth flow are
monkeypatched fakes; credentials come from fake token/creds files. The suite
pins the security contract — the app password is prompted hidden (never
argv), the creds file lands owner-only (0600), and no secret value ever
appears on stdout/stderr.
"""

import json
import stat
from pathlib import Path

import pytest

from healthmes.__main__ import main
from healthmes.calendars import creds
from healthmes.calendars.base import CalendarAuthError

APP_PASSWORD = "abcd-efgh-ijkl-mnop"
REFRESH_TOKEN = "fake-refresh-token-value"


@pytest.fixture
def connect_env(tmp_path, monkeypatch) -> Path:
    """CLI environment: tmp cwd (no repo .env) + tmp data dir via env vars."""
    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "data"
    monkeypatch.setenv("HEALTHMES_DATA_DIR", str(data_dir))
    for var in (
        "HEALTHMES_CALDAV_USERNAME",
        "HEALTHMES_CALDAV_APP_PASSWORD",
        "HEALTHMES_CALDAV_ENABLED",
        "HEALTHMES_CALDAV_URL",
        "HEALTHMES_GOOGLE_CALENDAR_ENABLED",
        "HEALTHMES_GOOGLE_CLIENT_SECRET_FILE",
        "HEALTHMES_SCHEDULER_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    return data_dir


def write_google_token(data_dir: Path) -> Path:
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
    return token


class TestStatus:
    def test_nothing_connected(self, connect_env, capsys) -> None:
        assert main(["connect", "status"]) == 0
        out = capsys.readouterr().out
        assert "google: not connected" in out
        assert "icloud: not connected" in out

    def test_connected_states_never_print_secrets(self, connect_env, capsys) -> None:
        write_google_token(connect_env)
        creds.save_caldav_credentials(
            connect_env,
            username="me@icloud.com",
            app_password=APP_PASSWORD,
            url="https://caldav.icloud.com",
        )
        assert main(["connect", "status"]) == 0
        out = capsys.readouterr().out
        assert "google: connected" in out
        assert "icloud: connected as me@icloud.com" in out
        assert APP_PASSWORD not in out
        assert REFRESH_TOKEN not in out

    def test_broken_google_token_reported(self, connect_env, capsys) -> None:
        token = connect_env / "google" / "calendar_token.json"
        token.parent.mkdir(parents=True)
        token.write_text("{broken", encoding="utf-8")
        assert main(["connect", "status"]) == 0
        out = capsys.readouterr().out
        assert "google: not connected" in out
        assert "unusable" in out


class TestConnectICloud:
    def test_connect_writes_owner_only_creds(self, connect_env, capsys, monkeypatch) -> None:
        received = {}

        def fake_validate(*, username: str, app_password: str, url: str) -> str:
            received.update(username=username, app_password=app_password, url=url)
            return "2 calendar(s): 가족, 업무"

        monkeypatch.setattr("getpass.getpass", lambda prompt="": APP_PASSWORD + "\n")
        monkeypatch.setattr(
            "healthmes.calendars.creds.validate_caldav_connection", fake_validate
        )

        assert main(["connect", "icloud", "--username", "me@icloud.com"]) == 0

        # The prompted (stripped) password reached the real-connection check...
        assert received == {
            "username": "me@icloud.com",
            "app_password": APP_PASSWORD,
            "url": "https://caldav.icloud.com",
        }
        # ...and was persisted owner-only.
        path = creds.caldav_credentials_path(connect_env)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["username"] == "me@icloud.com"
        assert stored["app_password"] == APP_PASSWORD
        captured = capsys.readouterr()
        assert "connected as me@icloud.com" in captured.out
        assert APP_PASSWORD not in captured.out + captured.err  # never echoed

    def test_validation_failure_stores_nothing(self, connect_env, capsys, monkeypatch) -> None:
        monkeypatch.setattr("getpass.getpass", lambda prompt="": APP_PASSWORD)

        def failing_validate(**_kwargs):
            raise CalendarAuthError("CalDAV login/discovery failed: 401 unauthorized")

        monkeypatch.setattr(
            "healthmes.calendars.creds.validate_caldav_connection", failing_validate
        )

        assert main(["connect", "icloud", "--username", "me@icloud.com"]) == 1
        captured = capsys.readouterr()
        assert "error:" in captured.err
        assert APP_PASSWORD not in captured.out + captured.err
        assert not creds.caldav_credentials_path(connect_env).exists()

    def test_empty_password_aborts(self, connect_env, capsys, monkeypatch) -> None:
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "  ")
        monkeypatch.setattr(
            "healthmes.calendars.creds.validate_caldav_connection",
            lambda **_kw: pytest.fail("validation must not run on an empty password"),
        )
        assert main(["connect", "icloud", "--username", "me@icloud.com"]) == 1
        assert "empty password" in capsys.readouterr().err
        assert not creds.caldav_credentials_path(connect_env).exists()


class TestConnectGoogle:
    def test_missing_client_secret_prints_instructions(self, connect_env, capsys) -> None:
        assert main(["connect", "google"]) == 1
        err = capsys.readouterr().err
        assert "console.cloud.google.com" in err
        assert str(connect_env / "google" / "client_secret.json") in err
        assert "Desktop app" in err
        assert "HEALTHMES_GOOGLE_CLIENT_SECRET_FILE" in err

    def test_connect_runs_installed_app_flow(self, connect_env, capsys, monkeypatch) -> None:
        client_secret = connect_env / "google" / "client_secret.json"
        client_secret.parent.mkdir(parents=True)
        client_secret.write_text(json.dumps({"installed": {"client_id": "x"}}), encoding="utf-8")
        calls = {}

        def fake_flow(client_secret_file, token_file, scopes=None, *, port=0):
            calls["client_secret"] = Path(client_secret_file)
            calls["token_file"] = Path(token_file)
            calls["port"] = port
            write_google_token(connect_env)
            return object()

        monkeypatch.setattr("healthmes.calendars.google.run_installed_app_flow", fake_flow)
        # The identity probe degrades gracefully when the API is unreachable.
        monkeypatch.setattr(
            "healthmes.calendars.google.build_calendar_service",
            lambda credentials: (_ for _ in ()).throw(RuntimeError("offline")),
        )

        assert main(["connect", "google"]) == 0
        assert calls["client_secret"] == client_secret
        assert calls["token_file"] == connect_env / "google" / "calendar_token.json"
        out = capsys.readouterr().out
        assert "connected" in out
        assert "token saved to" in out

    def test_connect_reports_identity_when_probe_works(
        self, connect_env, capsys, monkeypatch
    ) -> None:
        client_secret = connect_env / "google" / "client_secret.json"
        client_secret.parent.mkdir(parents=True)
        client_secret.write_text("{}", encoding="utf-8")

        def fake_flow(client_secret_file, token_file, scopes=None, *, port=0):
            write_google_token(connect_env)
            return object()

        class FakeCalendars:
            def get(self, calendarId):  # noqa: N803 - google API parameter name
                assert calendarId == "primary"
                return self

            def execute(self):
                return {"summary": "me@gmail.com"}

        class FakeService:
            def calendars(self):
                return FakeCalendars()

        monkeypatch.setattr("healthmes.calendars.google.run_installed_app_flow", fake_flow)
        monkeypatch.setattr(
            "healthmes.calendars.google.build_calendar_service", lambda c: FakeService()
        )

        assert main(["connect", "google"]) == 0
        out = capsys.readouterr().out
        assert "connected as me@gmail.com" in out
        assert REFRESH_TOKEN not in out

    def test_client_secret_override_via_env(
        self, connect_env, capsys, monkeypatch, tmp_path
    ) -> None:
        elsewhere = tmp_path / "downloads" / "oauth-client.json"
        elsewhere.parent.mkdir(parents=True)
        elsewhere.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("HEALTHMES_GOOGLE_CLIENT_SECRET_FILE", str(elsewhere))
        calls = {}

        def fake_flow(client_secret_file, token_file, scopes=None, *, port=0):
            calls["client_secret"] = Path(client_secret_file)
            write_google_token(connect_env)
            return object()

        monkeypatch.setattr("healthmes.calendars.google.run_installed_app_flow", fake_flow)
        monkeypatch.setattr(
            "healthmes.calendars.google.build_calendar_service",
            lambda c: (_ for _ in ()).throw(RuntimeError("offline")),
        )

        assert main(["connect", "google"]) == 0
        assert calls["client_secret"] == elsewhere

    def test_already_connected_skips_flow(self, connect_env, capsys, monkeypatch) -> None:
        write_google_token(connect_env)
        monkeypatch.setattr(
            "healthmes.calendars.google.run_installed_app_flow",
            lambda *a, **kw: pytest.fail("flow must not run when already connected"),
        )
        assert main(["connect", "google"]) == 0
        assert "already connected" in capsys.readouterr().out


class TestDisconnect:
    def test_disconnect_google_removes_token(self, connect_env, capsys) -> None:
        token = write_google_token(connect_env)
        assert main(["connect", "disconnect", "google"]) == 0
        assert not token.exists()
        assert "removed" in capsys.readouterr().out
        # Idempotent second run.
        assert main(["connect", "disconnect", "google"]) == 0
        assert "nothing to remove" in capsys.readouterr().out

    def test_disconnect_icloud_removes_creds(self, connect_env, capsys) -> None:
        creds.save_caldav_credentials(
            connect_env, username="me@icloud.com", app_password=APP_PASSWORD, url="https://c.test"
        )
        assert main(["connect", "disconnect", "icloud"]) == 0
        assert not creds.caldav_credentials_path(connect_env).exists()
        out = capsys.readouterr().out
        assert "removed" in out
        assert APP_PASSWORD not in out

    def test_disconnect_icloud_warns_when_env_still_configured(
        self, connect_env, capsys, monkeypatch
    ) -> None:
        monkeypatch.setenv("HEALTHMES_CALDAV_USERNAME", "env@icloud.com")
        monkeypatch.setenv("HEALTHMES_CALDAV_APP_PASSWORD", "env-pw")
        creds.save_caldav_credentials(
            connect_env, username="me@icloud.com", app_password=APP_PASSWORD, url="https://c.test"
        )
        assert main(["connect", "disconnect", "icloud"]) == 0
        out = capsys.readouterr().out
        assert "HEALTHMES_CALDAV_USERNAME" in out
        assert "env-pw" not in out
