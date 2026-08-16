"""Content-bound identity and attestation for the dedicated Hermes runtime.

This module intentionally lives outside ``healthmes.decision`` so the
standalone Hermes supervisor can import it without loading the HealthMes
database/composition stack.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from healthmes.hermes_mcp_inventory import (
    HERMES_DECISION_MCP_SERVER,
    HERMES_DECISION_MCP_TOOL_NAMES,
    HermesMcpToolInventory,
    expected_hermes_mcp_inventory,
)

HERMES_DECISION_RUNTIME_MODEL_ALIAS = "healthmes-decision-runtime"
HERMES_RUNTIME_MANIFEST_SCHEMA = "healthmes.hermes-runtime.v2"
HERMES_RUNTIME_ATTESTATION_SCHEMA = "healthmes.hermes-runtime-attestation.v2"
HERMES_RUNTIME_ATTESTATION_PATH = "/healthmes/runtime-attestation"
HERMES_RUNTIME_HEALTH_PATH = "/healthmes/runtime-health"
HERMES_RUNTIME_HOME_ARTIFACT_NAMES = (
    "config.yaml",
    "SOUL.md",
    ".env",
    ".no-bundled-skills",
)
HERMES_RUNTIME_PROVIDER_ENV_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_DEFAULT_REGION",
        "AWS_REGION",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "XAI_API_KEY",
    }
)
HERMES_RUNTIME_REQUIRED_ENV_NAMES = (
    "HOME",
    "HERMES_DISABLE_LAZY_INSTALLS",
    "HERMES_HOME",
    "HERMES_MANAGED_DIR",
    "HERMES_WRITE_SAFE_ROOT",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONPYCACHEPREFIX",
    "PYTHONUNBUFFERED",
)
HERMES_RUNTIME_EXECUTION_ARTIFACT_NAMES = (
    "child_launcher",
    "child_environment",
    "vendor_entrypoint",
    "supervisor_interpreter",
    "supervisor_module",
    "identity_module",
)
HERMES_RUNTIME_CONTROL_SOURCE_NAMES = (
    "supervisor_module",
    "identity_module",
    "mcp_inventory_module",
)

_MAX_MANIFEST_BYTES = 64_000
_MAX_PROFILE_BYTES = 256_000
_MAX_HOME_ARTIFACT_BYTES = 512_000
_MAX_ATTESTATION_KEY_BYTES = 512
_MAX_VENDOR_ARTIFACT_BYTES = 32_000_000
_MAX_EXECUTION_ARTIFACT_BYTES = 256_000_000
_MAX_VENDOR_FINGERPRINT_FILES = 10_000
_MAX_VENDOR_FINGERPRINT_BYTES = 512_000_000
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_VENDOR_FINGERPRINT_ROOT_FILES = (
    "MANIFEST.in",
    "hermes",
    "pyproject.toml",
    "uv.lock",
)
_VENDOR_FINGERPRINT_TREES = (
    "agent",
    "gateway",
    "hermes_cli",
    "providers",
    "tools",
    "plugins/model-providers",
)
_VENDOR_FINGERPRINT_IMMEDIATE_PYTHON_DIRS = (
    "plugins",
)
_VENDOR_FINGERPRINT_IGNORED_DIRECTORIES = frozenset(
    {
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
    }
)
_VENDOR_FINGERPRINT_IGNORED_SUFFIXES = (
    ".md",
    ".pyc",
    ".pyo",
    ".so",
    ".swp",
    ".swo",
)
_FORBIDDEN_HOME_FILES = frozenset(
    {
        ".anthropic_oauth.json",
        "AGENTS.md",
        "CLAUDE.md",
        ".cursorrules",
        "MEMORY.md",
        "USER.md",
        "auth.json",
        "config.yaml.pre-healthmes-runtime",
        "mcp.json",
        "webhook_subscriptions.json",
    }
)
_FORBIDDEN_NONEMPTY_HOME_DIRS = (
    ".codex",
    ".pycache-disabled",
    "cron",
    "hooks",
    "memories",
    "mcp-tokens",
    "plugins",
    "profiles",
    "scripts",
    "skills",
)


class HermesRuntimeIdentityError(ValueError):
    """A dedicated runtime artifact failed closed validation."""


@dataclass(frozen=True, slots=True)
class HermesRuntimeMcpConnection:
    """Manifest-bound connection details used only for live MCP probing."""

    url: str
    headers: tuple[tuple[str, str], ...] = field(
        default=(),
        repr=False,
    )

    def header_mapping(self) -> dict[str, str]:
        return dict(self.headers)


class HermesRuntimeNamedDigest(BaseModel):
    """One canonical name-to-SHA256 binding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=255)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class HermesRuntimeExecutionArtifact(BaseModel):
    """One actual file whose bytes can change the launched runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=255)
    path: str = Field(min_length=1, max_length=4_096)
    resolved_path: str = Field(min_length=1, max_length=4_096)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: int = Field(ge=0, le=0o7777)

    @model_validator(mode="after")
    def validate_paths(self) -> HermesRuntimeExecutionArtifact:
        if not Path(self.path).is_absolute():
            raise ValueError("Hermes execution artifact path must be absolute")
        if not Path(self.resolved_path).is_absolute():
            raise ValueError(
                "Hermes execution artifact resolved path must be absolute"
            )
        return self


class HermesRuntimeEnvironmentValue(BaseModel):
    """One exact non-secret environment value bound into runtime identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=255)
    value: str = Field(max_length=4_096)


