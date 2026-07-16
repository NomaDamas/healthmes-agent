"""Creds-layer tests (healthmes/calendars/creds.py).

Pin the security and resolution contract of the runtime connection layer:
credential files are owner-only (0600) from creation, corrupt/missing files
degrade to "not connected" (never an exception in the resolution path), and
env settings override the file — the pre-existing configuration path must
keep working unchanged.
"""

import json
import stat

import pytest
from pydantic import SecretStr

from healthmes.calendars import creds
from healthmes.calendars.base import CalendarError

PASSWORD = "abcd-efgh-ijkl-mnop"


def file_mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def write_google_token(data_dir, payload) -> None:
    token = data_dir / "google" / "calendar_token.json"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
    )


class TestCalDavCredsFile:
    def test_save_writes_owner_only_file(self, tmp_path) -> None:
        path = creds.save_caldav_credentials(
            tmp_path,
            username="me@icloud.com",
            app_password=PASSWORD,
            url="https://caldav.icloud.com",
        )
        assert path == creds.caldav_credentials_path(tmp_path)
        assert file_mode(path) == 0o600
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == {
            "username": "me@icloud.com",
            "app_password": PASSWORD,
            "url": "https://caldav.icloud.com",
        }

    def test_roundtrip(self, tmp_path) -> None:
        creds.save_caldav_credentials(
            tmp_path, username="me@icloud.com", app_password=PASSWORD, url="https://c.test"
        )
        loaded = creds.load_caldav_credentials(tmp_path)
        assert loaded is not None
        assert loaded.username == "me@icloud.com"
        assert loaded.app_password == PASSWORD
        assert loaded.url == "https://c.test"
        assert loaded.source == "file"

    def test_save_overwrite_keeps_owner_only(self, tmp_path) -> None:
        creds.save_caldav_credentials(
            tmp_path, username="a@icloud.com", app_password="one", url="https://c.test"
        )
        path = creds.save_caldav_credentials(
            tmp_path, username="b@icloud.com", app_password="two", url="https://c.test"
        )
        assert file_mode(path) == 0o600
        loaded = creds.load_caldav_credentials(tmp_path)
        assert loaded is not None and loaded.username == "b@icloud.com"

    def test_save_rejects_empty_values(self, tmp_path) -> None:
        with pytest.raises(CalendarError):
            creds.save_caldav_credentials(
                tmp_path, username="  ", app_password=PASSWORD, url="https://c.test"
            )
        with pytest.raises(CalendarError):
            creds.save_caldav_credentials(
                tmp_path, username="me@icloud.com", app_password="", url="https://c.test"
            )
        assert not creds.caldav_credentials_path(tmp_path).exists()

    def test_load_missing_returns_none(self, tmp_path) -> None:
        assert creds.load_caldav_credentials(tmp_path) is None

    def test_load_corrupt_returns_none(self, tmp_path) -> None:
        path = creds.caldav_credentials_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        assert creds.load_caldav_credentials(tmp_path) is None

    def test_load_incomplete_returns_none(self, tmp_path) -> None:
        path = creds.caldav_credentials_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"username": "me@icloud.com"}), encoding="utf-8")
        assert creds.load_caldav_credentials(tmp_path) is None

    def test_delete(self, tmp_path) -> None:
        creds.save_caldav_credentials(
            tmp_path, username="me@icloud.com", app_password=PASSWORD, url="https://c.test"
        )
        assert creds.delete_caldav_credentials(tmp_path) is True
        assert creds.load_caldav_credentials(tmp_path) is None
        assert creds.delete_caldav_credentials(tmp_path) is False


class TestResolveCalDav:
    def test_none_when_nothing_configured(self, settings) -> None:
        assert creds.resolve_caldav_credentials(settings) is None

    def test_file_used_when_env_unset(self, settings) -> None:
        creds.save_caldav_credentials(
            settings.data_dir,
            username="file@icloud.com",
            app_password="file-pw",
            url="https://caldav.file",
        )
        resolved = creds.resolve_caldav_credentials(settings)
        assert resolved is not None
        assert resolved.source == "file"
        assert resolved.username == "file@icloud.com"
        assert resolved.app_password == "file-pw"
        assert resolved.url == "https://caldav.file"

    def test_env_overrides_file(self, settings) -> None:
        creds.save_caldav_credentials(
            settings.data_dir,
            username="file@icloud.com",
            app_password="file-pw",
            url="https://caldav.file",
        )
        env_settings = settings.model_copy(
            update={
                "caldav_username": "env@icloud.com",
                "caldav_app_password": SecretStr("env-pw"),
            }
        )
        resolved = creds.resolve_caldav_credentials(env_settings)
        assert resolved is not None
        assert resolved.source == "env"
        assert resolved.username == "env@icloud.com"
        assert resolved.app_password == "env-pw"
        assert resolved.url == env_settings.caldav_url

    def test_partial_env_falls_back_to_file(self, settings) -> None:
        # Username alone in env is not a usable credential set; the stored
        # file (a complete set) wins over mixing the two.
        creds.save_caldav_credentials(
            settings.data_dir,
            username="file@icloud.com",
            app_password="file-pw",
            url="https://caldav.file",
        )
        partial = settings.model_copy(update={"caldav_username": "env@icloud.com"})
        resolved = creds.resolve_caldav_credentials(partial)
        assert resolved is not None and resolved.source == "file"

    def test_file_without_url_uses_settings_url(self, settings) -> None:
        path = creds.caldav_credentials_path(settings.data_dir)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"username": "file@icloud.com", "app_password": "file-pw"}),
            encoding="utf-8",
        )
        resolved = creds.resolve_caldav_credentials(settings)
        assert resolved is not None
        assert resolved.url == settings.caldav_url


class TestGoogleConnectionState:
    def test_not_connected_without_token_file(self, tmp_path) -> None:
        assert creds.google_connection_state(tmp_path) == "not_connected"
        assert creds.google_connected(tmp_path) is False

    def test_connected_with_refresh_material(self, tmp_path) -> None:
        write_google_token(
            tmp_path,
            {
                "type": "authorized_user",
                "refresh_token": "fake-refresh",
                "client_id": "x.apps.googleusercontent.com",
                "client_secret": "fake-secret",
            },
        )
        assert creds.google_connection_state(tmp_path) == "connected"
        assert creds.google_connected(tmp_path) is True

    def test_invalid_when_garbage(self, tmp_path) -> None:
        write_google_token(tmp_path, "{broken")
        assert creds.google_connection_state(tmp_path) == "invalid"
        assert creds.google_connected(tmp_path) is False

    def test_invalid_without_refresh_token(self, tmp_path) -> None:
        write_google_token(
            tmp_path,
            {"type": "authorized_user", "client_id": "x", "client_secret": "y"},
        )
        assert creds.google_connection_state(tmp_path) == "invalid"

    def test_delete_google_token(self, tmp_path) -> None:
        write_google_token(tmp_path, {"refresh_token": "r", "client_id": "c", "client_secret": "s"})
        assert creds.delete_google_token(tmp_path) is True
        assert creds.google_connection_state(tmp_path) == "not_connected"
        assert creds.delete_google_token(tmp_path) is False
