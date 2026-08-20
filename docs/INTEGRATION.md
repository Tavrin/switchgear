# Integrating Switchgear

**Switchgear executes agents. It does not orchestrate them.** It runs one
provider job inside an OS boundary and produces evidence. Scheduling, boards,
queues, merge policy and human review belong to whatever calls it.

Callers are peers, not a hierarchy. An orchestrator, a shell script, CI, a human
at a terminal, and an AI agent delegating to another agent are all the same kind
of client and get the same contract. Switchgear imports nothing from any of them
and depends on none of them; the sections below that name a specific orchestrator
do so because that is where a behaviour was measured, not because it is
privileged.

That includes the recursive case, which is first-class rather than a special
feature: an agent may use Switchgear to obtain another agent, and that agent may
do the same.

## The contract

Call the CLI. Everything is argv-only; nothing reaches a shell.

```
switchgear --json [--profile P] [--state S] [--provider ABS] <command>
```

| Command | Purpose | Terminal states |
|---|---|---|
| `scout <dir> "<prompt>"` | read-only inspection | `ok`, `provider_error`, `timeout`, `dirty` |
| `review <dir> <role> [--envelope F]` | read-only review; attaches + may promote when the envelope names `parent_job` | `ok`, `provider_error` |
| `write <dir> <role> --envelope F --token T` | bounded write in a leased worktree | `awaiting_review` (or `awaiting_external_review` under `acceptance=external`), `provider_error`, `timeout`, `dirty` |
| `run --envelope F [--token T]` | dispatch by envelope `mode`/`role`/`cwd` | as above |
| `promote --subject J --review R` | atomic promotion under the worktree lock | `ok` or refusal |
| `lease acquire\|release\|show --dir D` | worktree lease lifecycle | — |
| `<job cmd> --background` | launch detached; prints `job_id` at once and returns | — |
| `resume <job-id> "<msg>"` | continue that job's provider session with a new message | as for the original mode |
| `cancel <job-id>` | stop a backgrounded job (pid + starttime + boot_id checked) | — |
| `status <job-id>` | **cheap poll**, valid while the job runs: state, elapsed, turns, tool count, last tool, tokens, cost, sessionId (~30 tokens) | — |
| `status <job-id> --full` | the whole persisted record (exists only once finished) | — |
| `logs <job-id> [--format digest\|normalized\|full]` | projections over the stream; **digest is the default and is byte-capped in code**. `normalized` is the same vocabulary uncapped; `full` is the raw provider stream | — |

`--json` prints a stable object: `schema_version, job_id, status, mode, role,
model, dir, exit, error, artifacts{events, events_normalized,
events_normalized_version, stderr, handoff}, freeze, review, provider_calls,
cost_usd, security, correlation`.
Those keys are **additive-only**; new keys may appear, existing ones will not
change meaning. Without `--json` the output is `key=value` lines for humans.

### The orchestrator lane contract

Switchgear's side of the contract an orchestrator adapter needs. It was written
against one real consumer, but nothing in it is specific to that consumer:

- **Capabilities** the lane declares: `canResume: true` (sessionId-based),
  `commitsOwnWork: false` (the git dir is a read-only mount, so the orchestrator's
  finalizer commits), `liveInput: false` (no stdin into the sandbox; replies are
  cold resumes), `liveStream: true` (the events file is tailable),
  `reportsCost: true`.
- **`finished.status`** maps 1:1 onto the orchestrator's outcome states — except
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
  `linux-proc-start:<bootId>:<startTime>` — the orchestrator's own process-fence shape. A
  pid alone is not an identity, and it cannot be re-derived once the process is
  gone.

### Execution profile pinning

`switchgear execution-profile` returns the content pin an orchestrator records
at first spawn and enforces on resume:

```json
{"launcher": "...", "package": "...", "file_count": 27, "launcherDigest": "..."}
```

