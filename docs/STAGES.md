# Stages

Only Stage 0 is executed in this repository.

| Stage | What | Install? |
|---|---|---|
| 0 | Contracts, hermetic OpenCode adapter, designed write, red-team suite | No |
| 1 | A **project-owned** profile wraps `switchgear`. Live wrapper stays | No change to live binaries |
| 2 | Thin alias / optional install of readonly wrapper **after** the Python-vs-Bash decision | Beside the live wrapper, never replacing it first |
| 3 | Readonly cutover after a live dry-run | One canonical skill location |
| W | Bounded-write: containment suite green **and** `bwrap` (or equivalent) required | Separate authorization |

Stage W gate: production bounded-write requires an OS-level write boundary
where supported. Provider policy alone is insufficient. If the backend is
missing, refuse. No silent insecure fallback.
