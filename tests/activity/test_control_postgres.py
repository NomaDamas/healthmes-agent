from __future__ import annotations

import asyncio
import multiprocessing
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

import healthmes.activity.locking as locking_module
from healthmes.activity.activitywatch import import_activitywatch
from healthmes.activity.aggregation import (
    SUMMARY_DERIVATION_VERSION,
    evidence_digest,
)
from healthmes.activity.contracts import (
    ActivityBatchIn,
    ActivityCapability,
    ActivityCollectionStatusUpdate,
    ActivityCollectionUpdate,
    ActivityPermissionStatus,
    ActivityPlatform,
    ActivityWatchImportRequest,
    AppHourRecord,
    AppIntervalRecord,
)
from healthmes.activity.locking import (
    global_write_plane_guard,
    lock_activity_write_plane,
    session_holds_write_plane,
)
from healthmes.activity.maintenance import delete_activity_data
from healthmes.activity.repository import (
    APP_HOUR_EVENT,
    APP_INTERVAL_EVENT,
    COLLECTION_CURSOR_EVENT,
    CONTROL_EVENT_TYPES,
    DAY_SUMMARY_EVENT,
    HOUR_SUMMARY_EVENT,
    get_control_payload,
    update_collection_config,
    update_collection_status,
)
from healthmes.activity.service import (
    ActivityCollectionBlockedError,
    ingest_activity_batch,
)
from healthmes.app import create_app
from healthmes.config import Settings
from healthmes.store import Base, WellnessEvent, create_db_engine
from healthmes.store.session import get_session


def _hold_postgres_write_plane(
    database_url: str,
    acquired,
    release,
) -> None:
    engine = create_db_engine(database_url)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    try:
        with factory() as session:
            lock_activity_write_plane(session)
            session.execute(sa.text("SELECT 1"))
            acquired.set()
            if not release.wait(timeout=10):
                raise TimeoutError(
                    "timed out waiting to release PostgreSQL write plane"
                )
            session.rollback()
    finally:
        engine.dispose()


def _acquire_postgres_write_plane(
    database_url: str,
    acquired,
) -> None:
    engine = create_db_engine(database_url)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    try:
        with factory() as session:
            lock_activity_write_plane(session)
            session.execute(sa.text("SELECT 1"))
            acquired.set()
            session.rollback()
    finally:
        engine.dispose()


