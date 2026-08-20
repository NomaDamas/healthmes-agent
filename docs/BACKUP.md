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
Every manifest path, component archive name/root and archive member must be a
normalized relative POSIX path owned by exactly one expected component.
Absolute paths, `..` traversal, duplicate/overlapping roots, duplicate
members, kind mismatches, undeclared members and escaping symlinks are rejected
before live mutation. Safe relative symlinks from older snapshots remain
restorable when their lexically resolved target stays inside the component,
including contained legacy targets written as `./target`.
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
operational warning when this mismatch exists. Its `next_snapshot_scope`
describes only the next attempt from current configuration and source
presence; `describes_latest_snapshot=false` explicitly prevents callers from
mistaking it for inspection of the encrypted latest snapshot. Snapshot
listing alone cannot prove recoverability because the manifest is encrypted.

### Snapshot consistency

- HealthMes DB, `media/` and `raw_ingest/` are captured while the global
  HealthMes write-plane fence is held. Cooperative API/background writers
  wait, so a raw/media file and its database references cannot land in
  different captured generations. Raw ingest and media upload stage bytes
  outside the live trees, then publish bytes plus database index under this
  same fence. If a database commit succeeds but its acknowledgement is lost,
  the handler checks the expected references through a fresh session and
  retains bytes whenever the outcome is committed or cannot be proved absent;
  it never deletes bytes that may already be referenced. Retention deletion
  uses the same fence.
- SQLite ORM transactions hold a process lease until transaction cleanup.
  The lease can be released by FastAPI's dependency-cleanup thread, but
  process-lock and global-guard reuse is re-entrant only for the exact
  originating thread and asyncio task. A child task cannot inherit a context
  token, guarded PostgreSQL connection, or anchored SQLite parent descriptor
  and bypass another in-flight writer.
- The sqlite member goes through `sqlite3.Connection.backup` (source opened
  read-only), producing a transactionally consistent single-file copy with no
  dependence on `-journal`/`-wal` sidecars. The copy is logically exact but
  not byte-identical to the live file (header change counters differ).
- `pg_dump` custom-format dumps are transactionally consistent on their own.
  `pg_dump`, `pg_restore` expansion and the `psql` apply stream all have finite
  deadlines. On timeout HealthMes terminates, kills when necessary, reaps the
  client process and removes only its partial dump generation. Restore always
  attempts to re-enable target connection admission; once `COMMIT` may have
  been sent, a lost completion acknowledgement is recorded as an unknown
  outcome that requires operator inspection.
  The connection URL passed to `pg_dump`/`psql` argv is
  **credential-stripped**. URL and query-string passwords travel via
  `PGPASSWORD`, TLS key passwords via `PGSSLPASSWORD`, and passfile paths via
  `PGPASSFILE`, so those values never appear in process listings.
- Open Wearables and Hermes are captured after the HealthMes DB/media/raw
  fence. They are valid component snapshots, but the envelope is not a
  distributed point-in-time transaction across HealthMes, Open Wearables and
  Hermes.
- Retention deletion is a two-phase database/filesystem operation. The
  `StorageObject` row is marked `purged_at` and committed before its payload
  file is removed; successful file cleanup is then recorded in
  `file_cleanup_completed_at`. A lost commit acknowledgement or an unlink
  failure therefore leaves an explicit retryable state, while an already
  completed cleanup is not scanned again on every maintenance run.
- Purged rows migrated from the legacy one-phase implementation are not
  assumed complete. Current maintenance acknowledges a missing path, or
  removes an existing regular file only after matching its indexed size and
  SHA-256. Existing bytes without a digest, symlinks and replacement
  generations remain pending for inspection. If the final path and every
  deterministic staging alias are already absent, cleanup is acknowledged
  even when the legacy digest is missing or malformed; no remaining bytes are
  inferred from corrupt metadata.
- Media/raw publication keeps a same-inode staging link through database
  commit. Startup and storage maintenance scan only the bounded
  `.staging/media` and `.staging/raw_ingest` namespaces. They remove a
  duplicate only when the committed storage index, size, SHA-256 and live
  file generation all match; they restore a missing live link only from that
  same proof. Unindexed, legacy, symlinked or conflicting artifacts are
  preserved and reported for operator inspection.