class HermesDecisionRuntimeManifest(BaseModel):
    """Immutable identity of one bootstrap-produced Hermes runtime."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    schema_name: Literal["healthmes.hermes-runtime.v2"] = Field(
        alias="schema"
    )
    runtime_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_semantic_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_alias: Literal["healthmes-decision-runtime"]
    model: str = Field(min_length=1, max_length=255)
    provider: str = Field(min_length=1, max_length=255)
    api_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attestation_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    hermes_home: str = Field(min_length=1, max_length=4_096)
    public_origin: str = Field(min_length=1, max_length=2_048)
    internal_origin: str = Field(min_length=1, max_length=2_048)
    vendor_root: str = Field(min_length=1, max_length=4_096)
    vendor_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    launch_argv: tuple[str, ...] = Field(min_length=1, max_length=32)
    sealed: bool
    control_source_artifacts: tuple[
        HermesRuntimeNamedDigest,
        ...,
    ] = Field(
        min_length=len(HERMES_RUNTIME_CONTROL_SOURCE_NAMES),
        max_length=len(HERMES_RUNTIME_CONTROL_SOURCE_NAMES),
    )
    execution_artifacts: tuple[
        HermesRuntimeExecutionArtifact,
        ...,
    ] = Field(default=(), max_length=len(HERMES_RUNTIME_EXECUTION_ARTIFACT_NAMES))
    home_artifacts: tuple[HermesRuntimeNamedDigest, ...] = Field(
        min_length=len(HERMES_RUNTIME_HOME_ARTIFACT_NAMES),
        max_length=len(HERMES_RUNTIME_HOME_ARTIFACT_NAMES),
    )
    required_environment: tuple[
        HermesRuntimeEnvironmentValue,
        ...,
    ] = Field(min_length=1, max_length=32)
    provider_environment: tuple[HermesRuntimeNamedDigest, ...] = Field(
        default=(),
        max_length=len(HERMES_RUNTIME_PROVIDER_ENV_NAMES),
    )

    @model_validator(mode="after")
    def validate_identity(self) -> HermesDecisionRuntimeManifest:
        if not Path(self.hermes_home).is_absolute():
            raise ValueError("Hermes runtime home must be absolute")
        if not Path(self.vendor_root).is_absolute():
            raise ValueError("Hermes vendor root must be absolute")
        if any(not item or "\x00" in item for item in self.launch_argv):
            raise ValueError("Hermes launch argv is invalid")
        if (
            len(self.launch_argv) != 5
            or not Path(self.launch_argv[0]).is_absolute()
            or self.launch_argv[1:]
            != ("-m", "hermes_cli.main", "gateway", "run")
        ):
            raise ValueError(
                "Hermes launch argv must use the dedicated Python module"
            )
        execution_names = tuple(
            item.name for item in self.execution_artifacts
        )
        if self.sealed:
            if execution_names != HERMES_RUNTIME_EXECUTION_ARTIFACT_NAMES:
                raise ValueError(
                    "Hermes runtime execution artifacts are invalid"
                )
        elif self.execution_artifacts:
            raise ValueError(
                "Unsealed Hermes runtime must not claim execution artifacts"
            )
        control_source_names = tuple(
            item.name for item in self.control_source_artifacts
        )
        if control_source_names != HERMES_RUNTIME_CONTROL_SOURCE_NAMES:
            raise ValueError(
                "Hermes runtime control sources are invalid"
            )
        artifact_names = tuple(item.name for item in self.home_artifacts)
        if artifact_names != HERMES_RUNTIME_HOME_ARTIFACT_NAMES:
            raise ValueError("Hermes runtime home artifacts are invalid")
        required_environment = {
            item.name: item.value for item in self.required_environment
        }
        if len(required_environment) != len(self.required_environment):
            raise ValueError("Hermes runtime environment has duplicates")
        if tuple(required_environment) != HERMES_RUNTIME_REQUIRED_ENV_NAMES:
            raise ValueError("Hermes runtime environment order is invalid")
        if required_environment != runtime_required_environment(
            Path(self.hermes_home)
        ):
            raise ValueError("Hermes runtime environment is invalid")
        provider_names = tuple(
            item.name for item in self.provider_environment
        )
        if (
            provider_names != tuple(sorted(provider_names))
            or len(set(provider_names)) != len(provider_names)
            or not set(provider_names).issubset(
                HERMES_RUNTIME_PROVIDER_ENV_NAMES
            )
        ):
            raise ValueError(
                "Hermes provider environment bindings are invalid"
            )
        _normalize_origin(self.public_origin)
        internal = urlsplit(_normalize_origin(self.internal_origin))
        if internal.scheme != "http" or internal.hostname not in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            raise ValueError("Hermes internal origin must be loopback HTTP")
        expected_id = _sha256_json(self.identity_payload())
        if not hmac.compare_digest(self.runtime_id, expected_id):
            raise ValueError("Hermes runtime id does not match its content")
        return self

    def identity_payload(self) -> dict[str, Any]:
        """Return the fields bound into ``runtime_id``."""

        return self.model_dump(
            mode="json",
            by_alias=True,
            exclude={"runtime_id"},
        )


class HermesRuntimeAttestation(BaseModel):
    """Nonce-bound proof served by the HealthMes-owned supervisor."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    schema_name: Literal[
        "healthmes.hermes-runtime-attestation.v2"
    ] = Field(alias="schema")
    nonce: str = Field(min_length=32, max_length=128)
    issued_at: int = Field(ge=0)
    runtime: HermesDecisionRuntimeManifest
    mcp_inventory: HermesMcpToolInventory
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")


def build_runtime_manifest(
    *,
    profile_bytes: bytes,
    profile_semantic_digest: str,
    model: str,
    provider: str,
    api_key: str,
    attestation_key: bytes,
    hermes_home: Path,
    public_origin: str,
    internal_origin: str,
    vendor_root: Path,
    launch_argv: tuple[str, ...],
    home_artifact_sha256: Mapping[str, str],
    provider_environment: Mapping[str, str] | None = None,
    vendor_fingerprint_source: Path | None = None,
) -> HermesDecisionRuntimeManifest:
    """Build a deterministic identity from exact runtime inputs."""

    if len(profile_bytes) > _MAX_PROFILE_BYTES:
        raise HermesRuntimeIdentityError("hermes_runtime_profile_too_large")
    if not _HEX_SHA256.fullmatch(profile_semantic_digest):
        raise HermesRuntimeIdentityError(
            "hermes_runtime_profile_digest_invalid"
        )
    model = _identity(model, "model")
    provider = _identity(provider, "provider")
    api_key = _api_key(api_key)
    key = _attestation_key_bytes(attestation_key)
    resolved_home = hermes_home.expanduser().resolve()
    artifacts = _home_artifact_digests(home_artifact_sha256)
    provider_digests = _provider_environment_digests(
        provider_environment or {}
    )
    payload: dict[str, Any] = {
        "schema": HERMES_RUNTIME_MANIFEST_SCHEMA,
        "profile_sha256": hashlib.sha256(profile_bytes).hexdigest(),
        "profile_semantic_digest": profile_semantic_digest,
        "model_alias": HERMES_DECISION_RUNTIME_MODEL_ALIAS,
        "model": model,
        "provider": provider,
        "api_key_sha256": _secret_fingerprint(api_key.encode()),
        "attestation_key_sha256": _secret_fingerprint(key),
        "hermes_home": str(resolved_home),
        "public_origin": _normalize_origin(public_origin),
        "internal_origin": _normalize_origin(internal_origin),
        "vendor_root": str(vendor_root.expanduser().resolve()),
        "vendor_fingerprint": vendor_fingerprint(
            vendor_fingerprint_source or vendor_root
        ),
        "launch_argv": list(launch_argv),
        "sealed": False,
        "control_source_artifacts": [
            item.model_dump(mode="json")
            for item in runtime_control_source_artifacts()
        ],
        "execution_artifacts": [],
        "home_artifacts": [
            item.model_dump(mode="json") for item in artifacts
        ],
        "required_environment": [
            {
                "name": name,
                "value": value,
            }
            for name, value in runtime_required_environment(
                resolved_home
            ).items()
        ],
        "provider_environment": [
            item.model_dump(mode="json") for item in provider_digests
        ],
    }
    payload["runtime_id"] = _sha256_json(payload)
    return _manifest_validate(payload)


