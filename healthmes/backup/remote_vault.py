"""RemoteVaultProvider — S3-compatible replication target for snapshot envelopes.

This is the implemented half of the docs/PLAN.md section 9 business seam: the
same :class:`healthmes.backup.provider.BackupProvider` protocol the local MVP
ships, pointed at any S3-compatible object store (AWS S3, Cloudflare R2,
MinIO, ...). Snapshots are age-encrypted *before they exist* (see
healthmes/backup/snapshot.py), so the vault only ever stores ciphertext —
the server can never see plaintext, and no key material ever leaves the
client.

Two rules keep the seam honest (PLAN section 9, verbatim: "이 인터페이스를
우회한 데이터 반출 금지" — no data export bypassing this interface):

1. **Envelope-only uploads.** :meth:`RemoteVaultProvider.push` refuses any
   file that is not a snapshot envelope: the name must be the canonical
   ``healthmes-backup-<UTC stamp>.tar.gz.age`` form *and* the content must
   start with the age v1 header. Renaming a raw database file to
   ``*.tar.gz.age`` is refused — this provider cannot be used as a generic
   uploader for health data.
2. **Local-first.** The vault is a replication target: ``export_snapshot``
   writes the local snapshot first and uploads a byte-identical copy; the
   local file is only removed when the caller explicitly opts into
   ``keep_local=False`` (CLI ``--remote-only``).

Configuration resolves attribute-first from Settings (typed fields the
integrator may add later) with documented ``HEALTHMES_VAULT_*`` environment
fallbacks, mirroring the resolution style of healthmes/backup/snapshot.py —
the module is fully usable from environment variables alone.
"""

import hashlib
import logging
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
)

from healthmes.backup.local import LocalDirectoryProvider
from healthmes.backup.provider import BackupError, SnapshotInfo
from healthmes.backup.snapshot import (
    PROVIDER_LOCAL,
    PROVIDER_REMOTE_VAULT,
    SNAPSHOT_PREFIX,
    SNAPSHOT_SUFFIX,
    parse_snapshot_name,
    resolve_backup_provider_name,
)
from healthmes.config import Settings

__all__ = [
    "DEFAULT_VAULT_PREFIX",
    "PROVIDER_LOCAL",
    "PROVIDER_REMOTE_VAULT",
    "MergedSnapshot",
    "RemoteVaultProvider",
    "VaultConfig",
    "merge_snapshot_listings",
    "resolve_backup_provider_name",
    "resolve_vault_config",
]

logger = logging.getLogger(__name__)

# The exact first line of every age v1 envelope (pyrage output included). The
# guard does not merely prefix-match this — a plaintext file can start with it
# too — it parses the surrounding header structure (see
# ``_is_age_envelope_header``); anything else must never be uploaded.
AGE_MAGIC = b"age-encryption.org/v1"

# How much of a candidate file to read for the structural header check. An age
# header (version line + recipient stanza(s) + ``--- <MAC>`` terminator) is a
# few hundred bytes for our single-scrypt-recipient snapshots and stays well
# under a kilobyte even for pathological multi-recipient headers; 16 KiB is a
# comfortable ceiling that never touches the ciphertext body.
_AGE_HEADER_READ = 16 * 1024

DEFAULT_VAULT_PREFIX = "healthmes-vault"

_ENV_ENDPOINT = "HEALTHMES_VAULT_ENDPOINT"
_ENV_BUCKET = "HEALTHMES_VAULT_BUCKET"
_ENV_ACCESS_KEY_ID = "HEALTHMES_VAULT_ACCESS_KEY_ID"
_ENV_SECRET_ACCESS_KEY = "HEALTHMES_VAULT_SECRET_ACCESS_KEY"
_ENV_REGION = "HEALTHMES_VAULT_REGION"
_ENV_PREFIX = "HEALTHMES_VAULT_PREFIX"

_MD5_HEX = re.compile(r"^[0-9a-f]{32}$")

_CREDENTIAL_ERROR_CODES = {
    "AccessDenied",
    "AccessDeniedException",
    "AuthorizationHeaderMalformed",
    "InvalidAccessKeyId",
    "InvalidClientTokenId",
    "SignatureDoesNotMatch",
    "UnrecognizedClientException",
    "403",
}

