# Observability without context flooding

> **This is a dated design note, not a reference.** It records the reasoning and
> the measurements behind the observability design, including one correction made
> during implementation. The work it describes has landed. Where it and the code
> disagree, the code is right; `switchgear capabilities` and the schemas are the
> executable contract, and `docs/INTEGRATION.md` is the caller-facing one.

Two requirements that pull in opposite directions, and must both be met:

1. **You cannot see what a delegated agent is doing.** Unlike a native Claude
   subagent, whose events the host harness renders inline as they happen, a
   cross-provider delegation is a black box until it finishes.
2. **A delegating agent must not have its context flooded** by the subagent's
   output. Native subagents avoid this precisely by *not* putting the child's
   stream into the parent's context — the parent gets a report, the human gets
   the live view.

The resolution: **observability and context are different consumers.** One
stream on disk; several projections over it; the cheap one is the default.

---

## Where each tool stands today (measured 2026-08-18)

| Tool | Live data exists? | Reachable? |
|---|---|---|
| `codex-companion` | yes — a `.log` grows during the run | no `logs`/`tail` subcommand; you must find the path in the launch JSON |
| `the old OpenCode wrapper` | yes — `opencode run --format json > "$out"` appends as it goes | nothing surfaces it |
| **switchgear, as measured** | **no** — stdout was buffered to a `tempfile.TemporaryFile()` and `evidence/events.jsonl` was only written after the process exited | nothing to reach |

So for Codex the data existed and the ergonomics didn't. For switchgear the data
did not exist at all. That was the first thing to fix, because everything else
depended on it.

**Since fixed.** Evidence now streams as it arrives (`job.py`, `process.py`), and
`logs`/`status` surface it. The rest of this note describes the design that got
there, and the one wrong turn taken on the way.

---

## The numbers that decide the design

Measured on a real multi-file write job (17 events, a small change):

| Projection | Bytes | ~Tokens | Ratio |
|---|---|---|---|
| full stream | 17,847 | ~4,461 | 1× |
| digest | 452 | ~113 | **40× smaller** |
| summary (`--json` record) | 225 | ~56 | **80× smaller** |

The full stream scales with job length; digest and summary are **bounded** and
stay roughly flat. On a long job the gap is one to two further orders of
magnitude. This is why the default must be summary and the full stream must
never reach a parent agent's context by accident.

---

## The design

### One stream

`evidence/events.jsonl`, append-only, written **as events arrive**.

> **CORRECTION.** An earlier version of this note justified the design with "the
> job directory is not bind-mounted into the sandbox, so a worker cannot tamper
> with its own record." **That reasoning is wrong and the obvious implementation
> it suggests is unsafe.** Bind-mounting is not the only route to a file:
> **stdout is an inherited file descriptor.** Hand the child the evidence file as
> its stdout and it inherits fd 1 against that open file description — it can
> `lseek(1, 0)` + `ftruncate` and erase everything it has already emitted, with
> no path access whatsoever. Moving the sink from a controller-private temp file
> into the job directory would have quietly re-opened precisely what
> "persist evidence before the integrity asserts" exists to prevent.
> Caught by `switchgear-opus-2` while implementing it.

The safe shape, as implemented: the child gets **pipes**; controller threads
drain them and stream to disk. The worker can append and nothing else — it never
holds a descriptor on the evidence file.

Two consequences to keep in mind:

- The drains must keep draining even after the cap is hit. A reader that stops
  reading deadlocks the worker on a full pipe.
- **`RLIMIT_FSIZE` no longer bounds the stdout spool**, because a pipe is not a
  file. The drain cap does. `RLIMIT_FSIZE` still bounds files the worker writes
  into its worktree, which is its actual job.

This replaces the temp-file buffering in `process.run_sandboxed`. It is the
enabling change: nothing else here is possible without it.

### Three projections, cheapest by default

| Projection | Who it's for | Guarantee |
|---|---|---|
| **summary** — the existing `--json` record | a delegating agent, always | bounded, ~50-100 tokens |
| **digest** — STRUCTURED: state, counters, tokens/cost, parsed failures | an agent that needs to know *what happened*, or is debugging a failure | **hard byte cap**, states when it truncated. Not prose — see the orchestrator section for why |
| **full** — the raw stream | a human terminal, a TUI, the orchestrator's UI, a file tail | unbounded — never for agent context |

### Enforcement, not advice

A convention that says "please don't pipe the full stream" will be violated. So:

- job-running commands emit the **summary only**; that stays the default forever.
- `logs --format` has no default that is `full`; choosing it is explicit.
- `--digest` enforces a byte cap in code and appends a truncation marker.
- `status` is the *polling* answer: state, elapsed, tool-call count, last tool,
  tokens and cost so far — roughly 30 tokens per poll. That is the equivalent of
  watching a spinner, and it is what a parent agent should call in a loop, never
  `logs --follow`.

### A provider-neutral vocabulary

The digest and any UI are computed from **normalized** events, not raw provider
output. The normalized contract is:

```
status   {sessionId}                 # REQUIRED: without it resume cannot exist
                                     # NB: the SOURCE field is `sessionID` (capital
                                     # ID) and appears on EVERY provider event, not
                                     # on a dedicated status event -- see below
tool     {name, target}              # rendered as text by the orchestrator; keep minimal
text     {content}                   # truncate at write time (the orchestrator uses 400 chars)
progress {turns, costUSD, tokens}    # a running job has no finished event
finished {status, exitSummary, turns, costUSD, tokens, sawTerminal}
         # status: completed | completed_empty | needs_input | failed
         # needs_input parks the ticket back to the operator -- load-bearing
```

Every line carries the `event` discriminator shown above. `v` is carried by the
written-out normalized stream — `evidence/events.v1.jsonl` and
`logs --format normalized` — and not by the byte-capped `logs` digest, which is a
projection over the same events and can add a final `truncated` line of its own.
`completed_empty` means the execution succeeded without non-whitespace
closing assistant text; it does not say whether files changed. Change presence
comes from the controller-computed `change.state` and `freeze.changed_files`,
and the enum name remains unchanged until the contract-v1-rc1 compatibility
review.

`changed_files` is deliberately absent: the orchestrator derives the result manifest from
git itself and ignores agent-reported file lists. Keep it internally if useful.

This is the structural advantage over `the old Codex wrapper`, which passes Codex's raw
format through: one viewer, one digest implementation, and one `normalizeLine`
in the orchestrator work for OpenCode, Codex and Grok alike. Provider-specific shapes stay
inside each adapter's `parse()`.

Cost and token counters are already present in real OpenCode `step_finish`
events (`tokens.total`, `cost`), so `finished` can carry them today — which also
gives the quota work its input.

---

## Consumption by the orchestrator

Confirmed against the consuming orchestrator's source, with file:line
references so it can be re-verified. This section is **fact**, not inference.

### Write to the codex adapter shape

The orchestrator supports both models, per adapter. The claude lane is stdout-as-pipe
(`server/lib/agents/claude.mjs:99-127`). The codex lane is exactly what switchgear
already does: a detached background job whose stdout yields only a job id, after
which the adapter polls job-state JSON/log files and can reattach after a daemon
restart (`codex.mjs:908+`, `capabilities.canResume=true`, `liveInput=false`).

So "append-only JSONL + job id on stdout" does **not** fight the dispatcher — it
is the better-behaved pattern, because it survives daemon restarts and the pipe
model cannot. Build the adapter to the codex shape.

**New constraint (a tracked change in the consuming orchestrator):** adapters implement `executionEnv(base)` and the
dispatcher records an *execution profile* — a digest of the exact spawn env plus
the resolved executable path — at first spawn, enforced on resume. A stable
absolute launcher path makes this trivial; a "latest version wins" resolver is
what they had to pin away for codex.

> **Watch out:** `~/.local/bin/switchgear` is currently a symlink into the
> working tree. The *path* is stable but its *content* changes with every edit.
>
> The orchestrator's ruling on this (their words): profile pinning for the switchgear
> adapter needs **a content digest of the launcher, not just a resolved path** —
> "same class as the codex 'latest wins' drift we pinned away, one level
> deeper." Pinning a path that always resolves is worthless when what it
> resolves *to* changes underneath you.
>
> Two consequences. The adapter must digest the launcher (and arguably the
> `python/switchgear/` tree it execs). And for anything beyond lab use, install a
> **released copy** rather than a symlink into a working tree — the convenience
> that made today's iteration fast is precisely what breaks reproducibility.

### Event vocabulary — corrected

The first draft was missing fields the orchestrator actually consumes
(`claude.mjs:113-147`):

| Field | Why |
|---|---|
| `event` ∈ `status` / `tool` / `text` / `progress` / `finished` | the discriminator for the complete normalized event vocabulary |
| `v` | the contract version carried by every normalized event |
| `sessionID` — see correction below | captured for resume; **without it, reply-by-restart cannot work at all** |
| `turns`, `costUSD` (deltas or totals; both handled at `:123-127`) | cumulative usage for the record |
| `finished.status` ∈ `completed` / `completed_empty` / `needs_input` / `failed` | `needs_input` parks the ticket back to the operator; `failed` records provider errors and streams that ended without a terminal event |
| `exitSummary` text | shown on the record |
| `text{content}` | mid-run display; the orchestrator truncates to 400 chars per line (measured in the consuming orchestrator) |

Here too, `completed_empty` is transcript emptiness, not diff emptiness. Read
`change.state` and `freeze.changed_files` for controller-measured change
presence; the name is frozen until the contract-v1-rc1 compatibility review.

**Drop `changed_files` from the orchestrator-facing contract.** The orchestrator never trusts
agent-reported file lists: it derives the result manifest itself from git at
finalization (a tracked change in the consuming orchestrator) and validates the landed tree against it at merge
(a tracked change in the consuming orchestrator). Keep it for our own viewer if useful; the orchestrator will ignore it.

