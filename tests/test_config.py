"""Unit tests for Settings (env prefix, defaults, secret handling)."""

import datetime
from pathlib import Path

import pytest

from healthmes.config import Settings

ALL_ENV_VARS = [
    "HEALTHMES_DATABASE_URL",
    "HEALTHMES_OW_BASE_URL",
    "HEALTHMES_OW_API_KEY",
    "HEALTHMES_OW_USER_ID",
    "HEALTHMES_HERMES_WEBHOOK_URL",
    "HEALTHMES_HERMES_WEBHOOK_SECRET",
    "HEALTHMES_PUBLIC_BASE_URL",
    "HEALTHMES_DATA_DIR",
    "HEALTHMES_PORT",
    "HEALTHMES_HOST",
    "HEALTHMES_API_TOKEN",
    "HEALTHMES_SCHEDULER_ENABLED",
    "HEALTHMES_TIMEZONE",
    "HEALTHMES_BACKUP_DIR",
    "HEALTHMES_BACKUP_PASSPHRASE",
    "HEALTHMES_OW_DATABASE_URL",
    "HEALTHMES_HERMES_HOME",
    "HEALTHMES_QUIET_HOURS_START",
    "HEALTHMES_QUIET_HOURS_END",
    "HEALTHMES_ALERT_DAILY_BUDGET",
    "HEALTHMES_ALERT_COOLDOWN_MINUTES",
    "HEALTHMES_GOOGLE_CALENDAR_ENABLED",
    "HEALTHMES_GOOGLE_CALENDAR_ID",
    "HEALTHMES_GOOGLE_POLL_MINUTES",
    "HEALTHMES_CALDAV_ENABLED",
    "HEALTHMES_CALDAV_URL",
    "HEALTHMES_CALDAV_USERNAME",
    "HEALTHMES_CALDAV_APP_PASSWORD",
    "HEALTHMES_CALDAV_CALENDAR_NAME",
    "HEALTHMES_CALDAV_POLL_MINUTES",
]


def _clean_settings(**overrides) -> Settings:
    """Settings that ignore any local .env file."""
    return Settings(_env_file=None, **overrides)


def test_defaults(monkeypatch) -> None:
    for var in ALL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    settings = _clean_settings()

    # Zero-setup native default: repo-local sqlite (postgres via env/.env).
    assert settings.database_url == "sqlite:///./data/healthmes.db"
    assert settings.ow_base_url == "http://localhost:8000"
    assert settings.ow_api_key.get_secret_value() == ""
    assert settings.ow_user_id is None
    # 8644 = DEFAULT_PORT in vendor/hermes-agent/gateway/platforms/webhook.py;
    # 'healthmes-alerts' is the route name in config/hermes-config.yaml.tmpl.
    assert settings.hermes_webhook_url == "http://localhost:8644/webhooks/healthmes-alerts"
    assert settings.hermes_webhook_secret.get_secret_value() == ""
    assert settings.public_base_url == "http://localhost:8100"
    assert settings.data_dir == Path("data")
    assert settings.port == 8100
    # Localhost-native bind + no token by default: the surface stays off the
    # network unless the operator opts in (and then a token is enforced).
    assert settings.host == "127.0.0.1"
    assert settings.api_token.get_secret_value() == ""
    assert settings.scheduler_enabled is False
    assert settings.timezone is None  # machine-local tz (mac-native default)
    # Backup seam (docs/PLAN.md §9): everything optional by default.
    assert settings.backup_dir is None  # -> {data_dir}/backups
    assert settings.backup_passphrase.get_secret_value() == ""
    assert settings.ow_database_url is None
    assert settings.hermes_home is None
    assert settings.quiet_hours_start == datetime.time(22, 30)
    assert settings.quiet_hours_end == datetime.time(7, 0)
    assert settings.alert_daily_budget == 8
    assert settings.alert_cooldown_minutes == 60
    # Calendar backends are opt-in (real credentials required).
    assert settings.google_calendar_enabled is False
    assert settings.google_calendar_id == "primary"
    assert settings.google_poll_minutes == 5
    assert settings.caldav_enabled is False
    assert settings.caldav_url == "https://caldav.icloud.com"
    assert settings.caldav_username == ""
    assert settings.caldav_app_password.get_secret_value() == ""
    assert settings.caldav_calendar_name is None
    assert settings.caldav_poll_minutes == 10


