# contract-v1-rc1 — CANDIDATE, not accepted

**Status: owner-reviewed, still NOT accepted. Nothing here is declared,
tagged or published.** This file describes what a first externally consumable
contract candidate would freeze, so the decision to freeze it can be made
against measurement rather than against a summary.

A consumer review of the previous candidate was accepted and returned six
blockers (B1–B6). They are fixed and proved in this revision; the round is
summarised in §11.

The owner ruled on four questions on 2026-08-21, and this document records the
rulings rather than re-opening them:

| # | question | ruling |
|---|---|---|
| 1 | consumer re-pin for normalized event v2 | **approved.** v2 may be used; historical v1 must stay readable and reproducible; the consumer reviews and re-pins independently before rc1 is declared |
| 2 | HEAD-sensitive resume | **approved for LEGACY MIGRATION ONLY.** It must never become the rule for minted lineages — see §6, which now states and tests each required property |
| 3 | failed durable launch attribution | **approved fail-closed.** Refuse before provider execution; attempt guarded cleanup of the partial job directory, and if cleanup fails report its exact path for deliberate operator removal |
| 4 | `gc`/resume race | **accepted as an rc1 residual.** No broad session-locking subsystem in this freeze wave; the concurrency constraint is documented instead — see §9 |

Candidate commit: see the handoff accompanying this file. Proven on that tree,
from a clean worktree:

- `bash tests/run.sh` — **523 tests, exit 0**. Committed mock provider, no spend.
  The uid-boundary suite ran 9/9 for real on the host, not skipped.
- `bash tests/soak.sh` — 60/60 jobs, peak concurrent 3 against a cap of 3,
  file descriptors 4 → 4.
- `doctor` — 29 pass / 5 warn / **0 fail**.
- The machine-path, doc-link, schema and operator noun gates, the last against
  the real operator-owned list, which is neither printed nor committed. The
  fixture pack is separately checked against that list, because `tests/` is
  outside the noun gate's scan.
- G8 (operator `acceptance=external` ⇒ `awaiting_external_review`, `promote`
  refuses) independently exercised: 5 tests, exit 0.

---

## 1. Versions this candidate freezes

| contract | version | where it is declared |
|---|---|---|
| `result.json` record | `schema_version: 2` for new records; absent/`1` remains readable | `data/schemas/result.schema.json` (v2), `data/schemas/result-v1.schema.json` (historical v1) |
| normalized event stream | **`2`** | `evidence/events.v2.jsonl`, `v: 2` on every line, `artifacts.events_normalized_version`, and `runner.json` for a job with no result yet |
| `logs` digest projection | **`1`** | `digest_v` on every digest line and in the `--json` envelope |
| session store binding | **`1`** | `binding.json` `binding_version`, `data/schemas/session-binding.schema.json` |
| contract fixture pack | **`1`** | `tests/fixtures/adapter-v1-contract-fixtures.v1/MANIFEST.json` |
| task envelope | unchanged | `data/schemas/task-envelope.schema.json`, closed |

`schema_version` moved to **2** because both result schemas are closed and
`session_store_id` breaks a strict reader pinned to the historical v1 shape.
The frozen v1 schema omits that field and accepts `schema_version` absent or 1;
the v2 schema includes it and is pinned to 2. Both remain closed.

## 2. Lifecycle and state semantics

**Persisted** statuses, the complete set from `result.schema.json`:
`ok`, `dirty`, `timeout`, `provider_error`, `awaiting_review`,
`awaiting_external_review`.

**Derived** states, which never appear in `result.json` and are computed from
liveness: `running`, `queued`, `died`, `cancelled`, `unknown`.

`status` is derived from four independently recorded facts — `execution`,
`integrity.outcome`, `change.state`, `acceptance.state` — which are all on the
record and all on the `--json` projection. A caller asking "did the provider
fail?" reads `execution` rather than having to know that `dirty` outranks it.

Absence is never evidence of a benign state. A missing record yields `unknown`,
and `unknown` is protected by `gc`, never collected.

## 3. Exit semantics

| exit | meaning |
|---|---|
| 0 | the job did its work (`ok`, `awaiting_review`, `awaiting_external_review`) |
| 1 | refusal or provider error |
| 2 | `dirty` — worktree integrity changed during the job — **or** an argparse usage error before any job started |
| 124 | the job timed out |

