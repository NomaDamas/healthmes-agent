"""Shared test fixtures for the HealthMes test suite.

Tests must never need network, Docker, or real credentials: the ``settings``
fixture points at in-memory sqlite and dummy endpoints, and disables both
``.env`` loading and the scheduler.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from healthmes.app import create_app
from healthmes.config import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Fully-explicit Settings for tests.

    Every field is passed as an init kwarg (highest pydantic-settings source
    priority) and ``_env_file=None`` disables .env loading, so neither the
    developer's environment nor a local .env can leak into tests.
    """
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        ow_base_url="http://open-wearables.test",
        ow_api_key="test-ow-api-key",
        ow_user_id=None,
        hermes_webhook_url="http://hermes.test:8644/webhooks/healthmes-alerts",
        hermes_webhook_secret="test-webhook-secret",
        public_base_url="http://healthmes.test:8100",
        data_dir=tmp_path / "data",
        port=8100,
        host="127.0.0.1",
        api_token="",
        scheduler_enabled=False,
        # Pinned so local-time semantics (insight bucketing, alert hygiene)
        # are deterministic on any machine; tz-specific tests override it.
        timezone="UTC",
        quiet_hours_start="22:30",
        quiet_hours_end="07:00",
        alert_daily_budget=8,
        alert_cooldown_minutes=60,
        google_calendar_enabled=False,
        google_calendar_id="primary",
        google_poll_minutes=5,
        caldav_enabled=False,
        caldav_url="https://caldav.test",
        caldav_username="",
        caldav_app_password="",
        caldav_calendar_name=None,
        caldav_poll_minutes=10,
        _env_file=None,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """A HealthMes app built from the test settings."""
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI):
    """TestClient running the app's lifespan (startup/shutdown) events."""
    with TestClient(app) as test_client:
        yield test_client
