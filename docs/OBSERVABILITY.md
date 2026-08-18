# Observability without context flooding

Design note for the delegation-visibility work. Two requirements that pull in
opposite directions, and must both be met:

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
| **agent-ops** | **no** — stdout is buffered to a `tempfile.TemporaryFile()` and `evidence/events.jsonl` is only written after the process exits | nothing to reach |

So for Codex the data exists and the ergonomics don't. For agent-ops the data
does not exist yet. That is the first thing to fix, because everything else
depends on it.

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

`evidence/events.jsonl`, append-only, written **as events arrive**. It lives in
the job directory, which is *not* bind-mounted into the sandbox, so a worker
still cannot tamper with its own record. `RLIMIT_FSIZE` still bounds it.

This replaces the temp-file buffering in `process.run_sandboxed`. It is the
enabling change: nothing else here is possible without it.

### Three projections, cheapest by default

| Projection | Who it's for | Guarantee |
|---|---|---|
| **summary** — the existing `--json` record | a delegating agent, always | bounded, ~50-100 tokens |
| **digest** — tool names, counters, tokens/cost, truncated final text | an agent that needs to know *what happened*, or is debugging a failure | **hard byte cap**, states when it truncated |
| **full** — the raw stream | a human terminal, a TUI, atelier's UI, a file tail | unbounded — never for agent context |

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
output. Straw man:

```
started  {job, provider, model, mode, role}
tool     {name, target}
text     {content}
finished {status, exit, changed_files, tokens, cost}
```

This is the structural advantage over `the old Codex wrapper`, which passes Codex's raw
format through: one viewer, one digest implementation, and one `normalizeLine`
in atelier work for OpenCode, Codex and Grok alike. Provider-specific shapes stay
inside each adapter's `parse()`.

Cost and token counters are already present in real OpenCode `step_finish`
events (`tokens.total`, `cost`), so `finished` can carry them today — which also
gives the quota work its input.

---

## Consumption by atelier

Confirmation was requested from `atelier-fable-agent` and had not arrived when
this was written; treat the following as inference to be checked, not fact.

Atelier's `docs/AGENTS-ADAPTERS.md` describes `normalizeLine` mapping
Claude-style NDJSON, which suggests it reads the spawned process's **stdout as a
pipe**. agent-ops instead writes an append-only file and prints a small JSON
record on stdout. Two ways to fit:

- the adapter tails `artifacts.events` (already published in the `--json` record)
  and feeds `normalizeLine`; or
- agent-ops grows a `--stream` mode that mirrors normalized events to stdout as
  NDJSON while the summary goes to a file.

**Ask before building.** If atelier wants stdout NDJSON, `--stream` is a small
addition; if it is happy tailing a file, do nothing.

Two things atelier will need that are not yet settled:

- **Mid-run steering.** Atelier can reply to a running agent. agent-ops has no
  stdin path into the sandboxed process. If that capability must survive, it
  changes the sandbox design and should be settled before the adapter is written.
- **Verify collides with the freeze.** Atelier's verify step runs the real test
  suite; running a Python suite creates `__pycache__`, which changes the worktree
  and invalidates the frozen digest, so promotion refuses. Observed today. The
  fix belongs in the adapter — verify on a copy, or set `PYTHONDONTWRITEBYTECODE=1`
  and equivalents — **not** in the digest, because including ignored files is a
  deliberate measure against a worker hiding payloads behind `.gitignore`.

---

## Build order

1. **Stream to `evidence/events.jsonl` as events arrive.** Everything depends on
   it. Keep the size cap and truncation reporting intact.
2. **Normalize the event vocabulary** behind an adapter `parse()`. Do this before
   adding providers, or you will write the digest three times.
3. **`ai-opencode logs <job> [--follow] [--format digest|full]`**, digest capped.
4. **Make `status` cheap and informative** — the polling answer for a parent
   agent.
5. **Background jobs** (`--background` returning a job id and log path). Then a
   caller launches, polls `status`, and reads `logs --digest` only if something
   looks wrong. That is the full solution to both requirements at once.