`launcherDigest` is sha256 over the resolved launcher **plus a deterministic walk
of `python/switchgear/`** — sorted relative paths, per-file digest, digest of the
digest list, `__pycache__` excluded. Pinning by resolved path would be worthless
here: `~/.local/bin/switchgear` is a stable path that symlinks into the working
tree, so it always resolves while its content changes with every edit. And the
launcher alone is an 11-line stub, so digesting only the executable freezes the
one file that never changes. Digesting the digest list rather than the bytes
means a rename moves the pin too.

### `.orchestrator-workspace.json`

The orchestrator writes its workspace identity token at the worktree root during dispatch.
**switchgear does not exclude that filename from its integrity digest, and must
not.** Excluding a name creates a hiding place — precisely the finding that put
untracked and ignored content into the digest to begin with.

No special case is needed. The per-job delta is before-vs-after fingerprints, so
a token written before the job has the same fingerprint after and is never
attributed to the job, while a worker that *modifies* it does appear in the
delta — which is exactly what the orchestrator refuses at merge.

This depends on the orchestrator writing the token **before** invoking switchgear.
Confirmed in their code, not assumed: the token is written at worktree
preparation (`its worktree-preparation step`) strictly before `agent.launch` (`:6279`), on
every path — verify, post-merge and merge-scratch checkouts all write it at
creation. Recorded as a contract on their side, with the undertaking that
switchgear is told first if that ordering ever changes. If it ever lands
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
- **Measured spend.** The pool switchgear actually bills (`opencode-go`) publishes
  no quota at all, so there is nothing to read — but every job's real cost comes
  back in its stream. `cost_usd` is on each record and appended to
  `<state>/spend.jsonl`.

Limits are **operator-owned**, in `~/.config/switchgear/budget.json`
(`SWITCHGEAR_BUDGET_FILE` overrides). They are never profile-declared, for the same
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

### Delegation from inside the sandbox (off by default)

An agent with ordinary host access can already call this CLI recursively — that
is the first-class recursive case above. A worker running *inside* a sandbox
cannot: it has no CLI, no state root and no credential, which is the point.

The wrong fix is to mount them in. Instead the capability stays controller-side
and the sandbox gets a socket, exactly as the credential broker does:

```json
{"delegation": {"enabled": true, "roles": ["scout"], "max_children": 2, "max_depth": 1}}
```

With that set, a read-only job's sandbox gains
`/run/switchgear-delegate.sock`, speaking JSON over HTTP:

```
POST /delegate       {"role": "scout", "prompt": "..."}  -> {"job_id": "..."}
GET  /delegate/<id>                                      -> {"state": ..., "answer": ...}
```

**The client is the untrusted worker**, so the protocol is deliberately anaemic.
It names a role from the operator's allowlist and supplies a prompt. It cannot
choose a model, a mode, a directory, a timeout, an effort or a budget — and
unknown fields are *refused*, not ignored, so it cannot probe for one that is
silently accepted. `GET` is scoped to that job's own children; otherwise the
socket would be a read primitive over the whole state root, handed to the process
the state root is hidden from.

**Children are read-only, without exception.** A nested writer needs a second
worktree — leases are exclusive, and two writers on one worktree is what the
lease exists to refuse — and Switchgear does not create worktrees, because that
is orchestration. A worker needing a writing subagent must escalate to you.

Off unless an operator enables it, and when off the socket is **absent** rather
than present-and-refusing: a worker should not be able to tell the feature
exists. Every request, granted or denied, lands on the record under `delegation`
— a worker probing its own boundary is a fact worth keeping.

### Who accepts a change

The same operator-owned file decides which layer holds semantic acceptance:

```json
{"acceptance": "interlock"}
```

- **`interlock`** (default, and what this tool has always done) — Switchgear's own
  review gate decides. A bounded write finishes `awaiting_review` and `promote`
  binds it to reviewer-attested evidence.
- **`external`** — you have assumed that responsibility. The write still freezes
  and still produces identical evidence, but it finishes
  **`awaiting_external_review`** and `promote` refuses.

Two different reviews, and it is worth keeping the names apart:

| | runs on | answers |
|---|---|---|
| **interlock review** (here) | the uncommitted worker delta, before any project verification | did this worker produce something acceptable to hand back? |
| **project review** (yours) | the exact head that passed your tests | should this land? |

