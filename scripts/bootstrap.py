#!/usr/bin/env python3
"""Bootstrap the dedicated Hermes Responses runtime for HealthMes.

The canonical deployment has one reasoning ingress:
``POST /v1/wellness-decisions``. This command writes only the isolated
``$HERMES_HOME/decision`` profile and its content-bound manifest and
attestation key. It also removes only legacy HealthMes-owned cron reasoning
jobs from the general Hermes home. User and otherwise unowned cron jobs,
Telegram, webhooks, configuration, and installed Hermes skills are preserved.

Run targets (HERMES_HOME resolution, highest precedence first):
  --hermes-home flag > HERMES_HOME env var > mode default
  (native: ~/.hermes, docker: <repo>/data/hermes).

Usage:
  uv run python scripts/bootstrap.py [--dry-run] [--mode native|docker]
      [--refresh-runtime-seal] [--hermes-home PATH] [--env-file PATH]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import stat
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined

from healthmes.decision.hermes_profile import (
    HermesDecisionProfileAssertion,
)
from healthmes.hermes_runtime_identity import (
    HERMES_RUNTIME_PROVIDER_ENV_NAMES,
    HermesDecisionRuntimeManifest,
    HermesRuntimeIdentityError,
    build_runtime_manifest,
    load_runtime_manifest,
    runtime_home_artifact_sha256,
    runtime_manifest_matches_preseal_identity,
    write_new_attestation_key,
    write_runtime_manifest,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DECISION_TEMPLATE_PATH = (
    REPO_ROOT / "config" / "hermes-decision-config.yaml.tmpl"
)
VENDOR_HERMES = REPO_ROOT / "vendor" / "hermes-agent"

GENERATED_ADJUSTMENT_SECRET_KEY = "HEALTHMES_CALENDAR_ADJUSTMENT_SECRET"
GENERATED_DECISION_API_KEY = "HEALTHMES_DECISION_HERMES_API_KEY"
GENERATED_DECISION_CORRELATION_SECRET = (
    "HEALTHMES_DECISION_CORRELATION_SECRET"
)
GENERATED_DECISION_PROFILE_PATH = (
    "HEALTHMES_DECISION_HERMES_PROFILE_PATH"
)
GENERATED_DECISION_MANIFEST_PATH = (
    "HEALTHMES_DECISION_HERMES_RUNTIME_MANIFEST_PATH"
)
GENERATED_DECISION_ATTESTATION_KEY_PATH = (
    "HEALTHMES_DECISION_HERMES_ATTESTATION_KEY_PATH"
)
NATIVE_DECISION_PUBLIC_ORIGIN = "http://127.0.0.1:8645"
DOCKER_DECISION_PUBLIC_ORIGIN = "http://hermes-decision:8645"
NATIVE_DECISION_VENV = (
    REPO_ROOT / "data" / "runtime" / "hermes-decision-venv"
)
GENERATED_DECISION_PUBLIC_ORIGINS = frozenset(
    {
        NATIVE_DECISION_PUBLIC_ORIGIN,
        DOCKER_DECISION_PUBLIC_ORIGIN,
    }
)
DECISION_SOUL = (
    "You are the dedicated Hermes execution runtime for HealthMes wellness "
    "decisions. Accept reasoning only through the configured API-server "
    "Responses route. Follow the per-request HealthMes instructions and use "
    "only the explicitly configured HealthMes MCP tools. Do not initiate "
    "messages, scheduled work, webhook reasoning, or unrelated tasks.\n"
)
DECISION_ENV = (
    "# Managed by HealthMes. Provider credentials are supplied only through\n"
    "# the manifest-bound supervisor environment; do not add values here.\n"
)
DECISION_HOME_ARTIFACT_CONTENT = {
    "SOUL.md": DECISION_SOUL,
    ".env": DECISION_ENV,
    ".no-bundled-skills": "",
}

# Legacy briefing jobs referenced this relative script name. It remains only
# as part of the exact pre-ownership-marker fingerprints below; bootstrap no
# longer installs or runs the script in the general Hermes home.
SNAPSHOT_SCRIPT_NAME = "healthmes_briefing_snapshot.py"
HEALTHMES_CRON_ORIGIN_SOURCE = "healthmes-bootstrap"

# ---------------------------------------------------------------------------
# Legacy cron reasoning fingerprints
# ---------------------------------------------------------------------------

# Jobs written by bootstrap versions before the ownership marker are removed
# only when their complete managed declaration is an exact match. A user job
# that merely reuses one of these names is not HealthMes-owned.
LEGACY_HEALTHMES_CRON_REASONING_FINGERPRINTS: tuple[
    dict[str, Any], ...
] = (
    {
        "name": "healthmes-morning-plan",
        "schedule": "0 7 * * *",
        "prompt": (
            "Morning briefing. A HealthMes state snapshot (open tasks, "
            "today's events, pending proposals, energy forecast) is injected "
            "above; use it as context. First call "
            "mcp__healthmes__evaluate_morning_calendar_nudge exactly once. "
            "If it returns a proposal, send exactly the returned display "
            "packet: exact change, limitation, viewer link, and the plain-text "
            "reply choices `적용 <handle>` / `그대로 <handle>`. Do not alter "
            "the handle or infer target event details. Send at most one "
            "proposal/message, do not call clarify, and exit after delivery "
            "without waiting for a reply. If it returns no-action or "
            "deduplicated, send only the returned no-action display text when "
            "present; otherwise stay silent."
        ),
        "skills": ["healthmes-planner"],
        "deliver": "telegram",
        "script": SNAPSHOT_SCRIPT_NAME,
    },
    {
        "name": "healthmes-evening-review",
        "schedule": "30 21 * * *",
        "prompt": (
            "Evening review. Using the injected HealthMes state snapshot as "
            "context, compare today's planned blocks against what actually "
            "happened, roll unfinished tasks forward, and flag tomorrow's "
            "first block. One short message in the standard notification "
            "grammar."
        ),
        "skills": ["healthmes-planner"],
        "deliver": "telegram",
        "script": SNAPSHOT_SCRIPT_NAME,
    },
    {
        "name": "healthmes-weekly-plan",
        "schedule": "0 18 * * 0",
        "prompt": (
            "Weekly planning session. Using the injected HealthMes state "
            "snapshot as context, review this week's goals and completion, "
            "surface one evidence-backed health/schedule pattern, then ask "
            "for next week's goal dump. Include the weekly report link so "
            "the full week's numbers are one tap away: use the snapshot's "
            "weekly_report.url verbatim — it is server-built with the "
            "read-only viewer credential embedded, exactly like the "
            "decision-viewer links. Never construct or edit /reports/weekly "
            "URLs yourself; if the snapshot has no weekly_report.url, omit "
            "the link. One message in the standard notification grammar."
        ),
        "skills": ["healthmes-planner"],
        "deliver": "telegram",
        "script": SNAPSHOT_SCRIPT_NAME,
    },
    {
        "name": "healthmes-morning-plan",
        "prompt": (
            "Morning briefing. A HealthMes state snapshot (open tasks, "
            "today's events, pending proposals, energy forecast) is injected "
            "above; use it as context and read today's readiness via the "
            "healthmes MCP tools, then propose today's block layout based "
            "on the energy picture. One message in the standard notification "
            "grammar."
        ),
        "schedule": "0 7 * * *",
        "skills": ["healthmes-planner"],
        "deliver": "telegram",
        "script": SNAPSHOT_SCRIPT_NAME,
    },
)


@dataclass
class Plan:
    """Collected actions; printed in --dry-run, executed otherwise."""

    dry_run: bool
    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def act(self, message: str) -> None:
        self.actions.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def report(self) -> None:
        prefix = "[dry-run] would" if self.dry_run else "[bootstrap]"
        for action in self.actions:
            print(f"{prefix} {action}")
        for warning in self.warnings:
            print(f"[warning] {warning}", file=sys.stderr)


# ---------------------------------------------------------------------------
# .env handling
# ---------------------------------------------------------------------------


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a dotenv-style file (KEY=VALUE lines; comments/blank ignored)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def resolve_env(env_file: Path) -> dict[str, str]:
    """Merged env view: .env file values, overridden by the process env."""
    merged = load_env_file(env_file)
    merged.update({k: v for k, v in os.environ.items()})
    return merged


def upsert_env_var(env_file: Path, key: str, value: str) -> None:
    """Set ``key=value`` in *env_file*, replacing an existing assignment."""
    lines: list[str] = []
    replaced = False
    if env_file.is_file():
        lines = env_file.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"export {key}="):
            lines[i] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Generated by scripts/bootstrap.py")
        lines.append(f"{key}={value}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_calendar_adjustment_secret(env_file: Path, env: dict[str, str], plan: Plan) -> str:
    existing = env.get(GENERATED_ADJUSTMENT_SECRET_KEY, "").strip()
    if existing:
        if len(existing) < 32:
            raise ValueError(
                f"{GENERATED_ADJUSTMENT_SECRET_KEY} must contain at least 32 characters"
            )
        return existing
    generated = secrets.token_hex(32)
    plan.act(f"generate {GENERATED_ADJUSTMENT_SECRET_KEY} into {env_file}")
    if not plan.dry_run:
        upsert_env_var(env_file, GENERATED_ADJUSTMENT_SECRET_KEY, generated)
    return generated


def ensure_decision_api_key(
    env_file: Path,
    env: dict[str, str],
    plan: Plan,
) -> str:
    """Return the dedicated Hermes API key, minting it when absent."""

    existing = env.get(GENERATED_DECISION_API_KEY, "").strip()
    if existing:
        if len(existing) < 32:
            raise ValueError(
                f"{GENERATED_DECISION_API_KEY} must contain at least "
                "32 characters"
            )
        return existing
    generated = secrets.token_hex(32)
    plan.act(f"generate {GENERATED_DECISION_API_KEY} into {env_file}")
    if not plan.dry_run:
        upsert_env_var(env_file, GENERATED_DECISION_API_KEY, generated)
    return generated


def ensure_decision_correlation_secret(
    env_file: Path,
    env: dict[str, str],
    plan: Plan,
) -> str:
    """Return the stable decision correlation secret, minting it once."""

    existing = env.get(
        GENERATED_DECISION_CORRELATION_SECRET,
        "",
    ).strip()
    if existing:
        if len(existing) < 32:
            raise ValueError(
                f"{GENERATED_DECISION_CORRELATION_SECRET} must contain at "
                "least 32 characters"
            )
        return existing
    generated = secrets.token_hex(32)
    plan.act(
        f"generate {GENERATED_DECISION_CORRELATION_SECRET} into {env_file}"
    )
    if not plan.dry_run:
        upsert_env_var(
            env_file,
            GENERATED_DECISION_CORRELATION_SECRET,
            generated,
        )
    return generated


def ensure_decision_profile_path(
    env_file: Path,
    env: dict[str, str],
    profile_path: Path,
    plan: Plan,
) -> str:
    """Persist the rendered profile path without replacing an override."""

    existing = env.get(GENERATED_DECISION_PROFILE_PATH, "").strip()
    if existing:
        return existing
    generated = str(profile_path.expanduser().resolve())
    plan.act(
        f"set {GENERATED_DECISION_PROFILE_PATH} in {env_file} "
        f"to {generated}"
    )
    if not plan.dry_run:
        upsert_env_var(
            env_file,
            GENERATED_DECISION_PROFILE_PATH,
            generated,
        )
    return generated


def ensure_generated_path(
    env_file: Path,
    env: dict[str, str],
    *,
    key: str,
    path: Path,
    plan: Plan,
) -> str:
    """Persist one bootstrap-owned artifact path unless explicitly set."""

    existing = env.get(key, "").strip()
    if existing:
        return existing
    generated = str(path.expanduser().resolve())
    plan.act(f"set {key} in {env_file} to {generated}")
    if not plan.dry_run:
        upsert_env_var(env_file, key, generated)
    return generated


def validate_generated_path_override(
    env: Mapping[str, str],
    *,
    key: str,
    expected_path: Path,
) -> None:
    """Reject a path override that would detach the attested runtime bundle."""

    configured = env.get(key, "").strip()
    if not configured:
        return
    if (
        Path(configured).expanduser().resolve()
        != expected_path.expanduser().resolve()
    ):
        raise ValueError(f"{key} must point to {expected_path}")


def validate_dedicated_home_path(decision_home: Path) -> None:
    """Reject a symlink or non-directory before any bootstrap mutation."""

    try:
        metadata = decision_home.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError(
            f"dedicated Hermes runtime home is unreadable: {decision_home}"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode) or decision_home.is_symlink()
    ):
        raise ValueError(
            f"dedicated Hermes runtime home is unsafe: {decision_home}"
        )


def prepare_dedicated_home(
    decision_home: Path,
    plan: Plan,
) -> None:
    """Create or tighten the validated owner-only runtime directory."""

    validate_dedicated_home_path(decision_home)
    plan.act(f"ensure owner-only dedicated runtime home {decision_home}")
    if plan.dry_run:
        return
    decision_home.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(decision_home, 0o700)
    verified = decision_home.lstat()
    if (
        not stat.S_ISDIR(verified.st_mode)
        or decision_home.is_symlink()
        or stat.S_IMODE(verified.st_mode) != 0o700
    ):
        raise ValueError(
            f"dedicated Hermes runtime home is unsafe: {decision_home}"
        )


def resolve_decision_public_origin(
    env: Mapping[str, str],
    *,
    mode: str,
) -> tuple[str, bool]:
    """Switch bootstrap-owned defaults while preserving a custom origin."""

    generated = (
        DOCKER_DECISION_PUBLIC_ORIGIN
        if mode == "docker"
        else NATIVE_DECISION_PUBLIC_ORIGIN
    )
    configured = env.get(
        "HEALTHMES_DECISION_HERMES_BASE_URL",
        "",
    ).strip()
    if not configured or configured in GENERATED_DECISION_PUBLIC_ORIGINS:
        return generated, configured != generated
    return configured, False


def ensure_docker_bind_identity(
    env_file: Path,
    env: dict[str, str],
    plan: Plan,
) -> None:
    """Bind the container user to the owner of the 0600 runtime files."""

    for key, generated in (
        ("HERMES_UID", str(os.getuid())),
        ("HERMES_GID", str(os.getgid())),
    ):
        configured = env.get(key, "").strip()
        if configured:
            if not configured.isdecimal():
                raise ValueError(f"{key} must be a non-negative integer")
            continue
        plan.act(f"set {key} in {env_file} to {generated}")
        env[key] = generated
        if not plan.dry_run:
            upsert_env_var(env_file, key, generated)


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


def build_decision_context(
    env: Mapping[str, str],
    mode: str,
    repo_root: Path,
) -> dict[str, str]:
    """Build only the values consumed by the dedicated decision profile."""

    del repo_root
    healthmes_port = env.get("HEALTHMES_PORT", "8100").strip() or "8100"
    default_mcp_url = (
        "http://healthmes:8100/mcp"
        if mode == "docker"
        else f"http://localhost:{healthmes_port}/mcp"
    )
    return {
        "healthmes_mcp_url": env.get("HEALTHMES_MCP_URL", "").strip()
        or default_mcp_url,
        "healthmes_api_token": env.get(
            "HEALTHMES_API_TOKEN",
            "",
        ).strip(),
        "decision_hermes_host": env.get(
            "HEALTHMES_DECISION_HERMES_INTERNAL_HOST",
            "",
        ).strip()
        or "127.0.0.1",
        "decision_hermes_port": env.get(
            "HEALTHMES_DECISION_HERMES_INTERNAL_PORT",
            "",
        ).strip()
        or "8646",
        "decision_hermes_api_key": env.get(
            GENERATED_DECISION_API_KEY,
            "",
        ).strip(),
        "decision_hermes_model": env.get(
            "HEALTHMES_DECISION_HERMES_MODEL",
            "",
        ).strip(),
        "decision_hermes_provider": env.get(
            "HEALTHMES_DECISION_HERMES_PROVIDER",
            "",
        ).strip(),
        "decision_hermes_model_base_url": env.get(
            "HEALTHMES_DECISION_HERMES_MODEL_BASE_URL",
            "",
        ).strip(),
        "decision_hermes_model_api_key": env.get(
            "HEALTHMES_DECISION_HERMES_MODEL_API_KEY",
            "",
        ).strip(),
    }


def render_template(
    context: dict[str, Any],
    *,
    template_path: Path,
) -> str:
    """Render the Jinja2 template and fail fast if the result is not YAML."""
    jinja_env = Environment(undefined=StrictUndefined, keep_trailing_newline=True)
    rendered = jinja_env.from_string(
        template_path.read_text(encoding="utf-8")
    ).render(**context)
    parsed = yaml.safe_load(rendered)
    if not isinstance(parsed, dict):
        raise ValueError("rendered hermes config is not a YAML mapping")
    return rendered


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *overlay* into *base* (overlay wins on conflicts)."""
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def write_config(hermes_home: Path, rendered: str, plan: Plan) -> Path:
    """Write/merge the rendered config into ``$HERMES_HOME/config.yaml``."""
    config_path = hermes_home / "config.yaml"
    if not config_path.exists():
        plan.act(f"write {config_path} (rendered template, comments preserved)")
        if not plan.dry_run:
            hermes_home.mkdir(parents=True, exist_ok=True)
            config_path.write_text(rendered, encoding="utf-8")
            _chmod_quiet(config_path, 0o600)
        return config_path

    existing_text = config_path.read_text(encoding="utf-8")
    existing = yaml.safe_load(existing_text) or {}
    if not isinstance(existing, dict):
        plan.warn(f"{config_path} is not a YAML mapping; replacing it wholesale")
        existing = {}
    merged = _deep_merge(existing, yaml.safe_load(rendered))
    if merged == existing:
        plan.act(f"keep {config_path} (already up to date)")
        return config_path

    backup_path = config_path.with_name("config.yaml.healthmes-backup")
    plan.act(
        f"merge rendered keys into existing {config_path} "
        f"(backup once at {backup_path.name}; YAML comments in the previous "
        f"file are not preserved by the merge)"
    )
    if not plan.dry_run:
        if not backup_path.exists():
            backup_path.write_text(existing_text, encoding="utf-8")
            _chmod_quiet(backup_path, 0o600)
        config_path.write_text(
            yaml.safe_dump(merged, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        _chmod_quiet(config_path, 0o600)
    return config_path


def write_exact_decision_config(
    decision_home: Path,
    rendered: str,
    plan: Plan,
) -> Path:
    """Atomically replace the dedicated profile without preserving broad keys."""

    config_path = decision_home / "config.yaml"
    if (
        config_path.is_file()
        and config_path.read_text(encoding="utf-8") == rendered
    ):
        plan.act(f"keep {config_path} (exact decision profile is current)")
        if not plan.dry_run:
            os.chmod(config_path, 0o600)
        return config_path
    plan.act(f"replace {config_path} with the exact dedicated profile")
    if not plan.dry_run:
        decision_home.mkdir(parents=True, exist_ok=True)
        _backup_runtime_artifact(config_path)
        _atomic_write_text(config_path, rendered, mode=0o600)
    return config_path


def write_decision_runtime_artifacts(
    decision_home: Path,
    plan: Plan,
) -> None:
    """Write fixed runtime files while archiving any prior user content."""

    for name, content in DECISION_HOME_ARTIFACT_CONTENT.items():
        path = decision_home / name
        if path.is_file() and path.read_text(encoding="utf-8") == content:
            plan.act(f"keep {path} (managed runtime artifact is current)")
            if not plan.dry_run:
                os.chmod(path, 0o600)
            continue
        plan.act(f"replace {path} with the managed runtime artifact")
        if not plan.dry_run:
            decision_home.mkdir(parents=True, exist_ok=True)
            _backup_runtime_artifact(path)
            _atomic_write_text(path, content, mode=0o600)


def write_prepared_runtime_manifest(
    path: Path,
    manifest: HermesDecisionRuntimeManifest,
    plan: Plan,
    *,
    verify_execution_artifacts: bool,
    refresh_runtime_seal: bool = False,
) -> None:
    """Preserve an equivalent seal; publish changed intent as unsealed."""

    existing = None
    if path.is_file():
        try:
            existing = load_runtime_manifest(path)
        except HermesRuntimeIdentityError:
            pass
    if (
        not refresh_runtime_seal
        and existing is not None
        and runtime_manifest_matches_preseal_identity(
            existing,
            manifest,
            vendor_root=Path(manifest.vendor_root),
            verify_execution_artifacts=verify_execution_artifacts,
        )
    ):
        state = "supervisor-sealed" if existing.sealed else "prepared"
        plan.act(
            f"keep {state} content-bound runtime manifest {path} "
            f"({existing.runtime_id})"
        )
        return
    plan.act(
        f"write content-bound runtime manifest {path} "
        f"({manifest.runtime_id}) for supervisor sealing"
    )
    if not plan.dry_run:
        write_runtime_manifest(path, manifest)


def _backup_runtime_artifact(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_file():
        raise ValueError(
            f"dedicated Hermes runtime artifact is unsafe: {path}"
        )
    backup_dir = path.parent.parent / "decision-runtime-backups"
    backup_name = path.name.lstrip(".") or "artifact"
    backup_path = backup_dir / f"{backup_name}.pre-healthmes-runtime"
    if backup_path.exists():
        return
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup_path)
    _chmod_quiet(backup_path, 0o600)


def assert_dedicated_home_has_no_broad_reasoning(
    decision_home: Path,
) -> None:
    """Fail closed instead of deleting unknown state from an existing home."""

    forbidden_files = [
        decision_home / name
        for name in (
            ".anthropic_oauth.json",
            "AGENTS.md",
            "CLAUDE.md",
            "MEMORY.md",
            "USER.md",
            "auth.json",
            "config.yaml.pre-healthmes-runtime",
            "mcp.json",
            "webhook_subscriptions.json",
        )
        if (decision_home / name).exists()
        or (decision_home / name).is_symlink()
    ]
    forbidden_dirs: list[Path] = []
    for name in (
        ".codex",
        "cron",
        "hooks",
        "memories",
        "mcp-tokens",
        "plugins",
        "profiles",
        "scripts",
        "skills",
    ):
        path = decision_home / name
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            forbidden_dirs.append(path)
            continue
        if not path.is_dir():
            continue
        try:
            next(path.iterdir())
        except StopIteration:
            continue
        forbidden_dirs.append(path)
    forbidden = [*forbidden_files, *forbidden_dirs]
    if forbidden:
        rendered = ", ".join(
            str(path) for path in sorted(forbidden, key=str)
        )
        raise ValueError(
            "dedicated Hermes home contains broad reasoning artifacts; "
            f"archive them before bootstrap: {rendered}"
        )


def _atomic_write_text(path: Path, content: str, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
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


def _chmod_quiet(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Skills install (copies into $HERMES_HOME/skills/)
# ---------------------------------------------------------------------------


def discover_skill_dirs(repo_root: Path) -> list[Path]:
    """Repo skill directories (immediate children of skills/ with a SKILL.md)."""
    skills_root = repo_root / "skills"
    if not skills_root.is_dir():
        return []
    return sorted(
        child
        for child in skills_root.iterdir()
        if child.is_dir() and (child / "SKILL.md").is_file()
    )


def _dir_snapshot(root: Path) -> dict[str, bytes]:
    """Relative-path -> content map for regular files under root."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def install_skills(repo_root: Path, hermes_home: Path, plan: Plan) -> list[Path]:
    """Copy repo skills into ``$HERMES_HOME/skills/`` (idempotent by content).

    Copies, not symlinks: the vendor trust check resolves symlinks
    (vendor/hermes-agent/tools/skills_tool.py, ``skill_md.resolve()
    .relative_to(trusted)``) and logs a security warning for every skill
    load of a symlinked skill — on every alert and briefing. Copies inside
    $HERMES_HOME resolve as trusted and are the only layout that works
    unchanged in docker mode (./data/hermes is mounted; the repo is not).
    Symlinks left by earlier bootstrap versions are migrated to copies;
    content drift (edits in either direction) is resynced from the repo.
    """
    skills_home = hermes_home / "skills"
    installed: list[Path] = []

    for skill_dir in discover_skill_dirs(repo_root):
        dest = skills_home / skill_dir.name
        if dest.is_symlink():
            plan.act(f"migrate symlink {dest} to a copy of {skill_dir}")
            if not plan.dry_run:
                dest.unlink()
                shutil.copytree(skill_dir, dest)
            installed.append(dest)
            continue
        if dest.exists() and not dest.is_dir():
            plan.warn(
                f"{dest} exists and is not a directory; leaving it untouched "
                f"(remove it manually to let bootstrap manage this skill)"
            )
            continue
        if dest.is_dir():
            if _dir_snapshot(dest) == _dir_snapshot(skill_dir):
                plan.act(f"keep skill copy {dest} (content up to date)")
                installed.append(dest)
                continue
            plan.act(f"resync skill copy {dest} from {skill_dir}")
            if not plan.dry_run:
                shutil.rmtree(dest)
                shutil.copytree(skill_dir, dest)
            installed.append(dest)
            continue
        plan.act(f"copy skill {skill_dir} -> {dest}")
        if not plan.dry_run:
            skills_home.mkdir(parents=True, exist_ok=True)
            shutil.copytree(skill_dir, dest)
        installed.append(dest)
    return installed


# ---------------------------------------------------------------------------
# Legacy general-Hermes cron migration
# ---------------------------------------------------------------------------


def _cron_schedule_expr(job: Mapping[str, Any]) -> str:
    schedule = job.get("schedule")
    if isinstance(schedule, Mapping):
        return str(schedule.get("expr") or schedule.get("display") or "")
    return str(schedule or "")


def _cron_skills(job: Mapping[str, Any]) -> list[str]:
    skills = job.get("skills")
    if isinstance(skills, str):
        raw = [skills]
    elif isinstance(skills, list):
        raw = skills
    else:
        raw = [job.get("skill")]
    return [str(skill).strip() for skill in raw if str(skill or "").strip()]


def _cron_managed_declaration(job: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": str(job.get("name") or ""),
        "prompt": str(job.get("prompt") or ""),
        "schedule": _cron_schedule_expr(job),
        "skills": _cron_skills(job),
        "deliver": job.get("deliver"),
        "script": job.get("script"),
    }


def _is_legacy_healthmes_owned_cron(job: Mapping[str, Any]) -> bool:
    origin = job.get("origin")
    if origin is not None:
        return (
            isinstance(origin, Mapping)
            and origin.get("source") == HEALTHMES_CRON_ORIGIN_SOURCE
        )
    declaration = _cron_managed_declaration(job)
    return declaration in LEGACY_HEALTHMES_CRON_REASONING_FINGERPRINTS


def _read_regular_cron_database(jobs_file: Path) -> bytes:
    if jobs_file.is_symlink() or jobs_file.parent.is_symlink():
        raise ValueError(
            "legacy Hermes cron database path is unsafe; refusing migration"
        )
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(jobs_file, flags)
    except OSError as exc:
        raise RuntimeError(
            "legacy Hermes cron database is unreadable"
        ) from exc
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError(
                "legacy Hermes cron database path is unsafe; "
                "refusing migration"
            )
        return handle.read()


def _load_cron_document(
    jobs_file: Path,
) -> tuple[dict[str, Any] | list[Any], list[Any], bytes]:
    raw = _read_regular_cron_database(jobs_file)
    try:
        document = json.loads(raw.decode("utf-8"), strict=False)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "legacy Hermes cron database is malformed; refusing migration"
        ) from exc
    if isinstance(document, dict):
        jobs = document.get("jobs")
    elif isinstance(document, list):
        jobs = document
    else:
        jobs = None
    if not isinstance(jobs, list):
        raise ValueError(
            "legacy Hermes cron database has an invalid jobs envelope; "
            "refusing migration"
        )
    return document, jobs, raw


def _write_cron_document_if_unchanged(
    jobs_file: Path,
    *,
    original: bytes,
    document: dict[str, Any] | list[Any],
) -> None:
    encoded = (
        json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    fd, tmp_path = tempfile.mkstemp(
        dir=str(jobs_file.parent),
        prefix=".healthmes-cron-migration-",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            current = _read_regular_cron_database(jobs_file)
        except (RuntimeError, ValueError) as exc:
            raise RuntimeError(
                "legacy Hermes cron database changed during migration"
            ) from exc
        if current != original:
            raise RuntimeError(
                "legacy Hermes cron database changed during migration"
            )
        os.replace(tmp_path, jobs_file)
        _chmod_quiet(jobs_file, 0o600)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def remove_legacy_healthmes_cron_reasoning(
    hermes_home: Path,
    plan: Plan,
) -> int:
    """Remove only cron jobs proven to be owned by legacy HealthMes bootstrap."""

    jobs_file = hermes_home / "cron" / "jobs.json"
    try:
        jobs_file.lstat()
    except FileNotFoundError:
        plan.act(
            "legacy HealthMes cron reasoning migration: no general cron "
            "database found"
        )
        return 0
    except OSError as exc:
        raise RuntimeError(
            "legacy Hermes cron database is unreadable"
        ) from exc

    document, jobs, original = _load_cron_document(jobs_file)
    retained: list[Any] = []
    removed: list[str] = []
    for index, job in enumerate(jobs):
        if isinstance(job, Mapping) and _is_legacy_healthmes_owned_cron(job):
            name = str(job.get("name") or "<unnamed>")
            job_id = str(job.get("id") or f"index-{index}")
            removed.append(f"{name}:{job_id}")
        else:
            retained.append(job)

    if not removed:
        plan.act(
            "legacy HealthMes cron reasoning migration: preserve all "
            f"{len(jobs)} general cron job(s); no owned job found"
        )
        return 0

    plan.act(
        "remove legacy HealthMes-owned Hermes cron reasoning job(s) "
        f"{', '.join(removed)}; preserve {len(retained)} unowned job(s)"
    )
    if plan.dry_run:
        return len(removed)

    if isinstance(document, dict):
        migrated: dict[str, Any] | list[Any] = dict(document)
        migrated["jobs"] = retained
        migrated["updated_at"] = datetime.now().astimezone().isoformat()
    else:
        migrated = retained
    _write_cron_document_if_unchanged(
        jobs_file,
        original=original,
        document=migrated,
    )
    return len(removed)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _decision_profile_digest(
    *,
    rendered: str,
    model: str,
    provider: str,
    api_key: str,
    persisted_path: Path | None,
) -> str:
    if persisted_path is not None:
        path = persisted_path
        return HermesDecisionProfileAssertion(
            path,
            expected_model=model,
            expected_provider=provider,
            expected_api_key=api_key,
        ).verify()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "config.yaml"
        path.write_text(rendered, encoding="utf-8")
        return HermesDecisionProfileAssertion(
            path,
            expected_model=model,
            expected_provider=provider,
            expected_api_key=api_key,
        ).verify()


def resolve_hermes_home(args: argparse.Namespace, repo_root: Path) -> Path:
    if args.hermes_home:
        return Path(args.hermes_home).expanduser()
    env_home = os.environ.get("HERMES_HOME", "").strip()
    if env_home:
        return Path(env_home).expanduser()
    if args.mode == "docker":
        return repo_root / "data" / "hermes"
    return Path.home() / ".hermes"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bootstrap.py",
        description=(
            "Render and attest the single dedicated Hermes Responses runtime "
            "used by HealthMes wellness decisions."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report every action without writing anything.",
    )
    parser.add_argument(
        "--mode",
        choices=("native", "docker"),
        default="native",
        help=(
            "Default-value profile: 'native' targets a mac-local stack "
            "(localhost endpoints, ~/.hermes/decision); 'docker' targets the "
            "docker-compose stack (in-cluster endpoints, "
            "./data/hermes/decision). "
            "Explicit env vars always override either profile."
        ),
    )
    parser.add_argument(
        "--hermes-home",
        help="Hermes home directory (default: $HERMES_HOME, else per --mode).",
    )
    parser.add_argument(
        "--env-file",
        default=str(REPO_ROOT / ".env"),
        help="dotenv file to read and to receive generated secrets (default: <repo>/.env).",
    )
    parser.add_argument(
        "--refresh-runtime-seal",
        action="store_true",
        help=(
            "Publish an unsealed runtime manifest even when the current seal "
            "matches the configured intent. Stop the old supervisor, run "
            "this after rebuilding or replacing the Docker image, then start "
            "the new supervisor so it seals the new container execution "
            "artifacts."
        ),
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    plan = Plan(dry_run=args.dry_run)
    env_file = Path(args.env_file).expanduser()
    hermes_home = resolve_hermes_home(args, REPO_ROOT)
    decision_home = hermes_home / "decision"
    env = resolve_env(env_file)

    model = env.get("HEALTHMES_DECISION_HERMES_MODEL", "").strip()
    provider = env.get(
        "HEALTHMES_DECISION_HERMES_PROVIDER",
        "",
    ).strip()
    if not model or not provider:
        raise ValueError(
            "HEALTHMES_DECISION_HERMES_MODEL and "
            "HEALTHMES_DECISION_HERMES_PROVIDER are required"
        )

    profile_path = decision_home / "config.yaml"
    manifest_path = decision_home / "runtime-manifest.json"
    key_path = decision_home / "runtime-attestation.key"
    validate_dedicated_home_path(decision_home)
    assert_dedicated_home_has_no_broad_reasoning(decision_home)
    for key, expected_path in (
        (GENERATED_DECISION_PROFILE_PATH, profile_path),
        (GENERATED_DECISION_MANIFEST_PATH, manifest_path),
        (GENERATED_DECISION_ATTESTATION_KEY_PATH, key_path),
    ):
        validate_generated_path_override(
            env,
            key=key,
            expected_path=expected_path,
        )

    for key in (
        GENERATED_DECISION_API_KEY,
        GENERATED_DECISION_CORRELATION_SECRET,
    ):
        existing_secret = env.get(key, "").strip()
        if existing_secret and len(existing_secret) < 32:
            raise ValueError(
                f"{key} must contain at least 32 characters"
            )

    remove_legacy_healthmes_cron_reasoning(hermes_home, plan)

    decision_api_key = ensure_decision_api_key(env_file, env, plan)
    env[GENERATED_DECISION_API_KEY] = decision_api_key
    decision_correlation_secret = ensure_decision_correlation_secret(
        env_file,
        env,
        plan,
    )
    env[GENERATED_DECISION_CORRELATION_SECRET] = (
        decision_correlation_secret
    )
    decision_profile_path = ensure_generated_path(
        env_file,
        env,
        key=GENERATED_DECISION_PROFILE_PATH,
        path=profile_path,
        plan=plan,
    )
    env[GENERATED_DECISION_PROFILE_PATH] = decision_profile_path
    configured_manifest_path = ensure_generated_path(
        env_file,
        env,
        key=GENERATED_DECISION_MANIFEST_PATH,
        path=manifest_path,
        plan=plan,
    )
    configured_key_path = ensure_generated_path(
        env_file,
        env,
        key=GENERATED_DECISION_ATTESTATION_KEY_PATH,
        path=key_path,
        plan=plan,
    )
    env[GENERATED_DECISION_MANIFEST_PATH] = configured_manifest_path
    env[GENERATED_DECISION_ATTESTATION_KEY_PATH] = configured_key_path
    if args.mode == "docker":
        ensure_docker_bind_identity(env_file, env, plan)

    context = build_decision_context(env, args.mode, REPO_ROOT)
    decision_rendered = render_template(
        context,
        template_path=DECISION_TEMPLATE_PATH,
    )

    prepare_dedicated_home(decision_home, plan)
    write_exact_decision_config(decision_home, decision_rendered, plan)
    write_decision_runtime_artifacts(decision_home, plan)
    if args.dry_run:
        attestation_key = secrets.token_bytes(32)
        plan.act(f"generate owner-only attestation key {key_path}")
    else:
        attestation_key = write_new_attestation_key(key_path)
        plan.act(f"keep owner-only attestation key {key_path}")

    profile_digest = _decision_profile_digest(
        rendered=decision_rendered,
        model=model,
        provider=provider,
        api_key=decision_api_key,
        persisted_path=(profile_path if not args.dry_run else None),
    )
    public_origin, update_public_origin = resolve_decision_public_origin(
        env,
        mode=args.mode,
    )
    if args.mode == "docker":
        runtime_home = Path("/opt/data")
        runtime_vendor_root = Path("/opt/hermes")
        launch_argv = (
            "/opt/hermes/.venv/bin/python",
            "-m",
            "hermes_cli.main",
            "gateway",
            "run",
        )
    else:
        runtime_home = decision_home.expanduser().resolve()
        runtime_vendor_root = VENDOR_HERMES.resolve()
        launch_argv = (
            str(NATIVE_DECISION_VENV / "bin" / "python"),
            "-m",
            "hermes_cli.main",
            "gateway",
            "run",
        )
    if update_public_origin:
        plan.act(
            "set HEALTHMES_DECISION_HERMES_BASE_URL in "
            f"{env_file} to {public_origin}"
        )
        if not args.dry_run:
            upsert_env_var(
                env_file,
                "HEALTHMES_DECISION_HERMES_BASE_URL",
                public_origin,
            )
    internal_origin = (
        f"http://{context['decision_hermes_host']}:"
        f"{context['decision_hermes_port']}"
    )
    if args.dry_run:
        home_artifact_sha256 = {
            "config.yaml": hashlib.sha256(
                decision_rendered.encode("utf-8")
            ).hexdigest(),
            **{
                name: hashlib.sha256(content.encode("utf-8")).hexdigest()
                for name, content in DECISION_HOME_ARTIFACT_CONTENT.items()
            },
        }
    else:
        home_artifact_sha256 = runtime_home_artifact_sha256(
            decision_home
        )
    provider_environment = {
        name: value
        for name, value in env.items()
        if name in HERMES_RUNTIME_PROVIDER_ENV_NAMES and value
    }
    manifest = build_runtime_manifest(
        profile_bytes=decision_rendered.encode("utf-8"),
        profile_semantic_digest=profile_digest,
        model=model,
        provider=provider,
        api_key=decision_api_key,
        attestation_key=attestation_key,
        hermes_home=runtime_home,
        public_origin=public_origin,
        internal_origin=internal_origin,
        vendor_root=runtime_vendor_root,
        launch_argv=launch_argv,
        home_artifact_sha256=home_artifact_sha256,
        provider_environment=provider_environment,
        vendor_fingerprint_source=VENDOR_HERMES,
    )
    write_prepared_runtime_manifest(
        manifest_path,
        manifest,
        plan,
        verify_execution_artifacts=args.mode == "native",
        refresh_runtime_seal=args.refresh_runtime_seal,
    )

    plan.act(
        "leave general Hermes config, Telegram, webhook, skills, and unowned "
        "cron jobs untouched; do not install new HealthMes reasoning there"
    )

    plan.report()
    print(
        "[bootstrap] decision HERMES_HOME: "
        f"{decision_home} (profile: {decision_home / 'config.yaml'})"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
