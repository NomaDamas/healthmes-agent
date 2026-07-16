"""HealthMes command line: serve the API, manage backups, connect calendars.

``python -m healthmes``            → serve (uvicorn), same as before
``python -m healthmes serve``      → serve explicitly
``python -m healthmes backup create``            → write one encrypted snapshot
``python -m healthmes backup list``              → list snapshots (no passphrase)
``python -m healthmes backup restore <snapshot>``→ inspect; add --yes to apply
``python -m healthmes backup push <snapshot>``   → upload one snapshot to the vault
``python -m healthmes connect google``           → browser OAuth; token saved locally
``python -m healthmes connect icloud --username <apple-id>`` → app-password prompt
``python -m healthmes connect status``           → which calendars are connected
``python -m healthmes connect disconnect google|icloud``     → remove stored creds

Calendar connections are runtime state under ``Settings.data_dir`` (docs/
PLAN.md §6): once ``connect`` succeeds, the sync jobs pick the backend up
automatically (healthmes/calendars/jobs.py::enabled_sources) — no ``.env``
edit needed. Secrets are never taken from argv (the iCloud app password is
prompted hidden via getpass) and never echoed.

``create``/``list``/``restore`` accept ``--provider {local,remote}``
(default: the HEALTHMES_BACKUP_PROVIDER selector, then local). The remote
vault (docs/BACKUP.md) is a replication target for the age-encrypted
envelopes: ``create --provider remote`` writes locally first and then
uploads; ``list`` shows the merged view labeled by origin; ``restore``
downloads the envelope when it is not already local. The local copy is only
skipped with the explicit ``--remote-only`` flag.

Backup commands read Settings from the environment/.env like the service
does; the passphrase comes from HEALTHMES_BACKUP_PASSPHRASE or
``--passphrase-file`` (never a CLI argument — argv leaks into shell history
and process listings).
"""

import argparse
import getpass
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from healthmes.backup.local import LocalDirectoryProvider
from healthmes.backup.provider import BackupError
from healthmes.backup.snapshot import (
    PROVIDER_LOCAL,
    PROVIDER_REMOTE_VAULT,
    read_manifest,
    resolve_backup_provider_name,
    resolve_passphrase,
)
from healthmes.calendars import creds as calendar_creds
from healthmes.calendars.base import CalendarError
from healthmes.config import Settings, get_settings, is_loopback_host

if TYPE_CHECKING:  # pragma: no cover — typing only; runtime import stays lazy
    from healthmes.backup.remote_vault import RemoteVaultProvider


def check_bind_safety(settings: Settings) -> str | None:
    """Refuse an unauthenticated non-loopback bind; None when safe to serve.

    The surface carries medical records, transcripts and full health context
    (docs/PLAN.md §9: medical data never leaves this machine). Binding beyond
    loopback (LAN Android ingest, docker compose) is only allowed with the
    bearer token configured — otherwise any Wi-Fi peer could read everything.
    """
    if is_loopback_host(settings.host) or settings.api_token.get_secret_value().strip():
        return None
    return (
        f"refusing to bind {settings.host}:{settings.port} without authentication: "
        "the HealthMes surface exposes medical data. Set HEALTHMES_API_TOKEN "
        "(clients send 'Authorization: Bearer <token>') or bind a loopback "
        "host (HEALTHMES_HOST=127.0.0.1)."
    )


def _serve() -> int:
    """Run the FastAPI service (the pre-CLI behavior of ``python -m healthmes``)."""
    import uvicorn

    settings = get_settings()
    error = check_bind_safety(settings)
    if error is not None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    uvicorn.run(
        "healthmes.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
    )
    return 0


def _cli_settings() -> Settings:
    """Fresh env-derived Settings for one-shot commands (no singleton caching)."""
    return Settings()


def _passphrase_from(args: argparse.Namespace, settings: Settings) -> str | None:
    """Resolve the passphrase: --passphrase-file wins, then Settings/env."""
    passphrase_file: Path | None = getattr(args, "passphrase_file", None)
    if passphrase_file is not None:
        try:
            text = passphrase_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise BackupError(f"could not read --passphrase-file: {exc}") from exc
        if not text:
            raise BackupError(f"passphrase file is empty: {passphrase_file}")
        return text
    return resolve_passphrase(settings)


