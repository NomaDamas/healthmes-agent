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
        # Defect 2: never a captured fixed offset -- a real DST-aware IANA zone.
        assert isinstance(resolved, zoneinfo.ZoneInfo)


class TestSystemTimezone:
    """Defect 2: the machine fallback resolves a real IANA zone, never a fixed
    offset captured at process start (which would lose DST for off-season
    queries)."""

    def test_returns_real_iana_zone_not_fixed_offset(self):
        from healthmes.config import system_timezone

        tz = system_timezone()
        assert isinstance(tz, zoneinfo.ZoneInfo)
        assert not isinstance(tz, dt.timezone)  # never datetime.now().astimezone() offset

    def test_falls_back_to_localtime_symlink_when_tzlocal_fails(self, monkeypatch):
        import tzlocal

        from healthmes.config import system_timezone

        def _boom():
            raise RuntimeError("tzlocal unavailable")

        monkeypatch.setattr(tzlocal, "get_localzone", _boom)
        tz = system_timezone()
        # Resolved from the /etc/localtime symlink (Asia/Seoul on this machine);
        # tolerate UTC on hosts without a zoneinfo symlink -- either way never a
        # captured fixed local offset.
        assert isinstance(tz, zoneinfo.ZoneInfo) or tz == dt.UTC

    def test_falls_back_to_utc_when_everything_fails(self, monkeypatch):
        import tzlocal

        from healthmes import config
        from healthmes.config import system_timezone

        def _boom():
            raise RuntimeError("tzlocal unavailable")

        monkeypatch.setattr(tzlocal, "get_localzone", _boom)

        class _BoomPath:
            def __init__(self, *args):
                pass

            def resolve(self, *args, **kwargs):
                raise OSError("no /etc/localtime")

        monkeypatch.setattr(config, "Path", _BoomPath)
        assert system_timezone() == dt.UTC

