from __future__ import annotations

import os
from typing import Any

from . import jobstate, quota
from .errors import Refuse
from .schema import validate, validate_result
from .state import atomic_write_json, read_json


def independence(subject_model: dict[str, Any], reviewer_model: dict[str, Any], sjob: str, rjob: str) -> dict[str, bool]:
    return {
        "different_job": sjob != rjob,
        "different_model": subject_model.get("id") != reviewer_model.get("id"),
        "different_family": bool(subject_model.get("model_family") and reviewer_model.get("model_family"))
        and subject_model.get("model_family") != reviewer_model.get("model_family"),
        "different_provider": (subject_model.get("provider") or "")
        != (reviewer_model.get("provider") or ""),
    }


def unmet_required(policy_indep: dict[str, str], indep: dict[str, bool]) -> list[str]:
    unmet = []
    for key in ("different_job", "different_model", "different_family", "different_provider"):
        if policy_indep.get(key) == "required" and not indep.get(key):
            unmet.append(key)
    return unmet


# Severities that disqualify a promotion regardless of the stated verdict.
BLOCKING_SEVERITIES = ("blocker", "critical", "high")


def blocking_findings(
    findings: Any, severities: tuple[str, ...] = BLOCKING_SEVERITIES
) -> list[dict[str, Any]]:
    """Findings serious enough to override an approving verdict.

    A live reviewer documented two real defects and still returned
    `promote` -- and a second run of the SAME model on the SAME diff returned
    `needs_changes`. The verdict is a probabilistic judgement; the findings the
    reviewer itself wrote down are harder evidence, so they get a vote.
    """
    out = []
    for f in findings or []:
        if isinstance(f, dict) and str(f.get("severity", "")).lower() in severities:
            out.append(f)
    return out