def _create_app_with_primed_routes(settings, freezer):
    """Compile FastAPI's lazy route schemas outside freezegun."""
    freezer.stop()
    try:
        application = create_app(settings)
        primer = TestClient(
            application,
            base_url="http://127.0.0.1:8100",
            client=("127.0.0.1", 43122),
        )
        try:
            assert primer.get("/v1/__prime_routes__").status_code == 404
        finally:
            primer.close()
    finally:
        freezer.start()
    return application


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_postgres_write_plane_serializes_independent_processes() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    context = multiprocessing.get_context("spawn")
    first_acquired = context.Event()
    release_first = context.Event()
    second_acquired = context.Event()
    first = context.Process(
        target=_hold_postgres_write_plane,
        args=(database_url, first_acquired, release_first),
    )
    second = context.Process(
        target=_acquire_postgres_write_plane,
        args=(database_url, second_acquired),
    )

    first.start()
    try:
        assert first_acquired.wait(timeout=5)
        second.start()
        assert not second_acquired.wait(timeout=0.25)
        release_first.set()
        assert second_acquired.wait(timeout=5)
    finally:
        release_first.set()
        first.join(timeout=5)
        if first.is_alive():
            first.terminate()
            first.join(timeout=5)
        if second.pid is not None:
            second.join(timeout=5)
            if second.is_alive():
                second.terminate()
                second.join(timeout=5)

    assert first.exitcode == 0
    assert second.exitcode == 0


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
async def test_child_task_cannot_inherit_postgres_global_guard() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    engine = create_db_engine(
        database_url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.25,
    )

    async def inspect_child_context(connection) -> None:
        with Session(bind=connection) as child_session:
            assert not session_holds_write_plane(child_session)
        assert not locking_module._connection_holds_write_fence(connection)

    try:
        with global_write_plane_guard(engine) as guard_connection:
            assert guard_connection is not None
            with Session(bind=guard_connection) as parent_session:
                assert session_holds_write_plane(parent_session)
            assert locking_module._connection_holds_write_fence(
                guard_connection
            )
            await asyncio.create_task(
                inspect_child_context(guard_connection)
            )
    finally:
        engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_same_owner_reuses_postgres_global_guard_connection() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    engine = create_db_engine(
        database_url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.25,
    )

    try:
        with global_write_plane_guard(engine) as outer_connection:
            assert outer_connection is not None
            with global_write_plane_guard(
                engine,
                timeout_seconds=0.02,
            ) as inner_connection:
                assert inner_connection is outer_connection
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("guard_name", "process_lock_name"),
    (
        ("global_write_plane_guard", "_ACTIVITY_WRITE_LOCK"),
        ("payload_generation_guard", "_PAYLOAD_GENERATION_LOCK"),
    ),
)
def test_postgres_guard_new_engine_uses_remaining_deadline(
    monkeypatch,
    guard_name,
    process_lock_name,
) -> None:
    clock = [100.0]
    captured: dict[str, object] = {}
    connection_closed = False

    class FakeConnection:
        closed = False
        invalidated = False
        dialect = type("FakeDialect", (), {"name": "postgresql"})()

        def __init__(self) -> None:
            self.info: dict[str, object] = {}
            self.in_transaction_state = False

        def in_transaction(self) -> bool:
            return self.in_transaction_state

        def get_isolation_level(self) -> str:
            return "READ COMMITTED"

        def execution_options(self, **_options):
            return self

        def commit(self) -> None:
            self.in_transaction_state = False

        def rollback(self) -> None:
            self.in_transaction_state = False

        def close(self) -> None:
            nonlocal connection_closed
            connection_closed = True
            self.closed = True

    class FakeEngine:
        def connect(self) -> FakeConnection:
            return FakeConnection()

        def dispose(self) -> None:
            captured["disposed"] = True

    @contextmanager
    def delayed_process_lock(deadline):
        assert deadline == pytest.approx(101.0)
        clock[0] += 0.4
        yield

    process_lock = getattr(locking_module, process_lock_name)
    real_process_acquire = process_lock.acquire

    def delayed_process_acquire(
        lease,
        *,
        timeout_seconds,
        deadline=None,
    ):
        assert deadline == pytest.approx(101.0)
        acquired = real_process_acquire(
            lease,
            timeout_seconds=timeout_seconds,
            deadline=deadline,
        )
        clock[0] += 0.4
        return acquired

    def fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeEngine()

    monkeypatch.setattr(locking_module, "steady_time", lambda: clock[0])
    if guard_name == "global_write_plane_guard":
        monkeypatch.setattr(
            locking_module,
            "_activity_write_lock_until",
            delayed_process_lock,
        )
    else:
        monkeypatch.setattr(
            process_lock,
            "acquire",
            delayed_process_acquire,
        )
    monkeypatch.setattr(locking_module, "create_engine", fake_create_engine)
    monkeypatch.setattr(
        locking_module,
        "try_postgres_advisory_lock",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        locking_module,
        "release_postgres_advisory_lock",
        lambda *_args: True,
    )

    guard = getattr(locking_module, guard_name)
    with guard(
        "postgresql+psycopg://healthmes@example/healthmes",
        timeout_seconds=1.0,
    ) as connection:
        assert connection is not None

    assert captured["pool_timeout"] == pytest.approx(0.6)
    assert captured["connect_args"] == {"connect_timeout": 1}
    assert captured["disposed"] is True
    assert connection_closed is True


@pytest.mark.parametrize(
    ("guard_name", "timeout_message"),
    (
        (
            "global_write_plane_guard",
            "PostgreSQL activity write-plane connection",
        ),
        (
            "payload_generation_guard",
            "PostgreSQL payload-generation connection",
        ),
    ),
)
def test_postgres_guard_supplied_engine_checkout_cannot_exceed_deadline(
    monkeypatch,
    guard_name,
    timeout_message,
) -> None:
    source = sa.create_engine(
        "postgresql+psycopg://healthmes@example/healthmes"
    )
    late_connection_closed = threading.Event()

    class FakeConnection:
        closed = False
        invalidated = False
        dialect = type("FakeDialect", (), {"name": "postgresql"})()

        def __init__(self) -> None:
            self.info: dict[str, object] = {}

        def in_transaction(self) -> bool:
            return False

        def close(self) -> None:
            self.closed = True
            late_connection_closed.set()

    late_connection = FakeConnection()

    def slow_connect():
        time.sleep(0.08)
        return late_connection

    monkeypatch.setattr(source, "connect", slow_connect)

    started = time.monotonic()
    try:
        with pytest.raises(
            TimeoutError,
            match=timeout_message,
        ):
            guard = getattr(locking_module, guard_name)
            with guard(
                source,
                timeout_seconds=0.02,
            ):
                pytest.fail("slow checkout unexpectedly entered the guard")
    finally:
        source.dispose()

    assert time.monotonic() - started < 0.15
    assert late_connection_closed.wait(timeout=1)


