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
| `<job cmd> --background` | launch detached; prints `job_id` at once and returns | — |
| `resume <job-id> "<msg>"` | continue that job's provider session with a new message | as for the original mode |
| `cancel <job-id>` | stop a backgrounded job (pid + starttime + boot_id checked) | — |
| `status <job-id>` | **cheap poll**, valid while the job runs: state, elapsed, turns, tool count, last tool, tokens, cost, sessionId (~30 tokens) | — |
| `status <job-id> --full` | the whole persisted record (exists only once finished) | — |
| `logs <job-id> [--format digest\|full]` | projections over `evidence/events.jsonl`; **digest is the default and is byte-capped in code** | — |

`--json` prints a stable object: `job_id, status, mode, role, model, dir, exit,
error, artifacts{events,stderr,handoff}, freeze, review, provider_calls,
cost_usd`.
Those keys are **additive-only**; new keys may appear, existing ones will not
change meaning. Without `--json` the output is `key=value` lines for humans.

### The atelier lane contract

Spec: `atelier:specs/wave-3/agent-ops-adapter.md` (owner-confirmed, atelier main
`04a3f07`). agent-ops's side of it:

- **Capabilities** the lane declares: `canResume: true` (sessionId-based),
  `commitsOwnWork: false` (the git dir is a read-only mount, so atelier's
  finalizer commits), `liveInput: false` (no stdin into the sandbox; replies are
  cold resumes), `liveStream: true` (the events file is tailable),
  `reportsCost: true`.
- **`finished.status`** maps 1:1 onto atelier's outcome states — except
  `sawTerminal: false`, which is **never** `completed`. A run that ended without
  the provider closing its stream reports `failed` with a truncation
  `exitSummary`, however much assistant text it emitted first. "Claims done,
  evidence truncated" is the suspicious case, and over-reporting truncation is
  the right default.
- **A running job emits no `finished` event at all**, only `progress` with the
  counters. The same partial stream means "still working" during a job and
  "truncated" after one, so the normalizer takes that fact from the caller rather
  than guessing — an adapter tailing for `finished` must not transition early.
- **`turns` and `costUSD` are totals**, not deltas.
- **`status.fence`** publishes the launch record's pid identity as
  `linux-proc-start:<bootId>:<startTime>` — atelier's own process-fence shape. A
  pid alone is not an identity, and it cannot be re-derived once the process is
  gone.

### Execution profile pinning

`ai-opencode execution-profile` returns the content pin an orchestrator records
at first spawn and enforces on resume:

```json
{"launcher": "...", "package": "...", "file_count": 27, "launcherDigest": "..."}
```

`launcherDigest` is sha256 over the resolved launcher **plus a deterministic walk
of `python/ai_ops/`** — sorted relative paths, per-file digest, digest of the
digest list, `__pycache__` excluded. Pinning by resolved path would be worthless
here: `~/.local/bin/ai-opencode` is a stable path that symlinks into the working
tree, so it always resolves while its content changes with every edit. And the
launcher alone is an 11-line stub, so digesting only the executable freezes the
one file that never changes. Digesting the digest list rather than the bytes
means a rename moves the pin too.

### `.atelier-workspace.json`

atelier writes its workspace identity token at the worktree root during dispatch.
**agent-ops does not exclude that filename from its integrity digest, and must
not.** Excluding a name creates a hiding place — precisely the finding that put
untracked and ignored content into the digest to begin with.

No special case is needed. The per-job delta is before-vs-after fingerprints, so
a token written before the job has the same fingerprint after and is never
attributed to the job, while a worker that *modifies* it does appear in the
delta — which is exactly what atelier refuses at merge.

This depends on atelier writing the token **before** invoking agent-ops.
Confirmed in their code, not assumed: the token is written at worktree
preparation (`dispatch.mjs:6205`) strictly before `agent.launch` (`:6279`), on
every path — verify, post-merge and merge-scratch checkouts all write it at
creation. Recorded as a contract on their side, with the undertaking that
agent-ops is told first if that ordering ever changes. If it ever lands
mid-dispatch, the token would read as worker-caused and this decision needs
revisiting.

### Quota and budget

`quota` reports two things and deliberately does not blend them:

- **External readings** for subscription pools that publish one
  (`~/.cache/ai-quota/{claude,codex}.json`). This rail does not spend those; it
  reports them so a caller routing across providers can decide. Every reading is
  reduced to one shape and carries its age, and one older than an hour is marked
  `STALE` — a stale reading is worse than none, because it invites a confident
  wrong decision. Route on `min_remaining_percent`: the worst window, since a
  weekly pool at 3% is not rescued by a five-hour window that just reset.
