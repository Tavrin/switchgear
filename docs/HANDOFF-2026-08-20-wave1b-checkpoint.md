# Handoff — Wave 1B checkpoint (2026-08-20)

**Status:** P5–P8 of `docs/PLAN-2026-08-20-contract-stabilization.md` §2 are
landed, and all six open items carried by
`docs/HANDOFF-2026-08-20-wave1a-checkpoint.md` are dispositioned. That file is
unchanged and stays the record of what was believed at the Wave 1A checkpoint;
this one says what happened to each item.

Wave 1A remains addressable as the tag `wave1a-checkpoint` → commit `555ee96`.
Nothing here rewrites or invalidates it.

Started from `c12455d` (`origin/main`). Three commits:

| commit | what |
|---|---|
| `1159ff7` | P5, P6, P7 and Wave 1A items 1 and 4 — behaviour, plus their tests |
| `f9fe5df` | P8 docs-conformance, Wave 1A items 2, 5 and 6 |
| `5abf986` | the adversarial review's nine findings |

## Proven on this tree

- `bash tests/run.sh` — 449 unittest cases plus the schema, machine-path, noun
  and doc-link gates, exit 0. Committed mock provider, no spend. The uid-boundary
  suite ran 9/9 for real on this host, not skipped. The same command measured on
  a clean worktree at `c12455d` runs 434, so this wave adds 15.
- `bash tests/soak.sh` — 60/60 jobs, peak concurrent 3 against a cap of 3, max
  queue wait 8.1s, file descriptors 4 → 4, doctor 29 pass / 5 warn / 0 fail.
- `SWITCHGEAR_PROJECT_NOUNS=<operator list> bash tests/policy/test_no_project_nouns.sh`
  — passes against the real operator-owned noun list, which is not printed and
  not committed.
- Every load-bearing fix was verified by **backing it out on this tree and
  confirming the test that exists for it fails.** Ten of them, each with the
  assertion message recorded in its commit. A test that also passes against the
  broken version is not a test, and two in this wave were caught that way: the
  first regression harness invoked its selector as part of the file path, so
  python exited 2 for "no such file" and four checks proved nothing until they
  were re-run correctly.

## Disposition of the six Wave 1A open items

| # | item | disposition |
|---|---|---|
| 1 | absent `status` becomes the invented terminal state `"None"` | **Fixed.** `live_state` accepts a persisted status only when it is actually a string, and falls through to liveness otherwise. `health.observe` classifies from explicit sets in both directions and counts an unrecognised or unstated outcome as neither success nor failure. Regression coverage for both halves, and for the case where a statusless record's process is measurably dead. |
| 2 | `completed_empty` is transcript emptiness, not "no files changed" | **Fixed as contract semantics, no behaviour change.** All four publishing sites now state what it means, what it does not mean, and that `change.state` / `freeze.changed_files` are authoritative for change presence. A conformance test generates the legal combination from a real bounded-write run. The enum name is unchanged, by ruling, until the contract-v1-rc1 compatibility review. |
| 3 | `gc --include-sessions` can delete a protected job's session store | **Investigated, not implemented.** `docs/DESIGN-2026-08-20-session-retention.md` records the measurement and the recommendation: do not couple session retention to job protection. Two small corrections found while investigating are surfaced there for a separate decision, not made here. |
| 4 | a test that cannot fail for the reason it exists | **Fixed honestly.** Renamed to `test_real_job_runner_constructs_complete_start_record`, which is what it covers; the unconditional `SWITCHGEAR_NO_UID_BOUNDARY=1` override now names `tests/test_uid_boundary.py` as where the boundary is proven for real, and says why it cannot be conditional. No false coverage preserved. |
| 5 | `lease acquire` output key vs input flag | **Fixed.** The lease section of `docs/INTEGRATION.md` shows the emitted object and states that the value under the JSON key `lease` (`lease=<uuid>` in text form) is what `--token` takes. |
| 6 | contract fixture set for adapter v1 | **Specification pinned, corpus deferred.** `docs/ADAPTER-V1-CONTRACT-FIXTURES.md` holds the scenarios, the normalisation rules and the generate-from-real-runs rule. Only the `completed_empty` conformance case is built. No fixture directories, generator or capture harness — that is contract-v1-rc1 work. |

## What changed in the public contract

Every record and `--json` change is **additive**; no key changed meaning, no enum
gained or lost a value, and `schema_version` did not move.

- **Launch records** (`<state>/launch/<job-id>.json`) gain `launch_state`
  (`intent` / `spawned` / `failed`), `intent_at`, and `launch_error` on a failed
  spawn. A record now exists from before the spawn rather than only after it.
