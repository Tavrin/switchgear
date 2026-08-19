# Architecture

Generic provider/worker substrate. Not a manager.

```
caller (out of scope: orchestrator, CI, shell, human, another agent)
    │ profile + envelope + leased cwd
    ▼
switchgear (contracts, integrity, process, leases, review predicates)
    │ adapter, one per provider binary
    ▼
OpenCode · Claude Code · Codex · Grok
```

Project policy (tracker, tiers, product gates, locks, deploy, model taste)
stays in the **profile** and the **caller**. Switchgear does not interpret a
caller's own vocabulary at all: the envelope's `correlation` object is persisted
onto the result and handed back verbatim, and is never read for policy, routing
or permissions.

## The nouns

`provider` used to mean two different things, in the same record: the agent CLI
that ran the job, and the service that served the model. The result schema had to
carry a description explaining which was which.

| noun | is | on a record |
|---|---|---|
| **harness** | the agent CLI that runs a job — Claude Code, Codex, Grok, OpenCode | `harness` |
| **pool** | the service serving a model, named by the model id's prefix (`opencode-go/…`, `openrouter/…`) | `model.provider` |
| **model** | the exact, pool-qualified model id | `model.id` |
| **role** | a profile-level purpose (scout, implement, review) fixing model, mode and effort | `role` |
| **job** | one execution: one agent, one process, one sandbox, one record | `job_id` |

`provider` survives as an alias of `harness` and always carries the same value.

Deliberately **not** called *upstream*: the registry already uses `upstream` for a
pool's base URL (`providers.<id>.upstream`), so reusing it for the pool's name
would replace one ambiguity with another.

`switchgear capabilities` reports this table and the current alias list, so a
caller never has to trust this file.

## Language disposition

The 2026-08-16 remediation **moved the security-sensitive control plane
to Python** (`python/switchgear/`). The independent review of
`47e21bdd` showed Bash could not own config isolation, leases, process
trees, schema authority, or sandbox construction safely.

Bash remaining: `bin/switchgear` is a tiny launcher that `exec`s
`/usr/bin/python3` on the committed `__main__.py`. No security decision
is encoded in the shell wrapper. The shell files that remain are tests
and policy gates under `tests/`; nothing under `python/` sources shell.

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
