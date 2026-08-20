"""LocalDirectoryProvider + weekly job factory tests."""

import os
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Lock
from time import sleep

import pytest

from healthmes.backup import filesystem as filesystem_mod
from healthmes.backup import local as local_mod
from healthmes.backup.filesystem import RegularFileIdentity
from healthmes.backup.local import LocalDirectoryProvider, build_backup_job
from healthmes.backup.provider import BackupError, BackupProvider, SnapshotInfo
from healthmes.backup.snapshot import (
    DataLocations,
    resolve_backup_dir,
    resolve_data_locations,
    resolve_passphrase,
)
from healthmes.config import Settings

T1 = datetime(2026, 7, 5, 3, 30, 0, tzinfo=UTC)
T2 = datetime(2026, 7, 12, 3, 30, 0, tzinfo=UTC)


def make_provider(source_env, backup_dir, clock=None, passphrase=...):
    return LocalDirectoryProvider(
        backup_dir,
        locations=source_env.locations,
        passphrase=source_env.passphrase if passphrase is ... else passphrase,
        clock=clock,
    )


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "database_url": f"sqlite:///{tmp_path / 'data' / 'healthmes.db'}",
        "data_dir": tmp_path / "data",
        "scheduler_enabled": False,
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def leave_crash_left_quarantine(
    provider,
    path,
    monkeypatch,
):
    with path.open("rb") as handle:
        expected = RegularFileIdentity.from_descriptor(handle.fileno())
    real_unlink = local_mod._unlink_snapshot_quarantine_entry
    failed = False

    def fail_once(quarantine):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected quarantine unlink interruption")
        return real_unlink(quarantine)

    monkeypatch.setattr(
        local_mod,
        "_unlink_snapshot_quarantine_entry",
        fail_once,
    )
    with pytest.raises(
        BackupError,
        match="quarantine unlink interruption",
    ):
        provider.remove_snapshot_if_unchanged(
            path,
            expected=expected,
        )
    monkeypatch.setattr(
        local_mod,
        "_unlink_snapshot_quarantine_entry",
        real_unlink,
    )
    prefix = local_mod._snapshot_quarantine_prefix(path.name)
    quarantines = list(path.parent.glob(f"{prefix}*"))
    assert not path.exists()
    assert len(quarantines) == 1
    return quarantines[0]