def runtime_required_environment(hermes_home: Path) -> dict[str, str]:
    """Return the complete non-secret child environment fixed by identity."""

    home = hermes_home.expanduser().resolve()
    managed_dir = home / ".managed-scope-disabled"
    return {
        "HOME": str(home),
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "HERMES_HOME": str(home),
        "HERMES_MANAGED_DIR": str(managed_dir),
        "HERMES_WRITE_SAFE_ROOT": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": str(home / ".pycache-disabled"),
        "PYTHONUNBUFFERED": "1",
    }


def runtime_home_artifact_sha256(hermes_home: Path) -> dict[str, str]:
    """Hash the exact bootstrap-owned files without following symlinks."""

    home = hermes_home.expanduser()
    _validate_runtime_home_directory(home)

    digests: dict[str, str] = {}
    for name in HERMES_RUNTIME_HOME_ARTIFACT_NAMES:
        path = home / name
        content = _read_regular_file(
            path,
            code="hermes_runtime_home_artifact",
            max_bytes=_MAX_HOME_ARTIFACT_BYTES,
            owner_only=True,
        )
        digests[name] = hashlib.sha256(content).hexdigest()
    return digests


def load_runtime_mcp_connection(
    hermes_home: Path,
) -> HermesRuntimeMcpConnection:
    """Load the exact filtered HealthMes MCP endpoint from the sealed profile."""

    profile = _read_regular_file(
        hermes_home.expanduser() / "config.yaml",
        code="hermes_runtime_profile",
        max_bytes=_MAX_PROFILE_BYTES,
        owner_only=True,
    )
    return _profile_mcp_connection(profile)


def load_runtime_manifest(path: Path) -> HermesDecisionRuntimeManifest:
    """Load one bounded strict manifest."""

    raw = _read_regular_file(
        path.expanduser(),
        code="hermes_runtime_manifest",
        max_bytes=_MAX_MANIFEST_BYTES,
        owner_only=True,
    )
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_manifest_invalid"
        ) from exc
    return _manifest_validate(payload)


def write_runtime_manifest(
    path: Path,
    manifest: HermesDecisionRuntimeManifest,
) -> None:
    """Atomically persist a stable manifest."""

    content = (
        json.dumps(
            manifest.model_dump(mode="json", by_alias=True),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    _atomic_write(path, content.encode("ascii"), mode=0o600)


def runtime_manifest_matches_preseal_identity(
    existing: HermesDecisionRuntimeManifest,
    prepared: HermesDecisionRuntimeManifest,
    *,
    vendor_root: Path | None = None,
    supervisor_interpreter: Path | None = None,
    supervisor_module: Path | None = None,
    identity_module: Path | None = None,
    verify_execution_artifacts: bool = True,
) -> bool:
    """Return whether a seal and its live execution files remain current."""

    if prepared.sealed:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_prepared_manifest_sealed"
        )
    if not existing.sealed:
        return existing == prepared
    payload = existing.model_dump(
        mode="json",
        by_alias=True,
        exclude={"runtime_id"},
    )
    payload["sealed"] = False
    payload["execution_artifacts"] = []
    payload["runtime_id"] = _sha256_json(payload)
    if _manifest_validate(payload) != prepared:
        return False
    if not verify_execution_artifacts:
        return True
    try:
        actual_execution_artifacts = runtime_execution_artifacts(
            manifest=existing,
            vendor_root=(
                Path(existing.vendor_root)
                if vendor_root is None
                else vendor_root
            ),
            supervisor_interpreter=supervisor_interpreter,
            supervisor_module=supervisor_module,
            identity_module=identity_module,
        )
    except HermesRuntimeIdentityError:
        return False
    return actual_execution_artifacts == existing.execution_artifacts


def load_attestation_key(path: Path) -> bytes:
    """Read an owner-only hex key without following a symlink."""

    try:
        encoded = _read_regular_file(
            path.expanduser(),
            code="hermes_runtime_attestation_key",
            max_bytes=_MAX_ATTESTATION_KEY_BYTES,
            owner_only=True,
        ).decode("ascii").strip()
    except UnicodeError as exc:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_attestation_key_unreadable"
        ) from exc
    if not re.fullmatch(r"[0-9a-f]{64,256}", encoded) or len(encoded) % 2:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_attestation_key_invalid"
        )
    return _attestation_key_bytes(bytes.fromhex(encoded))


def write_new_attestation_key(path: Path) -> bytes:
    """Create a 256-bit key once and preserve it across bootstraps."""

    if path.exists():
        return load_attestation_key(path)
    key = secrets.token_bytes(32)
    _atomic_write(path, (key.hex() + "\n").encode("ascii"), mode=0o600)
    return load_attestation_key(path)


