# contract-v1-rc1 — frozen

**Status: frozen and consumer-reviewed.** The first externally consumable
Switchgear execution contract was frozen at an exact commit and tagged, after an
independent consumer review returned a verdict of SAFE TO FREEZE at that same
commit. This file is the current status document for that contract. Read it
before relying on any contract claim found elsewhere in the repository.

## What was frozen, and where

| | |
|---|---|
| tag | `contract-v1-rc1` (annotated) |
| annotated tag object | `2f71dd076e7bbbfe2cbaffbb1ec62a00590b205f` |
| peeled commit (`contract-v1-rc1^{}`) | `ef34954a60236545295c6bf7101066a3c2c47998` |

The tag object and the commit it peels to are the authoritative record. Nothing
in this document, and nothing added to `main` after the freeze, changes what the
contract froze — to read the contract as frozen, check out the peeled commit.

## Frozen component versions

| contract | frozen version | compatibility |
|---|---|---|
| `result.json` record | **2** | historical v1 remains readable; both schemas closed, version-dispatched |
| normalized event stream | **2** | historical v1 remains readable and reproducible; a historical `evidence/events.v1.jsonl` is never rewritten or migrated |
| `logs` digest projection | **1** | — |
| session store binding | **1** | — |
| adapter contract fixture pack | **1** | — |

## The candidate document, and why its header is stale

`docs/CONTRACT-V1-RC1-CANDIDATE.md`, at the frozen commit, is the **pre-freeze
candidate document**. It opens by stating that the contract is a candidate, not
accepted, and that nothing in it is declared, tagged or published. That sentence
was accurate when it was written, and it became historically stale the moment
the exact reviewed commit was subsequently tagged — the tag was created after
that commit, so the commit could not describe its own tagging.

That file is historical evidence and is left exactly as it is. It is not
rewritten, and it is not to be read as claiming something it never said. Its
technical content — the versions, the semantics, the residuals, the consumer
do / don't-rely-on list — is what was frozen and remains the substance of the
contract.

**The immutable `contract-v1-rc1` tag, its annotation, and this document
supersede the candidate document's candidate-status sentence.** Where the two
disagree about acceptance status, this document is correct. Where they concern
anything else, the frozen commit governs.

## Where to read the contract itself

- [docs/CONTRACT-V1-RC1-CANDIDATE.md](CONTRACT-V1-RC1-CANDIDATE.md) — the
  frozen contract package: versions, lifecycle and exit semantics, the v2 event
  vocabulary, legacy compatibility, session stores, security facts, fixtures,
  known residuals, and the consumer do / don't-rely-on list. Read its opening
  status line as the pre-freeze statement it is; read the rest as the contract.
- [docs/INTEGRATION.md](INTEGRATION.md) — the caller contract.
- [docs/ADAPTER-V1-CONTRACT-FIXTURES.md](ADAPTER-V1-CONTRACT-FIXTURES.md) —
  the fixture pack specification.
