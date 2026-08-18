"""How many jobs may run at once against one state root.

N dispatches spawn N sandboxes and N model streams, with nothing between the
caller and the machine. That is fine until an orchestrator fans out.

Operator-owned, in budget.json beside daily_usd, and **absent means unlimited**.
Never profile-declared, for the same reason the budget is not: a project that can
raise its own ceiling does not have one. Absent-means-unlimited is also what lets
this land without breaking anyone who has not opted in.

Counting is the interesting part. The obvious approach -- scan job records for
ones without a result -- is wrong in a way that gets worse with time: job dirs
are permanent (only sandbox-home is reclaimed), so that scan grows with the state
root's age forever, and it would have to re-derive liveness for every job ever
run just to start one. Instead each running job holds a marker in
`<state>/running/`, carrying the same {pid, starttime, boot_id} triple as
runner.json. Counting is then proportional to jobs CURRENTLY running, and a
crashed job's stale marker is skipped by the same liveness check used everywhere
else -- so a crash cannot permanently consume a slot.
"""

from __future__ import annotations

import os
import time
from typing import Any

from .errors import Refuse
from .state import atomic_write_json, read_json

#: How long a background job may wait for a slot before refusing. Bounded on
#: purpose: an unbounded wait turns a full queue into a hang with no diagnosis.
WAIT_S = int(os.environ.get("AI_OPS_CONCURRENCY_WAIT_S") or 600)

_POLL_S = 2.0


def limit() -> int | None:
    """The configured ceiling, or None for unlimited."""
    from .quota import load_budget

    value = load_budget().get("max_concurrent_jobs")
    if not isinstance(value, int) or value <= 0:
        return None
    return value


def _markers_dir(state_path: str) -> str:
    return os.path.join(state_path, "running")


def running(state_path: str) -> list[dict[str, Any]]:
    """Jobs currently holding a slot, stale markers excluded.

    A marker whose process is gone is removed as it is found. Leaving it would
    let one crash consume a slot until someone noticed, which is exactly the
    failure mode a concurrency cap must not introduce.
    """
    from .lease import _alive

    out = []
    d = _markers_dir(state_path)
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(d, name)
        try:
            rec = read_json(path)
            alive = _alive(int(rec["pid"]), rec.get("starttime", ""), rec.get("boot_id", ""))
        except Exception:
            # An unreadable marker cannot be shown to be alive, and holding a
            # slot for it forever is worse than reclaiming it.
            alive = False
        if alive:
            out.append({"job_id": name[: -len(".json")], **rec})
        else:
            try:
                os.unlink(path)
            except OSError:
                pass
    return out


def acquire(state_path: str, job_id: str, *, wait: bool) -> float:
    """Take a slot, or refuse. Returns seconds spent queued.

    Foreground callers refuse immediately: a person or a script waiting at a
    terminal wants to be told, not stalled. Background callers wait up to WAIT_S,
    because a fan-out that briefly exceeds the cap should smooth out rather than
    fail -- but the wait is bounded, and the time spent queued is returned so it
    lands in the record instead of silently inflating "elapsed".
    """
    cap = limit()
    if cap is None:
        return 0.0

    d = _markers_dir(state_path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    started = time.time()
    deadline = started + (WAIT_S if wait else 0)

    while True:
        current = running(state_path)
        if len(current) < cap:
            _write_marker(d, job_id)
            # Re-count after claiming: two callers can pass the check at the same
            # moment, and the marker is what makes the race visible. Whoever ends
            # up over the line yields rather than both proceeding.
            if len(running(state_path)) <= cap:
                return round(time.time() - started, 2)
            release(state_path, job_id)

        if time.time() >= deadline:
            waited = "" if not wait else f" after waiting {int(time.time() - started)}s"
            raise Refuse(
                f"concurrency limit reached: {len(current)} of {cap} slots in use"
                f"{waited}. Wait for a job to finish, raise max_concurrent_jobs in "
                "the budget file, or run with --background to queue "
                f"(up to {WAIT_S}s; AI_OPS_CONCURRENCY_WAIT_S overrides)."
            )
        time.sleep(_POLL_S)


def _write_marker(d: str, job_id: str) -> None:
    from .lease import _boot_id, _starttime

    atomic_write_json(os.path.join(d, f"{job_id}.json"), {
        "pid": os.getpid(),
        "starttime": _starttime(os.getpid()),
        "boot_id": _boot_id(),
        "since": time.time(),
    })


def release(state_path: str, job_id: str) -> None:
    """Give the slot back. Safe to call when no marker exists."""
    try:
        os.unlink(os.path.join(_markers_dir(state_path), f"{job_id}.json"))
    except OSError:
        pass
