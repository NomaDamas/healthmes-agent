"""BackupProvider protocol — the local-first encrypted backup seam.

This module is the *business seam* from docs/PLAN.md section 9: every byte of
HealthMes data that leaves the live stores (databases, media tree, Hermes
state) must travel through a :class:`BackupProvider`. The MVP ships
``LocalDirectoryProvider`` (healthmes/backup/local.py); the future paid
``RemoteVaultProvider`` (S3-compatible vault, client-side encryption — see
docs/BACKUP.md) implements this exact protocol and nothing else. Exporting
data around the protocol is forbidden.

Only contract types live here so that alternative providers depend on a tiny,
stable surface: the protocol itself, the snapshot descriptor, and the error
hierarchy shared by all implementations.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = [
    "BackupError",
    "BackupProvider",
    "SnapshotInfo",
    "SnapshotIntegrityError",
    "WrongPassphraseError",
]


class BackupError(RuntimeError):
    """Base error for every backup/restore failure.

    Raised with an actionable, single-line message; CLI and scheduler
    surfaces print it without a traceback.
    """


class WrongPassphraseError(BackupError):
    """A snapshot could not be decrypted (wrong passphrase or corrupt file).

    age's scrypt envelope cannot distinguish a bad passphrase from a
    truncated/corrupted ciphertext, so both surface here — either way the
    snapshot yielded no plaintext and nothing was restored.
    """


class SnapshotIntegrityError(BackupError):
    """Decryption succeeded but the archive contradicts its manifest.

    Examples: a file listed in the manifest inventory is missing, a SHA-256
    digest does not match, or the archive contains files the inventory never
    declared. Restore aborts before touching any live target.
    """


@dataclass(frozen=True, slots=True)
class SnapshotInfo:
    """Descriptor of one immutable snapshot held by a provider.

    ``path`` is a provider-scoped locator: a real filesystem path for
    ``LocalDirectoryProvider``, an object key rendered as a path for remote
    vaults. ``created_at`` is the timezone-aware creation instant baked into
    both the snapshot name and its manifest.
    """

    name: str
    path: Path
    created_at: datetime
    size_bytes: int


@runtime_checkable
class BackupProvider(Protocol):
    """Storage backend for encrypted snapshot envelopes (PLAN section 9).

    Implementations store *only* the age-encrypted envelope produced by
    healthmes/backup/snapshot.py — plaintext never reaches the storage
    layer, which is what makes a remote (untrusted) vault viable.
    """

    def export_snapshot(self) -> SnapshotInfo:
        """Create one new encrypted snapshot of the live data and store it.

        Returns the descriptor of the stored snapshot. Raises
        :class:`BackupError` when a required input (passphrase, database,
        dump tooling) is missing or the storage write fails.
        """
        ...

    def restore(self, path: Path | str) -> None:
        """Restore the snapshot at ``path`` over the live data locations.

        ``path`` is a locator previously returned in ``SnapshotInfo.path``
        (implementations may also accept the bare snapshot ``name``).
        Restore is destructive by design: live targets are replaced with the
        snapshot contents. Raises :class:`WrongPassphraseError` when the
        envelope cannot be decrypted and :class:`SnapshotIntegrityError`
        when the archive fails verification — in both cases no live target
        has been modified yet.
        """
        ...

    def list_snapshots(self) -> list[SnapshotInfo]:
        """Return all snapshots held by this provider, newest first.

        Listing must not require the passphrase: implementations derive
        metadata from snapshot names/sizes, never from envelope contents.
        """
        ...
