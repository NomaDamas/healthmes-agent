"""Local-first encrypted backup seam (BackupProvider protocol).

Snapshot envelope (manifest + pg dumps + media + hermes state) encrypted
with age (pyrage). MVP ships LocalDirectoryProvider; a future paid
RemoteVaultProvider implements the same protocol. No data leaves the
machine except through this interface. See docs/PLAN.md section 9 and
docs/BACKUP.md for the envelope format and the remote-vault contract.
"""

from healthmes.backup.local import LocalDirectoryProvider, build_backup_job
from healthmes.backup.provider import (
    BackupError,
    BackupProvider,
    SnapshotInfo,
    SnapshotIntegrityError,
    WrongPassphraseError,
)
from healthmes.backup.snapshot import (
    SCHEMA_VERSION,
    SNAPSHOT_PREFIX,
    SNAPSHOT_SUFFIX,
    DataLocations,
    create_snapshot,
    read_manifest,
    resolve_backup_dir,
    resolve_data_locations,
    resolve_passphrase,
    restore_snapshot,
)

__all__ = [
    "SCHEMA_VERSION",
    "SNAPSHOT_PREFIX",
    "SNAPSHOT_SUFFIX",
    "BackupError",
    "BackupProvider",
    "DataLocations",
    "LocalDirectoryProvider",
    "SnapshotInfo",
    "SnapshotIntegrityError",
    "WrongPassphraseError",
    "build_backup_job",
    "create_snapshot",
    "read_manifest",
    "resolve_backup_dir",
    "resolve_data_locations",
    "resolve_passphrase",
    "restore_snapshot",
]
