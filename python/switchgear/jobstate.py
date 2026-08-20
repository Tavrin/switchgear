"""What a job is doing right now, and what counts as failure.

Extracted from cli.py because three consumers need it for MANY jobs, not one:
`status` (a single job), `jobs` (a listing) and provider health (an aggregate).
The CLI version took an argparse Namespace purely to read `ns.job` and resolve
the state path, which made it unusable anywhere else.

The rule this module encodes was earned five separate times during dogfooding:
**absence of a record is not evidence of a benign state.** A missing result.json
was read as "running" for a cancelled job, a crashed background job, a foreground
job whose process died, and a job id that never existed. Liveness is therefore
recorded (pid + starttime + boot_id, since pids are recycled) and checked, never
inferred from what is missing.
"""

from __future__ import annotations

import os
from typing import Any

from .state import read_json

# Terminal statuses that mean the job did NOT do its work. Shared so the exit-code
# mapping, provider health and any future consumer cannot drift apart -- they had
# already drifted once between cmd_run_like and cmd_review.
#
# `refused` and `review_failed` are deliberately still here although
# project_status cannot produce them and the schema no longer lists them. This
# set decides EXIT CODES, so it must fail safe: an unexpected status reaching it
# should map to failure, never to success. Being a superset costs nothing;
# being a subset would report a bad job as a good one.
FAILURE_STATUSES = frozenset(
    {"provider_error", "timeout", "dirty", "refused", "review_failed"}
)

# Terminal statuses that mean the job DID do its work. `exit_code_for` must use
# this set directly: provider health had already drifted from the zero-exit table
# by treating cancellation and unknown terminal values as success. Neither
# consumer may manufacture success outside the same closed set.
SUCCESS_STATUSES = frozenset(
    {"ok", "awaiting_review", "awaiting_external_review"}
)

# States derived from liveness rather than from a persisted record.
DERIVED_STATES = frozenset({"running", "queued", "died", "cancelled", "unknown"})

# --- the four orthogonal facts behind `status` --------------------------------
#
# `status` answers four unrelated questions with one word: did the process do its
# work (ok / provider_error / timeout), is the worktree still what it was
# (dirty), is there a frozen change, and has anything accepted it
# (awaiting_review). That is serviceable for a CLI and poor as an infrastructure
# protocol -- a caller wanting "did the provider fail?" has to know that `dirty`
# outranks it, and a job that BOTH errored and left a dirty tree reports only the
# dirty half.
#
# So the facts are recorded separately and `status` is derived from them. It is
# still the primary field and its values have not changed; nothing that reads it
# needs to know this happened.

EXECUTION_COMPLETED = "completed"
EXECUTION_PROVIDER_ERROR = "provider_error"
EXECUTION_TIMEOUT = "timeout"

INTEGRITY_CLEAN = "clean"
INTEGRITY_DIRTY = "dirty"

CHANGE_NONE = "none"
CHANGE_FROZEN = "frozen"

ACCEPTANCE_NOT_REQUIRED = "not_required"
ACCEPTANCE_AWAITING_REVIEW = "awaiting_review"
ACCEPTANCE_ACCEPTED = "accepted"


#: Who owns semantic acceptance of a bounded write.
#:
#: `interlock` (the default, and what this tool has always done): Switchgear's
#: own review gate decides. A write finishes `awaiting_review` and `promote`
#: binds it to reviewer-attested evidence.
#:
#: `external`: the caller has explicitly assumed acceptance. A write still
#: freezes and still produces the same evidence -- Switchgear's responsibility,
#: proving what the worker did inside the boundary, is unchanged -- but nothing
#: here will mark it accepted, and `promote` refuses rather than pretending to
#: an authority it was told it does not have.
#:
#: The distinction this exists to make legible: Switchgear's review is an
#: INTERLOCK on worker output, run on an uncommitted delta before any project
#: verification. A caller's review is PROJECT ACCEPTANCE, run on the exact head
#: that passed its tests. Stacked, those are defence in depth; conflated, they
#: are two overlapping sources of truth and it is not obvious which you have.
ACCEPTANCE_INTERLOCK = "interlock"
ACCEPTANCE_EXTERNAL = "external"
ACCEPTANCE_AWAITING_EXTERNAL = "awaiting_external_review"


def acceptance_authority() -> str:
    """Operator-owned, never caller-supplied and never profile-declared.

    In the same file as `daily_usd` and for the same reason: a project that can
    vote itself out of review does not have review. A CLI flag would be worse
    still -- the worker's own output can reach a caller's argv.

    Absent means `interlock`, so an operator who has never heard of this setting
    gets the stricter behaviour.
    """
    from .errors import Refuse
    from .quota import budget_path, load_budget

    value = (load_budget().get("acceptance") or ACCEPTANCE_INTERLOCK)
    if value not in (ACCEPTANCE_INTERLOCK, ACCEPTANCE_EXTERNAL):
        raise Refuse(
            f"acceptance must be {ACCEPTANCE_INTERLOCK!r} or "
            f"{ACCEPTANCE_EXTERNAL!r} in {budget_path()}, got {value!r}. "
            f"{ACCEPTANCE_INTERLOCK!r} means this tool's review gate decides; "
            f"{ACCEPTANCE_EXTERNAL!r} means the caller has assumed that "
            "responsibility and `promote` will refuse."
        )
    return value


