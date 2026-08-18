# Tests required before any future install

Gate: `agent-ops/tests/run.sh` exits 0.

The suite is hermetic. `tests/helpers/mock-opencode` is first on `PATH`.
It never calls a live model and never touches live config.

## Must stay green

- Schema fixtures (including rejection of string `commands`)
- Model discovery from the profile + mock catalog
- Fail-closed refusals (role, model, `.git`, timeout 0, foreign config dir,
  state-inside-target, state symlink, profile traversal)
- Readonly happy path and dirty / already-dirty
- Write kill switch; example profile `write_enabled: false`
- Linked-worktree + lease required for write
- Atomic lease race; stale lease with live pid + wrong starttime
- In-tree write → `awaiting_review`; handoff only under `$STATE`
- Review independence (family required vs preferred)
- Process-group timeout reaps grandchildren; `orphans_remaining=0`
- Red-team suite in `tests/write/test_redteam_containment.sh`
- `containment.required=true` + missing backend refuses
- Noun grep on the generic substrate (every scan directory that exists)

## Later live dry-run (not this repo)

1. `opencode models` still matches the project allowlist
2. One readonly scout + one review against a disposable clone
3. Confirm the live wrapper is unchanged
4. Only then consider Stage 2 install of the **readonly** rail
