# Handoff — Wave 1A checkpoint (2026-08-20)

**Status:** P1–P4 of the contract-stabilization plan are landed, owner-accepted,
and independently confirmed by the external consumer. **Nothing below is
implemented.** This file exists so the next implementation agent inherits the
open items as evidence rather than as hearsay.

Wave 1A is the tag `wave1a-checkpoint` → commit `555ee96`, tree
`874c3ac7f3ad0fd529f295a2409e134a5320d8fc`. That is the exact tree the consumer
measured its A2 spike against, and it stays addressable by that tag however far
`main` moves on.

Proven at that SHA, on the authoritative checkout:

- `bash tests/run.sh` — 433 tests, exit 0. Mock provider, no spend.
- `bash tests/soak.sh` — 60/60 jobs, peak concurrent 3 against a cap of 3,
  file descriptors 4 → 4.
- An independent end-to-end verifier — 55/55. Its control value: the same script
  fails 22 of 48 checks at the `1f63be5` baseline, so it detects the four
  defects rather than passing vacuously.

The plan itself is `docs/PLAN-2026-08-20-contract-stabilization.md`. P5–P8 in
its §2 table are **not** started and are a separate owner decision.

---

## Open items

Six, carried deliberately. **Do not resolve these opportunistically.** Each was
verified at source and then explicitly left alone, either because it is
pre-existing and outside the P1–P4 package or because it needs a decision rather
than a patch.

### 1. Absent `status` becomes the invented terminal state `"None"`

`jobstate.live_state` returns `str(rec.get("status"))`, so a result record
missing `status` yields the terminal state `"None"`. Provider health then counts
any unrecognised terminal state as ok.

The health half is the worse one: a job that never recorded an outcome currently
*improves* a provider's success rate, so this does not merely mislabel a record,
it inflates an aggregate someone routes work on. It is also a direct violation of
this repo's oldest rule — absence is not evidence of a benign state.

Verified present at `1f63be5`; pre-existing, not introduced by P1–P4. Raised by
the consumer as a formal non-blocking request. It touches a function every
listing, gc rule and health aggregate reads, so it wants its own change with its
own evidence.

### 2. `completed_empty` is transcript emptiness, not "no files changed"

**The trap most likely to bite a consumer, and the reason this file exists.**

`harnesses/normalization.py` derives it as `elif not last_text.strip():`. So
`completed_empty` means **the provider emitted no closing assistant text**. It is
a statement about the transcript. It says *nothing* about whether the tree
changed, and a worker can edit files and then say nothing on the way out.

A single record can therefore carry, all three correct simultaneously:

```
events.v1.jsonl   finished.status      = "completed_empty"
result.json       change.state         = "frozen"
result.json       freeze.changed_files = ["tracked.txt"]
```

Why it matters: the name invites the diff reading, and the diff reading fails
silently and directionally. A consumer mapping `completed_empty` onto "changed
nothing" refuses to merge work that really landed. A wrong mapping that threw
would be self-correcting; this one quietly drops good work.

Change-emptiness derives from `change` / `freeze.changed_files`, never from
`finished.status`.

None of the four sites publishing this vocabulary says which sense is meant —
`python/switchgear/harnesses/__init__.py`, `docs/INTEGRATION.md`, and
`docs/OBSERVABILITY.md` (twice). No behaviour change is warranted; the fields are
individually correct. This is a docs/vocabulary fix and a natural fit for P8,
which already owns the event-vocabulary section.

### 3. `gc --include-sessions` can delete a protected job's session store

`_session_candidates` receives the job rows and never reads them, so a session
store is collectable on worktree-absence alone even when its job is protected as
`awaiting_review` or `awaiting_external_review`.

Verified present at `1f63be5` and equally true of `awaiting_review`, so not a
regression. Needs a design decision rather than a patch: a session store is the
only durable copy of a conversation and a precondition for `resume`, so coupling
session retention to job protection is a real choice with a cost either way.

### 4. A test that cannot fail for the reason it exists

`CrashedJobAttribution.test_real_job_runner_carries_complete_start_attribution`
passes `SWITCHGEAR_NO_UID_BOUNDARY=1` unconditionally, because the container it
was authored in refused `newuidmap`.

The uid-boundary suite itself runs 9/9 for real on a normal host, so the boundary
IS covered — but that specific test never exercises it anywhere, and reads as
coverage it does not provide. Smallest honest fix is comment-only: say beside the
override that the boundary is covered by `tests/test_uid_boundary.py` and that
this test deliberately targets record construction only.

### 5. `lease acquire` output key vs input flag

`lease acquire --dir D` emits the token under the key `lease`; it is passed back
as `--token`. No document mentions the `lease=` key at all. The `release` refusal
does say "the uuid printed by `lease acquire`", so the remedy exists only in a
path a caller reaches *after* getting it wrong. It cost the consumer a run. One
sentence in the lease section of `docs/INTEGRATION.md`.

### 6. Contract fixture set the consumer will need for adapter v1

Not needed yet — adapter v1 is not being written — and it must not gate a
checkpoint. Recorded so the requirement is not re-derived later.

One directory per scenario, holding the durable records exactly as the code wrote
them, absolute paths normalised to a placeholder, any provider credential path
removed, the directory version-stamped:

| scenario | contents |
|---|---|
| `ok` | clean bounded write: `result.json`, `runner.json`, `events.v1.jsonl`, the `jobs --json` row |
| `awaiting_external_review` | same four — exercises the P1 exit code and the P3 `freeze` block |
| `crashed_launch_only` | the P2 case: `runner.json`, no `result.json`, `jobs --worktree` row with `state=died` |
| `dirty`, `provider_error` | one each, so a decoder's closed enums are exercised on the failure side |
| `needs_input`, `completed_empty` | terminal events, per the finished-status enum |
| `gc_plan.json` | a dry run with a protected `awaiting_external_review` job and a protected dead launch-only record, showing `launch_artifacts`, `protected`, `launch_artifacts_removed` |

**Generate these from real runs. Never hand-write one.** Item 2 above is the
proof: a human authoring `completed_empty` beside `changed_files: ["tracked.txt"]`
would have "corrected" one of them and destroyed the exact signal that exposed
the ambiguity. The captured artifact is the authority and the document is the
suspect.

---

## Two notes for whoever picks this up

**Run the policy gates from a clean worktree, not the tree you are working in.**
The plan commit shipped two dangling doc references and the gate passed locally,
because the file it named was present as an *untracked* file in that checkout.
It failed only in a fresh worktree — which is what CI and every other clone see.

**Attribution is not evidence of execution.** On a row with no result record,
`harness` / `model` / `role` say what the job was *launched* to run. `runner.json`
is written at job-directory creation, before the lease check, so a write refused
for a missing lease still carries attribution while reporting `state=died`. That
ordering is deliberate — the same record is the liveness marker, and writing it
later would leave a job that died in that window reporting `unknown` instead of
`died`. Read `state` for what happened.