def test_postgres_checkout_timeouts_bound_live_workers() -> None:
    release = threading.Event()
    state_lock = threading.Lock()
    started = 0
    closed = 0

    class FakeConnection:
        def close(self) -> None:
            nonlocal closed
            with state_lock:
                closed += 1

    class BlockingEngine:
        def connect(self):
            nonlocal started
            with state_lock:
                started += 1
            assert release.wait(timeout=5)
            return FakeConnection()

    engine = BlockingEngine()
    try:
        for _ in range(12):
            with pytest.raises(
                TimeoutError,
                match="bounded PostgreSQL checkout",
            ):
                locking_module._connect_postgres_before_deadline(
                    engine,  # type: ignore[arg-type]
                    deadline=locking_module.steady_time() + 0.01,
                    timeout_message="bounded PostgreSQL checkout",
                    dispose_engine=False,
                )

        assert started == locking_module._POSTGRES_CONNECT_WORKER_LIMIT
        live_workers = [
            thread
            for thread in threading.enumerate()
            if thread.name == "healthmes-postgres-guard-connect"
        ]
        assert (
            len(live_workers)
            <= locking_module._POSTGRES_CONNECT_WORKER_LIMIT
        )
    finally:
        release.set()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with state_lock:
            if closed == started:
                break
        time.sleep(0.01)
    assert closed == started


@pytest.mark.parametrize(
    "guard_name",
    ("postgres_activity_write_plane_guard", "payload_generation_guard"),
)
def test_postgres_advisory_cleanup_failure_retires_caller_connection(
    monkeypatch,
    guard_name,
) -> None:
    class BrokenPoolConnection:
        def __init__(self) -> None:
            self.invalidate_calls = 0

        def invalidate(self, _cause) -> None:
            self.invalidate_calls += 1
            raise RuntimeError("injected pool invalidate failure")

    class FakeEngine:
        url = sa.make_url(
            "postgresql+psycopg://healthmes@example/healthmes"
        )

        def __init__(self) -> None:
            self.dispose_calls = 0

        def dispose(self) -> None:
            self.dispose_calls += 1

    class FakeConnection:
        dialect = type("FakeDialect", (), {"name": "postgresql"})()

        def __init__(self) -> None:
            self.engine = FakeEngine()
            self.info: dict[str, object] = {}
            self.closed = False
            self.invalidated = False
            self.invalidate_calls = 0
            self.detached = False
            self.pool_connection = BrokenPoolConnection()

        @property
        def connection(self):
            return self.pool_connection

        def in_transaction(self) -> bool:
            return False

        def get_isolation_level(self) -> str:
            return "READ COMMITTED"

        def execution_options(self, **_options):
            return self

        def commit(self) -> None:
            pass

        def rollback(self) -> None:
            pass

        def invalidate(self, _cause=None) -> None:
            self.invalidate_calls += 1
            raise RuntimeError("injected connection invalidate failure")

        def detach(self) -> None:
            self.detached = True

        def close(self) -> None:
            self.closed = True

    connection = FakeConnection()
    monkeypatch.setattr(locking_module, "Connection", FakeConnection)
    monkeypatch.setattr(
        locking_module,
        "try_postgres_advisory_lock",
        lambda *_args: True,
    )

    def fail_unlock(*_args):
        raise RuntimeError("injected advisory unlock failure")

    monkeypatch.setattr(
        locking_module,
        "release_postgres_advisory_lock",
        fail_unlock,
    )

    guard = getattr(locking_module, guard_name)
    with pytest.raises(
        RuntimeError,
        match="connection was retired",
    ):
        with guard(
            connection,  # type: ignore[arg-type]
            timeout_seconds=0.2,
            poll_seconds=0.01,
        ):
            pass

    assert connection.invalidate_calls == 1
    assert connection.pool_connection.invalidate_calls == 1
    assert connection.detached is True
    assert connection.closed is True
    assert connection.engine.dispose_calls == 0


