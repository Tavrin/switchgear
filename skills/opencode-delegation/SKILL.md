---
name: opencode-delegation
description: Use before delegating a read-only scout or independent review to OpenCode. Covers ai-opencode invocation, model routing via project profile, fail-closed git checks, and the no-write default. Triggers: OpenCode, opencode, ai-opencode, independent review pool.
---

# OpenCode delegation — generic rail

OpenCode is **not** a manager and has **no program-state authority**.
It is a provider/worker pool. Do not launch write/implementation lanes
until a project profile enables write **and** `AI_OPS_WRITE=1` is set
for an authorized test. Production write also requires an OS-level
containment backend (see `docs/CONTAINMENT.md`).

**Require** the wrapper: `ai-opencode`. Do not invoke `opencode run`
directly — the wrapper is the integrity boundary.

This skill is the **single canonical** definition. Do not maintain a
second copy per harness.

## Discover the live CLI (do not remember syntax)

```bash
opencode --help
opencode run --help
opencode models
ai-opencode --profile <profile.json> models
```

`opencode models` is authoritative for IDs. The profile allowlist is the
rail. Re-run `ai-opencode models` after OpenCode upgrades.

## Wrapper

```bash
ai-opencode [--profile PATH] models
ai-opencode [--profile PATH] scout  <dir> "<prompt>"
ai-opencode [--profile PATH] review <dir> <role> "<prompt>"
ai-opencode [--profile PATH] run    --envelope FILE
```

Roles and models come from the **project profile**, not this skill.

## Fail-closed contract

The wrapper:

1. Requires `<dir>` to exist and contain `.git` (repo or worktree).
2. Refuses a non-default `OPENCODE_CONFIG_DIR`.
3. Snapshots HEAD + porcelain + dirty-file hashes + `git diff HEAD` hash
   (`git --no-optional-locks`) before the job.
4. Runs `opencode run --pure --dir … --model <id> --agent ai-ops-readonly
   --format json` with **no** `--auto`, inside a new session/process group.
5. Agent `ai-ops-readonly` denies `edit` and **denies bash entirely**.
   The wrapper sets `OPENCODE_DISABLE_PROJECT_CONFIG=1` and
   `OPENCODE_CONFIG_CONTENT` from the adapter runtime JSON.
6. Snapshots the same integrity tuple after. Exit 2 if anything changed,
   including edits to already-dirty files.
7. Writes uniquely named results under `$STATE/jobs/<job-id>/`
   (default state: `$HOME/.local/state/ai-opencode`, never `XDG_STATE_HOME`).
8. Refuses any model not allowed by the profile.
9. Rejects timeout outside 1..1800 (0 is not a disable).
10. Owns the process group: TERM, grace, KILL, orphan check.

If the wrapper reports dirty: treat the job as a **failed rail**, not a
valid review. Restore the tree before trusting output.

Shell-prefix injection is closed by denying bash, not by filtering
prefixes. Residual: ignored files, other worktrees, and writes outside
`$abs` can be invisible to the integrity snapshot. Provider permission
policy is not an OS write boundary.

## Review briefs

Read-only, blind first pass. Give the spec, the current SHA, the actual
diff, and objective gate evidence.

Do **not** include the manager's acceptance rationale or the implementer's
self-evaluation on the first pass. Attack a named claim. Reviewers must
not implement.

Independence level (different job / model / family / provider) is set by
the project profile.

## Forbidden

- git mutations, worktree add/remove, merge, push
- treating provider output as tracker/git truth
- `--auto`
- write/implementation jobs unless the project has explicitly authorized
  the Stage-W rail (not this Stage-0 default)

## Result collection

Stdout prints `model=`, `dir=`, `exit=`, `result=`, `meta=`, `job=`.
Read the JSON event stream at `result=`. Structured result is
`$STATE/jobs/<job-id>/result.json`.

## Failure handling

- Unknown role/model → refuse (no OpenCode process).
- Missing `.git` → refuse.
- Provider non-zero → print stderr tail, propagate exit.
- Dirty tree → exit 2, do not treat as a review.
