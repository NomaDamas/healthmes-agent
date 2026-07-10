"""HealthMes command line: serve the API or manage encrypted backups.

``python -m healthmes``            → serve (uvicorn), same as before
``python -m healthmes serve``      → serve explicitly
``python -m healthmes backup create``            → write one encrypted snapshot
``python -m healthmes backup list``              → list snapshots (no passphrase)
``python -m healthmes backup restore <snapshot>``→ inspect; add --yes to apply
``python -m healthmes backup push <snapshot>``   → upload one snapshot to the vault

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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "func", None) is None:
        return _serve()  # bare `python -m healthmes` keeps serving (compose/dev_mac.sh)
    try:
        return args.func(args)
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