Exit 2 is disambiguated by output: a dirty outcome carries a job record or a
`switchgear: REFUSING — ` line, while argparse prints its own `usage:` message
and creates no job.

> **`exit` on the record is the PROVIDER's exit code, not the CLI's.** They are
> different numbers. A `dirty` job carries `exit: 0` on its record while the CLI
> exits 2 and nothing was promoted. Branch on `status` and the four facts it
> projects; use the process exit code for the table above. The fixture pack
> ships the `dirty` case so a decoder meets this before production does.

## 4. Normalized event vocabulary (v2)

Five events, discriminated by `event`: `status`, `tool`, `text`, `progress`,
`finished`. Every line carries `v`.

On `finished`, transcript status and closing-text presence are now
**orthogonal**:

```
status            completed | needs_input | failed
final_text_state  present | empty | unknown
```

plus `turns`, `costUSD`, `tokens`, `exitSummary`, `sawTerminal`, unchanged.

`completed_empty` is gone from v2. It fused two independent facts, and the name
invited a diff reading that fails silently and in the dangerous direction: a
consumer mapping it onto "changed nothing" refuses to merge work that really
landed. The external consumer met exactly that.

**A live v2 run never emits `unknown`.** `final_text_state` is always
determinable for a run this code produced. `unknown` exists only as the honest
value when a consumer lifts historical v1 evidence into v2 terms, because v1
recorded text presence for the two completed spellings and discarded it for
`needs_input` and `failed`. v2 therefore records a fact v1 threw away rather
than merely renaming one.

`sawTerminal: false` is **never** `completed`. It is not always `failed` either,
and an earlier revision of this document said it was. The terminal status is
decided by a strictly ordered cascade, and the error branch runs BEFORE the
truncation branch:

1. the provider reported an error → `needs_input` if the message is an
   input request, otherwise `failed`;
2. else no terminal provider event was seen → `failed`, with a truncation
   `exitSummary`;
3. else the provider declared an abnormal stop → `failed`;
4. else → `completed`, with `final_text_state` recording whether it closed with
   assistant text.

So `sawTerminal: false` validly accompanies **`needs_input`**: a run that stopped
to ask for permission never closed its stream, and branch 1 claims it before
branch 2 can call it truncated. The shipped `needs_input` fixture is exactly that
case — `status: needs_input`, `final_text_state: empty`, `sawTerminal: false`.

What holds unconditionally is the narrower claim: `sawTerminal: false` is never
`completed`, however much assistant text preceded it. "Claims done, evidence
truncated" is the suspicious case, and over-reporting truncation is the right
default.

**Change presence never comes from the terminal status.** It comes from
`change.state` and `freeze.changed_files`, computed by the controller from the
worktree.

**`finished.status` is a transcript interpretation, not the job outcome.** It
normalizes what the provider's event stream says. `result.execution.outcome` is
authoritative for provider execution, while projected `result.status` is the job
outcome. The committed `provider_error` fixture proves the distinction: its
transcript finishes `completed` with `final_text_state=empty`, but the provider
exits 7 and both result fields say `provider_error`.

## 5. Legacy compatibility

The rail never rewrites or migrates a historical `evidence/events.v1.jsonl`,
and changing projection versions does not modify it. The opt-in retention
operation is the explicit exception: `gc --yes` collects an eligible job's
whole directory, including every normalized artifact inside it, as documented
in §9.

Projections in this rail are RECOMPUTED from the raw provider stream, not read
back from the normalized artifact. A bare version bump would therefore have
relabelled every historical job's recomputed events as v2 while its durable file
said v1. So the vocabulary is resolved per job, in this precedence:

1. the result record's `artifacts.events_normalized_version`;
2. else, for a job with no result, `runner.json`'s `events_normalized_version`,
   stamped before the provider started — so upgrading the installed package
   underneath a running job cannot relabel it;
3. else `1`, for records written before either key existed;
4. else the current version, for a job this binary is starting now.

A record that exists but is unreadable, is not a JSON object, or names a version
this build cannot honour is **refused** with the file and a remedy — never
silently treated as history and never a traceback.

The exact two-way mapping:

| v2 | v1 |
|---|---|
| `(completed, present)` | `completed` |
| `(completed, empty)` | `completed_empty` |
| `(needs_input, present\|empty)` | `needs_input` |
| `(failed, present\|empty)` | `failed` |

