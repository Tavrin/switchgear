# Plan — public execution contract stabilization (2026-08-20)

**Revision 1 — FROZEN 2026-08-20** after charter §12 cross-review: peer boundary
verdict X-006 (opus-atelier3) found no violations in the cross-repo sections
(§1 dispositions, §3 freeze, §4 sequencing). Changes to those sections now move
only via request-IDs. §2 (the package) awaits the owner's go/no-go.

Planning-round output per the shared Atelier × Switchgear coordination charter
(`00_SHARED_ATELIER_SWITCHGEAR_COORDINATION_CHARTER_2026-08-20.md`). Produced by
opus-switchgear at `b3c4dc4`; evidence from two independent read-only Codex
audit lanes (contract conformance; consumer reliance across a restart), with
every load-bearing finding re-verified at source before it was accepted.

**This is a plan. Nothing here is implemented by this document.** Charter §10:
implementation needs a separate execution decision by the owner.

## 1. What this round established

- The four public contracts (ARCHITECTURE.md §5) are real, but the documents
  describing them diverge from the code in specific, consumer-breaking places.
  Per the repo's own precedence rule the code is right and the docs are bugs —
  the list is §3 below.
- The external consumer (Atelier) has pinned its adapter v1 against `b3c4dc4`
  via the cross-repo handshake. Handshake A delivered and frozen 2026-08-20;
  **Handshake B received the same day** (Tavrin/atelier
  `specs/PROGRAM-2026-08-20.md` §5/§8b, request X-005). Its remaining blockers
  on this repo: `crashed-job-attribution` (P2) and `gc-external-protection`
  (P4) — both already in the first package. All other required items are
  IMPLEMENTED at b3c4dc4.
- Settled cross-repo dispositions (recorded here so no manager has to remember
  them; peer records mirror these in Tavrin/atelier's gap table):
  - **Event spool is Atelier's.** No cursor/incremental normalized stream is
    planned; becomes a joint decision only if recompute cost forces it.
  - **Acceptance authority** stays operator-owned (budget file); the caller
    reads `awaiting_external_review` and branches; it can never select the mode.
  - **Lease nesting** (corrected per peer request X-007, 2026-08-21): the
    strictly-nested rule — Atelier's organizational writer lease outside, this
    repo's mechanical flock-fenced lease inside — is the agreed TARGET shape,
    binding from Atelier's WP-A3-WSLEASE onward. Atelier's organizational
    lease does not exist yet; adapter v1 runs under this repo's flock lease
    only, plus Atelier's isolation-by-construction (one writer per dispatch
    workspace by construction). Nothing changes on this repo's side either
    way. Expiry is the kernel's (flock dies with the holder); no steal
    command needed.
  - **No confinement of the controller by the caller** (the controller is the
    confining layer); no worktree allocation here, ever; callers stay peers.
  - Correlation indexing, live steering, in-sandbox delegation reliance:
    NOT PLANNED unless raised as formal requirements.
  - **Caller-attested external-closure fact: DECLINED by the consumer**
    (X-005): the accept/reject decision is Atelier's authority and lives
    durably in Atelier's state with the job id; mirroring it here would be a
    second source of truth for a decision this tool does not own. The
    forever-`awaiting_external_review` job in this state root is honest — it
    says truthfully that this tool never learned the outcome. CLOSED.
  - Consumer boundary note accepted into P8 scope: THREAT-MODEL.md must state
    the assumption behind item 15 — any state-root holder can cancel any
    background job (single-user root by design), so a caller must never mount
    the state root into a worker sandbox. Atelier records the never-mount rule
    as its own obligation; the assumption still belongs in this repo's threat
    model.

## 2. First executable package (pending owner go)

Each item has a proof artifact: the named behavior plus a test that constructs
the failure and asserts the fix. Ordered: P1–P4 block or de-risk the external
adapter; P5–P8 close claims the audit proved false.

