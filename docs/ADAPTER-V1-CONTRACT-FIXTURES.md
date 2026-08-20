# Adapter contract fixture pack

`tests/fixtures/adapter-v1-contract-fixtures.v1/` holds the durable records a
caller decodes when it writes an adapter against this rail. It is **built**, not
specified: every file in it is what the code actually wrote during a real run,
put through one normalisation pass and nothing else.

Regenerate it with:

```console
$ python3 tests/helpers/capture_contract_fixtures.py
```

That drives the real CLI against the committed mock provider by absolute path.
It is hermetic, costs nothing, and never runs a live provider. The pack is
checked by `tests/test_contract_fixtures.py`, which runs in `tests/run.sh`.

**A consumer does not need Switchgear installed to use this pack.** That is the
point: the fixtures are decodable JSON, so an external project can test its
decoder in its own CI without this tool, this repository, or a sandbox.

## Load-bearing capture rule

**Generate every fixture from a real run; never hand-write a record.**

This is a rule rather than a preference because two of the scenarios below are
combinations a human author would have "corrected" and destroyed:

- `empty_final_text_with_change` carries `finished.status=completed` and
  `final_text_state=empty` **beside** `change.state=frozen` and
  `freeze.changed_files=["tracked.txt"]`. A worker edited a file and said
  nothing on the way out. All four fields are correct simultaneously.
- `needs_input` carries a normalized terminal event saying `needs_input` while
  `result.json` says `provider_error`. Two vocabularies disagreeing
  legitimately: the event parks the work back to the operator, the record says
  the run produced no result.

Capturing also found a live contract ambiguity that no amount of re-reading the
documentation had: `dirty` records carry `exit: 0`. See the `exit` note below.
The captured artifact is the authority and the document is the suspect.

## Scenarios

One directory per scenario. Every completed-job scenario holds `result.json`,
`runner.json`, the versioned `events.v<N>.jsonl`, the `jobs --json` row for it,
and the `logs --json` digest and normalized envelopes. The
`crashed_launch_only` scenario deliberately holds only `runner.json` and its
`jobs --json` row: by definition it produced neither a result nor a completed
normalized projection.

| scenario | what it pins |
|---|---|
| `ok` | clean bounded write that also emitted closing text → `awaiting_review`, `completed` / `present` |
| `empty_final_text_with_change` | successful completion with **no** closing assistant text beside a real frozen change |
| `awaiting_external_review` | operator `acceptance=external`: identical freeze and evidence, exit 0, `promote` refused |
| `provider_error` | the provider exited non-zero after a well-formed handoff |
| `needs_input` | terminal event vocabulary and record vocabulary disagreeing correctly |
| `dirty` | worktree git identity moved **on the host** during a readonly job; CLI exit 2, nothing promoted |
| `crashed_launch_only` | `runner.json` with no `result.json`; the row reads `state=died` and still carries launch attribution |
| `gc_plan.json` | a dry run; every protected entry carries the rule that kept it |
| `gc_applied.json` | the same selection applied — the only shape carrying `launch_artifacts_removed`, which `gc.plan()` cannot produce |

The dry-run and applied `gc` shapes are both captured because they are genuinely
different objects. An earlier statement of this table asked for
`launch_artifacts_removed` in the dry run; it is not there and cannot be, which
is exactly the kind of correction capturing from a real run produces and
hand-authoring does not.

## Two things a decoder must not get wrong

**`exit` on the record is the provider's exit code, not this CLI's.** The
`dirty` fixture carries `exit: 0` — the provider ran fine — while the CLI exits
`2` and nothing was promoted. Branch on `status` and the four facts it projects,
never on the record's `exit`. The fixture exists so a decoder meets this case
before production does.

**Change presence never comes from the terminal status.** It comes from
`change.state` and `freeze.changed_files`. `final_text_state` is a statement
about the transcript and says nothing about the tree.

## Normalization and versioning

- Every absolute prefix is replaced with the single placeholder
  `<ABSOLUTE_PATH>`, preserving the path structure after it — so a decoder still
  sees `…/evidence/events.v2.jsonl`, whose filename carries the event
  vocabulary version.
- No credential path or credential-shaped value is captured. The generator
  refuses to write a pack containing one, and a test re-checks the committed
  pack; `tests/fixtures/` is exempt from the machine-path policy gate, so that
  test is the only thing between a developer's home directory and a published
  fixture.
- Every other value is preserved exactly as the run wrote it. Semantic fields
  are never reconciled by hand.
- The directory is version-stamped. Changing what captured content **means**
  requires a new directory version, never an in-place reinterpretation — a
  consumer pins the pack it decoded.
- `MANIFEST.json` records the commit captured from and the exact contract
  versions frozen: `result` schema version, normalized events version and digest
  version.

**Do not pin volatile values.** Job ids, timestamps, digests, durations and
costs are real values from the capture run and change on every re-capture — and
the freshness rule in `AGENTS.md` actively asks for re-capture. Assert the
contract, never the values.