| v1 | v2 |
|---|---|
| `completed` | `(completed, present)` |
| `completed_empty` | `(completed, empty)` |
| `needs_input` | `(needs_input, unknown)` |
| `failed` | `(failed, unknown)` |

The v2→v1 direction is exact and is implemented in this rail. The v1→v2
direction is a **consumer** mapping and is deliberately not implemented here:
adding it would create a second place that could invent a `final_text_state` v1
never recorded.

## 6. Session stores

A conversation is a **controller-minted uuid4 lineage**, not an inference from
the filesystem.

Previously a store was keyed `sha256(st_dev:st_ino:realpath)` and its marker
recorded exactly those three fields — the key's own preimage, carrying no
information that could detect anything. Measured on ext4, deleting and
recreating a worktree at the same path reused the inode 4/4, so an orphaned
store was bind-mounted **writable** into the next worker's HOME at the
provider's normal session location.

Now:

- a fresh job mints its own lineage and **adopts nothing**, whatever the
  worktree; creation is exclusive, so even a uuid collision refuses;
- `resume` follows the prior job's recorded `session_store_id`. Continuity is
  authorized by the resume, never by worktree coincidence, and the binding is
  verified — schema, version, harness, and the same `identity_core` the rail
  already trusts for integrity — before anything is mounted;
- any mismatch **fails closed** with a remedy, before credentials, the version
  probe or the provider;
- a pre-lineage store migrates lazily on resume only when its marker matches AND
  the prior job's recorded `integrity.git_identity_after` equals the current
  worktree's git identity. That covers HEAD, so a legacy migration refuses once
  the branch has moved on — deliberately. Migration is a one-time convenience;
  mounting an unrelated conversation is not a trade worth making. An
  unverifiable legacy store is quarantined, never mounted, and the refusal says
  where it went.

### A lineage is NOT HEAD-sensitive

Legacy migration's strictness is scoped to legacy migration. Per the owner's
ruling it must never become the rule for minted lineages, so each required
property is stated here and pinned by a test:

| property | how it holds | proof |
|---|---|---|
| a lineage is identified by its durable store id | `session_store_id`, a controller-minted uuid4, on `runner.json`, `result.json` and the `--json` projection | `test_fresh_result_runner_projection_and_binding_name_one_lineage` |
| `resume(prior_job)` explicitly requests that lineage | `cmd_resume` reads the prior record's `session_store_id` and passes it through; worktree coincidence never selects one | `test_ordinary_head_movement_never_invalidates_a_new_lineage` |
| stable workspace identity constrains where it may resume | the binding stores `identity.identity_core` — `realpath`, `st_dev`, `st_ino`, `git_dir`, `common_git_dir`, `common_dev`, `common_ino` | `test_resume_refuses_repository_and_worktree_admin_slot_reuse` |
| **ordinary commits and HEAD movement do not invalidate it** | `identity_core` excludes `head`, `branch` and `linked_worktree` by construction | `test_ordinary_head_movement_never_invalidates_a_new_lineage` |
| a workspace whose stable binding identity CHANGED fails closed | `sessions.verify_lineage` compares every `identity_core` fact and refuses before anything is mounted; there is no rebind mechanism, and adding one would be an explicit caller-controlled operation with its own evidence | `test_resume_refuses_missing_invalid_and_unknown_binding_records`, `test_resume_refuses_repository_and_worktree_admin_slot_reuse` |

Note the precise scope of the last row, because an earlier revision of this
document overstated it. A destroyed-and-recreated workspace fails closed **when
its stable binding identity changes** — a different repository, a different
worktree admin slot, a different device or inode. It does **not** fail closed
when every one of those facts is reproduced: see the bounded residual in §9.

The HEAD-movement test moves HEAD the way ordinary work does — a branch and a
real commit — then asserts the resume rejoins the **same** lineage and that the
resumed worker still sees the earlier conversation, so it cannot pass by merely
failing to refuse. It was proved non-vacuous by making `verify_lineage`
HEAD-sensitive, which is exactly the regression it guards.

The legacy refusal says which case it applies to. An unscoped "a later commit
refuses" would teach a caller that this rail cannot resume across ordinary work
— false, and the opposite of what the lineage model provides — so a test asserts
the refusal scopes itself to legacy migration.

