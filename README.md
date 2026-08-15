# agent-ops

Generic provider/worker substrate. First adapter: OpenCode.

This is **not** a manager, tracker, or scheduler. A project profile supplies
model allowlists, role maps, and command verbs. The adapter does not know
about any particular product, gate, or lock.

## Status

Stage 0 prototype. Bash is an implementation choice, not an architectural
commitment (see `docs/ARCHITECTURE.md`). Bounded-write is designed and
tested against disposable synthetic git repos. It is **disabled** by default
and must not be installed.

## Do not install

See `docs/INSTALL-MAP.md`. The live OpenCode rail stays untouched.

## CLI

```
ai-opencode [--profile PATH] models
ai-opencode [--profile PATH] scout  <dir> "<prompt>"
ai-opencode [--profile PATH] review <dir> <role> "<prompt>"
ai-opencode [--profile PATH] run    --envelope FILE
ai-opencode [--profile PATH] lease  acquire|release|show --dir <dir> --owner <id>
ai-opencode [--profile PATH] status <job-id>
ai-opencode [--profile PATH] write  <dir> <role> --envelope FILE
```

`write` is refused unless `AI_OPS_WRITE=1` **and** the profile has
`write_enabled: true`. The example profile leaves write off.

## Tests

```bash
tests/run.sh
```

Hermetic. Uses `tests/helpers/mock-opencode`. Never calls a live model.
