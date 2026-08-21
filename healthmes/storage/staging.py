"""Bounded recovery for crash-left media and raw-ingest staging files."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from healthmes.activity.locking import global_write_plane_guard
from healthmes.config import Settings
from healthmes.durable_files import (
    DurableFileIdentity,
    DurableUnlinkRecoveryReport,
    MaintenanceBudget,
    MaintenanceBudgetExceeded,
    durable_publish_no_clobber,
    durable_unlink,
    open_directory_anchored,
    read_directory_batch,
    recover_durable_unlink_quarantines,
    verify_regular_file,
)
from healthmes.store import RawIngestEvent, StorageObject, WellnessEvent

_MEDIA_NAME = re.compile(r"^[0-9a-f]{32}\.[a-z0-9]+$")
_RAW_NAME = re.compile(
    r"^\d{6}_\d{6}-[0-9a-f]{12}\.(?:bin|json|xml)$"
)
_YEAR = re.compile(r"^\d{4}$")
_MONTH_OR_DAY = re.compile(r"^\d{2}$")

DEFAULT_STAGING_RECONCILE_MAX_ENTRIES = 256
DEFAULT_STAGING_RECONCILE_MAX_SECONDS = 1.0
_INDEX_CURSOR_NAME = ".healthmes-staging-index-cursor-v1.json"
_INDEX_CURSOR_MAX_BYTES = 1024
_FALLBACK_CURSOR_NAME = ".healthmes-staging-fallback-cursor-v1.json"
_FALLBACK_CURSOR_MAX_BYTES = 128 * 1024
_CURSOR_TEMP_CLEANUP_MAX_ENTRIES = 32
_FALLBACK_ROOTS = ("media", "raw_ingest")
_FALLBACK_MAX_DEPTH = 64


class _StagingAncestorMissing(OSError):
    """An anchored staging ancestor does not currently exist."""


class _StagingUnsafeAncestor(OSError):
    """A staging ancestor is unavailable, non-directory, or symlinked."""


@dataclass(frozen=True, slots=True)
class StagingReconciliationReport:
    scanned: int
    cleaned: int
    restored: int
    unlink_quarantines_scanned: int
    unlink_quarantines_cleaned: int
    unresolved: int
    truncated: bool
    errors: tuple[str, ...]
    budget_exhausted: bool = False


@dataclass(frozen=True, slots=True)
class _IndexCursorState:
    storage_object_id: uuid.UUID | None
    next_pass: str
    turn_generation: int = 0


@dataclass(slots=True)
class _FallbackFrame:
    component: str | None
    device: int | None = None
    inode: int | None = None
    mtime_ns: int | None = None
    ctime_ns: int | None = None
    offset: int = 0
    batch_index: int = 0
    rescan: bool = False


@dataclass(slots=True)
class _FallbackCursorState:
    next_root: str
    stacks: dict[str, list[_FallbackFrame]]
    next_pass: str = "indexed"
    turn_generation: int = 0
    cleanup_offset: int = 0
    cleanup_batch_index: int = 0


@dataclass(frozen=True, slots=True)
class _FallbackScanItem:
    candidate: _StagingCandidate | None
    error: str | None
    report_scanned: bool


@dataclass(frozen=True, slots=True)
class _FallbackScanResult:
    items: tuple[_FallbackScanItem, ...]
    consumed: int
    truncated: bool
    budget_error: MaintenanceBudgetExceeded | None = None


@dataclass(frozen=True, slots=True)
class _CursorCleanupResult:
    errors: tuple[str, ...]
    consumed: int
    truncated: bool


def _is_cursor_temporary(name: str) -> bool:
    for cursor_name in (_INDEX_CURSOR_NAME, _FALLBACK_CURSOR_NAME):
        prefix = f"{cursor_name}.tmp-"
        if not name.startswith(prefix):
            continue
        token = name[len(prefix) :]
        return len(token) == 32 and all(
            character in "0123456789abcdef" for character in token
        )
    return False


def _clean_cursor_temporary_entry(
    settings: Settings,
    parent: int,
    name: str,
    *,
    maintenance_budget: MaintenanceBudget | None = None,
) -> str | None:
    if not _is_cursor_temporary(name):
        return None
    path = settings.data_dir / ".staging" / name
    try:
        metadata = os.stat(
            name,
            dir_fd=parent,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"could not inspect staging cursor temporary {path}: {exc}"
    current_uid = getattr(
        os,
        "geteuid",
        lambda: metadata.st_uid,
    )()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != current_uid
    ):
        return f"unsafe staging cursor temporary preserved: {path}"
    _charge_directory_entry(
        maintenance_budget,
        phase="staging cursor temporary cleanup",
        operation="unlink",
    )
    try:
        os.unlink(name, dir_fd=parent)
        os.fsync(parent)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"could not remove staging cursor temporary {path}: {exc}"
    return None


@dataclass(frozen=True, slots=True)
class _StagingCandidate:
    kind: str
    staged: Path
    destination: Path
    relative_path: str


def _candidate_parts(
    candidate: _StagingCandidate,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    destination = tuple(candidate.relative_path.split("/"))
    staged = (
        ".staging",
        *destination[:-1],
        f"{destination[-1]}.part",
    )
    return staged, destination


def _validate_relative_parts(parts: tuple[str, ...]) -> None:
    if not parts or any(
        part in {"", ".", ".."}
        or "/" in part
        or (os.name == "nt" and "\\" in part)
        or "\x00" in part
        for part in parts
    ):
        raise ValueError("unsafe staging relative path")


def _checkpoint_maintenance(
    budget: MaintenanceBudget | None,
    *,
    phase: str,
) -> None:
    if budget is not None:
        budget.checkpoint(phase=phase)


def _checkpoint_deadline(
    deadline: float | None,
    maintenance_budget: MaintenanceBudget | None,
    *,
    phase: str,
) -> None:
    _checkpoint_maintenance(
        maintenance_budget,
        phase=phase,
    )
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError(f"{phase} deadline exceeded")


def _charge_directory_entry(
    budget: MaintenanceBudget | None,
    *,
    phase: str,
    operation: str,
) -> None:
    if budget is not None:
        budget.consume_directory_entry(
            phase=phase,
            operation=operation,
        )


def _reserve_directory_entries(
    budget: MaintenanceBudget | None,
    count: int,
    *,
    phase: str,
    operation: str,
) -> None:
    if budget is not None:
        budget.reserve_directory_entries(
            count,
            phase=phase,
            operation=operation,
        )


def _relative_entry_missing(parent: int, name: str) -> bool:
    try:
        os.stat(
            name,
            dir_fd=parent,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return True
    return False


@contextmanager
def _open_data_root(settings: Settings) -> Iterator[int]:
    root = settings.data_dir.expanduser()
    if os.name != "nt":
        with open_directory_anchored(root) as (_canonical, descriptor):
            yield descriptor
        return
    descriptor = os.open(
        root,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError(f"storage root must be a directory: {root}")
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _open_relative_directory(
    root_descriptor: int,
    root: Path,
    parts: tuple[str, ...],
    *,
    create: bool = False,
    maintenance_budget: MaintenanceBudget | None = None,
    creation_precharged: bool = False,
) -> Iterator[int]:
    _validate_relative_parts(parts)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    current = os.dup(root_descriptor)
    opened: list[int] = [current]
    current_path = root
    try:
        for component in parts:
            next_path = current_path / component
            try:
                next_descriptor = os.open(
                    component,
                    flags,
                    dir_fd=current,
                )
            except FileNotFoundError as exc:
                if not create:
                    raise _StagingAncestorMissing(
                        f"staging ancestor is missing: {next_path}"
                    ) from exc
                if not creation_precharged:
                    _charge_directory_entry(
                        maintenance_budget,
                        phase="staging directory creation",
                        operation="mkdir",
                    )
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current)
                except FileExistsError:
                    pass
                else:
                    os.fsync(current)
                try:
                    next_descriptor = os.open(
                        component,
                        flags,
                        dir_fd=current,
                    )
                except OSError as open_exc:
                    raise _StagingUnsafeAncestor(
                        "staging ancestor is unavailable or symlinked: "
                        f"{next_path}"
                    ) from open_exc
            except OSError as exc:
                raise _StagingUnsafeAncestor(
                    "staging ancestor is unavailable or symlinked: "
                    f"{next_path}"
                ) from exc
            metadata = os.fstat(next_descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(next_descriptor)
                raise _StagingUnsafeAncestor(
                    f"staging ancestor is not a directory: {next_path}"
                )
            current = next_descriptor
            opened.append(current)
            current_path = next_path
        yield current
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


@contextmanager
def _open_relative_parent(
    root_descriptor: int,
    root: Path,
    parts: tuple[str, ...],
    *,
    create: bool = False,
    maintenance_budget: MaintenanceBudget | None = None,
    creation_precharged: bool = False,
) -> Iterator[tuple[int, str]]:
    _validate_relative_parts(parts)
    with _open_relative_directory(
        root_descriptor,
        root,
        parts[:-1],
        create=create,
        maintenance_budget=maintenance_budget,
        creation_precharged=creation_precharged,
    ) as parent:
        yield parent, parts[-1]


def _metadata_generation(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_mode,
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _revalidate_verified_entry(
    parent: int,
    name: str,
    descriptor: int,
    *,
    expected_generation: tuple[int, ...],
    display_path: Path,
) -> None:
    descriptor_metadata = os.fstat(descriptor)
    named_metadata = os.stat(
        name,
        dir_fd=parent,
        follow_symlinks=False,
    )
    if (
        _metadata_generation(descriptor_metadata) != expected_generation
        or _metadata_generation(named_metadata) != expected_generation
    ):
        raise OSError(
            f"verified staging entry generation changed: {display_path}"
        )


def _hash_generation(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        stat.S_IFMT(metadata.st_mode),
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _named_identity_matches(
    parent: int,
    name: str,
    identity: DurableFileIdentity,
) -> bool:
    metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
    return identity.matches(metadata)


@contextmanager
def _open_verified_entry(
    parent: int,
    name: str,
    *,
    display_path: Path,
    expected_size: int,
    expected_sha256: str,
    deadline: float,
    maintenance_budget: MaintenanceBudget | None = None,
    digest_cache: (
        dict[tuple[int, int], tuple[tuple[int, ...], str]] | None
    ) = None,
) -> Iterator[
    tuple[int, DurableFileIdentity, tuple[int, ...]]
]:
    descriptor = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent,
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError(f"staging entry is not a regular file: {display_path}")
        if before.st_size != expected_size:
            raise ValueError(f"staging entry has an unexpected size: {display_path}")
        cache_key = before.st_dev, before.st_ino
        cache_generation = _hash_generation(before)
        cached = (
            digest_cache.get(cache_key)
            if digest_cache is not None
            else None
        )
        if cached is None or cached[0] != cache_generation:
            if maintenance_budget is not None:
                maintenance_budget.reserve_hash_bytes(
                    before.st_size,
                    phase="staging entry verification",
                )
            digest = hashlib.sha256()
            while True:
                _checkpoint_maintenance(
                    maintenance_budget,
                    phase="staging entry verification",
                )
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "staging reconciliation deadline exceeded"
                    )
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            digest_hex = digest.hexdigest()
        else:
            digest_hex = cached[1]
        after = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        generation = _metadata_generation(before)
        if (
            _metadata_generation(after) != generation
            or _metadata_generation(named) != generation
        ):
            raise OSError(
                f"staging entry changed while it was verified: {display_path}"
            )
        if digest_hex != expected_sha256.lower():
            raise ValueError(
                f"staging entry has an unexpected SHA-256: {display_path}"
            )
        if digest_cache is not None:
            digest_cache[cache_key] = cache_generation, digest_hex
        os.fsync(descriptor)
        yield (
            descriptor,
            DurableFileIdentity.from_metadata(before),
            generation,
        )
    finally:
        os.close(descriptor)


def _unlink_anchored_entry(
    parent: int,
    name: str,
    *,
    display_path: Path,
    expected: DurableFileIdentity,
    maintenance_budget: MaintenanceBudget | None = None,
    mutation_precharged: bool = False,
    descriptor: int | None = None,
    expected_generation: tuple[int, ...] | None = None,
) -> None:
    if expected_generation is not None:
        if descriptor is None:
            raise ValueError(
                "a descriptor is required for full generation validation"
            )
        _revalidate_verified_entry(
            parent,
            name,
            descriptor,
            expected_generation=expected_generation,
            display_path=display_path,
        )
    elif not _named_identity_matches(parent, name, expected):
        raise OSError(
            f"staging entry generation changed before unlink: {display_path}"
        )
    if not mutation_precharged:
        _charge_directory_entry(
            maintenance_budget,
            phase="staging entry cleanup",
            operation="unlink",
        )
    os.unlink(name, dir_fd=parent)
    os.fsync(parent)


def _remove_created_entry(
    parent: int,
    name: str,
    metadata: os.stat_result,
    *,
    maintenance_budget: MaintenanceBudget | None = None,
    mutation_precharged: bool = False,
    expected_generation: tuple[int, ...] | None = None,
) -> None:
    try:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_dev != metadata.st_dev
        or current.st_ino != metadata.st_ino
        or (
            expected_generation is not None
            and _metadata_generation(current) != expected_generation
        )
    ):
        return
    if not mutation_precharged:
        _charge_directory_entry(
            maintenance_budget,
            phase="staging publication rollback",
            operation="unlink",
        )
    os.unlink(name, dir_fd=parent)
    os.fsync(parent)


def _mapped_candidate(
    settings: Settings,
    *,
    kind: str,
    root: Path,
    staged: Path,
) -> _StagingCandidate | None:
    try:
        parts = staged.relative_to(root).parts
    except ValueError:
        return None
    if kind == "media":
        if (
            len(parts) != 3
            or _YEAR.fullmatch(parts[0]) is None
            or _MONTH_OR_DAY.fullmatch(parts[1]) is None
        ):
            return None
        filename = parts[2].removesuffix(".part")
        if (
            not parts[2].endswith(".part")
            or _MEDIA_NAME.fullmatch(filename) is None
        ):
            return None
        destination_parts = ("media", parts[0], parts[1], filename)
    elif kind == "raw_ingest":
        if (
            len(parts) != 4
            or _YEAR.fullmatch(parts[0]) is None
            or _MONTH_OR_DAY.fullmatch(parts[1]) is None
            or _MONTH_OR_DAY.fullmatch(parts[2]) is None
        ):
            return None
        filename = parts[3].removesuffix(".part")
        if (
            not parts[3].endswith(".part")
            or _RAW_NAME.fullmatch(filename) is None
        ):
            return None
        destination_parts = (
            "raw_ingest",
            parts[0],
            parts[1],
            parts[2],
            filename,
        )
    else:
        return None
    relative_path = "/".join(destination_parts)
    return _StagingCandidate(
        kind=kind,
        staged=staged,
        destination=settings.data_dir.joinpath(*destination_parts),
        relative_path=relative_path,
    )


def _indexed_candidate(
    settings: Settings,
    relative_path: str,
) -> _StagingCandidate | None:
    parts = tuple(relative_path.split("/"))
    if (
        len(parts) == 4
        and parts[0] == "media"
        and _YEAR.fullmatch(parts[1]) is not None
        and _MONTH_OR_DAY.fullmatch(parts[2]) is not None
        and _MEDIA_NAME.fullmatch(parts[3]) is not None
    ):
        kind = "media"
    elif (
        len(parts) == 5
        and parts[0] == "raw_ingest"
        and _YEAR.fullmatch(parts[1]) is not None
        and _MONTH_OR_DAY.fullmatch(parts[2]) is not None
        and _MONTH_OR_DAY.fullmatch(parts[3]) is not None
        and _RAW_NAME.fullmatch(parts[4]) is not None
    ):
        kind = "raw_ingest"
    else:
        return None
    destination = settings.data_dir.joinpath(*parts)
    staged = settings.data_dir.joinpath(
        ".staging",
        *parts,
    ).with_name(f"{parts[-1]}.part")
    return _StagingCandidate(
        kind=kind,
        staged=staged,
        destination=destination,
        relative_path=relative_path,
    )


def _raw_references_match(
    session: Session,
    storage: StorageObject,
    *,
    relative_path: str,
) -> bool:
    raw_rows = list(
        session.scalars(
            select(RawIngestEvent)
            .where(RawIngestEvent.path == relative_path)
            .limit(2)
        )
    )
    if len(raw_rows) != 1:
        return False
    raw = raw_rows[0]
    if (
        raw.size_bytes != storage.size_bytes
        or raw.sha256 != storage.sha256
        or raw.content_type != storage.content_type
    ):
        return False
    wellness_rows = list(
        session.scalars(
            select(WellnessEvent)
            .where(
                WellnessEvent.raw_object_id == storage.id,
                WellnessEvent.event_type == "raw_ingest",
            )
            .limit(2)
        )
    )
    return (
        len(wellness_rows) == 1
        and wellness_rows[0].source_provider == raw.source
        and wellness_rows[0].source_record_id == str(raw.id)
    )


def _indexed_storage(
    session: Session,
    candidate: _StagingCandidate,
) -> StorageObject | None:
    rows = list(
        session.scalars(
            select(StorageObject)
            .where(StorageObject.relative_path == candidate.relative_path)
            .limit(2)
        )
    )
    if len(rows) != 1:
        return None
    storage = rows[0]
    if (
        storage.purged_at is not None
        or storage.file_cleanup_completed_at is not None
        or storage.sha256 is None
    ):
        return None
    if candidate.kind == "media":
        if storage.data_class not in {"media", "nutrition_media"}:
            return None
    elif (
        storage.data_class not in {"raw_payload", "nutrition_raw_capture"}
        or not _raw_references_match(
            session,
            storage,
            relative_path=candidate.relative_path,
        )
    ):
        return None
    return storage


def _stage_identity_windows(
    candidate: _StagingCandidate,
    storage: StorageObject,
    *,
    deadline: float,
    maintenance_budget: MaintenanceBudget | None = None,
) -> DurableFileIdentity:
    metadata = candidate.staged.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(f"staging entry is not a regular file: {candidate.staged}")
    identity = DurableFileIdentity.from_metadata(metadata)
    if maintenance_budget is not None:
        maintenance_budget.reserve_hash_bytes(
            metadata.st_size,
            phase="staging entry verification",
        )
    verify_regular_file(
        candidate.staged,
        identity,
        expected_size=storage.size_bytes,
        expected_sha256=storage.sha256,
        deadline=deadline,
    )
    return identity


def _reconcile_candidate_windows(
    session: Session,
    candidate: _StagingCandidate,
    *,
    deadline: float,
    maintenance_budget: MaintenanceBudget | None = None,
) -> str:
    storage = _indexed_storage(session, candidate)
    if storage is None:
        return "unresolved"
    identity = _stage_identity_windows(
        candidate,
        storage,
        deadline=deadline,
        maintenance_budget=maintenance_budget,
    )
    try:
        candidate.destination.lstat()
    except FileNotFoundError:
        _reserve_directory_entries(
            maintenance_budget,
            3,
            phase="staging publication",
            operation="mutation",
        )
        published = durable_publish_no_clobber(
            candidate.staged,
            candidate.destination,
        )
        verify_regular_file(
            candidate.destination,
            published,
            expected_size=storage.size_bytes,
            expected_sha256=None,
            deadline=deadline,
        )
        durable_unlink(
            candidate.staged,
            expected=published,
            budget=None,
        )
        return "restored"

    _reserve_directory_entries(
        maintenance_budget,
        2,
        phase="staging entry cleanup",
        operation="mutation",
    )
    verify_regular_file(
        candidate.destination,
        identity,
        expected_size=storage.size_bytes,
        expected_sha256=None,
        deadline=deadline,
    )
    durable_unlink(
        candidate.staged,
        expected=identity,
        budget=None,
    )
    return "cleaned"


def _reconcile_candidate(
    session: Session,
    settings: Settings,
    root_descriptor: int,
    candidate: _StagingCandidate,
    *,
    deadline: float,
    maintenance_budget: MaintenanceBudget | None = None,
) -> str:
    if os.name == "nt":  # pragma: no cover - exercised on Windows runners
        return _reconcile_candidate_windows(
            session,
            candidate,
            deadline=deadline,
            maintenance_budget=maintenance_budget,
        )
    storage = _indexed_storage(session, candidate)
    if storage is None:
        return "unresolved"
    digest_cache: dict[
        tuple[int, int],
        tuple[tuple[int, ...], str],
    ] = {}
    staged_parts, destination_parts = _candidate_parts(candidate)
    with _open_relative_parent(
        root_descriptor,
        settings.data_dir,
        staged_parts,
    ) as (staged_parent, staged_name):
        with _open_verified_entry(
            staged_parent,
            staged_name,
            display_path=candidate.staged,
            expected_size=storage.size_bytes,
            expected_sha256=storage.sha256,
            deadline=deadline,
            maintenance_budget=maintenance_budget,
            digest_cache=digest_cache,
        ) as (
            staged_descriptor,
            identity,
            staged_generation,
        ):
            with _open_relative_parent(
                root_descriptor,
                settings.data_dir,
                destination_parts,
                create=True,
                maintenance_budget=maintenance_budget,
            ) as (destination_parent, destination_name):
                try:
                    destination_metadata = os.stat(
                        destination_name,
                        dir_fd=destination_parent,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    if not _named_identity_matches(
                        staged_parent, staged_name, identity
                    ):
                        raise OSError(
                            "staging entry generation changed before publish: "
                            f"{candidate.staged}"
                        )
                    _revalidate_verified_entry(
                        staged_parent,
                        staged_name,
                        staged_descriptor,
                        expected_generation=staged_generation,
                        display_path=candidate.staged,
                    )
                    _reserve_directory_entries(
                        maintenance_budget,
                        2,
                        phase="staging publication completion",
                        operation="mutation",
                    )
                    os.link(
                        staged_name,
                        destination_name,
                        src_dir_fd=staged_parent,
                        dst_dir_fd=destination_parent,
                        follow_symlinks=False,
                    )
                    published = os.stat(
                        destination_name,
                        dir_fd=destination_parent,
                        follow_symlinks=False,
                    )
                    rollback_generation = _metadata_generation(published)
                    if not identity.matches(published):
                        _remove_created_entry(
                            destination_parent,
                            destination_name,
                            published,
                            maintenance_budget=maintenance_budget,
                            mutation_precharged=True,
                            expected_generation=rollback_generation,
                        )
                        raise OSError(
                            "published entry does not match the verified "
                            f"staging generation: {candidate.destination}"
                        )
                    try:
                        with _open_verified_entry(
                            destination_parent,
                            destination_name,
                            display_path=candidate.destination,
                            expected_size=storage.size_bytes,
                            expected_sha256=storage.sha256,
                            deadline=deadline,
                            maintenance_budget=maintenance_budget,
                            digest_cache=digest_cache,
                        ) as (
                            destination_descriptor,
                            destination_identity,
                            destination_generation,
                        ):
                            if (
                                destination_identity.device != identity.device
                                or destination_identity.inode != identity.inode
                            ):
                                raise OSError(
                                    "published entry does not name the staged "
                                    f"inode: {candidate.destination}"
                                )
                            rollback_generation = destination_generation
                            os.fsync(destination_parent)
                            _revalidate_verified_entry(
                                destination_parent,
                                destination_name,
                                destination_descriptor,
                                expected_generation=destination_generation,
                                display_path=candidate.destination,
                            )
                            _revalidate_verified_entry(
                                staged_parent,
                                staged_name,
                                staged_descriptor,
                                expected_generation=destination_generation,
                                display_path=candidate.staged,
                            )
                    except BaseException:
                        _remove_created_entry(
                            destination_parent,
                            destination_name,
                            published,
                            maintenance_budget=maintenance_budget,
                            mutation_precharged=True,
                            expected_generation=rollback_generation,
                        )
                        raise
                    # The destination name and contents are durable now. Staging
                    # cleanup is a separate best-effort transition: if unlink
                    # succeeds but its directory fsync fails, rolling the
                    # destination back would remove the payload's only name.
                    _unlink_anchored_entry(
                        staged_parent,
                        staged_name,
                        display_path=candidate.staged,
                        expected=identity,
                        maintenance_budget=maintenance_budget,
                        mutation_precharged=True,
                        descriptor=staged_descriptor,
                        expected_generation=destination_generation,
                    )
                    return "restored"

                if not identity.matches(destination_metadata):
                    raise OSError(
                        "destination generation conflicts with staging entry: "
                        f"{candidate.destination}"
                    )
                with _open_verified_entry(
                    destination_parent,
                    destination_name,
                    display_path=candidate.destination,
                    expected_size=storage.size_bytes,
                    expected_sha256=storage.sha256,
                    deadline=deadline,
                    maintenance_budget=maintenance_budget,
                    digest_cache=digest_cache,
                ) as (
                    destination_descriptor,
                    destination_identity,
                    destination_generation,
                ):
                    if destination_identity != identity:
                        raise OSError(
                            "destination generation conflicts with staging "
                            f"entry: {candidate.destination}"
                        )
                    _revalidate_verified_entry(
                        destination_parent,
                        destination_name,
                        destination_descriptor,
                        expected_generation=destination_generation,
                        display_path=candidate.destination,
                    )
                    _revalidate_verified_entry(
                        staged_parent,
                        staged_name,
                        staged_descriptor,
                        expected_generation=destination_generation,
                        display_path=candidate.staged,
                    )
                    _charge_directory_entry(
                        maintenance_budget,
                        phase="staging entry cleanup",
                        operation="unlink",
                    )
                    _unlink_anchored_entry(
                        staged_parent,
                        staged_name,
                        display_path=candidate.staged,
                        expected=identity,
                        maintenance_budget=maintenance_budget,
                        mutation_precharged=True,
                        descriptor=staged_descriptor,
                        expected_generation=destination_generation,
                    )
                return "cleaned"


def _indexed_stage_metadata(
    settings: Settings,
    root_descriptor: int,
    candidate: _StagingCandidate,
) -> os.stat_result | None:
    staged_parts, _destination_parts = _candidate_parts(candidate)
    try:
        with _open_relative_parent(
            root_descriptor,
            settings.data_dir,
            staged_parts,
        ) as (parent, name):
            try:
                return os.stat(
                    name,
                    dir_fd=parent,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return None
    except _StagingAncestorMissing:
        return None


def _fresh_fallback_state() -> _FallbackCursorState:
    return _FallbackCursorState(
        next_root=_FALLBACK_ROOTS[0],
        stacks={
            root: [_FallbackFrame(component=None)]
            for root in _FALLBACK_ROOTS
        },
    )


def _fallback_generation(
    metadata: os.stat_result,
) -> tuple[int, int, int, int]:
    if not stat.S_ISDIR(metadata.st_mode):
        raise _StagingUnsafeAncestor(
            "staging fallback path is not a directory"
        )
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _fallback_frame_generation(
    frame: _FallbackFrame,
) -> tuple[int, int, int, int] | None:
    values = (
        frame.device,
        frame.inode,
        frame.mtime_ns,
        frame.ctime_ns,
    )
    if any(value is None for value in values):
        return None
    return values  # type: ignore[return-value]


def _set_fallback_generation(
    frame: _FallbackFrame,
    generation: tuple[int, int, int, int],
) -> None:
    (
        frame.device,
        frame.inode,
        frame.mtime_ns,
        frame.ctime_ns,
    ) = generation


def _reset_fallback_frame(
    frame: _FallbackFrame,
    generation: tuple[int, int, int, int],
) -> None:
    _set_fallback_generation(frame, generation)
    frame.offset = 0
    frame.batch_index = 0
    frame.rescan = False


def _fallback_stack_parts(
    root_name: str,
    stack: list[_FallbackFrame],
) -> tuple[str, ...]:
    return (
        ".staging",
        root_name,
        *(
            frame.component
            for frame in stack[1:]
            if frame.component is not None
        ),
    )


def _encode_fallback_state(state: _FallbackCursorState) -> bytes:
    payload = json.dumps(
        {
            "next_root": state.next_root,
            "next_pass": state.next_pass,
            "roots": {
                root: [
                    {
                        "batch_index": frame.batch_index,
                        "component": frame.component,
                        "ctime_ns": frame.ctime_ns,
                        "device": frame.device,
                        "inode": frame.inode,
                        "mtime_ns": frame.mtime_ns,
                        "offset": frame.offset,
                        "rescan": frame.rescan,
                    }
                    for frame in state.stacks[root]
                ]
                for root in _FALLBACK_ROOTS
            },
            "cleanup_batch_index": state.cleanup_batch_index,
            "cleanup_offset": state.cleanup_offset,
            "turn_generation": state.turn_generation,
            "version": 3,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    if len(payload) > _FALLBACK_CURSOR_MAX_BYTES:
        raise ValueError("staging fallback cursor exceeds the size limit")
    return payload


def _decode_fallback_state(payload: bytes) -> _FallbackCursorState:
    value = json.loads(payload.decode("utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("version") not in {1, 2, 3}
        or value.get("next_root") not in _FALLBACK_ROOTS
        or not isinstance(value.get("roots"), dict)
        or set(value["roots"]) != set(_FALLBACK_ROOTS)
    ):
        raise ValueError("unsupported staging fallback cursor")
    next_pass = (
        value.get("next_pass", "indexed")
        if value.get("version") in {2, 3}
        else "indexed"
    )
    turn_generation = (
        value.get("turn_generation", 0)
        if value.get("version") in {2, 3}
        else 0
    )
    cleanup_offset = (
        value.get("cleanup_offset", 0)
        if value.get("version") == 3
        else 0
    )
    cleanup_batch_index = (
        value.get("cleanup_batch_index", 0)
        if value.get("version") == 3
        else 0
    )
    if next_pass not in {"indexed", "fallback"} or (
        isinstance(turn_generation, bool)
        or not isinstance(turn_generation, int)
        or turn_generation < 0
    ) or (
        isinstance(cleanup_offset, bool)
        or not isinstance(cleanup_offset, int)
        or cleanup_offset < 0
        or cleanup_offset > 2**63 - 1
    ) or (
        isinstance(cleanup_batch_index, bool)
        or not isinstance(cleanup_batch_index, int)
        or cleanup_batch_index < 0
        or cleanup_batch_index > 4096
    ):
        raise ValueError("invalid staging fallback schedule")
    stacks: dict[str, list[_FallbackFrame]] = {}
    for root in _FALLBACK_ROOTS:
        raw_stack = value["roots"][root]
        if (
            not isinstance(raw_stack, list)
            or len(raw_stack) > _FALLBACK_MAX_DEPTH
        ):
            raise ValueError("invalid staging fallback cursor stack")
        stack: list[_FallbackFrame] = []
        for depth, raw_frame in enumerate(raw_stack):
            if not isinstance(raw_frame, dict):
                raise ValueError("invalid staging fallback cursor frame")
            component = raw_frame.get("component")
            if (
                (depth == 0 and component is not None)
                or (
                    depth > 0
                    and (
                        not isinstance(component, str)
                        or component in {"", ".", ".."}
                        or "/" in component
                        or (os.name == "nt" and "\\" in component)
                        or "\x00" in component
                    )
                )
            ):
                raise ValueError("invalid staging fallback cursor path")
            identity = tuple(
                raw_frame.get(field)
                for field in ("device", "inode", "mtime_ns", "ctime_ns")
            )
            if any(
                item is not None
                and (
                    isinstance(item, bool)
                    or not isinstance(item, int)
                    or item < 0
                )
                for item in identity
            ) or (
                any(item is None for item in identity)
                and not all(item is None for item in identity)
            ):
                raise ValueError("invalid staging fallback identity")
            offset = raw_frame.get("offset", 0)
            batch_index = raw_frame.get("batch_index", 0)
            if (
                isinstance(offset, bool)
                or not isinstance(offset, int)
                or offset < 0
                or offset > 2**63 - 1
                or isinstance(batch_index, bool)
                or not isinstance(batch_index, int)
                or batch_index < 0
                or batch_index > 4096
            ):
                raise ValueError("invalid staging fallback position")
            stack.append(
                _FallbackFrame(
                    component=component,
                    device=identity[0],
                    inode=identity[1],
                    mtime_ns=identity[2],
                    ctime_ns=identity[3],
                    offset=offset,
                    batch_index=batch_index,
                    rescan=raw_frame.get("rescan") is True,
                )
            )
        stacks[root] = stack
    return _FallbackCursorState(
        next_root=value["next_root"],
        stacks=stacks,
        next_pass=next_pass,
        turn_generation=turn_generation,
        cleanup_offset=cleanup_offset,
        cleanup_batch_index=cleanup_batch_index,
    )


def _read_fallback_state(
    parent: int,
    *,
    deadline: float | None = None,
    maintenance_budget: MaintenanceBudget | None = None,
) -> tuple[_FallbackCursorState, str | None]:
    fresh = _fresh_fallback_state()
    try:
        _checkpoint_deadline(
            deadline,
            maintenance_budget,
            phase="staging fallback cursor read",
        )
        descriptor = os.open(
            _FALLBACK_CURSOR_NAME,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
    except FileNotFoundError:
        return fresh, None
    except (MaintenanceBudgetExceeded, TimeoutError):
        raise
    except OSError as exc:
        return fresh, f"invalid staging fallback cursor: {exc}"
    try:
        metadata = os.fstat(descriptor)
        current_uid = getattr(os, "geteuid", lambda: metadata.st_uid)()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != current_uid
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > _FALLBACK_CURSOR_MAX_BYTES
        ):
            raise ValueError(
                "staging fallback cursor must be a small owner-only "
                "regular file"
            )
        payload = bytearray()
        while len(payload) <= _FALLBACK_CURSOR_MAX_BYTES:
            _checkpoint_deadline(
                deadline,
                maintenance_budget,
                phase="staging fallback cursor read",
            )
            chunk = os.read(descriptor, 4096)
            _checkpoint_deadline(
                deadline,
                maintenance_budget,
                phase="staging fallback cursor read",
            )
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > _FALLBACK_CURSOR_MAX_BYTES:
            raise ValueError("staging fallback cursor exceeds the size limit")
    except (MaintenanceBudgetExceeded, TimeoutError):
        raise
    except (OSError, ValueError) as exc:
        return fresh, f"invalid staging fallback cursor: {exc}"
    finally:
        os.close(descriptor)
    try:
        return _decode_fallback_state(bytes(payload)), None
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        return fresh, f"invalid staging fallback cursor: {exc}"


def _write_private_cursor(
    parent: int,
    *,
    name: str,
    payload: bytes,
    maintenance_budget: MaintenanceBudget | None = None,
    mutations_precharged: bool = False,
) -> None:
    if not mutations_precharged:
        _reserve_directory_entries(
            maintenance_budget,
            3,
            phase="staging cursor publication",
            operation="mutation",
        )
    temporary = f"{name}.tmp-{uuid.uuid4().hex}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=parent,
    )
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("staging cursor write made no progress")
            offset += written
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(descriptor)
    try:
        os.replace(
            temporary,
            name,
            src_dir_fd=parent,
            dst_dir_fd=parent,
        )
        os.fsync(parent)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        raise


def _write_fallback_state(
    parent: int,
    state: _FallbackCursorState,
    *,
    maintenance_budget: MaintenanceBudget | None = None,
    mutations_precharged: bool = False,
) -> None:
    _write_private_cursor(
        parent,
        name=_FALLBACK_CURSOR_NAME,
        payload=_encode_fallback_state(state),
        maintenance_budget=maintenance_budget,
        mutations_precharged=mutations_precharged,
    )


def _advance_fallback_root(
    settings: Settings,
    root_descriptor: int,
    *,
    root_name: str,
    stack: list[_FallbackFrame],
    budget: int,
    deadline: float,
    excluded: set[tuple[str, ...]],
    maintenance_budget: MaintenanceBudget | None = None,
) -> tuple[list[_FallbackScanItem], int, bool]:
    items: list[_FallbackScanItem] = []
    consumed = 0
    root = settings.data_dir / ".staging" / root_name
    while stack and consumed < budget and time.monotonic() < deadline:
        parts = _fallback_stack_parts(root_name, stack)
        display = settings.data_dir.joinpath(*parts)
        try:
            context = _open_relative_directory(
                root_descriptor,
                settings.data_dir,
                parts,
            )
            directory = context.__enter__()
        except _StagingAncestorMissing:
            stack.pop()
            continue
        except OSError as exc:
            items.append(
                _FallbackScanItem(
                    candidate=None,
                    error=f"could not scan staging directory {display}: {exc}",
                    report_scanned=False,
                )
            )
            stack.pop()
            continue
        try:
            frame = stack[-1]
            initial_generation = _fallback_generation(os.fstat(directory))
            recorded = _fallback_frame_generation(frame)
            if recorded is None or recorded[:2] != initial_generation[:2]:
                _reset_fallback_frame(frame, initial_generation)
            elif recorded != initial_generation:
                frame.rescan = True
                _set_fallback_generation(frame, initial_generation)
            try:
                entries, next_offset, complete = read_directory_batch(
                    directory,
                    frame.offset,
                )
            except (OSError, ValueError) as exc:
                if frame.offset or frame.batch_index:
                    _reset_fallback_frame(frame, initial_generation)
                    continue
                items.append(
                    _FallbackScanItem(
                        candidate=None,
                        error=f"could not scan staging directory {display}: {exc}",
                        report_scanned=False,
                    )
                )
                stack.pop()
                continue
            if frame.batch_index > len(entries):
                _reset_fallback_frame(frame, initial_generation)
                continue
            descended = False
            while (
                frame.batch_index < len(entries)
                and consumed < budget
                and time.monotonic() < deadline
            ):
                name = entries[frame.batch_index]
                _charge_directory_entry(
                    maintenance_budget,
                    phase="staging fallback scan",
                    operation="scan",
                )
                frame.batch_index += 1
                consumed += 1
                entry_parts = (*parts, name)
                path = settings.data_dir.joinpath(*entry_parts)
                if entry_parts in excluded:
                    continue
                try:
                    metadata = os.stat(
                        name,
                        dir_fd=directory,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    items.append(
                        _FallbackScanItem(
                            candidate=None,
                            error=f"could not inspect staging entry {path}: {exc}",
                            report_scanned=True,
                        )
                    )
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    items.append(
                        _FallbackScanItem(
                            candidate=None,
                            error=f"staging symlink preserved: {path}",
                            report_scanned=True,
                        )
                    )
                elif stat.S_ISDIR(metadata.st_mode):
                    if len(stack) >= _FALLBACK_MAX_DEPTH:
                        items.append(
                            _FallbackScanItem(
                                candidate=None,
                                error=(
                                    "staging fallback depth limit reached: "
                                    f"{path}"
                                ),
                                report_scanned=True,
                            )
                        )
                        continue
                    stack.append(
                        _FallbackFrame(
                            component=name,
                            device=metadata.st_dev,
                            inode=metadata.st_ino,
                            mtime_ns=metadata.st_mtime_ns,
                            ctime_ns=metadata.st_ctime_ns,
                        )
                    )
                    descended = True
                    break
                elif stat.S_ISREG(metadata.st_mode):
                    candidate = _mapped_candidate(
                        settings,
                        kind=root_name,
                        root=root,
                        staged=path,
                    )
                    items.append(
                        _FallbackScanItem(
                            candidate=candidate,
                            error=(
                                None
                                if candidate is not None
                                else f"unmapped staging file preserved: {path}"
                            ),
                            report_scanned=True,
                        )
                    )
                else:
                    items.append(
                        _FallbackScanItem(
                            candidate=None,
                            error=f"non-regular staging entry preserved: {path}",
                            report_scanned=True,
                        )
                    )
            if descended:
                continue
            if frame.batch_index == len(entries):
                frame.offset = next_offset
                frame.batch_index = 0
            if complete and not entries:
                final_generation = _fallback_generation(os.fstat(directory))
                if final_generation != initial_generation:
                    frame.rescan = True
                _set_fallback_generation(frame, final_generation)
                if frame.rescan:
                    _reset_fallback_frame(frame, final_generation)
                else:
                    stack.pop()
        finally:
            context.__exit__(None, None, None)
    return items, consumed, not stack


def _scan_staging(
    settings: Settings,
    root_descriptor: int,
    *,
    max_entries: int,
    deadline: float,
    excluded: set[tuple[str, ...]],
    maintenance_budget: MaintenanceBudget | None = None,
    turn_generation: int | None = None,
) -> _FallbackScanResult:
    control_missing = _relative_entry_missing(
        root_descriptor,
        ".staging",
    )
    cursor_precharged = maintenance_budget is not None
    if cursor_precharged:
        _reserve_directory_entries(
            maintenance_budget,
            3 + int(control_missing),
            phase="staging fallback cursor publication",
            operation="mutation",
        )
    with _open_relative_directory(
        root_descriptor,
        settings.data_dir,
        (".staging",),
        create=control_missing,
        creation_precharged=cursor_precharged and control_missing,
    ) as parent:
        _secure_staging_control_directory(
            parent,
            maintenance_budget=maintenance_budget,
        )
        state, cursor_error = _read_fallback_state(
            parent,
            deadline=deadline,
            maintenance_budget=maintenance_budget,
        )
        items: list[_FallbackScanItem] = []
        if cursor_error is not None:
            items.append(
                _FallbackScanItem(
                    candidate=None,
                    error=cursor_error,
                    report_scanned=False,
                )
            )
        # A completed root is re-armed for the next bounded slice. Root
        # directory metadata cannot reveal additions in an already existing
        # deep descendant, so retaining an empty stack until the other root
        # finishes would let a continuously large tree starve new staging
        # entries indefinitely.
        for root in _FALLBACK_ROOTS:
            if not state.stacks[root]:
                state.stacks[root] = [_FallbackFrame(component=None)]
        cleanup_result = _CursorCleanupResult(
            errors=(),
            consumed=0,
            truncated=False,
        )
        consumed = 0
        completed: set[str] = set()
        budget_error: MaintenanceBudgetExceeded | None = None
        try:
            while (
                consumed < max_entries
                and len(completed) < len(_FALLBACK_ROOTS)
                and time.monotonic() < deadline
            ):
                index = _FALLBACK_ROOTS.index(state.next_root)
                selected: str | None = None
                for _ in _FALLBACK_ROOTS:
                    root_name = _FALLBACK_ROOTS[index]
                    state.next_root = _FALLBACK_ROOTS[
                        (index + 1) % len(_FALLBACK_ROOTS)
                    ]
                    index = _FALLBACK_ROOTS.index(state.next_root)
                    if root_name not in completed:
                        selected = root_name
                        break
                if selected is None:
                    break
                root_items, root_consumed, root_complete = (
                    _advance_fallback_root(
                        settings,
                        root_descriptor,
                        root_name=selected,
                        stack=state.stacks[selected],
                        budget=min(
                            32,
                            max_entries - consumed,
                        ),
                        deadline=deadline,
                        excluded=excluded,
                        maintenance_budget=maintenance_budget,
                    )
                )
                items.extend(root_items)
                consumed += root_consumed
                if root_complete:
                    completed.add(selected)
                if root_consumed == 0 and not root_complete:
                    break
        except MaintenanceBudgetExceeded as exc:
            budget_error = exc
        cleanup_limit = max_entries - consumed
        if (
            budget_error is None
            and cleanup_limit > 0
            and time.monotonic() < deadline
            and (
                maintenance_budget is None
                or getattr(
                    maintenance_budget,
                    "_remaining_directory_entries",
                    0,
                )
                > 0
            )
        ):
            try:
                cleanup_result = _cleanup_cursor_temporaries(
                    settings,
                    parent,
                    state,
                    max_entries=min(
                        cleanup_limit,
                        _CURSOR_TEMP_CLEANUP_MAX_ENTRIES,
                    ),
                    deadline=deadline,
                    maintenance_budget=maintenance_budget,
                )
                consumed += cleanup_result.consumed
                items.extend(
                    _FallbackScanItem(
                        candidate=None,
                        error=error,
                        report_scanned=False,
                    )
                    for error in cleanup_result.errors
                )
            except MaintenanceBudgetExceeded:
                raise
        roots_complete = len(completed) == len(_FALLBACK_ROOTS)
        if roots_complete:
            fresh = _fresh_fallback_state()
            state.next_root = fresh.next_root
            state.stacks = fresh.stacks
        if turn_generation is not None:
            state.next_pass = (
                "fallback"
                if budget_error is not None and consumed == 0
                else "indexed"
            )
            state.turn_generation = turn_generation
        cursor_write_failed = False
        try:
            _write_fallback_state(
                parent,
                state,
                maintenance_budget=maintenance_budget,
                mutations_precharged=cursor_precharged,
            )
        except (OSError, ValueError) as exc:
            cursor_write_failed = True
            items.append(
                _FallbackScanItem(
                    candidate=None,
                    error=(
                        "could not persist staging fallback cursor "
                        f"{settings.data_dir / '.staging' / _FALLBACK_CURSOR_NAME}: "
                        f"{exc}"
                    ),
                    report_scanned=False,
                )
            )
    return _FallbackScanResult(
        items=tuple(items),
        consumed=consumed,
        truncated=(
            not roots_complete
            or cleanup_result.truncated
            or cursor_write_failed
            or budget_error is not None
            or time.monotonic() >= deadline
        ),
        budget_error=budget_error,
    )


def _cursor_path(settings: Settings) -> Path:
    return settings.data_dir / ".staging" / _INDEX_CURSOR_NAME


def _secure_staging_control_directory(
    descriptor: int,
    *,
    maintenance_budget: MaintenanceBudget | None = None,
) -> None:
    metadata = os.fstat(descriptor)
    current_uid = getattr(os, "geteuid", lambda: metadata.st_uid)()
    if metadata.st_uid != current_uid:
        raise _StagingUnsafeAncestor(
            "staging control directory is not user-owned"
        )
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        os.fchmod(descriptor, 0o700)
        os.fsync(descriptor)


def _read_index_cursor(
    settings: Settings,
    root_descriptor: int,
    *,
    deadline: float | None = None,
    maintenance_budget: MaintenanceBudget | None = None,
) -> tuple[_IndexCursorState, str | None]:
    fresh = _IndexCursorState(
        storage_object_id=None,
        next_pass="indexed",
    )
    try:
        _checkpoint_deadline(
            deadline,
            maintenance_budget,
            phase="staging index cursor read",
        )
        with _open_relative_directory(
            root_descriptor,
            settings.data_dir,
            (".staging",),
        ) as parent:
            _secure_staging_control_directory(parent)
            descriptor = os.open(
                _INDEX_CURSOR_NAME,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            try:
                metadata = os.fstat(descriptor)
                current_uid = getattr(
                    os,
                    "geteuid",
                    lambda: metadata.st_uid,
                )()
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != current_uid
                    or stat.S_IMODE(metadata.st_mode) & 0o077
                    or metadata.st_size > _INDEX_CURSOR_MAX_BYTES
                ):
                    raise ValueError(
                        "staging index cursor must be a small owner-only "
                        "regular file"
                    )
                payload = bytearray()
                while len(payload) <= _INDEX_CURSOR_MAX_BYTES:
                    _checkpoint_deadline(
                        deadline,
                        maintenance_budget,
                        phase="staging index cursor read",
                    )
                    chunk = os.read(descriptor, 4096)
                    _checkpoint_deadline(
                        deadline,
                        maintenance_budget,
                        phase="staging index cursor read",
                    )
                    if not chunk:
                        break
                    payload.extend(chunk)
                if len(payload) > _INDEX_CURSOR_MAX_BYTES:
                    raise ValueError(
                        "staging index cursor exceeds the size limit"
                    )
            finally:
                os.close(descriptor)
    except (_StagingAncestorMissing, FileNotFoundError):
        return fresh, None
    except (MaintenanceBudgetExceeded, TimeoutError):
        raise
    except (OSError, ValueError) as exc:
        return fresh, f"invalid staging index cursor: {exc}"
    try:
        value = json.loads(payload)
        if (
            not isinstance(value, dict)
            or value.get("version") not in {1, 2, 3}
        ):
            raise ValueError("unsupported staging index cursor")
        raw_object_id = value.get("storage_object_id")
        cursor = (
            None
            if raw_object_id is None
            else uuid.UUID(str(raw_object_id))
        )
        next_pass = (
            value.get("next_pass", "indexed")
            if value.get("version") in {2, 3}
            else "indexed"
        )
        if next_pass not in {"indexed", "fallback"}:
            raise ValueError("unsupported staging cursor pass")
        turn_generation = (
            value.get("turn_generation", 0)
            if value.get("version") == 3
            else 0
        )
        if (
            isinstance(turn_generation, bool)
            or not isinstance(turn_generation, int)
            or turn_generation < 0
        ):
            raise ValueError("invalid staging cursor generation")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return fresh, f"invalid staging index cursor: {exc}"
    return (
        _IndexCursorState(
            storage_object_id=cursor,
            next_pass=next_pass,
            turn_generation=turn_generation,
        ),
        None,
    )


def _write_index_cursor(
    settings: Settings,
    root_descriptor: int,
    cursor: _IndexCursorState,
    *,
    maintenance_budget: MaintenanceBudget | None = None,
    mutations_precharged: bool = False,
    create_control: bool | None = None,
) -> None:
    payload = json.dumps(
        {
            "next_pass": cursor.next_pass,
            "storage_object_id": (
                str(cursor.storage_object_id)
                if cursor.storage_object_id is not None
                else None
            ),
            "turn_generation": cursor.turn_generation,
            "version": 3,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    if create_control is None:
        create_control = _relative_entry_missing(
            root_descriptor,
            ".staging",
        )
    if not mutations_precharged:
        _reserve_directory_entries(
            maintenance_budget,
            3 + int(create_control),
            phase="staging index cursor publication",
            operation="mutation",
        )
    with _open_relative_directory(
        root_descriptor,
        settings.data_dir,
        (".staging",),
        create=create_control,
        creation_precharged=mutations_precharged and create_control,
    ) as parent:
        _secure_staging_control_directory(
            parent,
            maintenance_budget=maintenance_budget,
        )
        _write_private_cursor(
            parent,
            name=_INDEX_CURSOR_NAME,
            payload=payload,
            maintenance_budget=maintenance_budget,
            mutations_precharged=True,
        )


def _read_fallback_cursor(
    settings: Settings,
    root_descriptor: int,
    *,
    deadline: float,
    maintenance_budget: MaintenanceBudget | None = None,
) -> tuple[_FallbackCursorState, str | None]:
    try:
        with _open_relative_directory(
            root_descriptor,
            settings.data_dir,
            (".staging",),
        ) as parent:
            _secure_staging_control_directory(parent)
            return _read_fallback_state(
                parent,
                deadline=deadline,
                maintenance_budget=maintenance_budget,
            )
    except (_StagingAncestorMissing, _StagingUnsafeAncestor):
        return _fresh_fallback_state(), None


def _latest_staging_schedule(
    index_state: _IndexCursorState,
    fallback_state: _FallbackCursorState,
) -> tuple[str, int]:
    if fallback_state.turn_generation > index_state.turn_generation:
        return fallback_state.next_pass, fallback_state.turn_generation
    return index_state.next_pass, index_state.turn_generation


def _cleanup_cursor_temporaries(
    settings: Settings,
    parent: int,
    state: _FallbackCursorState,
    *,
    max_entries: int,
    deadline: float,
    maintenance_budget: MaintenanceBudget | None = None,
) -> _CursorCleanupResult:
    errors: list[str] = []
    try:
        _secure_staging_control_directory(parent)
        offset = state.cleanup_offset
        batch_index = state.cleanup_batch_index
        scanned = 0
        limit = min(max_entries, _CURSOR_TEMP_CLEANUP_MAX_ENTRIES)
        complete = False
        while scanned < limit:
            _checkpoint_deadline(
                deadline,
                maintenance_budget,
                phase="staging cursor temporary cleanup",
            )
            names, next_offset, complete = read_directory_batch(
                parent,
                offset,
            )
            if batch_index > len(names):
                offset = 0
                batch_index = 0
                continue
            while batch_index < len(names) and scanned < limit:
                name = names[batch_index]
                _charge_directory_entry(
                    maintenance_budget,
                    phase="staging cursor temporary cleanup",
                    operation="scan",
                )
                error = _clean_cursor_temporary_entry(
                    settings,
                    parent,
                    name,
                    maintenance_budget=maintenance_budget,
                )
                if error is not None:
                    errors.append(error)
                batch_index += 1
                scanned += 1
                state.cleanup_offset = offset
                state.cleanup_batch_index = batch_index
            if batch_index == len(names):
                offset = next_offset
                batch_index = 0
                state.cleanup_offset = offset
                state.cleanup_batch_index = 0
            if complete and not names:
                break
        state.cleanup_offset = 0 if complete else offset
        state.cleanup_batch_index = 0 if complete else batch_index
    except _StagingAncestorMissing:
        return _CursorCleanupResult(errors=(), consumed=0, truncated=False)
    return _CursorCleanupResult(
        errors=tuple(errors),
        consumed=scanned,
        truncated=not complete,
    )


def _indexed_staging_candidates(
    session: Session,
    settings: Settings,
    *,
    limit: int,
    cursor: uuid.UUID | None,
):
    """Yield one bounded keyset page of exact DB-derived staging paths."""
    filters = (
        StorageObject.purged_at.is_(None),
        StorageObject.sha256.is_not(None),
        StorageObject.data_class.in_(
            (
                "media",
                "nutrition_media",
                "raw_payload",
                "nutrition_raw_capture",
            )
        ),
    )
    statement = select(
        StorageObject.id,
        StorageObject.relative_path,
    ).where(*filters)
    if cursor is not None:
        statement = statement.where(StorageObject.id > cursor)
    statement = (
        statement
        .order_by(StorageObject.id)
        .limit(limit)
    )
    rows = list(session.execute(statement))
    if cursor is not None and len(rows) < limit:
        rows.extend(
            session.execute(
                select(StorageObject.id, StorageObject.relative_path)
                .where(
                    *filters,
                    StorageObject.id <= cursor,
                )
                .order_by(StorageObject.id)
                .limit(limit - len(rows))
            )
        )
    for object_id, relative_path in rows:
        yield object_id, _indexed_candidate(settings, relative_path)


def _reconcile_staging_session(
    session: Session,
    settings: Settings,
    *,
    max_entries: int,
    max_seconds: float,
    maintenance_budget: MaintenanceBudget | None = None,
) -> StagingReconciliationReport:
    started_at = time.monotonic()
    deadline = min(
        started_at + max_seconds,
        (
            maintenance_budget.deadline
            if maintenance_budget is not None
            else float("inf")
        ),
    )
    unlink_deadline = min(
        started_at + (max_seconds / 2),
        deadline,
    )
    unlink_report = DurableUnlinkRecoveryReport(
        scanned=0,
        restored=0,
        cleaned=0,
        unresolved=0,
        truncated=False,
        errors=(),
    )
    budget_used = 0
    scanned = 0
    cleaned = 0
    restored = 0
    unresolved = 0
    truncated = False
    errors: list[str] = []
    processed_paths: set[tuple[str, ...]] = set()
    last_indexed_object_id: uuid.UUID | None = None
    cursor_state = _IndexCursorState(
        storage_object_id=None,
        next_pass="indexed",
    )
    fallback_state = _fresh_fallback_state()
    scheduled_pass = "indexed"
    schedule_generation = 0
    indexed_budget = 0
    fallback_budget = 0
    maintenance_budget_exhausted = False
    maintenance_budget_error: MaintenanceBudgetExceeded | None = None

    def record_budget_error(exc: MaintenanceBudgetExceeded) -> None:
        nonlocal maintenance_budget_error
        nonlocal maintenance_budget_exhausted
        nonlocal truncated
        if maintenance_budget_error is None:
            maintenance_budget_error = exc
            errors.append(str(exc))
        maintenance_budget_exhausted = True
        truncated = True

    def build_report() -> StagingReconciliationReport:
        return StagingReconciliationReport(
            scanned=scanned,
            cleaned=cleaned,
            restored=restored,
            unlink_quarantines_scanned=unlink_report.scanned,
            unlink_quarantines_cleaned=unlink_report.cleaned,
            unresolved=unresolved,
            truncated=truncated,
            errors=tuple(errors),
            budget_exhausted=maintenance_budget_exhausted,
        )

    def apply_candidate(
        root_descriptor: int,
        candidate: _StagingCandidate,
    ) -> bool:
        nonlocal cleaned, restored, unresolved
        processed_paths.add(_candidate_parts(candidate)[0])
        try:
            outcome = _reconcile_candidate(
                session,
                settings,
                root_descriptor,
                candidate,
                deadline=deadline,
                maintenance_budget=maintenance_budget,
            )
        except MaintenanceBudgetExceeded as exc:
            record_budget_error(exc)
            return False
        except TimeoutError:
            return False
        except (OSError, ValueError) as exc:
            unresolved += 1
            errors.append(f"{candidate.staged}: {exc}")
            return True
        if outcome == "cleaned":
            cleaned += 1
        elif outcome == "restored":
            restored += 1
        else:
            unresolved += 1
            errors.append(
                f"staging file has no exact committed index: {candidate.staged}"
            )
        return True

    if maintenance_budget is not None:
        try:
            maintenance_budget.checkpoint(
                phase="staging reconciliation",
            )
        except MaintenanceBudgetExceeded as exc:
            record_budget_error(exc)

    # Self-describing durable-unlink journals have their own bounded recovery
    # queue and report fields. Recover them before the index pass so a full
    # StorageObject page cannot starve crash-left deletion journals. The
    # caller's wall-clock deadline and optional lifecycle budget are shared.
    if (
        not maintenance_budget_exhausted
        and time.monotonic() < deadline
    ):
        try:
            unlink_report = recover_durable_unlink_quarantines(
                settings.data_dir,
                max_entries=max_entries,
                max_seconds=max_seconds,
                deadline=unlink_deadline,
                budget=maintenance_budget,
            )
        except MaintenanceBudgetExceeded as exc:
            record_budget_error(exc)
            unlink_report = DurableUnlinkRecoveryReport(
                scanned=0,
                restored=0,
                cleaned=0,
                unresolved=0,
                truncated=True,
                errors=(),
                budget_exhausted=True,
            )
        maintenance_budget_exhausted = unlink_report.budget_exhausted
        unresolved += unlink_report.unresolved
        truncated = truncated or unlink_report.truncated
        errors.extend(unlink_report.errors)
    else:
        truncated = True

    # Exact DB-derived paths are cheap and actionable. Process them before any
    # fallback tree walk so unrelated junk cannot starve a committed staged
    # payload.
    if (
        not maintenance_budget_exhausted
        and time.monotonic() < deadline
    ):
        try:
            root_context = _open_data_root(settings)
            root_descriptor = root_context.__enter__()
        except OSError as exc:
            unresolved += 1
            errors.append(f"could not open storage root for staging: {exc}")
            root_context = None
        if root_context is not None:
            try:
                try:
                    cursor_state, cursor_error = _read_index_cursor(
                        settings,
                        root_descriptor,
                        deadline=deadline,
                        maintenance_budget=maintenance_budget,
                    )
                    fallback_state, fallback_cursor_error = (
                        _read_fallback_cursor(
                            settings,
                            root_descriptor,
                            deadline=deadline,
                            maintenance_budget=maintenance_budget,
                        )
                    )
                except MaintenanceBudgetExceeded as exc:
                    record_budget_error(exc)
                    return build_report()
                except TimeoutError:
                    truncated = True
                    return build_report()
                if cursor_error is not None:
                    errors.append(cursor_error)
                if fallback_cursor_error is not None:
                    errors.append(fallback_cursor_error)
                scheduled_pass, schedule_generation = (
                    _latest_staging_schedule(
                        cursor_state,
                        fallback_state,
                    )
                )
                index_control_missing = _relative_entry_missing(
                    root_descriptor,
                    ".staging",
                )
                index_cursor_precharged = False
                if max_entries == 1:
                    indexed_budget = (
                        1 if scheduled_pass == "indexed" else 0
                    )
                    fallback_budget = 1 - indexed_budget
                elif (
                    scheduled_pass == "fallback"
                    and maintenance_budget is not None
                ):
                    # A prior run that completed the indexed cursor but ran
                    # out of the shared lifecycle budget must let fallback
                    # recovery go first on the next run. Otherwise the index
                    # cursor publication can consume the same small budget
                    # forever and starve the filesystem queue.
                    indexed_budget = 0
                    fallback_budget = max_entries
                else:
                    fallback_budget = max(1, (max_entries + 3) // 4)
                    indexed_budget = max_entries - fallback_budget
                if maintenance_budget is not None and indexed_budget:
                    try:
                        _reserve_directory_entries(
                            maintenance_budget,
                            3 + int(index_control_missing),
                            phase="staging index cursor publication",
                            operation="mutation",
                        )
                    except MaintenanceBudgetExceeded as exc:
                        record_budget_error(exc)
                    else:
                        index_cursor_precharged = True
                if indexed_budget == 0:
                    # The other pass owns this one-entry maintenance slice.
                    # We cannot claim that the skipped index was exhausted.
                    truncated = True
                if indexed_budget and not maintenance_budget_exhausted:
                    indexed_candidates = _indexed_staging_candidates(
                        session,
                        settings,
                        limit=indexed_budget + 1,
                        cursor=cursor_state.storage_object_id,
                    )
                    for object_id, candidate in indexed_candidates:
                        if (
                            budget_used >= indexed_budget
                            or time.monotonic() >= deadline
                        ):
                            truncated = True
                            break
                        try:
                            _charge_directory_entry(
                                maintenance_budget,
                                phase="staging indexed scan",
                                operation="scan",
                            )
                        except MaintenanceBudgetExceeded as exc:
                            record_budget_error(exc)
                            break
                        budget_used += 1
                        scanned += 1
                        if candidate is None:
                            last_indexed_object_id = object_id
                            continue
                        try:
                            metadata = _indexed_stage_metadata(
                                settings,
                                root_descriptor,
                                candidate,
                            )
                        except OSError as exc:
                            processed_paths.add(
                                _candidate_parts(candidate)[0]
                            )
                            unresolved += 1
                            errors.append(
                                "could not inspect indexed staging entry "
                                f"{candidate.staged}: {exc}"
                            )
                            continue
                        if metadata is None:
                            last_indexed_object_id = object_id
                            continue
                        if stat.S_ISLNK(
                            metadata.st_mode
                        ) or not stat.S_ISREG(metadata.st_mode):
                            processed_paths.add(
                                _candidate_parts(candidate)[0]
                            )
                            unresolved += 1
                            errors.append(
                                "indexed staging entry is not a regular file: "
                                f"{candidate.staged}"
                            )
                            last_indexed_object_id = object_id
                            continue
                        if not apply_candidate(root_descriptor, candidate):
                            truncated = True
                            break
                        last_indexed_object_id = object_id
                if (
                    indexed_budget
                    and (
                        maintenance_budget is None
                        or index_cursor_precharged
                    )
                ):
                    next_cursor = _IndexCursorState(
                        storage_object_id=(
                            last_indexed_object_id
                            or cursor_state.storage_object_id
                        ),
                        next_pass="fallback",
                        turn_generation=schedule_generation + 1,
                    )
                    try:
                        _write_index_cursor(
                            settings,
                            root_descriptor,
                            next_cursor,
                            maintenance_budget=maintenance_budget,
                            mutations_precharged=index_cursor_precharged,
                            create_control=index_control_missing,
                        )
                    except MaintenanceBudgetExceeded as exc:
                        record_budget_error(exc)
                    except _StagingUnsafeAncestor:
                        if cursor_error is None or budget_used:
                            raise
                        # The fallback scan below owns reporting an inaccessible
                        # staging namespace when no indexed progress was made.
                    except OSError as exc:
                        raise OSError(
                            "could not persist staging index cursor "
                            f"{_cursor_path(settings)}: {exc}"
                        ) from exc
            finally:
                root_context.__exit__(None, None, None)
    else:
        truncated = True

    # Indexed staging aliases and the fallback tree walk share their entry
    # budget. Durable-unlink recovery keeps a separate queue/report while
    # consuming the same optional lifecycle resource budget.
    if max_entries == 1:
        remaining = fallback_budget
    else:
        # Preserve a fallback reservation even under a full index page, but
        # lend any unused indexed capacity to the tree walk.
        remaining = fallback_budget + max(
            0,
            indexed_budget - budget_used,
        )
    if (
        not maintenance_budget_exhausted
        and
        remaining > 0
        and time.monotonic() < deadline
    ):
        try:
            root_context = _open_data_root(settings)
            root_descriptor = root_context.__enter__()
        except OSError as exc:
            unresolved += 1
            errors.append(f"could not open storage root for staging: {exc}")
            root_context = None
        if root_context is not None:
            try:
                try:
                    scan = _scan_staging(
                        settings,
                        root_descriptor,
                        max_entries=remaining,
                        deadline=deadline,
                        excluded=processed_paths,
                        maintenance_budget=maintenance_budget,
                        turn_generation=(
                            schedule_generation
                            + (2 if indexed_budget else 1)
                        ),
                    )
                except MaintenanceBudgetExceeded as exc:
                    record_budget_error(exc)
                    scan = None
                if scan is not None:
                    if scan.budget_error is not None:
                        record_budget_error(scan.budget_error)
                    budget_used += scan.consumed
                    truncated = truncated or scan.truncated
                    for item in scan.items:
                        if item.report_scanned:
                            scanned += 1
                        if item.error is not None:
                            unresolved += 1
                            errors.append(item.error)
                            continue
                        if (
                            item.candidate is not None
                            and not maintenance_budget_exhausted
                        ):
                            if not apply_candidate(
                                root_descriptor,
                                item.candidate,
                            ):
                                truncated = True
                                break
            except (OSError, ValueError) as exc:
                truncated = True
                unresolved += 1
                errors.append(
                    "could not scan staging fallback namespace "
                    f"{settings.data_dir / '.staging'}: "
                    f"{exc}"
                )
            finally:
                root_context.__exit__(None, None, None)
    elif (
        maintenance_budget_exhausted
        or remaining <= 0
        or time.monotonic() >= deadline
    ):
        truncated = True
    return build_report()


def reconcile_staging_files(
    bind,
    settings: Settings,
    *,
    max_entries: int = DEFAULT_STAGING_RECONCILE_MAX_ENTRIES,
    max_seconds: float = DEFAULT_STAGING_RECONCILE_MAX_SECONDS,
    maintenance_budget: MaintenanceBudget | None = None,
) -> StagingReconciliationReport:
    """Repair only staging artifacts proven by the committed storage index."""
    if (
        isinstance(max_entries, bool)
        or not isinstance(max_entries, int)
        or max_entries <= 0
    ):
        raise ValueError("max_entries must be a positive integer")
    if isinstance(max_seconds, bool):
        raise ValueError("max_seconds must be a finite positive number")
    try:
        bounded_seconds = float(max_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "max_seconds must be a finite positive number"
        ) from exc
    if not math.isfinite(bounded_seconds) or bounded_seconds <= 0:
        raise ValueError("max_seconds must be a finite positive number")
    with global_write_plane_guard(bind) as guard_connection:
        reader_bind = guard_connection if guard_connection is not None else bind
        with Session(bind=reader_bind) as session:
            return _reconcile_staging_session(
                session,
                settings,
                max_entries=max_entries,
                max_seconds=bounded_seconds,
                maintenance_budget=maintenance_budget,
            )