They are not redundant — different times, different material — so running both is
defence in depth. Set `external` when your own gate is the one that matters and
you do not want to pay for a second model review.

What Switchgear attests does not change either way: that the worker did what the
record says, inside the boundary. `promote` was never permission to merge.

Not a profile field and not a flag, for the same reason `daily_usd` is not: a
project that can vote itself out of review does not have review, and a worker's
own output can reach a caller's argv.

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

### Finding out what the tool can do, from the tool

```
switchgear --json capabilities
```

Commands and their flags, providers with version/resume/effort/credential-tier,
the limits in force, the refusal contract and the exit table — for a caller that
has never seen this tool and cannot go and read docs mid-task.

Everything in it is **derived from live code**: commands by walking the argparse
parser, providers from the adapter registry crossed with the version pins, effort
from each adapter's own `effort_support()`, limits from the budget file. Nothing
is written down twice, because a hand-maintained capability list is stale the day
after it is written — the model registry had already drifted to 18 hand-listed
ids where the provider served 26.

Tests keep it honest rather than trusting it: the reported provider set must
equal the adapter registry (so adding a fifth provider without wiring it in fails
CI), every command must document itself, the published exit table is asserted
against `jobstate.exit_code_for`, and the published refusal prefix is asserted
against a real refusal's stderr. It answers even with a broken or absent profile,
which is when a caller needs it most.

### Concurrency

Operator-owned, in the same budget file as `daily_usd`, and **absent means
unlimited** — nothing changes for anyone who has not opted in:

```json
{"daily_usd": 5.00, "max_concurrent_jobs": 3}
```

A **foreground** job over the cap refuses immediately: a caller at a terminal
wants to be told, not stalled. A **`--background`** job waits for a slot, bounded
by `SWITCHGEAR_CONCURRENCY_WAIT_S` (default 600) and then refuses — an unbounded wait
turns a full queue into a hang with no diagnosis. Time spent waiting is recorded
as `queued_s` on the job, so queueing shows up as queueing instead of silently
inflating the job's apparent duration.

Slots are counted from markers in `<state>/running/`, each carrying the same
`{pid, starttime, boot_id}` triple as `runner.json`. Counting is therefore
proportional to jobs *currently* running rather than to the state root's whole
history, and a crashed job's stale marker is reclaimed by the same liveness check
used everywhere else — a crash cannot permanently consume a slot.

### Provider health, and where the money went

`models` carries a `health` block per allowlisted model, aggregated from past job
records: recent ok/failed counts, a failure ratio, and `unhealthy` once a model
fails a majority of at least three recent jobs. `doctor` surfaces the same thing
as a **warning**.

**It reports and never gates.** The rail will not refuse a job for an unhealthy
model, and the reason is this tool's audience: an AI agent testing a fix *for*
the failing model would otherwise be refused from testing its own fix. A test
asserts `run_job` never consults health at all.

Broker counters are kept apart in the same way `quota` keeps them apart: `denied`
is policy, `transport` is upstream-unreachable. A network blip must not read as a
model refusing requests.

```
switchgear --state <root> quota --rollup [--today]
```

aggregates measured spend by provider, model and UTC day. One honesty note it
prints: a provider at `$0.00` is labelled **unmetered, not free** — Codex on a
ChatGPT subscription reports no per-step cost at all, so the total is a floor on
what was spent rather than the whole bill. Blending those would invite routing
everything at the "free" provider.

### Retention: `gc`

Nothing is ever removed unless you ask. `gc` is opt-in, reports by default, and
needs a selector — a bare `gc` refuses rather than guessing what "clean up" meant.

```
switchgear --state <root> gc --older-than 7d          # report only
switchgear --state <root> gc --older-than 7d --yes    # actually delete
```

Protected regardless of the selector: anything `awaiting_review` or
`awaiting_external_review`; any job whose process is alive (re-checked at delete
time, since a job can start between the plan and the deletion); any review whose
subject still awaits review; and any job whose **liveness could not be established
at all**. That last one matters — `unknown` is not `dead`, and the rest of the
rail never reads a missing record as a benign state. A dead launch record with no
job directory is also protected: its usable liveness triple is the only surviving
identity of a launch that crashed before creating the directory. Only malformed
launch-only records with no usable triple are swept as litter.

