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
2. **Local-first creation, remote-authoritative restore.** The vault is a
   replication target: ``export_snapshot`` writes the local snapshot first and
   uploads a byte-identical copy. Even when the caller explicitly opts into
   ``keep_local=False`` (CLI ``--remote-only``), the local file is removed only
   after the vault returns and re-serves an immutable object generation
   identifier. Conversely, selecting the remote provider for restore always
   fetches and verifies the named vault object; a same-name local file is only
   a replaceable cache and can never silently become the restore source.

Configuration resolves attribute-first from Settings (typed fields the
integrator may add later) with documented ``HEALTHMES_VAULT_*`` environment
fallbacks, mirroring the resolution style of healthmes/backup/snapshot.py —
the module is fully usable from environment variables alone.
"""

import hashlib
import logging
import os
import re
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
)

from healthmes.backup.filesystem import (
    PinnedPublishedFile,
    RegularFileIdentity,
    durable_atomic_writer,
    open_regular_file,
)
from healthmes.backup.local import LocalDirectoryProvider
from healthmes.backup.provider import BackupError, SnapshotInfo
from healthmes.backup.snapshot import (
    PROVIDER_LOCAL,
    PROVIDER_REMOTE_VAULT,
    SNAPSHOT_PREFIX,
    SNAPSHOT_SUFFIX,
    RestoreResult,
    _read_manifest_from_handle,
    _require_disk_capacity,
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
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

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
_MAX_REMOTE_NAME_COLLISIONS = 100
_IMMUTABLE_CONFLICT_CODES = {
    "PreconditionFailed",
    "ConditionalRequestConflict",
    "412",
}
_SAFE_VAULT_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SAFE_VAULT_ERROR_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class _VaultObjectCollision(BackupError):
    """A different immutable vault object already owns this snapshot name."""


def _redacted_endpoint_label(endpoint_url: str | None) -> str:
    """Return endpoint transport identity without userinfo or URL payload."""
    if not endpoint_url:
        return "<configured endpoint>"
    try:
        parsed = urlsplit(endpoint_url)
        hostname = parsed.hostname
        if not parsed.scheme or not hostname:
            return "<configured endpoint>"
        host = f"[{hostname}]" if ":" in hostname else hostname
        try:
            port = parsed.port
        except ValueError:
            port = None
        suffix = f":{port}" if port is not None else ""
        return f"{parsed.scheme}://{host}{suffix}"
    except ValueError:
        return "<configured endpoint>"


def _safe_vault_error_code(value: object) -> str:
    """Return only an opaque provider code safe for user-facing output."""
    candidate = str(value).strip()
    if _SAFE_VAULT_ERROR_CODE.fullmatch(candidate) is None:
        return "unknown"
    return candidate


def _sanitized_vault_diagnostic(exc: BaseException) -> str:
    """Identify an exception without rendering its potentially secret text."""
    name = type(exc).__name__
    if _SAFE_VAULT_ERROR_TYPE.fullmatch(name) is None:
        return "Exception"
    return name


def _vault_operation_error(
    action: str,
    *,
    code: object | None = None,
) -> BackupError:
    """Build one single-line public error without provider-controlled text."""
    safe_action = " ".join(action.split())
    suffix = (
        f" ({_safe_vault_error_code(code)})"
        if code is not None
        else ""
    )
    return BackupError(
        f"vault {safe_action} failed{suffix}; check "
        f"{_ENV_ENDPOINT}, {_ENV_BUCKET}, and provider status"
    )


def _log_suppressed_vault_exception(
    action: str,
    exc: BaseException,
) -> None:
    """Log a bounded diagnostic while suppressing the provider's message."""
    logger.warning(
        "Vault operation failed: action=%s error_type=%s "
        "provider_detail=suppressed",
        " ".join(action.split()),
        _sanitized_vault_diagnostic(exc),
    )


@dataclass(frozen=True, slots=True)
class _VaultObjectReceipt:
    """Identity returned by an S3-compatible object operation.

    Only a non-null VersionId proves an immutable generation. ETag remains
    useful as a best-effort conditional read on non-versioned endpoints, but
    it is not treated as a generation identifier: gateways may use opaque,
    multipart, or encryption-dependent ETags.
    """

    version_id: str | None
    etag: str | None

    @property
    def proves_immutable_generation(self) -> bool:
        return self.version_id is not None


