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

## Hermes Boundary

- `vendor/hermes-agent/` is read-only from HealthMes tasks.
- Hermes development must happen in its own repository, branch, and worktree.
- HealthMes may depend only on documented Hermes contracts such as MCP,
  webhook, configuration, skill, and delivery interfaces.
- A required Hermes change must be proposed as a separate Hermes commit or PR.
  Do not patch the vendored tree as part of a HealthMes branch.

## Scope Of This Branch

- Storage architecture and product documentation may change.
- Application, migration, runtime, and vendored code must not change unless a
  later implementation task explicitly owns those files in a new worktree.

