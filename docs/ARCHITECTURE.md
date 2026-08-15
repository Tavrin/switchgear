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

Bash is the Stage-0 implementation language so the contracts can be proved
against the shape of the live OpenCode wrapper. **It is not a permanent
architectural commitment.** Do not treat `lib/*.sh` as the long-term layout.

### Pre-Stage-2 decision gate

Before any Stage 2 installation, evaluate moving the stateful/control-plane
core (leases, process groups, schema validation, result/review state,
routing, integrity snapshots, adapters) to Python. Keep shell only for
tiny launch wrappers if a wrapper is still useful.

Criteria: JSON/schema safety, atomic locking, subprocess process-group
control, testability, and the cost of a second provider adapter.

Default suspicion: Python wins (`asyncio`, `sqlite3`, `jsonschema`,
`subprocess` process groups). Do not rewrite this prototype unless Stage-0
evidence itself makes Bash untenable.

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
