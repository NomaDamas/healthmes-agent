# HealthMes Backup — Snapshot Format & Provider Contract

Local-first with an encrypted backup *seam* (docs/PLAN.md §9): the selected
HealthMes component data leaves the live stores only as an **age-encrypted
snapshot envelope**
moving through the `BackupProvider` protocol
(`healthmes/backup/provider.py`). `LocalDirectoryProvider` is the default;
`RemoteVaultProvider` (`healthmes/backup/remote_vault.py`) implements the
same protocol against any S3-compatible storage (AWS S3, Cloudflare R2,
MinIO, …) — self-hostable today, and the exact seam the future paid vault
service runs on. **Exporting data around this interface is forbidden** —
that rule is what makes the remote vault a viable business: the server only
ever stores ciphertext.

## 1. Snapshot envelope format (schema_version 2)

A snapshot is a single immutable file:

```
healthmes-backup-<YYYYMMDDTHHMMSSZ>.tar.gz.age
└── age encryption, scrypt passphrase recipient (pyrage)
    └── gzip-compressed tar
        ├── manifest.json
        ├── db/healthmes.sqlite3        # OR db/healthmes.dump
        ├── db/open_wearables.dump      # optional
        ├── media/**                    # optional
        ├── raw_ingest/**               # optional
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
| `raw_ingest/**` | `{HEALTHMES_DATA_DIR}/raw_ingest` exists | Raw-first ingest payloads referenced by the HealthMes DB. |
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
  "schema_version": 2,              // v2 adds raw_ingest
  "created_at": "2026-07-09T03:30:00+00:00",  // injected by the caller, tz-aware
  "healthmes_version": "0.1.0",
  "recovery": {
    "scope": "partial_component_snapshot",
    "full_node_recovery": false,
    "components": {
      "healthmes_db": {"status": "included"},
      "media": {"status": "included" | "source_not_present" | "not_configured"},
      "raw_ingest": {"status": "included" | "source_not_present" | "not_configured"},
      "open_wearables_db": {
        "status": "included" | "omitted_missing_dump_url" | "not_configured",
        "runtime_configured": true,
        "dump_configured": false
      },
      "hermes_home": {"status": "included" | "source_not_present" | "not_configured"}
    },
    "operational_warnings": [
      "Partial backup: Open Wearables is configured for runtime, but ..."
    ]
  },
  "contents": {
    "healthmes_db":      {"kind": "sqlite_file" | "pg_dump", "arcname": "db/…"},
    "open_wearables_db": {"kind": "pg_dump", "arcname": "db/open_wearables.dump"} | null,
    "media":       {"arcroot": "media",  "file_count": 12, "total_bytes": 5182034,
                    "skipped": [ {"path": "…", "reason": "symlink-outside-tree",
                                  "target": "/abs/target"} ]} | null,
    "raw_ingest":  {"arcroot": "raw_ingest", "file_count": 3,
                    "total_bytes": 1048576, "skipped": []} | null,
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
forever (schema changes are additive or come with migration code). The
additive `recovery` block does not change schema version 2 payload or restore
semantics. It records what the snapshot can recover and never marks an
envelope as a full-node backup.

### Recovery boundary

This envelope is a **partial component snapshot**, not a complete Personal
Data Node image. It includes only the members listed in the manifest. It does
not automatically include `.env`, provider OAuth credentials stored outside
the selected Hermes home, external object stores, device secrets, or the
Open Wearables database when `HEALTHMES_OW_DATABASE_URL` is unset. Restoring
the snapshot therefore restores the archived data components; reconnecting
external providers and secrets remains an operator step.

Open Wearables runtime access and Open Wearables backup access are separate
capabilities:

- `HEALTHMES_OW_API_KEY` means HealthMes is configured to read Open Wearables
  at runtime.
- `HEALTHMES_OW_DATABASE_URL` gives `pg_dump` the independent database access
  needed to archive Open Wearables.
- When runtime access is configured but the dump URL is absent, backup
  creation still succeeds and produces a valid partial snapshot. The local
  provider, CLI, weekly scheduler job, and remote-create path emit an explicit
  `Partial backup` warning, and `manifest.json` records
  `omitted_missing_dump_url`. That snapshot can recover only the archived
  HealthMes DB, media/raw-ingest trees that were present, and any included
  Hermes state. It cannot recover Open Wearables data.

`GET /v1/storage/settings` reports the same current boundary as
`recovery_scope=partial_component_snapshot`,
`full_node_recovery=false`, the runtime/dump configuration booleans, and the
operational warning when this mismatch exists. Snapshot listing alone cannot
prove recoverability because the manifest is encrypted.

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

Remote-vault replication (`--provider remote` on the commands above, plus
`backup push <name>`): see §3.

Configuration (Settings fields / env fallbacks — see `resolve_*` in
`healthmes/backup/snapshot.py`):

| Setting | Env var | Default | Meaning |
|---|---|---|---|
| `backup_dir` | `HEALTHMES_BACKUP_DIR` | `{data_dir}/backups` | Where `LocalDirectoryProvider` stores snapshots. |
| `backup_passphrase` | `HEALTHMES_BACKUP_PASSPHRASE` | — (required) | age scrypt passphrase; `--passphrase-file` overrides. |
| `ow_database_url` | `HEALTHMES_OW_DATABASE_URL` | unset → OW dump skipped | Direct postgres URL of the open-wearables DB. If runtime API access is configured, leaving this unset creates a valid snapshot with an explicit partial-backup warning. |
| `hermes_home` | `HERMES_HOME` | unset → Hermes state skipped | Hermes state directory. |

The weekly snapshot runs through the scheduler hook
`healthmes.engine.scheduler.register_backup_job` with the callable from
`healthmes.backup.local.build_backup_job` (Sunday 03:30 local, inside quiet
hours; skips with a log warning when no passphrase is configured). A
successful weekly snapshot also logs the explicit partial-backup warning when
Open Wearables runtime access is configured without a dump URL.

Restore drill (PLAN §10 Phase 3 / 검증 방법): on a fresh checkout, set
`HEALTHMES_DATABASE_URL`/`HEALTHMES_DATA_DIR`/`HERMES_HOME` to the new
locations, run `backup restore <file> --yes`, start the stack, and re-run
the Phase-0 demo query. Opening a snapshot without the passphrase must fail
(`WrongPassphraseError`). The automated hardening drill creates representative
HealthMes DB rows plus binary `media/` and `raw_ingest/` files, snapshots
them, deletes all three live stores, restores, and byte-verifies both file
trees before reopening the database.

## 3. RemoteVault (the business seam — implemented)

`RemoteVaultProvider` (`healthmes/backup/remote_vault.py`) implements
`BackupProvider` against any S3-compatible endpoint. It is **self-hostable
today** (your own AWS/R2/MinIO bucket, your keys, your bill) and is the
exact provider the future paid vault service runs on — the paid offering is
this seam plus managed storage, retention and billing on top; nothing about
the data path changes. Because the seam is the product, the invariants
below are enforced in code, not just documented.

### What the server can and cannot see

The vault operator (whether that is AWS, Cloudflare, your own MinIO box, or
a future HealthMes-run service) stores **ciphertext only**. Snapshots are
age-encrypted *before the provider ever sees them* — there is no plaintext
moment on the upload path, and no key material is ever transmitted.

| The server sees | The server can NEVER see |
|---|---|
| Snapshot name → creation timestamp | Any plaintext (databases, media, Hermes state) |
| Ciphertext size | The manifest / file listing inside the envelope |
| Upload time, source IP, credentials/account identity | The passphrase or any derived key |
| A SHA-256 of the **ciphertext** (integrity metadata) | Any health-domain metadata usable for analytics |

**Privacy invariants (non-negotiable, enforced)**

1. **Client-side encryption only.** The envelope is encrypted with the
   user's passphrase *before* upload; the vault stores ciphertext it can
   never open. Any additional server-side encryption is defense in depth,
   never a substitute.
2. **The passphrase (or any derived key) never leaves the client.** No
   key escrow, no server-side recovery. Losing the passphrase loses the
   vault — the product communicates this loudly at setup.
3. **Metadata minimalism.** Exactly the left column above; nothing about
   the plaintext is used for analytics.
4. **Same seam, no side doors.** The vault client is a `BackupProvider`;
   sync/telemetry/"insights" uploads that bypass `export_snapshot()` are
   architecture violations (PLAN §9: "이 인터페이스를 우회한 데이터 반출 금지").
   The provider **refuses to upload anything that is not a snapshot
   envelope**: the file name must be the canonical
   `healthmes-backup-<UTC stamp>.tar.gz.age` form *and* the content must
   carry the age v1 header. Renaming a raw database to `*.tar.gz.age` is
   refused — the vault client cannot be repurposed as a generic uploader
   for health data.

### Local-first semantics

The vault is a **replication target**, never the primary store:

- `backup create --provider remote` writes the local snapshot first, then
  uploads a byte-identical copy. The local file stays unless you pass the
  explicit `--remote-only` flag (then the vault holds the only copy and the
  CLI says so on stderr).
- `backup push <name>` uploads an already-existing local snapshot.
- `backup restore <name> --provider remote` uses a local copy when present;
  otherwise it downloads the envelope into the backup dir and runs the
  exact local pipeline (decrypt → extract → inventory verify → replace).
- `backup list --provider remote` shows the union of both sides, labeled
  `local` / `remote` / `both` (plus a loud `size mismatch!` marker that
  should never appear — snapshots are immutable).
- The weekly scheduler job replicates to the vault when
  `HEALTHMES_BACKUP_PROVIDER=remote_vault`; a failed upload only logs — the
  local snapshot is already safe on disk.

### Storage model

- One object per snapshot, key `{HEALTHMES_VAULT_PREFIX}/{snapshot_name}`
  (default prefix `healthmes-vault`; a hosted multi-tenant vault uses
  `vaults/{vault_id}` as the prefix); the object body is byte-identical to
  the local `*.tar.gz.age` file.
- Objects are treated as immutable: no overwrite, no rename; the provider
  never deletes remote objects except to clean up its own failed upload.
  Server-side versioning + object lock (compliance mode) recommended for
  hosted deployments.
- `list_snapshots()` maps to a key listing; `SnapshotInfo` derives from the
  key name and object size — identical semantics to the local provider,
  and like it, **listing never needs the passphrase**.

### Operational contract (as implemented)

- Uploads are verified (single-part ETag vs local MD5 where the gateway
  provides it, object size otherwise) and all-or-nothing: S3 PUT semantics
  never expose a partial object, and a failed verification deletes the
  object before raising. The ciphertext SHA-256 travels as object metadata
  (`healthmes-sha256`) and is re-checked on download.
- Downloads land atomically (`.part` + rename); corruption is additionally
  caught by age's authenticated encryption and the manifest inventory
  check during restore.
- botocore's flexible-checksum negotiation is pinned to `when_required`
  for compatibility with non-AWS gateways; integrity comes from the checks
  above, not from AWS-only headers.
- Errors (wrong credentials, missing bucket, unreachable endpoint, missing
  object) surface as single-line actionable `BackupError`s naming the env
  var to fix — never a traceback.
- Recommended extras for a hosted service (not in the protocol): retention
  policy, bandwidth limits, resumable multipart uploads, a `verify`
  endpoint that re-checks stored object checksums — all operate on
  ciphertext only.

### Configuration matrix

Resolution is Settings-attribute first, then the env var (same pattern as
the other backup knobs); everything works from env vars alone.

| Env var | Required | Meaning | Example |
|---|---|---|---|
| `HEALTHMES_VAULT_BUCKET` | yes (turns the vault on) | Bucket name | `my-healthmes-vault` |
| `HEALTHMES_VAULT_ENDPOINT` | non-AWS | S3 API endpoint URL | `https://<account>.r2.cloudflarestorage.com` |
| `HEALTHMES_VAULT_ACCESS_KEY_ID` | usually | Access key (unset → boto3 default chain: env/profile/role) | `AKIA…` |
| `HEALTHMES_VAULT_SECRET_ACCESS_KEY` | usually | Secret key (paired with the above) | — |
| `HEALTHMES_VAULT_REGION` | provider-specific | Region (`auto` for R2; any value for MinIO) | `us-east-1` |
| `HEALTHMES_VAULT_PREFIX` | no | Key prefix, default `healthmes-vault` | `vaults/minseong` |
| `HEALTHMES_BACKUP_PROVIDER` | no | `local` (default) or `remote_vault` — default provider when no `--provider` flag is given (weekly job included) | `remote_vault` |

### Examples

Cloudflare R2:

```sh
export HEALTHMES_VAULT_ENDPOINT="https://<account-id>.r2.cloudflarestorage.com"
export HEALTHMES_VAULT_BUCKET="healthmes-vault"
export HEALTHMES_VAULT_ACCESS_KEY_ID="<r2-access-key-id>"
export HEALTHMES_VAULT_SECRET_ACCESS_KEY="<r2-secret>"
export HEALTHMES_VAULT_REGION="auto"

uv run healthmes backup create --provider remote   # local write + upload
uv run healthmes backup list --provider remote     # merged view with origins
```

Self-hosted MinIO:

```sh
export HEALTHMES_VAULT_ENDPOINT="http://localhost:9000"
export HEALTHMES_VAULT_BUCKET="healthmes"
export HEALTHMES_VAULT_ACCESS_KEY_ID="minioadmin"
export HEALTHMES_VAULT_SECRET_ACCESS_KEY="minioadmin"
export HEALTHMES_VAULT_REGION="us-east-1"          # MinIO accepts any region

uv run healthmes backup push healthmes-backup-20260709T033000Z.tar.gz.age
uv run healthmes backup restore healthmes-backup-20260709T033000Z.tar.gz.age \
    --provider remote --yes                        # downloads when not local
```

AWS S3 needs no endpoint — just bucket, credentials and region. To make the
vault the default for every `backup` invocation and the weekly job, set
`HEALTHMES_BACKUP_PROVIDER=remote_vault` in `.env`.

## 4. Compatibility & versioning rules

- `SCHEMA_VERSION` lives in `healthmes/backup/snapshot.py` and inside every
  manifest. Bump it only for layout changes a v1 reader cannot survive;
  readers must keep restoring every version ≤ their own.
- New *optional* manifest fields/sections may be added without a bump;
  readers ignore unknown fields (forward-tolerant, like the API schemas).
- The snapshot name format is part of the contract (`list_snapshots()` and
  remote vaults parse it); never localize or reorder it.