_CHUNK = 1024 * 1024


# ---------------------------------------------------------------------------
# Settings resolution (attribute-first, HEALTHMES_VAULT_* env fallback)
# ---------------------------------------------------------------------------


def _setting(settings: Settings, attr: str, env: str) -> str | None:
    """Resolve one config value: Settings attribute first, then env var.

    Accepts plain strings and pydantic ``SecretStr`` values; blank strings
    count as unset (docker compose forwards optional vars as empty strings).
    """
    value: Any = getattr(settings, attr, None)
    if value is not None and hasattr(value, "get_secret_value"):
        value = value.get_secret_value()
    if value is not None:
        text = str(value).strip()
        if text:
            return text
    text = os.environ.get(env, "").strip()
    return text or None


def _normalize_prefix(prefix: str) -> str:
    """Collapse a user-supplied key prefix to ``a/b`` form ('' = bucket root)."""
    return "/".join(part for part in prefix.strip().split("/") if part)


@dataclass(frozen=True, slots=True)
class VaultConfig:
    """Connection settings for one S3-compatible vault.

    ``endpoint_url`` is None for AWS S3 proper; R2/MinIO deployments set it.
    When ``access_key_id``/``secret_access_key`` are unset the boto3 default
    credential chain applies (env vars, shared config, instance roles).
    """

    bucket: str
    endpoint_url: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = field(default=None, repr=False)
    region: str | None = None
    prefix: str = DEFAULT_VAULT_PREFIX


def resolve_vault_config(settings: Settings) -> VaultConfig | None:
    """Vault configuration from Settings attributes / ``HEALTHMES_VAULT_*`` env.

    Returns None when no bucket is configured (the vault feature is off).
    """
    bucket = _setting(settings, "vault_bucket", _ENV_BUCKET)
    if bucket is None:
        return None
    prefix = _setting(settings, "vault_prefix", _ENV_PREFIX)
    return VaultConfig(
        bucket=bucket,
        endpoint_url=_setting(settings, "vault_endpoint", _ENV_ENDPOINT),
        access_key_id=_setting(settings, "vault_access_key_id", _ENV_ACCESS_KEY_ID),
        secret_access_key=_setting(settings, "vault_secret_access_key", _ENV_SECRET_ACCESS_KEY),
        region=_setting(settings, "vault_region", _ENV_REGION),
        prefix=_normalize_prefix(prefix) if prefix is not None else DEFAULT_VAULT_PREFIX,
    )


# ---------------------------------------------------------------------------
# Merged (local + remote) listing for the CLI
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MergedSnapshot:
    """One snapshot name as seen across the local dir and the remote vault."""

    name: str
    local: SnapshotInfo | None
    remote: SnapshotInfo | None

    @property
    def origin(self) -> str:
        if self.local is not None and self.remote is not None:
            return "both"
        return "local" if self.local is not None else "remote"

    @property
    def info(self) -> SnapshotInfo:
        """Preferred descriptor: the local copy wins (local-first)."""
        chosen = self.local if self.local is not None else self.remote
        assert chosen is not None  # by construction one side exists
        return chosen

    @property
    def size_mismatch(self) -> bool:
        """True when both copies exist but differ in size (should never happen:
        snapshots are immutable and uploads are byte-identical)."""
        return (
            self.local is not None
            and self.remote is not None
            and self.local.size_bytes != self.remote.size_bytes
        )


def merge_snapshot_listings(
    local: list[SnapshotInfo], remote: list[SnapshotInfo]
) -> list[MergedSnapshot]:
    """Join two listings by snapshot name, newest first (like the providers)."""
    local_by_name = {info.name: info for info in local}
    remote_by_name = {info.name: info for info in remote}
    merged = [
        MergedSnapshot(name, local_by_name.get(name), remote_by_name.get(name))
        for name in local_by_name.keys() | remote_by_name.keys()
    ]
    merged.sort(key=lambda entry: (entry.info.created_at, entry.name), reverse=True)
    return merged


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


