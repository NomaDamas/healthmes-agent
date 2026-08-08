#!/usr/bin/env python3
"""Observable, idempotent Mac setup wrapper for HealthMes.

This script is the typed boundary consumed by the macOS app. It delegates the
actual runtime work to ``healthmes_local.sh`` and ``dev_mac.sh`` so there is
still one installer implementation.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

SCHEMA = "healthmes.setup.v1"
REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_SCRIPT = REPO_ROOT / "scripts" / "healthmes_local.sh"
BOOTSTRAP_SCRIPT = REPO_ROOT / "scripts" / "bootstrap.py"
ENV_FILE = REPO_ROOT / ".env"
ENV_EXAMPLE = REPO_ROOT / ".env.example"


@dataclass(frozen=True, slots=True)
class SetupEvent:
    action: str
    step: str
    state: Literal[
        "pending",
        "running",
        "ready",
        "action_required",
        "failed",
    ]
    message: str
    detail: str | None = None
    expires_at: int | None = None
    schema: str = SCHEMA


class SetupFailure(RuntimeError):
    pass


def emit(event: SetupEvent, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(asdict(event), separators=(",", ":"), sort_keys=True))
    else:
        suffix = f" ({event.detail})" if event.detail else ""
        print(f"[{event.state}] {event.step}: {event.message}{suffix}")
    sys.stdout.flush()


def preflight() -> list[SetupEvent]:
    events: list[SetupEvent] = []
    if platform.system() != "Darwin":
        events.append(
            SetupEvent(
                "preflight",
                "platform",
                "failed",
                "HealthMes local install currently requires macOS.",
                platform.platform(),
            )
        )
        return events
    events.append(
        SetupEvent(
            "preflight",
            "platform",
            "ready",
            "Supported macOS detected.",
            f"{platform.mac_ver()[0]} · {platform.machine()}",
        )
    )
    for command, required in (
        ("bash", True),
        ("git", True),
        ("python3", True),
        ("brew", True),
        ("uv", False),
    ):
        path = shutil.which(command)
        if path:
            events.append(
                SetupEvent(
                    "preflight",
                    f"tool_{command}",
                    "ready",
                    f"{command} is available.",
                    path,
                )
            )
        else:
            events.append(
                SetupEvent(
                    "preflight",
                    f"tool_{command}",
                    "action_required" if required else "pending",
                    (
                        f"Install {command} before setup."
                        if required
                        else "uv will be installed through Homebrew."
                    ),
                )
            )
    free_bytes = shutil.disk_usage(REPO_ROOT).free
    events.append(
        SetupEvent(
            "preflight",
            "disk",
            "ready" if free_bytes >= 5 * 1024**3 else "action_required",
            (
                "Disk space is sufficient."
                if free_bytes >= 5 * 1024**3
                else "At least 5 GB of free space is required."
            ),
            str(free_bytes),
        )
    )
    return events


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ")
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _write_private_env(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        stream = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)
        raise


def _upsert_env(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replacement = f"{key}={value}"
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"export {key}="):
            lines[index] = replacement
            break
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["# Generated by HealthMes setup", replacement])
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_private_env(path, "\n".join(lines) + "\n")


def ensure_environment(*, json_output: bool, dry_run: bool) -> None:
    emit(
        SetupEvent(
            "install",
            "environment",
            "running",
            "Preparing a private local configuration.",
        ),
        json_output=json_output,
    )
    if dry_run:
        emit(
            SetupEvent(
                "install",
                "environment",
                "ready",
                "Dry-run completed for environment.",
            ),
            json_output=json_output,
        )
        return
    if not ENV_FILE.exists():
        if not ENV_EXAMPLE.is_file():
            raise SetupFailure(f"Missing environment template: {ENV_EXAMPLE}")
        shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
        ENV_FILE.chmod(0o600)
    env = _load_env(ENV_FILE)
    token = env.get("HEALTHMES_API_TOKEN", "").strip()
    if not token:
        _upsert_env(ENV_FILE, "HEALTHMES_API_TOKEN", secrets.token_urlsafe(32))
    elif len(token) < 32:
        raise SetupFailure("Existing HEALTHMES_API_TOKEN is shorter than 32 characters.")
    public_base_url = env.get("HEALTHMES_PUBLIC_BASE_URL", "").strip()
    port = env.get("HEALTHMES_PORT", "8100").strip() or "8100"
    if not public_base_url.lower().startswith("https://"):
        _upsert_env(ENV_FILE, "HEALTHMES_HOST", "127.0.0.1")
        _upsert_env(
            ENV_FILE,
            "HEALTHMES_PUBLIC_BASE_URL",
            f"http://127.0.0.1:{port}",
        )
    ENV_FILE.chmod(0o600)
    emit(
        SetupEvent(
            "install",
            "environment",
            "ready",
            (
                "Using the configured HTTPS instance URL."
                if public_base_url.lower().startswith("https://")
                else "Local runtime is limited to this Mac until an HTTPS URL is configured."
            ),
        ),
        json_output=json_output,
    )


def prepare_runtime(
    action: str,
    *,
    json_output: bool,
    dry_run: bool,
) -> None:
    checks = preflight()
    for event in checks:
        emit(event, json_output=json_output)
    blockers = [
        event
        for event in checks
        if event.state in {"failed", "action_required"}
    ]
    if blockers:
        raise SetupFailure(blockers[0].message)
    if shutil.which("uv") is None:
        run_command(
            action,
            "dependency_uv",
            ["brew", "install", "uv"],
            json_output=json_output,
            dry_run=dry_run,
        )
    ensure_environment(json_output=json_output, dry_run=dry_run)
    run_command(
        action,
        "bootstrap",
        [
            "uv",
            "run",
            "python",
            str(BOOTSTRAP_SCRIPT),
            "--mode",
            "native",
            "--env-file",
            str(ENV_FILE),
        ],
        json_output=json_output,
        dry_run=dry_run,
    )


def run_command(
    action: str,
    step: str,
    command: list[str],
    *,
    json_output: bool,
    dry_run: bool,
) -> None:
    emit(
        SetupEvent(action, step, "running", f"Running {step}."),
        json_output=json_output,
    )
    if dry_run:
        emit(
            SetupEvent(action, step, "ready", f"Dry-run completed for {step}."),
            json_output=json_output,
        )
        return
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = _last_nonempty_line(result.stderr) or _last_nonempty_line(result.stdout)
        emit(
            SetupEvent(action, step, "failed", f"{step} failed.", detail),
            json_output=json_output,
        )
        raise SetupFailure(detail or f"{step} failed")
    emit(
        SetupEvent(
            action,
            step,
            "ready",
            f"{step} completed.",
            _last_nonempty_line(result.stdout),
        ),
        json_output=json_output,
    )


def verify(*, json_output: bool) -> bool:
    emit(
        SetupEvent("verify", "health", "running", "Checking the HealthMes service."),
        json_output=json_output,
    )
    port = (
        os.environ.get("HEALTHMES_PORT")
        or _load_env(ENV_FILE).get("HEALTHMES_PORT")
        or "8100"
    )
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health",
            timeout=3,
        ) as response:
            healthy = response.status == 200
    except (OSError, urllib.error.URLError):
        healthy = False
    emit(
        SetupEvent(
            "verify",
            "health",
            "ready" if healthy else "action_required",
            (
                "HealthMes is reachable."
                if healthy
                else "HealthMes is not reachable; run Repair."
            ),
        ),
        json_output=json_output,
    )
    return healthy


def issue_pairing_links(*, json_output: bool) -> None:
    code = "\n".join(
        (
            "import json",
            "from healthmes.config import Settings",
            "from healthmes.pairing import issue_pairing_grant",
            "settings = Settings(_env_file='.env')",
            "mac = issue_pairing_grant(settings)",
            "phone = issue_pairing_grant(settings) "
            "if settings.public_base_url.startswith('https://') else None",
            "print(json.dumps({'mac': mac.deep_link, "
            "'mac_expires_at': mac.expires_at, "
            "'phone': phone.deep_link if phone else None, "
            "'phone_expires_at': phone.expires_at if phone else None}))",
        )
    )
    result = subprocess.run(
        ["uv", "run", "python", "-c", code],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SetupFailure(
            _last_nonempty_line(result.stderr) or "Could not create pairing grants."
        )
    try:
        payload = json.loads(result.stdout)
        mac_link = str(payload["mac"])
        mac_expires_at = int(payload["mac_expires_at"])
        phone_link = (
            str(payload["phone"])
            if payload.get("phone")
            else None
        )
        phone_expires_at = (
            int(payload["phone_expires_at"])
            if payload.get("phone_expires_at") is not None
            else None
        )
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SetupFailure("Pairing grant output was invalid.") from exc
    emit(
        SetupEvent(
            "install",
            "pair_mac",
            "ready",
            "One-time Mac pairing is ready.",
            mac_link if json_output else None,
            mac_expires_at,
        ),
        json_output=json_output,
    )
    if phone_link is not None:
        emit(
            SetupEvent(
                "install",
                "pair_phone",
                "ready",
                "Scan the one-time QR with iPhone.",
                phone_link if json_output else None,
                phone_expires_at,
            ),
            json_output=json_output,
        )
    else:
        emit(
            SetupEvent(
                "install",
                "pair_phone",
                "action_required",
                "Configure an HTTPS public URL to pair iPhone securely.",
            ),
            json_output=json_output,
        )


def install(*, json_output: bool, dry_run: bool) -> None:
    prepare_runtime(
        "install",
        json_output=json_output,
        dry_run=dry_run,
    )
    run_command(
        "install",
        "runtime_install",
        ["bash", str(LOCAL_SCRIPT), "install"],
        json_output=json_output,
        dry_run=dry_run,
    )
    if not dry_run:
        if not verify(json_output=json_output):
            raise SetupFailure("HealthMes did not become reachable after installation.")
        issue_pairing_links(json_output=json_output)


def diagnostics(*, json_output: bool) -> Path:
    directory = REPO_ROOT / "data" / "diagnostics"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (
        "setup-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        + ".json"
    )
    commands = {
        "git": ["git", "-C", str(REPO_ROOT), "status", "--short", "--branch"],
        "runtime": ["bash", str(LOCAL_SCRIPT), "status"],
    }
    report: dict[str, object] = {
        "schema": SCHEMA,
        "created_at": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "commands": {},
    }
    for key, command in commands.items():
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        report["commands"][key] = {
            "exit_code": result.returncode,
            "stdout": result.stdout[-8_000:],
            "stderr": result.stderr[-8_000:],
        }
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)
    emit(
        SetupEvent(
            "diagnostics",
            "export",
            "ready",
            "Diagnostics exported without environment secrets.",
            str(path),
        ),
        json_output=json_output,
    )
    return path


def _last_nonempty_line(value: str) -> str | None:
    return next((line.strip() for line in reversed(value.splitlines()) if line.strip()), None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=(
            "preflight",
            "install",
            "pair",
            "verify",
            "repair",
            "update",
            "diagnostics",
            "uninstall",
        ),
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "preflight":
            for event in preflight():
                emit(event, json_output=args.json)
        elif args.action == "install":
            install(json_output=args.json, dry_run=args.dry_run)
        elif args.action == "pair":
            issue_pairing_links(json_output=args.json)
        elif args.action == "verify":
            return 0 if verify(json_output=args.json) else 2
        elif args.action == "repair":
            prepare_runtime(
                "repair",
                json_output=args.json,
                dry_run=args.dry_run,
            )
            run_command(
                "repair",
                "runtime_repair",
                ["bash", str(LOCAL_SCRIPT), "install"],
                json_output=args.json,
                dry_run=args.dry_run,
            )
            if not args.dry_run:
                return 0 if verify(json_output=args.json) else 2
        elif args.action == "update":
            prepare_runtime(
                "update",
                json_output=args.json,
                dry_run=args.dry_run,
            )
            run_command(
                "update",
                "runtime_update",
                ["bash", str(LOCAL_SCRIPT), "update"],
                json_output=args.json,
                dry_run=args.dry_run,
            )
            if not args.dry_run:
                return 0 if verify(json_output=args.json) else 2
        elif args.action == "diagnostics":
            diagnostics(json_output=args.json)
        elif args.action == "uninstall":
            run_command(
                "uninstall",
                "runtime_uninstall",
                ["bash", str(LOCAL_SCRIPT), "uninstall"],
                json_output=args.json,
                dry_run=args.dry_run,
            )
        return 0
    except SetupFailure as exc:
        emit(
            SetupEvent(
                args.action,
                "complete",
                "failed",
                "Setup could not complete.",
                str(exc),
            ),
            json_output=args.json,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
