"""Focused resource-boundary tests for local snapshots and Remote Vault."""

from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import stat
import subprocess
import sys
import tarfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pyrage import passphrase as age_passphrase

from healthmes.backup import filesystem as filesystem_mod
from healthmes.backup import snapshot as snapshot_mod
from healthmes.backup.limits import SnapshotResourceLimits
from healthmes.backup.local import LocalDirectoryProvider
from healthmes.backup.provider import (
    BackupError,
    BackupPublicationError,
    SnapshotIntegrityError,
)
from healthmes.backup.remote_vault import RemoteVaultProvider, VaultConfig
from healthmes.backup.snapshot import (
    DataLocations,
    create_snapshot,
    read_manifest,
    restore_snapshot,
    snapshot_name,
)

CREATED_AT = datetime(2026, 8, 18, 3, 30, tzinfo=UTC)
PASSPHRASE = "resource-limit-test-passphrase"


@dataclass(frozen=True, slots=True)
class SealedSnapshot:
    path: Path
    plaintext: bytes
    member_count: int
    max_member_bytes: int
    expanded_bytes: int
    compression_ratio: float


class TrackedBody:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._offset = 0
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._payload):
            return b""
        if size < 0:
            size = len(self._payload) - self._offset
        chunk = self._payload[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class FailingBody:
    def __init__(self) -> None:
        self.closed = False

    def read(self, _size: int = -1) -> bytes:
        raise ValueError("injected stream failure")

    def close(self) -> None:
        self.closed = True


class CallbackBody(TrackedBody):
    def __init__(self, payload: bytes, callback) -> None:
        super().__init__(payload)
        self._callback = callback
        self._called = False

    def read(self, size: int = -1) -> bytes:
        if not self._called:
            self._called = True
            self._callback()
        return super().read(size)


class ObjectClient:
    def __init__(self, response: object) -> None:
        self.response = response

    def get_object(self, **_kwargs) -> object:
        return self.response


def _limits(**overrides) -> SnapshotResourceLimits:
    values = {
        "max_encrypted_bytes": 512 * 1024 * 1024,
        "max_decrypted_bytes": 768 * 1024 * 1024,
        "max_members": 100_000,
        "max_member_bytes": 1024 * 1024 * 1024,
        "max_expanded_bytes": 4 * 1024 * 1024 * 1024,
        "max_identity_depth": 128,
        "identity_traversal_timeout_seconds": 300.0,
        "max_compression_ratio": 100.0,
        "min_free_bytes": 0,
    }
    values.update(overrides)
    return SnapshotResourceLimits(**values)


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory descriptor contract")
def test_atomic_writer_never_overwrites_a_replacement_parent(tmp_path) -> None:
    parent = tmp_path / "target"
    parent.mkdir()
    destination = parent / "snapshot.age"
    displaced = tmp_path / "displaced"

    with pytest.raises(OSError, match="destination directory changed"):
        with filesystem_mod.durable_atomic_writer(destination) as output:
            output.write(b"verified ciphertext")
            parent.rename(displaced)
            parent.mkdir()
            destination.write_bytes(b"new directory generation")

    assert destination.read_bytes() == b"new directory generation"
    assert not (displaced / "snapshot.age").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX no-clobber link contract")
def test_atomic_writer_recovers_from_one_temporary_unlink_failure(
    tmp_path,
    monkeypatch,
) -> None:
    destination = tmp_path / "snapshot.age"
    payload = b"durable no-clobber snapshot"
    real_unlink = filesystem_mod.os.unlink
    real_fsync = filesystem_mod.os.fsync
    unlink_calls = 0
    events: list[str] = []

    def fail_first_temporary_unlink(path, *args, **kwargs):
        nonlocal unlink_calls
        if (
            isinstance(path, str)
            and path.startswith(".snapshot.age.")
            and path.endswith(".tmp")
        ):
            unlink_calls += 1
            events.append(f"unlink-{unlink_calls}")
            if unlink_calls == 1:
                raise OSError("injected transient temporary unlink failure")
        return real_unlink(path, *args, **kwargs)

    def tracked_fsync(descriptor):
        events.append("fsync")
        return real_fsync(descriptor)

    monkeypatch.setattr(
        filesystem_mod.os,
        "unlink",
        fail_first_temporary_unlink,
    )
    monkeypatch.setattr(filesystem_mod.os, "fsync", tracked_fsync)

    with filesystem_mod.durable_atomic_writer(
        destination,
        replace_existing=False,
    ) as output:
        output.write(payload)

    assert destination.read_bytes() == payload
    assert unlink_calls == 2
    assert list(tmp_path.glob(".snapshot.age.*.tmp")) == []
    assert events.index("unlink-2") < len(events) - 1
    assert "fsync" in events[events.index("unlink-2") + 1 :]


@pytest.mark.skipif(os.name == "nt", reason="POSIX no-clobber link contract")
def test_atomic_writer_reports_created_destination_after_parent_fsync_failure(
    tmp_path,
    monkeypatch,
) -> None:
    destination = tmp_path / "snapshot.age"
    payload = b"published before directory fsync acknowledgement"
    parent_identity = (tmp_path.stat().st_dev, tmp_path.stat().st_ino)
    real_fsync = filesystem_mod.os.fsync

    def fail_parent_fsync(descriptor):
        metadata = os.fstat(descriptor)
        if (
            stat.S_ISDIR(metadata.st_mode)
            and (metadata.st_dev, metadata.st_ino) == parent_identity
        ):
            raise OSError("injected backup destination-directory fsync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(filesystem_mod.os, "fsync", fail_parent_fsync)

    with pytest.raises(BackupPublicationError) as raised:
        with filesystem_mod.durable_atomic_writer(
            destination,
            replace_existing=False,
        ) as output:
            output.write(payload)

    assert raised.value.destination_created is True
    assert destination.read_bytes() == payload
    assert list(tmp_path.glob(".snapshot.age.*.tmp")) == []
    with pytest.raises(FileExistsError):
        with filesystem_mod.durable_atomic_writer(
            destination,
            replace_existing=False,
        ) as output:
            output.write(b"must not replace the published generation")


@pytest.mark.skipif(os.name == "nt", reason="POSIX cleanup injection contract")
def test_atomic_writer_cleanup_failure_does_not_mask_primary_exception(
    tmp_path,
    monkeypatch,
) -> None:
    destination = tmp_path / "snapshot.age"
    real_unlink = filesystem_mod.os.unlink

    def fail_temporary_cleanup(path, *args, **kwargs):
        if (
            isinstance(path, str)
            and path.startswith(".snapshot.age.")
            and path.endswith(".tmp")
        ):
            raise OSError("injected temporary cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(
        filesystem_mod.os,
        "unlink",
        fail_temporary_cleanup,
    )

    with pytest.raises(RuntimeError, match="primary publication failure") as raised:
        with filesystem_mod.durable_atomic_writer(destination) as output:
            output.write(b"unpublished ciphertext")
            raise RuntimeError("primary publication failure")

    assert any(
        "suppressed backup publication cleanup failure: OSError" in note
        for note in getattr(raised.value, "__notes__", ())
    )
    assert not destination.exists()


def _source_locations(
    root: Path,
    *,
    limits: SnapshotResourceLimits | None = None,
) -> DataLocations:
    db_path = root / "data" / "healthmes.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO events (payload) VALUES (?)",
            [(f"wellness-event-{index}",) for index in range(8)],
        )
        connection.commit()
    finally:
        connection.close()

    media_dir = root / "data" / "media"
    media_dir.mkdir(parents=True)
    (media_dir / "compressible.bin").write_bytes(b"A" * (16 * 1024))
    (media_dir / "note.txt").write_text("short wellness note\n", encoding="utf-8")
    return DataLocations(
        database_url=f"sqlite:///{db_path}",
        media_dir=media_dir,
        resource_limits=limits or _limits(),
    )


def _restore_locations(root: Path, limits: SnapshotResourceLimits) -> DataLocations:
    return DataLocations(
        database_url=f"sqlite:///{root / 'data' / 'healthmes.db'}",
        media_dir=root / "data" / "media",
        restore_state_dir=root / ".restore",
        resource_limits=limits,
    )


def _create_snapshot(locations: DataLocations, root: Path) -> Path:
    out_path = root / "backups" / snapshot_name(CREATED_AT)
    create_snapshot(
        locations,
        passphrase=PASSPHRASE,
        out_path=out_path,
        created_at=CREATED_AT,
    )
    return out_path


@pytest.fixture(scope="module")
def sealed_snapshot(tmp_path_factory) -> SealedSnapshot:
    root = tmp_path_factory.mktemp("resource-limits")
    path = _create_snapshot(_source_locations(root / "source"), root)
    plaintext = age_passphrase.decrypt(path.read_bytes(), PASSPHRASE)
    with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:gz") as archive:
        members = archive.getmembers()
    file_sizes = [member.size for member in members if member.isfile()]
    expanded_bytes = sum(file_sizes)
    compression_ratio = expanded_bytes / len(plaintext)
    assert len(members) > 1
    assert expanded_bytes > max(file_sizes) > 1
    assert compression_ratio > 1
    return SealedSnapshot(
        path=path,
        plaintext=plaintext,
        member_count=len(members),
        max_member_bytes=max(file_sizes),
        expanded_bytes=expanded_bytes,
        compression_ratio=compression_ratio,
    )


@pytest.mark.parametrize(
    "field",
    [
        "max_encrypted_bytes",
        "max_decrypted_bytes",
        "max_members",
        "max_member_bytes",
        "max_expanded_bytes",
        "max_identity_depth",
    ],
)
@pytest.mark.parametrize("bad_value", [0, -1, True, 1.5, "1", None])
def test_integer_limits_require_strictly_positive_integers(field, bad_value):
    with pytest.raises(ValueError, match="snapshot resource limits must be positive"):
        _limits(**{field: bad_value})


@pytest.mark.parametrize("bad_value", [-1, True, 1.5, "0", None])
def test_free_space_reserve_requires_a_non_negative_integer(bad_value):
    with pytest.raises(ValueError, match="free-space reserve must be non-negative"):
        _limits(min_free_bytes=bad_value)


@pytest.mark.parametrize(
    "bad_value",
    [1, 0, -1, True, float("nan"), float("inf"), "2", None],
)
def test_compression_ratio_requires_a_finite_number_greater_than_one(bad_value):
    with pytest.raises(ValueError, match="compression ratio limit must be greater than one"):
        _limits(max_compression_ratio=bad_value)


@pytest.mark.parametrize(
    "bad_value",
    [0, -1, True, float("nan"), float("inf"), "2", None],
)
def test_identity_timeout_requires_a_positive_finite_number(bad_value):
    with pytest.raises(
        ValueError,
        match="identity traversal timeout must be a positive finite number",
    ):
        _limits(identity_traversal_timeout_seconds=bad_value)


def test_member_limit_cannot_exceed_expanded_limit():
    with pytest.raises(ValueError, match="member limit cannot exceed expanded archive limit"):
        _limits(max_member_bytes=11, max_expanded_bytes=10)


def test_strict_boundary_values_are_accepted():
    assert SnapshotResourceLimits(
        max_encrypted_bytes=1,
        max_decrypted_bytes=1,
        max_members=1,
        max_member_bytes=1,
        max_expanded_bytes=1,
        max_identity_depth=1,
        identity_traversal_timeout_seconds=0.001,
        max_compression_ratio=1.0001,
        min_free_bytes=0,
    ).min_free_bytes == 0


def _hash_identity_tree(
    path: Path,
    *,
    phase: snapshot_mod._IdentityPhaseBudget,
) -> tuple[os.stat_result, str]:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        return snapshot_mod._hash_directory_descriptor(
            descriptor,
            traversal=phase.traversal(label=str(path)),
            label=str(path),
        )
    finally:
        os.close(descriptor)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor traversal contract")
def test_restore_identity_phase_shares_entry_budget_across_trees(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "one.bin").write_bytes(b"1")
    (second / "two.bin").write_bytes(b"2")
    limits = _limits(max_members=3)

    with snapshot_mod._identity_phase_scope(
        limits,
        phase="test restore identity",
    ) as phase:
        _hash_identity_tree(first, phase=phase)
        with pytest.raises(
            BackupError,
            match="more than 3 identity traversal entries",
        ):
            _hash_identity_tree(second, phase=phase)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor traversal contract")
def test_restore_identity_rejects_tree_beyond_depth_limit(tmp_path):
    root = tmp_path / "root"
    nested = root / "level-one" / "level-two"
    nested.mkdir(parents=True)
    (nested / "payload.bin").write_bytes(b"x")

    with snapshot_mod._identity_phase_scope(
        _limits(max_identity_depth=1),
        phase="test restore identity",
    ) as phase:
        with pytest.raises(
            BackupError,
            match="1-level identity traversal depth",
        ):
            _hash_identity_tree(root, phase=phase)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor traversal contract")
def test_restore_identity_rejects_oversized_regular_file(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload.bin").write_bytes(b"x" * 9)

    with snapshot_mod._identity_phase_scope(
        _limits(max_member_bytes=8, max_expanded_bytes=8),
        phase="test restore identity",
    ) as phase:
        with pytest.raises(
            BackupError,
            match="8-byte identity traversal limit",
        ):
            _hash_identity_tree(root, phase=phase)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor traversal contract")
def test_restore_identity_phase_shares_expanded_byte_budget(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "one.bin").write_bytes(b"1" * 6)
    (second / "two.bin").write_bytes(b"2" * 6)

    with snapshot_mod._identity_phase_scope(
        _limits(max_member_bytes=8, max_expanded_bytes=10),
        phase="test restore identity",
    ) as phase:
        _hash_identity_tree(first, phase=phase)
        with pytest.raises(
            BackupError,
            match="10-byte identity traversal limit",
        ):
            _hash_identity_tree(second, phase=phase)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor traversal contract")
def test_restore_identity_enforces_one_absolute_phase_deadline(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload.bin").write_bytes(b"x")
    ticks = iter((0.0, 1.0))
    monkeypatch.setattr(snapshot_mod, "monotonic", lambda: next(ticks))

    with snapshot_mod._identity_phase_scope(
        _limits(identity_traversal_timeout_seconds=0.5),
        phase="test restore identity",
    ) as phase:
        with pytest.raises(
            BackupError,
            match="0.5-second identity traversal deadline",
        ):
            _hash_identity_tree(root, phase=phase)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor traversal contract")
def test_restore_identity_never_uses_pathname_listdir(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload.bin").write_bytes(b"x")
    monkeypatch.setattr(
        snapshot_mod.os,
        "listdir",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("identity traversal must use descriptor scandir")
        ),
    )

    with snapshot_mod._identity_phase_scope(
        _limits(),
        phase="test restore identity",
    ) as phase:
        _hash_identity_tree(root, phase=phase)


@pytest.mark.skipif(os.name == "nt", reason="symlink descriptor contract")
def test_restore_identity_rejects_symlink_target_change(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "root"
    root.mkdir()
    (root / "first").write_bytes(b"1")
    (root / "second").write_bytes(b"2")
    link = root / "current"
    link.symlink_to("first")
    real_readlink = snapshot_mod.os.readlink
    calls = 0

    def replace_after_first_read(path, *args, **kwargs):
        nonlocal calls
        target = real_readlink(path, *args, **kwargs)
        calls += 1
        if calls == 1:
            link.unlink()
            link.symlink_to("second")
        return target

    monkeypatch.setattr(snapshot_mod.os, "readlink", replace_after_first_read)

    with snapshot_mod._identity_phase_scope(
        _limits(),
        phase="test restore identity",
    ) as phase:
        with pytest.raises(BackupError, match="symlink changed"):
            _hash_identity_tree(root, phase=phase)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor traversal contract")
def test_restore_identity_reads_exact_recorded_file_size(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"12345678")
    descriptor = os.open(
        target,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    real_read = snapshot_mod.os.read
    reads = 0

    def grow_after_first_read(file_descriptor, size):
        nonlocal reads
        chunk = real_read(file_descriptor, size)
        if file_descriptor == descriptor and reads == 0:
            reads += 1
            with target.open("ab") as output:
                output.write(b"9")
                output.flush()
                os.fsync(output.fileno())
        return chunk

    monkeypatch.setattr(snapshot_mod.os, "read", grow_after_first_read)
    try:
        with snapshot_mod._identity_phase_scope(
            _limits(),
            phase="test restore identity",
        ) as phase:
            with pytest.raises(BackupError, match="changed size"):
                snapshot_mod._hash_regular_descriptor(
                    descriptor,
                    traversal=phase.traversal(label=str(target)),
                    label=str(target),
                )
    finally:
        os.close(descriptor)


def test_create_rejects_an_oversized_encrypted_envelope(tmp_path, monkeypatch):
    limits = _limits(max_encrypted_bytes=8)
    locations = _source_locations(tmp_path / "source", limits=limits)
    out_path = tmp_path / "backups" / snapshot_name(CREATED_AT)
    monkeypatch.setattr(
        snapshot_mod,
        "_tar_gz_bytes",
        lambda _stage, *, limits: b"x",
    )
    monkeypatch.setattr(
        snapshot_mod.age_passphrase,
        "encrypt",
        lambda _plaintext, _passphrase: b"x" * 9,
    )

    with pytest.raises(BackupError, match="encrypted snapshot exceeds.*8-byte limit"):
        _create_snapshot(locations, tmp_path)

    assert not out_path.exists()
    assert not out_path.with_name(out_path.name + ".part").exists()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires Windows privileges")
def test_create_does_not_follow_predictable_part_symlink(tmp_path):
    locations = _source_locations(tmp_path / "source")
    out_path = tmp_path / "backups" / snapshot_name(CREATED_AT)
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"must survive")
    out_path.parent.mkdir(parents=True)
    out_path.with_name(out_path.name + ".part").symlink_to(victim)

    _create_snapshot(locations, tmp_path)

    assert victim.read_bytes() == b"must survive"
    assert out_path.is_file() and not out_path.is_symlink()
    assert out_path.with_name(out_path.name + ".part").is_symlink()


def test_create_rejects_an_oversized_decrypted_payload(tmp_path):
    locations = _source_locations(
        tmp_path / "source",
        limits=_limits(max_decrypted_bytes=1),
    )

    with pytest.raises(BackupError, match="compressed snapshot payload exceeds.*1-byte limit"):
        _create_snapshot(locations, tmp_path)


def test_create_rejects_too_many_archive_members(tmp_path):
    locations = _source_locations(
        tmp_path / "source",
        limits=_limits(max_members=1),
    )

    with pytest.raises(BackupError, match="more than 1 (files|archive members)"):
        _create_snapshot(locations, tmp_path)


def test_create_rejects_an_oversized_member(tmp_path):
    locations = _source_locations(tmp_path / "source")
    database_bytes = Path(
        locations.database_url.removeprefix("sqlite:///")
    ).stat().st_size
    locations = replace(
        locations,
        resource_limits=_limits(max_member_bytes=database_bytes),
    )

    with pytest.raises(
        BackupError,
        match=rf"compressible\.bin exceeds.*{database_bytes}-byte limit",
    ):
        _create_snapshot(locations, tmp_path)


def test_create_rejects_excessive_expanded_bytes(tmp_path):
    locations = _source_locations(tmp_path / "source")
    largest_source = max(
        locations.media_dir.joinpath("compressible.bin").stat().st_size,
        Path(locations.database_url.removeprefix("sqlite:///")).stat().st_size,
    )
    locations = replace(
        locations,
        resource_limits=_limits(
            max_member_bytes=largest_source,
            max_expanded_bytes=largest_source,
        ),
    )

    with pytest.raises(BackupError, match="snapshot expands beyond the configured"):
        _create_snapshot(locations, tmp_path)


def test_create_rejects_excessive_compression_ratio(tmp_path):
    locations = _source_locations(
        tmp_path / "source",
        limits=_limits(max_compression_ratio=1.0001),
    )

    with pytest.raises(BackupError, match="compression ratio exceeds"):
        _create_snapshot(locations, tmp_path)


def test_create_enforces_free_space_reserve(tmp_path, monkeypatch):
    limits = _limits(max_encrypted_bytes=8, min_free_bytes=1)
    locations = _source_locations(tmp_path / "source", limits=limits)
    out_path = tmp_path / "backups" / snapshot_name(CREATED_AT)
    monkeypatch.setattr(
        snapshot_mod,
        "_tar_gz_bytes",
        lambda _stage, *, limits: b"x",
    )
    monkeypatch.setattr(
        snapshot_mod.age_passphrase,
        "encrypt",
        lambda _plaintext, _passphrase: b"x" * 8,
    )

    output_directory = out_path.parent.resolve()

    def disk_usage(path):
        if Path(path).resolve() == output_directory:
            return SimpleNamespace(free=8)
        return SimpleNamespace(free=1024 * 1024 * 1024)

    monkeypatch.setattr(
        snapshot_mod.shutil,
        "disk_usage",
        disk_usage,
    )

    with pytest.raises(BackupError, match="insufficient disk space for encrypted snapshot output"):
        _create_snapshot(locations, tmp_path)

    assert not out_path.exists()
    assert not out_path.with_name(out_path.name + ".part").exists()


def test_sqlite_stage_rejects_oversize_before_opening_destination(
    tmp_path,
    monkeypatch,
):
    locations = _source_locations(tmp_path / "source")
    source = Path(locations.database_url.removeprefix("sqlite:///"))
    stage = tmp_path / "stage"
    stage.mkdir()
    destination = stage / "db" / "healthmes.sqlite3"
    limits = _limits(max_member_bytes=1)
    real_connect = sqlite3.connect
    opened: list[str] = []

    def tracked_connect(database, *args, **kwargs):
        opened.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(snapshot_mod.sqlite3, "connect", tracked_connect)

    with pytest.raises(BackupError, match="healthmes.sqlite3 exceeds"):
        snapshot_mod._sqlite_snapshot_to(
            source,
            destination,
            stage=stage,
            budget=snapshot_mod._StageBudget(limits),
        )

    assert opened == [f"file:{source}?mode=ro"]
    assert not destination.exists()


def test_tree_stage_rejects_oversize_without_copying_or_leaving_partial(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source.bin"
    source.write_bytes(b"x" * 32)
    stage = tmp_path / "stage"
    stage.mkdir()
    destination = stage / "media" / source.name
    read_attempted = False

    def unexpected_read(*_args, **_kwargs):
        nonlocal read_attempted
        read_attempted = True
        raise AssertionError("oversized source must be rejected before copying")

    monkeypatch.setattr(snapshot_mod.os, "read", unexpected_read)

    with pytest.raises(BackupError, match="source.bin exceeds.*8-byte limit"):
        snapshot_mod._copy_regular_file_to_stage(
            source,
            destination,
            stage=stage,
            budget=snapshot_mod._StageBudget(
                _limits(max_member_bytes=8),
            ),
        )

    assert read_attempted is False
    assert not destination.exists()


def test_tree_stage_rejects_same_size_rewrite_during_multi_chunk_copy(
    tmp_path,
    monkeypatch,
):
    chunk_size = snapshot_mod._STAGE_COPY_CHUNK_BYTES
    source = tmp_path / "source.bin"
    source.write_bytes((b"a" * chunk_size) + (b"b" * chunk_size))
    original_metadata = source.stat()
    stage = tmp_path / "stage"
    stage.mkdir()
    destination = stage / "media" / source.name
    real_read = snapshot_mod.os.read
    read_count = 0

    def mutate_after_first_chunk(descriptor, size):
        nonlocal read_count
        chunk = real_read(descriptor, size)
        if chunk and read_count == 0:
            read_count += 1
            with source.open("r+b") as handle:
                handle.seek(chunk_size)
                handle.write(b"c" * chunk_size)
                handle.flush()
                os.fsync(handle.fileno())
            os.utime(
                source,
                ns=(
                    original_metadata.st_atime_ns,
                    original_metadata.st_mtime_ns,
                ),
            )
        return chunk

    monkeypatch.setattr(snapshot_mod.os, "read", mutate_after_first_chunk)

    with pytest.raises(BackupError, match="changed while being copied"):
        snapshot_mod._copy_regular_file_to_stage(
            source,
            destination,
            stage=stage,
            budget=snapshot_mod._StageBudget(_limits()),
        )

    assert read_count == 1
    assert not destination.exists()


def _write_executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.mark.parametrize(
    ("stream", "redirect"),
    (("stdout", ""), ("stderr", " >&2")),
)
def test_pg_tool_rejects_excessive_output(
    tmp_path,
    monkeypatch,
    stream,
    redirect,
):
    tool = _write_executable(
        tmp_path / f"noisy-{stream}",
        f"#!/bin/sh\nprintf '0123456789abcdef0123456789abcdef'{redirect}\n",
    )
    monkeypatch.setattr(
        snapshot_mod,
        "_require_pg_tool",
        lambda *_args, **_kwargs: tool,
    )
    monkeypatch.setattr(snapshot_mod, "_POSTGRES_TOOL_STDOUT_BYTES", 8)
    monkeypatch.setattr(snapshot_mod, "_POSTGRES_TOOL_STDERR_BYTES", 8)

    with pytest.raises(
        BackupError,
        match=rf"{stream} exceeds the configured 8-byte limit",
    ):
        snapshot_mod._run_pg_tool(
            "psql",
            [],
            action="run a bounded test",
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX process signal contract")
def test_pg_tool_that_never_exits_is_stopped_at_timeout(
    tmp_path,
    monkeypatch,
):
    tool = _write_executable(
        tmp_path / "hung-pg-tool",
        (
            f"#!{sys.executable}\n"
            "import signal\n"
            "import time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "while True:\n"
            "    time.sleep(1)\n"
        ),
    )
    monkeypatch.setattr(
        snapshot_mod,
        "_require_pg_tool",
        lambda *_args, **_kwargs: tool,
    )
    monkeypatch.setattr(snapshot_mod, "_POSTGRES_TOOL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(snapshot_mod, "_POSTGRES_TOOL_POLL_SECONDS", 0.005)
    monkeypatch.setattr(snapshot_mod, "_POSTGRES_TARGET_STOP_TIMEOUT_SECONDS", 0.5)

    with pytest.raises(BackupError, match="timed out after 0.05 seconds"):
        snapshot_mod._run_pg_tool(
            "psql",
            [],
            action="run a timeout test",
        )


def test_pg_tool_reports_process_that_cannot_be_reaped(
    monkeypatch,
):
    class UnreapableProcess:
        def __init__(self):
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            self.returncode = None

        def poll(self):
            return None

        def terminate(self):
            return None

        def kill(self):
            return None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("psql", timeout)

    monkeypatch.setattr(
        snapshot_mod,
        "_require_pg_tool",
        lambda *_args, **_kwargs: Path("/fake/psql"),
    )
    monkeypatch.setattr(
        snapshot_mod.subprocess,
        "Popen",
        lambda *_args, **_kwargs: UnreapableProcess(),
    )
    monkeypatch.setattr(snapshot_mod, "_POSTGRES_TOOL_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(snapshot_mod, "_POSTGRES_TOOL_POLL_SECONDS", 0.001)
    monkeypatch.setattr(snapshot_mod, "_POSTGRES_TARGET_STOP_TIMEOUT_SECONDS", 0.0)

    with pytest.raises(BackupError, match="could not be reaped"):
        snapshot_mod._run_pg_tool(
            "psql",
            [],
            action="run an unreapable test",
        )


def test_pg_tool_cancellation_stops_and_reaps_child(monkeypatch):
    class InterruptibleProcess:
        def __init__(self):
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            self.returncode = None
            self.terminated = False
            self.wait_calls = 0

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            self.wait_calls += 1
            return self.returncode

    process = InterruptibleProcess()
    monkeypatch.setattr(
        snapshot_mod,
        "_require_pg_tool",
        lambda *_args, **_kwargs: Path("/fake/psql"),
    )
    monkeypatch.setattr(
        snapshot_mod.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )

    def interrupt(_seconds):
        raise KeyboardInterrupt("cancel backup command")

    monkeypatch.setattr(snapshot_mod, "sleep", interrupt)

    with pytest.raises(KeyboardInterrupt, match="cancel backup command"):
        snapshot_mod._run_pg_tool(
            "psql",
            [],
            action="run a cancellable test",
        )

    assert process.terminated is True
    assert process.returncode == -15
    assert process.wait_calls == 1


def test_pg_tool_uses_configured_timeout_above_legacy_thirty_seconds(
    monkeypatch,
):
    class DelayedSuccessProcess:
        def __init__(self):
            self.stdout = io.BytesIO(b"ok")
            self.stderr = io.BytesIO()
            self.returncode = None
            self.poll_calls = 0

        def poll(self):
            self.poll_calls += 1
            if self.poll_calls >= 2:
                self.returncode = 0
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    process = DelayedSuccessProcess()
    clock = iter((100.0, 131.0))
    monkeypatch.setattr(
        snapshot_mod,
        "_require_pg_tool",
        lambda *_args, **_kwargs: Path("/fake/psql"),
    )
    monkeypatch.setattr(
        snapshot_mod.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(snapshot_mod, "monotonic", lambda: next(clock))
    monkeypatch.setattr(snapshot_mod, "sleep", lambda _seconds: None)

    with snapshot_mod._postgres_tool_timeout_scope(91.0):
        output = snapshot_mod._run_pg_tool(
            "psql",
            [],
            action="run beyond the legacy timeout",
        )

    assert output == "ok"
    assert process.returncode == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX process signal contract")
def test_pg_dump_stream_that_never_closes_is_stopped_at_timeout(
    tmp_path,
    monkeypatch,
):
    tool = _write_executable(
        tmp_path / "hung-pg-dump",
        (
            f"#!{sys.executable}\n"
            "import signal\n"
            "import time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "while True:\n"
            "    time.sleep(1)\n"
        ),
    )
    monkeypatch.setattr(
        snapshot_mod,
        "_require_pg_tool",
        lambda *_args, **_kwargs: tool,
    )
    monkeypatch.setattr(snapshot_mod, "_POSTGRES_TOOL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(snapshot_mod, "_POSTGRES_TOOL_POLL_SECONDS", 0.005)
    monkeypatch.setattr(
        snapshot_mod,
        "_POSTGRES_TARGET_STOP_TIMEOUT_SECONDS",
        0.5,
    )
    destination = tmp_path / "hung.dump"

    with pytest.raises(BackupError, match="pg_dump timed out after 0.05"):
        snapshot_mod._pg_dump_to(
            "postgresql://healthmes@invalid/test",
            destination,
            limits=_limits(min_free_bytes=0),
        )

    assert not destination.exists()


def test_pg_dump_output_creation_failure_never_starts_process(
    tmp_path,
    monkeypatch,
):
    destination = tmp_path / "existing.dump"
    destination.write_bytes(b"existing generation")
    process_started = False

    def unexpected_process(*_args, **_kwargs):
        nonlocal process_started
        process_started = True
        pytest.fail("pg_dump must not start without an exclusive output file")

    monkeypatch.setattr(
        snapshot_mod,
        "_require_pg_tool",
        lambda *_args, **_kwargs: Path("/fake/pg-dump"),
    )
    monkeypatch.setattr(
        snapshot_mod.subprocess,
        "Popen",
        unexpected_process,
    )

    with pytest.raises(
        BackupError,
        match="could not create PostgreSQL dump output",
    ):
        snapshot_mod._pg_dump_to(
            "postgresql://healthmes@invalid/test",
            destination,
            limits=_limits(min_free_bytes=0),
        )

    assert process_started is False
    assert destination.read_bytes() == b"existing generation"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process signal contract")
def test_pg_restore_stream_that_never_closes_is_stopped_at_timeout(
    tmp_path,
    monkeypatch,
):
    tool = _write_executable(
        tmp_path / "hung-pg-restore",
        (
            f"#!{sys.executable}\n"
            "import signal\n"
            "import time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "while True:\n"
            "    time.sleep(1)\n"
        ),
    )
    monkeypatch.setattr(
        snapshot_mod,
        "_require_pg_tool",
        lambda *_args, **_kwargs: tool,
    )
    monkeypatch.setattr(snapshot_mod, "_POSTGRES_TOOL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(snapshot_mod, "_POSTGRES_TOOL_POLL_SECONDS", 0.005)
    monkeypatch.setattr(
        snapshot_mod,
        "_POSTGRES_TARGET_STOP_TIMEOUT_SECONDS",
        0.5,
    )
    dump_path = tmp_path / "input.dump"
    dump_path.write_bytes(b"custom archive placeholder")

    with pytest.raises(
        BackupError,
        match="pg_restore timed out after 0.05",
    ):
        snapshot_mod._pg_restore_from(
            "postgresql://healthmes@invalid/test",
            dump_path,
            ("7675026451568287782", 16384),
            limits=_limits(min_free_bytes=0),
        )


def test_stage_tree_discovery_stops_near_member_limit(
    tmp_path,
    monkeypatch,
):
    stage = tmp_path / "stage"
    stage.mkdir()
    for index in range(100):
        (stage / f"{index:04d}.bin").write_bytes(b"x")
    real_scandir = snapshot_mod._scandir
    entries_requested = 0

    class CountingScandir:
        def __init__(self, iterator):
            self._iterator = iterator

        def __enter__(self):
            self._iterator.__enter__()
            return self

        def __exit__(self, exc_type, exc, traceback):
            return self._iterator.__exit__(exc_type, exc, traceback)

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal entries_requested
            entries_requested += 1
            if entries_requested > 2:
                raise AssertionError("stage traversal consumed the full tree")
            return next(self._iterator)

    monkeypatch.setattr(
        snapshot_mod,
        "_scandir",
        lambda path: CountingScandir(real_scandir(path)),
    )

    with pytest.raises(BackupError, match="more than 1 archive members"):
        snapshot_mod._tar_gz_bytes(
            stage,
            limits=_limits(max_members=1),
        )

    assert entries_requested == 2


def test_source_tree_discovery_stops_near_member_limit(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    for index in range(100):
        (source / f"{index:04d}.bin").write_bytes(b"x")
    stage = tmp_path / "stage"
    stage.mkdir()
    real_scandir = snapshot_mod._scandir
    entries_requested = 0

    class CountingScandir:
        def __init__(self, iterator):
            self._iterator = iterator

        def __enter__(self):
            self._iterator.__enter__()
            return self

        def __exit__(self, exc_type, exc, traceback):
            return self._iterator.__exit__(exc_type, exc, traceback)

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal entries_requested
            entries_requested += 1
            if entries_requested > 2:
                raise AssertionError("source traversal consumed the full tree")
            return next(self._iterator)

    monkeypatch.setattr(
        snapshot_mod,
        "_scandir",
        lambda path: CountingScandir(real_scandir(path)),
    )

    with pytest.raises(
        BackupError,
        match="source contains more than 1 filesystem entries",
    ):
        snapshot_mod._stage_tree(
            source,
            stage,
            "media",
            limits=_limits(max_members=1),
        )

    assert entries_requested == 2


def test_create_snapshot_rejects_source_tree_beyond_depth_limit(tmp_path):
    limits = _limits(max_identity_depth=1)
    locations = _source_locations(tmp_path / "source", limits=limits)
    nested = locations.media_dir / "level-one" / "level-two"
    nested.mkdir(parents=True)
    (nested / "payload.bin").write_bytes(b"too deep")
    out_path = tmp_path / "backups" / snapshot_name(CREATED_AT)

    with pytest.raises(
        BackupError,
        match="1-level tree depth",
    ):
        create_snapshot(
            locations,
            passphrase=PASSPHRASE,
            out_path=out_path,
            created_at=CREATED_AT,
        )

    assert not out_path.exists()


@pytest.mark.parametrize("surface", ["read", "restore"])
def test_read_and_restore_reject_deep_tree_before_extraction_or_journal(
    tmp_path,
    monkeypatch,
    surface,
):
    source = _source_locations(
        tmp_path / "source",
        limits=_limits(max_identity_depth=4),
    )
    nested = source.media_dir / "level-one" / "level-two"
    nested.mkdir(parents=True)
    (nested / "payload.bin").write_bytes(b"deep snapshot payload")
    snapshot = _create_snapshot(source, tmp_path)
    limits = _limits(max_identity_depth=1)

    def unexpected_extract(*_args, **_kwargs):
        raise AssertionError("deep archive reached extraction")

    def unexpected_journal(*_args, **_kwargs):
        raise AssertionError("deep archive reached restore journaling")

    monkeypatch.setattr(
        snapshot_mod.tarfile.TarFile,
        "extractall",
        unexpected_extract,
    )
    monkeypatch.setattr(
        snapshot_mod,
        "write_restore_journal",
        unexpected_journal,
    )

    with pytest.raises(
        SnapshotIntegrityError,
        match="1-level identity depth",
    ):
        if surface == "read":
            read_manifest(
                snapshot,
                PASSPHRASE,
                limits=limits,
            )
        else:
            restore_snapshot(
                snapshot,
                passphrase=PASSPHRASE,
                locations=_restore_locations(tmp_path / "target", limits),
            )


def test_snapshot_tree_depth_limit_is_inclusive(tmp_path):
    limits = _limits(max_identity_depth=2)
    source = _source_locations(tmp_path / "source", limits=limits)
    nested = source.media_dir / "level-one"
    nested.mkdir()
    payload = nested / "payload.bin"
    payload.write_bytes(b"at the inclusive boundary")
    snapshot = _create_snapshot(source, tmp_path)

    manifest = read_manifest(
        snapshot,
        PASSPHRASE,
        limits=limits,
    )
    target_root = tmp_path / "target"
    restore_snapshot(
        snapshot,
        passphrase=PASSPHRASE,
        locations=_restore_locations(target_root, limits),
    )

    assert any(
        entry["path"] == "media/level-one/payload.bin"
        for entry in manifest["inventory"]
    )
    assert (
        target_root / "data" / "media" / "level-one" / "payload.bin"
    ).read_bytes() == b"at the inclusive boundary"


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor cleanup contract")
def test_copy_depth_backstop_removes_partial_stage_and_restore_journal(
    tmp_path,
    monkeypatch,
):
    source = _source_locations(
        tmp_path / "source",
        limits=_limits(max_identity_depth=4),
    )
    nested = source.media_dir / "level-one"
    nested.mkdir()
    (nested / "payload.bin").write_bytes(b"rejected by copy backstop")
    snapshot = _create_snapshot(source, tmp_path)
    limits = _limits(max_identity_depth=1)
    target_root = tmp_path / "target"
    target = _restore_locations(target_root, limits)

    monkeypatch.setattr(
        snapshot_mod,
        "_validate_archive_members",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(
        BackupError,
        match="1-level identity depth",
    ):
        restore_snapshot(
            snapshot,
            passphrase=PASSPHRASE,
            locations=target,
        )

    journal = snapshot_mod.restore_journal_path(target.restore_state_dir)
    assert not journal.exists()
    assert list(target_root.rglob("*.healthmes-restore-*.staged")) == []
    assert list(target_root.rglob("*.healthmes-restore-*.backup")) == []
    assert not (target_root / "data" / "healthmes.db").exists()
    assert not (target_root / "data" / "media").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor traversal contract")
def test_stage_tree_rejects_source_ancestor_replacement_before_root_open(
    tmp_path,
    monkeypatch,
):
    configured_parent = tmp_path / "configured-parent"
    source = configured_parent / "media"
    source.mkdir(parents=True)
    (source / "expected.txt").write_text("expected", encoding="utf-8")
    displaced_parent = tmp_path / "displaced-parent"
    external_parent = tmp_path / "external-parent"
    external_source = external_parent / "media"
    external_source.mkdir(parents=True)
    (external_source / "private.txt").write_text("must not export", encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    real_open = filesystem_mod.os.open
    swapped = False

    def replace_after_root_anchor_open(path, flags, *args, **kwargs):
        nonlocal swapped
        descriptor = real_open(path, flags, *args, **kwargs)
        if not swapped and os.fspath(path) == source.resolve().anchor:
            swapped = True
            configured_parent.rename(displaced_parent)
            configured_parent.symlink_to(
                external_parent,
                target_is_directory=True,
            )
        return descriptor

    monkeypatch.setattr(
        filesystem_mod.os,
        "open",
        replace_after_root_anchor_open,
    )

    with pytest.raises(
        BackupError,
        match="changed while it was being opened|could not stage snapshot source tree",
    ):
        snapshot_mod._stage_tree(
            source,
            stage,
            "media",
            limits=_limits(),
        )

    assert swapped is True
    assert not (stage / "media" / "private.txt").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor traversal contract")
def test_stage_tree_rejects_regular_source_ancestor_replacement_before_root_open(
    tmp_path,
    monkeypatch,
):
    configured_parent = tmp_path / "configured-parent"
    source = configured_parent / "media"
    source.mkdir(parents=True)
    (source / "expected.txt").write_text("expected", encoding="utf-8")
    displaced_parent = tmp_path / "displaced-parent"
    replacement_source = tmp_path / "replacement-parent" / "media"
    replacement_source.mkdir(parents=True)
    (replacement_source / "private.txt").write_text(
        "must not export",
        encoding="utf-8",
    )
    stage = tmp_path / "stage"
    stage.mkdir()
    real_open = filesystem_mod.os.open
    swapped = False

    def replace_after_root_anchor_open(path, flags, *args, **kwargs):
        nonlocal swapped
        descriptor = real_open(path, flags, *args, **kwargs)
        if not swapped and os.fspath(path) == source.resolve().anchor:
            swapped = True
            configured_parent.rename(displaced_parent)
            replacement_source.parent.rename(configured_parent)
        return descriptor

    monkeypatch.setattr(
        filesystem_mod.os,
        "open",
        replace_after_root_anchor_open,
    )

    with pytest.raises(
        BackupError,
        match="changed while it was being opened|could not stage snapshot source tree",
    ):
        snapshot_mod._stage_tree(
            source,
            stage,
            "media",
            limits=_limits(),
        )

    assert swapped is True
    assert not (stage / "media" / "private.txt").exists()


def test_bounded_tar_writer_never_writes_past_decrypted_limit(
    tmp_path,
    monkeypatch,
):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "payload.bin").write_bytes(bytes(range(256)) * 16)
    tracked_files = []

    class TrackedTemporaryFile(io.BytesIO):
        def __init__(self):
            super().__init__()
            self.max_size = 0
            self.was_closed = False

        def write(self, payload):
            written = super().write(payload)
            self.max_size = max(self.max_size, len(self.getbuffer()))
            return written

        def close(self):
            self.was_closed = True

    def temporary_file(*_args, **_kwargs):
        handle = TrackedTemporaryFile()
        tracked_files.append(handle)
        return handle

    monkeypatch.setattr(snapshot_mod.tempfile, "TemporaryFile", temporary_file)
    limit = 64

    with pytest.raises(
        BackupError,
        match=rf"compressed snapshot payload exceeds.*{limit}-byte limit",
    ):
        snapshot_mod._tar_gz_bytes(
            stage,
            limits=_limits(
                max_decrypted_bytes=limit,
                max_compression_ratio=10_000,
            ),
        )

    assert len(tracked_files) == 1
    assert tracked_files[0].max_size <= limit
    assert tracked_files[0].was_closed


def _read_restore_limit(
    sealed: SealedSnapshot,
    kind: str,
) -> tuple[SnapshotResourceLimits, type[BackupError], str]:
    if kind == "encrypted":
        return (
            _limits(max_encrypted_bytes=sealed.path.stat().st_size - 1),
            BackupError,
            "encrypted snapshot exceeds",
        )
    if kind == "decrypted":
        return (
            _limits(max_decrypted_bytes=len(sealed.plaintext) - 1),
            SnapshotIntegrityError,
            "decrypted snapshot exceeds",
        )
    if kind == "member_count":
        return (
            _limits(max_members=sealed.member_count - 1),
            SnapshotIntegrityError,
            "more than .* archive members",
        )
    if kind == "member_bytes":
        return (
            _limits(
                max_member_bytes=sealed.max_member_bytes - 1,
                max_expanded_bytes=sealed.expanded_bytes,
            ),
            SnapshotIntegrityError,
            "archive member .* exceeds",
        )
    if kind == "expanded":
        return (
            _limits(
                max_member_bytes=sealed.max_member_bytes,
                max_expanded_bytes=sealed.expanded_bytes - 1,
            ),
            SnapshotIntegrityError,
            "snapshot expands beyond",
        )
    if kind == "compression_ratio":
        return (
            _limits(
                max_compression_ratio=(sealed.compression_ratio + 1) / 2,
            ),
            SnapshotIntegrityError,
            "compression ratio exceeds",
        )
    raise AssertionError(f"unknown limit kind: {kind}")


@pytest.mark.parametrize("surface", ["read", "restore"])
@pytest.mark.parametrize(
    "kind",
    [
        "encrypted",
        "decrypted",
        "member_count",
        "member_bytes",
        "expanded",
        "compression_ratio",
    ],
)
def test_read_and_restore_enforce_snapshot_limits(
    sealed_snapshot,
    tmp_path,
    monkeypatch,
    surface,
    kind,
):
    limits, error_type, match = _read_restore_limit(sealed_snapshot, kind)
    monkeypatch.setattr(
        snapshot_mod.age_passphrase,
        "decrypt",
        lambda _ciphertext, _passphrase: sealed_snapshot.plaintext,
    )

    with pytest.raises(error_type, match=match):
        if surface == "read":
            read_manifest(
                sealed_snapshot.path,
                PASSPHRASE,
                limits=limits,
            )
        else:
            restore_snapshot(
                sealed_snapshot.path,
                passphrase=PASSPHRASE,
                locations=_restore_locations(tmp_path / kind, limits),
            )


@pytest.mark.parametrize("surface", ["read", "restore"])
def test_read_and_restore_enforce_free_space_reserve(
    sealed_snapshot,
    tmp_path,
    monkeypatch,
    surface,
):
    limits = _limits(min_free_bytes=1)
    monkeypatch.setattr(
        snapshot_mod.age_passphrase,
        "decrypt",
        lambda _ciphertext, _passphrase: sealed_snapshot.plaintext,
    )
    monkeypatch.setattr(
        snapshot_mod.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=0),
    )

    with pytest.raises(BackupError, match="insufficient disk space for snapshot"):
        if surface == "read":
            read_manifest(
                sealed_snapshot.path,
                PASSPHRASE,
                limits=limits,
            )
        else:
            restore_snapshot(
                sealed_snapshot.path,
                passphrase=PASSPHRASE,
                locations=_restore_locations(tmp_path / surface, limits),
            )


def _remote_vault(
    tmp_path: Path,
    *,
    max_encrypted_bytes: int,
    min_free_bytes: int = 0,
) -> RemoteVaultProvider:
    local = LocalDirectoryProvider(
        tmp_path / "backups",
        locations=DataLocations(
            database_url=f"sqlite:///{tmp_path / 'unused.db'}",
            resource_limits=_limits(
                max_encrypted_bytes=max_encrypted_bytes,
                min_free_bytes=min_free_bytes,
            ),
        ),
        passphrase=PASSPHRASE,
    )
    return RemoteVaultProvider(
        VaultConfig(
            bucket="resource-limit-test",
            access_key_id="testing",
            secret_access_key="testing",
            region="us-east-1",
        ),
        local=local,
    )


def test_remote_vault_rejects_declared_oversize_and_closes_body(tmp_path):
    limit = 8
    name = snapshot_name(CREATED_AT)
    body = TrackedBody(b"unused")
    vault = _remote_vault(tmp_path, max_encrypted_bytes=limit)
    vault._s3 = ObjectClient(
        {
            "Body": body,
            "ContentLength": limit + 1,
            "Metadata": {},
            "ETag": "",
        }
    )

    with pytest.raises(BackupError, match="vault snapshot exceeds.*8-byte encrypted limit"):
        vault.download(name)

    destination = vault.local.backup_dir / name
    assert body.closed
    assert not destination.exists()
    assert not destination.with_name(destination.name + ".part").exists()


def test_remote_vault_streamed_oversize_removes_partial_and_closes_body(tmp_path):
    limit = 8
    name = snapshot_name(CREATED_AT)
    body = TrackedBody(b"x" * (limit + 1))
    vault = _remote_vault(tmp_path, max_encrypted_bytes=limit)
    vault._s3 = ObjectClient(
        {
            "Body": body,
            "ContentLength": limit,
            "Metadata": {"healthmes-sha256": "0" * 64},
            "ETag": "",
        }
    )

    with pytest.raises(BackupError, match="vault snapshot stream exceeds.*8-byte encrypted limit"):
        vault.download(name)

    destination = vault.local.backup_dir / name
    assert not destination.exists()
    assert not destination.with_name(destination.name + ".part").exists()
    assert body.closed


def test_remote_vault_download_preserves_configured_free_space_reserve(
    tmp_path,
    monkeypatch,
):
    name = snapshot_name(CREATED_AT)
    body = TrackedBody(b"small")
    vault = _remote_vault(
        tmp_path,
        max_encrypted_bytes=8,
        min_free_bytes=1,
    )
    vault._s3 = ObjectClient(
        {
            "Body": body,
            "ContentLength": 5,
            "Metadata": {
                "healthmes-sha256": hashlib.sha256(b"small").hexdigest()
            },
            "ETag": "",
        }
    )
    monkeypatch.setattr(
        snapshot_mod.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=5),
    )

    with pytest.raises(BackupError, match="insufficient disk space.*including reserve"):
        vault.download(name)

    destination = vault.local.backup_dir / name
    assert not destination.exists()
    assert not destination.with_name(destination.name + ".part").exists()
    assert body.closed


@pytest.mark.parametrize(
    ("response", "match"),
    (
        ("not-a-mapping", "invalid response"),
        (
            {
                "Body": TrackedBody(b""),
                "ContentLength": "not-an-integer",
                "Metadata": {},
                "ETag": "",
            },
            "invalid ContentLength",
        ),
        (
            {
                "Body": TrackedBody(b""),
                "ContentLength": 0,
                "Metadata": [],
                "ETag": "",
            },
            "invalid Metadata",
        ),
        (
            {
                "ContentLength": 0,
                "Metadata": {},
                "ETag": "",
            },
            "invalid Body",
        ),
    ),
)
def test_remote_vault_malformed_download_response_uses_backup_error(
    tmp_path,
    response,
    match,
):
    name = snapshot_name(CREATED_AT)
    vault = _remote_vault(tmp_path, max_encrypted_bytes=8)
    vault._s3 = ObjectClient(response)

    with pytest.raises(BackupError, match=match):
        vault.download(name)

    destination = vault.local.backup_dir / name
    assert not destination.exists()
    assert not destination.with_name(destination.name + ".part").exists()


def test_remote_vault_stream_failure_uses_backup_error_and_closes_body(
    tmp_path,
):
    name = snapshot_name(CREATED_AT)
    body = FailingBody()
    vault = _remote_vault(tmp_path, max_encrypted_bytes=8)
    vault._s3 = ObjectClient(
        {
            "Body": body,
            "ContentLength": 1,
            "Metadata": {"healthmes-sha256": "0" * 64},
            "ETag": "",
        }
    )

    with pytest.raises(
        BackupError,
        match="vault download snapshot failed.*provider status",
    ) as raised:
        vault.download(name)

    assert "stream failure" not in str(raised.value)
    destination = vault.local.backup_dir / name
    assert not destination.exists()
    assert not destination.with_name(destination.name + ".part").exists()
    assert body.closed


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires Windows privileges")
def test_remote_vault_download_does_not_follow_predictable_part_symlink(tmp_path):
    name = snapshot_name(CREATED_AT)
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"must survive")
    vault = _remote_vault(tmp_path, max_encrypted_bytes=32)
    destination = vault.local.backup_dir / name
    destination.parent.mkdir(parents=True)
    destination.with_name(destination.name + ".part").symlink_to(victim)
    payload = b"downloaded"
    vault._s3 = ObjectClient(
        {
            "Body": TrackedBody(payload),
            "ContentLength": len(payload),
            "Metadata": {"healthmes-sha256": hashlib.sha256(payload).hexdigest()},
            "ETag": "",
        }
    )

    vault.download(name)

    assert victim.read_bytes() == b"must survive"
    assert destination.read_bytes() == payload
    assert destination.with_name(destination.name + ".part").is_symlink()


def test_remote_vault_truncated_body_is_never_published(tmp_path):
    name = snapshot_name(CREATED_AT)
    payload = b"short"
    vault = _remote_vault(tmp_path, max_encrypted_bytes=32)
    vault._s3 = ObjectClient(
        {
            "Body": TrackedBody(payload),
            "ContentLength": len(payload) + 1,
            "Metadata": {"healthmes-sha256": hashlib.sha256(payload).hexdigest()},
            "ETag": "",
        }
    )

    with pytest.raises(BackupError, match="stream length does not match"):
        vault.download(name)

    destination = vault.local.backup_dir / name
    assert not destination.exists()
    assert not destination.with_name(destination.name + ".part").exists()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires Windows privileges")
def test_remote_vault_destination_race_does_not_overwrite_existing_entry(tmp_path):
    name = snapshot_name(CREATED_AT)
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"must survive")
    vault = _remote_vault(tmp_path, max_encrypted_bytes=32)
    destination = vault.local.backup_dir / name
    payload = b"downloaded"
    body = CallbackBody(
        payload,
        lambda: destination.symlink_to(victim),
    )
    vault._s3 = ObjectClient(
        {
            "Body": body,
            "ContentLength": len(payload),
            "Metadata": {"healthmes-sha256": hashlib.sha256(payload).hexdigest()},
            "ETag": "",
        }
    )

    with pytest.raises(BackupError, match="destination appeared during download"):
        vault.download(name, overwrite=False)

    assert victim.read_bytes() == b"must survive"
    assert destination.is_symlink()
    assert not destination.with_name(destination.name + ".part").exists()
