"""Tests for the FastAPI app factory: composition-root wiring of all planes.

Covers the integration contracts recorded by each scope's manifest:
store engine bound to the app's Settings, REST routers + error envelope,
the MCP Streamable-HTTP endpoint at exactly ``/mcp`` (with its session-manager
lifespan running), the MCP settings override, and the APScheduler lifecycle
gated on ``Settings.scheduler_enabled``.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from healthmes import __version__
from healthmes.app import create_app
from healthmes.config import Settings
from healthmes.engine.scheduler import BACKUP_JOB_ID, ENERGY_JOB_ID, TRIGGER_JOB_ID
from healthmes.mcp_server import server as mcp_server
from healthmes.store import Base, get_engine
from healthmes.store import session as store_session

_MCP_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "smoke", "version": "0"},
    },
}
_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def test_create_app_returns_fastapi_with_settings_on_state(settings: Settings) -> None:
    app = create_app(settings)

    assert isinstance(app, FastAPI)
    assert app.state.settings is settings
    assert app.version == __version__


def test_health_endpoint_returns_ok(client) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_app_without_args_uses_env_settings(monkeypatch) -> None:
    monkeypatch.setenv("HEALTHMES_PORT", "9999")
    # get_settings() is cached; bypass the cache to observe the env.
    from healthmes.config import get_settings

    get_settings.cache_clear()
    try:
        app = create_app()
        assert app.state.settings.port == 9999
    finally:
        get_settings.cache_clear()


class TestStoreWiring:
    def test_lifespan_binds_engine_to_app_settings_and_serves_rest(self, settings) -> None:
        """init_engine(settings) runs at startup so SessionDep hits the app db."""
        app = create_app(settings)
        with TestClient(app) as client:
            engine = get_engine()  # initialised by the lifespan, not lazily
            assert str(engine.url) == settings.database_url
            Base.metadata.create_all(engine)

            created = client.post(
                "/v1/goals", json={"week_start": "2026-07-06", "title": "Integration"}
            )
            assert created.status_code == 201

            listed = client.get("/v1/goals")
            assert listed.status_code == 200
            assert [goal["title"] for goal in listed.json()["data"]] == ["Integration"]

        # Shutdown disposes the process-wide engine singleton.
        assert store_session._engine is None
        assert store_session._session_factory is None


class TestMcpWiring:
    def test_mcp_initialize_handshake_at_exactly_slash_mcp(self, settings) -> None:
        """The MCP session manager runs (chained lifespan) and serves POST /mcp."""
        app = create_app(settings)
        with TestClient(app) as client:
            response = client.post("/mcp", json=_MCP_INITIALIZE, headers=_MCP_HEADERS)

            assert response.status_code == 200
            assert response.headers.get("mcp-session-id")
            assert '"serverInfo"' in response.text
            assert '"healthmes"' in response.text

    def test_fastapi_routes_keep_precedence_and_404s_keep_the_envelope(self, settings) -> None:
        """/health & /v1 stay FastAPI-served; unknown paths keep the envelope."""
        app = create_app(settings)
        with TestClient(app) as client:
            assert client.get("/health").json() == {"status": "ok"}

            missing = client.get("/v1/nope")
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "not_found"

            # Sub-paths of /mcp are not MCP endpoints either.
            assert client.get("/mcp/nested").status_code == 404
            assert client.get("/mcp/nested").json()["error"]["code"] == "not_found"

    def test_lifespan_scopes_the_mcp_settings_override(self, settings) -> None:
        """MCP tools see the app's Settings while it runs, env settings after."""
        app = create_app(settings)
        with TestClient(app):
            assert mcp_server._active_settings() is settings
        assert mcp_server._settings_override is None


class TestSchedulerWiring:
    def test_disabled_scheduler_stays_off(self, settings) -> None:
        assert settings.scheduler_enabled is False
        app = create_app(settings)
        with TestClient(app):
            assert app.state.scheduler is None

    def test_enabled_scheduler_starts_with_all_jobs_and_stops(self, settings) -> None:
        """The lifespan registers all three background jobs before start:
        the 10-minute trigger sweep, the hourly cognitive-energy persist and
        the weekly backup (energy + backup manifests' app_wiring needs)."""
        enabled = settings.model_copy(update={"scheduler_enabled": True})
        app = create_app(enabled)
        with TestClient(app):
            scheduler = app.state.scheduler
            assert scheduler is not None
            assert scheduler.running
            job_ids = {job.id for job in scheduler.get_jobs()}
            assert job_ids == {TRIGGER_JOB_ID, ENERGY_JOB_ID, BACKUP_JOB_ID}
        assert not scheduler.running
        assert app.state.scheduler is None

    def test_enabled_calendar_flags_register_poll_jobs(self, settings) -> None:
        """PLAN §6 wiring: the settings flags actually create the calendar
        mirror poll jobs (previously dead code — the flags gated nothing)."""
        from healthmes.calendars.jobs import calendar_job_id
        from healthmes.store import CalendarSource

        enabled = settings.model_copy(
            update={
                "scheduler_enabled": True,
                "google_calendar_enabled": True,
                "caldav_enabled": True,
            }
        )
        app = create_app(enabled)
        with TestClient(app):
            scheduler = app.state.scheduler
            assert scheduler is not None
            job_ids = {job.id for job in scheduler.get_jobs()}
            assert job_ids == {
                TRIGGER_JOB_ID,
                ENERGY_JOB_ID,
                BACKUP_JOB_ID,
                calendar_job_id(CalendarSource.GOOGLE),
                calendar_job_id(CalendarSource.CALDAV),
            }
            google_job = scheduler.get_job(calendar_job_id(CalendarSource.GOOGLE))
            caldav_job = scheduler.get_job(calendar_job_id(CalendarSource.CALDAV))
            assert google_job.trigger.interval.total_seconds() == 5 * 60
            assert caldav_job.trigger.interval.total_seconds() == 10 * 60

    def test_disabled_scheduler_still_wires_jobs_without_starting(
        self, settings, monkeypatch
    ) -> None:
        """Job registration happens on the (unstarted) scheduler either way;
        only start_scheduler is gated on settings.scheduler_enabled."""
        import healthmes.app as app_module

        captured: dict[str, object] = {}
        real_start = app_module.start_scheduler

        def spying_start(settings_arg, *, scheduler=None):
            captured["scheduler"] = scheduler
            return real_start(settings_arg, scheduler=scheduler)

        monkeypatch.setattr(app_module, "start_scheduler", spying_start)
        app = create_app(settings)  # scheduler_enabled=False
        with TestClient(app):
            assert app.state.scheduler is None  # gate held
        prepared = captured["scheduler"]
        assert prepared is not None
        assert {job.id for job in prepared.get_jobs()} == {
            TRIGGER_JOB_ID,
            ENERGY_JOB_ID,
            BACKUP_JOB_ID,
        }
        assert not prepared.running
