# HealthMes Backup — Snapshot Format & Provider Contract

Local-first with an encrypted backup *seam* (docs/PLAN.md §9): all HealthMes
data leaves the live stores only as an **age-encrypted snapshot envelope**
moving through the `BackupProvider` protocol
(`healthmes/backup/provider.py`). The MVP ships `LocalDirectoryProvider`;
the future paid remote vault implements the same protocol against
S3-compatible storage. **Exporting data around this interface is
forbidden** — that rule is what makes the remote vault a viable business:
the server only ever stores ciphertext.

## 1. Snapshot envelope format (schema_version 1)

A snapshot is a single immutable file:

```
healthmes-backup-<YYYYMMDDTHHMMSSZ>.tar.gz.age
└── age encryption, scrypt passphrase recipient (pyrage)
    └── gzip-compressed tar
        ├── manifest.json
        ├── db/healthmes.sqlite3        # OR db/healthmes.dump
        ├── db/open_wearables.dump      # optional
        ├── media/**                    # optional
        └── hermes/**                   # optional
```

- The UTC timestamp in the name is the caller-injected creation instant, so
  lexicographic file order equals chronological order. Listing snapshots
  never requires the passphrase (metadata comes from name + size only).
- Compression happens **before** encryption (age output is incompressible).
- Encryption is age v1 with a passphrase-derived scrypt recipient
  (`pyrage.passphrase`). No key files; losing the passphrase loses every
  snapshot — by design, there is no recovery path.

### Envelope members

| Member | Present | Contents |
|---|---|---|
| `manifest.json` | always | See below. |
| `db/healthmes.sqlite3` | sqlite `HEALTHMES_DATABASE_URL` | Consistent `sqlite3.Connection.backup` snapshot of the database. |
| `db/healthmes.dump` | postgres `HEALTHMES_DATABASE_URL` | `pg_dump --format=custom --no-owner --no-privileges`. |
| `db/open_wearables.dump` | open-wearables database URL configured | `pg_dump -Fc` of the vendor database (same flags). |
| `media/**` | `{HEALTHMES_DATA_DIR}/media` exists | Full media tree (photos/voice notes; the DB stores only relative paths, so DB + media restore reconnects everything). |
| `hermes/**` | `HERMES_HOME` configured | Hermes agent memory/state (config, memory, cron state). |

Exactly one of the two `db/healthmes.*` members is present. `pg_dump` is
located via `PATH` first, then the Homebrew kegs (`brew --prefix
postgresql@16` / `libpq` / `postgresql`) because macOS keeps keg-only
postgres binaries off `PATH`.

**Symlink policy:** symlinks that stay inside a copied tree are preserved as
symlinks. Symlinks pointing *outside* the tree (notably
`$HERMES_HOME/skills/*` → this repo, re-created by `scripts/bootstrap.py`)
are **skipped and recorded** in the manifest (`contents.<section>.skipped`),
keeping the archive self-contained and extraction safe under `tarfile`'s
`data` filter. Sockets/fifos are skipped the same way.

### manifest.json

```jsonc
{
  "schema_version": 1,              // integer; bumped on breaking layout change
  "created_at": "2026-07-09T03:30:00+00:00",  // injected by the caller, tz-aware
  "healthmes_version": "0.1.0",
  "contents": {
    "healthmes_db":      {"kind": "sqlite_file" | "pg_dump", "arcname": "db/…"},
    "open_wearables_db": {"kind": "pg_dump", "arcname": "db/open_wearables.dump"} | null,
    "media":       {"arcroot": "media",  "file_count": 12, "total_bytes": 5182034,
                    "skipped": [ {"path": "…", "reason": "symlink-outside-tree",
                                  "target": "/abs/target"} ]} | null,
    "hermes_home": {"arcroot": "hermes", "file_count": 4, "total_bytes": 9182,
                    "skipped": []} | null
  },
  "inventory": [                    // every archived file and symlink
    {"path": "db/healthmes.sqlite3", "kind": "file",
     "size_bytes": 32768, "sha256": "9f86d0…"},
    {"path": "hermes/memory/current.json", "kind": "symlink", "target": "state.json"}
  ]
}
```

Restore extracts to a scratch directory, verifies the **whole inventory in
both directions** (every listed file exists with matching size + SHA-256;
the archive holds nothing undeclared) and only then replaces live targets.
A snapshot with `schema_version` greater than the tool's supported version
is refused with an upgrade hint; older versions must remain restorable
forever (schema changes are additive or come with migration code).

### Consistency caveats

- The sqlite member goes through `sqlite3.Connection.backup` (source opened
  read-only), which holds the database lock and produces a transactionally
  consistent single-file snapshot **even while the in-process jobs (trigger
  sweep, energy persist) keep writing** — no torn pages, no dependence on
  the `-journal`/`-wal` sidecars. The copy is logically exact but not
  byte-identical to the live file (header change counters differ).
