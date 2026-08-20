"""Crash-state coverage for the durable local restore journal."""

import json
import os
import sqlite3
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread

import pytest

from healthmes.activity.locking import sqlite_runtime_guard
from healthmes.backup import recovery as recovery_mod
from healthmes.backup import snapshot as snapshot_mod
from healthmes.backup.provider import BackupError
from healthmes.backup.recovery import (
    JournalOperation,
    JournalPostgresTarget,
    RestoreJournal,
    restore_journal_path,
    write_restore_journal,
)
from healthmes.backup.snapshot import (
    DataLocations,
    create_snapshot,
    recover_incomplete_restore,
    restore_snapshot,
)

TRANSACTION_ID = "0123456789abcdef0123456789abcdef"


def _empty_journal() -> RestoreJournal:
    return RestoreJournal(
        transaction_id=TRANSACTION_ID,
        phase="staging",
        recovery_mode="recoverable_local_swaps",
        operations=[],
        postgres_targets=[],
    )


def _sqlite_locations(root: Path) -> DataLocations:
    return DataLocations(
        database_url=f"sqlite:///{root / 'data' / 'healthmes.db'}",
        restore_state_dir=root / ".restore",
    )


def _operation(
    locations: DataLocations,
    *,
    state: str,
    original_existed: bool | None,
) -> JournalOperation:
    destination = snapshot_mod._sqlite_file_path(locations.database_url).resolve()
    return JournalOperation(
        component="healthmes_db",
        destination=destination,
        staged=destination.with_name(
            f".{destination.name}.healthmes-restore-{TRANSACTION_ID}.staged"
        ),
        backup=destination.with_name(
            f".{destination.name}.healthmes-restore-{TRANSACTION_ID}.backup"
        ),
        original_existed=original_existed,
        state=state,
    )


def _anchor_operations(
    operations: list[JournalOperation],
) -> list[JournalOperation]:
    for operation in operations:
        parent = operation.destination.parent
        parent.mkdir(parents=True, exist_ok=True)
        metadata = parent.stat(follow_symlinks=False)
        operation.parent_device = metadata.st_dev
        operation.parent_inode = metadata.st_ino
    return operations


def _record_operation_identities(
    operations: list[JournalOperation],
) -> list[JournalOperation]:
    _anchor_operations(operations)
    journal = RestoreJournal(
        transaction_id=TRANSACTION_ID,
        phase="staging",
        recovery_mode="recoverable_local_swaps",
        operations=operations,
        postgres_targets=[],
    )
    swaps = snapshot_mod._journal_operations_as_swaps(journal)
    with snapshot_mod._operation_parent_scope(swaps, create=False):
        for journal_operation, operation in zip(
            operations,
            swaps,
            strict=True,
        ):
            if journal_operation.staged is None:
                continue
            staged_identity = snapshot_mod._capture_operation_entry_identity(
                operation,
                journal_operation.staged,
                is_tree=operation.is_tree,
            )
            destination_identity = (
                snapshot_mod._capture_operation_entry_identity(
                    operation,
                    journal_operation.destination,
                    is_tree=operation.is_tree,
                )
            )
            backup_identity = (
                snapshot_mod._capture_operation_entry_identity(
                    operation,
                    journal_operation.backup,
                    is_tree=operation.is_tree,
                )
            )
            replacement_identity = staged_identity
            if (
                replacement_identity is None
                and journal_operation.state
                in {"applying", "applied", "rolling_back"}
            ):
                replacement_identity = destination_identity
            journal_operation.staged_identity = replacement_identity
            journal_operation.applied_identity = replacement_identity
            if journal_operation.original_existed:
                journal_operation.rollback_identity = backup_identity
                if (
                    journal_operation.rollback_identity is None
                    and (
                        journal_operation.state == "pending"
                        or staged_identity is not None
                    )
                ):
                    journal_operation.rollback_identity = (
                        destination_identity
                    )
    return operations


def _operations(
    locations: DataLocations,
    *,
    main_state: str,
    main_original_existed: bool | None,
) -> tuple[list[JournalOperation], JournalOperation]:
    destination = snapshot_mod._sqlite_file_path(locations.database_url).resolve()
    if main_original_existed is None:
        sidecar_state = "pending"
        sidecar_original_existed = None
    elif main_state == "applying":
        sidecar_state = "applied"
        sidecar_original_existed = False
    elif main_state == "rolling_back":
        sidecar_state = "rolled_back"
        sidecar_original_existed = False
    else:
        sidecar_state = main_state
        sidecar_original_existed = False
    operations = [
        JournalOperation(
            component="healthmes_db",
            destination=Path(f"{destination}{suffix}"),
            staged=None,
            backup=Path(f"{destination}{suffix}").with_name(
                f".{destination.name}{suffix}.healthmes-restore-"
                f"{TRANSACTION_ID}.backup"
            ),
            original_existed=sidecar_original_existed,
            state=sidecar_state,
        )
        for suffix in ("-wal", "-shm", "-journal")
    ]
    main = _operation(
        locations,
        state=main_state,
        original_existed=main_original_existed,
    )
    operations.append(main)
    return operations, main


