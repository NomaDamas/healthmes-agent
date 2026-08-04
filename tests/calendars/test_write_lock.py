from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import sessionmaker

from healthmes.calendars.write_lock import calendar_write_lock
from healthmes.store import CalendarSource, create_db_engine


def test_sqlite_calendar_write_lock_serializes_process_writers(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'calendar-lock.db'}")
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    first_entered = threading.Event()
    release_first = threading.Event()
    timeline: list[str] = []

    def first_writer() -> None:
        with factory() as session, calendar_write_lock(
            session,
            CalendarSource.GOOGLE,
        ):
            timeline.append("first-enter")
            first_entered.set()
            assert release_first.wait(timeout=5)
            timeline.append("first-exit")

    def second_writer() -> None:
        assert first_entered.wait(timeout=5)
        with factory() as session, calendar_write_lock(
            session,
            CalendarSource.GOOGLE,
        ):
            timeline.append("second-enter")

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(first_writer)
            second = pool.submit(second_writer)
            assert first_entered.wait(timeout=5)
            assert timeline == ["first-enter"]
            release_first.set()
            first.result(timeout=5)
            second.result(timeout=5)
        assert timeline == ["first-enter", "first-exit", "second-enter"]
    finally:
        engine.dispose()
