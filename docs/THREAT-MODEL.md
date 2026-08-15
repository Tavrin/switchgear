# Threat model

## Actors

- Honest manager launching scouts/reviews/writes
- Confused manager (wrong cwd, two workers, stale lease)
- Model/tool that tries to escape the worktree
- Prompt-injection content in a repo or web page
- Concurrent managers racing a lease

## Assets

- Primary checkout and other worktrees
- `.git` identity (HEAD, remotes, config, worktree list)
- User home, harness config, credentials
- Job results (integrity of review artifacts)

## Controls (Stage 0)

- Deny bash in the provider agent (closes shell-prefix injection)
- Pin runtime via `OPENCODE_CONFIG_CONTENT`; disable project config
- Refuse foreign `OPENCODE_CONFIG_DIR`
- Before/after integrity snapshots (tree + git identity + other worktrees)
- Symlink-target resolution of dirty paths
- Optional canaries (`AI_OPS_CANARIES`)
- Atomic leases (`flock`) with pid **and** starttime **and** boot_id
- Process group TERM/KILL + descendant walk
- Structured `{verb,args[]}` only; no `eval` / `sh -c`
- Handoff/results only under `$STATE/jobs/<job-id>/`
- Write kill switch + `write_enabled: false` on the example profile
- Review independence at profile-required level (job/model/family/provider)
- Never eval provider JSON

## Residual (not fully fail-closed)

Inherited from the live rail and still true here:

- Ignored files may be invisible depending on git status
- Writes far outside `$abs` that also avoid canaries
- A child that `setsid`s away from the recorded process group (Stage W
  `bwrap` is the intended closure)
- Provider permission semantics can change upstream

**Provider permission policy + canaries are not an OS write boundary.**

## Stage W requirement

Production bounded-write requires an OS-level write boundary where
supported (`bwrap` on Linux). If the backend is missing, refuse.
No silent insecure fallback.

## Install-time risk

Installing over the live wrapper, live agent file, or live skill would
change a running manager. This repo must not do that. See `INSTALL-MAP.md`.