def _provider(args: argparse.Namespace, settings: Settings) -> LocalDirectoryProvider:
    return LocalDirectoryProvider.from_settings(
        settings, passphrase=_passphrase_from(args, settings)
    )


def _selected_provider(args: argparse.Namespace, settings: Settings) -> str:
    """Effective provider name: --provider flag first, then Settings/env selector."""
    flag = getattr(args, "provider", None)
    if flag is not None:
        return PROVIDER_REMOTE_VAULT if flag == "remote" else PROVIDER_LOCAL
    return resolve_backup_provider_name(settings)


def _vault_provider(
    args: argparse.Namespace, settings: Settings, *, keep_local: bool = True
) -> "RemoteVaultProvider":
    """Vault provider for the CLI; errors cleanly when no vault is configured.

    The passphrase is resolved the same way as for the local provider (push
    and list never use it; create/restore do).
    """
    from healthmes.backup.remote_vault import RemoteVaultProvider

    return RemoteVaultProvider.from_settings(
        settings, passphrase=_passphrase_from(args, settings), keep_local=keep_local
    )


def _human_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size_bytes} B"


def _cmd_backup_create(args: argparse.Namespace) -> int:
    settings = _cli_settings()
    provider_name = _selected_provider(args, settings)
    if provider_name == PROVIDER_LOCAL:
        if args.remote_only:
            raise BackupError("--remote-only requires --provider remote")
        info = _provider(args, settings).export_snapshot()
        print(f"snapshot written: {info.path} ({_human_size(info.size_bytes)})")
        return 0
    vault = _vault_provider(args, settings, keep_local=not args.remote_only)
    local_info, remote_info = vault.create_and_replicate()
    print(f"snapshot written: {local_info.path} ({_human_size(local_info.size_bytes)})")
    print(f"uploaded to vault: {vault.object_uri(remote_info.name)}")
    if args.remote_only:
        print(
            "local copy removed (--remote-only): the vault now holds the only copy",
            file=sys.stderr,
        )
    return 0


def _cmd_backup_list(args: argparse.Namespace) -> int:
    settings = _cli_settings()
    provider_name = _selected_provider(args, settings)
    provider = LocalDirectoryProvider.from_settings(settings)
    if provider_name == PROVIDER_LOCAL:
        snapshots = provider.list_snapshots()
        if not snapshots:
            print(f"no snapshots in {provider.backup_dir}")
            return 0
        for info in snapshots:
            stamp = info.created_at.isoformat().replace("+00:00", "Z")
            print(f"{info.name}\t{stamp}\t{_human_size(info.size_bytes)}")
        return 0
    vault = _vault_provider(args, settings)
    merged = vault.list_merged()
    if not merged:
        print(f"no snapshots in {provider.backup_dir} or {vault.vault_uri}")
        return 0
    for entry in merged:
        info = entry.info
        stamp = info.created_at.isoformat().replace("+00:00", "Z")
        origin = entry.origin + (" (size mismatch!)" if entry.size_mismatch else "")
        print(f"{info.name}\t{stamp}\t{_human_size(info.size_bytes)}\t{origin}")
    return 0


def _cmd_backup_push(args: argparse.Namespace) -> int:
    settings = _cli_settings()
    vault = _vault_provider(args, settings)
    remote_info = vault.push(args.snapshot)
    print(
        f"pushed: {remote_info.name} -> {vault.object_uri(remote_info.name)} "
        f"({_human_size(remote_info.size_bytes)})"
    )
    return 0


def _summarize_manifest(manifest: dict) -> list[str]:
    contents = manifest.get("contents", {})
    lines = [
        f"created_at:         {manifest.get('created_at')}",
        f"schema_version:     {manifest.get('schema_version')}",
        f"healthmes_version:  {manifest.get('healthmes_version')}",
    ]
    db_entry = contents.get("healthmes_db") or {}
    lines.append(f"healthmes db:       {db_entry.get('kind', 'missing')}")
    ow_entry = contents.get("open_wearables_db")
    lines.append(f"open-wearables db:  {ow_entry['kind'] if ow_entry else 'not included'}")
    for label, key in (("media", "media"), ("hermes state", "hermes_home")):
        entry = contents.get(key)
        if entry:
            lines.append(
                f"{label + ':':<20}{entry['file_count']} files, "
                f"{_human_size(entry['total_bytes'])}"
            )
        else:
            lines.append(f"{label + ':':<20}not included")
    return lines