- `pg_dump` custom-format dumps are transactionally consistent on their own.
  The connection URL passed to `pg_dump`/`pg_restore` argv is
  **credential-stripped**; the password travels via the `PGPASSWORD`
  environment variable so it never appears in process listings.
- The whole envelope passes through memory once during encrypt/decrypt
  (pyrage's passphrase API is bytes-based) — fine at personal scale.

## 2. Using it

```sh
export HEALTHMES_BACKUP_PASSPHRASE='correct horse battery staple'

uv run python -m healthmes backup create
uv run python -m healthmes backup list
uv run python -m healthmes backup restore healthmes-backup-20260709T033000Z.tar.gz.age        # dry-run: prints manifest
uv run python -m healthmes backup restore healthmes-backup-20260709T033000Z.tar.gz.age --yes  # applies (destructive)
```

Configuration (Settings fields / env fallbacks — see `resolve_*` in
`healthmes/backup/snapshot.py`):

| Setting | Env var | Default | Meaning |
|---|---|---|---|
| `backup_dir` | `HEALTHMES_BACKUP_DIR` | `{data_dir}/backups` | Where `LocalDirectoryProvider` stores snapshots. |
| `backup_passphrase` | `HEALTHMES_BACKUP_PASSPHRASE` | — (required) | age scrypt passphrase; `--passphrase-file` overrides. |
| `ow_database_url` | `HEALTHMES_OW_DATABASE_URL` | unset → OW dump skipped | Direct postgres URL of the open-wearables DB. |
| `hermes_home` | `HERMES_HOME` | unset → Hermes state skipped | Hermes state directory. |

The weekly snapshot runs through the scheduler hook
`healthmes.engine.scheduler.register_backup_job` with the callable from
`healthmes.backup.local.build_backup_job` (Sunday 03:30 local, inside quiet
hours; skips with a log warning when no passphrase is configured).

Restore drill (PLAN §10 Phase 3 / 검증 방법): on a fresh checkout, set
`HEALTHMES_DATABASE_URL`/`HEALTHMES_DATA_DIR`/`HERMES_HOME` to the new
locations, run `backup restore <file> --yes`, start the stack, and re-run
the Phase-0 demo query. Opening a snapshot without the passphrase must fail
(`WrongPassphraseError`).

## 3. RemoteVaultProvider contract (the business seam — no code yet)

A future paid service implements `BackupProvider` against an S3-compatible
vault. This section is the contract; MVP intentionally ships **no**
implementation beyond the protocol.

**Storage model**

- One object per snapshot, key `vaults/{vault_id}/{snapshot_name}`; the
  object body is byte-identical to the local `*.tar.gz.age` file.
- Objects are immutable: no overwrite, no rename; deletion only through an
  explicit retention/pruning call. Server-side versioning + object lock
  (compliance mode) recommended.
- `list_snapshots()` maps to a key listing; `SnapshotInfo` derives from the
  key name and object size — identical semantics to the local provider.

**Privacy invariants (non-negotiable)**

1. **Client-side encryption only.** The envelope is encrypted with the
   user's passphrase *before* upload; the vault stores ciphertext it can
   never open. Any additional server-side encryption is defense in depth,
   never a substitute.
2. **The passphrase (or any derived key) never leaves the client.** No
   key escrow, no server-side recovery. Losing the passphrase loses the
   vault — the product communicates this loudly at setup.
3. **Metadata minimalism.** The server may see: snapshot name (creation
   timestamp), ciphertext size, upload time, account/billing identity.
   It must never receive the manifest, file listings, or any health-domain
   metadata; nothing about the plaintext is used for analytics.
4. **Same seam, no side doors.** The vault client is a `BackupProvider`;
   sync/telemetry/"insights" uploads that bypass `export_snapshot()` are
   architecture violations (PLAN §9: "이 인터페이스를 우회한 데이터 반출 금지").

**Operational contract**

- `export_snapshot()` uploads with content-integrity protection (e.g.
  SHA-256 checksum header / multipart ETag verification) and must be
  all-or-nothing: a failed upload leaves no partial object visible.
- `restore(path)` downloads to a temp file, then reuses the exact local
  verification pipeline (decrypt → extract → inventory check → replace).
- Recommended extras (not in the protocol): retention policy, bandwidth
  limits, resumable uploads, a `verify` endpoint that re-checks stored
  object checksums — all operate on ciphertext only.

## 4. Compatibility & versioning rules

- `SCHEMA_VERSION` lives in `healthmes/backup/snapshot.py` and inside every
  manifest. Bump it only for layout changes a v1 reader cannot survive;
  readers must keep restoring every version ≤ their own.
- New *optional* manifest fields/sections may be added without a bump;
  readers ignore unknown fields (forward-tolerant, like the API schemas).
- The snapshot name format is part of the contract (`list_snapshots()` and
  remote vaults parse it); never localize or reorder it.
