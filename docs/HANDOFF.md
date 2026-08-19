# Handoff — agent-ops as the universal agent invocator

Written 2026-08-18 by the session that built the current rail, handing lead to
`agent-ops-opus-2`. Everything below is measured unless marked as opinion.

---

## 1. The mission

Turn agent-ops into **one tool that invokes any coding agent, on any model the
caller chooses, inside a real security boundary, producing evidence** — and
retire the fragmented per-agent wrappers into it.

It replaces `the old Codex wrapper`, `the old OpenCode wrapper`, `the old read-only wrapper` and friends. It does **not**
replace atelier. Atelier is the orchestration layer above (board, queue, verify
from the real test suite, event log, human merge gate); agent-ops is the
execution rail below it. The hard rule:

> **The moment agent-ops grows a queue or a board, it has become a second
> atelier. Don't.**

What makes it better than what it replaces: `the old Codex wrapper` is another project-specific and
Codex-specific, `the old OpenCode wrapper` is another project-specific and OpenCode-specific. agent-ops
is neither — **the calling agent picks the provider and the model**, per job,
from a controller-owned registry.

---

## 2. Where things stand

Updated 2026-08-18 after the operability and hardening work. Suite: **387 tests across 17 suites** (`bash tests/run.sh`), green on this machine AND against a
synthetic clean HOME with no providers installed. CI runs it on every push.

20 commands, 38 modules, ~10k lines.

Installed here as `~/.local/bin/ai-opencode` → symlink to the working tree, so
edits are live immediately.

**All four providers live on both lanes.** OpenCode, Claude Code and Codex at the
FULL credential tier (the token never enters the sandbox — placeholder in, real
value swapped by the broker); Grok at the FALLBACK tier (access token inside,
refresh stripped) because its CLI validates its session locally. Every lane is
proven by a completed job, not by inspection — the last one, Grok bounded-write,
closed 2026-08-18 with a cross-vendor review and promote.

**Security properties, measured from inside the sandbox rather than assumed:**

- READONLY: worktree, git dir, primary and siblings all unwritable; host `$HOME`,
  sibling worktrees and the state store simply **do not exist** (`ENOENT`).
- bounded-WRITE: only the leased worktree is writable; the git dir stays
  read-only, which is why the controller commits and the worker never runs git.
- **No network** when a broker is in play (`--unshare-net`). Measured:
  `direct_internet: BLOCKED`, `via_broker: OK 200`.
- **Credential never enters the sandbox** at the full tier. The broker allowlists
  the inference paths, pins the request model, and counts ATTEMPTS against the
  per-job ceiling. Proven live: a Grok job was denied mid-run for reaching at
  `grok-4.6` while pinned to `grok-4.5`.
- **Provider children cannot outlive their job.** `--unshare-pid` makes the
  sandbox pid 1 of its own namespace. Measured on a real Codex job: 55 matching
  host processes before, 55 after. This is the leak that cost another project
  754 processes and 15.4 GB with swap exhausted.
- `RLIMIT_FSIZE` caps any single file the worker writes (kernel-enforced).
- Evidence is written through pipes drained by the controller, so a worker can
  append to its own record and never seek back over it.

**The operability layer**, all added after dogfooding showed the gaps were absent
systems rather than broken ones: `jobs`, `doctor`, `gc`, `capabilities`,
`quota --rollup`, provider health, a concurrency cap, per-role reasoning effort,
and secret scanning of worker output.

**Known limits, stated because they bound what this is ready for:** Linux only
(bwrap); not a uid boundary; prompt injection is unmitigated by design — the
containment limits what a steered worker can *do*, not what it can *say*; spend
is bounded before a job, not within one; single operator, single machine, no
authn on the state root.

---

## 3. The single most important lesson

**Six defects reached production today. Five adversarial review rounds found
none of them. Running the thing found all six.**

1. The committed mock invented an event vocabulary (`{"type":"complete"}`) that
   real OpenCode never emits (`step_start`/`tool_use`/`text`/`step_finish`).
   64 tests validated a fiction. Worse: `handoff`/`review` objects appear **zero
   times** in a real stream, so the entire promote chain could never have run
   against a live model.
