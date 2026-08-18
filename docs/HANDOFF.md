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

## 2. Where things stand (all verified today)

HEAD `b83a33c` on `rem/stage0-security-remediation`. Suite: **71 hermetic + 3
config probes green** (`bash agent-ops/tests/run.sh`).

Installed: `~/.local/bin/ai-opencode` → symlink to the working tree, so edits are
live immediately.

**Working, proven live end-to-end** (real models, real repos, today):

| Lane | Proof |
|---|---|
| `scout` (readonly) | found planted bugs with line numbers |
| `write` (bounded-write) | multi-file edit, valid handoff, `awaiting_review` |
| `review` (readonly) | cross-vendor (kimi/moonshot judging deepseek) found two real defects the implementer missed |
| `promote` | atomic, generation CAS, refused correctly on every negative case |

**Security properties, measured from inside the sandbox, not assumed:**

- READONLY: worktree/gitdir/primary/siblings all unwritable; host `$HOME`,
  sibling worktrees, state store simply **do not exist** (`ENOENT`). `/` inside is
  `[bin, dev, etc, lib, lib64, proc, tmp, usr]`.
- bounded-WRITE: only the leased worktree is writable.
- **No network**: with a credential broker in play the sandbox runs
  `--unshare-net`. Measured: `direct_internet: BLOCKED`, `via_broker: OK 200`.
- **Credential never enters the sandbox**: broker holds it controller-side and
  injects `Authorization` upstream; the sandbox gets
  `broker-placeholder-not-a-credential`. Broker allowlists
  `/chat/completions` + `/messages` and pins the request model.
- Process ownership: SIGKILL of the controller leaves **zero** surviving `bwrap`
  or provider processes.
- `RLIMIT_FSIZE` caps any single file the worker writes (kernel-enforced).

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

---

## 7. Unfinished work, in priority order

**Closed 2026-08-18 by `agent-ops-opus-2`** (items 1-5 of the original list):

1. `tests/live.sh` retry-on-transport-failure was **already complete** -- the
   fear that the edit was cut mid-write was unfounded. Verified by running it,
   not reading it, and the harness that verified it is now a committed hermetic
   test (`tests/test_live_retry.sh`): it extracts `live_job()` verbatim and
   drives it with a stub CLI, so the rule cannot rot into "retry until green".
2. Doc drift closed. `THREAT-MODEL.md` and `CONTAINMENT.md` rewritten from the
   code. Grounding them turned up a real defect (below).
3. Dead artifacts deleted (`adapters/*`, `policies/*`), completing F20. They had
   drifted **weaker** than the generated config: a `/tmp/opencode/**` hole in
   `external_directory` and no `plugin: []`.
4. `openrouter/*` marked clearly. `ai-opencode models` now reports each id as
   reachable or UNREACHABLE with the path where its credential belongs.
5. Verify-mutates-worktree: `PYTHONDONTWRITEBYTECODE=1` in the sandbox env, so an
   agent-ops job cannot dirty its own freeze with `__pycache__`. The atelier-side
   half (its verify step runs the real suite *outside* this sandbox) is still
   atelier's adapter to fix.

**Found while doing the above** -- a live job with `AI_OPS_ALLOW_LIVE_PROVIDER=1`
and no credential fell through to the unbrokered path, and since `--unshare-net`
is requested only when there is a broker socket to bind, it ran the provider on
the **host network**. Now refuses. Same lesson as the six: "the sandbox has no
network" was true of every path anyone had run and false of one nobody had.

Still open:

0. **OAuth providers — where each stands.** Grok is LIVE at the fallback tier
   (chosen by Etienne 2026-08-18): access token in the sandbox, refresh stripped,
   egress broker-locked. Proven with real scouts. Codex and Claude Code are
   full-tier candidates (they send a placeholder to their redirected endpoint,
   measured) with the broker plumbing built and the registry scaffolded to the
   researched-correct upstreams/headers; each still needs its adapter, a captured
   fixture, and one ~$0.01 run to confirm the backend honours its OAuth access
   token. `docs/ADDING-A-PROVIDER.md` has the full fleet table and procedure.

1. **The OAuth broker** (`docs/PROVIDERS.md`, "The OAuth problem"). Target
   confirmed by Etienne 2026-08-18: one rail invoking OpenCode, Grok, Codex
   (ChatGPT account) and Claude Code (Claude account). All three OAuth CLIs
   measured structurally identical (access + refresh token in a readable file)
   and all carry backend-redirect + token-injection knobs, so the FULL
   containment tier extends to them: broker holds the tokens, refreshes
   controller-side, refresh token never crosses any boundary. Grok is first —
   its vocabulary is captured (`tests/fixtures/grok-real-scout.jsonl`), its
   adapter is shipped, and its auth file carries its own OIDC issuer/client id.
   The ordered engineering list is at the end of that section — note item 2:
   `resolve_provider` currently classifies any non-OpenCode binary as a *mock*,
   which is fail-closed but wrong, and must become per-provider pinning first.
2. **The atelier adapter lane.** atelier will spec it against their real
   capabilities/executionEnv test contracts once Etienne confirms in their own
   session — they correctly declined a green-light relayed through a peer.
3. **Launcher pinning (ATT-006).** Recommendation given: keep the symlink, pin by
   CONTENT digest, and digest `python/ai_ops/` too — the launcher is an 11-line
   stub, so digesting the resolved executable alone pins the one file that never
   changes. A released copy becomes right when the lab-only constraint lifts.
4. **The verify side-effect allowlist.** Joint ruling with atelier: integrity
   keeps covering untracked + ignored content; out-of-tree cache env vars are
   first-line (`PYTHONDONTWRITEBYTECODE` is done); the escape hatch is an
   operator-owned, registry-level allowlist. The "never project-declared" half is
   load-bearing — a project that can allowlist its own hiding place has none. The
   test to write is that a project-supplied entry is IGNORED, not merged.

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
  docs/REVIEW-6d217a6.md   the independent review that started the remediation
  tests/run.sh             hermetic, must stay free of live calls
  tests/live.sh            opt-in live smoke (AI_OPS_LIVE=1), uncommitted
```

Fixtures: `~/Documents/agent-ops-dogfood` and `~/Documents/agent-ops-trial` are
disposable and safe to delete or reuse.

**Standing constraint:** disposable and lab repositories only. The write lane has
a handful of live cycles behind it — enough to call it working, not enough to
call it trusted. Do not point bounded-write at a real project yet.
