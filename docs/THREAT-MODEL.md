# Threat model

Current as of 2026-08-18. Controls listed here are implemented, not planned;
`CONTAINMENT.md` describes the OS boundary in detail.

## Actors

- Honest manager launching scouts/reviews/writes
- Confused manager (wrong cwd, two workers, stale lease)
- Model/tool that tries to escape the worktree
- Prompt-injection content in a repo or a fetched page
- Concurrent managers racing a lease
- A hostile or compromised provider binary
- A reviewer job trying to launder a promotion

## Assets

- Primary checkout and sibling worktrees
- `.git` identity (HEAD, remotes, config, worktree list)
- User home and harness config
- **The provider credential**
- Job results — the integrity of the evidence a promotion rests on

## Assumptions and caller obligations

The state root is a **single-user trust domain**. Any process that can read it
can read a background job's recorded process-group identity and ask `cancel` to
signal that job; there is no per-caller authorization layer by design. A caller
must therefore never mount the state root into a worker sandbox. Doing so would
let that worker cancel arbitrary jobs in the same root, including a job
reviewing its work.

## Controls

### OS boundary (the authority)

`bwrap` mount namespace; only the leased worktree is writable, and only for
bounded-write. Host `$HOME`, sibling worktrees and the state store are absent
rather than merely denied. Refuses if the backend is unavailable; the provider
is never executed outside it, not even for `--version`. `RLIMIT_FSIZE` and
process-group ownership bound resource abuse. See `CONTAINMENT.md`.

### Credential

Read controller-side from an operator-owned mode-600 file, never from the
controller's environment (which is where unrelated secrets live). Held by the
broker; the sandbox receives a placeholder. With a broker the sandbox has
`--unshare-net` and reaches exactly one upstream, over a bind-mounted unix
socket, on an allowlisted path, pinned to the resolved model. A live job with
no credential refuses rather than running unbrokered on the host network.

Provider env is constructed from scratch and asserted secret-free — never
filtered from the host env, which fails open on anything unanticipated.

### Provider posture (defence in depth, not the boundary)

Bash denied. Runtime pinned via `OPENCODE_CONFIG_CONTENT`; project config,
external skills and default plugins disabled; a foreign `OPENCODE_CONFIG_DIR`
refused. The agent definition and runtime config are **generated** from the
compiled policy into the sandbox's own `$HOME` — there are no static copies to
drift, and nothing is installed into the host provider config, which the job
cannot see anyway.

### Evidence integrity

Before/after snapshots of tree, git identity and sibling worktrees; digests cover
untracked and ignored content and resolve symlinked directories, because a worker
cannot stage and `.gitignore` is worker-writable. Each job freezes its **own**
delta, not the cumulative worktree. Git-dir *legitimacy* is checked, not only
stability, so a poisoned pointer is not adopted by a later job. Evidence is
persisted before integrity asserts, so a worker cannot erase its record by
tripping one.

### Promotion

Promotion binds to reviewer-attested fields (`reviewed_tree_digest`,
`reviewed_dir` from the reviewer's own record) — comparing freeze-derived values
back to the freeze proves nothing. Serious findings block promotion regardless of
the reviewer's verdict. Review independence (job/model/family/provider) is
enforced against a controller-owned registry: a profile may name model ids but
may never declare families, or it manufactures its own independence.

### Concurrency and containment of intent

Atomic leases (`flock`) with pid, starttime and boot_id. Mutual exclusion is
unconditional for bounded-write; only the lease *token* requirement is
profile-gated. Structured `{verb,args[]}` commands only; no `eval`/`sh -c`;
provider JSON is never evaluated. Handoff and results only under
`$STATE/jobs/<job-id>/`. Write kill switch plus `write_enabled: false` by default.

## Residual, knowingly accepted

- **Review is a probabilistic signal, not an authority boundary.** The same model
  on the same diff with the same prompt returned `needs_changes` on one run and
  `promote` on the next, documenting the same two real defects both times. Never
  make it the only gate; the human merge decision stays upstream in the orchestrator.
- **Provider permission semantics can change upstream.** They are depth, not the
  boundary — which is why the boundary is the kernel's.
- **No uid boundary for bounded-write jobs.** Read-only jobs run as a subuid
  where the machine supports it, so the kernel refuses writes to the invoking
  user's files. Bounded-write cannot: the controller must read back and commit
  what the worker produced, and subuid-owned files cannot be handed back without
  privileges this tool does not have. For those jobs, anything the invoking user
  can write *and* that is visible inside the namespace is writable by the job.
  See `CONTAINMENT.md`.
- **A hostile upstream** sees the prompts and the diff. Injection reaching the
  prompt is a real risk; the containment limits what it can *do*, not what it can
  *say*.
- **Prompt injection is not solved, and cannot be by this rail.** What is bounded
  is its reach into the one gate that depends on a model's judgement. Everything
  else in `promote` is model-free — tree digests, worktree identity, the
  generation CAS, independence — and none of it can be forged without changing
  the tree, which invalidates the digest. The verdict is the exception, so the
  promotion path now fails CLOSED when the reviewed diff contains content
  addressed at a reviewing agent, or forging this rail's own handoff/review
  blocks. That is a refusal to let a model's word carry a promotion when the
  input it read was trying to produce that word — not a detector, and it does not
  claim to catch a careful attacker. A human decides instead.
- **Exfiltration through the legitimate channel is unbounded.** The broker
  restricts which HOST a job may reach, not what it sends there. A secret the
  worker reads can leave inside a normal model call. Open.
- **Spend is bounded before a job, not during one.** `daily_usd` in the
  operator-owned budget file refuses to START a job once the day's measured spend
  reaches it, and the broker's `max_provider_calls_per_job` denies past a per-job
  ceiling counting ATTEMPTS (not successes — a loop whose calls all fail upstream
  would never trip a ceiling counting forwards). What remains unbounded is a
  single job's own cost: it is only known once its stream reports it, so one
  runaway job can exceed the daily ceiling within itself. The job timeout and the
  call ceiling are what bound that, not the budget.
- **Secrets in worker OUTPUT are flagged, not prevented.** A pattern detector
  records `secrets_suspected` on the record and warns; the evidence is kept
  byte-intact and the status is unchanged. It is deliberately low-recall to stay
  low-false-positive, so it is a tripwire, not a control.

## Install-time risk

Installing over a live wrapper, agent file or skill would change a running
manager underneath itself. This repo must not do that: it installs as a package
plus one launcher and owns nothing else on the machine.

A related and sharper case, because it is easy to get wrong in a lab: if the
launcher on `PATH` is a symlink into a working tree, its *path* is stable while
its *content* changes with every edit. Pin a released copy for anything beyond
local iteration — a caller that digests the launcher is pinning nothing
otherwise.
