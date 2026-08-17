# Independent adversarial review — Stage-0 remediation `6d217a6`

**Reviewed commit (immutable):** `6d217a69647212fbee02f3c6e8f30507f58cb379`
**Reviewed baseline (parent):** `47e21bdd8e8da7a49116cd7ef748a946a20342e3`
**Branch (informational):** `rem/stage0-security-remediation`
**Review date:** 2026-08-17
**Reviewer:** independent read-only pass; not the author of the remediation.

This document is the **reviewer's** disposition. It is deliberately separate from
`SECURITY-REMEDIATION.md`, which is the **author's** disposition. Where the two disagree, this
file records the disagreement rather than resolving it in the author's favour.

> **Status: findings N1–N8 were remediated in the commit that follows `6d217a6`.**
> This document is left as the record of `6d217a6` **as reviewed** and is deliberately not
> rewritten — the findings below describe that commit, not the current tree. See the follow-up
> commit for each fix and `tests/test_adversarial.py::test_n1_*` … `test_n8_*` for the
> regression guards. The verdicts in §7 were issued against `6d217a6` and require a fresh
> review pass to be revised.

Companion report (same content, browsable):
<https://claude.ai/code/artifact/91397cbf-fc39-4bdb-b997-b0c20af2cf32>

---

## 0. Method and integrity

The author's report and the previous review's conclusions were treated as **unverified claims**.
The architecture was derived from source first; `SECURITY-REMEDIATION.md` was read only after
that derivation and after the exploit work, so its framing could not shape the findings.

Findings below are **empirical unless explicitly marked "(source)"**. Exploits were reconstructed
and run against disposable fixtures in a session scratchpad — a synthetic primary checkout, a
linked worktree, a sibling worktree, a provisioned `$STATE` root, and purpose-written probe
providers. **No model was invoked and no quota was spent**; `AI_OPS_ALLOW_LIVE_PROVIDER` remained
unset for every job. Live-binary interaction was limited to no-model operations (`--version`,
`run --help`, embedded-string inspection).

Pre-flight and post-flight integrity:

| Check | Result |
|---|---|
| `git rev-parse HEAD` | `6d217a6…` — exact match, before and after |
| `HEAD^` | `47e21bd…` — intended baseline is the direct parent |
| `merge-base --is-ancestor` | baseline is an ancestor of HEAD |
| `git status --porcelain -uall` | empty before and after the review |

No file in the reviewed tree was modified, no finding was applied, and the remediation commit is
unchanged. *(This document is a new untracked file added afterwards at the user's request; it
touches no reviewed file and no commit.)*

---

## 1. Architecture as independently derived

```
bin/ai-opencode  (4 lines, /bin/sh — no security decision)
  └─ exec /usr/bin/python3 -s python/ai_ops/__main__.py
       └─ cli.py        state | models | scout | review | write | run | lease | status | promote
            ├─ profile.py   load + jsonschema-validate the project profile
            ├─ policy.py    compile_policy() → frozen CompiledPolicy + sha256 digest
            ├─ identity.py  inspect_worktree(): host git, realpath + st_dev/st_ino + gitdir + common
            ├─ lease.py     WorkerLock: flock(LOCK_EX|LOCK_NB) held for the whole worker
            ├─ job.py       run_job(): the worker lifecycle
            │    ├─ provider.py  resolve_provider() — explicit absolute path, never PATH
            │    ├─ sandbox.py   build_bwrap_argv()
            │    ├─ process.py   run_sandboxed() — argv[0] must be /usr/bin/bwrap
            │    ├─ events.py    parse_event_stream() / extract_review_verdict()
            │    └─ review.py    promote() — generation CAS
            └─ state.py     StateRoot, atomic_write_json, per-job dirs
```

The claim that the security-sensitive control plane moved from Bash to Python, with Bash reduced
to a launcher, is **accurate**. `bin/ai-opencode` is 4 lines and makes no decision.

Effective bwrap policy (`sandbox.py:29-91`), dumped from the real code path rather than read from
comments:

