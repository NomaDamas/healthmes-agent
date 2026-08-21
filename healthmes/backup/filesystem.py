"""Symlink-safe, power-loss-aware filesystem publication primitives."""

from __future__ import annotations

import os
import stat
import sys
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from healthmes.backup.provider import BackupPublicationError
from healthmes.durable_files import (
    ensure_durable_directory,
    open_directory_anchored,
    require_directory_entry_durability,
)


@dataclass(frozen=True, slots=True)
class RegularFileIdentity:
    """Stable identity fields for one already-open regular file."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_metadata(cls, metadata: os.stat_result) -> RegularFileIdentity:
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("file identity requires a regular file")
        return cls(
            device=metadata.st_dev,
            inode=metadata.st_ino,
            size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
            ctime_ns=metadata.st_ctime_ns,
        )

    @classmethod
    def from_descriptor(cls, descriptor: int) -> RegularFileIdentity:
        return cls.from_metadata(os.fstat(descriptor))

    def matches(self, metadata: os.stat_result) -> bool:
        """Whether ``metadata`` still names this exact regular file state."""
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_dev == self.device
            and metadata.st_ino == self.inode
            and metadata.st_size == self.size
            and metadata.st_mtime_ns == self.mtime_ns
            and metadata.st_ctime_ns == self.ctime_ns
        )


@dataclass(slots=True)
class PinnedPublishedFile:
    """One successfully published generation retained by an open descriptor."""

    handle: BinaryIO | None = None
    identity: RegularFileIdentity | None = None

    def close(self) -> None:
        if self.handle is None:
            return
        self.handle.close()
        self.handle = None
        self.identity = None

    def __enter__(self) -> PinnedPublishedFile:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


def fsync_directory(path: Path) -> None:
    """Persist directory entry changes or fail when they cannot be proven."""
    require_directory_entry_durability()
    with open_directory_anchored(path) as (_canonical, descriptor):
        os.fsync(descriptor)


@contextmanager
def open_regular_file(path: Path) -> Iterator[BinaryIO]:
    """Open one regular-file generation through an anchored parent."""
    candidate = Path(path).expanduser()
    if candidate.name in {"", ".", ".."}:
        raise OSError(f"invalid file path: {candidate}")
    if os.name == "nt":  # pragma: no cover - exercised on Windows runners
        if candidate.is_symlink():
            raise OSError(f"file must not be a symlink: {candidate}")
        descriptor = os.open(
            candidate,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError(f"file must be regular: {candidate}")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                yield handle
        finally:
            os.close(descriptor)
        return

    with open_directory_anchored(candidate.parent) as (
        _canonical_parent,
        parent_descriptor,
    ):
        descriptor = os.open(
            candidate.name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        try:
            identity = RegularFileIdentity.from_descriptor(descriptor)
            named = os.stat(
                candidate.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if not identity.matches(named):
                raise OSError(
                    f"file path changed while it was being opened: {candidate}"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                yield handle
        finally:
            os.close(descriptor)


@contextmanager
def durable_atomic_writer(
    destination: Path,
    *,
    mode: int = 0o600,
    replace_existing: bool = True,
    pinned: PinnedPublishedFile | None = None,
) -> Iterator[BinaryIO]:
    """Write a unique temporary regular file and durably publish it atomically.

    The temporary name is unguessable and opened with exclusive no-follow
    semantics. On POSIX, publication and cleanup are relative to an already
    opened parent directory descriptor so a raced path replacement cannot
    redirect the write. ``replace_existing=False`` uses no-clobber publication
    so a destination created while the caller is writing is never overwritten.
    """
    requested = Path(destination).expanduser()
    if pinned is not None and (
        pinned.handle is not None or pinned.identity is not None
    ):
        raise ValueError("published file pin is already in use")
    ensure_durable_directory(requested.parent)
    final_name = requested.name
    if final_name in {"", ".", ".."}:
        raise OSError(f"invalid destination filename: {requested}")

    stack = ExitStack()
    if os.name == "nt":  # pragma: no cover - exercised on Windows runners
        parent = requested.parent.resolve(strict=True)
        parent_descriptor: int | None = None
    else:
        parent, parent_descriptor = stack.enter_context(
            open_directory_anchored(requested.parent)
        )
    temporary_name = f".{final_name}.{uuid.uuid4().hex}.tmp"
    temporary_path = parent / temporary_name
    final_path = parent / final_name
    descriptor: int | None = None
    handle: BinaryIO | None = None
    retained_handle: BinaryIO | None = None
    published = False
    destination_created = False
    try:
        flags = (
            (os.O_RDWR if pinned is not None else os.O_WRONLY)
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        if os.name == "nt":  # pragma: no cover - exercised on Windows runners
            descriptor = os.open(temporary_path, flags, mode)
        else:
            descriptor = os.open(
                temporary_name,
                flags,
                mode,
                dir_fd=parent_descriptor,
            )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"temporary output must be regular: {temporary_path}")
        handle = os.fdopen(descriptor, "wb")
        descriptor = None
        yield handle
        handle.flush()
        os.fsync(handle.fileno())
        if pinned is not None:
            retained_handle = os.fdopen(
                os.dup(handle.fileno()),
                "rb",
            )
        handle.close()
        handle = None
        if parent_descriptor is not None:
            opened_parent = os.fstat(parent_descriptor)
            named_parent = os.stat(requested.parent, follow_symlinks=True)
            if (
                not stat.S_ISDIR(named_parent.st_mode)
                or named_parent.st_dev != opened_parent.st_dev
                or named_parent.st_ino != opened_parent.st_ino
            ):
                raise OSError(
                    f"destination directory changed while writing: {requested.parent}"
                )
        if parent_descriptor is None:
            if replace_existing:
                os.replace(temporary_path, final_path)
            else:  # os.rename is no-clobber on Windows.
                os.rename(temporary_path, final_path)
            destination_created = True
            published = True
            fsync_directory(parent)
        elif replace_existing:
            os.replace(
                temporary_name,
                final_name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            destination_created = True
            published = True
            os.fsync(parent_descriptor)
        else:
            os.link(
                temporary_name,
                final_name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            destination_created = True
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            except OSError:
                try:
                    os.unlink(temporary_name, dir_fd=parent_descriptor)
                except FileNotFoundError:
                    pass
            os.fsync(parent_descriptor)
            published = True
        if retained_handle is not None:
            retained_identity = RegularFileIdentity.from_descriptor(
                retained_handle.fileno()
            )
            named = (
                os.stat(final_path, follow_symlinks=False)
                if parent_descriptor is None
                else os.stat(
                    final_name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            )
            if not retained_identity.matches(named):
                raise OSError(
                    f"published file changed before it could be pinned: "
                    f"{final_path}"
                )
            retained_handle.seek(0)
            assert pinned is not None
            pinned.handle = retained_handle
            pinned.identity = retained_identity
            retained_handle = None
    except OSError as exc:
        if destination_created:
            raise BackupPublicationError(
                f"backup destination was created but durability could not be "
                f"confirmed: {final_path}: {exc}",
                destination_created=True,
            ) from exc
        raise
    finally:
        active_error = sys.exception()
        cleanup_errors: list[Exception] = []
        try:
            if handle is not None:
                handle.close()
        except Exception as exc:
            cleanup_errors.append(exc)
        try:
            if descriptor is not None:
                os.close(descriptor)
        except Exception as exc:
            cleanup_errors.append(exc)
        try:
            if retained_handle is not None:
                retained_handle.close()
        except Exception as exc:
            cleanup_errors.append(exc)
        if not published:
            try:
                if parent_descriptor is None:
                    temporary_path.unlink(missing_ok=True)
                else:
                    os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            except Exception as exc:
                cleanup_errors.append(exc)
        try:
            stack.close()
        except Exception as exc:
            cleanup_errors.append(exc)
        if cleanup_errors:
            if active_error is None:
                raise cleanup_errors[0]
            for cleanup_error in cleanup_errors:
                active_error.add_note(
                    "suppressed backup publication cleanup failure: "
                    f"{type(cleanup_error).__name__}"
                )
