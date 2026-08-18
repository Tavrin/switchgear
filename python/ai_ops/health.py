"""How each provider and model has actually been behaving, from the records.

A dead model (glm-5.3 during dogfooding) failed every job it touched, and the
rail had no memory of it: each job discovered the failure from scratch, spent
its timeout, and left the next caller no wiser.

**Reports; never gates.** This deliberately does not refuse a job for an
unhealthy model, and the argument that settles it is the audience: part of it is
an AI agent driving this tool, and an agent testing a fix FOR the failing model
would be refused from testing its own fix. A silent auto-refusal is also a second
invisible gate whose state is not inspectable unless it is surfaced anyway -- so
surfacing is a strict subset of the work, and the useful subset. If a hard gate
is ever wanted, it should be operator-owned and opt-in like every other limit
here.

Aggregated from the same enumeration `jobs` uses, so the two cannot disagree
about what "recent" means.
"""

from __future__ import annotations

import os
from typing import Any

from . import jobstate
from .state import StateRoot, read_json

#: How many recent jobs to consider. Bounded because this is read by `models`
#: and `doctor`, which must stay cheap enough to call casually.
DEFAULT_SCAN = 200

#: Below this many observations, a failure ratio is noise rather than a signal.
#: Two failures out of two is not evidence a model is dead.
MIN_OBSERVATIONS = 3

#: At or above this failure ratio, a model is worth mentioning.
UNHEALTHY_RATIO = 0.5


def observe(state_path: str, scan: int = DEFAULT_SCAN) -> dict[str, Any]:
    """Per-model outcome counts over the most recent jobs.

    Only TERMINAL states are counted. A running job has no outcome yet, and a job
    of unknown liveness has no outcome anyone can establish -- counting either as
    a failure would manufacture bad news out of missing information.
    """
    from .joblist import enumerate_jobs

    root = StateRoot(state_path)
    rows = enumerate_jobs(state_path, limit=scan)["jobs"]

    models: dict[str, dict[str, Any]] = {}
    for row in rows:
        state = row.get("state")
        if state in ("running", "unknown"):
            continue
        model = row.get("model")
        if not model:
            continue
        slot = models.setdefault(model, {
            "model": model,
            "provider": row.get("provider"),
            "ok": 0,
            "failed": 0,
            "denied": 0,
            "transport": 0,
            "last_failure": None,
        })
        if state in jobstate.FAILURE_STATUSES or state == "died":
            slot["failed"] += 1
            slot["last_failure"] = state
        else:
            slot["ok"] += 1

        # Broker counters separate a POLICY denial from an upstream blip. A
        # network problem and a model refusing requests need different actions,
        # and blending them would hide both.
        try:
            rec = read_json(os.path.join(root.jobs, row["job_id"], "result.json"))
            calls = rec.get("provider_calls") or {}
            slot["denied"] += int(calls.get("denied") or 0)
            slot["transport"] += int(calls.get("transport") or 0)
        except Exception:
            pass

    out = []
    for slot in models.values():
        total = slot["ok"] + slot["failed"]
        slot["observations"] = total
        slot["failure_ratio"] = round(slot["failed"] / total, 3) if total else 0.0
        slot["unhealthy"] = (
            total >= MIN_OBSERVATIONS and slot["failure_ratio"] >= UNHEALTHY_RATIO
        )
        out.append(slot)
    out.sort(key=lambda s: (-s["failure_ratio"], -s["observations"]))
    return {"models": out, "scanned": len(rows), "scan_limit": scan}


def warnings(state_path: str, scan: int = DEFAULT_SCAN) -> list[str]:
    """One line per unhealthy model, for doctor. Empty when all is well."""
    try:
        seen = observe(state_path, scan)
    except Exception:
        return []
    return [
        f"{m['model']}: {m['failed']}/{m['observations']} recent jobs failed "
        f"(last: {m['last_failure']})"
        for m in seen["models"] if m["unhealthy"]
    ]