```
/usr/bin/bwrap --unshare-pid --unshare-uts --unshare-ipc --die-with-parent --new-session
  --proc /proc --dev /dev --tmpfs /tmp
  --ro-bind /usr /usr  --ro-bind /bin /bin  --ro-bind /lib /lib  --ro-bind /lib64 /lib64
  --ro-bind {resolv.conf, ssl, ca-certificates, hosts, nsswitch.conf, passwd, group}
  --ro-bind  <worktree> <worktree>        # readonly mode
  --bind     <worktree> <worktree>        # bounded-write mode
  --ro-bind  <common_git_dir>  …          # always read-only, applied AFTER the worktree bind
  --ro-bind  <git_dir>         …
  --bind     <synth_home> <synth_home>
  --chdir    <worktree>
  -- <provider argv>
```

No `--unshare-net` (network intentionally available), no `--unshare-user`, no `--clearenv` (the
environment is supplied wholesale via `Popen(env=)`, which is equivalent and is what makes the
env allowlist effective).

**Important interpretation note for future reviewers.** Inside this sandbox the filesystem root is
built from scratch, so any path *not* bound simply does not exist. A naive probe that appends to
`/etc/PWN`, `/tmp/PWN` or `$STATE/PWN` from inside the sandbox will report success — those are
writes into the `--tmpfs`/masked parent chains, **not** host writes. Every apparent write in this
review was re-checked on the host before being believed. All of them were shadows.

---

## 2. What was proven to hold

These are genuine properties, verified by execution rather than by reading tests.

**READONLY containment (F01/F18).** From inside the real sandbox, write attempts against the
worktree, its `.git`, the common git dir, the per-worktree gitdir, the primary checkout and the
primary's ignored files all failed with `EROFS`. The sibling worktree, host `$HOME`,
`~/.bashrc`, `~/.claude`, the agent-ops repo itself and the fake host-home canary did not exist in
the namespace at all (`ENOENT`). Root listing was `[bin, dev, etc, lib, lib64, proc, tmp, usr]` —
no `/home`. Host verification afterwards showed **zero** files created and `git status` clean on
both primary and sibling.

**Bounded-WRITE surface (F18).** Only the leased worktree was writable. Primary tracked and
ignored files, the sibling worktree, the common git dir, the per-worktree gitdir and the real
state store were all unwritable or absent; host verification confirmed primary/sibling contents
and the state marker were untouched, while the authorized worktree correctly showed
`M tracked.txt` / `?? NEWFILE.txt`.

**Process supervision (F11).** A provider that forked a `setsid` grandchild which wrote once
immediately and again after a 20-second delay: the controller returned promptly, the in-sandbox
write landed, and after waiting out the delay the **second write never appeared**. The pid
namespace tears down with bwrap and takes detached descendants with it. A job cannot reach a
terminal state while a process with job authority survives. This is prevention, not detection.

**Lease mutual exclusion (part of F10).** With a worker holding the lock, a second concurrent
worker on the same worktree was refused: `REFUSING — another worker holds this worktree lease`.
The flock genuinely spans the worker lifetime.

**Provider selection and test credibility (F17).** With a hostile `opencode` planted first on
`PATH` and no provider configured, the rail refused —
`provider path required (--provider or AI_OPS_PROVIDER); no PATH lookup` — and the hostile binary
never ran. A missing/broken mock hard-fails rather than silently falling through. Live OpenCode is
refused unless both `AI_OPS_ALLOW_LIVE_PROVIDER=1` and the path equals the pinned binary.

**Provider output discipline (F12).** Exit-0-with-malformed output produced
`provider_error: malformed provider JSON at offset 0`. Truncated, plain-text, duplicate-terminal,
prefix-then-garbage, and missing/wrong handoff cases are all rejected.

**Parts of the review state machine (F13/F19).** Verdict `reject` refuses
(`verdict reject cannot promote`); an empty review with no verdict refuses; and a subject whose
tree changed after the freeze refuses (`worktree changed after review`). These three work.

**Environment and identity hardening (F03/F08/F09).** The child environment is constructed from an
allowlist; `GIT_*`, `LD_*`, `PYTHON*`, `BASH_*`, `XDG_*` and inherited `OPENCODE_*` do not reach
the provider, and `assert_no_host_secrets` refuses `OPENCODE_PERMISSION` outright. Identity is
`realpath + st_dev/st_ino + gitdir + common gitdir + HEAD`, re-validated after the run.