Consequence a caller should know: stores are per conversation rather than per
worktree, so a state root holds more of them, and OpenCode's store is a whole
data directory including a SQLite database.

## 7. Security facts

Unchanged by this wave, and reported per job on `result.security`, read back off
the sandbox argv that was actually constructed rather than from the request:

- `containment` — backend and which namespaces were really unshared;
- `credential` — whether a credential entered the worker, and the posture;
- `identity` — the uid boundary and the payload uid;
- `network` — whether the sandbox was broker-only or had direct access.

Policies express requirements; the record states realized properties. A caller
states what it needs and tests the outcome.

Also unchanged: never executing a provider outside `bwrap`, mutual exclusion for
bounded-write, evidence persisted before integrity asserts, promotion bound to
reviewer-attested evidence, and the refusal of `promote` under
`acceptance=external`.

One assumption a caller must honour: the state root is single-user by design —
any holder can cancel any background job — so **never mount a state root into a
worker sandbox**.

## 8. Fixtures

`tests/fixtures/adapter-v1-contract-fixtures.v1/`, generated by
`tests/helpers/capture_contract_fixtures.py` from real hermetic runs of the real
code paths. Never hand-authored.

Scenarios: `ok`, `empty_final_text_with_change`, `awaiting_external_review`,
`provider_error`, `legacy_v1` (explicitly derived through the real projection),
`needs_input`, `dirty`, `crashed_launch_only`, plus
`gc_plan.json` and `gc_applied.json`.

A consumer needs none of this repository to use the pack: it is decodable JSON,
so an external project can test its decoder in its own CI without Switchgear, a
sandbox or a provider.

Capturing rather than authoring caught two things re-reading the docs had not:
the two meanings of `exit`, and a published `--json` key list that was wrong by
twelve keys.

## 9. Known residuals

Every remaining item, categorised.

### Fixed in this wave

Nine defects and eleven adversarial findings; see the commit messages, which
record the disposition of each. Two review claims survived a real attack and are
recorded as such: normalized v2 is exactly orthogonal with all six
down-projection pairs correct, and a fresh job cannot adopt an existing
conversation.

### Architectural residual

- **`gc` and active session use can race on a session lineage.** `gc` re-verifies
  the binding, its digest, the bound worktree's absence and the lease at delete
  time, but holds no lock across the delete or across a job's use of the store.
  A currently running read-only or otherwise session-using job can lose its
  lineage when its bound worktree disappears while the job is running and
  destructive `gc --include-sessions --yes` executes: gc sees the path as absent
  and deletes the store underneath the live job. No later reappearance and no
  resume is required.
  Closing it properly needs a lock held across session use, which is a design
  change and was deliberately not made at a freeze. **Accepted by the owner as
  an rc1 residual; recorded, not fixed.**

  The operational constraint that follows, now stated in `docs/INTEGRATION.md`
  beside the `gc` contract: **`gc --include-sessions --yes` is destructive and is
  not concurrent-safe with `resume` or with a running job's session use.** Run
  destructive session collection in an idle or maintenance window. An external
  consumer must not build a caller that treats it as safe to run concurrently
  with dispatch.
- **`gc` treats `ENOENT` as absence.** An unmounted mount point can also present
  as `ENOENT`, so absence is not a perfect signal. Every other errno is reported
  as unverifiable and skipped. The limit is stated rather than papered over.
- **Quarantined session stores are never collected automatically.** A store
  whose binding could not be verified is reported and left for an operator to
  remove deliberately, because deleting it would be the
  unverifiable-means-absent mistake this wave removed.

### Intentionally deferred

- The remaining test-coverage debt from the frozen plan's §5: cancel escalation,
  end-to-end resume, and envelope closure negatives. **The `--json` shape debt is
  paid** — a test now asserts the documented key set equals what `_print_job`
  emits, because that list was found wrong by twelve keys.
- Everything in `ROADMAP.md` §§1–5 and 9–10, plus correlation persisted
  pre-result. The `schema_version` bump guard is now implemented.

### Unsupported / unknown

- A v1 → v2 upgrade of historical evidence is not performed by this rail, by
  design. A consumer that wants v2 terms for a v1 record must use the documented
  mapping and accept `unknown` for `needs_input` and `failed`.
- Whether a re-captured fixture pack stays byte-stable across provider or mock
  changes is not asserted, and deliberately so: volatile values are expected to
  move, and the freshness rule asks for re-capture.