2. The broker forwarded only `content-type`/`accept`, so the upstream CDN
   returned a Cloudflare 403.
3. `baseURL` appended `/v1` to an upstream already ending in `/v1` → 404.
4. The launcher derived `ROOT` from `$0` without resolving symlinks — it broke
   the instant it was installed, having worked for every prior invocation.
5. The broker denied `/messages`, silently breaking every Anthropic-shaped model.
6. `promote()` never inspected `findings` at all — a reviewer could document real
   defects and still promote. The F13 spec required this; nothing enforced it.

**Corollary for you:** adversarial review is good at code that runs and blind to
code that has never run. Prioritise live exercise over more review rounds. Every
new provider adapter must be proven against the real binary before you trust it.

**Second lesson:** a live reviewer, same model, same diff, same prompt, returned
`needs_changes` on one run and `promote` on the next — documenting the same two
defects both times. **Review is a probabilistic quality signal, not an authority
boundary.** Never let it be the only gate; atelier's human-clicked merge stays.

---

## 4. What to absorb, and from where

Survey of what exists on this machine (`~/.local/bin`):

| Tool | Worth taking | Worth dropping |
|---|---|---|
| `the old OpenCode wrapper` | fail-closed dirty snapshot; role→model table; **bash denied entirely** in the agent posture; model allowlist refusal | its integrity model is weaker than agent-ops' freeze; credential sits in the agent env |
| `the old Codex wrapper` | **background job lifecycle** (launch → poll → fetch → board); spec-file discipline; worktree creation | Codex-specific and another project-specific coupling |
| `the old read-only wrapper` | nothing — already superseded and deleted from this repo | — |
| `the old merge-policy script`/`ship`/`push`/`lane-*` | nothing — that is **merge policy**, which is atelier's job | do not absorb |
| `codex-quota.mjs`, `claude-quota` | **quota brokering** — agent-ops has none and will burn credit until something 402s | — |
| atelier | nothing to take; it is the layer above | — |
| `~/.claude/skills/*-delegation` | doctrine stays as skills, not code | — |

### The two real gaps

1. **Background jobs.** agent-ops blocks for the whole job. Every long run today
   had to be hand-backgrounded. Needs launch/poll/result/cancel with job ids —
   `the old Codex wrapper` is the model to copy.
2. **Quota awareness.** No concept of it. Read
   `~/.cache/ai-quota/{claude,codex}.json`; route or refuse on remaining budget.

---

## 5. Multi-provider: where the danger is

Today's rail is OpenCode-shaped throughout: `job.py` hardcodes
`run --pure --dir --model --agent --format json --title`; `events.py` expects
OpenCode's stream; `policy.to_opencode_runtime()` emits OpenCode config;
`provider.isolation_env` sets `OPENCODE_*`; `compat` pins one binary.

The seam already exists — every registry model carries a `provider`, and
`registry.provider_record()` maps it to an upstream + credential name. Build the
adapter interface there: `argv(job)`, `env(home, policy)`, `parse(stdout)`,
`version_check()`, `agent_definition(policy, role)`.

**Be warned:** provider-config isolation (finding F04) took four review rounds to
settle for OpenCode alone — proving that host global agents, plugins and
`OPENCODE_PERMISSION` cannot leak in. Each new provider reopens that question
from scratch, with its own config discovery, credential store, event vocabulary
and permission model. Codex and Grok both have native CLIs on this machine
(`codex`, `~/.grok/bin/grok`).

> **Corrected 2026-08-18 by measurement — see `PROVIDERS.md`.** The claim that
> Grok has "no agent/permission model" is wrong: it has `--agent`, `--agents`,
> `--allow`/`--deny` and headless NDJSON output. And Codex's isolation question
> is already settled (the synthetic HOME hides its auth, MCP servers, history and
> config); what actually blocks a Codex adapter is that its auth is ChatGPT OAuth
> rather than an API key, which does not fit the broker.

Verify each adapter with a **no-model probe inside the real sandbox** (the
technique that settled F04: run the binary's own config-dump command under
`build_bwrap_argv` and diff host vs isolated resolution).