`tool{name,target}` is rendered only as text there — keep it minimal.

### Correction: the vocabulary was wrong until it was measured

Step 2 could not be written from this document. No real provider stream existed
anywhere in the repo — the mock was the only evidence of the vocabulary, and the
mock is exactly what invented a fiction that 64 tests then validated. So
`switchgear-opus-2` captured a live run and committed it as
`tests/fixtures/opencode-real-scout.jsonl`. Verified against that fixture:

| I wrote | Reality |
|---|---|
| `sessionId` | **`sessionID`** — capital ID |
| on a dedicated `status` event | on **every** event, at top level |
| terminal = `step_finish` | terminal = `step_finish` **with `reason: "stop"`**; `"tool-calls"` only ends a step (the fixture has 3 `step_finish`, 1 terminal) |
| read `turns` / `costUSD` | neither field exists. Cost and tokens are **per-step and must be summed**; `turns` is derived from the `step_start` count |

The first row is the one that mattered: a normalizer written from this doc would
have looked for `sessionId`, found nothing, and silently produced no session
identifier — on the single field without which resume cannot exist. It would have
passed every test that did not check the value.

The fixture now carries a tripwire asserting the real type set and the **absence**
of `complete`, so the mock's fiction cannot be re-adopted as truth. The mock had
already drifted a second time — its slow-stream behaviour emitted events the
normalizer could not read — and is now shaped from the fixture.

**Rule for the next provider:** capture a real stream and commit it as a fixture
*before* writing the normalizer. Never derive a wire format from a mock, and
never from a design note — including this one.

### Mid-run steering is NOT required

`capabilities.liveInput` is per-adapter: claude `true`, codex `false`. A reply to
a running codex dispatch is refused with 409 and works instead as a **cold
resume** — new process, prior context via the provider's own resume mechanism,
keyed on the captured `sessionId`.

So switchgear needs **no stdin path into the sandbox**. What it does need is a
durable session/thread identifier surfaced in events, or resume cannot exist.

### The verify collision is shared, and unresolved on both sides

The orchestrator has the *same* problem. Its verification runs in a fresh
detached checkout with pre/post probes, and the post status uses
`--untracked-files=all --ignored=matching` — so a Python suite creating
`__pycache__` there would raise its verification-mutated-worktree error. Their
golden suite is Node-based and hermetic, so they have not paid this cost yet;
their own review flagged "verify in a pristine checkout against non-fixture
projects" as untested.

**Therefore: do not treat the orchestrator as the layer that absorbs this.** Both layers
currently demand side-effect-free verify commands (`PYTHONDONTWRITEBYTECODE`,
`CARGO_TARGET_DIR`, npm cache redirection). A shared out-of-tree-cache convention
is an open design item, and the orchestrator has said it would likely adopt whatever
switchgear settles on.

Their suggested shape was "digest over tracked content only + an explicit
side-effect allowlist". **The first half was withdrawn after pushback** — see the
ruling below. Do not reintroduce it. Digesting
non-tracked content was earned by a live finding: a worker cannot stage, so all
its output is untracked, and `.gitignore` is worker-writable — dropping ignored
content re-opens hiding a payload behind it. The reconcilable version is: keep
non-tracked content in the *integrity* digest, and add an explicit, operator-owned
**side-effect allowlist** of paths permitted to change during verify without
invalidating the freeze. That preserves the anti-hiding property while making
verify survivable.

### Context budget — the orchestrator solved it, copy the approach

Bounded **at write time, not summarised after**: the full stream goes only to an
append-only per-dispatch JSONL on disk; emitted lines truncate to 400 chars;
verify output keeps head 1KB + tail 1KB with a parsed failure collector
(measured in the consuming orchestrator); the record carries only summary fields. The UI tails
the file, a delegating agent reads the record.

**Their one correction to the tiering above, and it is important:** make the
bounded tier **structured** — state, exit summary, parsed failures, counters —
**not a prose summary**. Prose self-reports from the worker are exactly what
The orchestrator distrusts; they tripwire agents whose self-report claims tests passed
when verify says otherwise.

That matches what this rail learned independently today: `handoff.changes` came
back with absolute paths and nothing validated it, and a reviewer's verdict
flipped between runs. **Never let a worker's self-description be load-bearing.**
The digest should carry counters and parsed facts; model prose belongs in the
full stream, clearly marked as a self-report.

## Build order

1. **Stream to `evidence/events.jsonl` as events arrive.** Everything depends on
   it. Keep the size cap and truncation reporting intact.
2. **Normalize the event vocabulary** behind an adapter `parse()`. Do this before
   adding providers, or you will write the digest three times.
3. **`switchgear logs <job> [--follow] [--format digest|full]`**, digest capped.
4. **Make `status` cheap and informative** — the polling answer for a parent
   agent.
5. **Background jobs** (`--background` returning a job id and log path). Then a
   caller launches, polls `status`, and reads `logs --digest` only if something
   looks wrong. That is the full solution to both requirements at once.