def validate_expected_runtime(
    *,
    manifest_path: Path,
    attestation_key_path: Path,
    profile_path: Path,
    profile_semantic_digest: str,
    expected_origin: str,
    expected_model: str,
    expected_provider: str,
    expected_api_key: str,
) -> tuple[HermesDecisionRuntimeManifest, bytes]:
    """Bind local expected artifacts before trusting a remote proof."""

    manifest = load_runtime_manifest(manifest_path)
    if not manifest.sealed:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_manifest_unsealed"
        )
    key = load_attestation_key(attestation_key_path)
    try:
        profile_root = profile_path.expanduser().parent
        profile = _read_regular_file(
            profile_path.expanduser(),
            code="hermes_runtime_profile",
            max_bytes=_MAX_PROFILE_BYTES,
            owner_only=True,
        )
        local_artifacts = runtime_home_artifact_sha256(profile_root)
    except HermesRuntimeIdentityError:
        raise
    checks = (
        (
            manifest.profile_sha256,
            hashlib.sha256(profile).hexdigest(),
            "hermes_runtime_profile_mismatch",
        ),
        (
            manifest.profile_semantic_digest,
            profile_semantic_digest,
            "hermes_runtime_profile_digest_mismatch",
        ),
        (
            manifest.api_key_sha256,
            _secret_fingerprint(_api_key(expected_api_key).encode()),
            "hermes_runtime_api_key_mismatch",
        ),
        (
            manifest.attestation_key_sha256,
            _secret_fingerprint(key),
            "hermes_runtime_attestation_key_mismatch",
        ),
    )
    for actual, expected, code in checks:
        if not hmac.compare_digest(actual, expected):
            raise HermesRuntimeIdentityError(code)
    expected_artifacts = {
        item.name: item.sha256 for item in manifest.home_artifacts
    }
    if expected_artifacts != local_artifacts:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_home_artifact_mismatch"
        )
    if manifest.model != _identity(expected_model, "model"):
        raise HermesRuntimeIdentityError("hermes_runtime_model_mismatch")
    if manifest.provider != _identity(expected_provider, "provider"):
        raise HermesRuntimeIdentityError("hermes_runtime_provider_mismatch")
    if manifest.public_origin != _normalize_origin(expected_origin):
        raise HermesRuntimeIdentityError("hermes_runtime_origin_mismatch")
    return manifest, key


def validate_supervised_runtime(
    *,
    manifest_path: Path,
    attestation_key_path: Path,
    hermes_home: Path,
    vendor_root: Path,
    environment: Mapping[str, str] | None = None,
    expected_launch_argv: tuple[str, ...] | None = None,
    require_sealed: bool = True,
    supervisor_interpreter: Path | None = None,
    supervisor_module: Path | None = None,
    identity_module: Path | None = None,
    mcp_inventory_module: Path | None = None,
) -> tuple[HermesDecisionRuntimeManifest, bytes, str]:
    """Validate the exact files and paths the supervisor will execute."""

    manifest = load_runtime_manifest(manifest_path)
    key = load_attestation_key(attestation_key_path)
    resolved_home = str(hermes_home.expanduser().resolve())
    resolved_vendor = str(vendor_root.expanduser().resolve())
    if manifest.hermes_home != resolved_home:
        raise HermesRuntimeIdentityError("hermes_runtime_home_mismatch")
    if manifest.vendor_root != resolved_vendor:
        raise HermesRuntimeIdentityError("hermes_runtime_vendor_root_mismatch")
    if manifest.vendor_fingerprint != vendor_fingerprint(vendor_root):
        raise HermesRuntimeIdentityError(
            "hermes_runtime_vendor_fingerprint_mismatch"
        )
    if manifest.attestation_key_sha256 != _secret_fingerprint(key):
        raise HermesRuntimeIdentityError(
            "hermes_runtime_attestation_key_mismatch"
        )
    actual_artifacts = runtime_home_artifact_sha256(hermes_home)
    expected_artifacts = {
        item.name: item.sha256 for item in manifest.home_artifacts
    }
    if actual_artifacts != expected_artifacts:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_home_artifact_mismatch"
        )
    profile_bytes = _read_regular_file(
        hermes_home / "config.yaml",
        code="hermes_runtime_profile",
        max_bytes=_MAX_PROFILE_BYTES,
        owner_only=True,
    )
    api_key, model, provider, internal_origin = _profile_runtime_identity(
        profile_bytes
    )
    if manifest.api_key_sha256 != _secret_fingerprint(api_key.encode()):
        raise HermesRuntimeIdentityError("hermes_runtime_api_key_mismatch")
    if model != manifest.model:
        raise HermesRuntimeIdentityError("hermes_runtime_model_mismatch")
    if provider != manifest.provider:
        raise HermesRuntimeIdentityError("hermes_runtime_provider_mismatch")
    if internal_origin != manifest.internal_origin:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_internal_origin_mismatch"
        )
    if (
        expected_launch_argv is not None
        and manifest.launch_argv != expected_launch_argv
    ):
        raise HermesRuntimeIdentityError(
            "hermes_runtime_launch_identity_mismatch"
        )
    if environment is not None:
        actual_provider_environment = {
            name: value
            for name, value in environment.items()
            if name in HERMES_RUNTIME_PROVIDER_ENV_NAMES and value
        }
        actual_provider_digests = _provider_environment_digests(
            actual_provider_environment
        )
        if actual_provider_digests != manifest.provider_environment:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_provider_environment_mismatch"
            )
    _assert_no_broad_home_artifacts(hermes_home)
    actual_control_sources = runtime_control_source_artifacts(
        supervisor_module=supervisor_module,
        identity_module=identity_module,
        mcp_inventory_module=mcp_inventory_module,
    )
    if actual_control_sources != manifest.control_source_artifacts:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_control_source_mismatch"
        )
    if manifest.sealed:
        actual_execution_artifacts = runtime_execution_artifacts(
            manifest=manifest,
            vendor_root=vendor_root,
            supervisor_interpreter=supervisor_interpreter,
            supervisor_module=supervisor_module,
            identity_module=identity_module,
        )
        if actual_execution_artifacts != manifest.execution_artifacts:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_execution_artifact_mismatch"
            )
    elif require_sealed:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_manifest_unsealed"
        )
    return manifest, key, api_key