def test_env_prefix_is_healthmes(monkeypatch) -> None:
    monkeypatch.setenv("HEALTHMES_PORT", "8555")
    monkeypatch.setenv("HEALTHMES_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("HEALTHMES_OW_BASE_URL", "http://ow.internal:8000")
    monkeypatch.setenv("HEALTHMES_DATA_DIR", "/tmp/hm-data")
    monkeypatch.setenv("HEALTHMES_PUBLIC_BASE_URL", "https://healthmes.example.com")

    settings = _clean_settings()

    assert settings.port == 8555
    assert settings.scheduler_enabled is True
    assert settings.ow_base_url == "http://ow.internal:8000"
    assert settings.data_dir == Path("/tmp/hm-data")
    assert settings.public_base_url == "https://healthmes.example.com"


def test_quiet_hours_and_alert_budget_from_env(monkeypatch) -> None:
    monkeypatch.setenv("HEALTHMES_QUIET_HOURS_START", "23:15")
    monkeypatch.setenv("HEALTHMES_QUIET_HOURS_END", "06:00")
    monkeypatch.setenv("HEALTHMES_ALERT_DAILY_BUDGET", "3")
    monkeypatch.setenv("HEALTHMES_ALERT_COOLDOWN_MINUTES", "120")

    settings = _clean_settings()

    assert settings.quiet_hours_start == datetime.time(23, 15)
    assert settings.quiet_hours_end == datetime.time(6, 0)
    assert settings.alert_daily_budget == 3
    assert settings.alert_cooldown_minutes == 120


def test_alert_budget_must_be_non_negative() -> None:
    with pytest.raises(ValueError):
        _clean_settings(alert_daily_budget=-1)


def test_calendar_and_ow_user_settings_from_env(monkeypatch) -> None:
    monkeypatch.setenv("HEALTHMES_OW_USER_ID", "0f2f6a51-9a7e-4a76-9f4e-3f2b0c8d9e10")
    monkeypatch.setenv("HEALTHMES_GOOGLE_CALENDAR_ENABLED", "true")
    monkeypatch.setenv("HEALTHMES_GOOGLE_CALENDAR_ID", "work@example.com")
    monkeypatch.setenv("HEALTHMES_GOOGLE_POLL_MINUTES", "3")
    monkeypatch.setenv("HEALTHMES_CALDAV_ENABLED", "true")
    monkeypatch.setenv("HEALTHMES_CALDAV_URL", "https://dav.example.com")
    monkeypatch.setenv("HEALTHMES_CALDAV_USERNAME", "me@example.com")
    monkeypatch.setenv("HEALTHMES_CALDAV_APP_PASSWORD", "abcd-efgh-ijkl-mnop")
    monkeypatch.setenv("HEALTHMES_CALDAV_CALENDAR_NAME", "HealthMes")
    monkeypatch.setenv("HEALTHMES_CALDAV_POLL_MINUTES", "15")

    settings = _clean_settings()

    assert settings.ow_user_id == "0f2f6a51-9a7e-4a76-9f4e-3f2b0c8d9e10"
    assert settings.google_calendar_enabled is True
    assert settings.google_calendar_id == "work@example.com"
    assert settings.google_poll_minutes == 3
    assert settings.caldav_enabled is True
    assert settings.caldav_url == "https://dav.example.com"
    assert settings.caldav_username == "me@example.com"
    assert settings.caldav_app_password.get_secret_value() == "abcd-efgh-ijkl-mnop"
    assert "abcd-efgh-ijkl-mnop" not in repr(settings)
    assert settings.caldav_calendar_name == "HealthMes"
    assert settings.caldav_poll_minutes == 15


def test_timezone_and_backup_settings_from_env(monkeypatch) -> None:
    monkeypatch.setenv("HEALTHMES_TIMEZONE", "Asia/Seoul")
    monkeypatch.setenv("HEALTHMES_BACKUP_DIR", "/tmp/hm-backups")
    monkeypatch.setenv("HEALTHMES_BACKUP_PASSPHRASE", "correct horse battery staple")
    monkeypatch.setenv(
        "HEALTHMES_OW_DATABASE_URL", "postgresql://ow:ow@localhost:5432/open-wearables"
    )
    monkeypatch.setenv("HEALTHMES_HERMES_HOME", "/tmp/hermes-home")

    settings = _clean_settings()

    assert settings.timezone == "Asia/Seoul"
    assert settings.backup_dir == Path("/tmp/hm-backups")
    assert settings.backup_passphrase.get_secret_value() == "correct horse battery staple"
    assert "correct horse battery staple" not in repr(settings)
    assert settings.ow_database_url == "postgresql://ow:ow@localhost:5432/open-wearables"
    assert settings.hermes_home == Path("/tmp/hermes-home")


def test_blank_optional_env_vars_behave_like_unset(monkeypatch) -> None:
    """docker-compose forwards optional vars as empty strings; '' must mean
    'unset' (Path('') would otherwise silently become Path('.'))."""
    for var in (
        "HEALTHMES_TIMEZONE",
        "HEALTHMES_BACKUP_DIR",
        "HEALTHMES_OW_DATABASE_URL",
        "HEALTHMES_HERMES_HOME",
        "HEALTHMES_OW_USER_ID",
    ):
        monkeypatch.setenv(var, "")

    settings = _clean_settings()

    assert settings.timezone is None
    assert settings.backup_dir is None
    assert settings.ow_database_url is None
    assert settings.hermes_home is None
    assert settings.ow_user_id is None


def test_unprefixed_env_vars_are_ignored(monkeypatch) -> None:
    monkeypatch.delenv("HEALTHMES_PORT", raising=False)
    monkeypatch.setenv("PORT", "1234")

    settings = _clean_settings()

    assert settings.port == 8100


def test_secrets_are_not_leaked_in_repr(monkeypatch) -> None:
    monkeypatch.setenv("HEALTHMES_OW_API_KEY", "super-secret-key")
    monkeypatch.setenv("HEALTHMES_HERMES_WEBHOOK_SECRET", "hmac-secret")
    monkeypatch.setenv("HEALTHMES_API_TOKEN", "bearer-secret")

    settings = _clean_settings()

    assert "super-secret-key" not in repr(settings)
    assert "hmac-secret" not in repr(settings)
    assert "bearer-secret" not in repr(settings)
    assert settings.ow_api_key.get_secret_value() == "super-secret-key"
    assert settings.hermes_webhook_secret.get_secret_value() == "hmac-secret"
    assert settings.api_token.get_secret_value() == "bearer-secret"


def test_host_and_api_token_from_env(monkeypatch) -> None:
    monkeypatch.setenv("HEALTHMES_HOST", "0.0.0.0")
    monkeypatch.setenv("HEALTHMES_API_TOKEN", "tok")

    settings = _clean_settings()

    assert settings.host == "0.0.0.0"
    assert settings.api_token.get_secret_value() == "tok"


def test_is_loopback_host() -> None:
    from healthmes.config import is_loopback_host

    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("127.0.0.2")
    assert is_loopback_host("::1")
    assert is_loopback_host("localhost")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("192.168.0.12")
    assert not is_loopback_host("my-laptop.local")


def test_resolve_timezone(monkeypatch) -> None:
    import zoneinfo

    from healthmes.config import resolve_timezone

    seoul = _clean_settings(timezone="Asia/Seoul")
    assert resolve_timezone(seoul) == zoneinfo.ZoneInfo("Asia/Seoul")

    machine = _clean_settings(timezone=None)
    assert resolve_timezone(machine) is not None  # machine-local tz

    broken = _clean_settings(timezone="Not/AZone")
    with pytest.raises(zoneinfo.ZoneInfoNotFoundError):
        resolve_timezone(broken)