**Provider-isolation env names are real.** All the knobs the design relies on
(`OPENCODE_PERMISSION`, `OPENCODE_CONFIG{,_DIR,_CONTENT}`, `OPENCODE_DISABLE_PROJECT_CONFIG`,
`OPENCODE_DISABLE_CLAUDE_CODE{,_PROMPT,_SKILLS}`, `OPENCODE_DISABLE_EXTERNAL_SKILLS`,
`OPENCODE_DISABLE_DEFAULT_PLUGINS`, `OPENCODE_PURE`, `OPENCODE_FAKE_VCS`) are present in the
installed 1.18.18 binary, and `opencode run` really does accept `--pure --model --agent --format
--title --dir`. *(An early exact-line `strings` grep suggested otherwise; a substring search
corrected it. Recorded here so the correction is durable.)*

---

## 3. Material findings

### N1 — Promotion does not verify the reviewer examined the subject — **CRITICAL**

*Exploitable defect. Blocks Gate B and Gate C.*

**Where:** `job.attach_review` (`python/ai_ops/job.py:239-278`), `review.promote`
(`python/ai_ops/review.py:29-61`), `cli.cmd_promote` (`python/ai_ops/cli.py:188-203`).

**Mechanism.** `attach_review` constructs the review artifact's provenance fields by copying them
out of the **subject's own freeze block**:

```python
"subject_head":           freeze.get("head"),
"subject_tree_digest":    freeze.get("tree_digest"),
"subject_policy_digest":  freeze.get("policy_digest"),
```

`review.promote` then validates by comparing those same fields back against the same freeze:

```python
if review_artifact["subject_head"]        != freeze.get("head"):        raise Refuse(...)
if review_artifact["subject_tree_digest"] != freeze.get("tree_digest"): raise Refuse(...)
if review_artifact["subject_policy_digest"]!= freeze.get("policy_digest"):raise Refuse(...)
```

All three compare the freeze to itself. They are tautologies and can never fire. The reviewer job
contributes only `model`, `role` and `job_id`; its own working directory, tree digest and diff are
**never compared to the subject's**. `cmd_promote` accepts any `--review <job-id>` and re-extracts
a verdict from that job's stored events.

The only genuine gates on promotion are: subject status is `awaiting_review`, generation CAS,
verdict is exactly `promote`, independence requirements met, and the subject's *live* tree still
matches its own freeze.

**Reproduction (verified).**

1. Run a bounded-write subject job on `wt-a` that edits `tracked.txt` → status `awaiting_review`,
   freeze recorded.
2. Run a reviewer job against **`sibling-b`** — a different worktree, containing none of the
   subject's changes — emitting `{"type":"complete","review":{"verdict":"promote","findings":[]}}`.
3. `ai-opencode promote --subject <S> --review <R>` → exit 0, subject status flips to `ok`.

The reviewer never saw the change it approved. Observed reviewer `dir` was `…/rig/sibling-b` while
the subject `dir` was `…/rig/wt-a`.

**Do current tests catch it?** No. `test_write_inside_and_review_promote` passes, but it never
asserts any reviewer↔subject binding — it happens to point the reviewer at the same directory.

**Minimum repair.** Make the reviewer attest to what it inspected: put the subject's frozen
tree/diff identity into the reviewer's task envelope, have the reviewer record carry the digest of
the tree it actually reviewed (and its worktree identity), and have `promote` compare
**reviewer-attested digest → subject freeze** rather than freeze → freeze. Additionally require the
reviewer job's `dir` to match the subject's `dir`, its mode to be `readonly`, and its status `ok`.

**Disposition impact.** This is the property F13 and F19 exist to establish. The author marks both
`implemented`; at the property level the reviewer-binding half is not.

---

### N2 — Legacy un-sandboxed entrypoints still shipped and install-mapped — **HIGH**

*Residual / packaging defect. Blocks Gate A hygiene.*

**Where:** `bin/ai-cmd`, `bin/ai-ro`, the superseded `lib/*.sh`; `docs/INSTALL-MAP.md:22` still
lists `bin/ai-cmd`, and lines 10-12 map `bin/ai-ro` and `lib/*` into `~/.local/`.

**Mechanism (source).** The remediation moved command execution into `commands.py` — absolute
argv0 from a controller-owned `commands/registry.json`, forbidden shell/interpreter bases, arity
bounds, option-injection and path-bearing-argument refusal, executed **inside the same bwrap**.
That is a real improvement. But `bin/ai-cmd` survives untouched and bypasses all of it:

- it sources the old `lib/common.sh`, `lib/validate.sh`, `lib/policy.sh`;
- it reads the command `argv` from a **user-supplied `--profile`** rather than the controller
  registry;