---

## 6. Invariants that must not regress

Every one of these was earned by a specific bug. Breaking one silently re-opens it.

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

---

## 7. Unfinished work, in priority order

Everything from the original list is closed. What follows is what stands between
this and "a small team can rely on it", hardest last.

1. **Prompt injection is unmitigated.** The threat model names it: containment
   limits what a steered worker can *do*, not what it can *say*. In readonly the
   blast radius is a wrong answer; in bounded-write the worker writes into the
   worktree and the only check is a review performed by another model, which is
   equally steerable. This is the live attack surface for anything pointed at
   content the operator did not write. **Design work, not a fix** — it needs a
   decision about how far the boundary should go.
2. **Not a uid boundary.** No user namespace: the worker runs as the invoking
   user. Mount and network isolation hold, but a bwrap escape or a mount mistake
   is the whole account rather than a container. Also design work.
3. **Never run under real load.** The concurrency cap was verified at cap=1 with
   two jobs. No soak test, no many-worktree fan-out, no multi-day run. Everything
   verified so far is one operator, roughly sequential.
4. **Fixture freshness is manual.** `providers verify` checks that a new build
   still offers the CLI surface the adapter's argv needs, and explicitly does NOT
   check the event vocabulary — that needs a re-captured stream. These CLIs update
   weekly, so a silent normalization regression is plausible and would present as
   "jobs stopped working" with no obvious cause. A check that flags when an
   installed version has moved past the version its fixture came from is cheap and
   unwritten.
5. **No live channel into a running job.** Messages are launch-time inputs plus
   cold resumes. Another project built a `queue` command because
   cancel-and-relaunch was otherwise the only way to add information to a running
   lane, and measured five of seven restarts as pure waste. Whether this belongs
   here or in the orchestrator is unresolved.
6. **Grok is fallback tier.** Its access token is inside the sandbox because its
   CLI validates the session locally. Egress is still broker-locked and the
   refresh token is stripped, but it is a weaker claim than the other three.
7. **The atelier adapter lane.** agent-ops's side is built
   (`docs/INTEGRATION.md`, "atelier lane contract"); the adapter is atelier's to
   write and they own the scheduling. Do NOT rename our `execution-profile` fields
   preemptively to match their `executable`/`digestPaths` shape — they will ask
   where our behaviour is ground truth.

---

## 8. Orientation

```
agent-ops/
  bin/ai-opencode              4-line launcher, resolves symlinks, no decisions
  python/ai_ops/
    cli.py         subcommands, --json contract, reviewer diff injection
    job.py         run_job(): the whole worker lifecycle
    sandbox.py     build_bwrap_argv() — the authority boundary
    broker.py      credential broker (loopback or unix socket)
    sandbox_relay.py   in-sandbox TCP->unix bridge (holds no secret)
    identity.py    worktree identity, digests, delta attribution
    review.py      promote(): every gate lives here
    policy.py      CompiledPolicy + generated agent definitions
    registry.py    controller-owned models/providers
  docs/INTEGRATION.md      the caller contract — read this first
  docs/ADDING-A-PROVIDER.md  how a new harness is added (7 methods, ~25 lines)
  docs/FRICTION-AUDIT.md   another project's failure catalogue, checked against this
  docs/PORTABILITY.md      what is Linux-bound and what to do elsewhere
  docs/REVIEW-6d217a6.md   the independent review that started the remediation
  tests/run.sh             hermetic, must stay free of live calls
  tests/live.sh            opt-in live smoke (AI_OPS_LIVE=1), uncommitted
  .github/workflows/tests.yml  CI: the suite on a machine that is not the author's
```

Fixtures: `~/Documents/agent-ops-dogfood` and `~/Documents/agent-ops-trial` are
disposable and safe to delete or reuse.

**Standing constraint:** disposable and lab repositories only. The write lane now
has a good many live cycles behind it across all four providers — enough to call
it working, not enough to call it trusted with someone else's repository. See
§7.1–7.3 for what would change that; prompt injection is the one that matters
most, and it is unaddressed.