@pytest.mark.parametrize(
    ("guard_name", "timeout_message"),
    (
        (
            "postgres_activity_write_plane_guard",
            "timed out waiting for the activity write plane",
        ),
        (
            "payload_generation_guard",
            "timed out waiting for the payload-generation lock",
        ),
    ),
)
def test_postgres_guard_timeout_does_not_unlock_an_unacquired_lock(
    monkeypatch,
    guard_name,
    timeout_message,
) -> None:
    class FakeEngine:
        url = sa.make_url(
            "postgresql+psycopg://healthmes@example/healthmes"
        )

    class FakeConnection:
        dialect = type("FakeDialect", (), {"name": "postgresql"})()

        def __init__(self) -> None:
            self.engine = FakeEngine()
            self.info: dict[str, object] = {}
            self.closed = False
            self.invalidated = False
            self.invalidate_calls = 0

        def in_transaction(self) -> bool:
            return False

        def get_isolation_level(self) -> str:
            return "READ COMMITTED"

        def execution_options(self, **_options):
            return self

        def invalidate(self, _cause=None) -> None:
            self.invalidate_calls += 1
            self.invalidated = True

    connection = FakeConnection()
    unlock_calls = 0

    def unexpected_unlock(*_args):
        nonlocal unlock_calls
        unlock_calls += 1
        return False

    monkeypatch.setattr(locking_module, "Connection", FakeConnection)
    monkeypatch.setattr(
        locking_module,
        "try_postgres_advisory_lock",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        locking_module,
        "release_postgres_advisory_lock",
        unexpected_unlock,
    )

    with pytest.raises(
        TimeoutError,
        match=timeout_message,
    ):
        guard = getattr(locking_module, guard_name)
        with guard(
            connection,  # type: ignore[arg-type]
            timeout_seconds=0.01,
            poll_seconds=0.001,
        ):
            pytest.fail("guard unexpectedly acquired")

    assert unlock_calls == 0
    assert connection.invalidate_calls == 0
    assert connection.invalidated is False


