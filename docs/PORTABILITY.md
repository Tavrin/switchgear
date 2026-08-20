# Running switchgear somewhere other than Linux

Short answer: **run it inside a Linux VM or container.** Everything works, with
every containment property intact, and there is no code to write.

A native macOS or Windows port is possible but is not a porting job — it is a
re-proving job. This file records exactly what is platform-bound, so that
decision is made against a measurement rather than an impression.

## What is actually platform-bound

Measured across the 9.4k-line codebase, and it is smaller than it feels: **two
things**, both behind a single seam each.

### 1. Containment: `bwrap`

Three call sites, all through `sandbox.build_bwrap_argv()`. Nothing else in the
rail builds a sandbox.

The security model is expressed in **mounts**: for a bounded-write job the
worktree is the only writable bind, the git dir is read-only, HOME is a synthetic
empty directory, and the network namespace is unshared whenever a broker socket
exists. Those are not decorative flags — they are the boundary, and the rest of
the design (evidence integrity, the credential broker, the freeze/review chain)
assumes it holds.

### 2. Liveness: `/proc`

Three functions in `lease.py` — `_boot_id()`, `_starttime()`, `_alive()` —
reading `/proc/{pid}/stat` and `/proc/sys/kernel/random/boot_id`. Everything that
asks "is this still running?" goes through `_alive`: leases, `status`, `jobs`,
`gc`, and the concurrency cap.

This exists because **a pid alone is not an identity** — pids are recycled, so a
stale record could otherwise point at an unrelated live process. The triple
(pid, process start time, boot id) is what makes the answer trustworthy.

### What is *not* a problem

`flock`, `killpg`, `start_new_session`, `RLIMIT_FSIZE`, `st_blocks`, atomic
`os.replace` and the whole evidence/normalization layer are POSIX and work on
macOS as-is. The four provider CLIs all ship macOS builds.

## Option 1 — Linux VM or container (recommended)

Lima, OrbStack, UTM, or Docker. Every containment property holds **because it is
still Linux**: the same bwrap flags, the same namespaces, the same `/proc`.
Nothing is re-proven because nothing changed.

Caveats worth knowing before starting:

- The provider CLIs authenticate with OAuth device flows that open a browser. In
  a headless VM you log in once and the credential file lands inside the VM;
  `doctor` will tell you if it is missing or expired.
- Keep the state root **inside** the VM's own filesystem, not on a shared mount.
  Worktree identity is `(st_dev, st_ino)` and lease keys hash it; virtiofs and
  other shared filesystems do not always preserve inode identity across
  remounts, which would invalidate leases. Session lineages have random ids, but
  their binding records still require those worktree facts to match on resume,
  so a remount can deliberately make an existing conversation non-resumable.
- Bind the repo you are working on into the VM as normal, but expect shared-mount
  filesystems to be slower for the tree digest on large repos.

## Option 2 — a native macOS backend

Real work, and the work is mostly *not* the code.

**Containment.** macOS has no bubblewrap and no user namespaces. The nearest
equivalent is `sandbox-exec` (Seatbelt), which is deprecated-but-present and
takes an SBPL profile. It can express file read/write allow-deny and can deny
network. But it is a **different mechanism, not a translation**: bwrap says "this
is the only writable mount", Seatbelt says "these path patterns are writable".
Those are equivalent in intent and not equivalent in failure mode — path-pattern
rules have to contend with symlinks, `/private` vs `/tmp` aliasing, and
`realpath` differences, each of which is a way for a boundary to be weaker than
it reads.

So the port is: implement the backend, then **re-run the entire adversarial suite
on macOS** and re-establish the containment claims there. The suite exists
precisely because these properties were not obvious the first time — several were
wrong until a test caught them. None of that evidence transfers to a different
kernel.

**Liveness.** Straightforward: process start time from `sysctl KERN_PROC` (or
`psutil`), boot id from `sysctl kern.boottime`. Behind the same `_alive` seam, so
it is one small module.

**Honest estimate of the split:** the code is perhaps a few days. Re-proving the
boundary is the actual project, and until it is done the tool would be claiming a
security property it has not demonstrated on that platform — which is the one
thing this rail does not do.

## Option 3 — no sandbox

Not offered, and will not be. `require_bwrap` refuses rather than degrading, and
`doctor` reports the absence as `fail`. A rail whose containment silently becomes
optional is worse than no rail, because callers keep trusting the evidence.

## Windows

Same shape as macOS but further away: no `/proc`, no bwrap, and the closest
containment primitives (job objects, AppContainer) are a larger gap from the
mount-based model than Seatbelt is. WSL2 is Linux and therefore Option 1.

## What about the orchestrator?

Different repo, different answer, and it is not settled here. The orchestrator is the
orchestration layer above this one; its portability depends on its own
dependencies, not on bwrap. But note the coupling: if the orchestrator runs natively on
macOS while switchgear runs inside a Linux VM, then **paths and process identity
cross a boundary** — the orchestrator's worktree paths, its process fence
(`linux-proc-start:<bootId>:<startTime>`, which switchgear publishes from the
launch record) and the state root all have to be meaningful on both sides. The
simplest arrangement by a distance is to run both inside the same Linux VM.
