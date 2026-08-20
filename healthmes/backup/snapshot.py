"""Versioned, encrypted snapshot envelope (docs/PLAN.md section 9).

Envelope layout — a gzip'd tar, then age-encrypted with an scrypt passphrase
recipient (pyrage). Inside the tar::

    manifest.json                 schema version, caller-injected timestamp,
                                  content inventory (path/size/sha256)
    db/healthmes.sqlite3          sqlite3.Connection.backup snapshot
                                  (sqlite database_url; consistent even while
                                  the in-process jobs keep writing)
    db/healthmes.dump             pg_dump -Fc       (postgres database_url)
    db/open_wearables.dump        pg_dump -Fc       (optional, when the
                                  open-wearables database URL is configured)
    media/**                      HEALTHMES_DATA_DIR/media tree
    raw_ingest/**                 HEALTHMES_DATA_DIR/raw_ingest tree
    hermes/**                     HERMES_HOME memory/state (when configured)

Design points:

- **Timestamps are injected by the caller** (``created_at``), never read from
  the clock here — providers own naming/clocking, tests freeze it.
- **pg_dump / pg_restore discovery**: ``shutil.which`` first, then the
  Homebrew keg fallback (``brew --prefix postgresql@16`` etc.) because macOS
  keeps keg-only postgres binaries off PATH.
- **Symlinks**: links that stay inside the copied tree are preserved as
  links; links escaping the tree (e.g. legacy ``$HERMES_HOME/skills/*``
  symlinks left by pre-copy-install bootstraps, re-creatable by
  ``scripts/bootstrap.py``) are recorded in the manifest and skipped — the
  archive never references paths outside itself, so extraction is safe
  under ``tarfile``'s ``data`` filter.
- **Restore verifies before it writes**: the archive is extracted to a
  scratch directory and checked against the manifest inventory (SHA-256)
  before any live target is replaced.
- The whole envelope passes through memory once (pyrage's passphrase API is
  bytes-based); personal-scale archives (MBs to a few hundred MBs) are fine.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import os
import queue
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import threading
import uuid
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from time import monotonic, sleep
from typing import Any, BinaryIO, NoReturn

import pyrage
from psycopg import sql
from pyrage import passphrase as age_passphrase
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError

from healthmes import __version__
from healthmes.activity.locking import (
    anchored_sqlite_lock_parent,
    exclusive_file_lock,
    global_write_plane_guard,
    payload_generation_guard,
    sqlite_runtime_guard,
)
from healthmes.backup.filesystem import (
    durable_atomic_writer,
    open_directory_anchored,
    open_regular_file,
)
from healthmes.backup.limits import SnapshotResourceLimits, limits_from_settings
from healthmes.backup.provider import (
    BackupError,
    SnapshotIntegrityError,
    WrongPassphraseError,
)
from healthmes.backup.recovery import (
    JournalEntryIdentity,
    JournalOperation,
    JournalPostgresTarget,
    RestoreJournal,
    anchored_restore_journal_directory,
    fsync_path,
    load_restore_journal,
    remove_restore_journal,
    restore_journal_path,
    write_restore_journal,
)
from healthmes.config import Settings

__all__ = [
    "PROVIDER_LOCAL",
    "PROVIDER_REMOTE_VAULT",
    "RECOVERY_SCOPE_PARTIAL_COMPONENT",
    "SCHEMA_VERSION",
    "SNAPSHOT_PREFIX",
    "SNAPSHOT_SUFFIX",
    "DataLocations",
    "RestoreResult",
    "SnapshotResourceLimits",
    "create_snapshot",
    "find_pg_tool",
    "libpq_env",
    "libpq_url",
    "parse_snapshot_name",
    "partial_backup_warning",
    "read_manifest",
    "recover_incomplete_restore",
    "recovered_runtime_guard",
    "restore_admission_guard",
    "resolve_backup_dir",
    "resolve_backup_provider_name",
    "resolve_data_locations",
    "resolve_passphrase",
    "restore_snapshot",
    "snapshot_name",
]

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2  # v2: adds the raw_ingest section (older binaries must refuse)
RECOVERY_SCOPE_PARTIAL_COMPONENT = "partial_component_snapshot"

SNAPSHOT_PREFIX = "healthmes-backup-"
SNAPSHOT_SUFFIX = ".tar.gz.age"
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"

MANIFEST_ARCNAME = "manifest.json"
HEALTHMES_SQLITE_ARCNAME = "db/healthmes.sqlite3"
HEALTHMES_PG_DUMP_ARCNAME = "db/healthmes.dump"
OW_PG_DUMP_ARCNAME = "db/open_wearables.dump"
MEDIA_ARCROOT = "media"
RAW_INGEST_ARCROOT = "raw_ingest"
HERMES_ARCROOT = "hermes"

_SQLITE_MEMORY_DATABASES = (None, "", ":memory:")

# Keg-only formulae probed (in order) when pg_dump/pg_restore is not on PATH.
_BREW_POSTGRES_FORMULAE = ("postgresql@16", "libpq", "postgresql")
_COMPONENT_ORDER = (
    "healthmes_db",
    "open_wearables_db",
    "media",
    "raw_ingest",
    "hermes_home",
)
_TREE_COMPONENT_ROOTS = {
    "media": MEDIA_ARCROOT,
    "raw_ingest": RAW_INGEST_ARCROOT,
    "hermes_home": HERMES_ARCROOT,
}
_LIBPQ_SECRET_QUERY_ENV = {
    "password": "PGPASSWORD",
    "sslpassword": "PGSSLPASSWORD",
    "passfile": "PGPASSFILE",
}
_SQLITE_RESTORE_RUNTIME_LOCK_TIMEOUT_SECONDS = 0.25
_RESTORE_ADMISSION_LOCK_TIMEOUT_SECONDS = 30.0
_POSTGRES_IDENTITY_MISMATCH_MARKER = "HEALTHMES_POSTGRES_TARGET_IDENTITY_MISMATCH"
_POSTGRES_TARGET_PID_MARKER = "HEALTHMES_POSTGRES_RESTORE_TARGET_PID:"
_POSTGRES_TARGET_START_TIMEOUT_SECONDS = 15.0
_POSTGRES_TARGET_STOP_TIMEOUT_SECONDS = 10.0
_POSTGRES_SESSION_FENCE_TIMEOUT_SECONDS = 10.0
_POSTGRES_SESSION_FENCE_POLL_SECONDS = 0.05
_POSTGRES_TOOL_TIMEOUT_SECONDS = 1800.0
_POSTGRES_TOOL_STDOUT_BYTES = 4 * 1024 * 1024
_POSTGRES_TOOL_STDERR_BYTES = 64 * 1024
_POSTGRES_TOOL_POLL_SECONDS = 0.01
_POSTGRES_RENDER_CHUNK_BYTES = 1024 * 1024
_POSTGRES_RENDER_ERROR_BYTES = 64 * 1024
_STAGE_COPY_CHUNK_BYTES = 1024 * 1024
_scandir = os.scandir
_POSTGRES_TOOL_TIMEOUT_OVERRIDE: ContextVar[float | None] = ContextVar(
    "healthmes_postgres_tool_timeout_override",
    default=None,
)


class _PostgresRestoreNotStarted(BackupError):
    """A PostgreSQL restore failed before any restore SQL could commit."""


class _PostgresConnectionFenceUncertain(BackupError):
    """The restore outcome is known separately from connection admission state."""

    def __init__(self, message: str, *, restore_outcome: str) -> None:
        super().__init__(message)
        if restore_outcome not in {"not_started", "committed", "unknown"}:
            raise ValueError("unsupported PostgreSQL restore outcome")
        self.restore_outcome = restore_outcome


def _postgres_tool_timeout_seconds(
    override: float | None = None,
) -> float:
    """Return the active per-operation PostgreSQL client deadline."""
    value = (
        override
        if override is not None
        else _POSTGRES_TOOL_TIMEOUT_OVERRIDE.get()
    )
    if value is None:
        value = _POSTGRES_TOOL_TIMEOUT_SECONDS
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(
            "PostgreSQL backup tool timeout must be a positive finite number"
        )
    return float(value)


@contextmanager
def _postgres_tool_timeout_scope(timeout_seconds: float) -> Any:
    """Apply one configured timeout to every PostgreSQL operation in a run."""
    effective = _postgres_tool_timeout_seconds(timeout_seconds)
    token = _POSTGRES_TOOL_TIMEOUT_OVERRIDE.set(effective)
    try:
        yield
    finally:
        _POSTGRES_TOOL_TIMEOUT_OVERRIDE.reset(token)


@dataclass(frozen=True, slots=True)
class _FilesystemGeneration:
    """Exact metadata generation for one filesystem entry."""

    device: int
    inode: int
    size: int
    mode: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_metadata(cls, metadata: os.stat_result) -> _FilesystemGeneration:
        return cls(
            device=metadata.st_dev,
            inode=metadata.st_ino,
            size=metadata.st_size,
            mode=metadata.st_mode,
            mtime_ns=metadata.st_mtime_ns,
            ctime_ns=metadata.st_ctime_ns,
        )

    def matches(self, metadata: os.stat_result) -> bool:
        return (
            metadata.st_dev == self.device
            and metadata.st_ino == self.inode
            and metadata.st_size == self.size
            and metadata.st_mode == self.mode
            and metadata.st_mtime_ns == self.mtime_ns
            and metadata.st_ctime_ns == self.ctime_ns
        )


def _bounded_descriptor_bytes(
    handle: BinaryIO,
    *,
    path: Path,
    limit: int,
    label: str,
) -> bytes:
    """Read one stable, already-open regular-file generation within ``limit``."""
    try:
        before_metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(before_metadata.st_mode):
            raise BackupError(f"{label} is not a regular file: {path}")
        before = _FilesystemGeneration.from_metadata(before_metadata)
        if before.size > limit:
            raise BackupError(f"{label} exceeds the configured {limit}-byte limit")
        handle.seek(0)
        payload = handle.read(limit + 1)
        after_metadata = os.fstat(handle.fileno())
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError(f"could not read {label} {path}: {exc}") from exc
    if not isinstance(payload, bytes):
        raise BackupError(f"could not read {label} {path}: non-bytes payload")
    if len(payload) > limit:
        raise BackupError(f"{label} exceeds the configured {limit}-byte limit")
    if len(payload) != before.size or not before.matches(after_metadata):
        raise BackupError(f"{label} changed while it was being read: {path}")
    return payload


def _nearest_existing_directory(path: Path) -> Path:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            break
        current = parent
    if current.is_file():
        return current.parent
    return current


def _require_disk_capacity(
    path: Path,
    *,
    payload_bytes: int,
    limits: SnapshotResourceLimits,
    label: str,
) -> None:
    root = _nearest_existing_directory(path)
    try:
        free = shutil.disk_usage(root).free
    except OSError as exc:
        raise BackupError(f"could not inspect free space for {label} at {root}: {exc}") from exc
    required = payload_bytes + limits.min_free_bytes
    if free < required:
        raise BackupError(
            f"insufficient disk space for {label}: need {required} bytes "
            f"including reserve, have {free}"
        )


def _path_payload_bytes(path: Path) -> int:
    if path.is_symlink():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file() and not child.is_symlink():
            total += child.stat().st_size
    return total


# ---------------------------------------------------------------------------
# Settings resolution
# ---------------------------------------------------------------------------
#
# The backup-specific Settings fields (backup_dir, backup_passphrase,
# ow_database_url, hermes_home) are typed fields of healthmes/config.py's
# Settings. Resolution here stays attribute-first with a documented
# environment-variable fallback so the module also works against slimmed-down
# Settings doubles in tests (getattr defaults) and tolerates configs written
# before the fields existed.


def _unwrap_secret(value: Any) -> str | None:
    """Return the plain string behind a SecretStr/str setting, or None."""
    if value is None:
        return None
    if hasattr(value, "get_secret_value"):
        value = value.get_secret_value()
    text = str(value).strip()
    return text or None


@dataclass(frozen=True, slots=True)
class DataLocations:
    """Where the live data lives — read at export time, written at restore.

    ``ow_database_url``, ``media_dir``, ``raw_ingest_dir`` and ``hermes_home``
    are optional sections: unset (or missing on disk at export time) sections
    are recorded as absent in the manifest and skipped symmetrically on
    restore. ``ow_runtime_configured`` records whether HealthMes can access
    Open Wearables at runtime independently of whether a database dump URL is
    available.
    """

    database_url: str
    ow_database_url: str | None = None
    media_dir: Path | None = None
    raw_ingest_dir: Path | None = None
    hermes_home: Path | None = None
    ow_runtime_configured: bool = False
    restore_state_dir: Path | None = None
    resource_limits: SnapshotResourceLimits = field(default_factory=SnapshotResourceLimits)
    postgres_tool_timeout_seconds: float = _POSTGRES_TOOL_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        timeout = self.postgres_tool_timeout_seconds
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError(
                "PostgreSQL backup tool timeout must be a positive finite number"
            )


@dataclass(slots=True)
class _StageBudget:
    limits: SnapshotResourceLimits
    member_count: int = 0
    discovered_entries: int = 0
    expanded_bytes: int = 0
    members: set[str] = field(default_factory=set)

    def discover_source_entry(self, path: str) -> None:
        self.discovered_entries += 1
        if self.discovered_entries > self.limits.max_members:
            raise BackupError(
                "snapshot source contains more than "
                f"{self.limits.max_members} filesystem entries "
                f"(limit reached at {path})"
            )

    def reserve_member(self, arcname: str) -> None:
        if arcname in self.members:
            return
        if self.member_count + 1 > self.limits.max_members:
            raise BackupError(
                f"snapshot contains more than {self.limits.max_members} archive members"
            )
        self.members.add(arcname)
        self.member_count += 1

    def reserve_file(self, arcname: str, size: int) -> None:
        if size < 0:
            raise BackupError(f"snapshot source reported a negative size: {arcname}")
        if size > self.limits.max_member_bytes:
            raise BackupError(
                f"{arcname} exceeds the configured "
                f"{self.limits.max_member_bytes}-byte limit"
            )
        if self.expanded_bytes + size > self.limits.max_expanded_bytes:
            raise BackupError(
                "snapshot expands beyond the configured "
                f"{self.limits.max_expanded_bytes}-byte limit"
            )
        self.reserve_member(arcname)
        self.expanded_bytes += size

    def reserve_file_member(self, arcname: str) -> int:
        self.reserve_member(arcname)
        remaining = self.limits.max_expanded_bytes - self.expanded_bytes
        limit = min(self.limits.max_member_bytes, remaining)
        if limit <= 0:
            raise BackupError(
                "snapshot expands beyond the configured "
                f"{self.limits.max_expanded_bytes}-byte limit"
            )
        return limit

    def add_file_bytes(self, arcname: str, size: int) -> None:
        if arcname not in self.members:
            raise BackupError(f"snapshot stage member was not reserved: {arcname}")
        if size > self.limits.max_member_bytes:
            raise BackupError(
                f"{arcname} exceeds the configured "
                f"{self.limits.max_member_bytes}-byte limit"
            )
        if self.expanded_bytes + size > self.limits.max_expanded_bytes:
            raise BackupError(
                "snapshot expands beyond the configured "
                f"{self.limits.max_expanded_bytes}-byte limit"
            )
        self.expanded_bytes += size


def _ensure_stage_directory(
    stage: Path,
    directory: Path,
    *,
    budget: _StageBudget,
) -> None:
    try:
        relative = directory.relative_to(stage)
    except ValueError as exc:
        raise BackupError(f"snapshot stage path escapes its root: {directory}") from exc
    current = stage
    for part in relative.parts:
        current = current / part
        arcname = current.relative_to(stage).as_posix()
        if current.exists():
            if current.is_symlink() or not current.is_dir():
                raise BackupError(f"snapshot stage directory is invalid: {current}")
            budget.reserve_member(arcname)
            continue
        budget.reserve_member(arcname)
        current.mkdir()


def _copy_regular_file_to_stage(
    source: Path,
    target: Path,
    *,
    stage: Path,
    budget: _StageBudget,
    source_parent_descriptor: int | None = None,
    source_name: str | None = None,
    expected_source_generation: _FilesystemGeneration | None = None,
) -> int:
    _ensure_stage_directory(stage, target.parent, budget=budget)
    arcname = target.relative_to(stage).as_posix()
    source_descriptor: int | None = None
    target_descriptor: int | None = None
    completed = False
    try:
        if source_parent_descriptor is not None and source_name is None:
            raise BackupError("descriptor-relative snapshot source name is missing")
        source_descriptor = os.open(
            source if source_parent_descriptor is None else source_name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=source_parent_descriptor,
        )
        source_metadata = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_metadata.st_mode):
            raise BackupError(f"snapshot source is not a regular file: {source}")
        source_generation = _FilesystemGeneration.from_metadata(source_metadata)
        if (
            expected_source_generation is not None
            and source_generation != expected_source_generation
        ):
            raise BackupError(
                f"snapshot source changed before it could be copied: {source}"
            )
        expected_size = source_metadata.st_size
        budget.reserve_file(arcname, expected_size)
        _require_disk_capacity(
            target.parent,
            payload_bytes=expected_size,
            limits=budget.limits,
            label=f"snapshot stage member {arcname}",
        )
        target_descriptor = os.open(
            target,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            stat.S_IMODE(source_metadata.st_mode) or 0o600,
        )
        copied = 0
        while True:
            chunk = os.read(source_descriptor, _STAGE_COPY_CHUNK_BYTES)
            if not chunk:
                break
            copied += len(chunk)
            if copied > expected_size:
                raise BackupError(
                    f"snapshot source changed size while being copied: {source}"
                )
            view = memoryview(chunk)
            while view:
                written = os.write(target_descriptor, view)
                view = view[written:]
        if copied != expected_size:
            raise BackupError(
                f"snapshot source changed size while being copied: {source}"
            )
        after_metadata = os.fstat(source_descriptor)
        try:
            named_metadata = os.stat(
                source if source_parent_descriptor is None else source_name,
                dir_fd=source_parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise BackupError(
                f"snapshot source path changed while being copied: {source}"
            ) from exc
        if (
            not source_generation.matches(after_metadata)
            or not source_generation.matches(named_metadata)
        ):
            raise BackupError(
                f"snapshot source changed while being copied: {source}"
            )
        os.fsync(target_descriptor)
        completed = True
        return copied
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError(f"could not stage snapshot source {source}: {exc}") from exc
    finally:
        if target_descriptor is not None:
            os.close(target_descriptor)
        if source_descriptor is not None:
            os.close(source_descriptor)
        if not completed:
            target.unlink(missing_ok=True)


class RestoreResult(dict[str, Any]):
    """Manifest-compatible result with the exact successful restore scope.

    ``restore_snapshot`` historically returned the manifest dictionary.
    Subclassing ``dict`` preserves indexing/equality for existing callers
    while exposing the recovery outcome needed by CLI/provider surfaces.
    """

    recovery_mode: str
    recovered_components: tuple[str, ...]
    skipped_components: tuple[str, ...]

    def __init__(
        self,
        manifest: dict[str, Any],
        *,
        recovery_mode: str,
        recovered_components: tuple[str, ...],
        skipped_components: tuple[str, ...],
    ) -> None:
        super().__init__(manifest)
        self.recovery_mode = recovery_mode
        self.recovered_components = recovered_components
        self.skipped_components = skipped_components

    @property
    def manifest(self) -> dict[str, Any]:
        """Return a plain manifest copy for explicit new callers."""
        return dict(self)


@dataclass(frozen=True, slots=True)
class _ManifestLayout:
    contents: dict[str, Any]
    inventory: dict[str, dict[str, Any]]
    owners: dict[str, str]
    component_paths: dict[str, PurePosixPath]


def resolve_backup_dir(settings: Settings) -> Path:
    """Target directory for local snapshots: Settings, env, then data_dir/backups."""
    configured = getattr(settings, "backup_dir", None)
    if configured:
        return Path(configured)
    env_value = os.environ.get("HEALTHMES_BACKUP_DIR", "").strip()
    if env_value:
        return Path(env_value)
    return Path(settings.data_dir) / "backups"


def resolve_passphrase(settings: Settings) -> str | None:
    """Snapshot passphrase: Settings field, then HEALTHMES_BACKUP_PASSPHRASE env."""
    configured = _unwrap_secret(getattr(settings, "backup_passphrase", None))
    if configured:
        return configured
    return _unwrap_secret(os.environ.get("HEALTHMES_BACKUP_PASSPHRASE"))


PROVIDER_LOCAL = "local"
PROVIDER_REMOTE_VAULT = "remote_vault"
# "remote" is accepted as a convenience alias (it is what the CLI flag uses).
_PROVIDER_ALIASES = {
    "local": PROVIDER_LOCAL,
    "remote_vault": PROVIDER_REMOTE_VAULT,
    "remote": PROVIDER_REMOTE_VAULT,
}


def resolve_backup_provider_name(settings: Settings) -> str:
    """The configured backup provider: ``local`` (default) or ``remote_vault``.

    Reads ``Settings.backup_provider`` first, then the
    ``HEALTHMES_BACKUP_PROVIDER`` env var. Unknown values raise
    :class:`BackupError` — a typo must never silently fall back to a
    different storage destination. Lives here (not in
    healthmes/backup/remote_vault.py) so resolving the selector never
    imports boto3.
    """
    raw = _unwrap_secret(getattr(settings, "backup_provider", None)) or _unwrap_secret(
        os.environ.get("HEALTHMES_BACKUP_PROVIDER")
    )
    if raw is None:
        return PROVIDER_LOCAL
    normalized = _PROVIDER_ALIASES.get(raw.lower())
    if normalized is None:
        raise BackupError(
            f"unknown backup provider {raw!r}: expected 'local' or 'remote_vault' "
            "(set via HEALTHMES_BACKUP_PROVIDER or the backup_provider setting)"
        )
    return normalized


def resolve_data_locations(settings: Settings) -> DataLocations:
    """Derive the live data locations covered by a snapshot from Settings.

    - healthmes database: ``Settings.database_url`` (always included);
    - open-wearables database: optional — ``Settings.ow_database_url`` or
      the ``HEALTHMES_OW_DATABASE_URL`` env var (direct postgres URL; the
      REST ``ow_base_url`` cannot produce a dump);
    - open-wearables runtime: configured when ``Settings.ow_api_key`` /
      ``HEALTHMES_OW_API_KEY`` is non-empty, independently of dump access;
    - media: always ``{data_dir}/media`` (healthmes/api/food.py convention);
    - raw ingest: always ``{data_dir}/raw_ingest``;
    - Hermes state: optional — ``Settings.hermes_home`` or the vendor's own
      ``HERMES_HOME`` env var; only included "when configured" (PLAN §9).
    """
    ow_database_url = _unwrap_secret(getattr(settings, "ow_database_url", None)) or _unwrap_secret(
        os.environ.get("HEALTHMES_OW_DATABASE_URL")
    )
    ow_runtime_configured = bool(
        _unwrap_secret(getattr(settings, "ow_api_key", None))
        or _unwrap_secret(os.environ.get("HEALTHMES_OW_API_KEY"))
    )
    hermes_home = getattr(settings, "hermes_home", None)
    if not hermes_home:
        env_home = os.environ.get("HERMES_HOME", "").strip()
        hermes_home = Path(env_home).expanduser() if env_home else None
    restore_state_dir = getattr(settings, "restore_state_dir", None)
    database = make_url(settings.database_url)
    if (
        restore_state_dir is None
        and database.get_backend_name() == "postgresql"
    ):
        # Password rotation must not orphan an unfinished restore journal.
        database_identity = "|".join(
            (
                database.get_backend_name(),
                database.host or "",
                str(database.port or ""),
                database.database or "",
                database.username or "",
            )
        )
        digest = hashlib.sha256(database_identity.encode()).hexdigest()[:16]
        restore_state_dir = (
            Path(settings.data_dir)
            / ".healthmes-restore-state"
            / digest
        )
    locations = DataLocations(
        database_url=settings.database_url,
        ow_database_url=ow_database_url,
        media_dir=Path(settings.data_dir) / "media",
        raw_ingest_dir=Path(settings.data_dir) / "raw_ingest",
        hermes_home=Path(hermes_home) if hermes_home else None,
        ow_runtime_configured=ow_runtime_configured,
        resource_limits=limits_from_settings(settings),
        postgres_tool_timeout_seconds=getattr(
            settings,
            "backup_postgres_tool_timeout_seconds",
            _POSTGRES_TOOL_TIMEOUT_SECONDS,
        ),
        restore_state_dir=(
            Path(restore_state_dir)
            if restore_state_dir is not None
            else None
        ),
    )
    if (
        database.get_backend_name() == "sqlite"
        and database.database in _SQLITE_MEMORY_DATABASES
    ):
        return locations
    return replace(
        locations,
        restore_state_dir=_restore_state_identity_path(locations),
    )


def partial_backup_warning(locations: DataLocations) -> str | None:
    """Operational warning when runtime Open Wearables data cannot be recovered."""
    if locations.ow_runtime_configured and not locations.ow_database_url:
        return (
            "Partial backup: Open Wearables is configured for runtime, but "
            "HEALTHMES_OW_DATABASE_URL is unset, so this valid snapshot omits "
            "the Open Wearables database and cannot recover that data. It "
            "restores only the components listed in manifest.json."
        )
    return None


# ---------------------------------------------------------------------------
# Snapshot naming
# ---------------------------------------------------------------------------


def snapshot_name(created_at: datetime) -> str:
    """Canonical snapshot file name for a creation instant.

    The stamp is normalized to UTC so lexicographic name order equals
    chronological order regardless of the machine's local timezone.
    """
    _require_aware(created_at)
    stamp = created_at.astimezone(UTC)
    return f"{SNAPSHOT_PREFIX}{stamp.strftime(_TIMESTAMP_FORMAT)}{SNAPSHOT_SUFFIX}"


def parse_snapshot_name(name: str) -> datetime | None:
    """Inverse of :func:`snapshot_name`; None when ``name`` is not a snapshot."""
    if not (name.startswith(SNAPSHOT_PREFIX) and name.endswith(SNAPSHOT_SUFFIX)):
        return None
    stamp = name[len(SNAPSHOT_PREFIX) : len(name) - len(SNAPSHOT_SUFFIX)]
    # Collision suffix ("-2") appended by providers parses back to the base stamp.
    stamp = stamp.split("-", 1)[0]
    try:
        return datetime.strptime(stamp, _TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _require_aware(moment: datetime) -> None:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("created_at must be timezone-aware")


# ---------------------------------------------------------------------------
# pg_dump / pg_restore discovery and invocation
# ---------------------------------------------------------------------------


def find_pg_tool(name: str) -> Path | None:
    """Locate a postgres client binary: PATH first, Homebrew kegs second.

    macOS installs ``postgresql@16``/``libpq`` keg-only, so ``pg_dump`` is
    frequently absent from PATH even when installed; ``brew --prefix`` finds
    the keg without requiring the user to relink anything.
    """
    found = shutil.which(name)
    if found:
        return Path(found)
    brew = shutil.which("brew")
    if brew is None:
        return None
    for formula in _BREW_POSTGRES_FORMULAE:
        try:
            result = subprocess.run(
                [brew, "--prefix", formula],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            continue
        candidate = Path(result.stdout.strip()) / "bin" / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def libpq_url(sqlalchemy_url: str) -> str:
    """Convert an SQLAlchemy postgres URL to the libpq form pg_dump accepts.

    Strips the driver qualifier (``postgresql+psycopg://`` →
    ``postgresql://``) and **removes the password**: the URL travels on the
    pg tool's argv, and argv is readable by other processes (`ps aux` on
    macOS/Linux; /proc/<pid>/cmdline is world-readable). The password goes
    through the ``PGPASSWORD`` environment variable instead
    (:func:`libpq_env`) — same reason the CLI never takes the age passphrase
    as an argument (healthmes/__main__.py).
    """
    url = make_url(sqlalchemy_url)
    # URL.set() ignores None values, so the password is dropped by rebuilding
    # the URL without it.
    safe_query = {
        key: value
        for key, value in url.query.items()
        if key.casefold() not in _LIBPQ_SECRET_QUERY_ENV
    }
    safe = URL.create(
        drivername=url.get_backend_name(),
        username=url.username,
        host=url.host,
        port=url.port,
        database=url.database,
        query=safe_query,
    )
    return safe.render_as_string(hide_password=False)


def _libpq_query_value(value: str | tuple[str, ...]) -> str:
    if isinstance(value, tuple):
        return value[-1]
    return value


def libpq_env(sqlalchemy_url: str) -> dict[str, str]:
    """Process environment for a pg tool run, carrying credentials privately."""
    env = dict(os.environ)
    url = make_url(sqlalchemy_url)
    password = url.password
    if password:
        env["PGPASSWORD"] = str(password)
    for key, value in url.query.items():
        variable = _LIBPQ_SECRET_QUERY_ENV.get(key.casefold())
        if variable is None:
            continue
        if variable == "PGPASSWORD" and password:
            continue
        env[variable] = _libpq_query_value(value)
    return env


def _run_pg_tool(
    tool: str, args: list[str], *, action: str, env: dict[str, str] | None = None
) -> str:
    tool_timeout = _postgres_tool_timeout_seconds()
    binary = _require_pg_tool(tool, action=action)
    try:
        process = subprocess.Popen(
            [str(binary), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        raise BackupError(f"could not start {tool} while attempting to {action}: {exc}") from exc
    if process.stdout is None or process.stderr is None:
        _stop_postgres_target(process)
        raise BackupError(f"{tool} output pipes are unavailable while attempting to {action}")

    stdout = bytearray()
    stderr = bytearray()
    overflow = threading.Event()
    overflow_stream: list[str] = []
    reader_errors: list[BaseException] = []
    result_lock = threading.Lock()

    def consume(stream, destination: bytearray, *, limit: int, label: str) -> None:
        total = 0
        try:
            for chunk in iter(lambda: stream.read(8192), b""):
                total += len(chunk)
                remaining = limit - len(destination)
                if remaining > 0:
                    destination.extend(chunk[:remaining])
                if total > limit and not overflow.is_set():
                    with result_lock:
                        if not overflow.is_set():
                            overflow_stream.append(label)
                            overflow.set()
                            try:
                                process.terminate()
                            except OSError:
                                pass
        except BaseException as exc:
            with result_lock:
                reader_errors.append(exc)
        finally:
            stream.close()

    stdout_thread = threading.Thread(
        target=consume,
        args=(process.stdout, stdout),
        kwargs={"limit": _POSTGRES_TOOL_STDOUT_BYTES, "label": "stdout"},
        name=f"healthmes-{tool}-stdout",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=consume,
        args=(process.stderr, stderr),
        kwargs={"limit": _POSTGRES_TOOL_STDERR_BYTES, "label": "stderr"},
        name=f"healthmes-{tool}-stderr",
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    deadline = monotonic() + tool_timeout
    timed_out = False
    try:
        while process.poll() is None:
            if overflow.is_set():
                try:
                    _stop_postgres_target(process)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise BackupError(
                        f"{tool} could not be stopped while attempting to {action}"
                    ) from exc
                break
            if monotonic() >= deadline:
                timed_out = True
                try:
                    _stop_postgres_target(process)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise BackupError(
                        f"{tool} could not be stopped while attempting to {action}"
                    ) from exc
                break
            sleep(_POSTGRES_TOOL_POLL_SECONDS)
        process.wait(timeout=_POSTGRES_TARGET_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        try:
            _stop_postgres_target(process)
        except (OSError, subprocess.TimeoutExpired) as stop_exc:
            raise BackupError(
                f"{tool} could not be stopped or reaped while attempting to {action}"
            ) from stop_exc
        raise BackupError(
            f"{tool} could not be reaped while attempting to {action}"
        ) from exc
    except BaseException:
        try:
            _stop_postgres_target(process)
        except BaseException:
            logger.exception(
                "%s could not be stopped after cancellation while attempting to %s",
                tool,
                action,
            )
        finally:
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
        raise
    finally:
        stdout_thread.join(timeout=_POSTGRES_TARGET_STOP_TIMEOUT_SECONDS)
        stderr_thread.join(timeout=_POSTGRES_TARGET_STOP_TIMEOUT_SECONDS)

    if stdout_thread.is_alive() or stderr_thread.is_alive():
        raise BackupError(
            f"{tool} output readers did not stop while attempting to {action}"
        )
    if reader_errors:
        raise BackupError(
            f"could not read {tool} output while attempting to {action}: "
            f"{reader_errors[0]}"
        ) from reader_errors[0]
    if timed_out:
        raise BackupError(
            f"{tool} timed out after {tool_timeout:g} seconds "
            f"while attempting to {action}"
        )
    if overflow.is_set():
        label = overflow_stream[0] if overflow_stream else "output"
        limit = (
            _POSTGRES_TOOL_STDOUT_BYTES
            if label == "stdout"
            else _POSTGRES_TOOL_STDERR_BYTES
        )
        raise BackupError(
            f"{tool} {label} exceeds the configured {limit}-byte limit "
            f"while attempting to {action}"
        )

    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")
    if process.returncode != 0:
        detail = stderr_text.strip() or stdout_text.strip() or "no output"
        raise BackupError(f"{tool} failed (exit {process.returncode}): {detail}")
    return stdout_text


def _require_pg_tool(tool: str, *, action: str) -> Path:
    binary = find_pg_tool(tool)
    if binary is None:
        raise BackupError(
            f"{tool} not found on PATH and no Homebrew postgres keg detected; "
            f"install it (e.g. `brew install postgresql@16`) to {action} a postgres database"
        )
    return binary


def _pg_dump_to(
    database_url: str,
    dest: Path,
    *,
    limits: SnapshotResourceLimits | None = None,
    budget: _StageBudget | None = None,
    arcname: str | None = None,
) -> None:
    """Stream a custom-format dump into a bounded stage file."""
    tool_timeout = _postgres_tool_timeout_seconds()
    pg_dump = _require_pg_tool("pg_dump", action="dump")
    effective_limits = limits or SnapshotResourceLimits()
    effective_budget = budget or _StageBudget(effective_limits)
    member_name = arcname or dest.name
    output_limit = effective_budget.reserve_file_member(member_name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    env = libpq_env(database_url)
    try:
        output_handle = dest.open("xb")
    except OSError as exc:
        raise BackupError(
            f"could not create PostgreSQL dump output {dest}: {exc}"
        ) from exc
    output_metadata = os.fstat(output_handle.fileno())

    def remove_partial_output() -> None:
        try:
            named = dest.lstat()
        except FileNotFoundError:
            return
        except OSError:
            return
        if (
            stat.S_ISREG(named.st_mode)
            and named.st_dev == output_metadata.st_dev
            and named.st_ino == output_metadata.st_ino
        ):
            try:
                dest.unlink()
            except FileNotFoundError:
                pass

    try:
        process = subprocess.Popen(
            [
                str(pg_dump),
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                f"--dbname={libpq_url(database_url)}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        output_handle.close()
        remove_partial_output()
        raise BackupError(f"could not start pg_dump: {exc}") from exc
    if process.stdout is None or process.stderr is None:
        try:
            _stop_postgres_target(process)
        finally:
            output_handle.close()
            remove_partial_output()
        raise BackupError("pg_dump output pipes are unavailable")

    stderr_chunks: list[bytes] = []
    stderr_bytes = 0
    stderr_error: list[BaseException] = []
    stderr_done = threading.Event()

    def consume_stderr() -> None:
        nonlocal stderr_bytes
        assert process.stderr is not None
        try:
            for chunk in iter(lambda: process.stderr.read(8192), b""):
                if stderr_bytes >= _POSTGRES_RENDER_ERROR_BYTES:
                    continue
                remaining = _POSTGRES_RENDER_ERROR_BYTES - stderr_bytes
                stderr_chunks.append(chunk[:remaining])
                stderr_bytes += min(len(chunk), remaining)
        except BaseException as exc:
            stderr_error.append(exc)
        finally:
            stderr_done.set()

    stderr_thread = threading.Thread(
        target=consume_stderr,
        name="healthmes-pg-dump-stderr",
        daemon=True,
    )
    stderr_thread.start()
    stdout_error: list[BaseException] = []
    stdout_done = threading.Event()
    written_bytes = 0

    def consume_stdout() -> None:
        nonlocal written_bytes
        assert process.stdout is not None
        try:
            for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
                if written_bytes + len(chunk) > output_limit:
                    raise BackupError(
                        f"{member_name} exceeds the configured "
                        f"{output_limit}-byte remaining stage limit"
                    )
                _require_disk_capacity(
                    dest.parent,
                    payload_bytes=len(chunk),
                    limits=effective_limits,
                    label=f"PostgreSQL dump {member_name}",
                )
                output_handle.write(chunk)
                written_bytes += len(chunk)
        except BaseException as exc:
            stdout_error.append(exc)
        finally:
            stdout_done.set()

    stdout_thread = threading.Thread(
        target=consume_stdout,
        name="healthmes-pg-dump-stdout",
        daemon=True,
    )
    stdout_thread.start()
    deadline = monotonic() + tool_timeout
    primary_error: BaseException | None = None
    timed_out = False
    try:
        while not stdout_done.is_set() or process.poll() is None:
            if stdout_error:
                primary_error = stdout_error[0]
                break
            if monotonic() >= deadline:
                timed_out = True
                primary_error = BackupError(
                    f"pg_dump timed out after {tool_timeout:g} "
                    f"seconds while dumping {member_name}"
                )
                break
            sleep(_POSTGRES_TOOL_POLL_SECONDS)
        if primary_error is None and stdout_error:
            primary_error = stdout_error[0]
        if primary_error is None:
            remaining = max(0.001, deadline - monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                primary_error = BackupError(
                    f"pg_dump timed out after {tool_timeout:g} "
                    f"seconds while dumping {member_name}"
                )
                primary_error.__cause__ = exc
    except BaseException as exc:
        primary_error = exc
    finally:
        if primary_error is not None or timed_out:
            try:
                _stop_postgres_target(process)
            except BaseException as exc:
                primary_error = BackupError(
                    f"{primary_error or 'pg_dump failed'}; pg_dump could not "
                    f"be stopped: {exc}"
                )
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        stdout_thread.join(timeout=_POSTGRES_TARGET_STOP_TIMEOUT_SECONDS)
        stderr_thread.join(timeout=_POSTGRES_TARGET_STOP_TIMEOUT_SECONDS)
        try:
            output_handle.flush()
            os.fsync(output_handle.fileno())
        except BaseException as exc:
            if primary_error is None:
                primary_error = exc
            else:
                primary_error = BackupError(
                    f"{primary_error}; PostgreSQL dump output could not be "
                    f"flushed: {exc}"
                )
        finally:
            try:
                output_handle.close()
            except BaseException as exc:
                if primary_error is None:
                    primary_error = exc
                else:
                    primary_error = BackupError(
                        f"{primary_error}; PostgreSQL dump output could not be "
                        f"closed: {exc}"
                    )

    if stdout_thread.is_alive() or stderr_thread.is_alive():
        primary_error = BackupError(
            f"pg_dump output readers did not stop while dumping {member_name}"
        )
    if stderr_error and primary_error is None:
        primary_error = BackupError(
            f"could not read pg_dump output while dumping {member_name}: "
            f"{stderr_error[0]}"
        )
    if primary_error is not None:
        remove_partial_output()
        if isinstance(primary_error, BackupError):
            raise primary_error
        raise BackupError(f"pg_dump failed: {primary_error}") from primary_error
    if process.returncode != 0:
        remove_partial_output()
        detail = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
        raise BackupError(
            f"pg_dump failed (exit {process.returncode}): {detail or 'no output'}"
        )
    effective_budget.add_file_bytes(member_name, written_bytes)


def _postgres_identity_assertion(
    expected_identity: tuple[str, int],
    *,
    mismatch_marker: str,
) -> str:
    system_identifier, database_oid = expected_identity
    if (
        not system_identifier.isascii()
        or not system_identifier.isdecimal()
        or database_oid <= 0
    ):
        raise _PostgresRestoreNotStarted(
            "PostgreSQL restore target has an invalid preflight identity"
        )
    return (
        "DO $healthmes_identity$ "
        "DECLARE "
        "actual_system_identifier text; "
        "actual_database_oid oid; "
        "BEGIN "
        "SELECT (pg_control_system()).system_identifier::text "
        "INTO actual_system_identifier; "
        "SELECT oid INTO actual_database_oid "
        "FROM pg_database WHERE datname = current_database(); "
        f"IF actual_system_identifier <> '{system_identifier}' "
        f"OR actual_database_oid <> {database_oid}::oid THEN "
        "RAISE EXCEPTION USING "
        f"MESSAGE = '{mismatch_marker}'; "
        "END IF; "
        "END "
        "$healthmes_identity$;"
    )


def _postgres_schema_reset() -> str:
    """Remove every non-system schema before replaying the snapshot."""
    return (
        "DO $healthmes_reset$ "
        "DECLARE schema_name text; "
        "BEGIN "
        "FOR schema_name IN "
        "SELECT nspname FROM pg_namespace "
        "WHERE nspname NOT LIKE 'pg_%' "
        "AND nspname <> 'information_schema' "
        "LOOP "
        "EXECUTE format('DROP SCHEMA %I CASCADE', schema_name); "
        "END LOOP; "
        "CREATE SCHEMA public; "
        "END "
        "$healthmes_reset$;"
    )


def _database_url_for_database(database_url: str, database: str) -> str:
    url = make_url(database_url)
    return url.set(
        drivername=url.get_backend_name(),
        database=database,
    ).render_as_string(hide_password=False)


def _maintenance_database_urls(database_url: str) -> tuple[str, ...]:
    target_database = make_url(database_url).database
    if not target_database:
        raise _PostgresRestoreNotStarted(
            "PostgreSQL restore target database name is missing"
        )
    names = tuple(
        name
        for name in ("postgres", "template1")
        if name != target_database
    )
    if not names:
        raise _PostgresRestoreNotStarted(
            "no PostgreSQL maintenance database is available"
        )
    return tuple(
        _database_url_for_database(database_url, name)
        for name in names
    )


def _run_maintenance_sql(
    maintenance_url: str,
    statement: str,
    *,
    action: str,
) -> str:
    return _run_pg_tool(
        "psql",
        [
            "--no-psqlrc",
            "--set=ON_ERROR_STOP=1",
            "--tuples-only",
            "--no-align",
            f"--dbname={libpq_url(maintenance_url)}",
            f"--command={statement}",
        ],
        action=action,
        env=libpq_env(maintenance_url),
    )


def _set_postgres_connections_allowed(
    database_url: str,
    *,
    allowed: bool,
    maintenance_url: str | None = None,
) -> str:
    target_database = make_url(database_url).database
    if not target_database:
        raise BackupError("PostgreSQL restore target database name is missing")
    statement = sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS {}").format(
        sql.Identifier(target_database),
        sql.SQL("true" if allowed else "false"),
    ).as_string()
    candidates = (
        (maintenance_url,)
        if maintenance_url is not None
        else _maintenance_database_urls(database_url)
    )
    failures: list[str] = []
    for candidate in candidates:
        try:
            _run_maintenance_sql(
                candidate,
                statement,
                action=(
                    "re-enable connections after restoring"
                    if allowed
                    else "fence connections before restoring"
                ),
            )
        except BackupError as exc:
            failures.append(str(exc))
            continue
        return candidate
    action = "re-enable" if allowed else "disable"
    raise BackupError(
        f"could not {action} PostgreSQL target connections through a "
        "maintenance database: " + "; ".join(failures)
    )


def _terminate_postgres_target_sessions(
    database_url: str,
    *,
    maintenance_url: str,
    protected_backend_pids: set[int],
) -> None:
    target_database = make_url(database_url).database
    if not target_database:
        raise _PostgresRestoreNotStarted(
            "PostgreSQL restore target database name is missing"
        )
    protected = sql.SQL(", ").join(
        sql.Literal(pid) for pid in sorted(protected_backend_pids)
    )
    protected_array = sql.SQL("ARRAY[{}]::integer[]").format(protected)
    target_literal = sql.Literal(target_database)
    predicate = sql.SQL(
        "datname = {target} "
        "AND backend_type = 'client backend' "
        "AND NOT (pid = ANY ({protected}))"
    ).format(
        target=target_literal,
        protected=protected_array,
    )
    terminate_statement = sql.SQL(
        "SELECT pg_terminate_backend(pid) "
        "FROM pg_stat_activity "
        "WHERE {predicate};"
    ).format(
        predicate=predicate,
    )
    _run_maintenance_sql(
        maintenance_url,
        terminate_statement.as_string(),
        action="terminate PostgreSQL sessions before restoring",
    )
    count_statement = sql.SQL(
        "SELECT count(*) FROM pg_stat_activity WHERE {predicate};"
    ).format(
        predicate=predicate,
    )
    deadline = monotonic() + _POSTGRES_SESSION_FENCE_TIMEOUT_SECONDS
    while True:
        output = _run_maintenance_sql(
            maintenance_url,
            count_statement.as_string(),
            action="verify the PostgreSQL restore session fence",
        )
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        try:
            remaining = int(lines[-1]) if lines else 0
        except ValueError as exc:
            raise BackupError(
                "PostgreSQL session fence verification returned an invalid count"
            ) from exc
        if remaining == 0:
            return
        if monotonic() >= deadline:
            raise BackupError(
                "timed out waiting for PostgreSQL target sessions to terminate"
            )
        sleep(_POSTGRES_SESSION_FENCE_POLL_SECONDS)


def _postgres_connections_allowed(
    database_url: str,
    *,
    maintenance_url: str | None = None,
) -> tuple[bool, str]:
    """Read the target database admission state through a maintenance DB."""
    target_database = make_url(database_url).database
    if not target_database:
        raise BackupError("PostgreSQL restore target database name is missing")
    statement = sql.SQL(
        "SELECT datallowconn FROM pg_database WHERE datname = {}"
    ).format(sql.Literal(target_database))
    candidates = (
        (maintenance_url,)
        if maintenance_url is not None
        else _maintenance_database_urls(database_url)
    )
    failures: list[str] = []
    for candidate in candidates:
        try:
            output = _run_maintenance_sql(
                candidate,
                statement.as_string(),
                action="verify PostgreSQL target connection admission",
            )
        except BackupError as exc:
            failures.append(str(exc))
            continue
        lines = [line.strip().casefold() for line in output.splitlines() if line.strip()]
        if len(lines) != 1 or lines[0] not in {"t", "true", "f", "false"}:
            failures.append(
                "PostgreSQL connection-admission verification returned an invalid value"
            )
            continue
        return lines[0] in {"t", "true"}, candidate
    raise BackupError(
        "could not verify PostgreSQL target connection admission through a "
        "maintenance database: " + "; ".join(failures)
    )


def _stop_postgres_target(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=_POSTGRES_TARGET_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=_POSTGRES_TARGET_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise BackupError(
                "PostgreSQL target process could not be reaped after termination"
            ) from exc


def _postgres_process_stderr(stderr_file) -> str:
    stderr_file.flush()
    stderr_file.seek(0)
    return stderr_file.read().strip()


def _pg_restore_sql_limit(
    dump_path: Path,
    *,
    limits: SnapshotResourceLimits,
) -> int:
    try:
        dump_bytes = dump_path.stat().st_size
    except OSError as exc:
        raise _PostgresRestoreNotStarted(
            f"could not inspect PostgreSQL dump {dump_path}: {exc}"
        ) from exc
    ratio_limit = max(1, math.ceil(dump_bytes * limits.max_compression_ratio))
    return min(limits.max_expanded_bytes, ratio_limit)


def _render_pg_restore_sql(
    pg_restore: Path,
    dump_path: Path,
    restore_sql,
    *,
    env: dict[str, str],
    limits: SnapshotResourceLimits,
) -> None:
    tool_timeout = _postgres_tool_timeout_seconds()
    output_limit = _pg_restore_sql_limit(dump_path, limits=limits)
    _require_disk_capacity(
        Path(tempfile.gettempdir()),
        payload_bytes=output_limit,
        limits=limits,
        label="PostgreSQL restore SQL expansion",
    )
    try:
        process = subprocess.Popen(
            [
                str(pg_restore),
                "--exit-on-error",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-privileges",
                "--file=-",
                str(dump_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=env,
        )
    except OSError as exc:
        raise _PostgresRestoreNotStarted(
            f"could not start pg_restore while preparing restore SQL: {exc}"
        ) from exc
    if process.stdout is None or process.stderr is None:
        _stop_postgres_target(process)
        raise _PostgresRestoreNotStarted(
            "pg_restore output pipes are unavailable while preparing restore SQL"
        )

    stderr_chunks: list[str] = []
    stderr_bytes = 0
    stderr_error: list[BaseException] = []
    stderr_done = threading.Event()

    def consume_stderr() -> None:
        nonlocal stderr_bytes
        assert process.stderr is not None
        try:
            for chunk in iter(lambda: process.stderr.read(8192), ""):
                if stderr_bytes >= _POSTGRES_RENDER_ERROR_BYTES:
                    continue
                encoded = chunk.encode("utf-8")
                remaining = _POSTGRES_RENDER_ERROR_BYTES - stderr_bytes
                stderr_chunks.append(
                    encoded[:remaining].decode("utf-8", errors="replace")
                )
                stderr_bytes += min(len(encoded), remaining)
        except BaseException as exc:
            stderr_error.append(exc)
        finally:
            stderr_done.set()

    stderr_thread = threading.Thread(
        target=consume_stderr,
        name="healthmes-pg-restore-stderr",
        daemon=True,
    )
    stderr_thread.start()
    stdout_error: list[BaseException] = []
    stdout_done = threading.Event()
    expanded_bytes = 0

    def consume_stdout() -> None:
        nonlocal expanded_bytes
        assert process.stdout is not None
        try:
            for chunk in iter(lambda: process.stdout.read(_POSTGRES_RENDER_CHUNK_BYTES), ""):
                chunk_bytes = len(chunk.encode("utf-8"))
                if expanded_bytes + chunk_bytes > output_limit:
                    raise _PostgresRestoreNotStarted(
                        "pg_restore SQL expansion exceeds the configured "
                        f"{output_limit}-byte nested expansion limit"
                    )
                restore_sql.write(chunk)
                expanded_bytes += chunk_bytes
        except BaseException as exc:
            stdout_error.append(exc)
        finally:
            stdout_done.set()

    stdout_thread = threading.Thread(
        target=consume_stdout,
        name="healthmes-pg-restore-stdout",
        daemon=True,
    )
    stdout_thread.start()
    deadline = monotonic() + tool_timeout
    primary_error: BaseException | None = None
    timed_out = False
    try:
        while not stdout_done.is_set() or process.poll() is None:
            if stdout_error:
                primary_error = stdout_error[0]
                break
            if monotonic() >= deadline:
                timed_out = True
                primary_error = _PostgresRestoreNotStarted(
                    f"pg_restore timed out after {tool_timeout:g} "
                    "seconds while preparing restore SQL"
                )
                break
            sleep(_POSTGRES_TOOL_POLL_SECONDS)
        if primary_error is None and stdout_error:
            primary_error = stdout_error[0]
        if primary_error is None:
            remaining = max(0.001, deadline - monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                primary_error = _PostgresRestoreNotStarted(
                    f"pg_restore timed out after {tool_timeout:g} "
                    "seconds while preparing restore SQL"
                )
                primary_error.__cause__ = exc
    except BaseException as exc:
        primary_error = exc
    finally:
        if primary_error is not None or timed_out:
            try:
                _stop_postgres_target(process)
            except BaseException as exc:
                primary_error = BackupError(
                    f"{primary_error or 'pg_restore failed'}; pg_restore could "
                    f"not be stopped: {exc}"
                )
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        stdout_thread.join(timeout=_POSTGRES_TARGET_STOP_TIMEOUT_SECONDS)
        stderr_thread.join(timeout=_POSTGRES_TARGET_STOP_TIMEOUT_SECONDS)

    if stdout_thread.is_alive() or stderr_thread.is_alive():
        primary_error = BackupError(
            "pg_restore output readers did not stop while preparing restore SQL"
        )
    if stderr_error and primary_error is None:
        primary_error = BackupError(
            "could not read pg_restore output while preparing restore SQL: "
            f"{stderr_error[0]}"
        )
    if primary_error is not None:
        if isinstance(primary_error, _PostgresRestoreNotStarted):
            raise primary_error
        raise _PostgresRestoreNotStarted(
            f"pg_restore failed while preparing restore SQL: {primary_error}"
        ) from primary_error
    if process.returncode != 0:
        detail = "".join(stderr_chunks).strip() or "no output"
        raise _PostgresRestoreNotStarted(
            f"pg_restore failed while preparing restore SQL "
            f"(exit {process.returncode}): {detail}"
        )


def _pg_restore_from(
    database_url: str,
    dump_path: Path,
    expected_identity: tuple[str, int],
    *,
    protected_backend_pids: set[int] | None = None,
    limits: SnapshotResourceLimits | None = None,
) -> None:
    """Exclusively replace every user schema in one PostgreSQL transaction.

    ``pg_restore`` first expands the custom archive into an anonymous
    temporary file. A target ``psql`` session is then opened before a
    maintenance-database controller disables new connections and terminates
    every unprotected target session. The same target session validates its
    physical identity, removes all non-system schemas, and replays the dump in
    one transaction. Connections are re-enabled on every handled exit path.
    """
    pg_restore = _require_pg_tool("pg_restore", action="restore")
    psql = _require_pg_tool("psql", action="restore")
    env = libpq_env(database_url)
    effective_limits = limits or SnapshotResourceLimits()
    tool_timeout = _postgres_tool_timeout_seconds()
    identity_mismatch_marker = (
        f"{_POSTGRES_IDENTITY_MISMATCH_MARKER}:{uuid.uuid4().hex}"
    )
    env["HEALTHMES_IDENTITY_MISMATCH_MARKER"] = identity_mismatch_marker
    with tempfile.TemporaryFile(
        mode="w+",
        encoding="utf-8",
        newline="",
    ) as restore_sql:
        _render_pg_restore_sql(
            pg_restore,
            dump_path,
            restore_sql,
            env=env,
            limits=effective_limits,
        )

        restore_sql.seek(0)
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
            try:
                target = subprocess.Popen(
                    [
                        str(psql),
                        "--no-psqlrc",
                        "--quiet",
                        "--tuples-only",
                        "--no-align",
                        "--set=ON_ERROR_STOP=1",
                        f"--dbname={libpq_url(database_url)}",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=stderr_file,
                    text=True,
                    bufsize=1,
                    env=env,
                )
            except OSError as exc:
                raise _PostgresRestoreNotStarted(
                    f"could not start PostgreSQL restore session: {exc}"
                ) from exc

            if target.stdin is None or target.stdout is None:
                _stop_postgres_target(target)
                raise _PostgresRestoreNotStarted(
                    "PostgreSQL restore session pipes are unavailable"
                )

            pid_queue: queue.Queue[int | None] = queue.Queue(maxsize=1)
            stdout_lines: list[str] = []

            def consume_target_stdout() -> None:
                assert target.stdout is not None
                found_pid = False
                for line in target.stdout:
                    stdout_lines.append(line)
                    stripped = line.strip()
                    if not stripped.startswith(_POSTGRES_TARGET_PID_MARKER):
                        continue
                    try:
                        backend_pid = int(
                            stripped.removeprefix(_POSTGRES_TARGET_PID_MARKER)
                        )
                    except ValueError:
                        continue
                    if backend_pid > 0 and not found_pid:
                        found_pid = True
                        pid_queue.put(backend_pid)
                if not found_pid:
                    pid_queue.put(None)

            stdout_thread = threading.Thread(
                target=consume_target_stdout,
                name="healthmes-postgres-restore-stdout",
                daemon=True,
            )
            stdout_thread.start()

            maintenance_url: str | None = None
            connection_fence_attempted = False
            commit_attempted = False
            primary_error: BaseException | None = None
            connection_fence_error: BaseException | None = None
            applied_returncode: int | None = None
            restore_writer: threading.Thread | None = None
            restore_writer_errors: list[BaseException] = []
            restore_writer_done = threading.Event()
            try:
                target.stdin.write(
                    "SELECT "
                    f"'{_POSTGRES_TARGET_PID_MARKER}' || pg_backend_pid()::text;\n"
                )
                target.stdin.flush()
                try:
                    target_backend_pid = pid_queue.get(
                        timeout=_POSTGRES_TARGET_START_TIMEOUT_SECONDS
                    )
                except queue.Empty as exc:
                    raise _PostgresRestoreNotStarted(
                        "timed out waiting for the PostgreSQL restore session"
                    ) from exc
                if target_backend_pid is None:
                    detail = _postgres_process_stderr(stderr_file) or "no output"
                    raise _PostgresRestoreNotStarted(
                        "PostgreSQL restore session exited before reporting its "
                        f"backend PID: {detail}"
                    )
                target.stdin.write(f"\\o {os.devnull}\n")
                target.stdin.flush()

                connection_fence_attempted = True
                maintenance_url = _set_postgres_connections_allowed(
                    database_url,
                    allowed=False,
                )
                protected = set(protected_backend_pids or ())
                protected.add(target_backend_pid)
                _terminate_postgres_target_sessions(
                    database_url,
                    maintenance_url=maintenance_url,
                    protected_backend_pids=protected,
                )

                restore_deadline = monotonic() + tool_timeout
                target_stdin = target.stdin

                def feed_restore_sql() -> None:
                    nonlocal commit_attempted
                    try:
                        target_stdin.write("BEGIN;\n")
                        target_stdin.write(
                            _postgres_identity_assertion(
                                expected_identity,
                                mismatch_marker=identity_mismatch_marker,
                            )
                            + "\n"
                        )
                        target_stdin.write(_postgres_schema_reset() + "\n")
                        for chunk in iter(
                            lambda: restore_sql.read(1024 * 1024),
                            "",
                        ):
                            target_stdin.write(chunk)
                        commit_attempted = True
                        target_stdin.write("\nCOMMIT;\n")
                        target_stdin.close()
                    except BaseException as exc:
                        restore_writer_errors.append(exc)
                    finally:
                        restore_writer_done.set()

                restore_writer = threading.Thread(
                    target=feed_restore_sql,
                    name="healthmes-postgres-restore-stdin",
                    daemon=True,
                )
                restore_writer.start()
                while True:
                    if restore_writer_errors:
                        raise restore_writer_errors[0]
                    if restore_writer_done.is_set():
                        break
                    if target.poll() is not None:
                        restore_writer_done.wait(
                            timeout=_POSTGRES_TARGET_STOP_TIMEOUT_SECONDS
                        )
                        if restore_writer_errors:
                            raise restore_writer_errors[0]
                        break
                    if monotonic() >= restore_deadline:
                        raise BackupError(
                            "psql restore timed out after "
                            f"{tool_timeout:g} seconds"
                        )
                    sleep(_POSTGRES_TOOL_POLL_SECONDS)
                remaining = restore_deadline - monotonic()
                if remaining <= 0:
                    raise BackupError(
                        "psql restore timed out after "
                        f"{tool_timeout:g} seconds"
                    )
                try:
                    applied_returncode = target.wait(timeout=remaining)
                except subprocess.TimeoutExpired as exc:
                    raise BackupError(
                        "psql restore timed out after "
                        f"{tool_timeout:g} seconds"
                    ) from exc
                stdout_thread.join(timeout=_POSTGRES_TARGET_STOP_TIMEOUT_SECONDS)
            except BaseException as exc:
                primary_error = exc
            finally:
                try:
                    _stop_postgres_target(target)
                except BaseException as exc:
                    if primary_error is None:
                        primary_error = exc
                    else:
                        primary_error = BackupError(
                            f"{primary_error}; PostgreSQL restore session could "
                            f"not be stopped: {exc}"
                        )
                target_stdin = target.stdin
                if target_stdin is not None and not target_stdin.closed:
                    try:
                        target_stdin.close()
                    except OSError as exc:
                        if primary_error is None:
                            primary_error = BackupError(
                                "PostgreSQL restore input pipe could not be "
                                f"closed: {exc}"
                            )
                if restore_writer is not None:
                    restore_writer.join(
                        timeout=_POSTGRES_TARGET_STOP_TIMEOUT_SECONDS
                    )
                    if restore_writer.is_alive():
                        writer_error = BackupError(
                            "PostgreSQL restore input writer did not stop after "
                            "the target process was terminated"
                        )
                        if primary_error is None:
                            primary_error = writer_error
                        else:
                            primary_error = BackupError(
                                f"{primary_error}; {writer_error}"
                            )
                stdout_thread.join(timeout=_POSTGRES_TARGET_STOP_TIMEOUT_SECONDS)
                if connection_fence_attempted:
                    try:
                        maintenance_url = _set_postgres_connections_allowed(
                            database_url,
                            allowed=True,
                            maintenance_url=maintenance_url,
                        )
                    except BaseException as exc:
                        try:
                            allowed, verified_url = _postgres_connections_allowed(
                                database_url,
                                maintenance_url=maintenance_url,
                            )
                            maintenance_url = verified_url
                            if not allowed:
                                raise BackupError(
                                    "PostgreSQL target still rejects new connections"
                                )
                        except BaseException as verification_exc:
                            connection_fence_error = BackupError(
                                "PostgreSQL target connections could not be "
                                f"re-enabled: {exc}; admission verification failed: "
                                f"{verification_exc}"
                            )

            detail = (
                _postgres_process_stderr(stderr_file)
                or "".join(stdout_lines).strip()
                or "no output"
            )
            identity_mismatch = identity_mismatch_marker in detail
            if connection_fence_error is not None:
                if applied_returncode == 0:
                    restore_outcome = "committed"
                elif not commit_attempted or identity_mismatch:
                    restore_outcome = "not_started"
                else:
                    restore_outcome = "unknown"
                message = str(connection_fence_error)
                if primary_error is not None:
                    message = f"{primary_error}; {message}"
                raise _PostgresConnectionFenceUncertain(
                    message,
                    restore_outcome=restore_outcome,
                ) from connection_fence_error
            if identity_mismatch:
                raise _PostgresRestoreNotStarted(
                    "PostgreSQL restore target identity changed inside the "
                    "restore transaction; restore SQL was not executed"
                )
            if primary_error is not None:
                if isinstance(primary_error, _PostgresRestoreNotStarted):
                    raise primary_error
                if not commit_attempted:
                    raise _PostgresRestoreNotStarted(
                        f"PostgreSQL restore did not start: {primary_error}"
                    ) from primary_error
                if isinstance(primary_error, BackupError):
                    raise primary_error
                raise BackupError(f"PostgreSQL restore failed: {primary_error}") from primary_error
            if applied_returncode == 0:
                return
            raise BackupError(
                f"psql restore failed (exit {applied_returncode}): {detail}"
            )


def _preflight_pg_dump(dump_path: Path) -> None:
    """Require a readable custom-format dump and an available pg_restore."""
    _run_pg_tool(
        "pg_restore",
        ["--list", str(dump_path)],
        action="inspect",
    )


def _preflight_pg_target(database_url: str) -> tuple[str, int]:
    """Return the physical PostgreSQL cluster/database identity read-only."""
    output = _run_pg_tool(
        "psql",
        [
            "--no-psqlrc",
            "--set=ON_ERROR_STOP=1",
            "--tuples-only",
            "--no-align",
            f"--dbname={libpq_url(database_url)}",
            "--command="
            "SELECT json_build_array("
            "(pg_control_system()).system_identifier::text, "
            "oid::text"
            ")::text "
            "FROM pg_database WHERE datname = current_database()",
        ],
        action="preflight",
        env=libpq_env(database_url),
    )
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) != 1:
        raise BackupError("psql preflight returned an unexpected PostgreSQL target identity")
    try:
        payload = json.loads(lines[0])
        if (
            not isinstance(payload, list)
            or len(payload) != 2
            or not all(isinstance(value, str) for value in payload)
        ):
            raise ValueError
        system_identifier, database_oid_text = payload
        database_oid = int(database_oid_text)
        if not system_identifier or database_oid <= 0:
            raise ValueError
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise BackupError("psql preflight returned an invalid PostgreSQL target identity") from exc
    return system_identifier, database_oid


# ---------------------------------------------------------------------------
# Staging (export side)
# ---------------------------------------------------------------------------


def _sqlite_file_path(database_url: str) -> Path:
    url = make_url(database_url)
    if url.database in _SQLITE_MEMORY_DATABASES:
        raise BackupError(
            "cannot snapshot an in-memory sqlite database; "
            "point HEALTHMES_DATABASE_URL at a file or postgres database"
        )
    return Path(url.database)


def _sqlite_snapshot_to(
    source: Path,
    dest: Path,
    *,
    stage: Path,
    budget: _StageBudget,
) -> None:
    """Consistent point-in-time copy of a (possibly live) sqlite database.

    The weekly backup job runs in the same process as the 10-minute trigger
    sweep and the hourly energy persist, all writing to this file; a plain
    ``shutil.copy2`` can read pages mid-commit and drops the -journal/-wal
    sidecar, yielding a torn copy that only fails at disaster-recovery time.
    ``sqlite3.Connection.backup`` takes the database lock and produces a
    transactionally consistent single-file snapshot (same pattern as
    vendor/hermes-agent/hermes_cli/backup.py::_safe_copy_db).
    """
    _ensure_stage_directory(stage, dest.parent, budget=budget)
    arcname = dest.relative_to(stage).as_posix()
    src_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    completed = False
    try:
        page_size_row = src_conn.execute("PRAGMA page_size").fetchone()
        page_count_row = src_conn.execute("PRAGMA page_count").fetchone()
        if (
            page_size_row is None
            or page_count_row is None
            or not isinstance(page_size_row[0], int)
            or not isinstance(page_count_row[0], int)
            or page_size_row[0] <= 0
            or page_count_row[0] < 0
        ):
            raise BackupError("sqlite snapshot reported an invalid page layout")
        page_size = page_size_row[0]
        expected_size = page_size * page_count_row[0]
        budget.reserve_file(arcname, expected_size)
        _require_disk_capacity(
            dest.parent,
            payload_bytes=expected_size,
            limits=budget.limits,
            label=f"SQLite snapshot {arcname}",
        )
        dst_conn = sqlite3.connect(str(dest))
        try:
            def check_progress(
                _status: int,
                _remaining: int,
                total_pages: int,
            ) -> None:
                if total_pages * page_size > expected_size:
                    raise BackupError(
                        "sqlite database grew beyond its reserved snapshot size"
                    )

            src_conn.backup(
                dst_conn,
                pages=1,
                progress=check_progress,
            )
        finally:
            dst_conn.close()
        actual_size = dest.stat().st_size
        if actual_size != expected_size:
            raise BackupError(
                "sqlite database changed size while its snapshot was being created"
            )
        fsync_path(dest)
        completed = True
    except BackupError:
        raise
    except sqlite3.Error as exc:
        raise BackupError(f"sqlite snapshot of {source} failed: {exc}") from exc
    except OSError as exc:
        raise BackupError(f"sqlite snapshot of {source} failed: {exc}") from exc
    finally:
        src_conn.close()
        if not completed:
            dest.unlink(missing_ok=True)


def _stage_healthmes_db(
    database_url: str,
    stage: Path,
    *,
    budget: _StageBudget | None = None,
) -> dict[str, Any]:
    """Dump the healthmes database into the stage; returns the manifest entry."""
    effective_budget = budget or _StageBudget(SnapshotResourceLimits())
    backend = make_url(database_url).get_backend_name()
    if backend == "sqlite":
        source = _sqlite_file_path(database_url)
        if not source.is_file():
            raise BackupError(f"sqlite database file not found: {source}")
        _sqlite_snapshot_to(
            source,
            stage / HEALTHMES_SQLITE_ARCNAME,
            stage=stage,
            budget=effective_budget,
        )
        return {"kind": "sqlite_file", "arcname": HEALTHMES_SQLITE_ARCNAME}
    if backend == "postgresql":
        destination = stage / HEALTHMES_PG_DUMP_ARCNAME
        _ensure_stage_directory(stage, destination.parent, budget=effective_budget)
        _pg_dump_to(
            database_url,
            destination,
            limits=effective_budget.limits,
            budget=effective_budget,
            arcname=HEALTHMES_PG_DUMP_ARCNAME,
        )
        return {"kind": "pg_dump", "arcname": HEALTHMES_PG_DUMP_ARCNAME}
    raise BackupError(f"unsupported database backend for backup: {backend}")


def _stage_ow_db(
    ow_database_url: str,
    stage: Path,
    *,
    budget: _StageBudget | None = None,
) -> dict[str, Any]:
    effective_budget = budget or _StageBudget(SnapshotResourceLimits())
    backend = make_url(ow_database_url).get_backend_name()
    if backend != "postgresql":
        raise BackupError(
            f"open-wearables database URL must be postgres (vendor stack), got: {backend}"
        )
    destination = stage / OW_PG_DUMP_ARCNAME
    _ensure_stage_directory(stage, destination.parent, budget=effective_budget)
    _pg_dump_to(
        ow_database_url,
        destination,
        limits=effective_budget.limits,
        budget=effective_budget,
        arcname=OW_PG_DUMP_ARCNAME,
    )
    return {"kind": "pg_dump", "arcname": OW_PG_DUMP_ARCNAME}


def _preflight_snapshot_sources(locations: DataLocations) -> None:
    """Reject unavailable source/tool combinations before taking the write fence."""
    backend = make_url(locations.database_url).get_backend_name()
    if backend == "sqlite":
        source = _sqlite_file_path(locations.database_url)
        if not source.is_file():
            raise BackupError(f"sqlite database file not found: {source}")
    elif backend == "postgresql":
        if find_pg_tool("pg_dump") is None:
            raise BackupError(
                "pg_dump not found on PATH and no Homebrew postgres keg detected; "
                "install it (e.g. `brew install postgresql@16`) to dump a postgres database"
            )
    else:
        raise BackupError(f"unsupported database backend for backup: {backend}")
    if locations.ow_database_url:
        ow_backend = make_url(locations.ow_database_url).get_backend_name()
        if ow_backend != "postgresql":
            raise BackupError(
                f"open-wearables database URL must be postgres (vendor stack), got: {ow_backend}"
            )
        if find_pg_tool("pg_dump") is None:
            raise BackupError(
                "pg_dump not found on PATH and no Homebrew postgres keg detected; "
                "install it (e.g. `brew install postgresql@16`) to dump a postgres database"
            )


def _relative_symlink_stays_in_tree(
    parent_parts: tuple[str, ...],
    target: PurePosixPath,
) -> bool:
    resolved = list(parent_parts)
    for part in target.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not resolved:
                return False
            resolved.pop()
            continue
        resolved.append(part)
    return True


def _open_anchored_tree_directory(
    root_descriptor: int,
    relative_parts: tuple[str, ...],
    generations: dict[tuple[str, ...], _FilesystemGeneration],
    *,
    source: Path,
) -> int:
    descriptor = os.dup(root_descriptor)
    traversed: tuple[str, ...] = ()
    try:
        if not generations[()].matches(os.fstat(descriptor)):
            raise BackupError(
                f"snapshot source root changed while being copied: {source}"
            )
        for part in relative_parts:
            next_descriptor = os.open(
                part,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
            traversed = (*traversed, part)
            expected = generations.get(traversed)
            if expected is None or not expected.matches(os.fstat(descriptor)):
                raise BackupError(
                    "snapshot source directory changed while being copied: "
                    f"{source.joinpath(*traversed)}"
                )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _scan_anchored_tree_directory(
    directory_descriptor: int,
    *,
    relative_parts: tuple[str, ...],
    arcroot: str,
    budget: _StageBudget,
) -> list[tuple[str, _FilesystemGeneration]]:
    entries: list[tuple[str, _FilesystemGeneration]] = []
    with _scandir(directory_descriptor) as iterator:
        for entry in iterator:
            relative = (*relative_parts, entry.name)
            arcname = f"{arcroot}/{'/'.join(relative)}"
            budget.discover_source_entry(arcname)
            metadata = os.stat(
                entry.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            entries.append(
                (entry.name, _FilesystemGeneration.from_metadata(metadata))
            )
    entries.sort(key=lambda item: item[0])
    return entries


def _normalized_stage_symlink_target(
    link_target: str,
) -> PurePosixPath | None:
    link_path = PurePosixPath(link_target)
    canonical_target = link_path.as_posix()
    if (
        not link_target
        or "\x00" in link_target
        or "\\" in link_target
        or link_path.is_absolute()
        or (
            link_target != canonical_target
            and link_target != f"./{canonical_target}"
        )
    ):
        return None
    return link_path


def _stage_tree(
    source: Path,
    stage: Path,
    arcroot: str,
    *,
    limits: SnapshotResourceLimits | None = None,
    budget: _StageBudget | None = None,
) -> dict[str, Any]:
    """Copy ``source`` under ``stage/arcroot``; returns the manifest entry.

    Regular files, directories (including empty ones) and intra-tree
    symlinks are preserved. Symlinks whose resolved target escapes
    ``source`` are skipped and recorded (the archive must stay
    self-contained); other special files (sockets, fifos) are skipped too.
    """
    requested_source = Path(source).expanduser()
    try:
        source = requested_source.resolve(strict=True)
    except OSError as exc:
        raise BackupError(
            f"could not resolve snapshot source tree {source}: {exc}"
        ) from exc
    effective_budget = budget or _StageBudget(limits or SnapshotResourceLimits())
    file_count = 0
    total_bytes = 0
    skipped: list[dict[str, str]] = []
    _ensure_stage_directory(
        stage,
        stage / arcroot,
        budget=effective_budget,
    )
    if os.name == "nt":  # pragma: no cover - Windows lacks safe dir_fd traversal
        pending = [source]
        while pending:
            directory = pending.pop()
            relative_directory = directory.relative_to(source)
            entries: list[tuple[str, _FilesystemGeneration]] = []
            with _scandir(directory) as iterator:
                for entry in iterator:
                    relative = relative_directory / entry.name
                    arcname = f"{arcroot}/{relative.as_posix()}"
                    effective_budget.discover_source_entry(arcname)
                    entries.append(
                        (
                            entry.name,
                            _FilesystemGeneration.from_metadata(
                                entry.stat(follow_symlinks=False)
                            ),
                        )
                    )
            for name, expected in reversed(
                sorted(entries, key=lambda item: item[0])
            ):
                path = directory / name
                relative = path.relative_to(source)
                arcname = f"{arcroot}/{relative.as_posix()}"
                target = stage / arcroot / relative
                metadata = path.lstat()
                if not expected.matches(metadata):
                    raise BackupError(
                        f"snapshot source changed while being copied: {path}"
                    )
                if stat.S_ISDIR(metadata.st_mode):
                    _ensure_stage_directory(
                        stage,
                        target,
                        budget=effective_budget,
                    )
                    pending.append(path)
                elif stat.S_ISLNK(metadata.st_mode):
                    link_target = os.readlink(path)
                    link_path = _normalized_stage_symlink_target(link_target)
                    if link_path is None:
                        skipped.append(
                            {
                                "path": arcname,
                                "reason": "symlink-target-not-normalized",
                                "target": link_target,
                            }
                        )
                    elif _relative_symlink_stays_in_tree(
                        relative.parent.parts,
                        link_path,
                    ):
                        _ensure_stage_directory(
                            stage,
                            target.parent,
                            budget=effective_budget,
                        )
                        effective_budget.reserve_member(arcname)
                        os.symlink(link_target, target)
                    else:
                        skipped.append(
                            {
                                "path": arcname,
                                "reason": "symlink-outside-tree",
                                "target": link_target,
                            }
                        )
                elif stat.S_ISREG(metadata.st_mode):
                    copied = _copy_regular_file_to_stage(
                        path,
                        target,
                        stage=stage,
                        budget=effective_budget,
                        expected_source_generation=expected,
                    )
                    file_count += 1
                    total_bytes += copied
                else:
                    skipped.append(
                        {"path": arcname, "reason": "special-file"}
                    )
        return {
            "arcroot": arcroot,
            "file_count": file_count,
            "total_bytes": total_bytes,
            "skipped": skipped,
        }

    root_descriptor: int | None = None
    root_context = None
    try:
        root_context = open_directory_anchored(requested_source)
        anchored_source, root_descriptor = root_context.__enter__()
        if anchored_source != source:
            raise BackupError(
                "snapshot source changed while it was being opened: "
                f"{requested_source}"
            )
        source = anchored_source
        root_metadata = os.fstat(root_descriptor)
        requested_metadata = os.stat(
            requested_source,
            follow_symlinks=True,
        )
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_dev != requested_metadata.st_dev
            or root_metadata.st_ino != requested_metadata.st_ino
        ):
            raise BackupError(
                "snapshot source changed while it was being opened: "
                f"{requested_source}"
            )
        root_generation = _FilesystemGeneration.from_metadata(root_metadata)
        directory_generations = {(): root_generation}
        pending: list[
            tuple[
                str,
                tuple[str, ...],
                str | None,
                _FilesystemGeneration | None,
            ]
        ] = [("scan", (), None, None)]

        while pending:
            action, parent_parts, name, expected = pending.pop()
            if action == "scan":
                directory_descriptor = _open_anchored_tree_directory(
                    root_descriptor,
                    parent_parts,
                    directory_generations,
                    source=source,
                )
                try:
                    entries = _scan_anchored_tree_directory(
                        directory_descriptor,
                        relative_parts=parent_parts,
                        arcroot=arcroot,
                        budget=effective_budget,
                    )
                finally:
                    os.close(directory_descriptor)
                for entry_name, generation in reversed(entries):
                    pending.append(
                        ("entry", parent_parts, entry_name, generation)
                    )
                continue

            if action == "finish":
                assert name is not None
                relative_parts = (*parent_parts, name)
                directory_descriptor = _open_anchored_tree_directory(
                    root_descriptor,
                    relative_parts,
                    directory_generations,
                    source=source,
                )
                os.close(directory_descriptor)
                continue

            assert name is not None
            assert expected is not None
            relative_parts = (*parent_parts, name)
            relative = Path(*relative_parts)
            arcname = f"{arcroot}/{relative.as_posix()}"
            target = stage / arcroot / relative
            source_path = source.joinpath(*relative_parts)
            parent_descriptor = _open_anchored_tree_directory(
                root_descriptor,
                parent_parts,
                directory_generations,
                source=source,
            )
            try:
                metadata = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if not expected.matches(metadata):
                    raise BackupError(
                        "snapshot source changed while being copied: "
                        f"{source_path}"
                    )
                if stat.S_ISDIR(metadata.st_mode):
                    child_descriptor = os.open(
                        name,
                        os.O_RDONLY
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=parent_descriptor,
                    )
                    try:
                        if not expected.matches(os.fstat(child_descriptor)):
                            raise BackupError(
                                "snapshot source directory changed while being "
                                f"copied: {source_path}"
                            )
                    finally:
                        os.close(child_descriptor)
                    _ensure_stage_directory(
                        stage,
                        target,
                        budget=effective_budget,
                    )
                    directory_generations[relative_parts] = expected
                    pending.append(("finish", parent_parts, name, expected))
                    pending.append(("scan", relative_parts, None, None))
                elif stat.S_ISLNK(metadata.st_mode):
                    link_target = os.readlink(
                        name,
                        dir_fd=parent_descriptor,
                    )
                    if not expected.matches(
                        os.stat(
                            name,
                            dir_fd=parent_descriptor,
                            follow_symlinks=False,
                        )
                    ):
                        raise BackupError(
                            "snapshot source symlink changed while being copied: "
                            f"{source_path}"
                        )
                    link_path = _normalized_stage_symlink_target(link_target)
                    if link_path is None:
                        skipped.append(
                            {
                                "path": arcname,
                                "reason": "symlink-target-not-normalized",
                                "target": link_target,
                            }
                        )
                    elif _relative_symlink_stays_in_tree(
                        parent_parts,
                        link_path,
                    ):
                        _ensure_stage_directory(
                            stage,
                            target.parent,
                            budget=effective_budget,
                        )
                        effective_budget.reserve_member(arcname)
                        os.symlink(link_target, target)
                    else:
                        skipped.append(
                            {
                                "path": arcname,
                                "reason": "symlink-outside-tree",
                                "target": link_target,
                            }
                        )
                elif stat.S_ISREG(metadata.st_mode):
                    copied = _copy_regular_file_to_stage(
                        source_path,
                        target,
                        stage=stage,
                        budget=effective_budget,
                        source_parent_descriptor=parent_descriptor,
                        source_name=name,
                        expected_source_generation=expected,
                    )
                    file_count += 1
                    total_bytes += copied
                else:
                    skipped.append(
                        {"path": arcname, "reason": "special-file"}
                    )
            finally:
                os.close(parent_descriptor)

        if (
            not root_generation.matches(os.fstat(root_descriptor))
            or not root_generation.matches(
                os.stat(source, follow_symlinks=False)
            )
            or not root_generation.matches(
                os.stat(requested_source, follow_symlinks=True)
            )
        ):
            raise BackupError(
                f"snapshot source root changed while being copied: {source}"
            )
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError(
            f"could not stage snapshot source tree {source}: {exc}"
        ) from exc
    finally:
        if root_context is not None:
            root_context.__exit__(None, None, None)
    return {
        "arcroot": arcroot,
        "file_count": file_count,
        "total_bytes": total_bytes,
        "skipped": skipped,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bounded_stage_paths(
    stage: Path,
    *,
    max_members: int,
) -> list[Path]:
    """Materialize at most one configured archive tree before rejecting it."""
    discovered = 0
    pending = [stage]
    paths: list[Path] = []
    while pending:
        directory = pending.pop()
        with _scandir(directory) as iterator:
            for entry in iterator:
                discovered += 1
                if discovered > max_members:
                    raise BackupError(
                        "snapshot contains more than "
                        f"{max_members} archive members"
                    )
                path = directory / entry.name
                paths.append(path)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
    paths.sort(key=lambda path: path.relative_to(stage).as_posix())
    return paths


def _build_inventory(
    stage: Path,
    *,
    max_members: int,
) -> list[dict[str, Any]]:
    """Inventory every archived file/symlink (manifest.json itself excluded)."""
    entries: list[dict[str, Any]] = []
    for path in _bounded_stage_paths(stage, max_members=max_members):
        rel = path.relative_to(stage).as_posix()
        if rel == MANIFEST_ARCNAME:
            continue
        if path.is_symlink():
            entries.append({"path": rel, "kind": "symlink", "target": os.readlink(path)})
        elif path.is_file():
            entries.append(
                {
                    "path": rel,
                    "kind": "file",
                    "size_bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
    return entries


class _BoundedWriteProxy:
    def __init__(
        self,
        handle,
        *,
        limit: int,
        limits: SnapshotResourceLimits,
        capacity_path: Path,
        label: str,
    ) -> None:
        self._handle = handle
        self._limit = limit
        self._limits = limits
        self._capacity_path = capacity_path
        self._label = label
        self.bytes_written = 0

    def write(self, payload: bytes) -> int:
        if self.bytes_written + len(payload) > self._limit:
            raise BackupError(
                f"{self._label} exceeds the configured {self._limit}-byte limit"
            )
        _require_disk_capacity(
            self._capacity_path,
            payload_bytes=len(payload),
            limits=self._limits,
            label=self._label,
        )
        written = self._handle.write(payload)
        self.bytes_written += written
        return written

    def __getattr__(self, name: str):
        return getattr(self._handle, name)


def _tar_gz_bytes(
    stage: Path,
    *,
    limits: SnapshotResourceLimits,
) -> bytes:
    stage_paths = _bounded_stage_paths(
        stage,
        max_members=limits.max_members,
    )
    member_count = 0
    expanded_bytes = 0
    for path in stage_paths:
        member_count += 1
        if path.is_file() and not path.is_symlink():
            size = path.stat().st_size
            if size > limits.max_member_bytes:
                raise BackupError(
                    f"{path.relative_to(stage)} exceeds the configured "
                    f"{limits.max_member_bytes}-byte limit"
                )
            expanded_bytes += size
    if member_count > limits.max_members:
        raise BackupError(
            f"snapshot contains more than {limits.max_members} archive members"
        )
    if expanded_bytes > limits.max_expanded_bytes:
        raise BackupError(
            f"snapshot expands beyond the configured {limits.max_expanded_bytes}-byte limit"
        )
    with tempfile.TemporaryFile(mode="w+b") as compressed:
        bounded = _BoundedWriteProxy(
            compressed,
            limit=limits.max_decrypted_bytes,
            limits=limits,
            capacity_path=Path(tempfile.gettempdir()),
            label="compressed snapshot payload",
        )
        with tarfile.open(fileobj=bounded, mode="w:gz") as tar:
            for path in stage_paths:
                tar.add(
                    path,
                    arcname=path.relative_to(stage).as_posix(),
                    recursive=False,
                )
        compressed.seek(0)
        payload = compressed.read(limits.max_decrypted_bytes + 1)
    if len(payload) > limits.max_decrypted_bytes:
        raise BackupError(
            "compressed snapshot payload exceeds the configured "
            f"{limits.max_decrypted_bytes}-byte limit"
        )
    if payload and expanded_bytes / len(payload) > limits.max_compression_ratio:
        raise BackupError("snapshot compression ratio exceeds the configured limit")
    return payload


def _tree_recovery_status(
    configured_path: Path | None,
    manifest_entry: dict[str, Any] | None,
) -> str:
    if manifest_entry is not None:
        return "included"
    if configured_path is None:
        return "not_configured"
    return "source_not_present"


def _build_recovery_metadata(
    locations: DataLocations,
    contents: dict[str, Any],
) -> dict[str, Any]:
    warning = partial_backup_warning(locations)
    ow_entry = contents["open_wearables_db"]
    if ow_entry is not None:
        ow_status = "included"
    elif locations.ow_runtime_configured:
        ow_status = "omitted_missing_dump_url"
    else:
        ow_status = "not_configured"
    return {
        "scope": RECOVERY_SCOPE_PARTIAL_COMPONENT,
        "full_node_recovery": False,
        "components": {
            "healthmes_db": {"status": "included"},
            "media": {"status": _tree_recovery_status(locations.media_dir, contents["media"])},
            "raw_ingest": {
                "status": _tree_recovery_status(locations.raw_ingest_dir, contents["raw_ingest"])
            },
            "open_wearables_db": {
                "status": ow_status,
                "runtime_configured": locations.ow_runtime_configured,
                "dump_configured": bool(locations.ow_database_url),
            },
            "hermes_home": {
                "status": _tree_recovery_status(locations.hermes_home, contents["hermes_home"])
            },
        },
        "operational_warnings": [warning] if warning else [],
    }


# ---------------------------------------------------------------------------
# Public API — create / read / restore
# ---------------------------------------------------------------------------


def create_snapshot(
    locations: DataLocations,
    *,
    passphrase: str,
    out_path: Path,
    created_at: datetime,
) -> dict[str, Any]:
    """Build the encrypted snapshot envelope at ``out_path``.

    ``created_at`` (timezone-aware) is injected by the caller — providers
    own the clock. Returns the manifest that was sealed into the envelope.
    The output file appears atomically (temp file + rename), so a crashed
    run never leaves a half-written ``*.tar.gz.age`` behind.
    """
    _require_aware(created_at)
    if not passphrase:
        raise BackupError(
            "no backup passphrase configured; set HEALTHMES_BACKUP_PASSPHRASE "
            "(losing it makes every snapshot unrecoverable)"
        )
    _preflight_snapshot_sources(locations)
    with tempfile.TemporaryDirectory(prefix="healthmes-backup-") as tmp:
        stage = Path(tmp) / "stage"
        stage.mkdir()
        stage_budget = _StageBudget(locations.resource_limits)

        contents: dict[str, Any] = {
            "healthmes_db": None,
            "open_wearables_db": None,
            "media": None,
            "raw_ingest": None,
            "hermes_home": None,
        }
        try:
            with (
                _postgres_tool_timeout_scope(
                    locations.postgres_tool_timeout_seconds
                ),
                payload_generation_guard(locations.database_url),
            ):
                with global_write_plane_guard(locations.database_url):
                    contents["healthmes_db"] = _stage_healthmes_db(
                        locations.database_url,
                        stage,
                        budget=stage_budget,
                    )
                    if (
                        locations.media_dir is not None
                        and locations.media_dir.is_dir()
                    ):
                        contents["media"] = _stage_tree(
                            locations.media_dir,
                            stage,
                            MEDIA_ARCROOT,
                            limits=locations.resource_limits,
                            budget=stage_budget,
                        )
                    if (
                        locations.raw_ingest_dir is not None
                        and locations.raw_ingest_dir.is_dir()
                    ):
                        contents["raw_ingest"] = _stage_tree(
                            locations.raw_ingest_dir,
                            stage,
                            RAW_INGEST_ARCROOT,
                            limits=locations.resource_limits,
                            budget=stage_budget,
                        )
                if locations.ow_database_url:
                    contents["open_wearables_db"] = _stage_ow_db(
                        locations.ow_database_url,
                        stage,
                        budget=stage_budget,
                    )
                if (
                    locations.hermes_home is not None
                    and locations.hermes_home.is_dir()
                ):
                    contents["hermes_home"] = _stage_tree(
                        locations.hermes_home,
                        stage,
                        HERMES_ARCROOT,
                        limits=locations.resource_limits,
                        budget=stage_budget,
                    )
        except TimeoutError as exc:
            raise BackupError(
                "timed out waiting for the HealthMes payload generation or "
                "write plane; snapshot was not created"
            ) from exc
        except (SQLAlchemyError, OSError) as exc:
            raise BackupError(
                "could not acquire or use the HealthMes payload generation "
                "or write plane; "
                f"snapshot was not created: {exc}"
            ) from exc

        manifest: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "created_at": created_at.isoformat(),
            "healthmes_version": __version__,
            "recovery": _build_recovery_metadata(locations, contents),
            "contents": contents,
            "inventory": _build_inventory(
                stage,
                max_members=locations.resource_limits.max_members,
            ),
        }
        manifest_payload = (
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        stage_budget.reserve_file(MANIFEST_ARCNAME, len(manifest_payload))
        _require_disk_capacity(
            stage,
            payload_bytes=len(manifest_payload),
            limits=locations.resource_limits,
            label="snapshot manifest",
        )
        manifest_path = stage / MANIFEST_ARCNAME
        with manifest_path.open("xb") as manifest_file:
            manifest_file.write(manifest_payload)
            manifest_file.flush()
            os.fsync(manifest_file.fileno())

        plaintext = _tar_gz_bytes(stage, limits=locations.resource_limits)
        if len(plaintext) > locations.resource_limits.max_encrypted_bytes:
            raise BackupError(
                "compressed snapshot payload cannot fit within the configured "
                f"{locations.resource_limits.max_encrypted_bytes}-byte encrypted limit"
            )
        ciphertext = age_passphrase.encrypt(plaintext, passphrase)
        if len(ciphertext) > locations.resource_limits.max_encrypted_bytes:
            raise BackupError(
                "encrypted snapshot exceeds the configured "
                f"{locations.resource_limits.max_encrypted_bytes}-byte limit"
            )

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _require_disk_capacity(
            out_path.parent,
            payload_bytes=len(ciphertext),
            limits=locations.resource_limits,
            label="encrypted snapshot output",
        )
        with durable_atomic_writer(
            out_path,
            replace_existing=False,
        ) as output:
            output.write(ciphertext)
    except FileExistsError:
        raise
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError(
            f"could not write encrypted snapshot {out_path}: {exc}"
        ) from exc
    logger.info("Snapshot written: %s (%d bytes encrypted)", out_path, len(ciphertext))
    for warning in manifest["recovery"]["operational_warnings"]:
        logger.warning("%s Snapshot written: %s", warning, out_path)
    return manifest


def _decrypt_snapshot(
    path: Path,
    passphrase: str,
    *,
    limits: SnapshotResourceLimits,
) -> bytes:
    if not passphrase:
        raise BackupError(
            "no backup passphrase configured; set HEALTHMES_BACKUP_PASSPHRASE to decrypt snapshots"
        )
    try:
        with open_regular_file(path) as handle:
            return _decrypt_snapshot_handle(
                path,
                handle,
                passphrase,
                limits=limits,
            )
    except FileNotFoundError as exc:
        raise BackupError(f"snapshot not found: {path}") from exc
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError(f"could not read encrypted snapshot {path}: {exc}") from exc


def _decrypt_snapshot_handle(
    path: Path,
    handle: BinaryIO,
    passphrase: str,
    *,
    limits: SnapshotResourceLimits,
) -> bytes:
    """Decrypt one already-open, no-follow snapshot file generation."""
    if not passphrase:
        raise BackupError(
            "no backup passphrase configured; set HEALTHMES_BACKUP_PASSPHRASE to decrypt snapshots"
        )
    try:
        decrypted = age_passphrase.decrypt(
            _bounded_descriptor_bytes(
                handle,
                path=path,
                limit=limits.max_encrypted_bytes,
                label="encrypted snapshot",
            ),
            passphrase,
        )
    except pyrage.DecryptError as exc:
        raise WrongPassphraseError(
            f"could not decrypt {path.name}: wrong passphrase or corrupted snapshot"
        ) from exc
    if len(decrypted) > limits.max_decrypted_bytes:
        raise SnapshotIntegrityError(
            "decrypted snapshot exceeds the configured "
            f"{limits.max_decrypted_bytes}-byte limit"
        )
    return decrypted


def _normalized_relative_posix(value: Any, *, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise SnapshotIntegrityError(f"{field} must be a non-empty relative POSIX path")
    if "\x00" in value or "\\" in value:
        raise SnapshotIntegrityError(f"{field} is not a normalized POSIX path: {value!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise SnapshotIntegrityError(f"{field} contains an unsafe path segment: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise SnapshotIntegrityError(f"{field} is not a normalized relative path: {value!r}")
    return path


def _normalized_relative_symlink_target(
    value: Any,
    *,
    field: str,
) -> PurePosixPath:
    """Validate a relative POSIX symlink target without rejecting safe ``..``.

    Older snapshots can contain links such as ``../shared/file``. Their
    containment is checked against the owning component after lexical
    resolution, so rejecting every parent segment would break valid backups
    without improving traversal safety.
    """
    if not isinstance(value, str) or not value:
        raise SnapshotIntegrityError(f"{field} must be a non-empty relative POSIX path")
    if "\x00" in value or "\\" in value:
        raise SnapshotIntegrityError(f"{field} is not a normalized POSIX path: {value!r}")
    path = PurePosixPath(value)
    canonical = path.as_posix()
    if path.is_absolute() or (value != canonical and value != f"./{canonical}"):
        raise SnapshotIntegrityError(f"{field} is not a normalized relative path: {value!r}")
    return path


def _resolve_posix_symlink(
    link_path: PurePosixPath,
    target: PurePosixPath,
    *,
    root: PurePosixPath,
) -> PurePosixPath:
    parts = list(link_path.parent.parts)
    root_depth = len(root.parts)
    for part in target.parts:
        if part == ".":
            continue
        if part == "..":
            if len(parts) <= root_depth:
                raise SnapshotIntegrityError(
                    f"symlink target escapes component root: {link_path.as_posix()}"
                )
            parts.pop()
            continue
        parts.append(part)
    resolved = PurePosixPath(*parts)
    if (
        not _under(resolved, root)
        or resolved == link_path
        or _under(resolved, link_path)
    ):
        raise SnapshotIntegrityError(
            f"symlink target escapes or loops within component: {link_path.as_posix()}"
        )
    return resolved


def _under(path: PurePosixPath, root: PurePosixPath) -> bool:
    return path == root or path.parts[: len(root.parts)] == root.parts


def _validate_recovery_metadata(
    manifest: dict[str, Any],
    contents: dict[str, Any],
) -> None:
    recovery = manifest.get("recovery")
    if recovery is None:
        return
    if not isinstance(recovery, dict):
        raise SnapshotIntegrityError("manifest recovery metadata must be an object")
    if recovery.get("scope") != RECOVERY_SCOPE_PARTIAL_COMPONENT:
        raise SnapshotIntegrityError("manifest recovery scope is unsupported")
    if recovery.get("full_node_recovery") is not False:
        raise SnapshotIntegrityError("manifest must not claim full-node recovery")
    components = recovery.get("components")
    if not isinstance(components, dict):
        raise SnapshotIntegrityError("manifest recovery components must be an object")
    health = components.get("healthmes_db")
    if not isinstance(health, dict) or health.get("status") != "included":
        raise SnapshotIntegrityError("recovery metadata contradicts healthmes_db contents")
    for component in ("media", "raw_ingest", "hermes_home"):
        metadata = components.get(component)
        if not isinstance(metadata, dict):
            raise SnapshotIntegrityError(f"recovery metadata is missing {component}")
        included = contents.get(component) is not None
        status = metadata.get("status")
        if status not in {"included", "source_not_present", "not_configured"}:
            raise SnapshotIntegrityError(f"recovery metadata has unsupported {component} status")
        if included != (status == "included"):
            raise SnapshotIntegrityError(f"recovery metadata contradicts {component} contents")
    wearable = components.get("open_wearables_db")
    if not isinstance(wearable, dict):
        raise SnapshotIntegrityError("recovery metadata is missing open_wearables_db")
    if not isinstance(wearable.get("runtime_configured"), bool) or not isinstance(
        wearable.get("dump_configured"), bool
    ):
        raise SnapshotIntegrityError("Open Wearables recovery configuration flags must be booleans")
    status = wearable.get("status")
    if status not in {"included", "omitted_missing_dump_url", "not_configured"}:
        raise SnapshotIntegrityError("recovery metadata has unsupported open_wearables_db status")
    included = contents.get("open_wearables_db") is not None
    if included != (status == "included"):
        raise SnapshotIntegrityError("recovery metadata contradicts open_wearables_db contents")
    if included != wearable["dump_configured"]:
        raise SnapshotIntegrityError(
            "Open Wearables dump configuration contradicts snapshot contents"
        )
    if status == "omitted_missing_dump_url" and (
        not wearable["runtime_configured"] or wearable["dump_configured"]
    ):
        raise SnapshotIntegrityError(
            "Open Wearables omission metadata contradicts runtime/dump configuration"
        )
    if status == "not_configured" and (
        wearable["runtime_configured"] or wearable["dump_configured"]
    ):
        raise SnapshotIntegrityError(
            "Open Wearables not_configured metadata contradicts configuration"
        )
    warnings = recovery.get("operational_warnings")
    if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
        raise SnapshotIntegrityError("recovery operational_warnings must be strings")
    if status == "omitted_missing_dump_url" and not warnings:
        raise SnapshotIntegrityError("Open Wearables omission must include an operational warning")


def _validate_manifest_layout(manifest: dict[str, Any]) -> _ManifestLayout:
    version = manifest.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise SnapshotIntegrityError(f"snapshot manifest has invalid schema_version: {version!r}")
    if version > SCHEMA_VERSION:
        raise BackupError(
            f"snapshot schema_version {version} is newer than this tool "
            f"(supports up to {SCHEMA_VERSION}); upgrade healthmes to restore it"
        )
    raw_contents = manifest.get("contents")
    if not isinstance(raw_contents, dict):
        raise SnapshotIntegrityError("snapshot manifest has no contents object")
    unknown = set(raw_contents) - set(_COMPONENT_ORDER)
    if unknown:
        raise SnapshotIntegrityError(
            f"snapshot manifest contains unsupported components: {sorted(unknown)}"
        )
    contents = {name: raw_contents.get(name) for name in _COMPONENT_ORDER}
    health = contents["healthmes_db"]
    if not isinstance(health, dict):
        raise SnapshotIntegrityError("snapshot manifest has no healthmes_db component")
    health_kind = health.get("kind")
    expected_health_arcname = {
        "sqlite_file": HEALTHMES_SQLITE_ARCNAME,
        "pg_dump": HEALTHMES_PG_DUMP_ARCNAME,
    }.get(health_kind)
    if expected_health_arcname is None:
        raise SnapshotIntegrityError(f"healthmes_db has unsupported kind: {health_kind!r}")
    health_arcname = _normalized_relative_posix(
        health.get("arcname"),
        field="contents.healthmes_db.arcname",
    )
    if health_arcname.as_posix() != expected_health_arcname:
        raise SnapshotIntegrityError("healthmes_db arcname does not match its kind")

    component_paths: dict[str, PurePosixPath] = {
        "healthmes_db": health_arcname,
    }
    wearable = contents["open_wearables_db"]
    if wearable is not None:
        if not isinstance(wearable, dict) or wearable.get("kind") != "pg_dump":
            raise SnapshotIntegrityError("open_wearables_db must be a pg_dump object")
        wearable_arcname = _normalized_relative_posix(
            wearable.get("arcname"),
            field="contents.open_wearables_db.arcname",
        )
        if wearable_arcname.as_posix() != OW_PG_DUMP_ARCNAME:
            raise SnapshotIntegrityError("open_wearables_db arcname must be db/open_wearables.dump")
        component_paths["open_wearables_db"] = wearable_arcname

    for component, expected_root in _TREE_COMPONENT_ROOTS.items():
        entry = contents[component]
        if entry is None:
            continue
        if not isinstance(entry, dict):
            raise SnapshotIntegrityError(f"{component} contents must be an object")
        root = _normalized_relative_posix(
            entry.get("arcroot"),
            field=f"contents.{component}.arcroot",
        )
        if root.as_posix() != expected_root:
            raise SnapshotIntegrityError(f"{component} arcroot must be {expected_root!r}")
        for metadata_field in ("file_count", "total_bytes"):
            value = entry.get(metadata_field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise SnapshotIntegrityError(
                    f"contents.{component}.{metadata_field} must be a non-negative integer"
                )
        skipped = entry.get("skipped")
        if not isinstance(skipped, list):
            raise SnapshotIntegrityError(f"contents.{component}.skipped must be a list")
        seen_skipped: set[str] = set()
        for index, skipped_entry in enumerate(skipped):
            if not isinstance(skipped_entry, dict):
                raise SnapshotIntegrityError(
                    f"contents.{component}.skipped[{index}] must be an object"
                )
            skipped_path = _normalized_relative_posix(
                skipped_entry.get("path"),
                field=f"contents.{component}.skipped[{index}].path",
            )
            if not _under(skipped_path, root) or skipped_path == root:
                raise SnapshotIntegrityError(f"skipped path is outside the {component} root")
            if skipped_path.as_posix() in seen_skipped:
                raise SnapshotIntegrityError(f"duplicate skipped path: {skipped_path.as_posix()}")
            seen_skipped.add(skipped_path.as_posix())
            if not isinstance(skipped_entry.get("reason"), str):
                raise SnapshotIntegrityError("skipped entry reason must be a string")
        component_paths[component] = root

    locators = list(component_paths.items())
    for index, (left_name, left) in enumerate(locators):
        for right_name, right in locators[index + 1 :]:
            left_tree = left_name in _TREE_COMPONENT_ROOTS
            right_tree = right_name in _TREE_COMPONENT_ROOTS
            if (
                left == right
                or (left_tree and _under(right, left))
                or (right_tree and _under(left, right))
            ):
                raise SnapshotIntegrityError(
                    f"component roots overlap: {left_name} and {right_name}"
                )

    raw_inventory = manifest.get("inventory")
    if not isinstance(raw_inventory, list):
        raise SnapshotIntegrityError("snapshot manifest has no inventory")
    inventory: dict[str, dict[str, Any]] = {}
    owners: dict[str, str] = {}
    counts = {name: 0 for name in _TREE_COMPONENT_ROOTS}
    byte_counts = {name: 0 for name in _TREE_COMPONENT_ROOTS}
    for index, entry in enumerate(raw_inventory):
        if not isinstance(entry, dict):
            raise SnapshotIntegrityError(f"inventory[{index}] must be an object")
        path = _normalized_relative_posix(
            entry.get("path"),
            field=f"inventory[{index}].path",
        )
        path_text = path.as_posix()
        if path_text in inventory:
            raise SnapshotIntegrityError(f"duplicate inventory path: {path_text}")
        matches: list[str] = []
        for component, locator in component_paths.items():
            if component in _TREE_COMPONENT_ROOTS:
                if _under(path, locator) and path != locator:
                    matches.append(component)
            elif path == locator:
                matches.append(component)
        if len(matches) != 1:
            raise SnapshotIntegrityError(
                f"inventory path does not belong to exactly one component: {path_text}"
            )
        owner = matches[0]
        kind = entry.get("kind")
        if kind not in {"file", "symlink"}:
            raise SnapshotIntegrityError(
                f"inventory entry has unsupported kind for {path_text}: {kind!r}"
            )
        if owner not in _TREE_COMPONENT_ROOTS and kind != "file":
            raise SnapshotIntegrityError(f"database component must be a regular file: {path_text}")
        if kind == "file":
            size = entry.get("size_bytes")
            checksum = entry.get("sha256")
            if (
                not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or not isinstance(checksum, str)
                or len(checksum) != 64
                or any(char not in "0123456789abcdef" for char in checksum)
            ):
                raise SnapshotIntegrityError(f"inventory file metadata is invalid for {path_text}")
            if owner in _TREE_COMPONENT_ROOTS:
                counts[owner] += 1
                byte_counts[owner] += size
        else:
            target = _normalized_relative_symlink_target(
                entry.get("target"),
                field=f"inventory[{index}].target",
            )
            owner_root = component_paths[owner]
            _resolve_posix_symlink(path, target, root=owner_root)
        inventory[path_text] = entry
        owners[path_text] = owner

    for component in ("healthmes_db", "open_wearables_db"):
        locator = component_paths.get(component)
        if locator is None:
            continue
        entry = inventory.get(locator.as_posix())
        if entry is None or entry.get("kind") != "file":
            raise SnapshotIntegrityError(f"{component} archive member is missing from inventory")
    for component in _TREE_COMPONENT_ROOTS:
        entry = contents[component]
        if entry is None:
            continue
        if (
            counts[component] != entry["file_count"]
            or byte_counts[component] != entry["total_bytes"]
        ):
            raise SnapshotIntegrityError(
                f"{component} inventory counts contradict the component metadata"
            )
        skipped_paths = {item["path"] for item in entry["skipped"]}
        overlap = skipped_paths & set(inventory)
        if overlap:
            raise SnapshotIntegrityError(
                f"{component} paths are both skipped and inventoried: {sorted(overlap)}"
            )

    _validate_recovery_metadata(manifest, contents)
    return _ManifestLayout(
        contents=contents,
        inventory=inventory,
        owners=owners,
        component_paths=component_paths,
    )


def _manifest_from_tar(tar: tarfile.TarFile) -> dict[str, Any]:
    members = [member for member in tar.getmembers() if member.name == MANIFEST_ARCNAME]
    if len(members) != 1:
        raise SnapshotIntegrityError("snapshot archive must contain one manifest.json")
    member = members[0]
    if not member.isfile() or member.size > 4 * 1024 * 1024:
        raise SnapshotIntegrityError("snapshot manifest is not a bounded regular file")
    extracted = tar.extractfile(member)
    if extracted is None:
        raise SnapshotIntegrityError("snapshot manifest is not readable")
    try:
        manifest = json.loads(extracted.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotIntegrityError(f"snapshot manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise SnapshotIntegrityError("snapshot manifest root must be an object")
    return manifest


def _open_snapshot_archive(data: bytes) -> tarfile.TarFile:
    """Open decrypted snapshot bytes with one stable integrity error contract."""
    try:
        return tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise SnapshotIntegrityError(
            "decrypted snapshot is not a valid gzip tar archive"
        ) from exc


def _enforce_archive_resource_limits(
    tar: tarfile.TarFile,
    *,
    compressed_bytes: int,
    limits: SnapshotResourceLimits,
) -> int:
    member_count = 0
    expanded_bytes = 0
    while member := tar.next():
        member_count += 1
        if member_count > limits.max_members:
            raise SnapshotIntegrityError(
                f"snapshot contains more than {limits.max_members} archive members"
            )
        if member.isfile():
            if member.size > limits.max_member_bytes:
                raise SnapshotIntegrityError(
                    f"archive member {member.name!r} exceeds the configured "
                    f"{limits.max_member_bytes}-byte limit"
                )
            expanded_bytes += member.size
            if expanded_bytes > limits.max_expanded_bytes:
                raise SnapshotIntegrityError(
                    "snapshot expands beyond the configured "
                    f"{limits.max_expanded_bytes}-byte limit"
                )
    if (
        compressed_bytes > 0
        and expanded_bytes / compressed_bytes > limits.max_compression_ratio
    ):
        raise SnapshotIntegrityError("snapshot compression ratio exceeds the configured limit")
    return expanded_bytes


def _validate_archive_members(
    tar: tarfile.TarFile,
    layout: _ManifestLayout,
) -> None:
    seen: set[str] = set()
    archived_payloads: set[str] = set()
    tree_roots = {
        path
        for component, path in layout.component_paths.items()
        if component in _TREE_COMPONENT_ROOTS
    }
    for index, member in enumerate(tar.getmembers()):
        path = _normalized_relative_posix(
            member.name,
            field=f"archive member[{index}].name",
        )
        path_text = path.as_posix()
        if path_text in seen:
            raise SnapshotIntegrityError(f"duplicate archive member: {path_text}")
        seen.add(path_text)
        if path_text == MANIFEST_ARCNAME:
            if not member.isfile():
                raise SnapshotIntegrityError("manifest.json must be a regular file")
            continue
        inventory_entry = layout.inventory.get(path_text)
        if member.isdir():
            allowed = any(_under(path, root) for root in tree_roots) or any(
                _under(inventory_path, path)
                for inventory_path in (PurePosixPath(value) for value in layout.inventory)
            )
            if not allowed:
                raise SnapshotIntegrityError(
                    f"archive directory is outside declared components: {path_text}"
                )
            continue
        if inventory_entry is None:
            raise SnapshotIntegrityError(f"archive contains undeclared entry: {path_text}")
        expected_kind = inventory_entry["kind"]
        if expected_kind == "file":
            if not member.isfile():
                raise SnapshotIntegrityError(f"archive kind contradicts inventory for {path_text}")
            if member.size != inventory_entry["size_bytes"]:
                raise SnapshotIntegrityError(f"archive size contradicts inventory for {path_text}")
        if expected_kind == "symlink":
            if not member.issym() or member.linkname != inventory_entry["target"]:
                raise SnapshotIntegrityError(
                    f"archive symlink contradicts inventory for {path_text}"
                )
        if expected_kind not in {"file", "symlink"}:
            raise SnapshotIntegrityError(f"unsupported inventory kind for {path_text}")
        archived_payloads.add(path_text)
    if archived_payloads != set(layout.inventory):
        missing = sorted(set(layout.inventory) - archived_payloads)
        raise SnapshotIntegrityError(f"archive is missing inventoried entries: {missing}")


def _component_root_path(
    extracted: Path,
    layout: _ManifestLayout,
    component: str,
) -> Path:
    return extracted.joinpath(*layout.component_paths[component].parts)


def _ensure_no_symlink_ancestor(path: Path, root: Path) -> None:
    current = path
    root = root.resolve()
    while current != root:
        if current.is_symlink():
            raise SnapshotIntegrityError(f"archive path traverses a symlink ancestor: {current}")
        if root not in current.parents:
            raise SnapshotIntegrityError(f"archive path escapes extraction root: {path}")
        current = current.parent


def _verify_inventory(
    extracted: Path,
    layout: _ManifestLayout,
) -> None:
    """Check checksums, resolved containment, and symlink targets."""
    extracted = extracted.resolve()
    for component in _TREE_COMPONENT_ROOTS:
        if component not in layout.component_paths:
            continue
        root = _component_root_path(extracted, layout, component)
        _ensure_no_symlink_ancestor(root.parent, extracted)
        if not root.is_dir() or root.is_symlink():
            raise SnapshotIntegrityError(f"{component} archive root is missing or not a directory")
    for rel, entry in layout.inventory.items():
        path = extracted.joinpath(*PurePosixPath(rel).parts)
        _ensure_no_symlink_ancestor(path.parent, extracted)
        component = layout.owners[rel]
        component_root = _component_root_path(extracted, layout, component)
        if entry["kind"] == "symlink":
            if not path.is_symlink():
                raise SnapshotIntegrityError(f"inventory mismatch for symlink: {rel}")
            actual_target = os.readlink(path)
            expected_target = entry["target"]
            if actual_target != expected_target:
                # Python's tar data filter can canonicalize a safe legacy
                # spelling such as "./state.json" to "state.json". The archive
                # member and manifest already matched exactly before
                # extraction, so restore the validated spelling in scratch.
                if PurePosixPath(actual_target) != PurePosixPath(expected_target):
                    raise SnapshotIntegrityError(f"inventory mismatch for symlink: {rel}")
                path.unlink()
                os.symlink(expected_target, path)
            try:
                resolved = path.resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise SnapshotIntegrityError(f"symlink cannot be resolved safely: {rel}") from exc
            if not resolved.is_relative_to(component_root.resolve()):
                raise SnapshotIntegrityError(f"symlink escapes component root: {rel}")
            continue
        if not path.is_file() or path.is_symlink():
            raise SnapshotIntegrityError(f"file listed in inventory is missing: {rel}")
        resolved = path.resolve()
        root_resolved = (
            component_root.resolve() if component in _TREE_COMPONENT_ROOTS else extracted
        )
        if not resolved.is_relative_to(root_resolved):
            raise SnapshotIntegrityError(f"file escapes component root: {rel}")
        if path.stat().st_size != entry["size_bytes"] or _sha256(path) != entry["sha256"]:
            raise SnapshotIntegrityError(f"checksum mismatch for: {rel}")


def read_manifest(
    path: Path,
    passphrase: str,
    *,
    limits: SnapshotResourceLimits | None = None,
) -> dict[str, Any]:
    """Decrypt and fully validate a snapshot without restoring live data."""
    limits = limits or SnapshotResourceLimits()
    data = _decrypt_snapshot(path, passphrase, limits=limits)
    return _read_manifest_data(data, limits=limits)


def _read_manifest_from_handle(
    path: Path,
    handle: BinaryIO,
    passphrase: str,
    *,
    limits: SnapshotResourceLimits,
) -> dict[str, Any]:
    """Fully validate the exact encrypted generation held by ``handle``."""
    data = _decrypt_snapshot_handle(
        path,
        handle,
        passphrase,
        limits=limits,
    )
    return _read_manifest_data(data, limits=limits)


def _read_manifest_data(
    data: bytes,
    *,
    limits: SnapshotResourceLimits,
) -> dict[str, Any]:
    """Validate decrypted archive bytes and their complete manifest inventory."""
    with tempfile.TemporaryDirectory(prefix="healthmes-inspect-") as tmp:
        extracted = Path(tmp) / "extracted"
        extracted.mkdir()
        with _open_snapshot_archive(data) as tar:
            expanded_bytes = _enforce_archive_resource_limits(
                tar,
                compressed_bytes=len(data),
                limits=limits,
            )
            manifest = _manifest_from_tar(tar)
            layout = _validate_manifest_layout(manifest)
            _validate_archive_members(tar, layout)
            _require_disk_capacity(
                extracted,
                payload_bytes=expanded_bytes,
                limits=limits,
                label="snapshot inspection",
            )
            tar.extractall(extracted, filter="data")
        _verify_inventory(extracted, layout)
    return manifest


@dataclass(frozen=True, slots=True)
class _DirectoryIdentity:
    device: int
    inode: int

    @classmethod
    def from_metadata(cls, metadata: os.stat_result) -> _DirectoryIdentity:
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError("restore parent descriptor must refer to a directory")
        return cls(device=metadata.st_dev, inode=metadata.st_ino)

    def matches(self, metadata: os.stat_result) -> bool:
        return (
            stat.S_ISDIR(metadata.st_mode)
            and metadata.st_dev == self.device
            and metadata.st_ino == self.inode
        )


@dataclass(slots=True)
class _DirectoryAnchor:
    """One validated restore parent held open across every local mutation."""

    path: Path
    descriptor: int
    identity: _DirectoryIdentity

    def close(self) -> None:
        if self.descriptor < 0:
            return
        descriptor = self.descriptor
        self.descriptor = -1
        os.close(descriptor)

    def assert_current_path(self, *, component: str) -> None:
        try:
            metadata = os.stat(self.path, follow_symlinks=False)
        except OSError as exc:
            raise BackupError(
                f"{component} restore target parent path changed during restore: "
                f"{self.path}: {exc}"
            ) from exc
        if not self.identity.matches(metadata):
            raise BackupError(
                f"{component} restore target parent path identity changed during "
                f"restore: {self.path}"
            )


def _require_fd_relative_restore_support() -> None:
    """Fail closed where pathname-race-safe local restore is unavailable."""
    required_dir_fd_functions = (
        os.open,
        os.stat,
        os.rename,
        os.unlink,
        os.mkdir,
        os.symlink,
    )
    if (
        os.name == "nt"
        or not getattr(os, "O_DIRECTORY", 0)
        or not getattr(os, "O_NOFOLLOW", 0)
        or not hasattr(os, "fstatvfs")
        or not hasattr(os, "fchmod")
        or any(function not in os.supports_dir_fd for function in required_dir_fd_functions)
        or not shutil.rmtree.avoids_symlink_attacks
    ):
        raise BackupError(
            "secure local restore requires no-follow directory descriptors and "
            "fd-relative filesystem operations; this platform cannot guarantee "
            "that a replaced parent path will not redirect restore data"
        )


def _open_restore_parent(path: Path, *, create: bool) -> _DirectoryAnchor:
    """Canonicalize once, then open one no-follow component at a time."""
    _require_fd_relative_restore_support()
    requested = Path(path)
    if not requested.is_absolute():
        raise BackupError(f"restore target parent must be absolute: {requested}")
    try:
        canonical = requested.resolve(strict=False)
    except OSError as exc:
        raise BackupError(
            f"could not resolve restore target parent {requested}: {exc}"
        ) from exc
    if not canonical.is_absolute():
        raise BackupError(
            f"resolved restore target parent must be absolute: {canonical}"
        )
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | os.O_NOFOLLOW
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(canonical.anchor, flags)
        for part in canonical.parts[1:]:
            try:
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o777, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        identity = _DirectoryIdentity.from_metadata(os.fstat(descriptor))
        anchor = _DirectoryAnchor(
            path=canonical,
            descriptor=descriptor,
            identity=identity,
        )
        descriptor = None
        anchor.assert_current_path(component="local")
        return anchor
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError(
            f"could not open restore target parent without following symlinks "
            f"{canonical}: {exc}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


@dataclass(slots=True)
class _SwapOperation:
    component: str
    destination: Path
    staged: Path | None
    backup: Path
    source: Path | None = None
    is_tree: bool = False
    had_original: bool | None = None
    applied: bool = False
    staged_identity: JournalEntryIdentity | None = None
    rollback_identity: JournalEntryIdentity | None = None
    applied_identity: JournalEntryIdentity | None = None
    parent_identity: _DirectoryIdentity | None = field(default=None, repr=False)
    parent_anchor: _DirectoryAnchor | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class _PostgresRestore:
    component: str
    database_url: str
    dump_path: Path
    expected_identity: tuple[str, int] | None = None


@dataclass(frozen=True, slots=True)
class _LocalRestoreRequest:
    component: str
    source: Path
    destination: Path
    is_tree: bool


@dataclass(slots=True)
class _RestorePlan:
    transaction_id: str
    included: tuple[str, ...]
    skipped: tuple[str, ...]
    recovery_mode: str
    local_operations: list[_SwapOperation]
    postgres_restores: list[_PostgresRestore]
    journal_path: Path
    journal: RestoreJournal
    resource_limits: SnapshotResourceLimits = field(
        default_factory=SnapshotResourceLimits,
        repr=False,
    )
    parent_anchors: list[_DirectoryAnchor] = field(default_factory=list, repr=False)


def _operation_entry_name(
    operation: _SwapOperation,
    path: Path,
) -> str:
    anchor = operation.parent_anchor
    if anchor is None:
        raise BackupError(
            f"{operation.component} restore target parent is not anchored"
        )
    if path.parent != anchor.path or path.name in {"", ".", ".."}:
        raise BackupError(
            f"{operation.component} restore path is outside its anchored parent: {path}"
        )
    return path.name


def _bind_operation_parent_anchors(
    operations: list[_SwapOperation],
    *,
    create: bool,
) -> list[_DirectoryAnchor]:
    anchors_by_path: dict[Path, _DirectoryAnchor] = {
        operation.parent_anchor.path: operation.parent_anchor
        for operation in operations
        if operation.parent_anchor is not None
    }
    opened: list[_DirectoryAnchor] = []
    assigned: list[_SwapOperation] = []
    try:
        for operation in operations:
            expected_parent = operation.destination.parent
            for path in (operation.staged, operation.backup):
                if path is not None and path.parent != expected_parent:
                    raise BackupError(
                        f"{operation.component} restore artifacts do not share "
                        f"the destination parent: {path}"
                    )
            anchor = operation.parent_anchor
            if anchor is not None:
                if anchor.path != expected_parent:
                    raise BackupError(
                        f"{operation.component} restore parent anchor does not "
                        f"match its destination: {operation.destination}"
                    )
                if (
                    operation.parent_identity is not None
                    and operation.parent_identity != anchor.identity
                ):
                    raise BackupError(
                        f"{operation.component} restore parent identity changed: "
                        f"{expected_parent}"
                    )
                operation.parent_identity = anchor.identity
                continue
            anchor = anchors_by_path.get(expected_parent)
            if anchor is None:
                anchor = _open_restore_parent(expected_parent, create=create)
                anchors_by_path[expected_parent] = anchor
                opened.append(anchor)
            if (
                operation.parent_identity is not None
                and operation.parent_identity != anchor.identity
            ):
                raise BackupError(
                    f"{operation.component} restore parent identity changed: "
                    f"{expected_parent}"
                )
            operation.parent_identity = anchor.identity
            operation.parent_anchor = anchor
            assigned.append(operation)
        for operation in operations:
            if operation.parent_anchor is not None:
                operation.parent_anchor.assert_current_path(
                    component=operation.component
                )
                _operation_entry_name(operation, operation.destination)
                _operation_entry_name(operation, operation.backup)
                if operation.staged is not None:
                    _operation_entry_name(operation, operation.staged)
        return opened
    except BaseException:
        opened_ids = {id(anchor) for anchor in opened}
        for operation in assigned:
            if (
                operation.parent_anchor is not None
                and id(operation.parent_anchor) in opened_ids
            ):
                operation.parent_anchor = None
        for anchor in reversed(opened):
            anchor.close()
        raise


def _close_operation_parent_anchors(
    operations: list[_SwapOperation],
    anchors: list[_DirectoryAnchor],
) -> None:
    anchor_ids = {id(anchor) for anchor in anchors}
    for operation in operations:
        if (
            operation.parent_anchor is not None
            and id(operation.parent_anchor) in anchor_ids
        ):
            operation.parent_anchor = None
    for anchor in reversed(anchors):
        anchor.close()


@contextmanager
def _operation_parent_scope(
    operations: list[_SwapOperation],
    *,
    create: bool,
):
    opened = _bind_operation_parent_anchors(operations, create=create)
    try:
        yield
    finally:
        _close_operation_parent_anchors(operations, opened)


def _assert_operation_parent_current(operation: _SwapOperation) -> None:
    anchor = operation.parent_anchor
    if anchor is None:
        raise BackupError(
            f"{operation.component} restore target parent is not anchored"
        )
    anchor.assert_current_path(component=operation.component)


def _assert_operation_parents_current(
    operations: list[_SwapOperation],
) -> None:
    seen: set[int] = set()
    for operation in operations:
        anchor = operation.parent_anchor
        if anchor is None:
            raise BackupError(
                f"{operation.component} restore target parent is not anchored"
            )
        if id(anchor) in seen:
            continue
        seen.add(id(anchor))
        anchor.assert_current_path(component=operation.component)


def _anchored_metadata(
    operation: _SwapOperation,
    path: Path,
) -> os.stat_result | None:
    anchor = operation.parent_anchor
    if anchor is None:
        raise BackupError(
            f"{operation.component} restore target parent is not anchored"
        )
    try:
        return os.stat(
            _operation_entry_name(operation, path),
            dir_fd=anchor.descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None


def _digest_identity_field(
    digest: Any,
    value: str | int,
) -> None:
    encoded = str(value).encode("utf-8", errors="surrogateescape")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def _metadata_stable(
    before: os.stat_result,
    after: os.stat_result,
) -> bool:
    return (
        before.st_mode == after.st_mode
        and before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
    )


@dataclass(slots=True)
class _IdentityPhaseBudget:
    """One absolute deadline shared by every identity traversal in a phase."""

    limits: SnapshotResourceLimits
    phase: str
    deadline: float
    member_count: int = 0
    expanded_bytes: int = 0

    @classmethod
    def start(
        cls,
        limits: SnapshotResourceLimits,
        *,
        phase: str,
    ) -> _IdentityPhaseBudget:
        return cls(
            limits=limits,
            phase=phase,
            deadline=(
                monotonic()
                + limits.identity_traversal_timeout_seconds
            ),
        )

    def traversal(self, *, label: str) -> _IdentityTraversal:
        return _IdentityTraversal(
            phase=self,
            label=label,
        )

    def consume_entry(self, *, entry: str) -> None:
        self.member_count += 1
        if self.member_count > self.limits.max_members:
            raise BackupError(
                f"{self.phase} contains more than "
                f"{self.limits.max_members} identity traversal entries "
                f"(limit reached at {entry})"
            )

    def consume_file(
        self,
        metadata: os.stat_result,
        *,
        entry: str,
    ) -> None:
        if not stat.S_ISREG(metadata.st_mode):
            return
        size = metadata.st_size
        if size < 0:
            raise BackupError(
                f"{self.phase} reported a negative file size at {entry}"
            )
        if size > self.limits.max_member_bytes:
            raise BackupError(
                f"{entry} exceeds the configured "
                f"{self.limits.max_member_bytes}-byte identity traversal limit"
            )
        if self.expanded_bytes + size > self.limits.max_expanded_bytes:
            raise BackupError(
                f"{self.phase} exceeds the configured "
                f"{self.limits.max_expanded_bytes}-byte identity traversal limit"
            )
        self.expanded_bytes += size


@dataclass(slots=True)
class _IdentityTraversal:
    """Per-generation member and byte accounting under a phase deadline."""

    phase: _IdentityPhaseBudget
    label: str

    def check_deadline(self, *, entry: str) -> None:
        if monotonic() > self.phase.deadline:
            raise BackupError(
                f"{self.phase.phase} exceeded the configured "
                f"{self.phase.limits.identity_traversal_timeout_seconds:g}-second "
                f"identity traversal deadline while inspecting {entry}"
            )

    def reserve_entry(
        self,
        metadata: os.stat_result,
        *,
        entry: str,
        depth: int,
    ) -> None:
        self.check_deadline(entry=entry)
        limits = self.phase.limits
        if depth > limits.max_identity_depth:
            raise BackupError(
                f"{self.phase.phase} exceeds the configured "
                f"{limits.max_identity_depth}-level identity traversal depth "
                f"at {entry}"
            )
        self.phase.consume_entry(entry=entry)
        self.phase.consume_file(
            metadata,
            entry=entry,
        )

    def reserve_cleanup_entry(
        self,
        *,
        entry: str,
        depth: int,
    ) -> None:
        self.check_deadline(entry=entry)
        if depth > self.phase.limits.max_identity_depth:
            raise BackupError(
                f"{self.phase.phase} exceeds the configured "
                f"{self.phase.limits.max_identity_depth}-level cleanup depth "
                f"at {entry}"
            )
        self.phase.consume_entry(entry=entry)


_ACTIVE_IDENTITY_PHASE: ContextVar[_IdentityPhaseBudget | None] = (
    ContextVar(
        "healthmes_active_restore_identity_phase",
        default=None,
    )
)


@contextmanager
def _identity_phase_scope(
    limits: SnapshotResourceLimits,
    *,
    phase: str,
):
    budget = _IdentityPhaseBudget.start(limits, phase=phase)
    token = _ACTIVE_IDENTITY_PHASE.set(budget)
    try:
        yield budget
    finally:
        _ACTIVE_IDENTITY_PHASE.reset(token)


def _identity_traversal(*, label: str) -> _IdentityTraversal:
    phase = _ACTIVE_IDENTITY_PHASE.get()
    if phase is None:
        phase = _IdentityPhaseBudget.start(
            SnapshotResourceLimits(),
            phase="restore generation identity",
        )
    return phase.traversal(label=label)


def _valid_directory_entry_name(name: object) -> str:
    if (
        not isinstance(name, str)
        or name in {"", ".", ".."}
        or "/" in name
        or (os.altsep is not None and os.altsep in name)
    ):
        raise BackupError("restore tree contains an invalid entry name")
    return name


def _scan_identity_entries(
    descriptor: int,
    *,
    traversal: _IdentityTraversal,
    label: str,
    depth: int,
) -> list[tuple[str, os.stat_result]]:
    entries: list[tuple[str, os.stat_result]] = []
    try:
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                name = _valid_directory_entry_name(entry.name)
                child_label = f"{label}/{name}"
                metadata = entry.stat(follow_symlinks=False)
                traversal.reserve_entry(
                    metadata,
                    entry=child_label,
                    depth=depth + 1,
                )
                entries.append((name, metadata))
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError(
            f"could not enumerate restore generation {label}: {exc}"
        ) from exc
    entries.sort(key=lambda item: os.fsencode(item[0]))
    return entries


def _scan_identity_names(
    descriptor: int,
    *,
    traversal: _IdentityTraversal,
    label: str,
) -> tuple[str, ...]:
    names: list[str] = []
    try:
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                traversal.check_deadline(entry=label)
                name = _valid_directory_entry_name(entry.name)
                traversal.phase.consume_entry(
                    entry=f"{label}/{name}",
                )
                names.append(name)
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError(
            f"could not revalidate restore generation {label}: {exc}"
        ) from exc
    names.sort(key=os.fsencode)
    return tuple(names)


def _hash_regular_descriptor(
    descriptor: int,
    *,
    traversal: _IdentityTraversal | None = None,
    label: str = "restore file",
    depth: int = 0,
    entry_reserved: bool = False,
) -> tuple[os.stat_result, str]:
    traversal = traversal or _identity_traversal(label=label)
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise BackupError("restore generation identity requires a regular file")
    if not entry_reserved:
        traversal.reserve_entry(
            before,
            entry=label,
            depth=depth,
        )
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    remaining = before.st_size
    while remaining:
        traversal.check_deadline(entry=label)
        chunk = os.read(
            descriptor,
            min(1024 * 1024, remaining),
        )
        if not chunk:
            raise BackupError(
                "restore generation changed size while its identity was "
                f"being captured: {label}"
            )
        digest.update(chunk)
        remaining -= len(chunk)
    traversal.check_deadline(entry=label)
    if os.read(descriptor, 1):
        raise BackupError(
            "restore generation changed size while its identity was "
            f"being captured: {label}"
        )
    after = os.fstat(descriptor)
    if not _metadata_stable(before, after):
        raise BackupError(
            "restore generation changed while its identity was being captured"
        )
    return after, digest.hexdigest()


def _hash_directory_descriptor(
    descriptor: int,
    *,
    traversal: _IdentityTraversal | None = None,
    label: str = "restore tree",
    depth: int = 0,
    entry_reserved: bool = False,
) -> tuple[os.stat_result, str]:
    traversal = traversal or _identity_traversal(label=label)
    before = os.fstat(descriptor)
    if not stat.S_ISDIR(before.st_mode):
        raise BackupError("restore generation identity requires a directory")
    if not entry_reserved:
        traversal.reserve_entry(
            before,
            entry=label,
            depth=depth,
        )
    digest = hashlib.sha256()
    entries = _scan_identity_entries(
        descriptor,
        traversal=traversal,
        label=label,
        depth=depth,
    )
    initial_names = tuple(name for name, _metadata in entries)
    for name, metadata in entries:
        child_label = f"{label}/{name}"
        traversal.check_deadline(entry=child_label)
        _digest_identity_field(digest, name)
        _digest_identity_field(digest, stat.S_IFMT(metadata.st_mode))
        _digest_identity_field(digest, stat.S_IMODE(metadata.st_mode))
        if stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(name, dir_fd=descriptor)
            after_metadata = os.stat(
                name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            after_target = os.readlink(name, dir_fd=descriptor)
            if (
                not _metadata_stable(metadata, after_metadata)
                or target != after_target
            ):
                raise BackupError(
                    "restore tree symlink changed while its identity was "
                    f"being captured: {child_label}"
                )
            _digest_identity_field(
                digest,
                target,
            )
            continue
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        if stat.S_ISDIR(metadata.st_mode):
            child = os.open(
                name,
                flags | getattr(os, "O_DIRECTORY", 0),
                dir_fd=descriptor,
            )
            try:
                child_metadata, child_digest = (
                    _hash_directory_descriptor(
                        child,
                        traversal=traversal,
                        label=child_label,
                        depth=depth + 1,
                        entry_reserved=True,
                    )
                )
            finally:
                os.close(child)
        elif stat.S_ISREG(metadata.st_mode):
            child = os.open(name, flags, dir_fd=descriptor)
            try:
                child_metadata, child_digest = _hash_regular_descriptor(
                    child,
                    traversal=traversal,
                    label=child_label,
                    depth=depth + 1,
                    entry_reserved=True,
                )
            finally:
                os.close(child)
        else:
            raise BackupError(
                "restore tree contains an unsupported entry type"
            )
        named_metadata = os.stat(
            name,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        if (
            not _metadata_stable(metadata, child_metadata)
            or not _metadata_stable(metadata, named_metadata)
        ):
            raise BackupError(
                "restore tree changed while its identity was being captured"
            )
        for value in (
            child_metadata.st_dev,
            child_metadata.st_ino,
            child_metadata.st_size,
            child_metadata.st_mtime_ns,
            child_digest,
        ):
            _digest_identity_field(digest, value)
    if (
        _scan_identity_names(
            descriptor,
            traversal=traversal,
            label=label,
        )
        != initial_names
    ):
        raise BackupError(
            "restore tree entries changed while its identity was being captured"
        )
    after = os.fstat(descriptor)
    if not _metadata_stable(before, after):
        raise BackupError(
            "restore tree changed while its identity was being captured"
        )
    return after, digest.hexdigest()


def _capture_operation_entry_identity(
    operation: _SwapOperation,
    path: Path,
    *,
    is_tree: bool,
) -> JournalEntryIdentity | None:
    metadata = _anchored_metadata(operation, path)
    if metadata is None:
        return None
    if stat.S_ISLNK(metadata.st_mode):
        raise BackupError(
            f"{operation.component} restore generation must not be a symlink: "
            f"{path}"
        )
    anchor = operation.parent_anchor
    if anchor is None:
        raise BackupError(
            f"{operation.component} restore target parent is not anchored"
        )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    if is_tree:
        flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(
        _operation_entry_name(operation, path),
        flags,
        dir_fd=anchor.descriptor,
    )
    try:
        label = f"{operation.component} generation {path}"
        traversal = _identity_traversal(label=label)
        captured, digest = (
            _hash_directory_descriptor(
                descriptor,
                traversal=traversal,
                label=label,
            )
            if is_tree
            else _hash_regular_descriptor(
                descriptor,
                traversal=traversal,
                label=label,
            )
        )
    finally:
        os.close(descriptor)
    if not _metadata_stable(metadata, captured):
        raise BackupError(
            f"{operation.component} restore generation changed while it was "
            f"being inspected: {path}"
        )
    return JournalEntryIdentity(
        kind="directory" if is_tree else "file",
        device=captured.st_dev,
        inode=captured.st_ino,
        size=captured.st_size,
        mtime_ns=captured.st_mtime_ns,
        sha256=digest,
    )


def _require_operation_entry_identity(
    operation: _SwapOperation,
    path: Path,
    *,
    is_tree: bool,
    expected: JournalEntryIdentity | None,
    label: str,
) -> bool:
    current = _capture_operation_entry_identity(
        operation,
        path,
        is_tree=is_tree,
    )
    if current is None:
        return False
    if expected is None:
        raise BackupError(
            f"{label} has no recorded restore generation identity: {path}"
        )
    if current != expected:
        raise BackupError(
            f"{label} changed after the restore journal was written; "
            f"preserving the newer generation: {path}"
        )
    return True


def _validate_anchored_entry_type(
    operation: _SwapOperation,
    path: Path,
    *,
    is_tree: bool,
    label: str,
) -> bool:
    metadata = _anchored_metadata(operation, path)
    if metadata is None:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        raise BackupError(f"{label} must not be a symlink: {path}")
    if is_tree and not stat.S_ISDIR(metadata.st_mode):
        raise BackupError(f"{label} must be a directory: {path}")
    if not is_tree and not stat.S_ISREG(metadata.st_mode):
        raise BackupError(f"{label} must be a regular file: {path}")
    return True


def _require_anchored_disk_capacity(
    operation: _SwapOperation,
    *,
    payload_bytes: int,
    limits: SnapshotResourceLimits,
    label: str,
) -> None:
    anchor = operation.parent_anchor
    if anchor is None:
        raise BackupError(
            f"{operation.component} restore target parent is not anchored"
        )
    try:
        filesystem = os.fstatvfs(anchor.descriptor)
    except OSError as exc:
        raise BackupError(
            f"could not inspect free space for {label} at {anchor.path}: {exc}"
        ) from exc
    block_size = filesystem.f_frsize or filesystem.f_bsize
    free = filesystem.f_bavail * block_size
    required = payload_bytes + limits.min_free_bytes
    if free < required:
        raise BackupError(
            f"insufficient disk space for {label}: need {required} bytes "
            f"including reserve, have {free}"
        )


def _copy_regular_file_to_descriptor(
    source: Path,
    *,
    destination_name: str,
    destination_parent_descriptor: int,
) -> None:
    source_descriptor: int | None = None
    destination_descriptor: int | None = None
    try:
        source_descriptor = os.open(
            source,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        source_metadata = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_metadata.st_mode):
            raise BackupError(f"restore source is not a regular file: {source}")
        destination_descriptor = os.open(
            destination_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            stat.S_IMODE(source_metadata.st_mode) or 0o600,
            dir_fd=destination_parent_descriptor,
        )
        copied = 0
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            if copied > source_metadata.st_size:
                raise BackupError(
                    f"restore source changed size while being staged: {source}"
                )
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                view = view[written:]
        if copied != source_metadata.st_size:
            raise BackupError(
                f"restore source changed size while being staged: {source}"
            )
        os.fchmod(
            destination_descriptor,
            stat.S_IMODE(source_metadata.st_mode),
        )
        os.fsync(destination_descriptor)
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        if source_descriptor is not None:
            os.close(source_descriptor)


def _copy_tree_contents_to_descriptor(
    source: Path,
    *,
    destination_descriptor: int,
) -> None:
    for entry in sorted(os.scandir(source), key=lambda item: item.name):
        metadata = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            os.symlink(
                os.readlink(entry.path),
                entry.name,
                dir_fd=destination_descriptor,
            )
            continue
        if stat.S_ISREG(metadata.st_mode):
            _copy_regular_file_to_descriptor(
                Path(entry.path),
                destination_name=entry.name,
                destination_parent_descriptor=destination_descriptor,
            )
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            raise BackupError(
                f"restore source tree contains an unsupported entry: {entry.path}"
            )
        os.mkdir(
            entry.name,
            stat.S_IMODE(metadata.st_mode) or 0o700,
            dir_fd=destination_descriptor,
        )
        child_descriptor = os.open(
            entry.name,
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_CLOEXEC", 0)
            | os.O_NOFOLLOW,
            dir_fd=destination_descriptor,
        )
        try:
            _copy_tree_contents_to_descriptor(
                Path(entry.path),
                destination_descriptor=child_descriptor,
            )
            os.fchmod(child_descriptor, stat.S_IMODE(metadata.st_mode))
            os.fsync(child_descriptor)
        finally:
            os.close(child_descriptor)
    os.fsync(destination_descriptor)


def _stage_operation_payload(operation: _SwapOperation) -> None:
    source = operation.source
    staged = operation.staged
    anchor = operation.parent_anchor
    if source is None or staged is None or anchor is None:
        raise BackupError(
            f"{operation.component} restore staging operation is incomplete"
        )
    staged_name = _operation_entry_name(operation, staged)
    if operation.is_tree:
        source_metadata = os.stat(source, follow_symlinks=False)
        if not stat.S_ISDIR(source_metadata.st_mode):
            raise BackupError(f"restore source is not a directory: {source}")
        os.mkdir(
            staged_name,
            stat.S_IMODE(source_metadata.st_mode) or 0o700,
            dir_fd=anchor.descriptor,
        )
        staged_descriptor = os.open(
            staged_name,
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_CLOEXEC", 0)
            | os.O_NOFOLLOW,
            dir_fd=anchor.descriptor,
        )
        try:
            _copy_tree_contents_to_descriptor(
                source,
                destination_descriptor=staged_descriptor,
            )
            os.fchmod(
                staged_descriptor,
                stat.S_IMODE(source_metadata.st_mode),
            )
            os.fsync(staged_descriptor)
        finally:
            os.close(staged_descriptor)
    else:
        _copy_regular_file_to_descriptor(
            source,
            destination_name=staged_name,
            destination_parent_descriptor=anchor.descriptor,
        )
    os.fsync(anchor.descriptor)


def _fsync_operation_entry(
    operation: _SwapOperation,
    path: Path,
    *,
    is_tree: bool,
) -> None:
    anchor = operation.parent_anchor
    if anchor is None:
        raise BackupError(
            f"{operation.component} restore target parent is not anchored"
        )
    name = _operation_entry_name(operation, path)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    if is_tree:
        flags |= os.O_DIRECTORY
    descriptor = os.open(name, flags, dir_fd=anchor.descriptor)
    try:
        metadata = os.fstat(descriptor)
        if is_tree and not stat.S_ISDIR(metadata.st_mode):
            raise BackupError(f"restore entry is not a directory: {path}")
        if not is_tree and not stat.S_ISREG(metadata.st_mode):
            raise BackupError(f"restore entry is not a regular file: {path}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(anchor.descriptor)


def _rename_operation_entry(
    operation: _SwapOperation,
    source: Path,
    destination: Path,
) -> None:
    anchor = operation.parent_anchor
    if anchor is None:
        raise BackupError(
            f"{operation.component} restore target parent is not anchored"
        )
    os.rename(
        _operation_entry_name(operation, source),
        _operation_entry_name(operation, destination),
        src_dir_fd=anchor.descriptor,
        dst_dir_fd=anchor.descriptor,
    )
    os.fsync(anchor.descriptor)


def _operation_cleanup_path(
    operation: _SwapOperation,
    path: Path,
) -> Path:
    cleanup = _cleanup_quarantine_path(path)
    _operation_entry_name(operation, cleanup)
    return cleanup


def _cleanup_quarantine_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.healthmes-delete")


def _journal_identity_matches_root(
    expected: JournalEntryIdentity,
    metadata: os.stat_result,
) -> bool:
    expected_kind = (
        stat.S_ISDIR(metadata.st_mode)
        if expected.kind == "directory"
        else stat.S_ISREG(metadata.st_mode)
    )
    return (
        expected_kind
        and metadata.st_dev == expected.device
        and metadata.st_ino == expected.inode
    )


def _bounded_remove_tree_contents(
    descriptor: int,
    *,
    traversal: _IdentityTraversal,
    label: str,
    depth: int,
    entry_reserved: bool = False,
) -> None:
    if not entry_reserved:
        traversal.reserve_cleanup_entry(entry=label, depth=depth)
    try:
        os.fchmod(descriptor, 0o700)
    except OSError:
        pass
    with os.scandir(descriptor) as iterator:
        for entry in iterator:
            name = _valid_directory_entry_name(entry.name)
            child_label = f"{label}/{name}"
            traversal.reserve_cleanup_entry(
                entry=child_label,
                depth=depth + 1,
            )
            try:
                metadata = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(
                metadata.st_mode
            ):
                child_descriptor = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
                try:
                    opened = os.fstat(child_descriptor)
                    if (
                        opened.st_dev != metadata.st_dev
                        or opened.st_ino != metadata.st_ino
                    ):
                        raise BackupError(
                            "restore cleanup tree changed before deletion: "
                            f"{child_label}"
                        )
                    _bounded_remove_tree_contents(
                        child_descriptor,
                        traversal=traversal,
                        label=child_label,
                        depth=depth + 1,
                        entry_reserved=True,
                    )
                finally:
                    os.close(child_descriptor)
                current = os.stat(
                    name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if (
                    current.st_dev != metadata.st_dev
                    or current.st_ino != metadata.st_ino
                    or not stat.S_ISDIR(current.st_mode)
                ):
                    raise BackupError(
                        "restore cleanup tree changed during deletion: "
                        f"{child_label}"
                    )
                os.rmdir(name, dir_fd=descriptor)
            else:
                current = os.stat(
                    name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if (
                    current.st_dev != metadata.st_dev
                    or current.st_ino != metadata.st_ino
                    or stat.S_IFMT(current.st_mode)
                    != stat.S_IFMT(metadata.st_mode)
                ):
                    raise BackupError(
                        "restore cleanup entry changed during deletion: "
                        f"{child_label}"
                    )
                os.unlink(name, dir_fd=descriptor)
            os.fsync(descriptor)


def _remove_operation_entry(
    operation: _SwapOperation,
    path: Path,
    *,
    expected: JournalEntryIdentity | None,
    label: str,
) -> None:
    anchor = operation.parent_anchor
    if anchor is None:
        raise BackupError(
            f"{operation.component} restore target parent is not anchored"
        )
    cleanup = _operation_cleanup_path(operation, path)
    metadata = _anchored_metadata(operation, path)
    cleanup_metadata = _anchored_metadata(operation, cleanup)
    if metadata is None and cleanup_metadata is None:
        os.fsync(anchor.descriptor)
        return
    if expected is None:
        raise BackupError(
            f"{label} has no recorded restore generation identity: {path}"
        )
    if metadata is not None and cleanup_metadata is not None:
        raise BackupError(
            f"{label} and its cleanup quarantine coexist; preserving both: "
            f"{path}"
        )
    if cleanup_metadata is None:
        if not _require_operation_entry_identity(
            operation,
            path,
            is_tree=expected.kind == "directory",
            expected=expected,
            label=label,
        ):
            os.fsync(anchor.descriptor)
            return
        os.rename(
            _operation_entry_name(operation, path),
            _operation_entry_name(operation, cleanup),
            src_dir_fd=anchor.descriptor,
            dst_dir_fd=anchor.descriptor,
        )
        os.fsync(anchor.descriptor)
        cleanup_metadata = _anchored_metadata(operation, cleanup)
        if cleanup_metadata is None:
            raise BackupError(
                f"{label} cleanup quarantine disappeared after rename: "
                f"{cleanup}"
            )
    if not _journal_identity_matches_root(expected, cleanup_metadata):
        raise BackupError(
            f"{label} cleanup quarantine does not contain the journaled "
            f"generation; preserving it: {cleanup}"
        )
    traversal = _identity_traversal(label=f"{label} cleanup")
    cleanup_name = _operation_entry_name(operation, cleanup)
    if expected.kind == "directory":
        descriptor = os.open(
            cleanup_name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=anchor.descriptor,
        )
        try:
            opened = os.fstat(descriptor)
            if not _journal_identity_matches_root(expected, opened):
                raise BackupError(
                    f"{label} cleanup quarantine changed before deletion: "
                    f"{cleanup}"
                )
            _bounded_remove_tree_contents(
                descriptor,
                traversal=traversal,
                label=str(cleanup),
                depth=0,
            )
        finally:
            os.close(descriptor)
        current = _anchored_metadata(operation, cleanup)
        if current is None:
            os.fsync(anchor.descriptor)
            return
        if not _journal_identity_matches_root(expected, current):
            raise BackupError(
                f"{label} cleanup quarantine changed during deletion: "
                f"{cleanup}"
            )
        os.rmdir(cleanup_name, dir_fd=anchor.descriptor)
    else:
        traversal.reserve_cleanup_entry(
            entry=str(cleanup),
            depth=0,
        )
        os.unlink(cleanup_name, dir_fd=anchor.descriptor)
    os.fsync(anchor.descriptor)


def _path_entry_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif _path_entry_exists(path):
        path.unlink()


def _remove_restore_scratch(path: Path) -> str | None:
    """Remove decrypted restore scratch and return a reportable error."""
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        detail = f"{path}: {exc}"
        logger.error("Could not remove decrypted restore scratch: %s", detail)
        return detail
    return None


def _validate_sqlite_snapshot(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()
    finally:
        connection.close()
    if result != ("ok",):
        raise SnapshotIntegrityError(f"restored SQLite database failed quick_check: {result!r}")


def _canonical_restore_target(path: Path, *, component: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise BackupError(f"{component} restore target must not be a symlink: {candidate}")
    return candidate.resolve(strict=False)


def _default_restore_state_identity_path(locations: DataLocations) -> Path:
    """Return persistent control state outside a replaceable data parent."""
    database = make_url(locations.database_url)
    if (
        database.get_backend_name() == "sqlite"
        and database.database not in _SQLITE_MEMORY_DATABASES
    ):
        database_path = _lexical_absolute_path(
            _sqlite_file_path(locations.database_url)
        )
        target_parent = database_path.parent
        control_parent = target_parent.parent
        if control_parent == target_parent:
            control_parent = _lexical_absolute_path(Path.home())
        digest = hashlib.sha256(str(database_path).encode()).hexdigest()[:16]
        return (
            control_parent
            / ".healthmes-restore-state"
            / digest
        )
    raise BackupError(
        "restore_state_dir must be configured for a PostgreSQL-only restore"
    )


def _restore_state_directory(locations: DataLocations) -> Path:
    return _restore_state_identity_path(locations)


def _lexical_absolute_path(path: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return Path(os.path.abspath(candidate))


def _restore_state_identity_path(locations: DataLocations) -> Path:
    if locations.restore_state_dir is not None:
        return _lexical_absolute_path(locations.restore_state_dir)
    return _default_restore_state_identity_path(locations)


def _lexical_local_restore_target_parents(
    locations: DataLocations,
) -> set[Path]:
    parents: set[Path] = set()
    database = make_url(locations.database_url)
    if (
        database.get_backend_name() == "sqlite"
        and database.database not in _SQLITE_MEMORY_DATABASES
    ):
        parents.add(
            _lexical_absolute_path(
                _sqlite_file_path(locations.database_url)
            ).parent
        )
    for target in (
        locations.media_dir,
        locations.raw_ingest_dir,
        locations.hermes_home,
    ):
        if target is not None:
            parents.add(_lexical_absolute_path(Path(target)).parent)
    return parents


def _nearest_existing_ancestor(path: Path) -> Path:
    current = path
    while not _path_entry_exists(current):
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current


def _configured_local_restore_targets(
    locations: DataLocations,
) -> list[tuple[str, Path, bool]]:
    targets: list[tuple[str, Path, bool]] = []
    database = make_url(locations.database_url)
    if (
        database.get_backend_name() == "sqlite"
        and database.database not in _SQLITE_MEMORY_DATABASES
    ):
        targets.append(
            (
                "healthmes_db",
                _canonical_restore_target(
                    _sqlite_file_path(locations.database_url),
                    component="healthmes_db",
                ),
                False,
            )
        )
    for component, target in (
        ("media", locations.media_dir),
        ("raw_ingest", locations.raw_ingest_dir),
        ("hermes_home", locations.hermes_home),
    ):
        if target is not None:
            targets.append(
                (
                    component,
                    _canonical_restore_target(Path(target), component=component),
                    True,
                )
            )
    return targets


def _restore_admission_lock_path(locations: DataLocations) -> Path:
    state_dir = _restore_state_identity_path(locations)
    digest = hashlib.sha256(str(state_dir).encode()).hexdigest()[:16]
    target_parents = _lexical_local_restore_target_parents(locations)
    admission_anchor = state_dir.parent
    while (
        admission_anchor in target_parents
        and admission_anchor.parent != admission_anchor
    ):
        admission_anchor = admission_anchor.parent
    candidates = [_nearest_existing_ancestor(admission_anchor)]
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime_dir:
        candidates.append(Path(runtime_dir))
    candidates.extend((Path(tempfile.gettempdir()), Path.home()))
    lock_anchor: Path | None = None
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve(strict=True)
            metadata = resolved.stat()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if not stat.S_ISDIR(metadata.st_mode):
            continue
        if os.name != "nt" and (
            metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            continue
        if not os.access(resolved, os.R_OK | os.W_OK | os.X_OK):
            continue
        lock_anchor = resolved
        break
    if lock_anchor is None:
        raise BackupError(
            "no user-owned, non-writable-by-others directory is available "
            "for the restore admission lock"
        )
    # Admission locks are persistent coordination state, not per-restore
    # rollback artifacts. Keep their name outside the
    # ``*.healthmes-restore-*`` artifact namespace used by recovery scans.
    lock_root = lock_anchor / ".healthmes-admission-locks"
    return lock_root / f"{digest}.lock"


def _ensure_private_lock_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise BackupError(
            f"could not create restore admission lock directory {path}: {exc}"
        ) from exc
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BackupError(
            f"could not inspect restore admission lock directory {path}: {exc}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise BackupError(
            f"restore admission lock directory is not a real directory: {path}"
        )
    if os.name != "nt":
        if metadata.st_uid != os.geteuid():
            raise BackupError(
                f"restore admission lock directory is owned by another user: {path}"
            )
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            try:
                path.chmod(0o700)
                metadata = path.lstat()
            except OSError as exc:
                raise BackupError(
                    f"could not secure restore admission lock directory {path}: {exc}"
                ) from exc
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise BackupError(
                    f"restore admission lock directory is not private: {path}"
                )


def _validate_restore_control_paths(locations: DataLocations) -> None:
    state_dir = _restore_state_directory(locations)
    journal_path = restore_journal_path(state_dir)
    admission_lock = _restore_admission_lock_path(locations)
    protected: list[tuple[str, Path]] = [
        ("restore state directory", state_dir),
        ("restore journal", journal_path),
        ("restore admission lock", admission_lock),
    ]
    database = make_url(locations.database_url)
    if (
        database.get_backend_name() == "sqlite"
        and database.database not in _SQLITE_MEMORY_DATABASES
    ):
        sqlite_path = _canonical_restore_target(
            _sqlite_file_path(locations.database_url),
            component="healthmes_db",
        )
        protected.extend(
            (
                (
                    "SQLite runtime lock",
                    sqlite_path.with_name(f"{sqlite_path.name}.runtime.lock"),
                ),
                (
                    "SQLite activity lock",
                    sqlite_path.with_name(f"{sqlite_path.name}.activity.lock"),
                ),
            )
        )
    for component, destination, is_tree in _configured_local_restore_targets(locations):
        for label, path in protected:
            if path == destination or (is_tree and path.is_relative_to(destination)):
                raise BackupError(
                    f"{label} is inside the {component} restore target and could be replaced: "
                    f"{path}"
                )
    if _path_entry_exists(state_dir) and (
        state_dir.is_symlink() or not state_dir.is_dir()
    ):
        raise BackupError(f"restore_state_dir is not a directory: {state_dir}")
    for label, path in protected:
        if not label.endswith("lock") or not _path_entry_exists(path):
            continue
        if path.is_symlink() or not path.is_file():
            raise BackupError(f"{label} is not a regular file: {path}")
    _ensure_private_lock_directory(admission_lock.parent)


@contextmanager
def restore_admission_guard(locations: DataLocations):
    """Serialize restore planning, mutation, and startup crash recovery."""
    _validate_restore_control_paths(locations)
    lock_path = _restore_admission_lock_path(locations)
    stack = ExitStack()
    try:
        stack.enter_context(
            exclusive_file_lock(
                lock_path,
                timeout_seconds=_RESTORE_ADMISSION_LOCK_TIMEOUT_SECONDS,
            )
        )
    except TimeoutError as exc:
        raise BackupError(
            "another HealthMes restore or startup recovery owns the restore admission lock"
        ) from exc
    except OSError as exc:
        raise BackupError(
            f"could not open the restore admission lock safely: {exc}"
        ) from exc
    try:
        state_dir = _restore_state_directory(locations)
        if os.name == "nt":  # pragma: no cover - Windows has no dir_fd restore
            state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        else:
            state_anchor = _open_restore_parent(state_dir, create=True)
            stack.callback(state_anchor.close)
            stack.enter_context(
                anchored_restore_journal_directory(
                    state_dir,
                    state_anchor.descriptor,
                )
            )
        yield
    finally:
        stack.close()


def _preflight_local_target(
    request: _LocalRestoreRequest,
    *,
    transaction_id: str,
) -> None:
    destination = request.destination
    if _path_entry_exists(destination):
        if destination.is_symlink():
            raise BackupError(
                f"{request.component} restore target must not be a symlink: {destination}"
            )
        if request.is_tree != destination.is_dir():
            expected = "directory" if request.is_tree else "file"
            raise BackupError(
                f"{request.component} restore target is not a {expected}: {destination}"
            )
    ancestor = _nearest_existing_ancestor(destination.parent)
    if ancestor.is_symlink() or not ancestor.is_dir():
        raise BackupError(
            f"{request.component} restore target parent is not a directory: {destination.parent}"
        )
    if not os.access(ancestor, os.R_OK | os.W_OK | os.X_OK):
        raise BackupError(
            f"{request.component} restore target parent is not accessible: {ancestor}"
        )
    staged = destination.with_name(f".{destination.name}.healthmes-restore-{transaction_id}.staged")
    backup = destination.with_name(f".{destination.name}.healthmes-restore-{transaction_id}.backup")
    if _path_entry_exists(staged) or _path_entry_exists(backup):
        raise BackupError(
            f"restore staging or rollback path already exists for {request.component}"
        )
    for label, path in (
        ("live", destination),
        ("staging", staged),
        ("rollback", backup),
    ):
        cleanup = _cleanup_quarantine_path(path)
        if _path_entry_exists(cleanup):
            raise BackupError(
                f"restore {label} cleanup quarantine already exists for "
                f"{request.component}: {cleanup}"
            )
    if request.component == "healthmes_db":
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{destination}{suffix}")
            sidecar_backup = sidecar.with_name(
                f".{sidecar.name}.healthmes-restore-{transaction_id}.backup"
            )
            if sidecar.is_dir() and not sidecar.is_symlink():
                raise BackupError(
                    f"healthmes_db SQLite sidecar is unexpectedly a directory: {sidecar}"
                )
            if _path_entry_exists(sidecar_backup):
                raise BackupError("restore rollback path already exists for healthmes_db sidecar")
            for label, path in (
                ("live", sidecar),
                ("rollback", sidecar_backup),
            ):
                cleanup = _cleanup_quarantine_path(path)
                if _path_entry_exists(cleanup):
                    raise BackupError(
                        "restore "
                        f"{label} cleanup quarantine already exists for "
                        f"healthmes_db sidecar: {cleanup}"
                    )


def _preflight_anchored_operation(operation: _SwapOperation) -> None:
    """Validate one local target through its retained parent descriptor."""
    _assert_operation_parent_current(operation)
    _validate_anchored_entry_type(
        operation,
        operation.destination,
        is_tree=operation.is_tree,
        label=f"{operation.component} restore target",
    )
    if operation.staged is not None and _anchored_metadata(
        operation,
        operation.staged,
    ) is not None:
        raise BackupError(
            f"restore staging path already exists for {operation.component}"
        )
    if _anchored_metadata(operation, operation.backup) is not None:
        raise BackupError(
            f"restore rollback path already exists for {operation.component}"
        )
    for label, path in (
        (
            f"{operation.component} live cleanup quarantine",
            operation.destination,
        ),
        (
            f"{operation.component} staged cleanup quarantine",
            operation.staged,
        ),
        (
            f"{operation.component} rollback cleanup quarantine",
            operation.backup,
        ),
    ):
        if path is None:
            continue
        cleanup = _operation_cleanup_path(operation, path)
        if _anchored_metadata(operation, cleanup) is not None:
            raise BackupError(f"{label} already exists: {cleanup}")


def _plan_local_component(
    *,
    component: str,
    source: Path,
    destination: Path,
    transaction_id: str,
    is_tree: bool,
) -> _SwapOperation:
    staged = destination.with_name(f".{destination.name}.healthmes-restore-{transaction_id}.staged")
    backup = destination.with_name(f".{destination.name}.healthmes-restore-{transaction_id}.backup")
    return _SwapOperation(
        component=component,
        destination=destination,
        staged=staged,
        backup=backup,
        source=source,
        is_tree=is_tree,
    )


def _stage_planned_operation(
    operation: _SwapOperation,
    *,
    limits: SnapshotResourceLimits,
) -> None:
    source = operation.source
    staged = operation.staged
    if source is None or staged is None:
        return
    with _operation_parent_scope([operation], create=True):
        _assert_operation_parent_current(operation)
        _require_anchored_disk_capacity(
            operation,
            payload_bytes=_path_payload_bytes(source),
            limits=limits,
            label=f"{operation.component} restore staging",
        )
        if _anchored_metadata(operation, staged) is not None:
            raise BackupError(
                f"{operation.component} staged target already exists: {staged}"
            )
        _stage_operation_payload(operation)
        staged_metadata = _anchored_metadata(operation, staged)
        anchor = operation.parent_anchor
        if staged_metadata is None or anchor is None:
            raise BackupError(
                f"{operation.component} staged target disappeared during restore: "
                f"{staged}"
            )
        if staged_metadata.st_dev != anchor.identity.device:
            raise BackupError(
                f"{operation.component} could not be staged on the target filesystem"
            )
        _assert_operation_parent_current(operation)


def _stage_local_component(
    *,
    component: str,
    source: Path,
    destination: Path,
    transaction_id: str,
    is_tree: bool,
    limits: SnapshotResourceLimits | None = None,
) -> _SwapOperation:
    operation = _plan_local_component(
        component=component,
        source=source,
        destination=destination,
        transaction_id=transaction_id,
        is_tree=is_tree,
    )
    effective_limits = limits or SnapshotResourceLimits()
    try:
        _stage_planned_operation(
            operation,
            limits=effective_limits,
        )
    except BaseException as exc:
        try:
            with (
                _identity_phase_scope(
                    effective_limits,
                    phase="failed restore staging cleanup identity",
                ),
                _operation_parent_scope([operation], create=False),
            ):
                failed_identity = _capture_operation_entry_identity(
                    operation,
                    operation.staged,
                    is_tree=operation.is_tree,
                )
                if failed_identity is not None:
                    operation.staged_identity = failed_identity
                    operation.applied_identity = failed_identity
                _remove_operation_entry(
                    operation,
                    operation.staged,
                    expected=failed_identity,
                    label=f"{operation.component} failed staged target",
                )
        except (OSError, BackupError) as cleanup_exc:
            raise BackupError(
                f"{operation.component} staging failed and decrypted restore data "
                f"could not be removed from {operation.staged}: {cleanup_exc}"
            ) from exc
        raise
    return operation


def _sqlite_sidecar_operations(
    destination: Path,
    *,
    transaction_id: str,
) -> list[_SwapOperation]:
    operations: list[_SwapOperation] = []
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{destination}{suffix}")
        operations.append(
            _SwapOperation(
                component="healthmes_db",
                destination=sidecar,
                staged=None,
                backup=sidecar.with_name(
                    f".{sidecar.name}.healthmes-restore-{transaction_id}.backup"
                ),
            )
        )
    return operations


def _path_is_replaced_by(
    path: Path,
    *,
    destination: Path,
    destination_is_tree: bool,
) -> bool:
    return path == destination or (
        destination_is_tree and path.is_relative_to(destination)
    )


def _assert_complete_restore_path_graph(
    operations: list[_SwapOperation],
    *,
    locations: DataLocations,
    snapshot_source: Path,
    extracted_root: Path,
    journal_path: Path,
    transaction_id: str,
) -> None:
    destinations = [
        (operation.component, operation.destination, operation.is_tree)
        for operation in operations
    ]
    for index, (left_name, left, left_is_tree) in enumerate(destinations):
        for right_name, right, right_is_tree in destinations[index + 1 :]:
            if (
                left == right
                or (left_is_tree and right.is_relative_to(left))
                or (right_is_tree and left.is_relative_to(right))
            ):
                raise BackupError(
                    f"restore targets overlap: {left_name}={left} and "
                    f"{right_name}={right}"
                )

    artifacts: list[tuple[str, Path]] = []
    for operation in operations:
        if operation.staged is not None:
            artifacts.append(
                (f"{operation.component} staged path", operation.staged)
            )
        artifacts.append((f"{operation.component} rollback path", operation.backup))
        for cleanup_label, cleanup_source in (
            ("live cleanup quarantine", operation.destination),
            ("staged cleanup quarantine", operation.staged),
            ("rollback cleanup quarantine", operation.backup),
        ):
            if cleanup_source is None:
                continue
            artifacts.append(
                (
                    f"{operation.component} {cleanup_label}",
                    _cleanup_quarantine_path(cleanup_source),
                )
            )
    artifacts.extend(
        (
            ("restore journal", journal_path),
            (
                "restore journal temporary file",
                journal_path.with_name(
                    f".{journal_path.name}.{transaction_id}.tmp"
                ),
            ),
            ("restore admission lock", _restore_admission_lock_path(locations)),
        )
    )
    database = make_url(locations.database_url)
    if (
        database.get_backend_name() == "sqlite"
        and database.database not in _SQLITE_MEMORY_DATABASES
    ):
        sqlite_path = _canonical_restore_target(
            _sqlite_file_path(locations.database_url),
            component="healthmes_db",
        )
        artifacts.extend(
            (
                (
                    "SQLite runtime lock",
                    sqlite_path.with_name(f"{sqlite_path.name}.runtime.lock"),
                ),
                (
                    "SQLite activity lock",
                    sqlite_path.with_name(f"{sqlite_path.name}.activity.lock"),
                ),
            )
        )

    seen_artifacts: dict[Path, str] = {}
    for label, path in artifacts:
        existing = seen_artifacts.get(path)
        if existing is not None:
            raise BackupError(
                f"restore control paths overlap: {existing} and {label} both use {path}"
            )
        seen_artifacts[path] = label
        for component, destination, is_tree in destinations:
            if _path_is_replaced_by(
                path,
                destination=destination,
                destination_is_tree=is_tree,
            ):
                raise BackupError(
                    f"{label} is inside the {component} restore target and "
                    f"could be replaced: {path}"
                )

    protected_paths = (
        ("snapshot source", snapshot_source),
        ("restore state directory", journal_path.parent),
    )
    for label, path in protected_paths:
        for component, destination, is_tree in destinations:
            if _path_is_replaced_by(
                path,
                destination=destination,
                destination_is_tree=is_tree,
            ):
                if label == "snapshot source":
                    raise BackupError(
                        "snapshot source is inside a restore target and would "
                        f"be deleted: {path} (component={component})"
                    )
                raise BackupError(
                    f"{label} is inside the {component} restore target and "
                    f"would be deleted: {path}"
                )
        artifact_label = seen_artifacts.get(path)
        if artifact_label is not None:
            raise BackupError(
                f"{label} collides with {artifact_label}: {path}"
            )

    for component, destination, _is_tree in destinations:
        if (
            destination == extracted_root
            or destination.is_relative_to(extracted_root)
            or extracted_root.is_relative_to(destination)
        ):
            raise BackupError(
                f"{component} restore target overlaps the restore scratch space: "
                f"{destination}"
            )
    for label, path in artifacts:
        if (
            path == extracted_root
            or path.is_relative_to(extracted_root)
            or extracted_root.is_relative_to(path)
        ):
            raise BackupError(
                f"{label} overlaps the restore scratch space: {path}"
            )

    for label, path in artifacts:
        if label.endswith("lock") and _path_entry_exists(path):
            if path.is_symlink() or not path.is_file():
                raise BackupError(f"{label} is not a regular file: {path}")


def _validate_restore_request_contract(
    layout: _ManifestLayout,
    locations: DataLocations,
    *,
    allow_cross_store_partial: bool,
) -> None:
    """Reject incompatible restore requests without touching live targets."""
    contents = layout.contents
    included = tuple(
        name for name in _COMPONENT_ORDER if contents[name] is not None
    )
    health = contents["healthmes_db"]
    target_backend = make_url(locations.database_url).get_backend_name()
    health_kind = health["kind"]
    if health_kind == "sqlite_file" and target_backend != "sqlite":
        raise BackupError(
            "snapshot holds a sqlite database but the target database_url "
            f"backend is {target_backend}"
        )
    if health_kind == "pg_dump" and target_backend != "postgresql":
        raise BackupError(
            "snapshot holds a postgres dump but the target database_url "
            f"backend is {target_backend}"
        )

    wearable = contents["open_wearables_db"]
    if wearable is not None:
        if not locations.ow_database_url:
            raise BackupError(
                "snapshot includes open_wearables_db but no restore target is "
                "configured; set HEALTHMES_OW_DATABASE_URL before retrying"
            )
        if (
            make_url(locations.ow_database_url).get_backend_name()
            != "postgresql"
        ):
            raise BackupError("Open Wearables restore target must be PostgreSQL")

    for component, target in (
        ("media", locations.media_dir),
        ("raw_ingest", locations.raw_ingest_dir),
        ("hermes_home", locations.hermes_home),
    ):
        if contents[component] is None or target is not None:
            continue
        setting = {
            "media": "media_dir",
            "raw_ingest": "raw_ingest_dir",
            "hermes_home": "HERMES_HOME",
        }[component]
        raise BackupError(
            f"snapshot includes {component} but no {setting} restore target is "
            "configured"
        )

    includes_postgres = (
        health_kind == "pg_dump" or wearable is not None
    )
    if (
        includes_postgres
        and len(included) > 1
        and not allow_cross_store_partial
    ):
        names = ", ".join(included)
        raise BackupError(
            "restore spans PostgreSQL and additional components "
            f"({names}); distributed atomic commit is unavailable. Stop all "
            "services and rerun with --allow-cross-store-partial to explicitly "
            "accept that risk"
        )


def _preflight_restore(
    extracted: Path,
    layout: _ManifestLayout,
    locations: DataLocations,
    *,
    snapshot_path: Path,
    allow_cross_store_partial: bool,
) -> _RestorePlan:
    _validate_restore_request_contract(
        layout,
        locations,
        allow_cross_store_partial=allow_cross_store_partial,
    )
    contents = layout.contents
    included = tuple(name for name in _COMPONENT_ORDER if contents[name] is not None)
    skipped = tuple(name for name in _COMPONENT_ORDER if contents[name] is None)
    transaction_id = uuid.uuid4().hex
    local_requests: list[_LocalRestoreRequest] = []
    postgres_restores: list[_PostgresRestore] = []

    health = contents["healthmes_db"]
    target_backend = make_url(locations.database_url).get_backend_name()
    health_source = _component_root_path(extracted, layout, "healthmes_db")
    if health["kind"] == "sqlite_file":
        if target_backend != "sqlite":
            raise BackupError(
                "snapshot holds a sqlite database but the target database_url "
                f"backend is {target_backend}"
            )
        _validate_sqlite_snapshot(health_source)
        local_requests.append(
            _LocalRestoreRequest(
                component="healthmes_db",
                source=health_source,
                destination=_canonical_restore_target(
                    _sqlite_file_path(locations.database_url),
                    component="healthmes_db",
                ),
                is_tree=False,
            )
        )
    else:
        if target_backend != "postgresql":
            raise BackupError(
                "snapshot holds a postgres dump but the target database_url "
                f"backend is {target_backend}"
            )
        _preflight_pg_dump(health_source)
        postgres_restores.append(
            _PostgresRestore("healthmes_db", locations.database_url, health_source)
        )

    wearable = contents["open_wearables_db"]
    if wearable is not None:
        if not locations.ow_database_url:
            raise BackupError(
                "snapshot includes open_wearables_db but no restore target is configured; "
                "set HEALTHMES_OW_DATABASE_URL before retrying"
            )
        if make_url(locations.ow_database_url).get_backend_name() != "postgresql":
            raise BackupError("Open Wearables restore target must be PostgreSQL")
        wearable_source = _component_root_path(
            extracted,
            layout,
            "open_wearables_db",
        )
        _preflight_pg_dump(wearable_source)
        postgres_restores.append(
            _PostgresRestore(
                "open_wearables_db",
                locations.ow_database_url,
                wearable_source,
            )
        )

    for component, target in (
        ("media", locations.media_dir),
        ("raw_ingest", locations.raw_ingest_dir),
        ("hermes_home", locations.hermes_home),
    ):
        if contents[component] is None:
            continue
        if target is None:
            setting = {
                "media": "media_dir",
                "raw_ingest": "raw_ingest_dir",
                "hermes_home": "HERMES_HOME",
            }[component]
            raise BackupError(
                f"snapshot includes {component} but no {setting} restore target is configured"
            )
        local_requests.append(
            _LocalRestoreRequest(
                component=component,
                source=_component_root_path(extracted, layout, component),
                destination=_canonical_restore_target(
                    Path(target),
                    component=component,
                ),
                is_tree=True,
            )
        )

    physical_identities: dict[tuple[str, int], str] = {}
    verified_postgres_restores: list[_PostgresRestore] = []
    for operation in postgres_restores:
        identity = _preflight_pg_target(operation.database_url)
        existing = physical_identities.get(identity)
        if existing is not None:
            raise BackupError(
                "PostgreSQL restore targets resolve to the same physical "
                f"database: {existing} and {operation.component}"
            )
        physical_identities[identity] = operation.component
        verified_postgres_restores.append(replace(operation, expected_identity=identity))
    postgres_restores = verified_postgres_restores

    if postgres_restores and len(included) > 1 and not allow_cross_store_partial:
        names = ", ".join(included)
        raise BackupError(
            "restore spans PostgreSQL and additional components "
            f"({names}); distributed atomic commit is unavailable. Stop all services "
            "and rerun with --allow-cross-store-partial to explicitly accept that risk"
        )
    if postgres_restores and len(included) > 1:
        recovery_mode = "operator_approved_cross_store_partial"
    elif postgres_restores:
        recovery_mode = "single_postgres_transaction"
    else:
        recovery_mode = "recoverable_local_swaps"

    local_operations: list[_SwapOperation] = []
    for request in local_requests:
        if request.component == "healthmes_db":
            local_operations.extend(
                _sqlite_sidecar_operations(
                    request.destination,
                    transaction_id=transaction_id,
                )
            )
        local_operations.append(
            _plan_local_component(
                component=request.component,
                source=request.source,
                destination=request.destination,
                transaction_id=transaction_id,
                is_tree=request.is_tree,
            )
        )
    journal_path = restore_journal_path(_restore_state_directory(locations))
    _assert_complete_restore_path_graph(
        local_operations,
        locations=locations,
        snapshot_source=snapshot_path.resolve(strict=True),
        extracted_root=extracted.resolve(),
        journal_path=journal_path,
        transaction_id=transaction_id,
    )
    parent_anchors = _bind_operation_parent_anchors(
        local_operations,
        create=True,
    )
    try:
        for operation in local_operations:
            _preflight_anchored_operation(operation)
        if _path_entry_exists(journal_path):
            raise BackupError(
                "an unfinished restore journal already exists; recover it before "
                f"starting another restore: {journal_path}"
            )
        journal_operations: list[JournalOperation] = []
        for operation in local_operations:
            identity = operation.parent_identity
            if identity is None:
                raise BackupError(
                    f"{operation.component} restore target parent is not anchored"
                )
            journal_operations.append(
                JournalOperation(
                    component=operation.component,
                    destination=operation.destination,
                    staged=operation.staged,
                    backup=operation.backup,
                    parent_device=identity.device,
                    parent_inode=identity.inode,
                )
            )
        journal = RestoreJournal(
            transaction_id=transaction_id,
            phase="staging",
            recovery_mode=recovery_mode,
            operations=journal_operations,
            postgres_targets=[
                JournalPostgresTarget(
                    component=operation.component,
                    expected_system_identifier=operation.expected_identity[0],
                    expected_database_oid=operation.expected_identity[1],
                )
                for operation in postgres_restores
                if operation.expected_identity is not None
            ],
        )
        return _RestorePlan(
            transaction_id=transaction_id,
            included=included,
            skipped=skipped,
            recovery_mode=recovery_mode,
            local_operations=local_operations,
            postgres_restores=postgres_restores,
            journal_path=journal_path,
            journal=journal,
            resource_limits=locations.resource_limits,
            parent_anchors=parent_anchors,
        )
    except BaseException:
        _close_operation_parent_anchors(
            local_operations,
            parent_anchors,
        )
        raise


def _stage_restore_plan(
    plan: _RestorePlan,
    *,
    limits: SnapshotResourceLimits,
) -> None:
    _assert_operation_parents_current(plan.local_operations)
    write_restore_journal(plan.journal_path, plan.journal)
    try:
        with _identity_phase_scope(
            limits,
            phase="restore staging identity",
        ):
            for operation, journal_operation in zip(
                plan.local_operations,
                plan.journal.operations,
                strict=True,
            ):
                _stage_planned_operation(operation, limits=limits)
                if operation.staged is not None:
                    operation.staged_identity = (
                        _capture_operation_entry_identity(
                            operation,
                            operation.staged,
                            is_tree=operation.is_tree,
                        )
                    )
                    if operation.staged_identity is None:
                        raise BackupError(
                            f"{operation.component} staged target disappeared "
                            "before its generation could be journaled"
                        )
                    operation.applied_identity = operation.staged_identity
                    journal_operation.staged_identity = (
                        operation.staged_identity
                    )
                    journal_operation.applied_identity = (
                        operation.applied_identity
                    )
                    write_restore_journal(plan.journal_path, plan.journal)
            _assert_operation_parents_current(plan.local_operations)
            plan.journal.phase = "prepared"
            write_restore_journal(plan.journal_path, plan.journal)
    except BaseException as exc:
        cleanup_errors: list[str] = []
        identity_changed = False
        with _identity_phase_scope(
            limits,
            phase="restore staging cleanup identity",
        ):
            for operation in plan.local_operations:
                staged = operation.staged
                if (
                    staged is None
                    or operation.staged_identity is not None
                ):
                    continue
                try:
                    failed_identity = _capture_operation_entry_identity(
                        operation,
                        staged,
                        is_tree=operation.is_tree,
                    )
                except (OSError, BackupError) as identity_exc:
                    cleanup_errors.append(
                        f"{operation.component}={staged}: {identity_exc}"
                    )
                    continue
                if failed_identity is None:
                    continue
                operation.staged_identity = failed_identity
                operation.applied_identity = failed_identity
                identity_changed = True
            persistence_failed = False
            if identity_changed:
                try:
                    # Persist the exact failed generation before its name can
                    # move into the deterministic cleanup quarantine.
                    _persist_plan_journal(plan)
                except (OSError, BackupError) as journal_exc:
                    persistence_failed = True
                    cleanup_errors.append(
                        f"restore staging cleanup journal: {journal_exc}"
                    )
            if not persistence_failed:
                cleanup_errors.extend(
                    _cleanup_restore_artifacts(
                        plan.local_operations,
                    )
                )
        if not cleanup_errors:
            _remove_restore_journal_without_masking(plan.journal_path)
            raise
        try:
            _persist_plan_journal(plan)
        except (OSError, BackupError) as journal_exc:
            cleanup_errors.append(
                f"restore staging failure journal: {journal_exc}"
            )
        raise BackupError(
            "restore staging failed and some decrypted artifacts could not "
            "be removed: " + "; ".join(cleanup_errors)
        ) from exc


def _apply_swap(operation: _SwapOperation) -> None:
    with _operation_parent_scope([operation], create=False):
        _assert_operation_parent_current(operation)
        destination = operation.destination
        destination_exists = _require_operation_entry_identity(
            operation,
            destination,
            is_tree=operation.is_tree,
            expected=operation.rollback_identity,
            label=f"{operation.component} live target",
        )
        if _anchored_metadata(operation, operation.backup) is not None:
            raise BackupError(
                f"{operation.component} rollback path appeared during restore: "
                f"{operation.backup}"
            )
        if operation.staged is not None:
            if not _require_operation_entry_identity(
                operation,
                operation.staged,
                is_tree=operation.is_tree,
                expected=operation.staged_identity,
                label=f"{operation.component} staged target",
            ):
                raise BackupError(
                    f"{operation.component} staged target disappeared during restore: "
                    f"{operation.staged}"
                )
        if destination_exists:
            _rename_operation_entry(operation, destination, operation.backup)
            _fsync_operation_entry(
                operation,
                operation.backup,
                is_tree=operation.is_tree,
            )
        if operation.staged is not None:
            _rename_operation_entry(operation, operation.staged, destination)
            operation.applied = True
            _fsync_operation_entry(
                operation,
                destination,
                is_tree=operation.is_tree,
            )
            if not _require_operation_entry_identity(
                operation,
                destination,
                is_tree=operation.is_tree,
                expected=operation.applied_identity,
                label=f"{operation.component} applied target",
            ):
                raise BackupError(
                    f"{operation.component} applied target disappeared during restore"
                )
        else:
            operation.applied = True
        _assert_operation_parent_current(operation)


class _LocalRollbackErrors(list[str]):
    def __init__(self) -> None:
        super().__init__()
        self.journal_errors: list[str] = []


def _rollback_local_operations(plan: _RestorePlan) -> list[str]:
    errors = _LocalRollbackErrors()
    paired_operations = list(
        zip(
            plan.local_operations,
            plan.journal.operations,
            strict=True,
        )
    )
    try:
        with (
            _identity_phase_scope(
                plan.resource_limits,
                phase="restore rollback identity",
            ),
            _operation_parent_scope(
                plan.local_operations,
                create=False,
            ),
        ):
            for operation, journal_operation in reversed(paired_operations):
                if not operation.applied and not operation.had_original:
                    continue
                journal_operation.state = "rolling_back"
                try:
                    write_restore_journal(plan.journal_path, plan.journal)
                except (OSError, BackupError) as exc:
                    # Journal durability must not prevent the filesystem
                    # rollback itself. The final failure path reports this
                    # separately from an incomplete rollback.
                    errors.journal_errors.append(
                        f"{operation.component}: restore journal persistence "
                        f"failed during rollback start: {exc}"
                    )
                try:
                    destination_exists = (
                        _capture_operation_entry_identity(
                            operation,
                            operation.destination,
                            is_tree=operation.is_tree,
                        )
                        is not None
                    )
                    backup_exists = (
                        _capture_operation_entry_identity(
                            operation,
                            operation.backup,
                            is_tree=operation.is_tree,
                        )
                        is not None
                    )
                    destination_cleanup_exists = (
                        _anchored_metadata(
                            operation,
                            _operation_cleanup_path(
                                operation,
                                operation.destination,
                            ),
                        )
                        is not None
                    )
                    if operation.had_original:
                        if not backup_exists:
                            if operation.applied or not destination_exists:
                                raise OSError(
                                    "expected rollback copy is missing; "
                                    "live target was preserved"
                                )
                        else:
                            if (
                                destination_exists
                                or destination_cleanup_exists
                            ):
                                if not operation.applied:
                                    raise OSError(
                                        "rollback copy and unexpected live target coexist"
                                    )
                                _remove_operation_entry(
                                    operation,
                                    operation.destination,
                                    expected=operation.applied_identity,
                                    label=(
                                        f"{operation.component} applied target"
                                    ),
                                )
                            _require_operation_entry_identity(
                                operation,
                                operation.backup,
                                is_tree=operation.is_tree,
                                expected=operation.rollback_identity,
                                label=(
                                    f"{operation.component} rollback copy"
                                ),
                            )
                            _rename_operation_entry(
                                operation,
                                operation.backup,
                                operation.destination,
                            )
                            _fsync_operation_entry(
                                operation,
                                operation.destination,
                                is_tree=operation.is_tree,
                            )
                    else:
                        if backup_exists:
                            raise OSError("unexpected rollback copy exists")
                        if (
                            operation.applied
                            and operation.staged is not None
                            and (
                                destination_exists
                                or destination_cleanup_exists
                            )
                        ):
                            _require_operation_entry_identity(
                                operation,
                                operation.destination,
                                is_tree=operation.is_tree,
                                expected=operation.applied_identity,
                                label=(
                                    f"{operation.component} applied target"
                                ),
                            )
                            _remove_operation_entry(
                                operation,
                                operation.destination,
                                expected=operation.applied_identity,
                                label=(
                                    f"{operation.component} applied target"
                                ),
                            )
                    operation.applied = False
                    journal_operation.state = "rolled_back"
                except (OSError, BackupError) as exc:
                    errors.append(
                        f"{operation.component}: {exc} "
                        f"(rollback copy retained at {operation.backup})"
                    )
                    continue
                try:
                    write_restore_journal(plan.journal_path, plan.journal)
                except (OSError, BackupError) as exc:
                    errors.journal_errors.append(
                        f"{operation.component}: restore journal persistence "
                        f"failed during rollback completion: {exc}"
                    )
    except (OSError, BackupError) as exc:
        errors.append(f"restore target parent: {exc}")
    return errors


def _cleanup_restore_artifacts(
    operations: list[_SwapOperation],
    *,
    preserve_backups: bool = False,
) -> list[str]:
    if _ACTIVE_IDENTITY_PHASE.get() is None:
        with _identity_phase_scope(
            SnapshotResourceLimits(),
            phase="restore artifact cleanup identity",
        ):
            return _cleanup_restore_artifacts(
                operations,
                preserve_backups=preserve_backups,
            )
    errors: list[str] = []
    active_operations = [
        operation
        for operation in operations
        if operation.parent_anchor is not None
        or operation.parent_identity is not None
        or any(
            path is not None
            and (
                _path_entry_exists(path)
                or _path_entry_exists(_cleanup_quarantine_path(path))
            )
            for path in (operation.staged, operation.backup)
        )
    ]
    try:
        with _operation_parent_scope(active_operations, create=False):
            for operation in operations:
                if operation.parent_anchor is None:
                    continue
                artifacts = (
                    (
                        (
                            operation.staged,
                            operation.staged_identity,
                            f"{operation.component} staged target",
                        ),
                    )
                    if preserve_backups
                    else (
                        (
                            operation.staged,
                            operation.staged_identity,
                            f"{operation.component} staged target",
                        ),
                        (
                            operation.backup,
                            operation.rollback_identity,
                            f"{operation.component} rollback copy",
                        ),
                    )
                )
                for path, expected_identity, label in artifacts:
                    if path is None:
                        continue
                    try:
                        _remove_operation_entry(
                            operation,
                            path,
                            expected=expected_identity,
                            label=label,
                        )
                    except (OSError, BackupError) as exc:
                        detail = f"{operation.component}={path}: {exc}"
                        errors.append(detail)
                        logger.error(
                            "Could not remove restore artifact: %s",
                            detail,
                        )
    except (OSError, BackupError) as exc:
        errors.append(f"restore target parent: {exc}")
        logger.error(
            "Could not anchor restore artifact parent: %s",
            exc,
        )
    return errors


def _bounded_cleanup_restore_artifacts(
    operations: list[_SwapOperation],
    *,
    limits: SnapshotResourceLimits,
    phase: str,
    preserve_backups: bool = False,
) -> list[str]:
    with _identity_phase_scope(limits, phase=phase):
        return _cleanup_restore_artifacts(
            operations,
            preserve_backups=preserve_backups,
        )


def _remove_restore_journal_without_masking(path: Path) -> None:
    """Best-effort cleanup used only while another restore failure is active."""
    try:
        remove_restore_journal(path)
    except BackupError:
        logger.warning(
            "Could not remove restore journal %s while preserving the "
            "original restore failure",
            path,
            exc_info=True,
        )


def _journal_operations_as_swaps(journal: RestoreJournal) -> list[_SwapOperation]:
    operations: list[_SwapOperation] = []
    for operation in journal.operations:
        parent_identity = None
        if (
            operation.parent_device is not None
            and operation.parent_inode is not None
        ):
            parent_identity = _DirectoryIdentity(
                device=operation.parent_device,
                inode=operation.parent_inode,
            )
        operations.append(
            _SwapOperation(
                component=operation.component,
                destination=operation.destination,
                staged=operation.staged,
                backup=operation.backup,
                is_tree=_journal_operation_is_tree(operation),
                had_original=operation.original_existed,
                applied=operation.state == "applied",
                staged_identity=operation.staged_identity,
                rollback_identity=operation.rollback_identity,
                applied_identity=operation.applied_identity,
                parent_identity=parent_identity,
            )
        )
    return operations


def _sqlite_main_restore_operation(
    operations: list[_SwapOperation],
) -> _SwapOperation:
    candidates = [
        operation
        for operation in operations
        if operation.component == "healthmes_db"
        and operation.staged is not None
        and not operation.is_tree
    ]
    if len(candidates) != 1:
        raise BackupError(
            "SQLite restore must contain exactly one main database operation"
        )
    return candidates[0]


@contextmanager
def _sqlite_lock_parent_scope(
    database_url: str,
    *,
    operations: list[_SwapOperation] | None = None,
):
    database = make_url(database_url)
    if (
        database.get_backend_name() != "sqlite"
        or database.database in _SQLITE_MEMORY_DATABASES
    ):
        yield
        return

    if operations is None:
        database_path = Path(database.database).expanduser()
        if not database_path.is_absolute():
            database_path = Path.cwd() / database_path
        anchor = _open_restore_parent(
            database_path.parent,
            create=True,
        )
        try:
            with anchored_sqlite_lock_parent(
                database_url,
                anchor.descriptor,
            ):
                yield
        finally:
            anchor.close()
        return

    operation = _sqlite_main_restore_operation(operations)
    opened = _bind_operation_parent_anchors(
        [operation],
        create=False,
    )
    try:
        _assert_operation_parent_current(operation)
        anchor = operation.parent_anchor
        if anchor is None:
            raise BackupError(
                "SQLite restore target parent is not anchored"
            )
        with anchored_sqlite_lock_parent(
            database_url,
            anchor.descriptor,
        ):
            yield
    finally:
        _close_operation_parent_anchors([operation], opened)


@contextmanager
def _restore_payload_generation_guard(locations: DataLocations):
    """Acquire the payload fence without touching a replaced SQLite parent.

    Recovery journal validation is read-only, so it can safely identify and
    anchor the journaled database parent before the payload lock file is
    opened. The journal is loaded again under restore admission before any
    recovery decision or mutation.
    """
    journal = _load_validated_restore_journal(locations)
    operations = (
        _journal_operations_as_swaps(journal)
        if journal is not None
        else None
    )
    with _sqlite_lock_parent_scope(
        locations.database_url,
        operations=operations,
    ):
        with payload_generation_guard(locations.database_url):
            yield


def _allowed_journal_destinations(locations: DataLocations) -> dict[str, set[Path]]:
    allowed: dict[str, set[Path]] = {}
    database = make_url(locations.database_url)
    if (
        database.get_backend_name() == "sqlite"
        and database.database not in _SQLITE_MEMORY_DATABASES
    ):
        destination = _canonical_restore_target(
            _sqlite_file_path(locations.database_url),
            component="healthmes_db",
        )
        allowed["healthmes_db"] = {
            destination,
            *(Path(f"{destination}{suffix}") for suffix in ("-wal", "-shm", "-journal")),
        }
    for component, target in (
        ("media", locations.media_dir),
        ("raw_ingest", locations.raw_ingest_dir),
        ("hermes_home", locations.hermes_home),
    ):
        if target is not None:
            allowed[component] = {
                _canonical_restore_target(Path(target), component=component)
            }
    return allowed


def _validate_restore_journal(
    journal: RestoreJournal,
    *,
    locations: DataLocations,
) -> None:
    allowed = _allowed_journal_destinations(locations)
    database = make_url(locations.database_url)
    actual_layout = [
        (operation.component, operation.destination)
        for operation in journal.operations
    ]
    expected_layout: list[tuple[str, Path]] = []
    if (
        database.get_backend_name() == "sqlite"
        and database.database not in _SQLITE_MEMORY_DATABASES
    ):
        sqlite_destination = _canonical_restore_target(
            _sqlite_file_path(locations.database_url),
            component="healthmes_db",
        )
        expected_layout.extend(
            (
                ("healthmes_db", Path(f"{sqlite_destination}-wal")),
                ("healthmes_db", Path(f"{sqlite_destination}-shm")),
                ("healthmes_db", Path(f"{sqlite_destination}-journal")),
                ("healthmes_db", sqlite_destination),
            )
        )
    present_components = {operation.component for operation in journal.operations}
    for component in ("media", "raw_ingest", "hermes_home"):
        if component in present_components:
            candidates = allowed.get(component, set())
            if len(candidates) != 1:
                raise BackupError(
                    "restore journal targets do not match the current "
                    "HealthMes configuration"
                )
            expected_layout.append((component, next(iter(candidates))))
    if actual_layout != expected_layout:
        raise BackupError(
            "restore journal local operation set or order is invalid"
        )

    for operation in journal.operations:
        candidates = allowed.get(operation.component, set())
        if operation.destination not in candidates:
            raise BackupError(
                "restore journal targets do not match the current HealthMes configuration"
            )
        expected_backup = operation.destination.with_name(
            f".{operation.destination.name}.healthmes-restore-"
            f"{journal.transaction_id}.backup"
        )
        if operation.backup != expected_backup:
            raise BackupError("restore journal rollback path is invalid")
        is_sqlite_sidecar = (
            operation.component == "healthmes_db"
            and operation.destination.name.endswith(("-wal", "-shm", "-journal"))
        )
        expected_staged = operation.destination.with_name(
            f".{operation.destination.name}.healthmes-restore-"
            f"{journal.transaction_id}.staged"
        )
        if (is_sqlite_sidecar and operation.staged is not None) or (
            not is_sqlite_sidecar and operation.staged != expected_staged
        ):
            raise BackupError("restore journal staging path is invalid")
        if operation.staged is None:
            if (
                operation.staged_identity is not None
                or operation.applied_identity is not None
            ):
                raise BackupError(
                    "restore journal sidecar generation identity is invalid"
                )
        elif (
            operation.staged_identity is None
            or operation.applied_identity is None
        ):
            if journal.phase != "staging":
                raise BackupError(
                    "restore journal lacks a staged generation identity"
                )
        elif operation.staged_identity != operation.applied_identity:
            raise BackupError(
                "restore journal applied generation does not match staging"
            )
        if operation.original_existed is None:
            if operation.rollback_identity is not None:
                raise BackupError(
                    "restore journal records rollback identity before mutation"
                )
        elif operation.original_existed:
            if operation.rollback_identity is None:
                raise BackupError(
                    "restore journal lacks a rollback generation identity"
                )
        elif operation.rollback_identity is not None:
            raise BackupError(
                "restore journal has rollback identity for a new target"
            )

    configured_postgres: list[str] = []
    if database.get_backend_name() == "postgresql":
        configured_postgres.append("healthmes_db")
    if (
        locations.ow_database_url
        and make_url(locations.ow_database_url).get_backend_name() == "postgresql"
    ):
        configured_postgres.append("open_wearables_db")
    postgres_components = [
        target.component for target in journal.postgres_targets
    ]
    if (
        len(postgres_components) != len(set(postgres_components))
        or any(component not in configured_postgres for component in postgres_components)
        or postgres_components
        != [
            component
            for component in configured_postgres
            if component in postgres_components
        ]
        or (
            database.get_backend_name() == "postgresql"
            and "healthmes_db" not in postgres_components
        )
    ):
        raise BackupError(
            "restore journal PostgreSQL targets do not match the current configuration"
        )

    if journal.postgres_targets:
        expected_recovery_mode = (
            "single_postgres_transaction"
            if not journal.operations and len(journal.postgres_targets) == 1
            else "operator_approved_cross_store_partial"
        )
    else:
        expected_recovery_mode = "recoverable_local_swaps"
    if journal.recovery_mode != expected_recovery_mode:
        raise BackupError("restore journal recovery mode is inconsistent")

    local_states = [operation.state for operation in journal.operations]
    postgres_states = [target.state for target in journal.postgres_targets]
    if journal.phase in {"staging", "prepared"}:
        if any(operation.original_existed is not None for operation in journal.operations):
            raise BackupError(
                "restore journal records live target state before mutation"
            )
        if any(state != "pending" for state in local_states):
            raise BackupError("restore journal local state is inconsistent with staging")
        if any(state != "pending" for state in postgres_states):
            raise BackupError(
                "restore journal PostgreSQL state is inconsistent with staging"
            )
        if journal.current_postgres is not None:
            raise BackupError("restore journal current PostgreSQL target is inconsistent")
        return

    if any(operation.original_existed is None for operation in journal.operations):
        raise BackupError("restore journal lacks the original local target state")

    if journal.phase == "applying_local":
        encountered_non_applied = False
        applying_count = 0
        for state in local_states:
            if state == "applied" and not encountered_non_applied:
                continue
            encountered_non_applied = True
            if state == "applying":
                applying_count += 1
            elif state != "pending":
                raise BackupError(
                    "restore journal local apply sequence is inconsistent"
                )
        if applying_count > 1:
            raise BackupError("restore journal has multiple applying operations")
        if any(state != "pending" for state in postgres_states):
            raise BackupError(
                "restore journal PostgreSQL state changed before local apply completed"
            )
        if journal.current_postgres is not None:
            raise BackupError("restore journal current PostgreSQL target is inconsistent")
        return

    if journal.phase == "local_applied":
        if any(state != "applied" for state in local_states):
            raise BackupError("restore journal local apply is incomplete")
        if any(state != "pending" for state in postgres_states):
            raise BackupError(
                "restore journal PostgreSQL state changed before its phase"
            )
        if journal.current_postgres is not None:
            raise BackupError("restore journal current PostgreSQL target is inconsistent")
        return

    if journal.phase == "postgres_in_progress":
        if any(state != "applied" for state in local_states):
            raise BackupError("restore journal local apply is incomplete")
        if journal.current_postgres is None:
            raise BackupError("restore journal current PostgreSQL target is missing")
        current_indexes = [
            index
            for index, target in enumerate(journal.postgres_targets)
            if target.component == journal.current_postgres
        ]
        if len(current_indexes) != 1:
            raise BackupError("restore journal current PostgreSQL target is invalid")
        current_index = current_indexes[0]
        expected_states = (
            ["committed"] * current_index
            + ["applying"]
            + ["pending"] * (len(postgres_states) - current_index - 1)
        )
        if postgres_states != expected_states:
            raise BackupError(
                "restore journal PostgreSQL apply sequence is inconsistent"
            )
        return

    if journal.phase == "committed":
        if any(state != "applied" for state in local_states) or any(
            state != "committed" for state in postgres_states
        ):
            raise BackupError("restore journal committed state is incomplete")
        if journal.current_postgres is not None:
            raise BackupError("restore journal current PostgreSQL target is inconsistent")
        return

    if journal.phase == "rolling_back":
        if journal.current_postgres is not None and journal.current_postgres not in {
            target.component for target in journal.postgres_targets
        }:
            raise BackupError("restore journal current PostgreSQL target is inconsistent")
        return

    if journal.phase == "manual_recovery_required":
        if not any(
            state
            in {
                "committed",
                "unknown",
                "applying",
                "fence_unknown",
                "committed_fence_unknown",
                "unknown_fence_unknown",
            }
            for state in postgres_states
        ):
            raise BackupError(
                "restore journal manual recovery phase lacks an uncertain "
                "PostgreSQL target"
            )
        return

    raise BackupError("restore journal phase is unsupported")


def _journal_operation_is_tree(operation: JournalOperation) -> bool:
    return operation.component in _TREE_COMPONENT_ROOTS


def _validate_journal_entry_type(
    path: Path,
    *,
    is_tree: bool,
    label: str,
) -> bool:
    if not _path_entry_exists(path):
        return False
    if path.is_symlink():
        raise BackupError(f"{label} must not be a symlink: {path}")
    if is_tree and not path.is_dir():
        raise BackupError(f"{label} must be a directory: {path}")
    if not is_tree and not path.is_file():
        raise BackupError(f"{label} must be a regular file: {path}")
    return True


def _recover_one_local_journal_operation(
    journal_operation: JournalOperation,
    operation: _SwapOperation,
    *,
    prior_state: str,
) -> None:
    is_tree = operation.is_tree
    destination_identity = _capture_operation_entry_identity(
        operation,
        operation.destination,
        is_tree=is_tree,
    )
    backup_identity = _capture_operation_entry_identity(
        operation,
        operation.backup,
        is_tree=is_tree,
    )
    staged_identity = None
    if operation.staged is not None:
        staged_identity = _capture_operation_entry_identity(
            operation,
            operation.staged,
            is_tree=is_tree,
        )
    destination_cleanup_exists = (
        _anchored_metadata(
            operation,
            _operation_cleanup_path(operation, operation.destination),
        )
        is not None
    )
    backup_cleanup_exists = (
        _anchored_metadata(
            operation,
            _operation_cleanup_path(operation, operation.backup),
        )
        is not None
    )

    if backup_identity is not None:
        if backup_identity != journal_operation.rollback_identity:
            raise BackupError(
                f"{operation.component} rollback copy changed after the "
                "restore journal was written; preserving all generations"
            )
    if staged_identity is not None:
        if staged_identity != journal_operation.staged_identity:
            raise BackupError(
                f"{operation.component} staged target changed after the "
                "restore journal was written; preserving all generations"
            )
    if backup_cleanup_exists:
        raise BackupError(
            f"{operation.component} rollback cleanup quarantine is unexpected "
            "during rollback; preserving all generations"
        )

    if journal_operation.original_existed:
        if backup_identity is not None:
            if destination_identity is not None or destination_cleanup_exists:
                if prior_state not in {"applying", "applied", "rolling_back"}:
                    raise BackupError(
                        "restore journal is ambiguous: rollback copy and live "
                        "target coexist before an applied operation"
                    )
                if (
                    destination_identity is not None
                    and destination_identity
                    != journal_operation.applied_identity
                ):
                    raise BackupError(
                        f"{operation.component} live target is not the "
                        "journaled restore generation; preserving the newer "
                        f"generation at {operation.destination}"
                    )
                _remove_operation_entry(
                    operation,
                    operation.destination,
                    expected=journal_operation.applied_identity,
                    label=f"{operation.component} applied target",
                )
            _rename_operation_entry(
                operation,
                operation.backup,
                operation.destination,
            )
            _fsync_operation_entry(
                operation,
                operation.destination,
                is_tree=is_tree,
            )
        elif destination_identity is None:
            raise BackupError(
                "restore journal is ambiguous: original local data cannot be located"
            )
        elif destination_identity == journal_operation.rollback_identity:
            # The first rename never happened, or rollback already restored
            # the original before the journal state update.
            if destination_cleanup_exists:
                raise BackupError(
                    f"{operation.component} restored live target coexists with "
                    "a cleanup quarantine; preserving both generations"
                )
        elif destination_identity == journal_operation.applied_identity:
            raise BackupError(
                "restore journal is ambiguous: applied operation lost its rollback copy"
            )
        else:
            raise BackupError(
                f"{operation.component} live target changed after the restore "
                "journal was written; preserving the newer generation"
            )
    else:
        if backup_identity is not None:
            raise BackupError(
                "restore journal is ambiguous: unexpected rollback copy exists"
            )
        if operation.staged is None:
            if destination_cleanup_exists:
                raise BackupError(
                    f"{operation.component} cleanup quarantine is unexpected "
                    "for a SQLite sidecar; preserving it"
                )
            if destination_identity is not None:
                raise BackupError(
                    "restore journal is ambiguous: a new SQLite sidecar appeared"
                )
        elif staged_identity is not None and destination_identity is not None:
            raise BackupError(
                "restore journal is ambiguous: staged and live replacements coexist"
            )
        elif destination_identity is not None and prior_state in {
            "applying",
            "applied",
            "rolling_back",
        }:
            if destination_identity != journal_operation.applied_identity:
                raise BackupError(
                    f"{operation.component} live target is not the journaled "
                    "restore generation; preserving the newer generation"
                )
            _remove_operation_entry(
                operation,
                operation.destination,
                expected=journal_operation.applied_identity,
                label=f"{operation.component} applied target",
            )
        elif destination_cleanup_exists and prior_state in {
            "applying",
            "applied",
            "rolling_back",
        }:
            _remove_operation_entry(
                operation,
                operation.destination,
                expected=journal_operation.applied_identity,
                label=f"{operation.component} applied target",
            )
        elif destination_cleanup_exists:
            raise BackupError(
                f"{operation.component} cleanup quarantine is inconsistent "
                "with the restore journal; preserving it"
            )
        elif destination_identity is not None:
            raise BackupError(
                "restore journal is ambiguous: an unexpected local target appeared"
            )

    if operation.staged is not None:
        _remove_operation_entry(
            operation,
            operation.staged,
            expected=journal_operation.staged_identity,
            label=f"{operation.component} staged target",
        )


def _recover_local_journal(
    journal: RestoreJournal,
    *,
    journal_path: Path,
) -> None:
    swaps = _journal_operations_as_swaps(journal)
    with _operation_parent_scope(swaps, create=False):
        _assert_operation_parents_current(swaps)
        if journal.phase in {"staging", "prepared"}:
            for journal_operation, operation in zip(
                journal.operations,
                swaps,
                strict=True,
            ):
                if _anchored_metadata(operation, operation.backup) is not None:
                    raise BackupError(
                        "restore journal is ambiguous: rollback data exists "
                        "before live mutation"
                    )
                if journal_operation.staged is not None:
                    _remove_operation_entry(
                        operation,
                        operation.staged,
                        expected=journal_operation.staged_identity,
                        label=(
                            f"{operation.component} staged target"
                        ),
                    )
            return

        if journal.phase != "rolling_back":
            journal.phase = "rolling_back"
            journal.current_postgres = None
            write_restore_journal(journal_path, journal)
        for journal_operation, operation in reversed(
            list(zip(journal.operations, swaps, strict=True))
        ):
            prior_state = journal_operation.state
            if journal_operation.state != "rolled_back":
                journal_operation.state = "rolling_back"
                write_restore_journal(journal_path, journal)
            _recover_one_local_journal_operation(
                journal_operation,
                operation,
                prior_state=prior_state,
            )
            journal_operation.state = "rolled_back"
            write_restore_journal(journal_path, journal)


def _validate_committed_local_state(journal: RestoreJournal) -> None:
    swaps = _journal_operations_as_swaps(journal)
    with _operation_parent_scope(swaps, create=False):
        _assert_operation_parents_current(swaps)
        for journal_operation, operation in zip(
            journal.operations,
            swaps,
            strict=True,
        ):
            is_tree = operation.is_tree
            destination_exists = _require_operation_entry_identity(
                operation,
                operation.destination,
                is_tree=is_tree,
                expected=journal_operation.applied_identity,
                label=f"{operation.component} committed live target",
            )
            if operation.staged is not None:
                if not destination_exists:
                    raise BackupError(
                        "committed restore journal is missing its live replacement: "
                        f"{operation.destination}"
                    )
                if _anchored_metadata(operation, operation.staged) is not None:
                    _require_operation_entry_identity(
                        operation,
                        operation.staged,
                        is_tree=is_tree,
                        expected=journal_operation.staged_identity,
                        label=f"{operation.component} staged target",
                    )
            elif destination_exists:
                raise BackupError(
                    "committed restore journal contains an unexpected SQLite "
                    f"sidecar: {operation.destination}"
                )
            backup_exists = (
                _anchored_metadata(operation, operation.backup) is not None
            )
            if not journal_operation.original_existed and backup_exists:
                raise BackupError(
                    "committed restore journal contains an unexpected rollback "
                    f"copy: {operation.backup}"
                )
            if backup_exists:
                _require_operation_entry_identity(
                    operation,
                    operation.backup,
                    is_tree=is_tree,
                    expected=journal_operation.rollback_identity,
                    label=f"{operation.component} rollback copy",
                )


def _load_validated_restore_journal(
    locations: DataLocations,
) -> RestoreJournal | None:
    journal_path = restore_journal_path(
        _restore_state_directory(locations)
    )
    journal = load_restore_journal(journal_path)
    if journal is not None:
        _validate_restore_journal(journal, locations=locations)
    return journal


def _recover_incomplete_restore_locked(locations: DataLocations) -> None:
    """Recover an interrupted restore while admission and runtime are held."""
    journal_path = restore_journal_path(_restore_state_directory(locations))
    journal = _load_validated_restore_journal(locations)
    if journal is None:
        return
    if journal.phase == "committed":
        with _identity_phase_scope(
            locations.resource_limits,
            phase="committed restore recovery identity",
        ):
            _validate_committed_local_state(journal)
        operations = _journal_operations_as_swaps(journal)
        cleanup_errors = _bounded_cleanup_restore_artifacts(
            operations,
            limits=locations.resource_limits,
            phase="committed restore cleanup identity",
        )
        if cleanup_errors:
            raise BackupError(
                "committed restore cleanup is incomplete: " + "; ".join(cleanup_errors)
            )
        remove_restore_journal(journal_path)
        return
    postgres_states = {target.state for target in journal.postgres_targets}
    postgres_requires_manual_recovery = (
        journal.phase in {"postgres_in_progress", "manual_recovery_required"}
        or postgres_states
        & {
            "applying",
            "committed",
            "unknown",
            "fence_unknown",
            "committed_fence_unknown",
            "unknown_fence_unknown",
        }
    )
    if postgres_requires_manual_recovery:
        try:
            if any(
                operation.state != "rolled_back"
                for operation in journal.operations
            ):
                with _identity_phase_scope(
                    locations.resource_limits,
                    phase="interrupted restore recovery identity",
                ):
                    _recover_local_journal(
                        journal,
                        journal_path=journal_path,
                    )
            journal.phase = "manual_recovery_required"
            journal.current_postgres = None
            write_restore_journal(journal_path, journal)
        except OSError as exc:
            raise BackupError(
                "could not roll back local components from interrupted restore "
                f"{journal_path}: {exc}"
            ) from exc
        if postgres_states & {
            "fence_unknown",
            "committed_fence_unknown",
            "unknown_fence_unknown",
        }:
            detail = (
                "an interrupted restore left PostgreSQL connection admission "
                "uncertain"
            )
        else:
            detail = "an interrupted restore may have changed PostgreSQL"
        local_detail = (
            "; local components were rolled back"
            if journal.operations
            else ""
        )
        raise BackupError(
            f"{detail}{local_detail}; HealthMes is "
            "stopped until the listed database targets are inspected and the "
            f"restore journal is resolved: {journal_path}"
        )
    try:
        with _identity_phase_scope(
            locations.resource_limits,
            phase="interrupted restore recovery identity",
        ):
            _recover_local_journal(
                journal,
                journal_path=journal_path,
            )
    except OSError as exc:
        raise BackupError(
            f"could not recover interrupted local restore from {journal_path}: {exc}"
        ) from exc
    remove_restore_journal(journal_path)


def _recovery_requires_payload_guard(locations: DataLocations) -> bool:
    """Return whether crash recovery can mutate a local payload generation."""
    database = make_url(locations.database_url)
    return (
        database.get_backend_name() != "postgresql"
        or bool(_configured_local_restore_targets(locations))
    )


@contextmanager
def recovered_runtime_guard(locations: DataLocations):
    """Recover under admission, then retain only the SQLite runtime fence."""
    from contextlib import ExitStack

    database = make_url(locations.database_url)
    if (
        database.get_backend_name() == "sqlite"
        and database.database in _SQLITE_MEMORY_DATABASES
    ):
        yield
        return

    runtime_stack = ExitStack()
    try:
        if not _recovery_requires_payload_guard(locations):
            with restore_admission_guard(locations):
                _recover_incomplete_restore_locked(locations)
        else:
            with _restore_payload_generation_guard(locations):
                with restore_admission_guard(locations):
                    journal = _load_validated_restore_journal(locations)
                    operations = (
                        _journal_operations_as_swaps(journal)
                        if journal is not None
                        else None
                    )
                    with _sqlite_lock_parent_scope(
                        locations.database_url,
                        operations=operations,
                    ):
                        runtime_stack.enter_context(
                            _sqlite_restore_runtime_guard(locations.database_url)
                        )
                        with global_write_plane_guard(locations.database_url):
                            _recover_incomplete_restore_locked(locations)
    except BaseException:
        runtime_stack.close()
        raise
    try:
        yield
    finally:
        runtime_stack.close()


def recover_incomplete_restore(locations: DataLocations) -> None:
    """Recover a crash-interrupted local restore or fail closed for ambiguity."""
    database = make_url(locations.database_url)
    if (
        database.get_backend_name() == "sqlite"
        and database.database in _SQLITE_MEMORY_DATABASES
    ):
        return
    if not _recovery_requires_payload_guard(locations):
        with restore_admission_guard(locations):
            _recover_incomplete_restore_locked(locations)
        return
    with _restore_payload_generation_guard(locations):
        with restore_admission_guard(locations):
            journal = _load_validated_restore_journal(locations)
            operations = (
                _journal_operations_as_swaps(journal)
                if journal is not None
                else None
            )
            with _sqlite_lock_parent_scope(
                locations.database_url,
                operations=operations,
            ):
                with _sqlite_restore_runtime_guard(locations.database_url):
                    with global_write_plane_guard(locations.database_url):
                        _recover_incomplete_restore_locked(locations)


def _persist_plan_journal(plan: _RestorePlan) -> None:
    for operation, journal_operation in zip(
        plan.local_operations,
        plan.journal.operations,
        strict=True,
    ):
        journal_operation.original_existed = operation.had_original
        journal_operation.staged_identity = operation.staged_identity
        journal_operation.rollback_identity = (
            operation.rollback_identity
        )
        journal_operation.applied_identity = operation.applied_identity
        if operation.applied:
            journal_operation.state = "applied"
    write_restore_journal(plan.journal_path, plan.journal)


def _persist_plan_journal_for_failure(
    plan: _RestorePlan,
    journal_errors: list[str],
    *,
    phase: str,
) -> None:
    try:
        _persist_plan_journal(plan)
    except (OSError, BackupError) as exc:
        journal_errors.append(f"{phase}: {exc}")


def _assert_postgres_target_identity(
    operation: _PostgresRestore,
    *,
    phase: str,
) -> tuple[str, int]:
    expected = operation.expected_identity
    if expected is None:
        raise BackupError(
            f"PostgreSQL restore target identity is missing for {operation.component}"
        )
    current = _preflight_pg_target(operation.database_url)
    if current != expected:
        raise BackupError(
            "PostgreSQL restore target identity changed since preflight for "
            f"{operation.component} during {phase}; expected "
            f"cluster={expected[0]} database_oid={expected[1]}, got "
            f"cluster={current[0]} database_oid={current[1]}"
        )
    return current


def _revalidate_postgres_targets(operations: list[_PostgresRestore]) -> None:
    """Reject routing/failover drift before any live restore mutation."""
    physical_identities: dict[tuple[str, int], str] = {}
    for operation in operations:
        identity = _assert_postgres_target_identity(
            operation,
            phase="pre-mutation revalidation",
        )
        existing = physical_identities.get(identity)
        if existing is not None:
            raise BackupError(
                "PostgreSQL restore targets now resolve to the same physical "
                f"database: {existing} and {operation.component}"
            )
        physical_identities[identity] = operation.component


def _raise_restore_failure(
    plan: _RestorePlan,
    committed_postgres: list[str],
    uncertain_postgres: list[str],
    fence_uncertain_postgres: dict[str, str],
    exc: BaseException,
) -> NoReturn:
    journal_errors: list[str] = []
    local_mutation_intent = any(
        operation.had_original is not None
        for operation in plan.local_operations
    )
    if local_mutation_intent:
        plan.journal.phase = "rolling_back"
    elif committed_postgres or uncertain_postgres or fence_uncertain_postgres:
        plan.journal.phase = "manual_recovery_required"
    else:
        plan.journal.phase = "prepared"
        plan.journal.current_postgres = None
        for target in plan.journal.postgres_targets:
            target.state = "pending"
    for target in plan.journal.postgres_targets:
        fence_outcome = fence_uncertain_postgres.get(target.component)
        if fence_outcome == "committed":
            target.state = "committed_fence_unknown"
        elif fence_outcome == "unknown":
            target.state = "unknown_fence_unknown"
        elif fence_outcome == "not_started":
            target.state = "fence_unknown"
        elif target.component in committed_postgres:
            target.state = "committed"
        elif target.component in uncertain_postgres:
            target.state = "unknown"
    _persist_plan_journal_for_failure(
        plan,
        journal_errors,
        phase="before local rollback",
    )
    rollback_errors = _rollback_local_operations(plan)
    journal_errors.extend(
        getattr(rollback_errors, "journal_errors", ())
    )
    if not rollback_errors:
        for operation in plan.journal.operations:
            operation.state = "rolled_back"
    if rollback_errors:
        if committed_postgres or uncertain_postgres or fence_uncertain_postgres:
            plan.journal.phase = "manual_recovery_required"
        _persist_plan_journal_for_failure(
            plan,
            journal_errors,
            phase="after incomplete local rollback",
        )
        cleanup_errors = _bounded_cleanup_restore_artifacts(
            plan.local_operations,
            limits=plan.resource_limits,
            phase="failed restore artifact cleanup identity",
            preserve_backups=True,
        )
        details = [f"original restore failure: {exc}"]
        if journal_errors:
            details.append(
                "restore journal persistence failures: "
                + " | ".join(journal_errors)
            )
        details.append(
            "local rollback failures: " + "; ".join(rollback_errors)
        )
        if cleanup_errors:
            details.append(
                "additional decrypted restore artifacts could not be removed: "
                + "; ".join(cleanup_errors)
            )
        if committed_postgres:
            details.append(
                "committed PostgreSQL components cannot be "
                f"automatically reverted: {committed_postgres}"
            )
        if uncertain_postgres:
            details.append(
                "PostgreSQL commit outcome is unknown and requires operator "
                f"inspection for: {uncertain_postgres}"
            )
        if fence_uncertain_postgres:
            details.append(
                "PostgreSQL connection admission is unknown and requires "
                f"operator inspection for: {fence_uncertain_postgres}"
            )
        raise BackupError(
            "restore failed and local rollback was incomplete: "
            + "; ".join(details)
        ) from exc
    cleanup_errors = _bounded_cleanup_restore_artifacts(
        plan.local_operations,
        limits=plan.resource_limits,
        phase="rolled-back restore artifact cleanup identity",
    )
    if committed_postgres or uncertain_postgres or fence_uncertain_postgres:
        plan.journal.phase = "manual_recovery_required"
        plan.journal.current_postgres = None
        _persist_plan_journal_for_failure(
            plan,
            journal_errors,
            phase="after local rollback",
        )
        state: list[str] = []
        if committed_postgres:
            state.append(f"confirmed committed={committed_postgres}")
        if uncertain_postgres:
            state.append(f"commit outcome unknown={uncertain_postgres}")
        if fence_uncertain_postgres:
            state.append(
                "connection admission unknown="
                f"{fence_uncertain_postgres}"
            )
        local_state = (
            "local components were rolled back"
            if plan.local_operations
            else "no local components required rollback"
        )
        message = (
            f"restore failed during PostgreSQL execution: {exc}; "
            + ", ".join(state)
            + f"; {local_state}; inspect every listed PostgreSQL target before retrying"
        )
    elif isinstance(exc, TimeoutError):
        message = "timed out waiting for the HealthMes write plane; nothing was restored"
    elif isinstance(exc, BackupError):
        if not cleanup_errors and not journal_errors:
            _remove_restore_journal_without_masking(plan.journal_path)
            raise exc
        message = str(exc)
    else:
        message = f"restore failed before completion: {exc}"
    if cleanup_errors:
        _persist_plan_journal_for_failure(
            plan,
            journal_errors,
            phase="after restore artifact cleanup failure",
        )
    if journal_errors:
        message += (
            "; local rollback completed, but restore journal persistence failed: "
            + " | ".join(journal_errors)
        )
    if cleanup_errors:
        message += (
            "; local rollback completed, but decrypted restore artifacts "
            "could not be removed: " + "; ".join(cleanup_errors)
        )
    elif (
        not committed_postgres
        and not uncertain_postgres
        and not fence_uncertain_postgres
    ):
        _remove_restore_journal_without_masking(plan.journal_path)
    raise BackupError(message) from exc


@contextmanager
def _sqlite_restore_runtime_guard(database_url: str):
    try:
        with sqlite_runtime_guard(
            database_url,
            timeout_seconds=_SQLITE_RESTORE_RUNTIME_LOCK_TIMEOUT_SECONDS,
        ):
            yield
    except TimeoutError as exc:
        raise BackupError(
            "SQLite restore target is attached to a running HealthMes process; "
            "stop HealthMes and retry the restore"
        ) from exc


def _apply_restore_plan(
    plan: _RestorePlan,
    *,
    locations: DataLocations,
) -> None:
    committed_postgres: list[str] = []
    uncertain_postgres: list[str] = []
    fence_uncertain_postgres: dict[str, str] = {}
    rollback_attempted = False
    rollback_failure: BaseException | None = None
    restore_body_completed = False

    @contextmanager
    def restore_failure_boundary():
        nonlocal rollback_attempted, rollback_failure
        try:
            yield
        except BaseException as exc:
            # Keep the cross-process fence held until the previous local
            # generation is fully restored. Otherwise a waiting writer could
            # commit into the failed generation and be erased by rollback.
            rollback_attempted = True
            try:
                _raise_restore_failure(
                    plan,
                    committed_postgres,
                    uncertain_postgres,
                    fence_uncertain_postgres,
                    exc,
                )
            except BaseException as failure:
                rollback_failure = failure
                raise

    try:
        with _sqlite_lock_parent_scope(
            locations.database_url,
            operations=plan.local_operations,
        ):
            with _sqlite_restore_runtime_guard(locations.database_url):
                with (
                    global_write_plane_guard(
                        locations.database_url
                    ) as guard_connection,
                    restore_failure_boundary(),
                    _identity_phase_scope(
                        locations.resource_limits,
                        phase="restore live apply identity",
                    ),
                ):
                    _assert_operation_parents_current(
                        plan.local_operations
                    )
                    guard_backend_pid: int | None = None
                    if guard_connection is not None:
                        raw_guard_backend_pid = guard_connection.scalar(
                            text("SELECT pg_backend_pid()")
                        )
                        if (
                            not isinstance(raw_guard_backend_pid, int)
                            or isinstance(raw_guard_backend_pid, bool)
                            or raw_guard_backend_pid <= 0
                        ):
                            raise BackupError(
                                "PostgreSQL write-plane guard returned an invalid backend PID"
                            )
                        guard_backend_pid = raw_guard_backend_pid
                    _revalidate_postgres_targets(plan.postgres_restores)
                    plan.journal.phase = "applying_local"
                    for operation, journal_operation in zip(
                        plan.local_operations,
                        plan.journal.operations,
                        strict=True,
                    ):
                        operation.rollback_identity = (
                            _capture_operation_entry_identity(
                                operation,
                                operation.destination,
                                is_tree=operation.is_tree,
                            )
                        )
                        operation.had_original = (
                            operation.rollback_identity is not None
                        )
                        journal_operation.original_existed = operation.had_original
                    _persist_plan_journal(plan)
                    for operation, journal_operation in zip(
                        plan.local_operations,
                        plan.journal.operations,
                        strict=True,
                    ):
                        journal_operation.state = "applying"
                        _persist_plan_journal(plan)
                        _apply_swap(operation)
                        journal_operation.state = "applied"
                        _persist_plan_journal(plan)
                    plan.journal.phase = "local_applied"
                    _persist_plan_journal(plan)
                    if not plan.postgres_restores:
                        # A local-only restore is already durable at this
                        # point. Persist that fact before deleting rollback
                        # copies so startup recovery keeps the new generation
                        # after a crash during artifact cleanup.
                        plan.journal.phase = "committed"
                        plan.journal.current_postgres = None
                        _persist_plan_journal(plan)
                    for postgres_index, operation in enumerate(
                        plan.postgres_restores
                    ):
                        journal_target = next(
                            target
                            for target in plan.journal.postgres_targets
                            if target.component == operation.component
                        )
                        plan.journal.phase = "postgres_in_progress"
                        plan.journal.current_postgres = operation.component
                        journal_target.state = "applying"
                        _persist_plan_journal(plan)
                        try:
                            try:
                                _assert_postgres_target_identity(
                                    operation,
                                    phase="immediately before pg_restore",
                                )
                            except BackupError as exc:
                                raise _PostgresRestoreNotStarted(str(exc)) from exc
                            expected_identity = operation.expected_identity
                            if expected_identity is None:
                                raise _PostgresRestoreNotStarted(
                                    "PostgreSQL restore target identity is missing for "
                                    f"{operation.component}"
                                )
                            _pg_restore_from(
                                operation.database_url,
                                operation.dump_path,
                                expected_identity,
                                protected_backend_pids=(
                                    {guard_backend_pid}
                                    if operation.component == "healthmes_db"
                                    and guard_backend_pid is not None
                                    else set()
                                ),
                                limits=locations.resource_limits,
                            )
                        except _PostgresConnectionFenceUncertain as exc:
                            fence_uncertain_postgres[operation.component] = (
                                exc.restore_outcome
                            )
                            if exc.restore_outcome == "committed":
                                committed_postgres.append(operation.component)
                                journal_target.state = "committed_fence_unknown"
                            elif exc.restore_outcome == "unknown":
                                uncertain_postgres.append(operation.component)
                                journal_target.state = "unknown_fence_unknown"
                            else:
                                journal_target.state = "fence_unknown"
                            plan.journal.phase = "manual_recovery_required"
                            plan.journal.current_postgres = None
                            _persist_plan_journal(plan)
                            raise
                        except _PostgresRestoreNotStarted:
                            journal_target.state = "pending"
                            plan.journal.phase = (
                                "manual_recovery_required"
                                if committed_postgres
                                else "local_applied"
                            )
                            plan.journal.current_postgres = None
                            _persist_plan_journal(plan)
                            raise
                        except BaseException:
                            # A non-zero client exit cannot prove that the
                            # server did not commit before its acknowledgement
                            # was lost. Report the component as unknown rather
                            # than claiming a fully rolled-back generation.
                            uncertain_postgres.append(operation.component)
                            journal_target.state = "unknown"
                            plan.journal.phase = "manual_recovery_required"
                            plan.journal.current_postgres = None
                            _persist_plan_journal(plan)
                            raise
                        committed_postgres.append(operation.component)
                        journal_target.state = "committed"
                        next_index = postgres_index + 1
                        if next_index < len(plan.postgres_restores):
                            next_component = plan.postgres_restores[
                                next_index
                            ].component
                            next_target = next(
                                target
                                for target in plan.journal.postgres_targets
                                if target.component == next_component
                            )
                            next_target.state = "applying"
                            plan.journal.phase = "postgres_in_progress"
                            plan.journal.current_postgres = next_component
                        else:
                            plan.journal.phase = "committed"
                            plan.journal.current_postgres = None
                        _persist_plan_journal(plan)
                    restore_body_completed = True
    except BaseException as exc:
        if rollback_attempted:
            if exc is rollback_failure:
                raise
            original = (
                str(rollback_failure)
                if rollback_failure is not None
                else "restore rollback failed without a complete error report"
            )
            raise BackupError(
                f"{original}; additionally, releasing the HealthMes write plane failed: {exc}"
            ) from exc
        if restore_body_completed:
            # The guard's __exit__ failed after every component was applied.
            # It may already have released the cross-process lock, so rolling
            # back here could erase a write that committed in that interval.
            # Keep the restored generation and report the precise state.
            cleanup_errors = _bounded_cleanup_restore_artifacts(
                plan.local_operations,
                limits=plan.resource_limits,
                phase="committed restore artifact cleanup identity",
            )
            if not cleanup_errors:
                _remove_restore_journal_without_masking(
                    plan.journal_path
                )
            message = (
                "all restore components were applied, but releasing the "
                "HealthMes write plane failed; the restored generation remains "
                f"active and was not rolled back: {exc}"
            )
            if cleanup_errors:
                message += "; decrypted restore artifacts could not be removed: " + "; ".join(
                    cleanup_errors
                )
            raise BackupError(message) from exc
        # Guard acquisition can fail before any live mutation.
        _raise_restore_failure(
            plan,
            committed_postgres,
            uncertain_postgres,
            fence_uncertain_postgres,
            exc,
        )
    cleanup_errors = _bounded_cleanup_restore_artifacts(
        plan.local_operations,
        limits=plan.resource_limits,
        phase="committed restore artifact cleanup identity",
    )
    if cleanup_errors:
        raise BackupError(
            "all restore components were applied and the restored generation "
            "remains active, but decrypted restore artifacts could not be removed: "
            + "; ".join(cleanup_errors)
        )
    remove_restore_journal(plan.journal_path)


def _restore_snapshot_admitted(
    path: Path,
    *,
    passphrase: str,
    locations: DataLocations,
    allow_cross_store_partial: bool = False,
    snapshot_handle: BinaryIO | None = None,
    decrypted_data: bytes | None = None,
) -> RestoreResult:
    data = (
        decrypted_data
        if decrypted_data is not None
        else (
            _decrypt_snapshot_handle(
                path,
                snapshot_handle,
                passphrase,
                limits=locations.resource_limits,
            )
            if snapshot_handle is not None
            else _decrypt_snapshot(
                path,
                passphrase,
                limits=locations.resource_limits,
            )
        )
    )
    scratch = Path(tempfile.mkdtemp(prefix="healthmes-restore-"))
    restore_error: BaseException | None = None
    plan: _RestorePlan | None = None
    try:
        tmp = scratch
        extracted = Path(tmp) / "extracted"
        extracted.mkdir()
        with _open_snapshot_archive(data) as tar:
            expanded_bytes = _enforce_archive_resource_limits(
                tar,
                compressed_bytes=len(data),
                limits=locations.resource_limits,
            )
            manifest = _manifest_from_tar(tar)
            layout = _validate_manifest_layout(manifest)
            _validate_archive_members(tar, layout)
            _require_disk_capacity(
                extracted,
                payload_bytes=expanded_bytes,
                limits=locations.resource_limits,
                label="snapshot restore extraction",
            )
            tar.extractall(extracted, filter="data")
        _verify_inventory(extracted, layout)
        plan = _preflight_restore(
            extracted,
            layout,
            locations,
            snapshot_path=path,
            allow_cross_store_partial=allow_cross_store_partial,
        )
        _stage_restore_plan(plan, limits=locations.resource_limits)
        _apply_restore_plan(plan, locations=locations)
    except BaseException as exc:
        restore_error = exc
        raise
    finally:
        if plan is not None and plan.parent_anchors:
            _close_operation_parent_anchors(
                plan.local_operations,
                plan.parent_anchors,
            )
            plan.parent_anchors.clear()
        cleanup_error = _remove_restore_scratch(scratch)
        if cleanup_error:
            if restore_error is not None:
                raise BackupError(
                    f"{restore_error}; decrypted restore scratch could not be removed: "
                    f"{cleanup_error}"
                ) from restore_error
            raise BackupError(
                "restore completed and the restored generation remains active, "
                "but decrypted restore scratch could not be removed: "
                f"{cleanup_error}"
            )
    logger.info(
        "Snapshot restored from %s; recovered=%s skipped=%s mode=%s",
        path,
        plan.included,
        plan.skipped,
        plan.recovery_mode,
    )
    return RestoreResult(
        manifest,
        recovery_mode=plan.recovery_mode,
        recovered_components=plan.included,
        skipped_components=plan.skipped,
    )


def _validate_restore_payload_before_admission(
    path: Path,
    handle: BinaryIO,
    *,
    passphrase: str,
    locations: DataLocations,
    allow_cross_store_partial: bool,
) -> bytes:
    """Validate one exact encrypted generation before acquiring live locks."""
    data = _decrypt_snapshot_handle(
        path,
        handle,
        passphrase,
        limits=locations.resource_limits,
    )
    manifest = _read_manifest_data(
        data,
        limits=locations.resource_limits,
    )
    layout = _validate_manifest_layout(manifest)
    _validate_restore_request_contract(
        layout,
        locations,
        allow_cross_store_partial=allow_cross_store_partial,
    )
    return data


def restore_snapshot(
    path: Path,
    *,
    passphrase: str,
    locations: DataLocations,
    allow_cross_store_partial: bool = False,
    snapshot_handle: BinaryIO | None = None,
) -> RestoreResult:
    """Validate, preflight, stage, and recoverably restore one snapshot.

    Local files and trees are staged beside their destinations and retain
    rollback copies until every component succeeds. PostgreSQL expands each
    dump before mutation, then runs its target identity assertion and restore
    SQL in one psql connection and transaction. A restore spanning a
    PostgreSQL database and any additional component fails closed unless the
    operator explicitly accepts the cross-store atomicity limitation.
    """
    path = Path(path).expanduser()
    database = make_url(locations.database_url)
    if (
        database.get_backend_name() == "postgresql"
        and locations.restore_state_dir is None
    ):
        raise BackupError(
            "restore_state_dir must be configured for a PostgreSQL restore"
        )
    if snapshot_handle is None:
        try:
            with open_regular_file(path) as opened_snapshot:
                return restore_snapshot(
                    path,
                    passphrase=passphrase,
                    locations=locations,
                    allow_cross_store_partial=allow_cross_store_partial,
                    snapshot_handle=opened_snapshot,
                )
        except BackupError:
            raise
        except FileNotFoundError as exc:
            raise BackupError(f"snapshot not found: {path}") from exc
        except OSError as exc:
            raise BackupError(
                f"could not open encrypted snapshot {path}: {exc}"
            ) from exc
    decrypted_data = _validate_restore_payload_before_admission(
        path,
        snapshot_handle,
        passphrase=passphrase,
        locations=locations,
        allow_cross_store_partial=allow_cross_store_partial,
    )
    with (
        _postgres_tool_timeout_scope(
            locations.postgres_tool_timeout_seconds
        ),
        _restore_payload_generation_guard(locations),
    ):
        with restore_admission_guard(locations):
            journal = _load_validated_restore_journal(locations)
            if journal is not None:
                operations = _journal_operations_as_swaps(journal)
                with _sqlite_lock_parent_scope(
                    locations.database_url,
                    operations=operations,
                ):
                    with _sqlite_restore_runtime_guard(
                        locations.database_url
                    ):
                        with global_write_plane_guard(
                            locations.database_url
                        ):
                            _recover_incomplete_restore_locked(locations)
            return _restore_snapshot_admitted(
                path,
                passphrase=passphrase,
                locations=locations,
                allow_cross_store_partial=allow_cross_store_partial,
                snapshot_handle=snapshot_handle,
                decrypted_data=decrypted_data,
            )
