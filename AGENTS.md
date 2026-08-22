# AGENTS.md — for an AI agent working on Switchgear

Read `CONTRIBUTING.md` first; it is short and it holds the one rule that explains
everything else. This file is the part you specifically will need, and the part
previous agents got wrong.

## What this project is

Switchgear runs one coding-agent job inside a `bwrap` sandbox and produces a
record of what happened. One invocation is one agent, one process, one sandbox.
It is deliberately **not** an orchestrator: no queue, no board, no scheduling,
no fan-out. If a change you are considering adds one of those, it will be
refused no matter how well it is written.

Orientation, in reading order:

| file | what it tells you |
|---|---|
| `docs/CONTRACT-V1-RC1.md` | the frozen contract status — read before trusting any contract claim elsewhere |
| `docs/INTEGRATION.md` | the caller contract |
| `docs/INVARIANTS.md` | invariants that must not regress, each with the bug that earned it |
| `docs/ROADMAP.md` | what is unfinished, and why each gap is still open |
| `docs/ADDING-A-PROVIDER.md` | the adapter seam |
| `docs/THREAT-MODEL.md` | what is defended and what is not |
| `docs/FRICTION-AUDIT.md` | failure modes from a real multi-agent deployment, checked against this code |

## The rule you are most likely to break

**A claim this tool makes must be true.** Not "should be", not "was when
written". A comment describing a check that does not exist is a defect of the
same severity as a crash, because the entire value of this tool is that its
output can be relied on afterwards.

Every one of these was a real finding here, most of them recent:

- a comment saying "then confirm the refresh token really is gone" above code
  that confirmed nothing
- a docstring promising it "never returns the matched text verbatim", directly
  above the code returning a bounded excerpt of the match
- a threat model saying spend was unbounded, months after the budget gate landed
- a refusal telling the user to run `providers verify`, which the parser did not
  accept
- a doc listing three items as "Open" that were all closed, one of them 230 lines
  above the function implementing it

So: **if you change behaviour, grep for what described the old behaviour.** Code
comments, docstrings, `docs/*.md`, error strings, and the `capabilities` output
are all part of the surface.

## How to know something is true here

Run it. This codebase has a strong bias toward measurement over reasoning, and it
is not stylistic — it is because reasoning has repeatedly been wrong:

- `--unshare-user --uid N` looks like a uid boundary. Measured: your files appear
  owned by the sandbox uid and stay writable. It confines nothing.
- Provider `--version` output looks harmless. It was being run on the host with
  the full caller environment, against an invariant stated in three places.
- A 400-line diff scan looked equivalent to the 5MB the reviewer was given. It
  was not, and the gate failed open above 2MB.

When you assert something about behaviour, say what command you ran and what it
printed. "I verified X" without the evidence is not verification.

## Traps specific to this codebase

**`pgrep` and process matching will fool you.** It has done so at least six
times, including matching the checker's own shell command. Liveness here is
always `(pid, start time, boot id)` — see `lease.py`. If you need to observe a
process from a test, use a heartbeat file, not a pattern.

**Absence is never evidence of a benign state.** A missing `result.json` meant
"running" for a cancelled job, a crashed background job, a dead foreground job,
and a job id that never existed. `jobstate.py` encodes the rule; do not add a
path that infers "fine" from something not being there.

**Tests can pass for the wrong reason.** A streaming test once passed against
code that wrote everything after exit. If your test would also pass against the
broken version, it is not a test yet. Where it matters, assert the *non-vacuity*
too — the containment test proves the child was alive before proving it died.

**Concurrency bugs are invisible at n=1.** `tests/soak.sh` found a cap that
over-ran and an `atomic_write_json` that was destructive with two writers. If you
touch leases, the state root, the broker or the concurrency cap, run it.

**Do not pin run-specific values in fixture tests.** Turn counts, costs and
phrases the model happened to say will break the next re-capture — which the
freshness check actively asks you to do. Assert the contract: read `num_turns`
off the stream rather than hardcoding it.

## Working here safely

- **Never run live provider jobs to test a change.** They cost real money.
  `bash tests/run.sh` uses a committed mock and is free. If a change genuinely
  needs a live check, say so and ask — do not spend on your own initiative.
- **Never commit a state root, a captured home, or a credential.** A retained
  sandbox home can hold a live access token. `.gitignore` covers the usual
  shapes; check `git status` before committing anyway.
- **Do not weaken a guard to make a test pass.** If `test_no_machine_paths.sh`,
  `test_no_dangling_doc_links.sh`, the bare-`rmtree` check or the
  no-provider-execution-outside-bwrap check fails, it has found something. Three
  of those caught regressions by the author within minutes of landing, and the
  doc-link gate found a fourth on the run that introduced it.
- **`test_no_project_nouns.sh` ships with an empty noun list, so by default it
  cannot fail.** That is not a spare guard: 14 references to a private consuming
  project reached the tree past a release-readiness pass precisely because the
  list was empty. If you develop against a consuming project, keep a list outside
  the repo — committing one would publish the names it exists to exclude — and
  point the gate at it:

  ```console
  $ export SWITCHGEAR_PROJECT_NOUNS=~/.config/switchgear/project-nouns.txt
  $ bash tests/policy/test_no_project_nouns.sh
  ```

  Note it scans `bin/`, `python/` and `skills/` only. Docs may cite a real
  consumer as a worked example; the substrate itself must name nobody.
- **Scope discipline.** Fix what was asked. If you find something else that looks
  serious, report it with evidence and stop; do not expand into it.

## Style that is load-bearing

Comments here explain *why*, and usually name the failure that caused the code to
be that way. That is not decoration — it is how the next reader avoids
re-introducing the bug. When you add a guard, say what it is guarding against and
what happened when it was absent.

Refusals name a remedy. `Refuse("bad job id")` is incomplete; say what a good one
looks like and where to find it. You are frequently the caller reading these, so
you already know why it matters.

## Before you open a PR

1. `bash tests/run.sh` — green, and not flaky. A flake is a bug; report it.
2. `bash tests/soak.sh` if you touched anything concurrent.
3. Grep for descriptions of the behaviour you changed, and update them.
4. State what you measured, not what you expect.
