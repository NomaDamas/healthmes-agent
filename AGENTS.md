# HealthMes Repository Isolation Rules

These rules apply to the entire repository.

## Worktree Isolation

- Never implement substantial work in the primary worktree when another task
  has uncommitted changes.
- Create one dedicated branch and one dedicated worktree per task:

  ```text
  branch:   codex/<scope>-<YYYYMMDD>
  worktree: /private/tmp/healthmes-<scope>
  ```

- Run `git worktree list --porcelain`, `git branch --show-current`, and
  `git status --short` before editing.
- One branch must be checked out by only one active worktree and one agent.
- Integrate completed work by reviewed commit, merge, or cherry-pick. Do not
  copy an entire dirty worktree over another worktree.
- If another task changes the same file, stop and resolve ownership before
  continuing. Never overwrite or revert the other task's changes.

## Vendor Boundary

`vendor/hermes-agent/` and `vendor/open-wearables/` are pinned upstream
snapshots, not immutable code. Prefer documented extension points first:
MCP, REST, webhooks, configuration, skills, plugins, and HealthMes glue.

- A vendor source change is allowed when an extension point cannot implement
  the required behavior safely or completely.
- The task must explicitly own the affected `vendor/<name>/` path and use its
  own branch and worktree. Do not edit a vendor path owned by another task.
- Keep each vendor change in a separate, minimal commit named
  `vendor(hermes): ...` or `vendor(ow): ...`. That commit may include only
  the vendor patch and its upstream-side regression tests; keep HealthMes
  glue, integration tests, dependency changes, and product documentation in
  separate commits.
- The vendor commit or its PR description must state: the missing extension
  point, why a root-level implementation is insufficient, the upstream base
  revision, how the patch can be reapplied during an upstream sync, and the
  tests run.
- Add the relevant vendor regression test and any HealthMes contract or
  integration test affected by the patch. Preserve upstream copyright and
  license notices; do not mix formatting sweeps or unrelated upgrades into a
  vendor patch.
- Send generally useful fixes upstream and link the upstream PR. A
  HealthMes-specific or urgent patch may land first, but the PR must explain
  why an upstream PR is not appropriate or is still pending.

The development scripts must never mutate vendor source or lockfiles as a
side effect. That runtime safety rule does not prohibit an explicitly owned,
reviewed vendor patch.

## Task Scope

- Every task must state the files and boundaries it owns before editing.
- Application, migration, runtime, documentation, and vendor changes are
  allowed only when they are explicitly in scope for that task and follow the
  worktree and vendor rules above.