- **Measured spend.** The pool agent-ops actually bills (`opencode-go`) publishes
  no quota at all, so there is nothing to read — but every job's real cost comes
  back in its stream. `cost_usd` is on each record and appended to
  `<state>/spend.jsonl`.

Limits are **operator-owned**, in `~/.config/ai-ops/budget.json`
(`AI_OPS_BUDGET_FILE` overrides). They are never profile-declared, for the same
reason the model registry is not: a project that can raise its own ceiling does
not have a ceiling.

```json
{"daily_usd": 5.00, "max_provider_calls_per_job": 12}
```

- `daily_usd` refuses to **start** a job once the day's measured spend reaches
  it. Honest limit: this bounds spend before a job, not during one, since a job's
  cost is only known once its stream reports it.
- `max_provider_calls_per_job` is the per-job bound, enforced in the broker,
  which denies rather than throttles — a runaway agent that is merely slowed down
  still spends the budget, just later. It counts **attempts**, not forwards:
  `provider_calls.forwarded` means "a model answered", and a loop whose calls all
  fail upstream would never trip a ceiling counting successes.

Absent budget file means unlimited, and `quota` says so rather than implying a
limit exists.

### Steering a job: resume

`resume <job-id> "<message>"` continues that job's provider session. Every
provider supports it natively (codex `exec resume`, claude `--resume`, grok
`--resume`, opencode `run --session`), keyed on the session id the rail already
captures.

It is deliberately **not** a live channel into a running sandbox. The resumed
turn is a **new bounded job** with its own boundary, evidence and cost, and the
record carries `resumed: {session, from_job}` so a reader knows the model already
held context. That keeps the freeze/review chain reasoning about a complete
record rather than one shaped by inputs it never saw. The message is delivered as
the prompt, so the role instructions — and the rail's authority over what the
worker may do — are reapplied exactly as on a first run.

**Conversation state is persisted per worktree per provider**, under
`<state>/sessions/`. It has to be: providers keep history inside their HOME, and
each job gets a fresh synthetic HOME that is reclaimed afterwards — without this
a resume finds nothing and the CLI answers "No conversation found with session
ID". Only the paths an adapter names are persisted, never the whole provider
config directory, which is where credentials live.

All four providers are measured and resume live: Claude
(`~/.claude/projects`), Codex (`~/.codex/sessions`), Grok (`~/.grok/sessions`),
OpenCode (`~/.local/share/opencode`). A provider whose store is undeclared is
REFUSED rather than faked — resuming without it would start a fresh conversation
wearing the previous session's id.

Two constraints worth knowing. Grok emits its session id only in its terminal
event, so a Grok job that dies mid-run has nothing to resume from — a stream
property, not a storage one. And some stores are credential-ADJACENT on the host
(OpenCode keeps `auth.json` in the same data directory as its session database),
so the rail refuses to bind a session store containing anything
credential-shaped.

### Background jobs

Adding `--background` to `scout`, `review`, `write` or `run` launches the job in
its own session and returns immediately with:

```json
{"job_id": "...", "state": "launched", "pid": 1234,
 "events": "<state>/jobs/<id>/evidence/events.jsonl",
 "launch_stderr": "<state>/launch/<id>.err"}
```

The intended loop is: **launch → poll `status` → read `logs` only if something
looks wrong**. The job outlives the caller, so a delegating agent does not have
to hold a process open for the duration.

`status` reports `running` / `cancelled` / `died` for a job that has not written
a record yet, and the persisted status once it has. `died` matters: a job killed
before writing `result.json` has no record of its own, and reporting it as
`running` forever is the worst answer a poll can give an orchestrator, so the
launch record's liveness (pid **and** starttime **and** boot_id — pids are
recycled) is the fallback authority.

`cancel <job-id>` terminates the job's process group, escalating TERM→KILL, and
records the cancellation so a later poll says `cancelled` rather than `died`.

### Retention: `gc`

Nothing is ever removed unless you ask. `gc` is opt-in, reports by default, and
needs a selector — a bare `gc` refuses rather than guessing what "clean up" meant.

```
ai-opencode --state <root> gc --older-than 7d          # report only
ai-opencode --state <root> gc --older-than 7d --yes    # actually delete
```

Protected regardless of the selector: anything `awaiting_review`; any job whose
process is alive (re-checked at delete time, since a job can start between the
plan and the deletion); any review whose subject still awaits review; and any job
whose **liveness could not be established at all**. That last one matters —
`unknown` is not `dead`, and the rest of the rail never reads a missing record as
a benign state.

