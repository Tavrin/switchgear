# Integrating agent-ops

agent-ops is a **sandboxed execution rail**, not an orchestrator. It runs one
provider job inside an OS boundary and produces evidence. Scheduling, boards,
queues, merge policy and human review belong to whatever calls it.

It is designed to be driven by **any** agent or tool, with
[atelier](https://github.com/etiennedoux/atelier) as the first-class consumer but
not a dependency. agent-ops imports nothing from atelier and works standalone.

## The contract

Call the CLI. Everything is argv-only; nothing reaches a shell.

```
ai-opencode --json [--profile P] [--state S] [--provider ABS] <command>
```

| Command | Purpose | Terminal states |
|---|---|---|
| `scout <dir> "<prompt>"` | read-only inspection | `ok`, `provider_error`, `timeout`, `dirty` |
| `review <dir> <role> [--envelope F]` | read-only review; attaches + may promote when the envelope names `parent_job` | `ok`, `provider_error` |
| `write <dir> <role> --envelope F --token T` | bounded write in a leased worktree | `awaiting_review`, `provider_error`, `timeout`, `dirty` |
| `run --envelope F [--token T]` | dispatch by envelope `mode`/`role`/`cwd` | as above |
| `promote --subject J --review R` | atomic promotion under the worktree lock | `ok` or refusal |
| `lease acquire\|release\|show --dir D` | worktree lease lifecycle | — |
| `status <job-id>` | full persisted record | — |

`--json` prints a stable object: `job_id, status, mode, role, model, dir, exit,
error, artifacts{events,stderr,handoff}, freeze, review, provider_calls`.
Those keys are **additive-only**; new keys may appear, existing ones will not
change meaning. Without `--json` the output is `key=value` lines for humans.

Exit codes: `0` success · `1` refusal or provider error · `2` dirty (integrity
changed) · `124` timeout.

Every refusal is a single line on stderr beginning `ai-opencode: REFUSING — `.
Treat any refusal as fail-closed: no work was promoted.

## What the caller must supply

- **A state root**, provisioned once (`state provision DIR`), on a path that does
  **not** overlap the target worktree or its git dir. The rail refuses overlap,
  because the per-job writable HOME lives under the state root.
- **An absolute provider path.** There is no PATH lookup, ever. The live binary
  is refused unless `AI_OPS_ALLOW_LIVE_PROVIDER=1` and the path matches the
  pinned build.
- **A profile** naming allowed models and role→model/mode bindings. Model
  families come from the controller-owned registry, never the profile.
- **A lease token** for bounded-write (`lease acquire` then pass `--token`).

## Evidence

Each job writes `<state>/jobs/<job-id>/`:

- `result.json` — the schema-validated record (the authoritative outcome)
- `evidence/events.jsonl` — raw provider event stream, capped
- `evidence/stderr` — provider stderr, capped
- `evidence/handoff.json` — the worker's structured handoff (write jobs)

`sandbox-home/` is reclaimed after the record is written; set
`AI_OPS_KEEP_SANDBOX_HOME=1` to retain it for forensics.

## Writing an adapter

An adapter needs four things, all already available:

1. **launch** — spawn `ai-opencode --json …`; the process is the job.
2. **stream** — tail `artifacts.events`; it is newline-delimited JSON from the
   provider. Map it to your own event shape.
3. **stop** — kill the controller process. The sandbox dies with it: verified
   that SIGKILL of the controller leaves zero surviving `bwrap` or provider
   processes (pid namespace + `--die-with-parent`).
4. **result** — read `result.json`, or the `--json` object on stdout.

For **atelier** specifically the mapping is direct: `write` corresponds to a
dispatch into an isolated worktree; `awaiting_review` is the state before the
merge gate; `promote` is a verification step, not a merge — atelier's
human-clicked merge remains the only path to main. agent-ops adds an OS boundary
*under* atelier's worktree isolation; it does not replace the merge gate, the
event log, or verification from the real test suite.

## Environment knobs

| Variable | Effect |
|---|---|
| `AI_OPS_STATE`, `AI_OPS_PROFILE`, `AI_OPS_PROVIDER` | defaults for the flags |
| `AI_OPS_WRITE=1` | required kill-switch for any bounded-write job |
| `AI_OPS_ALLOW_LIVE_PROVIDER=1` | permit the pinned live provider |
| `AI_OPS_PROVIDER_CREDENTIAL_FILE` | credential file (default `~/.config/ai-ops/provider-credential`, mode 600 enforced) |
| `AI_OPS_PROVIDER_UPSTREAM` | broker upstream base URL |
| `AI_OPENCODE_TIMEOUT` | per-job timeout, bounded by the profile |
| `AI_OPS_MIN_FREE_BYTES`, `AI_OPS_MAX_UNTRACKED_BYTES` | disk/digest bounds |

## What the caller must NOT assume

- **Review is not an authority boundary.** The reviewer is another untrusted
  model. `reviewed_files` proves it named the subject's change; it cannot prove
  the change is good. Keep a human in the loop for anything reaching main.
- **Aggregate disk is not bounded.** Single files are capped in the kernel
  (`RLIMIT_FSIZE`) and the rail refuses to start without headroom, but bwrap
  offers no quota for a bind mount. Run bounded-write on a filesystem you are
  willing to see filled, or a size-limited one.
- **The provider has network access** while a job runs. The credential itself
  stays in the controller (see the broker), but the provider proxies through it
  for the job's duration.
