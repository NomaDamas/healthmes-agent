"""Sync jobs pick up runtime connections established via ``healthmes connect``.

Pins the contract of docs/PLAN.md §6 + the connect onboarding: "connected" =
the token/creds file under ``Settings.data_dir`` exists — no ``.env`` edit
needed — while the pre-existing env-based path keeps working and env
credentials override the file.
"""

import json

import pytest
from pydantic import SecretStr

from healthmes.calendars import creds, jobs
from healthmes.calendars.base import CalendarAuthError
from healthmes.store.enums import CalendarSource


def write_google_token(data_dir) -> None:
    token = data_dir / "google" / "calendar_token.json"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(
        json.dumps(
            {
                "type": "authorized_user",
                "refresh_token": "fake-refresh",
                "client_id": "x.apps.googleusercontent.com",
                "client_secret": "fake-secret",
            }
        ),
        encoding="utf-8",
    )


class TestRuntimeEnablement:
    def test_caldav_creds_file_enables_without_flag(self, settings) -> None:
        creds.save_caldav_credentials(
            settings.data_dir,
            username="me@icloud.com",
            app_password="pw",
            url="https://caldav.test",
        )
        assert jobs.enabled_sources(settings) == (CalendarSource.CALDAV,)
        assert jobs.write_source(settings) is CalendarSource.CALDAV
        specs = jobs.build_calendar_jobs(settings)
        assert [spec.source for spec in specs] == [CalendarSource.CALDAV]
        assert specs[0].interval_minutes == settings.caldav_poll_minutes

    def test_google_token_enables_without_flag(self, settings) -> None:
        write_google_token(settings.data_dir)
        assert jobs.enabled_sources(settings) == (CalendarSource.GOOGLE,)
        assert jobs.write_source(settings) is CalendarSource.GOOGLE

    def test_both_runtime_connections_keep_google_as_writer(self, settings) -> None:
        write_google_token(settings.data_dir)
        creds.save_caldav_credentials(
            settings.data_dir, username="me@icloud.com", app_password="pw", url="https://c.test"
        )
        assert jobs.enabled_sources(settings) == (
            CalendarSource.GOOGLE,
            CalendarSource.CALDAV,
        )
        assert jobs.write_source(settings) is CalendarSource.GOOGLE

    def test_broken_google_token_does_not_enable(self, settings) -> None:
        token = settings.data_dir / "google" / "calendar_token.json"
        token.parent.mkdir(parents=True)
        token.write_text("{broken", encoding="utf-8")
        assert jobs.enabled_sources(settings) == ()

    def test_env_flags_still_enable_without_files(self, settings) -> None:
        enabled = settings.model_copy(
            update={"google_calendar_enabled": True, "caldav_enabled": True}
        )
        assert jobs.enabled_sources(enabled) == (
            CalendarSource.GOOGLE,
            CalendarSource.CALDAV,
        )


class TestBackendCredsResolution:
    @pytest.fixture
    def captured_connect(self, monkeypatch) -> dict:
        captured: dict = {}

        def fake_connect(**kwargs):
            captured.update(kwargs)
            return object()

        monkeypatch.setattr(
            "healthmes.calendars.caldav_icloud.CalDavCalendarBackend.connect",
            staticmethod(fake_connect),
        )
        return captured

    def test_file_creds_used_when_env_unset(self, settings, captured_connect) -> None:
        creds.save_caldav_credentials(
            settings.data_dir,
            username="file@icloud.com",
            app_password="file-pw",
            url="https://caldav.file",
        )
        jobs._build_backend(settings, CalendarSource.CALDAV)
        assert captured_connect["username"] == "file@icloud.com"
        assert captured_connect["app_password"] == "file-pw"
        assert captured_connect["url"] == "https://caldav.file"

    def test_env_creds_override_file(self, settings, captured_connect) -> None:
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
        jobs._build_backend(env_settings, CalendarSource.CALDAV)
        assert captured_connect["username"] == "env@icloud.com"
        assert captured_connect["app_password"] == "env-pw"
        assert captured_connect["url"] == env_settings.caldav_url

    def test_no_credentials_raises_pointer_to_connect(self, settings) -> None:
        with pytest.raises(CalendarAuthError, match="connect icloud"):
            jobs._build_backend(settings, CalendarSource.CALDAV)
