from __future__ import annotations

import os
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
    expected_files: list[str] | None = None,
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
        if sorted({str(f) for f in named}) != sorted(set(expected_files)):
            raise Refuse(
                "reviewer's file list does not match the subject's actual change"
            )
    if review_artifact.get("required_unmet"):
        raise Refuse("independence requirements unmet")
    subject["status"] = "ok"
    subject["review"] = review_artifact
    subject["generation"] = generation + 1
    atomic_write_json(subject_path, subject)
    return subject
