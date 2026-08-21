"""Cross-process serialization for restore and startup recovery admission."""

import multiprocessing
import os
from pathlib import Path

import pytest

from healthmes.backup import snapshot as snapshot_mod
from healthmes.backup.provider import BackupError
from healthmes.backup.snapshot import (
    DataLocations,
    recovered_runtime_guard,
    restore_admission_guard,
)


def _locations(root: Path) -> DataLocations:
    return DataLocations(
        database_url=f"sqlite:///{root / 'data' / 'healthmes.db'}",
        restore_state_dir=root / ".restore",
    )


def _hold_admission(locations: DataLocations, acquired, release) -> None:
    with restore_admission_guard(locations):
        acquired.set()
        if not release.wait(timeout=10):
            raise TimeoutError("timed out waiting for admission release")


def test_restore_admission_serializes_independent_processes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    locations = _locations(tmp_path)
    monkeypatch.setattr(
        "healthmes.backup.snapshot._RESTORE_ADMISSION_LOCK_TIMEOUT_SECONDS",
        0.1,
    )
    context = multiprocessing.get_context("spawn")
    acquired = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_admission,
        args=(locations, acquired, release),
    )
    holder.start()
    try:
        assert acquired.wait(timeout=5)
        with pytest.raises(BackupError, match="admission lock"):
            with restore_admission_guard(locations):
                pytest.fail("restore admission unexpectedly acquired")
        release.set()
        holder.join(timeout=5)
        assert holder.exitcode == 0
        with restore_admission_guard(locations):
            pass
    finally:
        release.set()
        holder.join(timeout=5)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5)


def test_startup_runtime_seam_cannot_enter_during_restore(
    tmp_path: Path,
    monkeypatch,
) -> None:
    locations = _locations(tmp_path)
    monkeypatch.setattr(
        "healthmes.backup.snapshot._RESTORE_ADMISSION_LOCK_TIMEOUT_SECONDS",
        0.1,
    )
    context = multiprocessing.get_context("spawn")
    acquired = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_admission,
        args=(locations, acquired, release),
    )
    holder.start()
    try:
        assert acquired.wait(timeout=5)
        with pytest.raises(BackupError, match="admission lock"):
            with recovered_runtime_guard(locations):
                pytest.fail("startup runtime guard unexpectedly acquired")
    finally:
        release.set()
        holder.join(timeout=5)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5)
    assert holder.exitcode == 0


def test_restore_admission_preserves_oserror_from_guarded_operation(
    tmp_path: Path,
) -> None:
    locations = _locations(tmp_path)

    with pytest.raises(OSError, match="injected guarded operation failure"):
        with restore_admission_guard(locations):
            raise OSError("injected guarded operation failure")


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symlinks")
def test_default_sqlite_admission_lock_survives_database_parent_replacement(
    tmp_path: Path,
) -> None:
    root = tmp_path / "node"
    data_parent = root / "data"
    data_parent.mkdir(parents=True)
    locations = DataLocations(
        database_url=f"sqlite:///{data_parent / 'healthmes.db'}",
    )

    before = snapshot_mod._restore_admission_lock_path(locations)
    assert not before.is_relative_to(data_parent)

    displaced = root / "original-data"
    attacker = root / "attacker"
    attacker.mkdir()
    data_parent.rename(displaced)
    data_parent.symlink_to(attacker, target_is_directory=True)

    after = snapshot_mod._restore_admission_lock_path(locations)

    assert after == before
    assert list(attacker.iterdir()) == []


def test_default_sqlite_restore_state_is_outside_database_parent(
    tmp_path: Path,
) -> None:
    data_parent = tmp_path / "node" / "data"
    locations = DataLocations(
        database_url=f"sqlite:///{data_parent / 'healthmes.db'}",
    )

    state_dir = snapshot_mod._restore_state_directory(locations)

    assert not state_dir.is_relative_to(data_parent)
    assert state_dir.parent.parent == data_parent.parent


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symlinks")
def test_restore_admission_rejects_symlink_lock_root(tmp_path: Path) -> None:
    locations = _locations(tmp_path)
    lock_path = snapshot_mod._restore_admission_lock_path(locations)
    attacker_directory = tmp_path / "attacker-lock-root"
    attacker_directory.mkdir()
    lock_path.parent.symlink_to(attacker_directory, target_is_directory=True)

    with pytest.raises(BackupError, match="not a real directory"):
        with restore_admission_guard(locations):
            pytest.fail("symlinked admission lock root was accepted")


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symlinks")
def test_restore_admission_rejects_precreated_lock_symlink(
    tmp_path: Path,
) -> None:
    locations = _locations(tmp_path)
    lock_path = snapshot_mod._restore_admission_lock_path(locations)
    lock_path.parent.mkdir(mode=0o700)
    attacker_file = tmp_path / "attacker.lock"
    attacker_file.write_text("do not modify", encoding="utf-8")
    lock_path.symlink_to(attacker_file)

    with pytest.raises(BackupError, match="not a regular file"):
        with restore_admission_guard(locations):
            pytest.fail("symlinked admission lock file was accepted")

    assert attacker_file.read_text(encoding="utf-8") == "do not modify"
