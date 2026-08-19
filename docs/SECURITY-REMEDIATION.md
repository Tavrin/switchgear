# Security remediation — finding disposition

Baseline (immutable): `47e21bdd8e8da7a49116cd7ef748a946a20342e3`

Independent review verdicts (binding):

- Stage-0 architecture: promising prototype, **REJECT as a security boundary**
- generic READONLY live dry-run: disposable environment only
- production READONLY installation: **NO-GO**
- synthetic WRITE: mock/disposable-repo experimentation only
- real-project WRITE: **ABSOLUTE NO-GO**

This document is the disposition matrix. Status values:

- `implemented` — repaired invariant is stronger than the failing design
- `deferred-refuse` — not implemented; live path hard-refuses
- `refuted` — finding does not apply, with evidence

Readiness classes: **A** production READONLY · **B** meaningful bounded-write
security testing · **C** real-project WRITE (always NO-GO this round).

## Shared root causes (not 15 one-liners)

1. **Detection after the fact was treated as a boundary.** Snapshots are
   evidence. They do not prevent mutation.
2. **The security-sensitive control plane lived in Bash** (config, leases,
   process trees, JSON, policy merge, env inheritance).
3. **Authority was split across drifting artifacts** (profile JSON, policy
   JSON, agent Markdown, runtime JSON, env overrides).
4. **The host environment was trusted** (PATH, HOME/XDG, `OPENCODE_*`,
   `GIT_*`, interpreter injection).
5. **Identity was a pathname** (state, worktree, job IDs, model family).
6. **The test harness could fall through to a real provider.**

Architecture response: move the control/security plane to Python; compile
one policy; execute the provider only inside a constructed `bwrap`
filesystem/process boundary; fail closed when that boundary cannot be built.

Bash remaining: a 6-line launcher that `exec`s `/usr/bin/python3` on a
committed `__main__.py`. No security decision stays in the shell wrapper.

---

## Findings

### F01 — Post-hoc snapshot is not READONLY

- **Severity:** critical
- **Mechanism:** provider runs unsandboxed; wrapper hashes the tree afterward
  and may notice damage.
- **Root cause:** RC1 (detection as boundary).
- **Strategy:** for any advertised READONLY job, construct an OS mount
  namespace in which the project, `.git`, siblings, HOME, and host config
  are not writable. Refuse if the backend cannot do that. Snapshots remain
  evidence only.
- **Probes:** tracked/ignored/`.git`/sibling/primary/host-config mutation
  attempts must fail *inside* the sandbox (file remains unchanged, write
  syscall fails or is invisible to the host object).
- **Status:** implemented
- **Blocks:** A, B, C

### F02 — `containment.required` only checks `bwrap` exists

- **Severity:** critical
- **Mechanism:** `command -v bwrap` then continue; no sandbox argv built.
- **Root cause:** RC1 + RC3 (decorative field).
- **Strategy:** compiled policy always requires `bwrap` for live-provider
  execution. Backend is trusted `/usr/bin/bwrap` only. Construction failure
  or missing binary → refuse **before** exec. No `SWITCHGEAR_BWRAP` override.
- **Probes:** missing/untrusted bwrap refuses; constructed argv includes
  `--ro-bind` of the target for READONLY.
- **Status:** implemented
- **Blocks:** A, B, C

### F03 — `OPENCODE_PERMISSION` overrides inline deny

- **Severity:** critical
- **Mechanism:** inherited env var re-enables bash/edit after
  `OPENCODE_CONFIG_CONTENT` deny.
- **Root cause:** RC4.
- **Strategy:** allowlisted child environment. Do not forward any
  `OPENCODE_*` except the rail-set isolation pins. Never set
  `OPENCODE_PERMISSION`. Synthetic HOME/XDG. Config-precedence probe
  against real OpenCode (no model).
- **Probes:** hostile `OPENCODE_PERMISSION` absent in child; real
  `opencode models` under isolation does not see host permission override.
