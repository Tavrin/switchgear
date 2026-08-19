# OS containment

**Status: deployed.** Every provider invocation runs under `bwrap`, including
`--version`. There is no path that executes the provider outside the sandbox and
no silent fallback: if `/usr/bin/bwrap` is missing, not executable, or is a
symlink resolving outside `/usr/`, the rail refuses (`sandbox.require_bwrap`).

This file describes what `sandbox.build_bwrap_argv` actually constructs. When it
and the code disagree, the code is right and this file is a bug.

## The mount namespace

```
--unshare-pid --unshare-uts --unshare-ipc --die-with-parent --new-session
--proc /proc   --dev /dev   --tmpfs /tmp

ro-bind   /usr /bin /lib /lib64
ro-bind   /etc/{resolv.conf,ssl,ca-certificates,hosts,nsswitch.conf,passwd,group}
ro-bind   the common git dir, and the worktree's own git dir
bind      the synthetic HOME            (read/write)
bind|ro   the leased worktree           (rw for bounded-write, ro for readonly)
ro-bind   any command binds the profile allows
--chdir   the worktree
```

Nothing else is mounted. Measured from inside: `/` contains exactly
`[bin, dev, etc, lib, lib64, proc, tmp, usr]`. The host `$HOME`, sibling
worktrees and the state store do not merely deny access — they do not exist
(`ENOENT`), which is a stronger and much harder-to-probe property than a
permission denial.

For a readonly job the worktree, the git dir and the common git dir are all
read-only; for bounded-write only the leased worktree becomes writable. The
common git dir is never writable in either mode.

## Network

With a credential broker in play the sandbox also gets `--unshare-net` and the
broker's unix socket bind-mounted at `/run/switchgear-broker.sock`. A unix socket is
a filesystem object and keeps working across a network namespace, so the job has
**no outbound reachability whatsoever** except the one brokered upstream.
Measured: `direct_internet: BLOCKED`, `via_broker: OK 200`.

The namespace is requested *only* when there is a broker socket to bind. That
coupling is deliberate but it is also a sharp edge: a live job with no
credential used to fall through to the unbrokered path and thereby run on the
host network. It now refuses (`job.run_job`, "no credential"). Keep that
refusal — any future path that runs a provider without a broker must either
supply its own network isolation or be refused.

The hermetic mock path has no broker and therefore no `--unshare-net`. The mock
makes no outbound connections; the property that matters there is that the mock
is never the pinned binary.

## The credential

The credential never enters the sandbox. It is read controller-side from an
operator-owned file (mode 600 enforced), held by `broker.CredentialBroker`, and
attached as `Authorization` on the way upstream. The sandbox's config carries
the literal string `broker-placeholder-not-a-credential` and a loopback URL.

The broker is not a transparent proxy. It allowlists request paths
(`/chat/completions`, `/messages` — OpenAI-shaped and Anthropic-shaped models
respectively) and pins the request to the model the controller resolved, so a
job cannot spend the key on a model the profile never allowed. It counts what it
forwarded and what it denied, and those counts are part of the job record —
which is what makes `forwarded == 0` a usable signal for "never reached a model".

The host environment is never forwarded. The provider environment is built from
scratch by `env.allowlisted_env` and asserted secret-free, rather than filtered
from the controller's own — filtering fails open on every variable nobody thought
of.

## Resource bounds

`RLIMIT_FSIZE` (default 256MiB) is applied by prefixing the launch with
`prlimit --fsize=`, so the kernel inherits it to every process in the sandbox.
It is deliberately *not* a `preexec_fn`: that runs between `fork` and `exec` in a
process that may hold locks a thread was holding at fork time, which is unsafe in
a controller that drains output on threads.

It bounds any single file the worker writes. Polling for size cannot do this: a
worker writes 500MB well inside any poll interval.

**It does not bound the stdout spool**, because evidence is streamed through a
pipe and a pipe is not a file — the drain's own byte cap does that. See
`docs/OBSERVABILITY.md`. `RLIMIT_FSIZE` still bounds the files the worker writes
into its worktree, which is its actual job.

The worker runs in its own session and process group (`start_new_session`,
`--new-session`), and `--die-with-parent` plus a descendant walk means SIGKILL of
the controller leaves **zero** surviving `bwrap` or provider processes (measured).

## The uid boundary

Read-only jobs get a real one; bounded-write jobs deliberately do not. That
asymmetry is an ownership problem, not an oversight.

**Read-only.** When the machine can establish it, the payload runs as a subuid:
`--userns-block-fd` plus a two-range `newuidmap`/`newgidmap` map, then
`setpriv --reuid 1`. A write to a file owned by the invoking user is then refused
by the kernel rather than by policy. `userns.capability()` reports whether this
machine can do it and `job.py` gates on that — a machine without `/etc/subuid`
entries or the setuid map helpers gets mount and network isolation only.
`switchgear capabilities` reports which of the two this machine gives you.

> `--unshare-user --uid N` on its own looks like this and is not. Measured: your
> files appear owned by the sandbox uid and stay writable. It confines nothing.
> That is why the map helpers are load-bearing.

**Bounded-write.** Same uid as the invoking user. A worker must produce files the
controller then reads, freezes and commits; files written by a subuid are owned by
that subuid, and handing them back needs privileges Switchgear does not have.
Closing this needs idmapped mounts, ACLs, or an ownership fix-up at collection —
an ownership model, not a flag. Until then, for a bounded-write job the isolation
is the mount and network namespace: anything writable by the invoking uid *and
visible in the namespace* is writable by the job, and the defence is that almost
nothing is visible.

## What this is not

- **Not a uid boundary for bounded-write jobs.** See above.
- **Not a defence against a malicious `bwrap`, kernel or upstream.**
- **Not a substitute for pointing it at the right directory.**

## Policy

```json
"containment": { "mode": "none"|"bwrap", "required": false }
```

If `required` is true and the backend is missing or unusable → **refuse**.
If `mode=bwrap` and `bwrap` is absent → **refuse**. No silent fallback to `none`.
