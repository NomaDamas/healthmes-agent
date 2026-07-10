"""Tests for the user-local timezone resolution of the tranche-2 tools.

Resolution order (server._local_timezone): explicit override ->
Settings.timezone (field pending in the shared config) -> HEALTHMES_TIMEZONE
env var -> the machine's local timezone. Misconfigured names fail loudly.
"""

import datetime as dt
import zoneinfo

import pytest
from fastmcp.exceptions import ToolError

from healthmes.mcp_server import server as server_module


class _StubSettings:
    """Just enough settings surface for _local_timezone."""

    def __init__(self, timezone: str | None) -> None:
        self.timezone = timezone


@pytest.fixture(autouse=True)
def _clean_runtime_state(monkeypatch):
    monkeypatch.delenv("HEALTHMES_TIMEZONE", raising=False)
    yield
    server_module.reset_runtime_state()


class TestLocalTimezoneResolution:
    def test_override_wins(self):
        pinned = dt.timezone(dt.timedelta(hours=9))
        server_module.set_timezone(pinned)
        assert server_module._local_timezone() is pinned

    def test_set_timezone_accepts_an_iana_name(self):
        server_module.set_timezone("Asia/Seoul")
        assert str(server_module._local_timezone()) == "Asia/Seoul"

    def test_settings_field_is_consulted(self):
        server_module.set_settings(_StubSettings("Asia/Seoul"))  # type: ignore[arg-type]
        resolved = server_module._local_timezone()
        assert isinstance(resolved, zoneinfo.ZoneInfo)
        assert str(resolved) == "Asia/Seoul"

    def test_env_var_fallback(self, monkeypatch):
        server_module.set_settings(_StubSettings(None))  # type: ignore[arg-type]
        monkeypatch.setenv("HEALTHMES_TIMEZONE", "Asia/Seoul")
        assert str(server_module._local_timezone()) == "Asia/Seoul"

    def test_invalid_name_fails_loudly_never_silent_utc(self):
        server_module.set_settings(_StubSettings("Mars/Olympus_Mons"))  # type: ignore[arg-type]
        with pytest.raises(ToolError, match="not a valid IANA name"):
            server_module._local_timezone()

    def test_system_local_is_the_last_resort(self):
        server_module.set_settings(_StubSettings(None))  # type: ignore[arg-type]
        resolved = server_module._local_timezone()
        assert isinstance(resolved, dt.tzinfo)
