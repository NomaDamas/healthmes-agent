#!/usr/bin/env python3
"""Machine-readable setup bridge used by the macOS companion.

The script intentionally uses only the Python standard library: a stock Mac
can run it before ``uv sync`` creates the managed HealthMes environment.
Existing lifecycle ownership stays in ``scripts/healthmes_local.sh``; this
adapter adds private configuration, Tailscale discovery, one-time pairing
grants, and stable JSONL progress events for the app.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

SCHEMA = "healthmes.setup.v1"
GRANT_SCHEMA = "healthmes.pairing-grant.v1"
GRANT_TTL_SECONDS = 5 * 60
SUPPORTED_ACTIONS = (
    "install",
    "pair",
    "repair",
    "update",
    "diagnostics",
    "uninstall",
)


@dataclass(frozen=True)
class SetupEvent:
    schema: str
    action: str
    step: str
    state: Literal["ready", "running", "action_required", "failed"]
    message: str
    detail: str | None = None
    expires_at: int | None = None


@dataclass(frozen=True)
class RuntimeConfig:
    root: Path
    env_file: Path
    data_dir: Path
    port: int
    api_token: str
    public_base_url: str


class SetupFailure(RuntimeError):
    def __init__(self, step: str, message: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.step = step
        self.message = message
        self.detail = detail


class Reporter:
    def __init__(self, action: str, *, json_output: bool) -> None:
        self.action = action
        self.json_output = json_output

    def emit(
        self,
        step: str,
        state: Literal["ready", "running", "action_required", "failed"],
        message: str,
        *,
        detail: str | None = None,
        expires_at: int | None = None,
    ) -> None:
        event = SetupEvent(
            schema=SCHEMA,
            action=self.action,
            step=step,
            state=state,
            message=message,
            detail=detail,
            expires_at=expires_at,
        )
        if self.json_output:
            print(
                json.dumps(
                    asdict(event),
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
                flush=True,
            )
            return
        suffix = f" ({detail})" if detail else ""
        print(f"[{state}] {message}{suffix}", flush=True)


def _repo_root() -> Path:
    override = os.environ.get("HEALTHMES_REPO_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _resolved_env(root: Path) -> dict[str, str]:
    values = _parse_env_file(root / ".env")
    values.update(_parse_env_file(root / ".env.local"))
    values.update(
        {
            key: value
            for key, value in os.environ.items()
            if key.startswith("HEALTHMES_") and value.strip()
        }
    )
    return values


def _upsert_env(path: Path, updates: dict[str, str]) -> None:
    lines = (
        path.read_text(encoding="utf-8").splitlines()
        if path.is_file()
        else ["# Managed by HealthMes setup."]
    )
    remaining = dict(updates)
    rewritten: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                rewritten.append(f"{key}={remaining.pop(key)}")
                continue
        rewritten.append(line)
    if remaining and rewritten and rewritten[-1] != "":
        rewritten.append("")
    for key, value in remaining.items():
        rewritten.append(f"{key}={value}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(rewritten).rstrip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _valid_port(raw: str) -> int:
    try:
        port = int(raw)
    except ValueError as exc:
        raise SetupFailure(
            "configuration",
            "HealthMes port is invalid.",
            detail="HEALTHMES_PORT must be an integer from 1 through 65535.",
        ) from exc
    if not 1 <= port <= 65_535:
        raise SetupFailure(
            "configuration",
            "HealthMes port is invalid.",
            detail="HEALTHMES_PORT must be an integer from 1 through 65535.",
        )
    return port


def _data_dir(root: Path, raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve()


def _secure_https_origin(raw: str) -> str | None:
    candidate = raw.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    return candidate


def _tailscale_binary() -> str | None:
    override = os.environ.get("HEALTHMES_TAILSCALE_BINARY")
    if override is not None:
        candidate = override.strip()
        if not candidate:
            return None
        discovered = shutil.which(candidate)
        if discovered:
            return discovered
        path = Path(candidate).expanduser()
        return str(path) if path.is_file() else None

    discovered = shutil.which("tailscale")
    if discovered:
        return discovered
    bundled = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
    return str(bundled) if bundled.is_file() else None


def _tailscale_dns_name(binary: str) -> str | None:
    try:
        result = subprocess.run(
            [binary, "status", "--json"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if str(payload.get("BackendState", "")).casefold() != "running":
        return None
    self_node = payload.get("Self")
    if not isinstance(self_node, dict):
        return None
    dns_name = str(self_node.get("DNSName", "")).strip().rstrip(".")
    return dns_name or None


def _prepare_phone_origin(
    config: RuntimeConfig,
    reporter: Reporter,
) -> tuple[str | None, RuntimeConfig]:
    configured = _secure_https_origin(config.public_base_url)
    if configured is not None:
        reporter.emit(
            "tailscale",
            "ready",
            "Secure iPhone route is configured.",
            detail=configured,
        )
        return configured, config

    binary = _tailscale_binary()
    if binary is None:
        reporter.emit(
            "tailscale",
            "action_required",
            "Install Tailscale to connect iPhone.",
            detail="Install Tailscale on Mac and iPhone, then sign in to the same account.",
        )
        return None, config
    dns_name = _tailscale_dns_name(binary)
    if dns_name is None:
        reporter.emit(
            "tailscale",
            "action_required",
            "Sign in to Tailscale to connect iPhone.",
            detail="Tailscale is installed but its local backend is not running and signed in.",
        )
        return None, config

    origin = f"https://{dns_name}"
    try:
        result = subprocess.run(
            [
                binary,
                "serve",
                "--bg",
                "--yes",
                f"http://127.0.0.1:{config.port}",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        reporter.emit(
            "tailscale",
            "action_required",
            "Tailscale did not finish publishing the HealthMes route.",
            detail="Open Tailscale, confirm it is connected, then try again.",
        )
        return None, config
    except OSError as exc:
        reporter.emit(
            "tailscale",
            "action_required",
            "Tailscale could not publish the HealthMes route.",
            detail=str(exc),
        )
        return None, config
    if result.returncode != 0:
        reporter.emit(
            "tailscale",
            "action_required",
            "Tailscale could not publish the HealthMes route.",
            detail=_last_output_line(result.stdout),
        )
        return None, config
    _upsert_env(config.env_file, {"HEALTHMES_PUBLIC_BASE_URL": origin})
    updated = RuntimeConfig(
        root=config.root,
        env_file=config.env_file,
        data_dir=config.data_dir,
        port=config.port,
        api_token=config.api_token,
        public_base_url=origin,
    )
    reporter.emit(
        "tailscale",
        "ready",
        "Secure iPhone route is ready.",
        detail=origin,
    )
    return origin, updated


def _ensure_runtime_config(root: Path, reporter: Reporter) -> RuntimeConfig:
    env_file = root / ".env"
    values = _resolved_env(root)
    token = values.get("HEALTHMES_API_TOKEN", "").strip()
    if not token:
        token = secrets.token_urlsafe(32)
    port = _valid_port(values.get("HEALTHMES_PORT", "8100"))
    public_base_url = values.get(
        "HEALTHMES_PUBLIC_BASE_URL",
        f"http://127.0.0.1:{port}",
    ).strip()
    updates = {
        "HEALTHMES_API_TOKEN": token,
        "HEALTHMES_HOST": values.get("HEALTHMES_HOST", "127.0.0.1").strip()
        or "127.0.0.1",
        "HEALTHMES_PORT": str(port),
        "HEALTHMES_PUBLIC_BASE_URL": public_base_url,
    }
    _upsert_env(env_file, updates)
    data_dir = _data_dir(
        root,
        values.get("HEALTHMES_DATA_DIR", "data"),
    )
    reporter.emit(
        "configuration",
        "ready",
        "Private HealthMes configuration is ready.",
        detail=str(env_file),
    )
    return RuntimeConfig(
        root=root,
        env_file=env_file,
        data_dir=data_dir,
        port=port,
        api_token=token,
        public_base_url=public_base_url,
    )


def _last_output_line(output: str, *, fallback: str | None = None) -> str | None:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1] if lines else fallback


def _require_tool(
    name: str,
    reporter: Reporter,
    *,
    action_required: str,
) -> str:
    path = shutil.which(name)
    if path:
        reporter.emit(
            f"tool_{name}",
            "ready",
            f"{name} is available.",
            detail=path,
        )
        return path
    reporter.emit(
        f"tool_{name}",
        "action_required",
        action_required,
    )
    raise SetupFailure(
        f"tool_{name}",
        action_required,
    )


def _run_lifecycle(
    config: RuntimeConfig,
    action: str,
    reporter: Reporter,
) -> None:
    lifecycle_action = {
        "install": "install",
        "repair": "install",
        "update": "update",
        "diagnostics": "status",
        "uninstall": "uninstall",
    }[action]
    script = config.root / "scripts" / "healthmes_local.sh"
    if not script.is_file():
        raise SetupFailure(
            "runtime",
            "HealthMes lifecycle script is missing.",
            detail=str(script),
        )
    reporter.emit(
        "runtime",
        "running",
        f"Running HealthMes {action}.",
    )
    result = subprocess.run(
        ["/bin/bash", str(script), lifecycle_action],
        cwd=config.root,
        env=os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode != 0:
        raise SetupFailure(
            "runtime",
            f"HealthMes {action} did not complete.",
            detail=_last_output_line(result.stdout),
        )
    reporter.emit(
        "runtime",
        "ready",
        f"HealthMes {action} completed.",
        detail=_last_output_line(result.stdout),
    )


def _write_grant(config: RuntimeConfig, base_url: str) -> tuple[str, int]:
    code = secrets.token_urlsafe(32)
    digest = hashlib.sha256(code.encode("ascii")).hexdigest()
    root = config.data_dir / "pairing-grants"
    active = root / "active"
    for directory in (
        root,
        active,
        root / "consumed",
        root / "expired",
    ):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            directory.chmod(0o700)
    issued_at = time.time()
    expires_at = int(issued_at + GRANT_TTL_SECONDS)
    target = active / f"{digest}.json"
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema": GRANT_SCHEMA,
                    "base_url": base_url,
                    "issued_at": issued_at,
                    "expires_at": expires_at,
                },
                handle,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        if os.name != "nt":
            target.chmod(0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    pairing_url = (
        f"healthmes://pair?url={quote(base_url, safe='')}"
        f"&code={quote(code, safe='')}"
    )
    return pairing_url, expires_at


def _emit_pairing_grants(
    config: RuntimeConfig,
    reporter: Reporter,
    *,
    include_mac: bool,
    phone_origin: str | None,
) -> None:
    if include_mac:
        mac_url, mac_expiry = _write_grant(
            config,
            f"http://127.0.0.1:{config.port}",
        )
        reporter.emit(
            "pair_mac",
            "ready",
            "Pair this Mac app with the local HealthMes runtime.",
            detail=mac_url,
            expires_at=mac_expiry,
        )
    if phone_origin is None:
        reporter.emit(
            "pair_phone",
            "action_required",
            "iPhone pairing needs a reachable HTTPS route.",
            detail="Complete Tailscale setup, then generate a new QR.",
        )
        return
    phone_url, phone_expiry = _write_grant(config, phone_origin)
    reporter.emit(
        "pair_phone",
        "ready",
        "Scan this one-time QR with iPhone.",
        detail=phone_url,
        expires_at=phone_expiry,
    )


def _http_health(config: RuntimeConfig) -> tuple[bool, str]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{config.port}/health",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = response.read(4096).decode("utf-8", errors="replace")
            return response.status == 200, payload
    except (OSError, urllib.error.URLError) as exc:
        return False, str(exc)


def run(action: str, reporter: Reporter) -> int:
    root = _repo_root()
    if not (root / "pyproject.toml").is_file():
        raise SetupFailure(
            "repository",
            "The managed HealthMes repository is incomplete.",
            detail=str(root),
        )
    reporter.emit(
        "repository",
        "ready",
        "Managed HealthMes source is available.",
        detail=str(root),
    )
    config = _ensure_runtime_config(root, reporter)

    if action in {"install", "repair"}:
        _require_tool(
            "brew",
            reporter,
            action_required="Install Homebrew, then run setup again.",
        )
        _require_tool(
            "uv",
            reporter,
            action_required="Install uv, then run setup again.",
        )
        phone_origin, config = _prepare_phone_origin(config, reporter)
        _run_lifecycle(config, action, reporter)
        _emit_pairing_grants(
            config,
            reporter,
            include_mac=True,
            phone_origin=phone_origin,
        )
        return 0

    if action == "pair":
        phone_origin, config = _prepare_phone_origin(config, reporter)
        if phone_origin is None:
            _emit_pairing_grants(
                config,
                reporter,
                include_mac=False,
                phone_origin=None,
            )
            return 0
        _emit_pairing_grants(
            config,
            reporter,
            include_mac=False,
            phone_origin=phone_origin,
        )
        return 0

    _run_lifecycle(config, action, reporter)
    if action == "diagnostics":
        healthy, detail = _http_health(config)
        reporter.emit(
            "health",
            "ready" if healthy else "action_required",
            (
                "HealthMes HTTP service is reachable."
                if healthy
                else "HealthMes HTTP service is not reachable."
            ),
            detail=detail,
        )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="HealthMes macOS one-page setup adapter."
    )
    parser.add_argument("action", choices=SUPPORTED_ACTIONS)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one healthmes.setup.v1 JSON object per line.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    reporter = Reporter(args.action, json_output=args.json)
    try:
        return run(args.action, reporter)
    except SetupFailure as exc:
        reporter.emit(
            exc.step,
            "failed",
            exc.message,
            detail=exc.detail,
        )
        return 1
    except Exception as exc:
        reporter.emit(
            "unexpected",
            "failed",
            "HealthMes setup stopped unexpectedly.",
            detail=str(exc),
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