- **Status:** implemented
- **Blocks:** A, B, C

### F04 — Incomplete provider-config isolation

- **Severity:** critical
- **Mechanism:** host `OPENCODE_CONFIG`, `OPENCODE_CONFIG_DIR`, XDG,
  global agents/plugins, Claude-imported instructions still apply.
- **Root cause:** RC4.
- **Strategy:** synthetic HOME/XDG owned by the job; pin
  `OPENCODE_CONFIG` / `OPENCODE_CONFIG_DIR` / `OPENCODE_CONFIG_CONTENT`
  to the compiled policy; set disable flags
  (`DISABLE_PROJECT_CONFIG`, `DISABLE_CLAUDE_CODE*`,
  `DISABLE_EXTERNAL_SKILLS`, `DISABLE_DEFAULT_PLUGINS`); `--pure`;
  do not mount host `~/.config/opencode`.
- **Probes:** hostile HOME/XDG/plugin not visible; child HOME is synthetic.
- **Status:** implemented
- **Blocks:** A, B

### F05 — State path redirectable / symlink into `.git`

- **Severity:** critical
- **Mechanism:** state created/used before target validation; symlink
  components followed into the repo.
- **Root cause:** RC5.
- **Strategy:** require an already-provisioned absolute state root with
  no symlink components and a rail marker file. Validate target identity
  before creating per-job directories. `O_NOFOLLOW` / exclusive create.
  Opaque job IDs. Worker never gets general write to the state store
  (not mounted writable in the sandbox).
- **Probes:** `$STATE/jobs` symlink into `.git` refused; nested
  symlinks refused; pre-existing malicious job dir refused.
- **Status:** implemented
- **Blocks:** A, B, C

### F06 — Predictable job IDs and unsafe identifiers

- **Severity:** high
- **Mechanism:** `timestamp-role-pid` plus unsanitized role/parent used
  as path components.
- **Root cause:** RC5.
- **Strategy:** UUIDv4 job IDs. Role/parent/lease tokens constrained to
  `[A-Za-z0-9._-]` with a max length. Path join never interpolates raw
  user strings as `..` segments.
- **Probes:** parent-job / role traversal refused.
- **Status:** implemented
- **Blocks:** A, B

### F07 — Shallow schema fallback

- **Severity:** high
- **Mechanism:** if `jsonschema` missing, a partial structural check
  accepted objects.
- **Root cause:** RC2.
- **Strategy:** import `jsonschema` at startup or refuse. Every
  trust-boundary object is Draft-07 validated on consume.
- **Probes:** startup without jsonschema fails (simulated); invalid
  persisted lease/result/handoff fail closed.
- **Status:** implemented
- **Blocks:** A, B

### F08 — Hostile host execution environment

- **Severity:** critical
- **Mechanism:** `GIT_DIR`, `LD_PRELOAD`, `PYTHONPATH`, `BASH_ENV`,
  injected PATH, host HOME reach git/python/provider.
- **Root cause:** RC4.
- **Strategy:** allowlisted env for all children (git, bwrap, provider,
  commands). Trusted absolute executables: `/usr/bin/git`,
  `/usr/bin/bwrap`, `/usr/bin/python3`.
- **Probes:** hostile GIT_*/LD_PRELOAD/PYTHONPATH/PATH do not affect
  identity checks or child env.
- **Status:** implemented
- **Blocks:** A, B, C

### F09 — Pathname-only worktree identity

- **Severity:** high
- **Mechanism:** `readlink -f` path compared; inherited Git env can
  redirect; porcelain parsed with whitespace-sensitive tools.
- **Root cause:** RC5.
- **Strategy:** record realpath + `st_dev`/`st_ino` + exact gitdir +
  common gitdir + HEAD. Clean Git env. `-z` porcelain. Revalidate
  before exec and before promotion. Mount boundary is the stronger
  invariant.