def promote(
    *,
    subject_path: str,
    review_artifact: dict[str, Any],
    live_head: str,
    live_tree_digest: str,
    expected_files: list[str] | None = None,
    blocking_severities: tuple[str, ...] = BLOCKING_SEVERITIES,
    generation: int,
    reviewed_content: str | None = None,
) -> dict[str, Any]:
    """Promote a reviewed subject, or refuse with the reason.

    `reviewed_content` is the material the reviewer actually read — the frozen
    diff. It is scanned for content addressed at a reviewing agent, because the
    verdict is the ONE load-bearing input here that comes from a model's
    judgement rather than from a digest, and a diff that talks to the reviewer
    makes that judgement untrustworthy. See injection.py.
    """
    validate(review_artifact, "review.schema.json")
    subject = read_json(subject_path)
    if subject.get("generation", 0) != generation:
        raise Refuse("concurrent promotion (generation mismatch)")
    if subject.get("status") == jobstate.ACCEPTANCE_AWAITING_EXTERNAL:
        # Refusing rather than promoting anyway is the whole point of the mode.
        # An operator said acceptance belongs to the caller; promoting here would
        # hand back an `accepted` this tool has no standing to assert, and the
        # caller's own gate would then be reviewing something already marked good.
        raise Refuse(
            "this job's acceptance is owned by the caller, not by switchgear "
            f"(acceptance=external in {quota.budget_path()}). The change is frozen "
            "and its evidence is complete -- what switchgear can attest is that "
            "the worker did what the record says, inside the boundary. Whether it "
            "should land is the caller's decision to record. Set "
            "acceptance=interlock if you want this tool's review gate back."
        )
    if subject.get("status") != "awaiting_review":
        raise Refuse(f"subject is not awaiting_review ({subject.get('status')})")
    freeze = subject.get("freeze") or {}
    if review_artifact["subject_job"] != subject["job_id"]:
        raise Refuse("review subject_job mismatch")

    # The binding gate: what the REVIEWER inspected must be the subject's frozen
    # tree. Comparing freeze-derived fields back to the freeze proves nothing,
    # so the authoritative comparison is reviewer-attested -> freeze.
    reviewed = review_artifact.get("reviewed_tree_digest")
    if not reviewed:
        raise Refuse("review carries no reviewed-tree evidence")
    if reviewed != freeze.get("tree_digest"):
        raise Refuse("review does not correspond to the subject's frozen tree")
    if os.path.realpath(review_artifact.get("reviewed_dir") or "") != os.path.realpath(
        subject.get("dir") or ""
    ):
        raise Refuse("review was performed against a different worktree")
    if review_artifact.get("models_registry_digest") != freeze.get("models_registry_digest"):
        raise Refuse("model registry changed between implementation and review")

    if live_head != freeze.get("head") or live_tree_digest != freeze.get("tree_digest"):
        raise Refuse("worktree changed after review")
    if review_artifact["verdict"] != "promote":
        raise Refuse(f"verdict {review_artifact['verdict']} cannot promote")

    # Everything checked above is model-free: digests, identity, the generation
    # CAS, independence. The verdict is the single place a model's judgement is
    # load-bearing, which makes it the thing worth attacking -- steer the
    # implementer, have it write text into the diff aimed at the reviewer, and
    # the gate approves itself.
    #
    # So the promotion path fails CLOSED when the reviewed material is addressed
    # at a reviewing agent. This does not detect prompt injection; nothing does.
    # It refuses to let a model's word carry a promotion when the input it read
    # was trying to produce that word.
    if reviewed_content:
        from .injection import blocks_promotion, scan, summarize

        found = scan(reviewed_content, "reviewed diff")
        if blocks_promotion(found):
            raise Refuse(
                summarize(found)
                + " Read the diff yourself and promote by hand if it is genuinely "
                "benign, or reject it."
            )

    # The reviewer must name the change it is approving. This does not prove
    # semantic review -- a hostile reviewer inside the sandbox can read the tree
    # and echo the names back -- but it does stop a reviewer that inspected
    # NOTHING from promoting, which controller-side hashing alone cannot.
    if expected_files is not None:
        named = review_artifact.get("reviewed_files")
        if not isinstance(named, list):
            raise Refuse("reviewer did not name the files it reviewed")
        if expected_files and not named:
            raise Refuse("reviewer did not name the files it reviewed")
        # Subset, not equality: the reviewer sees the whole dirty worktree (which
        # may carry earlier jobs' uncommitted work), so it may legitimately name
        # more than this subject changed. What it may NOT do is omit any of the
        # subject's own delta -- that would be approving a change it never looked at.
        missing = sorted(set(expected_files) - {str(f) for f in named})
        if missing:
            raise Refuse(
                f"reviewer did not cover the subject's change: {', '.join(missing)}"
            )
    if review_artifact.get("required_unmet"):
        raise Refuse("independence requirements unmet")

    blocking = blocking_findings(review_artifact.get("findings"), blocking_severities)
    if blocking:
        claims = "; ".join(str(f.get("claim", "?"))[:80] for f in blocking[:3])
        raise Refuse(
            f"review reports {len(blocking)} disqualifying finding(s) despite a "
            f"promote verdict: {claims}"
        )
    # Promotion is an ACCEPTANCE transition and nothing else: the process
    # outcome, the tree integrity and the frozen delta are all already decided
    # and none of them change here. Keeping `status` in step is what callers
    # branching on it expect, and it is derived from the same facts.
    subject["acceptance"] = {"state": jobstate.ACCEPTANCE_ACCEPTED}
    subject["status"] = jobstate.project_status(
        execution=(subject.get("execution") or {}).get(
            "outcome", jobstate.EXECUTION_COMPLETED),
        integrity=(subject.get("integrity") or {}).get(
            "outcome", jobstate.INTEGRITY_CLEAN),
        acceptance=jobstate.ACCEPTANCE_ACCEPTED,
        change=(subject.get("change") or {}).get("state", jobstate.CHANGE_FROZEN),
    )
    subject["review"] = review_artifact
    subject["generation"] = generation + 1
    # Promotion was the one durable result-record write that skipped schema
    # validation. A malformed preserved field could therefore turn an acceptance
    # decision into a record no reader could trust; validate the complete mutation
    # before the atomic write so a failure leaves the on-disk subject untouched.
    try:
        validate_result(subject)
    except Refuse as exc:
        raise Refuse(
            "promotion would produce an invalid result record that no reader can "
            "trust; the subject record on disk is unchanged. Inspect the subject "
            f"record at {subject_path} and repair or recover it before retrying "
            f"promotion. Schema error: {exc}"
        ) from exc
    atomic_write_json(subject_path, subject)
    return subject
