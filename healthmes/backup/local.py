"""LocalDirectoryProvider — MVP BackupProvider writing to a local directory.

Snapshots land in ``resolve_backup_dir(settings)`` (default:
``{HEALTHMES_DATA_DIR}/backups``) as ``healthmes-backup-<UTC stamp>.tar.gz.age``
files. Listing never needs the passphrase: metadata comes from the file name
and size, keeping ``healthmes backup list`` usable on a machine that only
holds the ciphertext.

``build_backup_job`` is the zero-arg callable handed to
``healthmes.engine.scheduler.register_backup_job`` (the weekly Sunday-03:30
slot); it never raises so a misconfigured backup can never take the
scheduler thread down.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from healthmes.backup.provider import BackupError, SnapshotInfo
from healthmes.backup.snapshot import (
    PROVIDER_REMOTE_VAULT,
    SNAPSHOT_SUFFIX,
    DataLocations,
    create_snapshot,
    parse_snapshot_name,
    resolve_backup_dir,
    resolve_backup_provider_name,
    resolve_data_locations,
    resolve_passphrase,
    restore_snapshot,
    snapshot_name,
)
from healthmes.config import Settings

__all__ = ["LocalDirectoryProvider", "build_backup_job"]

logger = logging.getLogger(__name__)


class LocalDirectoryProvider:
    """Store age-encrypted snapshot envelopes in a local directory.

    Implements the :class:`healthmes.backup.provider.BackupProvider`
    protocol. ``clock`` is injectable so tests (and callers that must align
    names with external timestamps) control the creation instant; snapshots
    themselves only ever receive caller-injected timestamps.
    """

    def __init__(
        self,
        backup_dir: Path,
        *,
        locations: DataLocations,
        passphrase: str | None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._backup_dir = Path(backup_dir)
        self._locations = locations
        self._passphrase = passphrase
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)

    @classmethod
    def from_settings(
        cls, settings: Settings, *, passphrase: str | None = None
    ) -> "LocalDirectoryProvider":
        """Build the provider from Settings (target dir, locations, passphrase).

        ``passphrase`` overrides the Settings/env resolution — the CLI uses
        it for ``--passphrase-file``.
        """
        return cls(
            resolve_backup_dir(settings),
            locations=resolve_data_locations(settings),
            passphrase=passphrase if passphrase is not None else resolve_passphrase(settings),
        )

    @property
    def backup_dir(self) -> Path:
        return self._backup_dir

    def _require_passphrase(self) -> str:
        if not self._passphrase:
            raise BackupError(
                "no backup passphrase configured; set HEALTHMES_BACKUP_PASSPHRASE "
                "(losing it makes every snapshot unrecoverable)"
            )
        return self._passphrase

    def _unique_out_path(self, created_at: datetime) -> Path:
        """Snapshot path for ``created_at``, deduplicated on same-second runs."""
        base = snapshot_name(created_at)
        candidate = self._backup_dir / base
        counter = 2
        while candidate.exists():
            stem = base[: -len(SNAPSHOT_SUFFIX)]
            candidate = self._backup_dir / f"{stem}-{counter}{SNAPSHOT_SUFFIX}"
            counter += 1
        return candidate

    def export_snapshot(self) -> SnapshotInfo:
        """Create one encrypted snapshot of the live data in the backup dir."""
        passphrase = self._require_passphrase()
        created_at = self._clock()
        out_path = self._unique_out_path(created_at)
        create_snapshot(
            self._locations,
            passphrase=passphrase,
            out_path=out_path,
            created_at=created_at,
        )
        return SnapshotInfo(
            name=out_path.name,
            path=out_path,
            created_at=created_at,
            size_bytes=out_path.stat().st_size,
        )

    def restore(self, path: Path | str) -> None:
        """Restore a snapshot by absolute path or bare name in the backup dir."""
        restore_snapshot(
            self.resolve_snapshot_path(path),
            passphrase=self._require_passphrase(),
            locations=self._locations,
        )

    def resolve_snapshot_path(self, path: Path | str) -> Path:
        """Accept an absolute/relative path or a bare snapshot name in backup_dir."""
        candidate = Path(path).expanduser()
        if candidate.is_file():
            return candidate
        if candidate.parent == Path("."):  # bare name → look in the backup dir
            named = self._backup_dir / candidate.name
            if named.is_file():
                return named
        raise BackupError(
            f"snapshot not found: {path} (looked in {self._backup_dir}; "
            "run `healthmes backup list`)"
        )

    def list_snapshots(self) -> list[SnapshotInfo]:
        """All snapshots in the backup dir, newest first; no passphrase needed."""
        if not self._backup_dir.is_dir():
            return []
        snapshots: list[SnapshotInfo] = []
        for entry in self._backup_dir.iterdir():
            if not entry.is_file():
                continue
            created_at = parse_snapshot_name(entry.name)
            if created_at is None:
                continue
            snapshots.append(
                SnapshotInfo(
                    name=entry.name,
                    path=entry,
                    created_at=created_at,
                    size_bytes=entry.stat().st_size,
                )
            )
        snapshots.sort(key=lambda info: (info.created_at, info.name), reverse=True)
        return snapshots


def build_backup_job(settings: Settings) -> Callable[[], None]:
    """Zero-arg weekly backup callable for ``register_backup_job``.

    Intended wiring (healthmes/app.py lifespan, before ``start_scheduler``)::

        scheduler = create_scheduler(settings)
        register_backup_job(scheduler, build_backup_job(settings))
        app.state.scheduler = start_scheduler(settings, scheduler=scheduler)

    Skips (with a warning) when no passphrase is configured and logs — never
    raises — on failure, so the APScheduler thread stays healthy.

    When the backup provider selector (``HEALTHMES_BACKUP_PROVIDER`` /
    ``Settings.backup_provider``) is ``remote_vault``, the local snapshot is
    additionally replicated to the configured S3-compatible vault. Local
    first: the local write happens (and is kept) regardless; a failed
    replication only logs. The remote_vault module is imported lazily so the
    default local path never pays the boto3 import.
    """

    def _replicate_to_vault(snapshot_path: Path) -> None:
        if resolve_backup_provider_name(settings) != PROVIDER_REMOTE_VAULT:
            return
        from healthmes.backup.remote_vault import RemoteVaultProvider

        remote_info = RemoteVaultProvider.from_settings(settings).push(snapshot_path)
        logger.info("Weekly backup replicated to vault: %s", remote_info.path)

    def run_weekly_backup() -> None:
        if resolve_passphrase(settings) is None:
            logger.warning(
                "Weekly backup skipped: no passphrase configured "
                "(set HEALTHMES_BACKUP_PASSPHRASE)."
            )
            return
        try:
            info = LocalDirectoryProvider.from_settings(settings).export_snapshot()
        except Exception:
            logger.exception("Weekly backup failed.")
            return
        logger.info("Weekly backup written: %s (%d bytes)", info.path, info.size_bytes)
        try:
            _replicate_to_vault(info.path)
        except Exception:
            # The local snapshot exists and stays valid; only replication failed.
            logger.exception("Weekly backup vault replication failed (local copy kept).")

    return run_weekly_backup
