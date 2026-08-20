"""Adversarial manifest/archive and recoverable local restore tests."""

import copy
import io
import json
import os
import shutil
import sqlite3
import tarfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread

import pytest
from pyrage import passphrase as age_passphrase

from healthmes.activity.locking import (
    activity_write_lock,
    sqlite_runtime_guard,
)
from healthmes.backup import snapshot as snapshot_mod
from healthmes.backup.provider import BackupError, SnapshotIntegrityError
from healthmes.backup.snapshot import (
    MANIFEST_ARCNAME,
    create_snapshot,
    read_manifest,
    recover_incomplete_restore,
    restore_snapshot,
    snapshot_name,
)

from .test_snapshot import CREATED_AT


def _rewrite_snapshot(
    source: Path,
    destination: Path,
    secret: str,
    *,
    mutate_manifest=None,
    mutate_members=None,
) -> Path:
    plaintext = age_passphrase.decrypt(source.read_bytes(), secret)
    entries: list[tuple[tarfile.TarInfo, bytes | None]] = []
    with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:gz") as archive:
        for member in archive.getmembers():
            cloned = copy.copy(member)
            extracted = archive.extractfile(member) if member.isfile() else None
            entries.append((cloned, extracted.read() if extracted else None))

    for index, (member, payload) in enumerate(entries):
        if member.name != MANIFEST_ARCNAME:
            continue
        manifest = json.loads((payload or b"").decode("utf-8"))
        if mutate_manifest is not None:
            mutate_manifest(manifest)
        encoded = (json.dumps(manifest, sort_keys=True) + "\n").encode()
        member.size = len(encoded)
        entries[index] = (member, encoded)
        break
    if mutate_members is not None:
        mutate_members(entries)

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for member, payload in entries:
            archive.addfile(
                member,
                io.BytesIO(payload) if payload is not None else None,
            )
    destination.write_bytes(age_passphrase.encrypt(buffer.getvalue(), secret))
    return destination


def _make_snapshot(source_env, tmp_path: Path) -> Path:
    out = tmp_path / snapshot_name(CREATED_AT)
    create_snapshot(
        source_env.locations,
        passphrase=source_env.passphrase,
        out_path=out,
        created_at=CREATED_AT,
    )
    return out


def _assert_rejected_without_mutation(
    path: Path,
    *,
    source_env,
    fresh_locations,
    match: str,
) -> None:
    target, target_root = fresh_locations("malicious-target")
    target_db = target_root / "data" / "healthmes.db"
    target_db.parent.mkdir(parents=True)
    target_db.write_bytes(source_env.db_path.read_bytes())
    connection = sqlite3.connect(target_db)
    try:
        connection.execute(
            "INSERT INTO task (title, status) VALUES (?, ?)",
            ("must survive rejected restore", "live"),
        )
        connection.commit()
    finally:
        connection.close()
    marker = target_root / "data" / "media" / "live-only.txt"
    marker.parent.mkdir(parents=True)
    marker.write_text("live", encoding="utf-8")

    with pytest.raises(SnapshotIntegrityError, match=match):
        restore_snapshot(
            path,
            passphrase=source_env.passphrase,
            locations=target,
        )

    connection = sqlite3.connect(target_db)
    try:
        titles = {row[0] for row in connection.execute("SELECT title FROM task")}
    finally:
        connection.close()
    assert "must survive rejected restore" in titles
    assert marker.read_text(encoding="utf-8") == "live"


@pytest.mark.parametrize(
    ("bad_path", "match"),
    [
        ("../escape", "unsafe path segment"),
        ("/absolute", "unsafe path segment"),
        ("media//note.txt", "unsafe path segment"),
        ("media/./note.txt", "unsafe path segment"),
    ],
)
def test_inventory_paths_are_strictly_normalized_before_restore(
    source_env,
    fresh_locations,
    tmp_path,
    bad_path,
    match,
):
    original = _make_snapshot(source_env, tmp_path / "original")

    def mutate(manifest):
        manifest["inventory"][0]["path"] = bad_path

    malicious = _rewrite_snapshot(
        original,
        tmp_path / f"inventory-{len(bad_path)}.age",
        source_env.passphrase,
        mutate_manifest=mutate,
    )
    _assert_rejected_without_mutation(
        malicious,
        source_env=source_env,
        fresh_locations=fresh_locations,
        match=match,
    )


@pytest.mark.parametrize(
    ("component", "field", "value", "match"),
    [
        ("healthmes_db", "arcname", "../healthmes.db", "unsafe path segment"),
        ("healthmes_db", "arcname", "/healthmes.db", "unsafe path segment"),
        ("media", "arcroot", "../media", "unsafe path segment"),
        ("media", "arcroot", "hermes", "arcroot must be"),
    ],
)
def test_component_locations_cannot_escape_or_overlap_roots(
    source_env,
    fresh_locations,
    tmp_path,
    component,
    field,
    value,
    match,
):
    original = _make_snapshot(source_env, tmp_path / "original")

    def mutate(manifest):
        manifest["contents"][component][field] = value

    malicious = _rewrite_snapshot(
        original,
        tmp_path / f"component-{component}-{field}.age",
        source_env.passphrase,
        mutate_manifest=mutate,
    )
    _assert_rejected_without_mutation(
        malicious,
        source_env=source_env,
        fresh_locations=fresh_locations,
        match=match,
    )


def test_duplicate_inventory_path_is_rejected(
    source_env,
    fresh_locations,
    tmp_path,
):
    original = _make_snapshot(source_env, tmp_path / "original")

    def mutate(manifest):
        manifest["inventory"].append(copy.deepcopy(manifest["inventory"][0]))

    malicious = _rewrite_snapshot(
        original,
        tmp_path / "duplicate-inventory.age",
        source_env.passphrase,
        mutate_manifest=mutate,
    )
    _assert_rejected_without_mutation(
        malicious,
        source_env=source_env,
        fresh_locations=fresh_locations,
        match="duplicate inventory path",
    )


def test_symlink_escape_is_rejected_before_extraction(
    source_env,
    fresh_locations,
    tmp_path,
):
    original = _make_snapshot(source_env, tmp_path / "original")

    def mutate_manifest(manifest):
        link = next(entry for entry in manifest["inventory"] if entry["kind"] == "symlink")
        link["target"] = "../../outside"

    def mutate_members(entries):
        member, _payload = next(
            (member, payload)
            for member, payload in entries
            if member.name == "hermes/memory/current.json"
        )
        member.linkname = "../../outside"

    malicious = _rewrite_snapshot(
        original,
        tmp_path / "symlink-escape.age",
        source_env.passphrase,
        mutate_manifest=mutate_manifest,
        mutate_members=mutate_members,
    )
    _assert_rejected_without_mutation(
        malicious,
        source_env=source_env,
        fresh_locations=fresh_locations,
        match="escapes component root",
    )