| # | Artifact | Defect (verified at source) | Done when |
|---|---|---|---|
| P1 | `external-acceptance-exit-code` | `exit_code_for("awaiting_external_review")` → 1 (jobstate.py:218-236); untested (test_capabilities.py:84-91 omits it) | maps to 0; capabilities exit-table test enumerates it |
| P2 | `crashed-job-attribution` | jobs with no result.json report null attribution and are silently dropped by `--worktree` (joblist.py:109-140); listing `provider` is the pool, not the harness | recordless jobs appear in `--worktree` output with runner.json-backed harness attribution; test constructs runner.json-without-result and asserts both |
| P3 | `external-freeze-binding` | freeze populated only when `status == "awaiting_review"` (job.py:1021); external records say `change.state=frozen` with `freeze:null`; in-code comment at job.py:884-886 claims otherwise | external write persists the identical freeze block; live-write test asserts it |
| P4 | `gc-external-protection` | gc protects `awaiting_review` but not `awaiting_external_review` (gc.py:117); deletes dead launch-only records unconditionally — the only identity of a crashed launch (gc.py:171) | both protected; tests for each |
| P5 | `background-launch-intent` | launch record written after Popen (cli.py:452-473); launcher crash in the window makes never-started vs vanished indistinguishable | intent durably recorded before spawn; window test |
| P6 | `delegate-child-liveness` | `child_result` returns `running` for any child with no result.json, no liveness check (delegate.py:295-298) — violates the absence-is-not-benign invariant | dead child reports died/unknown via the triple; test kills a child and asserts |
| P7 | `promote-revalidation` | promotion mutates acceptance/status/review/generation and rewrites without schema validation (job.py:1140-1143 path; review.py:164-179 validates other writes) | post-mutation record validated before atomic write; test with an invalid mutation refuses |
| P8 | `docs-conformance` | INTEGRATION.md: "tail artifacts.events_normalized" false for running jobs; event vocabulary omits `progress` and `finished.status=failed` and the `event` discriminator; contract table omits ten commands and real states (`queued`, `unknown`, `awaiting_external_review`, review timeout/dirty); ARCHITECTURE.md: "validated on every write" false at promote; exit-code wording omits argparse's exit 2; capabilities generated-vs-declared unlabeled; THREAT-MODEL.md silent on the single-user state-root assumption (any holder can cancel; callers must not mount it into sandboxes) | each named doc claim matches measured behavior; the vocabulary section lists all five events and four terminal statuses |

Deliberately **not** in the package (needs design, not a fix): correlation
persisted pre-result (C3), a bounded full-answer projection (ROADMAP §6), a
schema_version bump-guard test, nested result-schema closure, and everything in
ROADMAP §§1–5, 9–10.

## 3. Contract freeze

The surface sent to the consumer as Handshake A (2026-08-20) is frozen for
reconciliation: additive-only on record and `--json` keys; the pinned defects
above are the only intended behavior changes; anything else moves only through
a request-ID exchange and a `schema_version` decision.

## 4. Sequencing and blockers

1. Owner go/no-go on the package above (only owner decision required).
2. P1–P4 land first (unblock the external adapter); P5–P8 follow.
3. Handshake B + gap-table half (G1–G31) received 2026-08-20 and reconciled —
   no discrepancies; P1–P4 ordering matches the consumer's blockers verbatim.
   §12 boundary verdict received (X-006): no violations; this plan revision is
   frozen. Remaining: my boundary review of Atelier's cross-repo sections when
   they arrive (post their C-A8 adversarial review).
4. Nothing in this repo waits on Atelier: the package is independently useful.

## 5. Audit-finding dispositions

All lane findings accepted; none refuted. Findings verified directly before
acceptance: A2/A5.1/A5.2 (exit codes, normalized stream timing and vocabulary),
B2.2 (delegate liveness), B8.1 (launch window), B9.2 (freeze null),
B9.3/A7 partially via B9.2's sources. Lane A finding A6 (envelope) confirmed
conformant — no action beyond the negative-coverage note. Test-coverage gaps
named by the lanes (cancel escalation path, end-to-end resume, envelope
closure negatives, `--json` shape) are recorded here as accepted debt, not
scheduled.