def _file_digests(path: Path) -> tuple[str, str, int]:
    """One read pass over ``path``: (md5 hex, sha256 hex, size in bytes).

    MD5 is used strictly for the S3 single-part ETag comparison (transport
    integrity, not security — hence ``usedforsecurity=False``, which also
    keeps FIPS-enabled builds working); SHA-256 is the durable digest stored
    in object metadata.
    """
    md5 = hashlib.md5(usedforsecurity=False)
    sha256 = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            md5.update(chunk)
            sha256.update(chunk)
            size += len(chunk)
    return md5.hexdigest(), sha256.hexdigest(), size


def _is_age_envelope_header(prefix: bytes | str) -> bool:
    """True only for a *structurally valid* age v1 header — not just the magic.

    A real age v1 envelope opens with::

        age-encryption.org/v1
        -> <recipient type> <args...>
        <base64 body line(s)>          # base64, so never begins with '-'
        --- <base64 MAC>

    Parsing that shape (rather than trusting the leading magic line) is what
    stops a plaintext file that merely *starts* with ``age-encryption.org/v1``:
    it has no ``-> `` recipient stanza and no ``--- <MAC>`` terminator, so it is
    rejected. Because base64 body lines never begin with ``-``, the ``-> ``
    stanza header and the ``--- `` MAC marker are unambiguous. ``prefix`` may be
    a truncated read of the file — the whole header fits comfortably within it
    (see ``_AGE_HEADER_READ``) — and the scan returns at the MAC line, so the
    binary ciphertext body is never interpreted.
    """
    raw = prefix.encode("utf-8", "replace") if isinstance(prefix, str) else prefix
    lines = raw.split(b"\n")
    # Version line must be exactly the magic *and* be a complete line (a
    # following newline, hence a second split element) — so a bare truncated
    # "age-encryption.org/v1" with nothing after it does not pass.
    if len(lines) < 2 or lines[0] != AGE_MAGIC:
        return False

    saw_recipient = False
    index = 1
    while index < len(lines):
        line = lines[index]
        if line.startswith(b"-> "):
            if not line[3:].strip():  # stanza header needs a recipient type
                return False
            saw_recipient = True
            index += 1
            # Consume this stanza's wrapped base64 body lines (never start '-').
            while index < len(lines) and not lines[index].startswith(b"-"):
                index += 1
            continue
        if line.startswith(b"--- "):
            # The MAC terminator closes the header: valid only after at least
            # one recipient stanza, and the MAC itself must be present.
            return saw_recipient and bool(line[4:].strip())
        # Anything else at stanza position (body lines were consumed above) is
        # malformed — e.g. plaintext following a forged magic line.
        return False
    # Ran out of input before the MAC terminator: not a well-formed header.
    return False


