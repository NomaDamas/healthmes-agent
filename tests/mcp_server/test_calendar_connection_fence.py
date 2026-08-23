from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from healthmes.calendars import creds
from healthmes.calendars.base import CalendarAuthError, ExternalEvent
from healthmes.mcp_server import server as server_module
from healthmes.store import Base, CalendarSource, create_db_engine


def _write_google_token(data_dir, refresh_token: str) -> None:
    path = data_dir / "google" / "calendar_token.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "authorized_user",
                "refresh_token": refresh_token,
                "client_id": "client-id",
                "client_secret": "client-secret",
            }
        ),
        encoding="utf-8",
    )


def _event(external_id: str) -> ExternalEvent:
    start = datetime(2026, 8, 12, 9, tzinfo=UTC)
    return ExternalEvent(
        external_id=external_id,
        summary="Recovery focus",
        start_at=start,
        end_at=start + timedelta(hours=1),
        etag='"etag-v1"',
    )


def test_lazy_google_writer_fences_disconnect_and_rebuilds_after_reconnect(
    settings,
    tmp_path,
    monkeypatch,
) -> None:
    database_url = (
        f"sqlite+pysqlite:///{tmp_path / 'adjustment-writer-fence.db'}"
    )
    engine = create_db_engine(database_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    active_settings = settings.model_copy(
        update={
            "database_url": database_url,
            "data_dir": tmp_path / "data",
        }
    )
    _write_google_token(active_settings.data_dir, "refresh-token-1")
    read_started = threading.Event()
    release_read = threading.Event()
    disconnect_attempted = threading.Event()
    disconnect_completed = threading.Event()
    failures: list[BaseException] = []

    class Backend:
        source = CalendarSource.GOOGLE

        def __init__(self, name: str, *, block: bool = False) -> None:
            self.name = name
            self.block = block
            self.reads: list[str] = []

        def read_event(self, external_id: str) -> ExternalEvent:
            self.reads.append(external_id)
            if self.block:
                read_started.set()
                assert release_read.wait(timeout=5)
            return _event(f"{self.name}:{external_id}")

        def apply_confirmed_external_time_change(self, change):
            raise AssertionError("not used by this test")

    backends = [
        Backend("generation-1", block=True),
        Backend("generation-2"),
    ]
    factory_calls = 0

    def build_backend(*_args, **_kwargs):
        nonlocal factory_calls
        backend = backends[factory_calls]
        factory_calls += 1
        return backend

    monkeypatch.setattr(
        server_module.GoogleCalendarBackend,
        "from_data_dir",
        build_backend,
    )
    server_module.set_session_factory(factory)
    writer = server_module._LazyGoogleAdjustmentWriter(active_settings)
    results: list[ExternalEvent] = []

    def read() -> None:
        try:
            results.append(writer.read_event("target"))
        except BaseException as exc:
            failures.append(exc)

    def disconnect() -> None:
        try:
            disconnect_attempted.set()
            with factory() as session:
                with creds.calendar_connection_write(
                    session,
                    CalendarSource.GOOGLE,
                ):
                    assert creds.delete_google_token(
                        active_settings.data_dir
                    )
            disconnect_completed.set()
        except BaseException as exc:
            failures.append(exc)

    read_thread = threading.Thread(target=read)
    disconnect_thread = threading.Thread(target=disconnect)
    try:
        read_thread.start()
        assert read_started.wait(timeout=5)
        disconnect_thread.start()
        assert disconnect_attempted.wait(timeout=5)
        time.sleep(0.1)
        assert not disconnect_completed.is_set()

        release_read.set()
        read_thread.join(timeout=10)
        disconnect_thread.join(timeout=10)

        assert not read_thread.is_alive()
        assert not disconnect_thread.is_alive()
        assert failures == []
        assert disconnect_completed.is_set()
        assert [event.external_id for event in results] == [
            "generation-1:target"
        ]
        assert factory_calls == 1
        assert backends[0].reads == ["target"]

        with pytest.raises(CalendarAuthError):
            writer.read_event("must-not-reach-stale-backend")
        assert factory_calls == 1
        assert backends[0].reads == ["target"]

        with factory() as session:
            with creds.calendar_connection_write(
                session,
                CalendarSource.GOOGLE,
            ):
                _write_google_token(
                    active_settings.data_dir,
                    "refresh-token-2",
                )
        refreshed = writer.read_event("after-reconnect")
        assert refreshed.external_id == "generation-2:after-reconnect"
        assert factory_calls == 2
        assert backends[0].reads == ["target"]
        assert backends[1].reads == ["after-reconnect"]
    finally:
        release_read.set()
        read_thread.join(timeout=5)
        disconnect_thread.join(timeout=5)
        server_module.reset_runtime_state()
        engine.dispose()