A cool-down floor (`AI_OPS_GC_MIN_AGE_S`, default 1h) applies **on top of** your
selector, so `--older-than 1s` still does not mean "delete everything".

Session stores need `--include-sessions` **in addition to** `--yes`: a job
directory is reproducible by re-running the job, while a session store is the
only durable copy of a conversation you may still want to `resume`. A store whose
worktree cannot be stat'ed is reported as **skipped**, never deleted —
unverifiable is not absent.

Every protected entry carries the reason it was kept, so a caller who expected a
job to go can see which rule kept it instead of concluding `gc` is broken. Bytes
are measured by block count, never `os.path.getsize`, which follows symlinks.

### Secrets in worker output

The rail guards credentials going **in** — the broker keeps them out of the
sandbox entirely for three of four providers. It is now also not indifferent to
what comes **out**: a worker that cats a `.env`, echoes an `Authorization` header
while debugging, or pastes a key into its own reasoning writes that straight into
evidence, which `logs` reads, review prompts quote, and the state root keeps.

Output is scanned after the job's record is built. A finding adds
`secrets_suspected: [{pattern, where, count}]` to the record and one `WARNING`
line to stderr. It does **not** change the job's status, and it never contains the
matched value — a finding that quoted the secret would make `result.json` a
second, more portable copy of it.

**Flagged, never destroyed.** Evidence is audit material; a rail that silently
rewrites the bytes it recorded is worth less than one that records honestly and
points at the problem. The evidence file stays byte-identical, and a test asserts
it.

Tuned for a low false-positive rate rather than coverage, because a detector that
fires on ordinary output is one everyone learns to ignore. Only vendor key
prefixes, JWTs, PEM private-key headers and value-carrying auth headers match;
bare high-entropy strings, hashes and UUIDs deliberately do not. The rail's own
broker placeholder is excluded by name, and the committed real provider streams
are asserted clean.

### Reasoning effort

Effort is **profile-owned**, set per role, never a caller flag:

```json
"roles": { "scout": { "model": "claude/claude-haiku-4-5-20251001",
                      "mode": "readonly", "effort": "low" } }
```

It is a cost and behaviour lever exactly like model choice, so it lives where
model choice already lives and is validated against the same kind of allowlist. A
bare `--effort` override would defeat the invariant the budget, the model
allowlist and the command allowlist all rely on.

An adapter reports one of **three** states, not a boolean — `supported` (with the
measured values), `unmeasured`, or `unsupported`. "This provider has no effort
control" and "nobody has measured which values it accepts" are different facts.
`doctor` prints the current table; today only Claude Code is `supported`
(`low, medium, high, xhigh, max`, enumerated by its own `--help`).

A request the rail cannot verify is **refused**, never silently dropped, and
refused before the job directory exists so it costs nothing. This matters because
Grok, Codex and OpenCode were each measured to **accept an unrecognised effort
value at parse time and run anyway** — passing one through would buy a job that
quietly ran at the model's default while the record claimed otherwise.

The value actually sent is recorded on the job as `effort` (null when none was).

### Checking the install before you depend on it

```
ai-opencode --state <root> --json doctor
```

Every check reports `{name, status: pass|warn|fail, detail, remedy}`, and any
check that reports a problem also names what to do about it — a refusal without a
remedy is a dead end for an agent, which cannot tell "you configured this wrong"
from "the model failed".

Exit is `1` only when something **failed**. Warnings never decide the verdict, so
CI can gate on `doctor` without an unused provider or an unfunded model pool
turning the build red. A provider you do not use, or a pool with no credential
installed, is a warning by design.

Doctor reports and never repairs — including no token refresh as a side effect of
being asked a question. Credential checks name the class, source path and time to
expiry, never the secret, so the output is safe to paste into a bug report.

### Finding jobs you have lost track of

`status` answers about one job you already know the id of. `jobs` answers what a
state root contains at all:

```
ai-opencode --state <root> --json jobs --state-filter awaiting_review
```

Every row carries the job's **live** state, not merely what it last wrote — a job
whose process is gone but which never persisted a result reads `died`, and one
whose liveness cannot be established at all reads `unknown`. Those are different
facts and the listing keeps them apart; neither is ever rendered as `running`.
`awaiting_review` is surfaced as its own boolean so a poller does not have to
know how the status is spelled.

