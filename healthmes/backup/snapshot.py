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

import hashlib
import io
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyrage
from pyrage import passphrase as age_passphrase
from sqlalchemy.engine import URL, make_url

from healthmes import __version__
from healthmes.backup.provider import (
    BackupError,
    SnapshotIntegrityError,
    WrongPassphraseError,
)
from healthmes.config import Settings

__all__ = [
    "SCHEMA_VERSION",
    "SNAPSHOT_PREFIX",
    "SNAPSHOT_SUFFIX",
    "DataLocations",
    "create_snapshot",
    "find_pg_tool",
    "libpq_env",
    "libpq_url",
    "parse_snapshot_name",
    "read_manifest",
    "resolve_backup_dir",
    "resolve_data_locations",
    "resolve_passphrase",
    "restore_snapshot",
    "snapshot_name",
]

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SNAPSHOT_PREFIX = "healthmes-backup-"
SNAPSHOT_SUFFIX = ".tar.gz.age"
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"

MANIFEST_ARCNAME = "manifest.json"
HEALTHMES_SQLITE_ARCNAME = "db/healthmes.sqlite3"
HEALTHMES_PG_DUMP_ARCNAME = "db/healthmes.dump"
OW_PG_DUMP_ARCNAME = "db/open_wearables.dump"
MEDIA_ARCROOT = "media"
HERMES_ARCROOT = "hermes"

_SQLITE_MEMORY_DATABASES = (None, "", ":memory:")

# Keg-only formulae probed (in order) when pg_dump/pg_restore is not on PATH.
_BREW_POSTGRES_FORMULAE = ("postgresql@16", "libpq", "postgresql")


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

    ``ow_database_url``, ``media_dir`` and ``hermes_home`` are optional
    sections: unset (or missing on disk at export time) sections are recorded
    as absent in the manifest and skipped symmetrically on restore.
    """

    database_url: str
    ow_database_url: str | None = None
    media_dir: Path | None = None
    hermes_home: Path | None = None


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


def resolve_data_locations(settings: Settings) -> DataLocations:
    """Derive the live data locations covered by a snapshot from Settings.

    - healthmes database: ``Settings.database_url`` (always included);
    - open-wearables database: optional — ``Settings.ow_database_url`` or
      the ``HEALTHMES_OW_DATABASE_URL`` env var (direct postgres URL; the
      REST ``ow_base_url`` cannot produce a dump);
    - media: always ``{data_dir}/media`` (healthmes/api/food.py convention);
    - Hermes state: optional — ``Settings.hermes_home`` or the vendor's own
      ``HERMES_HOME`` env var; only included "when configured" (PLAN §9).
    """
    ow_database_url = _unwrap_secret(getattr(settings, "ow_database_url", None)) or _unwrap_secret(
        os.environ.get("HEALTHMES_OW_DATABASE_URL")
    )
    hermes_home = getattr(settings, "hermes_home", None)
    if not hermes_home:
        env_home = os.environ.get("HERMES_HOME", "").strip()
        hermes_home = Path(env_home).expanduser() if env_home else None
    return DataLocations(
        database_url=settings.database_url,
        ow_database_url=ow_database_url,
        media_dir=Path(settings.data_dir) / "media",
        hermes_home=Path(hermes_home) if hermes_home else None,
    )


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
    safe = URL.create(
        drivername=url.get_backend_name(),
        username=url.username,
        host=url.host,
        port=url.port,
        database=url.database,
        query=url.query,
    )
    return safe.render_as_string(hide_password=False)


def libpq_env(sqlalchemy_url: str) -> dict[str, str]:
    """Process environment for a pg tool run, carrying the password privately."""
    env = dict(os.environ)
    password = make_url(sqlalchemy_url).password
    if password:
        env["PGPASSWORD"] = str(password)
    return env


def _run_pg_tool(
    tool: str, args: list[str], *, action: str, env: dict[str, str] | None = None
) -> None:
    binary = find_pg_tool(tool)
    if binary is None:
        raise BackupError(
            f"{tool} not found on PATH and no Homebrew postgres keg detected; "
            f"install it (e.g. `brew install postgresql@16`) to {action} a postgres database"
        )
    result = subprocess.run(
        [str(binary), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise BackupError(f"{tool} failed (exit {result.returncode}): {detail}")


def _pg_dump_to(database_url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run_pg_tool(
        "pg_dump",
        [
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            f"--dbname={libpq_url(database_url)}",
            f"--file={dest}",
        ],
        action="dump",
        env=libpq_env(database_url),
    )


def _pg_restore_from(database_url: str, dump_path: Path) -> None:
    _run_pg_tool(
        "pg_restore",
        [
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            f"--dbname={libpq_url(database_url)}",
            str(dump_path),
        ],
        action="restore",
        env=libpq_env(database_url),
    )


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


def _sqlite_snapshot_to(source: Path, dest: Path) -> None:
    """Consistent point-in-time copy of a (possibly live) sqlite database.

    The weekly backup job runs in the same process as the 10-minute trigger
    sweep and the hourly energy persist, all writing to this file; a plain
    ``shutil.copy2`` can read pages mid-commit and drops the -journal/-wal
    sidecar, yielding a torn copy that only fails at disaster-recovery time.
    ``sqlite3.Connection.backup`` takes the database lock and produces a
    transactionally consistent single-file snapshot (same pattern as
    vendor/hermes-agent/hermes_cli/backup.py::_safe_copy_db).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst_conn = sqlite3.connect(str(dest))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    except sqlite3.Error as exc:
        raise BackupError(f"sqlite snapshot of {source} failed: {exc}") from exc
    finally:
        src_conn.close()