## 10. Consumer do / don't-rely-on list

**Do rely on:**

- `result.json` and the `--json` projection, whose key set is now documented and
  test-asserted, and which are additive-only;
- `status` and the four facts it projects (`execution`, `integrity_outcome`,
  `change`, `acceptance`);
- `change.state` and `freeze.changed_files` for whether anything changed;
- the normalized stream — `evidence/events.v<N>.jsonl` or
  `logs --format normalized` — branching on `v`;
- the CLI's process exit code for the table in §3;
- `artifacts.events_normalized_version` to learn a finished job's vocabulary;
- `security` for what containment a job actually got;
- `session_store_id` to know which conversation a job belongs to;
- the fixture pack, pinned by its directory version.

**Do not rely on:**

- `evidence/events.jsonl` — the provider's raw stdout, forensic evidence whose
  shape is whichever CLI ran. It is explicitly not a contract;
- the record's `exit` as though it were the CLI's exit code;
- `finished.status` for whether files changed, whether the provider process
  succeeded, or the overall job outcome; it describes only the transcript;
- `final_text_state == unknown` appearing from a live run — it will not;
- the digest as a versionless stable shape: branch on `digest_v`, and on an
  unrecognised value fall back to `logs --format normalized` rather than
  decoding it;
- the `--json` digest envelope fitting in `DIGEST_MAX_BYTES`. The cap bounds the
  compact JSONL event payload that plain `--format digest` emits; `--json` wraps
  the same bounded events in an indented envelope and is larger;
- a session store being shared between independent jobs on one worktree — it no
  longer is;
- a destroyed-and-recreated workspace ALWAYS failing closed. It fails closed when
  its stable binding identity changes, which is the ordinary case; it does not
  when every `identity_core` fact is reproduced — same path, same admin slot,
  same device, reused inode. See the bounded residual in §9. A legacy migration
  after the branch has moved on does fail closed. **Ordinary commits and branch
  changes do NOT invalidate a minted lineage** — that strictness is legacy-only;
- `gc --include-sessions --yes` being safe to run concurrently with `resume` or
  a running job's session use. It is not; see §9;
- job ids, timestamps, digests, durations or costs in the fixture pack. They are
  real values from a real run and change on every re-capture.

## 11. Consumer-review round (B1–B6)

The consumer review of the previous candidate was accepted. It returned six
blockers; all six are fixed here, each proved by backing the fix out and
confirming its regression fails.

| # | blocker | disposition |
|---|---|---|
| B1 | an unsupported normalized-event version failed **open** — a job recording `3` recomputed the current v2 vocabulary and emitted it stamped `v: 3` | fixed. This build honours only versions it can actually spell (1 via down-projection, 2 natively) and refuses anything else through the public surface, on both the result- and runner-recorded paths. The v2→v1 down-projection also raised a bare `KeyError` out of a public command; it now names the offending pair |
| B2 | launch-attribution failure bypassed the refusal contract with a raw traceback | fixed. Both existing good behaviours preserved — the provider never spawns, the partial job directory is cleaned — and the failure is now a `Refuse` on the `switchgear: REFUSING — ` surface, with the original error chained so the errno survives |
| B3 | the gc/session race description was too narrow | corrected. Documentation only, per the freeze decision — see below |
| B4 | `finished.status` was not stated to be a transcript fact | fixed. Stated at every publishing site and pinned by the real `provider_error` fixture |
| B5 | bare `provider` meant different things on different surfaces | fixed. `harness`/`pool` frozen as canonical, `provider` documented as a surface-specific alias, explicit `pool` added to the single-job projection, every mapping tested |
| B6 | a closed schema gained a field without moving its version | fixed. Result schema v2 with the historical shape frozen as `result-v1.schema.json`, version-dispatched validation, both closed |

### B3 — the corrected mechanism

The earlier description said a bound worktree had to **reappear and be resumed**
inside the delete window. That understated it. The real mechanism:

> A currently **running** read-only or otherwise session-using job loses its
> lineage if its bound worktree disappears while the job is running and
> destructive `gc --include-sessions --yes` executes. `gc` sees the bound path as
> absent and deletes the store underneath the live job. **No later reappearance
> and no resume is required.**

The operational constraint is unchanged and still accepted: destructive session
collection is not concurrent-safe with `resume` **or** with a running job's
session use, and must run in an idle or maintenance window. No session lock was
added; that remains the deliberately deferred design change.