def test_safe_parent_symlink_target_remains_backward_compatible(
    source_env,
    tmp_path,
):
    original = _make_snapshot(source_env, tmp_path / "original")

    def mutate_manifest(manifest):
        link = next(entry for entry in manifest["inventory"] if entry["kind"] == "symlink")
        link["target"] = "../config.yaml"

    def mutate_members(entries):
        member, _payload = next(
            (member, payload)
            for member, payload in entries
            if member.name == "hermes/memory/current.json"
        )
        member.linkname = "../config.yaml"

    compatible = _rewrite_snapshot(
        original,
        tmp_path / "safe-parent-link.age",
        source_env.passphrase,
        mutate_manifest=mutate_manifest,
        mutate_members=mutate_members,
    )

    manifest = read_manifest(compatible, source_env.passphrase)

    link = next(entry for entry in manifest["inventory"] if entry["kind"] == "symlink")
    assert link["target"] == "../config.yaml"


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symlinks")
def test_schema_v1_ancestor_directory_symlink_restores(
    source_env,
    fresh_locations,
    tmp_path,
):
    subdirectory = source_env.media_dir / "sub"
    subdirectory.mkdir()
    os.symlink("..", subdirectory / "back", target_is_directory=True)
    original = _make_snapshot(source_env, tmp_path / "original")

    compatible = _rewrite_snapshot(
        original,
        tmp_path / "schema-v1-ancestor-link.age",
        source_env.passphrase,
        mutate_manifest=lambda manifest: manifest.__setitem__(
            "schema_version",
            1,
        ),
    )
    locations, _target_root = fresh_locations(
        "schema-v1-ancestor-link"
    )

    restore_snapshot(
        compatible,
        passphrase=source_env.passphrase,
        locations=locations,
    )

    restored = locations.media_dir / "sub" / "back"
    assert restored.is_symlink()
    assert os.readlink(restored) == ".."
    assert (restored / "note.txt").read_text(
        encoding="utf-8"
    ) == "voice memo transcript\n"


def test_descendant_symlink_loop_is_rejected(
    source_env,
    tmp_path,
):
    original = _make_snapshot(source_env, tmp_path / "original")

    def mutate_manifest(manifest):
        link = next(
            entry
            for entry in manifest["inventory"]
            if entry["kind"] == "symlink"
        )
        link["target"] = "current.json/child"

    def mutate_members(entries):
        member, _payload = next(
            (member, payload)
            for member, payload in entries
            if member.name == "hermes/memory/current.json"
        )
        member.linkname = "current.json/child"

    malicious = _rewrite_snapshot(
        original,
        tmp_path / "descendant-link-loop.age",
        source_env.passphrase,
        mutate_manifest=mutate_manifest,
        mutate_members=mutate_members,
    )

    with pytest.raises(
        SnapshotIntegrityError,
        match="loops within component",
    ):
        read_manifest(malicious, source_env.passphrase)


def test_duplicate_archive_member_is_rejected(
    source_env,
    fresh_locations,
    tmp_path,
):
    original = _make_snapshot(source_env, tmp_path / "original")

    def mutate(entries):
        member, payload = next(
            (member, payload) for member, payload in entries if member.name == "media/note.txt"
        )
        entries.append((copy.copy(member), payload))

    malicious = _rewrite_snapshot(
        original,
        tmp_path / "duplicate-member.age",
        source_env.passphrase,
        mutate_members=mutate,
    )
    _assert_rejected_without_mutation(
        malicious,
        source_env=source_env,
        fresh_locations=fresh_locations,
        match="duplicate archive member",
    )


def test_undeclared_archive_member_is_rejected(
    source_env,
    fresh_locations,
    tmp_path,
):
    original = _make_snapshot(source_env, tmp_path / "original")

    def mutate(entries):
        payload = b"not declared"
        member = tarfile.TarInfo("media/rogue.bin")
        member.mode = 0o600
        member.size = len(payload)
        entries.append((member, payload))

    malicious = _rewrite_snapshot(
        original,
        tmp_path / "undeclared-member.age",
        source_env.passphrase,
        mutate_members=mutate,
    )
    _assert_rejected_without_mutation(
        malicious,
        source_env=source_env,
        fresh_locations=fresh_locations,
        match="undeclared entry",
    )


def test_archive_member_kind_must_match_inventory(
    source_env,
    fresh_locations,
    tmp_path,
):
    original = _make_snapshot(source_env, tmp_path / "original")

    def mutate(entries):
        for index, (member, _payload) in enumerate(entries):
            if member.name != "media/note.txt":
                continue
            member.type = tarfile.SYMTYPE
            member.linkname = "archive"
            member.size = 0
            entries[index] = (member, None)
            return
        raise AssertionError("media/note.txt not found")

    malicious = _rewrite_snapshot(
        original,
        tmp_path / "wrong-kind.age",
        source_env.passphrase,
        mutate_members=mutate,
    )
    _assert_rejected_without_mutation(
        malicious,
        source_env=source_env,
        fresh_locations=fresh_locations,
        match="kind contradicts inventory",
    )


def test_archive_member_size_must_match_inventory_before_extraction(
    source_env,
    fresh_locations,
    tmp_path,
):
    original = _make_snapshot(source_env, tmp_path / "original")

    def mutate(entries):
        for index, (member, _payload) in enumerate(entries):
            if member.name != "media/note.txt":
                continue
            payload = b"x" * (member.size + 4096)
            member.size = len(payload)
            entries[index] = (member, payload)
            return
        raise AssertionError("media/note.txt not found")

    malicious = _rewrite_snapshot(
        original,
        tmp_path / "wrong-size.age",
        source_env.passphrase,
        mutate_members=mutate,
    )
    _assert_rejected_without_mutation(
        malicious,
        source_env=source_env,
        fresh_locations=fresh_locations,
        match="size contradicts inventory",
    )