class RemoteVaultProvider:
    """BackupProvider replicating encrypted snapshot envelopes to an S3 vault.

    Wraps a :class:`LocalDirectoryProvider` (local-first: creation and
    restore always go through the local pipeline) and adds the vault side:
    ``push`` (upload one existing envelope), ``download``, remote
    ``list_snapshots`` and the protocol methods on top of them.
    """

    def __init__(
        self,
        config: VaultConfig,
        *,
        local: LocalDirectoryProvider,
        keep_local: bool = True,
    ) -> None:
        self._config = config
        self._local = local
        self._keep_local = keep_local
        self._s3: Any = None  # lazily built boto3 client

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        passphrase: str | None = None,
        keep_local: bool = True,
    ) -> "RemoteVaultProvider":
        """Build the provider from Settings; errors when no vault is configured."""
        config = resolve_vault_config(settings)
        if config is None:
            raise BackupError(
                "remote vault is not configured; set HEALTHMES_VAULT_BUCKET "
                "(plus HEALTHMES_VAULT_ENDPOINT / _ACCESS_KEY_ID / "
                "_SECRET_ACCESS_KEY / _REGION as needed — see docs/BACKUP.md)"
            )
        return cls(
            config,
            local=LocalDirectoryProvider.from_settings(settings, passphrase=passphrase),
            keep_local=keep_local,
        )

    # -- plumbing ---------------------------------------------------------

    @property
    def config(self) -> VaultConfig:
        return self._config

    @property
    def local(self) -> LocalDirectoryProvider:
        return self._local

    @property
    def vault_uri(self) -> str:
        """Display form of the vault location (``s3://bucket[/prefix]``)."""
        if self._config.prefix:
            return f"s3://{self._config.bucket}/{self._config.prefix}"
        return f"s3://{self._config.bucket}"

    def object_uri(self, name: str) -> str:
        return f"s3://{self._config.bucket}/{self._key_for(name)}"

    def _key_for(self, name: str) -> str:
        if self._config.prefix:
            return f"{self._config.prefix}/{name}"
        return name

    def _client(self) -> Any:
        """The boto3 S3 client, built lazily so construction never touches boto.

        ``request_checksum_calculation``/``response_checksum_validation`` are
        pinned to ``when_required`` because several S3-compatible gateways
        reject the flexible-checksum headers newer botocore sends by default;
        integrity is enforced by this module instead (MD5/ETag comparison and
        a SHA-256 stored in object metadata) plus age's authenticated
        encryption and the manifest verification on restore.
        """
        if self._s3 is None:
            kwargs: dict[str, Any] = {
                "config": BotoConfig(
                    retries={"max_attempts": 4, "mode": "standard"},
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                ),
            }
            if self._config.endpoint_url:
                kwargs["endpoint_url"] = self._config.endpoint_url
            if self._config.region:
                kwargs["region_name"] = self._config.region
            if self._config.access_key_id and self._config.secret_access_key:
                kwargs["aws_access_key_id"] = self._config.access_key_id
                kwargs["aws_secret_access_key"] = self._config.secret_access_key
            self._s3 = boto3.client("s3", **kwargs)
        return self._s3

    def _translate_client_error(self, exc: ClientError, action: str) -> BackupError:
        error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
        code = str(error.get("Code", "")) or "unknown"
        message = str(error.get("Message", "")).strip()
        if code in _CREDENTIAL_ERROR_CODES:
            return BackupError(
                f"vault rejected the credentials while trying to {action} ({code}): "
                f"check {_ENV_ACCESS_KEY_ID} / {_ENV_SECRET_ACCESS_KEY} "
                "(and that the key may access the bucket)"
            )
        if code == "NoSuchBucket":
            return BackupError(
                f"vault bucket not found: {self._config.bucket!r} — "
                f"check {_ENV_BUCKET} / {_ENV_ENDPOINT}"
            )
        if code in {"NoSuchKey", "404"}:
            return BackupError(
                f"snapshot not found in vault while trying to {action} "
                f"({self.vault_uri}); run `healthmes backup list --provider remote`"
            )
        detail = f"{code}: {message}" if message else code
        return BackupError(f"vault {action} failed: {detail}")

    @contextmanager
    def _vault_call(self, action: str) -> Iterator[None]:
        """Translate every boto failure into a clean, single-line BackupError."""
        try:
            yield
        except ClientError as exc:
            raise self._translate_client_error(exc, action) from exc
        except NoCredentialsError as exc:
            raise BackupError(
                f"no vault credentials available to {action}: set "
                f"{_ENV_ACCESS_KEY_ID} / {_ENV_SECRET_ACCESS_KEY} "
                "(or configure a boto3 credential source)"
            ) from exc
        except EndpointConnectionError as exc:
            raise BackupError(
                f"cannot reach the vault endpoint to {action}: {exc} — "
                f"check {_ENV_ENDPOINT}"
            ) from exc
        except BotoCoreError as exc:
            raise BackupError(f"vault {action} failed: {exc}") from exc

    # -- the seam guard ----------------------------------------------------

    def _require_snapshot_envelope(self, path: Path) -> None:
        """Refuse anything that is not an age-encrypted snapshot envelope.

        Defense against accidental raw-data exfiltration (PLAN section 9: no
        data export bypassing the backup seam): both the canonical snapshot
        name *and* a structurally valid age v1 header are required, so neither a
        stray file nor a renamed plaintext database — even one forged to start
        with the literal ``age-encryption.org/v1`` magic line — can ever reach
        the vault.
        """
        if parse_snapshot_name(path.name) is None:
            raise BackupError(
                f"refusing to upload {path.name!r}: the remote vault stores only "
                f"snapshot envelopes named {SNAPSHOT_PREFIX}<UTC stamp>{SNAPSHOT_SUFFIX} "
                "(docs/PLAN.md section 9 forbids exporting data around the backup seam)"
            )
        with path.open("rb") as handle:
            header = handle.read(_AGE_HEADER_READ)
        if not _is_age_envelope_header(header):
            raise BackupError(
                f"refusing to upload {path.name!r}: not an age-encrypted envelope "
                "(missing or malformed age v1 header) — only ciphertext may leave "
                "this machine (docs/PLAN.md section 9)"
            )

    # -- vault operations ---------------------------------------------------

    def push(self, path: Path | str) -> SnapshotInfo:
        """Upload one existing local snapshot envelope to the vault.

        ``path`` is an absolute/relative file path or a bare snapshot name in
        the local backup dir. The upload is verified (single-part ETag ==
        local MD5 where the gateway provides it, size otherwise) and
        all-or-nothing: S3 PUT semantics never expose a partial object, and a
        failed verification deletes the object before raising.
        """
        local_path = self._local.resolve_snapshot_path(path)
        self._require_snapshot_envelope(local_path)
        md5_hex, sha256_hex, size = _file_digests(local_path)
        created_at = parse_snapshot_name(local_path.name)
        assert created_at is not None  # _require_snapshot_envelope guarantees it
        key = self._key_for(local_path.name)
        with self._vault_call("upload a snapshot"), local_path.open("rb") as body:
            response = self._client().put_object(
                Bucket=self._config.bucket,
                Key=key,
                Body=body,
                ContentType="application/octet-stream",
                Metadata={
                    "healthmes-sha256": sha256_hex,
                    "healthmes-created-at": created_at.isoformat(),
                },
            )
        self._verify_upload(response, key=key, md5_hex=md5_hex, size=size)
        logger.info(
            "Snapshot pushed to vault: %s (%d bytes)", self.object_uri(local_path.name), size
        )
        return SnapshotInfo(
            name=local_path.name,
            path=Path(key),
            created_at=created_at,
            size_bytes=size,
        )

    def _verify_upload(self, response: dict, *, key: str, md5_hex: str, size: int) -> None:
        etag = str(response.get("ETag", "")).strip('"')
        if _MD5_HEX.match(etag):
            if etag == md5_hex:
                return
        else:
            # Gateway did not return a plain-MD5 ETag; fall back to a size check.
            with self._vault_call("verify an upload"):
                head = self._client().head_object(Bucket=self._config.bucket, Key=key)
            if int(head.get("ContentLength", -1)) == size:
                return
        try:  # best effort: do not leave a corrupt object behind
            self._client().delete_object(Bucket=self._config.bucket, Key=key)
        except Exception:  # pragma: no cover - cleanup only
            logger.warning("Could not delete corrupt vault object %s", key, exc_info=True)
        raise BackupError(
            f"vault upload integrity check failed for {key} "
            "(stored object does not match the local envelope); upload was removed, retry"
        )

    def download(self, name: str, *, overwrite: bool = False) -> Path:
        """Fetch one snapshot envelope from the vault into the local backup dir.

        Snapshots are immutable, so an existing local file of the same name
        is trusted and returned as-is unless ``overwrite`` is set. The write
        is atomic (``.part`` + rename) and verified against the object's
        recorded SHA-256 metadata / MD5 ETag when available; decryption and
        manifest verification during restore re-check everything anyway.
        """
        if parse_snapshot_name(name) is None:
            raise BackupError(
                f"not a snapshot name: {name!r} (expected "
                f"{SNAPSHOT_PREFIX}<UTC stamp>{SNAPSHOT_SUFFIX})"
            )
        dest = self._local.backup_dir / name
        if dest.is_file() and not overwrite:
            return dest
        key = self._key_for(name)
        with self._vault_call("download a snapshot"):
            response = self._client().get_object(Bucket=self._config.bucket, Key=key)
        expected_sha256 = response.get("Metadata", {}).get("healthmes-sha256")
        etag = str(response.get("ETag", "")).strip('"')
        md5 = hashlib.md5(usedforsecurity=False)
        sha256 = hashlib.sha256()
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_name(dest.name + ".part")
        try:
            with self._vault_call("download a snapshot"), partial.open("wb") as out:
                body = response["Body"]
                for chunk in iter(lambda: body.read(_CHUNK), b""):
                    md5.update(chunk)
                    sha256.update(chunk)
                    out.write(chunk)
            if expected_sha256 and sha256.hexdigest() != expected_sha256:
                raise BackupError(
                    f"vault download integrity check failed for {name} "
                    "(SHA-256 mismatch against the recorded upload digest)"
                )
            if _MD5_HEX.match(etag) and md5.hexdigest() != etag:
                raise BackupError(
                    f"vault download integrity check failed for {name} (ETag mismatch)"
                )
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        partial.replace(dest)
        logger.info("Snapshot downloaded from vault: %s -> %s", self.object_uri(name), dest)
        return dest

    def ensure_local_copy(self, path: Path | str) -> tuple[Path, bool]:
        """Resolve a snapshot locally, downloading from the vault when absent.

        Returns ``(local_path, downloaded)``. Local-first: an existing local
        file (by path or bare name) always wins; only then is the vault hit.
        """
        try:
            return self._local.resolve_snapshot_path(path), False
        except BackupError:
            name = Path(str(path)).name
            if parse_snapshot_name(name) is None:
                raise
            return self.download(name), True

    def create_and_replicate(self) -> tuple[SnapshotInfo, SnapshotInfo]:
        """Local write first, then upload; returns ``(local_info, remote_info)``.

        When the provider was built with ``keep_local=False`` the local copy
        is deleted *after* the verified upload — the vault then holds the
        only copy (explicit opt-in; local-first is the default).
        """
        local_info = self._local.export_snapshot()
        remote_info = self.push(local_info.path)
        if not self._keep_local:
            local_info.path.unlink()
            logger.info(
                "Local snapshot copy removed after replication (keep_local=False): %s",
                local_info.path,
            )
        return local_info, remote_info

    # -- BackupProvider protocol -------------------------------------------

    def export_snapshot(self) -> SnapshotInfo:
        """Create one snapshot (locally) and replicate it to the vault."""
        local_info, remote_info = self.create_and_replicate()
        return local_info if self._keep_local else remote_info

    def restore(self, path: Path | str) -> None:
        """Restore a snapshot, downloading it from the vault when not local.

        The actual restore is the exact local pipeline (decrypt → extract →
        verify inventory → replace live targets).
        """
        local_path, _ = self.ensure_local_copy(path)
        self._local.restore(local_path)

    def list_snapshots(self) -> list[SnapshotInfo]:
        """Snapshots held by the vault, newest first; needs no passphrase.

        ``SnapshotInfo.path`` is the object key rendered as a path. Keys not
        directly under the configured prefix, or whose basename is not a
        canonical snapshot name, are ignored (strays), matching the local
        provider's listing semantics.
        """
        key_prefix = f"{self._config.prefix}/" if self._config.prefix else ""
        snapshots: list[SnapshotInfo] = []
        with self._vault_call("list snapshots"):
            paginator = self._client().get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._config.bucket, Prefix=key_prefix):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    name = key[len(key_prefix):]
                    if "/" in name:
                        continue  # nested under a deeper prefix — not ours
                    created_at = parse_snapshot_name(name)
                    if created_at is None:
                        continue
                    snapshots.append(
                        SnapshotInfo(
                            name=name,
                            path=Path(key),
                            created_at=created_at,
                            size_bytes=int(obj["Size"]),
                        )
                    )
        snapshots.sort(key=lambda info: (info.created_at, info.name), reverse=True)
        return snapshots

    def list_merged(self) -> list[MergedSnapshot]:
        """Union of the local dir and the vault, labeled by origin."""
        return merge_snapshot_listings(self._local.list_snapshots(), self.list_snapshots())
