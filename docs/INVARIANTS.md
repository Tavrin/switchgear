# Invariants

Every one of these was earned by a specific bug. Breaking one silently
re-opens it, so each is stated with the failure that caused it.




- **The registry is controller-owned.** A profile may name model ids; it may never
  declare `model_family`/`vendor_family`, or it manufactures its own reviewer
  independence.
- **Promotion binds to reviewer-attested evidence.** Comparing freeze-derived
  fields back to the freeze proves nothing (that was round 1's CRITICAL).
  `reviewed_tree_digest` and `reviewed_dir` come from the *reviewer's* record.
- **The controller hands the reviewer the diff.** The reviewer has no shell and
  no git; asking it to find the diff made it loop 35 times. Injecting the frozen
  diff also binds the review to what will actually be promoted.
- **Each job freezes its OWN delta**, not the cumulative worktree state, or one
  job gets credit for another's work.
- **Digest covers non-tracked content**, including ignored files and symlinked
  directories. A worker cannot stage, so all its output is untracked; `.gitignore`
  is worker-writable.
- **Git dir legitimacy, not just stability.** A poisoned `.git` pointer survives
  the job that wrote it; a later job must not adopt it.
- **Never execute the provider outside bwrap** — not even `--version`.
- **Mutual exclusion is unconditional** for bounded-write; only the lease *token*
  requirement is profile-gated.
- **Evidence is persisted before integrity asserts**, so a worker cannot erase its
  own record by tripping one.

Earned later, in the operability and hardening rounds:

- **The absence of a record is never evidence of a benign state.** A missing
  `result.json` meant "running" for a cancelled job, a crashed background job, a
  dead foreground job, and a job id that never existed. Liveness is recorded
  (pid + start time + boot id) and checked, never inferred from what is missing —
  and `gc` protects `unknown` for the same reason.
- **Never identify a process by pattern.** Matching command lines fooled another
  project three times, once matching the operator's own shell; it fooled me three
  more times in one session while I was writing the test for their finding. Use
  the triple, or a heartbeat.
- **Effort values are per MODEL, not per provider.** One provider was measured
  serving two models with different sets. Adapters declare mechanism; the
  controller registry holds measured values with an `effort_source`. Unmeasured
  means refuse — OpenCode silently ignores an effort it does not understand and
  runs at the default while the record claims otherwise.
- **A projection resolves the adapter from the JOB, not from the caller.** Reading
  a Claude job's logs under the wrong profile normalized with the OpenCode adapter
  and reported a completed job as `failed / truncated`. A confident falsehood.
- **Every recursive delete is guarded at the delete.** Not by the care of whoever
  built the path.
- **One owner per file descriptor.** A `close()` in an `except` beside a `finally`
  that also closes made lock contention raise EBADF, which *replaced* the refusal
  — so the guard reported itself as a crash on the only path that happens under
  load.
- **Provider installs are discovered, never hardcoded**, and every installed build
  is recognised. An unrecognised real binary is treated as a mock and runs without
  the live gate.
- **The rail states the sandbox's limits to the worker.** A limit hit is the
  boundary, not a defect — say which and stop. Elsewhere this cost two dead lanes
  and a run of false "wedged GPU" reports.

