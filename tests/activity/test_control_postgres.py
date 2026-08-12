from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

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
from healthmes.activity.locking import lock_activity_write_plane
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
    application = create_app(settings)

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
    application = create_app(settings)

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
    worker_pid: list[int] = []
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
            worker_pid.append(
                int(session.scalar(sa.text("SELECT pg_backend_pid()")))
            )
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

            deadline = time.monotonic() + 5
            waiting_for_advisory_lock = False
            while time.monotonic() < deadline and worker.is_alive():
                wait_event = writer.execute(
                    sa.text(
                        "SELECT wait_event_type, wait_event "
                        "FROM pg_stat_activity WHERE pid = :pid"
                    ),
                    {"pid": worker_pid[0]},
                ).one_or_none()
                if wait_event is not None and tuple(wait_event) == (
                    "Lock",
                    "advisory",
                ):
                    waiting_for_advisory_lock = True
                    break
                time.sleep(0.05)

            assert waiting_for_advisory_lock
            assert not network_called.is_set()
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
    worker_pid: list[int] = []
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
            worker_pid.append(
                int(session.scalar(sa.text("SELECT pg_backend_pid()")))
            )
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

            deadline = time.monotonic() + 5
            waiting_for_advisory_lock = False
            while time.monotonic() < deadline and worker.is_alive():
                wait_event = first.execute(
                    sa.text(
                        "SELECT wait_event_type, wait_event "
                        "FROM pg_stat_activity WHERE pid = :pid"
                    ),
                    {"pid": worker_pid[0]},
                ).one_or_none()
                if wait_event is not None and tuple(wait_event) == (
                    "Lock",
                    "advisory",
                ):
                    waiting_for_advisory_lock = True
                    break
                time.sleep(0.05)

            assert waiting_for_advisory_lock
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
    worker_pid: list[int] = []
    worker_started = threading.Event()
    outcome: list[str] = []
    failures: list[BaseException] = []

    def write_ios_status() -> None:
        with factory() as session:
            worker_pid.append(
                int(session.scalar(sa.text("SELECT pg_backend_pid()")))
            )
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

            deadline = time.monotonic() + 5
            waiting_for_write_plane = False
            while time.monotonic() < deadline and worker.is_alive():
                wait_event = first.execute(
                    sa.text(
                        "SELECT wait_event_type, wait_event "
                        "FROM pg_stat_activity WHERE pid = :pid"
                    ),
                    {"pid": worker_pid[0]},
                ).one_or_none()
                if wait_event is not None and tuple(wait_event) == (
                    "Lock",
                    "advisory",
                ):
                    waiting_for_write_plane = True
                    break
                time.sleep(0.05)

            assert waiting_for_write_plane
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
    worker_pid: list[int] = []
    worker_started = threading.Event()
    deleted_counts: list[int] = []
    failures: list[BaseException] = []

    def delete_control() -> None:
        with factory() as session:
            worker_pid.append(
                int(session.scalar(sa.text("SELECT pg_backend_pid()")))
            )
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

            deadline = time.monotonic() + 5
            waiting_for_write_plane = False
            while time.monotonic() < deadline and worker.is_alive():
                wait_event = writer.execute(
                    sa.text(
                        "SELECT wait_event_type, wait_event "
                        "FROM pg_stat_activity WHERE pid = :pid"
                    ),
                    {"pid": worker_pid[0]},
                ).one_or_none()
                if wait_event is not None and tuple(wait_event) == (
                    "Lock",
                    "advisory",
                ):
                    waiting_for_write_plane = True
                    break
                time.sleep(0.05)

            assert waiting_for_write_plane
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