def seal_supervised_runtime(
    *,
    manifest_path: Path,
    attestation_key_path: Path,
    hermes_home: Path,
    vendor_root: Path,
    environment: Mapping[str, str] | None = None,
    expected_launch_argv: tuple[str, ...] | None = None,
    supervisor_interpreter: Path | None = None,
    supervisor_module: Path | None = None,
    identity_module: Path | None = None,
    mcp_inventory_module: Path | None = None,
) -> tuple[HermesDecisionRuntimeManifest, bytes, str]:
    """Bind actual launch files into the manifest before child execution."""

    manifest, key, api_key = validate_supervised_runtime(
        manifest_path=manifest_path,
        attestation_key_path=attestation_key_path,
        hermes_home=hermes_home,
        vendor_root=vendor_root,
        environment=environment,
        expected_launch_argv=expected_launch_argv,
        require_sealed=False,
        supervisor_interpreter=supervisor_interpreter,
        supervisor_module=supervisor_module,
        identity_module=identity_module,
        mcp_inventory_module=mcp_inventory_module,
    )
    if manifest.sealed:
        return manifest, key, api_key

    artifacts = runtime_execution_artifacts(
        manifest=manifest,
        vendor_root=vendor_root,
        supervisor_interpreter=supervisor_interpreter,
        supervisor_module=supervisor_module,
        identity_module=identity_module,
    )
    payload = manifest.model_dump(
        mode="json",
        by_alias=True,
        exclude={"runtime_id"},
    )
    payload["sealed"] = True
    payload["execution_artifacts"] = [
        artifact.model_dump(mode="json") for artifact in artifacts
    ]
    payload["runtime_id"] = _sha256_json(payload)
    sealed_manifest = _manifest_validate(payload)

    current = load_runtime_manifest(manifest_path)
    if current != manifest:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_identity_changed"
        )
    write_runtime_manifest(manifest_path, sealed_manifest)
    return validate_supervised_runtime(
        manifest_path=manifest_path,
        attestation_key_path=attestation_key_path,
        hermes_home=hermes_home,
        vendor_root=vendor_root,
        environment=environment,
        expected_launch_argv=expected_launch_argv,
        supervisor_interpreter=supervisor_interpreter,
        supervisor_module=supervisor_module,
        identity_module=identity_module,
        mcp_inventory_module=mcp_inventory_module,
    )


def runtime_execution_artifacts(
    *,
    manifest: HermesDecisionRuntimeManifest,
    vendor_root: Path,
    supervisor_interpreter: Path | None = None,
    supervisor_module: Path | None = None,
    identity_module: Path | None = None,
) -> tuple[HermesRuntimeExecutionArtifact, ...]:
    """Hash the concrete executables and modules used by the supervisor."""

    launcher = Path(manifest.launch_argv[0])
    runtime_paths = (
        ("child_launcher", launcher, True),
        (
            "child_environment",
            launcher.parent.parent / "pyvenv.cfg",
            False,
        ),
        (
            "vendor_entrypoint",
            vendor_root.expanduser() / "hermes_cli" / "main.py",
            False,
        ),
        (
            "supervisor_interpreter",
            Path(sys.executable)
            if supervisor_interpreter is None
            else supervisor_interpreter,
            True,
        ),
        (
            "supervisor_module",
            Path(__file__).with_name("hermes_runtime_supervisor.py")
            if supervisor_module is None
            else supervisor_module,
            False,
        ),
        (
            "identity_module",
            Path(__file__) if identity_module is None else identity_module,
            False,
        ),
    )
    return tuple(
        _execution_artifact(name, path, executable=executable)
        for name, path, executable in runtime_paths
    )


def open_verified_runtime_launcher(
    manifest: HermesDecisionRuntimeManifest,
) -> int:
    """Open and pin the exact launcher sealed into ``manifest``."""

    if not manifest.sealed:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_manifest_unsealed"
        )
    try:
        artifact = next(
            item
            for item in manifest.execution_artifacts
            if item.name == "child_launcher"
        )
    except StopIteration as exc:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_execution_artifact_mismatch"
        ) from exc

    requested = Path(manifest.launch_argv[0]).expanduser()
    if str(requested) != artifact.path:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_execution_artifact_mismatch"
        )
    try:
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_execution_artifact_unreadable"
        ) from exc
    if str(resolved) != artifact.resolved_path:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_execution_artifact_mismatch"
        )

    descriptor: int | None = None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
        opened = os.fstat(descriptor)
        current = resolved.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or _stat_identity(opened) != _stat_identity(current)
            or stat.S_IMODE(opened.st_mode) != artifact.mode
            or not stat.S_IMODE(opened.st_mode) & 0o111
        ):
            raise HermesRuntimeIdentityError(
                "hermes_runtime_execution_artifact_unsafe"
            )

        digest = hashlib.sha256()
        remaining = _MAX_EXECUTION_ARTIFACT_BYTES
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        if remaining == 0 and os.read(descriptor, 1):
            raise HermesRuntimeIdentityError(
                "hermes_runtime_execution_artifact_too_large"
            )

        after = os.fstat(descriptor)
        resolved_after = requested.resolve(strict=True)
        current_after = resolved_after.lstat()
        if (
            _stat_identity(opened) != _stat_identity(after)
            or resolved_after != resolved
            or _stat_identity(after) != _stat_identity(current_after)
            or not hmac.compare_digest(digest.hexdigest(), artifact.sha256)
        ):
            raise HermesRuntimeIdentityError(
                "hermes_runtime_execution_artifact_unsafe"
            )
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except HermesRuntimeIdentityError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise HermesRuntimeIdentityError(
            "hermes_runtime_execution_artifact_unreadable"
        ) from exc