def test_recovery_metadata_cannot_claim_full_node_recovery(
    source_env,
    fresh_locations,
    tmp_path,
):
    original = _make_snapshot(source_env, tmp_path / "original")

    def mutate(manifest):
        manifest["recovery"]["full_node_recovery"] = True

    malicious = _rewrite_snapshot(
        original,
        tmp_path / "false-recovery-claim.age",
        source_env.passphrase,
        mutate_manifest=mutate,
    )
    _assert_rejected_without_mutation(
        malicious,
        source_env=source_env,
        fresh_locations=fresh_locations,
        match="must not claim full-node recovery",
    )


@pytest.mark.parametrize("target_kind", ["media", "database"])
def test_snapshot_source_cannot_be_inside_a_replaced_target(
    source_env,
    fresh_locations,
    tmp_path,
    target_kind,
):
    original = _make_snapshot(source_env, tmp_path / "original")
    target, target_root = fresh_locations(f"source-overlap-{target_kind}")
    if target_kind == "media":
        target.media_dir.mkdir(parents=True)
        overlapping = target.media_dir / original.name
    else:
        overlapping = target_root / "data" / original.name
        overlapping.parent.mkdir(parents=True)
        target = replace(
            target,
            database_url=f"sqlite:///{overlapping}",
        )
    shutil.copy2(original, overlapping)

    with pytest.raises(
        BackupError,
        match="snapshot source is inside a restore target",
    ):
        restore_snapshot(
            overlapping,
            passphrase=source_env.passphrase,
            locations=target,
        )

    assert overlapping.is_file()


def test_snapshot_source_cannot_be_at_sqlite_sidecar_path(
    source_env,
    fresh_locations,
    tmp_path,
):
    original = _make_snapshot(source_env, tmp_path / "original")
    target, target_root = fresh_locations("source-overlap-wal")
    sidecar = target_root / "data" / "healthmes.db-wal"
    sidecar.parent.mkdir(parents=True)
    shutil.copy2(original, sidecar)

    with pytest.raises(
        BackupError,
        match="snapshot source is inside a restore target",
    ):
        restore_snapshot(
            sidecar,
            passphrase=source_env.passphrase,
            locations=target,
        )

    assert sidecar.is_file()


def test_restore_rejects_tree_target_that_is_runtime_lock_path(
    source_env,
    fresh_locations,
    tmp_path,
):
    snapshot = _make_snapshot(source_env, tmp_path / "original")
    target, target_root = fresh_locations("runtime-lock-target")
    database = target_root / "data" / "healthmes.db"
    target = replace(
        target,
        media_dir=database.with_name(f"{database.name}.runtime.lock"),
    )

    with pytest.raises(BackupError, match="SQLite runtime lock"):
        restore_snapshot(
            snapshot,
            passphrase=source_env.passphrase,
            locations=target,
        )

    assert not target.media_dir.exists()


def test_restore_state_directory_cannot_be_inside_replaced_tree(
    source_env,
    fresh_locations,
    tmp_path,
):
    snapshot = _make_snapshot(source_env, tmp_path / "original")
    target, _target_root = fresh_locations("state-inside-tree")
    target = replace(
        target,
        restore_state_dir=target.media_dir / ".restore",
    )

    with pytest.raises(BackupError, match="restore state directory"):
        restore_snapshot(
            snapshot,
            passphrase=source_env.passphrase,
            locations=target,
        )

    assert not target.restore_state_dir.exists()


def test_restore_targets_cannot_claim_sqlite_sidecar_path(
    source_env,
    fresh_locations,
    tmp_path,
):
    snapshot = _make_snapshot(source_env, tmp_path / "original")
    target, target_root = fresh_locations("target-sidecar")
    database = target_root / "data" / "healthmes.db"
    target = replace(
        target,
        media_dir=Path(f"{database}-wal"),
    )

    with pytest.raises(BackupError, match="restore targets overlap"):
        restore_snapshot(
            snapshot,
            passphrase=source_env.passphrase,
            locations=target,
        )

    assert not target.media_dir.exists()


@pytest.mark.parametrize("component", ["healthmes_db", "media"])
def test_post_rename_fsync_failure_rolls_back_absent_target(
    source_env,
    fresh_locations,
    tmp_path,
    monkeypatch,
    component,
):
    snapshot = _make_snapshot(source_env, tmp_path / "original")
    target, target_root = fresh_locations(f"post-rename-fsync-{component}")
    destination = (
        target_root / "data" / "healthmes.db"
        if component == "healthmes_db"
        else target.media_dir
    )
    assert not destination.exists()
    real_fsync = snapshot_mod._fsync_operation_entry
    failed = False

    def fail_after_rename(operation, path, *, is_tree):
        nonlocal failed
        if (
            not failed
            and path == destination
            and snapshot_mod._anchored_metadata(operation, path) is not None
        ):
            failed = True
            raise OSError("injected post-rename fsync failure")
        real_fsync(operation, path, is_tree=is_tree)

    monkeypatch.setattr(
        snapshot_mod,
        "_fsync_operation_entry",
        fail_after_rename,
    )
    with pytest.raises(BackupError, match="post-rename fsync failure"):
        restore_snapshot(
            snapshot,
            passphrase=source_env.passphrase,
            locations=target,
        )

    assert failed is True
    assert not destination.exists()


