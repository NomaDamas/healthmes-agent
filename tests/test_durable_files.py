import hashlib
import json
import os
import sys
import threading
import time
import uuid

import pytest

from healthmes import durable_files as durable_files_mod
from healthmes.backup import filesystem as backup_filesystem_mod
from healthmes.durable_files import (
    DurabilityUnsupportedError,
    DurableFileIdentity,
    DurablePublishError,
    FileGenerationMismatchError,
    MaintenanceBudget,
    MaintenanceBudgetExceeded,
    durable_publish_no_clobber,
    durable_unlink,
    recover_durable_unlink_quarantines,
    recover_durable_unlink_target,
)


def test_maintenance_budget_reports_cumulative_resource_limits():
    budget = MaintenanceBudget.start(
        timeout_seconds=60,
        max_hash_bytes=5,
        max_directory_entries=2,
    )

    budget.reserve_hash_bytes(3, phase="first hash")
    with pytest.raises(MaintenanceBudgetExceeded) as hash_error:
        budget.reserve_hash_bytes(3, phase="second hash")

    assert hash_error.value.resource == "hash_bytes"
    assert hash_error.value.phase == "second hash"
    assert hash_error.value.used == 6
    assert hash_error.value.limit == 5

    budget.consume_directory_entry(phase="scan", operation="scan")
    budget.consume_directory_entry(phase="unlink", operation="mutation")
    with pytest.raises(MaintenanceBudgetExceeded) as directory_error:
        budget.consume_directory_entry(
            phase="completion",
            operation="mutation",
        )

    assert directory_error.value.resource == "directory_entries"
    assert directory_error.value.phase == "completion"
    assert directory_error.value.used == 3
    assert directory_error.value.limit == 2


def test_maintenance_budget_reserves_completion_capsule_atomically():
    budget = MaintenanceBudget.start(
        timeout_seconds=60,
        max_hash_bytes=0,
        max_directory_entries=2,
    )

    with pytest.raises(MaintenanceBudgetExceeded):
        budget.reserve_directory_entries(
            3,
            phase="terminal publication",
            operation="mutation",
        )

    budget.reserve_directory_entries(
        2,
        phase="retry publication",
        operation="mutation",
    )
    with pytest.raises(MaintenanceBudgetExceeded) as raised:
        budget.consume_directory_entry(
            phase="after reservation",
            operation="scan",
        )

    assert raised.value.used == 3
    assert raised.value.limit == 2


