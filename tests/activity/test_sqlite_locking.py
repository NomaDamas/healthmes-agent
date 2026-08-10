from __future__ import annotations

import threading
import time

from sqlalchemy import text
from sqlalchemy.orm import Session

from healthmes.activity.locking import lock_activity_write_plane
from healthmes.store.session import create_db_engine


def test_file_sqlite_activity_lock_lasts_until_transaction_end(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'activity.db'}")
    first = Session(engine)
    second = Session(engine)
    acquired = threading.Event()

    lock_activity_write_plane(first)
    first.execute(text("SELECT 1"))

    def acquire_second() -> None:
        lock_activity_write_plane(second)
        second.execute(text("SELECT 1"))
        acquired.set()
        second.rollback()

    thread = threading.Thread(target=acquire_second, daemon=True)
    thread.start()
    time.sleep(0.1)
    assert not acquired.is_set()

    first.rollback()
    thread.join(timeout=2)

    assert acquired.is_set()
    assert not thread.is_alive()
    first.close()
    second.close()
    engine.dispose()


def test_file_sqlite_engine_enables_wal_and_busy_timeout(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'activity.db'}")
    with engine.connect() as connection:
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
        busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()

    assert str(journal_mode).casefold() == "wal"
    assert int(busy_timeout) == 30_000
    engine.dispose()