def _stage_healthmes_db(database_url: str, stage: Path) -> dict[str, Any]:
    """Dump the healthmes database into the stage; returns the manifest entry."""
    backend = make_url(database_url).get_backend_name()
    if backend == "sqlite":
        source = _sqlite_file_path(database_url)
        if not source.is_file():
            raise BackupError(f"sqlite database file not found: {source}")
        _sqlite_snapshot_to(source, stage / HEALTHMES_SQLITE_ARCNAME)
        return {"kind": "sqlite_file", "arcname": HEALTHMES_SQLITE_ARCNAME}
    if backend == "postgresql":
        _pg_dump_to(database_url, stage / HEALTHMES_PG_DUMP_ARCNAME)
        return {"kind": "pg_dump", "arcname": HEALTHMES_PG_DUMP_ARCNAME}
    raise BackupError(f"unsupported database backend for backup: {backend}")


def _stage_ow_db(ow_database_url: str, stage: Path) -> dict[str, Any]:
    backend = make_url(ow_database_url).get_backend_name()
    if backend != "postgresql":
        raise BackupError(
            f"open-wearables database URL must be postgres (vendor stack), got: {backend}"
        )
    _pg_dump_to(ow_database_url, stage / OW_PG_DUMP_ARCNAME)
    return {"kind": "pg_dump", "arcname": OW_PG_DUMP_ARCNAME}


def _stage_tree(source: Path, stage: Path, arcroot: str) -> dict[str, Any]:
    """Copy ``source`` under ``stage/arcroot``; returns the manifest entry.

    Regular files, directories (including empty ones) and intra-tree
    symlinks are preserved. Symlinks whose resolved target escapes
    ``source`` are skipped and recorded (the archive must stay
    self-contained); other special files (sockets, fifos) are skipped too.
    """
    source = source.resolve()
    file_count = 0
    total_bytes = 0
    skipped: list[dict[str, str]] = []
    (stage / arcroot).mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source)
        arcname = f"{arcroot}/{rel.as_posix()}"
        target = stage / arcroot / rel
        if path.is_symlink():
            link_target = os.readlink(path)
            resolved = (
                Path(link_target)
                if os.path.isabs(link_target)
                else (path.parent / link_target)
            ).resolve()
            if resolved == source or resolved.is_relative_to(source):
                target.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(link_target, target)
            else:
                skipped.append(
                    {"path": arcname, "reason": "symlink-outside-tree", "target": link_target}
                )
            continue
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not path.is_file():
            skipped.append({"path": arcname, "reason": "special-file"})
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        file_count += 1
        total_bytes += target.stat().st_size
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