@pytest.mark.skipif(os.name == "nt", reason="POSIX durable unlink contract")
def test_durable_unlink_fails_before_hash_when_hash_budget_is_exhausted(
    tmp_path,
):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"abcdef")
    expected = DurableFileIdentity.from_metadata(target.stat())
    budget = MaintenanceBudget.start(
        timeout_seconds=60,
        max_hash_bytes=5,
        max_directory_entries=32,
    )

    with pytest.raises(MaintenanceBudgetExceeded) as raised:
        durable_unlink(target, expected=expected, budget=budget)

    assert raised.value.resource == "hash_bytes"
    assert raised.value.phase == "durable unlink target hash"
    assert target.read_bytes() == b"abcdef"
    assert list(tmp_path.glob(".healthmes-unlink-v2-*")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX durable unlink contract")
def test_durable_unlink_rehashes_after_renaming_the_same_inode(
    tmp_path,
):
    payload = b"hash this inode exactly once"
    target = tmp_path / "payload.bin"
    target.write_bytes(payload)
    expected = DurableFileIdentity.from_metadata(target.stat())
    budget = MaintenanceBudget.start(
        timeout_seconds=60,
        max_hash_bytes=2 * len(payload),
        max_directory_entries=64,
    )

    assert durable_unlink(target, expected=expected, budget=budget) is True

    assert not target.exists()
    assert list(tmp_path.glob(".healthmes-unlink-v2-*")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX durable unlink contract")
def test_partial_unlink_metadata_never_appears_under_the_committed_name(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"metadata publication must be atomic")
    expected = DurableFileIdentity.from_metadata(target.stat())
    real_write = durable_files_mod.os.write
    metadata_writes = 0

    def fail_during_metadata_write(descriptor, payload):
        nonlocal metadata_writes
        metadata_writes += 1
        if metadata_writes == 1:
            return real_write(descriptor, payload[:8])
        raise OSError("simulated metadata write crash")

    monkeypatch.setattr(
        durable_files_mod.os,
        "write",
        fail_during_metadata_write,
    )

    with pytest.raises(OSError, match="metadata write crash"):
        durable_unlink(target, expected=expected)

    assert target.read_bytes() == b"metadata publication must be atomic"
    quarantines = list(tmp_path.glob(".healthmes-unlink-v2-*"))
    assert len(quarantines) == 1
    assert not (
        quarantines[0] / durable_files_mod._UNLINK_METADATA_NAME
    ).exists()

    monkeypatch.setattr(durable_files_mod.os, "write", real_write)
    report = recover_durable_unlink_quarantines(tmp_path)

    assert report.cleaned == 1
    assert report.unresolved == 0
    assert not quarantines[0].exists()
    assert target.read_bytes() == b"metadata publication must be atomic"


@pytest.mark.skipif(os.name == "nt", reason="POSIX durable unlink contract")
def test_unlink_metadata_publication_never_clobbers_existing_intent(
    tmp_path,
):
    quarantine = tmp_path / ".healthmes-unlink-v2-test"
    quarantine.mkdir()
    metadata = quarantine / durable_files_mod._UNLINK_METADATA_NAME
    metadata.write_bytes(b"existing deletion intent")
    descriptor = os.open(
        quarantine,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        with pytest.raises(FileExistsError):
            durable_files_mod._publish_unlink_metadata_posix(
                descriptor,
                b"replacement deletion intent",
            )
    finally:
        os.close(descriptor)

    assert metadata.read_bytes() == b"existing deletion intent"
    assert list(
        quarantine.glob(
            f"{durable_files_mod._UNLINK_METADATA_TMP_PREFIX}*"
        )
    ) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX durable unlink contract")
def test_durable_unlink_does_not_scan_unrelated_parent_entries(
    tmp_path,
):
    for index in range(40):
        (tmp_path / f"noise-{index:02d}.bin").write_bytes(b"noise")
    target = tmp_path / "payload.bin"
    target.write_bytes(b"payload")
    expected = DurableFileIdentity.from_metadata(target.stat())
    budget = MaintenanceBudget.start(
        timeout_seconds=60,
        max_hash_bytes=2 * len(b"payload"),
        max_directory_entries=8,
    )

    assert durable_unlink(target, expected=expected, budget=budget) is True

    assert not target.exists()
    assert list(tmp_path.glob(".healthmes-unlink-v2-*")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX durable unlink contract")
def test_unrelated_malformed_quarantine_never_blocks_live_target_unlink(
    tmp_path,
    monkeypatch,
):
    malformed = tmp_path / (
        f"{durable_files_mod._UNLINK_QUARANTINE_V2_PREFIX}"
        f"{uuid.uuid4().hex}"
    )
    malformed.mkdir()
    (malformed / durable_files_mod._UNLINK_METADATA_NAME).write_text(
        "{malformed",
        encoding="ascii",
    )
    target = tmp_path / "payload.bin"
    target.write_bytes(b"independent live target")
    expected = DurableFileIdentity.from_metadata(target.stat())
    legacy = tmp_path / (
        f".healthmes-unlink-{uuid.uuid4().hex}-{target.name}"
    )
    os.rename(target, legacy)

    def fail_isolation(*_args, **_kwargs):
        raise OSError("simulated manual-review isolation failure")

    monkeypatch.setattr(
        durable_files_mod,
        "_isolate_unlink_quarantine_posix",
        fail_isolation,
    )

    assert durable_unlink(
        target,
        missing_ok=True,
        expected=expected,
    ) is True

    assert not target.exists()
    assert not legacy.exists()
    assert malformed.is_dir()


@pytest.mark.skipif(os.name == "nt", reason="POSIX durable unlink contract")
def test_durable_unlink_completes_after_payload_unlink_when_deadline_passes(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"payload")
    expected = DurableFileIdentity.from_metadata(target.stat())
    budget = MaintenanceBudget.start(
        timeout_seconds=60,
        max_hash_bytes=64,
        max_directory_entries=32,
    )
    real_unlink = durable_files_mod.os.unlink
    payload_unlinked = False

    def expire_budget_after_payload_unlink(path, *args, **kwargs):
        nonlocal payload_unlinked
        if (
            path == durable_files_mod._UNLINK_PAYLOAD_NAME
            and kwargs.get("dir_fd") is not None
            and not payload_unlinked
        ):
            payload_unlinked = True
            budget.deadline = time.monotonic() - 1
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(
        durable_files_mod.os,
        "unlink",
        expire_budget_after_payload_unlink,
    )

    assert durable_unlink(target, expected=expected, budget=budget) is True
    assert payload_unlinked is True
    assert not target.exists()
    assert list(tmp_path.glob(".healthmes-unlink-v2-*")) == []


def test_directory_batch_rejects_non_progress(monkeypatch):
    monkeypatch.setattr(
        durable_files_mod,
        "_read_directory_batch",
        lambda _descriptor, offset: (("payload.bin",), offset, False),
    )

    with pytest.raises(OSError, match="without advancing"):
        durable_files_mod.read_directory_batch(-1, 7)


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Darwin native directory-cookie regression",
)
def test_native_directory_batch_accepts_opaque_darwin_cursors(tmp_path):
    expected = {f"payload-{index:04d}.bin" for index in range(1024)}
    for name in expected:
        (tmp_path / name).write_bytes(b"x")

    descriptor = os.open(
        tmp_path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    seen: set[str] = set()
    offset = 0
    try:
        for _ in range(256):
            names, offset, complete = durable_files_mod.read_directory_batch(
                descriptor,
                offset,
            )
            seen.update(names)
            if complete:
                break
        else:
            pytest.fail("native Darwin directory scan did not terminate")
    finally:
        os.close(descriptor)

    assert expected == seen


@pytest.mark.skipif(os.name == "nt", reason="POSIX anchored-directory contract")
def test_anchored_directory_allows_a_configured_symlink_alias(tmp_path):
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    alias = tmp_path / "configured-alias"
    alias.symlink_to(real_directory, target_is_directory=True)

    with durable_files_mod.open_directory_anchored(alias) as (
        canonical,
        descriptor,
    ):
        metadata = os.fstat(descriptor)

    assert canonical == real_directory.resolve()
    assert (metadata.st_dev, metadata.st_ino) == (
        real_directory.stat().st_dev,
        real_directory.stat().st_ino,
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX anchored-directory contract")
def test_durable_unlink_rejects_an_ancestor_symlink_swap(
    tmp_path,
    monkeypatch,
):
    configured = tmp_path / "configured"
    nested = configured / "data"
    nested.mkdir(parents=True)
    target = nested / "payload.bin"
    target.write_bytes(b"configured generation")
    displaced = tmp_path / "displaced"
    redirected = tmp_path / "redirected"
    redirected_nested = redirected / "data"
    redirected_nested.mkdir(parents=True)
    redirected_target = redirected_nested / target.name
    redirected_target.write_bytes(b"must not be deleted")
    parent_identity = (tmp_path.stat().st_dev, tmp_path.stat().st_ino)
    real_open = durable_files_mod.os.open
    swapped = False

    def swap_ancestor_before_open(path, *args, **kwargs):
        nonlocal swapped
        directory_descriptor = kwargs.get("dir_fd")
        if (
            not swapped
            and path == configured.name
            and directory_descriptor is not None
        ):
            parent = os.fstat(directory_descriptor)
            if (parent.st_dev, parent.st_ino) == parent_identity:
                configured.rename(displaced)
                configured.symlink_to(
                    redirected,
                    target_is_directory=True,
                )
                swapped = True
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(
        durable_files_mod.os,
        "open",
        swap_ancestor_before_open,
    )

    with pytest.raises(OSError):
        durable_unlink(target)

    assert swapped is True
    assert redirected_target.read_bytes() == b"must not be deleted"
    assert (displaced / "data" / target.name).read_bytes() == (
        b"configured generation"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX anchored-directory contract")
def test_durable_writer_rejects_an_ancestor_symlink_swap(
    tmp_path,
    monkeypatch,
):
    configured = tmp_path / "configured"
    configured.mkdir()
    displaced = tmp_path / "displaced"
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    destination = configured / "new" / "payload.bin"
    parent_identity = (tmp_path.stat().st_dev, tmp_path.stat().st_ino)
    real_open = durable_files_mod.os.open
    swapped = False

    def swap_ancestor_before_open(path, *args, **kwargs):
        nonlocal swapped
        directory_descriptor = kwargs.get("dir_fd")
        if (
            not swapped
            and path == configured.name
            and directory_descriptor is not None
        ):
            parent = os.fstat(directory_descriptor)
            if (parent.st_dev, parent.st_ino) == parent_identity:
                configured.rename(displaced)
                configured.symlink_to(
                    redirected,
                    target_is_directory=True,
                )
                swapped = True
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(
        durable_files_mod.os,
        "open",
        swap_ancestor_before_open,
    )

    with pytest.raises(OSError):
        with durable_files_mod.durable_exclusive_writer(destination) as output:
            output.write(b"must not escape")

    assert swapped is True
    assert not (redirected / "new").exists()
    assert not (displaced / "new").exists()


def _write_v2_unlink_quarantine(
    parent,
    *,
    target_name,
    expected,
    payload_source=None,
    keep_target=False,
    expected_sha256=None,
):
    quarantine = parent / f".healthmes-unlink-v2-{uuid.uuid4().hex}"
    quarantine.mkdir()
    (quarantine / durable_files_mod._UNLINK_METADATA_NAME).write_bytes(
        durable_files_mod._unlink_metadata(
            target_name=target_name,
            expected=expected,
            expected_sha256=(
                expected_sha256
                if expected_sha256 is not None
                else hashlib.sha256(
                    payload_source.read_bytes()
                    if payload_source is not None
                    else (parent / target_name).read_bytes()
                ).hexdigest()
            ),
        )
    )
    if payload_source is not None:
        if keep_target:
            os.link(
                payload_source,
                quarantine / durable_files_mod._UNLINK_PAYLOAD_NAME,
            )
        else:
            os.rename(
                payload_source,
                quarantine / durable_files_mod._UNLINK_PAYLOAD_NAME,
            )
    return quarantine


def _manual_unlink_quarantines(parent):
    return list(
        parent.glob(f"{durable_files_mod._UNLINK_MANUAL_REVIEW_PREFIX}*")
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX dir_fd race regression")
def test_expected_generation_unlink_never_deletes_raced_replacement(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "payload.bin"
    original_link = tmp_path / "original-link.bin"
    replacement_source = tmp_path / "replacement.bin"
    target.write_bytes(b"original generation")
    os.link(target, original_link)
    replacement_source.write_bytes(b"replacement generation")
    expected = DurableFileIdentity.from_metadata(target.stat())
    real_stat = durable_files_mod.os.stat
    real_rename = durable_files_mod.os.rename
    replacement_installed = False

    def install_replacement() -> None:
        nonlocal replacement_installed
        if replacement_installed:
            return
        replacement_installed = True
        os.replace(replacement_source, target)

    def stat_with_race(path, *args, **kwargs):
        if path == target.name and kwargs.get("dir_fd") is not None:
            install_replacement()
        return real_stat(path, *args, **kwargs)

    def rename_with_race(source, destination, *args, **kwargs):
        if source == target.name and kwargs.get("src_dir_fd") is not None:
            install_replacement()
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(durable_files_mod.os, "stat", stat_with_race)
    monkeypatch.setattr(durable_files_mod.os, "rename", rename_with_race)

    with pytest.raises(FileGenerationMismatchError, match="generation changed"):
        durable_unlink(target, expected=expected)

    assert target.read_bytes() == b"replacement generation"
    assert original_link.read_bytes() == b"original generation"
    assert list(tmp_path.glob(".healthmes-unlink-*")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_recovery_finishes_crash_left_v2_unlink(tmp_path):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"crash-left payload")
    expected = DurableFileIdentity.from_metadata(target.stat())
    quarantine = _write_v2_unlink_quarantine(
        tmp_path,
        target_name=target.name,
        expected=expected,
        payload_source=target,
    )

    report = recover_durable_unlink_quarantines(tmp_path)

    assert report.cleaned == 1
    assert report.unresolved == 0
    assert not target.exists()
    assert not quarantine.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_recovery_cleans_leftover_metadata_temp_after_final_publication(
    tmp_path,
):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"crash after metadata publication")
    expected = DurableFileIdentity.from_metadata(target.stat())
    quarantine = _write_v2_unlink_quarantine(
        tmp_path,
        target_name=target.name,
        expected=expected,
        payload_source=target,
    )
    temporary = (
        quarantine
        / f"{durable_files_mod._UNLINK_METADATA_TMP_PREFIX}{uuid.uuid4().hex}"
    )
    temporary.write_bytes(b"fully written leftover temp")

    report = recover_durable_unlink_quarantines(tmp_path)

    assert report.cleaned == 1
    assert report.unresolved == 0
    assert not target.exists()
    assert not quarantine.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_recovery_removes_both_names_for_same_journaled_generation(tmp_path):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"linked crash-left payload")
    expected = DurableFileIdentity.from_metadata(target.stat())
    quarantine = _write_v2_unlink_quarantine(
        tmp_path,
        target_name=target.name,
        expected=expected,
        payload_source=target,
        keep_target=True,
    )

    report = recover_durable_unlink_quarantines(tmp_path)

    assert report.cleaned == 1
    assert not target.exists()
    assert not quarantine.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_recovery_hashes_hard_linked_target_and_payload_only_once(tmp_path):
    payload = b"one inode visible through two names"
    target = tmp_path / "payload.bin"
    target.write_bytes(payload)
    expected = DurableFileIdentity.from_metadata(target.stat())
    quarantine = _write_v2_unlink_quarantine(
        tmp_path,
        target_name=target.name,
        expected=expected,
        payload_source=target,
        keep_target=True,
    )
    budget = MaintenanceBudget.start(
        timeout_seconds=60,
        max_hash_bytes=2 * len(payload),
        max_directory_entries=64,
    )

    report = recover_durable_unlink_quarantines(
        tmp_path,
        budget=budget,
    )

    assert report.cleaned == 1
    assert report.budget_exhausted is False
    assert not target.exists()
    assert not quarantine.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_recovery_never_deletes_target_replaced_after_validation(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"journaled generation")
    expected = DurableFileIdentity.from_metadata(target.stat())
    quarantine = _write_v2_unlink_quarantine(
        tmp_path,
        target_name=target.name,
        expected=expected,
        payload_source=target,
        keep_target=True,
    )
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"new replacement generation")
    real_rename = durable_files_mod.os.rename
    replacement_installed = False

    def race_recovery_rename(source, destination, *args, **kwargs):
        nonlocal replacement_installed
        if (
            source == target.name
            and destination == durable_files_mod._UNLINK_PAYLOAD_NAME
            and kwargs.get("src_dir_fd") is not None
            and not replacement_installed
        ):
            replacement_installed = True
            os.replace(replacement, target)
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(
        durable_files_mod.os,
        "rename",
        race_recovery_rename,
    )

    report = recover_durable_unlink_quarantines(tmp_path)

    assert replacement_installed is True
    assert report.cleaned == 0
    assert report.unresolved == 1
    assert target.read_bytes() == b"new replacement generation"
    assert not quarantine.exists()
    assert len(_manual_unlink_quarantines(tmp_path)) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_legacy_recovery_never_deletes_raced_replacement(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "payload.bin"
    legacy = tmp_path / (
        f".healthmes-unlink-{uuid.uuid4().hex}-{target.name}"
    )
    replacement = tmp_path / "replacement.bin"
    target.write_bytes(b"legacy journaled generation")
    os.link(target, legacy)
    expected = DurableFileIdentity.from_metadata(target.stat())
    replacement.write_bytes(b"new replacement generation")
    real_rename = durable_files_mod.os.rename
    replacement_installed = False

    def race_legacy_recovery(source, destination, *args, **kwargs):
        nonlocal replacement_installed
        if (
            source == target.name
            and destination == durable_files_mod._UNLINK_PAYLOAD_NAME
            and kwargs.get("src_dir_fd") is not None
            and not replacement_installed
        ):
            replacement_installed = True
            os.replace(replacement, target)
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(
        durable_files_mod.os,
        "rename",
        race_legacy_recovery,
    )

    with pytest.raises(
        FileGenerationMismatchError,
        match="file generation changed",
    ):
        durable_unlink(target, expected=expected)

    assert replacement_installed is True
    assert target.read_bytes() == b"new replacement generation"
    assert legacy.read_bytes() == b"legacy journaled generation"
    assert list(tmp_path.glob(".healthmes-unlink-v2-*")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX digest-cache contract")
def test_digest_cache_rehashes_same_inode_after_ctime_only_change(tmp_path):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"first!")
    initial = target.stat()
    expected = DurableFileIdentity.from_metadata(initial)
    cache = {}
    budget = MaintenanceBudget.start(
        timeout_seconds=60,
        max_hash_bytes=2 * initial.st_size,
        max_directory_entries=0,
    )
    parent = os.open(
        tmp_path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        first = durable_files_mod._sha256_expected_entry_posix(
            parent,
            target.name,
            expected,
            budget=budget,
            digest_cache=cache,
        )
        target.write_bytes(b"second")
        os.utime(
            target,
            ns=(initial.st_atime_ns, initial.st_mtime_ns),
        )
        second = durable_files_mod._sha256_expected_entry_posix(
            parent,
            target.name,
            expected,
            budget=budget,
            digest_cache=cache,
        )
    finally:
        os.close(parent)

    assert first == hashlib.sha256(b"first!").hexdigest()
    assert second == hashlib.sha256(b"second").hexdigest()
    assert first != second


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_recovery_preserves_new_target_generation(tmp_path):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"old generation")
    expected = DurableFileIdentity.from_metadata(target.stat())
    quarantine = _write_v2_unlink_quarantine(
        tmp_path,
        target_name=target.name,
        expected=expected,
        payload_source=target,
    )
    target.write_bytes(b"new generation")

    report = recover_durable_unlink_quarantines(tmp_path)

    assert report.cleaned == 1
    assert target.read_bytes() == b"new generation"
    assert not quarantine.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_recovery_preserves_same_metadata_replacement_with_wrong_digest(
    tmp_path,
):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"replacement bytes")
    expected = DurableFileIdentity.from_metadata(target.stat())
    quarantine = _write_v2_unlink_quarantine(
        tmp_path,
        target_name=target.name,
        expected=expected,
        expected_sha256=hashlib.sha256(b"journaled bytes").hexdigest(),
    )

    report = recover_durable_unlink_quarantines(tmp_path)

    assert report.cleaned == 0
    assert report.unresolved == 1
    assert target.read_bytes() == b"replacement bytes"
    assert not quarantine.exists()
    manual = _manual_unlink_quarantines(tmp_path)
    assert len(manual) == 1
    assert "contents do not match" in report.errors[0]

    repeated = recover_durable_unlink_quarantines(tmp_path)

    assert repeated.unresolved == 0
    assert repeated.errors == ()
    assert manual[0].exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_recovery_preserves_digestless_legacy_v2_metadata(tmp_path):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"legacy v2 bytes")
    expected = DurableFileIdentity.from_metadata(target.stat())
    quarantine = tmp_path / f".healthmes-unlink-v2-{uuid.uuid4().hex}"
    quarantine.mkdir()
    (quarantine / durable_files_mod._UNLINK_METADATA_NAME).write_text(
        json.dumps(
            {
                "expected": {
                    "device": expected.device,
                    "inode": expected.inode,
                    "mtime_ns": expected.mtime_ns,
                    "size": expected.size,
                },
                "target_name": "payload.bin",
                "version": 2,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    report = recover_durable_unlink_quarantines(tmp_path)

    assert report.cleaned == 0
    assert report.unresolved == 1
    assert target.read_bytes() == b"legacy v2 bytes"
    assert not quarantine.exists()
    manual = _manual_unlink_quarantines(tmp_path)
    assert len(manual) == 1
    assert "unsupported durable-unlink metadata version" in report.errors[0]

    repeated = recover_durable_unlink_quarantines(tmp_path)

    assert repeated.unresolved == 0
    assert repeated.errors == ()
    assert manual[0].exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_recovery_cleans_only_empty_metadata_free_v2_journal(tmp_path):
    empty = tmp_path / f".healthmes-unlink-v2-{uuid.uuid4().hex}"
    empty.mkdir()
    ambiguous = tmp_path / f".healthmes-unlink-v2-{uuid.uuid4().hex}"
    ambiguous.mkdir()
    (ambiguous / durable_files_mod._UNLINK_PAYLOAD_NAME).write_bytes(
        b"metadata-free payload"
    )

    report = recover_durable_unlink_quarantines(tmp_path)

    assert report.cleaned == 1
    assert report.unresolved == 1
    assert not empty.exists()
    assert not ambiguous.exists()
    manual = _manual_unlink_quarantines(tmp_path)
    assert len(manual) == 1
    assert (
        manual[0] / durable_files_mod._UNLINK_PAYLOAD_NAME
    ).read_bytes() == b"metadata-free payload"

    repeated = recover_durable_unlink_quarantines(tmp_path)

    assert repeated.unresolved == 0
    assert repeated.errors == ()


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_malformed_quarantine_does_not_block_unrelated_target_unlink(
    tmp_path,
):
    malformed = tmp_path / (
        f"{durable_files_mod._UNLINK_QUARANTINE_V2_PREFIX}"
        f"{'0' * 32}"
    )
    malformed.mkdir()
    (malformed / durable_files_mod._UNLINK_METADATA_NAME).write_text(
        "{malformed",
        encoding="ascii",
    )
    target = tmp_path / "payload.bin"
    target.write_bytes(b"independent deletion")
    expected = DurableFileIdentity.from_metadata(target.stat())

    assert durable_unlink(target, expected=expected) is True

    assert not target.exists()
    assert malformed.exists()

    report = recover_durable_unlink_quarantines(tmp_path)

    assert report.unresolved == 1
    assert not malformed.exists()
    manual = _manual_unlink_quarantines(tmp_path)
    assert len(manual) == 1
    assert (
        manual[0] / durable_files_mod._UNLINK_METADATA_NAME
    ).read_text(encoding="ascii") == "{malformed"


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_manual_review_rename_failure_preserves_original_quarantine(
    tmp_path,
    monkeypatch,
):
    malformed = tmp_path / (
        f"{durable_files_mod._UNLINK_QUARANTINE_V2_PREFIX}"
        f"{'1' * 32}"
    )
    malformed.mkdir()
    (malformed / durable_files_mod._UNLINK_METADATA_NAME).write_text(
        "{malformed",
        encoding="ascii",
    )
    real_rename = durable_files_mod.os.rename

    def fail_manual_review_rename(source, destination, *args, **kwargs):
        if (
            source == malformed.name
            and str(destination).startswith(
                durable_files_mod._UNLINK_MANUAL_REVIEW_PREFIX
            )
        ):
            raise OSError("simulated manual-review rename failure")
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(
        durable_files_mod.os,
        "rename",
        fail_manual_review_rename,
    )

    report = recover_durable_unlink_quarantines(tmp_path)

    assert report.cleaned == 0
    assert report.unresolved == 1
    assert "manual-review isolation failed" in report.errors[0]
    assert malformed.is_dir()
    assert _manual_unlink_quarantines(tmp_path) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_global_recovery_preserves_legacy_unlink_quarantine(tmp_path):
    legacy = tmp_path / (
        f".healthmes-unlink-{uuid.uuid4().hex}-payload.bin"
    )
    legacy.write_bytes(b"legacy payload")

    report = recover_durable_unlink_quarantines(tmp_path)

    assert report.cleaned == 0
    assert report.unresolved == 1
    assert legacy.read_bytes() == b"legacy payload"


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_global_recovery_stops_discovery_at_entry_budget(
    tmp_path,
):
    for index in range(20):
        (tmp_path / f"ordinary-{index:02d}.txt").write_bytes(b"x")

    report = recover_durable_unlink_quarantines(
        tmp_path,
        max_entries=3,
        max_seconds=10,
    )

    assert report.scanned == 3
    assert report.truncated
    assert not report.errors


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_recovery_cursor_publication_reserves_its_mutation_budget(tmp_path):
    budget = MaintenanceBudget.start(
        timeout_seconds=60,
        max_hash_bytes=0,
        max_directory_entries=4,
    )

    report = recover_durable_unlink_quarantines(
        tmp_path,
        max_entries=1,
        max_seconds=10,
        budget=budget,
    )

    cursor = (
        tmp_path
        / durable_files_mod._UNLINK_RECOVERY_CONTROL_DIRECTORY
        / durable_files_mod._UNLINK_RECOVERY_CURSOR_NAME
    )
    assert report.scanned == 0
    assert report.budget_exhausted is False
    assert cursor.is_file()
    with pytest.raises(MaintenanceBudgetExceeded) as raised:
        budget.consume_directory_entry(
            phase="after cursor publication",
            operation="scan",
        )
    assert raised.value.used == 5
    assert raised.value.limit == 4


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_existing_recovery_control_reserves_three_cursor_mutations(
    tmp_path,
):
    control = (
        tmp_path
        / durable_files_mod._UNLINK_RECOVERY_CONTROL_DIRECTORY
    )
    control.mkdir(mode=0o700)
    budget = MaintenanceBudget.start(
        timeout_seconds=60,
        max_hash_bytes=0,
        max_directory_entries=3,
    )

    report = recover_durable_unlink_quarantines(
        tmp_path,
        max_entries=1,
        max_seconds=10,
        budget=budget,
    )

    cursor = control / durable_files_mod._UNLINK_RECOVERY_CURSOR_NAME
    assert report.scanned == 0
    assert report.budget_exhausted is False
    assert cursor.is_file()
    with pytest.raises(MaintenanceBudgetExceeded) as raised:
        budget.consume_directory_entry(
            phase="after cursor publication",
            operation="scan",
        )
    assert raised.value.used == 4
    assert raised.value.limit == 3


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_missing_recovery_control_needs_four_cursor_mutations(tmp_path):
    budget = MaintenanceBudget.start(
        timeout_seconds=60,
        max_hash_bytes=0,
        max_directory_entries=3,
    )

    report = recover_durable_unlink_quarantines(
        tmp_path,
        max_entries=1,
        max_seconds=10,
        budget=budget,
    )

    assert report.scanned == 0
    assert report.budget_exhausted is True
    assert not (
        tmp_path
        / durable_files_mod._UNLINK_RECOVERY_CONTROL_DIRECTORY
    ).exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery lock contract")
def test_recovery_serializes_cursor_workers_per_root(tmp_path):
    acquired = threading.Event()
    release = threading.Event()
    worker_errors = []

    def hold_root_lock():
        descriptor = os.open(
            tmp_path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            durable_files_mod._acquire_recovery_lock(
                descriptor,
                deadline=time.monotonic() + 5,
                budget=None,
            )
            acquired.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release recovery lock")
        except BaseException as exc:
            worker_errors.append(exc)
        finally:
            os.close(descriptor)

    worker = threading.Thread(target=hold_root_lock)
    worker.start()
    assert acquired.wait(timeout=5)
    try:
        blocked = recover_durable_unlink_quarantines(
            tmp_path,
            max_entries=1,
            max_seconds=0.02,
        )
    finally:
        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert worker_errors == []
    assert blocked.truncated is True
    assert any("per-root lock" in error for error in blocked.errors)

    resumed = recover_durable_unlink_quarantines(
        tmp_path,
        max_entries=1,
        max_seconds=1,
    )

    assert resumed.budget_exhausted is False
    assert (
        tmp_path
        / durable_files_mod._UNLINK_RECOVERY_CONTROL_DIRECTORY
        / durable_files_mod._UNLINK_RECOVERY_CURSOR_NAME
    ).is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery cursor contract")
def test_recovery_cleans_crash_left_cursor_temporary(tmp_path):
    first = recover_durable_unlink_quarantines(
        tmp_path,
        max_entries=1,
        max_seconds=1,
    )
    assert first.budget_exhausted is False
    control = (
        tmp_path
        / durable_files_mod._UNLINK_RECOVERY_CONTROL_DIRECTORY
    )
    temporary = control / (
        f"{durable_files_mod._UNLINK_RECOVERY_CURSOR_TMP_PREFIX}"
        f"{uuid.uuid4().hex}"
    )
    temporary.write_bytes(b"crash-left cursor bytes")
    temporary.chmod(0o600)

    report = recover_durable_unlink_quarantines(
        tmp_path,
        max_entries=1,
        max_seconds=1,
    )

    assert report.budget_exhausted is False
    assert not temporary.exists()
    assert (
        control / durable_files_mod._UNLINK_RECOVERY_CURSOR_NAME
    ).is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery cursor contract")
def test_recovery_deadline_bounds_cursor_read(
    tmp_path,
    monkeypatch,
):
    control = (
        tmp_path
        / durable_files_mod._UNLINK_RECOVERY_CONTROL_DIRECTORY
    )
    control.mkdir(mode=0o700)
    cursor = control / durable_files_mod._UNLINK_RECOVERY_CURSOR_NAME
    cursor.write_bytes(b"{" * (32 * 1024))
    cursor.chmod(0o600)
    real_read = durable_files_mod.os.read
    cursor_reads = 0

    def slow_cursor_read(descriptor, size):
        nonlocal cursor_reads
        if size == 16 * 1024:
            cursor_reads += 1
            time.sleep(0.03)
        return real_read(descriptor, size)

    monkeypatch.setattr(
        durable_files_mod.os,
        "read",
        slow_cursor_read,
    )

    report = recover_durable_unlink_quarantines(
        tmp_path,
        max_entries=1,
        max_seconds=0.02,
    )

    assert report.cleaned == 0
    assert report.truncated is True
    assert cursor_reads == 1
    assert any("reading its cursor" in error for error in report.errors)
    assert cursor.is_file()


@pytest.mark.parametrize(
    "max_seconds",
    [float("nan"), float("inf"), float("-inf"), True],
)
def test_recovery_rejects_non_finite_time_budget(
    tmp_path,
    max_seconds,
):
    with pytest.raises(
        ValueError,
        match="max_seconds must be a finite positive number",
    ):
        recover_durable_unlink_quarantines(
            tmp_path,
            max_seconds=max_seconds,
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_repeated_small_shared_budgets_reach_a_late_quarantine(
    tmp_path,
    monkeypatch,
):
    for index in range(12):
        (tmp_path / f"!ordinary-{index:02d}.txt").write_bytes(b"x")
    target = tmp_path / "payload.bin"
    payload = b"late recovery payload"
    target.write_bytes(payload)
    expected = DurableFileIdentity.from_metadata(target.stat())
    quarantine = _write_v2_unlink_quarantine(
        tmp_path,
        target_name=target.name,
        expected=expected,
        payload_source=target,
    )

    def ordered_directory_batch(descriptor, offset):
        names = tuple(sorted(os.listdir(descriptor)))
        batch = names[offset : offset + 4]
        next_offset = offset + len(batch)
        return batch, next_offset, next_offset >= len(names)

    monkeypatch.setattr(
        durable_files_mod,
        "_read_directory_batch",
        ordered_directory_batch,
    )

    reports = []
    for _ in range(20):
        budget = MaintenanceBudget.start(
            timeout_seconds=60,
            max_hash_bytes=len(payload),
            # Existing cursor scan (1), publication (3), retry scan (1),
            # quarantine validation (2), and completion (3) must fit.
            max_directory_entries=10,
        )
        report = recover_durable_unlink_quarantines(
            tmp_path,
            max_entries=4,
            max_seconds=10,
            budget=budget,
        )
        reports.append(report)
        if not quarantine.exists():
            break
    else:
        pytest.fail("shared maintenance budget starved unlink recovery")

    assert not target.exists()
    assert any(report.truncated for report in reports[:-1])
    assert reports[-1].cleaned == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_repeated_recovery_reaches_quarantine_after_noisy_prefix(
    tmp_path,
    monkeypatch,
):
    for index in range(300):
        (tmp_path / f"!ordinary-{index:03d}.txt").write_bytes(b"x")
    target = tmp_path / "payload.bin"
    target.write_bytes(b"crash-left payload")
    expected = DurableFileIdentity.from_metadata(target.stat())
    quarantine = _write_v2_unlink_quarantine(
        tmp_path,
        target_name=target.name,
        expected=expected,
        payload_source=target,
    )
    batches: dict[tuple[int, int], tuple[str, ...]] = {}

    def ordered_directory_batch(descriptor, offset):
        identity = (
            os.fstat(descriptor).st_dev,
            os.fstat(descriptor).st_ino,
        )
        names = batches.setdefault(
            identity,
            tuple(sorted(os.listdir(descriptor))),
        )
        batch = names[offset : offset + 64]
        next_offset = offset + len(batch)
        return batch, next_offset, next_offset >= len(names)

    monkeypatch.setattr(
        durable_files_mod,
        "_read_directory_batch",
        ordered_directory_batch,
    )

    reports = []
    for _ in range(20):
        report = recover_durable_unlink_quarantines(
            tmp_path,
            max_entries=32,
            max_seconds=10,
        )
        reports.append(report)
        if not quarantine.exists():
            break

    assert not quarantine.exists()
    assert not target.exists()
    assert len(reports) > 1
    assert any(report.truncated for report in reports[:-1])


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_completed_sweep_discovers_later_nested_quarantine(tmp_path):
    nested = tmp_path / "media" / "2026" / "08"
    nested.mkdir(parents=True)
    (nested / "ordinary.bin").write_bytes(b"ordinary")

    for _ in range(20):
        report = recover_durable_unlink_quarantines(
            tmp_path,
            max_entries=2,
            max_seconds=10,
        )
        if not report.truncated:
            break
    else:
        pytest.fail("initial durable-unlink sweep did not complete")

    target = nested / "late.bin"
    target.write_bytes(b"late crash-left payload")
    expected = DurableFileIdentity.from_metadata(target.stat())
    quarantine = _write_v2_unlink_quarantine(
        nested,
        target_name=target.name,
        expected=expected,
        payload_source=target,
    )

    for _ in range(20):
        recover_durable_unlink_quarantines(
            tmp_path,
            max_entries=2,
            max_seconds=10,
        )
        if not quarantine.exists():
            break

    assert not quarantine.exists()
    assert not target.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX filename contract")
def test_recovery_traverses_backslash_directory_and_target_name(tmp_path):
    nested = tmp_path / "directory\\name"
    nested.mkdir()
    target = nested / "payload\\name.bin"
    target.write_bytes(b"backslash is a POSIX filename character")
    expected = DurableFileIdentity.from_metadata(target.stat())
    quarantine = _write_v2_unlink_quarantine(
        nested,
        target_name=target.name,
        expected=expected,
        payload_source=target,
    )

    for _ in range(20):
        report = recover_durable_unlink_quarantines(
            tmp_path,
            max_entries=2,
            max_seconds=10,
        )
        if not quarantine.exists():
            break
    else:
        pytest.fail("backslash-named quarantine was starved")

    assert not target.exists()
    assert report.cleaned == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_recovery_cursor_byte_budget_preserves_eventual_progress(
    tmp_path,
    monkeypatch,
):
    directory_names = [
        f"{index:03d}-{'x' * 96}"
        for index in range(24)
    ]
    for name in directory_names:
        directory = tmp_path / name
        directory.mkdir()
        (directory / "ordinary.bin").write_bytes(b"x")
    target_directory = tmp_path / directory_names[-1]
    target = target_directory / "target.bin"
    target.write_bytes(b"cursor byte pressure target")
    expected = DurableFileIdentity.from_metadata(target.stat())
    quarantine = _write_v2_unlink_quarantine(
        target_directory,
        target_name=target.name,
        expected=expected,
        payload_source=target,
    )
    batches: dict[tuple[int, int], tuple[str, ...]] = {}

    def ordered_directory_batch(descriptor, offset):
        metadata = os.fstat(descriptor)
        identity = metadata.st_dev, metadata.st_ino
        names = batches.setdefault(
            identity,
            tuple(sorted(os.listdir(descriptor))),
        )
        batch = names[offset : offset + 16]
        next_offset = offset + len(batch)
        return batch, next_offset, next_offset >= len(names)

    monkeypatch.setattr(
        durable_files_mod,
        "_UNLINK_RECOVERY_CURSOR_MAX_BYTES",
        2048,
    )
    monkeypatch.setattr(
        durable_files_mod,
        "_UNLINK_RECOVERY_CURSOR_MAX_GROWTH_RESERVE",
        256,
    )
    monkeypatch.setattr(
        durable_files_mod,
        "_read_directory_batch",
        ordered_directory_batch,
    )

    for _ in range(200):
        report = recover_durable_unlink_quarantines(
            tmp_path,
            max_entries=4,
            max_seconds=10,
        )
        cursor = (
            tmp_path
            / durable_files_mod._UNLINK_RECOVERY_CONTROL_DIRECTORY
            / durable_files_mod._UNLINK_RECOVERY_CURSOR_NAME
        )
        assert cursor.stat().st_size <= 2048
        assert not any(
            "cursor is too large" in error for error in report.errors
        )
        if not quarantine.exists():
            break
    else:
        pytest.fail("cursor byte pressure prevented eventual recovery")

    assert not target.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_global_recovery_stops_payload_hash_at_deadline(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"x" * (3 * 1024 * 1024))
    expected = DurableFileIdentity.from_metadata(target.stat())
    quarantine = _write_v2_unlink_quarantine(
        tmp_path,
        target_name=target.name,
        expected=expected,
        payload_source=target,
    )
    real_read = durable_files_mod.os.read
    payload_reads = 0

    def slow_read(descriptor, size):
        nonlocal payload_reads
        if size == 1024 * 1024:
            payload_reads += 1
            time.sleep(0.03)
        return real_read(descriptor, size)

    monkeypatch.setattr(durable_files_mod.os, "read", slow_read)

    report = recover_durable_unlink_quarantines(
        tmp_path,
        max_entries=1,
        max_seconds=0.02,
    )

    assert report.cleaned == 0
    assert report.truncated is True
    assert payload_reads == 1
    assert quarantine.exists()
    assert (
        quarantine / durable_files_mod._UNLINK_PAYLOAD_NAME
    ).stat().st_size == 3 * 1024 * 1024


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_explicit_deadline_cannot_extend_recovery_max_seconds(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"x" * (3 * 1024 * 1024))
    expected = DurableFileIdentity.from_metadata(target.stat())
    quarantine = _write_v2_unlink_quarantine(
        tmp_path,
        target_name=target.name,
        expected=expected,
        payload_source=target,
    )
    real_read = durable_files_mod.os.read
    payload_reads = 0

    def slow_read(descriptor, size):
        nonlocal payload_reads
        if size == 1024 * 1024:
            payload_reads += 1
            time.sleep(0.03)
        return real_read(descriptor, size)

    monkeypatch.setattr(durable_files_mod.os, "read", slow_read)

    report = recover_durable_unlink_quarantines(
        tmp_path,
        max_entries=1,
        max_seconds=0.02,
        deadline=time.monotonic() + 10,
    )

    assert report.cleaned == 0
    assert report.truncated is True
    assert payload_reads == 1
    assert quarantine.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_expected_unlink_recovers_matching_legacy_quarantine(tmp_path):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"legacy payload")
    expected = DurableFileIdentity.from_metadata(target.stat())
    legacy = tmp_path / (
        f".healthmes-unlink-{uuid.uuid4().hex}-{target.name}"
    )
    os.rename(target, legacy)

    assert durable_unlink(
        target,
        missing_ok=True,
        expected=expected,
    )
    assert not target.exists()
    assert not legacy.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_targeted_recovery_honors_budget_for_matching_legacy_quarantine(
    tmp_path,
):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"legacy payload")
    expected = DurableFileIdentity.from_metadata(target.stat())
    legacy = tmp_path / (
        f".healthmes-unlink-{uuid.uuid4().hex}-{target.name}"
    )
    os.rename(target, legacy)
    budget = MaintenanceBudget.start(
        timeout_seconds=60,
        max_hash_bytes=0,
        max_directory_entries=16,
    )

    assert recover_durable_unlink_target(
        target,
        expected,
        budget=budget,
    )
    assert not target.exists()
    assert not legacy.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_targeted_recovery_scans_all_journal_kinds_before_mutating(
    tmp_path,
):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"legacy payload")
    expected = DurableFileIdentity.from_metadata(target.stat())
    legacy = tmp_path / (
        f".healthmes-unlink-{uuid.uuid4().hex}-{target.name}"
    )
    os.rename(target, legacy)
    budget = MaintenanceBudget.start(
        timeout_seconds=60,
        max_hash_bytes=0,
        max_directory_entries=2,
    )

    with pytest.raises(MaintenanceBudgetExceeded):
        recover_durable_unlink_target(
            target,
            expected,
            budget=budget,
        )

    assert not target.exists()
    assert legacy.read_bytes() == b"legacy payload"


@pytest.mark.skipif(os.name == "nt", reason="POSIX recovery contract")
def test_targeted_recovery_finishes_matching_v2_quarantine_with_budget(
    tmp_path,
):
    payload = b"v2 targeted recovery payload"
    target = tmp_path / "payload.bin"
    target.write_bytes(payload)
    expected = DurableFileIdentity.from_metadata(target.stat())
    quarantine = _write_v2_unlink_quarantine(
        tmp_path,
        target_name=target.name,
        expected=expected,
        payload_source=target,
    )
    budget = MaintenanceBudget.start(
        timeout_seconds=60,
        max_hash_bytes=2 * len(payload),
        max_directory_entries=64,
    )

    assert recover_durable_unlink_target(
        target,
        expected,
        budget=budget,
    )
    assert not target.exists()
    assert not quarantine.exists()


def test_directory_entry_durability_fails_closed_when_platform_cannot_prove_it(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        durable_files_mod,
        "_DIRECTORY_ENTRY_DURABILITY_SUPPORTED",
        False,
    )

    with pytest.raises(DurabilityUnsupportedError, match="unsupported"):
        durable_files_mod._fsync_directory(tmp_path, -1)
    with pytest.raises(DurabilityUnsupportedError, match="unsupported"):
        backup_filesystem_mod.fsync_directory(tmp_path)


def test_windows_quarantine_recovery_never_reports_false_success(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(durable_files_mod.os, "name", "nt")

    report = recover_durable_unlink_quarantines(tmp_path)

    assert report.cleaned == 0
    assert report.unresolved == 1
    assert report.truncated is True
    assert report.errors
    assert "unsupported on Windows" in report.errors[0]


@pytest.mark.skipif(os.name == "nt", reason="POSIX publication race contract")
def test_publish_removes_only_the_replaced_staging_generation(
    tmp_path,
    monkeypatch,
):
    staged = tmp_path / "staged.bin"
    original_link = tmp_path / "original-link.bin"
    replacement = tmp_path / "replacement.bin"
    destination = tmp_path / "published.bin"
    staged.write_bytes(b"opened original generation")
    os.link(staged, original_link)
    replacement.write_bytes(b"raced replacement generation")
    real_link = durable_files_mod.os.link
    raced = False

    def replace_then_link(source, target, *args, **kwargs):
        nonlocal raced
        if (
            source == staged.name
            and target == destination.name
            and kwargs.get("src_dir_fd") is not None
            and not raced
        ):
            raced = True
            os.replace(replacement, staged)
        return real_link(source, target, *args, **kwargs)

    monkeypatch.setattr(durable_files_mod.os, "link", replace_then_link)

    with pytest.raises(
        DurablePublishError,
        match="replaced staged generation",
    ) as raised:
        durable_publish_no_clobber(staged, destination)

    assert raced is True
    assert raised.value.destination_created is True
    assert not destination.exists()
    assert staged.read_bytes() == b"raced replacement generation"
    assert original_link.read_bytes() == b"opened original generation"
    assert list(tmp_path.glob(".healthmes-unlink-*")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX publication race contract")
def test_publish_removes_a_symlink_generation_linked_from_raced_staging(
    tmp_path,
    monkeypatch,
):
    staged = tmp_path / "staged.bin"
    original_link = tmp_path / "original-link.bin"
    outside = tmp_path / "outside.bin"
    destination = tmp_path / "published.bin"
    staged.write_bytes(b"opened original generation")
    os.link(staged, original_link)
    outside.write_bytes(b"must remain untouched")
    real_link = durable_files_mod.os.link
    raced = False

    def replace_with_symlink_then_link(source, target, *args, **kwargs):
        nonlocal raced
        if (
            source == staged.name
            and target == destination.name
            and kwargs.get("src_dir_fd") is not None
            and not raced
        ):
            raced = True
            staged.unlink()
            staged.symlink_to(outside)
        return real_link(source, target, *args, **kwargs)

    monkeypatch.setattr(
        durable_files_mod.os,
        "link",
        replace_with_symlink_then_link,
    )

    with pytest.raises(
        DurablePublishError,
        match="replaced staged generation",
    ) as raised:
        durable_publish_no_clobber(staged, destination)

    assert raced is True
    assert raised.value.destination_created is True
    assert not destination.exists()
    assert staged.is_symlink()
    assert original_link.read_bytes() == b"opened original generation"
    assert outside.read_bytes() == b"must remain untouched"
    assert list(tmp_path.glob(".healthmes-publish-rollback-*")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX publication race contract")
def test_publish_cleanup_preserves_a_newer_destination_generation(
    tmp_path,
    monkeypatch,
):
    staged = tmp_path / "staged.bin"
    staged.write_bytes(b"opened original generation")
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"replacement linked by publish")
    newer = tmp_path / "newer.bin"
    newer.write_bytes(b"newer destination generation")
    destination = tmp_path / "published.bin"
    real_link = durable_files_mod.os.link
    real_rename = durable_files_mod.os.rename
    source_raced = False
    cleanup_raced = False

    def replace_then_link(source, target, *args, **kwargs):
        nonlocal source_raced
        if (
            source == staged.name
            and target == destination.name
            and kwargs.get("src_dir_fd") is not None
            and not source_raced
        ):
            source_raced = True
            os.replace(replacement, staged)
        return real_link(source, target, *args, **kwargs)

    def replace_then_quarantine(source, target, *args, **kwargs):
        nonlocal cleanup_raced
        if (
            source == destination.name
            and target == durable_files_mod._UNLINK_PAYLOAD_NAME
            and kwargs.get("src_dir_fd") is not None
            and not cleanup_raced
        ):
            cleanup_raced = True
            os.replace(newer, destination)
        return real_rename(source, target, *args, **kwargs)

    monkeypatch.setattr(durable_files_mod.os, "link", replace_then_link)
    monkeypatch.setattr(
        durable_files_mod.os,
        "rename",
        replace_then_quarantine,
    )

    with pytest.raises(
        DurablePublishError,
        match="could not be safely removed",
    ) as raised:
        durable_publish_no_clobber(staged, destination)

    assert source_raced is True
    assert cleanup_raced is True
    assert raised.value.destination_created is True
    assert destination.read_bytes() == b"newer destination generation"
    assert staged.read_bytes() == b"replacement linked by publish"
    assert list(tmp_path.glob(".healthmes-unlink-*")) == []
