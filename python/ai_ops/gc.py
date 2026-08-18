"""Reclaim old jobs from a state root, on purpose and never by surprise.

Nothing was ever removed. That is defensible -- evidence is audit material -- but
it is unbounded, and there was no way to even see what had accumulated.

Every default here leans the same way: **prefer keeping something eligible over
destroying something irreplaceable.** Concretely that means opt-in (never
automatic, never on a timer), dry-run unless `--yes`, a required selector so an
empty invocation deletes nothing, a cool-down floor on top of whatever the caller
asked for, and session stores excluded from a bare `--yes` because a job
directory is reproducible -- re-run the job -- while a session store is the only
durable copy of a conversation someone may still want to resume.

The protection rules are checked against records, not inferred. A review's link
to its subject is read from `review_of`, which cmd_review persists
unconditionally; deriving it from timestamps and directory names is exactly the
kind of guess that tests clean and deletes a real pending review in production.
"""

from __future__ import annotations

import os
import time
from typing import Any

from . import jobstate
from .errors import Refuse
from .paths import safe_rmtree
from .state import StateRoot, read_json

# A floor UNDER the caller's selector, never a substitute for it. Rule 3 below
# only protects a chain whose reviewer recorded `review_of`; the floor bounds the
# blast radius for anything written before that key existed, or by a caller that
# does not set it. Mirrors MIN_FREE_BYTES: an operator-overridable safety margin.
MIN_AGE_S = int(os.environ.get("AI_OPS_GC_MIN_AGE_S") or 3600)