### B4 — which field is authoritative for what

Three different questions, three different fields. Conflating them is what B4
exists to prevent:

| question | authoritative field |
|---|---|
| what does the provider's transcript show? | normalized `finished.status` (+ `final_text_state`) |
| did the provider process execute successfully? | `result.execution.outcome` |
| what is the job's outcome? | `result.status` (projected from four facts) |
| did anything change in the tree? | `change.state` / `freeze.changed_files` |

The committed `provider_error` fixture proves the divergence is real and legal:
its transcript finishes `completed` with `final_text_state: empty`, while the
provider exits `7` and both result fields say `provider_error`.

### B5 — the frozen vocabulary and its aliases

**Canonical:** `harness` is the agent CLI/executable family; `pool` is the
model-serving provider/pool.

`provider` survives **only** as a compatibility alias, and its historical meaning
is surface-specific:

| surface | `harness` | `pool` | bare `provider` means |
|---|---|---|---|
| `jobs --json` row | agent CLI | model pool | the **pool** |
| single-job `--json` projection | agent CLI | model pool (from `model.provider`) | the **harness** |
| `result.json` | agent CLI | via `model.provider` | the **harness** |

**A consumer must not treat bare `provider` as a canonical cross-surface field.**
Read `harness` and `pool`.

### B6 — result schema versions

`result.json` records are closed on both versions and validation dispatches on
the record's own declared `schema_version`:

- absent or `1` → `data/schemas/result-v1.schema.json`, the historical shape
  taken from commit `f59e233`, which does **not** contain `session_store_id`;
- `2` → `data/schemas/result.schema.json`, which adds `session_store_id`;
- anything else **refuses**, exactly as an unsupported event version does.

`additionalProperties: false` is preserved on both; it was not relaxed anywhere.
A v1 record is rejected by the v2 schema and a v2 record by the v1 schema, and
both directions are fixtured — the pack's `legacy_v1` scenario is the v1 side.

`contract-v1` and the result schema version are **separate axes**. There is no
requirement that the numbers match, and none is implied.

**Audit of every other closed durable schema between `f59e233` and this
candidate:** `session_store_id` on the result record is the only property added
to an existing closed schema. `session-binding.schema.json` is new in this wave
and legitimately starts at `binding_version: 1`. No other closed schema changed.
A guard test now fails if a closed durable shape gains a property without its
version moving, so the next occurrence is caught rather than reviewed for.

### Bounded residual carried forward — forced inode reuse on a recreated workspace

**This residual is NOT confined to legacy migration.** An earlier revision of
this document said it was. That was wrong, and the consumer reproduced it on the
minted-lineage path:

1. a fresh job mints a `session_store_id`;
2. the worktree is removed and recreated at the same path and the same worktree
   admin slot;
3. the inode is reused;
4. an explicit `resume(prior_job)` passes `sessions.verify_lineage`;
5. the same minted lineage resumes, and the worker sees the previous
   conversation.

It is structurally possible because minted-lineage verification compares
`identity.identity_core`, which deliberately excludes `head`, `branch` and
`linked_worktree` under the owner's ruling that ordinary commits must never
invalidate a lineage. When a recreated workspace reproduces every remaining fact
— `realpath`, `st_dev`, `st_ino`, `git_dir`, `common_git_dir`, `common_dev`,
`common_ino` — it is **indistinguishable** to the current verifier from the
workspace the lineage was bound to.

So the accurate statement is: a recreated workspace normally fails closed,
because recreating one normally changes at least one of those facts; but forced
or reused filesystem identity can make a recreated same-path/same-slot workspace
indistinguishable to the current minted-lineage verifier, and then the resume
succeeds.

What still holds, and bounds it:

- a **fresh** job never adopts an existing store, whatever the worktree — this
  path requires an explicit `resume` naming the prior job;
- a workspace that differs in any `identity_core` fact still fails closed;
- legacy migration is separately and more strictly gated on the prior job's
  recorded git identity, which covers HEAD.

**Accepted as a bounded residual and carried forward unchanged.** Closing it
would need either HEAD-sensitivity on minted lineages — which the owner has
ruled out, because it would break resume across ordinary work — or a new
rebind/incarnation mechanism, which is not a change to make at a contract
freeze.