The listing is bounded by default (20 rows, newest first, `truncated` and `total`
in the JSON), because an agent should not have to read a whole history to find
one job. `--all` removes the cap. Filters: `--state-filter`, `--since 30m|24h|7d`,
`--worktree <path>`, `--limit N`.

It is deliberately cheap: it reads each job's result record and start marker and
never opens `evidence/events.jsonl`. Use `logs` when you want the stream.

### Observing a running job without flooding your context

`evidence/events.jsonl` is written **as events arrive**, so a job can be watched
while it runs. There is one stream and several projections over it, and the cheap
ones are the defaults on purpose:

- **poll `status`** in a loop. That is the spinner equivalent, roughly 30 tokens
  a call, and it is the intended way for a delegating agent to follow a job.
- **read `logs` (digest)** only when something looks wrong. It is normalized,
  structured, and capped at 8 KiB in code; on truncation it emits a final
  `{"event":"truncated","dropped_events":N}` rather than trimming silently.
- **`logs --format full`** is the raw provider stream. It is unbounded and grows
  with job length. It is for a human terminal, a TUI or a file tail — never for
  an agent's context. There is deliberately no default that lands here.

The digest speaks a provider-neutral vocabulary (`status`/`tool`/`text`/
`finished`), so it reads the same whichever provider ran the job. `finished`
carries `status` ∈ `completed` | `completed_empty` | `needs_input`, plus `turns`,
`tokens`, `costUSD` and a bounded `exitSummary`. `sessionId` is surfaced because
without it a resume cannot exist.

Counters and parsed facts are the load-bearing part; model prose appears only as
a bounded `exitSummary` and is a self-report, not evidence.

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

## Multiple providers (OpenRouter, swarms)

Models are provider-qualified and the controller registry maps each provider to
an upstream and a credential name:

```
opencode-go/glm-5.3                     -> https://opencode.ai/zen/go/v1
openrouter/anthropic/claude-sonnet-4.5  -> https://openrouter.ai/api/v1
```

Install one credential per provider, mode 600:

```
~/.config/ai-ops/credentials/opencode-go
~/.config/ai-ops/credentials/openrouter
```

Each job starts a broker bound to *that model's* upstream and credential, and
pins the request to that model, so a job for one provider cannot spend another
provider's key.

**Why OpenRouter matters for review independence.** `different_family` is weak on
its own -- two ids can share a vendor. The registry therefore carries
`vendor_family`, and it is controller-owned: a project profile may name model
ids but can never declare a family, because that would let a profile manufacture
its own reviewer independence. OpenRouter gives a genuinely cross-vendor pool, so
`implement` on one vendor can be reviewed by another.

**Swarms.** The rail is one job per invocation; a swarm is N invocations by the
caller. Two constraints:

- Leases are per-worktree and exclusive, so **parallel writers need one worktree
  each**. Verified: a second writer on a held worktree is refused, and three
  concurrent jobs on separate worktrees run cleanly.
- Readonly jobs (scouts, reviewers) can fan out freely on the same worktree.

## Environment knobs

| Variable | Effect |
|---|---|
| `AI_OPS_STATE`, `AI_OPS_PROFILE`, `AI_OPS_PROVIDER` | defaults for the flags |
| `AI_OPS_WRITE=1` | required kill-switch for any bounded-write job |
| `AI_OPS_ALLOW_LIVE_PROVIDER=1` | permit the pinned live provider |
| `AI_OPS_PROVIDER_CREDENTIAL_FILE` | single-credential override; otherwise `~/.config/ai-ops/credentials/<provider>`, mode 600 enforced |
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
- **The provider can spend the credential while a job runs.** It cannot read it
  (the broker holds it) and it has no other network, but it is proxying through
  the broker by design, so it can issue model calls for the job's duration.
  Bound that by installing a per-provider key with its own budget.

## Network posture

When a credential broker is in play the sandbox runs with `--unshare-net` and
has **no network at all**. Its only reachable endpoint is a bind-mounted unix
socket to the controller-side broker, which allowlists the chat-completions path
and pins the request to the job's model. Unix sockets are filesystem objects, so
they keep working across a network namespace -- that is what makes a
zero-network sandbox compatible with a provider that needs an API.

Measured from inside a real job:

```
direct_internet : BLOCKED:OSError        (1.1.1.1:443 unreachable)
via_broker      : OK 200                 (the one brokered upstream)
upstream saw    : Bearer <real key>      (injected controller-side)
sandbox saw     : broker-placeholder-not-a-credential
```

Without a credential (mock providers, hermetic tests) there is no broker and the
sandbox keeps host networking, since nothing sensitive is present.
