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
MIN_AGE_S = int(os.environ.get("SWITCHGEAR_GC_MIN_AGE_S") or 3600)


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


def _launch_artifacts(root: StateRoot, job_id: str) -> list[str]:
    """Existing launch files tied to a job, computed for the dry-run plan."""
    launch = os.path.join(root.path, "launch")
    paths = [os.path.join(launch, f"{job_id}{suffix}")
             for suffix in (".json", ".out", ".err")]
    return [path for path in paths if os.path.lexists(path)]


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

        # Launch-only records are classified below from their liveness triple.
        # Running them through job-directory retention rules would report a
        # malformed record as protected and then sweep it in the same plan.
        if not os.path.isdir(jd):
            continue

        if row["state"] == "awaiting_review":
            keep("awaiting review")
            continue
        if row["state"] == "awaiting_external_review":
            # This is an outstanding decision the tool does not own and cannot
            # observe. It can therefore never learn that the job is finished
            # with, so collecting its evidence would silently decide for the
            # external acceptance authority.
            keep("awaiting external review")
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
            # Additive and planned: dry-run callers see every side effect before
            # --yes can remove the files.
            "launch_artifacts": _launch_artifacts(root, job_id),
        })

    orphan_launches, protected_launches = _orphan_launch_records(root)
    protected.extend(protected_launches)
    sessions = _session_candidates(root, rows) if include_sessions else {"remove": [], "skipped": []}

    return {
        "jobs": candidates,
        "protected": protected,
        "orphan_launch_records": orphan_launches,
        "sessions": sessions["remove"],
        "sessions_skipped": sessions["skipped"],
        "bytes": sum(c["bytes"] for c in candidates) + sum(s["bytes"] for s in sessions["remove"]),
    }


def _orphan_launch_records(root: StateRoot) -> tuple[list[str], list[dict[str, str]]]:
    """Classify launch records that have no job directory.

    Sweepable litter is only a record liveness cannot be DECIDED from at all --
    unparseable, or with no convertible pid. Anything liveness can be decided
    from is kept: a dead verdict is the sole identity left by a launch that
    crashed before its job directory existed, and deleting it erased exactly the
    crash evidence an operator needed to reconstruct the failure. Live
    launch-only records are protected for the same reason live jobs are.

    Stated as "decidable" rather than "a usable triple" because those are not
    the same set and the difference is not academic: `_liveness` only requires a
    convertible `pid`, so a record carrying a pid but no starttime or boot_id is
    decided (dead) and protected. That errs toward keeping evidence, which is
    this module's standing bias -- but the comment used to describe a stricter
    rule than the code applies, and a comment that overstates the code is the
    defect class this project grades most seriously.
    """
    out: list[str] = []
    protected: list[dict[str, str]] = []
    launch = os.path.join(root.path, "launch")
    if not os.path.isdir(launch):
        return out, protected
    for name in sorted(os.listdir(launch)):
        if not name.endswith(".json"):
            continue
        job_id = name[: -len(".json")]
        if os.path.isdir(os.path.join(root.jobs, job_id)):
            continue
        path = os.path.join(launch, name)
        liveness = jobstate._liveness(path)
        if liveness is None:
            out.append(path)
            continue
        state = jobstate.live_state(
            root.path, job_id, {}, os.path.join(root.jobs, job_id)
        )
        if liveness is False:
            reason = (
                "cancelled launch-only record is terminal evidence"
                if state == "cancelled"
                else "dead launch-only record is crash evidence"
            )
        else:
            reason = "launch process is still alive"
        protected.append({"job_id": job_id, "state": state, "reason": reason})
    return out, protected


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
    launch_artifacts_removed: list[str] = []
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
            status = rec.get("status")
            if status == "awaiting_review":
                kept.append({"job_id": job_id, "reason": "now awaiting review"})
                continue
            if status == "awaiting_external_review":
                # The external authority's outstanding decision is not
                # observable here, including during the delete-time recheck.
                kept.append({"job_id": job_id,
                             "reason": "now awaiting external review"})
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
        # The plan named these exact files, and they are reclaimed only after
        # the corresponding job directory was actually removed.
        expected = set(_launch_artifacts(root, job_id))
        for path in cand.get("launch_artifacts", []):
            if path not in expected:
                continue
            try:
                os.unlink(path)
            except OSError:
                continue
            launch_artifacts_removed.append(path)

    launch_records_removed = 0
    for path in planned.get("orphan_launch_records", []):
        # The same re-check the job loop does, and for the same reason: planning
        # walks a whole state root, and a record that was unusable litter at plan
        # time can be a live launch by the time we get here -- the id is reused
        # by a relaunch, or the record was mid-write when it was classified.
        # This loop deleted unconditionally, so it was the one destructive path
        # in gc with no delete-time recheck at all.
        if jobstate._liveness(path) is not None:
            kept.append({"job_id": os.path.basename(path)[: -len(".json")],
                         "reason": "launch record became identifiable since the plan"})
            continue
        try:
            os.unlink(path)
        except OSError:
            continue
        launch_records_removed += 1

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
        "protected": planned.get("protected", []),
        "sessions_removed": sessions_removed,
        # Counted from what was ACTUALLY unlinked. Reporting the planned length
        # would over-report every record the recheck above just saved.
        "launch_records_removed": launch_records_removed,
        "launch_artifacts_removed": launch_artifacts_removed,
        "bytes_freed": freed,
    }
