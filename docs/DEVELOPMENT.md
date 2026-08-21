# Development Guide

HealthMes Agent glue code lives at the repo root (`healthmes/`, `config/`,
`scripts/`, `skills/`, `tests/`). `vendor/` contains read-only upstream
snapshots (`hermes-agent`, `open-wearables`) — **never modify anything under
`vendor/`**; all integration happens via config rendered outside the vendor
trees, the root `Makefile`/`scripts/`, and the root `docker-compose.yml`.
Architecture: `docs/PLAN.md`.

There are two run paths. **Mac-native is the primary one** (this stack is
developed and run directly on macOS); docker compose is the alternative for
a full one-command stack.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (manages Python 3.12 automatically via
  `.python-version` — no system Python needed). The vendored open-wearables
  backend targets Python 3.13 and asks for uv >= 0.9.17; uv downloads the
  toolchain on demand.
- Homebrew (mac-native path — installs `postgresql@16` + `redis` on demand)
- Docker + Docker Compose v2.24+ (only for the compose path)

## Mac-native quickstart (primary)

```bash
make mac-setup            # brew install postgresql@16 + redis (if missing),
                          # initdb ./data/pg, start pg+redis, create the
                          # open-wearables + healthmes databases, uv sync
install -m 600 .env.example .env  # optional: tweak HEALTHMES_* values
make mac-run              # alembic upgrade head + uvicorn :8100
curl http://localhost:8100/health   # -> {"status":"ok"}
make mac-test             # uv run pytest -q
```

The service exposes `/health`, the REST surface under `/v1/*` (including the
companion-app glance briefing at `/v1/briefing/glance`), the cognitive-energy
forecast at `/cognitive-energy/forecast`, the decision viewer at `/decisions`
+ `/decisions/{id}` (Mermaid, served fully locally), the weekly report at
`/reports/weekly` (+ `.json` twin), the read-only calendar-connection status
page at `/connect` (see "캘린더 연결"), and the Layer-B MCP server
(Streamable HTTP) at exactly `/mcp`.

Background jobs (the 10-minute trigger sweep, the hourly cognitive-energy
persist and the weekly encrypted backup) are all registered at startup but
only run when `HEALTHMES_SCHEDULER_ENABLED=true` — keep it off in tests and
one-off tooling.

Everything is ephemeral and repo-local — postgres runs out of `./data/pg`
via `pg_ctl`, redis daemonizes with `./data/redis.pid`, and **nothing is
registered with `brew services`** (no autostart). Stop the services with
`make mac-services-stop`; all targets are idempotent and re-runnable.

| Target | What |
|---|---|
| `make mac-setup` | one-time bootstrap (safe to re-run): brew pkgs, `initdb` `./data/pg`, create `open-wearables` + `healthmes` DBs/roles, `uv sync` |
| `make mac-services-start` | start ephemeral postgres (`pg_ctl`) + redis (pidfile `./data/redis.pid`) |
| `make mac-services-stop` | stop both (the inverse — leaves data dirs intact) |
| `make mac-services-status` | report what is running |
| `make mac-run` | `alembic upgrade head` + healthmes on `HEALTHMES_PORT` (8100) |
| `make mac-test` | `uv run pytest -q` |
| `make mac-ow` | best-effort native boot of the vendored open-wearables backend (see below) |
| `make mac-ow-worker` | its celery worker (**requires redis** from `mac-services-start`) |
| `make mac-ow-beat` | its Celery Beat scheduler (**requires redis** from `mac-services-start`) |
| `make compose-config` | validate `docker-compose.yml` without a daemon |

Notes:

- **Zero-setup mode:** with no `.env`, healthmes defaults to a repo-local
  sqlite database (`sqlite:///./data/healthmes.db`) — `make mac-run` works
  without postgres. For the postgres-backed run, uncomment the
  `postgresql+psycopg://...@localhost:5432/healthmes` line in `.env`
  (database/role are created by `mac-setup`).
- **`make mac-ow`** boots `vendor/open-wearables/backend` natively per its
  own README/start scripts: it source-exports `config/open-wearables.env`
  (falling back to the `.example`, localhost defaults), redirects the venv
  to `./data/ow-backend-venv` (vendor tree stays untouched), runs `uv sync`,
  then `scripts/start/app.sh` (migrations + seeds + `fastapi dev` on :8000
  when `ENVIRONMENT=local`). Requires postgres from `mac-services-start`.
  The svix webhook-registration step retries and is non-fatal (no svix
  server in this stack).
- **Celery worker, Beat & redis:** provider syncs and score jobs run in the
  Celery worker (`make mac-ow-worker`), while the separate Celery Beat process
  (`make mac-ow-beat`) enqueues periodic syncs. Both need redis as
  broker/result backend — start it via `make mac-services-start` first.
  Without the worker tasks cannot run; without Beat an existing connection
  stays stale until a sync is enqueued manually.
- postgresql@16 is keg-only; the scripts call binaries via
  `$(brew --prefix postgresql@16)/bin` — no PATH changes needed.

### Oura OAuth dogfooding (mac-native)