- it applies only a metacharacter/regex filter;
- it ends in `os.execvp(argv[0], argv)` — **PATH-resolved**, with no absoluteness check, on the
  **host**, with no bwrap at all.

No Python control-plane code references it (`grep -rn "ai-cmd" python/` → nothing), so it is
orphaned rather than wired in. That makes it dead weight with live authority: anyone or anything
invoking the installed `ai-cmd` gets host-side execution entirely outside the new boundary.

**Do current tests catch it?** No test exercises or forbids these binaries.

**Minimum repair.** Delete `bin/ai-cmd`, `bin/ai-ro` and the superseded `lib/*.sh`, and remove
their rows from `INSTALL-MAP.md`; or route them through the same bwrap + registry path. Until then
F15 cannot be called closed, because the attack surface it describes is still physically present
and still install-mapped.

---

### N3 — Host-side git runs on the worker-writable tree; RCE blocked only by check ordering — **MEDIUM**

*Architectural fragility. Blocks Gate B/C; near-zero for READONLY.*

**Where:** `identity.tree_digest` and `identity.git_identity_digest`
(`python/ai_ops/identity.py:119-149`), called from `job.run_job` at `job.py:110-111`, `118-119`
and `169-170` — all **outside** bwrap, on the host.

**Mechanism.** In bounded-write the worktree is bound read-write, and for a linked worktree the
`.git` **pointer file lives inside that writable worktree**. It is covered by neither the
`--ro-bind` of the common git dir nor that of the per-worktree gitdir. A worker can therefore
rewrite `.git` to `gitdir: <path it controls>`, pointing at a repository whose config sets
`core.fsmonitor` or `diff.<driver>.textconv` to an arbitrary command. The controller subsequently
runs host `git status --porcelain` and `git diff HEAD` over that pointer.

`git_clean_env()` sets `GIT_CONFIG_NOSYSTEM=1` and `HOME=/nonexistent`, which correctly kills
system and global config — but the **repository-local** config still applies, and that is exactly
what the worker now controls.

**Verified, in two parts.**
- *The write lands:* a bounded-write probe appended to `wt-a/.git`; the host file was modified
  (`gitdir: …/worktrees/wt-a\nPWNED\n`), breaking the worktree.
- *The primitive is real:* a worker-built repo with `core.fsmonitor` pointing at a marker script,
  invoked as `git -C <repo> --no-optional-locks status --porcelain=v1`, **fired the hook** and
  wrote the marker.

**Why it did not become RCE.** `job.py` calls `inspect_worktree` (rev-parse only — no tree scan,
so no fsmonitor/textconv) at line 115 and `same_core` at 116, which detect the changed gitdir and
`Refuse` **before** `tree_digest` at line 118 does the tree-scanning work that would fire the hook.
The observed failure was `REFUSING — worktree identity changed during job`, and the marker was
absent. Containment here is a **consequence of statement order**, not of a stated invariant. Any
reordering, or any tree-scanning git command added to `inspect_worktree`, converts this into host
command execution.

Note the failure mode is also not clean: the job aborted with a bare `Refuse` out of `_execute()`,
leaving no `result.json` for that job.

For READONLY this is near-zero risk, because the worktree (and hence `.git`) is read-only and
cannot be repointed.

**Minimum repair.** Run identity/tree digests inside the sandbox, or pin `--git-dir` explicitly and
refuse to follow a worker-written `.git` pointer; assert gitdir invariance before any command that
scans the tree. Make the ordering an explicit documented invariant with a test, not an accident.

---

### N4 — Bounded-write provider exiting non-zero still reaches `awaiting_review` — **MEDIUM**

*Correctness defect. Blocks Gate B.*

**Where:** `job.run_job`, `python/ai_ops/job.py:145`:

```python
if status == "ok" and result.returncode not in (0, None) and mode == "readonly":
```

The non-zero-exit → `provider_error` guard is gated on `mode == "readonly"`, so it never applies to
bounded-write.

**Reproduction (verified).** A provider that emits a schema-valid handoff and then `exit(7)`
produced a job record with `exit: 7` and `status: awaiting_review`. A crashed or half-finished
write becomes a promotable candidate.

**Minimum repair.** Treat non-zero provider exit as `provider_error` in bounded-write as well; the
presence of a well-formed handoff object is a claim by the provider, not evidence of success.

---

### N5 — Reviewer model family resolved from a mutable registry at review time — **MEDIUM**