def runtime_control_source_artifacts(
    *,
    supervisor_module: Path | None = None,
    identity_module: Path | None = None,
    mcp_inventory_module: Path | None = None,
) -> tuple[HermesRuntimeNamedDigest, ...]:
    """Hash HealthMes-owned source before a runtime is allowed to seal."""

    paths = (
        (
            "supervisor_module",
            Path(__file__).with_name("hermes_runtime_supervisor.py")
            if supervisor_module is None
            else supervisor_module,
        ),
        (
            "identity_module",
            Path(__file__) if identity_module is None else identity_module,
        ),
        (
            "mcp_inventory_module",
            Path(__file__).with_name("hermes_mcp_inventory.py")
            if mcp_inventory_module is None
            else mcp_inventory_module,
        ),
    )
    artifacts: list[HermesRuntimeNamedDigest] = []
    for name, path in paths:
        requested = path.expanduser()
        if not requested.is_absolute():
            raise HermesRuntimeIdentityError(
                "hermes_runtime_control_source_path_invalid"
            )
        content = _read_regular_file(
            requested,
            code="hermes_runtime_control_source",
            max_bytes=_MAX_HOME_ARTIFACT_BYTES,
            owner_only=False,
        )
        artifacts.append(
            HermesRuntimeNamedDigest(
                name=name,
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )
    return tuple(artifacts)


def new_attestation_nonce() -> str:
    """Create a unique challenge for one pre-execution proof."""

    return secrets.token_urlsafe(32)


def sign_runtime_attestation(
    *,
    manifest: HermesDecisionRuntimeManifest,
    key: bytes,
    nonce: str,
    mcp_inventory: HermesMcpToolInventory | None = None,
    issued_at: int | None = None,
) -> HermesRuntimeAttestation:
    """Create a nonce-bound HMAC proof over runtime and live MCP inventory."""

    if not manifest.sealed:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_manifest_unsealed"
        )
    if not _NONCE.fullmatch(nonce):
        raise HermesRuntimeIdentityError(
            "hermes_runtime_attestation_nonce_invalid"
        )
    issued = int(time.time()) if issued_at is None else issued_at
    inventory = mcp_inventory or expected_hermes_mcp_inventory()
    unsigned = {
        "schema": HERMES_RUNTIME_ATTESTATION_SCHEMA,
        "nonce": nonce,
        "issued_at": issued,
        "runtime": manifest.model_dump(mode="json", by_alias=True),
        "mcp_inventory": inventory.model_dump(
            mode="json",
            by_alias=True,
        ),
    }
    signature = hmac.new(
        _attestation_key_bytes(key),
        _canonical_json(unsigned),
        hashlib.sha256,
    ).hexdigest()
    return HermesRuntimeAttestation.model_validate(
        {**unsigned, "signature": signature}
    )


def verify_runtime_attestation(
    raw: Any,
    *,
    expected_manifest: HermesDecisionRuntimeManifest,
    key: bytes,
    nonce: str,
    expected_mcp_inventory: HermesMcpToolInventory | None = None,
    now: int | None = None,
    max_age_seconds: int = 30,
) -> HermesRuntimeAttestation:
    """Verify freshness, runtime identity, live inventory, and HMAC."""

    try:
        proof = HermesRuntimeAttestation.model_validate(raw)
    except ValidationError as exc:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_attestation_invalid"
        ) from exc
    if not hmac.compare_digest(proof.nonce, nonce):
        raise HermesRuntimeIdentityError(
            "hermes_runtime_attestation_nonce_mismatch"
        )
    current = int(time.time()) if now is None else now
    if (
        proof.issued_at > current + 5
        or current - proof.issued_at > max_age_seconds
    ):
        raise HermesRuntimeIdentityError(
            "hermes_runtime_attestation_expired"
        )
    if proof.runtime != expected_manifest:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_attestation_identity_mismatch"
        )
    expected_inventory = (
        expected_mcp_inventory or expected_hermes_mcp_inventory()
    )
    if proof.mcp_inventory != expected_inventory:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_mcp_inventory_mismatch"
        )
    unsigned = proof.model_dump(
        mode="json",
        by_alias=True,
        exclude={"signature"},
    )
    expected_signature = hmac.new(
        _attestation_key_bytes(key),
        _canonical_json(unsigned),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(proof.signature, expected_signature):
        raise HermesRuntimeIdentityError(
            "hermes_runtime_attestation_signature_mismatch"
        )
    return proof


def vendor_fingerprint(vendor_root: Path) -> str:
    """Fingerprint the complete canonical Hermes reasoning source surface."""

    root = _validated_directory(
        vendor_root.expanduser(),
        code="hermes_runtime_vendor",
    )
    relative_paths = _vendor_runtime_relative_paths(root)
    digest = hashlib.sha256()
    digest.update(b"healthmes.hermes-vendor-runtime.v2\x00")
    total_bytes = 0
    for relative in relative_paths:
        path = root / relative
        content = _read_regular_file(
            path,
            code="hermes_runtime_vendor",
            max_bytes=_MAX_VENDOR_ARTIFACT_BYTES,
            owner_only=False,
        )
        total_bytes += len(content)
        if total_bytes > _MAX_VENDOR_FINGERPRINT_BYTES:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_vendor_too_large"
            )
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\x00")
        digest.update(hashlib.sha256(content).digest())
        digest.update(b"\x00")
    if relative_paths != _vendor_runtime_relative_paths(root):
        raise HermesRuntimeIdentityError("hermes_runtime_vendor_unsafe")
    return digest.hexdigest()


def _vendor_runtime_relative_paths(root: Path) -> tuple[Path, ...]:
    paths: set[Path] = set()
    for name in _VENDOR_FINGERPRINT_ROOT_FILES:
        paths.add(Path(name))
    try:
        root_entries = tuple(os.scandir(root))
    except OSError as exc:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_vendor_unreadable"
        ) from exc
    for entry in root_entries:
        if entry.name.endswith(".py"):
            if entry.is_symlink() or not entry.is_file(
                follow_symlinks=False
            ):
                raise HermesRuntimeIdentityError(
                    "hermes_runtime_vendor_unsafe"
                )
            paths.add(Path(entry.name))
    for relative in _VENDOR_FINGERPRINT_IMMEDIATE_PYTHON_DIRS:
        directory = _validated_directory(
            root / relative,
            code="hermes_runtime_vendor",
        )
        try:
            entries = tuple(os.scandir(directory))
        except OSError as exc:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_vendor_unreadable"
            ) from exc
        for entry in entries:
            if entry.name.endswith(".py"):
                if entry.is_symlink() or not entry.is_file(
                    follow_symlinks=False
                ):
                    raise HermesRuntimeIdentityError(
                        "hermes_runtime_vendor_unsafe"
                    )
                paths.add(Path(relative) / entry.name)
    for relative in _VENDOR_FINGERPRINT_TREES:
        directory = _validated_directory(
            root / relative,
            code="hermes_runtime_vendor",
        )
        paths.update(
            _walk_vendor_runtime_tree(
                root=root,
                directory=directory,
            )
        )
    ordered = tuple(sorted(paths, key=lambda path: path.as_posix()))
    if not ordered or len(ordered) > _MAX_VENDOR_FINGERPRINT_FILES:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_vendor_file_count_invalid"
        )
    return ordered