def project_status(
    *, execution: str, integrity: str, acceptance: str, change: str = CHANGE_NONE
) -> str:
    """The four facts -> the one `status` value callers already branch on.

    The precedence is not arbitrary and must not be reordered: it reproduces
    exactly what the old flat assignment produced, including the cases where two
    things went wrong at once. A timed-out job that also left the tree dirty
    reported `timeout` before this existed, and still does.

    `change` is not consulted. A frozen delta is a fact about the job, not an
    outcome -- it is `awaiting_review` that says something is waiting on a
    decision -- and folding it in here would make `status` mean a fifth thing.
    """
    if execution == EXECUTION_TIMEOUT:
        return "timeout"
    if execution == EXECUTION_PROVIDER_ERROR:
        return "provider_error"
    if integrity == INTEGRITY_DIRTY:
        return "dirty"
    if acceptance == ACCEPTANCE_AWAITING_REVIEW:
        return "awaiting_review"
    if acceptance == ACCEPTANCE_AWAITING_EXTERNAL:
        # Deliberately NOT reported as `awaiting_review`. The job is not waiting
        # on this tool's gate -- there is no gate here to wait for -- and a
        # caller that polled for `awaiting_review` and promoted would be acting
        # on a decision nothing made. Only reachable when an operator has set
        # acceptance=external, so no default behaviour changes.
        return "awaiting_external_review"
    return "ok"


def _liveness(path: str) -> bool | None:
    """True/False if the record lets us tell, None if there is no usable record."""
    from .lease import _alive

    if not os.path.isfile(path):
        return None
    try:
        meta = read_json(path)
        return _alive(int(meta["pid"]), meta.get("starttime", ""), meta.get("boot_id", ""))
    except Exception:
        return None


def launch_record_path(state_path: str, job_id: str) -> str:
    return os.path.join(state_path, "launch", f"{job_id}.json")


def _running_or_queued(state_path: str, job_id: str) -> str:
    """Executing, or alive but waiting for a concurrency slot.

    A soak run made the difference matter: 12 jobs against a cap of 2 reported
    ELEVEN as `running`, because "its process is alive" was being read as "it is
    doing work". The cap was holding perfectly -- only two were ever executing --
    but an orchestrator polling for running jobs would have seen a number that
    was true of nothing.

    The concurrency marker is the ground truth: a job holds one for exactly as
    long as it occupies a slot. With no cap configured there are no markers and
    nothing to wait for, so every live job is running.
    """
    from . import concurrency

    if concurrency.limit() is None:
        return "running"
    marker = os.path.join(state_path, "running", f"{job_id}.json")
    return "running" if os.path.isfile(marker) else "queued"


def live_state(state_path: str, job_id: str, rec: dict[str, Any], jd: str) -> str:
    """The job's state now: a persisted status, or one derived from liveness.

    `rec` is the parsed result.json (empty dict if absent) and `jd` the job
    directory. A persisted string status always wins -- it is the job's own
    account of how it ended. A record with no string outcome falls through to
    the same liveness evidence as an absent record; it cannot invent a terminal
    state from missing or malformed data.
    """
    if rec:
        status = rec.get("status")
        if isinstance(status, str):
            return status

    # A backgrounded job's launch record also carries cancellation INTENT, which
    # the in-job runner record cannot know.
    meta_path = launch_record_path(state_path, job_id)
    launched = _liveness(meta_path)
    if launched is False:
        try:
            if read_json(meta_path).get("cancelled"):
                return "cancelled"
        except Exception:
            pass
        return "died"
    if launched is True:
        return _running_or_queued(state_path, job_id)

    # Foreground jobs write the same triple into the job directory.
    runner = _liveness(os.path.join(jd, "runner.json"))
    if runner is True:
        return _running_or_queued(state_path, job_id)
    if runner is False:
        return "died"
    return "unknown"


def exit_code_for(status: str) -> int:
    """The one exit-code mapping, so callers can branch on it reliably.

    It had already drifted: cmd_run_like mapped timeout->124 while cmd_review
    silently did not, so `rc == 124` meant different things for `scout` and
    `review`. One table, used everywhere.

        0    the job did its work (ok, awaiting_review, awaiting_external_review)
        1    refusal or provider error
        2    dirty -- worktree integrity changed during the job
        124  timed out
    """
    if status == "dirty":
        return 2
    if status == "timeout":
        return 124
    # External acceptance is success here because the handoff happened; whether
    # the frozen change lands is a decision this tool does not own.
    if status in SUCCESS_STATUSES:
        return 0
    return 1
