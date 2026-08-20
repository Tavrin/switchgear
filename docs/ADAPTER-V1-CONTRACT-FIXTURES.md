# Adapter v1 contract fixture specification

This specifies the durable fixture pack a caller will use to implement adapter
v1. It pins the scenarios and capture rules now; it does not create the corpus,
a generator or capture tooling. The full pack is scheduled for the
contract-v1-rc1 phase.

## Load-bearing capture rule

**Generate every fixture from a real run; never hand-write a record.** Closing
text and worktree changes are orthogonal: a valid v2 capture can put
`finished.status=completed` and `final_text_state=empty` beside
`freeze.changed_files=["tracked.txt"]`. Its v1 down-projection spells the same
run as `finished.status=completed_empty`. A human author would be tempted to
"correct" one side of that evidence and destroy the compatibility signal the
fixture exists to preserve.

Only that orthogonality conformance case is built in this wave, as a test that
runs the committed mock through the real bounded-write rail. All fixture
directories and every other scenario below remain contract-v1-rc1 work.

New jobs use normalized-event v2, while this v1 fixture pack and existing v1
artifacts remain readable and are never rewritten. The exact v2 -> v1 mapping is
`(completed,present) -> completed`, `(completed,empty) -> completed_empty`, both
`needs_input` pairs -> `needs_input`, and both `failed` pairs -> `failed`; v1
drops `final_text_state` and carries `v: 1`. The exact consumer-side v1 -> v2
mapping is `completed -> (completed,present)`, `completed_empty ->
(completed,empty)`, `needs_input -> (needs_input,unknown)`, and `failed ->
(failed,unknown)`. A live v2 run never emits `unknown`; it is the honest value
when old v1 evidence did not record text presence. Non-terminal events are
identical apart from `v`.

## Scenario set

One directory per scenario, with these contents:

| scenario | contents |
|---|---|
| `ok` | clean bounded write: `result.json`, `runner.json`, `events.v1.jsonl`, the `jobs --json` row |
| `awaiting_external_review` | same four — exercises the P1 exit code and the P3 `freeze` block |
| `crashed_launch_only` | the P2 case: `runner.json`, no `result.json`, `jobs --worktree` row with `state=died` |
| `dirty`, `provider_error` | one each, so a decoder's closed enums are exercised on the failure side |
| `needs_input`, `completed_empty` | v1 terminal events, exercising the historical enum and the consumer mapping above |
| `gc_plan.json` | a dry run with a protected `awaiting_external_review` job and a protected dead launch-only record, showing `launch_artifacts` and `protected` |
| `gc_applied.json` | the same selection actually applied, which is the only place `launch_artifacts_removed` exists |

The Wave 1A statement of this table asked for `launch_artifacts_removed` in the
dry run. It is not there and cannot be: a dry run returns `gc.plan()`, whose keys
are `jobs`, `protected`, `orphan_launch_records`, `sessions`, `sessions_skipped`
and `bytes`, while `launch_artifacts_removed` is produced by `gc.apply()`. A
decoder needs both shapes, so both are captured — which is exactly the kind of
correction capturing from a real run produces and hand-authoring does not.

## Normalization and versioning

- Replace every absolute path in captured content with the same explicit
  `<ABSOLUTE_PATH>` placeholder. Do not substitute a developer's path or a
  plausible example path.
- Remove provider credential paths from the normalized copy. Credentials and
  captured homes are not fixture material.
- Version-stamp the containing directory so a consumer can pin the complete
  pack it decoded. The first corpus should use a name such as
  `adapter-v1-contract-fixtures.v1`; changing captured contract meaning requires
  a new directory version rather than an in-place reinterpretation.
- Preserve every other value exactly as the real run wrote it. In particular,
  do not reconcile event outcomes with change fields by hand.
