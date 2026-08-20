# Adapter v1 contract fixture specification

This specifies the durable fixture pack a caller will use to implement adapter
v1. It pins the scenarios and capture rules now; it does not create the corpus,
a generator or capture tooling. The full pack is scheduled for the
contract-v1-rc1 phase.

## Load-bearing capture rule

**Generate every fixture from a real run; never hand-write a record.** The
`completed_empty` scenario is why this is a rule rather than a preference: a
valid capture can put `finished.status=completed_empty` beside
`freeze.changed_files=["tracked.txt"]`. A human author would be tempted to
"correct" one side of that pair and destroy the compatibility signal the
fixture exists to preserve.

Only that `completed_empty` conformance case is built in this wave, as a test
that runs the committed mock through the real bounded-write rail. All fixture
directories and every other scenario below remain contract-v1-rc1 work.

## Scenario set

One directory per scenario, with these contents:

| scenario | contents |
|---|---|
| `ok` | clean bounded write: `result.json`, `runner.json`, `events.v1.jsonl`, the `jobs --json` row |
| `awaiting_external_review` | same four — exercises the P1 exit code and the P3 `freeze` block |
| `crashed_launch_only` | the P2 case: `runner.json`, no `result.json`, `jobs --worktree` row with `state=died` |
| `dirty`, `provider_error` | one each, so a decoder's closed enums are exercised on the failure side |
| `needs_input`, `completed_empty` | terminal events, per the finished-status enum |
| `gc_plan.json` | a dry run with a protected `awaiting_external_review` job and a protected dead launch-only record, showing `launch_artifacts`, `protected`, `launch_artifacts_removed` |

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