- **Probes:** GIT_DIR cannot redirect identity; identity mismatch
  refuses promotion.
- **Status:** implemented
- **Blocks:** A, B, C

### F10 — Lease is acquire-metadata, not a lifetime lock

- **Severity:** critical
- **Mechanism:** two workers can share one acquired lease; flock only
  covered the metadata write.
- **Root cause:** RC2 + RC5.
- **Strategy:** exclusive flock on a lock file **held for the entire
  worker**. Token binds job, lease UUID, worktree identity, mode,
  owner process identity. Second worker fails. Release requires token.
- **Probes:** simultaneous workers; forged token; stale owner; PID
  reuse; unauthorized release; wrong worktree/mode.
- **Status:** implemented
- **Blocks:** B, C

### F11 — Incomplete process-lifetime ownership

- **Severity:** critical
- **Mechanism:** descendant cleanup mainly on timeout; `setsid` children
  survive; result finalized while children can still write.
- **Root cause:** RC2.
- **Strategy:** provider runs under `bwrap --unshare-pid --die-with-parent
  --new-session`. Result finalization waits for the sandbox process to
  exit (namespace teardown). If pid-namespace flags cannot be applied,
  refuse live-provider execution.
- **Probes:** child on normal exit; grandchild; detached session;
  timeout; delayed write after apparent exit (must not land on host
  protected paths).
- **Status:** implemented (prevention via pid ns + die-with-parent;
  we do **not** claim detection of a hypothetical kernel-level escape)
- **Blocks:** A, B, C

### F12 — Handoff synthesized from arbitrary text

- **Severity:** high
- **Mechanism:** last text/JSON fragment wrapped into `{summary, status}`.
- **Root cause:** RC2.
- **Strategy:** JSONL events only. Exactly one terminal `complete` or
  `error`. Write jobs require one schema-valid `handoff` object on that
  terminal event. Trailing garbage, duplicates, plain text, missing
  handoff → `provider_error`. Bounded output size.
- **Probes:** malformed, truncated, prefix+garbage, plain text,
  duplicates, missing/wrong handoff, oversized.
- **Status:** implemented
- **Blocks:** B, C (readonly does not require a handoff object)

### F13 — Manufactured review verdict promotes the subject

- **Severity:** critical
- **Mechanism:** wrapper wrote `{"verdict":"attack","findings":[]}` and
  set subject `ok`.
- **Root cause:** RC2 + RC3.
- **Strategy:** delete that semantic. Write ends `awaiting_review` with
  frozen evidence. Reviewer must emit a schema-valid verdict
  (`promote`|`reject`|`needs_changes`). CAS promotion checks frozen
  policy, identity, digests, independence. No merge.
- **Probes:** FAIL/empty/stale/wrong-subject/same-family/forged-family/
  concurrent promotion all refuse.
- **Status:** implemented
- **Blocks:** B, C

### F14 — Model family declared by mutable profile

- **Severity:** high
- **Mechanism:** profile catalog can claim any `model_family`.
- **Root cause:** RC3 + RC5.
- **Strategy:** controller-owned `switchgear/data/models/registry.json`. Profile may
  only name IDs. Family/vendor come from the registry. Subject freezes
  independence policy + model identity at implement time. Review uses
  the subject's freeze, not a later profile.
- **Probes:** forged family in a reviewer profile cannot satisfy
  `different_family`.
- **Status:** implemented
- **Blocks:** B, C

### F15 — Post-write commands not in the authority boundary

- **Severity:** high
- **Mechanism:** commands ran on the host via `ai-cmd` after the agent;
  only metacharacter filters.
- **Root cause:** RC1 + RC4.
- **Strategy:** trusted command registry (absolute executable, arity,
  option schema). Commands run inside the same bwrap. No `sh`, no
  generic interpreter, no git mutation verbs. Then process-set death,
  identity revalidation, final evidence, then terminal record.
- **Probes:** malicious base, shell trampoline, outside path, option
  injection refused.
