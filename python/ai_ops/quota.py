"""Quota and budget.

Two different things, and conflating them would produce a number that looks
authoritative and is not:

1. **External quota readings** for subscription pools that publish one --
   `~/.cache/ai-quota/{claude,codex}.json`. agent-ops does not spend these; it
   reports them so a CALLER routing across providers can decide. Every reading
   carries `captured_at`, and a stale reading is worse than no reading because it
   invites a confident wrong decision, so staleness is always reported alongside.

2. **The budget agent-ops actually owns.** The pool it really spends
   (`opencode-go`) publishes no quota at all, so there is nothing to read. What
   the rail *can* do is measure: every job's real cost now comes back from the
   provider stream. So the enforceable control is a spend ceiling over measured
   cost, plus a hard cap on provider calls per job.

The budget is OPERATOR-owned, never profile-declared, for the same reason the
model registry is: anything a repository can declare about itself, a hostile
repository will declare about itself. A project that could raise its own ceiling
does not have a ceiling.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from .errors import Refuse

QUOTA_DIR = os.path.expanduser("~/.cache/ai-quota")
DEFAULT_BUDGET_FILE = os.path.expanduser("~/.config/ai-ops/budget.json")

# A reading older than this is reported as stale. The routing policy is explicit
# that quota is measured, not guessed, and a six-hour-old percentage is a guess
# wearing a measurement's clothes.
STALE_AFTER_S = 3600


def budget_path() -> str:
    return os.environ.get("AI_OPS_BUDGET_FILE") or DEFAULT_BUDGET_FILE


def load_budget() -> dict[str, Any]:
    """Operator-owned limits. Absent file means unlimited, and says so."""
    path = budget_path()
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise Refuse(f"budget file {path} must contain an object")
    return data


def read_external(provider: str) -> dict[str, Any] | None:
    """Normalize a published quota file, or None if there is not one.

    claude.json carries `windows` as an object keyed by window name; codex.json
    carries it as a list of records. Both are reduced to the same shape so a
    caller does not have to know which it is looking at.
    """
    path = os.path.join(QUOTA_DIR, f"{provider}.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return None

    windows: list[dict[str, Any]] = []
    src = raw.get("windows")
    if isinstance(src, dict):
        for name, rec in src.items():
            if isinstance(rec, dict):
                windows.append(
                    {
                        "name": name,
                        "remaining_percent": rec.get("remaining_percent"),
                        "resets_at": rec.get("resets_at"),
                    }
                )
    elif isinstance(src, list):
        for rec in src:
            if isinstance(rec, dict):
                windows.append(
                    {
                        "name": rec.get("limit_id") or rec.get("window") or "?",
                        "remaining_percent": rec.get("remaining_percent"),
                        "resets_at": rec.get("resets_at"),
                    }
                )

    captured = raw.get("captured_at")
    age = int(time.time() - captured) if isinstance(captured, (int, float)) else None
    remaining = [
        w["remaining_percent"] for w in windows if isinstance(w["remaining_percent"], (int, float))
    ]
    return {
        "provider": provider,
        "captured_at": captured,
        "age_s": age,
        # Route on the WORST window: a weekly pool with 3% left is not rescued by
        # a five-hour window that just reset.
        "min_remaining_percent": min(remaining) if remaining else None,
        "stale": (age is None or age > STALE_AFTER_S),
        "windows": windows,
    }


def external_all() -> list[dict[str, Any]]:
    if not os.path.isdir(QUOTA_DIR):
        return []
    out = []
    for entry in sorted(os.listdir(QUOTA_DIR)):
        if entry.endswith(".json"):
            rec = read_external(entry[: -len(".json")])
            if rec:
                out.append(rec)
    return out


# --- the ledger agent-ops keeps for itself ------------------------------------


def ledger_path(state_path: str) -> str:
    return os.path.join(state_path, "spend.jsonl")


def record_spend(state_path: str, job_id: str, model: str, cost_usd: float) -> None:
    """Append one job's measured cost. Never raises into the caller's path.

    Recording spend must not be able to fail a job that already ran and already
    cost money -- losing the accounting is bad, losing the work as well is worse.
    """
    try:
        line = json.dumps(
            {"ts": time.time(), "job": job_id, "model": model, "costUSD": float(cost_usd)},
            separators=(",", ":"),
        )
        with open(ledger_path(state_path), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except (OSError, TypeError, ValueError):
        pass


def spent_since(state_path: str, since_ts: float) -> float:
    path = ledger_path(state_path)
    if not os.path.isfile(path):
        return 0.0
    total = 0.0
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # a torn final line is not a reason to refuse
                if float(rec.get("ts") or 0) >= since_ts:
                    total += float(rec.get("costUSD") or 0.0)
    except OSError:
        return total
    return total


def day_start(now: float | None = None) -> float:
    now = time.time() if now is None else now
    return now - (now % 86400)


def assert_within_budget(state_path: str) -> None:
    """Pre-flight. Refuse to start a job once the day's ceiling is reached.

    HONEST LIMIT, stated because a budget that quietly overshoots is worse than
    none: this bounds spend BEFORE a job starts, not during one. A job's cost is
    only known once its stream reports it, so a single runaway job can still
    exceed the ceiling within itself. The per-job bound is
    `max_provider_calls_per_job`, enforced by the broker, plus the job timeout.
    """
    budget = load_budget()
    limit = budget.get("daily_usd")
    if not isinstance(limit, (int, float)) or limit <= 0:
        return
    spent = spent_since(state_path, day_start())
    if spent >= limit:
        raise Refuse(
            f"daily budget exhausted: ${spent:.4f} spent of ${float(limit):.2f} "
            f"(ledger {ledger_path(state_path)}); raise daily_usd in {budget_path()} "
            "or wait for the next UTC day"
        )


def max_provider_calls() -> int | None:
    """A hard per-job ceiling on brokered requests.

    Earned by a real incident: a reviewer with no shell and no git, asked to find
    a diff it could not reach, looped 35 times. Nothing stopped it -- the job
    timeout was the only bound, and it billed every one of those calls. A call
    ceiling turns that from a bill into a refusal.
    """
    value = load_budget().get("max_provider_calls_per_job")
    return int(value) if isinstance(value, (int, float)) and value > 0 else None