def _cmd_backup_restore(args: argparse.Namespace) -> int:
    settings = _cli_settings()
    provider = _provider(args, settings)
    if _selected_provider(args, settings) == PROVIDER_LOCAL:
        snapshot_path = provider.resolve_snapshot_path(args.snapshot)
    else:
        # Local-first: an existing local copy wins; otherwise the envelope is
        # downloaded into the backup dir and the restore path is identical.
        vault = _vault_provider(args, settings)
        snapshot_path, downloaded = vault.ensure_local_copy(args.snapshot)
        if downloaded:
            print(f"downloaded from vault: {vault.object_uri(snapshot_path.name)}")
    if not args.yes:
        passphrase = _passphrase_from(args, settings)
        if passphrase is None:
            raise BackupError(
                "no backup passphrase configured; set HEALTHMES_BACKUP_PASSPHRASE "
                "or pass --passphrase-file"
            )
        manifest = read_manifest(snapshot_path, passphrase)
        print(f"snapshot: {snapshot_path}")
        for line in _summarize_manifest(manifest):
            print(line)
        print(
            "\nrestore REPLACES the live database, media tree and Hermes state.\n"
            f"re-run with --yes to apply:  healthmes backup restore {args.snapshot} --yes",
            file=sys.stderr,
        )
        return 2
    provider.restore(snapshot_path)
    print(f"restored: {snapshot_path}")
    return 0


# --- calendar connections (healthmes connect ...) ---------------------------


GOOGLE_SETUP_INSTRUCTIONS = """\
One-time Google setup (Google requires registering your own OAuth client;
there is no way around this for a personal installed app):

  1. Open https://console.cloud.google.com/ and create (or select) a project.
  2. "APIs & Services" -> "Library": enable the **Google Calendar API**.
  3. "APIs & Services" -> "OAuth consent screen": configure it and add your
     own Google account as a test user.
  4. "APIs & Services" -> "Credentials" -> "Create credentials" ->
     "OAuth client ID" -> application type **Desktop app**.
  5. Download the client JSON and save it to:
       {client_secret_path}
     (or point HEALTHMES_GOOGLE_CLIENT_SECRET_FILE at wherever you keep it).

Then re-run:  healthmes connect google"""


def _google_client_secret(settings: Settings) -> Path | None:
    """The client secret to use: the data-dir standard path, else the override."""
    from healthmes.calendars import google as google_calendar

    standard = google_calendar.google_client_secret_path(settings.data_dir)
    if standard.exists():
        return standard
    override = settings.google_client_secret_file
    if override is not None and Path(override).exists():
        return Path(override)
    return None


def _google_identity(google_calendar, credentials, calendar_id: str) -> str | None:
    """Best-effort display identity of the authorized calendar (never fails).

    ``calendars.get('primary')`` returns the account email as the summary for
    most accounts; any API/permission hiccup degrades to ``None`` — the token
    is already saved, so the connect must not fail on a cosmetic probe.
    """
    try:
        service = google_calendar.build_calendar_service(credentials)
        info = service.calendars().get(calendarId=calendar_id).execute()
        return str(info.get("summary") or info.get("id") or "") or None
    except Exception:  # noqa: BLE001 - cosmetic probe only
        return None


def _cmd_connect_google(args: argparse.Namespace) -> int:
    settings = _cli_settings()
    from healthmes.calendars import google as google_calendar

    token_path = google_calendar.google_token_path(settings.data_dir)
    if calendar_creds.google_connection_state(settings.data_dir) == "connected":
        print(f"Google Calendar is already connected (token at {token_path}).")
        print("To re-authorize, run `healthmes connect disconnect google` first.")
        return 0

    client_secret = _google_client_secret(settings)
    if client_secret is None:
        expected = google_calendar.google_client_secret_path(settings.data_dir)
        print("error: no Google OAuth client secret found.\n", file=sys.stderr)
        print(
            GOOGLE_SETUP_INSTRUCTIONS.format(client_secret_path=expected),
            file=sys.stderr,
        )
        return 1

    print("Opening your browser for Google login + consent ...")
    credentials = google_calendar.run_installed_app_flow(
        client_secret, token_path, port=args.port
    )
    identity = _google_identity(google_calendar, credentials, settings.google_calendar_id)
    if identity:
        print(f"connected as {identity}")
    else:
        print("connected")
    print(f"token saved to {token_path} (owner-only)")
    print(
        "Google Calendar sync is now enabled automatically (the poll job "
        "detects this token; HEALTHMES_GOOGLE_CALENDAR_ENABLED=true also "
        "works). Polling runs while the service has "
        "HEALTHMES_SCHEDULER_ENABLED=true."
    )
    return 0