@pytest.mark.parametrize(
    ("boundary_change", "expected_reason"),
    (
        ("disable", "collection_disabled"),
        ("revoke", "permission_revoked"),
    ),
)
@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_activitywatch_rest_import_loses_race_to_privacy_boundary(
    tmp_path,
    monkeypatch,
    stable_activity_wall_clock,
    boundary_change: str,
    expected_reason: str,
) -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_test_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    device_id = f"mac-rest-race-{boundary_change}"
    network_started = threading.Event()
    network_release = threading.Event()
    responses = []
    failures: list[BaseException] = []

    def list_buckets(self):
        return {
            "window": {"type": "currentwindow"},
            "afk": {"type": "afkstatus"},
        }

    def get_events(self, bucket_id, *, start, end):
        if bucket_id == "window":
            network_started.set()
            assert network_release.wait(timeout=5)
            return [
                {
                    "id": 1,
                    "timestamp": "2026-08-09T10:00:00Z",
                    "duration": 3600,
                    "data": {
                        "app": "Code",
                        "title": "must not be stored",
                    },
                }
            ]
        return [
            {
                "id": 2,
                "timestamp": "2026-08-09T10:00:00Z",
                "duration": 3600,
                "data": {"status": "not-afk"},
            }
        ]

    monkeypatch.setattr(
        "healthmes.activity.activitywatch.ActivityWatchClient.list_buckets",
        list_buckets,
    )
    monkeypatch.setattr(
        "healthmes.activity.activitywatch.ActivityWatchClient.get_events",
        get_events,
    )
    settings = Settings(
        database_url=database_url,
        data_dir=tmp_path / "data",
        scheduler_enabled=False,
        timezone="UTC",
        api_token="",
        _env_file=None,
    )
    application = _create_app_with_primed_routes(
        settings,
        stable_activity_wall_clock,
    )

    def override_get_session():
        with factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_get_session
    import_client = TestClient(
        application,
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    )
    control_client = TestClient(
        application,
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43124),
    )

    def run_import() -> None:
        try:
            responses.append(
                import_client.post(
                    "/v1/activity/activitywatch/import",
                    json={
                        "device_id": device_id,
                        "platform": "macos",
                        "timezone": "UTC",
                        "start_at": "2026-08-09T10:00:00Z",
                        "end_at": "2026-08-09T11:00:00Z",
                    },
                )
            )
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=run_import)
    try:
        worker.start()
        assert network_started.wait(timeout=5)

        if boundary_change == "disable":
            boundary_response = control_client.put(
                f"/v1/activity/devices/{device_id}/collection",
                json={"enabled": False},
            )
        else:
            boundary_response = control_client.post(
                f"/v1/activity/devices/{device_id}/status",
                json={
                    "platform": "macos",
                    "capability": "detailed",
                    "permission_status": "revoked",
                    "status_observed_at": "2026-08-09T10:30:00Z",
                },
            )
        assert boundary_response.status_code == 200

        network_release.set()
        worker.join(timeout=10)

        assert not worker.is_alive()
        assert failures == []
        assert len(responses) == 1
        assert responses[0].status_code == 409
        assert (
            responses[0].json()["error"]["code"]
            == "activity_collection_blocked"
        )
        assert responses[0].json()["error"]["message"] == expected_reason

        with factory() as session:
            state = get_control_payload(session, device_id)
            stored = list(
                session.scalars(
                    sa.select(WellnessEvent).where(
                        WellnessEvent.event_type.in_(
                            (
                                APP_INTERVAL_EVENT,
                                HOUR_SUMMARY_EVENT,
                                DAY_SUMMARY_EVENT,
                                COLLECTION_CURSOR_EVENT,
                            )
                        )
                    )
                )
            )
            assert stored == []
            assert state["cursors"] == {}
            assert state["last_uploaded_at"] is None
            if boundary_change == "disable":
                assert state["enabled"] is False
            else:
                assert state["permission_status"] == "revoked"
    finally:
        network_release.set()
        worker.join(timeout=5)
        import_client.close()
        control_client.close()
        application.dependency_overrides.clear()
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_activitywatch_rest_rejects_older_snapshot_after_newer_empty_import(
    tmp_path,
    monkeypatch,
    stable_activity_wall_clock,
) -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_test_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    device_id = "mac-rest-latest-started-wins"
    old_network_started = threading.Event()
    old_network_release = threading.Event()
    window_call_lock = threading.Lock()
    window_calls = 0
    old_responses = []
    failures: list[BaseException] = []

    def list_buckets(self):
        return {
            "window": {"type": "currentwindow"},
            "afk": {"type": "afkstatus"},
        }

    def get_events(self, bucket_id, *, start, end):
        nonlocal window_calls
        if bucket_id == "window":
            with window_call_lock:
                window_calls += 1
                call_number = window_calls
            if call_number == 1:
                old_network_started.set()
                assert old_network_release.wait(timeout=5)
                return [
                    {
                        "id": 1,
                        "timestamp": "2026-08-09T10:00:00Z",
                        "duration": 3600,
                        "data": {
                            "app": "Code",
                            "title": "must not be restored",
                        },
                    }
                ]
            assert call_number == 2
            return []
        return [
            {
                "id": 2,
                "timestamp": start.isoformat(),
                "duration": (end - start).total_seconds(),
                "data": {"status": "not-afk"},
            }
        ]

    monkeypatch.setattr(
        "healthmes.activity.activitywatch.ActivityWatchClient.list_buckets",
        list_buckets,
    )
    monkeypatch.setattr(
        "healthmes.activity.activitywatch.ActivityWatchClient.get_events",
        get_events,
    )
    settings = Settings(
        database_url=database_url,
        data_dir=tmp_path / "data",
        scheduler_enabled=False,
        timezone="UTC",
        api_token="",
        _env_file=None,
    )
    application = _create_app_with_primed_routes(
        settings,
        stable_activity_wall_clock,
    )

    def override_get_session():
        with factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_get_session
    old_client = TestClient(
        application,
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43123),
    )
    latest_client = TestClient(
        application,
        base_url="http://127.0.0.1:8100",
        client=("127.0.0.1", 43124),
    )
    payload = {
        "device_id": device_id,
        "platform": "macos",
        "timezone": "UTC",
        "start_at": "2026-08-09T10:00:00Z",
        "end_at": "2026-08-09T11:00:00Z",
    }

    def run_old_import() -> None:
        try:
            old_responses.append(
                old_client.post(
                    "/v1/activity/activitywatch/import",
                    json=payload,
                )
            )
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=run_old_import)
    try:
        worker.start()
        assert old_network_started.wait(timeout=5)

        latest_response = latest_client.post(
            "/v1/activity/activitywatch/import",
            json=payload,
        )
        assert latest_response.status_code == 200
        assert latest_response.json()["accepted"] == 0

        old_network_release.set()
        worker.join(timeout=10)

        assert not worker.is_alive()
        assert failures == []
        assert len(old_responses) == 1
        assert old_responses[0].status_code == 409
        assert (
            old_responses[0].json()["error"]["code"]
            == "stale_activitywatch_import"
        )

        with factory() as session:
            state = get_control_payload(session, device_id)
            stored = list(
                session.scalars(
                    sa.select(WellnessEvent).where(
                        WellnessEvent.event_type.in_(
                            (
                                APP_INTERVAL_EVENT,
                                HOUR_SUMMARY_EVENT,
                                DAY_SUMMARY_EVENT,
                            )
                        )
                    )
                )
            )
            assert stored == []
            assert state["cursors"]["activitywatch:window"] == (
                "2026-08-09T11:00:00+00:00"
            )
            assert state["last_uploaded_at"] is not None
    finally:
        old_network_release.set()
        worker.join(timeout=5)
        old_client.close()
        latest_client.close()
        application.dependency_overrides.clear()
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_activitywatch_reservation_waits_for_first_disable_before_network() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_test_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    device_id = "mac-first-disable-race"
    network_called = threading.Event()
    worker_started = threading.Event()
    outcome: list[str] = []

    class FakeActivityWatchClient:
        def list_buckets(self):
            network_called.set()
            return {
                "window": {"type": "currentwindow"},
                "afk": {"type": "afkstatus"},
            }

        def get_events(self, bucket_id, *, start, end):
            network_called.set()
            if bucket_id == "window":
                return [
                    {
                        "id": 1,
                        "timestamp": "2026-08-01T10:00:00Z",
                        "duration": 3600,
                        "data": {"app": "Code", "title": "must not be stored"},
                    }
                ]
            return [
                {
                    "id": 2,
                    "timestamp": "2026-08-01T10:00:00Z",
                    "duration": 3600,
                    "data": {"status": "not-afk"},
                }
            ]

    def run_import() -> None:
        with factory() as session:
            worker_started.set()
            try:
                import_activitywatch(
                    session,
                    ActivityWatchImportRequest(
                        device_id=device_id,
                        platform=ActivityPlatform.MACOS,
                        timezone="UTC",
                        start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                        end_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
                    ),
                    client=FakeActivityWatchClient(),
                    now=datetime(2026, 8, 1, 11, tzinfo=UTC),
                )
            except ActivityCollectionBlockedError as exc:
                session.rollback()
                outcome.append(exc.reason)
            else:
                session.commit()
                outcome.append("stored")

    worker: threading.Thread | None = None
    try:
        with factory() as writer:
            update_collection_config(
                writer,
                device_id,
                ActivityCollectionUpdate(enabled=False),
                now=datetime(2026, 8, 1, 10, 30, tzinfo=UTC),
            )
            worker = threading.Thread(target=run_import)
            worker.start()
            assert worker_started.wait(timeout=5)

            assert not network_called.wait(timeout=0.2)
            assert worker.is_alive()
            assert outcome == []
            writer.commit()
            worker.join(timeout=5)

        assert worker is not None
        assert not worker.is_alive()
        assert outcome == ["collection_disabled"]
        assert not network_called.is_set()
        with factory() as session:
            assert (
                session.scalar(
                    sa.select(WellnessEvent).where(
                        WellnessEvent.event_type == APP_INTERVAL_EVENT
                    )
                )
                is None
            )
    finally:
        if worker is not None:
            worker.join(timeout=5)
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_canonical_ingest_refreshes_stale_control_identity_map() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_test_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    device_id = "mac-stale-control-identity"

    try:
        with factory() as stale, factory() as writer:
            update_collection_config(
                stale,
                device_id,
                ActivityCollectionUpdate(enabled=True),
                now=datetime(2026, 8, 1, 9, tzinfo=UTC),
            )
            stale.commit()
            assert get_control_payload(stale, device_id)["enabled"] is True

            update_collection_config(
                writer,
                device_id,
                ActivityCollectionUpdate(enabled=False),
                now=datetime(2026, 8, 1, 9, 30, tzinfo=UTC),
            )
            writer.commit()

            with pytest.raises(
                ActivityCollectionBlockedError,
                match="collection_disabled",
            ):
                ingest_activity_batch(
                    stale,
                    ActivityBatchIn(
                        source_provider="postgres-stale-test",
                        source_device=device_id,
                        platform=ActivityPlatform.MACOS,
                        capability=ActivityCapability.DETAILED,
                        timezone="UTC",
                        collected_at=datetime(
                            2026, 8, 1, 10, tzinfo=UTC
                        ),
                        records=[
                            AppIntervalRecord(
                                source_record_id="must-not-store",
                                start_at=datetime(
                                    2026, 8, 1, 9, tzinfo=UTC
                                ),
                                end_at=datetime(
                                    2026, 8, 1, 10, tzinfo=UTC
                                ),
                                state="active",
                                app_id="Code",
                            )
                        ],
                    ),
                    already_filtered=True,
                    now=datetime(2026, 8, 1, 10, tzinfo=UTC),
                )
            stale.rollback()

        with factory() as session:
            assert (
                session.scalar(
                    sa.select(WellnessEvent).where(
                        WellnessEvent.event_type
                        == APP_INTERVAL_EVENT
                    )
                )
                is None
            )
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_concurrent_devices_serialize_one_summary_scope() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_test_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    worker_started = threading.Event()
    outcome: list[str] = []
    failures: list[BaseException] = []

    def batch(provider: str, device: str) -> ActivityBatchIn:
        return ActivityBatchIn(
            source_provider=provider,
            source_device=device,
            platform=ActivityPlatform.MACOS,
            capability=ActivityCapability.DETAILED,
            timezone="UTC",
            collected_at=datetime(2026, 8, 1, 11, tzinfo=UTC),
            records=[
                AppIntervalRecord(
                    source_record_id=f"{provider}-active",
                    start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
                    end_at=datetime(2026, 8, 1, 10, 30, tzinfo=UTC),
                    state="active",
                    app_id="editor",
                )
            ],
        )

    def run_second_ingest() -> None:
        with factory() as session:
            worker_started.set()
            try:
                ingest_activity_batch(
                    session,
                    batch("desktop-provider-b", "desktop-b"),
                    now=datetime(2026, 8, 1, 11, tzinfo=UTC),
                )
                session.commit()
            except BaseException as exc:
                session.rollback()
                failures.append(exc)
            else:
                outcome.append("stored")

    worker: threading.Thread | None = None
    try:
        with factory() as first:
            lock_activity_write_plane(first)
            ingest_activity_batch(
                first,
                batch("desktop-provider-a", "desktop-a"),
                now=datetime(2026, 8, 1, 11, tzinfo=UTC),
            )

            worker = threading.Thread(target=run_second_ingest)
            worker.start()
            assert worker_started.wait(timeout=5)

            worker.join(timeout=0.2)
            assert worker.is_alive()
            assert outcome == []
            first.commit()
            worker.join(timeout=5)

        assert worker is not None
        assert not worker.is_alive()
        assert failures == []
        assert outcome == ["stored"]

        with factory() as session:
            raw_events = list(
                session.scalars(
                    sa.select(WellnessEvent).where(
                        WellnessEvent.event_type == APP_INTERVAL_EVENT
                    )
                )
            )
            daily = session.scalar(
                sa.select(WellnessEvent).where(
                    WellnessEvent.event_type == DAY_SUMMARY_EVENT
                )
            )

            assert len(raw_events) == 2
            assert daily is not None
            assert daily.payload["total_active_minutes"] == 30.0
            assert daily.payload["active_time_range"] == {
                "lower_bound_minutes": 30.0,
                "upper_bound_minutes": 30.0,
                "precision": "exact",
            }
            assert daily.payload["deduplication"] == {
                "precision": "exact",
                "method": "wall_clock_interval_union",
            }
            assert len(daily.payload["device_active_time_ranges"]) == 2
            assert all(
                item["active_time_range"] == {
                    "lower_bound_minutes": 30.0,
                    "upper_bound_minutes": 30.0,
                    "precision": "exact",
                }
                and item["timeline_precision"] == "interval"
                for item in daily.payload["device_active_time_ranges"]
            )
            assert daily.payload["device_count"] == 2
            assert (
                daily.derived_from["derivation_version"]
                == SUMMARY_DERIVATION_VERSION
            )
            assert daily.derived_from["raw_event_count"] == 2
            assert daily.derived_from["raw_evidence_sha256"] == evidence_digest(
                str(event.id) for event in raw_events
            )
    finally:
        if worker is not None:
            worker.join(timeout=5)
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_cross_device_aggregate_summary_preserves_bounded_ranges() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_test_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    try:
        with factory() as session:
            for device_id, platform in (
                ("phone", ActivityPlatform.ANDROID),
                ("desktop", ActivityPlatform.WINDOWS),
            ):
                ingest_activity_batch(
                    session,
                    ActivityBatchIn(
                        source_provider="aggregate-test",
                        source_device=device_id,
                        platform=platform,
                        capability=ActivityCapability.AGGREGATE,
                        timezone="UTC",
                        collected_at=datetime(
                            2026,
                            8,
                            1,
                            11,
                            tzinfo=UTC,
                        ),
                        records=[
                            AppHourRecord(
                                source_record_id=(
                                    f"{device_id}-same-hour"
                                ),
                                bucket_start=datetime(
                                    2026,
                                    8,
                                    1,
                                    10,
                                    tzinfo=UTC,
                                ),
                                app_id="reader",
                                foreground_seconds=30 * 60,
                                category="research",
                                coverage_seconds=3600,
                            )
                        ],
                    ),
                    now=datetime(2026, 8, 1, 12, tzinfo=UTC),
                )
            session.commit()

        with factory() as session:
            raw_events = list(
                session.scalars(
                    sa.select(WellnessEvent).where(
                        WellnessEvent.event_type == APP_HOUR_EVENT
                    )
                )
            )
            daily = session.scalar(
                sa.select(WellnessEvent).where(
                    WellnessEvent.event_type == DAY_SUMMARY_EVENT
                )
            )

            assert len(raw_events) == 2
            assert daily is not None
            assert daily.payload["active_time_range"] == {
                "lower_bound_minutes": 30.0,
                "upper_bound_minutes": 60.0,
                "precision": "bounded",
            }
            assert daily.payload["deduplication"] == {
                "precision": "bounded",
                "method": "bounded_device_ranges",
            }
            assert len(daily.payload["device_active_time_ranges"]) == 2
            assert daily.payload["category_minutes"] == {
                "research": 30.0
            }
            assert daily.payload["category_time_ranges"] == {
                "research": {
                    "lower_bound_minutes": 30.0,
                    "upper_bound_minutes": 60.0,
                    "precision": "bounded",
                }
            }
            assert daily.payload["category_attribution"] == {
                "precision": "bounded",
                "conflict": "none",
                "confidence": "limited",
            }
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_ios_status_waits_for_write_plane_before_device_lock() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_test_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    device_id = "ios-lock-order"
    worker_started = threading.Event()
    outcome: list[str] = []
    failures: list[BaseException] = []

    def write_ios_status() -> None:
        with factory() as session:
            worker_started.set()
            try:
                update_collection_status(
                    session,
                    device_id,
                    ActivityCollectionStatusUpdate(
                        platform=ActivityPlatform.IOS,
                        capability=ActivityCapability.AGGREGATE,
                        permission_status=ActivityPermissionStatus.GRANTED,
                        status_observed_at=datetime(
                            2026, 8, 1, 10, tzinfo=UTC
                        ),
                    ),
                    now=datetime(2026, 8, 1, 10, tzinfo=UTC),
                )
                session.commit()
            except BaseException as exc:
                session.rollback()
                failures.append(exc)
            else:
                outcome.append("stored")

    worker: threading.Thread | None = None
    try:
        with factory() as first:
            lock_activity_write_plane(first)
            worker = threading.Thread(target=write_ios_status)
            worker.start()
            assert worker_started.wait(timeout=5)

            worker.join(timeout=0.2)
            assert worker.is_alive()
            assert first.scalar(
                sa.text(
                    "SELECT pg_try_advisory_xact_lock("
                    "hashtextextended(:control_key, 0)"
                    ")"
                ),
                {
                    "control_key": (
                        f"healthmes:activity:control:{device_id}"
                    )
                },
            )
            first.commit()
            worker.join(timeout=5)

        assert worker is not None
        assert not worker.is_alive()
        assert failures == []
        assert outcome == ["stored"]
    finally:
        if worker is not None:
            worker.join(timeout=5)
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("HEALTHMES_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL URL in HEALTHMES_TEST_POSTGRES_URL",
)
def test_control_delete_waits_for_prior_writer_and_removes_its_row() -> None:
    database_url = os.environ["HEALTHMES_TEST_POSTGRES_URL"]
    admin_engine = create_db_engine(database_url)
    schema = f"hm_test_{uuid.uuid4().hex}"
    with admin_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

    engine = create_db_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    device_id = "control-delete-order"
    worker_started = threading.Event()
    deleted_counts: list[int] = []
    failures: list[BaseException] = []

    def delete_control() -> None:
        with factory() as session:
            worker_started.set()
            try:
                report = delete_activity_data(
                    session,
                    device_id=device_id,
                    start=None,
                    end=None,
                    include_summaries=False,
                    include_control=True,
                    now=datetime(2026, 8, 1, 11, tzinfo=UTC),
                )
                session.commit()
            except BaseException as exc:
                session.rollback()
                failures.append(exc)
            else:
                deleted_counts.append(report.control_events_deleted)

    worker: threading.Thread | None = None
    try:
        with factory() as writer:
            lock_activity_write_plane(writer)
            update_collection_config(
                writer,
                device_id,
                ActivityCollectionUpdate(enabled=False),
                now=datetime(2026, 8, 1, 10, tzinfo=UTC),
            )

            worker = threading.Thread(target=delete_control)
            worker.start()
            assert worker_started.wait(timeout=5)

            worker.join(timeout=0.2)
            assert worker.is_alive()
            writer.commit()
            worker.join(timeout=5)

        assert worker is not None
        assert not worker.is_alive()
        assert failures == []
        assert deleted_counts == [1]
        with factory() as session:
            assert (
                session.scalar(
                    sa.select(WellnessEvent).where(
                        WellnessEvent.event_type.in_(
                            CONTROL_EVENT_TYPES
                        ),
                        WellnessEvent.source_device == device_id,
                    )
                )
                is None
            )
    finally:
        if worker is not None:
            worker.join(timeout=5)
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()