- Crash durability for directory-entry creation, rename and deletion is
  currently supported only on POSIX filesystems with working directory
  `fsync`. A Windows companion may connect to a HealthMes node, but running
  the Personal Data Node itself on Windows fails closed with
  `DurabilityUnsupportedError` for SQLite runtime/write lock files,
  media/raw publication, retention cleanup, backup and restore rather than
  relying on pathname-only reparse-point checks or claiming an unproven
  durable commit.
- The whole envelope passes through memory once during encrypt/decrypt
  (pyrage's passphrase API is bytes-based) — fine at personal scale.

### File recovery journals and reconciliation order

HealthMes uses three private recovery artifacts. They are deliberately not
interchangeable:

| Owner | On-disk shape | Recovery authority |
|---|---|---|
| Generic durable unlink | `.healthmes-unlink-v2-<uuid>/metadata.json` plus optional `payload` | The self-describing metadata records the original basename and exact file generation. Startup/storage reconciliation may finish this deletion without consulting a caller-specific database row. |
| Retention payload quarantine | `.healthmes-storage-delete-<name-hash>-<uuid>/payload` | The matching `StorageObject.file_cleanup_identity` is the authority. Only storage maintenance may remove it and acknowledge `file_cleanup_completed_at`. |
| Retention cleanup journal | `raw_ingest/.healthmes-storage-delete-journal/.healthmes-storage-cleanup-v1-<object-id>-<state>.json` | A canonical, fsynced record binding one pending `StorageObject` to the exact inode generations guarded during deletion. Storage maintenance is the only writer. |

The retention cleanup journal has three mutually exclusive state records:

| State | Meaning |
|---|---|
| `intent` | Written and fsynced before any proved HealthMes-owned name is unlinked. It contains the object ID, relative path, normalized cleanup identity and guarded device/inode generations. |
| `complete` | Written and fsynced only after every guarded file descriptor reports `st_nlink == 0`. It allows a later run to restore a lost second-database-commit acknowledgement without deleting bytes again. |
| `manual-review` | Records an ambiguous physical result such as a hard link created during final unlink. Automatic cleanup remains blocked until an operator resolves the generation. |

`complete` and `manual-review` both include the SHA-256 of the canonical
`intent`, so a state file cannot be attached to a different cleanup attempt.
Malformed, non-canonical, conflicting, ownerless or active-object journals are
preserved and reported; they are never deleted merely because they are old.
Journal files are retired only after the matching database row durably records
`file_cleanup_completed_at`.

The older durable-unlink format,
`.healthmes-unlink-<uuid>-<original-filename>`, contains no self-describing
metadata. A general startup scan therefore preserves and reports it. A
target-specific retention retry may remove it only when the committed
`StorageObject` identity proves that the legacy quarantine still contains the
exact generation scheduled for deletion. During that retry, the identity is
upgraded from version 1 to version 2 so known staging aliases remain
recoverable.

Reconciliation runs under the same global HealthMes write-plane fence in this
order:

```text
1. Recover self-describing v2 durable-unlink journals.
2. Reconcile exact DB-indexed media/raw staging aliases.
3. Bounded-scan the remaining staging namespaces and preserve unknown entries.
4. Inspect the bounded central retention-journal namespace; preserve and report
   malformed, ownerless or active-object entries, and retire only journals whose
   matching database cleanup acknowledgement is already durable.
5. Commit retention tombstones and their versioned cleanup identities.
6. Fsync `intent`, remove only the proved live/staging/quarantine generations,
   prove final link count zero, then fsync `complete` (or `manual-review`).
7. Commit file_cleanup_completed_at and resolve crash-stranded PurgeJob rows.
8. Retire the matching intent/state journal only after that commit succeeds.
```

All private durable-unlink, retention-quarantine and central-journal
namespaces are excluded from unindexed-object discovery and usage
measurement. Bytes remain on disk until recovery succeeds, but they are not
re-imported as new wellness data or double-counted as active
`raw_payload`/`media`.

The entire `raw_ingest/.healthmes-storage-delete-journal/` subtree is reserved,
not only filenames that match the current journal schema. Malformed, unknown
and future-version entries therefore cannot be reclassified as ordinary raw
payloads or deleted by retention; only the bounded cleanup-journal reconciler
may inspect and report them. Usage measurement counts no symlink target bytes:
only no-follow regular files inside the data tree contribute to quota totals.

Five bounded scan cursors keep that recovery work fair across maintenance
runs:

| Cursor | Role |
|---|---|
| `{data_dir}/.healthmes-recovery/unlink-recovery-v1.json` | Round-robin directory queue, kernel offsets, retry names and directory generations for generic durable-unlink recovery. |
| `{data_dir}/.staging/.healthmes-unindexed-discovery-v2.json` | Independent resumable DFS stacks for legacy `media` and `raw_ingest` payload discovery, so neither class can starve the other. |
| `{data_dir}/.staging/.healthmes-staging-index-cursor-v1.json` | Keyset position for exact `StorageObject`-derived staging reconciliation. |
| `{data_dir}/.staging/.healthmes-staging-fallback-cursor-v1.json` | Independent resumable DFS stacks, kernel offsets and directory generations for the remaining unindexed staging tree. |
| `{data_dir}/.staging/.healthmes-storage-cleanup-scan-cursor-v1.json` | Directory identity, kernel offset and in-batch position for the central retention-journal namespace, so persistent malformed, ownerless or active entries cannot starve later journals. |

One storage-maintenance call creates one shared absolute budget for unindexed
discovery hashing, every directory scan, namespace mutations, cleanup-journal
publication, retention quarantine and generic durable unlink/recovery:

| Setting / env var | Default | Cumulative boundary for one run |
|---|---:|---|
| `storage_maintenance_timeout_seconds` / `HEALTHMES_STORAGE_MAINTENANCE_TIMEOUT_SECONDS` | 10 s | Absolute deadline for filesystem work while the global write-plane fence is held. |
| `storage_maintenance_max_hash_bytes` / `HEALTHMES_STORAGE_MAINTENANCE_MAX_HASH_BYTES` | 256 MiB | Payload bytes hashed by discovery and cleanup identity verification. |
| `storage_maintenance_max_directory_entries` / `HEALTHMES_STORAGE_MAINTENANCE_MAX_DIRECTORY_ENTRIES` | 4,096 | Directory entries scanned plus namespace create/link/rename/unlink/rmdir mutations. |

Pending purged-row retries are prepared before unindexed discovery and before
new tombstones. Once the shared budget is exhausted, the current unfinished
candidate and every later candidate remain pending, persisted cursors retain
completed progress, and a fresh maintenance run continues with a new budget.
Within the shared entry budget, DB-indexed staging paths receive at most three
quarters of a multi-entry slice and fallback retains at least one quarter;
unused indexed capacity is lent to fallback. With a one-entry slice, the index
cursor's persisted `next_pass` alternates the indexed and fallback passes.

A fallback root that completed during one bounded slice is re-armed on the
next slice while the other root is still in progress. This intentionally
rechecks the completed namespace: metadata on the root directory cannot reveal
a new file created inside an already existing deep descendant. Each root still
receives at most the bounded round-robin quantum, so neither can starve the
other.

Their control directories are owner-only (`0700`) and cursor files are
owner-only regular files (`0600`). Updates use a same-directory temporary
file, file `fsync`, atomic replace and directory `fsync`. A missing, malformed,
unsafe or stale cursor starts a safe new sweep; it never authorizes deleting a
payload. These files are operational control state, not wellness objects:
they are excluded from `StorageObject` discovery, active usage/quota and
retention. The root `.healthmes-recovery/` and `.staging/` control trees are
also outside the selected snapshot components, so backup and restore recreate
progress from the durable database/filesystem state instead of transporting a
stale scan position.

Cursor publication and a pre-reserved terminal cleanup capsule are small,
fixed crash-progress records. If the shared deadline expires just after a
destructive durable transition has begun, HealthMes may finish that capsule or
publish the cursor so the next run can prove where to resume. This exception
does not start another candidate, hash more payload bytes or continue an
unbounded scan after the deadline.

Staging cursor names are reserved only at their exact direct-child path below
`.staging/`. A user payload with the same basename below `media/` or
`raw_ingest/` remains a normal indexed and measured object.

Local usage measurement is a separate, no-follow filesystem pass capped at
100,000 directory entries and two seconds. Scheduled maintenance and
`POST /v1/storage/maintenance` run that pass only after releasing the global
write-plane fence, so a large data tree cannot hold every writer behind quota
accounting. The measurement is published only after the complete data root has
been scanned and its root inode revalidated. A missing root, permission error,
replacement root or exhausted bound therefore leaves the previous
`storage_usage_daily` values unchanged; the API reports the measurement as
deferred and a later run retries it.

Each usage measurement also writes zero bytes and zero objects to any existing
current-day local class row that no longer has a regular file. Removing the
last payload, or replacing it with an external symlink, cannot leave a stale
non-zero quota measurement.

The retention cleanup identity records file kind, device/inode generation,
size, timestamps, digest, link count and known HealthMes staging aliases.
Cleanup opens the proved inode before unlinking names and requires its final
link count to be zero. If another hard link appears, a path is replaced, or a
quarantine no longer matches, HealthMes reports the object as pending and
preserves the ambiguous generation instead of claiming reclaimed bytes.
The database also enforces the cleanup state boundary: an active object cannot
carry cleanup identity/completion metadata, any populated cleanup identity
requires `purged_at`, and `file_cleanup_completed_at` requires both
`purged_at` and a populated identity. SQL `NULL` and JSON `null` are both
treated as an unset identity so SQLite and PostgreSQL enforce the same rule.
Deleting any completed journal resets the bounded journal cursor to the start;
directory cookies can otherwise skip entries after the directory mutates.

`POST /v1/storage/maintenance` reports:

| Field | Exact meaning |
|---|---|
| `candidates` | Active storage rows currently eligible for a retention purge. |
| `records_purged` | Rows made unreachable by this live run; always `0` for dry-run. |
| `files_deleted` | Storage-object cleanup operations that removed at least one proved HealthMes-owned name. |
| `file_cleanup_pending` | Unresolved `StorageObject` rows (`purged_at` set and `file_cleanup_completed_at` unset) after a live run; for dry-run, the unresolved count already present at preview time. Dry-run candidates are not included because no tombstone is committed. |
| `deleted` | Compatibility alias for `files_deleted`; it is not a row-purge count. |
| `bytes_reclaimed` | Bytes credited only after every proved HealthMes-owned link to a regular-file generation is gone and no unknown hard link remains. |
| `decision_receipt_candidates` | Compact decision receipts eligible for retention deletion. |
| `decision_receipts_deleted` | Compact decision receipts deleted by a live run; always `0` for dry-run. |
| `budget_exhausted` | Whether the shared maintenance budget stopped this run. |
| `budget_resource` | Exhausted `deadline`, `hash_bytes` or `directory_entries`; otherwise `null`. |
| `budget_phase` | Exact maintenance phase that exhausted the budget; otherwise `null`. |
| `errors` | Failures observed by this maintenance attempt. A nonzero pending count remains authoritative even when this list is empty. |

Each `PurgeJob.detail` also records cleanup candidate/completed/pending/retry
counts, affected object IDs, row-purge count and physical cleanup count. When
a later run finishes files from a crash-stranded job, that earlier job is
closed with `file_cleanup_pending: 0` and the recovering job ID/timestamp; it
is never marked completed while still claiming pending cleanup.

### Restore transaction boundary

Before changing live data, restore validates the encrypted archive, verifies
every included component, checks every configured destination, stages every
local file/tree beside its destination on the same filesystem, validates
SQLite with `PRAGMA quick_check`, inspects PostgreSQL dumps, and checks target
connectivity. An included Open Wearables dump, media tree, raw-ingest tree or
Hermes tree without its target configuration is an error; it is never
silently skipped.

Local SQLite/filesystem components use atomic same-filesystem renames while
keeping rollback copies until all components finish. A later failure restores
the prior local generation while the write fence is still held. SQLite
restore is an **offline/cooperatively locked protocol**: a file-backed
HealthMes runtime holds a process-lifetime lock from before engine creation
until after engine disposal, and restore fails before mutation while that
runtime is active. This prevents pooled SQLite connections from staying
attached to the pre-restore inode. Stop every HealthMes process using the
target SQLite file before restore. A host crash or filesystem failure can
still leave staging/rollback artifacts for an operator to inspect.

If every component was applied but write-fence cleanup itself reports an
error after releasing the lock, HealthMes keeps the completed restored
generation and reports that exact state. It never performs an out-of-fence
rollback that could erase a writer that committed after unlock. If rollback
and fence release both fail, the operator-facing error preserves the original
restore/rollback/cleanup details and appends the fence-release failure rather
than replacing the first error.

For each PostgreSQL database, `pg_restore --exit-on-error` first expands the
custom archive completely into an anonymous temporary SQL file. A failed
expansion cannot mutate the target. HealthMes then gives that complete SQL to
one `psql` connection; the physical identity assertion runs first and the
restore SQL follows under the same `--single-transaction`. PostgreSQL cannot
commit atomically with filesystem swaps or another PostgreSQL database.
Therefore a restore spanning PostgreSQL plus any other component fails before
mutation by default. An operator who has stopped all affected services may
explicitly accept this unavoidable boundary with
`--allow-cross-store-partial`; a later failure still rolls local components
back and reports an error, but an already committed PostgreSQL database cannot
be automatically reverted. Success is never reported for a known mixed
generation. A failed `psql` process can still lose the server's final commit
acknowledgement, so HealthMes reports that component as
`commit outcome unknown` and requires target inspection before retrying. An
identity assertion failure is different: it occurs before restore SQL in the
same transaction and is reported as not started, not commit-ambiguous.

Preflight reads PostgreSQL's cluster system identifier and target database OID
with a read-only `psql` query. HealthMes rejects two configured restore URLs
that resolve through DNS aliases, `hostaddr`, service aliases, or query
overrides to the same physical database before any restore command runs. The
same physical identities are checked again immediately before live mutation
and once more before each restore. The final assertion runs inside the same
`psql` connection and transaction that executes the restore SQL, so routing or
failover drift cannot open a check/use gap before destructive statements.

## 2. Using it

```sh
export HEALTHMES_BACKUP_PASSPHRASE='correct horse battery staple'

uv run python -m healthmes backup create
uv run python -m healthmes backup list
uv run python -m healthmes backup restore healthmes-backup-20260709T033000Z.tar.gz.age        # dry-run: prints manifest
uv run python -m healthmes backup restore healthmes-backup-20260709T033000Z.tar.gz.age --yes  # applies (destructive)
```

Successful dry-run validation exits with status `2` and prints a shell-quoted
apply command that preserves the selected provider, passphrase file and
cross-store acknowledgement flags.

Stop HealthMes, Open Wearables and any writer using the restore targets before
an applied restore. The CLI prints the recovery mode plus the exact
`recovered` and `not in snapshot` component lists. For the explicit
cross-store exception:

```sh
uv run python -m healthmes backup restore <name> --yes \
  --allow-cross-store-partial
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

Resource limits apply to new snapshots, local restores and Remote Vault
downloads. Operators restoring an older, larger snapshot must raise only the
specific bound required for that trusted snapshot, validate it with the
non-mutating restore preview, and return the setting to its normal value
afterward.

| Setting / env var | Default | Enforced boundary |
|---|---:|---|
| `backup_max_encrypted_bytes` / `HEALTHMES_BACKUP_MAX_ENCRYPTED_BYTES` | 512 MiB | Encrypted age envelope size. |
| `backup_max_decrypted_bytes` / `HEALTHMES_BACKUP_MAX_DECRYPTED_BYTES` | 768 MiB | Decrypted gzip/tar bytes held before extraction. |
| `backup_max_archive_members` / `HEALTHMES_BACKUP_MAX_ARCHIVE_MEMBERS` | 100,000 | Tar member count. |
| `backup_max_member_bytes` / `HEALTHMES_BACKUP_MAX_MEMBER_BYTES` | 1 GiB | Expanded size of one regular member. |
| `backup_max_expanded_bytes` / `HEALTHMES_BACKUP_MAX_EXPANDED_BYTES` | 4 GiB | Total expanded regular-file bytes. |
| `backup_max_identity_depth` / `HEALTHMES_BACKUP_MAX_IDENTITY_DEPTH` | 128 | Maximum directory depth while binding staged, live, and rollback generations to the restore journal. |
| `backup_identity_traversal_timeout_seconds` / `HEALTHMES_BACKUP_IDENTITY_TRAVERSAL_TIMEOUT_SECONDS` | 300 s | Absolute deadline shared by all descriptor-bound identity traversals in one staging, apply, rollback, recovery, or cleanup phase. |
| `backup_max_compression_ratio` / `HEALTHMES_BACKUP_MAX_COMPRESSION_RATIO` | 100 | Expanded-to-compressed ratio. |
| `backup_min_free_bytes` / `HEALTHMES_BACKUP_MIN_FREE_BYTES` | 256 MiB | Free-space reserve retained during snapshot, download and restore staging. |
| `backup_postgres_tool_timeout_seconds` / `HEALTHMES_BACKUP_POSTGRES_TOOL_TIMEOUT_SECONDS` | 1,800 s | Per-operation deadline for `pg_dump`, `pg_restore`, and `psql`; increase for large or remote databases. |

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
HealthMes DB rows plus binary `media/` and `raw_ingest/` files, including real
`RawIngestEvent -> StorageObject -> WellnessEvent -> bytes` references. It
snapshots them, deletes all three live stores, restores, byte-verifies files
and references, and reopens the database. Separate concurrency tests prove
that ingest publication and retention deletion wait behind the snapshot
fence.

### Manual follow-up for unresolved file cleanup

Do not delete a `.healthmes-unlink-*` or
`.healthmes-storage-delete-*` entry merely because it looks old. Its
timestamp does not prove that no process or committed row owns it.

1. Stop duplicate HealthMes writers and preserve a filesystem copy or
   encrypted snapshot before manual inspection.
2. Restart one current HealthMes node. Startup reconciliation safely handles
   self-describing durable-unlink journals and DB-proved staging aliases.
3. Preview storage maintenance and inspect `file_cleanup_pending` and
   `errors`:

   ```sh
   curl -sS -X POST \
     'http://127.0.0.1:8100/v1/storage/maintenance?dry_run=true'
   ```

4. Run one live maintenance attempt:

   ```sh
   curl -sS -X POST \
     'http://127.0.0.1:8100/v1/storage/maintenance'
   ```

5. If pending cleanup remains, use the `PurgeJob.detail` object IDs and error
   paths to inspect the corresponding `StorageObject` rows and filesystem
   generations. A malformed identity, replacement generation, unknown hard
   link, symlinked parent, or unrecognized legacy entry requires operator
   investigation; do not repair it with a blind `rm` or by setting
   `file_cleanup_completed_at` manually.

An exact legacy durable-unlink entry may disappear only through a matching
target-specific retry. Unknown or malformed quarantines are intentionally
left in place so an operator can recover bytes rather than lose them through
an unsafe automated guess.

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

### Local-first creation and remote-authoritative restore

The vault is a **replication target**, never the primary store:

- `backup create --provider remote` writes the local snapshot first, then
  uploads a byte-identical copy. The local file stays unless you pass the
  explicit `--remote-only` flag (then the vault holds the only copy and the
  CLI says so on stderr).
- `backup push <name>` uploads an already-existing local snapshot.
- `backup restore <name> --provider remote` always fetches that named remote
  object. It verifies the remote digest and atomically replaces any same-name
  local cache before running the exact local pipeline (decrypt → strict
  validation → preflight/stage → recoverable swaps/transactional database
  restore). If the remote object is missing or invalid, restore fails; it never
  silently substitutes different local bytes.
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
  uses a conditional create and never deletes remote objects except to clean
  up its own failed upload. A same-second name collision is retried with
  `-2`, `-3`, and so on without replacing the existing object.
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
- Before any vault request, the selected encrypted file descriptor is copied
  into an anonymous private temporary file. HealthMes validates the age
  envelope and manifest, hashes it and uploads only that sealed private
  generation. Replacing or overwriting the named local snapshot during this
  interval cannot turn the outbound body into plaintext or another file.
- Downloads land atomically (`.part` + rename). For remote restore the
  verified remote object is authoritative and replaces any same-name local
  cache; corruption is additionally caught by age's authenticated encryption
  and the manifest inventory check during restore.
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
    --provider remote --yes                        # refreshes verified local cache
```

AWS S3 needs no endpoint — just bucket, credentials and region. To make the
vault the default for every `backup` invocation and the weekly job, set
`HEALTHMES_BACKUP_PROVIDER=remote_vault` in `.env`.

## 4. Compatibility & versioning rules

- `SCHEMA_VERSION` lives in `healthmes/backup/snapshot.py` and inside every
  manifest. Bump it only for layout changes a v1 reader cannot survive;
  readers must keep restoring every version ≤ their own.
- New optional metadata fields inside an existing component may be added
  without a bump; readers ignore unknown metadata fields. A new archived
  component or layout is not merely metadata and requires a schema-version
  bump so an older reader cannot silently omit data during restore.
- The snapshot name format is part of the contract (`list_snapshots()` and
  remote vaults parse it); never localize or reorder it.
