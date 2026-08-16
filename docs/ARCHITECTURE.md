# Architecture

Generic provider/worker substrate. Not a manager.

```
manager (out of scope)
    │ profile + envelope + leased cwd
    ▼
agent-ops (contracts, integrity, process, leases, review predicates)
    │ adapter
    ▼
OpenCode (first provider)
```

Project policy (tracker, tiers, product gates, locks, deploy, model taste)
stays in the **profile** and the **manager**. The adapter does not interpret
the envelope `project` object.

## Language disposition

The 2026-08-16 remediation **moved the security-sensitive control plane
to Python** (`python/ai_ops/`). The independent review of
`47e21bdd` showed Bash could not own config isolation, leases, process
trees, schema authority, or sandbox construction safely.

Bash remaining: `bin/ai-opencode` is a tiny launcher that `exec`s
`/usr/bin/python3` on the committed `__main__.py`. No security decision
is encoded in the shell wrapper.

Stage-0 `lib/*.sh` files remain in the tree as historical artifacts from
the reviewed baseline. They are not sourced.

See `docs/SECURITY-REMEDIATION.md` for finding dispositions.

## Data flow

1. Load and schema-validate the project profile.
2. Resolve role → model id → catalog metadata (`model_family`, `vendor_family`,
   reserved capability/trust/cost slots).
3. Validate cwd as a git worktree; isolate `$STATE` from the target.
4. Snapshot tree + git identity (+ other worktrees, canaries if provided).
5. Spawn the provider in a new session/process group.
6. Snapshot again. Readonly: byte-identical tree. Write: git identity
   unchanged; in-tree edits expected; escapes fail the rail.
7. Capture events under `$STATE/jobs/<job-id>/`. Write jobs parse a handoff
   **there**, never in the source tree.
8. Write jobs finish `awaiting_review`. A later readonly review with
   `parent_job` attaches an independence record. Required predicates are
   profile policy.

## OS containment

Provider permissions + canaries are defense in depth. Production write
requires an OS-level write boundary (`bwrap` on Linux). See
`CONTAINMENT.md`. No silent fallback.