def _vault_response(
    value: object,
    *,
    action: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BackupError(
            f"vault returned an invalid response while attempting to {action}"
        )
    return value


def _vault_content_length(
    response: Mapping[str, Any],
    *,
    action: str,
    required: bool,
) -> int | None:
    value = response.get("ContentLength")
    if value is None and not required:
        return None
    if isinstance(value, bool):
        raise BackupError(
            f"vault returned an invalid ContentLength while attempting to {action}"
        )
    try:
        size = int(value)
    except (TypeError, ValueError) as exc:
        raise BackupError(
            f"vault returned an invalid ContentLength while attempting to {action}"
        ) from exc
    if size < 0:
        raise BackupError(
            f"vault returned an invalid ContentLength while attempting to {action}"
        )
    return size


def _vault_metadata(
    response: Mapping[str, Any],
    *,
    action: str,
) -> Mapping[str, Any]:
    value = response.get("Metadata", {})
    if not isinstance(value, Mapping):
        raise BackupError(
            f"vault returned invalid Metadata while attempting to {action}"
        )
    return value


def _vault_sha256_metadata(
    response: Mapping[str, Any],
    *,
    action: str,
) -> str:
    value = _vault_metadata(response, action=action).get(
        "healthmes-sha256"
    )
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not _SHA256_HEX.fullmatch(
        value.lower()
    ):
        raise BackupError(
            f"vault returned invalid SHA-256 Metadata while attempting to {action}"
        )
    return value.lower()


def _vault_object_receipt(
    response: Mapping[str, Any],
    *,
    action: str,
) -> _VaultObjectReceipt:
    version_value = response.get("VersionId")
    if version_value is None or version_value == "":
        version_id = None
    elif not isinstance(version_value, str):
        raise BackupError(
            f"vault returned an invalid VersionId while attempting to {action}"
        )
    else:
        normalized_version = version_value.strip()
        version_id = (
            None
            if not normalized_version or normalized_version.lower() == "null"
            else normalized_version
        )

    etag_value = response.get("ETag")
    if etag_value is None or etag_value == "":
        etag = None
    elif not isinstance(etag_value, str):
        raise BackupError(
            f"vault returned an invalid ETag while attempting to {action}"
        )
    else:
        etag = etag_value.strip() or None

    return _VaultObjectReceipt(version_id=version_id, etag=etag)


def _latest_vault_entries(
    pages: Iterator[Mapping[str, Any]],
    *,
    action: str,
) -> dict[str, tuple[str, Mapping[str, Any]]]:
    """Return each key's explicitly latest version or delete marker."""
    latest: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for page in pages:
        if not isinstance(page, Mapping):
            raise BackupError(
                f"vault returned an invalid version listing while attempting to {action}"
            )
        for listing_field, kind in (
            ("Versions", "version"),
            ("DeleteMarkers", "delete_marker"),
        ):
            entries = page.get(listing_field, [])
            if not isinstance(entries, list):
                raise BackupError(
                    "vault returned an invalid version listing while "
                    f"attempting to {action}"
                )
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                key = entry.get("Key")
                if not isinstance(key, str) or entry.get("IsLatest") is not True:
                    continue
                if key in latest:
                    raise BackupError(
                        "vault returned multiple latest generations for one "
                        f"object while attempting to {action}"
                    )
                latest[key] = (kind, entry)
    return latest


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


def _file_digests(handle) -> tuple[str, str, int]:
    """One read pass over an open file: (md5 hex, sha256 hex, size in bytes).

    MD5 is used strictly for the S3 single-part ETag comparison (transport
    integrity, not security — hence ``usedforsecurity=False``, which also
    keeps FIPS-enabled builds working); SHA-256 is the durable digest stored
    in object metadata.
    """
    md5 = hashlib.md5(usedforsecurity=False)
    sha256 = hashlib.sha256()
    size = 0
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

    Wraps a :class:`LocalDirectoryProvider` (creation starts locally and the
    validated restore pipeline remains local) and adds the vault side:
    ``push`` (upload one existing envelope), remote-authoritative ``download``,
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
        code = _safe_vault_error_code(error.get("Code", ""))
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
        if code in _IMMUTABLE_CONFLICT_CODES:
            return _VaultObjectCollision(
                f"vault refused to overwrite an existing immutable snapshot while "
                f"trying to {action} ({code})"
            )
        return _vault_operation_error(action, code=code)

    @contextmanager
    def _vault_call(self, action: str) -> Iterator[None]:
        """Translate every boto failure into a clean, single-line BackupError."""
        try:
            yield
        except BackupError:
            raise
        except ClientError as exc:
            raise self._translate_client_error(exc, action) from exc
        except NoCredentialsError as exc:
            raise BackupError(
                f"no vault credentials available to {action}: set "
                f"{_ENV_ACCESS_KEY_ID} / {_ENV_SECRET_ACCESS_KEY} "
                "(or configure a boto3 credential source)"
            ) from exc
        except EndpointConnectionError:
            raise BackupError(
                "cannot reach the vault endpoint to "
                f"{action} ({_redacted_endpoint_label(self._config.endpoint_url)}): "
                f"check {_ENV_ENDPOINT}"
            ) from None
        except BotoCoreError as exc:
            _log_suppressed_vault_exception(action, exc)
            raise _vault_operation_error(action) from exc
        except Exception as exc:
            _log_suppressed_vault_exception(action, exc)
            raise _vault_operation_error(action) from exc

    # -- the seam guard ----------------------------------------------------

    def _require_snapshot_envelope(self, path: Path, handle) -> None:
        """Refuse anything that is not an age-encrypted snapshot envelope.

        Defense against accidental raw-data exfiltration (PLAN section 9: no
        data export bypassing the backup seam): the canonical snapshot name,
        age v1 structure, configured-passphrase decryption, manifest schema,
        archive membership, and complete inventory must all validate against
        this same open file generation before any vault call.
        """
        if parse_snapshot_name(path.name) is None:
            raise BackupError(
                f"refusing to upload {path.name!r}: the remote vault stores only "
                f"snapshot envelopes named {SNAPSHOT_PREFIX}<UTC stamp>{SNAPSHOT_SUFFIX} "
                "(docs/PLAN.md section 9 forbids exporting data around the backup seam)"
            )
        handle.seek(0)
        header = handle.read(_AGE_HEADER_READ)
        if not _is_age_envelope_header(header):
            raise BackupError(
                f"refusing to upload {path.name!r}: not an age-encrypted envelope "
                "(missing or malformed age v1 header) — only ciphertext may leave "
                "this machine (docs/PLAN.md section 9)"
            )
        try:
            _read_manifest_from_handle(
                path,
                handle,
                self._local._require_passphrase(),
                limits=self._local.resource_limits,
            )
        except BackupError as exc:
            raise BackupError(
                f"refusing to upload {path.name!r}: encrypted payload is not a "
                f"valid HealthMes snapshot: {exc}"
            ) from exc
        finally:
            handle.seek(0)

    # -- vault operations ---------------------------------------------------

    def push(self, path: Path | str) -> SnapshotInfo:
        """Upload one existing local snapshot envelope to the vault.

        ``path`` is an absolute/relative file path or a bare snapshot name in
        the local backup dir. The stored bytes are read back and checked
        against the local SHA-256 digest. When the endpoint returns a
        VersionId, verification and any corrupt-upload cleanup are bound to
        that exact generation. A non-versioned endpoint remains usable as a
        replication target, but cannot qualify for remote-only local removal.
        """
        info, _, _ = self._push_with_receipt(path)
        return info

    def _push_with_receipt(
        self,
        path: Path | str,
        *,
        _sealed: Any | None = None,
        _sealed_identity: RegularFileIdentity | None = None,
        _local_identity: RegularFileIdentity | None = None,
    ) -> tuple[SnapshotInfo, RegularFileIdentity, _VaultObjectReceipt]:
        """Upload through one descriptor and return local and remote identity."""
        if _sealed is None:
            local_path, selected_identity = (
                self._local._resolve_snapshot_generation(path)
            )
        else:
            local_path = Path(path).expanduser()
            if local_path.parent == Path("."):
                local_path = self._local.backup_dir / local_path.name
            selected_identity = _sealed_identity
            if _local_identity is None:
                raise BackupError(
                    "sealed upload is missing its local snapshot identity"
                )
        try:
            source_scope = (
                nullcontext(_sealed)
                if _sealed is not None
                else open_regular_file(local_path)
            )
            with source_scope as source, tempfile.TemporaryFile(
                mode="w+b"
            ) as body:
                identity = RegularFileIdentity.from_descriptor(
                    source.fileno()
                )
                expected_identity = (
                    _sealed_identity
                    if _sealed is not None
                    else selected_identity
                )
                if expected_identity is None or identity != expected_identity:
                    raise BackupError(
                        "snapshot generation changed after it was selected for upload: "
                        f"{local_path}"
                    )
                source.seek(0)
                max_bytes = self._local.resource_limits.max_encrypted_bytes
                if identity.size > max_bytes:
                    raise BackupError(
                        "snapshot exceeds the configured "
                        f"{max_bytes}-byte encrypted limit"
                    )
                _require_disk_capacity(
                    Path(tempfile.gettempdir()),
                    payload_bytes=identity.size,
                    limits=self._local.resource_limits,
                    label="private vault upload generation",
                )
                copied = 0
                for chunk in iter(lambda: source.read(_CHUNK), b""):
                    copied += len(chunk)
                    if copied > max_bytes:
                        raise BackupError(
                            "snapshot exceeds the configured "
                            f"{max_bytes}-byte encrypted limit"
                        )
                    body.write(chunk)
                body.flush()
                os.fsync(body.fileno())
                if (
                    copied != identity.size
                    or RegularFileIdentity.from_descriptor(source.fileno())
                    != identity
                ):
                    raise BackupError(
                        "local snapshot changed while it was copied for upload; "
                        "nothing was sent to the vault"
                    )
                body.seek(0)
                self._require_snapshot_envelope(local_path, body)
                body.seek(0)
                _, sha256_hex, size = _file_digests(body)
                created_at = parse_snapshot_name(local_path.name)
                assert created_at is not None
                key = self._key_for(local_path.name)
                body.seek(0)
                try:
                    with self._vault_call("upload a snapshot"):
                        response = self._client().put_object(
                            Bucket=self._config.bucket,
                            Key=key,
                            Body=body,
                            ContentType="application/octet-stream",
                            IfNoneMatch="*",
                            Metadata={
                                "healthmes-sha256": sha256_hex,
                                "healthmes-created-at": created_at.isoformat(),
                            },
                        )
                except _VaultObjectCollision as exc:
                    matches, remote_receipt = self._remote_object_matches(
                        key,
                        sha256_hex=sha256_hex,
                        size=size,
                    )
                    if matches:
                        logger.info(
                            "Snapshot already present in vault with identical content: %s",
                            self.object_uri(local_path.name),
                        )
                        remote_info = SnapshotInfo(
                            name=local_path.name,
                            path=Path(key),
                            created_at=created_at,
                            size_bytes=size,
                            version_id=remote_receipt.version_id,
                        )
                    else:
                        raise _VaultObjectCollision(
                            f"vault snapshot {self.object_uri(local_path.name)} already exists "
                            "with different encrypted content; refusing to overwrite it"
                        ) from exc
                else:
                    remote_receipt = self._verify_upload(
                        response,
                        key=key,
                        sha256_hex=sha256_hex,
                        size=size,
                    )
                    remote_info = SnapshotInfo(
                        name=local_path.name,
                        path=Path(key),
                        created_at=created_at,
                        size_bytes=size,
                        version_id=remote_receipt.version_id,
                    )
                source_changed = (
                    RegularFileIdentity.from_descriptor(source.fileno())
                    != identity
                )
                if _sealed is None:
                    try:
                        named_identity = RegularFileIdentity.from_metadata(
                            local_path.lstat()
                        )
                    except (OSError, ValueError):
                        named_identity = None
                    source_changed = (
                        source_changed or named_identity != identity
                    )
                if source_changed:
                    raise BackupError(
                        "local snapshot changed while it was being uploaded; "
                        "the local file was preserved"
                    )
                source.seek(0)
        except BackupError:
            raise
        except OSError as exc:
            raise BackupError(
                f"could not read local snapshot {local_path}: {exc}"
            ) from exc
        logger.info(
            "Snapshot pushed to vault: %s (%d bytes)", self.object_uri(local_path.name), size
        )
        return (
            remote_info,
            identity if _sealed is None else _local_identity,
            remote_receipt,
        )

    def _discover_snapshot_version(self, name: str) -> str | None:
        """Resolve only the currently visible generation for one object key."""
        client = self._client()
        paginator_factory = getattr(client, "get_paginator", None)
        head_object = getattr(client, "head_object", None)
        if not callable(paginator_factory) or not callable(head_object):
            return None
        key = self._key_for(name)
        try:
            paginator = paginator_factory("list_object_versions")
        except Exception:
            return None
        created_at = parse_snapshot_name(name)
        assert created_at is not None
        expected_created_at = created_at.isoformat()
        try:
            with self._vault_call("resolve a snapshot generation"):
                latest = _latest_vault_entries(
                    paginator.paginate(
                        Bucket=self._config.bucket,
                        Prefix=key,
                    ),
                    action="resolve a snapshot generation",
                )
                state = latest.get(key)
                if state is None:
                    return None
                kind, version = state
                if kind == "delete_marker":
                    raise BackupError(
                        "snapshot not found in vault while trying to resolve "
                        f"a snapshot generation ({self.vault_uri}); run "
                        "`healthmes backup list --provider remote`"
                    )
                raw_version = version.get("VersionId")
                version_id = (
                    raw_version.strip()
                    if isinstance(raw_version, str)
                    else ""
                )
                request: dict[str, Any] = {
                    "Bucket": self._config.bucket,
                    "Key": key,
                }
                if version_id and version_id.lower() != "null":
                    request["VersionId"] = version_id
                response = _vault_response(
                    head_object(**request),
                    action="resolve a snapshot generation",
                )
                metadata = _vault_metadata(
                    response,
                    action="resolve a snapshot generation",
                )
                if (
                    metadata.get("healthmes-created-at")
                    != expected_created_at
                    or not _vault_sha256_metadata(
                        response,
                        action="resolve a snapshot generation",
                    )
                ):
                    return None
                return version_id if "VersionId" in request else None
        except BackupError:
            raise
        except Exception as exc:
            _log_suppressed_vault_exception(
                "resolve snapshot generation",
                exc,
            )
            raise _vault_operation_error(
                "resolve snapshot generation"
            ) from exc
        return None

    def _remote_object_matches(
        self,
        key: str,
        *,
        sha256_hex: str,
        size: int,
        put_receipt: _VaultObjectReceipt | None = None,
    ) -> tuple[bool, _VaultObjectReceipt]:
        """Read one resolved generation back and cryptographically compare it."""
        request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": key,
        }
        if put_receipt is not None:
            if put_receipt.version_id is not None:
                request["VersionId"] = put_receipt.version_id
            elif put_receipt.etag is not None:
                # This narrows non-versioned races, but ETag is deliberately
                # not promoted to an immutable-generation guarantee.
                request["IfMatch"] = put_receipt.etag
        try:
            with self._vault_call("verify stored snapshot bytes"):
                response = _vault_response(
                    self._client().get_object(**request),
                    action="verify stored snapshot bytes",
                )
        except _VaultObjectCollision:
            if (
                put_receipt is not None
                and put_receipt.version_id is None
                and put_receipt.etag is not None
            ):
                return False, put_receipt
            raise

        body = response.get("Body")
        try:
            response_receipt = _vault_object_receipt(
                response,
                action="verify stored snapshot bytes",
            )
            if put_receipt is not None and put_receipt.version_id is not None:
                if response_receipt.version_id != put_receipt.version_id:
                    raise BackupError(
                        "vault did not return the exact uploaded object generation "
                        f"while verifying {key}; the local snapshot was preserved"
                    )
                verified_receipt = put_receipt
            elif put_receipt is not None:
                if (
                    put_receipt.etag is not None
                    and response_receipt.etag != put_receipt.etag
                ):
                    return False, put_receipt
                # A VersionId observed only after an unbound PUT cannot prove
                # that GET returned that PUT's generation.
                verified_receipt = put_receipt
            else:
                # Idempotent collision path: this GET itself resolves the
                # existing object. VersionId identifies the bytes streamed.
                verified_receipt = response_receipt

            if body is None or not callable(getattr(body, "read", None)):
                raise BackupError(
                    "vault returned an invalid Body while attempting to "
                    "verify stored snapshot bytes"
                )
            declared_size = _vault_content_length(
                response,
                action="verify stored snapshot bytes",
                required=True,
            )
            if declared_size != size:
                return False, verified_receipt
            digest = hashlib.sha256()
            downloaded = 0
            try:
                with self._vault_call("verify stored snapshot bytes"):
                    for chunk in iter(lambda: body.read(_CHUNK), b""):
                        if not isinstance(chunk, bytes):
                            raise BackupError(
                                "vault snapshot Body returned a non-bytes chunk"
                            )
                        downloaded += len(chunk)
                        if downloaded > size:
                            return False, verified_receipt
                        digest.update(chunk)
            except BackupError:
                raise
            except Exception as exc:
                _log_suppressed_vault_exception(
                    "verify stored snapshot bytes",
                    exc,
                )
                raise _vault_operation_error(
                    "verify stored snapshot bytes"
                ) from exc
            return (
                downloaded == size and digest.hexdigest() == sha256_hex,
                verified_receipt,
            )
        finally:
            if body is not None and hasattr(body, "close"):
                try:
                    body.close()
                except Exception as exc:  # pragma: no cover - cleanup only
                    _log_suppressed_vault_exception(
                        "close vault verification body",
                        exc,
                    )

    def _verify_upload(
        self,
        response: object,
        *,
        key: str,
        sha256_hex: str,
        size: int,
    ) -> _VaultObjectReceipt:
        put_response = _vault_response(
            response,
            action="verify an upload",
        )
        put_receipt = _vault_object_receipt(
            put_response,
            action="verify an upload",
        )
        matches, verified_receipt = self._remote_object_matches(
            key,
            sha256_hex=sha256_hex,
            size=size,
            put_receipt=put_receipt,
        )
        if matches:
            return verified_receipt

        cleanup_detail = (
            "no remote object was deleted because the endpoint did not return "
            "an immutable generation identifier"
        )
        if put_receipt.version_id is not None:
            try:
                self._client().delete_object(
                    Bucket=self._config.bucket,
                    Key=key,
                    VersionId=put_receipt.version_id,
                )
            except Exception as exc:  # pragma: no cover - cleanup only
                cleanup_detail = (
                    "cleanup of the exact uploaded generation failed; inspect "
                    "the vault before retrying"
                )
                _log_suppressed_vault_exception(
                    "delete corrupt vault object generation",
                    exc,
                )
            else:
                cleanup_detail = "the exact uploaded generation was removed"
        raise BackupError(
            f"vault upload integrity check failed for {key} "
            f"(stored object does not match the local envelope); {cleanup_detail}"
        )

    def download(
        self,
        name: str | SnapshotInfo,
        *,
        overwrite: bool = True,
        _pinned: PinnedPublishedFile | None = None,
    ) -> Path:
        """Fetch one snapshot envelope from the vault into the local backup dir.

        The remote object is authoritative by default. Any same-name local
        file is treated as a cache entry and atomically replaced only after
        the downloaded bytes pass the object's recorded SHA-256 metadata /
        MD5 ETag checks. ``overwrite=False`` is retained only for callers that
        explicitly need no-clobber cache population; restore never uses it.
        Decryption and manifest verification re-check the envelope afterward.
        """
        requested_version = None
        if isinstance(name, SnapshotInfo):
            requested_version = name.version_id
            name = name.name
        if requested_version is not None:
            requested_version = requested_version.strip()
            if (
                not requested_version
                or requested_version.lower() == "null"
            ):
                raise BackupError(
                    "snapshot version_id must identify an immutable vault "
                    "generation"
                )
        if parse_snapshot_name(name) is None:
            raise BackupError(
                f"not a snapshot name: {name!r} (expected "
                f"{SNAPSHOT_PREFIX}<UTC stamp>{SNAPSHOT_SUFFIX})"
            )
        dest = self._local.backup_dir / name
        if not overwrite:
            if dest.is_symlink():
                raise BackupError(
                    f"refusing to trust a symlink as a local snapshot: {dest}"
                )
            if dest.is_file():
                raise BackupError(
                    "local snapshot destination already exists; refusing to "
                    f"overwrite it: {dest}"
                )
            if dest.exists():
                raise BackupError(
                    f"local snapshot destination is not a regular file: {dest}"
                )
        key = self._key_for(name)
        version_id = (
            requested_version
            if requested_version is not None
            else self._discover_snapshot_version(name)
        )
        request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": key,
        }
        if version_id is not None:
            request["VersionId"] = version_id
        with self._vault_call("download a snapshot"):
            response = _vault_response(
                self._client().get_object(**request),
                action="download a snapshot",
            )
        body = response.get("Body")
        try:
            if body is None or not callable(getattr(body, "read", None)):
                raise BackupError(
                    "vault returned an invalid Body while attempting to "
                    "download a snapshot"
                )
            response_receipt = _vault_object_receipt(
                response,
                action="download a snapshot",
            )
            if (
                version_id is not None
                and response_receipt.version_id != version_id
            ):
                raise BackupError(
                    "vault did not return the selected immutable snapshot "
                    f"generation for {name}"
                )
            max_bytes = self._local.resource_limits.max_encrypted_bytes
            declared_size = _vault_content_length(
                response,
                action="download a snapshot",
                required=True,
            )
            assert declared_size is not None
            if declared_size > max_bytes:
                raise BackupError(
                    f"vault snapshot exceeds the configured {max_bytes}-byte encrypted limit"
                )
            expected_sha256 = _vault_sha256_metadata(
                response,
                action="download a snapshot",
            )
            etag = str(response.get("ETag", "")).strip('"')
            has_md5_etag = bool(_MD5_HEX.fullmatch(etag.lower()))
            if not expected_sha256 and not has_md5_etag:
                raise BackupError(
                    "vault snapshot has no cryptographic integrity digest; "
                    "refusing to publish it locally"
                )
            md5 = hashlib.md5(usedforsecurity=False)
            sha256 = hashlib.sha256()
            downloaded = 0
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with durable_atomic_writer(
                    dest,
                    replace_existing=overwrite,
                    pinned=_pinned,
                ) as out:
                    _require_disk_capacity(
                        dest.parent,
                        payload_bytes=declared_size,
                        limits=self._local.resource_limits,
                        label="vault snapshot download",
                    )
                    for chunk in iter(
                        lambda: body.read(_CHUNK),
                        b"",
                    ):
                        if not isinstance(chunk, bytes):
                            raise BackupError(
                                "vault snapshot Body returned a non-bytes chunk"
                            )
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            raise BackupError(
                                "vault snapshot stream exceeds the configured "
                                f"{max_bytes}-byte encrypted limit"
                            )
                        _require_disk_capacity(
                            dest.parent,
                            payload_bytes=len(chunk),
                            limits=self._local.resource_limits,
                            label="vault snapshot download",
                        )
                        md5.update(chunk)
                        sha256.update(chunk)
                        out.write(chunk)
                    if downloaded != declared_size:
                        raise BackupError(
                            f"vault download integrity check failed for {name} "
                            "(stream length does not match ContentLength)"
                        )
                    if expected_sha256 and sha256.hexdigest() != expected_sha256:
                        raise BackupError(
                            f"vault download integrity check failed for {name} "
                            "(SHA-256 mismatch against the recorded upload digest)"
                        )
                    if has_md5_etag and md5.hexdigest() != etag.lower():
                        raise BackupError(
                            f"vault download integrity check failed for {name} "
                            "(ETag mismatch)"
                        )
            except BaseException as exc:
                if isinstance(exc, BackupError):
                    raise
                if isinstance(exc, FileExistsError):
                    try:
                        os.lstat(dest)
                    except OSError:
                        # mkdir() also raises FileExistsError when an ancestor
                        # that must be a directory is a regular file. Reserve
                        # the collision message for an actual final entry.
                        raise BackupError(
                            f"could not write vault snapshot {dest}: {exc}"
                        ) from exc
                    raise BackupError(
                        "local snapshot destination appeared during download; "
                        f"refusing to overwrite it: {dest}"
                    ) from exc
                if isinstance(exc, OSError):
                    raise BackupError(
                        f"could not write vault snapshot {dest}: {exc}"
                    ) from exc
                if isinstance(exc, Exception):
                    _log_suppressed_vault_exception(
                        "download snapshot",
                        exc,
                    )
                    raise _vault_operation_error(
                        "download snapshot"
                    ) from exc
                raise
            logger.info(
                "Snapshot downloaded from vault: %s -> %s",
                self.object_uri(name),
                dest,
            )
            return dest
        finally:
            if body is not None and hasattr(body, "close"):
                try:
                    body.close()
                except Exception as exc:  # pragma: no cover - cleanup only
                    _log_suppressed_vault_exception(
                        "close vault download body",
                        exc,
                    )

    def ensure_local_copy(
        self,
        path: Path | str | SnapshotInfo,
    ) -> tuple[Path, bool]:
        """Materialize the selected vault snapshot as a verified local cache.

        Returns ``(local_path, downloaded)``. Selecting the remote provider is
        an explicit provenance choice, so the canonical basename is always
        fetched from the vault and atomically replaces any same-name local
        cache. A missing or invalid remote object never falls back to local
        bytes.
        """
        if isinstance(path, SnapshotInfo):
            name = path.name
            locator: str | SnapshotInfo = path
        else:
            name = Path(str(path)).name
            locator = name
        if parse_snapshot_name(name) is None:
            raise BackupError(
                f"not a snapshot name: {name!r} (expected "
                f"{SNAPSHOT_PREFIX}<UTC stamp>{SNAPSHOT_SUFFIX})"
            )
        return self.download(locator, overwrite=True), True

    def create_and_replicate(self) -> tuple[SnapshotInfo, SnapshotInfo]:
        """Local write first, then upload; returns ``(local_info, remote_info)``.

        When the provider was built with ``keep_local=False`` the local copy
        is deleted only after a verified upload whose exact immutable
        generation was returned and re-read. Non-versioned S3-compatible
        endpoints still replicate normally, but remote-only mode fails safely
        and preserves the local copy.
        """
        with (
            PinnedPublishedFile() as published,
            tempfile.TemporaryFile(mode="w+b") as sealed,
        ):
            local_info = self._local.export_snapshot(_pinned=published)
            if published.handle is None or published.identity is None:
                raise BackupError(
                    "local export did not retain its published snapshot generation"
                )
            published_identity = published.identity
            if (
                RegularFileIdentity.from_descriptor(
                    published.handle.fileno()
                )
                != published_identity
            ):
                raise BackupError(
                    "local snapshot changed before it could be sealed for upload"
                )
            _require_disk_capacity(
                Path(tempfile.gettempdir()),
                payload_bytes=published_identity.size,
                limits=self._local.resource_limits,
                label="private vault sealed generation",
            )
            copied = 0
            max_bytes = self._local.resource_limits.max_encrypted_bytes
            published.handle.seek(0)
            for chunk in iter(
                lambda: published.handle.read(_CHUNK),
                b"",
            ):
                copied += len(chunk)
                if copied > max_bytes:
                    raise BackupError(
                        "snapshot exceeds the configured "
                        f"{max_bytes}-byte encrypted limit"
                    )
                sealed.write(chunk)
            sealed.flush()
            os.fsync(sealed.fileno())
            if (
                copied != published_identity.size
                or RegularFileIdentity.from_descriptor(
                    published.handle.fileno()
                )
                != published_identity
            ):
                raise BackupError(
                    "local snapshot changed while it was being sealed for upload"
                )
            sealed.seek(0)
            sealed_identity = RegularFileIdentity.from_descriptor(
                sealed.fileno()
            )
            current_local_identity = published_identity
            collisions = 0
            while True:
                try:
                    (
                        remote_info,
                        _uploaded_local_identity,
                        remote_receipt,
                    ) = self._push_with_receipt(
                        local_info.path,
                        _sealed=sealed,
                        _sealed_identity=sealed_identity,
                        _local_identity=current_local_identity,
                    )
                    break
                except _VaultObjectCollision:
                    collisions += 1
                    if collisions > _MAX_REMOTE_NAME_COLLISIONS:
                        raise BackupError(
                            "could not allocate an immutable vault snapshot name after "
                            f"{_MAX_REMOTE_NAME_COLLISIONS} collision retries; "
                            "the local encrypted snapshot remains available"
                        )
                    (
                        local_info,
                        current_local_identity,
                    ) = (
                        self._local._relocate_sealed_snapshot_after_collision(
                            local_info,
                            minimum_counter=collisions + 1,
                            sealed=sealed,
                            expected=current_local_identity,
                        )
                    )
            if not self._keep_local:
                if not remote_receipt.proves_immutable_generation:
                    raise BackupError(
                        "remote-only mode requires an immutable vault generation "
                        "receipt (VersionId); this endpoint did not provide one, "
                        f"so the local snapshot was preserved at {local_info.path}"
                    )
                self._local.remove_snapshot_if_unchanged(
                    local_info.path,
                    expected=current_local_identity,
                )
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

    def restore(
        self,
        path: Path | str | SnapshotInfo,
        *,
        allow_cross_store_partial: bool = False,
    ) -> RestoreResult:
        """Restore the authoritative named vault snapshot.

        The verified remote envelope is atomically cached locally, then the
        actual restore uses the exact local pipeline (decrypt → extract →
        verify inventory → replace live targets).
        """
        name = (
            path.name
            if isinstance(path, SnapshotInfo)
            else Path(str(path)).name
        )
        with PinnedPublishedFile() as pinned:
            local_path = self.download(
                path if isinstance(path, SnapshotInfo) else name,
                overwrite=True,
                _pinned=pinned,
            )
            if pinned.handle is None:
                raise BackupError(
                    "vault restore did not retain the verified local "
                    "snapshot generation"
                )
            return self._local.restore_open_snapshot(
                local_path,
                pinned.handle,
                allow_cross_store_partial=allow_cross_store_partial,
            )

    def list_snapshots(self) -> list[SnapshotInfo]:
        """Snapshots held by the vault, newest first; needs no passphrase.

        ``SnapshotInfo.path`` is the object key rendered as a path. Keys not
        directly under the configured prefix, or whose basename is not a
        canonical snapshot name, are ignored (strays), matching the local
        provider's listing semantics.
        """
        key_prefix = f"{self._config.prefix}/" if self._config.prefix else ""
        snapshots: list[SnapshotInfo] = []
        client = self._client()
        with self._vault_call("list snapshots"):
            try:
                paginator = client.get_paginator("list_object_versions")
            except Exception:
                paginator = client.get_paginator("list_objects_v2")
                for page in paginator.paginate(
                    Bucket=self._config.bucket,
                    Prefix=key_prefix,
                ):
                    for obj in page.get("Contents", []):
                        key = obj["Key"]
                        name = key[len(key_prefix):]
                        if "/" in name:
                            continue
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
            else:
                latest = _latest_vault_entries(
                    paginator.paginate(
                        Bucket=self._config.bucket,
                        Prefix=key_prefix,
                    ),
                    action="list snapshots",
                )
                for key, (kind, obj) in latest.items():
                    if kind == "delete_marker":
                        continue
                    name = key[len(key_prefix):]
                    if "/" in name:
                        continue
                    created_at = parse_snapshot_name(name)
                    if created_at is None:
                        continue
                    version_id = str(obj.get("VersionId", "")).strip()
                    request: dict[str, Any] = {
                        "Bucket": self._config.bucket,
                        "Key": key,
                    }
                    if version_id and version_id.lower() != "null":
                        request["VersionId"] = version_id
                    response = _vault_response(
                        client.head_object(**request),
                        action="list snapshots",
                    )
                    metadata = _vault_metadata(
                        response,
                        action="list snapshots",
                    )
                    if (
                        metadata.get("healthmes-created-at")
                        != created_at.isoformat()
                        or not _vault_sha256_metadata(
                            response,
                            action="list snapshots",
                        )
                    ):
                        continue
                    resolved_version = (
                        version_id
                        if version_id
                        and version_id.lower() != "null"
                        else None
                    )
                    snapshots.append(
                        SnapshotInfo(
                            name=name,
                            path=Path(key),
                            created_at=created_at,
                            size_bytes=int(obj["Size"]),
                            version_id=resolved_version,
                        )
                    )
        snapshots.sort(key=lambda info: (info.created_at, info.name), reverse=True)
        return snapshots

    def list_merged(self) -> list[MergedSnapshot]:
        """Union of the local dir and the vault, labeled by origin."""
        return merge_snapshot_listings(self._local.list_snapshots(), self.list_snapshots())