def _dir_bytes(path: str) -> int:
    """Size by BLOCK COUNT, following no symlinks.

    `os.path.getsize` follows symlinks, and Codex symlinks ~258MB of binaries
    into each sandbox home -- measured, and it made a 10MB state root report as
    2GB. Reporting bytes we cannot actually reclaim would justify deletions that
    free nothing.
    """
    try:
        # The directory itself, which os.walk yields as `base` but never as an
        # entry. Omitting it undercounted every job by exactly one block.
        total = os.lstat(path).st_blocks * 512
    except OSError:
        return 0
    for base, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(base, d))]
        for name in files + dirs:
            full = os.path.join(base, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            total += st.st_blocks * 512
    return total


def _alive(state_path: str, job_id: str, jd: str) -> bool:
    # `queued` is alive too -- waiting for a concurrency slot, not finished.
    # Deleting one would destroy a job that is about to start.
    return jobstate.live_state(state_path, job_id, {}, jd) in ("running", "queued")


def plan(
    state_path: str,
    *,
    older_than_s: float | None = None,
    keep_last: int | None = None,
    include_sessions: bool = False,
) -> dict[str, Any]:
    """What WOULD be removed, and what is protected and why.

    Every protected entry carries its reason, so a caller who expected a job to
    go can see which rule kept it rather than assuming gc is broken.
    """
    from .joblist import enumerate_jobs

    if older_than_s is None and keep_last is None:
        raise Refuse(
            "gc needs a selector: --older-than <duration> or --keep-last N. "
            "Refusing to guess what you meant by 'clean up' — nothing was removed."
        )

    root = StateRoot(state_path)
    now = time.time()
    listing = enumerate_jobs(state_path, limit=None)
    rows = listing["jobs"]

    # Which subjects still await a review. A reviewer job pointing at one of
    # these is part of a live chain and must survive with it.
    awaiting = {r["job_id"] for r in rows if r["state"] == "awaiting_review"}

    candidates: list[dict[str, Any]] = []
    protected: list[dict[str, Any]] = []

    # keep_last counts by recency over ALL jobs, before any other rule, so
    # "keep the last 10" means the last 10 that exist.
    keep_ids: set[str] = set()
    if keep_last is not None:
        keep_ids = {r["job_id"] for r in rows[:keep_last]}

    for row in rows:
        job_id = row["job_id"]
        jd = os.path.join(root.jobs, job_id)
        started = row.get("started_at")
        age = (now - started) if started else None

        def keep(reason: str) -> None:
            protected.append({"job_id": job_id, "state": row["state"], "reason": reason})

        if row["state"] == "awaiting_review":
            keep("awaiting review")
            continue
        if row.get("review_of") in awaiting:
            keep(f"review of {row['review_of']}, which still awaits review")
            continue
        if _alive(state_path, job_id, jd):
            keep("still running")
            continue
        if row["state"] == "unknown":
            # `unknown` means liveness could not be established at all -- no
            # result, no runner record, no launch record. That is not the same as
            # dead, and the rest of this rail never reads a missing record as a
            # benign state. Deleting here would be the one place that does, on
            # the job most likely to be mid-flight. Measured: 3 of 22 jobs on the
            # real dogfooding root are `unknown`, and every one was a candidate
            # before this rule existed.
            keep("liveness unknown; a missing record is not evidence it is dead")
            continue
        if job_id in keep_ids:
            keep(f"within the {keep_last} most recent")
            continue
        if age is None:
            # No start marker means no age; an unknown age is not an old age.
            keep("no start time recorded, so its age is unknown")
            continue
        if age < MIN_AGE_S:
            keep(f"younger than the {MIN_AGE_S}s cool-down floor")
            continue
        if older_than_s is not None and age < older_than_s:
            keep("newer than the requested window")
            continue

        candidates.append({
            "job_id": job_id,
            "state": row["state"],
            "age_s": round(age),
            "bytes": _dir_bytes(jd) if os.path.isdir(jd) else 0,
            "path": jd,
        })

    orphan_launches = _orphan_launch_records(root, {r["job_id"] for r in rows})
    sessions = _session_candidates(root, rows) if include_sessions else {"remove": [], "skipped": []}

    return {
        "jobs": candidates,
        "protected": protected,
        "orphan_launch_records": orphan_launches,
        "sessions": sessions["remove"],
        "sessions_skipped": sessions["skipped"],
        "bytes": sum(c["bytes"] for c in candidates) + sum(s["bytes"] for s in sessions["remove"]),
    }


def _orphan_launch_records(root: StateRoot, known_jobs: set[str]) -> list[str]:
    """Launch records whose job directory was never created and whose pid is
    dead. Pure litter, always eligible -- there is nothing to preserve."""
    out = []
    launch = os.path.join(root.path, "launch")
    if not os.path.isdir(launch):
        return out
    for name in sorted(os.listdir(launch)):
        if not name.endswith(".json"):
            continue
        job_id = name[: -len(".json")]
        if os.path.isdir(os.path.join(root.jobs, job_id)):
            continue
        if jobstate.live_state(root.path, job_id, {}, os.path.join(root.jobs, job_id)) in ("running", "queued"):
            continue
        out.append(os.path.join(launch, name))
    return out


def _session_candidates(root: StateRoot, rows: list[dict[str, Any]]) -> dict[str, list]:
    """Session stores whose worktree no longer exists.

    Returns `remove` and `skipped`. The distinction is the whole point: if a
    worktree cannot be stat'ed, its key is UNVERIFIABLE, not absent -- an
    unmounted disk or a permission error is not proof that a conversation is
    orphaned. Those are skipped and reported, never deleted.
    """
    remove: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    sessions = os.path.join(root.path, "sessions")
    if not os.path.isdir(sessions):
        return {"remove": remove, "skipped": skipped}

    for key in sorted(os.listdir(sessions)):
        kdir = os.path.join(sessions, key)
        if not os.path.isdir(kdir):
            continue
        marker = os.path.join(kdir, "worktree.json")
        if not os.path.isfile(marker):
            skipped.append({"key": key, "reason": "no worktree marker; cannot identify it"})
            continue
        try:
            wt = read_json(marker).get("worktree")
        except Exception:
            skipped.append({"key": key, "reason": "worktree marker unreadable"})
            continue
        if not wt:
            skipped.append({"key": key, "reason": "worktree marker names no path"})
            continue
        try:
            exists = os.path.exists(wt)
        except OSError:
            skipped.append({"key": key, "reason": f"cannot stat {wt}"})
            continue
        if exists:
            continue  # the worktree is still there; the conversation may be wanted
        # Leases are stored under the SAME identity key, so a live lease is a
        # direct lookup rather than something to infer. A leased worktree whose
        # path has vanished is a contradiction worth reporting, not resolving.
        if os.path.isdir(os.path.join(root.leases, key)):
            skipped.append({"key": key, "reason": "a lease still exists for it"})
            continue
        remove.append({"key": key, "worktree": wt, "bytes": _dir_bytes(kdir), "path": kdir})
    return {"remove": remove, "skipped": skipped}


def apply(state_path: str, planned: dict[str, Any]) -> dict[str, Any]:
    """Delete what the plan chose, re-checking liveness at delete time.

    The recheck is not paranoia: planning walks a whole state root, and a job can
    be launched between the plan and the deletion. Removing a running job's
    directory would destroy evidence of work in flight.
    """
    root = StateRoot(state_path)
    removed, kept = [], []
    freed = 0
    for cand in planned.get("jobs", []):
        job_id = cand["job_id"]
        jd = os.path.join(root.jobs, job_id)
        if _alive(state_path, job_id, jd):
            kept.append({"job_id": job_id, "reason": "started running since the plan"})
            continue
        # A result written since the plan can change the state to one that is
        # protected, so re-read rather than trusting the snapshot.
        try:
            rec = read_json(os.path.join(jd, "result.json"))
            if rec.get("status") == "awaiting_review":
                kept.append({"job_id": job_id, "reason": "now awaiting review"})
                continue
        except Exception:
            pass
        try:
            # Guarded: the job id is validated, but the guard belongs at the
            # delete rather than in the discipline of whoever assembled the path.
            safe_rmtree(jd, must_be_under=root.jobs, label=f"job {job_id}")
        except (OSError, Refuse) as exc:
            kept.append({"job_id": job_id, "reason": f"could not remove: {exc}"})
            continue
        freed += cand.get("bytes", 0)
        removed.append(job_id)

    for path in planned.get("orphan_launch_records", []):
        try:
            os.unlink(path)
        except OSError:
            continue

    sessions_removed = []
    sessions_root = os.path.join(root.path, "sessions")
    for store in planned.get("sessions", []):
        try:
            safe_rmtree(store["path"], must_be_under=sessions_root,
                        label=f"session store {store['key']}")
        except (OSError, Refuse):
            continue
        freed += store.get("bytes", 0)
        sessions_removed.append(store["key"])

    return {
        "removed": removed,
        "kept": kept,
        "sessions_removed": sessions_removed,
        "launch_records_removed": len(planned.get("orphan_launch_records", [])),
        "bytes_freed": freed,
    }