def test_restore_preflight_requires_readable_target_parent(
    tmp_path,
    monkeypatch,
):
    destination = tmp_path / "target" / "healthmes.db"
    destination.parent.mkdir()
    source = tmp_path / "source.db"
    source.write_bytes(b"source")
    observed_modes: list[int] = []

    def deny_access(_path, mode):
        observed_modes.append(mode)
        return False

    monkeypatch.setattr(snapshot_mod.os, "access", deny_access)
    request = snapshot_mod._LocalRestoreRequest(
        component="healthmes_db",
        source=source,
        destination=destination,
        is_tree=False,
    )

    with pytest.raises(BackupError, match="parent is not accessible"):
        snapshot_mod._preflight_local_target(
            request,
            transaction_id="permission-check",
        )

    assert observed_modes
    assert observed_modes[-1] & os.R_OK


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symlinks")
def test_parent_symlink_swap_after_staging_never_redirects_restore(
    source_env,
    fresh_locations,
    tmp_path,
    monkeypatch,
):
    snapshot = _make_snapshot(source_env, tmp_path / "original")
    target, target_root = fresh_locations("parent-swap-target")
    target = replace(
        target,
        restore_state_dir=tmp_path / "parent-swap-restore-state",
    )
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    displaced_parent = target_root / "original-data-parent"
    real_runtime_guard = snapshot_mod._sqlite_restore_runtime_guard
    swapped = False

    @contextmanager
    def swap_parent_before_runtime_guard(database_url):
        nonlocal swapped
        if not swapped:
            data_parent = target_root / "data"
            data_parent.rename(displaced_parent)
            data_parent.symlink_to(attacker, target_is_directory=True)
            swapped = True
        with real_runtime_guard(database_url):
            yield

    monkeypatch.setattr(
        snapshot_mod,
        "_sqlite_restore_runtime_guard",
        swap_parent_before_runtime_guard,
    )

    with pytest.raises(BackupError, match="parent path identity changed"):
        restore_snapshot(
            snapshot,
            passphrase=source_env.passphrase,
            locations=target,
        )

    assert swapped is True
    assert (target_root / "data").is_symlink()
    assert list(attacker.iterdir()) == []
    assert list(displaced_parent.glob(".*.healthmes-restore-*.staged"))


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symlinks")
def test_open_restore_parent_accepts_canonicalized_symlink_ancestor(
    tmp_path,
) -> None:
    real = tmp_path / "real"
    target = real / "nested"
    target.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    anchor = snapshot_mod._open_restore_parent(
        alias / "nested",
        create=False,
    )
    try:
        assert anchor.path == target.resolve()
        assert snapshot_mod._DirectoryIdentity.from_metadata(
            os.fstat(anchor.descriptor)
        ) == snapshot_mod._DirectoryIdentity.from_metadata(target.stat())
    finally:
        anchor.close()


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symlinks")
def test_restore_parent_alias_swap_after_canonicalization_cannot_redirect(
    tmp_path,
    monkeypatch,
) -> None:
    real = tmp_path / "real"
    target = real / "nested"
    target.mkdir(parents=True)
    attacker = tmp_path / "attacker"
    (attacker / "nested").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    snapshot_mod._require_fd_relative_restore_support()
    original_open = snapshot_mod.os.open
    swapped = False

    def swap_alias_then_open(*args, **kwargs):
        nonlocal swapped
        if not swapped:
            alias.unlink()
            alias.symlink_to(attacker, target_is_directory=True)
            swapped = True
        return original_open(*args, **kwargs)

    monkeypatch.setattr(snapshot_mod.os, "open", swap_alias_then_open)
    monkeypatch.setattr(
        snapshot_mod,
        "_require_fd_relative_restore_support",
        lambda: None,
    )
    anchor = snapshot_mod._open_restore_parent(
        alias / "nested",
        create=False,
    )
    try:
        descriptor = original_open(
            "written-through-anchor",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=anchor.descriptor,
        )
        os.close(descriptor)
        assert anchor.path == target.resolve()
    finally:
        anchor.close()

    assert swapped is True
    assert (target / "written-through-anchor").is_file()
    assert not (attacker / "nested" / "written-through-anchor").exists()


@pytest.mark.skipif(
    os.name == "nt" or not Path("/var").is_symlink(),
    reason="requires the macOS-style /var filesystem alias",
)
def test_open_restore_parent_accepts_macos_var_alias() -> None:
    anchor = snapshot_mod._open_restore_parent(
        Path("/var/folders"),
        create=False,
    )
    try:
        assert anchor.path == Path("/var/folders").resolve()
        assert anchor.identity.matches(os.fstat(anchor.descriptor))
    finally:
        anchor.close()


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symlinks")
def test_default_restore_state_never_redirects_journal_after_parent_swap(
    source_env,
    fresh_locations,
    tmp_path,
    monkeypatch,
):
    snapshot = _make_snapshot(source_env, tmp_path / "default-state-parent-swap")
    target, target_root = fresh_locations("default-state-parent-swap-target")
    data_parent = target_root / "data"
    data_parent.mkdir()
    target_db = data_parent / "healthmes.db"
    connection = sqlite3.connect(target_db)
    try:
        connection.execute("CREATE TABLE canary (value TEXT NOT NULL)")
        connection.execute("INSERT INTO canary VALUES ('original')")
        connection.commit()
    finally:
        connection.close()

    attacker = tmp_path / "default-state-attacker"
    attacker.mkdir()
    displaced_parent = target_root / "original-data-parent"
    real_runtime_guard = snapshot_mod._sqlite_restore_runtime_guard
    swapped = False

    @contextmanager
    def swap_parent_before_runtime_guard(database_url):
        nonlocal swapped
        if not swapped:
            data_parent.rename(displaced_parent)
            data_parent.symlink_to(attacker, target_is_directory=True)
            swapped = True
        with real_runtime_guard(database_url):
            yield

    monkeypatch.setattr(
        snapshot_mod,
        "_sqlite_restore_runtime_guard",
        swap_parent_before_runtime_guard,
    )

    with pytest.raises(BackupError, match="parent path identity changed"):
        restore_snapshot(
            snapshot,
            passphrase=source_env.passphrase,
            locations=target,
        )

    assert swapped is True
    assert data_parent.is_symlink()
    assert list(attacker.iterdir()) == []
    restored_connection = sqlite3.connect(displaced_parent / "healthmes.db")
    try:
        assert restored_connection.execute("SELECT value FROM canary").fetchone() == (
            "original",
        )
    finally:
        restored_connection.close()


