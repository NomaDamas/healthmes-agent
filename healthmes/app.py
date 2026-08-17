"""FastAPI application factory for the HealthMes service.

This is the single composition root (docs/PLAN.md §1): it binds together the
domain store, the REST surface, the Layer-B MCP server (Streamable HTTP at
exactly ``/mcp`` — the URL Hermes registers per vendor/hermes-agent/tools/
mcp_tool.py), and the in-process APScheduler loops.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from threading import Event

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
    HealthMesDecisionService,
    build_configured_decision_engine,
    build_decision_context_search_session_service,
    build_decision_recovery_finalizer,
    ensure_decision_domain_policies,
)
from healthmes.decision.domain_providers import WearableReader
from healthmes.decision.responses import HermesResponsesTransport
from healthmes.engine.cognitive_energy import build_energy_job
from healthmes.engine.decision_dispatch import (
    DecisionAlertSender,
    DecisionServiceThreadBridge,
)
from healthmes.engine.scheduler import (
    create_scheduler,
    register_activitywatch_job,
    register_backup_job,
    register_calendar_adjustment_maintenance_job,
    register_calendar_job,
    register_energy_job,
    register_scheduled_briefing_jobs,
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
from healthmes.store.decision_receipts import (
    DEFAULT_RECEIPT_MAINTENANCE_BATCH_SIZE,
    DEFAULT_RECEIPT_MAINTENANCE_MAX_ROWS,
    DecisionReceiptMaintenanceBatch,
    maintain_decision_receipt_results,
)
from healthmes.timing import steady_time

_LOGGER = logging.getLogger(__name__)
_DECISION_RECEIPT_MAINTENANCE_INTERVAL_SECONDS = 30.0
_DECISION_RECEIPT_MAINTENANCE_TIMEOUT_SECONDS = 5.0
_DECISION_RECEIPT_MAINTENANCE_CANCEL_GRACE_SECONDS = 1.0


class _DecisionReceiptMaintenanceCancelled(RuntimeError):
    """Raised by a worker after its owning async task requests cancellation."""


def _receipt_maintenance_cancellation_check(
    cancellation: Event | None,
) -> None:
    if cancellation is not None and cancellation.is_set():
        raise _DecisionReceiptMaintenanceCancelled


def _receipt_maintenance_remaining(deadline: float) -> float:
    remaining = deadline - steady_time()
    if remaining <= 0:
        raise TimeoutError(
            "timed out waiting for decision receipt maintenance"
        )
    return remaining


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


def _run_mandatory_decision_receipt_maintenance(
    *,
    now: datetime | None = None,
    batch_size: int = DEFAULT_RECEIPT_MAINTENANCE_BATCH_SIZE,
    max_rows: int = DEFAULT_RECEIPT_MAINTENANCE_MAX_ROWS,
    after_id: uuid.UUID | None = None,
    timeout_seconds: float = (
        _DECISION_RECEIPT_MAINTENANCE_TIMEOUT_SECONDS
    ),
    cancellation: Event | None = None,
) -> tuple[int, uuid.UUID | None]:
    """Commit bounded cleanup batches independently from optional scheduling."""

    if batch_size < 1:
        raise ValueError("decision receipt batch_size must be positive")
    if max_rows < 1:
        raise ValueError("decision receipt max_rows must be positive")
    if timeout_seconds <= 0:
        raise ValueError(
            "decision receipt maintenance timeout must be positive"
        )
    current = now or datetime.now(UTC)
    processed = 0
    cursor = after_id
    deadline = steady_time() + timeout_seconds

    def cancellation_check() -> None:
        _receipt_maintenance_cancellation_check(cancellation)

    while processed < max_rows:
        cancellation_check()
        current_batch_size = min(batch_size, max_rows - processed)
        with activity_write_lock(
            timeout_seconds=_receipt_maintenance_remaining(deadline),
            cancellation_check=cancellation_check,
        ):
            with session_scope() as session:
                lock_activity_write_plane(
                    session,
                    timeout_seconds=_receipt_maintenance_remaining(
                        deadline
                    ),
                    cancellation_check=cancellation_check,
                )
                batch = maintain_decision_receipt_results(
                    session,
                    now=current,
                    batch_size=current_batch_size,
                    after_id=cursor,
                )
        processed += batch.scanned
        cursor = batch.next_cursor
        if cursor is None:
            break
    return processed, cursor


def _run_one_decision_receipt_maintenance_batch(
    *,
    now: datetime | None = None,
    after_id: uuid.UUID | None = None,
    timeout_seconds: float = (
        _DECISION_RECEIPT_MAINTENANCE_TIMEOUT_SECONDS
    ),
    cancellation: Event | None = None,
) -> DecisionReceiptMaintenanceBatch:
    """Run one short recurring transaction so normal writes regain the lock."""

    if timeout_seconds <= 0:
        raise ValueError(
            "decision receipt maintenance timeout must be positive"
        )
    deadline = steady_time() + timeout_seconds

    def cancellation_check() -> None:
        _receipt_maintenance_cancellation_check(cancellation)

    with activity_write_lock(
        timeout_seconds=_receipt_maintenance_remaining(deadline),
        cancellation_check=cancellation_check,
    ):
        with session_scope() as session:
            lock_activity_write_plane(
                session,
                timeout_seconds=_receipt_maintenance_remaining(deadline),
                cancellation_check=cancellation_check,
            )
            return maintain_decision_receipt_results(
                session,
                now=now or datetime.now(UTC),
                batch_size=(
                    DEFAULT_RECEIPT_MAINTENANCE_BATCH_SIZE
                ),
                after_id=after_id,
            )


async def _run_receipt_maintenance_durably(
    *,
    after_id: uuid.UUID | None = None,
    timeout_seconds: float = (
        _DECISION_RECEIPT_MAINTENANCE_TIMEOUT_SECONDS
    ),
) -> DecisionReceiptMaintenanceBatch:
    if timeout_seconds <= 0:
        raise ValueError(
            "decision receipt maintenance timeout must be positive"
        )
    cancellation = Event()
    worker = asyncio.create_task(
        asyncio.to_thread(
            _run_one_decision_receipt_maintenance_batch,
            after_id=after_id,
            timeout_seconds=timeout_seconds,
            cancellation=cancellation,
        )
    )
    try:
        return await asyncio.wait_for(
            asyncio.shield(worker),
            timeout=(
                timeout_seconds
                + _DECISION_RECEIPT_MAINTENANCE_CANCEL_GRACE_SECONDS
            ),
        )
    except asyncio.CancelledError:
        cancellation.set()
        try:
            await asyncio.wait_for(
                asyncio.shield(worker),
                timeout=(
                    _DECISION_RECEIPT_MAINTENANCE_CANCEL_GRACE_SECONDS
                ),
            )
        except (
            TimeoutError,
            _DecisionReceiptMaintenanceCancelled,
        ):
            pass
        raise
    except TimeoutError:
        cancellation.set()
        try:
            await asyncio.wait_for(
                asyncio.shield(worker),
                timeout=(
                    _DECISION_RECEIPT_MAINTENANCE_CANCEL_GRACE_SECONDS
                ),
            )
        except (
            TimeoutError,
            _DecisionReceiptMaintenanceCancelled,
        ):
            pass
        raise TimeoutError(
            "decision receipt maintenance exceeded its deadline"
        ) from None
    except _DecisionReceiptMaintenanceCancelled:
        raise asyncio.CancelledError from None


async def _decision_receipt_maintenance_loop(
    stop: asyncio.Event,
    *,
    initial_cursor: uuid.UUID | None = None,
) -> None:
    cursor = initial_cursor
    while not stop.is_set():
        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=(
                    _DECISION_RECEIPT_MAINTENANCE_INTERVAL_SECONDS
                ),
            )
        except TimeoutError:
            try:
                batch = await _run_receipt_maintenance_durably(
                    after_id=cursor,
                )
                cursor = batch.next_cursor
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception(
                    "mandatory decision receipt maintenance failed"
                )


async def _stop_decision_receipt_maintenance(
    task: asyncio.Task[None],
    stop: asyncio.Event,
    *,
    timeout_seconds: float = (
        _DECISION_RECEIPT_MAINTENANCE_TIMEOUT_SECONDS
        + _DECISION_RECEIPT_MAINTENANCE_CANCEL_GRACE_SECONDS
    ),
) -> None:
    """Stop the loop and attempt one final scrub within bounded deadlines."""

    if timeout_seconds <= 0:
        raise ValueError(
            "decision receipt shutdown timeout must be positive"
        )
    stop.set()
    cancellation: asyncio.CancelledError | None = None
    current = asyncio.current_task()
    cancelling_before = current.cancelling() if current is not None else 0
    try:
        await asyncio.wait_for(
            asyncio.shield(task),
            timeout=timeout_seconds,
        )
    except asyncio.CancelledError as exc:
        cancelling_after = current.cancelling() if current is not None else 0
        if current is None or cancelling_after <= cancelling_before:
            raise
        cancellation = exc
    except TimeoutError:
        task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=(
                    _DECISION_RECEIPT_MAINTENANCE_CANCEL_GRACE_SECONDS
                ),
            )
        except (TimeoutError, asyncio.CancelledError):
            pass
        _LOGGER.warning(
            "decision receipt maintenance loop exceeded shutdown deadline"
        )

    try:
        await _run_receipt_maintenance_durably(
            timeout_seconds=min(
                timeout_seconds,
                _DECISION_RECEIPT_MAINTENANCE_TIMEOUT_SECONDS,
            )
        )
    except TimeoutError:
        _LOGGER.warning(
            "final decision receipt maintenance exceeded shutdown deadline"
        )
    except asyncio.CancelledError as exc:
        cancellation = exc
    if cancellation is not None:
        raise cancellation


def create_app(
    settings: Settings | None = None,
    *,
    decision_transport: HermesResponsesTransport | None = None,
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
        app.state.decision_recovery_finalizer = None
        app.state.decision_receipt_maintenance_task = None
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
            _, receipt_maintenance_cursor = (
                _run_mandatory_decision_receipt_maintenance()
            )

            receipt_maintenance_stop = asyncio.Event()
            receipt_maintenance_task = asyncio.create_task(
                _decision_receipt_maintenance_loop(
                    receipt_maintenance_stop,
                    initial_cursor=receipt_maintenance_cursor,
                ),
                name="healthmes-decision-receipt-maintenance",
            )
            app.state.decision_receipt_maintenance_task = (
                receipt_maintenance_task
            )

            async def stop_receipt_maintenance() -> None:
                try:
                    await _stop_decision_receipt_maintenance(
                        receipt_maintenance_task,
                        receipt_maintenance_stop,
                    )
                finally:
                    app.state.decision_receipt_maintenance_task = (
                        None
                    )

            cleanup.push_async_callback(stop_receipt_maintenance)

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
            decision_search_service = (
                build_decision_context_search_session_service(
                    settings=settings,
                    session_factory=get_session_factory(),
                    wearable_reader=wearable_reader,
                    clock=decision_clock,
                )
            )
            mcp_server.set_decision_search_session_service(
                decision_search_service
            )
            decision_recovery_finalizer = (
                build_decision_recovery_finalizer(
                    settings=settings,
                    session_factory=get_session_factory(),
                    search_service=decision_search_service,
                    clock=decision_clock,
                )
            )
            app.state.decision_recovery_finalizer = (
                decision_recovery_finalizer
            )
            if decision_recovery_finalizer is not None:

                async def close_decision_recovery_finalizer() -> None:
                    try:
                        await decision_recovery_finalizer.aclose()
                    finally:
                        app.state.decision_recovery_finalizer = None

                cleanup.push_async_callback(
                    close_decision_recovery_finalizer
                )
            decision_engine = build_configured_decision_engine(
                settings=settings,
                session_factory=get_session_factory(),
                transport=decision_transport,
                search_service=decision_search_service,
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

            decision_alert_sender = None
            if decision_engine is not None:
                decision_alert_sender = DecisionAlertSender(
                    settings,
                    bridge=DecisionServiceThreadBridge(
                        service=app.state.decision_service,
                        loop=asyncio.get_running_loop(),
                        timeout_seconds=(
                            settings.decision_timeout_seconds
                            + settings.decision_finalization_timeout_seconds
                        ),
                    ),
                )

            # Background loops are prepared even when globally disabled so
            # their configuration remains testable. Register shutdown before
            # job setup so a partial scheduler startup cannot leak a thread.
            scheduler = create_scheduler(
                settings,
                alert_sender=decision_alert_sender,
            )

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
            if decision_alert_sender is not None:
                register_scheduled_briefing_jobs(
                    scheduler,
                    settings,
                    alert_sender=decision_alert_sender,
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
    app.state.decision_recovery_finalizer = None
    app.state.decision_service = HealthMesDecisionService(
        settings=settings,
        engine_provider=lambda: app.state.decision_engine,
        session_factory_provider=get_session_factory,
        recovery_provider=lambda: (
            app.state.decision_recovery_finalizer
        ),
        clock=decision_clock,
    )
    app.state.scheduler = None
    app.state.decision_receipt_maintenance_task = None
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
