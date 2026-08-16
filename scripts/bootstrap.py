#!/usr/bin/env python3
"""Bootstrap the dedicated Hermes Responses runtime for HealthMes.

The canonical deployment has one reasoning ingress:
``POST /v1/wellness-decisions``. This command writes only the isolated
``$HERMES_HOME/decision`` profile and its content-bound manifest and
attestation key. It deliberately leaves the legacy general Hermes home,
Telegram, webhooks, cron jobs, and installed Hermes skills untouched.

Run targets (HERMES_HOME resolution, highest precedence first):
  --hermes-home flag > HERMES_HOME env var > mode default
  (native: ~/.hermes, docker: <repo>/data/hermes).

Usage:
  uv run python scripts/bootstrap.py [--dry-run] [--mode native|docker]
      [--hermes-home PATH] [--env-file PATH]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from jinja2 import Environment, StrictUndefined

from healthmes.decision.hermes_profile import (
    HermesDecisionProfileAssertion,
)
from healthmes.hermes_runtime_identity import (
    HERMES_RUNTIME_PROVIDER_ENV_NAMES,
    build_runtime_manifest,
    runtime_home_artifact_sha256,
    write_new_attestation_key,
    write_runtime_manifest,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "config" / "hermes-config.yaml.tmpl"
DECISION_TEMPLATE_PATH = (
    REPO_ROOT / "config" / "hermes-decision-config.yaml.tmpl"
)
VENDOR_HERMES = REPO_ROOT / "vendor" / "hermes-agent"

# The one secret bootstrap may mint itself (shared HMAC between
# healthmes/engine/triggers.py and the Hermes webhook route).
GENERATED_SECRET_KEY = "HEALTHMES_HERMES_WEBHOOK_SECRET"
GENERATED_ADJUSTMENT_SECRET_KEY = "HEALTHMES_CALENDAR_ADJUSTMENT_SECRET"
GENERATED_DECISION_API_KEY = "HEALTHMES_DECISION_HERMES_API_KEY"
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

# Non-generatable credentials we can only warn about.
WARN_IF_MISSING = (
    "TELEGRAM_BOT_TOKEN",
    "OPEN_WEARABLES_API_KEY",
    "HEALTHMES_TELEGRAM_OWNER_USER_ID",
    "HEALTHMES_TELEGRAM_OWNER_CHAT_ID",
)

# Briefing state-snapshot script (docs/PLAN.md section 4 `script:` context
# injection). The vendor scheduler resolves relative script paths under
# $HERMES_HOME/scripts/ and rejects anything outside it, so bootstrap copies
# the repo script there; the sidecar JSON feeds it the healthmes base URL
# (env HEALTHMES_BASE_URL still wins at run time).
SNAPSHOT_SCRIPT_NAME = "healthmes_briefing_snapshot.py"
SNAPSHOT_SCRIPT_SOURCE = REPO_ROOT / "scripts" / SNAPSHOT_SCRIPT_NAME
SNAPSHOT_SIDECAR_NAME = "healthmes_snapshot.json"
HEALTHMES_CRON_ORIGIN = {
    "source": "healthmes-bootstrap",
    "version": 1,
}
HEALTHMES_MORNING_CRON_NAME = "healthmes-morning-plan"
HEALTHMES_MANAGED_CRON_FIELDS = (
    "prompt",
    "schedule",
    "skills",
    "deliver",
    "script",
)

# Every variable the template references. Optional ones render as "" so the
# template's `| default(..., true)` fallbacks kick in under StrictUndefined.
TEMPLATE_KEYS = (
    "telegram_bot_token",
    "telegram_home_chat_id",
    "telegram_home_chat_name",
    "telegram_allowed_user_ids",
    "telegram_owner_user_id",
    "telegram_owner_chat_id",
    "hermes_webhook_port",
    "hermes_webhook_secret",
    "healthmes_alert_prompt",
    "hermes_model",
    "hermes_provider",
    "hermes_model_base_url",
    "hermes_model_api_key",
    "ow_mcp_dir",
    "ow_base_url",
    "ow_api_key",
    "ow_mcp_venv_dir",
    "ow_mcp_uv_cache_dir",
    "healthmes_mcp_url",
    "healthmes_api_token",
    "decision_hermes_host",
    "decision_hermes_port",
    "decision_hermes_api_key",
    "decision_hermes_model",
    "decision_hermes_provider",
    "decision_hermes_model_base_url",
    "decision_hermes_model_api_key",
)

# ---------------------------------------------------------------------------
# Cron briefings (docs/PLAN.md section 4 "time-driven briefings").
# Keys are keyword arguments of vendor cron/jobs.py::create_job — the glue
# test suite asserts this against inspect.signature(create_job).
# ---------------------------------------------------------------------------

# Each job pre-injects the state snapshot (script stdout) as prompt context;
# the prompts therefore say "the snapshot above" and keep MCP for verification.
BRIEFING_JOBS: tuple[dict[str, Any], ...] = (
    {
        "name": HEALTHMES_MORNING_CRON_NAME,
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
)

LEGACY_HEALTHMES_MORNING_CRON_FINGERPRINTS: tuple[dict[str, Any], ...] = (
    {
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

_CRON_FIELD_RE = re.compile(r"^[\d\*\-,/]+$")  # same shape check as parse_schedule


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


def ensure_webhook_secret(env_file: Path, env: dict[str, str], plan: Plan) -> str:
    """Return the webhook HMAC secret, minting + persisting it if missing."""
    existing = env.get(GENERATED_SECRET_KEY, "").strip()
    if existing:
        return existing
    generated = secrets.token_hex(32)
    plan.act(f"generate {GENERATED_SECRET_KEY} into {env_file}")
    if not plan.dry_run:
        upsert_env_var(env_file, GENERATED_SECRET_KEY, generated)
    return generated


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


def mode_defaults(mode: str, repo_root: Path, env: dict[str, str]) -> dict[str, str]:
    """Per-mode default values (env vars always take precedence)."""
    if mode == "docker":
        return {
            "ow_mcp_dir": "/opt/vendor/open-wearables-mcp",
            "ow_mcp_venv_dir": "/opt/data/ow-mcp-venv",
            "ow_mcp_uv_cache_dir": "/opt/data/uv-cache",
            "ow_base_url": "http://ow-backend:8000",
            "healthmes_mcp_url": "http://healthmes:8100/mcp",
        }
    healthmes_port = env.get("HEALTHMES_PORT", "8100").strip() or "8100"
    return {
        "ow_mcp_dir": str(repo_root / "vendor" / "open-wearables" / "mcp"),
        "ow_mcp_venv_dir": str(repo_root / "data" / "ow-mcp-venv"),
        "ow_mcp_uv_cache_dir": str(repo_root / "data" / "uv-cache"),
        "ow_base_url": env.get("HEALTHMES_OW_BASE_URL", "").strip()
        or "http://localhost:8000",
        "healthmes_mcp_url": f"http://localhost:{healthmes_port}/mcp",
    }


def build_decision_context(
    env: Mapping[str, str],
    mode: str,
    repo_root: Path,
) -> dict[str, str]:
    """Build only the values consumed by the dedicated decision profile."""

    defaults = mode_defaults(mode, repo_root, dict(env))
    return {
        "healthmes_mcp_url": env.get("HEALTHMES_MCP_URL", "").strip()
        or defaults["healthmes_mcp_url"],
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


def build_context(
    env: dict[str, str],
    mode: str,
    repo_root: Path,
    webhook_secret: str,
) -> dict[str, Any]:
    """Template context: every TEMPLATE_KEYS entry is present (maybe '')."""
    defaults = mode_defaults(mode, repo_root, env)
    owner_user_id = env.get("HEALTHMES_TELEGRAM_OWNER_USER_ID", "").strip()
    owner_chat_id = env.get("HEALTHMES_TELEGRAM_OWNER_CHAT_ID", "").strip()
    if "*" in {owner_user_id, owner_chat_id}:
        raise ValueError("Telegram owner user/chat ids must be explicit; '*' is forbidden")
    context: dict[str, Any] = {
        "telegram_bot_token": env.get("TELEGRAM_BOT_TOKEN", "").strip(),
        "telegram_home_chat_id": env.get("TELEGRAM_HOME_CHAT_ID", "").strip(),
        "telegram_home_chat_name": env.get("TELEGRAM_HOME_CHAT_NAME", "").strip(),
        "telegram_allowed_user_ids": [owner_user_id] if owner_user_id else [],
        "telegram_owner_user_id": owner_user_id,
        "telegram_owner_chat_id": owner_chat_id,
        "hermes_webhook_port": env.get("HERMES_WEBHOOK_PORT", "").strip(),
        "hermes_webhook_secret": webhook_secret,
        "healthmes_alert_prompt": env.get("HEALTHMES_ALERT_PROMPT", "").strip(),
        # LLM selection (optional). Omitted -> the vendor auto-defaults to
        # Anthropic/Claude. Any of the ~29 vendor model-provider plugins
        # (vendor/hermes-agent/plugins/model-providers/) can be selected;
        # keys land in the root `model:` section that both the gateway and
        # the `hermes chat` CLI resolve (hermes_cli/config.py
        # ::_normalize_root_model_keys — model.default/provider/base_url).
        "hermes_model": env.get("HERMES_MODEL", "").strip(),
        "hermes_provider": env.get("HERMES_PROVIDER", "").strip(),
        "hermes_model_base_url": env.get("HERMES_MODEL_BASE_URL", "").strip(),
        # Providers with a fixed key env (ANTHROPIC_API_KEY, XAI_API_KEY,
        # GEMINI_API_KEY/GOOGLE_API_KEY) do not need this; the `custom`
        # OpenAI-compatible provider reads model.api_key from config.
        "hermes_model_api_key": env.get("HERMES_MODEL_API_KEY", "").strip(),
        "ow_mcp_dir": env.get("OW_MCP_DIR", "").strip() or defaults["ow_mcp_dir"],
        "ow_base_url": env.get("OW_BASE_URL", "").strip() or defaults["ow_base_url"],
        "ow_api_key": (
            env.get("OPEN_WEARABLES_API_KEY", "").strip()
            or env.get("HEALTHMES_OW_API_KEY", "").strip()
        ),
        "ow_mcp_venv_dir": env.get("OW_MCP_VENV_DIR", "").strip()
        or defaults["ow_mcp_venv_dir"],
        "ow_mcp_uv_cache_dir": env.get("OW_MCP_UV_CACHE_DIR", "").strip()
        or defaults["ow_mcp_uv_cache_dir"],
        "healthmes_mcp_url": env.get("HEALTHMES_MCP_URL", "").strip()
        or defaults["healthmes_mcp_url"],
        # Bearer token of the healthmes surface (REST + /mcp). When set, the
        # rendered MCP registration carries the Authorization header so the
        # agent keeps reaching its Layer-B tools behind auth.
        "healthmes_api_token": env.get("HEALTHMES_API_TOKEN", "").strip(),
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
    for key in TEMPLATE_KEYS:
        context.setdefault(key, "")
    return context


def render_template(context: dict[str, Any], template_path: Path = TEMPLATE_PATH) -> str:
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
# Briefing snapshot script (docs/PLAN.md section 4 `script:` context injection)
# ---------------------------------------------------------------------------


def snapshot_base_url(context: dict[str, Any]) -> str:
    """HealthMes base URL for the snapshot sidecar, derived from the MCP URL.

    The template context already carries the mode-correct healthmes endpoint
    (http://localhost:8100/mcp native, http://healthmes:8100/mcp docker);
    the REST base is the same origin without the /mcp path.
    """
    mcp_url = str(context.get("healthmes_mcp_url", "")).strip()
    base = mcp_url[: -len("/mcp")] if mcp_url.endswith("/mcp") else mcp_url
    return (base or "http://localhost:8100").rstrip("/")


def install_snapshot_script(hermes_home: Path, context: dict[str, Any], plan: Plan) -> None:
    """Copy the snapshot script + base-URL sidecar into $HERMES_HOME/scripts/.

    A copy (not a symlink): the vendor path guard resolves symlinks and
    rejects scripts outside $HERMES_HOME/scripts/, and in docker mode the
    repo path is not mounted into the hermes container while ./data/hermes
    is. Idempotent by content comparison. Must run BEFORE cron registration
    so create_job's lifecycle guard scans the file that will actually run.
    """
    if not SNAPSHOT_SCRIPT_SOURCE.is_file():
        plan.warn(f"{SNAPSHOT_SCRIPT_SOURCE} is missing; briefing snapshot not installed")
        return
    scripts_dir = hermes_home / "scripts"
    script_target = scripts_dir / SNAPSHOT_SCRIPT_NAME
    source_text = SNAPSHOT_SCRIPT_SOURCE.read_text(encoding="utf-8")
    sidecar_target = scripts_dir / SNAPSHOT_SIDECAR_NAME
    sidecar: dict[str, str] = {"base_url": snapshot_base_url(context)}
    # The snapshot script must authenticate when the healthmes surface is
    # token-protected; the sidecar is chmod 600 inside $HERMES_HOME.
    api_token = str(context.get("healthmes_api_token", "")).strip()
    if api_token:
        sidecar["api_token"] = api_token
    sidecar_text = json.dumps(sidecar, indent=2, sort_keys=True) + "\n"

    for target, content, label in (
        (script_target, source_text, "briefing snapshot script"),
        (sidecar_target, sidecar_text, "snapshot base-url sidecar"),
    ):
        if target.is_file() and target.read_text(encoding="utf-8") == content:
            plan.act(f"keep {target} ({label} already up to date)")
            continue
        plan.act(f"write {target} ({label})")
        if not plan.dry_run:
            scripts_dir.mkdir(parents=True, exist_ok=True)
            _chmod_quiet(scripts_dir, 0o700)
            target.write_text(content, encoding="utf-8")
            _chmod_quiet(target, 0o600)


# ---------------------------------------------------------------------------
# Cron briefings
# ---------------------------------------------------------------------------


def _restore_env(key: str, previous: str | None) -> None:
    if previous is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = previous


def _resolve_hermes_timezone(hermes_home: Path) -> ZoneInfo | None:
    """The configured Hermes IANA timezone, or None for server-local time.

    Mirror of vendor/hermes-agent/hermes_time.py (_resolve_timezone_name +
    _get_zoneinfo): 1. HERMES_TIMEZONE env var, 2. ``timezone`` key of
    ``$HERMES_HOME/config.yaml``, 3. None. Invalid names fall back to None
    exactly like the vendor (which logs and never crashes on a bad string).
    """
    name = os.environ.get("HERMES_TIMEZONE", "").strip()
    if not name:
        config_path = hermes_home / "config.yaml"
        try:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            raw = config.get("timezone", "") if isinstance(config, dict) else ""
            name = raw.strip() if isinstance(raw, str) else ""
        except (OSError, yaml.YAMLError):
            name = ""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def _hermes_now(hermes_home: Path) -> datetime:
    """Timezone-aware "now" as the vendor scheduler would compute it.

    The vendor's create_job stamps created_at/next_run_at with
    hermes_time.now(), which honors the configured Hermes timezone; the
    payload fallback must match, or the first briefing fires on the wrong
    wall clock whenever the Hermes timezone differs from the system one
    (the vendor scheduler recomputes subsequent runs correctly).
    """
    tz = _resolve_hermes_timezone(hermes_home)
    if tz is not None:
        return datetime.now(tz)
    return datetime.now().astimezone()


def _import_vendor_cron_jobs(hermes_home: Path) -> Any | None:
    """Import vendor cron.jobs bound to *hermes_home*, or None.

    The module resolves HERMES_DIR from the HERMES_HOME env var at import
    time, so the env var is set first (and restored when the import is
    unusable). Returns None when the import fails, when croniter is
    unavailable (cron-expression schedules require it), or when an
    already-imported copy is bound to a different home. On success the
    HERMES_HOME env var stays pointed at *hermes_home* — vendor helpers
    (e.g. the timezone lookup in hermes_time.now) re-read it at call time.
    """
    if not (VENDOR_HERMES / "cron" / "jobs.py").is_file():
        return None
    previous_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(hermes_home)
    vendor_path = str(VENDOR_HERMES)
    inserted = vendor_path not in sys.path
    if inserted:
        sys.path.insert(0, vendor_path)
    try:
        from cron import jobs as vendor_jobs  # type: ignore[import-not-found]
    except Exception:
        _restore_env("HERMES_HOME", previous_home)
        return None
    finally:
        if inserted:
            try:
                sys.path.remove(vendor_path)
            except ValueError:
                pass
    jobs_file = Path(getattr(vendor_jobs, "JOBS_FILE", ""))
    usable = getattr(vendor_jobs, "HAS_CRONITER", False)
    try:
        usable = usable and jobs_file.parent.parent.resolve() == hermes_home.resolve()
    except OSError:
        usable = False
    if not usable:
        # Either croniter is missing or a previously-imported copy is bound
        # to a different home; the payload fallback takes over.
        _restore_env("HERMES_HOME", previous_home)
        return None
    return vendor_jobs


def _next_cron_run(expr: str, now: datetime) -> datetime:
    """Next fire time for the restricted cron shapes bootstrap registers.

    Supports ``M H * * *`` (daily) and ``M H * * D`` (weekly, D: 0=Sunday,
    croniter convention). Only used by the payload fallback; the gateway's
    scheduler recomputes subsequent runs with croniter.
    """
    fields = expr.split()
    if len(fields) != 5 or not all(_CRON_FIELD_RE.match(f) for f in fields):
        raise ValueError(f"unsupported cron expression: {expr!r}")
    minute, hour, dom, month, dow = fields
    if dom != "*" or month != "*":
        raise ValueError(f"unsupported cron expression (day/month field): {expr!r}")
    candidate = now.replace(
        hour=int(hour), minute=int(minute), second=0, microsecond=0
    )
    if dow == "*":
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    python_weekday = (int(dow) - 1) % 7  # cron 0=Sunday -> python 6
    days_ahead = (python_weekday - candidate.weekday()) % 7
    candidate += timedelta(days=days_ahead)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def build_fallback_job(
    *,
    prompt: str,
    schedule: str,
    name: str,
    deliver: str,
    skills: list[str],
    script: str | None = None,
    origin: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The exact job dict vendor create_job() persists for these arguments.

    Mirrors vendor/hermes-agent/cron/jobs.py::create_job for the subset
    bootstrap uses (agent job, cron-expression schedule, context script, no
    overrides). ``attach_to_session`` is omitted, matching create_job's
    behavior when the argument is not explicitly set. Key parity with the
    real function is asserted by tests/glue/test_cron_payload.py.

    Callers should pass ``now=_hermes_now(hermes_home)`` so timestamps honor
    the configured Hermes timezone like the vendor's hermes_time.now(); the
    default only covers the unconfigured (server-local) case.
    """
    now = now or datetime.now().astimezone()
    normalized_skills = [s.strip() for s in skills if s and s.strip()]
    normalized_script = (script.strip() if isinstance(script, str) else None) or None
    return {
        "id": uuid.uuid4().hex[:12],
        "name": name or prompt[:50].strip(),
        "prompt": prompt,
        "skills": normalized_skills,
        "skill": normalized_skills[0] if normalized_skills else None,
        "model": None,
        "provider": None,
        "provider_snapshot": None,
        "model_snapshot": None,
        "base_url": None,
        "script": normalized_script,
        "no_agent": False,
        "context_from": None,
        "schedule": {"kind": "cron", "expr": schedule, "display": schedule},
        "schedule_display": schedule,
        "repeat": {"times": None, "completed": 0},
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "paused_reason": None,
        "created_at": now.isoformat(),
        "next_run_at": _next_cron_run(schedule, now).isoformat(),
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
        "last_delivery_error": None,
        "deliver": deliver,
        "origin": dict(origin) if origin is not None else None,
        "enabled_toolsets": None,
        "workdir": None,
    }


def _load_jobs_envelope(jobs_file: Path) -> list[dict[str, Any]]:
    """Existing jobs from jobs.json ({"jobs": [...]} or legacy bare list)."""
    if not jobs_file.is_file():
        return []
    try:
        data = json.loads(jobs_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(data, dict):
        jobs = data.get("jobs", [])
        return jobs if isinstance(jobs, list) else []
    if isinstance(data, list):
        return data
    return []


def _write_jobs_envelope(
    jobs_file: Path, jobs: list[dict[str, Any]], now: datetime | None = None
) -> None:
    """Atomically write the vendor jobs.json envelope (save_jobs shape)."""
    jobs_file.parent.mkdir(parents=True, exist_ok=True)
    _chmod_quiet(jobs_file.parent, 0o700)
    now = now or datetime.now().astimezone()
    payload = {"jobs": jobs, "updated_at": now.isoformat()}
    fd, tmp_path = tempfile.mkstemp(dir=str(jobs_file.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(tmp_path, jobs_file)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    _chmod_quiet(jobs_file, 0o600)


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
        "prompt": str(job.get("prompt") or ""),
        "schedule": _cron_schedule_expr(job),
        "skills": _cron_skills(job),
        "deliver": job.get("deliver"),
        "script": job.get("script"),
    }


def _is_healthmes_managed_cron(
    existing: Mapping[str, Any], desired: Mapping[str, Any]
) -> bool:
    if str(existing.get("name") or "") != desired["name"]:
        return False
    origin = existing.get("origin")
    if origin is not None:
        return (
            isinstance(origin, Mapping)
            and origin.get("source") == HEALTHMES_CRON_ORIGIN["source"]
        )
    declaration = _cron_managed_declaration(existing)
    return any(
        declaration == fingerprint
        for fingerprint in LEGACY_HEALTHMES_MORNING_CRON_FINGERPRINTS
    )


def _managed_cron_updates(
    existing: Mapping[str, Any], desired: Mapping[str, Any]
) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if str(existing.get("prompt") or "") != desired["prompt"]:
        updates["prompt"] = desired["prompt"]
    if _cron_schedule_expr(existing) != desired["schedule"]:
        updates["schedule"] = desired["schedule"]
    if _cron_skills(existing) != desired["skills"]:
        updates["skills"] = list(desired["skills"])
    if existing.get("deliver") != desired["deliver"]:
        updates["deliver"] = desired["deliver"]
    if existing.get("script") != desired.get("script"):
        updates["script"] = desired.get("script")
    if existing.get("origin") != HEALTHMES_CRON_ORIGIN:
        updates["origin"] = dict(HEALTHMES_CRON_ORIGIN)
    return updates


def _apply_fallback_cron_updates(
    existing: Mapping[str, Any],
    updates: Mapping[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    updated = {**existing, **updates}
    if "skills" in updates:
        skills = list(updates["skills"])
        updated["skills"] = skills
        updated["skill"] = skills[0] if skills else None
    if "schedule" in updates:
        schedule = str(updates["schedule"])
        updated["schedule"] = {
            "kind": "cron",
            "expr": schedule,
            "display": schedule,
        }
        updated["schedule_display"] = schedule
        if updated.get("state") != "paused":
            updated["next_run_at"] = _next_cron_run(schedule, now).isoformat()
    return updated


def register_cron_jobs(hermes_home: Path, plan: Plan) -> str:
    """Create BRIEFING_JOBS and reconcile the HealthMes-owned morning job.

    The morning job is updated only when a HealthMes ownership marker or a
    legacy HealthMes-specific fingerprint is present. Runtime state and job
    identity remain untouched; only the managed declaration fields drift.
    """
    jobs_file = hermes_home / "cron" / "jobs.json"
    existing_jobs = _load_jobs_envelope(jobs_file)
    jobs_by_name: dict[str, list[dict[str, Any]]] = {}
    for existing in existing_jobs:
        jobs_by_name.setdefault(str(existing.get("name") or ""), []).append(existing)

    missing: list[dict[str, Any]] = []
    drifted: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for desired in BRIEFING_JOBS:
        matches = jobs_by_name.get(desired["name"], [])
        if not matches:
            missing.append(desired)
            continue
        if len(matches) > 1:
            plan.warn(
                f"cron job name '{desired['name']}' is ambiguous; "
                "leaving all matching jobs unchanged"
            )
            continue
        existing = matches[0]
        if desired["name"] != HEALTHMES_MORNING_CRON_NAME:
            plan.act(f"keep cron job '{desired['name']}' (already registered)")
            continue
        job_id = str(existing.get("id") or "").strip()
        id_matches = [
            job
            for job in existing_jobs
            if str(job.get("id") or "").strip() == job_id
        ]
        if not job_id or len(id_matches) != 1:
            plan.warn(
                f"cron job '{desired['name']}' has a missing or ambiguous id; "
                "leaving it unchanged"
            )
            continue
        if not _is_healthmes_managed_cron(existing, desired):
            plan.warn(
                f"cron job '{desired['name']}' is not HealthMes-managed; "
                "leaving it unchanged"
            )
            continue
        updates = _managed_cron_updates(existing, desired)
        if updates:
            drifted.append((existing, desired, updates))
        else:
            plan.act(f"keep cron job '{desired['name']}' (already current)")

    if not missing and not drifted:
        return "no-op"

    vendor_jobs = _import_vendor_cron_jobs(hermes_home)
    method = "vendor-create_job" if vendor_jobs is not None else "payload-fallback"

    for existing, desired, updates in drifted:
        managed_fields = sorted(set(updates) & set(HEALTHMES_MANAGED_CRON_FIELDS))
        plan.act(
            f"update cron job '{desired['name']}' "
            f"(id={existing.get('id')}, fields={managed_fields}) via {method}"
        )
    for job in missing:
        plan.act(
            f"register cron job '{job['name']}' ({job['schedule']}, "
            f"skills={job['skills']}, deliver={job['deliver']}) via {method}"
        )
    if plan.dry_run:
        return method

    if vendor_jobs is not None:
        for existing, _desired, updates in drifted:
            if vendor_jobs.update_job(str(existing.get("id") or ""), updates) is None:
                raise RuntimeError(
                    f"cron job disappeared during update: {existing.get('id')}"
                )
        for job in missing:
            vendor_jobs.create_job(
                prompt=job["prompt"],
                schedule=job["schedule"],
                name=job["name"],
                deliver=job["deliver"],
                skills=list(job["skills"]),
                script=job.get("script"),
                origin=dict(HEALTHMES_CRON_ORIGIN),
            )
        return method

    # Same clock the vendor's create_job would use (hermes_time.now()):
    # honors HERMES_TIMEZONE / the config.yaml `timezone` key so the first
    # next_run_at lands on the configured wall clock, not the system one.
    now = _hermes_now(hermes_home)
    all_jobs = _load_jobs_envelope(jobs_file)
    updates_by_id = {
        str(existing.get("id") or ""): updates
        for existing, _desired, updates in drifted
    }
    all_jobs = [
        _apply_fallback_cron_updates(
            existing,
            updates_by_id[str(existing.get("id") or "")],
            now=now,
        )
        if str(existing.get("id") or "") in updates_by_id
        else existing
        for existing in all_jobs
    ]
    for job in missing:
        all_jobs.append(
            build_fallback_job(
                prompt=job["prompt"],
                schedule=job["schedule"],
                name=job["name"],
                deliver=job["deliver"],
                skills=list(job["skills"]),
                script=job.get("script"),
                origin=HEALTHMES_CRON_ORIGIN,
                now=now,
            )
        )
    _write_jobs_envelope(jobs_file, all_jobs, now=now)
    return method


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

    decision_api_key = ensure_decision_api_key(env_file, env, plan)
    env[GENERATED_DECISION_API_KEY] = decision_api_key
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
    plan.act(
        f"write content-bound runtime manifest {manifest_path} "
        f"({manifest.runtime_id})"
    )
    if not args.dry_run:
        write_runtime_manifest(manifest_path, manifest)

    plan.act(
        "leave the legacy general Hermes home untouched and do not install "
        "HealthMes Telegram, webhook, skill, MCP, or cron reasoning there"
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