def _cmd_connect_icloud(args: argparse.Namespace) -> int:
    settings = _cli_settings()
    username = args.username.strip()
    if not username:
        print("error: --username must be a non-empty Apple ID email", file=sys.stderr)
        return 1
    url = (args.url or settings.caldav_url).strip()
    app_password = getpass.getpass(
        "App-specific password (hidden; create one at https://appleid.apple.com): "
    ).strip()
    if not app_password:
        print("error: empty password — nothing stored.", file=sys.stderr)
        return 1

    print(f"validating CalDAV connection to {url} as {username} ...")
    summary = calendar_creds.validate_caldav_connection(
        username=username, app_password=app_password, url=url
    )
    path = calendar_creds.save_caldav_credentials(
        settings.data_dir, username=username, app_password=app_password, url=url
    )
    print(f"connected as {username} — {summary}")
    print(f"credentials saved to {path} (owner-only, mode 600)")
    print(
        "iCloud calendar sync is now enabled automatically (the poll job "
        "detects this credentials file; the HEALTHMES_CALDAV_* env vars keep "
        "working and override it). Polling runs while the service has "
        "HEALTHMES_SCHEDULER_ENABLED=true."
    )
    return 0


def _cmd_connect_status(_args: argparse.Namespace) -> int:
    settings = _cli_settings()
    from healthmes.calendars import google as google_calendar

    google_state = calendar_creds.google_connection_state(settings.data_dir)
    token_path = google_calendar.google_token_path(settings.data_dir)
    if google_state == "connected":
        print(f"google: connected (token at {token_path})")
    elif google_state == "invalid":
        print(
            "google: not connected — token file exists but is unusable; "
            "run `healthmes connect disconnect google`, then `healthmes connect google`"
        )
    else:
        print("google: not connected — run `healthmes connect google`")
    if settings.google_calendar_enabled and google_state != "connected":
        print(
            "        note: HEALTHMES_GOOGLE_CALENDAR_ENABLED=true forces the poll "
            "job on, but it will fail until a token exists"
        )

    resolved = calendar_creds.resolve_caldav_credentials(settings)
    if resolved is not None:
        origin = ".env (HEALTHMES_CALDAV_*)" if resolved.source == "env" else (
            f"creds file at {calendar_creds.caldav_credentials_path(settings.data_dir)}"
        )
        print(f"icloud: connected as {resolved.username} (via {origin})")
        if resolved.source == "env" and (
            calendar_creds.load_caldav_credentials(settings.data_dir) is not None
        ):
            print("        note: a creds file also exists; the env values override it")
    else:
        print(
            "icloud: not connected — run `healthmes connect icloud "
            "--username <apple-id>`"
        )
    if not settings.scheduler_enabled:
        print(
            "note: HEALTHMES_SCHEDULER_ENABLED is false — connected calendars "
            "are polled only while the service runs with it set to true"
        )
    return 0


def _cmd_connect_disconnect(args: argparse.Namespace) -> int:
    settings = _cli_settings()
    if args.target == "google":
        from healthmes.calendars import google as google_calendar

        removed = calendar_creds.delete_google_token(settings.data_dir)
        token_path = google_calendar.google_token_path(settings.data_dir)
        print(f"removed {token_path}" if removed else "nothing to remove (no stored token)")
        if settings.google_calendar_enabled:
            print(
                "note: HEALTHMES_GOOGLE_CALENDAR_ENABLED=true still forces the "
                "poll job on; unset it to fully disable"
            )
        return 0
    removed = calendar_creds.delete_caldav_credentials(settings.data_dir)
    creds_path = calendar_creds.caldav_credentials_path(settings.data_dir)
    print(f"removed {creds_path}" if removed else "nothing to remove (no stored credentials)")
    if settings.caldav_username.strip() and settings.caldav_app_password.get_secret_value():
        print(
            "note: HEALTHMES_CALDAV_USERNAME/HEALTHMES_CALDAV_APP_PASSWORD are "
            "still set in the environment/.env and keep the connection alive; "
            "clear them to fully disconnect"
        )
    return 0