def _write_journal(
    locations: DataLocations,
    *,
    phase: str,
    operations: list[JournalOperation],
    postgres_targets: list[JournalPostgresTarget] | None = None,
) -> Path:
    path = restore_journal_path(locations.restore_state_dir or Path())
    write_restore_journal(
        path,
        RestoreJournal(
            transaction_id=TRANSACTION_ID,
            phase=phase,
            recovery_mode="recoverable_local_swaps",
            operations=_record_operation_identities(operations),
            postgres_targets=postgres_targets or [],
        ),
    )
    return path


def test_journal_directory_io_failure_uses_backup_error_contract(
    tmp_path: Path,
) -> None:
    blocked = tmp_path / "restore-state-is-a-file"
    blocked.write_text("not a directory", encoding="utf-8")

    with pytest.raises(
        BackupError,
        match="could not prepare restore journal directory",
    ):
        write_restore_journal(
            blocked / "pending.json",
            _empty_journal(),
        )


def test_journal_open_failure_uses_backup_error_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    journal_path = tmp_path / ".restore" / "pending.json"
    real_open = Path.open

    def fail_temporary_open(path, *args, **kwargs):
        if path.name.startswith(".pending.json."):
            raise OSError("injected journal open failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_temporary_open)

    with pytest.raises(
        BackupError,
        match="injected journal open failure",
    ):
        write_restore_journal(journal_path, _empty_journal())


def test_legacy_deterministic_temp_does_not_block_journal_rewrite(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / ".restore" / "pending.json"
    journal_path.parent.mkdir(parents=True)
    legacy_temp = journal_path.with_name(
        f".{journal_path.name}.{TRANSACTION_ID}.tmp"
    )
    legacy_temp.write_text("stale", encoding="utf-8")

    write_restore_journal(journal_path, _empty_journal())

    assert journal_path.is_file()
    assert legacy_temp.read_text(encoding="utf-8") == "stale"


def test_journal_cleanup_failure_preserves_original_replace_error(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    journal_path = tmp_path / ".restore" / "pending.json"
    real_unlink = Path.unlink

    def fail_replace(_source, _destination):
        raise OSError("injected journal replace failure")

    def fail_temporary_cleanup(path, *args, **kwargs):
        if path.name.startswith(".pending.json."):
            raise OSError("injected temporary cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(recovery_mod.os, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_temporary_cleanup)

    with caplog.at_level(
        "WARNING",
        logger="healthmes.backup.recovery",
    ), pytest.raises(
        BackupError,
        match="injected journal replace failure",
    ):
        write_restore_journal(journal_path, _empty_journal())

    assert "Could not remove temporary restore journal" in caplog.text
    assert "injected temporary cleanup failure" in caplog.text


def test_failure_path_journal_cleanup_is_best_effort(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    journal_path = tmp_path / ".restore" / "pending.json"

    def fail_remove(_path):
        raise BackupError("injected journal removal failure")

    monkeypatch.setattr(
        snapshot_mod,
        "remove_restore_journal",
        fail_remove,
    )

    with caplog.at_level(
        "WARNING",
        logger="healthmes.backup.snapshot",
    ):
        snapshot_mod._remove_restore_journal_without_masking(
            journal_path
        )

    assert "preserving the original restore failure" in caplog.text
    assert "injected journal removal failure" in caplog.text


def test_staging_crash_removes_unapplied_staged_file(tmp_path: Path) -> None:
    locations = _sqlite_locations(tmp_path)
    operations, operation = _operations(
        locations,
        main_state="pending",
        main_original_existed=None,
    )
    assert operation.staged is not None
    operation.staged.parent.mkdir(parents=True)
    operation.staged.write_bytes(b"decrypted restore")
    operation.destination.parent.mkdir(parents=True, exist_ok=True)
    operation.destination.write_bytes(b"live")
    journal_path = _write_journal(
        locations,
        phase="prepared",
        operations=operations,
    )

    recover_incomplete_restore(locations)

    assert operation.destination.read_bytes() == b"live"
    assert not operation.staged.exists()
    assert not journal_path.exists()


@pytest.mark.parametrize("original_existed", [True, False])
def test_applying_crash_restores_or_removes_new_generation(
    tmp_path: Path,
    original_existed: bool,
) -> None:
    locations = _sqlite_locations(tmp_path)
    operations, operation = _operations(
        locations,
        main_state="applying",
        main_original_existed=original_existed,
    )
    operation.destination.parent.mkdir(parents=True)
    if original_existed:
        operation.backup.write_bytes(b"original")
    operation.destination.write_bytes(b"replacement")
    journal_path = _write_journal(
        locations,
        phase="applying_local",
        operations=operations,
    )

    recover_incomplete_restore(locations)

    if original_existed:
        assert operation.destination.read_bytes() == b"original"
    else:
        assert not operation.destination.exists()
    assert not operation.backup.exists()
    assert not journal_path.exists()


def test_rollback_crash_after_original_was_restored_is_idempotent(
    tmp_path: Path,
) -> None:
    locations = _sqlite_locations(tmp_path)
    operations, operation = _operations(
        locations,
        main_state="rolling_back",
        main_original_existed=True,
    )
    operation.destination.parent.mkdir(parents=True)
    operation.destination.write_bytes(b"original")
    assert operation.staged is not None
    operation.staged.write_bytes(b"staged")
    journal_path = _write_journal(
        locations,
        phase="rolling_back",
        operations=operations,
    )

    recover_incomplete_restore(locations)

    assert operation.destination.read_bytes() == b"original"
    assert not operation.staged.exists()
    assert not journal_path.exists()


def test_committed_crash_only_cleans_rollback_artifacts(tmp_path: Path) -> None:
    locations = _sqlite_locations(tmp_path)
    operations, operation = _operations(
        locations,
        main_state="applied",
        main_original_existed=True,
    )
    operation.destination.parent.mkdir(parents=True)
    operation.destination.write_bytes(b"restored")
    operation.backup.write_bytes(b"old")
    journal_path = _write_journal(
        locations,
        phase="committed",
        operations=operations,
    )

    recover_incomplete_restore(locations)

    assert operation.destination.read_bytes() == b"restored"
    assert not operation.backup.exists()
    assert not operation.staged.exists()
    assert not journal_path.exists()


def test_local_only_restore_cleanup_failure_keeps_committed_generation(
    source_env,
    fresh_locations,
    tmp_path,
    monkeypatch,
) -> None:
    snapshot_path = tmp_path / "local-only-restore.age"
    create_snapshot(
        source_env.locations,
        passphrase=source_env.passphrase,
        out_path=snapshot_path,
        created_at=datetime(2026, 8, 18, 3, 30, tzinfo=UTC),
    )
    locations, _root = fresh_locations("local-only-cleanup-failure")
    locations.media_dir.mkdir(parents=True)
    (locations.media_dir / "note.txt").write_text(
        "old generation",
        encoding="utf-8",
    )
    journal_path = restore_journal_path(
        snapshot_mod._restore_state_directory(locations)
    )
    real_cleanup = snapshot_mod._cleanup_restore_artifacts

    def crash_after_commit(operations, *, preserve_backups=False):
        del operations, preserve_backups
        journal = recovery_mod.load_restore_journal(journal_path)
        assert journal is not None
        assert journal.phase == "committed"
        raise KeyboardInterrupt("injected cleanup crash")

    monkeypatch.setattr(
        snapshot_mod,
        "_cleanup_restore_artifacts",
        crash_after_commit,
    )
    with pytest.raises(KeyboardInterrupt, match="injected cleanup crash"):
        restore_snapshot(
            snapshot_path,
            passphrase=source_env.passphrase,
            locations=locations,
        )

    assert (locations.media_dir / "note.txt").read_text(
        encoding="utf-8"
    ) == "voice memo transcript\n"
    assert journal_path.is_file()

    monkeypatch.setattr(
        snapshot_mod,
        "_cleanup_restore_artifacts",
        real_cleanup,
    )
    recover_incomplete_restore(locations)

    assert (locations.media_dir / "note.txt").read_text(
        encoding="utf-8"
    ) == "voice memo transcript\n"
    assert not journal_path.exists()


def test_journal_rejects_integer_disguised_as_boolean(tmp_path: Path) -> None:
    locations = _sqlite_locations(tmp_path)
    operations, _operation_under_test = _operations(
        locations,
        main_state="applying",
        main_original_existed=True,
    )
    journal_path = _write_journal(
        locations,
        phase="applying_local",
        operations=operations,
    )
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    payload["operations"][-1]["original_existed"] = 1
    journal_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BackupError, match="original_existed is invalid"):
        recover_incomplete_restore(locations)


def test_postgres_interrupted_restore_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    locations = DataLocations(
        database_url="postgresql://healthmes@localhost/healthmes",
        restore_state_dir=tmp_path / ".restore",
    )
    journal_path = restore_journal_path(locations.restore_state_dir)
    write_restore_journal(
        journal_path,
        RestoreJournal(
            transaction_id=TRANSACTION_ID,
            phase="postgres_in_progress",
            recovery_mode="single_postgres_transaction",
            operations=[],
            postgres_targets=[
                JournalPostgresTarget(
                    component="healthmes_db",
                    expected_system_identifier="123",
                    expected_database_oid=16384,
                    state="applying",
                )
            ],
            current_postgres="healthmes_db",
        ),
    )
    monkeypatch.setattr(
        snapshot_mod,
        "global_write_plane_guard",
        lambda _database_url: nullcontext(),
    )

    with pytest.raises(BackupError, match="may have changed PostgreSQL"):
        recover_incomplete_restore(locations)

    assert journal_path.exists()


def test_fully_committed_postgres_journal_is_cleaned(
    tmp_path: Path,
    monkeypatch,
) -> None:
    locations = DataLocations(
        database_url="postgresql://healthmes@localhost/healthmes",
        restore_state_dir=tmp_path / ".restore",
    )
    journal_path = restore_journal_path(locations.restore_state_dir)
    write_restore_journal(
        journal_path,
        RestoreJournal(
            transaction_id=TRANSACTION_ID,
            phase="committed",
            recovery_mode="single_postgres_transaction",
            operations=[],
            postgres_targets=[
                JournalPostgresTarget(
                    component="healthmes_db",
                    expected_system_identifier="123",
                    expected_database_oid=16384,
                    state="committed",
                )
            ],
        ),
    )
    monkeypatch.setattr(
        snapshot_mod,
        "global_write_plane_guard",
        lambda _database_url: nullcontext(),
    )

    recover_incomplete_restore(locations)

    assert not journal_path.exists()


def test_uncertain_postgres_recovery_rolls_back_local_components_first(
    tmp_path: Path,
) -> None:
    sqlite_locations = _sqlite_locations(tmp_path)
    locations = DataLocations(
        database_url=sqlite_locations.database_url,
        ow_database_url="postgresql://open-wearables@localhost/open_wearables",
        restore_state_dir=sqlite_locations.restore_state_dir,
    )
    operations, operation = _operations(
        locations,
        main_state="applied",
        main_original_existed=True,
    )
    operation.destination.parent.mkdir(parents=True)
    operation.destination.write_bytes(b"replacement")
    operation.backup.write_bytes(b"original")
    journal_path = restore_journal_path(locations.restore_state_dir or Path())
    write_restore_journal(
        journal_path,
        RestoreJournal(
            transaction_id=TRANSACTION_ID,
            phase="manual_recovery_required",
            recovery_mode="operator_approved_cross_store_partial",
            operations=_record_operation_identities(operations),
            postgres_targets=[
                JournalPostgresTarget(
                    component="open_wearables_db",
                    expected_system_identifier="123",
                    expected_database_oid=16384,
                    state="unknown",
                )
            ],
        ),
    )

    with pytest.raises(
        BackupError,
        match="may have changed PostgreSQL; local components were rolled back",
    ):
        recover_incomplete_restore(locations)

    assert operation.destination.read_bytes() == b"original"
    assert not operation.backup.exists()
    recovered = snapshot_mod.load_restore_journal(journal_path)
    assert recovered is not None
    assert recovered.phase == "manual_recovery_required"
    assert all(item.state == "rolled_back" for item in recovered.operations)


def test_journal_rejects_incomplete_sqlite_operation_set(tmp_path: Path) -> None:
    locations = _sqlite_locations(tmp_path)
    operation = _operation(
        locations,
        state="pending",
        original_existed=None,
    )
    journal_path = _write_journal(
        locations,
        phase="prepared",
        operations=[operation],
    )

    with pytest.raises(BackupError, match="operation set or order"):
        recover_incomplete_restore(locations)

    assert journal_path.exists()


def test_staging_phase_rejects_recorded_live_target_state(
    tmp_path: Path,
) -> None:
    locations = _sqlite_locations(tmp_path)
    operations, operation = _operations(
        locations,
        main_state="pending",
        main_original_existed=True,
    )
    assert operation.staged is not None
    operation.destination.parent.mkdir(parents=True)
    operation.destination.write_bytes(b"original")
    operation.staged.write_bytes(b"replacement")
    journal_path = _write_journal(
        locations,
        phase="prepared",
        operations=operations,
    )

    with pytest.raises(BackupError, match="before mutation"):
        recover_incomplete_restore(locations)

    assert journal_path.exists()


def test_committed_missing_live_target_preserves_rollback_copy(
    tmp_path: Path,
) -> None:
    locations = _sqlite_locations(tmp_path)
    operations, operation = _operations(
        locations,
        main_state="applied",
        main_original_existed=True,
    )
    operation.backup.parent.mkdir(parents=True)
    operation.backup.write_bytes(b"only surviving original")
    operation.destination.write_bytes(b"restored")
    journal_path = _write_journal(
        locations,
        phase="committed",
        operations=operations,
    )
    operation.destination.unlink()

    with pytest.raises(BackupError, match="missing its live replacement"):
        recover_incomplete_restore(locations)

    assert operation.backup.read_bytes() == b"only surviving original"
    assert journal_path.exists()


def test_recovery_rejects_symlink_rollback_copy(tmp_path: Path) -> None:
    locations = _sqlite_locations(tmp_path)
    operations, operation = _operations(
        locations,
        main_state="rolling_back",
        main_original_existed=True,
    )
    external = tmp_path / "external-original"
    external.write_bytes(b"external")
    operation.backup.parent.mkdir(parents=True)
    operation.destination.write_bytes(b"replacement")
    operation.backup.write_bytes(b"original")
    journal_path = _write_journal(
        locations,
        phase="rolling_back",
        operations=operations,
    )
    operation.backup.unlink()
    operation.backup.symlink_to(external)

    with pytest.raises(BackupError, match="must not be a symlink"):
        recover_incomplete_restore(locations)

    assert external.read_bytes() == b"external"
    assert operation.backup.is_symlink()
    assert journal_path.exists()


def test_recovery_preserves_live_generation_replaced_after_journal(
    tmp_path: Path,
) -> None:
    locations = _sqlite_locations(tmp_path)
    operations, operation = _operations(
        locations,
        main_state="applying",
        main_original_existed=True,
    )
    operation.destination.parent.mkdir(parents=True)
    operation.destination.write_bytes(b"journaled replacement")
    operation.backup.write_bytes(b"original")
    journal_path = _write_journal(
        locations,
        phase="applying_local",
        operations=operations,
    )

    newer = operation.destination.with_name("newer-healthmes.db")
    newer.write_bytes(b"new user generation")
    os.replace(newer, operation.destination)

    with pytest.raises(
        BackupError,
        match="live target is not the journaled restore generation",
    ):
        recover_incomplete_restore(locations)

    assert operation.destination.read_bytes() == b"new user generation"
    assert operation.backup.read_bytes() == b"original"
    assert journal_path.exists()


def test_staging_recovery_preserves_artifact_without_recorded_identity(
    tmp_path: Path,
) -> None:
    locations = _sqlite_locations(tmp_path)
    operations, operation = _operations(
        locations,
        main_state="pending",
        main_original_existed=None,
    )
    assert operation.staged is not None
    operation.staged.parent.mkdir(parents=True)
    operation.staged.write_bytes(b"uncommitted staged generation")
    journal_path = restore_journal_path(locations.restore_state_dir or Path())
    write_restore_journal(
        journal_path,
        RestoreJournal(
            transaction_id=TRANSACTION_ID,
            phase="staging",
            recovery_mode="recoverable_local_swaps",
            operations=_anchor_operations(operations),
            postgres_targets=[],
        ),
    )

    with pytest.raises(
        BackupError,
        match="has no recorded restore generation identity",
    ):
        recover_incomplete_restore(locations)

    assert operation.staged.read_bytes() == b"uncommitted staged generation"
    assert journal_path.exists()


def test_staging_recovery_detects_directory_content_replacement(
    tmp_path: Path,
) -> None:
    base = _sqlite_locations(tmp_path)
    media_dir = tmp_path / "data" / "media"
    locations = DataLocations(
        database_url=base.database_url,
        media_dir=media_dir,
        restore_state_dir=base.restore_state_dir,
    )
    operations, database_operation = _operations(
        locations,
        main_state="pending",
        main_original_existed=None,
    )
    assert database_operation.staged is not None
    database_operation.staged.parent.mkdir(parents=True)
    database_operation.staged.write_bytes(b"staged database")
    media_staged = media_dir.with_name(
        f".{media_dir.name}.healthmes-restore-{TRANSACTION_ID}.staged"
    )
    media_staged.mkdir()
    staged_entry = media_staged / "capture.bin"
    staged_entry.write_bytes(b"journaled media")
    operations.append(
        JournalOperation(
            component="media",
            destination=media_dir,
            staged=media_staged,
            backup=media_dir.with_name(
                f".{media_dir.name}.healthmes-restore-"
                f"{TRANSACTION_ID}.backup"
            ),
            original_existed=None,
            state="pending",
        )
    )
    journal_path = _write_journal(
        locations,
        phase="prepared",
        operations=operations,
    )

    replacement = media_staged / "replacement.bin"
    replacement.write_bytes(b"new media generation")
    os.replace(replacement, staged_entry)

    with pytest.raises(
        BackupError,
        match="staged target changed after the restore journal was written",
    ):
        recover_incomplete_restore(locations)

    assert staged_entry.read_bytes() == b"new media generation"
    assert journal_path.exists()


def test_recovery_retries_after_crash_following_original_restore(
    tmp_path: Path,
    monkeypatch,
) -> None:
    locations = _sqlite_locations(tmp_path)
    operations, operation = _operations(
        locations,
        main_state="applying",
        main_original_existed=True,
    )
    operation.destination.parent.mkdir(parents=True)
    operation.destination.write_bytes(b"replacement")
    operation.backup.write_bytes(b"original")
    journal_path = _write_journal(
        locations,
        phase="applying_local",
        operations=operations,
    )
    real_write = snapshot_mod.write_restore_journal
    calls = 0

    def crash_after_original_restore(path, journal):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("injected crash after original restore")
        real_write(path, journal)

    monkeypatch.setattr(
        snapshot_mod,
        "write_restore_journal",
        crash_after_original_restore,
    )
    with pytest.raises(BackupError, match="injected crash after original restore"):
        recover_incomplete_restore(locations)

    assert operation.destination.read_bytes() == b"original"
    assert not operation.backup.exists()
    assert journal_path.exists()

    monkeypatch.setattr(snapshot_mod, "write_restore_journal", real_write)
    recover_incomplete_restore(locations)

    assert operation.destination.read_bytes() == b"original"
    assert not journal_path.exists()


def test_recovery_waits_for_sqlite_runtime_before_touching_staged_data(
    tmp_path: Path,
    monkeypatch,
) -> None:
    locations = _sqlite_locations(tmp_path)
    operations, operation = _operations(
        locations,
        main_state="pending",
        main_original_existed=None,
    )
    assert operation.staged is not None
    operation.staged.parent.mkdir(parents=True)
    operation.staged.write_bytes(b"staged")
    journal_path = _write_journal(
        locations,
        phase="prepared",
        operations=operations,
    )
    monkeypatch.setattr(
        snapshot_mod,
        "_SQLITE_RESTORE_RUNTIME_LOCK_TIMEOUT_SECONDS",
        0.05,
    )

    with sqlite_runtime_guard(locations.database_url):
        with pytest.raises(BackupError, match="running HealthMes process"):
            recover_incomplete_restore(locations)

    assert operation.staged.read_bytes() == b"staged"
    assert journal_path.exists()


def test_recovery_rejects_replaced_parent_identity_before_mutation(
    tmp_path: Path,
) -> None:
    locations = _sqlite_locations(tmp_path)
    operations, operation = _operations(
        locations,
        main_state="pending",
        main_original_existed=None,
    )
    assert operation.staged is not None
    operation.staged.parent.mkdir(parents=True)
    operation.staged.write_bytes(b"staged")
    operation.destination.write_bytes(b"live")
    journal_path = _write_journal(
        locations,
        phase="prepared",
        operations=operations,
    )

    original_parent = operation.destination.parent
    displaced_parent = tmp_path / "original-data-parent"
    original_parent.rename(displaced_parent)
    original_parent.mkdir()
    marker = original_parent / "attacker-marker"
    marker.write_bytes(b"untouched")

    with pytest.raises(BackupError, match="parent identity changed"):
        recover_incomplete_restore(locations)

    assert marker.read_bytes() == b"untouched"
    assert list(original_parent.iterdir()) == [marker]
    assert (
        displaced_parent / operation.destination.name
    ).read_bytes() == b"live"
    assert (
        displaced_parent / operation.staged.name
    ).read_bytes() == b"staged"
    assert journal_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symlinks")
def test_restart_finds_default_journal_after_database_parent_replacement(
    tmp_path: Path,
) -> None:
    data_parent = tmp_path / "node" / "data"
    locations = DataLocations(
        database_url=f"sqlite:///{data_parent / 'healthmes.db'}",
    )
    operations, operation = _operations(
        locations,
        main_state="pending",
        main_original_existed=None,
    )
    assert operation.staged is not None
    operation.staged.parent.mkdir(parents=True)
    operation.staged.write_bytes(b"staged")
    operation.destination.write_bytes(b"live")
    state_dir = snapshot_mod._restore_state_directory(locations)
    journal_path = restore_journal_path(state_dir)
    write_restore_journal(
        journal_path,
        RestoreJournal(
            transaction_id=TRANSACTION_ID,
            phase="prepared",
            recovery_mode="recoverable_local_swaps",
            operations=_record_operation_identities(operations),
            postgres_targets=[],
        ),
    )

    displaced_parent = tmp_path / "node" / "original-data"
    attacker = tmp_path / "node" / "attacker"
    attacker.mkdir()
    data_parent.rename(displaced_parent)
    data_parent.symlink_to(attacker, target_is_directory=True)

    assert snapshot_mod._restore_state_directory(locations) == state_dir
    with pytest.raises(
        BackupError,
        match=(
            "restore journal local operation set or order is invalid|"
            "targets do not match the current HealthMes configuration"
        ),
    ):
        recover_incomplete_restore(locations)

    assert journal_path.exists()
    assert list(attacker.iterdir()) == []


def test_snapshot_and_startup_rollback_cannot_capture_mixed_generations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "data" / "healthmes.db"
    media_path = tmp_path / "data" / "media"
    locations = DataLocations(
        database_url=f"sqlite:///{database_path}",
        media_dir=media_path,
        restore_state_dir=tmp_path / ".restore",
    )
    operations, database_operation = _operations(
        locations,
        main_state="applied",
        main_original_existed=True,
    )

    def write_marker(path: Path, marker: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE generation (marker TEXT NOT NULL)")
            connection.execute(
                "INSERT INTO generation (marker) VALUES (?)",
                (marker,),
            )
            connection.commit()

    write_marker(database_operation.destination, "replacement")
    write_marker(database_operation.backup, "original")
    media_path.mkdir(parents=True)
    (media_path / "generation.txt").write_text(
        "replacement",
        encoding="utf-8",
    )
    media_backup = media_path.with_name(
        f".{media_path.name}.healthmes-restore-{TRANSACTION_ID}.backup"
    )
    media_backup.mkdir()
    (media_backup / "generation.txt").write_text(
        "original",
        encoding="utf-8",
    )
    media_operation = JournalOperation(
        component="media",
        destination=media_path,
        staged=media_path.with_name(
            f".{media_path.name}.healthmes-restore-{TRANSACTION_ID}.staged"
        ),
        backup=media_backup,
        original_existed=True,
        state="applied",
    )
    operations.append(media_operation)
    _write_journal(
        locations,
        phase="local_applied",
        operations=operations,
    )

    snapshot_path = tmp_path / "generation.age"
    database_captured = Event()
    release_snapshot = Event()
    recovery_started = Event()
    recovery_finished = Event()
    errors: list[BaseException] = []
    original_stage = snapshot_mod._stage_healthmes_db

    def paused_database_stage(database_url, stage, **kwargs):
        result = original_stage(database_url, stage, **kwargs)
        database_captured.set()
        if not release_snapshot.wait(10):
            raise TimeoutError("test did not release snapshot staging")
        return result

    monkeypatch.setattr(
        snapshot_mod,
        "_stage_healthmes_db",
        paused_database_stage,
    )

    def take_snapshot() -> None:
        try:
            create_snapshot(
                locations,
                passphrase="journal-fence-passphrase",
                out_path=snapshot_path,
                created_at=datetime(2026, 8, 18, 1, tzinfo=UTC),
            )
        except BaseException as exc:
            errors.append(exc)

    def recover_startup_journal() -> None:
        recovery_started.set()
        try:
            recover_incomplete_restore(locations)
        except BaseException as exc:
            errors.append(exc)
        finally:
            recovery_finished.set()

    snapshot_thread = Thread(target=take_snapshot)
    recovery_thread = Thread(target=recover_startup_journal)
    snapshot_thread.start()
    assert database_captured.wait(10)
    recovery_thread.start()
    assert recovery_started.wait(10)
    assert not recovery_finished.wait(0.2)

    release_snapshot.set()
    snapshot_thread.join(10)
    recovery_thread.join(10)
    assert not snapshot_thread.is_alive()
    assert not recovery_thread.is_alive()
    assert errors == []

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT marker FROM generation"
        ).fetchone() == ("original",)
    assert (media_path / "generation.txt").read_text(encoding="utf-8") == "original"

    monkeypatch.setattr(
        snapshot_mod,
        "_stage_healthmes_db",
        original_stage,
    )
    restored_root = tmp_path / "restored"
    restored_database = restored_root / "healthmes.db"
    restored_media = restored_root / "media"
    restore_snapshot(
        snapshot_path,
        passphrase="journal-fence-passphrase",
        locations=DataLocations(
            database_url=f"sqlite:///{restored_database}",
            media_dir=restored_media,
            restore_state_dir=restored_root / ".restore",
        ),
    )

    with sqlite3.connect(restored_database) as connection:
        assert connection.execute(
            "SELECT marker FROM generation"
        ).fetchone() == ("replacement",)
    assert (restored_media / "generation.txt").read_text(
        encoding="utf-8"
    ) == "replacement"


def test_dangling_journal_symlink_is_not_treated_as_no_journal(
    tmp_path: Path,
) -> None:
    locations = _sqlite_locations(tmp_path)
    journal_path = restore_journal_path(locations.restore_state_dir or Path())
    journal_path.parent.mkdir(parents=True)
    journal_path.symlink_to(tmp_path / "missing-journal-target")

    with pytest.raises(BackupError, match="journal is not a regular file"):
        recover_incomplete_restore(locations)

    assert journal_path.is_symlink()


def test_oversized_journal_is_rejected_before_json_decode(tmp_path: Path) -> None:
    locations = _sqlite_locations(tmp_path)
    journal_path = restore_journal_path(locations.restore_state_dir or Path())
    journal_path.parent.mkdir(parents=True)
    journal_path.write_bytes(b"{" + b"x" * (1024 * 1024))

    with pytest.raises(BackupError, match="exceeds the 1 MiB safety limit"):
        recover_incomplete_restore(locations)

    assert journal_path.exists()


def test_missing_rollback_copy_never_deletes_live_replacement(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "healthmes.db"
    destination.write_bytes(b"replacement")
    staged = tmp_path / ".healthmes.db.restore.staged"
    backup = tmp_path / ".healthmes.db.restore.backup"
    operation = snapshot_mod._SwapOperation(
        component="healthmes_db",
        destination=destination,
        staged=staged,
        backup=backup,
        had_original=True,
        applied=True,
    )
    journal_operation = JournalOperation(
        component="healthmes_db",
        destination=destination,
        staged=staged,
        backup=backup,
        original_existed=True,
        state="applied",
    )
    _anchor_operations([journal_operation])
    journal_path = tmp_path / ".restore" / "pending.json"
    plan = snapshot_mod._RestorePlan(
        transaction_id=TRANSACTION_ID,
        included=("healthmes_db",),
        skipped=(),
        recovery_mode="recoverable_local_swaps",
        local_operations=[operation],
        postgres_restores=[],
        journal_path=journal_path,
        journal=RestoreJournal(
            transaction_id=TRANSACTION_ID,
            phase="rolling_back",
            recovery_mode="recoverable_local_swaps",
            operations=[journal_operation],
            postgres_targets=[],
        ),
    )

    errors = snapshot_mod._rollback_local_operations(plan)

    assert errors
    assert "expected rollback copy is missing" in errors[0]
    assert destination.read_bytes() == b"replacement"
    assert journal_operation.state == "rolling_back"
    assert journal_path.exists()


def test_cleanup_ignores_absent_artifacts_with_absent_parent(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "never-created"
    operation = snapshot_mod._SwapOperation(
        component="media",
        destination=tmp_path / "media",
        staged=parent / "staged",
        backup=parent / "backup",
        is_tree=True,
    )

    assert snapshot_mod._cleanup_restore_artifacts([operation]) == []
    assert not parent.exists()


def test_cleanup_preserves_artifact_without_recorded_identity(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "data"
    parent.mkdir()
    staged = parent / ".media.healthmes-restore.staged"
    staged.mkdir()
    (staged / "capture.bin").write_bytes(b"possibly replaced")
    operation = snapshot_mod._SwapOperation(
        component="media",
        destination=parent / "media",
        staged=staged,
        backup=parent / ".media.healthmes-restore.backup",
        is_tree=True,
    )

    errors = snapshot_mod._cleanup_restore_artifacts(
        [operation],
        preserve_backups=True,
    )

    assert len(errors) == 1
    assert "has no recorded restore generation identity" in errors[0]
    assert (staged / "capture.bin").read_bytes() == b"possibly replaced"


def test_committed_cleanup_deadline_retries_quarantine_on_next_startup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    locations = _sqlite_locations(tmp_path)
    operations, operation = _operations(
        locations,
        main_state="applied",
        main_original_existed=True,
    )
    operation.destination.parent.mkdir(parents=True)
    operation.destination.write_bytes(b"restored")
    operation.backup.write_bytes(b"old")
    journal_path = _write_journal(
        locations,
        phase="committed",
        operations=operations,
    )
    cleanup = snapshot_mod._cleanup_quarantine_path(operation.backup)
    real_reserve = (
        snapshot_mod._IdentityTraversal.reserve_cleanup_entry
    )
    expired = False

    def expire_cleanup_deadline(self, *, entry, depth):
        nonlocal expired
        if not expired:
            expired = True
            self.phase.deadline = snapshot_mod.monotonic() - 1
        return real_reserve(self, entry=entry, depth=depth)

    monkeypatch.setattr(
        snapshot_mod._IdentityTraversal,
        "reserve_cleanup_entry",
        expire_cleanup_deadline,
    )

    with pytest.raises(
        BackupError,
        match="committed restore cleanup is incomplete",
    ):
        recover_incomplete_restore(locations)

    assert expired is True
    assert operation.destination.read_bytes() == b"restored"
    assert not operation.backup.exists()
    assert cleanup.read_bytes() == b"old"
    assert journal_path.is_file()

    monkeypatch.setattr(
        snapshot_mod._IdentityTraversal,
        "reserve_cleanup_entry",
        real_reserve,
    )
    recover_incomplete_restore(locations)

    assert operation.destination.read_bytes() == b"restored"
    assert not cleanup.exists()
    assert not journal_path.exists()


def test_rollback_cleanup_deadline_preserves_new_live_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    locations = _sqlite_locations(tmp_path)
    operations, operation = _operations(
        locations,
        main_state="applying",
        main_original_existed=True,
    )
    operation.destination.parent.mkdir(parents=True)
    operation.destination.write_bytes(b"replacement")
    operation.backup.write_bytes(b"original")
    journal_path = _write_journal(
        locations,
        phase="applying_local",
        operations=operations,
    )
    cleanup = snapshot_mod._cleanup_quarantine_path(
        operation.destination
    )
    real_reserve = (
        snapshot_mod._IdentityTraversal.reserve_cleanup_entry
    )
    expired = False

    def expire_cleanup_deadline(self, *, entry, depth):
        nonlocal expired
        if not expired:
            expired = True
            self.phase.deadline = snapshot_mod.monotonic() - 1
        return real_reserve(self, entry=entry, depth=depth)

    monkeypatch.setattr(
        snapshot_mod._IdentityTraversal,
        "reserve_cleanup_entry",
        expire_cleanup_deadline,
    )

    with pytest.raises(
        BackupError,
        match="identity traversal deadline",
    ):
        recover_incomplete_restore(locations)

    assert expired is True
    assert not operation.destination.exists()
    assert operation.backup.read_bytes() == b"original"
    assert cleanup.read_bytes() == b"replacement"
    assert journal_path.is_file()

    operation.destination.write_bytes(b"new user generation")
    monkeypatch.setattr(
        snapshot_mod._IdentityTraversal,
        "reserve_cleanup_entry",
        real_reserve,
    )

    with pytest.raises(
        BackupError,
        match="live target is not the journaled restore generation",
    ):
        recover_incomplete_restore(locations)

    assert operation.destination.read_bytes() == b"new user generation"
    assert operation.backup.read_bytes() == b"original"
    assert cleanup.read_bytes() == b"replacement"
    assert journal_path.is_file()


def test_preflight_rejects_existing_cleanup_quarantine(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "data" / "healthmes.db"
    destination.parent.mkdir()
    source = tmp_path / "source.db"
    source.write_bytes(b"source")
    request = snapshot_mod._LocalRestoreRequest(
        component="healthmes_db",
        source=source,
        destination=destination,
        is_tree=False,
    )
    transaction_id = "cleanup-conflict"
    staged = destination.with_name(
        f".{destination.name}.healthmes-restore-"
        f"{transaction_id}.staged"
    )
    cleanup = snapshot_mod._cleanup_quarantine_path(staged)
    cleanup.write_bytes(b"unfinished cleanup")

    with pytest.raises(
        BackupError,
        match="staging cleanup quarantine already exists",
    ):
        snapshot_mod._preflight_local_target(
            request,
            transaction_id=transaction_id,
        )

    assert cleanup.read_bytes() == b"unfinished cleanup"