- **Status:** implemented
- **Blocks:** B, C

### F16 — Decorative security fields unused at runtime

- **Severity:** high
- **Mechanism:** policy/profile fields documented but not consumed.
- **Root cause:** RC3.
- **Strategy:** one compiled-policy object; runtime (sandbox, provider
  config, commands, review, mode) derives from it; digest stored on
  the job. Unused security-looking fields removed from the profile
  schema (`quota_hook` decorative hook removed; catalog family ignored).
- **Probes:** compiled policy digest changes if mode/tools/review
  change; job record carries that digest.
- **Status:** implemented
- **Blocks:** A, B

### F17 — Test harness PATH fallthrough to real provider

- **Severity:** critical
- **Mechanism:** `PATH=helpers:...` and a helper named `opencode`.
- **Root cause:** RC6.
- **Strategy:** hermetic tests pass an absolute committed mock via
  `--provider`. Live binary is refused unless
  `SWITCHGEAR_ALLOW_LIVE_PROVIDER=1` **and** the path equals the pinned
  OpenCode binary. Missing mock fails. No PATH lookup for the provider.
- **Probes:** missing mock fails; live path without allow fails.
- **Status:** implemented
- **Blocks:** A, B (test credibility)

### F18 — No real OS filesystem boundary

- **Severity:** critical
- **Mechanism:** OpenCode `external_directory` deny + canaries only.
- **Root cause:** RC1.
- **Strategy:** same as F01/F02 — `bwrap` mount namespace.
- **Probes:** see F01; write mode: own worktree writable, primary/
  sibling/common-git/state not.
- **Status:** implemented
- **Blocks:** A, B, C

### F19 — Review not bound to frozen subject evidence

- **Severity:** high
- **Mechanism:** review attached by `parent_job` string only.
- **Root cause:** RC5.
- **Strategy:** subject record freezes HEAD, tree digest, profile
  digest, policy digest, independence, worktree identity. Promotion
  re-reads and compares.
- **Probes:** stale diff / changed tree cannot promote.
- **Status:** implemented
- **Blocks:** B, C

### F20 — Multiple drifting policy representations

- **Severity:** medium
- **Mechanism:** `policies/*.json` + agent frontmatter + runtime JSON
  + profile could disagree.
- **Root cause:** RC3.
- **Strategy:** compile once from profile+mode+registries; generate the
  OpenCode runtime JSON from that object only.
- **Probes:** unit test that compiled tools.bash=deny ⇒ runtime JSON
  bash deny and sandbox does not grant extra writes.
- **Status:** implemented; the static `policies/*.json` and
  `adapters/opencode/*` copies were deleted 2026-08-18. They had survived the
  fix unreferenced and had already drifted weaker than the generated config —
  the drift this finding predicted, arriving on schedule.
- **Blocks:** A, B

---

## Readiness (this round — author evaluation, not authorization)

| Gate | Verdict | Why |
|---|---|---|
| A production READONLY | **NO-GO for install.** Mechanical candidate only after independent review. | Isolation + bwrap implemented in this lab; not installed; not authorized. |
| B meaningful WRITE security tests | **lab-only** with mock + disposable repos + real bwrap | Lifetime lock + sandbox + review SM exist; C still forbidden. |
| C real-project WRITE | **NO-GO** | Explicitly out of scope pending fresh independent review and human decision. |

## Residuals (honest)

- Provider **network remains available** (OpenCode needs it). Credentials
  are not forwarded; that is not a network jail.
- We do not claim a kernel exploit / bwrap breakout is impossible.
- User namespaces: bwrap may use them; if the host forbids userns we
  refuse rather than degrade.
- Live OpenCode config-precedence probes do not call a model; they do
  not prove every undocumented future env var.
- Cgroup v2 attach is used when writable; if not, pid-namespace +
  die-with-parent is the required primitive (refuse if bwrap cannot
  unshare pid).
