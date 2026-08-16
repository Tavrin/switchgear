from __future__ import annotations

from typing import Any

from .errors import Refuse
from .schema import validate
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


def promote(
    *,
    subject_path: str,
    review_artifact: dict[str, Any],
    live_head: str,
    live_tree_digest: str,
    generation: int,
) -> dict[str, Any]:
    validate(review_artifact, "review.schema.json")
    subject = read_json(subject_path)
    if subject.get("generation", 0) != generation:
        raise Refuse("concurrent promotion (generation mismatch)")
    if subject.get("status") != "awaiting_review":
        raise Refuse(f"subject is not awaiting_review ({subject.get('status')})")
    freeze = subject.get("freeze") or {}
    if review_artifact["subject_job"] != subject["job_id"]:
        raise Refuse("review subject_job mismatch")
    if review_artifact["subject_head"] != freeze.get("head"):
        raise Refuse("stale review: subject HEAD mismatch")
    if review_artifact["subject_tree_digest"] != freeze.get("tree_digest"):
        raise Refuse("stale review: tree digest mismatch")
    if review_artifact["subject_policy_digest"] != freeze.get("policy_digest"):
        raise Refuse("stale review: policy digest mismatch")
    if live_head != freeze.get("head") or live_tree_digest != freeze.get("tree_digest"):
        raise Refuse("worktree changed after review")
    if review_artifact["verdict"] != "promote":
        raise Refuse(f"verdict {review_artifact['verdict']} cannot promote")
    if review_artifact.get("required_unmet"):
        raise Refuse("independence requirements unmet")
    subject["status"] = "ok"
    subject["review"] = review_artifact
    subject["generation"] = generation + 1
    atomic_write_json(subject_path, subject)
    return subject
