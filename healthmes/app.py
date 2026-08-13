"""FastAPI application factory for the HealthMes service.

This is the single composition root (docs/PLAN.md §1): it binds together the
domain store, the REST surface, the Layer-B MCP server (Streamable HTTP at
exactly ``/mcp`` — the URL Hermes registers per vendor/hermes-agent/tools/
mcp_tool.py), and the in-process APScheduler loops.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from sqlalchemy.orm import Session
from starlette.types import Receive, Scope, Send

from healthmes import __version__
from healthmes.activity import api as activity_api
from healthmes.activity.aggregation import (
    migrate_activity_summary_derivations,
)
from healthmes.activity.android import backfill_android_canonical_events
from healthmes.activity.jobs import build_activitywatch_job
from healthmes.activity.locking import (
    activity_write_lock,
    lock_activity_write_plane,
)
from healthmes.activity.maintenance import (
    build_activity_maintenance_job,
    register_activity_maintenance_job,
)
from healthmes.api import include_all
from healthmes.api.auth import install_auth
from healthmes.api.google_oauth import install_google_oauth
from healthmes.api.local_session import install_local_sessions
from healthmes.backup.local import build_backup_job
from healthmes.calendars.jobs import build_calendar_jobs
from healthmes.calendars.sleep_job import build_sleep_reconciliation_job
from healthmes.config import Settings, get_settings, resolve_timezone
from healthmes.decision import (
    build_configured_decision_engine,
    ensure_decision_domain_policies,
)
from healthmes.decision.domain_providers import WearableReader
from healthmes.decision.hermes import HermesIterationTransport
from healthmes.engine.cognitive_energy import build_energy_job
from healthmes.engine.scheduler import (
    create_scheduler,
    register_activitywatch_job,
    register_backup_job,
    register_calendar_adjustment_maintenance_job,
    register_calendar_job,
    register_energy_job,
    register_sleep_reconciliation_job,
    register_storage_maintenance_job,
    shutdown_scheduler,
    start_scheduler,
)
from healthmes.mcp_server import server as mcp_server
from healthmes.storage import build_storage_maintenance_job
from healthmes.store import (
    Base,
    dispose_engine,
    get_session_factory,
    init_engine,
    session_scope,
)


async def _close_decision_engine_durably(decision_engine) -> None:
    """Drain accepted decisions before propagating external cancellation."""

    cancelled: asyncio.CancelledError | None = None
    current = asyncio.current_task()
    while True:
        cancelling_before = (
            current.cancelling() if current is not None else 0
        )
        try:
            await decision_engine.aclose()
            break
        except asyncio.CancelledError as exc:
            cancelling_after = (
                current.cancelling() if current is not None else 0
            )
            if current is None or cancelling_after <= cancelling_before:
                raise
            cancelled = exc
    if cancelled is not None:
        raise cancelled


def _initialize_activity_storage(
    session: Session,
    *,
    timezone: str,
    decision_owner_principal_id: str,
) -> None:
    """Bootstrap local data policies under the global write-plane lock."""
    with activity_write_lock():
        lock_activity_write_plane(session)
        backfill_android_canonical_events(
            session,
            timezone=timezone,
        )
        migrate_activity_summary_derivations(session)
        ensure_decision_domain_policies(
            session,
            decision_owner_principal_id,
        )


def create_app(
    settings: Settings | None = None,
    *,
    decision_transport: HermesIterationTransport | None = None,
    decision_wearable_reader: WearableReader | None = None,
    decision_clock: Callable[[], datetime] | None = None,
) -> FastAPI:
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
        app.state.scheduler = None
        app.state.decision_engine = None
        async with AsyncExitStack() as cleanup:
            # Register each cleanup immediately after ownership is acquired.
            # LIFO then guarantees scheduler -> decisions -> MCP -> DB even
            # when a later startup step fails.
            engine = init_engine(settings)
            cleanup.callback(dispose_engine)
            if engine.dialect.name == "sqlite":
                Base.metadata.create_all(engine)
            with session_scope() as session:
                _initialize_activity_storage(
                    session,
                    timezone=str(resolve_timezone(settings)),
                    decision_owner_principal_id=(
                        settings.decision_owner_principal_id
                    ),
                )

            mcp_server.set_settings(settings)
            cleanup.callback(mcp_server.set_settings, None)
            await cleanup.enter_async_context(mcp_app.lifespan(mcp_app))

            wearable_reader = (
                decision_wearable_reader
                if decision_wearable_reader is not None
                else lambda day: mcp_server.get_daily_readiness_context(
                    day.isoformat()
                )
            )
            decision_engine = build_configured_decision_engine(
                settings=settings,
                session_factory=get_session_factory(),
                transport=decision_transport,
                wearable_reader=wearable_reader,
                clock=decision_clock,
            )
            app.state.decision_engine = decision_engine
            if decision_engine is not None:

                async def close_decision_engine() -> None:
                    try:
                        await _close_decision_engine_durably(
                            decision_engine
                        )
                    finally:
                        app.state.decision_engine = None

                cleanup.push_async_callback(close_decision_engine)

            # Background loops are prepared even when globally disabled so
            # their configuration remains testable. Register shutdown before
            # job setup so a partial scheduler startup cannot leak a thread.
            scheduler = create_scheduler(settings)

            def stop_scheduler() -> None:
                try:
                    shutdown_scheduler(scheduler)
                finally:
                    app.state.scheduler = None

            cleanup.callback(stop_scheduler)
            register_energy_job(scheduler, build_energy_job(settings))
            register_backup_job(scheduler, build_backup_job(settings))
            register_storage_maintenance_job(
                scheduler,
                build_storage_maintenance_job(settings),
            )
            register_activity_maintenance_job(
                scheduler,
                build_activity_maintenance_job(),
            )
            activitywatch_job = build_activitywatch_job(settings)
            if activitywatch_job is not None:
                register_activitywatch_job(
                    scheduler,
                    activitywatch_job,
                    minutes=settings.activitywatch_interval_minutes,
                )
            register_calendar_adjustment_maintenance_job(
                scheduler,
                mcp_server.expire_and_reconcile_calendar_adjustments,
            )
            for spec in build_calendar_jobs(settings):
                register_calendar_job(
                    scheduler,
                    spec.job,
                    job_id=spec.job_id,
                    minutes=spec.interval_minutes,
                )
            sleep_job = build_sleep_reconciliation_job(settings)
            if sleep_job is not None:
                register_sleep_reconciliation_job(scheduler, sleep_job)
            app.state.scheduler = start_scheduler(
                settings,
                scheduler=scheduler,
            )
            yield

    app = FastAPI(
        title="HealthMes Agent",
        version=__version__,
        description="Health-aware proactive assistant service "
        "(domain store, engines, calendar sync, MCP tools).",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.decision_clock = decision_clock
    app.state.decision_engine = None
    app.state.scheduler = None
    install_local_sessions(app)
    install_google_oauth(app)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness probe used by docker-compose and smoke tests."""
        return {"status": "ok"}

    # REST surface: error-envelope handlers + every /v1 router + the decision
    # viewer page (idempotent — test fixtures may call it again).
    include_all(app)
    app.include_router(activity_api.router)

    # Bearer-token gate over the whole surface — REST, viewer pages AND /mcp
    # (middleware wraps the router, so the /mcp default-handler dispatch below
    # is covered too). No-op when Settings.api_token is empty; the serve
    # entrypoint also refuses non-loopback binds; the tokenless middleware
    # independently checks the actual socket peer so direct factory execution
    # cannot bypass the local-only boundary.
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
