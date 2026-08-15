# Migration from the live OpenCode wrapper

Do not migrate in this scratch pass. The live wrapper remains the
production rail.

## Argv

| Live | Generic |
|---|---|
| `the old OpenCode wrapper models` | `ai-opencode --profile <project>.json models` |
| `the old OpenCode wrapper scout <dir> "…"` | `ai-opencode --profile … scout <dir> "…"` |
| `the old OpenCode wrapper review <dir> <role> "…"` | `ai-opencode --profile … review <dir> <role> "…"` |

A later project alias can be:

```bash
exec ai-opencode --profile "$PROJECT_PROFILE" "$@"
```

## Names

| Live | Generic |
|---|---|
| `OLD_WRAPPER_TIMEOUT` | profile `timeout_env` (example: `AI_OPENCODE_TIMEOUT`) |
| agent `the old read-only agent` | `ai-ops-readonly` |
| `$HOME/.local/state/the old OpenCode wrapper/` | `$STATE/jobs/<job-id>/` |
| `the old read-only wrapper` | `ai-ro` (repo-local until Stage 2) |

Live role names (`semantic`, `wiring`, …) stay in the **project profile**,
not in this adapter.

## Dual-run

1. Keep the live binary as the only installed rail.
2. Point a project profile at this tree and run hermetic + one dry-run
   readonly review **without** installing.
3. After the pre-Stage-2 language decision, install `ai-opencode` **beside**
   the live wrapper.
4. Cut the alias only after a successful dry-run.

Rollback: leave the live binary in place.

## Skill

One canonical source: `agent-ops/skills/opencode-delegation/SKILL.md`.

Do not install a second independently maintained copy into another
harness. If a harness already discovers the canonical skill, do not
duplicate it. Generated/symlinked adapters only if a concrete
compatibility reason appears.

Never overwrite the live project-specific skill before a project profile
exists.
