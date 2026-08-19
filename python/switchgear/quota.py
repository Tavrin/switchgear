"""Quota and budget.

Two different things, and conflating them would produce a number that looks
authoritative and is not:

1. **External quota readings** for subscription pools that publish one --
   `~/.cache/ai-quota/{claude,codex}.json`. switchgear does not spend these; it
   reports them so a CALLER routing across providers can decide. Every reading
   carries `captured_at`, and a stale reading is worse than no reading because it
   invites a confident wrong decision, so staleness is always reported alongside.

2. **The budget switchgear actually owns.** The pool it really spends
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

import calendar
import json
import os
import time
from typing import Any

from .errors import Refuse

QUOTA_DIR = os.path.expanduser("~/.cache/ai-quota")
DEFAULT_BUDGET_FILE = os.path.expanduser("~/.config/switchgear/budget.json")

# A reading older than this is reported as stale. The routing policy is explicit
# that quota is measured, not guessed, and a six-hour-old percentage is a guess
# wearing a measurement's clothes.
STALE_AFTER_S = 3600


def budget_path() -> str:
    return os.environ.get("SWITCHGEAR_BUDGET_FILE") or DEFAULT_BUDGET_FILE


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


# --- the ledger switchgear keeps for itself ------------------------------------


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


def read_ledger(state_path: str, since_ts: float = 0.0) -> list[dict[str, Any]]:
    """Ledger entries at or after `since_ts`.

    Same torn-final-line tolerance as spent_since, and for the same reason: the
    file is appended to while jobs run, so the last line is routinely half
    written. A malformed line is skipped, never a reason to refuse an answer
    about the lines that are fine.
    """
    path = ledger_path(state_path)
    out: list[dict[str, Any]] = []
    if not os.path.isfile(path):
        return out
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if float(rec.get("ts") or 0) >= since_ts:
                    out.append(rec)
    except OSError:
        pass
    return out


def rollup(state_path: str, since_ts: float = 0.0) -> dict[str, Any]:
    """Measured spend aggregated by provider, by model and by UTC day.

    The ledger was a flat list with a single "spent today" sum over it, which
    answers whether you may start a job and nothing else. The question a caller
    routing across providers actually has -- where is the money going -- needed
    every entry read by hand.

    Costs are summed as recorded. No estimation, no extrapolation: every figure
    here traces to a provider's own per-step report for a specific job.
    """
    entries = read_ledger(state_path, since_ts)
    by_provider: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    by_day: dict[str, float] = {}

    for rec in entries:
        model = str(rec.get("model") or "unknown")
        # The pool, which is what a caller routes on. Model ids are
        # provider-qualified by construction (`claude/claude-haiku-4-5`).
        provider = model.split("/", 1)[0] if "/" in model else "unknown"
        cost = float(rec.get("costUSD") or 0.0)
        ts = float(rec.get("ts") or 0)

        for bucket, key in ((by_provider, provider), (by_model, model)):
            slot = bucket.setdefault(key, {"cost_usd": 0.0, "jobs": 0})
            slot["cost_usd"] += cost
            slot["jobs"] += 1
        day = time.strftime("%Y-%m-%d", time.gmtime(ts))
        by_day[day] = by_day.get(day, 0.0) + cost

    def ranked(bucket: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        rows = [{"name": k, **v} for k, v in bucket.items()]
        for row in rows:
            row["cost_usd"] = round(row["cost_usd"], 6)
            # $0.00 across every job does NOT mean free. Measured: Codex on a
            # ChatGPT subscription reports no per-step cost at all, so its
            # entries are genuinely 0.0 while the work is billed against a
            # subscription elsewhere. Reporting that as a cost of zero would
            # invite a caller to route everything there believing it is free, so
            # the two cases are labelled rather than blended.
            row["metered"] = row["cost_usd"] > 0
            # True when some of this row's spend came from compacted history,
            # whose per-day totals carry no per-model job count.
            row.setdefault("jobs_partial", False)
        return sorted(rows, key=lambda r: -r["cost_usd"])

    # Fold in compacted history, or a `quota --rollup` after a compaction would
    # report the past as never having happened -- the exact failure that makes
    # people distrust a compaction step.
    compacted_total = 0.0
    compacted_jobs = 0
    for day_rec in read_rollup(state_path):
        day = day_rec.get("day")
        cost = float(day_rec.get("cost_usd") or 0.0)
        # calendar.timegm, not time.mktime: the days are formatted with
        # time.gmtime and day_start() is UTC, so parsing them as LOCAL time gave
        # an offset-sized window where --rollup --today included or dropped the
        # wrong day.
        day_ts = calendar.timegm(time.strptime(day, "%Y-%m-%d")) if day else 0
        if since_ts and day and day_ts < since_ts:
            continue
        compacted_total += cost
        compacted_jobs += int(day_rec.get("jobs") or 0)
        if day:
            by_day[day] = by_day.get(day, 0.0) + cost
        for model, mcost in (day_rec.get("by_model") or {}).items():
            provider = model.split("/", 1)[0] if "/" in model else "unknown"
            for bucket, key in ((by_provider, provider), (by_model, model)):
                slot = bucket.setdefault(key, {"cost_usd": 0.0, "jobs": 0})
                slot["cost_usd"] += float(mcost or 0.0)
                # The rollup keeps a per-DAY job count, not a per-model one, so
                # the exact split cannot be recovered. Mark the row rather than
                # leave its count silently short of the total, which is what it
                # did: after a compaction the per-row counts and the headline
                # figure disagreed with nothing to say why.
                slot["jobs_partial"] = True

    unmetered = sorted(k for k, v in by_provider.items() if v["cost_usd"] <= 0)
    return {
        "total_usd": round(
            sum(float(r.get("costUSD") or 0.0) for r in entries) + compacted_total, 6),
        "jobs": len(entries) + compacted_jobs,
        "from_compacted_rollup_usd": round(compacted_total, 6),
        # Providers that ran jobs and reported no cost. The total below is
        # therefore a floor on what was spent, not the whole bill.
        "unmetered_providers": unmetered,
        "by_provider": ranked(by_provider),
        "by_model": ranked(by_model),
        "by_day": [{"day": d, "cost_usd": round(c, 6)} for d, c in sorted(by_day.items())],
    }


def rollup_path(state_path: str) -> str:
    return os.path.join(state_path, "spend-rollup.jsonl")


def compact_ledger(state_path: str, *, apply: bool = False) -> dict[str, Any]:
    """Fold history into an append-only rollup and keep only today in the ledger.

    `spend.jsonl` grows forever, and the ONLY thing that reads it in the hot path
    is assert_within_budget, via spent_since(day_start()) -- so everything before
    today is pure history. It is still history worth keeping, hence a rollup file
    rather than a delete.

    The rollup is append-only and per UTC day, so compacting twice cannot
    double-count: a day already present is not re-added. Today is never folded,
    because it is not finished and the ledger must keep every entry the budget
    check needs to see.

    The property that matters is asserted by a test rather than argued here:
    spent_since(day_start()) is identical before and after.
    """
    entries = read_ledger(state_path)
    today = day_start()
    old = [r for r in entries if float(r.get("ts") or 0) < today]
    current = [r for r in entries if float(r.get("ts") or 0) >= today]
    if not old:
        return {"compacted": 0, "kept": len(current), "days": [], "applied": False}

    by_day: dict[str, dict[str, Any]] = {}
    for rec in old:
        day = time.strftime("%Y-%m-%d", time.gmtime(float(rec.get("ts") or 0)))
        model = str(rec.get("model") or "unknown")
        slot = by_day.setdefault(day, {"day": day, "jobs": 0, "cost_usd": 0.0, "by_model": {}})
        slot["jobs"] += 1
        slot["cost_usd"] += float(rec.get("costUSD") or 0.0)
        slot["by_model"][model] = round(
            slot["by_model"].get(model, 0.0) + float(rec.get("costUSD") or 0.0), 6)

    existing_days = {r.get("day") for r in read_rollup(state_path)}
    new_days = [v for k, v in sorted(by_day.items()) if k not in existing_days]

    result = {
        "compacted": len(old),
        "kept": len(current),
        "days": [d["day"] for d in new_days],
        "rollup": rollup_path(state_path),
        "applied": False,
    }
    if not apply:
        return result

    # Append the rollup BEFORE truncating the ledger. If this process dies
    # between the two, the worst case is a rollup entry with the ledger still
    # holding its rows -- recoverable and visible. The other order loses spend
    # history outright.
    with open(rollup_path(state_path), "a", encoding="utf-8") as fh:
        for day in new_days:
            day["cost_usd"] = round(day["cost_usd"], 6)
            fh.write(json.dumps(day, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())

    tmp = ledger_path(state_path) + ".compact"
    with open(tmp, "w", encoding="utf-8") as fh:
        for rec in current:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, ledger_path(state_path))
    result["applied"] = True
    return result


def read_rollup(state_path: str) -> list[dict[str, Any]]:
    """Compacted per-day history. Absent file is fine; a torn line is skipped."""
    out: list[dict[str, Any]] = []
    path = rollup_path(state_path)
    if not os.path.isfile(path):
        return out
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


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