@pytest.mark.parametrize(
    ("missing_component", "setting_name"),
    [
        ("media", "media_dir"),
        ("raw_ingest", "raw_ingest_dir"),
        ("hermes_home", "HERMES_HOME"),
    ],
)
def test_missing_included_local_target_fails_before_database_mutation(
    source_env,
    fresh_locations,
    tmp_path,
    missing_component,
    setting_name,
):
    raw_dir = source_env.data_dir / "raw_ingest"
    raw_dir.mkdir(parents=True)
    (raw_dir / "sample.bin").write_bytes(b"raw")
    source_locations = replace(
        source_env.locations,
        raw_ingest_dir=raw_dir,
    )
    snapshot = tmp_path / "included-targets.age"
    create_snapshot(
        source_locations,
        passphrase=source_env.passphrase,
        out_path=snapshot,
        created_at=CREATED_AT,
    )

    target, target_root = fresh_locations(f"missing-{missing_component}")
    target = replace(
        target,
        raw_ingest_dir=target_root / "data" / "raw_ingest",
    )
    target_field = {
        "media": "media_dir",
        "raw_ingest": "raw_ingest_dir",
        "hermes_home": "hermes_home",
    }[missing_component]
    target = replace(target, **{target_field: None})
    target_db = target_root / "data" / "healthmes.db"
    target_db.parent.mkdir(parents=True, exist_ok=True)
    target_db.write_bytes(source_env.db_path.read_bytes())
    connection = sqlite3.connect(target_db)
    try:
        connection.execute(
            "INSERT INTO task (title, status) VALUES (?, ?)",
            ("live generation", "live"),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(BackupError, match=setting_name):
        restore_snapshot(
            snapshot,
            passphrase=source_env.passphrase,
            locations=target,
        )

    connection = sqlite3.connect(target_db)
    try:
        titles = {row[0] for row in connection.execute("SELECT title FROM task")}
    finally:
        connection.close()
    assert "live generation" in titles


def test_later_local_failure_rolls_back_database_and_earlier_trees(
    source_env,
    fresh_locations,
    tmp_path,
    monkeypatch,
):
    snapshot = _make_snapshot(source_env, tmp_path / "original")
    target, target_root = fresh_locations("rollback-target")
    target_db = target_root / "data" / "healthmes.db"
    target_db.parent.mkdir(parents=True)
    target_db.write_bytes(source_env.db_path.read_bytes())
    connection = sqlite3.connect(target_db)
    try:
        connection.execute(
            "INSERT INTO task (title, status) VALUES (?, ?)",
            ("live generation", "live"),
        )
        connection.commit()
    finally:
        connection.close()
    target.media_dir.mkdir(parents=True)
    live_media = target.media_dir / "live-only.bin"
    live_media.write_bytes(b"live media")

    real_apply = snapshot_mod._apply_swap

    def fail_on_hermes(operation):
        if operation.component == "hermes_home":
            raise OSError("injected later swap failure")
        real_apply(operation)

    monkeypatch.setattr(snapshot_mod, "_apply_swap", fail_on_hermes)
    with pytest.raises(BackupError, match="injected later swap failure"):
        restore_snapshot(
            snapshot,
            passphrase=source_env.passphrase,
            locations=target,
        )

    connection = sqlite3.connect(target_db)
    try:
        titles = {row[0] for row in connection.execute("SELECT title FROM task")}
    finally:
        connection.close()
    assert "live generation" in titles
    assert live_media.read_bytes() == b"live media"


def test_journal_persistence_failure_after_local_mutation_still_rolls_back(
    source_env,
    fresh_locations,
    tmp_path,
    monkeypatch,
):
    snapshot = _make_snapshot(source_env, tmp_path / "original")
    target, target_root = fresh_locations("journal-failure-rollback-target")
    target_db = target_root / "data" / "healthmes.db"
    target_db.parent.mkdir(parents=True)
    target_db.write_bytes(source_env.db_path.read_bytes())
    connection = sqlite3.connect(target_db)
    try:
        connection.execute(
            "INSERT INTO task (title, status) VALUES (?, ?)",
            ("live generation", "live"),
        )
        connection.commit()
    finally:
        connection.close()

    real_apply = snapshot_mod._apply_swap
    real_write = snapshot_mod.write_restore_journal
    local_mutated = False

    def mark_first_mutation(operation):
        nonlocal local_mutated
        real_apply(operation)
        if (
            operation.component == "healthmes_db"
            and operation.staged is not None
        ):
            local_mutated = True

    def fail_after_mutation(path, journal):
        if local_mutated:
            raise BackupError("injected journal persistence failure")
        real_write(path, journal)

    monkeypatch.setattr(snapshot_mod, "_apply_swap", mark_first_mutation)
    monkeypatch.setattr(
        snapshot_mod,
        "write_restore_journal",
        fail_after_mutation,
    )

    with pytest.raises(BackupError) as excinfo:
        restore_snapshot(
            snapshot,
            passphrase=source_env.passphrase,
            locations=target,
        )

    message = str(excinfo.value)
    assert "injected journal persistence failure" in message
    assert "local rollback completed" in message
    assert "local rollback was incomplete" not in message
    connection = sqlite3.connect(target_db)
    try:
        titles = {row[0] for row in connection.execute("SELECT title FROM task")}
    finally:
        connection.close()
    assert "live generation" in titles


def test_journal_and_rollback_failures_are_combined_deterministically(
    source_env,
    fresh_locations,
    tmp_path,
    monkeypatch,
):
    snapshot = _make_snapshot(source_env, tmp_path / "original")
    target, target_root = fresh_locations("compound-journal-rollback-target")
    target_db = target_root / "data" / "healthmes.db"
    target_db.parent.mkdir(parents=True)
    target_db.write_bytes(source_env.db_path.read_bytes())
    connection = sqlite3.connect(target_db)
    try:
        connection.execute(
            "INSERT INTO task (title, status) VALUES (?, ?)",
            ("live generation", "live"),
        )
        connection.commit()
    finally:
        connection.close()

    real_apply = snapshot_mod._apply_swap
    real_write = snapshot_mod.write_restore_journal
    real_remove = snapshot_mod._remove_operation_entry
    local_mutated = False

    def mark_first_mutation(operation):
        nonlocal local_mutated
        real_apply(operation)
        if (
            operation.component == "healthmes_db"
            and operation.staged is not None
        ):
            local_mutated = True

    def fail_after_mutation(path, journal):
        if local_mutated:
            raise BackupError("injected journal persistence failure")
        real_write(path, journal)

    def fail_rollback_remove(operation, path, *, expected, label):
        if (
            operation.component == "healthmes_db"
            and operation.staged is not None
            and path == operation.destination
        ):
            real_remove(
                operation,
                path,
                expected=expected,
                label=label,
            )
            raise OSError("injected rollback removal failure")
        real_remove(
            operation,
            path,
            expected=expected,
            label=label,
        )

    monkeypatch.setattr(snapshot_mod, "_apply_swap", mark_first_mutation)
    monkeypatch.setattr(
        snapshot_mod,
        "write_restore_journal",
        fail_after_mutation,
    )
    monkeypatch.setattr(
        snapshot_mod,
        "_remove_operation_entry",
        fail_rollback_remove,
    )

    with pytest.raises(BackupError) as excinfo:
        restore_snapshot(
            snapshot,
            passphrase=source_env.passphrase,
            locations=target,
        )

    message = str(excinfo.value)
    expected_original = (
        "original restore failure: injected journal persistence failure"
    )
    assert expected_original in message, message
    original_index = message.index(expected_original)
    journal_index = message.index("restore journal persistence failures:")
    rollback_index = message.index(
        "local rollback failures: healthmes_db: "
        "injected rollback removal failure"
    )
    assert original_index < journal_index < rollback_index
    assert "local rollback was incomplete" in message
    assert not target_db.exists()
    assert list(
        target_db.parent.glob(
            ".healthmes.db.healthmes-restore-*.backup"
        )
    )


def test_successful_restore_reports_plaintext_artifact_cleanup_failure(
    source_env,
    fresh_locations,
    tmp_path,
    monkeypatch,
):
    snapshot = _make_snapshot(source_env, tmp_path / "original")
    target, target_root = fresh_locations("successful-cleanup-failure-target")
    target_db = target_root / "data" / "healthmes.db"
    target_db.parent.mkdir(parents=True)
    target_db.write_bytes(source_env.db_path.read_bytes())
    connection = sqlite3.connect(target_db)
    try:
        connection.execute(
            "INSERT INTO task (title, status) VALUES (?, ?)",
            ("live generation", "live"),
        )
        connection.commit()
    finally:
        connection.close()

    real_remove = snapshot_mod._remove_operation_entry
    retained_artifacts: list[Path] = []

    def fail_first_backup_cleanup(operation, path, *, expected, label):
        if (
            not retained_artifacts
            and path.name.endswith(".backup")
            and snapshot_mod._anchored_metadata(operation, path) is not None
        ):
            retained_artifacts.append(path)
            raise OSError("injected backup cleanup failure")
        real_remove(
            operation,
            path,
            expected=expected,
            label=label,
        )

    monkeypatch.setattr(
        snapshot_mod,
        "_remove_operation_entry",
        fail_first_backup_cleanup,
    )

    with pytest.raises(
        BackupError,
        match=(
            "restored generation remains active, but decrypted restore "
            "artifacts could not be removed"
        ),
    ):
        restore_snapshot(
            snapshot,
            passphrase=source_env.passphrase,
            locations=target,
        )

    connection = sqlite3.connect(target_db)
    try:
        titles = {row[0] for row in connection.execute("SELECT title FROM task")}
    finally:
        connection.close()
    assert "live generation" not in titles
    assert len(retained_artifacts) == 1
    assert retained_artifacts[0].exists()


def test_restore_scratch_cleanup_failure_preserves_original_failure(
    source_env,
    fresh_locations,
    tmp_path,
    monkeypatch,
):
    snapshot = _make_snapshot(source_env, tmp_path / "original")
    target, target_root = fresh_locations("scratch-cleanup-failure-target")
    target_db = target_root / "data" / "healthmes.db"
    target_db.parent.mkdir(parents=True)
    target_db.write_bytes(source_env.db_path.read_bytes())

    real_remove_scratch = snapshot_mod._remove_restore_scratch

    def fail_restore_body(*_args, **_kwargs):
        raise BackupError("injected restore body failure")

    def remove_then_report(path):
        assert real_remove_scratch(path) is None
        return f"{path}: injected scratch cleanup failure"

    monkeypatch.setattr(
        snapshot_mod,
        "_stage_restore_plan",
        fail_restore_body,
    )
    monkeypatch.setattr(
        snapshot_mod,
        "_remove_restore_scratch",
        remove_then_report,
    )

    with pytest.raises(
        BackupError,
        match="injected restore body failure.*decrypted restore scratch could not be removed",
    ):
        restore_snapshot(
            snapshot,
            passphrase=source_env.passphrase,
            locations=target,
        )


def test_successful_restore_reports_scratch_cleanup_failure(
    source_env,
    fresh_locations,
    tmp_path,
    monkeypatch,
):
    snapshot = _make_snapshot(source_env, tmp_path / "original")
    target, _target_root = fresh_locations("successful-scratch-cleanup-failure-target")
    real_remove_scratch = snapshot_mod._remove_restore_scratch

    def remove_then_report(path):
        assert real_remove_scratch(path) is None
        return f"{path}: injected scratch cleanup failure"

    monkeypatch.setattr(
        snapshot_mod,
        "_remove_restore_scratch",
        remove_then_report,
    )

    with pytest.raises(
        BackupError,
        match="restore completed.*decrypted restore scratch could not be removed",
    ):
        restore_snapshot(
            snapshot,
            passphrase=source_env.passphrase,
            locations=target,
        )


def test_failed_restore_reports_cleanup_failure_after_completed_rollback(
    source_env,
    fresh_locations,
    tmp_path,
    monkeypatch,
):
    snapshot = _make_snapshot(source_env, tmp_path / "original")
    target, target_root = fresh_locations("rollback-cleanup-failure-target")
    target_db = target_root / "data" / "healthmes.db"
    target_db.parent.mkdir(parents=True)
    target_db.write_bytes(source_env.db_path.read_bytes())
    connection = sqlite3.connect(target_db)
    try:
        connection.execute(
            "INSERT INTO task (title, status) VALUES (?, ?)",
            ("live generation", "live"),
        )
        connection.commit()
    finally:
        connection.close()

    real_apply = snapshot_mod._apply_swap
    real_remove = snapshot_mod._remove_operation_entry
    retained_artifacts: list[Path] = []

    def fail_on_hermes(operation):
        if operation.component == "hermes_home":
            raise OSError("injected later swap failure")
        real_apply(operation)

    def fail_first_staged_cleanup(operation, path, *, expected, label):
        if (
            not retained_artifacts
            and path.name.endswith(".staged")
            and snapshot_mod._anchored_metadata(operation, path) is not None
        ):
            retained_artifacts.append(path)
            raise OSError("injected staged cleanup failure")
        real_remove(
            operation,
            path,
            expected=expected,
            label=label,
        )

    monkeypatch.setattr(snapshot_mod, "_apply_swap", fail_on_hermes)
    monkeypatch.setattr(
        snapshot_mod,
        "_remove_operation_entry",
        fail_first_staged_cleanup,
    )

    with pytest.raises(
        BackupError,
        match=(
            "injected later swap failure.*local rollback completed, but "
            "decrypted restore artifacts could not be removed"
        ),
    ):
        restore_snapshot(
            snapshot,
            passphrase=source_env.passphrase,
            locations=target,
        )

    connection = sqlite3.connect(target_db)
    try:
        titles = {row[0] for row in connection.execute("SELECT title FROM task")}
    finally:
        connection.close()
    assert "live generation" in titles
    assert len(retained_artifacts) == 1
    assert retained_artifacts[0].exists()


@pytest.mark.parametrize("is_tree", [False, True])
def test_partial_local_staging_is_removed_when_copy_fails(
    tmp_path,
    monkeypatch,
    is_tree,
):
    source = tmp_path / ("source-tree" if is_tree else "source.db")
    if is_tree:
        source.mkdir()
        (source / "secret.bin").write_bytes(b"decrypted tree bytes")
    else:
        source.write_bytes(b"decrypted database bytes")
    destination = tmp_path / "live" / ("media" if is_tree else "healthmes.db")
    transaction_id = "partial-copy"
    staged = destination.with_name(f".{destination.name}.healthmes-restore-{transaction_id}.staged")

    if is_tree:

        def fail_tree_copy(
            _source,
            *,
            destination_descriptor,
        ):
            descriptor = os.open(
                "secret.bin",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=destination_descriptor,
            )
            try:
                os.write(descriptor, b"partial decrypted bytes")
            finally:
                os.close(descriptor)
            raise OSError("injected partial tree copy")

        monkeypatch.setattr(
            snapshot_mod,
            "_copy_tree_contents_to_descriptor",
            fail_tree_copy,
        )
    else:

        def fail_file_copy(
            _source,
            *,
            destination_name,
            destination_parent_descriptor,
        ):
            descriptor = os.open(
                destination_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=destination_parent_descriptor,
            )
            try:
                os.write(descriptor, b"partial decrypted bytes")
            finally:
                os.close(descriptor)
            raise OSError("injected partial file copy")

        monkeypatch.setattr(
            snapshot_mod,
            "_copy_regular_file_to_descriptor",
            fail_file_copy,
        )

    with pytest.raises(OSError, match="injected partial"):
        snapshot_mod._stage_local_component(
            component="media" if is_tree else "healthmes_db",
            source=source,
            destination=destination,
            transaction_id=transaction_id,
            is_tree=is_tree,
        )

    assert not staged.exists()


def test_preflight_cleanup_attempts_every_staged_component(
    source_env,
    fresh_locations,
    tmp_path,
    monkeypatch,
):
    snapshot = _make_snapshot(source_env, tmp_path / "original")
    target, target_root = fresh_locations("cleanup-all-target")
    real_stage = snapshot_mod._stage_planned_operation
    real_remove = snapshot_mod._remove_operation_entry
    failed_cleanup = False
    removed: list[Path] = []

    def fail_late_stage(operation, *, limits):
        if operation.component == "hermes_home":
            raise OSError("injected late staging failure")
        return real_stage(operation, limits=limits)

    def fail_first_cleanup(operation, path, *, expected, label):
        nonlocal failed_cleanup
        if (
            not failed_cleanup
            and ".healthmes-restore-" in path.name
            and path.name.endswith(".staged")
        ):
            failed_cleanup = True
            raise OSError("injected first cleanup failure")
        removed.append(path)
        real_remove(
            operation,
            path,
            expected=expected,
            label=label,
        )

    monkeypatch.setattr(
        snapshot_mod,
        "_stage_planned_operation",
        fail_late_stage,
    )
    monkeypatch.setattr(
        snapshot_mod,
        "_remove_operation_entry",
        fail_first_cleanup,
    )

    with pytest.raises(
        BackupError,
        match="some decrypted artifacts could not be removed",
    ):
        restore_snapshot(
            snapshot,
            passphrase=source_env.passphrase,
            locations=target,
        )

    assert failed_cleanup is True
    assert any("media" in path.name for path in removed)
    assert not list((target_root / "data").glob(".media.healthmes-restore-*.staged"))

    monkeypatch.setattr(
        snapshot_mod,
        "_remove_operation_entry",
        real_remove,
    )
    recover_incomplete_restore(target)

    assert not list((target_root / "data").glob(".*.healthmes-restore-*.staged"))
    assert not snapshot_mod.restore_journal_path(
        target.restore_state_dir or Path()
    ).exists()


def test_restore_refuses_running_sqlite_runtime_without_mutation(
    source_env,
    fresh_locations,
    tmp_path,
):
    snapshot = _make_snapshot(source_env, tmp_path / "original")
    target, target_root = fresh_locations("runtime-active-target")
    target_db = target_root / "data" / "healthmes.db"
    target_db.parent.mkdir(parents=True)
    target_db.write_bytes(source_env.db_path.read_bytes())
    connection = sqlite3.connect(target_db)
    try:
        connection.execute(
            "INSERT INTO task (title, status) VALUES (?, ?)",
            ("live runtime generation", "live"),
        )
        connection.commit()
    finally:
        connection.close()

    with sqlite_runtime_guard(target.database_url):
        with pytest.raises(
            BackupError,
            match="running HealthMes process",
        ):
            restore_snapshot(
                snapshot,
                passphrase=source_env.passphrase,
                locations=target,
            )

    connection = sqlite3.connect(target_db)
    try:
        titles = {row[0] for row in connection.execute("SELECT title FROM task")}
    finally:
        connection.close()
    assert "live runtime generation" in titles
    assert not list(target_db.parent.glob("*.healthmes-restore-*"))


def test_failed_restore_keeps_writer_fenced_until_rollback_finishes(
    source_env,
    fresh_locations,
    tmp_path,
    monkeypatch,
):
    snapshot = _make_snapshot(source_env, tmp_path / "original")
    target, _ = fresh_locations("rollback-fence-target")
    rollback_started = Event()
    release_rollback = Event()
    writer_acquired = Event()
    restore_errors: list[BaseException] = []
    real_apply = snapshot_mod._apply_swap
    real_rollback = snapshot_mod._rollback_local_operations

    def fail_on_hermes(operation):
        if operation.component == "hermes_home":
            raise OSError("injected later swap failure")
        real_apply(operation)

    def paused_rollback(operations):
        rollback_started.set()
        if not release_rollback.wait(10):
            raise TimeoutError("test did not release restore rollback")
        return real_rollback(operations)

    def run_restore():
        try:
            restore_snapshot(
                snapshot,
                passphrase=source_env.passphrase,
                locations=target,
            )
        except BaseException as exc:
            restore_errors.append(exc)

    def wait_for_write_plane():
        with activity_write_lock():
            writer_acquired.set()

    monkeypatch.setattr(snapshot_mod, "_apply_swap", fail_on_hermes)
    monkeypatch.setattr(
        snapshot_mod,
        "_rollback_local_operations",
        paused_rollback,
    )
    restore_thread = Thread(target=run_restore)
    writer_thread = Thread(target=wait_for_write_plane)
    restore_thread.start()
    assert rollback_started.wait(10)
    writer_thread.start()
    assert not writer_acquired.wait(0.2)
    release_rollback.set()
    restore_thread.join(10)
    writer_thread.join(10)

    assert not restore_thread.is_alive()
    assert not writer_thread.is_alive()
    assert writer_acquired.is_set()
    assert len(restore_errors) == 1
    assert isinstance(restore_errors[0], BackupError)
    assert "injected later swap failure" in str(restore_errors[0])


def test_restore_failure_details_survive_write_fence_release_failure(
    source_env,
    fresh_locations,
    tmp_path,
    monkeypatch,
):
    snapshot = _make_snapshot(source_env, tmp_path / "original")
    target, _ = fresh_locations("compound-failure-target")
    real_guard = snapshot_mod.global_write_plane_guard
    real_apply = snapshot_mod._apply_swap
    real_remove = snapshot_mod._remove_operation_entry
    retained_artifacts: list[Path] = []

    @contextmanager
    def replacing_failure_guard(database_url):
        body_failure: BaseException | None = None
        with real_guard(database_url):
            try:
                yield
            except BaseException as exc:
                body_failure = exc
        assert body_failure is not None
        raise OSError("injected fence release failure")

    def fail_on_hermes(operation):
        if operation.component == "hermes_home":
            raise OSError("injected later swap failure")
        real_apply(operation)

    def fail_first_staged_cleanup(operation, path, *, expected, label):
        if (
            not retained_artifacts
            and path.name.endswith(".staged")
            and snapshot_mod._anchored_metadata(operation, path) is not None
        ):
            retained_artifacts.append(path)
            raise OSError("injected plaintext cleanup failure")
        real_remove(
            operation,
            path,
            expected=expected,
            label=label,
        )

    monkeypatch.setattr(
        snapshot_mod,
        "global_write_plane_guard",
        replacing_failure_guard,
    )
    monkeypatch.setattr(snapshot_mod, "_apply_swap", fail_on_hermes)
    monkeypatch.setattr(
        snapshot_mod,
        "_remove_operation_entry",
        fail_first_staged_cleanup,
    )

    with pytest.raises(BackupError) as excinfo:
        restore_snapshot(
            snapshot,
            passphrase=source_env.passphrase,
            locations=target,
        )

    message = str(excinfo.value)
    assert "injected later swap failure" in message
    assert "decrypted restore artifacts could not be removed" in message
    assert "injected plaintext cleanup failure" in message
    assert "additionally, releasing the HealthMes write plane failed" in message
    assert "injected fence release failure" in message
    assert len(retained_artifacts) == 1
    assert retained_artifacts[0].exists()


def test_write_fence_exit_failure_never_erases_a_post_unlock_write(
    source_env,
    fresh_locations,
    tmp_path,
    monkeypatch,
):
    snapshot = _make_snapshot(source_env, tmp_path / "original")
    target, target_root = fresh_locations("fence-exit-target")
    target_db = target_root / "data" / "healthmes.db"
    target_db.parent.mkdir(parents=True)
    target_db.write_bytes(source_env.db_path.read_bytes())
    connection = sqlite3.connect(target_db)
    try:
        connection.execute(
            "INSERT INTO task (title, status) VALUES (?, ?)",
            ("live generation", "live"),
        )
        connection.commit()
    finally:
        connection.close()
    target.media_dir.mkdir(parents=True)
    live_media = target.media_dir / "live-only.bin"
    live_media.write_bytes(b"live media")
    expected_media = (source_env.media_dir / "note.txt").read_bytes()
    guard_released = Event()
    writer_committed = Event()
    writer_errors: list[BaseException] = []
    real_guard = snapshot_mod.global_write_plane_guard
    real_remove = snapshot_mod._remove_operation_entry
    retained_artifacts: list[Path] = []

    @contextmanager
    def failing_guard(database_url):
        with real_guard(database_url):
            yield
        guard_released.set()
        if not writer_committed.wait(10):
            raise TimeoutError("test writer did not commit after fence release")
        raise OSError("injected fence release failure")

    def write_after_unlock():
        try:
            if not guard_released.wait(10):
                raise TimeoutError("test guard was not released")
            connection = sqlite3.connect(target_db)
            try:
                connection.execute(
                    "INSERT INTO task (title, status) VALUES (?, ?)",
                    ("post-unlock committed write", "live"),
                )
                connection.commit()
            finally:
                connection.close()
        except BaseException as exc:
            writer_errors.append(exc)
        finally:
            writer_committed.set()

    def fail_first_backup_cleanup(operation, path, *, expected, label):
        if (
            not retained_artifacts
            and path.name.endswith(".backup")
            and snapshot_mod._anchored_metadata(operation, path) is not None
        ):
            retained_artifacts.append(path)
            raise OSError("injected fence-exit cleanup failure")
        real_remove(
            operation,
            path,
            expected=expected,
            label=label,
        )

    monkeypatch.setattr(
        snapshot_mod,
        "global_write_plane_guard",
        failing_guard,
    )
    monkeypatch.setattr(
        snapshot_mod,
        "_remove_operation_entry",
        fail_first_backup_cleanup,
    )
    writer = Thread(target=write_after_unlock)
    writer.start()
    with pytest.raises(
        BackupError,
        match=(
            "restored generation remains active and was not rolled back"
            ".*decrypted restore artifacts could not be removed"
        ),
    ):
        restore_snapshot(
            snapshot,
            passphrase=source_env.passphrase,
            locations=target,
        )
    writer.join(10)
    assert not writer.is_alive()
    assert writer_errors == []

    connection = sqlite3.connect(target_db)
    try:
        titles = {row[0] for row in connection.execute("SELECT title FROM task")}
    finally:
        connection.close()
    assert "post-unlock committed write" in titles
    assert "live generation" not in titles
    assert not live_media.exists()
    assert (target.media_dir / "note.txt").read_bytes() == expected_media
    assert len(retained_artifacts) == 1
    assert retained_artifacts[0].exists()


def test_restore_removes_stale_sqlite_sidecars(source_env, fresh_locations, tmp_path):
    snapshot = _make_snapshot(source_env, tmp_path / "original")
    target, target_root = fresh_locations("sidecar-target")
    target_db = target_root / "data" / "healthmes.db"
    target_db.parent.mkdir(parents=True)
    target_db.write_bytes(source_env.db_path.read_bytes())
    sidecars = [Path(f"{target_db}{suffix}") for suffix in ("-wal", "-shm", "-journal")]
    for sidecar in sidecars:
        sidecar.write_bytes(b"stale")

    restore_snapshot(
        snapshot,
        passphrase=source_env.passphrase,
        locations=target,
    )

    assert all(not sidecar.exists() for sidecar in sidecars)