*Architectural weakness. Blocks Gate B/C.*

**Where:** `review.independence` (`python/ai_ops/review.py:10-18`) with
`registry.model_record` (`python/ai_ops/registry.py:22-36`).

**Mechanism (source).** Moving `model_family` out of the mutable profile into a controller-owned
`models/registry.json` (F14) is a genuine improvement, and the subject correctly freezes its own
model record. But the **reviewer's** record resolves its family from the registry file read fresh
at reviewer-job time, and the registry is re-read on every call. An edit to `models/registry.json`
between subject creation and review can relabel the reviewer's family so a `required`
`different_family` is satisfied while both models actually share a family.

This requires write access to the controller's own files — the same trust tier as the registry —
so it is materially weaker than N1, but it means "independence" is not frozen end-to-end the way
the F14 write-up implies ("Review uses the subject's freeze, not a later profile" is true for the
subject side only).

**Minimum repair.** Freeze the resolved reviewer-family expectation, or a digest of the registry,
into the subject at implement time and compare against that.

---

### N6 — Lease is exclusion-only; the forged-token check is inert in the default path — **LOW**

*Defense-in-depth gap.*

**Where:** `job.run_job` (`python/ai_ops/job.py:67-70`) and `lease.WorkerLock.__enter__`
(`python/ai_ops/lease.py:200-208`).

```python
tok = lease.load_token(root, ident)
token_uuid = lease_token or tok["lease_uuid"]     # ← falls back to whatever is on disk
```

When no `--token` is presented, the controller reads the on-disk token and hands it to
`WorkerLock`, which then checks it against the same on-disk value — a self-comparison. `cmd_run`
has no `--token` flag at all, so the `run --envelope` path always self-authorizes.

**Verified.** `ai-opencode run --envelope …` in bounded-write succeeded with **no token
presented** (exit 0). Passing a deliberately wrong token *does* refuse
(`REFUSING — forged lease token`), so the check works only when a caller volunteers a wrong value.

Mutual exclusion — the property that actually matters for concurrent corruption — is sound and was
verified separately. But possession of a lease token is not an authorization factor, which is
weaker than F10's description ("Token binds job, lease UUID, worktree identity, mode, owner process
identity"). Acceptable under a strict single-controller trust model; it should be stated as such
rather than implied to be an authorization capability.

---

### N7 / N8 — Robustness and hygiene — **LOW / NOTE**

- **N7 (LOW, verified).** `job._timeout` (`job.py:24-25`): a non-numeric `AI_OPENCODE_TIMEOUT`
  (e.g. `abc`) is silently ignored and the policy default used. Given the surrounding code refuses
  out-of-range values explicitly ("0 is not a disable"), silently accepting garbage is inconsistent;
  it should refuse.
- **N8 (NOTE).** Verified: `paths._symlink_in_path` (`paths.py:28-49`) `break`s at the first
  non-existent component, so a path whose missing segment precedes a later symlink is reported
  clean. Source-only: dead policy representations (`env._DROP_PREFIXES` / `_KEEP` are unused
  alongside the live `allowlisted_env` allowlist), dead code (`policy.model_for_role`'s
  `if …: pass` at `policy.py:42-43`; the no-op deny loop at `policy.py:103-105`),
  `events.parse_event_stream`'s comment claiming newline-delimited JSONL while `raw_decode`
  accepts concatenated JSON, `schema.validate` rebuilding the whole schema store on every call and
  using the deprecated `RefResolver`, and `state.read_json`'s `islink`-then-`open` TOCTOU. None is
  independently exploitable in the current flow.

---

## 4. F01–F20 disposition

Author status is `implemented` for all twenty. Independent verdicts:

| ID | Finding | Independent verdict | Basis |
|---|---|---|---|
| F01 | Post-hoc snapshot is not READONLY | **FIXED** | RO write attempts fail inside the real sandbox; host untouched |
| F02 | `containment.required` decorative | **FIXED** | Real bwrap argv constructed; missing/untrusted bwrap refuses before exec; no override env |
| F03 | `OPENCODE_PERMISSION` override | **FIXED** | Allowlisted env; `assert_no_host_secrets` refuses it; absent in child |
| F04 | Incomplete provider-config isolation | **PARTIALLY FIXED** | Env pins, `--pure`, synthetic HOME/XDG, version pin all present and the env names are real; but no test proves the live binary fails to *discover* host agents/plugins — the probe runs `/usr/bin/env`, not opencode, and hand-rolls its own bwrap argv |
| F05 | State redirect / symlink into `.git` | **FIXED** | Marker-gated provisioned root, `O_NOFOLLOW`, symlink rejection |
| F06 | Predictable job IDs / unsafe identifiers | **FIXED** | UUIDv4; `SAFE_ID`/`SAFE_JOB`; traversal refused |
| F07 | Shallow schema fallback | **FIXED** | `schema.py` `SystemExit`s at import without jsonschema; boundary objects Draft-07 validated |
| F08 | Hostile host execution environment | **FIXED** | Clean git env + allowlisted child env; dangerous families dropped; absolute executables |
| F09 | Pathname-only worktree identity | **FIXED** | dev/ino + gitdir + common + HEAD; `GIT_DIR` cannot redirect; re-validated post-run |
| F10 | Lease not a lifetime lock | **PARTIALLY FIXED** | Lifetime flock and mutual exclusion verified; token is not an authorization capability (N6) |
| F11 | Incomplete process-lifetime ownership | **FIXED** | pid-ns + die-with-parent + finalize-after-exit; setsid grandchild died; delayed write never landed |
| F12 | Handoff synthesized from arbitrary text | **FIXED** | Strict parse, single terminal, schema handoff; all malformed classes rejected |
| F13 | Manufactured review verdict promotes | **NOT FIXED** (property) | Wrapper-authored verdict is gone, but a reviewer that never examined the subject still promotes it — **N1** |
| F14 | Model family from mutable profile | **PARTIALLY FIXED** | Registry is controller-owned and the subject freezes its model; reviewer family still resolved live (N5) |
| F15 | Post-write commands outside the boundary | **PARTIALLY FIXED** | New `commands.py` is well-constrained and runs inside bwrap; undermined by N2 — the old host-side `ai-cmd` still ships and is install-mapped |
| F16 | Decorative security fields unused | **FIXED** | One compiled policy, digest carried on the job; residual dead code is not security-decorative (N8) |
| F17 | Test harness PATH fallthrough | **FIXED** | No PATH lookup; absolute mock; live refused without allow + pinned path; missing mock hard-fails |
| F18 | No real OS filesystem boundary | **FIXED** | bwrap mount namespace; full write-surface matrix verified in both modes |
| F19 | Review not bound to frozen subject evidence | **PARTIALLY FIXED** | Subject-side freeze and live-tree staleness work; reviewer→subject binding absent — **N1** |
| F20 | Multiple drifting policy representations | **FIXED** | Runtime JSON generated from the single compiled policy |

**Tally:** 14 FIXED · 5 PARTIALLY FIXED (F04, F10, F14, F15, F19) · 1 NOT FIXED at the property
level (F13). No finding REGRESSED in code. No finding was INVALID. New findings outside F01–F20:
**N2** (legacy binaries still shipped), **N3** (host-side git on the writable tree), **N4**
(non-zero write exit), **N7/N8**.

---

## 5. Test harness assessment

`agent-ops/tests/run.sh` runs clean: **29 adversarial tests + 3 config probes, all passing**
(`ALL HERMETIC/ADVERSARIAL TESTS PASSED`). The author's count is accurate.

What it genuinely proves: the mock is selected explicitly by absolute path; removing or breaking it
hard-fails; there is no PATH fallthrough to real opencode; no credentials are present; refusal
paths for lease, role, timeout, symlink-state, envelope-shape and provider-output classes fire.

What it does **not** prove, and should not be read as proving:

1. **No test asserts reviewer↔subject binding** — which is why N1 (CRITICAL) passes through a green
   suite. Green tests are the reason this defect survived to review.
2. **The config probes are weaker than the claim.** Of the three, two `skipTest` when the live
   binary is absent; the remaining one asserts that a dict the controller just built lacks a key it
   never added. The one probe that does launch bwrap **hand-rolls its own argv** instead of calling
   `sandbox.build_bwrap_argv`, and executes `/usr/bin/env` rather than opencode — so it tests
   neither the real sandbox construction nor OpenCode's config precedence.
3. **Coverage narrowed silently.** `run.sh` no longer invokes the pre-existing shell suites that
   remain in the tree — `tests/readonly/`, `tests/write/` (including
   `test_redteam_containment.sh`), `tests/lease/`, `tests/process/`, `tests/routing/`, and
   `tests/policy/test_refusals.sh`. They are dead files. In some dimensions the 29 new tests are a
   net reduction, and the tree misleads a reader into thinking they still run.
