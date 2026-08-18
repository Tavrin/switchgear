# Friction audit: another project's Codex failure catalogue, checked against this rail

Source: `docs/agents/CODEX_LANE_FAILURE_MODES.md` in the another project repo — 559 lines
written from one continuous ~24-hour session, roughly 40 Codex lanes and 35
merges, plus a 126-entry lessons store. It is a failure catalogue rather than a
retrospective: each entry records what went wrong, the evidence, what was
changed, and whether the change actually holds.

Every entry below was **checked against agent-ops by running it**, not by reading
code and deciding it looked fine. Three of the checks failed, which is the reason
to run them.

## Fixed here

| Their finding | What it cost them | What we did |
|---|---|---|
| **Codex process leak** — every job spawns an app-server which spawns MCP servers; upstream reaps neither | 114 app-servers, **754 processes, 15.4 GB**, swap fully exhausted. Attribution was unsolvable: the worker is a *sibling* of its app-server, so process-tree, age and shared-pipe approaches all failed | Verified immune: `--unshare-pid` makes the sandbox pid 1 of its own namespace, so the kernel reaps the rest. Measured on a real Codex job — 55 host processes before, 55 after. Pinned by a test that watches a deliberately-orphaned child's heartbeat |
| **Unguarded `rm -rf`** — every recursive delete interpolated a variable | Flagged by the same human before it fired: an unset variable turns `rm -rf "$wt/.beads"` into `rm -rf /.beads` | `paths.safe_rmtree` guards all three recursive deletes here. Refuses anything outside a named root, the root itself, empty/relative/traversal paths, system roots, and symlinks resolving elsewhere. 13 execution-verified cases; a test greps for bare `rmtree` |
| **Sandbox limits discovered by hitting them** — `.git` read-only, no GPU | Two lanes stopped dead on the git one before anyone wrote it down; a GPU-less sandbox produced false "wedged GPU" defect reports across four lanes (~6 wasted lane-rounds) | The rail now **injects** the limits into every worker's instructions, derived from the compiled policy: writability for this mode, the literal `index.lock: Read-only file system` error, brokered-only network, no GPU/display, empty per-job HOME, no stdin — and that hitting a limit is the boundary, not a defect. Verified live: asked to `git commit`, Claude Haiku named the limit and stopped |

## Found in agent-ops while running their checks

Neither of these came from their catalogue directly. Both were found because
their catalogue said to go and look.

**`logs` read a job's stream with the wrong adapter.** A completed Claude job
reported `status: failed, turns: 0, "stream truncated … not evidence of
completion"`. The projection resolved the adapter from the caller's ambient
profile, because the record only ever carried `model.provider` (the *pool*), not
the adapter that ran the job. Jobs now stamp it — in `result.json` and in
`runner.json`, since a *running* job has no result yet and that is when `logs` is
used most — and an unidentifiable job is refused rather than guessed at. This is
their own through-line, "instruments lying and operators believing them", and it
lied to me while I implemented their fix for something else.

**Lease contention crashed instead of refusing.** `acquire` closed the lock fd in
its `BlockingIOError` handler and the `finally` closed it again; the resulting
`EBADF` *replaced* the `Refuse`. So the exclusivity guard did work — and reported
itself as a raw traceback, on the one path that only happens under load. Same bug
in `release`. Also an fd-reuse hazard, since the controller runs drain threads. A
structural test now fails on any `close()` inside an `except` whose `finally`
also closes.

## Already covered, verified rather than assumed

- **Two jobs in one worktree** (recurred four times there). The worker's flock is
  held for the whole job, so a second lease on a busy worktree is refused. Tested
  against a job that is actually running, not just against the token.
- **A plain directory that is not a worktree** silently redirecting writes into
  `main` (226 lines landed there). Refused here: `is not a git worktree`.
- **Trusting `pgrep`** (fooled them three times, once matching the operator's own
  shell). This rail never pattern-matches processes — liveness is
  `(pid, start time, boot id)` everywhere. Worth noting that I reproduced their
  bug *three times* while writing the containment test, which is why that test
  uses a heartbeat.
- **Background jobs reaped with the harness** (a 7-minute push vanished mid-run).
  `--background` re-execs into its own session; now pinned by a test asserting
  the job's session differs from the caller's.
- **Restarting a lane to deliver information** (five of seven restarts were
  waste). `resume` continues the same provider session. Their footgun —
  `--resume-last` keeping the sandbox policy from when the thread started —
  does not apply: a resume here is a new job that recompiles policy.

## Not this rail's to fix

Recorded so the boundary is explicit rather than an omission:

- **Stale tickets and stale context in briefs**, spec linting, precheck against
  `main` — orchestration. Belongs to whatever composes the envelope.
- **Shared mutable project data** across lanes, pre-merge gates, private-project
  schema drift, flaky-gate calibration — project- and orchestrator-level.
- **Queueing a message into a *running* job.** They built `queue <lane> "..."`
  because cancel-and-relaunch was the only channel. There is deliberately no live
  channel into a running sandbox here; that gap is open and unresolved between
  this rail and its orchestrator.
