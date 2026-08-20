# Design note — session-store retention and job protection (2026-08-20)

Wave 1A carried this as open item 3: `gc --include-sessions` can delete a
protected job's session store, because `_session_candidates` receives the job
rows and never reads them. The owner's ruling for Wave 1B was to **investigate
and recommend, not to build**: no new retention subsystem and no public-contract
behaviour change was to land with P5–P8.

Nothing in this note is implemented. It exists so the decision is made against
measurement rather than against the plausible-sounding version of the problem.

## What the code actually does

- A session store is keyed by **worktree identity, per provider** —
  `<state>/sessions/<identity_key>/<harness>/...`, created in `job.py` beside a
  `worktree.json` marker. `identity_key` is
  `sha256(f"{st_dev}:{st_ino}:{realpath}")` (`lease.py`).
- It is therefore **not** per job. One store serves every job any harness has
  ever run on that worktree.
- `gc._session_candidates` proposes a store for removal only when its recorded
  worktree path does not exist **and** no lease directory exists under the same
  identity key. A store whose worktree is still present is never a candidate,
  whatever the state of the jobs that wrote it.
- Session stores are excluded from a bare `--yes`; `--include-sessions` is
  required on top of it.

So the case the handoff names is real but narrow: a job protected as
`awaiting_review` or `awaiting_external_review` whose **worktree has been
deleted**, whose session store is then collectable under
`gc --include-sessions --yes`.

## What that case is actually worth

Measured against the code paths, once the worktree is gone the protected job can
no longer do either of the things the session store exists for:

- **It cannot be promoted.** `attach_review` inspects the subject's worktree and
  `promote` compares a freshly sampled live head and tree digest against the
  freeze. With the directory gone there is nothing to sample and nothing to land.
- **It cannot be resumed.** `resume` is a new bounded job in that directory; the
  rail refuses a cwd that is not a git worktree before any provider runs.

What remains in the store after that is the harness's private transcript. The
rail's own evidence for the job — `result.json`, `evidence/events.jsonl`, the
versioned `evidence/events.v*.jsonl`, the handoff — lives in the job directory,
which job protection already covers and which `--include-sessions` does not touch.

## Two measurements that change the shape of the question

**1. An orphaned store is not permanently unreachable — it is re-adoptable.**
The obvious argument for deleting orphans is that the identity key can never be
reconstructed. That argument is wrong on this filesystem. Recreating a worktree
at the same path, five trials on ext4:

```console
$ python3 probe_inode.py
trial 0: ino=10235910 key=011ebbe3a6ed8d16
trial 1: ino=10235910 key=011ebbe3a6ed8d16
trial 2: ino=10235910 key=011ebbe3a6ed8d16
trial 3: ino=10235910 key=011ebbe3a6ed8d16
trial 4: ino=10235910 key=011ebbe3a6ed8d16
identical key across recreate: 4/4
```

The inode was reused every time, so the recreated worktree resolved to the
**same** identity key. A store left behind by a deleted worktree can therefore be
picked up by a different, later worktree at the same path, and the harness would
begin with a conversation from work it has no relationship to. That is an
argument *for* collecting orphaned stores, not against it — and it is a distinct
hazard worth its own decision, independent of retention.

**2. The "unverifiable is skipped" rule is weaker than its docstring.**
`_session_candidates` documents that a worktree that cannot be stat'ed is
reported as skipped, "an unmounted disk or a permission error is not proof that a
conversation is orphaned". It implements that with `os.path.exists()` inside a
`try/except OSError`. Measured:

```console
$ python3 probe_exists.py
os.path.exists(unreadable-parent/wt): False
os.stat raises: PermissionError errno 13
```

`os.path.exists` swallows the `OSError` and returns `False`, so the `except`
branch is not reached for a permission error, and such a store is planned for
**removal** rather than skipped. The same is true of a path under a mount point
that is not currently mounted: the child simply does not exist. This is a claim
the code does not honour, which is the defect class this repo grades most
seriously. **Surfaced here rather than fixed** — per the owner's ruling, a
correction to an existing documented invariant is a separate decision, and it is
not part of P5–P8.

## Recommendation

**Do not couple session retention to job protection.** Three reasons, in order of
weight:

1. **The coupling is a category error.** Stores are per worktree; protection is
   per job. "Protect the store of a protected job" unavoidably means "protect the
   store if *any* job on this worktree is protected" — and on a long-lived
   worktree that is a store that can never be collected, which is how a bounded
   reclamation feature becomes an unbounded one.
2. **It would protect nothing recoverable.** In the only case where the rule
   fires, the worktree is already gone, so the protected job is neither
   promotable nor resumable and the rail's own evidence is retained separately.
3. **It buys a real hazard.** Measurement 1 shows a retained orphan can be
   adopted by an unrelated later worktree at the same path.

**Do fix the honesty of the interface, separately.** Two items, neither in
P5–P8's scope, both cheap, and each needing an owner decision because each
touches an existing documented invariant:

- `_session_candidates(root, rows)` accepts the job rows and never reads them.
  A signature that implies a coupling the function does not have is the same
  defect as a comment describing a check that does not exist: either consult the
  rows or drop the parameter, and say in the docstring that retention is
  worktree-scoped by design.
- Replace `os.path.exists` with an explicit `os.stat` so the documented
  skip-the-unverifiable rule is the rule that runs, and give the store's
  reported reason the errno that decided it.

**Open for contract-v1-rc1**, not for this wave: whether a session store should
be bound to more than the inode triple, so a recreated worktree at a reused
inode starts a fresh conversation instead of inheriting one. That is a change to
how a store is *identified*, not to how long it is *kept*, and it is the more
useful of the two questions.