- **`cancel`** refuses, with a remedy, on a launch record that names no process
  it can verify — including the truncated and hand-edited records that used to
  produce a traceback. New refusals for `intent` (undecided: check `status`) and
  `failed` (decided: the recorded reason is repeated).
- **The delegation socket** (`GET /delegate/<job-id>`) reports a child's real
  state — `running`, `queued`, `died`, `cancelled` or `unknown` — where it
  previously reported `running` for anything without a result record. `finished`
  now requires the child's record to state a string status. The bounded
  projection and the child-scoping check are unchanged.
- **`promote`** refuses if the record it is about to write would not validate,
  and leaves the on-disk record untouched when it does.
- **Job state**: a result record that does not state a string status no longer
  reports the invented terminal state `"None"`. Consequences a caller may see:
  `wait` on such a job now refuses as `unknown` instead of exiting 1 with the
  record; `gc` protects it under the existing `unknown` rule instead of treating
  it as a collectable terminal state; `jobs` and `status` report a derived state.
- **`health.observe`** gains `unrecognized` and `last_unrecognized` per model.
  `cancelled` is no longer counted as a provider success — an operator stopping a
  job says nothing about the model — and neither is any unrecognised terminal
  state. `models --json` still projects the pre-existing health keys only.
- **`capabilities --json`** gains a top-level `provenance` object labelling every
  top-level path derived or declared, and its description of exit code 2 now
  names the argparse collision.
- **`gc`** protects launch records declaring a well-formed `intent` or `failed`
  as the crash evidence they are, at plan time and again at delete time.

## What did not change

- `result.schema.json`, and every enum in it. No new status, no `schema_version`
  bump.
- The normalized event stream: same five events, same four terminal statuses,
  same `v`. `completed_empty` keeps its name and its meaning.
- The exit-code values 0 / 1 / 2 / 124, and what each means for a job.
- Freeze, review independence, promotion binding, acceptance authority, and the
  refusal of `promote` under `acceptance=external`.
- The authority boundary: this tool owns worker execution and evidence, and
  never project scheduling, project verification or merge authority. Nothing in
  this wave asserts otherwise.
- No new command, no new flag, no new retention subsystem.

## Adversarial review

One blind, read-only review of `c12455d..HEAD`, briefed to attack nine named
claims. Nine findings, **all accepted, none refuted**; eight were defects
introduced by this branch and one (`cancel` tracebacking on a malformed record)
was pre-existing but had just been claimed fixed by a comment. Three of them were
the absence-is-benign defect this wave exists to remove, rebuilt in a new place —
which is the most useful thing the review said, and the reason the fixes are in
their own commit with their own regression evidence. Two claims the review tried
and failed to falsify are worth recording: no legitimate schema-valid promotion
is newly refused, and the expanded command table matches the parser's 20 commands.

## Residuals

Open, evidence recorded, none blocking:

1. **`lease.WorktreeLock.__enter__` writes a mutated token without
   re-validating it.** Named explicitly in `docs/ARCHITECTURE.md` rather than
   glossed, so the "validated before every write" claim is true as written.
   Correcting the write is a separate decision.
2. **`gc._session_candidates` takes job rows it never reads**, and implements its
   documented skip-the-unverifiable rule with `os.path.exists`, which returns
   `False` for a permission error instead of raising. See
   `docs/DESIGN-2026-08-20-session-retention.md`; surfaced, not fixed.
3. **A session store can be re-adopted by a later worktree at the same path**,
   because the identity key includes the inode and inode reuse was measured 4/4
   on ext4. A question about how a store is identified, not how long it is kept.
4. **The `logs` digest carries no `v`.** Documented now; whether the bounded
   projection should carry the version is a contract-v1-rc1 question.
5. The test-coverage debt recorded in the plan's §5 is unchanged: cancel
   escalation, end-to-end resume, envelope closure negatives, `--json` shape.

## Ready for contract-v1-rc1?

On the evidence: yes for the work this wave owned. The plan's first executable
package is complete, every claim it named has been measured against the code, the
suite and the soak are green, and the six inherited items are each either closed
or carried forward with an explicit decision behind them rather than as hearsay.

Two things a next owner should decide **before** rc1 rather than during it: the
two `_session_candidates` corrections in residual 2 (they touch a documented
invariant, which is why they were not taken here), and whether `completed_empty`
and the digest's missing `v` are compatibility items for the rc1 review. Neither
blocks starting.

Nothing in this wave started rc1, live steering, ACP or UID-boundary research.
