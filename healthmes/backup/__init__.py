"""Local-first encrypted backup seam (BackupProvider protocol).

Snapshot envelope (manifest + pg dumps + media + hermes state) encrypted
with age (pyrage). LocalDirectoryProvider is the default;
RemoteVaultProvider (healthmes/backup/remote_vault.py) replicates the same
encrypted envelopes to any S3-compatible vault. No data leaves the machine
except through this interface. See docs/PLAN.md section 9 and
docs/BACKUP.md for the envelope format and the remote-vault contract.

Vault symbols are deliberately *not* re-exported here: import them from
``healthmes.backup.remote_vault`` (as the CLI and the weekly job do), which
keeps boto3 off the import path of ``import healthmes.backup`` — the
service/CLI startup path must stay cheap.
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
    PROVIDER_LOCAL,
    PROVIDER_REMOTE_VAULT,
    SCHEMA_VERSION,
    SNAPSHOT_PREFIX,
    SNAPSHOT_SUFFIX,
    DataLocations,
    create_snapshot,
    read_manifest,
    resolve_backup_dir,
    resolve_backup_provider_name,
    resolve_data_locations,
    resolve_passphrase,
    restore_snapshot,
)

__all__ = [
    "PROVIDER_LOCAL",
    "PROVIDER_REMOTE_VAULT",
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
    "resolve_backup_provider_name",
    "resolve_data_locations",
    "resolve_passphrase",
    "restore_snapshot",
]