def _walk_vendor_runtime_tree(
    *,
    root: Path,
    directory: Path,
) -> tuple[Path, ...]:
    discovered: list[Path] = []
    pending = [directory]
    while pending:
        current = pending.pop()
        try:
            entries = sorted(
                os.scandir(current),
                key=lambda entry: entry.name,
                reverse=True,
            )
        except OSError as exc:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_vendor_unreadable"
            ) from exc
        for entry in entries:
            if entry.name in _VENDOR_FINGERPRINT_IGNORED_DIRECTORIES:
                continue
            path = Path(entry.path)
            if entry.is_symlink():
                raise HermesRuntimeIdentityError(
                    "hermes_runtime_vendor_unsafe"
                )
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)
                continue
            if not entry.is_file(follow_symlinks=False):
                raise HermesRuntimeIdentityError(
                    "hermes_runtime_vendor_unsafe"
                )
            if entry.name == ".DS_Store" or entry.name.endswith(
                _VENDOR_FINGERPRINT_IGNORED_SUFFIXES
            ):
                continue
            try:
                discovered.append(path.relative_to(root))
            except ValueError as exc:
                raise HermesRuntimeIdentityError(
                    "hermes_runtime_vendor_unsafe"
                ) from exc
            if len(discovered) > _MAX_VENDOR_FINGERPRINT_FILES:
                raise HermesRuntimeIdentityError(
                    "hermes_runtime_vendor_file_count_invalid"
                )
    return tuple(discovered)


def _profile_runtime_identity(
    profile_bytes: bytes,
) -> tuple[str, str, str, str]:
    if len(profile_bytes) > _MAX_PROFILE_BYTES:
        raise HermesRuntimeIdentityError("hermes_runtime_profile_too_large")
    try:
        profile = yaml.safe_load(profile_bytes)
        model = profile["model"]["default"]
        provider = profile["model"]["provider"]
        extra = profile["platforms"]["api_server"]["extra"]
        api_key = extra["key"]
        model_name = extra["model_name"]
        route = extra["model_routes"][model]
        host = extra["host"]
        port = extra["port"]
    except (KeyError, TypeError, yaml.YAMLError) as exc:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_profile_invalid"
        ) from exc
    model = _identity(model, "model")
    provider = _identity(provider, "provider")
    if (
        model_name != HERMES_DECISION_RUNTIME_MODEL_ALIAS
        or not isinstance(route, Mapping)
        or route.get("model") != model
        or route.get("provider") != provider
        or not isinstance(host, str)
        or isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65_535
    ):
        raise HermesRuntimeIdentityError(
            "hermes_runtime_profile_invalid"
        )
    return (
        _api_key(api_key),
        model,
        provider,
        _normalize_origin(f"http://{host}:{port}"),
    )


def _profile_mcp_connection(
    profile_bytes: bytes,
) -> HermesRuntimeMcpConnection:
    if len(profile_bytes) > _MAX_PROFILE_BYTES:
        raise HermesRuntimeIdentityError("hermes_runtime_profile_too_large")
    try:
        profile = yaml.safe_load(profile_bytes)
        servers = profile["mcp_servers"]
        if (
            not isinstance(servers, Mapping)
            or set(servers) != {HERMES_DECISION_MCP_SERVER}
        ):
            raise TypeError
        healthmes = servers[HERMES_DECISION_MCP_SERVER]
        if not isinstance(healthmes, Mapping):
            raise TypeError
        url = healthmes["url"]
        tools = healthmes["tools"]
        if not isinstance(tools, Mapping):
            raise TypeError
        include = tools["include"]
        headers = healthmes.get("headers", {})
    except (KeyError, TypeError, yaml.YAMLError) as exc:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_mcp_profile_invalid"
        ) from exc
    if healthmes.get("enabled", True) is not True:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_mcp_profile_invalid"
        )
    if (
        tools.get("resources") is not False
        or tools.get("prompts") is not False
        or not isinstance(include, list)
        or any(not isinstance(item, str) for item in include)
        or frozenset(include) != frozenset(HERMES_DECISION_MCP_TOOL_NAMES)
        or len(include) != len(set(include))
        or tools.get("exclude") not in (None, [], ())
    ):
        raise HermesRuntimeIdentityError(
            "hermes_runtime_mcp_filter_invalid"
        )
    if not isinstance(url, str):
        raise HermesRuntimeIdentityError(
            "hermes_runtime_mcp_profile_invalid"
        )
    parsed = urlsplit(url.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise HermesRuntimeIdentityError(
            "hermes_runtime_mcp_profile_invalid"
        )
    if (
        not isinstance(headers, Mapping)
        or len(headers) > 16
        or any(
            not isinstance(name, str)
            or not isinstance(value, str)
            or not name
            or len(name) > 128
            or len(value) > 8_192
            or any(char in name for char in "\r\n\x00:")
            or any(char in value for char in "\r\n\x00")
            for name, value in headers.items()
        )
    ):
        raise HermesRuntimeIdentityError(
            "hermes_runtime_mcp_headers_invalid"
        )
    return HermesRuntimeMcpConnection(
        url=(
            f"{parsed.scheme}://{parsed.netloc}"
            f"{parsed.path.rstrip('/') or '/'}"
        ),
        headers=tuple(sorted(headers.items())),
    )


def _home_artifact_digests(
    values: Mapping[str, str],
) -> tuple[HermesRuntimeNamedDigest, ...]:
    if (
        set(values) != set(HERMES_RUNTIME_HOME_ARTIFACT_NAMES)
        or any(
            not isinstance(value, str)
            or _HEX_SHA256.fullmatch(value) is None
            for value in values.values()
        )
    ):
        raise HermesRuntimeIdentityError(
            "hermes_runtime_home_artifacts_invalid"
        )
    return tuple(
        HermesRuntimeNamedDigest(name=name, sha256=values[name])
        for name in HERMES_RUNTIME_HOME_ARTIFACT_NAMES
    )


def _provider_environment_digests(
    values: Mapping[str, str],
) -> tuple[HermesRuntimeNamedDigest, ...]:
    if any(
        name not in HERMES_RUNTIME_PROVIDER_ENV_NAMES
        or not isinstance(value, str)
        or not value
        for name, value in values.items()
    ):
        raise HermesRuntimeIdentityError(
            "hermes_runtime_provider_environment_invalid"
        )
    return tuple(
        HermesRuntimeNamedDigest(
            name=name,
            sha256=_secret_fingerprint(values[name].encode("utf-8")),
        )
        for name in sorted(values)
    )


def _execution_artifact(
    name: str,
    path: Path,
    *,
    executable: bool,
) -> HermesRuntimeExecutionArtifact:
    requested = path.expanduser()
    if not requested.is_absolute():
        raise HermesRuntimeIdentityError(
            "hermes_runtime_execution_artifact_path_invalid"
        )
    try:
        requested_before = requested.lstat()
        resolved = requested.resolve(strict=True)
        resolved_before = resolved.lstat()
    except OSError as exc:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_execution_artifact_unreadable"
        ) from exc
    if not (
        stat.S_ISREG(requested_before.st_mode)
        or stat.S_ISLNK(requested_before.st_mode)
    ):
        raise HermesRuntimeIdentityError(
            "hermes_runtime_execution_artifact_unsafe"
        )
    if not stat.S_ISREG(resolved_before.st_mode):
        raise HermesRuntimeIdentityError(
            "hermes_runtime_execution_artifact_unsafe"
        )
    content = _read_regular_file(
        resolved,
        code="hermes_runtime_execution_artifact",
        max_bytes=_MAX_EXECUTION_ARTIFACT_BYTES,
        owner_only=False,
    )
    try:
        resolved_after = requested.resolve(strict=True)
        resolved_metadata = resolved_after.lstat()
    except OSError as exc:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_execution_artifact_unreadable"
        ) from exc
    if (
        resolved_after != resolved
        or not stat.S_ISREG(resolved_metadata.st_mode)
        or _stat_identity(resolved_before)
        != _stat_identity(resolved_metadata)
    ):
        raise HermesRuntimeIdentityError(
            "hermes_runtime_execution_artifact_unsafe"
        )
    mode = stat.S_IMODE(resolved_metadata.st_mode)
    if executable and not mode & 0o111:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_execution_artifact_not_executable"
        )
    return HermesRuntimeExecutionArtifact(
        name=name,
        path=str(requested),
        resolved_path=str(resolved),
        sha256=hashlib.sha256(content).hexdigest(),
        mode=mode,
    )