def _add_passphrase_file(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--passphrase-file",
        type=Path,
        default=None,
        help="File whose (stripped) contents are the age passphrase; "
        "overrides HEALTHMES_BACKUP_PASSPHRASE.",
    )


def _add_provider_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provider",
        choices=("local", "remote"),
        default=None,
        help="Backup provider: 'local' (directory only) or 'remote' (replicate "
        "to the S3-compatible vault, HEALTHMES_VAULT_*). Default: the "
        "HEALTHMES_BACKUP_PROVIDER selector, then 'local'.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="healthmes",
        description="HealthMes service and local-first encrypted backups.",
    )
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="Run the HealthMes FastAPI service (default).")
    serve.set_defaults(func=lambda _args: _serve())

    backup = subparsers.add_parser("backup", help="Create/list/restore encrypted snapshots.")
    backup_sub = backup.add_subparsers(dest="backup_command", required=True)

    create = backup_sub.add_parser(
        "create", help="Snapshot databases + media + Hermes state into an age-encrypted archive."
    )
    _add_passphrase_file(create)
    _add_provider_flag(create)
    create.add_argument(
        "--remote-only",
        action="store_true",
        help="With --provider remote: delete the local copy after a verified "
        "upload — the vault then holds the ONLY copy (local-first is the default).",
    )
    create.set_defaults(func=_cmd_backup_create)

    list_parser = backup_sub.add_parser(
        "list", help="List snapshots in the backup directory (needs no passphrase)."
    )
    _add_provider_flag(list_parser)
    list_parser.set_defaults(func=_cmd_backup_list)

    restore = backup_sub.add_parser(
        "restore",
        help="Restore a snapshot (path or name). Without --yes only the manifest is shown.",
    )
    restore.add_argument("snapshot", help="Snapshot file path, or bare name in the backup dir.")
    restore.add_argument(
        "--yes",
        action="store_true",
        help="Actually apply the restore (it replaces live data).",
    )
    _add_passphrase_file(restore)
    _add_provider_flag(restore)
    restore.set_defaults(func=_cmd_backup_restore)

    push = backup_sub.add_parser(
        "push",
        help="Upload one existing local snapshot to the remote vault "
        "(refuses anything that is not an age-encrypted snapshot envelope).",
    )
    push.add_argument("snapshot", help="Snapshot file path, or bare name in the backup dir.")
    push.set_defaults(func=_cmd_backup_push)

    connect = subparsers.add_parser(
        "connect",
        help="Connect calendars: Google (browser OAuth) / iCloud (app-specific password).",
    )
    connect_sub = connect.add_subparsers(dest="connect_command", required=True)

    connect_google = connect_sub.add_parser(
        "google",
        help="Run the installed-app OAuth flow in your browser; the token is "
        "saved to {data_dir}/google/calendar_token.json. Requires the one-time "
        "OAuth client secret (instructions are printed when it is missing).",
    )
    connect_google.add_argument(
        "--port",
        type=int,
        default=0,
        help="Fixed localhost port for the OAuth loopback listener (default: random free port).",
    )
    connect_google.set_defaults(func=_cmd_connect_google)

    connect_icloud = connect_sub.add_parser(
        "icloud",
        help="Connect iCloud Calendar via CalDAV: prompts (hidden) for an "
        "app-specific password, validates against the server, then stores the "
        "credential owner-only under {data_dir}/caldav/.",
    )
    connect_icloud.add_argument(
        "--username", required=True, help="Apple ID email (the iCloud account)."
    )
    connect_icloud.add_argument(
        "--url",
        default=None,
        help="CalDAV discovery URL (default: HEALTHMES_CALDAV_URL, i.e. iCloud).",
    )
    connect_icloud.set_defaults(func=_cmd_connect_icloud)

    connect_status = connect_sub.add_parser(
        "status", help="Show which calendars are connected (never prints secrets)."
    )
    connect_status.set_defaults(func=_cmd_connect_status)

    connect_disconnect = connect_sub.add_parser(
        "disconnect", help="Remove the stored token/credentials for one calendar."
    )
    connect_disconnect.add_argument("target", choices=("google", "icloud"))
    connect_disconnect.set_defaults(func=_cmd_connect_disconnect)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "func", None) is None:
        return _serve()  # bare `python -m healthmes` keeps serving (compose/dev_mac.sh)
    try:
        return args.func(args)
    except (BackupError, CalendarError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
