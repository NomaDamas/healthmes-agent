#!/usr/bin/env python3
"""Bootstrap the Hermes agent plane for HealthMes (docs/PLAN.md Phase 0).

Idempotent glue between this repo and a Hermes home directory. It never
touches ``vendor/``; the only artifacts land inside ``$HERMES_HOME`` (a
directory outside the vendor tree) and the repo-root ``.env``:

1. Render ``config/hermes-config.yaml.tmpl`` (Jinja2) from env/.env into
   ``$HERMES_HOME/config.yaml`` — the file both the gateway
   (vendor/hermes-agent/gateway/config.py::load_gateway_config) and the MCP
   client (vendor/hermes-agent/tools/mcp_tool.py via hermes_cli.config)
   read. If a config.yaml already exists, the rendered mapping is
   deep-merged over it (rendered keys win, unrelated user keys survive) and
   the previous file is backed up once.
2. Copy each ``skills/<name>/`` directory (must contain SKILL.md) into
   ``$HERMES_HOME/skills/<name>`` — the discovery path scanned by
   vendor/hermes-agent/tools/skills_tool.py (SKILLS_DIR = HERMES_HOME/skills).
   A copy, not a symlink: the vendor trust check resolves symlinks
   (skills_tool.py ``skill_md.resolve().relative_to(trusted)``) and logs a
   "skill file is outside the trusted skills directory" security warning on
   every skill load — every alert and briefing. Copies inside $HERMES_HOME
   resolve as trusted, and they work identically in docker mode where
   ./data/hermes is the only path mounted into the hermes container.
   Legacy symlinks from earlier bootstraps are migrated to copies.
3. Generate missing secrets into ``.env`` (currently the webhook HMAC
   secret shared between healthmes triggers and the Hermes webhook route).
4. Install the briefing state-snapshot script (scripts/
   healthmes_briefing_snapshot.py) into ``$HERMES_HOME/scripts/`` plus a
   sidecar JSON carrying the healthmes base URL — the vendor cron scheduler
   only runs ``script:`` files from inside that directory
   (cron/scheduler.py::_run_job_script path guard) and injects their stdout
   into the briefing prompt as context (docs/PLAN.md section 4).
5. Register the three cron briefings (morning plan 07:00, evening review
   21:30, weekly planning Sunday 18:00) against
   vendor/hermes-agent/cron/jobs.py::create_job, each with ``script=`` set
   to the installed snapshot. The vendor module is imported and called
   directly when importable AND croniter is available (cron-expression
   schedules require it); otherwise the exact job payload ``create_job``
   would produce is written to ``$HERMES_HOME/cron/jobs.json`` in the
   vendor's ``{"jobs": [...], "updated_at": ...}`` envelope. Fallback
   timestamps honor the Hermes timezone the same way the vendor does
   (HERMES_TIMEZONE env, then the config.yaml ``timezone`` key —
   vendor/hermes-agent/hermes_time.py).

Run targets (HERMES_HOME resolution, highest precedence first):
  --hermes-home flag > HERMES_HOME env var > mode default
  (native: ~/.hermes, docker: <repo>/data/hermes — the host side of the
  ``./data/hermes:/opt/data`` bind mount in docker-compose.yml).

Mode only changes *defaults*; explicit environment variables always win, so
no docker service hostname is ever hardcoded for the native path:

  variable                     native default                  docker default
  OW_MCP_DIR                   <repo>/vendor/open-wearables/mcp  /opt/vendor/open-wearables-mcp
  OW_MCP_VENV_DIR              <repo>/data/ow-mcp-venv           /opt/data/ow-mcp-venv
  OW_MCP_UV_CACHE_DIR          <repo>/data/uv-cache              /opt/data/uv-cache
  OW_BASE_URL                  http://localhost:8000             http://ow-backend:8000
  HEALTHMES_MCP_URL            http://localhost:<port>/mcp       http://healthmes:8100/mcp

Usage:
  uv run python scripts/bootstrap.py [--dry-run] [--mode native|docker]
      [--hermes-home PATH] [--env-file PATH] [--print-config]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from jinja2 import Environment, StrictUndefined

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "config" / "hermes-config.yaml.tmpl"
VENDOR_HERMES = REPO_ROOT / "vendor" / "hermes-agent"

# The one secret bootstrap may mint itself (shared HMAC between
# healthmes/engine/triggers.py and the Hermes webhook route).
GENERATED_SECRET_KEY = "HEALTHMES_HERMES_WEBHOOK_SECRET"

# Non-generatable credentials we can only warn about.
WARN_IF_MISSING = ("TELEGRAM_BOT_TOKEN", "OPEN_WEARABLES_API_KEY")

# Briefing state-snapshot script (docs/PLAN.md section 4 `script:` context
# injection). The vendor scheduler resolves relative script paths under
# $HERMES_HOME/scripts/ and rejects anything outside it, so bootstrap copies
# the repo script there; the sidecar JSON feeds it the healthmes base URL
# (env HEALTHMES_BASE_URL still wins at run time).
SNAPSHOT_SCRIPT_NAME = "healthmes_briefing_snapshot.py"
SNAPSHOT_SCRIPT_SOURCE = REPO_ROOT / "scripts" / SNAPSHOT_SCRIPT_NAME
SNAPSHOT_SIDECAR_NAME = "healthmes_snapshot.json"

# Every variable the template references. Optional ones render as "" so the
# template's `| default(..., true)` fallbacks kick in under StrictUndefined.
TEMPLATE_KEYS = (
    "telegram_bot_token",
    "telegram_home_chat_id",
    "telegram_home_chat_name",
    "telegram_allowed_user_ids",
    "hermes_webhook_port",
    "hermes_webhook_secret",
    "healthmes_alert_prompt",
    "ow_mcp_dir",
    "ow_base_url",
    "ow_api_key",
    "ow_mcp_venv_dir",
    "ow_mcp_uv_cache_dir",
    "healthmes_mcp_url",
    "healthmes_api_token",
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
        "name": "healthmes-morning-plan",
        "schedule": "0 7 * * *",
        "prompt": (
            "Morning briefing. A HealthMes state snapshot (open tasks, "
            "today's events, pending proposals, energy forecast) is injected "
            "above; use it as context and read today's readiness via the "
            "healthmes MCP tools, then propose today's block layout based "
            "on the energy picture. One message in the standard notification "
            "grammar."
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
            "for next week's goal dump. One message in the standard "
            "notification grammar."
        ),
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


def build_context(
    env: dict[str, str],
    mode: str,
    repo_root: Path,
    webhook_secret: str,
) -> dict[str, Any]:
    """Template context: every TEMPLATE_KEYS entry is present (maybe '')."""
    defaults = mode_defaults(mode, repo_root, env)
    allowed_raw = env.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    allowed_ids = [part.strip() for part in allowed_raw.split(",") if part.strip()]
    context: dict[str, Any] = {
        "telegram_bot_token": env.get("TELEGRAM_BOT_TOKEN", "").strip(),
        "telegram_home_chat_id": env.get("TELEGRAM_HOME_CHAT_ID", "").strip(),
        "telegram_home_chat_name": env.get("TELEGRAM_HOME_CHAT_NAME", "").strip(),
        "telegram_allowed_user_ids": allowed_ids,
        "hermes_webhook_port": env.get("HERMES_WEBHOOK_PORT", "").strip(),
        "hermes_webhook_secret": webhook_secret,
        "healthmes_alert_prompt": env.get("HEALTHMES_ALERT_PROMPT", "").strip(),
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
        "origin": None,
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


def register_cron_jobs(hermes_home: Path, plan: Plan) -> str:
    """Register BRIEFING_JOBS idempotently (matched by job name).

    Returns the method used: ``"vendor-create_job"`` (vendor module imported
    and called directly), ``"payload-fallback"`` (exact create_job payload
    written into the vendor jobs.json envelope), or ``"no-op"`` (all three
    briefings were already registered).
    """
    jobs_file = hermes_home / "cron" / "jobs.json"
    existing_names = {
        str(job.get("name", "")) for job in _load_jobs_envelope(jobs_file)
    }
    missing = [job for job in BRIEFING_JOBS if job["name"] not in existing_names]
    for job in BRIEFING_JOBS:
        if job["name"] in existing_names:
            plan.act(f"keep cron job '{job['name']}' (already registered)")
    if not missing:
        return "no-op"

    vendor_jobs = _import_vendor_cron_jobs(hermes_home)
    method = "vendor-create_job" if vendor_jobs is not None else "payload-fallback"

    for job in missing:
        plan.act(
            f"register cron job '{job['name']}' ({job['schedule']}, "
            f"skills={job['skills']}, deliver={job['deliver']}) via {method}"
        )
    if plan.dry_run:
        return method

    if vendor_jobs is not None:
        for job in missing:
            vendor_jobs.create_job(
                prompt=job["prompt"],
                schedule=job["schedule"],
                name=job["name"],
                deliver=job["deliver"],
                skills=list(job["skills"]),
                script=job.get("script"),
            )
        return method

    # Same clock the vendor's create_job would use (hermes_time.now()):
    # honors HERMES_TIMEZONE / the config.yaml `timezone` key so the first
    # next_run_at lands on the configured wall clock, not the system one.
    now = _hermes_now(hermes_home)
    all_jobs = _load_jobs_envelope(jobs_file)
    for job in missing:
        all_jobs.append(
            build_fallback_job(
                prompt=job["prompt"],
                schedule=job["schedule"],
                name=job["name"],
                deliver=job["deliver"],
                skills=list(job["skills"]),
                script=job.get("script"),
                now=now,
            )
        )
    _write_jobs_envelope(jobs_file, all_jobs, now=now)
    return method


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


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
            "Render the Hermes config, link HealthMes skills, mint missing "
            "secrets, and register the cron briefings (idempotent)."
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
            "(localhost endpoints, ~/.hermes); 'docker' targets the "
            "docker-compose stack (in-cluster endpoints, ./data/hermes). "
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
        "--print-config",
        action="store_true",
        help="Print the rendered config.yaml to stdout.",
    )
    parser.add_argument(
        "--skip-cron",
        action="store_true",
        help="Skip cron briefing registration.",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    plan = Plan(dry_run=args.dry_run)
    env_file = Path(args.env_file).expanduser()
    hermes_home = resolve_hermes_home(args, REPO_ROOT)
    env = resolve_env(env_file)

    for key in WARN_IF_MISSING:
        if not env.get(key, "").strip():
            plan.warn(
                f"{key} is not set (in {env_file} or the environment); the "
                f"rendered config will contain an empty value"
            )
    if not env.get("TELEGRAM_HOME_CHAT_ID", "").strip():
        # Without it the rendered config has neither telegram.home_channel nor
        # the alert route's deliver_extra.chat_id, so `deliver: telegram`
        # (cron briefings + webhook alerts) fails at send time with "No
        # chat_id or home channel" (vendor gateway/platforms/webhook.py)
        # until a home channel exists.
        plan.warn(
            "TELEGRAM_HOME_CHAT_ID is not set: cron briefings and webhook "
            "alerts have no Telegram delivery target and will fail at send "
            "time until you set it (re-run bootstrap) or send /sethome to "
            "the bot in your Telegram chat"
        )

    webhook_secret = ensure_webhook_secret(env_file, env, plan)
    context = build_context(env, args.mode, REPO_ROOT, webhook_secret)
    rendered = render_template(context)

    if args.print_config:
        print(rendered)

    write_config(hermes_home, rendered, plan)
    install_skills(REPO_ROOT, hermes_home, plan)
    # Before cron registration: the jobs reference this script by name and
    # create_job's lifecycle guard reads it from $HERMES_HOME/scripts/.
    install_snapshot_script(hermes_home, context, plan)
    if args.skip_cron:
        plan.act("skip cron registration (--skip-cron)")
    else:
        method = register_cron_jobs(hermes_home, plan)
        plan.act(f"cron registration method: {method}")

    plan.report()
    print(f"[bootstrap] HERMES_HOME: {hermes_home} (mode: {args.mode})")
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