class TestProvider:
    def test_satisfies_backup_provider_protocol(self, source_env, tmp_path):
        provider = make_provider(source_env, tmp_path / "backups")
        assert isinstance(provider, BackupProvider)

    def test_export_snapshot_names_and_info(self, source_env, tmp_path):
        provider = make_provider(source_env, tmp_path / "backups", clock=lambda: T1)
        info = provider.export_snapshot()
        assert isinstance(info, SnapshotInfo)
        assert info.name == "healthmes-backup-20260705T033000Z.tar.gz.age"
        assert info.path == tmp_path / "backups" / info.name
        assert info.created_at == T1
        assert info.path.is_file()
        assert info.size_bytes == info.path.stat().st_size > 0

    def test_same_second_exports_get_distinct_names(self, source_env, tmp_path):
        provider = make_provider(source_env, tmp_path / "backups", clock=lambda: T1)
        first = provider.export_snapshot()
        second = provider.export_snapshot()
        assert first.path != second.path
        assert second.name == "healthmes-backup-20260705T033000Z-2.tar.gz.age"

    @pytest.mark.skipif(
        os.name == "nt",
        reason="dangling symlink occupancy contract",
    )
    def test_export_treats_dangling_symlink_as_an_occupied_name(
        self,
        source_env,
        tmp_path,
    ):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        occupied = backup_dir / "healthmes-backup-20260705T033000Z.tar.gz.age"
        occupied.symlink_to(tmp_path / "missing-snapshot")
        provider = make_provider(source_env, backup_dir, clock=lambda: T1)

        info = provider.export_snapshot()

        assert occupied.is_symlink()
        assert info.name == "healthmes-backup-20260705T033000Z-2.tar.gz.age"
        assert info.path.is_file()

    def test_export_retries_when_file_appears_after_name_selection(
        self,
        source_env,
        tmp_path,
        monkeypatch,
    ):
        backup_dir = tmp_path / "backups"
        provider = make_provider(source_env, backup_dir, clock=lambda: T1)
        original = b"raced regular file"
        real_unique = provider._unique_out_path
        real_create = local_mod.create_snapshot
        selections = 0
        creations = 0

        def race_first_selection(created_at):
            nonlocal selections
            candidate = real_unique(created_at)
            selections += 1
            if selections == 1:
                candidate.write_bytes(original)
            return candidate

        def count_creation(*args, **kwargs):
            nonlocal creations
            creations += 1
            return real_create(*args, **kwargs)

        monkeypatch.setattr(provider, "_unique_out_path", race_first_selection)
        monkeypatch.setattr(local_mod, "create_snapshot", count_creation)

        info = provider.export_snapshot()

        occupied = backup_dir / "healthmes-backup-20260705T033000Z.tar.gz.age"
        assert occupied.read_bytes() == original
        assert info.name == "healthmes-backup-20260705T033000Z-2.tar.gz.age"
        assert info.path.is_file()
        assert selections == 2
        assert creations == 1

    @pytest.mark.skipif(
        os.name == "nt",
        reason="dangling symlink race contract",
    )
    def test_export_retries_when_dangling_symlink_appears_after_selection(
        self,
        source_env,
        tmp_path,
        monkeypatch,
    ):
        backup_dir = tmp_path / "backups"
        provider = make_provider(source_env, backup_dir, clock=lambda: T1)
        missing_target = tmp_path / "missing-raced-snapshot"
        real_unique = provider._unique_out_path
        selections = 0

        def race_first_selection(created_at):
            nonlocal selections
            candidate = real_unique(created_at)
            selections += 1
            if selections == 1:
                candidate.symlink_to(missing_target)
            return candidate

        monkeypatch.setattr(provider, "_unique_out_path", race_first_selection)

        info = provider.export_snapshot()

        occupied = backup_dir / "healthmes-backup-20260705T033000Z.tar.gz.age"
        assert occupied.is_symlink()
        assert occupied.readlink() == missing_target
        assert info.name == "healthmes-backup-20260705T033000Z-2.tar.gz.age"
        assert info.path.is_file()
        assert selections == 2

    def test_concurrent_same_second_exports_are_serialized(
        self,
        source_env,
        tmp_path,
        monkeypatch,
    ):
        provider = make_provider(source_env, tmp_path / "backups", clock=lambda: T1)
        real_create_snapshot = local_mod.create_snapshot
        state_lock = Lock()
        active = 0
        max_active = 0

        def delayed_create_snapshot(*args, **kwargs):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            try:
                sleep(0.05)
                return real_create_snapshot(*args, **kwargs)
            finally:
                with state_lock:
                    active -= 1

        monkeypatch.setattr(
            local_mod,
            "create_snapshot",
            delayed_create_snapshot,
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(provider.export_snapshot)
            second_future = executor.submit(provider.export_snapshot)
            infos = [first_future.result(), second_future.result()]

        assert max_active == 1
        assert sorted(info.name for info in infos) == [
            "healthmes-backup-20260705T033000Z-2.tar.gz.age",
            "healthmes-backup-20260705T033000Z.tar.gz.age",
        ]
        assert all(info.path.is_file() and info.size_bytes > 0 for info in infos)

    def test_export_supports_symlinked_backup_directory(
        self,
        source_env,
        tmp_path,
        monkeypatch,
    ):
        target = tmp_path / "physical-backups"
        target.mkdir()
        configured = tmp_path / "configured-backups"
        configured.symlink_to(target, target_is_directory=True)
        provider = make_provider(source_env, configured, clock=lambda: T1)
        checked_paths = []
        real_require_capacity = local_mod._require_disk_capacity

        def record_capacity_check(path, **kwargs):
            checked_paths.append((path, kwargs))
            return real_require_capacity(path, **kwargs)

        monkeypatch.setattr(
            local_mod,
            "_require_disk_capacity",
            record_capacity_check,
        )

        info = provider.export_snapshot()

        assert info.path.parent == target.resolve()
        assert (configured / info.name).samefile(info.path)
        assert checked_paths == [
            (
                target.resolve(),
                {
                    "payload_bytes": info.size_bytes,
                    "limits": source_env.locations.resource_limits,
                    "label": "final local backup publication",
                },
            )
        ]

    def test_export_rejects_insufficient_final_destination_reserve(
        self,
        source_env,
        tmp_path,
        monkeypatch,
    ):
        backup_dir = tmp_path / "backups"
        provider = make_provider(source_env, backup_dir, clock=lambda: T1)
        staged_size = None
        real_create_snapshot = local_mod.create_snapshot

        def capture_staged_snapshot(*args, **kwargs):
            nonlocal staged_size
            result = real_create_snapshot(*args, **kwargs)
            staged_size = kwargs["out_path"].stat().st_size
            return result

        def reject_final_publication(path, **kwargs):
            assert path == backup_dir.resolve()
            assert staged_size is not None
            assert kwargs == {
                "payload_bytes": staged_size,
                "limits": source_env.locations.resource_limits,
                "label": "final local backup publication",
            }
            raise BackupError(
                "insufficient disk space for final local backup publication"
            )

        monkeypatch.setattr(
            local_mod,
            "create_snapshot",
            capture_staged_snapshot,
        )
        monkeypatch.setattr(
            local_mod,
            "_require_disk_capacity",
            reject_final_publication,
        )

        with pytest.raises(
            BackupError,
            match="insufficient disk space for final local backup publication",
        ):
            provider.export_snapshot()

        assert staged_size is not None
        assert list(backup_dir.glob("*.tar.gz.age")) == []

    def test_collision_relocation_preserves_source_when_destination_reserve_fails(
        self,
        source_env,
        tmp_path,
        monkeypatch,
    ):
        provider = make_provider(source_env, tmp_path / "backups", clock=lambda: T1)
        info = provider.export_snapshot()
        original = info.path.read_bytes()

        def reject_final_publication(path, **kwargs):
            assert path == info.path.parent
            assert kwargs == {
                "payload_bytes": info.size_bytes,
                "limits": source_env.locations.resource_limits,
                "label": "final local backup publication",
            }
            raise BackupError(
                "insufficient disk space for final local backup publication"
            )

        monkeypatch.setattr(
            local_mod,
            "_require_disk_capacity",
            reject_final_publication,
        )

        with pytest.raises(
            BackupError,
            match="insufficient disk space for final local backup publication",
        ):
            provider.relocate_snapshot_after_collision(
                info,
                minimum_counter=2,
            )

        candidate = info.path.with_name(
            "healthmes-backup-20260705T033000Z-2.tar.gz.age"
        )
        assert info.path.read_bytes() == original
        assert not candidate.exists()

    def test_collision_relocation_keeps_candidate_when_source_retirement_fails(
        self,
        source_env,
        tmp_path,
        monkeypatch,
    ):
        provider = make_provider(source_env, tmp_path / "backups", clock=lambda: T1)
        info = provider.export_snapshot()
        real_fsync = local_mod._fsync_snapshot_directory
        calls = 0

        def fail_second_fsync(path, descriptor):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected directory fsync failure")
            return real_fsync(path, descriptor)

        monkeypatch.setattr(
            local_mod,
            "_fsync_snapshot_directory",
            fail_second_fsync,
        )

        with pytest.raises(
            BackupError,
            match="could not remove replicated local snapshot",
        ):
            provider.relocate_snapshot_after_collision(
                info,
                minimum_counter=2,
            )

        candidate = info.path.with_name(
            "healthmes-backup-20260705T033000Z-2.tar.gz.age"
        )
        assert not info.path.exists()
        assert candidate.is_file()
        assert candidate.stat().st_size == info.size_bytes

    def test_collision_relocation_preserves_a_snapshot_if_candidate_fsync_fails(
        self,
        source_env,
        tmp_path,
        monkeypatch,
    ):
        provider = make_provider(source_env, tmp_path / "backups", clock=lambda: T1)
        info = provider.export_snapshot()
        original = info.path.read_bytes()
        real_fsync = filesystem_mod.os.fsync
        failed = False

        def fail_first_directory_fsync(descriptor):
            nonlocal failed
            if not failed and stat.S_ISDIR(os.fstat(descriptor).st_mode):
                failed = True
                raise OSError("injected candidate directory fsync failure")
            return real_fsync(descriptor)

        monkeypatch.setattr(
            filesystem_mod.os,
            "fsync",
            fail_first_directory_fsync,
        )

        with pytest.raises(
            BackupError,
            match="destination was created but durability could not be confirmed",
        ):
            provider.relocate_snapshot_after_collision(
                info,
                minimum_counter=2,
            )

        candidate = info.path.with_name(
            "healthmes-backup-20260705T033000Z-2.tar.gz.age"
        )
        survivors = [path for path in (info.path, candidate) if path.exists()]
        assert failed is True
        assert survivors
        assert all(path.read_bytes() == original for path in survivors)

    @pytest.mark.skipif(
        os.name == "nt",
        reason="POSIX directory descriptor contract",
    )
    def test_collision_relocation_rejects_replaced_backup_directory(
        self,
        source_env,
        tmp_path,
        monkeypatch,
    ):
        backup_dir = tmp_path / "backups"
        provider = make_provider(source_env, backup_dir, clock=lambda: T1)
        info = provider.export_snapshot()
        original = info.path.read_bytes()
        displaced = tmp_path / "displaced-backups"
        real_lock = local_mod.exclusive_file_lock

        @contextmanager
        def replace_before_lock(path, **kwargs):
            backup_dir.rename(displaced)
            backup_dir.mkdir()
            (backup_dir / info.name).write_bytes(b"replacement generation")
            with real_lock(path, **kwargs):
                yield

        monkeypatch.setattr(
            local_mod,
            "exclusive_file_lock",
            replace_before_lock,
        )

        with pytest.raises(
            BackupError,
            match="backup directory changed",
        ):
            provider.relocate_snapshot_after_collision(
                info,
                minimum_counter=2,
            )

        assert (backup_dir / info.name).read_bytes() == b"replacement generation"
        assert (displaced / info.name).read_bytes() == original

    def test_remove_preserves_replacement_raced_after_identity_check(
        self,
        source_env,
        tmp_path,
        monkeypatch,
    ):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        provider = make_provider(
            source_env,
            backup_dir,
        )
        path = backup_dir / "healthmes-backup-20260705T033000Z.tar.gz.age"
        path.write_bytes(b"uploaded encrypted snapshot generation")
        with path.open("rb") as handle:
            expected = RegularFileIdentity.from_descriptor(handle.fileno())
        replacement = b"replacement inserted after identity verification"
        real_quarantine = local_mod._quarantine_snapshot_entry
        raced = False

        def replace_then_quarantine(source_name, quarantine):
            nonlocal raced
            if not raced:
                raced = True
                source = quarantine.path.parent / source_name
                source.unlink()
                source.write_bytes(replacement)
            return real_quarantine(source_name, quarantine)

        monkeypatch.setattr(
            local_mod,
            "_quarantine_snapshot_entry",
            replace_then_quarantine,
        )

        with pytest.raises(
            BackupError,
            match="changed after upload",
        ):
            provider.remove_snapshot_if_unchanged(
                path,
                expected=expected,
            )

        assert raced is True
        assert path.read_bytes() == replacement
        prefix = local_mod._snapshot_quarantine_prefix(path.name)
        assert list(path.parent.glob(f"{prefix}*")) == []

    def test_remove_recovers_crash_left_quarantine(
        self,
        source_env,
        tmp_path,
        monkeypatch,
    ):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        provider = make_provider(
            source_env,
            backup_dir,
        )
        path = backup_dir / "healthmes-backup-20260705T033000Z.tar.gz.age"
        path.write_bytes(b"uploaded encrypted snapshot generation")
        with path.open("rb") as handle:
            expected = RegularFileIdentity.from_descriptor(handle.fileno())
        real_unlink = local_mod._unlink_snapshot_quarantine_entry
        failed = False

        def fail_once(quarantine):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("injected quarantine unlink interruption")
            return real_unlink(quarantine)

        monkeypatch.setattr(
            local_mod,
            "_unlink_snapshot_quarantine_entry",
            fail_once,
        )
        with pytest.raises(
            BackupError,
            match="quarantine unlink interruption",
        ):
            provider.remove_snapshot_if_unchanged(
                path,
                expected=expected,
            )

        prefix = local_mod._snapshot_quarantine_prefix(path.name)
        quarantines = list(path.parent.glob(f"{prefix}*"))
        assert not path.exists()
        assert len(quarantines) == 1
        assert (
            quarantines[0] / local_mod._SNAPSHOT_QUARANTINE_ENTRY
        ).is_file()

        monkeypatch.setattr(
            local_mod,
            "_unlink_snapshot_quarantine_entry",
            real_unlink,
        )
        provider.remove_snapshot_if_unchanged(
            path,
            expected=expected,
        )

        assert not path.exists()
        assert list(path.parent.glob(f"{prefix}*")) == []

    def test_startup_recovers_crash_left_quarantine_without_source_retry(
        self,
        source_env,
        tmp_path,
        monkeypatch,
    ):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        provider = make_provider(source_env, backup_dir)
        path = backup_dir / "healthmes-backup-20260705T033000Z.tar.gz.age"
        path.write_bytes(b"uploaded encrypted snapshot generation")
        quarantine = leave_crash_left_quarantine(
            provider,
            path,
            monkeypatch,
        )
        replacement = b"newer same-name local generation"
        path.write_bytes(replacement)

        make_provider(source_env, backup_dir)

        assert path.read_bytes() == replacement
        assert not quarantine.exists()

    def test_list_recovers_crash_left_quarantine_without_source_retry(
        self,
        source_env,
        tmp_path,
        monkeypatch,
    ):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        provider = make_provider(source_env, backup_dir)
        path = backup_dir / "healthmes-backup-20260705T033000Z.tar.gz.age"
        path.write_bytes(b"uploaded encrypted snapshot generation")
        quarantine = leave_crash_left_quarantine(
            provider,
            path,
            monkeypatch,
        )

        assert provider.list_snapshots() == []

        assert not quarantine.exists()

    def test_export_recovers_crash_left_quarantine_without_source_retry(
        self,
        source_env,
        tmp_path,
        monkeypatch,
    ):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        provider = make_provider(source_env, backup_dir, clock=lambda: T1)
        path = backup_dir / "healthmes-backup-20260705T033000Z.tar.gz.age"
        path.write_bytes(b"uploaded encrypted snapshot generation")
        quarantine = leave_crash_left_quarantine(
            provider,
            path,
            monkeypatch,
        )

        info = provider.export_snapshot()

        assert info.path == path
        assert info.path.is_file()
        assert not quarantine.exists()

    def test_global_quarantine_recovery_scan_is_bounded(
        self,
        tmp_path,
    ):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        for index in range(3):
            (backup_dir / f"{local_mod._SNAPSHOT_QUARANTINE_PREFIX}{index}").mkdir()

        with local_mod._open_snapshot_parent(backup_dir) as parent_descriptor:
            report = local_mod._recover_snapshot_quarantines(
                backup_dir,
                parent_descriptor,
                max_entries=1,
                max_seconds=60.0,
            )

        assert report.scanned == 1
        assert report.truncated is True

    def test_quarantine_recovery_is_not_starved_by_ordinary_files(
        self,
        source_env,
        tmp_path,
        monkeypatch,
    ):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        provider = make_provider(source_env, backup_dir)
        for index in range(
            local_mod._SNAPSHOT_RECOVERY_MAX_ENTRIES + 64
        ):
            (backup_dir / f"ordinary-{index:04d}.bin").write_bytes(b"x")
        path = backup_dir / "healthmes-backup-20260705T033000Z.tar.gz.age"
        path.write_bytes(b"uploaded encrypted snapshot generation")
        quarantine = leave_crash_left_quarantine(
            provider,
            path,
            monkeypatch,
        )

        with local_mod._open_snapshot_parent(backup_dir) as parent_descriptor:
            report = local_mod._recover_snapshot_quarantines(
                backup_dir,
                parent_descriptor,
                max_entries=local_mod._SNAPSHOT_RECOVERY_MAX_ENTRIES,
                max_seconds=60.0,
            )

        assert report.scanned == 1
        assert report.cleaned == 1
        assert report.unresolved == 0
        assert report.truncated is False
        assert not quarantine.exists()

    def test_unresolved_quarantines_do_not_starve_later_recovery(
        self,
        source_env,
        tmp_path,
        monkeypatch,
    ):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        provider = make_provider(source_env, backup_dir)
        invalid_names = tuple(
            f"{local_mod._SNAPSHOT_QUARANTINE_PREFIX}invalid-{index:03d}"
            for index in range(local_mod._SNAPSHOT_RECOVERY_MAX_ENTRIES)
        )
        for name in invalid_names:
            (backup_dir / name).mkdir()
        path = backup_dir / "healthmes-backup-20260705T033000Z.tar.gz.age"
        path.write_bytes(b"uploaded encrypted snapshot generation")
        quarantine = leave_crash_left_quarantine(
            provider,
            path,
            monkeypatch,
        )

        batches = {
            0: (invalid_names, 1, False),
            1: ((quarantine.name,), 2, False),
            2: ((), 2, True),
        }
        monkeypatch.setattr(
            local_mod,
            "read_directory_batch",
            lambda _descriptor, offset: batches[offset],
        )

        with local_mod._open_snapshot_parent(backup_dir) as parent_descriptor:
            first = local_mod._recover_snapshot_quarantines(
                backup_dir,
                parent_descriptor,
                max_entries=local_mod._SNAPSHOT_RECOVERY_MAX_ENTRIES,
                max_seconds=60.0,
            )
            second = local_mod._recover_snapshot_quarantines(
                backup_dir,
                parent_descriptor,
                max_entries=local_mod._SNAPSHOT_RECOVERY_MAX_ENTRIES,
                max_seconds=60.0,
            )

        assert first.scanned == local_mod._SNAPSHOT_RECOVERY_MAX_ENTRIES
        assert first.cleaned == 0
        assert (
            first.unresolved
            == local_mod._SNAPSHOT_RECOVERY_MAX_ENTRIES
        )
        assert first.truncated is True
        assert second.cleaned == 1
        assert second.unresolved == 0
        assert second.truncated is False
        assert not quarantine.exists()

    def test_source_cursor_advances_past_incomplete_quarantine(
        self,
        source_env,
        tmp_path,
        monkeypatch,
    ):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        provider = make_provider(source_env, backup_dir)
        path = backup_dir / "healthmes-backup-20260705T033000Z.tar.gz.age"
        path.write_bytes(b"uploaded encrypted snapshot generation")
        with path.open("rb") as handle:
            expected = RegularFileIdentity.from_descriptor(handle.fileno())
        quarantine = leave_crash_left_quarantine(
            provider,
            path,
            monkeypatch,
        )
        incomplete = (
            backup_dir
            / f"{local_mod._snapshot_quarantine_prefix(path.name)}incomplete"
        )
        incomplete.mkdir()
        batches = {
            0: ((incomplete.name, quarantine.name), 1, True),
        }
        monkeypatch.setattr(
            local_mod,
            "read_directory_batch",
            lambda _descriptor, offset: batches[offset],
        )

        with local_mod._open_snapshot_parent(backup_dir) as parent_descriptor:
            first = local_mod._recover_snapshot_quarantines(
                backup_dir,
                parent_descriptor,
                path.name,
                expected,
                max_entries=1,
                max_seconds=60.0,
            )
            second = local_mod._recover_snapshot_quarantines(
                backup_dir,
                parent_descriptor,
                path.name,
                expected,
                max_entries=1,
                max_seconds=60.0,
            )

        assert first.scanned == 1
        assert first.unresolved == 1
        assert first.truncated is True
        assert incomplete.exists()
        assert second.cleaned == 1
        assert second.recovered_target is True
        assert not quarantine.exists()
        cursor = (
            backup_dir
            / local_mod._SNAPSHOT_RECOVERY_CONTROL_DIRECTORY
            / local_mod._snapshot_source_recovery_cursor_name(path.name)
        )
        assert not cursor.exists()

    def test_source_cursor_resumes_after_directory_batch_budget(
        self,
        source_env,
        tmp_path,
        monkeypatch,
    ):
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        provider = make_provider(source_env, backup_dir)
        path = backup_dir / "healthmes-backup-20260705T033000Z.tar.gz.age"
        path.write_bytes(b"uploaded encrypted snapshot generation")
        with path.open("rb") as handle:
            expected = RegularFileIdentity.from_descriptor(handle.fileno())
        quarantine = leave_crash_left_quarantine(
            provider,
            path,
            monkeypatch,
        )
        offsets: list[int] = []
        batches = {
            0: (("ordinary-file.bin",), 7, False),
            7: ((quarantine.name,), 9, True),
        }

        def controlled_batch(_descriptor, offset):
            offsets.append(offset)
            return batches[offset]

        monkeypatch.setattr(
            local_mod,
            "_SNAPSHOT_RECOVERY_MAX_DIRECTORY_BATCHES",
            1,
        )
        monkeypatch.setattr(
            local_mod,
            "read_directory_batch",
            controlled_batch,
        )

        with local_mod._open_snapshot_parent(backup_dir) as parent_descriptor:
            first = local_mod._recover_snapshot_quarantines(
                backup_dir,
                parent_descriptor,
                path.name,
                expected,
                max_entries=1,
                max_seconds=60.0,
            )
            second = local_mod._recover_snapshot_quarantines(
                backup_dir,
                parent_descriptor,
                path.name,
                expected,
                max_entries=1,
                max_seconds=60.0,
            )

        assert first.scanned == 0
        assert first.truncated is True
        assert second.cleaned == 1
        assert second.recovered_target is True
        assert offsets == [0, 7]
        assert not quarantine.exists()

    def test_list_snapshots_newest_first_ignoring_strays(self, source_env, tmp_path):
        backup_dir = tmp_path / "backups"
        clock_values = [T1, T2]
        provider = make_provider(
            source_env, backup_dir, clock=lambda: clock_values.pop(0)
        )
        old = provider.export_snapshot()
        new = provider.export_snapshot()
        (backup_dir / "notes.txt").write_text("not a snapshot")
        (backup_dir / "other-backup-20260101T000000Z.tar.gz.age").write_bytes(b"stray")

        listed = provider.list_snapshots()
        assert [info.name for info in listed] == [new.name, old.name]
        assert listed[0].created_at == T2
        assert listed[1].created_at == T1

    def test_list_snapshots_empty_when_dir_missing(self, source_env, tmp_path):
        provider = make_provider(source_env, tmp_path / "never-created")
        assert provider.list_snapshots() == []

    def test_restore_by_bare_name(self, source_env, fresh_locations, tmp_path, sqlite_dump):
        exporter = make_provider(source_env, tmp_path / "backups", clock=lambda: T1)
        info = exporter.export_snapshot()

        target, target_root = fresh_locations()
        restorer = LocalDirectoryProvider(
            tmp_path / "backups", locations=target, passphrase=source_env.passphrase
        )
        result = restorer.restore(info.name)
        assert sqlite_dump(target_root / "data" / "healthmes.db") == sqlite_dump(
            source_env.db_path
        )
        assert result.recovery_mode == "recoverable_local_swaps"
        assert result.recovered_components == (
            "healthmes_db",
            "media",
            "hermes_home",
        )
        assert result.skipped_components == (
            "open_wearables_db",
            "raw_ingest",
        )
        assert result["schema_version"] == 2

    def test_restore_rejects_snapshot_replaced_after_generation_selection(
        self,
        source_env,
        fresh_locations,
        tmp_path,
        monkeypatch,
    ):
        backup_dir = tmp_path / "backups"
        clocks = iter((T1, T2))
        exporter = make_provider(
            source_env,
            backup_dir,
            clock=lambda: next(clocks),
        )
        selected = exporter.export_snapshot()
        replacement_source = exporter.export_snapshot()
        replacement = tmp_path / "replacement.age"
        replacement.write_bytes(replacement_source.path.read_bytes())
        target, target_root = fresh_locations("replace-before-open")
        restorer = LocalDirectoryProvider(
            backup_dir,
            locations=target,
            passphrase=source_env.passphrase,
        )
        real_open = local_mod.open_regular_file
        replaced = False

        def replace_then_open(path):
            nonlocal replaced
            if not replaced:
                replaced = True
                os.replace(replacement, path)
            return real_open(path)

        monkeypatch.setattr(local_mod, "open_regular_file", replace_then_open)

        with pytest.raises(
            BackupError,
            match="changed after it was selected for restore",
        ):
            restorer.restore(selected.name)

        assert replaced is True
        assert not (target_root / "data").exists()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor traversal contract")
    def test_restore_rejects_backup_ancestor_replaced_after_generation_selection(
        self,
        source_env,
        fresh_locations,
        tmp_path,
        monkeypatch,
    ):
        backup_dir = tmp_path / "backups"
        exporter = make_provider(
            source_env,
            backup_dir,
            clock=lambda: T1,
        )
        selected = exporter.export_snapshot()
        displaced_backup_dir = tmp_path / "displaced-backups"
        replacement_backup_dir = tmp_path / "replacement-backups"
        replacement_backup_dir.mkdir()
        replacement_path = replacement_backup_dir / selected.name
        replacement_path.write_bytes(selected.path.read_bytes())
        target, target_root = fresh_locations("replace-ancestor-before-open")
        restorer = LocalDirectoryProvider(
            backup_dir,
            locations=target,
            passphrase=source_env.passphrase,
        )
        real_open = local_mod.open_regular_file
        replaced = False

        def replace_ancestor_then_open(path):
            nonlocal replaced
            if not replaced:
                replaced = True
                backup_dir.rename(displaced_backup_dir)
                replacement_backup_dir.rename(backup_dir)
            return real_open(path)

        monkeypatch.setattr(
            local_mod,
            "open_regular_file",
            replace_ancestor_then_open,
        )

        with pytest.raises(
            BackupError,
            match="changed after it was selected for restore",
        ):
            restorer.restore(selected.name)

        assert replaced is True
        assert not (target_root / "data").exists()

    def test_restore_unknown_name_fails(self, source_env, tmp_path):
        provider = make_provider(source_env, tmp_path / "backups")
        with pytest.raises(BackupError, match="snapshot not found"):
            provider.restore("healthmes-backup-19700101T000000Z.tar.gz.age")

    def test_export_without_passphrase_fails_cleanly(self, source_env, tmp_path):
        provider = make_provider(source_env, tmp_path / "backups", passphrase=None)
        with pytest.raises(BackupError, match="HEALTHMES_BACKUP_PASSPHRASE"):
            provider.export_snapshot()


class TestSettingsResolution:
    def test_backup_dir_defaults_under_data_dir(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HEALTHMES_BACKUP_DIR", raising=False)
        settings = make_settings(tmp_path)
        assert resolve_backup_dir(settings) == tmp_path / "data" / "backups"

    def test_backup_dir_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HEALTHMES_BACKUP_DIR", str(tmp_path / "vault"))
        settings = make_settings(tmp_path)
        assert resolve_backup_dir(settings) == tmp_path / "vault"

    def test_passphrase_env_fallback(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HEALTHMES_BACKUP_PASSPHRASE", raising=False)
        settings = make_settings(tmp_path)
        assert resolve_passphrase(settings) is None
        monkeypatch.setenv("HEALTHMES_BACKUP_PASSPHRASE", "s3cret")
        assert resolve_passphrase(settings) == "s3cret"

    def test_data_locations_resolution(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HEALTHMES_OW_API_KEY", raising=False)
        monkeypatch.delenv("HEALTHMES_OW_DATABASE_URL", raising=False)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        settings = make_settings(tmp_path)
        locations = resolve_data_locations(settings)
        assert locations.database_url == settings.database_url
        assert locations.media_dir == tmp_path / "data" / "media"
        assert locations.ow_database_url is None
        assert locations.ow_runtime_configured is False
        assert locations.hermes_home is None

        monkeypatch.setenv("HEALTHMES_OW_API_KEY", "runtime-key")
        monkeypatch.setenv("HEALTHMES_OW_DATABASE_URL", "postgresql+psycopg://ow@localhost/ow")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        locations = resolve_data_locations(settings)
        assert locations.ow_database_url == "postgresql+psycopg://ow@localhost/ow"
        assert locations.ow_runtime_configured is True
        assert locations.hermes_home == tmp_path / "hermes"

    def test_from_settings_wires_everything(self, source_env, tmp_path, monkeypatch):
        monkeypatch.setenv("HEALTHMES_BACKUP_DIR", str(tmp_path / "vault"))
        monkeypatch.setenv("HEALTHMES_BACKUP_PASSPHRASE", source_env.passphrase)
        monkeypatch.setenv("HERMES_HOME", str(source_env.hermes_home))
        monkeypatch.delenv("HEALTHMES_OW_DATABASE_URL", raising=False)
        settings = Settings(
            database_url=source_env.database_url,
            data_dir=source_env.data_dir,
            scheduler_enabled=False,
            _env_file=None,
        )
        provider = LocalDirectoryProvider.from_settings(settings)
        info = provider.export_snapshot()
        assert info.path.parent == tmp_path / "vault"


class TestWeeklyJob:
    def test_skips_with_warning_when_no_passphrase(self, tmp_path, monkeypatch, caplog):
        monkeypatch.delenv("HEALTHMES_BACKUP_PASSPHRASE", raising=False)
        settings = make_settings(tmp_path)
        job = build_backup_job(settings)
        with caplog.at_level("WARNING", logger="healthmes.backup.local"):
            job()  # must not raise
        assert "no passphrase configured" in caplog.text

    def test_writes_snapshot_when_configured(self, source_env, tmp_path, monkeypatch):
        backup_dir = tmp_path / "weekly"
        monkeypatch.setenv("HEALTHMES_BACKUP_DIR", str(backup_dir))
        monkeypatch.setenv("HEALTHMES_BACKUP_PASSPHRASE", source_env.passphrase)
        monkeypatch.setenv("HERMES_HOME", str(source_env.hermes_home))
        monkeypatch.delenv("HEALTHMES_OW_DATABASE_URL", raising=False)
        settings = Settings(
            database_url=source_env.database_url,
            data_dir=source_env.data_dir,
            scheduler_enabled=False,
            _env_file=None,
        )
        build_backup_job(settings)()
        snapshots = list(backup_dir.glob("healthmes-backup-*.tar.gz.age"))
        assert len(snapshots) == 1

    def test_writes_partial_snapshot_and_warns_when_runtime_ow_dump_is_absent(
        self, source_env, tmp_path, monkeypatch, caplog
    ):
        backup_dir = tmp_path / "weekly"
        monkeypatch.setenv("HEALTHMES_BACKUP_DIR", str(backup_dir))
        monkeypatch.setenv("HEALTHMES_BACKUP_PASSPHRASE", source_env.passphrase)
        monkeypatch.setenv("HEALTHMES_OW_API_KEY", "runtime-key")
        monkeypatch.delenv("HEALTHMES_OW_DATABASE_URL", raising=False)
        settings = Settings(
            database_url=source_env.database_url,
            data_dir=source_env.data_dir,
            scheduler_enabled=False,
            _env_file=None,
        )

        with caplog.at_level("WARNING", logger="healthmes.backup.snapshot"):
            build_backup_job(settings)()

        snapshots = list(backup_dir.glob("healthmes-backup-*.tar.gz.age"))
        assert len(snapshots) == 1
        assert "Partial backup" in caplog.text
        assert "cannot recover that data" in caplog.text

    def test_logs_and_swallows_failures(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("HEALTHMES_BACKUP_PASSPHRASE", "pp")
        settings = make_settings(tmp_path, database_url="sqlite:///:memory:")
        job = build_backup_job(settings)
        with caplog.at_level("ERROR", logger="healthmes.backup.local"):
            job()  # in-memory sqlite cannot be dumped; job must swallow it
        assert "Weekly backup failed" in caplog.text

    def test_registers_on_scheduler_hook(self, tmp_path):
        """The callable plugs into the hook left by the triggers scope."""
        from healthmes.engine.scheduler import (
            BACKUP_JOB_ID,
            create_scheduler,
            register_backup_job,
        )

        settings = make_settings(tmp_path)
        scheduler = create_scheduler(settings)
        try:
            job = register_backup_job(scheduler, build_backup_job(settings))
            assert job.id == BACKUP_JOB_ID
            assert any(j.id == BACKUP_JOB_ID for j in scheduler.get_jobs())
        finally:
            if scheduler.running:  # pragma: no cover — never started here
                scheduler.shutdown(wait=False)


class TestRoundTripThroughProvider:
    def test_full_cycle(self, source_env, fresh_locations, tmp_path, tree_snapshot, sqlite_dump):
        """create -> list -> restore through the provider surface only."""
        backup_dir = tmp_path / "cycle"
        exporter = make_provider(source_env, backup_dir, clock=lambda: T1)
        exported = exporter.export_snapshot()

        target, target_root = fresh_locations("cycle-target")
        restorer = LocalDirectoryProvider(
            backup_dir, locations=target, passphrase=source_env.passphrase
        )
        listed = restorer.list_snapshots()
        assert [info.name for info in listed] == [exported.name]

        result = restorer.restore(listed[0].path)
        assert sqlite_dump(target_root / "data" / "healthmes.db") == sqlite_dump(
            source_env.db_path
        )
        assert tree_snapshot(target.media_dir) == tree_snapshot(source_env.media_dir)
        assert result.manifest["contents"]["media"] is not None


def test_data_locations_is_frozen():
    locations = DataLocations(database_url="sqlite:///x.db")
    with pytest.raises(AttributeError):
        locations.database_url = "other"  # type: ignore[misc]