def _stat_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _assert_no_broad_home_artifacts(hermes_home: Path) -> None:
    for name in _FORBIDDEN_HOME_FILES:
        path = hermes_home / name
        if path.exists() or path.is_symlink():
            raise HermesRuntimeIdentityError(
                "hermes_runtime_broad_home_rejected"
            )
    for name in _FORBIDDEN_NONEMPTY_HOME_DIRS:
        path = hermes_home / name
        if path.is_symlink():
            raise HermesRuntimeIdentityError(
                "hermes_runtime_broad_home_rejected"
            )
        if not path.exists():
            continue
        if not path.is_dir():
            raise HermesRuntimeIdentityError(
                "hermes_runtime_broad_home_rejected"
            )
        try:
            next(path.iterdir())
        except StopIteration:
            continue
        except OSError as exc:
            raise HermesRuntimeIdentityError(
                "hermes_runtime_broad_home_rejected"
            ) from exc
        raise HermesRuntimeIdentityError(
            "hermes_runtime_broad_home_rejected"
        )


def _validate_runtime_home_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_home_unreadable"
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise HermesRuntimeIdentityError("hermes_runtime_home_unsafe")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_home_permissions"
        )


def _validated_directory(path: Path, *, code: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise HermesRuntimeIdentityError(f"{code}_unreadable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise HermesRuntimeIdentityError(f"{code}_unsafe")
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise HermesRuntimeIdentityError(f"{code}_unreadable") from exc


def _read_regular_file(
    path: Path,
    *,
    code: str,
    max_bytes: int,
    owner_only: bool,
) -> bytes:
    expanded = path.expanduser()
    try:
        before = expanded.lstat()
    except OSError as exc:
        raise HermesRuntimeIdentityError(f"{code}_unreadable") from exc
    if not stat.S_ISREG(before.st_mode) or expanded.is_symlink():
        raise HermesRuntimeIdentityError(f"{code}_unsafe")
    if owner_only and stat.S_IMODE(before.st_mode) & 0o077:
        raise HermesRuntimeIdentityError(f"{code}_permissions")
    if before.st_size > max_bytes:
        raise HermesRuntimeIdentityError(f"{code}_too_large")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(expanded, flags)
    except OSError as exc:
        raise HermesRuntimeIdentityError(f"{code}_unreadable") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino)
            != (before.st_dev, before.st_ino)
        ):
            raise HermesRuntimeIdentityError(f"{code}_unsafe")
        if owner_only and stat.S_IMODE(opened.st_mode) & 0o077:
            raise HermesRuntimeIdentityError(f"{code}_permissions")
        if opened.st_size > max_bytes:
            raise HermesRuntimeIdentityError(f"{code}_too_large")

        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > max_bytes:
            raise HermesRuntimeIdentityError(f"{code}_too_large")

        after = os.fstat(descriptor)
        before_identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_identity != after_identity:
            raise HermesRuntimeIdentityError(f"{code}_unsafe")
        return content
    except OSError as exc:
        raise HermesRuntimeIdentityError(f"{code}_unreadable") from exc
    finally:
        os.close(descriptor)


def _manifest_validate(raw: Any) -> HermesDecisionRuntimeManifest:
    try:
        return HermesDecisionRuntimeManifest.model_validate(raw)
    except ValidationError as exc:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_manifest_invalid"
        ) from exc


def _identity(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HermesRuntimeIdentityError(f"hermes_runtime_{label}_invalid")
    return value.strip()


def _api_key(value: str) -> str:
    if not isinstance(value, str) or len(value.strip()) < 32:
        raise HermesRuntimeIdentityError("hermes_runtime_api_key_invalid")
    return value.strip()


def _attestation_key_bytes(value: bytes) -> bytes:
    if not isinstance(value, bytes) or len(value) < 32:
        raise HermesRuntimeIdentityError(
            "hermes_runtime_attestation_key_invalid"
        )
    return value


def _normalize_origin(value: str) -> str:
    if not isinstance(value, str):
        raise HermesRuntimeIdentityError("hermes_runtime_origin_invalid")
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise HermesRuntimeIdentityError("hermes_runtime_origin_invalid")
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            "",
            "",
            "",
        )
    ).rstrip("/")


def _secret_fingerprint(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _atomic_write(path: Path, content: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            mode,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
