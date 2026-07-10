"""FastAPI application factory for the HealthMes service.

This is the single composition root (docs/PLAN.md §1): it binds together the
domain store, the REST surface, the Layer-B MCP server (Streamable HTTP at
exactly ``/mcp`` — the URL Hermes registers per vendor/hermes-agent/tools/
mcp_tool.py), and the in-process APScheduler loops.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.types import Receive, Scope, Send

from healthmes import __version__
from healthmes.api import include_all
from healthmes.api.auth import install_auth
from healthmes.backup.local import build_backup_job
from healthmes.calendars.jobs import build_calendar_jobs
from healthmes.config import Settings, get_settings
from healthmes.engine.cognitive_energy import build_energy_job
from healthmes.engine.scheduler import (
    create_scheduler,
    register_backup_job,
    register_calendar_job,
    register_energy_job,
    shutdown_scheduler,
    start_scheduler,
)
from healthmes.mcp_server import server as mcp_server
from healthmes.store import dispose_engine, init_engine


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the HealthMes FastAPI application.

    Feature layers (store, engine, calendars, mcp_server, api) are wired here;
    keep the factory the single composition root. Passing ``settings``
    explicitly is intended for tests; by default the env-derived singleton is
    used.
    """
    settings = settings if settings is not None else get_settings()

    # Built before the lifespan closure: the MCP session manager lives on this
    # ASGI app and its lifespan must run inside the FastAPI lifespan (without
    # it every /mcp request 500s).
    mcp_app = mcp_server.build_mcp_http_app()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Bind the process-wide store engine to *this* app's settings so
        # SessionDep / session_scope() hit the configured database instead of
        # lazily reading the environment at first use.
        init_engine(settings)
        # MCP tools resolve settings through the same override hook the tests
        # use, so tools always agree with the app about endpoints/keys.
        mcp_server.set_settings(settings)
        # Background loops: the 10-minute trigger sweep (created with the
        # scheduler), the hourly cognitive-energy persist (PLAN §3), the
        # weekly encrypted backup (PLAN §9), and one calendar mirror poll per
        # enabled backend (PLAN §6 — Google 5 min / CalDAV 10 min; the write
        # backend also pushes accepted proposals to the external calendar).
        # All of them only ever run when settings.scheduler_enabled is True —
        # start_scheduler returns None otherwise (the gate lives inside it).
        scheduler = create_scheduler(settings)
        register_energy_job(scheduler, build_energy_job(settings))
        register_backup_job(scheduler, build_backup_job(settings))
        for spec in build_calendar_jobs(settings):
            register_calendar_job(
                scheduler, spec.job, job_id=spec.job_id, minutes=spec.interval_minutes
            )
        app.state.scheduler = start_scheduler(settings, scheduler=scheduler)
        try:
            # Chain the MCP app's lifespan: it starts the StreamableHTTP
            # session manager serving POST /mcp.
            async with mcp_app.lifespan(mcp_app):
                yield
        finally:
            shutdown_scheduler(app.state.scheduler)
            app.state.scheduler = None
            mcp_server.set_settings(None)
            dispose_engine()

    app = FastAPI(
        title="HealthMes Agent",
        version=__version__,
        description="Health-aware proactive assistant service "
        "(domain store, engines, calendar sync, MCP tools).",
        lifespan=lifespan,
    )
    app.state.settings = settings

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness probe used by docker-compose and smoke tests."""
        return {"status": "ok"}

    # REST surface: error-envelope handlers + every /v1 router + the decision
    # viewer page (idempotent — test fixtures may call it again).
    include_all(app)

    # Bearer-token gate over the whole surface — REST, viewer pages AND /mcp
    # (middleware wraps the router, so the /mcp default-handler dispatch below
    # is covered too). No-op when Settings.api_token is empty; the serve
    # entrypoint then refuses non-loopback binds (healthmes/__main__.py).
    install_auth(app, settings)

    # Serve the MCP app from the router's *default* handler (the last resort
    # invoked only when no FastAPI route matched). The bare-mount recipe
    # (`app.mount("", mcp_app)`, proven by tests/mcp_server/test_server_app.py)
    # keeps the endpoint at exactly /mcp too, but it also swallows every
    # unmatched path, downgrading REST 404s from the JSON error envelope
    # (healthmes/api/errors.py) to the sub-app's plain-text 404. Dispatching
    # here keeps all three contracts: FastAPI routes keep precedence, the MCP
    # endpoint stays at exactly /mcp (no redirect — the URL Hermes registers),
    # and unknown paths still raise through the installed envelope handlers.
    fastapi_default = app.router.default

    async def _default_with_mcp(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") == "/mcp":
            await mcp_app(scope, receive, send)
            return
        await fastapi_default(scope, receive, send)

    app.router.default = _default_with_mcp

    return app