4. **`child-survive` is a weak supervision test.** It forks a child inside the same pid namespace
   and exits immediately; it does not attempt the delayed-write-after-finalization case. The real
   property does hold — this review verified it separately — but the test does not establish it.

No test treats "damage detected afterward" as equivalent to prevention. That framing discipline is
respected throughout, and is one of the clearer wins over the baseline.

---

## 6. Code-quality judgement

The move to Python **reduced** the security reasoning surface rather than merely translating shell
complexity. Concretely: one compiled `CompiledPolicy` frozen dataclass replaces the previous spread
of profile JSON, policy JSON, agent frontmatter and runtime JSON; the sandbox argv is constructed
in a single readable function; identity is a value object rather than a string; state writes go
through one `atomic_write_json`. Control flow in `run_job` is linear and followable.

Weaknesses, none of which argue for reverting:

- `run_job._execute` is long and mixes execution, integrity capture, status derivation and record
  assembly; the status variable is reassigned across several branches, which is how N4's
  mode-gated guard hides in plain sight.
- Duplicated/dead policy representations remain (`env._DROP_PREFIXES`/`_KEEP` vs the live
  allowlist), which is a mild recurrence of the F20 pattern.
- Exception paths are not uniformly clean: a `Refuse` raised from post-run identity checks escapes
  `_execute()` leaving no `result.json` (seen in N3's reproduction).
- The tautological comparisons in `promote` are the clearest example of a check that *reads* like
  enforcement while enforcing nothing — the single most important thing to fix, and to guard with
  a test that would fail if the comparison degenerated again.

Judged on control-flow comprehensibility rather than language preference, this is a real
improvement.

---

## 7. Verdicts

| # | Question | Verdict | Conditions |
|---|---|---|---|
| 1 | Architecture quality | **CONDITIONAL GO** | Python control plane plus a genuine bwrap boundary is sound and comprehensible. Conditional on N1 (core review property) and N2 (legacy surface still shipped). |
| 2 | Genuinely fixes the previous critical root causes | **MOSTLY — not fully** | RC1 (detection-as-boundary), RC4 (host env trusted), RC5 (identity as pathname) and RC6 (test fallthrough) are substantively fixed and verified. The RC2/RC3 review-integrity root cause is **not** closed: the promote gate still accepts an unbound review. |
| 3 | Generic READONLY live dry-run in a disposable clone | **CONDITIONAL GO** | READONLY containment empirically holds. Conditions: disposable/untrusted clone only; no credentials; accept the residual that host-side git runs on the target (N3 is near-zero in RO because the tree and `.git` are read-only). Not on a trusted host, and not against a repo whose local git config is attacker-authored. |
| 4 | Production READONLY installation | **NO-GO** | Agrees with the author. Blocked by N2 (install map ships unsandboxed binaries), F04 unproven at runtime, and the absence of any end-to-end live-provider evidence — note the isolation env forwards **no** credential material (`OPENCODE_API_KEY` / `OPENCODE_AUTH_CONTENT` are never set and the synthetic HOME is empty), so the live rail's authentication path appears never to have been exercised. |
| 5 | Meaningful bounded-WRITE security testing | **NO-GO** | Blocked by N1 — the property such testing would be validating is precisely the one that fails — plus N3 and N4. Fix N1 and N4, remove N2, then re-review to reach a lab-only CONDITIONAL GO. |
| 6 | Real-project WRITE | **NO-GO** | Agrees with the author; absolute. Requires N1, N3, N4, N5 closed, N2 removed, and an explicit decision on the network + credential posture. |
| 7 | Another remediation round before freezing the prototype | **REQUIRED** | N1 is a CRITICAL defect on the central write-safety property and must be fixed before freeze; N2 removed; N3 and N4 addressed. The boundary work itself is solid and should **not** be redone. |

### Bottom line

The remediation delivers a real, OS-enforced authority boundary and closes most of the baseline's
critical root causes — verified by execution, not accepted on report. It should **not** be frozen
or advanced to any write gate yet, because the promote path still trusts a "review" that need never
have looked at the subject, and the superseded unsandboxed command binaries still ship. Neither
requires rewriting the boundary; both are bounded, well-understood fixes.

A green test suite was not sufficient to establish the authority boundary here, and did not: N1
passes 29 adversarial tests.