When a job directory is collected, its `launch/<id>.json`, `.out`, and `.err`
files are collected with it. Each existing path appears under the candidate's
`launch_artifacts` key in a dry run before anything is removed.

A cool-down floor (`SWITCHGEAR_GC_MIN_AGE_S`, default 1h) applies **on top of** your
selector, so `--older-than 1s` still does not mean "delete everything".

Session stores need `--include-sessions` **in addition to** `--yes`: a job
directory is reproducible by re-running the job, while a session store is the
only durable copy of a conversation you may still want to `resume`. A store whose
worktree cannot be stat'ed is reported as **skipped**, never deleted —
unverifiable is not absent.

`--compact-ledger` folds `spend.jsonl` history older than today into an
append-only `spend-rollup.jsonl`. Only `assert_within_budget` reads the ledger in
the hot path — via `spent_since(day_start())` — so everything before today is
history: worth keeping, not worth carrying in the file the budget check reads.
Totals are preserved (`quota --rollup` still shows the compacted days, and a
provider that appears only in history does not vanish), the budget reading is
byte-for-byte unchanged, and compacting twice cannot double-count. It removes no
jobs, so it needs no job selector.

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
"roles": { "scout": { "model": "codex/gpt-5.6-sol",
                      "mode": "readonly", "effort": "low" } }
