# Install map

**Status: the launcher IS installed.**

`~/.local/bin/ai-opencode` is a symlink to `agent-ops/bin/ai-opencode`, so the
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
- `AI_OPS_WRITE=1` required for any bounded-write job
- a lease token that must be presented, not read off disk
- `AI_OPS_ALLOW_LIVE_PROVIDER=1` plus a pinned binary for live runs
- **and above all: which directory you point it at**

## Current standing instruction

**Disposable and lab repositories only.** The write lane has exactly one live
end-to-end cycle behind it (a three-line change in a two-file toy repo, 2026-08-18).
That is enough to call it working, not enough to call it trusted. Do not point
bounded-write at a real project until the write lane has meaningful mileage and a
cross-vendor review has been demonstrated.

Read-only `scout` and `review` against a disposable clone are the intended use
today.

## Eventual destinations

This table is the destination list after Stage 2/3 decisions.

| Source in this repo | Eventual destination | When |
|---|---|---|
| `agent-ops/bin/ai-opencode` | `~/.local/bin/ai-opencode` | Stage 2, **beside** the live wrapper, after the language decision |
| `agent-ops/python/ai_ops/*` | `~/.local/lib/ai-ops/` (or next to the binary) | Stage 2; the launcher resolves the package relative to itself |
| `agent-ops/adapters/opencode/agents/readonly.md` | `~/.config/opencode/agents/ai-ops-readonly.md` | Stage 2 |
| `agent-ops/adapters/opencode/runtime-readonly.json` | `~/.config/opencode/ai-ops-readonly-runtime.json` | Stage 2 |
| `agent-ops/skills/opencode-delegation/SKILL.md` | **One** canonical skill location. Prefer leaving it in this repo, or a single user-level path that both harnesses already discover. Do **not** maintain a second copy. Never overwrite the live project-specific skill before a project profile exists. | Stage 3 |
| Project-authored profile (not in this repo) | owned by that project | Stage 1 |

## Never installed by this work

- `adapters/opencode/agents/bounded-write.md`
- `adapters/opencode/runtime-bounded-write.json`
- `bin/ai-cmd`, `bin/ai-ro`, `lib/*.sh` — **deleted** in the N2 remediation.
  They were the pre-Python host-side command path (`os.execvp` from a
  user-supplied profile, no sandbox) and are superseded by
  `python/ai_ops/commands.py` + `commands/registry.json`, which execute inside
  the same bwrap as the provider. Do not reintroduce them.
- any `bwrap` production wrapper
- anything that **replaces** the live OpenCode wrapper, helper, readonly
  agent, or runtime JSON
- a second harness-specific copy of the generic skill
