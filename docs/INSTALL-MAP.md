# Install map

**Status: the launcher IS installed.**

`~/.local/bin/switchgear` is a symlink to `switchgear/bin/switchgear`, so the
installed command tracks this working tree — an edit here is live immediately.
That is intentional for a lab tool and worth remembering when debugging.

Installing changes no security property. The code was always reachable by
absolute path; a symlink grants nothing new. It exists because a shell alias is
invisible to callers that spawn with argv arrays rather than through a shell —
atelier adapters, Codex, `execFile`, cron — and "callable by any agent" is the
point of the tool.

## What actually constrains use

Not the install state. These:

- `write_enabled: false` in the project profile
- `SWITCHGEAR_WRITE=1` required for any bounded-write job
- a lease token that must be presented, not read off disk
- `SWITCHGEAR_ALLOW_LIVE_PROVIDER=1` plus a pinned binary for live runs
- **and above all: which directory you point it at**

## Current standing instruction

**Disposable and lab repositories only.** The write lane has a handful of live
end-to-end cycles behind it (2026-08-18).
That is enough to call it working, not enough to call it trusted. Do not point
bounded-write at a real project until the write lane has meaningful mileage and a
cross-vendor review has been demonstrated.

Read-only `scout` and `review` against a disposable clone are the intended use
today.

## Eventual destinations

This table is the destination list after Stage 2/3 decisions.

| Source in this repo | Eventual destination | When |
|---|---|---|
| `switchgear/bin/switchgear` | `~/.local/bin/switchgear` | Stage 2, **beside** the live wrapper, after the language decision |
| `switchgear/python/switchgear/*` | `~/.local/lib/switchgear/` (or next to the binary) | Stage 2; the launcher resolves the package relative to itself |
| `switchgear/skills/opencode-delegation/SKILL.md` | **One** canonical skill location. Prefer leaving it in this repo, or a single user-level path that both harnesses already discover. Do **not** maintain a second copy. Never overwrite the live project-specific skill before a project profile exists. | Stage 3 |
| Project-authored profile (not in this repo) | owned by that project | Stage 1 |

## Never installed by this work

- `adapters/*`, `policies/*` — **deleted** (2026-08-18), completing F20. They
  were static copies of the agent frontmatter, the OpenCode runtime JSON and the
  mode policy; nothing loaded them, and they had already drifted weaker than what
  actually runs: they punched a `/tmp/opencode/**` hole in `external_directory`
  and lacked the `plugin: []` that keeps host plugins out. The agent definition
  and runtime config are now generated per job from the compiled policy
  (`CompiledPolicy.agent_definition` / `to_opencode_runtime`) and written into
  the sandbox's own `$HOME`. Nothing is installed into `~/.config/opencode`, and
  nothing should be: provider-config isolation depends on the host config being
  invisible to the job.
- `bin/ai-cmd`, `bin/ai-ro`, `lib/*.sh` — **deleted** in the N2 remediation.
  They were the pre-Python host-side command path (`os.execvp` from a
  user-supplied profile, no sandbox) and are superseded by
  `python/switchgear/commands.py` + `switchgear/data/commands/registry.json`, which execute inside
  the same bwrap as the provider. Do not reintroduce them.
- any `bwrap` production wrapper
- anything that **replaces** the live OpenCode wrapper, helper, readonly
  agent, or runtime JSON
- a second harness-specific copy of the generic skill