Create an OAuth application in the
[Oura Cloud developer portal](https://cloud.ouraring.com/oauth/applications).
The registered redirect URI must match this value exactly:

```text
http://localhost:8000/api/v1/oauth/oura/callback
```

Copy the root example and put the Oura credentials only in the ignored local
file:

```bash
install -m 600 config/open-wearables.env.example config/open-wearables.env
```

```dotenv
OURA_CLIENT_ID=replace-with-client-id
OURA_CLIENT_SECRET=replace-with-client-secret
OURA_DEFAULT_SCOPE="personal daily heartrate workout session spo2 ring_configuration heart_health"
API_BASE_URL=http://localhost:8000
HISTORICAL_SYNC_ON_CONNECT=false
```

`daily` includes Oura's daily sleep, activity, and readiness summaries. Do not
add the legacy separate `activity` scope when the Oura application UI does not
offer it. Never paste the client secret into Git, screenshots, issue comments,
or chat logs. Setting `HISTORICAL_SYNC_ON_CONNECT=false` disables the vendored
grace-period backfill so the explicit historical task below is the single sync
run being verified.

Start the data plane in separate terminals so the API, background sync worker,
and periodic Celery Beat scheduler are all present:

```bash
make mac-services-start
make mac-ow
```

```bash
make mac-ow-worker
```

```bash
make mac-ow-beat
```

Open <http://localhost:8000/docs> and use the OpenAPI operations in this order:

1. Click **Authorize** and enter the developer email and password from the local
   `ADMIN_EMAIL` and `ADMIN_PASSWORD` settings. The OpenAPI UI calls
   `POST /api/v1/auth/login` for the bearer token; do not copy the token into
   notes or logs.
2. Select or create the local test user with `GET /api/v1/users` or
   `POST /api/v1/users`.
3. Call `GET /api/v1/oauth/{provider}/authorize` with `provider=oura` and the
   selected `user_id`, then open the returned `authorization_url`. Complete
   consent in Oura; Oura redirects to the registered concrete callback URI and
   the callback should report a successful connection.
4. Confirm `GET /api/v1/users/{user_id}/connections` contains an `oura`
   connection with `status: active`.
5. Queue `POST /api/v1/providers/{provider}/users/{user_id}/sync/historical`
   with `provider=oura` and `days=90`. A successful request returns
   `success: true` and a task ID.
6. Confirm the corresponding Oura run reaches `status: success` in
   `GET /api/v1/users/{user_id}/sync/runs`. This proves the Celery worker
   completed the historical task, not merely that the API accepted it.
7. Query `GET /api/v1/users/{user_id}/summaries/sleep` and
   `GET /api/v1/users/{user_id}/health-scores?provider=oura&category=readiness`
   with a date range that covers days present in the Oura account. At least one
   non-empty response proves the personal-date sleep/readiness data path.

Record only sanitized evidence:

```text
repo commit: <commit>
Oura connection: active | blocked
sync: success | failed
sleep/readiness data: returned | empty
first blocker: <first failure or none>
screenshot/log: <redacted path or summary>
```

Do not record credentials, bearer/refresh tokens, authorization codes, email
addresses, user IDs, task IDs, or raw health payloads. A `200` from `/docs`
proves only that the API is serving; it does not prove provider sync unless the
Celery task also completes.

## Run the healthmes service directly

```bash
uv sync                              # if you skipped mac-setup
uv run alembic upgrade head          # required for an existing database
uv run python -m healthmes           # uvicorn on HEALTHMES_PORT (default 8100)
```

For auto-reload during development:

```bash
uv run alembic upgrade head
uv run uvicorn healthmes.app:create_app --factory --reload --port 8100
```

Both entrypoints refuse to serve an existing database whose Alembic revision
is not the current head. A completely empty SQLite database remains
zero-setup and is initialized on first startup.

Without `HEALTHMES_API_TOKEN`, the app factory accepts only actual loopback
socket peers even if Uvicorn is accidentally bound to `0.0.0.0`. LAN, proxy,
container, phone, and other remote clients require a token.

## Agent plane: Hermes decision bootstrap

The HealthMes decision runtime is configured entirely from outside `vendor/`.
`scripts/bootstrap.py` renders
`config/hermes-decision-config.yaml.tmpl` into the isolated
`$HERMES_HOME/decision/config.yaml` and creates the profile manifest and
attestation key. It does not install HealthMes skills, scripts, webhooks,
Telegram configuration, or new reasoning jobs into the general Hermes home.

The dedicated profile requires `compression.in_place: true`. Hermes may
compress a long request-scoped transcript, but it must keep the same session
ID so HealthMes can delete the exact session returned by `/v1/responses`
instead of losing track of a rotated session.

```bash
uv run python scripts/bootstrap.py --dry-run     # show what would change
uv run python scripts/bootstrap.py               # native run (HERMES_HOME=~/.hermes)
uv run python scripts/bootstrap.py --mode docker # compose paths (HERMES_HOME=./data/hermes)
```

After rebuilding or replacing the `hermes-decision` Docker image, revoke the
old container execution seal before starting the replacement:

```bash
docker compose stop --timeout 360 hermes-decision
uv run python scripts/bootstrap.py --mode docker --refresh-runtime-seal
docker compose up -d --build --force-recreate hermes-decision
```

Stop the old supervisor first so it cannot reseal the old image between
refresh and replacement. The new supervisor seals the new container artifacts
on startup. A normal bootstrap rerun deliberately preserves an equivalent
seal, so image replacement without the explicit refresh fails closed instead
of silently trusting different runtime files. The explicit 360-second stop
timeout matches the service's `stop_grace_period` and exceeds the maximum
supported 300-second overall decision wall clock plus the supervisor's bounded
10-second child TERM wait and 5-second post-SIGKILL wait.

The canonical native launcher serializes every decision-runtime mutation with
the atomic directory lock
`data/runtime/hermes-decision-lifecycle-lock/`. Version 2 of its strict owner
record binds the `start`, `stop`, `update`, `install`, or `uninstall`
operation to the shell PID, a native OS start token (`/proc` start ticks on
Linux or `libproc` start seconds/microseconds on macOS), nonce, acquisition and
update epochs, transaction phase, lifecycle contract version, and the exact
`healthmes_local.sh` SHA-256 seen by that shell. The token is independent of
timezone and locale. Version-1 `ps` records remain readable, but a live PID
whose formatted token differs is unknown and is never treated as a dead owner.
A live owner is waited for for at most 10 seconds; unreadable or malformed
identity fails closed. A waiting shell re-hashes the on-disk script on every
attempt. After `git pull`, the update holder re-hashes the script. If the
digest changed, it replaces itself with the newly pulled script by `exec`
without changing its PID or releasing the lifecycle lock. The new script
validates the native owner start token, nonce, exact `pulling` journal
generation, prior digest, and compatible lifecycle contract before atomically
moving the journal to its own digest and continuing setup/restart. The restart
decision is passed explicitly, the caller environment is inherited, and a
one-shot internal handoff marker prevents recursive re-exec. Any identity,
generation, digest, or contract mismatch fails closed and preserves the
durable journal instead of running stale in-memory functions.

A verified dead `start` or `stop` owner may be recovered only after the
two-second stale grace, and recovery never signals its numeric PID. A dead
non-complete `update`, `install`, or `uninstall` journal is instead atomically
advanced to `repair_required` and preserved; later lifecycle commands fail
closed until an operator performs an explicit, validated repair. A completed
durable transaction whose owner died just before lock removal may be removed
after the same identity and age checks. `update` holds the lock from the
initial decision stop through `git pull`, setup, and generation handoff.
`uninstall` holds it across LaunchAgent unload, application stop,
`services-stop`, and runtime/local-data cleanup. Cleanup excludes the lock
directory itself and the permanent
`data/.hermes-decision-runtime-transition.lock`; only after successful
transaction completion is the lifecycle directory released. The transition
mutex is intentionally retained across uninstall so waiters can never lock
different inodes after cleanup.
Durable subcommands run with Bash `errexit` active, so a failed pull, setup,
service stop, or cleanup cannot be hidden by a later successful command; the
transaction remains `repair_required`. Therefore a new start cannot execute
partially updated code or race a partial uninstall.

Lifecycle acquisition first writes a complete owner record into a private
sibling staging directory, then publishes the whole directory with an
OS-native exclusive rename under the permanent transition mutex. Canonical
lock directories are therefore never intentionally visible without a complete
record. Record rewrites use an external temp file and replace the canonical
record only if its pre-read SHA-256 still matches under that same mutex.
Removal similarly verifies the record digest and atomically retires the whole
directory before best-effort cleanup. A crash can consequently leave only a
non-authoritative staging, write-temp, or retired artifact; it cannot expose a
new empty canonical directory or move the canonical record away first. Such
non-authoritative artifacts do not block a later generation.

For compatibility with interrupted older launchers, exactly one valid
`.record.*`, `.lifecycle-lock-record.*`, or `.startup-lease-record.*` candidate
may be restored only after its recorded owner is proved absent and the stale
grace has elapsed. A live or unreadable owner, multiple candidates, malformed
content, symlinks, or a changed candidate fail closed and are preserved.
An ownerless empty directory contains no identity evidence, so it remains
`unknown` for explicit operator repair rather than being guessed abandoned.

While holding the lifecycle lock, start atomically creates the version-2
`data/runtime/hermes-decision-startup-lease/` before spawning the managed Bash
wrapper by the same complete-stage/exclusive-rename protocol. The strict lease
contains `created_at_epoch`, `updated_at_epoch`, the startup-owner identity,
the launcher service nonce, and a phase:
`intent`, `spawned`, `identity_verified`, or `failed`. The wrapper's first
managed action atomically changes `intent` to `spawned` with its own PID.
That lease record is the only initial Decision Runtime publication: the
parent does not require or create a separate PID tombstone before identity
verification. The parent captures all five `ps` identity fields, atomically
writes the complete launcher identity, changes the lease to
`identity_verified`, and removes the exact lease generation.
One failed or unreadable identity query is unknown, not evidence that the
process is absent.
Every configured `PS_BIN` invocation used by lifecycle acquisition or startup
recovery runs in a separate process group with a one-second per-snapshot cap
inside the shared 10-second lock or three-second recovery deadline. Timeout
cleanup kills that probe group and bounds direct-child reap, so a non-returning
wrapper or inherited pipe cannot extend either lifecycle budget.

If the startup owner crashes, stop waits through a bounded three-second
publication grace and repeatedly checks for a matching v3 budget. A matching
budget can recover the verified Python supervisor even when wrapper metadata
or the PID tombstone is missing. Without one, an `intent`/`failed` lease is
removed only after the recorded startup owner is verified absent; a phase
with a launcher PID additionally requires the exact wrapper to be absent and
its native process group to be proven empty. A reused PID, unreadable process
state, live group member, malformed record, or generation mismatch preserves
all diagnostics and fails closed. Therefore stop/update cannot report success
or delete metadata while a startup generation remains unverified.

Generic HealthMes and Open Wearables launchers use a separate bounded recovery
record. Before publishing the numeric PID, the managed Bash wrapper atomically
records its PID, native OS start token, service nonce, and expected service
name. If the initial `ps` snapshot fails, a later start or stop may reconstruct
the full identity only when the native token still matches and a bounded
snapshot proves the same PID/PGID, Bash wrapper command marker, nonce, and
service. Absence or proven PID reuse retires only that exact recovery
generation. A marker mismatch while the native token still identifies the same
live process remains unknown and cannot trigger cleanup or a duplicate launch.
Malformed, conflicting, timed-out, or unreadable evidence also remains fail
closed. Shutdown-budget integers use unsigned ASCII decimal digits in every
parser, and all managed runtime PIDs must be at least 2. Compose enables its
init shim so the managed Python supervisor never occupies container PID 1.

Immediately before Uvicorn calls its normal startup implementation, the
Python supervisor publishes an atomic
`data/runtime/hermes-decision-stop-budget` record that binds that exact value
to the running launcher PID, OS start token, service nonce, the actual Python
supervisor PID/native start token, and a unique publication instance nonce
before Uvicorn's first startup operation runs the ASGI lifespan that may
launch Hermes in its separate process group.
The owning supervisor removes its exact record only after controller cleanup
has successfully drained and reaped the Hermes child group. If cleanup fails,
the record remains as an explicit incomplete-cleanup diagnostic. Valid v3
records use their saved drain plus a bounded 2-second native launcher margin.
Legacy v1/v2 records retain their conservative 315-second drain plus that
margin. An existing malformed record is preserved byte-for-byte and a new
supervisor refuses to overwrite it without explicit validated repair.
For legacy `ps:` owner identities, a failed, timed-out, or empty `ps` probe is
unknown while the numeric PID still exists, so the existing record is
preserved. A formatted token mismatch for a still-live numeric PID is also
unknown. Replacement is allowed only after numeric process absence is
positively proved. Malformed or unprovable records fail closed without
signalling.
A failed competing startup therefore cannot overwrite or delete the ready
runtime's budget, even when both processes inherited the same launcher
identity.

The shutdown-budget directory, lock, canonical record, and publication
temporary are validated through descriptors as current-user-owned paths.
Symlinks, FIFOs, devices, directories, hard links, wrong-owner files, inode
changes, and records larger than 1 KiB fail closed. `O_NOFOLLOW` and
`O_NONBLOCK` are used where available; record reads validate size before
loading content and revalidate the descriptor/path inode afterward. Unsafe
records are preserved for explicit repair rather than opened, replaced, or
treated as missing.

When stop initially finds no budget during startup, it snapshots the exact
Bash launcher generation and rechecks immediately before TERM. After that
launcher exits, it rechecks for a late v3 publication, asks the native identity
helper to prove the launcher's complete process group is empty, and performs
one final budget read. Missing-budget stop reports success only after that
proof. A v3 record published during either probe wins the handoff and stop
switches to its verified Python supervisor identity. A surviving group,
unknown OS state, changed launcher generation, changed v3 generation, or
remaining cleanup record fails closed and preserves diagnostic metadata.
An active startup lease tightens this rule: without a verified launcher,
verified stale-generation cleanup, or a matching v3 record, stop/update fails
and preserves the available PID tombstone and lease. If the matching v3 record
appears later, its launcher PID/service nonce must match the lease before
native stop may signal the verified supervisor and remove that exact startup
generation after cleanup. `status` reads lifecycle lock, startup lease, v3
budget, and launcher metadata before reporting liveness. Generation conflicts
win over a live launcher and are reported as `unknown`; an active owner is
reported as `starting`, `stopping`, or an in-progress update/install, never as
an unqualified `running`.
The native launcher executes the HealthMes runtime Python directly rather than
inserting an additional `uv run` process between the Bash wrapper and
supervisor. The supervisor then launches only the Hermes child with the
manifest-bound dedicated decision virtual environment's Python.

The decision supervisor owns a separate child process group. It tracks
PID/start-token identities for the leader and descendants. Linux opens a pidfd,
revalidates `/proc` identity after opening it, and delivers TERM/KILL through
that stable handle; a platform without pidfd support fails closed instead of
falling back to an uncertain numeric PID. macOS replaces second-resolution
`ps lstart` child identity with `libproc` `PROC_PIDTBSDINFO` start seconds and
microseconds. Group enumeration executes absolute `/bin/ps` with a fixed
minimal locale/path environment and requires every non-empty row to contain
exactly one positive PID and PGID. Empty, partial, extra-column, duplicate,
non-numeric, stderr-bearing, or libproc-inconsistent output is unknown and
fails closed. A final non-empty row without a newline is also treated as
possibly truncated rather than accepted.
macOS has no public pidfd-equivalent, so a small unavoidable interval remains
between the final libproc check and `kill(2)`; the implementation documents
that OS boundary rather than treating an unverified PID as safe.

This still cleans up descendants when the leader exits first without signaling
a later unrelated process group that reuses the numeric PGID. If the leader is
already absent from the first OS snapshot while asyncio has not yet published
its return code, the supervisor first reaps that exact subprocess handle and
then requires continuity between the pre/post-reap descendant snapshots before
adopting and signalling individual members. An empty pre-reap snapshot followed
by a non-empty snapshot is treated as reused-PGID identity loss; that generation
remains fail-closed on later close retries and the newly observed members are
never signalled. On Linux, both supervisor drain and launcher recovery require
two consecutive, independently enumerated empty `/proc` group observations;
one transient empty scan is not proof that the group is gone. The launcher
refuses to SIGKILL only the outer service group if that complete drain fails;
it exits non-zero instead of orphaning the child or reporting a false stop.
On any unproven cleanup, available launcher PID/identity metadata and the v3
record are preserved. Verified cleanup removes only the launcher metadata
generation captured by that stop operation. The maximum native TERM wait is
317 seconds, followed by at most one second to verify that the managed Bash
wrapper reaped the exited supervisor. A single native identity helper owns the
bounded wait, so interpreter startup overhead is not repeated on every poll.
Both Compose and the login LaunchAgent retain their consistent 360-second outer
shutdown budgets. `status` also consults a valid v3 record, so lost or dead
wrapper metadata does not hide a still-live verified Python supervisor;
retained records for dead or reused supervisor identities are reported as
incomplete or unknown rather than silently treated as stopped.

Re-runs are byte-idempotent when the desired decision artifacts are current.
Bootstrap also performs a one-way migration of the general
`$HERMES_HOME/cron/jobs.json`: jobs carrying the legacy
`origin.source=healthmes-bootstrap` marker are removed, while unmarked jobs
are removed only if their complete managed declaration exactly matches a
known old HealthMes briefing. Same-name customized jobs, foreign-origin jobs,
unknown records, and all other user jobs are preserved. Malformed or unsafe
cron storage fails closed, dry-run never writes, and a concurrent content
change aborts replacement.

The legacy Hermes scheduler does not share a cross-process lock with
bootstrap. Stop or pause that scheduler while performing the migration.

The wellness decision runtime uses a separate rendered profile at
`config/hermes-decision-config.yaml.tmpl`. It exposes one product MCP server,
`healthmes`, and exactly six read-only tools:

```text
search_activity
search_nutrition
search_calendar
search_wearable
list_wellness_skills
read_wellness_skill
```

Do not add direct `open_wearables`, native Hermes toolsets, mutation tools,
memory, browser, terminal, or writable Skill tools to this profile. Product
wellness requests enter through `POST /v1/wellness-decisions`; the server
calls Hermes `/v1/responses` once and validates the live tool profile,
transcript, strict result envelope, and source references.

The runtime manifest detects drift in its declared launch-control artifacts
at startup and during operation. It does not hash every transitive Python
package or native library, and it is not a sandbox against a process that
already has the same OS user and can rewrite the runtime, venv, and manifest
concurrently. The supervisor's boot snapshot is captured after Python has
imported its control modules: it detects later on-disk drift, but is not proof
that the already-loaded bytecode came from exactly those captured bytes.
No authorization or sandbox decision may rely on that stronger claim.
Protect the repository, runtime venv, decision home, and attestation key with
OS ownership and filesystem permissions. The supervisor executes the original
manifest-bound venv Python path on every platform (required for macOS
`@executable_path` library resolution), revalidates the manifest immediately
around startup, and holds generation-aware child leases from response
attestation until each complete `/v1/responses` stream closes. Up to
`HEALTHMES_DECISION_RUNTIME_MAX_CONCURRENT_RESPONSES` responses may share the
same verified child generation. Once watchdog restart or shutdown is waiting,
new leases pause and the writer drains every active lease before replacing the
child, preventing starvation and mid-response generation switches. Response
resources and leases are released exactly once on authentication drift,
upstream connection failure, ASGI response-start failure, caller disconnect,
cancellation, or normal stream completion.

On SIGTERM, the Uvicorn signal hook closes response admission immediately.
Existing generation leases may drain for the same finite saved budget before
the child group is terminated. The supervisor verifies the complete process
group, not only its leader, and escalates remaining descendants to SIGKILL
within the bounded cleanup window. Its Hermes-facing HTTPX stream uses
explicit 5-second connect/write/pool bounds with no per-chunk read timeout, so
a valid SSE turn may be silent for more than five seconds while the overall
decision wall clock remains authoritative.

The native shutdown budget v3 records both the managed Bash launcher
PID/start token/service nonce and the actual Python supervisor PID/native start
token. Stop validates both identities and always sends TERM to the verified
Python supervisor, which owns Hermes descendant draining. Missing or dead
launcher metadata therefore cannot hide a surviving supervisor. After that
supervisor exits, native stop reports success only if the exact v3 record is
gone, proving that the supervisor completed child-group cleanup; otherwise it
keeps available launcher metadata and the record for diagnosis. Legacy v1/v2
budgets remain readable only for conservative launcher-group shutdown; they
never shorten the 317-second native wait. Missing-budget startup shutdown uses
the proven-empty launcher-group handoff described above; it is not evidence
that an untracked child was cleaned merely because the Bash wrapper exited.
Generic `ps:` identities are not eligible for Python numeric signalling.
Unsupported platforms, Linux without `/proc` or pidfd signalling, unreadable
`/proc` process records, malformed Darwin process listings, and unprovable
identities fail closed.

Running the gateway natively (verified live on macOS with dummy creds):

```bash
cd vendor/hermes-agent && \
  HERMES_HOME=~/.hermes/decision \
  UV_PROJECT_ENVIRONMENT=../../data/hermes-venv \
  uv run --frozen --no-dev --extra messaging hermes gateway run
```

Two caveats from live verification: (1) if a supervised hermes service is
installed on the machine (launchd), `hermes gateway run` refuses to start
even for a different `HERMES_HOME` — add `--force`; (2) `uv run` inside
`vendor/hermes-agent` drops a `hermes_agent.egg-info/` directory into the
vendor tree (setuptools metadata, ignored by git) — harmless, delete it if
you want the vendor tree pristine; the venv itself stays outside via
`UV_PROJECT_ENVIRONMENT`.

### Hermes CLI diagnostics (not the HealthMes product ingress)

The vendor CLI can read the isolated decision profile. Use it only to diagnose
Hermes model/provider or MCP registration behavior:

```bash
cd vendor/hermes-agent && \
  HERMES_HOME=~/.hermes/decision \
  UV_PROJECT_ENVIRONMENT=../../data/hermes-venv \
  uv run --frozen --no-dev --extra messaging hermes            # interactive
# one-shot:
#   ... hermes chat -q "List the tools visible to this runtime."
```

Direct CLI chat bypasses the HealthMes DecisionRequest ingress, source
validation, and finalization policy. End-to-end product wellness QA must call
`POST /v1/wellness-decisions`; direct Hermes output is not proof that the
HealthMes product path works. Every POST requires an `Idempotency-Key`: use
one stable key per logical request and reuse it for retries. Reusing a key
with different input returns `409 decision_idempotency_conflict`.

### Choosing the LLM (not just Claude)

The vendor ships ~29 model-provider plugins
(`vendor/hermes-agent/plugins/model-providers/`: anthropic, openai-codex,
gemini, openrouter, ollama-cloud, bedrock, vertex, deepseek, xai, …).
Selection is config, not code: set
`HEALTHMES_DECISION_HERMES_MODEL` /
`HEALTHMES_DECISION_HERMES_PROVIDER` (optionally
`HEALTHMES_DECISION_HERMES_MODEL_BASE_URL` for OpenAI-compatible self-hosted
endpoints) in `.env`, re-run bootstrap, and export the matching provider API
key. These values configure only the isolated HealthMes decision runtime; the
general Hermes gateway and `hermes chat` retain their own configuration.

## Backups (local-first, encrypted)

Snapshots bundle the healthmes DB dump, raw ingest, an optional
open-wearables pg_dump, the media tree and optional Hermes state, then
age-encrypt them with a passphrase (docs/PLAN.md §9; format spec +
remote-vault contract in `docs/BACKUP.md`). This is a partial component
snapshot: `.env`, external OAuth credentials and an unconfigured
Open Wearables DB are not included. If `HEALTHMES_OW_API_KEY` configures
Open Wearables for runtime use while `HEALTHMES_OW_DATABASE_URL` is absent,
manual and weekly creation still write a valid snapshot but emit an explicit
partial-backup warning; the manifest records that Open Wearables data cannot
be recovered from it.

```bash
export HEALTHMES_BACKUP_PASSPHRASE='...'    # or set it in .env
uv run healthmes backup create              # writes {data_dir}/backups/...
uv run healthmes backup list                # needs no passphrase
uv run healthmes backup restore <name>      # dry-run: prints the manifest
uv run healthmes backup restore <name> --yes  # actually replaces live data
```

Stop HealthMes, Open Wearables and other writers before applying a restore.
Restore strictly validates archive paths and checksums, preflights and stages
all included targets, then uses rollback-capable same-filesystem swaps for
SQLite/media/raw/Hermes. An included component without a configured target
fails before mutation. For each PostgreSQL database, `pg_restore` first
expands a complete SQL script into an anonymous temporary file. One `psql`
connection then checks the physical database identity and executes that SQL
under `--single-transaction`. A PostgreSQL-plus-files or multi-database restore
fails closed unless the operator explicitly passes
`--allow-cross-store-partial`. That flag acknowledges that an already
committed PostgreSQL database cannot be rolled back together with another
store. PostgreSQL physical target identity is revalidated before mutation,
before each restore, and finally in the same `psql` transaction as the restore
SQL. A same-session identity mismatch executes no restore SQL. Other failed
`psql` restore commands are reported as `commit outcome unknown` because a
lost client acknowledgement cannot prove that the server did not commit.

(`healthmes` is a console script installed by `uv sync`; `uv run python -m
healthmes backup ...` is equivalent. Bare `python -m healthmes` still serves
the API.) Knobs: `HEALTHMES_BACKUP_DIR` (default `{data_dir}/backups`),
`HEALTHMES_OW_DATABASE_URL` (include the open-wearables dump),
`HEALTHMES_HERMES_HOME`/`HERMES_HOME` (include Hermes state),
`--passphrase-file` (keep the passphrase out of argv/history). A weekly
backup job (Sunday 03:30) runs when the scheduler is enabled; without a
passphrase it skips with a warning. `/v1/storage/settings` always labels this
as a `partial_component_snapshot` with `full_node_recovery=false` and reports
the Open Wearables runtime/dump mismatch. `next_snapshot_scope` is a
prospective view based on current configuration/source presence and explicitly
does not describe the encrypted latest snapshot. **Losing the passphrase
means losing the backups** — there is no recovery path by design.

The snapshot write-plane fence captures the HealthMes DB, media and raw-ingest
trees as one cooperative generation and blocks raw/media publication and
retention deletion during that interval. Open Wearables and Hermes are
separate component snapshots, not part of a distributed point-in-time
transaction. Local rollback protects against an operation failure while the
process is running; it does not claim power-loss atomicity.

### Storage maintenance recovery

Startup and every scheduled storage-maintenance run first reconcile
self-describing `.healthmes-unlink-v2-*` journals and exact DB-indexed
`.staging/media` / `.staging/raw_ingest` aliases under the global write-plane
fence. Retention then commits `StorageObject.purged_at` plus a versioned file
identity before removing bytes, and commits
`file_cleanup_completed_at` afterward. This order makes an interrupted unlink
retryable.

One `MaintenanceBudget` is created for the whole run and shared by unindexed
discovery hashing, directory scans, namespace mutations, cleanup-journal
publication, quarantine handling and generic durable unlink/recovery. The
defaults are a 10-second absolute deadline, 256 MiB of cumulative hashing and
4,096 cumulative scan/mutation entries:

```text
HEALTHMES_STORAGE_MAINTENANCE_TIMEOUT_SECONDS=10
HEALTHMES_STORAGE_MAINTENANCE_MAX_HASH_BYTES=268435456
HEALTHMES_STORAGE_MAINTENANCE_MAX_DIRECTORY_ENTRIES=4096
```

Pending purged-row retries are prepared before unindexed discovery and before
any new retention tombstone. If the shared budget is exhausted, the current
unfinished candidate and every later candidate remain pending, bounded cursors
retain the completed scan position, and a fresh maintenance run continues with
a new budget. No later candidate starts under an already exhausted budget.

Rows already marked `purged_at` before file-cleanup identities were introduced
remain pending after migration. The first current maintenance run acknowledges
a missing path, or captures and removes an existing regular file only when its
size and indexed SHA-256 match. Existing bytes without a digest, symlinks,
replacement generations and unverifiable paths are preserved and reported.
An absent final path with no deterministic staging aliases is acknowledged
even when the legacy indexed digest is missing or malformed, because no
HealthMes-owned payload name remains to delete.

Use the API to distinguish row purging from physical cleanup:

```bash
curl -sS -X POST \
  'http://127.0.0.1:8100/v1/storage/maintenance?dry_run=true'
curl -sS -X POST \
  'http://127.0.0.1:8100/v1/storage/maintenance'
```

- `records_purged` is the number of rows tombstoned by the live run.
- `files_deleted` is the number of object cleanups that removed a proved
  HealthMes-owned name; legacy `deleted` is an alias for this field.
- `file_cleanup_pending` is the unresolved purged-row count after a live run,
  or the already-existing unresolved count in dry-run.
- `bytes_reclaimed` is credited only after no unknown hard link remains.
- `decision_receipt_candidates` / `decision_receipts_deleted` report compact
  decision-receipt retention separately from full decision records.
- `budget_exhausted`, `budget_resource` and `budget_phase` provide a structured
  continuation signal instead of requiring clients to parse `errors`.

Do not manually remove `.healthmes-unlink-*`,
`.healthmes-storage-delete-*`, or
`raw_ingest/.healthmes-storage-delete-journal/*` entries based on age. The
generic v2 journal is self-describing, but retention quarantine authority
lives in `StorageObject.file_cleanup_identity`. Retention also writes a
canonical central journal before deleting any name:

```text
...-intent.json
    fsynced deletion intent + exact guarded inode generations
...-complete.json
    every guarded generation proved st_nlink == 0
...-manual-review.json
    physical outcome is ambiguous; automatic completion is blocked
```

The state files bind to the SHA-256 of `intent`. A `complete` journal survives
a failed second database commit and lets the next maintenance run acknowledge
the already-finished deletion without unlinking again. The journal is removed
only after `file_cleanup_completed_at` commits. Corrupt, conflicting,
ownerless, active-object, legacy and malformed recovery artifacts are
preserved intentionally and reported in maintenance errors.

All three private recovery namespaces are excluded from
`_discover_unindexed()` and `measure_usage()`: they remain on disk for
recovery but cannot become new wellness records or active quota usage. If a
live retry leaves pending rows, stop duplicate writers, preserve a
snapshot/copy, and inspect the object IDs and paths in `PurgeJob.detail`.
Never clear `file_cleanup_completed_at` or use a blind `rm` to silence
recovery state.

Bounded reconciliation persists five owner-only operational cursors:

- `.healthmes-recovery/unlink-recovery-v1.json` tracks the round-robin
  durable-unlink directory queue and retry state.
- `.staging/.healthmes-unindexed-discovery-v2.json` tracks independent
  `media` and `raw_ingest` DFS stacks.
- `.staging/.healthmes-staging-index-cursor-v1.json` tracks the
  `StorageObject` keyset page used by exact staging reconciliation.
- `.staging/.healthmes-staging-fallback-cursor-v1.json` tracks independent
  resumable DFS stacks, kernel offsets and directory generations for
  unindexed staging traversal.
- `.staging/.healthmes-storage-cleanup-scan-cursor-v1.json` tracks the
  central retention-journal directory identity, kernel offset and in-batch
  position. A bounded maintenance run therefore continues past persistent
  malformed, ownerless or active-object journals instead of rescanning the
  same first page forever.

Within the shared entry budget, exact indexed reconciliation receives at most
three quarters of a multi-entry slice while fallback retains at least one
quarter; unused indexed capacity flows to fallback. A one-entry slice
alternates passes through the persisted index-cursor `next_pass`.

A fallback root completed in one slice is re-armed in the next while its peer
is still in progress. This catches additions in existing deep descendants
that cannot be inferred from root-directory metadata, while the per-root
quantum preserves round-robin fairness.

The control directories are `0700`; cursor files are `0600` and are published
with temp-write, file `fsync`, atomic replace and directory `fsync`. Invalid,
unsafe or stale state restarts a sweep. These cursors are not `StorageObject`
rows, active usage/quota, retention candidates or snapshot members. Do not
copy or edit them to force progress; preserve the data tree and let the next
maintenance run rebuild state.

Cursor publication and a pre-reserved terminal cleanup capsule are deliberately
small, fixed crash-progress writes. If the shared deadline expires just after a
destructive durable transition has started, HealthMes may finish that capsule
or publish the cursor so the next run can prove where to resume. This exception
does not permit another candidate, another hash, or an unbounded scan after the
deadline.

These cursor basenames are internal only as direct children of `.staging/`.
The same exact basename under `media/` or `raw_ingest/` is a normal user
payload and must remain indexed and measured.

The whole `raw_ingest/.healthmes-storage-delete-journal/` subtree is reserved,
including malformed and unknown names. Never index those entries as
`raw_payload`; the bounded journal reconciler owns preservation and reporting.
Usage measurement uses no-follow metadata and counts only regular files, never
the target bytes of a symlink that escapes the HealthMes data tree. The scan is
capped at 100,000 entries and two seconds for the complete operation, including
database connection-pool checkout. One clean-session transaction owns
the global write-plane fence while it reads the storage index, scans the
filesystem and publishes the daily snapshot; SQLite begins that transaction
with `BEGIN IMMEDIATE`. This prevents concurrent first measurements from
racing on the daily unique key and prevents an older scan from overwriting a
newer storage generation. Results are written only after a complete scan and
root-inode revalidation. A missing/replaced root, permission failure or
exhausted bound rolls back the measurement and preserves the previous
`storage_usage_daily` rows for a later retry. Callers must use a SQLAlchemy
session with one effective database bind and commit or roll back pending ORM
changes or an active transaction first. A custom `get_bind()` wrapper or
mapper route is allowed only when SQLAlchemy's base resolver maps its
configured default, mapped-class, ORM `Mapper` and query-clause routes for
`StorageObject` and `StorageUsageDaily` to the same `session.bind`. Caller
`get_bind()` overrides are never invoked. The measurement itself runs in a
standard internal Session bound to one deadline-bounded pinned connection, so
caller routing cannot move the usage transaction, storage index or snapshot
outside that connection or deadline. SQLite `busy_timeout` and PostgreSQL
transaction-local
`lock_timeout`/`statement_timeout` are refreshed from that same remaining
deadline before blocking database phases. When a successful scan finds that
the last regular file in a class disappeared or became a symlink, the current
row is explicitly updated to zero.

`StorageObject` has a database CHECK for the two-phase cleanup state: an active
row cannot carry cleanup identity/completion metadata, a populated cleanup
identity requires `purged_at`, and completion requires both `purged_at` and a
populated identity. SQL `NULL` and JSON `null` both mean unset. The central
journal directory is owner-only, and retiring any completed journal resets its
bounded directory cursor so a deletion cannot make the next entry disappear
behind a stale directory cookie.

### Remote vault (S3-compatible, ciphertext-only)

`RemoteVaultProvider` (docs/PLAN.md §9 business seam) replicates the same
age-encrypted envelopes to any S3-compatible bucket — AWS S3, Cloudflare R2
or MinIO. The vault never sees plaintext: snapshots are encrypted before
upload and the provider refuses to upload anything that is not an age
envelope. Configure `HEALTHMES_VAULT_BUCKET` (+ `HEALTHMES_VAULT_ENDPOINT` /
`_ACCESS_KEY_ID` / `_SECRET_ACCESS_KEY` / `_REGION` / `_PREFIX`; see
`.env.example`), then:

```bash
uv run healthmes backup push <name>               # replicate one snapshot
uv run healthmes backup create --provider remote  # create locally + replicate
uv run healthmes backup list --provider remote    # merged local/remote view
uv run healthmes backup restore <name> --provider remote --yes
```

`HEALTHMES_BACKUP_PROVIDER=remote_vault` makes the selector the default for
the CLI **and** the weekly job (which keeps the local snapshot even when
replication fails). Local-first always: restore prefers the local copy and
only downloads when it is missing. Full contract, privacy table and
R2/MinIO/AWS examples: `docs/BACKUP.md` §3.

## Android usage collector

`apps/android-usage/` hosts the Android apps. The original `:app` module is
the minimal usage collector (docs/PLAN.md §7) that feeds
`POST /v1/app-usage/batch` — pairing + toggle UI, hourly
`UsageStatsManager` buckets, WorkManager uploads every 30 minutes. It builds
with its own vendored Gradle wrapper (JDK 17+ and Android SDK platform 35
required; not part of the Python test suite or CI). Build, pairing and
verification steps: `apps/android-usage/README.md`. For phone → Mac uploads
the service must listen on the LAN: set `HEALTHMES_HOST=0.0.0.0` **and**
`HEALTHMES_API_TOKEN=<token>` (serve refuses a non-loopback bind without a
token — the surface carries medical data), then enter the same token in the
app. The fragmentation term of the energy engine activates automatically
once samples arrive. iOS Screen Time has a separate aggregate-only server
contract and injectable `ScreenTimeActivitySyncService` core. Authorization
success first registers an absent stable `ios-collector-v1-*` instance through
the existing input-control GET/ETag/If-Match PUT contract, then foreground
activation, pairing changes and a Screen Time-specific `BGAppRefreshTask`
enter the same single-flight sync and bounded-outbox pipeline. Registration
writes only `instance_id`, `platform=ios`, and `enabled=true`, retries CAS
conflicts by re-reading, and never overwrites an existing disabled or paused
instance. Automatic lifecycle paths only read central state and never register
or reactivate an instance. Cold launch, authorization notifications, and
background refresh never open a permission sheet. For a persisted opt-in, the
first active foreground catch-up may restore authorization through a
single-flight request after a read-only central-state preflight; it rechecks
opt-out, pairing, collector identity, enabled/pause, and revision before
upload. An unresolved noninteractive status returns
`ios_screen_time_reauthorization_required`. The normal repository build always
selects the unavailable adapter and collects no Apple activity. Saved
input/retention revisions have a UI-neutral
`inputConfigurationDidChange()` hook. Authorization, configuration, and
timezone changes observed during an active sync coalesce into one fresh
pending run. BG task expiration cancels the service pipeline only when no
foreground waiter shares it. The local outbox is capped at 8 entries/16 MiB,
expires entries after 14 days across restart and offline retry, and is
excluded from device backup. Explicit authorization requires an existing
HealthMes pairing; unpaired calls fail before Apple authorization. Local
opt-out cancels and awaits an in-flight authorization/bootstrap or foreground
restoration before privacy cleanup.

`HealthMesCompanionScreenTimeOptIn` is an opt-in request scheme, not an
eligibility assertion. Run
`bash Scripts/build-screen-time-opt-in.sh build` from
`apps/ios-companion`; it type-checks the required Apple APIs in the selected
SDK and compiles the real collector only when the probe succeeds. Unsupported
SDKs compile the explicit `ios_screen_time_export_sdk_unavailable` adapter.
The device settings UI remains device-team work. A real build also needs a
provisioning profile whose App ID includes both Family Controls entitlements;
Family Controls permission is required before App Store submission. Customer
use is limited to a device in the EU with an EU-country/region Apple Account,
while Apple-provisioned development/test builds may be exercised elsewhere.
The data-access authorization and export API require iOS 26.4 or later, and
only one app per device can hold `approvedWithDataAccess`. Signed-profile
eligibility, authorization ownership, and real-iPhone verification remain
external to unsigned CI. Device code must use
`docs/INPUT-CONTROL-PLANE.ko.md` rather than inventing a second settings model.

## Companion & desktop apps (issues #7 · #10 · #11)

Five native surfaces render the briefing: Android phone (+ Wear OS), iOS
(+ watchOS), a macOS menu bar app (+ widgets + screensaver), a Windows tray
app (+ screensaver), and the web pages the service itself serves (decision
viewer, weekly report). All are **local-first**: each app pairs with your
own healthmes instance (base URL + bearer token) and talks to nothing else;
polling only (ETag/304, 5-minute cache floor), no APNs/FCM/WNS relay —
the durable `/v1/alerts` stream is the current product-owned delivery
surface, while guaranteed real-time push remains future work. Visual design stays
deliberately placeholder-labeled — the notification/watch UX belongs to the
healthcare domain expert (`docs/design/WATCH-NOTIFICATIONS.ko.md`; the PLAN
§8.5 notification grammar is the design system). The apps are not part of
the Python suite, but each platform has its own path-filtered CI workflow
(see "Continuous integration"); real-device passes are still owed everywhere
(each README keeps an honest not-verified list).

**Android / Wear OS** — `apps/android-usage/` (same Gradle wrapper as the
collector): `:shared` (contracts: glance, alerts page, weekly report,
proposals, capture requests; ETag-aware client; encrypted pairing),
`:companion` (the full phone app of issue #10 — single-activity Compose:
briefing home + 24h curve, alert history, weekly report, camera/photo/voice
capture, proposal accept/decline with 409 → "already resolved", settings;
plus the home/keyguard widget, §8.5 notifications with real WorkManager
actions and the ongoing focus-block notification that Wear OS bridges to the
wrist) and `:wear` (standalone Wear OS app — ProtoLayout tile + energy
complication, on-watch pairing). Per-module docs, build matrix and device
caveats: `apps/android-usage/README.md`.

```bash
cd apps/android-usage
./gradlew assembleDebug   # all four APKs (:app, :companion, :wear + lib)
./gradlew test            # all JVM unit tests (contract fixtures included)
```

**iOS / watchOS** — `apps/ios-companion/`, an XcodeGen-generated project:
the full iOS app of issue #10 (briefing home, weekly report view, in-app
decision viewer, camera/photos/voice capture, §8.5 local notifications from
BGAppRefreshTask with real accept/decline actions, focus-block Live
Activity), WidgetKit home/lock widgets, watchOS app + complications, XCTest
+ XCUITest bundles (UI tests self-skip without a live paired instance).
Requires Xcode with the iOS **and watchOS** simulator platforms
(`xcodebuild -downloadPlatform watchOS` once, ~3.6 GB) and
`brew install xcodegen`. Simulator-only builds, never signed:

```bash
cd apps/ios-companion
xcodegen generate
xcodebuild -project HealthMesCompanion.xcodeproj -scheme HealthMesCompanion \
  -destination "generic/platform=iOS Simulator" build CODE_SIGNING_ALLOWED=NO
xcodebuild -project HealthMesCompanion.xcodeproj -scheme HealthMesWatchApp \
  -destination "generic/platform=watchOS Simulator" build CODE_SIGNING_ALLOWED=NO
xcodebuild test -project HealthMesCompanion.xcodeproj -scheme HealthMesCompanion \
  -destination "platform=iOS Simulator,name=iPhone 17 Pro" CODE_SIGNING_ALLOWED=NO
```

**macOS** — `apps/macos-companion/` (issue #11), XcodeGen project reusing
`apps/ios-companion/Sources/Shared` verbatim (one contract/client across
Apple platforms): `HealthMesMac` menu bar app (status-item score, popover
briefing with real proposal actions, optional §8.5 notifications),
`HealthMesMacWidgets` WidgetKit extension, `HealthMesSaver` screensaver
bundle with the privacy toggle (hide health numbers — redaction is a tested
data rule). Native, unsigned:

```bash
cd apps/macos-companion
xcodegen generate
xcodebuild -project HealthMesMac.xcodeproj -scheme HealthMesMac \
  -destination "platform=macOS" build CODE_SIGNING_ALLOWED=NO   # + Widgets/Saver schemes
xcodebuild test -project HealthMesMac.xcodeproj -scheme HealthMesMac \
  -destination "platform=macOS" CODE_SIGNING_ALLOWED=NO
```

**Windows** — `apps/windows-companion/` (issue #11), a .NET 8 solution:
`HealthMes.Glance.Core` (portable contracts + ETag client + §8.5 grammar,
xunit-tested on any OS), `HealthMes.Tray` (WinForms tray icon + flyout +
toast balloons), `HealthMes.Screensaver` (`.scr` honoring `/s`, `/p`, `/c`
with the privacy toggle), a widgets-board card builder (the board provider
itself is deferred — MSIX/signing; see `DEFERRED.md`), DPAPI-protected
pairing. There is no Windows toolchain on this Mac: the compile-and-test
proof on real Windows is the `windows-apps.yml` CI job. Locally (any OS —
but the WinForms projects need the **official** .NET 8 SDK, not Homebrew's
`dotnet@8`, which lacks the WindowsDesktop targets):

```bash
cd apps/windows-companion
dotnet build HealthMes.Companion.sln -c Release
dotnet test tests/HealthMes.Glance.Core.Tests -c Release
```

**Cross-platform contract pinning** — every companion pins the app-facing
response schemas in fixture JSON, and a server-side contract change must
update **all platforms' fixtures in the same PR**:

- glance (`healthmes/api/briefing.py` → `GlanceOut`):
  `apps/android-usage/companion/src/test/resources/glance_*.json`,
  `apps/ios-companion/Tests/Fixtures/glance.json`, and their byte-identical
  Windows twins under
  `apps/windows-companion/tests/HealthMes.Glance.Core.Tests/Fixtures/`
- alerts (`healthmes/api/alerts.py` → `Page[AlertOut]`): `alerts_page.json`
  (Android, Windows), `alerts.json` (iOS)
- weekly report (`healthmes/api/reports.py` → `WeeklyReportOut`):
  `weekly_report.json` (Android, iOS; the Windows copy is envelope-only by
  design — its desktop parser types just the envelope)

The rule is enforced by the Python suite: `tests/api/test_glance_fixtures.py`
validates every in-repo fixture against the live server models, so contract
drift fails CI even where the companion suites themselves do not run.

### App-facing REST contracts (issue #10)

Endpoints the apps consume beyond the glance briefing (full request/response
shapes are pinned in `tests/api/`):

| Endpoint | Auth | Contract |
|---|---|---|
| `POST /v1/media` | bearer **only** | `multipart/form-data`, field name exactly `file`; client filename ignored. `Content-Length` **required** (`411` without one — chunked bodies are refused; the size cap is enforced off the header BEFORE the body is received/spooled). Content-type allowlist (jpeg/png/heic/webp images, m4a/mp3/ogg/wav audio; aliases normalized). `201 → {media_path, content_type, bytes}`; `415` (detail.allowed), `413` (cap = `HEALTHMES_MEDIA_MAX_UPLOAD_BYTES`, default 15 MiB; declared length beyond cap + 64 KiB envelope allowance is refused unread), `422` missing `file` field or empty file. Files land under `{data_dir}/media/YYYY/MM/` (UTC sharding). |
| `GET /v1/media/{media_path}` | bearer **or** derived viewer `?token=` (GET/HEAD only) | Serves the upload back (real content type, `Cache-Control: private, max-age=86400, immutable`); decision/report pages and in-app web views can embed via `<img>`/`<audio>`. All path tricks → uniform 404. |
| `POST /v1/nutrition-observations/analyze` | bearer | Accepts an uploaded image `media_path` plus capture time/timezone/provenance. Calls the configured Ollama/OpenAI/Gemini/Anthropic/xAI vision provider with a strict JSON schema and stores serving, core nutrients, optional additional nutrients, and provenance in `WellnessEvent.payload`. Ollama is the local default. Remote providers require a configured key, per-request `allow_remote_vision: true`, and HTTPS; there is no automatic cloud fallback. Repeating the same media returns the existing observation. Text/voice automatic nutrition analysis is not implemented. |
| `GET /v1/nutrition-observations[/{id}]` | bearer | Reads immutable structured observations. Estimates remain exact/range/unknown and are never promoted to confirmed intake merely because they were stored. |
| `POST /v1/nutrition-observations/{id}/confirm` | bearer | Appends a confirmed/corrected/rejected caffeine event. Confirmed/corrected requests must provide one finite non-negative `caffeine_mg` for every item in the observation. |
| `POST /v1/nutrition-observations/daily-confirmations` | bearer | Appends a local-day coverage event. `total_intake_complete: true` is accepted only when the ID set contains every observation for that local date. |
| `POST /v1/medical-records` | bearer | REST twin of the bounded `create_medical_record` capture command: `{kind: medication\|symptom, description, media_path?, transcript?, context?}`. The server attaches the deterministic health snapshot under `context.health` (degrades to `{status: unavailable}` when open-wearables is down — capture never fails for infra reasons); caller context is stored under `context.capture`. |
| `GET /v1/alerts` | bearer | Alert history in glance semantics ("unresolved == recently pushed"): `?hours=1..168` (default 24), paginated `Page` envelope, newest first. Items carry the §8.5 grammar recorded at fire time (`summary`/`evidence`/`proposal`) + `decision_url`; `alerts[0]` agrees verbatim with the glance top alert (test-pinned). |
| `POST /v1/schedule/proposals/{id}/accept` / `/decline` | bearer | The apps' ✅/❌ actions. Second tap → `409 invalid_transition` with `detail {current, requested}` (render "already resolved"); unknown id → 404. |
| `POST /v1/food-logs` | bearer | Accepts `media_path` from `POST /v1/media` (≤500 chars). |

Client caveats worth knowing (all handled by the shipped apps):

- **Timestamp quirk**: store-backed endpoints (schedule proposals,
  food-logs) serialize naive-UTC datetimes (no `Z`), while glance/alerts
  serialize timezone-aware — clients must parse both (the shared parsers
  treat naive as UTC).
- **No alert→proposal linkage yet**: alert items carry no
  `schedule_proposal` id, so notification action buttons act only when
  exactly one proposal is pending (the no-guessing policy of PLAN §11);
  otherwise they route into the app. Lifting this needs a server-side
  linkage field.
- Push relay (APNs/FCM/WNS) is out of scope **by design** — notification
  delivery is OS-budgeted polling and no guaranteed real-time channel ships
  in the canonical runtime.

## 캘린더 연결 (calendar connect)

`healthmes connect` is the low-friction onboarding for the two calendar
mirrors (docs/PLAN.md §6). A successful connect stores the credential as
runtime state under `{HEALTHMES_DATA_DIR}` and the sync jobs detect it
automatically — **no `.env` edit needed** (the `HEALTHMES_GOOGLE_CALENDAR_
ENABLED` / `HEALTHMES_CALDAV_*` settings keep working and override the stored
files). Polling itself runs only while the service has
`HEALTHMES_SCHEDULER_ENABLED=true`. Connection status is also served
read-only at `GET /connect` (linked from the landing page as "캘린더 연결";
gated like the other viewer pages, renders no secrets).

### Google Calendar — one-time OAuth client, then one browser login

Honest caveat: Google has no way around a **one-time app registration** for a
personal installed app — you must create your own OAuth client once. After
that, connecting (and re-connecting) is a single browser login.

One-time (Google Cloud Console):

1. Open <https://console.cloud.google.com/> and create (or select) a project.
2. "APIs & Services" → "Library": enable the **Google Calendar API**.
3. "APIs & Services" → "OAuth consent screen": configure it and add your own
   Google account as a test user.
4. "APIs & Services" → "Credentials" → "Create credentials" →
   "OAuth client ID" → application type **Desktop app**.
5. Download the client JSON and save it to
   `{HEALTHMES_DATA_DIR}/google/client_secret.json` (or point
   `HEALTHMES_GOOGLE_CLIENT_SECRET_FILE` at wherever you keep it).

Then, whenever you want to connect:

```bash
uv run healthmes connect google      # opens the browser: log in + consent
```

The token is saved to `{HEALTHMES_DATA_DIR}/google/calendar_token.json`
(owner-only) and the Google poll job is enabled by its presence. If the
client secret is missing, the command prints exactly these setup steps.

### iCloud Calendar (CalDAV) — app-specific password only

1. Create an **app-specific password** at <https://appleid.apple.com>
   (Sign-In and Security → App-Specific Passwords) — never the account
   password.
2. Connect (the password is prompted hidden — it never touches argv or shell
   history — and validated against `caldav.icloud.com` before anything is
   stored):

```bash
uv run healthmes connect icloud --username you@icloud.com
```

On success the credential lands in
`{HEALTHMES_DATA_DIR}/caldav/credentials.json` with mode 600 and the CalDAV
poll job is enabled by its presence.

### Status / disconnect

```bash
uv run healthmes connect status              # which calendars are connected (no secrets)
uv run healthmes connect disconnect google   # remove the stored token
uv run healthmes connect disconnect icloud   # remove the stored credentials
```

Future work (deliberately not built): a hosted "connect with Google" button
in the web UI would require a registered redirect URI on this service plus
web-flow secret handling; the `/connect` page therefore shows status +
instructions only and performs no writes.

## Real credentials — what needs what

Everything in `tests/` runs offline; the features below only come alive with
real credentials. Without them the service still boots and serves — the
corresponding integrations stay inactive.

| Feature | Credential | Where |
|---|---|---|
| Wellness Decision Agent | selected model-provider credential | provider key matching `HEALTHMES_DECISION_HERMES_PROVIDER` in `.env`; the isolated runtime is provider-agnostic |
| Health data reads (HealthMes MCP tools, triggers, insights) | open-wearables API key from its developer portal (`:8000/docs`) | `HEALTHMES_OW_API_KEY`; the product runtime does not start or expose the vendored Open Wearables MCP server |
| Wearable provider syncs | per-provider OAuth apps (Garmin, Oura, ...) | `config/open-wearables.env` (see the vendor backend docs) |
| Google Calendar mirror | OAuth client secret + one interactive consent | one-time client secret to `{HEALTHMES_DATA_DIR}/google/client_secret.json`, then `uv run healthmes connect google` (see "캘린더 연결") — the stored token auto-enables the mirror; `HEALTHMES_GOOGLE_CALENDAR_ENABLED=true` still works (polled every `HEALTHMES_GOOGLE_POLL_MINUTES` — needs `HEALTHMES_SCHEDULER_ENABLED=true`) |
| Apple Calendar (iCloud CalDAV) mirror | app-specific password from appleid.apple.com | `uv run healthmes connect icloud --username <apple-id>` (see "캘린더 연결") — the stored creds file auto-enables the mirror; the env pair `HEALTHMES_CALDAV_USERNAME` + `HEALTHMES_CALDAV_APP_PASSWORD` (+ `HEALTHMES_CALDAV_ENABLED=true`) still works and overrides it (polled every `HEALTHMES_CALDAV_POLL_MINUTES` — needs `HEALTHMES_SCHEDULER_ENABLED=true`) |
| Proactive wellness reasoning | the configured decision model/provider | the same isolated `hermes-decision` runtime used by `POST /v1/wellness-decisions`; no second reasoning credential or route is configured |
| Calendar adjustment confirmation handles | dedicated signing secret | `HEALTHMES_CALENDAR_ADJUSTMENT_SECRET` — generated into `.env` by `scripts/bootstrap.py`; never shared with model or API authentication |
| Encrypted backups (CLI + weekly job) | a passphrase you choose (and must not lose) | `HEALTHMES_BACKUP_PASSPHRASE` in `.env`, or `--passphrase-file` |
| Remote vault replication (ciphertext-only, optional) | S3-compatible bucket + access keys (AWS S3 / Cloudflare R2 / MinIO) | `HEALTHMES_VAULT_BUCKET` (+ `HEALTHMES_VAULT_ENDPOINT`/`_ACCESS_KEY_ID`/`_SECRET_ACCESS_KEY`/`_REGION`/`_PREFIX`); opt in with `HEALTHMES_BACKUP_PROVIDER=remote_vault` or `--provider remote` |
| Companion & desktop apps (Android/Wear/iOS/watchOS/macOS/Windows) | the service's `HEALTHMES_API_TOKEN` (same LAN rule as the collector) | entered in each app's pairing screen together with the base URL |
| Android usage collector | the service's `HEALTHMES_API_TOKEN` (verified server-side; required whenever the service binds beyond loopback) | entered in the app UI; sent as `Authorization: Bearer ...` |
| API/MCP surface auth | bearer token you mint (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`) | `HEALTHMES_API_TOKEN` in `.env`; required for `HEALTHMES_HOST=0.0.0.0` and for docker compose |

Calendar ownership remains immutable by default for user-created external
events. The only confirmed exception is the morning recovery Google
`SHORTEN` path: server evaluation binds the event snapshot and target change,
Telegram shows the one-time handle, and a trusted live Hermes session
parses `적용 <handle>` or `그대로 <handle>` and calls the HealthMes MCP tool
with the exact combined live reply and unchanged `reply_handle`; no
`proposal_id` or caller-supplied response channel is accepted.
No cron run waits for replies or performs the external write itself.

Not a credential but environment-shaped: `HEALTHMES_TIMEZONE` (IANA name,
e.g. `Asia/Seoul`) pins the user-local day for MCP joins and boundaries —
leave it unset on mac-native (machine timezone wins), set it for docker
(container clocks are UTC).

## Tests and lint

```bash
uv run pytest -q             # all tests (testpaths=tests, network-free)
uv run ruff check .          # lint (vendor/ excluded)
uv run ruff format healthmes tests   # optional formatting
```

Test conventions: fixtures in `tests/conftest.py` provide `settings`
(in-memory sqlite + dummy endpoints, `.env` disabled), `app`, and `client`.
Put new tests under `tests/<area>/`; pytest runs with
`--import-mode=importlib`, so no `__init__.py` files are needed and file
names may repeat across areas. Tests must not require network, Docker, or
real credentials.

## Full stack (docker compose, alternative path)

```bash
install -m 600 .env.example .env                           # tokens/keys
install -m 600 config/open-wearables.env.example config/open-wearables.env
docker compose config -q      # validate
docker compose up -d --build
```

Services and host ports (all overridable via `.env`):

| Service    | Port | What                                              |
|------------|------|---------------------------------------------------|
| postgres   | 5432 | `open-wearables` DB + dedicated `healthmes` DB (created by `scripts/initdb/`) |
| redis      | 6379 | celery broker/result backend                      |
| ow-backend | 8000 | open-wearables FastAPI (`/docs`)                  |
| ow-worker  | —    | open-wearables celery worker                      |
| ow-beat    | —    | open-wearables periodic sync scheduler            |
| healthmes  | 8100 | this repo's service (`/health`, `/v1/*`, `/mcp`, `/decisions`, `/cognitive-energy/forecast`; runs `alembic upgrade head` on start) |
| hermes-decision | — | optional isolated `/v1/responses` runtime behind the `decision` Compose profile; only HealthMes calls it |

Smoke test:

```bash
curl http://localhost:8100/health   # healthmes
curl http://localhost:8000/docs     # open-wearables
```

Notes:

- The `healthmes` database/role (`healthmes`/`healthmes`) is created by
  `scripts/initdb/01-create-healthmes-db.sh` on **first** boot of the
  `postgres_data` volume. To re-run it: `docker compose down -v` (destroys
  data) and `up` again. (`make mac-setup` is the native equivalent.)
- Compose injects docker service hostnames (`postgres`, `redis`,
  `ow-backend`) via container `environment:`; code and config defaults always
  stay localhost-native. HealthMes reads wearable data from `ow-backend`
  through its bounded REST/provider adapter.
- `hermes-decision` runs only under the `decision` profile. It uses the
  isolated, manifest-bound `./data/hermes/decision` home and is reachable
  only as HealthMes' Responses runtime, not as a public channel or webhook.
- Hermes decision config is **generated**: edit
  `config/hermes-decision-config.yaml.tmpl`, not the rendered
  `./data/hermes/decision/config.yaml`.

## Layout

```
healthmes/            service package (FastAPI composition root in app.py, settings in config.py)
  store/              SQLAlchemy models + engine/session singletons (healthmes DB)
  engine/             deterministic engines (trigger rules/sweep, canonical
                      decision dispatch, cognitive-energy engine, scheduler)
  calendars/          Google / iCloud CalDAV sync backends + mirror service
  mcp_server/         fastmcp Layer-B tools (14), served at exactly /mcp
  api/                REST routes (/v1/*, incl. the glance briefing), error
                      envelope, energy forecast, decision viewer + weekly
                      report (templates/ + vendored Mermaid in static/)
  backup/             local-first encrypted backup seam (age via pyrage) + CLI
                      + S3-compatible remote vault replication
alembic/              migrations for the healthmes DB (alembic.ini at repo root)
apps/android-usage/   usage collector (:app) + Android/Wear companions
                      (:shared/:companion/:wear) — own README
apps/ios-companion/   iOS/watchOS companion (XcodeGen project, own README)
apps/macos-companion/ macOS menu bar app + widgets + screensaver (XcodeGen,
                      reuses ios-companion/Sources/Shared — own README)
apps/windows-companion/ Windows tray app + screensaver + contract core
                      (.NET 8 solution, windows-latest CI — own README)
config/               templates + service env files (rendered copies gitignored)
docs/                 PLAN.md (architecture), BACKUP.md (snapshot format),
                      design/ (domain-expert worksheets, .ko.md), this guide
scripts/              dev_mac.sh (mac-native tooling), initdb/ (compose),
                      bootstrap.py (hermes), vendor_sync_check.sh (drift report)
skills/               hermes skills (copied into HERMES_HOME by bootstrap):
                      healthmes-planner, healthmes-capture, healthmes-sleep,
                      healthmes-stress, doctor-visit-summary
tests/                pytest suite (network-free)
data/                 runtime state (gitignored): pg, redis, sqlite, media, hermes home
vendor/               read-only upstreams - do not touch
```

## Conventions

- Python 3.12, typed, small modules; model/style conventions follow
  `vendor/open-wearables/backend` (see `docs/PLAN.md` for exact references).
- Everything in code/comments/docstrings is English.
- Dependencies are managed only in `pyproject.toml` + `uv.lock` (`uv add`,
  never pip).
- Settings come from `HEALTHMES_`-prefixed env vars via
  `healthmes.config.Settings`; never read raw `os.environ` in feature code.
- Never hardcode docker service hostnames in code or config defaults —
  every URL/host/port comes from `Settings`/env with localhost-native
  defaults; compose supplies docker values via `environment:`.

## Continuous integration

`.github/workflows/ci.yml` runs on pushes to `main` and on pull requests,
mirroring the run targets:

- **linux** — `uv sync --frozen`, `uv run ruff check .`, `uv run pytest -q`,
  `docker compose config -q` (compose validation without a daemon), and an
  alembic **offline** SQL render of the full migration chain for both the
  postgres and sqlite dialects (no database is ever started).
- **macos** — the mac-native developer entrypoint verbatim: `make mac-test`
  (uv + repo-local sqlite). No Homebrew services are installed or started.
- **compose-smoke** — actually boots the credential-free core of the compose
  stack (`postgres` + `redis` + `healthmes`, built from
  `Dockerfile.healthmes`; a throwaway `HEALTHMES_API_TOKEN` is minted inline
  because compose binds 0.0.0.0), curls `:8100/health` and verifies the
  bearer gate (401 without the token, 200 with) — the live half of the
  PLAN §11 "compose boot + Phase-0 demo query" smoke. The demo-query half
  needs a real decision-model provider and wearable credentials CI does not
  have; its contracts are pinned by the offline test suite instead.

Everything the two test jobs run is reproducible locally with the same
commands; the test suite is offline by convention (see "Tests and lint"
above). The hardening
tests under `tests/hardening/` add a restore drill (HealthMes DB + media +
raw-ingest snapshot → destroy all three → restore → byte-verify files and
`RawIngestEvent -> StorageObject -> WellnessEvent` references → reopen the
store), while backup/API tests prove concurrent ingest and retention wait on
the same snapshot fence. Trigger-flood tests pin the
alert-hygiene guarantees of `docs/PLAN.md` §11 (daily budget, dedup storms,
quiet-hours no-redelivery).

The native apps have their own **path-filtered** workflows (they only run
when the corresponding `apps/` tree or the workflow itself changes; all
support `workflow_dispatch`; nothing is ever signed):

- **`android-apps.yml`** (ubuntu) — `./gradlew` assembles every APK
  (`:app`, `:companion`, `:wear`) and runs the JVM unit-test suites, exactly
  the locally-proven matrix from `apps/android-usage/README.md`. No emulator.
- **`apple-apps.yml`** (macos) — two jobs. `ios`: XcodeGen + unsigned
  simulator builds of the normal iOS and watchOS schemes, then the
  XCTest/XCUITest suite on an iPhone simulator picked from the runner's newest
  installed iOS runtime (UI tests self-skip without a live paired instance).
  The Screen Time opt-in build is reproduced with
  `Scripts/build-screen-time-opt-in.sh`; unsupported runner SDKs compile its
  fail-closed adapter rather than the Apple collector. `macos`:
  XcodeGen + unsigned native builds of the menu bar app, widget extension
  and screensaver schemes, then the XCTest suite. Both jobs run when either
  Apple directory changes, because the macOS targets compile
  `apps/ios-companion/Sources/Shared` verbatim.
- **`windows-apps.yml`** (windows) — the compile-and-test proof for
  `apps/windows-companion` (no Windows toolchain exists on the dev machine):
  `dotnet build` (Release, warnings as errors) + the xunit contract suite +
  publish of the tray app and the `.scr` screensaver as build artifacts.

## Vendor upstream sync drill

`vendor/` holds read-only snapshots of the two upstreams; nothing under it
is ever hand-edited. Upstream sync therefore means **replacing a vendor tree
wholesale in a dedicated commit** — and before doing that, run the dry-run
drift report (docs/PLAN.md §10 Phase 3):

```bash
# 1. Get a fresh upstream checkout anywhere outside the repo:
git clone --depth 1 <upstream-url> /tmp/ow-upstream

# 2. Dry-run the diff (read-only; never writes anything):
scripts/vendor_sync_check.sh open-wearables /tmp/ow-upstream
scripts/vendor_sync_check.sh --list          # names under vendor/
```

The report classifies every path as **changed** (sync would replace),
**only in vendor/** (sync would delete) or **only upstream** (sync would
add), ignoring VCS internals and derived artifacts (`.git`, `__pycache__`,
`node_modules`, virtualenvs, caches). Exit codes: `0` in sync, `1` drift
found, `2` usage error — so the drill is scriptable.

When drift touches the coupling surface (docs/PLAN.md §11 — the Open
Wearables REST v1 routes, Hermes `/v1/responses`, and the MCP/config/outbound
delivery contracts), review the glue that pins it before syncing:
`healthmes/mcp_server/`, the HealthMes decision ingress/runtime adapter,
`config/hermes-config.yaml.tmpl`, `scripts/bootstrap.py` and their tests.
After replacing the tree, re-run `uv run pytest -q` and
`docker compose config -q` (CI runs both on the PR).
