# Install map

**DO NOT INSTALL NOW.**

This table is the *eventual* destination list after Stage 2/3 decisions.
Nothing in this work copies these files into a live location.

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