def _build_inventory(stage: Path) -> list[dict[str, Any]]:
    """Inventory every archived file/symlink (manifest.json itself excluded)."""
    entries: list[dict[str, Any]] = []
    for path in sorted(stage.rglob("*")):
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


def _tar_gz_bytes(stage: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in sorted(stage.rglob("*")):
            tar.add(path, arcname=path.relative_to(stage).as_posix(), recursive=False)
    return buffer.getvalue()


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
    with tempfile.TemporaryDirectory(prefix="healthmes-backup-") as tmp:
        stage = Path(tmp) / "stage"
        stage.mkdir()

        contents: dict[str, Any] = {
            "healthmes_db": _stage_healthmes_db(locations.database_url, stage),
            "open_wearables_db": None,
            "media": None,
            "hermes_home": None,
        }
        if locations.ow_database_url:
            contents["open_wearables_db"] = _stage_ow_db(locations.ow_database_url, stage)
        if locations.media_dir is not None and locations.media_dir.is_dir():
            contents["media"] = _stage_tree(locations.media_dir, stage, MEDIA_ARCROOT)
        if locations.hermes_home is not None and locations.hermes_home.is_dir():
            contents["hermes_home"] = _stage_tree(locations.hermes_home, stage, HERMES_ARCROOT)

        manifest: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "created_at": created_at.isoformat(),
            "healthmes_version": __version__,
            "contents": contents,
            "inventory": _build_inventory(stage),
        }
        (stage / MANIFEST_ARCNAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        ciphertext = age_passphrase.encrypt(_tar_gz_bytes(stage), passphrase)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    partial = out_path.with_name(out_path.name + ".part")
    partial.write_bytes(ciphertext)
    partial.replace(out_path)
    logger.info("Snapshot written: %s (%d bytes encrypted)", out_path, len(ciphertext))
    return manifest


def _decrypt_snapshot(path: Path, passphrase: str) -> bytes:
    if not path.is_file():
        raise BackupError(f"snapshot not found: {path}")
    if not passphrase:
        raise BackupError(
            "no backup passphrase configured; set HEALTHMES_BACKUP_PASSPHRASE "
            "to decrypt snapshots"
        )
    try:
        return age_passphrase.decrypt(path.read_bytes(), passphrase)
    except pyrage.DecryptError as exc:
        raise WrongPassphraseError(
            f"could not decrypt {path.name}: wrong passphrase or corrupted snapshot"
        ) from exc


def _load_manifest(extracted: Path) -> dict[str, Any]:
    manifest_path = extracted / MANIFEST_ARCNAME
    if not manifest_path.is_file():
        raise SnapshotIntegrityError("snapshot archive has no manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SnapshotIntegrityError(f"snapshot manifest is not valid JSON: {exc}") from exc
    version = manifest.get("schema_version")
    if not isinstance(version, int) or version < 1:
        raise SnapshotIntegrityError(f"snapshot manifest has invalid schema_version: {version!r}")
    if version > SCHEMA_VERSION:
        raise BackupError(
            f"snapshot schema_version {version} is newer than this tool "
            f"(supports up to {SCHEMA_VERSION}); upgrade healthmes to restore it"
        )
    return manifest


def _verify_inventory(extracted: Path, manifest: dict[str, Any]) -> None:
    """Check the extracted tree against the manifest inventory, both ways."""
    inventory = manifest.get("inventory")
    if not isinstance(inventory, list):
        raise SnapshotIntegrityError("snapshot manifest has no inventory")
    declared: set[str] = set()
    for entry in inventory:
        rel = entry["path"]
        declared.add(rel)
        path = extracted / rel
        if entry["kind"] == "symlink":
            if not path.is_symlink() or os.readlink(path) != entry["target"]:
                raise SnapshotIntegrityError(f"inventory mismatch for symlink: {rel}")
            continue
        if not path.is_file() or path.is_symlink():
            raise SnapshotIntegrityError(f"file listed in inventory is missing: {rel}")
        if path.stat().st_size != entry["size_bytes"] or _sha256(path) != entry["sha256"]:
            raise SnapshotIntegrityError(f"checksum mismatch for: {rel}")
    for path in extracted.rglob("*"):
        rel = path.relative_to(extracted).as_posix()
        if rel == MANIFEST_ARCNAME or (path.is_dir() and not path.is_symlink()):
            continue
        if rel not in declared:
            raise SnapshotIntegrityError(f"archive contains undeclared entry: {rel}")


def read_manifest(path: Path, passphrase: str) -> dict[str, Any]:
    """Decrypt ``path`` and return its manifest without touching live data."""
    data = _decrypt_snapshot(path, passphrase)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        try:
            member = tar.getmember(MANIFEST_ARCNAME)
        except KeyError as exc:
            raise SnapshotIntegrityError("snapshot archive has no manifest.json") from exc
        extracted = tar.extractfile(member)
        if extracted is None:
            raise SnapshotIntegrityError("snapshot manifest is not a regular file")
        with tempfile.TemporaryDirectory(prefix="healthmes-manifest-") as tmp:
            manifest_path = Path(tmp) / MANIFEST_ARCNAME
            manifest_path.write_bytes(extracted.read())
            return _load_manifest(Path(tmp))


def _replace_tree(staged: Path, dest: Path) -> None:
    """Atomically-ish swap ``dest`` for the verified staged tree."""
    dest = Path(dest)
    if dest.is_dir() and not dest.is_symlink():
        shutil.rmtree(dest)
    elif dest.exists() or dest.is_symlink():
        dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staged), str(dest))


