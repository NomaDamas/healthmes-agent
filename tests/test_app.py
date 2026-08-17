"""Tests for the FastAPI app factory: composition-root wiring of all planes.

Covers the integration contracts recorded by each scope's manifest:
store engine bound to the app's Settings, REST routers + error envelope,
the MCP Streamable-HTTP endpoint at exactly ``/mcp`` (with its session-manager
lifespan running), the MCP settings override, and the APScheduler lifecycle
gated on ``Settings.scheduler_enabled``.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes import __version__
from healthmes.activity.maintenance import ACTIVITY_MAINTENANCE_JOB_ID
from healthmes.app import (
    _run_mandatory_decision_receipt_maintenance,
    create_app,
)
from healthmes.config import Settings
from healthmes.engine.scheduler import (
    ACTIVITYWATCH_JOB_ID,
    BACKUP_JOB_ID,
    CALENDAR_ADJUSTMENT_MAINTENANCE_JOB_ID,
    ENERGY_JOB_ID,
    SLEEP_RECONCILIATION_JOB_ID,
    STORAGE_MAINTENANCE_JOB_ID,
    TRIGGER_JOB_ID,
)
from healthmes.mcp_server import server as mcp_server
from healthmes.store import (
    Base,
    DecisionRequestReceipt,
    create_db_engine,
    dispose_engine,
    get_engine,
    init_engine,
)
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


def _completed_receipt(
    *,
    now: datetime,
    payload: dict,
    retention_basis_at: datetime | None = None,
    result_expires_at: datetime | None = None,
) -> DecisionRequestReceipt:
    basis = retention_basis_at or now
    return DecisionRequestReceipt(
        request_id=uuid.uuid4(),
        request_fingerprint=uuid.uuid4().hex * 2,
        requested_at=now,
        state="completed",
        result_payload=payload,
        result_expires_at=(
            result_expires_at or now + timedelta(days=30)
        ),
        retention_basis_at=basis,
        expires_at=now + timedelta(days=30),
    )


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
        with TestClient(
            app,
            base_url="http://127.0.0.1:8100",
            client=("127.0.0.1", 43123),
        ) as client:
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

    def test_startup_failure_releases_mcp_settings_and_database(
        self,
        settings,
        monkeypatch,
    ) -> None:
        """A later composition failure must not leak process-global state."""
        import healthmes.app as app_module

        lifecycle = {"entered": False, "exited": False}

        class StubMcpApp:
            @asynccontextmanager
            async def lifespan(
                self,
                _app,
            ) -> AsyncIterator[None]:
                lifecycle["entered"] = True
                try:
                    yield
                finally:
                    lifecycle["exited"] = True

            async def __call__(self, _scope, _receive, _send) -> None:
                raise AssertionError("MCP request dispatch was not expected")

        def fail_decision_engine(**_kwargs):
            raise RuntimeError("decision composition failed")

        monkeypatch.setattr(
            app_module.mcp_server,
            "build_mcp_http_app",
            lambda: StubMcpApp(),
        )
        monkeypatch.setattr(
            app_module,
            "build_configured_decision_engine",
            fail_decision_engine,
        )

        app = create_app(settings)
        with pytest.raises(
            RuntimeError,
            match="decision composition failed",
        ):
            with TestClient(app):
                pass

        assert lifecycle == {"entered": True, "exited": True}
        assert app.state.decision_engine is None
        assert app.state.scheduler is None
        assert mcp_server._settings_override is None
        assert store_session._engine is None
        assert store_session._session_factory is None

    def test_shutdown_closes_decisions_before_mcp_and_database(
        self,
        settings,
        monkeypatch,
    ) -> None:
        """Accepted decisions retain their dependencies during shutdown."""
        import healthmes.app as app_module

        observed: dict[str, object] = {}

        class StubDecisionEngine:
            async def aclose(self) -> None:
                observed["engine_open"] = store_session._engine is not None
                observed["mcp_settings"] = mcp_server._settings_override

        engine = StubDecisionEngine()
        monkeypatch.setattr(
            app_module,
            "build_configured_decision_engine",
            lambda **_kwargs: engine,
        )

        app = create_app(settings)
        with TestClient(app):
            assert app.state.decision_engine is engine

        assert observed == {
            "engine_open": True,
            "mcp_settings": settings,
        }
        assert app.state.decision_engine is None
        assert mcp_server._settings_override is None
        assert store_session._engine is None

    def test_health_and_mcp_start_before_optional_decision_runtime(
        self,
        settings,
        monkeypatch,
    ) -> None:
        """Core HTTP and MCP must not wait for the optional Hermes runtime."""
        import healthmes.app as app_module

        class DeferredDecisionEngine:
            def __init__(self) -> None:
                self.start_calls = 0
                self.close_calls = 0

            async def astart(self) -> None:
                self.start_calls += 1
                raise AssertionError(
                    "the optional decision runtime must start lazily"
                )

            async def aclose(self) -> None:
                self.close_calls += 1

        engine = DeferredDecisionEngine()
        monkeypatch.setattr(
            app_module,
            "build_configured_decision_engine",
            lambda **_kwargs: engine,
        )

        app = create_app(settings)
        with TestClient(
            app,
            base_url="http://127.0.0.1:8100",
            client=("127.0.0.1", 43123),
        ) as client:
            assert client.get("/health").json() == {"status": "ok"}
            response = client.post(
                "/mcp",
                json=_MCP_INITIALIZE,
                headers=_MCP_HEADERS,
            )
            assert response.status_code == 200
            assert app.state.decision_engine is engine
            assert engine.start_calls == 0

        assert engine.close_calls == 1

    def test_scheduler_setup_failure_closes_decisions_mcp_and_database(
        self,
        settings,
        monkeypatch,
    ) -> None:
        """A failure after Decision Engine creation releases every dependency."""
        import healthmes.app as app_module

        observed: list[str] = []

        class StubMcpApp:
            @asynccontextmanager
            async def lifespan(
                self,
                _app,
            ) -> AsyncIterator[None]:
                observed.append("mcp_enter")
                try:
                    yield
                finally:
                    observed.append("mcp_exit")

            async def __call__(self, _scope, _receive, _send) -> None:
                raise AssertionError("MCP request dispatch was not expected")

        class StubDecisionEngine:
            async def aclose(self) -> None:
                observed.append("decision")

        monkeypatch.setattr(
            app_module.mcp_server,
            "build_mcp_http_app",
            lambda: StubMcpApp(),
        )
        monkeypatch.setattr(
            app_module,
            "build_configured_decision_engine",
            lambda **_kwargs: StubDecisionEngine(),
        )
        monkeypatch.setattr(
            app_module,
            "register_energy_job",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("scheduler setup failed")
            ),
        )

        app = create_app(settings)
        with pytest.raises(RuntimeError, match="scheduler setup failed"):
            with TestClient(app):
                pass

        assert observed == ["mcp_enter", "decision", "mcp_exit"]
        assert app.state.decision_engine is None
        assert app.state.scheduler is None
        assert mcp_server._settings_override is None
        assert store_session._engine is None
        assert store_session._session_factory is None

    def test_scheduler_shutdown_failure_does_not_skip_other_cleanup(
        self,
        settings,
        monkeypatch,
    ) -> None:
        """AsyncExitStack continues after one registered cleanup fails."""
        import healthmes.app as app_module

        observed: list[str] = []

        class StubMcpApp:
            @asynccontextmanager
            async def lifespan(
                self,
                _app,
            ) -> AsyncIterator[None]:
                observed.append("mcp_enter")
                try:
                    yield
                finally:
                    observed.append("mcp_exit")

            async def __call__(self, _scope, _receive, _send) -> None:
                raise AssertionError("MCP request dispatch was not expected")

        class StubDecisionEngine:
            async def aclose(self) -> None:
                observed.append("decision")

        monkeypatch.setattr(
            app_module.mcp_server,
            "build_mcp_http_app",
            lambda: StubMcpApp(),
        )
        monkeypatch.setattr(
            app_module,
            "build_configured_decision_engine",
            lambda **_kwargs: StubDecisionEngine(),
        )
        monkeypatch.setattr(
            app_module,
            "shutdown_scheduler",
            lambda _scheduler: (_ for _ in ()).throw(
                RuntimeError("scheduler shutdown failed")
            ),
        )

        app = create_app(settings)
        with pytest.raises(
            RuntimeError,
            match="scheduler shutdown failed",
        ):
            with TestClient(app):
                pass

        assert observed == ["mcp_enter", "decision", "mcp_exit"]
        assert app.state.decision_engine is None
        assert app.state.scheduler is None
        assert mcp_server._settings_override is None
        assert store_session._engine is None
        assert store_session._session_factory is None

    def test_mcp_lifespan_entry_failure_releases_settings_and_database(
        self,
        settings,
        monkeypatch,
    ) -> None:
        """MCP startup failure cannot leave process-global state installed."""
        import healthmes.app as app_module

        class FailingMcpApp:
            @asynccontextmanager
            async def lifespan(
                self,
                _app,
            ) -> AsyncIterator[None]:
                raise RuntimeError("MCP lifespan entry failed")
                yield

            async def __call__(self, _scope, _receive, _send) -> None:
                raise AssertionError("MCP request dispatch was not expected")

        monkeypatch.setattr(
            app_module.mcp_server,
            "build_mcp_http_app",
            lambda: FailingMcpApp(),
        )

        app = create_app(settings)
        with pytest.raises(
            RuntimeError,
            match="MCP lifespan entry failed",
        ):
            with TestClient(app):
                pass

        assert app.state.decision_engine is None
        assert app.state.scheduler is None
        assert mcp_server._settings_override is None
        assert store_session._engine is None
        assert store_session._session_factory is None

    async def test_repeated_lifespan_cancellation_drains_decisions_before_db(
        self,
        settings,
        monkeypatch,
    ) -> None:
        """Cancellation cannot let MCP or DB outrun accepted finalization."""
        import healthmes.app as app_module

        entered = asyncio.Event()
        hold_lifespan = asyncio.Event()
        observed: list[str] = []

        class StubMcpApp:
            @asynccontextmanager
            async def lifespan(
                self,
                _app,
            ) -> AsyncIterator[None]:
                observed.append("mcp_enter")
                try:
                    yield
                finally:
                    assert decision_engine.finished.is_set()
                    assert store_session._engine is not None
                    observed.append("mcp_exit")

            async def __call__(self, _scope, _receive, _send) -> None:
                raise AssertionError("MCP request dispatch was not expected")

        class StubDecisionEngine:
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.finished = asyncio.Event()
                self._shutdown_task: asyncio.Task[None] | None = None

            async def _shutdown(self) -> None:
                self.started.set()
                await self.release.wait()
                self.finished.set()
                observed.append("decision")

            async def aclose(self) -> None:
                if self._shutdown_task is None:
                    self._shutdown_task = asyncio.create_task(
                        self._shutdown()
                    )
                await asyncio.shield(self._shutdown_task)

        decision_engine = StubDecisionEngine()
        monkeypatch.setattr(
            app_module.mcp_server,
            "build_mcp_http_app",
            lambda: StubMcpApp(),
        )
        monkeypatch.setattr(
            app_module,
            "build_configured_decision_engine",
            lambda **_kwargs: decision_engine,
        )

        app = create_app(settings)

        async def run_lifespan() -> None:
            async with app.router.lifespan_context(app):
                entered.set()
                await hold_lifespan.wait()

        lifespan_task = asyncio.create_task(run_lifespan())
        await asyncio.wait_for(entered.wait(), timeout=1)
        lifespan_task.cancel()
        await asyncio.wait_for(decision_engine.started.wait(), timeout=1)

        assert lifespan_task.done() is False
        assert store_session._engine is not None
        assert mcp_server._settings_override is settings

        lifespan_task.cancel()
        await asyncio.sleep(0)
        assert lifespan_task.done() is False
        assert store_session._engine is not None
        assert mcp_server._settings_override is settings

        decision_engine.release.set()
        with pytest.raises(asyncio.CancelledError):
            await lifespan_task

        assert observed == ["mcp_enter", "decision", "mcp_exit"]
        assert app.state.decision_engine is None
        assert app.state.scheduler is None
        assert mcp_server._settings_override is None
        assert store_session._engine is None
        assert store_session._session_factory is None


def test_startup_compacts_legacy_and_scrubs_expired_transient_receipts(
    settings,
) -> None:
    now = datetime.now(UTC)
    database_url = (
        f"sqlite+pysqlite:///{settings.data_dir / 'receipt-startup.db'}"
    )
    database = create_db_engine(database_url)
    Base.metadata.create_all(database)
    record_id = uuid.uuid4()
    legacy = _completed_receipt(
        now=now,
        payload={
            "schema": "healthmes.decision-receipt.v1",
            "result": {
                "answer": "legacy sensitive answer",
                "persistence_status": "persisted",
                "decision_record_id": str(record_id),
            },
        },
    )
    expired = _completed_receipt(
        now=now,
        retention_basis_at=now - timedelta(minutes=15),
        payload={
            "schema": "healthmes.decision-receipt.v2",
            "kind": "transient_result",
            "result": {"answer": "expired transient answer"},
        },
    )
    malformed = DecisionRequestReceipt(
        request_id=uuid.uuid4(),
        request_fingerprint=uuid.uuid4().hex * 2,
        requested_at=now,
        state="completed",
        result_payload=["malformed sensitive answer"],
        result_expires_at=now + timedelta(days=30),
        retention_basis_at=now - timedelta(minutes=15),
        expires_at=now + timedelta(days=30),
    )
    forged_pointer = _completed_receipt(
        now=now,
        retention_basis_at=now - timedelta(minutes=15),
        payload={
            "schema": "healthmes.decision-receipt.v2",
            "kind": "decision_record",
            "decision_record_id": "not-a-uuid",
            "result": {"answer": "forged pointer sensitive answer"},
        },
    )
    with Session(database) as session:
        session.add_all(
            (legacy, expired, malformed, forged_pointer)
        )
        session.commit()
        legacy_id = legacy.id
        expired_id = expired.id
        malformed_id = malformed.id
        forged_pointer_id = forged_pointer.id
    database.dispose()

    configured = settings.model_copy(
        update={"database_url": database_url}
    )
    app = create_app(configured)
    with TestClient(app):
        assert app.state.scheduler is None
        with store_session.session_scope() as session:
            compacted = session.get(
                DecisionRequestReceipt,
                legacy_id,
            )
            scrubbed = session.get(
                DecisionRequestReceipt,
                expired_id,
            )
            rejected = session.get(
                DecisionRequestReceipt,
                malformed_id,
            )
            forged = session.get(
                DecisionRequestReceipt,
                forged_pointer_id,
            )
            assert compacted is not None
            assert compacted.result_payload == {
                "schema": "healthmes.decision-receipt.v2",
                "kind": "decision_record",
                "decision_record_id": str(record_id),
            }
            assert "legacy sensitive answer" not in str(
                compacted.result_payload
            )
            assert scrubbed is not None
            assert scrubbed.state == "tombstone"
            assert scrubbed.result_payload is None
            assert scrubbed.result_expires_at is None
            assert rejected is not None
            assert rejected.state == "tombstone"
            assert rejected.result_payload is None
            assert rejected.result_expires_at is None
            assert forged is not None
            assert forged.state == "tombstone"
            assert forged.result_payload is None
            assert forged.result_expires_at is None


def test_recurring_receipt_scrub_runs_with_scheduler_disabled(
    settings,
    monkeypatch,
) -> None:
    import healthmes.app as app_module

    monkeypatch.setattr(
        app_module,
        "_DECISION_RECEIPT_MAINTENANCE_INTERVAL_SECONDS",
        0.01,
    )
    configured = settings.model_copy(
        update={
            "database_url": (
                f"sqlite+pysqlite:///"
                f"{settings.data_dir / 'receipt-recurring.db'}"
            )
        }
    )
    app = create_app(configured)
    with TestClient(app):
        assert app.state.scheduler is None
        assert app.state.decision_receipt_maintenance_task is not None
        now = datetime.now(UTC)
        receipt = _completed_receipt(
            now=now,
            payload={
                "schema": "healthmes.decision-receipt.v1",
                "result": {
                    "answer": "recurring sensitive answer",
                    "persistence_status": "persisted",
                    "decision_record_id": str(uuid.uuid4()),
                },
            },
        )
        with store_session.session_scope() as session:
            session.add(receipt)
            session.flush()
            receipt_id = receipt.id

        deadline = time.monotonic() + 2
        stored_payload = None
        while time.monotonic() < deadline:
            with store_session.session_scope() as session:
                stored = session.get(
                    DecisionRequestReceipt,
                    receipt_id,
                )
                assert stored is not None
                if (
                    isinstance(stored.result_payload, dict)
                    and stored.result_payload.get("schema")
                    == "healthmes.decision-receipt.v2"
                ):
                    stored_payload = dict(stored.result_payload)
                    break
            time.sleep(0.01)
        else:
            pytest.fail("recurring receipt maintenance did not run")

        assert "recurring sensitive answer" not in str(
            stored_payload
        )


def test_mandatory_receipt_maintenance_commits_bounded_progress(
    settings,
) -> None:
    configured = settings.model_copy(
        update={
            "database_url": (
                f"sqlite+pysqlite:///"
                f"{settings.data_dir / 'receipt-bounded.db'}"
            )
        }
    )
    database = init_engine(configured)
    Base.metadata.create_all(database)
    now = datetime.now(UTC)
    try:
        with store_session.session_scope() as session:
            session.add_all(
                _completed_receipt(
                    now=now,
                    payload={
                        "schema": "healthmes.decision-receipt.v1",
                        "result": {
                            "answer": f"sensitive answer {index}",
                            "persistence_status": "persisted",
                            "decision_record_id": str(uuid.uuid4()),
                        },
                    },
                )
                for index in range(3)
            )

        processed, cursor = (
            _run_mandatory_decision_receipt_maintenance(
                now=now,
                batch_size=1,
                max_rows=2,
            )
        )
        assert processed == 2
        assert cursor is not None
        with store_session.session_scope() as session:
            remaining = session.scalar(
                select(DecisionRequestReceipt)
                .where(
                    DecisionRequestReceipt.result_payload[
                        "schema"
                    ].as_string()
                    == "healthmes.decision-receipt.v1"
                )
                .limit(1)
            )
            assert remaining is not None

        processed, cursor = (
            _run_mandatory_decision_receipt_maintenance(
                now=now,
                batch_size=1,
                max_rows=2,
                after_id=cursor,
            )
        )
        assert processed == 1
        assert cursor is None
        with store_session.session_scope() as session:
            assert session.scalar(
                select(DecisionRequestReceipt)
                .where(
                    DecisionRequestReceipt.result_payload[
                        "schema"
                    ].as_string()
                    == "healthmes.decision-receipt.v1"
                )
                .limit(1)
            ) is None
    finally:
        dispose_engine()


class TestMcpWiring:
    def test_mcp_initialize_handshake_at_exactly_slash_mcp(self, settings) -> None:
        """The MCP session manager runs (chained lifespan) and serves POST /mcp."""
        app = create_app(settings)
        with TestClient(
            app,
            base_url="http://127.0.0.1:8100",
            client=("127.0.0.1", 43123),
        ) as client:
            response = client.post("/mcp", json=_MCP_INITIALIZE, headers=_MCP_HEADERS)

            assert response.status_code == 200
            assert response.headers.get("mcp-session-id")
            assert '"serverInfo"' in response.text
            assert '"healthmes"' in response.text

    def test_fastapi_routes_keep_precedence_and_404s_keep_the_envelope(self, settings) -> None:
        """/health & /v1 stay FastAPI-served; unknown paths keep the envelope."""
        app = create_app(settings)
        with TestClient(
            app,
            base_url="http://127.0.0.1:8100",
            client=("127.0.0.1", 43123),
        ) as client:
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

    def test_lifespan_accepts_fixed_offset_timezone(self, settings) -> None:
        fixed = settings.model_copy(update={"timezone": "UTC+09:00"})
        app = create_app(fixed)

        with TestClient(app):
            assert str(mcp_server._local_timezone()) == "UTC+09:00"


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
            assert job_ids == {
                TRIGGER_JOB_ID,
                ENERGY_JOB_ID,
                BACKUP_JOB_ID,
                CALENDAR_ADJUSTMENT_MAINTENANCE_JOB_ID,
                STORAGE_MAINTENANCE_JOB_ID,
                ACTIVITY_MAINTENANCE_JOB_ID,
            }
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
                CALENDAR_ADJUSTMENT_MAINTENANCE_JOB_ID,
                SLEEP_RECONCILIATION_JOB_ID,
                STORAGE_MAINTENANCE_JOB_ID,
                ACTIVITY_MAINTENANCE_JOB_ID,
                calendar_job_id(CalendarSource.GOOGLE),
                calendar_job_id(CalendarSource.CALDAV),
            }
            google_job = scheduler.get_job(calendar_job_id(CalendarSource.GOOGLE))
            caldav_job = scheduler.get_job(calendar_job_id(CalendarSource.CALDAV))
            assert google_job.trigger.interval.total_seconds() == 5 * 60
            assert caldav_job.trigger.interval.total_seconds() == 10 * 60

    def test_enabled_activitywatch_registers_one_job_and_shutdown_stops_it(
        self, settings
    ) -> None:
        enabled = settings.model_copy(
            update={
                "scheduler_enabled": True,
                "activitywatch_enabled": True,
                "activitywatch_interval_minutes": 13,
            }
        )
        app = create_app(enabled)
        with TestClient(app):
            scheduler = app.state.scheduler
            assert scheduler is not None
            matches = [
                job
                for job in scheduler.get_jobs()
                if job.id == ACTIVITYWATCH_JOB_ID
            ]
            assert len(matches) == 1
            assert matches[0].trigger.interval.total_seconds() == 13 * 60
            assert matches[0].max_instances == 1
            assert matches[0].coalesce is True
        assert not scheduler.running
        assert app.state.scheduler is None

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
            CALENDAR_ADJUSTMENT_MAINTENANCE_JOB_ID,
            STORAGE_MAINTENANCE_JOB_ID,
            ACTIVITY_MAINTENANCE_JOB_ID,
        }
        assert not prepared.running

    def test_global_scheduler_gate_keeps_activitywatch_unstarted(
        self, settings, monkeypatch
    ) -> None:
        import healthmes.app as app_module

        captured = {}
        real_start = app_module.start_scheduler

        def spying_start(settings_arg, *, scheduler=None):
            captured["scheduler"] = scheduler
            return real_start(settings_arg, scheduler=scheduler)

        monkeypatch.setattr(app_module, "start_scheduler", spying_start)
        configured = settings.model_copy(
            update={"activitywatch_enabled": True}
        )
        app = create_app(configured)
        with TestClient(app):
            assert app.state.scheduler is None
        prepared = captured["scheduler"]
        assert prepared.get_job(ACTIVITYWATCH_JOB_ID) is not None
        assert not prepared.running
