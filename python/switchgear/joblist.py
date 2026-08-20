"""Enumerate jobs in a state root.

There was no way to see what a state root contains. Measured on a real one after
a day of use: 22 jobs, four of which had no result.json at all -- crashed or
interrupted runs that were completely invisible. You cannot operate what you
cannot list.

Cheap by construction, because an agent will poll this: it reads result.json,
runner.json and the started_at marker, and NEVER opens evidence/events.jsonl.
`logs` exists for that, and reading a stream per row would make a listing cost
more than the jobs it lists.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

from . import jobstate
from .errors import Refuse
from .paths import require_job_id
from .state import StateRoot, read_json

_DURATION = re.compile(r"^(\d+)([smhd])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> float:
    """`30m`, `24h`, `7d` -> seconds. Refuses anything else, by name."""
    match = _DURATION.match((text or "").strip())
    if not match:
        raise Refuse(
            f"unrecognised duration {text!r}: use a number followed by s, m, h or d "
            "(for example 30m, 24h, 7d)"
        )
    return float(match.group(1)) * _UNITS[match.group(2)]


def _as_record(value: Any) -> dict[str, Any]:
    """A parsed record, or {} if it is not an object.

    `json.load` succeeding does not mean a record was read: `[]`, `"x"` and `123`
    all parse fine and none of them has `.get`. Measured on the runner-record
    fallback -- a single job whose runner.json held a bare `123` raised
    AttributeError out of enumerate_jobs and took down the WHOLE listing, every
    other job included. This module's rule is that an unreadable record must not
    hide a job; a record that parses to the wrong type has to obey it too.
    """
    return value if isinstance(value, dict) else {}


def _started_at(job_dir: str) -> float | None:
    try:
        with open(os.path.join(job_dir, "started_at"), encoding="utf-8") as fh:
            return float(fh.read().strip())
    except (OSError, ValueError):
        return None


def _last_write(job_dir: str) -> float | None:
    """When this job last produced anything, from the files it actually wrote.

    A job that is no longer running stopped at its last write, not now. Anchoring
    a dead job to the wall clock made a crashed job read `4686s elapsed` on the
    real state root -- it had run for seconds and then been dead for an hour and
    a quarter. Returns None when nothing is datable, because an unknown duration
    must show as unknown rather than as a plausible number.
    """
    stamps = []
    for rel in ("result.json", "evidence/events.jsonl", "evidence/stderr", "runner.json"):
        try:
            stamps.append(os.path.getmtime(os.path.join(job_dir, rel)))
        except OSError:
            continue
    return max(stamps) if stamps else None


def _job_ids(root: StateRoot) -> set[str]:
    """Job ids known to this state root, from directories AND launch records.

    A job that was launched but died before its directory existed is real and
    must be listable -- that is precisely the kind of job nobody can currently
    see.
    """
    found: set[str] = set()
    for name in os.listdir(root.jobs):
        try:
            require_job_id(name)
        except Refuse:
            continue  # skip litter rather than aborting the whole listing
        found.add(name)
    launch = os.path.join(root.path, "launch")
    if os.path.isdir(launch):
        for name in os.listdir(launch):
            if not name.endswith(".json"):
                continue
            stem = name[: -len(".json")]
            try:
                require_job_id(stem)
            except Refuse:
                continue
            found.add(stem)
    return found


def enumerate_jobs(
    state_path: str,
    *,
    states: set[str] | None = None,
    since_s: float | None = None,
    worktree: str | None = None,
    limit: int | None = 20,
) -> dict[str, Any]:
    """Jobs newest-first, with live state. See module docstring for the cost rule."""
    root = StateRoot(state_path)
    now = time.time()
    want_dir = os.path.realpath(worktree) if worktree else None

    rows: list[dict[str, Any]] = []
    for job_id in _job_ids(root):
        jd = os.path.join(root.jobs, job_id)
        rec: dict[str, Any] = {}
        result_path = os.path.join(jd, "result.json")
        if os.path.isfile(result_path):
            try:
                rec = _as_record(read_json(result_path))
            except Exception:
                rec = {}  # unreadable record is not a reason to hide the job

        runner: dict[str, Any] = {}
        runner_path = os.path.join(jd, "runner.json")
        if os.path.isfile(runner_path):
            try:
                runner = _as_record(read_json(runner_path))
            except Exception:
                runner = {}  # attribution unknown; liveness decides separately

        # Attribution is descriptive only. live_state receives the result record
        # exactly as before, so runner metadata cannot manufacture or alter a
        # terminal state when result.json is absent or unreadable.
        state = jobstate.live_state(state_path, job_id, rec, jd)
        started = _started_at(jd)
        ref = now if state == "running" else _last_write(jd)
        attribution = rec or runner
        model = attribution.get("model") or {}
        job_dir_value = attribution.get("dir")
        harness = (
            rec.get("harness") or rec.get("provider")
            or runner.get("harness") or runner.get("provider")
        )
        pool = model.get("provider") if isinstance(model, dict) else None

        if states and state not in states:
            continue
        if since_s is not None and (started is None or now - started > since_s):
            continue
        if want_dir is not None:
            if not job_dir_value or os.path.realpath(job_dir_value) != want_dir:
                continue

        rows.append(
            {
                "job_id": job_id,
                "state": state,
                "mode": attribution.get("mode"),
                "role": attribution.get("role"),
                "model": model.get("id") if isinstance(model, dict) else None,
                # A listing row's legacy `provider` has always named the model
                # pool, while a result record's `provider` names the harness.
                # Both aliases remain so existing consumers keep their meaning.
                "provider": pool,
                "harness": harness,
                "pool": pool,
                "dir": job_dir_value,
                "started_at": started,
                "elapsed_s": (
                    round(ref - started, 1)
                    if started is not None and ref is not None
                    else None
                ),
                "cost_usd": rec.get("cost_usd"),
                # Derived rather than making every caller know this is spelled as
                # a status value -- it is the single field an orchestrator polls.
                "awaiting_review": state == "awaiting_review",
                "resumed_from": (rec.get("resumed") or {}).get("from_job"),
                "review_of": rec.get("review_of"),
            }
        )

    rows.sort(key=lambda r: (r["started_at"] is None, -(r["started_at"] or 0)))
    total = len(rows)
    truncated = limit is not None and total > limit
    if truncated:
        rows = rows[:limit]
    return {"jobs": rows, "truncated": truncated, "total": total}