def restore_snapshot(
    path: Path,
    *,
    passphrase: str,
    locations: DataLocations,
) -> dict[str, Any]:
    """Restore the snapshot at ``path`` over the live ``locations``.

    Order of operations: decrypt → extract to scratch (``data`` filter) →
    verify against the manifest inventory → only then replace live targets.
    Decryption/integrity failures therefore leave the system untouched.

    Restore is destructive: the media tree and Hermes state directory are
    *replaced* (not merged), the sqlite file is overwritten, and postgres
    restores run ``pg_restore --clean --if-exists``. Sections absent from
    the snapshot, or without a configured live location, are skipped (with a
    warning when data would be left unrestored). Returns the manifest.
    """
    data = _decrypt_snapshot(path, passphrase)
    with tempfile.TemporaryDirectory(prefix="healthmes-restore-") as tmp:
        extracted = Path(tmp) / "extracted"
        extracted.mkdir()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(extracted, filter="data")
        manifest = _load_manifest(extracted)
        _verify_inventory(extracted, manifest)
        contents = manifest["contents"]

        # healthmes database
        db_entry = contents["healthmes_db"]
        target_backend = make_url(locations.database_url).get_backend_name()
        if db_entry["kind"] == "sqlite_file":
            if target_backend != "sqlite":
                raise BackupError(
                    "snapshot holds a sqlite database but the target "
                    f"database_url backend is {target_backend}; restore into a sqlite URL "
                    "or migrate manually"
                )
            dest = _sqlite_file_path(locations.database_url)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(extracted / db_entry["arcname"], dest)
        else:
            if target_backend != "postgresql":
                raise BackupError(
                    "snapshot holds a postgres dump but the target "
                    f"database_url backend is {target_backend}"
                )
            _pg_restore_from(locations.database_url, extracted / db_entry["arcname"])

        # open-wearables database (optional section)
        ow_entry = contents.get("open_wearables_db")
        if ow_entry is not None:
            if locations.ow_database_url:
                _pg_restore_from(locations.ow_database_url, extracted / ow_entry["arcname"])
            else:
                logger.warning(
                    "Snapshot contains an open-wearables dump but no "
                    "open-wearables database URL is configured; skipping it."
                )

        # media tree (optional section)
        media_entry = contents.get("media")
        if media_entry is not None:
            if locations.media_dir is not None:
                _replace_tree(extracted / media_entry["arcroot"], locations.media_dir)
            else:
                logger.warning("Snapshot contains media but no media_dir target; skipping it.")

        # Hermes state (optional section)
        hermes_entry = contents.get("hermes_home")
        if hermes_entry is not None:
            if locations.hermes_home is not None:
                _replace_tree(extracted / hermes_entry["arcroot"], locations.hermes_home)
            else:
                logger.warning(
                    "Snapshot contains Hermes state but HERMES_HOME is not "
                    "configured; skipping it."
                )
    logger.info("Snapshot restored from %s", path)
    return manifest
