# Architecture

Four sections, in the order they matter: what this is for, who decides what, how
a job actually flows, and what is promised to callers.

> **When this file and the running code disagree, the code is right and this file
> is a bug.** `switchgear capabilities` reports the commands, flags and provider
> facts read off the parser, the harness registry and the runtime rather than
> maintained by hand — its refusal contract and fixed explanatory text are
> declared constants, and the emitted `provenance` block labels which is which.
> `result.json` is schema-validated before every write of it, including the
> promotion rewrite; the handoff, the review artifact, the profile and the lease
> token are validated before they are first trusted. Those are the executable
> contract. This document explains it; it does not define it. That rule exists
> because stale documentation here has already caused real drift, including a
> security property stated backwards in three places at once.
>
> The lease token is schema-validated before every `acquire` write, before
> `load_token` or `release` trusts its fields, and after `WorkerLock.__enter__`
> stamps `job_id` immediately before it rewrites the token (`lease.py`). These
> checks are stated at their actual boundaries because a claim this file cannot
> back is the defect this box exists to prevent.

## 1. Scope

Switchgear runs **one** agent job inside an OS boundary and produces a record of
what happened. One invocation is one agent, one process, one sandbox, one
outcome.

```
caller (out of scope: orchestrator, CI, shell script, human, another agent)
    │ profile + envelope + leased cwd
    ▼
switchgear (contracts, containment, process, leases, evidence, review predicates)
    │ harness adapter, one per agent CLI
    ▼
OpenCode · Claude Code · Codex · Grok
    │
    ▼
model pools (opencode-go, openrouter, anthropic, openai, xai …)
```

It is deliberately **not** an orchestrator: no queue, no board, no scheduling, no
fan-out, no retry policy. The concurrency cap is admission control, not
scheduling — it decides whether a job may start, never which job should exist or
in what order. That line is the reason the tool is small enough to trust, and a
change that crosses it will be refused however well written.

Callers are peers. An orchestrator, a shell script, CI, a human at a terminal and
an agent delegating to another agent all get the same contract, and Switchgear
imports nothing from any of them.

## 2. Authority

The distinction that governs almost every design decision here. Three domains,
one sentence each:

| domain | question | owner |
|---|---|---|
| **worker execution** | did this agent do what the record says, inside the boundary? | **Switchgear** |
| **project verification** | do the project's own gates pass on the result? | the **caller** |
| **workflow and merge** | should this land? | never Switchgear |

Two consequences that are easy to get wrong.

**Project verification needs its own containment, and Switchgear cannot provide
it.** Verification commands are as fallible and as hostile as the coding agent
that produced the change, and by the time they run Switchgear is finished and out
of the loop. A caller that sandboxes the worker and then runs the project's test
suite unconfined has secured the wrong half.

**`promote` is not permission to merge.** It attests that the worker did what the
record claims. Switchgear's gate is an **interlock review** — an uncommitted
worker delta, before any project verification. A caller's is a **project
review** — the exact head that passed its tests. Different times, different
material, so stacking them is defence in depth rather than duplication; but which
layer holds semantic acceptance is an operator's call, set in the operator-owned
budget file as `acceptance: interlock | external`.

Policies express **requirements**; evidence records **realized properties**. Each
record carries a `security` block read back off the sandbox argv that was really
constructed, so a caller states what it needs and tests the outcome rather than
knowing how namespaces are built here.

## 3. The nouns

`provider` used to mean two different things in the same record: the agent CLI
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

The adapter is keyed by **harness**, never by pool. One binary serves several
pools, so conflating them picks the wrong adapter the moment a second pool
appears — and then reads a stream with a parser that cannot recognise it, which
has produced a confident false failure report before.

## 4. Data flow

1. Load and schema-validate the project profile; compile policy.
2. Resolve role → model id → registry metadata (`model_family`, `vendor_family`,
   effort values, cost/trust slots). Effort is validated per **model**, not per
   harness.
3. Validate cwd as a git worktree; require `$STATE` disjoint from it.
4. Admission: budget, disk headroom, concurrency slot, exclusive lease.
5. Snapshot tree + git identity (+ sibling worktrees, canaries if provided).
6. Start the credential broker; build the bwrap argv; spawn the harness in its
   own session and process group.
7. **Evidence streams as it arrives** — the harness's stdout lands in
   `evidence/events.jsonl` through a controller-drained pipe, so a running job is
   observable and a crash cannot cost the record.
8. Snapshot again. Readonly: byte-identical tree. Bounded-write: git identity
   unchanged, in-tree edits expected, escapes fail the job.
9. Normalize the stream into `evidence/events.v1.jsonl`, compute the four outcome
   facts, freeze the delta, write and validate `result.json`.
10. Bounded writes finish `awaiting_review`, or `awaiting_external_review` when
    an operator has set `acceptance=external` -- same freeze, same evidence,
    both exit 0; what differs is only who may declare the change acceptable. A
    later readonly review with `parent_job` attaches an independence record;
    `promote` binds the change to reviewer-attested evidence under the worktree
    lock, and refuses outright under external acceptance.

## 5. Public contracts

Four, and only these:

| contract | where | stability |
|---|---|---|
| the CLI's argv and exit codes | `switchgear capabilities` | 0 ok · 1 refusal/error · 2 dirty **or argparse usage error** · 124 timeout |
| `result.json` | `data/schemas/result.schema.json` | additive keys; `schema_version` moves only on a breaking change |
| the normalized event stream | `evidence/events.v1.jsonl`, `logs --format normalized` | versioned in the filename and on every line |
| the task envelope | `data/schemas/task-envelope.schema.json` | closed; `correlation` is the caller's own space |

`evidence/events.jsonl` is **not** a contract. It is the harness's own stdout,
byte for byte — forensic evidence whose shape is whichever CLI ran. Consume the
normalized stream instead.

Exit 2 is necessarily disambiguated by output: a dirty refusal/outcome has the
`switchgear: REFUSING — ` prefix or a job record, while argparse prints its own
`usage:` message before any job starts and produces no job record.

## Language disposition

The 2026-08-16 remediation **moved the security-sensitive control plane to
Python** (`python/switchgear/`). An independent review showed Bash could not own
config isolation, leases, process trees, schema authority or sandbox construction
safely.

Bash remaining: `bin/switchgear` is a tiny launcher that `exec`s
`/usr/bin/python3` on the committed `__main__.py`. No security decision is
encoded in the shell wrapper. The other shell files are tests and policy gates
under `tests/`; nothing under `python/` sources shell.

## OS containment

Harness permissions and canaries are defence in depth. The boundary itself is the
kernel's: `bwrap` on Linux, with no silent fallback — if the backend is missing
or unusable, the job is refused. Readonly jobs additionally run under a subuid
boundary where the machine can establish one. See
[CONTAINMENT.md](CONTAINMENT.md) for what is actually constructed and
[THREAT-MODEL.md](THREAT-MODEL.md) for what is and is not defended.
