from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from healthmes.nutrition.ledger_lock import lock_nutrition_ledger
from healthmes.storage.retention_lock import lock_retention_policies
from healthmes.store import Base, create_db_engine

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PROCESS_LOCK_SCRIPT = """
import sys
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from healthmes.nutrition.ledger_lock import lock_nutrition_ledger
from healthmes.storage.retention_lock import lock_retention_policies
from healthmes.store import create_db_engine

database_url, lock_kind, attempted_path, acquired_path = sys.argv[1:]
engine = create_db_engine(database_url)
factory = sessionmaker(bind=engine, expire_on_commit=False)
try:
    with factory() as session:
        Path(attempted_path).touch()
        if lock_kind == "nutrition":
            lock_nutrition_ledger(session)
        else:
            lock_retention_policies(session, {"media": 7})
        Path(acquired_path).touch()
        session.commit()
finally:
    engine.dispose()
"""


def _acquire_lock(session: Session, lock_kind: str) -> None:
    if lock_kind == "nutrition":
        lock_nutrition_ledger(session)
    else:
        lock_retention_policies(session, {"media": 7})


def _wait_for_sentinel(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.01)
    return path.exists()


@pytest.mark.parametrize("lock_kind", ["nutrition", "retention"])
def test_sqlite_file_lock_serializes_separate_engines(
    tmp_path,
    lock_kind: str,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path}/shared-healthmes.db"
    first_engine = create_db_engine(database_url)
    second_engine = create_db_engine(database_url)
    Base.metadata.create_all(first_engine)
    first_factory = sessionmaker(bind=first_engine, expire_on_commit=False)
    second_factory = sessionmaker(bind=second_engine, expire_on_commit=False)
    attempted = threading.Event()
    acquired = threading.Event()
    errors: list[BaseException] = []

    def wait_for_lock() -> None:
        try:
            with second_factory() as session:
                attempted.set()
                _acquire_lock(session, lock_kind)
                acquired.set()
                session.commit()
        except BaseException as exc:
            errors.append(exc)

    try:
        with first_factory() as holder:
            _acquire_lock(holder, lock_kind)
            worker = threading.Thread(target=wait_for_lock)
            worker.start()
            assert attempted.wait(timeout=2)
            assert not acquired.wait(timeout=0.2)
            holder.commit()
            worker.join(timeout=5)

        assert not worker.is_alive()
        assert acquired.is_set()
        assert errors == []
    finally:
        first_engine.dispose()
        second_engine.dispose()


@pytest.mark.parametrize("lock_kind", ["nutrition", "retention"])
def test_sqlite_file_lock_serializes_separate_processes(
    tmp_path,
    lock_kind: str,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path}/shared-process-healthmes.db"
    engine = create_db_engine(database_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    attempted = tmp_path / f"{lock_kind}-attempted"
    acquired = tmp_path / f"{lock_kind}-acquired"
    worker = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _PROCESS_LOCK_SCRIPT,
            database_url,
            lock_kind,
            str(attempted),
            str(acquired),
        ],
        cwd=_PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        with factory() as holder:
            _acquire_lock(holder, lock_kind)
            assert _wait_for_sentinel(attempted, timeout=5)
            assert not _wait_for_sentinel(acquired, timeout=0.3)
            holder.commit()
            stdout, stderr = worker.communicate(timeout=10)

        assert worker.returncode == 0, f"stdout:\n{stdout}\nstderr:\n{stderr}"
        assert acquired.exists()
    finally:
        if worker.poll() is None:
            worker.terminate()
            try:
                worker.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.communicate()
        engine.dispose()
