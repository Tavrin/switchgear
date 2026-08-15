# OS containment (design only — not deployed)

Production bounded-write requires an OS-level write boundary where
supported. Provider/OpenCode permissions + canaries are defense in
depth, not a complete write boundary.

## Intended `bwrap` map (Linux)

```
worker process
    │
    ▼
mount namespace

worktree         → read/write
state/output fd  → controlled (job dir bind)
system dirs      → read-only
other projects   → inaccessible
HOME             → minimal synthetic HOME
network          → configurable (default off for write)
```

Even if OpenCode permission semantics change, a tool has an escape, or a
model finds an unexpected mutation route, the process must not be able to
write the primary checkout or harness config directories.

## Policy

Profile:

```json
"containment": { "mode": "none"|"bwrap", "required": false }
```

- Stage 0 synthetic tests: `mode=none`, `required=false`
- Stage W production: `mode=bwrap`, `required=true`

If `required` is true and the backend is missing or unusable → **refuse**.
If `mode=bwrap` and `bwrap` is absent → **refuse**.
No silent fallback to `none`.

Stage 0 implements the interface (`lib/containment.sh`) and a refuse-if-missing
test. It does **not** wrap a live provider in `bwrap` and does not install
any containment helper.
