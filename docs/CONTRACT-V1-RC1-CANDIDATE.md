# contract-v1-rc1 — CANDIDATE, not accepted

**Status: awaiting owner review. Nothing here is declared, tagged or
published.** This file describes what a first externally consumable contract
candidate would freeze, so the decision to freeze it can be made against
measurement rather than against a summary.

Candidate commit: see the handoff accompanying this file. Proven on that tree,
from a clean worktree:

- `bash tests/run.sh` — 493 tests, exit 0. Committed mock provider, no spend.
  The uid-boundary suite ran 9/9 for real on the host, not skipped.
- `bash tests/soak.sh` — 60/60 jobs, peak concurrent 3 against a cap of 3,
  file descriptors 4 → 4, doctor 29 pass / 5 warn / 0 fail.
- The machine-path, doc-link, schema and operator noun gates, the last against
  the real operator-owned list, which is neither printed nor committed.

---

## 1. Versions this candidate freezes

| contract | version | where it is declared |
|---|---|---|
| `result.json` record | `schema_version: 1` | `data/schemas/result.schema.json` |
| normalized event stream | **`2`** | `evidence/events.v2.jsonl`, `v: 2` on every line, `artifacts.events_normalized_version`, and `runner.json` for a job with no result yet |
| `logs` digest projection | **`1`** | `digest_v` on every digest line and in the `--json` envelope |
| session store binding | **`1`** | `binding.json` `binding_version`, `data/schemas/session-binding.schema.json` |
| contract fixture pack | **`1`** | `tests/fixtures/adapter-v1-contract-fixtures.v1/MANIFEST.json` |
| task envelope | unchanged | `data/schemas/task-envelope.schema.json`, closed |

`schema_version` did **not** move. Every record change in this wave is additive:
`session_store_id` is a new key, and no existing key changed meaning.

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

On `finished`, execution outcome and closing-text presence are now
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

`sawTerminal: false` is **never** `completed`. A run that ended without the
provider closing its stream reports `failed` with a truncation `exitSummary`,
however much assistant text preceded it.

**Change presence never comes from the terminal status.** It comes from
`change.state` and `freeze.changed_files`, computed by the controller from the
worktree.

## 5. Legacy compatibility

**`evidence/events.v1.jsonl` is never rewritten, migrated or deleted.**

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
  worktree's git identity. That covers HEAD, so a legacy resume after even one
  commit refuses — deliberately. Migration is a one-time convenience; mounting
  an unrelated conversation is not a trade worth making. An unverifiable legacy
  store is quarantined, never mounted, and the refusal says where it went.

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
`provider_error`, `needs_input`, `dirty`, `crashed_launch_only`, plus
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

- **`gc` and an active resume can race on a session lineage.** `gc` re-verifies
  the binding, its digest, the bound worktree's absence and the lease at delete
  time, but holds no lock across the delete, and a resumed job's verification is
  not held through mounting. To lose a conversation the worktree must be absent
  at plan AND at the delete-time recheck, then reappear and be resumed in the
  window before `rmtree` — with `--include-sessions --yes` explicitly given.
  Closing it properly needs a lock held across session use, which is a design
  change and was deliberately not made at a freeze. **Recorded, not fixed.**
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
  pre-result and a `schema_version` bump-guard test.

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
- `finished.status` for whether files changed;
- `final_text_state == unknown` appearing from a live run — it will not;
- the digest as a versionless stable shape: branch on `digest_v`, and on an
  unrecognised value fall back to `logs --format normalized` rather than
  decoding it;
- the `--json` digest envelope fitting in `DIGEST_MAX_BYTES`. The cap bounds the
  compact JSONL event payload that plain `--format digest` emits; `--json` wraps
  the same bounded events in an indented envelope and is larger;
- a session store being shared between independent jobs on one worktree — it no
  longer is;
- resuming across a destroyed-and-recreated workspace, or a legacy resume after
  a commit. Both now fail closed;
- job ids, timestamps, digests, durations or costs in the fixture pack. They are
  real values from a real run and change on every re-capture.