```

It is a cost and behaviour lever exactly like model choice, so it lives where
model choice already lives and is validated against the same kind of allowlist.

**The accepted values are a property of the MODEL, not the provider.** That was
forced by measurement, not chosen for tidiness: `gpt-5.6-codex` accepts `minimal`
and `gpt-5.6-sol` refuses it — one provider, one flag, two different sets. So an
adapter declares only the *mechanism* (does this CLI have an effort control, how
is the value spelled, and where a bad value gets caught), while the *values* live
per model in `switchgear/data/models/registry.json` alongside every measured entry:

```jsonc
"codex/gpt-5.6-sol": {
  "effort_values": ["none", "low", "medium", "high", "xhigh", "max"],
  "effort_source": "model-error: gpt-5.6-sol refused 'minimal' and enumerated these"
}
```

Every measured model records **how** it was measured, so an auditor can weigh the
set rather than just read it. A model with no `effort_values` is *unmeasured*, and
the rail refuses effort for it — identity can be derived from an id by rule, an
accepted-value set cannot.

Where a bad value is caught differs per provider, which `doctor` prints:

| provider | flag | catches a bad value |
|---|---|---|
| Claude Code | `--effort` | client-side |
| Grok | `--reasoning-effort` | client-side, names the set, free |
| Codex | `-c model_reasoning_effort=` | the API, HTTP 400, names the set for that model |
| OpenCode | `--variant` | **nothing** — see below |

OpenCode was measured to **accept `--variant not-a-real-value` and run the job to
completion at full price**, returning a real answer. It neither validates nor
reports. That is the whole argument for refusing an unmeasured value rather than
passing it through: a provider that silently drops an effort it does not
understand hands back a job that ran at the model's default while the record
claims otherwise — a lie in the evidence, bought at full price.

Refusals happen before the job directory exists, so a bad request costs nothing.
The value actually sent is recorded on the job as `effort` (null when none was).

### Checking the install before you depend on it

```
switchgear --state <root> --json doctor
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
switchgear --state <root> --json jobs --state-filter awaiting_review
```

Every row carries the job's **live** state, not merely what it last wrote — a job
whose process is gone but which never persisted a result reads `died`, and one
whose liveness cannot be established at all reads `unknown`. Those are different
facts and the listing keeps them apart; neither is ever rendered as `running`.
A crashed job is still attributed from its start-time `runner.json`, including
in a realpath-based `--worktree` query. Rows name both the `harness` (agent CLI)
and `pool` (model service); the legacy row key `provider` remains the pool.

On a row with **no result record**, that attribution says what the job was
launched to run, not that it ran. The record is written when the job directory
is created, which is before the lease check, so a write refused for a missing
lease also carries it. Deliberately: the same record is the job's liveness
marker, and writing it later would leave a job that died in that window with no
record at all — `unknown` instead of `died`, which is strictly less honest. Read
`state` for what happened; read `harness`/`model` for what it was going to use.
`awaiting_review` is surfaced as its own boolean so a poller does not have to
know how the status is spelled.

The listing is bounded by default (20 rows, newest first, `truncated` and `total`
in the JSON), because an agent should not have to read a whole history to find
one job. `--all` removes the cap. Filters: `--state-filter`, `--since 30m|24h|7d`,
`--worktree <path>`, `--limit N`.

It is deliberately cheap: it reads each job's result record, runner record and
start marker and never opens `evidence/events.jsonl`. Use `logs` when you want
the stream.

### Knowing when a job is done

Three shapes, in order of how much machinery they need:

**1. Foreground — no polling at all.** `scout`, `write`, `review` and `run` block
until the job is finished, print the whole record and return the job's exit code
(`0` ok · `1` refusal/error · `2` dirty · `124` timeout). One call, definitive
answer. This is the right default for an agent driving the tool.

**2. `--background` + `wait` — the same answer, later.**

```
job=$(switchgear --json scout . "..." --background | jq -r .job_id)
# ... launch others, do other work ...
switchgear --json wait "$job"
```

`wait` blocks until the job reaches a terminal state, then prints the **same
record shape** and returns the **same exit code** a foreground run would — so
`--background` + `wait` is indistinguishable from a foreground run except that
you got the id immediately and could launch others meanwhile. No polling loop, no
guessed interval, no monitor to arm.

It is bounded (`--timeout`, default 3600s) because an unbounded wait turns a
stuck job into a hang with no diagnosis. Giving up **does not cancel the job**,
and it does **not** exit `124` — that code means the *job* timed out, which is a
different fact from the waiter giving up on a job that is still running fine.
It exits `1` with `waited_out: true` and the job's current state.

It also answers rather than hanging when there is nothing to wait for: a job
whose process is gone reports `died` immediately, and one with no liveness record
is refused rather than waited on.

**3. `status` — for watching, not for finishing.** A ~30-token poll that is valid
*while* the job runs (state, turns, tool count, last tool, tokens, cost,
sessionId). Use it to show progress, not to detect completion — that is what
`wait` is for.

Whichever you use, "did it actually finish" is a normalized fact rather than an
inference: `finished.sawTerminal` is false when the stream ended without the
provider closing it, and such a run **never** reports `completed`, however much
assistant text it emitted first.

### Observing a running job without flooding your context

`evidence/events.jsonl` is written **as events arrive**, so a job can be watched
while it runs. There is one stream and several projections over it, and the cheap
ones are the defaults on purpose:

- **poll `status`** in a loop. That is the spinner equivalent, roughly 30 tokens
  a call, and it is the intended way for a delegating agent to follow a job.
- **read `logs` (digest)** only when something looks wrong. It is normalized,
  structured, and capped at 8 KiB in code; on truncation it emits a final
  `{"event":"truncated","dropped_events":N}` rather than trimming silently.
- **`logs --json`** wraps the digest in one object with `truncated` and
  `dropped_events` as fields, rather than as a sentinel line the caller has to
  notice. `--format full` under `--json` is **refused**, not wrapped.
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

Every refusal is a single line on stderr beginning `switchgear: REFUSING — `.
Treat any refusal as fail-closed: no work was promoted.

## What the caller must supply

- **A state root**, provisioned once (`state provision DIR`), on a path that does
  **not** overlap the target worktree or its git dir. The rail refuses overlap,
  because the per-job writable HOME lives under the state root.
- **An absolute provider path.** There is no PATH lookup, ever. The live binary
  is refused unless `SWITCHGEAR_ALLOW_LIVE_PROVIDER=1` and the path matches the
  pinned build.
- **A profile** naming allowed models and role→model/mode bindings. Model
  families come from the controller-owned registry, never the profile.
- **A lease token** for bounded-write (`lease acquire` then pass `--token`).

## Evidence

Each job writes `<state>/jobs/<job-id>/`:

- `result.json` — the schema-validated record (the authoritative outcome)
- `evidence/events.jsonl` — the provider's own stdout, byte for byte, capped.
  Forensic evidence; **not** the integration contract
- `evidence/events.v1.jsonl` — the same run in Switchgear's vocabulary. This is
  the one to consume
- `evidence/stderr` — provider stderr, capped
- `evidence/handoff.json` — the worker's structured handoff (write jobs)

`sandbox-home/` is reclaimed after the record is written; set
`SWITCHGEAR_KEEP_SANDBOX_HOME=1` to retain it for forensics.

**Retaining it keeps whatever the sandbox held, including credentials.** For a
fallback-tier provider (Grok today) that home contains a live access token at
`.grok/auth.json`, written there because its CLI validates its session locally.
The output secret scanner does not cover it — it reads the evidence stream and
stderr, not the home. Treat a retained home as sensitive, and delete it when the
investigation is done.

## Writing an adapter

An adapter needs four things, all already available:

1. **launch** — spawn `switchgear --json …`; the process is the job.
2. **stream** — tail `artifacts.events_normalized`. It is newline-delimited JSON
   in *Switchgear's* vocabulary — `status` / `tool` / `text` / `finished` — one
   object per line, each carrying `v` for the contract version. The same shape
   comes out of `logs --format normalized`, which recomputes it and therefore
   works while the job is still running.
3. **stop** — kill the controller process. The sandbox dies with it: verified
   that SIGKILL of the controller leaves zero surviving `bwrap` or provider
   processes (pid namespace + `--die-with-parent`).
4. **result** — read `result.json`, or the `--json` object on stdout.

> **Do not consume `artifacts.events`.** That file is the provider's own stdout,
> byte for byte: it is forensic evidence, and its shape is whichever agent CLI
> happened to run. Parsing it means implementing OpenCode's, Claude's, Codex's
> and Grok's event vocabularies in your code and re-implementing them whenever
> one of those CLIs changes — which is precisely the work the adapter seam exists
> to do once, here. An earlier version of this document told you to do that; it
> was wrong.
>
> `events_normalized` is `null` only when the projection could not be written.
> Fall back to `logs --format normalized`, and do not read the absence as an
> empty run.

Three views, and the difference between them is the one that matters:

| | shape | bounded? | for |
|---|---|---|---|
| `logs --format digest` (default) | normalized | **yes**, hard byte cap | an agent's context |
| `logs --format normalized` | normalized | no | a UI, a tail, a consumer |
| `logs --format full` | **raw provider** | no | a human debugging, forensics |

For **the orchestrator** specifically the mapping is direct: `write` corresponds to a
dispatch into an isolated worktree; `awaiting_review` is the state before the
merge gate; `promote` is a verification step, not a merge — the orchestrator's
human-clicked merge remains the only path to main. switchgear adds an OS boundary
*under* the orchestrator's worktree isolation; it does not replace the merge gate, the
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
~/.config/switchgear/credentials/opencode-go
~/.config/switchgear/credentials/openrouter
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
| `SWITCHGEAR_STATE`, `SWITCHGEAR_PROFILE`, `SWITCHGEAR_PROVIDER` | defaults for the flags |
| `SWITCHGEAR_WRITE=1` | required kill-switch for any bounded-write job |
| `SWITCHGEAR_ALLOW_LIVE_PROVIDER=1` | permit the pinned live provider |
| `SWITCHGEAR_PROVIDER_CREDENTIAL_FILE` | single-credential override; otherwise `~/.config/switchgear/credentials/<provider>`, mode 600 enforced |
| `SWITCHGEAR_PROVIDER_UPSTREAM` | broker upstream base URL |
| `AI_OPENCODE_TIMEOUT` | per-job timeout, bounded by the profile |
| `SWITCHGEAR_MIN_FREE_BYTES`, `SWITCHGEAR_MAX_UNTRACKED_BYTES` | disk/digest bounds |

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
